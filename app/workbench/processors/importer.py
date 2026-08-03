"""
Result importer — takes ProcessorResult and creates/updates database records.

Idempotent: re-importing the same job updates or replaces records without
creating duplicates.

Creates/updates:
    Document (processed, linked to SourceDocument)
    Page (with image paths, dimensions, text)
    PageRegion (with normalized coordinates 0-1, text retained)
    TableCandidate (with crop paths)
    ExtractionRun (per profile)
    TableExtraction (per table, per profile)
    ProcessingArtifact (for layout JSON, page text, etc.)

Image persistence:
    - Decodes embedded base64 images from Docling response
    - Writes to job/source-specific artifact directory
    - Stores relative path in Page.image_path
    - Saves page width and height
    - Verifies file exists before committing

Suggested layout:
    pages/<source-sha>/<job-id>/page-0001.png
    tables/<source-sha>/<job-id>/table-0001.png

Table crops are generated from page image + normalized bbox when Docling
does not return table crops directly.
"""
import base64
import json
import logging
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from ..models import (
    Collection,
    Document,
    ExtractionRun,
    Page,
    PageRegion,
    ProcessingArtifact,
    ProcessingJob,
    SourceDocument,
    TableCandidate,
    TableExtraction,
)
from .base import ProcessorResult

logger = logging.getLogger(__name__)


class ResultImporter:
    """Import processor results into the database (idempotent)."""

    def __init__(self, job: ProcessingJob):
        self.job = job
        self.source_doc = job.source_document
        self.collection = self.source_doc.collection
        self.artifacts_base = Path(
            getattr(settings, "ARTIFACTS_BASE_DIR", "/var/lib/dsw/artifacts")
        ).resolve()
        self.source_sha = self.source_doc.sha256[:12] or f"job-{self.job.pk}"
        self.job_dir = f"job-{self.job.pk}"

    def import_results(self, result: ProcessorResult) -> dict:
        """
        Import processor results into database (idempotent).

        On reimport of the same job, updates or replaces pages, regions,
        and artifacts without duplicating records.

        Returns:
            dict with counts of created/updated records.
        """
        counts = {
            "pages": 0,
            "regions": 0,
            "tables": 0,
            "extractions": 0,
            "artifacts": 0,
            "images_written": 0,
        }

        with transaction.atomic():
            # Get or create processed Document
            document = self._get_or_create_document(result)

            # Delete existing records for this job (idempotent reimport)
            self._cleanup_existing_records(document, result)

            # Import pages with images
            counts["pages"], counts["images_written"] = self._import_pages(document, result)

            # Import regions
            counts["regions"] = self._import_regions(result, document)

            # Import tables and extractions
            counts["tables"], counts["extractions"] = self._import_tables(document, result)

            # Import artifacts (layout JSON, page text)
            counts["artifacts"] = self._import_artifacts(result)

            # Update job stats
            self.job.pages_processed = result.pages_processed
            self.job.tables_found = result.tables_found
            self.job.save(update_fields=["pages_processed", "tables_found"])

        logger.info(
            "Import complete: %d pages, %d regions, %d tables, %d extractions, "
            "%d images written",
            counts["pages"], counts["regions"], counts["tables"],
            counts["extractions"], counts["images_written"],
        )
        return counts

    def _get_or_create_document(self, result: ProcessorResult) -> Document:
        """Get or create the processed Document linked to SourceDocument."""
        if hasattr(self.source_doc, "processed_document") and self.source_doc.processed_document:
            doc = self.source_doc.processed_document
            if result.pages_processed and doc.page_count != result.pages_processed:
                doc.page_count = result.pages_processed
                doc.save(update_fields=["page_count"])
            return doc

        document = Document.objects.create(
            collection=self.collection,
            external_id=self.source_doc.filename.replace(".pdf", ""),
            filename=self.source_doc.filename,
            sha256=self.source_doc.sha256,
            page_count=result.pages_processed or 0,
            source_path=self.source_doc.file_path,
        )

        self.source_doc.processed_document = document
        self.source_doc.save(update_fields=["processed_document"])

        return document

    def _cleanup_existing_records(self, document: Document, result: ProcessorResult):
        """Clean up existing records for idempotent reimport."""
        # Delete existing pages for this document (will be recreated)
        Page.objects.filter(document=document).delete()
        # Delete existing regions for this job
        PageRegion.objects.filter(job=self.job).delete()
        # Delete existing artifacts for this job
        ProcessingArtifact.objects.filter(job=self.job).delete()
        # Delete existing table candidates for this document
        TableCandidate.objects.filter(document=document).delete()

    def _import_pages(self, document: Document, result: ProcessorResult) -> tuple:
        """Import pages with image persistence."""
        count = 0
        images_written = 0

        for page_num in range(1, result.pages_processed + 1):
            # Persist page image
            image_path = ""
            page_images = result.page_images.get(page_num, {})
            if isinstance(page_images, dict) and "data" in page_images:
                image_path = self._persist_page_image(page_num, page_images)
                if image_path:
                    images_written += 1

            # Get page dimensions
            dims = result.processor_metadata.get("page_dimensions", {}).get(page_num, {})
            page_width = dims.get("width", 0)
            page_height = dims.get("height", 0)

            page, created = Page.objects.update_or_create(
                document=document,
                page_number=page_num,
                defaults={
                    "image_path": image_path,
                    "width": page_width,
                    "height": page_height,
                },
            )
            if created:
                count += 1

            # Store page text as artifact (FULL text, no truncation)
            text = result.page_texts.get(page_num, "")
            if text:
                ProcessingArtifact.objects.update_or_create(
                    job=self.job,
                    artifact_type="page_text",
                    page_number=page_num,
                    defaults={
                        "data": {"text": text},
                    },
                )

        return count, images_written

    def _persist_page_image(self, page_num: int, image_info: dict) -> str:
        """
        Decode and persist a page image.

        Returns relative path or empty string on failure.
        """
        img_data = image_info.get("data", "")
        img_format = image_info.get("format", "png")

        if not img_data:
            return ""

        try:
            # Decode base64
            if "," in img_data:
                img_data = img_data.split(",", 1)[1]
            raw_bytes = base64.b64decode(img_data)
        except Exception as e:
            logger.error("Failed to decode page %d image: %s", page_num, e)
            return ""

        # Write to job-specific directory
        rel_dir = f"pages/{self.source_sha}/{self.job_dir}"
        rel_name = f"page-{page_num:04d}.{img_format}"
        rel_path = f"{rel_dir}/{rel_name}"

        full_path = (self.artifacts_base / rel_path).resolve()
        full_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            full_path.write_bytes(raw_bytes)
            # Verify file exists
            if not full_path.exists():
                logger.error("Page image write failed: %s", full_path)
                return ""
        except OSError as e:
            logger.error("Failed to write page image %s: %s", full_path, e)
            return ""

        logger.info("Wrote page image: %s (%d bytes)", rel_path, len(raw_bytes))
        return rel_path

    def _import_regions(self, result: ProcessorResult, document: Document) -> int:
        """Import regions with normalized coordinates. Saves validated instance."""
        count = 0
        page_dimensions = result.processor_metadata.get("page_dimensions", {})

        for region in result.regions:
            page_num = region["page_number"]
            bbox = region.get("bbox", [0, 0, 0, 0])

            # Normalize bbox using real page dimensions
            left, top, right, bottom = self._normalize_bbox(bbox, page_num, page_dimensions)

            # Build region instance
            region_obj = PageRegion(
                source_document=self.source_doc,
                job=self.job,
                page_number=page_num,
                region_type=region.get("region_type", "text")[:50],
                left=left,
                top=top,
                right=right,
                bottom=bottom,
                confidence=region.get("confidence"),
                text=region.get("text", ""),
                metadata=region.get("metadata", {}),
            )

            # Validate before saving
            try:
                region_obj.full_clean()
            except ValidationError as e:
                # Log individual error messages
                for msg in e.messages:
                    logger.warning("Skipping invalid region on page %d: %s", page_num, msg)
                continue

            # Save the validated instance
            region_obj.save()
            count += 1

        return count

    def _import_tables(self, document: Document, result: ProcessorResult) -> tuple:
        """Import table candidates and extractions."""
        tables_count = 0
        extractions_count = 0

        for table_id, table_data in result.table_extractions.items():
            page_num = table_data.get("page_number", 1)
            page = Page.objects.filter(
                document=document, page_number=page_num
            ).first()

            # Create TableCandidate
            table, created = TableCandidate.objects.get_or_create(
                document=document,
                stable_table_id=table_id,
                defaults={
                    "page": page,
                    "bbox": table_data.get("bbox", []),
                },
            )
            if created:
                tables_count += 1

            # Persist table crop if available
            crop_data = table_data.get("crop_data", "")
            if crop_data and not table.crop_path:
                crop_path = self._persist_table_crop(table_id, page_num, crop_data)
                if crop_path:
                    table.crop_path = crop_path
                    table.save(update_fields=["crop_path"])
            elif not table.crop_path and page and page.image_path:
                # Generate crop from page image + bbox
                self._generate_table_crop(table, page)

            # Create ExtractionRun
            profile = "standard-docling"
            run, _ = ExtractionRun.objects.get_or_create(
                document=document,
                profile=profile,
                defaults={
                    "status": "completed",
                    "software_versions": {
                        "processor": result.processor_metadata.get("processor", "unknown"),
                        "version": result.processor_metadata.get("version", "unknown"),
                    },
                },
            )

            # Create TableExtraction
            extraction, created = TableExtraction.objects.get_or_create(
                table_candidate=table,
                extraction_run=run,
                defaults={
                    "rows": table_data.get("rows", 0),
                    "columns": table_data.get("columns", 0),
                    "raw_html": table_data.get("html", ""),
                    "raw_otsl": table_data.get("otsl", ""),
                    "status": "success" if table_data.get("html") else "missing",
                },
            )
            if created:
                extractions_count += 1

        return tables_count, extractions_count

    def _persist_table_crop(self, table_id: str, page_num: int, crop_data: str) -> str:
        """Decode and persist a table crop image."""
        try:
            if "," in crop_data:
                crop_data = crop_data.split(",", 1)[1]
            raw_bytes = base64.b64decode(crop_data)
        except Exception as e:
            logger.error("Failed to decode table crop %s: %s", table_id, e)
            return ""

        rel_dir = f"tables/{self.source_sha}/{self.job_dir}"
        rel_name = f"table-{page_num:04d}.png"
        rel_path = f"{rel_dir}/{rel_name}"

        full_path = (self.artifacts_base / rel_path).resolve()
        full_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            full_path.write_bytes(raw_bytes)
            if not full_path.exists():
                return ""
        except OSError:
            return ""

        return rel_path

    def _generate_table_crop(self, table: TableCandidate, page: Page):
        """Generate table crop from page image + normalized bbox."""
        if not page.image_path or not table.bbox:
            return

        page_path = (self.artifacts_base / page.image_path).resolve()
        if not page_path.exists():
            return

        try:
            from PIL import Image
            img = Image.open(page_path)
            width, height = img.size

            # Parse bbox [left, top, right, bottom] (normalized 0-1)
            bbox = table.bbox
            if isinstance(bbox, str):
                import json as _json
                bbox = _json.loads(bbox)
            if len(bbox) != 4:
                return

            left, top, right, bottom = bbox
            x0 = int(left * width)
            y0 = int(top * height)
            x1 = int(right * width)
            y1 = int(bottom * height)

            crop = img.crop((x0, y0, x1, y1))

            rel_dir = f"tables/{self.source_sha}/{self.job_dir}"
            rel_name = f"table-page{page.page_num:04d}-{table.pk}.png"
            rel_path = f"{rel_dir}/{rel_name}"

            full_path = (self.artifacts_base / rel_path).resolve()
            full_path.parent.mkdir(parents=True, exist_ok=True)
            crop.save(full_path)

            if full_path.exists():
                table.crop_path = rel_path
                table.save(update_fields=["crop_path"])
        except Exception as e:
            logger.warning("Failed to generate table crop for %s: %s", table.pk, e)

    def _import_artifacts(self, result: ProcessorResult) -> int:
        """Import layout JSON and other artifacts."""
        count = 0

        if result.layout_json:
            ProcessingArtifact.objects.create(
                job=self.job,
                artifact_type="layout_json",
                file_path=result.layout_json,
            )
            count += 1

        return count

    @staticmethod
    def _normalize_bbox(bbox: list, page_num: int, page_dimensions: dict) -> tuple:
        """Normalize bounding box to 0-1 range using real page dimensions."""
        if len(bbox) != 4:
            return (0.0, 0.0, 0.0, 0.0)

        x0, y0, x1, y1 = bbox

        # Already normalized?
        if all(0 <= v <= 1 for v in bbox):
            left, top, right, bottom = x0, y0, x1, y1
        else:
            dims = page_dimensions.get(page_num, {})
            width = dims.get("width", 1)
            height = dims.get("height", 1)

            if width > 0 and height > 0:
                left = max(0.0, min(1.0, x0 / width))
                top = max(0.0, min(1.0, y0 / height))
                right = max(0.0, min(1.0, x1 / width))
                bottom = max(0.0, min(1.0, y1 / height))
            else:
                left, top, right, bottom = x0, y0, x1, y1

        # Ensure ordering
        if left > right:
            left, right = right, left
        if top > bottom:
            top, bottom = bottom, top

        # Clamp
        return (
            max(0.0, min(1.0, left)),
            max(0.0, min(1.0, top)),
            max(0.0, min(1.0, right)),
            max(0.0, min(1.0, bottom)),
        )
