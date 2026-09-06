/**
 * The ordered processing chain — WHERE it sits, and what it still says.
 *
 * Finding D-5 of the 2026-08-12 visual review (github issue #69): the nine steps
 * this Datastream performs were the LAST block of the `Processing` tab, under
 * the schedule, the raw-zone policy, the version ledger and the collection
 * selector — and rendered as a four-column table, which is a lookup and not a
 * sequence. `Processing` answers « what does this Datastream do, in what order ».
 *
 * What these tests hold is the set of things a reordering can quietly cost:
 *
 *   * the chain drifting back down the page behind a panel someone appends;
 *   * a flow that reads as a flow but drops the two properties the table had —
 *     a step whose evidence is absent SAYS SO, and every step names the surface
 *     that repairs it;
 *   * "nothing applies here" rendered as "nobody could read this", which are
 *     opposite statements about a person's data;
 *   * the detail table losing its live doors to become a summary's decoration;
 *   * the block vanishing on a Datastream with no plan version at all, leaving a
 *     hole where the first reading of the tab belongs.
 */
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import WorkbenchProcessingPage from "../datastreams/workbench/pages/WorkbenchProcessingPage";
import type { WorkbenchTabPayload } from "../datastreams/workbench/workbenchTypes";

const PLAN = {
  source: {
    kind: "connector_pull",
    module: "google-ads",
    report_id: "catalog_daily",
    selection: {
      selection_mode: "catalog_driven",
      metrics: ["clicks"],
      dimensions: ["date"],
      grain: ["date"],
      filters: [],
    },
  },
  destination: { policy: "managed_raw" },
  schedule: { mode: "daily", timezone: "UTC" },
  historical: {},
};

/** A file source: no provider catalogue is offered, so the selector answers
 *  instead of disappearing and nothing here depends on a connector manifest. */
const NO_CATALOGUE = {
  state: "no_module",
  mode: "managed_feed",
  reason: "A file source pulls no selection: the columns are the ones the file brings.",
};

function payload(evidence: Record<string, unknown>): WorkbenchTabPayload {
  return {
    schema: "datastream_workbench.processing.v1",
    tab: "processing",
    project_id: "proj_EXAMPLE",
    datastream_id: "ds_EXAMPLE",
    evidence,
  } as unknown as WorkbenchTabPayload;
}

function evidence(overrides: Record<string, unknown> = {}) {
  return {
    plans: [
      {
        id: "dsp_HEAD",
        version_number: 1,
        normalized_payload: PLAN,
        content_hash: "a".repeat(64),
        executable: true,
        validation_issues: [],
        created_at: "2026-08-01T00:00:00Z",
      },
    ],
    active_version: "dsp_HEAD",
    active_mapping_version: "dmap_1",
    raw_zone_policy: "managed_raw",
    retention_days: 30,
    cleanup_rules: { state: "empty", rules: [] },
    source_catalogue: NO_CATALOGUE,
    ...overrides,
  };
}

function renderPage(over: Record<string, unknown> = {}) {
  return render(
    <WorkbenchProcessingPage
      payload={payload(evidence(over))}
      projectId="proj_EXAMPLE"
      datastreamId="ds_EXAMPLE"
      mode="managed_feed"
      onConfirmed={vi.fn()}
      onNavigateTab={vi.fn()}
    />,
  );
}

/** The rail itself, whatever it is built from. */
function stepper(container: HTMLElement): HTMLElement {
  const rail = container.querySelector<HTMLElement>('[data-slot="stepper"]');
  if (!rail) throw new Error("No stepper is rendered in the processing chain.");
  return rail;
}

describe("Processing · where the chain sits", () => {
  it("puts the chain FIRST, with the ledger that pins it right after", () => {
    renderPage();
    const headings = screen
      .getAllByRole("heading", { level: 2 })
      .map((node) => node.textContent ?? "");
    // Not "somewhere above the schedule": first. The tab's subject is what this
    // Datastream does; the schedule is when it does it.
    expect(headings[0]).toBe("Ordered processing chain · dsp_HEAD");
    // And the version ledger travels WITH it — the chain's own title names the
    // version it reads, and the ledger is the control that changes which one.
    expect(headings[1]).toBe("Immutable processing plan versions");
    // And WHEN it runs comes after WHAT it does. The schedule panel titles
    // itself from the source kind, so the assertion names the panel, not one of
    // its two wordings.
    expect(
      headings.findIndex((title) => title === "Schedule" || title === "How this file arrives"),
    ).toBeGreaterThan(1);
  });

  it("reads as a flow before it reads as a table", () => {
    const { container } = renderPage();
    const rail = stepper(container);
    expect(rail.getAttribute("data-orientation")).toBe("horizontal");
    // Nine steps, in order, named without their rank: the rail numbers its own
    // marks, and a number written into the label renumbers nothing when a step
    // is inserted above it.
    expect(
      Array.from(rail.querySelectorAll("li")).map((li) =>
        li.querySelector("span.text-label")?.textContent ?? "",
      ),
    ).toEqual([
      "Source or import", "Parse and select", "Map to governed fields",
      "Capability projection", "Resulting grain", "Cleanup rules",
      "Data quality", "Output", "Cadence and history",
    ]);
    // The table is the unfolding, not the replacement: the same nine steps with
    // what pins each one.
    const chain = within(screen.getByRole("region", { name: "Ordered processing steps" }));
    expect(chain.getByText("1 · Source or import")).toBeInTheDocument();
    expect(chain.getByText("9 · Cadence and history")).toBeInTheDocument();
  });
});

describe("Processing · what the flow must not lose", () => {
  it("says a step nothing pins is unpinned, and names the surface that repairs it", () => {
    const { container } = renderPage({ active_mapping_version: null });
    const rail = stepper(container);
    const step = Array.from(rail.querySelectorAll("li")).find((li) =>
      li.textContent?.includes("Map to governed fields"));
    expect(step).toBeDefined();
    // A step whose evidence is absent SAYS SO rather than disappearing…
    expect(step!.textContent).toContain("Nothing pins it");
    // …and it does not stop at the finding: the surface that repairs it is named
    // on the step, where the next gesture is decided.
    expect(step!.textContent).toContain("Repair in Mapping");
    // The full sentence stays in the unfolding, and so does the live door.
    const chain = within(screen.getByRole("region", { name: "Ordered processing steps" }));
    expect(
      chain.getByText("No active mapping version, so nothing binds the physical fields"),
    ).toBeInTheDocument();
    expect(chain.getByRole("button", { name: "Mapping" })).toBeInTheDocument();
  });

  it("never renders a settled 'nothing applies' as an unreadable step", () => {
    // Two opposite statements about a person's data. This plan carries no
    // `geographic` block — no capability projects an effect into it, which is an
    // ANSWER — while a cleanup store that failed to read is an unknown.
    const { container } = renderPage({
      cleanup_rules: { state: "unavailable", rules: [], reason: "The cleanup rules could not be read." },
    });
    const lines = Array.from(stepper(container).querySelectorAll("li"))
      .map((li) => li.textContent ?? "");
    expect(lines.find((line) => line.includes("Capability projection"))).toContain("Nothing applies");
    expect(lines.find((line) => line.includes("Cleanup rules"))).toContain("Nothing pins it");
    expect(lines.find((line) => line.includes("Cleanup rules"))).toContain(
      "Repair in Governance › Cleanup Rules",
    );
    // A settled step offers no repair, because there is nothing to repair.
    expect(lines.find((line) => line.includes("Capability projection"))).not.toContain("Repair in");
  });

  it("keeps the evidence and the owner of every step in the unfolding", () => {
    renderPage();
    const chain = within(screen.getByRole("region", { name: "Ordered processing steps" }));
    expect(chain.getByText("dmap_1")).toBeInTheDocument();
    expect(chain.getByText("1 metric(s), 1 dimension(s), 0 filter(s)")).toBeInTheDocument();
    expect(
      chain.getByText(/No cleanup rule reaches this Datastream/),
    ).toBeInTheDocument();
    // Owners that route inside the Workbench stay doors, not labels.
    for (const name of ["Mapping", "Outputs"]) {
      expect(chain.getByRole("button", { name })).toBeInTheDocument();
    }
  });
});

describe("Processing · a Datastream with no plan version", () => {
  it("says why the chain is empty and names where a first version is appended", () => {
    renderPage({ plans: [], active_version: null });
    // The block does NOT disappear: it is the first reading of the tab, and a
    // hole at the top of a page is not an answer.
    expect(screen.getByText("Ordered processing chain")).toBeInTheDocument();
    expect(screen.getByText("No plan version pins this chain yet")).toBeInTheDocument();
    expect(
      screen.getByText(/Append a first version from Immutable processing plan versions below/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Ordered processing steps" })).toBeNull();
  });
});
