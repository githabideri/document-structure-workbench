# API-parity and browser-verification baseline

Baseline recorded before implementation work on `main` at
`f5198facd69222c32554eb5b95773502c91f6849`.

## Repository state

- Working tree: clean at baseline capture.
- Application architecture: Django-rendered HTML, HTMX polling, token-authenticated JSON API, database-backed workers.
- Authoritative domain objects: `Collection`, `SourceDocument`, immutable `Document` revisions, `Page`, `PageRegion`, `RegionCorrection`, `OcrRequest`, `ChatThread`, `ChatRun`, `EvidenceItem`, and `ProcessingJob`.
- Existing OCR paths: PaddleOCR-VL ingestion and selectable Qwen/PaddleOCR-VL candidate reruns. Candidates are accepted through corrections.

## Existing endpoint inventory

| Method | Path | Scope / auth | Request | Response / HTML parity | Coverage at baseline |
|---|---|---|---|---|---|
| GET | `/api/v1/health/` | anonymous | none | health JSON | health tests |
| GET | `/api/v1/me/` | bearer token | none | identity/scopes JSON | API tests |
| GET, POST | `/api/v1/projects/` | `projects:read`; POST also `projects:write`, administrator | project JSON | list/create; project-create HTML exists | partial |
| GET, PATCH | `/api/v1/projects/{id}/` | `projects:read`; PATCH admin/write | archive flag | detail/archive; HTML archive exists | partial |
| GET | `/api/v1/projects/{id}/documents/` | `documents:read` | none | active source/revision summary | API tests |
| POST | `/api/v1/projects/{id}/upload/` | `documents:upload`, `jobs:submit` | multipart file/preset | queued upload | API + HTML service parity |
| GET | `/api/v1/jobs/{id}/` | `jobs:read` | none | processing status | API tests |
| POST | `/api/v1/jobs/{id}/recovery/` | `documents:manage` | action | retry/close/archive | partial |
| POST | `/api/v1/regions/{id}/ocr/` | `documents:manage` | provider/model/prompt | queued OCR request | API + HTML duplicate construction |
| POST | `/api/v1/pages/{id}/ocr/` | `documents:manage` | provider/model/prompt | queued OCR request | API + HTML duplicate construction |
| GET | `/api/v1/ocr/requests/{id}/` | `documents:read` | none | OCR request | API tests |
| POST | `/api/v1/ocr/requests/{id}/accept/` | `documents:manage` | none | accepted candidate | API + HTML duplicate construction |
| GET | `/api/v1/presets/` | `jobs:submit` | none | presets | API tests |
| GET | `/api/v1/tasks/`, `/api/v1/tasks/{id}/` | `tasks:read` | filters | blinded review data | legacy benchmark |
| POST | `/api/v1/tasks/{id}/submit/` | `tasks:write` | review JSON | review result | legacy benchmark |
| GET | `/api/v1/statistics/` | `statistics:read` | none | aggregates | legacy benchmark |
| GET, POST | `/api/v1/chat/threads/` | chat scopes | thread/run JSON | chat create/list; HTML chat create | partial |
| GET | `/api/v1/chat/threads/{id}/` | `chat:read` | none | thread/messages/runs | API tests |
| GET | `/api/v1/chat/threads/{id}/runs/` | `chat:read` | none | runs | API tests |
| GET | `/api/v1/chat/runs/{id}/` | `chat:read` | none | run/evidence summary | API tests |
| GET | `/api/v1/chat/runs/{id}/evidence/` | `chat:read` | none | persisted evidence | API tests |
| POST | `/api/v1/chat/runs/{id}/retry/` | `chat:retry` | none | queued retry | API tests |
| POST, GET | support bundle routes | support scopes | none | audited diagnostics export | API tests |

## Domain operations available only through HTML at baseline

- Source archive is HTML-only (`POST /uploads/{id}/archive/`).
- Region text/type/suppression corrections and correction revert are HTML-only.
- Chat rename and archive are HTML-only despite corresponding views.
- Document/revision/page/region inspection is rendered HTML only; the API has only project document listing.
- Search is HTML-only.
- Chat run diagnostics is HTML-only even though persisted diagnostic data exists.
- Page image and artifact delivery are HTML routes, intentionally not direct filesystem APIs.

## Presentation and JavaScript baseline

- Chat uses server-rendered forms/HTMX status partials and substantial inline behavior in the template.
- Document inspection contains inline zoom/pan, overlay selection, region filtering, and OCR polling behavior.
- Project processing and upload pages contain inline scripts for polling/drop-zone behavior.
- There is no shared API client, request cancellation layer, UI state manifest, or diagnostics endpoint.
- Region overlays are semantic links, but several controls and state transitions rely on template-local behavior.

## Browser-visible state baseline

Chat: thread, selected run, selected citation, selected source/revision/page/region, evidence visibility, composer draft, submission/run state, and return-to-chat context.

Document: source document, immutable revision, page, region, zoom/pan, region filters, suppressed visibility, OCR request/candidate state, correction draft/editor, and workspace display concern.

## Initial gaps and implementation constraints

1. Add read serializers and permission-scoped inspection routes before mutation parity.
2. Route HTML and API correction/OCR mutations through service boundaries.
3. Normalize structured API errors while preserving existing clients where practical.
4. Add URL/state contracts and diagnostics without exposing document text or model chain-of-thought.
5. Extract only substantial inline JavaScript; keep progressive-enhancement fallbacks.
6. Browser smoke currently verifies login/chat/citation screenshots but not keyboard, history, OCR, race, diagnostics, or responsive assertions.
