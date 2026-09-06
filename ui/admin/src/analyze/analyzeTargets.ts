/**
 * The exact console targets Analyze produces, built through the ONE router.
 *
 * Every address below goes through `buildPath`, which validates the object/tab/
 * tail combination against the registered contract and throws when it does not
 * hold. That is the point: a lens that assembled `"/org/" + id + "/…"` itself
 * would be a second router, and the two would disagree the first time the
 * address grammar moved — which it does in this very story, where the version
 * tail stopped being `versions`-only and an evidence tail appeared.
 *
 * `openInToorowTarget` is the external deep link (AC12). It carries organization,
 * Project, Business Domain, the exact Semantic View version, the exact Query Spec
 * version when there is one, the Result, the lens and the datum — so an
 * unauthorized recipient gets the same non-disclosing answer as a direct request,
 * and an authorized one lands on the same workbench, not on a collection.
 */
import { WORKSPACES } from "../shell/navigation";
import { buildPath, type CanonicalRoute } from "../shell/router";
import { DEFAULT_RESULT_LENS, type OwnerRef, type ResultLens } from "./workbenchClient";

export interface AnalyzeScope {
  organizationId: string;
  projectId: string;
  /** Narrows the authorized context when present; never invented when absent. */
  businessDomainId?: string | null;
  semanticViewId?: string | null;
  semanticViewVersionId?: string | null;
}

/** The Explore query state a section declares. Half a pin is dropped, not guessed. */
function exploreQuery(scope: AnalyzeScope): Record<string, string> {
  const query: Record<string, string> = {};
  if (scope.semanticViewId && scope.semanticViewVersionId) {
    query.semantic_view_id = scope.semanticViewId;
    query.semantic_view_version_id = scope.semanticViewVersionId;
  }
  if (scope.businessDomainId) query.business_domain_id = scope.businessDomainId;
  return query;
}

function route(scope: AnalyzeScope, rest: Partial<CanonicalRoute>): CanonicalRoute {
  return {
    scope: "project",
    organizationId: scope.organizationId,
    projectId: scope.projectId,
    workspace: "analyze",
    section: "explore",
    lens: null,
    objectType: null,
    objectId: null,
    tab: null,
    versionId: null,
    evidenceId: null,
    action: null,
    query: exploreQuery(scope),
    globalSurface: null,
    globalSection: null,
    ...rest,
  } as CanonicalRoute;
}

export function exploreCollectionTarget(scope: AnalyzeScope): string {
  return buildPath(route(scope, {}));
}

export function querySpecVersionTarget(
  scope: AnalyzeScope,
  querySpecId: string,
  querySpecVersionId: string,
): string {
  return buildPath(
    route(scope, {
      objectType: "query-spec",
      objectId: querySpecId,
      tab: "query",
      versionId: querySpecVersionId,
    }),
  );
}

export function resultTarget(
  scope: AnalyzeScope,
  resultId: string,
  lens: ResultLens = DEFAULT_RESULT_LENS,
  evidenceId?: string | null,
): string {
  return buildPath(
    route(scope, {
      objectType: "result",
      objectId: resultId,
      tab: lens,
      evidenceId: evidenceId ?? null,
    }),
  );
}

/**
 * The Visualization Builder, for one saved presentation identity (AC12).
 *
 * `result_id` is the declared, optional query pin this section owns: it says
 * WHICH immutable answer the preview is bound to, and without it the Builder
 * shows the validated plan and says plainly that no Result is pinned. It is
 * carried here rather than assembled by the caller so that "open the Builder on
 * this Result" and "copy this address" produce the same string.
 */
export function visualizationBuilderTarget(
  scope: AnalyzeScope,
  visualizationId: string,
  options: { tab?: "build" | "versions"; versionId?: string | null; resultId?: string | null } = {},
): string {
  const base = route(scope, {
    objectType: "visualization",
    objectId: visualizationId,
    tab: options.tab ?? "build",
    versionId: options.versionId ?? null,
  });
  return buildPath(
    options.resultId
      ? { ...base, query: { ...base.query, result_id: options.resultId } }
      : base,
  );
}

/**
 * The complete external target. Identical to `resultTarget` by construction —
 * one builder, so an "Open in Toorow" link and an internal navigation can never
 * differ. Kept as its own name because AC12 asks for it by that name and a
 * reader looking for it should find it.
 */
export function openInToorowTarget(
  scope: AnalyzeScope,
  resultId: string,
  lens: ResultLens = DEFAULT_RESULT_LENS,
  evidenceId?: string | null,
): string {
  return resultTarget(scope, resultId, lens, evidenceId);
}

/**
 * Which console section owns this object type, asked of the ONE registry.
 *
 * Some owner references carry only the triple the evidence recorded — an AI Path
 * step stores `owner_workspace`, `owner_object_type`, `owner_object_id` and
 * nothing more (migration 150). The section is a navigation fact, so it is
 * resolved here from `shell/navigation.ts` instead of being duplicated on the
 * server, where a hardcoded `"knowledge-library"` made every step that was not a
 * Context Topic unreachable — procedures and skills live in `skills-registry`.
 *
 * An ambiguous answer returns null rather than the first match: two sections
 * declaring the same object type is a registry question, and guessing one would
 * open the wrong workbench from an evidence link.
 */
export function sectionForObjectType(
  workspace: string,
  objectType: string | null | undefined,
): string | null {
  if (!objectType) return null;
  const owner = WORKSPACES.find((candidate) => candidate.key === workspace);
  if (!owner) return null;
  const matches = owner.subnav.filter((section) =>
    section.objects.some((object) => object.type === objectType),
  );
  return matches.length === 1 ? matches[0].slug : null;
}

/**
 * An owner reference the SERVER resolved, turned into an address.
 *
 * Returns null when the reference cannot be built — an unregistered object type,
 * a tab the contract does not declare, a missing id. A dead link that looks live
 * is worse than a disabled one, and AC10 requires a missing or denied link to be
 * shown honestly rather than to open something adjacent.
 */
export function ownerTarget(
  organizationId: string,
  projectId: string,
  owner: OwnerRef | null | undefined,
): string | null {
  if (!owner || !owner.workspace) return null;
  const section = owner.section ?? sectionForObjectType(owner.workspace, owner.object_type);
  if (!section) return null;
  try {
    return buildPath({
      scope: "project",
      organizationId,
      projectId,
      workspace: owner.workspace as CanonicalRoute["workspace"],
      section,
      lens: null,
      objectType: owner.object_type ?? null,
      objectId: owner.object_id ?? null,
      tab: owner.tab ?? null,
      versionId: owner.version_id ?? null,
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

/**
 * Where a proposed calculated field waits: Governance › Semantic Model,
 * `concepts` lens, whose panel is the promotion review queue (story 75-2).
 *
 * Built by the router like every other address here, and `null` when it cannot
 * be built — the confirmation then NAMES the queue in words instead of offering
 * a link nothing validated.
 */
export function governedFieldQueueTarget(scope: AnalyzeScope): string | null {
  try {
    return buildPath({
      scope: "project",
      organizationId: scope.organizationId,
      projectId: scope.projectId,
      workspace: "governance",
      section: "semantic-model",
      lens: "concepts",
      objectType: null,
      objectId: null,
      tab: null,
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
