/**
 * Story 50.1 AC11 — the narrow Explore door.
 *
 * The property these tests exist to protect is the one a screen is most likely
 * to get wrong: `empty` and `unavailable` are DIFFERENT answers. Rendering "no
 * data" for both would tell someone their marketing had no clicks when in fact
 * the pipeline never ran.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import QueryDoor from "../analyze/QueryDoor";
import { ApiError } from "../lib/apiFetch";

vi.mock("../analyze/queryClient", () => ({
  createQuerySpec: vi.fn(),
  executeQuerySpecVersion: vi.fn(),
  fetchResultEvidence: vi.fn(),
}));

// Story 50.2 widened the option read: the door now composes its controls from
// `query-facets`, the superset of `query-options`. Same server, same compiled
// artifact, one more read - so the mock moved and the properties did not.
vi.mock("../analyze/workbenchClient", async () => {
  const actual = await vi.importActual<typeof import("../analyze/workbenchClient")>(
    "../analyze/workbenchClient",
  );
  return { ...actual, fetchQueryFacets: vi.fn() };
});

import {
  createQuerySpec,
  executeQuerySpecVersion,
  fetchResultEvidence,
} from "../analyze/queryClient";
import { fetchQueryFacets } from "../analyze/workbenchClient";

const OPTIONS = {
  schema_version: "analyze-query-facets.v1",
  project_id: "proj_EXAMPLE",
  semantic_view_id: "sv_1",
  semantic_view_version_id: "svv_1",
  semantic_view_label: "Search performance",
  semantic_view_version_number: 1,
  executable: true,
  status: "published",
  measures: [
    { concept_id: "sc_clicks", version_id: "scv_clicks", label: "Clicks", owner_ref: null },
  ],
  dimensions: [
    { concept_id: "sc_date", version_id: "scv_date", label: "Date", owner_ref: null },
    { concept_id: "sc_page", version_id: "scv_page", label: "Page", owner_ref: null },
  ],
  pairs: [{ measure_id: "sc_clicks", dimension_id: "sc_date", queryable: true }],
  time: {
    members: [
      { concept_id: "sc_date", version_id: "scv_date", label: "Date", allowed_grains: ["day"] },
    ],
    grains: ["day"],
    grains_unavailable_reason: null,
    comparisons: ["none", "previous_period", "previous_year"],
    // The exact sentences and the exact order `query_specs.COMPARISON_PRECONDITIONS`
    // declares, because a fixture that softened them would let the screen prove
    // itself against words the door does not use.
    comparison_preconditions: [
      {
        condition: "time_member_required",
        message:
          "A period comparison needs the time member and the From and To dates of the window to compare. Choose them, or set comparison to none.",
      },
      {
        condition: "time_member_not_selected",
        message:
          "A period comparison labels each row with the period it belongs to, so the time member must be one of the dimensions of the request. Add it, or set comparison to none.",
      },
      {
        condition: "window_required",
        message:
          "A period comparison needs both the From and the To date of the window to compare. Set them, or set comparison to none.",
      },
    ],
    as_of_supported: true,
    reporting_boundary_supported: true,
  },
  sort: { directions: ["asc", "desc"] },
  filter_operators: ["eq", "in"],
  limits: {
    default_row_limit: 10000,
    max_row_limit: 100000,
    max_measures: 50,
    max_dimensions: 20,
    max_filters: 100,
  },
  classification_facets: [
    {
      facet: "market",
      dimension_id: "sc_market",
      dimension_version_id: "scv_market",
      members: [
        {
          classification_object_id: "mdc_EXAMPLE",
          slug: "france",
          label: "France",
          hierarchy_id: "mdd_EXAMPLE",
          hierarchy_version_id: "mdc_EXAMPLE@3",
        },
      ],
      reserved_members: ["Other", "Unknown"],
    },
  ],
  classification_facets_unavailable: [
    {
      facet: "product",
      dimension_id: "sc_product",
      reason: "no active product classification is approved in this organization",
    },
  ],
  unavailable_reasons: [],
  owner_ref: null,
};

function mount() {
  return render(
    <QueryDoor projectId="proj_EXAMPLE" semanticViewId="sv_1" semanticViewVersionId="svv_1" />,
  );
}

describe("the narrow Explore door", () => {
  beforeEach(() => {
    vi.mocked(fetchQueryFacets).mockResolvedValue(structuredClone(OPTIONS) as never);
    vi.mocked(createQuerySpec).mockResolvedValue({
      query_spec_id: "qs_1",
      id: "qsv_1",
      version_number: 1,
      content_hash: "a".repeat(64),
      semantic_view_version_id: "svv_1",
    });
    vi.mocked(fetchResultEvidence).mockResolvedValue({
      result_id: "qr_1",
      content_hash: "b".repeat(64),
      outcome: "empty",
      row_count: 0,
      truncated: false,
      schema: { fields: [] },
      manifest: {},
      rows: [],
    });
  });

  it("offers only the members the server returned", async () => {
    mount();
    expect(await screen.findByRole("checkbox", { name: /Clicks/ })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Date/ })).toBeInTheDocument();
    // Nothing invented: the door shows what query-facets carried, no more.
    expect(screen.queryByRole("checkbox", { name: /Impressions/ })).not.toBeInTheDocument();
  });

  it("disables a dimension the compiler did not prove, and says why", async () => {
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("checkbox", { name: /Clicks/ }));
    await waitFor(() =>
      expect(screen.getByText(/not proved compatible/)).toBeInTheDocument(),
    );
    // `Page` is refused BEFORE the round trip, from the server's own pair list.
    expect(screen.getByRole("checkbox", { name: /Page/ })).toBeDisabled();
  });

  it("distinguishes unavailable from empty, and names the missing link", async () => {
    vi.mocked(executeQuerySpecVersion).mockResolvedValue({
      attempt_id: "qea_1",
      result_id: "qr_1",
      outcome: "unavailable",
      row_count: 0,
      truncated: false,
      content_hash: "b".repeat(64),
      ai_path: "No AI path",
    });
    vi.mocked(fetchResultEvidence).mockResolvedValue({
      result_id: "qr_1",
      content_hash: "b".repeat(64),
      outcome: "unavailable",
      row_count: 0,
      truncated: false,
      schema: { fields: [] },
      manifest: {
        unavailable_reason: "this Datastream has no published output to query yet",
        missing_link: "datastream_output_versions",
      },
      rows: [],
    });
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("checkbox", { name: /Clicks/ }));
    await user.click(screen.getByTestId("explore-run"));

    const receipt = await screen.findByTestId("explore-receipt");
    expect(receipt).toHaveTextContent("could not be asked");
    expect(receipt).toHaveTextContent("datastream_output_versions");
    expect(receipt).toHaveTextContent("NOT an empty answer");
  });

  it("says an empty Result means the path is healthy", async () => {
    vi.mocked(executeQuerySpecVersion).mockResolvedValue({
      attempt_id: "qea_1",
      result_id: "qr_2",
      outcome: "empty",
      row_count: 0,
      truncated: false,
      content_hash: "c".repeat(64),
      ai_path: "No AI path",
    });
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("checkbox", { name: /Clicks/ }));
    await user.click(screen.getByTestId("explore-run"));
    const receipt = await screen.findByTestId("explore-receipt");
    expect(receipt).toHaveTextContent("nothing matched");
    expect(receipt).not.toHaveTextContent("could not be asked");
  });

  it("shows the server's structured refusal instead of rewriting it", async () => {
    vi.mocked(createQuerySpec).mockRejectedValue(
      new ApiError(422, "invalid_query_spec", "refused on 1 point(s)", {
        refusals: [
          { code: "unknown_member", message: "not a queryable measure", subject: "sc_ghost" },
        ],
      }),
    );
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("checkbox", { name: /Clicks/ }));
    await user.click(screen.getByTestId("explore-run"));
    expect(await screen.findByText(/refused on 1 point/)).toBeInTheDocument();
    // The exact subject reaches the user; without it they would have to guess.
    expect(screen.getByText("sc_ghost")).toBeInTheDocument();
  });

  it("refuses to run a version that is not executable, without substituting another", async () => {
    vi.mocked(fetchQueryFacets).mockResolvedValue({
      ...structuredClone(OPTIONS),
      executable: false,
      status: "draft",
    } as never);
    mount();
    expect(await screen.findByText(/cannot run a query/)).toBeInTheDocument();
    expect(screen.queryByTestId("explore-run")).not.toBeInTheDocument();
  });

  it("always reports an AI Path, never a blank", async () => {
    vi.mocked(executeQuerySpecVersion).mockResolvedValue({
      attempt_id: "qea_1",
      result_id: "qr_3",
      outcome: "success",
      row_count: 2,
      truncated: false,
      content_hash: "d".repeat(64),
      ai_path: "No AI path",
    });
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("checkbox", { name: /Clicks/ }));
    await user.click(screen.getByTestId("explore-run"));
    const receipt = await screen.findByTestId("explore-receipt");
    expect(receipt).toHaveTextContent("No AI path");
  });
  // -------------------------------------------------------------------------
  // Story 50.2 - the governed controls, and the way out to the Result.
  // -------------------------------------------------------------------------

  it("offers only the grains the published version declares", async () => {
    mount();
    const grain = (await screen.findByLabelText("Grain")) as HTMLSelectElement;
    expect(within(grain).getByRole("option", { name: "day" })).toBeInTheDocument();
    expect(within(grain).queryByRole("option", { name: "month" })).toBeNull();
  });

  it("disables the grain control and says why when the version declares none", async () => {
    const options = structuredClone(OPTIONS);
    vi.mocked(fetchQueryFacets).mockResolvedValue({
      ...options,
      time: {
        ...options.time,
        members: [],
        grains: [],
        grains_unavailable_reason:
          "no time member of this published version declares an allowed grain",
      },
    } as never);
    mount();
    expect(await screen.findByLabelText("Grain")).toBeDisabled();
    // A plausible day/week/month list here would offer a grain nobody proved.
    expect(screen.getByText(/declares an allowed grain/)).toBeInTheDocument();
  });

  it("keeps Other and Unknown selectable on a governed facet", async () => {
    mount();
    const facet = await screen.findByLabelText("Classification facet");
    await userEvent.setup().selectOptions(facet, "sc_market");
    const value = screen.getByLabelText("Facet value") as HTMLSelectElement;
    expect(within(value).getByRole("option", { name: "France" })).toBeInTheDocument();
    expect(within(value).getByRole("option", { name: "Other" })).toBeInTheDocument();
    expect(within(value).getByRole("option", { name: "Unknown" })).toBeInTheDocument();
  });

  it("names the facets that exist but cannot be chosen, rather than hiding them", async () => {
    mount();
    const banner = await screen.findByTestId("explore-facets-unavailable");
    expect(banner).toHaveTextContent("product");
    expect(banner).toHaveTextContent("no active product classification is approved");
  });

  it("sends the governed controls as one canonical request and lets the server judge it", async () => {
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("checkbox", { name: /Clicks/ }));
    await user.click(screen.getByRole("checkbox", { name: /Date/ }));
    await user.selectOptions(screen.getByLabelText("Time member"), "sc_date");
    await user.selectOptions(screen.getByLabelText("Grain"), "day");
    // Story 67.9 : la fenetre AVANT la comparaison. Le serveur refuse une
    // comparaison sans From/To (`comparison_window_required`), et l'ecran ne
    // pose plus la question tant qu'il ne peut pas y repondre -- ce test la
    // posait quand meme et comptait sur un choix que le submit aurait refuse.
    await user.type(screen.getByLabelText("From"), "2026-08-01");
    await user.type(screen.getByLabelText("To"), "2026-08-31");
    await user.selectOptions(screen.getByLabelText("Source comparison"), "previous_period");
    await user.selectOptions(screen.getByLabelText("Sort by"), "sc_clicks");
    await user.type(screen.getByLabelText("Row limit"), "250");
    await user.selectOptions(screen.getByLabelText("Classification facet"), "sc_market");
    await user.selectOptions(screen.getByLabelText("Facet value"), "mdc_EXAMPLE");
    await user.click(screen.getByTestId("explore-run"));

    await waitFor(() => expect(createQuerySpec).toHaveBeenCalled());
    // The LAST call: mocks are shared across the file, so calls[0] belongs to an
    // earlier test and would silently assert the wrong request.
    const [, body] = vi.mocked(createQuerySpec).mock.calls.at(-1)!;
    expect(body.spec).toMatchObject({
      measures: ["sc_clicks"],
      dimensions: ["sc_date"],
      grain: "day",
      comparison: "previous_period",
      row_limit: 250,
      sort: [{ member_id: "sc_clicks", direction: "desc" }],
      time: { member_id: "sc_date" },
    });
    // The classification pins travel WITH the filter: a value only means
    // something under the hierarchy version that defined it.
    expect((body.spec as { filters: Record<string, unknown>[] }).filters[0]).toMatchObject({
      member_id: "sc_market",
      operator: "eq",
      value: "france",
      classification_object_id: "mdc_EXAMPLE",
      hierarchy_version_id: "mdc_EXAMPLE@3",
    });
  });

  it("hands a terminal receipt to the Result workbench, including `unavailable`", async () => {
    vi.mocked(executeQuerySpecVersion).mockResolvedValue({
      attempt_id: "qea_1",
      result_id: "qr_9",
      outcome: "unavailable",
      row_count: 0,
      truncated: false,
      content_hash: "e".repeat(64),
      ai_path: "No AI path",
    });
    const onResult = vi.fn();
    const user = userEvent.setup();
    render(
      <QueryDoor
        projectId="proj_EXAMPLE"
        semanticViewId="sv_1"
        semanticViewVersionId="svv_1"
        onResult={onResult}
      />,
    );
    await user.click(await screen.findByRole("checkbox", { name: /Clicks/ }));
    await user.click(screen.getByTestId("explore-run"));
    // `unavailable` is an answer with inspectable evidence. Withholding the
    // navigation for it would hide the one Result whose lenses matter most.
    await waitFor(() => expect(onResult).toHaveBeenCalled());
    expect(vi.mocked(onResult).mock.calls[0][0].result_id).toBe("qr_9");
    expect(vi.mocked(onResult).mock.calls[0][1]).toMatchObject({
      query_spec_id: "qs_1",
      id: "qsv_1",
    });
  });

  // -------------------------------------------------------------------------
  // Story 67.9 — une question qui ne reduit rien ne se pose pas
  // -------------------------------------------------------------------------

  it("does not ask for a comparison until it could be answered, and says what is missing", async () => {
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("checkbox", { name: /Clicks/ }));

    const comparison = screen.getByLabelText("Source comparison");
    // `query_specs._compile_comparison` refuse sur trois conditions. L'ecran les
    // ignorait toutes les trois : il offrait le choix, le serveur refusait au
    // submit, et la personne apprenait son erreur apres avoir tout rempli.
    expect(comparison).toBeDisabled();
    expect(screen.getByText(/needs the time member and the From and To dates/)).toBeInTheDocument();

    // Un membre temps choisi mais PAS retenu comme dimension : la deuxieme
    // condition, et sa phrase est differente parce que le geste l est aussi.
    await user.selectOptions(screen.getByLabelText("Time member"), "sc_date");
    expect(comparison).toBeDisabled();
    expect(screen.getByText(/must be one of the dimensions of the request/)).toBeInTheDocument();

    // La dimension retenue : reste la fenetre.
    await user.click(screen.getByRole("checkbox", { name: /Date/ }));
    expect(comparison).toBeDisabled();
    expect(screen.getByText(/needs both the From and the To date/)).toBeInTheDocument();

    // Les trois tenues : la question se pose enfin.
    await user.type(screen.getByLabelText("From"), "2026-08-01");
    await user.type(screen.getByLabelText("To"), "2026-08-31");
    expect(comparison).toBeEnabled();
  });

  it("says what is missing in the SERVER's words, not in words of its own", async () => {
    // The rest of 67.9. The three sentences above used to be typed into
    // `QueryDoor.tsx` with their tails adapted, under a comment claiming they
    // were the server's. This proves they are read: the payload says something
    // else, and the screen says that instead of what it used to hold.
    vi.mocked(fetchQueryFacets).mockResolvedValue({
      ...structuredClone(OPTIONS),
      time: {
        ...structuredClone(OPTIONS).time,
        comparison_preconditions: [
          { condition: "time_member_required", message: "The validator changed its words." },
        ],
      },
    } as never);
    mount();
    await user_click_clicks();

    expect(screen.getByLabelText("Source comparison")).toBeDisabled();
    expect(await screen.findByText("The validator changed its words.")).toBeInTheDocument();
    expect(
      screen.queryByText(/needs the time member and the From and To dates/),
    ).not.toBeInTheDocument();
  });

  it("still refuses the question when the payload carries no sentence for it", async () => {
    // A bundle can outlive the server that answers it. The screen loses its
    // explanation, never its refusal: the gate reads the CONDITION, and a
    // missing sentence that re-enabled the control would be the fail-open this
    // whole repair exists to close.
    const withoutSentences = structuredClone(OPTIONS);
    delete (withoutSentences.time as { comparison_preconditions?: unknown })
      .comparison_preconditions;
    vi.mocked(fetchQueryFacets).mockResolvedValue(withoutSentences as never);
    mount();
    await user_click_clicks();

    expect(screen.getByLabelText("Source comparison")).toBeDisabled();
    // And it says NOTHING rather than falling back on a sentence of its own —
    // a fallback is a copy, and a copy is the defect.
    expect(screen.queryByText(/A period comparison/)).not.toBeInTheDocument();
  });
});

/** Select the one measure, which is what unlocks the controls panel. */
async function user_click_clicks() {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("checkbox", { name: /Clicks/ }));
}
