/**
 * The Governance dialog is a DOOR onto the ONE creation command.
 *
 * Ratified 2026-08-17 (`context-hub.md`, "where a Business Domain is created"):
 * one creation gesture, reached from both surfaces. Ratified 2026-08-25
 * (`governance.md`): that gesture is the Master Data authority's, because the
 * legacy identity writers refuse — *converge this organization, then create,
 * rename or archive it in Master Data*. So the address moved to
 * `POST /api/projects/{id}/governance/master-data/nodes` and the Context Hub
 * moved with it: still two doors, still one writer.
 *
 * The properties pinned here are the ones a green render cannot prove: the
 * address written to, the Idempotency-Key the route requires (428 without it,
 * and one key per submission so a retry cannot mint a second identity), that
 * there is exactly ONE write, and that each refusal reaches the person as the
 * gesture that repairs it — the convergence and a taken short code are both 409
 * and are repaired on different screens.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import NewBusinessDomainDialog from "../governance/NewBusinessDomainDialog";
import { ApiError, apiGet, apiPost } from "../lib/apiFetch";

vi.mock("../lib/apiFetch", () => ({
  // Same shape as the real class: the dialog branches on `instanceof ApiError`
  // and reads `status`, so a bare `Error` stub would silently exercise the
  // wrong arm of the error mapping.
  ApiError: class ApiError extends Error {
    status: number;
    code: string;
    constructor(status: number, code: string, message: string) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.code = code;
    }
  },
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiJson: vi.fn(),
}));

const mockedPost = vi.mocked(apiPost);
const mockedGet = vi.mocked(apiGet);

const openDialog = (overrides: Partial<{ onClose: () => void; onCreated: () => void }> = {}) => {
  const onClose = overrides.onClose ?? vi.fn();
  const onCreated = overrides.onCreated ?? vi.fn();
  render(
    <NewBusinessDomainDialog open projectId="project_1" onClose={onClose} onCreated={onCreated} />,
  );
  return { onClose, onCreated };
};

afterEach(() => vi.clearAllMocks());

test("a submitted Business Domain is written to the one creation endpoint", async () => {
  const user = userEvent.setup();
  mockedPost.mockResolvedValue({ id: "bdm_1", name: "Finance" } as never);
  const { onCreated } = openDialog();

  await user.type(screen.getByLabelText("Item Name"), "Finance");
  await user.type(screen.getByLabelText("Reason for this change"), "New finance perimeter");
  await user.click(screen.getByRole("button", { name: "Save" }));

  await waitFor(() => expect(onCreated).toHaveBeenCalled());

  expect(mockedPost).toHaveBeenCalledWith(
    "/api/projects/project_1/governance/master-data/nodes",
    {
      kind: "business_domain",
      name: "Finance",
      description: "",
      slug: undefined,
      reason: "New finance perimeter",
    },
    {
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": expect.stringMatching(/^md-create-/),
      },
    },
  );
  // Exactly one write, and never the superseded store: the legacy door refuses
  // since the cutover, so a second arm reaching it would be a hidden failure.
  expect(mockedPost).toHaveBeenCalledTimes(1);
  expect(
    mockedPost.mock.calls.some(([address]) => String(address).includes("/api/context/")),
  ).toBe(false);
});

test("a success refreshes the collection behind the dialog", async () => {
  const user = userEvent.setup();
  mockedPost.mockResolvedValue({ id: "bdm_1" } as never);
  const { onClose, onCreated } = openDialog();

  await user.type(screen.getByLabelText("Item Name"), "Sales");
  await user.type(screen.getByLabelText("Reason for this change"), "Split from Marketing");
  await user.click(screen.getByRole("button", { name: "Save" }));

  // `onCreated` is the collection's `reload`: without it the new row is written
  // and the screen still shows the list it was opened with.
  await waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
  expect(onClose).toHaveBeenCalledTimes(1);
});

test("a duplicate short code is rendered as the gesture that repairs it", async () => {
  const user = userEvent.setup();
  mockedPost.mockRejectedValue(
    new ApiError(409, "master_data_short_code_taken", ""),
  );
  const { onCreated } = openDialog();

  await user.type(screen.getByLabelText("Item Name"), "Finance");
  await user.type(screen.getByLabelText("Short Code"), "finance");
  await user.type(screen.getByLabelText("Reason for this change"), "New finance perimeter");
  await user.click(screen.getByRole("button", { name: "Save" }));

  expect(await screen.findByText(/Change the Short code and submit again/i)).toBeInTheDocument();
  // The dialog stays open with the entered values: a refused write must not
  // cost the person the form they filled in.
  expect(screen.getByLabelText("Item Name")).toHaveValue("Finance");
  expect(onCreated).not.toHaveBeenCalled();
});

test("an unavailable registry says nothing was created, not that it failed", async () => {
  const user = userEvent.setup();
  mockedPost.mockRejectedValue(
    new ApiError(500, "db_error", "Context Hub is temporarily unavailable"),
  );
  openDialog();

  await user.type(screen.getByLabelText("Item Name"), "Finance");
  await user.type(screen.getByLabelText("Reason for this change"), "New finance perimeter");
  await user.click(screen.getByRole("button", { name: "Save" }));

  expect(
    await screen.findByText(/temporarily unavailable, so nothing was created/i),
  ).toBeInTheDocument();
});

test("the reason the endpoint requires is asked for before any write is sent", async () => {
  const user = userEvent.setup();
  openDialog();

  await user.type(screen.getByLabelText("Item Name"), "Finance");
  await user.click(screen.getByRole("button", { name: "Save" }));

  expect(await screen.findByText(/Add a reason/i)).toBeInTheDocument();
  // `_audit` runs `reason` through `_required`, so sending without it would only
  // buy a 422. The question is asked here rather than answered by the server.
  expect(mockedPost).not.toHaveBeenCalled();
});

test("an unconverged organization is told to converge, not to change the short code", async () => {
  const user = userEvent.setup();
  const sentence =
    "This organization has not converged into Master Data yet, so nothing can be declared "
    + "here. Run the convergence on the Governance Master Data screen first.";
  mockedPost.mockRejectedValue(
    new ApiError(409, "master_data_organization_not_converged", sentence),
  );
  openDialog();

  await user.type(screen.getByLabelText("Item Name"), "Finance");
  await user.type(screen.getByLabelText("Reason for this change"), "New finance perimeter");
  await user.click(screen.getByRole("button", { name: "Save" }));

  // Verbatim: printing "change the Short code" over this would send a person to
  // edit a field that is not the problem, and hide the one gesture that works.
  expect(await screen.findByText(new RegExp("Run the convergence", "i"))).toBeInTheDocument();
  expect(screen.queryByText(/Change the Short code/i)).not.toBeInTheDocument();
});

test("a retry of one submission carries the SAME idempotency key", async () => {
  const user = userEvent.setup();
  mockedPost.mockRejectedValueOnce(new ApiError(503, "unavailable", "Master Data is unavailable"));
  mockedPost.mockResolvedValueOnce({ result: { node_id: "bd_1" } } as never);
  const { onCreated } = openDialog();

  await user.type(screen.getByLabelText("Item Name"), "Finance");
  await user.type(screen.getByLabelText("Reason for this change"), "New finance perimeter");
  await user.click(screen.getByRole("button", { name: "Save" }));
  await screen.findByText(/temporarily unavailable/i);
  await user.click(screen.getByRole("button", { name: "Save" }));

  await waitFor(() => expect(onCreated).toHaveBeenCalled());
  const keys = mockedPost.mock.calls.map(
    ([, , init]) => (init as { headers: Record<string, string> }).headers["Idempotency-Key"],
  );
  expect(keys).toHaveLength(2);
  // A fresh key on the second attempt is exactly how a client timeout mints a
  // second identity for the same declaration.
  expect(keys[0]).toBe(keys[1]);
});

test("a Classification asks for its parent, and carries it as a governed node id", async () => {
  const user = userEvent.setup();
  mockedGet.mockResolvedValue({
    domains: [{ id: "bdm_finance", name: "Finance" }],
  } as never);
  mockedPost.mockResolvedValue({ id: "bcl_1" } as never);
  const { onCreated } = openDialog();

  await user.selectOptions(screen.getByLabelText("Item Type"), "classification");

  const parent = await screen.findByLabelText("Parent Business Domain");
  expect(mockedGet).toHaveBeenCalledWith(
    "/api/context/business-taxonomy?projection=analysis-picker&status=active&project_id=project_1",
  );

  await user.selectOptions(parent, "bdm_finance");
  await user.type(screen.getByLabelText("Item Name"), "Retail");
  await user.type(screen.getByLabelText("Reason for this change"), "Retail line opened");
  await user.click(screen.getByRole("button", { name: "Save" }));

  await waitFor(() => expect(onCreated).toHaveBeenCalled());
  expect(mockedPost).toHaveBeenCalledWith(
    "/api/projects/project_1/governance/master-data/nodes",
    {
      kind: "business_classification",
      domain_node_id: "bdm_finance",
      classification_type: "product_line",
      name: "Retail",
      description: "",
      slug: undefined,
      reason: "Retail line opened",
    },
    {
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": expect.stringMatching(/^md-create-/),
      },
    },
  );
});

test("an organization with no domain names the gesture instead of an empty list", async () => {
  const user = userEvent.setup();
  mockedGet.mockResolvedValue({ domains: [] } as never);
  openDialog();

  await user.selectOptions(screen.getByLabelText("Item Type"), "classification");

  expect(await screen.findByTestId("parent-domain-empty")).toHaveTextContent(
    /Switch Item Type to Root Business Domain and create one first/i,
  );
  expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
});
