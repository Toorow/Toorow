/**
 * The immutable Result and its six lenses (Story 50.2, AC5-AC14).
 *
 * WHAT THIS SCREEN IS FOR. A figure on its own is not an answer; an answer is a
 * figure you can interrogate. The six lenses are that interrogation, and each one
 * exists because a specific question was being answered by guesswork before:
 *
 *   View        what does this Result say, bounded and disclosed
 *   Data        exactly which rows, which schema, which hashes, which limits
 *   Definitions what those members MEANT at the pinned version
 *   Quality     how fresh, how complete, and what is NOT safe about it
 *   Provenance  which owner chain produced it, and which links are unrecorded
 *   AI Path     which observed AI evidence, or the exact literal `No AI path`
 *
 * THE LENS IS THE ROUTE. Each lens is an anchor carrying a real address, so it is
 * copyable, middle-clickable and survives a refresh. A local `useState` tab would
 * make "send me what you are looking at" impossible, which is most of the point
 * of a workbench built around evidence.
 *
 * WHAT IT REFUSES TO DO:
 *
 *   - Render a chart. `View` is a bounded inspection projection; the Visualization
 *     Spec and its runtime are Stories 50.4-50.5, and a "temporary" chart here
 *     would be the presentation contract they have to unpick.
 *   - Compute anything. No aggregation, no comparison, no timezone conversion, no
 *     semantic sort. Every number displayed is a number the server returned.
 *   - Collapse outcomes. `empty` and `unavailable` render differently, always.
 *   - Fill a missing link. An unrecorded provenance link says it is unrecorded and
 *     names its owner; it never borrows today's bindings to look complete.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { AiPathCapability, formatGovernedValue, TargetedFeedback, type ExactFeedbackRequest, type FeedbackSelection, type GovernedValueMeaning } from "@toorow/card-shell/viz";

import { ApiError, apiGet, apiPut } from "../lib/apiFetch";
import { Button, Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, EmptyState, EvidenceRows, formatPercent, Input, Label, Metric, NativeSelect, NavTabs, ObjectHeader, ObjectId, PageHeader, Panel, PanelHeader, Stack, stateLabel, stateTone, Status, Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll, Textarea, wireWord } from "../ui";
import { governedFieldQueueTarget, openInToorowTarget, ownerTarget, resultTarget, type AnalyzeScope, visualizationBuilderTarget } from "./analyzeTargets";
import { createVisualizationFromResult } from "./builder/seedVisualization";
import StartingPointChoice from "./builder/StartingPointChoice";
import { describeAdditivity, describeCombinationCheck, describeCombinationRefusal, notCombinableMembers } from "./metricCombination";
import { createReportFromResult } from "./saveAsReport";
import ProposeCalculatedFieldDialog from "../governance/ProposeCalculatedFieldDialog";
import { submitExactFeedback } from "./queryClient";
import { DEFAULT_RESULT_LENS, EnvelopeMismatch, fetchResultLens, LENS_LABELS, RESULT_LENSES, type DataLens, type DefinitionsLens, type EntityDetailGaps, type GrainReconciliation, type MetricCombination, type OwnerRef, type ProvenanceLens, type QualityLens, type ResultLens, type ResultLensEnvelope, type ViewLens } from "./workbenchClient";

/** Outcome tone. `unavailable` is a warning, never a success and never an error:
 *  the request was answered, and the answer is "we could not ask". */
/*
 * THE PRIVATE OUTCOME MAP IS GONE (76-2) -- the same three disagreements as
 * `analyze/QueryDoor`, which is exactly why neither screen could see them:
 * `unavailable` neutral, `refused` error, `empty` warning, from the union.
 * `OUTCOME_SENTENCE` below stays: a sentence explaining a run is this screen's
 * copy, not the state's name.
 */
const ANSWER_SELECTION: FeedbackSelection = { target: { kind: "answer" }, label: "Answer" };

const OUTCOME_SENTENCE: Record<string, string> = {
  success: "The query ran on the governed path and returned rows.",
  empty: "The query ran on the governed path and nothing matched. The path is healthy.",
  degraded: "The query returned an answer that is not fully safe. Its limitations are listed.",
  refused: "The query was refused. No answer was produced and none has been substituted.",
  unavailable: "The query could not be asked. This is NOT an empty answer.",
};

type Phase =
  | { status: "loading" }
  | { status: "ready"; envelope: ResultLensEnvelope }
  | { status: "denied" }
  | { status: "mismatch"; message: string }
  | { status: "error"; message: string };

function displayScalar(value: unknown): string {
  if (value === null || value === undefined || value === "") return "Unavailable";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/**
 * ONE RESULT CELL, PRINTED AS WHAT IT MEANS — the same function the MCP App uses.
 *
 * `displayScalar` alone printed `String(cell.value)`, so a money column reached
 * this table as `124000000` while `viz/pivotMatrix` — reading the same Result
 * through the same `value_type` — printed `124.00 EUR`. Console and MCP stated
 * one Result cell two ways, which the surface document names an `Incomplete if`
 * of the 2026-08-15 amendment.
 *
 * The division happens in `formatGovernedValue` and NOWHERE ELSE (one formatter,
 * `viz/theme/canonicalAmount.ts`, imported by both readers). When the Result
 * carries no governed meaning for the column, that function returns the plain
 * number and nothing is inferred; when the value is absent it returns `null`, and
 * the words for an absence stay `displayScalar`'s.
 */
function displayGovernedScalar(
  value: unknown,
  meaning: GovernedValueMeaning | null | undefined,
): string {
  return formatGovernedValue(value, meaning) ?? displayScalar(value);
}

/**
 * A count the server measured, or the word for one it withheld.
 *
 * `String(0)` and `String(null)` are both truthy strings, which is how an
 * `unavailable` Result came to display "Rows returned 0" and "Truncated No" for
 * a query that was never asked. The server now returns `null` for those, and
 * this is the only place that turns a count into text — so a tile cannot print a
 * zero the Result never measured (AC9).
 */
function displayCount(value: number | null | undefined): string {
  return value === null || value === undefined ? "Unavailable" : String(value);
}

function displayFlag(value: boolean | null | undefined): string {
  if (value === null || value === undefined) return "Unavailable";
  return value ? "Yes" : "No";
}

/** An owner reference as a link, or as plain text saying it cannot be opened. */
function OwnerLink({
  scope,
  owner,
  children,
  onOpen,
}: {
  scope: AnalyzeScope;
  owner: OwnerRef | null | undefined;
  children: React.ReactNode;
  onOpen?: (href: string) => void;
}) {
  const href = ownerTarget(scope.organizationId, scope.projectId, owner);
  if (!href) {
    return (
      <span className="text-body text-text-secondary" title="This owner cannot be opened from here">
        {children}
      </span>
    );
  }
  return (
    <a
      className="text-body text-primary underline underline-offset-2 focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus"
      href={href}
      onClick={(event) => {
        if (!onOpen || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        onOpen(href);
      }}
    >
      {children}
    </a>
  );
}

function LimitationList({ limitations }: { limitations: { code: string; message: string }[] }) {
  if (limitations.length === 0) return null;
  return (
    <ul className="mt-2 list-disc pl-5">
      {limitations.map((limitation) => (
        <li key={limitation.code} className="text-body">
          <code className="text-technical">{limitation.code}</code> — {limitation.message}
        </li>
      ))}
    </ul>
  );
}

// ---------------------------------------------------------------------------
// The six lenses.
// ---------------------------------------------------------------------------

function ViewPanel({
  body,
  scope,
  resultId,
  onOpenEvidence,
  onFeedbackTarget,
}: {
  body: ViewLens;
  scope: AnalyzeScope;
  resultId: string;
  onOpenEvidence: (datumKey: string) => void;
  onFeedbackTarget: (selection: FeedbackSelection) => void;
}) {
  return (
    <Stack className="gap-6">
      <Panel className="grid gap-4 md:grid-cols-4" data-testid="result-view-counts">
        <Metric label="Outcome" value={body.outcome} />
        <Metric
          label="Rows returned"
          value={displayCount(body.returned_row_count)}
          hint={
            body.counts_unavailable_reason
              ?? (body.server_row_count !== null
              && body.server_row_count !== body.returned_row_count
                ? `of ${body.server_row_count} recorded on the Result`
                : undefined)
          }
        />
        <Metric
          label="Truncated"
          value={displayFlag(body.truncated)}
          hint={body.truncated ? "The Result is a bounded slice of a larger answer." : undefined}
        />
        <Metric
          label="Freshness"
          value={body.quality_summary.freshness.result_ended_at ?? "Unknown"}
          hint={
            body.quality_summary.freshness.requested_as_of
              ? `as of ${body.quality_summary.freshness.requested_as_of}`
              : undefined
          }
        />
      </Panel>

      {body.quality_summary.limitations.length > 0 ? (
        <Status as="block" tone="warning" title="This answer carries limitations">
          <LimitationList limitations={body.quality_summary.limitations} />
        </Status>
      ) : null}

      {body.values.length === 0 ? (
        <Panel>
          <EmptyState
            title="No value is shown"
            description={
              body.outcome === "unavailable"
                ? "The query could not be asked, so there is nothing to show. Open Quality and Provenance for the exact missing link. No placeholder value has been rendered in its place."
                : "This Result returned no row. Open Data for its manifest."
            }
          />
        </Panel>
      ) : (
        <Panel data-testid="result-view-values">
          <PanelHeader
            title="Returned values"
            description={body.local_interaction_scope}
          />
          <TableScroll label="Returned values">
            <Table>
              <TableHeader>
                <TableRow>
                  {body.fields.map((field) => (
                    <TableHead key={field}>
                      <ColumnMeaning
                        field={field}
                        meaning={body.field_semantics?.[field]}
                      />
                    </TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {body.values.map((row) => (
                  <TableRow key={row.row_index}>
                    {row.cells.map((cell) => (
                      <TableCell key={cell.datum_key}>
                        <span className="flex flex-wrap items-center gap-2">
                          <button
                            type="button"
                            className="text-left underline underline-offset-2 focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus"
                            onClick={() => onOpenEvidence(cell.datum_key)}
                            aria-label={`Evidence for ${cell.field}, row ${row.row_index + 1}`}
                          >
                            {displayGovernedScalar(
                              cell.value,
                              body.field_semantics?.[cell.field],
                            )}
                          </button>
                          <button
                            type="button"
                            className="text-caption underline underline-offset-2"
                            onClick={() => onFeedbackTarget({
                              target: { kind: "datum", row_index: row.row_index, field: cell.field },
                              label: `Row ${row.row_index + 1} · ${cell.field}`,
                            })}
                            aria-label={`Target feedback at row ${row.row_index + 1}, ${cell.field}`}
                          >
                            Target feedback
                          </button>
                        </span>
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
          <p className="mt-3 text-caption text-text-secondary">
            Immutable Result <code className="text-technical">{resultId}</code> in Project{" "}
            <ObjectId value={scope.projectId} title="Project" />.
            {body.read_sliced
              ? ` Showing the first ${body.read_slice_size} rows of this Result's payload.`
              : ""}
          </p>
        </Panel>
      )}
    </Stack>
  );
}

function DataPanel({ body }: { body: DataLens }) {
  return (
    <Stack className="gap-6">
      <Panel className="grid gap-4 md:grid-cols-4" data-testid="result-data-counts">
        <Metric
          label="Rows"
          value={displayCount(body.row_count)}
          hint={body.counts_unavailable_reason ?? undefined}
        />
        <Metric label="Cells" value={displayCount(body.cell_count)} />
        <Metric label="Bytes" value={displayCount(body.byte_count)} />
        <Metric label="Truncated" value={displayFlag(body.truncated)} />
      </Panel>

      <Panel>
        <PanelHeader title="Schema" description="Types as the Result recorded them." />
        <TableScroll label="Result schema">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Field</TableHead>
                <TableHead>Type</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {body.schema.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={2}>This Result declares no field.</TableCell>
                </TableRow>
              ) : (
                body.schema.map((field) => (
                  <TableRow key={field.name}>
                    <TableCell>{field.name}</TableCell>
                    <TableCell>{field.type}</TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </TableScroll>
      </Panel>

      <Panel>
        <PanelHeader
          title="Rows"
          description={
            body.read_sliced
              ? `A bounded read: the first ${body.read_slice_size} rows of this Result's payload.`
              : "Every row this Result carries."
          }
        />
        {body.rows.length === 0 ? (
          <Status as="block" tone="info" title="No row is shown">
            {body.no_rows_explanation ?? "This Result carries no row."}
          </Status>
        ) : (
          <TableScroll label="Result rows">
            <Table>
              <TableHeader>
                <TableRow>
                  {body.schema.map((field) => (
                    <TableHead key={field.name}>{field.name}</TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {body.rows.map((row, index) => (
                  <TableRow key={index}>
                    {body.schema.map((field) => (
                      <TableCell key={field.name}>
                        {displayGovernedScalar(row[field.name], field)}
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        )}
      </Panel>

      <Panel>
        <PanelHeader title="Analytical controls" description="Exactly what was asked." />
        <EvidenceRows label="Query context" source={body.query_context} />
      </Panel>

      <Panel>
        <PanelHeader title="Integrity and manifest" />
        <EvidenceRows label="Integrity" source={body.integrity} />
        <EvidenceRows label="Manifest" source={body.manifest} className="mt-4" />
      </Panel>
    </Stack>
  );
}

/** A measure this Result may not add up is NAMED, never left as a blank.
 *
 * The `Incomplete if` of `analyze-and-test.md:617` — "a refused metric reaches a
 * surface as an absence, with nothing naming the refusal" — is about exactly this
 * silence. The definitions lens has carried `additivity_class` per member since
 * story 60.2 and the console printed it as a raw database word in one table cell,
 * where a reader scanning a total would never look. Here the measures that cannot
 * be combined are stated first, each with the gesture that repairs it.
 *
 * SILENT WHEN THERE IS NOTHING TO SAY. A Result whose every measure is additive
 * renders no panel at all — a reassurance repeated on every screen is noise, and
 * noise is what made the one case that mattered invisible.
 */
function NotCombinablePanel({ members }: { members: DefinitionsLens["members"] }) {
  const named = notCombinableMembers(members);
  if (named.length === 0) return null;
  return (
    <Panel>
      <PanelHeader
        title="Measures this Result does not add up"
        description="Named here rather than left blank where a total would be."
      />
      <Stack className="gap-3">
        {named.map(({ member, explained }) => (
          <Status
            as="block"
            key={`${member.concept_id}:${member.version_id}`}
            tone="warning"
            title={`${member.label ?? "Unresolved member"} — ${explained.title}`}
          >
            {explained.sentence}
            {explained.gesture ? (
              <div className="mt-2 text-body">{explained.gesture}</div>
            ) : null}
          </Status>
        ))}
      </Stack>
    </Panel>
  );
}

function DefinitionsPanel({
  body,
  scope,
  onOpen,
}: {
  body: DefinitionsLens;
  scope: AnalyzeScope;
  onOpen?: (href: string) => void;
}) {
  return (
    <Stack className="gap-6">
      <Panel>
        <PanelHeader
          title="Semantic View"
          description="The exact published version this Result was answered from."
        />
        <p className="text-body">
          <OwnerLink scope={scope} owner={body.semantic_view.owner_ref} onOpen={onOpen}>
            {body.semantic_view.label ?? body.semantic_view.id}
          </OwnerLink>{" "}
          — version{" "}
          <ObjectId value={body.semantic_view.version_id} title="Version" />
          {body.semantic_view.version_number !== null
            ? ` (v${body.semantic_view.version_number})`
            : ""}
        </p>
        {!body.semantic_view.resolved ? (
          <Status as="block" tone="warning" title="This version could not be read" className="mt-3">
            The pinned Semantic View version is not readable here. The current version has NOT been
            shown in its place.
          </Status>
        ) : null}
      </Panel>

      <NotCombinablePanel members={body.members} />

      <Panel>
        <PanelHeader title="Members" description="Meaning as pinned, not as it is today." />
        <TableScroll label="Member definitions">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Member</TableHead>
                <TableHead>Kind</TableHead>
                <TableHead>Definition</TableHead>
                <TableHead>Aggregation</TableHead>
                <TableHead>Additivity</TableHead>
                <TableHead>Unit</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {body.members.map((member) => (
                <TableRow key={`${member.concept_id}:${member.version_id}`}>
                  {/* THE WORD, THEN THE ADDRESS — and the address is already on
                      the second line, marked technical. `_lens_definitions`
                      serves `label` off the PINNED concept version and answers
                      `resolved: false` with a reason when that version cannot be
                      read, so `?? member.concept_id` printed `sc_<ULID>` twice
                      in one cell and said nothing about WHY the word was
                      missing. The state is named here; the reason keeps its own
                      column. */}
                  <TableCell>
                    <OwnerLink scope={scope} owner={member.owner_ref} onOpen={onOpen}>
                      {member.label ?? "Unresolved member"}
                    </OwnerLink>
                    <div className="text-technical text-text-secondary"><ObjectId value={member.version_id} title="Version" /></div>
                  </TableCell>
                  <TableCell>{wireWord(member.kind)}</TableCell>
                  <TableCell>
                    {member.resolved
                      ? (member.definition ?? "Unavailable")
                      : (member.unresolved_reason ?? "Unavailable")}
                  </TableCell>
                  <TableCell>{member.aggregation ?? "Unavailable"}</TableCell>
                  <TableCell>
                    {/* THE DATABASE WORD USED TO LAND HERE. `non_additive` printed
                        raw told a reader nothing they could act on, and told the
                        one reader it mattered to the least. */}
                    {describeAdditivity(member.additivity_class)?.title ?? "Unavailable"}
                  </TableCell>
                  <TableCell>{member.unit ?? "Unavailable"}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableScroll>
      </Panel>

      <Panel>
        <PanelHeader title="Classification pins" />
        {body.classification_pins.length === 0 ? (
          <p className="text-body text-text-secondary">
            This Query Spec pins no governed classification.
          </p>
        ) : (
          <ul className="grid gap-2">
            {body.classification_pins.map((pin) => (
              <li key={pin.classification_object_id} className="text-body">
                <OwnerLink scope={scope} owner={pin.owner_ref} onOpen={onOpen}>
                  <ObjectId value={pin.classification_object_id} title="Classification object" />
                </OwnerLink>{" "}
                on <ObjectId value={pin.member_id} title="Member" />, hierarchy version{" "}
                <code className="text-technical">{pin.hierarchy_version_id ?? "Unavailable"}</code>
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <Panel>
        <PanelHeader title="Period comparison" />
        {/* WHAT RAN, NOT WHAT WAS ASKED. This panel used to print a reporting
            boundary and a time zone the read never applied, beside a sentence
            describing a comparison `build_sql` did not perform. Both controls are
            now refused at the door, and the comparison states the two windows it
            actually labelled — or says plainly that it was not executed. */}
        <EvidenceRows
          label="Semantics"
          source={{
            source_comparison: body.source_comparison.requested,
            executed: body.source_comparison.executed,
            current_window: body.source_comparison.current_window
              ? `${body.source_comparison.current_window.start} to ${body.source_comparison.current_window.end}`
              : null,
            baseline_window: body.source_comparison.baseline_window
              ? `${body.source_comparison.baseline_window.start} to ${body.source_comparison.baseline_window.end}`
              : null,
            comparison_meaning: body.source_comparison.meaning,
          }}
        />
      </Panel>
    </Stack>
  );
}

/**
 * CHANTIER B -- how far this breakdown is from the total its measure declares.
 *
 * IT SHOWS, IT DOES NOT CLOSE. Neither figure is scaled, capped or back-filled
 * so the two agree: the panel prints the total, the breakdown's own sum and
 * the difference between them, and then says which of five things that
 * difference is. A product that quietly aligned them would be lying about one of
 * the two, and which one is not even knowable.
 *
 * THE FIVE STATES DO NOT COLLAPSE. `undeclared` is not `expected` -- a gap
 * nobody has explained must never read like one somebody has -- and
 * `unavailable` is not a gap of zero. `null` (the comparison failed) is not an
 * empty list (there was nothing to compare).
 *
 * It computes nothing, like the rest of this workbench: every number here is a
 * number `core.metric_grain` returned at execution time.
 */
function GrainReconciliationPanel({
  entries,
  scope,
}: {
  entries?: GrainReconciliation[] | null;
  scope: AnalyzeScope;
}) {
  if (entries === undefined) return null;
  if (entries === null) {
    return (
      <Panel>
        <PanelHeader title="Against the declared total" />
        <Status as="block" tone="warning" title="This comparison could not be made">
          The Result stands; what is missing is its distance from the total. Re-run
          the query to obtain it.
        </Status>
      </Panel>
    );
  }
  if (entries.length === 0) return null;

  const tone = (verdict: GrainReconciliation["verdict"]) =>
    verdict === "unexplained" || verdict === "undeclared"
      ? ("warning" as const)
      : verdict === "unavailable"
        ? ("info" as const)
        : ("success" as const);
  const title = (verdict: GrainReconciliation["verdict"]) =>
    ({
      expected: "This difference was declared",
      reconciled: "This breakdown reconstitutes the total",
      unexplained: "This breakdown was declared to reconstitute the total, and does not",
      undeclared: "Nobody has explained this difference",
      unavailable: "The declared total could not be produced",
    })[verdict];

  return (
    <Panel>
      <PanelHeader
        title="Against the declared total"
        description="Shown, never closed. Neither figure is adjusted so that the two agree."
      />
      <Stack className="gap-4">
        {entries.map((entry) => (
          <div key={entry.measure_id} className="grid gap-3">
            <div className="grid gap-4 md:grid-cols-3">
              {/* `query_execution` resolves `measure_name` off the concept
                  versions at execution time and serves `None` when it could not.
                  A tile headed "Declared total — sc_<ULID>" names no measure at
                  all, and this panel exists precisely to say which measure the
                  two figures disagree about. */}
              <Metric
                label={`Declared total — ${entry.measure_name ?? "Unresolved measure"}`}
                value={displayScalar(entry.total)}
              />
              <Metric label="This Result" value={displayScalar(entry.breakdown_sum)} />
              <Metric
                label="Difference"
                value={displayScalar(entry.gap)}
                hint={
                  entry.gap_ratio === null || entry.gap_ratio === undefined
                    ? undefined
                    : `${formatPercent(entry.gap_ratio)} of the total`
                }
              />
            </div>
            <Status as="block" tone={tone(entry.verdict)} title={title(entry.verdict)}>
              {entry.statement}
            </Status>
            <DeclareBreakdown entry={entry} scope={scope} />
          </div>
        ))}
      </Stack>
    </Panel>
  );
}

/**
 * The gesture, where the gap is seen.
 *
 * `analyze-and-test.md` is explicit that Governance owns the reconciliation
 * method, and it still does -- this writes to the same governed endpoint and
 * authors nothing of its own. What it refuses to do is send a person somewhere
 * else for the one action this panel just asked of them: a screen that names a
 * gesture and cannot perform it is unfinished.
 *
 * IT DOES NOT RE-OPEN THIS RESULT, and says so. The Result is immutable and
 * carries the comparison as it stood when it ran; a declaration made now governs
 * the NEXT one. Letting the panel repaint itself would quietly rewrite a frozen
 * answer, which is the whole thing an immutable Result exists to prevent.
 */
function DeclareBreakdown({
  entry,
  scope,
}: {
  entry: GrainReconciliation;
  scope: AnalyzeScope;
}) {
  const [open, setOpen] = useState(false);
  const [sumsTo, setSumsTo] = useState<"equals" | "partial_by_design">(
    entry.sums_to ?? "partial_by_design",
  );
  const [reason, setReason] = useState(entry.reason ?? "");
  const [state, setState] = useState<
    { kind: "idle" } | { kind: "saving" } | { kind: "saved" } | { kind: "error"; message: string }
  >({ kind: "idle" });

  if (entry.verdict === "unavailable") return null;

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setState({ kind: "saving" });
    try {
      await apiPut(
        `/api/projects/${encodeURIComponent(scope.projectId)}/governance/metric-grain/` +
          `${encodeURIComponent(entry.measure_id)}/breakdowns/` +
          `${encodeURIComponent(entry.breakdown_datastream_id)}`,
        { sums_to: sumsTo, reason: sumsTo === "partial_by_design" ? reason : null },
      );
      setState({ kind: "saved" });
    } catch (error) {
      // The server's sentence, not ours: it names the gesture, and repeating it
      // in our own words is how two screens end up refusing differently.
      setState({
        kind: "error",
        message:
          error instanceof ApiError
            ? error.message
            : "The declaration could not be saved. Try again.",
      });
    }
  };

  if (!open) {
    return (
      <Button type="button" variant="secondary" onClick={() => setOpen(true)}>
        {entry.sums_to
          ? "Change what this Datastream sums to"
          : "State what this Datastream sums to"}
      </Button>
    );
  }

  return (
    <form className="grid gap-2" onSubmit={submit}>
      <Label htmlFor={`sums-to-${entry.measure_id}`}>
        Against the total, this Datastream
      </Label>
      <NativeSelect
        id={`sums-to-${entry.measure_id}`}
        value={sumsTo}
        onChange={(event: React.ChangeEvent<HTMLSelectElement>) =>
          setSumsTo(event.target.value === "equals" ? "equals" : "partial_by_design")
        }
      >
        <option value="partial_by_design">carries a share of it, by design</option>
        <option value="equals">reconstitutes it exactly</option>
      </NativeSelect>
      {sumsTo === "partial_by_design" ? (
        <>
          <Label htmlFor={`reason-${entry.measure_id}`}>What it does not contain</Label>
          <Textarea
            id={`reason-${entry.measure_id}`}
            value={reason}
            rows={3}
            onChange={(event) => setReason(event.target.value)}
            placeholder="The source withholds rows below its privacy thresholds."
          />
        </>
      ) : null}
      <div className="flex gap-2">
        <Button type="submit" disabled={state.kind === "saving"}>
          {state.kind === "saving" ? "Saving…" : "Declare"}
        </Button>
        <Button type="button" variant="secondary" onClick={() => setOpen(false)}>
          Cancel
        </Button>
      </div>
      {state.kind === "error" ? (
        <Status as="block" tone="error" title="This declaration was refused">
          {state.message}
        </Status>
      ) : null}
      {state.kind === "saved" ? (
        <Status as="block" tone="success" title="Declared">
          This Result keeps the comparison it was born with; the declaration governs
          the next one. Re-run the query to read it under this reason.
        </Status>
      ) : null}
    </form>
  );
}

/**
 * Chantier C -- which of the entities this Result measures carry no detail.
 *
 * ASKED, NOT PAID FOR ON EVERY OPEN. The inventory costs a DISTINCT over a
 * published relation, so folding it into every Result read would make looking at
 * a figure expensive in order to punish nobody. The panel offers the question and
 * the person asks it.
 *
 * `unavailable` IS NOT `0 missing`. A relation that could not be read closes no
 * case; it says so, and claims no coverage. That distinction is the whole reason
 * this inventory is worth having: an incomplete context you can SEE is
 * improvable, one that reports itself complete is not.
 */
function EntityDetailGapsPanel({
  body,
  scope,
}: {
  body: QualityLens;
  scope: AnalyzeScope;
}) {
  const [state, setState] = useState<
    | { kind: "idle" }
    | { kind: "loading" }
    | { kind: "ready"; gaps: EntityDetailGaps }
    | { kind: "error"; message: string }
  >({ kind: "idle" });

  const members = body.entity_gap_candidates ?? [];
  if (members.length === 0) return null;

  const ask = async (memberId: string) => {
    setState({ kind: "loading" });
    try {
      const gaps = await apiGet<EntityDetailGaps>(
        `/api/projects/${encodeURIComponent(scope.projectId)}/analyze/entity-gaps` +
          `?semantic_view_version_id=${encodeURIComponent(body.semantic_view_version_id ?? "")}` +
          `&member_id=${encodeURIComponent(memberId)}`,
      );
      setState({ kind: "ready", gaps });
    } catch (error) {
      setState({
        kind: "error",
        message: error instanceof Error ? error.message : "The request failed.",
      });
    }
  };

  return (
    <Panel>
      <PanelHeader
        title="What is measured here, and what is named"
        description="Asked on demand: it reads every distinct value this Result groups by."
      />
      <Stack className="gap-4">
        {state.kind === "idle" || state.kind === "loading" ? (
          <div className="flex flex-wrap gap-2">
            {members.map((member) => (
              <Button
                key={member.member_id}
                type="button"
                variant="secondary"
                disabled={state.kind === "loading"}
                onClick={() => void ask(member.member_id)}
              >
                {state.kind === "loading"
                  ? "Reading…"
                  : `Which ${member.member_name} carry no detail?`}
              </Button>
            ))}
          </div>
        ) : null}
        {state.kind === "error" ? (
          <Status as="block" tone="error" title="This inventory could not be read">
            {state.message} Nothing is claimed about what is missing.
          </Status>
        ) : null}
        {state.kind === "ready" && state.gaps.state === "unavailable" ? (
          <Status as="block" tone="warning" title="This inventory could not be taken">
            {state.gaps.unavailable_reason ?? "The relation could not be read."} No
            coverage is claimed.
          </Status>
        ) : null}
        {state.kind === "ready" && state.gaps.state !== "unavailable" ? (
          <>
            <div className="grid gap-4 md:grid-cols-3">
              <Metric label="Measured" value={displayScalar(state.gaps.observed_count)} />
              <Metric label="Named" value={displayScalar(state.gaps.detailed_count)} />
              <Metric label="Without detail" value={displayScalar(state.gaps.missing_count)} />
            </div>
            {state.gaps.next_gesture ? (
              <Status
                as="block"
                tone={state.gaps.missing_count ? "warning" : "info"}
                title="What would fill this"
              >
                {state.gaps.next_gesture}
              </Status>
            ) : (
              <Status as="block" tone="success" title="Every entity measured here is named">
                Nothing is missing for this dimension.
              </Status>
            )}
            {state.gaps.missing.length > 0 ? (
              <EvidenceRows
                label="Without detail"
                source={{
                  keys: state.gaps.missing.join(", "),
                  listed: state.gaps.missing_truncated
                    ? `${state.gaps.missing.length} of ${state.gaps.missing_count}`
                    : String(state.gaps.missing.length),
                }}
              />
            ) : null}
          </>
        ) : null}
      </Stack>
    </Panel>
  );
}

/** Was each total of this Result checked before it was combined — and refused?
 *
 * WHAT REACHED THIS SCREEN BEFORE, AND WHAT DID NOT. The server has stated the
 * gate's verdict since story 53.2, on `data.metrics[].combination_check` and
 * `data.metrics_not_combinable`. Measured 2026-08-17: the only REST route
 * carrying either is `GET /api/cards`, and `ui/admin/src` never fetches it —
 * only `/api/cards/templates`, from the catalog page, which draws no figure. The
 * report envelope that carries `metrics_not_combinable` has no REST route at all.
 * So a measure the gate refused arrived here as a hole, which reads exactly like
 * a measure nobody asked for. `quality.metric_combination` is that verdict, on
 * the one route this screen already fetches.
 *
 * NOT EVERY LINE, ONLY THE ONES THAT COST SOMETHING. `single_source` and
 * `verified` are the reassuring answers, and printing them for every measure of
 * every Result is the noise that hides the one line that matters. They are
 * summarised in a single count instead. What gets its own block: a REFUSAL, a
 * total nobody checked (`not_requested`), a check that could not answer
 * (`unavailable`), and a measure whose source count this Result never recorded.
 *
 * EVERY WORD COMES FROM `./metricCombination`. This component composes; it does
 * not phrase. A status this console does not know yields `null` from the
 * describers, and the raw status is then shown rather than a reassuring sentence
 * invented for it.
 */
function MetricCombinationPanel({ entries }: { entries?: MetricCombination[] }) {
  if (!entries || entries.length === 0) return null;

  const notable = entries.filter(
    (entry) => entry.refused !== null || entry.check === null || entry.check === "not_requested"
      || entry.check === "unavailable",
  );
  const settled = entries.length - notable.length;

  if (notable.length === 0) {
    return (
      <Panel data-testid="result-quality-combination">
        <PanelHeader
          title="Combining across sources"
          description="Every measure of this Result was checked before it was totalled."
        />
        <p className="text-body text-text-secondary">
          {settled} of {entries.length} measures carry a settled verdict from the reconciliation
          gate. No total here was published without it.
        </p>
      </Panel>
    );
  }

  return (
    <Panel data-testid="result-quality-combination">
      <PanelHeader
        title="Combining across sources"
        description="A refused or unchecked total is named here rather than left as a gap."
      />
      <Stack className="gap-3">
        {notable.map((entry) => {
          // Two served words and no third. `_lens_quality` carries both `label`
          // and `member_name` off the concept versions; when neither is
          // readable the panel says so, because "sc_<ULID> cannot be combined
          // across sources" tells a person nothing they can act on.
          const name = entry.label ?? entry.member_name ?? "Unresolved member";
          // The refusal is the stronger fact and wins the heading: `check` on a
          // refused entry only restates it as "refused".
          const explained = entry.refused
            ? describeCombinationRefusal(entry.refused)
            : describeCombinationCheck(entry.check);
          const rawStatus = entry.refused ?? entry.check;
          return (
            <Status
              as="block"
              key={entry.member_id}
              tone="warning"
              title={`${name} — ${explained?.title ?? rawStatus ?? "Could not be asked"}`}
              data-testid={`combination-${entry.member_id}`}
            >
              {explained?.sentence ?? entry.unavailable_reason ?? rawStatus}
              {explained && entry.unavailable_reason ? (
                <div className="mt-2 text-body">{entry.unavailable_reason}</div>
              ) : null}
              {entry.source_systems.length > 0 ? (
                <div className="mt-2 text-body text-text-secondary">
                  Sources behind it: {entry.source_systems.join(", ")}.
                </div>
              ) : null}
              {explained?.gesture ? (
                <div className="mt-2 text-body">{explained.gesture}</div>
              ) : null}
            </Status>
          );
        })}
      </Stack>
      {settled > 0 ? (
        <p className="mt-3 text-body text-text-secondary">
          The other {settled} of this Result&apos;s {entries.length} measures carry a settled
          verdict.
        </p>
      ) : null}
    </Panel>
  );
}

function QualityPanel({
  body,
  scope,
  onOpen,
}: {
  body: QualityLens;
  scope: AnalyzeScope;
  onOpen?: (href: string) => void;
}) {
  return (
    <Stack className="gap-6">
      <Panel className="grid gap-4 md:grid-cols-4" data-testid="result-quality-counts">
        <Metric label="Outcome" value={body.outcome} />
        <Metric label="Freshness" value={body.freshness.result_ended_at ?? "Unknown"} />
        <Metric
          label="Rows"
          value={displayCount(body.completeness.row_count)}
          hint={body.completeness.counts_unavailable_reason ?? undefined}
        />
        <Metric label="Truncated" value={displayFlag(body.completeness.truncated)} />
      </Panel>

      {body.unavailable_reason ? (
        <Status as="block" tone="warning" title="This query could not be asked">
          {body.unavailable_reason}
          {body.missing_link ? (
            <>
              {" "}
              Missing link: <code className="text-technical">{body.missing_link}</code>.
            </>
          ) : null}
        </Status>
      ) : null}
      {body.refused_reason ? (
        <Status as="block" tone="warning" title="This query was refused">
          {body.refused_reason}
        </Status>
      ) : null}
      {body.degraded_reason ? (
        <Status as="block" tone="warning" title="This answer is degraded">
          {body.degraded_reason}
        </Status>
      ) : null}

      <MetricCombinationPanel entries={body.metric_combination} />

      <GrainReconciliationPanel entries={body.grain_reconciliation} scope={scope} />

      <EntityDetailGapsPanel body={body} scope={scope} />

      <Panel>
        <PanelHeader title="Limitations" />
        {body.limitations.length === 0 ? (
          <p className="text-body text-text-secondary">
            This Result records no limitation. That is not a claim of completeness: see Provenance
            for the links this Result does and does not carry.
          </p>
        ) : (
          <LimitationList limitations={body.limitations} />
        )}
      </Panel>

      {body.analytical_paths ? (
        <Panel>
          <PanelHeader
            title="Another Analyze surface reads a different relation"
            description="Disclosed, not reconciled. Neither path resolves against the other."
          />
          <Status as="block" tone="warning" title="The same question can answer differently in chat">
            {body.analytical_paths.this_path.note}{" "}
            {body.analytical_paths.other_path.note}
          </Status>
        </Panel>
      ) : null}

      <Panel>
        <PanelHeader
          title="Data Quality evaluations"
          description="Referenced from their owner. Analyze authors no DQ verdict."
        />
        {body.dq_evaluations.length === 0 ? (
          <Status as="block" tone="info" title="No DQ evaluation is attached to this Result">
            {body.dq_unavailable_reason
              ?? "This Result references no DQ evaluation, and none has been attached in its place."}
          </Status>
        ) : (
          <TableScroll label="DQ evaluations">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Monitor</TableHead>
                  <TableHead>Outcome</TableHead>
                  <TableHead>Evaluated</TableHead>
                  <TableHead>Failed</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {body.dq_evaluations.map((evaluation) => (
                  <TableRow key={evaluation.evaluation_id}>
                    <TableCell>
                      <OwnerLink scope={scope} owner={evaluation.owner_ref} onOpen={onOpen}>
                        <ObjectId value={evaluation.monitor_id} title="DQ Monitor" />
                      </OwnerLink>
                    </TableCell>
                    <TableCell>{stateLabel(evaluation.outcome)}</TableCell>
                    <TableCell>{evaluation.evaluated_at ?? "Unknown"}</TableCell>
                    <TableCell>{String(evaluation.counts.failed ?? "Unknown")}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        )}
      </Panel>
    </Stack>
  );
}

function ProvenancePanel({
  body,
  scope,
  onOpen,
}: {
  body: ProvenanceLens;
  scope: AnalyzeScope;
  onOpen?: (href: string) => void;
}) {
  return (
    <Stack className="gap-6">
      <Panel>
        <PanelHeader
          title="Owner chain"
          description="Each link is recorded with its identity, or named as unrecorded."
        />
        <TableScroll label="Provenance chain">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Link</TableHead>
                <TableHead>State</TableHead>
                <TableHead>Identity</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {body.chain.map((entry) => (
                <TableRow key={entry.link}>
                  <TableCell>{entry.link}</TableCell>
                  <TableCell>
                    {/* Not colour alone: the state is a word. */}
                    {entry.status === "recorded" ? "Recorded" : "Not recorded"}
                  </TableCell>
                  <TableCell>
                    {entry.status === "recorded" ? (
                      <OwnerLink scope={scope} owner={entry.owner_ref} onOpen={onOpen}>
                        {entry.identity}
                      </OwnerLink>
                    ) : (
                      <span className="text-body text-text-secondary">{entry.reason}</span>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableScroll>
      </Panel>

      <Panel>
        <PanelHeader
          title="Per-value source evidence"
          description="Snapshotted when the query ran, never re-derived from today's bindings."
        />
        {body.value_source_unavailable ? (
          <Status
            as="block"
            tone="warning"
            title="This Result carries no per-value source tuple"
            data-testid="provenance-value-source-gap"
          >
            {body.value_source_unavailable.message} Owner:{" "}
            <code className="text-technical">{body.value_source_unavailable.owner}</code>.
          </Status>
        ) : (
          <TableScroll label="Per-value source tuples">
            <Table data-testid="provenance-value-source-tuples">
              <TableHeader>
                <TableRow>
                  <TableHead>Member</TableHead>
                  <TableHead>Source system</TableHead>
                  <TableHead>Source field</TableHead>
                  <TableHead>Run</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {body.value_source_tuples.map((tuple) => (
                  <TableRow key={`${tuple.member_id}:${tuple.source_field}`}>
                    <TableCell>
                      <code className="text-technical">{displayScalar(tuple.member_id)}</code>
                    </TableCell>
                    <TableCell>{displayScalar(tuple.source_system)}</TableCell>
                    <TableCell>
                      <code className="text-technical">{displayScalar(tuple.source_field)}</code>
                    </TableCell>
                    <TableCell>
                      <code className="text-technical">{displayScalar(tuple.pull_id)}</code>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        )}
      </Panel>

      <Panel>
        <PanelHeader title="Retry chain" description="A new execution creates a new Result." />
        <ul className="grid gap-2">
          <li className="text-body">
            Predecessor:{" "}
            {body.predecessor ? (
              <OwnerLink scope={scope} owner={body.predecessor.owner_ref} onOpen={onOpen}>
                <ObjectId value={body.predecessor.result_id} title="Result" />
              </OwnerLink>
            ) : (
              <span className="text-text-secondary">none recorded</span>
            )}
          </li>
          <li className="text-body">
            Later Results:{" "}
            {body.successors.length === 0 ? (
              <span className="text-text-secondary">none</span>
            ) : (
              body.successors.map((successor) => (
                <span key={successor.result_id} className="mr-3">
                  <OwnerLink scope={scope} owner={successor.owner_ref} onOpen={onOpen}>
                    <ObjectId value={successor.result_id} title="Result" />
                  </OwnerLink>{" "}
                  ({successor.outcome})
                </span>
              ))
            )}
          </li>
        </ul>
      </Panel>

      <Panel>
        <PanelHeader
          title="Freshness"
          description="How recent the data behind this Result is."
        />
        <FreshnessLines freshness={body.freshness} />
      </Panel>
    </Stack>
  );
}

/** Freshness in three states that are NOT the same absence (AI-296).
 *
 *  The line this replaces rendered `stale-since <ISO>` and rendered NOTHING
 *  otherwise. Two different facts shared that silence — "we looked and the data
 *  is current" and "nobody looked" — and silence reads as the first one. It was
 *  worse than that in practice: the server read the value from a manifest key
 *  nothing writes, so the line never appeared at all.
 *
 *  So the panel always says something, and `stale_since_evaluated` is what
 *  decides which. `unavailable` is not dressed up as a date: a Result whose
 *  manifest recorded no freshness says so, and names who would record it.
 */
/** What a column MEANS, where the number is read (AI-289).
 *
 *  The definition already existed — in the Definitions lens, ANOTHER tab. So a
 *  person reading a number had to leave the numbers to learn what they count,
 *  and a definition you have to go and find is a definition nobody reads.
 *
 *  THE LABEL IS THE HEADING, not the physical column name: `total_cost_eur` is
 *  what the warehouse calls it, `Media cost` is what the person asked for. The
 *  physical name stays visible underneath, because a Result is evidence and the
 *  column it came from is part of that.
 *
 *  A PINNED VERSION THAT CANNOT BE READ SAYS SO. Showing the concept's CURRENT
 *  definition in its place would be the historical rewrite AC8 forbids — the
 *  number would be described by a meaning it was never computed under.
 */
function ColumnMeaning({
  field,
  meaning,
}: {
  field: string;
  meaning: NonNullable<ViewLens["field_semantics"]>[string] | undefined;
}) {
  if (!meaning) {
    // Not every column is a Query Spec member — a Result can carry columns the
    // spec does not name. Silence is right here: inventing a definition for one
    // is exactly what this panel exists to avoid.
    return <>{field}</>;
  }
  if (!meaning.resolved) {
    return (
      <span title="This exact version is not readable in this Project. The current version has NOT been shown in its place.">
        {field} <span className="text-text-secondary">(definition unavailable)</span>
      </span>
    );
  }
  const parts = [
    meaning.definition,
    meaning.aggregation ? `Aggregated as ${meaning.aggregation}.` : null,
    meaning.unit ? `Unit: ${meaning.unit}.` : null,
    meaning.version_number != null ? `Version ${meaning.version_number}.` : null,
  ].filter(Boolean);
  return (
    <span title={parts.length > 0 ? parts.join(" ") : undefined}>
      {meaning.label ?? field}
      {meaning.label && meaning.label !== field ? (
        <span className="text-technical text-text-secondary"> ({field})</span>
      ) : null}
    </span>
  );
}

function FreshnessLines({
  freshness,
}: {
  freshness: ProvenanceLens["freshness"];
}) {
  const checked = freshness.stale_since_evaluated;
  const asOf = freshness.as_of;
  const state = freshness.state;

  if (!checked) {
    return (
      <ul className="grid gap-2">
        <li className="text-body">
          <span className="text-text-secondary">Not checked.</span> No freshness
          verdict was recorded for this Result, so this screen cannot tell you
          whether the data behind it is current. Re-run the query to get one.
        </li>
        {asOf ? (
          <li className="text-body">
            Data landed <time dateTime={asOf}>{asOf}</time> — that is when it was
            written, not a statement about whether it is still current.
          </li>
        ) : null}
      </ul>
    );
  }

  return (
    <ul className="grid gap-2">
      <li className="text-body">
        {state && state !== "unavailable" ? (
          <>Checked: {state}.</>
        ) : (
          <>Checked, and the data behind this Result is current.</>
        )}
      </li>
      {asOf ? (
        <li className="text-body">
          Data landed <time dateTime={asOf}>{asOf}</time>.
        </li>
      ) : null}
      {freshness.complete_through ? (
        <li className="text-body">
          Complete through{" "}
          <time dateTime={freshness.complete_through}>
            {freshness.complete_through}
          </time>
          .
        </li>
      ) : null}
    </ul>
  );
}

function AiPathPanel({
  body,
  onFeedbackTarget,
}: {
  body: unknown;
  onFeedbackTarget: (selection: FeedbackSelection) => void;
}) {
  return (
    <Panel data-testid="result-ai-path">
      <AiPathCapability
        projection={body}
        surface="console"
        onFeedbackTarget={(target, label) => onFeedbackTarget({ target, label })}
      />
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// The workbench.
// ---------------------------------------------------------------------------

export default function ResultWorkbench({
  scope,
  resultId,
  lens,
  evidenceId,
  onNavigateLens,
  onOpenTarget,
  onCloseEvidence,
}: {
  scope: AnalyzeScope;
  resultId: string;
  lens: ResultLens;
  evidenceId?: string | null;
  onNavigateLens: (lens: ResultLens) => void;
  onOpenTarget?: (href: string) => void;
  onCloseEvidence?: () => void;
}) {
  const [phase, setPhase] = useState<Phase>({ status: "loading" });
  // The element that opened the drawer, so focus returns exactly where it was.
  const invoker = useRef<HTMLElement | null>(null);
  // The Open in Toorow address, once the reader has asked for it. Held rather
  // than announced-and-forgotten so a browser without clipboard permission still
  // leaves the person with something they can select.
  const [shareTarget, setShareTarget] = useState<string | null>(null);
  const [shareState, setShareState] = useState<"idle" | "copied" | "manual">("idle");
  // The entry into the Visualization Builder (Story 50.4). Held here because the
  // act has three visible outcomes — working, refused, unreachable — and each
  // has to be said rather than swallowed.
  const [buildState, setBuildState] = useState<"idle" | "creating">("idle");
  const [buildFailure, setBuildFailure] = useState<string | null>(null);
  const [feedbackSelection, setFeedbackSelection] = useState<FeedbackSelection>(ANSWER_SELECTION);

  // Sauver la question en Report (50.3 AC4). Quatre etats visibles, parce que
  // creer un objet possede par le Projet ne doit jamais etre silencieux.
  // Story 75-2: the promotion dialog, open or not. One boolean, because the
  // dialog owns its own reads and its own refusals.
  const [proposingField, setProposingField] = useState(false);
  const [savingReport, setSavingReport] = useState(false);
  const [reportSaving, setReportSaving] = useState(false);
  const [reportLabel, setReportLabel] = useState("");
  const [reportDescription, setReportDescription] = useState("");
  const [savedReportId, setSavedReportId] = useState<string | null>(null);
  const [reportFailure, setReportFailure] = useState<string | null>(null);

  // THE READ THIS SCREEN'S ERROR BLOCK OFFERS TO REPEAT (76-4). An error
  // that names no way forward is a dead end; this token is what `Retry`
  // moves, and the effect below is the read it re-runs.
  const [reloadToken, setReloadToken] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let live = true;
    setPhase({ status: "loading" });
    setFeedbackSelection(ANSWER_SELECTION);
    fetchResultLens(scope.projectId, resultId, lens, { signal: controller.signal })
      .then((envelope) => {
        if (live) setPhase({ status: "ready", envelope });
      })
      .catch((error: unknown) => {
        // A response that arrives after this effect was cleaned up belongs to a
        // route nobody is looking at. Ignoring it is what stops one Project's
        // Result from painting itself under another Project's address.
        if (!live || controller.signal.aborted) return;
        if (error instanceof EnvelopeMismatch) {
          setPhase({ status: "mismatch", message: error.message });
          return;
        }
        if (error instanceof ApiError && (error.status === 404 || error.unauthenticated)) {
          setPhase({ status: "denied" });
          return;
        }
        setPhase({
          status: "error",
          message: error instanceof Error ? error.message : String(error),
        });
      });
    return () => {
      live = false;
      controller.abort();
    };
  }, [scope.projectId, resultId, lens, reloadToken]);

  const tabs = useMemo(
    () =>
      RESULT_LENSES.map((slug) => ({
        key: slug,
        label: LENS_LABELS[slug],
        href: resultTarget(scope, resultId, slug),
      })),
    [scope, resultId],
  );

  const openEvidence = useCallback(
    (datumKey: string) => {
      invoker.current = document.activeElement as HTMLElement | null;
      onOpenTarget?.(resultTarget(scope, resultId, lens, datumKey));
    },
    [scope, resultId, lens, onOpenTarget],
  );

  /**
   * AC12, from the console side.
   *
   * `openInToorowTarget` is the address an external surface — an MCP App card, a
   * Share page — uses to land on exactly this Result, this lens and this datum.
   * Emitting it here is what makes the builder reachable from production code
   * rather than from a unit test only: the same function, the same router, so an
   * external link and an internal navigation cannot diverge.
   */
  const copyOpenInToorow = useCallback(async () => {
    let href: string;
    try {
      href = openInToorowTarget(scope, resultId, lens, evidenceId ?? null);
    } catch {
      // The router refused to build it. Say nothing rather than hand over an
      // address that would open something else.
      setShareTarget(null);
      setShareState("idle");
      return;
    }
    setShareTarget(href);
    try {
      await navigator.clipboard.writeText(href);
      setShareState("copied");
    } catch {
      // No clipboard permission, or no clipboard at all. The address stays on
      // screen to be selected by hand — a silent failure would leave the person
      // pasting whatever was there before.
      setShareState("manual");
    }
  }, [scope, resultId, lens, evidenceId]);

  /**
   * The Visualization Builder's entry affordance (Story 50.4 AC7/AC12).
   *
   * Nothing navigated to `objectType: "visualization"` before this: the Builder
   * was mounted at an address no screen produced, so `POST /visualizations` was
   * unreachable from a browser and the whole create path was dead.
   *
   * It creates the identity FIRST and then navigates, because the canonical
   * router refuses to build an object address without an identifier — there is no
   * `/object/visualization/new` to open and no sentinel id worth inventing. The
   * presentation it seeds is the `table` family, which is the fallback every
   * visual owes anyway; changing it to any other family is one control away in
   * the Builder and appends a version rather than a Result.
   */
  const buildVisualization = useCallback(async (templateVersionId?: string) => {
    const querySpecVersionId =
      phase.status === "ready" ? phase.envelope.query_spec_version_id : null;
    if (!querySpecVersionId) return;
    setBuildFailure(null);
    setBuildState("creating");
    try {
      //  THE STARTING POINT IS A CHOICE NOW (story 72.5, AC22). With a template
      //  the server materialises from it; without one the fallback family is
      //  seeded, and `StartingPointChoice` above has already said in words why
      //  there was nothing to choose.
      const visualizationId = await createVisualizationFromResult(
        scope.projectId,
        querySpecVersionId,
        undefined,
        templateVersionId ? { templateVersionId, resultId } : undefined,
      );
      const href = visualizationBuilderTarget(scope, visualizationId, {
        tab: "build",
        resultId,
      });
      if (onOpenTarget) {
        onOpenTarget(href);
      } else {
        // No navigator was supplied. Hand over the exact address rather than
        // silently doing nothing: the Visualization was created either way, and
        // pretending otherwise would leave an orphan the reader never learns of.
        setBuildFailure(
          `The Visualization was created. This screen has no navigator, so open it at ${href}.`,
        );
      }
    } catch (error: unknown) {
      const body = error instanceof ApiError ? error.body : null;
      const refusals = (body as { refusals?: { message: string; remedy?: string }[] } | null)
        ?.refusals;
      setBuildFailure(
        refusals?.length
          ? refusals.map((r) => `${r.message} ${r.remedy ?? ""}`.trim()).join(" ")
          : error instanceof Error
            ? error.message
            : "The Visualization could not be created.",
      );
    } finally {
      setBuildState("idle");
    }
  }, [phase, scope, resultId, onOpenTarget]);

  /**
   * Save the question as a Report. The Result is NOT sent — only the Query Spec
   * version it pins, which is what makes a Report re-askable instead of a cached
   * answer (`analyze-and-test.md:50`).
   */
  const saveAsReport = useCallback(async () => {
    const querySpecVersionId =
      phase.status === "ready" ? phase.envelope.query_spec_version_id : null;
    if (!querySpecVersionId) return;
    setReportSaving(true);
    setReportFailure(null);
    try {
      const created = await createReportFromResult(
        scope.projectId,
        querySpecVersionId,
        reportLabel,
        reportDescription,
      );
      setSavedReportId(created.report_id);
      setSavingReport(false);
      setReportLabel("");
      setReportDescription("");
    } catch (error: unknown) {
      const body = error instanceof ApiError ? error.body : null;
      const message = (body as { message?: string } | null)?.message;
      setReportFailure(
        message ?? (error instanceof Error ? error.message : "The Report could not be created."),
      );
    } finally {
      setReportSaving(false);
    }
  }, [phase, scope, reportLabel, reportDescription]);

  const closeReportDialog = useCallback(() => {
    setSavingReport(false);
    setReportFailure(null);
  }, []);

  const closeEvidence = useCallback(() => {
    onCloseEvidence?.();
    // Focus restoration is not decoration: without it, closing the drawer sends
    // a keyboard user back to the top of the document, several tab stops from
    // the value they were inspecting.
    invoker.current?.focus?.();
  }, [onCloseEvidence]);

  const envelope = phase.status === "ready" ? phase.envelope : null;
  const outcome = envelope?.outcome ?? null;

  return (
    <Stack className="gap-6" data-owner="analyze/explore/result">
      <PageHeader
        title="Result"
        description="An immutable answer, with the exact data, definitions, quality, provenance and AI Path behind it."
      />
      <ObjectHeader
        name={resultId}
        source={
          /* WORDS AND NUMBERS, NEVER THE TWO IDENTIFIERS. This line read
             "Query Spec version qsv_01KZ… · Semantic View version svv_01KZ…" --
             the exact defect story 66.8 arbitrated for the Builder rail ("a
             legend reading `mdm_01KZ…` is a legend nobody can use").

             NO FALLBACK, because there is no state to fall back FROM. The first
             version carried "a Semantic View this Project no longer names" and a
             test that fabricated it; the schema forbids that state
             (`fk_query_spec_versions_semantic_scope`, plus NOT NULL on label and
             version_number), so the branch was dead and the test measured the
             component's copy of itself rather than the product. */
          envelope
            ? `Query Spec v${envelope.query_spec_version_number}`
              + ` · ${envelope.semantic_view_label} v${envelope.semantic_view_version_number}`
            : "Resolving this Result…"
        }
      />
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="secondary" onClick={() => void copyOpenInToorow()}>
          Copy Open in Toorow link
        </Button>
        <span className="text-caption text-text-secondary">
          The exact address of this Result, this lens and this datum.
        </span>
      </div>

      {/* STORY 75-2 -- the rail back into the shared model. A calculation worked
          out while reading this Result had nowhere to go: the only formula
          editor lived in Governance, four screens away, and nothing recorded
          which exploration produced the idea. This files a PROPOSAL, pinned to
          the Result and to the exact Concept versions this execution used; a
          person decides it in the Governance queue and nothing is published
          here. */}
      <div className="flex flex-wrap items-center gap-3">
        <Button
          variant="secondary"
          onClick={() => setProposingField(true)}
          disabled={phase.status !== "ready"}
          data-testid="result-propose-governed-field"
        >
          Propose as governed field
        </Button>
        <span className="text-caption text-text-secondary">
          A calculation found here becomes a proposal in Governance, with this Result named as
          where it came from.
        </span>
      </div>
      {envelope ? (
        <ProposeCalculatedFieldDialog
          open={proposingField}
          onClose={() => setProposingField(false)}
          projectId={scope.projectId}
          resultId={resultId}
          querySpecVersionId={envelope.query_spec_version_id}
          queueHref={governedFieldQueueTarget(scope)}
        />
      ) : null}

      {/* The path the Reports empty state used to INSTRUCT without it existing.
          `POST .../analyze/reports` has been served since 50.3 with zero call
          sites in `ui/admin/src`; this is the only one. A Report pins the Query
          Spec VERSION, never this Result — `analyze-and-test.md:50`, "the Report
          itself is not a cached answer" — so re-running it re-asks the question
          rather than replaying a stored answer. */}
      <div className="flex flex-wrap items-center gap-3">
        <Button
          variant="secondary"
          onClick={() => setSavingReport(true)}
          disabled={phase.status !== "ready"}
          data-testid="result-save-as-report"
        >
          Save this question as a Report
        </Button>
        <span className="text-caption text-text-secondary">
          A Report pins the Query Spec version, not this answer. Re-running it asks the question
          again.
        </span>
      </div>
      {savedReportId ? (
        <Status as="block" tone="success" title="Report created">
          <code>{savedReportId}</code> now carries this question in Analyze › Reports.
        </Status>
      ) : null}
      {reportFailure ? (
        <Status as="block" tone="warning" title="No Report was created">
          {reportFailure}
        </Status>
      ) : null}
      <Dialog open={savingReport} onOpenChange={(next) => (next ? undefined : closeReportDialog())}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Save this question as a Report</DialogTitle>
            <DialogDescription>
              {/* THE SAME CLASS, SIXTY LINES BELOW THE ONE THAT WAS REPAIRED.
                  This dialog printed `qsv_01KZ…` at the person in the very act of
                  pinning it -- the identifier the header had just stopped
                  showing, on the same screen. A repair that fixes the instance
                  it was shown and not the class guarantees the next click
                  fails. */}
              The Report pins Query Spec{" "}
              <strong>v{envelope?.query_spec_version_number}</strong>. Editing it later appends a
              version; it never rewrites this one.
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="report-label">Name</Label>
              <Input
                id="report-label"
                value={reportLabel}
                onChange={(event) => setReportLabel(event.target.value)}
                placeholder="e.g. Plan pacing by channel"
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="report-description">Description (optional)</Label>
              <Textarea
                id="report-description"
                value={reportDescription}
                onChange={(event) => setReportDescription(event.target.value)}
                placeholder="What this Report answers, and for whom."
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="secondary" onClick={closeReportDialog} disabled={reportSaving}>
              Cancel
            </Button>
            <Button onClick={() => void saveAsReport()} disabled={reportSaving || !reportLabel.trim()}>
              {reportSaving ? "Creating…" : "Create Report"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* THE QUESTION BEFORE THE GESTURE (story 72.5, AC22). The Chart Templates
          of this Project that fit THIS Result, offered before the fallback —
          which stays exactly where it was, and is now the named answer rather
          than the only path. */}
      <StartingPointChoice
        projectId={scope.projectId}
        resultId={resultId}
        busy={buildState === "creating"}
        onStartFromTemplate={(templateVersionId) => void buildVisualization(templateVersionId)}
        testId="result-starting-point"
      />
      <div className="flex flex-wrap items-center gap-3">
        <Button
          variant="secondary"
          onClick={() => void buildVisualization()}
          disabled={buildState === "creating" || phase.status !== "ready"}
          data-testid="result-build-visualization"
        >
          {buildState === "creating"
            ? "Creating the Visualization…"
            : "Start from a blank table instead"}
        </Button>
        <span className="text-caption text-text-secondary">
          Changing how this answer looks appends a Visualization version. It does not re-ask the
          question.
        </span>
      </div>
      {buildFailure ? (
        <Status
          as="block"
          tone="warning"
          title="The Visualization Builder could not be opened"
          data-testid="result-build-visualization-failure"
        >
          {buildFailure}
        </Status>
      ) : null}
      {shareTarget ? (
        <div role="status" aria-live="polite" data-testid="result-open-in-toorow">
          <p className="text-body">
            {shareState === "copied"
              ? "Open in Toorow link copied."
              : "Open in Toorow link — copy it by hand; this browser refused clipboard access."}
          </p>
          <code className="text-technical break-all">{shareTarget}</code>
        </div>
      ) : null}

      <NavTabs
        label="Result"
        tabs={tabs}
        current={lens}
        onNavigate={(key) => onNavigateLens(key as ResultLens)}
      />

      <div role="status" aria-live="polite">
        {phase.status === "loading" ? (
          <span className="text-body text-text-secondary">Reading the {LENS_LABELS[lens]} lens…</span>
        ) : null}
      </div>

      {phase.status === "denied" ? (
        <Status as="block" tone="warning" title="This Result is not available to you">
          No other Result has been opened in its place.
        </Status>
      ) : null}
      {phase.status === "mismatch" ? (
        <Status as="block" tone="error" title="This response does not describe this address">
          {phase.message}. Nothing has been rendered from it.
        </Status>
      ) : null}
      {phase.status === "error" ? (
        <Status as="block" tone="error" title="This lens could not be read"
          action={<Retry onClick={() => setReloadToken((token) => token + 1)} />}
        >
          {phase.message}
        </Status>
      ) : null}

      {envelope ? (
        <>
          <Status
            as="block"
            tone={stateTone(envelope.outcome)}
            title={`Outcome: ${envelope.outcome}`}
            data-testid="result-outcome"
          >
            {OUTCOME_SENTENCE[envelope.outcome] ?? "This Result reached a terminal outcome."}
          </Status>
          {envelope.lens === "view" && envelope.view ? (
            <ViewPanel
              body={envelope.view}
              scope={scope}
              resultId={resultId}
              onOpenEvidence={openEvidence}
              onFeedbackTarget={setFeedbackSelection}
            />
          ) : null}
          {envelope.lens === "data" && envelope.data ? <DataPanel body={envelope.data} /> : null}
          {envelope.lens === "definitions" && envelope.definitions ? (
            <DefinitionsPanel body={envelope.definitions} scope={scope} onOpen={onOpenTarget} />
          ) : null}
          {envelope.lens === "quality" && envelope.quality ? (
            <QualityPanel body={envelope.quality} scope={scope} onOpen={onOpenTarget} />
          ) : null}
          {envelope.lens === "provenance" && envelope.provenance ? (
            <ProvenancePanel body={envelope.provenance} scope={scope} onOpen={onOpenTarget} />
          ) : null}
          {envelope.lens === "ai-path" ? (
            <AiPathPanel body={envelope.ai_path} onFeedbackTarget={setFeedbackSelection} />
          ) : null}
          <TargetedFeedback
            context={envelope.feedback_context}
            selection={feedbackSelection}
            onClear={() => setFeedbackSelection(ANSWER_SELECTION)}
            onSubmit={(request: ExactFeedbackRequest) => submitExactFeedback(scope.projectId, request)}
            resetKey={`${envelope.result_id}:${envelope.content_hash}:${envelope.lens}`}
          />
        </>
      ) : null}

      <Dialog open={!!evidenceId} onOpenChange={(open) => (!open ? closeEvidence() : undefined)}>
        <DialogContent aria-describedby="result-evidence-description">
          <DialogHeader>
            <DialogTitle>Evidence for one value</DialogTitle>
            <DialogDescription id="result-evidence-description">
              The stable datum key of the figure this address names. It is part of the URL, so it
              can be shared and reopened on exactly this value.
            </DialogDescription>
          </DialogHeader>
          <EvidenceRows
            label="Datum evidence"
            source={{
              datum_key: evidenceId ?? "",
              result_id: resultId,
              lens,
              outcome: outcome ?? "Unknown",
              content_hash: envelope?.content_hash ?? "Unknown",
              query_spec_version_id: envelope?.query_spec_version_id ?? "Unknown",
              semantic_view_version_id: envelope?.semantic_view_version_id ?? "Unknown",
            }}
          />
          {/* Deliberately not a claim about what this Result does or does not
              record: the drawer holds one lens's envelope and cannot know. The
              Provenance lens states each link's exact state, so it is named
              rather than paraphrased here. */}
          <Status as="block" tone="info" title="Where this value's source evidence lives" className="mt-4">
            Provenance carries the (source_system, source_field, pull_id) tuple recorded for this
            Result, and names every link that is not recorded together with the module that owns
            writing it.
          </Status>
          <div className="mt-4 flex justify-end">
            <Button variant="secondary" onClick={closeEvidence}>
              Close
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </Stack>
  );
}

export { DEFAULT_RESULT_LENS };
