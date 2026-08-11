"""
Django settings for Document Structure Workbench.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Security
SECRET_KEY = os.environ.get("DSW_DJANGO_SECRET_KEY", "django-insecure-change-me")
DEBUG = os.environ.get("DSW_DEBUG", "false").lower() == "true"
# Test-only browser hooks require an explicit deployment opt-in; DEBUG is not
# sufficient. Ordinary users cannot activate the tile-loaded draw fallback by
# adding a query parameter alone.
DSW_E2E_TEST_HOOKS_ENABLED = os.environ.get(
    "DSW_E2E_TEST_HOOKS_ENABLED", "false"
).lower() in {"true", "1", "yes"}
ALLOWED_HOSTS = os.environ.get(
    "DSW_ALLOWED_HOSTS", "localhost,127.0.0.1"
).split(",")

# API
API_KEY = os.environ.get("DSW_API_KEY", "")
RELEASE_FILE = os.environ.get("DSW_RELEASE_FILE", "")

# Applications
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "workbench",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "workbench.middleware.PasswordChangeRequiredMiddleware",
    "workbench.middleware.UserLanguageMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

# Templates
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "django.template.context_processors.i18n",
                "workbench.context_processors.release_info",
                "workbench.context_processors.user_roles",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# Database
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": Path(os.environ.get(
            "DSW_DATABASE_PATH", BASE_DIR / "db.sqlite3"
        )),
    }
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Internationalization
LANGUAGE_CODE = "en"
LANGUAGES = [
    ("en", "English"),
    ("de", "Deutsch"),
]

# Reviewed showcase translations take precedence over the legacy catalogue.
# Keeping the legacy catalogue second preserves existing coverage while allowing
# corrected wording and newly internationalized UI to ship independently.
LOCALE_PATHS = [
    BASE_DIR.parent / "locale_reviewed",
    BASE_DIR.parent / "locale",
]

TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# Static files
STATIC_URL = "static/"
STATIC_ROOT = Path(os.environ.get("DSW_STATIC_ROOT", BASE_DIR / "staticfiles"))
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# Default primary key field type
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Authentication
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

# Security (production defaults)
SECURE_BROWSER_XSS_FILTER = True
X_FRAME_OPTIONS = "DENY"
CSRF_COOKIE_SECURE = os.environ.get("DSW_SECURE_COOKIES", "false").lower() == "true"
SESSION_COOKIE_SECURE = os.environ.get("DSW_SECURE_COOKIES", "false").lower() == "true"

# --- Reverse-proxy / HTTPS hardening (public exposure via Caddy) ---
# The TLS-terminating Caddy proxy is the sole public ingress; trust its
# X-Forwarded-Proto so is_secure, URL generation and redirects are correct
# behind HTTPS. These are safe defaults and apply to every deployment.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_TRUSTED_ORIGINS = [
    "https://dsw.martinfellner.at",
    "https://dsw.tail51128.ts.net",
]

# Login brute-force throttle (cache-backed, no extra dependency/migration).
LOGIN_THROTTLE_FAILURE_LIMIT = int(os.environ.get("DSW_LOGIN_THROTTLE_FAILURE_LIMIT", "8"))
LOGIN_THROTTLE_COOLDOWN_SECONDS = int(os.environ.get("DSW_LOGIN_THROTTLE_COOLDOWN_SECONDS", "900"))

# Paths (overridable for deployment)
ARTIFACTS_BASE_DIR = Path(os.environ.get("DSW_ARTIFACTS_DIR", os.environ.get("DSW_ARTIFACTS_ROOT", "/var/lib/dsw/artifacts")))

# --- Upload limits ---
# Per-file size cap and per-request file-count cap for document uploads. Both
# the ingestion service and the upload UI read these so the limit lives in one
# place. When raising these in production, also bump the reverse-proxy
# (Caddy) request_body max_length and the Gunicorn --timeout in dsw-ops:
# a large multi-file POST is received and ingested synchronously by one worker.
DSW_UPLOAD_MAX_FILE_SIZE_BYTES = int(
    os.environ.get("DSW_UPLOAD_MAX_FILE_SIZE_BYTES", str(256 * 1024 * 1024))
)
DSW_UPLOAD_MAX_FILES_PER_REQUEST = int(
    os.environ.get("DSW_UPLOAD_MAX_FILES_PER_REQUEST", "200")
)
# Raise Django's per-request form-field cap so large multi-file posts are not
# rejected before the view runs (Django default is 1000 parts total).
DATA_UPLOAD_MAX_NUMBER_FIELDS = int(os.environ.get("DSW_DATA_UPLOAD_MAX_NUMBER_FIELDS", "10000"))

# Project visibility model.
#   "membership" (default) restricts each project to its members.
#   "all_users" makes every non-archived project visible and editable by every
#   signed-in user — a single shared workspace for trusted small teams.
# Review and curate remain membership-gated and API tokens keep their own
# scoping in both modes. Enable per deployment via DSW_PROJECT_VISIBILITY.
DSW_PROJECT_VISIBILITY = os.environ.get("DSW_PROJECT_VISIBILITY", "membership")

# --- Docling Serve integration ---
DSW_DOCLING_API_URL = os.environ.get("DSW_DOCLING_API_URL", "")
DSW_DOCLING_API_KEY = os.environ.get("DSW_DOCLING_API_KEY", "")
DSW_DOCLING_HEALTH_PATH = os.environ.get("DSW_DOCLING_HEALTH_PATH", "/health")
DSW_DOCLING_REQUEST_TIMEOUT = int(os.environ.get("DSW_DOCLING_REQUEST_TIMEOUT", "60"))
DSW_DOCLING_JOB_TIMEOUT = int(os.environ.get("DSW_DOCLING_JOB_TIMEOUT", "3600"))
DSW_DOCLING_OCR_ENGINE = os.environ.get("DSW_DOCLING_OCR_ENGINE", "rapidocr")
DSW_DOCLING_OCR_BACKEND = os.environ.get("DSW_DOCLING_OCR_BACKEND", "torch")
DSW_DOCLING_OCR_LANG = os.environ.get("DSW_DOCLING_OCR_LANG", "de,en")
DSW_DOCLING_OCR_ENABLED = os.environ.get("DSW_DOCLING_OCR_ENABLED", "false").lower() == "true"
DSW_DOCLING_IMAGES_SCALE = float(os.environ.get("DSW_DOCLING_IMAGES_SCALE", "2.0"))
DSW_PROCESSING_STALE_AFTER_SECONDS = int(
    os.environ.get("DSW_PROCESSING_STALE_AFTER_SECONDS", "90")
)
DSW_PROCESSING_LEASE_SECONDS = int(
    os.environ.get("DSW_PROCESSING_LEASE_SECONDS", "90")
)
DSW_PROCESSING_SUBMIT_RETRY_MAX = int(
    os.environ.get("DSW_PROCESSING_SUBMIT_RETRY_MAX", "3")
)
DSW_PROCESSING_SUBMIT_RETRY_BACKOFF = int(
    os.environ.get("DSW_PROCESSING_SUBMIT_RETRY_BACKOFF", "60")
)
DSW_PROCESSING_MAX_STATUS_ERRORS = int(os.environ.get("DSW_PROCESSING_MAX_STATUS_ERRORS", "5"))

# Optional read-only research assistant (OpenAI-compatible llama.cpp server).
DSW_CHAT_BASE_URL = os.environ.get("DSW_CHAT_BASE_URL", "")
DSW_CHAT_API_KEY = os.environ.get("DSW_CHAT_API_KEY", "")
DSW_CHAT_MODEL = os.environ.get("DSW_CHAT_MODEL", "")
DSW_CHAT_TIMEOUT = int(os.environ.get("DSW_CHAT_TIMEOUT", "600"))
DSW_CHAT_TOOL_REQUEST_TIMEOUT = int(os.environ.get("DSW_CHAT_TOOL_REQUEST_TIMEOUT", "300"))
DSW_CHAT_FINAL_REQUEST_TIMEOUT = int(os.environ.get("DSW_CHAT_FINAL_REQUEST_TIMEOUT", "600"))
DSW_CHAT_WALL_CLOCK_TIMEOUT = int(os.environ.get("DSW_CHAT_WALL_CLOCK_TIMEOUT", "900"))
DSW_CHAT_MAX_TOKENS = int(os.environ.get("DSW_CHAT_MAX_TOKENS", "16384"))
DSW_CHAT_TOOL_MODE = os.environ.get("DSW_CHAT_TOOL_MODE", "fallback")
DSW_CHAT_RETRIEVER = os.environ.get("DSW_CHAT_RETRIEVER", "deterministic_lexical")
DSW_CHAT_MAX_TOOL_CALLS = min(20, max(1, int(os.environ.get("DSW_CHAT_MAX_TOOL_CALLS", "10"))))
DSW_CHAT_MAX_RESULTS_PER_CALL = int(os.environ.get("DSW_CHAT_MAX_RESULTS_PER_CALL", "8"))
DSW_CHAT_MAX_EVIDENCE = int(os.environ.get("DSW_CHAT_MAX_EVIDENCE", "24"))
DSW_CHAT_CONTEXT_TOKEN_BUDGET = int(os.environ.get("DSW_CHAT_CONTEXT_TOKEN_BUDGET", "12000"))

# Optional visual OCR correction provider. This is deliberately separate from
# chat: OCR requests are persisted as candidates and never replace text in
# place. The URL is an OpenAI-compatible /v1 endpoint.
DSW_OCR_BASE_URL = os.environ.get("DSW_OCR_BASE_URL", "")
DSW_OCR_API_KEY = os.environ.get("DSW_OCR_API_KEY", "")
DSW_OCR_MODEL = os.environ.get("DSW_OCR_MODEL", "PaddleOCR-VL-0.9B")
DSW_OCR_TIMEOUT = int(os.environ.get("DSW_OCR_TIMEOUT", "300"))
DSW_OCR_MAX_TOKENS = int(os.environ.get("DSW_OCR_MAX_TOKENS", "4096"))
DSW_OCR_PROVIDER = os.environ.get("DSW_OCR_PROVIDER", "qwen")
DSW_INGESTION_OCR_ENABLED = os.environ.get("DSW_INGESTION_OCR_ENABLED", "true").lower() == "true"
DSW_INGESTION_OCR_REQUIRED = os.environ.get("DSW_INGESTION_OCR_REQUIRED", "true").lower() == "true"
DSW_INGESTION_OCR_PROVIDER = os.environ.get("DSW_INGESTION_OCR_PROVIDER", "paddleocr-vl")
DSW_INGESTION_OCR_MODEL = os.environ.get("DSW_INGESTION_OCR_MODEL", DSW_OCR_MODEL)

# Optional handwritten-text recognition (HTR) rerun. This is a distinct,
# region-scoped action separate from the full-page visual OCR rerun above: the
# selected region crop is submitted to the HTR inference service and the
# line-level transcription is stored as a *candidate* (OcrRequest provider="htr")
# that never overwrites region text until a curator accepts it as a correction.
DSW_HTR_ENABLED = os.environ.get("DSW_HTR_ENABLED", "false").lower() == "true"
DSW_HTR_BASE_URL = os.environ.get("DSW_HTR_BASE_URL", "").rstrip("/")
DSW_HTR_API_TOKEN = os.environ.get("DSW_HTR_API_TOKEN", "")
DSW_HTR_DEFAULT_PIPELINE = os.environ.get("DSW_HTR_DEFAULT_PIPELINE", "htrflow-trocr-kurrent")
DSW_HTR_POLL_INTERVAL_SECONDS = float(os.environ.get("DSW_HTR_POLL_INTERVAL_SECONDS", "2"))
DSW_HTR_TIMEOUT_SECONDS = int(os.environ.get("DSW_HTR_TIMEOUT_SECONDS", "600"))
DSW_HTR_CONNECT_TIMEOUT = int(os.environ.get("DSW_HTR_CONNECT_TIMEOUT", "10"))
DSW_HTR_FIXTURE_DELAY_SECONDS = float(os.environ.get("DSW_HTR_FIXTURE_DELAY_SECONDS", "3"))
# Local development / UI testing without a deployed HTR service: the worker
# returns a recorded fixture result (app/workbench/tests/fixtures/htr/) after a
# short delay so the queued/processing/completed flow is observable. Never set
# this in a real deployment.
DSW_HTR_FIXTURE_MODE = os.environ.get("DSW_HTR_FIXTURE_MODE", "false").lower() == "true"
DSW_HTR_FIXTURE_PATH = os.environ.get(
    "DSW_HTR_FIXTURE_PATH",
    str(Path(__file__).resolve().parent.parent / "workbench" / "tests" / "fixtures" / "htr" / "transcription-succeeded.json"),
)

# Fail clearly when API URL is absent in production
if not DEBUG and not DSW_DOCLING_API_URL:
    import warnings
    warnings.warn(
        "DSW_DOCLING_API_URL is not set. Document processing will fail. "
        "Set DSW_DOCLING_API_URL to your Docling Serve instance.",
        RuntimeWarning,
    )
IMPORTS_BASE_DIR = Path(os.environ.get("DSW_IMPORTS_DIR", "/var/lib/dsw/imports"))

# Logging
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{asctime} {levelname} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": os.environ.get("DSW_LOG_LEVEL", "INFO"),
    },
}
