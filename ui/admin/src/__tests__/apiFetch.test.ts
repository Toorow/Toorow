/**
 * Vitest tests for src/lib/apiFetch.ts — the single API access seam (F-010).
 *
 * Coverage:
 *   - the Authorization header is attached from localStorage.api_token
 *   - __TOOROW_API_KEY__ (harness injection) takes precedence
 *   - no token → no Authorization header (the server answers 401, which callers
 *     must surface rather than mask)
 *   - caller-supplied headers survive; an explicit Authorization is not clobbered
 *   - apiJson throws a typed ApiError carrying status + {code, message}
 *   - a network failure becomes ApiError(0, "unreachable"), never a silent value
 *   - the verb helpers send the method and the JSON body
 */
import { ApiError, apiDelete, apiFetch, apiGet, apiJson, apiPost, authHeaders } from "../lib/apiFetch";

function stub(status: number, body: unknown = {}) {
  const mock = vi.fn(() =>
    Promise.resolve({ ok: status >= 200 && status < 300, status, json: async () => body })
  );
  vi.stubGlobal("fetch", mock);
  return mock;
}

function headersOf(mock: ReturnType<typeof stub>): Headers {
  const init = (mock.mock.calls[0] as unknown as [string, RequestInit])[1];
  return new Headers(init.headers);
}

beforeEach(() => {
  localStorage.clear();
  delete (window as Window & { __TOOROW_API_KEY__?: string }).__TOOROW_API_KEY__;
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("apiFetch headers", () => {
  it("attaches the bearer token from localStorage", async () => {
    localStorage.setItem("api_token", "tok-abc");
    const mock = stub(200);
    await apiFetch("/api/organizations");
    expect(headersOf(mock).get("Authorization")).toBe("Bearer tok-abc");
  });

  it("prefers the injected harness key", async () => {
    localStorage.setItem("api_token", "tok-abc");
    (window as Window & { __TOOROW_API_KEY__?: string }).__TOOROW_API_KEY__ = "tok-harness";
    const mock = stub(200);
    await apiFetch("/api/organizations");
    expect(headersOf(mock).get("Authorization")).toBe("Bearer tok-harness");
  });

  it("sends no Authorization header when no token is held", async () => {
    const mock = stub(200);
    await apiFetch("/api/organizations");
    expect(headersOf(mock).has("Authorization")).toBe(false);
  });

  it("keeps caller headers and does not clobber an explicit Authorization", async () => {
    localStorage.setItem("api_token", "tok-abc");
    const mock = stub(200);
    await apiFetch("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer explicit" },
    });
    const h = headersOf(mock);
    expect(h.get("Content-Type")).toBe("application/json");
    expect(h.get("Authorization")).toBe("Bearer explicit");
  });

  it("authHeaders() omits the header entirely without a token", () => {
    expect(authHeaders()).toEqual({});
    localStorage.setItem("api_token", "t");
    expect(authHeaders({ "X-Extra": "1" })).toEqual({
      Authorization: "Bearer t",
      "X-Extra": "1",
    });
  });
});

describe("apiJson error handling", () => {
  it("returns the parsed body on 200", async () => {
    stub(200, { organizations: [{ id: "o1" }] });
    await expect(apiJson<{ organizations: unknown[] }>("/api/organizations")).resolves.toEqual({
      organizations: [{ id: "o1" }],
    });
  });

  it("throws ApiError with the status and envelope on 401", async () => {
    stub(401, { code: "unauthenticated", message: "Missing bearer token" });
    const err = await apiGet("/api/organizations").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    const api = err as ApiError;
    expect(api.status).toBe(401);
    expect(api.code).toBe("unauthenticated");
    expect(api.message).toBe("Missing bearer token");
    expect(api.unauthenticated).toBe(true);
  });

  it("falls back to the status when the error body is not JSON", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve({
          ok: false,
          status: 502,
          json: async () => {
            throw new Error("not json");
          },
        })
      )
    );
    const err = (await apiGet("/api/overview").catch((e: unknown) => e)) as ApiError;
    expect(err.status).toBe(502);
    expect(err.message).toBe("HTTP 502");
    expect(err.unauthenticated).toBe(false);
  });

  it("turns a network failure into ApiError(0, unreachable)", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))));
    const err = (await apiGet("/api/overview").catch((e: unknown) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(0);
    expect(err.code).toBe("unreachable");
  });
});

describe("verb helpers", () => {
  it("apiPost sends the method, the JSON content type and the body", async () => {
    localStorage.setItem("api_token", "tok-abc");
    const mock = stub(200, { id: "org_new" });
    await apiPost("/api/organizations", { name: "Acme" });
    const [url, init] = mock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/organizations");
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
    expect(JSON.parse(init.body as string)).toEqual({ name: "Acme" });
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer tok-abc");
  });

  it("apiDelete resolves on 204 without parsing a body", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve({
          ok: true,
          status: 204,
          json: async () => {
            throw new Error("no body");
          },
        })
      )
    );
    await expect(apiDelete("/api/projects/p1")).resolves.toBeUndefined();
  });
});
