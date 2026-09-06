import KnowledgeBasePage from "../KnowledgeBasePage";
import KnowledgeGraphPage from "../KnowledgeGraphPage";
import Procedures from "../shell/pages/Procedures";
import type { OwnerReference } from "../governance/governanceSurface";
import { WORKSPACE_BY_KEY, type WorkspaceKey } from "../shell/navigation";
import { useRoute } from "../shell/router";
import ContextHubLayout, { type ContextHubContentContext } from "./ContextHubLayout";

type Section = "knowledge-graph" | "knowledge-library" | "skills-registry";
type View = "graph" | "library" | "skills";

const viewBySection: Record<Section, View> = {
  "knowledge-graph": "graph",
  "knowledge-library": "library",
  "skills-registry": "skills",
};
const sectionByView: Record<View, Section> = {
  graph: "knowledge-graph",
  library: "knowledge-library",
  skills: "skills-registry",
};

export default function ContextHubRoute({ projectId, section }: { projectId: string; section: Section }) {
  const { navigate } = useRoute();
  const openCollection = (next: Section) => navigate({ workspace: "context-hub", section: next, objectType: null, objectId: null, tab: null, versionId: null, action: null });
  const openObject = (objectType: "context-topic" | "context-procedure", objectId: string) => navigate({
    workspace: "context-hub",
    section: objectType === "context-topic" ? "knowledge-library" : "skills-registry",
    objectType,
    objectId,
    tab: "content",
    versionId: null,
    action: null,
  });
  /**
   * Story 49.6 AC9 — hand an Event over to the surface that OWNS it.
   *
   * The reference is composed server-side by `evidence_index.owner_route`; this
   * only translates the wire's snake_case to the router's keys. Choosing the
   * workspace or the tab here would be Context Hub deciding where an Event
   * lives, which is what AC9 forbids.
   *
   * The workspace is CHECKED, not cast. `ContentRouter.openOwner` states the
   * reason on its own branch: an owner reference is untrusted input, and a
   * workspace this shell does not know must not be navigated to. An unknown
   * one leaves the person where they are rather than on a blank surface.
   */
  const openEventOwner = (reference: OwnerReference) => {
    const { workspace, section } = reference;
    // Both, or neither: a workspace without a section lands on a surface the
    // router has to guess, and guessing is how a link "works" onto the wrong
    // page. An incomplete reference is not a navigation.
    if (!workspace || !section || !(workspace in WORKSPACE_BY_KEY)) return;
    navigate({
      workspace: workspace as WorkspaceKey,
      section,
      objectType: reference.object_type,
      objectId: reference.object_id,
      tab: reference.tab,
      versionId: reference.version_id,
      action: null,
    });
  };
  /**
   * The layout owns the governed taxonomy and hands its selection down.
   *
   * Story 45.2 needs it so the skill drawer can assign a business scope through
   * the existing association store; Story 45.3 needs it so the mindmap can be
   * scoped to the selected domain or layer without a second request. Both were
   * unwired: the drawer had no call site at all, and the graph received no scope.
   */
  const renderContent = (context: ContextHubContentContext) => {
    if (section === "knowledge-library") {
      return (
        <KnowledgeBasePage
          projectId={projectId}
          onOpenTopic={(id) => openObject("context-topic", id)}
          businessContext={context}
        />
      );
    }
    if (section === "skills-registry") {
      return (
        <Procedures
          projectId={projectId}
          onOpenProcedure={(id) => openObject("context-procedure", id)}
          businessContext={context}
        />
      );
    }
    return (
      <KnowledgeGraphPage
        projectId={projectId}
        initialBundle={context.graph}
        businessScope={{
          selected: context.selected,
          domains: context.domains,
          classifications: context.classifications,
          links: context.links,
          onSelect: context.selectTaxonomy,
        }}
        onOpenKnowledge={() => openCollection("knowledge-library")}
        onOpenProcedure={(id) => openObject("context-procedure", id)}
        /* A remark read in the mindmap is answered on the node's own workbench,
           where the editor lives (`context-hub.md`: "one does not work a Skill
           inside a mindmap"). The route already knows how to open either kind
           of object; the drawer only names which one. */
        onAdjustNode={(nodeType, nodeId) => openObject(nodeType === "topic" ? "context-topic" : "context-procedure", nodeId)}
        /* Story 49.6 AC9 — the overlay hands over an Event to the surface that
           OWNS it. The reference is composed server-side by `owner_route`; this
           handler only translates the wire's snake_case to the router's keys.
           Deciding the workspace or the tab here would be Context Hub choosing
           where an Event lives, which is exactly what AC9 forbids. */
        onOpenEvent={openEventOwner}
        /* The AI Path picker's door. `navigation/contextHub.ts` declares
           `{ type: "ai-path" }` under Knowledge Graph and `objectSurfaces.tsx`
           serves `AiPathPage` for it — the address existed, nothing navigated
           to it. Built through `navigate`, the router's own vocabulary, so no
           part of this path is a string this file composed. */
        onOpenAiPath={(pathId) => navigate({
          workspace: "context-hub",
          section: "knowledge-graph",
          objectType: "ai-path",
          objectId: pathId,
          tab: null,
          versionId: null,
          action: null,
        })}
        onOpenSemanticModel={() => navigate({ workspace: "governance", section: "semantic-model", objectType: null, objectId: null, tab: null, versionId: null, action: null })}
      />
    );
  };
  return (
    <div data-owner={`context-hub/${section}`}>
      <ContextHubLayout
        projectId={projectId}
        activeView={viewBySection[section]}
        onNavigate={(view) => openCollection(sectionByView[view])}
        editable
        renderContent={renderContent}
      />
    </div>
  );
}
