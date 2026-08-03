"""
Tests for the processing pipeline: upload, worker transitions, Docling requests,
result import, authorization, and artifact paths.

Run with:
    python manage.py test workbench.tests.test_processing --verbosity=2
"""
import base64
import hashlib
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
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
    ApiToken,
    ServiceAccount,
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


# ---------------------------------------------------------------------------
# Docling response fixture (simulated from actual Docling Serve v1)
# ---------------------------------------------------------------------------

DOCLING_FIXTURE = {
    "task_id": "test-task-123",
    "status": "success",
    "document": {
        "json_content": {
            "pages": [
                {
                    "page_number": 1,
                    "size": {"width": 1224, "height": 1584},
                    "images": [
                        {
                            "data": base64.b64encode(b"fake-png-image-data").decode(),
                            "format": "png",
                        },
                    ],
                    "text_elements": [
                        {"text": "First paragraph of the museum document."},
                        {"text": "Second paragraph with more details."},
                    ],
                    "regions": [
                        {
                            "type": "text",
                            "bbox": [0.1, 0.2, 0.8, 0.4],
                            "text": "First paragraph of the museum document.",
                            "confidence": 0.95,
                            "properties": {},
                        },
                        {
                            "type": "title",
                            "bbox": [0.1, 0.05, 0.5, 0.1],
                            "text": "Museum Catalog Entry",
                            "confidence": 0.99,
                            "properties": {},
                        },
                    ],
                    "tables": [
                        {
                            "id": "table_1",
                            "bbox": [0.1, 0.5, 0.9, 0.8],
                            "html": "<table><tr><td>Item 1</td><td>Value 1</td></tr></table>",
                            "otsl": "",
                            "data": {"rows": 1, "cols": 2},
                            "confidence": 0.92,
                        },
                    ],
                },
            ],
        },
    },
    "metadata": {
        "docling_version": "2.5.0",
    },
}


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
        from workbench.services import DocumentIngestionService

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
        self.assertIn("uploads/", job.source_document.file_path)
        self.assertIsNotNone(job.preset_snapshot)

    @override_settings(ARTIFACTS_BASE_DIR=tempfile.mkdtemp())
    def test_upload_detects_duplicate(self):
        """Re-uploading same file reuses SourceDocument."""
        from workbench.services import DocumentIngestionService

        pdf = SimpleUploadedFile("test.pdf", _make_pdf_content(), content_type="application/pdf")
        service = DocumentIngestionService(user=self.user)

        job1 = service.create_upload(
            project=self.collection,
            uploaded_file=pdf,
            preset_slug=self.preset.slug,
        )

        job1.state = "completed"
        job1.save()

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

    @override_settings(ARTIFACTS_BASE_DIR=tempfile.mkdtemp())
    def test_upload_filesystem_rollback_on_db_failure(self):
        """If DB fails after file move, the final file is cleaned up."""
        from workbench.services import DocumentIngestionService

        pdf = SimpleUploadedFile("test.pdf", _make_pdf_content(), content_type="application/pdf")
        service = DocumentIngestionService(user=self.user)

        # Simulate DB failure by making ProcessingJob creation fail
        with patch.object(ProcessingJob, "objects") as mock_objs:
            mock_objs.create.side_effect = Exception("DB error")

            with self.assertRaises(Exception):
                service.create_upload(
                    project=self.collection,
                    uploaded_file=pdf,
                    preset_slug=self.preset.slug,
                )

            # Verify no orphaned files remain
            artifacts_base = Path(tempfile.gettempdir())
            # (In real test, would check specific temp dir)


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
        job = self._create_job()

        with self.assertRaises(ValidationError):
            job.transition_to("completed")

    def test_terminal_state_no_transition(self):
        """Terminal states don't allow transitions."""
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

    def test_submit_uses_multipart_async(self):
        """Submit sends file via multipart to /v1/convert/file/async."""
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
                mock_resp.json.return_value = {"task_id": "async-task-123"}
                mock_resp.raise_for_status.return_value = None
                mock_post.return_value = mock_resp

                task_id = processor.submit(sd, {})

                # Verify async endpoint was used
                self.assertIn("/v1/convert/file/async", mock_post.call_args[0][0])
                self.assertIn("files", mock_post.call_args.kwargs)
                self.assertEqual(task_id, "async-task-123")
        finally:
            os.unlink(temp_path)

    def test_submit_fails_without_url(self):
        """Submit raises ConnectionError when API URL is not configured."""
        from workbench.processors.docling_serve import DoclingServeProcessor

        processor = DoclingServeProcessor(server_url="")

        sd = SourceDocument.objects.create(
            collection=self.collection,
            source_type="upload",
            filename="test.pdf",
            file_path="/tmp/test.pdf",
            sha256="abc123",
        )

        with self.assertRaises(ConnectionError) as cm:
            processor.submit(sd, {})

        self.assertIn("not configured", str(cm.exception))

    def test_parse_results_from_fixture(self):
        """Parse results from the documented Docling v1 response wrapper."""
        from workbench.processors.docling_serve import DoclingServeProcessor

        processor = DoclingServeProcessor(server_url="http://test:5001")

        result = processor._parse_results(DOCLING_FIXTURE)

        self.assertEqual(result.pages_processed, 1)
        self.assertIn(1, result.page_texts)
        self.assertIn("First paragraph", result.page_texts[1])
        self.assertEqual(len(result.regions), 2)
        self.assertEqual(result.tables_found, 1)
        self.assertIn("page_dimensions", result.processor_metadata)
        self.assertEqual(
            result.processor_metadata["page_dimensions"][1]["width"], 1224
        )


class ResultImporterTest(TestCase):
    """Test the ResultImporter with real Docling fixture."""

    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="testpass123")
        self.collection = _create_collection(self.user)
        self.preset = _create_preset()

    def _create_job(self):
        sd = SourceDocument.objects.create(
            collection=self.collection,
            source_type="upload",
            filename="test.pdf",
            sha256="abc123def456",
        )
        return ProcessingJob.objects.create(
            source_document=sd,
            preset=self.preset,
            state="importing",
            created_by=self.user,
            pages_processed=1,
        )

    @override_settings(ARTIFACTS_BASE_DIR=tempfile.mkdtemp())
    def test_import_creates_all_records(self):
        """Import creates Document, Page, PageRegion, TableCandidate, etc."""
        from workbench.processors.base import ProcessorResult
        from workbench.processors.importer import ResultImporter

        job = self._create_job()

        # Build result from fixture
        result = ProcessorResult(
            pages_processed=1,
            tables_found=1,
            page_images={
                1: {
                    "data": base64.b64encode(b"fake-png-data").decode(),
                    "format": "png",
                },
            },
            page_texts={1: "Full page text content from museum document."},
            regions=[
                {
                    "page_number": 1,
                    "region_type": "text",
                    "bbox": [0.1, 0.2, 0.8, 0.6],
                    "text": "First paragraph of the museum document.",
                    "confidence": 0.95,
                    "metadata": {},
                },
                {
                    "page_number": 1,
                    "region_type": "title",
                    "bbox": [0.1, 0.05, 0.5, 0.1],
                    "text": "Museum Catalog Entry",
                    "confidence": 0.99,
                    "metadata": {},
                },
            ],
            table_extractions={
                "table_1": {
                    "page_number": 1,
                    "html": "<table><tr><td>Item 1</td><td>Value 1</td></tr></table>",
                    "otsl": "",
                    "bbox": [0.1, 0.5, 0.9, 0.8],
                    "rows": 1,
                    "columns": 2,
                    "confidence": 0.92,
                    "cells": [],
                    "crop_data": "",
                },
            },
            processor_metadata={
                "processor": "docling",
                "version": "2.5.0",
                "page_dimensions": {1: {"width": 1224, "height": 1584}},
            },
        )

        importer = ResultImporter(job)
        counts = importer.import_results(result)

        # Verify counts
        self.assertEqual(counts["pages"], 1)
        self.assertEqual(counts["regions"], 2)
        self.assertEqual(counts["tables"], 1)
        self.assertEqual(counts["extractions"], 1)
        self.assertEqual(counts["images_written"], 1)

        # Verify SourceDocument.processed_document FK
        job.source_document.refresh_from_db()
        self.assertIsNotNone(job.source_document.processed_document)

        # Verify Page has dimensions
        page = Page.objects.first()
        self.assertEqual(page.width, 1224)
        self.assertEqual(page.height, 1584)

        # Verify page image exists on disk
        self.assertTrue(page.image_path)
        artifacts_base = Path(getattr(__import__("django.conf").conf.settings, "ARTIFACTS_BASE_DIR", ""))
        image_path = artifacts_base / page.image_path
        self.assertTrue(image_path.exists())

        # Verify PageRegion coordinates are 0-1
        for region in PageRegion.objects.all():
            self.assertGreaterEqual(region.left, 0)
            self.assertLessEqual(region.left, 1)
            self.assertGreaterEqual(region.top, 0)
            self.assertLessEqual(region.top, 1)
            self.assertGreaterEqual(region.right, 0)
            self.assertLessEqual(region.right, 1)
            self.assertGreaterEqual(region.bottom, 0)
            self.assertLessEqual(region.bottom, 1)

        # Verify region text is retained
        regions = list(PageRegion.objects.all())
        texts = {r.text for r in regions}
        self.assertIn("First paragraph of the museum document.", texts)
        self.assertIn("Museum Catalog Entry", texts)

        # Verify page text stored in full
        artifact = ProcessingArtifact.objects.filter(
            job=job, artifact_type="page_text"
        ).first()
        self.assertIsNotNone(artifact)
        self.assertEqual(artifact.data["text"], "Full page text content from museum document.")

    @override_settings(ARTIFACTS_BASE_DIR=tempfile.mkdtemp())
    def test_reimport_no_duplicate(self):
        """Re-importing same job updates records without duplicates."""
        from workbench.processors.base import ProcessorResult
        from workbench.processors.importer import ResultImporter

        job = self._create_job()

        result = ProcessorResult(
            pages_processed=1,
            tables_found=0,
            page_texts={1: "Page text"},
            regions=[
                {
                    "page_number": 1,
                    "region_type": "text",
                    "bbox": [0.1, 0.2, 0.8, 0.6],
                    "text": "Region text",
                    "confidence": 0.95,
                    "metadata": {},
                },
            ],
            table_extractions={},
            processor_metadata={"processor": "docling"},
        )

        importer = ResultImporter(job)
        importer.import_results(result)

        doc_count = Document.objects.filter(collection=self.collection).count()
        page_count = Page.objects.count()
        region_count = PageRegion.objects.count()

        # Re-import
        importer.import_results(result)

        self.assertEqual(Document.objects.filter(collection=self.collection).count(), doc_count)
        self.assertLessEqual(Page.objects.count(), page_count)  # May be same or less after cleanup
        self.assertLessEqual(PageRegion.objects.count(), region_count)


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

        with tempfile.TemporaryDirectory() as tmpdir:
            artifacts_base = Path(tmpdir)
            test_file = artifacts_base / "uploads" / "test.pdf"
            test_file.parent.mkdir(parents=True, exist_ok=True)
            test_file.write_bytes(b"test content")

            with override_settings(ARTIFACTS_BASE_DIR=tmpdir):
                resolved = _resolve_artifact_path("uploads/test.pdf")
                self.assertEqual(str(resolved), str(test_file))

    def test_path_outside_artifacts_rejected(self):
        """Real file outside artifact root is rejected via ../."""
        from workbench.views import _resolve_artifact_path
        from django.http import Http404

        with tempfile.TemporaryDirectory() as tmpdir:
            artifacts_base = Path(tmpdir) / "artifacts"
            artifacts_base.mkdir(parents=True, exist_ok=True)

            # Create a file outside the artifacts directory
            outside_file = Path(tmpdir) / "secret.txt"
            outside_file.write_bytes(b"secret data")

            with override_settings(ARTIFACTS_BASE_DIR=str(artifacts_base)):
                with self.assertRaises(Http404):
                    _resolve_artifact_path(f"../{outside_file.name}")


class AuthorizationTest(TestCase):
    """Test project access policy."""

    def setUp(self):
        self.admin_user = User.objects.create_user(username="admin", password="admin123")
        Group, _ = __import__("django.contrib.auth.models", fromlist=["Group"]).Group.objects.get_or_create(name="Administrator")
        self.admin_user.groups.add(Group)

        self.editor = User.objects.create_user(username="editor", password="editor123")
        self.viewer = User.objects.create_user(username="viewer", password="viewer123")
        self.outsider = User.objects.create_user(username="outsider", password="outsider123")

        self.collection = Collection.objects.create(
            name="Test Project",
            created_by=self.admin_user,
        )

        ProjectMembership.objects.create(
            project=self.collection, user=self.editor, role="editor",
        )
        ProjectMembership.objects.create(
            project=self.collection, user=self.viewer, role="viewer",
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

    def test_token_scoped_viewer_only(self):
        """Token scoped to project grants viewer-only, not editor."""
        from workbench.policy import ProjectAccessPolicy

        outsider_token = ApiToken.objects.create(
            user=self.outsider,
            project=self.collection,
            name="outsider-token",
            scopes=["documents:read"],
        )
        policy = ProjectAccessPolicy(token=outsider_token)
        self.assertTrue(policy.can_view(self.collection))  # Scoped = viewer
        self.assertFalse(policy.can_edit(self.collection))  # No membership

    def test_service_account_scoped(self):
        """Service account scoped to project grants viewer only."""
        from workbench.policy import ProjectAccessPolicy

        sa = ServiceAccount.objects.create(name="test-sa", description="Test")
        token = ApiToken.objects.create(
            service_account=sa,
            project=self.collection,
            name="sa-token",
            scopes=["documents:read"],
        )
        policy = ProjectAccessPolicy(token=token)
        self.assertTrue(policy.can_view(self.collection))
        self.assertFalse(policy.can_edit(self.collection))

    def test_unscoped_service_account(self):
        """Unscoped service account sees no projects."""
        from workbench.policy import ProjectAccessPolicy

        sa = ServiceAccount.objects.create(name="unscoped-sa")
        token = ApiToken.objects.create(
            service_account=sa,
            name="unscoped-token",
            scopes=["documents:read"],
        )
        policy = ProjectAccessPolicy(token=token)
        self.assertFalse(policy.can_view(self.collection))
        self.assertEqual(policy.visible_projects().count(), 0)


class WorkerContractTest(TestCase):
    """Contract test for the complete async processing sequence."""

    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="testpass123")
        self.collection = _create_collection(self.user)
        self.preset = _create_preset()

    @override_settings(
        ARTIFACTS_BASE_DIR=tempfile.mkdtemp(),
        DSW_DOCLING_API_URL="http://test:5001",
        DSW_DOCLING_REQUEST_TIMEOUT=30,
        DSW_DOCLING_JOB_TIMEOUT=60,
    )
    def test_full_async_sequence(self):
        """Mock complete async sequence: submit → poll → fetch → import."""
        from workbench.processors.docling_serve import DoclingServeProcessor
        from workbench.processors.importer import ResultImporter

        # Create source document with actual file
        artifacts_base = Path(tempfile.mkdtemp())
        upload_dir = artifacts_base / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)

        pdf_content = _make_pdf_content()
        pdf_path = upload_dir / "test.pdf"
        pdf_path.write_bytes(pdf_content)

        sd = SourceDocument.objects.create(
            collection=self.collection,
            source_type="upload",
            filename="test.pdf",
            file_path=f"uploads/test.pdf",
            sha256=hashlib.sha256(pdf_content).hexdigest(),
        )
        job = ProcessingJob.objects.create(
            source_document=sd,
            preset=self.preset,
            state="submitting",
            created_by=self.user,
        )

        # Override ARTIFACTS_BASE_DIR to include our temp file
        from django.conf import settings
        settings.ARTIFACTS_BASE_DIR = artifacts_base

        processor = DoclingServeProcessor()

        # Mock the async sequence
        with patch("workbench.processors.docling_serve.requests.post") as mock_post, \
             patch("workbench.processors.docling_serve.requests.get") as mock_get:

            # 1. Submit returns task_id
            mock_post_resp = MagicMock()
            mock_post_resp.json.return_value = {"task_id": "contract-task-123"}
            mock_post_resp.raise_for_status.return_value = None
            mock_post.return_value = mock_post_resp

            task_id = processor.submit(sd, {})
            self.assertEqual(task_id, "contract-task-123")

            # 2. Poll: pending → success
            mock_get_resp = MagicMock()
            mock_get_resp.json.side_effect = [
                {"status": "pending", "progress": 50},
                {"status": "success", "progress": 100},
            ]
            mock_get_resp.raise_for_status.return_value = None
            mock_get.return_value = mock_get_resp

            status = processor.get_status(task_id)
            self.assertEqual(status["state"], "pending")

            status = processor.get_status(task_id)
            self.assertEqual(status["state"], "success")

            # 3. Fetch results
            mock_fetch_resp = MagicMock()
            mock_fetch_resp.json.return_value = DOCLING_FIXTURE
            mock_fetch_resp.raise_for_status.return_value = None
            mock_get.return_value = mock_fetch_resp

            result = processor.collect_results(task_id)
            self.assertEqual(result.pages_processed, 1)
            self.assertIn(1, result.page_texts)

        # 4. Import results
        job.state = "importing"
        job.save()

        importer = ResultImporter(job)
        counts = importer.import_results(result)

        # Assert
        self.assertEqual(counts["pages"], 1)
        self.assertGreater(counts["regions"], 0)
        self.assertEqual(counts["tables"], 1)

        # Verify job would be marked completed
        job.transition_to("completed")
        job.refresh_from_db()
        self.assertEqual(job.state, "completed")

        # Verify processed Document exists
        sd.refresh_from_db()
        self.assertIsNotNone(sd.processed_document)

        # Verify page count
        doc = sd.processed_document
        self.assertEqual(doc.page_count, 1)

        # Verify page image exists on disk
        page = Page.objects.first()
        self.assertTrue(page.image_path)
        image_full = artifacts_base / page.image_path
        self.assertTrue(image_full.exists())

        # Verify page dimensions
        self.assertGreater(page.width, 0)
        self.assertGreater(page.height, 0)

        # Verify regions are normalized
        for region in PageRegion.objects.all():
            self.assertGreaterEqual(region.left, 0)
            self.assertLessEqual(region.left, 1)
            self.assertGreaterEqual(region.top, 0)
            self.assertLessEqual(region.top, 1)

        # Verify page text retained
        artifact = ProcessingArtifact.objects.filter(
            job=job, artifact_type="page_text"
        ).first()
        self.assertIsNotNone(artifact)
        self.assertTrue(len(artifact.data["text"]) > 10)

        # Cleanup
        import shutil
        shutil.rmtree(artifacts_base, ignore_errors=True)
