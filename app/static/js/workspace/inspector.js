// Region inspector interactions: editable transcription, recognition split
// buttons, version/provenance comparison, HTR line overlays and the polling loop.
//
// IMPORTANT: background polling swaps ONLY the #inspector-versions rail so the
// transcription editor (and the viewer) never move while a job runs. Explicit
// user actions may swap the whole inspector; the controller re-wires this module
// after every swap.
import {t} from "../core/i18n.js";

const POLL_INTERVAL = 2500;
let pollTimer = null;

function csrfToken() {
  const meta = document.querySelector("meta[name='csrf-token']");
  if (meta) return meta.content;
  const input = document.querySelector("[name='csrfmiddlewaretoken']");
  return input ? input.value : "";
}

// Simple word-level diff → HTML with <ins>/<del>. Good enough for transcription
// comparison (inspired by eScriptorium's side-by-side candidate view).
function diffHtml(a, b) {
  const aw = String(a || "").split(/(\s+)/);
  const bw = String(b || "").split(/(\s+)/);
  const n = aw.length, m = bw.length;
  const dp = Array.from({length: n + 1}, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      dp[i][j] = aw[i] === bw[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const out = [];
  let i = 0, j = 0;
  const esc = (s) => s.replace(/[&<>]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;"}[c]));
  while (i < n && j < m) {
    if (aw[i] === bw[j]) { out.push(`<span>${esc(aw[i])}</span>`); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push(`<del>${esc(aw[i])}</del>`); i++; }
    else { out.push(`<ins>${esc(bw[j])}</ins>`); j++; }
  }
  while (i < n) { out.push(`<del>${esc(aw[i++])}</del>`); }
  while (j < m) { out.push(`<ins>${esc(bw[j++])}</ins>`); }
  return out.join("");
}

export function isInspectorDirty(root) {
  const area = root.querySelector("#transcription-area");
  return !!area && area.value !== area.dataset.expectedCurrent;
}

function autoSize(area) {
  area.style.height = "auto";
  area.style.height = `${Math.max(96, area.scrollHeight)}px`;
}

function setDirty(area, dirty) {
  const indicator = rootFor(area).querySelector("[data-dirty-state]");
  const saveBtn = rootFor(area).querySelector('[data-transcription-action="save"]');
  if (indicator) {
    indicator.textContent = dirty ? t("Modified — unsaved changes") : "";
    indicator.dataset.state = dirty ? "dirty" : "clean";
  }
  if (saveBtn) saveBtn.disabled = !dirty;
}

function rootFor(el) { return el.closest("#region-inspector"); }

async function saveTranscription(area, ctx) {
  const root = rootFor(area);
  const url = area.dataset.saveUrl;
  const status = root.querySelector("#inspector-status");
  const saveBtn = root.querySelector('[data-transcription-action="save"]');
  const text = area.value;
  if (!text.trim()) {
    if (status) { status.hidden = false; status.textContent = t("Transcription cannot be empty."); status.dataset.kind = "error"; }
    return;
  }
  if (saveBtn) saveBtn.disabled = true;
  try {
    const resp = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {"Content-Type": "application/json", "Accept": "application/json", "X-CSRFToken": csrfToken()},
      body: JSON.stringify({replacement_text: text, expected_current_text: area.dataset.expectedCurrent}),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      if (status) {
        status.hidden = false;
        status.textContent = (data.error && data.error.message) || t("Could not save the correction.");
        status.dataset.kind = "error";
      }
      if (saveBtn) saveBtn.disabled = false;
      return;
    }
    // Success: new baseline, clear dirty, refresh the version rail in place.
    area.value = data.effective_text ?? text;
    area.dataset.expectedCurrent = area.value;
    autoSize(area);
    setDirty(area, false);
    if (status) { status.hidden = true; }
    if (data.versions_html) {
      const versions = root.querySelector("#inspector-versions");
      if (versions) {
        const tmp = document.createElement("template");
        tmp.innerHTML = data.versions_html.trim();
        const fresh = tmp.content.firstElementChild;
        if (fresh && versions.parentNode) versions.replaceWith(fresh);
      }
    }
  } catch (err) {
    if (status) { status.hidden = false; status.textContent = t("Network error — try again."); status.dataset.kind = "error"; }
    if (saveBtn) saveBtn.disabled = false;
  }
}

function wireEditor(root, ctx) {
  const area = root.querySelector("#transcription-area");
  if (!area) return;
  autoSize(area);
  area.addEventListener("input", () => { autoSize(area); setDirty(area, isInspectorDirty(root)); });
  const saveBtn = root.querySelector('[data-transcription-action="save"]');
  saveBtn?.addEventListener("click", () => saveTranscription(area, ctx));
  // Ctrl/Cmd+Enter saves from the keyboard.
  area.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") { event.preventDefault(); saveTranscription(area, ctx); }
  });
}

function wireSplitButtons(root) {
  for (const group of root.querySelectorAll("[data-split-button]")) {
    const hidden = group.querySelector("input[type='hidden']");
    const modelLabel = group.querySelector("[data-recog-model]");
    const options = group.querySelectorAll("[data-recog-option]");
    const menu = group.querySelector(".split-button-menu");
    options.forEach((opt) => {
      opt.addEventListener("click", () => {
        if (hidden) hidden.value = opt.dataset.recogOption;
        if (modelLabel) modelLabel.textContent = opt.dataset.recogLabel;
        options.forEach((o) => o.setAttribute("aria-checked", o === opt ? "true" : "false"));
        if (menu) menu.open = false;
        group.querySelector(".split-button-main")?.focus();
      });
    });
  }
}

function currentText(root) {
  return root.querySelector(".version-current-text")?.textContent || "";
}

function wireVersions(root, ctx) {
  const compare = root.querySelector("#version-compare");
  const compareTitle = root.querySelector("#version-compare-title");
  const compareBody = root.querySelector("#version-compare-body");
  const compareAccept = root.querySelector("#version-compare-accept");
  const closer = root.querySelector("[data-version-compare-close]");

  for (const item of root.querySelectorAll(".version-item")) {
    const btn = item.querySelector("[data-version-select]");
    btn?.addEventListener("click", () => {
      if (btn.disabled) return;
      const text = item.querySelector(".version-text")?.textContent || "";
      const label = item.querySelector(".version-rail-main")?.textContent.trim() || "";
      if (!compare) return;
      compare.hidden = false;
      if (compareTitle) compareTitle.textContent = label;
      if (compareBody) compareBody.innerHTML = diffHtml(currentText(root), text);
      if (compareAccept) {
        compareAccept.hidden = false;
        compareAccept.dataset.acceptUrl = item.dataset.acceptUrl || "";
        compareAccept.disabled = !item.dataset.acceptUrl;
      }
      ctx.onCompareOpen?.(item);
    });
    const linesBtn = item.querySelector("[data-version-lines]");
    linesBtn?.addEventListener("click", async () => {
      const url = item.dataset.detailUrl;
      if (!url) return;
      linesBtn.disabled = true;
      try {
        const resp = await fetch(url, {credentials: "same-origin", headers: {"Accept": "application/json"}});
        const run = await resp.json();
        const result = run.result || {};
        ctx.viewer?.drawHtrLines(result.lines || [], result.crop);
        linesBtn.textContent = t("Hide lines");
        linesBtn.dataset.shown = "true";
      } catch { /* ignore transient errors */ }
      finally { linesBtn.disabled = false; }
    });
  }
  closer?.addEventListener("click", () => { if (compare) compare.hidden = true; });
}

// Poll the version rail while a recognition candidate is pending. Only the rail
// swaps — the editor and viewer are untouched, so the user keeps reading/typing.
export function startPolling(root, onVersionsSwapped) {
  stopPolling();
  const versions = root.querySelector("#inspector-versions");
  if (!versions || versions.dataset.pending !== "true") return;
  async function tick() {
    const url = versions.dataset.pollUrl;
    if (!url) return;
    try {
      const resp = await fetch(url, {credentials: "same-origin", headers: {"Accept": "text/html"}});
      if (resp.ok) {
        const html = await resp.text();
        const tmp = document.createElement("template");
        tmp.innerHTML = html.trim();
        const fresh = tmp.content.firstElementChild;
        if (fresh && versions.parentNode) {
          versions.replaceWith(fresh);
          onVersionsSwapped?.(fresh);
          if (fresh.dataset.pending === "true") { pollTimer = setTimeout(tick, POLL_INTERVAL); return; }
        }
      }
    } catch { /* keep the current rail on transient errors */ }
  }
  pollTimer = setTimeout(tick, POLL_INTERVAL);
}

export function stopPolling() {
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
}

export function wireInspector(root, ctx) {
  wireEditor(root, ctx);
  wireSplitButtons(root);
  wireVersions(root, ctx);
  // Focus region button inside the actions menu.
  root.querySelector("[data-inspector-action='focus-region']")?.addEventListener("click", () => ctx.onFocusRegion?.());
  // "Use transcription" in the comparison area → accept candidate (HTMX full swap).
  root.querySelector("#version-compare-accept")?.addEventListener("click", (event) => {
    const url = event.currentTarget.dataset.acceptUrl;
    if (!url) return;
    if (window.htmx) window.htmx.ajax("POST", url, {target: "#region-inspector", swap: "innerHTML"});
  });
  startPolling(root, (fresh) => wireVersions(root, ctx));
}
