/**
 * Two unknowns are not a match.
 *
 * The candidate-readiness table compares a candidate Output version with the
 * current one, row by row, and its Schema row says "two identical hashes mean
 * the shape did not move". When a hash is missing both sides read the literal
 * "Unavailable", compared equal, and the table announced a green **Unchanged** —
 * an assertion about a comparison it had never made, on the screen that decides
 * a promotion.
 *
 * Same class as the publication banner that rendered a genuine 404 in green: a
 * screen stating something it does not know.
 *
 * ## Why this file RENDERS since 2026-08-18
 *
 * It used to `readFileSync` the page and search its source for the literal
 * `? <Status tone="warning">Not comparable</Status>`, then compare that string's
 * offset with `Unchanged`'s to check the ternary's branch order. That is a text
 * search standing in for a behaviour, and it has both failure modes of one: it
 * went red on a pure reformat that changed no behaviour, and it would have
 * stayed green on a page that renders the branch and never mounts the table.
 * The property is about what a person sees, so it is asserted on what a person
 * sees.
 */
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import WorkbenchOutputsPage from "../datastreams/workbench/pages/WorkbenchOutputsPage";
import type { WorkbenchHeader, WorkbenchTabPayload } from "../datastreams/workbench/workbenchTypes";

const HEADER = {
  identity: { name: "Orders", mode: "managed_feed", module: null, connector: null,
    source_account_ref: null, delivery_channels: [] },
  axes: { lifecycle: "Active", configuration: "Ready", operations: "Healthy", publication: "Candidate" },
  versions: { active_plan: "dsp_1", active_mapping: "dmap_1", proposed_plan: null, proposed_mapping: null },
  operations_evidence: { next_run_at: null, missed_run_count: 0, schedule_state_known: true, late_reasons: [] },
  runs: { latest: "dse_cand", latest_state: "collected" },
  publications: { candidate: "dse_cand", current: "dse_cur", last_known_good: null },
  links: { source: null, project_settings: null, governance: null },
  primary_action: null,
} as unknown as WorkbenchHeader;

/** One Output version per publication role, with the hashes under test. */
function renderComparison(
  candidateHash: string | null,
  currentHash: string | null,
  publications: Partial<WorkbenchHeader["publications"]> = {},
) {
  const version = (executionId: string, schemaHash: string | null) => ({
    id: "dso_1", output_kind: "full_grain", stable_name: "orders_daily",
    version_id: `dsov_${executionId}`, execution_id: executionId,
    plan_version_id: "dsp_1", mapping_version_id: "dmap_1",
    grain_evidence: ["date"], schema_hash: schemaHash,
    delivery_ref: null, created_at: "2026-08-01T00:00:00Z",
  });
  render(
    <WorkbenchOutputsPage
      header={{ ...HEADER, publications: { ...HEADER.publications, ...publications } }}
      payload={{
        schema: "datastream_workbench.outputs.v1",
        tab: "outputs",
        project_id: "proj_EXAMPLE",
        datastream_id: "ds_EXAMPLE",
        evidence: {
          raw_zone_policy: "managed_raw",
          retention_days: null,
          used_by: [],
          outputs: [version("dse_cur", currentHash), version("dse_cand", candidateHash)],
        },
      } as unknown as WorkbenchTabPayload}
      projectId="proj_EXAMPLE"
      datastreamId="ds_EXAMPLE"
      onConfirmed={() => undefined}
    />,
  );
  // Row 0 is the header; row 1 is `Schema`, the property this file is about.
  return within(screen.getByRole("region", { name: "Candidate readiness" })).getAllByRole("row")[1];
}

describe("Candidate readiness · a comparison is never claimed, only made", () => {
  it("has a third state for a comparison it cannot make", () => {
    const schema = renderComparison(null, "c".repeat(64));
    expect(within(schema).getByText("Not comparable")).toBeInTheDocument();
  });

  it("does not let an unavailable value fall through to Unchanged", () => {
    // THE DEFECT, EXACTLY. `unknown` must be decided BEFORE equality, or two
    // "Unavailable" strings reach the equality test and read as agreement.
    const schema = renderComparison(null, null);
    expect(within(schema).queryByText("Unchanged")).not.toBeInTheDocument();
    expect(within(schema).getByText("Not comparable")).toBeInTheDocument();
  });

  it("still says Unchanged when it genuinely compared two hashes", () => {
    // The third state must not swallow the answer it was added beside: a table
    // that can only ever say "Not comparable" is no more use than one that
    // always says "Unchanged".
    const schema = renderComparison("c".repeat(64), "c".repeat(64));
    expect(within(schema).getByText("Unchanged")).toBeInTheDocument();
  });

  it("still distinguishes a first publication from a failed comparison", () => {
    // Nothing served yet is not the same as "we cannot tell": one is a fact
    // about the Datastream, the other about our own evidence.
    const schema = renderComparison("c".repeat(64), null, { current: null });
    expect(within(schema).getByText("First publication")).toBeInTheDocument();
    expect(within(schema).queryByText("Not comparable")).not.toBeInTheDocument();
  });
});
