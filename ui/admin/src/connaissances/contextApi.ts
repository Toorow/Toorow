/**
 * contextApi — fetch helpers for the 11.1 context REST endpoints.
 *
 * All writes go exclusively through these helpers (AD-15).
 * Auth header follows the same pattern as QualitePage._authHeader().
 */

import type {
  ApiError,
  CreateGraphEdgePayload,
  GraphEdge,
  GraphEdgesResponse,
  Procedure,
  ProceduresResponse,
  Topic,
  TopicsResponse,
} from "./types";

// ---------------------------------------------------------------------------
// Auth header (mirrors QualitePage pattern)
// ---------------------------------------------------------------------------

function authHeader(): Record<string, string> {
  const token =
    typeof window !== "undefined"
      ? (window as Window & { __TOOROW_API_KEY__?: string }).__TOOROW_API_KEY__ ?? ""
      : "";
  return token ? { Authorization: `Bearer ${token}` } : {};
}

// ---------------------------------------------------------------------------
// Topics
// ---------------------------------------------------------------------------

export async function listTopics(projectId: string | null): Promise<Topic[]> {
  // NOTE (AD-5): GET /api/context/topics?project_id=X returns BOTH project rows
  // (project_id = X) AND platform rows (project_id IS NULL). The backend SQL uses
  // WHERE (project_id IS NULL OR project_id = %s) so callers always see the full
  // visible scope for the given project, not just project-owned rows.
  const params = new URLSearchParams();
  if (projectId) params.set("project_id", projectId);
  const res = await fetch(`/api/context/topics?${params.toString()}`, {
    headers: authHeader(),
  });
  if (!res.ok) {
    const err: ApiError = await res.json().catch(() => ({ code: "unknown", message: `HTTP ${res.status}` }));
    throw err;
  }
  const data: TopicsResponse = await res.json();
  return data.topics ?? [];
}

export async function getTopic(id: string): Promise<Topic> {
  const res = await fetch(`/api/context/topics/${encodeURIComponent(id)}`, {
    headers: authHeader(),
  });
  if (!res.ok) {
    const err: ApiError = await res.json().catch(() => ({ code: "unknown", message: `HTTP ${res.status}` }));
    throw err;
  }
  return res.json() as Promise<Topic>;
}

export async function createTopic(payload: {
  project_id: string | null;
  title: string;
  body_md: string;
}): Promise<Topic> {
  const res = await fetch("/api/context/topics", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeader() },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const err: ApiError = await res.json().catch(() => ({ code: "unknown", message: `HTTP ${res.status}` }));
    throw err;
  }
  return res.json() as Promise<Topic>;
}

export async function updateTopic(
  id: string,
  patch: Partial<Pick<Topic, "title" | "body_md">>
): Promise<Topic> {
  const res = await fetch(`/api/context/topics/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", ...authHeader() },
    body: JSON.stringify(patch),
  });
  if (!res.ok) {
    const err: ApiError = await res.json().catch(() => ({ code: "unknown", message: `HTTP ${res.status}` }));
    throw err;
  }
  return res.json() as Promise<Topic>;
}

export async function archiveTopic(id: string): Promise<Topic> {
  const res = await fetch(`/api/context/topics/${encodeURIComponent(id)}/archive`, {
    method: "POST",
    headers: authHeader(),
  });
  if (!res.ok) {
    const err: ApiError = await res.json().catch(() => ({ code: "unknown", message: `HTTP ${res.status}` }));
    throw err;
  }
  return res.json() as Promise<Topic>;
}

// ---------------------------------------------------------------------------
// Procedures
// ---------------------------------------------------------------------------

export async function listProcedures(projectId: string | null): Promise<Procedure[]> {
  const params = new URLSearchParams();
  if (projectId) params.set("project_id", projectId);
  const res = await fetch(`/api/context/procedures?${params.toString()}`, {
    headers: authHeader(),
  });
  if (!res.ok) {
    const err: ApiError = await res.json().catch(() => ({ code: "unknown", message: `HTTP ${res.status}` }));
    throw err;
  }
  const data: ProceduresResponse = await res.json();
  return data.procedures ?? [];
}

export async function getProcedure(id: string): Promise<Procedure> {
  const res = await fetch(`/api/context/procedures/${encodeURIComponent(id)}`, {
    headers: authHeader(),
  });
  if (!res.ok) {
    const err: ApiError = await res.json().catch(() => ({ code: "unknown", message: `HTTP ${res.status}` }));
    throw err;
  }
  return res.json() as Promise<Procedure>;
}

export async function createProcedure(payload: {
  project_id: string | null;
  frontmatter_yaml: string;
  body_md: string;
}): Promise<Procedure> {
  const res = await fetch("/api/context/procedures", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeader() },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const err: ApiError = await res.json().catch(() => ({ code: "unknown", message: `HTTP ${res.status}` }));
    throw err;
  }
  return res.json() as Promise<Procedure>;
}

export async function updateProcedure(
  id: string,
  patch: Partial<Pick<Procedure, "frontmatter_yaml" | "body_md">>
): Promise<Procedure> {
  const res = await fetch(`/api/context/procedures/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", ...authHeader() },
    body: JSON.stringify(patch),
  });
  if (!res.ok) {
    const err: ApiError = await res.json().catch(() => ({ code: "unknown", message: `HTTP ${res.status}` }));
    throw err;
  }
  return res.json() as Promise<Procedure>;
}

export async function archiveProcedure(id: string): Promise<Procedure> {
  const res = await fetch(`/api/context/procedures/${encodeURIComponent(id)}/archive`, {
    method: "POST",
    headers: authHeader(),
  });
  if (!res.ok) {
    const err: ApiError = await res.json().catch(() => ({ code: "unknown", message: `HTTP ${res.status}` }));
    throw err;
  }
  return res.json() as Promise<Procedure>;
}

// ---------------------------------------------------------------------------
// Graph Edges (Story 11.4 — AD-15: all writes via API)
// ---------------------------------------------------------------------------

/** List graph edges visible in the given project scope (platform + project). */
export async function listGraphEdges(projectId: string | null): Promise<GraphEdge[]> {
  const params = new URLSearchParams();
  if (projectId) params.set("project_id", projectId);
  const res = await fetch(`/api/context/graph/edges?${params.toString()}`, {
    headers: authHeader(),
  });
  if (!res.ok) {
    const err: ApiError = await res.json().catch(() => ({ code: "unknown", message: `HTTP ${res.status}` }));
    throw err;
  }
  const data: GraphEdgesResponse = await res.json();
  return data.edges ?? [];
}

/** Create a new graph edge. */
export async function createGraphEdge(payload: CreateGraphEdgePayload): Promise<GraphEdge> {
  const res = await fetch("/api/context/graph/edges", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeader() },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const err: ApiError = await res.json().catch(() => ({ code: "unknown", message: `HTTP ${res.status}` }));
    throw err;
  }
  return res.json() as Promise<GraphEdge>;
}

/** Delete a graph edge by ID. */
export async function deleteGraphEdge(id: string): Promise<void> {
  const res = await fetch(`/api/context/graph/edges/${encodeURIComponent(id)}`, {
    method: "DELETE",
    headers: authHeader(),
  });
  if (!res.ok && res.status !== 204) {
    const err: ApiError = await res.json().catch(() => ({ code: "unknown", message: `HTTP ${res.status}` }));
    throw err;
  }
}
