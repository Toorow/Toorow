/**
 * DataWorkspace — "Published data: Trusted" must be a measurement, not a constant.
 *
 * The defect this pins: both /api/datastreams and /api/connections bailed with
 * `if (!resp.ok) return;` (and on an empty array), leaving a literal fleet of
 * four Datastreams and a literal summary asserting "Trusted", "Complete through
 * 22 Jul 2026", "6 healthy · 1 needs attention" and "4 connected sources".
 */
import { render, screen, waitFor } from "@testing-library/react";
import DataWorkspace from "../shell/pages/DataWorkspace";

function stubApi(opts: {
  datastreams?: { ok: boolean; body?: unknown };
  connections?: { ok: boolean; body?: unknown };
}) {
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    const u = String(url);
    const spec = u.includes("/api/connections")
      ? (opts.connections ?? { ok: true, body: { connections: [] } })
      : (opts.datastreams ?? { ok: true, body: [] });
    return Promise.resolve({
      ok: spec.ok,
      status: spec.ok ? 200 : 500,
      json: async () => spec.body ?? {},
      text: async () => "{}",
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const FICTION = [
  /Campaign performance/i,
  /Search performance/i,
  /Website acquisition/i,
  /Media plan 2026/i,
  /Complete through 22 Jul 2026/i,
  /6 healthy/i,
  /Acme Growth/i,
];

function expectNoFiction() {
  for (const pattern of FICTION) {
    expect(screen.queryByText(pattern)).not.toBeInTheDocument();
  }
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("DataWorkspace — load failure", () => {
  it("says the fleet could not be loaded and asserts no trust verdict", async () => {
    stubApi({ datastreams: { ok: false }, connections: { ok: false } });
    render(<DataWorkspace projectId="p1" />);

    await waitFor(() => {
      expect(screen.getByText(/Could not load the Datastreams/i)).toBeInTheDocument();
    });
    expect(screen.queryByText("Trusted")).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(
      screen.getAllByText(/Not available — the fleet could not be loaded/i).length,
    ).toBeGreaterThanOrEqual(1);
    expect(
      screen.getByText(/Not available — the authorizations could not be loaded/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/All authorizations usable/i)).not.toBeInTheDocument();
    expectNoFiction();
  });
});

describe("DataWorkspace — empty project", () => {
  it("renders an empty fleet as empty, with no trust claim", async () => {
    stubApi({ datastreams: { ok: true, body: [] }, connections: { ok: true, body: { connections: [] } } });
    render(<DataWorkspace projectId="p1" />);

    await waitFor(() => {
      expect(
        screen.getByText(/No Datastream is configured in this project yet/i),
      ).toBeInTheDocument();
    });
    expect(screen.queryByText("Trusted")).not.toBeInTheDocument();
    expect(screen.getByText(/No Datastream publishes into this project yet/i)).toBeInTheDocument();
    expect(screen.getByText(/No source connected yet/i)).toBeInTheDocument();
    expectNoFiction();
  });
});

describe("DataWorkspace — real fleet", () => {
  it("derives the verdict and the counts from the API rows", async () => {
    stubApi({
      datastreams: {
        ok: true,
        body: [
          {
            id: "ds1",
            name: "Real stream",
            module_name: "meta",
            published_state: "published",
            published_at: new Date().toISOString(),
            data_role: "Spend",
          },
          {
            id: "ds2",
            name: "Broken stream",
            module_name: "meta",
            published_state: "failed",
          },
        ],
      },
      connections: { ok: true, body: { connections: [{ id: "c1", health: { status: "ok" } }] } },
    });
    render(<DataWorkspace projectId="p1" />);

    await waitFor(() => {
      expect(screen.getByText("Real stream")).toBeInTheDocument();
    });
    // One of the two rows needs attention -> the verdict is NOT "Trusted".
    expect(screen.getByText("Attention")).toBeInTheDocument();
    expect(screen.getByText(/1 healthy · 1 need attention/i)).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    expectNoFiction();
  });

  it("hides unwired header and table controls", async () => {
    stubApi({});
    render(<DataWorkspace projectId="p1" />);

    await waitFor(() => {
      expect(
        screen.getByText(/No Datastream is configured in this project yet/i),
      ).toBeInTheDocument();
    });
    expect(screen.queryByRole("button", { name: /All statuses/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Search Datastreams/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Add Datastream/i })).not.toBeInTheDocument();
  });
});
