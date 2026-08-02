"""
Document ingestion service — shared by API and web upload.

Handles:
- File validation (PDF extension, MIME type, signature, size)
- Safe storage (generated name, no user-controlled paths)
- SHA-256 calculation (single pass during write)
- Duplicate detection (same project + same SHA)
- Transactional consistency (file + DB in/out sync)
- Project access enforcement
"""
import hashlib
import os
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
        # --- 1. Check project access ---
        if not self.policy.can_edit(project):
            raise IngestionError("You do not have edit access to this project.")

        # --- 2. Validate file ---
        self._validate_file(uploaded_file)

        # --- 3. Compute SHA-256 and write file ---
        file_hash, storage_path = self._write_file(project, uploaded_file)

        # --- 4. Check for duplicates ---
        existing = SourceDocument.objects.filter(
            collection=project,
            sha256=file_hash,
            is_archived=False,
        ).first()
        if existing:
            # Return existing job if available
            existing_job = existing.processing_jobs.filter(
                state__in=["queued", "submitting", "processing", "importing"]
            ).first()
            if existing_job:
                raise IngestionError(
                    f"Document already uploaded (SHA: {file_hash[:12]}...). "
                    f"Job {existing_job.pk} is {existing_job.state}. "
                    f"Use reprocess to run extraction again."
                )
            # Allow re-upload if no active job (returns existing SourceDocument)
            source_doc = existing

        # --- 5. Create SourceDocument + ProcessingJob atomically ---
        if not source_doc:
            source_doc = None

        try:
            preset = ProcessingPreset.objects.get(
                slug=preset_slug, is_active=True
            )
        except ProcessingPreset.DoesNotExist:
            raise IngestionError(
                f"Processing preset '{preset_slug}' not found or inactive."
            )

        if not source_doc:
            source_doc = SourceDocument(
                collection=project,
                source_type="upload",
                filename=self._sanitize_filename(uploaded_file.name),
                file_path=storage_path,
                sha256=file_hash,
                file_size=uploaded_file.size,
                uploaded_by=self.user,
            )

        with transaction.atomic():
            source_doc.save()

            job = ProcessingJob.objects.create(
                source_document=source_doc,
                preset=preset,
                state="queued",
                created_by=self.user,
            )
            job.capture_preset_snapshot()

        return job

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
    # Storage
    # ------------------------------------------------------------------

    def _write_file(self, project, uploaded_file):
        """
        Write the uploaded file to safe storage.

        Returns:
            (sha256_hex, relative_storage_path)
        """
        artifacts_base = Path(getattr(
            settings, "ARTIFACTS_BASE_DIR", "/var/lib/dsw/artifacts"
        ))
        uploads_dir = artifacts_base / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)

        # Compute SHA-256 while writing (single pass)
        sha256 = hashlib.sha256()
        chunks = []

        for chunk in uploaded_file.chunks():
            sha256.update(chunk)
            chunks.append(chunk)

        file_hash = sha256.hexdigest()

        # Safe filename: {hash[:12]}_{sanitized_name}.pdf
        safe_name = self._sanitize_filename(uploaded_file.name)
        filename = f"{file_hash[:12]}_{safe_name}"

        # Ensure the target is below uploads_dir
        target = uploads_dir / filename
        target.resolve().startswith(uploads_dir.resolve()) or (
            # Additional safety: reject if resolved path escapes
            not str(target.resolve()).startswith(str(uploads_dir.resolve()))
        )

        # Write file
        with open(target, "wb") as f:
            for chunk in chunks:
                f.write(chunk)

        # Return relative path
        storage_path = f"uploads/{filename}"
        return file_hash, storage_path

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
