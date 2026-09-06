/**
 * The project's canonical vocabulary, read and declared from the Mapping tab.
 *
 * AMENDMENT 5 OF THE 2026-08-11 REVIEW. `Canonical / MDM target` was dead text
 * on this tab and no control anywhere in the console wrote `mdm_target`. The
 * only door that mints a project-scoped concept was buried in
 * `FileSourceSamplePanel.tsx`, behind a file upload whose preview had to come
 * back `flagged` or `unmatched` before the catalog was even fetched.
 *
 * THE SAME TWO ROUTES, NOT A SECOND PAIR. `GET`/`POST
 * /api/projects/{id}/file-source-templates/canonical-fields` are the doors
 * `server/core/canonical_field_registry.py` was written for; this module calls
 * them and adds nothing. A second writer for one registry is how two screens
 * start disagreeing about what a project's vocabulary contains.
 */
import { useCallback, useEffect, useState } from "react";
import { apiFetch, apiPost, ApiError } from "../../../lib/apiFetch";
import { findObjectContract } from "../../../shell/navigation";
import type { OwnerReference } from "../../../shell/pages/ProjectSettings";
import {
  getGovernanceCollection,
  type GovernanceObject,
} from "../../../governance/governanceSurface";
import { buildExpression } from "../../../governance/formulaContract";
import type {
  AdditivityClass,
  AggregationFunction,
  FormulaDraft,
} from "../../../governance/formulaContract";

/** One row of `app.mdm_canonical_fields`, as the catalog route serves it. */
export interface CanonicalField {
  id: string;
  canonical_name: string;
  concept_kind: string;
  unit: string | null;
  aggregation: string | null;
  description: string | null;
}

/** What a person declares. `object_kind` is DERIVED by the server from the
 *  Datastream (story 64.15) and is deliberately absent: sending one would let a
 *  column be hung on another object's definition. */
export interface ConceptDeclaration {
  canonical_name: string;
  concept_kind: "metric" | "dimension";
  value_type: string;
  aggregation?: string;
}

/** The value types `semantic_concept_versions.value_type` accepts. NOT NULL
 *  there, so a field declared without one could never become a Concept. */
export const VALUE_TYPES = [
  "string", "integer", "decimal", "date", "timestamp",
  "boolean", "money", "duration", "ratio", "percent",
] as const;

/** `app.mdm_canonical_fields.aggregation` — the CHECK of migration 032, which is
 *  narrower than the semantic layer's list. Widening it here would compose rows
 *  the database refuses. */
export const AGGREGATIONS = ["sum", "average", "min", "max", "count"] as const;

function catalogPath(projectId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/file-source-templates/canonical-fields`;
}

function readFields(payload: unknown): CanonicalField[] {
  const rows = (payload as { fields?: unknown })?.fields;
  if (!Array.isArray(rows)) return [];
  return rows.flatMap((row) => {
    const entry = row as Record<string, unknown>;
    const id = typeof entry?.id === "string" ? entry.id : "";
    if (!id) return [];
    return [{
      id,
      canonical_name: typeof entry.canonical_name === "string" ? entry.canonical_name : id,
      concept_kind: typeof entry.concept_kind === "string" ? entry.concept_kind : "",
      unit: typeof entry.unit === "string" ? entry.unit : null,
      aggregation: typeof entry.aggregation === "string" ? entry.aggregation : null,
      description: typeof entry.description === "string" ? entry.description : null,
    }];
  });
}

export interface CanonicalCatalog {
  /** The project every door opened from this catalog writes into. Carried here
   *  so the calculated door reaches the governance route without the Mapping
   *  page having to thread a second prop through the row it renders. */
  projectId: string;
  fields: CanonicalField[];
  loading: boolean;
  /** The sentence to show when the vocabulary could not be read. An empty
   *  catalog and an unreadable one are two different states, and a selector that
   *  renders both as "no concept" sends a person to declare a duplicate. */
  error: string | null;
  declare: (declaration: ConceptDeclaration, datastreamId: string) => Promise<CanonicalField>;
}

/**
 * Read the vocabulary once, and mint into the same list.
 *
 * Only fetched when `enabled` — a read-only version offers no selector, so
 * asking the server for a catalog nobody can pick from is a query paid for
 * nothing on a service that scales to zero.
 */
export function useProjectCanonicalFields(
  projectId: string,
  enabled: boolean,
): CanonicalCatalog {
  const [fields, setFields] = useState<CanonicalField[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    (async () => {
      try {
        const response = await apiFetch(catalogPath(projectId));
        if (!response.ok) {
          const detail = await response.json().catch(() => null);
          throw new Error(
            (detail as { message?: string })?.message
            ?? "The project vocabulary could not be read.",
          );
        }
        const payload = await response.json();
        if (!cancelled) setFields(readFields(payload));
      } catch (reason) {
        if (!cancelled) {
          setError(
            reason instanceof Error ? reason.message : "The project vocabulary could not be read.",
          );
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [projectId, enabled]);

  const declare = useCallback(
    async (declaration: ConceptDeclaration, datastreamId: string) => {
      const response = await apiFetch(catalogPath(projectId), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ datastream_id: datastreamId, fields: [declaration] }),
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(
          (payload as { message?: string })?.message
          ?? "The concept could not be declared.",
        );
      }
      const minted = readFields(payload)[0];
      if (!minted) throw new Error("The server declared no field id.");
      setFields((current) => (
        current.some((entry) => entry.id === minted.id) ? current : [...current, minted]
      ));
      return minted;
    },
    [projectId],
  );

  return { projectId, fields, loading, error, declare };
}

/**
 * A CALCULATED CONCEPT, THROUGH THE ONE CONTRACT THAT CARRIES A FORMULA.
 *
 * `app.mdm_canonical_fields` has no expression column and the route above knows
 * nothing about one (`canonical_field_registry.validate_declaration` accepts
 * `canonical_name`, `concept_kind`, `value_type`, `aggregation`, `non_additive`,
 * `unit`, `object_kind` — and refuses everything else). So a value that is
 * COMPUTED cannot be minted by the door above; it is a Semantic Concept
 * carrying an `expression`, and the change-set contract below is the same one
 * `governance/NewConceptDialog.tsx` posts.
 *
 * NOTHING OF THE GRAMMAR IS RESTATED HERE. `buildExpression` is imported from
 * `governance/formulaContract.ts`, which is itself the mirror of
 * `semantic_expressions.ALLOWED_OPERATIONS` and is pinned to it by
 * `server/tests/core/test_semantic_dialog_paths.py`. A second palette in this
 * folder is exactly the drift that comment exists to prevent, and
 * `derived_columns.py` is deprecated precisely so no second engine is opened.
 */

/** A refusal as the server names it — the `code` a support conversation quotes
 *  and the `path` that says WHICH field to change. Never paraphrased. */
export interface ServerRefusal {
  code?: string;
  message?: string;
  path?: string;
}

/** Thrown when the server named the refusals. The message is still a sentence,
 *  so a caller that only renders `error` says something true. */
export class ConceptRefused extends Error {
  refusals: ServerRefusal[];
  constructor(message: string, refusals: ServerRefusal[] = []) {
    super(message);
    this.name = "ConceptRefused";
    this.refusals = refusals;
  }
}

export interface CalculatedConceptDraft {
  name: string;
  valueType: string;
  formula: FormulaDraft;
  additivity: AdditivityClass;
  /** Only read when `additivity` is `semi_additive`; empty otherwise. */
  nonAdditiveDimensions: string[];
  /** "" means "no aggregation", legal only for a `non_additive` metric
   *  (`semantic_expressions.validate_aggregation`). */
  aggregation: AggregationFunction | "";
}

/** The identifier shape `create_concept` matches on — the same normalization
 *  `NewConceptDialog` applies, so one name typed twice mints one concept. */
export function conceptKey(value: string): string {
  return value.trim().toLowerCase().replace(/\s+/g, "_");
}

function refusalSentence(refusals: ServerRefusal[]): string {
  const first = refusals[0];
  if (!first) return "This concept was not published: preparation did not clear it. Nothing was created.";
  return first.message ?? `The server refused this formula: ${first.code ?? "refused"}.`;
}

/**
 * Create it, prepare it, confirm it — and read `validation.publishable`, never
 * the token. `prepare` mints a confirmation token whether it refused or not,
 * which is what once let a refused change set be announced as a published
 * Concept.
 *
 * Returns the canonical name that now exists.
 */
export async function createCalculatedConcept(
  projectId: string,
  draft: CalculatedConceptDraft,
): Promise<string> {
  const name = conceptKey(draft.name);
  const base = `/api/projects/${encodeURIComponent(projectId)}/governance/semantic-model/change-sets`;
  try {
    const changeSet = await apiPost<{ change_set_id: string }>(base, {
      object_type: "semantic-concept",
      object_id: null,
      base_version_id: null,
      // NESTED under `concept`: the server reads `intent.get("concept")`. Flat,
      // `kind` resolves to "" and the change set is refused with `unknown_kind`.
      intent: {
        action: "create_concept",
        concept: {
          kind: "metric",
          name,
          label: draft.name.trim() || name,
          value_type: draft.valueType,
          definition: "",
          business_domain_refs: [],
          expression: buildExpression(draft.formula, name),
          aggregation: draft.aggregation ? { function: draft.aggregation } : null,
          additivity_class: draft.additivity,
          non_additive_dimensions: draft.nonAdditiveDimensions,
        },
      },
      idempotency_key: `scs-concept-${name}-${Date.now()}`,
    });

    const prepared = await apiPost<{
      confirmation_token?: string;
      refusals?: ServerRefusal[];
      validation?: {
        publishable?: boolean;
        refusals?: ServerRefusal[];
        test_gate?: { state?: string; message?: string };
      };
    }>(`${base}/${encodeURIComponent(changeSet.change_set_id)}/prepare`, {});

    const named = prepared?.refusals ?? prepared?.validation?.refusals ?? [];
    if (prepared?.validation?.publishable !== true) {
      const gate = prepared?.validation?.test_gate;
      throw new ConceptRefused(
        named.length > 0
          ? refusalSentence(named)
          : gate?.message
            ? `This concept was not published. ${gate.message}`
            : refusalSentence(named),
        named,
      );
    }
    if (!prepared.confirmation_token) {
      throw new ConceptRefused(
        "This concept was not published: preparation returned no confirmation. Nothing was created.",
      );
    }

    await apiPost(`${base}/${encodeURIComponent(changeSet.change_set_id)}/confirm`, {
      confirmation_token: prepared.confirmation_token,
    });
    return name;
  } catch (reason) {
    if (reason instanceof ConceptRefused) throw reason;
    if (reason instanceof ApiError) {
      const body = reason.body as { refusals?: ServerRefusal[] } | undefined;
      throw new ConceptRefused(
        reason.message || `The concept was refused (${reason.status}).`,
        Array.isArray(body?.refusals) ? body!.refusals! : [],
      );
    }
    throw new ConceptRefused(
      reason instanceof Error ? reason.message : "The concept could not be created.",
    );
  }
}

/** One Concept a `concept_ref` operand may point at, with its EXACT version. */
export interface ConceptReferenceOption {
  /** `"conceptId|versionId"` — both ids, always: a reference to a Concept
   *  without a version follows `latest` and is not a reference. */
  value: string;
  label: string;
  /** The normalized name, so a caller can tell which of these the project's own
   *  vocabulary also carries without re-normalizing in two places. */
  key: string;
}

/** Loading, ready or unreadable — never an empty list standing in for a failed
 *  read, because "this project has published no Concept" and "I could not ask"
 *  send a person to two different places. */
export type ConceptReferences =
  | { status: "loading" }
  | { status: "ready"; options: ConceptReferenceOption[] }
  | { status: "error"; message: string };

function referenceOptions(items: GovernanceObject[]): ConceptReferenceOption[] {
  return items
    .filter((item) => item.active_version_ref?.id)
    .map((item) => ({
      value: `${item.object_ref.id}|${item.active_version_ref!.id}`,
      label: `${item.object_ref.label} · version ${item.active_version_ref!.version ?? "?"}`,
      key: conceptKey(item.object_ref.label),
    }));
}

/**
 * The Concepts a formula written here may pin — read from the Semantic Model
 * itself, which is the only registry `_op_concept_ref` resolves against.
 *
 * A canonical field id (`mdm_…`) is NOT one of them: `resolve(concept_id,
 * version_id)` reads the concept versions of the project, so offering the
 * project vocabulary as operands would compose `unknown_reference` on every
 * send. What this project's vocabulary is good for is ORDERING the list — see
 * `groupReferences` — not standing in for it.
 */
export function useSemanticConceptReferences(
  projectId: string,
  enabled: boolean,
): ConceptReferences {
  const [state, setState] = useState<ConceptReferences>({ status: "loading" });

  useEffect(() => {
    if (!enabled || !projectId) return;
    const controller = new AbortController();
    setState({ status: "loading" });
    void getGovernanceCollection(projectId, "semantic-model", "concepts", {}, controller.signal)
      .then((envelope) => setState({ status: "ready", options: referenceOptions(envelope.items) }))
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        setState({
          status: "error",
          message: reason instanceof Error ? reason.message : "The request failed.",
        });
      });
    return () => controller.abort();
  }, [projectId, enabled]);

  return state;
}

/**
 * Split the pinnable Concepts by whether this project's own vocabulary carries
 * a field of the same name, and name the fields it carries that no Concept
 * answers.
 *
 * WHY THE SPLIT IS WORTH MAKING. A person on this tab is looking at columns; the
 * names they think in are the ones this project declared. Matching is by NAME —
 * the two registries hold no foreign key to each other — so the group is titled
 * for what a name match actually proves and never claims an identity link.
 */
export function groupReferences(
  options: ConceptReferenceOption[],
  fields: CanonicalField[],
): {
  inVocabulary: ConceptReferenceOption[];
  elsewhere: ConceptReferenceOption[];
  /** Declared here, publishable by no formula: a canonical field with no
   *  Concept version to pin. Said, rather than silently missing from the list. */
  unpinnable: string[];
} {
  const declared = new Map(fields.map((field) => [conceptKey(field.canonical_name), field]));
  const answered = new Set(options.map((option) => option.key));
  return {
    inVocabulary: options.filter((option) => declared.has(option.key)),
    elsewhere: options.filter((option) => !declared.has(option.key)),
    unpinnable: [...declared.values()]
      .filter((field) => !answered.has(conceptKey(field.canonical_name)))
      .map((field) => field.canonical_name),
  };
}

/**
 * WHERE A CANONICAL FIELD IS READ, WHEN THE ROUTER ADMITS ONE — amendment 10.
 *
 * « Un champ nomme son propriétaire et y mène. Une portée énoncée en nombre sans
 * adresse est un compte, pas un lien. » So the row builds the address rather than
 * printing the count and stopping.
 *
 * THE GUARD IS UNFORGIVING AND HAS ALREADY SHIPPED ONE DEAD CONTROL THIS WEEK.
 * `ContentRouter.openOwner` returns in silence when an `object_type` arrives
 * without an `object_id`, and refuses any `tab` or `action` the object contract
 * does not declare — that is exactly how `Add a check` did nothing on every
 * Datastream of every Project. So this asks `navigation.ts` whether the type is
 * declared instead of assuming it, returns `null` when it is not, and the caller
 * renders the concept's NAME rather than a control that opens nothing.
 *
 * Two candidate sites are tried, in the order the object could plausibly be
 * declared: `app.mdm_canonical_fields` is Master Data by its table, and the
 * client's own vocabulary is a lens of Semantic Model by story 60.1. Whichever
 * one declares it answers; neither is hardcoded as the truth.
 *
 * `tab: null` on purpose — the router canonicalizes to the contract's declared
 * default, and naming a tab here would duplicate a decision taken there and
 * be refused the day it changes.
 */
const CANONICAL_FIELD_SITES = [
  { workspace: "governance", section: "master-data" },
  { workspace: "governance", section: "semantic-model" },
] as const;

export const CANONICAL_FIELD_OBJECT_TYPE = "canonical-field";

export function canonicalFieldOwner(objectId: string): OwnerReference | null {
  if (!objectId) return null;
  for (const site of CANONICAL_FIELD_SITES) {
    if (!findObjectContract(site.workspace, site.section, CANONICAL_FIELD_OBJECT_TYPE)) continue;
    return {
      surface: "project",
      workspace: site.workspace,
      section: site.section,
      global_surface: null,
      global_section: null,
      object_type: CANONICAL_FIELD_OBJECT_TYPE,
      object_id: objectId,
      tab: null,
      action: null,
      version_id: null,
      evidence_id: null,
    };
  }
  return null;
}

/** Why a governed field is named and not linked. Said in the row, never left as
 *  a control that lands on the unknown-route screen. */
export const NO_CANONICAL_ADDRESS =
  "The console declares no page for a canonical field yet, so this concept is "
  + "named here rather than linked to a screen that would refuse the address.";
