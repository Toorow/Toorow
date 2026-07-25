/**
 * Overview — faithful React port of the validated Overview mockup.
 *
 * Source of visual truth:
 *   _bmad-output/planning-artifacts/ux-designs/ux-connector-2026-07-23/
 *     mockups/project-overview.html
 *
 * The application shell (ApplicationShell.tsx) already renders the frame,
 * sidebar, topbar, and <main className="main">. This component renders ONLY the
 * page content that lives inside <main>: the page header, metric strip,
 * attention row, and the widget grid (paid-media chart + two mini-widgets).
 *
 * Styling: application.css (global, via the shell) for shell/layout classes +
 * overview.css for the page-specific widgets. Colors come exclusively from the
 * application.css CSS variables; numbers use Geist tabular via those classes.
 *
 * Data: the mockup surfaces business-analytics values (revenue, ROAS, mapping
 * coverage, test runs, paid-media trend, data freshness, widget feedback). The
 * existing /api/overview endpoint (see OverviewPage.tsx) only exposes extract-
 * fleet ops (fleet rows, breakers), a different concern — so almost every value
 * here is the mockup's literal, flagged with // TODO(api). The one clean map is
 * the active-datastream count, derived from the fleet length when present.
 */
import { useEffect, useRef, useState } from "react";
import type { OverviewData, OverviewFetchState } from "../../vueensemble/types";
import "../application.css";
import "./overview.css";
import { apiFetch } from "../../lib/apiFetch";

async function fetchOverview(projectId: string): Promise<OverviewData> {
  const resp = await apiFetch(`/api/overview?project_id=${encodeURIComponent(projectId)}`);
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.message ?? `HTTP ${resp.status}`);
  }
  return resp.json() as Promise<OverviewData>;
}

interface OverviewProps {
  projectId?: string;
  onOpenDatastream?: (id: string) => void;
  /** Navigate to Analyze > Renders (the conserved-output store). */
  onOpenRenders?: () => void;
}

/** GET /api/overview/summary — the metric-strip aggregates. */
interface OverviewSummary {
  active_datastreams: number;
  canonical_concepts: number;
  mapping_coverage_pct: number;
  published_trust: "trusted" | "attention" | "no_data";
  latest_test: { passed: number; total: number; regressions: number } | null;
}

export default function Overview({
  projectId = "default",
  onOpenDatastream,
  onOpenRenders,
}: OverviewProps) {
  const [state, setState] = useState<OverviewFetchState>({ status: "loading" });
  const [summary, setSummary] = useState<OverviewSummary | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    async function load() {
      try {
        const data = await fetchOverview(projectId);
        setState({ status: "ok", data });
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        setState({ status: "error", message: msg });
      }
    }

    setState({ status: "loading" });
    void load();
    intervalRef.current = setInterval(() => void load(), 60_000);
    return () => {
      if (intervalRef.current !== null) clearInterval(intervalRef.current);
    };
  }, [projectId]);

  // Real metric-strip aggregates (datastreams, mapping coverage, latest eval run).
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await apiFetch(`/api/overview/summary?project_id=${encodeURIComponent(projectId)}`);
        if (res.ok && alive) setSummary((await res.json()) as OverviewSummary);
      } catch {
        /* keep the literal strip when offline */
      }
    })();
    return () => {
      alive = false;
    };
  }, [projectId]);

  const data = state.status === "ok" ? state.data : null;

  // Active datastreams: prefer the summary aggregate, then the fleet length.
  const activeDatastreams = summary?.active_datastreams ?? data?.fleet.length ?? 7;
  const trustLabel =
    summary == null
      ? "Trusted"
      : summary.published_trust === "trusted"
        ? "Trusted"
        : summary.published_trust === "attention"
          ? "Needs attention"
          : "No data yet";
  const coveragePct = summary?.mapping_coverage_pct;
  const canonicalConcepts = summary?.canonical_concepts;
  const latestTest = summary?.latest_test ?? null;

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Overview</h1>
          <p>Recent analysis, published-data trust, and the work that needs your attention.</p>
        </div>
        {/* TODO(api): date-range selector wiring */}
        <button className="selector">1–22 Jul 2026⌄</button>
      </div>

      <section className="metric-strip">
        <div className="metric">
          <span>Published data</span>
          <strong>{trustLabel}</strong>
          <small>
            <span className="signal" aria-hidden="true" />
            {/* TODO(api): freshness "complete through" date. */}
            {summary?.published_trust === "no_data" ? "No datastreams yet" : "Complete through 22 Jul"}
          </small>
        </div>
        <div className="metric">
          <span>Active Datastreams</span>
          <strong>{activeDatastreams}</strong>
          {/* TODO(api): healthy / needs-attention breakdown. */}
          <small>{activeDatastreams === 0 ? "None connected yet" : "Across your sources"}</small>
        </div>
        <div className="metric">
          <span>Mapping coverage</span>
          <strong>{coveragePct != null ? `${coveragePct}%` : "96%"}</strong>
          <small>
            {canonicalConcepts != null ? `${canonicalConcepts} canonical concepts` : "42 canonical concepts"}
          </small>
        </div>
        <div className="metric">
          <span>Latest test run</span>
          <strong>{latestTest ? `${latestTest.passed} / ${latestTest.total}` : "—"}</strong>
          <small>
            {latestTest
              ? `${latestTest.regressions} regression${latestTest.regressions === 1 ? "" : "s"} to review`
              : "No runs yet"}
          </small>
        </div>
      </section>

      <div className="attention-row">
        <span className="attention-light" />
        <div>
          <strong>Search performance needs attention</strong>
          <br />
          {/* TODO(api): attention message from health/breaker state */}
          <span className="copy">The latest Google Ads run failed. Published data remain available.</span>
        </div>
        <a
          className="action-link"
          href="#"
          onClick={(e) => {
            e.preventDefault();
            // TODO(api): resolve the real datastream id for "Search performance"
            onOpenDatastream?.("search-performance");
          }}
        >
          View Datastream →
        </a>
      </div>

      <section className="panel insights-panel">
        <div className="section-header">
          <div>
            <h2>Daily insights</h2>
            <p>Highlights the assistant surfaced from this project, conserved here.</p>
          </div>
          <button className="secondary-button" onClick={() => onOpenRenders?.()}>
            Open in Renders
          </button>
        </div>
        <ul className="insight-list">
          {/* TODO(api): conserved daily-insight highlights (host-generated, stored). */}
          <li className="insight-item">
            <span className="insight-dot up" aria-hidden="true" />
            <div>
              <strong>ROAS up 8% week-over-week</strong>
              <span className="copy">Meta Ads campaign performance drove most of the lift.</span>
            </div>
            <span className="insight-age">2h ago</span>
          </li>
          <li className="insight-item">
            <span className="insight-dot flat" aria-hidden="true" />
            <div>
              <strong>Search spend flat, conversions down 5%</strong>
              <span className="copy">Worth a look before the next budget review.</span>
            </div>
            <span className="insight-age">6h ago</span>
          </li>
          <li className="insight-item">
            <span className="insight-dot up" aria-hidden="true" />
            <div>
              <strong>Organic clicks recovered after the July dip</strong>
              <span className="copy">Search Console shows a steady 3-week climb.</span>
            </div>
            <span className="insight-age">1d ago</span>
          </li>
        </ul>
      </section>

      <section className="widget-grid">
        <article className="panel chart-card">
          <div className="section-header">
            <div>
              <h2>Paid media performance</h2>
              <p>Matched spend and attributed revenue</p>
            </div>
            <button className="secondary-button">Open in Analyze</button>
          </div>
          <div className="chart-body">
            <div className="chart-summary">
              <div>
                <span>Revenue</span>
                {/* TODO(api): attributed revenue */}
                <strong>€284,600</strong>
              </div>
              <div>
                <span>ROAS</span>
                {/* TODO(api): ROAS */}
                <strong>3.42</strong>
              </div>
              <div className="chart-legend">
                <span>
                  <i />
                  Spend
                </span>
                <span>
                  <i />
                  Revenue
                </span>
              </div>
            </div>
            {/* TODO(api): spend / revenue trend series -> SVG paths */}
            <svg
              className="chart"
              viewBox="0 0 720 245"
              role="img"
              aria-label="Spend and revenue trend"
            >
              <path className="grid" d="M44 24H704M44 76H704M44 128H704M44 180H704M44 224H704" />
              <text x="4" y="28">€120k</text>
              <text x="14" y="80">€90k</text>
              <text x="14" y="132">€60k</text>
              <text x="14" y="184">€30k</text>
              <path
                className="spend"
                d="M44 192 C95 184 113 178 153 182 S225 160 266 165 S338 139 380 145 S453 115 496 123 S569 92 611 98 S672 70 704 66"
              />
              <path
                className="revenue"
                d="M44 170 C91 160 118 152 153 157 S225 132 266 135 S338 105 380 112 S453 78 496 84 S569 54 611 61 S674 35 704 30"
              />
              <text x="44" y="242">1 Jul</text>
              <text x="350" y="242">11 Jul</text>
              <text x="668" y="242">22 Jul</text>
            </svg>
          </div>
        </article>

        <div className="side-stack">
          <article className="mini-widget">
            <h3>Data freshness</h3>
            {/* TODO(api): published-on-schedule count */}
            <div className="mini-value">6 of 7</div>
            <div className="mini-meta">Datastreams published on schedule</div>
            <div className="bar">
              <i />
            </div>
            <div className="source-line">
              <span>Search performance</span>
              {/* TODO(api): oldest datastream age */}
              <b>26h old</b>
            </div>
          </article>

          <article className="mini-widget">
            <h3>Widget feedback</h3>
            {/* TODO(api): positive feedback % over last 30 days */}
            <div className="mini-value">91%</div>
            <div className="mini-meta">Positive over the last 30 days</div>
            <div className="source-line">
              <span>12 new responses</span>
              <b>+4%</b>
            </div>
            <div className="source-line">
              <span>Needs review</span>
              <b>2</b>
            </div>
          </article>
        </div>
      </section>

      <section className="panel renders-panel">
        <div className="section-header">
          <div>
            <h2>Recent renders</h2>
            <p>The latest cards and reports conserved from the assistant.</p>
          </div>
          <button className="secondary-button" onClick={() => onOpenRenders?.()}>
            View all
          </button>
        </div>
        <div className="recent-renders">
          {/* TODO(api): most recent conserved snapshots (GET /api/snapshots). */}
          <button className="render-tile" type="button" onClick={() => onOpenRenders?.()}>
            <span className="render-kind">Card</span>
            <strong>Q3 pacing vs plan</strong>
            <span className="render-age">2h ago · Live</span>
          </button>
          <button className="render-tile" type="button" onClick={() => onOpenRenders?.()}>
            <span className="render-kind">Report</span>
            <strong>Search performance summary</strong>
            <span className="render-age">1d ago</span>
          </button>
          <button className="render-tile" type="button" onClick={() => onOpenRenders?.()}>
            <span className="render-kind">Card</span>
            <strong>Channel breakdown</strong>
            <span className="render-age">2d ago</span>
          </button>
        </div>
      </section>
    </>
  );
}
