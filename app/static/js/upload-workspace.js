import {publishDiagnostics} from "./core/diagnostics.js";
import {t} from "./core/i18n.js";

// The drop target is a plain <div>, NOT a <label> wrapping the file input.
// In Firefox (notably on Linux), dropping a file onto a <label> that is
// associated with a file input is intercepted by the browser's native
// file-input handling, so the JS drop handler never fires. Decoupling the
// drop target from the input makes drag-and-drop reliable across browsers.
//
// Upload model: each accepted file is POSTed to the single-file XHR endpoint
// one at a time, so a large batch becomes many small requests instead of one
// giant multipart POST (which would tie up a worker and risk the Gunicorn
// timeout). The native form submit is kept as a no-JS fallback.
const form = document.getElementById("upload-form");
const dropZone = document.getElementById("file-drop");
const fileInput = document.getElementById("file-input");
const fileList = document.getElementById("file-list");
const fileSummary = document.getElementById("file-summary");
const projectSelect = document.getElementById("project-select");
const submitButton = form?.querySelector("button[type=submit]");

const DEFAULT_MAX_BYTES = 50 * 1024 * 1024;
const DEFAULT_MAX_FILES = 200;
const MAX_BYTES = Number.parseInt(dropZone?.dataset.maxBytes, 10) || DEFAULT_MAX_BYTES;
const MAX_FILES = Number.parseInt(dropZone?.dataset.maxFiles, 10) || DEFAULT_MAX_FILES;
const MAX_MB = Math.round(MAX_BYTES / 1024 / 1024);

const ACCEPTED = {
    "application/pdf": "pdf",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/tiff": "tiff",
};
const ACCEPTED_EXT = /\.(pdf|jpe?g|png|tiff?)$/i;

// Selected files kept in memory as the source of truth (also mirrored into
// the file input so the no-JS fallback submit still carries them).
let accepted = [];
let rejected = [];
// selectionLocked is true while an upload is running or after a partial
// failure, so the file set cannot change underneath an in-progress batch.
let selectionLocked = false;
let uploading = false;

function setZoneState(state) {
    if (!dropZone) return;
    dropZone.classList.toggle("dragover", state === "dragover");
    dropZone.classList.toggle("has-file", state === "ready" && accepted.length > 0);
    dropZone.classList.toggle("has-error", rejected.length > 0);
}

function typeLabel(file) {
    return ACCEPTED[file.type] || (ACCEPTED_EXT.test(file.name) ? file.name.split(".").pop().toUpperCase() : "file");
}

function describeSize(bytes) {
    if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
    return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function fileKey(file) {
    return `${file.name}|${file.size}|${file.lastModified}`;
}

function validateFile(file) {
    if (!ACCEPTED[file.type] && !ACCEPTED_EXT.test(file.name)) {
        return t("“{name}” is not a supported file type. Use PDF, JPG, PNG or TIFF.", {name: file.name});
    }
    if (file.size > MAX_BYTES) {
        return t("“{name}” exceeds the {mb} MB upload limit.", {name: file.name, mb: MAX_MB});
    }
    return null;
}

function syncInputFiles() {
    if (!fileInput) return;
    try {
        const transfer = new DataTransfer();
        for (const file of accepted) transfer.items.add(file);
        fileInput.files = transfer.files;
    } catch (err) {
        // Some engines reject reassigning .files; the no-JS fallback still
        // works via the native picker, so this is best-effort.
    }
}

function rowFor(file) {
    return fileList?.querySelector(`[data-key="${CSS.escape(fileKey(file))}"]`) || null;
}

function setStatus(file, state, text) {
    const row = rowFor(file);
    if (!row) return;
    row.classList.remove("done", "error", "uploading");
    if (state) row.classList.add(state);
    const status = row.querySelector(".file-item-status");
    if (status) status.textContent = text;
}

function render() {
    setZoneState("ready");

    if (!fileList) return;
    fileList.innerHTML = "";

    const renderRow = ({file, error}) => {
        const li = document.createElement("li");
        li.className = error ? "file-item file-item-error" : "file-item";
        li.dataset.key = fileKey(file);

        const main = document.createElement("span");
        main.className = "file-item-main";
        const name = document.createElement("span");
        name.className = "file-item-name";
        name.textContent = file.name;
        const meta = document.createElement("span");
        meta.className = "file-item-meta";
        meta.textContent = error || `${describeSize(file.size)} · ${typeLabel(file)}`;
        main.append(name, meta);

        const status = document.createElement("span");
        status.className = "file-item-status";
        status.textContent = error ? "" : t("Ready to upload");

        li.append(main, status);

        if (!error) {
            const remove = document.createElement("button");
            remove.type = "button";
            remove.className = "file-item-remove";
            remove.textContent = t("Remove");
            remove.addEventListener("click", () => {
                if (selectionLocked) return;
                accepted = accepted.filter((f) => f !== file);
                syncInputFiles();
                render();
                publishDiagnostics();
            });
            li.append(remove);
        }
        fileList.append(li);
    };

    for (const file of accepted) renderRow({file});
    for (const {file, error} of rejected) renderRow({file, error});

    const hasAny = accepted.length || rejected.length;
    fileList.hidden = !hasAny;
    renderSelectionSummary();
}

function renderSelectionSummary() {
    if (!fileSummary) return;
    const total = accepted.reduce((sum, f) => sum + f.size, 0);
    if (!accepted.length && !rejected.length) {
        fileSummary.hidden = true;
        fileSummary.textContent = "";
        return;
    }
    const parts = [t("{count} file(s) ready · {size}", {count: accepted.length, size: describeSize(total)})];
    if (rejected.length) parts.push(t("{count} rejected", {count: rejected.length}));
    fileSummary.hidden = false;
    fileSummary.textContent = parts.join(" · ");
}

function addFiles(incoming) {
    if (selectionLocked) return;
    const seen = new Set(accepted.map(fileKey));
    for (const file of incoming) {
        if (seen.has(fileKey(file))) continue;
        const error = validateFile(file);
        if (error) {
            rejected.push({file, error});
            continue;
        }
        if (accepted.length >= MAX_FILES) {
            rejected.push({
                file,
                error: t("Too many files. At most {max} can be uploaded at once.", {max: MAX_FILES}),
            });
            continue;
        }
        accepted.push(file);
        seen.add(fileKey(file));
    }
    syncInputFiles();
    render();
    publishDiagnostics();
}

function clearAll() {
    if (selectionLocked) return;
    accepted = [];
    rejected = [];
    if (fileInput) fileInput.value = "";
    setZoneState("");
    if (fileList) {
        fileList.innerHTML = "";
        fileList.hidden = true;
    }
    renderSelectionSummary();
    publishDiagnostics();
}

function filesFromDrop(event) {
    const dt = event.dataTransfer;
    if (!dt || !dt.files || !dt.files.length) return [];
    return Array.from(dt.files);
}

function setControlsDisabled(disabled) {
    submitButton && (submitButton.disabled = disabled);
    projectSelect && (projectSelect.disabled = disabled);
    form?.querySelectorAll('input[name="preset"]').forEach((el) => (el.disabled = disabled));
}

// Upload each accepted file one at a time. Succeeded files are removed from
// `accepted` so a retry only re-sends the failures; their rows stay in the DOM
// marked as queued.
async function uploadBatch() {
    if (uploading) return;
    if (!projectSelect || !projectSelect.value) {
        if (fileSummary) {
            fileSummary.hidden = false;
            fileSummary.textContent = t("Choose a project first.");
        }
        return;
    }
    if (!accepted.length) return;

    uploading = true;
    selectionLocked = true;
    setControlsDisabled(true);
    publishDiagnostics();

    const endpoint = form.dataset.uploadEndpoint;
    const csrf = form.querySelector('[name="csrfmiddlewaretoken"]')?.value || "";
    const preset = form.querySelector('input[name="preset"]:checked')?.value || "";
    const batch = [...accepted];
    let okCount = 0;
    let collectionUrl = null;
    let singleJobUrl = null;

    for (const file of batch) {
        setStatus(file, "uploading", t("Uploading…"));
        const body = new FormData();
        body.append("file", file);
        body.append("project", projectSelect.value);
        body.append("preset", preset);
        body.append("csrfmiddlewaretoken", csrf);
        try {
            const resp = await fetch(endpoint, {
                method: "POST",
                body,
                headers: {"X-CSRFToken": csrf, "X-Requested-With": "XMLHttpRequest"},
            });
            const data = await resp.json().catch(() => ({}));
            if (resp.ok && data.status === "queued") {
                okCount += 1;
                collectionUrl = data.collection_url || collectionUrl;
                singleJobUrl = data.job_status_url || singleJobUrl;
                setStatus(file, "done", t("Queued"));
                accepted = accepted.filter((f) => f !== file);
            } else {
                setStatus(file, "error", data.error || t("Upload failed."));
            }
        } catch (err) {
            setStatus(file, "error", t("Upload failed."));
        }
    }

    syncInputFiles();
    uploading = false;

    if (okCount === batch.length) {
        // Everything queued: go to the single job's status, or the project.
        const target = batch.length === 1 ? singleJobUrl : collectionUrl;
        if (target) {
            window.location.href = target;
            return;
        }
    }

    // Partial or total failure: stay on the page so the user can see which
    // files failed and why. Re-enable submit to retry just the failures.
    setControlsDisabled(false);
    if (fileSummary) {
        fileSummary.hidden = false;
        fileSummary.textContent = t("{ok} of {total} file(s) queued.", {ok: okCount, total: batch.length});
    }
}

// Open the native picker when the drop zone is clicked.
dropZone?.addEventListener("click", (event) => {
    if (selectionLocked) return;
    if (fileInput && !event.target.closest(".file-item-remove")) fileInput.click();
});

fileInput?.addEventListener("change", () => {
    if (fileInput.files && fileInput.files.length) addFiles(Array.from(fileInput.files));
    if (!accepted.length && !rejected.length) clearAll();
});

form?.addEventListener("submit", (event) => {
    event.preventDefault();
    uploadBatch();
});

let dragDepth = 0;
dropZone?.addEventListener("dragenter", (event) => { event.preventDefault(); dragDepth += 1; setZoneState("dragover"); });
dropZone?.addEventListener("dragover", (event) => { event.preventDefault(); setZoneState("dragover"); });
dropZone?.addEventListener("dragleave", (event) => {
    event.preventDefault();
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) setZoneState("ready");
});
dropZone?.addEventListener("drop", (event) => {
    event.preventDefault();
    dragDepth = 0;
    const files = filesFromDrop(event);
    if (files.length) addFiles(files);
    else setZoneState("ready");
});

// Prevent the browser (especially Firefox) from navigating to a file dropped
// anywhere outside the drop zone on this page.
["dragover", "drop"].forEach((evtName) => {
    window.addEventListener(evtName, (event) => {
        if (dropZone && event.target instanceof Node && !dropZone.contains(event.target)) event.preventDefault();
    });
});
