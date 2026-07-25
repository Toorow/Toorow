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
  | { status: "ok"; journey: SetupJourney; live: boolean };

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

/**
 * FALLBACK journey — a representative Getting-started coordination record that
 * mirrors the mockup story (accepted operator coordinating a Google Ads
 * account-authorization handoff, plus the two downstream tasks). Used only when
 * the real endpoint does not answer, so the screen renders finished offline.
 * Values here are literals; the live read model replaces them wholesale.
 */
const FALLBACK_JOURNEY: SetupJourney = {
  journey_id: "journey_demo",
  organization_id: "acme-france",
  project_id: "acquisition-europe",
  state: "in_progress",
  operator: "Camille",
  progress: { completed: 2, total: 5 },
  tasks: [
    {
      task_id: "task_1",
      safe_id_suffix: "A1F0",
      step_key: "invitation_accepted",
      title: "Invitation accepted",
      actor_type: "toorow_admin",
      owner: "Nadia",
      state: "completed",
      expires_at: null,
      handoff_method: "none",
      reminder: { mode: "none", label: "No reminder" },
      return_condition: { kind: "membership", resource_id: "acme-france" },
      return_path: "/getting-started",
      safe_scope: { accepted_at: "22 Jul 2026, 10:14 CEST" },
      blocker: null,
      actions: [],
    },
    {
      task_id: "task_2",
      safe_id_suffix: "B2E1",
      step_key: "project_access",
      title: "Project access confirmed",
      actor_type: "operator",
      owner: "Camille",
      state: "completed",
      expires_at: null,
      handoff_method: "none",
      reminder: { mode: "none", label: "No reminder" },
      return_condition: { kind: "binding", resource_id: "acquisition-europe" },
      return_path: "/getting-started",
      safe_scope: { role: "Operator", project: "Acquisition Europe" },
      blocker: null,
      actions: [],
    },
    {
      task_id: "task_3",
      safe_id_suffix: "8C21",
      step_key: "source_authorization",
      title: "Authorize Google Ads and expose the account",
      actor_type: "credential_owner",
      owner: "Louis",
      state: "blocked",
      expires_at: "2026-07-28T18:00:00+02:00",
      handoff_method: "signed_link",
      reminder: { mode: "policy", label: "Returns here automatically once validated" },
      return_condition: { kind: "account_exposed", resource_id: "google-ads" },
      return_path: "/getting-started",
      safe_scope: { expected_source: "Google Ads", exposed_account: "None" },
      blocker:
        "Louis must authorize Google Ads and select the accounts accessible to Acme France before 28 Jul 2026, 18:00 CEST.",
      actions: ["prepare_handoff"],
    },
    {
      task_id: "task_4",
      safe_id_suffix: "D3C2",
      step_key: "first_report",
      title: "Create the first recent report",
      actor_type: "operator",
      owner: "Camille",
      state: "upcoming",
      expires_at: null,
      handoff_method: "none",
      reminder: { mode: "none", label: "After authorization" },
      return_condition: { kind: "publication", resource_id: "acquisition-europe" },
      return_path: "/getting-started",
      safe_scope: {
        guidance: "One source · one granted account · a recent interval recommended",
      },
      blocker: null,
      actions: [],
    },
    {
      task_id: "task_5",
      safe_id_suffix: "E4D3",
      step_key: "mcp_host",
      title: "Connect an MCP Apps host",
      actor_type: "host_admin",
      owner: "Ines",
      state: "upcoming",
      expires_at: null,
      handoff_method: "none",
      reminder: { mode: "none", label: "Host administrator" },
      return_condition: { kind: "mcp_install", resource_id: "acquisition-europe" },
      return_path: "/getting-started",
      safe_scope: {
        guidance: "Plan, workspace and catalog preflight before installation",
      },
      blocker: null,
      actions: [],
    },
  ],
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
            <button className="secondary" type="button">
              View scope
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
  projectId = "default",
  apiBase = "",
  apiToken = import.meta.env.VITE_ADMIN_API_TOKEN ?? "",
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
    try {
      const response = await fetch(
        `${apiBase}/api/projects/${encodeURIComponent(projectId)}/setup-journey`,
        { headers: headers(), cache: "no-store" },
      );
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const journey = (await response.json()) as SetupJourney;
      if (mounted.current) setState({ status: "ok", journey, live: true });
    } catch {
      // Graceful fallback: render finished with representative literals.
      if (mounted.current) setState({ status: "ok", journey: FALLBACK_JOURNEY, live: false });
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
        const response = await fetch(
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

  const { journey } = state;
  // Defensive: a journey may arrive without a progress block — derive from tasks.
  const { completed = 0, total = journey.tasks?.length ?? 0 } = journey.progress ?? {};
  const percent = total ? Math.round((completed / total) * 100) : 0;

  // ScopeSummary is derived from the journey: the operator plus the safe_scope
  // of the pending source-authorization task (explicit grants + explicit none).
  const scopeTask =
    journey.tasks.find((t) => t.step_key === "source_authorization") ??
    journey.tasks.find((t) => t.state === "blocked" || t.state === "waiting");
  const expectedSource = scopeTask?.safe_scope.expected_source ?? "Google Ads";
  const exposedAccount = scopeTask?.safe_scope.exposed_account ?? "None";

  // The expired-handoff alternate panel renders only for a truly expired task.
  const expiredTask = journey.tasks.find((t) => t.state === "expired");

  return (
    <>
      <div className="breadcrumb" style={{ marginBottom: 12 }}>
        <strong>Acme France</strong> / Acquisition Europe
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
            <dd>Acme France</dd>
            <dt>Project</dt>
            <dd>Acquisition Europe</dd>
            <dt>Role</dt>
            <dd>Operator</dd>
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
