/**
 * WidgetCardsPage — the widget catalog must list the server's card templates,
 * not four invented ones.
 *
 * Until 2026-07-25 this page had zero I/O, `void projectId`, four fabricated
 * cards ("KPI Hero Card", "Trend Sparkline Card", …) and a "Preview widget"
 * button on each that did nothing, while GET /api/cards/templates existed.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import WidgetCardsPage from "../WidgetCardsPage";
import { RouterProvider } from "../shell/router";

/** The screen is a LENS of a project workspace, and it now builds addresses for
 *  the objects its rows name — so it is mounted at the address the shell mounts
 *  it at, not in a vacuum. Rendering it outside a router would prove the links
 *  against a scope no operator is ever in. */
const ORG = "org_EXAMPLE";
const TOPICS_ADDRESS = (projectId: string) =>
  `/org/${ORG}/project/${projectId}/analyze/reports/lens/topics`;

function renderPage(projectId = "proj_test") {
  window.history.replaceState({}, "", TOPICS_ADDRESS(projectId));
  return render(
    <RouterProvider>
      <WidgetCardsPage projectId={projectId} />
    </RouterProvider>,
  );
}

/** The one POST this screen sent, whichever read happened to precede it.
 *  Indexing `calls[1]` tied every write assertion to how many reads the form
 *  makes before it opens — a form that starts listing what it used to ask the
 *  operator to retype legitimately makes one more. */
function postCall(fetchMock: ReturnType<typeof vi.fn>) {
  return fetchMock.mock.calls.find(
    (c) => (c[1] as RequestInit | undefined)?.method === "POST",
  );
}

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
  window.history.replaceState({}, "", "/");
});

describe("WidgetCardsPage — error state", () => {
  it("says the catalog could not be read, offers a retry, and lists no card", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(fail(500, "internal_error", "catalog unavailable")),
    );

    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId("widgets-error")).toBeInTheDocument();
    });
    expect(screen.getByTestId("widgets-error")).toHaveTextContent(
      /The topic catalog could not be read/i,
    );
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expectNoFiction();
  });

  it("reports a network failure rather than falling back to a catalog", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));

    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId("widgets-error")).toBeInTheDocument();
    });
    expectNoFiction();
  });
});

describe("WidgetCardsPage — empty state", () => {
  it("states that no template is registered instead of showing any", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(ok({ templates: [] })));

    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId("widgets-empty")).toBeInTheDocument();
    });
    expect(screen.getByTestId("widgets-empty")).toHaveTextContent(
      /answers no question yet/i,
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
    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId("card-spend_by_channel")).toBeInTheDocument();
    });
    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain("/api/cards/templates");
    expect(url).toContain("project_id=proj_test");
  });

  it("renders the server's template and its missing-input verdict", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getByText("Spend by channel")).toBeInTheDocument();
    });
    expect(screen.getByText("Where did my media budget go?")).toBeInTheDocument();
    // Scoped to the CARD: the catalog filter offers the same verdict as a value
    // to narrow by, deliberately in the same words — one notion, one word.
    const card = within(screen.getByTestId("card-spend_by_channel"));
    expect(card.getByText("Missing inputs")).toBeInTheDocument();
    expect(card.getByText("media_spend")).toBeInTheDocument();
    expectNoFiction();
  });
});

/**
 * Story 52.1 — the catalog is a governed object, so the screen carries its three
 * verbs. Without these the routes shipped with no door, which is the failure this
 * repository names as its most common one.
 */
describe("WidgetCardsPage — add, reword and retire", () => {
  const TEMPLATES = [
    {
      id: "kpi",
      title: "KPI Overview",
      answers_question: "How are my KPIs evolving?",
      kind: "kpi",
      usable: true,
    },
  ];

  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue(ok({ templates: TEMPLATES }));
    vi.stubGlobal("fetch", fetchMock);
  });

  // Review finding C-10: retiring is irreversible from this screen (create on the
  // same key is refused, a new version leaves the head retired, no unretire route
  // exists), so the single click had to become a confirmed one. That is why this
  // test now clicks twice — the route and the header it asserts are unchanged.
  it("retires a topic through the governed route, with an idempotency key", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByTestId("card-kpi")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Retire" }));
    await user.click(screen.getByRole("button", { name: "Retire this topic" }));

    await waitFor(() => expect(postCall(fetchMock)).toBeTruthy());
    const [url, init] = postCall(fetchMock)!;
    expect(String(url)).toBe("/api/projects/proj_test/answerable-topics/kpi/retire");
    expect((init as RequestInit).method).toBe("POST");
    const headers = (init as RequestInit).headers as Record<string, string>;
    expect(headers["Idempotency-Key"]).toBeTruthy();
  });

  it("rewording an inherited default creates its first stored version", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByTestId("card-kpi")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /Reword/i }));
    await user.click(screen.getByRole("button", { name: /Save new version/i }));

    await waitFor(() => expect(postCall(fetchMock)).toBeTruthy());
    // An entry with no `origin` was never stored, so the write goes to the
    // collection route — appending a version to a head that does not exist yet
    // would 404.
    expect(String(postCall(fetchMock)![0])).toBe(
      "/api/projects/proj_test/answerable-topics",
    );
  });

  it("offers adding a topic this project invents", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByTestId("card-kpi")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /Add topic/i })).toBeEnabled();
  });

  // C-10 — the confirmation is what makes the action reversible up to the last
  // click. Without it, one misclick removes a question the operator cannot
  // restore from any screen.
  it("asks before retiring, and sends nothing while the question is still open", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByTestId("card-kpi")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Retire" }));

    expect(await screen.findByText(/Retire this topic\?/i)).toBeInTheDocument();
    expect(screen.getByText(/no way back/i)).toBeInTheDocument();
    // The catalog read, and nothing else.
    expect(fetchMock.mock.calls.length).toBe(1);

    await user.click(screen.getByRole("button", { name: "Keep it" }));
    expect(fetchMock.mock.calls.length).toBe(1);
  });
});

/**
 * Story 52.2 — the governed queries that answer a topic. Production holds ZERO
 * Query Specs, so the empty state is what an operator actually sees: it must name
 * where a query comes from, not offer a picker over nothing.
 */
describe("WidgetCardsPage — bound queries", () => {
  const TEMPLATES = [
    { id: "kpi", title: "KPI Overview", answers_question: "How are my KPIs evolving?", kind: "kpi" },
  ];

  function routed(queries: unknown[]) {
    return vi.fn().mockImplementation((url: string) => {
      if (String(url).includes("/answerable-topics/")) return Promise.resolve(ok({ queries }));
      return Promise.resolve(ok({ templates: TEMPLATES }));
    });
  }

  it("says where a governed query comes from when none is bound", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", routed([]));
    renderPage();
    await waitFor(() => expect(screen.getByTestId("card-kpi")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /Queries/i }));

    await waitFor(() => expect(screen.getByTestId("queries-empty-kpi")).toBeInTheDocument());
    expect(screen.getByTestId("queries-empty-kpi")).toHaveTextContent(/Explore/i);
  });

  it("lists a bound query with its role and its exact pinned version", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      routed([
        {
          binding_id: "atq_1",
          topic_key: "kpi",
          role: "headline",
          position: 1,
          query_spec_id: "qs_1",
          query_spec_version_id: "qsv_42",
        },
      ]),
    );
    renderPage();
    await waitFor(() => expect(screen.getByTestId("card-kpi")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /Queries/i }));

    await waitFor(() => expect(screen.getByText("qsv_42")).toBeInTheDocument());
    expect(screen.getByText("Headline")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Unbind/i })).toBeEnabled();
  });
});

/**
 * Story 52.3 — the half that explains. An unreadable knowledge pin is SHOWN as
 * "context missing": production holds 41 knowledge version rows and zero heads,
 * so a pin that resolves to nothing is a state an operator meets, and hiding it
 * would make it look like nothing was ever declared.
 */
describe("WidgetCardsPage — governed knowledge", () => {
  const TEMPLATES = [
    { id: "kpi", title: "KPI Overview", answers_question: "How are my KPIs evolving?", kind: "kpi" },
  ];

  function routed(knowledge: unknown[]) {
    return vi.fn().mockImplementation((url: string) => {
      const u = String(url);
      if (u.includes("/knowledge")) return Promise.resolve(ok({ knowledge }));
      if (u.includes("/queries")) return Promise.resolve(ok({ queries: [] }));
      return Promise.resolve(ok({ templates: TEMPLATES }));
    });
  }

  it("names the Context Hub when a topic elaborates from nothing", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", routed([]));
    renderPage();
    await waitFor(() => expect(screen.getByTestId("card-kpi")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /Queries/i }));

    await waitFor(() => expect(screen.getByTestId("knowledge-empty-kpi")).toBeInTheDocument());
    expect(screen.getByTestId("knowledge-empty-kpi")).toHaveTextContent(/Context Hub/i);
  });

  it("shows an unreadable pin as context missing, never as an absent declaration", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      routed([
        {
          binding_id: "atk_1",
          knowledge_kind: "topic",
          knowledge_id: "ctx_1",
          knowledge_version: 3,
          title: null,
          readable: false,
        },
      ]),
    );
    renderPage();
    await waitFor(() => expect(screen.getByTestId("card-kpi")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /Queries/i }));

    await waitFor(() => expect(screen.getByTestId("knowledge-kpi")).toBeInTheDocument());
    expect(screen.getByText(/Context missing/i)).toBeInTheDocument();
    expect(screen.getByText(/ctx_1 v3/)).toBeInTheDocument();
  });
});

/**
 * Review findings C-5 / D-9 — POST and DELETE …/{topic_key}/knowledge existed and
 * were called by nothing. Combined with the `requires_knowledge` checkbox that was
 * delivered, that made a one-way trap: a topic could be told to answer ONLY from
 * governed knowledge, and had no console path to be given any.
 */
describe("WidgetCardsPage — declaring and withdrawing governed knowledge", () => {
  const TEMPLATES = [
    { id: "kpi", title: "KPI Overview", answers_question: "How are my KPIs evolving?", kind: "kpi" },
  ];

  function routed(knowledge: unknown[]) {
    return vi.fn().mockImplementation((url: string) => {
      const u = String(url);
      if (u.includes("/knowledge")) return Promise.resolve(ok({ knowledge }));
      if (u.includes("/queries")) return Promise.resolve(ok({ queries: [] }));
      return Promise.resolve(ok({ templates: TEMPLATES }));
    });
  }

  it("declares an exact knowledge version through the governed route", async () => {
    const user = userEvent.setup();
    const fetchMock = routed([]);
    vi.stubGlobal("fetch", fetchMock);
    renderPage();
    await waitFor(() => expect(screen.getByTestId("card-kpi")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /Queries/i }));
    await waitFor(() => expect(screen.getByTestId("knowledge-empty-kpi")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /Declare knowledge/i }));
    await user.selectOptions(screen.getByTestId("knowledge-kind"), "procedure");
    await user.type(screen.getByTestId("knowledge-id"), "ctx_1");
    await user.type(screen.getByTestId("knowledge-version"), "3");
    await user.click(screen.getByRole("button", { name: "Declare" }));

    await waitFor(() => {
      const posts = fetchMock.mock.calls.filter(
        (c) => (c[1] as RequestInit | undefined)?.method === "POST",
      );
      expect(posts.length).toBe(1);
    });
    const post = fetchMock.mock.calls.find(
      (c) => (c[1] as RequestInit | undefined)?.method === "POST",
    )!;
    expect(String(post[0])).toBe("/api/projects/proj_test/answerable-topics/kpi/knowledge");
    const headers = (post[1] as RequestInit).headers as Record<string, string>;
    expect(headers["Idempotency-Key"]).toBeTruthy();
    // The version is an exact NUMBER: the server refuses anything else, and a
    // pin that follows "the latest" changes what the topic says without a record.
    expect(JSON.parse(String((post[1] as RequestInit).body))).toEqual({
      knowledge_kind: "procedure",
      knowledge_id: "ctx_1",
      knowledge_version: 3,
    });
  });

  it("refuses to send a pin that names no id or no version", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", routed([]));
    renderPage();
    await waitFor(() => expect(screen.getByTestId("card-kpi")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /Queries/i }));
    await waitFor(() => expect(screen.getByTestId("knowledge-empty-kpi")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: /Declare knowledge/i }));

    expect(screen.getByRole("button", { name: "Declare" })).toBeDisabled();
    await user.type(screen.getByTestId("knowledge-id"), "ctx_1");
    expect(screen.getByRole("button", { name: "Declare" })).toBeDisabled();
  });

  it("withdraws a declaration through the governed route, with an idempotency key", async () => {
    const user = userEvent.setup();
    const fetchMock = routed([
      {
        binding_id: "atk_1",
        knowledge_kind: "topic",
        knowledge_id: "ctx_1",
        knowledge_version: 3,
        title: "Brand rules",
        readable: true,
      },
    ]);
    vi.stubGlobal("fetch", fetchMock);
    renderPage();
    await waitFor(() => expect(screen.getByTestId("card-kpi")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /Queries/i }));
    await waitFor(() => expect(screen.getByTestId("knowledge-kpi")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /Withdraw/i }));

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some((c) => (c[1] as RequestInit | undefined)?.method === "DELETE"),
      ).toBe(true);
    });
    const del = fetchMock.mock.calls.find(
      (c) => (c[1] as RequestInit | undefined)?.method === "DELETE",
    )!;
    expect(String(del[0])).toBe(
      "/api/projects/proj_test/answerable-topics/kpi/knowledge/atk_1",
    );
    const headers = (del[1] as RequestInit).headers as Record<string, string>;
    expect(headers["Idempotency-Key"]).toBeTruthy();
  });
});

/**
 * Review finding D-10 — a read that FAILED was turned into an empty list, so the
 * panel asserted "no governed query is bound" / "elaborates from no governed
 * knowledge". That is a claim about the project manufactured out of a failure of
 * ours, which is the exact lie the rest of this epic removes from the server.
 */
describe("WidgetCardsPage — a list that could not be read is not an empty list", () => {
  const TEMPLATES = [
    { id: "kpi", title: "KPI Overview", answers_question: "How are my KPIs evolving?", kind: "kpi" },
  ];

  it("says the bound queries could not be read, and claims no absence", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) => {
        const u = String(url);
        if (u.includes("/queries")) return Promise.resolve(fail(500, "internal_error", "boom"));
        if (u.includes("/knowledge")) return Promise.resolve(ok({ knowledge: [] }));
        return Promise.resolve(ok({ templates: TEMPLATES }));
      }),
    );
    renderPage();
    await waitFor(() => expect(screen.getByTestId("card-kpi")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /Queries/i }));

    await waitFor(() => expect(screen.getByTestId("queries-unavailable-kpi")).toBeInTheDocument());
    expect(screen.getByTestId("queries-unavailable-kpi")).toHaveTextContent(/Couldn't read/i);
    expect(screen.queryByTestId("queries-empty-kpi")).not.toBeInTheDocument();
    // The other half still answers: one failed read does not blank the panel.
    expect(screen.getByTestId("knowledge-empty-kpi")).toBeInTheDocument();
  });

  it("says the declared knowledge could not be read, and claims no absence", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) => {
        const u = String(url);
        if (u.includes("/knowledge")) return Promise.resolve(fail(503, "unavailable", "down"));
        if (u.includes("/queries")) return Promise.resolve(ok({ queries: [] }));
        return Promise.resolve(ok({ templates: TEMPLATES }));
      }),
    );
    renderPage();
    await waitFor(() => expect(screen.getByTestId("card-kpi")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /Queries/i }));

    await waitFor(() => expect(screen.getByTestId("knowledge-unavailable-kpi")).toBeInTheDocument());
    expect(screen.getByTestId("knowledge-unavailable-kpi")).toHaveTextContent(/Couldn't read/i);
    expect(screen.queryByTestId("knowledge-empty-kpi")).not.toBeInTheDocument();
  });
});

/**
 * Story 52.3 AC5 was unreachable until this control existed: `requires_knowledge`
 * was read by the server and written by nobody, so no topic could ever refuse.
 */
describe("WidgetCardsPage — a topic that answers only from governed knowledge", () => {
  const TEMPLATES = [
    {
      id: "pacing",
      title: "Pacing",
      answers_question: "Are we pacing to plan?",
      kind: "kpi",
      origin: "project",
      base_template_id: "kpi",
      requires_knowledge: true,
    },
  ];

  it("shows what the topic declared, and sends it back on save", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockResolvedValue(ok({ templates: TEMPLATES }));
    vi.stubGlobal("fetch", fetchMock);

    renderPage();
    await waitFor(() => expect(screen.getByTestId("card-pacing")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /Reword/i }));

    // The form SHOWS the current value: a control that always opens unchecked
    // teaches the operator that saving resets it.
    expect(screen.getByTestId("topic-requires-knowledge")).toBeChecked();

    await user.click(screen.getByRole("button", { name: /Save new version/i }));

    await waitFor(() => expect(postCall(fetchMock)).toBeTruthy());
    const body = JSON.parse(String((postCall(fetchMock)![1] as RequestInit).body));
    expect(body.requires_knowledge).toBe(true);
  });
});

/**
 * The knowledge a topic may read was declared by RETYPING an identifier and a
 * version number, from a screen that shows neither. Both Context Hub lists are
 * already read by this console (`/api/context/topics`, `/api/context/procedures`)
 * and both objects already expose their versions (`…/{id}/versions`), so the two
 * fields are now a choice — and the version that choice lands on is the head,
 * SAID to be the head, because a pin is what the topic commits to.
 */
describe("WidgetCardsPage — choosing the knowledge instead of retyping it", () => {
  const TEMPLATES = [
    { id: "kpi", title: "KPI Overview", answers_question: "How are my KPIs evolving?", kind: "kpi" },
  ];

  function routed() {
    return vi.fn().mockImplementation((url: string) => {
      const u = String(url);
      if (u.includes("/api/context/topics/") && u.includes("/versions")) {
        return Promise.resolve(ok({ versions: [{ version_number: 1 }, { version_number: 4 }] }));
      }
      if (u.startsWith("/api/context/topics?")) {
        return Promise.resolve(
          ok({ topics: [{ id: "ctx_brand", title: "Brand rules" }, { id: "ctx_fees", title: "Fee ladder" }] }),
        );
      }
      if (u.startsWith("/api/context/procedures?")) return Promise.resolve(ok({ procedures: [] }));
      if (u.includes("/knowledge")) return Promise.resolve(ok({ knowledge: [] }));
      if (u.includes("/queries")) return Promise.resolve(ok({ queries: [] }));
      return Promise.resolve(ok({ templates: TEMPLATES }));
    });
  }

  async function openDeclaration(user: ReturnType<typeof userEvent.setup>) {
    await waitFor(() => expect(screen.getByTestId("card-kpi")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: /Queries/i }));
    await waitFor(() => expect(screen.getByTestId("knowledge-empty-kpi")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: /Declare knowledge/i }));
  }

  it("lists the project's knowledge, pins the head, and SAYS which version that is", async () => {
    const user = userEvent.setup();
    const fetchMock = routed();
    vi.stubGlobal("fetch", fetchMock);
    renderPage();
    await openDeclaration(user);

    // The list is the Context Hub's own, read with the project scope.
    await waitFor(() => expect(screen.getByTestId("knowledge-pick")).toBeInTheDocument());
    expect(
      fetchMock.mock.calls.some((c) =>
        String(c[0]) === "/api/context/topics?project_id=proj_test"),
    ).toBe(true);
    expect(screen.getByRole("option", { name: "Brand rules" })).toBeInTheDocument();

    await user.selectOptions(screen.getByTestId("knowledge-pick"), "ctx_brand");

    // The head is v4 — the highest version that EXISTS, read from the versions
    // route rather than guessed from the list row.
    await waitFor(() =>
      expect((screen.getByTestId("knowledge-version") as HTMLSelectElement).value).toBe("4"),
    );
    expect(screen.getByRole("option", { name: /v4 — current head/ })).toBeInTheDocument();
    expect(screen.getByText(/Version 4 is the current head/)).toBeInTheDocument();
    // v1 is still choosable: a pin is an exact version, not "the newest".
    expect(screen.getByRole("option", { name: "v1" })).toBeInTheDocument();
  });

  it("sends the same wire payload the typed form sent", async () => {
    const user = userEvent.setup();
    const fetchMock = routed();
    vi.stubGlobal("fetch", fetchMock);
    renderPage();
    await openDeclaration(user);

    await waitFor(() => expect(screen.getByTestId("knowledge-pick")).toBeInTheDocument());
    await user.selectOptions(screen.getByTestId("knowledge-pick"), "ctx_brand");
    await waitFor(() =>
      expect((screen.getByTestId("knowledge-version") as HTMLSelectElement).value).toBe("4"),
    );
    await user.click(screen.getByRole("button", { name: "Declare" }));

    await waitFor(() => expect(postCall(fetchMock)).toBeTruthy());
    const post = postCall(fetchMock)!;
    expect(String(post[0])).toBe("/api/projects/proj_test/answerable-topics/kpi/knowledge");
    expect((post[1] as RequestInit).headers as Record<string, string>).toHaveProperty(
      "Idempotency-Key",
    );
    // Byte for byte what the free-text form produced: the picker changed how the
    // value is found, never what the server is told.
    expect(JSON.parse(String((post[1] as RequestInit).body))).toEqual({
      knowledge_kind: "topic",
      knowledge_id: "ctx_brand",
      knowledge_version: 4,
    });
  });

  it("keeps a paste escape for an object the list cannot carry", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", routed());
    renderPage();
    await openDeclaration(user);

    await waitFor(() => expect(screen.getByTestId("knowledge-pick")).toBeInTheDocument());
    await user.selectOptions(screen.getByTestId("knowledge-pick"), "");

    expect(screen.getByTestId("knowledge-id")).toBeInTheDocument();
    await user.type(screen.getByTestId("knowledge-id"), "ctx_platform");
    expect((screen.getByTestId("knowledge-id") as HTMLInputElement).value).toBe("ctx_platform");
    // And the way back, so the escape is not a one-way door.
    expect(screen.getByRole("button", { name: /Choose from the list instead/i })).toBeInTheDocument();
  });

  it("falls back to free text when the Context Hub list cannot be read, and says so", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) => {
        const u = String(url);
        if (u.startsWith("/api/context/topics?")) {
          return Promise.resolve(fail(503, "unavailable", "down"));
        }
        if (u.includes("/knowledge")) return Promise.resolve(ok({ knowledge: [] }));
        if (u.includes("/queries")) return Promise.resolve(ok({ queries: [] }));
        return Promise.resolve(ok({ templates: TEMPLATES }));
      }),
    );
    renderPage();
    await openDeclaration(user);

    await waitFor(() =>
      expect(screen.getByTestId("knowledge-choices-unavailable")).toBeInTheDocument(),
    );
    // NOT an empty picker: an unreadable list must not read as "this project has
    // authored nothing".
    expect(screen.queryByTestId("knowledge-pick")).not.toBeInTheDocument();
    expect(screen.getByTestId("knowledge-id")).toBeInTheDocument();
  });
});

/**
 * A declared row named `knowledge_id vN` and opened nothing — the object it
 * names has a page in this console, at an address the registry declares.
 */
describe("WidgetCardsPage — a declared row opens the object it names", () => {
  const TEMPLATES = [
    { id: "kpi", title: "KPI Overview", answers_question: "How are my KPIs evolving?", kind: "kpi" },
  ];

  function routed(knowledge: unknown[]) {
    return vi.fn().mockImplementation((url: string) => {
      const u = String(url);
      if (u.includes("/knowledge")) return Promise.resolve(ok({ knowledge }));
      if (u.includes("/queries")) return Promise.resolve(ok({ queries: [] }));
      return Promise.resolve(ok({ templates: TEMPLATES }));
    });
  }

  it("links a knowledge item to its Knowledge Library page and a Skill to its registry page", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      routed([
        {
          binding_id: "atk_1",
          knowledge_kind: "topic",
          knowledge_id: "ctx_brand",
          knowledge_version: 3,
          title: "Brand rules",
          readable: true,
        },
        {
          binding_id: "atk_2",
          knowledge_kind: "procedure",
          knowledge_id: "proc_close",
          knowledge_version: 2,
          title: "Month-end close",
          readable: true,
        },
      ]),
    );
    renderPage();
    await waitFor(() => expect(screen.getByTestId("card-kpi")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: /Queries/i }));
    await waitFor(() => expect(screen.getByTestId("knowledge-kpi")).toBeInTheDocument());

    const item = screen.getByTestId("knowledge-link-atk_1");
    expect(item).toHaveTextContent("Brand rules");
    expect(item).toHaveAttribute(
      "href",
      `/org/${ORG}/project/proj_test/context-hub/knowledge-library/object/context-topic/ctx_brand/tab/content`,
    );

    const skill = screen.getByTestId("knowledge-link-atk_2");
    expect(skill).toHaveAttribute(
      "href",
      `/org/${ORG}/project/proj_test/context-hub/skills-registry/object/context-procedure/proc_close/tab/content`,
    );
  });

  it("still names an unreadable pin, and still points at the object", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      routed([
        {
          binding_id: "atk_1",
          knowledge_kind: "topic",
          knowledge_id: "ctx_gone",
          knowledge_version: 3,
          title: null,
          readable: false,
        },
      ]),
    );
    renderPage();
    await waitFor(() => expect(screen.getByTestId("card-kpi")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: /Queries/i }));
    await waitFor(() => expect(screen.getByTestId("knowledge-kpi")).toBeInTheDocument());

    expect(screen.getByText(/Context missing/i)).toBeInTheDocument();
    expect(screen.getByTestId("knowledge-link-atk_1")).toHaveTextContent("ctx_gone");
  });
});

/**
 * The catalog arrives whole and had no way to be narrowed — nine questions today,
 * and a project that authors its own has no ceiling.
 */
describe("WidgetCardsPage — narrowing the catalog", () => {
  const TEMPLATES = [
    { id: "pacing", title: "Pacing", answers_question: "Are we pacing to plan?", kind: "kpi", usable: true },
    {
      id: "spend_by_channel",
      title: "Spend by channel",
      answers_question: "Where did my media budget go?",
      kind: "kpi",
      usable: false,
      missing: { metrics: ["media_spend"], dimensions: [] },
    },
  ];

  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(ok({ templates: TEMPLATES })));
  });

  it("narrows on what was typed, without asking the server again", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByTestId("card-pacing")).toBeInTheDocument());
    expect(screen.getByTestId("topics-count")).toHaveTextContent("2 of 2");

    await user.type(screen.getByTestId("topics-search"), "budget");

    await waitFor(() =>
      expect(screen.queryByTestId("card-pacing")).not.toBeInTheDocument(),
    );
    expect(screen.getByTestId("card-spend_by_channel")).toBeInTheDocument();
    expect(screen.getByTestId("topics-count")).toHaveTextContent("1 of 2");
    // One read on mount, and no other: the list was already in hand.
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(1);
  });

  it("narrows on the server's own usability verdict", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByTestId("card-pacing")).toBeInTheDocument());

    await user.selectOptions(screen.getByTestId("topics-usability"), "missing");

    await waitFor(() => expect(screen.queryByTestId("card-pacing")).not.toBeInTheDocument());
    expect(screen.getByTestId("card-spend_by_channel")).toBeInTheDocument();
  });

  it("narrowed to nothing names the term, and does not claim the project answers nothing", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByTestId("card-pacing")).toBeInTheDocument());

    await user.type(screen.getByTestId("topics-search"), "zzz");

    await waitFor(() => expect(screen.getByTestId("topics-no-match")).toBeInTheDocument());
    expect(screen.getByTestId("topics-no-match")).toHaveTextContent("zzz");
    expect(screen.queryByTestId("widgets-empty")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Show every question/i }));
    await waitFor(() => expect(screen.getByTestId("card-pacing")).toBeInTheDocument());
  });
});

/**
 * The rest of the audit: an empty catalog offers the gesture that fills it, a
 * stored topic shows the version it is at, and the widget URI stops being a chip.
 */
describe("WidgetCardsPage — version, empty state and technical reference", () => {
  it("offers the create action from the empty state itself", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(ok({ templates: [] })));
    renderPage();

    await waitFor(() => expect(screen.getByTestId("widgets-empty")).toBeInTheDocument());
    expect(
      within(screen.getByTestId("widgets-empty")).getByRole("button", {
        name: /Add the first topic/i,
      }),
    ).toBeEnabled();
  });

  it("shows the version a stored topic is at, and its reword lineage", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        ok({
          templates: [
            {
              id: "pacing",
              title: "Pacing",
              answers_question: "Are we pacing to plan?",
              kind: "kpi",
              origin: "project",
              version_number: 3,
            },
          ],
        }),
      ),
    );
    renderPage();

    await waitFor(() => expect(screen.getByTestId("version-pacing")).toBeInTheDocument());
    expect(screen.getByTestId("version-pacing")).toHaveTextContent("Version 3");
    expect(screen.getByTestId("lineage-pacing")).toHaveTextContent(/Reworded 2 times/);
  });

  it("keeps the widget URI, behind a technical disclosure rather than as a chip", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        ok({
          templates: [
            {
              id: "spend_by_channel",
              title: "Spend by channel",
              answers_question: "Where did my media budget go?",
              widget_uri: "ui://widget/cards/spend-by-channel.html",
              kind: "kpi",
            },
          ],
        }),
      ),
    );
    renderPage();
    await waitFor(() => expect(screen.getByTestId("card-spend_by_channel")).toBeInTheDocument());

    // Not on the card at rest: it is an implementation address, not a label.
    expect(
      screen.queryByText("ui://widget/cards/spend-by-channel.html"),
    ).not.toBeInTheDocument();

    await user.click(screen.getByTestId("technical-spend_by_channel"));

    await waitFor(() =>
      expect(screen.getByText("ui://widget/cards/spend-by-channel.html")).toBeInTheDocument(),
    );
  });
});

/**
 * THE QUERY PIN IS CHOSEN, AND IT IS CHOSEN OVER THE REAL ROUTE.
 *
 * The predecessor of this block asserted the OPPOSITE: that the field stayed an
 * `INPUT` and that no request touched a query-spec route. That was the correct
 * assertion for as long as `query_specs_api.py` mounted `POST /query-specs` and
 * `GET /query-specs/{id}` and nothing else — a picker would have listed an
 * invented collection, and the test existed to fail on the day someone built
 * one over an endpoint that did not exist.
 *
 * `GET /api/projects/{id}/analyze/query-specs` exists since 2026-08-18, so the
 * guard is inverted rather than deleted: what must now be measured is that the
 * select is fed by THAT route and by no other, that the exact version id it
 * pins is what reaches the wire, and that a list which cannot be read falls
 * back to the paste field with its sentence — never to an empty select, which
 * would assert this project has authored nothing.
 */
describe("WidgetCardsPage — choosing the Query Spec to pin", () => {
  const TEMPLATES = [
    { id: "kpi", title: "KPI Overview", answers_question: "How are my KPIs evolving?", kind: "kpi" },
  ];

  const SPECS = {
    query_specs: [
      { id: "qs_1", name: "Weekly clicks", current_version_id: "qsv_42", current_version_number: 3 },
      { id: "qs_2", name: null, current_version_id: "qsv_7", current_version_number: 1 },
      { id: "qs_3", name: "Drafted", current_version_id: null, current_version_number: null },
    ],
    next_cursor: null,
  };

  /** `specs` may be a body or a Response — an unreadable list is a case here. */
  function routed(specs: unknown) {
    return vi.fn().mockImplementation((url: string) => {
      const u = String(url);
      if (u.includes("/analyze/query-specs")) {
        return Promise.resolve(
          specs instanceof Object && "ok" in (specs as object) ? specs : ok(specs),
        );
      }
      if (u.includes("/knowledge")) return Promise.resolve(ok({ knowledge: [] }));
      if (u.includes("/queries")) return Promise.resolve(ok({ queries: [] }));
      return Promise.resolve(ok({ templates: TEMPLATES }));
    });
  }

  async function openBinder(user: ReturnType<typeof userEvent.setup>) {
    renderPage();
    await waitFor(() => expect(screen.getByTestId("card-kpi")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: /Queries/i }));
    await waitFor(() => expect(screen.getByTestId("queries-empty-kpi")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: /Bind a query/i }));
  }

  it("reads the collection only when the form opens, and lists what it holds", async () => {
    const user = userEvent.setup();
    const fetchMock = routed(SPECS);
    vi.stubGlobal("fetch", fetchMock);
    renderPage();
    await waitFor(() => expect(screen.getByTestId("card-kpi")).toBeInTheDocument());

    // Not on mount, and not on opening the panel: a catalog of nine cards must
    // not spend nine reads on a question nobody has asked.
    await user.click(screen.getByRole("button", { name: /Queries/i }));
    await waitFor(() => expect(screen.getByTestId("queries-empty-kpi")).toBeInTheDocument());
    expect(
      fetchMock.mock.calls.some((c) => String(c[0]).includes("/analyze/query-specs")),
    ).toBe(false);

    await user.click(screen.getByRole("button", { name: /Bind a query/i }));

    await waitFor(() => expect(screen.getByTestId("binding-spec")).toBeInTheDocument());
    const read = fetchMock.mock.calls.find((c) => String(c[0]).includes("/analyze/query-specs"))!;
    expect(String(read[0])).toBe("/api/projects/proj_test/analyze/query-specs");
    const options = within(screen.getByTestId("binding-spec")).getAllByRole("option");
    // The escape, then the three specs the server sent.
    expect(options.map((o) => o.textContent)).toEqual([
      "Paste a version id instead…",
      "Weekly clicks — v3, current version",
      // An unnamed spec is shown BY ITS ID and labelled as unnamed — never given
      // a title this screen invented.
      "qs_2 (unnamed) — v1, current version",
      "Drafted — no version to pin yet",
    ]);
    // A head with nothing to pin is offered and refused, not hidden.
    expect(options[3]).toBeDisabled();
  });

  it("pins the chosen spec's exact version, and says which one it pins", async () => {
    const user = userEvent.setup();
    const fetchMock = routed(SPECS);
    vi.stubGlobal("fetch", fetchMock);
    await openBinder(user);
    await waitFor(() => expect(screen.getByTestId("binding-spec")).toBeInTheDocument());

    await user.selectOptions(screen.getByTestId("binding-spec"), "qs_1");

    // The person SEES the version the topic is being committed to.
    expect(screen.getByTestId("binding-pinned")).toHaveTextContent("qsv_42");

    await user.click(screen.getByRole("button", { name: "Bind" }));

    await waitFor(() => expect(postCall(fetchMock)).toBeTruthy());
    const post = postCall(fetchMock)!;
    expect(String(post[0])).toBe("/api/projects/proj_test/answerable-topics/kpi/queries");
    // The VERSION id travels, never the head's — a pin that named the head would
    // follow the newest and change what the topic answers without a record.
    expect(JSON.parse(String((post[1] as RequestInit).body))).toEqual({
      query_spec_version_id: "qsv_42",
      role: "headline",
    });
  });

  it("still accepts a pasted version id for a spec the page cannot list", async () => {
    const user = userEvent.setup();
    const fetchMock = routed(SPECS);
    vi.stubGlobal("fetch", fetchMock);
    await openBinder(user);
    await waitFor(() => expect(screen.getByTestId("binding-spec")).toBeInTheDocument());

    // The escape is the empty option, exactly as the knowledge pinner's is.
    await user.selectOptions(screen.getByTestId("binding-spec"), "");
    await user.type(screen.getByTestId("binding-version"), "qsv_elsewhere");
    await user.click(screen.getByRole("button", { name: "Bind" }));

    await waitFor(() => expect(postCall(fetchMock)).toBeTruthy());
    expect(JSON.parse(String((postCall(fetchMock)![1] as RequestInit).body))).toEqual({
      query_spec_version_id: "qsv_elsewhere",
      role: "headline",
    });
  });

  it("says the list is a page, not the whole collection, when the server paged", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", routed({ ...SPECS, next_cursor: "qs_3" }));
    await openBinder(user);

    await waitFor(() => expect(screen.getByTestId("query-specs-truncated")).toBeInTheDocument());
    expect(screen.getByTestId("query-specs-truncated")).toHaveTextContent(/first page/i);
    // And the paste escape is still reachable for what the page does not carry.
    expect(screen.getByTestId("binding-spec")).toBeInTheDocument();
  });

  it("falls back to the paste field when the collection cannot be read", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", routed(fail(500, "internal_error", "query specs unavailable")));
    await openBinder(user);

    await waitFor(() => expect(screen.getByTestId("query-specs-unavailable")).toBeInTheDocument());
    // The sentence names what happened and what to do — it does NOT claim the
    // project has authored no governed query.
    expect(screen.getByTestId("query-specs-unavailable")).toHaveTextContent(
      /Couldn't read this project's governed queries/i,
    );
    expect(screen.getByTestId("query-specs-unavailable")).toHaveTextContent(/Paste a version id/i);
    expect(screen.queryByTestId("binding-spec")).not.toBeInTheDocument();
    expect(screen.getByTestId("binding-version").tagName).toBe("INPUT");
  });

  it("names where a Query Spec comes from when this project has authored none", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", routed({ query_specs: [], next_cursor: null }));
    await openBinder(user);

    await waitFor(() =>
      expect(screen.getByTestId("binding-version").tagName).toBe("INPUT"),
    );
    expect(screen.queryByTestId("binding-spec")).not.toBeInTheDocument();
    // An empty read is an ANSWER, and it names the gesture that fills the list.
    expect(screen.getByText(/authored no governed query yet/i)).toHaveTextContent(/Explore/i);
  });
});
