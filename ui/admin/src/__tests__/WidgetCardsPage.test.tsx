/**
 * WidgetCardsPage — the widget catalog must list the server's card templates,
 * not four invented ones.
 *
 * Until 2026-07-25 this page had zero I/O, `void projectId`, four fabricated
 * cards ("KPI Hero Card", "Trend Sparkline Card", …) and a "Preview widget"
 * button on each that did nothing, while GET /api/cards/templates existed.
 */
import { render, screen, waitFor } from "@testing-library/react";
import WidgetCardsPage from "../WidgetCardsPage";

const FICTION = [
  "KPI Hero Card",
  "Trend Sparkline Card",
  "Channel Breakdown Donut",
  "Conversion Funnel Card",
];

function expectNoFiction() {
  for (const label of FICTION) {
    expect(screen.queryByText(label)).not.toBeInTheDocument();
  }
  // A control that does nothing is one more lie.
  expect(screen.queryByRole("button", { name: /Preview widget/i })).not.toBeInTheDocument();
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

describe("WidgetCardsPage — error state", () => {
  it("says the catalog could not be read, offers a retry, and lists no card", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(fail(500, "internal_error", "catalog unavailable")),
    );

    render(<WidgetCardsPage projectId="proj_test" />);

    await waitFor(() => {
      expect(screen.getByTestId("widgets-error")).toBeInTheDocument();
    });
    expect(screen.getByTestId("widgets-error")).toHaveTextContent(
      /Couldn't load the card catalog/i,
    );
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expectNoFiction();
  });

  it("reports a network failure rather than falling back to a catalog", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));

    render(<WidgetCardsPage projectId="proj_test" />);

    await waitFor(() => {
      expect(screen.getByTestId("widgets-error")).toBeInTheDocument();
    });
    expectNoFiction();
  });
});

describe("WidgetCardsPage — empty state", () => {
  it("states that no template is registered instead of showing any", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(ok({ templates: [] })));

    render(<WidgetCardsPage projectId="proj_test" />);

    await waitFor(() => {
      expect(screen.getByTestId("widgets-empty")).toBeInTheDocument();
    });
    expect(screen.getByTestId("widgets-empty")).toHaveTextContent(
      /No card template is registered/i,
    );
    expectNoFiction();
  });
});

describe("WidgetCardsPage — ready state", () => {
  const TEMPLATES = [
    {
      id: "spend_by_channel",
      title: "Spend by channel",
      answers_question: "Where did my media budget go?",
      widget_uri: "ui://widget/cards/spend-by-channel.html",
      kind: "kpi",
      required_metrics: ["media_spend"],
      required_dimensions: ["channel"],
      comment_builder: true,
      usable: false,
      missing: { metrics: ["media_spend"], dimensions: [] },
    },
  ];

  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue(ok({ templates: TEMPLATES }));
    vi.stubGlobal("fetch", fetchMock);
  });

  it("scopes the request to the project so usability is server-computed", async () => {
    render(<WidgetCardsPage projectId="proj_test" />);

    await waitFor(() => {
      expect(screen.getByTestId("card-spend_by_channel")).toBeInTheDocument();
    });
    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain("/api/cards/templates");
    expect(url).toContain("project_id=proj_test");
  });

  it("renders the server's template and its missing-input verdict", async () => {
    render(<WidgetCardsPage projectId="proj_test" />);

    await waitFor(() => {
      expect(screen.getByText("Spend by channel")).toBeInTheDocument();
    });
    expect(screen.getByText("Where did my media budget go?")).toBeInTheDocument();
    expect(screen.getByText("Missing inputs")).toBeInTheDocument();
    expect(screen.getByText("media_spend")).toBeInTheDocument();
    expectNoFiction();
  });
});
