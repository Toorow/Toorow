/**
 * Story 66.7 -- the Analytics Explorer: five regions of ONE task, not five screens.
 *
 * The ratified target (`analyze-and-test.md`, *the Analytics Explorer*) names the
 * regions and their order: Sources, Useful matches, Match evidence, Composition,
 * Preview. They are progressive states of one workspace -- a region appears when
 * the previous one has been answered, and nothing that was already useful is
 * blanked when the next one loads.
 *
 * WHAT THIS COMPONENT IS NOT ALLOWED TO DO, and the reason each rule exists:
 *
 *   - it never aggregates. Every number on screen arrives computed from
 *     `/analyze/matches`, `/profile`, `/execute` or `/pivot`. A `reduce()` over
 *     cells here would be a second semantic engine with no measure contract;
 *   - it never merges the three states. Authority, observed coverage and
 *     execution safety are rendered as three separate, WORDED statements. One
 *     badge would let a governed path with no measured rows read as ready;
 *   - it never decides a path. Two equally valid relationships come back as a
 *     refusal naming both, and the screen asks the person;
 *   - it never blanks the last useful answer. A failed refresh leaves the Result
 *     and its evidence on screen with the failure stated beside them.
 *
 * ANALYTICAL VERSUS PRESENTATION, on screen. Choosing sources, a path or an
 * inclusion policy re-freezes a plan and re-executes: the screen says "this
 * creates a new Result". Switching Table/Pivot or moving a field between wells
 * re-projects the SAME Result: the screen says so, and the Result id does not
 * change.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  Button,
  Checkbox,
  Cluster,
  DraggableField,
  EmptyState,
  Field,
  FieldComposer,
  FieldShelf,
  FieldWell,
  Input,
  Metric,
  NativeSelect,
  Pager,
  Panel,
  PanelHeader,
  SectionHeader,
  Stack,
  Status,
  Table,
  TableBody,
  TableCell,
  TableFooter,
  TableHead,
  TableHeader,
  TableRow,
  TableScroll,
  type WellBindings,
  type WellChange,
  type WellField,
  type WellSpec,
  formatBytes, formatNumber, formatPercent,
  wireWord,
} from "../../ui";
import { createVisualizationFromResult } from "../builder/seedVisualization";
import StartingPointChoice from "../builder/StartingPointChoice";
import ProposeCalculatedFieldDialog from "../../governance/ProposeCalculatedFieldDialog";
// The read-time micros division, shared with the MCP App: one Result cannot
// state one amount two ways (`viz/theme/canonicalAmount.ts`).
import { formatGovernedValue } from "@toorow/card-shell/viz";
import { ApiError } from "../../lib/apiFetch";
import { fetchResultEvidence, type ResultEvidence } from "../queryClient";
import {
  compilePlan,
  executePlan,
  fetchAnalysisBusinessDomains,
  fetchAnalysisGoldenQuestions,
  fetchAnalysisSkills,
  fetchMatchProfile,
  fetchMatches,
  pivotResult,
  type AnalysisBusinessDomain,
  type AnalysisGoldenQuestion,
  type AnalysisSkill,
  type DatastreamMatch,
  type FrozenAnalysisContext,
  type MatchCatalog,
  type MatchProfile,
  type MatchProfileCost,
  type PivotAxisValue,
  type PivotMatrix,
} from "./explorerClient";
import {
  applyHandoffPins,
  describePinDisagreements,
  type HandoffPins,
  type PinDisagreement,
} from "./handoffPins";

type Presentation = "table" | "pivot" | "chart";

type CompiledMember = {
  datastream_id: string;
  /**
   * `canonical_name` IS FROZEN ON THE PLAN, so the legend of a Result is not
   * recomposed here. This screen used to look each measure up in the match
   * catalogue and fall back to `measure.canonical_field_id`, which legended
   * `mdm_01KZ...` for any measure that catalogue does not carry.
   */
  measures: {
    canonical_field_id: string;
    canonical_name?: string;
    result_field?: string;
  }[];
};

/** The words each state is said in. A colour alone is not one of them. */
const AUTHORITY_WORDS: Record<string, string> = {
  governed: "Approved to run",
  needs_governance: "Needs a governor",
};
const COVERAGE_WORDS: Record<string, string> = {
  exact: "Measured",
  estimated: "Estimated",
  unavailable: "Not measured",
};
const SAFETY_WORDS: Record<string, string> = {
  ready: "Safe to run",
  review_required: "Review before running",
  unsafe: "Unsafe as it stands",
};

function errorMessage(error: unknown): string {
  if (error && typeof error === "object" && "message" in error) {
    return String((error as { message: unknown }).message);
  }
  return String(error);
}

function MatchStates({ match }: { match: DatastreamMatch }) {
  return (
    <div className="grid gap-3 lg:grid-cols-3" data-testid="match-states">
      <Metric label="Authority" value={AUTHORITY_WORDS[match.authority] ?? match.authority} />
      <Metric
        label="Observed coverage"
        value={COVERAGE_WORDS[match.observed_coverage] ?? match.observed_coverage}
        hint="How many rows actually match is measured in the evidence step."
      />
      {/* The badge alone named no gesture: `Unsafe as it stands` said nothing
          about WHAT stands in the way. The executor's own refusal now travels
          with the match, so the missing thing is named where the verdict is
          read — a governed bridge, an explicit deduplication rule. */}
      <Metric
        label="Execution safety"
        value={SAFETY_WORDS[match.execution_safety] ?? match.execution_safety}
        hint={match.execution_blocked?.message}
      />
    </div>
  );
}

/**
 * What this check cost, in words, or nothing at all.
 *
 * NO EMPTY LINE — the rule the ratified paragraph names. A figure whose state is
 * `not_applicable` is ABSENT: on DuckDB the billed-bytes line does not exist,
 * rather than printing `0` (a measurement nobody made) or the words "not
 * applicable" (a row that says nothing, and noise teaches a reader to skip the
 * block that carries the one line that matters). An `unavailable` figure IS
 * shown, because "the engine bills and this job did not say" is something the
 * reader needs.
 */
function costSentence(cost: MatchProfileCost | undefined): string | null {
  if (!cost) return null;
  const parts: string[] = [];

  const jobs = cost.reused_from_receipt
    ? `${cost.warehouse_jobs_issued} warehouse job${cost.warehouse_jobs_issued === 1 ? "" : "s"} when it was measured`
    : `${cost.warehouse_jobs_issued} warehouse job${cost.warehouse_jobs_issued === 1 ? "" : "s"}`;
  parts.push(jobs);

  if (cost.elapsed_ms_state === "exact" && cost.elapsed_ms !== null) {
    parts.push(`${formatNumber(cost.elapsed_ms)} ms`);
  } else if (cost.elapsed_ms_state === "unavailable") {
    parts.push("time unavailable — a job never reached the engine");
  }

  if (cost.billed_bytes_state === "exact" && cost.billed_bytes !== null) {
    parts.push(`${formatBytes(cost.billed_bytes)} billed`);
  } else if (cost.billed_bytes_state === "unavailable") {
    parts.push("billed bytes unavailable for this run");
  }
  // `not_applicable` adds nothing, on purpose.

  return parts.join(" · ");
}

function ProfileEvidence({ profile }: { profile: MatchProfile }) {
  const sides = [profile.left, profile.right];
  const cost = costSentence(profile.cost);
  return (
    <Stack className="gap-4" data-testid="match-evidence">
      <Cluster className="gap-4">
        {sides.map((side) => (
          <Metric
            key={side.datastream_id}
            label={side.name}
            value={
              side.state === "exact"
                ? `${side.total_rows} rows, ${side.distinct_keys} keys`
                : "Not measured"
            }
            hint={
              side.state === "exact"
                ? `${side.null_key_rows} row(s) with an empty key, ${side.duplicated_keys ?? 0} repeated key(s)`
                : "The warehouse could not be read, so nothing here is a count of zero."
            }
          />
        ))}
        <Metric
          label="Matched keys"
          value={
            profile.matched.state === "exact"
              ? String(profile.matched.matched_keys)
              : "Not measured"
          }
          hint={
            profile.matched.state === "exact"
              ? `${profile.matched.left_unmatched_keys} unmatched on the left, ${profile.matched.right_unmatched_keys} on the right`
              : undefined
          }
        />
      </Cluster>
      <Status
        as="block"
        tone={profile.execution_safety === "unsafe" ? "error" : profile.execution_safety === "ready" ? "success" : "warning"}
        title={SAFETY_WORDS[profile.execution_safety] ?? profile.execution_safety}
        data-testid="multiplication-explanation"
      >
        {profile.multiplication.explanation}
        {/* An executor refusal is not a multiplication finding, and merging the
            two would hide it: the rows can measure perfectly clean while the
            relationship still has no execution path. */}
        {profile.execution_blocked && (
          <span className="mt-2 block" data-testid="execution-blocked">
            {profile.execution_blocked.message}
          </span>
        )}
      </Status>
      {/* BESIDE THE QUALITY FIGURES, because it is one of them: on a billed
          engine, inspecting is money the customer spends. A figure the product
          measures and never shows is a figure it did not measure, from where the
          person stands. */}
      {cost ? (
        <p className="m-0 text-caption text-text-secondary" data-testid="profile-cost">
          This check cost {cost}.
        </p>
      ) : null}
    </Stack>
  );
}

function PivotTable({ matrix, labels }: { matrix: PivotMatrix; labels: Record<string, string> }) {
  const wireKey = (key: PivotAxisValue[]) => JSON.stringify(key);
  const axisLabel = (key: PivotAxisValue[]) =>
    key.length
      ? key.map((value) => (value === null ? "(not set)" : String(value))).join(" / ")
      : "All";
  const cellAt = (rowKey: PivotAxisValue[], columnKey: PivotAxisValue[]) =>
    matrix.cells.find(
      (cell) =>
        JSON.stringify(cell.row_key) === JSON.stringify(rowKey) &&
        JSON.stringify(cell.column_key) === JSON.stringify(columnKey),
    );
  const subtotalAt = (rowKey: PivotAxisValue[]) => matrix.row_subtotals?.find(
    (subtotal) => JSON.stringify(subtotal.row_key) === JSON.stringify(rowKey),
  );
  // `column_subtotals` and the grand total's `by_column_key` are the same numbers
  // under two contracts; either one draws the bottom row.
  const columnTotals = matrix.column_subtotals ?? matrix.grand_total?.by_column_key ?? [];
  // A TOTAL SPANS THE FILTERED SET, NEVER THE PAGE. On a truncated page its
  // cells add to less than the figure beside them, and the reader has no way to
  // know which is wrong unless the label says. Recomputing over the served page
  // was refused: a subtotal that moves when a window is resized, while nothing
  // about the data moved, is worse than one that needs a word.
  const totalScope = matrix.bounds.totals_cover_unserved_rows === true
    ? ` · all ${matrix.bounds.total_row_keys ?? "?"} rows, not only this page`
    : "";
  // LE SOUS-TOTAL DE LIGNE COURT SUR LES COLONNES, donc c'est l'axe COLONNE qui
  // decide de son etiquette. `totals_cover_unserved_columns` etait calcule et lu
  // par personne : la moitie du defaut restait muette.
  const rowTotalScope = matrix.bounds.totals_cover_unserved_columns === true
    ? ` · all ${matrix.bounds.total_column_keys ?? "?"} columns`
    : "";
  const columnTotalAt = (columnKey: PivotAxisValue[]) => columnTotals.find(
    (entry) => JSON.stringify(entry.column_key) === JSON.stringify(columnKey),
  );
  const comparisonValue = (
    entry: { value: number | null; absent_reason?: string },
    relative = false,
  ) => {
    if (entry.value === null) {
      if (entry.absent_reason === "baseline_zero") return "baseline is zero";
      if (entry.absent_reason === "comparison_value_missing") return "comparison unavailable";
      return entry.absent_reason ?? "no data";
    }
    // A RELATIVE comparison is a ratio, and it is passed as one — the
    // `scaled` escape hatch of `formatPercent` exists for the other case and
    // is deliberately not taken here.
    return relative
      ? formatPercent(entry.value, { digits: 2 })
      : formatNumber(entry.value);
  };

  return (
    <Stack className="gap-2">
      {/* One named, keyboard-focusable horizontal scroller (UX-DR8). */}
      <TableScroll label="Pivot rows and columns" data-testid="pivot-scroller">
        <Table className="min-w-max">
          <TableHeader>
            <TableRow>
              <TableHead scope="col" className="min-w-36">
                {matrix.row_fields.map((field) => labels[field] ?? field).join(" / ") || "All rows"}
              </TableHead>
              {matrix.column_keys.flatMap((columnKey) =>
                matrix.value_fields.map((valueField) => (
                  <TableHead
                    key={`${wireKey(columnKey)}:${valueField.name}`}
                    scope="col"
                    numeric
                    className="min-w-44 max-w-56 whitespace-normal align-bottom normal-case tracking-normal"
                  >
                    {axisLabel(columnKey)} · {labels[valueField.name] ?? valueField.name}
                  </TableHead>
                )),
              )}
              {matrix.row_subtotals?.length
                ? matrix.value_fields.map((valueField) => (
                    <TableHead key={`subtotal:${valueField.name}`} scope="col" numeric>
                      Row total{rowTotalScope} · {labels[valueField.name] ?? valueField.name}
                    </TableHead>
                  ))
                : null}
            </TableRow>
          </TableHeader>
          <TableBody>
            {matrix.row_keys.map((rowKey) => (
              <TableRow key={wireKey(rowKey)} density="compact">
                <th
                  scope="row"
                  className="min-w-36 border-b border-divider-base px-4.5 py-2.5 text-left font-label"
                >
                  {axisLabel(rowKey)}
                </th>
                {matrix.column_keys.flatMap((columnKey) =>
                  matrix.value_fields.map((valueField) => {
                  const cell = cellAt(rowKey, columnKey);
                  const value = cell?.values[valueField.name];
                  return (
                    <TableCell
                      key={`${wireKey(columnKey)}:${valueField.name}`}
                      numeric
                      className="min-w-44"
                    >
                      {/* A cell with nothing in it says so in words. Rendering an
                          absent value as 0 would invent a business fact. */}
                      {value?.value === null || value?.value === undefined ? (
                        <span className="text-text-secondary">no data</span>
                      ) : (
                        formatGovernedValue(value.value, valueField) ?? String(value.value)
                      )}
                    </TableCell>
                  );
                  }),
                )}
                {matrix.row_subtotals?.length
                  ? matrix.value_fields.map((valueField) => {
                      const value = subtotalAt(rowKey)?.values[valueField.name];
                      return (
                        <TableCell key={`subtotal:${valueField.name}`} numeric className="font-semibold">
                          {value?.value === null || value?.value === undefined
                            ? "no data"
                            : formatGovernedValue(value.value, valueField) ?? String(value.value)}
                        </TableCell>
                      );
                    })
                  : null}
              </TableRow>
            ))}
          </TableBody>
          {columnTotals.length || matrix.grand_total?.overall ? (
            <TableFooter>
              {/* The column axis totals in place, under the column it totals —
                  a matrix that totals one axis and not the other is a grouped
                  list wearing a pivot's name. */}
              {columnTotals.length ? (
                <TableRow data-testid="pivot-column-totals">
                  <th
                    scope="row"
                    className="min-w-36 border-b border-divider-base px-4.5 py-2.5 text-left font-label"
                  >
                    Column total{totalScope}
                  </th>
                  {matrix.column_keys.flatMap((columnKey) =>
                    matrix.value_fields.map((valueField) => {
                      const value = columnTotalAt(columnKey)?.values[valueField.name];
                      return (
                        <TableCell
                          key={`column-total:${wireKey(columnKey)}:${valueField.name}`}
                          numeric
                          className="min-w-44 font-semibold"
                        >
                          {value?.value === null || value?.value === undefined
                            ? "no data"
                            : formatGovernedValue(value.value, valueField) ?? String(value.value)}
                        </TableCell>
                      );
                    }),
                  )}
                  {matrix.row_subtotals?.length
                    ? matrix.value_fields.map((valueField) => {
                        const value = matrix.grand_total?.overall?.[valueField.name];
                        return (
                          <TableCell key={`corner:${valueField.name}`} numeric className="font-semibold">
                            {value?.value === null || value?.value === undefined
                              ? "no data"
                              : formatGovernedValue(value.value, valueField) ?? String(value.value)}
                          </TableCell>
                        );
                      })
                    : null}
                </TableRow>
              ) : null}
              {/* The corner value is the whole. It exists only when both axes
                  collapsed, so a total of one axis is never printed under the
                  name of the total of everything. */}
              {matrix.grand_total?.overall ? (
                <TableRow>
                  <TableCell colSpan={1 + matrix.column_keys.length * matrix.value_fields.length
                    + (matrix.row_subtotals?.length ? matrix.value_fields.length : 0)}>
                    Grand total{totalScope} · {matrix.value_fields.map((field) => {
                      const value = matrix.grand_total?.overall?.[field.name];
                      const printed = formatGovernedValue(value?.value, field);
                      return `${labels[field.name] ?? field.name}: ${printed ?? "no data"}`;
                    }).join(" · ")}
                  </TableCell>
                </TableRow>
              ) : null}
            </TableFooter>
          ) : null}
        </Table>
      </TableScroll>
      {matrix.comparison ? (
        <Stack className="gap-2" data-testid="period-comparison">
          <Status as="block" tone="info" title={
            matrix.comparison.kind === "previous_year" ? "Compared with the previous year" : "Compared with the previous period"
          }>
            Current {matrix.comparison.current.start} to {matrix.comparison.current.end}; baseline{" "}
            {matrix.comparison.baseline.start} to {matrix.comparison.baseline.end}. Both windows
            belong to Result {matrix.result_id}.
          </Status>
          <TableScroll label="Period comparison deltas">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead scope="col">Rows</TableHead>
                  <TableHead scope="col">Columns</TableHead>
                  <TableHead scope="col">Value</TableHead>
                  <TableHead scope="col" numeric>Current</TableHead>
                  <TableHead scope="col" numeric>Baseline</TableHead>
                  <TableHead scope="col" numeric>Absolute change</TableHead>
                  <TableHead scope="col" numeric>Relative change</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {matrix.comparison.deltas.map((delta) => (
                  <TableRow
                    key={`${wireKey(delta.row_key)}:${wireKey(delta.column_key)}:${delta.value_field}`}
                    density="compact"
                  >
                    <TableCell>{axisLabel(delta.row_key)}</TableCell>
                    <TableCell>{axisLabel(delta.column_key)}</TableCell>
                    <TableCell>{labels[delta.value_field] ?? wireWord(delta.value_field)}</TableCell>
                    <TableCell numeric>{comparisonValue(delta.current)}</TableCell>
                    <TableCell numeric>{comparisonValue(delta.baseline)}</TableCell>
                    <TableCell numeric>{comparisonValue(delta.absolute_delta)}</TableCell>
                    <TableCell numeric>{comparisonValue(delta.relative_delta, true)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
          {matrix.comparison.unavailable_reason ? (
            <Status as="block" tone="warning" title="Comparison unavailable">
              {matrix.comparison.unavailable_reason}
            </Status>
          ) : null}
        </Stack>
      ) : null}
      {matrix.bounds.rows_truncated || matrix.bounds.columns_truncated || matrix.bounds.cells_truncated ? (
        <Status as="block" tone="warning" title="This pivot is truncated" data-testid="pivot-truncated">
          The first {matrix.row_keys.length} rows are shown; {matrix.bounds.rows_dropped} more were
          left out. {truncationAdvice(matrix.bounds.truncation_reason)}
        </Status>
      ) : null}
    </Stack>
  );
}

/**
 * Story 66.6. The gesture that REPAIRS this truncation, and it differs by cause.
 *
 * The banner used to end with "Narrow the analysis with a filter to see them"
 * whatever had cut the page. Measured at the cell cap exactly: it is the BYTE
 * budget that binds, and a filter is not what brings the next rows -- the
 * cursor already in the response is. Sending someone to filter a page that is
 * merely full makes them shrink an analysis that was the right size.
 */
function truncationAdvice(reason: PivotMatrix["bounds"]["truncation_reason"]): string {
  if (reason === "byte_budget") {
    return "The page is full, not the analysis: use the pager to see the rest.";
  }
  if (reason === "cell_cap") {
    return "The matrix is larger than one answer can carry. Remove a dimension, or split the analysis.";
  }
  return "Narrow the analysis with a filter to see them.";
}

function narrowingAdvice(error: unknown): string | null {
  if (!(error instanceof ApiError) || error.status !== 422) return null;
  if (["unsafe_fan_out", "profile_not_ready", "profile_unavailable"].includes(error.code)) {
    return "Choose another approved matching path, or narrow the date window before running again.";
  }
  if (["estimated_result_too_large", "too_many_result_columns", "row_limit_too_large"].includes(
    error.code,
  )) {
    return "Lower Maximum Result rows, select fewer Values, or add a narrower date window.";
  }
  return "Remove the field named by the refusal, or select a key governed across every connected source.";
}

function profileCanRun(profile: MatchProfile | null, match: DatastreamMatch | null): boolean {
  if (!profile || !match?.relationship) return false;
  if (profile.execution_safety === "ready") return true;
  if (
    profile.execution_safety !== "review_required" ||
    profile.left.state !== "exact" ||
    profile.right.state !== "exact" ||
    profile.multiplication.state !== "exact"
  ) return false;
  const leftDuplicates = profile.left.duplicated_keys ?? 0;
  const rightDuplicates = profile.right.duplicated_keys ?? 0;
  return (
    (match.relationship.cardinality === "many_to_one" && leftDuplicates > 0 && rightDuplicates === 0)
    || (match.relationship.cardinality === "one_to_many" && leftDuplicates === 0 && rightDuplicates > 0)
  );
}

function matchKey(match: DatastreamMatch): string {
  return [
    match.left.datastream_id,
    match.right.datastream_id,
    match.common_key?.version_id ?? match.kind,
    match.explore_together?.view_version_id ?? "unversioned",
    match.explore_together?.relationship_name ?? match.relationship?.relationship_name ?? "unrelated",
  ].join(":");
}

function graphSourceIds(matches: DatastreamMatch[]): Set<string> {
  return new Set(matches.flatMap((match) => [match.left.datastream_id, match.right.datastream_id]));
}

function canExtendGraph(matches: DatastreamMatch[], candidate: DatastreamMatch): boolean {
  if (!matches.length || !candidate.common_key || !candidate.explore_together) return false;
  if (matches.some((match) => matchKey(match) === matchKey(candidate))) return false;
  const sources = graphSourceIds(matches);
  const endpoints = [candidate.left.datastream_id, candidate.right.datastream_id];
  if (endpoints.filter((source) => sources.has(source)).length !== 1) return false;
  if (new Set([...sources, ...endpoints]).size > 4) return false;
  return matches.every(
    (match) => match.explore_together?.view_version_id === candidate.explore_together?.view_version_id,
  );
}

function sameOrientedPair(left: DatastreamMatch, right: DatastreamMatch): boolean {
  return left.left.datastream_id === right.left.datastream_id
    && left.right.datastream_id === right.right.datastream_id;
}

function governedAlternatives(
  catalog: MatchCatalog | null,
  current: DatastreamMatch,
): DatastreamMatch[] {
  if (!catalog || !current.explore_together) return [];
  return catalog.matches.filter((candidate) => (
    candidate.kind === "governed"
    && Boolean(candidate.common_key)
    && Boolean(candidate.explore_together)
    && sameOrientedPair(current, candidate)
    && candidate.explore_together?.view_version_id
      === current.explore_together?.view_version_id
    && matchKey(candidate) !== matchKey(current)
  ));
}

interface RemovableLeaf {
  edgeKey: string;
  sourceId: string;
  sourceName: string;
}

function removableLeaves(matches: DatastreamMatch[]): RemovableLeaf[] {
  if (graphSourceIds(matches).size <= 2) return [];
  const degrees = new Map<string, number>();
  for (const match of matches) {
    degrees.set(match.left.datastream_id, (degrees.get(match.left.datastream_id) ?? 0) + 1);
    degrees.set(match.right.datastream_id, (degrees.get(match.right.datastream_id) ?? 0) + 1);
  }
  return matches.flatMap((match) => [match.left, match.right]
    .filter((side) => degrees.get(side.datastream_id) === 1)
    .map((side) => ({
      edgeKey: matchKey(match),
      sourceId: side.datastream_id,
      sourceName: side.name,
    })));
}

const measureKey = (datastreamId: string, fieldId: string) => `${datastreamId}:${fieldId}`;

/** Same members, order ignored — the question "is this still the Result that is
 *  on screen". Order is a projection, membership is what was executed, and
 *  confusing the two is what would make a drag re-run a query. */
const sameMembers = (left: readonly string[], right: readonly string[]) =>
  left.length === right.length && left.every((value) => right.includes(value));

function ResultTable({ evidence, labels }: { evidence: ResultEvidence; labels: Record<string, string> }) {
  const fields = evidence.schema.fields ?? [];
  const columns = fields.map((field) => field.name);
  // The flat table is the same read as the pivot, so it states amounts the same
  // way. Two tables of one Result disagreeing on what 124000000 means would be
  // the defect this fixes, wearing a different shape.
  const meaningOf = new Map(fields.map((field) => [field.name, field]));
  return (
    <TableScroll label="Result rows" data-testid="result-table-scroller">
      <Table>
        <TableHeader>
          <TableRow>
            {columns.map((column) => <TableHead key={column}>{labels[column] ?? wireWord(column)}</TableHead>)}
          </TableRow>
        </TableHeader>
        <TableBody>
          {evidence.rows.map((row, index) => (
            <TableRow key={index}>
              {columns.map((column) => (
                <TableCell key={column}>
                  {row[column] === null || row[column] === undefined
                    ? <span className="text-text-secondary">no data</span>
                    : formatGovernedValue(row[column], meaningOf.get(column))
                      ?? String(row[column])}
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableScroll>
  );
}

export default function AnalyticsExplorer({
  projectId,
  onOpenVisualization,
  initialMatch,
}: {
  projectId: string;
  onOpenVisualization?: (visualizationId: string, resultId: string) => void;
  initialMatch?: {
    leftDatastreamId: string;
    rightDatastreamId: string;
    commonKeyVersionId: string;
    /** The exact versions `Explore together` handed over, or `null` when the
     *  address carried none. See `handoffPins.ts`. */
    pins?: HandoffPins | null;
  } | null;
}) {
  const [catalog, setCatalog] = useState<MatchCatalog | null>(null);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [selected, setSelected] = useState<DatastreamMatch | null>(null);
  // What the address pinned and this screen's own catalog read no longer agrees
  // with. `null` means no comparison was made (no pins came in); an empty array
  // means the two agree — the difference a reader needs.
  const [pinDisagreements, setPinDisagreements] = useState<PinDisagreement[] | null>(null);
  const [additionalMatches, setAdditionalMatches] = useState<DatastreamMatch[]>([]);
  const [additionalProfiles, setAdditionalProfiles] = useState<Record<string, MatchProfile>>({});
  const [addingMatchKey, setAddingMatchKey] = useState<string | null>(null);
  const [profile, setProfile] = useState<MatchProfile | null>(null);
  const [profileError, setProfileError] = useState<string | null>(null);
  const [inclusion, setInclusion] = useState("matched_only");
  const [presentation, setPresentation] = useState<Presentation>("table");
  const [resultId, setResultId] = useState<string | null>(null);
  const [querySpecVersionId, setQuerySpecVersionId] = useState<string | null>(null);
  const [resultEvidence, setResultEvidence] = useState<ResultEvidence | null>(null);
  const [resultValueFields, setResultValueFields] = useState<string[]>([]);
  const [compiledFieldLabels, setCompiledFieldLabels] = useState<Record<string, string>>({});
  const [matrix, setMatrix] = useState<PivotMatrix | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const [runAdvice, setRunAdvice] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [buildingVisualization, setBuildingVisualization] = useState(false);
  // Story 75-2: the promotion dialog. It owns its own reads and refusals.
  const [proposingField, setProposingField] = useState(false);
  const [projecting, setProjecting] = useState(false);
  // ORDERED LISTS, not one field each. The pivot projection has always accepted
  // `row_fields` / `column_fields` as lists (`pivot_projection.py`), and the
  // 2026-08-15 amendment makes the Composition region hold what the server
  // takes: an axis is a hierarchy, and its ORDER is what the reader nests by.
  const [rowFields, setRowFields] = useState<string[]>([]);
  const [columnFields, setColumnFields] = useState<string[]>([]);
  const [selectedMeasures, setSelectedMeasures] = useState<string[]>([]);
  // What the Result on screen was actually executed over. A composition that
  // has drifted from it is not repainted with new numbers -- it says it needs a
  // new Result, and the figures stay attached to the Result that produced them.
  const [executedDimensions, setExecutedDimensions] = useState<string[]>([]);
  const [executedMeasures, setExecutedMeasures] = useState<string[]>([]);
  const [filterField, setFilterField] = useState("");
  const [filterValue, setFilterValue] = useState("");
  const [grainField, setGrainField] = useState("");
  const [windowStart, setWindowStart] = useState("");
  const [windowEnd, setWindowEnd] = useState("");
  const [comparison, setComparison] = useState<"none" | "previous_period" | "previous_year">("none");
  const [rowLimit, setRowLimit] = useState("10000");
  const [rowSort, setRowSort] = useState("asc");
  const [subtotals, setSubtotals] = useState(false);
  const [grandTotal, setGrandTotal] = useState("none");
  const [goldenQuestions, setGoldenQuestions] = useState<AnalysisGoldenQuestion[]>([]);
  const [businessDomains, setBusinessDomains] = useState<AnalysisBusinessDomain[]>([]);
  const [skills, setSkills] = useState<AnalysisSkill[]>([]);
  const [goldenQuestionVersionId, setGoldenQuestionVersionId] = useState("");
  const [businessDomainVersionId, setBusinessDomainVersionId] = useState("");
  const [skillVersionIds, setSkillVersionIds] = useState<string[]>([]);
  const [analysisContext, setAnalysisContext] = useState<FrozenAnalysisContext | null>(null);
  const [contextOptionsUnavailable, setContextOptionsUnavailable] = useState(false);
  const profileGeneration = useRef(0);
  const addingMatchKeyRef = useRef<string | null>(null);
  const runGeneration = useRef(0);
  const runAbort = useRef<AbortController | null>(null);
  const projectionGeneration = useRef(0);
  const projectionAbort = useRef<AbortController | null>(null);
  const visualizationGeneration = useRef(0);
  const visualizationAbort = useRef<AbortController | null>(null);
  // A stack of the pages already left, as the SIGNED continuations the server
  // minted for them (story 66.10 AC 9). A number is still accepted because a
  // page the server never minted a token for -- an older Result, a `cursor`
  // the shape refused -- has no token to remember, and a Previous that then
  // did nothing would be worse than one that walks back by offset.
  const rowPageHistory = useRef<(string | number)[]>([]);
  const columnPageHistory = useRef<(string | number)[]>([]);
  const rowFieldsRef = useRef(rowFields);
  const columnFieldsRef = useRef(columnFields);
  const filterFieldRef = useRef(filterField);
  const grainFieldRef = useRef(grainField);
  const comparisonRef = useRef(comparison);
  rowFieldsRef.current = rowFields;
  columnFieldsRef.current = columnFields;
  filterFieldRef.current = filterField;
  grainFieldRef.current = grainField;
  comparisonRef.current = comparison;

  useEffect(() => () => {
    runAbort.current?.abort();
    projectionAbort.current?.abort();
    visualizationAbort.current?.abort();
  }, []);

  const invalidateExecutedResult = useCallback(() => {
    projectionGeneration.current += 1;
    visualizationGeneration.current += 1;
    projectionAbort.current?.abort();
    visualizationAbort.current?.abort();
    projectionAbort.current = null;
    visualizationAbort.current = null;
    setProjecting(false);
    setBuildingVisualization(false);
    setResultId(null);
    setQuerySpecVersionId(null);
    setResultEvidence(null);
    setMatrix(null);
    setAnalysisContext(null);
    setCompiledFieldLabels({});
    rowPageHistory.current = [];
    columnPageHistory.current = [];
  }, []);

  useEffect(() => {
    profileGeneration.current += 1;
    runGeneration.current += 1;
    projectionGeneration.current += 1;
    visualizationGeneration.current += 1;
    runAbort.current?.abort();
    projectionAbort.current?.abort();
    visualizationAbort.current?.abort();
    runAbort.current = null;
    projectionAbort.current = null;
    visualizationAbort.current = null;
    setCatalog(null);
    setCatalogError(null);
    setSelected(null);
    setAdditionalMatches([]);
    setAdditionalProfiles({});
    addingMatchKeyRef.current = null;
    setAddingMatchKey(null);
    setProfile(null);
    setProfileError(null);
    setResultId(null);
    setQuerySpecVersionId(null);
    setResultEvidence(null);
    setMatrix(null);
    rowPageHistory.current = [];
    columnPageHistory.current = [];
    setRunning(false);
    setProjecting(false);
    setBuildingVisualization(false);
    setRunError(null);
    setRunAdvice(null);
    setComparison("none");
    setGoldenQuestions([]);
    setBusinessDomains([]);
    setSkills([]);
    setGoldenQuestionVersionId("");
    setBusinessDomainVersionId("");
    setSkillVersionIds([]);
    setAnalysisContext(null);
    setContextOptionsUnavailable(false);
  }, [projectId]);

  useEffect(() => {
    let live = true;
    Promise.allSettled([
      fetchAnalysisBusinessDomains(projectId),
      fetchAnalysisGoldenQuestions(projectId),
      fetchAnalysisSkills(projectId),
    ]).then(([domains, questions, procedures]) => {
      if (!live) return;
      if (domains.status === "fulfilled") {
        setBusinessDomains(domains.value.domains ?? []);
      }
      if (questions.status === "fulfilled") {
        setGoldenQuestions(questions.value.golden_questions ?? []);
      }
      if (procedures.status === "fulfilled") {
        setSkills(procedures.value.procedures ?? []);
      }
      setContextOptionsUnavailable(
        domains.status === "rejected"
          || questions.status === "rejected"
          || procedures.status === "rejected"
          || (domains.status === "fulfilled" && domains.value.truncated)
          || (questions.status === "fulfilled" && questions.value.truncated)
          || (procedures.status === "fulfilled" && procedures.value.truncated),
      );
    });
    return () => {
      live = false;
    };
  }, [projectId]);

  useEffect(() => {
    let live = true;
    fetchMatches(projectId)
      .then((answer) => {
        if (live) {
          setCatalog(answer);
          setCatalogError(null);
        }
      })
      .catch((error: unknown) => {
        // The catalog could not be read. It is NOT an empty catalog, and the
        // screen must not offer "nothing can be crossed" as the explanation.
        if (live) setCatalogError(errorMessage(error));
      });
    return () => {
      live = false;
    };
  }, [projectId]);

  const inspect = useCallback(
    (match: DatastreamMatch) => {
      runGeneration.current += 1;
      runAbort.current?.abort();
      projectionGeneration.current += 1;
      visualizationGeneration.current += 1;
      projectionAbort.current?.abort();
      visualizationAbort.current?.abort();
      projectionAbort.current = null;
      visualizationAbort.current = null;
      setProjecting(false);
      setBuildingVisualization(false);
      const generation = profileGeneration.current + 1;
      profileGeneration.current = generation;
      setGoldenQuestionVersionId("");
      setBusinessDomainVersionId("");
      setSelected(match);
      setAdditionalMatches([]);
      setAdditionalProfiles({});
      addingMatchKeyRef.current = null;
      setAddingMatchKey(null);
      setProfile(null);
      setProfileError(null);
      setRunError(null);
      setRunAdvice(null);
      const components = match.common_key?.components ?? [];
      // A first placement, not a decision: the two first components of the
      // common key are what a person moves from, and every one of them stays
      // available in the shelf.
      setRowFields(components[0] ? [components[0].canonical_field_id] : []);
      setColumnFields(components[1] ? [components[1].canonical_field_id] : []);
      setFilterField(components[0]?.canonical_field_id ?? "");
      const likelyTime = components.find((component) =>
        /(^|[_ -])(day|date|time|month|week|year)($|[_ -])/i.test(component.canonical_name),
      );
      setGrainField(likelyTime?.canonical_field_id ?? components[0]?.canonical_field_id ?? "");
      setWindowStart("");
      setWindowEnd("");
      setComparison("none");
      setRowLimit("10000");
      setRowSort("asc");
      setFilterValue("");
      setResultId(null);
      setQuerySpecVersionId(null);
      setResultEvidence(null);
      setMatrix(null);
      rowPageHistory.current = [];
      columnPageHistory.current = [];
      setAnalysisContext(null);
      setCompiledFieldLabels({});
      setRunning(false);
      setSelectedMeasures(
        [match.left, match.right].flatMap((side) =>
          side.measures.map((measure) => measureKey(side.datastream_id, measure.canonical_field_id)),
        ),
      );
      if (!match.common_key || !match.explore_together) return;
      fetchMatchProfile(
        projectId,
        match.left.datastream_id,
        match.right.datastream_id,
        match.common_key.version_id,
        match.explore_together.relationship_name,
        match.explore_together.view_version_id,
      )
        .then((answer) => {
          if (profileGeneration.current === generation) setProfile(answer.profile);
        })
        .catch((error: unknown) => {
          if (profileGeneration.current === generation) setProfileError(errorMessage(error));
        });
    },
    [projectId],
  );

  const graphMatches = useMemo(
    () => (selected ? [selected, ...additionalMatches] : []),
    [additionalMatches, selected],
  );
  const graphSides = useMemo(() => {
    const sides = new Map<string, DatastreamMatch["left"]>();
    for (const match of graphMatches) {
      sides.set(match.left.datastream_id, match.left);
      sides.set(match.right.datastream_id, match.right);
    }
    return [...sides.values()];
  }, [graphMatches]);
  const graphComponents = useMemo(() => {
    const components = new Map<string, { canonical_field_id: string; canonical_name: string }>();
    for (const match of graphMatches) {
      for (const component of match.common_key?.components ?? []) {
        components.set(component.canonical_field_id, component);
      }
    }
    return [...components.values()];
  }, [graphMatches]);
  const graphGrainComponents = useMemo(
    () => graphComponents.filter(
      (component) => /(^|[_ -])(day|date|time|month|week|year)($|[_ -])/i.test(
        component.canonical_name,
      ) && graphMatches.every((match) => match.common_key?.components.some(
        (candidate) => candidate.canonical_field_id === component.canonical_field_id,
      )),
    ),
    [graphComponents, graphMatches],
  );
  const graphLeaves = useMemo(() => removableLeaves(graphMatches), [graphMatches]);
  const graphProfilesReady = Boolean(profile) && graphMatches.slice(1).every(
    (match) => Boolean(additionalProfiles[matchKey(match)]),
  );

  const applyGraphTransition = useCallback((
    nextMatches: DatastreamMatch[],
    profilesByEdge: Record<string, MatchProfile>,
  ) => {
    if (!nextMatches.length) return;
    runGeneration.current += 1;
    runAbort.current?.abort();
    runAbort.current = null;
    profileGeneration.current += 1;
    addingMatchKeyRef.current = null;
    setAddingMatchKey(null);
    setRunning(false);
    invalidateExecutedResult();
    setRunError(null);
    setRunAdvice(null);
    setProfileError(null);

    const nextPrimary = nextMatches[0];
    const nextAdditional = nextMatches.slice(1);
    setSelected(nextPrimary);
    setProfile(profilesByEdge[matchKey(nextPrimary)] ?? null);
    setAdditionalMatches(nextAdditional);
    setAdditionalProfiles(Object.fromEntries(nextAdditional.flatMap((match) => {
      const exactProfile = profilesByEdge[matchKey(match)];
      return exactProfile ? [[matchKey(match), exactProfile]] : [];
    })));

    const nextSides = new Map<string, DatastreamMatch["left"]>();
    const nextComponents = new Map<string, { canonical_field_id: string; canonical_name: string }>();
    for (const match of nextMatches) {
      nextSides.set(match.left.datastream_id, match.left);
      nextSides.set(match.right.datastream_id, match.right);
      for (const component of match.common_key?.components ?? []) {
        nextComponents.set(component.canonical_field_id, component);
      }
    }
    const componentIds = new Set(nextComponents.keys());
    const orderedComponents = [...nextComponents.values()];
    const currentFilterField = filterFieldRef.current;

    const commonTemporalIds = new Set(orderedComponents
      .filter((component) => /(^|[_ -])(day|date|time|month|week|year)($|[_ -])/i.test(
        component.canonical_name,
      ))
      .filter((component) => nextMatches.every((match) => match.common_key?.components.some(
        (candidate) => candidate.canonical_field_id === component.canonical_field_id,
      )))
      .map((component) => component.canonical_field_id));
    const retainedGrain = commonTemporalIds.has(grainFieldRef.current)
      ? grainFieldRef.current
      : "";
    const comparisonActive = comparisonRef.current !== "none" && Boolean(retainedGrain);
    const validWell = (fieldId: string) => componentIds.has(fieldId)
      && (!comparisonActive || fieldId !== retainedGrain);
    // A graph transition keeps every axis field the new graph still carries, in
    // the order the person put them in. What it cannot keep it drops -- it never
    // substitutes a neighbouring dimension, which would silently change what the
    // reading groups by. An axis emptied that way is refilled by hand, from the
    // shelf, which is the whole point of a composition made of moves.
    const nextRows = rowFieldsRef.current.filter(validWell);
    const nextColumns = columnFieldsRef.current
      .filter(validWell)
      .filter((fieldId) => !nextRows.includes(fieldId));
    setRowFields(nextRows);
    setColumnFields(nextColumns);

    const keepFilter = currentFilterField !== ""
      && componentIds.has(currentFilterField)
      && (!comparisonActive || currentFilterField !== retainedGrain);
    setFilterField(keepFilter ? currentFilterField : "");
    setFilterValue((current) => keepFilter ? current : "");

    if (!retainedGrain) {
      setGrainField("");
      setWindowStart("");
      setWindowEnd("");
      setComparison("none");
    }

    const measureIds = new Set([...nextSides.values()].flatMap((side) => side.measures.map(
      (measure) => measureKey(side.datastream_id, measure.canonical_field_id),
    )));
    setSelectedMeasures((current) => current.filter((measure) => measureIds.has(measure)));
  }, [invalidateExecutedResult]);

  const removeLeaf = useCallback((leaf: RemovableLeaf) => {
    if (
      running
      || projecting
      || buildingVisualization
      || addingMatchKeyRef.current
      || !graphProfilesReady
    ) return;
    const profilesByEdge: Record<string, MatchProfile> = { ...additionalProfiles };
    if (selected && profile) profilesByEdge[matchKey(selected)] = profile;
    applyGraphTransition(
      graphMatches.filter((match) => matchKey(match) !== leaf.edgeKey),
      profilesByEdge,
    );
  }, [
    additionalProfiles,
    applyGraphTransition,
    buildingVisualization,
    graphMatches,
    graphProfilesReady,
    profile,
    projecting,
    running,
    selected,
  ]);

  const switchRelationship = useCallback(async (
    current: DatastreamMatch,
    replacement: DatastreamMatch,
  ) => {
    if (
      running
      || projecting
      || buildingVisualization
      || addingMatchKeyRef.current
      || !graphProfilesReady
      || !replacement.common_key
      || !replacement.explore_together
    ) return;
    const key = matchKey(replacement);
    const generation = profileGeneration.current + 1;
    profileGeneration.current = generation;
    addingMatchKeyRef.current = key;
    setAddingMatchKey(key);
    setProfileError(null);
    try {
      const answer = await fetchMatchProfile(
        projectId,
        replacement.left.datastream_id,
        replacement.right.datastream_id,
        replacement.common_key.version_id,
        replacement.explore_together.relationship_name,
        replacement.explore_together.view_version_id,
      );
      if (profileGeneration.current !== generation) return;
      const profilesByEdge: Record<string, MatchProfile> = { ...additionalProfiles };
      if (selected && profile) profilesByEdge[matchKey(selected)] = profile;
      delete profilesByEdge[matchKey(current)];
      profilesByEdge[key] = answer.profile;
      applyGraphTransition(
        graphMatches.map((match) => matchKey(match) === matchKey(current) ? replacement : match),
        profilesByEdge,
      );
    } catch (error: unknown) {
      if (profileGeneration.current === generation) setProfileError(errorMessage(error));
    } finally {
      if (profileGeneration.current === generation && addingMatchKeyRef.current === key) {
        addingMatchKeyRef.current = null;
        setAddingMatchKey(null);
      }
    }
  }, [
    additionalProfiles,
    applyGraphTransition,
    buildingVisualization,
    graphMatches,
    graphProfilesReady,
    profile,
    projectId,
    projecting,
    running,
    selected,
  ]);

  useEffect(() => {
    if (grainField && !graphGrainComponents.some(
      (component) => component.canonical_field_id === grainField,
    )) {
      setGrainField("");
      setWindowStart("");
      setWindowEnd("");
      setComparison("none");
    }
  }, [grainField, graphGrainComponents]);

  const addSource = useCallback(async (match: DatastreamMatch) => {
    const key = matchKey(match);
    if (
      running
      || projecting
      || buildingVisualization
      || addingMatchKeyRef.current
      || !profile
      || !canExtendGraph(graphMatches, match)
      || !match.common_key
      || !match.explore_together
    ) return;
    const generation = profileGeneration.current;
    addingMatchKeyRef.current = key;
    setAddingMatchKey(key);
    setProfileError(null);
    try {
      const answer = await fetchMatchProfile(
        projectId,
        match.left.datastream_id,
        match.right.datastream_id,
        match.common_key.version_id,
        match.explore_together.relationship_name,
        match.explore_together.view_version_id,
      );
      if (profileGeneration.current !== generation) return;
      projectionGeneration.current += 1;
      projectionAbort.current?.abort();
      projectionAbort.current = null;
      setProjecting(false);
      visualizationGeneration.current += 1;
      visualizationAbort.current?.abort();
      visualizationAbort.current = null;
      setBuildingVisualization(false);
      setAdditionalMatches((current) => [...current, match]);
      setAdditionalProfiles((current) => ({ ...current, [key]: answer.profile }));
      setSelectedMeasures((current) => Array.from(new Set([
        ...current,
        ...[match.left, match.right].flatMap((side) =>
          side.measures.map((measure) => measureKey(side.datastream_id, measure.canonical_field_id)),
        ),
      ])));
      setResultId(null);
      setQuerySpecVersionId(null);
      setResultEvidence(null);
      setMatrix(null);
      setAnalysisContext(null);
      setCompiledFieldLabels({});
    } catch (error: unknown) {
      if (profileGeneration.current === generation) setProfileError(errorMessage(error));
    } finally {
      if (profileGeneration.current === generation && addingMatchKeyRef.current === key) {
        addingMatchKeyRef.current = null;
        setAddingMatchKey(null);
      }
    }
  }, [buildingVisualization, graphMatches, profile, projectId, projecting, running]);

  /** Open the pair the address named — AT THE VERSIONS IT PINNED.
   *
   *  THE DEFECT THIS CLOSES. This screen fetches its own match catalog, and it
   *  used to select the handed pair out of that fresh read and compose every
   *  request from it. So `Explore together` handed over a Datastream IDENTITY
   *  and the versions were resolved again here — the re-resolution data.md
   *  forbids by name in *Which Datastreams can usefully be crossed*: "a handoff
   *  that re-resolves `latest` hands off a different question than the one that
   *  was read". The pins now travel in the address and are worn by the match
   *  before anything is composed from it.
   *
   *  AND A DISAGREEMENT IS SAID, NEVER SETTLED HERE. If the catalog has moved on
   *  since the Workbench was read, both facts are kept: the pinned versions stay
   *  in force, and the drift is named with the gesture that moves off it. */
  useEffect(() => {
    if (!catalog || selected || !initialMatch) return;
    const match = catalog.matches.find((candidate) => {
      const sameOrder = candidate.left.datastream_id === initialMatch.leftDatastreamId
        && candidate.right.datastream_id === initialMatch.rightDatastreamId;
      const reverseOrder = candidate.left.datastream_id === initialMatch.rightDatastreamId
        && candidate.right.datastream_id === initialMatch.leftDatastreamId;
      return (sameOrder || reverseOrder)
        && candidate.common_key?.version_id === initialMatch.commonKeyVersionId;
    });
    if (!match) return;
    const pins = initialMatch.pins ?? null;
    if (!pins) {
      inspect(match);
      return;
    }
    setPinDisagreements(describePinDisagreements(match, pins));
    inspect(applyHandoffPins(match, pins));
  }, [catalog, initialMatch, inspect, selected]);

  /** The name the catalog gives a Datastream, so the drift above names sources
   *  rather than identifiers. Nothing is inferred when the catalog has none. */
  const sourceName = useCallback((datastreamId: string): string | null => {
    for (const match of catalog?.matches ?? []) {
      for (const side of [match.left, match.right]) {
        if (side.datastream_id === datastreamId) return side.name;
      }
    }
    return null;
  }, [catalog]);

  /** The gesture out of a superseded pin, taken by a person and never by the
   *  screen: re-open the same pair at the versions the catalog holds today. */
  const readCurrentVersions = useCallback(() => {
    if (!selected) return;
    const current = (catalog?.matches ?? []).find(
      (candidate) => matchKey(candidate) === matchKey(selected),
    );
    if (!current) return;
    setPinDisagreements(null);
    inspect(current);
  }, [catalog, inspect, selected]);

  const run = useCallback(async () => {
    if (!selected || !selected.common_key || !selected.explore_together) return;
    runAbort.current?.abort();
    projectionAbort.current?.abort();
    const controller = new AbortController();
    runAbort.current = controller;
    const generation = runGeneration.current + 1;
    runGeneration.current = generation;
    visualizationGeneration.current += 1;
    visualizationAbort.current?.abort();
    visualizationAbort.current = null;
    setBuildingVisualization(false);
    setRunning(true);
    setRunError(null);
    setRunAdvice(null);
    try {
      const selectedQuestion = goldenQuestions.find(
        (question) => question.current_version_id === goldenQuestionVersionId,
      );
      const selectedQuestionVersion = selectedQuestion?.current_version;
      const compiled = await compilePlan(projectId, {
        profile_receipts: [profile, ...Object.values(additionalProfiles)]
          .flatMap((entry) => entry?.profile_receipt ? [entry.profile_receipt] : []),
        members: graphSides.map((side) => ({
          datastream_id: side.datastream_id,
          mapping_version_id: side.mapping_version_id,
          published_execution_id: side.published_execution_id,
          output_version_id: side.output_version_id,
          measures: side.measures
            .filter((measure) =>
              selectedMeasures.includes(measureKey(side.datastream_id, measure.canonical_field_id)),
            )
            .map((measure) => ({ canonical_field_id: measure.canonical_field_id })),
        })),
        edges: graphMatches.map((match) => ({
          left: match.left.datastream_id,
          right: match.right.datastream_id,
          common_key_version_id: match.common_key!.version_id,
          relationship_name: match.explore_together!.relationship_name,
          view_version_id: match.explore_together!.view_version_id,
        })),
        inclusion_policy: inclusion,
        primary_datastream_id: selected.left.datastream_id,
        dimensions: Array.from(
          new Set([...rowFields, ...columnFields, grainField].filter(Boolean)),
        ).map((canonical_field_id) => ({ canonical_field_id })),
        grain: grainField || undefined,
        comparison,
        filters: [
          ...(filterField && filterValue ? [{
              stage: "post_aggregation",
              canonical_field_id: filterField,
              operator: "eq",
              value: filterValue,
            }] : []),
          ...graphSides.flatMap((side) => [
            ...(grainField && windowStart ? [{
              stage: "pre_aggregation",
              datastream_id: side.datastream_id,
              canonical_field_id: grainField,
              operator: "gte",
              value: windowStart,
            }] : []),
            ...(grainField && windowEnd ? [{
              stage: "pre_aggregation",
              datastream_id: side.datastream_id,
              canonical_field_id: grainField,
              operator: "lte",
              value: windowEnd,
            }] : []),
          ]),
        ],
        row_limit: Number(rowLimit),
        ...(
          selectedQuestionVersion || businessDomainVersionId || skillVersionIds.length
            ? {
                analysis_context: {
                  ...(selectedQuestionVersion && selectedQuestion?.current_version_id
                    ? {
                        business_domain_id: selectedQuestionVersion.business_domain_id,
                        business_domain_version_number:
                          selectedQuestionVersion.business_domain_version_number,
                        golden_question_version_id: selectedQuestion.current_version_id,
                      }
                    : businessDomainVersionId
                      ? {
                          business_domain_id: businessDomainVersionId.split("@")[0],
                          business_domain_version_number: Number(
                            businessDomainVersionId.split("@")[1],
                          ),
                        }
                      : {}),
                  skill_version_ids: skillVersionIds,
                },
              }
            : {}
        ),
        pivot: {
          rows: rowFields.map((canonical_field_id) => ({ canonical_field_id })),
          columns: columnFields.map((canonical_field_id) => ({ canonical_field_id })),
          values: graphSides.flatMap((side) =>
            side.measures
              .filter((measure) =>
                selectedMeasures.includes(
                  measureKey(side.datastream_id, measure.canonical_field_id),
                ),
              )
              .map((measure) => ({
                datastream_id: side.datastream_id,
                canonical_field_id: measure.canonical_field_id,
              })),
          ),
          filters: filterField && filterValue
            ? [{ canonical_field_id: filterField, in: [filterValue] }]
            : [],
          subtotals: comparison === "none" && subtotals,
          grand_total: comparison === "none" ? grandTotal : "none",
          row_sort: rowSort,
          column_sort: "asc",
        },
      }, controller.signal);
      const receipt = await executePlan(projectId, compiled.query_spec_version_id, controller.signal);
      if (runGeneration.current !== generation) return;
      setResultId(receipt.result.result_id);
      setQuerySpecVersionId(compiled.query_spec_version_id);
      setAnalysisContext(
        ((compiled.plan as { analysis_context?: FrozenAnalysisContext }).analysis_context) ?? null,
      );
      const compiledMembers = ((compiled.plan as { members?: CompiledMember[] }).members ?? []);
      const values = compiledMembers.flatMap((member) =>
        member.measures.map((measure) => measure.result_field ?? `m_${measure.canonical_field_id}`),
      );
      const labels: Record<string, string> = {};
      labels.k_comparison_period = "Period";
      for (const member of compiledMembers) {
        const side = graphSides.find((candidate) => candidate.datastream_id === member.datastream_id);
        if (!side) continue;
        for (const measure of member.measures) {
          const source = side.measures.find(
            (candidate) => candidate.canonical_field_id === measure.canonical_field_id,
          );
          const resultField = measure.result_field ?? `m_${measure.canonical_field_id}`;
          // THE PLAN CARRIES THE WORD. A measure the match catalogue does not
          // hold is still named by the plan that froze it; only a plan compiled
          // before that word existed has neither, and the legend then says the
          // source alone rather than printing a canonical id.
          const word = source?.name ?? measure.canonical_name;
          labels[resultField] = word ? `${word} — ${side.name}` : side.name;
        }
      }
      setCompiledFieldLabels(labels);
      setResultValueFields(values);
      const evidence = await fetchResultEvidence(
        projectId,
        receipt.result.result_id,
        { signal: controller.signal },
      );
      if (runGeneration.current !== generation) return;
      setResultEvidence(evidence);
      setExecutedDimensions(
        Array.from(new Set([...rowFields, ...columnFields, grainField].filter(Boolean))),
      );
      setExecutedMeasures([...selectedMeasures]);
      const answer = await pivotResult(projectId, receipt.result.result_id, {
        rows: rowFields.map((fieldId) => `k_${fieldId}`),
        columns: columnFields.map((fieldId) => `k_${fieldId}`),
        values,
        filters: filterField && filterValue
          ? [{ field: `k_${filterField}`, in: [filterValue] }]
          : [],
        subtotals: comparison === "none" && subtotals,
        grand_total: comparison === "none" ? grandTotal : "none",
        row_sort: rowSort,
      }, controller.signal);
      if (runGeneration.current !== generation) return;
      setMatrix(answer.pivot);
      setPresentation("pivot");
    } catch (error: unknown) {
      if (controller.signal.aborted || runGeneration.current !== generation) return;
      setRunError(errorMessage(error));
      setRunAdvice(narrowingAdvice(error));
    } finally {
      if (runGeneration.current === generation) {
        setRunning(false);
        runAbort.current = null;
      }
    }
  }, [
    projectId,
    selected,
    profile,
    graphMatches,
    graphSides,
    additionalProfiles,
    inclusion,
    rowFields,
    columnFields,
    selectedMeasures,
    filterField,
    filterValue,
    subtotals,
    grandTotal,
    grainField,
    comparison,
    windowStart,
    windowEnd,
    rowLimit,
    rowSort,
    goldenQuestions,
    goldenQuestionVersionId,
    skillVersionIds,
  ]);

  /**
   * The request that asks for one remembered page.
   *
   * A signed continuation when there is one — it is bound to this Result and
   * this matrix, so it cannot silently serve a page of another question — and
   * the bare offsets only as the fallback for a page nobody minted a token for.
   */
  const pageRequest = useCallback(
    (
      page: string | number | null | undefined,
      axis: "row" | "column",
      otherOffset: number,
    ): { row_offset?: number; column_offset?: number; cursor?: string } => {
      // THE OTHER AXIS TRAVELS EVEN WITH A TOKEN. A remembered page is the token
      // of the page that was LEFT, and it carries BOTH offsets as they stood
      // then — so after a move on the other axis it is stale by construction.
      // Returning `{cursor}` alone restored both, and the reader lost a column
      // they never asked to leave. The server lets an explicit offset override
      // the token's on that axis, because the offsets are the token's payload
      // and not its identity.
      if (typeof page === "string") {
        return axis === "row"
          ? { cursor: page, column_offset: otherOffset }
          : { cursor: page, row_offset: otherOffset };
      }
      const offset = typeof page === "number" ? page : 0;
      return axis === "row"
        ? { row_offset: offset, column_offset: otherOffset }
        : { row_offset: otherOffset, column_offset: offset };
    },
    [],
  );

  const project = useCallback(
    async (
      next: Presentation,
      /* A signed `cursor` when the server minted one, the bare offsets only for
         a page this screen has to reconstruct itself. See AC 9. */
      offsets: { row_offset?: number; column_offset?: number; cursor?: string } = {},
      history?: { axis: "row" | "column"; action: "push" | "pop"; value?: string | number },
      /** The axes to project ON, when a move has just happened and React has
       *  not yet re-rendered with them. Same Result, same content hash: this is
       *  the re-projection the amendment separates from an analytical edit. */
      axes?: { rows: string[]; columns: string[] },
    ) => {
      setPresentation(next);
      if (next !== "pivot" || !resultId) {
        projectionGeneration.current += 1;
        projectionAbort.current?.abort();
        projectionAbort.current = null;
        setProjecting(false);
        setRunError(null);
        return;
      }
      if (
        offsets.row_offset === undefined
        && offsets.column_offset === undefined
        && offsets.cursor === undefined
      ) {
        rowPageHistory.current = [];
        columnPageHistory.current = [];
      }
      projectionAbort.current?.abort();
      const controller = new AbortController();
      projectionAbort.current = controller;
      const generation = projectionGeneration.current + 1;
      projectionGeneration.current = generation;
      setProjecting(true);
      setRunError(null);
      try {
        const answer = await pivotResult(projectId, resultId, {
          rows: (axes?.rows ?? rowFields).map((fieldId) => `k_${fieldId}`),
          columns: (axes?.columns ?? columnFields).map((fieldId) => `k_${fieldId}`),
          values: resultValueFields,
          filters: filterField && filterValue
            ? [{ field: `k_${filterField}`, in: [filterValue] }]
            : [],
          subtotals: comparison === "none" && subtotals,
          grand_total: comparison === "none" ? grandTotal : "none",
          row_sort: rowSort,
          ...offsets,
        }, controller.signal);
        if (projectionGeneration.current !== generation) return;
        setMatrix(answer.pivot);
        if (history?.axis === "row") {
          if (history.action === "push") rowPageHistory.current.push(history.value ?? 0);
          else rowPageHistory.current.pop();
        } else if (history?.axis === "column") {
          if (history.action === "push") columnPageHistory.current.push(history.value ?? 0);
          else columnPageHistory.current.pop();
        }
      } catch (error: unknown) {
        if (controller.signal.aborted || projectionGeneration.current !== generation) return;
        // A refused projection keeps the previous evidence on screen.
        setRunError(errorMessage(error));
      } finally {
        if (projectionGeneration.current === generation) {
          projectionAbort.current = null;
          setProjecting(false);
        }
      }
    },
    [
      projectId,
      resultId,
      rowFields,
      columnFields,
      resultValueFields,
      filterField,
      filterValue,
      comparison,
      subtotals,
      grandTotal,
      rowSort,
    ],
  );

  // --- the Composition region, composed by hand ------------------------------
  //
  // `visualization-and-rendering.md`, *Composition is direct manipulation*: a
  // field is moved into a well, between wells and within a well, and the screen
  // says which of the two consequences the move had. Everything below is that
  // rule and nothing else -- no compatibility logic (roles come from the shape
  // of the object: a measure of a Datastream, a component of the common key)
  // and no aggregation.

  /** The grain cannot also group an axis while a comparison is pinned: the
   *  comparison already holds it. Stated in the shelf rather than by a field
   *  that silently disappears. */
  const grainHeldByComparison = comparison !== "none" && Boolean(grainField);

  const dimensionFields = useMemo<WellField[]>(
    () => graphComponents
      .filter((component) => !grainHeldByComparison || component.canonical_field_id !== grainField)
      .map((component) => ({
        id: component.canonical_field_id,
        label: component.canonical_name,
        role: "dimension",
        hint: component.canonical_field_id === grainField ? "time grain" : "common key",
      })),
    [graphComponents, grainField, grainHeldByComparison],
  );

  const measureFields = useMemo<WellField[]>(
    () => graphSides.flatMap((side) => side.measures.map((measure) => ({
      id: measureKey(side.datastream_id, measure.canonical_field_id),
      label: measure.name,
      role: "measure",
      hint: `${side.name} · ${measure.aggregation}`,
    }))),
    [graphSides],
  );

  const compositionFields = useMemo(
    () => [...measureFields, ...dimensionFields],
    [dimensionFields, measureFields],
  );

  const compositionWells = useMemo<WellSpec[]>(() => [
    {
      name: "values",
      label: "Values",
      accepts: ["measure"],
      required: true,
      hint: "The figures every cell holds. Each is served by its own measure contract.",
    },
    {
      name: "rows",
      label: "Rows",
      accepts: ["dimension"],
      hint: "Grouping down the table. Order nests: the first field is the outer level.",
    },
    {
      name: "columns",
      label: "Columns",
      accepts: ["dimension"],
      hint: "Grouping across the table. Order nests the same way.",
    },
  ], []);

  const compositionBindings = useMemo<WellBindings>(
    () => ({ values: selectedMeasures, rows: rowFields, columns: columnFields }),
    [columnFields, rowFields, selectedMeasures],
  );

  /** What is not on an axis yet. A field already placed is not offered twice:
   *  the shelf is what is left to place, so "what is bound where" reads at a
   *  glance instead of being deduced from a list that never changes. */
  const shelfFields = useMemo(
    () => compositionFields.filter((field) => !(
      selectedMeasures.includes(field.id)
      || rowFields.includes(field.id)
      || columnFields.includes(field.id)
    )),
    [columnFields, compositionFields, rowFields, selectedMeasures],
  );

  const composedDimensions = useMemo(
    () => Array.from(new Set([...rowFields, ...columnFields, grainField].filter(Boolean))),
    [columnFields, grainField, rowFields],
  );

  /** The composition has drifted from the Result on screen. The figures are NOT
   *  repainted: they belong to the Result that produced them, and the screen
   *  names the gesture that produces the next one. */
  const needsNewResult = Boolean(resultId) && !(
    sameMembers(composedDimensions, executedDimensions)
    && sameMembers(selectedMeasures, executedMeasures)
  );

  const composeChange = useCallback(
    (next: WellBindings, change: WellChange) => {
      const nextValues = next.values ?? [];
      let nextRows = next.rows ?? [];
      let nextColumns = next.columns ?? [];
      // A dimension lives on ONE axis. Placing it on the other moves it there
      // rather than grouping by it twice, which the projection would refuse.
      if (change.to === "rows") nextColumns = nextColumns.filter((id) => !nextRows.includes(id));
      if (change.to === "columns") nextRows = nextRows.filter((id) => !nextColumns.includes(id));
      setSelectedMeasures(nextValues);
      setRowFields(nextRows);
      setColumnFields(nextColumns);

      // THE TWO CONSEQUENCES. Rearranging what the Result was already executed
      // over re-projects it: same Result id, same content hash, no query. Adding
      // or removing a member is analytical and waits to be run.
      const stillTheSameResult = Boolean(resultId)
        && sameMembers(
          Array.from(new Set([...nextRows, ...nextColumns, grainField].filter(Boolean))),
          executedDimensions,
        )
        && sameMembers(nextValues, executedMeasures);
      if (stillTheSameResult) {
        void project("pivot", {}, undefined, { rows: nextRows, columns: nextColumns });
      }
    },
    [executedDimensions, executedMeasures, grainField, project, resultId],
  );

  const cancelRun = useCallback(() => {
    runGeneration.current += 1;
    runAbort.current?.abort();
    runAbort.current = null;
    setRunning(false);
    setRunError("The analysis was cancelled. No partial Result was shown.");
  }, []);

  const openChart = useCallback(async (templateVersionId?: string) => {
    if (!resultId || !querySpecVersionId) return;
    const generation = visualizationGeneration.current + 1;
    visualizationGeneration.current = generation;
    visualizationAbort.current?.abort();
    const controller = new AbortController();
    visualizationAbort.current = controller;
    setPresentation("chart");
    setBuildingVisualization(true);
    setRunError(null);
    try {
      //  THE STARTING POINT IS A CHOICE NOW (story 72.5, AC22). `Chart & save`
      //  used to seed the `table` family because a constant said so; a Chart
      //  Template of this Project that fits this Result is offered first, and
      //  the fallback is what the control below now says it is.
      const visualizationId = await createVisualizationFromResult(
        projectId,
        querySpecVersionId,
        { signal: controller.signal },
        templateVersionId ? { templateVersionId, resultId } : undefined,
      );
      if (visualizationGeneration.current !== generation) return;
      if (onOpenVisualization) onOpenVisualization(visualizationId, resultId);
      else setRunError("The visualization was saved, but this host exposes no Builder route.");
    } catch (error: unknown) {
      if (!controller.signal.aborted && visualizationGeneration.current === generation) {
        setRunError(errorMessage(error));
      }
    } finally {
      if (visualizationGeneration.current === generation) {
        visualizationAbort.current = null;
        setBuildingVisualization(false);
      }
    }
  }, [onOpenVisualization, projectId, querySpecVersionId, resultId]);

  const governed = useMemo(
    () => (catalog?.matches ?? []).filter((match) => match.authority === "governed"),
    [catalog],
  );
  const candidates = useMemo(
    () => (catalog?.matches ?? []).filter((match) => match.authority !== "governed"),
    [catalog],
  );
  const fieldLabels = useMemo(() => {
    if (!selected) return {};
    const labels: Record<string, string> = {};
    for (const match of graphMatches) {
      for (const component of match.common_key?.components ?? []) {
        labels[`k_${component.canonical_field_id}`] = component.canonical_name;
      }
    }
    for (const side of graphSides) {
      for (const measure of side.measures) labels[`m_${measure.canonical_field_id}`] = measure.name;
    }
    return { ...labels, ...compiledFieldLabels };
  }, [selected, graphMatches, graphSides, compiledFieldLabels]);
  const selectedGoldenQuestion = useMemo(
    () => goldenQuestions.find(
      (question) => question.current_version_id === goldenQuestionVersionId,
    ) ?? null,
    [goldenQuestionVersionId, goldenQuestions],
  );
  const compatibleBusinessDomains = useMemo(() => {
    const edgeRefs = graphMatches.map(
      (match) => new Set(match.relationship?.business_domain_refs ?? []),
    );
    const refs = new Set(
      [...(edgeRefs[0] ?? new Set<string>())].filter(
        (ref) => edgeRefs.every((candidate) => candidate.has(ref)),
      ),
    );
    const options = new Map<string, AnalysisBusinessDomain>();
    for (const domain of businessDomains) {
      if (refs.has(domain.id)) options.set(`${domain.id}@${domain.version_number}`, domain);
    }
    for (const question of goldenQuestions) {
      const version = question.current_version;
      if (!version || version.semantic_view_version_id !== selected?.explore_together?.view_version_id
        || !refs.has(version.business_domain_id)) continue;
      const key = `${version.business_domain_id}@${version.business_domain_version_number}`;
      if (!options.has(key)) {
        // THE NAME COMES FROM THE SERVER, and there is no third choice.
        // This read used to end on `?? version.business_domain_id`, which put
        // `bd_01KZ...` in a picker the moment a Golden Question pinned a domain
        // the org list does not carry. The pin's own name is served with it now
        // (`golden_questions.list_golden_questions`); when the domain was
        // deleted the server sends null, and a Business Domain nobody can name
        // is not offered as a choice rather than offered as a ULID.
        const word = businessDomains.find(
          (domain) => domain.id === version.business_domain_id,
        )?.name ?? version.business_domain_name;
        if (!word) continue;
        options.set(key, {
          id: version.business_domain_id,
          name: word,
          version_number: version.business_domain_version_number,
        });
      }
    }
    return [...options.values()].sort((left, right) =>
      left.name.localeCompare(right.name) || left.version_number - right.version_number);
  }, [businessDomains, goldenQuestions, graphMatches, selected]);
  const selectedBusinessDomain = useMemo(
    () => compatibleBusinessDomains.find(
      (domain) => `${domain.id}@${domain.version_number}` === businessDomainVersionId,
    ) ?? null,
    [businessDomainVersionId, compatibleBusinessDomains],
  );

  useEffect(() => {
    if (businessDomainVersionId && !selectedBusinessDomain) {
      setBusinessDomainVersionId("");
      setGoldenQuestionVersionId("");
    }
  }, [businessDomainVersionId, selectedBusinessDomain]);

  return (
    // ONE composer for the whole screen, not one per region. A field picked up
    // in the Composition region has to be droppable on the pivot's own axes and
    // back, and two providers would make that gesture stop at a region boundary
    // the person cannot see.
    <FieldComposer
      fields={compositionFields}
      wells={compositionWells}
      bindings={compositionBindings}
      onChange={composeChange}
    >
    <Stack className="gap-6" data-owner="analyze/explore" data-testid="analytics-explorer">
      <SectionHeader
        title="Sources you can cross"
        description="Published Datastreams of this Project, and the governed crosses they take part in."
      />

      {catalogError ? (
        <Status as="block" tone="error" title="The catalog of sources could not be read" data-testid="catalog-error"
          action={<Retry onClick={() => void readCurrentVersions()} />}
        >
          {catalogError} This is not a count of zero — nothing has been ruled out.
        </Status>
      ) : null}

      {/* THE PINNED THING IS NO LONGER THE CURRENT ONE, AND THAT IS SAID.
          Nothing is re-resolved: the analysis stays on the versions the
          Datastream Workbench handed over, and moving to what is published now
          is a gesture this offers rather than one it performs. */}
      {pinDisagreements && pinDisagreements.length > 0 ? (
        <Status
          as="block"
          tone="warning"
          title="This cross has been republished since it was opened"
          data-testid="pinned-versions-superseded"
        >
          <Stack className="gap-2">
            <p className="m-0">
              The analysis below reads the versions that were on the Datastream when you
              chose to explore it, not what is published now.
            </p>
            <ul className="m-0 pl-4">
              {pinDisagreements.map((drift) => (
                <li key={`${drift.datastreamId}:${drift.what}`}>
                  {sourceName(drift.datastreamId) ?? drift.datastreamId}: {drift.what}{" "}
                  {drift.pinned} was read; {drift.current} is published now.
                </li>
              ))}
            </ul>
            <div>
              <Button type="button" onClick={readCurrentVersions} data-testid="read-current-versions">
                Read what is published now
              </Button>
            </div>
          </Stack>
        </Status>
      ) : null}

      {catalog && catalog.matches.length === 0 && catalog.empty_reason ? (
        <Panel>
          <EmptyState
            title="No source can be crossed yet"
            description={catalog.empty_reason.message}
          />
        </Panel>
      ) : null}

      {catalog && catalog.matches.length > 0 ? (
        <Panel data-testid="useful-matches">
          <PanelHeader
            title="Useful matches"
            description={`${catalog.counts.governed} approved, ${catalog.counts.candidates} awaiting governance, over ${catalog.counts.datastreams_published} published sources.`}
          />
          <Stack className="gap-4">
            {[...governed, ...candidates].map((match) => (
              <Panel
                key={matchKey(match)}
                className="gap-3"
                data-testid={`match-${match.kind}`}
              >
                <p className="text-body">{match.analysis}</p>
                <MatchStates match={match} />
                {match.key_paths.length > 0 ? (
                  <p className="text-body-sm text-text-secondary">
                    Matched on{" "}
                    {match.key_paths
                      .map(
                        (path) =>
                          `${path.canonical_name ?? "an unnamed field"} (${path.left_field} ↔ ${path.right_field})`,
                      )
                      .join(", ")}
                    {match.relationship
                      ? ` — ${match.relationship.relationship_name}, ${match.relationship.cardinality}, fan-out ${match.relationship.fan_out_policy}`
                      : ""}
                  </p>
                ) : null}
                {match.next_action ? (
                  <Status as="block" tone="warning" title="Not available yet">
                    {match.next_action}
                  </Status>
                ) : (
                  <Cluster className="gap-2">
                    {canExtendGraph(graphMatches, match) ? (
                      <Button
                        type="button"
                        onClick={() => void addSource(match)}
                        disabled={
                          !profile
                          || addingMatchKey !== null
                          || running
                          || projecting
                          || buildingVisualization
                        }
                        data-testid="add-source"
                      >
                        {addingMatchKey === matchKey(match)
                          ? "Measuring the network connection…"
                          : "Add the connected source"}
                      </Button>
                    ) : graphMatches.some((edge) => matchKey(edge) === matchKey(match)) ? (
                      <Status as="inline" tone="success" title="Selected path">Included</Status>
                    ) : (
                      <Button
                        type="button"
                        onClick={() => {
                          // Picking a match by hand is a reading of the catalog,
                          // so the handoff's drift no longer describes what is
                          // on screen and must not be left standing over it.
                          setPinDisagreements(null);
                          inspect(match);
                        }}
                        disabled={running || projecting || buildingVisualization}
                        data-testid="inspect-match"
                      >
                        Inspect the match
                      </Button>
                    )}
                  </Cluster>
                )}
              </Panel>
            ))}
          </Stack>
        </Panel>
      ) : null}

      {selected ? (
        <Panel data-testid="composition">
          <PanelHeader
            title="What this cross would do to the rows"
            description="Measured on the published outputs, before anything is executed."
          />
          {profileError ? (
            <Status as="block" tone="warning" title="The match could not be measured">
              {profileError} Nothing here is a count of zero.
            </Status>
          ) : null}
          {profile ? <ProfileEvidence profile={profile} /> : null}

          <Panel data-testid="selected-source-graph" className="gap-3">
            <PanelHeader
              title={`${graphSides.length} connected Datastreams`}
              description="Every edge below is an approved, oriented MDM relationship; the browser never invents a join."
            />
            <Stack className="gap-2">
              {graphMatches.map((match, index) => (
                <div key={matchKey(match)} className="rounded-lg border border-divider-base p-3">
                  <p className="text-body font-medium">
                    {index + 1}. {match.left.name}{index === 0 ? " (Primary)" : ""} → {match.right.name}
                  </p>
                  <p className="text-body-sm text-text-secondary">
                    {match.common_key?.name} · {match.explore_together?.relationship_name} · {match.relationship?.cardinality}
                  </p>
                  {index > 0 && additionalProfiles[matchKey(match)] ? (
                    <p className="text-body-sm">
                      {SAFETY_WORDS[additionalProfiles[matchKey(match)].execution_safety]}
                    </p>
                  ) : null}
                  {governedAlternatives(catalog, match).length ? (
                    <Field
                      label={`Relationship for ${match.left.name} to ${match.right.name}`}
                      hint="Only approved paths for this exact oriented pair and Semantic View."
                    >
                      {(field) => <NativeSelect
                        {...field}
                        value={matchKey(match)}
                        disabled={
                          !graphProfilesReady
                          || running
                          || projecting
                          || buildingVisualization
                          || addingMatchKey !== null
                        }
                        onChange={(event) => {
                          const replacement = governedAlternatives(catalog, match).find(
                            (candidate) => matchKey(candidate) === event.target.value,
                          );
                          if (replacement) void switchRelationship(match, replacement);
                        }}
                      >
                        {[match, ...governedAlternatives(catalog, match)].map((candidate) => (
                          <option key={matchKey(candidate)} value={matchKey(candidate)}>
                            {candidate.explore_together?.relationship_name}
                            {candidate.common_key ? ` · ${candidate.common_key.name}` : ""}
                          </option>
                        ))}
                      </NativeSelect>}
                    </Field>
                  ) : null}
                  {graphLeaves.filter((leaf) => leaf.edgeKey === matchKey(match)).length ? (
                    <div className="mt-2 flex flex-wrap gap-2">
                      {graphLeaves
                        .filter((leaf) => leaf.edgeKey === matchKey(match))
                        .map((leaf) => (
                          <Button
                            key={leaf.sourceId}
                            type="button"
                            variant="secondary"
                            disabled={
                              running
                              || projecting
                              || buildingVisualization
                              || addingMatchKey !== null
                              || !graphProfilesReady
                            }
                            onClick={() => removeLeaf(leaf)}
                            data-testid={`remove-leaf-${leaf.sourceId}`}
                            aria-label={
                              `Remove ${leaf.sourceName} from ${match.explore_together?.relationship_name}`
                            }
                          >
                            Remove {leaf.sourceName}
                          </Button>
                        ))}
                    </div>
                  ) : null}
                </div>
              ))}
              {additionalMatches.length ? (
                <Button
                  type="button"
                  variant="secondary"
                  disabled={running || projecting || buildingVisualization}
                  onClick={() => {
                    if (running || projecting || buildingVisualization) return;
                    profileGeneration.current += 1;
                    projectionGeneration.current += 1;
                    projectionAbort.current?.abort();
                    projectionAbort.current = null;
                    setProjecting(false);
                    visualizationGeneration.current += 1;
                    visualizationAbort.current?.abort();
                    visualizationAbort.current = null;
                    setBuildingVisualization(false);
                    addingMatchKeyRef.current = null;
                    setAddingMatchKey(null);
                    setAdditionalMatches([]);
                    setAdditionalProfiles({});
                    setSelectedMeasures(
                      [selected.left, selected.right].flatMap((side) =>
                        side.measures.map((measure) =>
                          measureKey(side.datastream_id, measure.canonical_field_id),
                        ),
                      ),
                    );
                    const components = selected.common_key?.components ?? [];
                    setRowFields(components[0] ? [components[0].canonical_field_id] : []);
                    setColumnFields(components[1] ? [components[1].canonical_field_id] : []);
                    setFilterField(components[0]?.canonical_field_id ?? "");
                    setFilterValue("");
                    const likelyTime = components.find((component) =>
                      /(^|[_ -])(day|date|time|month|week|year)($|[_ -])/i.test(
                        component.canonical_name,
                      ),
                    );
                    setGrainField(likelyTime?.canonical_field_id ?? "");
                    setWindowStart("");
                    setWindowEnd("");
                    setComparison("none");
                    setResultId(null);
                    setQuerySpecVersionId(null);
                    setResultEvidence(null);
                    setMatrix(null);
                    setAnalysisContext(null);
                    setCompiledFieldLabels({});
                  }}
                >
                  Return to the first two sources
                </Button>
              ) : null}
            </Stack>
          </Panel>

          <Stack className="gap-3">
            <fieldset
              className="grid gap-4 rounded-lg border border-divider-base p-4"
              data-testid="analysis-context"
            >
              <legend className="text-body-sm">Business question and playbook</legend>
              <p className="text-body-sm text-text-secondary">
                Optional exact pins. They travel with the plan, Result and visualization;
                Skills actually used remain separate evidence on the AI Path.
              </p>
              {contextOptionsUnavailable ? (
                <Status as="block" tone="warning" title="Some context choices are unavailable">
                  You can still explore the data without inventing a Business Domain, Golden
                  Question or Skill.
                </Status>
              ) : null}
              <div className="grid gap-4 md:grid-cols-3">
                <Field label="Business Domain" hint="Pins the exact governed business version.">
                  {(field) => <NativeSelect
                    {...field}
                    value={businessDomainVersionId}
                    onChange={(event) => {
                      const next = event.target.value;
                      setBusinessDomainVersionId(next);
                      const question = goldenQuestions.find(
                        (candidate) => candidate.current_version_id === goldenQuestionVersionId,
                      );
                      const current = question?.current_version;
                      if (current && `${current.business_domain_id}@${current.business_domain_version_number}`
                        !== next) {
                        setGoldenQuestionVersionId("");
                      }
                    }}
                  >
                    <option value="">No Business Domain pin</option>
                    {compatibleBusinessDomains.map((domain) => (
                      <option key={`${domain.id}@${domain.version_number}`} value={`${domain.id}@${domain.version_number}`}>
                        {domain.name} · v{domain.version_number}
                      </option>
                    ))}
                  </NativeSelect>}
                </Field>
                <Field label="Golden Question" hint="Pins its exact current version and Business Domain.">
                  {(field) => <NativeSelect
                    {...field}
                    value={goldenQuestionVersionId}
                    onChange={(event) => {
                      const next = event.target.value;
                      setGoldenQuestionVersionId(next);
                      const question = goldenQuestions.find(
                        (candidate) => candidate.current_version_id === next,
                      );
                      if (question?.current_version) {
                        setBusinessDomainVersionId(
                          `${question.current_version.business_domain_id}@${question.current_version.business_domain_version_number}`,
                        );
                      }
                    }}
                  >
                    <option value="">Free exploration</option>
                    {goldenQuestions
                      .filter(
                        (question) =>
                          question.current_version_id
                          && question.current_version
                          && question.current_version.semantic_view_version_id
                            === selected.explore_together?.view_version_id
                          && compatibleBusinessDomains.some(
                            (domain) => domain.id === question.current_version?.business_domain_id,
                          )
                          && (!businessDomainVersionId
                            || `${question.current_version?.business_domain_id}@${question.current_version?.business_domain_version_number}`
                              === businessDomainVersionId),
                      )
                      .map((question) => (
                        <option key={question.current_version_id!} value={question.current_version_id!}>
                          {question.title} · v{question.current_version?.version_number}
                        </option>
                      ))}
                  </NativeSelect>}
                </Field>
                <fieldset className="grid content-start gap-2">
                  <legend className="text-ui font-medium">Requested Skills</legend>
                  <p className="text-caption text-text-secondary">
                    Choose up to 8 exact playbook versions. Observed use is proven later.
                  </p>
                  <div
                    className="grid max-h-32 gap-2 overflow-y-auto rounded-md border border-divider-base p-2"
                    aria-label="Requested Skills"
                  >
                    {skills.filter((skill) => skill.version_number).length ? skills
                      .filter((skill) => skill.version_number)
                      .map((skill) => {
                        const versionId = `${skill.id}@${skill.version_number}`;
                        const checked = skillVersionIds.includes(versionId);
                        return (
                          <label key={versionId} className="flex items-center gap-2 text-body-sm">
                            <Checkbox
                              checked={checked}
                              disabled={!checked && skillVersionIds.length >= 8}
                              onCheckedChange={(next) => setSkillVersionIds((current) => (
                                next === true
                                  ? current.includes(versionId) || current.length >= 8
                                    ? current
                                    : [...current, versionId]
                                  : current.filter((candidate) => candidate !== versionId)
                              ))}
                            />
                            <span>{skill.name ?? skill.id} · v{skill.version_number}</span>
                          </label>
                        );
                      }) : (
                        <span className="text-body-sm text-text-secondary">No active Skills.</span>
                      )}
                  </div>
                  <span className="text-caption text-text-secondary">
                    {skillVersionIds.length} of 8 selected
                  </span>
                </fieldset>
              </div>
              {selectedBusinessDomain || selectedGoldenQuestion?.current_version ? (
                <p className="text-body-sm text-text-secondary" data-testid="selected-business-domain">
                  {/* THE NAME, OR THE HONEST ABSENCE -- never the identifier.
                      This clause fell back to `business_domain_id` and printed
                      "Business Domain bd_01KZ... · v3" at a reader. The name is
                      served with the pin now, and when the Domain was deleted
                      after the Golden Question froze its version the sentence
                      says which version is pinned and that nothing names it any
                      more, which is what actually happened. */}
                  Business Domain {selectedBusinessDomain?.name
                    ?? selectedGoldenQuestion?.current_version?.business_domain_name
                    ?? "no longer in this organization"} · v
                  {selectedBusinessDomain?.version_number
                    ?? selectedGoldenQuestion?.current_version?.business_domain_version_number}
                  {/* THE RELATION HAS A NAME, so this line prints it. It read
                      "Semantic View svv_01KZ..." -- an identifier where a person
                      expects a word, the same defect story 66.8 arbitrated for the
                      Builder rail: "a legend reading `mdm_01KZ...` is a legend
                      nobody can use". The view's own name is not in this payload,
                      so what is named is what is: the relationship the two sources
                      are matched on, and the view version pinned to it.

                      THE WHOLE CLAUSE IS CONDITIONAL, and that is the repair of
                      2026-08-21. Only the NAME was guarded, so a single-source
                      selection -- `explore_together` null -- printed "matched on
                      the  relationship" with the word missing and two spaces
                      where it had been. A hole in a sentence is no better than
                      the identifier it replaced: this says what is true, or it
                      says nothing. */}
                  {selected.explore_together?.relationship_name ? (
                    <>
                      ; matched on the {selected.explore_together.relationship_name} relationship
                      {selected.relationship?.view_version_number
                        ? `, Semantic View v${selected.relationship.view_version_number}`
                        : ""}
                    </>
                  ) : null}
                  .
                </p>
              ) : null}
            </fieldset>
            {/* THE COMPOSITION IS MADE OF MOVES, not of a checkbox list and two
                menus (Jean, 2026-08-15; `visualization-and-rendering.md`,
                *Composition is direct manipulation*). The shelf on the left is
                what is left to place; the three wells on the right are what is
                bound, in order. Every gesture is available to the pointer as a
                drag and to the keyboard as the same buttons. */}
            <div className="grid gap-4 lg:grid-cols-[minmax(0,14rem)_minmax(0,1fr)]">
              <section
                aria-labelledby="composition-shelf"
                className="flex flex-col gap-2"
                data-testid="composition-shelf"
              >
                <h4 id="composition-shelf" className="m-0 text-label font-label text-text">
                  Fields
                </h4>
                <p className="m-0 text-caption text-text-secondary">
                  Pick one up, then place it in a well — or drag it there.
                </p>
                {/* The shelf itself takes a placed field back: dropping one here
                    unbinds it, the same consequence as its remove button. */}
                <FieldShelf testId="composition-shelf-drop">
                  {shelfFields.length === 0 ? (
                    <p className="m-0 text-caption text-text-secondary" data-testid="composition-shelf-empty">
                      Every field of this match is placed. Take one out of a well, or drop
                      one here, to get it back.
                    </p>
                  ) : (
                    <ul className="m-0 flex max-h-72 list-none flex-col gap-1.5 overflow-y-auto p-0">
                      {shelfFields.map((field) => (
                        <li key={field.id}>
                          <DraggableField field={field} compact className="w-full" />
                        </li>
                      ))}
                    </ul>
                  )}
                </FieldShelf>
                {grainHeldByComparison && (
                  <p className="m-0 text-caption text-text-secondary" data-testid="grain-held-by-comparison">
                    The comparison holds the time grain on its own axis, so it cannot also
                    group rows or columns. Set the comparison to none to place it.
                  </p>
                )}
              </section>

              <div className="grid gap-3 md:grid-cols-3">
                {compositionWells.map((well) => (
                  <FieldWell
                    key={well.name}
                    well={well}
                    consequence={
                      well.name === "values"
                        ? "Changing the Values runs a new analysis."
                        : "Moving a placed field between Rows and Columns re-projects the same Result."
                    }
                  />
                ))}
              </div>
            </div>

            {needsNewResult && (
              <Status as="block" tone="warning" title="This composition needs a new Result" data-testid="composition-stale">
                The figures below belong to the Result that was executed, and they have not
                been recomputed. Run the analysis to answer the composition on screen.
              </Status>
            )}
            <fieldset className="grid gap-4 rounded-lg border border-divider-base p-4" data-testid="time-and-bounds">
              <legend className="text-body-sm">Time, order and bounds</legend>
              <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-5">
                {graphGrainComponents.length ? <Field label="Grain" hint="Frozen into the executed Result.">
                  {(field) => <NativeSelect
                    {...field}
                    value={grainField}
                    disabled={running || projecting || buildingVisualization}
                    onChange={(event) => {
                      const next = event.target.value;
                      setGrainField(next);
                      setWindowStart("");
                      setWindowEnd("");
                      setComparison("none");
                      invalidateExecutedResult();
                    }}
                  >
                    <option value="">No time grain</option>
                    {graphGrainComponents.map((component) => (
                      <option key={component.canonical_field_id} value={component.canonical_field_id}>
                        {component.canonical_name}
                      </option>
                    ))}
                  </NativeSelect>}
                </Field> : null}
                {graphGrainComponents.length ? <Field label="From">
                  {(field) => <Input
                    {...field}
                    type="date"
                    value={windowStart}
                    onChange={(event) => {
                      const next = event.target.value;
                      setWindowStart(next);
                      if (!next) setComparison("none");
                      invalidateExecutedResult();
                    }}
                    disabled={!grainField || running || projecting || buildingVisualization}
                  />}
                </Field> : null}
                {graphGrainComponents.length ? <Field label="To">
                  {(field) => <Input
                    {...field}
                    type="date"
                    value={windowEnd}
                    onChange={(event) => {
                      const next = event.target.value;
                      setWindowEnd(next);
                      if (!next) setComparison("none");
                      invalidateExecutedResult();
                    }}
                    disabled={!grainField || running || projecting || buildingVisualization}
                  />}
                </Field> : null}
                {graphGrainComponents.length ? <Field
                  label="Source comparison"
                  hint="Both exact windows are frozen into one Result."
                >
                  {(field) => <NativeSelect
                    {...field}
                    value={comparison}
                    disabled={running || projecting || buildingVisualization || !grainField || !windowStart || !windowEnd}
                    onChange={(event) => {
                      const next = event.target.value as typeof comparison;
                      setComparison(next);
                      if (next !== "none") {
                        // The comparison takes the grain onto its own axis, so
                        // it leaves the wells it was grouping.
                        setRowFields((current) => current.filter((id) => id !== grainField));
                        setColumnFields((current) => current.filter((id) => id !== grainField));
                        if (filterField === grainField) {
                          setFilterField("");
                          setFilterValue("");
                        }
                        setSubtotals(false);
                        setGrandTotal("none");
                      }
                      invalidateExecutedResult();
                    }}
                  >
                    <option value="none">No comparison</option>
                    <option value="previous_period">Previous period</option>
                    <option value="previous_year">Previous year</option>
                  </NativeSelect>}
                </Field> : null}
                <Field label="Maximum Result rows" hint="100 to 100,000.">
                  {(field) => <Input {...field} type="number" min={100} max={100000} value={rowLimit} onChange={(event) => setRowLimit(event.target.value)} />}
                </Field>
              </div>
              <Field label="Pivot row order">
                {(field) => <NativeSelect {...field} value={rowSort} onChange={(event) => setRowSort(event.target.value)}>
                  <option value="asc">Ascending</option>
                  <option value="desc">Descending</option>
                </NativeSelect>}
              </Field>
            </fieldset>
            <fieldset className="grid gap-4 rounded-lg border border-divider-base p-4" data-testid="filter-fields">
              <legend className="text-body-sm">Filters</legend>
              <div className="grid gap-4 md:grid-cols-2">
              <Field label="Field">
                {(field) => <NativeSelect
                  {...field}
                  value={filterField}
                  onChange={(event) => setFilterField(event.target.value)}
                >
                  <option value="">No filter</option>
                  {graphComponents.filter((component) => (
                    comparison === "none" || component.canonical_field_id !== grainField
                  )).map((component) => (
                    <option key={component.canonical_field_id} value={component.canonical_field_id}>
                      {component.canonical_name}
                    </option>
                  ))}
                </NativeSelect>}
              </Field>
              <Field label="Exact value" hint="Filters the server-owned Result; no rows are filtered in the browser.">
                {(field) => <Input
                  {...field}
                  value={filterValue}
                  onChange={(event) => setFilterValue(event.target.value)}
                  disabled={!filterField}
                />}
              </Field>
              </div>
            </fieldset>
            <div className="grid gap-4 md:grid-cols-2">
              <label className="flex items-center gap-2 self-end pb-2.5 text-body-sm">
                <Checkbox
                  checked={subtotals}
                  disabled={comparison !== "none"}
                  onCheckedChange={(next) => setSubtotals(next === true)}
                />
                <span>Row subtotals</span>
              </label>
              <Field label="Grand totals">
                {(field) => <NativeSelect
                  {...field}
                  value={grandTotal}
                  disabled={comparison !== "none"}
                  onChange={(event) => setGrandTotal(event.target.value)}
                >
                  <option value="none">None</option>
                  <option value="rows">Rows</option>
                  <option value="columns">Columns</option>
                  <option value="both">Rows and columns</option>
                </NativeSelect>}
              </Field>
            </div>
            <Field
              label="Rows with no match on the other side"
              hint="This is an analytical choice: changing it produces a new Result."
            >
              {(field) => <NativeSelect
                {...field}
                value={inclusion}
                onChange={(event) => setInclusion(event.target.value)}
                data-testid="inclusion-policy"
              >
                <option value="matched_only">Keep only matched rows</option>
                <option value="preserve_primary">Keep every row of the first source</option>
                <option value="preserve_all">Keep every row of both sources</option>
              </NativeSelect>}
            </Field>
            <Cluster className="gap-2">
              <Button
                type="button"
                onClick={run}
                disabled={
                  running
                  || projecting
                  || buildingVisualization
                  || addingMatchKey !== null
                  || selectedMeasures.length === 0
                  || (comparison !== "none" && (!windowStart || !windowEnd || !grainField))
                  // A dimension on both axes would group by it twice. The wells
                  // move it rather than duplicate it, so this can only be a bug
                  // upstream -- it stays as the guard it always was.
                  || rowFields.some((fieldId) => columnFields.includes(fieldId))
                  || !profile
                  || (!profileCanRun(profile, selected)
                    && !(grainField && (windowStart || windowEnd)))
                  || additionalMatches.some(
                    (match) => !profileCanRun(additionalProfiles[matchKey(match)] ?? null, match)
                      && !(grainField && (windowStart || windowEnd)),
                  )
                }
                data-testid="run-analysis"
              >
                {running ? "Running…" : "Run this analysis"}
              </Button>
              {running ? (
                <Button
                  type="button"
                  variant="secondary"
                  onClick={cancelRun}
                  data-testid="cancel-analysis"
                >
                  Cancel
                </Button>
              ) : null}
            </Cluster>
          </Stack>
        </Panel>
      ) : null}

      {runError ? (
        <Status as="block" tone="error" title="The analysis was not run" data-testid="run-error">
          {runError}{runAdvice ? ` ${runAdvice}` : ""}
        </Status>
      ) : null}

      {resultId ? (
        <Panel data-testid="preview">
          <PanelHeader
            title="Preview"
            description="Table, Pivot and Chart use one immutable Result. Presentation changes never recalculate it in the browser."
          />
          <Cluster className="gap-2" role="group" aria-label="Presentation">
            <Button
              type="button"
              variant={presentation === "table" ? "default" : "secondary"}
              onClick={() => project("table")}
              data-testid="present-table"
            >
              Table
            </Button>
            <Button
              type="button"
              variant={presentation === "pivot" ? "default" : "secondary"}
              onClick={() => project("pivot")}
              data-testid="present-pivot"
            >
              Pivot
            </Button>
            <Button
              type="button"
              variant="secondary"
              onClick={() => void openChart()}
              disabled={buildingVisualization}
              data-testid="present-chart"
            >
              {buildingVisualization ? "Saving visualization…" : "Chart from a blank table"}
            </Button>
          </Cluster>
          {/* The same question the Result workbench asks, asked the same way:
              an affordance that differs between two screens is two affordances. */}
          <StartingPointChoice
            projectId={projectId}
            resultId={resultId}
            busy={buildingVisualization}
            onStartFromTemplate={(templateVersionId) => void openChart(templateVersionId)}
            testId="explorer-starting-point"
          />
          <p className="text-body-sm text-text-secondary" data-testid="result-identity">
            Result {resultId}
          </p>
          {/* STORY 75-2 -- the same rail the Result workbench opens, opened from
              the pivot. The measures HERE are canonical fields of a Datastream
              match, not Semantic Concepts, so nothing can be pre-filled from
              them: the dialog says exactly that and offers this Project's
              published Concept versions instead. It never sends a reference
              without its version. */}
          <div className="flex flex-wrap items-center gap-3">
            <Button
              type="button"
              variant="secondary"
              onClick={() => setProposingField(true)}
              data-testid="explorer-propose-governed-field"
            >
              Propose as governed field
            </Button>
            <span className="text-caption text-text-secondary">
              A calculation read off this pivot becomes a proposal in Governance, with this
              Result named as where it came from.
            </span>
          </div>
          <ProposeCalculatedFieldDialog
            open={proposingField}
            onClose={() => setProposingField(false)}
            projectId={projectId}
            resultId={resultId}
            querySpecVersionId={querySpecVersionId}
          />
          {analysisContext ? (
            <Status as="block" tone="info" title="Pinned analysis context" data-testid="pinned-analysis-context">
              {analysisContext.business_domain
                ? `${analysisContext.business_domain.name} · v${analysisContext.business_domain.version_number}. `
                : "No Business Domain. "}
              {analysisContext.golden_question
                ? `Golden Question: ${analysisContext.golden_question.title} · v${analysisContext.golden_question.version_number}. `
                : "Free exploration. "}
              {analysisContext.requested_skills.length
                ? `Requested Skill: ${analysisContext.requested_skills.map((skill) => `${skill.name} · v${skill.version_number}`).join(", ")}.`
                : "No requested Skill."}
            </Status>
          ) : null}
          {presentation === "table" && resultEvidence ? (
            <ResultTable evidence={resultEvidence} labels={fieldLabels} />
          ) : null}
          {presentation === "pivot" && matrix ? (
            <Stack className="gap-3">
              {/* The axes OF THIS TABLE, where the table is read. Not a second
                  control over the same fact: these are the same two wells as the
                  Composition region, and a field moved here re-projects the same
                  Result -- the id and the content hash below do not change. */}
              <div className="grid gap-2 md:grid-cols-2" data-testid="pivot-axis-bar">
                {compositionWells
                  .filter((well) => well.name !== "values")
                  .map((well) => (
                    <FieldWell key={`axis-${well.name}`} well={well} instance="axis" compact />
                  ))}
              </div>
              <p className="m-0 text-caption text-text-secondary">
                Move a field between Rows and Columns, or reorder one, to read the same
                Result another way. Nothing is recalculated: Result {matrix.result_id} stays
                as it is.
              </p>
              <PivotTable matrix={matrix} labels={fieldLabels} />
              {/* TWO AXES, TWO PAGERS, and that is the whole of what makes the
                  pivot different from a collection. The four buttons used to
                  sit in one row in bounds order — Previous rows, Next rows,
                  Next columns, Previous columns — so the two directions of the
                  rows axis stood on either side of the columns axis's. Each
                  axis now owns its control and names its own `unit`, which is
                  also what keeps "Next rows" and "Next columns" apart in the
                  accessibility tree.

                  THE OFFSETS AND THE HISTORY ARE UNCHANGED. `project` is called
                  with the same offsets, the same push/pop instruction and the
                  same value it was called with before; a direction that has
                  nowhere to go is `null` rather than absent, so the reader can
                  see that an axis HAS a previous page before there is one. */}
              <Cluster className="gap-6" aria-label="Pivot pages">
                <Pager
                  unit="rows"
                  label="Pivot rows"
                  busy={projecting}
                  onPrevious={
                    (matrix.bounds.row_offset ?? 0) > 0
                      ? () => void project("pivot", pageRequest(
                          rowPageHistory.current.at(-1),
                          "row",
                          matrix.bounds.column_offset ?? 0,
                        ), { axis: "row", action: "pop" })
                      : null
                  }
                  onNext={
                    matrix.bounds.next_row_offset !== null
                    && matrix.bounds.next_row_offset !== undefined
                      ? () => void project("pivot", pageRequest(
                          matrix.bounds.next_row_cursor ?? matrix.bounds.next_row_offset ?? 0,
                          "row",
                          matrix.bounds.column_offset ?? 0,
                        ), {
                          axis: "row",
                          action: "push",
                          // The token for the page being LEFT, so Previous walks
                          // back with a continuation and not with arithmetic.
                          value: matrix.bounds.cursor ?? matrix.bounds.row_offset ?? 0,
                        })
                      : null
                  }
                />
                <Pager
                  unit="columns"
                  label="Pivot columns"
                  busy={projecting}
                  onPrevious={
                    (matrix.bounds.column_offset ?? 0) > 0
                      ? () => void project("pivot", pageRequest(
                          columnPageHistory.current.at(-1),
                          "column",
                          matrix.bounds.row_offset ?? 0,
                        ), { axis: "column", action: "pop" })
                      : null
                  }
                  onNext={
                    matrix.bounds.next_column_offset !== null
                    && matrix.bounds.next_column_offset !== undefined
                      ? () => void project("pivot", pageRequest(
                          matrix.bounds.next_column_cursor
                            ?? matrix.bounds.next_column_offset ?? 0,
                          "column",
                          matrix.bounds.row_offset ?? 0,
                        ), {
                          axis: "column",
                          action: "push",
                          value: matrix.bounds.cursor ?? matrix.bounds.column_offset ?? 0,
                        })
                      : null
                  }
                />
              </Cluster>
            </Stack>
          ) : null}
        </Panel>
      ) : null}
    </Stack>
    </FieldComposer>
  );
}
