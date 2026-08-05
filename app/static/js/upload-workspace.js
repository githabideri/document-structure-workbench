import {publishDiagnostics} from "./core/diagnostics.js";

const dropZone = document.getElementById("file-drop");
const fileInput = document.getElementById("file-input");
const fileInfo = document.getElementById("file-info");
const showFile = () => {
    if (fileInput?.files.length) {
        const file = fileInput.files[0];
        fileInfo.textContent = `${file.name} · ${(file.size / 1024 / 1024).toFixed(1)} MB`;
    }
    publishDiagnostics();
};
fileInput?.addEventListener("change", showFile);
dropZone?.addEventListener("dragover", event => { event.preventDefault(); dropZone.classList.add("dragover"); });
dropZone?.addEventListener("dragleave", () => dropZone.classList.remove("dragover"));
dropZone?.addEventListener("drop", event => {
    event.preventDefault();
    dropZone.classList.remove("dragover");
    if (event.dataTransfer?.files.length && fileInput) {
        const transfer = new DataTransfer();
        [...event.dataTransfer.files].forEach(file => transfer.items.add(file));
        fileInput.files = transfer.files;
        showFile();
    }
});
