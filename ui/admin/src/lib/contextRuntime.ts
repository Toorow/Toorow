export const CONTEXT_REQUEST_TIMEOUT_MS = 15_000;

export interface ContextCapabilities {
  can_write: boolean;
  version_history: boolean;
  usage: boolean;
}

export interface ContextTopic {
  id: string;
  project_id: string | null;
  title: string;
  body_md: string;
  status: "active" | "archived";
  owner: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
  version_number: number;
  capabilities: ContextCapabilities | null;
}

/**
 * Ce qu'une Skill DESIGNE du modele de donnees, resolu PAR LE SERVEUR contre
 * `app.target_fields` -- ses `{{champ}}` inline et ses `mdm_tags`.
 *
 * Les deux cotes voyagent. Ne rendre que `resolved` laisserait croire qu'une
 * Skill ne cite que des champs existants -- et une reference qui ne resout pas
 * est justement le retour sur la Skill.
 */
export interface MdmReferences {
  inline: { resolved: Array<Record<string, unknown>>; unresolved: string[] };
  tags: { resolved: Array<Record<string, unknown>>; unresolved: string[] };
}

export interface ContextProcedure {
  id: string;
  project_id: string | null;
  name: string;
  description: string;
  frontmatter_yaml: string;
  body_md: string;
  status: "active" | "archived";
  owner: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
  version_number: number;
  capabilities: ContextCapabilities | null;
  /**
   * AI-157 : la route de DETAIL (`GET /api/context/procedures/{id}`) la porte ;
   * la route de LISTE non -- resoudre le catalogue pour chaque ligne d'une liste
   * couterait une lecture de `target_fields` par Skill pour un panneau qu'on
   * n'ouvre pas. `null` veut dire « pas encore lu », jamais « rien a citer ».
   */
  mdm_references: MdmReferences | null;
}

export interface ContextVersion {
  version_number: number;
  changed_by: string;
  changed_at: string;
  status: "active" | "archived";
  title?: string;
  name?: string;
  description?: string;
  frontmatter_yaml?: string;
  body_md: string;
}

export interface ContextUsage {
  id: string;
  from_id: string;
  to_id: string;
  edge_type: string;
  from_type: string;
  to_type: string;
}

export interface ContextEvent {
  id: string;
  project_id: string;
  event_date: string;
  type: "business" | "incident" | "deployment" | "other";
  label: string;
  description: string | null;
  created_by: string;
  created_at: string;
}

export type SurfaceFailureKind = "denied" | "capability" | "error";

export interface SurfaceFailure {
  kind: SurfaceFailureKind;
  message: string;
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} is not a JSON object.`);
  }
  return value as Record<string, unknown>;
}

function stringField(
  value: unknown,
  label: string,
  { empty = false }: { empty?: boolean } = {},
): string {
  if (typeof value !== "string" || (!empty && value.trim() === "")) {
    throw new Error(`${label} must be ${empty ? "a string" : "a non-empty string"}.`);
  }
  return value;
}

function nullableString(value: unknown, label: string): string | null {
  if (value === null || value === undefined) return null;
  return stringField(value, label, { empty: true });
}

function positiveInteger(value: unknown, label: string): number {
  if (!Number.isInteger(value) || Number(value) < 1) {
    throw new Error(`${label} must be a positive integer.`);
  }
  return Number(value);
}

function isoTimestamp(value: unknown, label: string): string {
  const timestamp = stringField(value, label);
  if (!Number.isFinite(Date.parse(timestamp))) {
    throw new Error(`${label} must be an ISO timestamp.`);
  }
  return timestamp;
}

export function isCalendarDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00.000Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

function statusField(value: unknown, label: string): "active" | "archived" {
  if (value !== "active" && value !== "archived") {
    throw new Error(`${label} must be active or archived.`);
  }
  return value;
}

function assertScope(
  rowProjectId: string | null,
  requestedProjectId: string,
  label: string,
): void {
  if (rowProjectId !== null && rowProjectId !== requestedProjectId) {
    throw new Error(`${label} belongs to a different project.`);
  }
}

function uniqueIds<T extends { id: string }>(rows: T[], label: string): T[] {
  const ids = new Set<string>();
  for (const row of rows) {
    if (ids.has(row.id)) throw new Error(`${label} contains duplicate id ${row.id}.`);
    ids.add(row.id);
  }
  return rows;
}

export function parseCapabilities(value: unknown): ContextCapabilities | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const source = value as Record<string, unknown>;
  if (
    typeof source.can_write !== "boolean" ||
    typeof source.version_history !== "boolean" ||
    typeof source.usage !== "boolean"
  ) {
    return null;
  }
  return {
    can_write: source.can_write,
    version_history: source.version_history,
    usage: source.usage,
  };
}

export function parseWriteCapability(value: unknown): boolean | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const canWrite = (value as Record<string, unknown>).can_write;
  return typeof canWrite === "boolean" ? canWrite : null;
}

export function parseTopic(value: unknown, projectId: string): ContextTopic {
  const source = record(value, "Topic");
  const project_id = nullableString(source.project_id, "Topic project_id");
  const topic: ContextTopic = {
    id: stringField(source.id, "Topic id"),
    project_id,
    title: stringField(source.title, "Topic title"),
    body_md: stringField(source.body_md, "Topic body_md", { empty: true }),
    status: statusField(source.status, "Topic status"),
    owner: nullableString(source.owner, "Topic owner"),
    created_by: stringField(source.created_by, "Topic created_by"),
    created_at: isoTimestamp(source.created_at, "Topic created_at"),
    updated_at: isoTimestamp(source.updated_at, "Topic updated_at"),
    version_number: positiveInteger(source.version_number, "Topic version_number"),
    capabilities: parseCapabilities(source.capabilities),
  };
  assertScope(project_id, projectId, `Topic ${topic.id}`);
  return topic;
}

export function parseTopicsEnvelope(
  value: unknown,
  projectId: string,
): { topics: ContextTopic[]; capabilities: ContextCapabilities | null } {
  const source = record(value, "Topics response");
  if (!Array.isArray(source.topics)) throw new Error("Topics response must contain a topics array.");
  return {
    topics: uniqueIds(
      source.topics.map((topic) => parseTopic(topic, projectId)),
      "Topics response",
    ),
    capabilities: parseCapabilities(source.capabilities),
  };
}

/** Une moitie de `mdm_references` : les champs resolus, et les noms qui ne le sont pas. */
function parseMdmSide(value: unknown): MdmReferences["inline"] {
  const source = value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
  return {
    resolved: Array.isArray(source.resolved)
      ? source.resolved.filter(
          (field): field is Record<string, unknown> =>
            !!field && typeof field === "object" && !Array.isArray(field),
        )
      : [],
    unresolved: Array.isArray(source.unresolved)
      ? source.unresolved.filter((name): name is string => typeof name === "string")
      : [],
  };
}

/**
 * AI-157 : `mdm_references` ne vivait que dans l'outil MCP `get_procedure`. La
 * route REST la porte desormais, et la console la lit ici.
 *
 * ABSENT n'est PAS une erreur : la route de liste ne la resout pas, et une
 * lecture qui echouerait dessus rendrait la liste illisible pour un champ
 * accessoire. Absent -> `null`, et le panneau ne s'affiche simplement pas.
 */
export function parseMdmReferences(value: unknown): MdmReferences | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const source = value as Record<string, unknown>;
  return { inline: parseMdmSide(source.inline), tags: parseMdmSide(source.tags) };
}

export function parseProcedure(value: unknown, projectId: string): ContextProcedure {
  const source = record(value, "Skill");
  const project_id = nullableString(source.project_id, "Skill project_id");
  const procedure: ContextProcedure = {
    id: stringField(source.id, "Skill id"),
    project_id,
    name: stringField(source.name, "Skill name"),
    description: stringField(source.description, "Skill description", { empty: true }),
    frontmatter_yaml: stringField(source.frontmatter_yaml, "Skill frontmatter_yaml"),
    body_md: stringField(source.body_md, "Skill body_md", { empty: true }),
    status: statusField(source.status, "Skill status"),
    owner: nullableString(source.owner, "Skill owner"),
    created_by: stringField(source.created_by, "Skill created_by"),
    created_at: isoTimestamp(source.created_at, "Skill created_at"),
    updated_at: isoTimestamp(source.updated_at, "Skill updated_at"),
    version_number: positiveInteger(source.version_number, "Skill version_number"),
    capabilities: parseCapabilities(source.capabilities),
    mdm_references: parseMdmReferences(source.mdm_references),
  };
  assertScope(project_id, projectId, `Skill ${procedure.id}`);
  return procedure;
}

export function parseProceduresEnvelope(
  value: unknown,
  projectId: string,
): { procedures: ContextProcedure[]; capabilities: ContextCapabilities | null } {
  const source = record(value, "Skills answer");
  if (!Array.isArray(source.procedures)) {
    throw new Error("The answer for this project carried no list of Skills.");
  }
  return {
    procedures: uniqueIds(
      source.procedures.map((procedure) => parseProcedure(procedure, projectId)),
      "Skills answer",
    ),
    capabilities: parseCapabilities(source.capabilities),
  };
}

export function parseVersionsEnvelope(
  value: unknown,
  kind: "topic" | "procedure",
): ContextVersion[] {
  const source = record(value, "Versions response");
  if (!Array.isArray(source.versions)) {
    throw new Error("Versions response must contain a versions array.");
  }
  const seen = new Set<number>();
  return source.versions.map((value, index) => {
    const row = record(value, `Version ${index + 1}`);
    const version_number = positiveInteger(row.version_number, "Version number");
    if (seen.has(version_number)) throw new Error(`Versions response duplicates v${version_number}.`);
    seen.add(version_number);
    const version: ContextVersion = {
      version_number,
      changed_by: stringField(row.changed_by, "Version changed_by"),
      changed_at: isoTimestamp(row.changed_at, "Version changed_at"),
      status: statusField(row.status, "Version status"),
      body_md: stringField(row.body_md, "Version body_md", { empty: true }),
    };
    if (kind === "topic") {
      version.title = stringField(row.title, "Version title");
    } else {
      version.name = stringField(row.name, "Version name");
      version.description = stringField(row.description, "Version description", { empty: true });
      version.frontmatter_yaml = stringField(
        row.frontmatter_yaml,
        "Version frontmatter_yaml",
      );
    }
    return version;
  });
}

export function parseUsageEnvelope(value: unknown, objectId: string): ContextUsage[] {
  const source = record(value, "Usage response");
  if (!Array.isArray(source.edges)) throw new Error("Usage response must contain an edges array.");
  const all = uniqueIds(
    source.edges.map((value, index) => {
      const edge = record(value, `Usage edge ${index + 1}`);
      return {
        id: stringField(edge.id, "Usage edge id"),
        from_id: stringField(edge.from_id, "Usage edge from_id"),
        to_id: stringField(edge.to_id, "Usage edge to_id"),
        edge_type: stringField(edge.edge_type, "Usage edge edge_type"),
        from_type: stringField(edge.from_type, "Usage edge from_type"),
        to_type: stringField(edge.to_type, "Usage edge to_type"),
      };
    }),
    "Usage response",
  );
  return all.filter((edge) => edge.from_id === objectId || edge.to_id === objectId);
}

export function parseEvent(value: unknown, projectId: string): ContextEvent {
  const source = record(value, "Context event");
  const eventProjectId = stringField(source.project_id, "Context event project_id");
  if (eventProjectId !== projectId) {
    throw new Error("Context event belongs to a different project.");
  }
  const eventDate = stringField(source.event_date, "Context event event_date");
  if (!isCalendarDate(eventDate)) throw new Error("Context event date is invalid.");
  if (
    source.type !== "business" &&
    source.type !== "incident" &&
    source.type !== "deployment" &&
    source.type !== "other"
  ) {
    throw new Error("Context event type is invalid.");
  }
  return {
    id: stringField(source.id, "Context event id"),
    project_id: eventProjectId,
    event_date: eventDate,
    type: source.type,
    label: stringField(source.label, "Context event label"),
    description: nullableString(source.description, "Context event description"),
    created_by: stringField(source.created_by, "Context event created_by"),
    created_at: isoTimestamp(source.created_at, "Context event created_at"),
  };
}

export function parseEventsEnvelope(
  value: unknown,
  projectId: string,
): { events: ContextEvent[]; canWrite: boolean | null } {
  const source = record(value, "Context events response");
  if (!Array.isArray(source.events)) {
    throw new Error("Context events response must contain an events array.");
  }
  return {
    events: uniqueIds(
      source.events.map((event) => parseEvent(event, projectId)),
      "Context events response",
    ),
    canWrite: parseWriteCapability(source.capabilities),
  };
}

export async function readSurfaceFailure(response: Response): Promise<SurfaceFailure> {
  let code = "";
  let message = `HTTP ${response.status}`;
  try {
    const body = record(await response.json(), "Error response");
    code = typeof body.code === "string" ? body.code : "";
    if (typeof body.message === "string") message = body.message;
    else if (typeof body.error === "string") message = body.error;
  } catch {
    // The status remains the honest fallback for a malformed/non-JSON error.
  }
  const capabilityCodes = new Set([
    "capability_unavailable",
    "context_unavailable",
    "not_implemented",
  ]);
  if (capabilityCodes.has(code) || response.status === 501) {
    return { kind: "capability", message };
  }
  if (response.status === 401 || response.status === 403 || response.status === 404) {
    return { kind: "denied", message: "This context resource is unavailable or you do not have access." };
  }
  return { kind: "error", message };
}

export function errorMessage(error: unknown): string {
  if (error instanceof DOMException && error.name === "AbortError") return "Request cancelled.";
  return error instanceof Error ? error.message : "Unexpected error.";
}
