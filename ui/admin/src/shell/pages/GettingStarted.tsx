import { useEffect, useState } from "react";
import { apiFetch, apiGet } from "../../lib/apiFetch";
import { ActivityLog, Button, EmptyState, formatPercent, NO_VALUE, Panel, PanelHeader, percentValue, Progress, stateLabel, Status } from "../../ui";
import GlobalScopeLayout from "../GlobalScopeLayout";

export interface SetupOwnerReference { surface: "project" | "global"; workspace?: string | null; section?: string | null; global_surface?: string | null; global_section?: string | null; object_type?: string | null; object_id?: string | null; tab?: string | null; action?: string | null; version_id?: string | null; evidence_id?: string | null; }
export interface SetupTask { id?: string; safe_id_suffix?: string; actor_type?: string; expires_at?: string | null; handoff_method?: string; reminder?: { mode: string; label: string }; return_path?: string; safe_scope?: Record<string, string>; task_id?: string; step_key: string; title?: string; state: string; owner: string | { actor_type?: string; identity?: string | null; label?: string }; authoritative_owner_ref?: SetupOwnerReference; readiness_ref?: Record<string, unknown> | null; handoff_summary?: { state?: string; expires_at?: string | null } | null; return_condition?: Record<string, unknown> | null; resume_ref?: string | null; blocker?: string | null; actions?: string[]; }
export interface SetupJourney { schema_version?: string; organization_id?: string; project_id?: string; operator?: string; project?: { id: string; name: string; organization: { id: string; name: string } | null }; materialization?: { state: "materialized" | "pending"; action: string | null; journal_behind?: boolean; explanation: string }; journey?: { id: string | null; state: string; started_at?: string | null; completed_at?: string | null; progress: { completed?: number; total?: number; percent?: number }; next_task_id?: string | null }; journey_id?: string; state?: string; progress?: { completed: number; total: number }; tasks: SetupTask[]; history?: Array<{ event_id: string; task_id?: string | null; event_type: string; actor: string; occurred_at: string; safe_refs?: Record<string, unknown> }>; readiness?: { version: string; components?: string[]; [component: string]: unknown }; }

/** THE SCOPE A PERSON READS, NEVER THE ONE THE DATABASE STORES.
 *
 *  This was `scopeLabel={projectId}`, so the eyebrow read "Project coordination ·
 *  proj_01K…". `GlobalScopeLayout` documents that exact string as the reason the
 *  "Current scope" panel was deleted — and it came straight back one prop over.
 *  The envelope carries the Project and Organization NAMES now; before they
 *  arrive, the honest label is the word "Project", never an identifier. */
function scopeLabelOf(data: SetupJourney | null): string {
  const project = data?.project;
  if (!project?.name) return "Project";
  return project.organization?.name ? `${project.organization.name} / ${project.name}` : project.name;
}

/** Titlecased component name, so "project_foundation" reads as "Project foundation". */
function componentLabel(key: string): string {
  const words = key.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export default function GettingStarted({ projectId = "", onOpenOwner }: { projectId?: string; onOpenOwner?: (owner: SetupOwnerReference) => void }) {
  const [state, setState] = useState<{ kind: "loading" } | { kind: "error"; message: string } | { kind: "ready"; data: SetupJourney }>({ kind: "loading" });
  const [busyTask, setBusyTask] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const load = () => { setState({ kind: "loading" }); apiGet<SetupJourney>(`/api/projects/${encodeURIComponent(projectId)}/getting-started`).then((data) => setState({ kind: "ready", data })).catch(() => setState({ kind: "error", message: "Getting Started is unavailable. No task was marked complete." })); };
  useEffect(load, [projectId]);
  const prepareHandoff = async (taskId: string) => { setBusyTask(taskId); try { const response = await apiFetch(`/api/projects/${encodeURIComponent(projectId)}/getting-started/tasks/${encodeURIComponent(taskId)}/handoffs`, { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify({ expires_in_hours: 48 }) }); if (!response.ok) throw new Error("handoff failed"); await load(); } catch { setState({ kind: "error", message: "The handoff was not created. The existing journey remains unchanged." }); } finally { setBusyTask(null); } };
  // THE GESTURE THE EMPTY STATE NAMES. Opening this page no longer creates the
  // journey — a read does not write — so a Project that has none says so and
  // offers the one explicit action that fills it.
  const startJourney = async () => { setStarting(true); try { const response = await apiFetch(`/api/projects/${encodeURIComponent(projectId)}/getting-started/journey`, { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() } }); if (!response.ok) throw new Error("materialization failed"); await load(); } catch { setState({ kind: "error", message: "The setup journey was not started. Nothing was changed." }); } finally { setStarting(false); } };
  const data = state.kind === "ready" ? state.data : null; const journey = data?.journey; const complete = journey?.progress.completed ?? data?.progress?.completed ?? 0; const total = journey?.progress.total ?? data?.progress?.total ?? data?.tasks.length ?? 0; const nextId = journey?.next_task_id ?? data?.tasks.find((task) => !["completed", "not_applicable"].includes(task.state))?.id ?? data?.tasks.find((task) => !["completed", "not_applicable"].includes(task.state))?.task_id;
  const pending = data?.materialization?.state === "pending";
  const readinessComponents = data?.readiness?.components ?? ["project_foundation", "source", "datastream", "governance", "first_value"];
  return <GlobalScopeLayout eyebrow="Project coordination" title="Getting Started" description="One durable journey coordinates setup while every object stays owned by its real workbench." scopeLabel={scopeLabelOf(data)} sections={[{ key: "journey", label: "Journey", description: "Next action, owners and retained history" }] as const} activeSection="journey" onSectionChange={() => undefined}>
    {state.kind === "loading" ? <Status as="block" active title="Loading setup journey">Reconciling authoritative readiness.</Status> : null}
    {state.kind === "error" ? <Status as="block" tone="error" title="Getting Started unavailable"
          action={<Retry onClick={() => void load()} />}
        >{state.message}</Status> : null}
    {data ? <div className="space-y-6"><Panel><PanelHeader title="Journey posture" description={`Readiness version: ${data.readiness?.version ?? "Unavailable"}`} /><div className="space-y-3 p-5">{/* "0 of 0" IS NOT A DENOMINATOR. A Project with no recorded journey has no
            steps to be behind on; saying so is a different fact from being at 0%. */}
<div className="flex items-center justify-between text-ui"><span>{total ? `${complete} of ${total} steps complete` : "No steps are recorded yet"}</span><strong>{total ? formatPercent(complete / total) : NO_VALUE}</strong></div>{total ? <Progress value={percentValue(complete / total)} aria-label="Setup steps complete" /> : null}{/* ITERATED, NOT HARDCODED. Four spans named four components while the
            projection had five — the Governance step was simply absent from the
            posture a person reads. The server names the order in
            `readiness.components`, so a sixth component arrives here on its own. */}
<div className="grid gap-2 text-caption text-text-secondary sm:grid-cols-3 lg:grid-cols-5">{readinessComponents.map((key) => <span key={key}>{componentLabel(key)}: {typeof data.readiness?.[key] === "string" ? String(data.readiness[key]) : "Unknown"}</span>)}</div></div></Panel><Panel><PanelHeader title="Setup tasks" description="The single next action is highlighted; completion comes only from owner evidence." />{data.tasks.length === 0 ? <EmptyState title={pending ? "This Project has no setup journey yet" : "No setup tasks"} description={pending ? `${data.materialization?.explanation ?? ""} Starting it records the five steps and who owns each one.` : "The journey exists but has no persisted tasks."} action={pending ? <Button type="button" disabled={starting} onClick={() => void startJourney()}>{starting ? "Starting…" : "Start setup journey"}</Button> : undefined} /> :<ol className="divide-y divide-divider-base">{data.tasks.map((task) => { const id = task.id ?? task.task_id ?? task.step_key; const owner = typeof task.owner === "string" ? task.owner : task.owner.label ?? task.owner.identity ?? task.owner.actor_type ?? "Unassigned"; const isNext = id === nextId; return <li key={id} className={`space-y-3 p-5 ${isNext ? "bg-primary-container text-on-primary" : ""}`}><div className="flex flex-wrap items-start justify-between gap-3"><div><span className="font-mono text-caption uppercase tracking-wide">{task.step_key}</span><h3 className="mt-1 text-ui font-semibold">{task.title ?? task.step_key.replaceAll("_", " ")}</h3><p className="text-caption text-text-secondary">Owner: {owner}</p></div><Status tone={task.state === "completed" ? "success" : task.state === "failed" || task.state === "expired" ? "error" : task.state === "blocked" ? "warning" : "neutral"}>{stateLabel(task.state)}</Status></div>{task.blocker ? <p className="text-body">{task.blocker}</p> : null}<div className="flex flex-wrap gap-2">{task.authoritative_owner_ref && onOpenOwner ? <Button type="button" variant={isNext ? "default" : "secondary"} onClick={() => onOpenOwner(task.authoritative_owner_ref!)}>Open owner</Button> : null}{task.actions?.includes("prepare_handoff") ? <Button type="button" variant="secondary" disabled={busyTask === id} onClick={() => void prepareHandoff(id)}>{busyTask === id ? "Preparing…" : "Prepare handoff"}</Button> : null}</div></li>; })}</ol>}</Panel><Panel><PanelHeader title="Journey history" description="Structured coordination evidence with safe references only." />{data.history?.length ? <div className="p-5"><ActivityLog entries={data.history.map((event) => ({ children: event.event_type.replaceAll("_", " "), at: `${event.actor} - ${event.occurred_at}`, tone: "neutral" as const }))} /></div> : <EmptyState title="No journey history yet" description="History will appear when tasks are assigned, handed off, reconciled or completed." />}</Panel></div> : null}
  </GlobalScopeLayout>;
}