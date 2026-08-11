// Page viewer: a thin OpenSeadragon wrapper plus the region-overlay adapter.
//
// OpenSeadragon is the single authoritative viewport/camera system. Region
// overlays are visual only (pointer-events: none) so panning always works even
// when a drag begins over a region; selection is driven by OSD's canvas-click,
// which only fires for genuine clicks (drag-vs-click is handled by OSD). The
// adapter shape (id/bbox/type/...) is deliberately decoupled from the Django
// model so a future annotation editor (e.g. Annotorious) can replace this layer.
//
// Coordinate model (see imaging.py / Page model):
//   * normalized page bbox  — Docling region normalized to [0,1]×[0,1].
//   * raster pixel bbox     — normalized * actual full-raster pixel dimensions.
//   * OSD viewport rect     — via the canonical OSD conversion
//     (TiledImage.imageToViewportRectangle). Overlay placement, hit testing,
//     focus and HTR lines all share this one adapter so they can never diverge.
//
// Image loading is a small state machine (empty/loading/preview/ready/error)
// whose transitions are driven by OSD *drawing* events (tile-drawn,
// fully-loaded-change), never by "no image" masquerading as a loading state.
import {t} from "../core/i18n.js";

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]
  ));
}

// ---- Deterministic focus ---------------------------------------------------
//
// The final camera target depends only on (page image bounds, region rect,
// current viewer aspect ratio) — never on the previous viewport position/zoom.
// One pure function computes the target bounds; one immediate camera command
// (viewport.fitBounds(rect, true)) carries them out. No staged fit→inspect→
// zoomTo dance, no viewport-level MAX_FOCUS_ZOOM constant.
const FOCUS_PAD_FRAC = 0.30;          // padding relative to region dimensions
const MIN_PAGE_PAD = 0.02;            // page-relative floor so thin regions aren't edge-to-edge
const MIN_SPAN_FRAC = 0.06;           // page-relative minimum span → bounds tiny-region zoom

export function computeRegionFocusBounds(bbox, image, aspect) {
  // bbox: normalized {left,top,width,height}; image: {x,y,width,height} (OSD
  // world bounds). Returns Rect.
  const rw = bbox.width * image.width;
  const rh = bbox.height * image.height;
  const rx = image.x + bbox.left * image.width;
  const ry = image.y + bbox.top * image.height;

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

  const x0 = image.x, y0 = image.y;
  const x1 = image.x + image.width, y1 = image.y + image.height;
  if (fw <= image.width) fx = Math.min(Math.max(fx, x0), x1 - fw);
  else fx = (x0 + x1) / 2 - fw / 2;
  if (fh <= image.height) fy = Math.min(Math.max(fy, y0), y1 - fh);
  else fy = (y0 + y1) / 2 - fh / 2;

  return new OpenSeadragon.Rect(fx, fy, fw, fh);
}

// ---- Canonical region coordinate adapter ----------------------------------
// The single place that converts a normalized page bbox into OSD geometry via
// the active TiledImage's own conversions. Overlay placement, hit testing,
// focus and HTR lines all go through this class.
class RegionAdapter {
  constructor(getItem) {
    this.getItem = getItem;
  }

  item() { return this.getItem ? this.getItem() : null; }

  // normalized bbox → full-raster pixel rect (image-pixel coordinates)
  pixelRect(bbox) {
    const item = this.item();
    if (!item) return null;
    const cs = item.getContentSize(); // {x: width, y: height} in image pixels
    return new OpenSeadragon.Rect(
      bbox.left * cs.x, bbox.top * cs.y,
      bbox.width * cs.x, bbox.height * cs.y,
    );
  }

  // normalized bbox → OSD viewport rectangle (canonical conversion)
  regionToViewport(bbox) {
    const item = this.item();
    if (!item) return null;
    return item.imageToViewportRectangle(this.pixelRect(bbox));
  }

  // OSD viewport point → image-pixel point (inverse conversion for hit testing)
  viewportToPixel(point) {
    const item = this.item();
    if (!item) return null;
    return item.viewportToImageCoordinates(point);
  }

  // image-pixel point → whether it lies inside a normalized bbox
  pixelContains(px, py, bbox) {
    const l = bbox.left * 1, t = bbox.top * 1;
    const r = (bbox.left + bbox.width) * 1, b = (bbox.top + bbox.height) * 1;
    const cs = this.item() ? this.item().getContentSize() : null;
    if (!cs) return false;
    const x = px / cs.x, y = py / cs.y;
    return x >= l && x <= r && y >= t && y <= b;
  }
}

// Region overlay layer — render/select detected regions over the image.
class RegionOverlay {
  constructor(viewer, host) {
    this.viewer = viewer;
    this.host = host;
    this.layers = new Map(); // regionId -> overlay element
    this.regions = [];
    this.adapter = null;
  }

  setAdapter(adapter) { this.adapter = adapter; }

  rebuild(regions) {
    this.clear();
    this.regions = regions || [];
    if (!this.adapter) return;
    for (const region of this.regions) {
      const rect = this.adapter.regionToViewport(region.bbox);
      if (!rect) continue;
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
      this.viewer.addOverlay(el, rect);
      this.layers.set(String(region.id), el);
    }
  }

  clear() {
    for (const el of this.layers.values()) this.viewer.removeOverlay(el);
    this.layers.clear();
  }

  setRegions(regions, selectedId) {
    this.regions = regions || [];
    this.rebuild(this.regions.map((r) => ({
      ...r,
      selected: selectedId != null && String(r.id) === String(selectedId),
    })));
  }

  rectFor(region) { return this.adapter ? this.adapter.regionToViewport(region.bbox) : null; }

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
  regionAtViewportPoint(viewportPoint) {
    if (!this.adapter) return null;
    const px = this.adapter.viewportToPixel(viewportPoint);
    if (!px) return null;
    let best = null;
    let bestArea = Infinity;
    for (const region of this.regions) {
      const r = this.adapter.pixelRect(region.bbox);
      if (this.adapter.pixelContains(px.x, px.y, region.bbox)) {
        const area = r.width * r.height;
        if (area < bestArea) { bestArea = area; best = region; }
      }
    }
    return best;
  }

  // HTR line overlays. HTR source coordinates are crop-relative normalized
  // [0,1] boxes; the crop metadata carries the region's page-normalized bbox
  // (crop.page_bbox / actual_padded_bbox). We map crop-relative → page-normalized
  // → OSD viewport through the same canonical adapter as regions.
  drawHtrLines(lines, crop) {
    this.clearHtrLines();
    if (!this.adapter || !lines || !lines.length) return;
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
      const rect = this.adapter.regionToViewport(frac);
      if (!rect) continue;
      const el = document.createElement("div");
      el.className = "htr-line-overlay";
      el.title = t("Line {order}", {order: line.order || ""});
      this.viewer.addOverlay(el, rect);
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
// empty      — server says this page has no image (never a transient state)
// loading    — an image URL exists but nothing usable has been drawn yet
// preview    — a low/medium level is visible; current viewport not fully resolved
// ready      — the current viewport's required imagery has been drawn/loaded
// error      — an actual OpenSeadragon load failure (not "no image")
//
// preview is meant to be *unobtrusive*: a small badge at the bottom of the
// viewer, never covering the page. It is hidden once the current view is ready.
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
      this.el.hidden = false;
    } else if (state === "loading") {
      this.el.innerHTML = `<div class="viewer-status-inner is-loading"><span class="viewer-spinner" aria-hidden="true"></span><span>${escapeHtml(t("Loading page image…"))}</span></div>`;
      this.el.hidden = false;
    } else if (state === "preview") {
      // Small unobtrusive badge; the page is fully usable while it shows.
      this.el.innerHTML = `<div class="viewer-status-badge">${escapeHtml(t("Loading full-resolution scan…"))}</div>`;
      this.el.hidden = false;
    } else if (state === "error") {
      this.el.innerHTML = `<div class="viewer-status-inner is-error"><span>${escapeHtml(t("The page image could not be loaded."))}</span><button type="button" class="btn btn-sm btn-secondary" data-status-retry>${escapeHtml(t("Retry"))}</button></div>`;
      this.el.hidden = false;
    } else { // ready
      this.el.innerHTML = "";
      this.el.hidden = true;
    }
  }
}

function buildTileSource(levels) {
  // levels: [{url, width, height}, ...] ascending resolution (thumbnail → preview
  // → full). Two+ valid levels become a legacy image pyramid; otherwise fall back
  // to the single-image source. Defensive: drop levels with missing/zero dims.
  if (!levels || !levels.length) return null;
  const valid = levels.filter((l) => l && l.url && l.width > 0 && l.height > 0);
  if (!valid.length) return null;
  const sorted = valid.slice().sort((a, b) => (a.height || 0) - (b.height || 0));
  if (sorted.length > 1) {
    return {type: "legacy-image-pyramid", levels: sorted.map((l) => ({url: l.url, width: l.width, height: l.height}))};
  }
  const only = sorted[0];
  return only && only.url ? {type: "image", url: only.url} : null;
}

export function createViewer(host, {regions = [], initialSelectedId = null, onSelect, statusEl = null, onRetry} = {}) {
  if (!window.OpenSeadragon) throw new Error("OpenSeadragon is not loaded.");
  const viewer = OpenSeadragon({
    element: host,
    tileSources: [],                       // opened via the unified openPage() path
    prefixUrl: "",
    showNavigationControl: false,
    showZoomControl: false,
    showHomeControl: false,
    showFullPageControl: false,
    scrollToZoom: false,                   // ordinary scrolling never zooms (see wheel below)
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
  const adapter = new RegionAdapter(() => viewer.world.getItemAt(0));
  overlay.setAdapter(adapter);
  const status = new ViewerStatus(statusEl);
  status.onRetry = onRetry;
  let ready = false;
  let drawnOnce = false;
  let pendingFocus = null;
  let activeSelectedId = initialSelectedId;
  let deepLevel = null; // highest-resolution level index (null for single image)
  let currentLevels = [];
  let logicalSize = null;
  let rasterSize = null;
  const stats = {
    generation: 1,
    imageLoads: 0,
    selectionChanges: 0,
    focusChanges: 0,
    stateChanges: 0,
    loadEvents: [],
  };

  function track(state) {
    stats.stateChanges += 1;
    stats.loadEvents.push({state, at: Date.now()});
  }

  function currentItem() { return viewer.world.getItemAt(0); }

  function focusBounds(bbox) {
    const item = currentItem();
    if (!item) return;
    const image = item.getBounds();
    const target = computeRegionFocusBounds(bbox, image, viewer.viewport.getAspectRatio());
    // One authoritative, immediate camera move — no staged fit→zoomTo.
    viewer.viewport.fitBounds(target, true);
  }

  function rebuildOverlays(regionList, selId) {
    overlay.setRegions(regionList, selId);
  }

  // Show overlays + resolve pending focus only once the first imagery is on
  // screen — so region boxes never float over a blank canvas.
  function onFirstDraw() {
    if (drawnOnce) return;
    drawnOnce = true;
    rebuildOverlays(regions, activeSelectedId);
    if (pendingFocus) { focusBounds(pendingFocus); pendingFocus = null; }
    if (status.state === "loading") {
      // Truthful: a single image is complete once drawn; a pyramid is only
      // preview until the current viewport is fully resolved.
      status.set(deepLevel == null ? "ready" : "preview");
      track(deepLevel == null ? "ready" : "preview");
    }
  }

  viewer.addHandler("open", () => {
    ready = true;
    drawnOnce = false;
  });

  viewer.addHandler("open-failed", () => {
    ready = false;
    overlay.clear();
    overlay.clearHtrLines();
    status.set("error"); track("error");
  });

  // First real imagery actually drawn → show overlays (never before).
  viewer.addHandler("tile-drawn", () => {
    if (!ready) return;
    onFirstDraw();
  });

  // tile-loaded is a robust alternative signal that real image data exists: in
  // headless/offscreen contexts tile-drawn may never fire. The first loaded tile
  // shows overlays; once a tile at the highest level loads, the page is usable.
  viewer.addHandler("tile-loaded", (event) => {
    if (!ready) return;
    onFirstDraw();
    if (deepLevel != null && status.state === "preview" && event && event.tile && event.tile.level === deepLevel) {
      status.set("ready"); track("ready");
    }
  });

  // Current viewport's required imagery fully loaded → ready (for pyramids).
  viewer.addHandler("fully-loaded-change", (event) => {
    if (!ready || !drawnOnce) return;
    if (event && event.fullyLoaded && status.state === "preview") {
      status.set("ready"); track("ready");
    }
  });

  // A single source whose image fails to load surfaces as tile-load failures
  // (for a plain image source there is exactly one tile). Escalate to error only
  // while nothing has been drawn (a genuine first load that produced nothing);
  // a transient failure on an already-visible image is not fatal.
  viewer.addHandler("tile-load-failed", () => {
    if (status.state === "loading" && !drawnOnce) {
      ready = false;
      overlay.clear();
      overlay.clearHtrLines();
      status.set("error"); track("error");
    }
  });

  viewer.addHandler("canvas-click", (event) => {
    if (event.quick === false) return; // OSD: not a quick click (a drag)
    const point = viewer.viewport.pointFromPixel(event.position);
    const region = overlay.regionAtViewportPoint(point);
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

  // Keyboard selection on the overlay buttons themselves.
  host.addEventListener("keydown", (event) => {
    const el = event.target.closest(".region-overlay");
    if (!el || !el.dataset.regionId) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (onSelect) onSelect(el.dataset.regionId, {focus: true});
    }
  });

  // ---- Unified page-loading path (initial load and page switches are the same) --
  function openPage({imageUrl = null, imageLevels = null, regionList = [], selectedId = null, focus = false, logicalSize = null, rasterSize = null} = {}) {
    regions = regionList || [];
    activeSelectedId = selectedId;
    logicalSize = logicalSize || null;
    rasterSize = rasterSize || null;
    currentLevels = imageLevels || (imageUrl ? [{url: imageUrl, width: null, height: null}] : []);
    stats.imageLoads += 1;
    overlay.clear();
    overlay.clearHtrLines();
    drawnOnce = false;
    ready = false;
    pendingFocus = (focus && activeSelectedId)
      ? (regions.find((r) => String(r.id) === String(activeSelectedId)) || {}).bbox || null
      : null;
    const levels = imageLevels || (imageUrl ? [{url: imageUrl, width: null, height: null}] : null);
    status.set("loading"); track("loading");
    const source = buildTileSource(levels);
    deepLevel = source && source.type === "legacy-image-pyramid"
      ? source.levels.length - 1   // LegacyTileSource sorts ascending; max = full-res
      : null;
    if (source) {
      viewer.open(source);
    } else {
      // no image: close any previously-visible page so we never show stale
      // content or float region overlays over a previous scan.
      viewer.close();
      status.set("empty");
      track("empty");
    }
  }

  return {
    viewer,
    isReady: () => ready,
    status: () => status.state,
    activeSelectedId: () => activeSelectedId,
    loadEvents: () => stats.loadEvents,
    setRegions(regionList, selectedId) {
      regions = regionList;
      overlay.setRegions(regionList, selectedId);
    },
    selectRegion(id, {focus = false} = {}) {
      if (String(id) !== String(activeSelectedId)) {
        overlay.clearHtrLines();
      }
      activeSelectedId = id;
      stats.selectionChanges += 1;
      if (focus) stats.focusChanges += 1;
      overlay.clearSelection();
      overlay.setSelected(id, true);
      const region = regions.find((r) => String(r.id) === String(id));
      if (focus && region) {
        if (ready && drawnOnce) focusBounds(region.bbox);
        else pendingFocus = region.bbox;
      }
    },
    clearSelection() { activeSelectedId = null; overlay.clearSelection(); },
    focusSelected() {
      const region = regions.find((r) => String(r.id) === String(activeSelectedId));
      if (region) { stats.focusChanges += 1; focusBounds(region.bbox); }
    },
    focusRegion(bbox) { if (ready && drawnOnce) focusBounds(bbox); else pendingFocus = bbox; },
    openPage,
    drawHtrLines(lines, crop) { overlay.drawHtrLines(lines, crop); },
    clearHtrLines() { overlay.clearHtrLines(); },
    // Recompute the viewport for a changed container size (splitter drag, page
    // rail collapse, browser/device resize). Overlays are re-placed by OSD from
    // the same image->viewport rects, so image content never drifts.
    resyncGeometry() {
      if (!viewer || !viewer.viewport) return;
      try {
        viewer.updateViewport(true);
      } catch { /* older/edge OSD */ }
    },
    // Load + geometry diagnostics for alignment/debugging reports.
    diagnostics() {
      const item = currentItem();
      const viewport = viewer.viewport;
      const cs = item ? item.getContentSize() : null;
      const imgBounds = item ? item.getBounds() : null;
      const selRegion = regions.find((r) => String(r.id) === String(activeSelectedId));
      return {
        logicalSize,
        rasterSize,
        osdSourceSize: cs ? {width: cs.x, height: cs.y} : null,
        worldBounds: imgBounds ? {x: imgBounds.x, y: imgBounds.y, width: imgBounds.width, height: imgBounds.height} : null,
        levels: currentLevels.map((l) => {
          const e = performance.getEntriesByName(l.url).pop();
          return {url: l.url, declared: {width: l.width, height: l.height},
                  timing: e ? {duration: Math.round(e.duration), transferSize: e.transferSize} : null};
        }),
        selectedRegion: selRegion ? {
          id: selRegion.id,
          normalized: selRegion.bbox,
          pixel: adapter.pixelRect(selRegion.bbox) ? {x: adapter.pixelRect(selRegion.bbox).x, y: adapter.pixelRect(selRegion.bbox).y, width: adapter.pixelRect(selRegion.bbox).width, height: adapter.pixelRect(selRegion.bbox).height} : null,
          viewport: adapter.regionToViewport(selRegion.bbox) ? {x: adapter.regionToViewport(selRegion.bbox).x, y: adapter.regionToViewport(selRegion.bbox).y, width: adapter.regionToViewport(selRegion.bbox).width, height: adapter.regionToViewport(selRegion.bbox).height} : null,
        } : null,
        viewportBounds: viewport ? viewport.getBounds(true) : null,
        zoom: viewport ? viewport.getZoom() : null,
        container: host ? {width: host.clientWidth, height: host.clientHeight} : null,
        devicePixelRatio: window.devicePixelRatio || 1,
        events: stats.loadEvents.map((e) => ({...e})),
      };
    },
    zoomBy(factor) { viewer.viewport.zoomBy(factor); },
    fitWidth() { if (ready) viewer.viewport.fitHorizontally(true); },
    fitPage() { if (ready) viewer.viewport.goHome(); },
    stats: () => stats,
    destroy() { overlay.clear(); overlay.clearHtrLines(); viewer.destroy(); },
  };
}

export {escapeHtml};