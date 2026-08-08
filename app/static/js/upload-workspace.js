import {publishDiagnostics} from "./core/diagnostics.js";
import {t} from "./core/i18n.js";

const MAX_SIZE_BYTES = 50 * 1024 * 1024;
const ACCEPTED = {
    "application/pdf": "pdf",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/tiff": "tiff",
};
const ACCEPTED_EXT = /\.(pdf|jpe?g|png|tiff?)$/i;

const dropZone = document.getElementById("file-drop");
const fileInput = document.getElementById("file-input");
const fileInfo = document.getElementById("file-info");
const selectedArea = document.getElementById("file-selected");

function setZoneState(state) {
    if (!dropZone) return;
    dropZone.classList.toggle("dragover", state === "dragover");
    dropZone.classList.toggle("has-file", state === "ready");
    dropZone.classList.toggle("has-error", state === "error");
}

function typeLabel(file) {
    return ACCEPTED[file.type] || (ACCEPTED_EXT.test(file.name) ? file.name.split(".").pop().toUpperCase() : "file");
}

function describeFile(file) {
    const mb = (file.size / 1024 / 1024).toFixed(1);
    return {name: file.name, type: typeLabel(file), size: `${mb} MB`, ready: t("Ready to upload")};
}

function validateFile(file) {
    if (!file) return t("No file was received.");
    if (!ACCEPTED[file.type] && !ACCEPTED_EXT.test(file.name)) {
        return t("“{name}” is not a supported file type. Use PDF, JPG, PNG or TIFF.", {name: file.name});
    }
    if (file.size > MAX_SIZE_BYTES) {
        return t("“{name}” exceeds the 50 MB upload limit.", {name: file.name});
    }
    return null;
}

function renderSelected(file) {
    const meta = describeFile(file);
    setZoneState("ready");
    if (selectedArea) {
        selectedArea.hidden = false;
        selectedArea.querySelector("[data-file-name]").textContent = meta.name;
        selectedArea.querySelector("[data-file-meta]").textContent = `${meta.size.split(" ")[0]} MB · ${meta.type}`;
        selectedArea.querySelector("[data-file-ok]").textContent = meta.ready;
    } else if (fileInfo) {
        fileInfo.textContent = `${meta.name} · ${meta.size} · ${meta.ready}`;
    }
}

function renderError(message) {
    setZoneState("error");
    if (selectedArea) {
        selectedArea.hidden = false;
        selectedArea.querySelector("[data-file-name]").textContent = "";
        selectedArea.querySelector("[data-file-meta]").textContent = t("Upload not possible");
        selectedArea.querySelector("[data-file-ok]").textContent = message;
    } else if (fileInfo) {
        fileInfo.textContent = message;
    }
}

function clearSelection() {
    fileInput.value = "";
    setZoneState("");
    if (selectedArea) selectedArea.hidden = true;
    else if (fileInfo) fileInfo.textContent = t("Maximum file size: 50 MB");
    publishDiagnostics();
}

function applyFile(file) {
    if (!file) return;
    const error = validateFile(file);
    if (error) {
        if (fileInput) fileInput.value = "";
        renderError(error);
        publishDiagnostics();
        return;
    }
    if (fileInput && (!fileInput.files.length || fileInput.files[0] !== file)) {
        try {
            const transfer = new DataTransfer();
            transfer.items.add(file);
            fileInput.files = transfer.files;
        } catch (err) {
            renderError(t("The file could not be attached here. Please use “Choose a file”."));
            publishDiagnostics();
            return;
        }
    }
    renderSelected(file);
    publishDiagnostics();
}

function filesFromDrop(event) {
    const dt = event.dataTransfer;
    if (!dt || !dt.files || !dt.files.length) return [];
    return Array.from(dt.files);
}

fileInput?.addEventListener("change", () => {
    if (fileInput.files && fileInput.files.length) applyFile(fileInput.files[0]);
    else clearSelection();
});

let dragDepth = 0;
dropZone?.addEventListener("dragenter", event => { event.preventDefault(); dragDepth += 1; setZoneState("dragover"); });
dropZone?.addEventListener("dragover", event => { event.preventDefault(); setZoneState("dragover"); });
dropZone?.addEventListener("dragleave", event => {
    event.preventDefault();
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) setZoneState("");
});
dropZone?.addEventListener("drop", event => {
    event.preventDefault();
    dragDepth = 0;
    setZoneState("");
    const files = filesFromDrop(event);
    if (!files.length) return;
    if (files.length > 1) {
        if (fileInput) fileInput.value = "";
        renderError(t("Only one document can be uploaded at a time. Please drop a single file."));
        return;
    }
    applyFile(files[0]);
});

["dragover", "drop"].forEach(evtName => {
    document.addEventListener(evtName, event => {
        if (dropZone && event.target instanceof Node && !dropZone.contains(event.target)) event.preventDefault();
    });
});
