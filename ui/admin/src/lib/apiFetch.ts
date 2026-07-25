/**
 * apiFetch — the ONE seam through which ui/admin talks to the connector API.
 *
 * The connector runs in TOOROW_AUTH_MODE=oauth: every /api/ route rejects an
 * unauthenticated request with 401. Before this module existed each call site
 * re-implemented (or, worse, forgot) the `Authorization: Bearer <token>` header
 * — ScopeProvider fetched /api/organizations bare, swallowed the 401 and fell
 * back to an invented organization. Finding F-010.
 *
 * The rule this module enforces: an /api/ call MUST go through `apiFetch` (or
 * one of the JSON helpers built on it). `apiFetch` is the only place that reads
 * the token, so forgetting the header is no longer possible — and a bare
 * `fetch("/api/...")` left anywhere is now the visible anomaly.
 *
 * Failure handling is centralized too: `apiJson` (and the verb helpers) throw a
 * typed `ApiError` carrying the HTTP status and the `{code, message}` envelope
 * the server returns, so a caller can distinguish "unauthenticated" (401) from
 * "empty" (200 with an empty list) instead of collapsing both into a fallback.
 */

/** Error thrown by `apiJson` / the verb helpers when the API does not return 2xx. */
export class ApiError extends Error {
  status: number;
  code: string;
  body: unknown;
  constructor(status: number, code: string, message: string, body?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.body = body;
  }
  /** True when the call failed because the caller is not (or no longer) authenticated. */
  get unauthenticated(): boolean {
    return this.status === 401 || this.status === 403;
  }
}

/** The bearer token AuthGate populates, or the build-time key used by harnesses. */
export function apiToken(): string {
  if (typeof window === "undefined") return "";
  const injected = (window as Window & { __TOOROW_API_KEY__?: string }).__TOOROW_API_KEY__;
  return injected ?? localStorage.getItem("api_token") ?? "";
}

/** Authorization header (plus any extras), omitted entirely when no token is held. */
export function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  const token = apiToken();
  return token ? { Authorization: `Bearer ${token}`, ...extra } : { ...extra };
}

/**
 * `fetch` for /api/ routes: identical contract (returns the raw Response, does
 * not throw on 4xx/5xx) with the Authorization header always attached. Use it
 * when the call site already handles `resp.ok` itself; prefer `apiJson` for new
 * code so error handling stays uniform.
 */
export function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  // Caller headers are carried over verbatim (a plain record keeps its exact
  // casing, which several call sites and their tests rely on); the bearer is
  // added only when the caller did not set an Authorization of its own.
  const provided = init.headers;
  const headers: Record<string, string> =
    provided instanceof Headers || Array.isArray(provided)
      ? Object.fromEntries(new Headers(provided).entries())
      : { ...((provided as Record<string, string> | undefined) ?? {}) };
  const token = apiToken();
  const hasAuth = Object.keys(headers).some((k) => k.toLowerCase() === "authorization");
  if (token && !hasAuth) headers.Authorization = `Bearer ${token}`;
  return fetch(path, { ...init, headers });
}

/** apiFetch + `{code, message}` envelope parsing; throws `ApiError` unless 2xx. */
export async function apiJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  let resp: Response;
  try {
    resp = await apiFetch(path, init);
  } catch (err) {
    // Network / DNS / offline: no status to report, but still a hard failure —
    // never a reason to substitute made-up data.
    throw new ApiError(0, "unreachable", err instanceof Error ? err.message : "Network error");
  }
  if (!resp.ok) {
    let body: unknown;
    let code = "unavailable";
    let message = `HTTP ${resp.status}`;
    try {
      body = await resp.json();
      const env = body as { code?: string; message?: string; detail?: string };
      code = env.code ?? code;
      message = env.message ?? env.detail ?? message;
    } catch {
      /* non-JSON error body — the status alone is the signal. */
    }
    throw new ApiError(resp.status, code, message, body);
  }
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

function jsonInit(method: string, body?: unknown): RequestInit {
  const init: RequestInit = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) init.body = JSON.stringify(body);
  return init;
}

export function apiGet<T>(path: string, init: RequestInit = {}): Promise<T> {
  return apiJson<T>(path, { ...init, method: "GET", cache: init.cache ?? "no-store" });
}

export function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return apiJson<T>(path, jsonInit("POST", body));
}

export function apiPatch<T>(path: string, body?: unknown): Promise<T> {
  return apiJson<T>(path, jsonInit("PATCH", body));
}

export function apiPut<T>(path: string, body?: unknown): Promise<T> {
  return apiJson<T>(path, jsonInit("PUT", body));
}

export function apiDelete<T>(path: string): Promise<T> {
  return apiJson<T>(path, { method: "DELETE" });
}
