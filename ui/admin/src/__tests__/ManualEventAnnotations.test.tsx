/**
 * The manual annotations of a Project — list, correct, withdraw.
 *
 * Until 2026-08-17 `app.context_events` had a create path reachable only from an
 * MCP tool and no screen at all: `GET`/`POST /api/context-events` were called by
 * nobody. These rows are read as CAUSES by `narrative` and `summarizer`, so a
 * wrong date entered once was repeated in every narration built afterwards, with
 * no way for anyone to correct it.
 */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ManualEventAnnotations from "../data/ManualEventAnnotations";

function response(status: number, body: unknown) {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

const MANUAL = {
  id: "evt_manual", project_id: "p1", event_date: "2026-08-14", type: "business",
  label: "Price increased", description: "Announced the same morning.",
  created_by: "owner@example.com", created_at: "2026-08-14T09:00:00Z", source: "manual",
  retired_at: null, retired_by: null, retired_reason: null,
  capabilities: { can_write: true, can_correct: true },
};

const FROM_CONNECTOR = {
  ...MANUAL, id: "evt_youtube", label: "Video published", source: "youtube",
  capabilities: { can_write: true, can_correct: false },
};

const RETIRED = {
  ...MANUAL, id: "evt_gone", label: "Wrong entry",
  retired_at: "2026-08-16T10:00:00Z", retired_by: "owner@example.com",
  retired_reason: "The date was wrong.",
  capabilities: { can_write: true, can_correct: false },
};

/** Records every call so the test can assert the wire, not just the pixels. */
function stub(events: unknown[], canWrite = true) {
  const calls: Array<{ url: string; method: string; body: string | null }> = [];
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => {
    calls.push({
      url: String(url),
      method: String(init?.method ?? "GET"),
      body: typeof init?.body === "string" ? init.body : null,
    });
    if (String(init?.method ?? "GET") !== "GET") return Promise.resolve(response(200, {}));
    return Promise.resolve(response(200, { events, capabilities: { can_write: canWrite } }));
  }));
  return calls;
}

afterEach(() => vi.restoreAllMocks());

it("lists the annotations a project has written", async () => {
  const calls = stub([MANUAL]);
  render(<ManualEventAnnotations projectId="p1" />);
  expect(await screen.findByText("Price increased")).toBeInTheDocument();
  expect(calls[0].url).toBe("/api/context-events?project_id=p1");
});

it("hides withdrawn annotations by default and asks for them explicitly", async () => {
  const user = userEvent.setup();
  const calls = stub([MANUAL]);
  render(<ManualEventAnnotations projectId="p1" />);
  await screen.findByText("Price increased");
  // The default read serves the live rows; a retired one is not silently
  // missing from a list that claims to be complete — it is asked for.
  expect(calls[0].url).not.toContain("include_retired");
  await user.click(screen.getByTestId("manual-annotations-include-retired"));
  await vi.waitFor(() => {
    expect(calls.some((call) => call.url.includes("include_retired=true"))).toBe(true);
  });
});

it("says a withdrawn annotation was withdrawn, by whom and why", async () => {
  stub([RETIRED]);
  render(<ManualEventAnnotations projectId="p1" />);
  const row = await screen.findByTestId("manual-annotation-retired-evt_gone");
  expect(row).toHaveTextContent("owner@example.com");
  expect(row).toHaveTextContent("The date was wrong.");
});

it("offers no correction on a connector-emitted event, reading the server's own answer", async () => {
  // Not inferred from `source` on the client: the server decides, per row, and
  // sends `can_correct`. A button the server would refuse is not an offer.
  stub([FROM_CONNECTOR]);
  render(<ManualEventAnnotations projectId="p1" />);
  expect(await screen.findByText("Video published")).toBeInTheDocument();
  expect(screen.queryByTestId("manual-annotation-correct-evt_youtube")).not.toBeInTheDocument();
  expect(screen.queryByTestId("manual-annotation-withdraw-evt_youtube")).not.toBeInTheDocument();
  // And it says where the row came from, so the absence of the button is
  // explained rather than mysterious.
  expect(screen.getByTestId("manual-annotation-evt_youtube")).toHaveTextContent("emitted by youtube");
});

it("corrects an annotation through PATCH, carrying only what was named", async () => {
  const user = userEvent.setup();
  const calls = stub([MANUAL]);
  render(<ManualEventAnnotations projectId="p1" />);
  await user.click(await screen.findByTestId("manual-annotation-correct-evt_manual"));
  const label = screen.getByTestId("manual-annotation-label");
  await user.clear(label);
  await user.type(label, "Price increased on the main plan");
  await user.click(screen.getByTestId("manual-annotation-save"));

  const patch = await vi.waitFor(() => {
    const found = calls.find((call) => call.method === "PATCH");
    expect(found).toBeTruthy();
    return found!;
  });
  expect(patch.url).toBe("/api/context-events/evt_manual?project_id=p1");
  expect(JSON.parse(patch.body ?? "{}")).toMatchObject({ label: "Price increased on the main plan" });
});

it("writes a new annotation, the gesture that had no screen at all", async () => {
  const user = userEvent.setup();
  const calls = stub([]);
  render(<ManualEventAnnotations projectId="p1" />);
  await user.click(await screen.findByTestId("manual-annotations-add"));
  await user.type(screen.getByTestId("manual-annotation-label"), "Outage");
  await user.type(screen.getByTestId("manual-annotation-date"), "2026-08-17");
  await user.click(screen.getByTestId("manual-annotation-save"));

  const post = await vi.waitFor(() => {
    const found = calls.find((call) => call.method === "POST");
    expect(found).toBeTruthy();
    return found!;
  });
  expect(post.url).toBe("/api/context-events?project_id=p1");
  expect(JSON.parse(post.body ?? "{}")).toMatchObject({ label: "Outage", event_date: "2026-08-17" });
});

it("refuses to withdraw without a reason, before asking the server", async () => {
  const user = userEvent.setup();
  const calls = stub([MANUAL]);
  render(<ManualEventAnnotations projectId="p1" />);
  await user.click(await screen.findByTestId("manual-annotation-withdraw-evt_manual"));
  await user.click(await screen.findByTestId("manual-annotation-withdraw-accept"));
  // The server requires it; asking here means the refusal is never the way a
  // person learns it.
  expect(calls.some((call) => call.url.includes("/retire"))).toBe(false);
});

it("withdraws with its reason, and says in words that this is not a delete", async () => {
  const user = userEvent.setup();
  const calls = stub([MANUAL]);
  render(<ManualEventAnnotations projectId="p1" />);
  await user.click(await screen.findByTestId("manual-annotation-withdraw-evt_manual"));
  const dialog = await screen.findByTestId("manual-annotation-withdraw-confirm");
  expect(dialog).toHaveTextContent("It is not deleted");
  await user.type(screen.getByTestId("manual-annotation-withdraw-reason"), "The date was wrong.");
  await user.click(within(dialog).getByTestId("manual-annotation-withdraw-accept"));

  const retire = await vi.waitFor(() => {
    const found = calls.find((call) => call.url.includes("/retire"));
    expect(found).toBeTruthy();
    return found!;
  });
  expect(retire.url).toBe("/api/context-events/evt_manual/retire?project_id=p1");
  expect(JSON.parse(retire.body ?? "{}")).toEqual({ reason: "The date was wrong." });
});

it("names the gesture that fills an empty list, never a deployment state", async () => {
  stub([]);
  render(<ManualEventAnnotations projectId="p1" />);
  const empty = await screen.findByTestId("manual-annotations-empty");
  expect(empty).toHaveTextContent(/price change, a launch or an outage/i);
});

it("keeps the server's sentence when a correction is refused", async () => {
  const user = userEvent.setup();
  vi.stubGlobal("fetch", vi.fn((_url: string, init?: RequestInit) => {
    if (String(init?.method ?? "GET") === "PATCH") {
      return Promise.resolve(response(409, {
        code: "not_correctable",
        message: "This event was emitted by the youtube connector. Correct it in that Datastream's Event Configuration.",
      }));
    }
    return Promise.resolve(response(200, { events: [MANUAL], capabilities: { can_write: true } }));
  }));
  render(<ManualEventAnnotations projectId="p1" />);
  await user.click(await screen.findByTestId("manual-annotation-correct-evt_manual"));
  await user.click(screen.getByTestId("manual-annotation-save"));
  // The refusal NAMES the gesture that repairs; a generic message would drop
  // the only part a person can act on.
  expect(await screen.findByTestId("manual-annotation-save-error"))
    .toHaveTextContent("Event Configuration");
});

it("does not render a row it could only half read", async () => {
  // A row with no id cannot be corrected or withdrawn, and one with no date is
  // not about a day. Rendering it would put a line on screen that looks like
  // evidence and answers none of the questions this panel exists to answer.
  stub([{ label: "Legacy launch" }]);
  render(<ManualEventAnnotations projectId="p1" />);
  expect(await screen.findByTestId("manual-annotations-error")).toBeInTheDocument();
  expect(screen.queryByText("Legacy launch")).not.toBeInTheDocument();
});

it("does not report a shape it could not read as an empty project", async () => {
  // Dropping the unreadable rows and showing the rest would let this panel
  // claim the project observed nothing.
  stub([{ label: "Legacy launch" }]);
  render(<ManualEventAnnotations projectId="p1" />);
  expect(await screen.findByTestId("manual-annotations-error")).toHaveTextContent(/could not read/i);
  expect(screen.queryByTestId("manual-annotations-empty")).not.toBeInTheDocument();
});

// ---------------------------------------------------------------------------
// Migration 322 — an annotation names the metric it is about, or every metric.
//
// `proactive-assertions.md` is incomplete if "a context event is attached to a
// claim without being scoped to that claim's metric, connector and date". The
// server can only scope on a metric an author was able to NAME, so the pick is
// part of the repair and not decoration.
// ---------------------------------------------------------------------------

/** Like `stub`, but answers the governed-metric catalogue with a real list. */
function stubWithMetrics(events: unknown[], metrics: unknown[]) {
  const calls: Array<{ url: string; method: string; body: string | null }> = [];
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => {
    const method = String(init?.method ?? "GET");
    calls.push({ url: String(url), method, body: typeof init?.body === "string" ? init.body : null });
    if (method !== "GET") return Promise.resolve(response(200, {}));
    if (String(url).startsWith("/api/datamodel/fields")) return Promise.resolve(response(200, metrics));
    return Promise.resolve(response(200, { events, capabilities: { can_write: true } }));
  }));
  return calls;
}

it("offers only metrics the project governs, from the catalogue the server validates against", async () => {
  const user = userEvent.setup();
  const calls = stubWithMetrics([], [{ name: "cost", display_name: "Cost" }]);
  render(<ManualEventAnnotations projectId="p1" />);
  await user.click(await screen.findByTestId("manual-annotations-add"));
  const picker = screen.getByTestId("manual-annotation-metric") as HTMLSelectElement;
  // The picker reads the SAME catalogue `assert_metric_is_governed` refuses
  // against; a name it offered and the door refused would teach distrust.
  expect(calls.some((call) => call.url === "/api/datamodel/fields?project_id=p1&kind=metric")).toBe(true);
  expect(Array.from(picker.options).map((option) => option.value)).toEqual(["", "cost"]);
  // "Every metric" is the default and a real answer, never a blank line.
  expect(picker.value).toBe("");
  expect(picker.options[0].textContent).toBe("Every metric");
});

it("writes the metric an author picked, so the event is offered under that claim only", async () => {
  const user = userEvent.setup();
  const calls = stubWithMetrics([], [{ name: "cost", display_name: "Cost" }]);
  render(<ManualEventAnnotations projectId="p1" />);
  await user.click(await screen.findByTestId("manual-annotations-add"));
  await user.type(screen.getByTestId("manual-annotation-label"), "Bid raised");
  await user.type(screen.getByTestId("manual-annotation-date"), "2026-08-17");
  await user.selectOptions(screen.getByTestId("manual-annotation-metric"), "cost");
  await user.click(screen.getByTestId("manual-annotation-save"));

  const post = await vi.waitFor(() => {
    const found = calls.find((call) => call.method === "POST");
    expect(found).toBeTruthy();
    return found!;
  });
  expect(JSON.parse(post.body ?? "{}")).toMatchObject({ label: "Bid raised", metric: "cost" });
});

it("sends null, not an empty string, when the annotation is about every metric", async () => {
  const user = userEvent.setup();
  const calls = stubWithMetrics([], [{ name: "cost", display_name: "Cost" }]);
  render(<ManualEventAnnotations projectId="p1" />);
  await user.click(await screen.findByTestId("manual-annotations-add"));
  await user.type(screen.getByTestId("manual-annotation-label"), "Site outage");
  await user.type(screen.getByTestId("manual-annotation-date"), "2026-08-17");
  await user.click(screen.getByTestId("manual-annotation-save"));

  const post = await vi.waitFor(() => {
    const found = calls.find((call) => call.method === "POST");
    expect(found).toBeTruthy();
    return found!;
  });
  // A blank metric is refused by the CHECK of migration 322: it reads as a name
  // nobody can compare, which would disqualify the event under EVERY claim
  // instead of none. `null` is the word this API uses for "no narrowing".
  expect(JSON.parse(post.body ?? "{}").metric).toBeNull();
});

it("shows the scope of each annotation rather than leaving it to be guessed", async () => {
  stubWithMetrics(
    [{ ...MANUAL, metric: "cost" }, { ...MANUAL, id: "evt_wide", label: "Outage", metric: null }],
    [{ name: "cost" }],
  );
  render(<ManualEventAnnotations projectId="p1" />);
  expect(await screen.findByTestId("manual-annotation-evt_manual")).toHaveTextContent("about cost");
  expect(screen.getByTestId("manual-annotation-evt_wide")).toHaveTextContent("every metric");
});

it("keeps a narrowing the catalogue no longer lists instead of silently widening it", async () => {
  const user = userEvent.setup();
  stubWithMetrics([{ ...MANUAL, metric: "legacy_metric" }], [{ name: "cost" }]);
  render(<ManualEventAnnotations projectId="p1" />);
  await user.click(await screen.findByTestId("manual-annotation-correct-evt_manual"));
  const picker = screen.getByTestId("manual-annotation-metric") as HTMLSelectElement;
  // Opening a correction dialog must not be a way to rewrite the row's scope.
  expect(picker.value).toBe("legacy_metric");
});

it("says why the picker is empty, and what it still allows", async () => {
  const user = userEvent.setup();
  stubWithMetrics([], []);
  render(<ManualEventAnnotations projectId="p1" />);
  await user.click(await screen.findByTestId("manual-annotations-add"));
  // An empty list names the state and the answer that still works, never a
  // deployment state and never a blank picker.
  expect(await screen.findByTestId("manual-annotation-metric-notice"))
    .toHaveTextContent(/governs no metric yet/i);
});


/**
 * Migration 328 — the annotation's earlier wordings, and the state vocabulary.
 *
 * Before it, a correction OVERWROTE the row: the list route answered
 * `version_history: false` and nothing on this screen could say that an
 * annotation a narration cited last week no longer reads the same way.
 */
const CORRECTED = {
  ...MANUAL,
  id: "evt_corrected",
  label: "Price increased on the main plan",
  revision_count: 2,
  capabilities: { can_write: true, can_correct: true, version_history: true },
};

const REVISIONS = [
  {
    id: "cer_one", revision_no: 1, superseded_at: "2026-08-15T09:00:00Z",
    corrected_by: "owner@example.com", corrected_fields: ["label"],
    event_date: "2026-08-14", type: "business", label: "Price increased",
    description: "Announced the same morning.", metric: null,
  },
  {
    id: "cer_two", revision_no: 2, superseded_at: "2026-08-16T09:00:00Z",
    corrected_by: "owner@example.com", corrected_fields: ["event_date", "metric"],
    event_date: "2026-08-13", type: "business", label: "Price increased on the main plan",
    description: "Announced the same morning.", metric: "cost",
  },
];

/** A stub that can answer the history call as well as the list. */
function stubWithHistory(events: unknown[], revisions: unknown[] = REVISIONS) {
  const calls: Array<{ url: string; method: string }> = [];
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => {
    const address = String(url);
    calls.push({ url: address, method: String(init?.method ?? "GET") });
    if (address.includes("/revisions")) {
      return Promise.resolve(response(200, { event: events[0], revisions }));
    }
    return Promise.resolve(response(200, { events, capabilities: { can_write: true } }));
  }));
  return calls;
}

it("draws the state of each annotation rather than leaving it to be inferred", async () => {
  stubWithHistory([MANUAL, CORRECTED, RETIRED]);
  render(<ManualEventAnnotations projectId="p1" />);
  expect(await screen.findByTestId("manual-annotation-state-evt_manual")).toHaveTextContent("Live");
  expect(screen.getByTestId("manual-annotation-state-evt_corrected")).toHaveTextContent("Corrected");
  // A withdrawal is SHOWN, not vanished: the row keeps its place and says what
  // happened to it.
  expect(screen.getByTestId("manual-annotation-state-evt_gone")).toHaveTextContent("Withdrawn");
});

it("offers the earlier wordings only where there are some, and names how many", async () => {
  stubWithHistory([MANUAL, CORRECTED]);
  render(<ManualEventAnnotations projectId="p1" />);
  await screen.findByText("Price increased");
  expect(screen.queryByTestId("manual-annotation-history-toggle-evt_manual")).not.toBeInTheDocument();
  expect(screen.getByTestId("manual-annotation-history-toggle-evt_corrected"))
    .toHaveTextContent("2 earlier wordings");
});

it("shows what an annotation said before, who corrected it and which fields moved", async () => {
  const user = userEvent.setup();
  const calls = stubWithHistory([CORRECTED]);
  render(<ManualEventAnnotations projectId="p1" />);
  await user.click(await screen.findByTestId("manual-annotation-history-toggle-evt_corrected"));

  const history = await screen.findByTestId("manual-annotation-history-evt_corrected");
  // The WORDING first: a narration cites the wording, never a revision number.
  expect(history).toHaveTextContent("Price increased");
  expect(history).toHaveTextContent("2026-08-14");
  expect(history).toHaveTextContent("owner@example.com");
  expect(history).toHaveTextContent("event_date, metric");
  expect(calls.some((call) =>
    call.url === "/api/context-events/evt_corrected/revisions?project_id=p1")).toBe(true);
});

it("asks for a history only when one is opened, never once per row", async () => {
  const user = userEvent.setup();
  const calls = stubWithHistory([CORRECTED]);
  render(<ManualEventAnnotations projectId="p1" />);
  await screen.findByText("Price increased on the main plan");
  expect(calls.filter((call) => call.url.includes("/revisions"))).toHaveLength(0);
  await user.click(screen.getByTestId("manual-annotation-history-toggle-evt_corrected"));
  await screen.findByTestId("manual-annotation-history-evt_corrected");
  expect(calls.filter((call) => call.url.includes("/revisions"))).toHaveLength(1);
});

it("keeps the server's sentence when a history cannot be read", async () => {
  const user = userEvent.setup();
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    if (String(url).includes("/revisions")) {
      return Promise.resolve(response(404, { code: "not_found", message: "Project not found" }));
    }
    return Promise.resolve(response(200, {
      events: [CORRECTED], capabilities: { can_write: true },
    }));
  }));
  render(<ManualEventAnnotations projectId="p1" />);
  await user.click(await screen.findByTestId("manual-annotation-history-toggle-evt_corrected"));
  expect(await screen.findByTestId("manual-annotation-history-error-evt_corrected"))
    .toHaveTextContent("Project not found");
});
