/** Canonical project navigation and owner-route registry (Story 46.1). */

// AD-42 : chaque espace declare ses sections chez lui. Ce fichier garde le
// vocabulaire commun, l ASSEMBLAGE -- dont l ordre est contractuel -- et les
// recherches qui lisent le registre.
export * from "./navigation/vocabulary";
import { overview } from "./navigation/overview";
import { analyze } from "./navigation/analyze";
import { test } from "./navigation/test";
import { data } from "./navigation/data";
import { governance } from "./navigation/governance";
import { contextHub } from "./navigation/contextHub";
import type {
  LensContract,
  QueryContract,
  Workspace,
  WorkspaceKey,
} from "./navigation/vocabulary";

/** Ordered and exact: consumers must render this registry, never a copied list. */
export const WORKSPACES = [
  overview,
  analyze,
  test,
  data,
  governance,
  contextHub,
] as const satisfies readonly Workspace[];

export const WORKSPACE_BY_KEY = Object.fromEntries(
  WORKSPACES.map((workspace) => [workspace.key, workspace]),
) as Record<WorkspaceKey, (typeof WORKSPACES)[number]>;

export const DEFAULT_WORKSPACE: WorkspaceKey = "overview";
export const DEFAULT_SECTION = "project-overview";

export function findSection(workspace: string, sectionSlug: string) {
  const owner = WORKSPACES.find((candidate) => candidate.key === workspace);
  return owner?.subnav.find((candidate) => candidate.slug === sectionSlug) ?? null;
}

export function findObjectContract(workspace: string, sectionSlug: string, objectType: string) {
  return findSection(workspace, sectionSlug)?.objects.find((object) => object.type === objectType) ?? null;
}

/** True when the section itself declares this contextual action. */
export function sectionOwnsAction(workspace: string, sectionSlug: string, actionSlug: string) {
  return findSection(workspace, sectionSlug)?.actions?.includes(actionSlug) ?? false;
}

/** The lenses a collection declares, in declaration order. Empty means the
 *  collection has no lens grammar at all — not that every lens is allowed. */
export function sectionLenses(workspace: string, sectionSlug: string): readonly LensContract[] {
  return findSection(workspace, sectionSlug)?.lenses ?? [];
}

/** The declared default lens, or null when the collection declares none. */
export function defaultLens(workspace: string, sectionSlug: string): string | null {
  return sectionLenses(workspace, sectionSlug)[0]?.slug ?? null;
}

export function sectionOwnsLens(workspace: string, sectionSlug: string, lensSlug: string) {
  return sectionLenses(workspace, sectionSlug).some((lens) => lens.slug === lensSlug);
}

/** The tab a bare object route canonicalizes to, or null when the contract
 *  declares no default. Never falls back to `tabs[0]`. */
export function defaultObjectTab(
  workspace: string,
  sectionSlug: string,
  objectType: string,
): string | null {
  return findObjectContract(workspace, sectionSlug, objectType)?.defaultTab ?? null;
}

/** The noun a person reads for an object type, or null when no contract in the
 *  registry declares one.
 *
 *  Searched across every workspace on purpose: the crumb and the workbench know
 *  the type before they know which section declared it, and a type is declared
 *  once. Callers keep their own fallback — this returns null rather than
 *  inventing a spelling, so a type nobody labelled stays whatever its reader
 *  already printed. */
export function objectTypeLabel(objectType: string): string | null {
  for (const workspace of WORKSPACES) {
    for (const section of workspace.subnav) {
      const contract = section.objects.find((object) => object.type === objectType);
      if (contract?.label) return contract.label;
    }
  }
  return null;
}

/** True when this object type declares a `versions` tab, which is the only tab
 *  an exact `/version/:versionId` segment may hang from. */
export function objectTypeHasVersions(
  workspace: string,
  sectionSlug: string,
  objectType: string,
): boolean {
  return findObjectContract(workspace, sectionSlug, objectType)?.tabs?.includes("versions") ?? false;
}
/** The query parameters a section declares, or null. A section that declares
 *  none accepts none: an undeclared parameter is dropped rather than carried,
 *  so a shared address never promises state nothing honours. */
export function sectionQueryContract(workspace: string, sectionSlug: string): QueryContract | null {
  return findSection(workspace, sectionSlug)?.query ?? null;
}

export interface QueryValidation {
  /** The canonical, declaration-ordered subset that survived validation. */
  query: Record<string, string>;
  /** Named reasons for everything that did not. A parameter silently dropped is
   *  how a person shares a link that opens something other than what they saw. */
  rejected: Array<{ name: string; code: string; message: string }>;
}

/** Validate and canonicalize one section's query state.
 *
 *  Canonical means: declaration order, no undeclared keys, no empty values, and
 *  no partially satisfied group. A parameter whose companions are missing is
 *  rejected, because half a pin pins nothing.
 */
export function validateSectionQuery(
  workspace: string,
  sectionSlug: string,
  raw: Record<string, string>,
): QueryValidation {
  const contract = sectionQueryContract(workspace, sectionSlug);
  const rejected: QueryValidation["rejected"] = [];
  if (!contract) {
    for (const name of Object.keys(raw)) {
      rejected.push({
        name,
        code: "undeclared_parameter",
        message: `This section declares no query state, so ${name} is not carried.`,
      });
    }
    return { query: {}, rejected };
  }

  const declared = new Set(contract.parameters.map((parameter) => parameter.name));
  for (const name of Object.keys(raw)) {
    if (!declared.has(name)) {
      rejected.push({
        name,
        code: "undeclared_parameter",
        message: `${name} is not a declared parameter of this section.`,
      });
    }
  }

  // A parameter carrying a forbidden value is not merely dropped: it counts as
  // ABSENT for every companion that required it. Treating it as present let
  // `?semantic_view_id=X&semantic_view_version_id=latest` keep the id — half a
  // pin, which pins nothing and is exactly what the pair exists to prevent.
  const usable = new Set(
    contract.parameters
      .filter((parameter) => {
        const value = (raw[parameter.name] ?? "").trim();
        return value !== "" && !parameter.forbiddenValues?.includes(value.toLowerCase());
      })
      .map((parameter) => parameter.name),
  );

  const query: Record<string, string> = {};
  for (const parameter of contract.parameters) {
    const value = (raw[parameter.name] ?? "").trim();
    if (!value) continue;
    if (parameter.forbiddenValues?.includes(value.toLowerCase())) {
      rejected.push({
        name: parameter.name,
        code: "forbidden_value",
        message: `${parameter.name} may not be "${value}". A shared address pins an exact version; one that follows the newest silently changes what it shows.`,
      });
      continue;
    }
    const missing = (parameter.requiredWith ?? []).filter((companion) => !usable.has(companion));
    if (missing.length > 0) {
      rejected.push({
        name: parameter.name,
        code: "incomplete_group",
        message: `${parameter.name} is only meaningful with ${missing.join(", ")}. It is not carried alone.`,
      });
      continue;
    }
    query[parameter.name] = value;
  }
  return { query, rejected };
}
