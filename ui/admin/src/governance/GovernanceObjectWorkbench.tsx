/**
 * The one Level-3 chassis behind all eleven governed object types.
 *
 * It answers four questions that are NOT the same question, and keeps them
 * apart on purpose:
 *
 *   Used by    who depends on this object right now
 *   Versions   what this object has been, in order
 *   Evidence   what was recorded about it, by its owner, immutably
 *   Selected   the exact version this address pins — current or not
 *
 * Collapsing any two of them produces the "generic event list" the architecture
 * document names as a failure. A tab whose domain content belongs to a later
 * story says so, by name, and links to the owner. It never shows a zero.
 */
import { useMemo, useState } from "react";
import { wireWord,
  ObjectId,
  Badge,
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  EmptyState,
  NavTabs,
  ObjectHeader,
  Panel,
  PanelHeader,
  Stack,
  Status,
  Table,
  TableBody,
  TableCell,
  TableRow,
  TableScroll,
  displayValue,
  fieldMeaning,
  label,
  notify,
  type NavTab,
  formatTimestamp,
  stateLabel,
  stateTone,
} from "../ui";
import { ApiError, apiPost } from "../lib/apiFetch";
import NewConceptDialog, { type SemanticEditTarget } from "./NewConceptDialog";
import NewSemanticViewDialog from "./NewSemanticViewDialog";
import {
  gateBlocksPublication,
  OVERRIDE_MINIMUM_REASON,
  TestGateOverridePanel,
  type TestGate,
} from "./TestGateOverride";
import RouteState from "../shell/RouteState";
import { buildPath, type CanonicalRoute, useRoute } from "../shell/router";
import { findObjectContract, findSection } from "../shell/navigation";
import { objectLabel, pendingOwner, scopeLabel, TAB_LABEL } from "./contracts";
import { EvidenceTabs } from "./EvidenceTabs";
import CountryWorkspace from "./CountryWorkspace";
import {
  ClientObjectSourcesPanel,
  MasterDataHierarchyTab,
  MasterDataMappingsAliasesTab,
} from "./MasterDataTabs";
import { CoverageTab, RepresentationsTab } from "./TrackedEntityTabs";
import {
  CaseCandidateChangeTab,
  CaseDecisionHistoryTab,
  CaseImpactTab,
  MonitorCoverageTab,
  MonitorHistoryTab,
  MonitorIssuesTab,
  MonitorOverviewTab,
  RuleSetApprovalsExceptionsTab,
  RuleSetEffectiveDatesTab,
  RuleSetRulesTab,
} from "./ControlsQualityTabs";
import { isTaxFeeRuleSet, TaxFeeLadderOverviewTab } from "./TaxFeeLadderTabs";
import { DimensionLineageTab } from "./DimensionLineageTab";
import {
  BusinessDomainLinks,
  ExploreDataAction,
  MetricsDimensionsTab,
  ownerPath,
  SemanticsTab,
  SourceBindingsTab,
  type ExploreHandoff,
  type SourceBindings,
} from "./SemanticModelTabs";
import {
  useGovernanceObject,
  type EvidenceRef,
  type GovernanceObject,
  type OwnerReference,
  type VersionRef,
} from "./governanceSurface";
import { FieldTable } from "./FieldTable";
import { MasterDataIdentityOverview } from "./MasterDataIdentityOverview";

/** One consumer, as the server composed it (Story 49.2 AC7). */
type UsedByRef = {
  id: string;
  kind: string;
  label: string;
  /** The owner workspace, or null when the server could not name one. */
  workspace: string | null;
  workspaceLabel: string | null;
  pinnedVersionId: string | null;
  recordedAt: string | null;
  ownerHref: OwnerReference | null;
  relation: string | null;
};

/**
 * The workspaces a consumer can live in, in reading order.
 *
 * The three at the top are always drawn, because "no Skill uses this" is an
 * answer a person came for. The rest appear only when they hold something.
 * Nothing is re-filed: a reference whose workspace the server could not name
 * goes to *Other*, never into one of these.
 */
const USED_BY_WORKSPACES: ReadonlyArray<{ key: string; title: string; empty: string; always: boolean }> = [
  { key: "context-hub", title: "Context Hub", empty: "No Skill or Knowledge item uses this object.", always: true },
  { key: "analyze", title: "Analyze", empty: "No Report or Render uses this object.", always: true },
  { key: "data", title: "Data", empty: "No physical Datastream uses this object.", always: true },
  { key: "governance", title: "Governance", empty: "No governed definition uses this object.", always: false },
  { key: "test", title: "Test", empty: "No evaluation uses this object.", always: false },
  { key: "overview", title: "Project settings", empty: "No Project setting uses this object.", always: false },
];

function readUsedByRef(raw: unknown, index: number): UsedByRef {
  const record = (typeof raw === "object" && raw !== null ? raw : {}) as Record<string, unknown>;
  const id = String(record.id ?? `Reference ${index + 1}`);
  const workspace = typeof record.workspace === "string" && record.workspace ? record.workspace : null;
  return {
    id,
    kind: typeof record.kind === "string" && record.kind ? record.kind : "consumer",
    label: typeof raw === "string" ? raw : String(record.label ?? id),
    workspace,
    workspaceLabel: typeof record.workspace_label === "string" ? record.workspace_label : null,
    pinnedVersionId: typeof record.pinned_version_id === "string" ? record.pinned_version_id : null,
    recordedAt: typeof record.recorded_at === "string" ? record.recorded_at : null,
    ownerHref: (record.owner_href ?? null) as OwnerReference | null,
    relation: typeof record.relation === "string" ? record.relation : null,
  };
}

function UsedByEntry({
  item,
  tone,
  organizationId,
  projectId,
}: {
  item: UsedByRef;
  tone: "accent" | "info" | "neutral";
  organizationId: string;
  projectId: string;
}) {
  const href = ownerPath(item.ownerHref, organizationId, projectId);
  return (
    <li className="flex items-start justify-between gap-3 rounded-md border border-divider-base p-2">
      <span className="min-w-0">
        {href ? (
          <a
            className="font-medium text-text underline-offset-2 hover:underline focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus"
            href={href}
          >
            {item.label}
          </a>
        ) : (
          <span className="font-medium text-text">{item.label}</span>
        )}
        <span className="block text-caption text-text-secondary">
          {item.relation ? `${item.relation} · ` : ""}
          {item.id}
          {item.pinnedVersionId ? ` · pinned ${item.pinnedVersionId}` : ""}
          {item.recordedAt ? ` · recorded ${formatTimestamp(item.recordedAt)}` : ""}
        </span>
      </span>
      <Badge tone={tone}>{item.kind.replaceAll("_", " ")}</Badge>
    </li>
  );
}

function UsedByPanel({
  usedBy,
  typeLabel,
  organizationId,
  projectId,
}: {
  usedBy: GovernanceObject["used_by"];
  typeLabel: string;
  organizationId: string;
  projectId: string;
}) {
  // COUNT AND LIST ARE TWO ANSWERS, AND ONLY ONE OF THEM MAKES AN ARCHIVE SAFE
  // (49-2 verdict, 2026-09-01). This branch used to read
  // `count === 0 || refs.length === 0` and print "Nothing depends on this
  // object" for a Business Domain with three live links. A count the server
  // could not turn into a list is now what it is: unknown consumers, said by
  // name, with the gesture that resolves them.
  if (usedBy.state === "unavailable") {
    const reason = (usedBy as { reason?: { message?: string } }).reason;
    return (
      <Panel>
        <PanelHeader title="Used by" description="What depends on this object today." />
        <EmptyState
          title={usedBy.count > 0 ? `${usedBy.count} consumer(s) recorded, not listed here` : "No owner answers Used by"}
          description={
            reason?.message ??
            (usedBy.count > 0
              ? `This ${typeLabel} is depended on ${usedBy.count} time(s) and its owner cannot name them on this screen. Reload; if the list stays absent, release the dependency from its consumer before retiring this object.`
              : `No adapter can list the consumers of this ${typeLabel} yet. This is not a count of zero.`)
          }
        />
      </Panel>
    );
  }

  if (usedBy.count === 0) {
    return (
      <Panel>
        <PanelHeader title="Used by" description="What depends on this object today." />
        <EmptyState title="Nothing depends on this object" description="Its owner answered, and the answer is none." />
      </Panel>
    );
  }

  // THE CLIENT HALF OF THE SAME INVARIANT. `_counted_facet` cannot compose a
  // positive count with an empty list any more, and this branch is what keeps
  // the screen honest if a future composer does: three consumers with nothing to
  // show is "not listed here", never "none". The branch above must stay the one
  // that answers for a genuine zero — merging the two is exactly the defect.
  if (usedBy.refs.length === 0) {
    return (
      <Panel>
        <PanelHeader title="Used by" description="What depends on this object today." />
        <EmptyState
          title={`${usedBy.count} consumer(s) recorded, not listed here`}
          description={`This ${typeLabel} is depended on ${usedBy.count} time(s) and its owner returned no list. Reload; if the list stays absent, release the dependency from its consumer before retiring this object.`}
        />
      </Panel>
    );
  }

  const refs = usedBy.refs.map(readUsedByRef);
  const grouped = new Map<string, UsedByRef[]>();
  const otherRefs: UsedByRef[] = [];
  const known = new Set(USED_BY_WORKSPACES.map((group) => group.key));
  refs.forEach((item) => {
    // NO DEFAULT RE-FILING. A reference whose workspace the server did not name
    // is shown as unplaced. Pouring the unplaced ones into Context Hub — which
    // is what this did — reported the wrong owner for every one of them.
    if (item.workspace && known.has(item.workspace)) {
      const bucket = grouped.get(item.workspace) ?? [];
      bucket.push(item);
      grouped.set(item.workspace, bucket);
    } else {
      otherRefs.push(item);
    }
  });

  const groups = USED_BY_WORKSPACES.filter(
    (group) => group.always || (grouped.get(group.key)?.length ?? 0) > 0,
  );
  const tones: Record<string, "accent" | "info" | "neutral"> = {
    "context-hub": "accent",
    analyze: "info",
    data: "neutral",
    governance: "info",
    test: "accent",
    overview: "neutral",
  };

  return (
    <Stack className="gap-6">
      <Panel>
        <PanelHeader
          title="Used by"
          description={`${usedBy.count} consumer(s), grouped by the workspace that owns each one. Never merged into one count.`}
        />
      </Panel>

      <div className="grid gap-6 md:grid-cols-3">
        {groups.map((group) => {
          const items = grouped.get(group.key) ?? [];
          return (
            // Named region per workspace: a group of consumers is a thing a
            // person reads as a unit, and a screen reader has to be able to say
            // which group it is in before the list means anything.
            <Panel key={group.key} role="group" aria-label={group.title}>
              <PanelHeader title={group.title} description={`${items.length} consumer(s)`} />
              {items.length === 0 ? (
                <p className="text-ui text-text-secondary">{group.empty}</p>
              ) : (
                <ul className="space-y-2 text-ui">
                  {items.map((item) => (
                    <UsedByEntry
                      key={`${item.kind}:${item.id}`}
                      item={item}
                      tone={tones[group.key] ?? "neutral"}
                      organizationId={organizationId}
                      projectId={projectId}
                    />
                  ))}
                </ul>
              )}
            </Panel>
          );
        })}
      </div>

      {otherRefs.length > 0 && (
        <Panel role="group" aria-label="Other">
          <PanelHeader
            title="Other"
            description={`${otherRefs.length} consumer(s) whose owning workspace is not recorded. They are listed here rather than filed under a workspace nobody proved.`}
          />
          <ul className="space-y-2 text-ui">
            {otherRefs.map((item) => (
              <UsedByEntry
                key={`${item.kind}:${item.id}`}
                item={item}
                tone="neutral"
                organizationId={organizationId}
                projectId={projectId}
              />
            ))}
          </ul>
        </Panel>
      )}
    </Stack>
  );
}

/**
 * The gestures the Semantic Model change set has always accepted, and which no
 * screen sent until 2026-08-18.
 *
 * `_SUPPORTED_INTENTS` (`server/core/semantic_model.py:465`) is the whole
 * vocabulary: `create_concept`, `edit_concept`, `create_view`, `edit_view`,
 * `archive_object`. The console sent the two creations and nothing else, so a
 * Concept published with the wrong additivity class was permanent and a View
 * nobody used could not be retired.
 *
 * WHICH ONES APPEAR IS THE SERVER'S ANSWER, not this file's. An action the
 * owner did not declare in `allowed_actions` renders nothing here — the same
 * rule `Explore data` already follows, and the only one that keeps a button
 * from promising a command the object's owner would refuse.
 */
const EDIT_ACTION_FOR_TYPE: Record<string, string> = {
  "semantic-concept": "edit_concept",
  "semantic-view": "edit_view",
};

const CHANGE_SET_ROOT = (projectId: string) =>
  `/api/projects/${encodeURIComponent(projectId)}/governance/semantic-model/change-sets`;

interface ServerRefusal {
  code?: string;
  message?: string;
  path?: string;
}

/** One group of `validation.used_by_impact`, measured server-side at prepare. */
interface ImpactGroup {
  owner?: string;
  label?: string;
  state?: string;
  count?: number | null;
  reason?: string;
  refs?: Array<Record<string, unknown>>;
}

function RefusalList({ refusals, title }: { refusals: ServerRefusal[]; title: string }) {
  if (refusals.length === 0) return null;
  return (
    <Status as="block" tone="error" title={title} data-testid="semantic-action-refusals">
      <ul className="m-0 list-none space-y-1 p-0">
        {refusals.map((refusal, index) => (
          <li key={`${refusal.code ?? "refusal"}-${index}`} className="text-caption">
            <strong>{refusal.code ?? "refused"}</strong>
            {refusal.path ? ` at ${refusal.path}` : ""}
            {refusal.message ? ` — ${refusal.message}` : ""}
          </li>
        ))}
      </ul>
    </Status>
  );
}

/**
 * Retiring a governed object, and what would be left pointing at it.
 *
 * The count comes from the object's own `used_by` facet before the act, and
 * from the server's `validation.used_by_impact` once prepare has measured it —
 * two different moments, and the second is the authority. An `unavailable`
 * group says so by name: "nobody could answer" and "nothing depends on it" are
 * different facts, and only one of them makes retiring safe.
 */
function ArchiveObjectDialog({
  projectId,
  objectType,
  typeLabel,
  detail,
  target,
  onDone,
  onClose,
}: {
  projectId: string;
  objectType: string;
  typeLabel: string;
  detail: GovernanceObject;
  target: SemanticEditTarget;
  onDone: () => void;
  onClose: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refusals, setRefusals] = useState<ServerRefusal[]>([]);
  const [impact, setImpact] = useState<ImpactGroup[] | null>(null);
  /** The token `prepare` minted for THIS measurement. Retiring is never one
   *  click: the first measures and names what would be left pointing at the
   *  object, the second is the person saying yes to that measurement. */
  const [confirmationToken, setConfirmationToken] = useState<string | null>(null);
  // The SAME change set is re-prepared on a retry: creating a second one would
  // ask the server to measure a different candidate.
  const [pendingChangeSetId, setPendingChangeSetId] = useState<string | null>(null);
  const [blockedGate, setBlockedGate] = useState<TestGate | null>(null);
  const [overrideReason, setOverrideReason] = useState("");

  const usedBy = detail.used_by;
  // `unavailable` no longer means "nobody could answer": since the 49-2 repair a
  // facet that counted its consumers and could not list them is unavailable WITH
  // its count. Throwing that count away here would tell a person about to
  // archive that nothing is known, when the number is.
  const consumerCount = usedBy.state === "unavailable" && usedBy.count === 0 ? null : usedBy.count;

  /** Create if needed, then measure. The Test gate applies to a retirement
   *  exactly as it applies to a publication — the module has one gate — so a
   *  written reason travels on the RE-prepare of the same change set. */
  async function measure() {
    setBusy(true);
    setError(null);
    setRefusals([]);
    try {
      const changeSet = pendingChangeSetId
        ? { change_set_id: pendingChangeSetId }
        : await apiPost<{ change_set_id: string }>(CHANGE_SET_ROOT(projectId), {
            object_type: objectType,
            object_id: target.objectId,
            base_version_id: target.baseVersionId,
            // NO BODY, and that is the contract: `archive_object` names its
            // object through `object_id`, and `create_change_set` skips the
            // `intent.concept` / `intent.view` requirement for it alone.
            intent: { action: "archive_object" },
            idempotency_key: `scs-archive-${target.objectId}-${Date.now()}`,
          });
      setPendingChangeSetId(changeSet.change_set_id);

      const prepared = await apiPost<{
        confirmation_token?: string;
        refusals?: ServerRefusal[];
        validation?: {
          publishable?: boolean;
          refusals?: ServerRefusal[];
          used_by_impact?: { state?: string; groups?: ImpactGroup[] };
          test_gate?: TestGate;
        };
      }>(
        `${CHANGE_SET_ROOT(projectId)}/${changeSet.change_set_id}/prepare`,
        overrideReason.trim().length >= OVERRIDE_MINIMUM_REASON
          ? { test_gate_override: { reason: overrideReason.trim() } }
          : {},
      );

      setImpact(prepared?.validation?.used_by_impact?.groups ?? null);
      const named = prepared?.refusals ?? prepared?.validation?.refusals ?? [];
      if (prepared?.validation?.publishable !== true) {
        setConfirmationToken(null);
        const gate = prepared?.validation?.test_gate;
        if (gate && gateBlocksPublication(gate, named)) {
          setBlockedGate(gate);
          setError(null);
          return;
        }
        setRefusals(named);
        if (named.length === 0) {
          setError(
            gate?.message
              ? `This ${typeLabel} was not retired. ${gate.message}`
              : `This ${typeLabel} was not retired: preparation did not clear it. Nothing changed.`,
          );
        }
        return;
      }
      setBlockedGate(null);
      if (!prepared.confirmation_token) {
        setError(
          `This ${typeLabel} was not retired: preparation returned no confirmation. Nothing changed.`,
        );
        return;
      }
      setConfirmationToken(prepared.confirmation_token);
    } catch (err: unknown) {
      report(err);
    } finally {
      setBusy(false);
    }
  }

  async function confirm() {
    if (!confirmationToken || !pendingChangeSetId) return;
    setBusy(true);
    setError(null);
    try {
      await apiPost(`${CHANGE_SET_ROOT(projectId)}/${pendingChangeSetId}/confirm`, {
        confirmation_token: confirmationToken,
      });
      notify(`${typeLabel} retired: ${detail.object_ref.label} is archived. Its versions stay readable.`);
      onDone();
      onClose();
    } catch (err: unknown) {
      // A consumed or expired confirmation is not retryable: the measurement it
      // belonged to is gone, and the next attempt measures again.
      setConfirmationToken(null);
      report(err);
    } finally {
      setBusy(false);
    }
  }

  function report(err: unknown) {
    if (err instanceof ApiError) {
      const body = err.body as { refusals?: ServerRefusal[] } | undefined;
      if (Array.isArray(body?.refusals)) setRefusals(body.refusals);
      setError(err.message || `API Error (${err.status})`);
    } else {
      setError(err instanceof Error ? err.message : "The command could not be sent.");
    }
  }

  return (
    <Dialog open onOpenChange={(value) => !value && onClose()}>
      <DialogContent className="sm:max-w-[560px]">
        <DialogHeader>
          <DialogTitle>Retire {detail.object_ref.label}?</DialogTitle>
          <DialogDescription>
            Nothing is deleted. Every published version of this {typeLabel} stays readable, and
            what already pins one keeps working — it stops being offered for new work.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-4 py-4">
          <Status
            as="block"
            tone={consumerCount === null ? "warning" : consumerCount > 0 ? "warning" : "info"}
            title="What points at it today"
            data-testid="archive-consumer-evidence"
          >
            {consumerCount === null
              ? `No owner could answer what depends on this ${typeLabel}. That is not a count of zero, and retiring it now retires something nobody could measure.`
              : consumerCount === 0
                ? "Its owner answered, and the answer is none."
                : `${consumerCount} consumer(s) still point at this ${typeLabel}.`}
            {usedBy.refs.length > 0 && (
              <ul className="mt-2 list-none space-y-1 p-0 text-caption">
                {usedBy.refs.slice(0, 10).map((ref, index) => (
                  <li key={index}>
                    {displayValue(
                      (ref as Record<string, unknown>).label ?? (ref as Record<string, unknown>).id,
                    )}
                  </li>
                ))}
              </ul>
            )}
          </Status>

          <RefusalList refusals={refusals} title={`This ${typeLabel} was not retired`} />

          {error && (
            <Status as="block" tone="error" title="Nothing was changed">
              {error}
            </Status>
          )}

          {blockedGate && (
            <TestGateOverridePanel
              gate={blockedGate}
              reason={overrideReason}
              onReasonChange={setOverrideReason}
              objectNoun={typeLabel}
            />
          )}

          {impact !== null && (
            <Status as="block" tone="info" title="Measured by the server at preparation" data-testid="archive-impact">
              <ul className="m-0 list-none space-y-1 p-0 text-caption">
                {impact.map((group, index) => (
                  <li key={`${group.owner ?? index}`}>
                    <strong>{group.label ?? group.owner ?? "Consumer group"}</strong>
                    {" — "}
                    {group.state === "unavailable"
                      ? (group.reason ?? "This owner could not answer. It is not a count of zero.")
                      : `${group.count ?? 0} reference(s)`}
                  </li>
                ))}
              </ul>
            </Status>
          )}

          <p className="mb-0 text-caption text-text-secondary">
            Retiring from version {target.baseVersionId}, the exact version this screen is showing.
          </p>
        </div>

        <DialogFooter>
          <Button type="button" variant="secondary" disabled={busy} onClick={onClose}>
            Keep it
          </Button>
          {confirmationToken ? (
            <Button type="button" variant="destructive" disabled={busy} onClick={() => void confirm()}>
              {busy ? "Retiring..." : `Retire this ${typeLabel}`}
            </Button>
          ) : (
            <Button
              type="button"
              variant="destructive"
              disabled={
                busy ||
                (blockedGate !== null && overrideReason.trim().length < OVERRIDE_MINIMUM_REASON)
              }
              onClick={() => void measure()}
            >
              {busy
                ? "Measuring..."
                : blockedGate
                  ? "Measure again with this reason"
                  : "Measure what depends on it"}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** The header region where a governed Semantic Model object is corrected or
 *  retired. It renders exactly what the owner declared, and nothing when it
 *  declared nothing. */
function SemanticModelActions({
  projectId,
  objectType,
  typeLabel,
  detail,
  onChanged,
}: {
  projectId: string;
  objectType: string;
  typeLabel: string;
  detail: GovernanceObject;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState<"edit" | "archive" | null>(null);
  const editAction = EDIT_ACTION_FOR_TYPE[objectType];
  const canEdit = Boolean(editAction) && detail.allowed_actions.includes(editAction);
  const canArchive = detail.allowed_actions.includes("archive_object");

  // An exact base version is the server's requirement for BOTH gestures
  // (`missing_exact_base`), so an object with no published version has nothing
  // to edit from and nothing to retire.
  const target = useMemo<SemanticEditTarget | null>(() => {
    const baseVersionId = detail.active_version_ref?.id;
    if (!baseVersionId) return null;
    return {
      objectId: detail.object_ref.id,
      baseVersionId,
      label: detail.object_ref.label,
      summary: detail.summary,
    };
  }, [detail]);

  if (!canEdit && !canArchive) return null;

  return (
    <Panel data-testid="semantic-model-actions">
      <PanelHeader
        title={`Change this ${typeLabel}`}
        description="An edit publishes a new immutable version from the exact version below; retiring stops it being offered without deleting anything."
      />
      {target === null ? (
        <Status as="block" tone="info" title="No published version to change">
          This {typeLabel} has no published version, and both gestures begin from an exact one.
          Publish it first.
        </Status>
      ) : (
        <div className="flex flex-wrap gap-2">
          {canEdit && (
            <Button variant="secondary" onClick={() => setOpen("edit")}>
              Edit this {typeLabel}
            </Button>
          )}
          {canArchive && (
            <Button variant="secondary" onClick={() => setOpen("archive")}>
              Retire this {typeLabel}
            </Button>
          )}
        </div>
      )}

      {open === "edit" && target && objectType === "semantic-concept" && (
        <NewConceptDialog
          open
          projectId={projectId}
          edit={target}
          onClose={() => setOpen(null)}
          onCreated={onChanged}
        />
      )}
      {open === "edit" && target && objectType === "semantic-view" && (
        <NewSemanticViewDialog
          open
          projectId={projectId}
          edit={target}
          onClose={() => setOpen(null)}
          onCreated={onChanged}
        />
      )}
      {open === "archive" && target && (
        <ArchiveObjectDialog
          projectId={projectId}
          objectType={objectType}
          typeLabel={typeLabel}
          detail={detail}
          target={target}
          onDone={onChanged}
          onClose={() => setOpen(null)}
        />
      )}
    </Panel>
  );
}

/*
 * `versionTone` IS GONE (76-2). Its three success words and three warning words
 * are the union's; what it added was a `default: "neutral"` -- a version state
 * the console has never heard of, drawn in the colour of "nothing to see". It
 * reads `Unknown` in the warning colour now, so a new server word is visible the
 * first time it ships instead of the first time somebody notices.
 */

export default function GovernanceObjectWorkbench({
  projectId,
  section,
  objectType,
  objectId,
  tab,
  versionId,
}: {
  projectId: string;
  section: string;
  objectType: string;
  objectId: string;
  tab: string;
  versionId: string | null;
}) {
  const { route, navigate } = useRoute();
  const { state, reload } = useGovernanceObject(projectId, section, objectType, objectId, versionId);
  const contract = findObjectContract("governance", section, objectType);
  const sectionMeta = findSection("governance", section);
  const typeLabel = objectLabel(objectType);

  const backToCollection = () =>
    navigate({
      workspace: "governance",
      section,
      lens: null,
      objectType: null,
      objectId: null,
      tab: null,
      versionId: null,
      action: null,
    });

  const tabs = useMemo<NavTab[]>(
    () =>
      (contract?.tabs ?? []).map((candidate) => ({
        key: candidate,
        label: TAB_LABEL[candidate] ?? candidate,
        href: buildPath({
          ...route,
          section,
          lens: null,
          objectType,
          objectId,
          tab: candidate,
          // Leaving the versions tab drops the pinned version: a version has no
          // meaning under Hierarchy, and carrying it would build an unknown route.
          versionId: candidate === "versions" ? versionId : null,
          action: null,
        } as CanonicalRoute),
      })),
    [contract, route, section, objectType, objectId, versionId],
  );

  if (state.status === "denied") return <RouteState kind="denied" onBackToOverview={backToCollection} backLabel={`Back to ${sectionMeta?.label ?? "Governance"}`} />;
  if (state.status === "unknown") {
    return (
      <RouteState
        kind="unknown"
        reason={`No ${typeLabel} with this identifier exists in this Project. Nothing similar has been opened in its place.`}
        onBackToOverview={backToCollection}
        backLabel={`Back to ${sectionMeta?.label ?? "Governance"}`}
      />
    );
  }
  if (state.status === "loading") {
    return <p role="status" className="p-8 text-body text-text-secondary">Loading {typeLabel}…</p>;
  }
  if (state.status === "error") {
    return (
      <Stack className="gap-6 p-8">
        <Status as="block" tone="error" title={`${typeLabel} is unavailable`} action={<Button variant="secondary" onClick={reload}>Retry</Button>}>
          {state.message} No object from another Project has been substituted.
        </Status>
      </Stack>
    );
  }

  const envelope = state.envelope;
  const detail: GovernanceObject | null = envelope.object;

  if (!detail) {
    return (
      <RouteState
        kind="unavailable"
        reason={envelope.unavailable_reasons[0]?.message
          ?? `The owner of this ${typeLabel} has not been delivered. Your reference is intact and nothing is shown in its place.`}
        onBackToOverview={backToCollection}
        backLabel={`Back to ${sectionMeta?.label ?? "Governance"}`}
      />
    );
  }

  const collectionHref = buildPath({
    ...route,
    section,
    lens: sectionMeta?.lenses?.[0]?.slug ?? null,
    objectType: null,
    objectId: null,
    tab: null,
    versionId: null,
    action: null,
  } as CanonicalRoute);

  const pending = pendingOwner(objectType, tab);
  const selected = detail.selected_version_ref;
  // Composed once, before the tab branches, so an Evidence tab is chosen by the
  // registry rather than by a `default:` that would swallow an unimplemented one.
  const evidenceTab = section === "evidence"
    ? EvidenceTabs({ objectType, tab, detail, organizationId: route.organizationId ?? "", projectId })
    : null;

  return (
    <Stack className="gap-6" data-owner={`governance/${section}/${objectType}/${objectId}`}>
      <Breadcrumb>
        <BreadcrumbList>
          <BreadcrumbItem>
            <BreadcrumbLink
              href={collectionHref}
              onClick={(event) => {
                if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                event.preventDefault();
                backToCollection();
              }}
            >
              {sectionMeta?.label ?? "Governance"}
            </BreadcrumbLink>
          </BreadcrumbItem>
          <BreadcrumbSeparator />
          <BreadcrumbItem>
            <BreadcrumbPage>{detail.object_ref.label}</BreadcrumbPage>
          </BreadcrumbItem>
        </BreadcrumbList>
      </Breadcrumb>

      <h1 className="text-h1 font-h1 tracking-tight text-text">{detail.object_ref.label}</h1>
      <ObjectHeader name={detail.object_ref.label} source={`${typeLabel} · ${detail.object_ref.id}`} />

      <Panel className="grid gap-4 md:grid-cols-4">
        <div>
          <p className="mb-2 text-caption font-semibold uppercase tracking-wide text-text-secondary">Status</p>
          <Status tone={detail.lifecycle_status === "active" ? "success" : "neutral"}>{detail.lifecycle_status.replaceAll("_", " ")}</Status>
        </div>
        <div>
          <p className="mb-2 text-caption font-semibold uppercase tracking-wide text-text-secondary">Scope</p>
          <span className="text-ui text-text">{scopeLabel(detail.scope)}</span>
        </div>
        <div>
          <p className="mb-2 text-caption font-semibold uppercase tracking-wide text-text-secondary">Active version</p>
          {detail.active_version_ref
            ? <Status tone={stateTone(detail.active_version_ref.state)}><ObjectId value={detail.active_version_ref.id} title="Active version" /></Status>
            : <span className="text-ui text-text-secondary">Unavailable</span>}
        </div>
        <div>
          <p className="mb-2 text-caption font-semibold uppercase tracking-wide text-text-secondary">Selected version</p>
          {selected
            ? <Status tone={envelope.version_state === "stale" ? "warning" : "success"}><ObjectId value={selected.id} title="Selected version" /></Status>
            : <span className="text-ui text-text-secondary">Current</span>}
        </div>
      </Panel>

      {selected && (
        <Status
          as="block"
          tone={envelope.version_state === "stale" ? "warning" : "success"}
          title={envelope.version_state === "stale" ? "This pinned version is no longer current" : "This pinned version is the current one"}
          data-testid="selected-version-banner"
        >
          {envelope.version_state === "stale"
            ? `Version ${selected.id} is authorized, immutable and superseded. It is shown exactly as requested; the current version has not been opened in its place.`
            : `Version ${selected.id} is the active version of this ${typeLabel}.`}
        </Status>
      )}

      <NavTabs
        label={typeLabel}
        tabs={tabs}
        current={tab}
        onNavigate={(next) => navigate({ tab: next, versionId: next === "versions" ? versionId : null, action: null })}
      />

      {/* Correcting and retiring a governed Semantic Model object. Both are the
          same Change Set the creation dialogs already use, from the exact
          version above — and both appear only when the SERVER declared them. */}
      {section === "semantic-model" && (
        <SemanticModelActions
          projectId={projectId}
          objectType={objectType}
          typeLabel={typeLabel}
          detail={detail}
          onChanged={reload}
        />
      )}

      {/* `Explore data` is the exact published-version handoff to Analyze. It
          appears only when the SERVER declared the action, which it does only
          for a published, compiled version with at least one queryable pair. */}
      {detail.allowed_actions.includes("explore-data") && (
        <Panel>
          <PanelHeader
            title="Explore this Semantic View"
            description="Opens Analyze on the exact published version below. The address is shareable and pins that version; it never follows the newest one."
          />
          <ExploreDataAction
            handoff={(detail.summary as Record<string, unknown>).explore_handoff as ExploreHandoff | null}
            onNavigate={(query) =>
              // Through the router, never window.location.assign: the router
              // owns validation and canonicalization, and an assign() would
              // skip both and reload the whole application to do it.
              navigate({
                workspace: "analyze",
                section: "explore",
                lens: null,
                objectType: null,
                objectId: null,
                tab: null,
                versionId: null,
                action: null,
                query,
              })
            }
          />
        </Panel>
      )}

      {section === "controls-quality" && objectType === "control-case" && tab === "candidate-change" ? (
        <CaseCandidateChangeTab detail={detail} />
      ) : section === "controls-quality" && objectType === "control-case" && tab === "impact" ? (
        <CaseImpactTab detail={detail} />
      ) : section === "controls-quality" && objectType === "control-case" && tab === "decision-history" ? (
        // `reload` is what makes the new decision appear in the list it was
        // just appended to; without it the tab shows the state before the act.
        <CaseDecisionHistoryTab detail={detail} projectId={projectId} onDecided={reload} />
      ) : section === "controls-quality" && objectType === "dq-monitor" && tab === "overview" ? (
        <MonitorOverviewTab detail={detail} />
      ) : section === "controls-quality" && objectType === "dq-monitor" && tab === "coverage" ? (
        <MonitorCoverageTab detail={detail} />
      ) : section === "controls-quality" && objectType === "dq-monitor" && tab === "history" ? (
        <MonitorHistoryTab detail={detail} />
      ) : section === "controls-quality" && objectType === "dq-monitor" && tab === "issues" ? (
        <MonitorIssuesTab detail={detail} />
      ) : section === "controls-quality" && objectType === "rule-set" && tab === "rules" ? (
        <RuleSetRulesTab detail={detail} projectId={projectId} onAdopted={reload} />
      ) : section === "controls-quality" && objectType === "rule-set" && tab === "effective-dates" ? (
        <RuleSetEffectiveDatesTab detail={detail} />
      ) : section === "controls-quality" && objectType === "rule-set" && tab === "approvals-exceptions" ? (
        <RuleSetApprovalsExceptionsTab detail={detail} />
      ) : /* A tax_fee ladder's Overview otherwise falls to the generic FieldTable
             below, which cannot separate "complete as a document" from "complete
             as an answer" — the one distinction the ladder needs stated (41.7). */
      section === "controls-quality" &&
        objectType === "rule-set" &&
        tab === "overview" &&
        isTaxFeeRuleSet(detail) ? (
        <TaxFeeLadderOverviewTab detail={detail} />
      ) : evidenceTab ? (
        evidenceTab
      ) : section === "semantic-model" && tab === "source-bindings" ? (
        <SourceBindingsTab
          bindings={(detail.summary as Record<string, unknown>).source_bindings as SourceBindings | null}
          organizationId={route.organizationId ?? ""}
          projectId={projectId}
          typeLabel={typeLabel}
        />
      ) : section === "semantic-model" && objectType === "semantic-concept" && tab === "semantics" ? (
        <SemanticsTab detail={detail} />
      ) : section === "semantic-model" && objectType === "semantic-view" && tab === "metrics-dimensions" ? (
        <MetricsDimensionsTab detail={detail} />
      ) : section === "semantic-model" && objectType === "canonical-field" && tab === "lineage" ? (
        // Audit 2026-08-17 P2-7: `/api/dimension-lineage/fed-by` shipped and no
        // screen anywhere called it. This is the reader, on the workbench
        // `governance.md:43` already ratifies for a Canonical Field.
        <DimensionLineageTab detail={detail} />
      ) : section === "semantic-model" && tab === "definition" ? (
        // The Business Domain links, on the tab that carries the object's own
        // identity. They were reaching the browser already -- the server sends
        // `business_domain_refs` in `summary` (`governance_read_model.py:761`,
        // `:830`) -- and the generic field dump below printed them as a raw
        // value. `governance.md:72` asks for a LINK to the owner, which a value
        // is not: Governance > Master Data owns the Business Domain
        // (`README.md:97`), so that is where each one opens.
        <Stack className="gap-6">
          <Panel>
            <PanelHeader
              title="Business Domains"
              description="The governed domains this object is classified under. Their owner is Governance › Master Data."
            />
            <BusinessDomainLinks
              refs={((detail.summary as Record<string, unknown>).business_domain_refs as string[]) ?? []}
              organizationId={route.organizationId ?? ""}
              projectId={projectId}
            />
          </Panel>
          <Panel flush>
            <PanelHeader title="Definition" description={`Owned fields of this ${typeLabel}.`} />
            <FieldTable
              caption={`${typeLabel} definition`}
              rows={[
                ["Identity", detail.object_ref.id, "The exact stored identity of this object. It is what every reference pins."],
                ["Scope", detail.scope, fieldMeaning("scope")],
                ...Object.entries(detail.summary)
                  .filter(([key]) => key !== "business_domain_refs")
                  .map(
                    ([key, value]) =>
                      [label(key), value, fieldMeaning(key)] as [string, unknown, string | null],
                  ),
                ["Evidence as of", detail.evidence_as_of, "When the figures above were last read from their owner."],
              ]}
            />
          </Panel>
        </Stack>
      ) : section === "master-data" && objectType === "registry" && tab === "hierarchy" &&
        (detail.summary as Record<string, unknown>).capability_key === "country" ? (
        <CountryWorkspace projectId={projectId} />
      ) : section === "master-data" && tab === "hierarchy" ? (
        // `reload` is passed so a completed command re-reads the object from its
        // owner rather than the tab patching its own row: the archived flag,
        // the used-by count and the version list all move together, and only
        // the server knows the new truth.
        <MasterDataHierarchyTab detail={detail} projectId={projectId} onChanged={reload} />
      ) : section === "master-data" && objectType === "tracked-entity" && tab === "representations" ? (
        // The last contracted Governance tab with no module of its own: it fell
        // to the generic field dump below, so the one subject the tab exists for
        // was shown as a raw record. `screens.py pages` counted it 51st of 51.
        <RepresentationsTab detail={detail} />
      ) : section === "master-data" && objectType === "tracked-entity" && tab === "coverage" ? (
        // The SECOND contracted facet of this object with no module of its own,
        // found by the same reading that found the first: its owner ships the
        // whole entity x Datastream matrix and the tab printed it as a raw
        // record. Ratified in `governance.md`, *Amendment, 2026-09-01*.
        <CoverageTab detail={detail} />
      ) : section === "master-data" && tab === "mappings-aliases" ? (
        <MasterDataMappingsAliasesTab detail={detail} />
      ) : section === "master-data" &&
        objectType === "registry" &&
        tab === "overview" &&
        (detail.summary as Record<string, unknown>).object_kind ? (
        // A registry a CLIENT declared (Story 64.1, AI-232). Its Overview fell to
        // the generic FieldTable below, which printed `sources` as a raw record --
        // so the one question this screen exists to answer, what feeds this
        // object, was the one thing it could not say.
        <ClientObjectSourcesPanel detail={detail} />
      ) : section === "master-data" &&
        (objectType === "business-domain" || objectType === "master-data-object") &&
        tab === "overview" ? (
        // The identity, typed -- and the commands that act on it, HERE. This
        // Overview was the generic field dump below, and the only console caller
        // of `runNodeCommand` was the Context Hub: the workbench a person opens
        // to govern a Business Domain named it and sent them elsewhere to rename,
        // archive or restore it. `reload` is passed for the same reason the
        // Hierarchy tab gets it -- a completed command re-reads the object from
        // its owner rather than the tab patching its own row.
        <MasterDataIdentityOverview
          detail={detail}
          projectId={projectId}
          typeLabel={typeLabel}
          onChanged={reload}
        />
      ) : pending ? (
        <Panel>
          <PanelHeader
            title={`${TAB_LABEL[tab] ?? tab} is not delivered`}
            description={`${label(pending.what)} is owned by Story ${pending.story}. Nothing is shown in its place: no sample object, no zero count, no borrowed screen.`}
          />
          <p className="text-ui text-text-secondary">
            The identity, scope, owner and versions above are real and come from this object's owner.
            This tab stays visible because it is contracted, and hiding it would erase the record of
            what is still to build.
          </p>
        </Panel>
      ) : tab === "used-by" ? (
        <UsedByPanel
          usedBy={detail.used_by}
          typeLabel={typeLabel}
          organizationId={route.organizationId ?? ""}
          projectId={projectId}
        />
      ) : tab === "versions" ? (
        <Panel flush>
          <PanelHeader title="Versions" description="Every recorded state of this object, newest first." />
          {/* THE SAME REPAIR AS `UsedByPanel`, ON THE SAME DEFECT. A
              `master-data-object` served `{count: 3, refs: []}` and this branch
              printed "No version recorded" for a Product with three published
              versions. A count without a list is now named as such. */}
          {detail.versions.state === "unavailable" ? (
            <EmptyState
              title={detail.versions.count > 0 ? `${detail.versions.count} version(s) recorded, not listed here` : "No version ledger"}
              description={
                (detail.versions as { reason?: { message?: string } }).reason?.message ??
                (detail.versions.count > 0
                  ? `This ${typeLabel} has ${detail.versions.count} recorded version(s) its owner cannot list on this screen. Reload to try again.`
                  : `This ${typeLabel} has no version store its owner can read. That is a gap, not an empty history.`)
              }
            />
          ) : detail.versions.count === 0 ? (
            <EmptyState title="No version recorded" description="Its owner answered, and there is no version yet." />
          ) : (
            <TableScroll label={`${typeLabel} versions`}>
              <Table>
                <TableBody>
                  {detail.versions.refs.map((ref: VersionRef) => {
                    const pinnable = (contract?.tabs ?? []).includes("versions");
                    const href = pinnable
                      ? buildPath({ ...route, section, lens: null, objectType, objectId, tab: "versions", versionId: ref.id, action: null } as CanonicalRoute)
                      : undefined;
                    return (
                      <TableRow key={ref.id} data-selected={ref.id === versionId ? "true" : undefined}>
                        <TableCell className="w-96 font-semibold text-text">
                          {href ? (
                            <a
                              className="underline-offset-2 hover:underline focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus"
                              href={href}
                              aria-current={ref.id === versionId ? "page" : undefined}
                              onClick={(event) => {
                                if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                                event.preventDefault();
                                navigate({ tab: "versions", versionId: ref.id, action: null });
                              }}
                            >
                              <ObjectId value={ref.id} title="Reference" />
                            </a>
                          ) : ref.id}
                        </TableCell>
                        <TableCell><Status tone={stateTone(ref.state)}>{stateLabel(ref.state)}</Status></TableCell>
                        <TableCell className="text-ui text-text-secondary">
                          {ref.recorded_at ? formatTimestamp(ref.recorded_at) : "No recorded time"}
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </TableScroll>
          )}
        </Panel>
      ) : tab === "evidence" ? (
        <Panel flush>
          <PanelHeader title="Evidence" description="Recorded by its owner, referenced here, copied nowhere." />
          {detail.evidence.state === "unavailable" ? (
            <EmptyState title="No evidence owner" description="No adapter can reference evidence for this object yet." />
          ) : detail.evidence.refs.length === 0 ? (
            <EmptyState title="No evidence recorded" description="Its owner answered, and there is none." />
          ) : (
            <TableScroll label={`${typeLabel} evidence`}>
              <Table>
                <TableBody>
                  {detail.evidence.refs.map((ref: EvidenceRef) => (
                    <TableRow key={ref.evidence_id}>
                      <TableCell className="w-64 font-semibold text-text">{wireWord(ref.kind)}</TableCell>
                      <TableCell><ObjectId value={ref.evidence_id} title="Evidence Record" /></TableCell>
                      <TableCell><Status tone={stateTone(ref.state)}>{stateLabel(ref.state)}</Status></TableCell>
                      <TableCell className="text-ui text-text-secondary">
                        {ref.recorded_at ? formatTimestamp(ref.recorded_at) : "No recorded time"}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableScroll>
          )}
        </Panel>
      ) : (
        <Panel flush>
          <PanelHeader title={TAB_LABEL[tab] ?? tab} description={`Owned fields of this ${typeLabel}.`} />
          <FieldTable
            caption={`${typeLabel} ${tab}`}
            rows={[
              ["Identity", detail.object_ref.id],
              ["Scope", detail.scope],
              ...Object.entries(detail.owner).map(([key, value]) => [`Owner · ${label(key)}`, value] as [string, unknown]),
              ...Object.entries(detail.summary).map(([key, value]) => [label(key), value] as [string, unknown]),
              ["Evidence as of", detail.evidence_as_of],
            ]}
          />
        </Panel>
      )}
    </Stack>
  );
}
