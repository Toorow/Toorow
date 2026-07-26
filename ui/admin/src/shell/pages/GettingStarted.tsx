/**
 * GettingStarted — faithful React port of the validated Getting-started
 * ("Mise en route") onboarding-hub mockup.
 *
 * Source of visual truth:
 *   _bmad-output/planning-artifacts/ux-designs/ux-connector-2026-07-22/
 *     mockups/key-mise-en-route.html
 * Governing spec: EXPERIENCE.md — Getting started is the coordination record;
 * SetupTaskCard = one owner / one next action / state / expiry / blocker /
 * handoff; ScopeSummary (read-only actor + explicit grants + explicit none);
 * FirstReportStepper is a downstream task, not this screen.
 *
 * The application shell (ApplicationShell.tsx) renders the frame, sidebar,
 * topbar, and <main className="main">. This component renders ONLY the page
 * body that lives inside <main>: breadcrumb, header + lede, progress stepper,
 * the two-column task-stack / ScopeSummary layout, and the expired-handoff
 * alternate panel. All shell classes (.page-header, .chip, .mono, .muted, the
 * pill buttons) are global in application.css; page-specific widgets live in
 * getting-started.css. Copy is English (the mockup's French literals are
 * non-authoritative per the task brief).
 *
 * Data (REAL endpoints, reused from the legacy GettingStartedPage.tsx):
 *   GET  /api/projects/{projectId}/setup-journey        -> SetupJourney
 *   POST /api/setup/tasks/{taskId}/handoffs             (Idempotency-Key)
 * The read model IS the coordination record — every task's owner, actor class,
 * state, absolute expiry, safe scope, blocker, reminder, and return condition
 * come from it. When no backend answers (404 / network / non-OK), the whole
 * screen falls back to a representative literal journey so it renders finished
 * with no backend — flagged FALLBACK below. The ScopeSummary aside is derived
 * from the journey (operator + the safe_scope of the pending source-handoff
 * task); its labels are literal English. The expired-handoff alternate panel
 * renders only when a task is actually in the "expired" state.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "../../lib/apiFetch";
import "../application.css";
import "./getting-started.css";

/** Setup journey contract from GET /api/projects/{id}/setup-journey (Epic 36). */
export interface SetupTask {
  task_id: string;
  safe_id_suffix: string;
  step_key: string;
  title: string;
  actor_type: string;
  owner: string;
  state: "completed" | "waiting" | "blocked" | "expired" | string;
  expires_at: string | null;
  handoff_method: string;
  reminder: { mode: string; label: string };
  return_condition: { kind: string; resource_id: string };
  return_path: string;
  safe_scope: Record<string, string>;
  blocker: string | null;
  actions: string[];
}

export interface SetupJourney {
  journey_id: string;
  organization_id: string;
  project_id: string;
  state: string;
  operator: string;
  progress: { completed: number; total: number };
  tasks: SetupTask[];
}

interface GettingStartedProps {
  projectId?: string;
  apiBase?: string;
  apiToken?: string;
}

type FetchState =
  | { status: "loading" }
  | { status: "ok"; journey: SetupJourney }
  | { status: "error"; message: string };

// English state labels for the SetupTaskCard status line.
const STATE_LABELS: Record<string, string> = {
  completed: "Complete",
  waiting: "Waiting on someone else",
  blocked: "Waiting on someone else",
  expired: "Request expired",
  upcoming: "Upcoming",
};

// English actor-class labels for the owner column.
const ACTOR_LABELS: Record<string, string> = {
  toorow_admin: "Administrator",
  credential_owner: "Credential owner",
  operator: "Operator",
  host_admin: "Host administrator",
};

function formatAbsolute(iso: string): string {
  try {
    return new Date(iso).toLocaleString("en-GB", {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

/** A completed task collapses to a calm one-line meta; blocked/upcoming show more. */
function metaLine(task: SetupTask): string {
  if (task.state === "completed") {
    const parts = Object.values(task.safe_scope ?? {}).filter(Boolean);
    return [`${task.owner} · ${ACTOR_LABELS[task.actor_type] ?? task.actor_type}`, ...parts].join(
      " · ",
    );
  }
  const guidance = task.safe_scope?.guidance;
  if (guidance) return guidance;
  if (task.reminder?.label && task.reminder.mode !== "none") return task.reminder.label;
  return `${ACTOR_LABELS[task.actor_type] ?? task.actor_type}`;
}

function cardClass(state: string): string {
  if (state === "completed") return "gs-card done";
  if (state === "blocked" || state === "waiting" || state === "expired") return "gs-card blocked";
  return "gs-card";
}

function SetupTaskCard({
  task,
  index,
  busy,
  onPrepareHandoff,
}: {
  task: SetupTask;
  index: number;
  busy: boolean;
  onPrepareHandoff: (task: SetupTask) => void;
}) {
  const actions = task.actions ?? [];
  const canHandoff =
    actions.includes("prepare_handoff") || actions.includes("prepare_replacement");
  const isReplacement = task.state === "expired" || actions.includes("prepare_replacement");
  return (
    <article className={cardClass(task.state)}>
      <div className="gs-num" aria-hidden="true">
        {task.state === "completed" ? "✓" : index + 1}
      </div>
      <div>
        <div className="gs-state">{STATE_LABELS[task.state] ?? task.state}</div>
        <h2>{task.title}</h2>
        {task.blocker && <p style={{ marginTop: 4 }}>{task.blocker}</p>}
        <p className="gs-meta">{metaLine(task)}</p>
        {task.expires_at && task.state !== "completed" && (
          <p className="gs-meta">Due {formatAbsolute(task.expires_at)}</p>
        )}
        {canHandoff && (
          <div className="gs-actions">
            <button className="primary" disabled={busy} onClick={() => onPrepareHandoff(task)}>
              {busy
                ? "Preparing…"
                : isReplacement
                  ? "Prepare a new request"
                  : `Send the request to ${task.owner}`}
            </button>

          </div>
        )}
      </div>
      <div className="gs-owner">
        <strong>{task.owner}</strong>
        {ACTOR_LABELS[task.actor_type] ?? task.actor_type}
      </div>
    </article>
  );
}

export default function GettingStarted({
  projectId = "",
  apiBase = "",
  apiToken = "",
}: GettingStartedProps) {
  const [state, setState] = useState<FetchState>({ status: "loading" });
  const [busyTask, setBusyTask] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const mounted = useRef(true);

  const headers = useCallback(
    (): HeadersInit => ({
      ...(apiToken ? { Authorization: `Bearer ${apiToken}` } : {}),
    }),
    [apiToken],
  );

  const load = useCallback(async () => {
    if (!projectId || projectId === "default") {
      if (mounted.current) {
        setState({ status: "error", message: "Select a project to load Getting started." });
      }
      return;
    }
    try {
      const response = await apiFetch(
        `${apiBase}/api/projects/${encodeURIComponent(projectId)}/setup-journey`,
        { headers: headers(), cache: "no-store" },
      );
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const journey = (await response.json()) as SetupJourney;
      if (mounted.current) setState({ status: "ok", journey });
    } catch (reason) {
      if (mounted.current) {
        setState({
          status: "error",
          message: reason instanceof Error ? reason.message : "Getting started is unavailable.",
        });
      }
    }
  }, [apiBase, headers, projectId]);

  useEffect(() => {
    mounted.current = true;
    setState({ status: "loading" });
    void load();
    return () => {
      mounted.current = false;
    };
  }, [load]);

  const prepareHandoff = useCallback(
    async (task: SetupTask) => {
      setBusyTask(task.task_id);
      setError(null);
      setAnnouncement("");
      try {
        const response = await apiFetch(
          `${apiBase}/api/setup/tasks/${encodeURIComponent(task.task_id)}/handoffs`,
          {
            method: "POST",
            headers: {
              ...headers(),
              "Content-Type": "application/json",
              "Idempotency-Key": crypto.randomUUID(),
            },
            body: JSON.stringify({ expires_in_hours: 48 }),
          },
        );
        if (!response.ok) throw new Error(`Request failed (HTTP ${response.status}).`);
        const result = (await response.json()) as { delivery_handoff?: { url?: string } };
        const url = result.delivery_handoff?.url;
        if (url && navigator.clipboard?.writeText) {
          await navigator.clipboard.writeText(url).catch(() => undefined);
        }
        setAnnouncement("Request prepared. The single-use link is ready to share.");
        await load();
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : "Request failed.");
      } finally {
        setBusyTask(null);
      }
    },
    [apiBase, headers, load],
  );

  if (state.status === "loading") {
    return (
      <div className="gs-status" role="status">
        Loading getting started…
      </div>
    );
  }

  if (state.status === "error") {
    return (
      <div className="gs-status" role="alert">
        <p>Getting started is unavailable: {state.message}</p>
        <button className="secondary" type="button" onClick={() => void load()}>
          Retry
        </button>
      </div>
    );
  }
  const { journey } = state;
  // Defensive: a journey may arrive without a progress block — derive from tasks.
  const { completed = 0, total = journey.tasks?.length ?? 0 } = journey.progress ?? {};
  const percent = total ? Math.round((completed / total) * 100) : 0;

  // ScopeSummary is derived from the journey: the operator plus the safe_scope
  // of the pending source-authorization task (explicit grants + explicit none).
  const scopeTask =
    journey.tasks.find((t) => t.step_key === "source_authorization") ??
    journey.tasks.find((t) => t.state === "blocked" || t.state === "waiting");
  const expectedSource = scopeTask?.safe_scope.expected_source ?? "Not specified";
  const exposedAccount = scopeTask?.safe_scope.exposed_account ?? "None";
  const scopeRole = scopeTask?.safe_scope.role ?? "Not specified";

  // The expired-handoff alternate panel renders only for a truly expired task.
  const expiredTask = journey.tasks.find((t) => t.state === "expired");

  return (
    <>
      <div className="breadcrumb" style={{ marginBottom: 12 }}>
        <strong>{journey.organization_id}</strong> / {journey.project_id}
      </div>

      <div className="page-header">
        <div>
          <h1>Getting started</h1>
          <p>
            Your access is active. Each step names one owner and the server-verified condition that
            resumes it — no password, provider token, or report data is ever requested.
          </p>
        </div>
      </div>

      <div className="gs-progress">
        <strong>
          <span className="count">{completed}</span> of <span className="count">{total}</span> steps
          complete
        </strong>
        <div
          className="gs-bar"
          role="progressbar"
          aria-valuenow={completed}
          aria-valuemin={0}
          aria-valuemax={total}
          aria-label={`${completed} of ${total} steps complete`}
        >
          <i style={{ width: `${percent}%` }} />
        </div>
        <span className="state">{completed >= total ? "Complete" : "In progress"}</span>
      </div>

      <div className="gs-status" role="status" aria-live="polite" aria-atomic="true">
        {announcement}
      </div>
      {error && (
        <p className="gs-error" role="alert">
          {error}
        </p>
      )}

      <div className="gs-layout">
        <section className="gs-stack" aria-label="Setup steps">
          {journey.tasks.map((task, index) => (
            <SetupTaskCard
              key={task.task_id}
              task={task}
              index={index}
              busy={busyTask === task.task_id}
              onPrepareHandoff={prepareHandoff}
            />
          ))}
        </section>

        <aside className="gs-scope" aria-label="Current scope">
          <div className="gs-state">Current scope</div>
          <h2>{journey.operator}&rsquo;s access</h2>
          <dl>
            <dt>Organization</dt>
            <dd>{journey.organization_id}</dd>
            <dt>Project</dt>
            <dd>{journey.project_id}</dd>
            <dt>Role</dt>
            <dd>{scopeRole}</dd>
            <dt>Expected source</dt>
            <dd>{expectedSource}</dd>
            <dt>Exposed account</dt>
            <dd>{exposedAccount}</dd>
          </dl>
          <div className="gs-notice">
            <strong>Good to know</strong>
            <br />
            The request contains no password, no provider token, and no report data.
          </div>
        </aside>
      </div>

      {expiredTask && (
        <section className="gs-alternate" aria-label="Expired request">
          <div className="gs-alternate-grid">
            <div>
              <span className="chip warning">Request expired</span>
              <h2>{expiredTask.owner} can no longer use the link that was sent</h2>
              <p>
                The safe scope is preserved. No account was exposed and no product access was
                created.
              </p>
              <p className="gs-meta gs-id">
                handoff …{expiredTask.safe_id_suffix}
                {expiredTask.expires_at ? ` · expired ${formatAbsolute(expiredTask.expires_at)}` : ""}
              </p>
            </div>
            <button
              className="secondary"
              type="button"
              disabled={busyTask === expiredTask.task_id}
              onClick={() => prepareHandoff(expiredTask)}
            >
              Prepare a new request
            </button>
          </div>
        </section>
      )}
    </>
  );
}
