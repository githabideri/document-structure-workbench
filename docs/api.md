# API v1 contract

All routes except health require `Authorization: Bearer <ApiToken>`. Every response produced by the authenticated API includes a `request_id`. Tokens must carry the operation scope and the identity must have project access through the centralized policy.

## Workbench inspection

`GET /api/v1/documents/` lists active source uploads. A source record is the uploaded document; each processing attempt may produce one immutable processed revision.

`GET /api/v1/documents/{source_id}/` returns the source and its revisions. Revision/page routes are:

- `GET /api/v1/documents/{source_id}/revisions/`
- `GET /api/v1/documents/{source_id}/revisions/{revision_id}/`
- `GET /api/v1/documents/{source_id}/revisions/{revision_id}/pages/`
- `GET /api/v1/documents/{source_id}/revisions/{revision_id}/pages/{page_number}/`
- `GET /api/v1/pages/{page_id}/regions/`
- `GET /api/v1/regions/{region_id}/`
- `GET /api/v1/regions/{region_id}/corrections/`

Page responses contain effective corrected text, original text, normalized geometry, source dimensions where known, tables, OCR candidates, and an authenticated image URL. Clients do not reconstruct correction state.

## Mutations

- `POST /api/v1/projects/` creates a project for an administrator with `projects:write`.
- `POST /api/v1/projects/{id}/archive/` archives/restores a project (`{"archived": true}`).
- `POST /api/v1/documents/{source_id}/archive/` archives/restores a source upload.
- `POST /api/v1/regions/{id}/text-corrections/`, `/type-corrections/`, and `/suppression-corrections/` create auditable corrections. Each accepts an expected current value and returns `409 region_state_conflict` when stale.
- `POST /api/v1/corrections/{id}/revert/` reverts an active correction; repeating it is idempotent.
- OCR uses `/regions/{id}/ocr/`, `/pages/{id}/ocr/`, `/ocr/requests/{id}/`, and `/ocr/requests/{id}/accept/`. Candidates never overwrite authoritative text automatically.
- `GET /api/v1/search/?q=...` returns revision/page/region provenance and is project-scoped.
- Chat thread rename/archive use `/chat/threads/{id}/rename/` and `/archive/`; run diagnostics use `/chat/runs/{id}/diagnostics/` and exclude hidden reasoning.

Errors have the form `{"error": {"code", "message", "fields"}, "request_id": "..."}`. `401` is authentication failure, `403` authorization/scope failure, `404` concealed inaccessible resources, `409` stale state or invalid transition, and `429` is reserved for bounded-operation throttling.

## UI diagnostics

`GET /api/v1/ui-diagnostics/` requires `diagnostics:read` and returns bounded server-side state. It contains URL state, pending OCR identifiers, and region selection metadata, but no full document text, secrets, filesystem paths, or model reasoning. Browser sessions additionally expose `window.__DSW_UI_DIAGNOSTICS__` after loading the diagnostics module; `?debug_ui=1` adds non-interactive outlines and labels.
