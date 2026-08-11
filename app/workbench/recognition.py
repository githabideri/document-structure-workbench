"""Recognition model/pipeline metadata and per-user preferences.

The document workspace exposes two recognition categories — HTR and visual OCR
— as model-aware controls. This module is the single source of truth for which
pipelines/providers exist, what their default model is, and how the last-used
choice is remembered per user (so it follows the user across browsers).

Screen-specific layout choices (page-rail collapse, inspector width) stay in the
browser via ``localStorage``; only the model choice is server-side because it is
conceptually a user preference rather than a device preference.
"""
from django.conf import settings

from .models import UserPreferences


# Ordered HTR pipeline catalogue: (id, label). The default is chosen from
# ``DSW_HTR_DEFAULT_PIPELINE`` if present, otherwise the first entry.
HTR_PIPELINES = [
    ("htrflow-trocr-kurrent", "TrOCR · Kurrent (19th-c. German)"),
    ("htrflow-trocr-prototype", "TrOCR · prototype (generic)"),
]


def available_htr_pipelines():
    """Return ``[(id, label), ...]`` for the HTR split-button dropdown."""
    return list(HTR_PIPELINES)


def default_htr_pipeline():
    configured = getattr(settings, "DSW_HTR_DEFAULT_PIPELINE", "")
    if configured and any(pid == configured for pid, _ in HTR_PIPELINES):
        return configured
    return HTR_PIPELINES[0][0] if HTR_PIPELINES else ""


# Visual OCR provider -> (label, default model setting). The model string is the
# "identifier" the workspace remembers; for vision it is derived from the chosen
# provider's configured model rather than selected directly.
def _vision_model_for(provider):
    if provider == "qwen":
        return getattr(settings, "DSW_CHAT_MODEL", "") or "qwen"
    if provider == "paddleocr-vl":
        return getattr(settings, "DSW_OCR_MODEL", "") or "paddleocr-vl"
    return getattr(settings, "DSW_OCR_MODEL", "") or provider


def _vision_base_url(provider):
    """The endpoint a provider would call; empty means it is not configured.

    Qwen reuses the chat provider endpoint; the others use the dedicated OCR
    endpoint."""
    if provider == "qwen":
        return getattr(settings, "DSW_CHAT_BASE_URL", "")
    return getattr(settings, "DSW_OCR_BASE_URL", "")


def available_vision_models():
    """Return ``[(provider, label, default_model), ...]`` for the Vision dropdown.

    The full catalogue is always returned so provider selection and preference
    memory work regardless of endpoint configuration; the UI only *shows* the
    Vision control when ``vision_enabled()`` is true."""
    from .processors.vision_ocr import OCR_PROVIDERS
    # Keep a stable, user-facing order: qwen first (the default), then others.
    order = ["qwen", "paddleocr-vl", "openai-compatible"]
    seen = set()
    rows = []
    for provider in order:
        if provider in OCR_PROVIDERS and provider not in seen:
            rows.append((provider, OCR_PROVIDERS[provider], _vision_model_for(provider)))
            seen.add(provider)
    for provider, label in OCR_PROVIDERS.items():
        if provider not in seen:
            rows.append((provider, label, _vision_model_for(provider)))
    return rows


def runnable_vision_models():
    """Return ``[(provider, label, default_model), ...]`` for the Vision dropdown,
    restricted to providers whose endpoint is actually configured.

    Presenting a provider that is certain to fail (its endpoint is missing) is
    a poor UX; the UI only offers runnable providers. The full catalogue remains
    available via ``available_vision_models()`` for internal knowledge and the
    server-side provider guard."""
    return [(p, l, m) for p, l, m in available_vision_models() if _vision_base_url(p)]


def vision_enabled():
    """Whether any visual-OCR provider has a configured endpoint.

    Independent of HTR: disabling HTR must never hide Vision (and vice versa)."""
    return bool(runnable_vision_models())


def default_vision_provider():
    return getattr(settings, "DSW_OCR_PROVIDER", "qwen") or "qwen"


def available_vision_models_dict():
    """Return ``{provider: default_model}`` for quick lookups."""
    return {provider: model for provider, _, model in available_vision_models()}


def _prefs(user):
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    return UserPreferences.get_or_create_for_user(user)


def effective_htr_pipeline(user, explicit=None):
    """Resolve the HTR pipeline to use for a new run.

    Precedence: an explicit value, then the user's last-used pipeline (only if it
    still exists), then the server default. Stale stored values never raise.
    """
    if explicit:
        return explicit
    prefs = _prefs(user)
    stored = prefs.last_htr_pipeline if prefs else ""
    if stored and any(pid == stored for pid, _ in HTR_PIPELINES):
        return stored
    return default_htr_pipeline()


def effective_vision_provider(user, explicit=None):
    """Resolve the visual OCR provider for a new run (see ``effective_htr_pipeline``).

    The user's stored provider is used only if it is still *runnable* with the
    current server configuration; otherwise we silently fall back to the
    configured default (if runnable) or the first runnable provider. A provider
    whose endpoint is missing is never returned, so the UI never offers a
    dropdown option that is guaranteed to fail."""
    if explicit:
        return explicit
    prefs = _prefs(user)
    stored = prefs.last_vision_provider if prefs else ""
    runnable = {p for p, _, _ in runnable_vision_models()}
    if stored and stored in runnable:
        return stored
    default = default_vision_provider()
    if default in runnable:
        return default
    rows = runnable_vision_models()
    return rows[0][0] if rows else ""


def remember_htr_pipeline(user, pipeline_id):
    prefs = _prefs(user)
    if prefs and pipeline_id and prefs.last_htr_pipeline != pipeline_id:
        prefs.last_htr_pipeline = pipeline_id
        prefs.save(update_fields=["last_htr_pipeline"])


def remember_vision_provider(user, provider):
    prefs = _prefs(user)
    if prefs and provider and prefs.last_vision_provider != provider:
        prefs.last_vision_provider = provider
        prefs.save(update_fields=["last_vision_provider"])
