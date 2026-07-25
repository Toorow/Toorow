/**
 * FirstPublication — faithful React port of the validated First-publication
 * mockup.
 *
 * Source of visual truth:
 *   _bmad-output/planning-artifacts/ux-designs/ux-connector-2026-07-23/
 *     mockups/first-publication.html
 *
 * The application shell (ApplicationShell.tsx) renders the frame, sidebar,
 * topbar, and <main className="main">. Like DatastreamOverview, this mockup
 * shows an OBJECT HEADER in the topbar (the datastream identity) and a
 * LOCAL-TABS bar directly above the page body, so this component renders the
 * FULL main content of the mockup — object header + local tabs + full-main body
 * — as a fragment inside the shell <main>, so the screen reads faithfully on its
 * own. All shell classes (.object, .local-tabs, .full-main, .page-header,
 * .header-actions, .panel, .signal-label, .signal-mark, .action-link, buttons)
 * are global in application.css.
 *
 * This screen is the FIRST-PUBLICATION lifecycle state: a recent interval that
 * is already published and usable, shown independently from the historical
 * backfill that continues in the background. Two tracks:
 *   1. Publication readiness — the versioned evidence for the version consumers
 *      can use NOW (published interval, freshness, rows, DQ, meaning/mapping,
 *      known limitation, immutable plan + publication id).
 *   2. First pull — the named phase timeline (authorization → recent extraction
 *      → validate/map → publish recent → historical backfill), where recent
 *      value and historical coverage progress SEPARATELY, plus a last-known-good
 *      preservation note.
 *
 * Data: this maps to the SAME readiness read-model that RapportPretCard drives —
 *   GET /api/projects/{projectId}/datastreams/{datastreamId}/first-report/readiness
 * returning FirstReportReadiness (see RapportPretCard). That model DOES expose:
 * overall disposition, headline, current_publication (execution_id / row_count /
 * published_at), selected_objects (report_id / grain / currency = the meaning),
 * mapping (version), dq (checks), freshness (last_pull_at), exclusions (known
 * limitations), historical_coverage.state (recent vs history separation),
 * last_successful_pull (last-known-good) and the named phases[] timeline. It
 * does NOT expose several exact display values the mockup shows — see the GAP
 * LIST in the port report. Every unmapped value is the mockup's literal, flagged
 * // TODO(api); the whole screen falls back to those literals so it renders
 * finished with NO backend.
 */
import { useEffect, useRef, useState } from "react";
import type {
  FirstReportReadiness,
  ReadinessPhase,
} from "../../datastreams/RapportPretCard";
import "../application.css";
import "./first-publication.css";

/** Provider logo assets that actually exist under public/connectors/. Anything not
 *  in this map has NO asset yet — we reference /connectors/<name>.svg rather than
 *  hand-draw a provider glyph (Meta is present for this screen). */
const PROVIDER_LOGOS: Record<string, { src: string; alt: string }> = {
  meta: { src: "/connectors/meta.svg", alt: "Meta" },
  "google-ads": { src: "/connectors/google-ads.svg", alt: "Google Ads" },
  "google-analytics": { src: "/connectors/google-analytics.svg", alt: "Google Analytics" },
  "google-sheets": { src: "/connectors/google-sheets.svg", alt: "Google Sheets" },
};

interface FirstPublicationProps {
  projectId?: string;
  datastreamId?: string;
  apiToken?: string;
}

type FetchState =
  | { status: "loading" }
  | { status: "ok"; readiness: FirstReportReadiness }
  | { status: "error" };

/** Readiness fetch, mirroring RapportPretCard's convention (project-scoped path,
 *  Bearer token, no-store). Kept local so this shell page has no MUI dependency;
 *  a failure is swallowed to the mockup fallback (the screen must render with no
 *  backend). */
async function fetchReadiness(
  projectId: string,
  datastreamId: string,
  apiToken: string,
): Promise<FirstReportReadiness> {
  const resp = await fetch(
    `/api/projects/${encodeURIComponent(projectId)}/datastreams/${encodeURIComponent(datastreamId)}/first-report/readiness`,
    {
      method: "GET",
      headers: apiToken ? { Authorization: `Bearer ${apiToken}` } : {},
      cache: "no-store",
    },
  );
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json() as Promise<FirstReportReadiness>;
}

/** e.g. 186420 -> "186,420" (tabular). */
function groupNum(n: number | null | undefined): string | null {
  if (n == null) return null;
  return n.toLocaleString("en-US");
}

/** Freshness age from an ISO instant, e.g. "22 minutes" / "3 hours". Returns
 *  null when the API is silent so the mockup literal shows instead. */
function freshnessAge(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return null;
  const mins = Math.max(0, Math.round((Date.now() - then) / 60_000));
  if (mins < 60) return `${mins} minute${mins === 1 ? "" : "s"}`;
  const hours = Math.round(mins / 60);
  if (hours < 48) return `${hours} hour${hours === 1 ? "" : "s"}`;
  return `${Math.round(hours / 24)} days`;
}

/** "1J4A8F2" -> "pub_01J4A8F2"-style short id; here we surface the raw id (mono).
 *  The mockup renders a short publication id; we show the real execution id when
 *  present, truncated to keep the mono chip compact. */
function shortId(id: string | null | undefined): string | null {
  if (!id) return null;
  return id.length > 14 ? id.slice(0, 14) : id;
}

/** A phase interval -> a compact human label, e.g. "1–21 Jul 2026". Returns null
 *  when the interval (or its start) is absent so the mockup literal shows. The
 *  end is inclusive-facing (end_exclusive - 1 day) to read as a closed range. */
function intervalLabel(
  interval: { start?: string; end_exclusive?: string } | null | undefined,
): string | null {
  if (!interval?.start) return null;
  const start = new Date(interval.start);
  if (Number.isNaN(start.getTime())) return null;
  const dayFmt = new Intl.DateTimeFormat("en-GB", { day: "numeric", timeZone: "UTC" });
  const fullFmt = new Intl.DateTimeFormat("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
  if (!interval.end_exclusive) return fullFmt.format(start);
  const endExcl = new Date(interval.end_exclusive);
  if (Number.isNaN(endExcl.getTime())) return fullFmt.format(start);
  // end_exclusive is the day AFTER the last covered day; step back one day so the
  // label reads as a closed interval (matches the mockup's "1–21 Jul" style).
  const endIncl = new Date(endExcl.getTime() - 86_400_000);
  const sameMonth =
    start.getUTCFullYear() === endIncl.getUTCFullYear() &&
    start.getUTCMonth() === endIncl.getUTCMonth();
  return sameMonth
    ? `${dayFmt.format(start)}–${fullFmt.format(endIncl)}`
    : `${fullFmt.format(start)} – ${fullFmt.format(endIncl)}`;
}

export default function FirstPublication({
  projectId = "default",
  datastreamId = "campaign-performance",
  apiToken = import.meta.env.VITE_ADMIN_API_TOKEN ?? "",
}: FirstPublicationProps) {
  const [state, setState] = useState<FetchState>({ status: "loading" });
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const readiness = await fetchReadiness(projectId, datastreamId, apiToken);
        if (!cancelled) setState({ status: "ok", readiness });
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
  }, [projectId, datastreamId, apiToken]);

  const r = state.status === "ok" ? state.readiness : null;
  const pub = r?.current_publication ?? null;

  // ---- Real maps from the readiness read-model (fall back to mockup literal) --

  // Published rows = current publication row_count.
  const publishedRows = groupNum(pub?.row_count) ?? "186,420"; // TODO(api): expected-volume baseline to compare against
  // Freshness = age since the last successful pull. The model carries a dedicated
  // freshness signal (freshness.last_pull_at — same field RapportPretCard reads);
  // fall back to the publication's published_at, then to the mockup literal.
  const freshness =
    freshnessAge(r?.freshness?.last_pull_at) ??
    freshnessAge(pub?.published_at) ??
    "22 minutes"; // TODO(api): freshness *target* (under 6 hours) is not in the model
  // Meaning = selected report grain + currency, versioned by the mapping.
  const mappingLabel =
    r?.mapping?.version_number != null
      ? `mapping_v${r.mapping.version_number}`
      : "mapping_v17"; // TODO(api): human mapping label
  const grain = r?.selected_objects?.grain?.join(" ") ?? "Campaign daily";
  const currency = r?.selected_objects?.currency ?? "EUR";
  const meaningSub = `${grain} · ${currency}`;
  // DQ = passed / total checks.
  const dqUnresolved = r?.dq?.total_unresolved ?? null;
  // The model exposes unresolved findings, not a passed/total denominator; the
  // mockup shows "8 / 8 passed", which we cannot reconstruct — literal below.
  const dqLabel =
    dqUnresolved != null && dqUnresolved === 0 ? "All checks passed" : "8 / 8 passed"; // TODO(api): passed/total denominator
  const dqSub =
    dqUnresolved != null && dqUnresolved > 0
      ? `${dqUnresolved} to review`
      : "No blocking findings";
  // Known limitation = first declared exclusion (source-declared).
  const knownLimitation = r?.exclusions?.[0]?.kind ?? "Conversions at D+1"; // TODO(api): human-readable exclusion label
  const knownLimitationReason =
    r?.exclusions?.[0]?.reason ?? "Declared by the source";
  // Immutable plan + publication ids (mono). The model exposes the execution id;
  // the mockup also shows the immutable plan id, which the readiness model does
  // NOT carry — plan_v12 stays a literal.
  const publicationId = shortId(pub?.execution_id) ?? "pub_01J4A8F2"; // TODO(api): stable short publication id
  const planLabel = "plan_v12"; // TODO(api): immutable plan version id is not in the readiness model
  // Score header: the mockup header is always "Published" — recent is usable in
  // both ready and degraded dispositions. (A blocked disposition is not designed
  // on this screen; the model's `overall` is read but never demotes the header.)
  const publishedHeadline = "Published"; // TODO(api): blocked-disposition header not designed here
  const checksLine = "8 of 8 checks passed"; // TODO(api): passed/total checks denominator

  // ---- Phase timeline ------------------------------------------------------
  // Faithful named phases from the mockup. When the model returns phases[], we
  // key by phase name to reflect real running/succeeded state; otherwise we use
  // the mockup literals verbatim. Recent value and historical coverage progress
  // separately: the last phase (history) is an independent, still-running track.
  const phasesById: Record<string, ReadinessPhase> = {};
  for (const p of r?.phases ?? []) phasesById[p.phase] = p;
  const isRunning = (phaseKey: string, fallbackRunning: boolean): boolean => {
    const p = phasesById[phaseKey];
    if (!p) return fallbackRunning;
    return p.state === "running" || p.state === "waiting";
  };
  const recentPhase = phasesById["recent_pull"] ?? null;
  const historyPhase = phasesById["history"] ?? null;

  // ---- Recent vs history: two INDEPENDENT tracks ---------------------------
  // The published interval (recent-first). recent_coverage carries only a state,
  // but the recent_pull phase carries the concrete extraction interval — derive
  // the label from it, else the mockup literal.
  const recentInterval = intervalLabel(recentPhase?.interval);
  const publishedInterval = recentInterval ?? "1–21 Jul 2026"; // TODO(api): concrete recent published interval label (when recent_pull phase omits interval)
  // Recent extraction phase copy: real rows/interval from the recent_pull phase.
  const recentPhaseRows = groupNum(recentPhase?.rows) ?? publishedRows;
  const recentPhaseInterval = recentInterval ?? "1–21 Jul"; // TODO(api): recent extraction interval (when phase omits it)
  // Historical backfill. historical_coverage exposes a coverage state and the
  // history phase carries its own interval; neither exposes a completion percent,
  // so the numeric % and its bar width stay a literal.
  const historyPercent = 31; // TODO(api): historical backfill completion percentage (not in the model)
  const historyInterval = intervalLabel(historyPhase?.interval);
  const historyState = r?.historical_coverage?.state ?? null;
  // Compose the sub-label from whatever is real (range and/or state); the percent
  // remains literal. When nothing is backed, fall to the full mockup literal.
  const historySub =
    historyInterval || historyState
      ? [historyInterval, historyState ? historyState.replace(/_/g, " ") : null]
          .filter(Boolean)
          .join(" · ") + ` · ${historyPercent}% complete` // TODO(api): completion percentage
      : "Jan–Jun 2026 · 31% complete"; // TODO(api): concrete historical range + percentage

  // ---- Last-known-good preservation ----------------------------------------
  // The degraded note is a preservation guarantee; when the model carries a
  // last_successful_pull, surface its execution id as the concrete anchor.
  const lastGoodId = shortId(r?.last_successful_pull?.execution_id);

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

      <div className="first-publication full-main">
        <div className="page-header">
          <div>
            <h1>First publication</h1>
            <p>
              Your recent interval is published and usable. Historical backfill
              continues independently.
            </p>
          </div>
          <div className="header-actions">
            {/* TODO(api): MCP-host connection flow */}
            <button className="secondary-button">Connect MCP host</button>
            <a className="primary-button action-link primary" href="#data">
              View published data
            </a>
          </div>
        </div>

        <section className="publication-grid">
          {/* Track 1 — Publication readiness: the versioned evidence for the
              version consumers can use NOW. */}
          <article className="panel readiness-panel">
            <div className="readiness-head">
              <div>
                <h2>Publication readiness</h2>
                <p>The evidence for the version that consumers can use now.</p>
              </div>
              <div className="readiness-score">
                <strong>{publishedHeadline}</strong>
                <span>{checksLine}</span>
              </div>
            </div>
            <div className="readiness-grid">
              <div className="readiness-item">
                <span>Published interval</span>
                <strong>{publishedInterval}</strong>
                <small>Recent-first publication</small>
              </div>
              <div className="readiness-item">
                <span>Freshness</span>
                <strong>{freshness}</strong>
                <small>Target: under 6 hours</small>
              </div>
              <div className="readiness-item">
                <span>Published rows</span>
                <strong>{publishedRows}</strong>
                <small>Expected volume</small>
              </div>
              <div className="readiness-item">
                <span>Data quality</span>
                <strong>{dqLabel}</strong>
                <small>{dqSub}</small>
              </div>
              <div className="readiness-item">
                <span>Meaning</span>
                <strong className="mono">{mappingLabel}</strong>
                <small>{meaningSub}</small>
              </div>
              <div className="readiness-item">
                <span>Known limitation</span>
                <strong>{knownLimitation}</strong>
                <small>{knownLimitationReason}</small>
              </div>
            </div>
            <div className="readiness-foot">
              <span>
                Published from immutable plan <code>{planLabel}</code>
              </span>
              <code>{publicationId}</code>
            </div>
          </article>

          {/* Track 2 — First pull: the named phase timeline. Recent value and
              historical coverage progress SEPARATELY. */}
          <article className="panel phase-panel">
            <h2>First pull</h2>
            <p>Recent value and historical coverage progress separately.</p>
            <ol className="phase-list">
              <li className="phase">
                <span className="phase-mark" />
                <div className="phase-copy">
                  <strong>Authorization verified</strong>
                  <small>Acme Ads · account access confirmed</small>
                </div>
                {/* TODO(api): per-phase timestamp (model carries state, not clock) */}
                <span className="phase-time">09:02</span>
              </li>
              <li className="phase">
                <span className="phase-mark" />
                <div className="phase-copy">
                  <strong>Recent extraction</strong>
                  <small>{recentPhaseInterval} · {recentPhaseRows} rows</small>
                </div>
                <span className="phase-time">09:06</span>
              </li>
              <li className="phase">
                <span className="phase-mark" />
                <div className="phase-copy">
                  <strong>Validate and map</strong>
                  <small>8 checks passed · {mappingLabel}</small>
                </div>
                <span className="phase-time">09:08</span>
              </li>
              <li className="phase">
                <span className="phase-mark" />
                <div className="phase-copy">
                  <strong>Publish recent data</strong>
                  <small>Available to UI, API and MCP</small>
                </div>
                <span className="phase-time">09:10</span>
              </li>
              {/* Historical backfill — the independent, still-running track. */}
              <li className={`phase${isRunning("history", true) ? " running" : ""}`}>
                <span className="phase-mark" />
                <div className="phase-copy">
                  <strong>Historical backfill</strong>
                  <small>{historySub}</small>
                  <div
                    className="history-progress"
                    role="progressbar"
                    aria-valuenow={historyPercent}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-label={`Historical backfill ${historyPercent} percent`}
                  >
                    <i style={{ width: `${historyPercent}%` }} />
                  </div>
                </div>
                <span className="phase-time">
                  {isRunning("history", true) ? "Running" : "Done"}
                </span>
              </li>
            </ol>
          </article>
        </section>

        {/* Last-known-good preservation note: a failed next historical batch
            never replaces the published data. */}
        <div className="degraded-note">
          <span className="signal-label warning">
            <span className="signal-mark" />
          </span>
          <div>
            <strong>
              If the next historical batch fails, this publication stays
              available.
            </strong>
            The page will show the failed interval, retry time and a route to
            Runs without replacing the last-known-good data.
            {lastGoodId && (
              <>
                {" "}
                Last-known-good: <code>{lastGoodId}</code>.
              </>
            )}
          </div>
        </div>
      </div>
    </>
  );
}
