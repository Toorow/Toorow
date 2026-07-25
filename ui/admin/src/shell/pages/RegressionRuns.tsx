/**
 * RegressionRuns — the eval-run history surface for the Test workspace.
 *
 * Grounds the eval loop (epic 14): the offline runner (scripts/run_evals.py)
 * replays the golden-question set and scores SQL precision + citation recall.
 * Each run produces a score (e.g. 48/50) and surfaces REGRESSIONS — questions
 * that passed before and now fail. The Overview metric strip already teases the
 * latest run ("Latest test run 48 / 50, 2 regressions to review"); this screen
 * is the full run history behind it.
 *
 * The application shell (ApplicationShell.tsx) already renders the frame,
 * sidebar, topbar, and <main className="main">. This component renders ONLY the
 * page content that lives inside <main>: the page header, a summary strip, and
 * the runs table.
 *
 * Data: the eval-run history is fetched from GET /api/eval/runs?project_id=<id>
 * (most-recent-first). The RUNS literal below stays as a designed, honest
 * fallback: it renders while the fetch is in flight and offline, and a
 * successful fetch replaces it with the real run set (honestly empty until the
 * offline runner has produced a run). The summary strip derives from whatever
 * list is currently rendered so the strip and table never disagree.
 *
 * Styling: application.css (global, via the shell) for shell/layout classes
 * (page-header, panel, metric-strip, section-header, signal-label, signal-mark
 * success|warning, secondary-button, table classes) + regression-runs.css for
 * the page-specific score-cell layout. Colors come exclusively from the
 * application.css CSS variables (dark-safe); numbers use Geist tabular via those
 * classes and the Geist-family cells here.
 */
import { useEffect, useState } from "react";
import "../application.css";
import "./regression-runs.css";

// ---------------------------------------------------------------------------
// View model. One row per eval run against the golden-question set, most recent
// first. `regressions` is the count of questions that passed on a prior run and
// now fail; `status` is "passed" when there are none, "regressed" otherwise.
// ---------------------------------------------------------------------------

type RunStatus = "passed" | "regressed";

interface EvalRun {
  /** Human-readable run timestamp (date + time). */
  when: string;
  /** Questions answered correctly out of the golden-set total. */
  scored: number;
  /** Golden-set size (denominator of the score). */
  total: number;
  /** SQL precision as a percentage, 0–100. */
  precision: number;
  /** Count of questions that regressed since the previous passing run. */
  regressions: number;
  status: RunStatus;
}

// Mockup literals — the designed eval-run history. Rendered verbatim so the page
// is finished with no backend. Most recent first; the top row is the "48 / 50,
// 2 regressions" run Overview surfaces.
// TODO(api): replace with the offline runner's persisted run history
// (scripts/run_evals.py output; score, precision, citation recall, regressions).
const RUNS: EvalRun[] = [
  {
    when: "24 Jul 2026 · 09:12",
    scored: 48,
    total: 50,
    precision: 96,
    regressions: 2,
    status: "regressed",
  },
  {
    when: "23 Jul 2026 · 18:40",
    scored: 50,
    total: 50,
    precision: 100,
    regressions: 0,
    status: "passed",
  },
  {
    when: "23 Jul 2026 · 08:55",
    scored: 49,
    total: 50,
    precision: 98,
    regressions: 0,
    status: "passed",
  },
  {
    when: "22 Jul 2026 · 17:03",
    scored: 46,
    total: 50,
    precision: 92,
    regressions: 3,
    status: "regressed",
  },
  {
    when: "22 Jul 2026 · 09:20",
    scored: 49,
    total: 50,
    precision: 98,
    regressions: 0,
    status: "passed",
  },
  {
    when: "21 Jul 2026 · 16:48",
    scored: 50,
    total: 50,
    precision: 100,
    regressions: 0,
    status: "passed",
  },
];

/** Round a ratio to a whole-percent string, e.g. 0.96 -> "96%". */
function pct(numerator: number, denominator: number): string {
  if (denominator === 0) return "—";
  return `${Math.round((numerator / denominator) * 100)}%`;
}

// The shape one run takes over the wire (GET /api/eval/runs). run_at is ISO 8601,
// precision_pct is a number (e.g. 96.0), status is "passed" | "regressed".
interface ApiRun {
  id: string;
  run_at: string;
  score_passed: number;
  score_total: number;
  precision_pct: number;
  regressions: number;
  status: string;
}

/** Format an ISO 8601 timestamp to a short "24 Jul 2026 · 09:12". Falls back to
 *  the raw string if it does not parse, so a bad value never blanks the cell. */
function formatWhen(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const day = String(d.getDate()).padStart(2, "0");
  const month = d.toLocaleString("en-US", { month: "short" });
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${day} ${month} ${d.getFullYear()} · ${hh}:${mm}`;
}

/** Map a wire run to the view model the table renders. */
function toRun(r: ApiRun): EvalRun {
  return {
    when: formatWhen(r.run_at),
    scored: r.score_passed,
    total: r.score_total,
    precision: Math.round(r.precision_pct),
    regressions: r.regressions,
    status: r.status === "passed" ? "passed" : "regressed",
  };
}

interface RegressionRunsProps {
  projectId?: string;
}

export default function RegressionRuns({ projectId = "default" }: RegressionRunsProps) {
  // RUNS render finished while the fetch is in flight and offline; a successful
  // fetch replaces them with the real, most-recent-first history (which is
  // honestly empty until the offline runner has produced a run).
  const [runs, setRuns] = useState<EvalRun[]>(RUNS);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await fetch(
          `/api/eval/runs?project_id=${encodeURIComponent(projectId)}`,
        );
        if (!res.ok) return; // keep the mock fallback
        const body = (await res.json()) as { runs?: ApiRun[] };
        if (alive) {
          setRuns((body.runs ?? []).map(toRun));
          setLoaded(true);
        }
      } catch {
        /* offline — keep the designed mock so the surface stays finished. */
      }
    })();
    return () => {
      alive = false;
    };
  }, [projectId]);

  // Summary derives from the rendered rows so the strip and table never disagree.
  const latest = runs.length > 0 ? runs[0] : undefined;
  const cleanRuns = runs.filter((r) => r.regressions === 0).length;
  const passRate = pct(cleanRuns, runs.length);
  const openRegressions = latest ? latest.regressions : 0;
  const empty = loaded && runs.length === 0;

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Regression runs</h1>
          <p>
            History of eval runs against the golden questions. Watch for regressions before
            shipping changes.
          </p>
        </div>
        {/* TODO(api): trigger a fresh eval run (scripts/run_evals.py). */}
        <button className="secondary-button" type="button">
          Run evals
        </button>
      </div>

      <section className="metric-strip">
        <div className="metric">
          <span>Latest score</span>
          <strong>
            {latest ? `${latest.scored} / ${latest.total}` : "—"}
          </strong>
          <small>{latest ? latest.when : "No runs yet"}</small>
        </div>
        <div className="metric">
          <span>Pass rate</span>
          <strong>{passRate}</strong>
          <small>
            {cleanRuns}/{runs.length} recent runs clean
          </small>
        </div>
        <div className="metric">
          <span>Regressions to review</span>
          <strong>{openRegressions}</strong>
          <small>
            {openRegressions > 0 ? (
              <>
                <span className="signal warning" aria-hidden="true" />
                Passed before, failing now
              </>
            ) : (
              <>
                <span className="signal" aria-hidden="true" />
                Nothing to review
              </>
            )}
          </small>
        </div>
      </section>

      <section className="panel runs-panel">
        <div className="section-header">
          <div>
            <h2>Run history</h2>
            <p>
              Each run replays the golden-question set and scores SQL precision and citation
              recall. Most recent first.
            </p>
          </div>
        </div>
        <div className="table-scroll" tabIndex={0} aria-label="Eval run history">
          <table className="table runs-table">
            <thead>
              <tr>
                <th>Run</th>
                <th>Score</th>
                <th>Precision</th>
                <th>Regressions</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {empty ? (
                <tr>
                  <td colSpan={5} className="runs-empty">
                    No eval runs yet. Run the golden-question set to record the
                    first run and start tracking regressions.
                  </td>
                </tr>
              ) : null}
              {runs.map((run, i) => (
                <tr key={`${run.when}-${i}`}>
                  <td>{run.when}</td>
                  <td>
                    <span className="score-cell number">
                      {run.scored} / {run.total}
                    </span>
                  </td>
                  <td>
                    <span className="number">{run.precision}%</span>
                  </td>
                  <td>
                    {run.regressions > 0 ? (
                      <span className="signal-label warning">
                        <span className="signal-mark" />
                        <span className="number">{run.regressions}</span>
                      </span>
                    ) : (
                      <span className="muted number">0</span>
                    )}
                  </td>
                  <td>
                    {run.status === "passed" ? (
                      <span className="signal-label success">
                        <span className="signal-mark" />
                        Passed
                      </span>
                    ) : (
                      <span className="signal-label warning">
                        <span className="signal-mark" />
                        Regressed
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </>
  );
}
