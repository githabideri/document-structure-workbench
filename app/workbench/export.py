"""Document export — assemble effective revision content and render it.

Mirrors the ``SupportBundleService`` shape: ``revision_data()`` / ``project_data()``
assemble plain structured data, and the ``to_text()`` / ``to_markdown()``
renderers consume it. A future self-contained HTML report will consume the same
``revision_data()`` output, so the assembly layer is the one piece the rendering
formats share.

Source of truth is the *effective* (corrected) representation, never the raw
machine output: corrections are applied and suppressed regions are excluded.
This honours the product invariant that a processed revision is immutable and
that the curator's corrections are the authoritative view.

The effective values are computed in bulk here (one corrections query per
revision) rather than via the ``PageRegion.effective_*`` properties, which would
query once per region. The semantics deliberately mirror those properties: the
latest active correction of each operation wins, and any active ``suppress``
correction hides the region.
"""
import zipfile
from io import BytesIO

from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from .models import PageRegion, RegionCorrection

# Renderers currently supported. ``content_type`` carries an explicit charset so
# downloaded text is interpreted as UTF-8 regardless of the client default.
FORMATS = {
    "txt": ("text/plain; charset=utf-8", ".txt"),
    "md": ("text/markdown; charset=utf-8", ".md"),
}
DEFAULT_FORMAT = "md"

# How a project export is packaged: a ZIP of one file per document, or a single
# concatenated file (index + every document).
BUNDLES = {"zip", "single"}
DEFAULT_BUNDLE = "zip"


class DocumentExportService:
    """Assemble exportable revision content and render it to text or markdown."""

    # ------------------------------------------------------------------
    # Assembly
    # ------------------------------------------------------------------

    @staticmethod
    def revision_data(revision):
        """Return the effective content of an immutable revision as plain data.

        Suppressed regions are excluded; corrections are applied. The returned
        dict is consumed by every renderer (text, markdown, future HTML).
        """
        source = getattr(getattr(revision, "processing_job", None), "source_document", None)
        job = getattr(revision, "processing_job", None)

        pages = list(revision.pages.order_by("page_number"))
        regions = list(
            PageRegion.objects.filter(page__in=pages).order_by("page_number", "top", "left")
        )

        # Bulk-load active corrections and resolve the latest one per operation.
        # Ordering by -created_at, -id mirrors PageRegion.effective_* (first wins).
        latest_by_region = {}
        if regions:
            correction_qs = RegionCorrection.objects.filter(
                region_id__in=[r.pk for r in regions], status="active",
            ).order_by("region_id", "-created_at", "-id")
            for correction in correction_qs:
                per_region = latest_by_region.setdefault(correction.region_id, {})
                # Only the first (latest) correction of each operation is kept.
                per_region.setdefault(correction.operation, correction)

        pages_out = []
        for page in pages:
            page_regions = []
            for region in regions:
                if region.page_id != page.pk:
                    continue
                corrections = latest_by_region.get(region.pk, {})
                if "suppress" in corrections:
                    continue  # suppressed regions are never exported
                rtype = region.region_type
                text = region.text
                if "type" in corrections:
                    rtype = corrections["type"].after.get("region_type", rtype)
                if "text" in corrections:
                    text = corrections["text"].after.get("text", text)
                page_regions.append({"type": rtype, "text": text or ""})
            pages_out.append({"page_number": page.page_number, "regions": page_regions})

        return {
            "source_id": source.pk if source else None,
            "source_filename": source.filename if source else revision.filename,
            "revision_id": revision.pk,
            "external_id": revision.external_id,
            "page_count": revision.page_count,
            "created_at": revision.created_at.isoformat(),
            "processor": job.processor if job else "",
            "pages": pages_out,
        }

    @staticmethod
    def project_data(project):
        """Return revision_data for the active revision of each non-archived source.

        Only the active revision (``source.active_document``) is exported per
        source, i.e. the curated result rather than every historical revision.
        Sources without an active revision are skipped.
        """
        sources = (
            project.source_documents.filter(is_archived=False, active_document__isnull=False)
            .select_related("active_document", "active_document__processing_job__source_document")
            .order_by("filename", "id")
        )
        return [DocumentExportService.revision_data(source.active_document) for source in sources]

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    @staticmethod
    def to_text(data):
        """Render revision data as flat, greppable plain text."""
        lines = [data["source_filename"], ""]
        for page in data["pages"]:
            lines.append(f"--- {_('Page')} {page['page_number']} ---")
            lines.append("")
            for region in page["regions"]:
                lines.extend(_region_text_lines(region))
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def to_markdown(data):
        """Render revision data as structured Markdown."""
        meta_parts = [
            f"{_('Revision')} {data['revision_id']}",
            data["external_id"],
            f"{data['page_count']} {_('pages')}",
        ]
        if data["processor"]:
            meta_parts.append(data["processor"])
        meta_parts.append(data["created_at"])
        lines = [
            f"# {data['source_filename']}",
            "",
            "*" + " · ".join(str(part) for part in meta_parts) + "*",
            "",
        ]
        for page in data["pages"]:
            lines.append(f"## {_('Page')} {page['page_number']}")
            lines.append("")
            for region in page["regions"]:
                rendered = _region_markdown_lines(region)
                if rendered:
                    lines.extend(rendered)
                    lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    # ------------------------------------------------------------------
    # Filename helpers (used by the view/API response builders)
    # ------------------------------------------------------------------

    @staticmethod
    def revision_filename(data, fmt):
        base = slugify(data.get("external_id")) or f"document-{data.get('revision_id')}"
        return f"{base}{FORMATS[fmt][1]}"

    @staticmethod
    def project_filename(project, fmt, bundle=DEFAULT_BUNDLE):
        base = slugify(project.name) or f"project-{project.pk}"
        if bundle == "single":
            return f"{base}{FORMATS[fmt][1]}"
        # The project export is always a ZIP archive; ``fmt`` only controls the
        # file extension of the documents *inside* it.
        return f"{base}.zip"

    @staticmethod
    def render_zip(project, fmt):
        """Render a project export as an in-memory ZIP of one file per document.

        Synchronous and in-memory by design for the first iteration, matching the
        existing CSV exports. A background-job + stored-artifact path is the
        documented escape hatch if a real corpus outgrows a single request.
        """
        renderer = DocumentExportService.to_markdown if fmt == "md" else DocumentExportService.to_text
        ext = FORMATS[fmt][1]
        documents = DocumentExportService.project_data(project)

        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            manifest = [
                f"# {project.name}",
                "",
                f"{_('Exported format')}: {fmt}",
                f"{_('Documents')}: {len(documents)}",
                "",
            ]
            for index, data in enumerate(documents, start=1):
                safe = slugify(data.get("external_id")) or slugify(data["source_filename"]) or f"document-{data['revision_id']}"
                name = f"{index:02d}-{safe}{ext}"
                archive.writestr(name, renderer(data))
                manifest.append(f"- `{name}` — {data['source_filename']} ({_('revision')} {data['revision_id']})")
            archive.writestr(f"INDEX{ext}", "\n".join(manifest).rstrip() + "\n")
        buffer.seek(0)
        return buffer.getvalue()

    @staticmethod
    def render_singlefile(project, fmt):
        """Render a project export as one concatenated file (index + every document).

        The mirror of ``render_zip`` for the "I want one greppable file" case:
        a table of contents followed by every document's rendered text, separated
        by clear dividers.
        """
        renderer = DocumentExportService.to_markdown if fmt == "md" else DocumentExportService.to_text
        documents = DocumentExportService.project_data(project)
        divider = "\n\n---\n\n" if fmt == "md" else "\n\n" + ("=" * 60) + "\n\n"
        parts = []
        if fmt == "md":
            parts.append(f"# {project.name}\n\n")
            parts.append(f"*{len(documents)} {_('documents')} · {_('combined export')}*\n\n")
        else:
            parts.append(f"{project.name}\n{len(documents)} {_('documents')}\n\n")
        if documents:
            label = _("Contents") if fmt == "md" else _("CONTENTS")
            parts.append(f"## {label}\n\n" if fmt == "md" else f"==== {label} ====\n\n")
            for index, data in enumerate(documents, start=1):
                parts.append(f"{index}. {data['source_filename']} ({_('revision')} {data['revision_id']})\n")
            parts.append(divider)
        for data in documents:
            parts.append(renderer(data))
            parts.append(divider)
        return "".join(parts)


# ----------------------------------------------------------------------
# Per-region renderers (module-private)
# ----------------------------------------------------------------------

_TABLE_PLACEHOLDER = str(_("[Table — structured table export is not yet available]"))


def _region_text_lines(region):
    """Flat text lines for one region."""
    rtype = region["type"]
    text = (region.get("text") or "").strip()
    if rtype == "table":
        lines = [_TABLE_PLACEHOLDER]
        if text:
            lines.append("")
            lines.append(text)
        return lines
    if text:
        return [text]
    return []


def _region_markdown_lines(region):
    """Markdown lines for one region, mapped from its effective type."""
    rtype = region["type"]
    text = (region.get("text") or "").strip()
    if rtype == "title":
        return [f"### {text}"] if text else []
    if rtype == "list":
        return [f"- {line.strip()}" for line in text.splitlines() if line.strip()]
    if rtype == "table":
        lines = [f"> {_TABLE_PLACEHOLDER}"]
        if text:
            lines.append("")
            lines.append(text)
        return lines
    # text, header, footer, figure, form, other → paragraph
    return [text] if text else []
