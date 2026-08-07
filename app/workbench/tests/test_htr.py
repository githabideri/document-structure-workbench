"""Region-scoped HTR rerun: client, service, worker, and API endpoints.

These tests run entirely against the frozen fixture
(``app/workbench/tests/fixtures/htr/transcription-succeeded.json``) — no network
and no GPU. They prove the full queued/processing/completed lifecycle, the
candidate-separation invariant (HTR never mutates region text until accepted),
and acceptance via the normal ``RegionCorrection`` path.
"""
import json
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from PIL import Image

from workbench.models import (
    Collection, Document, OcrRequest, Page, PageRegion,
    ProcessingJob, ProcessingPreset, ProjectMembership, RegionCorrection, SourceDocument,
)
from workbench.processors.htr import (
    HTR_PROVIDER, HtrClient, HtrError, validate_result,
)

User = get_user_model()

FIXTURE = Path(__file__).parent / "fixtures" / "htr" / "transcription-succeeded.json"


def _fixture_result():
    return json.loads(FIXTURE.read_text())


# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------

def _make_world(root):
    user = User.objects.create_user("htr-user", password="x")
    project = Collection.objects.create(name="HTR project", created_by=user)
    source = SourceDocument.objects.create(
        collection=project, filename="scan.pdf", uploaded_by=user,
    )
    preset = ProcessingPreset.objects.create(slug="htr-test", name="HTR test")
    job = ProcessingJob.objects.create(
        source_document=source, preset=preset, created_by=user,
    )
    document = Document.objects.create(
        collection=project, external_id="scan", filename="scan.pdf", sha256="b" * 64,
    )
    job.result_document = document
    job.save(update_fields=["result_document"])
    page = Page.objects.create(
        document=document, page_number=1, image_path="pages/test.png", width=200, height=200,
    )
    region = PageRegion.objects.create(
        source_document=source, job=job, page=page, page_number=1,
        region_type="text", left=0.1, top=0.1, right=0.9, bottom=0.9, text="machine text",
    )
    Image.new("RGB", (200, 200), "white").save(Path(root) / "pages" / "test.png")
    ProjectMembership.objects.create(project=project, user=user, role="owner")
    return {"user": user, "project": project, "source": source, "job": job,
            "document": document, "page": page, "region": region, "root": root}


class HtrWorldMixin:
    def setUp(self):
        import tempfile
        self.root = tempfile.mkdtemp()
        (Path(self.root) / "pages").mkdir()
        for key, value in _make_world(self.root).items():
            setattr(self, key, value)


# ---------------------------------------------------------------------------
# Contract validation
# ---------------------------------------------------------------------------

class HtrContractTests(HtrWorldMixin, TestCase):
    def test_frozen_fixture_is_valid(self):
        result = _fixture_result()
        validate_result(result)  # raises HtrError if invalid

    def test_out_of_contract_result_is_rejected(self):
        with self.assertRaises(HtrError):
            validate_result({"id": "x", "status": "succeeded"})  # missing required fields


# ---------------------------------------------------------------------------
# Client — fixture mode
# ---------------------------------------------------------------------------

@override_settings(
    DSW_HTR_FIXTURE_MODE=True,
    DSW_HTR_FIXTURE_PATH=str(FIXTURE),
    DSW_HTR_FIXTURE_DELAY_SECONDS=0.0,
)
class HtrClientFixtureTests(HtrWorldMixin, TestCase):
    def test_submit_returns_remote_id(self):
        client = HtrClient()
        remote_id = client.submit_transcription(b"png", mode="region", label="r1")
        self.assertTrue(remote_id.startswith("fixture-"))

    def test_poll_returns_succeeded_with_lines(self):
        client = HtrClient()
        remote_id = client.submit_transcription(b"png")
        result = client.get_transcription(remote_id)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(result["regions"][0]["lines"]), 8)

    def test_unknown_remote_id_raises(self):
        with self.assertRaises(HtrError):
            HtrClient().get_transcription("fixture-nope")

    def test_pending_state_before_delay(self):
        with override_settings(DSW_HTR_FIXTURE_DELAY_SECONDS=10.0):
            client = HtrClient()
            remote_id = client.submit_transcription(b"png")
            result = client.get_transcription(remote_id)
            self.assertEqual(result["status"], "pending")

    def test_missing_config_raises(self):
        with override_settings(DSW_HTR_FIXTURE_MODE=False, DSW_HTR_BASE_URL=""):
            with self.assertRaises(HtrError):
                HtrClient()


# ---------------------------------------------------------------------------
# Service — create + accept (candidate separation invariant)
# ---------------------------------------------------------------------------

@override_settings(
    DSW_HTR_FIXTURE_MODE=True,
    DSW_HTR_FIXTURE_PATH=str(FIXTURE),
    DSW_HTR_FIXTURE_DELAY_SECONDS=0.0,
)
class HtrServiceTests(HtrWorldMixin, TestCase):
    def _completed_request(self):
        from workbench.services import HtrService
        item = HtrService.create(page=self.page, region=self.region, user=self.user)
        item.state = "completed"
        item.candidate_text = _fixture_result()["text"]
        item.raw_response = _fixture_result()
        item.metadata = {"pipeline_id": "htrflow-trocr-prototype", "crop": {}}
        item.save(update_fields=["state", "candidate_text", "raw_response", "metadata"])
        return item

    def test_create_marks_request_as_htr_region_scoped(self):
        from workbench.services import HtrService
        item = HtrService.create(page=self.page, region=self.region, user=self.user)
        self.assertEqual(item.provider, HTR_PROVIDER)
        self.assertEqual(item.target, "region")
        self.assertEqual(item.state, "queued")
        self.assertEqual(item.metadata["pipeline_id"], "htrflow-trocr-kurrent")

    def test_accept_creates_text_correction_and_links_it(self):
        item = self._completed_request()
        from workbench.services import HtrService
        HtrService.accept(item=item, user=self.user)
        item.refresh_from_db()
        self.region.refresh_from_db()
        # Invariant: HTR never mutated the immutable machine text directly.
        self.assertEqual(self.region.text, "machine text")
        # Effective text now reflects the accepted candidate correction.
        self.assertEqual(self.region.effective_text, _fixture_result()["text"])
        self.assertEqual(item.accepted_correction.operation, "text")
        self.assertEqual(item.accepted_correction.status, "active")

    def test_accept_is_idempotent(self):
        item = self._completed_request()
        from workbench.services import HtrService
        first = HtrService.accept(item=item, user=self.user)
        item.refresh_from_db()
        second = HtrService.accept(item=item, user=self.user)
        self.assertEqual(first.id, second.id)
        self.assertEqual(RegionCorrection.objects.filter(region=self.region).count(), 1)

    def test_accept_rejects_non_completed(self):
        from workbench.services import HtrService
        item = HtrService.create(page=self.page, region=self.region, user=self.user)
        with self.assertRaises(ValueError):
            HtrService.accept(item=item, user=self.user)


# ---------------------------------------------------------------------------
# Worker — drives a queued request to completion via the fixture
# ---------------------------------------------------------------------------

@override_settings(
    ARTIFACTS_BASE_DIR="", DSW_HTR_FIXTURE_MODE=True,
    DSW_HTR_FIXTURE_PATH=str(FIXTURE), DSW_HTR_FIXTURE_DELAY_SECONDS=0.0,
)
class HtrWorkerTests(HtrWorldMixin, TestCase):
    def test_worker_completes_htr_request_and_persists_crop(self):
        from django.core.management import call_command
        from workbench.services import HtrService
        with override_settings(ARTIFACTS_BASE_DIR=self.root):
            item = HtrService.create(page=self.page, region=self.region, user=self.user)
            call_command("run_ocr_worker", "--once")
        item.refresh_from_db()
        self.assertEqual(item.state, "completed")
        self.assertEqual(item.provider, HTR_PROVIDER)
        # Candidate text matches the fixture.
        self.assertEqual(item.candidate_text, _fixture_result()["text"])
        # Crop provenance stored in page-relative coordinates.
        crop = item.metadata["crop"]
        self.assertEqual(crop["page_bbox"], [0.1, 0.1, 0.9, 0.9])
        self.assertEqual(crop["actual_padded_bbox"], crop["page_bbox"])
        # Region text untouched (candidate separation).
        self.region.refresh_from_db()
        self.assertEqual(self.region.text, "machine text")


# ---------------------------------------------------------------------------
# API endpoints — session-auth JSON
# ---------------------------------------------------------------------------

@override_settings(
    DSW_HTR_FIXTURE_MODE=True,
    DSW_HTR_FIXTURE_PATH=str(FIXTURE),
    DSW_HTR_FIXTURE_DELAY_SECONDS=0.0,
)
class HtrApiTests(HtrWorldMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_post_creates_distinct_run(self):
        r = self.client.post(f"/api/regions/{self.region.id}/htr-runs/",
                             data=json.dumps({}), content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertEqual(body["provider"], HTR_PROVIDER)
        self.assertEqual(body["state"], "queued")
        self.assertEqual(body["pipeline_id"], "htrflow-trocr-kurrent")

    def test_post_then_get_lists_run_newest_first(self):
        self.client.post(f"/api/regions/{self.region.id}/htr-runs/",
                         data=json.dumps({}), content_type="application/json")
        self.client.post(f"/api/regions/{self.region.id}/htr-runs/",
                         data=json.dumps({}), content_type="application/json")
        r = self.client.get(f"/api/regions/{self.region.id}/htr-runs/")
        self.assertEqual(r.status_code, 200)
        runs = r.json()["runs"]
        self.assertEqual(len(runs), 2)
        self.assertGreaterEqual(runs[0]["id"], runs[1]["id"])

    def test_get_detail_of_completed_includes_lines(self):
        item = OcrRequest.objects.create(
            source_document=self.source, document=self.document, page=self.page,
            region=self.region, target="region", provider=HTR_PROVIDER, prompt="htr",
            state="completed", candidate_text=_fixture_result()["text"],
            raw_response=_fixture_result(),
            metadata={"pipeline_id": "p", "crop": {}, "schema_version": "htr-transcription-v1"},
        )
        r = self.client.get(f"/api/htr-runs/{item.id}/")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["state"], "completed")
        self.assertEqual(len(body["result"]["lines"]), 8)
        self.assertEqual(body["result"]["lines"][0]["order"], 1)

    def test_get_detail_rejects_non_htr_request(self):
        vision = OcrRequest.objects.create(
            source_document=self.source, document=self.document, page=self.page,
            region=self.region, target="region", provider="qwen", prompt="x",
        )
        r = self.client.get(f"/api/htr-runs/{vision.id}/")
        self.assertEqual(r.status_code, 404)

    def test_accept_creates_correction_and_marks_accepted(self):
        item = OcrRequest.objects.create(
            source_document=self.source, document=self.document, page=self.page,
            region=self.region, target="region", provider=HTR_PROVIDER, prompt="htr",
            state="completed", candidate_text="HTR accepted text",
            raw_response={}, metadata={"pipeline_id": "p", "crop": {}},
        )
        r = self.client.post(f"/api/htr-runs/{item.id}/accept/",
                             data=json.dumps({}), content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        item.refresh_from_db()
        self.region.refresh_from_db()
        self.assertEqual(self.region.effective_text, "HTR accepted text")
        self.assertIsNotNone(item.accepted_correction_id)

    def test_accept_only_completed(self):
        item = OcrRequest.objects.create(
            source_document=self.source, document=self.document, page=self.page,
            region=self.region, target="region", provider=HTR_PROVIDER, prompt="htr",
            state="queued",
        )
        r = self.client.post(f"/api/htr-runs/{item.id}/accept/",
                             data=json.dumps({}), content_type="application/json")
        self.assertEqual(r.status_code, 409)

    def test_post_returns_409_when_disabled(self):
        with override_settings(DSW_HTR_FIXTURE_MODE=False, DSW_HTR_ENABLED=False):
            r = self.client.post(f"/api/regions/{self.region.id}/htr-runs/",
                                 data=json.dumps({}), content_type="application/json")
            self.assertEqual(r.status_code, 409)
            self.assertEqual(r.json()["error"]["code"], "htr_disabled")
