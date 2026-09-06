/**
 * The three Evidence workbenches (Story 49.5): Trace, Object Version and Audit
 * Event.
 *
 * Seven tabs, one rule: nothing here is composed in the browser. The graph, the
 * pinned predecessor, the approvals, the used-by list and the audit projection
 * all arrive from the server, already authorized, already redacted. A browser
 * that stitched them from four calls would look authoritative and be wrong the
 * moment one of them failed.
 *
 * Three distinctions the components keep visible rather than smoothing over:
 *
 *   - `Overview`, `Lineage` and `Provenance` are THREE projections of one
 *     trace, not one list shown three times. Overview says how far the evidence
 *     reaches; Lineage shows the owner steps in proven order; Provenance shows
 *     the integrity and correlation facts that make each step checkable.
 *   - "No pinned predecessor" is not "no change". A Diff without an exact
 *     authorized predecessor is UNAVAILABLE, and it never falls back to the
 *     current version, the previous one, or the latest.
 *   - A missing edge stops the chain. It is drawn as a gap with its reason, not
 *     closed by ordering the nodes on their timestamps.
 *
 * No new base class, no hardcoded colour, no literal spacing: every element
 * below is a primitive from `ui/index.ts`.
 */
import { EmptyState, formatTimestamp, label, Metric, Panel, PanelHeader, Stack, Status, Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll } from "../ui";
import { availabilityTone } from "./EvidenceCollection";
import { ownerPath } from "./SemanticModelTabs";
import type {
  AuditDetail,
  GovernanceObject,
  OwnerReference,
  TraceEdge,
  TraceGraph,
  TraceNode,
  VersionDetail,
} from "./governanceSurface";

function moment(value: string | null | undefined): string {
  if (!value) return "No recorded time";
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? "No recorded time" : formatTimestamp(parsed);
}

function summaryOf(detail: GovernanceObject): Record<string, unknown> {
  return (detail.summary ?? {}) as Record<string, unknown>;
}

/** An owner link, or the identifier alone.
 *
 *  When the reference has no registered route the identifier is still shown.
 *  Hiding it would erase the only proof the caller has that the reference
 *  exists; turning it into a guessed address would be worse. */
function OwnerLink({
  reference,
  fallback,
  organizationId,
  projectId,
}: {
  reference: OwnerReference | null | undefined;
  fallback: string;
  organizationId: string;
  projectId: string;
}) {
  const href = ownerPath(reference, organizationId, projectId);
  if (!href) return <span className="text-ui text-text-secondary">{fallback}</span>;
  return (
    <a
      className="font-semibold text-text underline-offset-2 hover:underline focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus"
      href={href}
    >
      {fallback}
    </a>
  );
}

function ReasonList({ reasons }: { reasons: TraceGraph["unavailable_reasons"] }) {
  if (!reasons || reasons.length === 0) return null;
  return (
    <Stack className="gap-2">
      {reasons.map((reason) => (
        <Status
          key={reason.code}
          as="block"
          tone="warning"
          title={label(reason.code)}
          data-testid={`trace-reason-${reason.code}`}
        >
          {reason.message}
        </Status>
      ))}
    </Stack>
  );
}

function emptyGraph(): TraceGraph {
  return {
    nodes: [],
    edges: [],
    owner_references: [],
    roots: [],
    terminals: [],
    coverage: "unavailable",
    unavailable_reasons: [
      {
        code: "trace_unavailable",
        message:
          "The server returned no graph for this record. Nothing has been drawn in its place.",
      },
    ],
  };
}

function graphOf(detail: GovernanceObject): TraceGraph {
  const raw = summaryOf(detail).trace;
  return raw && typeof raw === "object" ? (raw as TraceGraph) : emptyGraph();
}

// ---------------------------------------------------------------------------
// Evidence Trace — Overview
// ---------------------------------------------------------------------------

export function TraceOverviewTab({ detail }: { detail: GovernanceObject }) {
  const summary = summaryOf(detail);
  const graph = graphOf(detail);
  const times = graph.nodes.map((node) => node.occurred_at).filter(Boolean) as string[];
  const horizonStart = times.length > 0 ? times.reduce((a, b) => (a < b ? a : b)) : null;
  const horizonEnd = times.length > 0 ? times.reduce((a, b) => (a > b ? a : b)) : null;

  return (
    <Stack className="gap-6">
      <Panel className="grid gap-4 md:grid-cols-4">
        <Metric
          label="Scope"
          value={String(summary.owner_workspace ?? "unavailable")}
          hint="The workspace that owns the artifact this trace anchors. Governance references it; it never becomes its writer."
        />
        <Metric
          label="Proven steps"
          value={String(graph.nodes.length)}
          hint="Indexed records reachable from this anchor through a persisted edge. A shared correlation or an equal timestamp is not an edge."
        />
        <Metric
          label="Owner references"
          value={String(graph.owner_references.length)}
          hint="Exact owner artifacts this trace points at without indexing them."
        />
        <Metric
          label="Coverage"
          value={graph.coverage}
          hint="complete means every reachable step is available. partial means a step is bounded, unavailable, or that nothing links to this record."
        />
      </Panel>

      <Panel>
        <PanelHeader
          title="Evidence horizon"
          description="How far back this trace can prove anything. It is the range of what was indexed, not a claim about what happened outside it."
        />
        <p className="text-ui text-text-secondary">
          {horizonStart ? `${moment(horizonStart)} → ${moment(horizonEnd)}` : moment(summary.occurred_at as string)}
        </p>
      </Panel>

      <ReasonList reasons={graph.unavailable_reasons} />
    </Stack>
  );
}

// ---------------------------------------------------------------------------
// Evidence Trace — Lineage
// ---------------------------------------------------------------------------

/** The proven ORDER of a trace.
 *
 *  Ordered by the ordinal a producer proved, and only then by time. Sorting by
 *  timestamp alone would invent causality between two steps that merely
 *  happened in that order, which is the exact failure the contract names.
 */
function orderedSteps(graph: TraceGraph): Array<{ node: TraceNode; edge: TraceEdge | null }> {
  const byId = new Map(graph.nodes.map((node) => [node.record_id, node]));
  const seen = new Set<string>();
  const steps: Array<{ node: TraceNode; edge: TraceEdge | null }> = [];

  for (const root of graph.roots) {
    const node = byId.get(root);
    if (node && !seen.has(root)) {
      seen.add(root);
      steps.push({ node, edge: null });
    }
  }
  const ranked = [...graph.edges].sort((a, b) => {
    if (a.ordinal !== null && b.ordinal !== null) return a.ordinal - b.ordinal;
    if (a.ordinal !== null) return -1;
    if (b.ordinal !== null) return 1;
    return a.id.localeCompare(b.id);
  });
  for (const edge of ranked) {
    const node = byId.get(edge.to);
    if (node && !seen.has(edge.to)) {
      seen.add(edge.to);
      steps.push({ node, edge });
    }
  }
  for (const node of graph.nodes) {
    if (!seen.has(node.record_id)) {
      seen.add(node.record_id);
      steps.push({ node, edge: null });
    }
  }
  return steps;
}

export function TraceLineageTab({
  detail,
  organizationId,
  projectId,
}: {
  detail: GovernanceObject;
  organizationId: string;
  projectId: string;
}) {
  const graph = graphOf(detail);
  const steps = orderedSteps(graph);

  return (
    <Stack className="gap-6">
      <Panel flush>
        <PanelHeader
          title="Owner steps"
          description="Each step is an exact owner artifact. Branching stays branching: two steps at the same depth are not merged into a sequence."
        />
        {steps.length === 0 ? (
          <EmptyState
            title="No proven step"
            description="No indexed record links to this anchor. That is a gap in the chain, not a chain of length zero."
          />
        ) : (
          <TableScroll label="Trace lineage steps">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Step</TableHead>
                  <TableHead>Relation</TableHead>
                  <TableHead>Owner</TableHead>
                  <TableHead>Version</TableHead>
                  <TableHead>Occurred</TableHead>
                  <TableHead>Availability</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {steps.map(({ node, edge }) => (
                  <TableRow key={node.record_id} data-anchor={node.is_anchor ? "true" : undefined}>
                    <TableCell className="text-ui text-text-secondary">
                      {/* An ordinal the producer proved, or an explicit "unordered".
                          A row number would read as a sequence nobody established. */}
                      {edge?.ordinal !== null && edge?.ordinal !== undefined
                        ? `Step ${edge.ordinal + 1}`
                        : node.is_anchor
                          ? "Anchor"
                          : "Unordered"}
                    </TableCell>
                    <TableCell className="text-text-secondary">
                      {edge ? label(edge.relation) : "—"}
                    </TableCell>
                    <TableCell>
                      <OwnerLink
                        reference={node.owner_href}
                        fallback={`${node.owner_object_type} ${node.owner_object_id}`}
                        organizationId={organizationId}
                        projectId={projectId}
                      />
                    </TableCell>
                    <TableCell className="text-ui text-text-secondary">
                      {node.owner_version_id ?? "Unversioned"}
                    </TableCell>
                    <TableCell className="text-ui text-text-secondary">
                      {moment(node.occurred_at)}
                    </TableCell>
                    <TableCell>
                      <Status tone={availabilityTone(node.availability)}>
                        {node.availability.replaceAll("_", " ")}
                      </Status>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        )}
      </Panel>

      <Panel flush>
        <PanelHeader
          title="Referenced owners"
          description="Artifacts this trace points at without indexing them. They are links, not copies."
        />
        {graph.owner_references.length === 0 ? (
          <EmptyState
            title="No external owner reference"
            description="Every step of this trace is itself indexed."
          />
        ) : (
          <TableScroll label="Trace owner references">
            <Table>
              <TableBody>
                {graph.owner_references.map((reference) => (
                  <TableRow key={reference.id}>
                    <TableCell className="w-64 text-text-secondary">
                      {label(reference.relation)}
                    </TableCell>
                    <TableCell>
                      <OwnerLink
                        reference={reference.owner_href}
                        fallback={`${reference.owner_object_type ?? "unknown"} ${reference.owner_object_id ?? ""}`}
                        organizationId={organizationId}
                        projectId={projectId}
                      />
                    </TableCell>
                    <TableCell className="text-ui text-text-secondary">
                      {reference.owner_version_id ?? "Unversioned"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        )}
      </Panel>

      <ReasonList reasons={graph.unavailable_reasons} />
    </Stack>
  );
}

// ---------------------------------------------------------------------------
// Evidence Trace — Provenance
// ---------------------------------------------------------------------------

export function TraceProvenanceTab({ detail }: { detail: GovernanceObject }) {
  const graph = graphOf(detail);

  return (
    <Stack className="gap-6">
      <Panel flush>
        <PanelHeader
          title="Source and integrity"
          description="What makes each step checkable: who produced it, what it hashes to, and which identifiers correlate it elsewhere."
        />
        {graph.nodes.length === 0 ? (
          <EmptyState
            title="No provenance fact"
            description="No indexed step carries a producer or integrity reference for this record."
          />
        ) : (
          <TableScroll label="Trace provenance facts">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Producer</TableHead>
                  <TableHead>Owner object</TableHead>
                  <TableHead>Integrity</TableHead>
                  <TableHead>Correlations</TableHead>
                  <TableHead>Observed</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {graph.nodes.map((node) => (
                  <TableRow key={node.record_id}>
                    <TableCell className="font-semibold text-text">{node.producer}</TableCell>
                    <TableCell className="text-ui text-text-secondary">
                      {node.owner_object_type} · {node.owner_object_id}
                    </TableCell>
                    <TableCell className="font-mono text-caption text-text-secondary">
                      {node.integrity_hash ? node.integrity_hash.slice(0, 16) : "None recorded"}
                    </TableCell>
                    <TableCell className="text-ui text-text-secondary">
                      {node.correlations.length === 0
                        ? "None"
                        : node.correlations.map((item) => `${item.kind}:${item.id}`).join(" · ")}
                    </TableCell>
                    <TableCell className="text-ui text-text-secondary">
                      {moment(node.observed_at ?? node.occurred_at)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        )}
      </Panel>

      <ReasonList reasons={graph.unavailable_reasons} />
    </Stack>
  );
}

// ---------------------------------------------------------------------------
// Object Version — Diff, Approvals, Used by
// ---------------------------------------------------------------------------

function versionDetailOf(detail: GovernanceObject): VersionDetail | null {
  const raw = summaryOf(detail).version_detail;
  return raw && typeof raw === "object" ? (raw as VersionDetail) : null;
}

export function VersionOverviewTab({ detail }: { detail: GovernanceObject }) {
  const summary = summaryOf(detail);
  const availability = String(summary.availability ?? "available");
  return (
    <Stack className="gap-6">
      <Panel className="grid gap-4 md:grid-cols-4">
        <Metric label="Owner" value={String(summary.owner_workspace ?? "unavailable")} hint="The workspace that owns this version. Governance references it and copies none of its content." />
        <Metric label="Object" value={String(summary.owner_object_id ?? "unavailable")} hint="The exact owner object this version belongs to." />
        <Metric label="Version" value={String(summary.owner_version_id ?? "Unversioned")} hint="The owner's own immutable version identity — never a confirmation id standing in for one." />
        <Metric label="Recorded" value={moment(summary.occurred_at as string)} hint="When the owner recorded this version." />
      </Panel>
      <Panel>
        <PanelHeader title="Integrity" description="The owner's content hash, referenced here and computed nowhere else." />
        <p className="font-mono text-ui text-text-secondary">
          {summary.integrity_hash ? String(summary.integrity_hash) : "No integrity reference is recorded for this version."}
        </p>
        <p className="mt-4 text-ui text-text-secondary">
          Availability: <Status tone={availabilityTone(availability)}>{availability.replaceAll("_", " ")}</Status>
        </p>
      </Panel>
    </Stack>
  );
}

export function VersionDiffTab({
  detail,
  organizationId,
  projectId,
}: {
  detail: GovernanceObject;
  organizationId: string;
  projectId: string;
}) {
  const version = versionDetailOf(detail);
  const diff = version?.diff;

  if (!diff || diff.state !== "available") {
    return (
      <Panel>
        <PanelHeader
          title="Diff is unavailable"
          description={diff?.reason_code ? label(diff.reason_code) : "No comparison could be stated."}
        />
        <p className="text-ui text-text-secondary">
          {diff?.message ??
            "No predecessor is pinned to this version by its owner, so there is nothing exact to compare it with."}
        </p>
        <p className="mt-4 text-ui text-text-secondary">
          A plausible earlier version has NOT been searched for, and neither `current` nor `latest`
          has been substituted. An unavailable diff is a fact about the evidence; a guessed one
          would be a claim about the data.
        </p>
      </Panel>
    );
  }

  return (
    <Stack className="gap-6">
      <Status
        as="block"
        tone={diff.changed ? "warning" : "success"}
        title={diff.changed ? "This version differs from its pinned predecessor" : "This version is identical to its pinned predecessor"}
        data-testid="version-diff-verdict"
      >
        Compared by integrity reference between the exact selected version and the exact
        predecessor its owner pinned. The field-level difference lives with the owner; Governance
        does not hold a copy of either version's content.
      </Status>

      <Panel flush>
        <PanelHeader title="The two exact versions" description="Both are pinned. Neither is resolved by label." />
        <TableScroll label="Version diff">
          <Table>
            <TableBody>
              <TableRow>
                <TableCell className="w-64 font-semibold text-text">Selected version</TableCell>
                <TableCell className="text-ui text-text-secondary">{diff.selected_version_id ?? "Unversioned"}</TableCell>
                <TableCell className="font-mono text-caption text-text-secondary">{diff.selected_integrity_hash ?? "None"}</TableCell>
              </TableRow>
              <TableRow>
                <TableCell className="w-64 font-semibold text-text">Pinned predecessor</TableCell>
                <TableCell className="text-ui text-text-secondary">{diff.predecessor_version_id ?? "Unversioned"}</TableCell>
                <TableCell className="font-mono text-caption text-text-secondary">{diff.predecessor_integrity_hash ?? "None"}</TableCell>
              </TableRow>
            </TableBody>
          </Table>
        </TableScroll>
      </Panel>

      <Panel>
        <PanelHeader title="Open the owner" description="Where the version content itself lives." />
        <OwnerLink
          reference={diff.owner_href}
          fallback="Open this version at its owner"
          organizationId={organizationId}
          projectId={projectId}
        />
      </Panel>
    </Stack>
  );
}

export function VersionApprovalsTab({ detail }: { detail: GovernanceObject }) {
  const version = versionDetailOf(detail);
  const approvals = version?.approvals;

  if (!approvals || approvals.state === "unavailable") {
    return (
      <EmptyState
        title="No approval owner answers"
        description="No adapter can reference the approvals of this version yet. This is not a count of zero."
      />
    );
  }
  if (approvals.items.length === 0) {
    return (
      <EmptyState
        title="No approval recorded"
        description="The owner answered, and this version carries no approval. An unapproved version is a real state, not a missing one."
      />
    );
  }
  return (
    <Panel flush>
      <PanelHeader title="Approvals" description="Exact approval records, referenced by identifier. Their content stays with their owner." />
      <TableScroll label="Version approvals">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Approval</TableHead>
              <TableHead>Owner</TableHead>
              <TableHead>Type</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {approvals.items.map((item, index) => (
              <TableRow key={String(item.approval_id ?? index)}>
                <TableCell className="font-mono text-ui text-text">{String(item.approval_id ?? "—")}</TableCell>
                <TableCell className="text-text-secondary">{String(item.owner_workspace ?? "—")}</TableCell>
                <TableCell className="text-text-secondary">{String(item.owner_object_type ?? "—")}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableScroll>
    </Panel>
  );
}

export function VersionUsedByTab({ detail }: { detail: GovernanceObject }) {
  const version = versionDetailOf(detail);
  const usedBy = version?.used_by;

  if (!usedBy || usedBy.state === "unavailable") {
    return (
      <EmptyState
        title="No consumer owner answers"
        description="No adapter can list what depends on this version yet. This is not a count of zero."
      />
    );
  }
  if (usedBy.items.length === 0) {
    return (
      <EmptyState
        title="Nothing indexed depends on this version"
        description="Only authorized, explicitly linked consumers are listed. A consumer this caller cannot see is not counted here either."
      />
    );
  }
  return (
    <Panel flush>
      <PanelHeader title="Used by" description="Indexed records that explicitly link to this version." />
      <TableScroll label="Version consumers">
        <Table>
          <TableBody>
            {usedBy.items.map((item, index) => (
              <TableRow key={String(item.record_id ?? index)}>
                <TableCell className="w-64 text-text-secondary">{label(String(item.relation ?? "references"))}</TableCell>
                <TableCell className="font-mono text-ui text-text">{String(item.record_id ?? "—")}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableScroll>
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// Audit Event — Overview
// ---------------------------------------------------------------------------

export function AuditOverviewTab({ detail }: { detail: GovernanceObject }) {
  const raw = summaryOf(detail).audit_detail;
  const audit = (raw && typeof raw === "object" ? raw : null) as AuditDetail | null;

  if (!audit || audit.state !== "available") {
    return (
      <Panel>
        <PanelHeader
          title="The audited record is unavailable"
          description={audit?.reason_code ? label(audit.reason_code) : "No audit owner answered."}
        />
        <p className="text-ui text-text-secondary">
          {audit?.message ??
            "The audited record is no longer readable. Its reference is intact and nothing has been reconstructed in its place."}
        </p>
      </Panel>
    );
  }

  return (
    <Stack className="gap-6">
      <Panel className="grid gap-4 md:grid-cols-4">
        <Metric label="Actor" value={audit.actor ?? "Not disclosed"} hint="Shown exactly as policy permits. An actor the policy hides is not replaced by a placeholder identity." />
        <Metric label="Action" value={audit.action ?? "—"} hint="The audited command." />
        <Metric label="Outcome" value={audit.outcome ?? "—"} hint="What the command did, as the audit spine recorded it." />
        <Metric label="Occurred" value={moment(audit.occurred_at)} hint="When the audited command ran." />
      </Panel>

      <Panel flush>
        <PanelHeader
          title="Authorized resource"
          description="The resource graph the Project scope was resolved from. It is what makes this event belong to this Project — not a client parameter."
        />
        {(audit.resource_path ?? []).length === 0 ? (
          <EmptyState title="No resource path" description="This event carries no normalized resource reference." />
        ) : (
          <TableScroll label="Audit resource path">
            <Table>
              <TableBody>
                {(audit.resource_path ?? []).map((entry, index) => (
                  <TableRow key={`${entry}-${index}`}>
                    <TableCell className="w-24 text-text-secondary">{index + 1}</TableCell>
                    <TableCell className="font-mono text-ui text-text">{entry}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        )}
      </Panel>

      <Panel flush>
        <PanelHeader
          title="Correlation and versions"
          description="Opaque identifiers and the policy, catalog and tool versions in force. Provider accounts, Authorization references, secrets and raw metadata are never returned here."
        />
        <TableScroll label="Audit correlation">
          <Table>
            <TableBody>
              <TableRow>
                <TableCell className="w-64 font-semibold text-text">W3C trace id</TableCell>
                <TableCell className="font-mono text-ui text-text-secondary">
                  {audit.correlation?.trace_id ?? "None recorded in W3C format"}
                </TableCell>
              </TableRow>
              <TableRow>
                <TableCell className="w-64 font-semibold text-text">Operation</TableCell>
                <TableCell className="font-mono text-ui text-text-secondary">
                  {audit.correlation?.operation_id ?? "None"}
                </TableCell>
              </TableRow>
              {Object.entries(audit.versions ?? {}).map(([key, value]) => (
                <TableRow key={key}>
                  <TableCell className="w-64 font-semibold text-text">{label(key)}</TableCell>
                  <TableCell className="text-ui text-text-secondary">{value ?? "None"}</TableCell>
                </TableRow>
              ))}
              {Object.entries(audit.hashes ?? {}).map(([key, value]) => (
                <TableRow key={key}>
                  <TableCell className="w-64 font-semibold text-text">{label(key)}</TableCell>
                  <TableCell className="font-mono text-caption text-text-secondary">{value ?? "None"}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableScroll>
      </Panel>
    </Stack>
  );
}

/** The one place the workbench chassis asks "is this an Evidence tab?".
 *
 *  Returns `null` when it is not, so the chassis falls through to its own
 *  branches rather than this file growing a copy of them. */
export function EvidenceTabs({
  objectType,
  tab,
  detail,
  organizationId,
  projectId,
}: {
  objectType: string;
  tab: string;
  detail: GovernanceObject;
  organizationId: string;
  projectId: string;
}): React.ReactElement | null {
  if (objectType === "evidence-trace") {
    if (tab === "overview") return <TraceOverviewTab detail={detail} />;
    if (tab === "lineage")
      return <TraceLineageTab detail={detail} organizationId={organizationId} projectId={projectId} />;
    if (tab === "provenance") return <TraceProvenanceTab detail={detail} />;
  }
  if (objectType === "object-version") {
    if (tab === "overview") return <VersionOverviewTab detail={detail} />;
    if (tab === "diff")
      return <VersionDiffTab detail={detail} organizationId={organizationId} projectId={projectId} />;
    if (tab === "approvals") return <VersionApprovalsTab detail={detail} />;
    if (tab === "used-by") return <VersionUsedByTab detail={detail} />;
  }
  if (objectType === "audit-event" && tab === "overview") {
    return <AuditOverviewTab detail={detail} />;
  }
  return null;
}
