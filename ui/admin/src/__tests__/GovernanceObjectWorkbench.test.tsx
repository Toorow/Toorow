/**
 * Story 49.2 AC7/AC10 — the Used by and Versions tabs never answer "none" for
 * an object that has consumers.
 *
 * THE SENTENCE THIS FILE EXISTS TO KEEP OFF THE SCREEN. `UsedByPanel` branched
 * on `usedBy.count === 0 || usedBy.refs.length === 0`, so a Business Domain
 * with three live links — served as `{state: "available", count: 3, refs: []}`
 * — was rendered as *"Nothing depends on this object. Its owner answered, and
 * the answer is none."*, in the tab a person opens before archiving. The
 * Versions tab held the mirror image of the same branch.
 *
 * Mounted through the REAL router, for the reason `GovernanceScreens.test.tsx`
 * states: what fails is the chain, not a component handed the right props.
 */
import { cleanup, render, screen, within } from "@testing-library/react";
import ContentRouter from "../shell/ContentRouter";
import { RouterProvider } from "../shell/router";

const ORG = "org_EXAMPLE";
const PROJECT = "proj_EXAMPLE";
const ROOT = `/org/${ORG}/project/${PROJECT}`;
const DOMAIN = "bd_EXAMPLE";

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) } as Response;
}

function consumer(id: string, kind: string, workspace: string | null, label: string) {
  return {
    id,
    kind,
    label,
    workspace,
    workspace_label: workspace,
    pinned_version_id: null,
    recorded_at: "2026-08-30T09:00:00Z",
    owner_href:
      workspace === "context-hub"
        ? {
            surface: "project",
            workspace: "context-hub",
            section: "knowledge-library",
            object_type: "context-topic",
            object_id: id,
            tab: "usage",
          }
        : null,
    relation: "explains",
  };
}

function objectEnvelope(usedBy: unknown, versions: unknown) {
  return {
    schema_version: "governance-object.v1",
    project_ref: { object_type: "project", id: PROJECT },
    organization_ref: { object_type: "organization", id: ORG },
    section: "master-data",
    object_type: "business-domain",
    generated_at: "2026-08-30T09:00:00Z",
    evidence_as_of: "2026-08-30T08:00:00Z",
    state: "available",
    unavailable_reasons: [],
    object: {
      object_ref: {
        type: "business-domain",
        id: DOMAIN,
        label: "Commerce",
        owner_href: { surface: "project", workspace: "governance", section: "master-data" },
      },
      scope: "organization",
      owner: { kind: "business-taxonomy", organization_id: ORG },
      lifecycle_status: "active",
      active_version_ref: null,
      selected_version_ref: null,
      available_tabs: ["overview", "hierarchy", "mappings-aliases", "used-by", "versions"],
      default_tab: "overview",
      allowed_actions: [],
      used_by: usedBy,
      versions,
      evidence: { state: "empty", count: 0, refs: [], truncated: false },
      summary: { slug: "commerce" },
      evidence_as_of: "2026-08-30T08:00:00Z",
    },
  };
}

const EMPTY_VERSIONS = { state: "empty", count: 0, refs: [], truncated: false };
const EMPTY_USED_BY = { state: "empty", count: 0, refs: [], truncated: false };

function serve(body: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve(ok(body))),
  );
}

function open(pathname: string) {
  window.history.replaceState({}, "", pathname);
  return render(
    <RouterProvider>
      <ContentRouter />
    </RouterProvider>,
  );
}

const USED_BY_TAB = `${ROOT}/governance/master-data/object/business-domain/${DOMAIN}/tab/used-by`;
const VERSIONS_TAB = `${ROOT}/governance/master-data/object/business-domain/${DOMAIN}/tab/versions`;

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  window.history.replaceState({}, "", "/");
});

describe("Used by never says none for an object that has consumers", () => {
  it("lists three consumers under the workspace each one belongs to", async () => {
    serve(
      objectEnvelope(
        {
          state: "available",
          count: 3,
          truncated: false,
          refs: [
            consumer("top_1", "topic", "context-hub", "Quarterly revenue"),
            consumer("prc_1", "procedure", "context-hub", "Close the month"),
            consumer("active_users", "target_field", "data", "Active users"),
          ],
        },
        EMPTY_VERSIONS,
      ),
    );
    open(USED_BY_TAB);

    expect(await screen.findByText("Quarterly revenue")).toBeInTheDocument();
    expect(screen.queryByText("Nothing depends on this object")).not.toBeInTheDocument();

    // Grouped by the workspace the SERVER named, and by nothing else.
    const contextHub = await screen.findByRole("group", { name: "Context Hub" });
    expect(within(contextHub).getByText("Close the month")).toBeInTheDocument();
    expect(within(contextHub).queryByText("Active users")).not.toBeInTheDocument();
  });

  it("opens each consumer at its owner, when the server proved a route", async () => {
    serve(
      objectEnvelope(
        {
          state: "available",
          count: 1,
          truncated: false,
          refs: [consumer("top_1", "topic", "context-hub", "Quarterly revenue")],
        },
        EMPTY_VERSIONS,
      ),
    );
    open(USED_BY_TAB);

    const link = await screen.findByRole("link", { name: "Quarterly revenue" });
    expect(link).toHaveAttribute(
      "href",
      `${ROOT}/context-hub/knowledge-library/object/context-topic/top_1/tab/usage`,
    );
  });

  it("files a consumer with no recorded workspace under Other, never under a workspace", async () => {
    serve(
      objectEnvelope(
        {
          state: "available",
          count: 1,
          truncated: false,
          refs: [consumer("bud_1", "budget", null, "FY26 budget")],
        },
        EMPTY_VERSIONS,
      ),
    );
    open(USED_BY_TAB);

    const other = await screen.findByRole("group", { name: "Other" });
    expect(within(other).getByText("FY26 budget")).toBeInTheDocument();
    // The re-ventilation this replaced poured every unplaced reference into
    // Context Hub, which reported the wrong owner for all of them.
    const contextHub = screen.getByRole("group", { name: "Context Hub" });
    expect(within(contextHub).queryByText("FY26 budget")).not.toBeInTheDocument();
    expect(within(contextHub).getByText("No Skill or Knowledge item uses this object.")).toBeInTheDocument();
  });

  it("says how many there are when the owner counted them and could not list them", async () => {
    serve(
      objectEnvelope(
        {
          state: "unavailable",
          count: 3,
          truncated: false,
          refs: [],
          reason: {
            code: "facet_refs_not_composed",
            message: "3 recorded, and this owner cannot list them here.",
          },
        },
        EMPTY_VERSIONS,
      ),
    );
    open(USED_BY_TAB);

    expect(await screen.findByText("3 consumer(s) recorded, not listed here")).toBeInTheDocument();
    expect(screen.queryByText("Nothing depends on this object")).not.toBeInTheDocument();
  });

  it("refuses to say none for the exact payload the verdict measured", async () => {
    // `{state: "available", count: 3, refs: []}` is what the read model served on
    // 2026-09-01, and the screen answered "Nothing depends on this object". The
    // server cannot compose that shape any more; this holds the screen to the
    // same rule, so a regression on either side is visible on this one line.
    serve(
      objectEnvelope(
        { state: "available", count: 3, refs: [], truncated: false },
        EMPTY_VERSIONS,
      ),
    );
    open(USED_BY_TAB);

    expect(await screen.findByText("3 consumer(s) recorded, not listed here")).toBeInTheDocument();
    expect(screen.queryByText("Nothing depends on this object")).not.toBeInTheDocument();
  });

  it("still says none when the owner answered and the answer is none", async () => {
    serve(objectEnvelope(EMPTY_USED_BY, EMPTY_VERSIONS));
    open(USED_BY_TAB);

    expect(await screen.findByText("Nothing depends on this object")).toBeInTheDocument();
  });
});

describe("Versions never says none for an object that has versions", () => {
  it("lists the versions the server composed", async () => {
    serve(
      objectEnvelope(EMPTY_USED_BY, {
        state: "available",
        count: 3,
        truncated: false,
        refs: [
          { object_type: "master-data-object-version", id: "mdver_3", version: 3, state: "current", recorded_at: "2026-08-30T09:00:00Z" },
          { object_type: "master-data-object-version", id: "mdver_2", version: 2, state: "superseded", recorded_at: "2026-08-29T09:00:00Z" },
          { object_type: "master-data-object-version", id: "mdver_1", version: 1, state: "superseded", recorded_at: "2026-08-28T09:00:00Z" },
        ],
      }),
    );
    open(VERSIONS_TAB);

    expect(await screen.findByText("mdver_3")).toBeInTheDocument();
    expect(screen.queryByText("No version recorded")).not.toBeInTheDocument();
  });

  it("says how many versions there are when the ledger could not be listed", async () => {
    serve(
      objectEnvelope(EMPTY_USED_BY, {
        state: "unavailable",
        count: 3,
        truncated: false,
        refs: [],
        reason: { code: "facet_refs_not_composed", message: "3 recorded, and this owner cannot list them here." },
      }),
    );
    open(VERSIONS_TAB);

    expect(await screen.findByText("3 version(s) recorded, not listed here")).toBeInTheDocument();
    expect(screen.queryByText("No version recorded")).not.toBeInTheDocument();
  });
});
