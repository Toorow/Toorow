/**
 * DataWorkspace — the Data overview surface.
 *
 * Visual lineage:
 *   _bmad-output/planning-artifacts/ux-designs/ux-connector-2026-07-23/
 *     mockups/data-workspace.html
 *
 * The application shell (ApplicationShell.tsx) already renders the frame,
 * sidebar (including the Data nav tree), topbar, and <main className="main">.
 * This component renders ONLY the page body: the page header, the three
 * data-summary cards, and the fleet table grouped by source family.
 *
 * Data:
 *   GET /api/datastreams?project_id=…  -> the fleet rows (Epic-8/12 read-model,
 *       with Epic-42 published_state / published_at and migration-093 data_role)
 *   GET /api/connections?project_id=…  -> the connected-sources card
 *
 * WHAT WAS REMOVED
 * ----------------
 * The page used to open on a literal fleet (Campaign performance, Search
 * performance, Website acquisition, Media plan 2026) and a literal summary that
 * asserted "Published data: Trusted", "Complete through 22 Jul 2026", "6 healthy
 * · 1 needs attention" and "4 connected sources · All authorizations usable".
 * Both were KEPT on `if (!resp.ok) return;` and on an empty array — so a broken
 * API, and a project with no Datastreams at all, both reported a healthy,
 * trusted, fully published fleet. "Published data: Trusted" is the single most
 * consequential claim on this screen and it was a constant.
 *
 * Now each of the two loads carries its own state: a failure is said, an empty
 * list is rendered as empty, and no summary figure is shown for a load that did
 * not succeed. Header/table controls are rendered only when the shell wires a
 * handler for them.
 */
import { useCallback, useEffect, useState } from "react";
import "../application.css";
import "./data-workspace.css";
import { apiFetch } from "../../lib/apiFetch";

/** Provider logo assets that actually exist under public/connectors/. Anything not
 *  in this map has NO asset yet — the row renders without a logo rather than
 *  borrowing another vendor's mark. */
const PROVIDER_LOGOS: Record<string, { src: string; alt: string }> = {
  meta: { src: "/connectors/meta.svg", alt: "Meta" },
  "google-ads": { src: "/connectors/google-ads.png", alt: "Google Ads" },
  "google-analytics": { src: "/connectors/google-analytics.png", alt: "Google Analytics" },
  "google-sheets": { src: "/connectors/google-sheets.png", alt: "Google Sheets" },
};

type Signal = "success" | "warning" | "error" | "info";

interface FleetRowVM {
  id: string;
  provider: string;
  name: string;
  dataType: string;
  freshness: string;
  nextRun: string;
  published: string;
  status: { label: string; signal: Signal };
}

interface FleetGroupVM {
  type: string;
  rows: FleetRowVM[];
}

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

/** Map an API module/source kind to a provider logo key. */
function providerOf(ds: DatastreamSummary): string {
  // module_name is the connector (e.g. "google_sheets"); source_kind is only the
  // ingestion MECHANISM (connector_pull/external_bq/managed_feed), so it must not
  // win for provider detection.
  const k = (ds.module_name || ds.source_kind || "").toLowerCase();
  if (k.includes("meta") || k.includes("facebook")) return "meta";
  if (k.includes("google_ads") || k.includes("google-ads") || k.includes("googleads"))
    return "google-ads";
  if (k.includes("analytics") || k.includes("ga4")) return "google-analytics";
  if (k.includes("sheet")) return "google-sheets";
  return k;
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

/** Connection health as returned by GET /api/connections (nested object). */
interface ConnectionHealthVM {
  status?: "ok" | "stale" | "revoked" | string | null;
  [k: string]: unknown;
}

interface ConnectionSummary {
  id: string;
  health?: ConnectionHealthVM | null;
  [k: string]: unknown;
}

interface ConnectionsResponseVM {
  connections?: ConnectionSummary[];
}

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

/** Build the grouped view-model from the API rows. */
function fleetFromApi(rows: DatastreamSummary[]): FleetGroupVM[] {
  const byFamily = new Map<string, FleetRowVM[]>();
  for (const ds of rows) {
    const provider = providerOf(ds);
    const family = familyOf(provider);
    const row: FleetRowVM = {
      id: ds.id,
      provider,
      name: ds.name,
      dataType: ds.data_role || "—",
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

type FleetState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ok"; rows: DatastreamSummary[]; groups: FleetGroupVM[] };

type ConnectionsState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ok"; rows: ConnectionSummary[] };

interface DataWorkspaceProps {
  projectId?: string;
  apiBase?: string;
  onOpenDatastream?: (id: string) => void;
  /** Header/summary actions — rendered ONLY when the shell wires them. */
  onAddDatastream?: () => void;
  onOpenModules?: () => void;
  onOpenSources?: () => void;
}

export default function DataWorkspace({
  projectId = "default",
  apiBase = "",
  onOpenDatastream,
  onAddDatastream,
  onOpenModules,
  onOpenSources,
}: DataWorkspaceProps) {
  const [fleet, setFleet] = useState<FleetState>({ status: "loading" });
  const [connections, setConnections] = useState<ConnectionsState>({ status: "loading" });

  const loadFleet = useCallback(async () => {
    setFleet({ status: "loading" });
    try {
      const resp = await apiFetch(
        `${apiBase}/api/datastreams?project_id=${encodeURIComponent(projectId)}`,
      );
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = (await resp.json()) as DatastreamSummary[];
      const rows = Array.isArray(data) ? data : [];
      setFleet({ status: "ok", rows, groups: fleetFromApi(rows) });
    } catch (err) {
      setFleet({ status: "error", message: err instanceof Error ? err.message : String(err) });
    }
  }, [projectId, apiBase]);

  const loadConnections = useCallback(async () => {
    setConnections({ status: "loading" });
    try {
      const resp = await apiFetch(
        `${apiBase}/api/connections?project_id=${encodeURIComponent(projectId)}`,
      );
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = (await resp.json()) as ConnectionsResponseVM;
      setConnections({ status: "ok", rows: Array.isArray(data?.connections) ? data.connections : [] });
    } catch (err) {
      setConnections({
        status: "error",
        message: err instanceof Error ? err.message : String(err),
      });
    }
  }, [projectId, apiBase]);

  useEffect(() => {
    void loadFleet();
  }, [loadFleet]);

  useEffect(() => {
    void loadConnections();
  }, [loadConnections]);

  // ---- Summary figures: computed ONLY from a successful read ---------------
  let publishedTrust = "—";
  let publishedTrustSignal: Signal = "info";
  let publishedThrough = "Loading…";
  let activeCount = "—";
  let fleetHealthNote = "Loading…";

  if (fleet.status === "error") {
    publishedThrough = "Not available — the fleet could not be loaded";
    fleetHealthNote = "Not available — the fleet could not be loaded";
  } else if (fleet.status === "ok") {
    const healthy = fleet.rows.filter((ds) => statusOf(ds).signal === "success").length;
    const attention = fleet.rows.length - healthy;
    activeCount = String(fleet.rows.length);
    if (fleet.rows.length === 0) {
      publishedTrust = "No data";
      publishedTrustSignal = "info";
      publishedThrough = "No Datastream publishes into this project yet";
      fleetHealthNote = "No Datastream configured";
    } else {
      publishedTrust = attention === 0 ? "Trusted" : "Attention";
      publishedTrustSignal = attention === 0 ? "success" : "warning";
      publishedThrough =
        latestPublishedThrough(fleet.rows) ?? "No publication recorded yet";
      fleetHealthNote =
        attention === 0 ? `${healthy} healthy` : `${healthy} healthy · ${attention} need attention`;
    }
  }

  let sourcesCount = "—";
  let sourcesNote = "Loading…";
  if (connections.status === "error") {
    sourcesNote = "Not available — the authorizations could not be loaded";
  } else if (connections.status === "ok") {
    sourcesCount = String(connections.rows.length);
    const attention = connections.rows.filter(
      (c) => (c.health?.status ?? "").toString().toLowerCase() !== "ok",
    ).length;
    sourcesNote =
      connections.rows.length === 0
        ? "No source connected yet"
        : attention === 0
          ? "All authorizations usable"
          : `${attention} need attention`;
  }

  const groups = fleet.status === "ok" ? fleet.groups : [];

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Data</h1>
          <p>Sources, Datastreams, imports and optional modules for this project.</p>
        </div>
        {(onOpenModules || onAddDatastream) && (
          <div className="header-actions">
            {onOpenModules && (
              <button className="secondary-button" type="button" onClick={() => onOpenModules()}>
                Modules
              </button>
            )}
            {onAddDatastream && (
              <button className="primary-button" type="button" onClick={() => onAddDatastream()}>
                + Add Datastream
              </button>
            )}
          </div>
        )}
      </div>

      <section className="data-summary">
        <article className="panel summary-card">
          <span>Published data</span>
          <strong className={`dw-trust dw-trust-${publishedTrustSignal}`}>{publishedTrust}</strong>
          <p>{publishedThrough}</p>
        </article>
        <article className="panel summary-card">
          <span>Active Datastreams</span>
          <strong>{activeCount}</strong>
          <p>{fleetHealthNote}</p>
        </article>
        {onOpenSources ? (
          <button
            className="panel summary-card"
            type="button"
            onClick={() => onOpenSources()}
          >
            <span>Connected sources</span>
            <strong>{sourcesCount}</strong>
            <p>{sourcesNote}</p>
          </button>
        ) : (
          <article className="panel summary-card">
            <span>Connected sources</span>
            <strong>{sourcesCount}</strong>
            <p>{sourcesNote}</p>
          </article>
        )}
      </section>

      <section className="panel fleet">
        <div className="fleet-head">
          <h2>Datastreams</h2>
        </div>

        {fleet.status === "loading" && (
          <p className="dw-status" role="status">
            Loading the Datastreams…
          </p>
        )}

        {fleet.status === "error" && (
          <div className="dw-load-error" role="alert">
            <span className="signal-label error">
              <span className="signal-mark" />
              Could not load the Datastreams
            </span>
            <p>
              {fleet.message}. No Datastream is being listed and no figure above is being
              reported — this is a loading failure, not an empty project.
            </p>
            <button className="secondary-button" type="button" onClick={() => void loadFleet()}>
              Retry
            </button>
          </div>
        )}

        {fleet.status === "ok" && groups.length === 0 && (
          <p className="dw-status">
            No Datastream is configured in this project yet.
          </p>
        )}

        {fleet.status === "ok" && groups.length > 0 && (
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
                <FleetGroup key={group.type} group={group} onOpenDatastream={onOpenDatastream} />
              ))}
            </tbody>
          </table>
        )}
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
                <button
                  className="name-link"
                  type="button"
                  onClick={() => onOpenDatastream(row.id)}
                >
                  {row.name}
                </button>
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
  // No asset for this provider: render no logo rather than a broken image or
  // another vendor's mark.
  if (!logo) return null;
  return (
    <span className="provider-logo">
      <img src={logo.src} alt={logo.alt} />
    </span>
  );
}
