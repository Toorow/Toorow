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

const CAPS = { can_write: true, version_history: true, usage: false };

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
  capabilities: CAPS,
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
      if (url.includes("/api/context/topics")) return resp(200, { capabilities: CAPS, topics: [TOPIC] });
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
      url.includes("/api/context/topics") ? resp(200, { capabilities: CAPS, topics: [TOPIC] }) : resp(404, {}),
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
        ? resp(200, { capabilities: CAPS, topics: [PLATFORM_TOPIC] })
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
      url.includes("/api/context/topics") ? resp(200, { capabilities: CAPS, topics: [] }) : resp(404, {}),
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
        return resp(200, { capabilities: CAPS, topics: [] });
      }
      if (url.startsWith("/api/context/topics?") && init.method === "POST") {
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
        return resp(200, { capabilities: CAPS, topics: [] });
      }
      if (url.startsWith("/api/context/topics?") && init.method === "POST") {
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
        return resp(200, { capabilities: CAPS, topics: [TOPIC] });
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
        return resp(200, { capabilities: CAPS, topics: [] });
      }
      if (url.startsWith("/api/context/topics?") && init.method === "POST") {
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
        return resp(200, { capabilities: CAPS, topics: [] });
      }
      if (url.startsWith("/api/context/topics?") && init.method === "POST") {
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
      url.includes("/api/context/topics") ? resp(200, { capabilities: CAPS, topics: [owned] }) : resp(404, {}),
    );
    render(<KnowledgeBasePage projectId="p1" />);
    await waitFor(() => expect(screen.getByText(TOPIC.title)).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Edit entry" }));
    expect(await screen.findByDisplayValue("prefilled@toorow.com")).toBeInTheDocument();
  });
});

describe("KnowledgeBasePage — governed business context", () => {
  it("creates the business link only after the topic save is acknowledged", async () => {
    const user = userEvent.setup();
    const refresh = vi.fn().mockResolvedValue(undefined);
    const calls = stubFetch((url, init) => {
      if (url.includes("/api/context/topics") && (!init.method || init.method === "GET")) {
        return resp(200, { capabilities: CAPS, topics: [] });
      }
      if (url.startsWith("/api/context/topics?") && init.method === "POST") {
        return resp(201, { ...TOPIC, id: "top_NEW", title: "Market policy", version_number: 1 });
      }
      if (url.includes("/api/context/business-links") && init.method === "POST") {
        return resp(201, { id: "blink_1" });
      }
      return resp(404, {});
    });
    const businessContext = {
      selected: null,
      domains: [{ id: "bd_market", name: "Markets", status: "active", version_number: 1 }],
      classifications: [],
      links: [],
      refresh,
    } as any;
    render(<KnowledgeBasePage projectId="p1" businessContext={businessContext} />);

    await user.click(await screen.findByRole("button", { name: /Add knowledge entry/i }));
    await user.type(screen.getByLabelText("Title"), "Market policy");
    await user.selectOptions(
      screen.getByTestId("knowledge-editor-business-context"),
      "business_domain:bd_market",
    );
    await user.click(screen.getByRole("button", { name: "Save" }));

    await screen.findByText(/saved and linked to its governed business context/i);
    const topicIndex = calls.findIndex((call) => call.url.startsWith("/api/context/topics?") && call.init.method === "POST");
    const linkIndex = calls.findIndex((call) => call.url.includes("/api/context/business-links") && call.init.method === "POST");
    expect(linkIndex).toBeGreaterThan(topicIndex);
    expect(JSON.parse(String(calls[linkIndex].init.body))).toMatchObject({
      taxonomy_type: "business_domain",
      taxonomy_id: "bd_market",
      target_type: "topic",
      target_id: "top_NEW",
    });
    expect(refresh).toHaveBeenCalled();
  });
});

describe("KnowledgeBasePage — archiving asks before it happens", () => {
  it("names the entry and names the way back before archiving anything", async () => {
    const user = userEvent.setup();
    const calls = stubFetch((url, init) => {
      if (url.includes("/api/context/topics?") && (!init.method || init.method === "GET")) {
        return resp(200, { capabilities: CAPS, topics: [TOPIC] });
      }
      return resp(404, {});
    });

    render(<KnowledgeBasePage projectId="p1" />);
    await waitFor(() => expect(screen.getByText(TOPIC.title)).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Archive" }));

    const dialog = await screen.findByTestId("knowledge-archive-confirm");
    expect(dialog).toHaveTextContent(TOPIC.title);
    // Since 2026-08-18 an archive IS reversible, so the copy names the gesture
    // that reverses it instead of claiming finality it no longer has.
    expect(dialog).toHaveTextContent(/Show archived/i);
    expect(dialog).toHaveTextContent(/restore it/i);
    expect(dialog).not.toHaveTextContent(/no way back/i);
    expect(calls.some((call) => call.url.includes("/archive"))).toBe(false);

    await user.click(screen.getByTestId("knowledge-archive-confirm-cancel"));
    expect(calls.some((call) => call.url.includes("/archive"))).toBe(false);
    expect(screen.getByText(TOPIC.title)).toBeInTheDocument();
  });

  it("archives only once the confirmation is accepted", async () => {
    const user = userEvent.setup();
    const calls = stubFetch((url, init) => {
      if (url.includes("/api/context/topics?") && (!init.method || init.method === "GET")) {
        return resp(200, { capabilities: CAPS, topics: [TOPIC] });
      }
      if (url.includes(`/api/context/topics/${TOPIC.id}/archive`) && init.method === "POST") {
        return resp(200, { ...TOPIC, status: "archived", version_number: 3 });
      }
      return resp(404, {});
    });

    render(<KnowledgeBasePage projectId="p1" />);
    await waitFor(() => expect(screen.getByText(TOPIC.title)).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Archive" }));
    await user.click(await screen.findByTestId("knowledge-archive-confirm-accept"));

    await waitFor(() => expect(screen.queryByText(TOPIC.title)).not.toBeInTheDocument());
    expect(calls.filter((call) => call.url.includes("/archive")).length).toBe(1);
  });
});

describe("KnowledgeBasePage — the archive has a way back (2026-08-18)", () => {
  const ARCHIVED = {
    ...TOPIC,
    id: "top_archived",
    title: "Retired attribution window",
    status: "archived",
    version_number: 4,
  };

  it("hides archived entries until Show archived is turned on, then reads ?status=all", async () => {
    const user = userEvent.setup();
    const calls = stubFetch((url, init) => {
      if (url.includes("/api/context/topics?") && (!init.method || init.method === "GET")) {
        return url.includes("status=all")
          ? resp(200, { capabilities: CAPS, topics: [TOPIC, ARCHIVED] })
          : resp(200, { capabilities: CAPS, topics: [TOPIC] });
      }
      return resp(404, {});
    });

    render(<KnowledgeBasePage projectId="p1" />);
    await waitFor(() => expect(screen.getByText(TOPIC.title)).toBeInTheDocument());
    expect(screen.queryByText(ARCHIVED.title)).not.toBeInTheDocument();
    expect(calls.some((call) => call.url.includes("status=all"))).toBe(false);

    await user.click(screen.getByTestId("knowledge-show-archived"));

    await waitFor(() => expect(screen.getByText(ARCHIVED.title)).toBeInTheDocument());
    expect(calls.some((call) => call.url.includes("status=all"))).toBe(true);
    // The archived row is marked, and does not pretend to be editable.
    expect(screen.getByTestId(`knowledge-card-archived-${ARCHIVED.id}`)).toBeInTheDocument();
    expect(screen.getByText("Archived")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restore" })).toBeInTheDocument();
    // Only the ACTIVE row keeps Edit/Archive — the server refuses a PATCH on
    // an archived row, so the console does not offer one.
    expect(screen.getAllByRole("button", { name: "Edit entry" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Archive" })).toHaveLength(1);
  });

  it("asks before restoring, and cancelling sends nothing", async () => {
    const user = userEvent.setup();
    const calls = stubFetch((url, init) => {
      if (url.includes("/api/context/topics?") && (!init.method || init.method === "GET")) {
        return resp(200, { capabilities: CAPS, topics: url.includes("status=all") ? [ARCHIVED] : [] });
      }
      return resp(404, {});
    });

    render(<KnowledgeBasePage projectId="p1" />);
    await waitFor(() => expect(screen.getByTestId("knowledge-empty")).toBeInTheDocument());
    await user.click(screen.getByTestId("knowledge-show-archived"));
    await user.click(await screen.findByRole("button", { name: "Restore" }));

    const dialog = await screen.findByTestId("knowledge-restore-confirm");
    expect(dialog).toHaveTextContent(ARCHIVED.title);
    expect(calls.some((call) => call.url.includes("/restore"))).toBe(false);

    await user.click(screen.getByTestId("knowledge-restore-confirm-cancel"));
    expect(calls.some((call) => call.url.includes("/restore"))).toBe(false);
    expect(screen.getByText(ARCHIVED.title)).toBeInTheDocument();
  });

  it("posts the restore route with the row's version once the confirmation is accepted", async () => {
    const user = userEvent.setup();
    const calls = stubFetch((url, init) => {
      if (url.includes("/api/context/topics?") && (!init.method || init.method === "GET")) {
        return resp(200, { capabilities: CAPS, topics: url.includes("status=all") ? [ARCHIVED] : [] });
      }
      if (url.includes(`/api/context/topics/${ARCHIVED.id}/restore`) && init.method === "POST") {
        return resp(200, { ...ARCHIVED, status: "active", version_number: 5 });
      }
      return resp(404, {});
    });

    render(<KnowledgeBasePage projectId="p1" />);
    await waitFor(() => expect(screen.getByTestId("knowledge-empty")).toBeInTheDocument());
    await user.click(screen.getByTestId("knowledge-show-archived"));
    await user.click(await screen.findByRole("button", { name: "Restore" }));
    await user.click(await screen.findByTestId("knowledge-restore-confirm-accept"));

    const restore = calls.filter((call) => call.url.includes("/restore"));
    expect(restore).toHaveLength(1);
    expect(restore[0].url).toContain("project_id=p1");
    expect(JSON.parse(String(restore[0].init.body))).toEqual({ expected_version: 4 });
    // The row comes back active in place: the badge and the Restore action go.
    await waitFor(() =>
      expect(screen.queryByTestId(`knowledge-card-archived-${ARCHIVED.id}`)).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: "Edit entry" })).toBeInTheDocument();
    expect(screen.getByText("v5")).toBeInTheDocument();
  });

  it("shows the server's exact refusal on the card when the restore fails", async () => {
    const user = userEvent.setup();
    stubFetch((url, init) => {
      if (url.includes("/api/context/topics?") && (!init.method || init.method === "GET")) {
        return resp(200, { capabilities: CAPS, topics: url.includes("status=all") ? [ARCHIVED] : [] });
      }
      if (url.includes(`/api/context/topics/${ARCHIVED.id}/restore`) && init.method === "POST") {
        return resp(409, {
          code: "not_archived",
          message: "This topic is not archived, so there is nothing to restore.",
        });
      }
      return resp(404, {});
    });

    render(<KnowledgeBasePage projectId="p1" />);
    await waitFor(() => expect(screen.getByTestId("knowledge-empty")).toBeInTheDocument());
    await user.click(screen.getByTestId("knowledge-show-archived"));
    await user.click(await screen.findByRole("button", { name: "Restore" }));
    await user.click(await screen.findByTestId("knowledge-restore-confirm-accept"));

    await waitFor(() =>
      expect(screen.getByTestId(`knowledge-archive-error-${ARCHIVED.id}`)).toHaveTextContent(
        "This topic is not archived, so there is nothing to restore.",
      ),
    );
  });

  it("keeps the archived row visible in place when archiving with Show archived on", async () => {
    const user = userEvent.setup();
    stubFetch((url, init) => {
      if (url.includes("/api/context/topics?") && (!init.method || init.method === "GET")) {
        return resp(200, { capabilities: CAPS, topics: [TOPIC] });
      }
      if (url.includes(`/api/context/topics/${TOPIC.id}/archive`) && init.method === "POST") {
        return resp(200, { ...TOPIC, status: "archived", version_number: 3 });
      }
      return resp(404, {});
    });

    render(<KnowledgeBasePage projectId="p1" />);
    await waitFor(() => expect(screen.getByText(TOPIC.title)).toBeInTheDocument());
    await user.click(screen.getByTestId("knowledge-show-archived"));
    await user.click(await screen.findByRole("button", { name: "Archive" }));
    await user.click(await screen.findByTestId("knowledge-archive-confirm-accept"));

    // It does not vanish: it becomes the archived row the person can restore.
    await waitFor(() =>
      expect(screen.getByTestId(`knowledge-card-archived-${TOPIC.id}`)).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: "Restore" })).toBeInTheDocument();
  });
});

describe("KnowledgeBasePage — the schema-context generation reaches the server", () => {
  it("posts project_id in the body and reports the counts the generator returned", async () => {
    const user = userEvent.setup();
    const calls = stubFetch((url, init) => {
      if (url.includes("/api/context/topics?") && (!init.method || init.method === "GET")) {
        return resp(200, { capabilities: CAPS, topics: [TOPIC] });
      }
      if (url.includes("/api/admin/context/generate-schema-context") && init.method === "POST") {
        return resp(200, { processed: 12, updated: 4, skipped: 8, errors: [] });
      }
      return resp(404, {});
    });

    render(<KnowledgeBasePage projectId="p1" />);
    await waitFor(() => expect(screen.getByText(TOPIC.title)).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /Generate schema context/i }));

    const generate = calls.find((call) => call.url.includes("/api/admin/context/generate-schema-context"));
    expect(generate).toBeDefined();
    // The handler reads project_id from the JSON body; a query-string-only call 422s.
    expect(JSON.parse(String(generate?.init.body))).toEqual({ project_id: "p1" });

    await waitFor(() => {
      expect(screen.getByTestId("knowledge-schema-notice")).toHaveTextContent(
        "Read 12 warehouse tables: 4 entries written, 8 already up to date.",
      );
    });
  });

  it("names the gesture that repairs when the route refuses the identity", async () => {
    const user = userEvent.setup();
    stubFetch((url, init) => {
      if (url.includes("/api/context/topics?") && (!init.method || init.method === "GET")) {
        return resp(200, { capabilities: CAPS, topics: [TOPIC] });
      }
      if (url.includes("/api/admin/context/generate-schema-context") && init.method === "POST") {
        return resp(403, { code: "forbidden", message: "Administrator access required" });
      }
      return resp(404, {});
    });

    render(<KnowledgeBasePage projectId="p1" />);
    await waitFor(() => expect(screen.getByText(TOPIC.title)).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /Generate schema context/i }));

    await waitFor(() => {
      expect(screen.getByTestId("knowledge-schema-notice")).toHaveTextContent(
        "Ask a platform administrator to generate the schema context for this project.",
      );
    });
  });

  it("is disabled for a read-only identity rather than sending a refused write", async () => {
    stubFetch((url, init) =>
      url.includes("/api/context/topics?") && (!init.method || init.method === "GET")
        ? resp(200, { capabilities: { ...CAPS, can_write: false }, topics: [TOPIC] })
        : resp(404, {}),
    );

    render(<KnowledgeBasePage projectId="p1" />);
    await waitFor(() => expect(screen.getByText(TOPIC.title)).toBeInTheDocument());

    expect(screen.getByRole("button", { name: /Generate schema context/i })).toBeDisabled();
  });
});

describe("KnowledgeBasePage — archive failures are not silent", () => {
  it("shows the server's exact message on the failing card when archive fails", async () => {
    const user = userEvent.setup();
    stubFetch((url, init) => {
      if (url.includes("/api/context/topics?") && (!init.method || init.method === "GET")) {
        return resp(200, { capabilities: CAPS, topics: [TOPIC] });
      }
      if (url.includes(`/api/context/topics/${TOPIC.id}/archive`) && init.method === "POST") {
        return resp(409, { code: "conflict", message: "Ce topic est référencé ailleurs." });
      }
      return resp(404, {});
    });

    render(<KnowledgeBasePage projectId="p1" />);
    await waitFor(() => expect(screen.getByText(TOPIC.title)).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Archive" }));
    await user.click(await screen.findByTestId("knowledge-archive-confirm-accept"));

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
        return resp(200, { capabilities: CAPS, topics: [TOPIC] });
      }
      if (url.includes(`/api/context/topics/${TOPIC.id}/archive`) && init.method === "POST") {
        return Promise.reject(new Error("network down"));
      }
      return resp(404, {});
    });

    render(<KnowledgeBasePage projectId="p1" />);
    await waitFor(() => expect(screen.getByText(TOPIC.title)).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Archive" }));
    await user.click(await screen.findByTestId("knowledge-archive-confirm-accept"));

    await waitFor(() => {
      expect(screen.getByTestId(`knowledge-archive-error-${TOPIC.id}`)).toHaveTextContent(
        "network down",
      );
    });
  });
});

describe("KnowledgeBasePage — optimistic concurrency", () => {
  it("reloads the authoritative version after a conflict while preserving the draft", async () => {
    const user = userEvent.setup();
    let patchCalls = 0;
    let retriedExpectedVersion: number | null = null;
    stubFetch((url, init) => {
      if (url.includes("/api/context/topics?") && (!init.method || init.method === "GET")) {
        return resp(200, { capabilities: CAPS, topics: [TOPIC] });
      }
      if (url.includes(`/api/context/topics/${TOPIC.id}?`) && (!init.method || init.method === "GET")) {
        return resp(200, { ...TOPIC, title: "Server title", version_number: 3 });
      }
      if (url.includes(`/api/context/topics/${TOPIC.id}?`) && init.method === "PATCH") {
        patchCalls += 1;
        const body = JSON.parse(String(init.body)) as { expected_version: number; title: string };
        if (patchCalls === 1) {
          return resp(409, { code: "version_conflict", message: "Topic changed elsewhere." });
        }
        retriedExpectedVersion = body.expected_version;
        return resp(200, { ...TOPIC, title: body.title, version_number: 4 });
      }
      return resp(404, {});
    });

    render(<KnowledgeBasePage projectId="p1" />);
    await screen.findByText(TOPIC.title);
    await user.click(screen.getByRole("button", { name: "Edit entry" }));
    const title = screen.getByDisplayValue(TOPIC.title);
    await user.clear(title);
    await user.type(title, "My preserved draft");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText(/Latest version v3 loaded/)).toBeInTheDocument();
    expect(screen.getByDisplayValue("My preserved draft")).toBeInTheDocument();
    expect(screen.getByText("Server title")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(retriedExpectedVersion).toBe(3));
  });

  it("restores focus to the opening control when the editor closes", async () => {
    const user = userEvent.setup();
    stubFetch((url) => url.includes("/api/context/topics")
      ? resp(200, { capabilities: CAPS, topics: [TOPIC] })
      : resp(404, {}));
    render(<KnowledgeBasePage projectId="p1" />);
    const edit = await screen.findByRole("button", { name: "Edit entry" });
    await user.click(edit);
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(edit).toHaveFocus());
  });
});
