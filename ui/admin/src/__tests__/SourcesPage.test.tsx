/**
 * Sources — the page must never open on invented provider accounts.
 *
 * The defect this pins: `if (!resp.ok) return; // keep mockup literals` plus
 * `if (connections.length > 0)` meant a failed load AND a genuinely empty
 * account list both rendered four fictional accounts ("Acme Ads", "Northwind
 * Search", …) with credential expiry dates, and the summary cards counted them.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import Sources from "../shell/pages/Sources";

function stubConnections(result: { ok: boolean; body?: unknown; status?: number }) {
  const fetchMock = vi.fn().mockImplementation(() =>
    Promise.resolve({
      ok: result.ok,
      status: result.status ?? (result.ok ? 200 : 500),
      json: async () => result.body ?? {},
      text: async () => "{}",
    }),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const FICTION = [/Acme Ads/i, /Northwind Search/i, /Northwind Studio/i, /Acme Growth/i, /Acme Group/i];

function expectNoFiction() {
  for (const pattern of FICTION) {
    expect(screen.queryByText(pattern)).not.toBeInTheDocument();
  }
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Sources — load failure", () => {
  it("says the accounts could not be loaded and lists none", async () => {
    stubConnections({ ok: false });
    render(<Sources projectId="p1" />);

    await waitFor(() => {
      expect(screen.getByText(/Could not load the provider accounts/i)).toBeInTheDocument();
    });
    expect(screen.getByRole("alert")).toHaveTextContent(/not an empty account list/i);
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expectNoFiction();
  });

  it("reports no summary figure for a load that failed", async () => {
    stubConnections({ ok: false });
    render(<Sources projectId="p1" />);

    await waitFor(() => {
      expect(screen.getByText(/Could not load the provider accounts/i)).toBeInTheDocument();
    });
    // "4 usable / 3 healthy / 1 needs attention" used to be reported here.
    expect(screen.queryByText("4")).not.toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(3);
  });
});

describe("Sources — empty account list", () => {
  it("renders an empty list as empty", async () => {
    stubConnections({ ok: true, body: { connections: [] } });
    render(<Sources projectId="p1" />);

    await waitFor(() => {
      expect(
        screen.getByText(/No provider account is connected to this project yet/i),
      ).toBeInTheDocument();
    });
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expectNoFiction();
    // The counts are the real zero, not the literal four.
    expect(screen.getAllByText("0").length).toBeGreaterThanOrEqual(3);
  });
});

describe("Sources — real accounts", () => {
  it("renders the accounts the API returned", async () => {
    stubConnections({
      ok: true,
      body: {
        connections: [
          {
            id: "c1",
            provider: "meta",
            nango_connection_id: "meta-real-1",
            health: { status: "ok" },
            active_datastream_count: 2,
            exposure: "owned",
            owner_org_name: "Real Org",
          },
        ],
      },
    });
    render(<Sources projectId="p1" />);

    await waitFor(() => {
      expect(screen.getByText("meta-real-1")).toBeInTheDocument();
    });
    expect(screen.getByText("Real Org")).toBeInTheDocument();
    expect(screen.getByText("2 Datastreams")).toBeInTheDocument();
    expectNoFiction();
  });

  it("mounts the real connection action without inventing a provider filter", async () => {
    stubConnections({ ok: true, body: { connections: [] } });
    render(<Sources projectId="p1" />);

    await waitFor(() => {
      expect(
        screen.getByText(/No provider account is connected to this project yet/i),
      ).toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: /Add connection/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /All providers/i })).not.toBeInTheDocument();
  });
});
describe("Sources — scope and account actions", () => {
  it("renders access denial separately from an operational error", async () => {
    stubConnections({ ok: false, status: 403 });
    render(<Sources projectId="private-project" />);

    await waitFor(() => expect(screen.getByText(/Access denied/i)).toBeInTheDocument());
    expect(screen.queryByText(/Could not load the provider accounts/i)).not.toBeInTheDocument();
  });

  it("uses the selected provider account label and exposes owner management", async () => {
    const fetchMock = stubConnections({
      ok: true,
      body: {
        connections: [{
          id: "c1",
          provider: "meta",
          nango_connection_id: "technical-id",
          account_label: "Real provider account",
          auth_path: "nango",
          can_manage: true,
          health: { status: "stale" },
          exposure: "owned",
          owner_org_name: "Real Org",
        }],
      },
    });
    render(<Sources projectId="p1" />);

    await waitFor(() => expect(screen.getByText("Real provider account")).toBeInTheDocument());
    expect(screen.queryByText("technical-id")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Manage" }));
    expect(screen.getByRole("region", { name: /Manage Real provider account/i })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Reconnect" })).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Revoke" })).toBeInTheDocument();
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(3));
  });

  it("does not expose credential mutations for a beneficiary organization", async () => {
    stubConnections({
      ok: true,
      body: {
        connections: [{
          id: "shared-c1",
          provider: "meta",
          nango_connection_id: "shared-id",
          account_label: "Shared account",
          can_manage: false,
          health: { status: "stale" },
          exposure: "provided_by_org",
          owner_org_name: "Provider Org",
        }],
      },
    });
    render(<Sources projectId="p1" />);

    await waitFor(() => expect(screen.getByText("Shared account")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Manage" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reconnect" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Revoke" })).not.toBeInTheDocument();
  });
});
