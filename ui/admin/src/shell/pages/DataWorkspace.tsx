/**
 * DataWorkspace — faithful React port of the validated Data-workspace mockup.
 *
 * Source of visual truth:
 *   _bmad-output/planning-artifacts/ux-designs/ux-connector-2026-07-23/
 *     mockups/data-workspace.html
 *
 * The application shell (ApplicationShell.tsx) already renders the frame,
 * sidebar (including the Data nav tree), topbar, and <main className="main">.
 * The mockup's left "Data tree" (Data overview / Datastreams / Sources /
 * Imports / Modules) is SIDEBAR NAV and belongs to the shell — NOT this page.
 * This component renders ONLY the page body that lives inside <main>: the page
 * header, the three data-summary cards, and the fleet table with per-data-type
 * groupings.
 *
 * Styling: application.css (global, via the shell) supplies every class used
 * here (data-summary, summary-card, fleet, fleet-table, .type-row, .name,
 * .number, signal-label, provider-logo, page-header, header-actions, .selector,
 * .secondary-button, .action-link/.primary). data-workspace.css is a documented
 * stub — the mockup added no page-specific CSS for this body. Colors come only
 * from the application.css CSS variables; numbers use Geist tabular via .number.
 *
 * Data: the fleet table maps to GET /api/datastreams (the Epic-8/12 read-model,
 * reused by DatastreamOpsList). That endpoint DOES expose name, source_kind,
 * last publication and next_run_at — but NOT the mockup's data-type grouping,
 * the "freshness age" column ("3h"), a published-date distinct from status, or a
 * provider→logo mapping. So the fleet rows are enriched with mockup-literal
 * fields (flagged // TODO(api)) and, when the API is silent, the whole table
 * falls back to the mockup's literal rows so the page renders finished with no
 * backend. The three summary cards have no endpoint at all — mockup literals.
 */
import { useEffect, useState } from "react";
import "../application.css";
import "./data-workspace.css";

/** Provider logo assets that actually exist under public/connectors/. Anything not
 *  in this map has NO asset yet — see the GAP list; we still reference
 *  /connectors/<name>.svg rather than hand-draw a provider glyph. */
const PROVIDER_LOGOS: Record<string, { src: string; alt: string }> = {
  meta: { src: "/connectors/meta.svg", alt: "Meta" },
  "google-ads": { src: "/connectors/google-ads.svg", alt: "Google Ads" },
  "google-analytics": { src: "/connectors/google-analytics.svg", alt: "Google Analytics" },
  "google-sheets": { src: "/connectors/google-sheets.svg", alt: "Google Sheets" },
};

type Signal = "success" | "warning" | "error" | "info";

interface FleetRowVM {
  /** stable key + click-through id */
  id: string;
  /** provider logo key into PROVIDER_LOGOS */
  provider: string;
  name: string;
  dataType: string;
  freshness: string; // e.g. "3h" — TODO(api): no age field on the read-model
  nextRun: string; // e.g. "06:00" | "Retry pending" | "Daily"
  published: string; // e.g. "22 Jul" — TODO(api): distinct published date
  status: { label: string; signal: Signal };
}

interface FleetGroupVM {
  /** the .type-row heading (data type grouping) */
  type: string;
  rows: FleetRowVM[];
}

/**
 * The mockup's literal fleet — the source of visual truth and the no-backend
 * fallback. Grouped by data type with a .type-row heading per group, exactly as
 * the mockup renders it.
 */
const MOCKUP_FLEET: FleetGroupVM[] = [
  {
    type: "Paid media",
    rows: [
      {
        id: "campaign-performance",
        provider: "meta",
        name: "Campaign performance",
        dataType: "Spend",
        freshness: "3h",
        nextRun: "06:00",
        published: "22 Jul",
        status: { label: "Healthy", signal: "success" },
      },
      {
        id: "search-performance",
        provider: "google-ads",
        name: "Search performance",
        dataType: "Spend",
        freshness: "26h",
        nextRun: "Retry pending",
        published: "21 Jul",
        status: { label: "Needs attention", signal: "warning" },
      },
    ],
  },
  {
    type: "Analytics",
    rows: [
      {
        id: "website-acquisition",
        provider: "google-analytics",
        name: "Website acquisition",
        dataType: "Context",
        freshness: "4h",
        nextRun: "06:15",
        published: "22 Jul",
        status: { label: "Healthy", signal: "success" },
      },
    ],
  },
  {
    type: "Files & plans",
    rows: [
      {
        id: "media-plan-2026",
        provider: "google-sheets",
        name: "Media plan 2026",
        dataType: "Forecast & plan",
        freshness: "1d",
        nextRun: "Daily",
        published: "22 Jul",
        status: { label: "Healthy", signal: "success" },
      },
    ],
  },
];

/** The (superset) datastream summary GET /api/datastreams returns. Only the
 *  fields this page reads are declared; the rest are permitted. */
interface DatastreamSummary {
  id: string;
  name: string;
  source_kind?: string | null;
  module_name?: string | null;
  last_pull_state?: string | null;
  next_run_at?: string | null;
  // Epic 42: published execution lifted onto the list row (state/freshness/date).
  published_state?: string | null;
  published_at?: string | null;
  // Epic 42 (migration 093): business data role -> the "Data type" column.
  data_role?: string | null;
  [k: string]: unknown;
}

/** Source family (fleet grouping) derived from the provider logo key. */
function familyOf(provider: string): string {
  if (provider === "meta" || provider === "google-ads") return "Paid media";
  if (provider === "google-analytics") return "Analytics";
  if (provider === "google-sheets") return "Files & plans";
  return "Other";
}
const FAMILY_ORDER = ["Paid media", "Analytics", "Files & plans", "Other"];

/** Relative age of the last publication, e.g. "3h", "1d". "—" when unknown. */
function freshnessOf(ds: DatastreamSummary): string {
  if (!ds.published_at) return "—";
  const then = new Date(ds.published_at).getTime();
  if (Number.isNaN(then)) return "—";
  const mins = Math.max(0, Math.round((Date.now() - then) / 60000));
  if (mins < 60) return `${mins}m`;
  const hours = Math.round(mins / 60);
  if (hours < 48) return `${hours}h`;
  return `${Math.round(hours / 24)}d`;
}

/** Published-through date, e.g. "22 Jul". "—" when unknown. */
function publishedDateOf(ds: DatastreamSummary): string {
  if (!ds.published_at) return "—";
  const d = new Date(ds.published_at);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

/** Map an API source_kind to a provider logo key. Best-effort; unknowns fall
 *  through to a null provider (rendered without a logo). */
function providerOf(ds: DatastreamSummary): string {
  // module_name is the connector (e.g. "google_sheets"); source_kind is only the
  // ingestion MECHANISM (connector_pull/external_bq/managed_feed), so it must not
  // win for provider detection.
  const k = (ds.module_name || ds.source_kind || "").toLowerCase();
  if (k.includes("meta") || k.includes("facebook")) return "meta";
  if (k.includes("google_ads") || k.includes("google-ads") || k.includes("googleads")) return "google-ads";
  if (k.includes("analytics") || k.includes("ga4")) return "google-analytics";
  if (k.includes("sheet")) return "google-sheets";
  return k; // unknown key -> handled at render; listed as a GAP
}

/** Derive a signal + label, preferring the published execution state. */
function statusOf(ds: DatastreamSummary): { label: string; signal: Signal } {
  const s = (ds.published_state || ds.last_pull_state || "").toLowerCase();
  if (["failed", "invalid", "error", "blocked"].includes(s)) {
    return { label: "Needs attention", signal: "warning" };
  }
  if (s === "degraded" || s === "partial") return { label: "Degraded", signal: "warning" };
  if (["running", "loading", "validating", "publishing"].includes(s)) {
    return { label: "Running", signal: "info" };
  }
  return { label: "Healthy", signal: "success" };
}

function nextRunOf(ds: DatastreamSummary): string {
  if (!ds.next_run_at) return "—";
  try {
    return new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit" }).format(
      new Date(ds.next_run_at),
    );
  } catch {
    return String(ds.next_run_at).slice(11, 16) || "—";
  }
}

/** Connection health as returned by GET /api/connections (nested object). Only
 *  the status this page reads is declared; other fields are permitted. */
interface ConnectionHealthVM {
  status?: "ok" | "stale" | "revoked" | string | null;
  [k: string]: unknown;
}

/** A single connection row from GET /api/connections. Response envelope is
 *  { connections: ConnectionSummary[] }. Only the fields this page reads are
 *  declared. */
interface ConnectionSummary {
  id: string;
  health?: ConnectionHealthVM | null;
  [k: string]: unknown;
}

interface ConnectionsResponseVM {
  connections?: ConnectionSummary[];
}

/**
 * Summary-card figures. Every field carries a mockup-literal fallback so the
 * three top cards still render finished when no backend answers. Cards are
 * populated from real sources as each API responds (fleet + connections),
 * independently — a silent /api/connections leaves only "Connected sources"
 * on its literal while the fleet-derived cards still upgrade to real.
 */
interface SummaryVM {
  publishedTrust: string; // "Trusted" | "Attention"
  publishedTrustSignal: Signal;
  publishedThrough: string; // "Complete through 22 Jul 2026"
  activeCount: number;
  fleetHealthNote: string; // "6 healthy · 1 needs attention"
  sourcesCount: string; // e.g. "4"
  sourcesNote: string; // "All authorizations usable" | "N need attention"
}

/** Literal summary — the mockup source of visual truth and the no-backend
 *  fallback for the three top cards. */
const MOCKUP_SUMMARY: SummaryVM = {
  publishedTrust: "Trusted",
  publishedTrustSignal: "success",
  publishedThrough: "Complete through 22 Jul 2026",
  activeCount: MOCKUP_FLEET.reduce((n, g) => n + g.rows.length, 0),
  fleetHealthNote: "6 healthy · 1 needs attention",
  sourcesCount: "4",
  sourcesNote: "All authorizations usable",
};

/** Latest published_at across all API rows, formatted "22 Jul 2026"; null when
 *  no row carries a parseable published_at. */
function latestPublishedThrough(rows: DatastreamSummary[]): string | null {
  let latest = Number.NEGATIVE_INFINITY;
  for (const ds of rows) {
    if (!ds.published_at) continue;
    const t = new Date(ds.published_at).getTime();
    if (!Number.isNaN(t) && t > latest) latest = t;
  }
  if (latest === Number.NEGATIVE_INFINITY) return null;
  return `Complete through ${new Date(latest).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
  })}`;
}

/** Derive the fleet-driven summary fields from the real datastream rows:
 *  active count, published-data trust (Trusted unless any row is non-healthy),
 *  completeness date, and a healthy / needs-attention breakdown note. */
function summaryFromFleet(rows: DatastreamSummary[]): Pick<
  SummaryVM,
  "publishedTrust" | "publishedTrustSignal" | "publishedThrough" | "activeCount" | "fleetHealthNote"
> {
  let healthy = 0;
  let attention = 0;
  for (const ds of rows) {
    if (statusOf(ds).signal === "success") healthy += 1;
    else attention += 1;
  }
  const allTrusted = attention === 0;
  const through = latestPublishedThrough(rows);
  const note =
    attention === 0
      ? `${healthy} healthy`
      : `${healthy} healthy · ${attention} needs attention`;
  return {
    publishedTrust: allTrusted ? "Trusted" : "Attention",
    publishedTrustSignal: allTrusted ? "success" : "warning",
    publishedThrough: through ?? MOCKUP_SUMMARY.publishedThrough,
    activeCount: rows.length,
    fleetHealthNote: note,
  };
}

/** Derive the connected-sources fields from GET /api/connections rows. A source
 *  is "usable" when its health status is "ok" (or absent/unknown, treated as
 *  not-yet-a-problem is too optimistic — we count only "ok" as usable and every
 *  other explicit status as needing attention). */
function summaryFromConnections(rows: ConnectionSummary[]): Pick<
  SummaryVM,
  "sourcesCount" | "sourcesNote"
> {
  let attention = 0;
  for (const c of rows) {
    const status = (c.health?.status ?? "").toString().toLowerCase();
    if (status !== "ok") attention += 1;
  }
  return {
    sourcesCount: String(rows.length),
    sourcesNote: attention === 0 ? "All authorizations usable" : `${attention} need attention`,
  };
}

/** Build the grouped view-model from the API rows. Since the read-model has no
 *  data-type grouping, freshness age, or published-date, every API row lands in
 *  a single "Datastreams" group and borrows mockup-literal cells for the columns
 *  the API cannot answer. */
function fleetFromApi(rows: DatastreamSummary[]): FleetGroupVM[] {
  const byFamily = new Map<string, FleetRowVM[]>();
  for (const ds of rows) {
    const provider = providerOf(ds);
    const family = familyOf(provider);
    const row: FleetRowVM = {
      id: ds.id,
      provider,
      name: ds.name,
      dataType: ds.data_role || "—", // REAL from migration 093 (data_role)
      freshness: freshnessOf(ds),
      nextRun: nextRunOf(ds),
      published: publishedDateOf(ds),
      status: statusOf(ds),
    };
    (byFamily.get(family) ?? byFamily.set(family, []).get(family)!).push(row);
  }
  const groups: FleetGroupVM[] = [];
  for (const type of FAMILY_ORDER) {
    const grp = byFamily.get(type);
    if (grp && grp.length) groups.push({ type, rows: grp });
  }
  return groups;
}

interface DataWorkspaceProps {
  projectId?: string;
  apiBase?: string;
  onOpenDatastream?: (id: string) => void;
  /** Header actions — wired by the shell/router when available. */
  onAddDatastream?: () => void;
  onOpenModules?: () => void;
}

export default function DataWorkspace({
  projectId = "default",
  apiBase = "",
  onOpenDatastream,
  onAddDatastream,
  onOpenModules,
}: DataWorkspaceProps) {
  // Start from the mockup literal so the page renders finished with no backend;
  // swap in the real fleet if /api/datastreams answers with rows.
  const [groups, setGroups] = useState<FleetGroupVM[]>(MOCKUP_FLEET);
  // Summary cards start on the mockup literal and upgrade field-group by
  // field-group as each real source answers (fleet -> cards 1 & 2; connections
  // -> card 3). Each source is independent, so a silent one keeps only its own
  // fields on the literal fallback.
  const [summary, setSummary] = useState<SummaryVM>(MOCKUP_SUMMARY);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const resp = await fetch(
          `${apiBase}/api/datastreams?project_id=${encodeURIComponent(projectId)}`,
        );
        if (!resp.ok) return; // keep the literal fallback on error
        const data = (await resp.json()) as DatastreamSummary[];
        if (cancelled || !Array.isArray(data) || data.length === 0) return;
        setGroups(fleetFromApi(data));
        const fleetSummary = summaryFromFleet(data);
        setSummary((prev) => ({ ...prev, ...fleetSummary }));
      } catch {
        /* keep the literal fallback offline */
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [projectId, apiBase]);

  // Connected-sources card: count + authorization-health note from the real
  // /api/connections list. Independent of the fleet fetch; on error/silence the
  // card keeps its mockup-literal fields.
  useEffect(() => {
    let cancelled = false;
    async function loadConnections() {
      try {
        const resp = await fetch(
          `${apiBase}/api/connections?project_id=${encodeURIComponent(projectId)}`,
        );
        if (!resp.ok) return; // keep the literal fallback on error
        const data = (await resp.json()) as ConnectionsResponseVM;
        const rows = data?.connections;
        if (cancelled || !Array.isArray(rows) || rows.length === 0) return;
        const connSummary = summaryFromConnections(rows);
        setSummary((prev) => ({ ...prev, ...connSummary }));
      } catch {
        /* keep the literal fallback offline */
      }
    }
    void loadConnections();
    return () => {
      cancelled = true;
    };
  }, [projectId, apiBase]);

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Data</h1>
          <p>Sources, Datastreams, imports and optional modules for Acme Growth.</p>
          {/* TODO(api): project name ("Acme Growth") from scope context */}
        </div>
        <div className="header-actions">
          {/* TODO(api): Modules manager surface */}
          <button className="secondary-button" type="button" onClick={() => onOpenModules?.()}>
            Modules
          </button>
          {/* TODO(api): Add-Datastream launches the create wizard */}
          <a
            className="primary-button action-link primary"
            href="#"
            onClick={(e) => {
              e.preventDefault();
              onAddDatastream?.();
            }}
          >
            + Add Datastream
          </a>
        </div>
      </div>

      <section className="data-summary">
        {/* Published-data trust + completeness date derived from the fleet
            (published_state across /api/datastreams). Falls back to the mockup
            literal when the fleet fetch is silent. */}
        <article className="panel summary-card">
          <span>Published data</span>
          <strong>{summary.publishedTrust}</strong>
          <p>{summary.publishedThrough}</p>
        </article>
        <article className="panel summary-card">
          <span>Active Datastreams</span>
          <strong>{summary.activeCount}</strong>
          {/* Healthy / needs-attention breakdown from the real fleet rows. */}
          <p>{summary.fleetHealthNote}</p>
        </article>
        {/* Connected-sources count + authorization health from /api/connections
            (health.status). Falls back to the mockup literal when silent. */}
        <a
          className="panel summary-card"
          href="#"
          onClick={(e) => {
            e.preventDefault();
            // TODO(api): navigate to Sources
          }}
        >
          <span>Connected sources</span>
          <strong>{summary.sourcesCount}</strong>
          <p>{summary.sourcesNote}</p>
        </a>
      </section>

      <section className="panel fleet">
        <div className="fleet-head">
          <h2>Datastreams</h2>
          <div className="fleet-tools">
            {/* TODO(api): status filter — not wired */}
            <button className="selector" type="button">
              All statuses
            </button>
            {/* TODO(api): Datastream search — not wired */}
            <button className="selector" type="button">
              Search Datastreams
            </button>
          </div>
        </div>
        <table className="fleet-table">
          <thead>
            <tr>
              <th>Datastream</th>
              <th>Data type</th>
              <th>Freshness</th>
              <th>Next run</th>
              <th>Published</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {groups.map((group) => (
              <FleetGroup
                key={group.type}
                group={group}
                onOpenDatastream={onOpenDatastream}
              />
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}

function FleetGroup({
  group,
  onOpenDatastream,
}: {
  group: FleetGroupVM;
  onOpenDatastream?: (id: string) => void;
}) {
  return (
    <>
      <tr className="type-row">
        <td colSpan={6}>{group.type}</td>
      </tr>
      {group.rows.map((row) => (
        <tr key={row.id}>
          <td>
            <div className="name">
              <ProviderLogo provider={row.provider} />
              {onOpenDatastream ? (
                <a
                  className="name-link"
                  href="#"
                  onClick={(e) => {
                    e.preventDefault();
                    onOpenDatastream(row.id);
                  }}
                >
                  {row.name}
                </a>
              ) : (
                row.name
              )}
            </div>
          </td>
          <td>{row.dataType}</td>
          <td className="number">{row.freshness}</td>
          <td className="number">{row.nextRun}</td>
          <td className="number">{row.published}</td>
          <td>
            <span className={`signal-label ${row.status.signal}`}>
              <span className="signal-mark" />
              {row.status.label}
            </span>
          </td>
        </tr>
      ))}
    </>
  );
}

function ProviderLogo({ provider }: { provider: string }) {
  const logo = PROVIDER_LOGOS[provider];
  // If a provider has no asset we still reference /connectors/<name>.svg (never
  // hand-draw a provider glyph) — the broken-image case is a listed GAP.
  const src = logo?.src ?? `/connectors/${provider}.svg`;
  const alt = logo?.alt ?? provider;
  return (
    <span className="provider-logo">
      <img src={src} alt={alt} />
    </span>
  );
}
