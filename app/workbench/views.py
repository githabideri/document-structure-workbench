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
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST
from django.contrib.auth import logout as auth_logout

from .models import (
    AuditEvent, Collection, Decision, Document, ExtractionRun,
    Page, Review, ReviewTask, TableCandidate, TableExtraction,
)


# --- Permission helpers ---

def is_reviewer(user):
    return user.groups.filter(name__in=["Reviewer", "Curator", "Administrator"]).exists()


def is_curator(user):
    return user.groups.filter(name__in=["Curator", "Administrator"]).exists()


def is_admin(user):
    return user.groups.filter(name="Administrator").exists()


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
    })


# --- Collections ---

@login_required
def collection_list(request):
    collections = Collection.objects.all()
    return render(request, "workbench/collection_list.html", {"collections": collections})


@login_required
def collection_detail(request, collection_id):
    collection = get_object_or_404(Collection, pk=collection_id)
    documents = collection.documents.filter(is_archived=False)
    tables = TableCandidate.objects.filter(document__collection=collection)
    reviews = Review.objects.filter(review_task__table_candidate__document__collection=collection)

    return render(request, "workbench/collection_detail.html", {
        "collection": collection,
        "documents": documents,
        "total_tables": tables.count(),
        "total_reviews": reviews.count(),
    })


# --- Documents ---

@login_required
def document_list(request):
    documents = Document.objects.filter(is_archived=False)
    collection_id = request.GET.get("collection")
    if collection_id:
        documents = documents.filter(collection_id=collection_id)

    return render(request, "workbench/document_list.html", {"documents": documents})


@login_required
def document_detail(request, document_id):
    document = get_object_or_404(Document, pk=document_id)
    tables = document.tables.select_related("page").prefetch_related("extractions")
    pages = document.pages.all()

    return render(request, "workbench/document_detail.html", {
        "document": document,
        "tables": tables,
        "pages": pages,
    })


# --- Reviews ---

@login_required
def review_list(request):
    tasks = ReviewTask.objects.filter(assigned_to=request.user)
    if is_curator(request.user):
        tasks = ReviewTask.objects.all()

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
    task = get_object_or_404(ReviewTask, pk=task_id)
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
    task = get_object_or_404(ReviewTask, pk=task_id)
    if task.assigned_to != request.user and not is_curator(request.user):
        return redirect("review_list")

    task.state = "needs_expert"
    task.save(update_fields=["state"])
    log_audit(request, "task_needs_expert", "ReviewTask", task.id)

    messages.info(request, _("Task marked as needing expert review."))
    return redirect("review_list")


@login_required
def review_detail(request, task_id):
    task = get_object_or_404(ReviewTask, pk=task_id)

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
    task = get_object_or_404(ReviewTask, pk=task_id)

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
    task = get_object_or_404(ReviewTask, pk=task_id)

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
    task = get_object_or_404(ReviewTask, pk=task_id)
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
    tables = TableCandidate.objects.select_related("decision", "document")
    return render(request, "workbench/decision_list.html", {"tables": tables})


@login_required
@user_passes_test(is_curator)
def decision_detail(request, table_id):
    table = get_object_or_404(TableCandidate, pk=table_id)
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
    table = get_object_or_404(TableCandidate, pk=table_id)
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
    collections = Collection.objects.all()
    total_documents = Document.objects.filter(is_archived=False).count()
    total_tables = TableCandidate.objects.count()
    total_reviews = Review.objects.count()
    pending_reviews = ReviewTask.objects.filter(state__in=["unassigned", "assigned", "in_progress"]).count()

    # Preference distribution
    preferences = Review.objects.values("preferred_result").annotate(count=Count("id"))

    # Error category totals
    error_counts = {}
    for review in Review.objects.all():
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
    runs = ExtractionRun.objects.select_related("document")
    extractions = TableExtraction.objects.select_related("table_candidate", "extraction_run")

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
    reviews = Review.objects.select_related("review_task__table_candidate", "reviewer")
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
    decisions = Decision.objects.select_related("table_candidate", "decided_by")
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
    extractions = TableExtraction.objects.select_related("table_candidate", "extraction_run")
    reviews = Review.objects.select_related("review_task__table_candidate")

    summary = {
        "exported_at": AuditEvent.objects.latest("created_at").created_at.isoformat() if AuditEvent.objects.exists() else "",
        "collection": "DP-Bench full tables",
        "total_tables": TableCandidate.objects.count(),
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
