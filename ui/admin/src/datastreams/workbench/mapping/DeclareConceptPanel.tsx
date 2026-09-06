/**
 * Declare a project-scoped CANONICAL FIELD from the field's own row (amendment
 * 5), and — since story 60.2's engine reached only Governance — publish a
 * SEMANTIC CONCEPT from here.
 *
 * TWO OBJECTS, AND THE COPY NAMES WHICH (glossary, arbitration 2026-08-14). The
 * `collected` door appends a row to `app.mdm_canonical_fields`: a name a binding
 * is checked against, no formula, no version. The `calculated` door publishes a
 * versioned `app.semantic_concepts` definition carrying an expression. The rule
 * is explicit — *a control that mints names the object it mints* — so no label
 * here says the unqualified word « concept » for the gesture: the file name and
 * the `data-testid`s keep it, because a module path and a test selector are not
 * product words.
 *
 * WHY IT IS HERE AND NOT ONLY IN THE FILE PREVIEW. The one door that mints a
 * project canonical field lived in `FileSourceSamplePanel`, behind a file upload
 * whose preview had to come back `flagged` or `unmatched` before the catalog was
 * even requested — so a column could sit on this tab with no canonical field and
 * no way to give it one. Same route, same registry, reached from the row that
 * needs it.
 *
 * WHY THE SECOND DOOR. Measured: this panel posted `canonical_name`,
 * `concept_kind`, `value_type` and `aggregation` and nothing else, because the
 * registry it writes has no expression column — `canonical_field_registry`
 * accepts seven keys and none of them is a formula. So `CPA = cost /
 * conversions` meant leaving the Datastream, finding the Governance screen,
 * creating it there and coming back to bind. Both doors are now this panel's,
 * and the FIRST question is which one applies.
 *
 * ONE ENGINE, AND IT IS NOT REOPENED HERE. `derived_columns.py` is deprecated
 * with its replacement named by case: a CALCULATED VALUE is
 * `semantic_expressions.py`. The calculated door therefore posts the same
 * three-call change set `governance/NewConceptDialog.tsx` posts, and builds its
 * tree with `governance/formulaContract.ts` — imported, never restated, because
 * that file exists to be the single mirror of `ALLOWED_OPERATIONS`.
 *
 * WHAT WRITES WHEN. Declaring a collected canonical field IS an immediate write:
 * it appends a row to `app.mdm_canonical_fields`. BINDING the column to it does
 * not — that leaves through `Prepare mapping change` like every other decision
 * on this tab. A Semantic Concept is published outright, and binds no column:
 * `mdm_target` resolves against `app.mdm_canonical_fields`, so pointing a
 * binding at a Semantic Concept id would make the row `blocking` with
 * `register_or_pick_canonical_field`. The panel says so rather than composing a
 * binding the mapping append refuses.
 *
 * A PLATFORM FIELD IS NOT DECLARED HERE, and the server refuses it by name: the
 * shared vocabulary changes what every project of the instance aligns on.
 */
import { useEffect, useMemo, useState } from "react";
import { Button, Checkbox, Input, NativeSelect, Status, label as humanLabel } from "../../../ui";
import {
  ADDITIVITY_CLASSES,
  ADDITIVITY_HINT,
  ADDITIVITY_LABEL,
  AGGREGATION_FUNCTIONS,
  FORMULA_OPERATIONS,
  FORMULA_OPERATION_LABEL,
  OPERAND_OPERATIONS,
  OPERAND_OPERATION_LABEL,
  ZERO_DENOMINATOR_LABEL,
  ZERO_DENOMINATOR_POLICIES,
  activeOperands,
  emptyFormula,
  emptyOperand,
  operandIsIncomplete,
  type AdditivityClass,
  type AggregationFunction,
  type FormulaDraft,
  type FormulaOperation,
  type OperandDraft,
  type OperandOperation,
  type ZeroDenominatorPolicy,
} from "../../../governance/formulaContract";
import {
  AGGREGATIONS,
  VALUE_TYPES,
  ConceptRefused,
  conceptKey,
  createCalculatedConcept,
  groupReferences,
  useSemanticConceptReferences,
  type CanonicalCatalog,
  type CanonicalField,
  type ConceptReferences,
  type ServerRefusal,
} from "./canonicalFields";

/** The two ways a project gets a value, and they are two different registries.
 *  Asked first, because every question below it changes with the answer. */
type ValueSource = "collected" | "calculated";

/** `source_measure` is the COLLECTED door in formula clothing — "the value
 *  comes from the mapped source measure" is what the other branch of this panel
 *  already does, with a binding. Offering it here would offer the same thing
 *  twice under two names. */
const CALCULATED_OPERATIONS = FORMULA_OPERATIONS.filter((op) => op !== "source_measure");

function OperandFields({
  id,
  title,
  operand,
  onChange,
  references,
  fields,
  disabled,
}: {
  id: string;
  title: string;
  operand: OperandDraft;
  onChange: (next: OperandDraft) => void;
  references: ConceptReferences;
  fields: CanonicalField[];
  disabled: boolean;
}) {
  const grouped = useMemo(
    () =>
      references.status === "ready"
        ? groupReferences(references.options, fields)
        : { inVocabulary: [], elsewhere: [], unpinnable: [] },
    [references, fields],
  );

  return (
    <div className="grid gap-1 border-t border-divider-base p-5" data-testid={`operand-${id}`}>
      <span className="text-caption text-text-secondary">{title}</span>
      <NativeSelect
        aria-label={`${title} kind`}
        data-testid={`operand-kind-${id}`}
        value={operand.op}
        disabled={disabled}
        onChange={(event: React.ChangeEvent<HTMLSelectElement>) =>
          onChange({ ...operand, op: event.target.value as OperandOperation })
        }
      >
        {OPERAND_OPERATIONS.map((op) => (
          <option key={op} value={op}>{OPERAND_OPERATION_LABEL[op]}</option>
        ))}
      </NativeSelect>

      {operand.op === "concept_ref" && references.status === "loading" && (
        <span className="text-caption text-text-secondary">Reading this project's concepts…</span>
      )}
      {operand.op === "concept_ref" && references.status === "error" && (
        <Status as="block" tone="error" title="The concept list could not be read">
          {references.message} No reference can be pinned until it answers — an empty list here
          would read as "this project has no concept", which is a different fact.
        </Status>
      )}
      {operand.op === "concept_ref" && references.status === "ready" && (
        references.options.length === 0 ? (
          <p className="mb-0 text-caption text-text-secondary">
            No published Concept to reference yet. A formula pins a Concept AND its exact version,
            and this project has published no Concept version, so there is nothing to point at. A
            Concept is published in Governance.
          </p>
        ) : (
          <>
            <NativeSelect
              aria-label={`${title} concept`}
              data-testid={`operand-reference-${id}`}
              value={operand.reference}
              disabled={disabled}
              onChange={(event: React.ChangeEvent<HTMLSelectElement>) =>
                onChange({ ...operand, reference: event.target.value })
              }
            >
              <option value="">Select a concept version…</option>
              {/* What this Datastream's project actually names, first. The two
                  registries hold no key to each other, so this is a NAME match
                  and the group is titled for exactly what that proves. */}
              {grouped.inVocabulary.length > 0 && (
                <optgroup label="Also in this project's vocabulary">
                  {grouped.inVocabulary.map((option) => (
                    <option key={option.value} value={option.value}>{option.label}</option>
                  ))}
                </optgroup>
              )}
              {grouped.elsewhere.length > 0 && (
                <optgroup label="Other concepts of this project">
                  {grouped.elsewhere.map((option) => (
                    <option key={option.value} value={option.value}>{option.label}</option>
                  ))}
                </optgroup>
              )}
            </NativeSelect>
            {grouped.unpinnable.length > 0 && (
              <span
                className="text-caption text-text-secondary"
                data-testid={`operand-unpinnable-${id}`}
              >
                Declared in this project's vocabulary but answered by no published concept, so a
                formula cannot pin them yet: {grouped.unpinnable.join(", ")}.
              </span>
            )}
          </>
        )
      )}

      {operand.op === "literal" && (
        <div className="flex flex-wrap items-center gap-2">
          <Input
            aria-label={`${title} value`}
            data-testid={`operand-value-${id}`}
            placeholder="1000"
            value={operand.literalValue}
            disabled={disabled}
            onChange={(event: React.ChangeEvent<HTMLInputElement>) =>
              onChange({ ...operand, literalValue: event.target.value })
            }
          />
          <NativeSelect
            aria-label={`${title} value type`}
            data-testid={`operand-value-type-${id}`}
            value={operand.literalType}
            disabled={disabled}
            onChange={(event: React.ChangeEvent<HTMLSelectElement>) =>
              onChange({ ...operand, literalType: event.target.value })
            }
          >
            {VALUE_TYPES.map((entry) => (
              <option key={entry} value={entry}>{humanLabel(entry)}</option>
            ))}
          </NativeSelect>
        </div>
      )}
    </div>
  );
}

export default function DeclareConceptPanel({
  fieldId,
  datastreamId,
  catalog,
  onDeclared,
  onCancel,
}: {
  /** The source column this concept is being declared for. */
  fieldId: string;
  datastreamId: string;
  catalog: CanonicalCatalog;
  onDeclared: (field: CanonicalField) => void;
  onCancel: () => void;
}) {
  const [source, setSource] = useState<ValueSource>("collected");
  const [name, setName] = useState(fieldId);
  const [kind, setKind] = useState<"dimension" | "metric">("dimension");
  const [valueType, setValueType] = useState("string");
  const [aggregation, setAggregation] = useState("sum");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // --- the calculated branch, and every key the server reads on it ----------
  //
  // `ratio` and not `source_measure`: a door named "calculated" that opens on
  // "no formula" is the collected door under another label.
  const [formula, setFormula] = useState<FormulaDraft>(() => ({ ...emptyFormula(), op: "ratio" }));
  // No default. Migration 237 made `additivity_class` NOT NULL for a metric and
  // `validate_aggregation` refuses a metric that declares neither half; a
  // pre-selected value would be the guess the field exists to remove.
  const [additivity, setAdditivity] = useState<AdditivityClass | "">("");
  const [nonAdditiveDimensions, setNonAdditiveDimensions] = useState("");
  const [aggregateFn, setAggregateFn] = useState<AggregationFunction | "">("sum");
  const [refusals, setRefusals] = useState<ServerRefusal[]>([]);
  const [published, setPublished] = useState<string | null>(null);

  const calculated = source === "calculated";
  const references = useSemanticConceptReferences(catalog.projectId, calculated);

  /**
   * WHICH FIELD THEY WERE LOOKING AT, carried into the formula without a second
   * gesture. A calculated concept cannot be BOUND to this column — `mdm_target`
   * resolves against the canonical registry — so what the column contributes is
   * its place in the expression: if a published concept answers this column's
   * name, it lands in the first operand slot of every root, and the person
   * picks only the other half.
   */
  useEffect(() => {
    if (!calculated || references.status !== "ready") return;
    const match = references.options.find((option) => option.key === conceptKey(fieldId));
    if (!match) return;
    setFormula((current) => {
      const first = current.operands[0] ?? emptyOperand();
      // Never overwrite a choice already made: the seed is a starting point, and
      // re-applying it under the person's hand is the second gesture it exists
      // to remove.
      if (first.reference || current.numerator.reference) return current;
      const seeded: OperandDraft = { ...emptyOperand(), op: "concept_ref", reference: match.value };
      return {
        ...current,
        operands: [seeded, ...current.operands.slice(1)],
        numerator: seeded,
        aggregateOperand: seeded,
      };
    });
  }, [calculated, references, fieldId]);

  const cleanName = conceptKey(name);

  /** Every refusal this browser can make on its own, in the order the server
   *  makes them. Everything else is left to the one authority that owns the
   *  grammar, and rendered with its exact code and JSON path. */
  const localBlock = useMemo(() => {
    if (!cleanName) {
      return calculated
        ? "A Semantic Concept name is required."
        : "A canonical field name is required.";
    }
    if (!calculated) return null;
    if (!additivity) {
      return "Aggregation behaviour is required: declare whether this metric may be summed.";
    }
    if (additivity === "semi_additive" && !nonAdditiveDimensions.trim()) {
      return "A semi-additive metric names the dimensions it may NOT be summed across.";
    }
    if (aggregateFn === "" && additivity !== "non_additive") {
      // `validate_aggregation`: a null aggregation is legal only when the metric
      // declared itself non-additive. Said here so the refusal arrives before
      // the change set exists, not after it has to be explained.
      return "A metric declares how it aggregates, or declares itself non-additive. Pick a function, or set the behaviour to Non-additive.";
    }
    if (activeOperands(formula).some(operandIsIncomplete)) {
      return "Every operand of the formula needs its value: a concept reference pins a concept and its exact version.";
    }
    return null;
  }, [cleanName, calculated, additivity, nonAdditiveDimensions, aggregateFn, formula]);

  const submit = async () => {
    setError(null);
    setRefusals([]);
    if (localBlock) {
      setError(localBlock);
      return;
    }
    setBusy(true);
    try {
      if (calculated) {
        const created = await createCalculatedConcept(catalog.projectId, {
          name,
          valueType,
          formula,
          additivity: additivity as AdditivityClass,
          nonAdditiveDimensions: nonAdditiveDimensions
            .split(",")
            .map((entry) => entry.trim())
            .filter(Boolean),
          aggregation: aggregateFn,
        });
        setPublished(created);
        return;
      }
      // A measure must never be silently non-summable (migration 032), so the
      // aggregation travels with a metric rather than letting the refusal arrive
      // after the send.
      const minted = await catalog.declare(
        kind === "metric"
          ? { canonical_name: name.trim(), concept_kind: "metric", value_type: valueType, aggregation }
          : { canonical_name: name.trim(), concept_kind: "dimension", value_type: valueType },
        datastreamId,
      );
      onDeclared(minted);
    } catch (reason) {
      if (reason instanceof ConceptRefused) setRefusals(reason.refusals);
      setError(
        reason instanceof Error
          ? reason.message
          : calculated
            ? "The Semantic Concept could not be published."
            : "The canonical field could not be declared.",
      );
    } finally {
      setBusy(false);
    }
  };

  if (published) {
    return (
      <div className="grid gap-3 border-t border-divider-base p-5" data-testid="declare-concept-panel">
        <Status as="block" tone="success" title="The Semantic Concept is published" data-testid="declare-concept-published">
          <span className="font-mono">{published}</span> exists in this project's semantic model and
          computes itself. It binds no column: a calculated value is not collected from one, and a
          binding pointed at it would be refused.
        </Status>
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" data-testid="declare-concept-done" onClick={onCancel}>Done</Button>
        </div>
      </div>
    );
  }

  return (
    <div className="grid gap-3 border-t border-divider-base p-5" data-testid="declare-concept-panel">
      {/* NOT « Declare the concept … means »: this panel mints EITHER object,
          and the glossary refuses the unqualified word wherever the gesture can
          produce either one. The heading asks the question; the selector below
          names which object each answer creates. */}
      <span className="text-label text-text-secondary">
        Declare what <span className="font-mono">{fieldId}</span> means
      </span>
      {/* THE FIRST QUESTION, because it decides every other one below it. */}
      <NativeSelect
        aria-label="Where the value comes from"
        data-testid="declare-concept-source"
        value={source}
        onChange={(event: React.ChangeEvent<HTMLSelectElement>) => {
          const next = event.target.value === "calculated" ? "calculated" : "collected";
          setSource(next);
          setValueType(next === "calculated" ? "decimal" : "string");
          setError(null);
          setRefusals([]);
        }}
      >
        <option value="collected">
          Collected — this column carries the value · declares a canonical field
        </option>
        <option value="calculated">
          Calculated — computed from other Concepts · publishes a Semantic Concept
        </option>
      </NativeSelect>
      <span className="text-caption text-text-secondary">
        {calculated
          ? "A Semantic Concept is published now, computes itself wherever the data already is, and binds no column. It is a metric: only a metric carries a formula."
          : "This canonical field belongs to THIS project. The platform vocabulary stays governed and is not declared here. Declaring writes the canonical field now; binding this column to it is still a prepared mapping change."}
      </span>
      <Input
        aria-label={calculated ? "Semantic Concept name" : "Canonical field name"}
        data-testid="declare-concept-name"
        value={name}
        onChange={(event: React.ChangeEvent<HTMLInputElement>) => setName(event.target.value)}
      />
      {!calculated && (
        <NativeSelect
          aria-label="Canonical field kind"
          data-testid="declare-concept-kind"
          value={kind}
          onChange={(event: React.ChangeEvent<HTMLSelectElement>) =>
            setKind(event.target.value === "metric" ? "metric" : "dimension")
          }
        >
          <option value="dimension">Dimension</option>
          <option value="metric">Metric</option>
        </NativeSelect>
      )}
      <NativeSelect
        aria-label="Value type"
        data-testid="declare-concept-value-type"
        value={valueType}
        onChange={(event: React.ChangeEvent<HTMLSelectElement>) => setValueType(event.target.value)}
      >
        {VALUE_TYPES.map((entry) => (
          <option key={entry} value={entry}>{entry}</option>
        ))}
      </NativeSelect>
      {!calculated && kind === "metric" && (
        <NativeSelect
          aria-label="Aggregation"
          data-testid="declare-concept-aggregation"
          value={aggregation}
          onChange={(event: React.ChangeEvent<HTMLSelectElement>) => setAggregation(event.target.value)}
        >
          {AGGREGATIONS.map((entry) => (
            <option key={entry} value={entry}>{entry}</option>
          ))}
        </NativeSelect>
      )}

      {calculated && (
        <>
          <NativeSelect
            aria-label="Aggregation behaviour"
            data-testid="declare-concept-additivity"
            value={additivity}
            onChange={(event: React.ChangeEvent<HTMLSelectElement>) =>
              setAdditivity(event.target.value as AdditivityClass | "")
            }
          >
            <option value="">Select the behaviour…</option>
            {ADDITIVITY_CLASSES.map((entry) => (
              <option key={entry} value={entry}>{ADDITIVITY_LABEL[entry]}</option>
            ))}
          </NativeSelect>
          <span className="text-caption text-text-secondary">
            {additivity
              ? ADDITIVITY_HINT[additivity]
              : "Required, and never defaulted: the render reads this to decide whether two days may be added."}
          </span>
          {additivity === "semi_additive" && (
            <Input
              aria-label="Dimensions it must not be summed across"
              data-testid="declare-concept-non-additive"
              placeholder="date, account"
              value={nonAdditiveDimensions}
              onChange={(event: React.ChangeEvent<HTMLInputElement>) =>
                setNonAdditiveDimensions(event.target.value)
              }
            />
          )}
          <NativeSelect
            aria-label="Aggregation function"
            data-testid="declare-concept-aggregation-function"
            value={aggregateFn}
            onChange={(event: React.ChangeEvent<HTMLSelectElement>) =>
              setAggregateFn(event.target.value as AggregationFunction | "")
            }
          >
            {AGGREGATION_FUNCTIONS.map((entry) => (
              <option key={entry} value={entry}>{humanLabel(entry)}</option>
            ))}
            <option value="">None (non-additive only)</option>
          </NativeSelect>
          <NativeSelect
            aria-label="Formula"
            data-testid="declare-concept-formula"
            value={formula.op}
            onChange={(event: React.ChangeEvent<HTMLSelectElement>) =>
              setFormula({ ...formula, op: event.target.value as FormulaOperation })
            }
          >
            {CALCULATED_OPERATIONS.map((op) => (
              <option key={op} value={op}>{FORMULA_OPERATION_LABEL[op]}</option>
            ))}
          </NativeSelect>
          <span className="text-caption text-text-secondary">
            A formula is a typed tree over concepts, never SQL. The operations are the allowlist the
            server enforces, and it is what validates this — nothing is judged here that it judges.
          </span>

          {(formula.op === "add" || formula.op === "subtract" || formula.op === "multiply") &&
            formula.operands.map((operand, index) => (
              <OperandFields
                key={`operand-${index}`}
                id={`operand-${index}`}
                title={`Operand ${index + 1}`}
                operand={operand}
                references={references}
                fields={catalog.fields}
                disabled={busy}
                onChange={(next) =>
                  setFormula({
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
              <OperandFields
                id="numerator"
                title="Numerator"
                operand={formula.numerator}
                references={references}
                fields={catalog.fields}
                disabled={busy}
                onChange={(next) => setFormula({ ...formula, numerator: next })}
              />
              <OperandFields
                id="denominator"
                title="Denominator"
                operand={formula.denominator}
                references={references}
                fields={catalog.fields}
                disabled={busy}
                onChange={(next) => setFormula({ ...formula, denominator: next })}
              />
              <NativeSelect
                aria-label="When the denominator is zero"
                data-testid="declare-concept-zero-denominator"
                value={formula.zeroDenominator}
                onChange={(event: React.ChangeEvent<HTMLSelectElement>) =>
                  setFormula({
                    ...formula,
                    zeroDenominator: event.target.value as ZeroDenominatorPolicy,
                  })
                }
              >
                {ZERO_DENOMINATOR_POLICIES.map((policy) => (
                  <option key={policy} value={policy}>{ZERO_DENOMINATOR_LABEL[policy]}</option>
                ))}
              </NativeSelect>
              <div className="flex flex-wrap items-center gap-2">
                <Checkbox
                  id="declare-concept-as-percent"
                  data-testid="declare-concept-as-percent"
                  checked={formula.asPercent}
                  onCheckedChange={(checked: boolean | "indeterminate") =>
                    setFormula({ ...formula, asPercent: checked === true })
                  }
                />
                <label htmlFor="declare-concept-as-percent" className="text-caption text-text-secondary">
                  Express the result as a percentage
                </label>
              </div>
            </>
          )}

          {formula.op === "aggregate" && (
            <>
              <NativeSelect
                aria-label="Function"
                data-testid="declare-concept-formula-function"
                value={formula.aggregateFunction}
                onChange={(event: React.ChangeEvent<HTMLSelectElement>) =>
                  setFormula({
                    ...formula,
                    aggregateFunction: event.target.value as AggregationFunction,
                  })
                }
              >
                {AGGREGATION_FUNCTIONS.map((entry) => (
                  <option key={entry} value={entry}>{humanLabel(entry)}</option>
                ))}
              </NativeSelect>
              <OperandFields
                id="aggregate-operand"
                title="Operand"
                operand={formula.aggregateOperand}
                references={references}
                fields={catalog.fields}
                disabled={busy}
                onChange={(next) => setFormula({ ...formula, aggregateOperand: next })}
              />
            </>
          )}
        </>
      )}

      {error && (
        <Status
          as="block"
          tone="error"
          title={calculated ? "The Semantic Concept was not published" : "The canonical field was not declared"}
          data-testid="declare-concept-error"
        >
          {error}
        </Status>
      )}
      {/* THE SERVER'S OWN SENTENCE, WHOLE. The `code` is what a support
          conversation quotes and the `path` says WHICH field to change; a
          paraphrase loses both. */}
      {refusals.length > 0 && (
        <Status as="block" tone="error" title="The formula could not be validated" data-testid="declare-concept-refusals">
          <ul className="m-0 list-none space-y-1 p-0">
            {refusals.map((refusal, index) => (
              <li key={`${refusal.code ?? "refusal"}-${index}`} className="text-caption">
                <strong>{refusal.code ?? "refused"}</strong>
                {refusal.path ? ` at ${refusal.path}` : ""}
                {refusal.message ? ` — ${refusal.message}` : ""}
              </li>
            ))}
          </ul>
        </Status>
      )}
      {/* The refusal arrives BEFORE the click wherever it can: the button says
          what is still missing instead of sending a change set that has to be
          explained away. */}
      {localBlock && !error && (
        <span className="text-caption text-text-secondary" data-testid="declare-concept-blocked">
          {localBlock}
        </span>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          data-testid="declare-concept-submit"
          disabled={busy || localBlock !== null}
          onClick={() => void submit()}
        >
          {busy
            ? calculated ? "Publishing…" : "Declaring…"
            : calculated ? "Publish this Semantic Concept" : "Declare this canonical field"}
        </Button>
        <Button variant="secondary" size="sm" data-testid="declare-concept-cancel" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </div>
  );
}
