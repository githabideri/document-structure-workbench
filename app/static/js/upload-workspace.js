import {publishDiagnostics} from "./core/diagnostics.js";

// Document upload drop-zone integration.
//
// Keeps the normal ingestion flow (explicit submit of the multipart form)
// unchanged. The drop zone only ensures a dropped file lands in the same
// <input type="file"> the click-to-select path already uses, and gives
// immediate, unambiguous feedback: the file name/type/size and a "ready"
// state, or a visible error for an unsupported/multi/oversized file.
//
// The fix guards every drag propagation step (dragenter/dragover/drop and a
// document-level default-prevent so an accidental drop elsewhere never
// navigates away), validates the dropped file before accepting it, and
// unifies drop + click-select through one apply path.

const MAX_SIZE_BYTES = 50 * 1024 * 1024; // matches the template's "Maximum file size: 50 MB"

// The MIME/file types advertised by the input's `accept` attribute.
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
    // state: "" (neutral) | "dragover" | "ready" | "error"
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
    return {
        name: file.name,
        type: typeLabel(file),
        size: `${mb} MB`,
        ready: "Ready to upload",
    };
}

function validateFile(file) {
    if (!file) return "No file was received.";
    if (!ACCEPTED[file.type] && !ACCEPTED_EXT.test(file.name)) {
        return `“${file.name}” is not a supported file type. Use PDF, JPG, PNG or TIFF.`;
    }
    if (file.size > MAX_SIZE_BYTES) {
        return `“${file.name}” exceeds the 50 MB upload limit.`;
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
    } else {
        // Fallback for older markup: reuse the hint line.
        if (fileInfo) fileInfo.textContent = `${meta.name} · ${meta.size} · ${meta.ready}`;
    }
}

function renderError(message) {
    setZoneState("error");
    if (selectedArea) {
        selectedArea.hidden = false;
        selectedArea.querySelector("[data-file-name]").textContent = "";
        selectedArea.querySelector("[data-file-meta]").textContent = "Upload not possible";
        selectedArea.querySelector("[data-file-ok]").textContent = message;
    } else if (fileInfo) {
        fileInfo.textContent = message;
    }
}

function clearSelection() {
    fileInput.value = "";
    setZoneState("");
    if (selectedArea) {
        selectedArea.hidden = true;
    } else if (fileInfo) {
        fileInfo.textContent = "Maximum file size: 50 MB";
    }
    publishDiagnostics();
}

// The single accepted path from both click-to-select and drop.
function applyFile(file) {
    if (!file) return;
    const error = validateFile(file);
    if (error) {
        // Clear the input so an invalid drop can never slip into submission.
        if (fileInput) fileInput.value = "";
        renderError(error);
        publishDiagnostics();
        return;
    }
    // For a drop the current input has no file yet; set it explicitly so the
    // normal form submission carries the dropped file exactly like a clicked
    // selection would. When this is reached from a real change event the input
    // already holds the file and this is a no-op reassignment.
    if (fileInput && (!fileInput.files.length || fileInput.files[0] !== file)) {
        try {
            const transfer = new DataTransfer();
            transfer.items.add(file);
            fileInput.files = transfer.files;
        } catch (err) {
            // Some engines cannot reassign input.files; keep the original value
            // and still surface the visible selection, but do not claim success
            // for submission.
            renderError("The file could not be attached here. Please use “Choose a file”.");
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
    if (fileInput.files && fileInput.files.length) {
        applyFile(fileInput.files[0]);
    } else {
        clearSelection();
    }
});

let dragDepth = 0;
dropZone?.addEventListener("dragenter", event => {
    event.preventDefault();
    dragDepth += 1;
    setZoneState("dragover");
});
dropZone?.addEventListener("dragover", event => {
    event.preventDefault();
    setZoneState("dragover");
});
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
        renderError("Only one document can be uploaded at a time. Please drop a single file.");
        return;
    }
    applyFile(files[0]);
});

// Prevent an accidental drop/dragover outside the zone from navigating the page
// to the file (or visually echoing a dragover globally). This never selects a
// file; selection only happens when the drop lands on the zone itself.
["dragover", "drop"].forEach(evtName => {
    document.addEventListener(evtName, event => {
        if (dropZone && event.target instanceof Node && !dropZone.contains(event.target)) {
            event.preventDefault();
        }
    });
});
