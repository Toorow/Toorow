/**
 * opsApi — the single governed-API access seam for the Datastreams v2
 * operational surface (Story 12.14).
 *
 * ALL admin mutations for 12.14 go through the governed server API only
 * (AC "any admin mutation ... uses the governed server API only"). This module
 * is the ONE place that talks to the wire, so the RBAC / cross-project denial
 * story is enforced server-side and surfaced uniformly here.
 *
 * Convention (matches DatastreamsList / DatastreamDetail / RapportPretCard):
 *   - fetch with `Authorization: Bearer <token>` when a token is present.
 *   - project_id in the query for GET, in the JSON body for POST.
 *   - On !ok, parse the `{code, message, ...}` envelope and throw an OpsApiError
 *     carrying the stable code + any route-specific evidence (deadline, issues,
 *     blocking_execution_id, ...).
 */
import type { ApiError } from "./opsTypes";

export class OpsApiError extends Error {
  code: string;
  status: number;
  detail: ApiError;
  constructor(status: number, detail: ApiError) {
    super(detail.message || detail.code || `Erreur ${status}`);
    this.name = "OpsApiError";
    this.code = detail.code || "unknown";
    this.status = status;
    this.detail = detail;
  }
}

function authHeaders(apiToken: string): HeadersInit {
  return apiToken ? { Authorization: `Bearer ${apiToken}` } : {};
}

async function parseError(resp: Response): Promise<OpsApiError> {
  let body: ApiError;
  try {
    body = (await resp.json()) as ApiError;
  } catch {
    body = { code: "unavailable", message: `Erreur ${resp.status}` };
  }
  return new OpsApiError(resp.status, body || { code: "unavailable" });
}

export interface OpsApiCtx {
  apiBase: string;
  apiToken: string;
  projectId: string;
}

/** GET with project_id + arbitrary extra query params. */
export async function opsGet<T>(
  ctx: OpsApiCtx,
  path: string,
  extraQuery: Record<string, string | undefined> = {}
): Promise<T> {
  const params = new URLSearchParams({ project_id: ctx.projectId });
  for (const [k, v] of Object.entries(extraQuery)) {
    if (v != null && v !== "") params.set(k, v);
  }
  const resp = await fetch(`${ctx.apiBase}${path}?${params.toString()}`, {
    method: "GET",
    headers: authHeaders(ctx.apiToken),
    cache: "no-store",
  });
  if (!resp.ok) throw await parseError(resp);
  return (await resp.json()) as T;
}

/** POST a JSON body (project_id injected). Optional Idempotency-Key header. */
export async function opsPost<T>(
  ctx: OpsApiCtx,
  path: string,
  body: Record<string, unknown> = {},
  opts: { idempotencyKey?: string } = {}
): Promise<T> {
  const headers: HeadersInit = {
    "Content-Type": "application/json",
    ...authHeaders(ctx.apiToken),
  };
  if (opts.idempotencyKey) {
    (headers as Record<string, string>)["Idempotency-Key"] = opts.idempotencyKey;
  }
  const resp = await fetch(`${ctx.apiBase}${path}`, {
    method: "POST",
    headers,
    body: JSON.stringify({ project_id: ctx.projectId, ...body }),
  });
  if (!resp.ok) throw await parseError(resp);
  return (await resp.json()) as T;
}
