/**
 * Provenance — the lineage read-out for the Governance workspace.
 *
 * PURPOSE
 * Provenance answers one question: "where did this published number come from?".
 * In toorow every published figure carries provenance in its envelope
 * `meta.provenance`:
 *   - the pull_ids that fed it,
 *   - the mapping version used to shape it,
 *   - the published execution pointer that produced it, and
 *   - for money, the FX as-of-day rate used to convert AT READ
 *     (epic 39: FX AT READ, with as-of-day provenance).
 * This is a Governance surface: it explains how the data are verified and
 * traceable back to their inputs.
 *
 * The application shell (ApplicationShell.tsx) already renders the frame,
 * sidebar, topbar, and <main>. This component renders ONLY the page content that
 * lives inside <main>: the page header, a short "what provenance guarantees"
 * explainer panel, and a panel with a table of recent published figures /
 * executions.
 *
 * Styling: application.css (global, via the shell) for shell/layout classes
 * (page-header, panel, signal-label, signal-mark, number, mono/event) +
 * provenance.css for the page-specific explainer rows and lineage table. Colors
 * come exclusively from the application.css CSS variables (dark-safe); IDs use
 * JetBrains Mono (.mono) and numbers/dates use Geist tabular (.number).
 *
 * ── Data ──────────────────────────────────────────────────────────────────────
 * There is no single dedicated provenance endpoint yet. The real source is the
 * published state of GET /api/datastreams plus each published figure's envelope
 * `meta.provenance` (pull_ids, mapping_version, published execution pointer, and
 * — for money — the FX as-of-day rate). Until that read is wired, the surface
 * renders designed, honest placeholder content, each block flagged with
 * // TODO(api) (exactly as Overview.tsx and CountrySplit.tsx use mockup literals
 * + TODO(api)). The page renders finished with no backend.
 */
import type { ReactElement } from "react";
import ConnectorLogo from "../ConnectorLogo";
import "../application.css";
import "./provenance.css";

// ---------------------------------------------------------------------------
// View model — one published figure and the provenance envelope behind it.
// TODO(api): every field below is a designed placeholder. The real source is
// GET /api/datastreams published state + each figure's envelope meta.provenance.
// ---------------------------------------------------------------------------

interface ProvenanceRow {
  /** Connector/provider id resolved through ConnectorLogo's managed registry. */
  provider: string;
  datastream: string; // friendly datastream label
  metric: string; // the published figure / metric name
  metricNote: string; // grain / scope note under the metric
  pullIds: string[]; // pull_ids that fed this figure (mono)
  mappingVersion: string; // mapping version used, e.g. "mapping_v17" (mono)
  /** FX as-of-day provenance — null when the figure is single-currency. */
  fx: { asOf: string; rate: string } | null;
  published: string; // published execution date (Geist tabular)
}

// Mockup literals — the validated placeholder lineage. Rendered verbatim so the
// surface is finished with no backend.
// TODO(api): replace with GET /api/datastreams published state + meta.provenance.
const MOCK_ROWS: ProvenanceRow[] = [
  {
    provider: "meta",
    datastream: "Meta Ads · Acme",
    metric: "Media spend",
    metricNote: "EUR · daily grain",
    pullIds: ["pull_9f2ac1", "pull_9f2b07", "pull_9f2b44"],
    mappingVersion: "mapping_v17",
    fx: { asOf: "2026-07-22", rate: "1 USD = 0.9184 EUR" },
    published: "22 Jul 2026",
  },
  {
    provider: "google-ads",
    datastream: "Google Ads · Northwind",
    metric: "Media spend",
    metricNote: "EUR · daily grain",
    pullIds: ["pull_7c10de", "pull_7c1122"],
    mappingVersion: "mapping_v17",
    fx: { asOf: "2026-07-22", rate: "1 USD = 0.9184 EUR" },
    published: "22 Jul 2026",
  },
  {
    provider: "google-analytics",
    datastream: "GA4 · acme.com",
    metric: "Revenue",
    metricNote: "EUR · daily grain",
    pullIds: ["pull_4b88a0"],
    mappingVersion: "mapping_v16",
    fx: { asOf: "2026-07-21", rate: "1 USD = 0.9203 EUR" },
    published: "21 Jul 2026",
  },
  {
    provider: "google-analytics",
    datastream: "GA4 · acme.com",
    metric: "Sessions",
    metricNote: "count · daily grain",
    pullIds: ["pull_4b88a0"],
    mappingVersion: "mapping_v16",
    fx: null, // single-currency figure — no FX applied
    published: "21 Jul 2026",
  },
  {
    provider: "google-sheets",
    datastream: "Media plan · Planning team",
    metric: "Planned spend",
    metricNote: "EUR · monthly grain",
    pullIds: ["pull_2fd001", "pull_2fd014", "pull_2fd022", "pull_2fd031"],
    mappingVersion: "mapping_v17",
    fx: { asOf: "2026-07-20", rate: "1 GBP = 1.1642 EUR" },
    published: "20 Jul 2026",
  },
  {
    provider: "meta",
    datastream: "Meta Ads · Acme",
    metric: "ROAS",
    metricNote: "ratio · daily grain",
    pullIds: ["pull_9f2ac1"],
    mappingVersion: "mapping_v17",
    fx: null, // derived ratio — currency cancels, no FX applied
    published: "19 Jul 2026",
  },
];

// The four things a published figure's provenance guarantees. Calm explainer
// rows. TODO(api): each maps to an envelope meta.provenance field.
interface Guarantee {
  title: string;
  copy: string;
  mono: string; // the envelope key this guarantee reads from (mono)
  icon: ReactElement;
}

const GUARANTEES: Guarantee[] = [
  {
    title: "Pull lineage",
    copy: "Every published figure records the exact pull ids that fed it, so any number traces back to the raw extractions behind it.",
    mono: "meta.provenance.pull_ids",
    icon: (
      <svg viewBox="0 0 24 24">
        <path d="M7 7h10v10H7zM7 12H3M21 12h-4M12 7V3M12 21v-4" />
      </svg>
    ),
  },
  {
    title: "Mapping version",
    copy: "The mapping version that shaped the figure is pinned, so you can see which canonical rules were in force when it was published.",
    mono: "meta.provenance.mapping_version",
    icon: (
      <svg viewBox="0 0 24 24">
        <path d="M4 6h16M4 12h12M4 18h8" />
      </svg>
    ),
  },
  {
    title: "FX as-of-day",
    copy: "Money is converted at read using the as-of-day rate for the figure's date, and that rate is recorded — never re-derived after the fact.",
    mono: "meta.provenance.fx_as_of",
    icon: (
      <svg viewBox="0 0 24 24">
        <path d="M6 18L18 6M8 7h.01M16 17h.01" />
      </svg>
    ),
  },
  {
    title: "Published execution",
    copy: "A pointer to the execution that published the figure closes the loop: the run, its inputs, and the moment it went live are all recoverable.",
    mono: "meta.provenance.published_execution",
    icon: (
      <svg viewBox="0 0 24 24">
        <path d="M12 7v5l3 2M20 12a8 8 0 11-16 0 8 8 0 0116 0z" />
      </svg>
    ),
  },
];

interface ProvenanceProps {
  projectId?: string;
}

export default function Provenance({ projectId }: ProvenanceProps) {
  // projectId will scope GET /api/datastreams once the published-state read is
  // wired; referenced here so the prop is part of the contract from day one.
  void projectId;

  const rows = MOCK_ROWS;

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Provenance</h1>
          <p>
            Trace every published figure back to the pulls, mapping version, and
            as-of FX rate that produced it.
          </p>
        </div>
      </div>

      <section className="panel provenance-explainer">
        <div className="provenance-explainer-header">
          <div>
            <h2>What provenance guarantees</h2>
            <p>
              Each published figure carries a provenance envelope. These four
              records make the number verifiable and traceable back to its inputs.
            </p>
          </div>
          <span className="signal-label success">
            <span className="signal-mark" />
            Always recorded
          </span>
        </div>
        <div className="guarantee-rows">
          {GUARANTEES.map((g) => (
            <div className="guarantee-row" key={g.title}>
              <span className="guarantee-icon">{g.icon}</span>
              <div className="guarantee-copy">
                <strong>{g.title}</strong>
                <p>{g.copy}</p>
              </div>
              {/* TODO(api): envelope key this guarantee reads from. */}
              <code className="mono guarantee-key">{g.mono}</code>
            </div>
          ))}
        </div>
      </section>

      <section className="panel provenance-panel">
        <div className="provenance-panel-header">
          <div>
            <h2>Recent published figures</h2>
            <p>
              {/* TODO(api): real source is GET /api/datastreams published state +
                  each figure's envelope meta.provenance. */}
              The pulls, mapping version, and as-of FX rate behind each figure
              published to the data model.
            </p>
          </div>
          {/* TODO(api): figure / datastream filter. */}
          <button className="selector" type="button">
            All datastreams
          </button>
        </div>

        <div
          className="table-scroll"
          tabIndex={0}
          aria-label="Recent published figures and their provenance"
        >
          <table className="provenance-table">
            <thead>
              <tr>
                <th style={{ width: "22%" }}>Datastream</th>
                <th style={{ width: "16%" }}>Figure</th>
                <th style={{ width: "20%" }}>Pull ids</th>
                <th style={{ width: "13%" }}>Mapping version</th>
                <th style={{ width: "17%" }}>FX as-of</th>
                <th style={{ width: "12%" }}>Published</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr key={`${row.datastream}-${row.metric}-${i}`}>
                  <td>
                    <div className="datastream-cell">
                      <ConnectorLogo provider={row.provider} alt={row.datastream} />
                      <div>
                        <strong>{row.datastream}</strong>
                        <small>Published state</small>
                      </div>
                    </div>
                  </td>
                  <td>
                    <div className="figure-cell">
                      <strong>{row.metric}</strong>
                      <small>{row.metricNote}</small>
                    </div>
                  </td>
                  <td>
                    {/* TODO(api): pull_ids from meta.provenance. */}
                    <div className="pull-cell">
                      {row.pullIds.slice(0, 2).map((id) => (
                        <code className="mono" key={id}>
                          {id}
                        </code>
                      ))}
                      {row.pullIds.length > 2 ? (
                        <span className="pull-more number">
                          +{row.pullIds.length - 2}
                        </span>
                      ) : null}
                    </div>
                  </td>
                  <td>
                    {/* TODO(api): mapping_version from meta.provenance. */}
                    <code className="mono mapping-tag">{row.mappingVersion}</code>
                  </td>
                  <td>
                    {/* TODO(api): fx as-of-day rate from meta.provenance (epic 39,
                        FX AT READ). "—" when the figure is single-currency. */}
                    {row.fx ? (
                      <div className="fx-cell">
                        <strong className="number">{row.fx.asOf}</strong>
                        <small className="mono">{row.fx.rate}</small>
                      </div>
                    ) : (
                      <span className="fx-none" aria-label="Single-currency, no FX applied">
                        —
                      </span>
                    )}
                  </td>
                  {/* TODO(api): published execution date from meta.provenance. */}
                  <td className="number published-cell">{row.published}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="provenance-footer">
          <span>
            Showing <span className="number">{rows.length}</span> recent published
            figures
          </span>
          {/* TODO(api): no dedicated provenance endpoint yet — reads from
              GET /api/datastreams published state + envelope meta.provenance. */}
          <span className="footer-note mono">meta.provenance</span>
        </div>
      </section>
    </>
  );
}
