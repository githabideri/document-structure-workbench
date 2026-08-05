import {publishDiagnostics} from "./core/diagnostics.js";

const viewport = document.getElementById("scan-viewport");
const stage = document.getElementById("scan-stage");
const zoomValue = document.querySelector("[data-zoom-value]");
let zoom = 1;
let offsetX = 0;
let offsetY = 0;

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
if (document.querySelector("[data-ocr-pending*='true']")) window.setTimeout(() => window.location.reload(), 3000);
const selected = document.querySelector("[data-selected-region]");
if (selected) document.getElementById(`region-${selected.dataset.selectedRegion}`)?.scrollIntoView({block: "nearest"});
publishDiagnostics();
