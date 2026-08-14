# Editorial uncertainty markers

Corrected region text in DSW uses three standardized editorial markers to
express certainty distinctions that diplomatic transcription practice expects.
They follow the conventions of scholarly editions and map directly to TEI
elements on export (see ADR 0002 and ADR 0004).

| Marker | Meaning | TEI export |
|---|---|---|
| `[?]` | Uncertain reading — the editor transcribed something but is not confident. Attached after a word (`word[?]`) it marks that word; standalone it marks the preceding position. | `<unclear>` |
| `[illegible]` (or `[...]`) | Text is present on the page at this position but could not be read. | `<gap reason="illegible">` |
| `abbrev[expansion]` | The source abbreviates (`abbrev`); the editor resolved it (`expansion`). E.g. `d.[omi]ni`. | `<choice><abbr>abbrev</abbr><expan>expansion</expan></choice>` |

Notes:

- Users never need to type these markers: the correction UI offers
  affordances ("can't read this", "mark uncertain", "resolve abbreviation")
  that insert them. Raw-marker display is an expert toggle.
- Any other bracketed text is ordinary text — only these three patterns are
  interpreted by validation (ADR 0003) and export (ADR 0004).
- Machine-generated candidates may also contain marker-shaped strings; the
  validation pass treats them as uncertainty signals regardless of origin.
