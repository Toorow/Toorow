import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ContentRouter from "../shell/ContentRouter";

const { navigateMock } = vi.hoisted(() => ({ navigateMock: vi.fn() }));

vi.mock("../shell/router", () => ({
  useRoute: () => ({
    route: {
      projectId: "proj-1",
      workspace: "data",
      section: "add",
      objectType: null,
      objectId: null,
      tab: null,
    },
    navigate: navigateMock,
  }),
}));

vi.mock("../shell/scope", () => ({
  useScope: () => ({
    org: { id: "org-1", name: "Example Org", branding: null, projects: [] },
  }),
}));

vi.mock("../shell/pages/DatastreamCreate", () => ({
  default: ({
    projectId,
    onActivated,
  }: {
    projectId: string;
    onActivated: (datastreamId: string) => void;
  }) => (
    <button type="button" onClick={() => onActivated("ds-created-1")}>
      Complete creation for {projectId}
    </button>
  ),
}));

beforeEach(() => navigateMock.mockClear());

it("routes successful creation to the canonical first-publication destination", async () => {
  const user = userEvent.setup();
  render(<ContentRouter />);
  await user.click(screen.getByRole("button", { name: /Complete creation for proj-1/ }));
  expect(navigateMock).toHaveBeenCalledWith({
    workspace: "data",
    section: "datastreams",
    objectType: "datastream",
    objectId: "ds-created-1",
    tab: "first-publication",
  });
});
