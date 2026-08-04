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
import json
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from .models import (
    Collection,
    PageRegion,
    ProcessingJob,
    ProcessingPreset,
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


class ChatRunService:
    """Common execution boundary used by workers and future agent adapters."""
    @staticmethod
    def execute(run, worker_id="chat-worker"):
        from .chat import process_chat_run
        return process_chat_run(run, worker_id=worker_id)


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
        from .models import ChatRun
        latest = ChatRun.objects.filter(worker_heartbeat_at__isnull=False).order_by("-worker_heartbeat_at").first()
        return {"status": "ok" if database == "ok" else "error", "release": release, "database": database,
                "worker": {"status": "ok" if latest else "unknown", "last_seen": latest.worker_heartbeat_at.isoformat() if latest else None,
                            "queue_depth": ChatRun.objects.filter(state="queued").count() if database == "ok" else None},
                "chat_provider": {"configured": bool(getattr(settings, "DSW_CHAT_BASE_URL", "") and getattr(settings, "DSW_CHAT_MODEL", "")),
                                  "reachable": None, "model_configured": bool(getattr(settings, "DSW_CHAT_MODEL", "")), "model_available": None}}


class SupportBundleService:
    """Build a secret-free reconstruction of one authorized chat run."""
    @staticmethod
    def build(run):
        thread = run.thread
        provider = (run.model_metadata or {}).get("provider", {})
        sources = [{"id": source.id, "filename": source.filename, "revision_id": source.active_document_id}
                   for source in thread.selected_sources.all()]
        bundle = {
            "run": {"id": run.id, "state": run.state, "status": run.status_message, "error": run.error_message,
                    "created_at": run.created_at.isoformat(), "started_at": run.started_at.isoformat() if run.started_at else None,
                    "finished_at": run.finished_at.isoformat() if run.finished_at else None, "worker_id": run.worker_id,
                    "token_budget": run.token_budget, "source_tokens": run.source_tokens},
            "project": {"id": thread.project_id, "name": thread.project.name},
            "scope": {"sources": sources, "revision_ids": thread.selected_revisions or []},
            "question": run.user_message.text,
            "history": list(thread.messages.exclude(pk=run.user_message_id).order_by("ordinal").values("role", "text")),
            "evidence": [{"marker": item.marker, "source_document_id": item.source_document_id, "revision_id": item.processed_revision_id,
                          "page": item.page.page_number if item.page else None, "text": item.text, "score": item.score,
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
        lines = [f"# Chat support bundle — run {run['id']}", "", f"- State: {run['state']}", f"- Project: {bundle['project']['name']} ({bundle['project']['id']})", "", "## Question", bundle["question"], "", "## Final answer", bundle["final_answer"]]
        if bundle.get("reasoning_content"):
            lines += ["", "## Provider reasoning content (untrusted diagnostic output)", bundle["reasoning_content"]]
        lines += ["", "## Evidence"]
        for item in bundle["evidence"]:
            lines += [f"### [{item['marker']}] source {item['source_document_id']} page {item['page']}", item["text"], f"Score: {item['score']}; reason: {item['reason']}"]
        lines += ["", "## Timeline"]
        lines += [f"- {event['created_at']} — {event['name']} {event['error_code']}" for event in bundle["events"]]
        return "\n".join(lines) + "\n"


class CorrectionError(Exception):
    """A correction could not be applied safely."""


class CorrectionService:
    """Single application boundary for human corrections."""

    @staticmethod
    def apply(*, region, user, operation, before, after, reason=""):
        from .policy import ProjectAccessPolicy

        if not ProjectAccessPolicy(user=user).can_edit(region.page.document.collection):
            raise CorrectionError("You do not have permission to edit this project.")
        if operation not in dict(RegionCorrection.OPERATIONS):
            raise CorrectionError("Unsupported correction operation.")
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


class IngestionError(Exception):
    """Raised when upload/ingestion fails."""
    pass


class DocumentIngestionService:
    """Shared upload and ingestion logic."""

    MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

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

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_file(self, uploaded_file):
        """Validate uploaded file type, size, and content."""
        if not uploaded_file.name or not uploaded_file.size:
            raise IngestionError("File is empty.")

        if not uploaded_file.name.lower().endswith(".pdf"):
            raise IngestionError("Only PDF files are supported.")

        if uploaded_file.size > self.MAX_FILE_SIZE:
            raise IngestionError(
                f"File too large ({uploaded_file.size / 1024 / 1024:.1f} MB). "
                f"Maximum {self.MAX_FILE_SIZE / 1024 / 1024:.0f} MB."
            )

        uploaded_file.seek(0)
        header = uploaded_file.read(5)
        uploaded_file.seek(0)
        if header != b"%PDF-":
            raise IngestionError(
                "File does not appear to be a valid PDF "
                "(missing %PDF- header)."
            )

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
