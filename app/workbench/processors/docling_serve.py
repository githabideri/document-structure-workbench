"""
Docling server processor — calls Docling Serve v1 API via multipart upload.

Configuration:
    DSW_DOCLING_SERVER_URL: http://localhost:5001 (Docling serve endpoint)

This processor uploads PDFs to a Docling Serve instance, waits for completion,
and collects the structured results.

API reference: https://github.com/DS4SD/docling-serve/blob/main/docs/usage.md
- Submit: POST /v1/convert/file (multipart) or /v1/convert/source (JSON)
- Sync mode: waits for completion within MAX_SYNC_WAIT seconds
- Result: Docling document JSON with pages, tables, regions, images
"""
import base64
import json
import logging
import time
from pathlib import Path
from typing import Optional

import requests
from django.conf import settings

from .base import DocumentProcessor, ProcessorResult

logger = logging.getLogger(__name__)


class DoclingServeProcessor(DocumentProcessor):
    """Process documents via Docling Serve v1 API."""

    DEFAULT_TIMEOUT = 3600  # 1 hour max for sync mode
    POLL_INTERVAL = 5  # seconds between status checks

    def __init__(self, server_url: Optional[str] = None, api_key: Optional[str] = None):
        self.server_url = (
            server_url
            or getattr(settings, "DSW_DOCLING_SERVER_URL", "http://localhost:5001")
        ).rstrip("/")
        self.api_key = api_key or getattr(settings, "DSW_DOCLING_API_KEY", None)

    @property
    def _headers(self):
        headers = {}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        return headers

    def submit(self, source_document, configuration: dict) -> str:
        """
        Submit a PDF to Docling Serve via multipart upload.

        Uses POST /v1/convert/file with the actual PDF file (not a local path).
        This works across container/LXC boundaries without shared mounts.

        Args:
            source_document: SourceDocument model instance.
            configuration: Processing preset snapshot dict.

        Returns:
            external_job_id: Docling job ID (or "sync" for synchronous results).
        """
        file_path = Path(getattr(settings, "ARTIFACTS_BASE_DIR", "/var/lib/dsw/artifacts")) / source_document.file_path

        if not file_path.exists():
            raise FileNotFoundError(f"Source file not found: {file_path}")

        # Build conversion options from preset snapshot
        options = self._build_options(configuration)

        logger.info(
            "Submitting %s to Docling server %s (sync=%s)",
            source_document.filename,
            self.server_url,
            options.get("sync", True),
        )

        try:
            # Multipart upload — works across containers without shared mounts
            with open(file_path, "rb") as f:
                files = {
                    "file": (source_document.filename, f, "application/pdf"),
                }
                data = {
                    "options": json.dumps(options),
                }
                resp = requests.post(
                    f"{self.server_url}/v1/convert/file",
                    files=files,
                    data=data,
                    headers=self._headers,
                    timeout=30,  # Connection timeout
                )
                resp.raise_for_status()

            result = resp.json()

            # Check if response is synchronous (result included) or async (job ID)
            if "job_id" in result or "id" in result:
                job_id = result.get("job_id") or result.get("id")
                logger.info("Docling job submitted (async): %s", job_id)
                return job_id
            else:
                # Synchronous response — result is inline
                logger.info("Docling job completed synchronously")
                return self._parse_results(result)

        except requests.ConnectionError:
            logger.error("Cannot connect to Docling server at %s", self.server_url)
            raise ConnectionError(f"Docling server unreachable: {self.server_url}")
        except requests.RequestException as e:
            logger.error("Docling submission failed: %s", e)
            logger.error("Response: %s", resp.text if 'resp' in dir() else "no response")
            raise

    def get_status(self, external_job_id: str) -> dict:
        """Poll Docling server for job status."""
        try:
            resp = requests.get(
                f"{self.server_url}/v1/jobs/{external_job_id}",
                headers=self._headers,
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
        # If external_job_id is already a ProcessorResult (sync mode), return it
        if isinstance(external_job_id, ProcessorResult):
            return external_job_id

        try:
            resp = requests.get(
                f"{self.server_url}/v1/jobs/{external_job_id}/result",
                headers=self._headers,
                timeout=120,
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
                headers=self._headers,
                timeout=10,
            )
            resp.raise_for_status()
            return True
        except requests.RequestException as e:
            logger.error("Cancel failed: %s", e)
            return False

    def _build_options(self, configuration: dict) -> dict:
        """Build Docling conversion options from preset snapshot."""
        options = {
            "from_formats": ["pdf"],
            "to_formats": ["json"],
            "include_page_images": True,
            "images_scale": 2.0,
            "do_table_structure": True,
            "table_mode": "accurate",
            "do_ocr": True,
            "image_export_mode": "embedded",
            "sync": True,  # Wait for completion (within MAX_SYNC_WAIT)
        }

        # Override from configuration if present
        if configuration.get("table_mode"):
            options["table_mode"] = configuration["table_mode"]
        if configuration.get("do_ocr") is not None:
            options["do_ocr"] = configuration["do_ocr"]
        if configuration.get("include_page_images") is not None:
            options["include_page_images"] = configuration["include_page_images"]
        if configuration.get("images_scale"):
            options["images_scale"] = configuration["images_scale"]

        return options

    def _parse_results(self, results: dict) -> ProcessorResult:
        """
        Parse Docling JSON results into ProcessorResult.

        Docling Serve v1 returns a Docling document structure with:
        - pages: array of page objects
        - Each page: page_number, size (width/height), images, text elements, tables
        - Bounding boxes are normalized 0-1 range
        """
        result = ProcessorResult()

        # Handle different response formats
        # Format 1: {"pages": [...]}
        # Format 2: {"documents": [{"pages": [...]}]}
        # Format 3: Direct document with "pages" key

        pages = results.get("pages", [])
        if not pages and "documents" in results:
            # Nested document format
            pages = results["documents"][0].get("pages", []) if results["documents"] else []

        result.pages_processed = len(pages)

        for page_data in pages:
            page_num = page_data.get("page_number", 0)
            if not page_num:
                page_num = result.pages_processed  # Fallback

            # Page dimensions (in pixels at images_scale)
            page_size = page_data.get("size", {})
            page_width = page_size.get("width", 0)
            page_height = page_size.get("height", 0)

            # Page images
            page_images = page_data.get("images", [])
            if page_images:
                # First image is typically the page scan
                img_data = page_images[0]
                if isinstance(img_data, dict):
                    img_ref = img_data.get("ref", img_data.get("data", ""))
                    if img_ref:
                        # Store as relative path for later serving
                        result.page_images[page_num] = f"pages/{page_num}.png"
                elif isinstance(page_images[0], str):
                    result.page_images[page_num] = f"pages/{page_num}.png"

            # Page text — collect from all text elements
            text_parts = []
            # Docling uses various keys for text content
            for key in ("text_elements", "texts", "content", "children"):
                elements = page_data.get(key, [])
                for el in elements:
                    text = el.get("text", el.get("content", ""))
                    if text:
                        text_parts.append(text)

            page_text = "\n".join(text_parts) if text_parts else ""
            if page_text:
                # Store FULL text — no truncation
                result.page_texts[page_num] = page_text

            # Store page dimensions for bbox normalization
            if page_width and page_height:
                result.processor_metadata.setdefault("page_dimensions", {})[page_num] = {
                    "width": page_width,
                    "height": page_height,
                }

            # Regions
            for region in page_data.get("regions", []):
                region_type = region.get("type", "text")
                bbox = region.get("bbox", [0, 0, 0, 0])

                result.regions.append({
                    "page_number": page_num,
                    "region_type": region_type,
                    "bbox": bbox,
                    "text": region.get("text", region.get("content", "")),
                    "confidence": region.get("confidence"),
                    "metadata": region.get("properties", {}),
                })

            # Tables
            for table in page_data.get("tables", []):
                result.tables_found += 1
                table_id = table.get("id", f"page_{page_num}_table_{result.tables_found}")

                # Table data
                table_data = table.get("data", {})
                cells = table_data.get("cells", [])

                result.table_extractions[table_id] = {
                    "page_number": page_num,
                    "html": table.get("html", table.get("table_html", "")),
                    "otsl": table.get("otsl", ""),
                    "bbox": table.get("bbox", [0, 0, 0, 0]),
                    "rows": table_data.get("rows", 0),
                    "columns": table_data.get("cols", table_data.get("columns", 0)),
                    "confidence": table.get("confidence"),
                    "cells": cells,
                }

        # Processor metadata
        result.processor_metadata.setdefault("processor", "docling")
        result.processor_metadata.setdefault("version", results.get("metadata", {}).get("docling_version", "unknown"))

        return result
