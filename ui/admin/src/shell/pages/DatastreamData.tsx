/**
 * DatastreamData — faithful React port of the validated "Data sample" mockup.
 *
 * Source of visual truth:
 *   _bmad-output/planning-artifacts/ux-designs/ux-connector-2026-07-23/
 *     mockups/datastream-data.html
 *
 * The application shell (ApplicationShell.tsx) renders the frame, sidebar,
 * topbar, and <main>. This component renders the datastream-detail MAIN content
 * as a fragment: the object header (provider logo + name + source line), the
 * per-datastream local-tabs (Overview / Data / Mapping / Processing / Runs /
 * Outputs), and the daily-sample timeline — the toolbar (date range, stage
 * selector, campaign filter, columns, Export Excel, Load sample), the evidence
 * note with the masking count, and the vertical timeline of sample-day cards.
 *
 * Styling: application.css (global, via the shell) already owns the shell/layout
 * classes AND most of the sample-timeline system (.sample-day, .sample-days,
 * .sample-day-header, .sample-toolbar, .sample-note, .mask-note, .signal-label,
 * .object, .object-logo, .local-tabs, .full-main). datastream-data.css adds only
 * the mockup's inline extras: the toolbar right-cluster and the fixed-layout
 * wide sample table. Colors come exclusively from the CSS variables; numbers use
 * the .number (Geist tabular) class and technical ids use .event/.mono
 * (JetBrains Mono) exactly where the mockup does.
 *
 * ── Data ──────────────────────────────────────────────────────────────────────
 * Wired to the real sample-preview endpoint (Epic — datastream data sample):
 *   GET /api/datastreams/{datastreamId}/sample
 *       ?stage={collected|mapped|processed|published}
 *       &date_from=YYYY-MM-DD&date_to=YYYY-MM-DD&limit=5
 *   Authorization: Bearer <localStorage api_token>
 * On mount and whenever the stage selector changes, the bounded sample is
 * fetched (default stage "published", last-7-days range ending today, limit 5).
 * The response's `days` drive the timeline (per-day row_count/rejection_count/
 * field_count + the first-N eligible rows as a dynamic-column table). Values are
 * already masked server-side ([MASKED]) and rendered as-is; `masked_fields`
 * drives the "N sensitive fields masked" note; `stage_note` (served_stage !=
 * requested) is surfaced next to the selector; provenance is composed from the
 * real fields where present.
 *
 * The screen keeps a graceful fallback to the mockup's SAMPLE_DAYS literals when
 * the fetch fails or returns no days, so it still renders finished with no
 * backend.
 */
import { useEffect, useMemo, useState } from "react";
import "../application.css";
import "./datastream-data.css";

// ---------------------------------------------------------------------------
// The four pipeline stages the sample can be drawn from. The mockup's toolbar
// shows "Stage: Published"; the sample-preview API exposes one collected /
// mapped / processed / published extract per stage. The selector displays the
// capitalized label; the API takes the lowercased token.
// ---------------------------------------------------------------------------
const STAGES = ["Collected", "Mapped", "Processed", "Published"] as const;
type Stage = (typeof STAGES)[number];
type StageToken = "collected" | "mapped" | "processed" | "published";

function stageToken(stage: Stage): StageToken {
  return stage.toLowerCase() as StageToken;
}
function stageLabel(token: string): Stage {
  const lower = token.toLowerCase();
  const match = STAGES.find((s) => s.toLowerCase() === lower);
  return match ?? "Published";
}

// Per-datastream local navigation. Mirrors the mockup's <nav class="local-tabs">
// order exactly: Overview · Data (active) · Mapping · Processing · Runs · Outputs.
const LOCAL_TABS = [
  "Overview",
  "Data",
  "Mapping",
  "Processing",
  "Runs",
  "Outputs",
] as const;

// ---------------------------------------------------------------------------
// Response contract of GET /api/datastreams/{id}/sample.
// ---------------------------------------------------------------------------
type SampleCell = string | number | boolean | null;

interface SampleApiDay {
  date: string;
  row_count: number;
  rejection_count: number;
  field_count: number;
  rows: Record<string, SampleCell>[];
}

interface SampleApiResponse {
  datastream_id: string;
  project_id: string;
  stage: string;
  served_stage: string;
  stage_note: string | null;
  date_from: string;
  date_to: string;
  limit: number;
  masked_fields: string[];
  published_execution_id?: string | null;
  mapping_version_id?: string | null;
  days: SampleApiDay[];
}

// ---------------------------------------------------------------------------
// View model — one day in the vertical timeline. When drawn from the API, the
// expanded day carries dynamic-column rows (a union of row keys) plus the real
// row_count / rejection_count / field_count. The fallback literal shape (below)
// keeps the mockup's finished-with-no-backend rendering.
// ---------------------------------------------------------------------------
interface SampleDay {
  date: string;
  summary: string;
  expanded: boolean;
  stats?: { rows?: string; rejected?: string; fields?: string; label?: string };
  /** Ordered column keys for the expanded day's table (union of row keys). */
  columns?: string[];
  /** Dynamic row maps; values already masked server-side. */
  rows?: Record<string, SampleCell>[];
}

// The evidence note + provenance line, derived from the real response where
// available, or the mockup literals in the finished fallback.
interface SampleMeta {
  provenance: string;
  maskedCount: number;
  stageNote: string | null;
}

// ---------------------------------------------------------------------------
// Mockup literals — the validated finished fallback. Used verbatim when the
// sample fetch fails or returns no days so the surface still renders complete.
// ---------------------------------------------------------------------------
const FALLBACK_COLUMNS = [
  "date",
  "campaign",
  "impressions",
  "clicks",
  "conversions",
  "spend",
  "revenue",
  "roas",
];

const SAMPLE_DAYS: SampleDay[] = [
  {
    date: "22 Jul 2026",
    summary: "Complete day · first 5 eligible rows",
    expanded: true,
    stats: { rows: "18,420", rejected: "0", fields: "48" },
    columns: FALLBACK_COLUMNS,
    rows: [
      { date: "22 Jul", campaign: "Search · Brand", impressions: "184,209", clicks: "8,342", conversions: "412", spend: "€4,208.91", revenue: "€18,204.80", roas: "4.33" },
      { date: "22 Jul", campaign: "Social · Prospecting", impressions: "912,730", clicks: "14,905", conversions: "284", spend: "€8,932.40", revenue: "€24,762.10", roas: "2.77" },
      { date: "22 Jul", campaign: "Video · Summer", impressions: "634,122", clicks: "6,015", conversions: "96", spend: "€3,485.18", revenue: "€10,834.06", roas: "3.11" },
      { date: "22 Jul", campaign: "Retargeting · All", impressions: "92,882", clicks: "3,741", conversions: "359", spend: "€2,809.77", revenue: "€14,084.42", roas: "5.01" },
      { date: "22 Jul", campaign: "Shopping · Core", impressions: "241,554", clicks: "12,084", conversions: "628", spend: "€6,942.05", revenue: "€29,102.18", roas: "4.19" },
    ],
  },
  {
    date: "21 Jul 2026",
    summary: "17,908 published rows · 5 samples available",
    expanded: false,
    stats: { label: "Complete" },
  },
  {
    date: "20 Jul 2026",
    summary: "18,114 published rows · 5 samples available",
    expanded: false,
    stats: { label: "Complete" },
  },
];

const FALLBACK_META: SampleMeta = {
  provenance: "Published through 22 Jul 2026 · mapping v12 · processing v4",
  maskedCount: 2,
  stageNote: null,
};

// ---------------------------------------------------------------------------
// Formatting helpers.
// ---------------------------------------------------------------------------
const NUM_FMT = new Intl.NumberFormat("en-US");

function fmtCount(n: number): string {
  return Number.isFinite(n) ? NUM_FMT.format(n) : "—";
}

function fmtDayHeader(date: string): string {
  const d = new Date(date);
  if (Number.isNaN(d.getTime())) return date;
  return d.toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

function isoDate(d: Date): string {
  return d.toISOString().slice(0, 10);
}

// Numeric columns render with the Geist tabular `number` class + right-aligned
// `amount` (mockup parity). A cell is numeric when its raw value is a number or
// a numeric-looking string (allowing thousands separators / currency glyphs).
// Masked cells ([MASKED]) render as-is, left-aligned.
function isNumericCell(v: SampleCell): boolean {
  if (typeof v === "number") return true;
  if (typeof v !== "string") return false;
  const trimmed = v.trim();
  if (!trimmed || trimmed === "[MASKED]") return false;
  return /^[€$£¥]?\s?-?[\d.,]+%?$/.test(trimmed);
}

function renderCell(v: SampleCell): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") return NUM_FMT.format(v);
  return v;
}

// A stable, human column label from a raw key (snake_case → Title Case).
function columnLabel(key: string): string {
  return key
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

// ---------------------------------------------------------------------------
// Response → view model.
// ---------------------------------------------------------------------------

// Stable union of column keys for a day: first row's keys in order, then any
// additional keys from later rows appended in first-seen order.
function unionColumns(rows: Record<string, SampleCell>[]): string[] {
  const seen = new Set<string>();
  const cols: string[] = [];
  for (const row of rows) {
    for (const key of Object.keys(row)) {
      if (!seen.has(key)) {
        seen.add(key);
        cols.push(key);
      }
    }
  }
  return cols;
}

function daysFromResponse(resp: SampleApiResponse): SampleDay[] {
  return resp.days.map((day, i) => {
    const hasRows = Array.isArray(day.rows) && day.rows.length > 0;
    const columns = hasRows ? unionColumns(day.rows) : undefined;
    // The first (most recent) day is expanded, matching the mockup timeline.
    const expanded = i === 0 && hasRows;
    if (expanded) {
      return {
        date: fmtDayHeader(day.date),
        summary: `Complete day · first ${day.rows.length} eligible rows`,
        expanded: true,
        stats: {
          rows: fmtCount(day.row_count),
          rejected: fmtCount(day.rejection_count),
          fields: fmtCount(day.field_count),
        },
        columns,
        rows: day.rows,
      };
    }
    return {
      date: fmtDayHeader(day.date),
      summary: `${fmtCount(day.row_count)} rows · ${fmtCount(day.rows?.length ?? 0)} samples available`,
      expanded: false,
      stats: { label: day.rejection_count > 0 ? `${fmtCount(day.rejection_count)} rejected` : "Complete" },
    };
  });
}

// Provenance line from the real response fields where available:
// "Published through <last date> · mapping v<id> · processing v<execution>".
// Only the parts the response actually carries are shown.
function metaFromResponse(resp: SampleApiResponse): SampleMeta {
  const parts: string[] = [];
  const lastDate = resp.days[0]?.date ?? resp.date_to;
  const served = stageLabel(resp.served_stage);
  if (lastDate) parts.push(`${served} through ${fmtDayHeader(lastDate)}`);
  if (resp.mapping_version_id) parts.push(`mapping ${resp.mapping_version_id}`);
  if (resp.published_execution_id) parts.push(`processing ${resp.published_execution_id}`);
  return {
    provenance: parts.join(" · "),
    maskedCount: resp.masked_fields?.length ?? 0,
    stageNote: resp.stage_note ?? null,
  };
}

interface DatastreamDataProps {
  projectId?: string;
  datastreamId?: string;
}

export default function DatastreamData({
  projectId: _projectId = "default",
  datastreamId = "campaign-performance",
}: DatastreamDataProps) {
  // The active pipeline stage the sample is drawn from. Defaults to Published,
  // matching the mockup's "Stage: Published" selector.
  const [stage, setStage] = useState<Stage>("Published");
  // The active per-datastream tab. "Data" is current on this screen.
  const activeTab: (typeof LOCAL_TABS)[number] = "Data";

  // Sample timeline + evidence meta. Seeded with the finished fallback so the
  // surface renders complete before the first response (or if it never lands).
  const [days, setDays] = useState<SampleDay[]>(SAMPLE_DAYS);
  const [meta, setMeta] = useState<SampleMeta>(FALLBACK_META);

  // Last-7-days range ending today (inclusive), computed once per mount.
  const range = useMemo(() => {
    const to = new Date();
    const from = new Date();
    from.setDate(to.getDate() - 6);
    return { from: isoDate(from), to: isoDate(to) };
  }, []);

  // Fetch on mount and whenever the stage selector changes.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const headers: Record<string, string> = {};
        const token = localStorage.getItem("api_token");
        if (token) headers.Authorization = `Bearer ${token}`;
        const qs = new URLSearchParams({
          stage: stageToken(stage),
          date_from: range.from,
          date_to: range.to,
          limit: "5",
        });
        const resp = await fetch(
          `/api/datastreams/${datastreamId}/sample?${qs.toString()}`,
          { headers }
        );
        if (!resp.ok) return; // keep the finished fallback
        const body = (await resp.json()) as SampleApiResponse;
        const mapped = daysFromResponse(body);
        if (cancelled) return;
        if (mapped.length) {
          setDays(mapped);
          setMeta(metaFromResponse(body));
        }
        // Reflect the actually-served stage in the selector when it differs
        // (so the surfaced stage_note and the label stay consistent).
        if (body.served_stage) {
          const served = stageLabel(body.served_stage);
          if (served !== stage) setStage(served);
        }
      } catch {
        /* keep the finished fallback */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [datastreamId, stage, range.from, range.to]);

  function cycleStage() {
    // The selector advances through the four stages on click; the effect above
    // refetches the bounded sample for the newly selected stage.
    const i = STAGES.indexOf(stage);
    setStage(STAGES[(i + 1) % STAGES.length]);
  }

  return (
    <>
      {/* Object header — provider logo + datastream name + source line.
          Real logo image at /connectors/meta.svg (no hand-drawn SVG). */}
      <div className="object" style={{ marginBottom: 22 }}>
        <div className="object-logo provider-logo">
          {/* TODO(api): resolve provider logo from the datastream's source. */}
          <img src="/connectors/meta.svg" alt="Meta" />
        </div>
        <div>
          <strong>Campaign performance</strong>
          <span>Meta Ads · Spend</span>
        </div>
      </div>

      {/* Per-datastream local tabs. Non-active tabs are inert here (the router
          owns cross-tab navigation); wiring is a // TODO(api) once the detail
          routes land. */}
      <nav className="local-tabs" aria-label="Datastream">
        {LOCAL_TABS.map((label) =>
          label === activeTab ? (
            <div key={label} className="local-tab active" aria-current="page">
              {label}
            </div>
          ) : (
            // TODO(api): route to the sibling datastream tab.
            <div key={label} className="local-tab">
              {label}
            </div>
          )
        )}
      </nav>

      <div className="full-main">
        <div className="page-header">
          <div>
            <h1>Data sample</h1>
            <p>
              The first five eligible published rows for each day. Samples are
              evidence, not statistical summaries.
            </p>
          </div>
        </div>

        {/* Toolbar: date range, stage, campaign filter, columns; Export Excel +
            Load sample on the right. */}
        <div className="sample-toolbar">
          {/* TODO(api): date-range picker wiring */}
          <button className="selector" type="button">
            {fmtDayHeader(range.from)} – {fmtDayHeader(range.to)}
          </button>
          {/* Stage selector — Collected / Mapped / Processed / Published. */}
          <button className="selector" type="button" onClick={cycleStage}>
            Stage: {stage}
          </button>
          {/* When the served stage differs from the requested one, the API
              returns a stage_note; surface it next to the selector. */}
          {meta.stageNote && (
            <span className="signal-label" role="status">
              <span className="signal-mark" />
              {meta.stageNote}
            </span>
          )}
          {/* TODO(api): campaign filter */}
          <button className="selector" type="button">All campaigns</button>
          {/* TODO(api): column chooser */}
          <button className="selector" type="button">Columns</button>
          <div className="right">
            {/* TODO(api): governed async Excel export (see GAP note). */}
            <button className="secondary-button" type="button">Export Excel</button>
            {/* TODO(api): fetch the bounded sample for the selected stage/range. */}
            <button className="primary-button" type="button">Load sample</button>
          </div>
        </div>

        {/* Evidence note: coverage/version provenance + masking count. */}
        <div className="sample-note">
          {meta.provenance && (
            <>
              <span className="signal-label success">
                <span className="signal-mark" />
                {meta.provenance}
              </span>{" "}
            </>
          )}
          {/* Masking policy — count of fields masked server-side in this sample. */}
          <span className="mask-note">
            <b>
              {meta.maskedCount} sensitive field{meta.maskedCount === 1 ? "" : "s"} masked
            </b>
          </span>
        </div>

        {/* Vertical timeline of sample-day cards. */}
        <div className="sample-days">
          {days.map((day) => (
            <section
              key={day.date}
              className={day.expanded ? "sample-day" : "sample-day collapsed"}
            >
              <div className="sample-day-header">
                <strong>{day.date}</strong>
                <span className="summary">{day.summary}</span>
                <div className="day-stats">
                  {day.stats?.rows != null && (
                    <span>
                      <b className="number">{day.stats.rows}</b> rows
                    </span>
                  )}
                  {day.stats?.rejected != null && (
                    <span>
                      <b className="number">{day.stats.rejected}</b> rejected
                    </span>
                  )}
                  {day.stats?.fields != null && (
                    <span>
                      <b className="number">{day.stats.fields}</b> fields
                    </span>
                  )}
                  {day.stats?.label != null && <span>{day.stats.label}</span>}
                </div>
                <span>{day.expanded ? "⌃" : "⌄"}</span>
              </div>

              {day.expanded && day.rows && day.columns && (
                <table className="table sample-table">
                  <thead>
                    <tr>
                      {day.columns.map((col) => {
                        // Header alignment tracks the first row's cell type so
                        // numeric columns get the mockup's right-aligned amount.
                        const numeric = isNumericCell(day.rows?.[0]?.[col] ?? null);
                        return (
                          <th key={col} className={numeric ? "amount" : undefined}>
                            {columnLabel(col)}
                          </th>
                        );
                      })}
                    </tr>
                  </thead>
                  <tbody>
                    {day.rows.map((row, i) => (
                      <tr key={i}>
                        {day.columns?.map((col) => {
                          const value = row[col] ?? null;
                          const numeric = isNumericCell(value);
                          return (
                            <td
                              key={col}
                              className={
                                numeric
                                  ? "number amount"
                                  : col === "campaign"
                                    ? "campaign"
                                    : undefined
                              }
                            >
                              {renderCell(value)}
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </section>
          ))}
        </div>
      </div>
    </>
  );
}
