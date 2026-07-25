/**
 * MediaplansPage — the media-plan list for the current project (Data > Imports).
 *
 * Shows every media plan for the active project (several plans can be active at
 * once), lets you create a new plan (name + currency), and drills into a plan's
 * detail on click.
 *
 * Restyled onto the validated v3 design system: content lives inside the shell
 * <main>, using application.css shell classes (page-header, panel, buttons,
 * signal marks) + imports.css for the page-specific summary cards, table, and the
 * native create-plan overlay.
 *
 * AD-15: everything goes through the server API — no direct DB access.
 * WCAG 2.2 AA: status is never conveyed by color alone.
 */
import { useCallback, useEffect, useState } from "react";
import type { MediaPlan } from "./types";
import "../shell/application.css";
import "./imports.css";
import { apiFetch } from "../lib/apiFetch";

// ---------------------------------------------------------------------------
// Common currencies (non-exhaustive, extensible)
// ---------------------------------------------------------------------------

const CURRENCIES = ["EUR", "USD", "GBP", "CHF", "CAD"];

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface MediaplansPageProps {
  projectId: string;
  /** Navigate to a plan's detail. */
  onSelectPlan: (planId: string) => void;
  apiBase?: string;
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

function fmtVersionLabel(plan: MediaPlan): string {
  if (!plan.active_version) return "No active version";
  const v = plan.active_version;
  const date = v.published_at ? fmtDate(v.published_at) : fmtDate(v.created_at);
  return `v${v.version_number} — ${date}`;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function MediaplansPage({
  projectId,
  onSelectPlan,
  apiBase = "",
}: MediaplansPageProps) {
  const [plans, setPlans] = useState<MediaPlan[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Create form
  const [createOpen, setCreateOpen] = useState(false);
  const [createName, setCreateName] = useState("");
  const [createCurrency, setCreateCurrency] = useState("EUR");
  const [createError, setCreateError] = useState<string | null>(null);
  const [createSaving, setCreateSaving] = useState(false);

  // ---------------------------------------------------------------------------
  // Loading
  // ---------------------------------------------------------------------------

  const loadPlans = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await apiFetch(
        `${apiBase}/api/projects/${encodeURIComponent(projectId)}/mediaplans`
      );
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        throw new Error(
          data?.message ?? `HTTP ${resp.status} — unable to load media plans.`
        );
      }
      const data = (await resp.json()) as { plans: MediaPlan[] };
      setPlans(data.plans ?? []);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [projectId, apiBase]);

  useEffect(() => {
    void loadPlans();
  }, [loadPlans]);

  // ---------------------------------------------------------------------------
  // Create
  // ---------------------------------------------------------------------------

  async function handleCreate() {
    if (!createName.trim()) return;
    setCreateSaving(true);
    setCreateError(null);
    try {
      const resp = await apiFetch(
        `${apiBase}/api/projects/${encodeURIComponent(projectId)}/mediaplans`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: createName.trim(), currency: createCurrency }),
        }
      );
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        setCreateError(data?.message ?? `HTTP ${resp.status} while creating the plan.`);
        return;
      }
      setCreateName("");
      setCreateCurrency("EUR");
      setCreateOpen(false);
      await loadPlans();
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : "Unexpected error.");
    } finally {
      setCreateSaving(false);
    }
  }

  function handleCloseCreate() {
    setCreateOpen(false);
    setCreateName("");
    setCreateCurrency("EUR");
    setCreateError(null);
  }

  // ---------------------------------------------------------------------------
  // Derived summary
  // ---------------------------------------------------------------------------

  const activeCount = plans.filter((p) => !p.archived_at).length;
  const archivedCount = plans.length - activeCount;

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  return (
    <div className="imports-surface">
      <div className="page-header">
        <div>
          <h1>Media plans</h1>
          <p>Investment intentions imported from Excel, versioned and mapped to real spend.</p>
        </div>
        <div className="header-actions">
          <button
            className="primary-button"
            type="button"
            onClick={() => setCreateOpen(true)}
            data-testid="create-plan-btn"
          >
            + New plan
          </button>
        </div>
      </div>

      {error && (
        <div className="imports-alert error" role="alert" data-testid="plans-error">
          {error}
        </div>
      )}

      <section className="imports-summary">
        <article className="panel imports-card">
          <span>Media plans</span>
          <strong>{plans.length}</strong>
          <p>In this project</p>
        </article>
        <article className="panel imports-card">
          <span>Active plans</span>
          <strong>{activeCount}</strong>
          <p>Not archived</p>
        </article>
        <article className="panel imports-card">
          <span>Archived plans</span>
          <strong>{archivedCount}</strong>
          <p>Kept for reference</p>
        </article>
      </section>

      {loading && (
        <div className="imports-inline-state">
          <span className="imports-spinner" aria-hidden="true" />
          Loading media plans…
        </div>
      )}

      {!loading && !error && plans.length === 0 && (
        <div className="panel imports-empty" data-testid="plans-empty">
          <strong>No media plan yet</strong>
          <p>Create a plan to start importing your investment intentions.</p>
        </div>
      )}

      {!loading && plans.length > 0 && (
        <section className="panel imports-panel">
          <div className="table-scroll" tabIndex={0} aria-label="Media plans">
            <table className="imports-table" data-testid="plans-table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Currency</th>
                  <th>Active version</th>
                  <th>Status</th>
                  <th>Created</th>
                  <th>
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {plans.map((plan) => (
                  <tr
                    key={plan.id}
                    className="clickable"
                    data-testid={`plan-row-${plan.id}`}
                    onClick={() => onSelectPlan(plan.id)}
                  >
                    <td>
                      <span className="imports-cell-strong">{plan.name}</span>
                    </td>
                    <td>{plan.currency}</td>
                    <td className="imports-cell-muted">{fmtVersionLabel(plan)}</td>
                    <td>
                      {plan.archived_at ? (
                        <span
                          className="imports-chip"
                          title={`Archived ${fmtDate(plan.archived_at)}`}
                          aria-label="Status: archived"
                        >
                          Archived
                        </span>
                      ) : (
                        <span className="imports-chip success" aria-label="Status: active">
                          Active
                        </span>
                      )}
                    </td>
                    <td className="imports-cell-muted">{fmtDate(plan.created_at)}</td>
                    <td style={{ textAlign: "right" }}>
                      <button
                        className="quiet-button"
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          onSelectPlan(plan.id);
                        }}
                        data-testid={`plan-detail-btn-${plan.id}`}
                      >
                        Open →
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {createOpen && (
        <div
          className="imports-overlay"
          role="dialog"
          aria-modal="true"
          aria-labelledby="create-plan-title"
          data-testid="create-plan-dialog"
          onClick={handleCloseCreate}
        >
          <div className="imports-dialog" onClick={(e) => e.stopPropagation()}>
            <div className="imports-dialog-head">
              <h2 id="create-plan-title">New media plan</h2>
            </div>
            <div className="imports-dialog-body">
              <label className="imports-field">
                <span className="imports-label">Plan name</span>
                <input
                  className="imports-input"
                  value={createName}
                  onChange={(e) => setCreateName(e.target.value)}
                  data-testid="create-plan-name"
                  aria-label="Plan name"
                />
              </label>
              <label className="imports-field">
                <span className="imports-label">Currency</span>
                <select
                  className="imports-select"
                  value={createCurrency}
                  onChange={(e) => setCreateCurrency(e.target.value)}
                  data-testid="create-plan-currency"
                  aria-label="Plan currency"
                >
                  {CURRENCIES.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </select>
              </label>
              {createError && (
                <div className="imports-alert error" role="alert" data-testid="create-plan-error">
                  {createError}
                </div>
              )}
            </div>
            <div className="imports-dialog-actions">
              <button className="secondary-button" type="button" onClick={handleCloseCreate}>
                Cancel
              </button>
              <button
                className="primary-button"
                type="button"
                onClick={handleCreate}
                disabled={createSaving || !createName.trim()}
                data-testid="create-plan-submit"
              >
                {createSaving ? "Creating…" : "Create"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
