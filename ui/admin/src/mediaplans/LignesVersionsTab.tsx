/**
 * LignesVersionsTab — the "Lines & versions" section of a media plan.
 *
 * - Lines of the active version (label, support, dates, budget, "Plan only" badge).
 * - Version history (number, status, date, note) with a "Publish" action on a
 *   candidate version.
 * - Readable diff between two versions (added / removed / changed via the API).
 * - A non-active version is read-only (the invariant is surfaced).
 *
 * Restyled onto the v3 design system (imports.css). AD-9: NULL ≠ 0, plan-only
 * shown honestly. WCAG 2.2 AA: status is never conveyed by color alone.
 */
import { useCallback, useEffect, useState } from "react";
import type { DiffResult, MediaPlanDetail, MediaPlanVersion } from "./types";
import "./imports.css";
import { apiFetch } from "../lib/apiFetch";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

function fmtBudget(amount: number | null | undefined, currency: string): string {
  if (amount == null) return "—";
  return new Intl.NumberFormat("en-GB", {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
  }).format(amount);
}

const STATUS_LABELS: Record<string, string> = {
  candidate: "Candidate",
  published: "Published",
  superseded: "Superseded",
};

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface LignesVersionsTabProps {
  plan: MediaPlanDetail;
  onRefresh: () => void;
  apiBase?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function LignesVersionsTab({
  plan,
  onRefresh,
  apiBase = "",
}: LignesVersionsTabProps) {
  const [versions, setVersions] = useState<MediaPlanVersion[]>([]);
  const [versionsLoading, setVersionsLoading] = useState(true);
  const [versionsError, setVersionsError] = useState<string | null>(null);

  const [publishError, setPublishError] = useState<string | null>(null);
  const [publishingId, setPublishingId] = useState<string | null>(null);
  const [publishSuccess, setPublishSuccess] = useState<string | null>(null);

  // Diff
  const [diffFrom, setDiffFrom] = useState<string>("");
  const [diffTo, setDiffTo] = useState<string>("");
  const [diff, setDiff] = useState<DiffResult | null>(null);
  const [diffLoading, setDiffLoading] = useState(false);
  const [diffError, setDiffError] = useState<string | null>(null);

  // ---------------------------------------------------------------------------
  // Versions
  // ---------------------------------------------------------------------------

  const loadVersions = useCallback(async () => {
    setVersionsLoading(true);
    setVersionsError(null);
    try {
      const resp = await apiFetch(
        `${apiBase}/api/mediaplans/${encodeURIComponent(plan.id)}/versions`
      );
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        throw new Error(data?.message ?? `HTTP ${resp.status}`);
      }
      const data = (await resp.json()) as { versions: MediaPlanVersion[] };
      setVersions(data.versions ?? []);
    } catch (err) {
      setVersionsError(err instanceof Error ? err.message : String(err));
    } finally {
      setVersionsLoading(false);
    }
  }, [plan.id, apiBase]);

  useEffect(() => {
    void loadVersions();
  }, [loadVersions]);

  // ---------------------------------------------------------------------------
  // Publish
  // ---------------------------------------------------------------------------

  async function handlePublish(versionId: string) {
    setPublishingId(versionId);
    setPublishError(null);
    setPublishSuccess(null);
    try {
      const resp = await apiFetch(
        `${apiBase}/api/mediaplans/versions/${encodeURIComponent(versionId)}/publish`,
        { method: "POST", headers: { "Content-Type": "application/json" } }
      );
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        setPublishError(data?.message ?? `HTTP ${resp.status} while publishing.`);
        return;
      }
      setPublishSuccess("Version published successfully.");
      await loadVersions();
      onRefresh();
    } catch (err) {
      setPublishError(err instanceof Error ? err.message : "Unexpected error.");
    } finally {
      setPublishingId(null);
    }
  }

  // ---------------------------------------------------------------------------
  // Diff
  // ---------------------------------------------------------------------------

  async function handleDiff() {
    if (!diffFrom || !diffTo) return;
    setDiffLoading(true);
    setDiffError(null);
    setDiff(null);
    try {
      const url = `${apiBase}/api/mediaplans/${encodeURIComponent(plan.id)}/diff?from=${encodeURIComponent(diffFrom)}&to=${encodeURIComponent(diffTo)}`;
      const resp = await apiFetch(url);
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        throw new Error(data?.message ?? `HTTP ${resp.status}`);
      }
      setDiff((await resp.json()) as DiffResult);
    } catch (err) {
      setDiffError(err instanceof Error ? err.message : String(err));
    } finally {
      setDiffLoading(false);
    }
  }

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  const activeLines = plan.lines ?? [];
  const currency = plan.currency;

  return (
    <div>
      {/* ------------------------------------------------------------------ */}
      {/* Lines of the active version                                         */}
      {/* ------------------------------------------------------------------ */}
      <section className="imports-section">
        <h3 className="imports-section-title">Active version lines</h3>

        {plan.active_version == null ? (
          <p className="imports-section-note" data-testid="no-active-version">
            No active version — publish a candidate version to see its lines.
          </p>
        ) : (
          <>
            <span className="imports-subnote">
              Active version: v{plan.active_version.version_number} —{" "}
              {fmtDate(plan.active_version.published_at)}
            </span>
            {activeLines.length === 0 ? (
              <p className="imports-section-note" data-testid="lines-empty">
                No lines in the active version.
              </p>
            ) : (
              <div className="panel imports-panel">
                <div className="table-scroll" tabIndex={0} aria-label="Active version lines">
                  <table className="imports-table" data-testid="lines-table">
                    <thead>
                      <tr>
                        <th>Label</th>
                        <th>Support</th>
                        <th>Start</th>
                        <th>End</th>
                        <th className="num">Budget</th>
                        <th>Type</th>
                      </tr>
                    </thead>
                    <tbody>
                      {activeLines.map((line) => (
                        <tr key={line.id} data-testid={`line-row-${line.line_key}`}>
                          <td>{line.label}</td>
                          <td className="imports-cell-muted">{line.support ?? "—"}</td>
                          <td>{fmtDate(line.date_debut)}</td>
                          <td>{fmtDate(line.date_fin)}</td>
                          <td className="num">{fmtBudget(line.budget, currency)}</td>
                          <td>
                            {line.is_plan_only && (
                              <span
                                className="imports-chip"
                                aria-label="Type: plan only — no real data"
                                data-testid={`badge-plan-only-${line.line_key}`}
                              >
                                Plan only
                              </span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </>
        )}
      </section>

      {/* ------------------------------------------------------------------ */}
      {/* Version history                                                     */}
      {/* ------------------------------------------------------------------ */}
      <section className="imports-section">
        <h3 className="imports-section-title">Version history</h3>
        <p className="imports-section-note">
          Immutable versions cannot be edited. Only the active (published) version applies to
          pacing and mappings.
        </p>

        {publishError && (
          <div className="imports-alert error" role="alert" data-testid="publish-error">
            {publishError}
          </div>
        )}
        {publishSuccess && (
          <div className="imports-alert success" role="status" data-testid="publish-success">
            {publishSuccess}
          </div>
        )}

        {versionsLoading && (
          <div className="imports-inline-state">
            <span className="imports-spinner" aria-hidden="true" />
            Loading versions…
          </div>
        )}
        {versionsError && (
          <div className="imports-alert error" role="alert" data-testid="versions-error">
            {versionsError}
          </div>
        )}

        {!versionsLoading && versions.length > 0 && (
          <div className="panel imports-panel">
            <div className="table-scroll" tabIndex={0} aria-label="Version history">
              <table className="imports-table" data-testid="versions-table">
                <thead>
                  <tr>
                    <th>Number</th>
                    <th>Status</th>
                    <th>Active</th>
                    <th>Date</th>
                    <th>Source note</th>
                    <th>
                      <span className="sr-only">Action</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {versions.map((v) => (
                    <tr key={v.id} data-testid={`version-row-${v.id}`}>
                      <td>
                        <span className="imports-cell-strong">v{v.version_number}</span>
                      </td>
                      <td>
                        <span
                          className="imports-chip"
                          aria-label={`Status: ${STATUS_LABELS[v.status] ?? v.status}`}
                        >
                          {STATUS_LABELS[v.status] ?? v.status}
                        </span>
                      </td>
                      <td>
                        {v.is_active ? (
                          <span className="imports-chip success" aria-label="Active version">
                            Active
                          </span>
                        ) : (
                          <span className="imports-cell-muted" aria-label="Not active">
                            —
                          </span>
                        )}
                      </td>
                      <td className="imports-cell-muted">
                        {fmtDate(v.published_at ?? v.created_at)}
                      </td>
                      <td className="imports-cell-muted">{v.source_note ?? "—"}</td>
                      <td style={{ textAlign: "right" }}>
                        {v.status === "candidate" ? (
                          <button
                            className="secondary-button"
                            type="button"
                            onClick={() => handlePublish(v.id)}
                            disabled={publishingId === v.id}
                            data-testid={`publish-version-btn-${v.id}`}
                          >
                            {publishingId === v.id ? "Publishing…" : "Publish"}
                          </button>
                        ) : (
                          <span className="imports-cell-muted">
                            {v.is_active ? "Current" : "Archived"}
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </section>

      {/* ------------------------------------------------------------------ */}
      {/* Compare two versions                                                */}
      {/* ------------------------------------------------------------------ */}
      <section className="imports-section">
        <h3 className="imports-section-title">Compare two versions</h3>

        <div className="imports-compare">
          <div className="field">
            <span className="imports-label">Reference version (from)</span>
            <select
              className="imports-select"
              value={diffFrom}
              onChange={(e) => setDiffFrom(e.target.value)}
              aria-label="Reference version for the diff"
              data-testid="diff-from"
            >
              <option value="">— Select</option>
              {versions.map((v) => (
                <option key={v.id} value={v.id}>
                  v{v.version_number} ({STATUS_LABELS[v.status] ?? v.status})
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <span className="imports-label">Target version (to)</span>
            <select
              className="imports-select"
              value={diffTo}
              onChange={(e) => setDiffTo(e.target.value)}
              aria-label="Target version for the diff"
              data-testid="diff-to"
            >
              <option value="">— Select</option>
              {versions.map((v) => (
                <option key={v.id} value={v.id}>
                  v{v.version_number} ({STATUS_LABELS[v.status] ?? v.status})
                </option>
              ))}
            </select>
          </div>
          <button
            className="secondary-button"
            type="button"
            onClick={handleDiff}
            disabled={!diffFrom || !diffTo || diffLoading || diffFrom === diffTo}
            data-testid="diff-submit"
          >
            {diffLoading ? "Comparing…" : "Compare"}
          </button>
        </div>

        {diffError && (
          <div className="imports-alert error" role="alert" data-testid="diff-error">
            {diffError}
          </div>
        )}

        {diff && (
          <div data-testid="diff-result">
            {diff.added.length > 0 && (
              <div className="imports-diff-group">
                <span
                  className="imports-diff-heading added"
                  aria-label={`${diff.added.length} line(s) added`}
                >
                  {diff.added.length} line(s) added
                </span>
                {diff.added.map((l) => (
                  <div className="imports-diff-row added" key={l.line_key} aria-label={`Added: ${l.label}`}>
                    <span>+ {l.label}</span>
                    <span className="amount">{fmtBudget(l.budget, currency)}</span>
                  </div>
                ))}
              </div>
            )}

            {diff.removed.length > 0 && (
              <div className="imports-diff-group">
                <span
                  className="imports-diff-heading removed"
                  aria-label={`${diff.removed.length} line(s) removed`}
                >
                  {diff.removed.length} line(s) removed
                </span>
                {diff.removed.map((l) => (
                  <div className="imports-diff-row removed" key={l.line_key} aria-label={`Removed: ${l.label}`}>
                    <span>− {l.label}</span>
                    <span className="amount">{fmtBudget(l.budget, currency)}</span>
                  </div>
                ))}
              </div>
            )}

            {diff.changed.length > 0 && (
              <div className="imports-diff-group">
                <span
                  className="imports-diff-heading changed"
                  aria-label={`${diff.changed.length} change(s)`}
                >
                  {diff.changed.length} change(s)
                </span>
                {diff.changed.map((d, i) => (
                  <div
                    className="imports-diff-row changed"
                    // eslint-disable-next-line react/no-array-index-key
                    key={`${d.line_key}-${d.field}-${i}`}
                    aria-label={`Changed: ${d.line_key} — ${d.field}`}
                  >
                    <span>
                      ≠ {d.line_key} · {d.field}: <del>{String(d.from ?? "—")}</del> →{" "}
                      {String(d.to ?? "—")}
                    </span>
                  </div>
                ))}
              </div>
            )}

            {diff.added.length === 0 && diff.removed.length === 0 && diff.changed.length === 0 && (
              <p className="imports-section-note" data-testid="diff-identical">
                The two versions are identical.
              </p>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
