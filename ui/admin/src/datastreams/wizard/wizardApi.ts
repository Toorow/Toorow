/** Thin, source-agnostic API client for the canonical Datastream wizard. */
import type {
  ConnectionSummary,
  ImportPreview,
  SourceCapabilities,
} from "./wizardTypes";
import { apiFetch } from "../../lib/apiFetch";

export interface WizardApiConfig {
  apiBase: string;
  projectId: string;
}

export interface ServerValidationIssue {
  code: string;
  path?: string;
  message: string;
  repair?: Record<string, unknown>;
  details?: Record<string, unknown>;
}

export interface IntentValidationResponse {
  normalized_intent: Record<string, unknown>;
  content_hash: string;
  executable: boolean;
  issues: ServerValidationIssue[];
  capability_contract_version: string | null;
  capability_fingerprint: string | null;
}

export interface DatastreamPlanVersion {
  id: string;
  executable: boolean;
  content_hash?: string;
  capability_contract_version?: string | null;
  capability_fingerprint?: string | null;
  normalized_payload?: Record<string, unknown>;
  validation_issues?: ServerValidationIssue[];
  idempotent_replay?: boolean;
}

export interface DatastreamMutationResult {
  id: string;
  plan_version?: DatastreamPlanVersion;
  enabled?: boolean;
}

export class WizardApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly issues: ServerValidationIssue[];

  constructor(
    status: number,
    code: string,
    message: string,
    issues: ServerValidationIssue[] = [],
  ) {
    super(message);
    this.name = "WizardApiError";
    this.status = status;
    this.code = code;
    this.issues = issues;
  }
}

function jsonHeaders(idempotencyKey?: string): HeadersInit {
  return {
    "Content-Type": "application/json",
    ...(idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}),
  };
}

function fallbackMessage(status: number, fallback: string): string {
  if (status === 401 || status === 403) {
    return "Access denied: your permissions do not allow this action.";
  }
  if (status === 404) return "Resource not found or outside the active project.";
  if (status === 409) return "This request conflicts with an existing operation.";
  if (status === 422) return "Validation failed. Review the configuration and try again.";
  return `${fallback} (HTTP ${status}).`;
}

async function responseError(response: Response, fallback: string): Promise<WizardApiError> {
  let code = "unavailable";
  let message = fallbackMessage(response.status, fallback);
  let issues: ServerValidationIssue[] = [];
  try {
    const body = (await response.json()) as {
      code?: string;
      message?: string;
      issues?: ServerValidationIssue[];
      errors?: ServerValidationIssue[];
    };
    code = body.code ?? code;
    message = body.message ?? message;
    issues = body.issues ?? body.errors ?? [];
    if (issues.length > 0) {
      const details = issues
        .map((issue) => `${issue.message}${issue.path ? ` (${issue.path})` : ""}`)
        .join(" ");
      message = body.message ? `${body.message} ${details}` : details;
    }
  } catch {
    // The HTTP status and stable fallback remain authoritative.
  }
  return new WizardApiError(response.status, code, message, issues);
}

/** Project/org-scoped provider accounts usable by the active operator. */
export async function listConnections(
  cfg: WizardApiConfig,
): Promise<ConnectionSummary[]> {
  const qs = `project_id=${encodeURIComponent(cfg.projectId)}`;
  const response = await apiFetch(`${cfg.apiBase}/api/connections?${qs}`, {
    method: "GET",
    cache: "no-store",
  });
  if (!response.ok) throw await responseError(response, "Provider accounts are unavailable");
  const data = (await response.json()) as
    | { connections?: ConnectionSummary[] }
    | ConnectionSummary[];
  return Array.isArray(data) ? data : data.connections ?? [];
}

/** Governed, project-scoped connector capability catalog. */
export async function getSourceCapabilities(
  cfg: WizardApiConfig,
  connectionRefId: string,
): Promise<SourceCapabilities> {
  const qs =
    `project_id=${encodeURIComponent(cfg.projectId)}` +
    `&connection_ref_id=${encodeURIComponent(connectionRefId)}`;
  const response = await apiFetch(`${cfg.apiBase}/api/source-capabilities?${qs}`, {
    method: "GET",
    cache: "no-store",
  });
  if (!response.ok) {
    throw await responseError(response, "Source capabilities are unavailable");
  }
  return (await response.json()) as SourceCapabilities;
}

/** Bounded CSV/Excel preview; never publishes. */
export async function previewImport(
  cfg: WizardApiConfig,
  datastreamId: string,
  fileBase64: string,
  filename: string,
): Promise<ImportPreview> {
  const response = await apiFetch(
    `${cfg.apiBase}/api/datastreams/${encodeURIComponent(datastreamId)}/imports/preview`,
    {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({
        project_id: cfg.projectId,
        file_base64: fileBase64,
        filename,
      }),
    },
  );
  if (!response.ok) throw await responseError(response, "File preview is unavailable");
  return (await response.json()) as ImportPreview;
}

/** Persist recurring managed-feed configuration after a draft exists. */
export async function configureManagedFeed(
  cfg: WizardApiConfig,
  datastreamId: string,
  body: Record<string, unknown>,
  idempotencyKey: string,
): Promise<Record<string, unknown>> {
  const response = await apiFetch(
    `${cfg.apiBase}/api/datastreams/${encodeURIComponent(datastreamId)}/managed-feed/configure`,
    {
      method: "POST",
      headers: jsonHeaders(idempotencyKey),
      body: JSON.stringify({ project_id: cfg.projectId, ...body }),
    },
  );
  if (!response.ok) {
    throw await responseError(response, "Managed-feed configuration could not be saved");
  }
  return (await response.json()) as Record<string, unknown>;
}

export async function saveDatastreamDraft(
  cfg: WizardApiConfig,
  body: { name: string; intent: Record<string, unknown>; reason?: string },
  idempotencyKey: string,
): Promise<DatastreamMutationResult> {
  const response = await apiFetch(`${cfg.apiBase}/api/datastreams`, {
    method: "POST",
    headers: jsonHeaders(idempotencyKey),
    body: JSON.stringify({ project_id: cfg.projectId, ...body }),
  });
  if (!response.ok) throw await responseError(response, "Draft could not be saved");
  return (await response.json()) as DatastreamMutationResult;
}

export async function reviseDatastreamDraft(
  cfg: WizardApiConfig,
  datastreamId: string,
  body: { name: string; intent: Record<string, unknown>; reason?: string },
  idempotencyKey: string,
): Promise<DatastreamMutationResult> {
  const response = await apiFetch(
    `${cfg.apiBase}/api/datastreams/${encodeURIComponent(datastreamId)}`,
    {
      method: "PATCH",
      headers: jsonHeaders(idempotencyKey),
      body: JSON.stringify({ project_id: cfg.projectId, ...body }),
    },
  );
  if (!response.ok) throw await responseError(response, "Draft could not be updated");
  return (await response.json()) as DatastreamMutationResult;
}

/** Server-side validation against current scoped capabilities and schedule policy. */
export async function validateDatastreamDraft(
  cfg: WizardApiConfig,
  datastreamId: string,
  intent: Record<string, unknown>,
): Promise<IntentValidationResponse> {
  const response = await apiFetch(
    `${cfg.apiBase}/api/datastreams/${encodeURIComponent(datastreamId)}/validate`,
    {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({ project_id: cfg.projectId, intent }),
    },
  );
  if (response.ok || response.status === 422) {
    const body = (await response.json()) as IntentValidationResponse & { code?: string };
    if (typeof body.executable === "boolean" && Array.isArray(body.issues)) return body;
    throw new WizardApiError(
      response.status,
      body.code ?? "invalid_validation_response",
      "The server returned incomplete validation evidence.",
      body.issues ?? [],
    );
  }
  throw await responseError(response, "Plan validation is unavailable");
}

/** Enable the exact current executable plan version. */
export async function activateDatastream(
  cfg: WizardApiConfig,
  datastreamId: string,
  planVersionId: string,
  idempotencyKey: string,
): Promise<DatastreamMutationResult> {
  const response = await apiFetch(
    `${cfg.apiBase}/api/datastreams/${encodeURIComponent(datastreamId)}`,
    {
      method: "PATCH",
      headers: jsonHeaders(idempotencyKey),
      body: JSON.stringify({
        project_id: cfg.projectId,
        enabled: true,
        plan_version_id: planVersionId,
      }),
    },
  );
  if (!response.ok) throw await responseError(response, "Datastream activation failed");
  return (await response.json()) as DatastreamMutationResult;
}

/** Read a File as a base64 string (without the data: prefix). */
export function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("The selected file could not be read."));
    reader.onload = () => {
      const result = String(reader.result ?? "");
      const comma = result.indexOf(",");
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.readAsDataURL(file);
  });
}