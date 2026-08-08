import json
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from workbench.models import ChatMessage, ChatRun, ChatThread, Collection, Document, EvidenceItem, Page, ProcessingJob, ProcessingPreset, ProjectMembership, SearchPassage, SourceDocument

User = get_user_model()


@override_settings(STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}, DSW_CHAT_BASE_URL="http://llama.test/v1", DSW_CHAT_MODEL="qwen", DSW_CHAT_MAX_TOKENS=16384)
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
        self.source.active_document = revision
        self.source.save(update_fields=["active_document"])
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
        post.side_effect = [
            Mock(status_code=200, json=lambda: {"choices": [{"message": {"content": None, "tool_calls": [{"id": "call-1", "function": {"arguments": '{"query":"restoration 1957"}'}}]}}]}),
            Mock(status_code=200, json=lambda: {"choices": [{"message": {"content": "It happened in 1957. [S1]"}}]}),
        ]
        thread = ChatThread.objects.create(project=self.project, created_by=self.user)
        thread.selected_sources.set([self.source])
        from workbench.chat import create_chat_run, process_chat_run
        run = create_chat_run(thread, "restoration 1957")
        with self.settings(DSW_CHAT_TOOL_MODE="native"):
            process_chat_run(run)
        run.refresh_from_db()
        self.assertEqual(run.state, "completed")
        self.assertEqual(run.evidence_items.count(), 1)
        self.client.force_login(self.user)
        response = self.client.get(reverse("chat_thread", args=[thread.pk]))
        self.assertContains(response, "It happened in 1957.")
        self.assertContains(response, "class=\"chat-citation\"")
        self.assertContains(response, f"revision={self.document.pk}")

    @patch("workbench.chat.requests.post")
    def test_worker_uses_frozen_revision_scope_and_conversation_history(self, post):
        post.side_effect = [
            Mock(status_code=200, json=lambda: {"choices": [{"message": {"content": None, "tool_calls": [{"id": "call-1", "function": {"arguments": '{"query":"restoration 1957"}'}}]}}]}),
            Mock(status_code=200, json=lambda: {"choices": [{"message": {"content": "The answer is 1957. [S1]"}}]}),
            Mock(status_code=200, json=lambda: {"choices": [{"message": {"content": None, "tool_calls": [{"id": "call-2", "function": {"arguments": '{"query":"restoration 1957"}'}}]}}]}),
            Mock(status_code=200, json=lambda: {"choices": [{"message": {"content": "The answer is 1957. [S1]"}}]}),
        ]
        thread = ChatThread.objects.create(
            project=self.project, created_by=self.user,
            selected_revisions=[self.document.pk],
        )
        thread.selected_sources.set([self.source])
        from workbench.chat import create_chat_run, process_chat_run
        first = create_chat_run(thread, "What year?")
        with self.settings(DSW_CHAT_TOOL_MODE="native"):
            process_chat_run(first)
        second = create_chat_run(thread, "Can you restate the restoration year?")
        with self.settings(DSW_CHAT_TOOL_MODE="native"):
            process_chat_run(second)
        sent_messages = post.call_args_list[-1].kwargs["json"]["messages"]
        self.assertEqual(post.call_args_list[-1].kwargs["json"]["max_tokens"], 16384)
        self.assertTrue(any(
            message.get("role") == "assistant" and message.get("content") == "The answer is 1957. [S1]"
            for message in sent_messages
        ))
        self.assertTrue(any(
            message.get("role") == "user" and message.get("content") == "Can you restate the restoration year?"
            for message in sent_messages
        ))
        self.assertEqual(second.evidence_items.first().processed_revision_id, self.document.pk)

    @patch("workbench.chat.requests.post")
    def test_attached_document_is_injected_before_provider_search(self, post):
        post.return_value = Mock(status_code=200, json=lambda: {"choices": [{"message": {
            "content": "The attached document says restoration happened in 1957. [S1]",
        }}]})
        thread = ChatThread.objects.create(project=self.project, created_by=self.user)
        thread.selected_sources.set([self.source])
        from workbench.chat import create_chat_run, process_chat_run
        run = create_chat_run(thread, "What is in notes.pdf?", scope={
            "mode": "project", "project_ids": [self.project.pk],
            "source_ids": [self.source.pk], "revision_ids": [self.document.pk],
            "attachment_ids": [self.source.pk], "filters": {},
        })
        with self.settings(DSW_CHAT_TOOL_MODE="native"):
            process_chat_run(run)
        run.refresh_from_db()
        self.assertEqual(run.state, "completed")
        self.assertEqual(run.evidence_items.count(), 1)
        evidence = run.evidence_items.first()
        self.assertEqual(evidence.retrieval_method, "direct-attachment")
        self.assertEqual(evidence.selection_reason, "attached-document/context")
        self.assertIn("notes.pdf", post.call_args.kwargs["json"]["messages"][0]["content"])
        self.assertIn("The restoration happened in 1957.", post.call_args.kwargs["json"]["messages"][0]["content"])
        self.assertEqual(post.call_count, 1)

    @patch("workbench.chat.requests.post")
    def test_empty_search_does_not_fallback_when_attachment_is_already_context(self, post):
        post.side_effect = [
            Mock(status_code=200, json=lambda: {"choices": [{"message": {
                "content": None,
                "tool_calls": [{"id": "call-1", "function": {"arguments": '{"query":"term absent from passage"}'}}],
            }}]}),
            Mock(status_code=200, json=lambda: {"choices": [{"message": {
                "content": "The attached document says restoration happened in 1957. [S1]",
            }}]}),
        ]
        thread = ChatThread.objects.create(project=self.project, created_by=self.user)
        thread.selected_sources.set([self.source])
        from workbench.chat import create_chat_run, process_chat_run
        run = create_chat_run(thread, "What is in notes.pdf?", scope={
            "mode": "project", "project_ids": [self.project.pk],
            "source_ids": [self.source.pk], "revision_ids": [self.document.pk],
            "attachment_ids": [self.source.pk], "filters": {},
        })
        with self.settings(DSW_CHAT_TOOL_MODE="native"):
            process_chat_run(run)
        run.refresh_from_db()
        self.assertEqual(run.state, "completed")
        self.assertEqual(run.evidence_items.count(), 1)
        self.assertEqual(run.evidence_items.first().selection_reason, "attached-document/context")
        tool_result = post.call_args_list[1].kwargs["json"]["messages"][-1]["content"]
        self.assertEqual(json.loads(tool_result)["results"], [])
        self.assertEqual(json.loads(tool_result)["status"], "no_progress")
        self.assertNotIn("tools", post.call_args_list[1].kwargs["json"])
        self.assertTrue(run.events.filter(name="tool_no_progress").exists())

    @patch("workbench.chat.requests.post")
    def test_repeated_search_is_a_synthesis_handoff(self, post):
        tool_call = {"content": None, "tool_calls": [{
            "id": "call-1", "function": {"arguments": '{"query":"restoration 1957"}'},
        }]}
        post.side_effect = [
            Mock(status_code=200, json=lambda: {"choices": [{"message": tool_call}]}),
            Mock(status_code=200, json=lambda: {"choices": [{"message": {
                "content": None,
                "tool_calls": [{"id": "call-2", "function": {"arguments": '{"query":"restoration 1957"}'}}],
            }}]}),
            Mock(status_code=200, json=lambda: {"choices": [{"message": {
                "content": "The evidence establishes restoration in 1957. [S1]",
            }}]}),
        ]
        thread = ChatThread.objects.create(project=self.project, created_by=self.user)
        thread.selected_sources.set([self.source])
        from workbench.chat import create_chat_run, process_chat_run
        run = create_chat_run(thread, "When was restoration?", scope={
            "mode": "project", "project_ids": [self.project.pk],
            "source_ids": [self.source.pk], "revision_ids": [self.document.pk],
            "attachment_ids": [], "filters": {},
        })
        with self.settings(DSW_CHAT_TOOL_MODE="native"):
            process_chat_run(run)
        run.refresh_from_db()
        self.assertEqual(run.state, "completed")
        self.assertEqual(post.call_count, 3)
        self.assertNotIn("tools", post.call_args_list[2].kwargs["json"])
        self.assertEqual(run.assistant_message.text, "The evidence establishes restoration in 1957. [S1]")
        event = run.events.get(name="tool_no_progress")
        self.assertEqual(event.metadata["outcome"], "repeated_query")
        self.assertTrue(run.events.filter(name="final_answer_request", metadata__reason="tool_no_progress").exists())

    @patch("workbench.chat.requests.post")
    def test_attached_context_uses_attachment_project_not_selected_search_project(self, post):
        other_project = Collection.objects.create(name="Attached archive", created_by=self.user)
        ProjectMembership.objects.create(project=other_project, user=self.user, role="owner")
        source = SourceDocument.objects.create(collection=other_project, filename="attached.pdf", uploaded_by=self.user)
        job = ProcessingJob.objects.create(source_document=source, preset=ProcessingPreset.objects.get(slug="chat"), state="completed")
        revision = Document.objects.create(collection=other_project, external_id="attached", filename="attached.pdf")
        job.result_document = revision
        job.save(update_fields=["result_document"])
        page = Page.objects.create(document=revision, page_number=1)
        SearchPassage.objects.create(
            project=other_project, source_document=source, processed_revision=revision,
            processing_job=job, page=page, text="The attached archive concerns river restoration.",
            normalized_text="the attached archive concerns river restoration.",
        )
        post.return_value = Mock(status_code=200, json=lambda: {"choices": [{"message": {
            "content": "The attachment concerns river restoration. [S1]",
        }}]})
        thread = ChatThread.objects.create(project=self.project, created_by=self.user)
        from workbench.chat import create_chat_run, process_chat_run
        run = create_chat_run(thread, "What is the attachment about?", scope={
            "mode": "project", "project_ids": [self.project.pk],
            "source_ids": [source.pk], "revision_ids": [revision.pk],
            "attachment_ids": [source.pk], "filters": {},
        })
        process_chat_run(run)
        run.refresh_from_db()
        self.assertEqual(run.state, "completed")
        self.assertEqual(run.evidence_items.count(), 1)
        self.assertIn("river restoration", post.call_args.kwargs["json"]["messages"][0]["content"])

    @patch("workbench.chat.requests.post")
    def test_provider_reasoning_is_diagnostic_only_and_support_bundle_is_auditable(self, post):
        post.return_value = Mock(status_code=200, json=lambda: {"model": "qwen", "choices": [{"finish_reason": "stop", "message": {
            "content": "The answer is 1957.", "reasoning_content": "untrusted internal diagnostic text",
        }}]})
        thread = ChatThread.objects.create(project=self.project, created_by=self.user)
        thread.selected_sources.set([self.source])
        from workbench.chat import create_chat_run, process_chat_run
        from workbench.services import SupportBundleService
        run = create_chat_run(thread, "restoration 1957")
        process_chat_run(run)
        run.refresh_from_db()
        self.assertNotIn("reasoning_content", run.assistant_message.text)
        bundle = SupportBundleService.build(run)
        self.assertEqual(bundle["final_answer"], "The answer is 1957.")
        self.assertEqual(bundle["reasoning_content"], "untrusted internal diagnostic text")
        self.assertNotIn("Authorization", json.dumps(bundle))

    def test_run_status_is_an_htmx_fragment_until_complete(self):
        thread = ChatThread.objects.create(project=self.project, created_by=self.user)
        thread.selected_sources.set([self.source])
        from workbench.chat import create_chat_run
        run = create_chat_run(thread, "What year?")
        self.client.force_login(self.user)
        response = self.client.get(reverse("chat_run_status", args=[run.pk]))
        self.assertContains(response, "chat-run-status")
        self.assertContains(response, "Waiting for an evidence worker.")

    def test_completed_run_redirects_htmx_to_updated_thread(self):
        thread = ChatThread.objects.create(project=self.project, created_by=self.user)
        message = ChatMessage.objects.create(thread=thread, role="user", text="What year?", ordinal=0)
        run = ChatRun.objects.create(thread=thread, user_message=message, state="completed")
        self.client.force_login(self.user)
        response = self.client.get(reverse("chat_run_status", args=[run.pk]))
        self.assertEqual(response.headers["HX-Redirect"], reverse("chat_thread", args=[thread.pk]))

    def test_chat_requires_document_and_question(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("chat"), {})
        self.assertContains(response, "Choose at least one document")

    def test_chat_has_session_sidebar_and_context_panel(self):
        thread = ChatThread.objects.create(
            project=self.project, created_by=self.user, title="Restoration history",
        )
        thread.selected_sources.set([self.source])
        self.client.force_login(self.user)
        response = self.client.get(reverse("chat_thread", args=[thread.pk]))
        self.assertContains(response, "Restoration history")
        self.assertContains(response, "Conversations")
        self.assertContains(response, "Evidence context")

    def test_owner_can_rename_and_archive_conversation(self):
        thread = ChatThread.objects.create(project=self.project, created_by=self.user, title="Old title")
        self.client.force_login(self.user)
        response = self.client.post(reverse("chat_thread_rename", args=[thread.pk]), {"title": "Research notes"})
        self.assertRedirects(response, reverse("chat_thread", args=[thread.pk]))
        thread.refresh_from_db()
        self.assertEqual(thread.title, "Research notes")
        response = self.client.post(reverse("chat_thread_archive", args=[thread.pk]))
        self.assertRedirects(response, reverse("chat"))
        thread.refresh_from_db()
        self.assertTrue(thread.is_archived)
        self.assertNotContains(self.client.get(reverse("chat")), "Research notes")

    def test_workspace_citation_offers_return_to_thread(self):
        thread = ChatThread.objects.create(project=self.project, created_by=self.user)
        self.client.force_login(self.user)
        response = self.client.get(reverse("document_detail", args=[self.document.pk]), {"thread": thread.pk})
        self.assertContains(response, f"/chat/{thread.pk}/")

    # ------------------------------------------------------------------
    # Research workflow V2 — per-turn scope controls (Milestone 1)
    # ------------------------------------------------------------------

    def resolved_project_scope(self):
        from workbench.policy import ProjectAccessPolicy
        return ProjectAccessPolicy(user=self.user).resolve_chat_scope(
            mode="project", project_id=self.project.pk, source_ids=[self.source.pk],
        )

    @staticmethod
    def newest_run(thread):
        return thread.runs.order_by("-id").first()

    def test_thread_view_renders_followup_scope_editor(self):
        thread = ChatThread.objects.create(project=self.project, created_by=self.user, title="Scope editor")
        thread.selected_sources.set([self.source])
        from workbench.chat import create_chat_run
        create_chat_run(thread, "What year?", scope=self.resolved_project_scope())
        self.client.force_login(self.user)
        response = self.client.get(reverse("chat_thread", args=[thread.pk]))
        self.assertContains(response, "data-scope-editor")
        self.assertContains(response, 'name="scope_inherit"')
        self.assertContains(response, 'data-scope-mode')
        # The editor pre-fills the inherited attachment so the summary matches
        # what will actually be sent.
        self.assertContains(response, f'value="{self.source.pk}"')

    def test_followup_inherits_previous_run_scope(self):
        from workbench.chat import create_chat_run
        thread = ChatThread.objects.create(project=self.project, created_by=self.user)
        scope_a = self.resolved_project_scope()
        create_chat_run(thread, "first", scope=scope_a)
        second = create_chat_run(thread, "second")
        self.assertEqual(second.scope_snapshot, scope_a)
        self.assertEqual(second.scope_snapshot["mode"], "project")

    def test_followup_changed_scope_leaves_previous_run_unchanged(self):
        from workbench.chat import create_chat_run
        from workbench.policy import ProjectAccessPolicy
        thread = ChatThread.objects.create(project=self.project, created_by=self.user)
        scope_a = self.resolved_project_scope()
        first = create_chat_run(thread, "first", scope=scope_a)
        scope_b = ProjectAccessPolicy(user=self.user).resolve_chat_scope(mode="all")
        second = create_chat_run(thread, "second", scope=scope_b)
        first.refresh_from_db()
        self.assertEqual(first.scope_snapshot["mode"], "project")
        self.assertEqual(second.scope_snapshot, scope_b)
        self.assertNotEqual(second.scope_snapshot, first.scope_snapshot)

    def test_followup_prefers_run_snapshot_over_legacy_thread_fields(self):
        from workbench.chat import create_chat_run
        from workbench.policy import ProjectAccessPolicy
        thread = ChatThread.objects.create(project=self.project, created_by=self.user)
        thread.selected_sources.set([self.source])  # legacy fields diverge from the run scope
        scope_all = ProjectAccessPolicy(user=self.user).resolve_chat_scope(mode="all")
        create_chat_run(thread, "first", scope=scope_all)
        second = create_chat_run(thread, "second")  # no scope -> inherit run snapshot, not legacy
        self.assertEqual(second.scope_snapshot, scope_all)

    def test_run_scope_snapshot_is_immutable_to_later_thread_changes(self):
        from workbench.chat import create_chat_run
        thread = ChatThread.objects.create(project=self.project, created_by=self.user)
        scope_a = self.resolved_project_scope()
        first = create_chat_run(thread, "first", scope=scope_a)
        thread.scope_mode = "all"
        thread.scope_config = {"mode": "all"}
        thread.save(update_fields=["scope_mode", "scope_config"])
        first.refresh_from_db()
        self.assertEqual(first.scope_snapshot, scope_a)

    def test_evidence_persists_after_run(self):
        from workbench.chat import create_chat_run
        from workbench.models import EvidenceItem
        from workbench.retrieval import DeterministicLexicalRetriever
        thread = ChatThread.objects.create(project=self.project, created_by=self.user)
        run = create_chat_run(thread, "evidence persistence", scope=self.resolved_project_scope())
        hits = DeterministicLexicalRetriever().attached_context(
            source_ids=[self.source.pk], project_ids=[self.project.pk],
        )
        self.assertEqual(len(hits), 1)
        passage = hits[0].passage
        EvidenceItem.objects.create(run=run, marker="S1", source_document=passage.source_document,
                                    processed_revision=passage.processed_revision, processing_job=passage.processing_job,
                                    page=passage.page, page_region=passage.page_region, passage=passage,
                                    text=passage.text, page_text=passage.text, retrieval_method=hits[0].method,
                                    selection_reason=hits[0].reason, ordinal=1)
        self.assertEqual(run.evidence_items.count(), 1)
        self.assertEqual(run.evidence_items.first().marker, "S1")

    def test_invalid_citation_marker_fails_run(self):
        from unittest.mock import Mock, patch
        from workbench.chat import create_chat_run, process_chat_run, ChatProviderError
        thread = ChatThread.objects.create(project=self.project, created_by=self.user)
        run = create_chat_run(thread, "invalid citation", scope=self.resolved_project_scope())
        with patch("workbench.chat.requests.post") as post, self.settings(DSW_CHAT_TOOL_MODE="native"):
            post.return_value = Mock(status_code=200, json=lambda: {"choices": [{"message": {"content": "Claim. [S9]"}}]})
            with self.assertRaises(ChatProviderError):
                process_chat_run(run)
        self.assertTrue(run.events.filter(name="citation_rejected").exists())

    def test_resolve_followup_scope_rejects_inaccessible_project(self):
        from workbench.chat import resolve_followup_scope
        from workbench.policy import ProjectAccessPolicy
        other_owner = User.objects.create_user("other-owner", password="pass")
        other = Collection.objects.create(name="Hidden", created_by=other_owner)
        thread = ChatThread.objects.create(project=self.project, created_by=self.user)
        with self.assertRaises(PermissionError):
            resolve_followup_scope(ProjectAccessPolicy(user=self.user), thread, scope={
                "mode": "project", "project_id": other.pk, "attachment_ids": [],
            })

    def test_resolve_followup_scope_rejects_inaccessible_source(self):
        from workbench.chat import resolve_followup_scope
        from workbench.policy import ProjectAccessPolicy
        other_owner = User.objects.create_user("other-owner-2", password="pass")
        other = Collection.objects.create(name="Other", created_by=other_owner)
        hidden_source = SourceDocument.objects.create(collection=other, filename="hidden.pdf", uploaded_by=other_owner)
        thread = ChatThread.objects.create(project=self.project, created_by=self.user)
        with self.assertRaises(PermissionError):
            resolve_followup_scope(ProjectAccessPolicy(user=self.user), thread, scope={
                "mode": "project", "project_id": self.project.pk, "attachment_ids": [hidden_source.pk],
            })

    def test_webui_followup_inherits_previous_scope(self):
        from workbench.chat import create_chat_run
        thread = ChatThread.objects.create(project=self.project, created_by=self.user)
        run1 = create_chat_run(thread, "first", scope=self.resolved_project_scope())
        self.client.force_login(self.user)
        response = self.client.post(reverse("chat_thread", args=[thread.pk]), {"question": "follow", "scope_inherit": "1"})
        self.assertEqual(response.status_code, 302)
        run2 = self.newest_run(thread)
        self.assertEqual(run1.scope_snapshot, run2.scope_snapshot)

    def test_webui_followup_changed_scope_is_frozen(self):
        from workbench.chat import create_chat_run
        thread = ChatThread.objects.create(project=self.project, created_by=self.user)
        run1 = create_chat_run(thread, "first", scope=self.resolved_project_scope())
        self.client.force_login(self.user)
        response = self.client.post(reverse("chat_thread", args=[thread.pk]), {
            "question": "follow all", "scope_inherit": "0", "scope_mode": "all", "project_id": "",
        })
        self.assertEqual(response.status_code, 302)
        run2 = self.newest_run(thread)
        run1.refresh_from_db()
        self.assertEqual(run2.scope_snapshot["mode"], "all")
        self.assertEqual(run1.scope_snapshot["mode"], "project")  # previous run untouched

    def test_webui_followup_rejects_inaccessible_scope(self):
        thread = ChatThread.objects.create(project=self.project, created_by=self.user)
        other_owner = User.objects.create_user("hidden-owner", password="pass")
        other = Collection.objects.create(name="Hidden", created_by=other_owner)
        self.client.force_login(self.user)
        before = thread.runs.count()
        response = self.client.post(reverse("chat_thread", args=[thread.pk]), {
            "question": "q", "scope_inherit": "0", "scope_mode": "project", "project_id": str(other.pk),
        })
        self.assertEqual(response.status_code, 200)  # re-render with error, no new run
        self.assertContains(response, "not accessible")
        self.assertEqual(thread.runs.count(), before)

    def make_evidence(self, *, thread, source=None, marker="S1"):
        source = source or self.source
        message = ChatMessage.objects.create(thread=thread, role="user", text="Find the source", ordinal=0)
        run = ChatRun.objects.create(thread=thread, user_message=message, state="completed")
        return EvidenceItem.objects.create(
            run=run, marker=marker, source_document=source, processed_revision=self.document,
            processing_job=source.processing_jobs.first(), page=self.document.pages.first(),
            text="A matching passage", page_text="A full page", selection_reason="fixture",
        )

    def test_evidence_detail_requires_private_thread_owner(self):
        thread = ChatThread.objects.create(project=self.project, created_by=self.user)
        item = self.make_evidence(thread=thread)
        other = User.objects.create_user("same-project-reader", password="pass")
        ProjectMembership.objects.create(project=self.project, user=other, role="viewer")
        self.client.force_login(other)
        response = self.client.get(reverse("chat_evidence_detail", args=[item.run_id, item.marker]))
        self.assertEqual(response.status_code, 403)

    def test_evidence_detail_checks_actual_source_for_multi_project_scope(self):
        other_project = Collection.objects.create(name="Other archive", created_by=self.user)
        ProjectMembership.objects.create(project=other_project, user=self.user, role="owner")
        other_source = SourceDocument.objects.create(collection=other_project, filename="other.pdf", uploaded_by=self.user)
        other_job = ProcessingJob.objects.create(source_document=other_source, preset=ProcessingPreset.objects.get(slug="chat"), state="completed")
        other_revision = Document.objects.create(collection=other_project, external_id="other", filename="other.pdf")
        other_job.result_document = other_revision
        other_job.save(update_fields=["result_document"])
        other_page = Page.objects.create(document=other_revision, page_number=1)
        thread = ChatThread.objects.create(
            project=None, created_by=self.user, scope_snapshot={"project_ids": [self.project.pk, other_project.pk]},
        )
        message = ChatMessage.objects.create(thread=thread, role="user", text="Find the source", ordinal=0)
        run = ChatRun.objects.create(thread=thread, user_message=message, state="completed", scope_snapshot=thread.scope_snapshot)
        item = EvidenceItem.objects.create(
            run=run, marker="S4", source_document=other_source, processed_revision=other_revision,
            processing_job=other_job, page=other_page, text="Private other project passage", page_text="Private other page",
        )
        limited = User.objects.create_user("project-a-reader", password="pass")
        ProjectMembership.objects.create(project=self.project, user=limited, role="viewer")
        self.client.force_login(limited)
        response = self.client.get(reverse("chat_evidence_detail", args=[run.pk, item.marker]))
        self.assertEqual(response.status_code, 403)

    def test_evidence_detail_allows_owner_and_admin_when_source_is_accessible(self):
        thread = ChatThread.objects.create(project=self.project, created_by=self.user)
        item = self.make_evidence(thread=thread)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("chat_evidence_detail", args=[item.run_id, item.marker])).status_code, 200)
        admin = User.objects.create_superuser("chat-admin", email="admin@example.test", password="pass")
        self.client.force_login(admin)
        self.assertEqual(self.client.get(reverse("chat_evidence_detail", args=[item.run_id, item.marker])).status_code, 200)
