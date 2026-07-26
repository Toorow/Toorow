/**
 * ReconciliationMethods — Story 44.1 moved the per-metric reconciliation list
 * (app.metric_procedures / GET /api/procedures) out of Context ▸ Procedures
 * and under Governance, read-only. Pins: it still reads the unchanged
 * endpoint, renders no write actions, and is honest when empty.
 */
import { render, screen, waitFor } from "@testing-library/react";
import ReconciliationMethods from "../shell/pages/ReconciliationMethods";

const PROC_ROW = {
  id: "mp_1",
  metric: "ROAS",
  method: "sum_then_divide",
  description: "Aggregate revenue and cost across every reporting source first.",
  owner: "Winston (Architect)",
  updated_at: "2026-07-24T10:00:00+00:00",
};

function resp(status: number, body: unknown) {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as unknown as Response;
}

function stubFetch(handler: (url: string) => Response | Promise<Response>) {
  const mock = vi.fn((url: string) => Promise.resolve(handler(String(url))));
  vi.stubGlobal("fetch", mock);
  return mock;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ReconciliationMethods — loading state", () => {
  it("shows a loading status line before the fetch resolves, then removes it", async () => {
    let resolveFetch: ((r: Response) => void) | null = null;
    const pending = new Promise<Response>((resolve) => {
      resolveFetch = resolve;
    });
    stubFetch((url) => (url.includes("/api/procedures") ? pending : resp(404, {})));

    render(<ReconciliationMethods projectId="p1" />);

    expect(screen.getByTestId("reconciliation-loading")).toBeInTheDocument();
    expect(screen.getByTestId("reconciliation-loading")).toHaveAttribute("role", "status");

    resolveFetch?.(resp(200, { procedures: [PROC_ROW] }));

    await waitFor(() => expect(screen.getByText("ROAS")).toBeInTheDocument());
    expect(screen.queryByTestId("reconciliation-loading")).not.toBeInTheDocument();
  });
});

describe("ReconciliationMethods — read-only Governance panel", () => {
  it("reads GET /api/procedures (unchanged endpoint) and renders no write action", async () => {
    const mock = stubFetch((url) =>
      url.includes("/api/procedures") ? resp(200, { procedures: [PROC_ROW] }) : resp(404, {}),
    );

    render(<ReconciliationMethods projectId="p1" />);

    await waitFor(() => expect(screen.getByText("ROAS")).toBeInTheDocument());
    expect(mock.mock.calls.some((c) => String(c[0]).includes("/api/procedures?project_id=p1"))).toBe(
      true,
    );
    expect(screen.queryByRole("button", { name: /Add procedure/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Edit/i })).not.toBeInTheDocument();
  });

  it("says no reconciliation methods are defined instead of showing an empty table", async () => {
    stubFetch((url) =>
      url.includes("/api/procedures") ? resp(200, { procedures: [] }) : resp(404, {}),
    );

    render(<ReconciliationMethods projectId="p1" />);

    await waitFor(() => {
      expect(screen.getByTestId("reconciliation-empty")).toBeInTheDocument();
    });
  });

  it("reports a load failure instead of a silent empty grid", async () => {
    stubFetch((url) => (url.includes("/api/procedures") ? resp(503, {}) : resp(404, {})));

    render(<ReconciliationMethods projectId="p1" />);

    await waitFor(() => {
      expect(screen.getByTestId("reconciliation-error")).toBeInTheDocument();
    });
  });
});
