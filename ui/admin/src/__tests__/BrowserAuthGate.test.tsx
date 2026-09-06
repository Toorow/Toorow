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

test("uses the passive shell frame while the browser session resolves", () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      () =>
        new Promise<Response>(() => {
          // Keep the request pending so the transient entry state is observable.
        }),
    ),
  );

  render(
    <BrowserAuthGate>
      <div>Protected application</div>
    </BrowserAuthGate>,
  );

  const status = screen.getByRole("status", { name: "Opening toorow" });
  expect(status).toHaveAttribute("aria-busy", "true");
  expect(status).toHaveClass("entry-boot-shell");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.queryByRole("heading")).not.toBeInTheDocument();
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
  expect(screen.queryByRole("link")).not.toBeInTheDocument();
  expect(status.querySelector("input, select, textarea")).toBeNull();
  expect(screen.queryByText(/workspace/i)).not.toBeInTheDocument();
  expect(screen.queryByText("Checking your session")).not.toBeInTheDocument();
  expect(screen.queryByText("Protected application")).not.toBeInTheDocument();
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

// ---------------------------------------------------------------------------
// Le client id OAuth vient du SERVEUR (2026-08-04)
//
// Il n'etait lu qu'au build (`VITE_GOOGLE_CLIENT_ID`). Un bundle construit sans
// lui a deploye en production un ecran de connexion qui ne pouvait pas aboutir :
// « VITE_GOOGLE_CLIENT_ID is not set at build time ». Les tests tournant sans
// cette variable, l'ancienne lecture rendait l'erreur -- ce qui rend ces deux
// tests capables d'echouer si on revenait en arriere.
// ---------------------------------------------------------------------------

test("initialises Google with the client id the SERVER served, not a build value", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      response(200, {
        mode: "google_gis",
        client_id: "example-client-id.apps.googleusercontent.com",
      }),
    ),
  );
  const initialize = vi.fn();
  vi.stubGlobal("google", {
    accounts: { id: { initialize, renderButton: vi.fn() } },
  });
  // `initializeGoogle` n'est appele qu'une fois le script GIS present.
  const script = document.createElement("script");
  script.id = "gsi-script";
  document.head.appendChild(script);

  render(
    <BrowserAuthGate>
      <div>Protected application</div>
    </BrowserAuthGate>,
  );

  await waitFor(() =>
    expect(initialize).toHaveBeenCalledWith(
      expect.objectContaining({
        client_id: "example-client-id.apps.googleusercontent.com",
      }),
    ),
  );
  // Et surtout : plus aucune accusation portee contre le build.
  expect(screen.queryByText(/not set at build time/i)).not.toBeInTheDocument();
  script.remove();
});

test("names the SERVER variable when no client id is available at all", async () => {
  // `ui/admin/.env` fournit la variable en local : sans la neutraliser, ce test
  // passerait pour de mauvaises raisons et n'attraperait jamais le defaut.
  vi.stubEnv("VITE_GOOGLE_CLIENT_ID", "");
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      response(200, { mode: "google_gis", reason: "oidc_client_id_missing" }),
    ),
  );

  render(
    <BrowserAuthGate>
      <div>Protected application</div>
    </BrowserAuthGate>,
  );

  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(/TOOROW_OIDC_CLIENT_ID/);
  // L'ancien message accusait le build d'une valeur que le serveur n'avait pas.
  expect(alert).not.toHaveTextContent(/VITE_GOOGLE_CLIENT_ID/);
  vi.unstubAllEnvs();
});
