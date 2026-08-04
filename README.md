# Document Structure Workbench

A transparent workspace for turning archival and museum scans into inspectable, searchable, and correctable structured documents. Users can upload a source, follow honest processing status, inspect the original scan with detected regions, search immutable processing revisions, and ask a citation-grounded research assistant about selected documents.

The original blinded extraction benchmark remains available as an evaluation capability, but it is no longer the primary product journey.

## Features

- **Document ingestion** — Upload PDFs, queue Docling analysis, and recover safely from worker restarts
- **Immutable revisions** — Each processing attempt owns a separate revision; the active revision is explicit
- **Document workspace** — Full-page scan viewer, zoom/pan, normalized region overlays, extracted text, tables, and deep links
- **Human corrections** — Correct text, region types, and suppression state without overwriting raw machine output; corrections are reversible
- **Revision-aware search** — Lexical passage indexing with page/region provenance and stable evidence links
- **Research chat** — Persistent, read-only conversations with selected-document evidence, Qwen/llama.cpp support, validated citations, and inspectable evidence runs
- **Blinded evaluation** — Compare two extraction results (labeled X/Y) without knowing which model produced which
- **Quality scoring** — Rate each extraction on a 0-3 scale with error classification
- **Post-reveal analysis** — After submission, see model identities, ground truth, and automated metrics
- **REST API** — Full programmatic access for automated review workflows
- **Multilingual** — English and German interface support
- **Document workspace** — Full-page viewer with region overlays and progressive disclosure

## Technology

- **Django 5.2** — Web framework
- **Python 3.11+** — Runtime
- **HTMX 2.x** — Dynamic UI without JavaScript framework
- **SQLite/PostgreSQL** — Database (SQLite for development, PostgreSQL for production)
- **Gunicorn** — WSGI server
- **Bleach** — HTML sanitization
- **llama.cpp-compatible API** — Optional local Qwen research assistant

## Quick Start

```bash
# Clone
git clone git@github.com:owner/document-structure-workbench.git
cd document-structure-workbench

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -e ".[dev]"

# Initialize database
cd app
python manage.py migrate
python manage.py createsuperuser

# Run development server
python manage.py runserver

# Run the document/ chat worker in another terminal
python manage.py run_processing_worker
```

### Optional chat configuration

The chat integration uses an OpenAI-compatible llama.cpp endpoint. Keep these values in the deployment environment, never in source control:

```text
DSW_CHAT_BASE_URL=http://127.0.0.1:8081/v1
DSW_CHAT_API_KEY=...
DSW_CHAT_MODEL=Qwen3.6-35B-A3B-UD-IQ4_XS.gguf
DSW_CHAT_TOOL_MODE=automatic  # automatic, native, or provider-without-tools
DSW_CHAT_WALL_CLOCK_TIMEOUT=300  # total budget for model/tool orchestration
DSW_CHAT_MAX_TOOL_CALLS=10  # bounded model-directed searches; hard cap 20
DSW_CHAT_MAX_RESULTS_PER_CALL=8
DSW_CHAT_MAX_EVIDENCE=24
DSW_CHAT_CONTEXT_TOKEN_BUDGET=12000
```

The chat workflow is model-directed. A conversation freezes its document/revision scope; the model may call the bounded search tool, whose initial implementation uses the lexical index. No server-side retrieval occurs before or instead of a model tool call, and the final answer can cite only persisted evidence markers.

## Project Structure

```
document-structure-workbench/
├── app/                    # Django project
│   ├── config/             # Settings, URLs, WSGI/ASGI
│   ├── workbench/          # Main application
│   │   ├── models.py       # Projects, revisions, regions, search, chat runs
│   │   ├── views.py        # Web views
│   │   ├── api.py          # REST API endpoints
│   │   ├── admin.py        # Django admin configuration
│   │   ├── templates/      # HTML templates
│   │   └── management/     # Management commands
│   ├── static/             # Static files (CSS, JS)
│   └── templates/          # Global templates
├── content/                # Localized help content
│   └── help/
│       ├── en/
│       └── de/
├── locale/                 # Translation files
├── tests/                  # Test suite
├── docs/                   # Documentation
├── deployment/             # Deployment examples
│   └── examples/
├── scripts/                # Utility scripts
├── pyproject.toml          # Project configuration
├── README.md
├── LICENSE
└── .gitignore
```

## Current product state

Completed foundations:

- reliable Docling processing with restart recovery and truthful status states;
- immutable processing revisions and revision-safe imports;
- read-only scan workspace with overlays and stable page/region URLs;
- reversible correction layer preserving raw machine output;
- revision-aware lexical search;
- persistent evidence-grounded chat runs using the configured Qwen endpoint.

The next work is to harden the evidence planner and asynchronous chat UX: full-document coverage, explicit retrieval/context diagnostics, streaming or polling status, conversation continuity when opening citations, and browser-level regression scenarios. Agent-assisted edits, Git-backed change sets, embeddings, and GraphRAG remain intentionally deferred until the read-only evidence workflow is dependable.

## API

The application provides a REST API for programmatic access:

- `GET /api/v1/health/` — Health check and release information
- `GET /api/v1/projects/` — List accessible projects
- `GET /api/v1/projects/<id>/documents/` — List source and processed documents
- `POST /api/v1/projects/<id>/upload/` — Upload a PDF and queue processing
- `GET /api/v1/jobs/<id>/` — Processing status and result metadata
- `GET /api/v1/tasks/` — List review tasks
- `GET /api/v1/tasks/<id>/` — Task detail with blinded extractions
- `POST /api/v1/tasks/<id>/submit/` — Submit a review
- `GET /api/v1/statistics/` — Review statistics
- `GET /api/v1/health/` — Safe release, database, worker, queue, and chat configuration status
- `GET|POST /api/v1/chat/threads/` — List or create evidence-grounded conversations
- `GET /api/v1/chat/threads/<id>/` — Conversation messages and runs
- `GET /api/v1/chat/runs/<id>/` — Run status, answer, and evidence
- `GET /api/v1/chat/runs/<id>/evidence/` — Persisted evidence references
- `POST /api/v1/chat/runs/<id>/retry/` — Queue a retry
- `POST /api/v1/chat/runs/<id>/support-bundle/` and `GET /api/v1/support-bundles/<id>/` — Privileged, audited support exports

All endpoints except health use `Authorization: Bearer <token>`. Tokens are
created from Settings and are project-scoped through membership and scopes.
- `POST /api/tasks/<id>/submit/` — Submit review (requires API key)
- `GET /api/statistics/` — Aggregate statistics

See [API Documentation](docs/api.md) for complete reference.

Agent and operator workflows use the thin HTTP client in `scripts/dsw`. Set
`DSW_API_BASE_URL` and `DSW_API_TOKEN`, then use `scripts/dsw doctor`,
`scripts/dsw chat ...`, or the deterministic `scripts/dsw smoke --fake` check.
Live smoke is explicit and data-independent: `scripts/dsw smoke --live
--project PROJECT_ID --source SOURCE_ID --question "..."`.
For a deployed acceptance run, `scripts/dsw verify-deployed --project
PROJECT_ID --source SOURCE_ID --question "..."` combines health, live smoke,
evidence, and a locally saved support bundle; add `--browser` when the browser
fixture environment variables are configured. `scripts/dsw support-bundle`
saves the audited export instead of only printing it.
The browser acceptance scenario is also data-independent; provide
`DSW_BROWSER_BASE_URL`, `DSW_BROWSER_USERNAME`, and `DSW_BROWSER_PASSWORD`,
then run `scripts/dsw browser-smoke`. It writes `result.json`, step logs, and
conversation/document screenshots to `DSW_BROWSER_OUTPUT_DIR` or `/tmp`.
See [Browser smoke and debugging](docs/browser-smoke.md) for local fixture
seeding, safe staging/deployed runs, and the failure-debugging workflow.

## Deployment

See [Deployment Guide](docs/deployment.md) for deployment, target discovery,
configuration restart behavior, and post-deployment verification.

Examples are provided in `deployment/examples/`.

## License

See [LICENSE](LICENSE) for details.

## Disclaimer

This is an independent project and is not affiliated with, endorsed by, or connected to IBM or the Docling project. It uses Docling as a document processing backend but is a separate application.
