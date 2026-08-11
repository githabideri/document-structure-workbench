"""Presentation-layer provenance for the document workspace inspector.

The workspace shows a compact version/provenance rail over a selected region.
This does **not** introduce a competing persistence model: it combines the
existing ``RegionCorrection`` and ``OcrRequest`` rows (section 13 of the plan)
into a single, ordered, selectable list suitable for the inspector template.

Exactly one provenance entry is ``current`` — the one matching the region's
single active effective text correction. Its source is labelled truthfully:

* accepted HTR candidate   -> ``HTR · <pipeline>``
* accepted Vision candidate -> ``Vision · <provider/model>``
* direct manual edit       -> ``Manual · <username>``
* no effective correction  -> ``Imported``

Reverting an accepted candidate's correction un-accepts the candidate: it stays
historical, can be selected again, and re-accepting creates a *fresh* correction
(never a recycled one).
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


def _current_summary(region, active_correction, active_ocr):
    """Describe the effective transcription and where it came from exactly once.

    ``active_correction`` is the single active text correction (or None).
    ``active_ocr`` is the recognition request that produced it (or None).
    """
    text = region.effective_text
    if active_correction is None:
        # No active text correction → machine-imported text.
        return {
            "text": text, "source": "imported",
            "source_label": str(_("Imported")), "updated_at": _ts(None),
        }
    if active_ocr is not None:
        if active_ocr.provider == "htr":
            pipeline = (active_ocr.metadata or {}).get("pipeline_id", "")
            source_label = f"{_('HTR')} · {pipeline}" if pipeline else str(_("HTR"))
            source = "htr"
        else:
            model = active_ocr.model or active_ocr.provider or ""
            source_label = f"{_('Vision')} · {model}" if model else str(_("Vision"))
            source = "vision"
        return {
            "text": text, "source": source, "source_label": source_label,
            "updated_at": _ts(active_ocr.finished_at or active_correction.created_at),
        }
    user = _user_label(active_correction.created_by)
    source_label = f"{_('Manual')} · {user}" if user and str(user) != str(_("system")) else str(_("Manual"))
    return {
        "text": text, "source": "manual", "source_label": source_label,
        "updated_at": _ts(active_correction.created_at),
    }


def build_region_versions(region):
    """Assemble the version rail + activity for one region.

    Returns a dict:
        current:        effective transcription summary (one authoritative value)
        entries:        selectable text versions (newest first)
        activity:       non-text corrections (type/suppress) + reverts
        any_pending:    True if any recognition candidate is queued/processing
    """
    text_corrections = list(
        region.corrections.filter(operation="text").select_related("created_by").order_by("-created_at", "-id")
    )
    ocr_items = list(region.ocr_requests.all().order_by("-created_at", "-id"))
    ocr_by_correction = {
        item.accepted_correction_id: item
        for item in ocr_items if item.accepted_correction_id
    }
    active_text = next((c for c in text_corrections if c.status == "active"), None)
    active_ocr = ocr_by_correction.get(active_text.pk) if active_text else None
    current = _current_summary(region, active_text, active_ocr)
    current_id = active_text.pk if active_text else None

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
        "accepted": current_id is None,
        "created_by": None,
        "created_at": _ts(getattr(region.job, "created_at", None)) if hasattr(region.job, "created_at") else None,
        "finished_at": None,
        "selectable": bool(region.text),
        "accept_url": "",
        "detail_url": "",
    })

    # Recognition candidates (vision + HTR), newest first.
    for item in ocr_items:
        is_htr = item.provider == "htr"
        kind = "htr" if is_htr else "vision"
        if is_htr:
            label = str(_("HTR"))
            pipeline = (item.metadata or {}).get("pipeline_id", "")
            sublabel = pipeline
            model = pipeline
        else:
            label = str(_("Vision"))
            sublabel = item.model or item.provider
            model = item.model or item.provider
        is_current = current_id is not None and item.accepted_correction_id == current_id
        entries.append({
            "id": f"ocr-{item.pk}",
            "kind": kind,
            "label": label,
            "sublabel": sublabel,
            "provider": item.provider,
            "model": model,
            "pipeline_id": pipeline if is_htr else "",
            "text": item.candidate_text if item.state == "completed" else "",
            "state": item.state,
            "status": "",
            "accepted": is_current,
            "created_by": _user_label(item.created_by),
            "created_at": _ts(item.created_at),
            "finished_at": _ts(item.finished_at),
            "selectable": item.state == "completed" and bool(item.candidate_text),
            "accept_url": (
                reverse("accept_region_htr", args=[item.pk]) if is_htr
                else reverse("accept_ocr_request", args=[item.pk])
            ),
            "detail_url": (reverse("api_htr_run_detail", args=[item.pk]) if is_htr else ""),
        })

    # Manual text corrections that are NOT the result of an accepted recognition
    # candidate (those are already represented by their ocr-* entry above, which
    # carries the correct HTR/Vision label). Only truly manual edits get their
    # own entry.
    accepted_ocr_correction_ids = {item.accepted_correction_id for item in ocr_items if item.accepted_correction_id}
    for corr in text_corrections:
        if corr.pk in accepted_ocr_correction_ids:
            continue
        user = _user_label(corr.created_by)
        label = f"{_('Manual')} · {user}" if user and str(user) != str(_("system")) else str(_("Manual"))
        entries.append({
            "id": f"corr-{corr.pk}",
            "kind": "manual",
            "label": label,
            "sublabel": corr.reason or "",
            "provider": "",
            "model": "",
            "pipeline_id": "",
            "text": corr.after.get("text", ""),
            "state": "completed",
            "status": corr.status,
            "accepted": corr.pk == current_id,
            "created_by": _user_label(corr.created_by),
            "created_at": _ts(corr.created_at),
            "finished_at": _ts(corr.created_at),
            "selectable": current_id is not None,  # manual edits are always selectable
            "accept_url": "",
            "detail_url": "",
        })

    # Newest first; imported stays conceptually oldest but keep it last.
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

    any_pending = any(item.state in {"queued", "processing"} for item in ocr_items)
    return {
        "current": current,
        "current_id": current_id,
        "entries": chronological,
        "activity": activity,
        "any_pending": any_pending,
    }


def first_pending_detail_url(region):
    """URL of the most recent HTR run that is still pending, for line polling."""
    item = (
        OcrRequest.objects.filter(region=region, provider="htr", state__in={"queued", "processing"})
        .order_by("-created_at", "-id").first()
    )
    return reverse("api_htr_run_detail", args=[item.pk]) if item else ""
