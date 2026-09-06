/**
 * The last file that arrived, on the `Data` tab of a file source — lot B1.
 *
 * Amendment 7 of the 2026-08-11 review took the connector pull axis away from a
 * `managed_feed`, and rightly: a pushed source has no report profile, no
 * connector relation and no collection window. Nothing was put in its place, so
 * 4 of the 6 live Datastreams — all four file sources — had no reading of their
 * own data on this tab at all. What these tests hold is the set of ways that
 * replacement can quietly become the defect it replaced:
 *
 *   * a `connector_pull` growing a panel about a file it never receives;
 *   * « no file has arrived yet » and « the last file could not be read »
 *     rendered in one tone, or one of them rendered as an empty grid;
 *   * an empty state that does not name the door a file arrives by;
 *   * a masked column shown, or a row substituted for a read that failed.
 *
 * `fetch` is stubbed rather than `apiFetch`: the seam guard is what proves the
 * bearer is attached, and stubbing one level lower would hide it.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import WorkbenchDataPage from "../datastreams/workbench/pages/WorkbenchDataPage";
import DatastreamLandedFile from "../datastreams/workbench/DatastreamLandedFile";
import type { WorkbenchTabPayload } from "../datastreams/workbench/workbenchTypes";

const TAB_PAYLOAD: WorkbenchTabPayload = {
  schema: "datastream_workbench.data.v1",
  tab: "data",
  project_id: "proj_EXAMPLE",
  datastream_id: "ds_EXAMPLE",
  evidence: {
    state: "unavailable",
    sample_state: "unavailable",
    availability: {
      collected: "unavailable", mapped: "unavailable",
      processed: "unavailable", published: "unavailable",
    },
    availability_reason: {},
    stages: [],
  },
};

/** The envelope the route sends. Every field the component reads is here, so a
 *  test never proves a shape the server does not answer. */
function payload(overrides: Record<string, unknown> = {}) {
  return {
    project_id: "proj_EXAMPLE",
    datastream_id: "ds_EXAMPLE",
    mode: "managed_feed",
    doors: { channels: [], upload_available: true },
    arrival: null,
    import: null,
    relation: null,
    columns: [],
    rows: null,
    row_count: null,
    truncated: false,
    masked_fields: [],
    reason: null,
    message: null,
    ...overrides,
  };
}

function stub(body: unknown, ok = true, status = 200) {
  const spy = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (!url.includes("/landed-file")) throw new Error(`unexpected ${url}`);
    return { ok, status, json: async () => body } as Response;
  });
  vi.stubGlobal("fetch", spy);
  return spy;
}

function renderPanel() {
  return render(
    <DatastreamLandedFile projectId="proj_EXAMPLE" datastreamId="ds_EXAMPLE" />,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("DatastreamLandedFile", () => {
  it("reads the file that landed and renders its rows", async () => {
    const spy = stub(
      payload({
        import: {
          ledger_id: "mfl_1", filename: "catalogue.csv", outcome: "published",
          row_count: 2, rejected_row_count: 0, imported_at: "2026-08-10T09:00:00+00:00",
        },
        relation: "main.managed_feed_ds_EXAMPLE",
        columns: ["titre", "contact", "vues"],
        rows: [
          { titre: "a", contact: "[MASKED]", vues: 10 },
          { titre: "b", contact: "[MASKED]", vues: 20 },
        ],
        row_count: 2,
        masked_fields: ["contact"],
      }),
    );
    renderPanel();

    await waitFor(() => expect(spy).toHaveBeenCalled());
    expect(String(spy.mock.calls[0][0])).toContain(
      "/api/projects/proj_EXAMPLE/datastreams/ds_EXAMPLE/workbench/landed-file",
    );
    await waitFor(() => expect(screen.getByText("catalogue.csv", { exact: false })).toBeInTheDocument());
    expect(screen.getByText("a")).toBeInTheDocument();
    expect(screen.getByText("b")).toBeInTheDocument();
    // The masking is the server's and is NAMED, so a person can tell a hidden
    // value from a value the file did not carry.
    expect(screen.getByText(/Masked before it left the server/)).toBeInTheDocument();
  });

  it("says no file has arrived, and names the door it arrives by", async () => {
    stub(
      payload({
        doors: { channels: ["email"], upload_available: true },
        reason: "no_file_yet",
        message: "No file has arrived on this Datastream yet.",
      }),
    );
    renderPanel();

    await waitFor(() => expect(screen.getByText("No file has arrived yet")).toBeInTheDocument());
    expect(screen.getByText(/inbound address/)).toBeInTheDocument();
    // Never a grid, never a zero.
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByText("0")).toBeNull();
  });

  it("names the upload when the Datastream declares no inbound channel", async () => {
    stub(payload({ reason: "no_file_yet", message: "No file has arrived on this Datastream yet." }));
    renderPanel();

    await waitFor(() =>
      expect(screen.getByText(/declares no inbound channel/)).toBeInTheDocument(),
    );
  });

  it("does not say « nothing arrived » when the last file could not be read", async () => {
    stub(
      payload({
        import: { ledger_id: "mfl_2", outcome: "failed", row_count: null, error_code: "file_source_drift" },
        reason: "import_failed",
        message: "The last file could not be imported, so no row of it landed.",
      }),
    );
    renderPanel();

    await waitFor(() =>
      expect(screen.getByText("The last file could not be read")).toBeInTheDocument(),
    );
    expect(screen.getByText(/file_source_drift/)).toBeInTheDocument();
    expect(screen.queryByText("No file has arrived yet")).toBeNull();
  });

  it("refuses an envelope that names another Datastream rather than rendering it", async () => {
    stub(payload({ datastream_id: "ds_OTHER" }));
    renderPanel();

    await waitFor(() =>
      expect(screen.getByText(/does not name the Project or Datastream/)).toBeInTheDocument(),
    );
  });

  it("shows the route's own sentence on a refusal, and substitutes no row", async () => {
    stub({ code: "unavailable", message: "Datastream evidence is unavailable" }, false, 503);
    renderPanel();

    await waitFor(() =>
      expect(screen.getByText(/Datastream evidence is unavailable/)).toBeInTheDocument(),
    );
    expect(screen.queryByRole("table")).toBeNull();
  });
});

/**
 * WHAT ARRIVED, BY DAY — amendment of 2026-08-18.
 *
 * A file source had no dated reading at all on this tab: the pull grid was taken
 * from it, rightly, and nothing put a day axis in its place. The ways THIS can go
 * wrong are the tests:
 *
 *   * inventing a history out of one file — the envelope carries the last arrival
 *     and the last import that wrote, and nothing else, so a table titled « by
 *     day » must say what it does not hold;
 *   * a blank panel when no instant is on the wire, instead of an absence that
 *     names who does hold the answer;
 *   * a `0` where `row_count` is `null` — the same rule the pull grid keeps on
 *     its own volumes.
 */
describe("The day reading of a file source", () => {
  it("dates every arrival the envelope carries, and never more than them", async () => {
    stub(
      payload({
        arrival: {
          raw_import_id: "raw_1", filename: "catalogue.csv", state: "IMPORTED",
          arrived_at: "2026-08-09T21:40:00+00:00",
        },
        import: {
          ledger_id: "mfl_1", filename: "catalogue.csv", outcome: "published",
          row_count: 519, imported_at: "2026-08-10T09:00:00+00:00",
          observed_at: "2026-08-10T08:55:00+00:00",
        },
        relation: "main.managed_feed_ds_EXAMPLE",
        columns: ["titre"],
        rows: [{ titre: "a" }],
        row_count: 1,
      }),
    );
    renderPanel();

    const reading = within(await screen.findByTestId("arrival-by-day"));
    // The arrival's day and the import's day are two different days here, and
    // both are read: a file that reaches a Datastream at 23:40 and lands the
    // next morning is the ordinary case, not an edge one.
    expect(reading.getByText("2026-08-09")).toBeInTheDocument();
    expect(reading.getAllByText("2026-08-10").length).toBeGreaterThan(0);
    expect(reading.getByText("catalogue.csv arrived")).toBeInTheDocument();
    expect(reading.getByText("Import published")).toBeInTheDocument();
    expect(reading.getByText("519")).toBeInTheDocument();
    // AND IT SAYS WHAT IT IS NOT. One file is not a history, and the collection
    // that holds every arrival is named rather than left to be guessed.
    expect(screen.getByText(/covers the last file only/)).toBeInTheDocument();
    expect(screen.getByText(/Imports holds every one of them/)).toBeInTheDocument();
  });

  it("says Not measured rather than 0 for a row count the import never wrote", async () => {
    stub(
      payload({
        import: {
          ledger_id: "mfl_2", outcome: "opened", row_count: null,
          imported_at: "2026-08-10T09:00:00+00:00",
        },
        reason: "import_not_landed",
        message: "The last import is still open: it has not written its rows yet.",
      }),
    );
    renderPanel();

    const reading = within(await screen.findByTestId("arrival-by-day"));
    expect(reading.getByText("Import opened")).toBeInTheDocument();
    expect(reading.getByText("Not measured")).toBeInTheDocument();
    expect(reading.queryByText("0")).toBeNull();
  });

  it("names the absence and its owner when nothing on the answer is dated", async () => {
    // NEVER A BLANK PANEL. No arrival, no import, so no instant — and the reading
    // says which question cannot be answered here and where it is answered.
    stub(payload({ reason: "no_file_yet", message: "No file has arrived on this Datastream yet." }));
    renderPanel();

    const reading = within(await screen.findByTestId("arrival-by-day"));
    expect(reading.getByText("No arrival of this Datastream is dated")).toBeInTheDocument();
    expect(reading.getByText(/Data › Imports/)).toBeInTheDocument();
    expect(reading.queryByRole("table")).toBeNull();
  });
});

/**
 * THE PULL AXIS WAS TAKEN AWAY; A SENTENCE TOOK ITS PLACE — amendment of
 * 2026-08-18, and an `Incomplete if` of this surface: « a block is withdrawn from
 * its `Data` tab without one sentence saying what is absent and where the same
 * question is answered instead ».
 */
describe("A pushed source is told, not left with a gap", () => {
  function mountMode(mode: string) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => ({
        ok: true,
        status: 200,
        json: async () =>
          String(input).includes("/landed-file")
            ? payload({ reason: "no_file_yet", message: "No file has arrived on this Datastream yet." })
            : { project_id: "proj_EXAMPLE", datastream_id: "ds_EXAMPLE", days: [], columns: [] },
      } as Response)),
    );
    return render(
      <WorkbenchDataPage
        payload={TAB_PAYLOAD}
        projectId="proj_EXAMPLE"
        datastreamId="ds_EXAMPLE"
        mode={mode}
      />,
    );
  }

  it("tells a managed_feed why it has no day grid, and where its days are read", async () => {
    mountMode("managed_feed");

    const said = within(await screen.findByTestId("pushed-source-no-day-axis"));
    expect(said.getByText(/pushed to it as a file/)).toBeInTheDocument();
    expect(said.getByText(/The last file that arrived/)).toBeInTheDocument();
    // Re-collection is absent AND says why — never a disabled control, never
    // silence.
    expect(said.getByText(/No re-collection is offered/)).toBeInTheDocument();
    // And the connector's own vocabulary stays out of it.
    expect(said.queryByText(/report profile/)).toBeNull();
  });

  it("tells an external_bq where its columns and its rows live instead", async () => {
    mountMode("external_bq");

    const said = within(await screen.findByTestId("pushed-source-no-day-axis"));
    expect(said.getByText(/relation it does not own/)).toBeInTheDocument();
    expect(said.getByText(/on Mapping/)).toBeInTheDocument();
    expect(said.getByText(/No re-collection is offered/)).toBeInTheDocument();
    // It receives no file, so it is never offered the file panel.
    expect(screen.queryByTestId("landed-file")).toBeNull();
  });

  it("leaves a connector pull its day grid, untouched", async () => {
    mountMode("connector_pull");

    await waitFor(() => expect(screen.getByTestId("daily-breakdown-fields")).toBeInTheDocument());
    expect(screen.queryByTestId("pushed-source-no-day-axis")).toBeNull();
  });
});

describe("WorkbenchDataPage mounts it for a pushed source only", () => {
  it("asks for the landed file when the mode is managed_feed", async () => {
    const spy = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      return {
        ok: true,
        status: 200,
        json: async () =>
          url.includes("/landed-file")
            ? payload({ reason: "no_file_yet", message: "No file has arrived on this Datastream yet." })
            : { project_id: "proj_EXAMPLE", datastream_id: "ds_EXAMPLE", days: [], columns: [] },
      } as Response;
    });
    vi.stubGlobal("fetch", spy);

    render(
      <WorkbenchDataPage
        payload={TAB_PAYLOAD}
        projectId="proj_EXAMPLE"
        datastreamId="ds_EXAMPLE"
        mode="managed_feed"
      />,
    );

    await waitFor(() =>
      expect(spy.mock.calls.some((call) => String(call[0]).includes("/landed-file"))).toBe(true),
    );
    await waitFor(() => expect(screen.getByTestId("landed-file")).toBeInTheDocument());
  });

  it("does not ask for one on a connector pull", async () => {
    const spy = vi.fn(async (_input: RequestInfo | URL) => ({
      ok: true,
      status: 200,
      json: async () => ({ project_id: "proj_EXAMPLE", datastream_id: "ds_EXAMPLE", days: [], columns: [] }),
    } as Response));
    vi.stubGlobal("fetch", spy);

    render(
      <WorkbenchDataPage
        payload={TAB_PAYLOAD}
        projectId="proj_EXAMPLE"
        datastreamId="ds_EXAMPLE"
        mode="connector_pull"
      />,
    );

    await waitFor(() => expect(spy).toHaveBeenCalled());
    expect(spy.mock.calls.some((call) => String(call[0]).includes("/landed-file"))).toBe(false);
    expect(screen.queryByTestId("landed-file")).toBeNull();
  });
});
