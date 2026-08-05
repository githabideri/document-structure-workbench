import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from PIL import Image

from workbench.models import Collection, Document, OcrRequest, Page, PageRegion, ProcessingJob, ProcessingPreset, SourceDocument
from workbench.processors.base import ProcessorResult
from workbench.processors.ingestion_ocr import apply_page_ocr
from workbench.processors.vision_ocr import VisionOcrClient, make_crop


class VisionOcrTests(TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.user = get_user_model().objects.create_user("ocr-user", password="x")
        self.project = Collection.objects.create(name="OCR project", created_by=self.user)
        self.source = SourceDocument.objects.create(collection=self.project, filename="scan.pdf", uploaded_by=self.user)
        self.preset = ProcessingPreset.objects.create(slug="ocr-test", name="OCR test")
        self.job = ProcessingJob.objects.create(source_document=self.source, preset=self.preset, created_by=self.user)
        self.document = Document.objects.create(collection=self.project, external_id="scan", filename="scan.pdf", sha256="a" * 64)
        self.job.result_document = self.document
        self.job.save(update_fields=["result_document"])
        self.page = Page.objects.create(document=self.document, page_number=1, image_path="pages/test.png", width=100, height=80)
        self.region = PageRegion.objects.create(source_document=self.source, job=self.job, page=self.page, page_number=1, region_type="text", left=.1, top=.1, right=.9, bottom=.9, text="old")
        image = Image.new("RGB", (100, 80), "white")
        (Path(self.root) / "pages").mkdir()
        image.save(Path(self.root) / "pages" / "test.png")

    @override_settings(ARTIFACTS_BASE_DIR="")
    def test_region_crop_is_bounded_and_hashed_by_client(self):
        with override_settings(ARTIFACTS_BASE_DIR=self.root):
            payload, info = make_crop(self.page, self.region)
        self.assertEqual(info["width"], 80)
        self.assertEqual(info["height"], 64)
        self.assertTrue(payload.startswith(b"\x89PNG"))

    @override_settings(
        DSW_OCR_BASE_URL="http://ocr.test/v1",
        DSW_OCR_MODEL="paddleocr-vl",
        DSW_OCR_PROVIDER="openai-compatible",
    )
    def test_client_sends_openai_vision_message_and_extracts_text(self):
        response = type("Response", (), {"raise_for_status": lambda self: None, "json": lambda self: {"choices": [{"message": {"content": "exact text"}}]}})()
        with patch("workbench.processors.vision_ocr.requests.post", return_value=response) as post:
            text, raw = VisionOcrClient().transcribe(b"png", "transcribe")
        self.assertEqual(text, "exact text")
        request = post.call_args.kwargs["json"]
        self.assertEqual(request["model"], "paddleocr-vl")
        self.assertEqual(request["messages"][0]["content"][0]["text"], "transcribe")
        self.assertTrue(request["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))

    @override_settings(
        DSW_CHAT_BASE_URL="http://qwen.test/v1",
        DSW_CHAT_API_KEY="chat-secret",
        DSW_CHAT_MODEL="Qwen3.6-35B-A3B",
        DSW_OCR_BASE_URL="http://paddle.test/v1",
        DSW_OCR_MODEL="PaddleOCR-VL-0.9B",
    )
    def test_qwen_provider_uses_chat_endpoint_configuration(self):
        response = type("Response", (), {"raise_for_status": lambda self: None, "json": lambda self: {"choices": [{"message": {"content": "qwen text"}}]}})()
        with patch("workbench.processors.vision_ocr.requests.post", return_value=response) as post:
            text, _ = VisionOcrClient(provider="qwen").transcribe(b"png", "read this")
        self.assertEqual(text, "qwen text")
        self.assertEqual(post.call_args.args[0], "http://qwen.test/v1/chat/completions")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer chat-secret")
        self.assertEqual(post.call_args.kwargs["json"]["model"], "Qwen3.6-35B-A3B")

    def test_unknown_provider_is_rejected(self):
        with self.assertRaisesMessage(Exception, "Unsupported visual OCR provider: made-up"):
            VisionOcrClient(provider="made-up")

    def test_request_keeps_candidate_separate_from_region_text(self):
        item = OcrRequest.objects.create(source_document=self.source, document=self.document, page=self.page, region=self.region, prompt="x", candidate_text="new", state="completed")
        self.assertEqual(self.region.text, "old")
        self.assertEqual(item.candidate_text, "new")

    def test_ingestion_ocr_replaces_page_text_and_matches_regions(self):
        result = ProcessorResult(
            pages_processed=1,
            page_images={1: {"data": __import__("base64").b64encode(b"png").decode(), "format": "png"}},
            page_texts={1: "Docling text"},
            regions=[{"page_number": 1, "bbox": [0, 0, 100, 40], "text": "Docling region", "metadata": {}}],
            processor_metadata={"page_dimensions": {1: {"width": 100, "height": 100}}},
        )

        class FakeClient:
            provider = "paddleocr-vl"
            model = "PaddleOCR-VL-0.9B"

            def transcribe(self, image, prompt):
                return "Paddle page text", {"result": {"layoutParsingResults": [{
                    "markdown": {"text": "Paddle page text"},
                    "prunedResult": {"parsing_res_list": [
                        {"block_bbox": [0, 0, 100, 40], "block_content": "Paddle region"},
                    ]},
                }]}}

        apply_page_ocr(result, client=FakeClient())
        self.assertEqual(result.page_texts[1], "Paddle page text")
        self.assertEqual(result.regions[0]["text"], "Paddle region")
        self.assertEqual(result.ocr_pages[1]["provider"], "paddleocr-vl")

    def test_ingestion_ocr_materializes_regions_for_scanned_image(self):
        buffer = tempfile.SpooledTemporaryFile()
        Image.new("RGB", (200, 100), "white").save(buffer, format="PNG")
        buffer.seek(0)
        result = ProcessorResult(
            pages_processed=1,
            page_images={1: {"data": __import__("base64").b64encode(buffer.read()).decode(), "format": "png"}},
            page_texts={1: "Docling text"},
            processor_metadata={"page_dimensions": {1: {"width": 200, "height": 100}}},
        )

        class FakeClient:
            provider = "paddleocr-vl"
            model = "PaddleOCR-VL-0.9B"

            def transcribe(self, image, prompt):
                return "Scanned text", {"result": {"layoutParsingResults": [{
                    "markdown": {"text": "Scanned text"},
                    "prunedResult": {"parsing_res_list": [
                        {"block_bbox": [20, 10, 180, 40], "block_content": "Scanned text"},
                    ]},
                }]}}

        apply_page_ocr(result, client=FakeClient())
        self.assertEqual(len(result.regions), 1)
        self.assertEqual(result.regions[0]["text"], "Scanned text")
        self.assertEqual(result.regions[0]["bbox"], [0.1, 0.1, 0.9, 0.4])
