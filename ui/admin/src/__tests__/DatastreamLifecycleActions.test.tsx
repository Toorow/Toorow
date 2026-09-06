/**
 * What can be DONE to a Datastream as an object — rename, archive, restore.
 *
 * WHAT THIS FILE HOLDS OPEN, added 2026-08-18. Measured before it was written:
 * `grep -rn "archive\|rename\|duplicate" ui/admin/src/datastreams/**` returned
 * comments and one unrelated observation code. There was no rename, no archive,
 * no restore and no delete anywhere on the Datastream surface — while
 * `SchedulePanel` told a person, in two of its four run states, that an archived
 * Datastream "cannot be started from here. Restoring it is what makes it
 * runnable again". The console named a gesture it did not offer, for a state it
 * could not leave.
 *
 *   * every verb offered has an executor. `Duplicate` is deliberately ABSENT —
 *     no route copies a Datastream's plan version, mapping version, schedule
 *     state and connection binding, and a control with no executor is the defect
 *     this repository keeps finding;
 *   * the archive names WHAT STOPS before it stops it, from the header's own
 *     evidence — never a count this screen invented;
 *   * archive and delete are ONE item, because they are one route and the
 *     disposition is decided from what is stored, not chosen by the clicker;
 *   * a hard delete leaves no object, so the screen says so instead of re-reading
 *     a header that would answer 404;
 *   * restore says it does NOT start collecting, which is the whole reason the
 *     server leaves `enabled` false.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import DatastreamWorkbenchRoute from "../datastreams/workbench/DatastreamWorkbenchRoute";

/**
 * `userEvent`, not `fireEvent.click` — a Radix `DropdownMenu` opens on
 * POINTERDOWN, and jsdom's `fireEvent.click` dispatches no pointer events at
 * all, so the menu never opens. `pointerEventsCheck: 0` because Radix puts
 * `pointer-events: none` on the body while a menu is open, which the default
 * check reads as "not clickable" — right in a browser, wrong in jsdom.
 * `ConnectButton.test.tsx` records the same two facts, learnt the same way.
 */
const user = userEvent.setup({ pointerEventsCheck: 0 });

const HEADER = {
  schema: "datastream_workbench.header.v1",
  identity: {
    datastream_id: "ds_EXAMPLE",
    project_id: "proj_EXAMPLE",
    name: "Search Console — daily",
    mode: "connector_pull",
    data_role: "fact",
    owner: "owner@example.com",
    module: "search-console",
    connector: "search-console",
    source_account_ref: "sacc_EXAMPLE",
    declared_writer: null,
    business_domains: [],
  },
  axes: {
    lifecycle: "Active",
    configuration: "Ready",
    operations: "Healthy",
    publication: "Current",
  },
  versions: { active_plan: "plan_1", active_mapping: "map_1", proposed_plan: null, proposed_mapping: null },
  operations_evidence: {
    next_run_at: "2026-08-19T06:00:00Z",
    missed_run_count: 0,
    schedule_state_known: true,
    late_reasons: [],
  },
  runs: { latest: "run_EXAMPLE", latest_state: "published" },
  publications: { candidate: null, current: "run_EXAMPLE", last_known_good: null },
  links: { source: "", project_settings: "", governance: "" },
  primary_action: { kind: "prepare_change", label: "Prepare change", reason: "Settled.", tab: "processing" },
};

const ARCHIVED_HEADER = { ...HEADER, axes: { ...HEADER.axes, lifecycle: "Archived" } };

function json(body: unknown, status = 200) {
  return Promise.resolve(
    new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }),
  );
}

function stubApi(options: {
  header?: unknown;
  patchStatus?: number;
  deleteAnswer?: unknown;
  restoreStatus?: number;
  restoreBody?: unknown;
} = {}) {
  const calls: Array<{ url: string; method: string; body?: string }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      calls.push({ url, method, body: init?.body as string | undefined });

      if (method === "PATCH") {
        if (options.patchStatus && options.patchStatus !== 200) {
          return json(
            { code: "conflict", message: "A Datastream of this project already uses that name." },
            options.patchStatus,
          );
        }
        return json({ id: "ds_EXAMPLE", name: "Renamed" });
      }
      if (method === "DELETE") return json(options.deleteAnswer ?? { status: "archived", id: "ds_EXAMPLE" });
      if (url.endsWith("/restore")) {
        if (options.restoreStatus && options.restoreStatus !== 200) {
          return json(
            options.restoreBody ?? {
              code: "name_taken",
              message: "Another Datastream of this project is already called « Search Console — daily ». "
                + "Rename that one, or rename this one, and restore it again.",
            },
            options.restoreStatus,
          );
        }
        return json({ id: "ds_EXAMPLE" });
      }
      if (url.endsWith("/progress")) {
        return json({
          schema: "datastream_progress.v1", project_id: "proj_EXAMPLE", datastream_id: "ds_EXAMPLE",
          progress: null,
          idle: { reason: "never_ran", execution_id: null, state: null, ended_at: null, error_code: null },
        });
      }
      if (url.endsWith("/processing")) {
        return json({
          schema: "datastream_workbench.processing.v1", tab: "processing",
          project_id: "proj_EXAMPLE", datastream_id: "ds_EXAMPLE",
          evidence: { plans: [], active_version: "plan_1" },
        });
      }
      return json(options.header ?? HEADER);
    }),
  );
  return calls;
}

function mount() {
  return render(
    <DatastreamWorkbenchRoute
      projectId="proj_EXAMPLE"
      datastreamId="ds_EXAMPLE"
      tab="processing"
      onNavigateTab={vi.fn()}
    />,
  );
}

async function openMenu() {
  await user.click(await screen.findByTestId("datastream-lifecycle-menu"));
}

describe("Datastream lifecycle actions", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("offers rename and archive on a live Datastream, and no verb without an executor", async () => {
    stubApi();
    mount();
    await openMenu();

    expect(await screen.findByTestId("lifecycle-rename")).toBeInTheDocument();
    expect(screen.getByTestId("lifecycle-archive")).toBeInTheDocument();
    // Restore is for an archived one, and it is absent here rather than inert.
    expect(screen.queryByTestId("lifecycle-restore")).not.toBeInTheDocument();
    // `Duplicate` is served by no route. A menu item that opens a dialog whose
    // confirmation cannot call anything is worse than its absence.
    //
    // SCOPED TO THE MENU, and that is not pedantry: `screen.queryByText` caught
    // `SchedulePanel`'s "no duplicate survives" three thousand pixels below, so
    // the unscoped version of this line failed for a reason that had nothing to
    // do with the claim it makes.
    const menu = within(screen.getByRole("menu"));
    expect(menu.queryByText(/Duplicate/i)).not.toBeInTheDocument();
    expect(menu.queryByText(/Delete/i)).not.toBeInTheDocument();
  });

  it("renames through the route that has always accepted a name", async () => {
    const calls = stubApi();
    mount();
    await openMenu();
    await user.click(await screen.findByTestId("lifecycle-rename"));

    const input = await screen.findByTestId("lifecycle-rename-input");
    fireEvent.change(input, { target: { value: "Search Console — weekly" } });
    fireEvent.click(screen.getByTestId("lifecycle-rename-go"));

    await waitFor(() => expect(screen.getByText("Renamed")).toBeInTheDocument());
    const patch = calls.find((call) => call.method === "PATCH");
    expect(patch).toBeDefined();
    expect(JSON.parse(patch!.body!)).toEqual({
      project_id: "proj_EXAMPLE",
      name: "Search Console — weekly",
    });
  });

  it("keeps the server's sentence on a name collision", async () => {
    stubApi({ patchStatus: 409 });
    mount();
    await openMenu();
    await user.click(await screen.findByTestId("lifecycle-rename"));
    fireEvent.change(screen.getByTestId("lifecycle-rename-input"), {
      target: { value: "Taken name" },
    });
    fireEvent.click(screen.getByTestId("lifecycle-rename-go"));

    // `HTTP 409` alone tells a person nothing they can act on.
    await waitFor(() =>
      expect(screen.getByText(/already uses that name/)).toBeInTheDocument(),
    );
    // And the dialog stays open, so the name can be corrected where it was typed.
    expect(screen.getByTestId("lifecycle-rename-confirm")).toBeInTheDocument();
  });

  it("refuses an empty rename before it reaches a route that would call it a success", async () => {
    const calls = stubApi();
    mount();
    await openMenu();
    await user.click(await screen.findByTestId("lifecycle-rename"));
    fireEvent.change(screen.getByTestId("lifecycle-rename-input"), { target: { value: "   " } });
    fireEvent.click(screen.getByTestId("lifecycle-rename-go"));

    await waitFor(() => expect(screen.getByText(/A Datastream needs a name/)).toBeInTheDocument());
    expect(calls.some((call) => call.method === "PATCH")).toBe(false);
  });

  /**
   * THE ARCHIVE NAMES WHAT STOPS, and every row of it is a fact the header
   * already carried. A confirmation that says only "are you sure?" asks for
   * consent to consequences it declined to state.
   */
  it("names the next run, what is served and the account before archiving", async () => {
    stubApi();
    mount();
    await openMenu();
    await user.click(await screen.findByTestId("lifecycle-archive"));

    const dialog = await screen.findByTestId("lifecycle-archive-confirm");
    expect(dialog).toHaveTextContent("search-console");
    expect(dialog).toHaveTextContent("sacc_EXAMPLE");
    // The run that will not happen...
    expect(dialog).toHaveTextContent(/this run will not happen/);
    // ...and what downstream is reading today, which stays readable.
    expect(dialog).toHaveTextContent(/run_EXAMPLE/);
    // ONE item for two outcomes, and the rule that picks between them is stated
    // rather than left to be discovered after the click.
    expect(dialog).toHaveTextContent(/it is ARCHIVED/);
    expect(dialog).toHaveTextContent(/it is DELETED outright/);
  });

  it("reports the disposition the ROUTE chose, not the one it hoped for", async () => {
    stubApi({ deleteAnswer: { status: "archived", id: "ds_EXAMPLE" } });
    mount();
    await openMenu();
    await user.click(await screen.findByTestId("lifecycle-archive"));
    fireEvent.click(await screen.findByTestId("lifecycle-archive-go"));

    await waitFor(() => expect(screen.getByText("Archived")).toBeInTheDocument());
    expect(screen.getByText(/Restore brings it back/)).toBeInTheDocument();
  });

  it("a hard delete leaves no object, and the screen stops pretending there is one", async () => {
    stubApi({ deleteAnswer: { status: "deleted", id: "ds_EXAMPLE" } });
    mount();
    await openMenu();
    await user.click(await screen.findByTestId("lifecycle-archive"));
    fireEvent.click(await screen.findByTestId("lifecycle-archive-go"));

    // Re-reading the header here answers 404 and lands a person on "Workbench
    // evidence unavailable" -- an error, for something that worked exactly as
    // they asked.
    await waitFor(() =>
      expect(screen.getByText("This Datastream no longer exists")).toBeInTheDocument(),
    );
    expect(screen.getByText(/nothing left to restore/)).toBeInTheDocument();
    expect(screen.queryByTestId("datastream-lifecycle-menu")).not.toBeInTheDocument();
  });

  it("an archived Datastream is told so once, and offered the way back", async () => {
    stubApi({ header: ARCHIVED_HEADER });
    mount();

    // Visible above the tab band, so the same sentence is read from all eight
    // readings rather than from whichever tab happens to be open.
    expect(await screen.findByText("This Datastream is archived")).toBeInTheDocument();

    await openMenu();
    expect(await screen.findByTestId("lifecycle-restore")).toBeInTheDocument();
    // And the archive is gone: archiving something already archived is not a
    // gesture, and offering it would be a control that refuses itself.
    expect(screen.queryByTestId("lifecycle-archive")).not.toBeInTheDocument();
  });

  it("restore says it does NOT start collecting, before and after", async () => {
    const calls = stubApi({ header: ARCHIVED_HEADER });
    mount();
    await openMenu();
    await user.click(await screen.findByTestId("lifecycle-restore"));

    const dialog = await screen.findByTestId("lifecycle-restore-confirm");
    // The two consents are named apart: restoring brings it back, starting it
    // spends. The server leaves `enabled` false for exactly this reason.
    expect(dialog).toHaveTextContent(/does NOT start collecting/);
    expect(dialog).toHaveTextContent("Configured, not collecting");
    // And the one refusal a person can act on is named before the click.
    expect(dialog).toHaveTextContent(/If the name was reused/);

    fireEvent.click(screen.getByTestId("lifecycle-restore-go"));
    await waitFor(() => expect(screen.getByText("Restored")).toBeInTheDocument());
    expect(screen.getByText(/it is NOT collecting yet/)).toBeInTheDocument();

    const restore = calls.find((call) => call.url.endsWith("/restore"));
    expect(restore?.method).toBe("POST");
    expect(JSON.parse(restore!.body!)).toEqual({ project_id: "proj_EXAMPLE" });
  });

  it("a name taken by a rebuild refuses the restore and says which one holds it", async () => {
    // The cost of migration 256, paid where it lands: an archived Datastream
    // stops reserving its name precisely so the same feed can be rebuilt under
    // it, which is what makes this collision a real case rather than a
    // theoretical one.
    stubApi({ header: ARCHIVED_HEADER, restoreStatus: 409 });
    mount();
    await openMenu();
    await user.click(await screen.findByTestId("lifecycle-restore"));
    fireEvent.click(await screen.findByTestId("lifecycle-restore-go"));

    await waitFor(() =>
      expect(screen.getByText(/Rename that one, or rename this one/)).toBeInTheDocument(),
    );
    expect(screen.getByTestId("lifecycle-restore-confirm")).toBeInTheDocument();
    expect(screen.queryByText("Restored")).not.toBeInTheDocument();
  });

  it("keeps the four state axes four -- the menu is not a fifth", async () => {
    stubApi();
    mount();
    await screen.findByTestId("datastream-lifecycle-menu");

    // "lifecycle, configuration, operations, run and publication states are
    // mixed into one status" is an `Incomplete if` of this surface, and a verb
    // menu folded into the axes panel would be the same defect one step over.
    const axes = screen.getByRole("group", { name: "Datastream state axes" });
    expect(axes).not.toContainElement(screen.getByTestId("datastream-lifecycle-menu"));
  });
});
