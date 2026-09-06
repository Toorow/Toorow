/**
 * The three Controls & Quality workbenches (Story 49.4, AC9).
 *
 * Ten tabs across three object types, and every one of them was a
 * `pendingOwner` entry until this file existed — the chassis showed "not
 * delivered by Story 49.4" and named itself. That placeholder was honest and is
 * now wrong, so it goes with these.
 *
 * One rule runs through all ten, and it is the reason several of them look
 * wordier than a table needs to be:
 *
 *   nothing renders a zero it cannot vouch for.
 *
 * The server distinguishes "the owner answered none" from "no owner could
 * answer" (`facets_state`), and each tab below keeps that distinction on screen.
 * A DQ monitor with no evaluation is not a monitor at 0% — it is a monitor
 * nobody has run, and those are different facts with different next actions.
 *
 * Everything is read from `detail.summary`, composed server-side. AC8 forbids
 * the browser joining across owner APIs, and the reason is not tidiness: a
 * fan-out across four endpoints looks authoritative and is wrong the moment one
 * of them fails.
 */
import { useState } from "react";
import {
  Badge,
  Button,
  EmptyState,
  Panel,
  PanelHeader,
  Status,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  TableScroll,
  displayValue,
} from "../ui";
import CaseDecisionDialog, { type CaseCandidate } from "./CaseDecisionDialog";
import type { GovernanceObject } from "./governanceSurface";
import RuleSetVersionAuthoring from "./RuleSetVersionAuthoring";
import {
  PolicyOnlyRulesTab,
  TaxFeeLadderRulesTab,
  isPolicyOnlyRuleSet,
  isTaxFeeRuleSet,
} from "./TaxFeeLadderTabs";

type Row = Record<string, unknown>;

function summaryOf(detail: GovernanceObject): Record<string, unknown> {
  return (detail.summary ?? {}) as Record<string, unknown>;
}

function rowsOf(detail: GovernanceObject, key: string): Row[] {
  const value = summaryOf(detail)[key];
  return Array.isArray(value) ? (value as Row[]) : [];
}

/**
 * The one place the three states are told apart. `unavailable` is not `empty`,
 * and neither is a count of zero — collapsing them is how a governance screen
 * starts reporting health it never measured.
 */
function FacetState({
  detail,
  emptyTitle,
  emptyDescription,
  children,
  rows,
}: {
  detail: GovernanceObject;
  emptyTitle: string;
  emptyDescription: string;
  rows: Row[];
  children: React.ReactNode;
}) {
  const state = summaryOf(detail).facets_state;
  if (state === "unavailable") {
    return (
      <Status as="block" tone="warning" title="This evidence could not be read">
        Its owner did not answer. This is not a count of zero, and nothing below should
        be read as one.
      </Status>
    );
  }
  if (rows.length === 0) {
    return <EmptyState title={emptyTitle} description={emptyDescription} />;
  }
  return <>{children}</>;
}

function DataTable({
  caption,
  columns,
  rows,
  render,
}: {
  caption: string;
  columns: string[];
  rows: Row[];
  render: (row: Row) => React.ReactNode[];
}) {
  return (
    <TableScroll label={caption}>
      <Table>
        <caption className="sr-only">{caption}</caption>
        <TableHeader>
          <TableRow>
            {columns.map((column) => (
              <TableHead key={column}>{column}</TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((row, index) => (
            <TableRow key={String(row.id ?? index)}>
              {render(row).map((cell, cellIndex) => (
                <TableCell key={columns[cellIndex] ?? cellIndex}>{cell}</TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableScroll>
  );
}

// ---------------------------------------------------------------------------
// Control Case
// ---------------------------------------------------------------------------

export function CaseCandidateChangeTab({ detail }: { detail: GovernanceObject }) {
  const rows = rowsOf(detail, "candidates");
  return (
    <Panel flush>
      <PanelHeader
        title="Candidate change"
        description="What could be done about this case. A candidate is held by its owner and referenced here; Governance never copies it."
      />
      <FacetState
        detail={detail}
        rows={rows}
        emptyTitle="No candidate change is attached"
        emptyDescription="Nothing has been proposed for this case yet. Its owner answered, and the answer is none."
      >
        <DataTable
          caption="Candidate changes"
          columns={["Kind", "Owner", "Object", "Version", "Proposed"]}
          rows={rows}
          render={(row) => [
            displayValue(row.candidate_kind),
            <Badge key="owner" tone={row.owner_kind === "data" ? "info" : "neutral"}>
              {displayValue(row.owner_kind)}
            </Badge>,
            `${displayValue(row.owner_object_type)} ${displayValue(row.owner_object_id)}`,
            displayValue(row.owner_version_id),
            displayValue(row.proposed_at),
          ]}
        />
      </FacetState>
    </Panel>
  );
}

export function CaseImpactTab({ detail }: { detail: GovernanceObject }) {
  const rows = rowsOf(detail, "impacts");
  return (
    <Panel flush>
      <PanelHeader
        title="Impact"
        description="Server-derived, never supplied by a caller: an impact a client sent describes what the client claimed."
      />
      <FacetState
        detail={detail}
        rows={rows}
        emptyTitle="No impact has been derived"
        emptyDescription="An impact is derived when a candidate is reviewed. None has been."
      >
        <DataTable
          caption="Derived impacts"
          columns={["Derived", "Affected", "Coverage", "Dependency fingerprint"]}
          rows={rows}
          render={(row) => [
            displayValue(row.derived_at),
            displayValue(row.affected),
            displayValue(row.coverage),
            <span key="fp" className="font-mono text-caption">
              {String(row.dependency_fingerprint ?? "").slice(0, 12)}…
            </span>,
          ]}
        />
      </FacetState>
    </Panel>
  );
}

/**
 * The one tab in this file that WRITES, and it was the last thing missing.
 *
 * `governance.md:111` — "Governance owns the consolidated policy, case and
 * decision". The command family has been served since 49.4 and this screen never
 * called it: measured 2026-08-04, the whole file held zero POST and zero
 * buttons, so a decision could be read and never taken (AI-192). The act lives
 * here rather than on the semantic-concept workbench because that is where the
 * target puts it — and because the legacy `target_fields` approval route writes
 * a parallel semantic store that `governance.md:853-854` refuses.
 */
export function CaseDecisionHistoryTab({
  detail,
  projectId,
  onDecided,
}: {
  detail: GovernanceObject;
  projectId?: string;
  onDecided?: () => void;
}) {
  const rows = rowsOf(detail, "decisions");
  const candidates = rowsOf(detail, "candidates") as CaseCandidate[];
  const [deciding, setDeciding] = useState(false);
  const caseId = detail.object_ref?.id ?? "";
  // No project, no case id, no write. The dialog would have nothing to address,
  // and a button that cannot act is the read-only theatre this tab just left.
  const canDecide = Boolean(projectId) && Boolean(caseId);
  return (
    <Panel flush>
      <PanelHeader
        title="Decision history"
        description="Append-only. A decision is never edited and never deleted, so this list only ever grows."
        actions={
          canDecide ? (
            <Button onClick={() => setDeciding(true)}>Take a decision</Button>
          ) : undefined
        }
      />
      {canDecide && (
        <CaseDecisionDialog
          open={deciding}
          projectId={projectId as string}
          caseId={caseId}
          candidates={candidates}
          onClose={() => setDeciding(false)}
          onDecided={() => onDecided?.()}
        />
      )}
      <FacetState
        detail={detail}
        rows={rows}
        emptyTitle="No decision has been taken"
        emptyDescription="This case is still open on its evidence."
      >
        <DataTable
          caption="Decision history"
          columns={["Decided", "Kind", "Actor", "Reason", "Effective", "Owner outcome"]}
          rows={rows}
          render={(row) => [
            displayValue(row.decided_at),
            displayValue(row.decision_kind),
            displayValue(row.actor),
            displayValue(row.reason),
            displayValue(row.effective_from),
            /* A failed handoff is the state most easily misread as done. */
            <Badge
              key="outcome"
              tone={
                row.owner_outcome === "failed"
                  ? "error"
                  : row.owner_outcome === "succeeded"
                    ? "success"
                    : "neutral"
              }
            >
              {row.owner_outcome === "failed"
                ? "Owner command failed"
                : displayValue(row.owner_outcome)}
            </Badge>,
          ]}
        />
      </FacetState>
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// DQ Monitor
// ---------------------------------------------------------------------------

export function MonitorOverviewTab({ detail }: { detail: GovernanceObject }) {
  const summary = summaryOf(detail);
  const runtime = String(summary.runtime_state ?? "unavailable");
  return (
    <Panel>
      <PanelHeader
        title="Overview"
        description="Runtime state is a different fact from lifecycle status: a published monitor that has never run is unavailable, not healthy."
      />
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {[
          ["Runtime state", runtime],
          ["Check", displayValue(summary.check_profile)],
          ["Severity", displayValue(summary.severity)],
          ["Window", summary.window_days ? `${summary.window_days} day(s)` : "—"],
          ["Last outcome", displayValue(summary.last_outcome)],
          ["Last evaluated", displayValue(summary.last_evaluated_at)],
          ["Open issues", String(summary.open_issues ?? 0)],
          ["Pending version", summary.has_pending_version ? "Yes" : "No"],
        ].map(([term, value]) => (
          <div key={term}>
            <p className="m-0 text-caption text-text-secondary">{term}</p>
            <p className="mt-1 mb-0 text-ui text-text">{value}</p>
          </div>
        ))}
      </div>
      {runtime === "unavailable" ? (
        <Status as="block" tone="warning" className="mt-4">
          This monitor has produced no readable evaluation. That is not a pass and not a
          failure — nothing has been measured.
        </Status>
      ) : null}
    </Panel>
  );
}

export function MonitorCoverageTab({ detail }: { detail: GovernanceObject }) {
  const coverage = (summaryOf(detail).coverage ?? {}) as Record<string, unknown>;
  const total = Number(coverage.total_eligible ?? 0);
  return (
    <Panel>
      <PanelHeader
        title="Coverage"
        description="The denominator, which is what makes a verdict readable. A pass over nothing is not a pass — the database refuses to store one."
      />
      {total === 0 ? (
        <EmptyState
          title="Nothing was eligible"
          description="Zero eligible members is Not applicable. A percentage over an empty denominator would be a number with no meaning."
        />
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
          {[
            ["Eligible", coverage.total_eligible],
            ["Evaluated", coverage.evaluated],
            ["Passed", coverage.passed],
            ["Failed", coverage.failed],
            ["Unavailable", coverage.unavailable],
          ].map(([term, value]) => (
            <div key={String(term)}>
              <p className="m-0 text-caption text-text-secondary">{String(term)}</p>
              <p className="mt-1 mb-0 font-numeric text-lg font-semibold text-text">
                {value === null || value === undefined ? "—" : String(value)}
              </p>
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}

export function MonitorHistoryTab({ detail }: { detail: GovernanceObject }) {
  const rows = rowsOf(detail, "evaluations");
  return (
    <Panel flush>
      <PanelHeader
        title="History"
        description="Every evaluation is immutable and records the five outcomes separately, so an error and a failure never merge."
      />
      <FacetState
        detail={detail}
        rows={rows}
        emptyTitle="This monitor has never been evaluated"
        emptyDescription="No evaluation exists. This is not a run that found nothing."
      >
        <DataTable
          caption="Evaluation history"
          columns={["Evaluated", "Outcome", "Window", "Eligible", "Passed", "Failed"]}
          rows={rows}
          render={(row) => [
            displayValue(row.evaluated_at),
            <Badge
              key="outcome"
              tone={
                row.outcome === "pass"
                  ? "success"
                  : row.outcome === "fail"
                    ? "error"
                    : row.outcome === "not_applicable"
                      ? "neutral"
                      : "warning"
              }
            >
              {displayValue(row.outcome)}
            </Badge>,
            `${displayValue(row.window_start)} → ${displayValue(row.window_end)}`,
            String(row.total_eligible ?? "—"),
            String(row.passed_count ?? "—"),
            String(row.failed_count ?? "—"),
          ]}
        />
      </FacetState>
    </Panel>
  );
}

export function MonitorIssuesTab({ detail }: { detail: GovernanceObject }) {
  const rows = rowsOf(detail, "issues");
  return (
    <Panel flush>
      <PanelHeader
        title="Issues"
        description="Closed issues are listed too: a resolved issue that comes back is the thing worth seeing, and hiding it makes every recurrence look new."
      />
      <FacetState
        detail={detail}
        rows={rows}
        emptyTitle="No issue has been raised"
        emptyDescription="This monitor has never failed."
      >
        <DataTable
          caption="Issues"
          columns={["Status", "Severity", "First seen", "Last seen", "Events", "Suppressed until"]}
          rows={rows}
          render={(row) => [
            <Badge
              key="status"
              tone={
                row.status === "closed" || row.status === "resolved"
                  ? "neutral"
                  : row.status === "suppressed"
                    ? "warning"
                    : "error"
              }
            >
              {displayValue(row.status)}
            </Badge>,
            displayValue(row.severity),
            displayValue(row.first_seen_at),
            displayValue(row.last_seen_at),
            String(row.event_count ?? 0),
            displayValue(row.suppressed_until),
          ]}
        />
      </FacetState>
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// Rule Set
// ---------------------------------------------------------------------------

/**
 * The Rules tab, which serves six families and was written for one.
 *
 * Two of the six carry an ordered rule list: `metric_reconciliation`, whose
 * columns these are, and `tax_fee`, whose rules carry none of these keys and so
 * rendered as rows of `Unavailable`. The other four carry no rules AT ALL by
 * construction — `governance_rule_sets.py:478-482` refuses to store a ladder for
 * a family with no rule normalizer — and their governed content is the version
 * payload, which nothing read.
 *
 * So the branch is on the family, not on whether the array happens to be empty:
 * an empty array means different things in the two cases, and that is exactly
 * the distinction this file exists to keep (Story 41.7).
 */
export function RuleSetRulesTab({
  detail,
  projectId,
  onAdopted,
}: {
  detail: GovernanceObject;
  // Optional, because five of the six families have nothing to adopt and their
  // callers should not have to carry an argument they never use.
  projectId?: string;
  onAdopted?: () => void;
}) {
  return (
    <div className="grid gap-4">
      <RuleSetRulesRead detail={detail} projectId={projectId} onAdopted={onAdopted} />
      {/* THE SAME DOOR, UNDER EVERY FAMILY'S READ. What a version of this family
          decides is declared by its profile on the server, so this line is the
          whole of the console's knowledge about it — and a ninth family needs no
          ninth screen (`governance.md`, "A Rule Set version is drafted, then
          published", 2026-08-24). It sits UNDER the read for the reason the
          ladder amendment gives: a gesture belongs where its consequence is
          read. */}
      <RuleSetVersionAuthoring
        projectId={projectId}
        ruleSetId={detail.object_ref.id}
        onChanged={onAdopted}
      />
    </div>
  );
}

/** What the family's published version says, and only that. */
function RuleSetRulesRead({
  detail,
  projectId,
  onAdopted,
}: {
  detail: GovernanceObject;
  projectId?: string;
  onAdopted?: () => void;
}) {
  if (isTaxFeeRuleSet(detail))
    return <TaxFeeLadderRulesTab detail={detail} projectId={projectId} onAdopted={onAdopted} />;
  if (isPolicyOnlyRuleSet(detail))
    return <PolicyOnlyRulesTab detail={detail} projectId={projectId} />;
  const rows = rowsOf(detail, "ordered_rules");
  return (
    <Panel flush>
      <PanelHeader
        title="Rules"
        description="Order is content, not a display choice. It is stored in the immutable version, because resolving a tie by write order is what the profile contract forbids."
      />
      <FacetState
        detail={detail}
        rows={rows}
        emptyTitle="This rule set has no published rules"
        emptyDescription="Its current version carries none, or nothing is published yet."
      >
        <DataTable
          caption="Ordered rules"
          columns={["#", "Method", "Governs", "Sources", "Note"]}
          rows={rows}
          render={(row, ) => {
            const concept = (row.concept ?? {}) as Record<string, unknown>;
            const sources = Array.isArray(row.sources) ? (row.sources as Row[]) : [];
            return [
              String(rows.indexOf(row) + 1),
              <Badge key="method" tone="info">
                {displayValue(row.method)}
              </Badge>,
              <span key="concept" className="font-mono text-caption">
                {displayValue(concept.object_id)}
              </span>,
              /* Every source pins a version. A rule bound to "the latest" would
                 change meaning without a new version, which the profile refuses. */
              <span key="sources">
                {sources.length === 0
                  ? "—"
                  : sources
                      .map((source) => `${source.object_id}@${source.version_id}`)
                      .join(", ")}
              </span>,
              displayValue(row.note),
            ];
          }}
        />
      </FacetState>
    </Panel>
  );
}

export function RuleSetEffectiveDatesTab({ detail }: { detail: GovernanceObject }) {
  const rows = rowsOf(detail, "version_windows");
  return (
    <Panel flush>
      <PanelHeader
        title="Effective dates"
        description="When each version applied. An open-ended window is shown as open-ended rather than as today."
      />
      <FacetState
        detail={detail}
        rows={rows}
        emptyTitle="No version has been created"
        emptyDescription="This rule set has no versions yet."
      >
        <DataTable
          caption="Effective windows"
          columns={["Version", "Status", "From", "To", "Created"]}
          rows={rows}
          render={(row) => [
            `v${displayValue(row.version_number)}`,
            <Badge key="status" tone={row.status === "published" ? "success" : "neutral"}>
              {displayValue(row.status)}
            </Badge>,
            row.effective_from ? displayValue(row.effective_from) : "Always",
            row.effective_to ? displayValue(row.effective_to) : "Open-ended",
            displayValue(row.created_at),
          ]}
        />
      </FacetState>
    </Panel>
  );
}

export function RuleSetApprovalsExceptionsTab({ detail }: { detail: GovernanceObject }) {
  const approvals = rowsOf(detail, "approvals");
  const exceptions = rowsOf(detail, "exceptions");
  const state = summaryOf(detail).facets_state;
  if (state === "unavailable") {
    return (
      <Status as="block" tone="warning" title="This evidence could not be read">
        Its owner did not answer. This is not a count of zero.
      </Status>
    );
  }
  return (
    <div className="grid gap-4">
      <Panel flush>
        <PanelHeader
          title="Approvals"
          description="Who accepted a version. Different from an exception, which is which subject is exempt — different actors, different records."
        />
        {approvals.length === 0 ? (
          <EmptyState
            title="No approval is recorded"
            description="No version of this rule set has been formally accepted."
          />
        ) : (
          <DataTable
            caption="Approvals"
            columns={["Decided", "Decision", "By", "Version", "Reason"]}
            rows={approvals}
            render={(row) => [
              displayValue(row.decided_at),
              <Badge key="d" tone={row.decision === "approved" ? "success" : "error"}>
                {displayValue(row.decision)}
              </Badge>,
              displayValue(row.decided_by),
              displayValue(row.version_id),
              displayValue(row.reason),
            ]}
          />
        )}
      </Panel>
      <Panel flush>
        <PanelHeader
          title="Exceptions"
          description="Expired ones are listed and marked expired. An exception that quietly extends itself is a silent hole with a friendly name."
        />
        {exceptions.length === 0 ? (
          <EmptyState
            title="No exception is recorded"
            description="This rule set applies everywhere in its scope."
          />
        ) : (
          <DataTable
            caption="Exceptions"
            columns={["Subject", "Reason", "From", "Expires", "By", "In force"]}
            rows={exceptions}
            render={(row) => [
              `${displayValue(row.subject_kind)} ${displayValue(row.subject_id)}`,
              `${displayValue(row.reason_code)} — ${displayValue(row.reason)}`,
              displayValue(row.effective_from),
              row.expires_at ? displayValue(row.expires_at) : "Never",
              displayValue(row.decided_by),
              <Badge key="f" tone={row.in_force ? "warning" : "neutral"}>
                {row.in_force ? "In force" : "Expired"}
              </Badge>,
            ]}
          />
        )}
      </Panel>
    </div>
  );
}
