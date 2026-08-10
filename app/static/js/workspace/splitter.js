// Workspace layout: draggable viewer/inspector splitter and collapsible page
// navigator. All preferences here are screen/device-specific, so they live in
// localStorage (per-section 2/11 of the plan), unlike recognition-model choice.
import {publishDiagnostics} from "../core/diagnostics.js";

const LS_SPLIT = "dsw.workspace.inspectorWidth";
const LS_PAGES = "dsw.workspace.pagesCollapsed";
const MIN_INSPECTOR = 360;
const MAX_INSPECTOR = 700;
const DEFAULT_INSPECTOR = 440;

function clampWidth(w, max) {
  return Math.max(MIN_INSPECTOR, Math.min(MAX_INSPECTOR, Math.min(w, max)));
}

export function initLayout({shell, splitter, inspector, navigator, opener}) {
  if (!shell || !splitter || !inspector) return;

  // --- Inspector width from localStorage (with a viewport-relative cap) ---
  function applyStoredWidth() {
    const stored = Number(localStorage.getItem(LS_SPLIT));
    const max = Math.min(window.innerWidth - 360, MAX_INSPECTOR);
    const width = Number.isFinite(stored) && stored > 0 ? clampWidth(stored, max) : DEFAULT_INSPECTOR;
    inspector.style.width = `${width}px`;
  }
  applyStoredWidth();

  // --- Draggable splitter with pointer capture (stable if pointer leaves) ---
  let dragging = false;

  function onPointerDown(event) {
    if (event.button !== undefined && event.button !== 0) return;
    dragging = true;
    splitter.setPointerCapture(event.pointerId);
    splitter.classList.add("is-dragging");
    document.body.classList.add("is-splitter-dragging");
    event.preventDefault();
  }

  function onPointerMove(event) {
    if (!dragging) return;
    // Inspector is on the right edge: width = viewport right - pointer x.
    const max = Math.min(window.innerWidth - 360, MAX_INSPECTOR);
    const width = clampWidth(window.innerWidth - event.clientX, max);
    inspector.style.width = `${width}px`;
  }

  function endDrag(event) {
    if (!dragging) return;
    dragging = false;
    try { splitter.releasePointerCapture(event.pointerId); } catch { /* already released */ }
    splitter.classList.remove("is-dragging");
    document.body.classList.remove("is-splitter-dragging");
    localStorage.setItem(LS_SPLIT, String(parseInt(inspector.style.width, 10) || DEFAULT_INSPECTOR));
    publishDiagnostics();
  }

  splitter.addEventListener("pointerdown", onPointerDown);
  splitter.addEventListener("pointermove", onPointerMove);
  splitter.addEventListener("pointerup", endDrag);
  splitter.addEventListener("pointercancel", endDrag);

  // Keyboard: arrow keys nudge the inspector width.
  splitter.addEventListener("keydown", (event) => {
    const step = event.shiftKey ? 40 : 16;
    let width = parseInt(inspector.style.width, 10) || DEFAULT_INSPECTOR;
    if (event.key === "ArrowLeft") width = clampWidth(width + step, MAX_INSPECTOR);
    else if (event.key === "ArrowRight") width = clampWidth(width - step, MAX_INSPECTOR);
    else return;
    event.preventDefault();
    inspector.style.width = `${width}px`;
    localStorage.setItem(LS_SPLIT, String(width));
    publishDiagnostics();
  });

  window.addEventListener("resize", applyStoredWidth);

  // --- Collapsible page navigator ---
  function applyNavigator(collapsed) {
    navigator?.classList.toggle("is-collapsed", collapsed);
    navigator?.setAttribute("aria-hidden", collapsed ? "true" : "false");
    if (opener) opener.hidden = !collapsed;
    publishDiagnostics();
  }
  const storedCollapsed = localStorage.getItem(LS_PAGES) === "true";
  applyNavigator(storedCollapsed);

  navigator?.querySelector("[data-action='collapse-pages']")?.addEventListener("click", () => {
    applyNavigator(true);
    localStorage.setItem(LS_PAGES, "true");
  });
  opener?.addEventListener("click", () => {
    applyNavigator(false);
    localStorage.setItem(LS_PAGES, "false");
  });

  // --- Full-viewport height correction (robust to any navbar height) ---
  function fitShellHeight() {
    const top = shell.getBoundingClientRect().top;
    const height = Math.max(320, window.innerHeight - top);
    shell.style.height = `${height}px`;
  }
  fitShellHeight();
  window.addEventListener("resize", fitShellHeight);
}
