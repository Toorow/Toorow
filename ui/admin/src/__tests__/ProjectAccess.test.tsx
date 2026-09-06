/**
 * Project Access — the grant lifecycle, not just the read (Story 46.4 AC2/AC10).
 *
 * The review that preceded this file found the surface rendering a read-only
 * table while the prepare/confirm/revoke endpoints existed server-side and
 * nothing called them. What is guarded here is that authority:
 *   - a capability change goes through prepare THEN confirm, never a direct
 *     write, and the frozen before/after is shown before anything applies;
 *   - the confirmation secret is issued and consumed, and never rendered;
 *   - revocation is the same contract with `after_capability: null`;
 *   - the owner floor is not offered at all;
 *   - a 409 says nothing was applied and drops the stale change;
 *   - a caller without `manage` gets no actionable control.
 *
 * The transport is the global fetch, because src/lib/apiFetch.ts is the single
 * seam every call goes through — stubbing fetch also proves the bearer travels.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { chooseOption } from "./radixJsdom";
import ProjectAccess from "../shell/pages/ProjectAccess";

function resp(status: number, body: unknown) {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as unknown as Response;
}

interface Call { url: string; init: RequestInit }

function stubFetch(handler: (url: string, init: RequestInit) => Response) {
  const calls: Call[] = [];
  const mock = vi.fn((url: string, init: RequestInit = {}) => {
    calls.push({ url: String(url), init });
    return Promise.resolve(handler(String(url), init));
  });
  vi.stubGlobal("fetch", mock);
  return calls;
}

const ENVELOPE = {
  schema_version: "project-access.v1",
  project: { id: "proj-1", name: "Acme Growth", organization: { id: "org-1", name: "Acme Media" } },
  caller_capability: "manage",
  people: [
    {
      identity: "owner@example.com",
      organization_role: "owner",
      membership_version: 3,
      explicit_grant: null,
      effective_capability: "manage",
      grant_source: "owner_floor",
    },
    {
      identity: "analyst@example.com",
      organization_role: "member",
      membership_version: 7,
      explicit_grant: "view",
      effective_capability: "view",
      grant_source: "explicit_grant",
    },
  ],
  handoffs: [],
};

const PREPARED = {
  change_id: "pgrantchg_1",
  state: "prepared",
  expires_at: "2026-07-29T10:15:00Z",
  identity: "analyst@example.com",
  before: "view",
  after: "edit",
  membership_version: 7,
};

function sent(call: Call): Record<string, unknown> {
  return JSON.parse(String(call.init.body ?? "{}"));
}

beforeEach(() => localStorage.setItem("api_token", "tok-access"));
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
});

function renderAccess(section: "people" | "handoffs" = "people") {
  return render(
    <ProjectAccess projectId="proj-1" section={section} onSectionChange={() => undefined} />,
  );
}

const capabilityFor = (identity: string) =>
  screen.getByRole("combobox", { name: `New capability for ${identity}` });

describe("Project Access — the read", () => {
  it("renders the effective membership and explicit-grant matrix through the authenticated seam", async () => {
    const calls = stubFetch(() => resp(200, ENVELOPE));
    renderAccess();

    expect(await screen.findByText("owner@example.com")).toBeInTheDocument();
    expect(screen.getByText("analyst@example.com")).toBeInTheDocument();
    expect(screen.getByText("owner_floor")).toBeInTheDocument();
    expect(screen.getByText("explicit_grant")).toBeInTheDocument();
    expect(calls[0].url).toBe("/api/projects/proj-1/access");
    expect(new Headers(calls[0].init.headers).get("Authorization")).toBe("Bearer tok-access");
  });

  it("does not invent access state when the authority is unavailable", async () => {
    stubFetch(() => { throw new Error("offline"); });
    renderAccess();

    expect(await screen.findByText(/No access state was inferred/i)).toBeInTheDocument();
    expect(screen.queryByText("owner@example.com")).not.toBeInTheDocument();
  });
});

describe("Project Access — the grant lifecycle", () => {
  it("changes a capability through prepare then confirm, showing the frozen diff first", async () => {
    const user = userEvent.setup();
    const calls = stubFetch((url, init) => {
      if (url.endsWith("/access")) return resp(200, ENVELOPE);
      if (url.endsWith("/grant-changes") && init.method === "POST") return resp(201, PREPARED);
      if (url.endsWith("/confirmations")) {
        return resp(201, { confirmation_id: "conf-1", confirmation_secret: "s3cret" });
      }
      return resp(200, { change_id: "pgrantchg_1", state: "confirmed" });
    });
    renderAccess();

    await screen.findByText("analyst@example.com");
    await chooseOption(user, capabilityFor("analyst@example.com"), "edit");
    await user.click(screen.getByRole("button", { name: "Prepare change" }));

    // The frozen change is shown BEFORE anything applies.
    expect(await screen.findByText("Review this change before it applies")).toBeInTheDocument();
    expect(screen.getByText("view → edit")).toBeInTheDocument();
    expect(screen.getByText("7")).toBeInTheDocument();

    const prepareCall = calls.find((c) => c.url.endsWith("/grant-changes"))!;
    expect(sent(prepareCall)).toEqual({ identity: "analyst@example.com", after_capability: "edit" });
    expect(new Headers(prepareCall.init.headers).get("Idempotency-Key")).toBeTruthy();
    // Preparing is not applying.
    expect(calls.some((c) => c.url.endsWith("/confirm"))).toBe(false);

    await user.click(screen.getByRole("button", { name: "Confirm change" }));

    await screen.findByText("analyst@example.com now has edit on this Project.");
    const confirmCall = calls.find((c) => c.url.endsWith("/confirm"))!;
    expect(confirmCall.url).toBe("/api/projects/proj-1/access/grant-changes/pgrantchg_1/confirm");
    expect(sent(confirmCall)).toEqual({ confirmation_id: "conf-1", confirmation_secret: "s3cret" });
    // The secret is consumed, never shown.
    expect(screen.queryByText(/s3cret/)).not.toBeInTheDocument();
    // The matrix is re-read from the server rather than patched locally.
    expect(calls.filter((c) => c.url === "/api/projects/proj-1/access")).toHaveLength(2);
  });

  it("revokes with the same contract and a null capability", async () => {
    const user = userEvent.setup();
    const calls = stubFetch((url, init) => {
      if (url.endsWith("/access")) return resp(200, ENVELOPE);
      if (url.endsWith("/grant-changes") && init.method === "POST") {
        return resp(201, { ...PREPARED, after: null });
      }
      if (url.endsWith("/confirmations")) {
        return resp(201, { confirmation_id: "conf-2", confirmation_secret: "s3cret" });
      }
      return resp(200, { change_id: "pgrantchg_1", state: "confirmed" });
    });
    renderAccess();

    await screen.findByText("analyst@example.com");
    await chooseOption(user, capabilityFor("analyst@example.com"), "Revoke");
    await user.click(screen.getByRole("button", { name: "Prepare change" }));

    expect(await screen.findByText("view → none (revoked)")).toBeInTheDocument();
    expect(sent(calls.find((c) => c.url.endsWith("/grant-changes"))!)).toEqual({
      identity: "analyst@example.com",
      after_capability: null,
    });

    await user.click(screen.getByRole("button", { name: "Confirm change" }));
    await screen.findByText("analyst@example.com no longer has an explicit grant on this Project.");
  });

  it("never offers to revoke the owner floor", async () => {
    stubFetch(() => resp(200, ENVELOPE));
    renderAccess();

    const ownerRow = (await screen.findByText("owner@example.com")).closest("tr")!;
    expect(within(ownerRow).getByText(/Owner floor/)).toBeInTheDocument();
    expect(within(ownerRow).queryByRole("button", { name: "Prepare change" })).not.toBeInTheDocument();
    expect(within(ownerRow).queryByRole("combobox")).not.toBeInTheDocument();
  });

  it("says nothing was applied when the frozen change went stale", async () => {
    const user = userEvent.setup();
    stubFetch((url, init) => {
      if (url.endsWith("/access")) return resp(200, ENVELOPE);
      if (url.endsWith("/grant-changes") && init.method === "POST") return resp(201, PREPARED);
      if (url.endsWith("/confirmations")) {
        return resp(201, { confirmation_id: "conf-3", confirmation_secret: "s3cret" });
      }
      return resp(409, { code: "conflict", message: "membership changed since the change was prepared" });
    });
    renderAccess();

    await screen.findByText("analyst@example.com");
    await chooseOption(user, capabilityFor("analyst@example.com"), "manage");
    await user.click(screen.getByRole("button", { name: "Prepare change" }));
    await user.click(await screen.findByRole("button", { name: "Confirm change" }));

    expect(await screen.findByText("No grant was changed")).toBeInTheDocument();
    expect(screen.getByText(/membership changed since the change was prepared/)).toBeInTheDocument();
    // The stale change is dropped rather than left confirmable.
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Confirm change" })).not.toBeInTheDocument(),
    );
    expect(screen.queryByText(/now has/)).not.toBeInTheDocument();
  });

  it("offers no actionable control to a caller without manage", async () => {
    stubFetch(() => resp(200, { ...ENVELOPE, caller_capability: "view" }));
    renderAccess();

    expect(await screen.findByText("View only")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Prepare change" })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });
});

describe("Project Access — handoffs", () => {
  it("creates a Project-scoped handoff that resumes the exact canonical route", async () => {
    const user = userEvent.setup();
    const calls = stubFetch((url, init) => {
      if (url.endsWith("/access")) return resp(200, ENVELOPE);
      if (url.endsWith("/handoffs") && init.method === "POST") {
        return resp(201, {
          handoff_id: "ho-1",
          state: "pending",
          expires_at: "2026-08-15T10:00:00Z",
          resume_ref: "/org/org-1/project/proj-1/access/people",
        });
      }
      return resp(200, {});
    });
    renderAccess("handoffs");

    await user.type(await screen.findByLabelText(/Identity \(leave empty/), "newcomer@example.com");
    await user.click(screen.getByRole("button", { name: "Create handoff" }));

    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/handoffs"))).toBe(true));
    expect(sent(calls.find((c) => c.url.endsWith("/handoffs"))!)).toEqual({
      identity: "newcomer@example.com",
      resume_ref: "/org/org-1/project/proj-1/access/people",
      expires_in_hours: 48,
    });
    expect(new Headers(calls.find((c) => c.url.endsWith("/handoffs"))!.init.headers)
      .get("Idempotency-Key")).toBeTruthy();
    expect(await screen.findByTestId("project-access-handoff-receipt"))
      .toHaveTextContent("Server receipt ho-1");
  });

  it("reports a refused handoff instead of claiming one exists", async () => {
    const user = userEvent.setup();
    stubFetch((url, init) => {
      if (url.endsWith("/access")) return resp(200, ENVELOPE);
      if (url.endsWith("/handoffs") && init.method === "POST") {
        return resp(422, { code: "invalid_request", message: "resume_ref must be a canonical route" });
      }
      return resp(200, {});
    });
    renderAccess("handoffs");

    await user.click(await screen.findByRole("button", { name: "Create handoff" }));

    expect(await screen.findByText("No handoff was created")).toBeInTheDocument();
    expect(screen.getByText(/resume_ref must be a canonical route/)).toBeInTheDocument();
    expect(screen.getByText("No pending handoffs")).toBeInTheDocument();
  });
});
