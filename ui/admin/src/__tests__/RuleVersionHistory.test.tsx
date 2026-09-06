/**
 * Story 60.5 — the version history of a transformation rule, on screen.
 *
 * Four things are held here, and they are the four the story's « Montre » names:
 *
 *   1. the confirmation of a change names the VERSION NUMBER that would be
 *      created, the CONTENT HASH of the proposed body, the Datastreams the change
 *      reaches BY NAME — not a bare count — and the sentence that separates the
 *      future from the past;
 *   2. « No version has been recorded for this rule yet. » and « The version
 *      history could not be read. » are two DISTINCT sentences, and only one is
 *      ever on screen;
 *   3. a fan-out that could not be read reads "unknown" and lists nothing. A `0`
 *      would tell a person nothing depends on the rule they are changing;
 *   4. the CURRENT version is read from the server's pointer rather than from the
 *      position in the list, because a rule edited back to a body it already
 *      carried points at an older version.
 *
 * Both families are exercised through their own screen, because the component is
 * shared and a shared component proven on one caller is proven for one caller.
 */
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import CleanupRules from "../governance/CleanupRules";
import TransformationsLibrary from "../governance/TransformationsLibrary";

const PROJECT = "proj_EXAMPLE";

/** `geographic_change.NO_BACKFILL_FACT` — the GENERIC half of the statement that
 *  module composes, and the only half true of a transformation rule. Its
 *  geographic half ("retained country data is reclassified semantically") is
 *  false twice about a table of campaign names, which is why the sentence was
 *  split at its source rather than reused whole. */
const NO_BACKFILL = "No raw rewrite, no provider pull and no backfill are required.";

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

/** Route the fetch mock by URL, so each test states what each address answers. */
function serve(routes: Array<[RegExp, Response | (() => Response)]>) {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init?: RequestInit) => {
      calls.push({ url, init });
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

function version(id: string, number: number, hash: string) {
  return {
    id,
    version_number: number,
    status: "published",
    content_hash: hash,
    created_at: "2026-08-09T10:00:00+00:00",
    created_by: "owner@example.com",
  };
}

const HASH_ONE = "a".repeat(64);
const HASH_TWO = "b".repeat(64);

const RULES_LIST = /cleanup-rules$/;
const EFFECT = /cleanup-rules\/crule_1\/effect$/;
const HISTORY = /rule-versions\/cleanup-rule\/crule_1$/;
const PREVIEW = /rule-versions\/cleanup-rule\/crule_1\/preview$/;

const TABLES_LIST = /value-mapping-tables$/;
const TABLE_DETAIL = /value-mapping-tables\/vmt_1$/;
const TABLE_HISTORY = /rule-versions\/value-mapping-table\/vmt_1$/;
const TABLE_PREVIEW = /rule-versions\/value-mapping-table\/vmt_1\/preview$/;

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// The history, and its two distinct absences
// ---------------------------------------------------------------------------

describe("the recorded history of a cleanup rule", () => {
  it("lists every version and names the current one from the server's pointer", async () => {
    serve([
      [EFFECT, ok({ effect_state: "measured", affected_rows: 41, window_days: 30 })],
      [
        HISTORY,
        ok({
          object_kind: "cleanup-rule",
          object_id: "crule_1",
          // The pointer names version 1, NOT the highest number: the rule was
          // edited back to a body it already carried.
          current_version_id: "crlv_1",
          versions: [version("crlv_2", 2, HASH_TWO), version("crlv_1", 1, HASH_ONE)],
        }),
      ],
      [RULES_LIST, ok({ rules: [rule()] })],
    ]);
    render(<CleanupRules projectId={PROJECT} />);

    await userEvent.click(await screen.findByTestId("history-crule_1"));

    const older = await screen.findByTestId("version-crlv_1");
    expect(within(older).getByText("Current")).toBeInTheDocument();
    const newer = screen.getByTestId("version-crlv_2");
    expect(within(newer).getByText("Previous")).toBeInTheDocument();
    // The hash is shown, shortened for reading and complete on the title.
    expect(within(older).getByTitle(HASH_ONE)).toBeInTheDocument();
    expect(within(older).getByText("owner@example.com")).toBeInTheDocument();
  });

  it("says « no version yet » when the ledger answered and holds none", async () => {
    serve([
      [EFFECT, ok({ effect_state: "measured", affected_rows: 41, window_days: 30 })],
      [HISTORY, ok({ current_version_id: null, versions: [] })],
      [RULES_LIST, ok({ rules: [rule()] })],
    ]);
    render(<CleanupRules projectId={PROJECT} />);

    await userEvent.click(await screen.findByTestId("history-crule_1"));
    expect(
      await screen.findByText("No version has been recorded for this rule yet."),
    ).toBeInTheDocument();
    expect(screen.getByTestId("version-history-empty")).toBeInTheDocument();
    // And the OTHER sentence is nowhere on screen.
    expect(screen.queryByTestId("version-history-broken")).not.toBeInTheDocument();
  });

  it("says « could not be read » when the ledger failed, and lists nothing", async () => {
    serve([
      [EFFECT, ok({ effect_state: "measured", affected_rows: 41, window_days: 30 })],
      [HISTORY, fail(503, "rule_version_history_unavailable", "the ledger is unreachable")],
      [RULES_LIST, ok({ rules: [rule()] })],
    ]);
    render(<CleanupRules projectId={PROJECT} />);

    await userEvent.click(await screen.findByTestId("history-crule_1"));
    expect(
      await screen.findByText("The version history could not be read."),
    ).toBeInTheDocument();
    // « Vide » is the other sentence and must not be on screen at the same time.
    expect(screen.queryByTestId("version-history-empty")).not.toBeInTheDocument();
    expect(screen.queryByTestId("version-crlv_1")).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// The confirmation
// ---------------------------------------------------------------------------

describe("the confirmation of a cleanup rule change", () => {
  it("names the version, the hash, the Datastreams BY NAME and the scope sentence", async () => {
    serve([
      [EFFECT, ok({ effect_state: "measured", affected_rows: 41, window_days: 30 })],
      [
        PREVIEW,
        ok({
          history_state: "available",
          current_version_number: 1,
          next_version_number: 2,
          returns_to_existing_version: false,
          content_hash: HASH_TWO,
          unchanged: false,
          impact_state: "known",
          datastream_count: 2,
          datastreams: [
            { datastream_id: "ds_1", datastream_name: "Search — France" },
            { datastream_id: "ds_2", datastream_name: "Social — Germany" },
          ],
          backfill_statement: NO_BACKFILL,
        }),
      ],
      [HISTORY, ok({ current_version_id: "crlv_1", versions: [version("crlv_1", 1, HASH_ONE)] })],
      [RULES_LIST, ok({ rules: [rule()] })],
    ]);
    render(<CleanupRules projectId={PROJECT} />);

    await userEvent.click(await screen.findByTestId("edit-crule_1"));
    await userEvent.clear(await screen.findByTestId("edit-rule-pattern"));
    await userEvent.type(screen.getByTestId("edit-rule-pattern"), "_STAGING_");
    await userEvent.click(screen.getByTestId("submit-rule-edit"));

    expect(await screen.findByTestId("rule-version-confirm-version")).toHaveTextContent(
      "This creates version 2.",
    );
    expect(screen.getByTestId("rule-version-confirm-hash")).toHaveTextContent(HASH_TWO);
    // NAMED, not counted: "six flows change tonight" is not an answer until the
    // six are named.
    expect(screen.getByTestId("rule-version-confirm-fan-out")).toHaveTextContent(
      "It reaches 2 Datastreams: Search — France, Social — Germany.",
    );
    // Reused from the server, never re-worded in the browser.
    expect(screen.getByTestId("rule-version-confirm-scope")).toHaveTextContent(NO_BACKFILL);
  });

  it("says unknown rather than zero when the fan-out could not be read", async () => {
    serve([
      [EFFECT, ok({ effect_state: "measured", affected_rows: 41, window_days: 30 })],
      [
        PREVIEW,
        ok({
          history_state: "available",
          current_version_number: 1,
          next_version_number: 2,
          returns_to_existing_version: false,
          content_hash: HASH_TWO,
          unchanged: false,
          impact_state: "unknown",
          datastream_count: null,
          datastreams: [],
          backfill_statement: NO_BACKFILL,
        }),
      ],
      [HISTORY, ok({ current_version_id: "crlv_1", versions: [version("crlv_1", 1, HASH_ONE)] })],
      [RULES_LIST, ok({ rules: [rule()] })],
    ]);
    render(<CleanupRules projectId={PROJECT} />);

    await userEvent.click(await screen.findByTestId("edit-crule_1"));
    await userEvent.clear(await screen.findByTestId("edit-rule-pattern"));
    await userEvent.type(screen.getByTestId("edit-rule-pattern"), "_STAGING_");
    await userEvent.click(screen.getByTestId("submit-rule-edit"));

    const fanOut = await screen.findByTestId("rule-version-confirm-fan-out");
    expect(fanOut).toHaveTextContent("could not be read");
    expect(fanOut).toHaveTextContent("This is not a count of zero.");
    expect(fanOut).not.toHaveTextContent("It reaches 0");
  });

  it("only PATCHes after the person confirmed the version it named", async () => {
    const calls = serve([
      [EFFECT, ok({ effect_state: "measured", affected_rows: 41, window_days: 30 })],
      [
        PREVIEW,
        ok({
          history_state: "available",
          current_version_number: 1,
          next_version_number: 2,
          returns_to_existing_version: false,
          content_hash: HASH_TWO,
          unchanged: false,
          impact_state: "known",
          datastream_count: 0,
          datastreams: [],
          backfill_statement: NO_BACKFILL,
        }),
      ],
      [HISTORY, ok({ current_version_id: "crlv_1", versions: [version("crlv_1", 1, HASH_ONE)] })],
      [/cleanup-rules\/crule_1$/, ok({ id: "crule_1" })],
      [RULES_LIST, ok({ rules: [rule()] })],
    ]);
    render(<CleanupRules projectId={PROJECT} />);

    await userEvent.click(await screen.findByTestId("edit-crule_1"));
    await userEvent.clear(await screen.findByTestId("edit-rule-pattern"));
    await userEvent.type(screen.getByTestId("edit-rule-pattern"), "_STAGING_");
    await userEvent.click(screen.getByTestId("submit-rule-edit"));
    await screen.findByTestId("rule-version-confirm-version");

    // Nothing has been written yet: the preview is a read.
    expect(
      calls.filter((call) => (call.init?.method ?? "GET") === "PATCH"),
    ).toHaveLength(0);

    await userEvent.click(screen.getByTestId("rule-version-confirm-confirm"));
    await waitFor(() =>
      expect(
        calls.filter((call) => (call.init?.method ?? "GET") === "PATCH"),
      ).toHaveLength(1),
    );
  });
});

// ---------------------------------------------------------------------------
// The same component, the other family
// ---------------------------------------------------------------------------

describe("the same history on a value mapping table", () => {
  const table = {
    id: "vmt_1",
    name: "Product lines",
    description: null,
    scope_level: "PROJECT",
    project_id: PROJECT,
    entry_count: 1,
    assignment_count: 1,
    datastream_count: 1,
    sample_source_field: "campaign_name",
    updated_at: "2026-08-09T10:00:00+00:00",
  };
  const detail = {
    table,
    entries: [{ id: "vment_1", source_value: "raw-value-1", canonical_value: "Line A" }],
    impact_state: "known" as const,
    datastream_count: 1,
    assignments: [
      {
        assignment_id: "vmasg_1",
        datastream_id: "ds_1",
        datastream_name: "Search — France",
        source_field: "campaign_name",
      },
    ],
  };

  it("names the version, the hash and the assigned Datastream before a pair is edited", async () => {
    serve([
      [
        TABLE_PREVIEW,
        ok({
          history_state: "available",
          current_version_number: 2,
          next_version_number: 3,
          returns_to_existing_version: false,
          content_hash: HASH_TWO,
          unchanged: false,
          impact_state: "known",
          datastream_count: 1,
          datastreams: [
            {
              datastream_id: "ds_1",
              datastream_name: "Search — France",
              source_field: "campaign_name",
            },
          ],
          backfill_statement: NO_BACKFILL,
        }),
      ],
      [
        TABLE_HISTORY,
        ok({ current_version_id: "vmtv_2", versions: [version("vmtv_2", 2, HASH_TWO)] }),
      ],
      [TABLE_DETAIL, ok(detail)],
      [TABLES_LIST, ok({ tables: [table] })],
    ]);
    render(<TransformationsLibrary projectId={PROJECT} />);

    await userEvent.click(await screen.findByTestId("open-vmt_1"));
    await userEvent.click(await screen.findByTestId("edit-entry-vment_1"));
    await userEvent.clear(await screen.findByTestId("edit-value-vment_1"));
    await userEvent.type(screen.getByTestId("edit-value-vment_1"), "Line B");
    await userEvent.click(screen.getByTestId("save-entry-vment_1"));

    expect(
      await screen.findByTestId("entry-version-confirm-vment_1-version"),
    ).toHaveTextContent("This creates version 3.");
    expect(screen.getByTestId("entry-version-confirm-vment_1-hash")).toHaveTextContent(
      HASH_TWO,
    );
    expect(screen.getByTestId("entry-version-confirm-vment_1-fan-out")).toHaveTextContent(
      "It reaches 1 Datastream: Search — France (campaign_name).",
    );
    expect(screen.getByTestId("entry-version-confirm-vment_1-scope")).toHaveTextContent(
      NO_BACKFILL,
    );
  });

  it("renders the table's own history beside its pairs", async () => {
    serve([
      [
        TABLE_HISTORY,
        ok({
          current_version_id: "vmtv_2",
          versions: [version("vmtv_2", 2, HASH_TWO), version("vmtv_1", 1, HASH_ONE)],
        }),
      ],
      [TABLE_DETAIL, ok(detail)],
      [TABLES_LIST, ok({ tables: [table] })],
    ]);
    render(<TransformationsLibrary projectId={PROJECT} />);

    await userEvent.click(await screen.findByTestId("open-vmt_1"));
    const current = await screen.findByTestId("version-vmtv_2");
    expect(within(current).getByText("Current")).toBeInTheDocument();
    expect(within(screen.getByTestId("version-vmtv_1")).getByText("Previous")).toBeInTheDocument();
  });
});
