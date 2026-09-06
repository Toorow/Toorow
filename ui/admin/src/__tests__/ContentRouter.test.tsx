import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ContentRouter, { collectionOwnerKey } from "../shell/ContentRouter";
import { WORKSPACES } from "../shell/navigation";
import exactFixture from "./fixtures/feedbackReviewExact.json";
import regressionFixture from "./fixtures/feedbackRegressionExact.json";

const REGRESSION_OPTIONS = {
  business_domains: [], business_classifications: [], semantic_view_versions: [],
  semantic_view_version_roles: [], result_types: [], severities: [], lifecycles: [],
  lifecycle_transitions: {}, assertion_types: [], tolerance_kinds: [],
  provenance_link_kinds: ["semantic_view"], path_step_kinds: [],
  path_owner_workspaces: [], reference_path_roles: [], capability_rule: "",
};

const { navigateMock, routeState } = vi.hoisted(() => ({
  navigateMock: vi.fn(),
  routeState: {
    scope: "project", organizationId: "org-1", projectId: "proj-1", workspace: "overview", section: "project-overview",
    lens: null, objectType: null, objectId: null, tab: null, versionId: null, action: null,
    globalSurface: null, globalSection: null,
  },
}));

// `result` is not decoration: ContentRouter reads it BEFORE `route`, because an
// unresolved address must not be served by the Account placeholder `route`
// falls back to. A mock that omits it would hide that gate.
vi.mock("../shell/router", () => ({
  buildPath: () => "/",
  useRoute: () => ({
    result: { kind: "resolved", route: routeState },
    route: routeState,
    navigate: navigateMock,
  }),
}));
vi.mock("../shell/pages/ProjectOverview", () => ({
  default: ({ onOpenOwner }: { onOpenOwner: (owner: unknown) => void }) => <div>
    <button onClick={() => onOpenOwner({
      surface: "project", workspace: "data", section: "datastreams", global_surface: null, global_section: null,
      object_type: "datastream", object_id: "ds-1", tab: "runs", action: null, version_id: null, evidence_id: "ev-1",
    })}>Open project owner</button>
    <button onClick={() => onOpenOwner({
      surface: "global", workspace: null, section: null, global_surface: "project-settings", global_section: "capabilities",
      object_type: null, object_id: null, tab: null, action: null, version_id: null, evidence_id: null,
    })}>Open global owner</button>
    {/* The Organization is a ratified object owned by Organization Settings
        (`README.md:94`), and `project-settings.md:43` requires the association
        to carry a link to that owner. Before this button existed, `openOwner`
        accepted `project-settings` alone and DROPPED every other global owner in
        silence — so the link was not merely unbuilt, it was unbuildable. */}
    <button onClick={() => onOpenOwner({
      surface: "global", workspace: null, section: null, global_surface: "organization-settings", global_section: "general",
      object_type: null, object_id: null, tab: null, action: null, version_id: null, evidence_id: null,
    })}>Open organization owner</button>
    <button onClick={() => onOpenOwner({
      surface: "global", workspace: null, section: null, global_surface: "organization-settings", global_section: "not-a-section",
      object_type: null, object_id: null, tab: null, action: null, version_id: null, evidence_id: null,
    })}>Open invalid organization section</button>
  </div>,
}));

// The fleet screen, reduced to the one thing this file is the authority on:
// whether the caller hands it the create route. `DataWorkspace` draws
// "+ Add Datastream" only when `onAddDatastream` is supplied — not supplied
// means not drawn, rather than drawn and inert — so a caller that forgets the
// prop removes the action silently, and the component's own suite stays green
// because it hands itself the callback.
vi.mock("../shell/pages/DataWorkspace", () => ({
  default: ({ onAddDatastream }: { onAddDatastream?: () => void }) => (
    <div>
      {onAddDatastream
        ? <button onClick={onAddDatastream}>+ Add Datastream</button>
        : <span>the caller owns no create route</span>}
    </div>
  ),
}));

beforeEach(() => {
  navigateMock.mockClear();
  Object.assign(routeState, {
    projectId: "proj-1",
    workspace: "overview", section: "project-overview", objectType: null, objectId: null,
    tab: null, versionId: null, action: null, globalSurface: null, globalSection: null,
  });
  vi.stubGlobal("fetch", vi.fn(async (input: string, init?: RequestInit) => {
    const url = String(input);
    const body = url.endsWith(`/evaluation-runs/${regressionFixture.evaluation_receipt.run_id}/cases`)
      ? { run_id: regressionFixture.evaluation_receipt.run_id, cases: [regressionFixture.evaluation_case] }
      : url.endsWith(`/evaluation-cases/${regressionFixture.evaluation_case.id}/evaluate`) && init?.method === "POST"
        ? regressionFixture.evaluation_receipt
        : url.endsWith(`/feedback/${regressionFixture.scope.feedback_id}/regression-resolution`) && init?.method === "POST"
          ? regressionFixture.resolution_receipt
          : url.endsWith("/regression-draft")
      ? regressionFixture.draft
      : url.endsWith("/regression-cases") && init?.method === "POST"
        ? regressionFixture.create_receipt
        : url.endsWith("/golden-questions/options")
          ? REGRESSION_OPTIONS
          : url.endsWith(`/golden-questions/${regressionFixture.golden_question.id}`)
            ? regressionFixture.golden_question
            : url.includes("/feedback/aggregates")
      ? exactFixture.aggregates
      : url.includes("/feedback/critical-negatives")
        ? exactFixture.critical_negatives
        : /\/feedback\/[^?]+$/.test(url)
          ? exactFixture.detail
          : exactFixture.collection;
    return { ok: true, status: 200, json: async () => body } as Response;
  }));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const EXPECTED = [
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
];

it("assigns one explicit content owner to every canonical collection route", () => {
  const actual = WORKSPACES.flatMap((workspace) =>
    workspace.subnav.map((section) =>
      collectionOwnerKey({ workspace: workspace.key, section: section.slug }),
    ),
  );
  expect(actual).toEqual(EXPECTED);
  expect(actual).not.toContain(null);
});

it("does not turn unregistered routes into implicit fallbacks", () => {
  expect(collectionOwnerKey({ workspace: "data", section: "add" })).toBeNull();
  expect(collectionOwnerKey({ workspace: "overview", section: "getting-started" })).toBeNull();
  expect(collectionOwnerKey({ workspace: "context-hub", section: "knowledge" })).toBeNull();
});

it("translates project owner references into one exact canonical navigation", async () => {
  const user = userEvent.setup();
  render(<ContentRouter />);
  await user.click(await screen.findByRole("button", { name: "Open project owner" }));
  expect(navigateMock).toHaveBeenCalledWith({
    globalSurface: null, globalSection: null, workspace: "data", section: "datastreams",
    // `lens: null` since story 58.9 — and it is CLEARED, not merely absent. A
    // reference that named no lens used to inherit the one on the current route,
    // so opening an owner in another section carried the previous section's lens
    // into an address the router then refuses.
    lens: null,
    objectType: "datastream", objectId: "ds-1", tab: "runs", versionId: null, action: null,
  });
});

it("translates global owner references into the exact Project Settings section", async () => {
  const user = userEvent.setup();
  render(<ContentRouter />);
  await user.click(await screen.findByRole("button", { name: "Open global owner" }));
  expect(navigateMock).toHaveBeenCalledWith({
    globalSurface: "project-settings", globalSection: "capabilities", objectType: null,
    objectId: null, tab: null, versionId: null, action: null,
  });
});

it("opens the Organization owner, which every global reference but one used to lose", async () => {
  const user = userEvent.setup();
  render(<ContentRouter />);
  await user.click(await screen.findByRole("button", { name: "Open organization owner" }));
  expect(navigateMock).toHaveBeenCalledWith({
    globalSurface: "organization-settings", globalSection: "general", objectType: null,
    objectId: null, tab: null, versionId: null, action: null,
  });
});

it("still refuses a global section the Organization shell cannot resolve", async () => {
  // Widening the gate is not opening it: an owner reference is untrusted input,
  // and a section this shell does not know would navigate to a screen that
  // cannot resolve. Refusing is the correct outcome, and it must stay tested —
  // that is the difference between accepting a valid owner and accepting any.
  const user = userEvent.setup();
  render(<ContentRouter />);
  await user.click(await screen.findByRole("button", { name: "Open invalid organization section" }));
  expect(navigateMock).not.toHaveBeenCalled();
});

/**
 * Story 50.4 AC12 -- the Visualization Builder is MOUNTED, on the address the
 * Result workbench sends people to.
 *
 * `grep -rn "visualization" src/__tests__/ContentRouter.test.tsx` returned nothing
 * before this: the branch existed in `ContentRouter.tsx` and nothing asserted that
 * it was reached, so a reordering or a renamed object type would have shown the
 * Unknown screen with every test green.
 *
 * The Builder is lazy-loaded, so it is mocked here: what is under test is the
 * ROUTING decision and the props the branch passes, not the Builder's own render.
 */
vi.mock("../analyze/builder/VisualizationBuilder", () => ({
  default: (props: Record<string, unknown>) => (
    <div data-testid="visualization-builder">
      <span data-testid="builder-visualization-id">{String(props.visualizationId)}</span>
      <span data-testid="builder-tab">{String(props.tab)}</span>
      <span data-testid="builder-result-id">{String(props.resultId)}</span>
      <span data-testid="builder-query-spec-version-id">{String(props.querySpecVersionId)}</span>
      <span data-testid="builder-spec-version-id">{String(props.visualizationSpecVersionId)}</span>
    </div>
  ),
}));

vi.mock("../analyze/QuerySpecWorkbench", () => ({
  default: (props: Record<string, unknown>) => (
    <div data-testid="query-spec-workbench">
      <span data-testid="qs-id">{String(props.querySpecId)}</span>
      <span data-testid="qs-version-id">{String(props.querySpecVersionId)}</span>
    </div>
  ),
}));

describe("Visualization Builder mount (Story 50.4)", () => {
  function openBuilder(overrides: Record<string, unknown> = {}) {
    Object.assign(routeState, {
      workspace: "analyze",
      section: "explore",
      objectType: "visualization",
      objectId: "vis_1",
      tab: "build",
      versionId: null,
      action: null,
      query: {},
      ...overrides,
    });
  }

  it("renders the Builder for objectType visualization, not the Unknown screen", async () => {
    openBuilder();
    render(<ContentRouter />);
    expect(await screen.findByTestId("visualization-builder")).toBeInTheDocument();
    expect(screen.getByTestId("builder-visualization-id")).toHaveTextContent("vis_1");
    expect(screen.getByTestId("builder-tab")).toHaveTextContent("build");
  });

  it("passes the declared result_id pin straight through", async () => {
    openBuilder({ query: { result_id: "qr_1" } });
    render(<ContentRouter />);
    expect(await screen.findByTestId("builder-result-id")).toHaveTextContent("qr_1");
  });

  it("renders the versions tab through the same mount", async () => {
    openBuilder({ tab: "versions" });
    render(<ContentRouter />);
    expect(await screen.findByTestId("builder-tab")).toHaveTextContent("versions");
  });
});

/**
 * A version-pinned address is the whole of `README.md` invariant 7: "Every
 * projection deep-links to the exact owner, object and version."
 *
 * `router.tsx:200` has always RESOLVED these addresses for any contract with a
 * version-bearing tab. `ContentRouter` refused every one of them outside
 * Governance and Test, because the guard was placed above the Analyze branch —
 * so `VisualizationBuilder`'s `visualizationSpecVersionId` prop and
 * `QuerySpecWorkbench`'s `querySpecVersionId` prop could never receive a value.
 * Nothing failed: the refusal screen is a legitimate screen, and no test asked
 * for the version to arrive.
 *
 * These three cases pin the rule in both directions — the two types that read a
 * version must reach their workbench WITH it, and a type that does not must
 * still be refused BY NAME rather than served the current version silently.
 */
describe("Version-pinned addresses reach the workbenches that read them", () => {
  function openPinned(overrides: Record<string, unknown>) {
    Object.assign(routeState, {
      workspace: "analyze",
      section: "explore",
      tab: null,
      action: null,
      query: {},
      ...overrides,
    });
  }

  it("passes the pinned Visualization Spec version to the Builder", async () => {
    openPinned({ objectType: "visualization", objectId: "vis_1", tab: "build", versionId: "vsv_1" });
    render(<ContentRouter />);
    expect(await screen.findByTestId("visualization-builder")).toBeInTheDocument();
    expect(screen.getByTestId("builder-spec-version-id")).toHaveTextContent("vsv_1");
  });

  it("passes the pinned Query Spec version to its workbench", async () => {
    openPinned({ objectType: "query-spec", objectId: "qs_1", tab: "query", versionId: "qsv_1" });
    render(<ContentRouter />);
    expect(await screen.findByTestId("query-spec-workbench")).toBeInTheDocument();
    expect(screen.getByTestId("qs-version-id")).toHaveTextContent("qsv_1");
  });

  it.each([
    ["context-topic", "knowledge-library", "topic"],
    ["context-procedure", "skills-registry", "procedure"],
    ["skill", "skills-registry", "procedure"],
  ])("passes the pinned version of %s to the Context Hub workbench", async (objectType, section, kind) => {
    // Story 49.6 — these three were in the refused inventory beside `report`:
    // their ledger listed versions and the address that pins one answered
    // "the context-topic workbench does not open a pinned version yet". An AI
    // Path step pins a governed Skill version, and that pin had nowhere to land.
    openPinned({
      workspace: "context-hub", section,
      objectType, objectId: "obj_EXAMPLE", tab: "versions", versionId: "2",
    });
    render(<ContentRouter />);
    expect(await screen.findByText(`context object workbench: ${kind} / obj_EXAMPLE`)).toBeTruthy();
    expect(screen.getByTestId("context-object-version")).toHaveTextContent("2");
  });

  it("still refuses a type whose workbench reads no version, and names it", async () => {
    openPinned({ section: "reports", objectType: "report", objectId: "rep_1", tab: "versions", versionId: "rv_1" });
    render(<ContentRouter />);
    // Named, not generic: "no workbench outside Governance" was true when it was
    // written and became false without the sentence changing.
    expect(await screen.findByText(/report workbench does not open a pinned version/i)).toBeInTheDocument();
  });
});

/** Lot 2 of `SCREEN-FUNCTION-MATRIX.md` names `data/datastreams` the ENTRY of
 *  "create and publish a Datastream", and `data.md:57` fixes creation as the
 *  Add Datastream action. Both were unreachable by click: the wizard route
 *  existed, `ui/admin/src/shell/navigation.ts#WORKSPACES` declared the action, the button component was
 *  built and unit-tested — and the one production call site passed only
 *  `onOpenDatastream`. Four review lenses found it independently on 2026-08-03.
 *
 *  This asserts the WIRE, which is the part no component suite can see. */
describe("the collection owns its create route", () => {
  it("draws + Add Datastream and sends it to the five-stage wizard", async () => {
    Object.assign(routeState, {
      workspace: "data", section: "datastreams",
      objectType: null, objectId: null, tab: null, versionId: null, action: null,
    });
    render(<ContentRouter />);
    await userEvent.click(await screen.findByRole("button", { name: /\+ Add Datastream/i }));
    expect(navigateMock).toHaveBeenCalledWith(expect.objectContaining({
      workspace: "data", section: "datastreams", action: "create",
      objectType: null, objectId: null,
    }));
  });
});


/** Every Test collection opens its objects.
 *
 *  All three accept an open callback and the mount site passed NONE of them, so
 *  every run id, trace id, question and feedback row was inert text and the four
 *  workbenches behind them were reachable only by typing an address. Two review
 *  lenses found it at the same file:line; the third and fourth collection had
 *  the same defect and were not in that report.
 *
 *  Same shape as the "+ Add Datastream" wire on `data/datastreams`, found the
 *  same day on another workspace: a component builds its affordance
 *  conditionally, the caller forgets the prop, and the component's own suite
 *  stays green because it hands itself the callback. Only the CALLER can be
 *  asserted here. */
describe("the Test collections open their objects", () => {
  const CASES: ReadonlyArray<[string, string, string]> = [
    ["golden-questions", "golden-question", "Open golden question"],
    ["regression-runs", "evaluation-run", "Open evaluation run"],
    ["regression-runs", "trace-observation", "Open trace observation"],
    ["widget-feedback", "feedback-review", "Open feedback"],
  ];

  it.each(CASES)("wires %s -> %s", (section, objectType) => {
    const source = readSource();
    // The wire is asserted at the mount site, which is the half no component
    // test can see.
    expect(source).toContain(`openTestObject("${section}", "${objectType}")`);
  });

  it("lets the router canonicalize the tab rather than naming one here", () => {
    // `navigation.ts` DECLARES a `defaultTab` per contract. Naming one at the
    // call site would duplicate a decision already taken and drift when it
    // changes.
    const source = readSource();
    const helper = source.slice(source.indexOf("const openTestObject"));
    expect(helper.slice(0, 400)).toContain("tab: null");
  });

  it("opens the Feedback Review Workbench from the real Widget Feedback mount", async () => {
    Object.assign(routeState, {
      workspace: "test",
      section: "widget-feedback",
      objectType: null,
      objectId: null,
      tab: null,
      versionId: null,
      action: null,
    });
    render(<ContentRouter />);
    const annotations = await screen.findByRole("region", { name: "Feedback annotations" });
    await userEvent.click(within(annotations).getByRole("button", { name: exactFixture.collection.items[0].comment }));
    expect(navigateMock).toHaveBeenCalledWith({
      workspace: "test",
      section: "widget-feedback",
      objectType: "feedback-review",
      objectId: exactFixture.collection.items[0].id,
      tab: null,
      versionId: null,
      action: null,
    });
  });
});

describe("the feedback workbench opens exact evidence owners", () => {
  beforeEach(() => {
    Object.assign(routeState, {
      workspace: "test",
      section: "widget-feedback",
      objectType: "feedback-review",
      objectId: exactFixture.detail.id,
      tab: "feedback",
      versionId: null,
      action: null,
    });
  });

  it.each([
    ["explore", "result", exactFixture.detail.result.id],
    ["renders", "render", exactFixture.detail.render?.render_id],
  ])("routes the fixture-backed %s owner through the real ContentRouter gate", async (section, objectType, objectId) => {
    render(<ContentRouter />);
    const owners = await screen.findByRole("region", { name: "Owner links" });
    const row = within(owners).getByText(`${objectType} ${String(objectId)}`).closest("tr");
    expect(row).not.toBeNull();
    await userEvent.click(within(row as HTMLElement).getByRole("button", { name: "Open owner" }));
    expect(navigateMock).toHaveBeenCalledWith({
      globalSurface: null,
      globalSection: null,
      workspace: "analyze",
      section,
      lens: null,
      objectType,
      objectId,
      tab: null,
      versionId: null,
      action: null,
    });
  });

  it("routes every server-emitted owner link through its declared real object contract", async () => {
    const routeTabs: Record<string, string | null> = {
      "analyze/explore/result": null,
      "analyze/explore/query-spec": "query",
      "governance/semantic-model/semantic-view": "versions",
      "governance/master-data/business-domain": "versions",
      "analyze/renders/render": null,
      "test/regression-runs/trace-observation": "timeline",
    };
    render(<ContentRouter />);
    const owners = await screen.findByRole("region", { name: "Owner links" });
    for (const link of exactFixture.detail.owner_links) {
      const routeKey = `${link.workspace}/${link.section}/${link.object_type}`;
      expect(routeTabs, `${routeKey} must be a mounted ContentRouter object contract`).toHaveProperty(routeKey);
      expect("tab" in link ? link.tab : null).toBe(routeTabs[routeKey]);
      const row = within(owners)
        .getByText(`${link.object_type} ${link.object_id}${link.version_id ? ` v${link.version_id}` : ""}`)
        .closest("tr");
      expect(row).not.toBeNull();
      await userEvent.click(within(row as HTMLElement).getByRole("button", { name: "Open owner" }));
      expect(navigateMock).toHaveBeenCalledWith({
        globalSurface: null,
        globalSection: null,
        workspace: link.workspace,
        section: link.section,
        lens: null,
        objectType: link.object_type,
        objectId: link.object_id,
        tab: "tab" in link ? link.tab : null,
        versionId: link.version_id,
        action: null,
      });
    }
  });

  it("routes the fixture-backed automated verdict owner through the real workbench control", async () => {
    const link = exactFixture.detail.automated_verdicts.items[0].owner_link;
    expect(`${link.workspace}/${link.section}/${link.object_type}`).toBe(
      "test/regression-runs/evaluation-run",
    );
    expect("tab" in link ? link.tab : null).toBeNull();
    Object.assign(routeState, { tab: "classification" });
    render(<ContentRouter />);
    await userEvent.click(await screen.findByRole("button", { name: "Open evaluation owner" }));
    expect(navigateMock).toHaveBeenCalledWith({
      globalSurface: null,
      globalSection: null,
      workspace: link.workspace,
      section: link.section,
      lens: null,
      objectType: link.object_type,
      objectId: link.object_id,
      tab: "tab" in link ? link.tab : null,
      versionId: link.version_id,
      action: null,
    });
  });

  it("routes the AI Path to Test / Regression Runs / Trace Observation", async () => {
    Object.assign(routeState, { tab: "ai-path" });
    render(<ContentRouter />);
    await userEvent.click(await screen.findByRole("button", { name: "Open the Trace Observation" }));
    expect(navigateMock).toHaveBeenCalledWith({
      workspace: "test",
      section: "regression-runs",
      objectType: "trace-observation",
      objectId: exactFixture.detail.ai_path,
      tab: null,
      versionId: null,
      action: null,
    });
  });
});

describe("the feedback regression door uses the server-generated owner contract", () => {
  it("opens Golden Question Definition from the real Resolution acknowledgement", async () => {
    Object.assign(routeState, {
      projectId: regressionFixture.scope.project_id,
      workspace: "test",
      section: "widget-feedback",
      objectType: "feedback-review",
      objectId: regressionFixture.scope.feedback_id,
      tab: "resolution",
      versionId: null,
      action: null,
    });
    render(<ContentRouter />);
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText(/^Title/), regressionFixture.create_command.title);
    await user.type(screen.getByLabelText(/^Owner\*/), regressionFixture.create_command.owner);
    await user.type(screen.getByLabelText(/^Reproduction reason/), regressionFixture.create_command.reproduction_reason);
    await user.type(screen.getByLabelText(/^Business question/), regressionFixture.create_command.question);
    await user.click(screen.getByRole("button", { name: "Add assertion" }));
    await user.selectOptions(screen.getByLabelText(/^Assertion 1 type/), "cardinality");
    await user.type(screen.getByLabelText(/^Assertion 1 expected cardinality/), "3");
    await user.selectOptions(screen.getByLabelText("Requirement for semantic_view"), "required");
    await user.click(screen.getByTestId("feedback-regression-create"));
    await user.click(await screen.findByRole("button", { name: "Open Golden Question Definition" }));

    const link = regressionFixture.create_receipt.owner_links[0];
    expect(navigateMock).toHaveBeenCalledWith({
      globalSurface: null,
      globalSection: null,
      workspace: link.workspace,
      section: link.section,
      lens: null,
      objectType: link.object_type,
      objectId: link.object_id,
      tab: link.tab,
      versionId: link.version_id,
      action: null,
    });
  });

  it("serves the generated pinned v2 Definition at its direct URL", async () => {
    Object.assign(routeState, {
      projectId: regressionFixture.scope.project_id,
      workspace: "test",
      section: "golden-questions",
      objectType: "golden-question",
      objectId: regressionFixture.golden_question.id,
      tab: "definition",
      versionId: regressionFixture.create_receipt.golden_question_version_id,
      action: null,
    });
    render(<ContentRouter />);
    expect(await screen.findByText(regressionFixture.golden_question.title)).toBeInTheDocument();
    expect(screen.getByText("Pinned immutable Golden Question version")).toBeInTheDocument();
    expect(screen.getByLabelText(/^Business question/)).toHaveValue(
      regressionFixture.golden_question.current_version.question,
    );
    expect(screen.queryByTestId("golden-question-save")).not.toBeInTheDocument();
  });

  it("evaluates and resolves from the direct Evaluation Run Cases route, then opens the exact owner", async () => {
    Object.assign(routeState, {
      projectId: regressionFixture.scope.project_id,
      workspace: "test",
      section: "regression-runs",
      objectType: "evaluation-run",
      objectId: regressionFixture.evaluation_receipt.run_id,
      tab: "cases",
      versionId: null,
      action: null,
    });
    render(<ContentRouter />);
    const user = userEvent.setup();
    await user.click(await screen.findByTestId(
      `feedback-regression-evaluate-${regressionFixture.evaluation_case.id}`,
    ));
    await screen.findByText("Trusted evaluation recorded");
    await user.click(screen.getByTestId(
      `feedback-regression-resolve-${regressionFixture.evaluation_case.id}`,
    ));
    await screen.findByText("Feedback regression resolved");
    const labels = ["Open Evaluation Run", "Review Evaluation Case", "Review trusted Verdict"];
    for (const [index, owner] of regressionFixture.resolution_receipt.owner_links.entries()) {
      await user.click(screen.getByRole("button", { name: labels[index] }));
      expect(navigateMock).toHaveBeenLastCalledWith({
        globalSurface: null,
        globalSection: null,
        workspace: owner.workspace,
        section: owner.section,
        lens: null,
        objectType: owner.object_type,
        objectId: owner.object_id,
        tab: owner.tab,
        versionId: owner.version_id,
        action: null,
      });
    }
  });

  it.each([
    [1, "Focused Evaluation Case"],
    [2, "Focused evaluation Verdict"],
  ])("opens receipt owner %i as exact focused evidence in the real run workbench", async (ownerIndex, title) => {
    const owner = regressionFixture.resolution_receipt.owner_links[ownerIndex];
    Object.assign(routeState, {
      projectId: regressionFixture.scope.project_id,
      workspace: owner.workspace,
      section: owner.section,
      objectType: owner.object_type,
      objectId: owner.object_id,
      tab: owner.tab,
      versionId: owner.version_id,
      action: null,
    });
    render(<ContentRouter />);
    const focus = await screen.findByTestId(`evaluation-focus-${owner.version_id}`);
    expect(focus).toHaveTextContent(title);
    expect(focus).toHaveTextContent(String(owner.version_id));
  });
});

function readSource(): string {
  // AD-42 (2026-08-12) : les deux tables de dispatch ont quitte ContentRouter,
  // qui n assemble plus que les surfaces globales et la garde de portee. Un
  // garde de source doit suivre le CODE, pas le chemin -- lire les trois
  // fichiers ensemble garde ces cas vrais ou qu une branche vive.
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const { readFileSync } = require("node:fs");
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const { resolve } = require("node:path");
  return ["ContentRouter.tsx", "objectSurfaces.tsx", "collectionSurfaces.tsx"]
    .map((file) => readFileSync(resolve(__dirname, `../shell/${file}`), "utf-8"))
    .join("\n");
}

/**
 * `skill` is the CANONICAL noun (`glossary.md`) for the object the wire and the
 * database still call `context_procedure`. `navigation.ts` declared it under
 * Skills Registry and no branch answered it, so
 * `/context-hub/skills-registry/object/skill/:id` rendered `unavailable` — and
 * an AI Path step recording `owner_object_type: "skill"` (migration 150) had no
 * reachable target.
 *
 * Deleting the declaration would have removed the dead screen by breaking the
 * link instead. The branch serves it, from the same workbench as
 * `context-procedure`, because it is the same object.
 *
 * `ContextObjectPage` is mocked: what is under test is the ROUTING decision and
 * the props the branch passes, not that page's own render.
 */
vi.mock("../ContextObjectPage", () => ({
  default: ({ kind, objectId, versionId }: { kind: string; objectId: string | null; versionId?: string | null }) => (
    <div>
      context object workbench: {kind} / {objectId}
      <span data-testid="context-object-version">{versionId ?? "none"}</span>
    </div>
  ),
}));

describe("the Skills Registry serves its canonical noun", () => {
  it.each([
    ["context-procedure", "procedure"],
    ["skill", "procedure"],
    ["context-topic", "topic"],
  ])("routes %s to the %s workbench", async (objectType, kind) => {
    Object.assign(routeState, {
      workspace: "context-hub",
      section: objectType === "context-topic" ? "knowledge-library" : "skills-registry",
      objectType,
      objectId: "obj_EXAMPLE",
      tab: "content",
      versionId: null,
      action: null,
    });
    render(<ContentRouter />);
    expect(
      await screen.findByText(`context object workbench: ${kind} / obj_EXAMPLE`),
    ).toBeTruthy();
  });

  it("keeps `skill` declared in the registry, because a recorded step points at it", () => {
    const registry = WORKSPACES.find((workspace) => workspace.key === "context-hub");
    const section = registry?.subnav.find((candidate) => candidate.slug === "skills-registry");
    const types = section?.objects.map((object) => object.type) ?? [];
    expect(types).toContain("skill");
    expect(types).toContain("context-procedure");
  });
});
