"""
Archive Structure Workbench - Views.
"""
import csv
import io
import json
import secrets
from pathlib import Path

from django.conf import settings
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.contrib.auth.models import Group
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q, Avg, F
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST
from django.contrib.auth import logout as auth_logout

from .models import (
    AuditEvent, Collection, Decision, Document, ExtractionRun,
    Page, PageRegion, ProcessingArtifact, Review, ReviewTask, TableCandidate,
    TableExtraction,
)


# --- Permission helpers ---

def is_reviewer(user):
    return user.groups.filter(name__in=["Reviewer", "Curator", "Administrator"]).exists()


def is_curator(user):
    return user.groups.filter(name__in=["Curator", "Administrator"]).exists()


def is_admin(user):
    return user.groups.filter(name="Administrator").exists()


# --- Project access decorator ---

def require_project_access(view_func, project_kwarg="collection_id"):
    """Decorator that enforces project membership before rendering.

    Skips check for global administrators.
    """
    from functools import wraps
    from .policy import ProjectAccessPolicy

    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        # Global admins bypass
        if is_admin(request.user):
            return view_func(request, *args, **kwargs)

        project_id = kwargs.get(project_kwarg)
        if project_id is None:
            return view_func(request, *args, **kwargs)

        from .models import Collection
        project = Collection.objects.filter(pk=project_id).first()
        if not project:
            return view_func(request, *args, **kwargs)

        policy = ProjectAccessPolicy(user=request.user)
        if not policy.can_view(project):
            messages.error(request, _("You do not have access to this project."))
            return redirect("collection_list")

        return view_func(request, *args, **kwargs)
    return wrapper


# --- Audit helper ---

def log_audit(request, event_type, object_type="", object_id="", before=None, after=None):
    AuditEvent.objects.create(
        actor=request.user if request.user.is_authenticated else None,
        event_type=event_type,
        object_type=object_type,
        object_id=str(object_id),
        before=before,
        after=after,
    )


def logout_view(request):
    """Custom logout that accepts GET (for simple link-based logout)."""
    auth_logout(request)
    return redirect("login")


# --- Candidate assignment ---

def assign_candidate_order(task, first, second):
    """Atomically assign candidates X/Y to a review task."""
    if secrets.randbelow(2) == 0:
        task.candidate_x = first
        task.candidate_y = second
    else:
        task.candidate_x = second
        task.candidate_y = first
    task.save(update_fields=["candidate_x", "candidate_y"])


# --- Dashboard ---

@login_required
def dashboard(request):
    from .models import ProcessingJob, SourceDocument
    from .policy import ProjectAccessPolicy

    policy = ProjectAccessPolicy(user=request.user)
    visible_projects = policy.visible_projects()
    editable_projects = [project for project in visible_projects if policy.can_edit(project)]

    recent_documents = list(
        SourceDocument.objects.filter(
            collection__in=visible_projects,
            is_archived=False,
        ).select_related("collection", "active_document")[:6]
    )
    for source in recent_documents:
        source.latest_job = source.processing_jobs.first()

    active_states = ["queued", "submitting", "processing", "importing"]
    active_jobs = ProcessingJob.objects.filter(
        source_document__collection__in=visible_projects,
        state__in=active_states,
    ).select_related("source_document", "source_document__collection")[:6]
    attention_jobs = ProcessingJob.objects.filter(
        source_document__collection__in=visible_projects,
        state__in=["submission_uncertain", "interrupted", "partial", "failed"],
    ).select_related("source_document", "source_document__collection")[:6]

    tasks = ReviewTask.objects.filter(assigned_to=request.user)
    completed = tasks.filter(state="completed").count()
    in_progress = tasks.filter(state="in_progress").count()
    unassigned = tasks.filter(state="unassigned").count()
    needs_expert = tasks.filter(state="needs_expert").count()
    skipped = tasks.filter(state="skipped").count()
    total = tasks.count()

    return render(request, "workbench/dashboard.html", {
        "username": request.user.get_username(),
        "total_tasks": total,
        "completed": completed,
        "in_progress": in_progress,
        "unassigned": unassigned,
        "needs_expert": needs_expert,
        "skipped": skipped,
        "is_reviewer": is_reviewer(request.user),
        "is_curator": is_curator(request.user),
        "is_admin": is_admin(request.user),
        "can_add_document": bool(editable_projects),
        "recent_documents": recent_documents,
        "active_jobs": active_jobs,
        "attention_jobs": attention_jobs,
    })


# --- Collections ---

@login_required
def collection_list(request):
    from .policy import ProjectAccessPolicy
    policy = ProjectAccessPolicy(user=request.user)
    collections = list(policy.visible_projects())
    for collection in collections:
        collection.user_can_edit = policy.can_edit(collection)
    return render(request, "workbench/collection_list.html", {
        "collections": collections,
        "can_add_document": any(project.user_can_edit for project in collections),
    })


@login_required
def collection_detail(request, collection_id):
    from .policy import ProjectAccessPolicy
    collection = get_object_or_404(Collection, pk=collection_id)
    policy = ProjectAccessPolicy(user=request.user)
    if not policy.can_view(collection):
        messages.error(request, _("You do not have access to this project."))
        return redirect("collection_list")

    source_documents = list(
        collection.source_documents.filter(is_archived=False).select_related("active_document")
    )
    for source in source_documents:
        source.latest_job = source.processing_jobs.first()
    tables = TableCandidate.objects.filter(document__collection=collection)
    reviews = Review.objects.filter(review_task__table_candidate__document__collection=collection)

    return render(request, "workbench/collection_detail.html", {
        "collection": collection,
        "source_documents": source_documents,
        "can_edit_project": policy.can_edit(collection),
        "total_tables": tables.count(),
        "total_reviews": reviews.count(),
    })


# --- Documents ---

@login_required
def document_list(request):
    from .models import SourceDocument
    from .policy import ProjectAccessPolicy
    policy = ProjectAccessPolicy(user=request.user)
    visible_collections = policy.visible_projects()
    documents = SourceDocument.objects.filter(
        is_archived=False,
        collection__in=visible_collections,
    ).select_related("collection", "active_document")
    collection_id = request.GET.get("collection")
    if collection_id:
        documents = documents.filter(collection_id=collection_id)

    documents = list(documents)
    for document in documents:
        document.latest_job = document.processing_jobs.first()
    editable_projects = [project for project in visible_collections if policy.can_edit(project)]

    return render(request, "workbench/document_list.html", {
        "documents": documents,
        "can_add_document": bool(editable_projects),
    })


@login_required
def help_page(request):
    """Plain-language orientation for the primary document workflow."""
    return render(request, "workbench/help.html")


@login_required
def document_detail(request, document_id):
    from .policy import ProjectAccessPolicy
    document = get_object_or_404(
        Document.objects.select_related("collection", "processing_job__source_document"),
        pk=document_id,
    )
    policy = ProjectAccessPolicy(user=request.user)
    if not policy.can_view(document.collection):
        messages.error(request, _("You do not have access to this document."))
        return redirect("document_list")

    pages = list(document.pages.all())
    page_number = request.GET.get("page")
    try:
        requested_page = int(page_number) if page_number else 0
    except (TypeError, ValueError):
        requested_page = 0
    page = next((item for item in pages if item.page_number == requested_page), None)
    if page is None and pages:
        page = pages[0]

    regions = list(
        PageRegion.objects.filter(page__document=document)
        .select_related("page", "job")
    )
    for region in regions:
        region.overlay_width = region.right - region.left
        region.overlay_height = region.bottom - region.top
    page_regions = [region for region in regions if page and region.page_id == page.id]
    selected_region = None
    region_id = request.GET.get("region")
    if region_id:
        try:
            selected_region = next(
                (region for region in page_regions if region.id == int(region_id)),
                None,
            )
        except (TypeError, ValueError):
            selected_region = None

    page_text = ""
    processing_job = getattr(document, "processing_job", None)
    if processing_job:
        artifact = ProcessingArtifact.objects.filter(
            job=processing_job, artifact_type="page_text", page_number=page.page_number if page else None,
        ).first()
        if artifact:
            page_text = artifact.data.get("text", "") if isinstance(artifact.data, dict) else ""

    tables = document.tables.filter(page=page).select_related("page").prefetch_related("extractions") if page else []

    return render(request, "workbench/document_detail.html", {
        "document": document,
        "tables": tables,
        "pages": pages,
        "selected_page": page,
        "page_regions": page_regions,
        "selected_region": selected_region,
        "page_text": page_text,
        "processing_job": processing_job,
    })


# --- Reviews ---

@login_required
def review_list(request):
    from .policy import ProjectAccessPolicy
    policy = ProjectAccessPolicy(user=request.user)
    visible_projects = policy.visible_projects()

    # Only show tasks from projects the user can access
    tasks = ReviewTask.objects.filter(
        assigned_to=request.user,
        table_candidate__document__collection__in=visible_projects
    )
    if is_curator(request.user):
        tasks = ReviewTask.objects.filter(
            table_candidate__document__collection__in=visible_projects
        )

    return render(request, "workbench/review_list.html", {"tasks": tasks})


@login_required
def review_next(request):
    """Get the next unassigned or assigned review task."""
    task = (
        ReviewTask.objects
        .filter(assigned_to=request.user, state__in=["unassigned", "assigned"])
        .order_by("priority", "created_at")
        .first()
    )
    if not task:
        return redirect("review_list")

    return redirect("review_detail", task_id=task.id)


@login_required
def review_skip(request, task_id):
    """Skip a task for now (keeps it available, doesn't count as completed)."""
    from .policy import ProjectAccessPolicy
    task = get_object_or_404(ReviewTask, pk=task_id)
    policy = ProjectAccessPolicy(user=request.user)
    if not policy.can_access_task(task):
        return redirect("review_list")
    if task.assigned_to != request.user and not is_curator(request.user):
        return redirect("review_list")

    task.state = "skipped"
    task.save(update_fields=["state"])
    log_audit(request, "task_skipped", "ReviewTask", task.id)

    messages.info(request, _("Task skipped. It will remain available for later."))
    return redirect("review_list")


@login_required
def review_needs_expert(request, task_id):
    """Mark a task as needing expert review."""
    from .policy import ProjectAccessPolicy
    task = get_object_or_404(ReviewTask, pk=task_id)
    policy = ProjectAccessPolicy(user=request.user)
    if not policy.can_access_task(task):
        return redirect("review_list")
    if task.assigned_to != request.user and not is_curator(request.user):
        return redirect("review_list")

    task.state = "needs_expert"
    task.save(update_fields=["state"])
    log_audit(request, "task_needs_expert", "ReviewTask", task.id)

    messages.info(request, _("Task marked as needing expert review."))
    return redirect("review_list")


@login_required
def review_detail(request, task_id):
    from .policy import ProjectAccessPolicy
    task = get_object_or_404(ReviewTask, pk=task_id)

    # Check project access
    policy = ProjectAccessPolicy(user=request.user)
    if not policy.can_access_task(task):
        messages.error(request, _("You do not have access to this task."))
        return redirect("review_list")

    # Check if user is assigned or is a curator
    if task.assigned_to != request.user and not is_curator(request.user):
        return redirect("review_list")

    # Check if already reviewed
    existing_review = None
    if Review.objects.filter(review_task=task).exists():
        existing_review = Review.objects.get(review_task=task)

    # Get extractions for this table
    extractions = task.table_candidate.extractions.select_related("extraction_run")
    a_extraction = extractions.filter(extraction_run__profile="standard-docling").first()
    b_extraction = extractions.filter(extraction_run__profile="granite-table-crop").first()
    gt_extraction = extractions.filter(extraction_run__profile="ground-truth").first()

    # Ensure candidate ordering is stable
    if not task.candidate_x_id or not task.candidate_y_id:
        if a_extraction and b_extraction:
            with transaction.atomic():
                assign_candidate_order(task, a_extraction, b_extraction)

    # Determine candidates from task assignment
    if task.candidate_x_id and task.candidate_y_id:
        candidate_x = task.candidate_x
        candidate_y = task.candidate_y
    elif a_extraction and b_extraction:
        candidate_x = a_extraction
        candidate_y = b_extraction
    else:
        candidate_x = a_extraction
        candidate_y = b_extraction

    # Mark task as in progress
    if task.state not in ["in_progress", "completed", "needs_expert"]:
        task.state = "in_progress"
        task.assigned_to = request.user
        task.save(update_fields=["state", "assigned_to"])

    # Sanitize HTML for display
    import bleach
    allowed_tags = bleach.sanitizer.ALLOWED_TAGS | {"table", "tbody", "tr", "td", "th", "caption", "thead", "tfoot"}
    allowed_attrs = {"td": ["rowspan", "colspan"], "th": ["rowspan", "colspan"]}

    # Check for missing extractions
    x_missing = candidate_x is None or candidate_x.status == "missing" or not candidate_x.raw_html
    y_missing = candidate_y is None or candidate_y.status == "missing" or not candidate_y.raw_html

    return render(request, "workbench/review_detail.html", {
        "task": task,
        "table_candidate": task.table_candidate,
        "document": task.table_candidate.document,
        "candidate_x": candidate_x,
        "candidate_y": candidate_y,
        "candidate_x_html": bleach.clean(candidate_x.raw_html, tags=allowed_tags, attributes=allowed_attrs) if candidate_x and candidate_x.raw_html else "",
        "candidate_y_html": bleach.clean(candidate_y.raw_html, tags=allowed_tags, attributes=allowed_attrs) if candidate_y and candidate_y.raw_html else "",
        "x_missing": x_missing,
        "y_missing": y_missing,
        "existing_review": existing_review,
        "is_blinded": not existing_review,
        "has_gt": gt_extraction is not None and gt_extraction.raw_html,
        "gt_extraction": gt_extraction,
        "gt_html": bleach.clean(gt_extraction.raw_html, tags=allowed_tags, attributes=allowed_attrs) if gt_extraction and gt_extraction.raw_html else "",
        "is_curator": is_curator(request.user),
        "show_guidelines_link": True,
    })


@login_required
@require_POST
def review_submit(request, task_id):
    """Submit a review. Uses Post/Redirect/Get pattern."""
    from .policy import ProjectAccessPolicy
    task = get_object_or_404(ReviewTask, pk=task_id)

    # Check project access
    policy = ProjectAccessPolicy(user=request.user)
    if not policy.can_access_task(task):
        messages.error(request, _("You do not have access to this task."))
        return redirect("review_list")

    # Check if user is assigned or is a curator
    if task.assigned_to != request.user and not is_curator(request.user):
        return redirect("review_list")

    # Check for existing review (duplicate submission)
    existing = Review.objects.filter(review_task=task).first()
    if existing is not None:
        messages.info(
            request,
            _("This review was already submitted. Your original response has been preserved."),
        )
        return redirect("review_reveal", task_id=task.pk)

    # Get extractions from task assignment
    candidate_x = task.candidate_x
    candidate_y = task.candidate_y

    if not candidate_x or not candidate_y:
        messages.error(request, _("Missing extraction data for this table."))
        return redirect("review_detail", task_id=task_id)

    # Create review atomically
    with transaction.atomic():
        review = Review.objects.create(
            review_task=task,
            reviewer=request.user,
            candidate_x=candidate_x,
            candidate_y=candidate_y,
            candidate_x_score=int(request.POST.get("candidate_x_score", 0)),
            candidate_y_score=int(request.POST.get("candidate_y_score", 0)),
            preferred_result=request.POST.get("preferred_result", "equivalent"),
            structure_errors=request.POST.getlist("structure_errors"),
            text_errors=request.POST.getlist("text_errors"),
            comment=request.POST.get("comment", ""),
            confidence=request.POST.get("confidence", "medium"),
        )

        task.state = "completed"
        task.completed_at = review.created_at
        task.save(update_fields=["state", "completed_at"])

    log_audit(request, "review_created", "Review", review.id)

    messages.success(request, _("Review submitted successfully."))
    return redirect("review_reveal", task_id=task.pk)


@login_required
def review_reveal(request, task_id):
    """Show the reveal page after review submission."""
    from .policy import ProjectAccessPolicy
    task = get_object_or_404(ReviewTask, pk=task_id)

    # Check project access
    policy = ProjectAccessPolicy(user=request.user)
    if not policy.can_access_task(task):
        messages.error(request, _("You do not have access to this task."))
        return redirect("review_list")

    # Check if user is assigned or is a curator
    if task.assigned_to != request.user and not is_curator(request.user):
        return redirect("review_list")

    review = Review.objects.filter(review_task=task).first()
    if not review:
        return redirect("review_detail", task_id=task.pk)

    # Get ground truth
    gt_extraction = task.table_candidate.extractions.filter(
        extraction_run__profile="ground-truth"
    ).first()

    import bleach
    allowed_tags = bleach.sanitizer.ALLOWED_TAGS | {"table", "tbody", "tr", "td", "th", "caption", "thead", "tfoot"}
    allowed_attrs = {"td": ["rowspan", "colspan"], "th": ["rowspan", "colspan"]}

    return render(request, "workbench/review_reveal.html", {
        "task": task,
        "review": review,
        "gt_extraction": gt_extraction,
        "gt_html": bleach.clean(gt_extraction.raw_html, tags=allowed_tags, attributes=allowed_attrs) if gt_extraction and gt_extraction.raw_html else "",
        "has_gt": gt_extraction is not None and gt_extraction.raw_html,
        "is_curator": is_curator(request.user),
    })


@login_required
@require_POST
def review_post_reveal(request, task_id):
    """Add a post-reveal comment. Does not modify original scores."""
    from .policy import ProjectAccessPolicy
    task = get_object_or_404(ReviewTask, pk=task_id)
    policy = ProjectAccessPolicy(user=request.user)
    if not policy.can_access_task(task):
        return redirect("review_list")

    review = Review.objects.filter(review_task=task).first()

    if not review:
        return redirect("review_detail", task_id=task_id)

    review.post_reveal_comment = request.POST.get("post_reveal_comment", "")
    review.save(update_fields=["post_reveal_comment"])

    log_audit(request, "review_post_reveal", "Review", review.id, after={"post_reveal_comment": review.post_reveal_comment})

    messages.info(request, _("Post-reveal note saved."))
    return redirect("review_reveal", task_id=task.pk)


# --- Decisions ---

@login_required
@user_passes_test(is_curator)
def decision_list(request):
    from .policy import ProjectAccessPolicy
    policy = ProjectAccessPolicy(user=request.user)
    visible_projects = policy.visible_projects()
    tables = TableCandidate.objects.filter(
        document__collection__in=visible_projects
    ).select_related("decision", "document")
    return render(request, "workbench/decision_list.html", {"tables": tables})


@login_required
@user_passes_test(is_curator)
def decision_detail(request, table_id):
    from .policy import ProjectAccessPolicy
    table = get_object_or_404(TableCandidate, pk=table_id)
    policy = ProjectAccessPolicy(user=request.user)
    if not policy.can_view(table.document.collection):
        messages.error(request, _("You do not have access to this table."))
        return redirect("decision_list")

    decision = None
    if hasattr(table, "decision"):
        decision = table.decision
    extractions = table.extractions.select_related("extraction_run")
    review = None
    if hasattr(table, "review_task"):
        review = Review.objects.filter(review_task=table.review_task).first()

    return render(request, "workbench/decision_detail.html", {
        "table": table,
        "decision": decision,
        "extractions": extractions,
        "review": review,
    })


@login_required
@user_passes_test(is_curator)
@require_POST
def decision_submit(request, table_id):
    from .policy import ProjectAccessPolicy
    table = get_object_or_404(TableCandidate, pk=table_id)
    policy = ProjectAccessPolicy(user=request.user)
    if not policy.can_view(table.document.collection):
        messages.error(request, _("You do not have access to this table."))
        return redirect("decision_list")

    extraction_id = request.POST.get("selected_extraction_id")

    extraction = None
    if extraction_id:
        extraction = get_object_or_404(TableExtraction, pk=extraction_id)

    decision, created = Decision.objects.update_or_create(
        table_candidate=table,
        defaults={
            "selected_extraction": extraction,
            "decision": request.POST.get("decision", "accepted"),
            "decided_by": request.user,
            "reason": request.POST.get("reason", ""),
        },
    )

    log_audit(request, "decision_created", "Decision", decision.id)

    return redirect("decision_list")


# --- Curator Dashboard ---

@login_required
@user_passes_test(is_curator)
def curator_dashboard(request):
    from .policy import ProjectAccessPolicy
    policy = ProjectAccessPolicy(user=request.user)
    visible_projects = policy.visible_projects()
    collections = visible_projects

    total_documents = Document.objects.filter(
        is_archived=False, collection__in=visible_projects
    ).count()
    total_tables = TableCandidate.objects.filter(
        document__collection__in=visible_projects
    ).count()
    total_reviews = Review.objects.filter(
        review_task__table_candidate__document__collection__in=visible_projects
    ).count()
    pending_reviews = ReviewTask.objects.filter(
        state__in=["unassigned", "assigned", "in_progress"],
        table_candidate__document__collection__in=visible_projects
    ).count()

    # Preference distribution
    preferences = Review.objects.filter(
        review_task__table_candidate__document__collection__in=visible_projects
    ).values("preferred_result").annotate(count=Count("id"))

    # Error category totals
    error_counts = {}
    for review in Review.objects.filter(
        review_task__table_candidate__document__collection__in=visible_projects
    ).all():
        for err in review.structure_errors:
            error_counts[err] = error_counts.get(err, 0) + 1
        for err in review.text_errors:
            error_counts[err] = error_counts.get(err, 0) + 1

    return render(request, "workbench/curator_dashboard.html", {
        "collections": collections,
        "total_documents": total_documents,
        "total_tables": total_tables,
        "total_reviews": total_reviews,
        "pending_reviews": pending_reviews,
        "preferences": preferences,
        "error_counts": error_counts,
    })


@login_required
@user_passes_test(is_curator)
def technical_report(request):
    """Technical report with TEDS metrics."""
    from .policy import ProjectAccessPolicy
    policy = ProjectAccessPolicy(user=request.user)
    visible_projects = policy.visible_projects()
    runs = ExtractionRun.objects.filter(
        document__collection__in=visible_projects
    ).select_related("document")
    extractions = TableExtraction.objects.filter(
        table_candidate__document__collection__in=visible_projects
    ).select_related("table_candidate", "extraction_run")

    stats = {}
    for profile in ["standard-docling", "granite-table-crop"]:
        profile_extractions = extractions.filter(
            extraction_run__profile=profile,
            teds_s__isnull=False,
        )
        if profile_extractions.exists():
            stats[profile] = {
                "count": profile_extractions.count(),
                "mean_teds_s": profile_extractions.aggregate(Avg("teds_s"))["teds_s__avg"],
                "mean_teds": profile_extractions.aggregate(Avg("teds"))["teds__avg"],
                "failures": profile_extractions.filter(status="failed").count(),
            }

    return render(request, "workbench/technical_report.html", {
        "stats": stats,
        "extractions": extractions,
    })


# --- Exports ---

@login_required
@user_passes_test(is_curator)
def export_reviews(request):
    from .policy import ProjectAccessPolicy
    policy = ProjectAccessPolicy(user=request.user)
    visible_projects = policy.visible_projects()
    reviews = Review.objects.filter(
        review_task__table_candidate__document__collection__in=visible_projects
    ).select_related("review_task__table_candidate", "reviewer")
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="reviews.csv"'

    writer = csv.writer(response)
    writer.writerow([
        "table_id", "document", "reviewer", "preferred_result", "candidate_x_score",
        "candidate_y_score", "confidence", "comment", "structure_errors", "text_errors",
        "created_at",
    ])

    for r in reviews:
        writer.writerow([
            r.review_task.table_candidate.stable_table_id,
            r.review_task.table_candidate.document.external_id,
            r.reviewer.username,
            r.preferred_result,
            r.candidate_x_score,
            r.candidate_y_score,
            r.confidence,
            r.comment,
            json.dumps(r.structure_errors),
            json.dumps(r.text_errors),
            r.created_at.isoformat(),
        ])

    log_audit(request, "export_created", "Export", "reviews.csv")
    return response


@login_required
@user_passes_test(is_curator)
def export_decisions(request):
    from .policy import ProjectAccessPolicy
    policy = ProjectAccessPolicy(user=request.user)
    visible_projects = policy.visible_projects()
    decisions = Decision.objects.filter(
        table_candidate__document__collection__in=visible_projects
    ).select_related("table_candidate", "decided_by")
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="decisions.csv"'

    writer = csv.writer(response)
    writer.writerow(["table_id", "document", "decision", "decided_by", "reason", "created_at"])

    for d in decisions:
        writer.writerow([
            d.table_candidate.stable_table_id,
            d.table_candidate.document.external_id,
            d.decision,
            d.decided_by.username,
            d.reason,
            d.created_at.isoformat(),
        ])

    log_audit(request, "export_created", "Export", "decisions.csv")
    return response


@login_required
@user_passes_test(is_curator)
def export_summary(request):
    """Export benchmark summary JSON."""
    from .policy import ProjectAccessPolicy
    policy = ProjectAccessPolicy(user=request.user)
    visible_projects = policy.visible_projects()
    extractions = TableExtraction.objects.filter(
        table_candidate__document__collection__in=visible_projects
    ).select_related("table_candidate", "extraction_run")
    reviews = Review.objects.filter(
        review_task__table_candidate__document__collection__in=visible_projects
    ).select_related("review_task__table_candidate")

    summary = {
        "exported_at": AuditEvent.objects.latest("created_at").created_at.isoformat() if AuditEvent.objects.exists() else "",
        "collection": "DP-Bench full tables",
        "total_tables": TableCandidate.objects.filter(
            document__collection__in=visible_projects
        ).count(),
        "total_reviews": reviews.count(),
        "profiles": {},
    }

    for profile in ["standard-docling", "granite-table-crop"]:
        p_extractions = extractions.filter(extraction_run__profile=profile, teds_s__isnull=False)
        if p_extractions.exists():
            summary["profiles"][profile] = {
                "count": p_extractions.count(),
                "mean_teds_s": p_extractions.aggregate(Avg("teds_s"))["teds_s__avg"],
                "mean_teds": p_extractions.aggregate(Avg("teds"))["teds__avg"],
            }

    response = HttpResponse(content_type="application/json")
    response["Content-Disposition"] = 'attachment; filename="benchmark-summary.json"'
    response.write(json.dumps(summary, indent=2))

    log_audit(request, "export_created", "Export", "benchmark-summary.json")
    return response


# --- Guidelines ---

@login_required
def guidelines(request):
    return render(request, "workbench/guidelines.html")


# --- User Settings ---

@login_required
def user_settings(request):
    """User profile and language settings page."""
    from django.utils.crypto import get_random_string
    from workbench.models import UserPreferences, ApiToken
    

    prefs = UserPreferences.get_or_create_for_user(request.user)

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "update_preferences":
            prefs.ui_language = request.POST.get("ui_language", prefs.ui_language)
            prefs.timezone = request.POST.get("timezone", prefs.timezone)
            prefs.guided_explanations = "guided_explanations" in request.POST
            prefs.save()
            from django.contrib import messages
            messages.success(request, _("Preferences saved."))

        elif action == "create_api_token":
            token_name = request.POST.get("token_name", "API Token").strip()
            if token_name:
                import secrets
                raw_token = secrets.token_urlsafe(36)
                ApiToken.objects.create(
                    user=request.user,
                    name=token_name,
                    token_prefix=raw_token[:8],
                    token_hash=ApiToken.hash_token(raw_token),
                    scopes=["projects:read", "documents:read", "tasks:read",
                            "reviews:write", "statistics:read"],
                )
                from django.contrib import messages
                messages.success(
                    request,
                    _("Token created: %(token)s. Copy it now — it won't be shown again.") % {"token": raw_token},
                )

        elif action == "revoke_api_token":
            token_id = request.POST.get("token_id")
            token = ApiToken.objects.filter(user=request.user, pk=token_id).first()
            if token:
                from django.utils import timezone
                token.revoked_at = timezone.now()
                token.save(update_fields=["revoked_at"])
                from django.contrib import messages
                messages.success(request, _("Token revoked."))
            else:
                from django.contrib import messages
                messages.error(request, _("Token not found."))
        elif action == "change_password":
            old_password = request.POST.get("old_password", "")
            new_password = request.POST.get("new_password", "")
            confirm_password = request.POST.get("confirm_password", "")

            if not request.user.check_password(old_password):
                from django.contrib import messages
                messages.error(request, _("Current password is incorrect."))
            elif new_password != confirm_password:
                from django.contrib import messages
                messages.error(request, _("New passwords do not match."))
            elif len(new_password) < 8:
                from django.contrib import messages
                messages.error(request, _("New password must be at least 8 characters."))
            else:
                request.user.set_password(new_password)
                request.user.save()
                from django.contrib import messages
                messages.success(request, _("Password changed."))

        return redirect("user_settings")

    # Get user's existing tokens
    tokens = ApiToken.objects.filter(user=request.user).order_by("-created_at")
    from django.utils import timezone
    now = timezone.now()

    # Timezone choices as (code, display_name) tuples
    timezone_choices = [
        ("UTC", "UTC"),
        ("Europe/Vienna", "Vienna (CET/CEST)"),
        ("Europe/Berlin", "Berlin (CET/CEST)"),
        ("Europe/Zurich", "Zurich (CET/CEST)"),
        ("America/New_York", "New York (EST/EDT)"),
    ]

    return render(request, "workbench/user_settings.html", {
        "prefs": prefs,
        "tokens": tokens,
        "now": now,
        "timezone_choices": timezone_choices,
    })


# --- Static assets ---

def serve_htmx(request):
    """Serve self-hosted HTMX."""
    htmx_path = Path(settings.BASE_DIR) / "static" / "htmx.min.js"
    if htmx_path.exists():
        return HttpResponse(htmx_path.read_text(), content_type="application/javascript")
    # Fallback to CDN
    return redirect("https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js")


# --- Upload / Processing ---

@login_required
def document_new(request, project_id=None):
    """Choose an editable project, upload a PDF, and start processing."""
    from .policy import ProjectAccessPolicy
    from .services import DocumentIngestionService, IngestionError
    from .models import ProcessingPreset

    policy = ProjectAccessPolicy(user=request.user)
    editable_projects = [
        project for project in policy.visible_projects()
        if policy.can_edit(project)
    ]
    editable_project_ids = {project.pk for project in editable_projects}

    requested_project_id = project_id or request.POST.get("project") or request.GET.get("project")
    if not requested_project_id and len(editable_projects) == 1:
        requested_project_id = editable_projects[0].pk

    collection = None
    if requested_project_id:
        try:
            requested_project_id = int(requested_project_id)
        except (TypeError, ValueError):
            requested_project_id = None
        if requested_project_id in editable_project_ids:
            collection = next(
                project for project in editable_projects if project.pk == requested_project_id
            )

    for project in editable_projects:
        project.is_selected = collection is not None and project.pk == collection.pk

    presets = ProcessingPreset.objects.filter(is_active=True)
    standard_preset = presets.filter(slug="quick-extraction").first() or presets.first()
    advanced_presets = presets.exclude(pk=standard_preset.pk) if standard_preset else presets.none()
    selected_preset_slug = request.POST.get(
        "preset",
        standard_preset.slug if standard_preset else "",
    )

    if request.method == "POST":
        if collection is None:
            messages.error(request, _("Choose a project you can edit."))
        elif "file" not in request.FILES:
            messages.error(request, _("Choose a PDF to upload."))
        else:
            uploaded_file = request.FILES["file"]
            service = DocumentIngestionService(user=request.user, policy=policy)
            try:
                job = service.create_upload(
                    project=collection,
                    uploaded_file=uploaded_file,
                    preset_slug=selected_preset_slug,
                )
            except IngestionError as error:
                messages.error(request, str(error))
            else:
                log_audit(request, "document_uploaded", "SourceDocument", job.source_document_id)
                messages.success(
                    request,
                    _("Upload received. The document has been queued for analysis."),
                )
                return redirect("job_status", job_id=job.pk)

    return render(request, "workbench/document_new.html", {
        "editable_projects": editable_projects,
        "selected_project": collection,
        "standard_preset": standard_preset,
        "advanced_presets": advanced_presets,
        "selected_preset_slug": selected_preset_slug,
    })


@login_required
def project_process(request, project_id):
    """Compatibility route for old project-specific upload links."""
    if request.method == "GET":
        return redirect(f"{reverse('document_new')}?project={project_id}")
    return document_new(request, project_id=project_id)


@login_required
def job_status(request, job_id):
    """Show processing job status with HTMX polling."""
    from .policy import ProjectAccessPolicy
    from .models import ProcessingJob

    job = get_object_or_404(ProcessingJob, pk=job_id)
    policy = ProjectAccessPolicy(user=request.user)
    if not policy.can_access_job(job):
        messages.error(request, _("You do not have access to this job."))
        return redirect("dashboard")

    context = {
        "job": job,
        "source_document": job.source_document,
    }

    # HTMX partial refresh (exactly one replaceable root block)
    if request.headers.get("HX-Request"):
        return render(request, "workbench/_job_status_block.html", context)

    return render(request, "workbench/job_status.html", context)


# --- Secure artifact serving ---

def _resolve_artifact_path(relative_path: str) -> Path:
    """Resolve and validate an artifact file path.

    Uses explicit Boolean containment check.
    Raises Http404 if path escapes the artifacts base directory.
    """
    from django.http import Http404

    artifacts_base = Path(getattr(settings, "ARTIFACTS_BASE_DIR", "/var/lib/dsw/artifacts"))
    file_path = artifacts_base / relative_path

    base = artifacts_base.resolve()
    resolved = file_path.resolve()

    if not resolved.is_relative_to(base):
        raise Http404("Invalid file path.")

    if not resolved.exists():
        raise Http404("File not found on disk.")

    return resolved


@login_required
def page_image(request, page_id):
    """Serve a page image with permission check."""
    from .policy import ProjectAccessPolicy
    from django.http import FileResponse, Http404

    page = get_object_or_404(Page, pk=page_id)
    policy = ProjectAccessPolicy(user=request.user)
    if not policy.can_view(page.document.collection):
        messages.error(request, _("You do not have access to this document."))
        return redirect("document_list")

    if not page.image_path:
        raise Http404("No image available for this page.")

    resolved = _resolve_artifact_path(page.image_path)
    return FileResponse(open(resolved, "rb"), content_type="image/png")


@login_required
def table_crop(request, table_id):
    """Serve a table crop image with permission check."""
    from .policy import ProjectAccessPolicy
    from django.http import FileResponse, Http404

    table = get_object_or_404(TableCandidate, pk=table_id)
    policy = ProjectAccessPolicy(user=request.user)
    if not policy.can_view(table.document.collection):
        messages.error(request, _("You do not have access to this document."))
        return redirect("document_list")

    if not table.crop_path:
        raise Http404("No crop available for this table.")

    resolved = _resolve_artifact_path(table.crop_path)
    return FileResponse(open(resolved, "rb"), content_type="image/png")


@login_required
def artifact_content(request, artifact_id):
    """Serve a processing artifact with permission check."""
    from .policy import ProjectAccessPolicy
    from .models import ProcessingArtifact
    from django.http import FileResponse, Http404, JsonResponse

    artifact = get_object_or_404(ProcessingArtifact, pk=artifact_id)
    policy = ProjectAccessPolicy(user=request.user)
    if not policy.can_access_job(artifact.job):
        messages.error(request, _("You do not have access to this artifact."))
        return redirect("dashboard")

    # If it's JSON data, return directly
    if artifact.data and not artifact.file_path:
        return JsonResponse(artifact.data)

    if not artifact.file_path:
        raise Http404("No file for this artifact.")

    resolved = _resolve_artifact_path(artifact.file_path)

    # Determine content type
    content_type = "application/octet-stream"
    if resolved.suffix == ".json":
        content_type = "application/json"
    elif resolved.suffix in (".png", ".jpg", ".jpeg"):
        content_type = f"image/{resolved.suffix.lstrip('.')}"
    elif resolved.suffix == ".html":
        content_type = "text/html"
    elif resolved.suffix == ".txt":
        content_type = "text/plain"

    return FileResponse(open(resolved, "rb"), content_type=content_type)
