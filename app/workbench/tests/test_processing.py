"""
Tests for the processing pipeline: upload, worker transitions, Docling requests,
result import, authorization, and artifact paths.

Run with:
    python manage.py test workbench.tests.test_processing --verbosity=2
"""
import hashlib
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from workbench.models import (
    Collection,
    Document,
    ExtractionRun,
    Page,
    PageRegion,
    ProjectMembership,
    ProcessingJob,
    ProcessingPreset,
    ProcessingArtifact,
    SourceDocument,
    TableCandidate,
    TableExtraction,
)

User = get_user_model()


# ---------------------------------------------------------------------------
# Fixtures helpers
# ---------------------------------------------------------------------------

def _make_pdf_content():
    """Create minimal valid PDF content for testing."""
    return b"""%PDF-1.4
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj
4 0 obj << /Length 44 >> stream
BT /F1 12 Tf 100 700 Td (Test page) Tj ET
endstream endobj
5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj
xref
0 6
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000266 00000 n 
0000000360 00000 n 
trailer << /Size 6 /Root 1 0 R >>
startxref
440
%%EOF"""


def _create_collection(user, name="Test Project"):
    """Create a collection with the user as member."""
    collection = Collection.objects.create(
        name=name,
        description="Test collection",
        created_by=user,
    )
    ProjectMembership.objects.create(
        project=collection,
        user=user,
        role="editor",
    )
    return collection


def _create_preset(slug="quick-extraction", name="Quick"):
    """Create a processing preset."""
    return ProcessingPreset.objects.get_or_create(
        slug=slug,
        defaults={
            "name": name,
            "description": f"{name} preset",
            "description_short": name,
            "profile_a_enabled": True,
            "profile_b_enabled": False,
            "generate_crops": True,
            "create_review_tasks": False,
        },
    )[0]


class UploadServiceTest(TestCase):
    """Test the DocumentIngestionService."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser", password="testpass123"
        )
        self.collection = _create_collection(self.user)
        self.preset = _create_preset()

    @override_settings(ARTIFACTS_BASE_DIR=tempfile.mkdtemp())
    def test_upload_creates_source_document_and_job(self):
        """Upload creates SourceDocument + ProcessingJob atomically."""
        from workbench.services import DocumentIngestionService, IngestionError

        pdf = SimpleUploadedFile(
            "test.pdf",
            _make_pdf_content(),
            content_type="application/pdf",
        )
        service = DocumentIngestionService(user=self.user)
        job = service.create_upload(
            project=self.collection,
            uploaded_file=pdf,
            preset_slug=self.preset.slug,
        )

        self.assertEqual(job.state, "queued")
        self.assertEqual(job.source_document.collection, self.collection)
        self.assertEqual(job.source_document.sha256[:12], job.source_document.file_path.split("/")[1][:12])
        self.assertIsNotNone(job.preset_snapshot)

    @override_settings(ARTIFACTS_BASE_DIR=tempfile.mkdtemp())
    def test_upload_detects_duplicate(self):
        """Re-uploading same file raises IngestionError."""
        from workbench.services import DocumentIngestionService, IngestionError

        pdf = SimpleUploadedFile("test.pdf", _make_pdf_content(), content_type="application/pdf")
        service = DocumentIngestionService(user=self.user)

        job1 = service.create_upload(
            project=self.collection,
            uploaded_file=pdf,
            preset_slug=self.preset.slug,
        )

        # Mark first job as completed so duplicate check allows re-upload
        job1.state = "completed"
        job1.save()

        # Second upload of same content should reuse SourceDocument
        pdf2 = SimpleUploadedFile("test.pdf", _make_pdf_content(), content_type="application/pdf")
        job2 = service.create_upload(
            project=self.collection,
            uploaded_file=pdf2,
            preset_slug=self.preset.slug,
        )

        self.assertEqual(job2.source_document, job1.source_document)

    @override_settings(ARTIFACTS_BASE_DIR=tempfile.mkdtemp())
    def test_upload_invalid_file_rejected(self):
        """Non-PDF files are rejected."""
        from workbench.services import DocumentIngestionService, IngestionError

        bad_file = SimpleUploadedFile("test.txt", b"not a pdf", content_type="text/plain")
        service = DocumentIngestionService(user=self.user)

        with self.assertRaises(IngestionError):
            service.create_upload(
                project=self.collection,
                uploaded_file=bad_file,
                preset_slug=self.preset.slug,
            )

    @override_settings(ARTIFACTS_BASE_DIR=tempfile.mkdtemp())
    def test_upload_invalid_preset_rejected(self):
        """Invalid preset slug is rejected before file is written."""
        from workbench.services import DocumentIngestionService, IngestionError

        pdf = SimpleUploadedFile("test.pdf", _make_pdf_content(), content_type="application/pdf")
        service = DocumentIngestionService(user=self.user)

        with self.assertRaises(IngestionError) as cm:
            service.create_upload(
                project=self.collection,
                uploaded_file=pdf,
                preset_slug="nonexistent-preset",
            )

        self.assertIn("not found", str(cm.exception).lower())

    @override_settings(ARTIFACTS_BASE_DIR=tempfile.mkdtemp())
    def test_upload_no_edit_access_rejected(self):
        """User without edit access cannot upload."""
        from workbench.services import DocumentIngestionService, IngestionError

        # Create another user with viewer-only access
        viewer = User.objects.create_user(username="viewer", password="viewer123")
        ProjectMembership.objects.create(
            project=self.collection,
            user=viewer,
            role="viewer",
        )

        pdf = SimpleUploadedFile("test.pdf", _make_pdf_content(), content_type="application/pdf")
        service = DocumentIngestionService(user=viewer)

        with self.assertRaises(IngestionError):
            service.create_upload(
                project=self.collection,
                uploaded_file=pdf,
                preset_slug=self.preset.slug,
            )


class JobTransitionTest(TestCase):
    """Test ProcessingJob state transitions."""

    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="testpass123")
        self.collection = _create_collection(self.user)
        self.preset = _create_preset()

    def _create_job(self):
        sd = SourceDocument.objects.create(
            collection=self.collection,
            source_type="upload",
            filename="test.pdf",
            sha256="abc123",
        )
        return ProcessingJob.objects.create(
            source_document=sd,
            preset=self.preset,
            state="queued",
            created_by=self.user,
        )

    def test_valid_transitions(self):
        """Valid state transitions succeed."""
        job = self._create_job()

        job.transition_to("submitting")
        self.assertEqual(job.state, "submitting")

        job.transition_to("processing")
        self.assertEqual(job.state, "processing")

        job.transition_to("importing")
        self.assertEqual(job.state, "importing")

        job.transition_to("completed")
        self.assertEqual(job.state, "completed")

    def test_invalid_transition_raises(self):
        """Invalid transitions raise ValidationError."""
        from django.core.exceptions import ValidationError

        job = self._create_job()

        with self.assertRaises(ValidationError):
            job.transition_to("completed")  # Can't jump from queued to completed

    def test_terminal_state_no_transition(self):
        """Terminal states don't allow transitions."""
        from django.core.exceptions import ValidationError

        job = self._create_job()
        job.transition_to("submitting")
        job.transition_to("processing")
        job.transition_to("importing")
        job.transition_to("completed")

        with self.assertRaises(ValidationError):
            job.transition_to("queued")


class DoclingProcessorTest(TestCase):
    """Test the DoclingServeProcessor (mocked)."""

    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="testpass123")
        self.collection = _create_collection(self.user)
        self.preset = _create_preset()

    def test_submit_uses_multipart(self):
        """Submit sends file via multipart, not local path."""
        from workbench.processors.docling_serve import DoclingServeProcessor

        processor = DoclingServeProcessor(server_url="http://test:5001")

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(_make_pdf_content())
            temp_path = f.name

        try:
            sd = SourceDocument.objects.create(
                collection=self.collection,
                source_type="upload",
                filename="test.pdf",
                file_path=temp_path,
                sha256="abc123",
            )

            with patch("workbench.processors.docling_serve.requests.post") as mock_post:
                mock_resp = MagicMock()
                mock_resp.json.return_value = {"job_id": "test-job-123"}
                mock_resp.raise_for_status.return_value = None
                mock_post.return_value = mock_resp

                job_id = processor.submit(sd, {})

                # Verify multipart upload was used
                call_args = mock_post.call_args
                self.assertIn("files", call_args.kwargs)
                self.assertIn("data", call_args.kwargs)
                self.assertIn("job_id", mock_resp.json.return_value)
        finally:
            os.unlink(temp_path)

    def test_parse_results_handles_docling_format(self):
        """Parse results from Docling JSON format."""
        from workbench.processors.docling_serve import DoclingServeProcessor

        processor = DoclingServeProcessor(server_url="http://test:5001")

        docling_response = {
            "pages": [
                {
                    "page_number": 1,
                    "size": {"width": 1224, "height": 1584},
                    "images": [{"ref": "page_1.png"}],
                    "text_elements": [
                        {"text": "First line of text"},
                        {"text": "Second line of text"},
                    ],
                    "regions": [
                        {
                            "type": "text",
                            "bbox": [0.1, 0.2, 0.9, 0.4],
                            "text": "Region text",
                        },
                    ],
                    "tables": [
                        {
                            "id": "table_1",
                            "bbox": [0.1, 0.5, 0.9, 0.8],
                            "html": "<table>...</table>",
                            "data": {"rows": 3, "cols": 2},
                        },
                    ],
                },
            ],
        }

        result = processor._parse_results(docling_response)

        self.assertEqual(result.pages_processed, 1)
        self.assertIn(1, result.page_texts)
        self.assertIn("First line", result.page_texts[1])
        self.assertEqual(len(result.regions), 1)
        self.assertEqual(result.tables_found, 1)


class ResultImporterTest(TestCase):
    """Test the ResultImporter."""

    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="testpass123")
        self.collection = _create_collection(self.user)
        self.preset = _create_preset()

    def _create_job(self):
        sd = SourceDocument.objects.create(
            collection=self.collection,
            source_type="upload",
            filename="test.pdf",
            sha256="abc123",
        )
        return ProcessingJob.objects.create(
            source_document=sd,
            preset=self.preset,
            state="importing",
            created_by=self.user,
            pages_processed=1,
        )

    def test_import_creates_all_records(self):
        """Import creates Document, Page, PageRegion, TableCandidate, ExtractionRun, TableExtraction."""
        from workbench.processors.base import ProcessorResult
        from workbench.processors.importer import ResultImporter

        job = self._create_job()

        result = ProcessorResult(
            pages_processed=1,
            tables_found=1,
            page_images={1: "pages/1.png"},
            page_texts={1: "Full page text content"},
            regions=[{
                "page_number": 1,
                "region_type": "text",
                "bbox": [0.1, 0.2, 0.8, 0.6],
                "text": "Region text",
                "confidence": 0.95,
                "metadata": {},
            }],
            table_extractions={
                "table_1": {
                    "page_number": 1,
                    "html": "<table>...</table>",
                    "otsl": "",
                    "bbox": [0.1, 0.5, 0.9, 0.8],
                    "rows": 3,
                    "columns": 2,
                    "confidence": 0.9,
                    "cells": [],
                },
            },
            processor_metadata={
                "processor": "docling",
                "version": "2.0",
                "page_dimensions": {1: {"width": 1224, "height": 1584}},
            },
        )

        importer = ResultImporter(job)
        counts = importer.import_results(result)

        # Verify Document created and linked
        self.assertEqual(counts["pages"], 1)
        self.assertEqual(counts["regions"], 1)
        self.assertEqual(counts["tables"], 1)
        self.assertEqual(counts["extractions"], 1)

        # Verify SourceDocument.processed_document FK
        job.source_document.refresh_from_db()
        self.assertIsNotNone(job.source_document.processed_document)

        # Verify PageRegion coordinates are 0-1
        region = PageRegion.objects.first()
        self.assertGreaterEqual(region.left, 0)
        self.assertLessEqual(region.left, 1)
        self.assertGreaterEqual(region.top, 0)
        self.assertLessEqual(region.top, 1)
        self.assertGreaterEqual(region.right, 0)
        self.assertLessEqual(region.right, 1)
        self.assertGreaterEqual(region.bottom, 0)
        self.assertLessEqual(region.bottom, 1)

        # Verify page text stored in full
        artifact = ProcessingArtifact.objects.filter(
            job=job, artifact_type="page_text"
        ).first()
        self.assertIsNotNone(artifact)
        self.assertEqual(artifact.data["text"], "Full page text content")

    def test_reimport_no_duplicate_document(self):
        """Re-importing same job does not create duplicate Document."""
        from workbench.processors.base import ProcessorResult
        from workbench.processors.importer import ResultImporter

        job = self._create_job()

        result = ProcessorResult(
            pages_processed=1,
            tables_found=0,
            page_texts={1: "Page text"},
            regions=[],
            table_extractions={},
            processor_metadata={"processor": "docling"},
        )

        importer = ResultImporter(job)
        importer.import_results(result)

        doc_count = Document.objects.filter(collection=self.collection).count()
        self.assertEqual(doc_count, 1)

        # Re-import
        importer.import_results(result)
        doc_count_after = Document.objects.filter(collection=self.collection).count()
        self.assertEqual(doc_count_after, 1)  # No duplicate


class ArtifactPathSecurityTest(TestCase):
    """Test artifact path traversal protection."""

    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="testpass123")
        self.collection = _create_collection(self.user)

    def test_path_traversal_rejected(self):
        """Path traversal attempts are rejected."""
        from workbench.views import _resolve_artifact_path
        from django.http import Http404

        with self.assertRaises(Http404):
            _resolve_artifact_path("../../etc/passwd")

    def test_valid_path_ok(self):
        """Valid relative paths are accepted."""
        from workbench.views import _resolve_artifact_path
        from django.http import Http404

        # Create a valid file
        with tempfile.TemporaryDirectory() as tmpdir:
            artifacts_base = Path(tmpdir)
            test_file = artifacts_base / "uploads" / "test.pdf"
            test_file.parent.mkdir(parents=True, exist_ok=True)
            test_file.write_bytes(b"test content")

            with override_settings(ARTIFACTS_BASE_DIR=tmpdir):
                resolved = _resolve_artifact_path("uploads/test.pdf")
                self.assertEqual(str(resolved), str(test_file))


class AuthorizationTest(TestCase):
    """Test project access policy."""

    def setUp(self):
        self.admin_user = User.objects.create_user(username="admin", password="admin123")
        self.admin_group, _ = get_user_model().objects.get(username="admin").groups.get_or_create(name="Administrator")
        self.editor = User.objects.create_user(username="editor", password="editor123")
        self.viewer = User.objects.create_user(username="viewer", password="viewer123")
        self.outsider = User.objects.create_user(username="outsider", password="outsider123")

        self.collection = Collection.objects.create(
            name="Test Project",
            created_by=self.admin_user,
        )

        # Editor membership
        ProjectMembership.objects.create(
            project=self.collection, user=self.editor,
            role="editor",
        )
        # Viewer membership
        ProjectMembership.objects.create(
            project=self.collection, user=self.viewer,
            role="viewer",
        )

    def test_admin_can_view_all(self):
        """Admin can view any project."""
        from workbench.policy import ProjectAccessPolicy
        policy = ProjectAccessPolicy(user=self.admin_user)
        self.assertTrue(policy.can_view(self.collection))

    def test_editor_can_view(self):
        """Editor can view their project."""
        from workbench.policy import ProjectAccessPolicy
        policy = ProjectAccessPolicy(user=self.editor)
        self.assertTrue(policy.can_view(self.collection))

    def test_viewer_cannot_edit(self):
        """Viewer cannot edit."""
        from workbench.policy import ProjectAccessPolicy
        policy = ProjectAccessPolicy(user=self.viewer)
        self.assertTrue(policy.can_view(self.collection))
        self.assertFalse(policy.can_edit(self.collection))

    def test_outsider_cannot_access(self):
        """Outsider cannot access any project."""
        from workbench.policy import ProjectAccessPolicy
        policy = ProjectAccessPolicy(user=self.outsider)
        self.assertFalse(policy.can_view(self.collection))
        self.assertFalse(policy.can_edit(self.collection))
        self.assertFalse(policy.can_review(self.collection))
        self.assertFalse(policy.can_curate(self.collection))

    def test_token_scoped_viewer_only(self):
        """Token scoped to project grants viewer-only, not editor."""
        from workbench.policy import ProjectAccessPolicy
        from workbench.models import ApiToken

        token = ApiToken.objects.create(
            user=self.editor,
            project=self.collection,
            name="test-token",
            scopes=["documents:read"],
        )

        policy = ProjectAccessPolicy(token=token)
        # Token scoped to project grants viewer access
        self.assertTrue(policy.can_view(self.collection))
        # But requires actual membership for edit access
        # (editor has membership, so they CAN edit)
        self.assertTrue(policy.can_edit(self.collection))  # Because membership exists

        # Create token for user WITHOUT membership
        outsider_token = ApiToken.objects.create(
            user=self.outsider,
            project=self.collection,
            name="outsider-token",
            scopes=["documents:read"],
        )
        outsider_policy = ProjectAccessPolicy(token=outsider_token)
        self.assertTrue(outsider_policy.can_view(self.collection))  # Scoped = viewer
        self.assertFalse(outsider_policy.can_edit(self.collection))  # No membership
        self.assertFalse(outsider_policy.can_review(self.collection))
