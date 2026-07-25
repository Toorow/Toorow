/**
 * ReportsPanel — Analyze › Reports surface (restyled onto the v3 design system).
 *
 * Renders the page content inside the shell's <main>: a page-header, then a
 * panel with a table of the reports available to the project. Each row exposes
 * the report's enabled state (as a v3 signal-label), an accessible enable
 * toggle, and an expandable data-chain drawer (ReportChainPanel) that shows how
 * the report's metrics map to target fields and datastreams.
 *
 * Behavior is unchanged from the prior version: same /api/reports/available
 * fetch, same PATCH toggle, same loading/empty/error states, same chain drawer.
 * Only the presentation (v3 chrome, application.css + reports.css classes) and
 * the copy (English) changed.
 */
import { useCallback, useEffect, useState } from "react";
import "./shell/application.css";
import "./reports.css";
import ReportChainPanel from "./rapports/ReportChainPanel";

export interface AvailableReport {
  module_name: string;
  report_id: string;
  display_name: string;
  enabled: boolean;
  display_order: number;
}

interface ReportsPanelProps {
  projectId?: string;
}

function ReportRow({
  report,
  projectId,
  onToggle,
}: {
  report: AvailableReport;
  projectId: string;
  onToggle: (report: AvailableReport) => Promise<void>;
}) {
  const [expanded, setExpanded] = useState(false);
  const flowId = `${report.module_name}/${report.report_id}`;

  return (
    <>
      <tr className="report-row">
        <td>
          <div className="report-name">
            <strong>{report.display_name}</strong>
            <span className="report-module">{report.module_name}</span>
          </div>
        </td>
        <td>
          <span className={`signal-label ${report.enabled ? "success" : "info"}`}>
            <span className="signal-mark" />
            {report.enabled ? "Enabled" : "Disabled"}
          </span>
        </td>
        <td>
          <button
            type="button"
            role="switch"
            aria-checked={report.enabled}
            aria-label={`Enable report ${report.display_name}`}
            className="report-toggle"
            onClick={() => void onToggle(report)}
          />
        </td>
        <td>
          <div className="report-actions">
            <button
              type="button"
              className="quiet-button chain-toggle"
              data-testid={`chain-toggle-${flowId}`}
              aria-expanded={expanded}
              title={expanded ? "Hide data chain" : "Show data chain"}
              onClick={() => setExpanded((e) => !e)}
            >
              <span className="caret">›</span>
              Data chain
            </button>
          </div>
        </td>
      </tr>

      {expanded && (
        <tr className="chain-drawer-row">
          <td colSpan={4} className="chain-drawer-cell">
            <span className="chain-drawer-label">
              Data chain — metrics → target fields → datastreams
            </span>
            <ReportChainPanel moduleReportId={flowId} projectId={projectId} />
          </td>
        </tr>
      )}
    </>
  );
}

export default function ReportsPanel({ projectId = "default" }: ReportsPanelProps) {
  const [reports, setReports] = useState<AvailableReport[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchReports = useCallback(async () => {
    try {
      const resp = await fetch(`/api/reports/available?project_id=${encodeURIComponent(projectId)}`);
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      const data: AvailableReport[] = await resp.json();
      setReports(data ?? []);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void fetchReports();
  }, [fetchReports]);

  const handleToggle = useCallback(
    async (report: AvailableReport) => {
      try {
        const resp = await fetch(
          `/api/reports/${projectId}/${report.module_name}/${report.report_id}`,
          {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              enabled: !report.enabled,
              display_order: report.display_order,
            }),
          }
        );
        if (!resp.ok) {
          const body = await resp.json().catch(() => ({}));
          throw new Error(body.message ?? `HTTP ${resp.status}`);
        }
        await fetchReports();
      } catch (err) {
        setError(`Error updating report: ${err instanceof Error ? err.message : String(err)}`);
      }
    },
    [projectId, fetchReports]
  );

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Reports</h1>
          <p>
            Analytical reports available to this project. Enable a report to
            surface it, and inspect the data chain that feeds each one.
          </p>
        </div>
      </div>

      {loading ? (
        <div className="reports-loading">Loading reports…</div>
      ) : error ? (
        <div className="reports-alert" role="alert">
          Couldn't load reports: {error}
        </div>
      ) : (
        <section className="panel reports-panel">
          <div className="reports-panel-header">
            <div>
              <h2>Available reports</h2>
              <p>Each report reads from a defined chain of metrics and datastreams.</p>
            </div>
          </div>

          {reports.length === 0 ? (
            <div className="chain-empty">No reports available.</div>
          ) : (
            <table className="reports-table">
              <thead>
                <tr>
                  <th>Report</th>
                  <th>State</th>
                  <th>Enabled</th>
                  <th>
                    <span className="sr-only">Data chain</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {reports.map((report) => (
                  <ReportRow
                    key={`${report.module_name}/${report.report_id}`}
                    report={report}
                    projectId={projectId}
                    onToggle={handleToggle}
                  />
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}
    </>
  );
}
