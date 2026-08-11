"""Backend tests for the final stabilization pass.

Covers logical-vs-raster dimension separation, pyramid level validation/fallback,
atomic derivative regeneration, candidate supersession + revert/re-accept with
permanent source links, re-apply ("Use transcription") endpoints, and the page
inspector polling URL.
"""
import shutil
import tempfile
from pathlib import Path

from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from PIL import Image

from workbench.models import (
    Collection, Document, OcrRequest, Page, PageRegion, ProcessingArtifact,
    ProcessingJob, ProcessingPreset, ProjectMembership, RegionCorrection, SourceDocument,
)
from workbench.provenance import build_region_versions
from workbench.services import CorrectionService, OcrService

# A shared module-level artifacts directory. We use the class-decorator form of
# override_settings (not the .enable() manual form) because it is reliable with
# Django's settings caching.
ART = tempfile.mkdtemp(prefix="dsw-geom-")


def _world():
    owner = User.objects.create_user("owner", password="x")
    project = Collection.objects.create(name="Prj")
    ProjectMembership.objects.create(project=project, user=owner, role="owner")
    source = SourceDocument.objects.create(collection=project, filename="a.pdf", uploaded_by=owner)
    preset = ProcessingPreset.objects.create(slug="ws-geom", name="t")
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


def _make_image(size=(2000, 2600)):
    path = Path(ART) / "pages" / "a.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (200, 210, 220)).save(path, "PNG")
    return path


ST = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


@override_settings(STORAGES=ST, ARTIFACTS_BASE_DIR=ART)
class RasterLogicalDimensionTests(TestCase):
    """Logical (Docling) vs raster (actual image) dimensions stay separate."""

    def setUp(self):
        self.w = _world()
        _make_image()
        self.client.force_login(self.w["owner"])

    def test_raster_size_prefers_persisted_image_dims(self):
        from workbench.imaging import raster_size
        self.w["page"].width = 595
        self.w["page"].height = 841
        self.w["page"].image_width = 1190
        self.w["page"].image_height = 1682
        self.w["page"].save(update_fields=["width", "height", "image_width", "image_height"])
        # Decoding the header must not be needed when persisted dims exist.
        self.assertEqual(raster_size(self.w["page"]), (1190, 1682))

    def test_raster_size_falls_back_to_decoding_when_unsaved(self):
        from workbench.imaging import raster_size
        self.w["page"].image_width = None
        self.w["page"].image_height = None
        self.w["page"].save(update_fields=["image_width", "image_height"])
        self.assertEqual(raster_size(self.w["page"]), (2000, 2600))

    def test_pyramid_full_level_uses_raster_not_logical_dims(self):
        """Logical 595×841 vs raster 1190×1682 → the full level must be 1190×1682."""
        self.w["page"].width = 595
        self.w["page"].height = 841
        self.w["page"].image_width = 1190
        self.w["page"].image_height = 1682
        self.w["page"].save(update_fields=["width", "height", "image_width", "image_height"])
        _make_image((1190, 1682))
        body = self.client.get(reverse("page_workspace_data", args=[self.w["page"].pk])).json()
        levels = body["image_levels"]
        # The last (full) level must carry the raster dimensions.
        self.assertEqual(levels[-1]["width"], 1190)
        self.assertEqual(levels[-1]["height"], 1682)
        # Levels strictly ascending in height, and the full level is real-res.
        heights = [l["height"] for l in levels]
        self.assertEqual(heights, sorted(heights))
        self.assertEqual(heights[-1], 1682)
        # Logical size is reported separately, not confused with the pyramid.
        self.assertEqual(body["logical_size"], [595, 841])
        self.assertEqual(body["raster_size"], [1190, 1682])

    def test_malformed_pyramid_falls_back_to_single_full_image(self):
        """A raster no larger than the preview produces a redundant pyramid; the
        page must degrade safely to a single full-image source."""
        self.w["page"].image_width = 600
        self.w["page"].image_height = 800
        self.w["page"].save(update_fields=["image_width", "image_height"])
        _make_image((600, 800))
        body = self.client.get(reverse("page_workspace_data", args=[self.w["page"].pk])).json()
        levels = body["image_levels"]
        self.assertEqual(len(levels), 1)  # single full level fallback
        self.assertEqual(levels[0]["width"], 600)
        self.assertEqual(levels[0]["height"], 800)

    def test_derivative_metadata_non_blocking_expected_sizes(self):
        """Workspace metadata must not force derivative generation (no blocking
        resize). derivative_info reports deterministic expected sizes."""
        from workbench.imaging import derivative_info
        self.w["page"].image_width = 1190
        self.w["page"].image_height = 1682
        self.w["page"].save(update_fields=["image_width", "image_height"])
        tw, th = derivative_info(self.w["page"], "thumbnail")
        pw, ph = derivative_info(self.w["page"], "preview")
        self.assertLess(tw, pw)
        self.assertLess(th, ph)
        # No derivative file should have been generated by this metadata call.
        self.assertFalse((Path(ART) / "derived" / "pages" / str(self.w["page"].pk) / "thumbnail.png").exists())


@override_settings(STORAGES=ST, ARTIFACTS_BASE_DIR=ART)
class DerivativeAtomicityTests(TestCase):
    def setUp(self):
        self.w = _world()
        _make_image()

    def test_corrupt_existing_derivative_is_regenerated(self):
        from workbench.imaging import ensure_derivative
        # Write a corrupt placeholder at the deterministic path.
        dest = Path(ART) / "derived" / "pages" / str(self.w["page"].pk) / "thumbnail.png"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"not-a-png")
        result = ensure_derivative(self.w["page"], "thumbnail")
        self.assertIsNotNone(result)
        path, size = result
        with Image.open(path) as img:
            self.assertGreater(img.width, 0)
        self.assertGreater(size[0], 0)


@override_settings(STORAGES=ST, ARTIFACTS_BASE_DIR=ART)
class CandidateSupersessionTests(TestCase):
    def setUp(self):
        self.w = _world()
        self.client.force_login(self.w["owner"])

    def _completed(self, text, provider="qwen", model="m"):
        item = OcrService.create(page=self.w["page"], region=self.w["region"], provider=provider, model=model, user=self.w["owner"])
        item.state = "completed"
        item.candidate_text = text
        item.save(update_fields=["state", "candidate_text"])
        return item

    def test_accepted_candidate_reaccept_after_supersession_creates_fresh(self):
        """Candidate A accepted → manual edit B supersedes it → re-accepting A
        creates a fresh correction and makes A current again."""
        region = self.w["region"]
        item_a = self._completed("Candidate A")
        corr_a = OcrService.accept(item=item_a, user=self.w["owner"])
        # Manual edit supersedes A.
        CorrectionService.apply(
            region=region, user=self.w["owner"], operation="text",
            before={"text": region.effective_text}, after={"text": "Manual B"},
        )
        self.assertEqual(build_region_versions(region)["current"]["source"], "manual")
        # Re-accepting A must produce a fresh correction (not return corr_a).
        corr_a2 = OcrService.accept(item=item_a, user=self.w["owner"])
        self.assertNotEqual(corr_a2.pk, corr_a.pk)
        self.assertEqual(build_region_versions(region)["current"]["source"], "vision")
        self.assertEqual(region.effective_text, "Candidate A")

    def test_source_link_persists_across_revert_reaccept(self):
        """Every candidate-derived correction permanently keeps its Vision
        source, and filtered corrections stay labelled Vision historically."""
        region = self.w["region"]
        item = self._completed("Candidate A")
        corr1 = OcrService.accept(item=item, user=self.w["owner"])
        CorrectionService.revert(correction=corr1, user=self.w["owner"])
        corr2 = OcrService.accept(item=item, user=self.w["owner"])
        self.assertEqual(corr1.source_ocr_request_id, item.pk)
        self.assertEqual(corr2.source_ocr_request_id, item.pk)
        versions = build_region_versions(region)
        vision_entries = [e for e in versions["entries"] if e["kind"] == "vision"]
        self.assertGreaterEqual(len(vision_entries), 1)
        self.assertEqual(versions["current"]["source"], "vision")

    def test_htr_source_link_is_permanent(self):
        from workbench.services import HtrService
        region = self.w["region"]
        item = OcrRequest.objects.create(
            source_document=self.w["source"], document=self.w["document"], page=self.w["page"], region=region,
            target="region", provider="htr", prompt="htr", state="completed",
            candidate_text="HTR text", metadata={"pipeline_id": "htrflow-trocr-kurrent"},
        )
        corr = HtrService.accept(item=item, user=self.w["owner"])
        CorrectionService.revert(correction=corr, user=self.w["owner"])
        corr2 = HtrService.accept(item=item, user=self.w["owner"])
        self.assertEqual(corr2.source_ocr_request_id, item.pk)
        self.assertEqual(build_region_versions(region)["current"]["source"], "htr")


@override_settings(STORAGES=ST, ARTIFACTS_BASE_DIR=ART)
class HistoricalVersionReapplyTests(TestCase):
    def setUp(self):
        self.w = _world()
        self.client.force_login(self.w["owner"])

    def test_historical_manual_version_stays_selectable_and_reappliable(self):
        region = self.w["region"]
        CorrectionService.apply(
            region=region, user=self.w["owner"], operation="text",
            before={"text": "Machine"}, after={"text": "Old Manual"},
        )
        active = RegionCorrection.objects.filter(region=region, operation="text", status="active").first()
        CorrectionService.revert(correction=active, user=self.w["owner"])
        versions = build_region_versions(region)
        self.assertEqual(versions["current"]["source"], "imported")
        manual = next(e for e in versions["entries"] if e["kind"] == "manual")
        self.assertTrue(manual["selectable"])      # historical stay inspectable
        self.assertTrue(manual["reappliable"])     # differs from effective text
        self.assertTrue(manual["accept_url"])      # "Use transcription" offered

    def test_imported_reappliable_when_not_current(self):
        region = self.w["region"]
        CorrectionService.apply(
            region=region, user=self.w["owner"], operation="text",
            before={"text": "Machine"}, after={"text": "New"},
        )
        versions = build_region_versions(region)
        imported = next(e for e in versions["entries"] if e["kind"] == "imported")
        self.assertTrue(imported["reappliable"])
        self.assertTrue(imported["accept_url"])

    def test_reapply_endpoint_creates_fresh_manual_correction(self):
        region = self.w["region"]
        corr = CorrectionService.apply(
            region=region, user=self.w["owner"], operation="text",
            before={"text": "Machine"}, after={"text": "Old Manual"},
        )
        CorrectionService.revert(correction=corr, user=self.w["owner"])
        response = self.client.post(
            reverse("reapply_region_correction", args=[corr.pk]), **{"HTTP_HX_REQUEST": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(region.effective_text, "Old Manual")
        self.assertEqual(build_region_versions(region)["current"]["source"], "manual")

    def test_accept_imported_text_restores_and_is_auditable(self):
        region = self.w["region"]
        CorrectionService.apply(
            region=region, user=self.w["owner"], operation="text",
            before={"text": "Machine"}, after={"text": "Edited"},
        )
        response = self.client.post(
            reverse("accept_imported_text", args=[region.pk]), **{"HTTP_HX_REQUEST": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(region.effective_text, "Machine")
        # Restoring imported text is itself an auditable manual correction.
        self.assertEqual(build_region_versions(region)["current"]["source"], "manual")


@override_settings(STORAGES=ST, ARTIFACTS_BASE_DIR=ART)
class PageInspectorPollUrlTests(TestCase):
    def setUp(self):
        self.w = _world()
        _make_image()
        self.client.force_login(self.w["owner"])

    def test_page_inspector_provides_poll_url_when_pending(self):
        OcrService.create(page=self.w["page"], provider="qwen", user=self.w["owner"])
        response = self.client.get(reverse("page_inspector", args=[self.w["page"].pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-poll-url=")
        self.assertContains(response, 'data-async-state="pending"')

    def test_page_inspector_marks_completed_not_current(self):
        item = OcrService.create(page=self.w["page"], provider="qwen", user=self.w["owner"])
        item.state = "completed"
        item.candidate_text = "Page text"
        item.save(update_fields=["state", "candidate_text"])
        response = self.client.get(reverse("page_inspector", args=[self.w["page"].pk]))
        # A completed page OCR candidate must NOT be shown as "current"; its dot
        # uses is-completed, not is-current. Page text is labelled imported.
        self.assertContains(response, "is-completed")
        self.assertNotContains(response, "version-dot kind-vision is-current")
        self.assertContains(response, "Imported page text")
@override_settings(STORAGES=ST, ARTIFACTS_BASE_DIR=ART)
class SourceOcrRequestBackfillMigrationTests(TransactionTestCase):
    """Migration 0025 permanently links pre-existing accepted corrections to the
    recognition request that produced them.

    Regression: 0024 added RegionCorrection.source_ocr_request but left
    already-accepted corrections with NULL, so provenance resolution would call
    them Manual and the UI could show a Vision/HTR candidate AND a Manual
    correction as 'current' for the same text. 0025 backfills from the
    OcrRequest.accepted_correction shortcut.
    """

    migrate_from = [("workbench", "0024_page_image_height_page_image_width_and_more")]
    migrate_to = [("workbench", "0025_backfill_correction_source_ocr_request")]

    def _world_old(self, Old):
        user = Old.get_model("auth", "User").objects.create(username="migration-user")
        project = Old.get_model("workbench", "Collection").objects.create(name="Mig", created_by_id=user.pk)
        source = Old.get_model("workbench", "SourceDocument").objects.create(
            collection_id=project.pk, filename="a.pdf", sha256="s", uploaded_by_id=user.pk,
        )
        preset = Old.get_model("workbench", "ProcessingPreset").objects.create(slug="mig", name="m")
        job = Old.get_model("workbench", "ProcessingJob").objects.create(
            source_document_id=source.pk, preset_id=preset.pk, state="completed",
        )
        doc = Old.get_model("workbench", "Document").objects.create(
            collection_id=project.pk, external_id="r1", filename="a.pdf", page_count=1,
        )
        page = Old.get_model("workbench", "Page").objects.create(document_id=doc.pk, page_number=1)
        region = Old.get_model("workbench", "PageRegion").objects.create(
            source_document_id=source.pk, job_id=job.pk, page_id=page.pk,
            page_number=1, region_type="text", left=.1, top=.1, right=.7, bottom=.5,
            text="Machine",
        )
        return project, region

    def test_backfill_links_old_accepted_correction_to_request(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        project, region = self._world_old(old_apps)

        Corr = old_apps.get_model("workbench", "RegionCorrection")
        Req = old_apps.get_model("workbench", "OcrRequest")

        corr = Corr.objects.create(
            region_id=region.pk, document_id=region.page.document_id,
            created_by_id=None, operation="text", before={"text": "Machine"},
            after={"text": "HTR text"}, status="active",
        )
        req = Req.objects.create(
            source_document_id=region.source_document_id,
            document_id=region.page.document_id, page_id=region.page_id,
            region_id=region.pk, target="region", provider="htr", prompt="htr",
            state="completed", candidate_text="HTR text", accepted_correction_id=corr.pk,
        )
        # Pre-migration provenance: accepted but source_ocr_request is NULL.
        fresh = Req.objects.get(pk=req.pk)
        self.assertIsNone(Req.objects.filter(pk=req.pk).values_list("accepted_correction__source_ocr_request_id", flat=True).first())

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)

        NewCorr = executor.loader.project_state(self.migrate_to).apps.get_model("workbench", "RegionCorrection")
        migrated = NewCorr.objects.get(pk=corr.pk)
        self.assertEqual(migrated.source_ocr_request_id, req.pk)
        # Same correction is still the accepted shortcut on the request.
        NewReq = executor.loader.project_state(self.migrate_to).apps.get_model("workbench", "OcrRequest")
        self.assertEqual(NewReq.objects.get(pk=req.pk).accepted_correction_id, corr.pk)

    def test_backfill_preserves_existing_source_link(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        project, region = self._world_old(old_apps)

        Corr = old_apps.get_model("workbench", "RegionCorrection")
        Req = old_apps.get_model("workbench", "OcrRequest")

        # Two requests, both accepted; the second correction already points at
        # the first request (manual/populated link must be preserved).
        corr_a = Corr.objects.create(
            region_id=region.pk, document_id=region.page.document_id, operation="text",
            before={"text": "Machine"}, after={"text": "A"}, status="active",
        )
        corr_b = Corr.objects.create(
            region_id=region.pk, document_id=region.page.document_id, operation="text",
            before={"text": "Machine"}, after={"text": "B"}, status="active",
        )
        req_a = Req.objects.create(
            source_document_id=region.source_document_id, document_id=region.page.document_id,
            page_id=region.page_id, region_id=region.pk, target="region",
            provider="vision", prompt="p", state="completed", candidate_text="A",
            accepted_correction_id=corr_a.pk,
        )
        Req.objects.create(
            source_document_id=region.source_document_id, document_id=region.page.document_id,
            page_id=region.page_id, region_id=region.pk, target="region",
            provider="htr", prompt="h", state="completed", candidate_text="B",
            accepted_correction_id=corr_b.pk,
        )
        # corr_b already has a populated provenance link (to req_a) — the
        # backfill must not overwrite a non-NULL source_ocr_request.
        corr_b.source_ocr_request_id = req_a.pk
        corr_b.save()

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)

        NewCorr = executor.loader.project_state(self.migrate_to).apps.get_model("workbench", "RegionCorrection")
        self.assertEqual(NewCorr.objects.get(pk=corr_a.pk).source_ocr_request_id, req_a.pk)
        # corr_b's pre-existing link to req_a is preserved, NOT overwritten.
        self.assertEqual(NewCorr.objects.get(pk=corr_b.pk).source_ocr_request_id, req_a.pk)
