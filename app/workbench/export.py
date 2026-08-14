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
import re
import zipfile
from io import BytesIO
from xml.sax.saxutils import escape as xml_escape

from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from .models import PageRegion, RegionCorrection

# Renderers currently supported. ``content_type`` carries an explicit charset so
# downloaded text is interpreted as UTF-8 regardless of the client default.
FORMATS = {
    "txt": ("text/plain; charset=utf-8", ".txt"),
    "md": ("text/markdown; charset=utf-8", ".md"),
    "xml": ("application/xml; charset=utf-8", ".xml"),
    "tei": ("application/xml; charset=utf-8", ".tei.xml"),
}
DEFAULT_FORMAT = "md"

# Single-file concatenation only makes sense for the line-oriented text
# formats; XML documents must stay one-document-per-file.
SINGLE_FILE_FORMATS = {"txt", "md"}

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
                page_regions.append({
                    "id": region.pk,
                    "type": rtype,
                    "text": text or "",
                    # Page-relative [0,1] geometry; XML consumers scale these by
                    # the page raster dimensions recorded on the page dict.
                    "left": region.left, "top": region.top,
                    "right": region.right, "bottom": region.bottom,
                })
            pages_out.append({
                "page_number": page.page_number,
                "regions": page_regions,
                "image_width": page.image_width or page.width or 0,
                "image_height": page.image_height or page.height or 0,
            })

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
    # Rendering — PAGE-XML (ADR 0004, per page)
    # ------------------------------------------------------------------

    @staticmethod
    def to_page_xml(data, page):
        """Render one page of revision data as a PAGE-XML 2019-07-15 document.

        The schema allows exactly one ``<Page>`` per ``<PcGts>``, so callers
        iterate ``revision_data()["pages"]`` and render each page separately
        (see ``page_xml_files``). Region text is the effective text, markers
        included literally — marker semantics are carried by the TEI export.
        """
        width = page.get("image_width") or 0
        height = page.get("image_height") or 0
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            f'<PcGts xmlns="{PAGE_XML_NS}" '
            f'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            f'xsi:schemaLocation="{PAGE_XML_NS} {PAGE_XML_NS}/pagecontent.xsd">',
            "  <Metadata>",
            "    <Creator>Document Structure Workbench</Creator>",
            f"    <Created>{data['created_at']}</Created>",
            '    <MetadataItem type="processingStep" name="revision">',
            f"      <Value>revision-{data['revision_id']}</Value>",
            "    </MetadataItem>",
            "  </Metadata>",
            f'  <Page imageFilename="{xml_escape(str(data["source_filename"]))}" '
            f'imageWidth="{int(width)}" imageHeight="{int(height)}">',
        ]
        for index, region in enumerate(page["regions"], start=1):
            tag = _PAGE_REGION_TAGS.get(region["type"], "TextRegion")
            type_attr = ""
            if tag == "TextRegion" and region["type"] in _PAGE_TEXT_REGION_TYPES:
                type_attr = f' type="{_PAGE_TEXT_REGION_TYPES[region["type"]]}"'
            coords = _page_coords(region, width, height)
            lines.append(f'    <{tag} id="r{index}"{type_attr}>')
            lines.append(f"      <Coords points=\"{coords}\"/>")
            text = (region.get("text") or "").strip()
            if text:
                lines.append("      <TextEquiv>")
                lines.append(f"        <Unicode>{xml_escape(text)}</Unicode>")
                lines.append("      </TextEquiv>")
            lines.append(f"    </{tag}>")
        lines.append("  </Page>")
        lines.append("</PcGts>")
        return "\n".join(lines) + "\n"

    @staticmethod
    def page_xml_files(data):
        """Yield ``(filename, page_xml)`` per page of a revision."""
        for index, page in enumerate(data["pages"], start=1):
            yield f"page-{page['page_number']:04d}.xml", DocumentExportService.to_page_xml(data, page)

    @staticmethod
    def page_xml_payload(data):
        """Return ``(payload, content_type, filename)`` for a PAGE-XML export.

        Single-page revisions produce one XML file; multi-page revisions a ZIP
        of one PAGE file per page, because the schema forbids multiple
        ``<Page>`` elements per document.
        """
        base = slugify(data.get("external_id")) or f"document-{data.get('revision_id')}"
        files = list(DocumentExportService.page_xml_files(data))
        if len(files) == 1:
            return files[0][1], FORMATS["xml"][0], f"{base}{FORMATS['xml'][1]}"
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in files:
                archive.writestr(name, content)
        buffer.seek(0)
        return buffer.getvalue(), "application/zip", f"{base}-pages.zip"

    # ------------------------------------------------------------------
    # Rendering — TEI (ADR 0004, minimal profile)
    # ------------------------------------------------------------------

    @staticmethod
    def to_tei(data):
        """Render revision data as a minimal TEI document.

        Pages become ``<div type="page">``, titles ``<head>``, lists
        ``<list>/<item>``, other regions ``<p>``. Editorial markers
        (ADR 0002) are converted inline to ``<unclear>``, ``<gap>``, and
        ``<choice>``. This is a deliberately minimal profile, not a critical
        edition (see ADR 0004).
        """
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<TEI xmlns="http://www.tei-c.org/ns/1.0">',
            "  <teiHeader>",
            "    <fileDesc>",
            "      <titleStmt>",
            f"        <title>{xml_escape(str(data['source_filename']))}</title>",
            "      </titleStmt>",
            "      <publicationStmt>",
            f"        <p>Exported from Document Structure Workbench (revision {data['revision_id']}, {xml_escape(data['created_at'])}).</p>",
            "      </publicationStmt>",
            "      <sourceDesc>",
            f"        <p>{xml_escape(str(data['external_id']))} — {xml_escape(str(data['source_filename']))}</p>",
            "      </sourceDesc>",
            "    </fileDesc>",
            "  </teiHeader>",
            "  <text>",
            "    <body>",
        ]
        for page in data["pages"]:
            lines.append(f"      <div type=\"page\" n=\"{page['page_number']}\">")
            for region in page["regions"]:
                lines.extend(_region_tei_lines(region))
            lines.append("      </div>")
        lines.append("    </body>")
        lines.append("  </text>")
        lines.append("</TEI>")
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------
    # Renderer dispatch
    # ------------------------------------------------------------------

    @staticmethod
    def renderer(fmt):
        """Return the single-document renderer for a format.

        PAGE-XML (``xml``) has no single-document renderer (it is per page by
        schema); use ``page_xml_payload`` for it instead.
        """
        renderers = {
            "txt": DocumentExportService.to_text,
            "md": DocumentExportService.to_markdown,
            "tei": DocumentExportService.to_tei,
        }
        try:
            return renderers[fmt]
        except KeyError:
            raise ValueError(f"Unsupported export format '{fmt}'. Use one of: {', '.join(sorted(renderers))}.") from None

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

        PAGE-XML archives one file *per page* (schema constraint); every other
        format archives one file per document as before.
        """
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
                prefix = f"{index:02d}-{safe}"
                if fmt == "xml":
                    for name, content in DocumentExportService.page_xml_files(data):
                        archive.writestr(f"{prefix}/{name}", content)
                    manifest.append(f"- `{prefix}/` — {data['source_filename']} ({_('revision')} {data['revision_id']})")
                else:
                    name = f"{prefix}{ext}"
                    archive.writestr(name, DocumentExportService.renderer(fmt)(data))
                    manifest.append(f"- `{name}` — {data['source_filename']} ({_('revision')} {data['revision_id']})")
            archive.writestr(f"INDEX{ext}", "\n".join(manifest).rstrip() + "\n")
        buffer.seek(0)
        return buffer.getvalue()

    @staticmethod
    def render_singlefile(project, fmt):
        """Render a project export as one concatenated file (index + every document).

        The mirror of ``render_zip`` for the "I want one greppable file" case:
        a table of contents followed by every document's rendered text, separated
        by clear dividers. Only available for the text formats — XML documents
        cannot be concatenated into one valid file.
        """
        if fmt not in SINGLE_FILE_FORMATS:
            raise ValueError(f"Format '{fmt}' cannot be exported as a single file.")
        renderer = DocumentExportService.renderer(fmt)
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


# ----------------------------------------------------------------------
# PAGE-XML / TEI helpers (module-private, ADR 0004)
# ----------------------------------------------------------------------

PAGE_XML_NS = "http://schema.primaresearch.org/PAGE/gts/pagecontent/2019-07-15"

# PAGE-XML element per effective region type; unmapped types stay TextRegion.
_PAGE_REGION_TAGS = {"table": "TableRegion", "figure": "ImageRegion"}
# ``type`` attribute for TextRegion elements where PAGE has a specific value.
_PAGE_TEXT_REGION_TYPES = {"title": "heading", "header": "page-number"}


def _page_coords(region, width, height):
    """Integer pixel ``points`` attribute from a page-relative [0,1] bbox."""
    w, h = max(float(width), 0.0), max(float(height), 0.0)
    x0 = round(max(min(region["left"], 1.0), 0.0) * w)
    x1 = round(max(min(region["right"], 1.0), 0.0) * w)
    y0 = round(max(min(region["top"], 1.0), 0.0) * h)
    y1 = round(max(min(region["bottom"], 1.0), 0.0) * h)
    return f"{x0},{y0} {x1},{y0} {x1},{y1} {x0},{y1}"


# Editorial markers (ADR 0002) as TEI inline markup.
# ``word[?]`` (or a standalone ``[?]``) -> <unclear>; ``[illegible]``/``[...]``
# -> <gap>; ``abbrev[expansion]`` -> <choice><abbr/><expan/></choice>.
_TEI_MARKER_RE = re.compile(
    r"(\S+)?\[\?\]|\[(?:illegible|\.\.\.)\]|(\w[\w.]*)\[([A-Za-z][\w]*)\]",
    re.IGNORECASE,
)


def _tei_inline(text):
    """Escape plain text, converting ADR 0002 markers to TEI elements."""
    parts = []
    position = 0
    for match in _TEI_MARKER_RE.finditer(text):
        parts.append(xml_escape(text[position:match.start()]))
        expansion = match.group(3)
        if expansion is not None:
            parts.append(
                f"<choice><abbr>{xml_escape(match.group(2))}</abbr>"
                f"<expan>{xml_escape(expansion)}</expan></choice>"
            )
        elif match.group(0).endswith("[?]"):
            word = match.group(1)
            if word:
                parts.append(f"<unclear>{xml_escape(word)}</unclear>")
            else:
                parts.append('<unclear reason="uncertain"/>')
        else:
            parts.append('<gap reason="illegible"/>')
        position = match.end()
    parts.append(xml_escape(text[position:]))
    return "".join(parts)


def _region_tei_lines(region):
    """TEI lines for one region of the minimal profile."""
    rtype = region["type"]
    text = (region.get("text") or "").strip()
    if not text:
        return []
    if rtype == "title":
        return [f"        <head>{_tei_inline(text)}</head>"]
    if rtype == "list":
        lines = ["        <list>"]
        lines += [f"          <item>{_tei_inline(line.strip())}</item>" for line in text.splitlines() if line.strip()]
        lines.append("        </list>")
        return lines
    if rtype == "table":
        # Structured table markup remains a documented gap (as for txt/md);
        # the effective table text is preserved inside a typed division.
        return ["        <div type=\"table\">", f"          <p>{_tei_inline(text)}</p>", "        </div>"]
    return [f"        <p>{_tei_inline(text)}</p>"]
