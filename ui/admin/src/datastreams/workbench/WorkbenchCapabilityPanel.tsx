/**
 * The Datastream's capability projection (Story 48.1, AC5).
 *
 * One panel, three tabs. Overview answers "does this capability apply here and is
 * it covered"; Mapping and Processing show the impact blocks their own subject
 * owns. Which blocks reach which tab is decided by the server, so this component
 * renders whatever arrived and invents nothing when a block is absent.
 *
 * It is one component rather than three because the shape is the same on all
 * three tabs, and the console already carries 170 locally re-implemented
 * components.
 *
 * THIS PANEL ADDS NO TAB — AND A CAPABILITY DOES (story 58.6). This paragraph
 * read "it adds no navigation item and no seventh tab: the capability is a
 * projection inside the six tabs that exist", which the amendment « Une capacité
 * activée AJOUTE son onglet » of `datastream-workbench-and-wizard.md` reverses:
 * `Tax & fees` opens `Cost`, `Placement mapping` will open `Placements`. The
 * half that stays true is about THIS component: it is a projection rendered
 * inside a tab, and it is not itself one. Which tabs exist is decided by the
 * capability state on the Workbench header and by `shell/capabilityTabs.ts`.
 *
 * ─── AND IT IS NOW ONE PANEL, NOT TWO — amendment 11, the half left open ─────
 *
 * « Les deux panneaux `Modules` et `Project capabilities on this Datastream`
 * fusionnent en un seul ». Measured on the 2026-08-12 capture of `Overview` at
 * 1600px: both enumerated the same six capabilities, 3742px apart on one page —
 * one giving the Project decision and its switch, the other the compilation of
 * that same capability against this Datastream. Two lists of six names is one
 * list of six rows that was cut in half.
 *
 * So a row now carries, in one place: the capability, its scope in a sentence,
 * the tab it adds, the Project state and its switch (`modules`), and — when a
 * Change Set has compiled one — the coverage, the reason, the impact blocks and
 * the blockers (`projection`). The surviving NAME is `Modules`, because that is
 * the one the ratified `Overview` row uses.
 *
 * `modules` is what decides which panel this is. Absent, the component is the
 * projection-only panel Mapping and Processing were written against and its
 * markup is unchanged; present (including `null`, "the header carried no
 * capability state"), it is the merged `Modules` panel of `Overview`.
 */

import type { ReactNode } from "react";
import {
  Badge,
  CapabilityCoverage,
  CoverageStateBadge,
  Panel,
  PanelHeader,
  Status,
  Switch,
  capabilityLabel,
  type CoverageState,
  formatNumber,
  stateLabel,
  stateTone,
  Retry,
} from "../../ui";
import { record, records, text, titleCase } from "./evidence";
import type { OwnerReference } from "../../shell/pages/ProjectSettings";

export interface WorkbenchCapability {
  capability_key: string;
  availability?: string;
  applicability: string;
  coverage_state: CoverageState | "pending";
  reason?: string;
  impact?: Record<string, unknown>;
  blockers?: Array<{ code?: string; message?: string }>;
  exceptions?: Array<{ reason?: string; severity?: string }>;
  repair?: OwnerReference | null;
  governance_owner_reference?: OwnerReference;
}

export interface WorkbenchCapabilityProjection {
  capabilities: WorkbenchCapability[];
  primary_action?: {
    capability_key: string;
    kind: string;
    label: string;
    reason: string;
    owner_reference?: OwnerReference;
  } | null;
}

/**
 * One capability as the PROJECT decided it, from the Workbench header.
 *
 * The other half of a merged row: `WorkbenchCapability` above is what a Change
 * Set compiled against this Datastream, this is what the control plane wrote for
 * the whole Project. They are joined on `capability_key` and never on anything
 * else — the two payloads are composed by different modules.
 */
export interface CapabilityModule {
  capability_key: string;
  availability?: string;
  state: string;
  open: boolean;
  tab?: string | null;
}

/**
 * The scope of each Project capability, in one sentence — story 58.9.
 *
 * Read from the ratified capability cards (`docs/product-architecture/
 * capabilities/*.md`, « Outcome ») rather than invented here, because a module
 * row that describes a capability differently from its own card is a second
 * definition.
 */
const CAPABILITY_SCOPE: Record<string, string> = {
  country:
    "Turns source geography into one governed country and market model, without losing the source's own country grain.",
  currency_fx:
    "Makes monetary measures comparable through dated, versioned FX evidence, and keeps the native amount and currency beside the converted one.",
  reporting_timezone:
    "Preserves each source's own reporting day boundary and states the day-offset risk against the Project's. It signals; it never re-aligns a day by itself.",
  tax_fees:
    "Explains the gap between native media values and invoiced values through an exact, versioned rule ladder.",
  competitors:
    "Governs one entity identity per organization, its role on this Project, and the source-specific values that represent it.",
  // Written by story 61.1, from the first measurement of the matching itself:
  // story 61.5 opened the capability and deliberately left this line empty
  // because `project-settings.md` had not yet named the two columns it adds.
  placement_mapping:
    "Binds a media plan line to the campaigns and placements observed on a connector, and adds the matched line's key and label to an aggregation — null, never zero, on spend no line planned.",
};

/** The five states of migration 131, plus the absence of a row.
 *
 *  `unset` is not a sixth state and not a quiet `disabled`: nobody decided
 *  anything, the control plane never wrote the row. Rendering it as `Disabled`
 *  would present an absence as an operator's choice. */
const CAPABILITY_STATE_LABEL: Record<string, string> = {
  ready: "Ready",
  degraded: "Degraded",
  draft: "Draft",
  disabled: "Off",
  blocked: "Blocked",
  unset: "Never configured",
};

/*
 * THE PRIVATE CAPABILITY MAP IS GONE (76-2). Two disagreements it never
 * declared: `blocked` red where the union says warning (repairable -- the same
 * arbitration Governance's override lost), and `draft` grey where the union says
 * warning (a draft is work somebody still owes). `unset` is declared, at the
 * reading this panel's own label already gave it: "Never configured".
 */

/** Human sentences for the impact blocks, in the order a reader needs them. */
const BLOCK_LABEL: Record<string, string> = {
  detected_support_selection: "Detected support",
  grain_before_after: "Grain",
  cardinality_scan: "Cardinality and scan",
  quota_cost: "Quota and cost",
  recent_history: "Recent history",
  historical_coverage: "Historical coverage",
  backfill: "Backfill",
  fan_out: "Fan-out",
};

/**
 * A block's NAME, in words, whatever the server called it — finding D-9.
 *
 * `BLOCK_LABEL[key] ?? key` printed the payload key itself the moment a block
 * arrived that this map does not list, and two of them reach the screen today:
 * `bound_fields` and `expected_fields`, rendered as `bound_fields: Unavailable`
 * on a user screen. A key is a machine word — `CLAUDE.md` forbids it here — and
 * the repair is the FALLBACK, not two more entries: the next block the compiler
 * grows would leak in exactly the same way. `IMPACT_BLOCKS` in
 * `capability_proposals.py` lists eight and this map names those eight; anything
 * else is humanised rather than shown raw.
 */
function blockLabel(key: string): string {
  return BLOCK_LABEL[key] ?? titleCase(key);
}

function summarizeBlock(key: string, value: unknown): string {
  const block = record(value);
  // A SCALAR BLOCK IS A VALUE, NOT AN ABSENCE — the second half of D-9.
  // `record()` answers `null` to anything that is not an object, so a block
  // carrying a plain count (`bound_fields: 1`) reported "Unavailable" — an
  // unreadable measurement — for a number the payload had already given.
  if (!block) {
    if (typeof value === "number" && Number.isFinite(value)) return formatNumber(value);
    if (typeof value === "boolean") return value ? "Yes" : "No";
    if (Array.isArray(value)) return value.length ? value.map(String).join(", ") : "None";
    return text(value);
  }
  switch (key) {
    case "grain_before_after": {
      const added = Array.isArray(block.added) ? block.added : [];
      const removed = Array.isArray(block.removed) ? block.removed : [];
      if (added.length === 0 && removed.length === 0) return "Unchanged";
      const parts: string[] = [];
      if (added.length) parts.push(`+${added.map(String).join(", ")}`);
      if (removed.length) parts.push(`−${removed.map(String).join(", ")}`);
      return parts.join(" · ");
    }
    case "cardinality_scan":
      return block.state === "estimated"
        ? `${block.estimated_grain_cardinality} rows${block.over_limit ? " · over limit" : ""}`
        : text(block.reason);
    case "quota_cost":
      return block.state === "declared" ? "Declared by the Connector contract" : text(block.reason);
    case "recent_history": {
      const executions = Array.isArray(block.executions) ? block.executions : [];
      return executions.length ? `${executions.length} recent execution(s)` : text(block.reason);
    }
    case "historical_coverage":
      return block.earliest_completed_date
        ? `From ${text(block.earliest_completed_date)}`
        : text(block.reason);
    case "backfill":
      if (!block.required) return "Not required";
      return block.feasible
        ? `Required · bounded at ${text(block.max_provider_backfill_days)} day(s)`
        : "Required · no provider bound evidenced";
    case "fan_out":
      return `${block.downstream_consumers ?? 0} downstream consumer(s)`;
    case "detected_support_selection":
      if (block.state !== "detected") return text(block.reason ?? block.missing ?? block.state);
      // Additive (Story 41.7): Tax & Fees is the one capability whose detection
      // is about an OBSERVED source type rather than a provider feature, and
      // "Detected and selected" discards the only fact a reader needs — which
      // type was observed. Every other capability keeps the original wording,
      // byte for byte, because none of them carries this key.
      if (typeof block.declared_source_type === "string") {
        const observed =
          block.declared_source_type === "UNKNOWN"
            ? "source type unresolved"
            : block.declared_source_type;
        return block.selected
          ? `${observed} · rules matched`
          : `${observed} · no rule matched`;
      }
      return block.selected ? "Detected and selected" : "Detected, not selected";
    default:
      return text(block.state);
  }
}

/**
 * Tax & Fees, in detail (Story 41.7, AC10/AC11).
 *
 * The generic block grid collapses `detected_support_selection` to one line, and
 * for this capability that line throws away the whole verdict: which source type
 * was observed and on what evidence, which rules matched, and — the useful half —
 * which rules did NOT and why.
 *
 * `refused_rules` is deliberately returned by the compiler
 * (`capability_compilers.py:961-964`): "a scope filter that silently discards a
 * rule leaves an operator unable to tell 'this rule does not apply here' from
 * 'this rule was never considered', and those need different repairs". A
 * collapsed "3 rules did not apply" is a regression against what the server
 * already computed, so the whole list is rendered with its codes and reasons
 * verbatim.
 *
 * This is a block inside the existing `<li>`. It is not a second panel, not a
 * card, not a drawer, and it adds no tab.
 */
/**
 * What the publication actually landed (Story 48.4, Task 3 second bullet).
 *
 * The refusal list above says a CPM rule could not fire; this says which inputs
 * arrived, so the reader can check the refusal instead of taking it. The two
 * columns of the compiler's answer are deliberately kept apart: `observed_inputs`
 * is every key the publication wrote, `available_inputs` is the subset a rule may
 * rely on. A key present with a falsy value sits in the first and not the second
 * — `measured_impressions: 0` from a source that reports none is ABSENT for a
 * rule, and rendering `0` as a measurement is exactly how a verification fee
 * becomes a confident zero.
 */
function ObservedInputs({ support }: { support: Record<string, unknown> }) {
  const observed = record(support.observed_inputs) ?? {};
  const available = new Set(
    (Array.isArray(support.available_inputs) ? support.available_inputs : []).map(String),
  );
  const keys = Object.keys(observed).sort();
  // The values here are counts and micros, not strings, so `text()` — which
  // answers "Unavailable" to anything that is not a non-empty string — would
  // erase every one of them.
  const value = (raw: unknown) => (raw === null || raw === undefined ? "null" : String(raw));

  return (
    <div>
      <p className="m-0 text-caption text-text-secondary">
        {keys.length
          ? "Physical inputs this publication landed"
          : "No physical input was recorded by this publication"}
      </p>
      {keys.length ? (
        <ul className="m-0 mt-1 list-none space-y-1 p-0 text-caption">
          {keys.map((key) => (
            <li key={key}>
              <span className="font-mono">{key}</span>
              {" — "}
              {available.has(key) ? (
                <span className="font-mono">{value(observed[key])}</span>
              ) : (
                <span className="text-text-secondary">
                  {`not landed (recorded as ${value(observed[key])}, which proves nothing)`}
                </span>
              )}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function TaxFeesDetail({ support }: { support: Record<string, unknown> }) {
  const matched = Array.isArray(support.matched_rule_keys) ? support.matched_rule_keys : [];
  const refused = records(support.refused_rules);
  const phases = Array.isArray(support.cascade_phases) ? support.cascade_phases : [];
  // UNKNOWN is a typed gap, not a source type. Printing it as one tells the
  // reader the system knows something it does not.
  const unresolvedType =
    support.declared_source_type === "UNKNOWN" || support.source_type_origin === "unresolved";

  return (
    <div className="space-y-3 rounded-medium border border-divider-base p-4">
      <dl className="m-0 grid grid-cols-3 gap-4 text-ui max-lg:grid-cols-2 max-md:grid-cols-1">
        <div>
          <dt className="text-caption text-text-secondary">Observed source type</dt>
          <dd className="m-0 text-text">
            {unresolvedType ? "Unresolved — no type is proved" : text(support.declared_source_type)}
          </dd>
        </div>
        <div>
          {/* Only a contracted or overridden type is enough to conclude "not
              applicable"; an inferred one is enough only to propose. So the
              origin and the confidence sit beside the value, never behind it. */}
          <dt className="text-caption text-text-secondary">How it was established</dt>
          <dd className="m-0 text-text">
            {`${text(support.source_type_origin)} · confidence ${text(support.source_type_confidence)}`}
          </dd>
        </div>
        <div>
          <dt className="text-caption text-text-secondary">Tax posture</dt>
          <dd className="m-0 text-text">{text(support.tax_posture)}</dd>
        </div>
        <div>
          <dt className="text-caption text-text-secondary">Cascade phases reached</dt>
          <dd className="m-0 text-text">
            {phases.length ? phases.map(String).join(", ") : "None"}
          </dd>
        </div>
        <div>
          <dt className="text-caption text-text-secondary">Rules matched</dt>
          <dd className="m-0 text-text">
            {matched.length ? `${matched.length} · ${matched.map(String).join(", ")}` : "None"}
          </dd>
        </div>
        <div>
          {/* Two versions, and the verdict is only readable against both. */}
          <dt className="text-caption text-text-secondary">Compiled against</dt>
          <dd className="m-0 font-mono text-caption text-text">
            {`ladder ${text(support.rule_set_version_id)} · evidence ${text(support.tax_evidence_version_id)}`}
          </dd>
        </div>
      </dl>
      {unresolvedType ? (
        <Status as="block" tone="warning" title="The source type is a typed gap, not a value">
          Nothing here should be read as "no fee applies". Only a contracted or overridden
          source type proves that conclusion; an unresolved one proves nothing either way.
        </Status>
      ) : null}
      <ObservedInputs support={support} />
      {refused.length > 0 ? (
        <div>
          <p className="m-0 text-caption text-text-secondary">
            Rules considered and not applied — why each one did not, kept apart from rules that
            were never considered
          </p>
          <ul className="m-0 mt-1 list-none space-y-1 p-0 text-caption">
            {refused.map((rule, index) => (
              <li key={`${text(rule.rule_key)}:${index}`}>
                <span className="font-mono">{text(rule.rule_key)}</span>
                {" — "}
                <span className="font-mono">{text(rule.code)}</span>
                {" — "}
                {text(rule.reason)}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

/** The server's sentence about this Datastream, verbatim.
 *
 *  `pending` is the state a reader meets most often and it is neither "not
 *  applicable" nor a zero: no Change Set has compiled this capability for this
 *  Datastream yet. Re-wording it in the client is how two screens end up
 *  disagreeing about what it means. */
function coverageReason(capability: WorkbenchCapability): string {
  return (
    capability.reason ??
    (capability.applicability === "not_applicable"
      ? "Not applicable to this Datastream."
      : capability.applicability === "pending"
        ? "No Change Set has compiled this capability for this Datastream."
        : "Coverage compiled by the last Change Set.")
  );
}

export default function WorkbenchCapabilityPanel({
  projection,
  modules,
  onModuleToggle,
  moduleBusy = false,
  renderModuleNotice,
  onOpenOwner,
  onRetryCapabilities,
}: {
  projection: WorkbenchCapabilityProjection | null;
  /** The Project rows from the Workbench header — `Overview` only.
   *
   *  Three values, three meanings, and they are not interchangeable: absent
   *  means "this mount is not the merged panel", `null` means "the header
   *  carried no capability state, nothing was measured", `[]` means "the control
   *  plane wrote no row for this Project". The last two are different repairs
   *  and have never been allowed to share a sentence. */
  modules?: CapabilityModule[] | null;
  onModuleToggle?: (change: { key: string; next: boolean; label: string }) => void;
  /** A preparation is in flight, so no second switch may be moved. */
  moduleBusy?: boolean;
  /** What the caller wants said under one row — the Change Set it just prepared.
   *  The panel owns the layout; the change-set flow stays with the page that
   *  performs it, so no confirmation ever passes through here. */
  renderModuleNotice?: (capabilityKey: string) => ReactNode;
  onOpenOwner?: (owner: OwnerReference) => void;
  /** Re-reads the Workbench header this panel is handed (76-4). Only the
   *  route that made that read can repeat it, so the gesture arrives as a
   *  prop rather than being invented here. */
  onRetryCapabilities: () => void;
}) {
  const compiled = projection?.capabilities ?? [];
  /** The merged `Modules` panel of amendment 11, or the projection-only panel. */
  const merged = modules !== undefined;
  if (!merged && compiled.length === 0) return null;

  const compiledByKey = new Map(compiled.map((entry) => [entry.capability_key, entry]));
  const declared = modules ?? [];
  const declaredKeys = new Set(declared.map((entry) => entry.capability_key));
  // The Project order first, then any capability the projection compiled and the
  // header did not declare — dropping it would hide a compiled verdict because
  // two payloads disagreed about a list.
  const rows: Array<{
    key: string;
    module: CapabilityModule | null;
    capability: WorkbenchCapability | null;
  }> = merged
    ? [
        ...declared.map((entry) => ({
          key: entry.capability_key,
          module: entry,
          capability: compiledByKey.get(entry.capability_key) ?? null,
        })),
        ...compiled
          .filter((entry) => !declaredKeys.has(entry.capability_key))
          .map((entry) => ({ key: entry.capability_key, module: null, capability: entry })),
      ]
    : compiled.map((entry) => ({ key: entry.capability_key, module: null, capability: entry }));

  const action = projection?.primary_action ?? null;
  return (
    <Panel flush data-testid={merged ? "modules" : undefined}>
      <PanelHeader
        title={merged ? "Modules" : "Project capabilities on this Datastream"}
        description={
          merged
            ? "One row per Project capability: what it covers, whether it adds a tab, its measured state, and what the last Change Set compiled for this Datastream. A switch PREPARES a Project Change Set — nothing changes until it is confirmed in Project Settings › Changes, and the state it lands in is decided there."
            : "Applicability, coverage and the exact reason, composed by the server."
        }
      />
      {/* One action, not six: the highest-priority repair or review. */}
      {action ? (
        <div className="px-5 pt-5">
          <Status
            as="block"
            tone={action.kind === "repair" ? "error" : "warning"}
            title={`${action.label} — ${capabilityLabel(action.capability_key)}`}
            action={
              action.owner_reference ? (
                <button type="button" onClick={() => onOpenOwner?.(action.owner_reference!)}>
                  Open owner
                </button>
              ) : undefined
            }
          >
            {action.reason}
          </Status>
        </div>
      ) : null}
      {merged && modules === null ? (
        <div className="p-5">
          <Status
            as="block"
            tone="error"
            title="Project capabilities could not be read"
            action={<Retry onClick={onRetryCapabilities} />}
          >
            This Workbench header carried no capability state, so no Connector is shown. This is not
            the same as a Project with no capability: nothing was measured.
          </Status>
        </div>
      ) : null}
      {merged && modules !== null && modules.length === 0 ? (
        <div className="p-5">
          <Status as="block" tone="warning" title="No capability is declared on this Project">
            The control plane writes six rows per Project and wrote none here, so there is
            nothing to switch. Project Settings › Capabilities owns that seeding.
          </Status>
        </div>
      ) : null}
      {rows.length ? (
        <ul className="m-0 list-none divide-y divide-divider-base p-0">
          {rows.map(({ key, module, capability }) => {
            const label = capabilityLabel(key);
            const blocks = Object.entries(capability?.impact ?? {});
            // Rendered under the generic grid, for this capability only.
            const taxSupport =
              key === "tax_fees" ? record((capability?.impact ?? {}).detected_support_selection) : null;
            const optional = module !== null && module.availability !== "always_present";
            return (
              <li key={key} className="space-y-3 p-5" data-testid={`module-${key}`}>
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="max-w-[72ch]">
                    <p className="m-0 text-ui font-semibold text-text">{label}</p>
                    <p className="mt-1 mb-0 text-caption text-text-secondary">
                      {CAPABILITY_SCOPE[key]
                        ?? "No scope sentence is declared for this capability in this build."}
                    </p>
                    {/* The tab is named only while the capability is ON, because
                        that is when the tab exists. Naming `Cost` under a
                        capability that is off would describe a tab the router
                        refuses to open. */}
                    {module ? (
                      <p className="mt-1 mb-0 text-caption text-text-secondary">
                        {module.tab
                          ? module.open
                            ? `Adds the ${titleCase(module.tab)} tab`
                            : "No tab is added while this capability is off."
                          : "Adds no tab."}
                      </p>
                    ) : null}
                    {/* WHAT THE SAME CAPABILITY MEASURES HERE. The state badge on
                        the right is the PROJECT's decision; this is the reading
                        compiled against this Datastream, and the two were a whole
                        panel apart until amendment 11 was applied. The words say
                        which is which, because two badges on one row that do not
                        is the very repetition this merge removes. */}
                    {capability ? (
                      <div className="mt-2 flex flex-wrap items-center gap-2 text-caption text-text-secondary">
                        <span>On this Datastream</span>
                        <CoverageStateBadge state={capability.coverage_state as CoverageState} />
                        <span>{coverageReason(capability)}</span>
                      </div>
                    ) : null}
                  </div>
                  <div className="flex items-center gap-3">
                    {module ? (
                      <Badge tone={stateTone(module.state)}>
                        {CAPABILITY_STATE_LABEL[module.state] ?? stateLabel(module.state)}
                      </Badge>
                    ) : null}
                    {module && optional ? (
                      <Switch
                        // The MEASURED position, and it does not move on click:
                        // preparing a Change Set changes nothing yet, and the
                        // state it lands in (`ready` or `degraded`) is not
                        // chosen by whoever clicked.
                        checked={module.open}
                        disabled={moduleBusy || !onModuleToggle}
                        onCheckedChange={(next) => onModuleToggle?.({ key, next, label })}
                        aria-label={`${label} capability`}
                        data-testid={`module-switch-${key}`}
                      />
                    ) : null}
                    {module && !optional ? (
                      // The database REFUSES `disabled` for these two
                      // (migration 131). What is forbidden is the control, not
                      // the row: the capability keeps its line and says why it
                      // has no switch.
                      <Badge tone="info" data-testid={`module-always-present-${key}`}>
                        Always present
                      </Badge>
                    ) : null}
                  </div>
                </div>
                {blocks.length ? (
                  <dl className="m-0 grid grid-cols-3 gap-4 text-ui max-lg:grid-cols-2 max-md:grid-cols-1">
                    {blocks.map(([blockKey, value]) => (
                      <div key={blockKey}>
                        <dt className="text-caption text-text-secondary">{blockLabel(blockKey)}</dt>
                        <dd className="m-0 text-text">{summarizeBlock(blockKey, value)}</dd>
                      </div>
                    ))}
                  </dl>
                ) : null}
                {taxSupport ? <TaxFeesDetail support={taxSupport} /> : null}
                {(capability?.blockers ?? []).map((blocker, index) => (
                  <Status
                    key={`${blocker.code ?? "blocker"}:${index}`}
                    as="block"
                    tone="error"
                    action={
                      capability?.repair ? (
                        <button type="button" onClick={() => onOpenOwner?.(capability.repair!)}>
                          Open repair owner
                        </button>
                      ) : undefined
                    }
                  >
                    {blocker.message ?? blocker.code}
                  </Status>
                ))}
                {(capability?.exceptions ?? []).map((exception, index) => (
                  <Status key={`exception:${index}`} tone="warning">
                    {exception.reason}
                  </Status>
                ))}
                {renderModuleNotice?.(key)}
              </li>
            );
          })}
        </ul>
      ) : null}
    </Panel>
  );
}

/** Read the projection out of a tab payload without asserting its shape blindly. */
export function capabilityProjection(value: unknown): WorkbenchCapabilityProjection | null {
  const block = record(value);
  if (!block) return null;
  const capabilities = records(block.capabilities) as unknown as WorkbenchCapability[];
  if (capabilities.length === 0) return null;
  return {
    capabilities,
    primary_action: (record(block.primary_action) ??
      null) as WorkbenchCapabilityProjection["primary_action"],
  };
}

/** Coverage roll-up across capabilities, for surfaces that want one number. */
export { CapabilityCoverage };
