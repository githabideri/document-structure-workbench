"""Region-scoped handwritten-text recognition (HTR) client.

Talks to the DSW HTR inference service (POST/GET ``/v1/transcriptions``) using
the frozen ``htr-transcription-result-v1`` contract (repo-root
``schemas/htr-transcription-result-v1.json``).

HTR output is ALWAYS a *candidate*. The worker stores it on an
``OcrRequest(provider="htr")``; acceptance is a normal ``RegionCorrection``.
No region text is mutated in place and there is never a fallback to another OCR
provider.
"""
import json
import logging
import time
import uuid
from pathlib import Path

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

HTR_PROVIDER = "htr"
HTR_SCHEMA_VERSION = "htr-transcription-v1"

# Path to the frozen result schema (shared with the HTR service and frontend).
_SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schemas" / "htr-transcription-result-v1.json"


class HtrError(RuntimeError):
    """Raised when the HTR service is unavailable or returns an invalid result."""


def _load_schema():
    """Load and cache the JSON schema, or None if it cannot be read."""
    try:
        return json.loads(_SCHEMA_PATH.read_text())
    except (OSError, ValueError):
        logger.debug("HTR result schema unavailable at %s", _SCHEMA_PATH)
        return None


def validate_result(result):
    """Validate an HTR service result against the frozen v1 contract.

    Validation is best-effort: if ``jsonschema`` or the schema file is not
    available the result is returned unchanged (with a debug log). In a healthy
    deployment both are present and an out-of-contract result raises HtrError.
    """
    schema = _load_schema()
    if schema is None:
        return result
    try:
        import jsonschema  # type: ignore
    except ImportError:
        logger.debug("jsonschema not installed; skipping HTR result validation.")
        return result
    try:
        jsonschema.validate(instance=result, schema=schema)
    except jsonschema.ValidationError as exc:
        raise HtrError(
            f"HTR service result violates the v1 contract: {exc.message}"
        ) from exc
    return result


class HtrClient:
    """Client for the DSW HTR inference service.

    submit_transcription(image_bytes, pipeline_id, mode, label) -> remote_id
    get_transcription(remote_id) -> dict (status: pending|succeeded|failed)

    In fixture mode (``DSW_HTR_FIXTURE_MODE``) no network call is made: a
    recorded result is served after a short delay so the UI can exercise the full
    queued/processing/completed flow without a deployed HTR service.
    """

    # Process-local fixture store (the OCR worker is serialized, one job at a
    # time). remote_id -> {"submitted_at": monotonic, "result": dict}
    _fixture_store = {}

    def __init__(self, base_url=None, api_token=None, default_pipeline=None,
                 fixture_mode=None, fixture_path=None, poll_interval=None,
                 timeout=None, connect_timeout=None, fixture_delay=None):
        self.fixture_mode = fixture_mode if fixture_mode is not None else getattr(
            settings, "DSW_HTR_FIXTURE_MODE", False
        )
        self.base_url = (base_url or getattr(settings, "DSW_HTR_BASE_URL", "")).rstrip("/")
        self.api_token = api_token if api_token is not None else getattr(settings, "DSW_HTR_API_TOKEN", "")
        self.default_pipeline = default_pipeline or getattr(
            settings, "DSW_HTR_DEFAULT_PIPELINE", "htrflow-trocr-prototype"
        )
        self.fixture_path = Path(fixture_path or getattr(
            settings, "DSW_HTR_FIXTURE_PATH",
            _SCHEMA_PATH.parent.parent / "app" / "workbench" / "tests" / "fixtures" / "htr" / "transcription-succeeded.json",
        ))
        self.poll_interval = poll_interval if poll_interval is not None else float(
            getattr(settings, "DSW_HTR_POLL_INTERVAL_SECONDS", 2.0)
        )
        self.timeout = timeout if timeout is not None else int(
            getattr(settings, "DSW_HTR_TIMEOUT_SECONDS", 600)
        )
        self.connect_timeout = connect_timeout if connect_timeout is not None else int(
            getattr(settings, "DSW_HTR_CONNECT_TIMEOUT", 10)
        )
        self.fixture_delay = fixture_delay if fixture_delay is not None else float(
            getattr(settings, "DSW_HTR_FIXTURE_DELAY_SECONDS", 3.0)
        )
        if not self.fixture_mode and not self.base_url:
            raise HtrError(
                "HTR is not configured: set DSW_HTR_BASE_URL or enable DSW_HTR_FIXTURE_MODE."
            )

    # -- fixture mode -------------------------------------------------------

    def _fixture_result(self):
        try:
            return json.loads(self.fixture_path.read_text())
        except (OSError, ValueError) as exc:
            raise HtrError(f"Could not load HTR fixture at {self.fixture_path}: {exc}") from exc

    # -- public API ---------------------------------------------------------

    def submit_transcription(self, image_bytes, pipeline_id=None, mode="region", label=None):
        """Submit a region crop. Returns the remote transcription id."""
        pipeline_id = pipeline_id or self.default_pipeline
        if self.fixture_mode:
            remote_id = f"fixture-{uuid.uuid4().hex[:12]}"
            result = dict(self._fixture_result())
            result["id"] = remote_id
            result["self"] = f"/v1/transcriptions/{remote_id}"
            result["status"] = "pending"
            if label:
                result["label"] = label
            self._fixture_store[remote_id] = {
                "submitted_at": time.monotonic(), "result": result,
            }
            return remote_id

        if not self.api_token:
            raise HtrError("HTR requires an API token (DSW_HTR_API_TOKEN).")
        headers = {"Authorization": f"Bearer {self.api_token}"}
        files = {"file": ("region.png", image_bytes, "image/png")}
        data = {"pipeline_id": pipeline_id, "mode": mode}
        if label:
            data["label"] = label
        try:
            response = requests.post(
                f"{self.base_url}/v1/transcriptions",
                headers=headers, files=files, data=data,
                timeout=(self.connect_timeout, self.timeout),
            )
        except requests.RequestException as exc:
            raise HtrError("Could not reach the HTR service.") from exc
        if response.status_code == 503:
            raise HtrError(
                "The HTR service is busy (another run is in progress). Retry shortly."
            )
        if response.status_code not in (200, 201, 202):
            raise HtrError(
                f"HTR service rejected the submission (HTTP {response.status_code})."
            )
        try:
            return response.json()["id"]
        except (ValueError, KeyError) as exc:
            raise HtrError(
                "HTR service returned an invalid submission response."
            ) from exc

    def get_transcription(self, remote_id):
        """Poll a transcription. Returns the raw service result dict."""
        if self.fixture_mode:
            entry = self._fixture_store.get(remote_id)
            if entry is None:
                raise HtrError(f"Unknown HTR fixture id: {remote_id}")
            elapsed = time.monotonic() - entry["submitted_at"]
            if elapsed < self.fixture_delay:
                return {
                    "id": remote_id, "status": "pending",
                    "self": f"/v1/transcriptions/{remote_id}",
                }
            result = dict(entry["result"])
            result["status"] = "succeeded"
            return validate_result(result)

        try:
            response = requests.get(
                f"{self.base_url}/v1/transcriptions/{remote_id}",
                headers={"Authorization": f"Bearer {self.api_token}"},
                timeout=(self.connect_timeout, self.timeout),
            )
        except requests.RequestException as exc:
            raise HtrError("Could not reach the HTR service while polling.") from exc
        if response.status_code == 404:
            raise HtrError(f"HTR service has no record of run {remote_id}.")
        if response.status_code != 200:
            raise HtrError(f"HTR service poll failed (HTTP {response.status_code}).")
        try:
            result = response.json()
        except ValueError as exc:
            raise HtrError("HTR service returned an invalid poll response.") from exc
        if result.get("status") == "succeeded":
            validate_result(result)
        return result
