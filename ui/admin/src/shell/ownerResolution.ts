/**
 * Resolving a server-composed owner reference -- and REFUSING ONE OUT LOUD.
 *
 * WHY THIS FILE EXISTS. `ContentRouter.openOwner` validated an owner reference
 * against the navigation contracts with eight guard clauses, and every one of
 * them ended in a bare `return`. A reference the shell could not resolve was
 * therefore dropped in complete silence: the button rendered, the pointer
 * changed, the click did nothing, and nothing anywhere said why.
 *
 * That is not a hypothesis. The audit of 2026-08-17 measured the instance: the
 * server emitted `action: "first-publication"` on the `datastream` object for
 * the most important attention item of a new Project, the `datastream` contract
 * declares no actions, and so the ONE gesture a new Project is asked to make
 * opened nothing at all. `overview.md:132` forbids exactly that -- "No silent
 * navigation fallback".
 *
 * THE CLASS, NOT THE INSTANCE. Repointing that one reference fixes one click.
 * What is repaired here is the whole family: EVERY owner reference this console
 * cannot resolve now returns a refusal that says, in the reader's vocabulary,
 * what could not be opened and NAMES THE GESTURE that gets them somewhere real
 * -- the nearest destination that does resolve, or the Project Overview.
 *
 * A refusal sentence never hands back a route-model term (`object_type`,
 * `tab`, `action`, a workspace key): those are our vocabulary. The machine-
 * readable `code` carries them for a log; the sentence carries the gesture.
 *
 * PURE ON PURPOSE. No React, no router, no navigation side effect: the shell
 * decides what to do with the verdict. That is what makes the refusal testable
 * without mounting a screen.
 */

import { WORKSPACES, findObjectContract, findSection, sectionOwnsAction, sectionOwnsLens } from "./navigation";

/** Which views of one object contract may carry a `/version/{id}` tail.
 *
 *  THE SAME RULE AS `router.tsx:versionBearingTabs`, and it has to be: that
 *  function is what `buildPath` consults, and a resolver that said yes where
 *  the builder says no produced a throw inside a click handler. It is duplicated
 *  rather than imported because `router.tsx` imports React and this file is pure
 *  by contract (see the header) -- so the duplication is asserted by a test
 *  instead of prevented by an import. */
function versionBearingTabs(contract: { tabs?: readonly string[]; versionTabs?: readonly string[] }): readonly string[] {
  if (contract.versionTabs) return contract.versionTabs;
  return contract.tabs?.includes("versions") ? ["versions"] : [];
}

/** The eleven semantic keys a server-composed owner reference carries. */
export interface OwnerReferenceInput {
  surface: "project" | "global" | string;
  workspace: string | null;
  section: string | null;
  global_surface: string | null;
  global_section: string | null;
  lens?: string | null;
  object_type: string | null;
  object_id: string | null;
  tab: string | null;
  action: string | null;
  version_id: string | null;
  evidence_id?: string | null;
}

/** Where a refusal invites the reader to go instead. `null` target = the
 *  Project Overview, the one destination that always exists in project scope. */
export interface OwnerGesture {
  label: string;
  target: { workspace: string; section: string } | null;
}

export type OwnerResolution =
  | {
      kind: "global";
      globalSurface: "project-settings" | "organization-settings" | "getting-started";
      globalSection: string;
    }
  | {
      kind: "workspace";
      workspace: string;
      section: string;
      lens: string | null;
      objectType: string | null;
      objectId: string | null;
      tab: string | null;
      versionId: string | null;
      action: string | null;
    }
  | {
      kind: "refused";
      /** Machine-readable, for the log. It may name route-model terms. */
      code: OwnerRefusalCode;
      /** What the reader is told. Never a route-model term. */
      reason: string;
      gesture: OwnerGesture;
    };

export type OwnerRefusalCode =
  | "unknown_surface"
  | "unknown_settings_area"
  | "unknown_section"
  | "incomplete_object"
  | "unknown_object_kind"
  | "unknown_object_view"
  | "unknown_object_action"
  | "unknown_section_action"
  | "version_without_view"
  | "version_not_openable"
  | "stray_object_detail";

/**
 * Which global surface an owner reference may open, and to which sections.
 *
 * Restated here rather than hardcoded to one surface: this gate once accepted
 * `project-settings` ALONE and dropped every other global owner in silence, so
 * a link to the Organization -- the ratified owner of the Organization object
 * (`README.md:94`) -- could not be built at all, however correct it was.
 */
const GLOBAL_OWNER_SECTIONS: Record<string, readonly string[]> = {
  "project-settings": ["general", "capabilities", "changes", "ai"],
  "organization-settings": [
    "general",
    "members",
    "credentials",
    "account-exposure",
    "data-access",
    // The organization twin of `project-settings/ai` (story 75-4): the ORG scope
    // of the AI settings cascade, and a reference that named it was refused.
    "ai",
    "actions",
  ],
  // `overview.md:141` requires Overview to DEEP-LINK to Getting Started. The
  // route has existed since the global surfaces landed (`router.tsx`), and this
  // gate did not list it -- so the one reference the ratified target names could
  // not be built at all. One section, `journey`, which is all that surface has.
  "getting-started": ["journey"],
};

/** The Project Overview gesture: the destination that is always there. */
const BACK_TO_OVERVIEW: OwnerGesture = {
  label: "Go to Project Overview",
  target: { workspace: "overview", section: "project-overview" },
};

function workspaceLabel(workspace: string): string | null {
  return WORKSPACES.find((candidate) => candidate.key === workspace)?.label ?? null;
}

/** The nearest destination that DOES resolve, phrased as a gesture.
 *
 *  A reference whose workspace and section are sound but whose object, view or
 *  action is not still knows where the reader wanted to be. Sending them to the
 *  collection is a real answer; sending everyone to the Overview would throw
 *  away the half of the reference that was correct. */
function nearestGesture(workspace: string | null, section: string | null): OwnerGesture {
  if (!workspace || !section) return BACK_TO_OVERVIEW;
  const found = findSection(workspace, section);
  const label = workspaceLabel(workspace);
  if (!found || !label) return BACK_TO_OVERVIEW;
  return { label: `Open ${label} › ${found.label}`, target: { workspace, section } };
}

function refuse(
  code: OwnerRefusalCode,
  reason: string,
  gesture: OwnerGesture,
): OwnerResolution {
  return { kind: "refused", code, reason, gesture };
}

/**
 * Resolve one owner reference into a navigation intent, or into a refusal that
 * names a gesture.
 *
 * The validation order is the shell's original order, unchanged -- what changed
 * is that each branch now RETURNS A SENTENCE instead of returning nothing.
 */
export function resolveOwnerReference(owner: OwnerReferenceInput): OwnerResolution {
  if (owner.surface === "global") {
    const surface = owner.global_surface ?? "";
    const allowed = GLOBAL_OWNER_SECTIONS[surface];
    const section = owner.global_section ?? "";
    if (!allowed || !allowed.includes(section)) {
      return refuse(
        "unknown_settings_area",
        "This link points at a settings page that this console does not have.",
        BACK_TO_OVERVIEW,
      );
    }
    return {
      kind: "global",
      globalSurface: surface as "project-settings" | "organization-settings" | "getting-started",
      globalSection: section,
    };
  }

  if (owner.surface !== "project") {
    return refuse(
      "unknown_surface",
      "This link does not say which part of toorow it belongs to.",
      BACK_TO_OVERVIEW,
    );
  }

  if (!owner.workspace || !owner.section || !findSection(owner.workspace, owner.section)) {
    return refuse(
      "unknown_section",
      "This link points at a screen that is not part of this project.",
      BACK_TO_OVERVIEW,
    );
  }
  const here = nearestGesture(owner.workspace, owner.section);

  if (owner.object_type || owner.object_id) {
    if (!owner.object_type || !owner.object_id) {
      return refuse(
        "incomplete_object",
        "This link names something to open without saying which one.",
        here,
      );
    }
    const contract = findObjectContract(owner.workspace, owner.section, owner.object_type);
    if (!contract) {
      return refuse(
        "unknown_object_kind",
        "This link points at something this screen does not hold.",
        here,
      );
    }
    if (owner.tab && !contract.tabs?.includes(owner.tab)) {
      return refuse(
        "unknown_object_view",
        "This link asks for a view that this item does not have.",
        here,
      );
    }
    if (owner.action && !contract.actions?.includes(owner.action)) {
      // THE MEASURED INSTANCE. `action: "first-publication"` on a `datastream`
      // landed here and vanished. It is repointed at the Renders collection
      // server-side (`project_readiness.py`, `project_overview.py`); this
      // branch is what stops the NEXT invented action from dying quietly.
      return refuse(
        "unknown_object_action",
        "This link asks for something this item cannot do here.",
        here,
      );
    }
    if (owner.version_id && !owner.tab) {
      return refuse(
        "version_without_view",
        "This link pins one exact version without saying where to open it.",
        here,
      );
    }
    // THE OTHER HALF OF THE SAME RULE, and it was missing.
    //
    // `buildPath` refuses to assemble a version tail under a view that cannot
    // carry one ("Version routes require the versions tab") -- and it refuses by
    // THROWING, inside the click handler, after this resolver said the reference
    // was sound. Measured on references the server composes today:
    // `event-configuration` + `usage` + a version (Story 49.6 AC9),
    // `dq-monitor-version` + `overview`, `project-configuration-version` +
    // `overview`. Every one of those links crashed its own click.
    //
    // Dropping the version instead would be worse than refusing: the reader
    // would land on a DIFFERENT version from the one the evidence pinned, with
    // nothing saying so. So the reference is refused, out loud, with a gesture
    // -- the same contract every other unresolvable reference gets here.
    if (owner.version_id && !versionBearingTabs(contract).includes(owner.tab ?? "")) {
      return refuse(
        "version_not_openable",
        "This link pins one exact version, and this item has no history view to open it in.",
        here,
      );
    }
  } else if (owner.tab || owner.version_id) {
    return refuse(
      "stray_object_detail",
      "This link asks for a detail view without naming the item it belongs to.",
      here,
    );
  } else if (owner.action && !sectionOwnsAction(owner.workspace, owner.section, owner.action)) {
    // A bare action is legal only when the COLLECTION declares it -- that is how
    // the empty Project reaches the one Data-owned Wizard entry.
    return refuse(
      "unknown_section_action",
      "This link asks for something this screen does not offer.",
      here,
    );
  }

  // THE LENS IS PART OF THE ADDRESS. A reference naming `controls-quality` alone
  // lands on that section's declared default -- a different collection from the
  // one the caller asked for (story 58.9). An unknown lens is DROPPED rather
  // than refused: the section still resolves, and the declared default is a
  // real answer to the reference, not a fallback wearing a costume.
  const lens =
    owner.lens && !owner.object_type && sectionOwnsLens(owner.workspace, owner.section, owner.lens)
      ? owner.lens
      : null;

  return {
    kind: "workspace",
    workspace: owner.workspace,
    section: owner.section,
    lens,
    objectType: owner.object_type,
    objectId: owner.object_id,
    tab: owner.tab,
    versionId: owner.version_id,
    action: owner.action,
  };
}
