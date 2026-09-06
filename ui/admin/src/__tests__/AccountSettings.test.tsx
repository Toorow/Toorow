import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import AccountSettings, { SessionPanel } from "../shell/pages/AccountSettings";

afterEach(() => vi.unstubAllGlobals());

describe("User Account", () => {
  it("loads personal authorizations without Organization or Project props", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ authorizations: [{ id: "auth-1", provider: "Provider", status: "Connected" }] }) }));
    render(<AccountSettings section="authorizations" />);
    expect(await screen.findByRole("heading", { name: "User Account" })).toBeInTheDocument();
    expect(screen.getAllByText("Provider")[0]).toBeInTheDocument();
    expect(screen.getByText(/without selecting a Project/i)).toBeInTheDocument();
  });

  it("edits the display name through the existing personal profile route", async () => {
    let acceptedName = "Ada";
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url === "/api/organizations") {
        return Promise.resolve({ ok: true, status: 200, json: async () => ({ organizations: [] }) });
      }
      if (url === "/api/me/profile" && init?.method === "PATCH") {
        acceptedName = "Ada Lovelace";
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({ identity: "person-1", email: "person@example.com", display_name: "Ada Lovelace" }),
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({ identity: "person-1", email: "person@example.com", display_name: acceptedName }),
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<AccountSettings section="profile" />);

    const input = await screen.findByLabelText("Display name");
    fireEvent.change(input, { target: { value: "Ada Lovelace" } });
    fireEvent.click(screen.getByRole("button", { name: "Save name" }));

    await screen.findByText("Display name saved.");
    const patchCall = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
    expect(JSON.parse(String(patchCall?.[1]?.body))).toEqual({ display_name: "Ada Lovelace" });
    await waitFor(() => expect(screen.getByText("Ada Lovelace")).toBeInTheDocument());
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/me/profile")).toHaveLength(3);
  });
});

describe("Preferences (page-structure.md §G.3.1)", () => {
  it("offers exactly what is real without a server: the appearance choice", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({
      ok: true, status: 200,
      json: async () => ({ identity: "person-1", email: "person@example.com", display_name: "Ada" }),
    })));
    render(<AccountSettings section="profile" />);

    const panel = await screen.findByTestId("account-preferences");
    // The SAME appearance control the rail menu renders — one store, two hosts.
    expect(within(panel).getByRole("switch", { name: "Dark theme" })).toBeInTheDocument();
    // Sign-out moved to Session: revoking every session is a SERVER act, and it
    // cannot sit under a header promising nothing is stored on the server.
    expect(within(panel).queryByRole("button", { name: /sign out/i })).toBeNull();
    // No promise the server cannot keep: me_api serves no locale or timezone.
    expect(within(panel).queryByText(/locale|timezone|density/i)).toBeNull();
  });
});

/**
 * `organization-settings.md:138-142` ratifies THREE distinct gestures. Two of
 * them are mine to make, and until 67-15's review both were a button labelled
 * "Sign out" posting `/api/auth/logout`: signing out everywhere existed on the
 * server (`me_api.py:660`) and for nobody else.
 */
describe("Session — the two gestures are distinct", () => {
  it("names them apart, and the page asks the question once", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({
      ok: true, status: 200,
      json: async () => ({ identity: "person-1", email: "person@example.com", display_name: "Ada" }),
    })));
    render(<AccountSettings section="profile" />);

    const panel = await screen.findByTestId("account-session");
    expect(within(panel).getByRole("button", { name: "Sign out" })).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "Sign out everywhere" })).toBeInTheDocument();
    // What each one reaches, in the person's words and not the store's.
    expect(within(panel).getByText(/Any other window you left signed in stays signed in/i)).toBeInTheDocument();
    expect(within(panel).getByText(/every window signed in before now/i)).toBeInTheDocument();
    // One sign-out per gesture on the whole page: the profile panel used to
    // carry a third button, labelled like the first and calling the same route.
    expect(screen.getAllByRole("button", { name: /^sign out/i })).toHaveLength(2);
  });

  it("signing out everywhere posts the revocation route, not the logout route", async () => {
    const fetchMock = vi.fn(() => Promise.resolve({ ok: true, status: 200, json: async () => ({ revoked: 1 }) }));
    vi.stubGlobal("fetch", fetchMock);
    const navigate = vi.fn();
    localStorage.setItem("api_token", "a-token");
    sessionStorage.setItem("toorow_browser_identity", '{"name":"Ada"}');

    render(<SessionPanel navigate={navigate} />);
    fireEvent.click(screen.getByRole("button", { name: "Sign out everywhere" }));

    await waitFor(() => expect(navigate).toHaveBeenCalledOnce());
    const calls = fetchMock.mock.calls as unknown as Array<[string, RequestInit]>;
    expect(calls).toHaveLength(1);
    expect(calls[0][0]).toBe("/api/me/sessions/revoke");
    expect(calls[0][1].method).toBe("POST");
    expect(calls[0][1].credentials).toBe("same-origin");
    // The bound covers this window too, so what this browser remembers goes.
    expect(localStorage.getItem("api_token")).toBeNull();
    expect(sessionStorage.getItem("toorow_browser_identity")).toBeNull();
    localStorage.clear();
    sessionStorage.clear();
  });

  it("a deployment that cannot cut sessions is told so, and names the gesture that still works", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({
      ok: false, status: 503,
      json: async () => ({ code: "unavailable", message: "Session revocation is disabled on this deployment." }),
    })));
    const navigate = vi.fn();

    render(<SessionPanel navigate={navigate} />);
    fireEvent.click(screen.getByRole("button", { name: "Sign out everywhere" }));

    const refusal = await screen.findByRole("alert");
    expect(refusal).toHaveTextContent(/Your other windows are still open/i);
    expect(refusal).toHaveTextContent(/Sign out of this window with the button above/i);
    // Never claimed and never left: a browser that walked away here would look
    // signed out while the laptop it meant to cut kept reading.
    expect(navigate).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Sign out everywhere" })).toBeEnabled();
  });

  it("a failed revocation says nothing was signed out", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({
      ok: false, status: 500,
      json: async () => ({ code: "db_error", message: "Database error" }),
    })));
    const navigate = vi.fn();

    render(<SessionPanel navigate={navigate} />);
    fireEvent.click(screen.getByRole("button", { name: "Sign out everywhere" }));

    const refusal = await screen.findByRole("alert");
    expect(refusal).toHaveTextContent(/Nothing was signed out/i);
    expect(navigate).not.toHaveBeenCalled();
  });
});
