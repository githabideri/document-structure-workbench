"""Revision-aware lexical search and passage indexing."""
import re
import unicodedata

from .models import PageRegion, SearchPassage, TableCandidate


def normalize_text(value):
    value = unicodedata.normalize("NFKC", value or "").casefold()
    return re.sub(r"\s+", " ", value).strip()


def rebuild_revision_index(document):
    """Rebuild passages for exactly one immutable processed revision."""
    job = getattr(document, "processing_job", None)
    source = job.source_document if job else None
    if not job or not source or not source.collection:
        return 0
    SearchPassage.objects.filter(processed_revision=document).delete()
    rows = []
    ordinal = 0
    for region in PageRegion.objects.filter(page__document=document).select_related("page"):
        text = region.effective_text
        if not text or region.is_suppressed:
            continue
        passage_type = "heading" if region.effective_region_type == "title" else "region"
        rows.append(SearchPassage(
            project=source.collection, source_document=source,
            processed_revision=document, processing_job=job,
            page=region.page, page_region=region, passage_type=passage_type,
            text=text, normalized_text=normalize_text(text), ordinal=ordinal,
        ))
        ordinal += 1
    for table in TableCandidate.objects.filter(document=document).select_related("page"):
        label = table.stable_table_id
        if label:
            rows.append(SearchPassage(
                project=source.collection, source_document=source,
                processed_revision=document, processing_job=job,
                page=table.page, passage_type="table", text=label,
                normalized_text=normalize_text(label), ordinal=ordinal,
            ))
            ordinal += 1
    SearchPassage.objects.bulk_create(rows)
    return len(rows)


def search_project(projects, query, *, source_ids=None, limit=20):
    """Search authorized projects using normalized token/phrase matching."""
    normalized = normalize_text(query)
    if not normalized:
        return []
    qs = SearchPassage.objects.filter(project__in=projects, normalized_text__contains=normalized)
    if source_ids:
        qs = qs.filter(source_document_id__in=source_ids)
    return list(qs.select_related("source_document", "processed_revision", "page", "page_region")[:limit])
