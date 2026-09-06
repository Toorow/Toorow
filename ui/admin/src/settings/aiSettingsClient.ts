/**
 * The AI settings seam — Story 75-4.
 *
 * ONE file for the three verbs of both scope families, so the panel never
 * builds a URL and the console has exactly one place that knows this address.
 * `apiGet` / `apiPut` / `apiDelete` carry the bearer and throw a typed
 * `ApiError`; a bare `fetch` here would be the anomaly `lib/apiFetch` exists to
 * remove.
 *
 * THE DELETE IS A CLEARING. It appends a version at the server and returns the
 * new resolution — the scope gives its override back to its parent, and nothing
 * anywhere is removed. The panel calls it "Return to the organization value"
 * for that reason, and never "Delete".
 */
import { apiDelete, apiGet, apiPut } from "../lib/apiFetch";

/** How far a model may reach for data. Names to arbitrate (Jean) — see context-hub.md. */
export type QueryScope = "governed_views_only" | "any_published_view" | "any_field";
export type NarrativeRegister = "plain" | "executive" | "technical";
export type WeekStartDay =
  | "monday" | "tuesday" | "wednesday" | "thursday" | "friday" | "saturday" | "sunday";

export type FiscalCalendar = { year_start_month?: number; week_start_day?: WeekStartDay };

export type AiSettingsValues = {
  rules_always: string[];
  rules_never: string[];
  query_scope: QueryScope;
  fiscal_calendar: FiscalCalendar;
  narrative_language: string;
  narrative_register: NarrativeRegister;
};

/** The six fields the cascade resolves, in the order the panel reads them. */
export const AI_SETTING_FIELDS = [
  "rules_always",
  "rules_never",
  "query_scope",
  "fiscal_calendar",
  "narrative_language",
  "narrative_register",
] as const;
export type AiSettingField = (typeof AI_SETTING_FIELDS)[number];

export type Scope = "PLATFORM" | "ORG" | "PROJECT";

/** One stored version. Every field is nullable: a version states only what its scope overrides. */
export type AiSettingsVersion = {
  id: string;
  version_number: number;
  cleared: boolean;
  rules_always: string[] | null;
  rules_never: string[] | null;
  query_scope: QueryScope | null;
  fiscal_calendar: FiscalCalendar | null;
  narrative_language: string | null;
  narrative_register: NarrativeRegister | null;
  note: string | null;
  created_by: string;
  created_at: string | null;
};

export type AiSettingsResponse = {
  scope: Scope;
  scope_id: string;
  org_id: string;
  resolved: AiSettingsValues;
  sources: Record<AiSettingField, Scope>;
  /** What THIS scope states today, or null when it states nothing. */
  own: AiSettingsVersion | null;
  /** What the parent scope states today, or null. */
  inherited: AiSettingsVersion | null;
  platform_defaults: AiSettingsValues;
  history: AiSettingsVersion[];
};

/** What a PUT sends: the fields this scope overrides, and nothing else. */
export type AiSettingsPatch = Partial<{
  rules_always: string[];
  rules_never: string[];
  query_scope: QueryScope;
  fiscal_calendar: FiscalCalendar;
  narrative_language: string;
  narrative_register: NarrativeRegister;
  note: string;
}>;

function base(scope: "project" | "organization", id: string): string {
  const family = scope === "project" ? "projects" : "organizations";
  return `/api/${family}/${encodeURIComponent(id)}/ai-settings`;
}

export function fetchProjectAiSettings(projectId: string): Promise<AiSettingsResponse> {
  return apiGet<AiSettingsResponse>(base("project", projectId));
}

export function saveProjectAiSettings(
  projectId: string,
  patch: AiSettingsPatch,
): Promise<AiSettingsResponse> {
  return apiPut<AiSettingsResponse>(base("project", projectId), patch);
}

/** Give this project's override back to its organization. Appends a version; deletes nothing. */
export function clearProjectAiSettings(projectId: string): Promise<AiSettingsResponse> {
  return apiDelete<AiSettingsResponse>(base("project", projectId));
}

export function fetchOrgAiSettings(orgId: string): Promise<AiSettingsResponse> {
  return apiGet<AiSettingsResponse>(base("organization", orgId));
}

export function saveOrgAiSettings(
  orgId: string,
  patch: AiSettingsPatch,
): Promise<AiSettingsResponse> {
  return apiPut<AiSettingsResponse>(base("organization", orgId), patch);
}

/** Give this organization's override back to the platform values. */
export function clearOrgAiSettings(orgId: string): Promise<AiSettingsResponse> {
  return apiDelete<AiSettingsResponse>(base("organization", orgId));
}
