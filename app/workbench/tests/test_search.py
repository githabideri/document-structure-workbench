from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from workbench.models import Collection, Document, Page, PageRegion, ProcessingJob, ProcessingPreset, SourceDocument, SearchPassage, ProjectMembership
from workbench.search import rebuild_revision_index

User = get_user_model()


@override_settings(STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})
class SearchTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("search-user", password="pass")
        self.project = Collection.objects.create(name="Archive", created_by=self.user)
        ProjectMembership.objects.create(project=self.project, user=self.user, role="owner")
        self.source = SourceDocument.objects.create(collection=self.project, filename="inventory.pdf", uploaded_by=self.user)
        preset = ProcessingPreset.objects.create(slug="search", name="Search")
        self.job = ProcessingJob.objects.create(source_document=self.source, preset=preset, state="completed")
        self.document = Document.objects.create(collection=self.project, external_id="inventory", filename="inventory.pdf")
        self.job.result_document = self.document
        self.job.save(update_fields=["result_document"])
        # The processing worker sets active_document on completion; mirror that
        # invariant so search (which is scoped to active revisions) sees this revision.
        self.source.active_document = self.document
        self.source.save(update_fields=["active_document"])
        self.page = Page.objects.create(document=self.document, page_number=4)
        self.region = PageRegion.objects.create(
            source_document=self.source, job=self.job, page=self.page, page_number=4,
            region_type="text", left=.1, top=.1, right=.9, bottom=.2,
            text="Object R-184 was restored in 1957.",
        )

    def test_rebuild_is_revision_scoped_and_idempotent(self):
        self.assertEqual(rebuild_revision_index(self.document), 1)
        self.assertEqual(SearchPassage.objects.filter(processed_revision=self.document).count(), 1)
        self.assertEqual(rebuild_revision_index(self.document), 1)
        self.assertEqual(SearchPassage.objects.filter(processed_revision=self.document).count(), 1)

    def test_search_result_links_to_inline_reader(self):
        rebuild_revision_index(self.document)
        self.client.force_login(self.user)
        passage = SearchPassage.objects.get(processed_revision=self.document)
        response = self.client.get(reverse("search"), {"q": "R-184"})
        self.assertEqual(response.status_code, 200)
        # The matching passage is shown with the query highlighted.
        self.assertContains(response, "<mark>R-184</mark>")
        self.assertContains(response, "was restored")
        # Each card targets the inline reader endpoint for its passage.
        self.assertContains(response, reverse("search_reader", args=[passage.pk]))

    def test_search_reader_returns_scan_highlight_and_citation(self):
        rebuild_revision_index(self.document)
        self.page.image_path = "pages/test.png"
        self.page.save(update_fields=["image_path"])
        passage = SearchPassage.objects.get(processed_revision=self.document)
        self.client.force_login(self.user)
        response = self.client.get(reverse("search_reader", args=[passage.pk]), {"q": "R-184"})
        self.assertEqual(response.status_code, 200)
        # Scan image + highlighted full passage + revision-aware deep link.
        self.assertContains(response, reverse("page_image", args=[self.page.pk]))
        self.assertContains(response, "<mark>R-184</mark>")
        self.assertContains(response, f"revision={self.document.pk}")
        self.assertContains(response, f"region={self.region.pk}")

    def test_search_reader_denies_other_projects(self):
        rebuild_revision_index(self.document)
        passage = SearchPassage.objects.get(processed_revision=self.document)
        other = User.objects.create_user("reader-outsider", password="pass")
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse("search_reader", args=[passage.pk])).status_code, 403)

    def test_search_groups_results_by_document_and_shows_counts(self):
        rebuild_revision_index(self.document)
        source2 = SourceDocument.objects.create(collection=self.project, filename="ledger.pdf", uploaded_by=self.user)
        job2 = ProcessingJob.objects.create(source_document=source2, preset=self.job.preset, state="completed")
        doc2 = Document.objects.create(collection=self.project, external_id="ledger", filename="ledger.pdf")
        job2.result_document = doc2
        job2.save(update_fields=["result_document"])
        source2.active_document = doc2
        source2.save(update_fields=["active_document"])
        page2 = Page.objects.create(document=doc2, page_number=1)
        PageRegion.objects.create(
            source_document=source2, job=job2, page=page2, page_number=1,
            region_type="text", left=.1, top=.1, right=.9, bottom=.2,
            text="R-184 appears in the ledger as well.",
        )
        rebuild_revision_index(doc2)
        self.client.force_login(self.user)
        response = self.client.get(reverse("search"), {"q": "R-184"})
        self.assertEqual(response.status_code, 200)
        # Two document groups, each with its filename header.
        self.assertContains(response, "inventory.pdf")
        self.assertContains(response, "ledger.pdf")
        # Both passages are highlighted.
        self.assertContains(response, "<mark>R-184</mark>", count=2)
        self.assertContains(response, "2 matches")

    def test_search_only_returns_active_revision_passages(self):
        # The active revision contains the query...
        rebuild_revision_index(self.document)
        # ...and so does a superseded second revision of the same source.
        job2 = ProcessingJob.objects.create(source_document=self.source, preset=self.job.preset, state="completed")
        doc2 = Document.objects.create(collection=self.project, external_id="inventory-v2", filename="inventory.pdf")
        job2.result_document = doc2
        job2.save(update_fields=["result_document"])
        page2 = Page.objects.create(document=doc2, page_number=4)
        PageRegion.objects.create(
            source_document=self.source, job=job2, page=page2, page_number=4,
            region_type="text", left=.1, top=.1, right=.9, bottom=.2,
            text="Object R-184 was restored in 1957.",
        )
        rebuild_revision_index(doc2)
        # active_document is still the first revision, so the duplicate passage
        # from the superseded revision must not multiply the result.
        self.assertEqual(self.source.active_document_id, self.document.pk)
        self.client.force_login(self.user)
        response = self.client.get(reverse("search"), {"q": "R-184"})
        self.assertContains(response, "<mark>R-184</mark>", count=1)

    def test_unrelated_user_cannot_search_project(self):
        rebuild_revision_index(self.document)
        other = User.objects.create_user("other", password="pass")
        self.client.force_login(other)
        response = self.client.get(reverse("search"), {"q": "R-184"})
        self.assertNotContains(response, "Object R-184")
