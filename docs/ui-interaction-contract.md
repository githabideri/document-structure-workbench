# UI interaction contract

This document defines URL-backed state, local presentation state, and invariants for the Django/HTMX workbench. Django remains authoritative for permissions and domain transitions.

## Chat workspace

URL: `/chat/{thread_id}/?run={run_id}&evidence=S2`.

URL/server state: `thread_id`, selected run, selected evidence marker, and evidence-derived immutable document/revision/page/region.

Local state: question draft, submission state before acknowledgement, originating citation element, chat/evidence scroll positions.

The thread remains visible while evidence is opened. Evidence selection is a real link and uses `pushState` for meaningful selection. Initialization and URL normalization use `replaceState`. `popstate` restores the selected run/evidence; temporary panel, zoom, hover, and scroll changes do not create history entries.

## Document workspace

URL: `/documents/{document_id}/?revision={revision_id}&page={page}&region={region}&mode={mode}`.

URL/server state: immutable revision, page number, optional region, and shareable workspace mode.

Local state: region filter, suppressed visibility, zoom/pan, correction draft/editor, and transient OCR submission state. A selected OCR request may be URL-backed when it is the subject of a copied link.

## Invariants

1. Evidence identifies one immutable revision/page and optional region.
2. URL and visible selection agree after every navigation and reload.
3. A stale asynchronous response cannot replace a newer selection.
4. Selecting another region on the same page preserves zoom/pan.
5. Back/Forward and reload restore URL-backed state.
6. Closing evidence returns focus to its originating citation.
7. A failed submission preserves the draft.
8. Failed OCR never changes authoritative text; completed OCR remains a candidate until acceptance.
9. Acceptance creates or references a correction and is idempotent.
10. Permission failures are rendered as failures, never local success.
11. Archived objects remain addressable for historical references but disappear from active lists.
12. Filtered controls are not keyboard-focusable.

## Semantic and keyboard contract

Primary automation uses accessible role/name, real links/buttons/forms, and stable `data-ui-id` identifiers. Citations are links. `Ctrl/Cmd+Enter` submits a question, `Shift+Enter` inserts a newline, and Escape closes evidence and restores citation focus. Page/region selection, zoom, provider selection, OCR actions, correction actions, and evidence navigation are keyboard operable. Global shortcuts are disabled while focus is in editable controls.

## Diagnostics

`?debug_ui=1` is an opt-in test/development mode. The machine-readable manifest is available only to authenticated callers with the diagnostics scope and contains identifiers, semantic roles, visibility, focus/selection, bounds, scroll state, URL state, and bounded async metadata. It never includes full document text, tokens, or hidden reasoning.
