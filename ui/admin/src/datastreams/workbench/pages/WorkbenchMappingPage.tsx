import { useState } from "react";
import {
  Badge, Button, ChoiceGroup, Input, Panel, PanelHeader, Status,
} from "../../../ui";
import { record, records, text } from "../evidence";
import DatastreamChangeDialog from "../DatastreamChangeDialog";
import PublicationReviewModal from "../../../governance/PublicationReviewModal";
import type { WorkbenchHeader, WorkbenchTabPayload } from "../workbenchTypes";
import type { OwnerReference } from "../../../shell/pages/ProjectSettings";
import FileSourceSamplePanel from "../../onboarding/FileSourceSamplePanel";
import ExternalBqVocabularyPanel from "../ExternalBqVocabularyPanel";
import UnresolvedValuesPanel from "../../../governance/UnresolvedValuesPanel";
import LanguageBindingsPanel from "../../../governance/LanguageBindingsPanel";
// What this tab READS off a version and what it PROPOSES back. Extracted so the
// three amendments of 2026-08-11 could land without pushing this file past the
// 1 000-line ceiling; nothing moved changed meaning.
import {
  claimedConcepts, declaredJoins, declaredSplits, FIELD_FILTERS, FILTER_LABEL,
  FILTER_PREDICATE, governedTarget, isExcluded, isGoverned, stringList, TREATMENT_NOT_APPLIED,
  withTreatments, type DraftJoin, type DraftSplit, type FieldFilter,
} from "../mapping/mappingModel";
import { useProjectCanonicalFields } from "../mapping/canonicalFields";
import CanonicalTargetCell from "../mapping/CanonicalTargetCell";
import DeclareConceptPanel from "../mapping/DeclareConceptPanel";
// Le geste que rien ne demandait : epingler l identite partagee, proposee par
// son NOM, avec le croisement qu elle ouvre. Mesure du 2026-08-13 : neuf flux
// publies, zero champ lie au MDM, tous confirmes.
import IdentityCandidatesPanel from "../mapping/IdentityCandidatesPanel";
// Story 71.3: the measure-axis twin of the panel above. A metric bound to the
// MDM, reported by the dimensions bound beside it, derived and offered as a
// governed grain — the fourth fact of `governance.md`.
import MeasurementGrainCandidatesPanel from "../mapping/MeasurementGrainCandidatesPanel";
// D-1 of the 2026-08-12 visual review. Which readings are columns and which are
// a per-row detail is DECIDED FROM THE DATA, here, on every render.
import BindingsTable from "../mapping/BindingsTable";
// The ledger as a HISTORY — version number, author, an order, a comparison and a
// way back that appends instead of rewriting. Amendment of 2026-08-18.
import MappingVersionLedger from "../mapping/MappingVersionLedger";
import {
  ALL_READINGS, foldedReadings, type ReadingContext,
} from "../mapping/bindingReadings";

export default function WorkbenchMappingPage({
  header,
  payload,
  projectId,
  datastreamId,
  onConfirmed,
  onOpenOwner,
  rawImportId,
}: {
  /** Identity, for the one thing this tab cannot read off its own payload: the
   *  mode. A file-source sample only means something for a `managed_feed`. */
  header?: WorkbenchHeader;
  payload: WorkbenchTabPayload;
  projectId: string;
  datastreamId: string;
  onConfirmed: () => void;
  /** The shell's own resolver — amendment 10. A field that names its canonical field has
   *  to LEAD to it, and only the router may build that address. Absent means the
   *  concept is named and inert, never a control that opens nothing. */
  onOpenOwner?: (owner: OwnerReference) => void;
  /** Present when this tab was opened from one retained inbound file. */
  rawImportId?: string | null;
}) {
  const versions = records(payload.evidence.versions);
  const active = typeof payload.evidence.active_version === "string" ? payload.evidence.active_version : null;
  const activeVersion = versions.find((version) => version.id === active) ?? null;
  /**
   * THE NEWEST VERSION, WHICH IS WHAT A DATASTREAM THAT NEVER PUBLISHED HAS —
   * amendment 4 of the 2026-08-11 review.
   *
   * The route serves `ORDER BY version_number DESC`, so this is the head.
   * Nothing here makes it active: `active` stays exactly what
   * `app.datastreams.current_mapping_version_id` says, and every sentence about
   * what is in force keeps reading that field. This is only what the screen
   * OPENS ON, and — below — what it lets a person edit while no pointer exists.
   */
  const newestVersion = versions[0] ?? null;
  // The bindings shown are those of the selected version, the active one by
  // default. AC5 asks for proposal *versus* confirmed state, so a non-live
  // version has to be inspectable without becoming active.
  //
  // FALLING BACK TO THE NEWEST IS NOT COSMETIC. `activeVersion` is `null` on a
  // Datastream that has never published — 4 of the 6 live ones — so this whole
  // panel, its bindings, its triage and its three verbs rendered NOTHING until
  // somebody thought to click a row of the version table above. "Je ne vois
  // nulle part où faire ma jointure" was that, exactly.
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected =
    versions.find((version) => text(version.id) === selectedId) ?? activeVersion ?? newestVersion;
  const mapping = record(selected?.mapping_payload);
  const fields = records(mapping?.fields);
  // `joint_grain` appartient au bundle d'intention, pas a la version de
  // mapping : mesure du 2026-08-12, 0 des 10 versions le portent et 10/10
  // portent `grain`. Le lire ici affichait « Not established » pour toujours.
  const jointGrain = stringList(mapping?.joint_grain ?? mapping?.grain);
  const grain = stringList(mapping?.grain);
  const blockingCount = typeof selected?.blocking_count === "number" ? selected.blocking_count : null;

  // Governed publication of the MAPPING pointer (AI-193). Distinct from the
  // publication offered on Outputs, and verified distinct rather than assumed:
  // `datastream_activation.publish_activate_mutation` advances
  // `current_published_execution_id` — the data candidate — while
  // `governed_publication._advance_pointer` advances `current_mapping_version_id`.
  // Two pointers, two acts, no second owner for either.
  //
  // The header announced "a proposed mapping is waiting" and this screen offered
  // nothing to do about it, because the payload named a VERSION and governed
  // publication consumes a `ready` PROPOSAL. That column now exists
  // (`datastream_workbench.py`), which is what makes the act mountable at all.
  const readyProposal = header?.versions?.ready_mapping_proposal ?? null;
  const [publishing, setPublishing] = useState(false);

  const [fieldFilter, setFieldFilter] = useState<FieldFilter>("all");
  const counts = Object.fromEntries(
    FIELD_FILTERS.map((key) => [key, fields.filter(FILTER_PREDICATE[key]).length]),
  ) as Record<FieldFilter, number>;
  // Selecting a version is not the same act as filtering it, and a filter that
  // survives a version change would show an empty table for a version where that
  // reading has no fields — which reads as "this version has no bindings".
  const effectiveFilter: FieldFilter = counts[fieldFilter] > 0 ? fieldFilter : "all";
  /**
   * FINDING ONE COLUMN AMONG A HUNDRED — amendment of 2026-08-18.
   *
   * The triage answers "which columns stop me"; it has never answered "where is
   * `campaign_name`". A source report brings tens to hundreds of physical
   * fields, and the only way to reach one of them was to read the wall. The
   * search runs over the two readings that are always on the row — the source
   * identity and the concept it becomes — because those are the two words a
   * person arrives holding.
   */
  const [search, setSearch] = useState("");
  const needle = search.trim().toLowerCase();
  const governedCount = fields.filter((field) => !isExcluded(field) && isGoverned(field)).length;
  const landingCount = fields.filter((field) => !isExcluded(field)).length;

  // Story 60.6. WHAT EACH COLUMN BECOMES, in one word, computed by the server.
  //
  // The word is a closed vocabulary shared with the file preview; deriving it a
  // second time here would be two copies free to diverge, which is the defect
  // 59.5 spent a day repairing. An ABSENT key is a read that failed, and it does
  // not render as "this version binds nothing" — the two are separate sentences.
  const columnReadings = Array.isArray(selected?.columns) ? records(selected.columns) : null;
  const readingOf = new Map(columnReadings?.map((row) => [text(row.field_id, ""), row]) ?? []);

  // The pending exclusion gesture: field_id -> "should be excluded".
  // Local until confirmed; the mapping in force is never edited in place.
  const [pending, setPending] = useState<Record<string, boolean>>({});
  /**
   * WHAT MAY BE EDITED — amendment 4 of the 2026-08-11 review.
   *
   * This was `selected?.id === active`, and `active` is
   * `app.datastreams.current_mapping_version_id`, which only a governed
   * publication ever sets. A Datastream that has never published therefore had
   * no editable version at all: no `Inclusion` column, no `Join / split` column,
   * no composer — on 4 of the 6 live Datastreams, measured 2026-08-11. It was a
   * circle: publish to edit, edit to have something worth publishing.
   *
   * So the head is editable when NOTHING is in force. That is the same object
   * `governed_publication` will consume, and editing changes nothing about how
   * it lands: `DatastreamChangeDialog` appends an immutable version exactly as
   * before, and the pointer still moves only through the publication above.
   * A version that is neither active nor the head stays read-only, because
   * editing history is what the immutability is for.
   */
  const editable = selected?.id === active || (active === null && selected?.id === newestVersion?.id);
  const pendingIds = Object.keys(pending);
  const excludingNow = pendingIds.filter((id) => pending[id]);
  const reincludingNow = pendingIds.filter((id) => !pending[id]);

  // The two verbs the mapping version could carry and nothing could WRITE.
  //
  // Until this, `Joined` and `Split into N` were words the table could render
  // from a `column_treatments` key that only the raw JSON contract could
  // compose — the very textarea this screen exists to replace. A reading with no
  // writer is the defect that rejected this story once already.
  const [joinSelection, setJoinSelection] = useState<string[]>([]);
  const [joinTarget, setJoinTarget] = useState("");
  const [joinSeparator, setJoinSeparator] = useState("-");
  const [draftJoins, setDraftJoins] = useState<DraftJoin[]>([]);
  const [splitting, setSplitting] = useState<string | null>(null);
  const [splitPattern, setSplitPattern] = useState("");
  const [splitTargets, setSplitTargets] = useState<Array<{ group: string; target: string }>>([
    { group: "", target: "" },
    { group: "", target: "" },
  ]);
  const [draftSplits, setDraftSplits] = useState<DraftSplit[]>([]);

  /**
   * BINDING A COLUMN TO ITS CONCEPT — amendment 5, the third pending gesture.
   *
   * field_id -> the registry identity picked, or `null` for "no governed
   * target". Local until confirmed, exactly like the exclusion beside it: the
   * mapping in force is never edited in place, and the pick leaves through
   * `Prepare mapping change` with the other decisions of this visit.
   */
  const [pendingTargets, setPendingTargets] = useState<Record<string, string | null>>({});
  const [declaringFor, setDeclaringFor] = useState<string | null>(null);
  const catalog = useProjectCanonicalFields(projectId, editable && fields.length > 0);
  const targetIds = Object.keys(pendingTargets);

  const allJoins = [...declaredJoins(mapping), ...draftJoins];
  const allSplits = [...declaredSplits(mapping), ...draftSplits];
  const claimed = claimedConcepts(mapping, allJoins, allSplits, pendingTargets);
  // Which columns a declared join or split names. They are the rows that must
  // carry the sentence below: a source of a treatment is exactly the column a
  // person expects to stop landing as itself, and it does not.
  const treatedSources = new Set([
    ...allJoins.flatMap((join) => join.sources),
    ...allSplits.map((split) => split.source),
  ]);

  // `catalog.fields` are rows of `app.mdm_canonical_fields`, so what this names
  // is a CANONICAL FIELD, never a Semantic Concept (glossary, 2026-08-14).
  const canonicalFieldName = (id: string) =>
    catalog.fields.find((field) => field.id === id)?.canonical_name || id;
  /** The concept a row SHOWS — the pending pick when there is one, so a search
   *  and an order both read what is on the screen and not what is in the store. */
  const shownTarget = (field: typeof fields[number]) => {
    const id = text(field.field_id, "");
    const bound = id in pendingTargets ? (pendingTargets[id] ?? "") : governedTarget(field);
    return bound ? canonicalFieldName(bound) : "";
  };
  const visibleFields = fields
    .filter(FILTER_PREDICATE[effectiveFilter])
    .filter((field) =>
      needle === ""
      || `${text(field.source_identity ?? field.field_id, "")} ${shownTarget(field)}`
        .toLowerCase()
        .includes(needle),
    );

  /**
   * THE ROWS AS THE TABLE READS THEM, AND WHAT AMONG THEM IS A FOOTNOTE.
   *
   * Recomputed on every render, from the rows actually shown: a triage filter
   * that leaves one blocking column shows every reading again, and declaring a
   * join brings `Treatment` back by itself, because the sentence it puts on the
   * rows it names makes that reading differ. Nothing here is a list of column
   * names — a version where `Role` varies keeps `Role`.
   *
   * A SEARCH NARROWS THE SAME WAY A TRIAGE DOES, and the fold follows it: "the
   * same thing on every row" is a statement about the rows RENDERED, which the
   * ratified amendment of 2026-08-12 spells out.
   */
  const rows: ReadingContext[] = visibleFields.map((field) => {
    const id = text(field.field_id, "");
    return { field, id, reading: readingOf.get(id) ?? null, treated: treatedSources.has(id) };
  });
  /** What a bulk act would actually reach: the rows shown that are not already
   *  where the act would put them. A button whose count includes rows it changes
   *  nothing about is a count that lies about the confirmation to come. */
  const excludedNow = (row: ReadingContext) =>
    row.id in pending ? pending[row.id] : isExcluded(row.field);
  const folded = foldedReadings(rows);
  const bulkExcludable = rows.filter((row) => !excludedNow(row));
  const bulkIncludable = rows.filter((row) => excludedNow(row));
  const foldedLabels = ALL_READINGS.filter((reading) => folded.has(reading.key)).map(
    (reading) => reading.label,
  );
  const dirty =
    pendingIds.length > 0 || targetIds.length > 0 || draftJoins.length > 0 || draftSplits.length > 0;
  const proposedPayload =
    editable && mapping && dirty
      ? withTreatments(mapping, pending, allJoins, allSplits, pendingTargets)
      : null;

  // A join needs at least two distinct sources and an unclaimed target — the
  // same three refusals the server applies, said before the click rather than
  // after it. An excluded column never lands, so it cannot feed a join.
  const joinTargetClaimed = joinTarget.trim() !== "" && claimed.has(joinTarget.trim());
  const canAddJoin =
    joinSelection.length >= 2 && joinTarget.trim() !== "" && !joinTargetClaimed;
  const splitNamed = splitTargets.filter((entry) => entry.group.trim() && entry.target.trim());
  const splitTargetClaimed = splitNamed.some((entry) => claimed.has(entry.target.trim()));
  // ONE grammar, and it is the one that will compile: the pattern is stored in
  // the mapping version and read by `column_treatments` in Python, where a named
  // group is `(?P<name>…)`. The JavaScript spelling `(?<name>…)` is a hard
  // `re.error` there, so a screen that let it through would compose a
  // declaration the append refuses — a control that looks like it worked.
  const splitPatternWrongDialect = /\(\?<[A-Za-z_]/.test(splitPattern);
  const splitGroupsCaptured = splitNamed.every((entry) =>
    splitPattern.includes(`(?P<${entry.group.trim()}>`),
  );
  const canAddSplit =
    splitting !== null &&
    splitPattern.trim() !== "" &&
    !splitPatternWrongDialect &&
    splitGroupsCaptured &&
    new Set(splitNamed.map((entry) => entry.target.trim())).size >= 2 &&
    !splitTargetClaimed;

  const scopeSentence = !dirty
    ? null
    : [
        excludingNow.length > 0
          ? `${excludingNow.length} of ${fields.length} column${fields.length === 1 ? "" : "s"} stop landing: ${excludingNow.join(", ")}.`
          : null,
        reincludingNow.length > 0
          ? `${reincludingNow.length} column${reincludingNow.length === 1 ? " returns" : "s return"} to review: ${reincludingNow.join(", ")}.`
          : null,
        ...draftJoins.map(
          (join) =>
            `${join.sources.length} columns join into ${join.target}: ${join.sources.join(", ")}. They stay listed.`,
        ),
        ...draftSplits.map(
          (split) =>
            `${split.source} splits into ${split.targets.length}: ${split.targets.map((entry) => entry.target).join(", ")}.`,
        ),
        // The canonical field is named, not the identity: a person confirms what
        // they read in the row, and `mdm_6D13WZ…` is not what they read.
        ...targetIds.map((id) =>
          pendingTargets[id]
            ? `${id} binds to ${canonicalFieldName(pendingTargets[id]!)}.`
            : `${id} stops naming a canonical field.`,
        ),
      ]
        .filter(Boolean)
        .join(" ");

  const discardChanges = () => {
    setPending({});
    setPendingTargets({});
    setDeclaringFor(null);
    setDraftJoins([]);
    setDraftSplits([]);
    setJoinSelection([]);
    setJoinTarget("");
    setSplitting(null);
  };

  return (
    <>
      {/* THE LEDGER READS AS A HISTORY — amendment of 2026-08-18. It carried a
          ULID, a state, two counts, two hashes and a date: no version number
          though the server selects one, no author though the same SELECT reads
          it, no way to see what one version decided, and no way back to one.
          `MappingVersionLedger` owns all four, and none of them writes history:
          going back is a PROPOSAL against the base, appended like any other. */}
      <MappingVersionLedger
        versions={versions}
        activeId={active}
        selectedId={text(selected?.id, "") || null}
        onSelect={setSelectedId}
        projectId={projectId}
        datastreamId={datastreamId}
        onConfirmed={onConfirmed}
        rawImportId={rawImportId}
        actions={
          <div className="flex items-center gap-2">
            {readyProposal ? (
              <>
                <Button
                  variant="secondary"
                  data-testid="publish-mapping"
                  onClick={() => setPublishing(true)}
                >
                  Publish this mapping…
                </Button>
                <PublicationReviewModal
                  open={publishing}
                  projectId={projectId}
                  proposalId={readyProposal}
                  onClose={() => setPublishing(false)}
                  onPublished={onConfirmed}
                />
              </>
            ) : null}
            <DatastreamChangeDialog
              projectId={projectId}
              datastreamId={datastreamId}
              kind="mapping"
              // The version in force when there is one, the head when there
              // is not — the same object `editable` lets the controls below
              // change. Handing the raw editor `undefined` on a Datastream
              // that never published made this door open on an empty contract.
              initialPayload={record((activeVersion ?? newestVersion)?.mapping_payload)}
              onConfirmed={onConfirmed}
              triggerLabel="Edit raw contract…"
              testId="raw-mapping-change"
              rawImportId={rawImportId}
            />
          </div>
        }
      />

      <IdentityCandidatesPanel
        evidence={payload.evidence}
        editable={editable}
        pendingTargets={pendingTargets}
        onPin={(targets) => setPendingTargets((current) => ({ ...current, ...targets }))}
      />

      {/* Story 71.3. Beside the identity panel because they are the two axes of
          the same mapping: one pins what two feeds SHARE (a common key), this
          declares what a measure is CUT BY (a grain). Both ride `mdm_target`, and
          this one confirms a governed MDM version rather than preparing a mapping
          change — the grain lives in Governance, not in the mapping payload. */}
      <MeasurementGrainCandidatesPanel
        evidence={payload.evidence}
        editable={editable}
        projectId={projectId}
        datastreamId={datastreamId}
        onConfirmed={onConfirmed}
      />

      {/* AC5: physical bindings, roles, classifications, confidence/evidence,
          inclusion and ambiguities. The compiler already puts every one of them
          in `mapping_payload.fields`; this reads them. */}
      {selected && (
        <Panel flush>
          <PanelHeader
            title={`Physical bindings · ${text(selected.id)}`}
            description={
              selected.id === active
                ? "The bindings in force. Changing one appends a version; it never edits this one."
                // Corrected 2026-08-04: this said the mapping becomes active when the
                // Ready CANDIDATE is published from Outputs. That path advances
                // `current_published_execution_id` — the data — and never this
                // pointer. The mapping pointer moves through governed publication,
                // which is the control above.
                : editable
                  ? "Nothing is in force yet, so this — the most recent version — is what you edit. Changing a binding appends a new version; publishing one is still a separate, governed act."
                  : "A non-live version. The live mapping pointer only moves through a governed publication, from its `ready` proposal."
            }
            actions={
              <div className="flex items-center gap-2">
                <Badge tone={selected.executable === true ? "success" : "error"}>
                  {selected.executable === true ? "Executable" : "Not executable"}
                </Badge>
                {blockingCount !== null && blockingCount > 0 && (
                  <Badge tone="error">{blockingCount} blocking</Badge>
                )}
              </div>
            }
          />
          <div className="grid gap-4 border-b border-divider-base p-5 md:grid-cols-3">
            <div>
              <span className="mb-1 block text-label text-text-secondary">Joint grain</span>
              <span className="font-mono text-ui">
                {jointGrain.length ? jointGrain.join(", ") : "Not established"}
              </span>
            </div>
            <div>
              <span className="mb-1 block text-label text-text-secondary">Declared source grain</span>
              <span className="font-mono text-ui">{grain.length ? grain.join(", ") : "Not established"}</span>
            </div>
            {/* The link to governance and MDM, stated as a number. A field that
                lands with no canonical and no MDM target becomes a column
                nothing else can join or compare against — and no other reading
                on this screen would ever mention it. */}
            <div>
              <span className="mb-1 block text-label text-text-secondary">Governed reach</span>
              <span className="font-numeric text-ui">
                {governedCount} / {landingCount} landing fields reach a canonical or MDM target
              </span>
              {/* AMENDMENT 10: « Une portée énoncée en nombre sans adresse est un
                  compte, pas un lien. » The count stays — it is the triage — and
                  the addresses live where they are useful, one per row. */}
              <span className="mt-1 block text-caption text-text-secondary">
                Each bound field names its concept in its own row, and opens it there.
              </span>
            </div>
          </div>
          {fields.length === 0 ? (
            <div className="p-5">
              <Status as="block" tone="warning" title="No field bindings in this version">
                This version carries no physical field bindings, so nothing can be reviewed field by field.
              </Status>
            </div>
          ) : (
            <>
              {/* The triage, not a decoration: a reading with no fields is
                  disabled rather than hidden, because "0 blocking" is the
                  answer an operator comes here for and an absent chip does not
                  give it. */}
              <div className="border-b border-divider-base p-5">
                <ChoiceGroup
                  aria-label="Field binding triage"
                  value={effectiveFilter}
                  onValueChange={(value) => setFieldFilter(value as FieldFilter)}
                  choices={FIELD_FILTERS.map((key) => ({
                    value: key,
                    label: FILTER_LABEL[key],
                    hint: `${counts[key]} field${counts[key] === 1 ? "" : "s"}`,
                    disabled: key !== "all" && counts[key] === 0,
                  }))}
                />
                {effectiveFilter === "ungoverned" && counts.ungoverned > 0 && (
                  <Status as="block" tone="warning" className="mt-4" title="These fields land ungoverned">
                    They will be written, and nothing in the platform can join, compare or
                    aggregate against them until a canonical or MDM target is bound. Binding
                    one appends a mapping version; it never edits the one in force.
                  </Status>
                )}
                {/* FINDING ONE COLUMN, AND ACTING ON WHAT THE SEARCH LEFT.
                    One question at a time: the triage above answers "which
                    columns stop me", this answers "where is this one", and the
                    acts below apply to exactly the rows both of them left. */}
                <div className="mt-4 flex flex-wrap items-center gap-2">
                  <Input
                    aria-label="Search the source columns and the concepts they become"
                    data-testid="binding-search"
                    placeholder="Search a column or a concept…"
                    value={search}
                    onChange={(event: React.ChangeEvent<HTMLInputElement>) =>
                      setSearch(event.target.value)
                    }
                  />
                  {needle !== "" && (
                    <Button variant="secondary" size="sm" onClick={() => setSearch("")}>
                      Clear the search
                    </Button>
                  )}
                  <span className="text-caption text-text-secondary" data-testid="binding-shown">
                    {rows.length === fields.length
                      ? `All ${fields.length} column${fields.length === 1 ? "" : "s"} shown`
                      : `${rows.length} of ${fields.length} columns shown`}
                  </span>
                </div>
                {/* BULK, AND ONLY WHERE THE CONTRACT ACCEPTS IT. One prepared
                    change carries any number of bindings — `withTreatments`
                    composes them into ONE proposed payload and `prepare_change`
                    freezes that payload whole — so excluding forty columns is
                    one confirmation, not forty.
                    THERE IS DELIBERATELY NO BULK BIND HERE. A concept is claimed
                    by exactly one binding (`claimedConcepts`, and
                    `column_treatments._claim_target` on the server), so binding N
                    columns at once could only work by inventing a target name per
                    column — a second authority over the vocabulary, which
                    `datastream-workbench-and-wizard.md:4299` refuses. The bulk
                    bind that IS legitimate exists above and is the server's own
                    proposal: `IdentityCandidatesPanel` pins every column whose
                    name matches a known identity, in one gesture. */}
                {editable && rows.length > 0 && (
                  <div className="mt-4 flex flex-wrap items-center gap-2" data-testid="binding-bulk">
                    <Button
                      variant="secondary"
                      size="sm"
                      data-testid="bulk-exclude"
                      disabled={bulkExcludable.length === 0}
                      onClick={() =>
                        setPending((current) => {
                          const next = { ...current };
                          for (const row of bulkExcludable) {
                            if (isExcluded(row.field)) delete next[row.id];
                            else next[row.id] = true;
                          }
                          return next;
                        })
                      }
                    >
                      {`Stop these ${bulkExcludable.length} column${bulkExcludable.length === 1 ? "" : "s"} landing`}
                    </Button>
                    <Button
                      variant="secondary"
                      size="sm"
                      data-testid="bulk-include"
                      disabled={bulkIncludable.length === 0}
                      onClick={() =>
                        setPending((current) => {
                          const next = { ...current };
                          for (const row of bulkIncludable) {
                            if (!isExcluded(row.field)) delete next[row.id];
                            else next[row.id] = false;
                          }
                          return next;
                        })
                      }
                    >
                      {`Let these ${bulkIncludable.length} column${bulkIncludable.length === 1 ? "" : "s"} land again`}
                    </Button>
                    <span className="text-caption text-text-secondary">
                      These act on the columns shown above, and nothing is written until the change
                      is prepared and confirmed.
                    </span>
                  </div>
                )}
                {/* WHERE THE FOLDED READINGS WENT — D-1. Said once, naming them,
                    so a person who came for `Sensitivity` knows it is on the
                    screen rather than gone from the product. */}
                {foldedLabels.length > 0 && (
                  <p className="mt-4 mb-0 text-caption text-text-secondary" data-testid="folded-readings">
                    {foldedLabels.join(", ")} read the same on all {rows.length} columns shown, so
                    they are folded into each column&rsquo;s own detail. Open a row to read them.
                  </p>
                )}
              </div>
            {columnReadings === null ? (
              // "Broken" is not "empty". A version that binds no field says so above;
              // this says the per-column reading could not be built, which is a
              // different fact and never a shorter table.
              <div className="p-5">
                <Status as="block" tone="error" title="The column mapping could not be read" data-testid="columns-broken">
                  The bindings of this version are shown, but what each column
                  becomes could not be established. This is not an absence of
                  treatments.
                </Status>
              </div>
            ) : null}
            <BindingsTable
              rows={rows}
              folded={folded}
              editable={editable}
              excludedOf={(row) => (row.id in pending ? pending[row.id] : isExcluded(row.field))}
              joinSelection={joinSelection}
              onToggleExclusion={(row) =>
                setPending((current) => {
                  const next = { ...current };
                  const wanted = !(row.id in pending ? pending[row.id] : isExcluded(row.field));
                  if (wanted === isExcluded(row.field)) delete next[row.id];
                  else next[row.id] = wanted;
                  return next;
                })
              }
              onToggleJoin={(id, wanted) =>
                setJoinSelection((current) =>
                  wanted
                    ? [...current.filter((one) => one !== id), id]
                    : current.filter((one) => one !== id),
                )
              }
              onSplit={(id) => {
                setSplitting(id);
                setSplitPattern("");
                setSplitTargets([
                  { group: "", target: "" },
                  { group: "", target: "" },
                ]);
              }}
              targetCell={(row, excluded) => (
                <CanonicalTargetCell
                  fieldId={row.id}
                  target={row.id in pendingTargets
                    ? (pendingTargets[row.id] ?? "")
                    : governedTarget(row.field)}
                  editable={editable && !excluded}
                  catalog={catalog}
                  claimed={claimed}
                  pending={row.id in pendingTargets}
                  onOpenOwner={onOpenOwner}
                  onSelect={(value) =>
                    setPendingTargets((current) => {
                      const next = { ...current };
                      // Returning a row to exactly what the version already says
                      // is not a change, and a scope sentence naming it would
                      // ask for a confirmation of nothing.
                      if ((value ?? "") === governedTarget(row.field)) delete next[row.id];
                      else next[row.id] = value;
                      return next;
                    })
                  }
                  onDeclare={() => setDeclaringFor(row.id)}
                />
              )}
              // The concept column orders by the NAME the row shows, pending pick
              // included — the identity it is stored under is not what is read.
              targetSortValue={(row) => shownTarget(row.field)}
            />
            {/* THE DOOR THAT MINTS A PROJECT CONCEPT, opened from the row that
                needs it — amendment 5. It was reachable only through a file
                upload whose preview came back `flagged` or `unmatched`. */}
            {editable && declaringFor && (
              <DeclareConceptPanel
                fieldId={declaringFor}
                datastreamId={datastreamId}
                catalog={catalog}
                onCancel={() => setDeclaringFor(null)}
                onDeclared={(field) => {
                  // The concept is born AND binds the column that made it exist:
                  // asking the person to re-pick it would be making them redo the
                  // gesture they just made. The BINDING is still only prepared.
                  setPendingTargets((current) => ({ ...current, [declaringFor]: field.id }));
                  setDeclaringFor(null);
                }}
              />
            )}
            {/* JOIN N COLUMNS INTO ONE CONCEPT. Appears once two columns are
                picked, because a join of one is not a join — the same refusal
                the server states (`treatment_join_needs_two_sources`), said
                before the click. The sources are NOT consumed: they keep their
                row, marked `Joined`, and the panel says so. */}
            {editable && joinSelection.length > 0 && (
              <div className="grid gap-3 border-t border-divider-base p-5" data-testid="join-composer">
                <span className="text-label text-text-secondary">
                  Join {joinSelection.length} column{joinSelection.length === 1 ? "" : "s"} into one canonical field
                </span>
                <span className="font-mono text-caption">{joinSelection.join(" · ")}</span>
                <div className="flex flex-wrap items-center gap-2">
                  <Input
                    aria-label="Canonical field the joined columns produce"
                    data-testid="join-target"
                    placeholder="event_date"
                    value={joinTarget}
                    onChange={(event: React.ChangeEvent<HTMLInputElement>) =>
                      setJoinTarget(event.target.value)
                    }
                  />
                  <Input
                    aria-label="Separator between joined values"
                    data-testid="join-separator"
                    value={joinSeparator}
                    onChange={(event: React.ChangeEvent<HTMLInputElement>) =>
                      setJoinSeparator(event.target.value)
                    }
                  />
                  <Button
                    size="sm"
                    data-testid="join-add"
                    disabled={!canAddJoin}
                    onClick={() => {
                      setDraftJoins((current) => [
                        ...current,
                        {
                          target: joinTarget.trim(),
                          sources: [...joinSelection],
                          separator: joinSeparator,
                        },
                      ]);
                      setJoinSelection([]);
                      setJoinTarget("");
                    }}
                  >
                    Add this join
                  </Button>
                  <Button variant="secondary" size="sm" onClick={() => setJoinSelection([])}>
                    Cancel
                  </Button>
                </div>
                {joinSelection.length < 2 && (
                  <span className="text-caption text-text-secondary" data-testid="join-needs-two">
                    A join reads at least two columns. Pick one more.
                  </span>
                )}
                {joinTargetClaimed && (
                  <Status as="block" tone="error" title="That canonical field is already claimed" data-testid="join-target-claimed">
                    Another binding or treatment already produces {joinTarget.trim()}. Two rules
                    claiming one concept cannot both win, so this is refused here rather than
                    discovered while rows are landing.
                  </Status>
                )}
                <span className="text-caption text-text-secondary">
                  The joined columns stay listed and carry the word <code>Joined</code>. Hiding them
                  would lose the inventory, exactly as hiding an excluded column would.
                </span>
                {/* WHAT THIS DECLARATION DOES, AND WHAT IT DOES NOT. Said in the
                    composer as well as on the rows, from ONE constant, because an
                    amendment that reaches one of its sites and not the others is
                    the fault this surface named three times in a day. */}
                <Status as="block" tone="warning" title="This join is recorded, not applied" data-testid="join-not-applied">
                  {TREATMENT_NOT_APPLIED}
                </Status>
              </div>
            )}

            {/* SPLIT ONE COLUMN INTO N CONCEPTS. One entry, one pattern, N named
                targets — never N rules repeating the same pattern, which is two
                copies free to diverge (arbitrage 6). */}
            {editable && splitting && (
              <div className="grid gap-3 border-t border-divider-base p-5" data-testid="split-composer">
                <span className="text-label text-text-secondary">
                  Split <span className="font-mono">{splitting}</span> into several canonical fields
                </span>
                <Input
                  aria-label="Pattern with named groups"
                  data-testid="split-pattern"
                  placeholder="^(?P&lt;country&gt;[A-Z]{2})_(?P&lt;theme&gt;.+)$"
                  value={splitPattern}
                  onChange={(event: React.ChangeEvent<HTMLInputElement>) =>
                    setSplitPattern(event.target.value)
                  }
                />
                {splitPatternWrongDialect && (
                  <Status as="block" tone="error" title="Name the groups the way the pattern is read" data-testid="split-pattern-dialect">
                    A named group is written <code>(?P&lt;name&gt;…)</code>. The
                    <code> (?&lt;name&gt;…)</code> spelling does not compile where this pattern is
                    applied, so it is refused here rather than at the append.
                  </Status>
                )}
                {!splitPatternWrongDialect && splitNamed.length > 0 && !splitGroupsCaptured && (
                  <span className="text-caption text-text-secondary" data-testid="split-group-missing">
                    Every named concept must sit on a group this pattern actually captures.
                  </span>
                )}
                {splitTargets.map((entry, index) => (
                  <div key={index} className="flex flex-wrap items-center gap-2">
                    <Input
                      aria-label={`Captured group ${index + 1}`}
                      data-testid={`split-group-${index}`}
                      placeholder="country"
                      value={entry.group}
                      onChange={(event: React.ChangeEvent<HTMLInputElement>) =>
                        setSplitTargets((current) =>
                          current.map((one, at) =>
                            at === index ? { ...one, group: event.target.value } : one,
                          ),
                        )
                      }
                    />
                    <Input
                      aria-label={`Canonical field ${index + 1}`}
                      data-testid={`split-target-${index}`}
                      placeholder="country"
                      value={entry.target}
                      onChange={(event: React.ChangeEvent<HTMLInputElement>) =>
                        setSplitTargets((current) =>
                          current.map((one, at) =>
                            at === index ? { ...one, target: event.target.value } : one,
                          ),
                        )
                      }
                    />
                  </div>
                ))}
                <div className="flex flex-wrap items-center gap-2">
                  <Button
                    variant="secondary"
                    size="sm"
                    data-testid="split-add-target"
                    onClick={() =>
                      setSplitTargets((current) => [...current, { group: "", target: "" }])
                    }
                  >
                    Another concept
                  </Button>
                  <Button
                    size="sm"
                    data-testid="split-add"
                    disabled={!canAddSplit}
                    onClick={() => {
                      setDraftSplits((current) => [
                        ...current,
                        {
                          source: splitting,
                          pattern: splitPattern.trim(),
                          targets: splitNamed.map((entry) => ({
                            group: entry.group.trim(),
                            target: entry.target.trim(),
                          })),
                        },
                      ]);
                      setSplitting(null);
                    }}
                  >
                    Add this split
                  </Button>
                  <Button variant="secondary" size="sm" onClick={() => setSplitting(null)}>
                    Cancel
                  </Button>
                </div>
                {new Set(splitNamed.map((entry) => entry.target.trim())).size < 2 && (
                  <span className="text-caption text-text-secondary" data-testid="split-needs-two">
                    A split names at least two distinct concepts, each on a group the pattern
                    captures. One entry carries them all.
                  </span>
                )}
                {splitTargetClaimed && (
                  <Status as="block" tone="error" title="That canonical field is already claimed" data-testid="split-target-claimed">
                    Another binding or treatment already produces one of these concepts.
                  </Status>
                )}
                <Status as="block" tone="warning" title="This split is recorded, not applied" data-testid="split-not-applied">
                  {TREATMENT_NOT_APPLIED}
                </Status>
              </div>
            )}

            {/* The gesture leaves through the ratified door: `Prepare mapping
                change`, then `Review and confirm`, appending an immutable
                version. Nothing is written at the click. */}
            {editable && dirty && (
              <div
                className="flex flex-wrap items-center justify-between gap-3 border-t border-divider-base p-5"
                data-testid="exclusion-review"
              >
                <span className="text-ui text-text-secondary">{scopeSentence}</span>
                <div className="flex items-center gap-2">
                  <Button variant="secondary" onClick={discardChanges}>
                    Discard these changes
                  </Button>
                  <DatastreamChangeDialog
                    projectId={projectId}
                    datastreamId={datastreamId}
                    kind="mapping"
                    initialPayload={mapping}
                    proposedPayload={proposedPayload}
                    scope={scopeSentence}
                    triggerLabel="Prepare mapping change"
                    testId="prepare-exclusion-change"
                    rawImportId={rawImportId}
                    onConfirmed={() => {
                      discardChanges();
                      onConfirmed();
                    }}
                  />
                </div>
              </div>
            )}
            </>
          )}
        </Panel>
      )}

      {/* S1 of `unresolved-values.md` — the values this Datastream carries that
          the reading cannot name. It sits BELOW the field table on purpose
          (:392-395): the value question only exists once the field question is
          answered, because a column that is not mapped to a dimension has no
          values to resolve. Each question narrows the next one.

          It adds no tab, no toggle and no navigation node (:369): the same
          component, reading the same route, is the panel of the `Value Tables`
          lens — so the Workbench and Governance cannot answer two different
          numbers for one Project, which that document refuses outright. */}
      <UnresolvedValuesPanel
        projectId={projectId}
        datastreamId={datastreamId}
        scope="datastream"
      />

      {/* Story 27.8. `core/language_bindings_api.py` mounted the routes and said
          in its own docstring that the screen was named rather than built, here,
          "beside the field table that already binds a column to a canonical
          target". Until 2026-08-22 a client could confirm a binding only with
          `curl`. Like the panel above it adds no tab, no toggle and no
          navigation node: language is not a project capability. The report comes
          from the selected version because a binding is keyed on it — one column
          can carry an observed language in one report and a targeting setting in
          another, and the panel refuses to offer the gesture without it. */}
      <LanguageBindingsPanel
        projectId={projectId}
        connector={header?.identity.connector ?? null}
        reportId={text(selected?.report_id, "") || null}
        columns={fields
          .filter((field) => !isExcluded(field))
          .map((field) => text(field.field_id, ""))
          .filter(Boolean)}
      />

      {/* Story 22.19. Only for a managed feed: a connector pull has no file to
          sample, and offering the control there would describe a chain that
          Datastream does not have. The server decides the rest — a managed feed
          with no template binding gets `file_source: null` and the panel says
          so, rather than rendering an empty review. */}
      {header?.identity.mode === "managed_feed" && (
        <FileSourceSamplePanel
          projectId={projectId}
          datastreamId={datastreamId}
          // THE SAME VERSION THE CONTROLS ABOVE EDIT — amendment 4 of the
          // 2026-08-11 review, finished. The first half of that amendment moved
          // `editable` off the active pointer; this prop was left on it, so on a
          // Datastream that has never published — 4 of the 6 live ones, and all
          // 4 of them managed feeds — `confirm()` still refused with « A pinned
          // mapping version is required before confirmation ». Half a repair
          // reads exactly like the defect it replaced.
          //
          // `active` when there is one, the head when there is not. The confirm
          // route appends a PENDING mapping version either way; it moves no
          // pointer, so handing it the head cannot make anything live.
          mappingVersionId={active ?? (text(newestVersion?.id, "") || null)}
        />
      )}

      {header?.identity.mode === "external_bq" && (
        <ExternalBqVocabularyPanel
          projectId={projectId}
          datastreamId={datastreamId}
        />
      )}

      {/* THE CAPABILITY PROJECTION IS NOT MOUNTED HERE — amendment 3 of the
          2026-08-11 review. It was, and so did `Processing`, and so does
          `Overview`: one subject, three panels titled identically on three tabs
          of one Datastream. `Overview` keeps it, because its subject IS the
          posture; this tab's subject is what each column becomes. */}
    </>
  );
}
