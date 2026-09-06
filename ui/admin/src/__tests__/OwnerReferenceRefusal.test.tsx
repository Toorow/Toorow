/**
 * An owner reference the shell cannot resolve is REFUSED OUT LOUD.
 *
 * The defect this file was written against, measured by the audit of
 * 2026-08-17: the server composed `action: "first-publication"` on the
 * `datastream` object, the `datastream` navigation contract declares no
 * actions, and `ContentRouter.openOwner` answered with a bare `return`. The
 * most important item of a brand-new Project rendered a button that clicked and
 * did nothing -- against `overview.md:132`, "No silent navigation fallback".
 *
 * Two halves are pinned here:
 *
 *   * THE REPOINTING. The reference the server composes today -- the Renders
 *     collection, ratified by the amendment of 2026-08-17 to
 *     `first-figure-path.md` -- resolves to one exact navigation. The shape it
 *     replaced does not, and never will, because no contract declares it.
 *   * THE CLASS. Every unresolvable reference, not just that one, produces a
 *     sentence and a gesture. The gesture is a real destination and it is
 *     followed when clicked.
 *
 * `Toaster` is mounted FIRST and it is not scaffolding: sonner subscribes at
 * mount, so a Toaster placed after the component misses a toast emitted during
 * that component's first render.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// jsdom implements no pointer capture, and sonner's swipe-to-dismiss handler
// calls it on every pointerdown -- so clicking the toast's own action button
// threw. The environment is incomplete, not the screen.
import "./radixJsdom";
import ContentRouter from "../shell/ContentRouter";
import { resolveOwnerReference, type OwnerReferenceInput } from "../shell/ownerResolution";
import { Toaster } from "../ui";

const owner = (patch: Partial<OwnerReferenceInput>): OwnerReferenceInput => ({
  surface: "project",
  workspace: null,
  section: null,
  global_surface: null,
  global_section: null,
  object_type: null,
  object_id: null,
  tab: null,
  action: null,
  version_id: null,
  evidence_id: null,
  ...patch,
});

/** What `project_readiness.py` and `project_overview.py` compose today. */
const FIRST_PUBLICATION_OWNER = owner({ workspace: "analyze", section: "renders" });

/** What they composed before the amendment -- kept as a test subject on purpose:
 *  it is the exact shape that must never resolve again. */
const REFUSED_FIRST_PUBLICATION_OWNER = owner({
  workspace: "data",
  section: "datastreams",
  object_type: "datastream",
  object_id: "ds-1",
  action: "first-publication",
});

// ---------------------------------------------------------------------------
// The repointing
// ---------------------------------------------------------------------------

describe("the first-publication gesture reaches the share and render destination", () => {
  it("resolves the ratified owner into one exact navigation", () => {
    const resolved = resolveOwnerReference(FIRST_PUBLICATION_OWNER);
    expect(resolved).toEqual({
      kind: "workspace",
      workspace: "analyze",
      section: "renders",
      lens: null,
      objectType: null,
      objectId: null,
      tab: null,
      versionId: null,
      action: null,
    });
  });

  it("still refuses the shape it replaced, and says so", () => {
    const resolved = resolveOwnerReference(REFUSED_FIRST_PUBLICATION_OWNER);
    expect(resolved.kind).toBe("refused");
    if (resolved.kind !== "refused") return;
    expect(resolved.code).toBe("unknown_object_action");
    // The sentence is the reader's, not the route model's.
    for (const ours of ["action", "object_type", "contract", "datastream", "navigation"]) {
      expect(resolved.reason.toLowerCase()).not.toContain(ours);
    }
  });

  it("keeps opening a Render on its Sharing tab, which is where a web link lives", () => {
    const resolved = resolveOwnerReference(
      owner({ workspace: "analyze", section: "renders", object_type: "render", object_id: "r-1", tab: "sharing" }),
    );
    expect(resolved.kind).toBe("workspace");
    if (resolved.kind !== "workspace") return;
    expect(resolved.tab).toBe("sharing");
  });
});

// ---------------------------------------------------------------------------
// The class: no refusal is silent, and each one names a gesture
// ---------------------------------------------------------------------------

describe("every unresolvable owner reference names why, and names a gesture", () => {
  it.each([
    ["an unknown workspace", owner({ workspace: "nowhere", section: "renders" }), "unknown_section"],
    ["an unknown section", owner({ workspace: "analyze", section: "nowhere" }), "unknown_section"],
    ["a kind of object the section does not hold", owner({ workspace: "analyze", section: "renders", object_type: "datastream", object_id: "x" }), "unknown_object_kind"],
    ["a view the object does not have", owner({ workspace: "analyze", section: "renders", object_type: "render", object_id: "x", tab: "nowhere" }), "unknown_object_view"],
    ["an action the object does not offer", REFUSED_FIRST_PUBLICATION_OWNER, "unknown_object_action"],
    ["an action the collection does not offer", owner({ workspace: "analyze", section: "renders", action: "publish" }), "unknown_section_action"],
    ["an object named without an id", owner({ workspace: "analyze", section: "renders", object_type: "render" }), "incomplete_object"],
    ["a version with no view to open it in", owner({ workspace: "analyze", section: "reports", object_type: "report", object_id: "x", version_id: "v1" }), "version_without_view"],
    ["a version pinned under a view that cannot carry one", owner({ workspace: "data", section: "events", object_type: "event-configuration", object_id: "ecfg_1", tab: "usage", version_id: "ecv_1" }), "version_not_openable"],
    ["a detail view with no item", owner({ workspace: "analyze", section: "renders", tab: "sharing" }), "stray_object_detail"],
    ["a settings page that does not exist", owner({ surface: "global", global_surface: "organization-settings", global_section: "nowhere" }), "unknown_settings_area"],
    ["a surface nobody declared", owner({ surface: "elsewhere", workspace: "analyze", section: "renders" }), "unknown_surface"],
  ])("refuses %s", (_label, reference, code) => {
    const resolved = resolveOwnerReference(reference);
    expect(resolved.kind).toBe("refused");
    if (resolved.kind !== "refused") return;

    expect(resolved.code).toBe(code);
    // A refusal that states a situation and stops is the same dead end as the
    // silence it replaced.
    expect(resolved.reason.length).toBeGreaterThan(20);
    expect(resolved.gesture.label.length).toBeGreaterThan(0);
  });

  it("sends the reader to the nearest screen that does resolve, not always to the Overview", () => {
    const resolved = resolveOwnerReference(REFUSED_FIRST_PUBLICATION_OWNER);
    if (resolved.kind !== "refused") throw new Error("expected a refusal");
    // The workspace and section of that reference WERE sound. Throwing them away
    // would discard the half of the address that was correct.
    expect(resolved.gesture.target).toEqual({ workspace: "data", section: "datastreams" });
    expect(resolved.gesture.label).toBe("Open Data › Datastreams");
  });

  it("falls back to the Project Overview when nothing in the reference resolves", () => {
    const resolved = resolveOwnerReference(owner({ workspace: "nowhere", section: "nowhere" }));
    if (resolved.kind !== "refused") throw new Error("expected a refusal");
    expect(resolved.gesture.target).toEqual({ workspace: "overview", section: "project-overview" });
  });
});

// ---------------------------------------------------------------------------
// Through the real shell: the sentence is SHOWN, and the gesture is followed
// ---------------------------------------------------------------------------

const { navigateMock, routeState } = vi.hoisted(() => ({
  navigateMock: vi.fn(),
  routeState: {
    scope: "project", organizationId: "org-1", projectId: "proj-1",
    workspace: "overview", section: "project-overview",
    lens: null, objectType: null, objectId: null, tab: null, versionId: null, action: null,
    globalSurface: null, globalSection: null,
  },
}));

vi.mock("../shell/router", () => ({
  buildPath: () => "/",
  useRoute: () => ({
    result: { kind: "resolved", route: routeState },
    route: routeState,
    navigate: navigateMock,
  }),
}));

vi.mock("../shell/pages/ProjectOverview", () => ({
  default: ({ onOpenOwner }: { onOpenOwner: (owner: unknown) => void }) => (
    <div>
      <button onClick={() => onOpenOwner(FIRST_PUBLICATION_OWNER)}>Open first publication owner</button>
      <button onClick={() => onOpenOwner(REFUSED_FIRST_PUBLICATION_OWNER)}>Open a reference nothing declares</button>
    </div>
  ),
}));

describe("the shell reports a refused owner reference instead of dropping it", () => {
  beforeEach(() => {
    navigateMock.mockClear();
  });

  it("navigates straight to Renders for the first-publication owner", async () => {
    const user = userEvent.setup();
    render(<><Toaster /><ContentRouter /></>);

    await user.click(await screen.findByRole("button", { name: "Open first publication owner" }));

    expect(navigateMock).toHaveBeenCalledWith({
      globalSurface: null, globalSection: null,
      workspace: "analyze", section: "renders", lens: null,
      objectType: null, objectId: null, tab: null, versionId: null, action: null,
    });
  });

  it("shows a sentence and a gesture when the reference cannot be opened", async () => {
    const user = userEvent.setup();
    render(<><Toaster /><ContentRouter /></>);

    await user.click(await screen.findByRole("button", { name: "Open a reference nothing declares" }));

    // THE DEFECT, INVERTED. Before the repair this click produced nothing at
    // all: no navigation, no message, no trace.
    expect(navigateMock).not.toHaveBeenCalled();
    expect(await screen.findByText("This link could not be opened")).toBeInTheDocument();
    expect(await screen.findByText(/this item cannot do here/i)).toBeInTheDocument();

    // And the gesture is a real destination, not a dismissal.
    await user.click(await screen.findByRole("button", { name: "Open Data › Datastreams" }));
    expect(navigateMock).toHaveBeenCalledWith({
      globalSurface: null, globalSection: null,
      workspace: "data", section: "datastreams", lens: null,
      objectType: null, objectId: null, tab: null, versionId: null, action: null,
    });
  });
});


/**
 * THE HALF OF THE VERSION RULE THAT WAS MISSING, and it crashed rather than
 * refused.
 *
 * `buildPath` refuses to assemble a `/version/{id}` tail under a view the
 * object contract does not declare as version-bearing -- and it refuses by
 * THROWING, inside the click handler, after this resolver had already said the
 * reference was sound. So a link did not go nowhere quietly: it took the click
 * down with it.
 *
 * These are the shapes the server composes TODAY, measured in
 * `evidence_index._OWNER_ROUTES`. Each one names a tab that carries no version
 * history, and each one was handed a version id:
 *
 *   * `event-configuration` + `usage` (Story 49.6 AC9, the Events an AI Path
 *     crossed -- the reference the Knowledge Graph overlay and now the AI Path
 *     workbench both offer);
 *   * `dq-monitor-version` -> `dq-monitor` + `overview`;
 *   * `project-configuration-version` -> `object-version` + `overview`.
 *
 * Dropping the version instead of refusing would be worse than the crash: the
 * reader would land on a DIFFERENT version from the one the evidence pinned,
 * and nothing on the screen would say so.
 */
describe("a version pinned where it cannot be opened is refused, not thrown", () => {
  const PINNED = [
    ["an Event configuration on its usage view", owner({ workspace: "data", section: "events", object_type: "event-configuration", object_id: "ecfg_1", tab: "usage", version_id: "ecv_1" })],
    ["a data-quality monitor on its overview", owner({ workspace: "governance", section: "controls-quality", object_type: "dq-monitor", object_id: "dqm_1", tab: "overview", version_id: "dqv_1" })],
    ["a configuration version on its overview", owner({ workspace: "governance", section: "evidence", object_type: "object-version", object_id: "ov_1", tab: "overview", version_id: "pcv_1" })],
  ] as const;

  it.each(PINNED)("refuses %s", (_label, reference) => {
    const resolved = resolveOwnerReference(reference);
    expect(resolved.kind).toBe("refused");
    if (resolved.kind !== "refused") return;
    expect(resolved.code).toBe("version_not_openable");
    // The gesture keeps the half of the address that WAS sound.
    expect(resolved.gesture.target).not.toBeNull();
  });

  it("still resolves a version on the view that does carry a history", () => {
    // The rule is not "no versions": `semantic-concept` declares `versions`, so
    // the exact version the evidence pinned opens where it is read.
    const resolved = resolveOwnerReference(
      owner({
        workspace: "governance",
        section: "semantic-model",
        object_type: "semantic-concept",
        object_id: "sc_1",
        tab: "versions",
        version_id: "scv_1",
      }),
    );
    expect(resolved.kind).toBe("workspace");
    if (resolved.kind !== "workspace") return;
    expect(resolved.versionId).toBe("scv_1");
    expect(resolved.tab).toBe("versions");
  });
});
