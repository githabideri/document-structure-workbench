"""Page image presentation derivatives.

Processed document revisions are immutable, so a given ``Page`` row's archival
image never changes. We therefore generate a small thumbnail and a medium
preview derivative exactly once, cache them at a deterministic path keyed by
the immutable page id, and reuse them across requests. The archival image is
never mutated or replaced — these are presentation derivatives only.

Derivative path layout (relative to ``ARTIFACTS_BASE_DIR``)::

    derived/pages/<page_id>/thumbnail.png
    derived/pages/<page_id>/preview.png

No database migration is needed to store derivatives; page identity is the key.
The ``legacy-image-pyramid`` OpenSeadragon source consumes thumbnail → preview
→ full levels, all of which are ordinary PNG files.
"""
import threading
from pathlib import Path

from django.conf import settings

from PIL import Image

# Long-edge targets in source pixels. The page rail is ~160px (scaled ~1.5x by
# devicePixelRatio → ~240px), and the preview should be much smaller/faster than
# the full archive while remaining readable in a normal viewer (1400–2000px).
THUMBNAIL_LONG_EDGE = 240
PREVIEW_LONG_EDGE = 1600

KINDS = {"thumbnail", "preview"}
FORMAT = "PNG"

# (page_id, kind) -> (mtime_ns, (width, height)) so we don't re-decode headers.
_size_cache = {}
_lock = threading.Lock()


def _derivative_path(page_id, kind):
    return (Path(settings.ARTIFACTS_BASE_DIR) / "derived" / "pages" / str(page_id) / f"{kind}.png")


def _source_path(page):
    if not page.image_path:
        return None
    base = Path(settings.ARTIFACTS_BASE_DIR).resolve()
    path = (base / page.image_path).resolve()
    if not path.is_relative_to(base) or not path.exists():
        return None
    return path


def full_size(page):
    """(width, height) of the archival page image, measuring only if needed."""
    if page.width and page.height:
        return page.width, page.height
    source = _source_path(page)
    if not source:
        return None
    with Image.open(source) as img:
        return img.size


def _resize_dims(size, long_edge):
    w, h = size
    if w >= h:
        nw, nh = long_edge, max(1, round(h * long_edge / w))
    else:
        nh, nw = long_edge, max(1, round(w * long_edge / h))
    # Never upscale beyond the source.
    if nw >= w and nh >= h:
        nw, nh = w, h
    return nw, nh


def ensure_derivative(page, kind):
    """Return ``(path, (width, height))`` for a derivative, generating it once.

    Thread-safe; idempotent. Returns ``None`` when the page has no image or the
    image file cannot be read.
    """
    if kind not in KINDS:
        raise ValueError(f"Unknown derivative kind: {kind}")
    long_edge = THUMBNAIL_LONG_EDGE if kind == "thumbnail" else PREVIEW_LONG_EDGE
    dest = _derivative_path(page.pk, kind)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists():
        mtime = dest.stat().st_mtime_ns
        cached = _size_cache.get((page.pk, kind))
        if cached and cached[0] == mtime:
            return dest, cached[1]
        with Image.open(dest) as img:
            size = img.size
        _size_cache[(page.pk, kind)] = (mtime, size)
        return dest, size

    with _lock:
        # Re-check under lock in case another worker just created it.
        if dest.exists():
            mtime = dest.stat().st_mtime_ns
            with Image.open(dest) as img:
                size = img.size
            _size_cache[(page.pk, kind)] = (mtime, size)
            return dest, size
        source = _source_path(page)
        if not source:
            return None
        try:
            with Image.open(source) as img:
                img = img.convert("RGB")
                nw, nh = _resize_dims(img.size, long_edge)
                img.resize((nw, nh), Image.LANCZOS).save(dest, FORMAT)
        except Exception:
            return None
    mtime = dest.stat().st_mtime_ns
    with Image.open(dest) as img:
        size = img.size
    _size_cache[(page.pk, kind)] = (mtime, size)
    return dest, size
