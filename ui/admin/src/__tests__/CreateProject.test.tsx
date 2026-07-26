import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import CreateProject from "../shell/pages/CreateProject";

afterEach(() => {
  vi.unstubAllGlobals();
});

it("creates the first project in the existing organization and returns its canonical id", async () => {
  const onCreated = vi.fn();
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    status: 201,
    json: async () => ({ id: "proj_first", name: "First project" }),
  });
  vi.stubGlobal("fetch", fetchMock);

  render(
    <CreateProject
      org={{ id: "org_acme", name: "Acme", branding: null, projects: [] }}
      onCreated={onCreated}
    />,
  );

  const user = userEvent.setup();
  expect(screen.getByRole("heading", { name: "Create your first project" })).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Create project" }));

  expect(fetchMock).toHaveBeenCalledWith(
    "/api/projects",
    expect.objectContaining({
      method: "POST",
      body: expect.any(String),
    }),
  );
  const init = fetchMock.mock.calls[0][1] as RequestInit;
  expect(JSON.parse(String(init.body))).toEqual(
    expect.objectContaining({
      name: "First project",
      slug: "first-project",
      org_id: "org_acme",
    }),
  );
  expect(onCreated).toHaveBeenCalledWith("proj_first");
});