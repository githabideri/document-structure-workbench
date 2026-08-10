"""Tests for the document-detail OCR history fragment + polling plumbing."""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from workbench.models import (
    Collection, Document, Page, PageRegion, ProcessingJob, ProcessingPreset,
    ProjectMembership, SourceDocument,
)
from workbench.services import OcrService

User = get_user_model()


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    "DSW_OCR_PROVIDER": "qwen",
})
class DocumentDetailOcrFragmentTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("viewer", password="pass")
        self.project = Collection.objects.create(name="Corpus", created_by=self.user)
        ProjectMembership.objects.create(project=self.project, user=self.user, role="editor")
        self.client.login(username="viewer", password="pass")
        self.source = SourceDocument.objects.create(collection=self.project, filename="doc.pdf", uploaded_by=self.user)
        self.revision = Document.objects.create(collection=self.project, external_id="doc.pdf", filename="doc.pdf")
        preset = ProcessingPreset.objects.create(slug="ocr-test-preset", name="t")
        self.job = ProcessingJob.objects.create(source_document=self.source, preset=preset, state="completed")
        self.job.result_document = self.revision
        self.job.save(update_fields=["result_document"])
        self.source.active_document = self.revision
        self.source.save(update_fields=["active_document"])
        self.page = Page.objects.create(document=self.revision, page_number=1)
        self.region = PageRegion.objects.create(
            page=self.page, source_document=self.source, job=self.job,
            page_number=self.page.page_number, region_type="text",
            left=0.1, top=0.1, right=0.9, bottom=0.5,
            text="Das Rundschreiben des Verbandes",
        )

    def _detail(self, **extra):
        params = {"revision": self.revision.pk, "page": self.page.page_number, "region": self.region.pk}
        params.update(extra)
        return self.client.get(reverse("document_detail", args=[self.source.pk]) + "?" + "&".join(f"{k}={v}" for k, v in params.items()))

    def test_detail_renders_workspace_and_region_inspector(self):
        # A pending candidate is present so the provenance rail executes.
        OcrService.create(page=self.page, region=self.region, provider="qwen", model="m", user=self.user)
        response = self._detail()
        self.assertEqual(response.status_code, 200)
        # Stable shell + independently reloadable inspector.
        self.assertContains(response, 'id="workspace-shell"')
        self.assertContains(response, 'id="region-inspector"')
        self.assertContains(response, f'data-region-id="{self.region.pk}"')
        self.assertContains(response, "Transcription")
        self.assertContains(response, "Versions / provenance")
        # Region overlay data is embedded for the OpenSeadragon viewer.
        self.assertContains(response, "page-regions-data")
        # The pending candidate is surfaced as a version rail entry (not a reload link).
        self.assertContains(response, "queued")
        # No legacy full-page region-selection links remain in normal work.
        self.assertNotContains(response, "Back to page")

    def test_fragment_endpoint_returns_candidate_history(self):
        OcrService.create(page=self.page, region=self.region, provider="qwen", model="m", user=self.user)
        url = reverse("ocr_history_fragment", args=[self.source.pk]) + f"?region={self.region.pk}"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Visual OCR candidates")
        self.assertContains(response, "data-ocr-pending")
        self.assertContains(response, "Waiting for the visual OCR worker.")

    def test_fragment_unlocks_when_no_candidate_pending(self):
        req = OcrService.create(page=self.page, region=self.region, provider="qwen", model="m", user=self.user)
        req.state = "completed"
        req.candidate_text = "Fertig"
        req.save(update_fields=["state", "candidate_text"])
        url = reverse("ocr_history_fragment", args=[self.source.pk]) + f"?region={self.region.pk}"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Fertig")
        self.assertContains(response, "data-ocr-pending=\"false\"")

    def test_create_region_ocr_request_queues_and_redirects(self):
        url = reverse("create_ocr_request", args=[self.region.pk])
        response = self.client.post(url, {"provider": "qwen"})
        self.assertEqual(response.status_code, 302)
        req = self.region.ocr_requests.first()
        self.assertEqual(req.state, "queued")
