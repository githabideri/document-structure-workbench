from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from workbench.models import Collection, Document, Page, ProcessingJob, ProcessingPreset, ProjectMembership, SearchPassage, SourceDocument

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
        revision = Document.objects.create(collection=self.project, external_id="notes", filename="notes.pdf")
        job.result_document = revision
        job.save(update_fields=["result_document"])
        page = Page.objects.create(document=revision, page_number=2)
        SearchPassage.objects.create(project=self.project, source_document=self.source, processed_revision=revision, processing_job=job, page=page, passage_type="region", text="The restoration happened in 1957.", normalized_text="the restoration happened in 1957.")

    @patch("workbench.chat.requests.post")
    def test_chat_returns_validated_citation(self, post):
        post.return_value = Mock(status_code=200, json=lambda: {"choices": [{"message": {"content": "It happened in 1957. [S1]"}}]})
        self.client.force_login(self.user)
        response = self.client.post(reverse("chat"), {"source": [str(self.source.pk)], "question": "When?"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "It happened in 1957.")
        self.assertContains(response, "revision=")
        post.assert_called_once()

    def test_chat_requires_document_and_question(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("chat"), {})
        self.assertContains(response, "Choose at least one document")
