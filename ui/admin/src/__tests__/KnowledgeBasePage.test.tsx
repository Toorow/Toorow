/**
 * KnowledgeBasePage — Story 44.1 rewiring onto the governed context store.
 *
 * Pins: the page reads GET /api/context/topics (never the deprecated
 * /api/knowledge), renders no literal fallback entries, an honest empty state
 * only after a real fetch returns zero topics, and a create/edit failure
 * preserves the open draft while showing the server's exact message.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import KnowledgeBasePage from "../KnowledgeBasePage";

const TOPIC = {
  id: "top_01JABCDEF",
  project_id: "p1",
  title: "ROAS calculation & deduplication policy",
  body_md: "Return on ad spend is computed on deduplicated conversions.",
  status: "active",
  created_by: "winston@toorow.com",
  created_at: "2026-07-20T10:00:00+00:00",
  updated_at: "2026-07-24T10:00:00+00:00",
  version_number: 2,
};

const PLATFORM_TOPIC = {
  ...TOPIC,
  id: "top_platform",
  project_id: null,
  title: "Platform-wide glossary",
};

function resp(status: number, body: unknown) {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as unknown as Response;
}

interface Call {
  url: string;
  init: RequestInit;
}

function stubFetch(handler: (url: string, init: RequestInit) => Response | Promise<Response>) {
  const calls: Call[] = [];
  const mock = vi.fn((url: string, init: RequestInit = {}) => {
    calls.push({ url: String(url), init });
    return Promise.resolve(handler(String(url), init));
  });
  vi.stubGlobal("fetch", mock);
  return calls;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("KnowledgeBasePage — data source", () => {
  it("reads GET /api/context/topics and never /api/knowledge", async () => {
    const calls = stubFetch((url) => {
      if (url.includes("/api/context/topics")) return resp(200, { topics: [TOPIC] });
      return resp(500, { code: "unexpected", message: `unexpected call: ${url}` });
    });

    render(<KnowledgeBasePage projectId="p1" />);

    await waitFor(() => {
      expect(screen.getByText(TOPIC.title)).toBeInTheDocument();
    });

    const urls = calls.map((c) => c.url);
    expect(urls.some((u) => u.includes("/api/context/topics?project_id=p1"))).toBe(true);
    expect(urls.some((u) => u.includes("/api/knowledge"))).toBe(false);
    // No literal fallback title survives a successful, non-empty fetch.
    expect(screen.queryByText("Canonical revenue vs commerce gross sales")).not.toBeInTheDocument();
  });

  it("renders the version number and author from the payload", async () => {
    stubFetch((url) =>
      url.includes("/api/context/topics") ? resp(200, { topics: [TOPIC] }) : resp(404, {}),
    );
    render(<KnowledgeBasePage projectId="p1" />);

    await waitFor(() => expect(screen.getByText(TOPIC.title)).toBeInTheDocument());
    expect(screen.getByText("v2")).toBeInTheDocument();
    // Twice by design since 44.11: the Author line AND the Owner line (which
    // falls back to created_by when no explicit owner is set).
    expect(screen.getAllByText(TOPIC.created_by)).toHaveLength(2);
  });

  it("flags a platform-scope row (project_id null) with a Platform badge", async () => {
    stubFetch((url) =>
      url.includes("/api/context/topics")
        ? resp(200, { topics: [PLATFORM_TOPIC] })
        : resp(404, {}),
    );
    render(<KnowledgeBasePage projectId="p1" />);

    await waitFor(() => expect(screen.getByText(PLATFORM_TOPIC.title)).toBeInTheDocument());
    expect(screen.getByText("Platform")).toBeInTheDocument();
  });
});

describe("KnowledgeBasePage — honest empty and error states", () => {
  it("says the base is empty and offers a working Add action when the fetch returns zero topics", async () => {
    stubFetch((url) =>
      url.includes("/api/context/topics") ? resp(200, { topics: [] }) : resp(404, {}),
    );
    render(<KnowledgeBasePage projectId="p1" />);

    await waitFor(() => {
      expect(screen.getByTestId("knowledge-empty")).toBeInTheDocument();
    });
    expect(screen.getByTestId("knowledge-empty")).toHaveTextContent(/No knowledge entries yet/i);
    expect(screen.getByRole("button", { name: /Add knowledge entry/i })).toBeInTheDocument();
  });

  it("reports a load failure rather than an empty state", async () => {
    stubFetch((url) =>
      url.includes("/api/context/topics")
        ? resp(500, { code: "db_error", message: "Erreur lors de la récupération des topics" })
        : resp(404, {}),
    );
    render(<KnowledgeBasePage projectId="p1" />);

    await waitFor(() => {
      expect(screen.getByTestId("knowledge-error")).toBeInTheDocument();
    });
    expect(screen.getByTestId("knowledge-error")).toHaveTextContent(
      /Erreur lors de la récupération des topics/,
    );
    expect(screen.queryByTestId("knowledge-empty")).not.toBeInTheDocument();
  });
});

describe("KnowledgeBasePage — create/edit preserves draft on failure", () => {
  it("keeps the draft and shows the exact 422 message when create fails", async () => {
    const user = userEvent.setup();
    stubFetch((url, init) => {
      if (url.includes("/api/context/topics") && (!init.method || init.method === "GET")) {
        return resp(200, { topics: [] });
      }
      if (url === "/api/context/topics" && init.method === "POST") {
        return resp(422, { code: "invalid_param", message: "Le titre du topic ne peut pas être vide." });
      }
      return resp(404, {});
    });

    render(<KnowledgeBasePage projectId="p1" />);
    await waitFor(() => expect(screen.getByTestId("knowledge-empty")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /Add knowledge entry/i }));
    const titleInput = await screen.findByPlaceholderText(/ROAS calculation/i);
    await user.type(titleInput, "Draft title kept on failure");
    const bodyInput = screen.getByPlaceholderText(/Write the entry in Markdown/i);
    await user.type(bodyInput, "draft body");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(screen.getByTestId("knowledge-editor-error")).toHaveTextContent(
        "Le titre du topic ne peut pas être vide.",
      );
    });
    // The draft survives the failed save — editor stays open with the typed values.
    expect(screen.getByDisplayValue("Draft title kept on failure")).toBeInTheDocument();
    expect(screen.getByDisplayValue("draft body")).toBeInTheDocument();
  });

  it("submits an empty title and shows the server's verbatim 422 rather than blocking client-side", async () => {
    const user = userEvent.setup();
    let createCalls = 0;
    stubFetch((url, init) => {
      if (url.includes("/api/context/topics") && (!init.method || init.method === "GET")) {
        return resp(200, { topics: [] });
      }
      if (url === "/api/context/topics" && init.method === "POST") {
        createCalls += 1;
        return resp(422, { code: "invalid_param", message: "Le titre du topic ne peut pas être vide." });
      }
      return resp(404, {});
    });

    render(<KnowledgeBasePage projectId="p1" />);
    await waitFor(() => expect(screen.getByTestId("knowledge-empty")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /Add knowledge entry/i }));
    // Title left empty on purpose: the Save button must not be disabled for
    // this, so the server's 422 is the sole authority (Story 44.1 review).
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(screen.getByTestId("knowledge-editor-error")).toHaveTextContent(
        "Le titre du topic ne peut pas être vide.",
      );
    });
    expect(createCalls).toBe(1);
  });

  it("reflects the new version_number and author after a successful edit", async () => {
    const user = userEvent.setup();
    stubFetch((url, init) => {
      if (url.includes("/api/context/topics?") && (!init.method || init.method === "GET")) {
        return resp(200, { topics: [TOPIC] });
      }
      if (url.includes(`/api/context/topics/${TOPIC.id}`) && init.method === "PATCH") {
        return resp(200, { ...TOPIC, title: "Updated title", version_number: 3 });
      }
      return resp(404, {});
    });

    render(<KnowledgeBasePage projectId="p1" />);
    await waitFor(() => expect(screen.getByText(TOPIC.title)).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Edit entry" }));
    const titleInput = await screen.findByDisplayValue(TOPIC.title);
    await user.clear(titleInput);
    await user.type(titleInput, "Updated title");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(screen.getByText("Updated title")).toBeInTheDocument();
    });
    expect(screen.getByText("v3")).toBeInTheDocument();
  });
});

describe("KnowledgeBasePage — owner (Story 44.11)", () => {
  it("sends the typed Owner value on the create POST body", async () => {
    const user = userEvent.setup();
    let createBody: unknown = null;
    stubFetch((url, init) => {
      if (url.includes("/api/context/topics") && (!init.method || init.method === "GET")) {
        return resp(200, { topics: [] });
      }
      if (url === "/api/context/topics" && init.method === "POST") {
        createBody = JSON.parse(String(init.body));
        return resp(201, { ...TOPIC, title: "New entry", owner: "owner@toorow.com" });
      }
      return resp(404, {});
    });

    render(<KnowledgeBasePage projectId="p1" />);
    await waitFor(() => expect(screen.getByTestId("knowledge-empty")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /Add knowledge entry/i }));
    await user.type(screen.getByPlaceholderText(/ROAS calculation/i), "New entry");
    await user.type(screen.getByTestId("knowledge-editor-owner"), "owner@toorow.com");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(createBody).not.toBeNull());
    expect((createBody as { owner: string }).owner).toBe("owner@toorow.com");
  });

  it("sends owner: null when the Owner field is left blank", async () => {
    const user = userEvent.setup();
    let createBody: unknown = null;
    stubFetch((url, init) => {
      if (url.includes("/api/context/topics") && (!init.method || init.method === "GET")) {
        return resp(200, { topics: [] });
      }
      if (url === "/api/context/topics" && init.method === "POST") {
        createBody = JSON.parse(String(init.body));
        return resp(201, { ...TOPIC, title: "No owner entry" });
      }
      return resp(404, {});
    });

    render(<KnowledgeBasePage projectId="p1" />);
    await waitFor(() => expect(screen.getByTestId("knowledge-empty")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /Add knowledge entry/i }));
    await user.type(screen.getByPlaceholderText(/ROAS calculation/i), "No owner entry");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(createBody).not.toBeNull());
    expect((createBody as { owner: string | null }).owner).toBeNull();
  });

  it("pre-fills the Owner input from the topic's raw owner when editing", async () => {
    const user = userEvent.setup();
    const owned = { ...TOPIC, owner: "prefilled@toorow.com" };
    stubFetch((url) =>
      url.includes("/api/context/topics") ? resp(200, { topics: [owned] }) : resp(404, {}),
    );
    render(<KnowledgeBasePage projectId="p1" />);
    await waitFor(() => expect(screen.getByText(TOPIC.title)).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Edit entry" }));
    expect(await screen.findByDisplayValue("prefilled@toorow.com")).toBeInTheDocument();
  });
});

describe("KnowledgeBasePage — archive failures are not silent", () => {
  it("shows the server's exact message on the failing card when archive fails", async () => {
    const user = userEvent.setup();
    stubFetch((url, init) => {
      if (url.includes("/api/context/topics?") && (!init.method || init.method === "GET")) {
        return resp(200, { topics: [TOPIC] });
      }
      if (url.includes(`/api/context/topics/${TOPIC.id}/archive`) && init.method === "POST") {
        return resp(409, { code: "conflict", message: "Ce topic est référencé ailleurs." });
      }
      return resp(404, {});
    });

    render(<KnowledgeBasePage projectId="p1" />);
    await waitFor(() => expect(screen.getByText(TOPIC.title)).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Archive" }));

    await waitFor(() => {
      expect(screen.getByTestId(`knowledge-archive-error-${TOPIC.id}`)).toHaveTextContent(
        "Ce topic est référencé ailleurs.",
      );
    });
    // The card is not removed on a failed archive.
    expect(screen.getByText(TOPIC.title)).toBeInTheDocument();
  });

  it("shows a network-error message on the failing card when archive throws", async () => {
    const user = userEvent.setup();
    stubFetch((url, init) => {
      if (url.includes("/api/context/topics?") && (!init.method || init.method === "GET")) {
        return resp(200, { topics: [TOPIC] });
      }
      if (url.includes(`/api/context/topics/${TOPIC.id}/archive`) && init.method === "POST") {
        return Promise.reject(new Error("network down"));
      }
      return resp(404, {});
    });

    render(<KnowledgeBasePage projectId="p1" />);
    await waitFor(() => expect(screen.getByText(TOPIC.title)).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Archive" }));

    await waitFor(() => {
      expect(screen.getByTestId(`knowledge-archive-error-${TOPIC.id}`)).toHaveTextContent(
        "network down",
      );
    });
  });
});
