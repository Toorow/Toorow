/**
 * Organization Settings — Account exposure: the OUTBOUND grant.
 *
 * The section used to filter the org's authorizations on `provided`, which the
 * server computes as `owner_org_id != org_id` (`org_members_api.py#_list_org_authorizations`) — that is
 * the INBOUND direction, what another owner exposes to us. It therefore showed
 * the one direction on which no action is possible, and the act the section is
 * named for (`README.md:83`, `glossary.md:93-101`: "Owner. Organization
 * Settings (exposure)") had no surface anywhere in the console.
 *
 * Pinned here: the two directions do not get mixed up again, the credential
 * identifier is CHOSEN rather than typed, and exposing/revoking reaches
 * `app.credential_account_grants` through the routes that already existed.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import OrgSettings from "../shell/pages/OrgSettings";

// The wire shape, not a convenient one. `_serialize_authorization` (me_api.py)
// sends `exposure` and `owner_org_name`; it has never sent `provided`, which is
// what these fixtures used to carry — so the split under test was only ever
// exercised against a field the server does not produce (AI-54).
const AUTHORIZATIONS = {
  authorizations: [
    // Ours: exposable by us.
    {
      id: "cred_owned", provider: "google_ads", account_label: "Google Ads — house",
      exposure: "owned", owner_org_id: "org-1", owner_org_name: "Acme",
    },
    // Someone else's, reaching us through a grant: not ours to grant on.
    {
      id: "cred_inbound", provider: "ga4", account_label: "GA4 — partner",
      exposure: "provided_by_org", owner_org_id: "org-2", owner_org_name: "Partner Agency",
    },
  ],
};
const ACCOUNTS = {
  accounts: [
    { credential_id: "cred_owned", external_account_id: "acc-1", label: "Account one", discovered_at: null },
    { credential_id: "cred_owned", external_account_id: "acc-2", label: "Account two", discovered_at: null },
  ],
};

interface Reply { ok: boolean; status: number; body: unknown }
const OK = (body: unknown): Reply => ({ ok: true, status: 200, body });

function stubApi(routes: Array<[string, RegExp, Reply]>) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit = {}) => {
    calls.push({ url, init });
    const method = (init.method ?? "GET").toUpperCase();
    const hit = routes.find(([m, pattern]) => m === method && pattern.test(url));
    if (!hit) throw new Error(`unrouted ${method} ${url}`);
    return { ok: hit[2].ok, status: hit[2].status, json: async () => hit[2].body };
  }));
  return calls;
}

const BASE: Array<[string, RegExp, Reply]> = [
  ["GET", /\/organizations\/[^/]+\/authorizations$/, OK(AUTHORIZATIONS)],
  ["GET", /\/credentials\/cred_owned\/accounts$/, OK(ACCOUNTS)],
  ["GET", /\/credentials\/cred_owned\/grants$/, OK({ grants: [] })],
];

afterEach(() => vi.unstubAllGlobals());

describe("Account exposure", () => {
  it("separates what we may expose from what is merely exposed to us", async () => {
    stubApi(BASE);
    render(<OrgSettings orgId="org-1" section="account-exposure" />);

    // Ours is offered as a choice…
    const chooser = await screen.findByTestId("exposure-credential");
    expect(chooser).toHaveTextContent("Google Ads — house");
    // …the inbound one is not: revoking it belongs to its owner.
    expect(chooser).not.toHaveTextContent("GA4 — partner");
    expect(screen.getByText("GA4 — partner")).toBeInTheDocument();
    expect(screen.getByText(/Revoking them belongs to that owner/i)).toBeInTheDocument();
  });

  it("names the party to ask on an inbound row, instead of leaving it inert", async () => {
    stubApi(BASE);
    render(<OrgSettings orgId="org-1" section="account-exposure" />);

    expect(
      await screen.findByText(
        "Exposed by Partner Agency. To end it, ask an owner or admin of Partner Agency to revoke it."
      )
    ).toBeInTheDocument();
  });

  it("does not ask for the connection identifier it just made you choose", async () => {
    stubApi(BASE);
    render(<OrgSettings orgId="org-1" section="account-exposure" />);

    await userEvent.selectOptions(await screen.findByTestId("exposure-credential"), "cred_owned");
    await screen.findByTestId("accounts-table");

    // The typed `cred_…` and its Load button are the question this section says
    // it does not ask. They are not rendered under the chooser that answers it.
    expect(screen.queryByTestId("credential-id-input")).not.toBeInTheDocument();
    expect(screen.queryByTestId("credential-load-button")).not.toBeInTheDocument();
  });

  it("reads the accounts of the SECOND connection chosen, not the first", async () => {
    const calls = stubApi([
      ["GET", /\/organizations\/[^/]+\/authorizations$/, OK({
        authorizations: [
          { id: "cred_a", provider: "google_ads", account_label: "A", exposure: "owned" },
          { id: "cred_b", provider: "ga4", account_label: "B", exposure: "owned" },
        ],
      })],
      ["GET", /\/credentials\/cred_a\/accounts$/, OK({ accounts: [
        { credential_id: "cred_a", external_account_id: "acc-a", label: "Account A", discovered_at: null },
      ] })],
      ["GET", /\/credentials\/cred_a\/grants$/, OK({ grants: [] })],
      ["GET", /\/credentials\/cred_b\/accounts$/, OK({ accounts: [
        { credential_id: "cred_b", external_account_id: "acc-b", label: "Account B", discovered_at: null },
      ] })],
      ["GET", /\/credentials\/cred_b\/grants$/, OK({ grants: [] })],
    ]);
    render(<OrgSettings orgId="org-1" section="account-exposure" />);

    const chooser = await screen.findByTestId("exposure-credential");
    await userEvent.selectOptions(chooser, "cred_a");
    expect(await screen.findByText("Account A")).toBeInTheDocument();

    await userEvent.selectOptions(chooser, "cred_b");
    expect(await screen.findByText("Account B")).toBeInTheDocument();
    expect(screen.queryByText("Account A")).not.toBeInTheDocument();
    expect(calls.some((c) => c.url.includes("/api/credentials/cred_b/accounts"))).toBe(true);
  });

  it("chooses the credential instead of asking for its identifier, and lists its accounts", async () => {
    const calls = stubApi(BASE);
    render(<OrgSettings orgId="org-1" section="account-exposure" />);

    await userEvent.selectOptions(await screen.findByTestId("exposure-credential"), "cred_owned");

    expect(await screen.findByTestId("accounts-table")).toBeInTheDocument();
    expect(screen.getByText("Account one")).toBeInTheDocument();
    await waitFor(() => {
      expect(calls.some((c) => c.url.includes("/api/credentials/cred_owned/accounts"))).toBe(true);
      expect(calls.some((c) => c.url.includes("/api/credentials/cred_owned/grants"))).toBe(true);
    });
  });

  it("exposes an account to this organization through the grant route", async () => {
    const calls = stubApi([
      ...BASE,
      ["POST", /\/credentials\/cred_owned\/accounts\/acc-1\/grants$/, OK({ granted: true })],
    ]);
    render(<OrgSettings orgId="org-1" section="account-exposure" />);

    await userEvent.selectOptions(await screen.findByTestId("exposure-credential"), "cred_owned");
    await userEvent.click(await screen.findByTestId("expose-grant-acc-1"));

    await waitFor(() => {
      const post = calls.find((c) => (c.init.method ?? "GET") === "POST");
      expect(post).toBeDefined();
      expect(post!.url).toContain("/api/credentials/cred_owned/accounts/acc-1/grants");
      // The grantee is this organization — the cross-org grantee is story 21.10.
      expect(JSON.parse(String(post!.init.body))).toEqual({ grantee_org_id: "org-1" });
    });
  });

  it("offers Revoke, not Expose, on an account already exposed here", async () => {
    stubApi([
      ["GET", /\/organizations\/[^/]+\/authorizations$/, OK(AUTHORIZATIONS)],
      ["GET", /\/credentials\/cred_owned\/accounts$/, OK(ACCOUNTS)],
      [
        "GET",
        /\/credentials\/cred_owned\/grants$/,
        OK({
          grants: [{
            id: "g1", credential_id: "cred_owned", external_account_id: "acc-1",
            grantee_org_id: "org-1", granted_by: null, created_at: null,
          }],
        }),
      ],
    ]);
    render(<OrgSettings orgId="org-1" section="account-exposure" />);

    await userEvent.selectOptions(await screen.findByTestId("exposure-credential"), "cred_owned");

    expect(await screen.findByTestId("revoke-grant-acc-1")).toBeInTheDocument();
    expect(screen.queryByTestId("expose-grant-acc-1")).not.toBeInTheDocument();
    // acc-2 carries no grant, so it keeps the other control.
    expect(screen.getByTestId("expose-grant-acc-2")).toBeInTheDocument();
  });

  it("says what to do when the organization owns no connection at all", async () => {
    stubApi([["GET", /\/authorizations$/, OK({ authorizations: [] })]]);
    render(<OrgSettings orgId="org-1" section="account-exposure" />);

    expect(await screen.findByText("No Authorization to expose")).toBeInTheDocument();
    expect(screen.getByText(/an Authorization appears here once a Source has been connected/i)).toBeInTheDocument();
    expect(screen.queryByTestId("exposure-credential")).not.toBeInTheDocument();
  });
});
