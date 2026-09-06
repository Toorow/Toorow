/**
 * Organization Settings — Members: the WRITE side.
 *
 * `OrgSettings.test.tsx` proves the section reads. It could not fail while the
 * section was read-only theatre, which is exactly how the gap survived: the
 * write routes were served and guarded since Story 21.5/21.8, a full panel was
 * written for them (`orgs/OrgDetailPanel.tsx`), and nothing mounted it. That file
 * was deleted on 2026-08-17; these cases are the only proof the capability has.
 *
 * What is pinned here is the contract with the server, not the layout:
 *   - a role change and a removal reach the right method on the right URL;
 *   - a removal passes through a confirmation first — it is irreversible for
 *     the person's access;
 *   - a 409 is reported as the LAST-OWNER GUARD and not swallowed (AD-9);
 *   - issuing/resending/revoking an invitation carries `Idempotency-Key`, which
 *     the server refuses to do without (422 `missing_idempotency_key`);
 *   - the single-use link is SHOWN, because the server hands it over once;
 *   - a 403 on the manage-gated read hides the write controls instead of
 *     rendering buttons whose every click would be refused.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import OrgSettings from "../shell/pages/OrgSettings";

const MEMBERS = {
  members: [
    { id: "m1", identity: "owner@example.com", role: "owner", status: "active", joined_at: "2026-01-05T09:00:00Z" },
    { id: "m2", identity: "analyst@example.com", role: "member", status: "active", joined_at: null },
  ],
};
const INVITATIONS = {
  items: [
    {
      invitation_id: "inv_1",
      role: "member",
      state: "pending",
      expires_at: "2026-08-06T10:00:00Z",
      issuer: "owner@example.com",
      available_actions: ["resend", "revoke"],
    },
  ],
};

interface Reply { ok: boolean; status: number; body: unknown }
const OK = (body: unknown): Reply => ({ ok: true, status: 200, body });
const FAIL = (status: number, body: unknown): Reply => ({ ok: false, status, body });

/** Routes by (method, url-fragment). Anything unrouted fails loudly rather than
 *  resolving to a shape the screen happens to tolerate. */
function stubApi(routes: Array<[string, RegExp, Reply | ((init: RequestInit) => Reply)]>) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const fetchMock = vi.fn(async (url: string, init: RequestInit = {}) => {
    calls.push({ url, init });
    const method = (init.method ?? "GET").toUpperCase();
    const hit = routes.find(([m, pattern]) => m === method && pattern.test(url));
    if (!hit) throw new Error(`unrouted ${method} ${url}`);
    const reply = typeof hit[2] === "function" ? hit[2](init) : hit[2];
    return { ok: reply.ok, status: reply.status, json: async () => reply.body };
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

const MEMBER_READ: [string, RegExp, Reply] = ["GET", /\/members$/, OK(MEMBERS)];
const INVITE_READ: [string, RegExp, Reply] = ["GET", /\/invitations$/, OK(INVITATIONS)];

afterEach(() => vi.unstubAllGlobals());

describe("Members — la liste nomme des gens", () => {
  /** Depuis la migration 111, `identity` est un `person_01K…`. La table n'affichait
   *  que lui : personne ne pouvait dire quelle ligne était qui, donc personne ne
   *  pouvait décider quel rôle changer. Le read-model résout l'e-mail vérifié ;
   *  l'écran doit le montrer, SANS perdre l'identifiant que l'API mute. */
  const CANONICAL = {
    members: [
      {
        id: "m1", identity: "person_01KYJ0M7QZXK4FSJX0XY3ZCP09", role: "owner",
        status: "active", joined_at: "2026-01-05T09:00:00Z",
        verified_email: "owner@example.com",
      },
      // Identité héritée d'avant la 111 : elle EST déjà un e-mail, et le
      // read-model ne lui trouve aucune ligne. Elle doit rester lisible.
      { id: "m2", identity: "analyst@example.com", role: "member", status: "active", joined_at: null },
    ],
  };

  it("affiche l'e-mail vérifié, et garde l'identifiant canonique sous les yeux", async () => {
    stubApi([["GET", /\/members$/, OK(CANONICAL)], INVITE_READ]);
    render(<OrgSettings orgId="org-1" section="members" />);

    const row = await screen.findByTestId("member-row-person_01KYJ0M7QZXK4FSJX0XY3ZCP09");
    expect(within(row).getByText("owner@example.com")).toBeInTheDocument();
    expect(within(row).getByText("person_01KYJ0M7QZXK4FSJX0XY3ZCP09")).toBeInTheDocument();
    // Le contrôle de rôle se nomme par la personne, pas par l'ULID.
    expect(within(row).getByLabelText("Role of owner@example.com")).toBeInTheDocument();
  });

  it("retombe sur l'identité quand aucun e-mail n'est résolu, plutôt que d'afficher un vide", async () => {
    stubApi([["GET", /\/members$/, OK(CANONICAL)], INVITE_READ]);
    render(<OrgSettings orgId="org-1" section="members" />);

    const row = await screen.findByTestId("member-row-analyst@example.com");
    expect(within(row).getByText("analyst@example.com")).toBeInTheDocument();
  });
});

describe("Members — roles", () => {
  it("PATCHes the member route when a role is changed", async () => {
    const calls = stubApi([
      MEMBER_READ,
      INVITE_READ,
      ["PATCH", /\/members\/analyst%40example\.com$/, OK({ updated: true })],
    ]);
    render(<OrgSettings orgId="org-1" section="members" />);

    const select = await screen.findByTestId("role-select-analyst@example.com");
    await userEvent.selectOptions(select, "admin");

    await waitFor(() => {
      const patch = calls.find((c) => (c.init.method ?? "GET") === "PATCH");
      expect(patch).toBeDefined();
      expect(patch!.url).toContain("/api/organizations/org-1/members/analyst%40example.com");
      expect(JSON.parse(String(patch!.init.body))).toEqual({ role: "admin" });
    });
  });

  it("reports a 409 as the last-owner guard rather than swallowing it", async () => {
    stubApi([
      MEMBER_READ,
      INVITE_READ,
      ["PATCH", /\/members\//, FAIL(409, { code: "conflict", message: "cannot drop the last active owner" })],
    ]);
    render(<OrgSettings orgId="org-1" section="members" />);

    await userEvent.selectOptions(await screen.findByTestId("role-select-owner@example.com"), "member");

    const warning = await screen.findByTestId("members-warning");
    expect(warning).toHaveTextContent(/cannot drop the last active owner/i);
    expect(warning).toHaveTextContent(/Nothing was modified/i);
  });
});

describe("Members — removal", () => {
  it("does not remove on the row button alone: a confirmation names the person first", async () => {
    const calls = stubApi([
      MEMBER_READ,
      INVITE_READ,
      ["DELETE", /\/members\//, OK({ removed: true })],
    ]);
    render(<OrgSettings orgId="org-1" section="members" />);

    await userEvent.click(await screen.findByTestId("remove-member-analyst@example.com"));

    // Nothing has been sent yet — the click opened a question, not a deletion.
    expect(calls.some((c) => (c.init.method ?? "GET") === "DELETE")).toBe(false);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("analyst@example.com")).toBeInTheDocument();

    await userEvent.click(within(dialog).getByRole("button", { name: /remove member/i }));

    await waitFor(() => {
      const del = calls.find((c) => (c.init.method ?? "GET") === "DELETE");
      expect(del).toBeDefined();
      expect(del!.url).toContain("/api/organizations/org-1/members/analyst%40example.com");
    });
  });

  it("surfaces the last-owner 409 on removal too", async () => {
    stubApi([
      MEMBER_READ,
      INVITE_READ,
      ["DELETE", /\/members\//, FAIL(409, { code: "conflict", message: "cannot remove the last active owner" })],
    ]);
    render(<OrgSettings orgId="org-1" section="members" />);

    await userEvent.click(await screen.findByTestId("remove-member-owner@example.com"));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: /remove member/i }));

    expect(await screen.findByTestId("members-warning")).toHaveTextContent(/cannot remove the last active owner/i);
  });
});

describe("Members — invitations", () => {
  it("issues with an Idempotency-Key and shows the single-use link", async () => {
    const calls = stubApi([
      MEMBER_READ,
      INVITE_READ,
      [
        "POST",
        /\/invitations$/,
        OK({
          invitation_id: "inv_2",
          state: "pending",
          delivery_handoff: { url: "https://app.example.com/join/token-abc", single_return: true },
        }),
      ],
    ]);
    render(<OrgSettings orgId="org-1" section="members" />);

    await userEvent.type(await screen.findByTestId("invite-identity"), "newcomer@example.com");
    await userEvent.click(screen.getByTestId("invite-submit"));

    await waitFor(() => {
      const post = calls.find((c) => (c.init.method ?? "GET") === "POST");
      expect(post).toBeDefined();
      const headers = post!.init.headers as Record<string, string>;
      // The server answers 422 `missing_idempotency_key` without it.
      expect(headers["Idempotency-Key"]).toBeTruthy();
      expect(JSON.parse(String(post!.init.body))).toMatchObject({
        invited_identity: "newcomer@example.com",
        role: "member",
        expires_in_hours: 48,
      });
    });

    // Shown once by the server: a screen that does not render it loses it.
    expect(await screen.findByTestId("invite-link")).toHaveValue("https://app.example.com/join/token-abc");
    expect(screen.getByTestId("invite-handoff")).toHaveTextContent(/Nothing was sent/i);
  });

  it("refuses a malformed grant at the field instead of posting it", async () => {
    const calls = stubApi([MEMBER_READ, INVITE_READ]);
    render(<OrgSettings orgId="org-1" section="members" />);

    await userEvent.type(await screen.findByTestId("invite-identity"), "newcomer@example.com");
    await userEvent.click(screen.getByRole("button", { name: /Grant project or datastream scope/i }));
    await userEvent.type(screen.getByLabelText("Project grants"), "proj_EXAMPLE:owner");
    await userEvent.click(screen.getByTestId("invite-submit"));

    expect(await screen.findByTestId("invite-error")).toHaveTextContent(/view\|edit\|manage/i);
    expect(calls.some((c) => (c.init.method ?? "GET") === "POST")).toBe(false);
  });

  it("resends and revokes through the invitation routes, each with a key", async () => {
    const calls = stubApi([
      MEMBER_READ,
      INVITE_READ,
      ["POST", /\/invitations\/inv_1\/revoke$/, OK({ invitation_id: "inv_1", state: "revoked" })],
    ]);
    render(<OrgSettings orgId="org-1" section="members" />);

    await userEvent.click(await screen.findByTestId("revoke-invitation-inv_1"));

    await waitFor(() => {
      const post = calls.find((c) => c.url.includes("/revoke"));
      expect(post).toBeDefined();
      expect((post!.init.headers as Record<string, string>)["Idempotency-Key"]).toBeTruthy();
    });
  });

  it("hides every write control when the server refuses the manage-gated read", async () => {
    stubApi([
      MEMBER_READ,
      ["GET", /\/invitations$/, FAIL(403, { code: "forbidden", message: "manage required" })],
    ]);
    render(<OrgSettings orgId="org-1" section="members" />);

    // The membership is still readable — that read is not manage-gated.
    expect(await screen.findByText("analyst@example.com")).toBeInTheDocument();
    expect(screen.queryByTestId("role-select-analyst@example.com")).not.toBeInTheDocument();
    expect(screen.queryByTestId("remove-member-analyst@example.com")).not.toBeInTheDocument();
    expect(screen.queryByTestId("invite-submit")).not.toBeInTheDocument();
    expect(screen.getByText(/reserved to the organization's owners and admins/i)).toBeInTheDocument();
  });

  it("does not present an empty invitation list when the read failed", async () => {
    stubApi([
      MEMBER_READ,
      ["GET", /\/invitations$/, FAIL(500, { code: "db_error", message: "Database error" })],
    ]);
    render(<OrgSettings orgId="org-1" section="members" />);

    expect(await screen.findByText(/Invitations could not be read/i)).toBeInTheDocument();
    expect(screen.queryByText("No invitations")).not.toBeInTheDocument();
  });
});
