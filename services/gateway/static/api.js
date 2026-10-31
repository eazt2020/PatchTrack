/* Thin API client for the gateway BFF.
 *
 * Every mutating request carries the CSRF header copied from the
 * non-HttpOnly cookie. Responses are JSON unless a stream/file is requested.
 */

export class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
    this.status = status;
    this.detail = detail;
  }
}

function csrfToken() {
  const match = document.cookie.match(/(?:^|; )patchtrack_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

/* `redirectOnUnauthorized: false` is for calls where a 401 means "wrong
 * credential supplied", not "your session ended" — the password-change
 * dialog, where the user mistyped their current password and should get an
 * inline message rather than being bounced to the login page. */
export async function api(path, options = {}) {
  const { method = "GET", body, signal, redirectOnUnauthorized = true } = options;
  const headers = { Accept: "application/json" };
  const isForm = body instanceof FormData;
  if (body !== undefined && !isForm) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-PatchTrack-CSRF"] = csrfToken();
  const response = await fetch(`/api/${path.replace(/^\//, "")}`, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : (isForm ? body : JSON.stringify(body)),
    signal,
  });
  if (response.status === 401 && redirectOnUnauthorized) {
    window.location.replace("/login");
    throw new ApiError(401, "Session expired");
  }
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const payload = await response.json();
      detail = payload.detail ?? detail;
    } catch { /* non-JSON error */ }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) return null;
  const type = response.headers.get("content-type") || "";
  if (type.includes("application/json")) return response.json();
  return response;
}

export async function download(path, fallbackName, options = {}) {
  // A POST download carries the rendered board to the reporting service, so it
  // needs the same JSON body and CSRF header as `api`.
  const { method = "GET", body } = options;
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-PatchTrack-CSRF"] = csrfToken();
  const response = await fetch(`/api/${path.replace(/^\//, "")}`, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = `Download failed (${response.status})`;
    try { detail = (await response.json()).detail ?? detail; } catch { /* ignore */ }
    throw new ApiError(response.status, detail);
  }
  const blob = await response.blob();
  const disposition = response.headers.get("content-disposition") || "";
  const match = disposition.match(/filename="?([^";]+)"?/);
  const name = match ? match[1] : (fallbackName || "patchtrack-report.pdf");
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}
