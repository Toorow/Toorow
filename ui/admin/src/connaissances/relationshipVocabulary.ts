/**
 * The console's ONE vocabulary for a context relationship endpoint.
 *
 * WHY IT IS A FILE AND NOT A CONSTANT IN A COMPONENT. The Usage facet carried a
 * local map of THREE node types, copied out of
 * `KnowledgeGraphPage.NODE_TYPE_LABELS` because importing it "would pull React
 * Flow and elkjs into the workbench's bundle for three strings". The copy was
 * the defect: `context_relationships.py` knows EIGHT endpoint types, and the
 * three AC5 exists for — `semantic_view`, `metric`, `datastream` — rendered
 * under their raw wire token, or under nothing at all.
 *
 * A leaf module with no React import is the answer to both problems at once:
 * the mindmap and the workbench read the same words, and neither drags the
 * other's dependencies behind it.
 *
 * THE KEYS ARE THE DELIVERED TOKENS, the ones `app.context_relationships`
 * stores and `ENDPOINT_TYPES` (`server/core/context_relationships.py:75`)
 * enumerates. The values are the product nouns of `glossary.md` — `procedure`
 * is the token, *Skill* is the word a person reads, until the rename of
 * `alignment-register.md` item 4 lands with its backfill.
 */
import { objectTypeLabel } from "../shell/navigation";
import { wireWord } from "../ui/glossary";

/** Every endpoint the relationship authority can name. Mirrors `ENDPOINT_TYPES`. */
export type RelationshipEndpointType =
  | "topic"
  | "procedure"
  | "schema_doc"
  | "target_field"
  | "master_data_node"
  | "semantic_view"
  | "metric"
  | "datastream";

export const RELATIONSHIP_ENDPOINT_TYPES: readonly RelationshipEndpointType[] = [
  "topic",
  "procedure",
  "schema_doc",
  "target_field",
  "master_data_node",
  "semantic_view",
  "metric",
  "datastream",
];

/**
 * The endpoint tokens that ARE object types of the navigation registry, and the
 * token each one is spelled with there.
 *
 * WHY A MAP AND NOT A STRING OPERATION. The two vocabularies differ in two ways
 * and only one of them is punctuation. `semantic_view` and `master_data_node`
 * are the registry's word with underscores where it uses hyphens, so the
 * normalisation below covers them; `topic` and `procedure` are SHORTENED —
 * the registry spells them `context-topic` and `context-procedure`, because a
 * route segment says which workspace owns the object and a relationship
 * endpoint does not. `master_data_node` is a third case again: `node` where the
 * registry says `object`. A regex would get one of the three right, so the
 * alias is written down.
 *
 * WHY DERIVE AT ALL. This map used to carry its own SPELLINGS —
 * `Semantic view`, `Master data object`, `Knowledge item` — and the registry
 * carried `Semantic View`, `Master Data Object`, `Knowledge`. Two answers to
 * one question, which is the defect `console-presentation.md` §4 deletes:
 * *"the registry is the only store"*. The words now come from
 * `objectTypeLabel()` and cannot disagree.
 */
const REGISTRY_TYPE_OF_ENDPOINT: Readonly<Record<string, string>> = {
  topic: "context-topic",
  procedure: "context-procedure",
  master_data_node: "master-data-object",
  semantic_view: "semantic-view",
  datastream: "datastream",
  canonical_field: "canonical-field",
  business_domain: "business-domain",
  semantic_concept: "semantic-concept",
};

/** `semantic_view` -> `semantic-view`. The authority stores endpoint tokens in
 *  snake_case and the registry addresses object types in kebab-case; that is
 *  the whole of the difference for every token the alias map above does not
 *  name, and it is written once, here, rather than at each call site. */
export function registryTypeOfEndpoint(type: string): string {
  return REGISTRY_TYPE_OF_ENDPOINT[type] ?? type.replaceAll("_", "-");
}

/**
 * The endpoints the registry does NOT name, with the reason each one is its own
 * word rather than a missing declaration.
 *
 * None of the three is an addressable object type: no route opens one, so no
 * route contract could carry its noun. Declaring them in `shell/navigation/*.ts`
 * to satisfy this file would mint three addresses that answer nothing.
 */
const ENDPOINT_ONLY_LABELS: Readonly<Record<string, string>> = {
  // Generated from the warehouse schema, never authored, never opened.
  schema_doc: "Schema doc",
  // "Dictionary field" and "Canonical field" are two notions and the labels say
  // so — the same distinction `KnowledgeGraphPage` draws (AI-298). The second IS
  // a registry type (`canonical-field`) and is derived above; this one is the
  // governed dictionary, one list for everyone, and has no workbench.
  target_field: "Dictionary field",
  // A measure of a Semantic Concept, not the `metric-definition` object of
  // Governance — those are different things and giving them one word here would
  // be the mistake this file just stopped making.
  metric: "Metric",
};

/** The word a person reads for one endpoint type.
 *
 *  DERIVED, never stored: the registry answers for every endpoint that is also
 *  an object type, and the three that are not carry their reason above. */
export function endpointLabelOf(type: string): string | null {
  return objectTypeLabel(registryTypeOfEndpoint(type)) ?? ENDPOINT_ONLY_LABELS[type] ?? null;
}

export const RELATIONSHIP_ENDPOINT_LABELS: Record<RelationshipEndpointType, string> =
  Object.fromEntries(
    RELATIONSHIP_ENDPOINT_TYPES.map((type) => [type, endpointLabelOf(type) ?? type]),
  ) as Record<RelationshipEndpointType, string>;

export function endpointTypeLabel(type: string): string {
  return endpointLabelOf(type) ?? type;
}

/**
 * The surfaces `owner_link` (`server/core/context_relationships.py:356`) names,
 * in the words of the console's own navigation — so a peer this workbench
 * cannot open still tells the reader WHERE it is held.
 */
const OWNER_SURFACE_LABELS: Record<string, string> = {
  knowledge: "Knowledge library",
  skills: "Skills registry",
  "knowledge-graph": "Knowledge graph",
  "data-dictionary": "Data dictionary",
  "master-data": "Master data",
  "semantic-model": "Semantic model",
  "canonical-fields": "Canonical fields",
  datastreams: "Datastreams",
};

export function ownerSurfaceLabel(surface: string): string {
  return OWNER_SURFACE_LABELS[surface] ?? surface;
}

/**
 * The peers this workbench can OPEN, because a workbench answers for them.
 *
 * Every other endpoint is named and located, never given a control that would
 * do nothing: `ownerResolution.ts` was written for exactly the defect of a
 * button that renders, changes the pointer and opens nothing.
 */
export const OPENABLE_ENDPOINT_TYPES: ReadonlySet<string> = new Set([
  "topic",
  "procedure",
]);

/**
 * A Context Hub graph word that is not an endpoint TYPE: a doc kind, a field
 * kind, an edge type, a relationship kind.
 *
 * `doc_kind` and the mindmap's node kinds share their tokens with the endpoint
 * types above — `topic`, `procedure`, `schema_doc` (`server/core/context_search.py:90`,
 * `_KIND_ORDER`) — so those read the RATIFIED noun through the registry and say
 * *Knowledge* and *Skill*. An edge type (`describes`, `feeds_report`,
 * `explains`, `owns`, `applies-to`, `uses`) is an open set on the authority's
 * side: `server/core/context_relationships.py` validates the endpoints and
 * carries the kind verbatim, *"with the kind verbatim even outside today's
 * vocabulary"* (`context-hub.md`, amendment of 2026-08-28). A closed map here
 * would drop the very words that amendment preserved, so the token is
 * de-tokenised rather than translated — `wireWord`, which changes spelling and
 * never meaning.
 */
export function graphWord(token: string | null | undefined): string {
  if (!token) return "";
  return endpointLabelOf(String(token)) ?? wireWord(token);
}
