"""Process queued visual OCR candidates one at a time."""
import logging
import os
import signal
import socket
import time
import uuid

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.utils import timezone

from workbench.models import OcrRequest
from workbench.processors.htr import HtrClient, HtrError, HTR_PROVIDER, HTR_SCHEMA_VERSION
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
            if request.provider == HTR_PROVIDER:
                self._process_htr(request)
            else:
                self._process_vision(request)
        except Exception as exc:
            logger.exception("OCR request %s failed", request.pk)
            request.state = "failed"
            request.error_message = str(exc)[:2000]
            request.finished_at = timezone.now()
            request.save(update_fields=[
                "input_sha256", "input_metadata", "candidate_text", "raw_response",
                "metadata", "state", "error_message", "finished_at",
            ])
            return
        request.finished_at = timezone.now()
        request.save(update_fields=[
            "input_sha256", "input_metadata", "candidate_text", "raw_response",
            "metadata", "state", "error_message", "finished_at",
        ])

    def _process_vision(self, request):
        image, image_info = make_crop(request.page, request.region)
        meta = request_metadata(image, image_info)
        request.input_sha256 = meta["sha256"]
        request.input_metadata = meta
        text, raw = VisionOcrClient(provider=request.provider, model=request.model or None).transcribe(image, request.prompt)
        request.candidate_text = text
        request.raw_response = raw if isinstance(raw, dict) else {"response": raw}
        request.state = "completed"
        request.error_message = ""

    def _process_htr(self, request):
        region = request.region
        if region is None:
            raise HtrError("HTR requires a region; this request has none.")
        image, image_info = make_crop(request.page, region)
        meta = request_metadata(image, image_info)
        # Crop provenance in page-relative coordinates. make_crop crops exactly
        # to the region bbox (no padding yet); padding stays 0 so the line
        # overlay maps crop-relative boxes through actual_padded_bbox == page_bbox.
        page_bbox = [region.left, region.top, region.right, region.bottom]
        crop = {
            "page_bbox": page_bbox,
            "actual_padded_bbox": page_bbox,
            "padding": 0.0,
            "width": image_info["width"],
            "height": image_info["height"],
            "source_page_image": image_info.get("source_page_image", ""),
        }
        pipeline_id = (request.metadata or {}).get("pipeline_id", "")
        label = f"document-{request.document_id}-page-{request.page_id}-region-{region.id}"
        client = HtrClient()
        remote_id = client.submit_transcription(
            image, pipeline_id=pipeline_id, mode="region", label=label,
        )
        # Poll the remote service until terminal. The OCR worker is serialized,
        # so blocking here is acceptable for the MVP (one job at a time).
        deadline = time.monotonic() + client.timeout
        max_errors = getattr(settings, "DSW_PROCESSING_MAX_STATUS_ERRORS", 5)
        consecutive_errors = 0
        result = {"status": "pending"}
        while True:
            if time.monotonic() > deadline:
                raise HtrError(
                    f"HTR run {remote_id} did not finish within {client.timeout}s."
                )
            try:
                result = client.get_transcription(remote_id)
                consecutive_errors = 0
            except HtrError:
                consecutive_errors += 1
                if consecutive_errors >= max_errors:
                    raise
                time.sleep(client.poll_interval)
                continue
            status = result.get("status")
            if status == "succeeded":
                break
            if status == "failed":
                msg = (result.get("error") or {}).get("message") or "HTR run failed."
                raise HtrError(msg)
            time.sleep(client.poll_interval)
        meta["crop"] = crop
        meta["remote_id"] = remote_id
        request.input_sha256 = meta["sha256"]
        request.input_metadata = meta
        request.candidate_text = result.get("text", "")
        request.raw_response = result
        request.metadata = {
            **(request.metadata or {}),
            "remote_id": remote_id,
            "crop": crop,
            "schema_version": HTR_SCHEMA_VERSION,
        }
        request.state = "completed"
        request.error_message = ""
