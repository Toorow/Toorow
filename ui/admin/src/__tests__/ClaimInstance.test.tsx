import { StrictMode } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import ClaimInstance from "../shell/pages/ClaimInstance";

vi.mock("../shell/AuthGate", () => ({
  default: ({ children }: { children: React.ReactNode }) => children,
}));

function signIn(): void {
  const payload = btoa(
    JSON.stringify({ exp: Math.floor(Date.now() / 1000) + 3600 }),
  );
  localStorage.setItem("api_token", `header.${payload}.signature`);
}

beforeEach(() => {
  localStorage.clear();
  signIn();
  window.history.replaceState({}, "", "/setup");
});

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

it("strips the bootstrap fragment before rendering authenticated setup", async () => {
  window.history.replaceState({}, "", "/setup#bootstrap=installer-secret");
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200 });
  vi.stubGlobal("fetch", fetchMock);

  render(
    <StrictMode>
      <ClaimInstance />
    </StrictMode>,
  );

  expect(window.location.hash).toBe("");
  expect(
    await screen.findByRole("heading", { name: "Claim this toorow instance" }),
  ).toBeVisible();
  expect(fetchMock).toHaveBeenCalledWith(
    "/api/instance/bootstrap/exchange",
    expect.objectContaining({
      method: "POST",
      credentials: "same-origin",
      body: JSON.stringify({ bootstrap_bearer: "installer-secret" }),
    }),
  );
  expect(claimCalls(fetchMock)).toHaveLength(1);
  expect(document.getElementById("gsi-script")).toBeNull();
});

/** The calls this file is about: the claim command chain.
 *
 *  The screen also READS two reference vocabularies (currencies, timezones) to
 *  offer the reporting defaults. Those are not part of the claim, and the
 *  assertions below used to index `mock.calls[1]` and `[2]` positionally — so
 *  adding any unrelated read silently shifted what they inspected. Selecting by
 *  path makes each assertion say what it means.
 */
function claimCalls(mock: { mock: { calls: unknown[][] } }) {
  return mock.mock.calls.filter(
    ([path]) => !String(path).startsWith("/api/reference/"),
  );
}

it("fails closed when the bootstrap capability is absent or rejected", async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 404 });
  vi.stubGlobal("fetch", fetchMock);
  const { rerender } = render(<ClaimInstance />);

  expect(
    await screen.findByRole("heading", { name: "Setup unavailable" }),
  ).toBeVisible();
  expect(fetchMock).toHaveBeenCalledWith(
    "/api/instance/claim/session",
    expect.objectContaining({
      method: "GET",
      credentials: "same-origin",
    }),
  );
  expect(
    screen.queryByRole("button", { name: "Claim instance" }),
  ).not.toBeInTheDocument();

  window.history.replaceState({}, "", "/setup#bootstrap=rejected-secret");
  fetchMock.mockResolvedValueOnce({ ok: false, status: 404 });
  rerender(<ClaimInstance key="rejected" />);
  expect(
    await screen.findByRole("heading", { name: "Setup unavailable" }),
  ).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "Claim instance" }),
  ).not.toBeInTheDocument();
});

it("submits the first organization and project through the claim command", async () => {
  window.history.replaceState({}, "", "/setup#bootstrap=installer-secret");
  const fetchMock = vi
    .fn()
    // The screen reads two reference vocabularies at mount to offer the
    // reporting defaults. A `...Once` queue is consumed BEFORE any default
    // implementation, so those two reads would otherwise eat the first two
    // claim responses and shift the whole chain. Answering them here, first and
    // explicitly, keeps the queue below reserved for the claim command.
    .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ items: [] }) })
    .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ items: [] }) })
    .mockResolvedValueOnce({ ok: true, status: 200 })
    .mockResolvedValueOnce({
      ok: true,
      status: 201,
      json: async () => ({
        confirmation_id: "econf_claim",
        confirmation_secret: "ecfs_server_secret",
      }),
    })
    .mockResolvedValueOnce({
      ok: true,
      status: 201,
      json: async () => ({ next_url: "https://attacker.example/p/project" }),
    });
  vi.stubGlobal("fetch", fetchMock);
  render(<ClaimInstance />);

  fireEvent.change(await screen.findByLabelText("Organization name"), {
    target: { value: "Acme France" },
  });
  expect(screen.getByLabelText("Organization slug")).toHaveAttribute(
    "maxLength",
    "50",
  );
  expect(screen.getByLabelText("Project slug")).toHaveAttribute(
    "maxLength",
    "50",
  );
  fireEvent.change(screen.getByLabelText("First project"), {
    target: { value: "Marketing" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Claim instance" }));

  await waitFor(() => expect(claimCalls(fetchMock)).toHaveLength(3));
  const [confirmationPath, confirmationInit] = claimCalls(fetchMock)[1] as [
    string,
    RequestInit,
  ];
  const [path, init] = claimCalls(fetchMock)[2] as [string, RequestInit];
  expect(confirmationPath).toBe("/api/instance/claim/confirmation");
  expect(confirmationInit.body).toBe(init.body);
  expect(path).toBe("/api/instance/claim");
  expect(init.credentials).toBe("same-origin");
  expect(init.headers).toEqual(
    expect.objectContaining({
      "Idempotency-Key": expect.stringMatching(/^instance-claim-/),
      "X-Confirmation-Id": "econf_claim",
      "X-Confirmation-Secret": "ecfs_server_secret",
    }),
  );
  expect(JSON.parse(String(init.body))).toEqual(
    expect.objectContaining({
      organization_name: "Acme France",
      organization_slug: "acme-france",
      project_name: "Marketing",
      project_slug: "marketing",
    }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "invalid destination",
  );
});
it("resumes a tokenless claim session and retries a network failure without reload", async () => {
  const fetchMock = vi
    .fn()
    // The screen reads two reference vocabularies at mount to offer the
    // reporting defaults. A `...Once` queue is consumed BEFORE any default
    // implementation, so those two reads would otherwise eat the first two
    // claim responses and shift the whole chain. Answering them here, first and
    // explicitly, keeps the queue below reserved for the claim command.
    .mockRejectedValueOnce(new Error("offline"))
    .mockResolvedValueOnce({ ok: true, status: 200 })
    // Here the session read comes FIRST: the form (and its vocabulary reads)
    // only appear once the session resolves, so these two answers belong at the
    // end rather than at the head as in the tests above.
    .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ items: [] }) })
    .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ items: [] }) });
  vi.stubGlobal("fetch", fetchMock);
  render(<ClaimInstance />);

  fireEvent.click(await screen.findByRole("button", { name: "Try again" }));

  expect(
    await screen.findByRole("heading", { name: "Claim this toorow instance" }),
  ).toBeVisible();
  expect(claimCalls(fetchMock)).toHaveLength(2);
  expect(claimCalls(fetchMock).at(-1)).toEqual([
    "/api/instance/claim/session",
    expect.objectContaining({ method: "GET" }),
  ]);
});

it("keeps claim success visible until the explicit next-step CTA", async () => {
  window.history.replaceState({}, "", "/setup#bootstrap=installer-secret");
  const fetchMock = vi
    .fn()
    // The screen reads two reference vocabularies at mount to offer the
    // reporting defaults. A `...Once` queue is consumed BEFORE any default
    // implementation, so those two reads would otherwise eat the first two
    // claim responses and shift the whole chain. Answering them here, first and
    // explicitly, keeps the queue below reserved for the claim command.
    .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ items: [] }) })
    .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ items: [] }) })
    .mockResolvedValueOnce({ ok: true, status: 200 })
    .mockResolvedValueOnce({
      ok: true,
      status: 201,
      json: async () => ({
        confirmation_id: "econf_claim",
        confirmation_secret: "ecfs_server_secret",
      }),
    })
    .mockResolvedValueOnce({
      ok: true,
      status: 201,
      json: async () => ({
        next_url: "/p/first-project/overview/getting-started",
      }),
    });
  vi.stubGlobal("fetch", fetchMock);
  render(<ClaimInstance />);

  fireEvent.change(await screen.findByLabelText("Organization name"), {
    target: { value: "Acme France" },
  });
  fireEvent.change(screen.getByLabelText("First project"), {
    target: { value: "Marketing" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Claim instance" }));

  expect(
    await screen.findByRole("heading", { name: "Instance claimed" }),
  ).toBeVisible();
  expect(
    screen.getByRole("button", { name: "Continue to getting started" }),
  ).toBeVisible();
});
