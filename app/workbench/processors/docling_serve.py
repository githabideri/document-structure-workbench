"""
Docling Serve v1 async processor — POST /v1/convert/file/async → poll → fetch.

Official async sequence:
    1. POST /v1/convert/file/async  (multipart, returns task_id)
    2. GET  /v1/status/poll/<task_id>  (poll until success/failed)
    3. GET  /v1/result/<task_id>  (fetch result)

Response wrapper:
    response["document"]["json_content"]  — the Docling document JSON

Configuration (Django settings):
    DSW_DOCLING_API_URL        — base URL (e.g. http://docling:5001)
    DSW_DOCLING_API_KEY        — optional API key (X-Api-Key header)
    DSW_DOCLING_REQUEST_TIMEOUT — per-request timeout (default 60s)
    DSW_DOCLING_JOB_TIMEOUT     — total job timeout (default 3600s)

submit() always returns a string task ID.
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
    """Process documents via Docling Serve v1 async API."""

    def __init__(self, server_url: Optional[str] = None, api_key: Optional[str] = None):
        self.server_url = (
            server_url
            or getattr(settings, "DSW_DOCLING_API_URL", "")
        ).rstrip("/")
        self.api_key = api_key or getattr(settings, "DSW_DOCLING_API_KEY", "")
        self.request_timeout = getattr(settings, "DSW_DOCLING_REQUEST_TIMEOUT", 60)
        self.job_timeout = getattr(settings, "DSW_DOCLING_JOB_TIMEOUT", 3600)

    @property
    def _headers(self):
        headers = {}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        return headers

    def submit(self, source_document, configuration: dict) -> str:
        """
        Submit a PDF via POST /v1/convert/file/async.

        Returns:
            task_id: string identifier for the async task.
        """
        if not self.server_url:
            raise ConnectionError(
                "DSW_DOCLING_API_URL is not configured. "
                "Set DSW_DOCLING_API_URL to your Docling Serve instance."
            )

        file_path = Path(getattr(settings, "ARTIFACTS_BASE_DIR", "/var/lib/dsw/artifacts")) / source_document.file_path

        if not file_path.exists():
            raise FileNotFoundError(f"Source file not found: {file_path}")

        # Build options as individual form fields
        files = {
            "files": (source_document.filename, open(file_path, "rb"), "application/pdf"),
        }
        data = {
            "from_formats": "pdf",
            "to_formats": "json",
            "image_export_mode": "embedded",
            "do_ocr": "true",
            "do_table_structure": "true",
            "include_page_images": "true",
            "images_scale": "2.0",
            "table_mode": "accurate",
        }

        # Override from configuration if present
        if configuration.get("table_mode"):
            data["table_mode"] = configuration["table_mode"]
        if configuration.get("do_ocr") is not None:
            data["do_ocr"] = str(configuration["do_ocr"]).lower()
        if configuration.get("include_page_images") is not None:
            data["include_page_images"] = str(configuration["include_page_images"]).lower()
        if configuration.get("images_scale"):
            data["images_scale"] = str(configuration["images_scale"])

        logger.info(
            "Submitting %s to Docling async %s",
            source_document.filename,
            self.server_url,
        )

        try:
            resp = requests.post(
                f"{self.server_url}/v1/convert/file/async",
                files=files,
                data=data,
                headers=self._headers,
                timeout=self.request_timeout,
            )
            resp.raise_for_status()
            result = resp.json()
        except requests.ConnectionError:
            logger.error("Cannot connect to Docling server at %s", self.server_url)
            raise ConnectionError(f"Docling server unreachable: {self.server_url}")
        except requests.RequestException as e:
            logger.error("Docling submission failed: %s", e)
            logger.error("Response: %s", getattr(e, "response", None))
            raise

        task_id = result.get("task_id")
        if not task_id:
            raise ValueError(f"No task_id in response: {result}")

        logger.info("Docling async task submitted: %s", task_id)
        return task_id

    def get_status(self, external_job_id: str) -> dict:
        """Poll via GET /v1/status/poll/<task_id>."""
        try:
            resp = requests.get(
                f"{self.server_url}/v1/status/poll/{external_job_id}",
                headers=self._headers,
                timeout=self.request_timeout,
            )
            resp.raise_for_status()
            data = resp.json()

            status = data.get("status", "unknown")
            return {
                "state": status,
                "progress": data.get("progress", 0),
                "error": data.get("error"),
            }
        except requests.RequestException as e:
            logger.error("Status poll failed for %s: %s", external_job_id, e)
            return {"state": "error", "progress": 0, "error": str(e)}

    def collect_results(self, external_job_id: str) -> ProcessorResult:
        """Fetch via GET /v1/result/<task_id>."""
        try:
            resp = requests.get(
                f"{self.server_url}/v1/result/{external_job_id}",
                headers=self._headers,
                timeout=self.request_timeout,
            )
            resp.raise_for_status()
            response = resp.json()
        except requests.RequestException as e:
            raise RuntimeError(f"Failed to fetch results for {external_job_id}: {e}")

        # Parse the documented response wrapper
        return self._parse_results(response)

    def cancel(self, external_job_id: str) -> bool:
        """Cancel is not supported by the async API; log and return False."""
        logger.warning("Cancel not supported by Docling async API for task %s", external_job_id)
        return False

    def _parse_results(self, response: dict) -> ProcessorResult:
        """
        Parse Docling Serve v1 async response.

        Structure:
            response["document"]["json_content"]  — Docling document JSON

        The json_content is the full Docling document with pages, tables, etc.
        """
        result = ProcessorResult()

        # Navigate the documented wrapper
        document = response.get("document", {})
        json_content = document.get("json_content", {})

        # Handle both string and dict json_content
        if isinstance(json_content, str):
            try:
                json_content = json.loads(json_content)
            except json.JSONDecodeError:
                logger.error("json_content is not valid JSON")
                result.error_summary = "Invalid JSON in Docling response"
                return result

        pages = json_content.get("pages", [])
        result.pages_processed = len(pages)

        for page_data in pages:
            page_num = page_data.get("page_number", 0)
            if not page_num:
                page_num = result.pages_processed

            # Page dimensions
            page_size = page_data.get("size", {})
            page_width = page_size.get("width", 0)
            page_height = page_size.get("height", 0)

            if page_width and page_height:
                result.processor_metadata.setdefault("page_dimensions", {})[page_num] = {
                    "width": page_width,
                    "height": page_height,
                }

            # Page images (embedded base64)
            page_images = page_data.get("images", [])
            for img in page_images:
                img_data = img.get("data", "")
                img_ref = img.get("ref", "")
                if img_data:
                    # Store metadata for importer to decode
                    result.page_images[page_num] = {
                        "data": img_data,
                        "format": img.get("format", "png"),
                    }
                elif img_ref:
                    result.page_images[page_num] = {"ref": img_ref}

            # Page text
            text_parts = []
            for key in ("text_elements", "texts", "content", "children"):
                elements = page_data.get(key, [])
                for el in elements:
                    text = el.get("text", el.get("content", ""))
                    if text:
                        text_parts.append(text)

            page_text = "\n".join(text_parts) if text_parts else ""
            if page_text:
                result.page_texts[page_num] = page_text

            # Regions
            for region in page_data.get("regions", []):
                result.regions.append({
                    "page_number": page_num,
                    "region_type": region.get("type", "text"),
                    "bbox": region.get("bbox", [0, 0, 0, 0]),
                    "text": region.get("text", region.get("content", "")),
                    "confidence": region.get("confidence"),
                    "metadata": region.get("properties", {}),
                })

            # Tables
            for table in page_data.get("tables", []):
                result.tables_found += 1
                table_id = table.get("id", f"page_{page_num}_table_{result.tables_found}")

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
                    "crop_data": table.get("crop", table.get("crop_data", "")),
                }

        # Processor metadata
        result.processor_metadata.setdefault("processor", "docling")
        result.processor_metadata.setdefault(
            "version",
            response.get("metadata", {}).get("docling_version", "unknown"),
        )

        return result
