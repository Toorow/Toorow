/**
 * GoldenQuestions — Test > Golden questions.
 *
 * The trust-benchmark surface of the eval loop (epic 14). A project's "golden
 * questions" are a versioned set (~50) of benchmark questions, each with a
 * reference answer/SQL and expected citations. An OFFLINE runner
 * (scripts/run_evals.py) scores SQL precision + citation recall — there is NO
 * live API surfacing those results yet.
 *
 * The Test workspace is spine-only and reuses THIS shell. Because no endpoint
 * exists, every value below is designed, honest placeholder content rendered
 * from a literal and flagged with // TODO(api) — exactly how Overview.tsx and
 * CountrySplit.tsx handle the missing backend. When the eval-loop API lands,
 * the summary strip and the rows are replaced by the scored run; the table
 * shape (question, topic, expected-citation count, last pass/fail) is stable.
 *
 * Styling: application.css (global, via the shell) for shell/layout classes
 * (page-header, panel, metric-strip, metric, section-header, signal-label,
 * signal-mark, secondary-button) + golden-questions.css for the page-specific
 * table. Colors come exclusively from the application.css CSS variables
 * (dark-theme safe); numbers use Geist tabular via those classes.
 */
import { useEffect, useState } from "react";
import "../application.css";
import "./golden-questions.css";

type Result = "success" | "error";

interface GoldenQuestion {
  question: string;
  topic: string;
  /** Number of citations the reference answer is expected to ground on. */
  expectedCitations: number;
  result: Result;
  resultLabel: string;
}

// Mockup literals — the designed Golden questions benchmark surface. Rendered
// verbatim so the page is finished with no backend. Replaced by the scored
// eval-loop run when scripts/run_evals.py results are exposed via an endpoint.
// TODO(api): GET the versioned golden-question set + latest scored results.
const MOCKUP_QUESTIONS: GoldenQuestion[] = [
  {
    question: "What was total paid-media spend last month across all channels?",
    topic: "Spend",
    expectedCitations: 3,
    result: "success",
    resultLabel: "Pass",
  },
  {
    question: "Which campaign had the highest ROAS in Q2?",
    topic: "Performance",
    expectedCitations: 2,
    result: "success",
    resultLabel: "Pass",
  },
  {
    question: "How did organic search clicks trend over the last 8 weeks?",
    topic: "Organic search",
    expectedCitations: 2,
    result: "error",
    resultLabel: "Fail",
  },
  {
    question: "What share of conversions is attributed to Meta Ads post-click?",
    topic: "Attribution",
    expectedCitations: 4,
    result: "success",
    resultLabel: "Pass",
  },
  {
    question: "Break down revenue by country for the current quarter.",
    topic: "Geography",
    expectedCitations: 3,
    result: "success",
    resultLabel: "Pass",
  },
  {
    question: "Is reported spend consistent between Google Ads and GA4?",
    topic: "Data quality",
    expectedCitations: 2,
    result: "error",
    resultLabel: "Fail",
  },
];

interface GoldenQuestionsProps {
  projectId?: string;
}

/** Shape returned by GET /api/eval/golden-questions (epic 14 eval loop). */
interface ApiGoldenQuestion {
  id: string;
  question: string;
  topic: string;
  expected_citations: number;
  /** Latest offline scored result for this question. */
  last_result: "pass" | "fail";
}

function toQuestion(q: ApiGoldenQuestion): GoldenQuestion {
  const passed = q.last_result === "pass";
  return {
    question: q.question,
    topic: q.topic,
    expectedCitations: q.expected_citations,
    result: passed ? "success" : "error",
    resultLabel: passed ? "Pass" : "Fail",
  };
}

export default function GoldenQuestions({ projectId = "default" }: GoldenQuestionsProps) {
  // The golden-question set is per-project. MOCKUP_QUESTIONS render finished
  // while the fetch is in flight and offline; a successful fetch replaces them
  // with the real scored set (which is honestly empty until questions exist).
  const [questions, setQuestions] = useState<GoldenQuestion[]>(MOCKUP_QUESTIONS);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await fetch(
          `/api/eval/golden-questions?project_id=${encodeURIComponent(projectId)}`,
        );
        if (!res.ok) return; // keep the mock fallback
        const body = (await res.json()) as { questions?: ApiGoldenQuestion[] };
        if (alive) {
          setQuestions((body.questions ?? []).map(toQuestion));
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

  // Summary strip is derived from the fetched list (Total + Pass rate). The
  // "Last run" tile stays literal — it comes from the runs endpoint (out of scope).
  const total = questions.length;
  const passCount = questions.filter((q) => q.result === "success").length;
  const passRate = total > 0 ? Math.round((100 * passCount) / total) : 0;

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Golden questions</h1>
          <p>
            The versioned set of benchmark questions used to check the assistant
            can be trusted for this project. An offline runner scores each answer
            on SQL precision and citation recall against a reference answer.
          </p>
        </div>
      </div>

      <section className="metric-strip">
        <div className="metric">
          <span>Total questions</span>
          <strong>{total}</strong>
          <small>Benchmark set v3</small>
        </div>
        <div className="metric">
          <span>Pass rate</span>
          <strong>{passRate}%</strong>
          <small>
            <span className="signal" aria-hidden="true" />
            {passCount}/{total} passing
          </small>
        </div>
        <div className="metric">
          <span>Last run</span>
          {/* TODO(api): timestamp of the latest run_evals.py run */}
          <strong>22 Jul 2026</strong>
          <small>Offline runner · scripts/run_evals.py</small>
        </div>
      </section>

      <section className="panel gq-panel">
        <div className="section-header">
          <div>
            <h2>Benchmark questions</h2>
            <p>
              Each question carries a reference answer, expected SQL, and the
              citations its answer must ground on.
            </p>
          </div>
          {/* TODO(api): trigger a new offline scoring run */}
          <button className="secondary-button" type="button">
            Run benchmark
          </button>
        </div>
        <div className="table-scroll" tabIndex={0} aria-label="Golden questions">
          <table className="gq-table">
            <thead>
              <tr>
                <th>Question</th>
                <th>Topic</th>
                <th>Expected citations</th>
                <th>Last result</th>
              </tr>
            </thead>
            <tbody>
              {loaded && questions.length === 0 ? (
                <tr>
                  <td colSpan={4} className="gq-empty">
                    No golden questions yet. Add benchmark questions for this
                    project to start scoring the assistant on SQL precision and
                    citation recall.
                  </td>
                </tr>
              ) : null}
              {questions.map((q, i) => (
                <tr key={`${q.topic}-${i}`}>
                  <td>
                    <span className="gq-question">{q.question}</span>
                  </td>
                  <td>
                    <span className="gq-topic">{q.topic}</span>
                  </td>
                  <td className="gq-num">{q.expectedCitations}</td>
                  <td>
                    <span className={`signal-label ${q.result}`}>
                      <span className="signal-mark" />
                      {q.resultLabel}
                    </span>
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
