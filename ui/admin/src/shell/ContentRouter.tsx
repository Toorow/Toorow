import { lazy, Suspense } from "react";
import { useRoute, type CanonicalRoute, type AccountSection, type OrganizationSettingsSection, type ProjectAccessSection, type ProjectSettingsSection } from "./router";
import RouteState from "./RouteState";
import type { OwnerReference } from "./pages/ProjectOverview";
// The owner-reference verdict, and the sentence that goes with a refusal.
import { resolveOwnerReference } from "./ownerResolution";
import { notify } from "../ui";
// AD-42 : les deux tables de dispatch ont leur fichier. Un ecran neuf ne
// rouvre plus celui-ci -- il ajoute son `lazy()` et sa branche chez elles.
import { renderObjectSurface } from "./objectSurfaces";
import { renderCollectionSurface } from "./collectionSurfaces";
import type { SurfaceContext } from "./surfaceContext";

// Epic 50: Explore/Result (50.2) and the Reports/Notebooks/Renders objects (50.3)
// take over the four Analyze sections. `../ReportsPanel` and `../NotebooksPanel`
// are SUPERSEDED but NOT retired: they still write the legacy tables, and the
// cutover is Story 50.3 AC12, which needs an edit to `admin_api.py` that this
// wave did not own. Their declarations are removed because an unread `lazy()`
// fails the build; the two files stay on disk and
// `scripts/finished_work_audit.py` reports them as mounted nowhere -- that
// report IS the inventory, and it is more honest than a dead import.
//
// `../RenderGalleryPage` WAS the third, and it is DELETED (2026-08-04). It is
// not the same case as the other two: the only capability it held that its
// replacement did not was the revocation of LEGACY snapshot share links, which
// Story 50.7 had just repaired into it while nothing mounted it. That is now a
// panel of `analyze-artifacts/Renders.tsx`, reachable by route -- so keeping the
// file would not have preserved an inventory of anything, only 419 lines of
// stylesheet nobody could see.
// Epic 51: the four Test Level 3 workbenches. A collection whose objects open
// nothing is the failure this repository has already shipped twice.
const ProjectSettings = lazy(() => import("./pages/ProjectSettings"));
const ProjectAccess = lazy(() => import("./pages/ProjectAccess"));
const OrgSettings = lazy(() => import("./pages/OrgSettings"));
const AccountSettings = lazy(() => import("./pages/AccountSettings"));
const GettingStarted = lazy(() => import("./pages/GettingStarted"));
const PlatformClocks = lazy(() => import("./pages/PlatformClocks"));


const COLLECTION_ROUTES: ReadonlySet<string> = new Set([
  "overview/project-overview",
  "analyze/explore",
  "analyze/reports",
  "analyze/notebooks",
  "analyze/renders",
  "test/golden-questions",
  "test/regression-runs",
  "test/widget-feedback",
  "data/data-overview",
  "data/datastreams",
  "data/events",
  "data/sources",
  "data/imports",
  "data/connectors",
  "governance/master-data",
  "governance/semantic-model",
  "governance/controls-quality",
  "governance/evidence",
  "context-hub/knowledge-graph",
  "context-hub/knowledge-library",
  "context-hub/skills-registry",
]);

export function collectionOwnerKey(route: Pick<CanonicalRoute, "workspace" | "section">): string | null {
  const key = `${route.workspace}/${route.section}`;
  return COLLECTION_ROUTES.has(key) ? key : null;
}

export default function ContentRouter() {
  const { result, route, navigate } = useRoute();
  /**
   * Open the object a server-composed reference names -- or SAY WHY NOT.
   *
   * The eight guard clauses that used to live here all ended in a bare
   * `return`: a reference this shell could not resolve was dropped in silence,
   * the button clicked, and nothing happened. `overview.md:132` forbids exactly
   * that ("No silent navigation fallback"), and the audit of 2026-08-17
   * measured the instance -- the `first-publication` action of a brand new
   * Project opened nothing at all.
   *
   * The validation itself moved WHOLE into `ownerResolution.ts`, unchanged in
   * order and in verdict, so it can be exercised without mounting a screen.
   * What is new is the `refused` branch: every unresolvable reference now
   * reports, in the reader's vocabulary, and NAMES A GESTURE -- the nearest
   * destination that does resolve, or the Project Overview.
   */
  const openOwner = (owner: OwnerReference) => {
    const resolved = resolveOwnerReference(owner);
    if (resolved.kind === "refused") {
      notify("This link could not be opened", {
        tone: "warning",
        description: resolved.reason,
        // ONE follow-up, and it is a real destination. A refusal that only
        // states a situation is the same dead end as the silence it replaced.
        action: {
          label: resolved.gesture.label,
          onClick: () => navigate(
            resolved.gesture.target
              ? {
                globalSurface: null, globalSection: null,
                workspace: resolved.gesture.target.workspace as CanonicalRoute["workspace"],
                section: resolved.gesture.target.section,
                lens: null, objectType: null, objectId: null, tab: null, versionId: null, action: null,
              }
              : {
                globalSurface: null, globalSection: null,
                workspace: "overview", section: "project-overview",
                lens: null, objectType: null, objectId: null, tab: null, versionId: null, action: null,
              },
          ),
        },
      });
      return;
    }
    if (resolved.kind === "global") {
      // Two branches, not one call with a widened cast: `CanonicalRoute` is a
      // discriminated union and the two surfaces do not take the same section
      // type. Casting past that would compile and then navigate to a section the
      // Organization shell cannot resolve.
      if (resolved.globalSurface === "getting-started") {
        // The third global surface an owner reference may name. It has one
        // section and the router already builds its address.
        navigate({
          globalSurface: "getting-started",
          globalSection: "journey",
          objectType: null, objectId: null, tab: null, versionId: null, action: null,
        });
        return;
      }
      if (resolved.globalSurface === "organization-settings") {
        navigate({
          globalSurface: "organization-settings",
          globalSection: resolved.globalSection as OrganizationSettingsSection,
          objectType: null, objectId: null, tab: null, versionId: null, action: null,
        });
        return;
      }
      navigate({
        globalSurface: "project-settings",
        globalSection: resolved.globalSection as ProjectSettingsSection,
        objectType: null, objectId: null, tab: null, versionId: null, action: null,
      });
      return;
    }
    navigate({ globalSurface: null, globalSection: null, workspace: resolved.workspace as CanonicalRoute["workspace"], section: resolved.section, lens: resolved.lens, objectType: resolved.objectType, objectId: resolved.objectId, tab: resolved.tab, versionId: resolved.versionId, action: resolved.action });
  };
  // An unresolved address is NOT a route. `route` falls back to the Account
  // placeholder when the parser refuses a path, so reading it without checking
  // `result` first rendered User Account for every unknown, denied or stale
  // address in all six workspaces — a fallback wearing the costume of a screen.
  if (result.kind !== "resolved" && result.kind !== "bootstrap") {
    const kind = result.kind === "denied" ? "denied" : result.kind === "stale" ? "stale" : "unknown";
    return <RouteState kind={kind} reason={result.reason} onBackToOverview={() => window.location.assign("/")} backLabel="Back to the entry page" />;
  }
  if (route.globalSurface === "account") {
    return <Suspense fallback={<div className="p-8 text-sm text-muted-foreground" role="status">Loading User Account...</div>}><AccountSettings section={route.globalSection as AccountSection} onSectionChange={(globalSection) => navigate({ globalSurface: "account", globalSection })} /></Suspense>;
  }
  // Must stay ABOVE any project-scope guard: a platform clock has no project,
  // by construction (migration 195 declares no org_id and no project_id).
  if (route.globalSurface === "platform") {
    return <Suspense fallback={<div className="p-8 text-sm text-muted-foreground" role="status">Loading Platform clocks...</div>}><PlatformClocks /></Suspense>;
  }
  if (route.globalSurface === "organization-settings") {
    return <Suspense fallback={<div className="p-8 text-sm text-muted-foreground" role="status">Loading Organization Settings...</div>}><OrgSettings orgId={route.organizationId} section={route.globalSection as OrganizationSettingsSection} onSectionChange={(globalSection) => navigate({ globalSurface: "organization-settings", globalSection })} /></Suspense>;
  }
  if (route.globalSurface === "project-access") {
    return <Suspense fallback={<div className="p-8 text-sm text-muted-foreground" role="status">Loading Project Access...</div>}><ProjectAccess projectId={route.projectId} section={route.globalSection as ProjectAccessSection} onSectionChange={(globalSection) => navigate({ globalSurface: "project-access", globalSection })} /></Suspense>;
  }
  if (route.globalSurface === "getting-started") {
    return <Suspense fallback={<div className="p-8 text-sm text-muted-foreground" role="status">Loading Getting Started...</div>}><GettingStarted projectId={route.projectId} onOpenOwner={(owner) => openOwner(owner as OwnerReference)} /></Suspense>;
  }
  if (route.globalSurface === "project-settings") {
    return <Suspense fallback={<div className="p-8 text-sm text-muted-foreground" role="status">Loading Project Settings...</div>}><ProjectSettings projectId={route.projectId} section={route.globalSection as ProjectSettingsSection} onSectionChange={(globalSection) => navigate({ globalSurface: "project-settings", globalSection, objectType: null, objectId: null, tab: null, versionId: null, action: null })} onOpenOwner={(owner) => openOwner(owner as OwnerReference)} /></Suspense>;
  }
  // No project scope means no project Overview to offer; the entry bootstrap is
  // the only safe way back, and every route state must offer one.
  if (route.scope !== "project") return <RouteState kind="unknown" onBackToOverview={() => window.location.assign("/")} backLabel="Back to the entry page" />;
  const projectId = route.projectId;

  const backToOverview = () => navigate({
    workspace: "overview",
    section: "project-overview",
    objectType: null,
    objectId: null,
    tab: null,
    versionId: null,
    action: null,
  });

  const onOpenDatastream = (objectId: string, tab = "overview") => navigate({
    workspace: "data",
    section: "datastreams",
    objectType: "datastream",
    objectId,
    tab,
    versionId: null,
    action: null,
  });

  /** The entry of Lot 2. `renderObject` below already answers
   *  `/data/datastreams/action/create`, and `ui/admin/src/shell/navigation.ts#WORKSPACES` already declares
   *  the action — but nothing navigated a person to it: `DataWorkspace` draws
   *  "+ Add Datastream" only when this prop is supplied (its own rule: not
   *  supplied means not drawn, rather than drawn and inert), and the call site
   *  below passed only `onOpenDatastream`. The wizard was reachable by typing
   *  the address and by no click. */
  const onAddDatastream = () => navigate({
    workspace: "data",
    section: "datastreams",
    objectType: null,
    objectId: null,
    tab: null,
    versionId: null,
    action: "create",
  });

  /** Open a Test object from its collection.
   *
   *  All THREE Test collections accept an open callback -- `RegressionRuns`
   *  takes `onOpenEvaluationRun` and `onOpenTraceObservation`,
   *  `GoldenQuestions` takes `onOpenGoldenQuestion`, `WidgetFeedback` takes
   *  `onOpenFeedback` -- and the call sites below passed NONE of them. Every run
   *  id, trace id, question and feedback row on those three pages was inert
   *  text, and the four workbenches behind them were reachable only by typing
   *  an address.
   *
   *  `tab: null` on purpose: `navigation.ts` DECLARES a `defaultTab` for each of
   *  these contracts and the router canonicalizes to it. Naming one here would
   *  duplicate a decision already taken, and would drift the day it changes.
   *
   *  Same defect as `onAddDatastream` above, found the same day on a different
   *  workspace: a component builds an affordance conditionally, the mount site
   *  forgets the prop, and the component's own suite stays green because it
   *  hands itself the callback. */
  const openTestObject = (section: string, objectType: string) => (objectId: string) => navigate({
    workspace: "test",
    section,
    objectType,
    objectId,
    tab: null,
    versionId: null,
    action: null,
  });

  /** A Result lives under `analyze/explore`, whichever surface points at it.
   *  `renderObject` already defines this for the Report and Notebook
   *  workbenches; the Renders collection needed the same one and could not
   *  reach that scope. `tab: null` lets the router canonicalize to the
   *  contract's declared default. */
  const openAnalyzeResult = (id: string) => navigate({
    workspace: "analyze",
    section: "explore",
    objectType: "result",
    objectId: id,
    tab: null,
    versionId: null,
    action: null,
  });

  const openDataObject = (section: string, objectType: string, objectId: string) => navigate({
    workspace: "data",
    section,
    objectType,
    objectId,
    tab: "overview",
    versionId: null,
    action: null,
  });
  // AD-42 : ce que les deux tables lisaient par FERMETURE, nomme une fois.
  // C'est tout ce que l'extraction a demande -- aucune branche n'a ete touchee.
  const surfaceContext: SurfaceContext = {
    route,
    projectId,
    navigate,
    openOwner,
    backToOverview,
    onOpenDatastream,
    onAddDatastream,
    openAnalyzeResult,
    openTestObject,
    openDataObject,
  };

  const objectSurface = renderObjectSurface(surfaceContext);
  const surface = objectSurface ?? renderCollectionSurface(collectionOwnerKey(route), surfaceContext);

  return (
    <Suspense fallback={<div className="p-8 text-sm text-muted-foreground" role="status">Loading workspace…</div>}>
      {surface}
    </Suspense>
  );
}
