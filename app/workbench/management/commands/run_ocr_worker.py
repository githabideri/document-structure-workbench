"""Process queued visual OCR candidates one at a time."""
import logging
import os
import signal
import socket
import time
import uuid

from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.utils import timezone

from workbench.models import OcrRequest
from workbench.processors.vision_ocr import VisionOcrClient, VisionOcrError, make_crop, request_metadata

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the serialized visual OCR worker"

    def add_arguments(self, parser):
        parser.add_argument("--poll-interval", type=int, default=5)
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        self.running = True
        self.once = options["once"]
        self.poll_interval = options["poll_interval"]
        self.worker_id = f"ocr:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        signal.signal(signal.SIGINT, self._stop)
        signal.signal(signal.SIGTERM, self._stop)
        self.stdout.write(self.style.SUCCESS(f"OCR worker {self.worker_id} started"))
        while self.running:
            request = self._claim()
            if request:
                self._process(request)
                if self.once:
                    break
            elif self.once:
                break
            else:
                time.sleep(self.poll_interval)

    def _stop(self, signum, frame):
        self.running = False

    def _claim(self):
        with transaction.atomic():
            options = {}
            if connection.features.has_select_for_update_skip_locked:
                options["skip_locked"] = True
            request = OcrRequest.objects.select_for_update(**options).filter(state="queued").order_by("created_at").first()
            if not request:
                return None
            request.state = "processing"
            request.started_at = timezone.now()
            request.metadata = {**(request.metadata or {}), "worker_id": self.worker_id}
            request.save(update_fields=["state", "started_at", "metadata"])
            return request

    def _process(self, request):
        try:
            image, image_info = make_crop(request.page, request.region)
            request.input_sha256 = request_metadata(image, image_info)["sha256"]
            request.input_metadata = request_metadata(image, image_info)
            text, raw = VisionOcrClient().transcribe(image, request.prompt)
            request.candidate_text = text
            request.raw_response = raw if isinstance(raw, dict) else {"response": raw}
            request.state = "completed"
            request.error_message = ""
        except Exception as exc:
            logger.exception("OCR request %s failed", request.pk)
            request.state = "failed"
            request.error_message = str(exc)[:2000]
        request.finished_at = timezone.now()
        request.save(update_fields=[
            "input_sha256", "input_metadata", "candidate_text", "raw_response",
            "state", "error_message", "finished_at",
        ])
