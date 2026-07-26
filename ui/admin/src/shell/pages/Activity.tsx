/**
 * Activity — Governance › Activity surface.
 *
 * PANEL 1 — Changes: the shared audit registry, read from GET /api/audit
 * (AD-9). Rows carry `created_at`, `identity`, `action`, `provider_account` and
 * `connection_ref`; the table shows exactly those and nothing else.
 *
 * PANEL 2 — Alert rules: there is NO rules backend. The panel therefore states
 * that plainly instead of listing rules, because the previous version's three
 * "Active" rules told the user they would be warned when an extraction fell
 * behind. They would not have been.
 *
 * History (audit 2026-07-25): this page made no network call. It rendered eight
 * fabricated audit entries attributed to NAMED people ("Jean Albany",
 * "Marie Chen", "Northwind Studio"), timestamped, with entities and outcomes,
 * under the footer "Shared audit registry · AD-9 provenance" — a counterfeit
 * audit trail. Four buttons did nothing. All removed; the real /api/audit, which
 * existed the whole time, is now wired, and the export and filter controls act
 * on it.
 *
 * Styling: application.css (global, via the shell) + activity.css. Colors come
 * exclusively from the application.css CSS variables (dark-safe).
 */
import { useCallback, useEffect, useMemo, useState, type ReactElement } from "react";
import { apiFetch, apiGet } from "../../lib/apiFetch";
import "../application.css";
import "./activity.css";

// ---------------------------------------------------------------------------
// GET /api/audit — {rows: AuditRow[], count: number}
// ---------------------------------------------------------------------------

interface AuditRow {
  id?: number | string;
  identity?: string | null;
  action?: string | null;
  provider_account?: string | null;
  connection_ref?: string | null;
  metadata?: unknown;
  created_at?: string | null;
}

interface AuditResponse {
  rows?: AuditRow[];
  count?: number;
}

type LoadState = "loading" | "error" | "ready";

/** The endpoint clamps to 500; 200 is plenty for a governance read-out. */
const LIMIT = 200;

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface ActivityProps {
  projectId?: string;
}

export default function Activity({ projectId }: ActivityProps): ReactElement {
  // /api/audit is deliberately org-wide: it carries no project scope (see
  // core/audit.py query_audit_log). projectId is accepted for interface parity
  // with the other Governance surfaces but must not be sent as a filter that the
  // server would ignore — pretending to scope is its own small lie.
  void projectId;

  const [state, setState] = useState<LoadState>("loading");
  const [error, setError] = useState("");
  const [rows, setRows] = useState<AuditRow[]>([]);
  /** Every action seen at least once, for the filter — grown, never reset by a filter. */
  const [actions, setActions] = useState<string[]>([]);
  const [action, setAction] = useState("all");
  const [attempt, setAttempt] = useState(0);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState("");

  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  const query = useMemo(() => {
    const parts = [`limit=${LIMIT}`];
    if (action !== "all") parts.push(`action=${encodeURIComponent(action)}`);
    return parts.join("&");
  }, [action]);

  useEffect(() => {
    let alive = true;
    setState("loading");
    setError("");

    (async () => {
      try {
        const body = await apiGet<AuditResponse>(`/api/audit?${query}`);
        if (!alive) return;
        const list = Array.isArray(body.rows) ? body.rows : [];
        setRows(list);
        setActions((prev) => {
          const seen = new Set(prev);
          for (const r of list) if (r.action) seen.add(r.action);
          return [...seen].sort();
        });
        setState("ready");
      } catch (err) {
        if (!alive) return;
        setRows([]);
        setError(err instanceof Error ? err.message : "Request failed");
        setState("error");
      }
    })();

    return () => {
      alive = false;
    };
  }, [query, attempt]);

  /** Export the SAME query as CSV, straight from /api/audit?format=csv. */
  const exportCsv = useCallback(async () => {
    setExporting(true);
    setExportError("");
    try {
      const resp = await apiFetch(`/api/audit?${query}&format=csv`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "audit.csv";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      setExportError(err instanceof Error ? err.message : "Export failed");
    } finally {
      setExporting(false);
    }
  }, [query]);

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Activity</h1>
          <p>
            The shared audit registry — who did what, against which account, and when.
          </p>
        </div>
        <div className="header-actions">
          <button
            className="secondary-button"
            type="button"
            data-testid="export-log"
            onClick={exportCsv}
            disabled={state !== "ready" || rows.length === 0 || exporting}
          >
            {exporting ? "Exporting…" : "Export log (CSV)"}
          </button>
        </div>
      </div>

      {/* PANEL 1 — Changes -------------------------------------------------- */}
      <section className="panel activity-panel">
        <div className="activity-panel-header">
          <div>
            <h2>Changes</h2>
            <p>
              Every recorded action, newest first. The registry is org-wide, not scoped
              to one project.
            </p>
          </div>
          <label className="activity-filter">
            <span className="sr-only">Filter by action</span>
            <select
              className="selector"
              data-testid="action-filter"
              value={action}
              onChange={(e) => setAction(e.target.value)}
              disabled={actions.length === 0}
            >
              <option value="all">All actions</option>
              {actions.map((a) => (
                <option key={a} value={a}>
                  {a}
                </option>
              ))}
            </select>
          </label>
        </div>

        {exportError ? (
          <p className="activity-state error" data-testid="export-error">
            Export failed. {exportError}
          </p>
        ) : null}

        {state === "loading" ? (
          <p className="activity-state" data-testid="activity-loading">
            Loading the audit registry…
          </p>
        ) : null}

        {state === "error" ? (
          <div className="activity-state error" data-testid="activity-error">
            <p>Couldn&apos;t load the audit registry. {error}</p>
            <button className="secondary-button" type="button" onClick={retry}>
              Retry
            </button>
          </div>
        ) : null}

        {state === "ready" && rows.length === 0 ? (
          <p className="activity-state" data-testid="activity-empty">
            {action === "all"
              ? "No action has been recorded yet."
              : `No action recorded for “${action}”.`}
          </p>
        ) : null}

        {state === "ready" && rows.length > 0 ? (
          <>
            <div className="table-scroll" tabIndex={0} aria-label="Audit registry">
              <table className="activity-table">
                <thead>
                  <tr>
                    <th style={{ width: "18%" }}>When</th>
                    <th style={{ width: "22%" }}>Identity</th>
                    <th style={{ width: "24%" }}>Action</th>
                    <th style={{ width: "18%" }}>Provider account</th>
                    <th style={{ width: "18%" }}>Connection</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row, i) => (
                    <tr key={String(row.id ?? `${row.created_at}-${i}`)}>
                      <td>
                        <div className="when-cell">
                          <strong className="number">{fmtDate(row.created_at)}</strong>
                          <small className="number">{fmtTime(row.created_at)}</small>
                        </div>
                      </td>
                      <td>
                        <span className="actor-cell">{row.identity || "—"}</span>
                      </td>
                      <td>
                        <span className="mono entity-id">{row.action || "—"}</span>
                      </td>
                      <td>
                        <span className="entity-text">{row.provider_account || "—"}</span>
                      </td>
                      <td>
                        <span className="mono entity-id">{row.connection_ref || "—"}</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="activity-footer">
              <span>
                Showing <span className="number">{rows.length}</span> entr
                {rows.length === 1 ? "y" : "ies"}
                {rows.length === LIMIT ? ` (capped at ${LIMIT})` : ""}
              </span>
              <span className="activity-footer-note mono">GET /api/audit</span>
            </div>
          </>
        ) : null}
      </section>

      {/* PANEL 2 — Alert rules ---------------------------------------------- */}
      <section className="panel activity-panel">
        <div className="activity-panel-header">
          <div>
            <h2>Alert rules</h2>
            <p>Conditions that would notify you when a datastream drifts or falls behind.</p>
          </div>
        </div>
        <p className="activity-state" data-testid="rules-unavailable">
          Alert rules are not available yet. No rule exists on this account, nothing is
          being watched, and no notification will be sent — check the audit registry above
          and the datastream states in Data for anything that needs attention.
        </p>
      </section>
    </>
  );
}
