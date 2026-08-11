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
    - Validates image with Pillow before storing
    - Rejects invalid image data

Suggested layout:
    pages/<source-sha>/<job-id>/page-0001.png
    tables/<source-sha>/<job-id>/table-<page>-<stable-id>.png

Table crops are generated from page image + normalized bbox when Docling
does not return table crops directly.

Revision safety:
    - Every processing job owns one immutable Document revision
    - Same-job re-import replaces only that revision's records
    - Other jobs, revisions, reviews, and decisions are never touched
    - Files are staged and atomically promoted with rollback backups
"""
import base64
import logging
import os
import shutil
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from ..models import (
    Document,
    ExtractionRun,
    Page,
    PageRegion,
    ProcessingArtifact,
    ProcessingJob,
    RegionCorrection,
    TableCandidate,
    TableExtraction,
)
from .base import ProcessorResult

logger = logging.getLogger(__name__)


class ImportError(Exception):
    """Raised when import fails."""
    pass


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
        self._staging_root = None
        self._staged_files = {}
        self._promoted_files = []
        self._backup_files = {}

    def import_results(self, result: ProcessorResult) -> dict:
        """
        Import processor results into database (idempotent).

        On reimport of the same job, updates or replaces pages, regions,
        and artifacts without duplicating records.

        Returns:
            dict with counts of created/updated records.

        The returned ``document`` is this job's processed revision. Activation
        is deliberately left to the worker's completed-job finalization.
        """
        self._prepare_staging()
        counts = {
            "pages": 0,
            "regions": 0,
            "tables": 0,
            "extractions": 0,
            "artifacts": 0,
            "images_written": 0,
        }

        try:
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
                self.job.save(update_fields=[
                    "pages_processed", "tables_found", "result_document",
                ])

                # Promote only after all database work has succeeded. If the
                # database commit then fails, the outer exception restores any
                # previous same-job files from backups.
                self._promote_staged_files()
        except Exception:
            self._restore_promoted_files()
            self._discard_staging()
            self.job.refresh_from_db()
            raise

        self._discard_backups()
        self._discard_staging()

        logger.info(
            "Import complete: %d pages, %d regions, %d tables, %d extractions, "
            "%d images written",
            counts["pages"], counts["regions"], counts["tables"],
            counts["extractions"], counts["images_written"],
        )
        counts["document"] = document
        # Keep revision-scoped lexical search in sync with the effective
        # representation (including any previously approved corrections).
        from ..search import rebuild_revision_index
        counts["search_passages"] = rebuild_revision_index(document)
        return counts

    def _prepare_staging(self):
        self.artifacts_base.mkdir(parents=True, exist_ok=True)
        staging_base = self.artifacts_base / ".staging"
        staging_base.mkdir(parents=True, exist_ok=True)
        self._staging_root = Path(tempfile.mkdtemp(
            prefix=f"job-{self.job.pk}-", dir=staging_base,
        ))
        self._staged_files = {}
        self._promoted_files = []
        self._backup_files = {}

    def _get_or_create_document(self, result: ProcessorResult) -> Document:
        """Get or create the immutable processed revision owned by this job."""
        if self.job.result_document_id:
            doc = self.job.result_document
            if result.pages_processed and doc.page_count != result.pages_processed:
                doc.page_count = result.pages_processed
                doc.save(update_fields=["page_count"])
            return doc

        document = Document.objects.create(
            collection=self.collection,
            external_id=Path(self.source_doc.filename).stem,
            filename=self.source_doc.filename,
            sha256=self.source_doc.sha256,
            page_count=result.pages_processed or 0,
            source_path=self.source_doc.file_path,
        )

        self.job.result_document = document

        return document

    def _cleanup_existing_records(self, document: Document, result: ProcessorResult):
        """Clean up existing records for idempotent reimport."""
        if RegionCorrection.objects.filter(region__job=self.job).exists():
            raise ImportError(
                "This processing revision has curator corrections and cannot be re-imported. "
                "Create a new processing job to preserve the correction history."
            )
        # Only clean records from THIS job (not other jobs' records)
        PageRegion.objects.filter(job=self.job).delete()
        ProcessingArtifact.objects.filter(job=self.job).delete()

        # The document is owned one-to-one by this job. Other revisions cannot
        # be reached through these filters.
        TableCandidate.objects.filter(document=document).delete()
        Page.objects.filter(document=document).delete()

    def _stage_bytes(self, rel_path: str, content: bytes) -> Path:
        """Write an artifact to this import's private staging directory."""
        staged_path = self._staging_root / "new" / rel_path
        staged_path.parent.mkdir(parents=True, exist_ok=True)
        staged_path.write_bytes(content)
        self._staged_files[rel_path] = staged_path
        return staged_path

    def _artifact_read_path(self, rel_path: str) -> Path:
        """Resolve a staged artifact first, then its currently committed path."""
        return self._staged_files.get(rel_path, self.artifacts_base / rel_path)

    def _promote_staged_files(self):
        """Atomically replace final files while retaining rollback backups."""
        for rel_path, staged_path in self._staged_files.items():
            final_path = (self.artifacts_base / rel_path).resolve()
            if not final_path.is_relative_to(self.artifacts_base):
                raise ImportError(f"Artifact path escapes storage root: {rel_path}")
            final_path.parent.mkdir(parents=True, exist_ok=True)
            if final_path.exists():
                backup_path = self._staging_root / "backup" / rel_path
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(final_path, backup_path)
                self._backup_files[rel_path] = backup_path
            self._promoted_files.append(rel_path)
            os.replace(staged_path, final_path)

    def _restore_promoted_files(self):
        """Restore the exact pre-import filesystem state after a failure."""
        for rel_path in reversed(self._promoted_files):
            final_path = self.artifacts_base / rel_path
            try:
                if final_path.exists():
                    final_path.unlink()
                backup_path = self._backup_files.get(rel_path)
                if backup_path and backup_path.exists():
                    final_path.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(backup_path, final_path)
            except OSError as exc:
                logger.error("Could not restore artifact %s: %s", rel_path, exc)
        self._promoted_files.clear()

    def _discard_backups(self):
        self._backup_files.clear()

    def _discard_staging(self):
        shutil.rmtree(self._staging_root, ignore_errors=True)

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

            # Authoritative raster dimensions: decode the persisted image header.
            # Prefer decoding the actual on-disk file so the viewer geometry is
            # always correct even if Docling's logical size differs from the
            # rendered raster. This is a one-time import-time cost.
            image_width = page_width
            image_height = page_height
            if image_path:
                from PIL import Image as _PILImage
                try:
                    # Resolve through the staging-aware helper: a freshly imported
                    # page image is staged under .staging and only atomically
                    # promoted later, so the final artifact path may not exist yet.
                    _sp = self._artifact_read_path(image_path)
                    with _PILImage.open(_sp) as _img:
                        image_width, image_height = _img.size
                except Exception:
                    logger.exception("Could not decode raster dims for page %d", page_num)

            page, created = Page.objects.update_or_create(
                document=document,
                page_number=page_num,
                defaults={
                    "image_path": image_path,
                    "width": page_width,
                    "height": page_height,
                    "image_width": image_width or None,
                    "image_height": image_height or None,
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
                        "metadata": {
                            "ocr": result.ocr_pages.get(page_num, {}),
                        } if page_num in result.ocr_pages else {},
                    },
                )

            if page_num in result.ocr_pages:
                ocr = result.ocr_pages[page_num]
                ProcessingArtifact.objects.update_or_create(
                    job=self.job,
                    artifact_type="ocr_page",
                    page_number=page_num,
                    defaults={
                        "data": {"text": ocr.get("text", ""), "blocks": ocr.get("blocks", [])},
                        "metadata": {k: v for k, v in ocr.items() if k not in {"text", "blocks"}},
                    },
                )

        return count, images_written

    def _persist_page_image(self, page_num: int, image_info: dict) -> str:
        """
        Decode and persist a page image.

        Validates image with Pillow before storing.
        Rejects invalid image data rather than saving arbitrary bytes.

        Returns relative path or empty string on failure.
        """
        img_data = image_info.get("data", "")
        img_format = image_info.get("format", "png")

        if not img_data:
            return ""

        try:
            raw_bytes = base64.b64decode(img_data)
        except Exception as e:
            logger.error("Failed to decode page %d image: %s", page_num, e)
            return ""

        # Validate image data with Pillow (if available)
        try:
            from io import BytesIO

            from PIL import Image

            img = Image.open(BytesIO(raw_bytes))
            img.verify()  # Validates the image data
        except ImportError:
            logger.debug("Pillow not installed, skipping image validation")
        except Exception as e:
            logger.error(
                "Page %d image data is not a valid image: %s",
                page_num, e,
            )
            return ""

        # Write to job-specific directory
        rel_dir = f"pages/{self.source_sha}/{self.job_dir}"
        rel_name = f"page-{page_num:04d}.{img_format}"
        rel_path = f"{rel_dir}/{rel_name}"

        try:
            staged_path = self._stage_bytes(rel_path, raw_bytes)
            if not staged_path.exists():
                logger.error("Page image staging failed: %s", staged_path)
                return ""
        except OSError as e:
            logger.error("Failed to stage page image %s: %s", rel_path, e)
            return ""

        logger.info("Staged page image: %s (%d bytes)", rel_path, len(raw_bytes))
        return rel_path

    def _import_regions(self, result: ProcessorResult, document: Document) -> int:
        """Import regions with normalized coordinates. Saves validated instance."""
        count = 0
        page_dimensions = result.processor_metadata.get("page_dimensions", {})

        for region in result.regions:
            page_num = region["page_number"]
            page = Page.objects.filter(
                document=document, page_number=page_num,
            ).first()
            if page is None:
                logger.warning("Skipping region for missing page %d", page_num)
                continue
            bbox = region.get("bbox", [0, 0, 0, 0])

            # Normalize bbox using real page dimensions and coord_origin
            left, top, right, bottom = self._normalize_bbox(
                bbox, page_num, page_dimensions, region.get("metadata", {})
            )

            # Build region instance
            region_obj = PageRegion(
                source_document=self.source_doc,
                job=self.job,
                page=page,
                page_number=page_num,
                region_type=region.get("region_type", "text")[:50],
                left=left,
                top=top,
                right=right,
                bottom=bottom,
                confidence=region.get("confidence"),
                text=region.get("text", ""),
                metadata={
                    **region.get("metadata", {}),
                    "external_ref": region.get("external_ref", ""),
                },
            )

            # Validate before saving
            try:
                region_obj.full_clean()
            except ValidationError as e:
                for msg in e.messages:
                    logger.warning(
                        "Skipping invalid region on page %d: %s", page_num, msg
                    )
                continue

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

            # Get coord_origin for bbox normalization
            coord_origin = table_data.get("coord_origin", "TOPLEFT")
            page_dimensions = result.processor_metadata.get("page_dimensions", {})
            raw_bbox = table_data.get("bbox", [])

            # Normalize bbox for crop generation (store normalized in candidate)
            normalized_bbox = self._normalize_bbox(
                raw_bbox, page_num, page_dimensions, {"coord_origin": coord_origin}
            )

            # Create TableCandidate
            table, created = TableCandidate.objects.get_or_create(
                document=document,
                stable_table_id=table_id,
                defaults={
                    "page": page,
                    "bbox": list(normalized_bbox),
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
                # Generate crop from page image + normalized bbox
                self._generate_table_crop(table, page, normalized_bbox)

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
            raw_bytes = base64.b64decode(crop_data)
        except Exception as e:
            logger.error("Failed to decode table crop %s: %s", table_id, e)
            return ""

        # Validate with Pillow (if available)
        try:
            from io import BytesIO

            from PIL import Image
            img = Image.open(BytesIO(raw_bytes))
            img.verify()
        except ImportError:
            logger.debug("Pillow not installed, skipping table crop validation")
        except Exception:
            logger.error("Table crop %s is not a valid image", table_id)
            return ""

        rel_dir = f"tables/{self.source_sha}/{self.job_dir}"
        rel_name = f"table-{page_num:04d}-{table_id}.png"
        rel_path = f"{rel_dir}/{rel_name}"

        try:
            staged_path = self._stage_bytes(rel_path, raw_bytes)
            if not staged_path.exists():
                return ""
        except OSError:
            return ""

        return rel_path

    def _generate_table_crop(self, table: TableCandidate, page: Page, normalized_bbox: tuple):
        """
        Generate table crop from page image + normalized bbox.

        Uses unique filename: table-<page>-<stable-table-id>.png
        """
        if not page.image_path or not normalized_bbox:
            return

        page_path = self._artifact_read_path(page.image_path).resolve()
        if not page_path.exists():
            return

        try:
            from PIL import Image
            img = Image.open(page_path)
            width, height = img.size

            left, top, right, bottom = normalized_bbox
            x0 = int(left * width)
            y0 = int(top * height)
            x1 = int(right * width)
            y1 = int(bottom * height)

            # Verify crop has nonzero dimensions
            if x1 <= x0 or y1 <= y0:
                logger.warning(
                    "Table %s has zero/negative crop dimensions", table.stable_table_id
                )
                return

            # Verify crop is inside source image
            x0 = max(0, min(x0, width))
            y0 = max(0, min(y0, height))
            x1 = max(0, min(x1, width))
            y1 = max(0, min(y1, height))

            crop = img.crop((x0, y0, x1, y1))

            # Verify crop can be opened
            if crop.width == 0 or crop.height == 0:
                logger.warning("Table %s crop is empty", table.stable_table_id)
                return

            rel_dir = f"tables/{self.source_sha}/{self.job_dir}"
            rel_name = f"table-page{page.page_number:04d}-{table.stable_table_id}.png"
            rel_path = f"{rel_dir}/{rel_name}"

            from io import BytesIO
            buffer = BytesIO()
            crop.save(buffer, format="PNG")
            staged_path = self._stage_bytes(rel_path, buffer.getvalue())

            # Verify generated crop with Pillow before exposing its final path.
            verify_img = Image.open(staged_path)
            verify_img.verify()
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
    def _normalize_bbox(
        bbox: list,
        page_num: int,
        page_dimensions: dict,
        metadata: dict = None,
    ) -> tuple:
        """
        Normalize bounding box to 0-1 range using real page dimensions.

        Supports Docling coord_origin:
            TOPLEFT: standard origin (top-left)
            BOTTOMLEFT: y-axis inverted (bottom-left origin)

        For BOTTOMLEFT, converts vertically before normalization:
            top    = (height - t) / height
            bottom = (height - b) / height

        For TOPLEFT:
            top    = t / height
            bottom = b / height
        """
        if not bbox or len(bbox) != 4:
            return (0.0, 0.0, 0.0, 0.0)

        x0, y0, x1, y1 = bbox
        coord_origin = (metadata or {}).get("coord_origin", "TOPLEFT")

        # Already normalized?
        if all(0 <= v <= 1 for v in bbox):
            left, top, right, bottom = x0, y0, x1, y1
        else:
            dims = page_dimensions.get(page_num, {})
            width = dims.get("width", 1)
            height = dims.get("height", 1)

            if width > 0 and height > 0:
                left = max(0.0, min(1.0, x0 / width))
                right = max(0.0, min(1.0, x1 / width))

                if coord_origin == "BOTTOMLEFT":
                    # Invert y-axis: bottom becomes top
                    top = max(0.0, min(1.0, (height - y0) / height))
                    bottom = max(0.0, min(1.0, (height - y1) / height))
                else:
                    # TOPLEFT (default)
                    top = max(0.0, min(1.0, y0 / height))
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
