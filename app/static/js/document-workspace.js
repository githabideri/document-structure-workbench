import {publishDiagnostics} from "./core/diagnostics.js";

const viewport = document.getElementById("scan-viewport");
const stage = document.getElementById("scan-stage");
const zoomValue = document.querySelector("[data-zoom-value]");
let zoom = 1;
let offsetX = 0;
let offsetY = 0;
let pointer = null;
let dragged = false;

function renderZoom() {
    if (!stage) return;
    stage.style.transform = `translate(${offsetX}px, ${offsetY}px) scale(${zoom})`;
    stage.style.transformOrigin = "top left";
    if (zoomValue) zoomValue.textContent = `${Math.round(zoom * 100)}%`;
    publishDiagnostics();
}

function setZoom(next) {
    zoom = Math.max(.5, Math.min(4, next));
    if (zoom <= 1) { offsetX = 0; offsetY = 0; }
    renderZoom();
}

document.querySelector("[data-zoom-in]")?.addEventListener("click", () => setZoom(zoom + .25));
document.querySelector("[data-zoom-out]")?.addEventListener("click", () => setZoom(zoom - .25));
document.querySelector("[data-zoom-fit]")?.addEventListener("click", () => { zoom = 1; offsetX = 0; offsetY = 0; renderZoom(); });

viewport?.addEventListener("wheel", event => {
    event.preventDefault();
    setZoom(zoom * (event.deltaY < 0 ? 1.1 : .9));
}, {passive: false});

viewport?.addEventListener("pointerdown", event => {
    if (event.target.closest(".region-hit, a")) return;
    viewport.setPointerCapture(event.pointerId);
    pointer = {id: event.pointerId, x: event.clientX, y: event.clientY, offsetX, offsetY};
    dragged = false;
});
viewport?.addEventListener("pointermove", event => {
    if (!pointer || pointer.id !== event.pointerId || zoom <= 1) return;
    offsetX = pointer.offsetX + event.clientX - pointer.x;
    offsetY = pointer.offsetY + event.clientY - pointer.y;
    dragged = Math.abs(event.clientX - pointer.x) + Math.abs(event.clientY - pointer.y) > 4;
    renderZoom();
});
const endPan = event => { if (pointer?.id === event.pointerId) pointer = null; };
viewport?.addEventListener("pointerup", endPan);
viewport?.addEventListener("pointercancel", endPan);

const links = [...document.querySelectorAll(".region-hit")];
const applyFilter = () => {
    const filter = document.querySelector("[data-region-filter].is-active")?.dataset.regionFilter || "all";
    const showSuppressed = document.querySelector("[data-show-suppressed]")?.dataset.showSuppressed === "true";
    links.forEach(link => {
        const visible = (filter === "all" || link.dataset.regionType === filter) && (showSuppressed || link.dataset.suppressed !== "true");
        link.hidden = !visible;
        link.tabIndex = visible ? 0 : -1;
    });
    publishDiagnostics();
};
document.querySelectorAll("[data-region-filter]").forEach(button => button.addEventListener("click", () => {
    document.querySelectorAll("[data-region-filter]").forEach(item => item.classList.toggle("is-active", item === button));
    applyFilter();
}));
document.querySelector("[data-show-suppressed]")?.addEventListener("click", event => {
    const control = event.currentTarget;
    control.dataset.showSuppressed = control.dataset.showSuppressed === "true" ? "false" : "true";
    control.setAttribute("aria-pressed", control.dataset.showSuppressed === "true" ? "true" : "false");
    applyFilter();
});
document.querySelector("[data-region-filter].is-active")?.click();

// Lightweight OCR-status polling: while any OCR candidate is pending, fetch a
// small status fragment and swap only that history block — never reload the
// page/image. Polling stops automatically when no candidate remains pending.
(function startOcrPolling() {
    const anyPending = () => !!document.querySelector("[data-ocr-pending='true'][data-ocr-url]");
    let timer = null;
    async function tick() {
        const blocks = [...document.querySelectorAll("[data-ocr-pending='true'][data-ocr-url]")];
        await Promise.all(blocks.map(async block => {
            try {
                const resp = await fetch(block.dataset.ocrUrl, {
                    credentials: "same-origin",
                    headers: {"Accept": "text/html", "X-Requested-With": "XMLHttpRequest"},
                });
                if (!resp.ok) return;
                const html = await resp.text();
                const tmp = document.createElement("template");
                tmp.innerHTML = html;
                const fresh = tmp.content.querySelector(".ocr-history");
                if (fresh && block.parentNode) block.replaceWith(fresh);
            } catch (err) { /* keep the current block on transient errors */ }
        }));
        if (anyPending()) timer = setTimeout(tick, 2500);
    }
    if (anyPending()) tick();
})();

const selected = document.querySelector("[data-selected-region]");
if (selected) document.getElementById(`region-${selected.dataset.selectedRegion}`)?.scrollIntoView({block: "nearest"});
publishDiagnostics();
