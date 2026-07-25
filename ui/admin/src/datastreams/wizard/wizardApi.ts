/**
 * Story 12.13 — the wizard's thin API helper.
 *
 * Reuses the EXACT fetch + Bearer-header convention proven across the Epic-36
 * datastream cards (PremierRapportCard / RapportPretCard / ConfigurationHoteCard):
 * a bearer token, `cache: "no-store"`, an `Idempotency-Key` on every mutation,
 * and stable French error messages. Every mutation goes through the governed
 * server API only (12.14 AC5) — nothing writes client-side.
 *
 * Where a create route is only partially wired (Phase B), the caller degrades
 * gracefully (disabled control + honest copy) rather than fabricating a call —
 * this module never invents a response.
 */
import type {
  ConnectionSummary,
  ImportPreview,
  SourceCapabilities,
} from "./wizardTypes";

export interface WizardApiConfig {
  apiBase: string;
  apiToken: string;
  projectId: string;
}

function authHeaders(token: string): HeadersInit {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function jsonHeaders(token: string, idempotent: boolean): HeadersInit {
  return {
    ...authHeaders(token),
    "Content-Type": "application/json",
    ...(idempotent ? { "Idempotency-Key": crypto.randomUUID() } : {}),
  };
}

/** A stable, French, HTTP-status-aware error. */
export function httpError(status: number, fallback: string): Error {
  if (status === 401 || status === 403) {
    return new Error("Accès refusé : vos droits ne permettent pas cette action.");
  }
  if (status === 404) {
    return new Error("Ressource introuvable ou hors de votre périmètre.");
  }
  if (status === 409) {
    return new Error("Conflit : cette opération a déjà été enregistrée ou est en cours.");
  }
  if (status === 422) {
    return new Error("Validation refusée : corrigez la configuration avant de continuer.");
  }
  return new Error(`${fallback} (HTTP ${status}).`);
}

/** GET /api/connections — project-owned connections for the Source stage. */
export async function listConnections(
  cfg: WizardApiConfig,
): Promise<ConnectionSummary[]> {
  const qs = `project_id=${encodeURIComponent(cfg.projectId)}`;
  const response = await fetch(`${cfg.apiBase}/api/connections?${qs}`, {
    method: "GET",
    headers: authHeaders(cfg.apiToken),
    cache: "no-store",
  });
  if (!response.ok) throw httpError(response.status, "Connexions indisponibles");
  const data = (await response.json()) as { connections?: ConnectionSummary[] } | ConnectionSummary[];
  return Array.isArray(data) ? data : data.connections ?? [];
}

/** GET /api/source-capabilities — the governed connector capability catalog (12.1). */
export async function getSourceCapabilities(
  cfg: WizardApiConfig,
  connectionRefId: string,
): Promise<SourceCapabilities> {
  const qs =
    `project_id=${encodeURIComponent(cfg.projectId)}` +
    `&connection_ref_id=${encodeURIComponent(connectionRefId)}`;
  const response = await fetch(`${cfg.apiBase}/api/source-capabilities?${qs}`, {
    method: "GET",
    headers: authHeaders(cfg.apiToken),
    cache: "no-store",
  });
  if (!response.ok) throw httpError(response.status, "Capacités de la source indisponibles");
  return (await response.json()) as SourceCapabilities;
}

/**
 * POST /api/datastreams/{id}/imports/preview — bounded CSV/Excel preview, NO
 * publish (12.9). The file is sent base64 in the JSON body (the wired route
 * accepts `file_base64`).
 */
export async function previewImport(
  cfg: WizardApiConfig,
  datastreamId: string,
  fileBase64: string,
  filename: string,
): Promise<ImportPreview> {
  const response = await fetch(
    `${cfg.apiBase}/api/datastreams/${encodeURIComponent(datastreamId)}/imports/preview`,
    {
      method: "POST",
      headers: jsonHeaders(cfg.apiToken, false),
      body: JSON.stringify({
        project_id: cfg.projectId,
        file_base64: fileBase64,
        filename,
      }),
    },
  );
  if (!response.ok) {
    // 422 carries a stable parse/contract code the caller surfaces verbatim.
    let detail = "";
    try {
      const body = (await response.json()) as { code?: string; message?: string };
      detail = body.message || body.code || "";
    } catch {
      /* ignore */
    }
    if (response.status === 422 && detail) throw new Error(`Fichier refusé : ${detail}`);
    throw httpError(response.status, "Aperçu du fichier indisponible");
  }
  return (await response.json()) as ImportPreview;
}

/**
 * POST /api/datastreams/{datastream_id}/managed-feed/configure — upsert the
 * recurring Google Sheets sync config (12.10). The server (admin_api.py
 * `_configure_managed_feed_sync`) REQUIRES a non-empty `connection_id`,
 * `spreadsheet_id` and `sheet_range` (422 otherwise). The Google connection id
 * must therefore be collected on the Sheets sub-flow BEFORE this call; the shell
 * gates the call when it is missing rather than firing a doomed request.
 */
export async function configureManagedFeed(
  cfg: WizardApiConfig,
  datastreamId: string,
  body: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const response = await fetch(
    `${cfg.apiBase}/api/datastreams/${encodeURIComponent(datastreamId)}/managed-feed/configure`,
    {
      method: "POST",
      headers: jsonHeaders(cfg.apiToken, false),
      body: JSON.stringify({ project_id: cfg.projectId, ...body }),
    },
  );
  if (!response.ok) throw httpError(response.status, "Configuration du flux managé impossible");
  return (await response.json()) as Record<string, unknown>;
}

/**
 * POST /api/datastreams — create (or Save draft) a versioned datastream intent.
 * A versioned create requires an Idempotency-Key and an `intent` object; the
 * server derives writer ownership from source kind (12.2 AC1).
 */
export async function saveDatastreamDraft(
  cfg: WizardApiConfig,
  body: { name: string; intent: Record<string, unknown>; reason?: string },
): Promise<{ id: string; plan_version?: unknown }> {
  const response = await fetch(`${cfg.apiBase}/api/datastreams`, {
    method: "POST",
    headers: jsonHeaders(cfg.apiToken, true),
    body: JSON.stringify({ project_id: cfg.projectId, ...body }),
  });
  if (!response.ok) throw httpError(response.status, "Enregistrement du brouillon impossible");
  return (await response.json()) as { id: string; plan_version?: unknown };
}

/**
 * PATCH /api/datastreams/{id} — append a NEW immutable version to an EXISTING
 * draft datastream (versioned revise path). Same intent contract + Idempotency-
 * Key as create; the server reads `source.kind` and mints a fresh plan version.
 * Used instead of a second create so an existing draft is versioned in place
 * rather than orphaned by a duplicate row.
 */
export async function reviseDatastreamDraft(
  cfg: WizardApiConfig,
  datastreamId: string,
  body: { name: string; intent: Record<string, unknown>; reason?: string },
): Promise<{ id: string; plan_version?: unknown }> {
  const response = await fetch(
    `${cfg.apiBase}/api/datastreams/${encodeURIComponent(datastreamId)}`,
    {
      method: "PATCH",
      headers: jsonHeaders(cfg.apiToken, true),
      body: JSON.stringify({ project_id: cfg.projectId, ...body }),
    },
  );
  if (!response.ok) throw httpError(response.status, "Mise à jour du brouillon impossible");
  return (await response.json()) as { id: string; plan_version?: unknown };
}

/** Read a File as a base64 string (no data: prefix). */
export function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("Lecture du fichier impossible."));
    reader.onload = () => {
      const result = String(reader.result ?? "");
      const comma = result.indexOf(",");
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.readAsDataURL(file);
  });
}
