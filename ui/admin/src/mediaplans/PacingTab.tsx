/**
 * PacingTab — the "Pacing" section of a media plan.
 *
 * Plan-vs-actual pacing (mart 22.4):
 *   - Per-line table: budget, consumed %, pace ±%, remaining, extrapolated.
 *   - Rollup by support + global.
 *   - Over-/under-delivery indicators by icon + text (never color alone).
 *   - "Plan only" = badge, no pacing %.
 *   - NULL → "—", never 0. Estimation is labeled.
 *   - Provenance shown (plan version, as_of).
 *
 * Restyled onto the v3 design system (imports.css). AD-9: honest NULL everywhere,
 * estimation ≠ measure. WCAG 2.2 AA: indicators are never conveyed by color alone.
 */
import { useCallback, useEffect, useState } from "react";
import type { PacingChannel, PacingLine, PacingPlan, PacingResponse } from "./types";
import "./imports.css";
import { apiFetch } from "../lib/apiFetch";

// ---------------------------------------------------------------------------
// Default thresholds (decision 6, epic 22 — asymmetric, configurable server-side).
// Shown here for the UI, but the server is the one that evaluates them.
// ---------------------------------------------------------------------------
const OVER_PCT = 10; // > +10% = over-delivery (financial risk)
const UNDER_PCT = 10; // < −10% = under-delivery

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Format a percentage or return "—" when null. */
function fmtPct(value: number | null | undefined): string {
  if (value == null) return "—";
  const pct = value * 100;
  return `${pct >= 0 ? "+" : ""}${pct.toFixed(1)}%`;
}

/** Format an amount or return "—" when null. */
function fmtMoney(amount: number | null | undefined, currency: string): string {
  if (amount == null) return "—";
  return new Intl.NumberFormat("en-GB", {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
  }).format(amount);
}

/** Format a consumed % or return "—" when null. */
function fmtConsumedPct(value: number | null | undefined): string {
  if (value == null) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

// ---------------------------------------------------------------------------
// Over-/under-delivery indicator
// WCAG: icon + text, never color alone.
// ---------------------------------------------------------------------------

function PaceIndicator({ pacePct }: { pacePct: number | null }) {
  if (pacePct == null) return <span className="imports-cell-muted">—</span>;

  const pct = pacePct * 100;

  if (pct > OVER_PCT) {
    return (
      <span className="imports-pace over" aria-label={`Over-delivery: ${fmtPct(pacePct)}`}>
        <span aria-hidden="true">⚠</span>
        {fmtPct(pacePct)} Over
      </span>
    );
  }

  if (pct < -UNDER_PCT) {
    return (
      <span className="imports-pace under" aria-label={`Under-delivery: ${fmtPct(pacePct)}`}>
        <span aria-hidden="true">▽</span>
        {fmtPct(pacePct)} Under
      </span>
    );
  }

  return (
    <span className="imports-pace" aria-label={`On track: ${fmtPct(pacePct)}`}>
      {fmtPct(pacePct)} On track
    </span>
  );
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface PacingTabProps {
  planId: string;
  currency: string;
  apiBase?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function PacingTab({ planId, currency, apiBase = "" }: PacingTabProps) {
  const [pacing, setPacing] = useState<PacingResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await apiFetch(
        `${apiBase}/api/mediaplans/${encodeURIComponent(planId)}/pacing`
      );
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        throw new Error(data?.message ?? `HTTP ${resp.status} — pacing unavailable.`);
      }
      setPacing((await resp.json()) as PacingResponse);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [planId, apiBase]);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading) {
    return (
      <div className="imports-inline-state">
        <span className="imports-spinner" aria-hidden="true" />
        Loading pacing…
      </div>
    );
  }

  if (error) {
    return (
      <div className="imports-alert error" role="alert" data-testid="pacing-error">
        {error}
      </div>
    );
  }

  if (!pacing) return null;

  const lines: PacingLine[] = pacing.lines ?? [];
  const channels: PacingChannel[] = pacing.channels ?? [];
  const plan: PacingPlan | null = pacing.plan ?? null;

  // Provenance (plan version, as_of)
  const versionId = plan?.plan_version_id ?? lines[0]?.plan_version_id ?? null;
  const asOf = plan?.as_of ?? lines[0]?.as_of ?? null;

  return (
    <div>
      {/* Provenance */}
      {(versionId || asOf) && (
        <span className="imports-subnote" data-testid="pacing-provenance">
          {versionId && `Plan version: ${versionId.substring(0, 8)}…`}
          {versionId && asOf && " · "}
          {asOf && `As of ${fmtDate(asOf)}`}
        </span>
      )}

      {/* ------------------------------------------------------------------ */}
      {/* Pacing by line                                                      */}
      {/* ------------------------------------------------------------------ */}
      <section className="imports-section">
        <h3 className="imports-section-title">By line</h3>

        {lines.length === 0 ? (
          <p className="imports-section-note" data-testid="pacing-lines-empty">
            No pacing line available.
          </p>
        ) : (
          <div className="panel imports-panel">
            <div className="table-scroll" tabIndex={0} aria-label="Pacing by line">
              <table className="imports-table" data-testid="pacing-lines-table">
                <thead>
                  <tr>
                    <th>Label</th>
                    <th>Support</th>
                    <th className="num">Budget</th>
                    <th className="num">Consumed</th>
                    <th>Pace</th>
                    <th className="num">Remaining</th>
                    <th className="num">Extrapolated (Estimation)</th>
                    <th>Type</th>
                  </tr>
                </thead>
                <tbody>
                  {lines.map((line) => (
                    <tr key={line.line_key} data-testid={`pacing-line-${line.line_key}`}>
                      <td>
                        <span className="imports-cell-strong">{line.label}</span>
                      </td>
                      <td className="imports-cell-muted">{line.support ?? "—"}</td>
                      <td className="num">{fmtMoney(line.budget, currency)}</td>
                      <td className="num">
                        {line.is_plan_only ? "—" : fmtConsumedPct(line.consumed_pct)}
                      </td>
                      <td>
                        {line.is_plan_only ? (
                          <span className="imports-cell-muted">—</span>
                        ) : (
                          <PaceIndicator pacePct={line.pace_pct} />
                        )}
                      </td>
                      <td className="num">
                        {line.is_plan_only ? "—" : fmtMoney(line.remaining, currency)}
                      </td>
                      <td className="num" data-testid={`pacing-extrapolated-${line.line_key}`}>
                        {line.is_plan_only ? "—" : fmtMoney(line.extrapolated_spend, currency)}
                      </td>
                      <td>
                        {line.is_plan_only ? (
                          <span
                            className="imports-chip"
                            aria-label="Plan-only line — no real data"
                            data-testid={`pacing-badge-plan-only-${line.line_key}`}
                          >
                            Plan only
                          </span>
                        ) : null}
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
      {/* Rollup by support                                                   */}
      {/* ------------------------------------------------------------------ */}
      {channels.length > 0 && (
        <section className="imports-section">
          <h3 className="imports-section-title">By support</h3>
          <div className="panel imports-panel">
            <div className="table-scroll" tabIndex={0} aria-label="Pacing by support">
              <table className="imports-table" data-testid="pacing-channels-table">
                <thead>
                  <tr>
                    <th>Support</th>
                    <th className="num">Budget</th>
                    <th className="num">Consumed</th>
                    <th>Pace</th>
                  </tr>
                </thead>
                <tbody>
                  {channels.map((ch) => (
                    <tr key={ch.support} data-testid={`pacing-channel-${ch.support}`}>
                      <td>
                        <span className="imports-cell-strong">{ch.support}</span>
                      </td>
                      <td className="num">{fmtMoney(ch.budget, currency)}</td>
                      <td className="num">{fmtConsumedPct(ch.consumed_pct)}</td>
                      <td>
                        <PaceIndicator pacePct={ch.pace_pct} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </section>
      )}

      {/* ------------------------------------------------------------------ */}
      {/* Rollup global                                                       */}
      {/* ------------------------------------------------------------------ */}
      {plan && (
        <section className="imports-section">
          <h3 className="imports-section-title">Global (plan)</h3>
          <div className="panel imports-panel">
            <div className="table-scroll" tabIndex={0} aria-label="Global pacing">
              <table className="imports-table" data-testid="pacing-plan-table">
                <thead>
                  <tr>
                    <th className="num">Total budget</th>
                    <th className="num">Consumed</th>
                    <th>Pace</th>
                    <th className="num">Remaining</th>
                  </tr>
                </thead>
                <tbody>
                  <tr data-testid="pacing-plan-row">
                    <td className="num">
                      <span className="imports-cell-strong">{fmtMoney(plan.budget, currency)}</span>
                    </td>
                    <td className="num">{fmtConsumedPct(plan.consumed_pct)}</td>
                    <td>
                      <PaceIndicator pacePct={plan.pace_pct} />
                    </td>
                    <td className="num">{fmtMoney(plan.remaining, currency)}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </section>
      )}
    </div>
  );
}
