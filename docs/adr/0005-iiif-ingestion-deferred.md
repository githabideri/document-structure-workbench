# ADR 0005: Defer IIIF ingestion

## Status

Accepted (deferral with an explicit trigger).

## Context

co-ocr-htr demonstrated that IIIF (Image Interoperability Framework) manifests
from libraries and archives can serve as an ingestion source: a manifest is a
standardized, machine-readable description of a digitized object and its
ordered page images — structurally close to `SourceDocument` + `Page`. IIIF
would let DSW ingest external digitized collections without manual scan
downloads, with stable institutional URLs as provenance.

However, DSW's current ingestion path starts from uploaded scans of material
digitized in-house. An IIIF ingester would be unused plumbing today: it needs
manifest → source mapping, remote-image fetching and caching through the
processing pipeline, and decisions about provenance for material DSW does not
control.

## Decision

Do not implement IIIF ingestion now. Revisit when an actual requirement
appears to ingest external, already-digitized collections by manifest URL.

To keep the later path cheap, hold one boundary constraint: ingestion creates
a `SourceDocument` with pages and provenance through the existing processor
pipeline; no processor or downstream consumer may assume the bytes arrived
via upload. A future IIIF ingester is then a new entry point into that
boundary, not a rework of it.

## Consequences

- No IIIF code, dependency, or configuration is added in this phase.
- Export-side IIIF (serving DSW pages as an IIIF endpoint) is a separate,
  also-unaddressed question; neither direction is implied by this ADR.
- When the trigger fires, the work is an ingester plus image-fetch/cache
  policy; `SourceDocument`/`Page` models are expected to be reusable as-is.
