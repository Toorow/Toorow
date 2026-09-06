/**
 * The `Processing` tab — what this Datastream DOES, in what order.
 *
 * ## The order of the page is the answer to the tab's own question
 *
 * Finding D-5 of the 2026-08-12 visual review (github issue #69), and Jean twice
 * before it: the nine-step chain — the thing a person opens this tab for — was
 * the LAST block, under the schedule, the raw-zone policy, the version ledger
 * and the collection selector. `Processing` answers « what does this Datastream
 * do, in what order ». The schedule is *when* it does it: a parameter of the
 * plan, not the plan.
 *
 * So the page now reads:
 *
 *   1. `Ordered processing chain`               what it does, in order
 *   2. `Immutable processing plan versions`     which version pins that, and how
 *                                               to propose another
 *   3. `Schedule`                               when it runs
 *   4. Raw zone ownership / retention           where the raw lands, how long
 *   5. `What this Datastream collects`          the fields the plan asks for
 *
 * The version ledger moved up WITH the chain rather than staying where it was:
 * the chain's own title names the version it is reading, and the ledger is the
 * control that changes which one. Leaving the schedule between them would have
 * split one subject in two, which is the defect this reordering exists to end.
 */
import { useState } from "react";
import { EmptyState, Metric, PAGE_SORT_NOTE, Panel, PanelHeader, SortableHead, Status,
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll,
  Timestamp, sortRows, useTableSort, type SortValue,
} from "../../../ui";
import { record, records, text } from "../evidence";
import DatastreamChangeDialog from "../DatastreamChangeDialog";
import ProcessingChainPanel from "./ProcessingChainPanel";
import SchedulePanel from "../SchedulePanel";
import SourceSelectionPanel from "../SourceSelectionPanel";
import type { DimensionHistory } from "../dimensionDebt";
import type { OwnerReference } from "../../../shell/pages/ProjectSettings";
import type { Tab } from "../../../shell/pages/datastreamTabs";
import type { WorkbenchTabPayload } from "../workbenchTypes";

export type { OwnerLinks } from "./ProcessingChainPanel";
import type { OwnerLinks } from "./ProcessingChainPanel";

/**
 * The contract, path by path — what a plan version actually says.
 *
 * Objects are walked; ARRAYS AND SCALARS ARE LEAVES. A list of dimensions is one
 * value a person reads as a list, and exploding it into `selection.dimensions.0`,
 * `.1`, `.2` turns "two dimensions were added" into eleven renumbered rows that
 * all look changed. An empty object is a leaf too, and it renders as `{}` rather
 * than vanishing: "this section is empty" and "this section is absent" are two
 * different statements about a contract.
 */
function contractPaths(payload: Record<string, unknown> | null): Map<string, string> {
  const paths = new Map<string, string>();
  const walk = (node: unknown, path: string) => {
    if (node !== null && typeof node === "object" && !Array.isArray(node)) {
      const entries = Object.entries(node as Record<string, unknown>);
      if (entries.length === 0) {
        paths.set(path, "{}");
        return;
      }
      for (const [key, child] of entries) walk(child, path ? `${path}.${key}` : key);
      return;
    }
    paths.set(path, JSON.stringify(node) ?? "null");
  };
  if (payload) walk(payload, "");
  paths.delete("");
  return paths;
}

/**
 * WHAT TWO PLAN VERSIONS DISAGREE ABOUT — the reading the ledger never offered.
 *
 * The ledger listed versions by id, hash and date and compared NOTHING. A person
 * looking at four immutable versions of what their Datastream does to their data
 * could read four 64-character hashes and learn only that they differ, which is
 * the one thing the ids already said.
 *
 * NOTHING IS FETCHED FOR THIS. `normalized_payload` is on the wire for every
 * version (`datastream_workbench.py`, the `processing` tab SELECT), so the
 * comparison is made from what the tab already holds.
 *
 * ONLY THE PATHS THAT MOVED. A contract has some sixty paths and four or five of
 * them change between two versions; listing all sixty with "unchanged" beside
 * most is how a diff becomes a dump. Identical versions say so in one sentence.
 */
function PlanVersionCompare({
  reference,
  referenceLabel,
  selected,
  selectedLabel,
}: {
  reference: Record<string, unknown> | null;
  referenceLabel: string;
  selected: Record<string, unknown> | null;
  selectedLabel: string;
}) {
  const left = contractPaths(reference);
  const right = contractPaths(selected);
  const changed = [...new Set([...left.keys(), ...right.keys()])]
    .sort()
    .filter((path) => left.get(path) !== right.get(path));

  if (changed.length === 0) {
    return (
      <Status as="block" tone="neutral" title="These two versions say the same thing">
        Every governed path of {selectedLabel} is identical to {referenceLabel}. The two version
        identities differ; what they instruct does not.
      </Status>
    );
  }

  return (
    <TableScroll label={`${selectedLabel} compared with ${referenceLabel}`} className="max-h-72">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Contract path</TableHead>
            <TableHead>{referenceLabel}</TableHead>
            <TableHead>{selectedLabel}</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {changed.map((path) => (
            <TableRow key={path}>
              <TableCell className="font-mono text-caption">{path}</TableCell>
              {/* `Absent` is not `null`. A path one version does not carry at
                  all and a path it sets to null are different instructions, and
                  the JSON value is printed verbatim for the second. */}
              <TableCell className="max-w-[28ch] truncate font-mono text-caption" title={left.get(path) ?? "Absent"}>
                {left.get(path) ?? "Absent"}
              </TableCell>
              <TableCell className="max-w-[28ch] truncate font-mono text-caption" title={right.get(path) ?? "Absent"}>
                {right.get(path) ?? "Absent"}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableScroll>
  );
}

export default function WorkbenchProcessingPage({
  payload,
  projectId,
  datastreamId,
  mode,
  deliveryChannels,
  onConfirmed,
  onOpenOwner,
  links,
  onNavigateTab,
}: {
  payload: WorkbenchTabPayload;
  projectId: string;
  datastreamId: string;
  /** `source_kind`, for the one control on this tab that must not exist on a
   *  pushed source — amendment 1 of the 2026-08-11 review. */
  mode?: string | null;
  deliveryChannels?: string[];
  onConfirmed: () => void;
  onOpenOwner?: (owner: OwnerReference) => void;
  /** The header's owner routes, so a step can reach the surface it names.
   *
   *  `null` per address, not absent: the Workbench asks the router whether each
   *  one resolves before handing them down (`shell/routeHref.ts`), and a refused
   *  address arrives here as an absence so the step keeps its owner's name
   *  without offering a link that opens the unknown-route screen. */
  links?: OwnerLinks;
  onNavigateTab?: (tab: Tab) => void;
}) {
  const plans = records(payload.evidence.plans);
  const active = typeof payload.evidence.active_version === "string" ? payload.evidence.active_version : null;
  const activePlan = plans.find((plan) => plan.id === active) ?? null;
  /**
   * THE VERSION A CHANGE IS BUILT FROM — the one in force when there is one, the
   * HEAD when there is not.
   *
   * The plans arrive ordered by `version_number DESC`, so `plans[0]` is the head.
   * Reading `activePlan` alone is the circle amendment 4 of the 2026-08-11 review
   * broke on the Mapping tab: 4 of the 6 live Datastreams have never published,
   * so `current_plan_version_id` is NULL on them and this tab offered nothing at
   * all — publish to edit, edit to have something worth publishing.
   *
   * It is never the version the table below is BROWSING: a change proposed from
   * a superseded version would silently undo everything appended after it, and
   * editing history is what the immutability is for.
   */
  const basePlan = activePlan ?? plans[0] ?? null;
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = plans.find((plan) => text(plan.id) === selectedId) ?? activePlan;
  const mappingVersion = typeof payload.evidence.active_mapping_version === "string"
    ? payload.evidence.active_mapping_version
    : null;

  /** How a version is NAMED in a sentence — its number when it has one, its id
   *  when it does not. A sentence about "dsp_01KZ…" is a sentence nobody reads. */
  const versionLabel = (plan: Record<string, unknown> | null | undefined): string =>
    plan
      ? typeof plan.version_number === "number"
        ? `Version ${plan.version_number}`
        : text(plan.id)
      : "No version";
  const selectedLabel = versionLabel(selected);
  /** The base is the version in force, or the head when nothing is — the same
   *  one `basePlan` above names, and the one the SERVER diffs every proposal
   *  against. Calling it "the active version" on a Datastream with no pointer is
   *  the lie amendment 4 of the 2026-08-11 review removed from the change
   *  dialog; the same care applies to this label. */
  const baseLabel = activePlan ? `${versionLabel(basePlan)} (in force)` : `${versionLabel(basePlan)} (head, nothing in force)`;
  const selectedIsBase = Boolean(selected && basePlan && text(selected.id) === text(basePlan.id));

  // The ledger arrives `version_number DESC`. Sorting reorders THIS list, which
  // is the whole ledger and not a page — the note under the table says so, and
  // it is mounted because `sortRows` cannot know that.
  const { sort, toggleSort } = useTableSort(null);
  const sortedPlans = sortRows(plans, sort, (plan, key): SortValue => {
    if (key === "version") return typeof plan.version_number === "number" ? plan.version_number : null;
    if (key === "executable") return plan.executable === true;
    if (key === "author") return typeof plan.created_by === "string" ? plan.created_by : null;
    if (key === "created") return typeof plan.created_at === "string" ? plan.created_at : null;
    return null;
  });

  return (
    <>
      {/* WHAT IT DOES, FIRST (D-5). The stepper is the first reading, the table
          under it the unfolding. It never disappears: with no plan version at
          all it says so and names the ledger below. */}
      <ProcessingChainPanel
        plan={selected ?? null}
        isActive={selected?.id === active}
        mappingVersion={mappingVersion}
        cleanup={payload.evidence.cleanup_rules}
        links={links}
        onOpenOwner={onOpenOwner}
        onNavigateTab={onNavigateTab}
      />

      <Panel flush>
        <PanelHeader
          title="Immutable processing plan versions"
          description="Filter, deduplication, joins, derivations and grain are pinned to a plan version. Select one to read it in the ordered chain above, and to compare it with the version this Datastream runs on."
          actions={
            <>
              {/* PROPOSE A CHANGE FROM THIS VERSION — added 2026-08-18.
                  The ledger had one door and it always opened on the same
                  payload: an operator could read a superseded version and had no
                  way to act on what they had just read. Seeded with the SELECTED
                  version, this is also the restore gesture — the server's own
                  diff, computed against the version in force, is what makes it
                  legible, and `whatItDoes` below says that before the click. */}
              <DatastreamChangeDialog
                projectId={projectId}
                datastreamId={datastreamId}
                kind="processing"
                initialPayload={record(selected?.normalized_payload)}
                onConfirmed={onConfirmed}
                triggerLabel={
                  selectedIsBase ? "Propose a change…" : "Propose a change from this version…"
                }
                scope={
                  selectedIsBase
                    ? null
                    : `Version ${text(selected?.version_number, text(selected?.id))} is not the one in force. Confirming this proposes its whole contract as the next version, which undoes every change recorded after it. The path-by-path review on the next screen is computed against the version in force and shows exactly that.`
                }
                testId="propose-from-plan-version"
              />
              {/* `Edit raw contract…` IS KEPT, BEHIND A DISCLOSURE — decided
                  2026-08-18.

                  KEPT, because amendment 13 of the 2026-08-11 review ratified
                  exactly this arrangement: « Le raw editor above stays for what
                  no selector covers ; it stops being the only way. »
                  `SourceSelectionPanel` covers the collected fields and nothing
                  else — filters, deduplication, joins, derivations and grain
                  have no control anywhere in the console — so retiring the raw
                  editor would remove the only way to express five of the nine
                  steps the chain above draws.

                  BEHIND A DISCLOSURE, because a free-text JSON contract sitting
                  in the panel header reads as the ordinary way to change a plan,
                  and it is the expert escape hatch. It also stays seeded with
                  the BASE (in force, or the head when nothing is), never with
                  the browsed version: an unguided edit of a superseded contract
                  is the one shape of this gesture that has no review to catch
                  it. */}
              <details className="inline-block">
                <summary className="cursor-pointer text-caption text-text-secondary underline">
                  Advanced
                </summary>
                <div className="mt-2">
                  <DatastreamChangeDialog
                    projectId={projectId}
                    datastreamId={datastreamId}
                    kind="processing"
                    // The version in force when there is one, the HEAD when there
                    // is not — the same base the selector below amends. Handing
                    // this door `activePlan` alone left it disabled on every
                    // Datastream that has never published, which is 4 of the 6
                    // live ones.
                    initialPayload={record(basePlan?.normalized_payload)}
                    onConfirmed={onConfirmed}
                    triggerLabel="Edit raw contract…"
                    testId="raw-processing-change"
                  />
                </div>
              </details>
            </>
          }
        />
        {plans.length === 0 ? (
          <div className="p-5">
            <EmptyState
              title="What this Datastream does to its data is not established"
              description="No processing has been agreed, so what happens to the data after collection — and in what order — is undecided. Open “Advanced” above and use “Edit raw contract…” to write a first one."
            />
          </div>
        ) : (
          <>
            <TableScroll label="Processing plan versions">
              <Table>
                <TableHeader>
                  <TableRow>
                    {/* THE VERSION NUMBER IS THE COLUMN, and the id is under it.
                        `version_number` has been on the wire since the tab was
                        built and the table showed the ULID instead — so two
                        versions of one Datastream were told apart by comparing
                        26 random characters. */}
                    <SortableHead sortKey="version" sort={sort} onSort={toggleSort}>Version</SortableHead>
                    <TableHead>State</TableHead>
                    <SortableHead sortKey="executable" sort={sort} onSort={toggleSort}>Executable</SortableHead>
                    <TableHead>Contract</TableHead>
                    {/* WHO PROPOSED IT. `created_by` was on the wire and on no
                        screen: an immutable ledger of decisions about a person's
                        data named nobody who took them. */}
                    <SortableHead sortKey="author" sort={sort} onSort={toggleSort}>Proposed by</SortableHead>
                    <SortableHead sortKey="created" sort={sort} onSort={toggleSort}>Created</SortableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {sortedPlans.map((plan) => {
                    const id = text(plan.id);
                    return (
                      <TableRow
                        key={id}
                        onClick={() => setSelectedId(id)}
                        aria-selected={id === text(selected?.id)}
                        className="cursor-pointer"
                      >
                        <TableCell>
                          <strong className="text-text">
                            {typeof plan.version_number === "number" ? `Version ${plan.version_number}` : "Unnumbered"}
                          </strong>
                          <span className="block font-mono text-caption text-text-secondary">{id}</span>
                        </TableCell>
                        <TableCell>
                          <Status tone={plan.id === active ? "success" : "neutral"}>
                            {plan.id === active ? "Active" : "Non-live"}
                          </Status>
                        </TableCell>
                        <TableCell>{plan.executable === true ? "Yes" : "No"}</TableCell>
                        <TableCell className="max-w-[16ch] truncate font-mono text-caption" title={text(plan.content_hash)}>
                          {text(plan.content_hash)}
                        </TableCell>
                        <TableCell>{text(plan.created_by, "Not recorded")}</TableCell>
                        <TableCell>
                          <Timestamp value={typeof plan.created_at === "string" ? plan.created_at : null} absentMeaning="No creation time recorded" />
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </TableScroll>

            {/* WHAT THE SELECTED VERSION CHANGES, AND FROM WHAT. The chain above
                READS the selected version; this says how it differs from the one
                the Datastream actually runs on, which is the question a person
                browsing an immutable ledger has. */}
            <div className="grid gap-3 border-t border-divider-base p-5">
              <p className="m-0 text-caption text-text-secondary">{PAGE_SORT_NOTE}</p>
              {selected && basePlan && text(selected.id) !== text(basePlan.id) ? (
                <PlanVersionCompare
                  reference={record(basePlan.normalized_payload)}
                  referenceLabel={baseLabel}
                  selected={record(selected.normalized_payload)}
                  selectedLabel={selectedLabel}
                />
              ) : (
                <Status as="block" tone="neutral" title={`${selectedLabel} is the version this Datastream builds on`}>
                  {active
                    ? "It is the plan in force, so there is nothing to compare it against. Select an earlier version to see what it would change."
                    : "Nothing is in force on this Datastream, so this is the head of the ledger and the base every change is proposed from. Select an earlier version to see what it would change."}
                </Status>
              )}
            </div>
          </>
        )}
      </Panel>

      {/* AI-119: the cadence, the retrieval window and the next run are edited
          HERE -- ratified in datastream-workbench-and-wizard.md, section "Where
          the schedule is edited". Overview keeps cadence as posture only.
          It sits UNDER the chain since D-5: the schedule is when the plan runs,
          which is a parameter of the plan and not the plan. */}
      <SchedulePanel
        projectId={projectId}
        datastreamId={datastreamId}
        mode={mode}
        deliveryChannels={deliveryChannels}
      />

      {/* WHERE THE RAW LANDS — THE ONE PLACE IT IS STATED. Amended 2026-08-18.
          `WorkbenchOutputsPage` drew these two Metrics word for word as well;
          it now names this tab instead. Two tabs answering one question is two
          copies free to diverge, and rank 4 of the ratified order (« Raw zone
          ownership / retention — où atterrit le brut, et pour combien de temps »)
          puts the subject here. */}
      <Panel flush className="grid grid-cols-2 divide-x divide-divider-base max-lg:grid-cols-1 max-lg:divide-x-0 max-lg:divide-y">
        <Metric
          label="Raw Zone Ownership"
          value={payload.evidence.raw_zone_policy === "external_read_only" ? "External Read-Only" : "Managed Raw"}
          hint={payload.evidence.raw_zone_policy === "external_read_only" ? "Client owns the storage" : "toorow manages the storage"}
        />
        {/* `Indefinite` WAS A NUMBER THAT LOOKED OPERATIVE, and it was printed on
            every Datastream in the product. Measured 2026-08-18:

              * `retention_days` is read from `config.destination.retention_days`
                (`datastream_workbench.py:1887` and `:2100`);
              * `$defs/destination` in `server/core/schemas/datastream-intent.
                schema.json` is `{additionalProperties: false, required:
                ["policy"], properties: {policy}}` — so a Datastream intent
                CANNOT carry a retention, and a write of one would be refused by
                the contract before it reached a column;
              * nothing in `server/` writes that key, and nothing reads it to
                purge. (`retention_days` elsewhere in the repository belongs to
                inbound raw imports and to snapshots — two other subjects with
                their own enforced policies.)

            So the value is not "Indefinite as a policy", it is "no policy
            exists here". A person who reads `Indefinite` concludes a decision
            was taken to keep the data for ever; what is true is that the
            question has no owner yet. And there is NO editable control, because
            an editor writing to a contract with no field for the value would be
            a control whose only effect is to be refused.

            RETENTION AS A PRODUCT DECISION IS NOT MADE HERE. This states the
            measurement; it invents nothing. */}
        <Metric
          label="Retention Policy"
          value={payload.evidence.retention_days ? `${String(payload.evidence.retention_days)} days, not enforced` : "Not set"}
          hint={
            payload.evidence.retention_days
              ? "This number is recorded and nothing acts on it: no schedule reads it and no raw extract is purged because of it."
              : "No retention is declared for this Datastream, and nothing purges its raw extracts on a schedule. Deleting collected data is done by erasing the Organization, not from here."
          }
        />
      </Panel>

      {/* AMENDMENT 13 (2026-08-11): « Une dimension s'ajoute depuis un écran, pas
          depuis une zone de texte JSON ». The raw editor above stays for what no
          selector covers; it stops being the only way. The panel is mounted for
          EVERY mode, because a control that silently disappears on a file source
          is the defect amendment 7 named — it answers there instead. */}
      <SourceSelectionPanel
        projectId={projectId}
        datastreamId={datastreamId}
        catalogue={payload.evidence.source_catalogue}
        // AMENDMENT 14: what each collected day was ASKED to carry. The panel
        // never counts a day itself — it intersects this measurement with the
        // fields just ticked.
        history={payload.evidence.dimension_history as DimensionHistory | null}
        basePlan={record(basePlan?.normalized_payload)}
        basePlanId={basePlan ? text(basePlan.id) : null}
        hasActivePlan={active !== null}
        // The seam freezes against a BASE since `14be81ef`: the version in
        // force, or the head of the ledger when nothing is. 6 of the 8 live
        // Datastreams have no pointer, and gating on one disabled the control
        // on every one of them.
        hasBasePlan={basePlan !== null}
        onConfirmed={onConfirmed}
        onNavigateTab={onNavigateTab}
      />

      {/* THE CAPABILITY PROJECTION IS NOT MOUNTED HERE — amendment 3 of the
          2026-08-11 review. `Overview` owns it; this tab owns the execution
          plan. Three identical panels on three tabs of one Datastream is one
          subject drawn three times, and it is what made a reader believe a
          `Country` module applied to a flux with no country field. */}
    </>
  );
}
