/**
 * DatastreamRecovery — faithful React port of the validated Datastream run
 * recovery mockup.
 *
 * Source of visual truth:
 *   _bmad-output/planning-artifacts/ux-designs/ux-connector-2026-07-23/
 *     mockups/datastream-recovery.html   (+ workflow-surfaces.css)
 *
 * The datastream "Runs" tab body: an execution-history table where each row
 * carries a run state (Failed / Complete) alongside its Started time+duration,
 * Rows, and Publication (last-known-good preservation). The FAILED run is
 * selected and opens a recovery drawer that states the failed scope
 * (datastream, partition, stage, authorization, run id), the recommended
 * bounded action, the evidence, and the retry/technical-events actions.
 *
 * The application shell (ApplicationShell.tsx) already renders the frame,
 * sidebar, topbar, and <main className="main">. This component renders ONLY the
 * page content that lives inside <main>: the recovery-stage grid (run-content +
 * recovery-drawer). The object header + local "Runs" tab belong to the
 * datastream-object shell, not this page (same split as ProjectMapping, which
 * omits the topbar/tabs the mockup shows).
 *
 * Styling: application.css (global, via the shell) for shell/layout classes
 * (page-header, signal-label, signal-mark, selector, quiet/primary/secondary-
 * button, event, number, field-control) + datastream-recovery.css for the
 * recovery-surface classes (recovery-stage, run-content, run-table, recovery-
 * drawer, drawer-meta, recovery-card, drawer-actions). Colours come exclusively
 * from the application.css CSS variables; run ids use the JetBrains Mono `event`
 * class, counts/durations use the Geist tabular `number` class.
 *
 * ── Data ──────────────────────────────────────────────────────────────────────
 * The recovery MUTATIONS map to the governed ops API through the SAME seam the
 * MUI RecoveryActionsDialog uses (opsApi + the 12.11/12.12 routes):
 *   - "Prepare retry"        -> POST /api/datastreams/{id}/bounded/prepare
 *                               (kind:"reload" — reload the failed partition).
 *   - the rollback-preview / bounded-confirm / append / replace routes exist on
 *     the same seam and are surfaced by RecoveryActionsDialog; this faithful
 *     port keeps the mockup's single "Prepare retry" primary action and defers
 *     the full multi-action matrix to that dialog.
 *
 * The run-history GRID itself has NO read model that returns a per-run
 * state+publication row list (see GAP LIST): the read-model exposes
 * publication_log + recent_imports, not a unified run table with the mockup's
 * state/partition/last-known-good semantics. So the four rows and the drawer
 * scope are the mockup's literals, flagged // TODO(api). The surface renders
 * finished with no backend.
 */
import { useEffect, useState } from "react";
import { OpsApiError, opsPost, type OpsApiCtx } from "../../datastreams/ops/opsApi";
import type {
  BoundedPreparation,
  ImportLedgerRow,
  PublicationLogEntry,
} from "../../datastreams/ops/opsTypes";
import "../application.css";
import "./datastream-recovery.css";

// ---------------------------------------------------------------------------
// View model — one execution-history row.
// TODO(api): no read model returns this per-run state+publication shape today.
// ---------------------------------------------------------------------------

type RunState = "error" | "success";

interface RunRow {
  /** signal-label state class (error = Failed, success = Complete). */
  state: RunState;
  stateLabel: string;
  /** Sub-note under the state (only the failed row has one in the mockup). */
  stateNote?: string;
  started: string; // "22 Jul · 03:00"
  duration: string; // "12m 14s"
  rows: string; // "0" / "18,420"
  rowsNote: string; // "No new rows" / "Published"
  publication: string; // "Last-known-good kept" / "21 Jul published"
  publicationNote: string; // "21 Jul remains available" / "All checks passed"
  selected?: boolean;
}

// Mockup literal rows — the validated source of truth. TODO(api): a per-run
// state / rows / publication / last-known-good read model does not exist.
const MOCK_RUNS: RunRow[] = [
  {
    state: "error",
    stateLabel: "Failed",
    stateNote: "Provider timeout",
    started: "22 Jul · 03:00",
    duration: "12m 14s",
    rows: "0",
    rowsNote: "No new rows",
    publication: "Last-known-good kept",
    publicationNote: "21 Jul remains available",
    selected: true,
  },
  {
    state: "success",
    stateLabel: "Complete",
    started: "21 Jul · 03:00",
    duration: "4m 08s",
    rows: "18,420",
    rowsNote: "Published",
    publication: "21 Jul published",
    publicationNote: "All checks passed",
  },
  {
    state: "success",
    stateLabel: "Complete",
    started: "20 Jul · 03:00",
    duration: "4m 21s",
    rows: "17,992",
    rowsNote: "Published",
    publication: "20 Jul published",
    publicationNote: "All checks passed",
  },
  {
    state: "success",
    stateLabel: "Complete",
    started: "19 Jul · 03:00",
    duration: "4m 02s",
    rows: "18,106",
    rowsNote: "Published",
    publication: "19 Jul published",
    publicationNote: "All checks passed",
  },
];

// Epic 42: build the run history from the real read-model (recent_imports joined
// with publication_log) — was mockup literals. Falls back to MOCK_RUNS when the
// read-model is unavailable so the surface still renders finished.
function fmtStarted(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const day = d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
  const time = d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  return `${day} · ${time}`;
}
function fmtDay(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}
function runsFromReadModel(
  imports: ImportLedgerRow[],
  pub: PublicationLogEntry[],
): RunRow[] {
  const pubByExec = new Map<string, PublicationLogEntry>();
  for (const p of pub) if (p.execution_id) pubByExec.set(p.execution_id, p);
  return imports.map((imp, i) => {
    const st = (imp.status || "").toLowerCase();
    const failed = ["failed", "invalid", "error", "rejected", "empty"].includes(st);
    const rows = imp.row_count ?? 0;
    const published = imp.execution_id ? pubByExec.get(imp.execution_id) : undefined;
    const pubDay = fmtDay(published?.published_at);
    return {
      state: failed ? "error" : "success",
      stateLabel: failed ? "Failed" : "Complete",
      stateNote: failed ? imp.status || undefined : undefined,
      started: fmtStarted(imp.loaded_at || imp.date),
      duration: "—", // TODO(api): no per-run duration on the ledger row
      rows: Number(rows).toLocaleString("en-US"),
      rowsNote: failed ? "No new rows" : "Published",
      publication: published ? `${pubDay} published` : "Last-known-good kept",
      publicationNote: published
        ? published.reason || "All checks passed"
        : "prior version remains available",
      selected: i === 0 && failed,
    };
  });
}

// Recovery-drawer literals for the selected failed run. TODO(api): the failed
// scope (partition, stage, authorization health, run id) is not returned by a
// recovery read model — the drawer is the mockup's validated content.
const FAILED_SCOPE = {
  datastream: "Campaign performance",
  partition: "22 Jul 2026",
  stage: "Extraction",
  authorization: "Healthy",
  runId: "run_01J3…8H",
} as const;

interface DatastreamRecoveryProps {
  projectId?: string;
  datastreamId?: string;
}

export default function DatastreamRecovery({
  projectId = "default",
  datastreamId = "campaign-performance",
}: DatastreamRecoveryProps) {
  // "Prepare retry" goes through the governed ops seam (12.11 bounded prepare).
  const [prepareState, setPrepareState] = useState<
    | { status: "idle" }
    | { status: "preparing" }
    | { status: "prepared"; preparation: BoundedPreparation }
    | { status: "error"; message: string }
  >({ status: "idle" });

  // Run history from the real read-model; MOCK_RUNS is the finished fallback.
  const [runs, setRuns] = useState<RunRow[]>(MOCK_RUNS);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const headers: Record<string, string> = {};
        const token = localStorage.getItem("api_token");
        if (token) headers.Authorization = `Bearer ${token}`;
        const resp = await fetch(`/api/datastreams/${datastreamId}/read-model`, { headers });
        if (!resp.ok) return;
        const rm = (await resp.json()) as {
          recent_imports?: ImportLedgerRow[];
          publication_log?: PublicationLogEntry[];
        };
        const rows = runsFromReadModel(rm.recent_imports ?? [], rm.publication_log ?? []);
        if (!cancelled && rows.length) setRuns(rows);
      } catch {
        /* keep the finished fallback */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [datastreamId]);

  async function handlePrepareRetry() {
    // Governed API seam (same as RecoveryActionsDialog): reload the failed
    // partition — reuse the published configuration, request only the failed day.
    const ctx: OpsApiCtx = {
      apiBase: "",
      apiToken: localStorage.getItem("api_token") || "",
      projectId,
    };
    setPrepareState({ status: "preparing" });
    try {
      const preparation = await opsPost<BoundedPreparation>(
        ctx,
        `/api/datastreams/${datastreamId}/bounded/prepare`,
        { kind: "reload" }
      );
      setPrepareState({ status: "prepared", preparation });
    } catch (e) {
      const message =
        e instanceof OpsApiError ? e.message : e instanceof Error ? e.message : String(e);
      setPrepareState({ status: "error", message });
    }
  }

  return (
    <div className="recovery-stage">
      <section className="run-content">
        <div className="page-header">
          <div>
            <h1>Runs</h1>
            <p>
              Inspect execution history and recover only the failed scope while published
              data remains available.
            </p>
          </div>
          <div className="header-actions">
            {/* TODO(api): date-range selector wiring */}
            <button className="selector">Last 30 days</button>
          </div>
        </div>

        <div className="mapping-panel">
          <div className="mapping-toolbar">
            <div>
              <h2>Run history</h2>
            </div>
            <div className="mapping-filters">
              {/* TODO(api): status filter wiring */}
              <button className="selector">Status: All</button>
              {/* TODO(api): run search wiring */}
              <div className="field-control mapping-search">Search runs</div>
            </div>
          </div>
          <table className="run-table">
            <thead>
              <tr>
                <th style={{ width: "26%" }}>Status</th>
                <th style={{ width: "22%" }}>Started</th>
                <th style={{ width: "18%" }}>Rows</th>
                <th>Publication</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run, i) => (
                <tr key={i} className={run.selected ? "selected" : undefined}>
                  <td>
                    {run.stateNote ? (
                      <div className="run-name">
                        <div>
                          <span className={`signal-label ${run.state}`}>
                            <span className="signal-mark" />
                            {run.stateLabel}
                          </span>
                          <small>{run.stateNote}</small>
                        </div>
                      </div>
                    ) : (
                      <span className={`signal-label ${run.state}`}>
                        <span className="signal-mark" />
                        {run.stateLabel}
                      </span>
                    )}
                  </td>
                  <td>
                    <strong>{run.started}</strong>
                    <small className="number">{run.duration}</small>
                  </td>
                  <td>
                    <strong className="number">{run.rows}</strong>
                    <small>{run.rowsNote}</small>
                  </td>
                  <td>
                    <strong>{run.publication}</strong>
                    <small>{run.publicationNote}</small>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <aside className="recovery-drawer" aria-label="Recovery details">
        <div className="drawer-header">
          <h2>Recover failed run</h2>
          <button className="quiet-button" aria-label="Close">
            ×
          </button>
        </div>
        <div className="drawer-body">
          <section className="drawer-section">
            <span className="signal-label error">
              <span className="signal-mark" />
              Failed
            </span>
            {/* TODO(api): last-known-good preservation message */}
            <p>
              No data was replaced. The 21 Jul publication remains available to downstream
              consumers.
            </p>
          </section>

          <section className="drawer-section">
            <h3>Failed scope</h3>
            {/* TODO(api): failed scope (partition/stage/auth/run id) not in a read model */}
            <dl className="drawer-meta">
              <dt>Datastream</dt>
              <dd>{FAILED_SCOPE.datastream}</dd>
              <dt>Partition</dt>
              <dd>{FAILED_SCOPE.partition}</dd>
              <dt>Stage</dt>
              <dd>{FAILED_SCOPE.stage}</dd>
              <dt>Authorization</dt>
              <dd>{FAILED_SCOPE.authorization}</dd>
              <dt>Run ID</dt>
              <dd className="event">{FAILED_SCOPE.runId}</dd>
            </dl>
          </section>

          <section className="drawer-section">
            <h3>Recommended action</h3>
            <div className="recovery-card">
              <strong>Retry this partition</strong>
              <p>Reuse the published configuration and request only the failed day from Meta Ads.</p>
            </div>
          </section>

          <section className="drawer-section">
            <h3>Evidence</h3>
            {/* TODO(api): evidence (timeout + authorization/mapping checks) not in a read model */}
            <p>
              The provider did not respond before the extraction timeout. Authorization health
              and mapping checks both passed.
            </p>
          </section>

          {/* Governed-seam feedback: the mockup shows only the two buttons; the
              prepare result is surfaced inline without altering the layout. */}
          {prepareState.status === "prepared" && (
            <section className="drawer-section" aria-live="polite">
              <p>
                Retry prepared — reload of {FAILED_SCOPE.partition} is ready to confirm
                (proposal <span className="event">{prepareState.preparation.preparation_id}</span>).
              </p>
            </section>
          )}
          {prepareState.status === "error" && (
            <section className="drawer-section" role="alert">
              <p>Could not prepare the retry: {prepareState.message}</p>
            </section>
          )}

          <div className="drawer-actions">
            {/* Real map: prepare-retry -> governed 12.11 bounded prepare (reload). */}
            <button
              className="primary-button"
              onClick={handlePrepareRetry}
              disabled={prepareState.status === "preparing"}
            >
              {prepareState.status === "preparing" ? "Preparing…" : "Prepare retry"}
            </button>
            {/* TODO(api): technical-events / trace view */}
            <button className="secondary-button">View technical events</button>
          </div>
        </div>
      </aside>
    </div>
  );
}
