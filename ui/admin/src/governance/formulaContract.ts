/**
 * The console half of `core.semantic_expressions`.
 *
 * WHY THIS FILE EXISTS AT ALL. `ALLOWED_OPERATIONS`
 * (`server/core/semantic_expressions.py:626-628`) carries its own reason in its
 * comment: "Exported so the workbench builds its palette from the SAME list the
 * server enforces, instead of a hand-kept copy that drifts." Until story 60.2
 * the workbench had neither a copy nor a palette — it composed
 * `{op: "formula", text}`, an operation the server has never had, so every
 * formula anyone typed was refused with `unknown_operation` and no Concept of
 * kind `metric` could be created from the console at all.
 *
 * A TypeScript file cannot import a Python frozenset, so this is a mirror. What
 * keeps a mirror honest is a test that compares it to the original:
 * `server/tests/core/test_semantic_dialog_paths.py` reads THIS file and asserts
 * every value below against the module, so a drift breaks a test rather than a
 * person's afternoon.
 *
 * Nothing here is SQL and nothing here builds SQL. A formula is a typed tree.
 */

/** The operations this dialog offers. A SUBSET of the server allowlist, and the
 *  subset is deliberate:
 *   - `concept_name` parses but can never be published (`:304-315`), so offering
 *     it would offer a dead end;
 *   - `comparison` returns a boolean and is only meaningful INSIDE a
 *     conditional, which this editor does not compose yet;
 *   - `conditional` needs a branch editor and is named in the story's plan as
 *     part of the same family — it is not offered here rather than offered
 *     half-built.
 *  Everything offered IS in `ALLOWED_OPERATIONS`; that is what the test pins. */
export const FORMULA_OPERATIONS = [
  "source_measure",
  "add",
  "subtract",
  "multiply",
  "ratio",
  "aggregate",
] as const;
export type FormulaOperation = (typeof FORMULA_OPERATIONS)[number];

/** The operand shapes a leaf of the tree can take. All three are server ops. */
export const OPERAND_OPERATIONS = ["source_measure", "concept_ref", "literal"] as const;
export type OperandOperation = (typeof OPERAND_OPERATIONS)[number];

/** `AGGREGATION_FUNCTIONS` (`semantic_expressions.py:63-65`). LOWERCASE, and
 *  `average` — not `avg`. The dialog used to offer `SUM/AVG/COUNT/...`: six
 *  values, six refusals, because the server matches these exact strings. */
export const AGGREGATION_FUNCTIONS = [
  "sum",
  "average",
  "count",
  "count_distinct",
  "min",
  "max",
  "median",
] as const;
export type AggregationFunction = (typeof AGGREGATION_FUNCTIONS)[number];

/** `ADDITIVITY_CLASSES` (`:71`) and the CHECK of `142_semantic_model.sql:157-160`.
 *  THREE classes and three labels. The epic plan writes "Additive /
 *  Non-Additive Ratio / Non-Additive Snapshot": those are CASES of these three,
 *  not a fourth vocabulary — a snapshot is `semi_additive` plus the dimensions
 *  it may not be summed across, which is the second field below. */
export const ADDITIVITY_CLASSES = ["additive", "semi_additive", "non_additive"] as const;
export type AdditivityClass = (typeof ADDITIVITY_CLASSES)[number];

export const ADDITIVITY_LABEL: Record<AdditivityClass, string> = {
  additive: "Additive",
  semi_additive: "Semi-additive",
  non_additive: "Non-additive",
};

export const ADDITIVITY_HINT: Record<AdditivityClass, string> = {
  additive: "Correct when rolled up across every dimension. Days, channels and countries all add.",
  semi_additive:
    "Summable across some dimensions and never across others — a stock, a balance, a snapshot. Name the dimensions it must not cross.",
  non_additive:
    "Never summable. A rate is recomputed from its numerator and denominator at each grain, never added.",
};

/** `ZERO_DENOMINATOR_POLICIES` (`:73`). A ratio that does not declare one is
 *  refused with `undeclared_zero_behavior` (`:460-467`) — deliberately no
 *  default, because an empty day and a zero rate are not the same fact. */
export const ZERO_DENOMINATOR_POLICIES = ["null", "zero", "error"] as const;
export type ZeroDenominatorPolicy = (typeof ZERO_DENOMINATOR_POLICIES)[number];

export const ZERO_DENOMINATOR_LABEL: Record<ZeroDenominatorPolicy, string> = {
  null: "No value (the day is blank)",
  zero: "Zero",
  error: "Refuse the row",
};

/** `VALUE_TYPES` (`:40-53`), for the literal operand and the Concept itself. */
export const VALUE_TYPES = [
  "integer",
  "decimal",
  "money",
  "ratio",
  "percent",
  "duration",
  "string",
  "date",
  "timestamp",
  "boolean",
] as const;

/** A dimension declares its semantic type or the server refuses it with
 *  `undeclared_semantic_type` (`semantic_model.py:975-982`) AND migration 142
 *  refuses the row (`ck_semantic_concept_versions_dimension`). The dialog never
 *  sent one, so a dimension could not be created either. */
export const SEMANTIC_TYPES = [
  "time",
  "geography",
  "channel",
  "audience",
  "content",
  "entity",
  "other",
] as const;

export interface OperandDraft {
  op: OperandOperation;
  /** `concept_ref`: the `"conceptId|versionId"` pair chosen from real Concepts. */
  reference: string;
  /** `literal`: the scalar and its declared type. */
  literalValue: string;
  literalType: string;
}

export interface FormulaDraft {
  op: FormulaOperation;
  operands: OperandDraft[];
  numerator: OperandDraft;
  denominator: OperandDraft;
  zeroDenominator: ZeroDenominatorPolicy;
  asPercent: boolean;
  aggregateFunction: AggregationFunction;
  aggregateOperand: OperandDraft;
}

export function emptyOperand(): OperandDraft {
  return { op: "source_measure", reference: "", literalValue: "", literalType: "decimal" };
}

export function emptyFormula(): FormulaDraft {
  return {
    op: "source_measure",
    operands: [emptyOperand(), emptyOperand()],
    numerator: emptyOperand(),
    denominator: emptyOperand(),
    zeroDenominator: "null",
    asPercent: false,
    aggregateFunction: "sum",
    aggregateOperand: emptyOperand(),
  };
}

/** One refusal the browser can make WITHOUT the server: a `concept_ref` that
 *  names no Concept. Everything else — types, units, currencies, additivity
 *  contradictions — is the server's to name, and this dialog displays its exact
 *  code and JSON path rather than paraphrasing it. */
export function operandIsIncomplete(operand: OperandDraft): boolean {
  if (operand.op === "concept_ref") return !operand.reference.includes("|");
  if (operand.op === "literal") return operand.literalValue.trim() === "";
  return false;
}

function buildOperand(operand: OperandDraft, conceptName: string): Record<string, unknown> {
  if (operand.op === "concept_ref") {
    const [conceptId, versionId] = operand.reference.split("|");
    // BOTH ids, always: "a reference to a Concept without a version follows
    // `latest` and is not a reference" (`semantic_expressions.py:281-283`).
    return { op: "concept_ref", concept_id: conceptId, version_id: versionId };
  }
  if (operand.op === "literal") {
    const raw = operand.literalValue.trim();
    const numeric = Number(raw);
    const isNumericType = operand.literalType !== "string" && operand.literalType !== "boolean";
    return {
      op: "literal",
      value_type: operand.literalType,
      value: isNumericType && raw !== "" && !Number.isNaN(numeric) ? numeric : raw,
    };
  }
  return { op: "source_measure", concept: conceptName };
}

/** The typed tree this dialog posts. Every `op` it can produce is in
 *  `FORMULA_OPERATIONS` or `OPERAND_OPERATIONS`, and both are asserted against
 *  the server allowlist by the test named at the top of this file. */
export function buildExpression(
  draft: FormulaDraft,
  conceptName: string,
): Record<string, unknown> {
  switch (draft.op) {
    case "add":
    case "subtract":
    case "multiply":
      return {
        op: draft.op,
        operands: draft.operands.map((operand) => buildOperand(operand, conceptName)),
      };
    case "ratio":
      return {
        op: "ratio",
        numerator: buildOperand(draft.numerator, conceptName),
        denominator: buildOperand(draft.denominator, conceptName),
        zero_denominator: draft.zeroDenominator,
        as_percent: draft.asPercent,
      };
    case "aggregate":
      return {
        op: "aggregate",
        function: draft.aggregateFunction,
        operand: buildOperand(draft.aggregateOperand, conceptName),
      };
    default:
      return { op: "source_measure", concept: conceptName };
  }
}

/** The operands a given root actually reads — used both by the editor and by
 *  the completeness check, so the two cannot disagree about which fields the
 *  person still has to fill. */
export function activeOperands(draft: FormulaDraft): OperandDraft[] {
  switch (draft.op) {
    case "add":
    case "subtract":
    case "multiply":
      return draft.operands;
    case "ratio":
      return [draft.numerator, draft.denominator];
    case "aggregate":
      return [draft.aggregateOperand];
    default:
      return [];
  }
}

export const FORMULA_OPERATION_LABEL: Record<FormulaOperation, string> = {
  source_measure: "No formula — the value comes from the mapped source measure",
  add: "Add",
  subtract: "Subtract",
  multiply: "Multiply",
  ratio: "Ratio (divide)",
  aggregate: "Aggregate",
};

export const OPERAND_OPERATION_LABEL: Record<OperandOperation, string> = {
  source_measure: "This Concept's own mapped measure",
  concept_ref: "Another Concept, at an exact version",
  literal: "A fixed value",
};
