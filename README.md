# Document Structure Workbench

A web application for reviewing, comparing, and validating document structure extraction results. Supports blinded side-by-side comparison of table extractions from different processing pipelines.

## Features

- **Blinded review** — Compare two extraction results (labeled X/Y) without knowing which model produced which
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
```

## Project Structure

```
document-structure-workbench/
├── app/                    # Django project
│   ├── config/             # Settings, URLs, WSGI/ASGI
│   ├── workbench/          # Main application
│   │   ├── models.py       # Data models
│   │   ├── views.py        # Web views
│   │   ├── api.py          # REST API endpoints
│   │   ├── admin.py        # Django admin configuration
│   │   ├── templates/      # HTML templates
│   │   └── management/     # Management commands
│   ├── static/             # Static files (CSS, JS)
│   └── templates/          # Global templates
├── processors/             # Document processing pipelines
├── schemas/                # Data schemas and validation
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

## API

The application provides a REST API for programmatic access:

- `GET /api/status/` — Health check and summary
- `GET /api/tasks/` — List review tasks
- `GET /api/tasks/<id>/` — Task detail with extractions
- `POST /api/tasks/<id>/submit/` — Submit review (requires API key)
- `GET /api/statistics/` — Aggregate statistics

See [API Documentation](docs/api.md) for complete reference.

## Deployment

See [Deployment Guide](docs/deployment.md) for production setup.

Examples are provided in `deployment/examples/`.

## License

See [LICENSE](LICENSE) for details.

## Disclaimer

This is an independent project and is not affiliated with, endorsed by, or connected to IBM or the Docling project. It uses Docling as a document processing backend but is a separate application.
