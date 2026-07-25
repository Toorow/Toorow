/**
 * Vitest tests for NotebooksPanel (Story 6.5, AC6, AC8).
 *
 * Coverage (AC8 minimum):
 *   - renders panel heading
 *   - lists notebooks (title, report_ref, window_rule, last_run_status)
 *   - shows last run status badge (Success / Failed / —)
 *   - "Run" button calls POST /api/notebooks/{id}/run
 *   - empty state when no notebooks
 *   - error state on fetch failure
 *
 * English product copy assertions (v3 restyle).
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@mui/material";
import { adminTheme } from "../theme";
import NotebooksPanel from "../NotebooksPanel";

function renderWithTheme(ui: React.ReactElement) {
  return render(<ThemeProvider theme={adminTheme}>{ui}</ThemeProvider>);
}

afterEach(() => {
  vi.restoreAllMocks();
});

interface Notebook {
  id: string;
  title: string;
  report_ref: string;
  window_rule: string;
  created_at: string;
  last_run_at: string | null;
  last_run_status: "success" | "error" | null;
}

function makeNotebook(overrides: Partial<Notebook> = {}): Notebook {
  return {
    id: "nb_TEST123",
    title: "Analyse SEO hebdo",
    report_ref: "gsc/position_movements",
    window_rule: "last_7d",
    created_at: "2026-07-12T12:00:00Z",
    last_run_at: "2026-07-12T12:05:00Z",
    last_run_status: "success",
    ...overrides,
  };
}

function mockFetchNotebooks(notebooks: Notebook[]) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => notebooks,
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------

describe("NotebooksPanel — render", () => {
  it("renders the panel heading 'Notebooks'", async () => {
    mockFetchNotebooks([]);
    renderWithTheme(<NotebooksPanel />);
    await waitFor(() => {
      expect(screen.getByText("Notebooks")).toBeTruthy();
    });
  });

  it("shows empty state message when no notebooks", async () => {
    mockFetchNotebooks([]);
    renderWithTheme(<NotebooksPanel />);
    await waitFor(() => {
      expect(screen.getByText(/No notebooks saved yet/)).toBeTruthy();
    });
  });

  it("renders error state on fetch failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 500, json: async () => ({}) }),
    );
    renderWithTheme(<NotebooksPanel />);
    await waitFor(() => {
      expect(screen.getByText(/Couldn't load notebooks/)).toBeTruthy();
    });
  });
});

// ---------------------------------------------------------------------------
// List display
// ---------------------------------------------------------------------------

describe("NotebooksPanel — list", () => {
  it("displays notebook title, report_ref, window_rule", async () => {
    mockFetchNotebooks([makeNotebook()]);
    renderWithTheme(<NotebooksPanel />);
    await waitFor(() => {
      expect(screen.getByText("Analyse SEO hebdo")).toBeTruthy();
      expect(screen.getByText("gsc/position_movements")).toBeTruthy();
      expect(screen.getByText("last_7d")).toBeTruthy();
    });
  });

  it("shows success badge for last_run_status='success'", async () => {
    mockFetchNotebooks([makeNotebook({ last_run_status: "success" })]);
    renderWithTheme(<NotebooksPanel />);
    await waitFor(() => {
      expect(screen.getByText("Success")).toBeTruthy();
    });
  });

  it("shows error badge for last_run_status='error'", async () => {
    mockFetchNotebooks([makeNotebook({ last_run_status: "error" })]);
    renderWithTheme(<NotebooksPanel />);
    await waitFor(() => {
      expect(screen.getByText("Failed")).toBeTruthy();
    });
  });

  it("shows '—' when last_run_status is null", async () => {
    mockFetchNotebooks([makeNotebook({ last_run_status: null, last_run_at: null })]);
    renderWithTheme(<NotebooksPanel />);
    await waitFor(() => {
      // Should have at least one '—' chip (last_run badge)
      const dashes = screen.getAllByText("—");
      expect(dashes.length).toBeGreaterThan(0);
    });
  });

  it("renders Run button per notebook row", async () => {
    mockFetchNotebooks([makeNotebook()]);
    renderWithTheme(<NotebooksPanel />);
    await waitFor(() => {
      expect(screen.getByText("Run")).toBeTruthy();
    });
  });
});

// ---------------------------------------------------------------------------
// Story 6.6 (AC2): Schedule toggle
// ---------------------------------------------------------------------------

describe("NotebooksPanel — schedule toggle (Story 6.6)", () => {
  it("shows 'Scheduled' badge for scheduled notebooks", async () => {
    mockFetchNotebooks([makeNotebook({ scheduled: true, schedule_rule: "nightly" } as unknown as Partial<Notebook>)]);
    renderWithTheme(<NotebooksPanel />);
    await waitFor(() => {
      expect(screen.getByText("Scheduled")).toBeTruthy();
    });
  });

  it("does NOT show 'Scheduled' badge for unscheduled notebooks", async () => {
    mockFetchNotebooks([makeNotebook({ scheduled: false, schedule_rule: null } as unknown as Partial<Notebook>)]);
    renderWithTheme(<NotebooksPanel />);
    await waitFor(() => {
      const badges = screen.queryAllByText("Scheduled");
      expect(badges.length).toBe(0);
    });
  });

  it("shows 'Recurring (nightly)' checkbox label", async () => {
    mockFetchNotebooks([makeNotebook()]);
    renderWithTheme(<NotebooksPanel />);
    await waitFor(() => {
      expect(screen.getByText("Recurring (nightly)")).toBeTruthy();
    });
  });

  it("calls PATCH /schedule when schedule checkbox is toggled ON", async () => {
    const nb = makeNotebook({ scheduled: false } as unknown as Partial<Notebook>);
    const fetchMock = vi
      .fn()
      // Initial list fetch
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => [nb],
      })
      // PATCH /schedule
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({
          ...nb,
          scheduled: true,
          schedule_rule: "nightly",
        }),
      });

    vi.stubGlobal("fetch", fetchMock);

    renderWithTheme(<NotebooksPanel />);

    const checkbox = await screen.findByLabelText(`Recurring (nightly) for ${nb.title}`);
    await userEvent.click(checkbox);

    await waitFor(() => {
      const calls = fetchMock.mock.calls;
      const scheduleCall = calls.find(
        (args: unknown[]) =>
          typeof args[0] === "string" &&
          (args[0] as string).includes("/schedule") &&
          (args[1] as RequestInit | undefined)?.method === "PATCH",
      );
      expect(scheduleCall).toBeTruthy();
    });
  });
});

// ---------------------------------------------------------------------------
// Run action
// ---------------------------------------------------------------------------

describe("NotebooksPanel — Run button", () => {
  it("calls POST /api/notebooks/{id}/run when Run is clicked", async () => {
    const nb = makeNotebook();
    // First fetch: list
    // Second fetch: POST run
    // Third fetch: refresh list
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => [nb],
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ run_id: "nbrun_XYZ", summary: "Clics: 1 000", pull_ids: [] }),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => [{ ...nb, last_run_at: "2026-07-12T12:10:00Z", last_run_status: "success" }],
      });

    vi.stubGlobal("fetch", fetchMock);

    renderWithTheme(<NotebooksPanel />);

    const btn = await screen.findByText("Run");
    await userEvent.click(btn);

    await waitFor(() => {
      // The POST to /run should have been called
      const calls = fetchMock.mock.calls;
      const runCall = calls.find(
        (args: unknown[]) =>
          typeof args[0] === "string" &&
          (args[0] as string).includes("/run") &&
          (args[1] as RequestInit | undefined)?.method === "POST",
      );
      expect(runCall).toBeTruthy();
    });
  });

  it("shows run confirmation message after successful run", async () => {
    const nb = makeNotebook();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => [nb] })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ run_id: "nbrun_XYZ", summary: "ok", pull_ids: [] }),
      })
      .mockResolvedValueOnce({ ok: true, json: async () => [nb] });

    vi.stubGlobal("fetch", fetchMock);

    renderWithTheme(<NotebooksPanel />);
    const btn = await screen.findByText("Run");
    await userEvent.click(btn);

    await waitFor(() => {
      expect(screen.getByText(/Run complete/)).toBeTruthy();
    });
  });
});
