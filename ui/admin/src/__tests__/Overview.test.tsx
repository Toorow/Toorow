import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import Overview from "../shell/pages/Overview43";

function response(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

const READY = {
  summary: {
    published_trust: "trusted",
    complete_through: "2026-07-19",
    no_data_reason: null,
    active_datastreams: 2,
    verified_datastreams: 2,
    attention_count: 0,
    active_work_count: 0,
    evidence_message:
      "Every active Datastream has current publication and verified coverage.",
    latest_test: {
      status: "ready",
      value: {
        passed: 48,
        total: 50,
        regressions: 2,
        result: "regressed",
        run_at: "2026-07-19T09:00:00Z",
      },
    },
  },
  attention: [],
  attention_total: 0,
  active_work: [],
  daily_insights: { status: "empty", items: [] },
  recent_renders: { status: "empty", items: [] },
};

const FICTION = [
  /€284,600/i,
  /ROAS up 8%/i,
  /Search spend flat/i,
  /Organic clicks recovered/i,
  /Q3 pacing vs plan/i,
  /91%/i,
  /Complete through 22 Jul/i,
];

function expectNoInventedEvidence() {
  for (const pattern of FICTION) {
    expect(screen.queryByText(pattern)).not.toBeInTheDocument();
  }
}

beforeEach(() => {
  vi.restoreAllMocks();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

test("loads one project-scoped read model and renders the server completion horizon", async () => {
  const fetchMock = vi.fn().mockResolvedValue(response(200, READY));
  vi.stubGlobal("fetch", fetchMock);

  render(<Overview projectId="project/acme" />);

  expect(
    screen.getByRole("status", { name: "Loading project evidence" }),
  ).toBeInTheDocument();
  expect(
    await screen.findByRole("heading", { name: "Trusted" }),
  ).toBeInTheDocument();
  expect(screen.getByText("Complete through 19 Jul 2026")).toBeInTheDocument();
  expect(screen.getByText("2 / 2")).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledWith(
    "/api/overview?project_id=project%2Facme",
    expect.objectContaining({ method: "GET", cache: "no-store" }),
  );
  expectNoInventedEvidence();
});

test("renders the empty project first action without claiming trust", async () => {
  const onAddDatastream = vi.fn();
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      response(200, {
        ...READY,
        summary: {
          ...READY.summary,
          published_trust: "no_data",
          complete_through: null,
          active_datastreams: 0,
          verified_datastreams: 0,
          no_data_reason: "empty_project",
        },
      }),
    ),
  );

  render(
    <Overview projectId="empty-project" onAddDatastream={onAddDatastream} />,
  );

  fireEvent.click(
    await screen.findByRole("button", { name: "Add Datastream" }),
  );
  expect(onAddDatastream).toHaveBeenCalledTimes(1);
  expect(screen.queryByText("Trusted")).not.toBeInTheDocument();
  expectNoInventedEvidence();
});

test("routes an only-disabled project to its Datastream overview", async () => {
  const onOpenDataOverview = vi.fn();
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      response(200, {
        ...READY,
        summary: {
          ...READY.summary,
          published_trust: "no_data",
          complete_through: null,
          active_datastreams: 0,
          verified_datastreams: 0,
          no_data_reason: "no_active_datastreams",
          evidence_message:
            "No Datastream is active, so published-data trust is unavailable.",
        },
      }),
    ),
  );

  render(
    <Overview
      projectId="inactive-project"
      onOpenDataOverview={onOpenDataOverview}
    />,
  );

  expect(
    await screen.findByRole("heading", { name: "No active Datastream" }),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("heading", { name: "Add your first Datastream" }),
  ).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Review Datastreams" }));
  expect(onOpenDataOverview).toHaveBeenCalledTimes(1);
});

test("renders a nondisclosing denied state and no project evidence", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(
        response(404, { code: "not_found", message: "Project not found" }),
      ),
  );

  render(<Overview projectId="other-project" />);

  expect(
    await screen.findByRole("heading", { name: "Project unavailable" }),
  ).toBeInTheDocument();
  expect(screen.queryByText("Verified inputs")).not.toBeInTheDocument();
  expectNoInventedEvidence();
});

test("keeps last-known-good completion and routes adverse evidence to exact Runs", async () => {
  const onOpenDatastream = vi.fn();
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      response(200, {
        ...READY,
        summary: {
          ...READY.summary,
          published_trust: "attention",
          evidence_message:
            "Last-known-good publications remain available, with newer evidence requiring review.",
          attention_count: 1,
        },
        attention_total: 1,
        attention: [
          {
            datastream_id: "ds-search",
            name: "Search performance",
            reason: "latest_run_failed",
            detail:
              "The latest run failed; the last verified publication remains available.",
            target: "runs",
          },
        ],
      }),
    ),
  );

  render(
    <Overview projectId="project-one" onOpenDatastream={onOpenDatastream} />,
  );

  expect(
    await screen.findByText("Last known good through 19 Jul 2026"),
  ).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "View evidence" }));
  expect(onOpenDatastream).toHaveBeenCalledWith("ds-search", "runs");
});

test("distinguishes unavailable optional evidence from an empty successful section", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      response(200, {
        ...READY,
        daily_insights: { status: "unavailable", items: [] },
        recent_renders: { status: "empty", items: [] },
      }),
    ),
  );

  render(<Overview projectId="project-one" />);

  expect(
    await screen.findByText("Insight evidence could not be loaded."),
  ).toBeInTheDocument();
  expect(screen.getByText("No persisted renders yet.")).toBeInTheDocument();
});

test("offers retry after a required read failure and never substitutes literals", async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(
      response(500, {
        code: "db_error",
        message: "Project overview is unavailable",
      }),
    )
    .mockResolvedValueOnce(response(200, READY));
  vi.stubGlobal("fetch", fetchMock);

  render(<Overview projectId="project-one" />);

  expect(
    await screen.findByRole("heading", {
      name: "Project trust could not be loaded",
    }),
  ).toBeInTheDocument();
  expectNoInventedEvidence();

  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await waitFor(() => {
    expect(
      screen.getByRole("heading", { name: "Trusted" }),
    ).toBeInTheDocument();
  });
  expect(fetchMock).toHaveBeenCalledTimes(2);
});

test("fails closed when the required read model is malformed", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      response(200, {
        ...READY,
        summary: { published_trust: "trusted" },
      }),
    ),
  );

  render(<Overview projectId="project-one" />);

  expect(
    await screen.findByRole("heading", {
      name: "Project trust could not be loaded",
    }),
  ).toBeInTheDocument();
});

test("never renders prior-project evidence under a new project scope", async () => {
  let resolveSecond: ((value: Response) => void) | undefined;
  const second = new Promise<Response>((resolve) => {
    resolveSecond = resolve;
  });
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(response(200, READY))
    .mockReturnValueOnce(second);
  vi.stubGlobal("fetch", fetchMock);

  const view = render(<Overview projectId="project-one" />);
  expect(
    await screen.findByRole("heading", { name: "Trusted" }),
  ).toBeInTheDocument();

  await act(async () => {
    view.rerender(<Overview projectId="project-two" />);
    await Promise.resolve();
  });

  expect(
    screen.getByRole("status", { name: "Loading project evidence" }),
  ).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Trusted" })).not.toBeInTheDocument();
  await act(async () => {
    resolveSecond?.(response(200, READY));
    await second;
  });
  expect(
    await screen.findByRole("heading", { name: "Trusted" }),
  ).toBeInTheDocument();
});


test("routes a persisted render with its exact object identity", async () => {
  const onOpenRender = vi.fn();
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      response(200, {
        ...READY,
        recent_renders: {
          status: "ready",
          items: [
            {
              id: "snap-42",
              kind: "get_card",
              title: "Persisted card",
              created_at: "2026-07-19T09:00:00Z",
              freshness: "live",
            },
          ],
        },
      }),
    ),
  );

  render(<Overview projectId="project-one" onOpenRender={onOpenRender} />);

  fireEvent.click(
    await screen.findByRole("button", { name: /Persisted card/i }),
  );
  expect(onOpenRender).toHaveBeenCalledWith("snap-42");
});


test("treats authentication expiry as a recoverable read error", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      response(401, { code: "unauthorized", message: "Authentication required" }),
    ),
  );

  render(<Overview projectId="project-one" />);

  expect(
    await screen.findByRole("heading", {
      name: "Project trust could not be loaded",
    }),
  ).toBeInTheDocument();
  expect(screen.queryByText("Project unavailable")).not.toBeInTheDocument();
});


test("rejects malformed optional artifact items instead of rendering undefined", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      response(200, {
        ...READY,
        recent_renders: { status: "ready", items: [{ id: "broken" }] },
      }),
    ),
  );

  render(<Overview projectId="project-one" />);

  expect(
    await screen.findByRole("heading", {
      name: "Project trust could not be loaded",
    }),
  ).toBeInTheDocument();
});
