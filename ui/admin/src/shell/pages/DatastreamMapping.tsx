import { useEffect, useState, type KeyboardEvent, type MouseEvent } from "react";
import { apiFetch } from "../../lib/apiFetch";
import "../application.css";
import "./datastream-mapping.css";

type Tab = "overview" | "data" | "mapping" | "runs";
type JsonRecord = Record<string, unknown>;
type BindingStatus = "suggested" | "confirmed" | "resolved" | "excluded" | "blocking";

interface FieldProfile {
  nullable: boolean;
  unique: boolean;
  cardinality_signal: string;
  sample_values: string[];
  confidence: number;
}
interface FieldSuggestion {
  semantic_role: string;
  aggregation: string;
  non_additive: boolean;
  currency: string;
  sensitivity: string;
  status: string;
  evidence: string[];
}
interface FieldBinding {
  canonical_target: string | null;
  mdm_target: string | null;
  status: BindingStatus;
  blocking_reason: string | null;
  confirmed_by: string | null;
  confirmed_reason: string | null;
}
interface MappingField {
  field_id: string;
  physical_type: string;
  profile: FieldProfile;
  suggestion: FieldSuggestion;
  binding: FieldBinding;
}
interface MappingAmbiguity {
  code: string;
  path: string;
  field_ids: string[];
  candidates: string[];
  repair: JsonRecord;
}
interface MappingPayload {
  mapping_contract_version: string;
  source_schema_hash: string;
  plan_version_id: string;
  grain: string[];
  fields: MappingField[];
  ambiguities: MappingAmbiguity[];
}
interface MappingVersion {
  id: string;
  datastream_id: string;
  version_number: number;
  plan_version_id: string;
  executable: boolean;
  blocking_count: number;
  ossie_spec_version: string;
  created_at: string | null;
  mapping_payload: MappingPayload;
  ossie_projection: JsonRecord | null;
}
interface MappingReadModel {
  datastream_id: string;
  project_id: string;
  datastream: { id: string; name?: string | null; module_name?: string | null; source_kind?: string | null };
  mapping_versions: MappingVersion[];
  published_execution: { id: string; mapping_version_id?: string | null } | null;
}
type LoadState =
  | { status: "loading"; key: string }
  | { status: "ok"; key: string; model: MappingReadModel }
  | { status: "denied"; key: string; message: string }
  | { status: "error"; key: string; message: string };

interface DatastreamMappingProps {
  projectId: string;
  datastreamId: string;
  onNavigateTab?: (tab: Tab) => void;
}

const BINDING_STATUSES: readonly BindingStatus[] = ["suggested", "confirmed", "resolved", "excluded", "blocking"];
function isRecord(value: unknown): value is JsonRecord { return typeof value === "object" && value !== null && !Array.isArray(value); }
function isNullableString(value: unknown): value is string | null { return value === null || typeof value === "string"; }
function isStringArray(value: unknown): value is string[] { return Array.isArray(value) && value.every((item) => typeof item === "string"); }
function isFiniteNumber(value: unknown): value is number { return typeof value === "number" && Number.isFinite(value); }
function parseField(value: unknown): MappingField {
  if (!isRecord(value) || typeof value.field_id !== "string" || typeof value.physical_type !== "string" || !isRecord(value.profile) || !isRecord(value.suggestion) || !isRecord(value.binding)) throw new Error("The mapping field contract was invalid.");
  const profile = value.profile;
  const suggestion = value.suggestion;
  const binding = value.binding;
  if (typeof profile.nullable !== "boolean" || typeof profile.unique !== "boolean" || typeof profile.cardinality_signal !== "string" || !isStringArray(profile.sample_values) || !isFiniteNumber(profile.confidence) || profile.confidence < 0 || profile.confidence > 1) throw new Error("The mapping profile contract was invalid.");
  if (typeof suggestion.semantic_role !== "string" || typeof suggestion.aggregation !== "string" || typeof suggestion.non_additive !== "boolean" || typeof suggestion.currency !== "string" || typeof suggestion.sensitivity !== "string" || typeof suggestion.status !== "string" || !isStringArray(suggestion.evidence)) throw new Error("The mapping suggestion contract was invalid.");
  if (!isNullableString(binding.canonical_target) || !isNullableString(binding.mdm_target) || !BINDING_STATUSES.includes(binding.status as BindingStatus) || !isNullableString(binding.blocking_reason) || !isNullableString(binding.confirmed_by) || !isNullableString(binding.confirmed_reason)) throw new Error("The mapping binding contract was invalid.");
  return value as unknown as MappingField;
}
function parseVersion(value: unknown, datastreamId: string): MappingVersion {
  if (!isRecord(value) || typeof value.id !== "string" || value.datastream_id !== datastreamId || !isFiniteNumber(value.version_number) || typeof value.plan_version_id !== "string" || typeof value.executable !== "boolean" || !isFiniteNumber(value.blocking_count) || typeof value.ossie_spec_version !== "string" || !isNullableString(value.created_at) || !isRecord(value.mapping_payload) || !(value.ossie_projection === null || isRecord(value.ossie_projection))) throw new Error("The mapping version contract was invalid.");
  const payload = value.mapping_payload;
  if (typeof payload.mapping_contract_version !== "string" || typeof payload.source_schema_hash !== "string" || typeof payload.plan_version_id !== "string" || !isStringArray(payload.grain) || !Array.isArray(payload.fields) || !Array.isArray(payload.ambiguities)) throw new Error("The mapping payload contract was invalid.");
  payload.fields = payload.fields.map(parseField);
  for (const ambiguity of payload.ambiguities) {
    if (!isRecord(ambiguity) || typeof ambiguity.code !== "string" || typeof ambiguity.path !== "string" || !isStringArray(ambiguity.field_ids) || !isStringArray(ambiguity.candidates) || !isRecord(ambiguity.repair)) throw new Error("The mapping ambiguity contract was invalid.");
  }
  return { ...value, mapping_payload: { ...payload, fields: payload.fields.map(parseField) } } as unknown as MappingVersion;
}
function parseReadModel(value: unknown, projectId: string, datastreamId: string): MappingReadModel {
  if (!isRecord(value) || value.project_id !== projectId || value.datastream_id !== datastreamId || !isRecord(value.datastream) || value.datastream.id !== datastreamId || !Array.isArray(value.mapping_versions)) throw new Error("The mapping response did not match the requested project and Datastream.");
  const versions = value.mapping_versions.map((item) => parseVersion(item, datastreamId)).sort((a, b) => b.version_number - a.version_number);
  if (!(value.published_execution === null || isRecord(value.published_execution))) throw new Error("The published mapping pointer contract was invalid.");
  if (isRecord(value.published_execution) && (typeof value.published_execution.id !== "string" || !isNullableString(value.published_execution.mapping_version_id))) throw new Error("The published mapping pointer contract was invalid.");
  return { ...value, mapping_versions: versions } as unknown as MappingReadModel;
}
function titleCase(value: string): string { return value.replace(/[_-]+/g, " ").replace(/\b\w/g, (char) => char.toUpperCase()); }
function formatDate(value: string | null): string { if (!value) return "Unknown"; const date = new Date(value); return Number.isNaN(date.getTime()) ? "Unknown" : date.toLocaleString("en-GB", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }); }
function bindingTone(status: BindingStatus): "success" | "warning" | "error" { if (status === "blocking") return "error"; if (status === "suggested" || status === "excluded") return "warning"; return "success"; }
function tabHref(projectId: string, datastreamId: string, tab: Tab): string { return `/p/${encodeURIComponent(projectId)}/data/datastreams/o/datastream/${encodeURIComponent(datastreamId)}/${tab}`; }

export default function DatastreamMapping({ projectId, datastreamId, onNavigateTab }: DatastreamMappingProps) {
  const key = `${projectId}\u0000${datastreamId}`;
  const [state, setState] = useState<LoadState>({ status: "loading", key });
  const [reload, setReload] = useState(0);
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(null);
  const [selectedFieldId, setSelectedFieldId] = useState<string | null>(null);
  const visibleState: LoadState = state.key === key ? state : { status: "loading", key };

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading", key });
    const query = new URLSearchParams({ project_id: projectId });
    void apiFetch(`/api/datastreams/${encodeURIComponent(datastreamId)}/read-model?${query.toString()}`, { cache: "no-store" })
      .then(async (response) => {
        if (!response.ok) {
          const body = await response.json().catch(() => null) as { message?: string } | null;
          const error = new Error(body?.message || `Mapping evidence could not be loaded (HTTP ${response.status}).`);
          if ([401, 403, 404].includes(response.status)) Object.assign(error, { denied: true });
          throw error;
        }
        return parseReadModel(await response.json() as unknown, projectId, datastreamId);
      })
      .then((model) => { if (!cancelled) setState({ status: "ok", key, model }); })
      .catch((error: unknown) => {
        if (cancelled) return;
        const message = error instanceof Error ? error.message : "Mapping evidence could not be loaded.";
        setState({ status: isRecord(error) && error.denied === true ? "denied" : "error", key, message });
      });
    return () => { cancelled = true; };
  }, [datastreamId, key, projectId, reload]);

  const model = visibleState.status === "ok" ? visibleState.model : null;
  const versions = model?.mapping_versions ?? [];
  const publishedId = model?.published_execution?.mapping_version_id ?? null;
  const published = versions.find((version) => version.id === publishedId) ?? null;
  const latest = versions[0] ?? null;
  const latestCandidate = latest && latest.id !== publishedId ? latest : null;
  const defaultVersion = published ?? latest;
  const selectedVersion = versions.find((version) => version.id === selectedVersionId) ?? defaultVersion ?? null;
  const fields = selectedVersion?.mapping_payload.fields ?? [];
  const selectedField = fields.find((field) => field.field_id === selectedFieldId) ?? fields[0] ?? null;
  const confirmedCount = fields.filter((field) => ["confirmed", "resolved"].includes(field.binding.status)).length;
  const name = model?.datastream.name?.trim() || datastreamId;
  const source = model?.datastream.module_name?.trim() || model?.datastream.source_kind?.trim() || "Source unavailable";
  const navigate = (event: MouseEvent<HTMLAnchorElement>, tab: Tab) => { if (!onNavigateTab || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return; event.preventDefault(); onNavigateTab(tab); };
  const selectField = (fieldId: string) => setSelectedFieldId(fieldId);
  const selectFieldByKeyboard = (event: KeyboardEvent<HTMLTableRowElement>, fieldId: string) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectField(fieldId); } };

  return <div className="workflow-main">
    <div className="object"><div className="object-logo provider-logo"><span aria-hidden="true">{name.slice(0, 1).toUpperCase()}</span></div><div><strong>{name}</strong><span>{source}</span></div></div>
    <nav className="local-tabs" aria-label="Datastream">
      {(["overview", "data", "mapping", "runs"] as const).map((tab) => <a key={tab} className={`local-tab${tab === "mapping" ? " active" : ""}`} aria-current={tab === "mapping" ? "page" : undefined} href={tabHref(projectId, datastreamId, tab)} onClick={(event) => navigate(event, tab)}>{titleCase(tab)}</a>)}
      <span className="local-tab" aria-disabled="true">Processing unavailable</span><span className="local-tab" aria-disabled="true">Outputs unavailable</span>
    </nav>
    <div className="page-header"><div><h1>Field mapping</h1><p>Persisted physical profiles, governed bindings and pinned Apache Ossie projection.</p></div><div className="header-actions"><button className="secondary-button" type="button" disabled title="A governed browser comparison contract is not available yet.">Compare unavailable</button><button className="primary-button" type="button" disabled title="Mapping publication requires the governed prepare-confirm command, which is not mounted here.">Publish mapping unavailable</button></div></div>

    {visibleState.status === "loading" && <section className="panel" role="status"><h2>Loading mapping evidence…</h2><p>No mapping values are shown until the scoped read completes.</p></section>}
    {visibleState.status === "denied" && <section className="panel" role="alert"><h2>Mapping access unavailable</h2><p>{visibleState.message}</p></section>}
    {visibleState.status === "error" && <section className="panel" role="alert"><h2>Mapping evidence unavailable</h2><p>{visibleState.message}</p><button className="secondary-button" type="button" onClick={() => setReload((value) => value + 1)}>Retry</button></section>}
    {model && versions.length === 0 && <section className="panel" data-testid="mapping-empty"><h2>No mapping versions</h2><p>No governed mapping evidence has been persisted for this Datastream.</p></section>}
    {model && publishedId && !published && <section className="panel" role="alert" data-testid="published-mapping-missing"><h2>Published mapping evidence unavailable</h2><p>The published execution references mapping {publishedId}, but that immutable version was not returned.</p></section>}

    {model && selectedVersion && <>
      <section className="mapping-summary">
        <div><span>Published mapping</span><strong className="event">{published ? `v${published.version_number}` : "None"}</strong><small>{published ? published.id : publishedId ? "Referenced version unavailable" : "No published mapping pointer"}</small></div>
        <div><span>Latest candidate</span><strong className="event">{latestCandidate ? `v${latestCandidate.version_number}` : "None"}</strong><small>{latestCandidate ? formatDate(latestCandidate.created_at) : "No newer mapping candidate"}</small></div>
        <div><span>Selected evidence</span><strong className="event">v{selectedVersion.version_number}</strong><small>{selectedVersion.id}</small></div>
        <div><span>Blocking</span><strong style={{ color: selectedVersion.blocking_count > 0 ? "var(--error)" : undefined }}>{selectedVersion.blocking_count}</strong><small>{selectedVersion.executable ? "Executable persisted version" : "Not executable"}</small></div>
      </section>
      <section className="panel mapping-version-picker"><label htmlFor="mapping-version">Evidence version</label><select id="mapping-version" className="selector" value={selectedVersion.id} onChange={(event) => { setSelectedVersionId(event.target.value); setSelectedFieldId(null); }}>{versions.map((version) => <option key={version.id} value={version.id}>v{version.version_number}{version.id === publishedId ? " — Published" : version.id === latestCandidate?.id ? " — Latest candidate" : ""}</option>)}</select><span>Plan {selectedVersion.plan_version_id} · Ossie {selectedVersion.ossie_spec_version} · {formatDate(selectedVersion.created_at)}</span></section>
      {selectedVersion.mapping_payload.ambiguities.length > 0 && <section className="panel" role="status"><h2>Persisted ambiguities</h2><ul>{selectedVersion.mapping_payload.ambiguities.map((ambiguity, index) => <li key={`${ambiguity.code}-${index}`}><b>{ambiguity.code}</b> — {ambiguity.field_ids.join(", ") || ambiguity.path}</li>)}</ul></section>}
      {fields.length === 0 ? <section className="panel" data-testid="mapping-fields-empty"><h2>No mapped fields</h2><p>The selected immutable version contains no field evidence.</p></section> : <section className="field-workbench">
        <div className="mapping-panel"><div className="mapping-toolbar"><div><h2>Source fields</h2></div><span>{confirmedCount} confirmed or resolved · {fields.length} persisted</span></div><div className="table-scroll" tabIndex={0} aria-label="Mapped fields horizontal scroll"><table className="field-table"><thead><tr><th style={{ width: "24%" }}>Source field</th><th style={{ width: "28%" }}>Physical profile</th><th style={{ width: "30%" }}>Governed meaning</th><th style={{ width: "18%" }}>State</th></tr></thead><tbody>{fields.map((field) => <tr key={field.field_id} className={field.field_id === selectedField?.field_id ? "selected" : undefined} tabIndex={0} aria-selected={field.field_id === selectedField?.field_id} onClick={() => selectField(field.field_id)} onKeyDown={(event) => selectFieldByKeyboard(event, field.field_id)}><td><div className="field-name"><div><strong className="field-code">{field.field_id}</strong><small>{field.suggestion.sensitivity}</small></div></div></td><td><strong>{field.physical_type}</strong><small>{field.profile.nullable ? "Nullable" : "Not nullable"} · {field.profile.cardinality_signal} cardinality · {Math.round(field.profile.confidence * 100)}% confidence</small></td><td><strong>{field.binding.canonical_target || field.binding.mdm_target || "No governed target"}</strong><small>{field.suggestion.semantic_role} · {field.suggestion.aggregation}</small></td><td><span className={`signal-label ${bindingTone(field.binding.status)}`}><span className="signal-mark" />{titleCase(field.binding.status)}</span></td></tr>)}</tbody></table></div><div className="mapping-footer"><span>{fields.length} persisted fields</span><strong>{selectedVersion.mapping_payload.grain.length > 0 ? `Grain: ${selectedVersion.mapping_payload.grain.join(", ")}` : "No persisted grain"}</strong></div></div>
        {selectedField && <aside className="evidence-panel" aria-label="Selected field evidence"><div className="evidence-header"><h2>{selectedField.field_id}</h2><span className={`signal-label ${bindingTone(selectedField.binding.status)}`}><span className="signal-mark" />{titleCase(selectedField.binding.status)}</span></div><div className="evidence-body"><section className="evidence-section"><span>Physical evidence</span><strong>{selectedField.physical_type}</strong><p>{selectedField.profile.nullable ? "Nullable" : "Not nullable"}; {selectedField.profile.unique ? "unique" : "not unique"}; {selectedField.profile.cardinality_signal} cardinality; {Math.round(selectedField.profile.confidence * 100)}% confidence.</p></section><section className="evidence-section"><span>Governed binding</span><strong>{selectedField.binding.canonical_target || selectedField.binding.mdm_target || "Not bound"}</strong><p>{selectedField.suggestion.semantic_role} · {selectedField.suggestion.aggregation} · {selectedField.suggestion.currency} · {selectedField.suggestion.sensitivity}</p>{selectedField.binding.blocking_reason && <p>Blocking reason: {selectedField.binding.blocking_reason}</p>}{selectedField.binding.confirmed_by && <p>Confirmed by {selectedField.binding.confirmed_by}{selectedField.binding.confirmed_reason ? `: ${selectedField.binding.confirmed_reason}` : ""}</p>}</section><section className="evidence-section"><span>Apache Ossie projection</span>{selectedVersion.ossie_projection ? <pre className="code-box mono">{JSON.stringify(selectedVersion.ossie_projection, null, 2)}</pre> : <p>No persisted Apache Ossie projection is available.</p>}</section>{selectedField.suggestion.evidence.length > 0 && <div className="suggestion-note">{selectedField.suggestion.evidence.join(" · ")}</div>}<div className="drawer-actions"><button className="primary-button" type="button" disabled title="A governed binding-confirmation browser command is not available.">Confirm suggestion unavailable</button><button className="secondary-button" type="button" disabled title="A governed change-binding browser command is not available.">Change binding unavailable</button></div></div></aside>}
      </section>}
    </>}
  </div>;
}