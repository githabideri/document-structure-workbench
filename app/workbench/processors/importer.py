"""
Result importer — takes ProcessorResult and creates database records.

Creates:
    Document (processed)
    Page (with image paths, dimensions, text)
    PageRegion (with normalized coordinates)
    TableCandidate (with crop paths)
    ExtractionRun (per profile)
    TableExtraction (per table, per profile)
    ProcessingArtifact (for layout JSON, page text, etc.)
"""
import json
import logging
from pathlib import Path

from django.conf import settings
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
            # Create processed Document linked to SourceDocument
            document = self._create_document()

            # Import pages
            counts["pages"] = self._import_pages(document, result)

            # Import regions
            counts["regions"] = self._import_regions(result)

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

    def _create_document(self) -> Document:
        """Create the processed Document linked to SourceDocument."""
        # Check if a processed document already exists
        if hasattr(self.source_doc, "processed_document"):
            return self.source_doc.processed_document

        # Create Document in the collection
        document = Document.objects.create(
            collection=self.collection,
            external_id=self.source_doc.filename.replace(".pdf", ""),
            filename=self.source_doc.filename,
            sha256=self.source_doc.sha256,
            page_count=self.job.pages_processed or 0,
            source_path=self.source_doc.file_path,
        )

        # Link SourceDocument to Document
        # Note: SourceDocument doesn't have a processed_document FK yet —
        # that's Section 6. For now, we link via collection + filename.
        return document

    def _import_pages(self, document: Document, result: ProcessorResult) -> int:
        """Import page records with image paths and text."""
        count = 0
        for page_num, text in result.page_texts.items():
            page, created = Page.objects.get_or_create(
                document=document,
                page_number=page_num,
                defaults={
                    "image_path": result.page_images.get(page_num, ""),
                },
            )
            if created:
                count += 1

            # Store page text as artifact
            if text:
                ProcessingArtifact.objects.get_or_create(
                    job=self.job,
                    artifact_type="page_text",
                    page_number=page_num,
                    defaults={
                        "data": {"text": text[:10000]},  # Truncate for storage
                    },
                )

        return count

    def _import_regions(self, result: ProcessorResult) -> int:
        """Import page regions with normalized coordinates."""
        count = 0
        for region in result.regions:
            # Normalize bbox: Docling uses [x0, y0, x1, y1] in pixels
            # We need normalized [left, top, right, bottom] in 0-1 range
            bbox = region.get("bbox", [0, 0, 0, 0])
            if len(bbox) == 4:
                left, top, right, bottom = self._normalize_bbox(bbox)
            else:
                left, top, right, bottom = 0, 0, 0, 0

            PageRegion.objects.create(
                source_document=self.source_doc,
                job=self.job,
                page_number=region["page_number"],
                region_type=region.get("region_type", "text"),
                left=left,
                top=top,
                right=right,
                bottom=bottom,
                confidence=region.get("confidence"),
                metadata=region.get("metadata", {}),
            )
            count += 1

        return count

    def _import_tables(self, document: Document, result: ProcessorResult):
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

            # Create ExtractionRun for each profile
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
            extraction = TableExtraction.objects.get_or_create(
                table_candidate=table,
                extraction_run=run,
                defaults={
                    "rows": table_data.get("rows", 0),
                    "columns": table_data.get("columns", 0),
                    "raw_html": table_data.get("html", ""),
                    "raw_otsl": table_data.get("otsl", ""),
                    "status": "success" if table_data.get("html") else "missing",
                },
            )[0]
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
    def _normalize_bbox(bbox: list) -> tuple:
        """
        Normalize bounding box to 0-1 range.

        Input: [x0, y0, x1, y1] in pixels (Docling format)
        Output: (left, top, right, bottom) normalized to 0-1

        For the MVP, we assume bbox is already normalized (0-1 range).
        In production, this would use page dimensions from the processor.
        """
        if len(bbox) != 4:
            return (0, 0, 0, 0)

        left, top, right, bottom = bbox

        # If values are > 1, assume pixels and normalize later
        # For now, pass through as-is (assumes normalized input)
        if left > 1 or top > 1 or right > 1 or bottom > 1:
            # Placeholder: will be normalized when page dimensions are known
            pass

        # Ensure left < right, top < bottom
        if left > right:
            left, right = right, left
        if top > bottom:
            top, bottom = bottom, top

        return (left, top, right, bottom)
