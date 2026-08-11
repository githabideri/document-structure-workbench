"""Page-level inspector features: full page text, page OCR candidates, tables.

Presentation helpers for the "Page" tab of the workspace inspector. They
assemble already-stored immutable extraction artifacts — no persistence or
semantic model changes.
"""
from .models import ProcessingArtifact
from .textutil import page_text_to_plain


def page_full_text(page):
    """Immutable extracted full-page text (plain), or ``""`` if unavailable."""
    if page is None:
        return ""
    job = page.document.processing_job
    if job is None:
        return ""
    artifact = ProcessingArtifact.objects.filter(
        job_id=job.id,
        artifact_type="page_text",
        page_number=page.page_number,
    ).values_list("data", flat=True).first()
    if isinstance(artifact, dict) and artifact.get("text"):
        return page_text_to_plain(str(artifact["text"]))
    # Older revisions may lack a page_text artifact; reconstruct deterministically
    # from the immutable region text in reading order.
    parts = [
        r.text for r in page.regions.all().order_by("top", "left")
        if (r.text or "").strip()
    ]
    reconstructed = "\n".join(parts)
    if reconstructed:
        return reconstructed
    return ""


def page_ocr_candidates(page, limit=10):
    """Completed/queued visual-OCR candidates for an entire page (excl. HTR)."""
    if page is None:
        return []
    return list(
        page.ocr_requests.exclude(provider="htr").order_by("-created_at", "-id")[:limit]
    )


def page_any_pending(candidates):
    return any(c.state in {"queued", "processing"} for c in candidates)


def page_tables(page):
    """Safe-Html table candidates for the page (bleach-cleaned for display)."""
    if page is None:
        return []
    import bleach

    from .models import TableCandidate
    tables = list(
        TableCandidate.objects.filter(document_id=page.document_id, page=page)
        .select_related("page").prefetch_related("extractions")
    )
    for table in tables:
        for extraction in table.extractions.all():
            if extraction.raw_html:
                extraction.safe_html = bleach.clean(
                    extraction.raw_html,
                    tags=["table", "thead", "tbody", "tr", "th", "td", "caption", "p", "br"],
                    attributes={"th": ["colspan", "rowspan"], "td": ["colspan", "rowspan"]},
                    strip=True,
                )
    return tables
