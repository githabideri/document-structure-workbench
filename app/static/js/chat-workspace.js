import {publishDiagnostics} from "./core/diagnostics.js";

const composer = document.querySelector(".chat-question-form, .chat-start-form");
composer?.querySelector("textarea")?.addEventListener("keydown", event => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && !event.shiftKey) {
        event.preventDefault();
        composer.requestSubmit();
    }
});

document.querySelectorAll(".chat-citation, .chat-evidence-list a").forEach(link => {
    link.addEventListener("click", () => {
        try { sessionStorage.setItem("dsw-originating-citation", link.getAttribute("href")); } catch (_) { /* optional */ }
    });
});

document.addEventListener("keydown", event => {
    if (event.key !== "Escape" || ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
    const evidence = document.querySelector("[data-ui-id='evidence-reader']");
    if (evidence) {
        const origin = sessionStorage.getItem("dsw-originating-citation");
        document.querySelector(origin)?.focus();
    }
});
publishDiagnostics();
