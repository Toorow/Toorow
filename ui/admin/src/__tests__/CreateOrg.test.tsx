/**
 * CreateOrg — the first-login screen, and the only place the platform ever
 * learns WHO the person is.
 *
 * Nothing upstream collects a name: the marketing form has one field (email),
 * the CRM stores the contact with name=None, and the app's Google flow does not
 * request an identity scope. Decided by Jean 2026-07-26: collect it BEFORE the
 * first organization exists — an organization has an owner, and a nameless
 * owner is what produces "jeanludovic.albany@gmail.com invited you to join
 * Acme Media" in the invitation another human receives.
 *
 * What is guarded here is the ORDERING and the honesty of the failure, not the
 * happy path alone:
 *   - the name is asked only when the profile has none (asked once, ever);
 *   - the name is PATCHed before POST /api/organizations;
 *   - if saving the name fails, the organization is NOT created — otherwise a
 *     transient error silently produces exactly the nameless owner this change
 *     exists to prevent;
 *   - Create stays disabled until both the person and the organization are
 *     named.
 *
 * The transport is the global fetch, because src/lib/apiFetch.ts is the single
 * seam every call goes through.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import CreateOrg from "../shell/pages/CreateOrg";

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
  headers: HeadersInit | undefined;
}

/** Stub fetch and record (url, method, parsed body) for ordering assertions. */
function stubFetch(handler: (url: string, method: string) => Response | Promise<Response>) {
  const calls: Call[] = [];
  const mock = vi.fn((url: string, init: RequestInit = {}) => {
    const method = (init.method ?? "GET").toUpperCase();
    let body: unknown = undefined;
    if (typeof init.body === "string") {
      try {
        body = JSON.parse(init.body);
      } catch {
        body = init.body;
      }
    }
    calls.push({ url: String(url), method, body, headers: init.headers });
    return Promise.resolve(handler(String(url), method));
  });
  vi.stubGlobal("fetch", mock);
  return calls;
}

const CREATED_ORG = {
  id: "org_acme",
  name: "Acme Media",
  slug: "acme-media",
  status: "active",
};

const isProfile = (url: string) => url.includes("/api/me/profile");
const isOrgs = (url: string) => url.includes("/api/entry/scope");
const isConfirmation = (url: string) => url.endsWith("/api/entry/scope/confirmation");

beforeEach(() => {
  localStorage.setItem("api_token", "tok-createorg");
});

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

// ---------------------------------------------------------------------------
// Asking (and not asking)
// ---------------------------------------------------------------------------

describe("CreateOrg — collecting who you are", () => {
  it("asks for a name when the profile has none", async () => {
    stubFetch((url) => (isProfile(url) ? resp(200, { display_name: null }) : resp(201, CREATED_ORG)));
    render(<CreateOrg welcome />);

    expect(await screen.findByLabelText("First name")).toBeInTheDocument();
    expect(screen.getByLabelText("Last name")).toBeInTheDocument();
  });

  it("does NOT ask again when the profile already carries a name", async () => {
    stubFetch((url) =>
      isProfile(url) ? resp(200, { display_name: "Jean Albany" }) : resp(201, CREATED_ORG),
    );
    render(<CreateOrg />);

    // Wait for the profile read to settle before asserting an absence.
    await waitFor(() => expect(screen.getByLabelText("Organization name")).toBeInTheDocument());
    await waitFor(() => expect(screen.queryByLabelText("First name")).not.toBeInTheDocument());
  });

  it("falls back to asking when the profile cannot be read, rather than skipping the question", async () => {
    stubFetch((url) => (isProfile(url) ? resp(500, { code: "db_error" }) : resp(201, CREATED_ORG)));
    render(<CreateOrg welcome />);

    expect(await screen.findByLabelText("First name")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Ordering — the point of the change
// ---------------------------------------------------------------------------

describe("CreateOrg — the name is saved before the organization exists", () => {
  it("PATCHes the profile first, then creates the organization", async () => {
    const calls = stubFetch((url) => {
      if (isProfile(url)) return resp(200, { display_name: null });
      if (isConfirmation(url)) {
        return resp(201, {
          confirmation_id: "econf_entry",
          confirmation_secret: "ecfs_server_secret",
        });
      }
      return resp(201, CREATED_ORG);
    });
    render(<CreateOrg welcome />);

    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("First name"), "Jean");
    await user.type(screen.getByLabelText("Last name"), "Albany");
    await user.type(screen.getByLabelText("Organization name"), "Acme Media");
    await user.click(screen.getByRole("button", { name: "Create organization" }));

    await screen.findByText("Organization created");

    const writes = calls.filter((c) => c.method !== "GET");
    expect(writes.map((c) => c.method)).toEqual(["PATCH", "POST", "POST"]);
    expect(writes[0].url).toContain("/api/me/profile");
    // Two inputs, one stored display_name — app.user_profiles has no
    // given/family columns today.
    expect(writes[0].body).toEqual({ display_name: "Jean Albany" });
    expect(writes[1].url).toContain("/api/entry/scope");
    expect(writes[1].body).toEqual(
      expect.objectContaining({
        organization_name: "Acme Media",
        organization_slug: "acme-media",
        project_name: "First project",
        project_slug: "first-project",
      }),
    );
  });

  it("does NOT create the organization when the name could not be saved", async () => {
    const calls = stubFetch((url, method) => {
      if (isProfile(url) && method === "GET") return resp(200, { display_name: null });
      if (isProfile(url)) return resp(500, { code: "db_error", message: "Database error: down" });
      return resp(201, CREATED_ORG);
    });
    render(<CreateOrg welcome />);

    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("First name"), "Jean");
    await user.type(screen.getByLabelText("Last name"), "Albany");
    await user.type(screen.getByLabelText("Organization name"), "Acme Media");
    await user.click(screen.getByRole("button", { name: "Create organization" }));

    // The screen says it failed...
    expect(await screen.findByText("Organization not created")).toBeInTheDocument();
    // ...and no organization was ever POSTed. This is the whole point: a
    // transient failure must not leave an owner with no name.
    expect(calls.some((c) => c.method === "POST" && isOrgs(c.url))).toBe(false);
  });

  it("skips the profile write entirely for somebody who already has a name", async () => {
    const calls = stubFetch((url) =>
      isProfile(url) ? resp(200, { display_name: "Jean Albany" }) : resp(201, CREATED_ORG),
    );
    render(<CreateOrg />);

    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Organization name"), "Acme Media");
    await user.click(screen.getByRole("button", { name: "Create organization" }));

    await screen.findByText("Organization created");
    expect(calls.filter((c) => c.method === "PATCH")).toHaveLength(0);
  });
});

describe("CreateOrg — disabled-auth local compatibility", () => {
  it("creates only the organization and leaves the first project to the next screen", async () => {
    const calls = stubFetch((url) =>
      isProfile(url) ? resp(200, { display_name: "Jean Albany" }) : resp(201, CREATED_ORG),
    );
    render(<CreateOrg creationMode="organization-only" />);

    const user = userEvent.setup();
    expect(await screen.findByLabelText("Organization name")).toBeInTheDocument();
    expect(screen.queryByLabelText("First project")).not.toBeInTheDocument();
    expect(screen.getByText(/first project comes next/i)).toBeInTheDocument();

    await user.type(screen.getByLabelText("Organization name"), "Acme Media");
    await user.click(screen.getByRole("button", { name: "Create organization" }));
    await screen.findByText("Organization created");

    const writes = calls.filter((call) => call.method !== "GET");
    expect(writes).toHaveLength(1);
    expect(writes[0].url).toMatch(/\/api\/organizations$/);
    expect(writes[0].body).toEqual({ name: "Acme Media", slug: "acme-media" });
  });
});

// ---------------------------------------------------------------------------
// Gating
// ---------------------------------------------------------------------------

describe("CreateOrg — Create is gated on both names", () => {
  it("stays disabled while the person is unnamed, even with a valid organization name", async () => {
    stubFetch((url) => (isProfile(url) ? resp(200, { display_name: null }) : resp(201, CREATED_ORG)));
    render(<CreateOrg welcome />);

    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Organization name"), "Acme Media");

    const create = screen.getByRole("button", { name: "Create organization" });
    expect(create).toBeDisabled();

    // A first name alone is not enough either.
    await user.type(screen.getByLabelText("First name"), "Jean");
    expect(create).toBeDisabled();

    await user.type(screen.getByLabelText("Last name"), "Albany");
    await waitFor(() => expect(create).toBeEnabled());
  });
});
