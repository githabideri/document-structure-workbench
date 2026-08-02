"""
Docling server processor — calls Docling via HTTP API.

Configuration:
    DSW_DOCLING_SERVER_URL: http://localhost:6000 (Docling serve endpoint)

This processor submits PDFs to a Docling server, polls for completion,
and collects the structured results.
"""
import json
import logging
import time
from pathlib import Path
from typing import Optional

import requests
from django.conf import settings
from django.core.exceptions import ValidationError

from .base import DocumentProcessor, ProcessorResult

logger = logging.getLogger(__name__)


class DoclingServeProcessor(DocumentProcessor):
    """Process documents via Docling HTTP server."""

    DEFAULT_TIMEOUT = 3600  # 1 hour max
    POLL_INTERVAL = 5  # seconds between status checks

    def __init__(self, server_url: Optional[str] = None):
        self.server_url = (
            server_url
            or getattr(settings, "DSW_DOCLING_SERVER_URL", "http://localhost:6000")
        ).rstrip("/")

    def submit(self, source_document, configuration: dict) -> str:
        """
        Submit a PDF to Docling server.

        Args:
            source_document: SourceDocument model instance.
            configuration: Processing preset snapshot dict.

        Returns:
            external_job_id: Docling job ID.
        """
        file_path = Path(getattr(settings, "ARTIFACTS_BASE_DIR", "/var/lib/dsw/artifacts")) / source_document.file_path

        if not file_path.exists():
            raise FileNotFoundError(f"Source file not found: {file_path}")

        # Build Docling request
        profiles = []
        if configuration.get("profile_a_enabled", True):
            profiles.append("standard-docling")
        if configuration.get("profile_b_enabled", False):
            profiles.append("granite-table-crop")

        payload = {
            "file_path": str(file_path),
            "profiles": profiles,
            "generate_crops": configuration.get("generate_crops", True),
            "output_format": "json",
        }

        logger.info("Submitting %s to Docling server %s", source_document.filename, self.server_url)

        try:
            resp = requests.post(
                f"{self.server_url}/v1/extract",
                json=payload,
                timeout=30,  # Connection timeout, not processing timeout
            )
            resp.raise_for_status()
            result = resp.json()
            job_id = result.get("job_id") or result.get("id")
            if not job_id:
                raise ValueError(f"No job ID in response: {result}")
            logger.info("Docling job submitted: %s", job_id)
            return job_id
        except requests.ConnectionError:
            logger.error("Cannot connect to Docling server at %s", self.server_url)
            raise ConnectionError(f"Docling server unreachable: {self.server_url}")
        except requests.RequestException as e:
            logger.error("Docling submission failed: %s", e)
            raise

    def get_status(self, external_job_id: str) -> dict:
        """Poll Docling server for job status."""
        try:
            resp = requests.get(
                f"{self.server_url}/v1/jobs/{external_job_id}",
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "state": data.get("status", "unknown"),
                "progress": data.get("progress", 0),
                "error": data.get("error"),
            }
        except requests.RequestException as e:
            logger.error("Status check failed: %s", e)
            return {"state": "error", "progress": 0, "error": str(e)}

    def collect_results(self, external_job_id: str) -> ProcessorResult:
        """Collect and parse Docling results."""
        try:
            resp = requests.get(
                f"{self.server_url}/v1/jobs/{external_job_id}/results",
                timeout=60,
            )
            resp.raise_for_status()
            results = resp.json()
        except requests.RequestException as e:
            raise RuntimeError(f"Failed to collect results: {e}")

        return self._parse_results(results)

    def cancel(self, external_job_id: str) -> bool:
        """Cancel a Docling job."""
        try:
            resp = requests.post(
                f"{self.server_url}/v1/jobs/{external_job_id}/cancel",
                timeout=10,
            )
            resp.raise_for_status()
            return True
        except requests.RequestException as e:
            logger.error("Cancel failed: %s", e)
            return False

    def _parse_results(self, results: dict) -> ProcessorResult:
        """Parse Docling JSON results into ProcessorResult."""
        result = ProcessorResult()

        pages = results.get("pages", [])
        result.pages_processed = len(pages)

        for page_data in pages:
            page_num = page_data.get("page_number", 0)

            # Page text
            text_elements = page_data.get("text_elements", [])
            page_text = "\n".join(
                el.get("text", "") for el in text_elements if el.get("text")
            )
            if page_text:
                result.page_texts[page_num] = page_text

            # Regions
            for region in page_data.get("regions", []):
                result.regions.append({
                    "page_number": page_num,
                    "region_type": region.get("type", "text"),
                    "bbox": region.get("bbox", [0, 0, 0, 0]),
                    "text": region.get("text", ""),
                    "confidence": region.get("confidence"),
                    "metadata": region.get("properties", {}),
                })

            # Tables
            for table in page_data.get("tables", []):
                result.tables_found += 1
                table_id = table.get("id", f"page_{page_num}_table_{result.tables_found}")

                # Table extraction
                result.table_extractions[table_id] = {
                    "page_number": page_num,
                    "html": table.get("html", ""),
                    "otsl": table.get("otsl", ""),
                    "bbox": table.get("bbox", [0, 0, 0, 0]),
                    "rows": table.get("rows", 0),
                    "columns": table.get("columns", 0),
                    "confidence": table.get("confidence"),
                }

        # Layout JSON
        if results.get("layout_json"):
            result.layout_json = results["layout_json"]

        # Processor metadata
        result.processor_metadata = {
            "processor": "docling",
            "version": results.get("metadata", {}).get("docling_version", "unknown"),
            "profiles": results.get("metadata", {}).get("profiles", []),
        }

        return result
