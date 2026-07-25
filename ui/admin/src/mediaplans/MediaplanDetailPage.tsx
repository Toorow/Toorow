/**
 * MediaplanDetailPage — a single media plan with its tabbed sections.
 *
 * Tabs:
 *   1. Lines & versions (LignesVersionsTab)
 *   2. Excel import (ImportTab)
 *   3. Mapping (MappingTab)
 *   4. Pacing (PacingTab)
 *
 * Restyled onto the v3 design system: content lives inside the shell <main>,
 * using application.css shell classes + imports.css for the breadcrumb, tab bar,
 * and header. A non-active version is read-only (surfaced within each tab).
 *
 * AD-15: everything goes through the server API — no direct DB access.
 * RBAC: 404/403 errors are rendered honestly.
 */
import { useCallback, useEffect, useState } from "react";
import type { MediaPlanDetail } from "./types";
import LignesVersionsTab from "./LignesVersionsTab";
import ImportTab from "./ImportTab";
import MappingTab from "./MappingTab";
import PacingTab from "./PacingTab";
import "../shell/application.css";
import "./imports.css";
import { apiFetch } from "../lib/apiFetch";

// ---------------------------------------------------------------------------
// Tab types
// ---------------------------------------------------------------------------

type DetailTab = "lignes" | "import" | "mapping" | "pacing";

const TAB_LABELS: Record<DetailTab, string> = {
  lignes: "Lines & versions",
  import: "Excel import",
  mapping: "Mapping",
  pacing: "Pacing",
};

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface MediaplanDetailPageProps {
  planId: string;
  onBack: () => void;
  apiBase?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function MediaplanDetailPage({
  planId,
  onBack,
  apiBase = "",
}: MediaplanDetailPageProps) {
  const [plan, setPlan] = useState<MediaPlanDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<DetailTab>("lignes");

  // ---------------------------------------------------------------------------
  // Loading
  // ---------------------------------------------------------------------------

  const loadPlan = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await apiFetch(
        `${apiBase}/api/mediaplans/${encodeURIComponent(planId)}`
      );
      if (resp.status === 403 || resp.status === 404) {
        const data = await resp.json().catch(() => null);
        throw new Error(
          data?.message ??
            (resp.status === 403
              ? "Access denied — insufficient rights for this plan."
              : "Plan not found.")
        );
      }
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        throw new Error(data?.message ?? `HTTP ${resp.status}`);
      }
      setPlan((await resp.json()) as MediaPlanDetail);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [planId, apiBase]);

  useEffect(() => {
    void loadPlan();
  }, [loadPlan]);

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  return (
    <div className="imports-surface">
      <nav className="imports-breadcrumb" aria-label="Breadcrumb">
        <button className="imports-back" type="button" onClick={onBack} data-testid="back-to-list">
          ← Media plans
        </button>
        {plan && (
          <>
            <span className="sep" aria-hidden="true">
              /
            </span>
            <strong>{plan.name}</strong>
          </>
        )}
      </nav>

      {loading && (
        <div className="imports-inline-state">
          <span className="imports-spinner" aria-hidden="true" />
          Loading plan…
        </div>
      )}

      {error && (
        <div className="imports-alert error" role="alert" data-testid="plan-detail-error">
          {error}
        </div>
      )}

      {!loading && plan && (
        <>
          <div className="page-header">
            <div>
              <h1>{plan.name}</h1>
              <p>
                {plan.currency} · Created {new Date(plan.created_at).toLocaleDateString("en-GB", {
                  day: "numeric",
                  month: "short",
                  year: "numeric",
                })}
                {plan.archived_at && (
                  <>
                    {" "}
                    · <strong>Archived</strong>
                  </>
                )}
              </p>
            </div>
          </div>

          <div className="imports-tabs" role="tablist" aria-label="Media plan sections">
            {(Object.keys(TAB_LABELS) as DetailTab[]).map((tab) => (
              <button
                key={tab}
                type="button"
                className={`imports-tab${activeTab === tab ? " active" : ""}`}
                onClick={() => setActiveTab(tab)}
                role="tab"
                aria-selected={activeTab === tab}
                aria-controls={`panel-${tab}`}
                id={`tab-${tab}`}
                data-testid={`tab-${tab}`}
              >
                {TAB_LABELS[tab]}
              </button>
            ))}
          </div>

          <div role="tabpanel" id={`panel-${activeTab}`} aria-labelledby={`tab-${activeTab}`}>
            {activeTab === "lignes" && (
              <LignesVersionsTab plan={plan} onRefresh={loadPlan} apiBase={apiBase} />
            )}
            {activeTab === "import" && (
              <ImportTab plan={plan} apiBase={apiBase} onRefresh={loadPlan} />
            )}
            {activeTab === "mapping" && <MappingTab plan={plan} apiBase={apiBase} />}
            {activeTab === "pacing" && (
              <PacingTab planId={plan.id} currency={plan.currency} apiBase={apiBase} />
            )}
          </div>
        </>
      )}
    </div>
  );
}
