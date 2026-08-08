import {t} from "./core/i18n.js";

const SVG_NS = "http://www.w3.org/2000/svg";

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

const panel = document.querySelector("[data-htr-region]");
if (panel) {
  const regionId = panel.dataset.htrRegion;
  const enabled = panel.dataset.htrEnabled === "true";
  const csrf = panel.dataset.htrCsrf;
  const statusEl = panel.querySelector("[data-htr-status]");
  const runsEl = panel.querySelector("[data-htr-runs]");
  const lineDetailEl = panel.querySelector("[data-htr-line-detail]");
  const runBtn = panel.querySelector("[data-htr-run]");
  const toggleBtn = panel.querySelector("[data-htr-toggle-lines]");
  const runAgainBtn = panel.querySelector("[data-htr-run-again]");
  const pipelineSelect = panel.querySelector("[data-htr-pipeline]");
  const lineLayer = document.querySelector('g[data-layer="htr-lines"]');

  let selectedRun = null;
  let selectedLine = null;
  let pollTimer = null;
  let inFlight = false;

  function status(msg, kind = "info") {
    statusEl.textContent = msg || "";
    statusEl.dataset.kind = kind;
  }

  async function json(method, url, body = null) {
    const opts = {method, headers: {"X-CSRFToken": csrf, "Accept": "application/json"}, credentials: "same-origin"};
    if (body !== null) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(url, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error((data.error && data.error.message) || `HTTP ${res.status}`);
      err.code = data.error && data.error.code;
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function mapLineToPage(line, crop) {
    const box = (crop && crop.actual_padded_bbox) || (crop && crop.page_bbox) || [0, 0, 1, 1];
    const [px0, py0, px1, py1] = box;
    const pw = px1 - px0;
    const ph = py1 - py0;
    const b = line.bbox || {};
    return {
      x: px0 + (b.xmin || 0) * pw,
      y: py0 + (b.ymin || 0) * ph,
      width: ((b.xmax || 0) - (b.xmin || 0)) * pw,
      height: ((b.ymax || 0) - (b.ymin || 0)) * ph,
    };
  }

  function clearLines() {
    if (!lineLayer) return;
    lineLayer.innerHTML = "";
    lineLayer.setAttribute("hidden", "");
  }

  function renderLines(run) {
    if (!lineLayer) return;
    clearLines();
    const result = run && run.result;
    if (!result || !result.lines || !result.lines.length) return;
    const crop = result.crop || {};
    for (const line of result.lines) {
      const m = mapLineToPage(line, crop);
      const g = document.createElementNS(SVG_NS, "g");
      g.setAttribute("class", "htr-line");
      g.dataset.order = line.order || "";
      g.dataset.label = line.label || "";
      const rect = document.createElementNS(SVG_NS, "rect");
      rect.setAttribute("class", "htr-line-box");
      rect.setAttribute("x", m.x.toFixed(6));
      rect.setAttribute("y", m.y.toFixed(6));
      rect.setAttribute("width", m.width.toFixed(6));
      rect.setAttribute("height", m.height.toFixed(6));
      rect.setAttribute("rx", "0.004");
      const label = document.createElementNS(SVG_NS, "text");
      label.setAttribute("class", "htr-line-order");
      label.setAttribute("x", (m.x + 0.006).toFixed(6));
      label.setAttribute("y", (m.y + 0.018).toFixed(6));
      label.textContent = line.order || "";
      g.appendChild(rect);
      g.appendChild(label);
      const activate = () => selectLine(run, line);
      g.addEventListener("click", activate);
      g.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); activate(); }
      });
      g.setAttribute("role", "button");
      g.setAttribute("tabindex", "0");
      g.setAttribute("aria-label", t("Line {order}: {text}", {order: line.order || "", text: (line.text || "").slice(0, 40)}));
      lineLayer.appendChild(g);
    }
    lineLayer.removeAttribute("hidden");
  }

  function highlightLine(order) {
    if (!lineLayer) return;
    lineLayer.querySelectorAll(".htr-line").forEach((g) => g.classList.toggle("is-selected", g.dataset.order === String(order)));
  }

  function selectLine(run, line) {
    selectedLine = line.order;
    highlightLine(line.order);
    lineDetailEl.hidden = false;
    const confPct = line.confidence != null ? Math.round(line.confidence * 100) : null;
    const segPct = line.segmentation_confidence != null ? Math.round(line.segmentation_confidence * 100) : null;
    const conf = confPct != null ? `${confPct}%` : "—";
    const seg = segPct != null ? `${segPct}%` : "—";
    const labelText = line.label || t("line {order}", {order: line.order || ""});
    lineDetailEl.innerHTML = `
      <div class="htr-line-meta"><strong>${escapeHtml(labelText)}</strong>
        <span>${escapeHtml(conf)} ${escapeHtml(t("transcription"))}</span><span>${escapeHtml(seg)} ${escapeHtml(t("segmentation"))}</span></div>
      <pre class="htr-line-text"></pre>`;
    lineDetailEl.querySelector(".htr-line-text").textContent = line.text || "";
  }

  function fmtDuration(exec) {
    if (!exec || !exec.duration_ms) return "";
    return `${(exec.duration_ms / 1000).toFixed(1)}s`;
  }

  function stateName(state) { return t(state); }

  function renderRunList(runs) {
    if (!runs || !runs.length) {
      runsEl.innerHTML = `<p class="empty-state">${escapeHtml(t("No HTR runs yet."))}</p>`;
      return;
    }
    runsEl.innerHTML = "";
    for (const run of runs) {
      const div = document.createElement("div");
      div.className = "history-item htr-history-item";
      div.dataset.runId = run.id;
      const isSel = selectedRun && String(selectedRun.id) === String(run.id);
      if (isSel) div.classList.add("is-selected");
      const lines = run.result ? run.result.lines.length : 0;
      const dur = fmtDuration(run.result && run.result.execution);
      const stateLabel = run.state === "completed" ? `${t("completed")} · ${t("{count} lines", {count: lines})}${dur ? " · " + dur : ""}` : stateName(run.state);
      const pipelineLabel = run.pipeline_id ? `<span class="badge badge-htr-pipeline">${escapeHtml(run.pipeline_id)}</span>` : "";
      let html = `<strong>#${run.id} · ${escapeHtml(stateLabel)}</strong> ${pipelineLabel}`;
      if (run.state === "completed" && run.error_message) html += `<p class="progress-message error">${escapeHtml(run.error_message)}</p>`;
      else if (run.state === "failed") html += `<p class="progress-message error">${escapeHtml(run.error_message || t("failed"))}</p>`;
      else if (run.state === "queued" || run.state === "processing") html += `<p class="progress-message">${escapeHtml(t("Working…"))}</p>`;
      const runText = run.result && run.result.text;
      if (run.state === "completed" && runText) html += `<pre class="htr-run-text">${escapeHtml(runText)}</pre>`;
      const btns = [];
      if (run.state === "completed") btns.push(`<button class="btn btn-sm btn-secondary" data-action="show">${escapeHtml(t(isSel ? "Hide lines" : "Show lines"))}</button>`);
      if (run.state === "completed" && !run.accepted_correction_id) btns.push(`<button class="btn btn-sm btn-primary" data-action="accept">${escapeHtml(t("Accept as correction"))}</button>`);
      if (run.accepted_correction_id) btns.push(`<span class="badge">${escapeHtml(t("accepted"))}</span>`);
      html += `<div class="htr-run-actions">${btns.join("")}</div>`;
      div.innerHTML = html;
      div.querySelector('[data-action="show"]')?.addEventListener("click", () => {
        if (isSel) { selectedRun = null; clearLines(); lineDetailEl.hidden = true; toggleBtn.hidden = true; }
        else selectRun(run);
        renderRunList(latestRuns);
      });
      div.querySelector('[data-action="accept"]')?.addEventListener("click", () => acceptRun(run));
      runsEl.appendChild(div);
    }
  }

  let latestRuns = [];

  function selectRun(run) {
    selectedRun = run;
    selectedLine = null;
    lineDetailEl.hidden = true;
    renderLines(run);
    toggleBtn.hidden = !(run && run.result && run.result.lines && run.result.lines.length);
    toggleBtn.textContent = t("Hide HTR lines");
  }

  function stopPolling() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  }

  async function pollRun(runId) {
    stopPolling();
    const tick = async () => {
      try {
        const run = await json("GET", `/api/htr-runs/${runId}/`);
        const idx = latestRuns.findIndex((r) => String(r.id) === String(runId));
        if (idx >= 0) latestRuns[idx] = run; else latestRuns.unshift(run);
        latestRuns.sort((a, b) => b.id - a.id);
        if (run.state === "queued" || run.state === "processing") {
          status(t("HTR run #{id} is {state}…", {id: run.id, state: stateName(run.state)}), "info");
          renderRunList(latestRuns);
          pollTimer = setTimeout(tick, 1500);
        } else {
          status(run.state === "completed" ? t("HTR run #{id} completed.", {id: run.id}) : t("HTR run #{id} failed.", {id: run.id}), run.state === "completed" ? "success" : "error");
          renderRunList(latestRuns);
          runAgainBtn.hidden = false;
          runBtn.hidden = true;
          if (run.state === "completed") selectRun(run);
        }
      } catch (err) {
        status(t("Polling error: {message}", {message: err.message}), "error");
      }
    };
    tick();
  }

  async function startRun() {
    if (inFlight) return;
    inFlight = true;
    runBtn.disabled = true;
    status(t("Submitting region to HTR…"));
    try {
      const run = await json("POST", `/api/regions/${regionId}/htr-runs/`, {pipeline_id: pipelineSelect ? pipelineSelect.value : undefined});
      latestRuns.unshift(run);
      latestRuns.sort((a, b) => b.id - a.id);
      renderRunList(latestRuns);
      runBtn.hidden = true;
      runAgainBtn.hidden = true;
      toggleBtn.hidden = true;
      pollRun(run.id);
    } catch (err) {
      status(err.message || t("Could not start HTR run."), "error");
      runBtn.disabled = false;
    } finally {
      inFlight = false;
    }
  }

  async function acceptRun(run) {
    try {
      status(t("Accepting HTR run #{id} as a correction…", {id: run.id}));
      const updated = await json("POST", `/api/htr-runs/${run.id}/accept/`, {});
      const idx = latestRuns.findIndex((r) => String(r.id) === String(run.id));
      if (idx >= 0) latestRuns[idx] = updated;
      renderRunList(latestRuns);
      status(t("Accepted. Reloading to show the corrected region text…"), "success");
      setTimeout(() => window.location.reload(), 1200);
    } catch (err) {
      status(err.message || t("Could not accept this run."), "error");
    }
  }

  runBtn?.addEventListener("click", startRun);
  runAgainBtn?.addEventListener("click", startRun);
  toggleBtn?.addEventListener("click", () => {
    if (lineLayer && !lineLayer.hasAttribute("hidden")) {
      lineLayer.setAttribute("hidden", "");
      toggleBtn.textContent = t("Show HTR lines");
    } else if (selectedRun) {
      renderLines(selectedRun);
      toggleBtn.textContent = t("Hide HTR lines");
    }
  });

  async function init() {
    if (!enabled) {
      status(t("HTR is not enabled on this server."), "info");
      runBtn.disabled = true;
      return;
    }
    runBtn.disabled = false;
    status(t("Loading previous HTR runs…"));
    try {
      const data = await json("GET", `/api/regions/${regionId}/htr-runs/`);
      latestRuns = data.runs || [];
      renderRunList(latestRuns);
      const autoShow = latestRuns.find((r) => r.state === "completed" && r.result && r.result.lines && r.result.lines.length);
      if (autoShow) {
        selectRun(autoShow);
        renderRunList(latestRuns);
        const detailsEl = panel.querySelector("details");
        if (detailsEl) detailsEl.open = true;
      }
      const active = latestRuns.find((r) => r.state === "queued" || r.state === "processing");
      if (active) { runBtn.hidden = true; pollRun(active.id); }
      else if (latestRuns.length) {
        status(t("{count} previous run(s) on this region.", {count: latestRuns.length}), "info");
        runBtn.hidden = true;
        runAgainBtn.hidden = false;
      } else {
        status(t("Ready. Click “Run HTR” to detect and transcribe handwritten lines."), "info");
      }
    } catch (err) {
      status(t("Could not load HTR runs: {message}", {message: err.message}), "error");
    }
  }

  init();
}
