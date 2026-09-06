/**
 * The ONE formula tree editor of the console.
 *
 * WHY IT LEFT `NewConceptDialog`. Story 60.2 built this editor inside that
 * dialog because that dialog was the only place a formula was authored. Story
 * 75-2 gives the same gesture a second door — "Propose as governed field", from
 * the Result Workbench and the pivot explorer — and a second editor would be a
 * second contract: the server validates ONE shape
 * (`core.semantic_expressions`), so two builders means one of them is wrong on
 * a day nobody notices. The markup below is the markup that was there, moved,
 * with the element ids and the accessible names UNCHANGED so the tests that
 * pin them still pin the same thing.
 *
 * Nothing here is SQL and nothing here builds SQL: `buildExpression`
 * (`formulaContract.ts`) is the only serializer, and it is the mirror of the
 * server's own `ALLOWED_OPERATIONS`.
 */
import { AGGREGATION_FUNCTIONS, FORMULA_OPERATION_LABEL, FORMULA_OPERATIONS, OPERAND_OPERATION_LABEL, OPERAND_OPERATIONS, type AggregationFunction, type FormulaDraft, type FormulaOperation, type OperandDraft, type OperandOperation, type ZeroDenominatorPolicy, VALUE_TYPES, ZERO_DENOMINATOR_LABEL, ZERO_DENOMINATOR_POLICIES } from "./formulaContract";
import { Checkbox, Input, Label, label as humanLabel, NativeSelect, Status } from "../ui";

/** A read that failed says so. An empty list is a different fact from a list
 *  that could not be read, and only one of the two means "nothing to point at". */
export type ReferenceState =
  | { status: "loading" }
  | { status: "ready"; options: Array<{ value: string; label: string }> }
  | { status: "error"; message: string };

export function OperandEditor({
  id,
  title,
  operand,
  onChange,
  references,
  disabled,
}: {
  id: string;
  title: string;
  operand: OperandDraft;
  onChange: (next: OperandDraft) => void;
  references: ReferenceState;
  disabled: boolean;
}) {
  return (
    <div className="space-y-1.5 rounded-lg border border-divider-base p-3">
      <span className="text-caption font-semibold text-text-secondary">{title}</span>
      <NativeSelect
        id={`${id}-kind`}
        aria-label={`${title} kind`}
        value={operand.op}
        disabled={disabled}
        onChange={(e) => onChange({ ...operand, op: e.target.value as OperandOperation })}
      >
        {OPERAND_OPERATIONS.map((op) => (
          <option key={op} value={op}>
            {OPERAND_OPERATION_LABEL[op]}
          </option>
        ))}
      </NativeSelect>

      {operand.op === "concept_ref" && references.status === "loading" && (
        <p className="mb-0 text-caption text-text-secondary">Reading this Project&apos;s Concepts…</p>
      )}
      {operand.op === "concept_ref" && references.status === "error" && (
        <Status as="block" tone="error" title="The Concept list could not be read">
          {references.message} No reference can be pinned until it answers — an empty list here
          would read as &quot;this Project has no Concepts&quot;, which is a different fact.
        </Status>
      )}
      {operand.op === "concept_ref" && references.status === "ready" && (
        references.options.length === 0 ? (
          <p className="mb-0 text-caption text-text-secondary">
            No published Concept to reference yet. A formula pins a Concept AND its exact version,
            and this Project has published no Concept version, so there is nothing to point at.
          </p>
        ) : (
          <NativeSelect
            id={`${id}-reference`}
            aria-label={`${title} concept`}
            value={operand.reference}
            disabled={disabled}
            onChange={(e) => onChange({ ...operand, reference: e.target.value })}
          >
            <option value="">Select a Concept version…</option>
            {references.options.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </NativeSelect>
        )
      )}

      {operand.op === "literal" && (
        <div className="grid grid-cols-2 gap-2">
          <Input
            id={`${id}-value`}
            aria-label={`${title} value`}
            placeholder="e.g. 1000"
            value={operand.literalValue}
            disabled={disabled}
            onChange={(e) => onChange({ ...operand, literalValue: e.target.value })}
          />
          <NativeSelect
            id={`${id}-type`}
            aria-label={`${title} value type`}
            value={operand.literalType}
            disabled={disabled}
            onChange={(e) => onChange({ ...operand, literalType: e.target.value })}
          >
            {VALUE_TYPES.map((type) => (
              <option key={type} value={type}>
                {humanLabel(type)}
              </option>
            ))}
          </NativeSelect>
        </div>
      )}
    </div>
  );
}

/**
 * The tree editor itself.
 *
 * `idPrefix` names the four root controls (`{prefix}-formula-op`,
 * `{prefix}-zero-denominator`, `{prefix}-as-percent`,
 * `{prefix}-formula-function`). The operand ids stay unprefixed — `operand-0`,
 * `numerator`, `denominator`, `aggregate-operand` — because only one dialog is
 * ever mounted at a time and those are the names the existing tests read.
 */
export default function FormulaTreeEditor({
  idPrefix,
  formula,
  onChange,
  references,
  disabled,
}: {
  idPrefix: string;
  formula: FormulaDraft;
  onChange: (next: FormulaDraft) => void;
  references: ReferenceState;
  disabled: boolean;
}) {
  const showsOperands =
    formula.op === "add" || formula.op === "subtract" || formula.op === "multiply";

  return (
    <div className="space-y-3 rounded-lg border border-divider-base p-3">
      <div className="space-y-1.5">
        <Label htmlFor={`${idPrefix}-formula-op`}>Formula</Label>
        <NativeSelect
          id={`${idPrefix}-formula-op`}
          value={formula.op}
          disabled={disabled}
          onChange={(e) => onChange({ ...formula, op: e.target.value as FormulaOperation })}
        >
          {FORMULA_OPERATIONS.map((op) => (
            <option key={op} value={op}>
              {FORMULA_OPERATION_LABEL[op]}
            </option>
          ))}
        </NativeSelect>
        <p className="mb-0 text-caption text-text-secondary">
          A formula is a typed tree over Concepts, never SQL. Operations come from the
          one allowlist the server enforces.
        </p>
      </div>

      {formula.op === "source_measure" && (
        <p className="mb-0 text-caption text-text-secondary">
          This Concept has no formula: its value comes from its mapped source measure.
        </p>
      )}

      {showsOperands &&
        formula.operands.map((operand, index) => (
          <OperandEditor
            key={`operand-${index}`}
            id={`operand-${index}`}
            title={`Operand ${index + 1}`}
            operand={operand}
            references={references}
            disabled={disabled}
            onChange={(next) =>
              onChange({
                ...formula,
                operands: formula.operands.map((current, position) =>
                  position === index ? next : current,
                ),
              })
            }
          />
        ))}

      {formula.op === "ratio" && (
        <>
          <OperandEditor
            id="numerator"
            title="Numerator"
            operand={formula.numerator}
            references={references}
            disabled={disabled}
            onChange={(next) => onChange({ ...formula, numerator: next })}
          />
          <OperandEditor
            id="denominator"
            title="Denominator"
            operand={formula.denominator}
            references={references}
            disabled={disabled}
            onChange={(next) => onChange({ ...formula, denominator: next })}
          />
          <div className="space-y-1.5">
            <Label htmlFor={`${idPrefix}-zero-denominator`}>
              When the denominator is zero
            </Label>
            <NativeSelect
              id={`${idPrefix}-zero-denominator`}
              value={formula.zeroDenominator}
              disabled={disabled}
              onChange={(e) =>
                onChange({
                  ...formula,
                  zeroDenominator: e.target.value as ZeroDenominatorPolicy,
                })
              }
            >
              {ZERO_DENOMINATOR_POLICIES.map((policy) => (
                <option key={policy} value={policy}>
                  {ZERO_DENOMINATOR_LABEL[policy]}
                </option>
              ))}
            </NativeSelect>
            <p className="mb-0 text-caption text-text-secondary">
              Leaving it implicit makes an empty day and a zero rate
              indistinguishable.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Checkbox
              id={`${idPrefix}-as-percent`}
              checked={formula.asPercent}
              disabled={disabled}
              onCheckedChange={(checked) =>
                onChange({ ...formula, asPercent: checked === true })
              }
            />
            <Label htmlFor={`${idPrefix}-as-percent`}>Express the result as a percentage</Label>
          </div>
        </>
      )}

      {formula.op === "aggregate" && (
        <>
          <div className="space-y-1.5">
            <Label htmlFor={`${idPrefix}-formula-function`}>Function</Label>
            <NativeSelect
              id={`${idPrefix}-formula-function`}
              value={formula.aggregateFunction}
              disabled={disabled}
              onChange={(e) =>
                onChange({
                  ...formula,
                  aggregateFunction: e.target.value as AggregationFunction,
                })
              }
            >
              {AGGREGATION_FUNCTIONS.map((fn) => (
                <option key={fn} value={fn}>
                  {humanLabel(fn)}
                </option>
              ))}
            </NativeSelect>
          </div>
          <OperandEditor
            id="aggregate-operand"
            title="Operand"
            operand={formula.aggregateOperand}
            references={references}
            disabled={disabled}
            onChange={(next) => onChange({ ...formula, aggregateOperand: next })}
          />
        </>
      )}
    </div>
  );
}
