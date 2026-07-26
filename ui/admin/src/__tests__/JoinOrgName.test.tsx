/**
 * JoinOrg — the name question, and the org-less arrival.
 *
 * Jean, 2026-07-26: « invitation -> on te demande ton nom et ton prénom (si pas
 * dispo dans l'oath) -> Tu accedes à une organisation (ou ca te permet d'en
 * creer une nouvelle) ». Acceptance is the one moment BOTH kinds of arrival
 * pass through, which is why the question lives here and not on
 * create-organization.
 *
 * Guarded here:
 *   - the question is asked ONLY when the server says nobody could give us a
 *     name (`profile.needs_name`) — a token that carries one must not produce a
 *     form;
 *   - the name is PATCHed before routing on, and a failed PATCH does NOT route
 *     silently — arriving named is the point of asking;
 *   - an ENTRY invitation (organization_id: null, real since migration 106)
 *     shows no membership summary and sends the person to create their own,
 *     instead of rendering an empty organization label.
 *
 * The transport is the global fetch, because src/lib/apiFetch.ts is the single
 * seam every call goes through.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import JoinOrg from "../shell/pages/JoinOrg";

function resp(status: number, body: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

interface Call {
  url: string;
  method: string;
  body: unknown;
}

function stubFetch(handler: (url: string, method: string) => Response | Promise<Response>) {
  const calls: Call[] = [];
  const mock = vi.fn((url: string, init: RequestInit = {}) => {
    const method = (init.method ?? "GET").toUpperCase();
    let body: unknown;
    if (typeof init.body === "string") {
      try {
        body = JSON.parse(init.body);
      } catch {
        body = init.body;
      }
    }
    calls.push({ url: String(url), method, body });
    return Promise.resolve(handler(String(url), method));
  });
  vi.stubGlobal("fetch", mock);
  return calls;
}

const AUTHORITY = { role_derived: "member", explicit_grants: [], explicit_none: true };
const PREVIEW = {
  organization_id: "org_acme",
  organization_label: "Acme",
  authority: AUTHORITY,
  expires_at: "2030-01-02T00:00:00Z",
};

function accepted(over: Record<string, unknown> = {}) {
  return {
    invitation_id: "inv_1",
    organization_id: "org_acme",
    authority: AUTHORITY,
    next_url: "/onboarding/responsibilities",
    replayed: false,
    profile: { display_name: "Jean Albany", needs_name: false },
    ...over,
  };
}

/** Route the two calls JoinOrg makes: exchange, then accept. */
function router(acceptBody: unknown, opts: { profilePatch?: number } = {}) {
  return (url: string, method: string) => {
    if (url.includes("/api/invitations/exchange")) return resp(200, { preview: PREVIEW });
    if (url.includes("/api/invitations/accept")) return resp(200, acceptBody);
    if (url.includes("/api/me/profile") && method === "PATCH") {
      return resp(opts.profilePatch ?? 200, { message: "Database error: down" });
    }
    return resp(200, {});
  };
}

beforeEach(() => {
  localStorage.setItem("api_token", "tok-join");
});

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

/** Drive the screen to the accepted state. */
async function acceptInvitation(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Accept invitation" }));
  await screen.findByText(/Invitation accepted/);
}

describe("JoinOrg — the name is asked only when nobody could give it to us", () => {
  it("does NOT ask when the identity token already carried a name", async () => {
    stubFetch(router(accepted()));
    render(<JoinOrg token="tok-join" onAccepted={() => {}} />);

    const user = userEvent.setup();
    await acceptInvitation(user);

    expect(screen.queryByLabelText("First name")).not.toBeInTheDocument();
  });

  it("asks when the server reports no name is on file", async () => {
    stubFetch(router(accepted({ profile: { display_name: null, needs_name: true } })));
    render(<JoinOrg token="tok-join" onAccepted={() => {}} />);

    const user = userEvent.setup();
    await acceptInvitation(user);

    expect(await screen.findByLabelText("First name")).toBeInTheDocument();
    expect(screen.getByLabelText("Last name")).toBeInTheDocument();
  });

  it("does not ask when an older server omits the profile field entirely", async () => {
    const body = accepted();
    delete (body as Record<string, unknown>).profile;
    stubFetch(router(body));
    render(<JoinOrg token="tok-join" onAccepted={() => {}} />);

    const user = userEvent.setup();
    await acceptInvitation(user);

    expect(screen.queryByLabelText("First name")).not.toBeInTheDocument();
  });
});

describe("JoinOrg — the name is saved before moving on", () => {
  it("PATCHes the profile, then routes", async () => {
    const calls = stubFetch(
      router(accepted({ profile: { display_name: null, needs_name: true } })),
    );
    const onAccepted = vi.fn();
    render(<JoinOrg token="tok-join" onAccepted={onAccepted} />);

    const user = userEvent.setup();
    await acceptInvitation(user);
    await user.type(await screen.findByLabelText("First name"), "Jean");
    await user.type(screen.getByLabelText("Last name"), "Albany");
    await user.click(screen.getByRole("button", { name: "Continue to getting started" }));

    await waitFor(() => expect(onAccepted).toHaveBeenCalled());
    const patch = calls.find((c) => c.method === "PATCH");
    expect(patch?.url).toContain("/api/me/profile");
    expect(patch?.body).toEqual({ display_name: "Jean Albany" });
  });

  it("does NOT route when the name could not be saved", async () => {
    stubFetch(
      router(accepted({ profile: { display_name: null, needs_name: true } }), {
        profilePatch: 500,
      }),
    );
    const onAccepted = vi.fn();
    render(<JoinOrg token="tok-join" onAccepted={onAccepted} />);

    const user = userEvent.setup();
    await acceptInvitation(user);
    await user.type(await screen.findByLabelText("First name"), "Jean");
    await user.type(screen.getByLabelText("Last name"), "Albany");
    await user.click(screen.getByRole("button", { name: "Continue to getting started" }));

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(onAccepted).not.toHaveBeenCalled();
  });

  it("keeps the action disabled until both parts are given", async () => {
    stubFetch(router(accepted({ profile: { display_name: null, needs_name: true } })));
    render(<JoinOrg token="tok-join" onAccepted={() => {}} />);

    const user = userEvent.setup();
    await acceptInvitation(user);

    const go = screen.getByRole("button", { name: "Continue to getting started" });
    expect(go).toBeDisabled();
    await user.type(await screen.findByLabelText("First name"), "Jean");
    expect(go).toBeDisabled();
    await user.type(screen.getByLabelText("Last name"), "Albany");
    await waitFor(() => expect(go).toBeEnabled());
  });
});

describe("JoinOrg — an entry invitation grants no membership", () => {
  it("shows no organization summary and offers to create one", async () => {
    stubFetch(router(accepted({ organization_id: null, next_url: "/" })));
    render(<JoinOrg token="tok-join" onAccepted={() => {}} />);

    const user = userEvent.setup();
    await acceptInvitation(user);

    // No fabricated organization label...
    expect(screen.queryByText("org_acme")).not.toBeInTheDocument();
    // ...and the next step is stated plainly.
    expect(
      screen.getByRole("button", { name: "Create your organization" }),
    ).toBeInTheDocument();
  });
});

describe("JoinOrg ? recoverable network failures", () => {
  it("retries exchange without a reload", async () => {
    let exchangeCount = 0;
    stubFetch((url) => {
      if (url.includes("/api/invitations/exchange")) {
        exchangeCount += 1;
        if (exchangeCount === 1) throw new Error("offline");
        return resp(200, { preview: PREVIEW });
      }
      return resp(200, {});
    });
    render(<JoinOrg token="tok-join" />);

    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Try again" }));

    expect(await screen.findByRole("button", { name: "Accept invitation" })).toBeVisible();
    expect(exchangeCount).toBe(2);
  });

  it("retries acceptance with its preview and waits for the success CTA", async () => {
    let acceptCount = 0;
    stubFetch((url) => {
      if (url.includes("/api/invitations/exchange")) return resp(200, { preview: PREVIEW });
      if (url.includes("/api/invitations/accept")) {
        acceptCount += 1;
        return acceptCount === 1 ? resp(500, {}) : resp(200, accepted());
      }
      return resp(200, {});
    });
    const onAccepted = vi.fn();
    render(<JoinOrg token="tok-join" onAccepted={onAccepted} />);

    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Accept invitation" }));
    await user.click(await screen.findByRole("button", { name: "Try again" }));

    expect(await screen.findByText(/Invitation accepted/)).toBeVisible();
    expect(acceptCount).toBe(2);
    expect(onAccepted).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Continue to getting started" }));
    expect(onAccepted).toHaveBeenCalledWith("/onboarding/responsibilities");
  });
});
