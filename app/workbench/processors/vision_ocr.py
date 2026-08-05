"""OpenAI-compatible visual OCR client and safe page/region image crops."""
import base64
import hashlib
import json
import logging
from io import BytesIO
from pathlib import Path

import requests
from django.conf import settings
from PIL import Image

logger = logging.getLogger(__name__)


class VisionOcrError(RuntimeError):
    pass


def page_image_path(page):
    base = Path(getattr(settings, "ARTIFACTS_BASE_DIR", "/var/lib/dsw/artifacts")).resolve()
    path = (base / page.image_path).resolve()
    if not path.is_relative_to(base) or not path.exists():
        raise VisionOcrError("The immutable page image is not available.")
    return path


def make_crop(page, region=None):
    """Return PNG bytes and provenance for a full page or normalized region."""
    path = page_image_path(page)
    with Image.open(path) as source:
        image = source.convert("RGB")
        width, height = image.size
        if region is not None:
            box = (
                max(0, int(region.left * width)),
                max(0, int(region.top * height)),
                min(width, int(region.right * width)),
                min(height, int(region.bottom * height)),
            )
            if box[2] <= box[0] or box[3] <= box[1]:
                raise VisionOcrError("The selected region has no visible area.")
            image = image.crop(box)
        output = BytesIO()
        image.save(output, format="PNG", optimize=True)
        payload = output.getvalue()
    return payload, {"width": image.width, "height": image.height, "source_page_image": page.image_path}


class VisionOcrClient:
    def __init__(self, base_url=None, api_key=None, model=None):
        self.base_url = (base_url or getattr(settings, "DSW_OCR_BASE_URL", "")).rstrip("/")
        self.api_key = api_key if api_key is not None else getattr(settings, "DSW_OCR_API_KEY", "")
        self.model = model or getattr(settings, "DSW_OCR_MODEL", "")
        self.timeout = getattr(settings, "DSW_OCR_TIMEOUT", 300)
        self.max_tokens = getattr(settings, "DSW_OCR_MAX_TOKENS", 4096)
        self.provider = getattr(settings, "DSW_OCR_PROVIDER", "openai-compatible")

    def transcribe(self, image_bytes, prompt):
        if not self.base_url or not self.model:
            raise VisionOcrError("Visual OCR is not configured.")
        encoded = base64.b64encode(image_bytes).decode("ascii")
        if self.provider == "paddleocr-vl":
            return self._transcribe_paddle(encoded)
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
            ]}],
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            response = requests.post(
                f"{self.base_url}/chat/completions", headers=headers,
                json=payload, timeout=self.timeout,
            )
            response.raise_for_status()
            result = response.json()
            text = result["choices"][0]["message"].get("content", "")
        except requests.Timeout as exc:
            raise VisionOcrError("The visual OCR provider timed out.") from exc
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
            raise VisionOcrError("The visual OCR provider returned an invalid response.") from exc
        if isinstance(text, list):
            text = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in text)
        text = str(text or "").strip()
        if not text:
            raise VisionOcrError("The visual OCR provider returned no transcription.")
        return text, result

    def _transcribe_paddle(self, encoded):
        """Call PaddleOCR-VL's full document parsing service.

        Paddle's service intentionally exposes a document API rather than an
        OpenAI chat API. Keeping the adapter here lets DSW use the same job,
        provenance, and acceptance flow for both PaddleOCR-VL and Qwen.
        """
        try:
            response = requests.post(
                f"{self.base_url}/layout-parsing",
                json={
                    "file": encoded, "fileType": 1,
                    "useDocOrientationClassify": False,
                    "useDocUnwarping": False,
                    "returnMarkdownImages": False,
                    "visualize": False,
                }, timeout=self.timeout,
            )
            response.raise_for_status()
            result = response.json()
            pages = result["result"]["layoutParsingResults"]
            text = "\n\n".join(
                page.get("markdown", {}).get("text", "") for page in pages
            ).strip()
        except requests.Timeout as exc:
            raise VisionOcrError("The PaddleOCR-VL provider timed out.") from exc
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
            raise VisionOcrError("The PaddleOCR-VL provider returned an invalid response.") from exc
        if not text:
            raise VisionOcrError("The PaddleOCR-VL provider returned no transcription.")
        return text, result


def request_metadata(image_bytes, image_info):
    return {**image_info, "sha256": hashlib.sha256(image_bytes).hexdigest(), "bytes": len(image_bytes)}
