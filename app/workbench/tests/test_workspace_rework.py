"""Backend coverage for the document workspace rework (acceptance criteria §19).

Covers: inspector partial authorization, manual correction attribution, revert
attribution (``reverted_by``), recognition-model preference persistence/fallback,
permission enforcement on recognition actions, idempotent candidate acceptance,
immutability of raw machine text, and the new HTMX/JSON endpoints that keep the
viewer from being recreated during region-level work.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from workbench.models import (
    Collection, Document, OcrRequest, Page, PageRegion, ProcessingJob,
    ProcessingPreset, ProjectMembership, RegionCorrection, SourceDocument,
    UserPreferences,
)
from workbench.services import OcrService

User = get_user_model()


def _world():
    owner = User.objects.create_user("owner", password="x")
    viewer = User.objects.create_user("viewer", password="x")
    project = Collection.objects.create(name="WS project", created_by=owner)
    ProjectMembership.objects.create(project=project, user=owner, role="owner")
    ProjectMembership.objects.create(project=project, user=viewer, role="viewer")
    source = SourceDocument.objects.create(collection=project, filename="doc.pdf", uploaded_by=owner)
    preset = ProcessingPreset.objects.create(slug="ws-preset", name="t")
    job = ProcessingJob.objects.create(source_document=source, preset=preset, created_by=owner, processor="docling")
    document = Document.objects.create(collection=project, external_id="doc.pdf", filename="doc.pdf")
    job.result_document = document
    job.save(update_fields=["result_document"])
    source.active_document = document
    source.save(update_fields=["active_document"])
    page = Page.objects.create(document=document, page_number=1)
    region = PageRegion.objects.create(
        source_document=source, job=job, page=page, page_number=1, region_type="text",
        left=0.1, top=0.1, right=0.9, bottom=0.5, text="Machine import",
    )
    return {"owner": owner, "viewer": viewer, "project": project, "source": source,
            "document": document, "page": page, "region": region}


HTMX_HEADERS = {"HTTP_HX_REQUEST": "true"}


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class WorkspaceInspectorTests(TestCase):
    def setUp(self):
        self.w = _world()
        self.client.force_login(self.w["owner"])

    def test_draw_fallback_requires_server_hook_and_query_flag(self):
        url = reverse("document_detail", args=[self.w["document"].id])
        off = self.client.get(url + "?test_drawn=1")
        self.assertContains(off, '"e2eTestHooksEnabled": false')
        self.assertContains(off, '"testDrawnFallback": false')

        with override_settings(DSW_E2E_TEST_HOOKS_ENABLED=True):
            no_query = self.client.get(url)
            self.assertContains(no_query, '"e2eTestHooksEnabled": true')
            self.assertContains(no_query, '"testDrawnFallback": false')
            with_query = self.client.get(url + "?test_drawn=1")
            self.assertContains(with_query, '"e2eTestHooksEnabled": true')
            self.assertContains(with_query, '"testDrawnFallback": true')

    def test_inspector_partial_authorized_for_member(self):
        url = reverse("region_inspector", args=[self.w["region"].pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Transcription")
        self.assertContains(response, f'data-region-id="{self.w["region"].pk}"')

    def test_inspector_partial_forbidden_to_unrelated_user(self):
        other = User.objects.create_user("other", password="x")
        self.client.force_login(other)
        response = self.client.get(reverse("region_inspector", args=[self.w["region"].pk]))
        self.assertIn(response.status_code, (302, 403))

    def test_inspector_shows_model_aware_recognition_controls(self):
        with override_settings(DSW_HTR_ENABLED=True, DSW_CHAT_BASE_URL="http://vision.test/v1"):
            response = self.client.get(reverse("region_inspector", args=[self.w["region"].pk]))
        self.assertContains(response, "Run HTR")
        self.assertContains(response, 'data-selection-field="pipeline_id"')
        self.assertContains(response, "Run Vision")
        self.assertContains(response, 'data-selection-field="provider"')
        # The button label exposes the currently-selected model/pipeline.
        self.assertContains(response, "TrOCR")  # default HTR pipeline label
        self.assertContains(response, "Qwen")   # default vision provider label

    def test_vision_independent_of_htr(self):
        # HTR disabled + Vision configured: Vision must still render.
        with override_settings(DSW_HTR_ENABLED=False, DSW_CHAT_BASE_URL="http://vision.test/v1"):
            response = self.client.get(reverse("region_inspector", args=[self.w["region"].pk]))
        self.assertContains(response, "Run Vision")
        self.assertNotContains(response, "Run HTR")
        # HTR enabled but no Vision endpoint configured: only HTR renders.
        with override_settings(DSW_HTR_ENABLED=True, DSW_CHAT_BASE_URL="", DSW_OCR_BASE_URL=""):
            response = self.client.get(reverse("region_inspector", args=[self.w["region"].pk]))
        self.assertContains(response, "Run HTR")
        self.assertNotContains(response, "Run Vision")

    def test_versions_fragment_reports_pending_state(self):
        region = self.w["region"]
        OcrService.create(page=self.w["page"], region=region, provider="qwen", model="m", user=self.w["owner"])
        response = self.client.get(reverse("region_versions_fragment", args=[region.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-pending="true"')
        self.assertContains(response, "queued")


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class WorkspaceCorrectionTests(TestCase):
    def setUp(self):
        self.w = _world()
        self.client.force_login(self.w["owner"])

    def test_manual_correction_json_creates_correction_with_created_by(self):
        region = self.w["region"]
        response = self.client.post(
            reverse("correct_region_text", args=[region.pk]),
            data='{"replacement_text":"Human fix","expected_current_text":"Machine import"}',
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["effective_text"], "Human fix")
        self.assertIn("versions_html", body)
        correction = region.corrections.get()
        self.assertEqual(correction.operation, "text")
        self.assertEqual(correction.created_by, self.w["owner"])
        region.refresh_from_db()
        # Raw machine text is never mutated.
        self.assertEqual(region.text, "Machine import")

    def test_manual_correction_conflict_returns_409(self):
        region = self.w["region"]
        response = self.client.post(
            reverse("correct_region_text", args=[region.pk]),
            data='{"replacement_text":"x","expected_current_text":"stale"}',
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "region_state_conflict")

    def test_revert_records_reverted_by(self):
        region = self.w["region"]
        correction = RegionCorrection.objects.create(
            region=region, document=self.w["document"], created_by=self.w["owner"],
            operation="text", before={"text": "Machine import"}, after={"text": "Tweaked"},
        )
        response = self.client.post(reverse("revert_region_correction", args=[correction.pk]), **HTMX_HEADERS)
        self.assertEqual(response.status_code, 200)
        correction.refresh_from_db()
        self.assertEqual(correction.status, "reverted")
        self.assertEqual(correction.reverted_by, self.w["owner"])
        self.assertIsNotNone(correction.reverted_at)

    def test_viewer_cannot_save_correction(self):
        self.client.force_login(self.w["viewer"])
        region = self.w["region"]
        response = self.client.post(
            reverse("correct_region_text", args=[region.pk]),
            data='{"replacement_text":"hack","expected_current_text":"Machine import"}',
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )
        # Permission denied surfaces as a conflict/invalid error, never a save.
        self.assertNotEqual(response.status_code, 200)
        self.assertFalse(region.corrections.exists())


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class WorkspaceRecognitionTests(TestCase):
    def setUp(self):
        self.w = _world()
        self.client.force_login(self.w["owner"])

    def test_vision_run_remembers_last_provider_and_returns_versions(self):
        region = self.w["region"]
        response = self.client.post(
            reverse("create_ocr_request", args=[region.pk]),
            data={"provider": "paddleocr-vl"},
            **HTMX_HEADERS,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="inspector-versions"')
        prefs = UserPreferences.get_or_create_for_user(self.w["owner"])
        prefs.refresh_from_db()
        self.assertEqual(prefs.last_vision_provider, "paddleocr-vl")
        self.assertTrue(region.ocr_requests.filter(provider="paddleocr-vl").exists())

    @override_settings(DSW_HTR_ENABLED=True)
    def test_htr_non_default_pipeline_is_stored_on_request(self):
        region = self.w["region"]
        response = self.client.post(
            reverse("create_region_htr", args=[region.pk]),
            data={"pipeline_id": "htrflow-trocr-prototype"},
            **HTMX_HEADERS,
        )
        self.assertEqual(response.status_code, 200)
        request = region.ocr_requests.get(provider="htr")
        self.assertEqual(request.metadata["pipeline_id"], "htrflow-trocr-prototype")
        prefs = UserPreferences.get_or_create_for_user(self.w["owner"])
        prefs.refresh_from_db()
        self.assertEqual(prefs.last_htr_pipeline, "htrflow-trocr-prototype")

    def test_viewer_cannot_run_vision(self):
        self.client.force_login(self.w["viewer"])
        response = self.client.post(reverse("create_ocr_request", args=[self.w["region"].pk]), data={"provider": "qwen"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.w["region"].ocr_requests.exists())

    def test_vision_accept_is_idempotent_and_keeps_raw_text(self):
        region = self.w["region"]
        item = OcrService.create(page=self.w["page"], region=region, provider="qwen", model="m", user=self.w["owner"])
        item.state = "completed"
        item.candidate_text = "Vision candidate"
        item.save(update_fields=["state", "candidate_text"])
        first = self.client.post(reverse("accept_ocr_request", args=[item.pk]), **HTMX_HEADERS)
        self.assertEqual(first.status_code, 200)
        region.refresh_from_db()
        self.assertEqual(region.effective_text, "Vision candidate")
        self.assertEqual(region.text, "Machine import")  # raw unchanged
        item.refresh_from_db()
        first_correction = item.accepted_correction
        self.assertIsNotNone(first_correction)
        # Second acceptance is a no-op (idempotent): same correction, no duplicate.
        second = self.client.post(reverse("accept_ocr_request", args=[item.pk]), **HTMX_HEADERS)
        self.assertEqual(second.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.accepted_correction_id, first_correction.pk)
        self.assertEqual(region.corrections.filter(operation="text").count(), 1)

    def test_accept_rejects_stale_expected_current_text(self):
        region = self.w["region"]
        item = OcrService.create(page=self.w["page"], region=region, provider="qwen", model="m", user=self.w["owner"])
        item.state = "completed"
        item.candidate_text = "Accepted text"
        item.save(update_fields=["state", "candidate_text"])
        response = self.client.post(
            reverse("accept_ocr_request", args=[item.pk]),
            data={"expected_current_text": "stale"}, **HTMX_HEADERS,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "changed before this correction")
        self.assertFalse(region.corrections.exists())

    def test_htmx_accept_returns_inspector_partial_not_redirect(self):
        region = self.w["region"]
        item = OcrService.create(page=self.w["page"], region=region, provider="qwen", model="m", user=self.w["owner"])
        item.state = "completed"
        item.candidate_text = "Accepted text"
        item.save(update_fields=["state", "candidate_text"])
        response = self.client.post(reverse("accept_ocr_request", args=[item.pk]), **HTMX_HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Location", response)  # no full-page redirect on HTMX
        self.assertContains(response, "Accepted text")


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class WorkspacePageDataTests(TestCase):
    def setUp(self):
        self.w = _world()
        self.client.force_login(self.w["owner"])

    def test_initial_page_inspector_renders_without_page_image(self):
        response = self.client.get(reverse("document_detail", args=[self.w["source"].pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="inspector-page-text"')

    def test_page_workspace_data_returns_region_adapter(self):
        response = self.client.get(reverse("page_workspace_data", args=[self.w["page"].pk]))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["page_number"], 1)
        self.assertTrue(data["regions"])
        region = data["regions"][0]
        self.assertEqual(region["id"], self.w["region"].pk)
        self.assertEqual(region["bbox"], {"left": 0.1, "top": 0.1, "width": 0.8, "height": 0.4})
        self.assertEqual(region["type"], "text")

    def test_page_workspace_data_forbidden_to_unrelated_user(self):
        other = User.objects.create_user("stranger", password="x")
        self.client.force_login(other)
        response = self.client.get(reverse("page_workspace_data", args=[self.w["page"].pk]))
        self.assertEqual(response.status_code, 403)


class RecognitionPreferenceFallbackTests(TestCase):
    def setUp(self):
        self.w = _world()

    def test_effective_selection_uses_default_without_preference(self):
        from workbench import recognition
        self.assertEqual(recognition.effective_htr_pipeline(self.w["owner"]), recognition.default_htr_pipeline())
        with override_settings(DSW_CHAT_BASE_URL="http://example.com/chat", DSW_OCR_PROVIDER="qwen"):
            self.assertEqual(recognition.effective_vision_provider(self.w["owner"]), "qwen")

    def test_stale_preference_falls_back_silently(self):
        from workbench import recognition
        prefs = UserPreferences.get_or_create_for_user(self.w["owner"])
        prefs.last_htr_pipeline = "removed-pipeline"
        prefs.last_vision_provider = "removed-provider"
        prefs.save(update_fields=["last_htr_pipeline", "last_vision_provider"])
        self.assertEqual(recognition.effective_htr_pipeline(self.w["owner"]), recognition.default_htr_pipeline())
        with override_settings(DSW_CHAT_BASE_URL="http://example.com/chat", DSW_OCR_PROVIDER="qwen"):
            self.assertEqual(recognition.effective_vision_provider(self.w["owner"]), "qwen")

    def test_remembered_preference_is_used(self):
        from workbench import recognition
        recognition.remember_htr_pipeline(self.w["owner"], "htrflow-trocr-prototype")
        with override_settings(DSW_OCR_BASE_URL="http://example.com/ocr", DSW_OCR_PROVIDER="paddleocr-vl"):
            recognition.remember_vision_provider(self.w["owner"], "paddleocr-vl")
            self.assertEqual(recognition.effective_vision_provider(self.w["owner"]), "paddleocr-vl")

    def test_non_runnable_provider_falls_back_and_is_not_offered(self):
        """A stored provider whose endpoint is missing must not be used/offered."""
        from workbench import recognition
        prefs = UserPreferences.get_or_create_for_user(self.w["owner"])
        prefs.last_vision_provider = "paddleocr-vl"
        prefs.save(update_fields=["last_vision_provider"])
        # Only qwen's endpoint is configured → stored paddleocr-vl is not runnable.
        with override_settings(DSW_CHAT_BASE_URL="http://example.com/chat", DSW_OCR_BASE_URL="", DSW_OCR_PROVIDER="qwen"):
            models = recognition.runnable_vision_models()
            offered = {p for p, _, _ in models}
            self.assertIn("qwen", offered)
            self.assertNotIn("paddleocr-vl", offered)
            self.assertEqual(recognition.effective_vision_provider(self.w["owner"]), "qwen")
