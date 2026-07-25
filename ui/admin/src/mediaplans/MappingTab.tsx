/**
 * MappingTab — the "Mapping" section of a media plan.
 *
 * For each active-plan line:
 *   - Active / orphaned mappings with a badge.
 *   - Split editing: client-side validation that the sum = 100% per campaign
 *     BEFORE sending. The server remains the authority (422 shown honestly).
 *
 * The "Unmapped actuals" section surfaces real spend that is not mapped — it is
 * never hidden.
 *
 * Restyled onto the v3 design system (imports.css). AD-5/AD-9: honest invariants,
 * 404/403 rendered honestly. WCAG 2.2 AA: status is never conveyed by color alone.
 */
import { useCallback, useEffect, useState } from "react";
import type {
  LineMapping,
  MediaPlanDetail,
  MediaPlanLine,
  UnmappedActual,
} from "./types";
import "./imports.css";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmtPct(weight: number): string {
  return `${(weight * 100).toFixed(1)}%`;
}

function fmtMoney(amount: number, currency: string): string {
  return new Intl.NumberFormat("en-GB", {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
  }).format(amount);
}

// ---------------------------------------------------------------------------
// Local editing types
// ---------------------------------------------------------------------------

interface MappingEntry {
  connector: string;
  campaign_ref: string;
  split_weight: number;
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface MappingTabProps {
  plan: MediaPlanDetail;
  apiBase?: string;
}

// ---------------------------------------------------------------------------
// Per-line mapping editor
// ---------------------------------------------------------------------------

function LineMappingEditor({
  line,
  mappings,
  apiBase,
  planId,
}: {
  line: MediaPlanLine;
  mappings: LineMapping[];
  currency?: string;
  apiBase: string;
  planId: string;
}) {
  const [editing, setEditing] = useState(false);
  const [entries, setEntries] = useState<MappingEntry[]>([]);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveOk, setSaveOk] = useState(false);

  // Client-side validation: sum = 100% per campaign (decision 5, epic 22)
  function totalWeight(): number {
    return entries.reduce((sum, e) => sum + (e.split_weight || 0), 0);
  }

  const sumOk = Math.abs(totalWeight() - 1.0) < 0.0001;

  function startEditing() {
    setEditing(true);
    setSaveError(null);
    setSaveOk(false);
    setEntries(
      mappings.map((m) => ({
        connector: m.connector,
        campaign_ref: m.campaign_ref,
        split_weight: m.split_weight,
      }))
    );
  }

  function addEntry() {
    setEntries((prev) => [...prev, { connector: "", campaign_ref: "", split_weight: 0 }]);
  }

  function removeEntry(i: number) {
    setEntries((prev) => prev.filter((_, idx) => idx !== i));
  }

  function updateEntry(i: number, field: keyof MappingEntry, value: string) {
    setEntries((prev) =>
      prev.map((e, idx) =>
        idx === i
          ? {
              ...e,
              [field]: field === "split_weight" ? parseFloat(value) / 100 : value,
            }
          : e
      )
    );
  }

  async function handleSave() {
    // Client-side validation: sum = 100% BEFORE sending
    if (!sumOk) {
      setSaveError(
        `The split sum is ${(totalWeight() * 100).toFixed(1)}% — it must be exactly 100%.`
      );
      return;
    }

    setSaving(true);
    setSaveError(null);
    try {
      // E3-F-2: line_key can contain "/" (e.g. "social/prospecting"). uvicorn
      // percent-decodes (including %2F) before Starlette routing, and the :path
      // converter accepts raw "/", so encodeURIComponent is already safe here.
      // We still normalize %2F back to "/" to stay server-independent.
      const lineKey = encodeURIComponent(line.line_key).replace(/%2F/gi, "/");
      const resp = await fetch(
        `${apiBase}/api/mediaplans/${encodeURIComponent(planId)}/lines/${lineKey}/mappings`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mappings: entries }),
        }
      );
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        setSaveError(
          data?.message ?? `HTTP ${resp.status} — the server rejected the mappings.`
        );
        return;
      }
      setSaveOk(true);
      setEditing(false);
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : "Unexpected error.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div>
      {/* Existing mappings */}
      {mappings.length === 0 && !editing ? (
        <p className="imports-section-note">No mapping defined for this line.</p>
      ) : !editing ? (
        <div className="panel imports-panel" style={{ marginBottom: 10 }}>
          <div className="table-scroll" tabIndex={0} aria-label="Line mappings">
            <table className="imports-table">
              <thead>
                <tr>
                  <th>Connector</th>
                  <th>Campaign</th>
                  <th>Split</th>
                  <th>State</th>
                </tr>
              </thead>
              <tbody>
                {mappings.map((m) => (
                  <tr key={m.id} data-testid={`mapping-row-${m.id}`}>
                    <td>{m.connector}</td>
                    <td>{m.campaign_ref}</td>
                    <td className="num">{fmtPct(m.split_weight)}</td>
                    <td>
                      <span
                        className={`imports-chip ${m.status === "orphaned" ? "warning" : "success"}`}
                        aria-label={`State: ${
                          m.status === "orphaned"
                            ? "orphaned — line disappeared from a previous version"
                            : "active"
                        }`}
                      >
                        {m.status === "orphaned" ? "Orphaned" : "Active"}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}

      {saveOk && (
        <div className="imports-alert success" role="status">
          Mappings saved.
        </div>
      )}

      {/* Editor */}
      {editing && (
        <div style={{ marginBottom: 10 }}>
          {entries.map((e, i) => (
            // eslint-disable-next-line react/no-array-index-key
            <div className="imports-split-row" key={i}>
              <div className="grow-1">
                <span className="imports-label">Connector</span>
                <input
                  className="imports-input"
                  value={e.connector}
                  onChange={(ev) => updateEntry(i, "connector", ev.target.value)}
                  aria-label={`Connector row ${i + 1}`}
                />
              </div>
              <div className="grow-2">
                <span className="imports-label">Campaign (ref)</span>
                <input
                  className="imports-input"
                  value={e.campaign_ref}
                  onChange={(ev) => updateEntry(i, "campaign_ref", ev.target.value)}
                  aria-label={`Campaign row ${i + 1}`}
                />
              </div>
              <div className="w-pct">
                <span className="imports-label">Split (%)</span>
                <input
                  className="imports-input"
                  type="number"
                  value={(e.split_weight * 100).toFixed(1)}
                  onChange={(ev) => updateEntry(i, "split_weight", ev.target.value)}
                  min={0}
                  max={100}
                  step={0.1}
                  aria-label={`Split percentage row ${i + 1}`}
                />
              </div>
              <div>
                <span className="imports-label" aria-hidden="true">
                  &nbsp;
                </span>
                <button
                  type="button"
                  className="imports-icon-button"
                  onClick={() => removeEntry(i)}
                  aria-label={`Remove mapping ${i + 1}`}
                  data-testid={`remove-mapping-${i}`}
                >
                  ×
                </button>
              </div>
            </div>
          ))}

          {/* Sum indicator */}
          <span
            className={`imports-sum-indicator ${sumOk ? "ok" : "off"}`}
            data-testid="split-sum-indicator"
            aria-live="polite"
          >
            Split sum: {(totalWeight() * 100).toFixed(1)}%
            {sumOk ? " ✓" : " — must be 100%"}
          </span>

          {saveError && (
            <div
              className="imports-alert error"
              role="alert"
              data-testid={`mapping-save-error-${line.line_key}`}
            >
              {saveError}
            </div>
          )}

          <div className="imports-actions">
            <button className="secondary-button" type="button" onClick={addEntry}>
              + Add campaign
            </button>
            <button
              className="quiet-button"
              type="button"
              onClick={() => {
                setEditing(false);
                setSaveError(null);
              }}
            >
              Cancel
            </button>
            <button
              className="primary-button"
              type="button"
              onClick={handleSave}
              disabled={saving || !sumOk}
              data-testid={`mapping-save-btn-${line.line_key}`}
            >
              {saving ? "Saving…" : "Save"}
            </button>
          </div>
        </div>
      )}

      {!editing && !line.is_plan_only && (
        <button
          className="quiet-button"
          type="button"
          onClick={startEditing}
          data-testid={`edit-mapping-${line.line_key}`}
        >
          Edit mappings
        </button>
      )}
      {line.is_plan_only && (
        <span className="imports-subnote" style={{ margin: 0 }}>
          Plan-only line — no real-data mapping.
        </span>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// MappingTab
// ---------------------------------------------------------------------------

export default function MappingTab({ plan, apiBase = "" }: MappingTabProps) {
  const [mappingsByLine, setMappingsByLine] = useState<Record<string, LineMapping[]>>({});
  const [mappingsLoading, setMappingsLoading] = useState(true);
  const [mappingsError, setMappingsError] = useState<string | null>(null);

  const [unmapped, setUnmapped] = useState<UnmappedActual[]>([]);
  const [unmappedAsOf, setUnmappedAsOf] = useState<string | null>(null);
  const [unmappedLoading, setUnmappedLoading] = useState(true);
  const [unmappedError, setUnmappedError] = useState<string | null>(null);

  const loadAll = useCallback(async () => {
    // Mappings
    setMappingsLoading(true);
    setMappingsError(null);
    try {
      const resp = await fetch(
        `${apiBase}/api/mediaplans/${encodeURIComponent(plan.id)}/mappings`
      );
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        throw new Error(data?.message ?? `HTTP ${resp.status}`);
      }
      const data = (await resp.json()) as { by_line: Record<string, LineMapping[]> };
      setMappingsByLine(data.by_line ?? {});
    } catch (err) {
      setMappingsError(err instanceof Error ? err.message : String(err));
    } finally {
      setMappingsLoading(false);
    }

    // Unmapped actuals
    setUnmappedLoading(true);
    setUnmappedError(null);
    try {
      const resp = await fetch(
        `${apiBase}/api/mediaplans/${encodeURIComponent(plan.id)}/unmapped-actuals`
      );
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        throw new Error(data?.message ?? `HTTP ${resp.status} — real spend unavailable.`);
      }
      const data = (await resp.json()) as { actuals: UnmappedActual[]; as_of: string | null };
      setUnmapped(data.actuals ?? []);
      setUnmappedAsOf(data.as_of ?? null);
    } catch (err) {
      setUnmappedError(err instanceof Error ? err.message : String(err));
    } finally {
      setUnmappedLoading(false);
    }
  }, [plan.id, apiBase]);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  const activeLines: MediaPlanLine[] = plan.lines ?? [];

  return (
    <div>
      {/* ------------------------------------------------------------------ */}
      {/* Mappings per line                                                   */}
      {/* ------------------------------------------------------------------ */}
      <section className="imports-section">
        <h3 className="imports-section-title">Line ↔ real-campaign mappings</h3>

        {mappingsError && (
          <div className="imports-alert error" role="alert" data-testid="mappings-error">
            {mappingsError}
          </div>
        )}

        {mappingsLoading ? (
          <div className="imports-inline-state">
            <span className="imports-spinner" aria-hidden="true" />
            Loading mappings…
          </div>
        ) : activeLines.length === 0 ? (
          <p className="imports-section-note">
            No lines in the active version — publish a version to start mapping.
          </p>
        ) : (
          <div>
            {activeLines.map((line) => (
              <div
                className="imports-line-block"
                key={line.line_key}
                data-testid={`line-mapping-section-${line.line_key}`}
              >
                <div className="imports-line-head">
                  <strong>{line.label}</strong>
                  {line.support && (
                    <span className="imports-cell-muted">({line.support})</span>
                  )}
                  {line.is_plan_only && (
                    <span className="imports-chip" aria-label="Plan-only line — no real data">
                      Plan only
                    </span>
                  )}
                </div>
                <LineMappingEditor
                  line={line}
                  mappings={mappingsByLine[line.line_key] ?? []}
                  apiBase={apiBase}
                  planId={plan.id}
                />
              </div>
            ))}
          </div>
        )}
      </section>

      {/* ------------------------------------------------------------------ */}
      {/* Unmapped actuals — never hidden (AD-9)                              */}
      {/* ------------------------------------------------------------------ */}
      <section className="imports-editor" data-testid="unmapped-actuals-section">
        <h3>Unmapped actuals</h3>
        <p className="imports-section-note">
          Real spend within the plan scope that is not attached to a line. This section can never
          be hidden (invariant).
          {unmappedAsOf &&
            ` — As of ${new Date(unmappedAsOf).toLocaleDateString("en-GB", {
              day: "numeric",
              month: "short",
              year: "numeric",
            })}.`}
        </p>

        {unmappedError && (
          <div className="imports-alert warning" data-testid="unmapped-error">
            {unmappedError}
          </div>
        )}

        {unmappedLoading && (
          <div className="imports-inline-state">
            <span className="imports-spinner" aria-hidden="true" />
            Loading…
          </div>
        )}

        {!unmappedLoading && !unmappedError && unmapped.length === 0 && (
          <p className="imports-section-note" data-testid="unmapped-empty" style={{ margin: 0 }}>
            No unmapped spend within this plan's scope.
          </p>
        )}

        {!unmappedLoading && unmapped.length > 0 && (
          <div className="panel imports-panel">
            <div className="table-scroll" tabIndex={0} aria-label="Unmapped actuals">
              <table className="imports-table" data-testid="unmapped-table">
                <thead>
                  <tr>
                    <th>Connector</th>
                    <th>Campaign</th>
                    <th className="num">Spend</th>
                  </tr>
                </thead>
                <tbody>
                  {unmapped.map((a, i) => (
                    // eslint-disable-next-line react/no-array-index-key
                    <tr key={`${a.connector}-${a.campaign_ref}-${i}`} data-testid={`unmapped-row-${i}`}>
                      <td>{a.connector}</td>
                      <td>{a.campaign_ref}</td>
                      <td className="num">{fmtMoney(a.spend, a.currency)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
