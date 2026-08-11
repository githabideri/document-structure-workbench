// Page viewer: a thin OpenSeadragon wrapper plus the region-overlay adapter.
//
// OpenSeadragon is the single authoritative viewport/camera system. Region
// overlays are visual only (pointer-events: none) so panning always works even
// when a drag begins over a region; selection is driven by OSD's canvas-click,
// which only fires for genuine clicks (drag-vs-click is handled by OSD). The
// adapter shape (id/bbox/type/...) is deliberately decoupled from the Django
// model so a future annotation editor (e.g. Annotorious) can replace this layer.
//
// Image loading is a small state machine (empty/loading/preview/ready/error)
// driven by OpenSeadragon lifecycle events, never by "no image" masquerading
// as a loading state.
import {t} from "../core/i18n.js";

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]
  ));
}

// ---- Deterministic focus ---------------------------------------------------
//
// The final camera target depends only on (page image bounds, region bbox,
// current viewer aspect ratio) — never on the previous viewport position/zoom.
// One pure function computes the target bounds; one immediate camera command
// (viewport.fitBounds(rect, true)) carries them out. No staged fit→inspect→
// zoomTo dance, no viewport-level MAX_FOCUS_ZOOM constant.
const FOCUS_PAD_FRAC = 0.30;          // padding relative to region dimensions
const MIN_PAGE_PAD = 0.02;            // page-relative floor so thin regions aren't edge-to-edge
const MIN_SPAN_FRAC = 0.06;           // page-relative minimum span → bounds tiny-region zoom

export function computeRegionFocusBounds(bbox, image, aspect) {
  // bbox: normalized {left,top,width,height}; image: {x,y,width,height}. Returns Rect.
  const rw = bbox.width * image.width;
  const rh = bbox.height * image.height;
  const rx = image.x + bbox.left * image.width;
  const ry = image.y + bbox.top * image.height;

  // Padding: primarily relative to the region itself, with a small page-relative
  // visual-margin floor for very narrow regions.
  const padX = Math.max(rw * FOCUS_PAD_FRAC, image.width * MIN_PAGE_PAD);
  const padY = Math.max(rh * FOCUS_PAD_FRAC, image.height * MIN_PAGE_PAD);

  const prw = rw + 2 * padX;
  const prh = rh + 2 * padY;
  const prx = rx - padX;
  const pry = ry - padY;

  // Expand outward so the target bounds match the viewer aspect ratio (never
  // crop/shrink the region).
  let fw, fh;
  if (prw / prh > aspect) { fw = prw; fh = fw / aspect; }
  else { fh = prh; fw = fh * aspect; }

  // Page-relative minimum span: avoids absurd over-zoom for tiny regions while
  // remaining image-relative and history-independent.
  const minW = image.width * MIN_SPAN_FRAC;
  const minH = image.height * MIN_SPAN_FRAC;
  if (fw < minW || fh < minH) {
    const scale = Math.max(minW / fw, minH / fh);
    fw *= scale; fh *= scale;
  }

  const cx = prx + prw / 2;
  const cy = pry + prh / 2;
  let fx = cx - fw / 2;
  let fy = cy - fh / 2;

  // Keep the resulting bounds inside the page where practical. Clamping shifts
  // the whole target rect so the (contained) region stays fully visible.
  const x0 = image.x, y0 = image.y;
  const x1 = image.x + image.width, y1 = image.y + image.height;
  if (fw <= image.width) fx = Math.min(Math.max(fx, x0), x1 - fw);
  else fx = (x0 + x1) / 2 - fw / 2;
  if (fh <= image.height) fy = Math.min(Math.max(fy, y0), y1 - fh);
  else fy = (y0 + y1) / 2 - fh / 2;

  return new OpenSeadragon.Rect(fx, fy, fw, fh);
}

// Region overlay layer — render/select detected regions over the image.
class RegionOverlay {
  constructor(viewer, host) {
    this.viewer = viewer;
    this.host = host;
    this.layers = new Map(); // regionId -> overlay element
    this.regions = [];
    this.imageBounds = null;
  }

  setImageBounds(bounds) { this.imageBounds = bounds; }

  rebuild(regions) {
    this.clear();
    this.regions = regions || [];
    if (!this.imageBounds) return;
    for (const region of this.regions) {
      const el = document.createElement("div");
      el.className = `region-overlay region-${region.type}`;
      el.dataset.regionId = String(region.id);
      el.setAttribute("role", "button");
      el.setAttribute("tabindex", "0");
      el.setAttribute(
        "aria-label",
        t("Region {id}: {type}", {id: region.id, type: region.type_label || region.type}),
      );
      if (region.suppressed) el.classList.add("is-suppressed");
      if (region.selected) el.classList.add("is-selected");
      const num = document.createElement("span");
      num.className = "region-overlay-number";
      num.textContent = String(region.id);
      num.setAttribute("aria-hidden", "true");
      el.appendChild(num);
      this.viewer.addOverlay(el, this._rect(region.bbox));
      this.layers.set(String(region.id), el);
    }
  }

  clear() {
    for (const el of this.layers.values()) this.viewer.removeOverlay(el);
    this.layers.clear();
  }

  // Update a single region's visual state (type/suppressed) and rebuild the
  // overlay layer without reopening the page image. Keeps viewer and inspector
  // in lock-step about type and suppressed state.
  setRegions(regions, selectedId) {
    this.regions = regions || [];
    this.rebuild(this.regions.map((r) => ({
      ...r,
      selected: selectedId != null && String(r.id) === String(selectedId),
    })));
  }

  _rect(bbox) {
    const b = this.imageBounds;
    return new OpenSeadragon.Rect(
      b.x + bbox.left * b.width,
      b.y + bbox.top * b.height,
      bbox.width * b.width,
      bbox.height * b.height,
    );
  }

  rectFor(region) { return this.imageBounds ? this._rect(region.bbox) : null; }

  setSelected(id, selected) {
    const key = String(id);
    for (const [rid, el] of this.layers) el.classList.toggle("is-selected", selected && rid === key);
  }

  clearSelection() {
    for (const el of this.layers.values()) el.classList.remove("is-selected");
  }

  // Deterministic hit-test: collect every region containing the point and pick
  // the smallest-area one, so a large enclosing region never captures a click
  // intended for a smaller Docling region. Area ties resolve to first-in-order.
  regionAtPoint(viewportPoint) {
    if (!this.imageBounds) return null;
    let best = null;
    let bestArea = Infinity;
    for (const region of this.regions) {
      const r = this._rect(region.bbox);
      if (viewportPoint.x >= r.x && viewportPoint.x <= r.x + r.width &&
          viewportPoint.y >= r.y && viewportPoint.y <= r.y + r.height) {
        const area = r.width * r.height;
        if (area < bestArea) { bestArea = area; best = region; }
      }
    }
    return best;
  }

  // HTR line overlays (optional, driven from the provenance rail).
  drawHtrLines(lines, crop) {
    this.clearHtrLines();
    if (!this.imageBounds || !lines || !lines.length) return;
    this.htrGroup = document.createElement("div");
    this.htrGroup.className = "htr-line-group";
    this.htrGroup.setAttribute("aria-hidden", "true");
    this.host.appendChild(this.htrGroup);
    const box = (crop && (crop.actual_padded_bbox || crop.page_bbox)) || [0, 0, 1, 1];
    const [px0, py0, px1, py1] = box;
    const pw = px1 - px0, ph = py1 - py0;
    for (const line of lines) {
      const b = line.bbox || {};
      const frac = {
        left: px0 + (b.xmin || 0) * pw,
        top: py0 + (b.ymin || 0) * ph,
        width: ((b.xmax || 0) - (b.xmin || 0)) * pw,
        height: ((b.ymax || 0) - (b.ymin || 0)) * ph,
      };
      const el = document.createElement("div");
      el.className = "htr-line-overlay";
      el.title = t("Line {order}", {order: line.order || ""});
      this.viewer.addOverlay(el, this._rect(frac));
      this.htrLineEls = this.htrLineEls || [];
      this.htrLineEls.push(el);
    }
  }

  clearHtrLines() {
    if (this.htrLineEls) {
      for (const el of this.htrLineEls) this.viewer.removeOverlay(el);
      this.htrLineEls = [];
    }
    if (this.htrGroup) { this.htrGroup.remove(); this.htrGroup = null; }
  }
}

// ---- Loading state machine -------------------------------------------------
//
// empty      — server says this page has no image (never a transient state)
// loading    — an image URL exists but nothing usable has been drawn yet
// preview    — a low/medium level is visible; full-resolution still loading
// ready      — done (single image, or the requested resolution is drawn)
// error      — an actual OpenSeadragon load failure (not "no image")
class ViewerStatus {
  constructor(el) {
    this.el = el;
    this.state = "loading";
    this.onRetry = null;
    if (el) {
      el.addEventListener("click", (e) => {
        if (e.target.closest("[data-status-retry]") && this.onRetry) this.onRetry();
      });
    }
  }

  set(state, opts = {}) {
    this.state = state;
    if (!this.el) return;
    this.el.dataset.state = state;
    if (state === "empty") {
      this.el.innerHTML = `<div class="viewer-status-inner">${escapeHtml(t("No page image is available for this page."))}</div>`;
    } else if (state === "loading") {
      this.el.innerHTML = `<div class="viewer-status-inner is-loading"><span class="viewer-spinner" aria-hidden="true"></span><span>${escapeHtml(t("Loading page image…"))}</span></div>`;
    } else if (state === "preview") {
      this.el.innerHTML = `<div class="viewer-status-inner is-preview">${escapeHtml(t("Loading full-resolution scan…"))}</div>`;
    } else if (state === "error") {
      this.el.innerHTML = `<div class="viewer-status-inner is-error"><span>${escapeHtml(t("The page image could not be loaded."))}</span><button type="button" class="btn btn-sm btn-secondary" data-status-retry>${escapeHtml(t("Retry"))}</button></div>`;
    } else { // ready
      this.el.innerHTML = "";
    }
    this.el.hidden = (state === "ready" || state === "preview");
  }
}

function buildTileSource(levels) {
  // levels: [{url, width, height}, ...] ascending resolution (thumbnail → preview
  // → full). Two+ levels become a legacy image pyramid; otherwise fall back to
  // the single-image source.
  if (!levels || !levels.length) return null;
  const sorted = levels.slice().sort((a, b) => (a.height || 0) - (b.height || 0));
  if (sorted.length > 1) {
    return {type: "legacy-image-pyramid", levels: sorted.map((l) => ({url: l.url, width: l.width, height: l.height}))};
  }
  const only = sorted[0];
  return only && only.url ? {type: "image", url: only.url} : null;
}

export function createViewer(host, {imageUrl = null, imageLevels = null, regions = [], initialSelectedId = null, onSelect, onReady, statusEl = null, onRetry} = {}) {
  if (!window.OpenSeadragon) throw new Error("OpenSeadragon is not loaded.");
  const viewer = OpenSeadragon({
    element: host,
    tileSources: buildTileSource(
      imageLevels || (imageUrl ? [{url: imageUrl, width: null, height: null}] : null)
    ),
    prefixUrl: "",
    showNavigationControl: false,
    showZoomControl: false,
    showHomeControl: false,
    showFullPageControl: false,
    scrollToZoom: false,           // ordinary scrolling never zooms (see wheel below)
    panHorizontal: true,
    panVertical: true,
    gestureSettingsMouse: {dragToPan: true, scrollToZoom: false, dblClickToZoom: true, pinchToZoom: false},
    gestureSettingsTouch: {dragToPan: true, pinchToZoom: true, dblClickToZoom: true},
    minZoomLevel: 0.2,
    maxZoomLevel: 20,
    visibilityRatio: 0.6,
    constrainDuringPan: false,
    animationTime: 0.25,
  });
  const overlay = new RegionOverlay(viewer, host);
  const status = new ViewerStatus(statusEl);
  status.onRetry = onRetry;
  let ready = false;
  let pendingFocus = null;
  let selectedId = initialSelectedId;
  let deepLevel = null; // highest-resolution level index (null for single image)
  const stats = {
    generation: 1,
    imageLoads: (imageLevels || imageUrl) ? 1 : 0,
    selectionChanges: 0,
    focusChanges: 0,
    stateChanges: 0,
    loadEvents: [], // {state, at}
  };

  function track(state) {
    stats.stateChanges += 1;
    stats.loadEvents.push({state, at: Date.now()});
  }

  function refreshImageBounds() {
    const item = viewer.world.getItemAt(0);
    overlay.setImageBounds(item ? item.getBounds() : null);
  }

  function focusBounds(bbox) {
    if (!ready || !overlay.imageBounds) return;
    const b = overlay.imageBounds;
    const target = computeRegionFocusBounds(bbox, b, viewer.viewport.getAspectRatio());
    // One authoritative, immediate camera move — no staged fit→zoomTo.
    viewer.viewport.fitBounds(target, true);
  }

  function rebuildOverlays(regionList, selId) {
    refreshImageBounds();
    overlay.setRegions(regionList, selId);
  }

  viewer.addHandler("open", () => {
    ready = true;
    refreshImageBounds();
    rebuildOverlays(regions, selectedId);
    if (pendingFocus) { focusBounds(pendingFocus); pendingFocus = null; }
    else { viewer.viewport.goHome(); }
    // A single-image source is fully usable at open; a pyramid keeps the subtle
    // "loading full-resolution" state until deep-level tiles are drawn.
    if (deepLevel == null) { status.set("ready"); track("ready"); }
    if (onReady) onReady();
  });

  viewer.addHandler("open-failed", () => {
    ready = false;
    status.set("error"); track("error");
  });

  // A single source whose image fails to load surfaces as tile-load failures
  // (for a plain image source there is exactly one tile). Escalate to error only
  // while the world is empty (i.e. a genuine first load that produced nothing);
  // a transient failure while replacing an already-visible image is not fatal.
  viewer.addHandler("tile-load-failed", () => {
    if (status.state === "loading" && viewer.world.getItemCount() === 0) {
      ready = false;
      status.set("error"); track("error");
    }
  });

  // First real imagery drawn beats any loading state.
  viewer.addHandler("tile-loaded", (event) => {
    if (!ready) return;
    if (status.state === "loading") { status.set("preview"); track("preview"); }
    // When the highest-resolution level tile has actually been drawn, the page
    // is fully usable → ready (single images reach ready via 'open').
    if (deepLevel != null && event && event.tile && event.tile.level === deepLevel) {
      status.set("ready"); track("ready");
    }
  });

  viewer.addHandler("canvas-click", (event) => {
    if (event.quick === false) return; // OSD: not a quick click (a drag)
    const point = viewer.viewport.pointFromPixel(event.position);
    const region = overlay.regionAtPoint(point);
    if (region && onSelect) onSelect(region.id, {focus: true, fromCanvas: true});
  });

  // Wheel: plain/trackpad pans (vertical + horizontal deltas), Ctrl/Cmd+wheel
  // zooms. The page body must not unexpectedly scroll while the pointer is
  // actively navigating the viewer.
  host.addEventListener("wheel", (event) => {
    if (event.ctrlKey || event.metaKey) {
      event.preventDefault();
      const factor = event.deltaY < 0 ? 1.15 : 1 / 1.15;
      const point = viewer.viewport.pointFromPixel(new OpenSeadragon.Point(event.offsetX, event.offsetY));
      viewer.viewport.zoomBy(factor, point);
      return;
    }
    event.preventDefault();
    if (!ready) return;
    const delta = viewer.viewport.deltaPointsFromPixels(new OpenSeadragon.Point(event.deltaX, event.deltaY));
    viewer.viewport.panBy(delta);
  }, {passive: false});

  // Keyboard selection on the overlay buttons themselves (clicks fall through to
  // canvas, but Enter/Space on a focused overlay is a keyboard event).
  host.addEventListener("keydown", (event) => {
    const el = event.target.closest(".region-overlay");
    if (!el || !el.dataset.regionId) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (onSelect) onSelect(el.dataset.regionId, {focus: true});
    }
  });

  function openSource(levels) {
    status.set("loading"); track("loading");
    const source = buildTileSource(levels);
    deepLevel = source && source.type === "legacy-image-pyramid"
      ? source.levels.length - 1   // LegacyTileSource sorts ascending; max = full-res
      : null;
    if (source) {
      viewer.open(source);
    } else {
      ready = false;
      // no image: close any previously-visible page so we never show stale
      // content or float region overlays over the previous scan
      viewer.close();
      status.set("empty");
      track("empty");
    }
  }

  return {
    viewer,
    isReady: () => ready,
    status: () => status.state,
    loadEvents: () => stats.loadEvents,
    setRegions(regionList, selectedId) {
      regions = regionList;
      overlay.setRegions(regionList, selectedId);
    },
    selectRegion(id, {focus = false} = {}) {
      if (String(id) !== String(selectedId)) {
        // Changing region clears the previous region's HTR line overlays.
        overlay.clearHtrLines();
      }
      selectedId = id;
      stats.selectionChanges += 1;
      if (focus) stats.focusChanges += 1;
      overlay.clearSelection();
      overlay.setSelected(id, true);
      const region = regions.find((r) => String(r.id) === String(id));
      if (focus && region) {
        if (ready) focusBounds(region.bbox);
        else pendingFocus = region.bbox;
      }
    },
    clearSelection() { selectedId = null; overlay.clearSelection(); },
    focusSelected() {
      const region = regions.find((r) => String(r.id) === String(selectedId));
      if (region) { stats.focusChanges += 1; focusBounds(region.bbox); }
    },
    focusRegion(bbox) { if (ready) focusBounds(bbox); else pendingFocus = bbox; },
    openPage({imageUrl, imageLevels = null, regionList = [], selectedId = null, focus = false} = {}) {
      regions = regionList;
      selectedId = selectedId;
      stats.imageLoads += 1;
      overlay.clear();
      overlay.clearHtrLines();
      ready = false;
      pendingFocus = focus && selectedId
        ? (regionList.find((r) => String(r.id) === String(selectedId)) || {}).bbox || null
        : null;
      const levels = imageLevels || (imageUrl ? [{url: imageUrl, width: null, height: null}] : null);
      openSource(levels);
    },
    drawHtrLines(lines, crop) { overlay.drawHtrLines(lines, crop); },
    clearHtrLines() { overlay.clearHtrLines(); },
    zoomBy(factor) { viewer.viewport.zoomBy(factor); },
    fitWidth() { if (ready) viewer.viewport.fitHorizontally(true); },
    fitPage() { if (ready) viewer.viewport.goHome(); },
    stats: () => stats,
    destroy() { overlay.clear(); overlay.clearHtrLines(); viewer.destroy(); },
  };
}

export {escapeHtml};
