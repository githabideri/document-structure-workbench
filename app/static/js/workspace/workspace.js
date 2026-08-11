// Document workspace controller.
//
// Shell = collapsible page navigator · OpenSeadragon viewer · resizable
// inspector with Region/Page tabs. The governing rule: once a page image is
// loaded, region-level work (selection, recognition runs, corrections, candidate
// selection, history, filtering) must NEVER recreate the viewer or reload the
// page. Only changing *page* may load another image.
//
// The inspector has two independent tab panes (#inspector-pane-region and
// #inspector-pane-page). Region-level HTMX swaps retarget to the region pane so
// the tab shell and the viewer stay put.
//
// Async state is guarded centrally: every region/page fetch carries an
// AbortController + a monotonically increasing generation id, and a response may
// only commit DOM/URL state if the entity it was requested for is still the
// current one. Stale responses can never overwrite newer state.
import {createViewer} from "./viewer.js";
import {initLayout} from "./splitter.js";
import {isInspectorDirty, stopPolling, wireInspector, wireSplitButtons, wireVersionRail} from "./inspector.js";
import {publishDiagnostics} from "../core/diagnostics.js";
import {t} from "../core/i18n.js";

const shell = document.getElementById("workspace-shell");
if (shell) initWorkspace(shell);

function readJSON(id, fallback) {
  const el = document.getElementById(id);
  if (!el) return fallback;
  try { return JSON.parse(el.textContent); } catch { return fallback; }
}

function emptyRegionHtml() {
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
    pageInspectorUrl: ds.pageInspectorUrl || "",
    regions: readJSON("page-regions-data", []),
    imageLevels: readJSON("page-image-data", null),
    imageUrl: ds.imageUrl || "",
    logicalSize: readJSON("page-meta-data", null)?.logical ?? null,
    rasterSize: readJSON("page-meta-data", null)?.raster ?? null,
    regionFilter: {type: "all", showSuppressed: false},
  };

  const host = document.getElementById("viewer-host");
  const statusEl = document.getElementById("viewer-status");
  let pagePollTimer = null; // hoisted: used by wirePageInspectorRoot during init

  // ---- Async request guards (item: stale responses never commit) ----
  // Each guarded resource keeps a generation counter + an AbortController. New
  // requests abort the previous one and bump the generation; a response only
  // commits if its captured generation is still the latest.
  const regionReq = {gen: 0, controller: null};
  const pageReq = {gen: 0, controller: null};
  const pagePaneReq = {gen: 0, controller: null};
  const refreshReq = {gen: 0, controller: null};

  function abort(res) {
    if (res && res.controller) { try { res.controller.abort(); } catch { /* ignore */ } }
    res.gen += 1;
    res.controller = null;
  }

  function fetchGuarded(res, url, options = {}) {
    abort(res);
    const controller = new AbortController();
    res.controller = controller;
    const gen = res.gen;
    const signal = controller.signal;
    return fetch(url, {...options, signal, credentials: options.credentials || "same-origin"})
      .then((resp) => ({resp, gen, signal}))
      .catch((err) => {
        if (err && err.name === "AbortError") return {aborted: true, gen};
        return {aborted: true, gen}; // transient network error → treat as no-op
      });
  }

  function isCurrent(res, gen) { return res.gen === gen; }

  const viewer = createViewer(host, {
    regions: visibleRegions(state),
    initialSelectedId: state.regionId || null,
    onSelect: (regionId, opts) => selectRegion(regionId, opts),
    statusEl,
    onRetry: () => viewer.openPage(pageOpenArgs({focus: !!state.regionId})),
  });
  window.__DSW_WORKSPACE__ = {viewer, state, selectRegion, loadPage, refreshRegions, setInspectorTab, applyRegionFilter};
  // Unified load path: the same openPage() used for page switches opens the
  // initial page, so initial and later imagery follow an identical state machine.
  viewer.openPage(pageOpenArgs({focus: !!state.regionId}));

  function pageOpenArgs({focus = false} = {}) {
    return {
      imageUrl: state.imageUrl,
      imageLevels: state.imageLevels,
      regionList: visibleRegions(state),
      selectedId: state.regionId,
      focus: focus && state.regionId != null,
      logicalSize: state.logicalSize,
      rasterSize: state.rasterSize,
    };
  }

  initLayout({
    shell,
    splitter: document.getElementById("splitter"),
    inspector: document.getElementById("region-inspector"),
    navigator: document.getElementById("page-navigator"),
    opener: document.getElementById("page-navigator-opener"),
    onLayoutChange: () => viewer.resyncGeometry(),
  });

  wireViewerToolbar();
  wireRegionFilter();
  wirePageNavigator();
  wireTabs();
  wireInspectorRoot();
  wirePageInspectorRoot();

  // Centralized dirty-editor guard for ANY full-inspector HTMX action
  // (change type, suppress/restore, revert text, accept candidate). Recognition
  // run creation targets only the version rail and is not affected.
  document.body.addEventListener("htmx:confirm", (event) => {
    const target = event.detail?.elt?.getAttribute?.("hx-target");
    if (target === "#inspector-pane-region" && isInspectorDirty(document.getElementById("inspector-pane-region"))) {
      if (!window.confirm(t("You have unsaved transcription changes. Continue anyway?"))) {
        event.preventDefault();
      }
    }
  });

  // Re-wire + resync overlays after any HTMX-driven swap.
  document.body.addEventListener("htmx:afterSwap", (event) => {
    const id = event.detail && event.detail.target && event.detail.target.id;
    if (id === "inspector-pane-region") {
      wireInspectorRoot();
      refreshRegions(); // type/suppress/revert mutate region state; resync overlays
    } else if (id === "inspector-versions") {
      // Rail-only swap: re-wire just the fresh rail (never the editor/split)
      // so repeated recognition runs don't accumulate duplicate listeners.
      wireRailOnly();
    } else if (id === "inspector-pane-page") {
      wirePageInspectorRoot();
    }
  });

  window.addEventListener("popstate", () => applyUrlState({fromPop: true}));

  // The page's authoritative region states, filtered for the viewer. The
  // selected region is always kept visible so a filter can't silently drop it.
  function visibleRegions(ctx) {
    const f = ctx.regionFilter;
    return ctx.regions.filter((r) => {
      if (ctx.regionId && String(r.id) === String(ctx.regionId)) return true;
      if (!f.showSuppressed && r.suppressed) return false;
      if (f.type !== "all" && r.type !== f.type) return false;
      return true;
    });
  }

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
    if (isInspectorDirty(document.getElementById("inspector-pane-region"))) {
      if (!confirm(t("You have unsaved transcription changes. Leave this region anyway?"))) return;
    }
    state.regionId = String(regionId);
    viewer.selectRegion(regionId, {focus});
    updateFocusButton(regionId);
    setInspectorTab("region");
    await swapInspector(reverse("region_inspector", state.regionId));
    if (push) pushState({region: state.regionId});
    publishDiagnostics();
  }

  async function swapInspector(url) {
    const requested = state.regionId;
    const target = document.getElementById("inspector-pane-region");
    const {resp, gen, aborted} = await fetchGuarded(regionReq, url, {headers: {"Accept": "text/html", "HX-Request": "true"}});
    if (aborted || !isCurrent(regionReq, gen)) return; // superseded or aborted
    if (String(requested) !== String(state.regionId)) return; // selection moved on
    if (!resp || !resp.ok || !target) return;
    const html = await resp.text();
    if (!isCurrent(regionReq, gen) || String(requested) !== String(state.regionId)) return;
    target.innerHTML = html;
    target.dataset.regionId = state.regionId || "";
    window.htmx?.process(target);
    wireInspectorRoot();
    publishDiagnostics();
  }

  function clearInspector() {
    const target = document.getElementById("inspector-pane-region");
    target.innerHTML = emptyRegionHtml();
    target.dataset.regionId = "";
    state.regionId = null;
    viewer.clearSelection();
    updateFocusButton(null);
    setInspectorTab("page");
    wireInspectorRoot();
  }

  // ---- Page switching (may load another image; keeps the shell stable) ----
  async function loadPage(pageId, {regionId = null, focus = false, push = true} = {}) {
    if (!pageId) return;
    const {resp, gen, aborted} = await fetchGuarded(pageReq, reverse("page_workspace_data", pageId), {headers: {"Accept": "application/json"}});
    if (aborted || !isCurrent(pageReq, gen)) return;
    if (!resp || !resp.ok) return;
    const data = await resp.json();
    if (!isCurrent(pageReq, gen)) return;
    // The page identity is fixed by the request; only a newer page load may
    // supersede it. Region-level interactions never supersede a page load.
    state.pageId = String(data.page_id);
    state.pageNumber = String(data.page_number);
    state.regions = data.regions || [];
    state.imageLevels = data.image_levels || null;
    state.imageUrl = data.image_url || "";
    state.logicalSize = data.logical_size || null;
    state.rasterSize = data.raster_size || null;
    state.regionId = regionId ? String(regionId) : null;
    shell.dataset.pageId = state.pageId;
    shell.dataset.pageNumber = state.pageNumber;
    viewer.openPage(pageOpenArgs({focus}));
    updatePageNav(state.pageId);
    updatePageIndicator(state.pageNumber);
    // Refresh the page pane for the newly active page.
    await loadPagePane();
    if (!isCurrent(pageReq, gen)) return;
    if (state.regionId) {
      setInspectorTab("region");
      await swapInspector(reverse("region_inspector", state.regionId));
    } else {
      setInspectorTab("page");
      clearInspector();
    }
    if (push) pushState({page: state.pageNumber, region: state.regionId});
    publishDiagnostics();
  }

  async function loadPagePane() {
    if (!state.pageId) return;
    const requestedPage = state.pageId;
    const pane = document.getElementById("inspector-pane-page");
    const {resp, gen, aborted} = await fetchGuarded(pagePaneReq, reverse("page_inspector", state.pageId), {headers: {"Accept": "text/html", "HX-Request": "true"}});
    if (aborted || !isCurrent(pagePaneReq, gen) || String(requestedPage) !== String(state.pageId)) return;
    if (!resp || !resp.ok || !pane) return;
    pane.innerHTML = await resp.text();
    if (!isCurrent(pagePaneReq, gen) || String(requestedPage) !== String(state.pageId)) return;
    window.htmx?.process(pane);
    wirePageInspectorRoot();
  }

  // ---- URL reconciliation (deep links + back/forward) ----
  async function applyUrlState({fromPop = false} = {}) {
    const params = new URLSearchParams(location.search);
    const urlRegion = params.get("region");
    const urlPage = params.get("page");
    const samePage = urlPage && String(urlPage) === String(state.pageNumber);
    if (!samePage && urlPage) {
      const pageId = pageIdFromNumber(urlPage);
      if (pageId) { await loadPage(pageId, {regionId: urlRegion, focus: !!urlRegion, push: false}); return; }
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

  // ---- Region overlay synchronization with inspector mutations ----
  let refreshing = false;
  async function refreshRegions() {
    if (refreshing || !state.pageId) return;
    refreshing = true;
    try {
      const {resp, gen, aborted} = await fetchGuarded(refreshReq, reverse("page_workspace_data", state.pageId), {headers: {"Accept": "application/json"}});
      if (aborted || !isCurrent(refreshReq, gen) || !resp || !resp.ok) return;
      const data = await resp.json();
      if (!isCurrent(refreshReq, gen)) return;
      state.regions = data.regions || state.regions;
      applyRegionFilter();
    } finally { refreshing = false; }
  }

  function applyRegionFilter() {
    viewer.setRegions(visibleRegions(state), state.regionId);
    window.__DSW_WORKSPACE__.state = state;
    publishDiagnostics();
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

  // ---- Region filtering (viewer concern; never reloads the image) ----
  function wireRegionFilter() {
    const typeSel = document.querySelector("[data-viewer-region-filter]");
    const suppressed = document.querySelector("[data-viewer-show-suppressed]");
    typeSel?.addEventListener("change", () => {
      state.regionFilter.type = typeSel.value;
      applyRegionFilter();
    });
    suppressed?.addEventListener("change", () => {
      state.regionFilter.showSuppressed = suppressed.checked;
      applyRegionFilter();
    });
  }

  // ---- Page navigator ----
  function wirePageNavigator() {
    const list = document.getElementById("page-list");
    list?.addEventListener("click", (event) => {
      const item = event.target.closest(".page-list-item");
      if (!item) return;
      const pageId = item.dataset.pageId;
      if (pageId === state.pageId) return;
      if (isInspectorDirty(document.getElementById("inspector-pane-region"))) {
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

  // ---- Inspector tabs (Region / Page) ----
  function setInspectorTab(name) {
    const regionPane = document.getElementById("inspector-pane-region");
    const pagePane = document.getElementById("inspector-pane-page");
    const tabRegion = document.getElementById("tab-region");
    const tabPage = document.getElementById("tab-page");
    const regionActive = name === "region" && !!state.regionId;
    const active = regionActive ? "region" : "page";
    if (regionPane) regionPane.hidden = active !== "region";
    if (pagePane) pagePane.hidden = active !== "page";
    if (tabRegion) {
      tabRegion.classList.toggle("is-active", active === "region");
      tabRegion.disabled = !state.regionId;
      tabRegion.setAttribute("aria-selected", active === "region" ? "true" : "false");
    }
    if (tabPage) {
      tabPage.classList.toggle("is-active", active === "page");
      tabPage.setAttribute("aria-selected", active === "page" ? "true" : "false");
    }
    if (active === "page") wirePageInspectorRoot();
    publishDiagnostics();
  }

  function wireTabs() {
    const tabRegion = document.getElementById("tab-region");
    const tabPage = document.getElementById("tab-page");
    // Tab switching is NON-destructive: the Region DOM/editor stays mounted with
    // its unsaved text intact, so no dirty guard is applied here.
    tabRegion?.addEventListener("click", () => {
      if (!state.regionId) return;
      setInspectorTab("region");
    });
    tabPage?.addEventListener("click", () => {
      setInspectorTab("page");
    });
    // Initial state: Page is the default when no region is selected.
    setInspectorTab(state.regionId ? "region" : "page");
  }

  // ---- Inspector wiring (re-run after every swap) ----
  function wireInspectorRoot() {
    stopPolling();
    const root = document.getElementById("inspector-pane-region");
    if (!root) return;
    wireInspectorInternal(root);
    publishDiagnostics();
  }

  function wireInspectorInternal(root) {
    wireInspector(root, {
      viewer,
      onFocusRegion: () => viewer.focusSelected(),
    });
  }

  // Rail-only rewiring (recognition starts/polling): never touches the editor
  // or split buttons.
  function wireRailOnly() {
    stopPolling();
    const root = document.getElementById("inspector-pane-region");
    if (!root) return;
    wireVersionRail(root, {
      viewer,
      onFocusRegion: () => viewer.focusSelected(),
    });
  }

  // ---- Page pane wiring (split buttons + lightweight pending polling) ----
  function wirePageInspectorRoot() {
    clearTimeout(pagePollTimer);
    const pane = document.getElementById("inspector-pane-page");
    if (!pane || pane.hidden) { return; }
    wireSplitButtons(pane);
    if (window.htmx) window.htmx.process(pane);
    const pending = pane.dataset.asyncState === "pending";
    if (pending && pane.dataset.pollUrl) {
      pagePollTimer = setTimeout(() => pollPagePane(pane.dataset.pollUrl), 3000);
    }
  }

  async function pollPagePane(url) {
    const requestedPage = state.pageId;
    const pane = document.getElementById("inspector-pane-page");
    const {resp, gen, aborted} = await fetchGuarded(pagePaneReq, url, {headers: {"Accept": "text/html", "HX-Request": "true"}});
    if (aborted || !isCurrent(pagePaneReq, gen) || String(requestedPage) !== String(state.pageId)) return;
    if (!resp || !resp.ok || !pane) { wirePageInspectorRoot(); return; }
    pane.innerHTML = await resp.text();
    if (!isCurrent(pagePaneReq, gen) || String(requestedPage) !== String(state.pageId)) return;
    window.htmx?.process(pane);
    wirePageInspectorRoot();
  }

  publishDiagnostics();
}

// Resolve a named workspace URL client-side.
function reverse(name, id) {
  const s = document.getElementById("workspace-shell").dataset;
  switch (name) {
    case "region_inspector": return `/regions/${id}/inspector/`;
    case "page_inspector": return `/pages/${id}/inspector/`;
    case "page_workspace_data": return `/pages/${id}/workspace-data/`;
    default: return "";
  }
}