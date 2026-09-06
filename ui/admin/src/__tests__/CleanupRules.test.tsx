/**
 * Story 60.3 — the cleanup rules, on screen.
 *
 * Five things are held here, and they are the five the story asks for:
 *
 *   1. the list shows one row per rule with its condition IN WORDS, not a bare
 *      regular expression;
 *   2. an effect that could not be counted writes its SENTENCE — never a `0`;
 *   3. the deletion confirmation NAMES the number of Datastreams the rule
 *      reaches, before the act and not after it;
 *   4. « Vide » and « Cassé » are two distinct sentences, and only one is ever
 *      on screen;
 *   5. what a cleanup rule does NOT do is said in the screen: a row a provider
 *      dropped before sending it is not counted here and is not recoverable
 *      without collecting that day again.
 *
 * The file is `CleanupRules.test.tsx` and not `RowFilters.test.tsx`, which
 * `epic-60:105` named: the object is a Cleanup rule, because `filter` is already
 * taken twice by living code (`query_specs.py:57-58`, `datastream_intents.py:539`).
 * The rename is the story's arbitrage 4, and an amendment to the plan.
 */
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import CleanupRules from "../governance/CleanupRules";

const PROJECT = "proj_EXAMPLE";

function ok(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

function fail(status: number, code: string, message = "refused"): Response {
  return {
    ok: false,
    status,
    json: async () => ({ code, message }),
    text: async () => code,
  } as Response;
}

function rule(overrides: Record<string, unknown> = {}) {
  return {
    id: "crule_1",
    name: "Drop the test campaigns",
    source_field: "campaign_name",
    rule_kind: "exclude_row",
    pattern: "_TEST_",
    enabled: true,
    condition: "Keeps a row only when campaign_name does not match `_TEST_`.",
    datastream_id: null,
    datastream_count: 6,
    dry_run_state: "passed",
    dry_run_detail: null,
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

const LIST = /cleanup-rules$/;
const EFFECT = /cleanup-rules\/crule_1\/effect$/;

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("the cleanup rules list", () => {
  it("shows one row per rule with the condition in words rather than a regex", async () => {
    serve([
      [EFFECT, ok({ effect_state: "measured", affected_rows: 41, window_days: 30 })],
      [LIST, ok({ rules: [rule()] })],
    ]);
    render(<CleanupRules projectId={PROJECT} />);

    const row = await screen.findByTestId("cleanup-rule-crule_1");
    expect(within(row).getByText("Drop the test campaigns")).toBeInTheDocument();
    expect(within(row).getByText("campaign_name")).toBeInTheDocument();
    // The sentence, and not the bare pattern, is what the column carries.
    expect(
      within(row).getByText("Keeps a row only when campaign_name does not match `_TEST_`."),
    ).toBeInTheDocument();
    expect(within(row).getByText("Enabled")).toBeInTheDocument();
    expect(within(row).getByText("6")).toBeInTheDocument();
  });

  it("shows the measured effect as a number only when the server measured it", async () => {
    serve([
      [EFFECT, ok({ effect_state: "measured", affected_rows: 41, window_days: 30 })],
      [LIST, ok({ rules: [rule()] })],
    ]);
    render(<CleanupRules projectId={PROJECT} />);

    // Awaited on the CONTENT: the cell exists while it still reads "Counting…",
    // so asserting on its mere presence would race the warehouse answer.
    await waitFor(() =>
      expect(screen.getByTestId("effect-crule_1")).toHaveTextContent("41 rows over 30 days"),
    );
  });

  it("writes the sentence and NEVER a zero when the effect could not be counted", async () => {
    serve([
      [
        EFFECT,
        ok({
          effect_state: "unmeasured",
          affected_rows: null,
          window_days: 30,
          message:
            "The rows this rule changes could not be counted: no warehouse is reachable. This is not a count of zero.",
        }),
      ],
      [LIST, ok({ rules: [rule()] })],
    ]);
    render(<CleanupRules projectId={PROJECT} />);

    // Awaited on the SENTENCE, not on the cell: the cell exists while it still
    // reads "Counting…", and asserting on it too early would pass for the wrong
    // reason.
    await waitFor(() =>
      expect(screen.getByTestId("effect-crule_1")).toHaveTextContent(
        "This is not a count of zero.",
      ),
    );
    const effect = screen.getByTestId("effect-crule_1");
    expect(effect).not.toHaveTextContent("0 rows");
    expect(effect.textContent).not.toMatch(/^0\b/);
  });

  it("says so rather than printing a zero when the effect request itself fails", async () => {
    serve([
      [EFFECT, fail(503, "unavailable")],
      [LIST, ok({ rules: [rule()] })],
    ]);
    render(<CleanupRules projectId={PROJECT} />);

    await waitFor(() =>
      expect(screen.getByTestId("effect-crule_1")).toHaveTextContent(
        "This is not a count of zero.",
      ),
    );
  });

  it("reads an unmeasured reach as « unknown » and never as a count", async () => {
    serve([
      [EFFECT, ok({ effect_state: "measured", affected_rows: 0, window_days: 30 })],
      [LIST, ok({ rules: [rule({ datastream_count: null })] })],
    ]);
    render(<CleanupRules projectId={PROJECT} />);

    const row = await screen.findByTestId("cleanup-rule-crule_1");
    expect(within(row).getByText("unknown")).toBeInTheDocument();
    // And a measured zero stays a measured zero, or the guard would be vacuous.
    expect(await screen.findByTestId("effect-crule_1")).toHaveTextContent("0 rows over 30 days");
  });

  it("really disables the deletion when the reach could not be read", async () => {
    serve([
      [EFFECT, ok({ effect_state: "measured", affected_rows: 3, window_days: 30 })],
      [LIST, ok({ rules: [rule({ datastream_count: null })] })],
    ]);
    render(<CleanupRules projectId={PROJECT} />);

    expect(await screen.findByTestId("delete-crule_1")).toBeDisabled();
  });

  it("says « no cleanup rule yet » and names who writes one", async () => {
    serve([[LIST, ok({ rules: [] })]]);
    render(<CleanupRules projectId={PROJECT} />);

    expect(await screen.findByText("No cleanup rule on this Project yet.")).toBeInTheDocument();
    expect(screen.getByTestId("cleanup-empty")).toHaveTextContent(
      "Governance › Semantic Model › Cleanup Rules",
    );
    // EMPTY is not BROKEN: the broken sentence is absent.
    expect(screen.queryByTestId("cleanup-broken")).not.toBeInTheDocument();
  });

  it("says « could not be read » and renders NO list when the read fails", async () => {
    serve([[LIST, fail(503, "unavailable")]]);
    render(<CleanupRules projectId={PROJECT} />);

    const broken = await screen.findByTestId("cleanup-broken");
    expect(broken).toHaveTextContent("this is not a Project without cleanup rules");
    expect(
      screen.getByText("The transformation library could not be read."),
    ).toBeInTheDocument();
    // The two sentences are distinct, and only one of them is on screen.
    expect(screen.queryByText("No cleanup rule on this Project yet.")).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    // And nothing can be created from a screen that did not read.
    expect(screen.getByTestId("new-cleanup-rule")).toBeDisabled();
  });

  it("says in the screen what a cleanup rule cannot undo", async () => {
    serve([
      [EFFECT, ok({ effect_state: "measured", affected_rows: 41, window_days: 30 })],
      [LIST, ok({ rules: [rule()] })],
    ]);
    render(<CleanupRules projectId={PROJECT} />);

    const note = await screen.findByTestId("cleanup-scope-note");
    expect(note).toHaveTextContent("It never deletes anything collected");
    expect(note).toHaveTextContent("collecting those days again");
  });
});

describe("changing a rule", () => {
  it("names the number of Datastreams BEFORE the deletion, not after it", async () => {
    serve([
      [EFFECT, ok({ effect_state: "measured", affected_rows: 41, window_days: 30 })],
      [LIST, ok({ rules: [rule()] })],
    ]);
    render(<CleanupRules projectId={PROJECT} />);

    await userEvent.click(await screen.findByTestId("delete-crule_1"));
    const dialog = await screen.findByTestId("delete-rule-confirm");
    expect(dialog).toHaveTextContent("reaches 6 Datastreams today");
    expect(dialog).toHaveTextContent("nothing collected is touched");
  });

  it("disables a rule through the server rather than in the browser", async () => {
    const calls = serve([
      [EFFECT, ok({ effect_state: "measured", affected_rows: 41, window_days: 30 })],
      [LIST, ok({ rules: [rule()] })],
    ]);
    render(<CleanupRules projectId={PROJECT} />);

    await userEvent.click(await screen.findByTestId("toggle-crule_1"));
    await waitFor(() =>
      expect(calls.filter((url) => url.endsWith("/cleanup-rules/crule_1"))).not.toHaveLength(0),
    );
  });
});

describe("writing a rule", () => {
  it("shows the condition in words while it is being typed", async () => {
    serve([[LIST, ok({ rules: [] })]]);
    render(<CleanupRules projectId={PROJECT} />);

    await userEvent.click(await screen.findByTestId("new-cleanup-rule"));
    await userEvent.type(screen.getByTestId("rule-field"), "campaign_name");
    await userEvent.type(screen.getByTestId("rule-pattern"), "_TEST_");

    expect(screen.getByTestId("rule-condition")).toHaveTextContent(
      "Keeps a row only when campaign_name does not match `_TEST_`.",
    );
  });

  it("renders NO row when the warehouse could not answer the preview", async () => {
    serve([
      [/cleanup-rules\/preview$/, fail(503, "cleanup_rule_preview_unavailable", "no warehouse")],
      [LIST, ok({ rules: [] })],
    ]);
    render(<CleanupRules projectId={PROJECT} />);

    await userEvent.click(await screen.findByTestId("new-cleanup-rule"));
    await userEvent.type(screen.getByTestId("rule-field"), "campaign_name");
    await userEvent.type(screen.getByTestId("rule-pattern"), "_TEST_");
    await userEvent.click(screen.getByTestId("run-preview"));

    const refusal = await screen.findByTestId("preview-unavailable");
    expect(refusal).toHaveTextContent("worse than none");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("shows real rows beside what the rule does to them", async () => {
    serve([
      [
        /cleanup-rules\/preview$/,
        ok({
          preview_state: "available",
          row_count: 2,
          rows: [
            { breakdown_value: "BRAND-summer", affected: false },
            { breakdown_value: "internal_TEST_run", affected: true },
          ],
        }),
      ],
      [LIST, ok({ rules: [] })],
    ]);
    render(<CleanupRules projectId={PROJECT} />);

    await userEvent.click(await screen.findByTestId("new-cleanup-rule"));
    await userEvent.type(screen.getByTestId("rule-field"), "campaign_name");
    await userEvent.type(screen.getByTestId("rule-pattern"), "_TEST_");
    await userEvent.click(screen.getByTestId("run-preview"));

    expect(await screen.findByText("internal_TEST_run")).toBeInTheDocument();
    expect(screen.getByText("BRAND-summer")).toBeInTheDocument();
  });

  it("shows the refusal of a pattern the engine does not implement", async () => {
    serve([
      [
        /cleanup-rules$/,
        (() => {
          let first = true;
          return () => {
            if (first) {
              first = false;
              return ok({ rules: [] });
            }
            return fail(
              422,
              "cleanup_rule_refused",
              "not allowed in a cleanup rule: lookahead is not part of RE2, which is the engine both warehouses use",
            );
          };
        })(),
      ],
    ]);
    render(<CleanupRules projectId={PROJECT} />);

    await userEvent.click(await screen.findByTestId("new-cleanup-rule"));
    await userEvent.type(screen.getByTestId("rule-name"), "Lookahead");
    await userEvent.type(screen.getByTestId("rule-field"), "campaign_name");
    await userEvent.type(screen.getByTestId("rule-pattern"), "(?=x)");
    await userEvent.click(screen.getByTestId("submit-rule"));

    // Scoped: the Pattern field's own hint names RE2 too, and it is not the
    // sentence this test is about.
    expect(await screen.findByText(/lookahead is not part of RE2/)).toBeInTheDocument();
  });
});
