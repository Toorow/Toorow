/**
 * DatastreamMapping — faithful React port of the validated Datastream mapping
 * mockup (the "Mapping" local-tab of a single Datastream).
 *
 * Source of visual truth:
 *   _bmad-output/planning-artifacts/ux-designs/ux-connector-2026-07-23/
 *     mockups/datastream-mapping.html   (+ workflow-surfaces.css)
 *
 * The application shell (ApplicationShell.tsx) already renders the frame, sidebar,
 * topbar, and <main>. This component renders ONLY the page content that lives
 * inside <main>: the object header row + local-tabs + the field-mapping workbench
 * (Source field / Physical profile / Governed meaning table on the left, and the
 * per-field evidence panel — Physical evidence / Governed binding / Apache Ossie
 * projection — on the right).
 *
 * The mockup keeps THREE layers separated and this port preserves that separation:
 *   • Physical profile  — the raw field as observed (type, unit, null-rate, samples)
 *   • Governed meaning  — the canonical/MDM binding (concept + role + aggregation)
 *   • Semantic model    — the pinned Apache Ossie 0.1.1 projection (code-box)
 *
 * Styling: application.css (global, via the shell) for shell/layout classes +
 * datastream-mapping.css for the mapping-surface classes not already in
 * application.css. Colors come exclusively from the application.css CSS variables
 * for every brand/semantic value; numbers use Geist tabular; ids / versions /
 * Ossie refs use JetBrains Mono via the .event / .mono / .field-code classes.
 *
 * ── Data ──────────────────────────────────────────────────────────────────────
 * The one honest reuse is GET /api/datastreams/{id}/read-model, which returns
 * DatastreamReadModel with mapping_versions: MappingVersion[] (version_number,
 * field_count, created_at) and published_execution (mapping_version_id,
 * state_changed_at). Those map to the summary strip's "Mapping version" (latest
 * version_number), its "Published" date (latest mapping created_at, else the
 * published execution's state_changed_at), and "Fields" (latest field_count).
 *
 * EVERYTHING else the mockup shows is a prepared-mapping / profiling / semantic
 * concern that has NO backing API today, so it is rendered as the mockup's literal
 * and flagged with // TODO(api). See the GAP LIST in the port report. The surface
 * renders finished with no backend: without a datastreamId, or when the fetch
 * fails, it falls back to the validated mockup literals so nothing is ever blank.
 */
import { useEffect, useMemo, useState } from "react";
import "../application.css";
import "./datastream-mapping.css";

// ---------------------------------------------------------------------------
// API shape (subset of DatastreamReadModel — see datastreams/ops/opsTypes.ts)
// ---------------------------------------------------------------------------

interface MappingVersion {
  id: string;
  version_number?: number | null;
  field_count?: number | null;
  created_at?: string | null;
}

interface DatastreamReadModelLite {
  mapping_versions?: MappingVersion[];
  published_execution?: {
    mapping_version_id?: string | null;
    state_changed_at?: string | null; // freshness signal — published-date fallback
  } | null;
}

// ---------------------------------------------------------------------------
// Field-row model — the columns the mapping table renders for one source field.
// Physical / Governed layers each keep their own head + sub-note, faithful to the
// mockup. `state` drives the signal-label variant (success | warning | error).
// ---------------------------------------------------------------------------

type FieldState = "success" | "warning" | "error";

interface FieldRow {
  /** Physical field name — mono, e.g. "spend". */
  code: string;
  /** Physical requiredness note — "Required" | "Optional". TODO(api). */
  requiredness: string;
  /** Physical profile head — observed type/unit, e.g. "NUMBER · EUR". TODO(api). */
  physicalType: string;
  /** Physical profile sub-note — cardinality / null-rate / samples. TODO(api). */
  physicalProfile: string;
  /** Governed meaning head — canonical concept, e.g. "Media spend". TODO(api). */
  governedMeaning: string;
  /** Governed meaning sub-note — role · aggregation, e.g. "Metric · Sum". TODO(api). */
  governedRole: string;
  /** Mapping state driving the State column + row selection. TODO(api). */
  state: FieldState;
  stateLabel: string;
  /** Selected row (drives the evidence panel + the left rose spine). */
  selected?: boolean;
}

// ---------------------------------------------------------------------------
// Mockup-literal rows — the validated source of truth. Used verbatim as the
// finished-with-no-backend fallback. None of the three layers below has a backing
// API today (see GAP LIST); every value here is a // TODO(api) literal.
// ---------------------------------------------------------------------------

const MOCK_FIELDS: FieldRow[] = [
  {
    code: "date",
    requiredness: "Required",
    physicalType: "DATE",
    physicalProfile: "0% null · 30 values",
    governedMeaning: "Primary date",
    governedRole: "Dimension · Day",
    state: "success",
    stateLabel: "Confirmed",
  },
  {
    code: "campaign_name",
    requiredness: "Required",
    physicalType: "STRING",
    physicalProfile: "0% null · 62 values",
    governedMeaning: "Campaign",
    governedRole: "Dimension",
    state: "success",
    stateLabel: "Confirmed",
  },
  {
    code: "spend",
    requiredness: "Required",
    physicalType: "NUMBER · EUR",
    physicalProfile: "0% null · non-negative",
    governedMeaning: "Media spend",
    governedRole: "Metric · Sum",
    state: "warning",
    stateLabel: "Suggestion",
    selected: true,
  },
  {
    code: "impressions",
    requiredness: "Optional",
    physicalType: "INTEGER",
    physicalProfile: "0% null · non-negative",
    governedMeaning: "Impressions",
    governedRole: "Metric · Sum",
    state: "success",
    stateLabel: "Confirmed",
  },
  {
    code: "country",
    requiredness: "Optional",
    physicalType: "STRING",
    physicalProfile: "ISO country labels",
    governedMeaning: "Country",
    governedRole: "Dimension",
    state: "error",
    stateLabel: "Conflict",
  },
];

// Summary-strip literals. `version`, `publishedOn` and `fields` are overridden
// with the real read-model values when the API answers (see below); `confirmed`
// and `blocking` have no prepared-mapping API and stay literal. TODO(api).
const MOCK_SUMMARY = {
  version: "v8",
  publishedOn: "Published 22 Jul",
  fields: 48,
  confirmed: 45,
  blocking: 1,
};

// ---------------------------------------------------------------------------
// Fetch — honest reuse of the Datastream ops read-model.
// ---------------------------------------------------------------------------

async function fetchReadModel(datastreamId: string): Promise<DatastreamReadModelLite> {
  const resp = await fetch(`/api/datastreams/${encodeURIComponent(datastreamId)}/read-model`, {
    headers: { Authorization: `Bearer ${localStorage.getItem("api_token") || ""}` },
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return (await resp.json()) as DatastreamReadModelLite;
}

/** Latest mapping version by version_number (fallback: last in list). */
function latestMappingVersion(model: DatastreamReadModelLite): MappingVersion | null {
  const versions = model.mapping_versions ?? [];
  if (versions.length === 0) return null;
  return versions.reduce((best, v) =>
    (v.version_number ?? -Infinity) >= (best.version_number ?? -Infinity) ? v : best,
  );
}

/** "Published 22 Jul" from an ISO date — locale-stable, no time component. */
function publishedLabel(iso?: string | null): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return `Published ${d.toLocaleDateString("en-GB", { day: "numeric", month: "short" })}`;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface DatastreamMappingProps {
  projectId?: string;
  datastreamId?: string;
}

export default function DatastreamMapping({
  projectId: _projectId = "default",
  datastreamId,
}: DatastreamMappingProps) {
  const [model, setModel] = useState<DatastreamReadModelLite | null>(null);

  useEffect(() => {
    if (!datastreamId) {
      setModel(null);
      return;
    }
    let cancelled = false;
    fetchReadModel(datastreamId)
      .then((data) => {
        if (!cancelled) setModel(data);
      })
      .catch(() => {
        // Non-fatal: fall back to the validated mockup literals.
        if (!cancelled) setModel(null);
      });
    return () => {
      cancelled = true;
    };
  }, [datastreamId]);

  // Summary — REAL mapping version / published date / field count from the
  // read-model when present; the rest stay mockup literals (no prepared-mapping
  // API exposes confirmed / blocking counts). See GAP LIST.
  const summary = useMemo(() => {
    if (!model) return MOCK_SUMMARY;
    const latest = latestMappingVersion(model);
    if (!latest) return MOCK_SUMMARY;
    return {
      version: latest.version_number != null ? `v${latest.version_number}` : MOCK_SUMMARY.version, // REAL
      // REAL: mapping_versions[].created_at, else published_execution.state_changed_at.
      publishedOn:
        publishedLabel(latest.created_at) ??
        publishedLabel(model.published_execution?.state_changed_at) ??
        MOCK_SUMMARY.publishedOn,
      fields: latest.field_count ?? MOCK_SUMMARY.fields, // REAL
      confirmed: MOCK_SUMMARY.confirmed, // TODO(api): no prepared-mapping confirmed count
      blocking: MOCK_SUMMARY.blocking, // TODO(api): no blocking-field count
    };
  }, [model]);

  // Field rows have NO backing API (physical profile / governed meaning / state
  // are prepared-mapping + profiling concerns). Always the mockup literals.
  const fields = MOCK_FIELDS; // TODO(api): per-field prepared-mapping list
  const selected = fields.find((f) => f.selected) ?? fields[0];

  return (
    <div className="workflow-main">
      {/* Object header — identifies the Datastream this mapping belongs to.
          TODO(api): name / provider / kind come from the datastream summary. */}
      <div className="object">
        <div className="object-logo provider-logo">
          {/* Real provider asset; never hand-drawn. TODO(api): resolve provider. */}
          <img src="/connectors/meta.svg" alt="Meta" />
        </div>
        <div>
          <strong>Campaign performance</strong>
          <span>Meta Ads · Spend</span>
        </div>
      </div>

      {/* Local tabs — the Datastream's own sub-navigation. "Mapping" is active.
          TODO(api): tab routing wiring; only "Mapping" is this surface. */}
      <nav className="local-tabs" aria-label="Datastream">
        <div className="local-tab">Overview</div>
        <div className="local-tab">Data</div>
        <div className="local-tab active">Mapping</div>
        <div className="local-tab">Processing</div>
        <div className="local-tab">Runs</div>
        <div className="local-tab">Outputs</div>
      </nav>

      <div className="page-header">
        <div>
          <h1>Field mapping</h1>
          <p>Govern physical fields, canonical meaning, and the pinned Apache Ossie projection.</p>
        </div>
        <div className="header-actions">
          {/* TODO(api): mapping-version compare flow */}
          <button className="secondary-button">Compare versions</button>
          {/* TODO(api): the prepare → confirm → publish mapping flow */}
          <button className="primary-button">Publish mapping</button>
        </div>
      </div>

      <section className="mapping-summary">
        <div>
          <span>Mapping version</span>
          {/* REAL: latest mapping_versions[].version_number */}
          <strong className="event">{summary.version}</strong>
          {/* REAL: latest mapping_versions[].created_at */}
          <small>{summary.publishedOn}</small>
        </div>
        <div>
          <span>Fields</span>
          {/* REAL: latest mapping_versions[].field_count */}
          <strong>{summary.fields}</strong>
          <small>From the latest schema</small>
        </div>
        <div>
          <span>Confirmed</span>
          {/* TODO(api): no prepared-mapping confirmed count */}
          <strong>{summary.confirmed}</strong>
          <small>Ready for processing</small>
        </div>
        <div>
          <span>Blocking</span>
          {/* TODO(api): no blocking-field count */}
          <strong style={{ color: "var(--error)" }}>{summary.blocking}</strong>
          <small>Requires a decision</small>
        </div>
      </section>

      <section className="field-workbench">
        <div className="mapping-panel">
          <div className="mapping-toolbar">
            <div>
              <h2>Source fields</h2>
            </div>
            <div className="mapping-filters">
              {/* TODO(api): state filter — prepared-mapping state not exposed */}
              <button className="selector">State: All</button>
              {/* TODO(api): field search wiring */}
              <div className="field-control mapping-search">Search fields</div>
            </div>
          </div>

          <table className="field-table">
            <thead>
              <tr>
                <th style={{ width: "26%" }}>Source field</th>
                <th style={{ width: "24%" }}>Physical profile</th>
                <th style={{ width: "30%" }}>Governed meaning</th>
                <th style={{ width: "20%" }}>State</th>
              </tr>
            </thead>
            <tbody>
              {fields.map((f) => (
                <tr key={f.code} className={f.selected ? "selected" : undefined}>
                  {/* Source field — physical field name (mono) + requiredness. */}
                  <td>
                    <div className="field-name">
                      <div>
                        <strong className="field-code">{f.code}</strong>
                        {/* TODO(api): requiredness from the physical schema */}
                        <small>{f.requiredness}</small>
                      </div>
                    </div>
                  </td>
                  {/* Physical profile — observed type/unit + cardinality/null-rate. */}
                  <td>
                    {/* TODO(api): physical type / unit not profiled */}
                    <strong>{f.physicalType}</strong>
                    {/* TODO(api): cardinality / null-rate / sample count not profiled */}
                    <small>{f.physicalProfile}</small>
                  </td>
                  {/* Governed meaning — canonical/MDM binding + role · aggregation. */}
                  <td>
                    {/* TODO(api): canonical/MDM binding not exposed per field */}
                    <strong>{f.governedMeaning}</strong>
                    {/* TODO(api): semantic role · aggregation not exposed per field */}
                    <small>{f.governedRole}</small>
                  </td>
                  {/* State — prepared-mapping signal. */}
                  <td>
                    {/* TODO(api): prepared-mapping state (confirmed/suggestion/conflict) */}
                    <span className={`signal-label ${f.state}`}>
                      <span className="signal-mark" />
                      {f.stateLabel}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <div className="mapping-footer">
            {/* REAL total (summary.fields); shown count is the literal row set. */}
            <span>
              {fields.length} of <span className="number">{summary.fields}</span> fields
            </span>
            {/* TODO(api): blocking-field gate on the prepare → publish flow */}
            <strong>1 blocking field must be resolved before publishing</strong>
          </div>
        </div>

        {/* Evidence panel — the selected field across all three layers. */}
        <aside className="evidence-panel" aria-label="Selected field evidence">
          <div className="evidence-header">
            <h2>{selected.code}</h2>
            <span className={`signal-label ${selected.state}`}>
              <span className="signal-mark" />
              {selected.stateLabel}
            </span>
          </div>
          <div className="evidence-body">
            {/* Physical layer. */}
            <section className="evidence-section">
              <span>Physical evidence</span>
              {/* TODO(api): physical profile / evidence not computed */}
              <strong>{selected.physicalType}</strong>
              <p>30 daily samples, no null values, and no negative observations.</p>
            </section>
            {/* Governed layer. */}
            <section className="evidence-section">
              <span>Governed binding</span>
              {/* TODO(api): canonical/MDM binding not exposed per field */}
              <strong>{selected.governedMeaning}</strong>
              <p>Metric · additive · project reporting currency.</p>
            </section>
            {/* Semantic layer — pinned Apache Ossie 0.1.1 projection. */}
            <section className="evidence-section">
              <span>Apache Ossie projection</span>
              {/* TODO(api): no Apache Ossie 0.1.1 projection is generated today */}
              <div className="code-box mono">
                ossie_spec_version: "0.1.1"
                <br />
                metrics:
                <br />
                &nbsp;&nbsp;- media_spend
              </div>
            </section>
            {/* TODO(api): suggestion provenance / confidence not computed */}
            <div className="suggestion-note">
              Suggested from field profile and three confirmed Datastreams. A person must confirm
              before publication.
            </div>
            <div className="drawer-actions">
              {/* TODO(api): confirm step of the prepare → confirm → publish flow */}
              <button className="primary-button">Confirm suggestion</button>
              {/* TODO(api): change-binding flow */}
              <button className="secondary-button">Change binding</button>
            </div>
          </div>
        </aside>
      </section>
    </div>
  );
}
