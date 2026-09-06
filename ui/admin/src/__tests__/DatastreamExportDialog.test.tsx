/**
 * The export control, which `SCREEN-FUNCTION-MATRIX.md` Lot 1 names as half of
 * the Data tab's primary action and which existed nowhere in `ui/admin/src` —
 * four review lenses grepped for it independently and found zero matches while
 * the server route was real.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import DatastreamExportDialog from "../datastreams/workbench/DatastreamExportDialog";

const PROJECT_ID = "proj_EXAMPLE";
const DATASTREAM_ID = "ds_EXAMPLE";
const TODAY = new Date("2026-08-02T00:00:00Z");

const COLUMNS = {
  project_id: PROJECT_ID,
  datastream_id: DATASTREAM_ID,
  columns: ["date", "page", "email", "clicks"],
  masked_columns: ["email"],
};

function stubFetch(handlers: { columns?: unknown; columnsOk?: boolean; export?: unknown; exportOk?: boolean; exportStatus?: number }) {
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    if (String(url).includes("/export/columns")) {
      return Promise.resolve({
        ok: handlers.columnsOk ?? true,
        status: handlers.columnsOk === false ? 500 : 200,
        json: async () => handlers.columns ?? COLUMNS,
      });
    }
    return Promise.resolve({
      ok: handlers.exportOk ?? true,
      status: handlers.exportStatus ?? 200,
      json: async () => handlers.export ?? {},
      blob: async () => new Blob(["date,clicks\n"], { type: "text/csv" }),
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => vi.unstubAllGlobals());

async function openDialog() {
  render(
    <DatastreamExportDialog projectId={PROJECT_ID} datastreamId={DATASTREAM_ID} today={TODAY} />,
  );
  await userEvent.click(screen.getByRole("button", { name: "Export…" }));
}

it("offers the real columns, and says which will arrive masked", async () => {
  stubFetch({});
  await openDialog();

  expect(await screen.findByLabelText(/^date$/)).toBeInTheDocument();
  // The masking is stated BEFORE the download, not discovered in the file.
  expect(screen.getByText("masked")).toBeInTheDocument();
});

it("sends the chosen period and the chosen columns, and nothing else", async () => {
  const fetchMock = stubFetch({});
  await openDialog();
  await screen.findByLabelText(/^date$/);

  await userEvent.click(screen.getByLabelText(/^clicks$/));
  await userEvent.click(screen.getByRole("button", { name: "Export CSV" }));

  await waitFor(() => {
    const url = fetchMock.mock.calls.map((c) => String(c[0])).find((u) => u.includes("/export?"));
    expect(url).toBeTruthy();
    expect(url).toContain("columns=clicks");
    // Seven days back from the injected today, so the default is a starting
    // point rather than a year quietly exported.
    expect(url).toContain("date_from=2026-07-27");
    expect(url).toContain("date_to=2026-08-02");
  });
});

it("shows the server's refusal VERBATIM rather than a generic failure", async () => {
  // The ceiling is the point: a truncated export is a file a person treats as
  // complete, so the server refuses and the reason must reach them intact.
  stubFetch({
    exportOk: false,
    exportStatus: 400,
    export: { code: "export_too_large", message: "this range holds more than 50,000 rows; narrow the period or the columns." },
  });
  await openDialog();
  await screen.findByLabelText(/^date$/);
  await userEvent.click(screen.getByRole("button", { name: "Export CSV" }));

  expect(await screen.findByText(/more than 50,000 rows/)).toBeInTheDocument();
});

it("stays usable when the column list cannot be read", async () => {
  stubFetch({ columnsOk: false, columns: { message: "the consolidated mart is unavailable" } });
  await openDialog();

  expect(await screen.findByText(/consolidated mart is unavailable/)).toBeInTheDocument();
  // Losing the column list must not lose the export: every column is still a
  // legitimate answer.
  expect(screen.getByRole("button", { name: "Export CSV" })).toBeEnabled();
});

it("refuses a column list that does not echo the Datastream asked for", async () => {
  stubFetch({ columns: { ...COLUMNS, datastream_id: "ds_SOMEONE_ELSE" } });
  await openDialog();

  expect(await screen.findByText(/does not match the requested Project or Datastream/)).toBeInTheDocument();
  expect(screen.queryByLabelText(/^email$/)).not.toBeInTheDocument();
});
