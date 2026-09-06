/**
 * The left rail, rendered on its own so it can be LOOKED AT.
 *
 * This entry is the reason the rail's defect survived a whole migration. Every
 * screen in the console has a sandbox address and has been captured; the SHELL
 * had none, because it lives behind `AuthGate` and `ScopeProvider` and needs a
 * project in scope. So the one surface visible on every single page was the one
 * surface no design pass could open — and it was rendering six workspaces as a
 * stack of outlined pills, which is what `Button` is contractually for and what
 * a menu is contractually not.
 *
 *     /debug/screen?name=ShellSidebar               the rail, Data expanded
 *     /debug/screen?name=ShellSidebar&api=refusing  the Datastream tree failing
 *     /debug/screen?name=ShellSidebar&theme=dark    the dark rail
 *
 * `StableSidebar` reads `useRoute()`, and `RouterProvider` parses the real
 * address — which under the sandbox is `/debug/screen`, an unresolved route
 * with no project. Rewriting the address BEFORE the provider mounts is what
 * gives it a project route to parse; the query string is preserved so `api` and
 * `theme` keep working. `ScreenSandbox` has already read `name` by then.
 */
import { useState } from "react";
import { RouterProvider } from "./router";
import { installSandboxApi } from "./sandboxApi";
import StableSidebar from "./StableSidebar";
import OrgSettings from "./pages/OrgSettings";
import type { OrganizationSettingsSection } from "./router";

const PROJECT_ID = "demo";
const PROJECT_PATH = `/org/acme/project/${PROJECT_ID}/data/datastreams`;

/**
 * Two Datastreams whose names are the shape that broke the rail: generated,
 * long, and carrying their selection. No production identifier appears.
 *
 * The envelope is the WIRE contract, not a convenient object: `getDataSurface`
 * refuses anything whose `schema_version` and `project_ref.id` do not echo what
 * it asked for, and the first fixture here was rejected by exactly that check.
 * The screen was right and the fixture was wrong.
 */
const DATASTREAMS = {
  schema_version: "data-datastreams.v1",
  project_ref: { object_type: "project", id: PROJECT_ID },
  generated_at: "2026-08-04T00:00:00+00:00",
  evidence_as_of: "2026-08-04T00:00:00+00:00",
  unavailable_reasons: [],
  allowed_actions: [],
  items: [
    {
      object_ref: { object_type: "datastream", id: "ds_EXAMPLE_ONE" },
      name: "Google Search Console - Custom selection [sc-domain:example.com]",
      source_kind: "connector_pull",
      connector_ref: { object_type: "connector", id: "search_console" },
      states: { run: "succeeded" },
      evidence: {},
      evidence_as_of: null,
      links: {},
    },
    {
      object_ref: { object_type: "datastream", id: "ds_EXAMPLE_TWO" },
      name: "Weekly media plan upload",
      source_kind: "managed_feed",
      states: { run: "blocked" },
      evidence: {},
      evidence_as_of: null,
      links: {},
    },
  ],
};

/** What `GET /api/organizations/{id}` answers. No production identifier. */
const ORG = {
  id: "org_EXAMPLE",
  name: "Acme",
  slug: "acme",
  status: "active",
  billing_ref: null,
  brand_primary: null,
  brand_secondary: null,
  brand_accent: null,
  logo_url: null,
};

/**
 * A scope surface INSIDE the shell — the shape Jean was looking at when he
 * asked why Organization Settings does not follow the console's organization.
 *
 * Organization Settings is the strict case of both repairs at once: it carries
 * no Project in its address, so it is the surface whose rail has to come from
 * the remembered scope, and it was the loudest of the four rendering its
 * sections as a card of rows instead of the rose band.
 *
 *     /debug/screen?name=ShellScopeSurface
 */
export function ShellScopeSurface() {
  const [section, setSection] = useState<OrganizationSettingsSection>("general");
  installSandboxApi(() => ({ body: ORG, status: 200 }));
  return (
    <RouterProvider>
      <div className="grid min-h-screen grid-cols-[16rem_minmax(0,1fr)] bg-background">
        <StableSidebar scope={{ organizationId: "acme", projectId: PROJECT_ID }} />
        {/* Same gutter as `ApplicationShell`. A sandbox that frames a screen
            differently from the shell is a sandbox nobody can judge alignment in. */}
        <main className="min-w-0 bg-background px-4 pt-6 pb-8 sm:px-page-gutter sm:pt-8 sm:pb-page-gutter">
          <OrgSettings orgId={ORG.id} section={section} onSectionChange={setSection} initialOrg={ORG} />
        </main>
      </div>
    </RouterProvider>
  );
}

export function ShellSidebar() {
  if (window.location.pathname !== PROJECT_PATH) {
    window.history.replaceState({}, "", `${PROJECT_PATH}${window.location.search}`);
  }
  installSandboxApi(() => ({ body: DATASTREAMS, status: 200 }));
  return (
    <RouterProvider>
      <div className="grid min-h-screen grid-cols-[16rem_minmax(0,1fr)] bg-background">
        <StableSidebar scope={{ organizationId: "acme", projectId: PROJECT_ID }} />
        <div />
      </div>
    </RouterProvider>
  );
}
