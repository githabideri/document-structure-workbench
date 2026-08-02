"""
Import benchmark artifacts into the workbench database.

Usage:
    python manage.py import_benchmark_bundle \
        --collection "DP-Bench full tables" \
        --source /imports/dpbench-full \
        --create-review-tasks
"""
import hashlib
import json
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from workbench.models import (
    Collection, Document, ExtractionRun, Page,
    ReviewTask, TableCandidate, TableExtraction,
)


class Command(BaseCommand):
    help = "Import benchmark artifacts into the workbench database."

    def add_arguments(self, parser):
        parser.add_argument(
            "--collection", required=True,
            help="Collection name for imported data.",
        )
        parser.add_argument(
            "--source", required=True,
            help="Path to the benchmark artifacts directory.",
        )
        parser.add_argument(
            "--create-review-tasks", action="store_true",
            help="Create review tasks for imported tables.",
        )
        parser.add_argument(
            "--profile-a", default="standard-docling",
            help="Profile name for Profile A extractions (default: standard-docling).",
        )
        parser.add_argument(
            "--profile-b", default="granite-table-crop",
            help="Profile name for Profile B extractions (default: granite-table-crop).",
        )

    def handle(self, *args, **options):
        collection_name = options["collection"]
        source_path = Path(options["source"])
        create_tasks = options["create_review_tasks"]
        profile_a = options["profile_a"]
        profile_b = options["profile_b"]

        if not source_path.exists():
            raise CommandError(f"Source path does not exist: {source_path}")

        report = {
            "documents_created": 0,
            "documents_updated": 0,
            "pages_created": 0,
            "tables_created": 0,
            "extractions_created": 0,
            "ground_truth_created": 0,
            "review_tasks_created": 0,
            "runs_created": 0,
            "warnings": [],
        }

        # Create or get collection
        collection, created = Collection.objects.get_or_create(
            name=collection_name,
            defaults={"source_type": "benchmark"},
        )
        if created:
            self.stdout.write(f"Created collection: {collection_name}")
        else:
            self.stdout.write(f"Using existing collection: {collection_name}")

        # Find full-42 directory
        full_dir = source_path / "full-42"
        if not full_dir.exists():
            # Try parent
            full_dir = source_path
        if not (full_dir / "profile-a").exists() and not (full_dir / "profile-b").exists():
            raise CommandError(f"Cannot find profile-a or profile-b in {source_path}")

        profile_a_dir = full_dir / "profile-a"
        profile_b_dir = full_dir / "profile-b"

        # Load reference
        ref_path = full_dir / "reference.json"
        reference = {}
        if ref_path.exists():
            reference = json.loads(ref_path.read_text())
            self.stdout.write(f"Loaded reference: {len(reference)} documents")
        else:
            self.stdout.write("No reference.json found, importing from artifacts only")

        # Load manifest
        manifest_path = profile_a_dir / "all-tables-manifest.json"
        manifest = None
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            self.stdout.write(f"Loaded manifest: {len(manifest.get('tables', []))} tables")

        # Import documents
        doc_dirs = sorted(profile_a_dir.glob("*"))
        doc_dirs = [d for d in doc_dirs if d.is_dir() and d.name != "tables"]

        for doc_dir in doc_dirs:
            external_id = doc_dir.name
            tables_json = doc_dir / "tables.json"

            if not tables_json.exists():
                continue

            # Compute SHA-256 from the tables.json
            sha256 = hashlib.sha256(tables_json.read_bytes()).hexdigest()

            # Create or get document
            document, created = Document.objects.get_or_create(
                collection=collection,
                sha256=sha256,
                defaults={
                    "external_id": external_id,
                    "filename": f"{external_id}.pdf",
                    "page_count": 1,
                },
            )
            if created:
                report["documents_created"] += 1
            else:
                report["documents_updated"] += 1

            # Import pages
            pages_dir = doc_dir / "pages"
            if pages_dir.exists():
                for page_img in sorted(pages_dir.glob("*.png")):
                    page_num = int(page_img.stem.replace("page-", ""))
                    page, p_created = Page.objects.get_or_create(
                        document=document,
                        page_number=page_num,
                        defaults={"image_path": str(page_img)},
                    )
                    if p_created:
                        report["pages_created"] += 1

            # Import tables from tables.json
            table_data = json.loads(tables_json.read_text())
            tables = table_data.get("tables", [])

            for table_info in tables:
                stable_id = table_info.get("table_id", "unknown")
                page_num = table_info.get("page_number", 1)

                # Ensure page exists
                page, _ = Page.objects.get_or_create(
                    document=document,
                    page_number=page_num,
                )

                # Create table candidate
                table_candidate, t_created = TableCandidate.objects.get_or_create(
                    document=document,
                    stable_table_id=stable_id,
                    defaults={
                        "page": page,
                        "crop_path": str(doc_dir / "tables" / f"{stable_id}.png") if (doc_dir / "tables" / f"{stable_id}.png").exists() else "",
                        "bbox": table_info.get("bbox", []),
                        "metadata": table_info,
                    },
                )
                if t_created:
                    report["tables_created"] += 1

                # Import Profile A extraction
                run_a, a_created = ExtractionRun.objects.get_or_create(
                    document=document,
                    profile=profile_a,
                    defaults={"status": "completed"},
                )
                if a_created:
                    report["runs_created"] += 1

                otsl = table_info.get("otsl_parsed", {})
                raw_html = table_info.get("html", "")
                if not raw_html and otsl:
                    raw_html = self._build_html_from_otsl(otsl)

                _, ext_created = TableExtraction.objects.get_or_create(
                    table_candidate=table_candidate,
                    extraction_run=run_a,
                    defaults={
                        "rows": otsl.get("num_rows", 0) or table_info.get("rows", 0),
                        "columns": otsl.get("num_cols", 0) or table_info.get("columns", 0),
                        "raw_otsl": json.dumps(otsl) if otsl else "",
                        "raw_html": raw_html,
                        "status": "success",
                    },
                )
                if ext_created:
                    report["extractions_created"] += 1

                # Import Profile B extraction
                if profile_b_dir.exists():
                    # Find matching Profile B result
                    b_tables_dir = profile_b_dir / "tables"
                    b_json = b_tables_dir / f"{external_id}-{stable_id}.json"

                    if b_json.exists():
                        b_data = json.loads(b_json.read_text())
                        if b_data.get("status") == "success" or "otsl_parsed" in b_data:
                            run_b, b_created = ExtractionRun.objects.get_or_create(
                                document=document,
                                profile=profile_b,
                                defaults={"status": "completed"},
                            )
                            if b_created:
                                report["runs_created"] += 1

                            b_otsl = b_data.get("otsl_parsed", {})
                            b_html = b_data.get("html", "")
                            if not b_html and b_otsl:
                                b_html = self._build_html_from_otsl(b_otsl)

                            _, b_ext_created = TableExtraction.objects.get_or_create(
                                table_candidate=table_candidate,
                                extraction_run=run_b,
                                defaults={
                                    "rows": b_otsl.get("num_rows", 0),
                                    "columns": b_otsl.get("num_cols", 0),
                                    "raw_otsl": json.dumps(b_otsl) if b_otsl else "",
                                    "raw_html": b_html,
                                    "status": "success",
                                },
                            )
                            if b_ext_created:
                                report["extractions_created"] += 1

                # Import ground truth from reference
                if external_id in reference:
                    gt_doc = reference[external_id]
                    gt_tables = [e for e in gt_doc.get("elements", []) if e.get("category", "").lower() == "table"]

                    run_gt, gt_created = ExtractionRun.objects.get_or_create(
                        document=document,
                        profile="ground-truth",
                        defaults={"status": "completed"},
                    )
                    if gt_created:
                        report["runs_created"] += 1

                    gt_idx = 0
                    for gt_table in gt_tables:
                        if gt_idx < len(tables):
                            gt_html = gt_table.get("content", {}).get("html", "")
                            _, gt_ext_created = TableExtraction.objects.get_or_create(
                                table_candidate=table_candidate,
                                extraction_run=run_gt,
                                defaults={
                                    "raw_html": gt_html,
                                    "status": "success",
                                },
                            )
                            if gt_ext_created:
                                report["ground_truth_created"] += 1

                        gt_idx += 1

                # Create review task
                if create_tasks:
                    task, task_created = ReviewTask.objects.get_or_create(
                        table_candidate=table_candidate,
                        defaults={"state": "unassigned", "priority": 50},
                    )
                    if task_created:
                        report["review_tasks_created"] += 1

        # Print report
        self.stdout.write("\n" + "=" * 50)
        self.stdout.write("Import Report")
        self.stdout.write("=" * 50)
        for key, value in report.items():
            if value:
                self.stdout.write(f"  {key}: {value}")

        self.stdout.write(json.dumps(report, indent=2))
        self.stdout.write(self.style.SUCCESS("Import complete."))

    def _build_html_from_otsl(self, otsl):
        """Build HTML table from OTSL parsed data."""
        cells = otsl.get("table_cells", [])
        num_rows = otsl.get("num_rows", 0)
        num_cols = otsl.get("num_cols", 0)

        if not cells or not num_rows or not num_cols:
            return ""

        grid = [[None for _ in range(num_cols)] for _ in range(num_rows)]
        for cell in cells:
            r = cell.get("start_row_offset_idx", 0)
            c = cell.get("start_col_offset_idx", 0)
            text = cell.get("text", "")
            rowspan = cell.get("row_span", 1) or 1
            colspan = cell.get("col_span", 1) or 1
            is_header = cell.get("column_header", False)

            if 0 <= r < num_rows and 0 <= c < num_cols:
                tag = "th" if is_header else "td"
                attrs = ""
                if rowspan > 1:
                    attrs += f' rowspan="{rowspan}"'
                if colspan > 1:
                    attrs += f' colspan="{colspan}"'
                grid[r][c] = f"<{tag}{attrs}>{text}</{tag}>"

        rows = []
        for row in grid:
            cells_html = "".join(c if c else "<td></td>" for c in row)
            rows.append(f"<tr>{cells_html}</tr>")

        return f"<table><tbody>{''.join(rows)}</tbody></table>"
