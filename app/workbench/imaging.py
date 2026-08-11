"""Page image presentation derivatives.

Processed document revisions are immutable, so a given ``Page`` row's archival
image never changes. We therefore produce a small thumbnail and a medium
preview derivative exactly once, cache them at a deterministic path keyed by
the immutable page id, and reuse them across requests. The archival image is
never mutated or replaced — these are presentation derivatives only.

Derivative path layout (relative to ``ARTIFACTS_BASE_DIR``)::

    derived/pages/<page_id>/thumbnail.png
    derived/pages/<page_id>/preview.png

No database migration is needed to store derivatives; page identity is the key.
The ``legacy-image-pyramid`` OpenSeadragon source consumes thumbnail → preview
→ full levels, all of which are ordinary PNG files.

Coordinate model
----------------
Two dimension spaces are kept separate (see ``Page`` in ``models.py``):

* **logical** page dimensions (Docling ``page.size``, e.g. 595×841) — used only
  to normalize Docling bounding boxes at import.
* **raster** image dimensions (the actual decoded pixel size of the persisted
  page image, e.g. 1190×1682) — authoritative for viewer geometry.

Every pyramid level must declare the actual pixel dimensions of *its own file*.
Derivatives are generated deterministically from the raster image, so expected
dims (computed without doing the work) always equal the on-disk dims once the
derivative exists.

Generation is deliberately non-blocking for the workspace metadata path: the
viewer is told the deterministic expected size and the URL; the derivative is
created lazily on first request, atomically (temp file + ``os.replace``) so a
partially-written PNG is never exposed even across multiple Gunicorn processes.
"""
import os
import threading
import tempfile
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


def decode_size(path):
    """Return ``(width, height)`` by reading only the image header, or None."""
    try:
        with Image.open(path) as img:
            return img.size
    except Exception:
        return None


def raster_size(page):
    """Authoritative ``(width, height)`` of the persisted page image.

    Prefers the persisted ``Page.image_width``/``image_height`` (set at import
    from Docling ``page.image.size`` or a Pillow decode). If they are missing
    (older revisions), decode the header once and cache it so we never reopen
    the archival image on every workspace request. Returns ``None`` when no
    image is available."""
    if page.image_width and page.image_height:
        return page.image_width, page.image_height
    source = _source_path(page)
    if not source:
        return None
    key = ("raster", page.pk)
    cached = _size_cache.get(key)
    if cached:
        return cached
    size = decode_size(source)
    if size:
        _size_cache[key] = size
    return size


def logical_size(page):
    """The Docling logical page dimensions (used for bbox normalization only)."""
    if page.width and page.height:
        return page.width, page.height
    return None


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


def _expected_dims(page, kind):
    """Deterministic expected (width, height) for a derivative, without doing it."""
    size = raster_size(page)
    if not size:
        return None
    long_edge = THUMBNAIL_LONG_EDGE if kind == "thumbnail" else PREVIEW_LONG_EDGE
    return _resize_dims(size, long_edge)


def derivative_info(page, kind):
    """Return ``(width, height)`` for a derivative's metadata non-blocking.

    Does **not** generate anything. If the derivative already exists its real
    on-disk size is read (header-only, cached); otherwise the deterministic
    expected size is reported so the viewer can build the pyramid immediately
    while the file is created lazily on first request. Returns ``None`` when the
    page has no image or the derivative kind is unknown."""
    if kind not in KINDS:
        raise ValueError(f"Unknown derivative kind: {kind}")
    if not page.image_path:
        return None
    dest = _derivative_path(page.pk, kind)
    size = None
    if dest.exists():
        mtime = dest.stat().st_mtime_ns
        cached = _size_cache.get((page.pk, kind))
        if cached and cached[0] == mtime:
            size = cached[1]
        else:
            size = decode_size(dest)
            if size:
                _size_cache[(page.pk, kind)] = (mtime, size)
    else:
        size = _expected_dims(page, kind)
    if not size:
        return None
    return size


def ensure_derivative(page, kind):
    """Ensure a derivative exists and return ``(path, (width, height))``.

    Idempotent and process-safe: the file is written to a unique temp file and
    atomically ``os.replace``-d into place, so a concurrent worker that wins the
    race simply discards its duplicate and a partially-written PNG is never
    exposed. Returns ``None`` when the page has no image or it cannot be read.
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
        size = decode_size(dest)
        if size:
            _size_cache[(page.pk, kind)] = (mtime, size)
            return dest, size
        # Corrupt existing file (e.g. interrupted legacy write): regenerate.

    with _lock:
        # Re-check under lock in case another worker just created it.
        if dest.exists():
            mtime = dest.stat().st_mtime_ns
            size = decode_size(dest)
            if size:
                _size_cache[(page.pk, kind)] = (mtime, size)
                return dest, size
        source = _source_path(page)
        if not source:
            return None
        try:
            with Image.open(source) as img:
                img = img.convert("RGB")
                nw, nh = _resize_dims(img.size, long_edge)
                resized = img.resize((nw, nh), Image.LANCZOS)
                # Atomic, cross-process-safe write: unique temp + os.replace.
                fd, tmp = tempfile.mkstemp(prefix=f".{kind}-", suffix=".tmp", dir=str(dest.parent))
                try:
                    with os.fdopen(fd, "wb") as fh:
                        resized.save(fh, FORMAT)
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.replace(tmp, dest)
                finally:
                    if os.path.exists(tmp):
                        try:
                            os.unlink(tmp)
                        except OSError:
                            pass
        except Exception:
            return None
    mtime = dest.stat().st_mtime_ns
    size = decode_size(dest)
    if size:
        _size_cache[(page.pk, kind)] = (mtime, size)
    return dest, size