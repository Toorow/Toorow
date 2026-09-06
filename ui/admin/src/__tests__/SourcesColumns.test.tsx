/**
 * Sources answers four things or it does not answer its function.
 *
 * `SCREEN-FUNCTION-MATRIX.md` Lot 2: "inspect organization-usable provider
 * accounts, **ownership, sharing** and **authorization health**". The page
 * rendered availability, freshness and a usage word — one of the four. Ownership
 * was computed in the query (`data_surface.py`) and projected, and no column
 * read it; the health state machine was one join away and the join was never
 * taken. Four review lenses found both, independently, with file:line.
 *
 * Nothing tested this page's columns, which is how two of its four promises
 * could go missing without a single test turning red.
 */
import { render, screen } from "@testing-library/react";
import Sources from "../shell/pages/Sources";

vi.mock("../data/dataSurface", () => ({
  usePageCursor: () => ({
    cursor: "",
    canGoBack: false,
    goToNextPage: () => undefined,
    goToPreviousPage: () => undefined,
    goToFirstPage: () => undefined,
  }),
  useDataSurface: () => ({
    state: {
      status: "ready",
      envelope: {
        items: [{
          object_ref: { object_type: "source-account", id: "sacct_EXAMPLE" },
          connector_ref: { object_type: "connector", id: "google-analytics" },
          label: "North America analytics",
          authorization_ref: { object_type: "source-authorization", owner_scope: "delegated", kind: "oauth2" },
          states: { availability: "available", authorization: "revoked", freshness: "observed", usage: "used" },
          evidence: { used_by_count: 2 },
          evidence_as_of: "2026-07-29T09:45:00Z",
          links: {},
        }],
        unavailable_reasons: [],
        allowed_actions: [],
        evidence_as_of: "2026-07-29T09:45:00Z",
      },
    },
    reload: () => undefined,
  }),
}));

it("says who OWNS an account, because a shared one cannot be repaired here", () => {
  render(<Sources projectId="proj_EXAMPLE" />);
  expect(screen.getByText("Shared with this Project")).toBeInTheDocument();
});

it("says the exact authorization health, not a boolean", () => {
  render(<Sources projectId="proj_EXAMPLE" />);
  // `revoked` and `stale` are different problems with different repairs. The
  // page used to collapse both into "available". Read as a person says it: the
  // cell prints the label, never the stored token.
  expect(screen.getByText("Revoked")).toBeInTheDocument();
  expect(screen.queryByText("revoked")).not.toBeInTheDocument();
});

it("says WHAT depends on the account, not merely that something does", () => {
  render(<Sources projectId="proj_EXAMPLE" />);
  // "used" cannot answer "is anything depending on this before I touch it?".
  expect(screen.getByText("2 Datastreams")).toBeInTheDocument();
  expect(screen.queryByText("used")).not.toBeInTheDocument();
  expect(screen.queryByText("In use")).not.toBeInTheDocument();
});

it("stops describing why it does not draw ownership", () => {
  render(<Sources projectId="proj_EXAMPLE" />);
  // The old description argued against the page's own function.
  expect(screen.queryByText(/stay in their governed account surfaces/)).not.toBeInTheDocument();
});
