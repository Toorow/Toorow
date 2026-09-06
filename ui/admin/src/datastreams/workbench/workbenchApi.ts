import { apiFetch } from "../../lib/apiFetch";
import { DATASTREAM_TABS, type Tab } from "../../shell/pages/datastreamTabs";
import type { WorkbenchHeader, WorkbenchTabPayload } from "./workbenchTypes";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function nullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function parseHeader(value: unknown, projectId: string, datastreamId: string): WorkbenchHeader {
  if (
    !isRecord(value) ||
    value.schema !== "datastream_workbench.header.v1" ||
    !isRecord(value.identity) ||
    value.identity.project_id !== projectId ||
    value.identity.datastream_id !== datastreamId ||
    !isRecord(value.axes) ||
    !isRecord(value.versions) ||
    !isRecord(value.runs) ||
    !isRecord(value.publications) ||
    !isRecord(value.links) ||
    !isRecord(value.primary_action)
  ) {
    throw new Error("The Workbench header did not match the requested Project and Datastream.");
  }
  const axes = value.axes;
  const versions = value.versions;
  if (
    !["lifecycle", "configuration", "operations", "publication"].every(
      (axis) => typeof axes[axis] === "string",
    ) ||
    !["active_plan", "active_mapping", "proposed_plan", "proposed_mapping"].every(
      (field) => nullableString(versions[field]),
    ) ||
    typeof value.primary_action.label !== "string" ||
    typeof value.primary_action.reason !== "string" ||
    !DATASTREAM_TABS.includes(value.primary_action.tab as Tab)
  ) {
    throw new Error("The Workbench header contract was invalid.");
  }
  return value as unknown as WorkbenchHeader;
}

function parsePayload(
  value: unknown,
  projectId: string,
  datastreamId: string,
  tab: Tab,
): WorkbenchTabPayload {
  if (
    !isRecord(value) ||
    value.schema !== `datastream_workbench.${tab}.v1` ||
    value.project_id !== projectId ||
    value.datastream_id !== datastreamId ||
    value.tab !== tab ||
    !isRecord(value.evidence)
  ) {
    throw new Error(`The ${tab} evidence did not match the requested Project and Datastream.`);
  }
  return value as unknown as WorkbenchTabPayload;
}

async function readJson(response: Response, fallback: string): Promise<unknown> {
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const message = isRecord(body) && typeof body.message === "string" ? body.message : fallback;
    const error = new Error(message);
    if ([401, 403, 404].includes(response.status)) Object.assign(error, { denied: true });
    throw error;
  }
  return body;
}

export async function loadWorkbench(
  projectId: string,
  datastreamId: string,
  tab: Tab,
  signal: AbortSignal,
): Promise<{ header: WorkbenchHeader; payload: WorkbenchTabPayload }> {
  const base = `/api/projects/${encodeURIComponent(projectId)}/datastreams/${encodeURIComponent(datastreamId)}/workbench`;
  const [headerResponse, tabResponse] = await Promise.all([
    apiFetch(base, { cache: "no-store", signal }),
    apiFetch(`${base}/${tab}`, { cache: "no-store", signal }),
  ]);
  const [headerValue, tabValue] = await Promise.all([
    readJson(headerResponse, `Workbench header could not be loaded (HTTP ${headerResponse.status}).`),
    readJson(tabResponse, `${tab} evidence could not be loaded (HTTP ${tabResponse.status}).`),
  ]);
  return {
    header: parseHeader(headerValue, projectId, datastreamId),
    payload: parsePayload(tabValue, projectId, datastreamId, tab),
  };
}

export function isDeniedError(error: unknown): boolean {
  return isRecord(error) && error.denied === true;
}
