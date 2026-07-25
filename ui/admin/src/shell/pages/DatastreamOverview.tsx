/**
 * DatastreamOverview — faithful React port of the validated
 * Datastream-health-overview mockup.
 *
 * Source of visual truth:
 *   _bmad-output/planning-artifacts/ux-designs/ux-connector-2026-07-23/
 *     mockups/datastream-overview.html
 *
 * The application shell (ApplicationShell.tsx) renders the frame, sidebar,
 * topbar, and <main className="main">. This mockup, unlike project-overview,
 * shows an OBJECT HEADER in the topbar (the datastream identity: provider logo
 * + "Campaign performance" / "Meta Ads · Spend") and a LOCAL-TABS bar
 * (Overview · Data · Mapping · Processing · Runs · Outputs) directly above the
 * page body. Per the port brief this component renders the FULL main content of
 * the mockup — including that object header and local-tabs bar — as a fragment
 * inside the shell <main>, so the screen reads faithfully on its own. The shell
 * classes (.local-tabs, .object, .full-main, .signal-label, .detail-strip,
 * .health-*) are all global in application.css.
 *
 * Styling: application.css (global, via the shell) supplies the shell/layout +
 * health classes; datastream-overview.css adds only the mockup's page-specific
 * inline <style> not already in application.css (health-legend, lower-grid /
 * detail-panel — kept for parity even though this screen renders the
 * detail-STRIP variant). Colors come exclusively from application.css CSS
 * variables; numbers use Geist tabular via those classes. The two translucent
 * legend swatch fills are copied verbatim from the mockup.
 *
 * Data: the operational values map to GET /api/datastreams/{id}/read-model (the
 * Story-12.14 versioned read-model, same endpoint DatastreamOpsDetail drives).
 * That read-model DOES expose the published execution (row_count = "Latest
 * volume"), its freshness signal (state_changed_at), the current published +
 * mapping version ids ("Current versions"), and the recent-imports ledger. It
 * does NOT expose the 30-day health-chart series, the expected-range band, the
 * anomaly evidence, a run-success denominator, next-run scheduling, the
 * completeness / schema-stability scores, nor a distinct last-known-good date —
 * see the GAP LIST in the port report. Every such value is the mockup's literal,
 * flagged // TODO(api), and the whole screen falls back to those literals so it
 * renders finished with NO backend.
 */
import { useEffect, useRef, useState } from "react";
import type {
  DatastreamReadModel,
  PublishedExecution,
} from "../../datastreams/ops/opsTypes";
import "../application.css";
import "./datastream-overview.css";
import { apiFetch } from "../../lib/apiFetch";

/** Provider logo assets that actually exist under public/connectors/. Anything not
 *  in this map has NO asset yet — we reference /connectors/<name>.svg rather than
 *  hand-draw a provider glyph (none missing for this screen). */
const PROVIDER_LOGOS: Record<string, { src: string; alt: string }> = {
  meta: { src: "/connectors/meta.svg", alt: "Meta" },
  "google-ads": { src: "/connectors/google-ads.svg", alt: "Google Ads" },
  "google-analytics": { src: "/connectors/google-analytics.svg", alt: "Google Analytics" },
  "google-sheets": { src: "/connectors/google-sheets.svg", alt: "Google Sheets" },
};

interface DatastreamOverviewProps {
  projectId?: string;
  datastreamId?: string;
}

type FetchState =
  | { status: "loading" }
  | { status: "ok"; model: DatastreamReadModel }
  | { status: "error" };

/** Read-model fetch, mirroring opsApi's convention (project_id in the query,
 *  no-store). Kept local so this shell page has no MUI/ops-ctx dependency; a
 *  failure is swallowed to the mockup fallback (screen must render with no
 *  backend). */
async function fetchReadModel(
  datastreamId: string,
  projectId: string,
): Promise<DatastreamReadModel> {
  const params = new URLSearchParams({ project_id: projectId });
  const resp = await apiFetch(
    `/api/datastreams/${encodeURIComponent(datastreamId)}/read-model?${params.toString()}`,
    { cache: "no-store" },
  );
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json() as Promise<DatastreamReadModel>;
}

/** Freshness age from the published execution's last state change, e.g. "3h".
 *  Returns null when the API is silent so the mockup literal shows instead. */
function freshnessAge(pub: PublishedExecution | null): string | null {
  const iso = pub?.state_changed_at;
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return null;
  const mins = Math.max(0, Math.round((Date.now() - then) / 60_000));
  if (mins < 60) return `${mins}m`;
  const hours = Math.round(mins / 60);
  if (hours < 48) return `${hours}h`;
  return `${Math.round(hours / 24)}d`;
}

/** e.g. 18420 -> "18,420" (tabular). */
function groupNum(n: number | null | undefined): string | null {
  if (n == null) return null;
  return n.toLocaleString("en-US");
}

/** ISO -> "22 Jul 2026 · 03:12" (mockup "Published through" format). Returns
 *  null when the API is silent so the mockup literal shows instead. */
function publishedThrough(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const day = d.toLocaleDateString("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
  const time = d.toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
  return `${day} · ${time}`;
}

/** Run success "N / M" derived from the recent-imports ledger: succeeded rows
 *  over total rows in the returned window. Returns null when the ledger is empty
 *  (nothing to count) so the mockup literal shows instead. */
function runSuccess(
  imports: readonly { status?: string | null }[] | null | undefined,
): { label: string; total: number; succeeded: number } | null {
  if (!imports || imports.length === 0) return null;
  const total = imports.length;
  const succeeded = imports.filter(
    (r) => (r.status ?? "").toLowerCase() === "succeeded",
  ).length;
  return { label: `${succeeded} / ${total}`, total, succeeded };
}

/** Overall health signal derived from the published execution state. Maps to the
 *  mockup's three .signal-label tones. Returns null when state is absent so the
 *  mockup literal (Healthy) shows instead. */
function healthSignal(
  state: string | null | undefined,
): { label: string; tone: "success" | "warning" | "error" } | null {
  switch ((state ?? "").toLowerCase()) {
    case "succeeded":
      return { label: "Healthy", tone: "success" };
    case "degraded":
      return { label: "Degraded", tone: "warning" };
    case "failed":
      return { label: "Needs attention", tone: "error" };
    default:
      return null;
  }
}

export default function DatastreamOverview({
  projectId = "default",
  datastreamId = "campaign-performance",
}: DatastreamOverviewProps) {
  const [state, setState] = useState<FetchState>({ status: "loading" });
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const model = await fetchReadModel(datastreamId, projectId);
        if (!cancelled) setState({ status: "ok", model });
      } catch {
        // Graceful fallback: render the mockup literals with no backend.
        if (!cancelled) setState({ status: "error" });
      }
    }
    setState({ status: "loading" });
    void load();
    intervalRef.current = setInterval(() => void load(), 60_000);
    return () => {
      cancelled = true;
      if (intervalRef.current !== null) clearInterval(intervalRef.current);
    };
  }, [projectId, datastreamId]);

  const model = state.status === "ok" ? state.model : null;
  const pub = model?.published_execution ?? null;

  // ---- Real maps from the read-model (fall back to the mockup literal) ------
  // Latest volume = published execution row_count.
  const latestVolume = groupNum(pub?.row_count) ?? "18,420"; // TODO(api): expected-range check
  // Freshness = age since the published execution's last state change.
  const freshness = freshnessAge(pub) ?? "3h"; // TODO(api): target threshold config
  // Current versions = published plan + mapping version ids. The read-model
  // exposes version *ids*; the mockup shows human labels (plan_v12 · mapping_v8),
  // which we derive from version_number when the joined rows are present.
  const planVersion = model?.plan_versions?.find(
    (p) => p.id === pub?.plan_version_id,
  );
  const mappingVersion = model?.mapping_versions?.find(
    (m) => m.id === pub?.mapping_version_id,
  );
  const versionsLabel =
    planVersion?.version_number != null && mappingVersion?.version_number != null
      ? `plan_v${planVersion.version_number} · mapping_v${mappingVersion.version_number}`
      : "plan_v12 · mapping_v8"; // TODO(api): human labels for published versions
  // Published through = the live execution's last state-change timestamp.
  const publishedThroughLabel =
    publishedThrough(pub?.state_changed_at) ?? "22 Jul 2026 · 03:12";
  // Run success = succeeded vs total in the returned recent-imports window.
  const success = runSuccess(model?.recent_imports);
  const runSuccessLabel = success?.label ?? "119 / 120"; // TODO(api): 30-day window denominator (server returns a bounded ledger)
  const runSuccessRate =
    success != null && success.total > 0
      ? `${((success.succeeded / success.total) * 100).toFixed(1)}% · last ${success.total} runs`
      : "99.2% · last 30 days"; // TODO(api): true 30-day rate (needs full-window counts)
  // Overall health signal = published execution state -> tone + label.
  const health = healthSignal(pub?.state) ?? { label: "Healthy", tone: "success" as const }; // TODO(api): degraded/failed evidence beyond state

  return (
    <>
      {/* Object header — datastream identity (topbar in the mockup). Rendered
          here so the screen reads faithfully as a self-contained fragment. */}
      <div className="topbar-left ds-object-header">
        <div className="object">
          <div className="object-logo provider-logo">
            <img src={PROVIDER_LOGOS.meta.src} alt={PROVIDER_LOGOS.meta.alt} />
          </div>
          <div>
            {/* TODO(api): datastream display name + "<source> · <kind>" subtitle */}
            <strong>Campaign performance</strong>
            <span>Meta Ads · Spend</span>
          </div>
        </div>
      </div>

      {/* Local tabs — datastream sections. */}
      <nav className="local-tabs" aria-label="Datastream">
        <a className="local-tab active" href="#overview" aria-current="page">
          Overview
        </a>
        <a className="local-tab" href="#data">
          Data
        </a>
        <a className="local-tab" href="#mapping">
          Mapping
        </a>
        {/* TODO(api): Processing tab target (no route yet) */}
        <div className="local-tab">Processing</div>
        <a className="local-tab" href="#runs">
          Runs
        </a>
        {/* TODO(api): Outputs tab target (no route yet) */}
        <div className="local-tab">Outputs</div>
      </nav>

      <div className="full-main">
        <div className="page-header">
          <div>
            <h1>Datastream health</h1>
            <p>
              Freshness, reliability, completeness, and mapping health for Campaign
              performance.
            </p>
          </div>
          <div className="header-actions">
            {/* Overall health signal ← published_execution.state; falls back to
                the mockup literal (Healthy) when the read-model is silent. */}
            <span className={`signal-label ${health.tone}`}>
              <span className="signal-mark" />
              {health.label}
            </span>
            <a className="secondary-button action-link" href="#mapping">
              Configure mapping
            </a>
            <a className="primary-button action-link primary" href="#data">
              View published data
            </a>
          </div>
        </div>

        <section className="metric-strip">
          <div className="metric">
            <span>Freshness</span>
            <strong>{freshness}</strong>
            {/* TODO(api): freshness target ("under 6h") is a config, not on the read-model */}
            <small>Target: under 6h</small>
          </div>
          <div className="metric">
            <span>Run success</span>
            {/* Run success ← recent_imports (succeeded vs total in the returned
                window); falls back to the mockup literal when the ledger is empty.
                TODO(api): a true 30-day denominator (server returns a bounded ledger). */}
            <strong>{runSuccessLabel}</strong>
            <small>{runSuccessRate}</small>
          </div>
          <div className="metric">
            <span>Latest volume</span>
            <strong>{latestVolume}</strong>
            {/* TODO(api): "within expected range" verdict needs the expected band */}
            <small>Within expected range</small>
          </div>
          <div className="metric">
            <span>Next run</span>
            {/* TODO(api): next-run schedule + countdown (no scheduler field on read-model) */}
            <strong>06:00</strong>
            <small>In 2h 48m</small>
          </div>
        </section>

        <section className="health-grid">
          <article className="panel health-chart">
            <div className="section-header">
              <div>
                <h2>Published row volume</h2>
                <p>Expected range and daily publications · last 30 days</p>
              </div>
              <div className="health-legend">
                <span>
                  <i style={{ background: "rgba(126,107,196,.18)" }} />
                  Expected
                </span>
                <span>
                  <i />
                  Rows
                </span>
                <span>
                  <i />
                  Anomaly
                </span>
              </div>
            </div>
            <div className="health-chart-body">
              {/* TODO(api): 30-day published-volume series, expected-range band,
                  and anomaly points/evidence -> SVG paths (mockup literals). */}
              <svg
                className="health-svg"
                viewBox="0 0 760 250"
                role="img"
                aria-label="Thirty-day run health and row volume"
              >
                <path className="grid" d="M48 26H740M48 82H740M48 138H740M48 194H740M48 226H740" />
                <text x="5" y="30">
                  20k
                </text>
                <text x="5" y="86">
                  15k
                </text>
                <text x="5" y="142">
                  10k
                </text>
                <text x="20" y="198">
                  5k
                </text>
                <path
                  className="expected-range"
                  d="M48 78 C130 76 208 82 303 78 S475 70 570 73 S680 65 740 63 L740 138 C675 142 640 146 570 143 S480 150 436 147 S340 155 303 152 S170 160 48 165Z"
                />
                <path
                  className="volume-line"
                  d="M48 111 C100 105 130 112 174 106 S255 99 303 104 S388 93 436 98 S522 88 570 92 S622 87 640 142 S680 86 696 84 S725 80 740 76"
                />
                <circle className="warning-point" cx="640" cy="142" r="7" />
                <text className="warning-label" x="626" y="148" textAnchor="end">
                  17 Jul · 22% below range
                </text>
                <text x="48" y="246">
                  23 Jun
                </text>
                <text x="370" y="246">
                  8 Jul
                </text>
                <text x="704" y="246">
                  22 Jul
                </text>
              </svg>
            </div>
          </article>
          <div className="health-side">
            <article className="health-card">
              <h3>Data completeness</h3>
              {/* TODO(api): completeness score + required-dimension coverage */}
              <span className="value">100%</span>
              <p>All required dates and dimensions are present.</p>
              <div className="health-bar">
                <i style={{ width: "100%" }} />
              </div>
            </article>
            <article className="health-card">
              <h3>Schema stability</h3>
              {/* TODO(api): schema-change surveillance window */}
              <span className="value">30 days</span>
              <p>No unexpected field or type changes.</p>
              <div className="health-bar">
                <i style={{ width: "100%" }} />
              </div>
            </article>
          </div>
        </section>

        <section className="detail-strip">
          <div className="detail-cell">
            <span>Published through</span>
            {/* Published through ← published_execution.state_changed_at; falls
                back to the mockup literal when the read-model is silent. */}
            <b>{publishedThroughLabel}</b>
          </div>
          <div className="detail-cell">
            <span>Current versions</span>
            <b className="event">{versionsLabel}</b>
          </div>
          <div className="detail-cell">
            <span>Last-known-good</span>
            {/* TODO(api): last-known-good execution date (rollback target) */}
            <b>22 Jul 2026</b>
          </div>
        </section>
      </div>
    </>
  );
}
