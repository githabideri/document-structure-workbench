"""
Processing worker — claims queued jobs, runs Docling, imports results.

Usage:
    python manage.py run_processing_worker

This worker:
    - Polls for queued jobs every 5 seconds
    - Claims a job atomically (state: queued -> submitting)
    - Submits to Docling server
    - Polls Docling for completion
    - Imports results into database
    - Handles failures gracefully (one failed job doesn't crash the loop)
"""
import logging
import signal
import sys
import time

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from workbench.models import ProcessingJob
from workbench.processors.docling_serve import DoclingServeProcessor
from workbench.processors.importer import ResultImporter

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the document processing worker"

    def add_arguments(self, parser):
        parser.add_argument(
            "--poll-interval",
            type=int,
            default=5,
            help="Seconds between job queue polls (default: 5)",
        )
        parser.add_argument(
            "--docling-url",
            type=str,
            default=None,
            help="Docling server URL (overrides DSW_DOCLING_SERVER_URL)",
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="Process one job and exit (for testing)",
        )

    def handle(self, *args, **options):
        self.poll_interval = options["poll_interval"]
        self.once = options["once"]
        self.running = True

        # Setup signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        # Initialize processor
        self.processor = DoclingServeProcessor(
            server_url=options["docling_url"],
        )

        self.stdout.write(self.style.SUCCESS(
            f"Processing worker started (poll: {self.poll_interval}s, "
            f"docling: {self.processor.server_url})"
        ))

        try:
            while self.running:
                try:
                    job = self._claim_next_job()
                    if job:
                        self._process_job(job)
                        if self.once:
                            break
                    else:
                        time.sleep(self.poll_interval)
                except Exception as e:
                    logger.exception("Worker loop error: %s", e)
                    time.sleep(self.poll_interval)
        finally:
            self.stdout.write(self.style.WARNING("Worker shutting down."))

    def _handle_signal(self, signum, frame):
        self.stdout.write(self.style.WARNING(
            f"Received signal {signum}, shutting down..."
        ))
        self.running = False

    def _claim_next_job(self) -> ProcessingJob:
        """Atomically claim the next queued job."""
        try:
            with transaction.atomic():
                job = (
                    ProcessingJob.objects
                    .filter(state="queued")
                    .order_by("created_at")
                    .first()
                )
                if not job:
                    return None

                # Atomic state transition
                job.transition_to("submitting")
                job.processor = "docling"
                job.save(update_fields=["state", "processor", "started_at"])

            logger.info("Claimed job %d: %s", job.pk, job.source_document.filename)
            return job
        except Exception as e:
            logger.error("Failed to claim job: %s", e)
            return None

    def _process_job(self, job: ProcessingJob):
        """Process a single job end-to-end."""
        logger.info("Processing job %d: %s", job.pk, job.source_document.filename)

        try:
            # 1. Submit to Docling
            job.transition_to("submitting")
            external_job_id = self.processor.submit(
                job.source_document,
                job.preset_snapshot or {},
            )
            job.external_job_id = external_job_id
            job.save(update_fields=["external_job_id"])

            # 2. Poll for completion
            job.transition_to("processing")
            self._wait_for_completion(job, external_job_id)

            # 3. Collect results
            job.transition_to("importing")
            result = self.processor.collect_results(external_job_id)

            # 4. Import into database
            importer = ResultImporter(job)
            counts = importer.import_results(result)

            # 5. Mark complete
            if result.error_summary:
                job.transition_to("partial")
                job.error_message = result.error_summary
            else:
                job.transition_to("completed")

            job.save(update_fields=["error_message"])

            logger.info(
                "Job %d completed: %s (pages=%d, tables=%d)",
                job.pk, job.state, counts.get("pages", 0), counts.get("tables", 0),
            )

        except FileNotFoundError as e:
            self._fail_job(job, f"Source file not found: {e}")
        except ConnectionError as e:
            self._fail_job(job, f"Docling server unreachable: {e}")
        except Exception as e:
            logger.exception("Job %d failed: %s", job.pk, e)
            self._fail_job(job, str(e))

    def _wait_for_completion(self, job, external_job_id, timeout=3600):
        """Poll Docling server until job completes or times out."""
        start = time.time()
        while time.time() - start < timeout:
            status = self.processor.get_status(external_job_id)

            if status["state"] in ("completed", "success"):
                return
            elif status["state"] in ("failed", "error"):
                raise RuntimeError(
                    f"Docling job failed: {status.get('error', 'unknown error')}"
                )
            elif status["state"] == "cancelled":
                raise RuntimeError("Docling job was cancelled")

            # Log progress
            progress = status.get("progress", 0)
            if isinstance(progress, (int, float)) and progress > 0:
                logger.info("Job %d: %.0f%%", job.pk, progress)

            time.sleep(5)

        raise TimeoutError(
            f"Docling job timed out after {timeout}s"
        )

    def _fail_job(self, job, error_message):
        """Mark a job as failed."""
        try:
            job.transition_to("failed")
        except Exception:
            # Already in a terminal state
            pass
        job.error_message = error_message[:500]  # Truncate
        job.finished_at = timezone.now()
        job.save(update_fields=["state", "error_message", "finished_at"])
        logger.error("Job %d failed: %s", job.pk, error_message)
