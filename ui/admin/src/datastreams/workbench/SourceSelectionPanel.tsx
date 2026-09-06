import { useState } from "react";
import {
  ObjectId,
  Badge, Button, Checkbox, Panel, PanelHeader, Status,
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll,
} from "../../ui";
import { numberText, record, records, text } from "./evidence";
import DatastreamChangeDialog from "./DatastreamChangeDialog";
import { RepullConfirmDialog, repullOutcomeSentence, useRepull } from "./repullDay";
import {
  debtDays, debtSentence, dimensionDebt, recoverableDays,
  type DimensionDebt, type DimensionHistory,
} from "./dimensionDebt";
import type { Tab } from "../../shell/pages/datastreamTabs";

/**
 * WHAT THIS DATASTREAM COLLECTS, chosen from the connector's own manifest.
 *
 * Amendment 13 of `datastream-workbench-and-wizard.md`, ratified 2026-08-11:
 * « Une dimension s'ajoute depuis un écran, pas depuis une zone de texte JSON ».
 * Measured the same day: the only path to `source.selection.dimensions` was
 * `Edit raw contract…`, and the tab COUNTED dimensions — "3 dimension(s)" — while
 * offering none.
 *
 * THE CATALOGUE IS NOT DECIDED HERE. The server projects the module's manifest
 * (`core/datastream_source_catalogue.py`) through the same function the creation
 * wizard reads, so the two surfaces cannot offer two different catalogues for
 * one connector. This file renders what arrives and composes one proposal.
 *
 * NOTHING IS WRITTEN AT THE CLICK. The change leaves through the ratified door —
 * `DatastreamChangeDialog`, prepare then confirm, appending an immutable plan
 * version that the Outputs publication may later make current — exactly as the
 * Mapping tab's exclude, join and split do.
 *
 * AND SINCE AMENDMENT 14, THE ADDITION SAYS WHAT IT OWES THE PAST, BEFORE THE
 * WRITE. « Cette dimension n'existe sur aucun des 60 jours de la fenêtre » is a
 * sentence of the confirmation, not of a discovery three weeks later. The count
 * is the server's (`core/datastream_dimension_history.py`), intersected with the
 * ticked field by `dimensionDebt.ts`, and it is a count of what each day was
 * ASKED for — what a provider actually returned is recorded nowhere in this
 * repository, and the panel renders the server's own sentence saying so rather
 * than a stronger one of its own.
 *
 * THE REPAIR IS THE GESTURE THAT ALREADY EXISTS. The owed days are proposed as
 * ONE re-collection through `repullDay`'s dialog — the same confirmation, the
 * same spend line, the same route as the day grid of story 58.4 — bounded by the
 * window, counted, and never launched without confirmation. And when the
 * provider bound does not reach a day, that is said as an answer: the dimension
 * will never come back on it.
 */

/** A field as the manifest describes it. */
interface CatalogueField {
  field_id: string;
  description: string;
}

/**
 * WHAT THE COLLECTION WRITES ONTO EVERY ROW — Jean, 2026-08-12: « ou par exemple
 * simplement ajouter la date de l'extraction dans le report ».
 *
 * IT IS ALREADY THERE, AND THAT IS THE ANSWER. Measured 2026-08-12: 38 of the 39
 * connector modules stamp `loaded_at` and `pull_id` onto every row they land, 47
 * of the 54 staging models carry them through, and `fact_daily_kpi` selects
 * `MAX(loaded_at)` in each of its 34 blocks — read on a built mart, 6861 rows of
 * 6861 carried one. Nothing had to be added to the data; it had never been shown.
 *
 * SO THERE IS NO CHECKBOX, AND ITS ABSENCE IS THE HONEST PART. A field the pinned
 * report does not declare is refused by the plan validator, so a toggle here would
 * compose a version nothing can execute — the same defect the `exact_bundle`
 * notice above exists to prevent. These rows carry a state and a reason instead.
 *
 * AND IT IS A READING BEFORE IT IS A DECLARATION. The real rows come first, from
 * `fact_daily_kpi` where the row actually is, so a person sees the value on their
 * own data before reading a sentence about it. The instant is NOT recomposed here
 * and no format is chosen in the browser: what the server read is what is shown.
 *
 * THE PROMISE IS THE WEAKER ONE, AND IT IS MEASURED, NOT ASSUMED. On the same mart
 * `count(distinct loaded_at)` equalled `count(distinct pull_id)` for every
 * connector — so a row carries the moment ITS COLLECTION ran, not the moment it
 * was itself read. The server publishes both counts and the sentence that reads
 * them; this file renders that sentence rather than writing a stronger one.
 */
interface CollectionField {
  field_id: string;
  label: string;
  description: string;
  reason: string;
}

interface CollectionRow {
  date: string;
  metric: string;
  loaded_at: string;
  pull_id: string;
}

function collectionFields(value: unknown): CollectionField[] {
  return records(value).map((entry) => ({
    field_id: text(entry.field_id, ""),
    label: text(entry.label, text(entry.field_id, "")),
    description: text(entry.description, ""),
    reason: text(entry.reason, ""),
  })).filter((entry) => entry.field_id !== "");
}

function collectionRows(value: unknown): CollectionRow[] {
  return records(value).map((entry) => ({
    date: text(entry.date, ""),
    metric: text(entry.metric, ""),
    loaded_at: text(entry.loaded_at, ""),
    pull_id: text(entry.pull_id, ""),
  })).filter((entry) => entry.loaded_at !== "");
}

/** The collection's own columns: the rows first, then what they are. */
function CollectionProvenance({ source }: { source: Record<string, unknown> }) {
  const fields = collectionFields(source.collection_fields);
  if (fields.length === 0) return null;
  const observation = record(source.collection_provenance);
  // A key that never arrived and a key that arrived empty are two different
  // failures — one is a server that did not answer, the other is a warehouse that
  // did and had nothing. They are never rendered as one another.
  const observed = observation !== null && text(observation.state) === "observed";
  const rows = observed ? collectionRows(observation?.rows) : [];

  return (
    <div className="border-t border-divider-base" data-testid="collection-fields">
      <PanelHeader
        title="What the collection writes onto every row"
        description="These come from toorow, not from the provider. They are on every row already, so there is nothing here to select."
      />
      {rows.length > 0 ? (
        <TableScroll label="Collected rows">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Day</TableHead>
                <TableHead>Measure</TableHead>
                <TableHead>Collected at</TableHead>
                <TableHead>Collection</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row) => (
                <TableRow key={`${row.pull_id}:${row.date}:${row.metric}`}>
                  <TableCell className="font-mono text-caption">{row.date}</TableCell>
                  <TableCell className="text-ui text-text-secondary">{row.metric}</TableCell>
                  <TableCell className="font-mono text-caption" data-testid="collected-at-value">
                    {row.loaded_at}
                  </TableCell>
                  <TableCell><ObjectId value={row.pull_id} title="Run" /></TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableScroll>
      ) : (
        <div className="px-5 pt-5">
          {/* Never "no row": the server's own sentence, whichever of the four it
              is — nothing collected yet, no mart built, this connector never in
              it, or a warehouse that could not be read. */}
          <Status as="block" tone="neutral" title="No collected row to read this from yet" data-testid="collection-unobserved">
            {text(
              observation?.reason,
              "Whether these columns carry a value on this Datastream's rows could not be read.",
            )}
          </Status>
        </div>
      )}
      {/* WHAT THE INSTANT IS THE INSTANT OF — the server's measured sentence, and
          the difference between "when this row was read" and "when the collection
          that brought it ran". Only the second is true, and it is said here. */}
      {observed && text(observation?.grain_note) !== "" && (
        <div className="px-5 pt-3 text-ui text-text-secondary" data-testid="collection-grain">
          {text(observation?.grain_note)}
        </div>
      )}
      <TableScroll label="Collection-written fields">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Field</TableHead>
              <TableHead>What it is</TableHead>
              <TableHead>State</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {fields.map((field) => (
              <TableRow key={`collection:${field.field_id}`}>
                <TableCell>
                  <div className="grid gap-1">
                    <span className="text-body text-text">{field.label}</span>
                    <ObjectId value={field.field_id} title="Source field name" />
                  </div>
                </TableCell>
                <TableCell className="text-ui text-text-secondary">
                  {field.description} {field.reason}
                </TableCell>
                <TableCell className="text-caption text-text-secondary">
                  <Badge tone="neutral">Always collected</Badge>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableScroll>
    </div>
  );
}

function fieldList(value: unknown): CatalogueField[] {
  return records(value).map((entry) => ({
    field_id: text(entry.field_id, ""),
    description: text(entry.description, ""),
  })).filter((entry) => entry.field_id !== "");
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.map((entry) => String(entry)) : [];
}

/**
 * The proposed plan, identical to the base one but for the two selected lists.
 *
 * THE GRAIN IS NOT REWRITTEN, and that is deliberate. `datastream_intents.
 * _validate_connector` checks `source.selection.grain` against the report's own
 * `supported_grains` as a separate rule, so a screen that derived the grain from
 * the dimensions — as the creation wizard does, where the bundle is complete by
 * construction — would compose an `unsupported_grain` plan on every subset. The
 * grain columns are therefore locked below instead.
 */
export function withSelection(
  plan: Record<string, unknown>,
  next: { metrics: string[]; dimensions: string[] },
): Record<string, unknown> {
  const source = record(plan.source) ?? {};
  const selection = record(source.selection) ?? {};
  return {
    ...plan,
    source: {
      ...source,
      selection: { ...selection, metrics: next.metrics, dimensions: next.dimensions },
    },
  };
}

/** The sentence a removal owes, and it is NOT the mirror of an addition.
 *
 *  Amendment 14 of the same section depends on both halves existing: an added
 *  dimension declares a HISTORY DEBT (the days already collected were pulled
 *  without it), a removed one does not (those rows keep it, the future ones lose
 *  it). No day count is stated here, because none is measured: amendment 14
 *  records that no per-field coverage exists anywhere in the repository, and a
 *  number nobody measured would be the invention this surface forbids. */
const ADDED_DEBT =
  "the days already collected were pulled without it, so no re-reading of the warehouse can add it to them";
const REMOVED_EFFECT =
  "the rows already collected keep it; the ones collected from now on will not carry it";

export default function SourceSelectionPanel({
  projectId,
  datastreamId,
  catalogue,
  history,
  basePlan,
  basePlanId,
  hasActivePlan,
  hasBasePlan,
  onConfirmed,
  onNavigateTab,
}: {
  projectId: string;
  datastreamId: string;
  /** `payload.evidence.source_catalogue`, as the server composed it. */
  catalogue: unknown;
  /** `payload.evidence.dimension_history` — what each collected day was ASKED to
   *  carry, measured through the run that collected it (amendment 14). */
  history?: DimensionHistory | null;
  /** The plan version this proposal is built from: the one in force when there
   *  is one, the head when there is not. */
  basePlan: Record<string, unknown> | null;
  basePlanId: string | null;
  /** Whether a plan version is CURRENT. The prepare seam
   *  (`core/datastream_change.py`, `JOIN … ON p.id=d.current_plan_version_id`)
   *  refuses without one, so the screen says so before the click rather than
   *  after it. */
  hasActivePlan: boolean;
  /**
   * IS THERE A BASE TO CHANGE AT ALL? — the test that replaced `hasActivePlan`
   * on 2026-08-12, once the seam stopped requiring a pointer.
   *
   * `hasActivePlan` was right for one day: `prepare_change` joined the plan on
   * `d.current_plan_version_id` and refused a Datastream with nothing in force,
   * so this panel said so before the click. Commit `14be81ef` moved the seam to
   * a BASE — the version in force, or the head of the ledger when nothing is —
   * and this control was not told. Measured the same day: 6 of the 8 live
   * Datastreams have no plan pointer, so `Prepare plan change` stayed disabled
   * on every one of them and « je ne peux pas simplement ajouter un metric » was
   * exactly that.
   *
   * What may be changed is what the seam accepts: a base. The panel keeps saying
   * that nothing is in force — that is true and it matters — but it no longer
   * turns it into a refusal.
   */
  hasBasePlan?: boolean;
  onConfirmed: () => void;
  onNavigateTab?: (tab: Tab) => void;
}) {
  const source = record(catalogue) ?? {};
  const state = text(source.state, "unavailable");
  const selection = record(record(basePlan?.source)?.selection) ?? {};
  const pinnedMetrics = strings(selection.metrics);
  const pinnedDimensions = strings(selection.dimensions);
  const grain = strings(selection.grain);
  const [metrics, setMetrics] = useState<string[] | null>(null);
  const [dimensions, setDimensions] = useState<string[] | null>(null);
  const [repullOutcome, setRepullOutcome] = useState<string | null>(null);
  // The re-collection is the SAME gesture as the day grid's, hook and dialog
  // included. A second implementation of "ask the provider for these days" is a
  // second confirmation free to say something else about the same spend.
  const repull = useRepull(projectId, datastreamId, (outcome) =>
    setRepullOutcome(repullOutcomeSentence(outcome)),
  );

  const chosenMetrics = metrics ?? pinnedMetrics;
  const chosenDimensions = dimensions ?? pinnedDimensions;
  const catalogueMetrics = fieldList(source.metrics);
  const catalogueDimensions = fieldList(source.dimensions);

  const header = (
    <PanelHeader
      title="What this Datastream collects"
      description="The dimensions and metrics this connector declares. A confirmed change appends an immutable plan version; the version in force does not move."
    />
  );

  // A file source and an external table pull no selection at all. The control
  // STAYS and answers the question instead of vanishing — a selector that
  // silently disappears on 4 of the 6 live Datastreams is the defect amendment 7
  // was written about — and it names the tab where their columns are decided.
  if (state === "no_module") {
    return (
      <Panel flush>
        {header}
        <div className="p-5">
          <Status as="block" tone="neutral" title="Nothing is requested from a provider" data-testid="selection-no-module">
            {text(source.reason, "This Datastream carries no Connector.")} What lands is chosen
            column by column on Mapping, where each one is included, excluded or bound to a concept.
          </Status>
          {onNavigateTab && (
            <div className="pt-3">
              <Button variant="secondary" onClick={() => onNavigateTab("mapping")}>
                Open Mapping
              </Button>
            </div>
          )}
        </div>
      </Panel>
    );
  }

  if (state !== "available") {
    const options = records(source.report_options);
    return (
      <Panel flush>
        {header}
        <div className="p-5">
          <Status as="block" tone="warning" title="This connector's catalogue could not be offered" data-testid="selection-unavailable">
            {text(source.reason, "What this connector declares as pullable is unknown.")}
            {options.length > 0
              ? ` This connector declares ${options.length} report famil${options.length === 1 ? "y" : "ies"}: ${options.map((entry) => text(entry.display_name, text(entry.report_ref, ""))).join(", ")}. Pin one on the plan contract above to bound what can be selected.`
              : " Edit the raw contract above to name a report family this connector declares."}
          </Status>
        </div>
        {/* WHAT THE COLLECTION WRITES IS STILL KNOWN HERE, and hiding it would be
            the amendment-7 defect again: a manifest nobody could read makes the
            PROVIDER's catalogue unknown and leaves this one exactly as certain. */}
        <CollectionProvenance source={source} />
      </Panel>
    );
  }

  const bundled = text(source.selection_mode) === "exact_bundle";
  const addedDimensions = chosenDimensions.filter((field) => !pinnedDimensions.includes(field));
  const removedDimensions = pinnedDimensions.filter((field) => !chosenDimensions.includes(field));
  const addedMetrics = chosenMetrics.filter((field) => !pinnedMetrics.includes(field));
  const removedMetrics = pinnedMetrics.filter((field) => !chosenMetrics.includes(field));
  const dirty =
    addedDimensions.length > 0 || removedDimensions.length > 0
    || addedMetrics.length > 0 || removedMetrics.length > 0;
  // The two refusals the plan validator applies, said BEFORE the click rather
  // than discovered as a non-executable version afterwards.
  const emptyList = chosenMetrics.length === 0 || chosenDimensions.length === 0;

  // AMENDMENT 14. The debt of each ADDED dimension, counted from the days the
  // server resolved. `null` for a field the history could not measure — and a
  // null is rendered as the server's own reason, never as "0 days missing",
  // which is the same string a real measurement of zero would produce.
  const debts: DimensionDebt[] = addedDimensions
    .map((field) => dimensionDebt(history, field))
    .filter((debt): debt is DimensionDebt => debt !== null);
  // The UNION, because one re-collection of a day brings back every dimension
  // the plan then asks for: a day owed by three added dimensions is one day of
  // spend. Summing them would inflate the confirmation's own figure.
  const owedDays = debtDays(debts);
  const owedRecoverable = recoverableDays(debts);
  const beyondRecovery = owedDays.length - owedRecoverable.length;
  const recoveryUnknown = debts.some((debt) => debt.recoveryUnknown);
  const historyState = text(history?.state, "");
  // A measured history that owes nothing still has to be distinguishable from a
  // history that could not be measured: two different silences, two sentences.
  const historyUnmeasured =
    addedDimensions.length > 0 && historyState !== "" && historyState !== "measured";

  const scopeSentence = !dirty
    ? null
    : [
        addedDimensions.length > 0
          ? `${addedDimensions.length} dimension(s) added — ${addedDimensions.join(", ")}: ${ADDED_DEBT}.`
          : null,
        // THE DEBT TRAVELS INTO THE CONFIRMATION ITSELF — « la confirmation dit
        // la dette AVANT d'écrire ». `scope` is what `DatastreamChangeDialog`
        // reads back before the prepare, so the count is on the screen a person
        // approves and not only on the one they composed.
        ...debts.map((debt) => debtSentence(debt, history?.window)),
        historyUnmeasured ? text(history?.reason, "") : null,
        removedDimensions.length > 0
          ? `${removedDimensions.length} dimension(s) removed — ${removedDimensions.join(", ")}: ${REMOVED_EFFECT}.`
          : null,
        addedMetrics.length > 0
          ? `${addedMetrics.length} metric(s) added — ${addedMetrics.join(", ")}: ${ADDED_DEBT}.`
          : null,
        removedMetrics.length > 0
          ? `${removedMetrics.length} metric(s) removed — ${removedMetrics.join(", ")}: ${REMOVED_EFFECT}.`
          : null,
      ]
        .filter(Boolean)
        .join(" ");

  const proposedPayload =
    basePlan && dirty && !emptyList && !bundled
      ? withSelection(basePlan, { metrics: chosenMetrics, dimensions: chosenDimensions })
      : null;

  const toggle = (
    current: string[],
    setter: (next: string[]) => void,
    fieldId: string,
    on: boolean,
  ) => setter(on ? [...current, fieldId] : current.filter((field) => field !== fieldId));

  const rows = (
    kind: "dimension" | "metric",
    fields: CatalogueField[],
    chosen: string[],
    setter: (next: string[]) => void,
  ) =>
    fields.map((field) => {
      // A grain column cannot be dropped: `supported_grains` is validated on its
      // own, so a plan whose grain names a column it no longer collects is a
      // plan nothing can execute. The row says why rather than refusing silently.
      const locked = kind === "dimension" && grain.includes(field.field_id);
      const checked = chosen.includes(field.field_id);
      // Against the list of ITS OWN kind: a connector free to declare one name
      // as both would otherwise report a metric as an added dimension.
      const pinned = (kind === "dimension" ? pinnedDimensions : pinnedMetrics)
        .includes(field.field_id);
      return (
        <TableRow key={`${kind}:${field.field_id}`}>
          <TableCell>
            <label className="flex items-center gap-2 text-body text-text">
              <Checkbox
                checked={checked}
                disabled={bundled || locked}
                onCheckedChange={(next) => toggle(chosen, setter, field.field_id, next === true)}
                aria-label={`${field.field_id} (${kind})`}
                data-testid={`select-${kind}-${field.field_id}`}
              />
              <ObjectId value={field.field_id} title="Source field name" />
            </label>
          </TableCell>
          <TableCell className="text-ui text-text-secondary">{field.description}</TableCell>
          <TableCell className="text-caption text-text-secondary">
            {locked ? (
              <Badge tone="neutral">Establishes the grain</Badge>
            ) : checked && !pinned ? (
              <Badge tone="warning">Being added</Badge>
            ) : !checked && pinned ? (
              <Badge tone="warning">Being removed</Badge>
            ) : checked ? (
              "Collected"
            ) : (
              "Not collected"
            )}
          </TableCell>
        </TableRow>
      );
    });

  return (
    <Panel flush>
      <PanelHeader
        title="What this Datastream collects"
        description={`${text(source.connector_display_name)} · ${text(source.report_display_name)}. A confirmed change appends an immutable plan version; the version in force does not move.`}
        actions={<Badge tone="neutral">{basePlanId ? `Base ${basePlanId}` : "No plan version"}</Badge>}
      />
      {bundled && (
        <div className="px-5 pt-5">
          {/* 115 of the 140 declared reports are `exact_bundle` (measured
              2026-08-12). The validator refuses any selection that is not the
              COMPLETE declared bundle, so offering toggles here would compose a
              plan nothing can execute. The list stays readable, and the sentence
              names the gesture that does add a field. */}
          <Status as="block" tone="neutral" title="This report is pulled as a complete bundle" data-testid="selection-exact-bundle">
            {text(source.report_display_name)} declares an exact bundle: the provider is asked for all
            of these fields or none, so none of them can be dropped and no other field can be added to
            it. A dimension this family does not declare is collected by pinning a report family that
            declares it, on the plan contract above.
          </Status>
        </div>
      )}
      {!hasActivePlan && (
        <div className="px-5 pt-5">
          {/* Measured 2026-08-12 in `core/datastream_change.py`: `prepare_change`
              joins the plan on `d.current_plan_version_id`, so a Datastream with
              no version in force cannot pass through this door at all. Saying it
              before the click is the difference between a refusal a person can
              act on and one they discover after composing a change. */}
          <Status as="block" tone="neutral" title="No version of this plan is in force yet" data-testid="selection-no-active-plan">
            The change below is composed against the most recent recorded version, and confirming it
            appends another. Nothing becomes live until a version is published from Outputs — which
            is a separate, governed act.
          </Status>
        </div>
      )}
      <TableScroll label="Declared dimensions">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Dimension</TableHead>
              <TableHead>What the connector says it is</TableHead>
              <TableHead>State</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>{rows("dimension", catalogueDimensions, chosenDimensions, (next) => setDimensions(next))}</TableBody>
        </Table>
      </TableScroll>
      <TableScroll label="Declared metrics">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Metric</TableHead>
              <TableHead>What the connector says it is</TableHead>
              <TableHead>State</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>{rows("metric", catalogueMetrics, chosenMetrics, (next) => setMetrics(next))}</TableBody>
        </Table>
      </TableScroll>
      {emptyList && dirty && (
        <div className="px-5 pt-5">
          <Status as="block" tone="error" title="A Run needs at least one of each" data-testid="selection-empty-list">
            A plan with no metric or no dimension is refused as an incomplete selection. Keep at least
            one of each, or use the raw contract above for what this selector does not cover.
          </Status>
        </div>
      )}
      {dirty && (
        <div
          className="flex flex-wrap items-center justify-between gap-3 border-t border-divider-base p-5"
          data-testid="selection-review"
        >
          {/* THE TWO FACTS THE CONFIRMATION OWES, named apart. Amendment 14 is
              built on top of this: it attaches the days a newly added dimension
              is missing from, and it needs the addition and the removal to be
              two distinct statements rather than one symmetric diff. */}
          <div className="grid gap-1 text-ui text-text-secondary">
            {addedDimensions.length > 0 && (
              <span data-testid="dimensions-added">
                Adding {addedDimensions.join(", ")}: {ADDED_DEBT}.
              </span>
            )}
            {removedDimensions.length > 0 && (
              <span data-testid="dimensions-removed">
                Removing {removedDimensions.join(", ")}: {REMOVED_EFFECT}.
              </span>
            )}
            {/* AMENDMENT 14: the debt, per added dimension, with the fact that
                established it. One line per dimension rather than one total —
                two dimensions added on the same day can owe two different
                histories, and a single number would hide which. */}
            {debts.map((debt) => (
              <span key={`debt:${debt.field_id}`} data-testid={`dimension-debt-${debt.field_id}`}>
                {debtSentence(debt, history?.window)}
              </span>
            ))}
            {/* An unmeasurable history is stated, never rounded to zero. */}
            {historyUnmeasured && (
              <span data-testid="dimension-debt-unmeasured">
                {text(history?.reason, "The collection history of this Datastream could not be read.")}
              </span>
            )}
            {/* What the measure IS — composed on the server, rendered here, so a
                person reading a day count can see it is a count of what was
                ASKED for and not of what came back. */}
            {debts.length > 0 && (
              <span data-testid="dimension-debt-basis">
                Counted from {text(history?.basis)} — {text(history?.unmeasurable)}.
              </span>
            )}
            {(addedMetrics.length > 0 || removedMetrics.length > 0) && (
              <span data-testid="metrics-changed">
                {addedMetrics.length > 0 ? `Adding ${addedMetrics.join(", ")}. ` : ""}
                {removedMetrics.length > 0 ? `Removing ${removedMetrics.join(", ")}.` : ""}
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant="secondary"
              onClick={() => { setMetrics(null); setDimensions(null); }}
            >
              Discard these changes
            </Button>
            <DatastreamChangeDialog
              projectId={projectId}
              datastreamId={datastreamId}
              kind="processing"
              initialPayload={basePlan}
              proposedPayload={proposedPayload}
              scope={scopeSentence}
              // A BASE, NOT A POINTER. `hasBasePlan` falls back to
              // `hasActivePlan` only for a caller that predates the seam change;
              // a base is what `prepare_change` now freezes against.
              disabled={!proposedPayload || !(hasBasePlan ?? hasActivePlan)}
              triggerLabel="Prepare selection change"
              testId="prepare-selection-change"
              // The local pick is NOT cleared here. Clearing it unmounts this
              // footer — and the dialog with it — the instant the confirmation
              // succeeds, so the one sentence that says the candidate was
              // dispatched disappears before it can be read. The reload the
              // parent starts makes the pick equal to what is pinned, and the
              // footer then leaves on its own, having said what it did.
              onConfirmed={onConfirmed}
            />
          </div>
        </div>
      )}
      {/* THE DEBT IS REPAIRED BY THE GESTURE THAT ALREADY EXISTS — amendment 14,
          « la dette d'une dimension est un ENSEMBLE de ces journées, et elle se
          propose comme telle : bornée, chiffrée, jamais lancée sans
          confirmation ». The union of the owed days, the route of story 58.4,
          and that story's own confirmation with its own spend line. */}
      {owedDays.length > 0 && (
        <div className="grid gap-3 border-t border-divider-base p-5" data-testid="dimension-debt-repair">
          <Status
            as="block"
            tone="neutral"
            title={`${owedDays.length} day(s) would have to be collected again`}
          >
            {/* THE ORDER IS PART OF THE ANSWER, and getting it wrong spends for
                nothing. A re-collection runs under the plan version IN FORCE, so
                a day pulled before this change is published comes back exactly
                as it was — without the dimension. Saying it here is the
                difference between a repair and a wasted quota. */}
            A re-collection asks the provider for a day again under the plan version in force at that
            moment, so publish this change from Outputs first — days collected before it is in force
            come back without {addedDimensions.join(", ")}.
            {recoveryUnknown ? (
              // Measured 2026-08-12: 0 of the 140 declared reports fill
              // `max_provider_backfill_days`. An unknown reach is stated, never
              // rounded to "all of them" — the promise this surface cannot keep.
              <> This connector declares no history bound, so how far back these days can be asked
              for again is not known until the windows run.</>
            ) : beyondRecovery > 0 ? (
              // « Et ce qui ne peut pas être rattrapé se dit. » An answer, not a
              // failure: past the provider's bound the dimension never returns.
              <> {beyondRecovery} of them fall past this provider's {numberText(history?.max_provider_backfill_days)}-day
              bound and will never carry {addedDimensions.join(", ")}; {owedRecoverable.length} can still
              be asked for.</>
            ) : (
              <> All of them fall inside this provider's {numberText(history?.max_provider_backfill_days)}-day bound.</>
            )}
          </Status>
          {repullOutcome && (
            <Status as="block" tone="neutral" title="The re-collection was dispatched" data-testid="dimension-debt-queued">
              {repullOutcome}
            </Status>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="secondary"
              disabled={recoveryUnknown ? owedDays.length === 0 : owedRecoverable.length === 0}
              onClick={() => repull.ask(recoveryUnknown ? owedDays : owedRecoverable)}
              data-testid="dimension-debt-recollect"
            >
              Re-collect {(recoveryUnknown ? owedDays : owedRecoverable).length} day(s)
            </Button>
          </div>
          <RepullConfirmDialog
            open={repull.days !== null}
            onOpenChange={(next) => { if (!next) repull.close(); }}
            days={repull.days ?? []}
            datastreamId={datastreamId}
            connector={text(source.connector_display_name)}
            what={`These days were not asked for ${addedDimensions.join(", ")}`}
            busy={repull.busy}
            error={repull.error}
            onConfirm={() => { void repull.confirm(); }}
          />
        </div>
      )}
      {/* LAST, AND NOT MIXED IN. The two tables above are what the PROVIDER sends
          and what a person chooses among; this one is what toorow writes. Putting
          a collection column into either list is exactly the mistake a person
          must not be able to make — they would read a technical field as one
          their source sent. */}
      <CollectionProvenance source={source} />
    </Panel>
  );
}
