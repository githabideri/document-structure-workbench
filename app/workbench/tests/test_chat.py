from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from workbench.models import ChatRun, ChatThread, Collection, Document, Page, ProcessingJob, ProcessingPreset, ProjectMembership, SearchPassage, SourceDocument

User = get_user_model()


@override_settings(STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}, DSW_CHAT_BASE_URL="http://llama.test/v1", DSW_CHAT_MODEL="qwen")
class ChatTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("chat-user", password="pass")
        self.project = Collection.objects.create(name="Archive", created_by=self.user)
        ProjectMembership.objects.create(project=self.project, user=self.user, role="owner")
        self.source = SourceDocument.objects.create(collection=self.project, filename="notes.pdf", uploaded_by=self.user)
        preset = ProcessingPreset.objects.create(slug="chat", name="Chat")
        job = ProcessingJob.objects.create(source_document=self.source, preset=preset, state="completed")
        revision = self.document = Document.objects.create(collection=self.project, external_id="notes", filename="notes.pdf")
        job.result_document = revision
        job.save(update_fields=["result_document"])
        page = Page.objects.create(document=revision, page_number=2)
        SearchPassage.objects.create(project=self.project, source_document=self.source, processed_revision=revision, processing_job=job, page=page, passage_type="region", text="The restoration happened in 1957.", normalized_text="the restoration happened in 1957.")

    def test_chat_queues_persistent_run(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("chat"), {"source": [str(self.source.pk)], "question": "When?"})
        self.assertEqual(response.status_code, 302)
        thread = ChatThread.objects.get()
        self.assertRedirects(response, reverse("chat_thread", args=[thread.pk]))
        self.assertEqual(thread.messages.filter(role="user").count(), 1)
        self.assertEqual(ChatRun.objects.filter(thread=thread, state="queued").count(), 1)

    @patch("workbench.chat.requests.post")
    def test_worker_run_persists_validated_citation(self, post):
        post.return_value = Mock(status_code=200, json=lambda: {"choices": [{"message": {"content": "It happened in 1957. [S1]"}}]})
        thread = ChatThread.objects.create(project=self.project, created_by=self.user)
        thread.selected_sources.set([self.source])
        from workbench.chat import create_chat_run, process_chat_run
        run = create_chat_run(thread, "restoration 1957")
        process_chat_run(run)
        run.refresh_from_db()
        self.assertEqual(run.state, "completed")
        self.assertEqual(run.evidence_items.count(), 1)
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse("chat_thread", args=[thread.pk])), "It happened in 1957.")

    def test_chat_requires_document_and_question(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("chat"), {})
        self.assertContains(response, "Choose at least one document")

    def test_workspace_citation_offers_return_to_thread(self):
        thread = ChatThread.objects.create(project=self.project, created_by=self.user)
        self.client.force_login(self.user)
        response = self.client.get(reverse("document_detail", args=[self.document.pk]), {"thread": thread.pk})
        self.assertContains(response, f"/chat/{thread.pk}/")
