"""Restart-safe processing worker for queued and recoverable Docling jobs."""
import logging
import os
import signal
import socket
import time
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from workbench.models import ChatRun, ProcessingJob, SourceDocument
from workbench.chat import process_chat_run
from workbench.processors.docling_serve import DoclingServeProcessor
from workbench.processors.importer import ImportError as ImporterError
from workbench.processors.importer import ResultImporter

logger = logging.getLogger(__name__)


class ProcessingInterrupted(RuntimeError):
    """The remote task is known, but this worker could not safely continue."""


class Command(BaseCommand):
    help = "Run the restart-safe document processing worker"

    def add_arguments(self, parser):
        parser.add_argument("--poll-interval", type=int, default=5)
        parser.add_argument("--docling-url", type=str, default=None)
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        self.poll_interval = options["poll_interval"]
        self.once = options["once"]
        self.running = True
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.processor = DoclingServeProcessor(server_url=options["docling_url"])
        configured_lease = getattr(settings, "DSW_PROCESSING_LEASE_SECONDS", 90)
        request_timeout = getattr(self.processor, "request_timeout", 60)
        self.lease_seconds = max(
            configured_lease,
            request_timeout + self.poll_interval + 10,
        )
        self.max_status_errors = getattr(settings, "DSW_PROCESSING_MAX_STATUS_ERRORS", 5)

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        self.stdout.write(self.style.SUCCESS(
            f"Processing worker {self.worker_id} started "
            f"(poll: {self.poll_interval}s, docling: {self.processor.server_url})"
        ))

        try:
            while self.running:
                try:
                    claimed = self._claim_next_job()
                    if claimed:
                        job, action = claimed
                        self._process_job(job, action)
                        if self.once:
                            break
                    else:
                        chat_run = self._claim_next_chat_run()
                        if chat_run:
                            try:
                                process_chat_run(chat_run, self.worker_id)
                            except Exception as exc:
                                logger.exception("Chat run %d failed: %s", chat_run.pk, exc)
                                ChatRun.objects.filter(pk=chat_run.pk).update(
                                    state="failed", error_message=str(exc)[:1000],
                                    status_message="The chat run failed. Retry it from the conversation.",
                                    finished_at=timezone.now(), worker_id="",
                                )
                        elif self.once:
                            break
                        else:
                            time.sleep(self.poll_interval)
                except Exception as exc:
                    logger.exception("Worker loop error: %s", exc)
                    if self.once:
                        raise
                    time.sleep(self.poll_interval)
        finally:
            self.stdout.write(self.style.WARNING("Worker shutting down."))

    def _handle_signal(self, signum, frame):
        self.stdout.write(self.style.WARNING(f"Received signal {signum}, shutting down..."))
        self.running = False

    def _locked_first(self, queryset):
        options = {}
        if connection.features.has_select_for_update_skip_locked:
            options["skip_locked"] = True
        return queryset.select_for_update(**options).first()

    def _claim_next_job(self):
        """Claim a queued job or atomically adopt one whose worker lease expired."""
        now = timezone.now()
        with transaction.atomic():
            recoverable = ProcessingJob.objects.filter(
                state__in=["submitting", "processing", "importing"],
            ).filter(
                Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lt=now)
            ).order_by("created_at")
            job = self._locked_first(recoverable)

            if job:
                if not job.external_job_id:
                    if job.state == "submitting":
                        job.transition_to("submission_uncertain")
                        job.status_message = (
                            "The processor may have accepted this document, but no task "
                            "identifier was saved. Reconcile it before retrying."
                        )
                    else:
                        job.transition_to("interrupted")
                        job.status_message = (
                            f"Job state '{job.state}' requires a processor task identifier."
                        )
                    job.worker_id = ""
                    job.lease_expires_at = None
                    job.save(update_fields=[
                        "status_message", "worker_id", "lease_expires_at",
                    ])
                    logger.error("Marked job %d as %s: missing external task ID", job.pk, job.state)
                    return None

                action = "import" if job.state == "importing" else "poll"
                self._adopt(job, now, f"Resuming {job.state} after worker interruption.")
                return job, action

            queued = ProcessingJob.objects.filter(state="queued").order_by("created_at")
            job = self._locked_first(queued)
            if not job:
                return None
            job.transition_to("submitting")
            job.processor = "docling"
            self._adopt(job, now, "Submitting document to Docling.")
            job.save(update_fields=["processor"])
            logger.info("Claimed job %d: %s", job.pk, job.source_document.filename)
            return job, "submit"

    def _claim_next_chat_run(self):
        """Claim one queued chat run using the same worker process."""
        with transaction.atomic():
            run = self._locked_first(ChatRun.objects.filter(state="queued").order_by("created_at"))
            if not run:
                return None
            run.status_message = "Evidence worker claimed this run."
            run.worker_id = self.worker_id
            run.worker_heartbeat_at = timezone.now()
            run.save(update_fields=["status_message", "worker_id", "worker_heartbeat_at"])
            return run

    def _adopt(self, job, now=None, message=""):
        now = now or timezone.now()
        job.worker_id = self.worker_id
        job.worker_heartbeat_at = now
        job.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
        if message:
            job.status_message = message
        job.save(update_fields=[
            "worker_id", "worker_heartbeat_at", "lease_expires_at", "status_message",
        ])

    def _heartbeat(self, job, message=None):
        now = timezone.now()
        updated = ProcessingJob.objects.filter(
            pk=job.pk, worker_id=self.worker_id,
        ).update(
            worker_heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=self.lease_seconds),
            **({"status_message": message} if message else {}),
        )
        if not updated:
            raise ProcessingInterrupted("Worker lease was lost to another worker.")
        job.worker_heartbeat_at = now
        job.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
        if message:
            job.status_message = message

    def _process_job(self, job, action):
        logger.info("Processing job %d (%s): %s", job.pk, action, job.source_document.filename)
        try:
            if action == "submit":
                self._heartbeat(job, "Submitting document to Docling.")
                try:
                    task_id = self.processor.submit(job.source_document, job.preset_snapshot or {})
                except FileNotFoundError:
                    raise
                except Exception as exc:
                    # Once submission begins, absence of a task ID is ambiguous.
                    self._mark_submission_uncertain(job, str(exc))
                    return
                job.external_job_id = task_id
                job.save(update_fields=["external_job_id"])
                job.transition_to("processing")
                action = "poll"
            elif action == "poll" and job.state == "submitting":
                job.transition_to("processing")

            if action == "poll":
                self._wait_for_completion(job, job.external_job_id)
                job.transition_to("importing")

            self._heartbeat(job, "Saving extracted results.")
            result = self.processor.collect_results(job.external_job_id)
            self._heartbeat(job, "Importing pages, regions, and tables.")
            counts = ResultImporter(job).import_results(result)
            self._heartbeat(job, "Finalizing processing revision.")
            self._finalize(job, result, counts["document"])
            logger.info(
                "Job %d completed: %s (pages=%d, tables=%d)",
                job.pk, job.state, counts.get("pages", 0), counts.get("tables", 0),
            )
        except ProcessingInterrupted as exc:
            self._interrupt_job(job, str(exc))
        except FileNotFoundError as exc:
            self._fail_job(job, f"Source file not found: {exc}")
        except ConnectionError as exc:
            self._interrupt_job(job, f"Docling connection interrupted: {exc}")
        except ImporterError as exc:
            self._fail_job(job, f"Import error: {exc}")
        except Exception as exc:
            logger.exception("Job %d failed: %s", job.pk, exc)
            self._fail_job(job, str(exc))

    def _wait_for_completion(self, job, task_id, timeout=None):
        timeout = timeout or self.processor.job_timeout
        started = time.monotonic()
        consecutive_errors = job.consecutive_poll_errors

        while time.monotonic() - started < timeout:
            self._heartbeat(job, "Docling is analyzing the document.")
            attempted_at = timezone.now()
            ProcessingJob.objects.filter(pk=job.pk, worker_id=self.worker_id).update(
                remote_poll_attempted_at=attempted_at,
            )
            try:
                status = self.processor.get_status(task_id)
            except Exception as exc:
                consecutive_errors += 1
                ProcessingJob.objects.filter(pk=job.pk, worker_id=self.worker_id).update(
                    consecutive_poll_errors=consecutive_errors,
                    status_message=(
                        f"Processor status check failed ({consecutive_errors}/"
                        f"{self.max_status_errors}); retrying."
                    ),
                )
                if consecutive_errors >= self.max_status_errors:
                    raise ProcessingInterrupted(
                        f"Processor status could not be checked after "
                        f"{consecutive_errors} consecutive attempts: {exc}"
                    ) from exc
                time.sleep(self.poll_interval)
                continue

            state = status.get("state", "unknown")
            now = timezone.now()
            consecutive_errors = 0
            ProcessingJob.objects.filter(pk=job.pk, worker_id=self.worker_id).update(
                remote_response_at=now,
                remote_status=state,
                consecutive_poll_errors=0,
                status_message=f"Docling status: {state}.",
            )
            job.remote_response_at = now
            job.remote_status = state
            job.consecutive_poll_errors = 0

            if state in ("success", "completed"):
                return
            if state in ("failed", "error"):
                raise RuntimeError(f"Docling task failed: {status.get('error', 'unknown error')}")
            if state == "cancelled":
                raise RuntimeError("Docling task was cancelled")
            time.sleep(self.poll_interval)

        raise ProcessingInterrupted(f"Docling task timed out after {timeout}s")

    def _finalize(self, job, result, document):
        """Set terminal state and activate only a completely successful revision."""
        with transaction.atomic():
            locked_job = ProcessingJob.objects.select_for_update().get(pk=job.pk)
            source = SourceDocument.objects.select_for_update().get(pk=locked_job.source_document_id)
            if locked_job.worker_id != self.worker_id:
                raise ProcessingInterrupted("Worker lease was lost before finalization.")
            locked_job.result_document = document
            if result.error_summary:
                locked_job.transition_to("partial")
                locked_job.error_message = result.error_summary
                locked_job.status_message = "Processing completed with partial results."
            else:
                locked_job.transition_to("completed")
                locked_job.status_message = "Document is ready."
                source.active_document = document
                source.save(update_fields=["active_document"])
            locked_job.worker_id = ""
            locked_job.lease_expires_at = None
            locked_job.save(update_fields=[
                "result_document", "error_message", "status_message",
                "worker_id", "lease_expires_at",
            ])
            job.state = locked_job.state

    def _mark_submission_uncertain(self, job, error_message):
        try:
            job.transition_to("submission_uncertain")
        except Exception:
            pass
        job.status_message = (
            "Submission outcome is uncertain and will not be retried automatically. "
            f"Operator detail: {error_message[:300]}"
        )
        self._release(job)

    def _interrupt_job(self, job, error_message):
        try:
            job.transition_to("interrupted")
        except Exception:
            pass
        job.error_message = error_message[:500]
        job.status_message = error_message[:500]
        self._release(job)
        logger.error("Job %d interrupted: %s", job.pk, error_message)

    def _fail_job(self, job, error_message):
        try:
            job.transition_to("failed")
        except Exception:
            pass
        job.error_message = error_message[:500]
        job.status_message = error_message[:500]
        self._release(job)
        logger.error("Job %d failed: %s", job.pk, error_message)

    def _release(self, job):
        job.worker_id = ""
        job.lease_expires_at = None
        job.save(update_fields=[
            "state", "finished_at", "error_message", "status_message",
            "worker_id", "lease_expires_at",
        ])
