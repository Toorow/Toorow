/**
 * The Semantic Model tabs (Story 49.3): Semantics, Source Bindings and
 * Metrics & Dimensions.
 *
 * Everything rendered here was composed by the server. There is no browser join
 * and no second fetch: a coverage number stitched from four calls in the browser
 * looks authoritative and is wrong the moment one of them fails.
 *
 * Three distinctions the components keep visible rather than smoothing over:
 *
 *   - A refusal has a REASON. "Not queryable" alone tells nobody what to change,
 *     so every refused cell carries its named code and its sentence.
 *   - `null` is not `0`. When the Data adapter could not be read, coverage shows
 *     "Unavailable", never "0 / 0".
 *   - Compiled SQL is EVIDENCE, never input. It is displayed read-only and
 *     labelled as an output of the formula above it.
 *
 * No new base class, no hardcoded colour, no literal spacing: every element
 * below is a primitive from `ui/index.ts`.
 */
import { useEffect, useMemo, useState, type CSSProperties, type ReactNode } from "react";
import { apiGet } from "../lib/apiFetch";
import {
  Button,
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
  EmptyState,
  Input,
  Metric,
  NativeSelect,
  ObjectId,
  Panel,
  PanelHeader,
  Stack,
  Status,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  TableScroll,
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
  displayValue,
  label,
  formatPercent,
  stateLabel,
  stateTone,
  Retry,
} from "../ui";
import { buildPath, useRoute, type CanonicalRoute } from "../shell/router";
import type { GovernanceObject, OwnerReference } from "./governanceSurface";
import {
  ADDITIVITY_LABEL,
  ADDITIVITY_HINT,
  type AdditivityClass,
} from "./formulaContract";

/*
 * THE PRIVATE COVERAGE MAP IS GONE (76-2), AND ITS ARGUMENT WAS ABOUT THE WRONG
 * WORD. It said: « `unavailable` is deliberately NOT neutral: an unreadable
 * owner is a problem, and neutral reads as "fine" ». The reading is fair and the
 * word was wrong -- `unavailable` is the absence the SERVER chose to state, and
 * since 76-2 an owner the console cannot read at all answers `Unknown` in the
 * warning colour, which is precisely the "this is a problem" this map wanted.
 * `blocking` is its own word at error (what a thing does downstream --
 * amendment 21), `blocked` stays at warning (what an object is); `candidate`,
 * `suggested` and `excluded` are declared words.
 */

export interface BindingRow {
  concept_id: string | null;
  concept_version_id: string | null;
  concept_name: string | null;
  datastream_id: string;
  datastream_name: string | null;
  mapping_version_id: string;
  mapping_version_number: number | null;
  source_field_id: string;
  source_field_path: string | null;
  state: string;
  confidence: number | null;
  publication_ref: { kind: string; id: string } | null;
  fingerprints: Record<string, unknown>;
  freshness: Record<string, unknown>;
  blocking_refs: Array<Record<string, unknown>>;
  provenance: Record<string, unknown>;
  owner_href: OwnerReference;
}

export interface SourceBindings {
  state: "available" | "empty" | "unavailable";
  rows: BindingRow[];
  coverage: {
    bound: number | null;
    eligible: number | null;
    /** Datastreams that publish NO mapping version. The server has sent this
     *  since Story 53.7 and nothing here declared it, so the compensating number
     *  never reached a screen: one bound field beside eight unmapped Datastreams
     *  rendered `1 / 1`, and the reader saw a complete Project. It is the count
     *  that keeps the fraction from being read as the whole answer. */
    unmapped_datastreams: number | null;
    by_state: Record<string, number>;
    returned: number;
    total: number;
    truncated: boolean;
  };
  unavailable_reason: { code: string; message: string } | null;
}

export interface MatrixCell {
  metric_id: string;
  metric_version_id: string;
  dimension_id: string;
  dimension_version_id: string;
  queryable: boolean;
  join_path?: Array<{ relationship: string; from: string; to: string; cardinality: string; bridge: string | null }>;
  refusal?: { code: string; message: string };
}

export interface CompiledMatrix {
  compiler_version?: string;
  expression_contract_version?: string;
  metrics?: Array<{ concept_id: string; version_id: string; label: string }>;
  dimensions?: Array<{ concept_id: string; version_id: string; label: string }>;
  cells?: MatrixCell[];
  summary?: Record<string, number>;
}

/** The one place a Data owner link is built. The server sends a semantic owner
 *  REFERENCE, never a URL, so a route rename cannot freeze a dead address.
 *
 *  It is built by the ROUTER, not assembled here. This function used to
 *  concatenate `/org/{org}/project/{project}/{workspace}/{section}[/object/…]`
 *  itself — the right shape, and still a second address grammar: nothing checked
 *  that the workspace, the section, the object type or the tab were declared, so
 *  a reference naming an unregistered pair produced a well-formed string that
 *  `parsePath` refuses, and the row offered a link to the unknown-route screen.
 *  `buildPath` validates the whole combination against the registry and throws
 *  when it does not hold; a reference that cannot be built returns `null` and the
 *  caller renders the owner's name without a link, which every call site already
 *  handles. */
export function ownerPath(reference: OwnerReference | null | undefined, organizationId: string, projectId: string): string | null {
  if (!reference || reference.surface !== "project" || !reference.workspace || !reference.section) return null;
  try {
    return buildPath({
      scope: "project",
      organizationId,
      projectId,
      workspace: reference.workspace as CanonicalRoute["workspace"],
      section: reference.section,
      lens: null,
      objectType: reference.object_type ?? null,
      objectId: reference.object_id ?? null,
      tab: reference.object_type && reference.object_id ? reference.tab ?? null : null,
      versionId: null,
      evidenceId: null,
      action: null,
      query: {},
      globalSurface: null,
      globalSection: null,
    } as CanonicalRoute);
  } catch {
    return null;
  }
}

/** A Governance object address, built by the ROUTER — the same rule `ownerPath`
 *  above states, applied to the addresses this file builds for itself.
 *
 *  Concatenation would be a second address grammar nothing validates: a link can
 *  be perfectly well-formed and still name a workspace/section/object-type/tab
 *  combination the registry never declared, in which case `parsePath` refuses it
 *  and the reader lands on the unknown-route screen. `buildPath` checks the whole
 *  combination — including a `versionId` tail, which it accepts only under a tab
 *  the contract declares as version-bearing — and throws when it does not hold.
 *  A combination that cannot be built returns `null`, and every caller renders
 *  the identity without a link rather than offering a dead one. */
export function governanceObjectPath(target: {
  organizationId: string;
  projectId: string;
  section: string;
  objectType: string;
  objectId: string | null | undefined;
  tab?: string | null;
  versionId?: string | null;
}): string | null {
  if (!target.organizationId || !target.projectId || !target.objectId) return null;
  try {
    return buildPath({
      scope: "project",
      organizationId: target.organizationId,
      projectId: target.projectId,
      workspace: "governance",
      section: target.section,
      lens: null,
      objectType: target.objectType,
      objectId: target.objectId,
      tab: target.tab ?? null,
      versionId: target.versionId ?? null,
      evidenceId: null,
      action: null,
      query: {},
      globalSurface: null,
      globalSection: null,
    } as CanonicalRoute);
  } catch {
    return null;
  }
}

/** An identity that opens its owner when the router can address it, and stays a
 *  plain identity when it cannot. Never a link to nowhere. */
function IdentityLink({ href, children }: { href: string | null; children: ReactNode }) {
  if (!href) return <>{children}</>;
  return (
    <a
      className="underline-offset-2 hover:underline focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus"
      href={href}
    >
      {children}
    </a>
  );
}

/** The fraction, and the population it does NOT describe, side by side.
 *
 *  The fraction counts (Datastream, field) pairs on both sides, so a Datastream
 *  that publishes no mapping contributes nothing to either: its field population
 *  is unknown, and folding it in would be the category error Story 53.7 removed.
 *  That correctness has a cost the server pays and the screen must not pocket —
 *  one bound field beside eight unmapped Datastreams is arithmetically `1 / 1`
 *  and reads as a finished Project. So the fraction is never shown alone: when
 *  Datastreams sit unmapped they get their own number next to it, and the
 *  fraction's own hint stops claiming to describe the Project. */
function CoverageHeadline({ coverage, state }: { coverage: SourceBindings["coverage"]; state: string }) {
  if (state === "unavailable" || coverage.bound === null || coverage.eligible === null) {
    return (
      <Metric
        label="Coverage"
        value="Unavailable"
        hint="The Data mapping owner could not be read. This is not a coverage of zero."
      />
    );
  }
  // `null` is "we could not look", and it must not become a reassuring zero here
  // either: an unknown unmapped count is reported as unknown, not as none.
  const unmapped = coverage.unmapped_datastreams;
  return (
    <Stack className="gap-4 md:flex-row md:items-end">
      <Metric
        label="Coverage"
        value={`${coverage.bound} / ${coverage.eligible}`}
        hint={
          unmapped === null || unmapped > 0
            ? "Bindings of the mapping versions Datastreams currently publish. It does not describe the Datastreams beside it, which publish none: this is not the coverage of the Project."
            : "Bindings from the mapping version each Datastream currently publishes. Proposals and connector defaults are shown but never counted."
        }
      />
      {unmapped === null ? (
        <Metric
          label="Datastreams with no published mapping"
          value="Unknown"
          hint="The owner did not say how many Datastreams publish no mapping, so how much of this Project the fraction covers is unknown."
        />
      ) : (
        unmapped > 0 && (
          <Metric
            label="Datastreams with no published mapping"
            value={String(unmapped)}
            hint="They publish no mapping version, so their fields are unknown and none of them is in the fraction. Coverage above is complete only when this is zero."
          />
        )
      )}
    </Stack>
  );
}

// ---------------------------------------------------------------------------
// Source Bindings — read-only, always
// ---------------------------------------------------------------------------

export function SourceBindingsTab({
  bindings,
  organizationId,
  projectId,
  typeLabel,
  onRetry,
}: {
  bindings: SourceBindings | null | undefined;
  /** Re-reads the governed object this tab is handed (76-4). Owned by the
   *  workbench, because the workbench made the read. */
  onRetry: () => void;
  organizationId: string;
  projectId: string;
  typeLabel: string;
}) {
  const [stateFilter, setStateFilter] = useState("all");

  if (!bindings) {
    return (
      <Panel>
        <PanelHeader title="Source Bindings" description="Where this meaning is physically bound." />
        <EmptyState
          title="No binding owner answered"
          description="The Data mapping owner could not be read, so it is unknown where this meaning is bound. This is not a count of zero."
        />
      </Panel>
    );
  }

  const rows = bindings.rows.filter((row) => stateFilter === "all" || row.state === stateFilter);
  const states = Object.keys(bindings.coverage.by_state);

  return (
    <Stack className="gap-6">
      <Panel>
        <PanelHeader
          title="Source Bindings"
          description="Composed from the mapping version each Datastream currently publishes. This surface is read-only: the editor is in Data, and accepting a Concept here never advances a mapping, an execution or a publication pointer."
        />
        <Stack className="gap-4 md:flex-row md:items-end md:justify-between">
          <CoverageHeadline coverage={bindings.coverage} state={bindings.state} />
          {states.length > 0 && (
            <NativeSelect
              aria-label="Filter bindings by state"
              value={stateFilter}
              onChange={(event) => setStateFilter(event.target.value)}
            >
              <option value="all">All states ({bindings.coverage.total})</option>
              {states.map((state) => (
                <option key={state} value={state}>
                  {label(state)} ({bindings.coverage.by_state[state]})
                </option>
              ))}
            </NativeSelect>
          )}
        </Stack>
      </Panel>

      {bindings.state === "unavailable" ? (
        <Status as="block" tone="error" title="Mapping coverage is unavailable"
          action={<Retry onClick={onRetry} />}
        >
          {bindings.unavailable_reason?.message ?? "The Data mapping owner could not be read."}
        </Status>
      ) : rows.length === 0 ? (
        <EmptyState
          title={bindings.coverage.total === 0 ? "Nothing is bound yet" : "No binding in this state"}
          description={
            bindings.coverage.total === 0
              ? `The Data owner answered, and no published mapping version binds this ${typeLabel} yet.`
              : "Its owner answered; no binding currently sits in the state you selected."
          }
        />
      ) : (
        <Panel flush>
          <TableScroll label={`${typeLabel} source bindings`}>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Datastream</TableHead>
                  <TableHead>Source field</TableHead>
                  <TableHead>Concept</TableHead>
                  <TableHead>Active mapping version</TableHead>
                  <TableHead>State</TableHead>
                  <TableHead>Confidence</TableHead>
                  <TableHead>Owner</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((row) => {
                  const href = ownerPath(row.owner_href, organizationId, projectId);
                  return (
                    <TableRow key={`${row.datastream_id}:${row.source_field_id}:${row.mapping_version_id}`}>
                      {/* THE NAME IS SERVED, NEVER COMPOSED HERE. Every row of
                          `mapping-coverage` is projected from `app.datastreams`
                          (`semantic_coverage._ACTIVE_MAPPING_VERSIONS` and
                          `_UNMAPPED_DATASTREAMS`, both `FROM app.datastreams d`),
                          and `d.name` is `NOT NULL` since migration 023. The
                          `?? row.datastream_id` this cell carried was therefore
                          unreachable; an absence, if the projection ever changes,
                          says so instead of printing `ds_<ULID>`. */}
                      <TableCell className="font-semibold text-text">
                        {row.datastream_name ?? "Unnamed Datastream"}
                      </TableCell>
                      {/* The path, never the values behind it. */}
                      <TableCell className="text-ui text-text-secondary">
                        {row.source_field_path ?? (row.source_field_id || "No field")}
                      </TableCell>
                      <TableCell className="text-ui text-text-secondary">
                        {row.concept_name ?? "Unattributed"}
                      </TableCell>
                      <TableCell className="text-ui text-text-secondary">
                        {row.mapping_version_id
                          ? `${row.mapping_version_id}${row.mapping_version_number ? ` (v${row.mapping_version_number})` : ""}`
                          : "None published"}
                      </TableCell>
                      <TableCell>
                        <Status tone={stateTone(row.state)}>{stateLabel(row.state)}</Status>
                        {row.blocking_refs.length > 0 && (
                          <p className="mt-2 text-caption text-text-secondary">
                            {row.blocking_refs.length} proposal(s) waiting. The decision lives in Controls &amp; Quality.
                          </p>
                        )}
                      </TableCell>
                      <TableCell className="text-ui text-text-secondary">
                        {row.confidence === null ? "Not scored" : formatPercent(row.confidence)}
                      </TableCell>
                      <TableCell>
                        {href ? (
                          <a
                            className="text-ui underline-offset-2 hover:underline focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus"
                            href={href}
                          >
                            Data · Mapping
                          </a>
                        ) : (
                          <span className="text-ui text-text-secondary">No owner link</span>
                        )}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </TableScroll>
          {bindings.coverage.truncated && (
            <Status as="block" tone="warning" title="This page is bounded">
              {bindings.coverage.total} bindings match and the first {bindings.coverage.returned} are shown.
              The remainder is not counted as absent.
            </Status>
          )}
        </Panel>
      )}
    </Stack>
  );
}

// ---------------------------------------------------------------------------
// Semantics — the typed formula, its dependencies, and read-only evidence
// ---------------------------------------------------------------------------

interface FormulaNode {
  op?: string;
  [key: string]: unknown;
}

/** The formula as an accessible LIST as well as a tree. A dependency graph that
 *  only exists as a diagram is unreadable to anyone using a screen reader, and
 *  the list is also what makes the tree checkable by eye. */
function formulaLines(node: unknown, depth = 0, role = "result"): Array<{ depth: number; role: string; text: string }> {
  if (!node || typeof node !== "object") return [];
  const typed = node as FormulaNode;
  const op = typeof typed.op === "string" ? typed.op : "unknown";
  const lines: Array<{ depth: number; role: string; text: string }> = [];
  let text = label(op);
  if (op === "concept_ref") text = `Concept ${String(typed.concept_id)} at version ${String(typed.version_id)}`;
  if (op === "concept_name") text = `Unresolved reference by name: ${String(typed.name)}`;
  if (op === "source_measure") text = `Mapped source measure of ${String(typed.concept)}`;
  if (op === "literal") text = `Literal ${displayValue(typed.value)} (${String(typed.value_type)})`;
  if (op === "aggregate") text = `${label(String(typed.function))} of`;
  if (op === "ratio") text = `Ratio · zero denominator becomes ${String(typed.zero_denominator ?? "undeclared")}`;
  lines.push({ depth, role, text });

  const children: Array<[string, unknown]> = [];
  if (Array.isArray(typed.operands)) typed.operands.forEach((child, index) => children.push([`operand ${index + 1}`, child]));
  if (typed.numerator) children.push(["numerator", typed.numerator]);
  if (typed.denominator) children.push(["denominator", typed.denominator]);
  if (typed.operand) children.push(["operand", typed.operand]);
  if (typed.left) children.push(["left", typed.left]);
  if (typed.right) children.push(["right", typed.right]);
  if (Array.isArray(typed.when)) {
    typed.when.forEach((branch, index) => {
      const typedBranch = branch as { condition?: unknown; then?: unknown };
      children.push([`when ${index + 1} condition`, typedBranch.condition]);
      children.push([`when ${index + 1} value`, typedBranch.then]);
    });
  }
  if (typed.otherwise) children.push(["otherwise", typed.otherwise]);
  for (const [childRole, child] of children) lines.push(...formulaLines(child, depth + 1, childRole));
  return lines;
}

/** The EXACT dependencies of a formula: one row per `(concept_id, version_id)`
 *  pair the tree pins, in the order it pins them. Derived from the stored
 *  expression, never fetched a second time and never completed by a guess — a
 *  formula that pins nothing shows nothing.
 *
 *  Why exact versions get their own list rather than only appearing inside the
 *  tree: `concept_ref` is the only leaf that reaches outside this Concept, and
 *  "what does this metric depend on, at which version" is the question asked
 *  before an edit, not while reading a formula top to bottom. */
function formulaDependencies(node: unknown): Array<{ conceptId: string; versionId: string; role: string }> {
  const seen = new Set<string>();
  const out: Array<{ conceptId: string; versionId: string; role: string }> = [];
  for (const line of formulaLines(node)) {
    const match = /^Concept (\S+) at version (\S+)$/.exec(line.text);
    if (!match) continue;
    const key = `${match[1]}|${match[2]}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ conceptId: match[1], versionId: match[2], role: line.role });
  }
  return out;
}

/** The three classes migration 142 stores, shown with the three labels of
 *  `formulaContract.ts` and no fourth vocabulary. `Non-Additive Ratio` and
 *  `Non-Additive Snapshot` from the epic plan are CASES of these three: a
 *  snapshot is `semi_additive` plus the dimensions shown in the panel below. */
function additivityLabel(value: unknown): string {
  const raw = typeof value === "string" ? value : "";
  return raw in ADDITIVITY_LABEL ? ADDITIVITY_LABEL[raw as AdditivityClass] : "Unavailable";
}

/** The verdict of the formula-parity gate, composed SERVER-SIDE and only read
 *  here. `scripts/check_metric_formula_parity.py` has confronted the three
 *  copies of a ratio's formula — the seed, the mart, the governed Concept —
 *  since 2026-08-16, and until 2026-08-31 its answer was visible to whoever ran
 *  the script and to nobody else: the read model composed no flag and this tab
 *  rendered none. The rule lives in `core.platform_semantic_concepts.
 *  formula_parity`; nothing about it is re-decided here, not even the sentence. */
type FormulaParity = {
  verdict: string;
  declared: string;
  governed: string | null;
  message: string;
};

/** Absent verdict → nothing at all. A "nothing to report" badge on every
 *  Concept teaches a reader to stop looking at the one that matters. */
function formulaParity(summary: Record<string, unknown>): FormulaParity | null {
  const raw = summary.formula_parity;
  if (!raw || typeof raw !== "object") return null;
  const value = raw as Record<string, unknown>;
  if (typeof value.verdict !== "string" || typeof value.message !== "string") return null;
  return {
    verdict: value.verdict,
    declared: typeof value.declared === "string" ? value.declared : "",
    governed: typeof value.governed === "string" ? value.governed : null,
    message: value.message,
  };
}

/** One alert language for the product: an override is not a fault, a platform
 *  divergence is, and "not compared" is neither — it is an admission. */
const PARITY_TONE: Record<string, "success" | "warning" | "error"> = {
  aligned: "success",
  project_override: "warning",
  unreadable: "warning",
  platform_divergence: "error",
};

const PARITY_TITLE: Record<string, string> = {
  aligned: "This formula matches the delivered catalogue",
  project_override: "This Project redefines a metric the delivered table serves",
  unreadable: "This formula was not compared with the delivered catalogue",
  platform_divergence: "This metric says two different things",
};

export function SemanticsTab({ detail }: { detail: GovernanceObject }) {
  // The scope comes from the ROUTER, not from a prop chain: this tab is only
  // ever mounted on a project address, and reading it here means the dependency
  // links exist without a second copy of the scope travelling through the
  // workbench.
  const { route } = useRoute();
  const organizationId = route.organizationId ?? "";
  const projectId = route.projectId ?? "";
  const summary = detail.summary as Record<string, unknown>;
  const expression = summary.expression;
  const unspecified = (summary.unspecified as string[] | undefined) ?? [];
  const lines = useMemo(() => formulaLines(expression), [expression]);
  const dependencies = useMemo(() => formulaDependencies(expression), [expression]);
  const unresolved = lines.filter((line) => line.text.startsWith("Unresolved reference"));
  const additivityClass = typeof summary.additivity_class === "string" ? summary.additivity_class : "";
  const parity = formulaParity(summary);

  return (
    <Stack className="gap-6">
      <Panel className="grid gap-4 md:grid-cols-4">
        <Metric label="Kind" value={label(String(summary.concept_kind ?? "unavailable"))} />
        <Metric label="Value type" value={label(String(summary.value_type ?? "unavailable"))} />
        <Metric
          label="Aggregation"
          value={summary.aggregation ? label(String(summary.aggregation)) : "Non-additive"}
          hint="A metric declares how it aggregates, or declares itself non-additive. Neither is a default."
        />
        <Metric
          label="Additivity"
          value={additivityLabel(summary.additivity_class)}
          hint={
            additivityClass in ADDITIVITY_HINT
              ? ADDITIVITY_HINT[additivityClass as AdditivityClass]
              : "Whether this measure stays correct when rolled up."
          }
        />
      </Panel>

      {Array.isArray(summary.non_additive_dimensions) && summary.non_additive_dimensions.length > 0 && (
        <Status as="block" tone="warning" title="This metric must not be summed across every dimension">
          It is declared non-additive on: {(summary.non_additive_dimensions as string[]).map(label).join(", ")}.
          A Semantic View that pairs it with one of them is refused at compilation.
        </Status>
      )}

      {unresolved.length > 0 && (
        <Status as="block" tone="warning" title="This formula still refers to Concepts by name">
          {unresolved.length} operand(s) were carried over by name when the three superseded semantic
          stores were reconciled. A published version pins exact versions, so this formula cannot be
          published until each name is replaced by an exact Concept version.
        </Status>
      )}

      {parity && (
        <Status
          as="block"
          tone={PARITY_TONE[parity.verdict] ?? "warning"}
          title={PARITY_TITLE[parity.verdict] ?? "Formula parity"}
          data-testid={`formula-parity-${parity.verdict}`}
        >
          {parity.message}
        </Status>
      )}

      <Panel flush>
        <PanelHeader
          title="Formula"
          description="A typed expression tree over exact Concept versions. Operations come from one server-enforced allowlist; executable SQL is never accepted as a definition."
        />
        {lines.length === 0 ? (
          <EmptyState
            title="No formula"
            description="This Concept has no expression. A dimension does not need one; a metric that has none cannot be published."
          />
        ) : (
          <TableScroll label="Formula dependency list">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Role</TableHead>
                  <TableHead>Operation</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {lines.map((line, index) => (
                  <TableRow key={`${line.role}-${index}`}>
                    {/* Depth is a MEASUREMENT, so it is rendered as one. It was
                        a run of em-spaces prepended to the role: text, not
                        layout. Nothing aligns to it, a screen reader reads it
                        out as part of the cell, and it disappears the moment
                        the label wraps. The depth is now carried as a custom
                        property and multiplied by the spacing scale, so nesting
                        is visible as indentation -- no literal spacing value,
                        and no class per level. */}
                    <TableCell
                      className="w-64 text-ui text-text-secondary ps-[calc(var(--formula-depth)*var(--spacing)*4)]"
                      style={{ "--formula-depth": line.depth } as CSSProperties}
                      data-depth={line.depth}
                    >
                      {label(line.role)}
                    </TableCell>
                    <TableCell className="text-ui text-text">{line.text}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        )}
      </Panel>

      <Panel flush>
        <PanelHeader
          title="Depends on"
          description="The exact Concept versions this formula pins. A reference without a version follows whatever became current and is not a reference."
        />
        {dependencies.length === 0 ? (
          <EmptyState
            title="This formula pins no other Concept"
            description="It reads its own mapped source measure, fixed values, or nothing at all. That is a complete answer, not a missing one."
          />
        ) : (
          <TableScroll label="Pinned Concept versions">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Role</TableHead>
                  <TableHead>Concept</TableHead>
                  <TableHead>Version</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {/* Both ids OPEN. "What does this metric depend on" is the
                    question asked before an edit, and the next move is always to
                    go and look at the dependency: an identity a reader has to
                    copy out and paste into a search is a dead end printed in a
                    monospace font. The version column addresses the exact pinned
                    version through the versions tab, which is the only tab its
                    contract declares as version-bearing — `buildPath` refuses
                    any other, so the pin cannot be linked to a screen that would
                    show the current version instead. */}
                {dependencies.map((dependency) => (
                  <TableRow key={`${dependency.conceptId}-${dependency.versionId}`}>
                    <TableCell className="text-ui text-text-secondary">{label(dependency.role)}</TableCell>
                    <TableCell className="text-ui text-text">
                      <IdentityLink
                        href={governanceObjectPath({
                          organizationId,
                          projectId,
                          section: "semantic-model",
                          objectType: "semantic-concept",
                          objectId: dependency.conceptId,
                          tab: "definition",
                        })}
                      >
                        <ObjectId value={dependency.conceptId} title="Pinned Concept" />
                      </IdentityLink>
                    </TableCell>
                    <TableCell className="text-ui text-text">
                      <IdentityLink
                        href={governanceObjectPath({
                          organizationId,
                          projectId,
                          section: "semantic-model",
                          objectType: "semantic-concept",
                          objectId: dependency.conceptId,
                          tab: "versions",
                          versionId: dependency.versionId,
                        })}
                      >
                        <ObjectId value={dependency.versionId} title="Pinned version" />
                      </IdentityLink>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        )}
      </Panel>

      {unspecified.length > 0 && (
        <Panel>
          <PanelHeader
            title="Not stated by the source this Concept came from"
            description="Recorded rather than defaulted, so a gap reads as a gap instead of as a decision."
          />
          <ul className="text-ui text-text-secondary">
            {unspecified.map((item) => (
              <li key={item}>{label(item)}</li>
            ))}
          </ul>
        </Panel>
      )}
    </Stack>
  );
}

// ---------------------------------------------------------------------------
// Metrics & Dimensions — the compatibility matrix, with reasons
// ---------------------------------------------------------------------------

export function MetricsDimensionsTab({ detail }: { detail: GovernanceObject }) {
  const summary = detail.summary as Record<string, unknown>;
  const matrix = summary.compiled_matrix as CompiledMatrix | null | undefined;
  const [query, setQuery] = useState("");

  if (matrix === null) {
    return (
      <Panel>
        <PanelHeader title="Metrics &amp; Dimensions" description="What this Semantic View can be asked." />
        <EmptyState
          title="The compiler output could not be read"
          description="No compatibility matrix is available for this version. This is not an empty matrix: nothing was measured."
        />
      </Panel>
    );
  }
  if (!matrix || !matrix.cells || matrix.cells.length === 0) {
    return (
      <Panel>
        <PanelHeader title="Metrics &amp; Dimensions" description="What this Semantic View can be asked." />
        <EmptyState
          title="This version has not been compiled"
          description="A Semantic View gets its compatibility matrix when a version is published. Nothing is queryable until then, and no pair is assumed compatible."
        />
      </Panel>
    );
  }

  const metrics = matrix.metrics ?? [];
  const dimensions = matrix.dimensions ?? [];
  const needle = query.trim().toLowerCase();

  /**
   * THE SEARCH READS BOTH AXES, and it used to read only one.
   *
   * The metrics are the rows and the dimensions are the COLUMNS, so a query
   * typed to find `Country` narrowed nothing at all: it matched no metric label,
   * emptied the table of every row, and left the column the person was looking
   * for standing over nothing. On a compiled View with thirty dimensions the
   * columns are the half that does not fit on screen, and they were the half the
   * control could not touch.
   *
   * THE RULE, and it is symmetric: the query is matched against metric names and
   * dimension names alike. Each axis narrows to what matches on it — and an axis
   * the query does not touch at all stays WHOLE. Searching a dimension name
   * keeps every metric, so the column can still be read against the measures it
   * refuses; searching a metric name keeps every dimension, which is what the
   * screen already did. A query that matches nothing on either axis narrows both
   * to nothing, because that is what "no match" means and a table that ignored
   * it would be showing a result nobody asked for.
   */
  const metricHits = needle
    ? metrics.filter((metric) => metric.label.toLowerCase().includes(needle))
    : metrics;
  const dimensionHits = needle
    ? dimensions.filter((dimension) => dimension.label.toLowerCase().includes(needle))
    : dimensions;
  const shownMetrics = !needle || metricHits.length > 0 || dimensionHits.length === 0
    ? metricHits
    : metrics;
  const shownDimensions = !needle || dimensionHits.length > 0 || metricHits.length === 0
    ? dimensionHits
    : dimensions;

  /** The compiler's own totals, which a browser filter never moves. */
  const totalMetrics = matrix.summary?.metrics ?? metrics.length;
  const totalDimensions = matrix.summary?.dimensions ?? dimensions.length;

  const cellFor = (metricId: string, dimensionId: string) =>
    matrix.cells?.find((cell) => cell.metric_id === metricId && cell.dimension_id === dimensionId);

  /**
   * Every refused pair, named — the same refusal the cells carry in a tooltip.
   *
   * A `title`-on-hover is not a reading on a touch screen and not a reading with
   * a keyboard, and the refusal is the one thing on this tab a person acts on:
   * it names what to change before a Semantic View will answer the pair. The
   * list is composed from the SAME cells the matrix draws, so it costs no
   * request and cannot disagree with the tooltip above it.
   *
   * It is NOT narrowed by the search. The count beside the heading is the number
   * of refusals this compilation produced, full stop; a list that shrank with
   * the filter would put a second, smaller number under the `Refused pairs`
   * metric tile and give one fact two values.
   */
  const metricLabel = (id: string) => metrics.find((metric) => metric.concept_id === id)?.label ?? id;
  const dimensionLabel = (id: string) =>
    dimensions.find((dimension) => dimension.concept_id === id)?.label ?? id;
  const refusedPairs = (matrix.cells ?? []).filter((cell) => !cell.queryable);

  return (
    <Stack className="gap-6">
      <Panel className="grid gap-4 md:grid-cols-4">
        <Metric label="Metrics" value={String(matrix.summary?.metrics ?? metrics.length)} />
        <Metric label="Dimensions" value={String(matrix.summary?.dimensions ?? dimensions.length)} />
        <Metric
          label="Queryable pairs"
          value={String(matrix.summary?.accepted ?? 0)}
          hint="Each pair was proved through the relationship graph. Listing a metric and a dimension does not make them compatible."
        />
        <Metric
          label="Refused pairs"
          value={String(matrix.summary?.refused ?? 0)}
          hint="Every refusal carries the exact reason it was refused."
        />
      </Panel>

      <Panel>
        <PanelHeader
          title="Compatibility matrix"
          description="Compiled by the server from exact Concept versions and declared relationships. A refused cell names why."
        />
        <Input
          aria-label="Search metrics and dimensions"
          placeholder="Search metrics and dimensions"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        {/* THE RULE, WHERE THE CONTROL IS. A search that narrows one axis and
            leaves the other whole is not guessable from the box, and a reader
            who cannot tell which half moved cannot trust either. */}
        <p className="m-0 text-caption text-text-secondary" data-testid="matrix-search-rule">
          Rows narrow to the metrics that match and columns to the dimensions that match. An axis
          the query touches on neither side stays whole.
        </p>
        {/* THE SERVER'S TOTALS STAY WHERE THEY ARE. The four numbers above are
            what the compiler produced and a box typed into here never moves
            them; this second line is the only place the narrowing is counted,
            so the two can never be read as one number contradicting itself. */}
        {needle ? (
          <p className="m-0 text-caption text-text-secondary" data-testid="matrix-filter-count">
            Showing {shownMetrics.length} of {totalMetrics} metrics ·{" "}
            {shownDimensions.length} of {totalDimensions} dimensions
          </p>
        ) : null}
      </Panel>

      <Panel flush>
        <TableScroll label="Metric by dimension queryability">
          <TooltipProvider>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Metric</TableHead>
                  {shownDimensions.map((dimension) => (
                    <TableHead key={dimension.concept_id}>{dimension.label}</TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {shownMetrics.map((metric) => (
                  <TableRow key={metric.concept_id}>
                    <TableCell className="font-semibold text-text">{metric.label}</TableCell>
                    {shownDimensions.map((dimension) => {
                      const cell = cellFor(metric.concept_id, dimension.concept_id);
                      if (!cell) {
                        return (
                          <TableCell key={dimension.concept_id} className="text-ui text-text-secondary">
                            Not compiled
                          </TableCell>
                        );
                      }
                      if (cell.queryable) {
                        const hops = cell.join_path ?? [];
                        return (
                          <TableCell key={dimension.concept_id}>
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <span>
                                  <Status tone="success">
                                    {hops.length === 0
                                      ? "Same dataset"
                                      : `${hops.length} join${hops.length === 1 ? "" : "s"}`}
                                  </Status>
                                </span>
                              </TooltipTrigger>
                              <TooltipContent>
                                {hops.length === 0
                                  ? "Both sit on the same dataset; no join is needed."
                                  : hops
                                      .map((hop) => `${hop.from} → ${hop.to} (${label(hop.cardinality)})`)
                                      .join(" · ")}
                              </TooltipContent>
                            </Tooltip>
                          </TableCell>
                        );
                      }
                      return (
                        <TableCell key={dimension.concept_id}>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <span>
                                <Status tone="error">{label(cell.refusal?.code ?? "refused")}</Status>
                              </span>
                            </TooltipTrigger>
                            <TooltipContent>{cell.refusal?.message ?? "Refused with no stated reason."}</TooltipContent>
                          </Tooltip>
                        </TableCell>
                      );
                    })}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TooltipProvider>
        </TableScroll>
      </Panel>

      {/* THE REFUSALS, REACHABLE. Every one of them is already on the screen
          above — inside a `Tooltip`, which opens on hover and on focus and
          therefore not at all on a touch screen, and which shows one refusal at
          a time so no reader can see whether thirty cells refuse for one reason
          or for thirty. Collapsed by default: the matrix is the answer, this is
          the follow-up question. */}
      {refusedPairs.length > 0 && (
        <Panel flush>
          <Collapsible>
            <div className="p-4">
              <CollapsibleTrigger
                className="rounded-pill border border-divider-base px-4 py-1.5 text-label font-label text-text"
                data-testid="refused-pairs-trigger"
              >
                Refused pairs ({refusedPairs.length})
              </CollapsibleTrigger>
              <p className="m-0 mt-2 text-caption text-text-secondary">
                Every pair the compiler refused, with the code and the sentence the matrix shows on
                hover. This list is the whole compilation and does not follow the search above.
              </p>
            </div>
            <CollapsibleContent data-testid="refused-pairs-list">
              <TableScroll label="Refused metric and dimension pairs">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Metric</TableHead>
                      <TableHead>Dimension</TableHead>
                      <TableHead>Refusal</TableHead>
                      <TableHead>Why</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {refusedPairs.map((cell) => (
                      <TableRow key={`${cell.metric_id}-${cell.dimension_id}`}>
                        <TableCell className="font-semibold text-text">{metricLabel(cell.metric_id)}</TableCell>
                        <TableCell className="text-ui text-text">{dimensionLabel(cell.dimension_id)}</TableCell>
                        <TableCell>
                          <Status tone="error">{label(cell.refusal?.code ?? "refused")}</Status>
                        </TableCell>
                        <TableCell className="text-ui text-text-secondary">
                          {cell.refusal?.message ?? "Refused with no stated reason."}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableScroll>
            </CollapsibleContent>
          </Collapsible>
        </Panel>
      )}

      {Array.isArray(summary.relationships) && (summary.relationships as unknown[]).length > 0 && (
        <Panel flush>
          <PanelHeader
            title="Relationship paths"
            description="Declared with join keys, cardinality and a fan-out policy. A many-to-many hop needs a bridge or the path is refused."
          />
          <TableScroll label="Declared relationships">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Relationship</TableHead>
                  <TableHead>From</TableHead>
                  <TableHead>To</TableHead>
                  <TableHead>Cardinality</TableHead>
                  <TableHead>Fan-out policy</TableHead>
                  <TableHead>Bridge</TableHead>
                  <TableHead>Common MDM key version</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {(summary.relationships as Array<Record<string, unknown>>).map((relationship) => (
                  <TableRow key={String(relationship.name)}>
                    <TableCell className="font-semibold text-text">{String(relationship.name)}</TableCell>
                    <TableCell className="text-ui text-text-secondary">
                      {String(relationship.from_dataset)} ({displayValue(relationship.from_columns)})
                    </TableCell>
                    <TableCell className="text-ui text-text-secondary">
                      {String(relationship.to_dataset)} ({displayValue(relationship.to_columns)})
                    </TableCell>
                    <TableCell className="text-ui text-text-secondary">
                      {label(String(relationship.cardinality_type))}
                    </TableCell>
                    <TableCell className="text-ui text-text-secondary">
                      {label(String(relationship.fan_out_policy))}
                    </TableCell>
                    <TableCell className="text-ui text-text-secondary">
                      {relationship.bridge_dataset ? String(relationship.bridge_dataset) : "None"}
                    </TableCell>
                    <TableCell className="font-mono text-caption text-text-secondary">
                      {relationship.mdm_common_key_version_id
                        ? String(relationship.mdm_common_key_version_id)
                        : "Unavailable"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        </Panel>
      )}

      <Panel>
        <PanelHeader
          title="Compiled artifact"
          description="An output of the version above, pinned and immutable. It is evidence, not a second definition: nothing reads it back as truth."
        />
        <Stack className="gap-4 md:flex-row">
          <Metric label="Compiler" value={String(matrix.compiler_version ?? "unavailable")} />
          <Metric label="Expression contract" value={String(matrix.expression_contract_version ?? "unavailable")} />
          <Metric label="Ossie interchange" value={String(summary.ossie_spec_version ?? "unavailable")} />
        </Stack>
      </Panel>
    </Stack>
  );
}

// ---------------------------------------------------------------------------
// The Explore handoff
// ---------------------------------------------------------------------------

export interface ExploreHandoff {
  semantic_view_id: string;
  semantic_view_version_id: string;
}

/** Enabled only for a published, queryable version. A button that opens Explore
 *  on a View with no queryable pair would be an invitation to a dead end.
 *
 *  It hands the router a validated query RECORD, not a URL. Building the address
 *  here would put a second copy of the route grammar in a component, and the
 *  router is the only thing that may canonicalize one. */

/** The Business Domains this Concept or View is linked to, as links.
 *
 *  `governance.md:72` -- "A Semantic View is linkable to Business Domains,
 *  Datastreams, knowledge, Skills, Analyze and evidence". `element-control-
 *  loop.md` §1 uses that exact sentence to define what an element IS: six
 *  capabilities, not one. This is the first of the six to get a surface.
 *
 *  It is built LAST on purpose. Measured 2026-08-03, before the write path was
 *  validated and the picker existed, the database held one view version and
 *  thirteen concept versions with not a single link -- rendering first would
 *  have shipped an empty list on every object, which reads as "nothing is
 *  linked" rather than "nothing can be".
 *
 *  A ref that resolves to no domain is shown AS ITS RAW ID, in a warning tone,
 *  never dropped. Versions published before `_validate_business_domain_refs`
 *  existed can carry a dangling reference, and a version is immutable: hiding it
 *  would make an unfixable row look clean. The refs come from the object, the
 *  names from the taxonomy -- the same endpoint the picker offers from, so the
 *  two sides cannot disagree about what a domain is called.
 */
export function BusinessDomainLinks({
  refs,
  organizationId,
  projectId,
}: {
  refs: readonly string[];
  organizationId: string;
  projectId: string;
}) {
  const [names, setNames] = useState<Record<string, string> | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const payload = await apiGet<{ domains?: Array<{ id: string; name: string }> }>(
          "/api/context/business-taxonomy",
        );
        if (cancelled) return;
        setNames(Object.fromEntries((payload.domains ?? []).map((d) => [d.id, d.name])));
      } catch {
        if (cancelled) return;
        // Names unreadable is not links unreadable: the ids are the object's own
        // and stay clickable. An empty map degrades to showing them raw.
        setNames({});
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, []);

  if (refs.length === 0) {
    return (
      <div data-testid="business-domain-links-empty">
        <EmptyState
          title="No Business Domain is linked to this object"
          description="A Business Domain is what makes this object findable beside the Knowledge, Skills and reporting views of the same area. Links are made in Context Hub."
        />
      </div>
    );
  }

  return (
    <ul className="m-0 flex flex-wrap gap-2 p-0" data-testid="business-domain-links">
      {refs.map((id) => {
        const name = names?.[id];
        // Built by the ROUTER. This line used to concatenate the address itself
        // — the header of this file forbids exactly that, and this was the one
        // place still doing it. The shape was right and that is what made it
        // dangerous: nothing checked that `master-data` declares a
        // `business-domain` with an `overview` tab, so a rename would have kept
        // producing a well-formed string that `parsePath` refuses.
        const href = governanceObjectPath({
          organizationId,
          projectId,
          section: "master-data",
          objectType: "business-domain",
          objectId: id,
          tab: "overview",
        });
        return (
          <li key={id} className="list-none">
            {href ? (
              <a href={href} className="text-ui text-primary underline" data-testid={`business-domain-link-${id}`}>
                {name ?? id}
              </a>
            ) : (
              // Unaddressable is not invisible: the ref belongs to the object
              // and stays on the screen, without a link that would open nothing.
              <span className="text-ui text-text-secondary" data-testid={`business-domain-link-${id}`}>
                {name ?? id}
              </span>
            )}
            {names !== null && name === undefined && (
              <Status tone="warning" className="ml-2 text-caption">
                unresolved
              </Status>
            )}
          </li>
        );
      })}
    </ul>
  );
}

export function ExploreDataAction({
  handoff,
  businessDomainId,
  onNavigate,
}: {
  handoff: ExploreHandoff | null | undefined;
  businessDomainId?: string | null;
  onNavigate: (query: Record<string, string>) => void;
}) {
  if (!handoff) {
    return (
      <Status tone="neutral">
        Explore data needs a published version with at least one queryable pair
      </Status>
    );
  }
  const query: Record<string, string> = {
    semantic_view_id: handoff.semantic_view_id,
    semantic_view_version_id: handoff.semantic_view_version_id,
  };
  if (businessDomainId) query.business_domain_id = businessDomainId;
  return (
    <Button variant="default" onClick={() => onNavigate(query)} data-testid="explore-data">
      Explore data
    </Button>
  );
}
