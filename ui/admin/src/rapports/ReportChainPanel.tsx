/**
 * ReportChainPanel — the data chain of a report (Story 8.9), restyled onto v3.
 *
 * Shows the chain metrics → target field → datastreams for a given report, per
 * Part C item 4 + R2/R6 of Epic 8. The number is the hero: the count of
 * fed metrics is shown large + tabular. Behavior (fetch, states, data shape) is
 * unchanged; only the presentation (application.css + reports.css classes) and
 * the copy (English) changed. Tooltips remain MUI (deeply MUI, low risk) but
 * read their colors from theme tokens / reports.css.
 */

import { Tooltip } from "@mui/material";
import { useEffect, useState } from "react";
import "../reports.css";
import SharedStatusDot from "../datastreams/StatusDot";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface LastExtract {
  date: string | null;
  status: string | null;
}

export interface DatastreamEntry {
  id: string;
  name: string;
  module: string;
  enabled: boolean;
  last_extract: LastExtract;
}

export interface MetricDefinition {
  definition?: string;
  unit?: string;
  good_direction?: "up" | "down" | null;
  caveats?: string | null;
}

export interface TargetFieldSummary {
  name: string;
  display_name: string;
  measure: string | null;
  data_type: string;
}

export type ChainStatus = "ok" | "no_stream" | "not_in_dictionary";

export interface ChainMetric {
  metric: string;
  definition: MetricDefinition | null;
  target_field: TargetFieldSummary | null;
  datastreams: DatastreamEntry[];
  status: ChainStatus;
}

export interface ValidationSummary {
  ok_count: number;
  warnings: string[];
}

export interface ReportChain {
  report_id: string;
  display_name: string | null;
  metric_definitions: Record<string, MetricDefinition> | null;
  llm_commentary_guidelines: string | null;
  metrics: ChainMetric[];
  validation: ValidationSummary;
}

interface ReportChainPanelProps {
  /** Full report id: "{module}/{report_id}" */
  moduleReportId: string;
  projectId: string;
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

/** Extract verdict badge for a datastream's last extraction. */
function ExtractBadge({ status }: { status: string | null }) {
  if (!status) {
    return <span className="extract-badge none">—</span>;
  }
  const tone =
    status === "ok" ? "ok" : status === "partial" ? "partial" : "failed";
  return (
    <span className={`extract-badge ${tone}`}>
      <span className="mark" />
      {status}
    </span>
  );
}

/** Chip for a target field name. */
function FieldChip({ field }: { field: TargetFieldSummary }) {
  return (
    <Tooltip
      title={
        <div>
          <strong>{field.name}</strong>
          {field.measure && <div>Aggregation: {field.measure}</div>}
          <div>Type: {field.data_type}</div>
        </div>
      }
      arrow
    >
      <span className="field-chip">
        <code>{field.name}</code>
        <span className="field-display">{field.display_name}</span>
      </span>
    </Tooltip>
  );
}

/** Single datastream pill. */
function DatastreamPill({ ds }: { ds: DatastreamEntry }) {
  return (
    <Tooltip
      title={
        <div>
          <strong>{ds.name}</strong>
          <div>Module: {ds.module}</div>
          <div>Status: {ds.enabled ? "active" : "disabled"}</div>
          {ds.last_extract?.date && <div>Last extract: {ds.last_extract.date}</div>}
        </div>
      }
      arrow
    >
      <span className={`ds-pill${ds.enabled ? "" : " disabled"}`}>
        <SharedStatusDot status={ds.enabled ? "ok" : "empty"} size={6} showTooltip={false} />
        <span className="ds-name">{ds.name}</span>
        <ExtractBadge status={ds.last_extract?.status ?? null} />
      </span>
    </Tooltip>
  );
}

/** Warning row for no_stream / not_in_dictionary statuses. */
function MetricWarning({ status, metric }: { status: ChainStatus; metric: string }) {
  if (status === "ok") return null;

  const isNoStream = status === "no_stream";
  return (
    <div className={`metric-warning ${isNoStream ? "warning" : "error"}`}>
      {isNoStream
        ? `No active datastream feeds ${metric} for this project — set up a datastream and its mapping under the Datastreams tab.`
        : `“${metric}” is not referenced in the data dictionary — add this target field to enable full tracking.`}
    </div>
  );
}

/** One metric row in the chain. */
function MetricChainRow({ m }: { m: ChainMetric }) {
  return (
    <div data-testid={`chain-metric-${m.metric}`} className="chain-metric-row">
      {/* Chain row: metric -> field chip -> datastreams */}
      <div className="chain-flow">
        <code className="metric-token">{m.metric}</code>

        <span className="chain-arrow" aria-hidden="true">→</span>

        {/* Target field chip or not-in-dictionary flag */}
        {m.target_field ? (
          <FieldChip field={m.target_field} />
        ) : (
          <span className="chain-flag error">not referenced</span>
        )}

        {m.target_field && (
          <>
            <span className="chain-arrow" aria-hidden="true">→</span>
            {m.datastreams.length > 0 ? (
              <span className="ds-pills">
                {m.datastreams.map((ds) => (
                  <DatastreamPill key={ds.id} ds={ds} />
                ))}
              </span>
            ) : (
              <span className="chain-flag warning">no active datastream</span>
            )}
          </>
        )}
      </div>

      {/* Definition (R6): show under metric if present */}
      {m.definition && (
        <div className="metric-def">
          {m.definition.definition && (
            <span className="def-text">{m.definition.definition}</span>
          )}
          {m.definition.unit && <span className="def-unit">{m.definition.unit}</span>}
        </div>
      )}

      {/* Warning row for non-ok status */}
      <MetricWarning status={m.status} metric={m.metric} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function ReportChainPanel({
  moduleReportId,
  projectId,
}: ReportChainPanelProps) {
  const [chain, setChain] = useState<ReportChain | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [module, reportId] = moduleReportId.split("/");

  useEffect(() => {
    if (!module || !reportId || !projectId) return;
    setLoading(true);
    setError(null);

    fetch(
      `/api/reports/${encodeURIComponent(module)}/${encodeURIComponent(reportId)}/chain?project_id=${encodeURIComponent(projectId)}`,
      {
        headers: {
          Authorization: `Bearer ${localStorage.getItem("api_token") || ""}`,
        },
      }
    )
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((data) => setChain(data as ReportChain))
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [module, reportId, projectId]);

  if (loading) {
    return (
      <div className="chain-loading">
        <span role="progressbar" aria-label="Loading data chain" className="signal running" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="reports-alert" role="alert">
        Couldn't load the data chain: {error}
      </div>
    );
  }

  if (!chain) return null;

  const totalMetrics = chain.metrics.length;
  const warnCount = chain.validation.warnings.length;

  return (
    <div data-testid="report-chain-panel" className="chain-panel">
      {/* Validation summary header — the number is the hero. */}
      <div className="chain-summary">
        <div className="chain-hero">
          <span className="count">{chain.validation.ok_count}</span>
          <span className="count-label">/{totalMetrics} metrics fed</span>
        </div>

        {warnCount > 0 && (
          <span className="signal-label warning">
            <span className="signal-mark" />
            {warnCount} alert{warnCount > 1 ? "s" : ""}
          </span>
        )}
      </div>

      {/* Chain table */}
      <div className="chain-table">
        {chain.metrics.length === 0 ? (
          <div className="chain-empty">No metrics defined for this report.</div>
        ) : (
          chain.metrics.map((m) => <MetricChainRow key={m.metric} m={m} />)
        )}
      </div>

      {/* LLM commentary guidelines (R6 passthrough) */}
      {chain.llm_commentary_guidelines && (
        <div className="chain-guidelines">
          <span className="label">LLM commentary guidelines</span>
          <p>{chain.llm_commentary_guidelines}</p>
        </div>
      )}
    </div>
  );
}
