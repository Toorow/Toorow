/**
 * Story 50.2 AC6-AC14 — the six lenses of an immutable Result.
 *
 * The response fixtures below are the shapes `core.analyze_workbench` actually
 * composes, including the one this repository really returns today: an
 * `unavailable` Result whose missing link is `datastream_output_versions`,
 * because no Datastream has published an output yet.
 *
 * Testing that state is the point. A workbench that has only ever been shown
 * rows is a workbench that renders "0" for "we could not ask", and the person
 * reading it concludes their marketing produced nothing.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import observedAiPathParity from "../../../cards/shell/src/viz/__tests__/fixtures/observedAiPathParity.json";
import analyzeFeedbackTargets from "../../../cards/shell/src/viz/__tests__/fixtures/analyzeFeedbackTargets.json";

vi.mock("../analyze/workbenchClient", async () => {
  const actual = await vi.importActual<typeof import("../analyze/workbenchClient")>(
    "../analyze/workbenchClient",
  );
  return { ...actual, fetchResultLens: vi.fn() };
});

vi.mock("../analyze/analyzeTargets", async () => {
  const actual = await vi.importActual<typeof import("../analyze/analyzeTargets")>(
    "../analyze/analyzeTargets",
  );
  return {
    ...actual,
    // The registry entries for `result` live in `shell/navigation.ts`, which
    // another session holds. The address GRAMMAR is proved in
    // AnalyzeRoutes.test.tsx against a synthetic contract; here the builders are
    // stubbed so the lens rendering is what is under test.
    resultTarget: (_scope: unknown, resultId: string, lens = "view", evidence?: string | null) =>
      `/result/${resultId}/${lens}${evidence ? `/evidence/${evidence}` : ""}`,
    ownerTarget: (_org: string, _project: string, owner: unknown) =>
      owner ? "/owner-target" : null,
  };
});

// CHANTIER B: the declaration form writes through the governed endpoint, so the
// transport is stubbed and the exact address and body are what is asserted.
vi.mock("../lib/apiFetch", async () => {
  const actual = await vi.importActual<typeof import("../lib/apiFetch")>("../lib/apiFetch");
  return {
    ...actual,
    apiPut: vi.fn().mockResolvedValue({}),
    apiGet: vi.fn().mockResolvedValue({}),
  };
});

vi.mock("../analyze/builder/seedVisualization", () => ({
  createVisualizationFromResult: vi.fn(),
  NoSeedableFamily: class extends Error {},
  // Story 72.5, AC22: the workbench now ASKS which starting point to use before
  // it falls back. The choice reads the Chart Templates that fit this exact
  // Result; a Project with none renders the named fallback, which is the state
  // every assertion below is written against.
  compatibleTemplatesForResult: vi.fn(async () => []),
  FALLBACK_FAMILY: "table",
  FALLBACK_REASON: "No Chart Template of this Project fits this Result.",
}));

import PivotMatrix from "../../../cards/shell/src/viz/pivotMatrix";
import ResultWorkbench from "../analyze/ResultWorkbench";
import { fetchResultLens } from "../analyze/workbenchClient";
import { createVisualizationFromResult } from "../analyze/builder/seedVisualization";
import { ApiError } from "../lib/apiFetch";

const SCOPE = { organizationId: "org_EXAMPLE", projectId: "proj_EXAMPLE" };
const RESULT_ID = "qr_EXAMPLE";

function envelope(lens: string, body: unknown, outcome = "unavailable") {
  return {
    schema_version: "analyze-result-lens.v1",
    result_id: RESULT_ID,
    project_id: "proj_EXAMPLE",
    lens,
    outcome,
    content_hash: "h".repeat(64),
    query_spec_id: "qs_EXAMPLE",
    query_spec_version_id: "qsv_EXAMPLE",
    query_spec_version_number: 4,
    semantic_view_id: "sv_EXAMPLE",
    semantic_view_version_id: "svv_EXAMPLE",
    semantic_view_label: "Search performance",
    semantic_view_version_number: 3,
    started_at: "2026-07-31T09:00:00+00:00",
    ended_at: "2026-07-31T09:00:01+00:00",
    lenses: ["view", "data", "definitions", "quality", "provenance", "ai-path"],
    [lens.replace("-", "_")]: body,
  };
}

const COUNTS_WITHHELD =
  "this query could not be asked, so no row, cell or byte count describes an answer.";

const QUALITY_SUMMARY = {
  outcome: "unavailable",
  freshness: { result_ended_at: "2026-07-31T09:00:01+00:00", requested_as_of: null, state: "recorded" },
  // `null`, exactly as `core.analyze_workbench` returns for a no-answer outcome.
  // The stored counts are 0/0/false; a fixture that repeated those numbers is
  // what let "Rows 0 / Truncated No" ship for a query nobody could ask.
  completeness: {
    row_count: null,
    cell_count: null,
    truncated: null,
    row_limit: 100,
    counts_unavailable_reason: COUNTS_WITHHELD,
  },
  limitations: [
    {
      code: "datastream_output_versions",
      message: "this Datastream has no published output to query yet",
    },
  ],
};

/** The View body of the Result this repository really returns today. */
const UNAVAILABLE_VIEW = {
  outcome: "unavailable",
  query_context: {},
  quality_summary: QUALITY_SUMMARY,
  fields: [],
  values: [],
  kind_unavailable_reason: null,
  server_row_count: null,
  returned_row_count: null,
  truncated: null,
  counts_unavailable_reason: COUNTS_WITHHELD,
  read_sliced: false,
  read_slice_size: 200,
  local_interaction_scope: "local only",
};

function mount(lens: string, evidenceId: string | null = null, onNavigateLens = vi.fn()) {
  return {
    onNavigateLens,
    ...render(
      <ResultWorkbench
        scope={SCOPE}
        resultId={RESULT_ID}
        lens={lens as never}
        evidenceId={evidenceId}
        onNavigateLens={onNavigateLens}
      />,
    ),
  };
}

describe("the Result workbench", () => {
  beforeEach(() => {
    vi.mocked(fetchResultLens).mockReset();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("names the two pins in words, never with their identifiers", async () => {
    // The measured defect: the header read "Query Spec version qsv_01KZ… ·
    // Semantic View version svv_01KZ…" — the exact class story 66.8 arbitrated
    // for the Builder rail. The server serves the numbers and the view's name.
    vi.mocked(fetchResultLens).mockResolvedValue(envelope("view", UNAVAILABLE_VIEW) as never);
    mount("view");
    const header = await screen.findByText(/Query Spec v4/);
    expect(header).toHaveTextContent("Search performance v3");
    expect(header.textContent).not.toMatch(/qsv_|svv_/);
  });

  it("names the Query Spec by version in the Save-as-Report dialog too", async () => {
    // THE SAME CLASS, SIXTY LINES BELOW. The dialog printed `qsv_01KZ…` at the
    // person in the very act of pinning it — the identifier the header had just
    // stopped showing, on the same screen. A repair that fixes the instance it
    // was shown and not the class guarantees the next click fails.
    vi.mocked(fetchResultLens).mockResolvedValue(envelope("view", UNAVAILABLE_VIEW) as never);
    mount("view");
    fireEvent.click(await screen.findByRole("button", { name: /Save.*Report/i }));
    const dialog = await screen.findByText(/The Report pins Query Spec/);
    expect(dialog).toHaveTextContent("v4");
    expect(dialog.textContent).not.toMatch(/qsv_/);
  });

  it("shows the six lenses as real, copyable addresses", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(envelope("view", UNAVAILABLE_VIEW) as never);
    mount("view");
    const nav = await screen.findByRole("navigation", { name: "Result" });
    for (const label of ["View", "Data", "Definitions", "Quality", "Provenance", "AI Path"]) {
      const link = within(nav).getByRole("link", { name: label });
      // An anchor with an href, not a button: a lens has to be middle-clickable
      // and copyable, or "send me what you are looking at" is impossible.
      expect(link).toHaveAttribute("href");
    }
    expect(within(nav).getByRole("link", { name: "View" })).toHaveAttribute("aria-current", "page");
  });

  it("says `unavailable` means we could not ask, never that there is no data", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(envelope("view", UNAVAILABLE_VIEW) as never);
    mount("view");
    const banner = await screen.findByTestId("result-outcome");
    expect(banner).toHaveTextContent("could not be asked");
    expect(banner).toHaveTextContent("NOT an empty answer");
    expect(screen.getByText(/no published output/i)).toBeInTheDocument();
  });

  it("renders no count at all for an unavailable Result, never a zero", async () => {
    // The measured defect: `Rows returned 0` and `Truncated No` beside an
    // `unavailable` banner. Both read as an answer of zero for a query that was
    // never asked, which is the one thing AC9 names.
    vi.mocked(fetchResultLens).mockResolvedValue(envelope("view", UNAVAILABLE_VIEW) as never);
    mount("view");
    const tiles = await screen.findByTestId("result-view-counts");
    expect(within(tiles).getAllByText("Unavailable")).toHaveLength(2);
    // Neither tile may print a number or a verdict the Result never measured.
    expect(within(tiles).queryByText("0")).toBeNull();
    expect(within(tiles).queryByText("No")).toBeNull();
    expect(within(tiles).getByText(COUNTS_WITHHELD)).toBeInTheDocument();
  });

  it("Data withholds rows, cells and bytes for an unavailable Result", async () => {
    // "Bytes 120" was the tell: the payload really does occupy bytes, and
    // printing them beside "Rows 0" makes an unasked question look measured.
    vi.mocked(fetchResultLens).mockResolvedValue(
      envelope("data", {
        schema: [],
        rows: [],
        row_count: null,
        cell_count: null,
        byte_count: null,
        truncated: null,
        counts_unavailable_reason: COUNTS_WITHHELD,
        read_sliced: false,
        read_slice_size: 200,
        integrity: {
          result_content_hash: "h".repeat(64),
          query_spec_content_hash: "s".repeat(64),
          attempt_id: "qea_EXAMPLE",
        },
        query_context: {},
        manifest: { missing_link: "datastream_output_versions" },
        no_rows_explanation: "this Datastream has no published output to query yet",
      }) as never,
    );
    mount("data");
    const tiles = await screen.findByTestId("result-data-counts");
    // Four tiles, four withheld measurements: Rows, Cells, Bytes, Truncated.
    expect(within(tiles).getAllByText("Unavailable")).toHaveLength(4);
    expect(within(tiles).queryByText("0")).toBeNull();
    expect(within(tiles).queryByText("120")).toBeNull();
  });

  it("Quality withholds the same counts rather than reporting a healthy zero", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(
      envelope("quality", {
        ...QUALITY_SUMMARY,
        dq_evaluations: [],
        dq_unavailable_reason: "this Result's manifest references no DQ evaluation.",
        unavailable_reason: "this Datastream has no published output to query yet",
        missing_link: "datastream_output_versions",
      }) as never,
    );
    mount("quality");
    const tiles = await screen.findByTestId("result-quality-counts");
    expect(within(tiles).getAllByText("Unavailable")).toHaveLength(2);
    expect(within(tiles).queryByText("0")).toBeNull();
  });

  // -------------------------------------------------------------------------
  // CHANTIER 67-15c -- the gate's verdict reaches the screen that draws the
  // measures. Before this, `combination_check` / `metrics_not_combinable` rode
  // only `GET /api/cards`, which this console never fetches, so a refused
  // measure arrived here as a gap indistinguishable from one nobody asked for.
  // -------------------------------------------------------------------------

  function qualityWithCombination(metric_combination: unknown[]) {
    return envelope("quality", {
      ...QUALITY_SUMMARY,
      dq_evaluations: [],
      dq_unavailable_reason: "this Result's manifest references no DQ evaluation.",
      metric_combination,
    }) as never;
  }

  it("names a refused measure with the refusal AND the gesture that repairs it", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(
      qualityWithCombination([
        {
          member_id: "sc_cost",
          member_name: "cost",
          label: "Cost",
          check: "refused",
          refused: "UNRULED_OVERLAP",
          source_systems: ["google-ads", "meta-ads"],
          unavailable_reason: null,
        },
      ]),
    );
    mount("quality");

    const block = await screen.findByTestId("combination-sc_cost");
    // The words are `metricCombination.ts`'s, not this screen's.
    expect(block).toHaveTextContent("Cost — Two sources report this, and no rule says which one counts");
    expect(block).toHaveTextContent("Adding them would double-count whatever they share.");
    // Both sources named: "no total" without them is a dead end.
    expect(block).toHaveTextContent("google-ads, meta-ads");
    // And the act that repairs it, not just the diagnosis.
    expect(block).toHaveTextContent("Declare a reconciliation rule for this metric in Governance.");
  });

  it("distinguishes a total nobody checked from a total the gate allowed", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(
      qualityWithCombination([
        {
          member_id: "sc_clicks",
          member_name: "clicks",
          label: "Clicks",
          check: "not_requested",
          refused: null,
          source_systems: ["google-ads", "meta-ads"],
          unavailable_reason: null,
        },
        {
          member_id: "sc_impressions",
          member_name: "impressions",
          label: "Impressions",
          check: "verified",
          refused: null,
          source_systems: ["google-ads", "meta-ads"],
          unavailable_reason: null,
        },
      ]),
    );
    mount("quality");

    // The unchecked one gets a block of its own...
    const block = await screen.findByTestId("combination-sc_clicks");
    expect(block).toHaveTextContent("Clicks — Not checked");
    expect(block).toHaveTextContent("It may be right; nobody verified it.");
    // ...and the checked one does NOT: a reassurance repeated per measure is the
    // noise that hid the line that mattered. It is counted instead.
    expect(screen.queryByTestId("combination-sc_impressions")).toBeNull();
    expect(screen.getByTestId("result-quality-combination")).toHaveTextContent(
      "The other 1 of this Result's 2 measures carry a settled verdict.",
    );
  });

  it("does not read a measure with no recorded source as a checked single source", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(
      qualityWithCombination([
        {
          member_id: "sc_clicks",
          member_name: "clicks",
          label: "Clicks",
          check: null,
          refused: null,
          source_systems: [],
          unavailable_reason:
            "this Result's manifest records no source system for this measure, so the number "
            + "of sources behind it is unknown. It has NOT been read as one source: an "
            + "unchecked total and a single-source total are different facts.",
        },
      ]),
    );
    mount("quality");

    const block = await screen.findByTestId("combination-sc_clicks");
    expect(block).toHaveTextContent("Could not be asked");
    expect(block).toHaveTextContent("has NOT been read as one source");
    expect(block).not.toHaveTextContent("Sources behind it");
  });

  it("shows the raw status rather than inventing a sentence for one it cannot read", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(
      qualityWithCombination([
        {
          member_id: "sc_cost",
          member_name: "cost",
          label: "Cost",
          check: "refused",
          refused: "SOME_FUTURE_STATUS",
          source_systems: ["google-ads", "meta-ads"],
          unavailable_reason: null,
        },
      ]),
    );
    mount("quality");

    const block = await screen.findByTestId("combination-sc_cost");
    expect(block).toHaveTextContent("SOME_FUTURE_STATUS");
    // A wrong explanation is worse than an unexplained code: only one of the two
    // makes the reader stop looking.
    expect(block).not.toHaveTextContent("no rule says which one counts");
  });

  it("stays silent when every measure carries a settled verdict rather than crying wolf", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(
      qualityWithCombination([
        {
          member_id: "sc_clicks",
          member_name: "clicks",
          label: "Clicks",
          check: "single_source",
          refused: null,
          source_systems: ["google-search-console"],
          unavailable_reason: null,
        },
      ]),
    );
    mount("quality");

    const panel = await screen.findByTestId("result-quality-combination");
    expect(panel).toHaveTextContent("No total here was published without it.");
    expect(screen.queryByTestId("combination-sc_clicks")).toBeNull();
  });

  // CHANTIER B -- the gap is shown, and the five states do not collapse.
  const RECONCILIATION = {
    measure_id: "sc_views",
    measure_name: "views",
    total: 15713,
    breakdown_sum: 12923,
    gap: 2790,
    gap_ratio: 0.1776,
    total_datastream_id: "ds_channel",
    breakdown_datastream_id: "ds_country",
    tolerance_ratio: null,
  };

  it("Quality prints a declared difference with the reason that was declared for it", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(
      envelope("quality", {
        ...QUALITY_SUMMARY,
        dq_evaluations: [],
        dq_unavailable_reason: "none referenced",
        grain_reconciliation: [
          {
            ...RECONCILIATION,
            verdict: "expected",
            sums_to: "partial_by_design",
            reason: "YouTube suppresses geography rows below its privacy thresholds.",
            statement: "YouTube suppresses geography rows below its privacy thresholds.",
          },
        ],
      }) as never,
    );
    mount("quality");
    expect(await screen.findByText("This difference was declared")).toBeTruthy();
    expect(
      screen.getByText(/suppresses geography rows below its privacy thresholds/),
    ).toBeTruthy();
    // Both figures survive intact: nothing was adjusted so they would agree.
    expect(screen.getByText("15713")).toBeTruthy();
    expect(screen.getByText("12923")).toBeTruthy();
    expect(screen.getByText("2790")).toBeTruthy();
  });

  it("Quality never lets an unexplained difference read like a declared one", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(
      envelope("quality", {
        ...QUALITY_SUMMARY,
        dq_evaluations: [],
        dq_unavailable_reason: "none referenced",
        grain_reconciliation: [
          {
            ...RECONCILIATION,
            verdict: "undeclared",
            sums_to: null,
            reason: null,
            statement: "Nobody has stated whether this breakdown reconstitutes the total.",
          },
        ],
      }) as never,
    );
    mount("quality");
    expect(await screen.findByText("Nobody has explained this difference")).toBeTruthy();
    expect(screen.queryByText("This difference was declared")).toBeNull();
    // And the gesture that would explain it is HERE, not somewhere else.
    expect(screen.getByRole("button", { name: /State what this Datastream sums to/ })).toBeTruthy();
  });

  it("Quality declares what a breakdown sums to without leaving the panel", async () => {
    const { apiPut } = await import("../lib/apiFetch");
    vi.mocked(apiPut).mockClear();
    vi.mocked(fetchResultLens).mockResolvedValue(
      envelope("quality", {
        ...QUALITY_SUMMARY,
        dq_evaluations: [],
        dq_unavailable_reason: "none referenced",
        grain_reconciliation: [
          {
            ...RECONCILIATION,
            verdict: "undeclared",
            sums_to: null,
            reason: null,
            statement: "Nobody has stated whether this breakdown reconstitutes the total.",
          },
        ],
      }) as never,
    );
    mount("quality");

    await userEvent.click(
      await screen.findByRole("button", { name: /State what this Datastream sums to/ }),
    );
    await userEvent.type(
      screen.getByLabelText(/What it does not contain/),
      "The source withholds rows below its privacy thresholds.",
    );
    await userEvent.click(screen.getByRole("button", { name: "Declare" }));

    await waitFor(() => expect(apiPut).toHaveBeenCalledTimes(1));
    const [path, body] = vi.mocked(apiPut).mock.calls[0];
    expect(path).toContain("/governance/metric-grain/sc_views/breakdowns/ds_country");
    expect(body).toEqual({
      sums_to: "partial_by_design",
      reason: "The source withholds rows below its privacy thresholds.",
    });
    // And the frozen Result is NOT repainted under the new reason.
    expect(
      await screen.findByText(/keeps the comparison it was born with/),
    ).toBeTruthy();
    expect(screen.getByText("Nobody has explained this difference")).toBeTruthy();
  });

  it("Quality tells a failed comparison apart from nothing to compare", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(
      envelope("quality", {
        ...QUALITY_SUMMARY,
        dq_evaluations: [],
        dq_unavailable_reason: "none referenced",
        grain_reconciliation: null,
      }) as never,
    );
    mount("quality");
    expect(await screen.findByText("This comparison could not be made")).toBeTruthy();
  });

  it("Quality draws no reconciliation panel when there is nothing to compare", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(
      envelope("quality", {
        ...QUALITY_SUMMARY,
        dq_evaluations: [],
        dq_unavailable_reason: "none referenced",
        grain_reconciliation: [],
      }) as never,
    );
    mount("quality");
    await screen.findByTestId("result-quality-counts");
    expect(screen.queryByText("Against the declared total")).toBeNull();
  });

  // CHANTIER C -- the inventory of holes: asked, not paid for on every open.
  it("Quality offers the inventory only for a dimension that names a landed entity kind", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(
      envelope("quality", {
        ...QUALITY_SUMMARY,
        dq_evaluations: [],
        dq_unavailable_reason: "none referenced",
        semantic_view_version_id: "svv_EXAMPLE",
        entity_gap_candidates: [{ member_id: "sc_video", member_name: "video" }],
      }) as never,
    );
    const { apiGet } = await import("../lib/apiFetch");
    vi.mocked(apiGet).mockClear();
    mount("quality");

    const ask = await screen.findByRole("button", { name: /Which video carry no detail/ });
    // Nothing was read until it was asked for.
    expect(apiGet).not.toHaveBeenCalled();

    vi.mocked(apiGet).mockResolvedValue({
      member_id: "sc_video",
      member_name: "video",
      entity_kind: "video",
      state: "exact",
      unavailable_reason: null,
      observed_count: 519,
      detailed_count: 3,
      missing_count: 516,
      missing: ["-2vI5TE4mKU"],
      missing_truncated: true,
      observed_truncated: false,
      next_gesture: "516 of the 519 `video` measured here carry no detail.",
    } as never);
    await userEvent.click(ask);

    await waitFor(() => expect(apiGet).toHaveBeenCalledTimes(1));
    expect(vi.mocked(apiGet).mock.calls[0][0]).toContain("/analyze/entity-gaps");
    expect(await screen.findByText("519")).toBeTruthy();
    expect(screen.getByText("516")).toBeTruthy();
  });

  it("Quality never draws an unreadable inventory as a complete one", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(
      envelope("quality", {
        ...QUALITY_SUMMARY,
        dq_evaluations: [],
        dq_unavailable_reason: "none referenced",
        semantic_view_version_id: "svv_EXAMPLE",
        entity_gap_candidates: [{ member_id: "sc_video", member_name: "video" }],
      }) as never,
    );
    const { apiGet } = await import("../lib/apiFetch");
    vi.mocked(apiGet).mockClear();
    vi.mocked(apiGet).mockResolvedValue({
      member_id: "sc_video",
      member_name: "video",
      entity_kind: "video",
      state: "unavailable",
      unavailable_reason: "the published relation could not be read",
      observed_count: null,
      detailed_count: null,
      missing_count: null,
      missing: [],
      missing_truncated: false,
      observed_truncated: false,
      next_gesture: "nothing is claimed about what is missing",
    } as never);
    mount("quality");
    await userEvent.click(
      await screen.findByRole("button", { name: /Which video carry no detail/ }),
    );

    expect(await screen.findByText("This inventory could not be taken")).toBeTruthy();
    expect(screen.getByText(/No\s+coverage is claimed/)).toBeTruthy();
    // No zero anywhere: an unread relation is not an empty one.
    expect(screen.queryByText("0")).toBeNull();
  });

  it("Quality draws no inventory offer when no dimension names a landed entity kind", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(
      envelope("quality", {
        ...QUALITY_SUMMARY,
        dq_evaluations: [],
        dq_unavailable_reason: "none referenced",
        entity_gap_candidates: [],
      }) as never,
    );
    mount("quality");
    await screen.findByTestId("result-quality-counts");
    expect(screen.queryByText("What is measured here, and what is named")).toBeNull();
  });

  it("keeps the server totals beside the returned slice", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(
      envelope(
        "view",
        {
          outcome: "success",
          query_context: {},
          quality_summary: { ...QUALITY_SUMMARY, outcome: "success", limitations: [] },
          fields: ["clicks", "date"],
          values: [
            {
              row_index: 0,
              cells: [
                { field: "clicks", value: 7, datum_key: `${RESULT_ID}:0:clicks`, kind: "measure" },
                {
                  field: "date",
                  value: "2026-07-01",
                  datum_key: `${RESULT_ID}:0:date`,
                  kind: "dimension",
                },
              ],
            },
          ],
          server_row_count: 4200,
          returned_row_count: 1,
          truncated: true,
          read_sliced: true,
          read_slice_size: 200,
          local_interaction_scope:
            "Hiding rows here changes only what is shown. Any other change needs a new Result.",
        },
        "success",
      ) as never,
    );
    mount("view");
    await screen.findByTestId("result-view-values");
    expect(screen.getByText(/of 4200 recorded on the Result/)).toBeInTheDocument();
    expect(screen.getByText("Yes")).toBeInTheDocument();
    expect(screen.getByText(/Any other change needs a new Result/)).toBeInTheDocument();
  });

  it("opens an evidence address from a value and restores focus when it closes", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchResultLens).mockResolvedValue(
      envelope(
        "view",
        {
          outcome: "success",
          query_context: {},
          quality_summary: { ...QUALITY_SUMMARY, outcome: "success", limitations: [] },
          fields: ["clicks"],
          values: [
            {
              row_index: 0,
              cells: [
                { field: "clicks", value: 7, datum_key: `${RESULT_ID}:0:clicks`, kind: "measure" },
              ],
            },
          ],
          server_row_count: 1,
          returned_row_count: 1,
          truncated: false,
          read_sliced: false,
          read_slice_size: 200,
          local_interaction_scope: "local only",
        },
        "success",
      ) as never,
    );
    const onOpenTarget = vi.fn();
    const onCloseEvidence = vi.fn();
    const { rerender } = render(
      <ResultWorkbench
        scope={SCOPE}
        resultId={RESULT_ID}
        lens={"view" as never}
        evidenceId={null}
        onNavigateLens={vi.fn()}
        onOpenTarget={onOpenTarget}
        onCloseEvidence={onCloseEvidence}
      />,
    );
    await screen.findByTestId("result-view-values");
    await user.click(screen.getByRole("button", { name: /Evidence for clicks, row 1/ }));
    expect(onOpenTarget).toHaveBeenCalledWith(
      `/result/${RESULT_ID}/view/evidence/${RESULT_ID}:0:clicks`,
    );

    rerender(
      <ResultWorkbench
        scope={SCOPE}
        resultId={RESULT_ID}
        lens={"view" as never}
        evidenceId={`${RESULT_ID}:0:clicks`}
        onNavigateLens={vi.fn()}
        onOpenTarget={onOpenTarget}
        onCloseEvidence={onCloseEvidence}
      />,
    );
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(`${RESULT_ID}:0:clicks`)).toBeInTheDocument();
    // Two "Close" controls: the drawer's own action and the Radix dismiss
    // affordance. Both must close it, so either is a legitimate target.
    await user.click(within(dialog).getAllByRole("button", { name: "Close" })[0]);
    expect(onCloseEvidence).toHaveBeenCalled();
  });

  it("submits the exact datum and keeps the draft until an exact acknowledgement", async () => {
    const user = userEvent.setup();
    const surface = analyzeFeedbackTargets.surfaces.console;
    const interaction = surface.interactions.workbench;
    vi.mocked(fetchResultLens).mockResolvedValue(surface.delivery.view_lens as never);
    const requests: Array<Record<string, unknown>> = [];
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
      const request = JSON.parse(String(init?.body)) as Record<string, unknown>;
      requests.push(request);
      if (requests.length === 1) {
        return { ok: false, status: 503, json: async () => ({ code: "unavailable", message: "retry" }) };
      }
      return {
        ok: true,
        status: 200,
        json: async () => interaction.receipt,
      };
    }));

    render(
      <ResultWorkbench
        scope={{ organizationId: "org_FIXTURE", projectId: "proj_FIXTURE" }}
        resultId={analyzeFeedbackTargets.result.result_id}
        lens="view"
        onNavigateLens={vi.fn()}
      />,
    );
    await user.click(await screen.findByRole("button", { name: "Target feedback at row 2, sessions" }));
    expect(screen.getByText("Row 2 · sessions")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Not helpful" }));
    await user.type(screen.getByLabelText("Optional comment"), "Wrong value");
    await user.click(screen.getByRole("button", { name: "Submit feedback" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("draft is still here");
    expect(screen.getByLabelText("Optional comment")).toHaveValue("Wrong value");
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Feedback recorded.")).toBeInTheDocument();
    expect(requests[0]).toMatchObject({
      context: interaction.context,
      target: interaction.target,
      polarity: "negative",
      comment: "Wrong value",
    });
    expect(requests[1]).toEqual(requests[0]);
    expect(interaction.authority.result_id)
      .toBe(analyzeFeedbackTargets.result.result_id);
    expect(surface.delivery.view_lens.content_hash).toBe(interaction.authority.result_content_hash);
    expect(analyzeFeedbackTargets.stored.find(
      (row) => row.surface === "console" && row.render_id === null,
    )?.target).toEqual(interaction.target);
  });

  it("Data explains an empty Result and an unavailable one differently", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(
      envelope("data", {
        schema: [],
        rows: [],
        row_count: 0,
        cell_count: 0,
        byte_count: 12,
        truncated: false,
        read_sliced: false,
        read_slice_size: 200,
        integrity: {
          result_content_hash: "h".repeat(64),
          query_spec_content_hash: "s".repeat(64),
          attempt_id: "qea_EXAMPLE",
        },
        query_context: { grain: "day" },
        manifest: { missing_link: "datastream_output_versions" },
        no_rows_explanation: "this Datastream has no published output to query yet",
      }) as never,
    );
    mount("data");
    expect(await screen.findByText(/no published output/)).toBeInTheDocument();
    expect(screen.getByText("qea_EXAMPLE")).toBeInTheDocument();
  });

  it("Definitions says a pinned member version is unresolved instead of showing today's", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(
      envelope("definitions", {
        semantic_view: {
          id: "sv_EXAMPLE",
          version_id: "svv_EXAMPLE",
          label: "Search performance",
          version_number: 3,
          status: "published",
          resolved: true,
          owner_ref: { workspace: "governance", section: "semantic-model" },
        },
        members: [
          {
            kind: "measure",
            concept_id: "sc_clicks",
            version_id: "scv_retired",
            label: null,
            definition: null,
            expression: null,
            aggregation: null,
            additivity_class: null,
            unit: null,
            value_type: null,
            allowed_grains: [],
            resolved: false,
            unresolved_reason:
              "this exact member version is not readable; the current version has NOT been shown in its place",
            owner_ref: { workspace: "governance", section: "semantic-model" },
          },
        ],
        classification_pins: [],
        source_comparison: {
          requested: "previous_period",
          executed: true,
          period_field: "comparison_period",
          current_window: { start: "2026-07-01", end: "2026-07-31" },
          baseline_window: { start: "2026-05-31", end: "2026-06-30" },
          meaning: "the two windows are carried side by side and never summed together",
        },
      }) as never,
    );
    mount("definitions");
    expect(await screen.findByText(/has NOT been shown in its place/)).toBeInTheDocument();
  });

  /**
   * The Definitions lens used to print a reporting boundary and a time zone the
   * read never applied, beside a sentence describing a comparison `build_sql`
   * did not perform. Both controls are refused when a Query Spec is written now,
   * and the comparison states the two windows it actually labelled.
   */
  it("Definitions show the windows a comparison really read, and no unexecuted control", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(
      envelope("definitions", {
        semantic_view: {
          id: "sv_EXAMPLE",
          version_id: "svv_EXAMPLE",
          label: "Search performance",
          version_number: 3,
          status: "published",
          resolved: true,
          owner_ref: { workspace: "governance", section: "semantic-model" },
        },
        members: [],
        classification_pins: [],
        source_comparison: {
          requested: "previous_period",
          executed: true,
          period_field: "comparison_period",
          current_window: { start: "2026-07-01", end: "2026-07-31" },
          baseline_window: { start: "2026-05-31", end: "2026-06-30" },
          meaning: "never summed together",
        },
      }) as never,
    );
    mount("definitions");
    expect(await screen.findByText(/2026-05-31 to 2026-06-30/)).toBeInTheDocument();
    expect(screen.queryByText(/reporting_boundary/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^timezone$/)).not.toBeInTheDocument();
  });

  /**
   * AI-293 / chantier 67-10, and the `Incomplete if` of `analyze-and-test.md:617`:
   * "a refused metric reaches a surface as an absence, with nothing naming the
   * refusal." The definitions lens has served `additivity_class` per member since
   * story 60.2 and this screen printed it as the raw database word `non_additive`
   * in one table cell — where a person scanning for a missing total never looks.
   */
  const additivityLens = (klass: string | null) => ({
    semantic_view: {
      id: "sv_EXAMPLE",
      version_id: "svv_EXAMPLE",
      label: "Search performance",
      version_number: 3,
      status: "published",
      resolved: true,
      owner_ref: { workspace: "governance", section: "semantic-model" },
    },
    members: [
      {
        kind: "measure",
        concept_id: "sc_roas",
        version_id: "scv_1",
        label: "Return on ad spend",
        definition: "revenue divided by spend",
        expression: null,
        aggregation: null,
        additivity_class: klass,
        unit: null,
        value_type: "ratio",
        allowed_grains: [],
        resolved: true,
        unresolved_reason: null,
        owner_ref: { workspace: "governance", section: "semantic-model" },
      },
    ],
    classification_pins: [],
    source_comparison: { requested: null, meaning: null },
  });

  it("a measure that cannot be added up is NAMED, with the gesture that repairs it", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(
      envelope("definitions", additivityLens("non_additive")) as never,
    );
    mount("definitions");
    expect(
      await screen.findByText(/Return on ad spend — Cannot be added up/),
    ).toBeInTheDocument();
    expect(screen.getByText(/states a number nobody measured/)).toBeInTheDocument();
    // The gesture, not the cause.
    expect(
      screen.getByText(/Ask for the measures it is computed from/),
    ).toBeInTheDocument();
    // And never the database word, anywhere on the screen.
    expect(screen.queryByText("non_additive")).not.toBeInTheDocument();
  });

  it("a semi-additive measure is named too, because a merge may not fold it either", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(
      envelope("definitions", additivityLens("semi_additive")) as never,
    );
    mount("definitions");
    expect(
      await screen.findByText(/Return on ad spend — Cannot be added up everywhere/),
    ).toBeInTheDocument();
    expect(screen.queryByText("semi_additive")).not.toBeInTheDocument();
  });

  it("an additive Result says nothing at all — a reassurance on every screen is noise", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(
      envelope("definitions", additivityLens("additive")) as never,
    );
    mount("definitions");
    expect(await screen.findByText("Return on ad spend")).toBeInTheDocument();
    expect(
      screen.queryByText(/Measures this Result does not add up/),
    ).not.toBeInTheDocument();
    // The cell speaks the reader's language, not the schema's.
    expect(screen.getByText("Can be added up")).toBeInTheDocument();
    expect(screen.queryByText("additive")).not.toBeInTheDocument();
  });

  it("an undeclared additivity is not an accusation", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(
      envelope("definitions", additivityLens(null)) as never,
    );
    mount("definitions");
    expect(await screen.findByText("Return on ad spend")).toBeInTheDocument();
    expect(
      screen.queryByText(/Measures this Result does not add up/),
    ).not.toBeInTheDocument();
    // The cell says "Unavailable" like every other undeclared field, and the
    // measure is not accused of anything.
    expect(screen.getAllByText("Unavailable").length).toBeGreaterThan(0);
  });

  it("Quality states the missing DQ evidence with its owner rather than leaving a blank", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(
      envelope("quality", {
        ...QUALITY_SUMMARY,
        dq_evaluations: [],
        dq_unavailable_reason:
          "this Result's manifest references no DQ evaluation. Story 50.1 writes the manifest.",
        unavailable_reason: "this Datastream has no published output to query yet",
        missing_link: "datastream_output_versions",
      }) as never,
    );
    mount("quality");
    expect(await screen.findByText(/references no DQ evaluation/)).toBeInTheDocument();
    // It appears twice on purpose: once as the limitation code, once as the
    // named missing link. Neither is a duplicate of the other.
    expect(screen.getAllByText("datastream_output_versions").length).toBeGreaterThan(0);
  });

  it("Provenance names each unrecorded link and the owner of the source-tuple gap", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(
      envelope("provenance", {
        chain: [
          {
            link: "semantic_view_version",
            status: "recorded",
            identity: "svv_EXAMPLE",
            owner_ref: { workspace: "governance", section: "semantic-model" },
            reason: null,
          },
          {
            link: "published_output_relation",
            status: "not_recorded",
            identity: null,
            owner_ref: null,
            reason: "this Datastream has no published output to query yet",
          },
        ],
        missing_link: "datastream_output_versions",
        predecessor: null,
        successors: [],
        value_source_tuples: [],
        value_source_unavailable: {
          code: "manifest_carries_no_source_tuple",
          message: "this Result's manifest records the relation it read but not the tuple.",
          owner: "core.query_execution.run_execution",
        },
        freshness: { state: null, as_of: null, complete_through: null, stale_since_evaluated: false },
      }) as never,
    );
    mount("provenance");
    const gap = await screen.findByTestId("provenance-value-source-gap");
    expect(gap).toHaveTextContent("core.query_execution.run_execution");
    // The state is a WORD, not a colour: "Not recorded" has to be readable by
    // someone who cannot see the difference between two greys.
    expect(screen.getByText("Not recorded")).toBeInTheDocument();
    expect(screen.getByText("Recorded")).toBeInTheDocument();
  });

  it("Provenance shows the per-value tuples once the execution captured them", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(
      envelope(
        "provenance",
        {
          chain: [
            {
              link: "source",
              status: "recorded",
              identity: "ds_EXAMPLE",
              owner_ref: { workspace: "data", section: "datastreams" },
              reason: null,
            },
          ],
          missing_link: null,
          predecessor: null,
          successors: [],
          source_system: "google-search-console",
          value_source_tuples: [
            {
              member_id: "sc_clicks",
              source_system: "google-search-console",
              source_field: "clicks",
              pull_id: "dse_EXAMPLE",
            },
          ],
          value_source_unavailable: null,
          freshness: { state: null, as_of: null, complete_through: null, stale_since_evaluated: false },
        },
        "success",
      ) as never,
    );
    mount("provenance");
    const table = await screen.findByTestId("provenance-value-source-tuples");
    expect(within(table).getByText("sc_clicks")).toBeInTheDocument();
    expect(within(table).getByText("clicks")).toBeInTheDocument();
    expect(within(table).getByText("dse_EXAMPLE")).toBeInTheDocument();
    // The gap panel must be gone: claiming "no per-value source tuple" beside a
    // table of them is the same dishonesty in the opposite direction.
    expect(screen.queryByTestId("provenance-value-source-gap")).toBeNull();
  });

  it("offers Open in Toorow from the workbench and copies the exact address", async () => {
    // AC12's emission half. Before this, the three builders were reached by no
    // production file at all and the proof rested on a unit test of a function
    // nothing called.
    const user = userEvent.setup();
    // AFTER setup: `userEvent.setup()` installs its own clipboard stub, so a
    // spy defined before it is silently replaced and the assertion below reads
    // an empty call list.
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    vi.mocked(fetchResultLens).mockResolvedValue(
      envelope("quality", {
        ...QUALITY_SUMMARY,
        dq_evaluations: [],
        dq_unavailable_reason: "none referenced",
      }) as never,
    );
    mount("quality");
    await screen.findByTestId("result-quality-counts");
    await user.click(screen.getByRole("button", { name: "Copy Open in Toorow link" }));
    const copied = writeText.mock.calls[0][0] as string;
    expect(copied).toContain("/org/org_EXAMPLE/project/proj_EXAMPLE/analyze/explore");
    // The lens the reader is on, not a collection fallback and not the default.
    expect(copied).toContain(`/object/result/${RESULT_ID}/tab/quality`);
    expect(await screen.findByTestId("result-open-in-toorow")).toHaveTextContent(copied);
  });

  it("AI Path renders the exact literal for a human-only Result", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(
      envelope("ai-path", {
        schema_version: "observed-ai-path.v1",
        state: "human_absent",
        literal: "No AI path",
      }) as never,
    );
    mount("ai-path");
    const panel = await screen.findByTestId("result-ai-path");
    // Character for character. `null`, an empty panel and `deferred` are all
    // refused by AC11; the heading and the body both carry the literal.
    expect(within(panel).getAllByText("No AI path").length).toBeGreaterThan(0);
  });

  it("AI Path renders the canonical observed projection expanded in the Console", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchResultLens).mockResolvedValue(
      envelope("ai-path", observedAiPathParity.workbench) as never,
    );
    mount("ai-path");
    const panel = await screen.findByTestId("result-ai-path");
    expect(within(panel).getByText(new RegExp(`Path ${observedAiPathParity.workbench.path_id}`)))
      .toHaveTextContent(observedAiPathParity.workbench.outcome);
    expect(within(panel).getByTestId("ai-path-timeline")).toHaveTextContent(
      observedAiPathParity.workbench.steps[0]!.tool_name,
    );
    expect(within(panel).queryByTestId("ai-path-branch-toggle")).toBeNull();
    expect(panel.querySelector("details[data-ai-path-capability]")).toHaveAttribute("open");
    expect(within(panel).queryByText("No AI path")).toBeNull();
    await user.click(within(panel).getAllByRole("button", { name: "Target this step" })[0]);
    expect(screen.getByText("AI Path step 1")).toBeInTheDocument();
  });

  it("clears the selected target and draft when the addressed Result changes", async () => {
    const user = userEvent.setup();
    const view = {
      ...UNAVAILABLE_VIEW,
      outcome: "success",
      quality_summary: { ...QUALITY_SUMMARY, outcome: "success", limitations: [] },
      fields: ["clicks"],
      values: [{
        row_index: 0,
        cells: [{ field: "clicks", value: 7, datum_key: `${RESULT_ID}:0:clicks`, kind: "measure" }],
      }],
      server_row_count: 1,
      returned_row_count: 1,
      truncated: false,
    };
    vi.mocked(fetchResultLens)
      .mockResolvedValueOnce(envelope("view", view, "success") as never)
      .mockResolvedValueOnce({ ...envelope("view", UNAVAILABLE_VIEW), result_id: "qr_NEXT" } as never);
    const rendered = render(
      <ResultWorkbench scope={SCOPE} resultId={RESULT_ID} lens="view" onNavigateLens={vi.fn()} />,
    );
    await user.click(await screen.findByRole("button", { name: "Target feedback at row 1, clicks" }));
    await user.type(screen.getByLabelText("Optional comment"), "stale draft");
    rendered.rerender(
      <ResultWorkbench scope={SCOPE} resultId="qr_NEXT" lens="view" onNavigateLens={vi.fn()} />,
    );
    await waitFor(() => expect(screen.getByText("Answer")).toBeInTheDocument());
    expect(screen.getByLabelText("Optional comment")).toHaveValue("");
  });

  it("AI Path fails closed on a malformed Console projection", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(
      envelope("ai-path", { path_id: "aip_FOREIGN", actor: "secret@example.test" }) as never,
    );
    mount("ai-path");
    const panel = await screen.findByTestId("result-ai-path");
    expect(within(panel).getByRole("status")).toHaveTextContent(
      "This AI Path is unavailable here.",
    );
    expect(panel).not.toHaveTextContent(/aip_FOREIGN|secret@example/);
    expect(within(panel).queryByTestId("ai-path-timeline")).toBeNull();
  });

  it("refuses a response that describes another Result rather than rendering it", async () => {
    const { fetchResultLens: real } = await vi.importActual<
      typeof import("../analyze/workbenchClient")
    >("../analyze/workbenchClient");
    void real;
    vi.mocked(fetchResultLens).mockRejectedValue(
      Object.assign(
        new (await vi.importActual<typeof import("../analyze/workbenchClient")>(
          "../analyze/workbenchClient",
        )).EnvelopeMismatch("this response belongs to another Project"),
      ),
    );
    mount("view");
    expect(await screen.findByText(/does not describe this address/i)).toBeInTheDocument();
    expect(screen.getByText(/Nothing has been rendered from it/)).toBeInTheDocument();
  });

  it("announces the asynchronous read without stealing focus", async () => {
    let resolve: (value: unknown) => void = () => {};
    vi.mocked(fetchResultLens).mockReturnValue(
      new Promise((r) => {
        resolve = r;
      }) as never,
    );
    mount("quality");
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Reading the Quality lens");
    expect(document.activeElement).toBe(document.body);
    resolve(
      envelope("quality", {
        ...QUALITY_SUMMARY,
        dq_evaluations: [],
        dq_unavailable_reason: "none referenced",
      }),
    );
    await waitFor(() => expect(screen.queryByText(/Reading the Quality lens/)).toBeNull());
  });

  it("does not paint a late answer for a Result the route has left", async () => {
    let resolveFirst: (value: unknown) => void = () => {};
    vi.mocked(fetchResultLens)
      .mockReturnValueOnce(
        new Promise((r) => {
          resolveFirst = r;
        }) as never,
      )
      .mockResolvedValue(
        envelope("quality", {
          ...QUALITY_SUMMARY,
          dq_evaluations: [],
          dq_unavailable_reason: "second answer",
        }) as never,
      );
    const { rerender } = render(
      <ResultWorkbench
        scope={SCOPE}
        resultId="qr_FIRST"
        lens={"quality" as never}
        onNavigateLens={vi.fn()}
      />,
    );
    rerender(
      <ResultWorkbench
        scope={SCOPE}
        resultId="qr_SECOND"
        lens={"quality" as never}
        onNavigateLens={vi.fn()}
      />,
    );
    resolveFirst(
      envelope("quality", {
        ...QUALITY_SUMMARY,
        dq_evaluations: [],
        dq_unavailable_reason: "FIRST ANSWER, must never appear",
      }),
    );
    expect(await screen.findByText("second answer")).toBeInTheDocument();
    expect(screen.queryByText(/FIRST ANSWER/)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Story 50.4 F4 — the Visualization Builder's entry affordance.
//
// Nothing on any screen navigated to `objectType: "visualization"`:
// `grep -rn '"visualization"' src` outside `builder/` returned exactly two hits,
// the router branch and the navigation contract. The Builder was mounted at an
// address the product never produced, so `POST /visualizations` was unreachable
// from a browser and the entire create path was dead code.
// ---------------------------------------------------------------------------

describe("the Visualization Builder entry", () => {
  it("creates the Visualization from this Result's Query Spec version, then opens it", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchResultLens).mockResolvedValue(envelope("view", UNAVAILABLE_VIEW) as never);
    vi.mocked(createVisualizationFromResult).mockResolvedValue("vis_1" as never);
    const opened: string[] = [];

    render(
      <ResultWorkbench
        scope={SCOPE}
        resultId={RESULT_ID}
        lens="view"
        onNavigateLens={() => {}}
        onOpenTarget={(href) => opened.push(href)}
      />,
    );

    await user.click(await screen.findByTestId("result-build-visualization"));

    await waitFor(() =>
      // The pin comes from the Result the person is looking at — the one source
      // that cannot disagree with what they saw.
      // The pin comes from the Result; the fourth argument is the STARTING POINT
      // (story 72.5, AC22), and `undefined` here is the named fallback — this
      // control is now "start from a blank table instead".
      expect(createVisualizationFromResult).toHaveBeenCalledWith(
        "proj_EXAMPLE",
        "qsv_EXAMPLE",
        undefined,
        undefined,
      ),
    );
    await waitFor(() => expect(opened).toHaveLength(1));
    expect(opened[0]).toContain("/object/visualization/vis_1");
    expect(opened[0]).toContain("result_id=qr_EXAMPLE");
  });

  it("shows the server's refusal instead of opening anything", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchResultLens).mockResolvedValue(envelope("view", UNAVAILABLE_VIEW) as never);
    vi.mocked(createVisualizationFromResult).mockRejectedValue(
      new ApiError(422, "visualization_spec_refused", "refused", {
        code: "visualization_spec_refused",
        refusals: [
          {
            code: "unknown_member",
            message: "`clicks` is not a member of this Query Spec version.",
            subject: "/bindings/measure/0",
            remedy: "Select it in Explore first.",
          },
        ],
      }),
    );
    const opened: string[] = [];

    render(
      <ResultWorkbench
        scope={SCOPE}
        resultId={RESULT_ID}
        lens="view"
        onNavigateLens={() => {}}
        onOpenTarget={(href) => opened.push(href)}
      />,
    );

    await user.click(await screen.findByTestId("result-build-visualization"));

    const failure = await screen.findByTestId("result-build-visualization-failure");
    expect(failure).toHaveTextContent("is not a member of this Query Spec version");
    expect(failure).toHaveTextContent("Select it in Explore first.");
    expect(opened).toHaveLength(0);
  });

  it("stays disabled until the Result has resolved", async () => {
    vi.mocked(fetchResultLens).mockReturnValue(new Promise(() => {}) as never);
    render(
      <ResultWorkbench
        scope={SCOPE}
        resultId={RESULT_ID}
        lens="view"
        onNavigateLens={() => {}}
      />,
    );
    // Without an envelope there is no Query Spec version to pin, and a button
    // that fires anyway would create a Visualization against nothing.
    expect(screen.getByTestId("result-build-visualization")).toBeDisabled();
  });
});

/**
 * AI-296 — la fraicheur atteint enfin un ecran, et dit lequel des deux silences.
 *
 * CE QUI ETAIT FAUX. La seule fraicheur dessinee de la console etait
 * `stale-since <ISO>`, rendue uniquement quand la valeur etait presente. Deux
 * faits differents partageaient ce silence -- « on a regarde, c'est a jour » et
 * « personne n'a regarde » -- et un silence se lit comme le premier. En pratique
 * c'etait pire : le serveur lisait la valeur a `manifest.stale_since`, une cle
 * que rien dans le depot n'ecrit, donc la ligne ne s'est jamais affichee.
 *
 * Les deux tests ci-dessous sont les deux etats, et il en faut deux : un garde
 * qui ne verifie que l'etat evalue laisserait revenir exactement le defaut
 * corrige ici.
 */
describe("the Result workbench, freshness", () => {
  beforeEach(() => {
    vi.mocked(fetchResultLens).mockReset();
  });

  function provenanceWith(freshness: unknown) {
    return envelope("provenance", {
      chain: [],
      missing_link: null,
      predecessor: null,
      successors: [],
      source_system: null,
      value_source_tuples: [],
      value_source_unavailable: null,
      freshness,
    }) as never;
  }

  it("says nobody looked, instead of drawing nothing", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(
      provenanceWith({
        state: null,
        as_of: "2026-08-16T02:00:00+00:00",
        complete_through: null,
        stale_since_evaluated: false,
      }),
    );
    mount("provenance");

    expect(await screen.findByText(/Not checked/i)).toBeInTheDocument();
    // La date d'atterrissage peut etre montree, mais JAMAIS comme un verdict de
    // fraicheur -- c'est la confusion que ce panneau existe pour empecher.
    expect(screen.getByText(/not a statement about whether it is still current/i))
      .toBeInTheDocument();
  });

  it("says it was checked when the verdict was actually recorded", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(
      provenanceWith({
        state: null,
        as_of: "2026-08-16T02:00:00+00:00",
        complete_through: "2026-08-15",
        stale_since_evaluated: true,
      }),
    );
    mount("provenance");

    expect(await screen.findByText(/Checked/i)).toBeInTheDocument();
    expect(screen.queryByText(/Not checked/i)).toBeNull();
    expect(screen.getByText(/Complete through/i)).toBeInTheDocument();
  });
});

/**
 * AI-289 — la definition atteint la personne LA OU ELLE LIT LE NOMBRE.
 *
 * Elle existait deja, dans le lens `definitions` : un AUTRE onglet. Une personne
 * qui lit un nombre devait donc quitter les nombres pour savoir ce qu'ils
 * comptent, et une definition qu'il faut aller chercher est une definition que
 * personne ne lit.
 *
 * Trois etats, trois tests, et le troisieme est celui qu'on oublie : une version
 * epinglee qui ne se relit pas doit le DIRE. Montrer la definition courante a sa
 * place decrirait le nombre par un sens sous lequel il n'a jamais ete calcule --
 * la reecriture d'historique qu'AC8 interdit.
 */
describe("the Result workbench, column meaning", () => {
  beforeEach(() => {
    vi.mocked(fetchResultLens).mockReset();
  });

  function viewWith(fieldSemantics: unknown) {
    return envelope("view", {
      ...UNAVAILABLE_VIEW,
      outcome: "success",
      fields: ["total_cost_eur"],
      field_semantics: fieldSemantics,
      values: [
        {
          row_index: 0,
          cells: [
            { field: "total_cost_eur", value: 124, datum_key: "d1", kind: "measure" },
          ],
        },
      ],
    }, "success") as never;
  }

  it("shows the published label and keeps the physical column visible", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(
      viewWith({
        total_cost_eur: {
          label: "Media cost",
          definition: "Platform spend before agency fees.",
          unit: "EUR",
          aggregation: "sum",
          version_number: 3,
          resolved: true,
          owner_ref: null,
        },
      }),
    );
    mount("view");

    // Le nom demande par la personne est le titre...
    expect(await screen.findByText("Media cost")).toBeInTheDocument();
    // ...et la colonne physique reste lisible : un Result est une preuve, et la
    // colonne d'ou il vient en fait partie.
    expect(screen.getByText("(total_cost_eur)")).toBeInTheDocument();
  });

  it("says the definition is unavailable rather than showing another version", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(
      viewWith({
        total_cost_eur: {
          label: null, definition: null, unit: null, aggregation: null,
          version_number: null, resolved: false, owner_ref: null,
        },
      }),
    );
    mount("view");

    expect(await screen.findByText(/definition unavailable/i)).toBeInTheDocument();
  });

  it("leaves a column the Query Spec does not name completely alone", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(viewWith({}));
    mount("view");

    // Ni etiquette inventee, ni mention d'indisponibilite : cette colonne n'est
    // pas un membre, il n'y a rien a en dire.
    expect(await screen.findByText("total_cost_eur")).toBeInTheDocument();
    expect(screen.queryByText(/definition unavailable/i)).toBeNull();
  });
});

/**
 * Amendment 2026-08-15 — « Console and MCP print the same Result cell
 * differently » is an `Incomplete if` of the surface document, and until now
 * nothing could fail on it.
 *
 * THE DEFECT THIS PINS. `viz/pivotMatrix` — what the MCP App draws — divided the
 * micros; the Console's Result table printed `String(cell.value)`. One Result,
 * one column, one governed `value_type`, and two answers six orders of magnitude
 * apart. Closing it meant carrying `value_type` in the workbench lens envelope
 * AND rendering through the SAME formatter, not a second copy of the arithmetic.
 *
 * WHY THIS TEST RENDERS BOTH. Asserting each surface against a literal would let
 * the two drift apart the day someone edits one literal: the pin has to be the
 * EQUALITY of what the two readers print, computed from the same input, so it
 * breaks the moment a second formatter appears anywhere.
 */
describe("one Result cell, one amount — Console and MCP App", () => {
  beforeEach(() => {
    vi.mocked(fetchResultLens).mockReset();
  });

  /** 124 EUR in canonical micros. Stored exactly, divided exactly once, at read. */
  const MICROS = 124_000_000;
  const MEANING = { value_type: "money", unit: "EUR" };

  function consolePrints(): string {
    vi.mocked(fetchResultLens).mockResolvedValue(
      envelope("view", {
        ...UNAVAILABLE_VIEW,
        outcome: "success",
        fields: ["total_cost_eur"],
        field_semantics: {
          total_cost_eur: {
            label: "Media cost",
            definition: null,
            ...MEANING,
            aggregation: "sum",
            version_number: 1,
            resolved: true,
            owner_ref: null,
          },
        },
        values: [
          {
            row_index: 0,
            cells: [
              {
                field: "total_cost_eur",
                value: MICROS,
                datum_key: `${RESULT_ID}:0:total_cost_eur`,
                kind: "measure",
              },
            ],
          },
        ],
      }, "success") as never,
    );
    mount("view");
    return "";
  }

  function mcpAppPrints(): string {
    const { container } = render(
      <PivotMatrix
        matrix={{
          result_id: RESULT_ID,
          content_hash: "h".repeat(64),
          row_fields: ["k_country"],
          column_fields: [],
          value_fields: [{ name: "m_total_cost_eur", ...MEANING }],
          row_keys: [["FR"]],
          column_keys: [[]],
          cells: [
            {
              row_key: ["FR"],
              column_key: [],
              values: { m_total_cost_eur: { value: MICROS } },
              contributing_rows: 1,
            },
          ],
          bounds: { response_bytes: 0, rows_truncated: false, columns_truncated: false },
        }}
      />,
    );
    const printed = Array.from(container.querySelectorAll("tbody td")).map(
      (cell) => cell.textContent ?? "",
    );
    expect(printed).toHaveLength(1);
    return printed[0];
  }

  it("prints the same (value, value_type, unit) triple as the same string", async () => {
    consolePrints();
    const consoleCell = await screen.findByRole("button", {
      name: /Evidence for total_cost_eur, row 1/,
    });
    const consoleText = consoleCell.textContent ?? "";
    cleanup();

    const mcpText = mcpAppPrints();

    // The pin itself: the two readers agree, character for character.
    expect(consoleText).toBe(mcpText);
    // ...and they agree on the RIGHT thing. Two surfaces that both printed the
    // stored integer would also be equal, and would both be wrong.
    expect(consoleText).toContain("124.00");
    expect(consoleText).toContain("EUR");
    expect(consoleText).not.toContain(String(MICROS));
  });

  it("prints the plain number, identically, when the Result governs nothing", async () => {
    vi.mocked(fetchResultLens).mockResolvedValue(
      envelope("view", {
        ...UNAVAILABLE_VIEW,
        outcome: "success",
        fields: ["clicks"],
        field_semantics: {},
        values: [
          {
            row_index: 0,
            cells: [
              { field: "clicks", value: 7, datum_key: `${RESULT_ID}:0:clicks`, kind: "measure" },
            ],
          },
        ],
      }, "success") as never,
    );
    mount("view");
    const consoleText =
      (await screen.findByRole("button", { name: /Evidence for clicks, row 1/ })).textContent ?? "";
    cleanup();

    const { container } = render(
      <PivotMatrix
        matrix={{
          result_id: RESULT_ID,
          content_hash: "h".repeat(64),
          row_fields: ["k_country"],
          column_fields: [],
          value_fields: [{ name: "clicks" }],
          row_keys: [["FR"]],
          column_keys: [[]],
          cells: [
            {
              row_key: ["FR"],
              column_key: [],
              values: { clicks: { value: 7 } },
              contributing_rows: 1,
            },
          ],
          bounds: { response_bytes: 0, rows_truncated: false, columns_truncated: false },
        }}
      />,
    );
    const mcpText = container.querySelector("tbody td")?.textContent ?? "";

    // No currency appears out of nowhere on either side: silence is not a unit.
    expect(consoleText).toBe(mcpText);
    expect(consoleText).toBe("7");
  });
});
