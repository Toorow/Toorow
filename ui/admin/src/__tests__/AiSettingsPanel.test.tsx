/**
 * Story 75-4 — the AI settings panel, and the three things it owes its reader.
 *
 *   1. INHERITANCE IS VISIBLE. Every field carries exactly one of three
 *      sentences beside its value — "Set here", "Inherited from organization",
 *      "Platform default" — because a value without its origin cannot be
 *      corrected: the person does not know where to go.
 *   2. THE RETURN IS AN ACT, AND ONLY WHERE IT DOES SOMETHING. A field group
 *      this scope states carries "Return to the organization value", which gives
 *      back THAT group (a clearing at the server, never a deletion); a group that
 *      is already inherited carries no such control, because undoing nothing is
 *      not an action.
 *   3. THE EMPTY STATE SAYS WHY AND NAMES THE GESTURE. A project that states
 *      nothing says so, says where the values come from, and says what to do —
 *      never a deployment state, never a table name.
 *   4. A SAVE SETS WHAT THE PERSON CHANGED, AND NOTHING ELSE. The panel used to
 *      PUT all six fields, so one Save turned every inherited value into an
 *      override of this scope: the cascade the screen exists to show went flat
 *      the first time anybody used it. The body carries the changed field.
 *   5. RETURNING ONE GROUP LEAVES THE OTHERS STANDING. The four buttons called
 *      the same scope-wide clearing, so giving the language back also gave back
 *      the rules nobody asked about. Clearing group A restates group B.
 */
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import AiSettingsPanel from "../settings/AiSettingsPanel";

const PROJECT = "proj_EXAMPLE";
const ORG = "org_EXAMPLE";

function ok(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

const PLATFORM_DEFAULTS = {
  rules_always: [],
  rules_never: [],
  query_scope: "governed_views_only",
  fiscal_calendar: { year_start_month: 1, week_start_day: "monday" },
  narrative_language: "en",
  narrative_register: "plain",
};

/** A response whose `sources` says which scope decided each field. */
function settings(
  sources: Partial<Record<string, string>> = {},
  resolved: Record<string, unknown> = {},
  own: Record<string, unknown> | null = null,
) {
  const allSources = {
    rules_always: "PLATFORM",
    rules_never: "PLATFORM",
    query_scope: "PLATFORM",
    fiscal_calendar: "PLATFORM",
    narrative_language: "PLATFORM",
    narrative_register: "PLATFORM",
    ...sources,
  };
  return {
    scope: "PROJECT",
    scope_id: PROJECT,
    org_id: ORG,
    resolved: { ...PLATFORM_DEFAULTS, ...resolved },
    sources: allSources,
    own: own
      ? {
          id: "aisetv_EXAMPLE",
          version_number: 1,
          cleared: false,
          note: null,
          created_by: "owner@example.com",
          created_at: null,
          ...own,
        }
      : null,
    inherited: null,
    platform_defaults: PLATFORM_DEFAULTS,
    history: [],
  };
}

/** Route the fetch mock by method, so each test states what each verb answers.
 *
 *  The BODY is recorded too: what a Save sends is the property findings 1 and 2
 *  are about, and a call log that only kept the verb could not see it. */
type Call = { url: string; method: string; body: Record<string, unknown> | null };

function serve(answers: { GET: unknown; DELETE?: unknown; PUT?: unknown }) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init?: RequestInit) => {
      const method = (init?.method ?? "GET").toUpperCase();
      calls.push({
        url,
        method,
        body: typeof init?.body === "string" ? JSON.parse(init.body) : null,
      });
      const body =
        method === "DELETE" ? answers.DELETE : method === "PUT" ? answers.PUT : answers.GET;
      return Promise.resolve(ok(body ?? answers.GET));
    }),
  );
  return calls;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("where each AI setting comes from", () => {
  it("names the scope beside every field, and never leaves a value unsourced", async () => {
    serve({
      GET: settings(
        { narrative_language: "PROJECT", query_scope: "ORG" },
        { narrative_language: "en-GB", query_scope: "any_published_view" },
      ),
    });
    render(<AiSettingsPanel projectId={PROJECT} />);

    expect(await screen.findByTestId("ai-setting-source-narrative_language")).toHaveTextContent(
      "Set here",
    );
    expect(screen.getByTestId("ai-setting-source-query_scope")).toHaveTextContent(
      "Inherited from organization",
    );
    expect(screen.getByTestId("ai-setting-source-fiscal_calendar")).toHaveTextContent(
      "Platform default",
    );
    // Every field of the object carries one, and exactly one.
    for (const field of [
      "rules_always",
      "rules_never",
      "query_scope",
      "fiscal_calendar",
      "narrative_language",
      "narrative_register",
    ]) {
      expect(screen.getByTestId(`ai-setting-source-${field}`)).toBeInTheDocument();
    }
  });

  it("reads the project door and shows the resolved value, not the platform default", async () => {
    const calls = serve({
      GET: settings({ narrative_language: "ORG" }, { narrative_language: "fr-FR" }),
    });
    render(<AiSettingsPanel projectId={PROJECT} />);

    await screen.findByTestId("ai-setting-source-narrative_language");
    expect(screen.getByDisplayValue("fr-FR")).toBeInTheDocument();
    expect(calls[0].url).toBe(`/api/projects/${PROJECT}/ai-settings`);
  });
});

describe("returning a value to the organization", () => {
  it("offers the action only on a group this project states", async () => {
    serve({ GET: settings({ narrative_language: "PROJECT" }, { narrative_language: "en-GB" }) });
    render(<AiSettingsPanel projectId={PROJECT} />);

    await screen.findByTestId("ai-setting-source-narrative_language");
    // The narrative group is set here, so it can be given back.
    expect(screen.getByTestId("ai-setting-clear-narrative")).toHaveTextContent(
      "Return to the organization value",
    );
    // The others are inherited, so there is nothing to undo and no control.
    expect(screen.queryByTestId("ai-setting-clear-rules")).toBeNull();
    expect(screen.queryByTestId("ai-setting-clear-calendar")).toBeNull();
    expect(screen.queryByTestId("ai-setting-clear-reach")).toBeNull();
  });

  it("calls the clearing door and shows the organization's value afterwards", async () => {
    const calls = serve({
      GET: settings({ narrative_language: "PROJECT" }, { narrative_language: "en-GB" }),
      DELETE: settings({ narrative_language: "ORG" }, { narrative_language: "fr-FR" }),
    });
    render(<AiSettingsPanel projectId={PROJECT} />);

    await screen.findByTestId("ai-setting-clear-narrative");
    await userEvent.click(screen.getByTestId("ai-setting-clear-narrative"));

    await waitFor(() =>
      expect(screen.getByTestId("ai-setting-source-narrative_language")).toHaveTextContent(
        "Inherited from organization",
      ),
    );
    expect(screen.getByDisplayValue("fr-FR")).toBeInTheDocument();
    const cleared = calls.find((call) => call.method === "DELETE");
    expect(cleared?.url).toBe(`/api/projects/${PROJECT}/ai-settings`);
    // The action is gone, because the group is now inherited.
    expect(screen.queryByTestId("ai-setting-clear-narrative")).toBeNull();
  });

  it("never words the action as a deletion", async () => {
    serve({ GET: settings({ narrative_language: "PROJECT" }, { narrative_language: "en-GB" }) });
    render(<AiSettingsPanel projectId={PROJECT} />);

    const action = await screen.findByTestId("ai-setting-clear-narrative");
    expect(action.textContent?.toLowerCase()).not.toContain("delete");
    expect(action.textContent?.toLowerCase()).not.toContain("remove");
  });
});

describe("a project that states nothing", () => {
  it("says so, says where the values come from, and names the gesture", async () => {
    serve({ GET: settings() });
    render(<AiSettingsPanel projectId={PROJECT} />);

    const empty = await screen.findByText("No AI setting is set on this project.");
    const block = empty.closest("[role='status']") as HTMLElement;
    // THE RATIFIED SENTENCE, word for word (context-hub.md, 2026-09-05).
    expect(
      within(block).getByText(
        "Everything below comes from the organization or from the platform. " +
          "Set one to change how agents answer here.",
      ),
    ).toBeInTheDocument();
    // Never a deployment state and never a table name.
    expect(block.textContent).not.toMatch(/ai_settings|migration|deploy/i);
  });

  it("disappears as soon as one field is set here", async () => {
    serve({ GET: settings({ query_scope: "PROJECT" }, { query_scope: "any_published_view" }) });
    render(<AiSettingsPanel projectId={PROJECT} />);

    await screen.findByTestId("ai-setting-source-query_scope");
    expect(screen.queryByText("No AI setting is set on this project.")).toBeNull();
  });
});

describe("what a save actually sends", () => {
  it("sends the field the person changed, and leaves the inherited ones inherited", async () => {
    const calls = serve({
      GET: settings(),
      PUT: settings({ narrative_language: "PROJECT" }, { narrative_language: "fr-FR" }),
    });
    render(<AiSettingsPanel projectId={PROJECT} />);

    const language = await screen.findByDisplayValue("en");
    await userEvent.clear(language);
    await userEvent.type(language, "fr-FR");
    await userEvent.click(screen.getByTestId("ai-settings-save"));

    const put = await waitFor(() => {
      const found = calls.find((call) => call.method === "PUT");
      expect(found).toBeDefined();
      return found!;
    });
    // ONE field. Sending the other five would have made this project state
    // rules, a reach and a calendar the person never touched.
    expect(put.body).toEqual({ narrative_language: "fr-FR" });
  });

  it("restates the field this scope already owns, so a save does not give it back", async () => {
    const calls = serve({
      GET: settings(
        { query_scope: "PROJECT" },
        { query_scope: "any_published_view" },
      ),
      PUT: settings(
        { query_scope: "PROJECT", narrative_register: "PROJECT" },
        { query_scope: "any_published_view", narrative_register: "executive" },
      ),
    });
    render(<AiSettingsPanel projectId={PROJECT} />);

    await screen.findByTestId("ai-setting-source-query_scope");
    await userEvent.selectOptions(screen.getByDisplayValue("Plain"), "executive");
    await userEvent.click(screen.getByTestId("ai-settings-save"));

    const put = await waitFor(() => {
      const found = calls.find((call) => call.method === "PUT");
      expect(found).toBeDefined();
      return found!;
    });
    // The changed one AND the one already set here: a PUT states the override
    // entire, so an owned field left out would silently be given back.
    expect(put.body).toEqual({
      query_scope: "any_published_view",
      narrative_register: "executive",
    });
  });

  it("says nothing has changed rather than asking the door to state nothing", async () => {
    const calls = serve({ GET: settings() });
    render(<AiSettingsPanel projectId={PROJECT} />);

    await screen.findByTestId("ai-settings-save");
    await userEvent.click(screen.getByTestId("ai-settings-save"));

    expect(await screen.findByTestId("ai-settings-refusal")).toHaveTextContent(
      /Nothing has changed yet/,
    );
    expect(calls.filter((call) => call.method === "PUT")).toHaveLength(0);
  });

  it("offers no writing gesture to a reader who cannot change this project", async () => {
    serve({ GET: settings({ narrative_language: "PROJECT" }, { narrative_language: "en-GB" }) });
    render(<AiSettingsPanel projectId={PROJECT} canEdit={false} />);

    expect(await screen.findByTestId("ai-settings-save")).toBeDisabled();
    expect(screen.getByTestId("ai-setting-clear-narrative")).toBeDisabled();
    // The reading half is untouched: the value and its origin are still there.
    expect(screen.getByTestId("ai-setting-source-narrative_language")).toHaveTextContent(
      "Set here",
    );
  });
});

describe("returning ONE group", () => {
  it("restates the other overrides instead of clearing the whole scope", async () => {
    const calls = serve({
      GET: settings(
        { narrative_language: "PROJECT", query_scope: "PROJECT" },
        { narrative_language: "en-GB", query_scope: "any_published_view" },
        {
          rules_always: null,
          rules_never: null,
          query_scope: "any_published_view",
          fiscal_calendar: null,
          narrative_language: "en-GB",
          narrative_register: null,
        },
      ),
      PUT: settings({ query_scope: "PROJECT" }, { query_scope: "any_published_view" }),
    });
    render(<AiSettingsPanel projectId={PROJECT} />);

    await screen.findByTestId("ai-setting-clear-narrative");
    await userEvent.click(screen.getByTestId("ai-setting-clear-narrative"));

    const put = await waitFor(() => {
      const found = calls.find((call) => call.method === "PUT");
      expect(found).toBeDefined();
      return found!;
    });
    // The reach stays this project's; only the narrative fell through.
    expect(put.body).toEqual({ query_scope: "any_published_view" });
    expect(calls.some((call) => call.method === "DELETE")).toBe(false);
    await waitFor(() =>
      expect(screen.getByTestId("ai-setting-clear-reach")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("ai-setting-clear-narrative")).toBeNull();
  });

  it("clears the scope only when the group returned was the last one it stated", async () => {
    const calls = serve({
      GET: settings(
        { query_scope: "PROJECT" },
        { query_scope: "any_published_view" },
        {
          rules_always: null,
          rules_never: null,
          query_scope: "any_published_view",
          fiscal_calendar: null,
          narrative_language: null,
          narrative_register: null,
        },
      ),
      DELETE: settings(),
    });
    render(<AiSettingsPanel projectId={PROJECT} />);

    await screen.findByTestId("ai-setting-clear-reach");
    await userEvent.click(screen.getByTestId("ai-setting-clear-reach"));

    await waitFor(() =>
      expect(calls.some((call) => call.method === "DELETE")).toBe(true),
    );
    expect(calls.some((call) => call.method === "PUT")).toBe(false);
  });
});

describe("the fiscal calendar, one field and two controls", () => {
  it("says where the calendar came from once, above both of its controls", async () => {
    serve({ GET: settings({ fiscal_calendar: "ORG" }, {
      fiscal_calendar: { year_start_month: 4, week_start_day: "sunday" },
    }) });
    render(<AiSettingsPanel projectId={PROJECT} />);

    const group = await screen.findByTestId("ai-setting-group-calendar");
    // ONE badge for the group, so the week start is not the one value on this
    // screen whose origin is never stated.
    expect(within(group).getByTestId("ai-setting-source-fiscal_calendar")).toHaveTextContent(
      "Inherited from organization",
    );
    expect(within(group).getByDisplayValue("April")).toBeInTheDocument();
    expect(within(group).getByDisplayValue("Sunday")).toBeInTheDocument();
  });
});
