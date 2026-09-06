/**
 * TopicViewsPanel — the third binding family reaches a screen.
 *
 * Story 75-5 opened `GET/POST …/{topic_key}/views` and
 * `DELETE …/views/{binding_id}` and no screen reached them: the amendment's own
 * "Incomplete if" said so. What is proved here is what the panel LETS A PERSON
 * DO — read the declarations, compose a path from the relations the model
 * declares (never free text), declare, withdraw — and what it refuses to say:
 * an unreadable list is never rendered as an empty one, and a refusal is
 * rendered as the gesture that repairs it, never as its code.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import TopicViewsPanel from "../topics/TopicViewsPanel";

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}
function fail(status: number, code: string, message: string): Response {
  return { ok: false, status, json: async () => ({ code, message }) } as unknown as Response;
}

const CHOICES = {
  semantic_views: [
    {
      view_id: "sv_1",
      view_version_id: "svv_1",
      view_version_number: 3,
      status: "published",
      view_name: "Spend",
      relations: [
        {
          relation_id: "campaign_to_account",
          from: "campaigns",
          to: "accounts",
          cardinality: "many_to_one",
          fan_out_policy: "forbid",
        },
        {
          relation_id: "account_to_market",
          from: "accounts",
          to: "markets",
          cardinality: "many_to_one",
          fan_out_policy: "forbid",
        },
        // A relation of the SAME version that starts nowhere the chain reaches.
        // It is what would produce `path_not_chained` if the composer offered it
        // after the first leg — and the point is that it does not.
        {
          relation_id: "market_to_region",
          from: "markets",
          to: "regions",
          cardinality: "many_to_one",
          fan_out_policy: "forbid",
        },
      ],
    },
  ],
  truncated: false,
};

const BOUND = {
  views: [
    {
      binding_id: "atvb_1",
      view_id: "sv_1",
      view_version_id: "svv_1",
      view_version_number: 3,
      view_name: "Spend",
      status: "published",
      stale: false,
      note: null,
      paths: [
        {
          relation_ids: ["campaign_to_account", "account_to_market"],
          relations: [
            { relation_id: "campaign_to_account", from: "campaigns", to: "accounts" },
            { relation_id: "account_to_market", from: "accounts", to: "markets" },
          ],
          resolved: true,
          from: "campaigns",
          to: "markets",
          fan_out_policy: "forbid",
        },
      ],
    },
  ],
};

/** `views` is the topic's declarations; `choices` the versions it may pin.
 *  Either may be a body or a Response — an unreadable list is a case here. */
function routed(views: unknown, choices: unknown = CHOICES) {
  return vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    const u = String(url);
    if (init?.method === "POST" || init?.method === "DELETE") {
      return Promise.resolve(
        views instanceof Object && "ok" in (views as object) ? views : ok({}),
      );
    }
    if (u.includes("/answerable-topics/semantic-views")) {
      return Promise.resolve(
        choices instanceof Object && "ok" in (choices as object) ? choices : ok(choices),
      );
    }
    return Promise.resolve(
      views instanceof Object && "ok" in (views as object) ? views : ok(views),
    );
  });
}

function renderPanel() {
  return render(
    <TopicViewsPanel
      projectId="proj_test"
      topicKey="kpi"
      busy={false}
      setBusy={() => undefined}
    />,
  );
}

function writeCall(fetchMock: ReturnType<typeof vi.fn>, method: string) {
  return fetchMock.mock.calls.find(
    (c) => (c[1] as RequestInit | undefined)?.method === method,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("TopicViewsPanel — reading what a topic declares", () => {
  it("lists the View, its version and every path as the chain it is", async () => {
    vi.stubGlobal("fetch", routed(BOUND));
    renderPanel();

    await waitFor(() =>
      expect(screen.getByTestId("view-binding-atvb_1")).toBeInTheDocument(),
    );
    const row = screen.getByTestId("view-binding-atvb_1");
    expect(row).toHaveTextContent("Spend");
    expect(row).toHaveTextContent("v3");
    // The walk, drawn from the endpoints the model resolved — not a relation id
    // an operator has to translate in their head.
    expect(screen.getByTestId("view-paths-atvb_1")).toHaveTextContent(
      "campaigns → accounts → markets",
    );
    expect(screen.getByTestId("view-paths-atvb_1")).toHaveTextContent(
      "campaign_to_account › account_to_market",
    );
  });

  it("names the gesture when the topic reads no Semantic View", async () => {
    vi.stubGlobal("fetch", routed({ views: [] }));
    renderPanel();

    await waitFor(() => expect(screen.getByTestId("views-empty-kpi")).toBeInTheDocument());
    // An empty read is an ANSWER, and it names where a Semantic View comes from.
    expect(screen.getByTestId("views-empty-kpi")).toHaveTextContent(/Semantic Model/i);
    expect(screen.getByTestId("views-empty-kpi")).toHaveTextContent(/exact version/i);
  });

  it("says the list could not be read, and claims no absence", async () => {
    vi.stubGlobal("fetch", routed(fail(500, "internal_error", "views unavailable")));
    renderPanel();

    await waitFor(() =>
      expect(screen.getByTestId("views-unavailable-kpi")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("views-unavailable-kpi")).toHaveTextContent(
      /Couldn't read the Semantic Views/i,
    );
    // The lie this whole surface refuses: "read, and empty" is not "could not read".
    expect(screen.queryByTestId("views-empty-kpi")).not.toBeInTheDocument();
  });
});

describe("TopicViewsPanel — declaring a View and a path", () => {
  async function openForm(user: ReturnType<typeof userEvent.setup>) {
    renderPanel();
    await waitFor(() => expect(screen.getByTestId("views-empty-kpi")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: /Read a Semantic View/i }));
  }

  it("reads the versions only when the form opens, and offers the pinnable ones", async () => {
    const user = userEvent.setup();
    const fetchMock = routed({ views: [] });
    vi.stubGlobal("fetch", fetchMock);
    renderPanel();

    await waitFor(() => expect(screen.getByTestId("views-empty-kpi")).toBeInTheDocument());
    // Not on mount: a catalog of nine topics must not spend nine reads on a
    // question nobody has asked.
    expect(
      fetchMock.mock.calls.some((c) => String(c[0]).includes("/semantic-views")),
    ).toBe(false);

    await user.click(screen.getByRole("button", { name: /Read a Semantic View/i }));

    await waitFor(() => expect(screen.getByTestId("view-pick")).toBeInTheDocument());
    const read = fetchMock.mock.calls.find((c) => String(c[0]).includes("/semantic-views"))!;
    expect(String(read[0])).toBe(
      "/api/projects/proj_test/answerable-topics/semantic-views",
    );
    expect(
      within(screen.getByTestId("view-pick")).getAllByRole("option").map((o) => o.textContent),
    ).toEqual(["Choose a version…", "Spend — v3, published"]);
  });

  it("composes the path from the version's own relations, and only those that continue it", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", routed({ views: [] }));
    await openForm(user);
    await waitFor(() => expect(screen.getByTestId("view-pick")).toBeInTheDocument());

    // The second question does not exist until the first is answered.
    expect(screen.queryByTestId("view-path-composer")).not.toBeInTheDocument();
    await user.selectOptions(screen.getByTestId("view-pick"), "svv_1");

    // Before any leg: every relation of the chosen version, and NOT free text.
    expect(screen.getByTestId("view-leg-pick").tagName).toBe("SELECT");
    expect(
      within(screen.getByTestId("view-leg-pick")).getAllByRole("option").map((o) => o.textContent),
    ).toEqual([
      "Choose a relationship…",
      "campaign_to_account — campaigns → accounts",
      "account_to_market — accounts → markets",
      "market_to_region — markets → regions",
    ]);

    await user.selectOptions(screen.getByTestId("view-leg-pick"), "campaign_to_account");

    // THE SECOND QUESTION IS REDUCED BY THE FIRST. Only a relation leaving
    // `accounts` can continue; `market_to_region` leaves `markets` and would
    // earn `path_not_chained`, so it is not offered at all. The leg already
    // crossed is not offered either — that is `duplicate_relation`.
    expect(
      within(screen.getByTestId("view-leg-pick")).getAllByRole("option").map((o) => o.textContent),
    ).toEqual(["Choose a relationship…", "account_to_market — accounts → markets"]);
    expect(screen.getByTestId("view-chain")).toHaveTextContent("campaigns → accounts");
  });

  it("declares the composed chain, with an idempotency key", async () => {
    const user = userEvent.setup();
    const fetchMock = routed({ views: [] });
    vi.stubGlobal("fetch", fetchMock);
    await openForm(user);
    await waitFor(() => expect(screen.getByTestId("view-pick")).toBeInTheDocument());

    await user.selectOptions(screen.getByTestId("view-pick"), "svv_1");
    await user.selectOptions(screen.getByTestId("view-leg-pick"), "campaign_to_account");
    await user.selectOptions(screen.getByTestId("view-leg-pick"), "account_to_market");
    expect(screen.getByTestId("view-chain")).toHaveTextContent(
      "campaigns → accounts → markets",
    );

    // A half-composed chain is neither sent silently nor dropped silently.
    expect(screen.getByTestId("view-declare")).toBeDisabled();
    expect(screen.getByTestId("view-declare-blocked")).toBeInTheDocument();

    await user.click(screen.getByTestId("view-add-path"));
    await user.click(screen.getByTestId("view-declare"));

    await waitFor(() => expect(writeCall(fetchMock, "POST")).toBeTruthy());
    const post = writeCall(fetchMock, "POST")!;
    expect(String(post[0])).toBe(
      "/api/projects/proj_test/answerable-topics/kpi/views",
    );
    expect(JSON.parse(String((post[1] as RequestInit).body))).toEqual({
      semantic_view_version_id: "svv_1",
      allowed_paths: [{ relation_ids: ["campaign_to_account", "account_to_market"] }],
    });
    expect(
      (post[1] as RequestInit).headers as Record<string, string>,
    ).toHaveProperty("Idempotency-Key");
  });

  it("declares a View with no path at all, which is a legitimate declaration", async () => {
    const user = userEvent.setup();
    const fetchMock = routed({ views: [] });
    vi.stubGlobal("fetch", fetchMock);
    await openForm(user);
    await waitFor(() => expect(screen.getByTestId("view-pick")).toBeInTheDocument());

    await user.selectOptions(screen.getByTestId("view-pick"), "svv_1");
    // The screen SAYS what an empty declaration means, rather than looking unfinished.
    expect(screen.getByTestId("view-chain")).toHaveTextContent(/cross nothing/i);
    await user.click(screen.getByTestId("view-declare"));

    await waitFor(() => expect(writeCall(fetchMock, "POST")).toBeTruthy());
    expect(
      JSON.parse(String((writeCall(fetchMock, "POST")![1] as RequestInit).body)),
    ).toEqual({ semantic_view_version_id: "svv_1", allowed_paths: [] });
  });

  it("renders a refusal as the gesture that repairs it, never as its code", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return Promise.resolve(
          fail(
            422,
            "binding_conflict",
            "that exact Semantic View version is already bound to this topic",
          ),
        );
      }
      if (String(url).includes("/answerable-topics/semantic-views")) {
        return Promise.resolve(ok(CHOICES));
      }
      return Promise.resolve(ok({ views: [] }));
    });
    vi.stubGlobal("fetch", fetchMock);
    await openForm(user);
    await waitFor(() => expect(screen.getByTestId("view-pick")).toBeInTheDocument());

    await user.selectOptions(screen.getByTestId("view-pick"), "svv_1");
    await user.click(screen.getByTestId("view-declare"));

    await waitFor(() => expect(screen.getByTestId("view-refusal-kpi")).toBeInTheDocument());
    const refusal = screen.getByTestId("view-refusal-kpi");
    expect(refusal).toHaveTextContent(/already reads that View version/i);
    expect(refusal).toHaveTextContent(/Withdraw the existing declaration/i);
    // The store's word is not the reader's: a code is a diagnosis, and what a
    // reader needs is the move.
    expect(refusal).not.toHaveTextContent("binding_conflict");
  });

  it("names the gesture when the project has published no Semantic View", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", routed({ views: [] }, { semantic_views: [], truncated: false }));
    await openForm(user);

    await waitFor(() => expect(screen.getByTestId("view-choices-empty")).toBeInTheDocument());
    expect(screen.getByTestId("view-choices-empty")).toHaveTextContent(/Semantic Model/i);
    expect(screen.queryByTestId("view-pick")).not.toBeInTheDocument();
  });
});

describe("TopicViewsPanel — withdrawing a declaration", () => {
  it("withdraws through the governed route, with an idempotency key", async () => {
    const user = userEvent.setup();
    const fetchMock = routed(BOUND);
    vi.stubGlobal("fetch", fetchMock);
    renderPanel();

    await waitFor(() =>
      expect(screen.getByTestId("view-binding-atvb_1")).toBeInTheDocument(),
    );
    await user.click(screen.getByRole("button", { name: /Withdraw View/i }));

    await waitFor(() => expect(writeCall(fetchMock, "DELETE")).toBeTruthy());
    const del = writeCall(fetchMock, "DELETE")!;
    expect(String(del[0])).toBe(
      "/api/projects/proj_test/answerable-topics/kpi/views/atvb_1",
    );
    expect(
      (del[1] as RequestInit).headers as Record<string, string>,
    ).toHaveProperty("Idempotency-Key");
  });

  it("shows a pin whose View moved on as stale, and still names it", async () => {
    vi.stubGlobal(
      "fetch",
      routed({
        views: [
          { ...BOUND.views[0], status: "archived", stale: true, paths: [] },
        ],
      }),
    );
    renderPanel();

    await waitFor(() => expect(screen.getByTestId("view-stale-atvb_1")).toBeInTheDocument());
    // Dropped, it would read as "this topic declared nothing" — the absence lie.
    expect(screen.getByTestId("view-binding-atvb_1")).toHaveTextContent("Spend");
    expect(screen.getByTestId("view-paths-empty-atvb_1")).toHaveTextContent(
      /crosses nothing/i,
    );
  });
});
