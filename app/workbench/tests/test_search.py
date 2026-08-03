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

    def test_search_returns_revision_aware_citation(self):
        rebuild_revision_index(self.document)
        self.client.force_login(self.user)
        response = self.client.get(reverse("search"), {"q": "R-184"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Object R-184")
        self.assertContains(response, f"revision={self.document.pk}")
        self.assertContains(response, f"region={self.region.pk}")

    def test_unrelated_user_cannot_search_project(self):
        rebuild_revision_index(self.document)
        other = User.objects.create_user("other", password="pass")
        self.client.force_login(other)
        response = self.client.get(reverse("search"), {"q": "R-184"})
        self.assertNotContains(response, "Object R-184")
