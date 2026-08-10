"""
Document ingestion service — shared by API and web upload.

Handles:
- File validation (PDF extension, MIME type, signature, size)
- Safe storage (generated name, no user-controlled paths)
- SHA-256 calculation (single pass during write, streamed to temp file)
- Duplicate detection (same project + same SHA)
- Transactional consistency (file + DB in/out sync)
- Project access enforcement
- Orphan cleanup on failures
- Filesystem rollback on DB failures
"""
import hashlib
import logging
import os
import tempfile
from pathlib import Path
import json
import re
import requests

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import (
    Collection,
    PageRegion,
    ProcessingJob,
    ProcessingPreset,
    OcrRequest,
    RegionCorrection,
    SourceDocument,
)

logger = logging.getLogger(__name__)


class ChatService:
    """Shared authorization-aware application boundary for chat workflows."""
    def __init__(self, *, identity, policy):
        self.identity = identity
        self.policy = policy

    def authorize_thread(self, thread):
        if not thread or not self.policy.can_view(thread.project):
            raise PermissionError("The conversation is not accessible.")
        return thread

    def create_run(self, thread, question):
        from .chat import create_chat_run
        self.authorize_thread(thread)
        if not question or not question.strip():
            raise ValueError("A question is required.")
        return create_chat_run(thread, question.strip())


class ChatManagementService:
    """Shared rename/archive transitions for conversation callers."""

    @staticmethod
    def authorize(thread, policy):
        if thread.project_id and not policy.can_view(thread.project):
            raise PermissionError("The conversation is not accessible.")
        if not thread.project_id:
            visible = set(policy.visible_projects().values_list("id", flat=True))
            if not visible.intersection((thread.scope_config or {}).get("project_ids", [])):
                raise PermissionError("The conversation is not accessible.")
        return thread

    @staticmethod
    def rename(thread, title, policy):
        ChatManagementService.authorize(thread, policy)
        title = (title or "").strip()
        if not title:
            raise ValueError("title is required")
        thread.title = title[:200]
        thread.save(update_fields=["title", "updated_at"])
        return thread

    @staticmethod
    def archive(thread, policy, archived=True):
        ChatManagementService.authorize(thread, policy)
        thread.is_archived = archived
        thread.save(update_fields=["is_archived", "updated_at"])
        return thread


class ChatRunService:
    """Common execution boundary used by workers and future agent adapters."""
    @staticmethod
    def execute(run, worker_id="chat-worker"):
        from .chat import process_chat_run
        return process_chat_run(run, worker_id=worker_id)


class LifecycleError(Exception):
    """A lifecycle operation is not valid for the current resource state."""


class ProjectLifecycleService:
    """Shared archive operations used by HTML and API callers."""

    @staticmethod
    def archive_project(*, project, policy, archived=True):
        if not policy.can_edit(project):
            raise PermissionError("You do not have permission to manage this project.")
        if project.is_archived == archived:
            raise LifecycleError("Project is already in the requested archive state.")
        project.is_archived = archived
        project.save(update_fields=["is_archived"])
        return project

    @staticmethod
    def archive_source(*, source, policy, archived=True):
        if not source.collection or not policy.can_edit(source.collection):
            raise PermissionError("You do not have permission to manage this document.")
        if source.is_archived == archived:
            raise LifecycleError("Document is already in the requested archive state.")
        if archived and source.processing_jobs.filter(state__in={"queued", "submitting", "processing", "importing"}).exists():
            raise LifecycleError("An active processing attempt must finish before archiving.")
        source.is_archived = archived
        source.save(update_fields=["is_archived"])
        return source


class DiagnosticsService:
    """Safe, deliberately small deployment diagnostics; never returns secrets."""
    @staticmethod
    def health():
        from pathlib import Path
        from django.db import connection
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            database = "ok"
        except Exception:
            database = "unreachable"
        release = None
        release_path = getattr(settings, "RELEASE_FILE", "")
        if release_path and Path(release_path).exists():
            release = Path(release_path).read_text().strip()
        from .models import ChatRun, WorkerHeartbeat
        latest = WorkerHeartbeat.objects.order_by("-last_seen").first()
        worker_seen = latest.last_seen if latest else None
        worker_age = (timezone.now() - worker_seen).total_seconds() if worker_seen else None
        worker_ok = worker_age is not None and worker_age <= getattr(settings, "DSW_PROCESSING_STALE_AFTER_SECONDS", 90)
        provider = DiagnosticsService.provider_status()
        processor = DiagnosticsService.processor_status()
        return {"status": "ok" if database == "ok" else "error", "release": release, "database": database,
                "project_visibility": getattr(settings, "DSW_PROJECT_VISIBILITY", "membership"),
                "worker": {"status": "ok" if worker_ok else "stale", "last_seen": worker_seen.isoformat() if worker_seen else None,
                            "queue_depth": ChatRun.objects.filter(state="queued").count() if database == "ok" else None},
                "chat_provider": provider, "document_processor": processor}

    @staticmethod
    def processor_status():
        """Probe the configured Docling health endpoint without exposing secrets."""
        base_url = getattr(settings, "DSW_DOCLING_API_URL", "").strip()
        configured = bool(base_url)
        result = {"configured": configured, "reachable": None, "health_path": getattr(settings, "DSW_DOCLING_HEALTH_PATH", "/health")}
        if not configured:
            return result
        path = result["health_path"]
        if not str(path).startswith("/"):
            path = "/" + str(path)
        try:
            response = requests.get(
                f"{base_url.rstrip('/')}{path}",
                headers=({"Authorization": f"Bearer {settings.DSW_DOCLING_API_KEY}"}
                         if getattr(settings, "DSW_DOCLING_API_KEY", "") else {}),
                timeout=min(getattr(settings, "DSW_DOCLING_REQUEST_TIMEOUT", 60), 5),
            )
            result["reachable"] = response.ok
            result["http_status"] = response.status_code
        except (requests.RequestException, ValueError):
            result["reachable"] = False
        return result

    @staticmethod
    def provider_status():
        """Probe the OpenAI-compatible models endpoint without exposing secrets."""
        base_url = getattr(settings, "DSW_CHAT_BASE_URL", "").strip()
        model = getattr(settings, "DSW_CHAT_MODEL", "").strip()
        configured = bool(base_url and model)
        result = {"configured": configured, "reachable": None,
                  "model_configured": bool(model), "model_available": None}
        result["tool_mode"] = getattr(settings, "DSW_CHAT_TOOL_MODE", "fallback")
        result["limits"] = {
            "max_tool_calls": getattr(settings, "DSW_CHAT_MAX_TOOL_CALLS", 10),
            "max_results_per_call": getattr(settings, "DSW_CHAT_MAX_RESULTS_PER_CALL", 8),
            "max_evidence": getattr(settings, "DSW_CHAT_MAX_EVIDENCE", 24),
            "context_token_budget": getattr(settings, "DSW_CHAT_CONTEXT_TOKEN_BUDGET", 12000),
            "wall_clock_timeout": getattr(settings, "DSW_CHAT_WALL_CLOCK_TIMEOUT", 300),
            "request_timeout": getattr(settings, "DSW_CHAT_TIMEOUT", 300),
            "tool_request_timeout": getattr(settings, "DSW_CHAT_TOOL_REQUEST_TIMEOUT", 300),
            "final_request_timeout": getattr(settings, "DSW_CHAT_FINAL_REQUEST_TIMEOUT", 600),
        }
        if not configured:
            return result
        headers = {"Authorization": f"Bearer {settings.DSW_CHAT_API_KEY}"} if getattr(settings, "DSW_CHAT_API_KEY", "") else {}
        try:
            response = requests.get(
                f"{base_url.rstrip('/')}/models",
                headers=headers,
                timeout=min(getattr(settings, "DSW_CHAT_TIMEOUT", 120), 5),
            )
            result["reachable"] = True
            response.raise_for_status()
            payload = response.json()
            models = payload.get("data", []) if isinstance(payload, dict) else []
            identifiers = {str(item.get("id")) for item in models if isinstance(item, dict)}
            result["model_available"] = model in identifiers
        except requests.HTTPError:
            result["model_available"] = False
        except (requests.RequestException, ValueError, TypeError):
            result["reachable"] = False
            result["model_available"] = False
        return result


class SupportBundleService:
    """Build a secret-free reconstruction of one authorized chat run."""
    @staticmethod
    def build(run):
        thread = run.thread
        provider = (run.model_metadata or {}).get("provider", {})
        snapshot = run.scope_snapshot or thread.scope_config or {}
        from .models import SourceDocument
        source_ids = snapshot.get("source_ids") or list(thread.selected_sources.values_list("id", flat=True))
        sources = [{"id": source.id, "filename": source.filename, "revision_id": source.active_document_id,
                    "project_id": source.collection_id}
                   for source in SourceDocument.objects.filter(id__in=source_ids).order_by("id")]
        project = thread.project
        project_data = {"id": project.id, "name": project.name} if project else {
            "id": None, "name": "All accessible projects",
            "ids": snapshot.get("project_ids", []),
        }
        bundle = {
            "run": {"id": run.id, "state": run.state, "status": run.status_message, "error": run.error_message,
                    "created_at": run.created_at.isoformat(), "started_at": run.started_at.isoformat() if run.started_at else None,
                    "finished_at": run.finished_at.isoformat() if run.finished_at else None, "worker_id": run.worker_id,
                    "token_budget": run.token_budget, "source_tokens": run.source_tokens},
            "project": project_data,
            "scope": {"mode": snapshot.get("mode", thread.scope_mode),
                      "project_ids": snapshot.get("project_ids", []),
                      "sources": sources,
                      "revision_ids": snapshot.get("revision_ids", thread.selected_revisions or []),
                      "attachment_ids": snapshot.get("attachment_ids", []),
                      "filters": snapshot.get("filters", {})},
            "question": run.user_message.text,
            "history": list(thread.messages.exclude(pk=run.user_message_id).order_by("ordinal").values("role", "text")),
            "evidence": [{"marker": item.marker, "source_document_id": item.source_document_id, "revision_id": item.processed_revision_id,
                          "page": item.page.page_number if item.page else None, "text": item.text, "page_text": item.page_text, "score": item.score,
                          "reason": item.selection_reason} for item in run.evidence_items.select_related("page").order_by("ordinal")],
            "prompt": (run.model_metadata or {}).get("prompt", ""),
            "provider_request": (run.model_metadata or {}).get("provider_request", {}),
            "provider": {key: value for key, value in provider.items() if key != "reasoning_content"},
            "final_answer": (run.model_metadata or {}).get("final_answer", run.assistant_message.text if run.assistant_message else ""),
            "reasoning_content": provider.get("reasoning_content"),
            "events": [{"name": event.name, "worker_id": event.worker_id, "duration_ms": event.duration_ms,
                         "metadata": event.metadata, "error_code": event.error_code, "created_at": event.created_at.isoformat()}
                        for event in run.events.all()],
        }
        return bundle

    @staticmethod
    def markdown(bundle):
        run = bundle["run"]
        project_id = bundle["project"].get("id")
        project_label = bundle["project"]["name"]
        if project_id is not None:
            project_label += f" ({project_id})"
        lines = [f"# Chat support bundle — run {run['id']}", "", f"- State: {run['state']}", f"- Project scope: {project_label}", "", "## Question", bundle["question"], "", "## Final answer", bundle["final_answer"]]
        if bundle.get("reasoning_content"):
            lines += ["", "## Provider reasoning content (untrusted diagnostic output)", bundle["reasoning_content"]]
        lines += ["", "## Evidence"]
        for item in bundle["evidence"]:
            lines += [f"### [{item['marker']}] source {item['source_document_id']} page {item['page']}", item["text"], f"Score: {item['score']}; reason: {item['reason']}"]
        lines += ["", "## Timeline"]
        lines += [f"- {event['created_at']} — {event['name']} {event['error_code']}" for event in bundle["events"]]
        return "\n".join(lines) + "\n"

    @staticmethod
    def human_timeline(bundle):
        """Return a plain-language explanation of the persisted run events."""
        timeline = []
        for event in bundle["events"]:
            name = event["name"]
            metadata = event.get("metadata") or {}
            if name == "queued":
                explanation = "The question was accepted and a durable run was created."
            elif name == "retrieving":
                explanation = "The server began retrieving evidence from the frozen scope."
            elif name == "evidence_selected":
                explanation = f"The server selected {metadata.get('count', 0)} passages before asking the model."
            elif name == "assembling":
                explanation = "The selected passages were assembled into the model's evidence context."
            elif name == "context_truncated":
                explanation = f"The evidence context was shortened to the configured {metadata.get('context_token_budget')} token budget."
            elif name == "provider_request":
                explanation = "The application sent the question and evidence context to the configured model provider."
            elif name == "tool_call":
                outcome = metadata.get("outcome")
                if outcome == "new_evidence":
                    explanation = f"The model requested a search; the server added {metadata.get('new_evidence_count', 0)} new evidence item(s) from {metadata.get('result_count', 0)} result(s)."
                elif outcome == "repeated_query":
                    explanation = "The model repeated an earlier search, so the server stopped retrieval and handed control back for synthesis."
                else:
                    explanation = f"The model requested a search; it produced no new evidence ({outcome or 'no_progress'}), so the server handed control back for synthesis."
            elif name == "tool_no_progress":
                explanation = "Retrieval made no progress. Further equivalent searches were disabled so the model could answer with the available evidence or explain what was missing."
            elif name == "tool_fallback":
                explanation = "The provider rejected native tools, so automatic mode retried once without tools; no hidden retrieval was performed."
            elif name == "tool_call_rejected":
                explanation = "The server rejected a malformed model tool request and did not execute it."
            elif name == "tool_call_limit":
                explanation = f"The configured maximum of {metadata.get('max_tool_calls')} model search calls was reached."
            elif name == "provider_response":
                explanation = "The model returned a response containing the answer and/or reasoning content."
            elif name == "final_answer_rejected":
                explanation = "The provider returned another tool request as text instead of a final answer; the run was not accepted as complete."
            elif name == "citation_rejected":
                explanation = f"The provider cited markers that were not retrieved for this run ({', '.join(metadata.get('invalid_citations', []))}); the answer was not accepted."
            elif name == "failed":
                explanation = f"The run stopped without a valid answer ({event.get('error_code') or 'unknown error'}). It can be retried if the provider issue is transient."
            elif name == "validating":
                explanation = f"The server validated {metadata.get('citation_count', 0)} citations against persisted evidence."
            elif name == "completed":
                explanation = "The answer and its evidence references were persisted successfully."
            else:
                explanation = "A persisted run event was recorded."
            timeline.append({**event, "explanation": explanation})
        return timeline

    @staticmethod
    def diagnostics(run):
        """Return a presentation-neutral, actionable run-inspector record."""
        events = list(run.events.all())
        provider = (run.model_metadata or {}).get("provider") or {}
        if not isinstance(provider, dict):
            provider = {}
        provider = {key: value for key, value in provider.items()
                    if key not in {"reasoning_content", "prompt", "messages"}}
        evidence = list(run.evidence_items.select_related(
            "source_document", "processed_revision", "page", "page_region"
        ).order_by("ordinal"))
        tool_events = [event for event in events if event.name == "tool_call"]
        no_progress_events = [event for event in events if event.name == "tool_no_progress"]
        retrieval_events = [event for event in events if event.name in {"retrieving", "tool_call", "evidence_selected", "context_truncated"}]
        started = run.started_at or run.created_at
        ended = run.finished_at or (events[-1].created_at if events and run.state in {"failed", "cancelled"} else None)
        duration_ms = max(0, int((ended - started).total_seconds() * 1000)) if ended else None
        failure_code = run.error_code or ""
        failure_labels = {
            "final_answer_timeout": "Final answer request timed out",
            "provider_request_timeout": "Provider retrieval request timed out",
            "run_wall_clock_timeout": "Research run exceeded wall-clock budget",
            "worker_interrupted": "Worker was interrupted",
            "provider_connection_error": "Provider connection failed",
            "provider_unreachable": "Provider is unreachable",
            "provider_rejected": "Provider rejected the request",
        }
        attachment_ids = (run.scope_snapshot or {}).get("attachment_ids", [])
        if attachment_ids and evidence:
            actual_path = "direct attachment context" + (" + model-requested search" if tool_events else "")
        else:
            actual_path = "model-requested search" if tool_events else "no retrieval call"
        phases = []
        for event in events:
            phase = event.metadata.get("phase") if isinstance(event.metadata, dict) else None
            phases.append({
                "name": event.name, "status": "failed" if event.error_code else "completed",
                "started_at": event.created_at.isoformat(), "duration_ms": event.duration_ms,
                "phase": phase, "error_code": event.error_code,
                "summary": event.metadata.get("query") if isinstance(event.metadata, dict) and event.metadata.get("query") else event.name,
            })
        cited = sorted({marker for item in evidence for marker in [item.marker]
                        if run.assistant_message and f"[{marker}]" in run.assistant_message.text})
        referenced = sorted(set(re.findall(r"\[(S\d+)\]", run.assistant_message.text if run.assistant_message else "")))
        return {
            "run": {"id": run.id, "thread_id": run.thread_id, "state": run.state,
                     "status": run.status_message, "request_id": run.request_id,
                     "question": run.user_message.text if run.user_message_id else ""},
            "outcome": {"state": run.state, "duration_ms": duration_ms,
                         "stage": run.get_state_display(), "failure_code": failure_code,
                         "failure_category": failure_labels.get(failure_code, failure_code or None)},
            "timing": {"created_at": run.created_at.isoformat(),
                       "started_at": run.started_at.isoformat() if run.started_at else None,
                       "finished_at": run.finished_at.isoformat() if run.finished_at else None},
            "limits": {"wall_clock_timeout": getattr(settings, "DSW_CHAT_WALL_CLOCK_TIMEOUT", 900),
                       "request_timeout": getattr(settings, "DSW_CHAT_TIMEOUT", 600),
                       "tool_request_timeout": getattr(settings, "DSW_CHAT_TOOL_REQUEST_TIMEOUT", 300),
                       "final_request_timeout": getattr(settings, "DSW_CHAT_FINAL_REQUEST_TIMEOUT", 600)},
            "provider": {"model": run.thread.model, "configured_mode": getattr(settings, "DSW_CHAT_TOOL_MODE", "fallback"), **provider},
            "phases": phases,
            "retrieval": {"path": actual_path, "tool_call_count": len(tool_events),
                          "queries": [event.metadata.get("query") for event in tool_events if event.metadata.get("query")],
                          "outcomes": [event.metadata.get("outcome") for event in tool_events if event.metadata.get("outcome")],
                          "no_progress_count": len(no_progress_events),
                          "evidence_count": len(evidence), "source_tokens": run.source_tokens},
            "evidence": [{"marker": item.marker, "filename": item.source_document.filename,
                          "document_id": item.source_document_id, "revision_id": item.processed_revision_id,
                          "page": item.page.page_number if item.page else None,
                          "region_id": item.page_region_id, "score": item.score,
                          "selection_reason": item.selection_reason, "passage": item.text,
                          "page_text": item.page_text} for item in evidence],
            "citations": {"referenced": referenced, "persisted": [item.marker for item in evidence],
                          "validated": cited, "missing": sorted(set(referenced) - {item.marker for item in evidence}),
                          "unused": sorted({item.marker for item in evidence} - set(referenced)),
                          "valid": not (set(referenced) - {item.marker for item in evidence})},
            "failure": {"code": failure_code, "message": run.error_message,
                         "retry_eligible": run.state == "failed"},
            "retry": {"eligible": run.state == "failed", "reason": "Retry the durable run after addressing the reported failure." if run.state == "failed" else None},
            "raw": {"events": [{"name": event.name, "metadata": event.metadata, "error_code": event.error_code,
                                  "duration_ms": event.duration_ms, "created_at": event.created_at.isoformat()} for event in events],
                    "reasoning_present": bool(provider.get("reasoning_content"))},
        }


class CorrectionError(Exception):
    """A correction could not be applied safely."""


class CorrectionService:
    """Single application boundary for human corrections."""

    @staticmethod
    def apply(*, region, user, operation, before, after, reason="", policy=None, expected_current=None):
        from .policy import ProjectAccessPolicy

        policy = policy or ProjectAccessPolicy(user=user)
        if not policy.can_edit(region.page.document.collection):
            raise CorrectionError("You do not have permission to edit this project.")
        if operation not in dict(RegionCorrection.OPERATIONS):
            raise CorrectionError("Unsupported correction operation.")
        if expected_current is not None:
            current = {
                "text": region.effective_text,
                "type": region.effective_region_type,
                "suppress": "true" if region.is_suppressed else "false",
            }.get(operation, "")
            if current != expected_current:
                raise CorrectionError("The region changed before this correction was applied.")
        correction = RegionCorrection(
            region=region, document=region.page.document, created_by=user,
            operation=operation, before=before, after=after, reason=reason,
        )
        try:
            with transaction.atomic():
                correction.full_clean()
                correction.save()
        except ValidationError as exc:
            raise CorrectionError("The correction is not valid.") from exc
        return correction

    @staticmethod
    def revert(*, correction, user=None, policy=None):
        """Mark an active correction reverted and attribute it to ``user``.

        Existing rows that predate ``reverted_by`` stay null; only new reverts
        record the acting user. Reverting an already-reverted row is a no-op so
        the operation remains idempotent.
        """
        from .policy import ProjectAccessPolicy
        policy = policy or ProjectAccessPolicy(user=user)
        if not policy.can_edit(correction.document.collection):
            raise CorrectionError("You do not have permission to edit this project.")
        if correction.status == "active":
            with transaction.atomic():
                correction.status = "reverted"
                correction.reverted_at = timezone.now()
                correction.reverted_by = user
                correction.save(update_fields=["status", "reverted_at", "reverted_by"])
        return correction


class OcrService:
    """Shared lifecycle boundary for visual OCR candidates."""

    DEFAULT_REGION_PROMPT = (
        "Transcribe exactly the visible text. Preserve spelling, punctuation and line breaks. "
        "Do not translate, summarize, correct, infer, or add text. Return only the transcription."
    )
    DEFAULT_PAGE_PROMPT = (
        "Transcribe exactly all visible text on this page. Preserve layout with line breaks. "
        "Do not translate, summarize, correct, infer, or add text. Return only the transcription."
    )

    @staticmethod
    def create(*, page, region=None, provider, model="", prompt="", user=None, policy=None):
        from .policy import ProjectAccessPolicy
        policy = policy or ProjectAccessPolicy(user=user)
        if not policy.can_edit(page.document.collection):
            raise PermissionError("You do not have permission to edit this project.")
        from .processors.vision_ocr import OCR_PROVIDERS
        if provider not in OCR_PROVIDERS:
            raise ValueError(f"Unsupported visual OCR provider: {provider}")
        return OcrRequest.objects.create(
            source_document=page.document.processing_job.source_document,
            document=page.document, page=page, region=region,
            target="region" if region else "page", provider=provider,
            model=model, prompt=prompt or (OcrService.DEFAULT_REGION_PROMPT if region else OcrService.DEFAULT_PAGE_PROMPT),
            created_by=user,
        )

    @staticmethod
    def accept(*, item, user=None, policy=None, expected_current=None):
        from .policy import ProjectAccessPolicy
        policy = policy or ProjectAccessPolicy(user=user)
        with transaction.atomic():
            locked = OcrRequest.objects.select_for_update().select_related("document__collection", "region").get(pk=item.pk)
            if not policy.can_edit(locked.document.collection):
                raise PermissionError("You do not have permission to edit this project.")
            if locked.state != "completed" or not locked.region_id:
                raise ValueError("Only completed region OCR candidates can be accepted.")
            if locked.accepted_correction_id:
                return locked.accepted_correction
            correction = CorrectionService.apply(
                region=locked.region, user=user, policy=policy, operation="text",
                before={"text": locked.region.effective_text}, after={"text": locked.candidate_text},
                reason=f"Accepted visual OCR candidate #{locked.pk}",
                expected_current=expected_current,
            )
            locked.accepted_correction = correction
            locked.save(update_fields=["accepted_correction"])
            return correction


class HtrService:
    """Region-scoped handwritten-text recognition candidate lifecycle.

    HTR is deliberately separate from the full-page visual OCR rerun: it targets
    a single detected region, produces line-level output, and is always stored
    as a candidate (``OcrRequest(provider="htr")``). Acceptance reuses the
    normal ``RegionCorrection`` text path so existing revert semantics apply.
    """

    @staticmethod
    def create(*, page, region, pipeline_id="", user=None, policy=None):
        from .policy import ProjectAccessPolicy
        from .processors.htr import HTR_PROVIDER, HTR_SCHEMA_VERSION
        policy = policy or ProjectAccessPolicy(user=user)
        if not policy.can_edit(page.document.collection):
            raise PermissionError("You do not have permission to edit this project.")
        if region is None:
            raise ValueError("HTR is region-scoped; select a region first.")
        if region.page_id != page.id:
            raise ValueError("The selected region does not belong to this page.")
        pipeline = pipeline_id or getattr(
            settings, "DSW_HTR_DEFAULT_PIPELINE", "htrflow-trocr-prototype"
        )
        return OcrRequest.objects.create(
            source_document=page.document.processing_job.source_document,
            document=page.document, page=page, region=region,
            target="region", provider=HTR_PROVIDER, model="",
            prompt="htr",
            metadata={"pipeline_id": pipeline, "schema_version": HTR_SCHEMA_VERSION},
            created_by=user,
        )

    @staticmethod
    def accept(*, item, user=None, policy=None, expected_current=None):
        from .policy import ProjectAccessPolicy
        from .processors.htr import HTR_PROVIDER
        policy = policy or ProjectAccessPolicy(user=user)
        with transaction.atomic():
            locked = OcrRequest.objects.select_for_update().select_related(
                "document__collection", "region"
            ).get(pk=item.pk)
            if not policy.can_edit(locked.document.collection):
                raise PermissionError("You do not have permission to edit this project.")
            if locked.provider != HTR_PROVIDER or locked.state != "completed" or not locked.region_id:
                raise ValueError("Only completed region HTR candidates can be accepted.")
            if locked.accepted_correction_id:
                return locked.accepted_correction
            text = (locked.candidate_text or "").strip()
            if not text:
                raise ValueError("This HTR candidate has no text to accept.")
            correction = CorrectionService.apply(
                region=locked.region, user=user, policy=policy, operation="text",
                before={"text": locked.region.effective_text}, after={"text": text},
                reason=f"Accepted HTR transcription #{locked.pk}",
                expected_current=expected_current,
            )
            locked.accepted_correction = correction
            locked.save(update_fields=["accepted_correction"])
            return correction


class IngestionError(Exception):
    """Raised when upload/ingestion fails."""
    pass


class DocumentIngestionService:
    """Shared upload and ingestion logic."""

    ALLOWED_UPLOAD_EXTENSIONS = {"pdf", "jpg", "jpeg", "png", "tif", "tiff"}

    @property
    def max_file_size(self):
        """Per-file upload cap, configurable via DSW_UPLOAD_MAX_FILE_SIZE_BYTES."""
        return getattr(settings, "DSW_UPLOAD_MAX_FILE_SIZE_BYTES", 50 * 1024 * 1024)

    def __init__(self, *, user, policy=None):
        self.user = user
        if policy:
            self.policy = policy
        else:
            from .policy import ProjectAccessPolicy
            self.policy = ProjectAccessPolicy(user=user)

    def create_upload(
        self,
        *,
        project: Collection,
        uploaded_file,
        preset_slug: str = "quick-extraction",
    ) -> ProcessingJob:
        """
        Upload a PDF and create a processing job.

        Tracks both temporary path and final path for rollback.
        If DB creation fails after file move, deletes the final file.
        If duplicate source exists but file is missing, replaces it.
        """
        temp_path = None
        final_path = None

        try:
            # 1. Check project access
            if not self.policy.can_edit(project):
                raise IngestionError("You do not have edit access to this project.")

            # 2. Validate file
            self._validate_file(uploaded_file)

            # 3. Validate preset BEFORE writing file
            try:
                preset = ProcessingPreset.objects.get(
                    slug=preset_slug, is_active=True
                )
            except ProcessingPreset.DoesNotExist:
                raise IngestionError(
                    f"Processing preset '{preset_slug}' not found or inactive."
                )

            # 4. Stream to temp file while hashing
            file_hash, temp_path = self._stream_to_temp(project, uploaded_file)

            # 5. Check for duplicates
            existing = SourceDocument.objects.filter(
                collection=project,
                sha256=file_hash,
                is_archived=False,
            ).first()

            if existing:
                # Check if existing file is missing on disk
                existing_file_missing = False
                if existing.file_path:
                    existing_full = self._resolve_artifact_path(existing.file_path)
                    if existing_full is None or not existing_full.exists():
                        existing_file_missing = True

                existing_job = existing.processing_jobs.filter(
                    state__in=["queued", "submitting", "processing", "importing"]
                ).first()
                if existing_job:
                    self._cleanup_temp(temp_path)
                    raise IngestionError(
                        f"Document already uploaded (SHA: {file_hash[:12]}...). "
                        f"Job {existing_job.pk} is {existing_job.state}. "
                        f"Use reprocess to run extraction again."
                    )

                # Re-use existing SourceDocument
                if existing_file_missing:
                    # Replace missing file with new upload
                    source_doc = existing
                    source_doc.file_path = ""  # Will be set after move
                else:
                    # Keep existing file, discard temp
                    self._cleanup_temp(temp_path)
                    temp_path = None
                    source_doc = existing
            else:
                source_doc = None

            # 6. Finalize file placement and create records
            if source_doc is None:
                source_doc = SourceDocument(
                    collection=project,
                    source_type="upload",
                    filename=self._sanitize_filename(uploaded_file.name),
                    file_path="",
                    sha256=file_hash,
                    file_size=uploaded_file.size,
                    uploaded_by=self.user,
                )

            try:
                with transaction.atomic():
                    if temp_path and not source_doc.file_path:
                        # New upload or replacing missing file
                        storage_path = self._finalize_file(
                            project, temp_path, file_hash, uploaded_file.name
                        )
                        source_doc.file_path = storage_path
                        final_path = storage_path  # Track for rollback
                        source_doc.save()

                    job = ProcessingJob.objects.create(
                        source_document=source_doc,
                        preset=preset,
                        state="queued",
                        created_by=self.user,
                    )
                    job.capture_preset_snapshot()

                return job

            except Exception:
                # DB failed — rollback file if it was moved
                if final_path:
                    self._cleanup_final(final_path)
                raise

        except Exception:
            self._cleanup_temp(temp_path)
            if final_path:
                self._cleanup_final(final_path)
            raise

    def create_uploads(self, *, project, uploaded_files, preset_slug="quick-extraction"):
        """Ingest one or more files, creating a processing job per file.

        Each file is ingested independently so a single bad file (wrong type,
        too large, a duplicate with an active job, …) never aborts the rest of
        the batch. Returns ``(jobs, errors)`` where ``errors`` is a list of
        ``{"filename", "error"}`` dicts for files that could not be ingested.
        Processing itself is asynchronous: this only enqueues jobs, it does not
        run extraction.
        """
        jobs = []
        errors = []
        for uploaded_file in uploaded_files:
            try:
                job = self.create_upload(
                    project=project,
                    uploaded_file=uploaded_file,
                    preset_slug=preset_slug,
                )
            except IngestionError as exc:
                errors.append({"filename": uploaded_file.name, "error": str(exc)})
            else:
                jobs.append(job)
        return jobs, errors

    def retry_existing(self, *, job: ProcessingJob) -> ProcessingJob:
        """Queue a new processing attempt while retaining the old job."""
        project = job.source_document.collection
        if not project or not self.policy.can_edit(project):
            raise IngestionError("You do not have edit access to this project.")
        if job.source_document.is_archived:
            raise IngestionError("Archived uploads cannot be retried.")
        if job.source_document.processing_jobs.filter(
            state__in=["queued", "submitting", "processing", "importing"]
        ).exists():
            raise IngestionError("This upload already has an active processing attempt.")
        with transaction.atomic():
            retry = ProcessingJob.objects.create(
                source_document=job.source_document,
                preset=job.preset,
                preset_snapshot=job.preset_snapshot or {},
                state="queued",
                created_by=self.user,
            )
        return retry

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_file(self, uploaded_file):
        """Validate uploaded file type, size, and content."""
        if not uploaded_file.name or not uploaded_file.size:
            raise IngestionError("File is empty.")

        suffix = Path(uploaded_file.name).suffix.lower().lstrip(".")
        if suffix not in self.ALLOWED_UPLOAD_EXTENSIONS:
            raise IngestionError("Supported files are PDF, JPG, PNG, and TIFF.")

        if uploaded_file.size > self.max_file_size:
            raise IngestionError(
                f"File too large ({uploaded_file.size / 1024 / 1024:.1f} MB). "
                f"Maximum {self.max_file_size / 1024 / 1024:.0f} MB."
            )

        uploaded_file.seek(0)
        header = uploaded_file.read(5)
        uploaded_file.seek(0)
        if suffix == "pdf" and header != b"%PDF-":
            raise IngestionError("File does not appear to be a valid PDF (missing %PDF- header).")
        if suffix != "pdf":
            try:
                from PIL import Image
                uploaded_file.seek(0)
                with Image.open(uploaded_file) as image:
                    image.verify()
            except Exception as exc:
                raise IngestionError("The uploaded image is not a valid JPG, PNG, or TIFF file.") from exc
        uploaded_file.seek(0)

    # ------------------------------------------------------------------
    # Storage — stream to temp, then finalize
    # ------------------------------------------------------------------

    def _stream_to_temp(self, project, uploaded_file):
        """Stream to temp file while computing SHA-256."""
        artifacts_base = self._get_artifacts_base()
        temp_dir = artifacts_base / "tmp_uploads"
        temp_dir.mkdir(parents=True, exist_ok=True)

        sha256 = hashlib.sha256()
        fd, temp_path = tempfile.mkstemp(dir=str(temp_dir), suffix=".pdf.tmp")
        os.close(fd)

        try:
            with open(temp_path, "wb") as f:
                for chunk in uploaded_file.chunks():
                    sha256.update(chunk)
                    f.write(chunk)
        except Exception:
            self._cleanup_temp(temp_path)
            raise

        return sha256.hexdigest(), temp_path

    def _finalize_file(self, project, temp_path, file_hash, original_name):
        """Move temp file to final storage location with path containment check."""
        temp_path = Path(temp_path)
        if not temp_path.exists():
            raise IngestionError("Temporary file was lost during processing.")

        artifacts_base = self._get_artifacts_base()
        uploads_dir = artifacts_base / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)

        safe_name = self._sanitize_filename(original_name)
        filename = f"{file_hash[:12]}_{safe_name}"
        target = uploads_dir / filename

        # Explicit Boolean containment check
        base = artifacts_base.resolve()
        resolved = target.resolve()
        if not resolved.is_relative_to(base):
            raise IngestionError("Invalid upload path detected.")

        temp_path.rename(target)
        return f"uploads/{filename}"

    def _resolve_artifact_path(self, relative_path: str) -> Path:
        """Resolve and validate an artifact path. Returns None if invalid."""
        artifacts_base = self._get_artifacts_base()
        file_path = artifacts_base / relative_path

        try:
            resolved = file_path.resolve()
            base = artifacts_base.resolve()
            if not resolved.is_relative_to(base):
                return None
        except (OSError, ValueError):
            return None

        return resolved

    def _get_artifacts_base(self) -> Path:
        """Get the artifacts base directory."""
        return Path(
            getattr(settings, "ARTIFACTS_BASE_DIR", "/var/lib/dsw/artifacts")
        )

    def _cleanup_temp(self, temp_path):
        """Remove a temporary file if it exists."""
        if temp_path:
            try:
                path = Path(temp_path)
                if path.exists():
                    path.unlink()
                    logger.debug("Cleaned up temp file: %s", temp_path)
            except OSError as e:
                logger.warning("Failed to clean up temp file %s: %s", temp_path, e)

    def _cleanup_final(self, relative_path):
        """Remove a final file (for rollback after DB failure)."""
        try:
            resolved = self._resolve_artifact_path(relative_path)
            if resolved and resolved.exists():
                resolved.unlink()
                logger.debug("Cleaned up final file: %s", relative_path)
        except OSError as e:
            logger.warning("Failed to clean up final file %s: %s", relative_path, e)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        """Strip path components and unsafe characters from filename."""
        name = Path(name).name
        safe = "".join(
            c if c.isalnum() or c in ("_", "-", ".") else "_"
            for c in name
        )
        safe = safe.strip("_.")
        if len(safe) > 200:
            safe = safe[:200]
        return safe or "document"
