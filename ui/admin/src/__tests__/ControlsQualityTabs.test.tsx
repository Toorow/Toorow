/**
 * The three Controls & Quality workbenches (Story 49.4, AC9).
 *
 * These ten tabs replaced ten `pendingOwner` placeholders, so the first thing
 * worth asserting is that they no longer claim to be undelivered. The rest is
 * one property, tested from several angles because it is the one that decides
 * whether a governance screen is worth trusting:
 *
 *   "no owner could answer"  is not  "the owner answered none"  is not  "zero".
 *
 * A dashboard that renders all three as an empty table reports health it never
 * measured, and that is the failure the whole story is written against.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  CaseDecisionHistoryTab,
  MonitorCoverageTab,
  MonitorHistoryTab,
  MonitorIssuesTab,
  MonitorOverviewTab,
  RuleSetApprovalsExceptionsTab,
  RuleSetRulesTab,
} from "../governance/ControlsQualityTabs";
import { pendingOwner } from "../governance/contracts";

function detail(summary: Record<string, unknown>) {
  return {
    object_ref: { type: "dq-monitor", id: "dqm_1", label: "Schema drift", owner_href: "" },
    scope: "project",
    owner: {},
    lifecycle_status: "published",
    active_version_ref: null,
    selected_version_ref: null,
    available_tabs: [],
    default_tab: "overview",
    allowed_actions: [],
    used_by: { state: "empty", count: 0, refs: [] },
    versions: { state: "empty", count: 0, refs: [] },
    evidence: { state: "empty", count: 0, refs: [] },
    summary,
    evidence_as_of: null,
  } as never;
}

describe("the ten tabs are delivered", () => {
  it("no longer declares itself undelivered", () => {
    // `pendingOwner` became a lookup FUNCTION while this story was in flight.
    // The question is unchanged: does the contract still advertise these ten
    // tabs as undelivered?
    for (const [objectType, tab] of [
      ["control-case", "candidate-change"],
      ["control-case", "impact"],
      ["control-case", "decision-history"],
      ["dq-monitor", "overview"],
      ["dq-monitor", "coverage"],
      ["dq-monitor", "history"],
      ["dq-monitor", "issues"],
      ["rule-set", "rules"],
      ["rule-set", "effective-dates"],
      ["rule-set", "approvals-exceptions"],
    ]) {
      expect(
        pendingOwner(objectType, tab),
        `${objectType}:${tab} still claims Story 49.4 has not shipped it`,
      ).toBeNull();
    }
  });
});

describe("unavailable is never rendered as empty", () => {
  it("says the evidence could not be read rather than showing an empty table", () => {
    render(<CaseDecisionHistoryTab detail={detail({ facets_state: "unavailable" })} />);
    expect(screen.getByText(/could not be read/i)).toBeInTheDocument();
    expect(screen.queryByText(/No decision has been taken/i)).not.toBeInTheDocument();
  });

  it("says none when the owner answered none", () => {
    render(
      <CaseDecisionHistoryTab detail={detail({ facets_state: "available", decisions: [] })} />,
    );
    expect(screen.getByText(/No decision has been taken/i)).toBeInTheDocument();
  });

  it("keeps the two apart on the approvals and exceptions tab too", () => {
    const { unmount } = render(
      <RuleSetApprovalsExceptionsTab detail={detail({ facets_state: "unavailable" })} />,
    );
    expect(screen.getByText(/could not be read/i)).toBeInTheDocument();
    unmount();

    render(
      <RuleSetApprovalsExceptionsTab
        detail={detail({ facets_state: "available", approvals: [], exceptions: [] })}
      />,
    );
    expect(screen.getByText(/No approval is recorded/i)).toBeInTheDocument();
    expect(screen.getByText(/No exception is recorded/i)).toBeInTheDocument();
  });
});

describe("a monitor that never ran is not a monitor at zero", () => {
  it("names the state instead of implying a verdict", () => {
    render(<MonitorOverviewTab detail={detail({ runtime_state: "unavailable" })} />);
    expect(screen.getByText(/not a pass and not a failure/i)).toBeInTheDocument();
  });

  it("refuses to show a percentage over an empty denominator", () => {
    render(<MonitorCoverageTab detail={detail({ coverage: { total_eligible: 0 } })} />);
    expect(screen.getByText(/Nothing was eligible/i)).toBeInTheDocument();
    expect(screen.getByText(/no meaning/i)).toBeInTheDocument();
  });

  it("shows the denominator when there is one", () => {
    render(
      <MonitorCoverageTab
        detail={detail({
          // Distinct numbers on purpose: equal ones make the assertion ambiguous
          // and hide which cell it actually matched.
          coverage: { total_eligible: 12, evaluated: 11, passed: 8, failed: 2, unavailable: 1 },
        })}
      />,
    );
    for (const value of ["12", "11", "8", "2", "1"]) {
      expect(screen.getByText(value)).toBeInTheDocument();
    }
  });

  it("says a monitor has never been evaluated rather than showing an empty history", () => {
    render(<MonitorHistoryTab detail={detail({ facets_state: "available", evaluations: [] })} />);
    expect(screen.getByText(/never been evaluated/i)).toBeInTheDocument();
    expect(screen.getByText(/not a run that found nothing/i)).toBeInTheDocument();
  });
});

describe("what the tables must not hide", () => {
  it("lists a closed issue, so a recurrence does not look new", () => {
    render(
      <MonitorIssuesTab
        detail={detail({
          facets_state: "available",
          issues: [
            {
              id: "dqi_1",
              status: "resolved",
              severity: "degrading",
              first_seen_at: "2026-01-01",
              last_seen_at: "2026-02-01",
              event_count: 4,
            },
          ],
        })}
      />,
    );
    expect(screen.getByText("resolved")).toBeInTheDocument();
    expect(screen.getByText("4")).toBeInTheDocument();
  });

  it("marks an expired exception as expired instead of dropping it", () => {
    render(
      <RuleSetApprovalsExceptionsTab
        detail={detail({
          facets_state: "available",
          approvals: [],
          exceptions: [
            {
              id: "grse_1",
              subject_kind: "datastream",
              subject_id: "ds_1",
              reason_code: "known_gap",
              reason: "Accepted for the quarter.",
              effective_from: "2026-01-01",
              expires_at: "2026-02-01",
              decided_by: "operator@example.com",
              in_force: false,
            },
          ],
        })}
      />,
    );
    expect(screen.getByText("Expired")).toBeInTheDocument();
  });

  it("shows an open-ended effective window as open-ended, never as today", () => {
    render(
      <RuleSetApprovalsExceptionsTab
        detail={detail({
          facets_state: "available",
          approvals: [],
          exceptions: [
            {
              id: "grse_2",
              subject_kind: "datastream",
              subject_id: "ds_2",
              reason_code: "permanent",
              reason: "No end date was set.",
              effective_from: "2026-01-01",
              expires_at: null,
              decided_by: "operator@example.com",
              in_force: true,
            },
          ],
        })}
      />,
    );
    expect(screen.getByText("Never")).toBeInTheDocument();
    // "In force" is also the column header, so the badge is the SECOND match.
    expect(screen.getAllByText("In force")).toHaveLength(2);
  });

  it("shows every rule source pinned to a version", () => {
    render(
      <RuleSetRulesTab
        detail={detail({
          facets_state: "available",
          ordered_rules: [
            {
              method: "PRIORITY",
              concept: { object_id: "mdm_conv", version_id: "scv_1" },
              sources: [
                { kind: "datastream", object_id: "ds_a", version_id: "dmv_1" },
                { kind: "datastream", object_id: "ds_b", version_id: "dmv_2" },
              ],
              note: null,
            },
          ],
        })}
      />,
    );
    // A rule bound to "the latest" would change meaning without a new version.
    expect(screen.getByText(/ds_a@dmv_1, ds_b@dmv_2/)).toBeInTheDocument();
  });

  it("renders a failed owner handoff as a failure, not as a decision taken", () => {
    render(
      <CaseDecisionHistoryTab
        detail={detail({
          facets_state: "available",
          decisions: [
            {
              id: "ctcd_1",
              decided_at: "2026-02-01",
              decision_kind: "approve_mapping",
              actor: "operator@example.com",
              reason: "Approved.",
              effective_from: "2026-02-01",
              owner_outcome: "failed",
            },
          ],
        })}
      />,
    );
    expect(screen.getByText("Owner command failed")).toBeInTheDocument();
  });
});
