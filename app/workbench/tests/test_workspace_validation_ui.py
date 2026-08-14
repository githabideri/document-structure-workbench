"""UI coverage for the editorial-marker toolbar and candidate triage badges.

Covers ADR 0002's UI obligation (nobody has to type the bracket syntax: the
editor offers buttons) and ADR 0003's follow-up obligation (the persisted
``needs_review`` verdict is surfaced in the version rail and the OCR history).
"""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from workbench.models import (
    Collection, Document, OcrRequest, Page, PageRegion, ProcessingJob,
    ProcessingPreset, ProjectMembership, SourceDocument,
)
from workbench.provenance import build_region_versions
from workbench.validation import evaluate_candidate

User = get_user_model()


def _world():
    owner = User.objects.create_user("owner", password="x")
    project = Collection.objects.create(name="UI project", created_by=owner)
    ProjectMembership.objects.create(project=project, user=owner, role="owner")
    source = SourceDocument.objects.create(collection=project, filename="doc.pdf", uploaded_by=owner)
    preset = ProcessingPreset.objects.create(slug="ui-preset", name="t")
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
    return {"owner": owner, "project": project, "source": source,
            "document": document, "page": page, "region": region}


def _candidate(world, *, text, raw=None, metadata=None):
    return OcrRequest.objects.create(
        source_document=world["source"], document=world["document"],
        page=world["page"], region=world["region"], target="region",
        provider="vision", model="test-model", prompt="transcribe",
        state="completed", candidate_text=text,
        raw_response=raw or {}, metadata=metadata or {},
        created_by=world["owner"],
    )


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class MarkerToolbarTests(TestCase):
    def setUp(self):
        self.w = _world()
        self.client.force_login(self.w["owner"])

    def _inspector(self):
        return self.client.get(reverse("region_inspector", args=[self.w["region"].pk]), **{"HTTP_HX_REQUEST": "true"})

    def test_editor_offers_marker_buttons_with_legend(self):
        response = self._inspector()
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("data-marker-action=\"uncertain\"", html)
        self.assertIn("data-marker-action=\"illegible\"", html)
        self.assertIn("data-marker-action=\"abbreviation\"", html)
        self.assertIn("marker-legend", html)

    def test_marker_buttons_hidden_for_viewers(self):
        viewer = User.objects.create_user("viewer", password="x")
        ProjectMembership.objects.create(project=self.w["project"], user=viewer, role="viewer")
        self.client.force_login(viewer)
        response = self._inspector()
        html = response.content.decode()
        self.assertNotIn("data-marker-action", html)


class ProvenanceReviewBadgeTests(TestCase):
    """build_region_versions surfaces the persisted verdict (ADR 0003)."""

    def setUp(self):
        self.w = _world()

    def test_flagged_candidate_entry_carries_reasons(self):
        request = _candidate(
            self.w, text="Wort[?] und [illegible]",
            metadata={"validation": evaluate_candidate("Wort[?] und [illegible]")},
        )
        versions = build_region_versions(self.w["region"])
        entry = next(e for e in versions["entries"] if e["id"] == f"ocr-{request.pk}")
        self.assertTrue(entry["needs_review"])
        self.assertTrue(any("uncertain" in label for label in entry["review_reasons"]))
        self.assertTrue(any("illegible" in label for label in entry["review_reasons"]))

    def test_clean_candidate_entry_is_not_flagged(self):
        request = _candidate(
            self.w, text="Ein klarer Satz.",
            metadata={"validation": evaluate_candidate("Ein klarer Satz.")},
        )
        versions = build_region_versions(self.w["region"])
        entry = next(e for e in versions["entries"] if e["id"] == f"ocr-{request.pk}")
        self.assertFalse(entry["needs_review"])
        self.assertEqual(entry["review_reasons"], [])

    def test_legacy_candidate_without_verdict_renders_unflagged(self):
        request = _candidate(self.w, text="old candidate")  # pre-ADR rows
        versions = build_region_versions(self.w["region"])
        entry = next(e for e in versions["entries"] if e["id"] == f"ocr-{request.pk}")
        self.assertFalse(entry["needs_review"])

    def test_version_rail_renders_badge_for_flagged_candidate(self):
        self.client.force_login(self.w["owner"])
        _candidate(
            self.w, text="Wort[?]",
            metadata={"validation": evaluate_candidate("Wort[?]")},
        )
        response = self.client.get(
            reverse("region_versions_fragment", args=[self.w["region"].pk]),
            **{"HTTP_HX_REQUEST": "true"},
        )
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("needs-review-badge", html)

    def test_ocr_history_renders_badge_for_flagged_candidate(self):
        from django.template.loader import render_to_string

        request = _candidate(
            self.w, text="Wort[?]",
            metadata={"validation": evaluate_candidate("Wort[?]")},
        )
        html = render_to_string(
            "workbench/_ocr_history_fragment.html",
            {"ocr_candidates": [request], "can_edit_document": False, "ocr_title": "OCR"},
        )
        self.assertIn("needs-review-badge", html)
        # The tooltip carries the translated reason, not the raw code.
        self.assertNotIn('"uncertain_marker"', html)

    def test_ocr_history_renders_no_badge_for_clean_candidate(self):
        from django.template.loader import render_to_string

        request = _candidate(
            self.w, text="Klarer Satz.",
            metadata={"validation": evaluate_candidate("Klarer Satz.")},
        )
        html = render_to_string(
            "workbench/_ocr_history_fragment.html",
            {"ocr_candidates": [request], "can_edit_document": False, "ocr_title": "OCR"},
        )
        self.assertNotIn("needs-review-badge", html)
