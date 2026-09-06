/**
 * Story 71.3, the console half: a metric bound to the MDM is reported by the
 * dimensions bound beside it, and the panel offers to declare that grain.
 *
 * Three rules, not three examples: the panel DERIVES the candidate and declares
 * it VIA the MDM (a POST to the Datastream's own door), it declares nothing when
 * no measure is bound and SAYS why, and a server refusal — a column not bound to
 * the MDM — is shown as the sentence that names the repair, never swallowed.
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import MeasurementGrainCandidatesPanel from "../datastreams/workbench/mapping/MeasurementGrainCandidatesPanel";
import { apiPost } from "../lib/apiFetch";

vi.mock("../lib/apiFetch", () => ({
  ApiError: class ApiError extends Error {
    code: string;
    constructor(code: string, message: string) {
      super(message);
      this.code = code;
    }
  },
  apiPost: vi.fn(),
}));

const mockedPost = vi.mocked(apiPost);

const SPEND = "mdm_METRIC_SPEND00000000000000";
const DAY = "mdm_DIM_DAY0000000000000000000";
const CAMPAIGN = "mdm_DIM_CAMPAIGN00000000000000";

const CANDIDATE = {
  measurement_grain_candidates: {
    candidates: [
      {
        head: { field_id: "cost", canonical_field_id: SPEND, canonical_name: "spend" },
        members: [
          { field_id: "date", canonical_field_id: DAY, canonical_name: "day" },
          { field_id: "campaign_name", canonical_field_id: CAMPAIGN, canonical_name: "campaign" },
        ],
      },
    ],
    bound_metrics: 1,
    bound_dimensions: 2,
    bound_unresolved: 0,
  },
};

describe("MeasurementGrainCandidatesPanel", () => {
  it("derives a grain of head plus its bound dimensions", () => {
    render(
      <MeasurementGrainCandidatesPanel
        evidence={CANDIDATE}
        editable
        projectId="proj_EXAMPLE"
        datastreamId="ds_EXAMPLE"
        onConfirmed={() => {}}
      />,
    );

    expect(screen.getByText("spend")).toBeTruthy();
    expect(screen.getByText("day")).toBeTruthy();
    expect(screen.getByText("campaign")).toBeTruthy();
    expect(screen.getByText(/1 measure declares a grain/)).toBeTruthy();
  });

  it("declares the grain VIA the MDM, referencing canonical fields on both sides", async () => {
    mockedPost.mockResolvedValueOnce({ measurement_grain: { id: "mmd_EXAMPLE" } });
    const onConfirmed = vi.fn();
    render(
      <MeasurementGrainCandidatesPanel
        evidence={CANDIDATE}
        editable
        projectId="proj_EXAMPLE"
        datastreamId="ds_EXAMPLE"
        onConfirmed={onConfirmed}
      />,
    );

    fireEvent.click(screen.getByTestId(`declare-grain-${SPEND}`));
    fireEvent.click(screen.getByTestId(`confirm-grain-${SPEND}`));

    await waitFor(() => expect(mockedPost).toHaveBeenCalledTimes(1));
    const [path, body] = mockedPost.mock.calls[0];
    expect(path).toBe(
      "/api/projects/proj_EXAMPLE/datastreams/ds_EXAMPLE/workbench/mapping/measurement-grains",
    );
    expect(body).toMatchObject({ head: SPEND, members: [DAY, CAMPAIGN] });
    await waitFor(() => expect(onConfirmed).toHaveBeenCalled());
  });

  it("shows the server refusal that names the column to bind, rather than swallowing it", async () => {
    const { ApiError } = (await import("../lib/apiFetch")) as unknown as {
      ApiError: new (code: string, message: string) => Error;
    };
    mockedPost.mockRejectedValueOnce(
      new ApiError(
        "grain_head_not_bound_to_mdm",
        "The measure a grain reports must be a column this Datastream has bound to a canonical field.",
      ),
    );
    render(
      <MeasurementGrainCandidatesPanel
        evidence={CANDIDATE}
        editable
        projectId="proj_EXAMPLE"
        datastreamId="ds_EXAMPLE"
        onConfirmed={() => {}}
      />,
    );

    fireEvent.click(screen.getByTestId(`declare-grain-${SPEND}`));
    fireEvent.click(screen.getByTestId(`confirm-grain-${SPEND}`));

    await waitFor(() =>
      expect(screen.getByText(/must be a column this Datastream has bound/)).toBeTruthy(),
    );
  });

  it("declares nothing when no measure is bound, and SAYS why", () => {
    render(
      <MeasurementGrainCandidatesPanel
        evidence={{
          measurement_grain_candidates: {
            candidates: [],
            bound_metrics: 0,
            bound_dimensions: 2,
            bound_unresolved: 0,
          },
        }}
        editable
        projectId="proj_EXAMPLE"
        datastreamId="ds_EXAMPLE"
        onConfirmed={() => {}}
      />,
    );

    expect(screen.getByText(/No measure is bound to declare a grain for/)).toBeTruthy();
    expect(screen.getByText(/Bind a metric column/)).toBeTruthy();
  });

  it("names a binding whose canonical field no longer resolves, rather than dropping it", () => {
    render(
      <MeasurementGrainCandidatesPanel
        evidence={{
          measurement_grain_candidates: {
            candidates: [],
            bound_metrics: 0,
            bound_dimensions: 1,
            bound_unresolved: 2,
          },
        }}
        editable
        projectId="proj_EXAMPLE"
        datastreamId="ds_EXAMPLE"
        onConfirmed={() => {}}
      />,
    );

    expect(screen.getByTestId("grain-unresolved")).toBeTruthy();
    expect(screen.getByText(/2 columns are bound to a canonical field that is archived/)).toBeTruthy();
  });

  it("a metric bound with no dimension beside it is still a legal grain — a total", () => {
    render(
      <MeasurementGrainCandidatesPanel
        evidence={{
          measurement_grain_candidates: {
            candidates: [
              {
                head: { field_id: "cost", canonical_field_id: SPEND, canonical_name: "spend" },
                members: [],
              },
            ],
            bound_metrics: 1,
            bound_dimensions: 0,
            bound_unresolved: 0,
          },
        }}
        editable
        projectId="proj_EXAMPLE"
        datastreamId="ds_EXAMPLE"
        onConfirmed={() => {}}
      />,
    );

    expect(screen.getByText(/no dimension — a total/)).toBeTruthy();
    expect(screen.getByTestId(`declare-grain-${SPEND}`)).toBeTruthy();
  });
});
