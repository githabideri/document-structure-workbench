"""Backend tests for the workspace stabilization pass.

Covers progressive image derivatives/URLs, MIME + authorization, deterministic
derivative caching, provenance current-source resolution, candidate reuse after
revert, recognition capability independence, and the page-level OCR/inspector
endpoints.
"""
import shutil
import tempfile
from pathlib import Path

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from workbench.models import (
    Collection, Document, OcrRequest, Page, PageRegion, ProcessingArtifact,
    ProcessingJob, ProcessingPreset, ProjectMembership, RegionCorrection, SourceDocument,
)
from workbench.provenance import build_region_versions
from workbench.services import HtrService, OcrService


def _world():
    owner = User.objects.create_user("owner", password="x")
    project = Collection.objects.create(name="Prj")
    ProjectMembership.objects.create(project=project, user=owner, role="owner")
    source = SourceDocument.objects.create(collection=project, filename="a.pdf", uploaded_by=owner)
    preset = ProcessingPreset.objects.create(slug="ws-preset", name="t")
    job = ProcessingJob.objects.create(
        source_document=source, preset=preset, state="completed",
        processor="docling", created_by=owner,
    )
    doc = Document.objects.create(collection=project, external_id="r1", filename="a.pdf", sha256="s", page_count=1)
    job.result_document = doc
    job.save(update_fields=["result_document"])
    source.active_document = doc
    source.save(update_fields=["active_document"])
    page = Page.objects.create(document=doc, page_number=1, image_path="pages/a.png", width=2000, height=2600)
    region = PageRegion.objects.create(
        source_document=source, job=job, page=page, page_number=1,
        region_type="text", left=.1, top=.1, right=.7, bottom=.5, text="Machine",
    )
    return {"owner": owner, "project": project, "source": source, "job": job,
            "document": doc, "page": page, "region": region}


def _make_image(tmp):
    path = Path(tmp) / "pages" / "a.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (2000, 2600), (200, 210, 220)).save(path, "PNG")
    return path


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class ImagingDerivativeTests(TestCase):
    def setUp(self):
        self.w = _world()
        self.tmp = tempfile.mkdtemp()
        _make_image(self.tmp)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._ov = override_settings(ARTIFACTS_BASE_DIR=self.tmp)
        self._ov.enable()
        self.addCleanup(self._ov.disable)
        self.client.force_login(self.w["owner"])

    def test_thumbnail_and_preview_endpoints_serve_with_mime_and_auth(self):
        thumb = self.client.get(reverse("page_image_derivative", args=[self.w["page"].pk, "thumbnail"]))
        self.assertEqual(thumb.status_code, 200)
        self.assertEqual(thumb["Content-Type"], "image/png")
        preview = self.client.get(reverse("page_image_derivative", args=[self.w["page"].pk, "preview"]))
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview["Content-Type"], "image/png")

    def test_derivative_is_deterministically_cached(self):
        from workbench.imaging import ensure_derivative
        p1 = ensure_derivative(self.w["page"], "thumbnail")
        self.assertIsNotNone(p1)
        mtime1 = p1[0].stat().st_mtime_ns
        import time
        time.sleep(0.01)
        p2 = ensure_derivative(self.w["page"], "thumbnail")
        self.assertEqual(mtime1, p2[0].stat().st_mtime_ns)  # not regenerated

    def test_derivative_forbidden_to_unrelated_user(self):
        other = User.objects.create_user("other", password="x")
        self.client.force_login(other)
        response = self.client.get(reverse("page_image_derivative", args=[self.w["page"].pk, "thumbnail"]))
        self.assertRedirects(response, reverse("document_list"))
        response = self.client.get(reverse("page_image", args=[self.w["page"].pk]))
        self.assertRedirects(response, reverse("document_list"))

    def test_page_workspace_data_returns_image_levels(self):
        response = self.client.get(reverse("page_workspace_data", args=[self.w["page"].pk]))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["has_image"])
        levels = body["image_levels"]
        self.assertIsNotNone(levels)
        # Thumbnail, preview and full levels, in ascending size, same page.
        widths = [l["width"] for l in levels]
        self.assertGreater(widths[-1], widths[0])
        self.assertTrue(all(l["url"] for l in levels))
        # The full level references the archival image.
        self.assertIn(reverse("page_image", args=[self.w["page"].pk]), {l["url"] for l in levels})


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class ProvenanceCurrentSourceTests(TestCase):
    def setUp(self):
        self.w = _world()
        self.client.force_login(self.w["owner"])

    def _current(self, region):
        return build_region_versions(region)["current"]

    def test_imported_is_current_with_no_correction(self):
        cur = self._current(self.w["region"])
        self.assertEqual(cur["source"], "imported")
        self.assertEqual(cur["source_label"], "Imported")

    def test_manual_correction_is_current_and_labelled_manual(self):
        region = self.w["region"]
        RegionCorrection.objects.create(
            region=region, document=self.w["document"], created_by=self.w["owner"],
            operation="text", before={"text": "Machine"}, after={"text": "Human edit"},
        )
        cur = self._current(region)
        self.assertEqual(cur["source"], "manual")
        self.assertIn("Manual", cur["source_label"])
        self.assertIn("owner", cur["source_label"])

    def test_accepted_htr_is_current_and_labelled_htr(self):
        region = self.w["region"]
        item = OcrRequest.objects.create(
            source_document=self.w["source"], document=self.w["document"], page=self.w["page"], region=region,
            target="region", provider="htr", prompt="htr", state="completed",
            candidate_text="HTR transcript",
            metadata={"pipeline_id": "htrflow-trocr-kurrent"},
        )
        HtrService.accept(item=item, user=self.w["owner"])
        cur = self._current(region)
        self.assertEqual(cur["source"], "htr")
        self.assertIn("HTR", cur["source_label"])
        self.assertIn("htrflow-trocr-kurrent", cur["source_label"])

    def test_accepted_vision_is_current_and_labelled_vision(self):
        region = self.w["region"]
        item = OcrService.create(page=self.w["page"], region=region, provider="qwen", model="qwen-vl", user=self.w["owner"])
        item.state = "completed"
        item.candidate_text = "Vision transcript"
        item.save(update_fields=["state", "candidate_text"])
        OcrService.accept(item=item, user=self.w["owner"])
        cur = self._current(region)
        self.assertEqual(cur["source"], "vision")
        self.assertIn("Vision", cur["source_label"])
        self.assertIn("qwen-vl", cur["source_label"])

    def test_only_one_entry_is_current(self):
        region = self.w["region"]
        RegionCorrection.objects.create(
            region=region, document=self.w["document"], created_by=self.w["owner"],
            operation="text", before={"text": "Machine"}, after={"text": "Edit"},
        )
        versions = build_region_versions(region)
        current_marked = [e for e in versions["entries"] if e["accepted"]]
        self.assertEqual(len(current_marked), 1)

    def test_reverted_accepted_candidate_is_not_current_and_can_be_reaught(self):
        region = self.w["region"]
        item = OcrService.create(page=self.w["page"], region=region, provider="qwen", model="m", user=self.w["owner"])
        item.state = "completed"
        item.candidate_text = "Candidate A"
        item.save(update_fields=["state", "candidate_text"])
        correction = OcrService.accept(item=item, user=self.w["owner"])
        self.assertEqual(build_region_versions(region)["current"]["source"], "vision")
        # Revert the accepted correction.
        from workbench.services import CorrectionService
        CorrectionService.revert(correction=correction, user=self.w["owner"])
        versions = build_region_versions(region)
        self.assertEqual(versions["current"]["source"], "imported")
        ocr_entry = next(e for e in versions["entries"] if e["kind"] == "vision")
        self.assertFalse(ocr_entry["accepted"])  # no longer current
        # Re-accepting creates a FRESH correction (not the reverted one recycled).
        fresh = OcrService.accept(item=item, user=self.w["owner"])
        self.assertNotEqual(fresh.pk, correction.pk)
        self.assertEqual(fresh.status, "active")


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class CapabilityIndependenceTests(TestCase):
    def setUp(self):
        self.w = _world()
        self.client.force_login(self.w["owner"])

    def test_vision_enabled_follows_configuration(self):
        from workbench import recognition
        with override_settings(DSW_CHAT_BASE_URL="", DSW_OCR_BASE_URL=""):
            self.assertFalse(recognition.vision_enabled())
        with override_settings(DSW_CHAT_BASE_URL="http://x.test/v1"):
            self.assertTrue(recognition.vision_enabled())


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class PageInspectorTests(TestCase):
    def setUp(self):
        self.w = _world()
        self.tmp = tempfile.mkdtemp()
        _make_image(self.tmp)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._ov = override_settings(ARTIFACTS_BASE_DIR=self.tmp)
        self._ov.enable()
        self.addCleanup(self._ov.disable)
        ProcessingArtifact.objects.create(
            job=self.w["job"], artifact_type="page_text", page_number=1,
            data={"text": "Complete page transcription."},
        )
        self.client.force_login(self.w["owner"])

    def test_page_inspector_renders_full_page_text(self):
        response = self.client.get(reverse("page_inspector", args=[self.w["page"].pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Complete page transcription.")
        self.assertContains(response, "Full-page Vision OCR")

    def test_create_page_ocr_request_htmx_returns_partial_not_redirect(self):
        headers = {"HTTP_HX_REQUEST": "true"}
        response = self.client.post(
            reverse("create_page_ocr_request", args=[self.w["page"].pk]),
            data={"provider": "qwen"}, **headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["HX-Retarget"], "#inspector-pane-page")
        self.assertTrue(OcrRequest.objects.filter(page=self.w["page"], target="page").exists())
