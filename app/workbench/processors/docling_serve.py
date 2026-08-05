"""
Docling Serve v1 async processor — POST /v1/convert/file/async → poll → fetch.

Official async sequence:
    1. POST /v1/convert/file/async  (multipart, returns task_id)
    2. GET  /v1/status/poll/<task_id>  (poll until success/failed)
    3. GET  /v1/result/<task_id>  (fetch result)

Response wrapper:
    response["document"]["json_content"]  — the Docling document JSON (DoclingDocument)

DoclingDocument schema (v2.5+):
    pages: mapping keyed by page number (integer keys)
        page.image: embedded image URI (data:image/png;base64,...)
        page.size: {width, height}
    texts: global list of text items
        text.self_ref: "#/texts/N"
        text.prov: list of provenance entries
            prov.page_no, prov.bbox ({l,t,r,b,coord_origin}), prov.charspan
    tables: global list
        table.self_ref: "#/tables/N"
        table.prov: provenance list
        table.data: {table_cells, num_rows, num_cols}
    pictures: global list
    body: document hierarchy

Configuration (Django settings):
    DSW_DOCLING_API_URL        — base URL (e.g. http://docling:5001)
    DSW_DOCLING_API_KEY        — optional API key (X-Api-Key header)
    DSW_DOCLING_REQUEST_TIMEOUT — per-request timeout (default 60s)
    DSW_DOCLING_JOB_TIMEOUT     — total job timeout (default 3600s)

submit() always returns a string task ID.
"""
import json
import logging
from pathlib import Path
from typing import Optional

import requests
from django.conf import settings

from .base import DocumentProcessor, ProcessorResult

logger = logging.getLogger(__name__)


class SubmissionRejected(RuntimeError):
    """The processor definitely rejected a submission."""


class SubmissionUncertain(RuntimeError):
    """The transport failed after submission may have reached the processor."""


class ProcessorProtocolError(RuntimeError):
    """The processor returned a response that violates its contract."""


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

        file_path = Path(
            getattr(settings, "ARTIFACTS_BASE_DIR", "/var/lib/dsw/artifacts")
        ) / source_document.file_path

        if not file_path.exists():
            raise FileNotFoundError(f"Source file not found: {file_path}")

        # Build options as individual form fields
        suffix = Path(source_document.filename).suffix.lower()
        content_type = {
            ".pdf": "application/pdf",
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".tif": "image/tiff", ".tiff": "image/tiff",
        }.get(suffix, "application/octet-stream")
        files = {
            "files": (source_document.filename, open(file_path, "rb"), content_type),
        }
        source_format = Path(source_document.filename).suffix.lower().lstrip(".")
        if source_format in {"jpg", "jpeg", "png", "tif", "tiff"}:
            source_format = "image"
        data = {
            "from_formats": source_format or "pdf",
            "to_formats": "json",
            "image_export_mode": "embedded",
            # PaddleOCR-VL is applied by DSW after Docling has rendered pages
            # and detected structure. Keeping Docling OCR off prevents a
            # second, lower-quality CPU OCR pass during normal ingestion.
            "do_ocr": str(getattr(settings, "DSW_DOCLING_OCR_ENABLED", False)).lower(),
            "do_table_structure": "true",
            "include_page_images": "true",
            "images_scale": str(configuration.get("images_scale", getattr(settings, "DSW_DOCLING_IMAGES_SCALE", 2.0))),
            "table_mode": "accurate",
        }

        # If the legacy Docling OCR stage is explicitly enabled, use the
        # current ocr_preset field. Normal ingestion leaves this disabled and
        # applies PaddleOCR-VL in DSW after page rendering.
        if data["do_ocr"] == "true":
            data["ocr_preset"] = configuration.get("ocr_preset", getattr(settings, "DSW_DOCLING_OCR_ENGINE", "rapidocr"))
            data["ocr_lang"] = configuration.get("ocr_lang", getattr(settings, "DSW_DOCLING_OCR_LANG", "de,en")).split(",")

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
        except (requests.ConnectionError, requests.Timeout) as exc:
            logger.error("Cannot connect to Docling server at %s", self.server_url)
            raise SubmissionUncertain(
                f"Docling submission outcome is uncertain: {self.server_url}"
            ) from exc
        except requests.HTTPError as exc:
            logger.error("Docling submission failed: %s", exc)
            raise SubmissionRejected(f"Docling rejected submission: {exc}") from exc
        except requests.RequestException as exc:
            logger.error("Docling submission failed: %s", exc)
            raise SubmissionRejected(f"Docling submission failed: {exc}") from exc

        task_id = result.get("task_id")
        if not task_id:
            raise ProcessorProtocolError(f"No task_id in response: {result}")

        logger.info("Docling async task submitted: %s", task_id)
        return task_id

    def get_status(self, external_job_id: str) -> dict:
        """
        Poll via GET /v1/status/poll/<task_id>.

        Reads documented fields:
            task_status  — primary status field
            error_message — error text
            failure — structured failure object with message

        Retains legacy "status" fallback for compatibility.
        """
        try:
            resp = requests.get(
                f"{self.server_url}/v1/status/poll/{external_job_id}",
                headers=self._headers,
                timeout=self.request_timeout,
            )
            resp.raise_for_status()
            data = resp.json()

            # Read documented fields with legacy fallback
            task_status = data.get("task_status", data.get("status", "unknown"))
            error_message = data.get("error_message")
            failure = data.get("failure") or {}
            if not error_message:
                error_message = failure.get("message")

            return {
                "state": task_status,
                "progress": data.get("progress", 0),
                "error": error_message,
            }
        except requests.RequestException as e:
            logger.error("Status poll failed for %s: %s", external_job_id, e)
            raise ConnectionError(
                f"Docling status request failed for {external_job_id}: {e}"
            ) from e

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
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise ConnectionError(
                f"Result retrieval interrupted for {external_job_id}: {exc}"
            ) from exc
        except requests.RequestException as exc:
            raise RuntimeError(f"Failed to fetch results for {external_job_id}: {exc}") from exc

        return self._parse_results(response)

    def cancel(self, external_job_id: str) -> bool:
        """Cancel is not supported by the async API; log and return False."""
        logger.warning("Cancel not supported by Docling async API for task %s", external_job_id)
        return False

    def _parse_results(self, response: dict) -> ProcessorResult:
        """
        Parse Docling Serve v1 async response.

        Navigates:
            response["document"]["json_content"]  — Docling document JSON

        Supports the actual DoclingDocument structure:
            pages: mapping keyed by page number (integer keys)
            texts: global list of text items
            tables: global list
            pictures: global list
            body: document hierarchy

        Also supports legacy page-local structure for backward compatibility.
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

        # Detect structure type
        has_global_lists = "texts" in json_content or "tables" in json_content
        has_page_list = "pages" in json_content

        if has_global_lists:
            # Real DoclingDocument structure
            return self._parse_docling_document(json_content, result, response)
        elif has_page_list:
            # Legacy page-local structure (backward compat)
            return self._parse_legacy_pages(json_content, result)
        else:
            logger.error("Unknown Docling response structure")
            result.error_summary = "Unrecognized Docling response format"
            return result

    def _parse_docling_document(self, doc: dict, result: ProcessorResult, response: dict = None) -> ProcessorResult:
        """
        Parse the actual DoclingDocument structure.

        Structure:
            pages: dict keyed by page number
                page.image: "data:image/png;base64,..."
                page.size: {width, height}
            texts: list of text items
                text.self_ref: "#/texts/N"
                text.prov: [{page_no, bbox, charspan}]
            tables: list
                table.self_ref: "#/tables/N"
                table.prov: [{page_no, bbox, charspan}]
                table.data: {table_cells, num_rows, num_cols}
            pictures: list
        """
        # Parse pages metadata
        pages_map = doc.get("pages", {})
        if isinstance(pages_map, dict):
            # Keys are page numbers (may be string or int)
            result.pages_processed = len(pages_map)
            for page_key, page_data in pages_map.items():
                page_num = int(page_key)

                # Page dimensions
                size = page_data.get("size", {})
                width = size.get("width", 0)
                height = size.get("height", 0)
                if width and height:
                    result.processor_metadata.setdefault("page_dimensions", {})[page_num] = {
                        "width": width,
                        "height": height,
                    }

                # Page image: can be data URI string or ImageRef dict
                image_val = page_data.get("image", "")
                if isinstance(image_val, dict):
                    # ImageRef: {"mimetype": "image/png", "dpi": 144, "size": {...}, "uri": "data:..."}
                    uri = image_val.get("uri", "")
                    if uri:
                        image_data = uri
                    else:
                        image_data = ""
                else:
                    image_data = image_val
                if image_data:
                    result.page_images[page_num] = self._extract_image_data(image_data)

        # Parse global texts → regions
        texts = doc.get("texts", [])
        for text_item in texts:
            self_ref = text_item.get("self_ref", "")
            text_content = text_item.get("text", "")
            label = text_item.get("label", "text")
            content_layer = text_item.get("content_layer", "body")
            prov_list = text_item.get("prov", [])

            for prov in prov_list:
                page_num = prov.get("page_no", 1)
                bbox = prov.get("bbox", {})

                result.regions.append({
                    "page_number": page_num,
                    "region_type": self._label_to_region_type(label),
                    "bbox": self._parse_bbox_dict(bbox),
                    "text": text_content,
                    "confidence": None,
                    "external_ref": self_ref,
                    "content_layer": content_layer,
                    "metadata": {
                        "label": label,
                        "content_layer": content_layer,
                        "coord_origin": bbox.get("coord_origin", "TOPLEFT") if isinstance(bbox, dict) else "TOPLEFT",
                    },
                })

            # Accumulate page text from prov entries
            if prov_list:
                for prov in prov_list:
                    page_num = prov.get("page_no", 1)
                    result.page_texts.setdefault(page_num, [])
                    result.page_texts[page_num].append(text_content)

        # Parse global tables
        tables = doc.get("tables", [])
        for table_item in tables:
            self_ref = table_item.get("self_ref", "")
            prov_list = table_item.get("prov", [])
            table_data = table_item.get("data", {})

            result.tables_found += 1

            # Extract table cells info
            table_cells = table_data.get("table_cells", [])
            num_rows = table_data.get("num_rows", 0)
            num_cols = table_data.get("num_cols", 0)

            for prov in prov_list:
                page_num = prov.get("page_no", 1)
                bbox = prov.get("bbox", {})

                table_id = self._ref_to_id(self_ref) or f"table_{result.tables_found}"

                # Generate HTML from table cells
                html = self._generate_table_html(table_cells, table_data)

                result.table_extractions[table_id] = {
                    "page_number": page_num,
                    "html": html,
                    "otsl": "",
                    "bbox": self._parse_bbox_dict(bbox),
                    "rows": num_rows,
                    "columns": num_cols,
                    "confidence": None,
                    "cells": table_cells,
                    "crop_data": "",
                    "external_ref": self_ref,
                    "coord_origin": bbox.get("coord_origin", "TOPLEFT") if isinstance(bbox, dict) else "TOPLEFT",
                }

        # Parse pictures (for future use)
        pictures = doc.get("pictures", [])
        if pictures:
            result.processor_metadata["pictures_found"] = len(pictures)

        # Join accumulated page texts
        for page_num in list(result.page_texts.keys()):
            if isinstance(result.page_texts[page_num], list):
                result.page_texts[page_num] = "\n".join(result.page_texts[page_num])

        # Processor metadata
        result.processor_metadata.setdefault("processor", "docling")
        if response:
            result.processor_metadata.setdefault(
                "version",
                response.get("metadata", {}).get("docling_version", "unknown"),
            )

        return result

    def _parse_legacy_pages(self, doc: dict, result: ProcessorResult) -> ProcessorResult:
        """Parse legacy page-local structure (backward compat)."""
        pages = doc.get("pages", [])
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
                if img_data:
                    result.page_images[page_num] = {
                        "data": img_data,
                        "format": img.get("format", "png"),
                    }

            # Page text
            text_parts = []
            for key in ("text_elements", "texts", "content", "children"):
                elements = page_data.get(key, [])
                for el in elements:
                    text = el.get("text", el.get("content", ""))
                    if text:
                        text_parts.append(text)

            if text_parts:
                result.page_texts[page_num] = "\n".join(text_parts)

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

        result.processor_metadata.setdefault("processor", "docling")
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_image_data(image_value: str) -> dict:
        """Extract image data from various formats (data URI, base64, ref)."""
        if not image_value:
            return {}

        # Data URI: "data:image/png;base64,..."
        if image_value.startswith("data:"):
            # Extract MIME type and base64 data
            header, b64_data = image_value.split(",", 1)
            mime = header.split(":")[1].split(";")[0] if ":" in header else "image/png"
            fmt = mime.split("/")[-1] if "/" in mime else "png"
            return {"data": b64_data, "format": fmt}

        # Plain base64 (no header)
        return {"data": image_value, "format": "png"}

    @staticmethod
    def _parse_bbox_dict(bbox) -> list:
        """Convert bbox dict or list to [left, top, right, bottom] list."""
        if isinstance(bbox, dict):
            return [
                bbox.get("l", 0),
                bbox.get("t", 0),
                bbox.get("r", 0),
                bbox.get("b", 0),
            ]
        if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
            return list(bbox[:4])
        return [0, 0, 0, 0]

    @staticmethod
    def _label_to_region_type(label: str) -> str:
        """Map Docling label to PageRegion.region_type."""
        mapping = {
            "title": "title",
            "text": "text",
            "paragraph": "text",
            "caption": "text",
            "footnote": "text",
            "table": "table",
            "figure": "figure",
            "picture": "figure",
            "formula": "other",
            "list_item": "list",
            "code": "other",
            "header": "header",
            "footer": "footer",
        }
        return mapping.get(label.lower(), "text")

    @staticmethod
    def _ref_to_id(ref: str) -> str:
        """Convert self_ref like '#/tables/2' to stable ID 'table_2'."""
        if not ref:
            return ""
        parts = ref.strip("#/").split("/")
        if len(parts) >= 2:
            kind = parts[0].rstrip("s")  # "tables" → "table"
            num = parts[1]
            return f"{kind}_{num}"
        return ref.strip("#/")

    @staticmethod
    def _generate_table_html(table_cells: list, table_data: dict) -> str:
        """
        Generate HTML table from Docling table_cells.

        Docling cell fields (actual schema):
            start_row_offset_idx, end_row_offset_idx
            start_col_offset_idx, end_col_offset_idx
            column_header, row_header
            text

        Legacy fields (backward compat):
            row_index, col_index, row_span, col_span, type
        """
        if not table_cells:
            return ""

        # Detect schema: Docling offset-based vs legacy index-based
        first_cell = table_cells[0]
        is_offset = "start_row_offset_idx" in first_cell

        # Build grid
        num_rows = table_data.get("num_rows", 0)
        num_cols = table_data.get("num_cols", 0)

        if is_offset:
            # Docling offset-based schema
            cell_map = {}
            for cell in table_cells:
                sr = cell.get("start_row_offset_idx", 0)
                er = cell.get("end_row_offset_idx", 1)
                sc = cell.get("start_col_offset_idx", 0)
                ec = cell.get("end_col_offset_idx", 1)
                rowspan = er - sr
                colspan = ec - sc
                # Fill all grid positions covered by this cell
                for ri in range(sr, er):
                    for ci in range(sc, ec):
                        cell_map[(ri, ci)] = cell
                # Infer dimensions
                num_rows = max(num_rows, er)
                num_cols = max(num_cols, ec)
        else:
            # Legacy index-based schema
            cell_map = {}
            for cell in table_cells:
                ri = cell.get("row_index", 0)
                ci = cell.get("col_index", 0)
                rowspan = cell.get("row_span", 1)
                colspan = cell.get("col_span", 1)
                for dr in range(rowspan):
                    for dc in range(colspan):
                        cell_map[(ri + dr, ci + dc)] = cell
                num_rows = max(num_rows, ri + rowspan)
                num_cols = max(num_cols, ci + colspan)

        # Generate HTML
        seen = set()
        html_parts = ["<table>"]
        for ri in range(num_rows):
            html_parts.append("<tr>")
            for ci in range(num_cols):
                cell = cell_map.get((ri, ci))
                if cell and (ri, ci) not in seen:
                    seen.add((ri, ci))
                    text = cell.get("text", "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    if is_offset:
                        is_header = cell.get("column_header", False) or cell.get("row_header", False)
                        sr = cell.get("start_row_offset_idx", ri)
                        er = cell.get("end_row_offset_idx", ri + 1)
                        sc = cell.get("start_col_offset_idx", ci)
                        ec = cell.get("end_col_offset_idx", ci + 1)
                        rowspan = er - sr
                        colspan = ec - sc
                        # Mark all covered cells as seen
                        for dr in range(sr, er):
                            for dc in range(sc, ec):
                                seen.add((dr, dc))
                    else:
                        is_header = cell.get("type", "") in ("header_cell", "header")
                        rowspan = cell.get("row_span", 1)
                        colspan = cell.get("col_span", 1)
                    tag = "th" if is_header else "td"
                    attrs = ""
                    if rowspan > 1:
                        attrs += f' rowspan="{rowspan}"'
                    if colspan > 1:
                        attrs += f' colspan="{colspan}"'
                    html_parts.append(f"<{tag}{attrs}>{text}</{tag}>")
                elif (ri, ci) not in seen:
                    seen.add((ri, ci))
                    if cell:
                        html_parts.append("<td></td>")
                    else:
                        html_parts.append("<td></td>")
            html_parts.append("</tr>")
        html_parts.append("</table>")

        return "".join(html_parts)
