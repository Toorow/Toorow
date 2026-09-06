/**
 * LEVEL 3 -- which workbench opens on an object, and nothing else.
 *
 * AD-42, 2026-08-12. Measured before the cut: `ContentRouter.tsx` was a single
 * function of 613 lines, opened by **35 distinct subjects** in 44 edits since
 * June -- 79 of its diff hunks landed in this table alone. Every console screen
 * ever delivered was added inside one scope, which is both the collision and the
 * reason a change to one workbench could reach another.
 *
 * Adding a workbench now touches THIS file and no other: its `lazy()` and its
 * branch. `ContentRouter` never has to be opened again.
 *
 * The branches are moved VERBATIM -- the diff reads "this code moved". What was
 * closure is now `context`, destructured once below.
 */
import { Suspense, lazy, type ReactNode } from "react";

import RouteState from "./RouteState";
import { buildPath } from "./router";

import type { SurfaceContext } from "./surfaceContext";
import type { OwnerReference } from "./pages/ProjectOverview";
import type { Tab as DatastreamTab } from "./pages/datastreamTabs";
import ContextObjectPage from "../ContextObjectPage";
import DataObjectWorkbench from "../data/DataObjectWorkbench";
import { exploreTogetherPins } from "../analyze/explorer/handoffPins";

const DatastreamCreate = lazy(() => import("./pages/DatastreamCreate"));
const ResultWorkbench = lazy(() => import("../analyze/ResultWorkbench"));
const QuerySpecWorkbench = lazy(() => import("../analyze/QuerySpecWorkbench"));
const VisualizationBuilder = lazy(() => import("../analyze/builder/VisualizationBuilder"));
const ReportWorkbench = lazy(() =>
  import("../analyze-artifacts/Reports").then((m) => ({ default: m.ReportWorkbench })));
const NotebookWorkbench = lazy(() =>
  import("../analyze-artifacts/Notebooks").then((m) => ({ default: m.NotebookWorkbench })));
const RenderWorkbench = lazy(() =>
  import("../analyze-artifacts/Renders").then((m) => ({ default: m.RenderWorkbench })));
const DossierWorkbench = lazy(() => import("../analyze-artifacts/Dossiers"));
const ChartTemplateWorkbench = lazy(() => import("../analyze/templates/ChartTemplateWorkbench"));
const GoldenQuestionWorkbench = lazy(() => import("./pages/GoldenQuestionWorkbench"));
const EvaluationRunWorkbench = lazy(() => import("./pages/EvaluationRunWorkbench"));
const TraceObservationWorkbench = lazy(() => import("./pages/TraceObservationWorkbench"));
const FeedbackReviewWorkbench = lazy(() => import("./pages/FeedbackReviewWorkbench"));
const GovernanceObjectWorkbench = lazy(() => import("../governance/GovernanceObjectWorkbench"));
const AiPathPage = lazy(() => import("../connaissances/AiPathPage"));
const DatastreamWorkbenchRoute = lazy(() => import("../datastreams/workbench/DatastreamWorkbenchRoute"));

/** Object types whose workbench actually OPENS a pinned version.
 *
 *  `router.tsx:200` resolves `/version/{id}` for every contract carrying a
 *  version-bearing tab — that is the TARGET (`README.md` invariant 7: every
 *  projection deep-links to the exact owner, object AND version). This set is
 *  what is DELIVERED. The two are not the same yet, and the gap is refused
 *  explicitly rather than rendered, because showing the current version at an
 *  address that pins one is the exact failure a pinned address exists to
 *  prevent.
 *
 *  Governance and Test resolve their own versions in the branches above and are
 *  deliberately absent — listing them here would duplicate a decision already
 *  taken twice.
 *
 *  What is missing from this set is INVENTORY, not dead code. `report`,
 *  `notebook` and `connector` each declare a `versions` tab and each render a
 *  versions LIST whose rows open nothing: measured, neither `Reports.tsx`,
 *  `Notebooks.tsx` nor `DataObjectWorkbench.tsx` accepts a version identifier at
 *  all. Adding a type here without giving its workbench that prop would turn an
 *  honest refusal into a silent lie.
 *
 *  `context-topic`, `context-procedure` and `skill` LEFT that inventory on
 *  2026-09-01 (story 49.6, AC1/AC3/AC4): `ContextObjectPage` resolves the pinned
 *  version against the ledger, opens it, and says so when the ledger does not
 *  hold it — it never shows the current version at an address that names
 *  another.
 *
 *  The previous form of this rule was a blanket `if (route.versionId)` placed
 *  ABOVE the Analyze branch, so it also refused the two types that do read a
 *  version: `QuerySpecWorkbench` takes `querySpecVersionId` and
 *  `VisualizationBuilder` takes `visualizationSpecVersionId`, and neither line
 *  could ever execute. A guard that depends on where it sits refuses by
 *  accident; this one refuses by declaration. */
const VERSION_READING_OBJECT_TYPES: ReadonlySet<string> = new Set([
  "query-spec",
  "visualization",
  "context-topic",
  "context-procedure",
  "skill",
]);

export function renderObjectSurface(context: SurfaceContext): ReactNode | null {
  const { route, projectId, navigate, openOwner, backToOverview } = context;
    // "Add Datastream" belongs to the collection, not to an object that does not
    // exist yet: /data/datastreams/action/create carries no object id.
    if (!route.objectType && !route.objectId && route.action === "create"
        && route.workspace === "data" && route.section === "datastreams") {
      return <DatastreamCreate
        projectId={projectId}
        onCancel={() => navigate({ objectType: null, objectId: null, tab: null, versionId: null, action: null })}
        onSourceSetup={() => navigate({
          workspace: "data", section: "sources", objectType: null, objectId: null, tab: null, versionId: null, action: null,
        })}
        onCreated={(datastreamId) => navigate({
          workspace: "data", section: "datastreams", objectType: "datastream", objectId: datastreamId, tab: "overview", versionId: null, action: null,
        })}
        // Story 57.4 — the same resolver every other owner reference goes through.
        // The wizard used to render a server-composed `/projects/{id}/...` href,
        // which `parsePath` refuses on its first segment: the only gesture the
        // capability rows offer opened nothing at all.
        onOpenOwner={(owner) => openOwner(owner as OwnerReference)}
      />;
    }
    if (!route.objectType || !route.objectId) return null;

    // Governance resolves its own objects, including the exact pinned version an
    // Epic 48 owner reference carries. The blanket "no workbench reads a version"
    // branch that used to sit here intercepted every one of them.
    if (route.workspace === "governance") {
      return (
        <GovernanceObjectWorkbench
          key={`${route.section}:${route.objectType}:${route.objectId}`}
          projectId={projectId}
          section={route.section}
          objectType={route.objectType}
          objectId={route.objectId}
          tab={route.tab ?? "overview"}
          versionId={route.versionId}
        />
      );
    }

    // Test owns four Level 3 workbenches across its three sections. This branch
    // sits ABOVE the `route.versionId` guard for the same reason Governance
    // does: the Golden Question workbench has a Versions tab, so a pinned
    // version is an address it reads rather than one it must refuse.
    if (route.workspace === "test") {
      const key = `${route.objectType}:${route.objectId}`;
      const onNavigateTab = (tab: string) => navigate({ tab, action: null, versionId: null });
      if (route.objectType === "golden-question") {
        return (
          <GoldenQuestionWorkbench
            key={key}
            projectId={projectId}
            goldenQuestionId={route.objectId}
            versionId={route.versionId}
            tab={route.tab ?? "definition"}
            onNavigateTab={(tab) => navigate({ tab, action: null, versionId: route.versionId })}
          />
        );
      }
      if (route.objectType === "evaluation-run") {
        return (
          <EvaluationRunWorkbench
            key={key}
            projectId={projectId}
            runId={route.objectId}
            tab={route.tab ?? "overview"}
            focusId={route.versionId}
            onNavigateTab={onNavigateTab}
            onOpenOwner={(link) => openOwner({
              surface: "project",
              workspace: link.workspace,
              section: link.section,
              global_surface: null,
              global_section: null,
              object_type: link.object_type,
              object_id: link.object_id,
              tab: link.tab,
              action: null,
              version_id: link.version_id,
              evidence_id: null,
            })}
          />
        );
      }
      if (route.objectType === "trace-observation") {
        return <TraceObservationWorkbench key={key} projectId={projectId} aiPathId={route.objectId} tab={route.tab ?? "timeline"} onNavigateTab={onNavigateTab} />;
      }
      if (route.objectType === "feedback-review") {
        return (
          <FeedbackReviewWorkbench
            key={key}
            projectId={projectId}
            feedbackId={route.objectId}
            tab={route.tab ?? "feedback"}
            onNavigateTab={onNavigateTab}
            onOpenOwner={(link) => openOwner({
              surface: "project",
              workspace: link.workspace,
              section: link.section,
              global_surface: null,
              global_section: null,
              object_type: link.object_type,
              object_id: link.object_id,
              tab: link.tab ?? null,
              action: null,
              version_id: link.version_id,
              evidence_id: null,
            })}
            onOpenTraceObservation={(aiPathId) => navigate({
              workspace: "test",
              section: "regression-runs",
              objectType: "trace-observation",
              objectId: aiPathId,
              tab: null,
              versionId: null,
              action: null,
            })}
          />
        );
      }
    }

    if (route.versionId && !VERSION_READING_OBJECT_TYPES.has(route.objectType)) {
      // Not `stale`: the pinned version is not claimed to be retired. This
      // object type declares a versions tab, so the address is legitimate; its
      // workbench simply does not open one yet. Naming the type is the point —
      // "no workbench reads a version" was true when it was written and became
      // false without the sentence changing.
      return (
        <RouteState
          kind="unavailable"
          reason={`The ${route.objectType} workbench does not open a pinned version yet. The version in this address is preserved and no other version is opened in its place.`}
          onBackToOverview={backToOverview}
        />
      );
    }

    if (route.workspace === "context-hub" && route.objectType === "ai-path") {
      // Story 49.6: `navigation.ts` declares `{ type: "ai-path" }` under
      // Knowledge Graph and nothing answered it — `evidence_index.py` says so in
      // its own source. Evidence links here; now the link resolves.
      return (
        <AiPathPage
          key={route.objectId ?? ""}
          projectId={projectId}
          pathId={route.objectId ?? ""}
          /* Story 49.6 lot 4 -- AC7 asks the trace lens for "exact owner
             links". The references are composed server-side; this is the same
             resolver every other workbench hands them to, so an owner this
             console cannot open is REFUSED out loud instead of clicking into
             nothing. */
          onOpenOwner={(owner) => openOwner(owner as OwnerReference)}
        />
      );
    }

    if (
      route.workspace === "context-hub"
      && (route.objectType === "context-topic"
        || route.objectType === "context-procedure"
        || route.objectType === "skill")
    ) {
      // `skill` is the canonical noun (glossary.md) for the object the wire and
      // the DB still call `context_procedure` — ONE object, two spellings, not
      // two workbenches. `navigation.ts` declared it under Skills Registry and
      // nothing answered it, so `/context-hub/skills-registry/object/skill/:id`
      // rendered `unavailable` — and an AI Path step that recorded
      // `owner_object_type: "skill"` (migration 150) had no reachable target.
      // Deleting the declaration would have removed the dead screen by breaking
      // the link instead; serving it removes both.
      const kind = route.objectType === "context-topic" ? "topic" : "procedure";
      const ownerSection = kind === "topic" ? "knowledge-library" : "skills-registry";
      return (
        <ContextObjectPage
          key={`${route.objectType}:${route.objectId}`}
          projectId={projectId}
          kind={kind}
          objectId={route.objectId}
          tab={route.tab}
          /* Story 49.6 — the pinned version this address names. The router
             already resolved `/tab/versions/version/{n}` for these contracts;
             what was missing was a workbench that reads it. */
          versionId={route.versionId}
          onNavigateTab={(tab) => navigate({ tab, action: null, versionId: null })}
          onOpenVersion={(versionId) => navigate({ tab: "versions", versionId, action: null })}
          versionHref={(versionId) => buildPath({
            ...route,
            tab: "versions",
            versionId,
            action: null,
          })}
          onBack={() => navigate({
            workspace: "context-hub",
            section: ownerSection,
            objectType: null,
            objectId: null,
            tab: null,
            versionId: null,
            action: null,
          })}
          // The Usage tab READS this object's links; the Knowledge Graph is
          // where they are drawn and removed (Story 44.5). The tab used to
          // carry its own writer, superseded since 2026-07-31 and still
          // mounted — a screen that asks a person to leave for the action it
          // names is unfinished, and one that keeps a second writer is worse.
          onOpenGraph={() => navigate({
            workspace: "context-hub",
            section: "knowledge-graph",
            objectType: null,
            objectId: null,
            tab: null,
            versionId: null,
            action: null,
          })}
          // Story 49-6 lot 3: the Usage facet names a peer by type and id, and
          // this is where the two the Context Hub owns become an address. Every
          // other endpoint type is located by name in the panel rather than
          // given a control that opens nothing (`ownerResolution.ts`).
          onOpenPeer={(peer) => navigate({
            workspace: "context-hub",
            section: peer.type === "topic" ? "knowledge-library" : "skills-registry",
            objectType: peer.type === "topic" ? "context-topic" : "context-procedure",
            objectId: peer.id,
            tab: "content",
            versionId: null,
            action: null,
          })}
        />
      );
    }

    if (route.workspace === "data") {
      const contracts = {
        sources: { type: "source-account", lens: "sources", tabs: [{ key: "overview", label: "Overview" }, { key: "accounts", label: "Accounts" }, { key: "health", label: "Health" }, { key: "used-by", label: "Used by" }] },
        imports: { type: "import", lens: "imports", tabs: [{ key: "overview", label: "Overview" }, { key: "raw-evidence", label: "Raw evidence" }, { key: "validation", label: "Validation" }, { key: "publication", label: "Publication" }] },
        events: { type: "event-configuration", lens: "events", tabs: [{ key: "overview", label: "Overview" }, { key: "source-mapping", label: "Source mapping" }, { key: "collection", label: "Collection" }, { key: "usage", label: "Usage" }] },
        connectors: { type: "connector", lens: "connectors", tabs: [{ key: "overview", label: "Overview" }, { key: "capabilities", label: "Capabilities" }, { key: "coverage", label: "Coverage" }, { key: "versions", label: "Versions" }] },
      } as const;
      const contract = contracts[route.section as keyof typeof contracts];
      if (contract && route.objectType === contract.type) {
        return (
          <DataObjectWorkbench
            projectId={projectId}
            lens={contract.lens}
            objectId={route.objectId}
            tab={route.tab ?? "overview"}
            tabs={contract.tabs}
            onNavigateTab={(tab) => navigate({ tab, action: null, versionId: null })}
          />
        );
      }
    }
    if (
      route.workspace === "data"
      && route.section === "datastreams"
      && route.objectType === "datastream"
    ) {
      const onNavigateTab = (tab: string) => navigate({ tab, action: null, versionId: null });
      return (
        <DatastreamWorkbenchRoute
          projectId={projectId}
          datastreamId={route.objectId}
          // THE UNION COMES FROM ITS OWN MODULE. This line spelled the six tabs
          // out, which made it a sixth registry of the tab list: story 58.6 adds
          // `cost` and this cast would have refused it while every other registry
          // accepted it.
          tab={(route.tab ?? "overview") as DatastreamTab}
          onNavigateTab={onNavigateTab}
          // Story 57.5 — the tab IS the route, so it has to be copyable and
          // middle-clickable, and only this level can build an address the
          // router resolves. `datastreamTabs` used to compose `/p/{id}/…`,
          // refused by `parsePath` on its first segment.
          tabHref={(tab) => buildPath({ ...route, tab, action: null, versionId: null })}
          onOpenOwner={(owner) => openOwner(owner as OwnerReference)}
          onOpenAnalytics={(match) => navigate({
            workspace: "analyze",
            section: "explore",
            lens: null,
            objectType: null,
            objectId: null,
            tab: null,
            versionId: null,
            evidenceId: null,
            action: null,
            // THE ADDRESS CARRIES THE PINS, OR IT HANDS OVER A DIFFERENT
            // QUESTION. data.md, *Which Datastreams can usefully be crossed*:
            // `Explore together` opens the Explorer with "the exact Datastream
            // and Output versions that were on the screen, not their current
            // heads". The server composes them on the match
            // (`datastream_matches.explore_together`, pinned by
            // `test_the_handoff_pins_the_exact_versions_that_were_read`); this
            // address carried three ids and dropped all six, so the Explorer
            // could only re-select the pair from a catalog it fetched itself.
            //
            // READ OFF THE HANDOFF, NOT OFF THE SIDES. `explore_together` is the
            // block the server declares as the handoff; taking the versions from
            // `match.left` / `match.right` instead would let the two disagree
            // one day without anything saying so.
            query: {
              left_datastream_id: match.left.datastream_id,
              right_datastream_id: match.right.datastream_id,
              common_key_version_id: match.common_key?.version_id ?? "",
              ...exploreTogetherPins(match),
            },
          })}
        />
      );
    }

    if (route.workspace === "analyze" && route.section === "explore") {
      const scope = {
        organizationId: route.organizationId,
        projectId,
        businessDomainId: route.query?.business_domain_id ?? null,
        semanticViewId: route.query?.semantic_view_id ?? null,
        semanticViewVersionId: route.query?.semantic_view_version_id ?? null,
      };
      if (route.objectType === "result") {
        return (
          <Suspense fallback={<div className="p-8 text-sm text-muted-foreground" role="status">Loading the Result...</div>}>
            <ResultWorkbench
              key={route.objectId}
              scope={scope}
              resultId={route.objectId}
              lens={(route.tab ?? "view") as never}
              evidenceId={route.evidenceId ?? null}
              onNavigateLens={(lens) => navigate({ tab: lens, versionId: null, evidenceId: null, action: null })}
              onOpenTarget={(href) => { window.history.pushState({}, "", href); window.dispatchEvent(new PopStateEvent("popstate")); }}
              onCloseEvidence={() => navigate({ evidenceId: null })}
            />
          </Suspense>
        );
      }
      if (route.objectType === "visualization") {
        return (
          <Suspense fallback={<div className="p-8 text-sm text-muted-foreground" role="status">Loading the Visualization Builder...</div>}>
            <VisualizationBuilder
              key={route.objectId}
              scope={{ organizationId: route.organizationId, projectId }}
              visualizationId={route.objectId}
              visualizationSpecVersionId={route.versionId}
              resultId={route.query?.result_id ?? null}
              querySpecVersionId={null}
              tab={route.tab ?? "build"}
              onNavigateTab={(tab) => navigate({ tab, versionId: null, action: null })}
              tabHref={(tab) => buildPath({ ...route, tab, action: null })}
              exploreHref={buildPath({ ...route, objectType: null, objectId: null, tab: null, versionId: null, action: null })}
              resultHref={(id) => buildPath({ ...route, objectType: "result", objectId: id, tab: "view", versionId: null, action: null })}
            />
          </Suspense>
        );
      }
      if (route.objectType === "query-spec") {
        return (
          <Suspense fallback={<div className="p-8 text-sm text-muted-foreground" role="status">Loading the Query Spec...</div>}>
            <QuerySpecWorkbench
              key={route.objectId}
              scope={scope}
              querySpecId={route.objectId}
              querySpecVersionId={route.versionId}
              onOpenTarget={(href) => { window.history.pushState({}, "", href); window.dispatchEvent(new PopStateEvent("popstate")); }}
            />
          </Suspense>
        );
      }
    }

    if (route.workspace === "analyze") {
      const onNavigateArtifactTab = (tab: string) => navigate({ tab, action: null, versionId: null });
      const tabHref = (tab: string) => buildPath({ ...route, tab, action: null, versionId: null });
      const openResult = (id: string) => navigate({ section: "explore", objectType: "result", objectId: id, tab: null, versionId: null, action: null });
      // THE WAY BACK OUT OF AN ADDRESS THAT NAMES NOTHING (76-4). A workbench
      // whose object does not exist draws no rail and no tabs, so the collection
      // it belongs to is the only reachable place, and only the router knows its
      // address. Same shape as `exploreHref` above: drop the object, keep the
      // section.
      const collectionHref = buildPath({ ...route, objectType: null, objectId: null, tab: null, versionId: null, action: null });
      // Story 72.5: the Chart Template workbench. It sits under `reports`
      // because the object is a LENS of that section — Analyze has exactly four
      // Level 2 screens — and it opens two peers by address: the Visualization a
      // materialisation produced, and the Report that pinned one of its versions.
      if (route.section === "reports" && route.objectType === "chart-template") {
        return (
          <Suspense fallback={<div className="p-8 text-sm text-muted-foreground" role="status">Loading the Chart Template...</div>}>
            <ChartTemplateWorkbench
              key={route.objectId}
              projectId={projectId}
              templateId={route.objectId}
              tab={route.tab ?? "overview"}
              onNavigateTab={onNavigateArtifactTab}
              tabHref={tabHref}
              collectionHref={collectionHref}
              onOpenReport={(id) => navigate({ section: "reports", objectType: "report", objectId: id, tab: "overview", versionId: null, action: null })}
              onOpenVisualization={(id) => navigate({ section: "explore", objectType: "visualization", objectId: id, tab: "build", versionId: null, action: null })}
            />
          </Suspense>
        );
      }
      if (route.section === "reports" && route.objectType === "report") {
        return (
          <ReportWorkbench projectId={projectId} reportId={route.objectId} tab={route.tab ?? "overview"} onNavigateTab={onNavigateArtifactTab} tabHref={tabHref} collectionHref={collectionHref} onOpenResult={openResult} />
        );
      }
      if (route.section === "notebooks" && route.objectType === "notebook") {
        return (
          <NotebookWorkbench projectId={projectId} notebookId={route.objectId} tab={route.tab ?? "content"} onNavigateTab={onNavigateArtifactTab} tabHref={tabHref} collectionHref={collectionHref} onOpenResult={openResult} />
        );
      }
      if (route.section === "renders" && route.objectType === "render") {
        return (
          <RenderWorkbench projectId={projectId} renderId={route.objectId} tab={route.tab ?? "result"} onNavigateTab={onNavigateArtifactTab} tabHref={tabHref} collectionHref={collectionHref} onOpenResult={openResult} />
        );
      }
      if (route.section === "renders" && route.objectType === "dossier") {
        // 74-2: the Dossier read in the console. The reasoning path of a figure
        // is the Result's AI Path lens, so its address is the Result opened on
        // that lens -- an exact reference, never "the Analyze screen".
        return (
          <DossierWorkbench
            projectId={projectId}
            dossierId={route.objectId}
            tab={route.tab ?? "document"}
            onNavigateTab={onNavigateArtifactTab}
            tabHref={tabHref}
            collectionHref={collectionHref}
            onOpenResult={openResult}
            onOpenReasoningPath={(id) => navigate({ section: "explore", objectType: "result", objectId: id, tab: "ai-path", versionId: null, action: null })}
          />
        );
      }
    }

    // The object is registered by the section and nothing says it is retired:
    // its workbench is simply not built. Saying "stale" would invent a fact.
    return <RouteState kind="unavailable" reason="This section owns this object type, but its workbench has not been delivered. No other screen is opened in its place." onBackToOverview={backToOverview} />;
  }
