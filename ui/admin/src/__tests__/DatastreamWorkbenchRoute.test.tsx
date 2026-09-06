import { render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import DatastreamWorkbenchRoute from "../datastreams/workbench/DatastreamWorkbenchRoute";

const header = {
  schema: "datastream_workbench.header.v1",
  identity: {
    datastream_id: "ds_1",
    project_id: "project_1",
    name: "Orders",
    mode: "connector_pull",
    data_role: "fact",
    owner: "owner@example.com",
    module: "shopify",
    source_account_ref: "acct_1",
    declared_writer: null,
    business_domains: [{ id: "domain_1", name: "Commerce", slug: "commerce" }],
  },
  axes: {
    lifecycle: "Active",
    configuration: "Ready",
    operations: "Healthy",
    publication: "Current",
  },
  versions: {
    active_plan: "plan_1",
    active_mapping: "map_1",
    proposed_plan: null,
    proposed_mapping: null,
  },
  operations_evidence: {
    next_run_at: "2026-08-03T06:00:00Z",
    missed_run_count: 0,
    schedule_state_known: true,
    late_reasons: [],
  },
  runs: { latest: "run_1", latest_state: "published" },
  publications: {
    candidate: null,
    current: "run_1",
    last_known_good: "run_0",
  },
  links: {
    source: "/org/org_1/project/proj_1/data/sources",
    project_settings: "/org/org_1/project/proj_1/settings/general",
    governance: "/org/org_1/project/proj_1/governance/master-data",
  },
  primary_action: {
    kind: "prepare_change",
    label: "Prepare change",
    reason: "Stable.",
    tab: "processing",
  },
};

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("DatastreamWorkbenchRoute", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("renders six real tabs and four independent server axes", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      return Promise.resolve(response(url.endsWith("/processing") ? {
        schema: "datastream_workbench.processing.v1",
        tab: "processing",
        project_id: "project_1",
        datastream_id: "ds_1",
        evidence: { plans: [], active_version: "plan_1" },
      } : header));
    }));
    render(
      <DatastreamWorkbenchRoute
        projectId="project_1"
        datastreamId="ds_1"
        tab="processing"
        onNavigateTab={vi.fn()}
        // Story 57.5 — the address of a tab is built by the shell's router, which
        // is the only thing that knows the Organization segment `parsePath`
        // requires. This module used to compose `/p/{projectId}/…` itself, and
        // every one of the six links resolved to nothing.
        tabHref={(tab) => `/org/acme/project/growth/data/datastreams/object/datastream/ds_1/tab/${tab}`}
      />,
    );

    expect(await screen.findByRole("heading", { name: "Orders" })).toBeInTheDocument();
    for (const tab of ["Overview", "Data", "Mapping", "Processing", "Runs", "Outputs"]) {
      expect(screen.getByRole("link", { name: tab })).toBeInTheDocument();
    }
    for (const axis of ["Lifecycle", "Configuration", "Operations", "Publication"]) {
      expect(screen.getByText(axis)).toBeInTheDocument();
    }
    expect(screen.getByRole("button", { name: "Prepare change" })).toBeInTheDocument();
  });

  it("gives every axis its own evidence, never a placeholder", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      return Promise.resolve(response(url.endsWith("/processing") ? {
        schema: "datastream_workbench.processing.v1", tab: "processing",
        project_id: "project_1", datastream_id: "ds_1",
        evidence: { plans: [], active_version: "plan_1" },
      } : header));
    }));
    render(
      <DatastreamWorkbenchRoute projectId="project_1" datastreamId="ds_1" tab="processing" onNavigateTab={vi.fn()} />,
    );
    await screen.findByRole("heading", { name: "Orders" });

    const axes = within(screen.getByRole("group", { name: "Datastream state axes" }));

    // Every one of these was on the wire and undrawn; the four axes rendered the
    // literal strings "lifecycle evidence", "configuration evidence", and so on.
    // Four state words with nothing under them are one badge printed four times,
    // which is exactly what `datastream-workbench-and-wizard.md:182` forbids.
    expect(axes.queryByText(/^\w+ evidence$/)).not.toBeInTheDocument();

    // Configuration must name WHICH artefacts are pinned (`:88`).
    //
    // This assertion used to read `Active plan …plan_1 · mapping …map_1`, from
    // an implementation that printed `id.slice(-6)`. The fixture ids here are
    // `plan_1` and `map_1`, so the tail happened to look like a version number
    // and the assertion looked right — while in production the same code drew
    // `plan …TW5FW`, the least distinguishing six characters of a ULID, where a
    // person reads a version. A fixture that makes a mangling legible is how a
    // display defect survives a green suite.
    expect(axes.getByText(/Active plan and mapping pinned/)).toBeInTheDocument();
    // Publication must distinguish the three pointers — the whole safety model
    // rests on their being told apart.
    expect(axes.getByText(/current served · last-known-good retained/)).toBeInTheDocument();
    // Operations must say WHY, not just how it feels.
    expect(axes.getByText(/Next run/)).toBeInTheDocument();
  });

  it("names the pending change and the missed runs rather than implying them", async () => {
    const pending = {
      ...header,
      axes: { ...header.axes, configuration: "Change pending", operations: "Stale", publication: "Candidate" },
      versions: { active_plan: "plan_1", active_mapping: "map_1", proposed_plan: "plan_2", proposed_mapping: null },
      operations_evidence: { next_run_at: null, missed_run_count: 3, schedule_state_known: true, late_reasons: ["missed_runs", "overdue"] },
      publications: { candidate: "run_2", current: null, last_known_good: null },
    };
    vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      return Promise.resolve(response(url.endsWith("/processing") ? {
        schema: "datastream_workbench.processing.v1", tab: "processing",
        project_id: "project_1", datastream_id: "ds_1",
        evidence: { plans: [], active_version: "plan_1" },
      } : pending));
    }));
    render(
      <DatastreamWorkbenchRoute projectId="project_1" datastreamId="ds_1" tab="processing" onNavigateTab={vi.fn()} />,
    );
    await screen.findByRole("heading", { name: "Orders" });

    // `Change pending` without the versions says a change exists and refuses to
    // say which — the operator cannot review what they cannot name.
    const pendingAxes = within(screen.getByRole("group", { name: "Datastream state axes" }));
    // The header payload carries version IDS, never version NUMBERS, so this
    // line says what it can prove — a change is waiting, and on what — instead
    // of implying a number it was never given.
    expect(pendingAxes.getByText(/Active plan and mapping · a proposed plan is waiting/)).toBeInTheDocument();
    expect(pendingAxes.getByText(/missed runs, overdue \(3 missed\)/)).toBeInTheDocument();
    // Nothing is being served, and that must be legible rather than inferred
    // from the absence of a word.
    expect(pendingAxes.getByText(/nothing served · candidate waiting/)).toBeInTheDocument();
  });

  /**
   * Story 63.5 — the live state is a BAND OF ITS OWN, before the tabs.
   *
   * Not a fifth tile in the axes panel: "lifecycle, configuration, operations,
   * run and publication states mixed into one status" is an `Incomplete if` of
   * this surface, and where a collection has got to is not an axis of the
   * Datastream. It sits above the tab band so it is visible from all six tabs —
   * a person watching a run must not have to guess which tab shows it.
   */
  it("carries the live collection band on the header, on every tab, without a fifth axis", async () => {
    const progress = {
      schema: "datastream_progress.v1",
      project_id: "project_1",
      datastream_id: "ds_1",
      progress: null,
      idle: {
        reason: "never_ran",
        execution_id: null,
        state: null,
        ended_at: null,
        error_code: null,
      },
    };
    const answer = (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/progress")) return Promise.resolve(response(progress));
      if (url.endsWith("/processing")) {
        return Promise.resolve(response({
          schema: "datastream_workbench.processing.v1", tab: "processing",
          project_id: "project_1", datastream_id: "ds_1",
          evidence: { plans: [], active_version: "plan_1" },
        }));
      }
      return Promise.resolve(response(header));
    };
    vi.stubGlobal("fetch", vi.fn().mockImplementation(answer));
    render(
      <DatastreamWorkbenchRoute projectId="project_1" datastreamId="ds_1" tab="processing" onNavigateTab={vi.fn()} />,
    );
    await screen.findByRole("heading", { name: "Orders" });

    // The band is there, on a tab that has nothing to do with runs...
    const band = await screen.findByTestId("datastream-run-live");
    // ...and it says the honest silence rather than nothing at all.
    await waitFor(() => expect(band).toHaveTextContent("This Datastream has never run."));

    // The four axes stay four. A run state mixed in with them is exactly what
    // this surface's `Incomplete if` list forbids.
    const axes = within(screen.getByRole("group", { name: "Datastream state axes" }));
    expect(axes.queryByTestId("datastream-run-live")).not.toBeInTheDocument();
    expect(axes.getAllByText(/^(Lifecycle|Configuration|Operations|Publication)$/)).toHaveLength(4);

    // And the band precedes the tab band, so it is visible from all six.
    const tabs = screen.getByRole("navigation", { name: "Datastream" });
    expect(band.compareDocumentPosition(tabs) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("does not replace denied evidence with a different surface", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ message: "Forbidden" }, 403)));
    render(
      <DatastreamWorkbenchRoute
        projectId="project_1"
        datastreamId="ds_1"
        tab="overview"
        onNavigateTab={vi.fn()}
      />,
    );
    expect(await screen.findByText("Workbench access unavailable")).toBeInTheDocument();
    expect(screen.queryByText("Datastream overview")).not.toBeInTheDocument();
  });

  it("keeps route changes scoped and ignores a late prior response", async () => {
    let resolveFirst!: (value: Response) => void;
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/ds_1/") && !url.endsWith("/overview")) {
        return new Promise<Response>((resolve) => { resolveFirst = resolve; });
      }
      if (url.endsWith("/overview")) {
        const datastreamId = url.includes("/ds_2/") ? "ds_2" : "ds_1";
        return Promise.resolve(response({
          schema: "datastream_workbench.overview.v1",
          tab: "overview",
          project_id: "project_1",
          datastream_id: datastreamId,
          evidence: { state: "available" },
        }));
      }
      return Promise.resolve(response({
        ...header,
        identity: { ...header.identity, datastream_id: "ds_2", name: "Refunds" },
      }));
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(
      <DatastreamWorkbenchRoute
        projectId="project_1"
        datastreamId="ds_1"
        tab="overview"
        onNavigateTab={vi.fn()}
      />,
    );
    view.rerender(
      <DatastreamWorkbenchRoute
        projectId="project_1"
        datastreamId="ds_2"
        tab="overview"
        onNavigateTab={vi.fn()}
      />,
    );
    expect(await screen.findByRole("heading", { name: "Refunds" })).toBeInTheDocument();
    resolveFirst(response(header));
    await waitFor(() => expect(screen.queryByRole("heading", { name: "Orders" })).not.toBeInTheDocument());
  });
});
