/**
 * The bounded masked sample — the thing `Data` exists for.
 *
 * `datastream-workbench-and-wizard.md:75` contracts the tab as "stage selector …
 * and bounded masked samples tied to exact run/plan/mapping versions". It had
 * neither: `sample_state` was the literal `"unavailable"`, written
 * unconditionally in `read_tab`, so the tab rendered an apology and no data
 * could ever have changed that. The endpoint it needed had existed, unused, for
 * one epic.
 */
import { render, screen } from "@testing-library/react";
import DatastreamSample from "../datastreams/workbench/DatastreamSample";

const PROJECT_ID = "proj_EXAMPLE";
const DATASTREAM_ID = "ds_EXAMPLE";
const TODAY = new Date("2026-08-02T00:00:00Z");

function answer(body: unknown, status = 200) {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  }));
}

afterEach(() => vi.unstubAllGlobals());

function mount() {
  return render(
    <DatastreamSample
      projectId={PROJECT_ID}
      datastreamId={DATASTREAM_ID}
      stage="published"
      watermark="2026-08-02"
      executionId="exec_123"
      today={TODAY}
    />,
  );
}

it("draws the rows the server returned, with the masked column named", async () => {
  answer({
    project_id: PROJECT_ID,
    datastream_id: DATASTREAM_ID,
    served_stage: "published",
    masked_fields: ["query"],
    masked_value_count: 12,
    days: [{ date: "2026-08-02", sampled_row_count: 1, field_count: 3,
             rows: [{ date: "2026-08-02", page: "/pricing", query: "***" }] }],
  });
  mount();

  expect(await screen.findByText("/pricing")).toBeInTheDocument();
  expect(screen.getByText("2026-08-02", { selector: "span" })).toBeInTheDocument();
  // A masked column that does not say it is masked reads as the real value.
  expect(screen.getAllByText("masked").length).toBeGreaterThan(0);
});

it("REFUSES rather than substitutes when the server says no", async () => {
  answer({ code: "ambiguous_materialization", message: "No Datastream-scoped sample materialisation is available for this connector." }, 409);
  mount();

  // The endpoint's own words, verbatim — the ambiguity rule lives there, and a
  // paraphrase here would drift from it.
  expect(await screen.findByText(/No Datastream-scoped sample materialisation/)).toBeInTheDocument();
  expect(screen.getByText(/No rows have been substituted/)).toBeInTheDocument();
});

it("refuses a payload that does not echo the Project and Datastream asked for", async () => {
  answer({
    project_id: "proj_SOMEONE_ELSE",
    datastream_id: DATASTREAM_ID,
    days: [{ date: "2026-08-02", rows: [{ page: "/leaked" }] }],
  });
  mount();

  expect(await screen.findByText(/does not match the requested Project or Datastream/)).toBeInTheDocument();
  expect(screen.queryByText("/leaked")).not.toBeInTheDocument();
});

it("says when the server served a different stage than the one asked for", async () => {
  answer({
    project_id: PROJECT_ID,
    datastream_id: DATASTREAM_ID,
    served_stage: "processed",
    stage_note: "Published has no materialisation of its own.",
    days: [],
  });
  mount();

  // A silent substitution would label processed rows as published.
  expect(await screen.findByText("Served from processed")).toBeInTheDocument();
});

it("reads a BOUNDED window, ending at the watermark", async () => {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true, status: 200,
    json: async () => ({ project_id: PROJECT_ID, datastream_id: DATASTREAM_ID, days: [] }),
  });
  vi.stubGlobal("fetch", fetchMock);
  mount();

  await screen.findByText(/No eligible row in this window/);
  const url = String(fetchMock.mock.calls[0][0]);
  // Seven days ending at the watermark: this surface shows evidence, and an
  // unbounded read of a warehouse is an export.
  expect(url).toContain("date_from=2026-07-27");
  expect(url).toContain("date_to=2026-08-02");
  expect(url).toContain("limit=5");
});
