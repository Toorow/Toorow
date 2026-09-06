import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ContentRouter from "../shell/ContentRouter";

const { navigateMock, routeState } = vi.hoisted(() => ({
  navigateMock: vi.fn(),
  routeState: {
    // Story 46.4 made the route a discriminated union: without `scope`,
    // ContentRouter answers with the unknown state before reaching any owner.
    scope: "project" as const,
    globalSurface: null as string | null,
    globalSection: null as string | null,
    organizationId: "org-1",
    projectId: "proj-1",
    workspace: "data",
    section: "datastreams",
    lens: null as string | null,
    objectType: "datastream" as string | null,
    objectId: "ds-1" as string | null,
    tab: "overview" as string | null,
    versionId: null as string | null,
    action: null as string | null,
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

vi.mock("../shell/pages/DatastreamCreate", () => ({
  default: ({ projectId, onCancel, onSourceSetup }: { projectId: string; onCancel: () => void; onSourceSetup: (draftId: string) => void }) => <div>
    <h1>Add Datastream for {projectId}</h1>
    <button onClick={onCancel}>Cancel wizard</button>
    <button onClick={() => onSourceSetup("dsd-resume")}>Source setup</button>
    <span>No activation authority</span>
  </div>,
}));

beforeEach(() => {
  navigateMock.mockClear();
  Object.assign(routeState, {
    scope: "project",
    globalSurface: null,
    globalSection: null,
    workspace: "data",
    section: "datastreams",
    lens: null,
    objectType: "datastream",
    objectId: "ds-1",
    tab: "overview",
    versionId: null,
    action: null,
  });
});

it("renders an honest unavailable state for a pinned object version, not a stale one", async () => {
  routeState.action = null;
  routeState.versionId = "retired-v1";
  render(<ContentRouter />);
  // The version is intact; no owner reads one yet. Calling it "stale" would
  // tell the operator their reference was retired, which is not true.
  expect(await screen.findByRole("heading", { name: "This route is registered, but its workbench does not exist yet" })).toBeInTheDocument();
  expect(screen.queryByText(/no longer current/)).not.toBeInTheDocument();
  expect(screen.queryByText(/First publication for/)).not.toBeInTheDocument();
});

it("mounts the canonical Add Datastream wizard and returns through canonical owner routes", async () => {
  const user = userEvent.setup();
  // "Add Datastream" is a COLLECTION action now: no object type, no object id,
  // and therefore no invented identifier in the address.
  routeState.action = "create";
  routeState.objectType = null;
  routeState.objectId = null;
  const first = render(<ContentRouter />);
  expect(await screen.findByRole("heading", { name: "Add Datastream for proj-1" })).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Cancel wizard" }));
  expect(navigateMock).toHaveBeenLastCalledWith({ objectType: null, objectId: null, tab: null, versionId: null, action: null });
  await user.click(screen.getByRole("button", { name: "Source setup" }));
  expect(navigateMock).toHaveBeenLastCalledWith({ workspace: "data", section: "sources", objectType: null, objectId: null, tab: null, versionId: null, action: null });
  expect(screen.getByText("No activation authority")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /activate/i })).not.toBeInTheDocument();
  first.unmount();
});
