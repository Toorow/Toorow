/**
 * ContentRouter — Epic 42 story 42.1.
 *
 * Maps the active route (workspace + section) to the surface it renders. During
 * the migration this mounts the EXISTING panels into their v3 homes; sections
 * without a real surface yet render a clean designed empty state (never blank).
 * Legacy cross-navigation callbacks are bridged onto the deep-linkable router.
 */
import { Box, Typography } from "@mui/material";
import { useRoute, type Route } from "./router";
import { useScope } from "./scope";
import { WORKSPACE_BY_KEY } from "./navigation";
import type { NavSection } from "../Sidebar";

// v3 pages (faithful mockup ports) + existing panels mounted in their v3 homes
import Overview from "./pages/Overview43";
import DataWorkspace from "./pages/DataWorkspace";
import Sources from "./pages/Sources";
import ProjectMapping from "./pages/ProjectMapping";
import CountrySplit from "./pages/CountrySplit";
import ModulesCatalog from "./pages/ModulesCatalog";
import GoldenQuestions from "./pages/GoldenQuestions";
import RegressionRuns from "./pages/RegressionRuns";
import WidgetFeedback from "./pages/WidgetFeedback";
import Provenance from "./pages/Provenance";
import Procedures from "./pages/Procedures";
import ReconciliationMethods from "./pages/ReconciliationMethods";
import Competitors from "./pages/Competitors";
import Activity from "./pages/Activity";
import DatastreamOverview from "./pages/DatastreamOverview";
import DatastreamData from "./pages/DatastreamData";
import DatastreamMapping from "./pages/DatastreamMapping";
import DatastreamRecovery from "./pages/DatastreamRecovery";
import DatastreamCreate from "./pages/DatastreamCreate";
import FirstPublication from "./pages/FirstPublication";
import GettingStarted from "./pages/GettingStarted";
import ProjectSettings from "./pages/ProjectSettings";
import OrgSettings from "./pages/OrgSettings";
import AccountSettings from "./pages/AccountSettings";
import ReportsPanel from "../ReportsPanel";
import WidgetCardsPage from "../WidgetCardsPage";
import NotebooksPanel from "../NotebooksPanel";
import RenderGalleryPage from "../RenderGalleryPage";
import MediaplansShell from "../mediaplans/MediaplansShell";
import DataModelPage from "../DataModelPage";
import DataQualityPage from "../DataQualityPage";
import KnowledgeBasePage from "../KnowledgeBasePage";
import KnowledgeGraphPage from "../KnowledgeGraphPage";
import BusinessContextPanel from "../BusinessContextPanel";

/** Bridge legacy NavSection navigation onto the new router. */
function navSectionToRoute(section: NavSection): Partial<Route> {
  switch (section) {
    case "vue-ensemble":
    case "mise-en-route":
      return { workspace: "overview", section: null };
    case "rapports":
      return { workspace: "analyze", section: "reports" };
    case "cartes":
      return { workspace: "analyze", section: "widgets" };
    case "rendus":
      return { workspace: "analyze", section: "renders" };
    case "notebooks":
      return { workspace: "analyze", section: "notebooks" };
    case "datastreams":
    case "creer-flux":
      return { workspace: "data", section: "datastreams" };
    case "autorisations":
    case "modules":
      return { workspace: "data", section: "sources" };
    case "mediaplans":
      return { workspace: "data", section: "imports" };
    case "data-model":
      return { workspace: "governance", section: "semantic-model" };
    case "qualite":
    case "conflits":
      return { workspace: "governance", section: "data-quality" };
    case "connaissances":
      return { workspace: "context", section: "knowledge" };
    case "contexte":
      return { workspace: "context", section: "events" };
    default:
      return { workspace: "overview", section: null };
  }
}

function ComingSoon({
  workspace,
  section,
}: {
  workspace: string;
  section: string;
}) {
  return (
    <Box
      sx={{
        border: 1,
        borderColor: "divider",
        borderRadius: 4,
        bgcolor: "background.paper",
        p: 6,
        textAlign: "center",
      }}
    >
      <Typography variant="h2" sx={{ mb: 1 }}>
        {section}
      </Typography>
      <Typography sx={{ color: "text.secondary", maxWidth: 520, mx: "auto" }}>
        This {workspace} surface is being migrated to the v3 experience. Its
        data and actions land here as the workspace story completes.
      </Typography>
    </Box>
  );
}

export default function ContentRouter() {
  const { route, navigate } = useRoute();
  const { org } = useScope();
  const projectId = route.projectId;
  const onNavigate = (section: NavSection) =>
    navigate(navSectionToRoute(section));
  const onOpenDatastream = (id: string, tab?: string) =>
    navigate({
      workspace: "data",
      section: "datastreams",
      objectType: "datastream",
      objectId: id,
      tab: tab ?? "overview",
    });
  const openModule = (moduleId: string) =>
    navigate({
      workspace: "data",
      section: "modules",
      objectType: "module",
      objectId: moduleId,
    });
  const full = { projectId, onOpenDatastream, onNavigate };

  // Datastream object detail — local tabs (Overview/Data/Mapping/Runs/…)
  if (route.objectType === "datastream" && route.objectId) {
    const dsId = route.objectId;
    switch (route.tab ?? "overview") {
      case "data":
        return (
          <DatastreamData
            projectId={projectId}
            datastreamId={dsId}
            onNavigateTab={(tab) => onOpenDatastream(dsId, tab)}
          />
        );
      case "mapping":
        return (
          <DatastreamMapping
            projectId={projectId}
            datastreamId={dsId}
            onNavigateTab={(tab) => onOpenDatastream(dsId, tab)}
          />
        );
      case "runs":
      case "recovery":
        return <DatastreamRecovery projectId={projectId} datastreamId={dsId} onNavigateTab={(tab) => onOpenDatastream(dsId, tab)} />;
      case "first-publication":
        return <FirstPublication projectId={projectId} datastreamId={dsId} />;
      case "overview":
      default:
        return (
          <DatastreamOverview
            projectId={projectId}
            datastreamId={dsId}
            onNavigateTab={(tab) => onOpenDatastream(dsId, tab)}
          />
        );
    }
  }

  // Activatable alignment module detail — Data > Modules > <module>. Country
  // split is the built member; its ModuleSettings surface opens here.
  if (route.objectType === "module" && route.objectId) {
    const backToModules = () =>
      navigate({ workspace: "data", section: "modules" });
    switch (route.objectId) {
      case "country-split":
        return <CountrySplit projectId={projectId} onBack={backToModules} />;
      default:
        return (
          <ModulesCatalog projectId={projectId} onOpenModule={openModule} />
        );
    }
  }

  // Scope settings (reached from the TopBar scope actions menu, any workspace).
  if (route.section === "project-settings")
    return <ProjectSettings projectId={projectId} />;
  // org is null until the scope has loaded (see ScopeValue.state) — the shell
  // gates on that, so this only guards the type.
  if (route.section === "org-settings")
    return org ? <OrgSettings orgId={org.id} /> : null;
  // The signed-in person's own account (identity + account erasure). Like the
  // organization settings it is NOT a project workspace: it is reached from the
  // TopBar scope control, and it needs no scope at all — it is about the user.
  if (route.section === "account") return <AccountSettings />;

  const key = `${route.workspace}/${route.section ?? ""}`;
  switch (key) {
    // Overview
    case "overview/":
      return (
        <Overview
          projectId={projectId}
          onAddDatastream={() =>
            navigate({ workspace: "data", section: "add" })
          }
          onOpenDataOverview={() =>
            navigate({ workspace: "data", section: "data-overview" })
          }
          onOpenAnalyze={() =>
            navigate({ workspace: "analyze", section: "reports" })
          }
          onOpenDatastream={onOpenDatastream}
          onOpenRenders={() =>
            navigate({ workspace: "analyze", section: "renders" })
          }
          onOpenRender={(renderId) =>
            navigate({
              workspace: "analyze",
              section: "renders",
              objectType: "render",
              objectId: renderId,
            })
          }
        />
      );
    case "overview/getting-started":
      return <GettingStarted projectId={projectId} />;

    // Analyze
    case "analyze/reports":
      return <ReportsPanel {...full} />;
    case "analyze/widgets":
      return <WidgetCardsPage {...full} />;
    case "analyze/notebooks":
      return <NotebooksPanel {...full} />;
    case "analyze/renders":
      return <RenderGalleryPage projectId={projectId} />;

    // Test
    case "test/golden-questions":
      return <GoldenQuestions projectId={projectId} />;
    case "test/regression-runs":
      return <RegressionRuns projectId={projectId} />;
    case "test/widget-feedback":
      return <WidgetFeedback projectId={projectId} />;

    // Data
    case "data/data-overview":
      return (
        <DataWorkspace
          projectId={projectId}
          onOpenDatastream={onOpenDatastream}
          onAddDatastream={() =>
            navigate({ workspace: "data", section: "add" })
          }
        />
      );
    case "data/sources":
      return (
        <Sources
          projectId={projectId}
          onAddDatastream={() =>
            navigate({ workspace: "data", section: "add" })
          }
        />
      );
    case "data/modules":
      return <ModulesCatalog projectId={projectId} onOpenModule={openModule} />;
    case "data/add":
      return (
        <DatastreamCreate
          projectId={projectId}
          onCancel={() =>
            navigate({ workspace: "data", section: "data-overview" })
          }
          onActivated={(datastreamId) =>
            navigate({
              workspace: "data",
              section: "datastreams",
              objectType: "datastream",
              objectId: datastreamId,
              tab: "first-publication",
            })
          }
        />
      );
    case "data/datastreams":
      // The v3 fleet list (DataWorkspace) is the datastreams list; individual
      // streams open into their object-detail tabs (handled above). The legacy
      // MUI ops list is retired.
      return (
        <DataWorkspace
          projectId={projectId}
          onOpenDatastream={onOpenDatastream}
          onAddDatastream={() =>
            navigate({ workspace: "data", section: "add" })
          }
        />
      );
    case "data/imports":
      return <MediaplansShell projectId={projectId} />;

    // Governance
    case "governance/semantic-model":
      return <DataModelPage {...full} />;
    case "governance/mapping":
      return <ProjectMapping projectId={projectId} />;
    case "governance/competitors":
      return <Competitors projectId={projectId} />;
    case "governance/data-quality":
      return <DataQualityPage {...full} />;
    case "governance/reconciliation":
      return <ReconciliationMethods projectId={projectId} />;
    case "governance/provenance":
      return <Provenance projectId={projectId} />;
    case "governance/activity":
      return <Activity projectId={projectId} />;

    // Context
    case "context/knowledge":
      return <KnowledgeBasePage projectId={projectId} />;
    case "context/graph":
      return (
        <KnowledgeGraphPage
          projectId={projectId}
          onOpenKnowledge={() =>
            navigate({ workspace: "context", section: "knowledge" })
          }
          onOpenProcedures={() =>
            navigate({ workspace: "context", section: "procedures" })
          }
          onOpenSemanticModel={() =>
            navigate({ workspace: "governance", section: "semantic-model" })
          }
        />
      );
    case "context/events":
      return <BusinessContextPanel {...full} />;
    case "context/procedures":
      return <Procedures projectId={projectId} />;

    default: {
      const ws = WORKSPACE_BY_KEY[route.workspace];
      return (
        <ComingSoon workspace={ws.label} section={route.section ?? ws.label} />
      );
    }
  }
}
