/**
 * The second facet of a Competitor, and the same defect one tab later.
 *
 * `tracked-entity/coverage` is contracted by the route
 * (`shell/navigation/governance.ts`) and by the read model
 * (`governance_read_model.py:126`); its owner ships the whole entity x
 * Datastream matrix (`:3309-3324`); and no branch of the workbench claimed it,
 * so it fell to the chassis' `Object.entries(summary)` dump. A person read
 * `matrix` as a raw record where the tab's whole subject belongs — exactly what
 * `RepresentationsTab.test.tsx` was written against for the tab before it.
 *
 * Ratified in `governance.md`, *Amendment, 2026-09-01 — the Competitor
 * workbench*. The two cases worth their own test are the ones a generic
 * renderer gets wrong: a state must never collapse into a tick, and an empty
 * answer must say which gesture fills it.
 */
import { render, screen, within } from "@testing-library/react";
import { CoverageTab } from "../governance/TrackedEntityTabs";
import type { GovernanceObject } from "../governance/governanceSurface";

function object(summary: Record<string, unknown>): GovernanceObject {
  return { summary } as unknown as GovernanceObject;
}

const MATRIX = [
  {
    datastream_ref: { object_type: "datastream", id: "ds_published" },
    state: "published",
    direction: "inbound",
    report_id: "rep_1",
    exception_reason_code: null,
    exception_reason: null,
  },
  {
    datastream_ref: { object_type: "datastream", id: "ds_candidate" },
    state: "candidate",
    direction: "inbound",
    report_id: null,
    exception_reason_code: null,
    exception_reason: null,
  },
  {
    datastream_ref: { object_type: "datastream", id: "ds_excluded" },
    state: "excluded",
    direction: "inbound",
    report_id: null,
    exception_reason_code: "no_placement_dimension",
    exception_reason: "This connector declares no placement dimension.",
  },
];

it("names each bound Datastream and its state, verbatim", () => {
  render(<CoverageTab detail={object({ matrix: MATRIX, published_count: 1, bound_count: 3 })} />);
  const published = screen.getByText("ds_published").closest("tr")!;
  expect(within(published).getByText("Published")).toBeInTheDocument();
  const candidate = screen.getByText("ds_candidate").closest("tr")!;
  expect(within(candidate).getByText("Candidate")).toBeInTheDocument();
});

it("never collapses a candidate binding into the published count", () => {
  // Three bindings, one of them collecting. A screen that derived "is this
  // collected?" from the length of the list would say three.
  render(<CoverageTab detail={object({ matrix: MATRIX, published_count: 1, bound_count: 3 })} />);
  expect(
    within(screen.getByTestId("coverage-published-count")).getByText("1"),
  ).toBeInTheDocument();
  expect(within(screen.getByTestId("coverage-bound-count")).getByText("3")).toBeInTheDocument();
});

it("states the exception its owner recorded, in its own words", () => {
  render(<CoverageTab detail={object({ matrix: MATRIX, published_count: 1, bound_count: 3 })} />);
  const excluded = screen.getByText("ds_excluded").closest("tr")!;
  expect(
    within(excluded).getByText("This connector declares no placement dimension."),
  ).toBeInTheDocument();
});

it("says nothing is bound, and names the gesture that binds one", () => {
  render(<CoverageTab detail={object({ matrix: [], published_count: 0, bound_count: 0 })} />);
  expect(screen.getByText(/no datastream is bound to this entity/i)).toBeInTheDocument();
  expect(screen.getByText(/competitor registry/i)).toBeInTheDocument();
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
});

it("treats a missing payload as none bound, never as a broken screen", () => {
  render(<CoverageTab detail={object({})} />);
  expect(screen.getByText(/no datastream is bound to this entity/i)).toBeInTheDocument();
});
