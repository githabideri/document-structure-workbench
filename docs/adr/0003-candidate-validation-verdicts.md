# ADR 0003: Persist a validation verdict on every OCR/HTR candidate

## Status

Accepted.

## Context

The product invariant says OCR/HTR output is always a *candidate* that a human
accepts through a correction. At scale this needs triage: a reviewer must know
*which* candidates deserve attention instead of accepting blind or reading
everything. Two signal sources already exist in DSW:

1. The frozen HTR contract (`htr-transcription-result-v1`) carries per-line
   `confidence` (mean of candidate scores, 0–1) and is retained verbatim on
   `OcrRequest.raw_response`.
2. The candidate *text* itself can be checked deterministically, regardless
   of provider: empty output, control characters, replacement characters, and
   the editorial markers of ADR 0002.

The Docling and vision-OCR paths currently provide no confidence values, so a
confidence-only triage would leave them uncovered. A deterministic rule layer
works on any text.

## Decision

Add a pure evaluation pass (`workbench/validation.py`) that merges both signal
sources into one persisted verdict. The OCR worker computes it when a request
completes and stores it under `OcrRequest.metadata["validation"]`:

    {
      "version": "candidate-validation-v1",
      "needs_review": bool,
      "reasons": ["empty_candidate", ...],
      "counts": {rule_code: count, ...},
      "confidence": {"lines": n, "below_threshold": k, "mean": x} | null
    }

Design points:

- The verdict is *metadata on the immutable candidate*. It never mutates
  candidate text, raw responses, or region state, and it creates no acceptance
  semantics of its own — acceptance remains a human correction.
- Line confidences are extracted only from the frozen HTR result shape;
  providers without confidence simply yield `confidence: null` and are judged
  on text rules alone.
- Thresholds are constants in the module, not per-request configuration,
  until triage experience justifies tuning.
- Verdicts are computed once at completion time, so a later "needs review"
  filter in the workspace UI is a free query over `metadata`.

An LLM-as-judge layer is deliberately *not* included: the existing per-region
OCR rerun already plays that role with better provenance. Revisit if triage
quality proves insufficient with the deterministic layer.

## Consequences

- Every new completed candidate carries a review triage verdict; existing
  rows have none and may be backfilled by re-evaluating stored `raw_response`
  and `candidate_text` in a management command if needed.
- `needs_review` is advisory. Reviewers can accept flagged and unflagged
  candidates alike; nothing is blocked.
- The UI obligation is a follow-up: surface the verdict and a filter in the
  workspace. This ADR only establishes the data.
- Rule changes bump `version` so verdicts stay interpretable.
