/**
 * WidgetFeedback — the "Widget feedback" surface for the Test workspace.
 *
 * PURPOSE: the trust signal for generated outputs. It surfaces the feedback
 * users and assistants left on generated cards/widgets/reports so the operator
 * can see, at a glance, whether the produced answers are landing.
 *
 * REAL DATA. Unlike the mockup-literal ports (Sources / DataWorkspace), this
 * screen wires the live endpoint:
 *
 *   GET /api/feedback?project_id=<projectId>&limit=50
 *
 * Each row: { id, rating, comment, module, report_ref, trace_id, created_at }.
 * The response is defensively unwrapped from either a bare array or a
 * { feedback: [...] } envelope. `rating` is a thumbs/score signal — a positive
 * value ("up" / 1 / "positive" / true …) renders as signal-label success, a
 * negative one as error; the exact encoding is treated defensively.
 *
 * The fetch follows the DataWorkspace pattern (fetch in useEffect, cancel guard)
 * but adds explicit loading / empty / error UI states — on this screen the data
 * IS the point, so we do not fall back to literals.
 *
 * Styling: application.css (global, via the shell) supplies every shared class
 * used here (page-header, panel, metric-strip / metric, section-header, table,
 * signal-label / signal-mark, provider-logo, number, mono, muted, sr-only) plus
 * the design tokens. widget-feedback.css only adds the few page-specific pieces
 * the shell does not have: the loading spinner, the designed empty/error states,
 * and the comment/source cell layouts. Colors come exclusively from the
 * application.css CSS variables (dark-safe); numbers and dates use Geist tabular.
 */
import { useEffect, useState } from "react";
import ConnectorLogo from "../ConnectorLogo";
import "../application.css";
import "./widget-feedback.css";

// ---------------------------------------------------------------------------
// Wire shape. Only the fields this screen reads are declared; the endpoint is
// permitted to return more. Every field is optional/unknown-tolerant because
// the exact encoding of `rating` (and the presence of the others) is defensive.
// ---------------------------------------------------------------------------

interface FeedbackRow {
  id?: string | null;
  rating?: unknown; // thumbs/score signal — see ratingSignal()
  comment?: string | null;
  module?: string | null; // connector/source id -> ConnectorLogo provider
  report_ref?: string | null; // rendered mono, like other IDs
  trace_id?: string | null;
  created_at?: string | null;
  [k: string]: unknown;
}

/** Response envelope: either a bare array or { feedback: [...] }. */
type FeedbackResponse = FeedbackRow[] | { feedback?: FeedbackRow[] | null };

type LoadState = "loading" | "ready" | "error";

// ---------------------------------------------------------------------------
// Rating -> signal. Defensive about the exact value: strings, numbers and
// booleans are all understood. Anything that is unambiguously positive
// ("up" / "positive" / "good" / "1" / 1 / true / a score > 0) maps to success;
// an unambiguously negative signal maps to error; anything else is neutral
// (rendered as the info signal so an unknown encoding still reads sensibly).
// ---------------------------------------------------------------------------

type RatingSignal = "success" | "error" | "info";

function ratingSignal(rating: unknown): { signal: RatingSignal; label: string } {
  if (rating === null || rating === undefined || rating === "") {
    return { signal: "info", label: "No rating" };
  }
  if (typeof rating === "boolean") {
    return rating
      ? { signal: "success", label: "Positive" }
      : { signal: "error", label: "Negative" };
  }
  if (typeof rating === "number") {
    if (rating > 0) return { signal: "success", label: "Positive" };
    if (rating < 0) return { signal: "error", label: "Negative" };
    return { signal: "info", label: "Neutral" };
  }
  const raw = String(rating).trim().toLowerCase();
  const positive = ["up", "positive", "good", "1", "yes", "helpful", "thumbs_up", "thumbsup", "like", "true"];
  const negative = ["down", "negative", "bad", "-1", "0", "no", "unhelpful", "thumbs_down", "thumbsdown", "dislike", "false"];
  if (positive.includes(raw)) return { signal: "success", label: "Positive" };
  if (negative.includes(raw)) return { signal: "error", label: "Negative" };
  // Numeric-looking string that is not in either list (e.g. "4").
  const asNum = Number(raw);
  if (!Number.isNaN(asNum)) {
    if (asNum > 0) return { signal: "success", label: "Positive" };
    if (asNum < 0) return { signal: "error", label: "Negative" };
    return { signal: "info", label: "Neutral" };
  }
  // Unknown encoding: surface it verbatim rather than mislabel it.
  return { signal: "info", label: String(rating) };
}

/** Absolute date "22 Jul 2026, 14:05" (en-GB, no seconds). "—" when unknown. */
function fmtDate(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

/** Unwrap the response defensively into a flat row array. */
function unwrap(data: FeedbackResponse | null | undefined): FeedbackRow[] {
  if (Array.isArray(data)) return data;
  if (data && Array.isArray(data.feedback)) return data.feedback;
  return [];
}

interface Summary {
  positivePct: number | null; // null when nothing rated
  total: number;
  needsReview: number;
}

/** Derive the summary strip from the fetched rows. Positive % is over the rows
 *  that carry a rating; needs-review counts explicit negative ratings. */
function summarize(rows: FeedbackRow[]): Summary {
  let positive = 0;
  let negative = 0;
  let rated = 0;
  for (const row of rows) {
    const { signal } = ratingSignal(row.rating);
    if (signal === "success") {
      positive += 1;
      rated += 1;
    } else if (signal === "error") {
      negative += 1;
      rated += 1;
    }
  }
  return {
    positivePct: rated > 0 ? Math.round((positive / rated) * 100) : null,
    total: rows.length,
    needsReview: negative,
  };
}

interface WidgetFeedbackProps {
  projectId?: string;
}

export default function WidgetFeedback({ projectId = "default" }: WidgetFeedbackProps) {
  const [rows, setRows] = useState<FeedbackRow[]>([]);
  const [state, setState] = useState<LoadState>("loading");

  // The exact request URL is stable per projectId — used for the fetch and kept
  // in scope so the failure state can be diagnosed against the same target.
  const url = `/api/feedback?project_id=${encodeURIComponent(projectId)}&limit=50`;

  useEffect(() => {
    let cancelled = false;
    setState("loading");
    async function load() {
      try {
        const resp = await fetch(url);
        if (!resp.ok) {
          if (!cancelled) setState("error");
          return;
        }
        const data = (await resp.json()) as FeedbackResponse;
        if (cancelled) return;
        setRows(unwrap(data));
        setState("ready");
      } catch {
        if (!cancelled) setState("error");
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [url]);

  const summary = summarize(rows);
  const isEmpty = state === "ready" && rows.length === 0;

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Widget feedback</h1>
          <p>What people thought of the generated cards and reports — the trust signal for the outputs.</p>
        </div>
      </div>

      <section className="metric-strip" aria-label="Feedback summary">
        <div className="metric">
          <span>Positive</span>
          <strong className="number">
            {summary.positivePct === null ? "—" : `${summary.positivePct}%`}
          </strong>
          <small>Of rated responses</small>
        </div>
        <div className="metric">
          <span>Total responses</span>
          <strong className="number">{summary.total}</strong>
          <small>Feedback collected on outputs</small>
        </div>
        <div className="metric">
          <span>Needs review</span>
          <strong className="number">{summary.needsReview}</strong>
          <small>
            {summary.needsReview === 0
              ? "Nothing flagged"
              : `${summary.needsReview} negative rating${summary.needsReview === 1 ? "" : "s"}`}
          </small>
        </div>
      </section>

      <section className="panel feedback-panel">
        <div className="section-header">
          <div>
            <h2>Feedback</h2>
            <p>Ratings and comments left on generated cards and reports.</p>
          </div>
        </div>

        {state === "loading" ? (
          <div className="feedback-state" role="status" aria-live="polite">
            <span className="feedback-spinner" aria-hidden="true" />
            <p>Loading feedback…</p>
          </div>
        ) : state === "error" ? (
          <div className="feedback-state feedback-state--error" role="alert">
            <span className="signal-label error">
              <span className="signal-mark" />
              Could not load feedback
            </span>
            <p>The feedback service did not respond. Refresh to try again.</p>
          </div>
        ) : isEmpty ? (
          <div className="feedback-state feedback-state--empty">
            <strong>No feedback yet</strong>
            <p>
              When users or assistants rate a generated card or report, their feedback appears here.
            </p>
          </div>
        ) : (
          <div className="table-scroll" tabIndex={0} aria-label="Feedback responses">
            <table className="table feedback-table">
              <thead>
                <tr>
                  <th>Rating</th>
                  <th>Comment</th>
                  <th>Source</th>
                  <th>Report</th>
                  <th>Date</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row, i) => {
                  const rating = ratingSignal(row.rating);
                  const module = (row.module ?? "").toString().trim();
                  const reportRef = (row.report_ref ?? "").toString().trim();
                  const key = row.id ?? row.trace_id ?? `feedback-${i}`;
                  return (
                    <tr key={key}>
                      <td>
                        <span className={`signal-label ${rating.signal}`}>
                          <span className="signal-mark" />
                          {rating.label}
                        </span>
                      </td>
                      <td>
                        {row.comment ? (
                          <span className="feedback-comment">{row.comment}</span>
                        ) : (
                          <span className="muted">No comment</span>
                        )}
                      </td>
                      <td>
                        {module ? (
                          <span className="feedback-source">
                            <ConnectorLogo provider={module} alt={module} />
                            <span>{module}</span>
                          </span>
                        ) : (
                          <span className="muted">—</span>
                        )}
                      </td>
                      <td>
                        {reportRef ? (
                          <span className="mono">{reportRef}</span>
                        ) : (
                          <span className="muted">—</span>
                        )}
                      </td>
                      <td className="number">{fmtDate(row.created_at)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}
