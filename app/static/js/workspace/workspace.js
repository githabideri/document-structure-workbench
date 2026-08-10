// Document workspace controller.
//
// Shell = collapsible page navigator · OpenSeadragon viewer · resizable
// inspector. The governing rule: once a page image is loaded, region-level
// work (selection, recognition runs, corrections, candidate selection, history)
// must NEVER recreate the viewer or reload the page. Only changing *page* may
// load another image.
import {createViewer} from "./viewer.js";
import {initLayout} from "./splitter.js";
import {wireInspector, isInspectorDirty, stopPolling} from "./inspector.js";
import {publishDiagnostics} from "../core/diagnostics.js";
import {t} from "../core/i18n.js";

const shell = document.getElementById("workspace-shell");
if (shell) initWorkspace(shell);

function readJSON(id, fallback) {
  const el = document.getElementById(id);
  if (!el) return fallback;
  try { return JSON.parse(el.textContent); } catch { return fallback; }
}

function emptyInspectorHtml() {
  return `<div class="inspector-empty"><div class="inspector-empty-card">
    <h2>${escape(t("Select a region"))}</h2>
    <p>${escape(t("Click a detected region in the page to inspect and transcribe it. Drag the page to pan; Ctrl/⌘ + scroll to zoom."))}</p>
  </div></div>`;
}

function escape(s) {
  return String(s).replace(/[&<>]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;"}[c]));
}

function initWorkspace(shell) {
  const ds = shell.dataset;
  const state = {
    sourceId: ds.sourceId,
    revisionId: ds.revisionId,
    pageId: ds.pageId || null,
    pageNumber: ds.pageNumber || null,
    regionId: ds.selectedRegion || null,
    inspectorUrl: ds.inspectorUrl || "",
    pageDataUrl: ds.pageDataUrl || "",
    regions: readJSON("page-regions-data", []),
    imageUrl: ds.imageUrl || "",
  };

  const host = document.getElementById("viewer-host");
  const empty = document.getElementById("viewer-empty");
  if (empty) empty.hidden = !!state.imageUrl;
  const viewer = createViewer(host, {
    imageUrl: state.imageUrl || null,
    regions: state.regions,
    initialSelectedId: state.regionId || null,
    onSelect: (regionId, opts) => selectRegion(regionId, opts),
  });
  window.__DSW_WORKSPACE__ = {viewer, state, selectRegion, loadPage};
  if (state.regionId) viewer.selectRegion(state.regionId, {focus: true}); // deep-link focus on open

  // Layout: splitter + page navigator collapse + full-viewport height.
  initLayout({
    shell,
    splitter: document.getElementById("splitter"),
    inspector: document.getElementById("region-inspector"),
    navigator: document.getElementById("page-navigator"),
    opener: document.getElementById("page-navigator-opener"),
  });

  wireViewerToolbar();
  wirePageNavigator();
  wireInspectorRoot();

  // Re-wire the inspector after any HTMX-driven swap of the inspector or rail.
  document.body.addEventListener("htmx:afterSwap", (event) => {
    const id = event.detail && event.detail.target && event.detail.target.id;
    if (id === "region-inspector" || id === "inspector-versions") wireInspectorRoot();
  });

  // Browser back/forward reconstructs page + region state from the URL.
  window.addEventListener("popstate", () => applyUrlState({fromPop: true}));

  // ---- Selection (no viewer recreation, deep-linkable) ----
  function buildUrl({page, region} = {}) {
    const params = new URLSearchParams();
    params.set("revision", state.revisionId);
    params.set("page", page ?? state.pageNumber ?? "");
    if (region != null) params.set("region", region);
    return `${location.pathname}?${params.toString()}`;
  }

  function pushState({page, region}) {
    history.pushState({}, "", buildUrl({page, region}));
  }

  async function selectRegion(regionId, {focus = true, push = true, fromCanvas = false} = {}) {
    if (regionId == null || String(regionId) === String(state.regionId)) {
      if (focus && regionId != null) viewer.selectRegion(regionId, {focus});
      return;
    }
    // Warn if unsaved edits would be obscured.
    if (isInspectorDirty(document.getElementById("region-inspector"))) {
      if (!confirm(t("You have unsaved transcription changes. Leave this region anyway?"))) {
        return;
      }
    }
    state.regionId = String(regionId);
    viewer.selectRegion(regionId, {focus});
    updateFocusButton(regionId);
    // Swap ONLY the inspector — never the viewer.
    await swapInspector(`${reverse("region_inspector", state.regionId)}`);
    if (push) pushState({region: state.regionId});
    publishDiagnostics();
  }

  async function swapInspector(url) {
    const target = document.getElementById("region-inspector");
    try {
      const resp = await fetch(url, {credentials: "same-origin", headers: {"Accept": "text/html", "HX-Request": "true"}});
      if (!resp.ok) return;
      const html = await resp.text();
      target.innerHTML = html;
      target.dataset.regionId = state.regionId || "";
      // The swapped content carries hx-post forms (type/suppress/revert,
      // recognition split buttons, candidate accept). Bind them to HTMX so a
      // click performs an AJAX partial swap instead of a native page submit.
      window.htmx?.process(target);
      wireInspectorRoot();
    } catch { /* transient */ }
  }

  function clearInspector() {
    const target = document.getElementById("region-inspector");
    target.innerHTML = emptyInspectorHtml();
    target.dataset.regionId = "";
    state.regionId = null;
    updateFocusButton(null);
    wireInspectorRoot();
  }

  // ---- Page switching (may load another image; keeps the shell stable) ----
  async function loadPage(pageId, {regionId = null, focus = false, push = true} = {}) {
    if (!pageId) return;
    let data;
    try {
      const resp = await fetch(reverse("page_workspace_data", pageId), {credentials: "same-origin", headers: {"Accept": "application/json"}});
      data = await resp.json();
    } catch { return; }
    state.pageId = String(data.page_id);
    state.pageNumber = String(data.page_number);
    state.regions = data.regions || [];
    state.imageUrl = data.image_url || "";
    shell.dataset.pageId = state.pageId;
    shell.dataset.pageNumber = state.pageNumber;
    if (empty) empty.hidden = !!state.imageUrl;
    viewer.openPage({
      imageUrl: data.image_url || "",
      regionList: state.regions,
      selectedId: regionId,
      focus: focus && regionId != null,
    });
    state.regionId = regionId ? String(regionId) : null;
    updatePageNav(state.pageId);
    updatePageIndicator(state.pageNumber);
    if (regionId) {
      await swapInspector(reverse("region_inspector", regionId));
    } else {
      viewer.clearHtrLines();
      clearInspector();
    }
    if (push) pushState({page: state.pageNumber, region: regionId});
    publishDiagnostics();
  }

  // ---- URL reconciliation (deep links + back/forward) ----
  async function applyUrlState({fromPop = false} = {}) {
    const params = new URLSearchParams(location.search);
    const urlRegion = params.get("region");
    const urlPage = params.get("page");
    const samePage = urlPage && String(urlPage) === String(state.pageNumber);
    if (!samePage && urlPage) {
      const pageId = pageIdFromNumber(urlPage);
      if (pageId) { await loadPage(pageId, {regionId: urlRegion, focus: fromPop || !!urlRegion, push: false}); return; }
    }
    if (urlRegion && String(urlRegion) !== String(state.regionId)) {
      await selectRegion(urlRegion, {focus: fromPop, push: false});
    } else if (!urlRegion && state.regionId) {
      viewer.clearSelection();
      clearInspector();
    }
  }

  function pageIdFromNumber(num) {
    const item = document.querySelector(`.page-list-item[data-page-number="${num}"]`);
    return item ? item.dataset.pageId : null;
  }

  // ---- Viewer toolbar ----
  function wireViewerToolbar() {
    const toolbar = document.getElementById("viewer-toolbar");
    toolbar?.addEventListener("click", (event) => {
      const btn = event.target.closest("[data-viewer-action]");
      if (!btn) return;
      const action = btn.dataset.viewerAction;
      if (action === "zoom-in") viewer.zoomBy(1.25);
      else if (action === "zoom-out") viewer.zoomBy(0.8);
      else if (action === "fit-page") viewer.fitPage();
      else if (action === "fit-width") viewer.fitWidth();
      else if (action === "focus-region") viewer.focusSelected();
    });
    updateFocusButton(state.regionId);
  }

  function updateFocusButton(regionId) {
    const btn = document.querySelector("[data-viewer-action='focus-region']");
    if (btn) btn.disabled = !regionId;
  }

  // ---- Page navigator ----
  function wirePageNavigator() {
    const list = document.getElementById("page-list");
    list?.addEventListener("click", (event) => {
      const item = event.target.closest(".page-list-item");
      if (!item) return;
      const pageId = item.dataset.pageId;
      if (pageId === state.pageId) return;
      if (isInspectorDirty(document.getElementById("region-inspector"))) {
        if (!confirm(t("You have unsaved transcription changes. Leave this page anyway?"))) return;
      }
      loadPage(pageId, {push: true});
    });
  }

  function updatePageNav(pageId) {
    document.querySelectorAll(".page-list-item").forEach((el) => {
      const selected = el.dataset.pageId === String(pageId);
      el.classList.toggle("is-selected", selected);
      if (selected) el.setAttribute("aria-current", "page"); else el.removeAttribute("aria-current");
    });
  }

  function updatePageIndicator(pageNumber) {
    const el = document.getElementById("viewer-page-indicator");
    if (el) el.textContent = pageNumber ? `${t("Page")} ${pageNumber}` : "";
  }

  // ---- Inspector wiring (re-run after every swap) ----
  function wireInspectorRoot() {
    stopPolling();
    const root = document.getElementById("region-inspector");
    if (!root) return;
    wireInspector(root, {
      viewer,
      onFocusRegion: () => viewer.focusSelected(),
    });
    publishDiagnostics();
  }

  publishDiagnostics();
}

// Resolve a named workspace URL client-side. The shell exposes the source id;
// region/page URLs are numeric paths, so we build them directly to avoid
// embedding every reverse() in JS.
function reverse(name, id) {
  const s = document.getElementById("workspace-shell").dataset;
  switch (name) {
    case "region_inspector": return `/regions/${id}/inspector/`;
    case "page_workspace_data": return `/pages/${id}/workspace-data/`;
    default: return "";
  }
}
