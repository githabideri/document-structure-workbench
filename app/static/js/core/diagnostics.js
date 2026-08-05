function describe(element) {
    const rect = element.getBoundingClientRect();
    return {
        id: element.dataset.uiId || element.id || null,
        role: element.getAttribute("role") || element.tagName.toLowerCase(),
        name: element.getAttribute("aria-label") || element.getAttribute("aria-labelledby") || element.textContent?.trim().slice(0, 120) || "",
        visible: !element.hidden && rect.width > 0 && rect.height > 0,
        focusable: element.tabIndex >= 0,
        selected: element.getAttribute("aria-selected") === "true" || element.classList.contains("is-selected"),
        bounds: {left: Math.round(rect.left), top: Math.round(rect.top), width: Math.round(rect.width), height: Math.round(rect.height)},
        scrollable: element.scrollHeight > element.clientHeight || element.scrollWidth > element.clientWidth,
        scroll: {x: element.scrollLeft, y: element.scrollTop},
    };
}

export function publishDiagnostics() {
    const areas = [...document.querySelectorAll("[data-ui-id]")].map(describe);
    const focused = document.activeElement;
    const manifest = {
        schema_version: 1,
        workspace: document.body.dataset.workspace || "unknown",
        url_state: Object.fromEntries(new URLSearchParams(location.search)),
        focus: {id: focused?.dataset.uiId || focused?.id || null},
        async_operations: [...document.querySelectorAll("[data-async-kind]")].map(item => ({kind: item.dataset.asyncKind, id: item.dataset.asyncId || null, state: item.dataset.asyncState || "unknown"})),
        areas,
        generated_at: new Date().toISOString(),
    };
    window.__DSW_UI_DIAGNOSTICS__ = manifest;
    if (new URLSearchParams(location.search).get("debug_ui") === "1") {
        document.documentElement.classList.add("debug-ui");
        areas.forEach(area => {
            const element = document.querySelector(`[data-ui-id="${CSS.escape(area.id)}"]`);
            if (element && !element.querySelector(":scope > .ui-debug-label")) {
                const label = document.createElement("span");
                label.className = "ui-debug-label";
                label.textContent = area.id;
                label.setAttribute("aria-hidden", "true");
                element.prepend(label);
            }
        });
    }
    return manifest;
}

document.addEventListener("DOMContentLoaded", publishDiagnostics);
window.addEventListener("resize", publishDiagnostics);
