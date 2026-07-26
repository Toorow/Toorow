import { render, screen, waitFor } from "@testing-library/react";
import BrowserAuthGate from "../shell/BrowserAuthGate";

function response(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  window.history.replaceState({}, "", "/p/project/overview?tab=one");
  document.getElementById("gsi-script")?.remove();
});

afterEach(() => {
  vi.restoreAllMocks();
});

test("accepts a valid HttpOnly OIDC session without loading Google GIS", async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(
      response(200, { mode: "oidc", provider_name: "Example SSO" }),
    )
    .mockResolvedValueOnce(
      response(200, {
        authenticated: true,
        display_name: "Person Example",
        email: "person@example.com",
      }),
    );
  vi.stubGlobal("fetch", fetchMock);

  render(
    <BrowserAuthGate>
      <div>Protected application</div>
    </BrowserAuthGate>,
  );

  expect(await screen.findByText("Protected application")).toBeInTheDocument();
  expect(fetchMock).toHaveBeenNthCalledWith(
    2,
    "/api/auth/session",
    expect.objectContaining({ credentials: "same-origin", cache: "no-store" }),
  );
  expect(document.getElementById("gsi-script")).toBeNull();
  expect(sessionStorage.getItem("toorow_browser_identity")).toContain(
    "Person Example",
  );
});

test("offers the server-side OIDC login when the session is absent", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValueOnce(
        response(200, { mode: "oidc", provider_name: "Company SSO" }),
      )
      .mockResolvedValueOnce(response(401, { authenticated: false })),
  );

  render(
    <BrowserAuthGate>
      <div>Protected application</div>
    </BrowserAuthGate>,
  );

  const link = await screen.findByRole("link", {
    name: "Sign in with Company SSO",
  });
  expect(link).toHaveAttribute(
    "href",
    "/api/auth/oidc/login?return_to=%2Fp%2Fproject%2Foverview%3Ftab%3Done",
  );
  expect(localStorage.getItem("api_token")).toBeNull();
});

test("does not let a stale GIS token bypass an explicit OIDC mode", async () => {
  const payload = btoa(
    JSON.stringify({ exp: Math.floor(Date.now() / 1000) + 3600 }),
  );
  localStorage.setItem("api_token", `header.${payload}.signature`);
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValueOnce(
        response(200, { mode: "oidc", provider_name: "Company SSO" }),
      )
      .mockResolvedValueOnce(response(401, { authenticated: false })),
  );

  render(
    <BrowserAuthGate>
      <div>Protected application</div>
    </BrowserAuthGate>,
  );

  expect(
    await screen.findByRole("link", { name: "Sign in with Company SSO" }),
  ).toBeInTheDocument();
  expect(screen.queryByText("Protected application")).not.toBeInTheDocument();
  expect(localStorage.getItem("api_token")).toBeNull();
});

test("fails closed instead of falling back to Google when config is invalid", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      response(200, {
        mode: "misconfigured",
        reason: "oidc_configuration_invalid",
      }),
    ),
  );

  render(
    <BrowserAuthGate>
      <div>Protected application</div>
    </BrowserAuthGate>,
  );

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "oidc_configuration_invalid",
  );
  expect(screen.queryByText("Protected application")).not.toBeInTheDocument();
  expect(document.getElementById("gsi-script")).toBeNull();
});

test("keeps the explicit static development mode closed without a token", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(response(200, { mode: "static" })),
  );

  render(
    <BrowserAuthGate>
      <div>Protected application</div>
    </BrowserAuthGate>,
  );

  expect(
    await screen.findByText(
      "This instance requires a static development token.",
    ),
  ).toBeInTheDocument();
  await waitFor(() => {
    expect(screen.queryByText("Protected application")).not.toBeInTheDocument();
  });
});
