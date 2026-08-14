# ADR 0004: Export accepted revisions as PAGE-XML and TEI

## Status

Accepted.

## Context

DSW's export formats (plain text, Markdown) serve human reading and grepping
but not the wider historical-documents tool ecosystem. PAGE-XML
(2019-07-15) is the de-facto interchange format of Transkribus-based HTR
work; TEI is the standard for scholarly digital editions. Without them,
curated DSW output cannot round-trip into those workflows.

Source-of-truth rules already fixed by the export service: exports render the
*effective* (corrected) revision — corrections applied, suppressed regions
excluded — never raw machine output.

## Decision

Add two renderers over the existing `revision_data()` assembly, registered in
the same `FORMATS` map used by the web UI and the API:

- `xml` — PAGE-XML 2019-07-15: rendered *per page*, because the schema
  allows exactly one `<Page>` per `<PcGts>` document. A single-page revision
  exports one XML file; a multi-page revision exports a ZIP of one PAGE file
  per page. Region geometry is emitted as integer pixel `Coords` derived from
  the stored page-relative boxes and the page raster dimensions, with the
  effective text in `TextEquiv/Unicode`.
- `tei` — a deliberately minimal TEI profile: `teiHeader` provenance
  (filename, revision id, processor, export timestamp), pages as
  `<div type="page">`, title regions as `<head>`, lists as
  `<list>/<item>`, other regions as `<p>`. Editorial markers (ADR 0002) are
  converted inline: `[?]` → `<unclear>`, `[illegible]`/`[...]` →
  `<gap reason="illegible">`, `abbrev[expansion]` → `<choice>`.

Both renderers consume the same `revision_data()` output as txt/md; the
assembly layer is extended to carry region geometry, which does not change
existing renderers. Table regions export their text content; structured table
markup remains a documented gap (as it already is for txt/md).

PAGE-XML **import** is explicitly deferred: it requires decisions about
creating revisions from external geometry/text without a processing job.
Revisit when a Transkribus-ingestion requirement materializes. TEI import is
out of scope.

## Consequences

- DSW's curated output becomes consumable by Transkribus round-trips and TEI
  publication pipelines with attribution to the DSW correction layer.
- Region boxes must remain present in `revision_data`; suppressed regions
  still never appear.
- The TEI profile is minimal and documented as such — it is not a critical
  edition. Richer markup requires an explicit profile decision, not silent
  growth of the renderer.
- Marker conversion is lossy in one direction by design: markers not matching
  the ADR 0002 set are exported as literal bracket text.
- Interoperability pressure may later demand ALTO or PAGE profiles beyond
  2019-07-15; the renderer boundary keeps that additive.
