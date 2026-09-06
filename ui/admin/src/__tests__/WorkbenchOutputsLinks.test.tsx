/**
 * `Outputs` links what it names — lot A4 of issue #68.
 *
 * Measured before this file existed: `WorkbenchOutputsPage.tsx` called
 * `onOpenOwner` zero times, mounted zero owner links and navigated to zero
 * tabs, while naming three publication pointers, one execution per Output
 * version and every downstream consumer. It was the tab whose whole subject is
 * *what is served, and to whom*, and it was one of four views out of nine with
 * no outgoing gesture at all.
 *
 * WHAT THESE TESTS REFUSE TO BE FOOLED BY. A dead click shipped on this surface
 * twice this week and a passing test never saw it, because a `vi.fn()` handler
 * accepts any object — including a reference `ContentRouter.openOwner` would
 * drop on the floor and an address `buildPath` would refuse to compose. So every
 * reference this page hands out is put through the REAL registry here:
 * `findObjectContract` (openOwner's own guard) and then `buildPath`/`parsePath`,
 * which throw and refuse respectively rather than returning something plausible.
 */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import WorkbenchOutputsPage from "../datastreams/workbench/pages/WorkbenchOutputsPage";
import { buildPath, parsePath, type CanonicalRoute } from "../shell/router";
import { findObjectContract } from "../shell/navigation";
import type { OwnerReference } from "../shell/pages/ProjectSettings";
import type { WorkbenchHeader, WorkbenchTabPayload } from "../datastreams/workbench/workbenchTypes";

const HEADER: WorkbenchHeader = {
  schema: "datastream_workbench.header.v1",
  identity: {
    datastream_id: "ds_EXAMPLE", project_id: "proj_EXAMPLE", name: "Orders",
    mode: "connector_pull", data_role: "fact", owner: "owner@example.com",
    module: "meta-ads", source_account_ref: "acct_1", declared_writer: null,
    business_domains: [],
  },
  axes: { lifecycle: "Active", configuration: "Ready", operations: "Healthy", publication: "Candidate" },
  versions: { active_plan: "dsp_1", active_mapping: "dmap_1", proposed_plan: null, proposed_mapping: null },
  operations_evidence: { next_run_at: null, missed_run_count: 0, schedule_state_known: true, late_reasons: [] },
  runs: { latest: "dse_cand", latest_state: "collected" },
  publications: { candidate: "dse_cand", current: "dse_cur", last_known_good: "dse_lkg" },
  links: {
    source: "/org/org_1/project/proj_EXAMPLE/data/sources",
    project_settings: "/org/org_1/project/proj_EXAMPLE/settings/general",
    governance: "/org/org_1/project/proj_EXAMPLE/governance/master-data",
  },
  primary_action: { kind: "review_candidate", label: "Review candidate", reason: "A candidate is waiting.", tab: "outputs" },
};

/**
 * The four consumer kinds `app.datastream_output_used_by` allows.
 *
 * The column carries a CHECK constraint over exactly these (migration 138), so
 * this fixture is the whole domain rather than a sample — and the fourth,
 * `delivery`, is the one this console registers no object for.
 */
function payload(overrides: Record<string, unknown> = {}): WorkbenchTabPayload {
  return {
    schema: "datastream_workbench.outputs.v1",
    tab: "outputs",
    project_id: "proj_EXAMPLE",
    datastream_id: "ds_EXAMPLE",
    evidence: {
      raw_zone_policy: "managed_raw",
      retention_days: 30,
      outputs: [
        {
          id: "dso_1", output_kind: "full_grain", stable_name: "orders_daily",
          version_id: "dsov_cur", execution_id: "dse_cur", plan_version_id: "dsp_1",
          mapping_version_id: "dmap_1", delivery_ref: null,
          created_at: "2026-07-30T08:00:00Z",
        },
      ],
      used_by: [
        { output_id: "dso_1", output_version_id: "dsov_cur", consumer_kind: "semantic_view",
          consumer_ref: "sv_EXAMPLE", consumer_version_ref: "svv_EXAMPLE",
          owner_href: "/not/an/address" },
        { output_id: "dso_1", output_version_id: "dsov_cur", consumer_kind: "report",
          consumer_ref: "rep_EXAMPLE", consumer_version_ref: "repv_EXAMPLE",
          owner_href: "/not/an/address" },
        { output_id: "dso_1", output_version_id: "dsov_cur", consumer_kind: "result",
          consumer_ref: "res_EXAMPLE", consumer_version_ref: null,
          owner_href: "/not/an/address" },
        { output_id: "dso_1", output_version_id: "dsov_cur", consumer_kind: "delivery",
          consumer_ref: "sftp://example.com/orders", consumer_version_ref: null,
          owner_href: "/not/an/address" },
      ],
      ...overrides,
    },
  } as WorkbenchTabPayload;
}

function renderOutputs(props: Record<string, unknown> = {}) {
  return render(
    <WorkbenchOutputsPage
      header={HEADER}
      payload={payload()}
      projectId="proj_EXAMPLE"
      datastreamId="ds_EXAMPLE"
      onConfirmed={() => undefined}
      {...props}
    />,
  );
}

/**
 * The two gates `ContentRouter` puts every owner reference through, applied to
 * the reference this page produced.
 *
 * `findObjectContract` is the guard in `openOwner` itself — a type the section
 * does not declare makes it return before navigating, in silence. `buildPath`
 * is the second: it validates the whole combination (tab addressable, version
 * only on a version-bearing tab) and THROWS rather than composing an address
 * that would reopen something else. A mocked handler sees neither.
 */
function assertTheRouterWouldAcceptIt(owner: OwnerReference) {
  expect(owner.object_id).toBeTruthy();
  expect(owner.object_type).toBeTruthy();
  expect(findObjectContract(owner.workspace!, owner.section!, owner.object_type!)).not.toBeNull();
  const route = {
    scope: "project",
    organizationId: "org_1",
    projectId: "proj_EXAMPLE",
    workspace: owner.workspace,
    section: owner.section,
    lens: null,
    objectType: owner.object_type,
    objectId: owner.object_id,
    tab: owner.tab,
    versionId: owner.version_id,
    evidenceId: null,
    action: null,
    query: {},
    globalSurface: null,
    globalSection: null,
  } as unknown as CanonicalRoute;
  const path = buildPath(route);
  expect(parsePath(path).kind).toBe("resolved");
}

describe("Outputs links what it names", () => {
  it("opens the run behind each of the three publication pointers", async () => {
    const opened = vi.fn();
    renderOutputs({ onOpenRun: opened });

    const roles = within(screen.getByRole("group", { name: "Publication roles" }));
    for (const executionId of ["dse_cand", "dse_cur", "dse_lkg"]) {
      await userEvent.click(roles.getByRole("button", { name: `Open the run ${executionId}` }));
    }
    // THE ID TRAVELS. The three roles stay roles — `Candidate`, `Current` and
    // `Last-known-good` are still the labels — and what opens is the exact run
    // that fills each, never the unfiltered Runs tab.
    expect(opened.mock.calls.map(([id]) => id)).toEqual(["dse_cand", "dse_cur", "dse_lkg"]);
  });

  it("opens the run that produced an Output version", async () => {
    const opened = vi.fn();
    renderOutputs({ onOpenRun: opened });

    const table = within(screen.getByRole("region", { name: "Physical Outputs" }));
    await userEvent.click(table.getByRole("button", { name: "Open the run dse_cur" }));
    expect(opened).toHaveBeenCalledWith("dse_cur");
  });

  it("names an execution without pretending to open it when no handler exists", () => {
    // The prop is optional, and the component sheet mounts this page without it.
    // A control that calls nothing is the dead click this lot exists to remove,
    // so the id stays visible and inert — and says so on hover.
    renderOutputs();

    expect(screen.queryByRole("button", { name: /^Open the run/ })).not.toBeInTheDocument();
    expect(screen.getAllByTitle(/opens nothing/).length).toBeGreaterThan(0);
    // Still NAMED: the pointer is the evidence, and hiding it would erase it.
    expect(screen.getAllByText("dse_cur").length).toBeGreaterThan(0);
  });

  it("builds an address the real router accepts for every consumer kind it links", async () => {
    const opened = vi.fn();
    renderOutputs({ onOpenOwner: opened });

    const panel = within(screen.getByRole("list", { name: "Used by" }));
    for (const label of [
      "Semantic View · sv_EXAMPLE",
      "Report · rep_EXAMPLE",
      "Result · res_EXAMPLE",
    ]) {
      await userEvent.click(panel.getByRole("button", { name: label }));
    }
    expect(opened).toHaveBeenCalledTimes(3);
    for (const [owner] of opened.mock.calls) assertTheRouterWouldAcceptIt(owner as OwnerReference);
  });

  it("pins the version only where a workbench opens one", async () => {
    const opened = vi.fn();
    renderOutputs({ onOpenOwner: opened });

    const panel = within(screen.getByRole("list", { name: "Used by" }));
    await userEvent.click(panel.getByRole("button", { name: "Semantic View · sv_EXAMPLE" }));
    // `GovernanceObjectWorkbench` reads `versionId` on the `versions` tab, so
    // the pin is real and the panel's "version-bound" claim is met.
    expect(opened).toHaveBeenLastCalledWith(expect.objectContaining({
      workspace: "governance", section: "semantic-model",
      object_type: "semantic-view", tab: "versions", version_id: "svv_EXAMPLE",
    }));

    // The Report carries a version ref too, and it is NOT pinned:
    // `VERSION_READING_OBJECT_TYPES` in `objectSurfaces.tsx` holds two types and
    // `report` is not one, so a pinned version opens a refusal screen instead of
    // the report. The version is still SHOWN, as text, beside the link.
    await userEvent.click(panel.getByRole("button", { name: "Report · rep_EXAMPLE" }));
    expect(opened).toHaveBeenLastCalledWith(expect.objectContaining({
      object_type: "report", tab: "overview", version_id: null,
    }));
    expect(panel.getByText("repv_EXAMPLE")).toBeInTheDocument();
  });

  it("leaves a delivery named and inert, with the reason it opens nothing", async () => {
    const opened = vi.fn();
    renderOutputs({ onOpenOwner: opened });

    const panel = within(screen.getByRole("list", { name: "Used by" }));
    const label = "Delivery · sftp://example.com/orders";
    // No control at all — not a disabled one, not a link to the stored
    // `owner_href`, which the router refuses anyway.
    expect(panel.queryByRole("button", { name: label })).not.toBeInTheDocument();
    expect(panel.queryByRole("link", { name: label })).not.toBeInTheDocument();
    expect(panel.getByText(label)).toBeInTheDocument();
    expect(panel.getByTitle(/not an object this console registers/)).toBeInTheDocument();
    expect(opened).not.toHaveBeenCalled();
  });

  it("never renders a stored owner_href the router refuses", () => {
    renderOutputs({ onOpenOwner: vi.fn() });

    // Every fixture row carries `/not/an/address`, which `parsePath` refuses on
    // its first segment. It must reach no `href` anywhere on this tab.
    for (const anchor of document.querySelectorAll("a[href]")) {
      expect(anchor.getAttribute("href")).not.toBe("/not/an/address");
    }
  });
});

/**
 * The comparison the tab exists to make, and what it owes a reader when it
 * cannot make it — amended 2026-08-18.
 *
 * `Not comparable` on the screen that decides a promotion told a person that
 * something was wrong and nothing about whether it was theirs to fix. The
 * `schema_hash` case in particular has a class of absentees no gesture can
 * repair: two writers insert into `app.datastream_output_versions`, both
 * `ON CONFLICT (output_id, execution_id) DO NOTHING`, and the run's row landed
 * first — so the publication's row, hash included, was discarded, and
 * `trg_datastream_output_versions_immutable` refuses the UPDATE that would fill
 * it. Those rows read "Unavailable" for ever, and the screen has to say so.
 */
describe("Outputs — the candidate comparison, and its honest refusals", () => {
  const withHashes = (candidate: string | null, current: string | null) =>
    payload({
      outputs: [
        { id: "dso_1", output_kind: "full_grain", stable_name: "orders_daily",
          version_id: "dsov_cur", execution_id: "dse_cur", plan_version_id: "dsp_1",
          mapping_version_id: "dmap_1", schema_hash: current, delivery_ref: null,
          created_at: "2026-07-30T08:00:00Z" },
        { id: "dso_1", output_kind: "full_grain", stable_name: "orders_daily",
          version_id: "dsov_cand", execution_id: "dse_cand", plan_version_id: "dsp_1",
          mapping_version_id: "dmap_1", schema_hash: candidate, delivery_ref: null,
          created_at: "2026-08-17T08:00:00Z" },
      ],
    });

  function readiness(): HTMLElement {
    return screen.getByRole("region", { name: "Candidate readiness" });
  }

  it("compares two schemas when both publications recorded one", () => {
    renderOutputs({ payload: withHashes("c".repeat(64), "c".repeat(64)) });
    const schema = within(readiness()).getAllByRole("row")[1];
    // The whole point of the repair: this line could never say anything but
    // "Not comparable", because nothing wrote the column on either side.
    expect(within(schema).getByText("Unchanged")).toBeInTheDocument();
  });

  it("calls a moved schema Changed, and never a verdict on it", () => {
    renderOutputs({ payload: withHashes("d".repeat(64), "c".repeat(64)) });
    const schema = within(readiness()).getAllByRole("row")[1];
    expect(within(schema).getByText("Changed")).toBeInTheDocument();
    // `Changed` is a fact; whether it is acceptable is the operator's call.
    expect(within(schema).queryByText(/unsafe|do not promote|invalid/i)).not.toBeInTheDocument();
  });

  it("says WHY a comparison cannot be made, and that it will never be repairable", () => {
    renderOutputs({ payload: withHashes(null, "c".repeat(64)) });
    const schema = within(readiness()).getAllByRole("row")[1];

    // TWO UNKNOWNS ARE NOT A MATCH, and one unknown is not a verdict either.
    expect(within(schema).getByText("Not comparable")).toBeInTheDocument();
    expect(within(schema).queryByText("Unchanged")).not.toBeInTheDocument();

    // The bare label sent a person looking for a gesture that does not exist.
    expect(within(schema).getByText(/carry none and never will/)).toBeInTheDocument();
    expect(within(schema).getByText(/Read the two runs side by side instead/)).toBeInTheDocument();
  });

  it("does not announce Unchanged when NEITHER side recorded a schema", () => {
    renderOutputs({ payload: withHashes(null, null) });
    const schema = within(readiness()).getAllByRole("row")[1];
    // This is the regression that shipped: both sides read the literal
    // "Unavailable", compared equal, and the table said `Unchanged` in green.
    expect(within(schema).queryByText("Unchanged")).not.toBeInTheDocument();
    expect(within(schema).getByText("Not comparable")).toBeInTheDocument();
  });
});

/**
 * The visual curation of issue #69, applied to this tab: what a person comes for
 * is at the top, and no sentence carries the repository's vocabulary.
 */
describe("Outputs — what the tab is about, and in whose words", () => {
  it("states the raw zone once, on Processing, and does not draw a second copy here", () => {
    renderOutputs();

    // AMENDED 2026-08-18. This tab drew `Raw Zone Ownership` and `Retention
    // Policy` WORD FOR WORD as `WorkbenchProcessingPage` draws them: one fact,
    // two panels, free to diverge the day either is reworded. Rank 4 of the
    // ratified `Processing` order owns the subject; this tab keeps the road to
    // it, which is what the 2026-08-05 amendment was protecting — the fact must
    // stay readable in the Workbench, not that it be readable HERE.
    expect(screen.queryByText("Raw Zone Ownership")).not.toBeInTheDocument();
    expect(screen.queryByText("Retention Policy")).not.toBeInTheDocument();

    // And it must still be findable: a fact moved without a road is a fact lost.
    const pointer = screen.getByText(/set with the plan that writes them/);
    expect(pointer).toBeInTheDocument();
    expect(pointer.textContent).toMatch(/Processing/);

    // Still LAST: what is served and who reads it come first. The heading is the
    // anchor, not a region — `region` on this tab comes from `TableScroll`, and
    // this block has no table.
    const usedBy = screen.getByRole("list", { name: "Used by" });
    const outputs = screen.getByRole("region", { name: "Physical Outputs" });
    const rawZone = screen.getByRole("heading", { name: "Where the raw extracts land" });
    expect(outputs.compareDocumentPosition(rawZone) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(usedBy.compareDocumentPosition(rawZone) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("says why each list is empty in words that are not the table's own", () => {
    render(
      <WorkbenchOutputsPage
        header={{ ...HEADER, publications: { candidate: null, current: null, last_known_good: null } }}
        payload={payload({ outputs: [], used_by: [] })}
        projectId="proj_EXAMPLE"
        datastreamId="ds_EXAMPLE"
        onConfirmed={() => undefined}
      />,
    );

    // `No published Output version evidence exists` named the column
    // (`datastream_output_versions.evidence`) and no gesture.
    expect(screen.queryByText(/Output version evidence/)).not.toBeInTheDocument();
    expect(screen.getByText(/A run that reaches Published produces an Output version/)).toBeInTheDocument();

    // `No governed consumer is pinned to an Output version` is the row's own
    // vocabulary, and it stays out.
    expect(screen.queryByText(/governed consumer/)).not.toBeInTheDocument();

    // THE GESTURE IS NAMED AGAIN — re-measured 2026-08-18. This assertion held
    // the OPPOSITE until today, on the finding that `INSERT INTO
    // app.datastream_output_used_by` appeared nowhere in `server/`. The writer
    // now exists: `query_execution.record_output_consumption` writes a
    // `result` row when an Analyze execution resolves this Datastream's
    // `relation_ref`. An empty state that promises nothing when something does
    // fill it is the same defect as one that promises a gesture that does not
    // exist.
    expect(screen.getByText(/ask a question in Explore/)).toBeInTheDocument();

    // And it still does not invite anyone to "bind a consumer": no screen binds
    // one, and the 2026-08-12 ruling forbids the publication path from writing
    // these rows at all. A row is an observation of what WAS read.
    expect(screen.getByText(/what was read, not who was promised access/)).toBeInTheDocument();
  });
});
