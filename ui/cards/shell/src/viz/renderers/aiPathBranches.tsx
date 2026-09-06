/** Public, bounded branch evidence for one eligible AI Path retrieval step. */
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { getVizPalette, readCssVizTheme, type VizPalette } from "../../vizTheme";

export const RETRIEVAL_BRANCH_EVIDENCE_SCHEMA = "retrieval-branch-evidence.v1";
export const MAX_BRANCHES_PER_STEP = 24;
export const MAX_BRANCH_EVIDENCE_BYTES = 8_192;
export const MAX_BRANCHES_PER_PATH = 200;
export const MAX_BRANCH_EVIDENCE_BYTES_PER_PATH = 65_536;

export const AI_PATH_BRANCH_FATES = ["selected", "rejected"] as const;
export type AiPathBranchFate = (typeof AI_PATH_BRANCH_FATES)[number];
export const AI_PATH_REJECTION_REASONS = [
  "below_cutoff",
  "date_mismatch",
  "connector_mismatch",
  "anti_trigger",
] as const;
export type AiPathRejectionReason = (typeof AI_PATH_REJECTION_REASONS)[number];
export const AI_PATH_BRANCH_STATES = [
  "branches_listed",
  "no_branch_judged",
  "branches_not_recorded",
  "unavailable",
] as const;
export type AiPathBranchState = (typeof AI_PATH_BRANCH_STATES)[number];
export const AI_PATH_INSPECTION_KINDS = [
  "branch_subtree_expanded",
  "branch_subtree_collapsed",
  "evidence_drilldown_opened",
  "text_fallback_shown",
] as const;
export type AiPathInspectionKind = (typeof AI_PATH_INSPECTION_KINDS)[number];

export const NOT_REACHED_NOTICE =
  "Candidates outside this bounded listing were not enumerated or judged. This is not an exhaustive search.";
export const SELECTED_MEANING =
  "Selected means retained by the retriever; it does not mean the model used or cited the candidate.";
export const NO_BRANCH_JUDGED = "This retrieval step judged no candidate.";
export const BRANCHES_NOT_RECORDED =
  "Branch evidence was not recorded for this step. No candidate or rejection reason is inferred.";
export const BRANCHES_UNAVAILABLE =
  "Branch evidence is unavailable for this step. No candidate or rejection reason is inferred.";

export type AiPathBranchKind = "topic" | "procedure" | "schema_doc" | "context_event";
export type AiPathBranchTier = "title" | "description" | "neighbor";

export interface AiPathBranch {
  id: string;
  kind: AiPathBranchKind;
  title: string;
  score: number | null;
  tier: AiPathBranchTier | null;
  matched: boolean | null;
  rank: number;
  fate: AiPathBranchFate;
  reason: AiPathRejectionReason | null;
}

export interface AiPathWalk {
  producer: "context_search" | "briefing_context_event";
  mode: string;
  graph_hop_depth: number;
  semantic_recall: boolean;
  selection_limit: number;
  judged_count: number;
  selected_count: number;
  rejected_count: number;
  listed_count: number;
  listing_truncated: boolean;
  not_reached_enumerated: false;
  tier_scale: { title: number; description: number; neighbor: number } | null;
}

export type AiPathBranchEvidence =
  | {
      schema_version: typeof RETRIEVAL_BRANCH_EVIDENCE_SCHEMA;
      state: "branches_listed";
      walk: AiPathWalk;
      branches: AiPathBranch[];
    }
  | {
      schema_version: typeof RETRIEVAL_BRANCH_EVIDENCE_SCHEMA;
      state: "no_branch_judged";
      walk: AiPathWalk;
      branches: AiPathBranch[];
    }
  | {
      schema_version: typeof RETRIEVAL_BRANCH_EVIDENCE_SCHEMA;
      state: "branches_not_recorded";
    }
  | {
      schema_version: typeof RETRIEVAL_BRANCH_EVIDENCE_SCHEMA;
      state: "unavailable";
    };

export interface AiPathInspection {
  kind: AiPathInspectionKind;
  stepOrdinal: number | null;
  displayedState: AiPathBranchState;
  branchesListed: number | null;
}

export type AiPathInspectionSink = (inspection: AiPathInspection) => void;
type JsonRecord = Record<string, unknown>;

function malformed(reason: string): never {
  throw new Error(`Malformed branch evidence: ${reason}`);
}

function record(value: unknown, name: string): JsonRecord {
  if (!value || typeof value !== "object" || Array.isArray(value)) malformed(`${name} object`);
  return value as JsonRecord;
}

function exactKeys(value: JsonRecord, keys: readonly string[], name: string): void {
  const expected = new Set(keys);
  const actual = Object.keys(value);
  const unknown = actual.find((key) => !expected.has(key));
  const missing = keys.find((key) => !(key in value));
  if (unknown || missing || actual.length !== keys.length) {
    malformed(`${name} keys`);
  }
}

function boundedInteger(value: unknown, name: string, maximum?: number): number {
  if (
    !Number.isSafeInteger(value) ||
    (value as number) < 0 ||
    (maximum !== undefined && (value as number) > maximum)
  ) {
    malformed(name);
  }
  return value as number;
}

function finiteNumber(value: unknown, name: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) malformed(name);
  return value;
}

function containsUnsafeControl(value: string): boolean {
  return /[\p{Cc}\p{Cf}]/u.test(value);
}

function safeText(value: unknown, name: string, maximum: number, title = false): string {
  const characters = typeof value === "string" ? [...value] : [];
  if (
    typeof value !== "string" ||
    characters.length === 0 ||
    characters.length > maximum ||
    characters.some((character) => {
      const point = character.codePointAt(0) ?? 0;
      return point >= 0xd800 && point <= 0xdfff;
    }) ||
    value.normalize("NFC") !== value ||
    containsUnsafeControl(value) ||
    (title && value.trim().split(/\s+/u).join(" ") !== value)
  ) {
    malformed(name);
  }
  return value;
}

/** Python `repr(float)` using ECMAScript's shortest round-trippable digits. */
export function pythonFloatJson(value: number): string {
  if (!Number.isFinite(value)) throw new Error("non-finite canonical float");
  if (Object.is(value, -0)) return "-0.0";
  if (value === 0) return "0.0";
  const prefix = value < 0 ? "-" : "";
  const raw = Math.abs(value).toString().toLowerCase();
  let digits: string;
  let exponent: number;
  if (raw.includes("e")) {
    const [mantissa, rawExponent] = raw.split("e");
    digits = mantissa!.replace(".", "").replace(/^0+/u, "").replace(/0+$/u, "");
    exponent = Number(rawExponent);
  } else {
    const [integer, fraction = ""] = raw.split(".");
    if (integer !== "0") {
      exponent = integer!.length - 1;
      digits = `${integer}${fraction}`.replace(/^0+/u, "").replace(/0+$/u, "");
    } else {
      const first = fraction.search(/[1-9]/u);
      exponent = -(first + 1);
      digits = fraction.slice(first).replace(/0+$/u, "");
    }
  }
  if (exponent < -4 || exponent >= 16) {
    const mantissa = digits.length === 1 ? digits : `${digits[0]}.${digits.slice(1)}`;
    const sign = exponent < 0 ? "-" : "+";
    return `${prefix}${mantissa}e${sign}${Math.abs(exponent).toString().padStart(2, "0")}`;
  }
  const point = exponent + 1;
  if (point <= 0) return `${prefix}0.${"0".repeat(-point)}${digits}`;
  if (point >= digits.length) {
    return `${prefix}${digits}${"0".repeat(point - digits.length)}.0`;
  }
  return `${prefix}${digits.slice(0, point)}.${digits.slice(point)}`;
}

function canonicalJson(value: unknown, path: readonly string[] = []): string {
  if (value === null || typeof value === "boolean" || typeof value === "string") {
    return JSON.stringify(value);
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error("non-finite canonical number");
    const key = path.at(-1);
    const parent = path.at(-2);
    return key === "score" || parent === "tier_scale"
      ? pythonFloatJson(value)
      : JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map((item, index) => canonicalJson(item, [...path, String(index)])).join(",")}]`;
  }
  if (value && typeof value === "object") {
    const object = value as JsonRecord;
    return `{${Object.keys(object)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(object[key], [...path, key])}`)
      .join(",")}}`;
  }
  throw new Error("non-JSON canonical value");
}

function serializedBytes(value: unknown): number {
  try {
    return new TextEncoder().encode(canonicalJson(value)).byteLength;
  } catch {
    return Number.POSITIVE_INFINITY;
  }
}

function decodeWalk(value: unknown): AiPathWalk {
  const walk = record(value, "walk");
  exactKeys(
    walk,
    [
      "producer",
      "mode",
      "graph_hop_depth",
      "semantic_recall",
      "selection_limit",
      "judged_count",
      "selected_count",
      "rejected_count",
      "listed_count",
      "listing_truncated",
      "not_reached_enumerated",
      "tier_scale",
    ],
    "walk",
  );
  if (walk.producer !== "context_search" && walk.producer !== "briefing_context_event") {
    malformed("walk producer");
  }
  safeText(walk.mode, "walk mode", 128, true);
  boundedInteger(walk.graph_hop_depth, "graph hop depth");
  const selectionLimit = boundedInteger(walk.selection_limit, "selection limit");
  const judged = boundedInteger(walk.judged_count, "judged count");
  const selected = boundedInteger(walk.selected_count, "selected count");
  const rejected = boundedInteger(walk.rejected_count, "rejected count");
  boundedInteger(walk.listed_count, "listed count bound", MAX_BRANCHES_PER_STEP);
  if (judged !== selected + rejected) malformed("count algebra");
  if (selected > selectionLimit) malformed("selection limit");
  if (typeof walk.semantic_recall !== "boolean") malformed("semantic recall");
  if (typeof walk.listing_truncated !== "boolean") malformed("listing truncated");
  if (walk.not_reached_enumerated !== false) malformed("unreached candidates");
  if (walk.producer === "context_search") {
    const scale = record(walk.tier_scale, "tier scale");
    exactKeys(scale, ["title", "description", "neighbor"], "tier scale");
    finiteNumber(scale.title, "title tier");
    finiteNumber(scale.description, "description tier");
    finiteNumber(scale.neighbor, "neighbor tier");
  } else if (walk.tier_scale !== null) {
    malformed("briefing tier scale");
  }
  return walk as unknown as AiPathWalk;
}

function decodeBranch(value: unknown, walk: AiPathWalk): AiPathBranch {
  const branch = record(value, "branch");
  exactKeys(
    branch,
    ["id", "kind", "title", "score", "tier", "matched", "rank", "fate", "reason"],
    "branch",
  );
  if (
    typeof branch.id !== "string" ||
    !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/.test(branch.id)
  ) {
    malformed("branch id");
  }
  safeText(branch.title, "branch title", 200, true);
  boundedInteger(branch.rank, "branch rank");
  if ((branch.rank as number) < 1) malformed("branch rank");
  if (branch.fate !== "selected" && branch.fate !== "rejected") malformed("branch fate");
  if (branch.fate === "selected" && branch.reason !== null) malformed("selected reason");
  const searchReasons = new Set(["below_cutoff", "anti_trigger"]);
  const briefingReasons = new Set(["below_cutoff", "date_mismatch", "connector_mismatch"]);
  if (
    branch.fate === "rejected" &&
    (typeof branch.reason !== "string" ||
      !(walk.producer === "context_search" ? searchReasons : briefingReasons).has(branch.reason))
  ) {
    malformed("rejection reason");
  }
  if (walk.producer === "context_search") {
    if (!new Set(["topic", "procedure", "schema_doc"]).has(branch.kind as string)) {
      malformed("search branch kind");
    }
    finiteNumber(branch.score, "branch score");
    if (!new Set(["title", "description", "neighbor"]).has(branch.tier as string)) {
      malformed("branch tier");
    }
    if (typeof branch.matched !== "boolean") malformed("branch matched");
  } else if (
    branch.kind !== "context_event" ||
    branch.score !== null ||
    branch.tier !== null ||
    branch.matched !== null
  ) {
    malformed("briefing branch metadata");
  }
  return branch as unknown as AiPathBranch;
}

/** Decode only the closed public contract; producer detail and unknown prose fail closed. */
export function decodeAiPathBranchEvidence(value: unknown): AiPathBranchEvidence {
  if (serializedBytes(value) > MAX_BRANCH_EVIDENCE_BYTES) malformed("byte bound");
  const evidence = record(value, "branch evidence");
  if (evidence.schema_version !== RETRIEVAL_BRANCH_EVIDENCE_SCHEMA) {
    malformed("schema version");
  }
  if (!AI_PATH_BRANCH_STATES.includes(evidence.state as AiPathBranchState)) {
    malformed("state");
  }
  if (evidence.state === "branches_not_recorded" || evidence.state === "unavailable") {
    exactKeys(evidence, ["schema_version", "state"], "stale evidence");
    return evidence as unknown as AiPathBranchEvidence;
  }
  exactKeys(evidence, ["schema_version", "state", "walk", "branches"], "evidence");
  const walk = decodeWalk(evidence.walk);
  if (!Array.isArray(evidence.branches) || evidence.branches.length > MAX_BRANCHES_PER_STEP) {
    malformed("branches bound");
  }
  const branches = evidence.branches.map((branch) => decodeBranch(branch, walk));
  if (walk.listed_count !== branches.length || walk.listed_count > walk.judged_count) {
    malformed("listed count");
  }
  if (walk.listing_truncated !== (walk.listed_count < walk.judged_count)) {
    malformed("listing truncation");
  }
  for (let index = 1; index < branches.length; index += 1) {
    if (branches[index]!.rank <= branches[index - 1]!.rank) malformed("branch rank order");
  }
  const shownSelected = branches.filter((branch) => branch.fate === "selected").length;
  const shownRejected = branches.length - shownSelected;
  if (shownSelected > walk.selected_count || shownRejected > walk.rejected_count) {
    malformed("shown fate counts");
  }
  if (evidence.state === "branches_listed" && branches.length === 0) {
    malformed("listed state empty");
  }
  if (
    evidence.state === "no_branch_judged" &&
    (branches.length !== 0 || walk.judged_count !== 0 || walk.listing_truncated)
  ) {
    malformed("no branch state");
  }
  return { ...evidence, walk, branches } as AiPathBranchEvidence;
}

export function branchEvidenceCanonicalBytes(value: AiPathBranchEvidence): number {
  return serializedBytes(value);
}

export interface AiPathBranchEvidenceAllocationInput {
  ordinal: number;
  eligible: boolean;
  evidence?: unknown;
  producer?: AiPathWalk["producer"];
}

const UNAVAILABLE_BRANCH_EVIDENCE: AiPathBranchEvidence = Object.freeze({
  schema_version: RETRIEVAL_BRANCH_EVIDENCE_SCHEMA,
  state: "unavailable",
});

/** Reapply the server's whole-path walls before either public UI adapter renders. */
export function allocateAiPathBranchEvidence(
  inputs: readonly AiPathBranchEvidenceAllocationInput[],
): (AiPathBranchEvidence | undefined)[] {
  const output: (AiPathBranchEvidence | undefined)[] = Array(inputs.length).fill(undefined);
  const ordered = inputs
    .map((input, index) => ({ input, index }))
    .filter(({ input }) => input.eligible)
    .sort((left, right) => left.input.ordinal - right.input.ordinal || left.index - right.index);
  let candidatesUsed = 0;
  let bytesUsed = 0;
  let budgetExhausted = false;
  for (const { input, index } of ordered) {
    if (budgetExhausted) {
      output[index] = UNAVAILABLE_BRANCH_EVIDENCE;
      continue;
    }
    let evidence: AiPathBranchEvidence;
    try {
      evidence = decodeAiPathBranchEvidence(input.evidence);
      if (
        input.producer &&
        "walk" in evidence &&
        evidence.walk.producer !== input.producer
      ) {
        throw new Error("producer mismatch");
      }
    } catch {
      output[index] = UNAVAILABLE_BRANCH_EVIDENCE;
      continue;
    }
    const candidates = "branches" in evidence ? evidence.branches.length : 0;
    const bytes = serializedBytes(evidence);
    if (
      candidatesUsed + candidates > MAX_BRANCHES_PER_PATH ||
      bytesUsed + bytes > MAX_BRANCH_EVIDENCE_BYTES_PER_PATH
    ) {
      output[index] = UNAVAILABLE_BRANCH_EVIDENCE;
      budgetExhausted = true;
      continue;
    }
    output[index] = evidence;
    candidatesUsed += candidates;
    bytesUsed += bytes;
  }
  return output;
}

export function branchListingState(evidence: AiPathBranchEvidence): AiPathBranchState {
  return evidence.state;
}

export function branchFate(branch: AiPathBranch): AiPathBranchFate {
  return branch.fate;
}

export function branchReason(branch: AiPathBranch): AiPathRejectionReason | null {
  return branch.reason;
}

const DASH = "—";

export function walkStatement(walk: AiPathWalk): string {
  return [
    `${walk.mode} matching`,
    `${walk.graph_hop_depth} graph hop${walk.graph_hop_depth === 1 ? "" : "s"}`,
    walk.semantic_recall ? "semantic recall" : "no semantic recall",
    `selection limit ${walk.selection_limit}`,
  ].join(", ") + ".";
}

export function tierScaleStatement(walk: AiPathWalk): string {
  if (!walk.tier_scale) return "";
  return `Tier scale: title ${walk.tier_scale.title}, description ${walk.tier_scale.description}, neighbor ${walk.tier_scale.neighbor}.`;
}

function countStatement(walk: AiPathWalk): string {
  return `${walk.judged_count} judged · ${walk.selected_count} selected · ${walk.rejected_count} rejected · ${walk.listed_count} shown.`;
}

export function aiPathBranchesText(options: {
  evidence: AiPathBranchEvidence;
  stepLabel?: string | null;
}): string {
  const { evidence, stepLabel } = options;
  const head = stepLabel ? `${stepLabel}: ` : "";
  if (evidence.state === "branches_not_recorded") return `${head}${BRANCHES_NOT_RECORDED}`;
  if (evidence.state === "unavailable") return `${head}${BRANCHES_UNAVAILABLE}`;
  const lines = [
    `${head}${countStatement(evidence.walk)}`,
    walkStatement(evidence.walk),
  ];
  const tierScale = tierScaleStatement(evidence.walk);
  if (tierScale) lines.push(tierScale);
  if (evidence.state === "no_branch_judged") lines.push(NO_BRANCH_JUDGED);
  for (const branch of evidence.branches) {
    lines.push(
      `- #${branch.rank} · ${branch.title} · ${branch.kind} · score ${branch.score ?? "n/a"} · tier ${branch.tier ?? "n/a"} · match ${branch.matched ?? "n/a"} · ${branch.fate}${branch.reason ? ` · ${branch.reason}` : ""}`,
    );
  }
  lines.push(SELECTED_MEANING, NOT_REACHED_NOTICE);
  return lines.join("\n");
}

export function safeInspect(
  sink: AiPathInspectionSink | undefined | null,
  inspection: AiPathInspection,
): boolean {
  if (!sink) return false;
  try {
    sink(inspection);
    return true;
  } catch {
    return false;
  }
}

export interface AiPathBranchSubtreeProps {
  evidence: AiPathBranchEvidence;
  stepOrdinal?: number | null;
  stepLabel?: string | null;
  defaultOpen?: boolean;
  palette?: VizPalette;
  onInspect?: AiPathInspectionSink;
  textOnly?: boolean;
}

export default function AiPathBranchSubtree({
  evidence,
  stepOrdinal = null,
  stepLabel = null,
  defaultOpen = false,
  palette,
  onInspect,
  textOnly = false,
}: AiPathBranchSubtreeProps) {
  const colours = useMemo(() => palette ?? getVizPalette(readCssVizTheme()), [palette]);
  const [open, setOpen] = useState(defaultOpen);
  const panelId = `ai-path-branches-${useId().replace(/:/g, "")}`;
  const listed = evidence.state === "branches_listed" ? evidence.walk.listed_count : null;
  const recordInspection = useCallback(
    (kind: AiPathInspectionKind) => {
      safeInspect(onInspect, {
        kind,
        stepOrdinal,
        displayedState: evidence.state,
        branchesListed: listed,
      });
    },
    [evidence.state, listed, onInspect, stepOrdinal],
  );
  useEffect(() => {
    if (textOnly) recordInspection("text_fallback_shown");
  }, [recordInspection, textOnly]);
  const text = aiPathBranchesText({ evidence, stepLabel });
  if (textOnly) {
    return (
      <pre className="whitespace-pre-wrap text-xs opacity-80" data-testid="ai-path-branch-fallback" data-branch-state={evidence.state}>
        {text}
      </pre>
    );
  }
  const summary =
    evidence.state === "branches_listed" || evidence.state === "no_branch_judged"
      ? countStatement(evidence.walk)
      : "branch count unknown";
  const humanOrdinal = stepOrdinal === null ? "unknown" : String(stepOrdinal + 1);
  return (
    <div className="flex flex-col gap-1" data-testid="ai-path-branches" data-branch-state={evidence.state}>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        aria-label={`Branch evidence for AI Path step ${humanOrdinal}: ${summary}`}
        className="w-fit rounded-md border px-2 py-0.5 text-left text-xs"
        style={{ borderColor: colours.hairline }}
        data-testid="ai-path-branch-toggle"
        onClick={() => {
          const next = !open;
          setOpen(next);
          recordInspection(next ? "branch_subtree_expanded" : "branch_subtree_collapsed");
        }}
      >
        {open ? "▾" : "▸"} {summary}
      </button>
      <div
        id={panelId}
        hidden={!open}
        className="flex flex-col gap-2 border-l pl-3"
        style={{ borderColor: colours.hairline }}
      >
          {evidence.state === "branches_listed" || evidence.state === "no_branch_judged" ? (
            <>
              <p className="m-0 text-xs font-medium">{countStatement(evidence.walk)}</p>
              <p className="m-0 text-xs opacity-70">
                {walkStatement(evidence.walk)} {tierScaleStatement(evidence.walk)}
              </p>
              {evidence.state === "branches_listed" ? (
                <BranchTable branches={evidence.branches} colours={colours} />
              ) : (
                <p className="m-0 text-xs opacity-70">{NO_BRANCH_JUDGED}</p>
              )}
              <p className="m-0 text-xs opacity-70">{SELECTED_MEANING}</p>
              <p className="m-0 text-xs opacity-60">{NOT_REACHED_NOTICE}</p>
            </>
          ) : (
            <p className="m-0 text-xs opacity-70">
              {evidence.state === "branches_not_recorded"
                ? BRANCHES_NOT_RECORDED
                : BRANCHES_UNAVAILABLE}
            </p>
          )}
      </div>
    </div>
  );
}

function BranchTable({ branches, colours }: { branches: AiPathBranch[]; colours: VizPalette }): ReactNode {
  return (
    <div
      className="overflow-x-auto rounded-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2"
      role="region"
      aria-label="Branch evidence table"
      tabIndex={0}
    >
      <table className="w-full border-collapse text-xs" aria-label="Branch evidence candidates">
        <thead>
          <tr>
            {['Rank', 'Candidate', 'Kind', 'Score', 'Tier', 'Match', 'Fate', 'Reason'].map((label) => (
              <th key={label} scope="col" className="py-1 pr-2 text-left font-medium">{label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {branches.map((branch) => (
            <tr key={branch.id} data-branch-id={branch.id} data-branch-fate={branch.fate} style={{ borderTop: `1px solid ${colours.hairline}` }}>
              <td className="py-1 pr-2 tabular-nums">{branch.rank}</td>
              <th scope="row" className="py-1 pr-2 text-left font-medium">{branch.title}</th>
              <td className="py-1 pr-2">{branch.kind}</td>
              <td className="py-1 pr-2 tabular-nums">{branch.score ?? DASH}</td>
              <td className="py-1 pr-2">{branch.tier ?? DASH}</td>
              <td className="py-1 pr-2">{branch.matched === null ? DASH : branch.matched ? "direct" : "graph"}</td>
              <td className="py-1 pr-2">{branch.fate}</td>
              <td className="py-1 pr-2">{branch.reason ?? DASH}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
