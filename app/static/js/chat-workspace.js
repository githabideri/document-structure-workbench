import {apiFetch} from "./core/api-client.js";
import {publishDiagnostics} from "./core/diagnostics.js";

const root = document.querySelector("[data-ui-id='chat-workspace']");
if (!root) {
    publishDiagnostics();
} else {
    const evidencePanel = root.querySelector(".evidence-reader-panel");
    const inspector = root.querySelector(".run-inspector");
    const heading = root.querySelector("[data-ui-id='evidence-heading']");
    const contextSummary = root.querySelector("[data-ui-id='context-summary']");
    let evidenceRequest;
    let evidenceGeneration = 0;
    let originatingCitationId = null;
    let evidenceItems = [];
    let evidenceState = null;

    const params = () => new URL(location.href).searchParams;
    const state = () => ({thread: root.dataset.threadId, run: params().get("run"), evidence: params().get("evidence")});
    const citationFor = (run, marker) => root.querySelector(`[data-run-id="${CSS.escape(String(run))}"][data-evidence-marker="${CSS.escape(marker)}"]`);
    const updateDiagnostics = () => {
        publishDiagnostics();
        const manifest = window.__DSW_UI_DIAGNOSTICS__ || {};
        manifest.url_state = {thread: root.dataset.threadId, run: params().get("run"), evidence: params().get("evidence")};
        manifest.chat = {selected_run_id: evidenceState?.run_id || null, selected_evidence_marker: evidenceState?.marker || null, evidence_open: !evidencePanel.hidden};
        root.querySelector(".chat-context").dataset.open = String(!evidencePanel.hidden || !inspector.hidden);
        window.__DSW_UI_DIAGNOSTICS__ = manifest;
    };
    const setUrl = (run, marker, mode = "push") => {
        const url = new URL(location.href);
        if (run) url.searchParams.set("run", run); else url.searchParams.delete("run");
        if (marker) url.searchParams.set("evidence", marker); else url.searchParams.delete("evidence");
        if (mode === "replace") history.replaceState({run, evidence: marker || null}, "", url);
        else history.pushState({run, evidence: marker || null}, "", url);
    };
    const setLoading = () => {
        heading.textContent = "Loading evidence";
        root.querySelector("[data-evidence-meta]").textContent = "";
        root.querySelector("[data-evidence-passage]").textContent = "";
        root.querySelector("[data-evidence-page-text]").textContent = "";
    };
    const setSelectedCitation = (run, marker) => {
        root.querySelectorAll(".chat-citation").forEach(item => item.setAttribute("aria-current", "false"));
        const citation = citationFor(run, marker);
        if (citation) {
            citation.setAttribute("aria-current", "true");
            originatingCitationId ||= citation.dataset.uiId;
        }
        evidenceItems = [...root.querySelectorAll(`[data-run-id="${CSS.escape(String(run))}"][data-evidence-marker]`)].map(item => item.dataset.evidenceMarker);
    };
    const renderEvidence = item => {
        evidenceState = item;
        heading.textContent = `[${item.marker}] ${item.filename}`;
        root.querySelector("[data-evidence-meta]").textContent = `${item.project} · Revision ${item.revision_id} · Page ${item.page_number ?? "?"}${item.region_id ? ` · Region ${item.region_id}` : ""}`;
        root.querySelector("[data-evidence-passage]").textContent = item.passage || "No matching passage was stored.";
        root.querySelector("[data-evidence-page-text]").textContent = item.page_text || item.passage || "No page text was stored.";
        root.querySelector("[data-evidence-reason]").textContent = item.selection_reason || "—";
        root.querySelector("[data-evidence-revision]").textContent = item.revision_id ?? "—";
        root.querySelector("[data-evidence-score]").textContent = item.score ?? "—";
        const image = root.querySelector("[data-evidence-image]");
        const noImage = root.querySelector("[data-evidence-no-image]");
        image.hidden = !item.image_url;
        noImage.hidden = Boolean(item.image_url);
        if (item.image_url) image.src = item.image_url;
        const open = root.querySelector(".evidence-open-document");
        open.href = item.document_url + `&return_thread=${encodeURIComponent(root.dataset.threadId)}&return_run=${encodeURIComponent(item.run_id)}&return_evidence=${encodeURIComponent(item.marker)}`;
        const index = evidenceItems.indexOf(item.marker);
        root.querySelector("[data-evidence-position]").textContent = evidenceItems.length ? `${index + 1} / ${evidenceItems.length}` : "";
        root.querySelector(".evidence-previous").disabled = index <= 0;
        root.querySelector(".evidence-next").disabled = index < 0 || index >= evidenceItems.length - 1;
        root.querySelectorAll(".chat-citation").forEach(citation => citation.classList.toggle("is-selected", citation.dataset.evidenceMarker === item.marker && citation.dataset.runId === String(item.run_id)));
        updateDiagnostics();
    };
    const closeEvidence = (restoreFocus = true, historyMode = "push") => {
        if (evidenceRequest) evidenceRequest.abort();
        evidencePanel.hidden = true;
        contextSummary.hidden = false;
        evidenceState = null;
        setUrl(params().get("run"), null, historyMode);
        updateDiagnostics();
        if (restoreFocus && originatingCitationId) document.querySelector(`[data-ui-id="${CSS.escape(originatingCitationId)}"]`)?.focus();
    };
    const loadEvidence = async (run, marker, {historyMode = "push", focus = true} = {}) => {
        if (!run || !marker || !citationFor(run, marker)) {
            if (marker) closeEvidence(false, "replace");
            return;
        }
        if (evidenceRequest) evidenceRequest.abort();
        evidenceRequest = new AbortController();
        const generation = ++evidenceGeneration;
        originatingCitationId = citationFor(run, marker)?.dataset.uiId || originatingCitationId;
        setUrl(run, marker, historyMode);
        setSelectedCitation(run, marker);
        evidencePanel.hidden = false;
        contextSummary.hidden = true;
        setLoading();
        updateDiagnostics();
        try {
            const item = await apiFetch(`/chat/runs/${encodeURIComponent(run)}/evidence/${encodeURIComponent(marker)}/`, {signal: evidenceRequest.signal});
            if (generation !== evidenceGeneration || state().run !== String(run) || state().evidence !== marker) return;
            renderEvidence(item);
            if (focus) heading.focus();
        } catch (error) {
            if (error.name === "AbortError" || generation !== evidenceGeneration) return;
            heading.textContent = error.code === "network_error" ? "Evidence could not be loaded" : "Evidence unavailable";
            root.querySelector("[data-evidence-passage]").textContent = error.message || "The evidence request failed.";
            updateDiagnostics();
        }
    };
    root.addEventListener("click", event => {
        const citation = event.target.closest?.(".chat-citation");
        if (citation) {
            event.preventDefault();
            loadEvidence(citation.dataset.runId, citation.dataset.evidenceMarker);
            return;
        }
        if (event.target.closest?.(".evidence-close")) { closeEvidence(); return; }
        if (event.target.closest?.(".inspector-close")) { closeInspector(); return; }
        const inspectorButton = event.target.closest?.(".run-inspector-open");
        if (inspectorButton) { openInspector(inspectorButton.dataset.runId, inspectorButton); return; }
        if (event.target.closest?.(".evidence-previous")) navigateEvidence(-1);
        if (event.target.closest?.(".evidence-next")) navigateEvidence(1);
        const tab = event.target.closest?.("[data-evidence-tab]");
        if (tab) {
            root.querySelectorAll("[data-evidence-tab]").forEach(item => item.setAttribute("aria-selected", String(item === tab)));
            root.querySelectorAll("[data-evidence-view]").forEach(view => { view.hidden = view.dataset.evidenceView !== tab.dataset.evidenceTab; });
        }
    });
    const navigateEvidence = delta => {
        const index = evidenceItems.indexOf(params().get("evidence"));
        const marker = evidenceItems[index + delta];
        if (marker) loadEvidence(params().get("run"), marker);
    };
    const openInspector = async (run, invoking) => {
        inspector.hidden = false;
        evidencePanel.hidden = true;
        contextSummary.hidden = true;
        inspector.dataset.invokingId = invoking.dataset.uiId || "";
        inspector.querySelector("#run-inspector-heading").focus();
        const body = inspector.querySelector("[data-inspector-body]");
        body.innerHTML = "<p>Loading run diagnostics…</p>";
        try {
            const data = await apiFetch(`/chat/runs/${encodeURIComponent(run)}/diagnostics/data/`);
            const d = data;
            body.innerHTML = `<div class="inspector-summary"><strong>${escapeHtml(d.outcome.state)}</strong><span>${escapeHtml(d.outcome.failure_category || d.outcome.stage || "—")}</span><span>${d.retrieval.tool_call_count} tool calls · ${d.retrieval.evidence_count} evidence</span><span>${escapeHtml(d.provider.model || "—")}</span></div><h3>Phases</h3><ol class="inspector-phases">${d.phases.map(phase => `<li class="${phase.status === "failed" ? "is-failed" : ""}"><strong>${escapeHtml(phase.name)}</strong><span>${escapeHtml(phase.summary || "")}</span><small>${phase.duration_ms ?? "—"} ms</small></li>`).join("")}</ol><h3>Retrieval</h3><p>${escapeHtml(d.retrieval.path)} · ${d.retrieval.queries.map(escapeHtml).join(", ") || "No queries"}</p><h3>Evidence</h3><ul class="inspector-evidence">${d.evidence.map(item => `<li><strong>[${escapeHtml(item.marker)}]</strong> ${escapeHtml(item.filename)} · p. ${item.page ?? "?"}<br><span>${escapeHtml(item.passage)}</span></li>`).join("") || "<li>No evidence</li>"}</ul><details><summary>Raw diagnostics</summary><pre>${escapeHtml(JSON.stringify(d.raw, null, 2))}</pre></details>`;
            updateDiagnostics();
        } catch (error) { body.textContent = error.message || "Diagnostics unavailable."; }
    };
    const closeInspector = () => {
        const current = state();
        inspector.hidden = true;
        if (current.run && current.evidence) {
            const hasCurrentEvidence = evidenceState
                && String(evidenceState.run_id) === String(current.run)
                && evidenceState.marker === current.evidence;
            evidencePanel.hidden = false;
            contextSummary.hidden = true;
            if (!hasCurrentEvidence && citationFor(current.run, current.evidence)) {
                loadEvidence(current.run, current.evidence, {historyMode: "replace", focus: false});
            }
        } else {
            evidenceState = null;
            evidencePanel.hidden = true;
            contextSummary.hidden = false;
        }
        const invoking = document.querySelector(`[data-ui-id="${CSS.escape(inspector.dataset.invokingId || "")}"]`);
        setTimeout(() => invoking?.focus(), 0);
        updateDiagnostics();
    };
    const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[char]));
    const composer = root.querySelector("[data-ui-id='composer']");
    const textarea = composer?.querySelector("textarea");
    textarea?.addEventListener("input", () => { textarea.style.height = "auto"; textarea.style.height = `${Math.min(textarea.scrollHeight, 220)}px`; });
    composer?.addEventListener("keydown", event => {
        if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && !event.shiftKey) { event.preventDefault(); composer.requestSubmit(); }
    });
    // Per-turn scope editor on the follow-up composer. The editor is collapsed
    // by default and submits scope_inherit=1 (inherit the previous run's frozen
    // scope). Any change marks the scope dirty, clears the flag, and previews
    // the configuration that will actually be frozen for this question.
    const scopeInherit = composer?.querySelector("[data-scope-inherit]");
    const scopeEditor = composer?.querySelector("[data-scope-editor]");
    if (scopeEditor) {
        const modeSelect = composer.querySelector("[data-scope-mode]");
        const projectSelect = composer.querySelector("[data-scope-project]");
        const summary = composer.querySelector("[data-ui-id='scope-summary']");
        let scopeDirty = false;
        const projectName = id => {
            if (!projectSelect) return "";
            const opt = [...projectSelect.options].find(option => option.value === String(id));
            return opt ? opt.textContent.trim() : "";
        };
        const updateProjectEnabled = () => {
            if (!projectSelect) return;
            const enabled = !modeSelect || modeSelect.value !== "all";
            projectSelect.disabled = !enabled;
            projectSelect.setAttribute("aria-disabled", String(!enabled));
        };
        const updateScopeSummary = () => {
            const attachedCount = [...composer.querySelectorAll("[data-scope-source]:checked")].length;
            let text;
            if (!modeSelect || modeSelect.value === "all") {
                text = `All accessible projects · ${attachedCount} attached`;
            } else {
                const name = projectName(projectSelect.value);
                text = name ? `Project "${name}" · ${attachedCount} attached` : `One project · ${attachedCount} attached`;
            }
            if (scopeDirty) text += " · changed for this question";
            if (summary) summary.textContent = text;
        };
        const markDirty = () => {
            scopeDirty = true;
            if (scopeInherit) scopeInherit.value = "0";
            updateProjectEnabled();
            updateScopeSummary();
        };
        modeSelect?.addEventListener("change", markDirty);
        projectSelect?.addEventListener("change", markDirty);
        composer.querySelectorAll("[data-scope-source]").forEach(input => input.addEventListener("change", markDirty));
        updateProjectEnabled();
        updateScopeSummary();
    }
    document.addEventListener("keydown", event => {
        if (event.key !== "Escape" || ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
        if (!inspector.hidden) closeInspector(); else if (!evidencePanel.hidden) closeEvidence();
    });
    const closeButton = root.querySelector(".evidence-close");
    if (closeButton) closeButton.onclick = event => {
        event.stopPropagation();
        closeEvidence();
    };
    window.addEventListener("popstate", () => {
        const current = state();
        if (current.evidence) loadEvidence(current.run, current.evidence, {historyMode: "replace", focus: true});
        else closeEvidence(false, "replace");
    });
    const initial = state();
    if (initial.evidence) loadEvidence(initial.run, initial.evidence, {historyMode: "replace", focus: false});
    updateDiagnostics();
}
