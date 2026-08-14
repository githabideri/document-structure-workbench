"""Candidate validation — deterministic triage over OCR/HTR candidates.

Implements ADR 0003. The pass merges two signal sources into one persisted
verdict on ``OcrRequest.metadata["validation"]``:

1. Provider confidence, extracted from the frozen HTR result contract
   (``regions[].lines[].confidence``, 0–1) retained on ``raw_response``.
   Providers without confidence (Docling, vision OCR) yield no signal here.
2. Deterministic text rules that work on any candidate: empty output, control
   characters, replacement characters, and the editorial uncertainty markers
   of ADR 0002.

The verdict is *metadata on the immutable candidate*. It never mutates
candidate text or raw responses, and it creates no acceptance semantics —
acceptance remains an explicit human correction. ``needs_review`` is advisory.
"""
import re

from django.utils.translation import gettext_lazy as _

#: Bumped when rule semantics change so persisted verdicts stay interpretable.
VALIDATION_VERSION = "candidate-validation-v1"

#: Mean line confidence at or below this marks the candidate for review.
LOW_CONFIDENCE_THRESHOLD = 0.6

# --- Deterministic rules (ADR 0002 editorial markers + OCR artifacts) ---

#: ``[?]`` — uncertain reading.
UNCERTAIN_MARKER_RE = re.compile(r"\[\?\]")
#: ``[illegible]`` or ``[...]`` — unreadable text present.
ILLEGIBLE_MARKER_RE = re.compile(r"\[(?:illegible|\.\.\.)\]", re.IGNORECASE)
#: ``abbrev[expansion]`` — abbreviation with editorial resolution. The
#: abbreviation may end in punctuation (``d.[omi]ni``).
ABBREVIATION_RE = re.compile(r"\w[\w.]*\[[A-Za-z][\w]*\]")

#: Codes that alone justify review. Marker counts for abbreviations and stats
#: are recorded but advisory-only.
_REVIEW_REASONS = frozenset(
    {
        "empty_candidate",
        "uncertain_marker",
        "illegible_marker",
        "control_characters",
        "replacement_characters",
        "low_confidence_lines",
        "low_mean_confidence",
    }
)

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


#: Human-readable labels for verdict reason codes (UI/tooltip use).
REASON_LABELS = {
    "empty_candidate": _("empty candidate text"),
    "uncertain_marker": _("contains uncertain readings ([?])"),
    "illegible_marker": _("contains unreadable positions ([illegible])"),
    "abbreviation_marker": _("contains resolved abbreviations"),
    "control_characters": _("contains control characters"),
    "replacement_characters": _("contains replacement characters"),
    "double_spaces": _("contains double spaces"),
    "line_count": _("line statistics"),
    "low_confidence_lines": _("low-confidence HTR lines"),
    "low_mean_confidence": _("low mean HTR confidence"),
}


def reason_labels(reasons):
    """Translate verdict reason codes to display labels (unknown codes kept)."""
    return [str(REASON_LABELS.get(code, code)) for code in reasons or []]


def verdict_summary(verdict):
    """Template-friendly summary of a persisted verdict (``None``-safe)."""
    if not isinstance(verdict, dict):
        return {"needs_review": False, "labels": []}
    return {
        "needs_review": bool(verdict.get("needs_review")),
        "labels": reason_labels(verdict.get("reasons")),
    }


def line_confidences(raw_response):
    """Extract per-line confidences from a frozen HTR result, if present.

    Tolerates any provider shape: anything that is not the documented
    ``{"regions": [{"lines": [{"confidence": 0..1}]}]}`` structure yields an
    empty list rather than an error.
    """
    confidences = []
    if not isinstance(raw_response, dict):
        return confidences
    regions = raw_response.get("regions")
    if not isinstance(regions, list):
        return confidences
    for region in regions:
        if not isinstance(region, dict):
            continue
        lines = region.get("lines")
        if not isinstance(lines, list):
            continue
        for line in lines:
            if not isinstance(line, dict):
                continue
            value = line.get("confidence")
            if isinstance(value, (int, float)) and 0.0 <= value <= 1.0:
                confidences.append(float(value))
    return confidences


def _text_counts(text):
    """Count deterministic rule hits in a candidate text."""
    return {
        "uncertain_marker": len(UNCERTAIN_MARKER_RE.findall(text)),
        "illegible_marker": len(ILLEGIBLE_MARKER_RE.findall(text)),
        "abbreviation_marker": len(ABBREVIATION_RE.findall(text)),
        "control_characters": len(_CONTROL_CHARS_RE.findall(text)),
        "replacement_characters": text.count("\ufffd"),
        "double_spaces": len(re.findall(r"  +", text)),
        "line_count": len([line for line in text.splitlines() if line.strip()]),
    }


def evaluate_candidate(text, raw_response=None):
    """Return the persisted verdict dict for one completed candidate.

    Pure function: no database access, no mutation. The worker calls it at
    completion time and stores the result under ``metadata["validation"]``.
    """
    text = text or ""
    counts = _text_counts(text)
    if not text.strip():
        counts["empty_candidate"] = 1

    reasons = [code for code, count in counts.items() if code in _REVIEW_REASONS and count]

    confidence_summary = None
    confidences = line_confidences(raw_response)
    if confidences:
        below = sum(1 for value in confidences if value <= LOW_CONFIDENCE_THRESHOLD)
        mean = sum(confidences) / len(confidences)
        confidence_summary = {
            "lines": len(confidences),
            "below_threshold": below,
            "mean": round(mean, 4),
        }
        if below:
            reasons.append("low_confidence_lines")
        if mean <= LOW_CONFIDENCE_THRESHOLD:
            reasons.append("low_mean_confidence")

    # Deduplicate while keeping deterministic order.
    reasons = list(dict.fromkeys(reasons))
    return {
        "version": VALIDATION_VERSION,
        "needs_review": bool(reasons),
        "reasons": reasons,
        "counts": counts,
        "confidence": confidence_summary,
    }
