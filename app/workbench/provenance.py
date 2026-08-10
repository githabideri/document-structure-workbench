"""Presentation-layer provenance for the document workspace inspector.

The workspace shows a compact version/provenance rail over a selected region.
This does **not** introduce a competing persistence model: it combines the
existing ``RegionCorrection`` and ``OcrRequest`` rows (section 13 of the plan)
into a single, ordered, selectable list suitable for the inspector template.

Text-bearing items (imported machine text, manual text corrections, completed
HTR and vision candidates) become selectable "versions" that can be compared
against the current effective transcription. Non-text corrections (type change,
suppress/restore) and reverts are surfaced as compact activity entries under
progressive disclosure.
"""
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from .models import OcrRequest


def _user_label(user):
    if not user:
        return _("system")
    return user.get_username()


def _ts(value):
    # Keep raw datetimes so templates can use |timesince; None stays None.
    return value


def _current_summary(region, accepted=None):
    """Describe the effective transcription and where it came from."""
    text = region.effective_text
    if accepted and accepted.status == "active" and accepted.operation == "text":
        label = accepted.created_by and _("Manual") or _("Manual")
        source = "manual"
        updated = accepted.created_at
    elif accepted is None:
        # No active text correction → machine-imported text.
        source = "imported"
        label = _("Imported")
        updated = None
    else:
        source = "manual"
        label = _("Manual")
        updated = accepted.created_at if accepted else None
    return {
        "text": text,
        "source": source,
        "source_label": str(label),
        "updated_at": _ts(updated),
    }


def build_region_versions(region):
    """Assemble the version rail + activity for one region.

    Returns a dict:
        current:        effective transcription summary
        entries:        selectable text versions (newest first)
        activity:       non-text corrections (type/suppress) + reverts
        any_pending:    True if any recognition candidate is queued/processing
    """
    text_corrections = list(
        region.corrections.filter(operation="text").select_related("created_by").order_by("-created_at", "-id")
    )
    active_text = next((c for c in text_corrections if c.status == "active"), None)
    current = _current_summary(region, active_text)

    entries = []

    # Imported machine transcription (the immutable raw text).
    entries.append({
        "id": f"imported-{region.pk}",
        "kind": "imported",
        "label": str(_("Imported")),
        "sublabel": str(_("machine extraction")),
        "provider": getattr(region.job, "processor", "") or "docling",
        "model": "",
        "pipeline_id": "",
        "text": region.text,
        "state": "completed",
        "status": "",
        "accepted": active_text is None,
        "created_by": None,
        "created_at": _ts(getattr(region.job, "created_at", None)) if hasattr(region.job, "created_at") else None,
        "finished_at": None,
        "selectable": bool(region.text),
        "accept_url": "",
        "detail_url": "",
    })

    # Manual text corrections (each is a selectable historical version).
    for corr in text_corrections:
        entries.append({
            "id": f"corr-{corr.pk}",
            "kind": "manual",
            "label": str(_("Manual")),
            "sublabel": corr.reason or "",
            "provider": "",
            "model": "",
            "pipeline_id": "",
            "text": corr.after.get("text", ""),
            "state": "completed",
            "status": corr.status,
            "accepted": corr.status == "active",
            "created_by": _user_label(corr.created_by),
            "created_at": _ts(corr.created_at),
            "finished_at": _ts(corr.created_at),
            "selectable": True,
            "accept_url": "",
            "detail_url": "",
        })

    # Recognition candidates (vision + HTR), newest first.
    for item in region.ocr_requests.all().order_by("-created_at", "-id"):
        is_htr = item.provider == "htr"
        kind = "htr" if is_htr else "vision"
        if is_htr:
            label = str(_("HTR"))
            sublabel = (item.metadata or {}).get("pipeline_id", "")
            model = sublabel
        else:
            label = str(_("Vision"))
            sublabel = item.model or item.provider
            model = item.model or item.provider
        text = item.candidate_text if item.state == "completed" else ""
        entries.append({
            "id": f"ocr-{item.pk}",
            "kind": kind,
            "label": label,
            "sublabel": sublabel,
            "provider": item.provider,
            "model": model,
            "pipeline_id": (item.metadata or {}).get("pipeline_id", "") if is_htr else "",
            "text": text,
            "state": item.state,
            "status": "",
            "accepted": bool(item.accepted_correction_id),
            "created_by": _user_label(item.created_by),
            "created_at": _ts(item.created_at),
            "finished_at": _ts(item.finished_at),
            "selectable": item.state == "completed" and bool(text),
            "accept_url": (
                reverse("accept_region_htr", args=[item.pk]) if is_htr
                else reverse("accept_ocr_request", args=[item.pk])
            ),
            "detail_url": (reverse("api_htr_run_detail", args=[item.pk]) if is_htr else ""),
        })

    # Newest first; imported stays conceptually oldest but keep it last for
    # chronological ordering against corrections/candidates.
    def sort_key(entry):
        return entry["created_at"] or ""

    chronological = [e for e in entries if e["kind"] != "imported"]
    chronological.sort(key=sort_key, reverse=True)
    chronological.append(entries[0])  # imported last

    # Non-text corrections (type change, suppress/restore) as activity entries.
    activity = []
    for corr in region.corrections.exclude(operation="text").select_related("created_by", "reverted_by").order_by("-created_at", "-id"):
        label_map = {"type": _("Region type"), "suppress": _("Suppression"), "note": _("Note")}
        detail = ""
        if corr.operation == "type":
            detail = corr.after.get("region_type", "")
        elif corr.operation == "suppress":
            detail = str(_("suppressed")) if corr.after.get("suppressed") else str(_("restored"))
        elif corr.operation == "note":
            detail = corr.after.get("note", "")
        activity.append({
            "id": f"act-{corr.pk}",
            "label": str(label_map.get(corr.operation, corr.operation)),
            "detail": detail,
            "status": corr.status,
            "created_by": _user_label(corr.created_by),
            "created_at": _ts(corr.created_at),
            "reverted_at": _ts(corr.reverted_at),
            "reverted_by": _user_label(corr.reverted_by) if corr.reverted_by_id else None,
        })

    any_pending = region.ocr_requests.filter(state__in={"queued", "processing"}).exists()
    return {"current": current, "entries": chronological, "activity": activity, "any_pending": any_pending}


def first_pending_detail_url(region):
    """URL of the most recent HTR run that is still pending, for line polling."""
    item = (
        OcrRequest.objects.filter(region=region, provider="htr", state__in={"queued", "processing"})
        .order_by("-created_at", "-id").first()
    )
    return reverse("api_htr_run_detail", args=[item.pk]) if item else ""
