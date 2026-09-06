/**
 * Story 60.1 — the client's value mapping tables, on screen.
 *
 * Four things are held here, and they are the four the story asks for:
 *
 *   1. the list shows one row per table with what it TRANSLATES and the real
 *      number of Datastreams it is applied to;
 *   2. opening a table shows its pairs AND its assignments with the named field;
 *   3. the deletion confirmation NAMES the number of Datastreams before the act,
 *      not after it;
 *   4. « Vide » and « Cassé » are two distinct sentences, and a count that could
 *      not be read reads `unknown` — never `0`.
 */
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import TransformationsLibrary from "../governance/TransformationsLibrary";

const PROJECT = "proj_EXAMPLE";

function ok(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

function fail(status: number, code: string): Response {
  return {
    ok: false,
    status,
    json: async () => ({ code, message: "refused" }),
    text: async () => code,
  } as Response;
}

function table(overrides: Record<string, unknown> = {}) {
  return {
    id: "vmt_1",
    name: "Product lines",
    description: null,
    scope_level: "PROJECT",
    project_id: PROJECT,
    entry_count: 50,
    assignment_count: 7,
    datastream_count: 6,
    sample_source_field: "a_raw_column",
    updated_at: "2026-08-08T09:00:00Z",
    ...overrides,
  };
}

function detail(overrides: Record<string, unknown> = {}) {
  return {
    table: table(),
    entries: [
      { id: "vment_1", source_value: "raw-1", canonical_value: "Line A" },
      { id: "vment_2", source_value: "raw-2", canonical_value: "Line B" },
    ],
    impact_state: "known",
    datastream_count: 6,
    assignments: [
      {
        assignment_id: "vmasg_1",
        datastream_id: "ds_1",
        datastream_name: "Fixture stream",
        source_field: "a_raw_column",
      },
    ],
    ...overrides,
  };
}

/** Route the fetch mock by URL, so each test states what each address answers. */
function serve(routes: Array<[RegExp, Response | (() => Response)]>) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      calls.push(url);
      for (const [pattern, answer] of routes) {
        if (pattern.test(url)) {
          return Promise.resolve(typeof answer === "function" ? answer() : answer);
        }
      }
      return Promise.resolve(fail(404, "not_found"));
    }),
  );
  return calls;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("the value mapping tables list", () => {
  it("shows one row per table with what it translates and its Datastream count", async () => {
    serve([[/value-mapping-tables$/, ok({ tables: [table()] })]]);
    render(<TransformationsLibrary projectId={PROJECT} />);

    const row = await screen.findByTestId("value-table-vmt_1");
    expect(within(row).getByText("Product lines")).toBeInTheDocument();
    // Derived from the assignment, never typed a second time.
    expect(within(row).getByText("a_raw_column → Product lines")).toBeInTheDocument();
    expect(within(row).getByText("PROJECT")).toBeInTheDocument();
    expect(within(row).getByText("50")).toBeInTheDocument();
    expect(within(row).getByText("6")).toBeInTheDocument();
  });

  it("says « no mapping table yet » and names who writes one", async () => {
    serve([[/value-mapping-tables$/, ok({ tables: [] })]]);
    render(<TransformationsLibrary projectId={PROJECT} />);

    expect(await screen.findByText("No mapping table on this Project yet.")).toBeInTheDocument();
    expect(screen.getByTestId("library-empty")).toHaveTextContent(
      "Governance › Semantic Model › Value Tables",
    );
    // EMPTY is not BROKEN: the broken sentence is absent.
    expect(screen.queryByTestId("library-broken")).not.toBeInTheDocument();
  });

  it("says « could not be read » and renders NO list when the read fails", async () => {
    serve([[/value-mapping-tables$/, fail(503, "unavailable")]]);
    render(<TransformationsLibrary projectId={PROJECT} />);

    const broken = await screen.findByTestId("library-broken");
    expect(broken).toHaveTextContent("this is not a Project without value tables");
    expect(
      screen.getByText("The transformations library could not be read."),
    ).toBeInTheDocument();
    // The two sentences are distinct, and only one of them is on screen.
    expect(screen.queryByText("No mapping table on this Project yet.")).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    // And nothing can be created from a screen that did not read.
    expect(screen.getByTestId("new-value-table")).toBeDisabled();
  });

  it("prints `unknown` and never `0` when the count could not be measured", async () => {
    serve([
      [
        /value-mapping-tables$/,
        ok({ tables: [table({ datastream_count: null, entry_count: null })] }),
      ],
    ]);
    render(<TransformationsLibrary projectId={PROJECT} />);

    const row = await screen.findByTestId("value-table-vmt_1");
    expect(within(row).getAllByText("unknown")).toHaveLength(2);
    expect(within(row).queryByText("0")).not.toBeInTheDocument();
  });
});

describe("opening one table", () => {
  it("shows its pairs and its assignments with the named field", async () => {
    const user = userEvent.setup();
    serve([
      [/value-mapping-tables\/vmt_1$/, ok(detail())],
      [/value-mapping-tables$/, ok({ tables: [table()] })],
    ]);
    render(<TransformationsLibrary projectId={PROJECT} />);

    await user.click(await screen.findByTestId("open-vmt_1"));

    const pairs = await screen.findByTestId("entry-vment_1");
    expect(within(pairs).getByText("raw-1")).toBeInTheDocument();
    expect(within(pairs).getByText("Line A")).toBeInTheDocument();

    const assignment = screen.getByTestId("assignment-vmasg_1");
    expect(within(assignment).getByText("Fixture stream")).toBeInTheDocument();
    expect(within(assignment).getByText("a_raw_column")).toBeInTheDocument();
    expect(screen.getByTestId("detail-impact")).toHaveTextContent("Applied to 6 Datastreams.");
  });

  it("says the impact is unknown rather than showing a zero", async () => {
    const user = userEvent.setup();
    serve([
      [
        /value-mapping-tables\/vmt_1$/,
        ok(detail({ impact_state: "unknown", datastream_count: null, assignments: [] })),
      ],
      [/value-mapping-tables$/, ok({ tables: [table()] })],
    ]);
    render(<TransformationsLibrary projectId={PROJECT} />);

    await user.click(await screen.findByTestId("open-vmt_1"));
    const impact = await screen.findByTestId("detail-impact");
    expect(impact).toHaveTextContent("Applied to unknown Datastreams");
    expect(impact).toHaveTextContent("this is not a count of zero");
  });

  it("keeps « this table could not be read » apart from « no pair yet »", async () => {
    const user = userEvent.setup();
    serve([
      [/value-mapping-tables\/vmt_1$/, fail(503, "unavailable")],
      [/value-mapping-tables$/, ok({ tables: [table()] })],
    ]);
    render(<TransformationsLibrary projectId={PROJECT} />);

    await user.click(await screen.findByTestId("open-vmt_1"));
    expect(await screen.findByTestId("detail-broken")).toHaveTextContent(
      "No pair and no assignment is shown.",
    );
    expect(screen.queryByText("No pair in this table yet.")).not.toBeInTheDocument();
  });
});

describe("the confirmation before a change", () => {
  it("names the number of Datastreams BEFORE the deletion happens", async () => {
    const user = userEvent.setup();
    const calls = serve([
      [/value-mapping-tables\/vmt_1$/, ok({ deleted: true })],
      [/value-mapping-tables$/, ok({ tables: [table()] })],
    ]);
    render(<TransformationsLibrary projectId={PROJECT} />);

    await user.click(await screen.findByTestId("delete-vmt_1"));

    const confirm = await screen.findByTestId("delete-table-confirm");
    expect(confirm).toHaveTextContent("applied to 6 Datastreams today");
    expect(confirm).toHaveTextContent("50 pairs");
    expect(confirm).toHaveTextContent("Already collected days are not rewritten.");
    // The count is stated and NOTHING has been sent yet.
    expect(calls.some((url) => url.includes("vmt_1"))).toBe(false);

    await user.click(within(confirm).getByRole("button", { name: "Delete value table" }));
    // The FIRST call carries no acknowledgement: this screen does not
    // acknowledge on the person's behalf.
    await waitFor(() => expect(calls.some((url) => url.includes("/vmt_1"))).toBe(true));
    expect(calls.some((url) => url.includes("acknowledge_impact"))).toBe(false);
  });

  it("shows the server's 409 and only then offers the acknowledgement", async () => {
    const user = userEvent.setup();
    let acknowledged = false;
    const calls = serve([
      [
        /value-mapping-tables\/vmt_1/,
        () => {
          if (calls[calls.length - 1]?.includes("acknowledge_impact=true")) {
            acknowledged = true;
            return ok({ deleted: true });
          }
          return {
            ok: false,
            status: 409,
            json: async () => ({
              code: "value_mapping_impact_not_acknowledged",
              message:
                "This table is applied to 6 Datastreams. To delete it without acknowledgement would change what those Datastreams render.",
              impact: { impact_state: "known", datastream_count: 6, assignment_count: 7 },
            }),
            text: async () => "409",
          } as Response;
        },
      ],
      [/value-mapping-tables$/, ok({ tables: [table()] })],
    ]);
    render(<TransformationsLibrary projectId={PROJECT} />);

    await user.click(await screen.findByTestId("delete-vmt_1"));
    const confirm = await screen.findByTestId("delete-table-confirm");
    await user.click(within(confirm).getByRole("button", { name: "Delete value table" }));

    // The 409 is REACHABLE from the console, and it is the server's sentence
    // and the server's count that are shown.
    expect(
      await within(confirm).findByText(/would change what those Datastreams render/),
    ).toBeInTheDocument();
    expect(confirm).toHaveTextContent("applied to 6 Datastreams today");
    expect(acknowledged).toBe(false);

    await user.click(
      within(confirm).getByRole("button", { name: "Delete anyway — impact acknowledged" }),
    );
    await waitFor(() => expect(acknowledged).toBe(true));
  });

  it("abandons the deletion when the impact could not be read", async () => {
    const user = userEvent.setup();
    serve([
      [
        /value-mapping-tables\/vmt_1/,
        {
          ok: false,
          status: 503,
          json: async () => ({
            code: "value_mapping_impact_unavailable",
            message: "unreadable",
            impact_state: "unknown",
            datastream_count: null,
          }),
          text: async () => "503",
        } as Response,
      ],
      [/value-mapping-tables$/, ok({ tables: [table()] })],
    ]);
    render(<TransformationsLibrary projectId={PROJECT} />);

    await user.click(await screen.findByTestId("delete-vmt_1"));
    const confirm = await screen.findByTestId("delete-table-confirm");
    await user.click(within(confirm).getByRole("button", { name: "Delete value table" }));

    // Fail closed: no acknowledgement is offered on an impact nobody measured.
    await waitFor(() =>
      expect(screen.queryByTestId("delete-table-confirm")).not.toBeInTheDocument(),
    );
    expect(await screen.findByTestId("library-broken")).toHaveTextContent(
      "This is not a count of zero.",
    );
  });

  it("cannot even open the confirmation when the count is unknown", async () => {
    serve([[/value-mapping-tables$/, ok({ tables: [table({ datastream_count: null })] })]]);
    render(<TransformationsLibrary projectId={PROJECT} />);

    const button = await screen.findByTestId("delete-vmt_1");
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute(
      "title",
      "The number of affected Datastreams could not be read. Nothing is deleted on an unknown impact.",
    );
  });

  it("cancelling sends nothing", async () => {
    const user = userEvent.setup();
    const calls = serve([[/value-mapping-tables$/, ok({ tables: [table()] })]]);
    render(<TransformationsLibrary projectId={PROJECT} />);

    await user.click(await screen.findByTestId("delete-vmt_1"));
    const confirm = await screen.findByTestId("delete-table-confirm");
    await user.click(within(confirm).getByRole("button", { name: "Cancel" }));

    expect(screen.queryByTestId("delete-table-confirm")).not.toBeInTheDocument();
    expect(calls.filter((url) => url.includes("vmt_1"))).toEqual([]);
  });
});

describe("writing into a table", () => {
  it("reports the refused lines of an import, with their line number", async () => {
    const user = userEvent.setup();
    serve([
      [
        /value-mapping-tables\/vmt_1\/import$/,
        ok({
          imported_count: 2,
          rejected_count: 1,
          rejected: [{ line: 2, reason: "not_two_columns", raw: "lonely" }],
          imported: [],
        }),
      ],
      [/value-mapping-tables\/vmt_1$/, ok(detail())],
      [/value-mapping-tables$/, ok({ tables: [table()] })],
    ]);
    render(<TransformationsLibrary projectId={PROJECT} />);

    await user.click(await screen.findByTestId("open-vmt_1"));
    await user.click(await screen.findByTestId("open-import"));
    await user.type(await screen.findByTestId("import-text"), "raw-1,Line A");
    await user.click(screen.getByTestId("submit-import"));

    const report = await screen.findByTestId("import-report");
    expect(report).toHaveTextContent("2 pair(s) imported");
    expect(report).toHaveTextContent("1 line(s) refused: line 2 (not two columns).");
  });

  it("assigns the table to a Datastream on a named raw field", async () => {
    const user = userEvent.setup();
    const calls = serve([
      [/value-mapping-tables\/vmt_1\/assignments$/, ok({ assignment_id: "vmasg_2" })],
      [/value-mapping-tables\/vmt_1$/, ok(detail())],
      [/value-mapping-tables$/, ok({ tables: [table()] })],
    ]);
    render(<TransformationsLibrary projectId={PROJECT} />);

    await user.click(await screen.findByTestId("open-vmt_1"));
    await user.click(await screen.findByTestId("open-assign"));
    await user.type(await screen.findByTestId("assign-datastream"), "ds_2");
    await user.type(screen.getByTestId("assign-field"), "another_raw_column");
    await user.click(screen.getByTestId("submit-assignment"));

    await waitFor(() =>
      expect(calls.some((url) => url.endsWith("/vmt_1/assignments"))).toBe(true),
    );
  });
});

describe("editing and deleting a pair", () => {
  it("edits what a source value becomes, through PATCH on the entry", async () => {
    // Story 60.5: Save now opens a confirmation that names the version this edit
    // would record, and the PATCH only leaves once the person confirmed it. The
    // confirmation itself is proven in `RuleVersionHistory.test.tsx`; what this
    // test still holds is that the verb reaching the store is unchanged.
    const user = userEvent.setup();
    const calls: Array<[string, string]> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init?: RequestInit) => {
        calls.push([url, String(init?.method ?? "GET")]);
        if (/rule-versions\/value-mapping-table\/vmt_1\/preview$/.test(url)) {
          return Promise.resolve(
            ok({
              history_state: "available",
              current_version_number: 1,
              next_version_number: 2,
              returns_to_existing_version: false,
              content_hash: "a".repeat(64),
              unchanged: false,
              impact_state: "known",
              datastream_count: 0,
              datastreams: [],
              backfill_statement: "No backfill is required.",
            }),
          );
        }
        if (/rule-versions\/value-mapping-table\/vmt_1$/.test(url)) {
          return Promise.resolve(ok({ current_version_id: null, versions: [] }));
        }
        if (/entries\/vment_1$/.test(url)) return Promise.resolve(ok({ id: "vment_1" }));
        if (/value-mapping-tables\/vmt_1$/.test(url)) return Promise.resolve(ok(detail()));
        if (/value-mapping-tables$/.test(url)) return Promise.resolve(ok({ tables: [table()] }));
        return Promise.resolve(fail(404, "not_found"));
      }),
    );
    render(<TransformationsLibrary projectId={PROJECT} />);

    await user.click(await screen.findByTestId("open-vmt_1"));
    await user.click(await screen.findByTestId("edit-entry-vment_1"));

    const input = screen.getByTestId("edit-value-vment_1");
    await user.clear(input);
    await user.type(input, "Line Z");
    await user.click(screen.getByTestId("save-entry-vment_1"));

    await user.click(await screen.findByTestId("entry-version-confirm-vment_1-confirm"));

    await waitFor(() =>
      expect(
        calls.some(([url, method]) => url.endsWith("/entries/vment_1") && method === "PATCH"),
      ).toBe(true),
    );
  });

  it("removes a pair, through DELETE on the entry", async () => {
    const user = userEvent.setup();
    const calls: Array<[string, string]> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init?: RequestInit) => {
        calls.push([url, String(init?.method ?? "GET")]);
        if (/entries\/vment_2$/.test(url)) return Promise.resolve(ok({ deleted: true }));
        if (/value-mapping-tables\/vmt_1$/.test(url)) return Promise.resolve(ok(detail()));
        if (/value-mapping-tables$/.test(url)) return Promise.resolve(ok({ tables: [table()] }));
        return Promise.resolve(fail(404, "not_found"));
      }),
    );
    render(<TransformationsLibrary projectId={PROJECT} />);

    await user.click(await screen.findByTestId("open-vmt_1"));
    await user.click(await screen.findByTestId("remove-entry-vment_2"));

    await waitFor(() =>
      expect(
        calls.some(([url, method]) => url.endsWith("/entries/vment_2") && method === "DELETE"),
      ).toBe(true),
    );
  });

  it("the source value is not editable — a different key is a different pair", async () => {
    const user = userEvent.setup();
    serve([
      [/value-mapping-tables\/vmt_1$/, ok(detail())],
      [/value-mapping-tables$/, ok({ tables: [table()] })],
    ]);
    render(<TransformationsLibrary projectId={PROJECT} />);

    await user.click(await screen.findByTestId("open-vmt_1"));
    await user.click(await screen.findByTestId("edit-entry-vment_1"));

    // Only what it BECOMES is an input; the key itself stays text.
    expect(screen.getByTestId("edit-value-vment_1")).toBeInTheDocument();
    const row = screen.getByTestId("entry-vment_1");
    expect(within(row).getByText("raw-1")).toBeInTheDocument();
  });

  it("cancelling an edit sends nothing and restores the value", async () => {
    const user = userEvent.setup();
    const calls = serve([
      [/value-mapping-tables\/vmt_1$/, ok(detail())],
      [/value-mapping-tables$/, ok({ tables: [table()] })],
    ]);
    render(<TransformationsLibrary projectId={PROJECT} />);

    await user.click(await screen.findByTestId("open-vmt_1"));
    await user.click(await screen.findByTestId("edit-entry-vment_1"));
    await user.type(screen.getByTestId("edit-value-vment_1"), "XX");
    await user.click(screen.getByTestId("cancel-entry-vment_1"));

    expect(screen.queryByTestId("edit-value-vment_1")).not.toBeInTheDocument();
    expect(within(screen.getByTestId("entry-vment_1")).getByText("Line A")).toBeInTheDocument();
    expect(calls.some((url) => url.includes("/entries/"))).toBe(false);
  });

  it("does not remove an assignment on an impact nobody could measure", async () => {
    const user = userEvent.setup();
    serve([
      [
        /value-mapping-tables\/vmt_1$/,
        ok(detail({ impact_state: "unknown", datastream_count: null })),
      ],
      [/value-mapping-tables$/, ok({ tables: [table()] })],
    ]);
    render(<TransformationsLibrary projectId={PROJECT} />);

    await user.click(await screen.findByTestId("open-vmt_1"));
    expect(await screen.findByTestId("unassign-vmasg_1")).toBeDisabled();
  });
});
