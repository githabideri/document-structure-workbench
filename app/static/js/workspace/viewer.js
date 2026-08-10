// Page viewer: a thin OpenSeadragon wrapper plus the region-overlay adapter.
//
// OpenSeadragon is the single authoritative viewport/camera system. Region
// overlays are visual only (pointer-events: none) so panning always works even
// when a drag begins over a region; selection is driven by OSD's canvas-click,
// which only fires for genuine clicks (drag-vs-click is handled by OSD). The
// adapter shape (id/bbox/type/...) is deliberately decoupled from the Django
// model so a future annotation editor (e.g. Annotorious) can replace this layer.
import {t} from "../core/i18n.js";

const MAX_FOCUS_ZOOM = 4.0;     // tiny detected regions must not be over-enlarged
const FOCUS_PAD = 0.06;         // padding (fraction of image size) around a focused region

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]
  ));
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
    this.regions = regions;
    if (!this.imageBounds) return;
    for (const region of regions) {
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

  // Hit-test a viewport point against the region rectangles.
  regionAtPoint(viewportPoint) {
    if (!this.imageBounds) return null;
    for (const region of this.regions) {
      const r = this._rect(region.bbox);
      if (viewportPoint.x >= r.x && viewportPoint.x <= r.x + r.width &&
          viewportPoint.y >= r.y && viewportPoint.y <= r.y + r.height) {
        return region;
      }
    }
    return null;
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

export function createViewer(host, {imageUrl, regions = [], initialSelectedId = null, onSelect, onReady} = {}) {
  if (!window.OpenSeadragon) throw new Error("OpenSeadragon is not loaded.");
  const viewer = OpenSeadragon({
    element: host,
    tileSources: imageUrl ? {type: "image", url: imageUrl} : [],
    // No built-in navigation controls: we provide our own toolbar. prefixUrl is
    // left empty so OSD never requests its bundled button images.
    prefixUrl: "",
    showNavigationControl: false,
    showZoomControl: false,
    showHomeControl: false,
    showFullPageControl: false,
    scrollToZoom: false,           // ordinary scrolling never zooms (see modifier-wheel below)
    panHorizontal: true,
    panVertical: true,
    gestureSettingsMouse: {dragToPan: true, scrollToZoom: false, dblClickToZoom: true, pinchToZoom: false},
    gestureSettingsTouch: {dragToPan: true, pinchToZoom: true, dblClickToZoom: true},
    minZoomLevel: 0.2,
    maxZoomLevel: 10,
    visibilityRatio: 0.6,
    constrainDuringPan: false,
    animationTime: 0.25,
  });
  const overlay = new RegionOverlay(viewer, host);
  let ready = false;
  let pendingFocus = null;
  let firstOpen = true;
  let selectedId = initialSelectedId;
  const stats = {generation: 1, imageLoads: imageUrl ? 1 : 0, selectionChanges: 0, focusChanges: 0};

  function refreshImageBounds() {
    const item = viewer.world.getItemAt(0);
    overlay.setImageBounds(item ? item.getBounds() : null);
  }

  function focusBounds(bbox) {
    if (!ready || !overlay.imageBounds) return;
    const b = overlay.imageBounds;
    let rx = b.x + bbox.left * b.width;
    let ry = b.y + bbox.top * b.height;
    let rw = bbox.width * b.width;
    let rh = bbox.height * b.height;
    const padX = b.width * FOCUS_PAD, padY = b.height * FOCUS_PAD;
    rx -= padX; ry -= padY; rw += padX * 2; rh += padY * 2;
    const rect = new OpenSeadragon.Rect(rx, ry, rw, rh);
    viewer.viewport.fitBounds(rect, false);
    const target = viewer.viewport.getZoom(false);
    if (target > MAX_FOCUS_ZOOM) viewer.viewport.zoomTo(MAX_FOCUS_ZOOM, rect.getCenter(), true);
  }

  function rebuildOverlays(regionList, selectedId) {
    refreshImageBounds();
    const withSelection = (regionList || []).map((r) => ({
      ...r, selected: selectedId != null && String(r.id) === String(selectedId),
    }));
    overlay.rebuild(withSelection);
  }

  viewer.addHandler("open", () => {
    ready = true;
    refreshImageBounds();
    rebuildOverlays(regions, initialSelectedId);
    if (pendingFocus) { focusBounds(pendingFocus); pendingFocus = null; }
    else if (firstOpen) { viewer.viewport.goHome(); }
    firstOpen = false;
    if (onReady) onReady();
  });

  viewer.addHandler("canvas-click", (event) => {
    if (event.quick === false) return; // OSD: not a quick click (a drag)
    const point = viewer.viewport.pointFromPixel(event.position);
    const region = overlay.regionAtPoint(point);
    if (region && onSelect) onSelect(region.id, {focus: true, fromCanvas: true});
  });

  // Modifier-wheel zoom: only Ctrl/Cmd + wheel zooms; plain wheel does nothing.
  host.addEventListener("wheel", (event) => {
    if (!(event.ctrlKey || event.metaKey)) return;
    event.preventDefault();
    const factor = event.deltaY < 0 ? 1.15 : 1 / 1.15;
    const point = viewer.viewport.pointFromPixel(new OpenSeadragon.Point(event.offsetX, event.offsetY));
    viewer.viewport.zoomBy(factor, point);
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

  return {
    viewer,
    isReady: () => ready,
    setRegions(regionList, selectedId) {
      regions = regionList;
      rebuildOverlays(regionList, selectedId);
    },
    selectRegion(id, {focus = false} = {}) {
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
      if (region) focusBounds(region.bbox);
    },
    focusRegion(bbox) { if (ready) focusBounds(bbox); else pendingFocus = bbox; },
    openPage({imageUrl: url, regionList = [], selectedId = null, focus = false} = {}) {
      regions = regionList;
      initialSelectedId = selectedId;
      stats.imageLoads += 1;
      overlay.clear();
      overlay.clearHtrLines();
      ready = false;
      pendingFocus = focus && selectedId
        ? (regionList.find((r) => String(r.id) === String(selectedId)) || {}).bbox || null
        : null;
      if (url) viewer.open({type: "image", url});
      else { /* no image: leave viewer cleared */ }
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
