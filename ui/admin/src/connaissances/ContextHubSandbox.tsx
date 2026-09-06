/**
 * ContextHubSandbox — the Context Hub family, mountable at `/debug/screen`.
 *
 * WHY IT DID NOT EXIST, AND WHAT THAT COST. `ScreenSandbox` registers
 * Governance (4), Analyze and Test (7), the Data screens, the shell rail and
 * the Datastream Workbench. It registered NOTHING from the Context Hub. So the
 * one requirement the note of 2026-08-07 §B.3 states — look at the ELK graph,
 * the drawers and the timeline in a REAL browser — could not be met at all,
 * and not for want of trying: there was no address to open.
 *
 * `GovernanceSandbox.tsx` says the same thing about its own family, in its own
 * words: a screen that cannot be mounted alone is a screen no design pass can
 * see. This module is that repair for the Hub, and it is deliberately the whole
 * family rather than the one screen that was asked about — a sandbox missing
 * four of five surfaces would leave the same hole one size smaller.
 *
 * NO ROUTER IS SUPPLIED, unlike GovernanceSandbox. Measured: none of these five
 * screens reads `shell/router` (`grep -c "shell/router"` → 0 on each). They take
 * their navigation as callbacks, so the sandbox hands them ones that record
 * rather than navigate — a click here must be visible to a browser pass, not
 * swallowed.
 *
 * The API is NOT stubbed here. In a browser the calls simply fail and each
 * screen shows its own error surface, which is itself worth looking at; under
 * Playwright they are intercepted and answered with fixtures
 * (`scripts/context_hub_browser_pass.py`). Building the fixture into the
 * component would make the sandbox prove the fixture instead of the screen.
 */

import AiPathPage from "./AiPathPage";
import ContextObjectPage from "../ContextObjectPage";
import KnowledgeBasePage from "../KnowledgeBasePage";
import KnowledgeGraphPage from "../KnowledgeGraphPage";
import Procedures from "../shell/pages/Procedures";

const PROJECT_ID = "proj_EXAMPLE";

/** Records the intent on the document, so a browser pass can assert the wire
 *  without a router. An `onOpen…` that silently does nothing would let a dead
 *  callback pass for a live one. */
function record(name: string) {
  return (arg?: unknown) => {
    if (typeof document === "undefined") return;
    const detail = typeof arg === "string" ? `${name}:${arg}` : name;
    document.documentElement.setAttribute("data-sandbox-last-intent", detail);
  };
}

/** Knowledge Graph — the ELK mindmap. The surface §B.3 names first. */
export function ContextKnowledgeGraph() {
  return (
    <KnowledgeGraphPage
      projectId={PROJECT_ID}
      onOpenKnowledge={record("open-knowledge")}
      onOpenProcedure={record("open-procedure")}
      onOpenSemanticModel={record("open-semantic-model")}
      onOpenEvent={record("open-event")}
    />
  );
}

/** Knowledge Library — the topic collection and its editor drawer. */
export function ContextKnowledgeLibrary() {
  return <KnowledgeBasePage projectId={PROJECT_ID} onOpenTopic={record("open-topic")} />;
}

/** Skills Registry — the Skill collection and its editor drawer. */
export function ContextSkillsRegistry() {
  return <Procedures projectId={PROJECT_ID} onOpenProcedure={record("open-procedure")} />;
}

/** A Skill's own workbench — the object page behind the registry. */
export function ContextObjectWorkbench() {
  return (
    <ContextObjectPage
      projectId={PROJECT_ID}
      kind="procedure"
      objectId="proc_EXAMPLE"
      tab="content"
      onNavigateTab={(tab) => record("navigate-tab")(tab)}
      onOpenVersion={(versionId) => record("open-version")(versionId ?? "current")}
      versionHref={(versionId) => `#version-${versionId}`}
      onBack={record("back")}
    />
  );
}

/** AI Path — the timeline. The third surface §B.3 names. */
export function ContextAiPath() {
  return <AiPathPage projectId={PROJECT_ID} pathId="aip_EXAMPLE" />;
}
