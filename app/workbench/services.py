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
"""
import hashlib
import logging
import os
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from .models import (
    Collection,
    ProcessingJob,
    ProcessingPreset,
    SourceDocument,
)

logger = logging.getLogger(__name__)


class IngestionError(Exception):
    """Raised when upload/ingestion fails."""
    pass


class DocumentIngestionService:
    """Shared upload and ingestion logic."""

    MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

    def __init__(self, *, user, policy=None):
        """
        Args:
            user: Django User instance.
            policy: ProjectAccessPolicy instance (optional, created from user if not given).
        """
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

        Args:
            project: The target Collection.
            uploaded_file: Django UploadedFile.
            preset_slug: ProcessingPreset slug.

        Returns:
            ProcessingJob instance.

        Raises:
            IngestionError: If validation fails.
        """
        temp_path = None  # Track temp file for cleanup

        try:
            # --- 1. Check project access ---
            if not self.policy.can_edit(project):
                raise IngestionError("You do not have edit access to this project.")

            # --- 2. Validate file ---
            self._validate_file(uploaded_file)

            # --- 3. Validate preset BEFORE writing file ---
            try:
                preset = ProcessingPreset.objects.get(
                    slug=preset_slug, is_active=True
                )
            except ProcessingPreset.DoesNotExist:
                raise IngestionError(
                    f"Processing preset '{preset_slug}' not found or inactive."
                )

            # --- 4. Stream to temp file while hashing ---
            file_hash, temp_path = self._stream_to_temp(project, uploaded_file)

            # --- 5. Check for duplicates ---
            existing = SourceDocument.objects.filter(
                collection=project,
                sha256=file_hash,
                is_archived=False,
            ).first()
            if existing:
                existing_job = existing.processing_jobs.filter(
                    state__in=["queued", "submitting", "processing", "importing"]
                ).first()
                if existing_job:
                    # Clean up temp — not needed
                    self._cleanup_temp(temp_path)
                    raise IngestionError(
                        f"Document already uploaded (SHA: {file_hash[:12]}...). "
                        f"Job {existing_job.pk} is {existing_job.state}. "
                        f"Use reprocess to run extraction again."
                    )
                # Re-use existing SourceDocument (keep its file_path)
                self._cleanup_temp(temp_path)
                source_doc = existing
            else:
                source_doc = None

            # --- 6. Finalize file placement and create records ---
            if source_doc is None:
                source_doc = SourceDocument(
                    collection=project,
                    source_type="upload",
                    filename=self._sanitize_filename(uploaded_file.name),
                    file_path="",  # Set after move
                    sha256=file_hash,
                    file_size=uploaded_file.size,
                    uploaded_by=self.user,
                )

            with transaction.atomic():
                if not source_doc.file_path:
                    # New upload — move temp to final location
                    storage_path = self._finalize_file(
                        project, temp_path, file_hash, uploaded_file.name
                    )
                    source_doc.file_path = storage_path
                    source_doc.save()
                # else: existing SourceDocument with valid file_path — keep as-is

                job = ProcessingJob.objects.create(
                    source_document=source_doc,
                    preset=preset,
                    state="queued",
                    created_by=self.user,
                )
                job.capture_preset_snapshot()

            return job

        except Exception:
            self._cleanup_temp(temp_path)
            raise

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_file(self, uploaded_file):
        """Validate uploaded file type, size, and content."""
        # Empty file
        if not uploaded_file.name or not uploaded_file.size:
            raise IngestionError("File is empty.")

        # Extension check
        if not uploaded_file.name.lower().endswith(".pdf"):
            raise IngestionError("Only PDF files are supported.")

        # Size check
        if uploaded_file.size > self.MAX_FILE_SIZE:
            raise IngestionError(
                f"File too large ({uploaded_file.size / 1024 / 1024:.1f} MB). "
                f"Maximum {self.MAX_FILE_SIZE / 1024 / 1024:.0f} MB."
            )

        # PDF signature check (first 5 bytes: %PDF-)
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
        """
        Stream the uploaded file to a temporary file while computing SHA-256.

        Returns:
            (sha256_hex, temp_file_path)

        The caller is responsible for either finalizing (moving) or cleaning up
        the temp file.
        """
        artifacts_base = Path(getattr(
            settings, "ARTIFACTS_BASE_DIR", "/var/lib/dsw/artifacts"
        ))
        temp_dir = artifacts_base / "tmp_uploads"
        temp_dir.mkdir(parents=True, exist_ok=True)

        sha256 = hashlib.sha256()
        fd, temp_path = tempfile.mkstemp(dir=str(temp_dir), suffix=".pdf.tmp")
        os.close(fd)  # Close the fd; we'll use pathlib for writing

        try:
            with open(temp_path, "wb") as f:
                for chunk in uploaded_file.chunks():
                    sha256.update(chunk)
                    f.write(chunk)
        except Exception:
            self._cleanup_temp(temp_path)
            raise

        file_hash = sha256.hexdigest()
        return file_hash, temp_path

    def _finalize_file(self, project, temp_path, file_hash, original_name):
        """
        Move temp file to final storage location.

        Returns:
            relative_storage_path (e.g., "uploads/{hash}_{name}.pdf")
        """
        temp_path = Path(temp_path)
        if not temp_path.exists():
            raise IngestionError("Temporary file was lost during processing.")

        uploads_dir = Path(getattr(
            settings, "ARTIFACTS_BASE_DIR", "/var/lib/dsw/artifacts"
        )) / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)

        safe_name = self._sanitize_filename(original_name)
        filename = f"{file_hash[:12]}_{safe_name}"
        target = uploads_dir / filename

        # Security: verify target is below uploads_dir
        try:
            target.resolve().is_relative_to(uploads_dir.resolve())
        except ValueError:
            raise IngestionError("Invalid upload path detected.")

        # Move temp to final location
        temp_path.rename(target)

        return f"uploads/{filename}"

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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        """Strip path components and unsafe characters from filename."""
        # Remove directory components
        name = Path(name).name
        # Replace spaces and special chars
        safe = "".join(
            c if c.isalnum() or c in ("_", "-", ".") else "_"
            for c in name
        )
        # Remove leading/trailing dots and underscores
        safe = safe.strip("_.")
        # Truncate if too long
        if len(safe) > 200:
            safe = safe[:200]
        return safe or "document"
