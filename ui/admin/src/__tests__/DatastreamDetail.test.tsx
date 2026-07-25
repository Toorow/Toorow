/**
 * Vitest tests for DatastreamDetail tabs (Extraits, Mapping, Réglages).
 *
 * Tests added for Epic 8 review findings:
 *   - Tabs are rendered: "Extraits", "Mapping", "Réglages"
 *   - Extraits tab is active by default and shows calendar
 *   - Clicking Mapping tab fetches and shows mapping table
 *   - Clicking Réglages tab shows settings form
 *   - Réglages: saving PATCH /api/datastreams/{id} with updated fields
 *
 * UX-DR10: French assertions.
 */

import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@mui/material";
import { adminTheme } from "../theme";
import DatastreamDetail from "../datastreams/DatastreamDetail";
import type { Datastream } from "../datastreams/types";

function renderWithTheme(ui: React.ReactElement) {
  return render(<ThemeProvider theme={adminTheme}>{ui}</ThemeProvider>);
}

const DATASTREAM: Datastream = {
  id: "ds_001",
  project_id: "proj_a",
  name: "GA Standard",
  module_name: "google-analytics",
  connection_ref_id: "conn_001",
  report_profile_id: null,
  enabled: true,
  schedule_mode: "nightly",
  refetch_days: 3,
  date_window_days: 30,
  config: null,
  created_by: "system",
  created_at: "2026-07-01T00:00:00+00:00",
  updated_at: "2026-07-11T00:00:00+00:00",
};

const LEDGER_DATA = {
  ledger: [
    {
      date: "2026-07-10",
      status: "ok",
      row_count: 150,
      expected_rows: 150,
      completeness_ratio: 1.0,
      pull_id: "pull_001",
      loaded_at: "2026-07-10T09:00:00+00:00",
    },
  ],
};

const MAPPINGS = [
  { datastream_id: "ds_001", source_field: "clicks", target_field: "clicks" },
  { datastream_id: "ds_001", source_field: "impressions", target_field: null },
];

afterEach(() => {
  vi.restoreAllMocks();
});

function mockFetchImpl() {
  vi.spyOn(globalThis, "fetch").mockImplementation((url: RequestInfo | URL) => {
    const urlStr = String(url);
    if (urlStr.includes("/ledger")) {
      return Promise.resolve(
        new Response(JSON.stringify(LEDGER_DATA), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      );
    }
    if (urlStr.includes("/api/datamodel/mappings")) {
      return Promise.resolve(
        new Response(JSON.stringify(MAPPINGS), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      );
    }
    if (urlStr.includes("/api/datastreams/ds_001") && !urlStr.includes("ledger")) {
      return Promise.resolve(
        new Response(JSON.stringify({ ...DATASTREAM }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      );
    }
    return Promise.resolve(new Response(null, { status: 404 }));
  });
}

// ---------------------------------------------------------------------------
// Tab navigation
// ---------------------------------------------------------------------------

describe("DatastreamDetail — tabs", () => {
  test("renders three tabs: Extraits, Mapping, Réglages", async () => {
    mockFetchImpl();

    renderWithTheme(
      <DatastreamDetail
        datastream={DATASTREAM}
        projectId="proj_a"
        apiBase=""
        apiToken="test"
        onBack={() => {}}
      />
    );

    await waitFor(() => {
      expect(screen.getByTestId("tab-extraits")).toBeInTheDocument();
    });

    expect(screen.getByTestId("tab-mapping")).toBeInTheDocument();
    expect(screen.getByTestId("tab-reglages")).toBeInTheDocument();
  });

  test("Extraits tab is shown by default (calendar visible)", async () => {
    mockFetchImpl();

    renderWithTheme(
      <DatastreamDetail
        datastream={DATASTREAM}
        projectId="proj_a"
        apiBase=""
        apiToken="test"
        onBack={() => {}}
      />
    );

    await waitFor(() => {
      expect(screen.getByTestId("panel-extraits")).toBeInTheDocument();
    });
    expect(screen.getByText(/Calendrier d'extraction/i)).toBeInTheDocument();
  });

  test("clicking Mapping tab fetches and renders mapping table", async () => {
    const user = userEvent.setup();
    mockFetchImpl();

    renderWithTheme(
      <DatastreamDetail
        datastream={DATASTREAM}
        projectId="proj_a"
        apiBase=""
        apiToken="test"
        onBack={() => {}}
      />
    );

    await waitFor(() => {
      expect(screen.getByTestId("tab-mapping")).toBeInTheDocument();
    });

    await user.click(screen.getByTestId("tab-mapping"));

    await waitFor(() => {
      expect(screen.getByTestId("panel-mapping")).toBeInTheDocument();
    });

    await waitFor(() => {
      expect(screen.getByTestId("mapping-table")).toBeInTheDocument();
    });

    // Source field "clicks" should appear
    expect(screen.getByTestId("mapping-row-clicks")).toBeInTheDocument();
    // "impressions" is unmapped
    expect(screen.getByTestId("mapping-row-impressions")).toBeInTheDocument();
  });

  test("clicking Réglages tab shows settings form", async () => {
    const user = userEvent.setup();
    mockFetchImpl();

    renderWithTheme(
      <DatastreamDetail
        datastream={DATASTREAM}
        projectId="proj_a"
        apiBase=""
        apiToken="test"
        onBack={() => {}}
      />
    );

    await waitFor(() => {
      expect(screen.getByTestId("tab-reglages")).toBeInTheDocument();
    });

    await user.click(screen.getByTestId("tab-reglages"));

    await waitFor(() => {
      expect(screen.getByTestId("panel-reglages")).toBeInTheDocument();
    });

    // Settings form labels are visible
    expect(screen.getByText(/Mode de planification/i)).toBeInTheDocument();
    expect(screen.getByText(/Rattrapage \(jours en arrière\)/i)).toBeInTheDocument();
    expect(screen.getByText(/Fenêtre de données \(jours\)/i)).toBeInTheDocument();
  });

  test("Réglages tab: save button calls PATCH with updated values", async () => {
    const user = userEvent.setup();
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation((url: RequestInfo | URL, opts?: RequestInit) => {
      const urlStr = String(url);
      if (urlStr.includes("/ledger")) {
        return Promise.resolve(
          new Response(JSON.stringify(LEDGER_DATA), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          })
        );
      }
      if (urlStr.includes("/api/datastreams/ds_001") && opts?.method === "PATCH") {
        return Promise.resolve(
          new Response(JSON.stringify({ ...DATASTREAM, refetch_days: 5 }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          })
        );
      }
      return Promise.resolve(new Response(null, { status: 200 }));
    });

    renderWithTheme(
      <DatastreamDetail
        datastream={DATASTREAM}
        projectId="proj_a"
        apiBase=""
        apiToken="test"
        onBack={() => {}}
      />
    );

    await waitFor(() => {
      expect(screen.getByTestId("tab-reglages")).toBeInTheDocument();
    });

    await user.click(screen.getByTestId("tab-reglages"));

    await waitFor(() => {
      expect(screen.getByText(/Rattrapage \(jours en arrière\)/i)).toBeInTheDocument();
    });

    // Change refetch_days via fireEvent on one of the number inputs
    const numberInputs = document.querySelectorAll("input[type='number']");
    expect(numberInputs.length).toBeGreaterThan(0);
    fireEvent.change(numberInputs[0], { target: { value: "5" } });

    // Save should now be enabled
    await waitFor(() => {
      const saveBtn = screen.getByText("Enregistrer");
      expect(saveBtn).not.toBeDisabled();
    });
    const saveBtn = screen.getByText("Enregistrer");
    fireEvent.click(saveBtn);

    await waitFor(() => {
      const patchCalls = fetchSpy.mock.calls.filter(
        (args: unknown[]) =>
          typeof args[0] === "string" &&
          (args[0] as string).includes("ds_001") &&
          (args[1] as RequestInit | undefined)?.method === "PATCH"
      );
      expect(patchCalls.length).toBeGreaterThan(0);
    });
  });
});
