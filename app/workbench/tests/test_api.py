import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.test import override_settings
from unittest.mock import patch

from workbench.models import (
    AuditEvent, ChatMessage, ChatRun, ChatThread, Collection, Document, ExtractionRun, Page, ProcessingJob,
    ProcessingPreset, ProjectMembership, ReviewTask, SourceDocument,
    TableCandidate, TableExtraction, PageRegion, RegionCorrection, SearchPassage,
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
            "documents:manage", "chat:read", "chat:write", "chat:retry", "support:read", "support:export"],
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

    def test_document_revision_page_and_region_reads_are_structured_and_scoped(self):
        source = self.make_source()
        preset = ProcessingPreset.objects.create(slug="read-api", name="Read API")
        job = ProcessingJob.objects.create(source_document=source, preset=preset, state="completed", processor="fixture")
        revision = Document.objects.create(collection=self.project, external_id="read-api", filename=source.filename, page_count=1)
        job.result_document = revision
        job.save(update_fields=["result_document"])
        source.active_document = revision
        source.page_count = 1
        source.save(update_fields=["active_document", "page_count"])
        page = Page.objects.create(document=revision, page_number=1, width=100, height=200)
        region = PageRegion.objects.create(
            source_document=source, job=job, page=page, page_number=1,
            region_type="title", left=.1, top=.2, right=.8, bottom=.4,
            page_width=100, page_height=200, confidence=.93, text="Imported title",
        )
        RegionCorrection.objects.create(
            region=region, document=revision, created_by=self.user,
            operation="text", before={"text": "Imported title"}, after={"text": "Effective title"},
            reason="fixture correction",
        )

        detail = self.client.get(reverse("api_document_detail", args=[source.pk]), **self.auth())
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["document"]["active_revision_id"], revision.pk)
        revisions = self.client.get(reverse("api_document_revisions", args=[source.pk]), **self.auth())
        self.assertEqual(revisions.status_code, 200)
        self.assertEqual(revisions.json()["revisions"][0]["immutable"], True)
        page_response = self.client.get(reverse("api_revision_page", args=[source.pk, revision.pk, 1]), **self.auth())
        self.assertEqual(page_response.status_code, 200)
        page_json = page_response.json()["page"]
        self.assertEqual(page_json["regions"][0]["text"], "Effective title")
        self.assertAlmostEqual(page_json["regions"][0]["normalized_bounds"]["width"], .7)
        corrections = self.client.get(reverse("api_region_corrections", args=[region.pk]), **self.auth())
        self.assertEqual(corrections.status_code, 200)
        self.assertEqual(corrections.json()["corrections"][0]["after"]["text"], "Effective title")

        mutation = self.client.post(
            reverse("api_region_text_correction", args=[region.pk]),
            data=json.dumps({"replacement_text": "New title", "expected_current_text": "Effective title", "reason": "test"}),
            content_type="application/json", **self.auth(),
        )
        self.assertEqual(mutation.status_code, 201)
        self.assertEqual(mutation.json()["region"]["text"], "New title")
        stale = self.client.post(
            reverse("api_region_text_correction", args=[region.pk]),
            data=json.dumps({"replacement_text": "Stale", "expected_current_text": "Effective title"}),
            content_type="application/json", **self.auth(),
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["error"]["code"], "region_state_conflict")

        SearchPassage.objects.create(
            project=self.project, source_document=source, processed_revision=revision,
            processing_job=job, page=page, page_region=region, passage_type="region",
            text="Effective title", normalized_text="effective title",
        )
        search = self.client.get(reverse("api_search"), {"q": "effective title", "document": source.pk}, **self.auth())
        self.assertEqual(search.status_code, 200)
        self.assertEqual(search.json()["results"][0]["revision_id"], revision.pk)

    def test_user_created_scope_set_can_submit_upload(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("user_settings"), {
            "action": "create_api_token", "token_name": "new-token",
        })
        self.assertEqual(response.status_code, 302)
        token = self.user.api_tokens.exclude(pk=self.token.pk).get()
        self.assertIn("documents:upload", token.scopes)
        self.assertIn("jobs:submit", token.scopes)
        self.assertIn("chat:read", token.scopes)
        self.assertIn("chat:write", token.scopes)
        self.assertIn("chat:retry", token.scopes)
        self.assertIn("support:export", token.scopes)

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

    def test_chat_run_reads_require_thread_owner_or_admin(self):
        other_user = User.objects.create_user("other-api-user", password="pass")
        ProjectMembership.objects.create(project=self.project, user=other_user, role="viewer")
        thread = ChatThread.objects.create(project=self.project, created_by=other_user, title="Private thread")
        message = ChatMessage.objects.create(thread=thread, role="user", text="private?", ordinal=0)
        run = ChatRun.objects.create(thread=thread, user_message=message, state="completed")

        self.assertEqual(self.client.get(reverse("api_chat_thread_detail", args=[thread.pk]), **self.auth()).status_code, 200)
        self.assertEqual(self.client.get(reverse("api_chat_run_detail", args=[run.pk]), **self.auth()).status_code, 404)
        self.assertEqual(self.client.get(reverse("api_chat_run_evidence", args=[run.pk]), **self.auth()).status_code, 404)

        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        self.assertEqual(self.client.get(reverse("api_chat_run_detail", args=[run.pk]), **self.auth()).status_code, 200)

    def test_chat_management_requires_scope_and_project_access(self):
        thread = self.make_thread()
        response = self.client.patch(
            reverse("api_chat_thread_detail", args=[thread.pk]),
            data=json.dumps({"title": "Renamed"}), content_type="application/json", **self.auth(),
        )
        self.assertEqual(response.status_code, 403)
        self.token.scopes = list(self.token.scopes) + ["chat:manage"]
        self.token.save(update_fields=["scopes"])
        other = Collection.objects.create(name="Other management archive")
        other_thread = ChatThread.objects.create(project=other, created_by=self.user, title="Private")
        response = self.client.patch(
            reverse("api_chat_thread_detail", args=[other_thread.pk]),
            data=json.dumps({"title": "Should not change"}), content_type="application/json", **self.auth(),
        )
        self.assertEqual(response.status_code, 404)

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
        self.assertContains(response, "What happened")
        self.assertContains(response, "Retrieval and model interaction")
        self.assertContains(response, "Raw diagnostic record")
        conversation = self.client.get(reverse("chat_thread", args=[thread.pk]))
        self.assertEqual(conversation.status_code, 200)
        self.assertContains(conversation, "Inspect run diagnostics")

    @override_settings(STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})
    def test_maintainer_diagnostics_supports_all_project_scope(self):
        thread = ChatThread.objects.create(
            project=None, created_by=self.user, scope_mode="all",
            scope_config={"mode": "all", "project_ids": [self.project.pk],
                          "source_ids": [self.make_source().pk], "revision_ids": [],
                          "attachment_ids": [], "filters": {}},
        )
        message = ChatMessage.objects.create(thread=thread, role="user", text="all projects", ordinal=0)
        run = ChatRun.objects.create(
            thread=thread, user_message=message, state="completed",
            scope_snapshot=thread.scope_config,
            model_metadata={"final_answer": "safe all-project answer"},
        )
        self.client.force_login(self.user)
        self.user.groups.create(name="Administrator")
        response = self.client.get(reverse("chat_run_diagnostics", args=[run.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "All accessible projects")
        self.assertContains(response, "safe all-project answer")
