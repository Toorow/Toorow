/**
 * Editing and retiring a governed Semantic Model object — 2026-08-18.
 *
 * WHAT WAS MEASURED BEFORE THIS FILE EXISTED. `_SUPPORTED_INTENTS`
 * (`server/core/semantic_model.py:465`) has accepted five actions since the
 * change set existed; the console sent two of them, both creations. So a
 * Concept published with the wrong additivity class was permanent, and no
 * screen in the product could retire a Semantic View nobody uses.
 * `GovernanceObjectWorkbench` read `allowed_actions` for exactly one value,
 * `explore-data`.
 *
 * WHAT THESE TESTS REFUSE TO BE FOOLED BY. A button that appears because the
 * console decided it should. Every gesture below appears only because the
 * fixture's `allowed_actions` declared it, and the undeclared case asserts
 * NOTHING renders — a console-side affordance for a command the owner did not
 * declare is how a screen offers what the server will refuse.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import GovernanceObjectWorkbench from "../governance/GovernanceObjectWorkbench";
import { useGovernanceObject } from "../governance/governanceSurface";

const PROJECT = "proj_EXAMPLE";
const CHANGE_SET = "scs_01EXAMPLE0000000000000000";

const ROUTE = {
  scope: "project",
  organizationId: "org_EXAMPLE",
  projectId: PROJECT,
  workspace: "governance",
  section: "semantic-model",
  lens: null,
  objectType: "semantic-concept",
  objectId: "sc_EXAMPLE",
  tab: "definition",
  versionId: null,
  evidenceId: null,
  action: null,
  query: {},
  globalSurface: null,
  globalSection: null,
};

vi.mock("../shell/router", () => ({
  useRoute: () => ({ route: ROUTE, navigate: vi.fn(), replace: vi.fn(), result: { kind: "resolved", route: ROUTE } }),
  buildPath: () => "/org/org_EXAMPLE/project/proj_EXAMPLE/governance/semantic-model",
}));

vi.mock("../governance/governanceSurface", () => ({
  useGovernanceObject: vi.fn(),
}));

/** The two dialogs are stubbed so this file asserts the WIRING — which dialog
 *  opens, on which object, from which base version. What each dialog then sends
 *  is asserted in `CalculatedFields` and `NewSemanticViewDialog`. */
vi.mock("../governance/NewConceptDialog", () => ({
  default: ({ open, edit }: { open: boolean; edit?: { objectId: string; baseVersionId: string } | null }) =>
    open ? <div>Concept dialog on {edit?.objectId} from {edit?.baseVersionId}</div> : null,
}));
vi.mock("../governance/NewSemanticViewDialog", () => ({
  default: ({ open, edit }: { open: boolean; edit?: { objectId: string; baseVersionId: string } | null }) =>
    open ? <div>View dialog on {edit?.objectId} from {edit?.baseVersionId}</div> : null,
}));

const mockedObject = vi.mocked(useGovernanceObject);
const reload = vi.fn();

interface Posted {
  url: string;
  body: Record<string, unknown>;
}

/** The archive lifecycle over the real `apiPost` seam.
 *
 *  `prepare` is configurable because the three answers it can give are the three
 *  states of this dialog: publishable (a token, and a second click retires),
 *  refused by a named `live_consumers` (`semantic_model.py` refuses when a live
 *  Semantic View still pins the object), or blocked by the Test gate — which is
 *  what EVERY retirement meets first, since Test records no verdict for a change
 *  set that publishes no version. */
function stubFetch(
  options: {
    prepare?: {
      publishable?: boolean;
      refusals?: Array<{ code: string; message: string; path?: string }>;
      gate?: { state: string; message: string };
    };
  } = {},
) {
  const posted: Posted[] = [];
  const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    const address = String(url);
    if (init?.method === "POST") {
      posted.push({ url: address, body: JSON.parse(String(init.body ?? "{}")) });
    }
    if (address.endsWith("/change-sets")) {
      return Promise.resolve({ ok: true, status: 201, json: async () => ({ change_set_id: CHANGE_SET }) });
    }
    if (address.endsWith("/prepare")) {
      const prepare = options.prepare ?? {};
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          // ALWAYS minted, refused or not: `publishable` is the field that answers.
          confirmation_token: "token-EXAMPLE",
          refusals: prepare.refusals ?? [],
          validation: {
            publishable: prepare.publishable ?? true,
            refusals: prepare.refusals ?? [],
            test_gate: prepare.gate ?? { state: "pass" },
            live_consumers: [],
            used_by_impact: {
              state: "available",
              groups: [
                { owner: "governance/semantic-model", label: "Semantic Views", state: "available", count: 2 },
                {
                  owner: "context-hub/knowledge-library",
                  label: "Knowledge and Skills",
                  state: "unavailable",
                  count: null,
                  reason: "The Knowledge and Skills relationship adapter is owned by Story 49.6 and is not delivered.",
                },
              ],
            },
          },
        }),
      });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
  });
  vi.stubGlobal("fetch", fetchMock);
  return posted;
}

function detail(overrides: Record<string, unknown> = {}) {
  return {
    object_ref: {
      type: "semantic-concept",
      id: "sc_EXAMPLE",
      label: "Gross revenue",
      owner_href: { surface: "project", workspace: "governance", section: "semantic-model" },
    },
    scope: "project",
    owner: { kind: "semantic-model", project_id: PROJECT },
    lifecycle_status: "active",
    active_version_ref: { object_type: "semantic-concept-version", id: "scv_EXAMPLE", version: 3, state: "published" },
    selected_version_ref: null,
    available_tabs: ["definition", "used-by", "versions"],
    default_tab: "definition",
    allowed_actions: [],
    used_by: { state: "available", count: 2, refs: [{ label: "Monthly sales", kind: "semantic-view" }], truncated: false },
    versions: { state: "empty", count: 0, refs: [], truncated: false },
    evidence: { state: "empty", count: 0, refs: [], truncated: false },
    summary: { concept_kind: "metric", additivity_class: "additive" },
    evidence_as_of: null,
    ...overrides,
  };
}

function mount(objectType: string, object: Record<string, unknown> | null) {
  mockedObject.mockReturnValue({
    state: {
      status: "ready",
      envelope: {
        schema_version: "governance-object.v1",
        project_ref: { object_type: "project", id: PROJECT },
        organization_ref: { object_type: "organization", id: "org_EXAMPLE" },
        section: "semantic-model",
        object_type: objectType,
        generated_at: "2026-08-18T00:00:00Z",
        evidence_as_of: null,
        state: "available",
        object,
        unavailable_reasons: [],
      },
    },
    reload,
  } as never);
  return render(
    <GovernanceObjectWorkbench
      projectId={PROJECT}
      section="semantic-model"
      objectType={objectType}
      objectId="sc_EXAMPLE"
      tab="definition"
      versionId={null}
    />,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

// ---------------------------------------------------------------------------
// The owner declares; the console renders
// ---------------------------------------------------------------------------

it("renders no action region at all when the owner declared none", () => {
  stubFetch();
  mount("semantic-concept", detail());
  expect(screen.queryByTestId("semantic-model-actions")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Edit this/i })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Retire this/i })).not.toBeInTheDocument();
});

it("opens the Concept dialog on the exact object and base version the owner declared editable", async () => {
  stubFetch();
  mount("semantic-concept", detail({ allowed_actions: ["edit_concept"] }));

  expect(screen.queryByRole("button", { name: /Retire this/i })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /Edit this/i }));
  expect(await screen.findByText(/Concept dialog on sc_EXAMPLE from scv_EXAMPLE/)).toBeInTheDocument();
});

it("does not open a Concept dialog for an action declared on another object type", () => {
  stubFetch();
  // `edit_view` on a Concept: the vocabulary is the server's, and the type it
  // belongs to is not this one. Offering the button would open a dialog that
  // composes the wrong intent.
  mount("semantic-concept", detail({ allowed_actions: ["edit_view"] }));
  expect(screen.queryByRole("button", { name: /Edit this/i })).not.toBeInTheDocument();
});

it("says why there is no gesture when the object has no published version", () => {
  stubFetch();
  mount("semantic-concept", detail({ allowed_actions: ["edit_concept", "archive_object"], active_version_ref: null }));
  expect(screen.getByText(/No published version to change/i)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Edit this/i })).not.toBeInTheDocument();
});

// ---------------------------------------------------------------------------
// Retiring names what would be left pointing at it
// ---------------------------------------------------------------------------

it("names the object and counts its consumers before anything is retired", async () => {
  const posted = stubFetch();
  mount("semantic-concept", detail({ allowed_actions: ["archive_object"] }));

  await userEvent.click(screen.getByRole("button", { name: /Retire this/i }));
  expect(await screen.findByText(/Retire Gross revenue\?/)).toBeInTheDocument();
  const evidence = screen.getByTestId("archive-consumer-evidence");
  expect(within(evidence).getByText(/2 consumer\(s\) still point at this/i)).toBeInTheDocument();
  expect(within(evidence).getByText("Monthly sales")).toBeInTheDocument();
  // Reading the dialog sends nothing: the count is the object's own facet.
  expect(posted).toHaveLength(0);
});

it("distinguishes an unreadable used-by from a count of zero", async () => {
  stubFetch();
  mount(
    "semantic-concept",
    detail({
      allowed_actions: ["archive_object"],
      used_by: { state: "unavailable", count: 0, refs: [], truncated: false },
    }),
  );

  await userEvent.click(screen.getByRole("button", { name: /Retire this/i }));
  const evidence = await screen.findByTestId("archive-consumer-evidence");
  expect(within(evidence).getByText(/No owner could answer/i)).toBeInTheDocument();
  expect(within(evidence).queryByText(/the answer is none/i)).not.toBeInTheDocument();
});

it("measures first and retires on a second, deliberate click", async () => {
  const posted = stubFetch();
  mount("semantic-concept", detail({ allowed_actions: ["archive_object"] }));

  await userEvent.click(screen.getByRole("button", { name: /Retire this/i }));
  // FIRST CLICK MEASURES. Nothing is retired by it, and the confirm control does
  // not exist yet: a retirement approved before its impact was measured is a
  // click people make to find out what it does.
  await userEvent.click(screen.getByRole("button", { name: /Measure what depends on it/i }));

  await waitFor(() => expect(posted.filter((entry) => entry.url.endsWith("/prepare"))).toHaveLength(1));
  const created = posted.find((entry) => entry.url.endsWith("/change-sets"))!;
  expect(created.body.object_id).toBe("sc_EXAMPLE");
  expect(created.body.base_version_id).toBe("scv_EXAMPLE");
  // NO BODY: `archive_object` names its object through `object_id`, and it is
  // the one action `create_change_set` exempts from `intent.concept`/`view`.
  expect(created.body.intent).toEqual({ action: "archive_object" });
  expect(created.body.idempotency_key).toBeTruthy();
  expect(posted.filter((entry) => entry.url.endsWith("/confirm"))).toHaveLength(0);

  // The impact the server measured, with the adapter that cannot answer saying
  // so rather than counting zero.
  const impact = await screen.findByTestId("archive-impact");
  expect(within(impact).getByText(/Semantic Views/)).toBeInTheDocument();
  expect(within(impact).getByText(/is not delivered/i)).toBeInTheDocument();

  await userEvent.click(await screen.findByRole("button", { name: "Retire this Semantic Concept" }));
  await waitFor(() => expect(posted.filter((entry) => entry.url.endsWith("/confirm"))).toHaveLength(1));
  expect(posted.find((entry) => entry.url.endsWith("/confirm"))!.body.confirmation_token).toBe(
    "token-EXAMPLE",
  );
  // The object is re-read from its owner: the archived flag, the used-by count
  // and the version list all move together, and only the server knows the truth.
  await waitFor(() => expect(reload).toHaveBeenCalled());
});

it("renders a live-consumer refusal whole and retires nothing", async () => {
  const posted = stubFetch({
    prepare: {
      publishable: false,
      refusals: [
        {
          code: "live_consumers",
          path: "$.object_id",
          message:
            "1 object(s) still hold this one: monthly_sales (published). A draft holds it too.",
        },
      ],
    },
  });
  mount("semantic-concept", detail({ allowed_actions: ["archive_object"] }));

  await userEvent.click(screen.getByRole("button", { name: /Retire this/i }));
  await userEvent.click(screen.getByRole("button", { name: /Measure what depends on it/i }));

  const refusals = await screen.findByTestId("semantic-action-refusals");
  expect(within(refusals).getByText("live_consumers", { exact: false })).toBeInTheDocument();
  expect(within(refusals).getByText(/monthly_sales \(published\)/)).toBeInTheDocument();
  expect(posted.filter((entry) => entry.url.endsWith("/confirm"))).toHaveLength(0);
  expect(screen.queryByRole("button", { name: "Retire this Semantic Concept" })).not.toBeInTheDocument();
  expect(reload).not.toHaveBeenCalled();
});

it("offers the written override when the Test gate blocks the retirement", async () => {
  // The real first answer for every retirement: Test records a verdict against a
  // candidate that publishes a version, and a retirement publishes none. The
  // module has ONE gate, so the reason is what carries it — and it travels on a
  // RE-prepare of the same change set, never a second one.
  const posted = stubFetch({
    prepare: {
      publishable: false,
      gate: { state: "unverifiable", message: "No Test gate verdict exists for this candidate." },
    },
  });
  mount("semantic-concept", detail({ allowed_actions: ["archive_object"] }));

  await userEvent.click(screen.getByRole("button", { name: /Retire this/i }));
  await userEvent.click(screen.getByRole("button", { name: /Measure what depends on it/i }));

  expect(await screen.findByText(/No Test gate verdict exists for this candidate/)).toBeInTheDocument();
  const again = screen.getByRole("button", { name: /Measure again with this reason/i });
  expect(again).toBeDisabled();

  await userEvent.type(
    screen.getByLabelText(/Why publish it anyway/i),
    "Superseded by the conformed channel dimension.",
  );
  expect(screen.getByRole("button", { name: /Measure again with this reason/i })).toBeEnabled();
  await userEvent.click(screen.getByRole("button", { name: /Measure again with this reason/i }));

  await waitFor(() => expect(posted.filter((entry) => entry.url.endsWith("/prepare"))).toHaveLength(2));
  // The SAME change set, re-prepared. A second one would measure a different
  // candidate and lose the reason.
  expect(posted.filter((entry) => entry.url.endsWith("/change-sets"))).toHaveLength(1);
  expect(posted[posted.length - 1].body).toEqual({
    test_gate_override: { reason: "Superseded by the conformed channel dimension." },
  });
});

it("opens the View dialog when the owner declared a Semantic View editable", async () => {
  stubFetch();
  mount(
    "semantic-view",
    detail({
      object_ref: {
        type: "semantic-view",
        id: "sv_EXAMPLE",
        label: "Monthly sales",
        owner_href: { surface: "project", workspace: "governance", section: "semantic-model" },
      },
      active_version_ref: { object_type: "semantic-view-version", id: "svv_EXAMPLE", version: 2, state: "published" },
      allowed_actions: ["edit_view"],
      summary: {},
    }),
  );

  await userEvent.click(screen.getByRole("button", { name: /Edit this/i }));
  expect(await screen.findByText(/View dialog on sv_EXAMPLE from svv_EXAMPLE/)).toBeInTheDocument();
});
