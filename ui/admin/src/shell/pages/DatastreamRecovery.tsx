import { useEffect, useMemo, useState, type MouseEvent } from "react";
import type {
  BoundedConfirmResult,
  BoundedPreparation,
  DatastreamReadModel,
  DatastreamRun,
} from "../../datastreams/ops/opsTypes";
import { apiFetch } from "../../lib/apiFetch";
import "../application.css";
import "./datastream-recovery.css";

type Tab = "overview" | "data" | "mapping" | "runs";
type LoadState =
  | { status: "loading"; key: string }
  | { status: "ok"; key: string; model: DatastreamReadModel }
  | { status: "denied"; key: string; message: string }
  | { status: "error"; key: string; message: string };
type RecoveryState =
  | { status: "idle" }
  | { status: "preparing" }
  | { status: "review"; preparation: BoundedPreparation }
  | { status: "confirming"; preparation: BoundedPreparation }
  | { status: "confirmed"; result: BoundedConfirmResult }
  | { status: "outcome_unknown"; message: string }
  | { status: "error"; message: string };

interface DatastreamRecoveryProps {
  projectId: string;
  datastreamId: string;
  onNavigateTab?: (tab: Tab) => void;
}

type JsonRecord = Record<string, unknown>;
const FAILED_STATES = new Set(["failed", "error"]);

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function nullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}
function parseRun(value: unknown): DatastreamRun {
  if (
    !isRecord(value) ||
    typeof value.id !== "string" ||
    typeof value.state !== "string" ||
    !nullableString(value.created_at) ||
    !nullableString(value.state_changed_at) ||
    !nullableString(value.error_code) ||
    !nullableString(value.plan_version_id) ||
    !nullableString(value.mapping_version_id) ||
    !(value.row_count === null || typeof value.row_count === "number") ||
    !(value.duration_seconds === null || typeof value.duration_seconds === "number") ||
    !["current", "previously_published", "unpublished"].includes(String(value.publication_state))
  ) {
    throw new Error("The run evidence contract was invalid.");
  }
  if (value.recovery_interval !== null) {
    if (
      !isRecord(value.recovery_interval) ||
      typeof value.recovery_interval.from !== "string" ||
      typeof value.recovery_interval.to_exclusive !== "string"
    ) {
      throw new Error("The run interval contract was invalid.");
    }
  }
  return value as unknown as DatastreamRun;
}
function parseReadModel(value: unknown, projectId: string, datastreamId: string): DatastreamReadModel {
  if (
    !isRecord(value) ||
    value.project_id !== projectId ||
    typeof value.data_project_id !== "string" ||
    value.datastream_id !== datastreamId ||
    !isRecord(value.datastream) ||
    value.datastream.id !== datastreamId ||
    !Array.isArray(value.runs) ||
    !nullableString(value.current_published_execution_id) ||
    !(
      value.published_execution === null ||
      (isRecord(value.published_execution) && typeof value.published_execution.id === "string")
    )
  ) {
    throw new Error("The runs response did not match the requested project and Datastream.");
  }
  return { ...value, runs: value.runs.map(parseRun) } as unknown as DatastreamReadModel;
}
function formatDateTime(value: string | null): string {
  if (!value) return "Unknown";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? "Unknown"
    : parsed.toLocaleString("en-GB", {
        day: "2-digit",
        month: "short",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
}
function formatDuration(seconds: number | null): string {
  if (seconds === null) return "Unknown duration";
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `${minutes}m ${rest}s`;
}
function titleCase(value: string): string {
  return value.replace(/[_-]+/g, " ").replace(/\b\w/g, (character) => character.toUpperCase());
}
function tabHref(projectId: string, datastreamId: string, tab: Tab): string {
  return `/p/${encodeURIComponent(projectId)}/data/datastreams/o/datastream/${encodeURIComponent(datastreamId)}/${tab}`;
}
async function responseMessage(response: Response, fallback: string): Promise<string> {
  const body = (await response.json().catch(() => null)) as { message?: string } | null;
  return body?.message || fallback;
}

function parsePreparation(
  value: unknown,
  dataProjectId: string,
  datastreamId: string,
  interval: { from: string; to_exclusive: string },
): BoundedPreparation {
  if (
    !isRecord(value) ||
    typeof value.preparation_id !== "string" ||
    value.kind !== "reload" ||
    !isRecord(value.target) ||
    value.target.project_id !== dataProjectId ||
    value.target.datastream_id !== datastreamId ||
    !isRecord(value.target_versions) ||
    !nullableString(value.target_versions.plan_version_id) ||
    !nullableString(value.target_versions.mapping_version_id) ||
    typeof value.target_versions.policy_version !== "string" ||
    !isRecord(value.interval) ||
    value.interval.from !== interval.from ||
    value.interval.to_exclusive !== interval.to_exclusive ||
    !isRecord(value.impact) ||
    !isRecord(value.quota) ||
    typeof value.quota.platform_known !== "boolean" ||
    typeof value.quota.estimated_points !== "number" ||
    typeof value.quota.verdict !== "string" ||
    typeof value.quota.can_proceed !== "boolean" ||
    !nullableString(value.lock_ref) ||
    !nullableString(value.rollback_ref) ||
    !nullableString(value.reason) ||
    typeof value.expires_in_seconds !== "number"
  ) {
    throw new Error("The prepared recovery contract was invalid or did not match the selected run scope.");
  }
  return value as unknown as BoundedPreparation;
}

export default function DatastreamRecovery({
  projectId,
  datastreamId,
  onNavigateTab,
}: DatastreamRecoveryProps) {
  const [reload, setReload] = useState(0);
  const key = `${projectId}\u0000${datastreamId}\u0000${reload}`;
  const [state, setState] = useState<LoadState>({ status: "loading", key });
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [recovery, setRecovery] = useState<RecoveryState>({ status: "idle" });
  const visibleState = state.key === key ? state : ({ status: "loading", key } as LoadState);

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading", key });
    setSelectedRunId(null);
    setRecovery({ status: "idle" });
    const query = new URLSearchParams({ project_id: projectId });
    void apiFetch(
      `/api/datastreams/${encodeURIComponent(datastreamId)}/read-model?${query.toString()}`,
      { cache: "no-store" },
    )
      .then(async (response) => {
        if (!response.ok) {
          const error = new Error(
            await responseMessage(response, `Run evidence could not be loaded (HTTP ${response.status}).`),
          );
          if ([401, 403, 404].includes(response.status)) Object.assign(error, { denied: true });
          throw error;
        }
        return parseReadModel(await response.json(), projectId, datastreamId);
      })
      .then((model) => {
        if (!cancelled) setState({ status: "ok", key, model });
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        const message = error instanceof Error ? error.message : "Run evidence could not be loaded.";
        setState({
          status: isRecord(error) && error.denied === true ? "denied" : "error",
          key,
          message,
        });
      });
    return () => {
      cancelled = true;
    };
  }, [datastreamId, key, projectId]);

  const model = visibleState.status === "ok" ? visibleState.model : null;
  const runs = model?.runs ?? [];
  const selectedRun = useMemo(
    () =>
      runs.find((run) => run.id === selectedRunId) ??
      runs.find((run) => FAILED_STATES.has(run.state.toLowerCase())) ??
      runs[0] ??
      null,
    [runs, selectedRunId],
  );
  const name = model?.datastream.name?.trim() || datastreamId;
  const source =
    model?.datastream.module_name?.trim() ||
    model?.datastream.source_kind?.trim() ||
    "Source unavailable";
  const currentPublishedId = model?.current_published_execution_id ?? null;
  const recoveryAvailable = Boolean(
    selectedRun &&
      FAILED_STATES.has(selectedRun.state.toLowerCase()) &&
      selectedRun.recovery_interval,
  );

  const navigate = (event: MouseEvent<HTMLAnchorElement>, tab: Tab) => {
    if (
      !onNavigateTab ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    ) {
      return;
    }
    event.preventDefault();
    onNavigateTab(tab);
  };
  const chooseRun = (runId: string) => {
    setSelectedRunId(runId);
    setRecovery({ status: "idle" });
  };

  async function prepareRecovery() {
    if (!selectedRun?.recovery_interval || !FAILED_STATES.has(selectedRun.state.toLowerCase())) return;
    setRecovery({ status: "preparing" });
    try {
      const response = await apiFetch(
        `/api/datastreams/${encodeURIComponent(datastreamId)}/bounded/prepare`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            project_id: projectId,
            kind: "reload",
            date_from: selectedRun.recovery_interval.from,
            date_to_exclusive: selectedRun.recovery_interval.to_exclusive,
            reason: `Retry failed run ${selectedRun.id}`,
          }),
        },
      );
      if (!response.ok) {
        throw new Error(await responseMessage(response, `Recovery could not be prepared (HTTP ${response.status}).`));
      }
      const preparation = parsePreparation(
        await response.json(),
        model?.data_project_id ?? "",
        datastreamId,
        selectedRun.recovery_interval,
      );
      setRecovery({ status: "review", preparation });
    } catch (error) {
      setRecovery({
        status: "error",
        message: error instanceof Error ? error.message : "Recovery could not be prepared.",
      });
    }
  }

  async function confirmRecovery(preparation: BoundedPreparation) {
    setRecovery({ status: "confirming", preparation });
    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => controller.abort(), 30_000);
    try {
      const response = await apiFetch(
        `/api/datastreams/${encodeURIComponent(datastreamId)}/bounded/confirm`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ project_id: projectId, preparation_id: preparation.preparation_id }),
          signal: controller.signal,
        },
      );
      const body = (await response.json().catch(() => null)) as
        | (Partial<BoundedConfirmResult> & { message?: string; code?: string })
        | null;
      if (!response.ok) {
        if (body?.outcome === "outcome_unknown" || body?.code === "outcome_unknown") {
          setRecovery({
            status: "outcome_unknown",
            message: body.message || "The durable recovery outcome could not be confirmed.",
          });
          return;
        }
        const message = body?.message || `Recovery confirmation failed (HTTP ${response.status}).`;
        if (response.status >= 400 && response.status < 500) {
          setRecovery({ status: "error", message });
          return;
        }
        throw new Error(message);
      }
      if (
        !body ||
        body.preparation_id !== preparation.preparation_id ||
        typeof body.operation_id !== "string" ||
        typeof body.trace_id !== "string" ||
        typeof body.replayed !== "boolean" ||
        !["succeeded", "failed", "outcome_unknown"].includes(String(body.outcome))
      ) {
        throw new Error("The recovery confirmation response was invalid.");
      }
      if (body.outcome === "outcome_unknown") {
        setRecovery({ status: "outcome_unknown", message: "The durable recovery outcome is unknown." });
        return;
      }
      setRecovery({ status: "confirmed", result: body as BoundedConfirmResult });
    } catch (error) {
      setRecovery({
        status: "outcome_unknown",
        message:
          error instanceof Error
            ? error.message
            : "The confirmation response was lost before its durable outcome could be verified.",
      });
    } finally {
      window.clearTimeout(timeoutId);
    }
  }

  return (
    <div className="workflow-main">
      <div className="object">
        <div className="object-logo provider-logo" aria-hidden="true">
          {name.slice(0, 1).toUpperCase()}
        </div>
        <div>
          <strong>{name}</strong>
          <span>{source}</span>
        </div>
      </div>
      <nav className="local-tabs" aria-label="Datastream">
        {(["overview", "data", "mapping", "runs"] as const).map((tab) => (
          <a
            key={tab}
            className={`local-tab${tab === "runs" ? " active" : ""}`}
            aria-current={tab === "runs" ? "page" : undefined}
            href={tabHref(projectId, datastreamId, tab)}
            onClick={(event) => navigate(event, tab)}
          >
            {titleCase(tab)}
          </a>
        ))}
        <span className="local-tab" aria-disabled="true">Processing unavailable</span>
        <span className="local-tab" aria-disabled="true">Outputs unavailable</span>
      </nav>

      {visibleState.status === "loading" && (
        <section className="panel" role="status">
          <h2>Loading run evidence...</h2>
          <p>No execution or recovery evidence is shown until the scoped read completes.</p>
        </section>
      )}
      {visibleState.status === "denied" && (
        <section className="panel" role="alert">
          <h2>Run access unavailable</h2>
          <p>{visibleState.message}</p>
        </section>
      )}
      {visibleState.status === "error" && (
        <section className="panel" role="alert">
          <h2>Run evidence unavailable</h2>
          <p>{visibleState.message}</p>
          <button className="secondary-button" type="button" onClick={() => setReload((value) => value + 1)}>
            Retry
          </button>
        </section>
      )}
      {model && runs.length === 0 && (
        <section className="panel" data-testid="runs-empty">
          <h2>No runs</h2>
          <p>No execution has been persisted for this Datastream.</p>
        </section>
      )}

      {model && runs.length > 0 && (
        <div className="recovery-stage">
          <section className="run-content">
            <div className="page-header">
              <div>
                <h1>Runs</h1>
                <p>Universal execution history from persisted Datastream executions.</p>
              </div>
            </div>
            <div className="mapping-panel">
              <div className="mapping-toolbar">
                <h2>Run history</h2>
                <span>{runs.length} persisted execution{runs.length === 1 ? "" : "s"}</span>
              </div>
              <table className="run-table">
                <thead>
                  <tr>
                    <th>Status</th>
                    <th>Started</th>
                    <th>Rows</th>
                    <th>Publication</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.map((run) => {
                    const selected = run.id === selectedRun?.id;
                    const failed = FAILED_STATES.has(run.state.toLowerCase());
                    return (
                      <tr
                        key={run.id}
                        className={selected ? "selected" : undefined}
                        onClick={() => chooseRun(run.id)}
                        aria-selected={selected}
                        data-testid={`run-${run.id}`}
                      >
                        <td>
                          <span className={`signal-label ${failed ? "error" : "success"}`}>
                            <span className="signal-mark" />
                            {titleCase(run.state)}
                          </span>
                          <small className="event">{run.error_code || run.id}</small>
                        </td>
                        <td>
                          <strong>{formatDateTime(run.created_at)}</strong>
                          <small className="number">{formatDuration(run.duration_seconds)}</small>
                        </td>
                        <td>
                          <strong className="number">{run.row_count === null ? "Unknown" : run.row_count.toLocaleString("en-US")}</strong>
                          <small>{run.import_evidence ? `Ledger: ${run.import_evidence.outcome || "unknown"}` : "No ledger enrichment"}</small>
                        </td>
                        <td>
                          <strong>{titleCase(run.publication_state)}</strong>
                          <small>{run.id === currentPublishedId ? "Last-known-good pointer" : "Published pointer unchanged"}</small>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>

          <aside className="recovery-drawer" aria-label="Recovery details">
            <div className="drawer-header"><h2>Bounded recovery</h2></div>
            <div className="drawer-body">
              <section className="drawer-section">
                <h3>Selected execution</h3>
                <dl className="drawer-meta">
                  <dt>Run ID</dt><dd className="event">{selectedRun?.id}</dd>
                  <dt>State</dt><dd>{selectedRun ? titleCase(selectedRun.state) : "Unknown"}</dd>
                  <dt>Plan</dt><dd className="event">{selectedRun?.plan_version_id || "Unknown"}</dd>
                  <dt>Mapping</dt><dd className="event">{selectedRun?.mapping_version_id || "Unknown"}</dd>
                  <dt>Interval</dt><dd>{selectedRun?.recovery_interval ? `${selectedRun.recovery_interval.from} to ${selectedRun.recovery_interval.to_exclusive} (exclusive)` : "Unavailable"}</dd>
                </dl>
              </section>
              <section className="drawer-section">
                <h3>Last-known-good</h3>
                <p>{currentPublishedId ? <>Published execution <span className="event">{currentPublishedId}</span> remains authoritative.</> : "No published execution pointer is available."}</p>
              </section>

              {recovery.status === "review" || recovery.status === "confirming" ? (
                <section className="drawer-section" aria-live="polite">
                  <h3>Review immutable proposal</h3>
                  <dl className="drawer-meta">
                    <dt>Proposal</dt><dd className="event">{recovery.preparation.preparation_id}</dd>
                    <dt>Interval</dt><dd>{recovery.preparation.interval ? `${recovery.preparation.interval.from} to ${recovery.preparation.interval.to_exclusive} (exclusive)` : "Unavailable"}</dd>
                    <dt>Quota</dt><dd>{recovery.preparation.quota.estimated_points} points - {recovery.preparation.quota.verdict}</dd>
                    <dt>Impact</dt><dd><code>{JSON.stringify(recovery.preparation.impact)}</code></dd>
                    <dt>Lock</dt><dd className="event">{recovery.preparation.lock_ref || "None"}</dd>
                    <dt>Rollback</dt><dd className="event">{recovery.preparation.rollback_ref || "None"}</dd>
                  </dl>
                  <button
                    className="primary-button"
                    type="button"
                    disabled={recovery.status === "confirming"}
                    onClick={() => void confirmRecovery(recovery.preparation)}
                  >
                    {recovery.status === "confirming" ? "Confirming..." : "Confirm exact recovery"}
                  </button>
                </section>
              ) : null}
              {recovery.status === "confirmed" && (
                <section className="drawer-section" aria-live="polite" data-testid="recovery-result">
                  <h3>Durable recovery outcome</h3>
                  <dl className="drawer-meta">
                    <dt>Outcome</dt><dd>{titleCase(recovery.result.outcome)}</dd>
                    <dt>Replayed</dt><dd>{recovery.result.replayed ? "Yes" : "No"}</dd>
                    <dt>Operation</dt><dd className="event">{recovery.result.operation_id}</dd>
                    <dt>Trace</dt><dd className="event">{recovery.result.trace_id || "Unavailable"}</dd>
                  </dl>
                </section>
              )}
              {recovery.status === "outcome_unknown" && (
                <section className="drawer-section" role="alert" data-testid="outcome-unknown">
                  <h3>Outcome unknown</h3>
                  <p>{recovery.message} Reload this scoped run history before attempting another action.</p>
                </section>
              )}
              {recovery.status === "error" && <section className="drawer-section" role="alert"><p>{recovery.message}</p></section>}

              <div className="drawer-actions">
                <button
                  className="primary-button"
                  type="button"
                  disabled={!recoveryAvailable || recovery.status === "preparing" || recovery.status === "confirming" || recovery.status === "review"}
                  title={!recoveryAvailable ? "Recovery requires a failed run with an exact persisted half-open interval." : undefined}
                  onClick={() => void prepareRecovery()}
                >
                  {recovery.status === "preparing" ? "Preparing..." : "Prepare exact retry"}
                </button>
                <button className="secondary-button" type="button" disabled title="No governed technical-events route is available for this execution.">
                  Technical events unavailable
                </button>
              </div>
            </div>
          </aside>
        </div>
      )}
    </div>
  );
}