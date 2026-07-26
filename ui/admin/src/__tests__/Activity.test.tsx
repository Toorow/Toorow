/**
 * Activity — the audit surface must show the audit registry, or say it could not.
 *
 * Until 2026-07-25 this page made no network call while /api/audit existed. It
 * rendered eight fabricated entries attributed to named people, four dead
 * buttons, and four invented alert rules of which three claimed to be "Active" —
 * telling the user they would be notified when an extraction fell behind. These
 * tests pin the three states and assert the fiction cannot return.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import Activity from "../shell/pages/Activity";

/** Named people and rules the old page invented. */
const FICTION = [
  "Jean Albany",
  "Marie Chen",
  "Northwind Studio",
  "Approved field",
  "Published mapping",
  "Resolved currency conflict",
  "Extraction delay",
  "Conversion-rate drop",
  "Spend cap exceeded",
  "Schema drift",
  "Slack #growth-alerts",
];

function expectNoFiction() {
  for (const label of FICTION) {
    expect(screen.queryByText(label)).not.toBeInTheDocument();
  }
}

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}
function fail(status: number, code: string, message: string): Response {
  return { ok: false, status, json: async () => ({ code, message }) } as unknown as Response;
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("Activity — error state", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(fail(500, "audit_query_error", "relation does not exist")),
    );
  });

  it("says the registry could not be read, offers a retry, and shows no entry", async () => {
    render(<Activity projectId="proj_test" />);

    await waitFor(() => {
      expect(screen.getByTestId("activity-error")).toBeInTheDocument();
    });
    expect(screen.getByTestId("activity-error")).toHaveTextContent(
      /Couldn't load the audit registry/i,
    );
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expectNoFiction();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("disables the export control when there is nothing to export", async () => {
    render(<Activity projectId="proj_test" />);

    await waitFor(() => {
      expect(screen.getByTestId("activity-error")).toBeInTheDocument();
    });
    expect(screen.getByTestId("export-log")).toBeDisabled();
  });
});

describe("Activity — empty state", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(ok({ rows: [], count: 0 })));
  });

  it("states that nothing has been recorded instead of listing entries", async () => {
    render(<Activity projectId="proj_test" />);

    await waitFor(() => {
      expect(screen.getByTestId("activity-empty")).toBeInTheDocument();
    });
    expect(screen.getByTestId("activity-empty")).toHaveTextContent(
      /No action has been recorded yet/i,
    );
    expectNoFiction();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("says plainly that no alert rule exists and nothing is being watched", async () => {
    render(<Activity projectId="proj_test" />);

    await waitFor(() => {
      expect(screen.getByTestId("rules-unavailable")).toBeInTheDocument();
    });
    expect(screen.getByTestId("rules-unavailable")).toHaveTextContent(
      /no notification will be sent/i,
    );
    // The dead "+ Add rule" buttons are gone, not merely inert.
    expect(screen.queryByRole("button", { name: /Add rule/i })).not.toBeInTheDocument();
  });
});

describe("Activity — ready state", () => {
  const ROWS = [
    {
      id: 12,
      identity: "svc:worker",
      action: "connection.created",
      provider_account: "act_1234",
      connection_ref: "conn_01JABC",
      created_at: "2026-07-22T16:42:00+00:00",
    },
    {
      id: 11,
      identity: "svc:worker",
      action: "connection.revoked",
      provider_account: null,
      connection_ref: "conn_01JDEF",
      created_at: "2026-07-21T09:15:00+00:00",
    },
  ];

  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(ok({ rows: ROWS, count: ROWS.length })));
  });

  it("renders exactly the audit fields the endpoint returns", async () => {
    render(<Activity projectId="proj_test" />);

    await waitFor(() => {
      expect(screen.getByRole("table")).toBeInTheDocument();
    });
    // The action code also appears in the filter <option> list, hence within().
    const table = screen.getByRole("table");
    expect(within(table).getByText("connection.created")).toBeInTheDocument();
    expect(screen.getByText("act_1234")).toBeInTheDocument();
    expect(screen.getByText("conn_01JABC")).toBeInTheDocument();
    expectNoFiction();
    // There is no "Result" field on an audit row, so there is no Result column.
    expect(screen.queryByRole("columnheader", { name: "Result" })).not.toBeInTheDocument();
  });

  it("offers a real action filter built from the actions actually present", async () => {
    render(<Activity projectId="proj_test" />);

    await waitFor(() => {
      expect(screen.getByTestId("action-filter")).toBeInTheDocument();
    });
    const options = Array.from(
      screen.getByTestId("action-filter").querySelectorAll("option"),
    ).map((o) => o.getAttribute("value"));
    expect(options).toEqual(["all", "connection.created", "connection.revoked"]);
  });
});
