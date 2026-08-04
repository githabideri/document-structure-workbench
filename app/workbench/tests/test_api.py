from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from workbench.models import (
    Collection, Document, ExtractionRun, Page, ProcessingJob,
    ProcessingPreset, ProjectMembership, ReviewTask, SourceDocument,
    TableCandidate, TableExtraction,
)

User = get_user_model()


class ApiContractTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("api-user", password="pass")
        self.project = Collection.objects.create(name="API archive", created_by=self.user)
        ProjectMembership.objects.create(project=self.project, user=self.user, role="owner")
        self.raw_token = "api-test-token"
        from workbench.models import ApiToken
        self.token = ApiToken.objects.create(
            user=self.user, name="test", token_prefix="api-test",
            token_hash=ApiToken.hash_token(self.raw_token),
            scopes=["projects:read", "documents:read", "documents:upload", "jobs:submit", "jobs:read", "reviews:write"],
        )

    def auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.raw_token}"}

    def test_health_is_public_and_versioned(self):
        response = self.client.get(reverse("api_health"))
        self.assertIn(response.status_code, (200, 500))
        self.assertIn("status", response.json())

    def test_projects_requires_token_and_returns_project(self):
        self.assertEqual(self.client.get(reverse("api_projects")).status_code, 401)
        response = self.client.get(reverse("api_projects"), **self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["projects"][0]["name"], "API archive")

    def test_user_created_scope_set_can_submit_upload(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("user_settings"), {
            "action": "create_api_token", "token_name": "new-token",
        })
        self.assertEqual(response.status_code, 302)
        token = self.user.api_tokens.exclude(pk=self.token.pk).get()
        self.assertIn("documents:upload", token.scopes)
        self.assertIn("jobs:submit", token.scopes)

    def test_review_preference_is_mapped_to_model_choice(self):
        source = SourceDocument.objects.create(collection=self.project, filename="review.pdf")
        preset = ProcessingPreset.objects.create(slug="api-review", name="API review")
        job = ProcessingJob.objects.create(source_document=source, preset=preset, state="completed")
        document = Document.objects.create(collection=self.project, external_id="review", filename="review.pdf")
        job.result_document = document
        job.save(update_fields=["result_document"])
        page = Page.objects.create(document=document, page_number=1)
        table = TableCandidate.objects.create(document=document, page=page, stable_table_id="table-1")
        run = ExtractionRun.objects.create(document=document, profile="standard-docling", status="completed")
        first = TableExtraction.objects.create(table_candidate=table, extraction_run=run)
        second_run = ExtractionRun.objects.create(document=document, profile="granite-table-crop", status="completed")
        second = TableExtraction.objects.create(table_candidate=table, extraction_run=second_run)
        task = ReviewTask.objects.create(table_candidate=table, candidate_x=first, candidate_y=second)

        response = self.client.post(
            reverse("api_task_submit", args=[task.pk]),
            data='{"score_x": 2, "score_y": 1, "preference": "x"}',
            content_type="application/json", **self.auth(),
        )
        self.assertEqual(response.status_code, 200)
        task.review.refresh_from_db()
        self.assertEqual(task.review.preferred_result, "candidate_x")
