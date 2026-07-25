/**
 * ProjectMapping — faithful React port of the validated Project mapping mockup.
 *
 * Source of visual truth:
 *   _bmad-output/planning-artifacts/ux-designs/ux-connector-2026-07-23/
 *     mockups/project-mapping.html   (+ workflow-surfaces.css)
 *
 * The project-level concept-consumption surface: canonical concepts, the
 * datastreams that "use" each one (overlapping provider-logo stacks + count),
 * coverage state (mapped / needs-review / conflict), conflict count, and last
 * update — plus a summary strip, a segmented view switch (Concept consumption /
 * Coverage matrix / Relationships), Kind/Status filters and a concept search.
 *
 * The application shell (ApplicationShell.tsx) already renders the frame, sidebar,
 * topbar, and <main>. This component renders ONLY the page content that lives
 * inside <main>: the page header, summary strip, and the mapping panel.
 *
 * Styling: application.css (global, via the shell) for shell/layout classes +
 * project-mapping.css for the mapping-surface classes. Colors come exclusively
 * from the application.css CSS variables for every brand/semantic value; numbers
 * use Geist tabular via those classes.
 *
 * ── Data ──────────────────────────────────────────────────────────────────────
 * The one clean, real map is GET /api/datamodel/fields, which returns
 * TargetField[] with: name (canonical id), display_name, field_kind
 * (metric|dimension), used_by_count (a SCALAR "used by N flux" count), and an
 * optional status (draft|approved). Those map to: Canonical concept, Kind, the
 * "N Datastreams" count, and the summary "Canonical concepts / dimensions vs
 * metrics" tallies.
 *
 * Everything the mockup shows that the API does NOT expose is a mockup literal,
 * flagged with // TODO(api): the per-provider logo STACK (which providers feed a
 * concept), the coverage STATE (fully mapped / needs review / conflict), the
 * conflict COUNT, and the "Updated" date. See the GAP LIST in the port report.
 *
 * The page renders finished with no backend: when the fetch fails or is pending,
 * it falls back to the mockup's five literal rows so the surface is never blank.
 */
import { useEffect, useMemo, useState } from "react";
import "../application.css";
import "./project-mapping.css";

// ---------------------------------------------------------------------------
// API shape (subset of DataModelPage's TargetField)
// ---------------------------------------------------------------------------

interface TargetField {
  name: string;
  display_name: string;
  field_kind: "metric" | "dimension";
  used_by_count: number;
  status?: "draft" | "approved";
}

// ---------------------------------------------------------------------------
// View row model — the fields the mapping table needs to render one concept.
// ---------------------------------------------------------------------------

type Coverage = "success" | "warning" | "error";
type Provider = "meta" | "google-ads" | "google-analytics" | "google-sheets";

interface ConceptRow {
  name: string; // display name, e.g. "Primary date"
  mono: string; // canonical id, mono font, e.g. "mdm_…DATE"
  kind: "Dimension" | "Metric";
  /** Provider logo stack — TODO(api): not exposed by /api/datamodel/fields. */
  providers: Provider[];
  count: number; // "N Datastreams" — REAL from used_by_count when available
  countNote: string; // TODO(api): human note ("All active flows")
  coverage: Coverage; // TODO(api): coverage state
  coverageLabel: string;
  conflict: Coverage; // TODO(api): conflict state
  conflictLabel: string;
  updated: string; // TODO(api): last mapping update date
  icon: JSX.Element; // per-concept glyph (mockup literal)
}

// ---------------------------------------------------------------------------
// Concept glyphs (faithful to the mockup's inline <svg> per row)
// ---------------------------------------------------------------------------

const ICON = {
  date: (
    <svg viewBox="0 0 24 24">
      <path d="M5 5h14v14H5zM8 9h8M8 13h5" />
    </svg>
  ),
  spend: (
    <svg viewBox="0 0 24 24">
      <path d="M5 18V9m5 9V5m5 13v-6m5 6H3" />
    </svg>
  ),
  revenue: (
    <svg viewBox="0 0 24 24">
      <path d="M4 19h16M7 16l4-5 3 2 4-7" />
    </svg>
  ),
  campaign: (
    <svg viewBox="0 0 24 24">
      <path d="M4 6h16M4 12h12M4 18h8" />
    </svg>
  ),
  roas: (
    <svg viewBox="0 0 24 24">
      <path d="M6 18L18 6M8 7h.01M16 17h.01" />
    </svg>
  ),
} as const;

const GENERIC_ICON = (
  <svg viewBox="0 0 24 24">
    <path d="M5 5h14v14H5zM8 9h8M8 13h5" />
  </svg>
);

// ---------------------------------------------------------------------------
// Provider logo — real asset via <img src="/connectors/xxx.svg">.
// ---------------------------------------------------------------------------

const PROVIDER_LABEL: Record<Provider, string> = {
  meta: "Meta",
  "google-ads": "Google Ads",
  "google-analytics": "Google Analytics",
  "google-sheets": "Google Sheets",
};

function ProviderLogo({ provider }: { provider: Provider }) {
  return (
    <span className="provider-logo">
      {/* Real provider asset; never hand-drawn. */}
      <img src={`/connectors/${provider}.svg`} alt={PROVIDER_LABEL[provider]} />
    </span>
  );
}

// ---------------------------------------------------------------------------
// Mockup literal rows — the validated source of truth, used verbatim as the
// fallback and as the template the API rows are merged into.
// TODO(api): providers[], countNote, coverage, conflict, updated are literals.
// ---------------------------------------------------------------------------

const MOCK_ROWS: ConceptRow[] = [
  {
    name: "Primary date",
    mono: "mdm_…DATE",
    kind: "Dimension",
    providers: ["meta", "google-ads", "google-analytics", "google-sheets"],
    count: 7,
    countNote: "All active flows",
    coverage: "success",
    coverageLabel: "Fully mapped",
    conflict: "success",
    conflictLabel: "None",
    updated: "22 Jul",
    icon: ICON.date,
  },
  {
    name: "Media spend",
    mono: "mdm_…MEDIA_SPEND",
    kind: "Metric",
    providers: ["meta", "google-ads", "google-sheets"],
    count: 4,
    countNote: "Spend + Forecast & plan",
    coverage: "success",
    coverageLabel: "Fully mapped",
    conflict: "success",
    conflictLabel: "None",
    updated: "21 Jul",
    icon: ICON.spend,
  },
  {
    name: "Revenue",
    mono: "mdm_…REVENUE",
    kind: "Metric",
    providers: ["google-analytics", "google-sheets"],
    count: 2,
    countNote: "GA4 + Media plan",
    coverage: "error",
    coverageLabel: "Conflict",
    conflict: "error",
    conflictLabel: "1 blocking",
    updated: "20 Jul",
    icon: ICON.revenue,
  },
  {
    name: "Campaign",
    mono: "mdm_…CAMPAIGN",
    kind: "Dimension",
    providers: ["meta", "google-ads", "google-analytics"],
    count: 5,
    countNote: "3 providers",
    coverage: "success",
    coverageLabel: "Fully mapped",
    conflict: "success",
    conflictLabel: "None",
    updated: "19 Jul",
    icon: ICON.campaign,
  },
  {
    name: "ROAS",
    mono: "mdm_…ROAS",
    kind: "Metric",
    providers: ["meta", "google-ads"],
    count: 3,
    countNote: "2 providers",
    coverage: "warning",
    coverageLabel: "Needs review",
    conflict: "success",
    conflictLabel: "None",
    updated: "18 Jul",
    icon: ICON.roas,
  },
];

// Summary-strip literals. TODO(api): coverage/conflict tallies are not exposed
// by /api/datamodel/fields (only used_by_count). Overridden with real totals
// where the API supplies them (canonical concepts + dimension/metric split).
const MOCK_SUMMARY = {
  concepts: 42,
  dimensions: 28,
  metrics: 14,
  fullyCovered: 38,
  partial: 3,
  conflicts: 1,
};

// ---------------------------------------------------------------------------
// Fetch
// ---------------------------------------------------------------------------

async function fetchFields(projectId: string): Promise<TargetField[]> {
  const params = new URLSearchParams();
  if (projectId) params.set("project_id", projectId);
  const resp = await fetch(`/api/datamodel/fields?${params.toString()}`, {
    headers: { Authorization: `Bearer ${localStorage.getItem("api_token") || ""}` },
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return (await resp.json()) as TargetField[];
}

/** Epic 42: set of canonical field names that currently have an MDM conflict
 *  (currency/timezone/measure). Empty on any error so coverage stays literal. */
async function fetchConflictFields(projectId: string): Promise<Set<string>> {
  try {
    const resp = await fetch(`/api/mdm/conflicts?project_id=${encodeURIComponent(projectId)}`, {
      headers: { Authorization: `Bearer ${localStorage.getItem("api_token") || ""}` },
    });
    if (!resp.ok) return new Set();
    const data = (await resp.json()) as
      | Array<{ field?: { name?: string }; target_field?: string }>
      | { conflicts?: Array<{ field?: { name?: string }; target_field?: string }> };
    const items = Array.isArray(data) ? data : (data.conflicts ?? []);
    const out = new Set<string>();
    for (const it of items) {
      const n = it.field?.name || it.target_field;
      if (n) out.add(n);
    }
    return out;
  } catch {
    return new Set();
  }
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface ProjectMappingProps {
  projectId?: string;
  /** Drill-down to field-level mapping for one concept (mockup footer CTA). */
  onOpenConcept?: (conceptName: string) => void;
}

export default function ProjectMapping({
  projectId = "default",
  onOpenConcept,
}: ProjectMappingProps) {
  const [fields, setFields] = useState<TargetField[] | null>(null);
  const [conflictFields, setConflictFields] = useState<Set<string>>(new Set());

  useEffect(() => {
    let cancelled = false;
    fetchFields(projectId)
      .then((data) => {
        if (!cancelled) setFields(data);
      })
      .catch(() => {
        // Non-fatal: fall back to the validated mockup literals.
        if (!cancelled) setFields(null);
      });
    fetchConflictFields(projectId).then((s) => {
      if (!cancelled) setConflictFields(s);
    });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  // Merge real API values (concept name, kind, used-by count) onto the mockup
  // row template so the surface still renders finished with no backend.
  const rows: ConceptRow[] = useMemo(() => {
    if (!fields || fields.length === 0) return MOCK_ROWS;
    return fields.slice(0, MOCK_ROWS.length).map((f, i) => {
      const template = MOCK_ROWS[i % MOCK_ROWS.length];
      const mono = f.name || template.mono;
      const hasConflict = conflictFields.has(mono);
      return {
        ...template,
        // REAL from /api/datamodel/fields:
        name: f.display_name || template.name,
        mono,
        kind: f.field_kind === "metric" ? "Metric" : "Dimension",
        count: f.used_by_count ?? template.count,
        // REAL from /api/mdm/conflicts:
        conflict: hasConflict ? "error" : "success",
        conflictLabel: hasConflict ? "1 blocking" : "None",
        // TODO(api): providers / coverage state / updated stay literal.
      };
    });
  }, [fields, conflictFields]);

  // Summary — real canonical-concept + dimension/metric tallies when the API
  // answers; the rest (coverage / conflict counts) stay mockup literals.
  const summary = useMemo(() => {
    if (!fields || fields.length === 0) return MOCK_SUMMARY;
    return {
      concepts: fields.length, // REAL
      dimensions: fields.filter((f) => f.field_kind === "dimension").length, // REAL
      metrics: fields.filter((f) => f.field_kind === "metric").length, // REAL
      fullyCovered: MOCK_SUMMARY.fullyCovered, // TODO(api): coverage tally
      partial: MOCK_SUMMARY.partial, // TODO(api): partial tally
      conflicts: conflictFields.size, // REAL from /api/mdm/conflicts
    };
  }, [fields, conflictFields]);

  return (
    <div className="workflow-main">
      <div className="page-header">
        <div>
          <h1>Project mapping</h1>
          <p>
            See which Datastreams consume each canonical concept and where coverage or
            conflicts remain.
          </p>
        </div>
        <div className="header-actions">
          {/* TODO(api): conflict-resolution flow */}
          <button className="secondary-button">Resolve conflict</button>
          {/* TODO(api): add-concept flow */}
          <button className="primary-button">+ Add concept</button>
        </div>
      </div>

      <section className="mapping-summary">
        <div>
          <span>Canonical concepts</span>
          <strong>{summary.concepts}</strong>
          <small>
            {summary.dimensions} dimensions · {summary.metrics} metrics
          </small>
        </div>
        <div>
          <span>Fully covered</span>
          {/* TODO(api): coverage state not exposed by /api/datamodel/fields */}
          <strong>{summary.fullyCovered}</strong>
          <small>Across active Datastreams</small>
        </div>
        <div>
          <span>Partial</span>
          {/* TODO(api): partial-coverage tally */}
          <strong>{summary.partial}</strong>
          <small>Missing in at least one flow</small>
        </div>
        <div>
          <span>Conflicts</span>
          {/* TODO(api): conflict tally — see /api/mdm/conflicts (MdmConflictsPage) */}
          <strong style={{ color: "var(--error)" }}>{summary.conflicts}</strong>
          <small>Currency binding</small>
        </div>
      </section>

      <section className="mapping-panel">
        <div className="mapping-toolbar">
          {/* TODO(api): view switch — only "Concept consumption" has a backing
              list today; Coverage matrix + Relationships have no API/logic. */}
          <div className="segmented">
            <button className="segment active">Concept consumption</button>
            <button className="segment">Coverage matrix</button>
            <button className="segment">Relationships</button>
          </div>
          <div className="mapping-filters">
            {/* TODO(api): Kind filter wiring (kind param exists on the API) */}
            <button className="selector">Kind: All</button>
            {/* TODO(api): Status filter — coverage status not in the API */}
            <button className="selector">Status: All</button>
            {/* TODO(api): concept search wiring */}
            <div className="field-control mapping-search">Search concepts</div>
          </div>
        </div>

        <div
          className="table-scroll"
          tabIndex={0}
          aria-label="Scrollable project mapping table"
        >
          <table className="concept-table">
            <thead>
              <tr>
                <th style={{ width: "25%" }}>Canonical concept</th>
                <th style={{ width: "12%" }}>Kind</th>
                <th style={{ width: "25%" }}>Used by</th>
                <th style={{ width: "17%" }}>Coverage</th>
                <th style={{ width: "13%" }}>Conflicts</th>
                <th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.mono}>
                  <td>
                    <div className="concept-name">
                      <span className="concept-icon">{row.icon ?? GENERIC_ICON}</span>
                      <div>
                        <strong>{row.name}</strong>
                        <small className="event">{row.mono}</small>
                      </div>
                    </div>
                  </td>
                  <td>
                    <span className="kind-label">{row.kind}</span>
                  </td>
                  <td>
                    <div className="consumer-cell">
                      <div className="logo-stack">
                        {/* TODO(api): which providers feed a concept is not
                            exposed by /api/datamodel/fields (scalar count only). */}
                        {row.providers.map((p) => (
                          <ProviderLogo key={p} provider={p} />
                        ))}
                      </div>
                      <div>
                        <strong>
                          <span className="number">{row.count}</span> Datastreams
                        </strong>
                        <small>{row.countNote}</small>
                      </div>
                    </div>
                  </td>
                  <td>
                    {/* TODO(api): coverage state literal */}
                    <span className={`signal-label ${row.coverage}`}>
                      <span className="signal-mark" />
                      {row.coverageLabel}
                    </span>
                  </td>
                  <td>
                    {/* TODO(api): conflict state literal */}
                    <span className={`signal-label ${row.conflict}`}>
                      <span className="signal-mark" />
                      {row.conflictLabel}
                    </span>
                  </td>
                  {/* TODO(api): last mapping-update date literal */}
                  <td className="number">{row.updated}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="mapping-footer">
          <span>
            Showing {rows.length} of{" "}
            <span className="number">{summary.concepts}</span> canonical concepts
          </span>
          {/* Drill-down CTA: select a Datastream count to inspect field-level
              mapping. TODO(api): no field-level-by-concept endpoint today. */}
          <strong
            role="button"
            tabIndex={0}
            style={{ cursor: onOpenConcept ? "pointer" : "default" }}
            onClick={() => onOpenConcept?.(rows[0]?.mono ?? "")}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onOpenConcept?.(rows[0]?.mono ?? "");
              }
            }}
          >
            Select a Datastream count to inspect field-level mapping →
          </strong>
        </div>
      </section>
    </div>
  );
}
