"""Wire-level chat tests against a real local OpenAI-compatible HTTP server.

These deliberately do not patch requests or the provider adapter. They exercise
the same HTTP boundary used by the worker and the deployed service.
"""
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from workbench.chat import ChatProviderError, create_chat_run, process_chat_run
from workbench.models import (
    ChatThread, Collection, Document, Page, ProcessingJob, ProcessingPreset,
    ProjectMembership, SearchPassage, SourceDocument, ApiToken,
)

User = get_user_model()


class FakeProviderHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    config = None

    def log_message(self, *_args):
        return

    def do_POST(self):  # noqa: N802
        config = type(self).config
        length = int(self.headers.get("Content-Length", "0"))
        config["request"] = json.loads(self.rfile.read(length) or b"{}")
        config["authorization"] = self.headers.get("Authorization")
        config["request_count"] = config.get("request_count", 0) + 1
        mode = config["mode"]
        if mode == "timeout":
            time.sleep(0.3)
            return
        if mode == "reset":
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
            return
        if mode == "reject-tools" and config["request"].get("tools"):
            self._send(400, {"error": {"message": "fake provider does not support tools"}})
            return
        if mode in {"bad-request", "unauthorized", "unprocessable", "unavailable-model"}:
            status = {"bad-request": 400, "unauthorized": 401, "unprocessable": 422, "unavailable-model": 404}[mode]
            self._send(status, {"error": {"message": f"fake {mode}"}})
            return
        if mode == "malformed-json":
            raw = b"not-json"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if mode == "native-tool" and config["request"].get("tools") and config["request_count"] == 1:
            message = {"content": None, "tool_calls": [{"id": "call-1", "type": "function",
                        "function": {"name": "search_evidence", "arguments": '{"query":"alpha"}'}}]}
            self._send(200, {"model": "fake-qwen", "choices": [{"finish_reason": "tool_calls", "message": message}]})
            return
        if mode == "malformed-tool" and config["request"].get("tools") and config["request_count"] == 1:
            message = {"content": None, "tool_calls": [{"id": "call-bad", "type": "function",
                        "function": {"name": "search_evidence", "arguments": "not-json"}}]}
            self._send(200, {"model": "fake-qwen", "choices": [{"finish_reason": "tool_calls", "message": message}]})
            return
        if mode == "reasoning-only":
            message = {"content": "", "reasoning_content": "untrusted fake reasoning"}
            finish_reason = "stop"
        elif mode == "empty-final":
            message = {"content": ""}
            finish_reason = "stop"
        else:
            message = {"content": "Alpha is documented here. [S1]"}
            finish_reason = "length" if mode == "finish-length" else "stop"
        self._send(200, {"model": "fake-qwen", "choices": [{"finish_reason": finish_reason, "message": message}],
                         "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19}})

    def _send(self, status, payload):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class FakeProviderServer:
    def __init__(self):
        self.config = {"mode": "success", "request": None, "authorization": None}
        FakeProviderHandler.config = self.config
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeProviderHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@override_settings(DSW_CHAT_MODEL="fake-qwen", DSW_CHAT_MAX_TOKENS=16384, DSW_CHAT_API_KEY="fake-secret")
class FakeProviderIntegrationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("fake-provider-user", password="pass")
        self.project = Collection.objects.create(name="Generic fake project", created_by=self.user)
        ProjectMembership.objects.create(project=self.project, user=self.user, role="owner")
        self.source = SourceDocument.objects.create(collection=self.project, filename="source-a.pdf", uploaded_by=self.user)
        preset = ProcessingPreset.objects.create(slug="fake-provider", name="Fake provider")
        job = ProcessingJob.objects.create(source_document=self.source, preset=preset, state="completed")
        document = Document.objects.create(collection=self.project, external_id="source-a", filename="source-a.pdf")
        job.result_document = document
        job.save(update_fields=["result_document"])
        self.source.active_document = document
        self.source.save(update_fields=["active_document"])
        page = Page.objects.create(document=document, page_number=1)
        SearchPassage.objects.create(
            project=self.project, source_document=self.source, processed_revision=document,
            processing_job=job, page=page, text="Alpha evidence is present.", normalized_text="alpha evidence is present.",
        )
        self.raw_token = "fake-api-token"
        self.token = ApiToken.objects.create(
            user=self.user, name="fake integration", token_prefix="fake-api",
            token_hash=ApiToken.hash_token(self.raw_token),
            scopes=["chat:read", "chat:write", "chat:retry", "support:read", "support:export"],
        )
        self.provider = FakeProviderServer().start()

    def tearDown(self):
        self.provider.stop()

    def run_mode(self, mode, timeout=2, tool_mode="fallback"):
        self.provider.config["mode"] = mode
        self.provider.config["request_count"] = 0
        thread = ChatThread.objects.create(project=self.project, created_by=self.user)
        thread.selected_sources.set([self.source])
        run = create_chat_run(thread, "alpha")
        with self.settings(DSW_CHAT_BASE_URL=self.provider.url, DSW_CHAT_TIMEOUT=timeout,
                           DSW_CHAT_TOOL_MODE=tool_mode):
            try:
                process_chat_run(run, worker_id="fake-integration-worker")
            except ChatProviderError as exc:
                return run, exc.code
        return run, None

    def test_success_uses_real_http_and_persists_answer_metadata(self):
        run, error = self.run_mode("success")
        self.assertIsNone(error)
        run.refresh_from_db()
        self.assertEqual(run.state, "completed")
        self.assertEqual(run.assistant_message.text, "Alpha is documented here. [S1]")
        self.assertEqual(self.provider.config["authorization"], "Bearer fake-secret")
        self.assertEqual(self.provider.config["request"]["model"], "fake-qwen")
        self.assertEqual(run.model_metadata["provider"]["finish_reason"], "stop")
        self.assertEqual(run.model_metadata["provider"]["usage"]["total_tokens"], 19)

    def test_api_to_worker_to_api_uses_real_provider_boundary(self):
        self.provider.config["mode"] = "success"
        with self.settings(DSW_CHAT_BASE_URL=self.provider.url, DSW_CHAT_TIMEOUT=2):
            response = self.client.post(
                reverse("api_chat_threads"),
                data={"project_id": self.project.pk, "source_ids": [self.source.pk], "question": "alpha"},
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {self.raw_token}",
            )
            self.assertEqual(response.status_code, 201)
            created = response.json()
            run = created["run"]["id"]
            queued_run = self._get_run(run)
            process_chat_run(queued_run, worker_id="fake-api-worker")
            inspected = self.client.get(
                reverse("api_chat_run_detail", args=[run]),
                HTTP_AUTHORIZATION=f"Bearer {self.raw_token}",
            )
        self.assertEqual(inspected.status_code, 200)
        self.assertEqual(inspected.json()["run"]["state"], "completed")
        self.assertEqual(inspected.json()["run"]["answer"], "Alpha is documented here. [S1]")

    @staticmethod
    def _get_run(run_id):
        from workbench.models import ChatRun
        return ChatRun.objects.get(pk=run_id)

    def test_finish_reason_length_with_final_content_is_completed(self):
        run, error = self.run_mode("finish-length")
        self.assertIsNone(error)
        run.refresh_from_db()
        self.assertEqual(run.state, "completed")
        self.assertEqual(run.model_metadata["provider"]["finish_reason"], "length")

    def test_reasoning_only_and_empty_final_are_distinct_no_answer_failures(self):
        for mode in ("reasoning-only", "empty-final"):
            with self.subTest(mode=mode):
                run, error = self.run_mode(mode)
                self.assertEqual(error, "provider_no_final_answer")
                run.refresh_from_db()
                self.assertEqual(run.model_metadata["provider"]["response_shape"]["has_content"], False)

    def test_http_rejections_and_unavailable_model_are_provider_rejected(self):
        for mode in ("bad-request", "unauthorized", "unprocessable", "unavailable-model"):
            with self.subTest(mode=mode):
                _run, error = self.run_mode(mode)
                self.assertEqual(error, "provider_rejected")

    def test_automatic_mode_retries_without_tools_when_provider_rejects_them(self):
        run, error = self.run_mode("reject-tools", tool_mode="automatic")
        self.assertIsNone(error)
        run.refresh_from_db()
        self.assertEqual(run.state, "completed")
        self.assertTrue(run.events.filter(name="tool_fallback").exists())
        self.assertEqual(self.provider.config["request_count"], 2)

    def test_native_tool_calls_persist_bounded_search_evidence(self):
        run, error = self.run_mode("native-tool", tool_mode="native")
        self.assertIsNone(error)
        run.refresh_from_db()
        self.assertEqual(run.state, "completed")
        self.assertEqual(run.evidence_items.count(), 1)
        self.assertTrue(run.events.filter(name="tool_call").exists())

    def test_malformed_tool_call_is_untrusted_and_does_not_crash_worker(self):
        run, error = self.run_mode("malformed-tool", tool_mode="native")
        self.assertIsNone(error)
        run.refresh_from_db()
        self.assertEqual(run.state, "completed")
        self.assertTrue(run.events.filter(error_code="provider_malformed_tool_call").exists())

    def test_timeout_connection_reset_and_malformed_json_are_classified(self):
        _run, error = self.run_mode("timeout", timeout=0.05)
        self.assertEqual(error, "worker_timeout")
        _run, error = self.run_mode("reset")
        self.assertEqual(error, "provider_unreachable")
        _run, error = self.run_mode("malformed-json")
        self.assertEqual(error, "provider_protocol_error")
