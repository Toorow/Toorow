/**
 * Archiving a Report from the list it disappears from.
 *
 * WHY THIS FILE EXISTS. `archived_at` was a column, an index predicate and two
 * server refusals, and NOTHING wrote it — so the Reports list carried an
 * `archived` boolean that was permanently false and offered no gesture that
 * could make it true. The console half of that repair is what is asserted here:
 * a button, a confirmation, and a list that is re-read from the server.
 *
 * The three assertions that matter, and why each one is not decoration:
 *
 *   1. THE CONFIRMATION IS REAL — the archive is not issued until it is
 *      confirmed. A destructive act one click away is the defect; a dialog that
 *      fires the request as it opens is the same defect with a modal on top.
 *   2. THE LIST IS RE-READ, not spliced. The server decides what is live. A
 *      screen that removes a row it did not confirm gone is how a failed write
 *      comes to look like a success.
 *   3. A FAILURE IS SAID, and the row stays. Closing the dialog on error would
 *      leave the Report in place with no explanation — a button that does
 *      nothing.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ReportsCollection } from "../analyze-artifacts/Reports";
import { NotebooksCollection } from "../analyze-artifacts/Notebooks";

const PROJECT = "proj_EXAMPLE";

function report(id: string, label: string) {
  return {
    id,
    label,
    description: null,
    seed_origin: "explore",
    seed: null,
    current_version_id: `repv_${id}`,
    archived: false,
    archived_at: null,
    archived_by: null,
    created_by: "owner@example.com",
    created_at: "2026-08-17T09:00:00Z",
    updated_at: "2026-08-17T09:00:00Z",
    current_version_number: 1,
    query_spec_version_id: "qsv_EXAMPLE",
    presentation_absent: "No accepted presentation contract",
    run_count: 2,
  };
}

function collection(reports: unknown[]) {
  return {
    reports,
    seeds: [],
    presentation_contract: { available: false, missing: [] },
  };
}

function ok(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("does not archive until the confirmation is confirmed", async () => {
  const fetchMock = vi.fn((_url: string, _init?: RequestInit) =>
    Promise.resolve(ok(collection([report("rep_A", "Weekly traffic")]))),
  );
  vi.stubGlobal("fetch", fetchMock);
  render(<ReportsCollection projectId={PROJECT} />);

  fireEvent.click(await screen.findByTestId("archive-report-rep_A"));
  // The dialog is up, and NOTHING has been sent: still the one list read.
  expect(await screen.findByText("Archive this Report?")).toBeInTheDocument();
  expect(
    fetchMock.mock.calls.filter(([, init]) =>
      String((init as RequestInit | undefined)?.method ?? "GET") === "POST",
    ),
  ).toHaveLength(0);
});

it("names what survives, because nothing is deleted", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve(ok(collection([report("rep_A", "Weekly traffic")])))),
  );
  render(<ReportsCollection projectId={PROJECT} />);
  fireEvent.click(await screen.findByTestId("archive-report-rep_A"));

  // The Report being retired is NAMED — a confirmation that does not say which
  // object it is about is a confirmation of nothing. Scoped to the dialog on
  // purpose: the label is legitimately on screen twice (its row, and the
  // confirmation), and a bare query that matched both would be ambiguous rather
  // than wrong — which is exactly the question this asserts an answer to.
  const dialog = within(await screen.findByTestId("archive-report-confirm"));
  expect(dialog.getByText("Weekly traffic")).toBeInTheDocument();
  // The way out names what stays, next to a destructive verb.
  expect(screen.getByText("Keep it active")).toBeInTheDocument();
  // And the description says the evidence survives, because it does: the schema
  // refuses to delete a head whose versions and runs are evidence.
  expect(screen.getByText(/kept as evidence/i)).toBeInTheDocument();
});

it("archives on confirm and re-reads the list, which no longer carries it", async () => {
  let listReads = 0;
  const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
    if (String(init?.method ?? "GET") === "POST") {
      return Promise.resolve(
        ok({
          id: "rep_A",
          archived: true,
          archived_at: "2026-08-17T10:00:00Z",
          archived_by: "owner@example.com",
          already_archived: false,
        }),
      );
    }
    listReads += 1;
    // The SERVER decides what is live: after the archive it simply stops
    // sending the row, exactly as `list_reports` now filters it.
    return Promise.resolve(
      ok(
        collection(
          listReads === 1
            ? [report("rep_A", "Weekly traffic"), report("rep_B", "Spend by market")]
            : [report("rep_B", "Spend by market")],
        ),
      ),
    );
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<ReportsCollection projectId={PROJECT} />);

  fireEvent.click(await screen.findByTestId("archive-report-rep_A"));
  fireEvent.click(await screen.findByText("Archive it"));

  await waitFor(() =>
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/projects/${PROJECT}/analyze/reports/rep_A/archive`,
      expect.objectContaining({ method: "POST" }),
    ),
  );
  // The archived Report is gone from the list, and the other one is untouched.
  await waitFor(() =>
    expect(screen.queryByTestId("archive-report-rep_A")).not.toBeInTheDocument(),
  );
  expect(screen.getByTestId("archive-report-rep_B")).toBeInTheDocument();
  expect(listReads).toBeGreaterThan(1);
});

it("keeps the row and says why when the archive is refused", async () => {
  const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
    if (String(init?.method ?? "GET") === "POST") {
      // A 403, NOT A 422. Archiving is idempotent -- `_archive_head` guards on
      // `archived_at IS NULL`, re-reads on zero rows and returns 200 with
      // `already_archived: true` -- and the route raises only `ArtifactNotFound`
      // (404). The 422 this test used to simulate cannot come out of it, so the
      // assertion was right about a response that does not exist. A member who
      // is not an owner is the refusal that does.
      return Promise.resolve({
        ok: false,
        status: 403,
        json: async () => ({ code: "forbidden", message: "Only an owner may retire a Report" }),
        text: async () => "Only an owner may retire a Report",
      } as Response);
    }
    return Promise.resolve(ok(collection([report("rep_A", "Weekly traffic")])));
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<ReportsCollection projectId={PROJECT} />);

  fireEvent.click(await screen.findByTestId("archive-report-rep_A"));
  fireEvent.click(await screen.findByText("Archive it"));

  // The Report is still there — a refused write must never look like a done one.
  await waitFor(() =>
    expect(screen.getByTestId("archive-report-rep_A")).toBeInTheDocument(),
  );
  // And the dialog is still open, carrying the reason rather than vanishing.
  expect(screen.getByText("Archive this Report?")).toBeInTheDocument();
});

// ---------------------------------------------------------------------------
// 67-19 — THE SAME WRITER, ON THE OTHER LIST.
//
// `POST .../notebooks/{id}/archive` was served, `archiveNotebook` existed in
// the client, and the Notebooks list offered no gesture that could reach
// either: zero call sites. A verb nobody can reach is a verb the product does
// not have. The defect is a CLASS — one artifact list had the writer and its
// twin did not — so the repair is the same four properties on the twin, not a
// second design.
// ---------------------------------------------------------------------------

function notebook(id: string, label: string) {
  return {
    id,
    label,
    description: null,
    current_version_id: `nbv_${id}`,
    current_version_number: 1,
    run_count: 2,
    last_run_at: null,
    last_run_outcome: null,
    schedule: null,
    legacy_notebook_id: null,
  };
}

/** The three reads the Notebooks list makes, answered by URL. */
function notebookFetch(
  notebooks: () => unknown[],
  onPost?: (url: string) => Response,
) {
  return vi.fn((url: string, init?: RequestInit) => {
    if (String(init?.method ?? "GET") === "POST") {
      return Promise.resolve(
        onPost?.(url)
          ?? ok({ id: "nb_A", archived: true, already_archived: false }),
      );
    }
    if (url.endsWith("/notebooks/legacy")) return Promise.resolve(ok({ notebooks: [] }));
    if (url.endsWith("/reports")) {
      return Promise.resolve(ok({ reports: [], seeds: [], presentation_contract: { available: false, missing: [] } }));
    }
    return Promise.resolve(ok({ notebooks: notebooks() }));
  });
}

it("does not archive a Notebook until the confirmation is confirmed", async () => {
  const fetchMock = notebookFetch(() => [notebook("nb_A", "Monday review")]);
  vi.stubGlobal("fetch", fetchMock);
  render(<NotebooksCollection projectId={PROJECT} />);

  fireEvent.click(await screen.findByTestId("archive-notebook-nb_A"));
  expect(await screen.findByText("Archive this Notebook?")).toBeInTheDocument();
  expect(
    fetchMock.mock.calls.filter(([, init]) =>
      String((init as RequestInit | undefined)?.method ?? "GET") === "POST",
    ),
  ).toHaveLength(0);
});

it("says the schedule stops, because for a Notebook it does", async () => {
  // NOT the Report's sentence with a word swapped. A Notebook carries a
  // schedule and archiving stops it; leaving that out would make the reader
  // discover it from a dispatch that never came.
  vi.stubGlobal("fetch", notebookFetch(() => [notebook("nb_A", "Monday review")]));
  render(<NotebooksCollection projectId={PROJECT} />);
  fireEvent.click(await screen.findByTestId("archive-notebook-nb_A"));

  const dialog = within(await screen.findByTestId("archive-notebook-confirm"));
  expect(dialog.getByText("Monday review")).toBeInTheDocument();
  expect(screen.getByText(/schedule stops dispatching/i)).toBeInTheDocument();
  expect(screen.getByText(/kept as evidence/i)).toBeInTheDocument();
  expect(screen.getByText("Keep it active")).toBeInTheDocument();
});

it("archives a Notebook on confirm and re-reads the list", async () => {
  let listReads = 0;
  const fetchMock = notebookFetch(() => {
    listReads += 1;
    return listReads === 1
      ? [notebook("nb_A", "Monday review"), notebook("nb_B", "Spend digest")]
      : [notebook("nb_B", "Spend digest")];
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<NotebooksCollection projectId={PROJECT} />);

  fireEvent.click(await screen.findByTestId("archive-notebook-nb_A"));
  fireEvent.click(await screen.findByText("Archive it"));

  await waitFor(() =>
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/projects/${PROJECT}/analyze/notebooks/nb_A/archive`,
      expect.objectContaining({ method: "POST" }),
    ),
  );
  await waitFor(() =>
    expect(screen.queryByTestId("archive-notebook-nb_A")).not.toBeInTheDocument(),
  );
  expect(screen.getByTestId("archive-notebook-nb_B")).toBeInTheDocument();
});

it("keeps the Notebook and says why when the archive is refused", async () => {
  vi.stubGlobal(
    "fetch",
    notebookFetch(
      () => [notebook("nb_A", "Monday review")],
      () =>
        // 403, for the same reason as the Report above: the archive route is
        // idempotent and raises only `ArtifactNotFound`. Simulating a refusal
        // the server cannot send proves the dialog against a fiction.
        ({
          ok: false,
          status: 403,
          json: async () => ({ code: "forbidden", message: "Only an owner may retire a Notebook" }),
          text: async () => "Only an owner may retire a Notebook",
        }) as Response,
    ),
  );
  render(<NotebooksCollection projectId={PROJECT} />);

  fireEvent.click(await screen.findByTestId("archive-notebook-nb_A"));
  fireEvent.click(await screen.findByText("Archive it"));

  // A refused write must never look like a done one.
  await waitFor(() =>
    expect(screen.getByTestId("archive-notebook-nb_A")).toBeInTheDocument(),
  );
  expect(screen.getByText("Archive this Notebook?")).toBeInTheDocument();
});


// ---------------------------------------------------------------------------
// The way back the confirmation promises (2026-08-22).
//
// Both dialogs say "an archived Report/Notebook can be listed again". The route
// has served `?include_archived=true` since the archive landed and nothing in
// `ui/admin/src` ever sent it, so the sentence was true of the server and false
// of the product. A screen that names a gesture and offers no control for it is
// a button that does nothing, read backwards.
// ---------------------------------------------------------------------------

it("lists archived Reports again, which is what the confirmation promised", async () => {
  const urls: string[] = [];
  const archived = { ...report("rep_OLD", "Retired weekly"), archived: true };
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      urls.push(url);
      return Promise.resolve(
        ok(collection(url.includes("include_archived=true")
          ? [report("rep_A", "Weekly traffic"), archived]
          : [report("rep_A", "Weekly traffic")])),
      );
    }),
  );
  render(<ReportsCollection projectId={PROJECT} />);

  // The live list first, and it does not carry the retired one.
  await screen.findByTestId("archive-report-rep_A");
  expect(screen.queryByText("Retired weekly")).toBeNull();

  fireEvent.click(screen.getByTestId("reports-show-archived"));

  expect(await screen.findByText("Retired weekly")).toBeInTheDocument();
  expect(urls.some((url) => url.includes("include_archived=true"))).toBe(true);
  // An archived Report is SHOWN, never re-archived: the verb it needs is a
  // restore and no route serves one, so the control says what is true of it
  // instead of offering a gesture that would change nothing.
  expect(screen.getByTestId("archive-report-rep_OLD")).toBeDisabled();
  expect(screen.getByTestId("archive-report-rep_OLD")).toHaveTextContent("Archived");
});

it("lists archived Notebooks again too", async () => {
  const urls: string[] = [];
  const archived = { ...notebook("nb_OLD", "Retired digest"), archived: true };
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      urls.push(url);
      if (url.includes("/notebooks/legacy")) return Promise.resolve(ok({ notebooks: [] }));
      if (url.includes("/reports")) {
        return Promise.resolve(ok({ reports: [], seeds: [], presentation_contract: { available: false, missing: [] } }));
      }
      return Promise.resolve(ok({
        notebooks: url.includes("include_archived=true")
          ? [notebook("nb_A", "Monday review"), archived]
          : [notebook("nb_A", "Monday review")],
      }));
    }),
  );
  render(<NotebooksCollection projectId={PROJECT} />);

  await screen.findByTestId("archive-notebook-nb_A");
  expect(screen.queryByText("Retired digest")).toBeNull();

  fireEvent.click(screen.getByTestId("notebooks-show-archived"));

  expect(await screen.findByText("Retired digest")).toBeInTheDocument();
  expect(urls.some((url) => url.includes("include_archived=true"))).toBe(true);
  expect(screen.getByTestId("archive-notebook-nb_OLD")).toBeDisabled();
});
