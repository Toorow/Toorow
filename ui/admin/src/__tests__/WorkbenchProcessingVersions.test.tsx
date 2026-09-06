/**
 * The immutable plan-version ledger, and the retention it must not overstate.
 *
 * ## What this file holds
 *
 * The ledger listed four immutable versions of what a Datastream does to a
 * person's data and offered no way to tell them apart: the column was the ULID,
 * `version_number` and `created_by` were on the wire and on no screen, there was
 * no comparison of any kind, and the single door it carried always opened on the
 * same payload — so an operator could read a superseded version and had no way to
 * act on what they had just read.
 *
 * Beside it, `Retention Policy` printed `Indefinite` on every Datastream in the
 * product. Measured 2026-08-18: `$defs/destination` in
 * `server/core/schemas/datastream-intent.schema.json` is `additionalProperties:
 * false` with `policy` as its ONLY property, so a Datastream intent cannot carry
 * a retention at all; nothing in `server/` writes the key and nothing reads it to
 * purge. `Indefinite` therefore read as a decision where there is no owner.
 *
 * These tests hold the four things a later edit can quietly cost:
 *
 *   * a version identified by its ULID again, or an author that vanishes;
 *   * a comparison that dumps every path instead of the ones that moved;
 *   * "propose from this version" losing the sentence that says it REVERTS;
 *   * a retention number that reads as operative.
 */
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import WorkbenchProcessingPage from "../datastreams/workbench/pages/WorkbenchProcessingPage";
import type { WorkbenchTabPayload } from "../datastreams/workbench/workbenchTypes";

const V1 = {
  source: { kind: "connector_pull", module: "google-ads", selection: { dimensions: ["date"] } },
  destination: { policy: "managed_raw" },
  schedule: { mode: "daily", timezone: "UTC" },
};

/** V2 moves exactly two paths: the cadence, and the collected dimensions. */
const V2 = {
  source: { kind: "connector_pull", module: "google-ads", selection: { dimensions: ["date", "campaign"] } },
  destination: { policy: "managed_raw" },
  schedule: { mode: "weekly", timezone: "UTC" },
};

const NO_CATALOGUE = {
  state: "no_module",
  mode: "managed_feed",
  reason: "A file source pulls no selection: the columns are the ones the file brings.",
};

const PLANS = [
  {
    id: "dsp_SECOND",
    version_number: 2,
    normalized_payload: V2,
    content_hash: "b".repeat(64),
    executable: true,
    validation_issues: [],
    created_by: "owner@example.com",
    created_at: "2026-08-10T09:30:00Z",
  },
  {
    id: "dsp_FIRST",
    version_number: 1,
    normalized_payload: V1,
    content_hash: "a".repeat(64),
    executable: true,
    validation_issues: [],
    created_by: "analyst@example.com",
    created_at: "2026-08-01T08:00:00Z",
  },
];

function renderPage(over: Record<string, unknown> = {}) {
  const payload = {
    schema: "datastream_workbench.processing.v1",
    tab: "processing",
    project_id: "proj_EXAMPLE",
    datastream_id: "ds_EXAMPLE",
    evidence: {
      plans: PLANS,
      active_version: "dsp_SECOND",
      active_mapping_version: "dmap_1",
      raw_zone_policy: "managed_raw",
      retention_days: null,
      cleanup_rules: { state: "empty", rules: [] },
      source_catalogue: NO_CATALOGUE,
      ...over,
    },
  } as unknown as WorkbenchTabPayload;
  return render(
    <WorkbenchProcessingPage
      payload={payload}
      projectId="proj_EXAMPLE"
      datastreamId="ds_EXAMPLE"
      mode="managed_feed"
      onConfirmed={vi.fn()}
      onNavigateTab={vi.fn()}
    />,
  );
}

/** The ledger's own table, never the chain's or the selector's. */
function ledger(): HTMLElement {
  return screen.getByRole("region", { name: "Processing plan versions" });
}

describe("Plan versions · how one version is told from another", () => {
  it("names each version by its number and its author, keeping the id underneath", () => {
    renderPage();
    const rows = within(ledger()).getAllByRole("row");

    // The ULID was the whole first column: two versions of one Datastream were
    // told apart by comparing 26 random characters.
    expect(within(rows[1]).getByText("Version 2")).toBeInTheDocument();
    expect(within(rows[2]).getByText("Version 1")).toBeInTheDocument();
    // The id stays reachable — it is what an operator quotes in a ticket.
    expect(within(rows[1]).getByText("dsp_SECOND")).toBeInTheDocument();

    // WHO DECIDED. An immutable ledger of decisions about a person's data named
    // nobody who took them, although `created_by` was already on the wire.
    expect(within(rows[1]).getByText("owner@example.com")).toBeInTheDocument();
    expect(within(rows[2]).getByText("analyst@example.com")).toBeInTheDocument();
  });

  it("says so rather than blanking when no author was recorded", () => {
    renderPage({
      plans: [{ ...PLANS[0], created_by: null }, PLANS[1]],
    });
    // A blank where an author belongs is indistinguishable from a rendering bug.
    expect(within(ledger()).getByText("Not recorded")).toBeInTheDocument();
  });

  it("offers the order as a column header, not as a select beside the table", () => {
    renderPage();
    // `aria-sort` is what tells assistive technology which column carries the
    // order; a select orders the data and announces nothing.
    const version = within(ledger()).getByRole("columnheader", { name: /Version/ });
    expect(version).toHaveAttribute("aria-sort", "none");
    expect(within(ledger()).getByRole("columnheader", { name: /Proposed by/ })).toBeInTheDocument();
  });
});

describe("Plan versions · what a browsed version would change", () => {
  it("compares the selected version with the one in force, path by path", async () => {
    const { container } = renderPage();

    // Nothing is selected yet, so the active version is read: it IS the base,
    // and the panel says there is nothing to compare rather than drawing an
    // empty table.
    expect(screen.getByText(/is the version this Datastream builds on/)).toBeInTheDocument();

    // Selecting the superseded version is the reading the ledger never offered.
    const firstRow = within(ledger()).getAllByRole("row")[2];
    firstRow.click();

    const compare = await screen.findByRole("region", {
      name: "Version 1 compared with Version 2 (in force)",
    });

    // ONLY THE PATHS THAT MOVED. A contract has some sixty paths; listing all of
    // them with "unchanged" beside most is how a diff becomes a dump.
    const paths = within(compare).getAllByRole("row").slice(1);
    expect(paths).toHaveLength(2);
    const cells = paths.map((row) => within(row).getAllByRole("cell")[0].textContent);
    expect(cells).toEqual(["schedule.mode", "source.selection.dimensions"]);

    // An array is ONE value a person reads as a list, not one row per index.
    expect(within(compare).getByText('["date"]')).toBeInTheDocument();
    expect(within(compare).getByText('["date","campaign"]')).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/dimensions\.0/);
  });

  it("says two versions are identical instead of drawing an empty comparison", async () => {
    renderPage({
      plans: [PLANS[0], { ...PLANS[1], normalized_payload: V2 }],
    });
    within(ledger()).getAllByRole("row")[2].click();
    expect(
      await screen.findByText(/Every governed path of Version 1 is identical to Version 2/),
    ).toBeInTheDocument();
  });
});

describe("Plan versions · proposing a change from one of them", () => {
  it("names the version it would propose from, and warns that it undoes what came after", async () => {
    renderPage();

    // On the base, the door is the ordinary one and carries no reversion
    // warning: there is nothing after it to undo.
    expect(screen.getByTestId("propose-from-plan-version")).toHaveTextContent("Propose a change…");

    within(ledger()).getAllByRole("row")[2].click();

    const door = await screen.findByTestId("propose-from-plan-version");
    expect(door).toHaveTextContent("Propose a change from this version…");
    expect(door).not.toBeDisabled();
  });

  it("keeps the raw JSON editor, behind a disclosure and seeded from the base", () => {
    renderPage();

    // KEPT: amendment 13 of the 2026-08-11 review ratified it for "what no
    // selector covers", and filters, deduplication, joins, derivations and grain
    // have no control anywhere in the console.
    const raw = screen.getByTestId("raw-processing-change");
    expect(raw).toBeInTheDocument();

    // BEHIND A DISCLOSURE: a free-text contract in the panel header reads as the
    // ordinary way to change a plan, and it is the expert escape hatch.
    expect(raw.closest("details")).not.toBeNull();
    expect(screen.getByText("Advanced")).toBeInTheDocument();
  });

  it("names the ledger by the heading a person can actually see when it is empty", () => {
    renderPage({ plans: [], active_version: null });
    // "the processing versions below" named no heading on the page.
    expect(
      screen.getByText(/Open “Advanced” above and use “Edit raw contract…”/),
    ).toBeInTheDocument();
  });
});

describe("Processing · retention says what it does and does not do", () => {
  it("never prints a retention that reads as a policy when none is declared", () => {
    renderPage();

    // `Indefinite` read as a decision taken to keep the data for ever. What is
    // true is that a Datastream intent cannot carry a retention at all
    // (`$defs/destination`, `additionalProperties: false`), nothing writes the
    // key and nothing purges on it.
    expect(screen.queryByText("Indefinite")).not.toBeInTheDocument();
    expect(screen.getByText("Not set")).toBeInTheDocument();
    expect(screen.getByText(/nothing purges its raw extracts on a schedule/)).toBeInTheDocument();
  });

  it("marks a declared retention as not enforced rather than showing a bare number", () => {
    renderPage({ retention_days: 30 });
    // A bare "30 days" is a number that looks operative on a value no schedule
    // reads.
    expect(screen.queryByText("30 days")).not.toBeInTheDocument();
    expect(screen.getByText("30 days, not enforced")).toBeInTheDocument();
    expect(screen.getByText(/no schedule reads it/)).toBeInTheDocument();
  });

  it("states the raw zone here, since Outputs no longer draws its own copy", () => {
    renderPage();
    expect(screen.getByText("Raw Zone Ownership")).toBeInTheDocument();
    expect(screen.getByText("Retention Policy")).toBeInTheDocument();
  });
});
