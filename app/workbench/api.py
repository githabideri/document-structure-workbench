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
    UserPreferences, OcrRequest, PageRegion, RegionCorrection,
    ChatThread, ChatRun, ChatMessage,
)
from .policy import ProjectAccessPolicy


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
    from .services import DiagnosticsService
    health = DiagnosticsService.health()
    if not health.get("release") or health.get("database") != "ok":
        return JsonResponse({
            **health,
        }, status=500)
    return JsonResponse(health)


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

@require_http_methods(["GET", "POST"])
@require_scope("projects:read")
def api_projects(request):
    """List projects the token owner can access."""
    token = request._api_token  # noqa: SLF001
    if request.method == "POST":
        if "projects:write" not in set(token.scopes or []):
            return JsonResponse({"error": {"code": "insufficient_scope", "message": "projects:write is required."}}, status=403)
        user = token.user if token.user_id else None
        if not user or not (user.is_superuser or user.groups.filter(name="Administrator").exists()):
            return JsonResponse({"error": {"code": "admin_required", "message": "Only administrators can create projects."}}, status=403)
        try:
            data = json.loads(request.body or "{}")
            name = str(data.get("name", "")).strip()
            if not name:
                raise ValueError("name is required")
            if Collection.objects.filter(name=name).exists():
                raise ValueError("A project with this name already exists.")
            source_type = data.get("source_type", "corpus")
            if source_type not in {choice[0] for choice in Collection._meta.get_field("source_type").choices}:
                raise ValueError("Invalid source_type")
            project = Collection.objects.create(
                name=name, description=str(data.get("description", "")).strip(),
                source_type=source_type, created_by=user,
            )
            ProjectMembership.objects.create(project=project, user=user, role="owner")
            AuditEvent.objects.create(actor=user, event_type="project_created", object_type="Collection", object_id=str(project.id), request_id=request._request_id)
            return JsonResponse({"request_id": request._request_id, "project": {"id": project.id, "name": project.name}}, status=201)
        except (ValueError, json.JSONDecodeError) as exc:
            return JsonResponse({"request_id": request._request_id, "error": {"code": "invalid_request", "message": str(exc)}}, status=400)
    policy = ProjectAccessPolicy(token=token)
    projects_qs = policy.visible_projects()

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


@require_http_methods(["GET", "PATCH"])
@require_scope("projects:read")
def api_project_detail(request, project_id):
    """Get project detail with membership info."""
    token = request._api_token  # noqa: SLF001
    project = get_object_or_404(Collection, pk=project_id)
    if request.method == "PATCH":
        if "projects:write" not in set(token.scopes or []):
            return JsonResponse({"error": {"code": "insufficient_scope", "message": "projects:write is required."}}, status=403)
        user = token.user if token.user_id else None
        if not user or not (user.is_superuser or user.groups.filter(name="Administrator").exists()):
            return JsonResponse({"error": {"code": "admin_required", "message": "Only administrators can archive projects."}}, status=403)
        try:
            data = json.loads(request.body or "{}")
            if "is_archived" not in data:
                raise ValueError("is_archived is required")
            project.is_archived = bool(data["is_archived"])
            project.save(update_fields=["is_archived"])
            AuditEvent.objects.create(actor=user, event_type="project_archived" if project.is_archived else "project_restored", object_type="Collection", object_id=str(project.id), request_id=request._request_id)
            return JsonResponse({"request_id": request._request_id, "id": project.id, "is_archived": project.is_archived})
        except (ValueError, json.JSONDecodeError) as exc:
            return JsonResponse({"request_id": request._request_id, "error": {"code": "invalid_request", "message": str(exc)}}, status=400)
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
        membership = ProjectMembership.objects.filter(
            project=project, user_id=token.user_id
        ).first()
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


@csrf_exempt
@require_http_methods(["POST"])
@require_scope("documents:manage")
def api_job_recovery(request, job_id):
    """Close, retry, or archive a failed processing attempt."""
    job = get_object_or_404(ProcessingJob.objects.select_related("source_document", "source_document__collection"), pk=job_id)
    token = request._api_token
    policy = ProjectAccessPolicy(token=token)
    if not policy.can_edit(job.source_document.collection):
        return JsonResponse({"request_id": request._request_id, "error": {"code": "forbidden", "message": "Access denied."}}, status=403)
    try:
        action = json.loads(request.body or "{}").get("action", "")
        if action == "mark_failed" and job.state == "submission_uncertain":
            job.transition_to("failed")
            job.error_message = "The uncertain processor submission was closed by an operator."
            job.status_message = "Marked failed."
            job.save(update_fields=["error_message", "status_message"])
        elif action == "retry_new" and job.state in {"submission_uncertain", "interrupted", "partial", "failed", "cancelled"}:
            from .services import DocumentIngestionService, IngestionError
            try:
                retry = DocumentIngestionService(user=token.user if token.user_id else None, policy=policy).retry_existing(job=job)
            except IngestionError as exc:
                raise ValueError(str(exc)) from exc
            return JsonResponse({"request_id": request._request_id, "job_id": retry.id, "state": retry.state}, status=201)
        elif action == "archive_upload":
            if job.source_document.processing_jobs.filter(state__in=["queued", "submitting", "processing", "importing"]).exists():
                raise ValueError("An active processing attempt must finish before archiving.")
            job.source_document.is_archived = True
            job.source_document.save(update_fields=["is_archived"])
        else:
            raise ValueError("Unsupported recovery action for this job.")
    except (ValueError, json.JSONDecodeError) as exc:
        return JsonResponse({"request_id": request._request_id, "error": {"code": "invalid_request", "message": str(exc)}}, status=400)
    return JsonResponse({"request_id": request._request_id, "job_id": job.id, "state": job.state, "archived": job.source_document.is_archived})


# ---------------------------------------------------------------------------
# Visual OCR candidates
# ---------------------------------------------------------------------------

def _ocr_request_json(item):
    return {
        "id": item.pk, "state": item.state, "target": item.target,
        "source_document_id": item.source_document_id, "revision_id": item.document_id,
        "page_id": item.page_id, "page_number": item.page.page_number,
        "region_id": item.region_id, "provider": item.provider, "model": item.model,
        "prompt": item.prompt, "candidate_text": item.candidate_text,
        "input_sha256": item.input_sha256, "input_metadata": item.input_metadata,
        "metadata": item.metadata, "error_message": item.error_message,
        "accepted_correction_id": item.accepted_correction_id,
        "created_at": item.created_at.isoformat(),
        "started_at": item.started_at.isoformat() if item.started_at else None,
        "finished_at": item.finished_at.isoformat() if item.finished_at else None,
    }


def _create_ocr_request(request, page, region=None):
    token = request._api_token
    if not ProjectAccessPolicy(token=token).can_edit(page.document.collection):
        return JsonResponse({"error": "Access denied"}, status=403)
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        body = {}
    prompt = str(body.get("prompt") or (
        "Transcribe exactly the visible text. Preserve spelling, punctuation and line breaks. "
        "Do not translate, summarize, correct, infer, or add text. Return only the transcription."
    )).strip()
    item = OcrRequest.objects.create(
        source_document=page.document.processing_job.source_document,
        document=page.document, page=page, region=region,
        target="region" if region else "page",
        provider=getattr(settings, "DSW_OCR_PROVIDER", "openai-compatible"),
        model=getattr(settings, "DSW_OCR_MODEL", ""), prompt=prompt,
        created_by=token.user if token.user_id else None,
    )
    return JsonResponse(_ocr_request_json(item), status=201)


@csrf_exempt
@require_http_methods(["POST"])
@require_scope("documents:manage")
def api_region_ocr(request, region_id):
    region = get_object_or_404(PageRegion.objects.select_related("page__document__collection", "page__document__processing_job__source_document"), pk=region_id)
    return _create_ocr_request(request, region.page, region)


@csrf_exempt
@require_http_methods(["POST"])
@require_scope("documents:manage")
def api_page_ocr(request, page_id):
    page = get_object_or_404(Page.objects.select_related("document__collection", "document__processing_job__source_document"), pk=page_id)
    return _create_ocr_request(request, page)


@require_http_methods(["GET"])
@require_scope("documents:read")
def api_ocr_request_detail(request, request_id):
    item = get_object_or_404(OcrRequest.objects.select_related("page", "document__collection"), pk=request_id)
    if not ProjectAccessPolicy(token=request._api_token).can_view(item.document.collection):
        return JsonResponse({"error": "Access denied"}, status=403)
    return JsonResponse(_ocr_request_json(item))


@csrf_exempt
@require_http_methods(["POST"])
@require_scope("documents:manage")
def api_ocr_request_accept(request, request_id):
    item = get_object_or_404(OcrRequest.objects.select_related("region", "document__collection"), pk=request_id)
    if not ProjectAccessPolicy(token=request._api_token).can_edit(item.document.collection):
        return JsonResponse({"error": "Access denied"}, status=403)
    if item.state != "completed" or not item.region_id:
        return JsonResponse({"error": "Only completed region OCR candidates can be accepted."}, status=409)
    if item.accepted_correction_id:
        return JsonResponse(_ocr_request_json(item))
    correction = RegionCorrection.objects.create(
        region=item.region, document=item.document,
        created_by=request._api_token.user if request._api_token.user_id else None,
        operation="text", before={"text": item.region.effective_text},
        after={"text": item.candidate_text}, reason=f"Accepted visual OCR candidate #{item.pk}",
    )
    item.accepted_correction = correction
    item.save(update_fields=["accepted_correction"])
    return JsonResponse(_ocr_request_json(item))


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


def _chat_thread_json(thread):
    return {"id": thread.id, "project_id": thread.project_id, "title": thread.title,
            "is_archived": thread.is_archived,
            "scope": {"mode": thread.scope_mode, "project_id": thread.project_id,
                       "source_ids": list(thread.selected_sources.values_list("id", flat=True)),
                       "revision_ids": thread.selected_revisions or [],
                       **(thread.scope_config or {})},
            "created_at": thread.created_at.isoformat(), "updated_at": thread.updated_at.isoformat()}


def _chat_run_json(run, include_evidence=False):
    answer = run.assistant_message.text if run.assistant_message else None
    result = {"id": run.id, "thread_id": run.thread_id, "state": run.state, "status": run.status_message,
              "error": {"code": getattr(run, "error_code", ""), "message": run.error_message} if run.error_message else None,
              "timestamps": {"created_at": run.created_at.isoformat(), "started_at": run.started_at.isoformat() if run.started_at else None, "finished_at": run.finished_at.isoformat() if run.finished_at else None},
              "answer": answer, "token_budget": run.token_budget, "model": run.thread.model,
              "request_id": getattr(run, "request_id", None)}
    if include_evidence:
        result["evidence"] = [{"marker": item.marker, "source_document_id": item.source_document_id, "revision_id": item.processed_revision_id,
                                "page": item.page.page_number if item.page else None, "text": item.text, "page_text": item.page_text, "score": item.score, "reason": item.selection_reason}
                               for item in run.evidence_items.select_related("page").order_by("ordinal")]
    return result


@csrf_exempt
@require_http_methods(["GET", "POST"])
@require_scope("chat:read")
def api_chat_threads(request):
    if request.method == "POST" and "chat:write" not in set(request._api_token.scopes or []):
        return JsonResponse({"request_id": request._request_id, "error": {"code": "insufficient_scope", "message": "chat:write is required."}}, status=403)
    if request.method == "GET":
        identity = request._api_token.user if request._api_token.user_id else None
        policy = ProjectAccessPolicy(user=identity, token=request._api_token)
        visible_ids = set(policy.visible_projects().values_list("id", flat=True))
        threads = [thread for thread in ChatThread.objects.filter(is_archived=False).select_related("project")
                   if thread.project_id in visible_ids or visible_ids.intersection((thread.scope_config or {}).get("project_ids", []))]
        return JsonResponse({"request_id": request._request_id, "threads": [_chat_thread_json(thread) for thread in threads[:100]]})
    from .chat import create_chat_run
    try:
        data = json.loads(request.body or "{}")
        mode = data.get("scope", {}).get("mode", data.get("scope_mode", "project"))
        project_value = data.get("project_id", data.get("scope", {}).get("project_id"))
        project_id = int(project_value) if project_value is not None else None
        source_ids = [int(value) for value in data.get("source_ids", [])]
        source_ids += [int(value) for value in data.get("scope", {}).get("attachment_ids", [])]
        question = str(data.get("question", "")).strip()
        if not question:
            raise ValueError("question is required")
        policy = ProjectAccessPolicy(user=request._api_token.user if request._api_token.user_id else None, token=request._api_token)
        try:
            scope = policy.resolve_chat_scope(mode=mode, project_id=project_id, source_ids=source_ids,
                                              revision_ids=data.get("scope", {}).get("revision_ids"),
                                              filters=data.get("scope", {}).get("filters"))
        except (ValueError, PermissionError) as exc:
            return JsonResponse({"error": {"code": "forbidden", "message": str(exc)}, "request_id": request._request_id}, status=403)
        project = Collection.objects.filter(pk=project_id).first() if project_id else None
        thread = ChatThread.objects.create(project=project, created_by=request._api_token.user if request._api_token.user_id else None,
                                            title=data.get("title", question[:120]), model=getattr(settings, "DSW_CHAT_MODEL", ""),
                                            scope_mode=mode, scope_config=scope,
                                            selected_revisions=scope["revision_ids"])
        thread.selected_sources.set(SourceDocument.objects.filter(pk__in=scope["attachment_ids"]))
        run = create_chat_run(thread, question, scope=scope)
        run.request_id = request._request_id
        run.save(update_fields=["request_id"])
        return JsonResponse({"request_id": request._request_id, "thread": _chat_thread_json(thread), "run": _chat_run_json(run) }, status=201)
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        return JsonResponse({"request_id": request._request_id, "error": {"code": "invalid_request", "message": str(exc)}}, status=400)


@require_http_methods(["GET"])
@require_scope("chat:read")
def api_chat_threads_list(request):
    identity = request._api_token.user if request._api_token.user_id else None
    policy = ProjectAccessPolicy(user=identity, token=request._api_token)
    visible_ids = set(policy.visible_projects().values_list("id", flat=True))
    threads = [thread for thread in ChatThread.objects.filter(is_archived=False).select_related("project")
               if thread.project_id in visible_ids or visible_ids.intersection((thread.scope_config or {}).get("project_ids", []))]
    return JsonResponse({"request_id": request._request_id, "threads": [_chat_thread_json(thread) for thread in threads[:100]]})


@require_http_methods(["GET", "PATCH"])
@require_scope("chat:read")
def api_chat_thread_detail(request, thread_id):
    thread = get_object_or_404(ChatThread.objects.select_related("project"), pk=thread_id)
    policy = ProjectAccessPolicy(user=request._api_token.user if request._api_token.user_id else None, token=request._api_token)
    if thread.project_id and not policy.can_view(thread.project):
        return JsonResponse({"request_id": request._request_id, "error": {"code": "not_found", "message": "Thread not found."}}, status=404)
    if not thread.project_id and not set((thread.scope_config or {}).get("project_ids", [])) & set(policy.visible_projects().values_list("id", flat=True)):
        return JsonResponse({"request_id": request._request_id, "error": {"code": "not_found", "message": "Thread not found."}}, status=404)
    if request.method == "PATCH":
        if "chat:manage" not in set(request._api_token.scopes or []):
            return JsonResponse({"request_id": request._request_id, "error": {"code": "insufficient_scope", "message": "chat:manage is required."}}, status=403)
        try:
            data = json.loads(request.body or "{}")
            if "title" in data:
                title = str(data["title"]).strip()
                if not title:
                    raise ValueError("title cannot be empty")
                thread.title = title[:200]
            if "is_archived" in data:
                thread.is_archived = bool(data["is_archived"])
            thread.save(update_fields=["title", "is_archived", "updated_at"])
            return JsonResponse({"request_id": request._request_id, "thread": _chat_thread_json(thread)})
        except (ValueError, json.JSONDecodeError) as exc:
            return JsonResponse({"request_id": request._request_id, "error": {"code": "invalid_request", "message": str(exc)}}, status=400)
    runs = thread.runs.select_related("assistant_message").order_by("created_at")
    messages = [{"id": message.id, "role": message.role, "text": message.text, "ordinal": message.ordinal, "created_at": message.created_at.isoformat()} for message in thread.messages.all()]
    return JsonResponse({"request_id": request._request_id, "thread": _chat_thread_json(thread), "messages": messages, "runs": [_chat_run_json(run) for run in runs]})


@csrf_exempt
@require_http_methods(["POST"])
@require_scope("chat:write")
def api_chat_thread_runs(request, thread_id):
    thread = get_object_or_404(ChatThread.objects.select_related("project"), pk=thread_id)
    policy = ProjectAccessPolicy(user=request._api_token.user if request._api_token.user_id else None, token=request._api_token)
    if thread.project_id and not policy.can_view(thread.project):
        return JsonResponse({"request_id": request._request_id, "error": {"code": "not_found", "message": "Thread not found."}}, status=404)
    try:
        data = json.loads(request.body or "{}")
        question = str(data.get("question", "")).strip()
        if not question:
            raise ValueError("question is required")
        from .chat import create_chat_run
        scope_data = data.get("scope")
        scope = None
        if scope_data is not None:
            try:
                scope = policy.resolve_chat_scope(
                    mode=scope_data.get("mode", thread.scope_mode),
                    project_id=scope_data.get("project_id", thread.project_id),
                    source_ids=scope_data.get("attachment_ids", scope_data.get("source_ids", [])),
                    revision_ids=scope_data.get("revision_ids"), filters=scope_data.get("filters"),
                )
            except (ValueError, PermissionError) as exc:
                return JsonResponse({"request_id": request._request_id, "error": {"code": "forbidden", "message": str(exc)}}, status=403)
        run = create_chat_run(thread, question, scope=scope)
        run.request_id = request._request_id
        run.save(update_fields=["request_id"])
        return JsonResponse({"request_id": request._request_id, "run": _chat_run_json(run)}, status=201)
    except (ValueError, json.JSONDecodeError) as exc:
        return JsonResponse({"request_id": request._request_id, "error": {"code": "invalid_request", "message": str(exc)}}, status=400)


def _api_chat_run(request, run_id):
    run = get_object_or_404(ChatRun.objects.select_related("thread", "assistant_message", "thread__project"), pk=run_id)
    policy = ProjectAccessPolicy(user=request._api_token.user if request._api_token.user_id else None, token=request._api_token)
    if run.thread.project_id and not policy.can_view(run.thread.project):
        return None
    if not run.thread.project_id and not set((run.scope_snapshot or {}).get("project_ids", [])) & set(policy.visible_projects().values_list("id", flat=True)):
        return None
    return run


@require_http_methods(["GET"])
@require_scope("chat:read")
def api_chat_run_detail(request, run_id):
    run = _api_chat_run(request, run_id)
    if not run:
        return JsonResponse({"request_id": request._request_id, "error": {"code": "not_found", "message": "Run not found."}}, status=404)
    return JsonResponse({"request_id": request._request_id, "thread": _chat_thread_json(run.thread), "run": _chat_run_json(run, include_evidence=True)})


@require_http_methods(["GET"])
@require_scope("chat:read")
def api_chat_run_evidence(request, run_id):
    run = _api_chat_run(request, run_id)
    if not run:
        return JsonResponse({"request_id": request._request_id, "error": {"code": "not_found", "message": "Run not found."}}, status=404)
    return JsonResponse({"request_id": request._request_id, "run_id": run.id, "evidence": _chat_run_json(run, True)["evidence"]})


@csrf_exempt
@require_http_methods(["POST"])
@require_scope("chat:retry")
def api_chat_run_retry(request, run_id):
    run = _api_chat_run(request, run_id)
    if not run:
        return JsonResponse({"request_id": request._request_id, "error": {"code": "not_found", "message": "Run not found."}}, status=404)
    from .chat import create_chat_run
    retry = create_chat_run(run.thread, run.retrieval_query)
    return JsonResponse({"request_id": request._request_id, "run": _chat_run_json(retry)}, status=201)


@csrf_exempt
@require_http_methods(["POST"])
@require_scope("support:export")
def api_chat_support_bundle(request, run_id):
    run = _api_chat_run(request, run_id)
    if not run:
        return JsonResponse({"request_id": request._request_id, "error": {"code": "not_found", "message": "Run not found."}}, status=404)
    from .services import SupportBundleService
    bundle = SupportBundleService.build(run)
    export_format = (json.loads(request.body or "{}").get("format", "json") if request.body else "json")
    AuditEvent.objects.create(actor=request._api_token.user if request._api_token.user_id else None, event_type="chat_support_bundle_exported",
                              object_type="ChatRun", object_id=str(run.id), after={"format": export_format}, request_id=request._request_id)
    if export_format == "markdown":
        from django.http import HttpResponse
        response = HttpResponse(SupportBundleService.markdown(bundle), content_type="text/markdown")
        response["Content-Disposition"] = f'attachment; filename="chat-run-{run.id}-support.md"'
        return response
    return JsonResponse({"request_id": request._request_id, "bundle": bundle})


@require_http_methods(["GET"])
@require_scope("support:read")
def api_support_bundle_detail(request, bundle_id):
    run = _api_chat_run(request, bundle_id)
    if not run:
        return JsonResponse({"request_id": request._request_id, "error": {"code": "not_found", "message": "Support bundle not found."}}, status=404)
    from .services import SupportBundleService
    return JsonResponse({"request_id": request._request_id, "bundle": SupportBundleService.build(run)})


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
    preference_map = {
        "x": "candidate_x", "y": "candidate_y", "tie": "equivalent", "neither": "neither",
    }
    if preference not in preference_map:
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
            preferred_result=preference_map[preference],
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
