/**
 * 67-16 — the MCP hosts screen, which did not exist.
 *
 * `host_preflight.py` and the four `/api/mcp-hosts/*` routes shipped with Epic
 * 36; the only component that ever named them was French, mounted nowhere, and
 * has been deleted. `screens/orphans.md` listed the bind route as a door with no
 * screen behind it. These tests hold the screen that closes it.
 *
 * What they assert, in order of what would hurt most if it broke:
 *   - a bound host is listed with what it can actually do, and a REVOKED one
 *     stays listed with its cut — a lifecycle list that hides what was cut
 *     cannot evidence a revocation;
 *   - the destructive path is confirmed, and the confirmation says the cut is
 *     felt at the host's NEXT call rather than at token expiry;
 *   - the revocation carries an Idempotency-Key, and the same key survives a
 *     retry after a 5xx;
 *   - an empty list says why and names the gesture that fills it;
 *   - a failed read is a failure, never an empty list, and a 403 says who to ask.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import McpHostsPanel, { type McpCapabilityContext } from "./McpHostsPanel";

afterEach(() => {
  vi.restoreAllMocks();
});

const ORG = "org_acme01";

const ACTIVE: McpCapabilityContext = {
  id: "mcpctx_01HACTIVE",
  org_id: ORG,
  host: "opaque-host",
  workspace_id: "ws-42",
  workspace_type: "team",
  client_id: "client-1",
  endpoint_binding: "https://mcp.example.com/admin",
  enabled_profiles: ["insights", "operations", "governance"],
  workspace_evidence_hash: "a".repeat(64),
  interactive_presence_evidence_hash: "b".repeat(64),
  policy_version: "p1",
  catalog_version: "c1",
  created_at: "2026-08-10T09:00:00Z",
  revoked_at: null,
  revoked_by: null,
  revocation_reason: null,
  preflight_id: "hpf_01",
  preflight_host_key: "host-a",
  preflight_state: "bound",
  preflight_dated_at: "2026-08-09T08:00:00Z",
};

const REVOKED: McpCapabilityContext = {
  ...ACTIVE,
  id: "mcpctx_02REVOKED",
  host: "another-host",
  endpoint_binding: "https://mcp.example.com/read",
  enabled_profiles: ["insights"],
  revoked_at: "2026-08-15T12:00:00Z",
  revoked_by: "owner@example.com",
  revocation_reason: "Host decommissioned",
  preflight_id: "hpf_02",
  preflight_state: "bound",
  preflight_dated_at: "2026-08-01T08:00:00Z",
};

/** One fetch double routed by URL — list and revoke are different answers. */
function routeFetch(handlers: {
  list?: () => { ok: boolean; status: number; body: unknown };
  revoke?: () => { ok: boolean; status: number; body: unknown };
}) {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      const answer = String(input).includes("/revoke")
        ? handlers.revoke?.()
        : handlers.list?.();
      const resolved = answer ?? { ok: true, status: 200, body: { contexts: [] } };
      return {
        ok: resolved.ok,
        status: resolved.status,
        json: async () => resolved.body,
      } as unknown as Response;
    })
  );
  return calls;
}

// ---------------------------------------------------------------------------
// (a) The list answers the question the screen exists for.
// ---------------------------------------------------------------------------

it("lists a bound host with what it is allowed to do and when it was negotiated", async () => {
  routeFetch({ list: () => ({ ok: true, status: 200, body: { contexts: [ACTIVE] } }) });
  render(<McpHostsPanel orgId={ORG} />);

  const row = await screen.findByTestId(`mcp-host-row-${ACTIVE.id}`);
  expect(within(row).getByText("opaque-host")).toBeTruthy();
  expect(within(row).getByText(ACTIVE.endpoint_binding)).toBeTruthy();
  // The high-risk profiles are what a revocation removes, so they are what the
  // row shows. `insights` is the floor and is never advertised as a capability.
  expect(within(row).getByText("operations")).toBeTruthy();
  expect(within(row).getByText("governance")).toBeTruthy();
  expect(within(row).queryByText("insights")).toBeNull();
  // The dated preflight travels with the binding.
  expect(within(row).getByText("bound")).toBeTruthy();
});

it("keeps a revoked context listed, with its cut, and offers no button to re-cut it", async () => {
  routeFetch({ list: () => ({ ok: true, status: 200, body: { contexts: [ACTIVE, REVOKED] } }) });
  render(<McpHostsPanel orgId={ORG} />);

  const row = await screen.findByTestId(`mcp-host-row-${REVOKED.id}`);
  const cut = within(row).getByTestId(`mcp-host-revoked-${REVOKED.id}`);
  expect(cut.textContent).toContain("owner@example.com");
  expect(cut.textContent).toContain("Host decommissioned");
  expect(within(row).queryByTestId(`mcp-host-revoke-${REVOKED.id}`)).toBeNull();
  // The live one is still actionable.
  expect(screen.getByTestId(`mcp-host-revoke-${ACTIVE.id}`)).toBeTruthy();
});

// ---------------------------------------------------------------------------
// (b) The destructive path.
// ---------------------------------------------------------------------------

it("confirms before revoking and says the cut lands at the host's next call", async () => {
  routeFetch({ list: () => ({ ok: true, status: 200, body: { contexts: [ACTIVE] } }) });
  render(<McpHostsPanel orgId={ORG} />);

  await userEvent.click(await screen.findByTestId(`mcp-host-revoke-${ACTIVE.id}`));
  const confirm = await screen.findByTestId("mcp-hosts-confirm-revoke");
  expect(confirm).toBeTruthy();
  // The sentence that distinguishes THIS revocation from a token that lapses.
  expect(screen.getByText(/at its very next call/i)).toBeTruthy();
  expect(screen.getByText(/cannot be undone/i)).toBeTruthy();
});

it("cancelling the confirmation revokes nothing", async () => {
  const calls = routeFetch({
    list: () => ({ ok: true, status: 200, body: { contexts: [ACTIVE] } }),
  });
  render(<McpHostsPanel orgId={ORG} />);

  await userEvent.click(await screen.findByTestId(`mcp-host-revoke-${ACTIVE.id}`));
  await userEvent.click(await screen.findByTestId("mcp-hosts-cancel-revoke"));
  await waitFor(() => expect(screen.queryByTestId("mcp-hosts-confirm-revoke")).toBeNull());
  expect(calls.some((call) => call.url.includes("/revoke"))).toBe(false);
});

it("revoking posts with an Idempotency-Key and re-reads the list", async () => {
  let listed = [ACTIVE];
  const calls = routeFetch({
    list: () => ({ ok: true, status: 200, body: { contexts: listed } }),
    revoke: () => {
      listed = [{ ...ACTIVE, revoked_at: "2026-08-17T10:00:00Z", revoked_by: "me@example.com" }];
      return { ok: true, status: 200, body: { capability_context_id: ACTIVE.id } };
    },
  });
  render(<McpHostsPanel orgId={ORG} />);

  await userEvent.click(await screen.findByTestId(`mcp-host-revoke-${ACTIVE.id}`));
  await userEvent.click(await screen.findByTestId("mcp-hosts-confirm-revoke"));

  await waitFor(() => expect(screen.getByTestId(`mcp-host-revoked-${ACTIVE.id}`)).toBeTruthy());
  const revoke = calls.find((call) => call.url.includes("/revoke"));
  expect(revoke).toBeTruthy();
  expect(revoke?.init?.method).toBe("POST");
  const headers = revoke?.init?.headers as Record<string, string>;
  expect(headers["Idempotency-Key"]).toBeTruthy();
  // The list was re-read from the server rather than patched locally.
  expect(calls.filter((call) => !call.url.includes("/revoke")).length).toBeGreaterThan(1);
});

it("a 5xx keeps the same Idempotency-Key so a retry is one command, not two", async () => {
  let fail = true;
  const calls = routeFetch({
    list: () => ({ ok: true, status: 200, body: { contexts: [ACTIVE] } }),
    revoke: () => {
      if (fail) {
        fail = false;
        return { ok: false, status: 503, body: { message: "Host preflight unavailable" } };
      }
      return { ok: true, status: 200, body: { capability_context_id: ACTIVE.id } };
    },
  });
  render(<McpHostsPanel orgId={ORG} />);

  await userEvent.click(await screen.findByTestId(`mcp-host-revoke-${ACTIVE.id}`));
  await userEvent.click(await screen.findByTestId("mcp-hosts-confirm-revoke"));
  await screen.findByTestId("mcp-hosts-op-error");
  await userEvent.click(screen.getByTestId("mcp-hosts-confirm-revoke"));

  const keys = calls
    .filter((call) => call.url.includes("/revoke"))
    .map((call) => (call.init?.headers as Record<string, string>)["Idempotency-Key"]);
  expect(keys.length).toBe(2);
  expect(keys[0]).toBe(keys[1]);
});

// ---------------------------------------------------------------------------
// (c) Empty, denied, broken — three different sentences, never a blank table.
// ---------------------------------------------------------------------------

it("an empty list says why it is empty and names the gesture that fills it", async () => {
  routeFetch({ list: () => ({ ok: true, status: 200, body: { contexts: [] } }) });
  render(<McpHostsPanel orgId={ORG} />);

  const empty = await screen.findByTestId("mcp-hosts-empty");
  expect(empty.textContent).toContain("No MCP host is connected");
  // It names the install flow, not a deployment state and not a table name.
  expect(empty.textContent).toContain("preflight");
  expect(screen.queryByTestId("mcp-hosts-table")).toBeNull();
});

it("a 403 names who can read this, and shows no list at all", async () => {
  routeFetch({ list: () => ({ ok: false, status: 403, body: { code: "forbidden" } }) });
  render(<McpHostsPanel orgId={ORG} />);

  const denied = await screen.findByTestId("mcp-hosts-denied");
  expect(denied.textContent).toContain("owners and admins");
  expect(screen.queryByTestId("mcp-hosts-empty")).toBeNull();
  expect(screen.queryByTestId("mcp-hosts-table")).toBeNull();
});

it("a failed read is reported as a failure, never as an empty list", async () => {
  routeFetch({ list: () => ({ ok: false, status: 500, body: { code: "unavailable" } }) });
  render(<McpHostsPanel orgId={ORG} />);

  expect(await screen.findByTestId("mcp-hosts-load-error")).toBeTruthy();
  expect(screen.queryByTestId("mcp-hosts-empty")).toBeNull();
  expect(screen.queryByTestId("mcp-hosts-table")).toBeNull();
});
