/**
 * Activity — Governance › Activity surface (v3).
 *
 * Puts the two "who/what happened" surfaces of governance on one page: the
 * definition-changes audit log (PANEL 1 — Changes) and the notification rules
 * (PANEL 2 — Alert rules) — the "alerts and logs together" idea.
 *
 * The application shell (ApplicationShell.tsx) already renders the frame,
 * sidebar, topbar, and <main>. This component renders ONLY the page content:
 * the page header and the two panels.
 *
 * Styling: application.css (global, via the shell) for shell/layout classes
 * (page-header, header-actions, panel, secondary-button, primary-button,
 * quiet-button, signal-label, signal-mark, number, event/mono, table-scroll) +
 * activity.css for the page-specific panel headers, table cell layouts, and
 * the calm alert-rule rows. Colors come exclusively from the application.css
 * CSS variables (dark-safe); dates/counts use Geist tabular, ids use mono.
 *
 * ── Data ─────────────────────────────────────────────────────────────────────
 * PANEL 1 is the type-1 distributed log: it draws on the shared audit registry
 * (envelope provenance, AD-9). A real GET /api/audit is NOT wired yet, so the
 * rows below are designed placeholders, flagged with // TODO(api).
 *
 * PANEL 2 rebuilds the intent of the legacy AlertsPanel.tsx (a static MUI
 * mockup) in v3 chrome + English. There is no rules backend yet, so the rules
 * are literals, also flagged with // TODO(api).
 */
import type { ReactElement, ReactNode } from "react";
import "../application.css";
import "./activity.css";

// ---------------------------------------------------------------------------
// PANEL 1 — Changes (definition-changes audit log)
// ---------------------------------------------------------------------------

type Signal = "success" | "warning" | "error" | "info";

interface ChangeRow {
  /** ISO-ish "when" — split into date + time in the cell (Geist tabular). */
  date: string;
  time: string;
  actor: string; // identity that made the change
  action: string; // e.g. "Approved field", "Published mapping"
  /** Canonical id / target — mono when it is a real identifier. */
  entity: string;
  entityMono: boolean;
  result: string; // signal-label copy
  resultSignal: Signal;
}

// Designed placeholder rows. TODO(api): source from the shared audit registry
// (GET /api/audit — AD-9 envelope provenance) once wired; nothing is real yet.
const CHANGE_ROWS: ChangeRow[] = [
  {
    date: "22 Jul 2026",
    time: "16:42",
    actor: "Jean Albany",
    action: "Approved field",
    entity: "mdm_…MEDIA_SPEND",
    entityMono: true,
    result: "Published",
    resultSignal: "success",
  },
  {
    date: "22 Jul 2026",
    time: "14:08",
    actor: "Marie Chen",
    action: "Published mapping",
    entity: "Meta Ads → Media spend",
    entityMono: false,
    result: "Live",
    resultSignal: "success",
  },
  {
    date: "21 Jul 2026",
    time: "11:27",
    actor: "Jean Albany",
    action: "Resolved currency conflict",
    entity: "mdm_…REVENUE",
    entityMono: true,
    result: "Resolved",
    resultSignal: "success",
  },
  {
    date: "21 Jul 2026",
    time: "09:15",
    actor: "System",
    action: "Activated Country split",
    entity: "module:country-split",
    entityMono: true,
    result: "Active",
    resultSignal: "info",
  },
  {
    date: "20 Jul 2026",
    time: "18:51",
    actor: "Northwind Studio",
    action: "Added tracked brand",
    entity: "brand:northwind",
    entityMono: true,
    result: "Added",
    resultSignal: "info",
  },
  {
    date: "20 Jul 2026",
    time: "10:33",
    actor: "Marie Chen",
    action: "Created canonical field",
    entity: "mdm_…ROAS",
    entityMono: true,
    result: "Draft",
    resultSignal: "warning",
  },
  {
    date: "19 Jul 2026",
    time: "15:20",
    actor: "Jean Albany",
    action: "Rejected mapping",
    entity: "Google Sheets → Revenue",
    entityMono: false,
    result: "Rejected",
    resultSignal: "error",
  },
  {
    date: "18 Jul 2026",
    time: "08:47",
    actor: "System",
    action: "Activated FX module",
    entity: "module:currency-fx",
    entityMono: true,
    result: "Active",
    resultSignal: "info",
  },
];

// ---------------------------------------------------------------------------
// PANEL 2 — Alert rules (notification rules)
// Rebuilt intent of the legacy AlertsPanel.tsx (static MUI mockup) in v3 + EN.
// ---------------------------------------------------------------------------

interface AlertRule {
  name: string;
  condition: string;
  channel: string; // Email / Slack
  active: boolean;
}

// TODO(api): no rules backend yet — these are literals mirroring the intent of
// the legacy AlertsPanel (extraction delay, conversion-rate drop, spend cap).
const ALERT_RULES: AlertRule[] = [
  {
    name: "Extraction delay",
    condition: "Stream pull delayed > 3h",
    channel: "Email & Slack",
    active: true,
  },
  {
    name: "Conversion-rate drop",
    condition: "Daily conversion rate drops > 25%",
    channel: "Slack #growth-alerts",
    active: true,
  },
  {
    name: "Spend cap exceeded",
    condition: "Daily spend > $5,000",
    channel: "Email",
    active: true,
  },
  {
    name: "Schema drift",
    condition: "Column set differs from reference",
    channel: "Slack #data-eng",
    active: false,
  },
];

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface ActivityProps {
  projectId?: string;
}

export default function Activity({ projectId }: ActivityProps): ReactElement {
  // projectId will scope GET /api/audit + the rules list once those are wired.
  void projectId;

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Activity</h1>
          <p>
            Every change to a definition and every alert rule, together — who changed what,
            and what gets flagged when it drifts.
          </p>
        </div>
        <div className="header-actions">
          {/* TODO(api): export the audit log (GET /api/audit — AD-9). */}
          <button className="secondary-button" type="button">
            Export log
          </button>
          {/* TODO(api): open the add-rule flow (no rules backend yet). */}
          <button className="primary-button" type="button">
            + Add rule
          </button>
        </div>
      </div>

      {/* PANEL 1 — Changes -------------------------------------------------- */}
      <section className="panel activity-panel">
        <div className="activity-panel-header">
          <div>
            <h2>Changes</h2>
            <p>
              Definition-changes audit log — canonical fields, mappings, conflicts, modules,
              and tracked brands.
            </p>
          </div>
          {/* TODO(api): actor / action filter over the audit registry. */}
          <button className="selector" type="button">
            All actors
          </button>
        </div>
        <div className="table-scroll" tabIndex={0} aria-label="Definition changes audit log">
          <table className="activity-table">
            <thead>
              <tr>
                <th style={{ width: "18%" }}>When</th>
                <th style={{ width: "18%" }}>Actor</th>
                <th style={{ width: "24%" }}>Action</th>
                <th style={{ width: "24%" }}>Entity</th>
                <th style={{ width: "16%" }}>Result</th>
              </tr>
            </thead>
            <tbody>
              {/* TODO(api): rows are designed placeholders (GET /api/audit not wired). */}
              {CHANGE_ROWS.map((row, i) => (
                <tr key={`${row.date}-${row.time}-${i}`}>
                  <td>
                    <div className="when-cell">
                      <strong className="number">{row.date}</strong>
                      <small className="number">{row.time}</small>
                    </div>
                  </td>
                  <td>
                    <span className="actor-cell">{row.actor}</span>
                  </td>
                  <td>{row.action}</td>
                  <td>
                    {row.entityMono ? (
                      <span className="mono entity-id">{row.entity}</span>
                    ) : (
                      <span className="entity-text">{row.entity}</span>
                    )}
                  </td>
                  <td>
                    <span className={`signal-label ${row.resultSignal}`}>
                      <span className="signal-mark" />
                      {row.result}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="activity-footer">
          <span>
            Showing <span className="number">{CHANGE_ROWS.length}</span> recent changes
          </span>
          {/* TODO(api): paginate the audit registry. */}
          <span className="activity-footer-note">Shared audit registry · AD-9 provenance</span>
        </div>
      </section>

      {/* PANEL 2 — Alert rules --------------------------------------------- */}
      <section className="panel activity-panel">
        <div className="activity-panel-header">
          <div>
            <h2>Alert rules</h2>
            <p>
              Notification rules — each watches a condition and posts to a channel when it
              fires. No configuration is required to receive them.
            </p>
          </div>
          {/* TODO(api): open the add-rule flow (no rules backend yet). */}
          <button className="quiet-button" type="button">
            + Add rule
          </button>
        </div>
        <ul className="rule-list">
          {/* TODO(api): rules are literals — no rules backend yet. */}
          {ALERT_RULES.map((rule) => (
            <li className="rule-row" key={rule.name}>
              <div className="rule-main">
                <strong>{rule.name}</strong>
                <small>{rule.condition}</small>
              </div>
              <div className="rule-channel">
                <span className="channel-label">{rule.channel}</span>
              </div>
              <div className="rule-state">
                {renderRuleState(rule.active)}
              </div>
            </li>
          ))}
        </ul>
      </section>
    </>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function renderRuleState(active: boolean): ReactNode {
  return active ? (
    <span className="signal-label success">
      <span className="signal-mark" />
      Active
    </span>
  ) : (
    <span className="signal-label">
      <span className="signal-mark" />
      Off
    </span>
  );
}
