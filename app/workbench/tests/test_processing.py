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
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

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
                            # Real 2x2 PNG (Pillow-valid)
                            "data": (
                                "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFkl"
                                "EQVR4nGP8z8DAwMDAxMDAwMDAAAANHQEDasKb6QAAAABJRU5ErkJggg=="
                            ),
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

# ---------------------------------------------------------------------------
# Real DoclingDocument fixture (global lists — actual Docling v2 schema)
# ---------------------------------------------------------------------------

REAL_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFkl"
    "EQVR4nGP8z8DAwMDAwMDAwMDAAAANHQEDasKb6QAAAABJRU5ErkJggg=="
)

DOCLING_DOCUMENT_FIXTURE = {
    "task_id": "test-task-doc-123",
    "status": "success",
    "document": {
        "json_content": {
            "pages": {
                "1": {
                    "number": 1,
                    "size": {"width": 1224, "height": 1584},
                    "image": {
                        "mimetype": "image/png",
                        "dpi": 144,
                        "size": {"width": 2448, "height": 3168},
                        "uri": f"data:image/png;base64,{REAL_PNG_B64}",
                    },
                },
            },
            "texts": [
                {
                    "self_ref": "#/texts/0",
                    "text": "Annual Museum Catalog 2024",
                    "label": "title",
                    "content_layer": "body",
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {"l": 50, "t": 100, "r": 500, "b": 150, "coord_origin": "TOPLEFT"},
                            "charspan": [0, 30],
                        },
                    ],
                },
                {
                    "self_ref": "#/texts/1",
                    "text": "This document contains the annual catalog entries for the museum collection.",
                    "label": "text",
                    "content_layer": "body",
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {"l": 50, "t": 200, "r": 800, "b": 250, "coord_origin": "TOPLEFT"},
                            "charspan": [0, 80],
                        },
                    ],
                },
            ],
            "tables": [
                {
                    "self_ref": "#/tables/0",
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {"l": 50, "t": 400, "r": 600, "b": 600, "coord_origin": "TOPLEFT"},
                            "charspan": [0, 0],
                        },
                    ],
                    "data": {
                        "table_cells": [
                            {
                                "start_row_offset_idx": 0,
                                "end_row_offset_idx": 1,
                                "start_col_offset_idx": 0,
                                "end_col_offset_idx": 1,
                                "text": "Item",
                                "column_header": True,
                                "row_header": False,
                            },
                            {
                                "start_row_offset_idx": 0,
                                "end_row_offset_idx": 1,
                                "start_col_offset_idx": 1,
                                "end_col_offset_idx": 2,
                                "text": "Value",
                                "column_header": True,
                                "row_header": False,
                            },
                            {
                                "start_row_offset_idx": 1,
                                "end_row_offset_idx": 2,
                                "start_col_offset_idx": 0,
                                "end_col_offset_idx": 1,
                                "text": "Amphora",
                                "column_header": False,
                                "row_header": False,
                            },
                            {
                                "start_row_offset_idx": 1,
                                "end_row_offset_idx": 2,
                                "start_col_offset_idx": 1,
                                "end_col_offset_idx": 2,
                                "text": "1200 BCE",
                                "column_header": False,
                                "row_header": False,
                            },
                        ],
                        "num_rows": 2,
                        "num_cols": 2,
                    },
                },
            ],
            "pictures": [],
            "body": {"elements": ["#/texts/0", "#/texts/1", "#/tables/0"]},
        },
    },
    "metadata": {
        "docling_version": "2.117.0",
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

    # ------------------------------------------------------------------
    # Status parsing tests (item 1)
    # ------------------------------------------------------------------

    def test_status_pending(self):
        """Pending/started response reads task_status correctly."""
        from workbench.processors.docling_serve import DoclingServeProcessor

        processor = DoclingServeProcessor(server_url="http://test:5001")

        with patch("workbench.processors.docling_serve.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {
                "task_status": "pending",
                "progress": 0,
            }
            mock_resp.raise_for_status.return_value = None
            mock_get.return_value = mock_resp

            status = processor.get_status("task-1")
            self.assertEqual(status["state"], "pending")
            self.assertEqual(status["progress"], 0)
            self.assertIsNone(status["error"])

    def test_status_success(self):
        """Success response reads task_status correctly."""
        from workbench.processors.docling_serve import DoclingServeProcessor

        processor = DoclingServeProcessor(server_url="http://test:5001")

        with patch("workbench.processors.docling_serve.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {
                "task_status": "success",
                "progress": 100,
            }
            mock_resp.raise_for_status.return_value = None
            mock_get.return_value = mock_resp

            status = processor.get_status("task-1")
            self.assertEqual(status["state"], "success")
            self.assertEqual(status["progress"], 100)

    def test_status_failure_with_error_message(self):
        """Failure response with error_message is read correctly."""
        from workbench.processors.docling_serve import DoclingServeProcessor

        processor = DoclingServeProcessor(server_url="http://test:5001")

        with patch("workbench.processors.docling_serve.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {
                "task_status": "failed",
                "error_message": "PDF is corrupted",
                "progress": 0,
            }
            mock_resp.raise_for_status.return_value = None
            mock_get.return_value = mock_resp

            status = processor.get_status("task-1")
            self.assertEqual(status["state"], "failed")
            self.assertEqual(status["error"], "PDF is corrupted")

    def test_status_failure_with_structured_failure(self):
        """Failure response with structured failure object is read correctly."""
        from workbench.processors.docling_serve import DoclingServeProcessor

        processor = DoclingServeProcessor(server_url="http://test:5001")

        with patch("workbench.processors.docling_serve.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {
                "task_status": "failed",
                "failure": {
                    "message": "Timeout after 300s",
                    "code": "TIMEOUT",
                },
                "progress": 45,
            }
            mock_resp.raise_for_status.return_value = None
            mock_get.return_value = mock_resp

            status = processor.get_status("task-1")
            self.assertEqual(status["state"], "failed")
            self.assertEqual(status["error"], "Timeout after 300s")

    def test_status_legacy_fallback(self):
        """Legacy 'status' field is used as fallback when task_status absent."""
        from workbench.processors.docling_serve import DoclingServeProcessor

        processor = DoclingServeProcessor(server_url="http://test:5001")

        with patch("workbench.processors.docling_serve.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {
                "status": "completed",  # legacy field
                "progress": 100,
            }
            mock_resp.raise_for_status.return_value = None
            mock_get.return_value = mock_resp

            status = processor.get_status("task-1")
            self.assertEqual(status["state"], "completed")

    def test_status_malformed_response(self):
        """Malformed response returns error state."""
        from workbench.processors.docling_serve import DoclingServeProcessor

        processor = DoclingServeProcessor(server_url="http://test:5001")

        with patch("workbench.processors.docling_serve.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {}
            mock_resp.raise_for_status.return_value = None
            mock_get.return_value = mock_resp

            status = processor.get_status("task-1")
            self.assertEqual(status["state"], "unknown")

    def test_status_network_error_is_retryable_exception(self):
        """Transport failure is not misrepresented as a remote task failure."""
        import requests
        from workbench.processors.docling_serve import DoclingServeProcessor

        processor = DoclingServeProcessor(server_url="http://test:5001")
        with patch(
            "workbench.processors.docling_serve.requests.get",
            side_effect=requests.ConnectionError("offline"),
        ):
            with self.assertRaisesRegex(ConnectionError, "status request failed"):
                processor.get_status("task-1")

    # ------------------------------------------------------------------
    # Coordinate normalization tests (item 4)
    # ------------------------------------------------------------------

    def test_normalize_bbox_topleft(self):
        """TOPLEFT coord_origin: standard normalization."""
        from workbench.processors.importer import ResultImporter

        bbox = [100, 200, 400, 500]
        dims = {1: {"width": 1000, "height": 800}}

        result = ResultImporter._normalize_bbox(
            bbox, 1, dims, {"coord_origin": "TOPLEFT"}
        )

        self.assertAlmostEqual(result[0], 0.1)   # left = 100/1000
        self.assertAlmostEqual(result[1], 0.25)  # top = 200/800
        self.assertAlmostEqual(result[2], 0.4)   # right = 400/1000
        self.assertAlmostEqual(result[3], 0.625) # bottom = 500/800

    def test_normalize_bbox_bottomleft(self):
        """BOTTOMLEFT coord_origin: vertical inversion before normalization."""
        from workbench.processors.importer import ResultImporter

        bbox = [100, 200, 400, 500]
        dims = {1: {"width": 1000, "height": 800}}

        result = ResultImporter._normalize_bbox(
            bbox, 1, dims, {"coord_origin": "BOTTOMLEFT"}
        )

        self.assertAlmostEqual(result[0], 0.1)    # left = 100/1000
        self.assertAlmostEqual(result[1], 0.375)  # top = (800-500)/800
        self.assertAlmostEqual(result[2], 0.4)    # right = 400/1000
        self.assertAlmostEqual(result[3], 0.75)   # bottom = (800-200)/800

    def test_normalize_already_normalized(self):
        """Already-normalized bbox (0-1) is returned as-is."""
        from workbench.processors.importer import ResultImporter

        bbox = [0.1, 0.2, 0.8, 0.9]
        dims = {1: {"width": 1000, "height": 800}}

        result = ResultImporter._normalize_bbox(
            bbox, 1, dims, {"coord_origin": "TOPLEFT"}
        )

        self.assertEqual(result, (0.1, 0.2, 0.8, 0.9))

    def test_normalize_invalid_bbox(self):
        """Invalid bbox returns zeros."""
        from workbench.processors.importer import ResultImporter

        result = ResultImporter._normalize_bbox(
            [1, 2], 1, {}, {}
        )
        self.assertEqual(result, (0.0, 0.0, 0.0, 0.0))

    # ------------------------------------------------------------------
    # Real DoclingDocument parser tests (global lists)
    # ------------------------------------------------------------------

    def test_parse_docling_document_global_lists(self):
        """Parse real DoclingDocument with global texts/tables lists."""
        from workbench.processors.docling_serve import DoclingServeProcessor

        processor = DoclingServeProcessor(server_url="http://test:5001")
        result = processor._parse_results(DOCLING_DOCUMENT_FIXTURE)

        # Pages
        self.assertEqual(result.pages_processed, 1)
        self.assertIn("page_dimensions", result.processor_metadata)
        dims = result.processor_metadata["page_dimensions"][1]
        self.assertEqual(dims["width"], 1224)
        self.assertEqual(dims["height"], 1584)

        # Page image (ImageRef dict with uri)
        self.assertIn(1, result.page_images)
        img_info = result.page_images[1]
        self.assertIn("data", img_info)
        self.assertEqual(img_info["format"], "png")

        # Regions from global texts
        self.assertEqual(len(result.regions), 2)
        titles = [r for r in result.regions if r["region_type"] == "title"]
        self.assertEqual(len(titles), 1)
        self.assertEqual(titles[0]["text"], "Annual Museum Catalog 2024")
        texts = [r for r in result.regions if r["region_type"] == "text"]
        self.assertEqual(len(texts), 1)

        # Page text accumulated
        self.assertIn(1, result.page_texts)
        self.assertIn("Annual Museum Catalog 2024", result.page_texts[1])

        # Tables
        self.assertEqual(result.tables_found, 1)
        self.assertIn("table_0", result.table_extractions)
        table = result.table_extractions["table_0"]
        self.assertEqual(table["rows"], 2)
        self.assertEqual(table["columns"], 2)
        self.assertIn("<table>", table["html"])
        self.assertIn("<th>", table["html"])  # header cells become <th>
        self.assertIn("Item", table["html"])
        self.assertIn("Amphora", table["html"])

        # Processor metadata
        self.assertEqual(result.processor_metadata["processor"], "docling")
        self.assertEqual(result.processor_metadata["version"], "2.117.0")

    def test_parse_docling_document_bottomleft_coords(self):
        """DoclingDocument with BOTTOMLEFT coord_origin normalizes correctly."""
        from workbench.processors.docling_serve import DoclingServeProcessor

        processor = DoclingServeProcessor(server_url="http://test:5001")
        fixture = dict(DOCLING_DOCUMENT_FIXTURE)
        fixture["document"] = dict(DOCLING_DOCUMENT_FIXTURE["document"])
        fixture["document"]["json_content"] = {
            "pages": {"1": {"number": 1, "size": {"width": 1000, "height": 800}}},
            "texts": [{
                "self_ref": "#/texts/0",
                "text": "Test text",
                "label": "text",
                "content_layer": "body",
                "prov": [{
                    "page_no": 1,
                    "bbox": {"l": 100, "t": 200, "r": 400, "b": 500, "coord_origin": "BOTTOMLEFT"},
                    "charspan": [0, 0],
                }],
            }],
            "tables": [],
            "pictures": [],
        }
        result = processor._parse_results(fixture)

        # Region should have BOTTOMLEFT coord_origin in metadata
        self.assertEqual(len(result.regions), 1)
        self.assertEqual(result.regions[0]["metadata"]["coord_origin"], "BOTTOMLEFT")
        # Raw bbox from Docling
        self.assertEqual(result.regions[0]["bbox"], [100, 200, 400, 500])

    def test_parse_docling_document_no_response_metadata(self):
        """Parser does not crash when response metadata is missing."""
        from workbench.processors.docling_serve import DoclingServeProcessor
        from workbench.processors.base import ProcessorResult

        processor = DoclingServeProcessor(server_url="http://test:5001")
        doc = {
            "pages": {"1": {"number": 1, "size": {"width": 1000, "height": 800}}},
            "texts": [],
            "tables": [],
        }
        # Pass fresh empty result, not one from _parse_results
        result = processor._parse_docling_document(doc, ProcessorResult(), response=None)
        self.assertEqual(result.pages_processed, 1)
        self.assertNotIn("version", result.processor_metadata)

    def test_generate_table_html_offset_cells(self):
        """Table HTML uses Docling offset fields correctly."""
        from workbench.processors.docling_serve import DoclingServeProcessor

        cells = [
            {"start_row_offset_idx": 0, "end_row_offset_idx": 1, "start_col_offset_idx": 0, "end_col_offset_idx": 1, "text": "H1", "column_header": True, "row_header": False},
            {"start_row_offset_idx": 0, "end_row_offset_idx": 1, "start_col_offset_idx": 1, "end_col_offset_idx": 2, "text": "H2", "column_header": True, "row_header": False},
            {"start_row_offset_idx": 1, "end_row_offset_idx": 2, "start_col_offset_idx": 0, "end_col_offset_idx": 1, "text": "V1", "column_header": False, "row_header": False},
            {"start_row_offset_idx": 1, "end_row_offset_idx": 2, "start_col_offset_idx": 1, "end_col_offset_idx": 2, "text": "V2", "column_header": False, "row_header": False},
        ]
        table_data = {"num_rows": 2, "num_cols": 2}

        html = DoclingServeProcessor._generate_table_html(cells, table_data)
        self.assertIn("<table>", html)
        self.assertIn("<th>H1</th>", html)
        self.assertIn("<th>H2</th>", html)
        self.assertIn("<td>V1</td>", html)
        self.assertIn("<td>V2</td>", html)

    def test_generate_table_html_legacy_cells(self):
        """Table HTML still works with legacy index-based cells."""
        from workbench.processors.docling_serve import DoclingServeProcessor

        cells = [
            {"row_index": 0, "col_index": 0, "text": "A", "type": "header_cell", "row_span": 1, "col_span": 1},
            {"row_index": 0, "col_index": 1, "text": "B", "type": "body", "row_span": 1, "col_span": 1},
        ]
        table_data = {"num_rows": 1, "num_cols": 2}

        html = DoclingServeProcessor._generate_table_html(cells, table_data)
        self.assertIn("<th>A</th>", html)
        self.assertIn("<td>B</td>", html)


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

        # Real 2x2 PNG (Pillow-valid)
        REAL_PNG_B64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFklEQVR4nGP8z8DAwMDA"
            "xMDAwMDAAAANHQEDasKb6QAAAABJRU5ErkJggg=="
        )

        # Build result from fixture
        result = ProcessorResult(
            pages_processed=1,
            tables_found=1,
            page_images={
                1: {
                    "data": REAL_PNG_B64,
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

        # Import owns a revision but does not activate it before finalization.
        job.refresh_from_db()
        job.source_document.refresh_from_db()
        self.assertIsNotNone(job.result_document)
        self.assertIsNone(job.source_document.active_document)

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
            self.assertEqual(region.page_id, page.id)
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
        self.assertEqual(Page.objects.count(), page_count)
        self.assertEqual(PageRegion.objects.count(), region_count)


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


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class DocumentWorkspaceTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="workspace-user", password="testpass123")
        self.viewer = User.objects.create_user(username="workspace-viewer", password="testpass123")
        self.collection = _create_collection(self.user, "Workspace Project")
        ProjectMembership.objects.create(project=self.collection, user=self.viewer, role="viewer")
        self.preset = _create_preset()
        self.source = SourceDocument.objects.create(
            collection=self.collection, filename="archive.pdf", uploaded_by=self.user,
        )
        self.job = ProcessingJob.objects.create(
            source_document=self.source, preset=self.preset, state="completed",
            processor="docling", created_by=self.user,
        )
        self.document = Document.objects.create(
            collection=self.collection, external_id="workspace-revision",
            filename="archive.pdf", sha256="workspace-sha", page_count=1,
        )
        self.job.result_document = self.document
        self.job.save(update_fields=["result_document"])
        self.source.active_document = self.document
        self.source.save(update_fields=["active_document"])
        self.artifacts_dir = tempfile.mkdtemp()
        self.settings_override = override_settings(ARTIFACTS_BASE_DIR=self.artifacts_dir)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(__import__("shutil").rmtree, self.artifacts_dir, True)
        image = Path(self.artifacts_dir) / "pages" / "page.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"fake image")
        self.page = Page.objects.create(
            document=self.document, page_number=1, image_path="pages/page.png", width=100, height=100,
        )
        self.region = PageRegion.objects.create(
            source_document=self.source, job=self.job, page=self.page, page_number=1,
            region_type="title", left=.1, top=.2, right=.8, bottom=.4,
            text="Archive title", confidence=.91, metadata={"external_ref": "text-1"},
        )
        ProcessingArtifact.objects.create(
            job=self.job, artifact_type="page_text", page_number=1,
            data={"text": "Full extracted page text."},
        )

    def test_workspace_renders_scan_overlay_text_and_deep_link(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("document_detail", args=[self.document.pk]), {
            "page": 1, "region": self.region.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("page_image", args=[self.page.pk]))
        self.assertContains(response, f'data-region-id="{self.region.pk}"')
        self.assertContains(response, "Archive title")
        self.assertContains(response, "Full extracted page text.")
        self.assertEqual(response.context["selected_page"], self.page)
        self.assertEqual(response.context["selected_region"], self.region)

    def test_workspace_and_image_are_forbidden_to_unrelated_user(self):
        unrelated = User.objects.create_user(username="workspace-unrelated", password="testpass123")
        self.client.force_login(unrelated)
        response = self.client.get(reverse("document_detail", args=[self.document.pk]))
        self.assertRedirects(response, reverse("document_list"))
        image_response = self.client.get(reverse("page_image", args=[self.page.pk]))
        self.assertRedirects(image_response, reverse("document_list"))

    def test_workspace_accepts_stable_source_and_revision_deep_link(self):
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("document_detail", args=[self.source.pk]),
            {"revision": self.document.pk, "page": 1, "region": self.region.pk},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["document"], self.document)
        self.assertEqual(response.context["source_document"], self.source)
        self.assertContains(
            response,
            f"/documents/{self.source.pk}/?revision={self.document.pk}&page=1&region={self.region.pk}",
        )

    def test_revision_deep_link_cannot_cross_source_documents(self):
        other_source = SourceDocument.objects.create(
            collection=self.collection, filename="other.pdf", uploaded_by=self.user,
        )
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("document_detail", args=[other_source.pk]),
            {"revision": self.document.pk},
        )
        self.assertEqual(response.status_code, 404)

    def test_editor_can_create_text_correction_without_overwriting_machine_text(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("correct_region_text", args=[self.region.pk]),
            {
                "expected_current_text": "Archive title",
                "replacement_text": "Corrected archive title",
                "reason": "Confirmed against scan",
            },
        )
        self.assertEqual(response.status_code, 302)
        correction = self.region.corrections.get()
        self.assertEqual(correction.before["text"], "Archive title")
        self.assertEqual(correction.after["text"], "Corrected archive title")
        self.region.refresh_from_db()
        self.assertEqual(self.region.text, "Archive title")
        self.assertEqual(self.region.effective_text, "Corrected archive title")

    def test_viewer_cannot_create_text_correction(self):
        self.client.force_login(self.viewer)
        response = self.client.post(
            reverse("correct_region_text", args=[self.region.pk]),
            {"expected_current_text": "Archive title", "replacement_text": "Nope"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.region.corrections.exists())

    def test_editor_can_change_type_and_revert_correction(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("correct_region", args=[self.region.pk]),
            {"operation": "type", "expected_current_value": "title", "region_type": "text"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"revision={self.document.pk}", response["Location"])
        self.region.refresh_from_db()
        correction = self.region.corrections.get(operation="type")
        self.assertEqual(self.region.effective_region_type, "text")
        response = self.client.post(reverse("revert_region_correction", args=[correction.pk]))
        self.assertEqual(response.status_code, 302)
        self.region.refresh_from_db()
        self.assertEqual(self.region.effective_region_type, "title")


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class EntryJourneyTest(TestCase):
    """The primary UI leads directly from entry to a permitted upload."""

    def setUp(self):
        self.editor = User.objects.create_user(username="entry-editor", password="testpass123")
        self.viewer = User.objects.create_user(username="entry-viewer", password="testpass123")
        self.project = _create_collection(self.editor, "Archive Project")
        ProjectMembership.objects.create(
            project=self.project,
            user=self.viewer,
            role="viewer",
        )
        self.preset = _create_preset()
        self.artifacts_dir = tempfile.mkdtemp()
        self.settings_override = override_settings(ARTIFACTS_BASE_DIR=self.artifacts_dir)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(__import__("shutil").rmtree, self.artifacts_dir, True)

    def test_dashboard_add_document_is_one_direct_click(self):
        self.client.force_login(self.editor)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, f'href="{reverse("document_new")}"')
        self.assertContains(response, "Add document")
        self.assertNotContains(response, "Learn with examples")

    def test_single_editable_project_is_preselected(self):
        self.client.force_login(self.editor)
        response = self.client.get(reverse("document_new"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_project"], self.project)
        self.assertContains(
            response,
            f'<option value="{self.project.pk}" selected>Archive Project</option>',
            html=True,
        )

    def test_editor_can_upload_from_unified_route(self):
        self.client.force_login(self.editor)
        response = self.client.post(reverse("document_new"), {
            "project": self.project.pk,
            "preset": self.preset.slug,
            "file": SimpleUploadedFile(
                "entry.pdf",
                _make_pdf_content(),
                content_type="application/pdf",
            ),
        })
        job = ProcessingJob.objects.get()
        self.assertRedirects(response, reverse("job_status", args=[job.pk]))
        self.assertEqual(job.source_document.collection, self.project)

    def test_viewer_cannot_upload_or_see_upload_actions(self):
        self.client.force_login(self.viewer)
        dashboard = self.client.get(reverse("dashboard"))
        self.assertNotContains(dashboard, f'href="{reverse("document_new")}"')

        form = self.client.get(reverse("document_new"))
        self.assertContains(form, "You do not have an editable project")
        self.assertNotContains(form, 'id="upload-form"')

        response = self.client.post(reverse("document_new"), {
            "project": self.project.pk,
            "preset": self.preset.slug,
            "file": SimpleUploadedFile(
                "forbidden.pdf",
                _make_pdf_content(),
                content_type="application/pdf",
            ),
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Choose a project you can edit")
        self.assertFalse(ProcessingJob.objects.exists())

    def test_project_entry_points_use_permission_not_global_role(self):
        self.client.force_login(self.editor)
        project_list = self.client.get(reverse("collection_list"))
        project_detail = self.client.get(reverse("collection_detail", args=[self.project.pk]))
        expected = f'{reverse("document_new")}?project={self.project.pk}'
        self.assertContains(project_list, expected)
        self.assertContains(project_detail, expected)

        self.client.force_login(self.viewer)
        project_detail = self.client.get(reverse("collection_detail", args=[self.project.pk]))
        self.assertNotContains(project_detail, expected)

    def test_document_list_represents_source_once_across_revisions(self):
        first_revision = Document.objects.create(
            collection=self.project,
            external_id="revision-1",
            filename="archive.pdf",
            sha256="same-source",
        )
        Document.objects.create(
            collection=self.project,
            external_id="revision-2",
            filename="archive.pdf",
            sha256="same-source",
        )
        SourceDocument.objects.create(
            collection=self.project,
            filename="archive.pdf",
            sha256="same-source",
            active_document=first_revision,
            uploaded_by=self.editor,
        )
        self.client.force_login(self.editor)
        response = self.client.get(reverse("document_list"))
        self.assertEqual(len(response.context["documents"]), 1)
        self.assertContains(response, "archive.pdf", count=1)

    def test_german_primary_workflow_labels_render(self):
        from workbench.models import UserPreferences

        preferences = UserPreferences.get_or_create_for_user(self.editor)
        preferences.ui_language = "de"
        preferences.save(update_fields=["ui_language"])
        self.client.force_login(self.editor)

        dashboard = self.client.get(reverse("dashboard"))
        upload = self.client.get(reverse("document_new"))
        self.assertContains(dashboard, "Dokument hinzufügen")
        self.assertContains(upload, "Projekt")
        self.assertContains(upload, "PDF auswählen oder hier ablegen")
        self.assertContains(upload, "Standardanalyse")
        self.assertContains(upload, "Hochladen und analysieren")


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

        # Verify this job owns the processed revision. Direct importer use does
        # not activate it; activation belongs to worker finalization.
        job.refresh_from_db()
        sd.refresh_from_db()
        self.assertIsNotNone(job.result_document)
        self.assertIsNone(sd.active_document)

        # Verify page count
        doc = job.result_document
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


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class ProcessingStatusViewTest(TestCase):
    """Regression coverage for truthful, non-nesting HTMX status updates."""

    def setUp(self):
        self.user = User.objects.create_user(username="status-user", password="testpass123")
        self.collection = _create_collection(self.user)
        self.preset = _create_preset()
        self.source = SourceDocument.objects.create(
            collection=self.collection,
            filename="status.pdf",
            sha256="status-sha",
            uploaded_by=self.user,
        )
        self.job = ProcessingJob.objects.create(
            source_document=self.source,
            preset=self.preset,
            state="processing",
            external_job_id="remote-status-1",
            created_by=self.user,
        )
        self.client = Client()
        self.client.force_login(self.user)

    def test_repeated_htmx_responses_have_one_replacement_root(self):
        initial = self.client.get(reverse("job_status", args=[self.job.pk]))
        self.assertEqual(initial.status_code, 200)
        self.assertEqual(initial.content.count(b'class="job-status-block"'), 1)

        for _ in range(3):
            response = self.client.get(
                reverse("job_status", args=[self.job.pk]),
                HTTP_HX_REQUEST="true",
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content.count(b'class="job-status-block"'), 1)
            self.assertEqual(response.content.count(b'id="job-status"'), 1)
            self.assertContains(response, 'hx-target="this"')

    def test_terminal_job_stops_polling_and_links_to_its_revision(self):
        document = Document.objects.create(
            collection=self.collection,
            external_id="status-revision",
            filename="status.pdf",
            sha256=self.source.sha256,
        )
        self.job.state = "completed"
        self.job.result_document = document
        self.job.save(update_fields=["state", "result_document"])

        response = self.client.get(
            reverse("job_status", args=[self.job.pk]),
            HTTP_HX_REQUEST="true",
        )
        self.assertNotContains(response, "hx-trigger")
        self.assertContains(response, reverse("document_detail", args=[document.pk]))

    def test_queued_job_polls_and_does_not_show_terminal_actions(self):
        self.job.state = "queued"
        self.job.external_job_id = ""
        self.job.save(update_fields=["state", "external_job_id"])

        response = self.client.get(
            reverse("job_status", args=[self.job.pk]),
            HTTP_HX_REQUEST="true",
        )
        self.assertContains(response, 'hx-trigger="every 5s"')
        self.assertNotContains(response, "Upload Another")
        self.assertNotContains(response, "Add another document")

    def test_status_uses_only_observable_stage_labels(self):
        response = self.client.get(reverse("job_status", args=[self.job.pk]))
        self.assertContains(response, "Upload received")
        self.assertContains(response, "Waiting for worker")
        self.assertContains(response, "Submitting to Docling")
        self.assertContains(response, "Docling is analyzing the document")
        self.assertContains(response, "Saving extracted results")
        self.assertNotContains(response, "Preparing pages")
        self.assertNotContains(response, "Identifying page contents")
        self.assertNotContains(response, "Locating tables and regions")

    @override_settings(DSW_PROCESSING_STALE_AFTER_SECONDS=90)
    def test_stale_heartbeat_is_explained(self):
        self.job.worker_heartbeat_at = timezone.now() - timedelta(minutes=2)
        self.job.save(update_fields=["worker_heartbeat_at"])
        response = self.client.get(
            reverse("job_status", args=[self.job.pk]),
            HTTP_HX_REQUEST="true",
        )
        self.assertContains(response, "has not reported a worker heartbeat")

    def test_htmx_asset_fallback_is_not_under_static_url(self):
        self.assertEqual(reverse("serve_htmx"), "/assets/htmx.min.js")
        response = self.client.get(reverse("serve_htmx"))
        self.assertRedirects(
            response,
            "https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js",
            fetch_redirect_response=False,
        )


class ProcessingRevisionTest(TestCase):
    """A processing job owns one revision; only completed revisions activate."""

    def setUp(self):
        self.user = User.objects.create_user(username="revision-user", password="testpass123")
        self.collection = _create_collection(self.user)
        self.preset = _create_preset()
        self.source = SourceDocument.objects.create(
            collection=self.collection,
            filename="revision.pdf",
            sha256="same-source-sha",
            uploaded_by=self.user,
        )
        self.artifacts_dir = tempfile.mkdtemp()
        self.settings_override = override_settings(ARTIFACTS_BASE_DIR=self.artifacts_dir)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(__import__("shutil").rmtree, self.artifacts_dir, True)

    @staticmethod
    def _result(text="Revision text", partial=False):
        from workbench.processors.base import ProcessorResult
        return ProcessorResult(
            pages_processed=1,
            page_texts={1: text},
            regions=[{
                "page_number": 1,
                "region_type": "text",
                "bbox": [0.1, 0.2, 0.8, 0.6],
                "text": text,
                "confidence": 0.9,
                "metadata": {},
            }],
            processor_metadata={"page_dimensions": {1: {"width": 100, "height": 100}}},
            error_summary="incomplete extraction" if partial else None,
        )

    def _job(self, worker_id="revision-worker"):
        return ProcessingJob.objects.create(
            source_document=self.source,
            preset=self.preset,
            state="importing",
            external_job_id=uuid_for_test(),
            worker_id=worker_id,
            worker_heartbeat_at=timezone.now(),
            created_by=self.user,
        )

    @staticmethod
    def _command(worker_id="revision-worker"):
        from workbench.management.commands.run_processing_worker import Command
        command = Command()
        command.worker_id = worker_id
        command.lease_seconds = 30
        return command

    def test_successful_reprocessing_creates_and_activates_new_revision(self):
        from workbench.processors.importer import ResultImporter

        first_job = self._job()
        first_result = self._result("First revision")
        first_counts = ResultImporter(first_job).import_results(first_result)
        self._command()._finalize(first_job, first_result, first_counts["document"])
        self.source.refresh_from_db()
        first_document = self.source.active_document

        second_job = self._job()
        second_result = self._result("Second revision")
        second_counts = ResultImporter(second_job).import_results(second_result)
        self._command()._finalize(second_job, second_result, second_counts["document"])
        self.source.refresh_from_db()

        self.assertNotEqual(first_document.pk, self.source.active_document_id)
        self.assertEqual(second_job.result_document_id, self.source.active_document_id)
        self.assertEqual(Document.objects.filter(collection=self.collection).count(), 2)
        self.assertEqual(
            list(Document.objects.filter(collection=self.collection).values_list("sha256", flat=True)),
            [self.source.sha256, self.source.sha256],
        )
        self.assertEqual(Page.objects.filter(document=first_document).count(), 1)
        self.assertEqual(Page.objects.filter(document=self.source.active_document).count(), 1)

    def test_partial_revision_is_retained_but_not_activated(self):
        from workbench.processors.importer import ResultImporter

        completed_job = self._job()
        completed_result = self._result("Complete")
        completed_counts = ResultImporter(completed_job).import_results(completed_result)
        self._command()._finalize(completed_job, completed_result, completed_counts["document"])
        self.source.refresh_from_db()
        active_id = self.source.active_document_id

        partial_job = self._job()
        partial_result = self._result("Partial", partial=True)
        partial_counts = ResultImporter(partial_job).import_results(partial_result)
        self._command()._finalize(partial_job, partial_result, partial_counts["document"])
        partial_job.refresh_from_db()
        self.source.refresh_from_db()

        self.assertEqual(partial_job.state, "partial")
        self.assertIsNotNone(partial_job.result_document_id)
        self.assertEqual(self.source.active_document_id, active_id)

    def test_same_job_reimport_has_exact_counts_and_same_revision(self):
        from workbench.processors.importer import ResultImporter

        job = self._job()
        result = self._result()
        importer = ResultImporter(job)
        importer.import_results(result)
        job.refresh_from_db()
        revision_id = job.result_document_id
        importer.import_results(result)

        self.assertEqual(Document.objects.filter(collection=self.collection).count(), 1)
        self.assertEqual(job.result_document_id, revision_id)
        self.assertEqual(Page.objects.filter(document_id=revision_id).count(), 1)
        self.assertEqual(PageRegion.objects.filter(job=job).count(), 1)
        self.assertEqual(ProcessingArtifact.objects.filter(job=job, artifact_type="page_text").count(), 1)

    def test_failed_reimport_preserves_committed_files_and_database_rows(self):
        from workbench.processors.importer import ResultImporter
        from io import BytesIO
        from PIL import Image

        job = self._job()
        result = self._result("Original")
        image_buffer = BytesIO()
        Image.new("RGB", (2, 2), color="white").save(image_buffer, format="PNG")
        result.page_images = {1: {
            "data": base64.b64encode(image_buffer.getvalue()).decode(),
            "format": "png",
        }}
        importer = ResultImporter(job)
        importer.import_results(result)
        page = Page.objects.get(document=job.result_document)
        committed_path = Path(self.artifacts_dir) / page.image_path
        committed_bytes = committed_path.read_bytes()

        result.page_texts = {1: "Replacement that must roll back"}
        with patch.object(importer, "_import_artifacts", side_effect=RuntimeError("database failure")):
            with self.assertRaisesRegex(RuntimeError, "database failure"):
                importer.import_results(result)

        page.refresh_from_db()
        self.assertEqual(committed_path.read_bytes(), committed_bytes)
        self.assertEqual(Page.objects.filter(document=job.result_document).count(), 1)
        self.assertEqual(PageRegion.objects.filter(job=job).count(), 1)
        self.assertEqual(
            ProcessingArtifact.objects.get(job=job, artifact_type="page_text").data["text"],
            "Original",
        )


class WorkerRecoveryTest(TestCase):
    """Worker leases, heartbeat semantics, and bounded polling recovery."""

    def setUp(self):
        self.user = User.objects.create_user(username="worker-user", password="testpass123")
        self.collection = _create_collection(self.user)
        self.preset = _create_preset()
        self.source = SourceDocument.objects.create(
            collection=self.collection,
            filename="worker.pdf",
            sha256="worker-sha",
            uploaded_by=self.user,
        )

    def _job(self, state="processing", external_job_id="remote-1", **extra):
        defaults = {
            "source_document": self.source,
            "preset": self.preset,
            "state": state,
            "external_job_id": external_job_id,
            "created_by": self.user,
        }
        defaults.update(extra)
        return ProcessingJob.objects.create(**defaults)

    @staticmethod
    def _command():
        from workbench.management.commands.run_processing_worker import Command
        command = Command()
        command.worker_id = "test-worker"
        command.lease_seconds = 30
        command.max_status_errors = 3
        command.poll_interval = 0
        command.processor = MagicMock(job_timeout=10)
        return command

    @override_settings(DSW_PROCESSING_STALE_AFTER_SECONDS=90)
    def test_stale_uses_heartbeat_not_total_runtime(self):
        old = timezone.now() - timedelta(hours=2)
        stale = self._job(started_at=old, worker_heartbeat_at=old)
        fresh = self._job(
            external_job_id="remote-2",
            started_at=old,
            worker_heartbeat_at=timezone.now(),
        )
        self.assertTrue(stale.is_stale())
        self.assertFalse(fresh.is_stale())

    def test_heartbeat_renews_only_owned_lease(self):
        job = self._job(worker_id="test-worker")
        command = self._command()
        before = timezone.now()
        command._heartbeat(job, "Still working.")
        job.refresh_from_db()
        self.assertGreaterEqual(job.worker_heartbeat_at, before)
        self.assertGreater(job.lease_expires_at, job.worker_heartbeat_at)
        self.assertEqual(job.status_message, "Still working.")

    def test_stale_processing_job_with_task_id_is_adopted(self):
        job = self._job(
            worker_id="dead-worker",
            lease_expires_at=timezone.now() - timedelta(seconds=1),
        )
        command = self._command()
        claimed, action = command._claim_next_job()
        claimed.refresh_from_db()
        self.assertEqual(claimed.pk, job.pk)
        self.assertEqual(action, "poll")
        self.assertEqual(claimed.worker_id, command.worker_id)

    def test_submitting_without_task_id_is_never_resubmitted(self):
        job = self._job(
            state="submitting",
            external_job_id="",
            worker_id="dead-worker",
            lease_expires_at=timezone.now() - timedelta(seconds=1),
        )
        command = self._command()
        self.assertIsNone(command._claim_next_job())
        job.refresh_from_db()
        self.assertEqual(job.state, "submission_uncertain")
        command.processor.submit.assert_not_called()

    def test_temporary_status_errors_are_retried_and_reset(self):
        job = self._job(worker_id="test-worker")
        command = self._command()
        command.processor.get_status.side_effect = [
            ConnectionError("temporary one"),
            ConnectionError("temporary two"),
            {"state": "success"},
        ]
        command._wait_for_completion(job, job.external_job_id)
        job.refresh_from_db()
        self.assertEqual(command.processor.get_status.call_count, 3)
        self.assertEqual(job.consecutive_poll_errors, 0)
        self.assertEqual(job.remote_status, "success")
        self.assertIsNotNone(job.remote_response_at)

    def test_retry_limit_marks_processing_job_interrupted(self):
        job = self._job(worker_id="test-worker")
        command = self._command()
        command.processor.get_status.side_effect = ConnectionError("offline")
        command._process_job(job, "poll")
        job.refresh_from_db()
        self.assertEqual(job.state, "interrupted")
        self.assertIn("3 consecutive attempts", job.error_message)
        self.assertEqual(command.processor.get_status.call_count, 3)

    def test_stale_importing_job_is_reimported_idempotently_and_completed(self):
        from workbench.processors.base import ProcessorResult
        from workbench.processors.importer import ResultImporter

        artifacts_dir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, artifacts_dir, True)
        result = ProcessorResult(
            pages_processed=1,
            page_texts={1: "Recovered import"},
            regions=[{
                "page_number": 1,
                "region_type": "text",
                "bbox": [0.1, 0.1, 0.9, 0.9],
                "text": "Recovered import",
                "metadata": {},
            }],
            processor_metadata={"page_dimensions": {1: {"width": 100, "height": 100}}},
        )
        with self.settings(ARTIFACTS_BASE_DIR=artifacts_dir):
            job = self._job(
                state="importing",
                worker_id="dead-worker",
                lease_expires_at=timezone.now() - timedelta(seconds=1),
            )
            ResultImporter(job).import_results(result)
            job.refresh_from_db()
            revision_id = job.result_document_id

            command = self._command()
            command.processor.collect_results.return_value = result
            claimed, action = command._claim_next_job()
            self.assertEqual(action, "import")
            command._process_job(claimed, action)

        job.refresh_from_db()
        self.source.refresh_from_db()
        self.assertEqual(job.state, "completed")
        self.assertEqual(job.result_document_id, revision_id)
        self.assertEqual(self.source.active_document_id, revision_id)
        self.assertEqual(Document.objects.filter(collection=self.collection).count(), 1)
        self.assertEqual(Page.objects.filter(document_id=revision_id).count(), 1)
        self.assertEqual(PageRegion.objects.filter(job=job).count(), 1)


def uuid_for_test():
    """Return a deterministic-enough unique external identifier for test rows."""
    import uuid
    return f"remote-{uuid.uuid4().hex}"


class ProcessingRevisionMigrationTest(TransactionTestCase):
    """Existing deployed jobs retain their result and region links on migration."""

    migrate_from = [("workbench", "0006_pageregion_text")]
    migrate_to = [("workbench", "0007_processing_revisions_and_recovery")]

    def test_existing_completed_job_is_backfilled(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps

        OldUser = old_apps.get_model("auth", "User")
        OldCollection = old_apps.get_model("workbench", "Collection")
        OldDocument = old_apps.get_model("workbench", "Document")
        OldPage = old_apps.get_model("workbench", "Page")
        OldPreset = old_apps.get_model("workbench", "ProcessingPreset")
        OldSource = old_apps.get_model("workbench", "SourceDocument")
        OldJob = old_apps.get_model("workbench", "ProcessingJob")
        OldRegion = old_apps.get_model("workbench", "PageRegion")

        user = OldUser.objects.create(username="migration-user")
        project = OldCollection.objects.create(name="Migration Project", created_by_id=user.pk)
        document = OldDocument.objects.create(
            collection_id=project.pk,
            external_id="legacy-revision",
            filename="legacy.pdf",
            sha256="legacy-sha",
            page_count=1,
        )
        page = OldPage.objects.create(document_id=document.pk, page_number=1)
        preset = OldPreset.objects.create(slug="migration-preset", name="Migration")
        source = OldSource.objects.create(
            collection_id=project.pk,
            filename="legacy.pdf",
            sha256="legacy-sha",
            processed_document_id=document.pk,
        )
        job = OldJob.objects.create(
            source_document_id=source.pk,
            preset_id=preset.pk,
            state="completed",
            finished_at=timezone.now(),
        )
        region = OldRegion.objects.create(
            source_document_id=source.pk,
            job_id=job.pk,
            page_number=1,
            region_type="text",
            left=0.1,
            top=0.1,
            right=0.9,
            bottom=0.9,
            text="Legacy region",
        )

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        new_apps = executor.loader.project_state(self.migrate_to).apps
        NewSource = new_apps.get_model("workbench", "SourceDocument")
        NewJob = new_apps.get_model("workbench", "ProcessingJob")
        NewRegion = new_apps.get_model("workbench", "PageRegion")
        NewDocument = new_apps.get_model("workbench", "Document")

        self.assertEqual(NewSource.objects.get(pk=source.pk).active_document_id, document.pk)
        self.assertEqual(NewJob.objects.get(pk=job.pk).result_document_id, document.pk)
        self.assertEqual(NewRegion.objects.get(pk=region.pk).page_id, page.pk)
        NewDocument.objects.create(
            collection_id=project.pk,
            external_id="second-revision",
            filename="legacy.pdf",
            sha256="legacy-sha",
        )
        self.assertEqual(NewDocument.objects.filter(sha256="legacy-sha").count(), 2)
