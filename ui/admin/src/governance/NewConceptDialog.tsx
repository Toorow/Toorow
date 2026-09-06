/**
 * NewConceptDialog — creating a Canonical Semantic Concept, formula included.
 *
 * WHAT THIS DIALOG USED TO DO, MEASURED (story 60.2). It posted
 * `expression: {op: "formula", text}` for a metric. `"formula"` is not an
 * operation of `semantic-expression.v1`, so the server answered
 * `unknown_operation` and refused. With the formula field left empty it posted
 * `expression: null`, which `validate_expression` walks into `malformed_node`
 * and migration 142 refuses outright (a metric version carries an expression).
 * Its aggregation options were `SUM/AVG/COUNT/...`; the server matches
 * lowercase names and has `average`, not `avg`. And it sent neither
 * `additivity_class` nor `non_additive_dimensions`, the two keys
 * `semantic_model.py:961-962` reads. Six values offered, six refused: a metric
 * could not be created from this screen at all, with or without a formula.
 *
 * So the field is now a TREE editor whose palette is `formulaContract.ts`, the
 * mirror of the server's own `ALLOWED_OPERATIONS` — the export whose comment
 * says it exists for exactly this. A text box would need a parser turning
 * `"cost / clicks"` into a typed tree; no such parser exists here, and the
 * shape that has already failed is not the shape to rebuild.
 *
 * Executes: POST /api/projects/{projectId}/governance/semantic-model/change-sets
 *           then /{id}/prepare, then /{id}/confirm.
 *
 * EDIT MODE (2026-08-18). The same dialog, seeded from an existing Concept and
 * sending `edit_concept` instead of `create_concept`. The server has accepted
 * that intent since the change set existed (`semantic_model.py:465`) and no
 * screen ever sent it, so a Concept published with the wrong additivity class
 * was permanent. An edit is not a rewrite: it carries the EXACT object and the
 * EXACT base version (`:541-548`), and `_apply_concept` appends version N+1
 * rather than touching the one being read.
 *
 * What is seeded is what the workbench read model CARRIES, and nothing else. A
 * field the summary does not carry is not guessed here: the canonical name is
 * the one the published version writes, so a guessed one would publish a version
 * under another name — the dialog refuses instead, and says which value is
 * missing.
 *
 * PRECONFIGURED METRICS (2026-08-25). A tree editor is the honest shape for a
 * formula and it is still five decisions deep for a metric the whole industry
 * agrees on. `GET .../governance/semantic-model/metric-presets` answers what
 * this Project could adopt — `roas`, `ctr`, `cpa` and the rest of the delivered
 * catalogue — with operands ALREADY pinned to exact Concept versions, so taking
 * one fills every field below and posts through these same three routes. It is
 * an option, never a default: `""` means "write my own" and that is where the
 * chooser starts. A preset whose operands this Project cannot read is not
 * offered at all; it is listed with the Concept to declare first.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, apiPost } from "../lib/apiFetch";
import BusinessDomainPicker from "./BusinessDomainPicker";
import { gateBlocksPublication, OVERRIDE_MINIMUM_REASON, TestGateOverridePanel, type TestGate } from "./TestGateOverride";
import { getGovernanceCollection, type GovernanceObject } from "./governanceSurface";
import FormulaTreeEditor, { type ReferenceState } from "./FormulaTreeEditor";
import { getMetricPresets, type MetricPreset } from "./metricPresets";
import { activeOperands, ADDITIVITY_CLASSES, ADDITIVITY_HINT, ADDITIVITY_LABEL, AGGREGATION_FUNCTIONS, buildExpression, emptyFormula, emptyOperand, operandIsIncomplete, SEMANTIC_TYPES, type AdditivityClass, type AggregationFunction, type FormulaDraft, type OperandDraft, type ZeroDenominatorPolicy, VALUE_TYPES, ZERO_DENOMINATOR_POLICIES } from "./formulaContract";
import { Button, Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, Input, Label, label as humanLabel, NativeSelect, notify, Status, Textarea } from "../ui";

/**
 * An edit of an object that already exists.
 *
 * The two ids are the server's own requirement, not a convenience: an intent
 * whose action starts with `edit` is refused `missing_exact_base` without BOTH
 * of them, "because editing the current version would rebase onto whatever
 * became current between reading the screen and submitting it"
 * (`semantic_model.py:541-548`). `summary` is the object's summary exactly as
 * the Governance read model composed it — the dialog seeds from that and from
 * nothing else.
 */
export interface SemanticEditTarget {
  objectId: string;
  baseVersionId: string;
  label: string;
  summary: Record<string, unknown>;
}

interface NewConceptDialogProps {
  open: boolean;
  projectId: string;
  onClose: () => void;
  /** Called once the change set is confirmed — a creation or an appended
   *  version. The caller re-reads the object from its owner. */
  onCreated: () => void;
  /** Absent for a creation. Present for an edit of this exact object. */
  edit?: SemanticEditTarget | null;
}

/** A refusal as the server names it. Never paraphrased: the `code` is what a
 *  support conversation quotes and the `path` is what tells the person WHICH
 *  field to change. */
interface ServerRefusal {
  code?: string;
  message?: string;
  path?: string;
}

/** The Concepts a `concept_ref` operand may point at, with their EXACT current
 *  version. Read from the Semantic Model itself — nothing is invented, and when
 *  the read fails the picker says so instead of offering an empty list that
 *  reads like "this Project has no Concepts". */

/** The preconfigured metrics this Project could adopt, as the server resolved
 *  them. Same posture as `ReferenceState`: a read that failed says so, because
 *  an empty chooser reads as "there are no preconfigured metrics", which is a
 *  different fact from "the offer could not be read". */
type PresetState =
  | { status: "loading" }
  | { status: "ready"; presets: MetricPreset[] }
  | { status: "error"; message: string };

/** The reverse of `buildExpression`, and it refuses rather than approximates.
 *
 *  `FORMULA_OPERATIONS` is a deliberate SUBSET of the server allowlist, so a
 *  published version may legitimately carry an operation this editor cannot
 *  compose (`conditional`, `comparison`, an unresolved `concept_name`). Seeding
 *  such a formula to the `source_measure` default would publish, one click
 *  later, a version whose formula nobody wrote — silently. `unsupported` names
 *  the operation instead, and the dialog blocks on it. */
export function conceptFormulaSeed(
  expression: unknown,
): { draft: FormulaDraft; names: string[] } | { unsupported: string } {
  const names: string[] = [];
  const draft = emptyFormula();
  if (expression === null || expression === undefined) return { draft, names };
  if (typeof expression !== "object") return { unsupported: "a formula that is not a typed tree" };
  const typed = expression as Record<string, unknown>;
  const op = String(typed.op ?? "");

  const operand = (node: unknown): OperandDraft | null => {
    if (!node || typeof node !== "object") return null;
    const leaf = node as Record<string, unknown>;
    const leafOp = String(leaf.op ?? "");
    if (leafOp === "source_measure") {
      // The ONE place a Concept's canonical name survives into the read model.
      if (typeof leaf.concept === "string" && leaf.concept) names.push(leaf.concept);
      return { ...emptyOperand(), op: "source_measure" };
    }
    if (leafOp === "concept_ref") {
      const conceptId = String(leaf.concept_id ?? "");
      const versionId = String(leaf.version_id ?? "");
      if (!conceptId || !versionId) return null;
      return { ...emptyOperand(), op: "concept_ref", reference: `${conceptId}|${versionId}` };
    }
    if (leafOp === "literal") {
      return {
        ...emptyOperand(),
        op: "literal",
        literalValue: String(leaf.value ?? ""),
        literalType: String(leaf.value_type ?? "decimal"),
      };
    }
    return null;
  };

  if (op === "source_measure") {
    if (typeof typed.concept === "string" && typed.concept) names.push(typed.concept);
    return { draft: { ...draft, op: "source_measure" }, names };
  }
  if (op === "add" || op === "subtract" || op === "multiply") {
    const operands = (Array.isArray(typed.operands) ? typed.operands : []).map(operand);
    if (operands.length === 0 || operands.some((entry) => entry === null)) {
      return { unsupported: `an operand of the published ${op} formula` };
    }
    return { draft: { ...draft, op, operands: operands as OperandDraft[] }, names };
  }
  if (op === "ratio") {
    const numerator = operand(typed.numerator);
    const denominator = operand(typed.denominator);
    const policy = String(typed.zero_denominator ?? "");
    if (!numerator || !denominator) return { unsupported: "an operand of the published ratio" };
    if (!(ZERO_DENOMINATOR_POLICIES as readonly string[]).includes(policy)) {
      return { unsupported: `the zero-denominator policy ${policy || "it does not declare"}` };
    }
    return {
      draft: {
        ...draft,
        op: "ratio",
        numerator,
        denominator,
        zeroDenominator: policy as ZeroDenominatorPolicy,
        asPercent: typed.as_percent === true,
      },
      names,
    };
  }
  if (op === "aggregate") {
    const fn = String(typed.function ?? "");
    const aggregateOperand = operand(typed.operand);
    if (!(AGGREGATION_FUNCTIONS as readonly string[]).includes(fn)) {
      return { unsupported: `the aggregation function ${fn || "it does not name"}` };
    }
    if (!aggregateOperand) return { unsupported: "the operand of the published aggregate" };
    return {
      draft: { ...draft, op: "aggregate", aggregateFunction: fn as AggregationFunction, aggregateOperand },
      names,
    };
  }
  return { unsupported: `the operation ${op || "it does not name"}` };
}

/** The canonical name the published version carries, or `null`.
 *
 *  `_apply_concept` writes `payload["name"]` into every version it appends
 *  (`semantic_model.py:1688`), so an edit MUST send it — and the Governance
 *  summary does not carry it: `_semantic_concept` composes `label` from
 *  `label or name` and stops there. These are the two places the same server
 *  put the machine name where this screen can still read it. When neither
 *  answers, the dialog refuses; typing one would rename an immutable version. */
export function conceptCanonicalName(summary: Record<string, unknown>): string | null {
  // Carried directly if the read model ever names it. Read first so this stops
  // being a reconstruction the day the owner answers the question.
  if (typeof summary.name === "string" && summary.name.trim()) return summary.name.trim();
  const seed = conceptFormulaSeed(summary.expression);
  if ("names" in seed) {
    const distinct = [...new Set(seed.names)];
    if (distinct.length === 1) return distinct[0];
  }
  const rows = (summary.source_bindings as { rows?: Array<Record<string, unknown>> } | undefined)?.rows;
  const named = [
    ...new Set(
      (Array.isArray(rows) ? rows : [])
        .map((row) => row.concept_name)
        .filter((value): value is string => typeof value === "string" && value.trim() !== ""),
    ),
  ];
  return named.length === 1 ? named[0] : null;
}

function referenceOptions(items: GovernanceObject[]): Array<{ value: string; label: string }> {
  return items
    .filter((item) => item.active_version_ref?.id)
    .map((item) => ({
      value: `${item.object_ref.id}|${item.active_version_ref!.id}`,
      label: `${item.object_ref.label} · version ${item.active_version_ref!.version ?? "?"}`,
    }));
}

export default function NewConceptDialog({
  open,
  projectId,
  onClose,
  onCreated,
  edit = null,
}: NewConceptDialogProps) {
  const [kind, setKind] = useState<"metric" | "dimension">("metric");
  const [name, setName] = useState("");
  const [labelStr, setLabelStr] = useState("");
  const [valueType, setValueType] = useState("decimal");
  const [semanticType, setSemanticType] = useState("time");
  const [definition, setDefinition] = useState("");
  const [businessDomainRefs, setBusinessDomainRefs] = useState<string[]>([]);

  // --- what the server reads, and what this dialog never sent ---------------
  const [formula, setFormula] = useState<FormulaDraft>(emptyFormula);
  // No default. `additivity_class` is the field this whole story is about; a
  // pre-selected value would be the guess it exists to remove, and migration 237
  // refuses a metric version that carries none.
  const [additivity, setAdditivity] = useState<AdditivityClass | "">("");
  const [nonAdditiveDimensions, setNonAdditiveDimensions] = useState("");
  // "" means "no aggregation", legal only for a `non_additive` metric
  // (`validate_aggregation:673-683`).
  const [aggregation, setAggregation] = useState<AggregationFunction | "">("sum");

  const [references, setReferences] = useState<ReferenceState>({ status: "loading" });
  // The preconfigured offer, and which one was taken. `""` is "write my own",
  // and it is the default: a preselected preset would answer, for someone, a
  // question they have not been asked yet.
  const [presets, setPresets] = useState<PresetState>({ status: "loading" });
  const [chosenPreset, setChosenPreset] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refusals, setRefusals] = useState<ServerRefusal[]>([]);
  // The gate that blocked the last attempt, the reason written for it, and the
  // change set it belongs to. Re-preparing THAT change set is what carries the
  // override; creating a second one would publish a different object.
  const [blockedGate, setBlockedGate] = useState<TestGate | null>(null);
  const [overrideReason, setOverrideReason] = useState("");
  const [pendingChangeSetId, setPendingChangeSetId] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !projectId) return;
    const controller = new AbortController();
    setReferences({ status: "loading" });
    void getGovernanceCollection(projectId, "semantic-model", "concepts", {}, controller.signal)
      .then((envelope) =>
        setReferences({ status: "ready", options: referenceOptions(envelope.items) }),
      )
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        setReferences({
          status: "error",
          message: reason instanceof Error ? reason.message : "The request failed.",
        });
      });
    return () => controller.abort();
  }, [open, projectId]);

  /** The preconfigured offer. Read only for a creation: an EDIT already has a
   *  published identity, and a preset would propose replacing it with another
   *  Concept's definition under the name being edited. */
  useEffect(() => {
    if (!open || !projectId || edit) return;
    const controller = new AbortController();
    setPresets({ status: "loading" });
    void getMetricPresets(projectId, controller.signal)
      .then((catalogue) => setPresets({ status: "ready", presets: catalogue.presets }))
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        setPresets({
          status: "error",
          message: reason instanceof Error ? reason.message : "The request failed.",
        });
      });
    return () => controller.abort();
  }, [open, projectId, edit]);

  /** Seeded from the object's own summary, once per opening. Every value below
   *  is one the server composed; a key the summary does not carry leaves the
   *  field as it is rather than inventing one. */
  useEffect(() => {
    if (!open || !edit) return;
    const summary = edit.summary;
    const seededKind = summary.concept_kind === "dimension" ? "dimension" : "metric";
    setKind(seededKind);
    setValueType(
      typeof summary.value_type === "string" && summary.value_type
        ? summary.value_type
        : seededKind === "dimension" ? "string" : "decimal",
    );
    if (typeof summary.semantic_type === "string" && summary.semantic_type) {
      setSemanticType(summary.semantic_type);
    }
    setName(conceptCanonicalName(summary) ?? "");
    setLabelStr(edit.label);
    setDefinition(typeof summary.description === "string" ? summary.description : "");
    setBusinessDomainRefs(
      (Array.isArray(summary.business_domain_refs) ? summary.business_domain_refs : []).filter(
        (value): value is string => typeof value === "string",
      ),
    );
    // NEVER DEFAULTED, on an edit least of all: the wrong additivity class is
    // the defect this dialog exists to let someone correct, and a seeded `sum`
    // would republish the guess.
    setAdditivity(
      (ADDITIVITY_CLASSES as readonly string[]).includes(String(summary.additivity_class))
        ? (summary.additivity_class as AdditivityClass)
        : "",
    );
    setNonAdditiveDimensions(
      (Array.isArray(summary.non_additive_dimensions) ? summary.non_additive_dimensions : []).join(", "),
    );
    setAggregation(
      (AGGREGATION_FUNCTIONS as readonly string[]).includes(String(summary.aggregation))
        ? (summary.aggregation as AggregationFunction)
        : "",
    );
    const seed = conceptFormulaSeed(summary.expression);
    if ("draft" in seed) setFormula(seed.draft);
  }, [open, edit]);

  const adoptable = presets.status === "ready"
    ? presets.presets.filter((preset) => preset.state === "adoptable")
    : [];
  const blocked = presets.status === "ready"
    ? presets.presets.filter((preset) => preset.state === "blocked")
    : [];
  const chosen = adoptable.find((preset) => preset.name === chosenPreset) ?? null;

  /** Taking a preset FILLS the form; it does not submit it.
   *
   *  Every value comes from `preset.intent.concept`, which the server resolved
   *  against this Project — operands already pinned to a concept id AND a
   *  version id. Nothing is recomposed here: `conceptFormulaSeed` is the exact
   *  reverse of the `buildExpression` this dialog posts, so what is shown and
   *  what is sent are the same tree. Every field stays editable, because a
   *  preconfigured metric is a starting point and not a contract. */
  const applyPreset = useCallback(
    (presetName: string) => {
      setChosenPreset(presetName);
      setError(null);
      setRefusals([]);
      if (!presetName) return;
      const preset = adoptable.find((entry) => entry.name === presetName);
      const concept = preset?.intent?.concept as Record<string, unknown> | undefined;
      if (!concept) return;
      setKind("metric");
      setName(typeof concept.name === "string" ? concept.name : "");
      setLabelStr(typeof concept.label === "string" ? concept.label : "");
      setValueType(typeof concept.value_type === "string" ? concept.value_type : "decimal");
      setDefinition(typeof concept.definition === "string" ? concept.definition : "");
      setAdditivity(
        (ADDITIVITY_CLASSES as readonly string[]).includes(String(concept.additivity_class))
          ? (concept.additivity_class as AdditivityClass)
          : "",
      );
      const fn = (concept.aggregation as { function?: string } | null)?.function;
      setAggregation(
        (AGGREGATION_FUNCTIONS as readonly string[]).includes(String(fn))
          ? (fn as AggregationFunction)
          : "",
      );
      setNonAdditiveDimensions(
        (Array.isArray(concept.non_additive_dimensions) ? concept.non_additive_dimensions : [])
          .join(", "),
      );
      const seed = conceptFormulaSeed(concept.expression);
      if ("draft" in seed) {
        setFormula(seed.draft);
      } else {
        // The server offered a formula this editor cannot compose. Said, never
        // seeded to the `source_measure` default — that would publish, one click
        // later, a metric that is not the one anybody chose.
        setChosenPreset("");
        setError(
          `${presetName} could not be filled in here: its formula uses ${seed.unsupported}. ` +
            "Nothing was changed.",
        );
      }
    },
    [adoptable],
  );

  const resetForm = useCallback(() => {
    setKind("metric");
    setName("");
    setLabelStr("");
    setValueType("decimal");
    setSemanticType("time");
    setDefinition("");
    setBusinessDomainRefs([]);
    setFormula(emptyFormula());
    setAdditivity("");
    setNonAdditiveDimensions("");
    setAggregation("sum");
    setError(null);
    setRefusals([]);
    setBlockedGate(null);
    setOverrideReason("");
    setPendingChangeSetId(null);
    setChosenPreset("");
  }, []);

  const handleClose = () => {
    resetForm();
    onClose();
  };

  /** On an edit the identity is the published one, verbatim. Re-cleaning it
   *  could turn a published `gross_revenue` into something else. */
  const cleanName = edit
    ? (conceptCanonicalName(edit.summary) ?? "")
    : name.trim().toLowerCase().replace(/\s+/g, "_");

  /** What makes THIS edit impossible to compose honestly, named. Both branches
   *  are gaps in what the read model carries, not faults of the person: an edit
   *  publishes an immutable version, so a value nobody can read is a value
   *  nobody may invent. */
  const editBlock = useMemo(() => {
    if (!edit) return null;
    const seed = conceptFormulaSeed(edit.summary.expression);
    if ("unsupported" in seed) {
      return (
        `This Concept's published formula uses ${seed.unsupported}, which this editor cannot ` +
        "compose. Editing it here would publish a formula nobody wrote. Nothing was sent."
      );
    }
    if (!conceptCanonicalName(edit.summary)) {
      return (
        "This Concept's canonical name is not carried by the Governance read model, and every " +
        "published version writes it. Nothing was sent: an edit that guessed the name would " +
        "publish this Concept under a different one."
      );
    }
    return null;
  }, [edit]);

  /** The refusals the browser can make on its own. Everything else is left to
   *  the server so that one authority names one refusal. */
  const localBlock = useMemo(() => {
    if (editBlock) return editBlock;
    if (!cleanName) return "Concept name is required.";
    if (kind === "dimension") return null;
    if (!additivity) {
      return "Aggregation behaviour is required: declare whether this metric may be summed.";
    }
    if (additivity === "semi_additive" && !nonAdditiveDimensions.trim()) {
      return "A semi-additive metric names the dimensions it may NOT be summed across.";
    }
    if (activeOperands(formula).some(operandIsIncomplete)) {
      return "Every operand of the formula needs its value: a Concept reference pins a Concept and its exact version.";
    }
    return null;
  }, [editBlock, cleanName, kind, additivity, nonAdditiveDimensions, formula]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setRefusals([]);
    if (localBlock) {
      setError(localBlock);
      return;
    }

    setSubmitting(true);
    const idempotencyKey = edit
      ? `scs-concept-edit-${edit.objectId}-${Date.now()}`
      : `scs-concept-${cleanName}-${Date.now()}`;
    const dimensions = nonAdditiveDimensions
      .split(",")
      .map((entry) => entry.trim())
      .filter(Boolean);

    try {
      const changeSet = pendingChangeSetId
        ? { change_set_id: pendingChangeSetId }
        : await apiPost<{ change_set_id: string }>(
        `/api/projects/${encodeURIComponent(projectId)}/governance/semantic-model/change-sets`,
        {
          object_type: "semantic-concept",
          // THE EXACT OBJECT AND THE EXACT BASE, or neither. `create_change_set`
          // refuses `missing_exact_base` for an `edit_*` action without both,
          // and a creation carries neither.
          object_id: edit ? edit.objectId : null,
          base_version_id: edit ? edit.baseVersionId : null,
          // NESTED under `concept` — the server reads `intent.get("concept")`
          // (`server/core/semantic_model.py:900`, `:1453`). Flat, it resolves to
          // an empty payload: `kind` becomes "" and validation refuses with
          // `unknown_kind`, which the swallowed `prepare` below used to hide.
          intent: {
            action: edit ? "edit_concept" : "create_concept",
            concept: {
              kind,
              name: cleanName,
              label: labelStr.trim() || cleanName,
              value_type: valueType,
              definition: definition.trim(),
              business_domain_refs: businessDomainRefs,
              ...(kind === "metric"
                ? {
                    expression: buildExpression(formula, cleanName),
                    aggregation: aggregation ? { function: aggregation } : null,
                    additivity_class: additivity,
                    non_additive_dimensions: dimensions,
                  }
                : { semantic_type: semanticType }),
            },
          },
          idempotency_key: idempotencyKey,
        },
      );
      setPendingChangeSetId(changeSet.change_set_id);

      // Prepare. NOT swallowed: a refusal here names the field that is wrong,
      // and eating it is what let this dialog announce a Concept that was never
      // published.
      const prepared = await apiPost<{
        confirmation_token?: string;
        refusals?: ServerRefusal[];
        validation?: { publishable?: boolean; refusals?: ServerRefusal[]; test_gate?: Record<string, unknown> };
      }>(
        `/api/projects/${encodeURIComponent(projectId)}/governance/semantic-model/change-sets/${changeSet.change_set_id}/prepare`,
        overrideReason.trim().length >= OVERRIDE_MINIMUM_REASON
          ? { test_gate_override: { reason: overrideReason.trim() } }
          : {},
      );

      // `prepare` ALWAYS mints a token, refusals or not — checking the token
      // alone (what this dialog used to do) reads a refused change set as an
      // accepted one. `validation.publishable` is the field that answers.
      const named = prepared?.refusals ?? prepared?.validation?.refusals ?? [];
      if (prepared?.validation?.publishable !== true) {
        setRefusals(named);
        const gate = prepared?.validation?.test_gate as TestGate | undefined;
        // THE GATE IS A QUESTION, not a wall. It only becomes one once it has
        // spoken, which is why the panel does not exist before this point.
        if (gate && gateBlocksPublication(gate, named)) {
          setBlockedGate(gate);
          setError(null);
          return;
        }
        if (named.length === 0) {
          setError(
            gate?.message
              ? `This Concept was not published. ${gate.message}`
              : "This Concept was not published: preparation did not clear it. Nothing was created.",
          );
        }
        return;
      }
      setBlockedGate(null);
      if (!prepared.confirmation_token) {
        setError(
          "This Concept was not published: preparation returned no confirmation. Nothing was created.",
        );
        return;
      }

      await apiPost(
        `/api/projects/${encodeURIComponent(projectId)}/governance/semantic-model/change-sets/${changeSet.change_set_id}/confirm`,
        { confirmation_token: prepared.confirmation_token },
      );

      notify(
        edit
          ? `Semantic Concept edited: a new version of ${cleanName} was published. The version it was edited from stays readable.`
          : `Semantic Concept created: Concept ${cleanName} was successfully registered.`,
      );
      handleClose();
      onCreated();
    } catch (err: unknown) {
      if (err instanceof ApiError) {
        const body = err.body as { refusals?: ServerRefusal[] } | undefined;
        if (Array.isArray(body?.refusals)) setRefusals(body.refusals);
        setError(err.message || `API Error (${err.status})`);
      } else {
        setError(
          err instanceof Error
            ? err.message
            : edit ? "Error editing concept." : "Error creating concept.",
        );
      }
    } finally {
      setSubmitting(false);
    }
  };


  return (
    <Dialog open={open} onOpenChange={(val) => !val && handleClose()}>
      <DialogContent className="sm:max-w-[640px]">
        <form onSubmit={handleSubmit}>
          <DialogHeader>
            <DialogTitle>{edit ? "Edit Semantic Concept" : "New Semantic Concept"}</DialogTitle>
            <DialogDescription>
              {edit
                ? `Editing from version ${edit.baseVersionId}. Publishing appends a new immutable version from that exact base; the version you are reading is never rewritten.`
                : "Define a canonical metric or dimension for reuse across the semantic model and analytical views."}
            </DialogDescription>
          </DialogHeader>

          <div className="grid gap-4 py-4">
            {editBlock && (
              <Status
                as="block"
                tone="error"
                title="This Concept cannot be edited from here"
                data-testid="concept-edit-block"
              >
                {editBlock}
              </Status>
            )}

            {error && (
              <Status as="block" tone="error" title={edit ? "This edit was not published" : "Creation failed"}>
                {error}
              </Status>
            )}

            {refusals.length > 0 && (
              <Status as="block" tone="error" title="The formula could not be validated">
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

            {blockedGate && (
              <TestGateOverridePanel
                gate={blockedGate}
                reason={overrideReason}
                onReasonChange={setOverrideReason}
                objectNoun="Concept"
              />
            )}

            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-1.5">
                <Label htmlFor="concept-kind">Concept Kind</Label>
                <NativeSelect
                  id="concept-kind"
                  value={kind}
                  // The kind and the canonical name are the object's identity:
                  // `app.semantic_concepts` keeps both when a version is
                  // appended, so changing them here would leave the object and
                  // its newest version disagreeing about what it is.
                  disabled={Boolean(edit)}
                  onChange={(e) => {
                    const nextKind = e.target.value as "metric" | "dimension";
                    setKind(nextKind);
                    setValueType(nextKind === "dimension" ? "string" : "decimal");
                  }}
                >
                  <option value="metric">Metric (Measurable value)</option>
                  <option value="dimension">Dimension (Analysis axis)</option>
                </NativeSelect>
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="concept-value-type">Value Type</Label>
                <NativeSelect
                  id="concept-value-type"
                  value={valueType}
                  onChange={(e) => setValueType(e.target.value)}
                >
                  {VALUE_TYPES.map((type) => (
                    <option key={type} value={type}>
                      {humanLabel(type)}
                    </option>
                  ))}
                </NativeSelect>
              </div>
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-1.5">
                <Label htmlFor="concept-name">Canonical Name (ID/Code)</Label>
                <Input
                  id="concept-name"
                  placeholder="e.g. gross_revenue, clicks"
                  value={name}
                  readOnly={Boolean(edit)}
                  onChange={(e) => setName(e.target.value)}
                  required
                />
                {edit && (
                  <p className="mb-0 text-caption text-text-secondary">
                    The identity every published version pins. It is shown as published and is not
                    changed by an edit.
                  </p>
                )}
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="concept-label">Display Label</Label>
                <Input
                  id="concept-label"
                  placeholder="e.g. Gross Revenue"
                  value={labelStr}
                  onChange={(e) => setLabelStr(e.target.value)}
                />
              </div>
            </div>

            {kind === "dimension" && (
              <div className="space-y-1.5">
                {/* A dimension without a semantic type is refused by
                    `undeclared_semantic_type` AND by migration 142. This dialog
                    never sent one, so a dimension could not be created either. */}
                <Label htmlFor="concept-semantic-type">Semantic type</Label>
                <NativeSelect
                  id="concept-semantic-type"
                  value={semanticType}
                  onChange={(e) => setSemanticType(e.target.value)}
                >
                  {SEMANTIC_TYPES.map((type) => (
                    <option key={type} value={type}>
                      {humanLabel(type)}
                    </option>
                  ))}
                </NativeSelect>
                <p className="mb-0 text-caption text-text-secondary">
                  Without it nothing can decide whether this dimension is a time axis.
                </p>
              </div>
            )}

            {/* THE FIRST QUESTION OF A NEW METRIC, and it narrows every one
                below it: taking a preconfigured metric answers the value type,
                the aggregation behaviour and the whole formula at once. Absent
                on an edit — that Concept's identity is already published. */}
            {kind === "metric" && !edit && (
              <div
                className="space-y-1.5 rounded-lg border border-divider-base p-3"
                data-testid="metric-preset-chooser"
              >
                <Label htmlFor="concept-preset">Start from</Label>
                {presets.status === "loading" && (
                  <p className="mb-0 text-caption text-text-secondary">
                    Reading the preconfigured metrics…
                  </p>
                )}
                {presets.status === "error" && (
                  <Status as="block" tone="warning" title="The preconfigured metrics could not be read">
                    {presets.message} You can still write the formula yourself below — nothing
                    here is required to declare a metric.
                  </Status>
                )}
                {presets.status === "ready" && (
                  <>
                    <NativeSelect
                      id="concept-preset"
                      value={chosenPreset}
                      disabled={submitting}
                      onChange={(e) => applyPreset(e.target.value)}
                    >
                      <option value="">Write my own metric</option>
                      {adoptable.map((preset) => (
                        <option key={preset.name} value={preset.name}>
                          {preset.label}
                          {preset.calculated ? ` — ${preset.dependencies.join(" / ")}` : ""}
                        </option>
                      ))}
                    </NativeSelect>
                    {adoptable.length === 0 && (
                      <p className="mb-0 text-caption text-text-secondary">
                        This Project already reads every preconfigured metric of the delivered
                        catalogue. Write your own below.
                      </p>
                    )}
                    {chosen && (
                      <p className="mb-0 text-caption text-text-secondary">
                        {chosen.calculated
                          ? `Adopting ${chosen.label} publishes it as this Project's own Concept, pinned to ${chosen.dependencies.join(" and ")} at their exact versions. Every field below is filled in and stays editable.`
                          : `Adopting ${chosen.label} publishes it as this Project's own Concept, reading its mapped source measure. Every field below is filled in and stays editable.`}
                      </p>
                    )}
                    {blocked.length > 0 && (
                      <Status
                        as="block"
                        tone="info"
                        title="Not offered yet, and what each one needs"
                        data-testid="metric-preset-blocked"
                      >
                        <ul className="m-0 list-none space-y-1 p-0">
                          {blocked.map((preset) => (
                            <li key={preset.name} className="text-caption">
                              <strong>{preset.label}</strong> — {preset.gesture}
                            </li>
                          ))}
                        </ul>
                      </Status>
                    )}
                  </>
                )}
              </div>
            )}

            {kind === "metric" && (
              <>
                <div className="space-y-1.5 rounded-lg border border-divider-base bg-surface-subtle/50 p-3">
                  <Label htmlFor="concept-additivity">Aggregation behaviour</Label>
                  <NativeSelect
                    id="concept-additivity"
                    value={additivity}
                    onChange={(e) => setAdditivity(e.target.value as AdditivityClass | "")}
                  >
                    <option value="">Select the behaviour…</option>
                    {ADDITIVITY_CLASSES.map((klass) => (
                      <option key={klass} value={klass}>
                        {ADDITIVITY_LABEL[klass]}
                      </option>
                    ))}
                  </NativeSelect>
                  <p className="mb-0 text-caption text-text-secondary">
                    {additivity
                      ? ADDITIVITY_HINT[additivity]
                      : "Required, and never defaulted: the render reads this to decide whether two days may be added."}
                  </p>

                  {additivity === "semi_additive" && (
                    <div className="space-y-1.5 pt-2">
                      <Label htmlFor="concept-non-additive-dimensions">
                        Dimensions it must not be summed across
                      </Label>
                      <Input
                        id="concept-non-additive-dimensions"
                        placeholder="e.g. date, account"
                        value={nonAdditiveDimensions}
                        onChange={(e) => setNonAdditiveDimensions(e.target.value)}
                      />
                      <p className="mb-0 text-caption text-text-secondary">
                        Comma separated. Without them, "semi-additive" warns nobody.
                      </p>
                    </div>
                  )}
                </div>

                <div className="space-y-1.5">
                  <Label htmlFor="concept-agg">Aggregation function</Label>
                  <NativeSelect
                    id="concept-agg"
                    value={aggregation}
                    onChange={(e) => setAggregation(e.target.value as AggregationFunction | "")}
                  >
                    {AGGREGATION_FUNCTIONS.map((fn) => (
                      <option key={fn} value={fn}>
                        {humanLabel(fn)}
                      </option>
                    ))}
                    <option value="">None (non-additive only)</option>
                  </NativeSelect>
                </div>

                <FormulaTreeEditor
                  idPrefix="concept"
                  formula={formula}
                  onChange={setFormula}
                  references={references}
                  disabled={submitting}
                />
              </>
            )}

            <div className="space-y-1.5">
              {/* Same contract as the Semantic View dialog: only the ACTIVE
                  Business Domains of this organization are offered, which is
                  exactly what `_validate_business_domain_refs` accepts. */}
              <Label>Business Domains</Label>
              <BusinessDomainPicker
                projectId={projectId}
                value={businessDomainRefs}
                onChange={setBusinessDomainRefs}
                disabled={submitting}
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="concept-def">Business Definition</Label>
              <Textarea
                id="concept-def"
                placeholder="Explain the business logic or meaning of this concept..."
                rows={2}
                value={definition}
                onChange={(e) => setDefinition(e.target.value)}
              />
            </div>
          </div>

          <DialogFooter>
            <Button type="button" variant="secondary" onClick={handleClose} disabled={submitting}>
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={
                submitting ||
                editBlock !== null ||
                (blockedGate !== null &&
                  overrideReason.trim().length < OVERRIDE_MINIMUM_REASON)
              }
            >
              {submitting
                ? edit ? "Publishing..." : "Creating..."
                : blockedGate
                  ? "Publish with this reason"
                  : edit ? "Edit Concept" : "Create Concept"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
