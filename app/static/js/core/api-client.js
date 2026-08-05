export class ApiError extends Error {
    constructor(message, {status = 0, code = "request_failed", fields = {}, requestId = ""} = {}) {
        super(message);
        this.name = "ApiError";
        this.status = status;
        this.code = code;
        this.fields = fields;
        this.requestId = requestId;
    }
}

function csrfToken() {
    return document.cookie.split(";").map(value => value.trim()).find(value => value.startsWith("csrftoken="))?.split("=")[1] || "";
}

export async function apiFetch(url, options = {}) {
    const controller = options.signal ? null : new AbortController();
    const headers = new Headers(options.headers || {});
    if (options.method && options.method !== "GET" && options.method !== "HEAD") {
        headers.set("X-CSRFToken", decodeURIComponent(csrfToken()));
    }
    headers.set("Accept", "application/json");
    let response;
    try {
        response = await fetch(url, {...options, headers, signal: options.signal || controller?.signal});
    } catch (error) {
        if (error.name === "AbortError") throw error;
        throw new ApiError("Network request failed.", {code: "network_error"});
    }
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("json") ? await response.json() : {error: {message: await response.text()}};
    if (!response.ok) {
        const detail = typeof payload.error === "object" ? payload.error : {message: payload.error || "Request failed."};
        throw new ApiError(detail.message || "Request failed.", {status: response.status, code: detail.code, fields: detail.fields, requestId: detail.request_id || response.headers.get("X-Request-ID") || ""});
    }
    return payload;
}
