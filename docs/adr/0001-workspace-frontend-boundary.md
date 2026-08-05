# ADR 0001: Keep Django and native modules as the workspace boundary

## Status

Accepted for the current workbench phase.

## Context

The application has Django-rendered HTML, HTMX polling, immutable domain state,
and two interaction-heavy workspaces. The immediate reliability problems were
API gaps, ambiguous URL state, stale asynchronous responses, and weak browser
diagnostics. A frontend framework would add a second state/authorization
implementation before those contracts are stable.

## Decision

Remain with Django templates, HTMX, and small native ES modules. Keep domain
authorization, validation, transitions, corrections, OCR acceptance, and
auditing in Django services. Use stable semantic attributes and a bounded
diagnostics manifest for browser agents.

Use TypeScript only if measured module complexity or static-analysis defects
justify it. Do not introduce React/Vue/Next.js in this phase.

## Evidence and reassessment trigger

The extracted modules currently cover focused behavior (API errors, upload
drop handling, scan zoom/pan/filtering, keyboard submission, and diagnostics)
without requiring a component runtime. Reassess after browser scenarios cover
OCR acceptance, request races, responsive layouts, and correction conflicts.
A typed component island becomes reasonable only if those scenarios expose
cross-workspace state coordination that native modules cannot keep explicit.

## Consequences

- One authoritative server-side domain model remains intact.
- Progressive-enhancement fallbacks remain available.
- Browser agents receive stable links, controls, URL state, and diagnostics.
- Native modules require disciplined cancellation/history tests as complexity grows.
