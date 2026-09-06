/**
 * LEVEL 2 -- which collection a workspace section opens.
 *
 * AD-42, 2026-08-12. The sibling of `objectSurfaces`: one table keyed by
 * `workspace/section`, moved out of the 613-line body of `ContentRouter` for the
 * same reason. A new section adds its `lazy()` and its `case` here.
 *
 * `collectionOwnerKey` stays in `ContentRouter` because the route state above
 * this table reads it too -- a key is not a surface.
 */
import { lazy, type ReactNode } from "react";

import RouteState from "./RouteState";
import type { SurfaceContext } from "./surfaceContext";

const ProjectOverview = lazy(() => import("./pages/ProjectOverview"));
const Explore = lazy(() => import("./pages/Explore"));
const ReportsCollection = lazy(() => import("../analyze-artifacts/Reports"));
const NotebooksCollection = lazy(() => import("../analyze-artifacts/Notebooks"));
const RendersCollection = lazy(() => import("../analyze-artifacts/Renders"));
const GoldenQuestions = lazy(() => import("./pages/GoldenQuestions"));
const RegressionRuns = lazy(() => import("./pages/RegressionRuns"));
const WidgetFeedback = lazy(() => import("./pages/WidgetFeedback"));
const DataOverview = lazy(() => import("./pages/DataOverview"));
const DataWorkspace = lazy(() => import("./pages/DataWorkspace"));
const BusinessContextPanel = lazy(() => import("../BusinessContextPanel"));
const Sources = lazy(() => import("./pages/Sources"));
const Imports = lazy(() => import("./pages/Imports"));
const ConnectorsCatalog = lazy(() => import("./pages/ConnectorsCatalog"));
const TopicCatalog = lazy(() => import("../WidgetCardsPage"));
const PacingReport = lazy(() => import("../analyze-artifacts/PacingReport"));
const ChartTemplates = lazy(() => import("../analyze/templates/ChartTemplates"));
const GovernanceCollection = lazy(() => import("../governance/GovernanceCollection"));
// THE ONE STATIC IMPORT IN A TABLE OF SEVENTEEN LAZY ONES, and it cost 1.1 MB.
// `ContextHubRoute` pulls `KnowledgeGraphPage`, which imports `@xyflow/react`
// and `elkjs/lib/elk.bundled.js` -- so the graph runtime of ONE section was
// linked into the entry chunk and every visitor of every screen downloaded it.
// Measured on the build: `App` 1 892.79 kB before, and the file's own rule
// already said what to do -- "a new section adds its `lazy()` and its `case`
// here".
const ContextHubRoute = lazy(() => import("../connaissances/ContextHubRoute"));

export function renderCollectionSurface(key: string | null, context: SurfaceContext): ReactNode {
  const {
    route, projectId, navigate, openOwner, backToOverview, onOpenDatastream,
    onAddDatastream, openAnalyzeResult, openTestObject, openDataObject,
  } = context;
  // Le `if (objectSurface)` qui enveloppait ce switch est reste chez l appelant :
  // c est lui qui sait s il y a deja une surface d objet. Ici, une table et rien
  // d autre.
  let surface: ReactNode;
  switch (key) {
    case "overview/project-overview":
      surface = <ProjectOverview projectId={projectId} onOpenOwner={openOwner} />;
      break;
    case "analyze/explore":
      surface = <Explore projectId={projectId} />;
      break;
    case "analyze/reports":
      // Story 52.1: Reports declares two lenses. `topics` opens the Answerable
      // Topic catalog -- the questions this project answers -- which is a lens
      // of Reports because `analyze-and-test.md:33` fixes Analyze at exactly
      // four Level 2 screens. A bare address canonicalizes to `reports`.
      // Story 67.26: `pacing` opens plan-versus-actual -- the Result the MCP App
      // has carried since Epic 22 and no console route could reach, which
      // `analyze-and-test.md:510` makes a criterion of Analyze being complete.
      // It is a lens for the same reason `topics` is: Analyze has exactly four
      // Level 2 screens.
      // Story 72.5: `templates` opens the Chart Template catalogue — the LAST
      // ratified object that reached no surface at all. It is a lens for the
      // reason `topics` and `pacing` are: Analyze has exactly four Level 2
      // screens. `result_id` travels in the address so a reader arriving from a
      // Result keeps the Result the list is judged against.
      surface = route.lens === "templates"
        ? <ChartTemplates
            projectId={projectId}
            resultId={route.query?.result_id ?? null}
            onOpenTemplate={(id) => navigate({ objectType: "chart-template", objectId: id, tab: "overview", versionId: null, action: null })}
          />
        : route.lens === "topics"
        ? <TopicCatalog projectId={projectId} />
        : route.lens === "pacing"
        ? <PacingReport
            projectId={projectId}
            // LA PORTE DE L'ÉTAT VIDE (ratifié 2026-08-24). La lentille dépend
            // d'un plan média, et le plan se crée dans le Workbench du
            // Datastream porteur : sans ces deux fonctions, l'état vide ne
            // pourrait que nommer le geste, ce que la décision refuse.
            onOpenDatastream={onOpenDatastream}
            onAddDatastream={onAddDatastream}
          />
        : <ReportsCollection projectId={projectId} onOpenReport={(id) => navigate({ objectType: "report", objectId: id, tab: "overview", versionId: null, action: null })} />;
      break;
    case "analyze/notebooks":
      surface = <NotebooksCollection projectId={projectId} onOpenNotebook={(id) => navigate({ objectType: "notebook", objectId: id, tab: "content", versionId: null, action: null })} />;
      break;
    case "analyze/renders":
      // `onOpenResult` crosses a SECTION, which is why the inline
      // objectType-only navigate the two siblings use could not express it:
      // a Result lives under `analyze/explore`, not under `renders`. It was
      // therefore left unpassed, and a Render could not reach the Result that
      // produced it -- the one link that makes a preserved presentation
      // auditable.
      surface = <RendersCollection
        projectId={projectId}
        onOpenRender={(id) => navigate({ objectType: "render", objectId: id, tab: "result", versionId: null, action: null })}
        onOpenResult={openAnalyzeResult}
        onOpenDossier={(id) => navigate({ objectType: "dossier", objectId: id, tab: "document", versionId: null, action: null })}
      />;
      break;
    case "test/golden-questions":
      surface = <GoldenQuestions projectId={projectId} onOpenGoldenQuestion={openTestObject("golden-questions", "golden-question")} />;
      break;
    case "test/regression-runs":
      surface = <RegressionRuns projectId={projectId} onOpenEvaluationRun={openTestObject("regression-runs", "evaluation-run")} onOpenTraceObservation={openTestObject("regression-runs", "trace-observation")} />;
      break;
    case "test/widget-feedback":
      surface = <WidgetFeedback projectId={projectId} onOpenFeedback={openTestObject("widget-feedback", "feedback-review")} />;
      // `onOpenOwner` is NOT passed here: `WidgetFeedback` takes an
      // `OwnerLink`, a different shape from the `OwnerReference` this shell
      // resolves. Mapping the two is a decision, not a wiring, and inventing
      // it silently is how an owner link starts pointing somewhere plausible
      // and wrong. Left unwired and named.
      break;
    case "data/data-overview":
      surface = <DataOverview projectId={projectId} />;
      break;
    case "data/datastreams":
      surface = <DataWorkspace projectId={projectId} onOpenDatastream={onOpenDatastream} onAddDatastream={onAddDatastream} />;
      break;
    case "data/events":
      surface = <BusinessContextPanel projectId={projectId} onOpenEventConfiguration={(id) => openDataObject("events", "event-configuration", id)} />;
      break;
    case "data/sources":
      // Two objects, two resolvers, one screen: the row opens the Source
      // Account, the Connector cell opens the Connector — through the same
      // `openDataObject` the Connectors catalog uses below, so there is one
      // address grammar and not a second one composed in the cell (AI-218).
      surface = (
        <Sources
          projectId={projectId}
          onOpenSourceAccount={(id) => openDataObject("sources", "source-account", id)}
          onOpenConnector={(id) => openDataObject("connectors", "connector", id)}
        />
      );
      break;
    case "data/imports":
      surface = <Imports projectId={projectId} onOpenImport={(id) => openDataObject("imports", "import", id)} />;
      break;
    case "data/connectors":
      surface = <ConnectorsCatalog projectId={projectId} onOpenConnector={(id) => openDataObject("connectors", "connector", id)} />;
      break;
    // One shell, four collections, fifteen route-backed lenses. The lens is
    // never null here: the parser canonicalizes a bare collection address to
    // the section's declared default before this switch is reached.
    case "governance/master-data":
    case "governance/semantic-model":
    case "governance/controls-quality":
    case "governance/evidence":
      surface = route.lens
        ? <GovernanceCollection projectId={projectId} section={route.section} lens={route.lens} />
        : <RouteState kind="unknown" onBackToOverview={backToOverview} />;
      break;
    case "context-hub/knowledge-graph":
    case "context-hub/knowledge-library":
    case "context-hub/skills-registry":
      surface = <ContextHubRoute projectId={projectId} section={route.section as "knowledge-graph" | "knowledge-library" | "skills-registry"} />;
      break;
    default:
      surface = <RouteState kind="unknown" onBackToOverview={backToOverview} />;
  }
  return surface;
}
