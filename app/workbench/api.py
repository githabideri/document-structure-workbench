"""
Archive Structure Workbench - REST API.
Simple JSON API for automated testing and agent integration.

Auth: Send X-API-Key header with the key from settings.
No auth needed for GET endpoints. POST endpoints require the key.
"""
import json
from functools import wraps
from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.shortcuts import get_object_or_404
from django.db import transaction
from .models import (
    Collection, Document, Page, TableCandidate,
    ExtractionRun, TableExtraction, ReviewTask, Review, Decision,
)


def api_key_required(func):
    """Decorator that checks X-API-Key header for POST endpoints."""
    @wraps(func)
    def wrapper(request, *args, **kwargs):
        key = request.headers.get("X-API-Key", "")
        expected = getattr(settings, "API_KEY", "")
        if not expected:
            return JsonResponse(
                {"error": "API key not configured"},
                status=400,
            )
        if not key or key != expected:
            return JsonResponse({"error": "Invalid or missing X-API-Key"}, status=401)
        return func(request, *args, **kwargs)
    return wrapper


def _serialize_task(task):
    """Serialize a ReviewTask to dict."""
    doc = task.table_candidate.page.document
    x_label = task.candidate_x.extraction_run.profile if task.candidate_x else None
    y_label = task.candidate_y.extraction_run.profile if task.candidate_y else None
    return {
        "id": task.pk,
        "table_candidate_id": task.table_candidate.pk,
        "external_id": doc.external_id,
        "table_id": task.table_candidate.stable_table_id,
        "page_number": task.table_candidate.page.page_number,
        "status": task.state,
        "candidate_x_run": x_label,
        "candidate_y_run": y_label,
        "has_review": task.state == "completed",
    }


def _serialize_extraction(extraction, label):
    """Serialize a TableExtraction to dict."""
    return {
        "label": label,
        "run_name": extraction.extraction_run.profile,
        "model": extraction.extraction_run.profile,
        "status": extraction.status,
        "rows": extraction.rows,
        "cols": extraction.columns,
        "cells": extraction.normalized_cells,
        "html_preview": (extraction.raw_html or "")[:500],
        "otsl_preview": (extraction.raw_otsl or "")[:500],
    }


@require_http_methods(["GET"])
def api_health(request):
    """Minimal health endpoint — release SHA + database check.

    Returns 200 with release SHA when healthy.
    Returns 500 when release file is missing or database is unreachable.
    """
    from pathlib import Path
    from django.db import connection

    release_sha = None
    release_file = Path(getattr(settings, "RELEASE_FILE", ""))
    if release_file and release_file.exists():
        release_sha = release_file.read_text().strip()

    database_ok = False
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        database_ok = True
    except Exception:
        pass

    if not release_sha or not database_ok:
        return JsonResponse({
            "status": "error",
            "release": release_sha,
            "database": "ok" if database_ok else "unreachable",
        }, status=500)

    return JsonResponse({
        "status": "ok",
        "release": release_sha,
        "database": "ok",
    })


@require_http_methods(["GET"])
def api_status(request):
    """API health check and summary."""
    return JsonResponse({
        "status": "ok",
        "version": "1.0",
        "collections": Collection.objects.count(),
        "documents": Document.objects.count(),
        "tables": TableCandidate.objects.count(),
        "tasks": ReviewTask.objects.count(),
        "reviews": Review.objects.count(),
        "decisions": Decision.objects.count(),
    })


@require_http_methods(["GET"])
def api_collections(request):
    """List all collections."""
    collections = []
    for c in Collection.objects.all():
        collections.append({
            "id": c.pk,
            "name": c.name,
            "description": c.description,
            "documents": c.documents.count(),
            "tables": TableCandidate.objects.filter(page__document__collection=c).count(),
        })
    return JsonResponse({"collections": collections})


@require_http_methods(["GET"])
def api_collection_detail(request, collection_id):
    """Get collection detail."""
    collection = get_object_or_404(Collection, pk=collection_id)
    documents = []
    for doc in collection.documents.all():
        tables = []
        for tc in TableCandidate.objects.filter(page__document=doc):
            extractions = []
            for ext in TableExtraction.objects.filter(table_candidate=tc):
                label = "ground_truth" if ext.run.name == "ground_truth" else ext.run.name
                extractions.append(_serialize_extraction(ext, label))
            tables.append({
                "id": tc.pk,
                "stable_table_id": tc.stable_table_id,
                "page_number": tc.page.page_number,
                "extractions": extractions,
            })
        documents.append({
            "id": doc.pk,
            "external_id": doc.external_id,
            "tables": tables,
        })
    return JsonResponse({
        "id": collection.pk,
        "name": collection.name,
        "description": collection.description,
        "documents": documents,
    })


@require_http_methods(["GET"])
def api_tasks(request):
    """List all review tasks. Supports ?status=unreviewed/reviewed/skipped/expert."""
    tasks = ReviewTask.objects.select_related(
        "table_candidate", "table_candidate__page",
        "table_candidate__page__document",
    )

    status_filter = request.GET.get("status")
    if status_filter == "unreviewed":
        tasks = tasks.filter(state__in=["unassigned", "in_progress"])
    elif status_filter == "reviewed":
        tasks = tasks.filter(state="completed")
    elif status_filter == "skipped":
        tasks = tasks.filter(state="skipped")
    elif status_filter == "expert":
        tasks = tasks.filter(priority__gt=5)

    result = [_serialize_task(t) for t in tasks]
    return JsonResponse({"tasks": result, "count": len(result)})


@require_http_methods(["GET"])
def api_task_detail(request, task_id):
    """Get task detail with extractions."""
    task = get_object_or_404(ReviewTask, pk=task_id)

    # Get extractions for candidates X and Y
    x_extraction = None
    y_extraction = None

    if task.candidate_x:
        x_extraction = _serialize_extraction(task.candidate_x, task.candidate_x.extraction_run.profile)

    if task.candidate_y:
        y_extraction = _serialize_extraction(task.candidate_y, task.candidate_y.extraction_run.profile)

    # Get ground truth
    gt = TableExtraction.objects.filter(
        table_candidate=task.table_candidate,
        extraction_run__profile="ground_truth"
    ).first()
    ground_truth = _serialize_extraction(gt, "ground_truth") if gt else None

    # Get existing review if any
    review = None
    existing = Review.objects.filter(review_task=task).first()
    if existing:
        review = {
            "id": existing.pk,
            "score_x": existing.candidate_x_score,
            "score_y": existing.candidate_y_score,
            "preference": existing.preferred_result,
            "confidence": existing.confidence,
            "notes": existing.comment,
            "post_reveal_comment": existing.post_reveal_comment,
        }

    return JsonResponse({
        "task": _serialize_task(task),
        "candidate_x": x_extraction,
        "candidate_y": y_extraction,
        "ground_truth": ground_truth,
        "review": review,
    })


@csrf_exempt
@require_http_methods(["POST"])
@api_key_required
def api_task_submit(request, task_id):
    """Submit a review for a task (API version)."""
    task = get_object_or_404(ReviewTask, pk=task_id)

    if task.state == "completed":
        return JsonResponse({"error": "Already reviewed"}, status=400)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, TypeError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    score_x = data.get("score_x")
    score_y = data.get("score_y")
    preference = data.get("preference")
    confidence = data.get("confidence", "medium")
    notes = data.get("notes", "")
    error_categories = data.get("error_categories", [])

    # Validation
    if score_x is None or score_y is None:
        return JsonResponse({"error": "score_x and score_y required"}, status=400)
    if preference not in ("x", "y", "tie"):
        return JsonResponse({"error": "preference must be x, y, or tie"}, status=400)

    with transaction.atomic():
        # Create review
        review = Review.objects.create(
            review_task=task,
            reviewer=None,  # API reviews are anonymous
            candidate_x=task.candidate_x,
            candidate_y=task.candidate_y,
            candidate_x_score=score_x,
            candidate_y_score=score_y,
            preferred_result=preference,
            confidence=confidence,
            comment=notes,
        )

        # Create decisions for error categories
        for cat in error_categories:
            Decision.objects.create(
                table_candidate=task.table_candidate,
                reviewer=None,
                decision_type=cat.get("type", "issue"),
                severity=cat.get("severity", "medium"),
                notes=cat.get("notes", ""),
            )

        # Update task status
        task.state = "completed"
        task.save()

    return JsonResponse({
        "status": "ok",
        "review_id": review.pk,
        "message": "Review submitted",
    })


@csrf_exempt
@require_http_methods(["POST"])
@api_key_required
def api_task_skip(request, task_id):
    """Skip a task."""
    task = get_object_or_404(ReviewTask, pk=task_id)
    task.state = "skipped"
    task.save()
    return JsonResponse({"status": "ok", "message": "Task skipped"})


@csrf_exempt
@require_http_methods(["POST"])
@api_key_required
def api_task_flag_expert(request, task_id):
    """Flag a task for expert review."""
    task = get_object_or_404(ReviewTask, pk=task_id)
    task.priority = 9
    task.save()
    return JsonResponse({"status": "ok", "message": "Flagged for expert review"})


@require_http_methods(["GET"])
def api_statistics(request):
    """Get review statistics."""
    total = ReviewTask.objects.count()
    reviewed = ReviewTask.objects.filter(state="completed").count()
    skipped = ReviewTask.objects.filter(state="skipped").count()
    needs_expert = ReviewTask.objects.filter(priority__gt=5).count()

    from django.db.models import Avg
    reviews = Review.objects.all()
    avg_score_x = reviews.aggregate(avg_x=Avg("candidate_x_score"))["avg_x"] or 0
    avg_score_y = reviews.aggregate(avg_y=Avg("candidate_y_score"))["avg_y"] or 0
    pref_x = reviews.filter(preferred_result="x").count()
    pref_y = reviews.filter(preferred_result="y").count()
    pref_tie = reviews.filter(preferred_result="tie").count()

    return JsonResponse({
        "total_tasks": total,
        "reviewed": reviewed,
        "unreviewed": total - reviewed - skipped,
        "skipped": skipped,
        "needs_expert": needs_expert,
        "avg_score_x": round(avg_score_x, 2),
        "avg_score_y": round(avg_score_y, 2),
        "preference_x": pref_x,
        "preference_y": pref_y,
        "preference_tie": pref_tie,
    })
