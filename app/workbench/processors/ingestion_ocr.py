"""Apply the configured page OCR layer to a Docling processor result."""
import base64
import hashlib
import logging

from .vision_ocr import VisionOcrClient, VisionOcrError

logger = logging.getLogger(__name__)


def _blocks_from_response(response):
    """Extract PaddleX layout blocks across minor response-shape changes."""
    pages = response.get("result", {}).get("layoutParsingResults", []) if isinstance(response, dict) else []
    blocks = []
    for page in pages:
        candidates = page.get("parsing_res_list") or page.get("parsingResList") or []
        if isinstance(candidates, dict):
            candidates = candidates.get("blocks", [])
        for block in candidates:
            bbox = block.get("block_bbox") or block.get("bbox") or block.get("blockBbox")
            text = block.get("block_content") or block.get("text") or block.get("content") or ""
            if isinstance(bbox, (list, tuple)) and len(bbox) >= 4 and text:
                blocks.append({
                    "bbox": [float(value) for value in bbox[:4]],
                    "text": str(text).strip(),
                    "label": block.get("block_label") or block.get("label") or "text",
                })
    return blocks


def _page_text(response, fallback=""):
    pages = response.get("result", {}).get("layoutParsingResults", []) if isinstance(response, dict) else []
    parts = []
    for page in pages:
        markdown = page.get("markdown", {})
        value = markdown.get("text", "") if isinstance(markdown, dict) else markdown
        if value:
            parts.append(str(value).strip())
    return "\n\n".join(part for part in parts if part) or fallback


def _overlap(a, b):
    left = max(a[0], b[0])
    top = max(a[1], b[1])
    right = min(a[2], b[2])
    bottom = min(a[3], b[3])
    if right <= left or bottom <= top:
        return 0.0
    intersection = (right - left) * (bottom - top)
    area_a = max(1.0, (a[2] - a[0]) * (a[3] - a[1]))
    return intersection / area_a


def apply_page_ocr(result, *, client=None, required=True, progress=None):
    """Replace Docling page text with Paddle text and retain OCR provenance.

    Docling still supplies page images, layout regions, and tables. Paddle is
    authoritative only for transcription; matching blocks back onto Docling
    regions preserves the structural geometry and the existing correction API.
    """
    client = client or VisionOcrClient(provider="paddleocr-vl")
    image_pages = sorted(result.page_images)
    if not image_pages:
        raise VisionOcrError("Docling returned no page images for the Paddle OCR stage.")

    for index, page_num in enumerate(image_pages, start=1):
        if progress:
            progress(page_num, len(image_pages))
        image_info = result.page_images[page_num]
        encoded = image_info.get("data", "") if isinstance(image_info, dict) else ""
        if not encoded:
            if required:
                raise VisionOcrError(f"Docling returned no image data for page {page_num}.")
            continue
        try:
            image_bytes = base64.b64decode(encoded)
            text, raw = client.transcribe(image_bytes, "Transcribe this document page faithfully. Preserve reading order and paragraphs.")
        except Exception:
            if required:
                raise
            logger.exception("Optional Paddle OCR failed for page %d", page_num)
            continue

        blocks = _blocks_from_response(raw)
        result.page_texts[page_num] = text
        result.ocr_pages[page_num] = {
            "provider": client.provider,
            "model": client.model,
            "text": text,
            "blocks": blocks,
            "input_sha256": hashlib.sha256(image_bytes).hexdigest(),
        }

        dimensions = result.processor_metadata.get("page_dimensions", {}).get(page_num, {})
        page_width = float(dimensions.get("width") or 0)
        page_height = float(dimensions.get("height") or 0)
        if not blocks or not page_width or not page_height:
            continue
        for region in result.regions:
            if region.get("page_number") != page_num:
                continue
            bbox = region.get("bbox", [0, 0, 0, 0])
            matched = [block for block in blocks if _overlap(bbox, block["bbox"]) >= 0.05]
            if matched:
                matched.sort(key=lambda block: (block["bbox"][1], block["bbox"][0]))
                region["text"] = "\n".join(block["text"] for block in matched)
                region.setdefault("metadata", {})["ocr_provider"] = client.provider

    result.processor_metadata["ocr"] = {
        "provider": client.provider,
        "model": client.model,
        "pages": len(result.ocr_pages),
        "authoritative": True,
    }
    return result
