# ADR 0002: Adopt editorial uncertainty markers as the correction convention

## Status

Accepted.

## Context

Corrected text in DSW is currently opaque about editorial certainty: a curator
can fix a region's text, but the result cannot express "I am unsure of this
reading", "there is text here I cannot read", or "the source abbreviates and I
resolved it". Diplomatic transcription practice in digital humanities
scholarship (and formalized in TEI) has long-standing conventions for exactly
these distinctions, and co-ocr-htr demonstrated that a small marker set is
sufficient for a client-side tool to build validation, search, and export
features on top.

Adopting the conventions does not require users to learn them (see
Consequences): the markers are the *storage and export* representation, while
the UI presents human-language affordances. Novice reviewers click "can't read
this"; the data records `[illegible]`; the TEI export renders `<gap>`.

## Decision

Adopt three editorial markers in corrected region text, documented in
`docs/editorial-markers.md`:

- `[?]` — uncertain reading (maps to TEI `<unclear>`)
- `[illegible]` (or `[...]`) — unreadable text present at this position
  (maps to TEI `<gap reason="illegible">`)
- `abbrev[expansion]` — abbreviation with editorial resolution
  (maps to TEI `<choice><abbr/><expan/></choice>`)

The markers are a plain-text convention inside the normal correction text.
They require no schema change: a marker is simply part of corrected region
text, and all existing correction/revert/search semantics apply unchanged.
The candidate validation pass (ADR 0003) detects them, and the PAGE-XML/TEI
export (ADR 0004) converts them to standard elements.

## Consequences

- Corrected text is self-describing about editorial certainty without a
  separate annotation model.
- The correction UI should offer button/menu affordances that insert the
  markers, plus a legend, so no one must learn the syntax. Raw-marker display
  is an expert toggle, not the default presentation.
- Markers flow through search like ordinary text today; "list uncertain
  readings" becomes a search feature later without data migration.
- Machine-generated candidates may also contain marker-shaped strings; the
  validation pass treats them as uncertainty signals regardless of origin.
- Only these three markers are standardized. Free-form bracketed editorial
  notes remain possible but are not interpreted.
