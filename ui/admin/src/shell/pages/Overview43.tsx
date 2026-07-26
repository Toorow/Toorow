import { useCallback, useEffect, useState } from "react";
import { ApiError, apiGet } from "../../lib/apiFetch";
import "../application.css";
import "./overview-evidence.css";

type SectionStatus = "ready" | "empty" | "unavailable";
type TrustStatus = "trusted" | "attention" | "unknown" | "no_data";
type DatastreamTarget = "overview" | "runs" | "first-publication";

interface LatestTest {
  status: SectionStatus;
  value: {
    passed: number;
    total: number;
    regressions: number;
    result: string;
    run_at: string | null;
  } | null;
}

interface OverviewSummary {
  published_trust: TrustStatus;
  complete_through: string | null;
  active_datastreams: number;
  no_data_reason: "empty_project" | "no_active_datastreams" | null;
  verified_datastreams: number;
  attention_count: number;
  active_work_count: number;
  evidence_message: string;
  latest_test: LatestTest;
}

interface AttentionItem {
  datastream_id: string;
  name: string;
  reason: string;
  detail: string;
  target: DatastreamTarget;
}

interface WorkItem {
  datastream_id: string;
  name: string;
  state: string;
  target: "runs";
}

interface DailyInsight {
  id: string;
  title: string;
  summary: string;
  confidence: string;
  created_at: string | null;
  render_snapshot_id: string | null;
}

interface RenderItem {
  id: string;
  kind: "get_card" | "get_report" | string;
  title: string;
  created_at: string | null;
  freshness: string | null;
}

interface ProjectOverviewData {
  summary: OverviewSummary;
  attention: AttentionItem[];
  attention_total: number;
  active_work: WorkItem[];
  daily_insights: { status: SectionStatus; items: DailyInsight[] };
  recent_renders: { status: SectionStatus; items: RenderItem[] };
}
function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object";
}

function isNullableString(value: unknown): boolean {
  return value === null || typeof value === "string";
}

function isSection(
  value: unknown,
  isItem: (item: unknown) => boolean,
): boolean {
  if (!isRecord(value) || !Array.isArray(value.items)) return false;
  if (!["ready", "empty", "unavailable"].includes(String(value.status))) {
    return false;
  }
  if (value.status === "ready") {
    return value.items.length > 0 && value.items.every(isItem);
  }
  return value.items.length === 0;
}

function isDailyInsight(item: unknown): boolean {
  return (
    isRecord(item) &&
    typeof item.id === "string" &&
    typeof item.title === "string" &&
    typeof item.summary === "string" &&
    typeof item.confidence === "string" &&
    isNullableString(item.created_at) &&
    isNullableString(item.render_snapshot_id)
  );
}

function isRenderItem(item: unknown): boolean {
  return (
    isRecord(item) &&
    typeof item.id === "string" &&
    typeof item.kind === "string" &&
    typeof item.title === "string" &&
    isNullableString(item.created_at) &&
    isNullableString(item.freshness)
  );
}

function isProjectOverviewData(value: unknown): value is ProjectOverviewData {
  if (!isRecord(value)) return false;
  const summary = value.summary;
  if (!isRecord(summary) || !isRecord(summary.latest_test)) return false;
  const latestTest = summary.latest_test;
  const validLatestTest =
    ["ready", "empty", "unavailable"].includes(String(latestTest.status)) &&
    (latestTest.status === "ready"
      ? isRecord(latestTest.value) &&
        typeof latestTest.value.passed === "number" &&
        typeof latestTest.value.total === "number" &&
        typeof latestTest.value.regressions === "number" &&
        typeof latestTest.value.result === "string" &&
        isNullableString(latestTest.value.run_at)
      : latestTest.value === null);
  const counts = [
    summary.active_datastreams,
    summary.verified_datastreams,
    summary.attention_count,
    summary.active_work_count,
  ];
  const trust = String(summary.published_trust);
  const validTrustEvidence =
    trust === "no_data"
      ? summary.complete_through === null &&
        ["empty_project", "no_active_datastreams"].includes(
          String(summary.no_data_reason),
        )
      : summary.no_data_reason === null &&
        (trust !== "trusted" ||
          (typeof summary.complete_through === "string" &&
            summary.verified_datastreams === summary.active_datastreams));
  return Boolean(
    ["trusted", "attention", "unknown", "no_data"].includes(trust) &&
    (summary.complete_through === null ||
      typeof summary.complete_through === "string") &&
    counts.every(
      (count) =>
        typeof count === "number" && Number.isInteger(count) && count >= 0,
    ) &&
    summary.verified_datastreams <= summary.active_datastreams &&
    typeof summary.evidence_message === "string" &&
    validTrustEvidence &&
    validLatestTest &&
    typeof value.attention_total === "number" &&
    Number.isInteger(value.attention_total) &&
    value.attention_total >= 0 &&
    Array.isArray(value.attention) &&
    value.attention_total >= value.attention.length &&
    value.attention.every(
      (item) =>
        isRecord(item) &&
        typeof item.datastream_id === "string" &&
        typeof item.name === "string" &&
        typeof item.reason === "string" &&
        typeof item.detail === "string" &&
        ["overview", "runs", "first-publication"].includes(String(item.target)),
    ) &&
    Array.isArray(value.active_work) &&
    value.active_work.every(
      (item) =>
        isRecord(item) &&
        typeof item.datastream_id === "string" &&
        typeof item.name === "string" &&
        typeof item.state === "string" &&
        item.target === "runs",
    ) &&
    isSection(value.daily_insights, isDailyInsight) &&
    isSection(value.recent_renders, isRenderItem),
  );
}
type FetchState =
  | { status: "loading" }
  | { status: "denied"; projectId: string }
  | { status: "error"; projectId: string }
  | { status: "ready"; projectId: string; data: ProjectOverviewData };

interface OverviewProps {
  projectId: string;
  onOpenDatastream?: (id: string, tab?: DatastreamTarget) => void;
  onAddDatastream?: () => void;
  onOpenDataOverview?: () => void;
  onOpenAnalyze?: () => void;
  onOpenRenders?: () => void;
  onOpenRender?: (id: string) => void;
}

const TRUST_LABEL: Record<TrustStatus, string> = {
  trusted: "Trusted",
  attention: "Needs attention",
  unknown: "Unknown",
  no_data: "No published data",
};

function formatDate(value: string | null): string {
  if (!value) return "Unavailable";
  const parsed = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) return "Unavailable";
  return parsed.toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
}

function formatTimestamp(value: string | null): string {
  if (!value) return "Time unavailable";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "Time unavailable";
  return parsed.toLocaleString("en-GB", {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function humanizeState(value: string): string {
  return value
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function OverviewSkeleton() {
  return (
    <div
      className="overview-skeleton"
      role="status"
      aria-label="Loading project evidence"
    >
      <div className="overview-skeleton-hero" />
      <div className="overview-skeleton-grid">
        {[1, 2, 3, 4].map((item) => (
          <div key={item} />
        ))}
      </div>
      <span className="sr-only">Loading project evidence</span>
    </div>
  );
}

function SectionMessage({
  status,
  empty,
  unavailable,
}: {
  status: SectionStatus;
  empty: string;
  unavailable: string;
}) {
  return (
    <p className="overview-section-message">
      {status === "empty" ? empty : unavailable}
    </p>
  );
}

export default function Overview({
  projectId,
  onOpenDatastream,
  onAddDatastream,
  onOpenDataOverview,
  onOpenAnalyze,
  onOpenRenders,
  onOpenRender,
}: OverviewProps) {
  const [state, setState] = useState<FetchState>({ status: "loading" });
  const [reloadKey, setReloadKey] = useState(0);

  const reload = useCallback(() => {
    setState({ status: "loading" });
    setReloadKey((value) => value + 1);
  }, []);

  useEffect(() => {
    let active = true;
    setState({ status: "loading" });
    void apiGet<unknown>(
      `/api/overview?project_id=${encodeURIComponent(projectId)}`,
    )
      .then((data) => {
        if (!isProjectOverviewData(data))
          throw new Error("Invalid overview response");
        if (active) setState({ status: "ready", projectId, data });
      })
      .catch((error: unknown) => {
        if (!active) return;
        if (
          error instanceof ApiError &&
          [403, 404].includes(error.status)
        ) {
          setState({ status: "denied", projectId });
          return;
        }
        setState({ status: "error", projectId });
      });
    return () => {
      active = false;
    };
  }, [projectId, reloadKey]);

  const visibleState: FetchState =
    state.status !== "loading" && state.projectId !== projectId
      ? { status: "loading" }
      : state;

  return (
    <>
      <div className="page-header overview-page-header">
        <div>
          <h1>Overview</h1>
          <p>
            Published-data trust, active work, and evidence that needs review.
          </p>
        </div>
        {visibleState.status === "ready" && (
          <button className="secondary-button" type="button" onClick={reload}>
            Refresh evidence
          </button>
        )}
      </div>

      {visibleState.status === "loading" && <OverviewSkeleton />}

      {visibleState.status === "denied" && (
        <section
          className="overview-state-card"
          aria-labelledby="overview-denied-title"
        >
          <span className="overview-eyebrow">Project scope</span>
          <h2 id="overview-denied-title">Project unavailable</h2>
          <p>
            This project does not exist or your current account cannot view it.
            Choose an authorized project from the scope control.
          </p>
        </section>
      )}

      {visibleState.status === "error" && (
        <section
          className="overview-state-card overview-state-error"
          aria-labelledby="overview-error-title"
          role="alert"
        >
          <span className="overview-eyebrow">Evidence unavailable</span>
          <h2 id="overview-error-title">Project trust could not be loaded</h2>
          <p>No cached figures or example data have been substituted.</p>
          <button className="primary-button" type="button" onClick={reload}>
            Retry
          </button>
        </section>
      )}

      {visibleState.status === "ready" && (
        <OverviewReady
          data={visibleState.data}
          onAddDatastream={onAddDatastream}
          onOpenAnalyze={onOpenAnalyze}
          onOpenDataOverview={onOpenDataOverview}
          onOpenDatastream={onOpenDatastream}
          onOpenRenders={onOpenRenders}
          onOpenRender={onOpenRender}
        />
      )}
    </>
  );
}

function OverviewReady({
  data,
  onAddDatastream,
  onOpenDataOverview,
  onOpenAnalyze,
  onOpenDatastream,
  onOpenRenders,
  onOpenRender,
}: {
  data: ProjectOverviewData;
  onAddDatastream?: () => void;
  onOpenDataOverview?: () => void;
  onOpenAnalyze?: () => void;
  onOpenDatastream?: (id: string, tab?: DatastreamTarget) => void;
  onOpenRenders?: () => void;
  onOpenRender?: (id: string) => void;
}) {
  const { summary } = data;
  const completion =
    summary.published_trust === "attention" && summary.complete_through
      ? `Last known good through ${formatDate(summary.complete_through)}`
      : summary.complete_through
        ? `Complete through ${formatDate(summary.complete_through)}`
        : "Completion unavailable";

  if (summary.published_trust === "no_data") {
    const emptyProject = summary.no_data_reason === "empty_project";
    const action = emptyProject ? onAddDatastream : onOpenDataOverview;
    return (
      <section
        className="overview-state-card"
        aria-labelledby="overview-empty-title"
      >
        <span className="overview-eyebrow">Published-data trust</span>
        <h2 id="overview-empty-title">
          {emptyProject ? "Add your first Datastream" : "No active Datastream"}
        </h2>
        {emptyProject ? (
          <p>
            Trust and completion appear after a Datastream has a current
            publication and successful completeness verification.
          </p>
        ) : (
          <p>
            Review the project Datastreams and enable the inputs that should
            contribute to published-data trust.
          </p>
        )}
        {action && (
          <button className="primary-button" type="button" onClick={action}>
            {emptyProject ? "Add Datastream" : "Review Datastreams"}
          </button>
        )}
      </section>
    );
  }

  return (
    <div className="overview-ledger">
      <section
        className={`overview-trust-panel trust-${summary.published_trust}`}
        aria-labelledby="overview-trust-title"
      >
        <div>
          <span className="overview-eyebrow">Published-data trust</span>
          <h2 id="overview-trust-title">
            {TRUST_LABEL[summary.published_trust]}
          </h2>
          <p>{summary.evidence_message}</p>
        </div>
        <div className="overview-completion">
          <span>Coverage horizon</span>
          <strong>{completion}</strong>
        </div>
      </section>

      <section className="overview-facts" aria-label="Project evidence summary">
        <article>
          <span>Verified inputs</span>
          <strong>
            {summary.verified_datastreams} / {summary.active_datastreams}
          </strong>
          <small>Active Datastreams with required evidence</small>
        </article>
        <article>
          <span>Needs attention</span>
          <strong>{summary.attention_count}</strong>
          <small>Datastreams with missing or adverse evidence</small>
        </article>
        <article>
          <span>Active work</span>
          <strong>{summary.active_work_count}</strong>
          <small>Persisted executions in progress</small>
        </article>
        <article>
          <span>Latest test run</span>
          <strong>
            {summary.latest_test.status === "ready" && summary.latest_test.value
              ? `${summary.latest_test.value.passed} / ${summary.latest_test.value.total}`
              : "Unavailable"}
          </strong>
          <small>
            {summary.latest_test.status === "ready" && summary.latest_test.value
              ? `${summary.latest_test.value.regressions} regressions`
              : summary.latest_test.status === "empty"
                ? "No persisted run"
                : "Evidence could not be loaded"}
          </small>
        </article>
      </section>

      <section
        className="panel overview-work-panel"
        aria-labelledby="overview-attention-title"
      >
        <div className="section-header">
          <div>
            <h2 id="overview-attention-title">Evidence requiring attention</h2>
            <p>
              Each item opens the exact Datastream evidence behind the status.
            </p>
          </div>
        </div>
        {data.attention.length === 0 ? (
          <p className="overview-section-message">
            No attention item is supported by the current evidence.
          </p>
        ) : (
          <>
            <ul className="overview-evidence-list">
              {data.attention.map((item) => (
                <li key={`${item.datastream_id}-${item.reason}`}>
                  <div>
                    <strong>{item.name}</strong>
                    <span>{item.detail}</span>
                  </div>
                  {onOpenDatastream && (
                    <button
                      className="action-link"
                      type="button"
                      onClick={() =>
                        onOpenDatastream(item.datastream_id, item.target)
                      }
                    >
                      View evidence
                    </button>
                  )}
                </li>
              ))}
            </ul>
            {data.attention_total > data.attention.length && (
              <div className="overview-bounded-notice">
                <p>
                  Showing {data.attention.length} of {data.attention_total}{" "}
                  evidence items.
                </p>
                {onOpenDataOverview && (
                  <button
                    className="action-link"
                    type="button"
                    onClick={onOpenDataOverview}
                  >
                    Review all Datastreams
                  </button>
                )}
              </div>
            )}
          </>
        )}
      </section>

      {data.active_work.length > 0 && (
        <section
          className="panel overview-work-panel"
          aria-labelledby="overview-work-title"
        >
          <div className="section-header">
            <div>
              <h2 id="overview-work-title">Active work</h2>
              <p>Current server-persisted publication executions.</p>
            </div>
          </div>
          <ul className="overview-evidence-list">
            {data.active_work.map((item) => (
              <li key={item.datastream_id}>
                <div>
                  <strong>{item.name}</strong>
                  <span>{humanizeState(item.state)}</span>
                </div>
                {onOpenDatastream && (
                  <button
                    className="action-link"
                    type="button"
                    onClick={() => onOpenDatastream(item.datastream_id, "runs")}
                  >
                    View Runs
                  </button>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      <div className="overview-artifact-grid">
        <section
          className="panel overview-artifact-panel"
          aria-labelledby="overview-insights-title"
        >
          <div className="section-header">
            <div>
              <h2 id="overview-insights-title">Daily insights</h2>
              <p>Persisted insights published for this project.</p>
            </div>
            {onOpenAnalyze && (
              <button
                className="secondary-button"
                type="button"
                onClick={onOpenAnalyze}
              >
                Open Analyze
              </button>
            )}
          </div>
          {data.daily_insights.status !== "ready" ? (
            <SectionMessage
              status={data.daily_insights.status}
              empty="No persisted daily insights yet."
              unavailable="Insight evidence could not be loaded."
            />
          ) : (
            <ul className="overview-artifact-list">
              {data.daily_insights.items.map((item) => (
                <li key={item.id}>
                  <strong>{item.title}</strong>
                  <span>{item.summary}</span>
                  <small>
                    {item.confidence} confidence ·{" "}
                    {formatTimestamp(item.created_at)}
                  </small>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section
          className="panel overview-artifact-panel"
          aria-labelledby="overview-renders-title"
        >
          <div className="section-header">
            <div>
              <h2 id="overview-renders-title">Recent renders</h2>
              <p>Authorized cards and reports conserved from the assistant.</p>
            </div>
            {onOpenRenders && (
              <button
                className="secondary-button"
                type="button"
                onClick={onOpenRenders}
              >
                View all
              </button>
            )}
          </div>
          {data.recent_renders.status !== "ready" ? (
            <SectionMessage
              status={data.recent_renders.status}
              empty="No persisted renders yet."
              unavailable="Render evidence could not be loaded."
            />
          ) : (
            <ul className="overview-artifact-list">
              {data.recent_renders.items.map((item) => (
                <li key={item.id}>
                  {onOpenRender ? (
                    <button type="button" onClick={() => onOpenRender(item.id)}>
                      <span>
                        {item.kind === "get_card" ? "Card" : "Report"}
                      </span>
                      <strong>{item.title}</strong>
                      <small>
                        {formatTimestamp(item.created_at)}
                        {item.freshness
                          ? ` · ${humanizeState(item.freshness)}`
                          : ""}
                      </small>
                    </button>
                  ) : (
                    <div>
                      <span>
                        {item.kind === "get_card" ? "Card" : "Report"}
                      </span>
                      <strong>{item.title}</strong>
                      <small>{formatTimestamp(item.created_at)}</small>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </div>
  );
}
