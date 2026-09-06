/**
 * The Datastream mapping tab — where governed publication became reachable.
 *
 * The Workbench header has always said *"a proposed mapping is waiting"*
 * (`DatastreamWorkbenchRoute.tsx`), and the mapping tab offered nothing to do
 * about it. The reason was a missing link, not a missing button: the payload
 * named a mapping VERSION, and governed publication consumes a `ready`
 * PROPOSAL — a different object it cannot be derived from. `POST
 * /api/governance/publication-reviews` therefore had no caller anywhere, and
 * `publication_reviews_api.py#_prepare_publication_review_console` says what that
 * costs: *"nobody can ever call
 * `confirm_and_publish` and governed publication is unreachable."*
 *
 * Pinned here: the act appears only when the server names a proposal, and it is
 * NOT the publication offered on Outputs — that one advances
 * `current_published_execution_id` (the data candidate), this one advances
 * `current_mapping_version_id`. Two pointers, two acts.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import WorkbenchMappingPage from "../datastreams/workbench/pages/WorkbenchMappingPage";
import type { WorkbenchHeader, WorkbenchTabPayload } from "../datastreams/workbench/workbenchTypes";

const PAYLOAD = {
  evidence: {
    active_version: "mv_live",
    versions: [
      { id: "mv_live", mapping_payload: { fields: [], joint_grain: [], grain: [] }, blocking_count: 0 },
      { id: "mv_next", mapping_payload: { fields: [], joint_grain: [], grain: [] }, blocking_count: 0 },
    ],
    capabilities: [],
  },
} as unknown as WorkbenchTabPayload;

function header(readyProposal: string | null): WorkbenchHeader {
  return {
    identity: { mode: "connector_pull" },
    versions: {
      active_plan: "pv_1",
      active_mapping: "mv_live",
      proposed_plan: null,
      proposed_mapping: "mv_next",
      ready_mapping_proposal: readyProposal,
    },
  } as unknown as WorkbenchHeader;
}

const props = {
  payload: PAYLOAD,
  projectId: "proj_1",
  datastreamId: "ds_1",
  onConfirmed: () => undefined,
};

afterEach(() => vi.unstubAllGlobals());

describe("Mapping tab — governed publication", () => {
  it("offers no publication when the server names no ready proposal", () => {
    render(<WorkbenchMappingPage onRetry={() => {}} {...props} header={header(null)} />);
    // A proposed VERSION exists (`mv_next`) and is deliberately not enough: a
    // button here would open a review the server would refuse to mint.
    expect(screen.queryByTestId("publish-mapping")).not.toBeInTheDocument();
  });

  it("offers it, and mints the review from the proposal the server named", async () => {
    const calls: Array<{ url: string; init: RequestInit }> = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit = {}) => {
      calls.push({ url, init });
      return {
        ok: true, status: 201,
        json: async () => ({
          confirmation_id: "conf_1",
          scope: { datastream_id: "ds_1" },
          versions: { candidate_mapping_version_id: "mv_next", prior_mapping_version_id: "mv_live" },
          confirmation_secret: "opaque",
        }),
      };
    }));

    render(<WorkbenchMappingPage onRetry={() => {}} {...props} header={header("prop_ready")} />);
    await userEvent.click(screen.getByTestId("publish-mapping"));

    expect(await screen.findByTestId("publication-review")).toBeInTheDocument();
    const prepare = calls.find((c) => c.url.endsWith("/publication-reviews"));
    expect(prepare).toBeDefined();
    expect(JSON.parse(String(prepare!.init.body))).toEqual({ proposal_id: "prop_ready" });
  });

  it("still offers the raw contract change, and it is the ONLY textarea left", async () => {
    // Story 60.6 did not remove the general-purpose contract editor -- a plan or
    // grain change has no dedicated control and would otherwise become
    // unreachable. What it removed is the textarea being the only way to exclude
    // a column, which is proved on the other side in FileMappingColumns.
    render(<WorkbenchMappingPage onRetry={() => {}} {...props} header={header(null)} />);

    // Named for what it IS since the binding table grew its own controls: the
    // raw editor is the advanced escape hatch, not the ordinary way to change a
    // mapping. `Prepare mapping change` now belongs to the exclusion review,
    // where FileMappingColumns pins it.
    await userEvent.click(screen.getByRole("button", { name: "Edit raw contract…" }));

    expect(screen.getByLabelText("Proposed mapping contract")).toBeInTheDocument();
  });

  it("no longer tells the reader that Outputs moves the mapping pointer", async () => {
    render(<WorkbenchMappingPage onRetry={() => {}} {...props} header={header("prop_ready")} />);
    // The sentence belongs to a NON-LIVE version, so select one. BY ITS CELL:
    // since the 2026-08-18 amendment the ledger also names every other version
    // in the "compare with" list, so a bare text query matches the row AND the
    // option — two legitimate mentions of one version, not an ambiguity to fix
    // in the screen.
    await userEvent.click(screen.getByRole("cell", { name: "mv_next" }));
    // The old copy said a non-live version "becomes active only when its Ready
    // candidate is published from Outputs". That path advances the DATA pointer
    // and never this one, so the sentence sent people to the wrong screen.
    expect(screen.queryByText(/published from Outputs/i)).not.toBeInTheDocument();
    expect(screen.getByText(/only moves through a governed publication/i)).toBeInTheDocument();
  });
});
