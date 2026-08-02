"""
Result importer — takes ProcessorResult and creates database records.

Creates:
    Document (processed, linked to SourceDocument)
    Page (with image paths, dimensions, text)
    PageRegion (with normalized coordinates 0-1)
    TableCandidate (with crop paths)
    ExtractionRun (per profile)
    TableExtraction (per table, per profile)
    ProcessingArtifact (for layout JSON, page text, etc.)

Key rules:
    - Bounding boxes are normalized to 0-1 using real page dimensions
    - Page text is stored in full (no truncation)
    - full_clean() is called before saving to enforce model validation
    - Duplicate processed Documents are not created on re-import
"""
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
    """Import processor results into the database."""

    def __init__(self, job: ProcessingJob):
        self.job = job
        self.source_doc = job.source_document
        self.collection = self.source_doc.collection

    def import_results(self, result: ProcessorResult) -> dict:
        """
        Import processor results into database.

        Returns:
            dict with counts of created records.
        """
        counts = {
            "pages": 0,
            "regions": 0,
            "tables": 0,
            "extractions": 0,
            "artifacts": 0,
        }

        with transaction.atomic():
            # Create or get processed Document linked to SourceDocument
            document = self._get_or_create_document(result)

            # Import pages
            counts["pages"] = self._import_pages(document, result)

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
            "Import complete: %d pages, %d regions, %d tables, %d extractions",
            counts["pages"], counts["regions"], counts["tables"], counts["extractions"],
        )
        return counts

    def _get_or_create_document(self, result: ProcessorResult) -> Document:
        """
        Get or create the processed Document linked to SourceDocument.

        Uses SourceDocument.processed_document FK to prevent duplicates.
        """
        # Check if a processed document already exists via FK
        if hasattr(self.source_doc, "processed_document") and self.source_doc.processed_document:
            doc = self.source_doc.processed_document
            # Update page_count if we have better data
            if result.pages_processed and doc.page_count != result.pages_processed:
                doc.page_count = result.pages_processed
                doc.save(update_fields=["page_count"])
            return doc

        # Create Document in the collection
        document = Document.objects.create(
            collection=self.collection,
            external_id=self.source_doc.filename.replace(".pdf", ""),
            filename=self.source_doc.filename,
            sha256=self.source_doc.sha256,
            page_count=result.pages_processed or 0,
            source_path=self.source_doc.file_path,
        )

        # Link SourceDocument to Document via FK
        self.source_doc.processed_document = document
        self.source_doc.save(update_fields=["processed_document"])

        return document

    def _import_pages(self, document: Document, result: ProcessorResult) -> int:
        """Import page records with image paths, dimensions, and full text."""
        count = 0
        for page_num in range(1, result.pages_processed + 1):
            page, created = Page.objects.get_or_create(
                document=document,
                page_number=page_num,
                defaults={
                    "image_path": result.page_images.get(page_num, ""),
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
                        "data": {"text": text},  # Full text — no truncation
                    },
                )

        return count

    def _import_regions(self, result: ProcessorResult, document: Document) -> int:
        """Import page regions with normalized coordinates (0-1 range)."""
        count = 0
        page_dimensions = result.processor_metadata.get("page_dimensions", {})

        for region in result.regions:
            page_num = region["page_number"]
            bbox = region.get("bbox", [0, 0, 0, 0])

            # Normalize bbox using real page dimensions
            left, top, right, bottom = self._normalize_bbox(bbox, page_num, page_dimensions)

            # Validate before saving
            try:
                PageRegion(
                    source_document=self.source_doc,
                    job=self.job,
                    page_number=page_num,
                    region_type=region.get("region_type", "text")[:50],
                    left=left,
                    top=top,
                    right=right,
                    bottom=bottom,
                    confidence=region.get("confidence"),
                    metadata=region.get("metadata", {}),
                ).full_clean()
            except ValidationError as e:
                logger.warning(
                    "Skipping invalid region on page %d: %s",
                    page_num, e.message,
                )
                continue

            PageRegion.objects.create(
                source_document=self.source_doc,
                job=self.job,
                page_number=page_num,
                region_type=region.get("region_type", "text")[:50],
                left=left,
                top=top,
                right=right,
                bottom=bottom,
                confidence=region.get("confidence"),
                metadata=region.get("metadata", {}),
            )
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

            # Create ExtractionRun for standard profile
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
        """
        Normalize bounding box to 0-1 range.

        Input: [x0, y0, x1, y1] — may be pixels or already normalized
        Output: (left, top, right, bottom) in 0-1 range

        Uses actual page dimensions from Docling when available.
        """
        if len(bbox) != 4:
            return (0.0, 0.0, 0.0, 0.0)

        x0, y0, x1, y1 = bbox

        # Check if bbox is already normalized (all values in 0-1)
        if all(0 <= v <= 1 for v in bbox):
            left, top, right, bottom = x0, y0, x1, y1
        else:
            # Pixel coordinates — normalize using page dimensions
            dims = page_dimensions.get(page_num, {})
            width = dims.get("width", 1)
            height = dims.get("height", 1)

            if width > 0 and height > 0:
                left = max(0.0, min(1.0, x0 / width))
                top = max(0.0, min(1.0, y0 / height))
                right = max(0.0, min(1.0, x1 / width))
                bottom = max(0.0, min(1.0, y1 / height))
            else:
                # No dimensions available — assume already normalized
                left, top, right, bottom = x0, y0, x1, y1

        # Ensure left < right, top < bottom
        if left > right:
            left, right = right, left
        if top > bottom:
            top, bottom = bottom, top

        # Clamp to 0-1
        left = max(0.0, min(1.0, left))
        top = max(0.0, min(1.0, top))
        right = max(0.0, min(1.0, right))
        bottom = max(0.0, min(1.0, bottom))

        return (left, top, right, bottom)
