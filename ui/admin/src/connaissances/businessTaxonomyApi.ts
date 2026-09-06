import { apiGet, apiJson, apiPost } from "../lib/apiFetch";
import type { GraphBundle } from "../KnowledgeGraphPage";

export type TaxonomyType = "business_domain" | "business_classification";
/**
 * What a governed business key can be attached to.
 *
 * A runtime array, and the type derived FROM it — not a type with a list
 * retyped beside it. There were three copies of this list: the server's
 * `BUSINESS_TARGET_TYPES`, this union, and a literal inside the Context Hub link
 * dialog. All three had drifted apart, in the same direction each time: the
 * server accepted `datastream` and neither of the other two named it, so a
 * Datastream could be linked by everything except a person. That is how
 * `context-hub.md`'s criterion — "a view, Datastream or semantic object cannot
 * be linked to business context" — stayed open with the capability built.
 *
 * `semantic_view` and `semantic_concept` were in none of the three.
 *
 * There are now two: this one and the server's, held in step by
 * `test_business_link_target_types.py`, which fails when they diverge.
 *
 * `target_field` and `canonical_field` are NOT the same notion twice (AI-298):
 * the first names a row of the governed dictionary by its name, the second a row
 * of the MDM canonical registry by its id — which is the only one of the two
 * that can reach a field a project declared under its own source.
 */
export const BUSINESS_TARGET_TYPES = [
  "topic",
  "procedure",
  "target_field",
  "canonical_field",
  "schema_doc",
  "report_view",
  "datastream",
  "semantic_view",
  "semantic_concept",
] as const;

export type BusinessTargetType = (typeof BUSINESS_TARGET_TYPES)[number];

export interface BusinessDomain {
  id: string;
  slug: string;
  name: string;
  description: string;
  owner: string | null;
  status: "active" | "archived";
  version_number: number;
  /** The AUTHORITY's current published revision, by id — the base a rename is
   *  preconditioned on (`governance.md`, 2026-08-30). `null` where the identity
   *  has no published revision. Never `version_number`: that number unions two
   *  ledgers counting the same object independently. */
  current_version_id: string | null;
}

export interface BusinessClassification {
  id: string;
  domain_id: string;
  parent_id: string | null;
  classification_type: string;
  slug: string;
  name: string;
  description: string;
  owner: string | null;
  status: "active" | "archived";
  version_number: number;
  /** The same base, for the same reason as `BusinessDomain` above. */
  current_version_id: string | null;
}

export interface BusinessTaxonomyEnvelope {
  org_id: string;
  domains: BusinessDomain[];
  classifications: BusinessClassification[];
}

export interface BusinessLink {
  id: string;
  org_id: string;
  project_id: string;
  taxonomy_type: TaxonomyType;
  taxonomy_id: string;
  /** The taxonomy node's own word, resolved by `business_taxonomy.list_links`.
   *
   *  `null` means the node this link hangs off is no longer readable — an
   *  absence a screen states, never one it papers over with `taxonomy_id`. Two
   *  screens used to resolve this name themselves against whatever catalogue
   *  they had loaded, which made the console a second authority on the
   *  vocabulary; the word now travels with the link. */
  taxonomy_name: string | null;
  target_type: BusinessTargetType;
  target_id: string;
  relation_type: string;
  link_origin: "direct";
  created_by: string;
  created_at: string;
}

export interface BusinessPath {
  path_key: string;
  target: { type: BusinessTargetType; id: string };
  link_origin: "direct" | "derived";
  ordered_path: Array<Record<string, unknown>>;
  source_versions: Array<{
    node_type: string;
    id: string;
    version_number: number;
  }>;
}

const scoped = (path: string, projectId: string) =>
  `${path}${path.includes("?") ? "&" : "?"}project_id=${encodeURIComponent(projectId)}`;

export async function loadContextHub(projectId: string): Promise<{
  taxonomy: BusinessTaxonomyEnvelope;
  links: BusinessLink[];
  graph: GraphBundle;
}> {
  const [taxonomy, links, graph] = await Promise.all([
    apiGet<BusinessTaxonomyEnvelope>(scoped("/api/context/business-taxonomy?status=all", projectId)),
    apiGet<{ links: BusinessLink[] }>(scoped("/api/context/business-links", projectId)),
    apiGet<GraphBundle>(scoped("/api/context/graph", projectId)),
  ]);
  return { taxonomy, links: links.links, graph };
}

/** The MDM canonical vocabulary, read for the link dialog's suggestion list.
 *
 *  A `canonical_field` target is an ID (`mdmcf_...`), and the only place those
 *  ids are visible is the Governance registry. Opening the type without this
 *  read would have shipped a field whose only instruction is "paste the ID" —
 *  a capability the server accepts and nobody can exercise.
 *
 *  Same route as the Governance screen (`GET .../mdm/canonical-fields`) on
 *  purpose: a field listed on one surface and absent from the other would make
 *  part of the vocabulary linkable only from one door. */
export const listCanonicalFields = (projectId: string) =>
  apiGet<{ fields: Array<{ id: string; canonical_name: string; scope: string }> }>(
    `/api/projects/${encodeURIComponent(projectId)}/mdm/canonical-fields`,
  );

/* THE FOUR LEGACY WRITERS ARE NOT HERE ANY MORE — measured 2026-08-25 (audit
 * 03): `createBusinessDomain`, `updateBusinessDomain`,
 * `createBusinessClassification` and `updateBusinessClassification` had ZERO
 * callers. Since the cutover of 2026-08-25 the Hub's identity gestures go
 * through the Master Data authority (`governance/masterDataApi`
 * `createBusinessIdentity` / `runNodeCommand`, mounted by `ContextHubLayout`
 * and `NewBusinessDomainDialog`), and the legacy endpoints answer 409
 * `legacy_store_is_read_only`. The docstring that stood here said "still
 * called on purpose… Both callers print that sentence" — it described the
 * morning of the cutover, not the evening. */

/** The active Business Domains of the organization, as creation options.
 *
 *  `projection=analysis-picker` returns id + name only — the projection built
 *  for exactly this: offering a parent without loading the whole taxonomy. A
 *  Classification cannot be created without a parent domain id, so a form that
 *  did not read this could only invite a 422. */
export const listBusinessDomainOptions = (projectId: string) =>
  apiGet<{
    domains: Array<{ id: string; name: string }>;
    truncated?: boolean;
  }>(scoped("/api/context/business-taxonomy?projection=analysis-picker&status=active", projectId));

export const createBusinessLink = (
  projectId: string,
  body: {
    taxonomy_type: TaxonomyType;
    taxonomy_id: string;
    target_type: BusinessTargetType;
    target_id: string;
    relation_type: string;
    reason: string;
  },
) => apiPost<BusinessLink>(scoped("/api/context/business-links", projectId), body);

export const deleteBusinessLink = (projectId: string, id: string, reason: string) =>
  apiJson<void>(scoped(`/api/context/business-links/${encodeURIComponent(id)}`, projectId), {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason }),
  });

export const previewBusinessPath = (
  projectId: string,
  targetType: BusinessTargetType,
  targetId: string,
  taxonomyId?: string,
) =>
  apiPost<BusinessPath>(scoped("/api/context/business-paths/preview", projectId), {
    target_type: targetType,
    target_id: targetId,
    taxonomy_id: taxonomyId,
  });
