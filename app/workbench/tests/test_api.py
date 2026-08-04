from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.test import override_settings
from unittest.mock import patch

from workbench.models import (
    AuditEvent, ChatMessage, ChatRun, ChatThread, Collection, Document, ExtractionRun, Page, ProcessingJob,
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
            scopes=["projects:read", "documents:read", "documents:upload", "jobs:submit", "jobs:read", "reviews:write",
                    "chat:read", "chat:write", "chat:retry", "support:read", "support:export"],
        )

    def auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.raw_token}"}

    def make_source(self):
        return SourceDocument.objects.create(collection=self.project, filename="source-a.pdf", uploaded_by=self.user)

    def make_thread(self):
        thread = ChatThread.objects.create(project=self.project, created_by=self.user, title="Generic test thread")
        thread.selected_sources.set([self.make_source()])
        return thread

    def test_health_is_public_and_versioned(self):
        response = self.client.get(reverse("api_health"))
        self.assertIn(response.status_code, (200, 500))
        self.assertIn("status", response.json())

    @override_settings(DSW_CHAT_BASE_URL="http://provider.test/v1", DSW_CHAT_MODEL="test-model", DSW_CHAT_API_KEY="secret")
    @patch("workbench.services.requests.get")
    def test_health_probes_provider_and_exact_model(self, get):
        get.return_value.json.return_value = {"data": [{"id": "test-model"}, {"id": "other-model"}]}
        get.return_value.raise_for_status.return_value = None
        response = self.client.get(reverse("api_health"))
        self.assertIn(response.status_code, (200, 500))
        self.assertEqual(response.json()["chat_provider"]["reachable"], True)
        self.assertEqual(response.json()["chat_provider"]["model_available"], True)
        get.assert_called_once()

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

    def test_chat_requires_scope_and_returns_request_id(self):
        self.token.scopes = ["chat:read"]
        self.token.save(update_fields=["scopes"])
        response = self.client.get(reverse("api_chat_threads"), **self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["request_id"])
        response = self.client.post(reverse("api_chat_threads"), data="{}", content_type="application/json", **self.auth())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "insufficient_scope")

    def test_chat_lifecycle_is_project_scoped(self):
        source = self.make_source()
        payload = {"project_id": self.project.pk, "source_ids": [source.pk], "question": "What is present?"}
        created = self.client.post(reverse("api_chat_threads"), data=payload, content_type="application/json", **self.auth())
        self.assertEqual(created.status_code, 201)
        body = created.json()
        self.assertTrue(body["request_id"])
        self.assertEqual(body["run"]["state"], "queued")
        thread_id = body["thread"]["id"]
        run_id = body["run"]["id"]

        detail = self.client.get(reverse("api_chat_thread_detail", args=[thread_id]), **self.auth())
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["thread"]["scope"]["source_ids"], [source.pk])
        follow_up = self.client.post(reverse("api_chat_thread_runs", args=[thread_id]), data={"question": "Follow up?"}, content_type="application/json", **self.auth())
        self.assertEqual(follow_up.status_code, 201)
        self.assertEqual(follow_up.json()["run"]["thread_id"], thread_id)
        evidence = self.client.get(reverse("api_chat_run_evidence", args=[run_id]), **self.auth())
        self.assertEqual(evidence.status_code, 200)
        retry = self.client.post(reverse("api_chat_run_retry", args=[run_id]), data="{}", content_type="application/json", **self.auth())
        self.assertEqual(retry.status_code, 201)

    def test_chat_and_support_bundle_cannot_cross_projects(self):
        thread = self.make_thread()
        other = Collection.objects.create(name="Other archive", created_by=self.user)
        other_source = SourceDocument.objects.create(collection=other, filename="source-b.pdf", uploaded_by=self.user)
        other_thread = ChatThread.objects.create(project=other, created_by=self.user)
        other_thread.selected_sources.set([other_source])
        message = ChatMessage.objects.create(thread=other_thread, role="user", text="private?", ordinal=0)
        run = ChatRun.objects.create(thread=other_thread, user_message=message)
        self.assertEqual(self.client.get(reverse("api_chat_thread_detail", args=[other_thread.pk]), **self.auth()).status_code, 404)
        self.assertEqual(self.client.get(reverse("api_support_bundle_detail", args=[run.pk]), **self.auth()).status_code, 404)
        self.assertEqual(AuditEvent.objects.filter(object_id=str(run.pk)).count(), 0)

    def test_support_bundle_export_is_scoped_and_audited(self):
        thread = self.make_thread()
        message = ChatMessage.objects.create(thread=thread, role="user", text="diagnostic question", ordinal=0)
        run = ChatRun.objects.create(thread=thread, user_message=message, state="completed", model_metadata={"final_answer": "safe answer"})
        response = self.client.post(reverse("api_chat_support_bundle", args=[run.pk]), data={"format": "json"}, content_type="application/json", **self.auth())
        self.assertEqual(response.status_code, 200)
        bundle = response.json()["bundle"]
        self.assertEqual(bundle["final_answer"], "safe answer")
        self.assertNotIn("Authorization", response.content.decode())
        audit = AuditEvent.objects.get(object_id=str(run.pk), event_type="chat_support_bundle_exported")
        self.assertEqual(audit.after["format"], "json")
        markdown_response = self.client.post(reverse("api_chat_support_bundle", args=[run.pk]), data={"format": "markdown"}, content_type="application/json", **self.auth())
        self.assertEqual(markdown_response.status_code, 200)
        self.assertEqual(markdown_response["Content-Type"], "text/markdown")

    @override_settings(STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})
    def test_maintainer_diagnostics_view_is_admin_only(self):
        thread = self.make_thread()
        message = ChatMessage.objects.create(thread=thread, role="user", text="diagnostic question", ordinal=0)
        run = ChatRun.objects.create(thread=thread, user_message=message, state="completed", model_metadata={"final_answer": "safe answer"})
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("chat_run_diagnostics", args=[run.pk])).status_code, 403)
        self.user.groups.create(name="Administrator")
        response = self.client.get(reverse("chat_run_diagnostics", args=[run.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Maintainer diagnostics")
        self.assertContains(response, "safe answer")
