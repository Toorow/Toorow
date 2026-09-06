/**
 * Data -> Connectors: the catalogue, and the installation gesture it owes.
 *
 * The first four tests are the original ones — the screen must keep being an
 * evidence table and not a bank of project toggles.
 *
 * The rest pin execution-substrate `Incomplete if` 11: registering an
 * installation must not require a REST call by hand, a `READY` installation must
 * be verifiable again from here, and a Connector whose family owes a domain step
 * must be told which step that is instead of being offered a button that would
 * be refused.
 *
 * `fetch` is stubbed rather than `apiFetch`: `apiSeamGuard.test.ts` is what
 * proves the bearer is attached, and stubbing one level lower would hide it —
 * the same choice every workbench suite in this folder makes.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import ConnectorsCatalog from "../shell/pages/ConnectorsCatalog";

function envelope(items: unknown[]) {
  return {
    schema_version: "data-connectors.v1",
    project_ref: { object_type: "project", id: "p1" },
    generated_at: "2026-07-29T10:00:00Z",
    evidence_as_of: "2026-07-29T09:00:00Z",
    items,
    unavailable_reasons: [],
    allowed_actions: ["connector.inspect"],
  };
}

const CONNECTOR = {
  object_ref: { object_type: "connector", id: "generic@production" },
  connector_id: "generic",
  environment: "production",
  states: { installation: "READY", activation: "ACTIVE", contract: "versioned", coverage: "used" },
  evidence: { datastream_count: 2 },
  evidence_as_of: "2026-07-29T09:00:00Z",
  links: {},
};

/** The same row with another installation state. */
function connectorIn(state: string, id = "generic") {
  return {
    ...CONNECTOR,
    object_ref: { object_type: "connector", id: `${id}@production` },
    connector_id: id,
    states: { ...CONNECTOR.states, installation: state },
  };
}

function response(body: unknown, ok = true, status = ok ? 200 : 503): Response {
  return { ok, status, json: async () => body, text: async () => JSON.stringify(body) } as Response;
}

/**
 * A fetch stub that answers by ROUTE.
 *
 * The screen reads three surfaces (the project lens, one installation, one
 * verification) and writes two, so a single always-the-same stub would let a
 * test pass while the screen called the wrong endpoint entirely.
 */
type Reply = { body: unknown; ok?: boolean; status?: number };
function router(routes: Array<[RegExp, (url: string, init?: RequestInit) => Reply]>) {
  return vi.fn((url: string, init?: RequestInit) => {
    for (const [pattern, reply] of routes) {
      if (pattern.test(url)) {
        const answer = reply(url, init);
        return Promise.resolve(response(answer.body, answer.ok ?? true, answer.status ?? (answer.ok === false ? 500 : 200)));
      }
    }
    return Promise.reject(new Error(`unrouted call: ${url}`));
  });
}

const LENS: [RegExp, () => Reply] = [/^\/api\/projects\/p1\/connectors/, () => ({ body: envelope([CONNECTOR]) })];

function lensOf(items: unknown[]): [RegExp, () => Reply] {
  return [/^\/api\/projects\/p1\/connectors/, () => ({ body: envelope(items) })];
}

function installation(overrides: Record<string, unknown> = {}) {
  return {
    connector_name: "generic",
    environment: "production",
    state: "READY",
    catalog_availability: "selectable",
    catalog_status: "ready",
    safe_next_action: "no action required -- connector is available for tenant activation",
    responsible_actor: "automated",
    blocking_cause: null,
    last_verified_at: "2026-07-29T09:00:00Z",
    ...overrides,
  };
}

function verification(overrides: Record<string, unknown> = {}) {
  return {
    connector_name: "generic",
    environment: "production",
    verified: true,
    installation_state: "READY",
    last_outcome: "passed",
    evidence_class: "auth_check",
    first_seen_at: null,
    last_run_at: new Date().toISOString(),
    blocking_reason: null,
    synthetic_delivery: false,
    ttl_seconds: 3600,
    ...overrides,
  };
}

/**
 * The setup dialog, and nothing else on the screen.
 *
 * Scoped on purpose: the table cell and the dialog both name a setup state, and
 * an unscoped `findByText("Available")` would be satisfied by the row — which is
 * exactly the assertion that would keep passing if the dialog said nothing.
 */
async function dialog() {
  return within(await screen.findByRole("dialog"));
}

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

// ---------------------------------------------------------------------------
// The table it already was.
// ---------------------------------------------------------------------------

it("requires exact Project scope", async () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  render(<ConnectorsCatalog />);
  expect(await screen.findByRole("alert")).toHaveTextContent(/Select a Project/i);
  expect(fetchMock).not.toHaveBeenCalled();
});

it("renders installed immutable Connector contract evidence, not project toggles", async () => {
  const fetchMock = router([LENS]);
  vi.stubGlobal("fetch", fetchMock);
  render(<ConnectorsCatalog projectId="p1" />);
  expect(await screen.findByText("generic")).toBeInTheDocument();
  expect(screen.getByText("Versioned")).toBeInTheDocument();
  expect(screen.queryByRole("switch")).not.toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledWith("/api/projects/p1/connectors", expect.objectContaining({ cache: "no-store" }));
});

it("opens the Connector workbench through the canonical owner callback", async () => {
  vi.stubGlobal("fetch", router([LENS]));
  const open = vi.fn();
  render(<ConnectorsCatalog projectId="p1" onOpenConnector={open} />);
  fireEvent.click(await screen.findByText("generic"));
  expect(open).toHaveBeenCalledWith("generic@production");
});

it("rejects the legacy project module catalog shape", async () => {
  vi.stubGlobal("fetch", router([[/^\/api\/projects\/p1\/connectors/, () => ({ body: [{ module_name: "generic", enabled: true }] })]]));
  render(<ConnectorsCatalog projectId="p1" />);
  expect(await screen.findByRole("alert")).toHaveTextContent(/malformed/i);
});

// ---------------------------------------------------------------------------
// The setup state, said in the reader's words.
// ---------------------------------------------------------------------------

it("names a setup state a person recognises, never the stored word", async () => {
  vi.stubGlobal("fetch", router([lensOf([connectorIn("DOMAIN_PENDING")])]));
  render(<ConnectorsCatalog projectId="p1" />);
  expect(await screen.findByText("Waiting for its delivery address")).toBeInTheDocument();
  expect(screen.queryByText("Domain pending")).not.toBeInTheDocument();
});

it("offers no step on a Connector that was turned off, and says so", async () => {
  vi.stubGlobal("fetch", router([lensOf([connectorIn("DISABLED")])]));
  render(<ConnectorsCatalog projectId="p1" />);
  expect(await screen.findByText("Turned off")).toBeInTheDocument();
  expect(screen.getByText("No step here")).toBeInTheDocument();
});

// ---------------------------------------------------------------------------
// Registering an installation — the gesture that had no console surface.
// ---------------------------------------------------------------------------

it("asks which Connector first, and only then reads how it is set up", async () => {
  const calls: string[] = [];
  vi.stubGlobal("fetch", router([
    lensOf([CONNECTOR]),
    [/^\/api\/connectors\/available/, (url) => { calls.push(url); return { body: [{ connector_name: "ga4", display_name: "Google Analytics 4" }] }; }],
    [/^\/api\/connectors\/ga4\/installation/, (url) => { calls.push(url); return { body: installation({ connector_name: "ga4", state: "NOT_INSTALLED", catalog_availability: "unavailable" }) }; }],
    [/^\/api\/connectors\/ga4\/verification/, (url) => { calls.push(url); return { body: { connector_name: "ga4", verified: false } }; }],
  ]));
  render(<ConnectorsCatalog projectId="p1" />);
  fireEvent.click(await screen.findByRole("button", { name: "Set up a Connector" }));

  // Question one, alone on screen. The state cannot be read before it is answered.
  const panel = await dialog();
  const picker = await panel.findByLabelText("Which Connector do you want to set up?");
  expect(calls.some((url) => url.includes("/installation"))).toBe(false);

  fireEvent.change(picker, { target: { value: "ga4" } });
  fireEvent.click(panel.getByRole("button", { name: "Continue" }));

  expect(await panel.findByText("Not set up")).toBeInTheDocument();
  expect(calls.some((url) => url.startsWith("/api/connectors/ga4/installation"))).toBe(true);
});

it("registers the installation against the installation route, with an idempotency key", async () => {
  let installed = false;
  const posts: Array<{ url: string; init?: RequestInit }> = [];
  vi.stubGlobal("fetch", router([
    lensOf([connectorIn("NOT_INSTALLED")]),
    [/^\/api\/connectors\/generic\/installation/, (url, init) => {
      if (init?.method === "POST") { posts.push({ url, init }); installed = true; return { body: installation({ state: "VERIFYING" }) }; }
      return { body: installation({ state: installed ? "VERIFYING" : "NOT_INSTALLED" }) };
    }],
    [/^\/api\/connectors\/generic\/verification/, () => ({ body: { connector_name: "generic", verified: false } })],
  ]));
  render(<ConnectorsCatalog projectId="p1" />);
  fireEvent.click(await screen.findByRole("button", { name: "Set up" }));
  const setup = await dialog();
  fireEvent.click(await setup.findByRole("button", { name: "Set up this Connector" }));

  await waitFor(() => expect(posts).toHaveLength(1));
  expect(posts[0].url).toBe("/api/connectors/generic/installation");
  expect((posts[0].init?.headers as Record<string, string>)["Idempotency-Key"]).toBeTruthy();
  // The state moved on, and the screen now names the ONE step that follows.
  expect(await setup.findByRole("button", { name: "Verify now" })).toBeInTheDocument();
});

// ---------------------------------------------------------------------------
// A READY installation can be verified again — its evidence has a TTL.
// ---------------------------------------------------------------------------

it("offers Verify again on a READY Connector whose evidence is still fresh", async () => {
  vi.stubGlobal("fetch", router([
    LENS,
    [/^\/api\/connectors\/generic\/installation/, () => ({ body: installation() })],
    [/^\/api\/connectors\/generic\/verification/, () => ({ body: verification() })],
  ]));
  render(<ConnectorsCatalog projectId="p1" />);
  fireEvent.click((await screen.findAllByRole("button", { name: "Verify again" }))[0]);
  const panel = await dialog();
  expect(await panel.findByText("Available")).toBeInTheDocument();
  expect(await panel.findByRole("button", { name: "Verify again" })).toBeInTheDocument();
});

it("says a READY Connector's evidence has run out, and refreshes it through POST /verify", async () => {
  const posts: Array<{ url: string; init?: RequestInit }> = [];
  let stale = true;
  vi.stubGlobal("fetch", router([
    LENS,
    [/^\/api\/connectors\/generic\/installation/, () => ({ body: installation() })],
    [/^\/api\/connectors\/generic\/verify$/, (url, init) => { posts.push({ url, init }); stale = false; return { body: verification() }; }],
    [/^\/api\/connectors\/generic\/verification/, () => ({
      body: verification(stale ? { last_run_at: "2026-07-01T00:00:00Z", ttl_seconds: 3600 } : {}),
    })],
  ]));
  render(<ConnectorsCatalog projectId="p1" />);
  fireEvent.click((await screen.findAllByRole("button", { name: "Verify again" }))[0]);

  const panel = await dialog();
  expect(await panel.findByText("Available, check overdue")).toBeInTheDocument();
  fireEvent.click(await panel.findByRole("button", { name: "Verify again" }));

  await waitFor(() => expect(posts).toHaveLength(1));
  expect(posts[0].url).toBe("/api/connectors/generic/verify");
  expect(posts[0].init?.method).toBe("POST");
  expect(await panel.findByText("Available")).toBeInTheDocument();
});

// ---------------------------------------------------------------------------
// A family that owes a domain step is told WHICH step, not offered a refusal.
// ---------------------------------------------------------------------------

it("names the delivery-domain step instead of offering a verify that would be refused", async () => {
  vi.stubGlobal("fetch", router([
    lensOf([connectorIn("DOMAIN_PENDING")]),
    [/^\/api\/connectors\/generic\/installation/, () => ({ body: installation({ state: "DOMAIN_PENDING", blocking_cause: "domain_configuration_pending" }) })],
    [/^\/api\/connectors\/generic\/verification/, () => ({ body: { connector_name: "generic", verified: false } })],
  ]));
  render(<ConnectorsCatalog projectId="p1" />);
  fireEvent.click(await screen.findByRole("button", { name: "Review setup" }));

  const panel = await dialog();
  expect(await panel.findByText(/Configure this Connector's delivery domain/i)).toBeInTheDocument();
  expect(panel.queryByRole("button", { name: /Verify/i })).not.toBeInTheDocument();
});

it("says what a degraded Connector is blocked on, in words, and offers the one repair", async () => {
  vi.stubGlobal("fetch", router([
    lensOf([connectorIn("DEGRADED")]),
    [/^\/api\/connectors\/generic\/installation/, () => ({ body: installation({ state: "DEGRADED", blocking_cause: "dependency_unavailable" }) })],
    [/^\/api\/connectors\/generic\/verification/, () => ({ body: verification({ last_outcome: "failed", blocking_reason: "dependency_unavailable" }) })],
  ]));
  render(<ConnectorsCatalog projectId="p1" />);
  fireEvent.click(await screen.findByRole("button", { name: "Verify again" }));

  const panel = await dialog();
  expect(await panel.findByText(/something it depends on is not answering/i)).toBeInTheDocument();
  expect(screen.queryByText("dependency_unavailable")).not.toBeInTheDocument();
});

// ---------------------------------------------------------------------------
// Refusals become the gesture that repairs them.
// ---------------------------------------------------------------------------

it("turns a hidden-surface refusal into the person who can do it", async () => {
  vi.stubGlobal("fetch", router([
    LENS,
    [/^\/api\/connectors\/generic\/installation/, () => ({ body: installation() })],
    [/^\/api\/connectors\/generic\/verification/, () => ({ body: verification() })],
    [/^\/api\/connectors\/generic\/verify$/, () => ({ body: { code: "not_found", message: "Resource not found" }, ok: false, status: 404 })],
  ]));
  render(<ConnectorsCatalog projectId="p1" />);
  fireEvent.click((await screen.findAllByRole("button", { name: "Verify again" }))[0]);
  const panel = await dialog();
  fireEvent.click(await panel.findByRole("button", { name: "Verify again" }));

  expect(await panel.findByText(/Ask one to do it for you/i)).toBeInTheDocument();
  expect(screen.queryByText(/404/)).not.toBeInTheDocument();
});

it("carries the nondisclosing projection through: no state, no gesture, one sentence", async () => {
  vi.stubGlobal("fetch", router([
    LENS,
    // What a non-platform-admin actually receives: no `state`, no evidence.
    [/^\/api\/connectors\/generic\/installation/, () => ({
      body: { connector_name: "generic", catalog_availability: "selectable", catalog_status: "ready", safe_next_action: "contact platform support" },
    })],
  ]));
  render(<ConnectorsCatalog projectId="p1" />);
  fireEvent.click((await screen.findAllByRole("button", { name: "Verify again" }))[0]);

  const panel = await dialog();
  expect(await panel.findByText(/Ask a platform administrator to set this Connector up/i)).toBeInTheDocument();
  expect(panel.queryByRole("button", { name: "Verify now" })).not.toBeInTheDocument();
});
