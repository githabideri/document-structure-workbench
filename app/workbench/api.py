"""
Secure REST API v1 — Token authentication, scoped access, audit trail.

Authentication:
  - All endpoints except /api/v1/health/ require a valid ApiToken.
  - Token sent via Authorization: Bearer <raw_token> header.
  - Token is validated (not expired, not revoked, owner active).
  - Token scopes are checked per endpoint.

Authorization:
  - Project-scoped endpoints require membership in the project.
  - Reviewer tokens cannot access ground truth or model identities before reveal.
  - Service accounts are restricted by their assigned scopes.

Blinding:
  - Review task detail returns candidate X/Y extractions without model identity.
  - Ground truth is hidden until a review is submitted.
  - Model identities are hidden until reveal.

Audit:
  - Every request gets a unique request_id (UUID4).
  - Authenticated requests log AuditEvent entries for mutations.
"""
import hashlib
import json
import uuid
from functools import wraps

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.shortcuts import get_object_or_404
from django.db import transaction
from django.utils import timezone

from .models import (
    ApiToken, AuditEvent, Collection, Decision, Document,
    ExtractionRun, Page, ProcessingArtifact, ProcessingJob,
    ProcessingPreset, ProjectMembership, Review, ReviewTask,
    ServiceAccount, SourceDocument, TableCandidate, TableExtraction,
    UserPreferences,
)


# ---------------------------------------------------------------------------
# Request ID middleware (lightweight, works with decorator pattern)
# ---------------------------------------------------------------------------

def _generate_request_id():
    """Generate a UUID4 request ID."""
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Token authentication
# ---------------------------------------------------------------------------

def _authenticate_token(request):
    """
    Authenticate via Bearer token. Returns (token, error_response).
    If token is valid, returns (token, None).
    If invalid, returns (None, JsonResponse error).
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None, JsonResponse(
            {"error": "Missing or invalid Authorization header. Use: Bearer <token>"},
            status=401,
        )

    raw_token = auth_header[7:].strip()
    if not raw_token:
        return None, JsonResponse(
            {"error": "Empty token"},
            status=401,
        )

    token_obj = ApiToken.verify_token(raw_token)
    if token_obj is None:
        return None, JsonResponse(
            {"error": "Invalid token"},
            status=401,
        )

    if not token_obj.is_valid():
        if token_obj.revoked_at:
            return None, JsonResponse({"error": "Token has been revoked"}, status=401)
        if token_obj.expires_at and token_obj.expires_at < timezone.now():
            return None, JsonResponse({"error": "Token has expired"}, status=401)
        return None, JsonResponse({"error": "Token owner is inactive"}, status=401)

    # Touch last_used_at (throttled in production via middleware)
    token_obj.touch()

    return token_obj, None


def require_scope(*required_scopes):
    """Decorator that checks the token has the required scopes."""
    def decorator(func):
        @wraps(func)
        def wrapper(request, *args, **kwargs):
            request_id = _generate_request_id()
            request._request_id = request_id  # noqa: SLF001

            token, error = _authenticate_token(request)
            if error:
                return error

            # Check scopes
            token_scopes = set(token.scopes or [])
            missing = [s for s in required_scopes if s not in token_scopes]
            if missing:
                return JsonResponse(
                    {
                        "error": "Insufficient scopes",
                        "required": required_scopes,
                        "missing": missing,
                        "token_name": token.name,
                    },
                    status=403,
                )

            # Attach token and identity to request for downstream use
            request._api_token = token  # noqa: SLF001
            request._api_identity = (
                token.user if token.user_id else token.service_account
            )  # noqa: SLF001

            return func(request, *args, **kwargs)
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Project access — use ProjectAccessPolicy (no legacy helpers)
# ---------------------------------------------------------------------------

# All project access checks use ProjectAccessPolicy.
# Legacy _get_user_membership and _check_project_access have been removed.
# See workbench/policy.py for the centralized authorization logic.


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _serialize_task_blinded(task, token):
    """Serialize a ReviewTask with blinding enforced for reviewers."""
    doc = task.table_candidate.document
    has_review = Review.objects.filter(review_task=task).exists()

    result = {
        "id": task.pk,
        "table_candidate_id": task.table_candidate.pk,
        "external_id": doc.external_id,
        "table_id": task.table_candidate.stable_table_id,
        "page_number": task.table_candidate.page.page_number if task.table_candidate.page else None,
        "state": task.state,
        "priority": task.priority,
        "has_review": has_review,
        "candidate_x_id": task.candidate_x_id,
        "candidate_y_id": task.candidate_y_id,
    }

    # Model identities are hidden until review is submitted
    if has_review:
        if task.candidate_x:
            result["candidate_x_profile"] = task.candidate_x.extraction_run.profile
        if task.candidate_y:
            result["candidate_y_profile"] = task.candidate_y.extraction_run.profile

    return result


def _serialize_extraction_blinded(extraction, label, reveal=False):
    """Serialize a TableExtraction without model identity unless revealed."""
    result = {
        "id": extraction.pk,
        "label": label,
        "status": extraction.status,
        "rows": extraction.rows,
        "columns": extraction.columns,
        "normalized_cells": extraction.normalized_cells,
    }

    if reveal:
        result["profile"] = extraction.extraction_run.profile
        result["software_versions"] = extraction.extraction_run.software_versions
        result["git_commit"] = extraction.extraction_run.git_commit
        result["teds"] = extraction.teds
        result["teds_s"] = extraction.teds_s

    return result


# ---------------------------------------------------------------------------
# Health endpoint (anonymous)
# ---------------------------------------------------------------------------

@require_http_methods(["GET"])
def api_health(request):
    """Minimal health endpoint — release SHA + database check.

    Returns 200 with release SHA when healthy.
    Returns 500 when release file is missing or database is unreachable.
    """
    from pathlib import Path
    from django.db import connection

    release_sha = None
    release_file_path = getattr(settings, "RELEASE_FILE", "")
    if release_file_path:
        release_file = Path(release_file_path)
        if release_file.exists():
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


# ---------------------------------------------------------------------------
# Identity endpoint
# ---------------------------------------------------------------------------

@require_http_methods(["GET"])
@require_scope()  # Any valid token, no specific scope required
def api_me(request):
    """Return current identity and scopes."""
    token = request._api_token  # noqa: SLF001
    identity = request._api_identity  # noqa: SLF001

    result = {
        "token_name": token.name,
        "token_prefix": token.token_prefix,
        "scopes": token.scopes or [],
        "expires_at": token.expires_at.isoformat() if token.expires_at else None,
        "project": token.project_id,
    }

    if token.user_id:
        result["type"] = "user"
        result["user_id"] = token.user.pk
        result["username"] = token.user.get_username()
    elif token.service_account_id:
        result["type"] = "service_account"
        result["service_account_id"] = token.service_account.pk
        result["service_account_name"] = token.service_account.name

    return JsonResponse(result)


# ---------------------------------------------------------------------------
# Projects (Collection)
# ---------------------------------------------------------------------------

@require_http_methods(["GET"])
@require_scope("projects:read")
def api_projects(request):
    """List projects the token owner can access."""
    token = request._api_token  # noqa: SLF001

    if token.user_id:
        # User sees only projects they are a member of
        memberships = ProjectMembership.objects.filter(user_id=token.user_id)
        project_ids = memberships.values_list("project_id", flat=True)
        projects_qs = Collection.objects.filter(pk__in=project_ids)
    else:
        # Service account sees projects it's scoped to, or all if no project
        if token.project_id:
            projects_qs = Collection.objects.filter(pk=token.project_id)
        else:
            projects_qs = Collection.objects.all()

    projects = []
    for p in projects_qs:
        projects.append({
            "id": p.pk,
            "name": p.name,
            "description": p.description,
            "source_type": p.source_type,
            "is_archived": p.is_archived,
            "document_count": p.source_documents.count() + p.documents.count(),
        })

    return JsonResponse({"projects": projects, "count": len(projects)})


@require_http_methods(["GET"])
@require_scope("projects:read")
def api_project_detail(request, project_id):
    """Get project detail with membership info."""
    token = request._api_token  # noqa: SLF001
    project = get_object_or_404(Collection, pk=project_id)
    policy = ProjectAccessPolicy(token=token)
    if not policy.can_view(project):
        return JsonResponse({"error": "Access denied"}, status=403)

    result = {
        "id": project.pk,
        "name": project.name,
        "description": project.description,
        "source_type": project.source_type,
        "is_archived": project.is_archived,
        "created_at": project.created_at.isoformat(),
    }

    # Include membership info for users
    if token.user_id:
        membership = _get_user_membership(token, project_id)
        if membership:
            result["membership"] = {
                "role": membership.role,
                "joined_at": membership.joined_at.isoformat(),
            }

    return JsonResponse(result)


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

@require_http_methods(["GET"])
@require_scope("documents:read")
def api_documents(request, project_id):
    """List documents in a project."""
    token = request._api_token  # noqa: SLF001
    project = get_object_or_404(Collection, pk=project_id)
    policy = ProjectAccessPolicy(token=token)
    if not policy.can_view(project):
        return JsonResponse({"error": "Access denied"}, status=403)

    # Include both legacy Document and new SourceDocument
    docs = []
    for sd in project.source_documents.filter(is_archived=False):
        docs.append({
            "id": sd.pk,
            "type": "source_document",
            "filename": sd.filename,
            "source_type": sd.source_type,
            "file_size": sd.file_size,
            "page_count": sd.page_count,
            "created_at": sd.created_at.isoformat(),
        })
    for d in project.documents.filter(is_archived=False):
        docs.append({
            "id": d.pk,
            "type": "document",
            "external_id": d.external_id,
            "filename": d.filename,
            "page_count": d.page_count,
            "created_at": d.created_at.isoformat(),
        })

    return JsonResponse({"documents": docs, "count": len(docs)})


# ---------------------------------------------------------------------------
# Processing Jobs
# ---------------------------------------------------------------------------

@csrf_exempt
@require_http_methods(["POST"])
@require_scope("documents:upload", "jobs:submit")
def api_upload_document(request, project_id):
    """Upload a PDF document to a project and create a processing job."""
    from .policy import ProjectAccessPolicy
    from .services import DocumentIngestionService, IngestionError

    token = request._api_token  # noqa: SLF001
    request_id = request._request_id  # noqa: SLF001

    project = get_object_or_404(Collection, pk=project_id)

    # Check for uploaded file
    if "file" not in request.FILES:
        return JsonResponse({"error": "No file uploaded. Use multipart/form-data with 'file' field."}, status=400)

    uploaded_file = request.FILES["file"]
    preset_slug = request.POST.get("preset", "quick-extraction")

    # Use shared ingestion service
    policy = ProjectAccessPolicy(token=token)
    service = DocumentIngestionService(user=(token.user if token.user_id else None), policy=policy)

    try:
        job = service.create_upload(
            project=project,
            uploaded_file=uploaded_file,
            preset_slug=preset_slug,
        )
    except IngestionError as e:
        return JsonResponse({"error": str(e)}, status=400)

    owner = token.user if token.user_id else None
    source_doc = job.source_document

    # Audit
    AuditEvent.objects.create(
        actor=owner,
        event_type="document_uploaded",
        object_type="SourceDocument",
        object_id=str(source_doc.pk),
        after={"filename": source_doc.filename, "sha256": source_doc.sha256, "size": source_doc.file_size},
        request_id=request_id,
    )

    return JsonResponse({
        "status": "queued",
        "source_document_id": source_doc.pk,
        "job_id": job.pk,
        "filename": source_doc.filename,
        "sha256": source_doc.sha256,
        "preset": job.preset.slug,
    }, status=201)


@require_http_methods(["GET"])
@require_scope("jobs:read")
def api_job_detail(request, job_id):
    """Get job status and results."""
    token = request._api_token  # noqa: SLF001
    job = get_object_or_404(ProcessingJob, pk=job_id)

    # Check access via source document's collection
    policy = ProjectAccessPolicy(token=token)
    if not policy.can_access_job(job):
        return JsonResponse({"error": "Access denied"}, status=403)

    result = {
        "id": job.pk,
        "state": job.state,
        "source_document_id": job.source_document.pk,
        "filename": job.source_document.filename,
        "preset": job.preset.slug,
        "preset_snapshot": job.preset_snapshot,
        "processor": job.processor,
        "external_job_id": job.external_job_id,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "error_message": job.error_message,
        "pages_processed": job.pages_processed,
        "tables_found": job.tables_found,
        "created_at": job.created_at.isoformat(),
    }

    # Include artifact count
    result["artifact_count"] = job.artifacts.count()

    return JsonResponse(result)


# ---------------------------------------------------------------------------
# Processing Presets
# ---------------------------------------------------------------------------

@require_http_methods(["GET"])
@require_scope("jobs:submit")
def api_presets(request):
    """List active processing presets."""
    presets = []
    for p in ProcessingPreset.objects.filter(is_active=True):
        presets.append({
            "id": p.pk,
            "slug": p.slug,
            "name": p.name,
            "description": p.description,
            "description_short": p.description_short,
            "profile_a_enabled": p.profile_a_enabled,
            "profile_b_enabled": p.profile_b_enabled,
            "generate_crops": p.generate_crops,
            "create_review_tasks": p.create_review_tasks,
        })

    return JsonResponse({"presets": presets})


# ---------------------------------------------------------------------------
# Review Tasks (blinded for reviewers)
# ---------------------------------------------------------------------------

@require_http_methods(["GET"])
@require_scope("tasks:read")
def api_tasks(request):
    """List review tasks. Supports ?state=unreviewed/reviewed/skipped/expert."""
    token = request._api_token  # noqa: SLF001

    # Filter by assigned tasks for users, all for service accounts/curators
    if token.user_id:
        tasks = ReviewTask.objects.filter(assigned_to=token.user)
    else:
        tasks = ReviewTask.objects.all()

    state_filter = request.GET.get("state")
    if state_filter == "unreviewed":
        tasks = tasks.filter(state__in=["unassigned", "assigned", "in_progress"])
    elif state_filter == "reviewed":
        tasks = tasks.filter(state="completed")
    elif state_filter == "skipped":
        tasks = tasks.filter(state="skipped")
    elif state_filter == "expert":
        tasks = tasks.filter(state="needs_expert")

    result = [_serialize_task_blinded(t, token) for t in tasks]
    return JsonResponse({"tasks": result, "count": len(result)})


@require_http_methods(["GET"])
@require_scope("tasks:read")
def api_task_detail(request, task_id):
    """Get task detail with blinded extractions."""
    token = request._api_token  # noqa: SLF001
    task = get_object_or_404(ReviewTask, pk=task_id)

    has_review = Review.objects.filter(review_task=task).exists()

    # Get extractions for candidates X and Y
    x_extraction = None
    y_extraction = None

    if task.candidate_x:
        x_extraction = _serialize_extraction_blinded(
            task.candidate_x, "X", reveal=has_review
        )
        x_extraction["html"] = task.candidate_x.raw_html or ""
        x_extraction["otsl"] = task.candidate_x.raw_otsl or ""

    if task.candidate_y:
        y_extraction = _serialize_extraction_blinded(
            task.candidate_y, "Y", reveal=has_review
        )
        y_extraction["html"] = task.candidate_y.raw_html or ""
        y_extraction["otsl"] = task.candidate_y.raw_otsl or ""

    # Ground truth is hidden until review is submitted
    ground_truth = None
    if has_review:
        gt = TableExtraction.objects.filter(
            table_candidate=task.table_candidate,
            extraction_run__profile="ground_truth"
        ).first()
        if gt:
            ground_truth = {
                "id": gt.pk,
                "label": "ground_truth",
                "rows": gt.rows,
                "columns": gt.columns,
                "html": gt.raw_html or "",
                "otsl": gt.raw_otsl or "",
                "teds": gt.teds,
                "teds_s": gt.teds_s,
            }

    # Existing review (if any)
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
            "created_at": existing.created_at.isoformat(),
        }

    return JsonResponse({
        "task": _serialize_task_blinded(task, token),
        "candidate_x": x_extraction,
        "candidate_y": y_extraction,
        "ground_truth": ground_truth,
        "review": review,
        "has_review": has_review,
    })


@csrf_exempt
@require_http_methods(["POST"])
@require_scope("reviews:write")
def api_task_submit(request, task_id):
    """Submit a review for a task (API version)."""
    token = request._api_token  # noqa: SLF001
    request_id = request._request_id  # noqa: SLF001
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
    if preference not in ("x", "y", "tie", "neither"):
        return JsonResponse({"error": "preference must be x, y, tie, or neither"}, status=400)

    owner = token.user if token.user_id else None

    with transaction.atomic():
        # Create review
        review = Review.objects.create(
            review_task=task,
            reviewer=owner,
            candidate_x=task.candidate_x,
            candidate_y=task.candidate_y,
            candidate_x_score=score_x,
            candidate_y_score=score_y,
            preferred_result=preference,
            confidence=confidence,
            comment=notes,
            structure_errors=[],
            text_errors=error_categories,
        )

        # Update task status
        task.state = "completed"
        task.save(update_fields=["state"])

    # Audit
    AuditEvent.objects.create(
        actor=owner,
        event_type="review_submitted",
        object_type="Review",
        object_id=str(review.pk),
        after={"task_id": task.pk, "preference": preference},
        request_id=request_id,
    )

    return JsonResponse({
        "status": "ok",
        "review_id": review.pk,
        "message": "Review submitted",
    })


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

@require_http_methods(["GET"])
@require_scope("statistics:read")
def api_statistics(request):
    """Get review statistics."""
    total = ReviewTask.objects.count()
    reviewed = ReviewTask.objects.filter(state="completed").count()
    skipped = ReviewTask.objects.filter(state="skipped").count()
    needs_expert = ReviewTask.objects.filter(state="needs_expert").count()

    from django.db.models import Avg
    reviews = Review.objects.all()
    avg_score_x = reviews.aggregate(avg_x=Avg("candidate_x_score"))["avg_x"] or 0
    avg_score_y = reviews.aggregate(avg_y=Avg("candidate_y_score"))["avg_y"] or 0
    pref_x = reviews.filter(preferred_result="candidate_x").count()
    pref_y = reviews.filter(preferred_result="candidate_y").count()
    pref_tie = reviews.filter(preferred_result="equivalent").count()

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
