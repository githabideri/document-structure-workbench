"""
Django settings for Document Structure Workbench.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Security
SECRET_KEY = os.environ.get("DSW_DJANGO_SECRET_KEY", "django-insecure-change-me")
DEBUG = os.environ.get("DSW_DEBUG", "false").lower() == "true"
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

LOCALE_PATHS = [
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

# Paths (overridable for deployment)
ARTIFACTS_BASE_DIR = Path(os.environ.get("DSW_ARTIFACTS_DIR", os.environ.get("DSW_ARTIFACTS_ROOT", "/var/lib/dsw/artifacts")))

# --- Docling Serve integration ---
DSW_DOCLING_API_URL = os.environ.get("DSW_DOCLING_API_URL", "")
DSW_DOCLING_API_KEY = os.environ.get("DSW_DOCLING_API_KEY", "")
DSW_DOCLING_REQUEST_TIMEOUT = int(os.environ.get("DSW_DOCLING_REQUEST_TIMEOUT", "60"))
DSW_DOCLING_JOB_TIMEOUT = int(os.environ.get("DSW_DOCLING_JOB_TIMEOUT", "3600"))

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
