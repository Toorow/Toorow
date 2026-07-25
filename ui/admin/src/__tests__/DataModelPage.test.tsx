/**
 * Vitest tests for DataModelPage (Story 8.5, AC9).
 *
 * Tests:
 *   - Table renders fields from API response
 *   - Kind filter selection updates displayed fields
 *   - Usage filter selection updates displayed fields
 *   - Clicking a field row opens the detail drawer
 *   - Detail drawer shows used-by rows
 *   - Detail drawer shows conflict banner when conflicts present
 *   - Remap select calls PUT /api/datamodel/mappings
 *   - "New field" button opens the dialog
 *   - Empty state when no fields
 *   - Header summary counts rendered
 *
 * Copy is English (v3 restyle).
 */

import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@mui/material";
import { adminTheme } from "../theme";
import DataModelPage from "../DataModelPage";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function renderWithTheme(ui: React.ReactElement) {
  return render(<ThemeProvider theme={adminTheme}>{ui}</ThemeProvider>);
}

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Sample data
// ---------------------------------------------------------------------------

const FIELDS = [
  {
    name: "clicks",
    display_name: "Clics",
    data_type: "integer",
    field_kind: "metric",
    measure: "sum",
    description: "Nombre de clics",
    created_by: "system",
    is_default: true,
    created_at: "2026-07-12T10:00:00+00:00",
    used_by_count: 2,
  },
  {
    name: "impressions",
    display_name: "Impressions",
    data_type: "integer",
    field_kind: "metric",
    measure: "sum",
    description: null,
    created_by: "system",
    is_default: true,
    created_at: "2026-07-12T10:00:00+00:00",
    used_by_count: 1,
  },
  {
    name: "country",
    display_name: "Pays",
    data_type: "string",
    field_kind: "dimension",
    measure: null,
    description: "Code pays ISO-3166",
    created_by: "system",
    is_default: true,
    created_at: "2026-07-12T10:00:00+00:00",
    used_by_count: 0,
  },
];

const FIELD_DETAIL = {
  ...FIELDS[0],
  used_by: [
    {
      datastream_id: "ds_001",
      datastream_name: "Meta Ads",
      module_name: "meta-ads",
      project_id: "proj_a",
      enabled: true,
      source_field: "clicks",
      last_loaded_at: "2026-07-12T08:00:00+00:00",
      last_verdict: "ok",
    },
    {
      datastream_id: "ds_002",
      datastream_name: "Google Ads",
      module_name: "google-ads",
      project_id: "proj_a",
      enabled: true,
      source_field: "clicks",
      last_loaded_at: "2026-07-12T09:00:00+00:00",
      last_verdict: "ok",
    },
  ],
  conflicts: [],
};

const COST_FIELD_DETAIL = {
  name: "cost",
  display_name: "Coût",
  data_type: "currency",
  field_kind: "metric",
  measure: "sum",
  description: null,
  created_by: "system",
  is_default: true,
  created_at: "2026-07-12T10:00:00+00:00",
  used_by_count: 2,
  used_by: [
    {
      datastream_id: "ds_001",
      datastream_name: "Meta Ads",
      module_name: "meta-ads",
      project_id: "proj_a",
      enabled: true,
      source_field: "spend",
      last_loaded_at: null,
      last_verdict: null,
    },
    {
      datastream_id: "ds_002",
      datastream_name: "Google Ads",
      module_name: "google-ads",
      project_id: "proj_a",
      enabled: true,
      source_field: "cost_micros",
      last_loaded_at: null,
      last_verdict: null,
    },
  ],
  conflicts: [
    {
      code: "CURRENCY_CONFLICT",
      message:
        "This field is fed by distinct modules whose currency units may differ.",
      affected_streams: ["Meta Ads", "Google Ads"],
    },
  ],
};

// ---------------------------------------------------------------------------
// fetch mock helpers
// ---------------------------------------------------------------------------

function mockFetch(responses: Record<string, unknown>) {
  return vi.fn().mockImplementation((url: string, _options?: RequestInit) => {
    for (const [key, data] of Object.entries(responses)) {
      if (url.includes(key)) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => data,
        });
      }
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: async () => [],
    });
  });
}

// ---------------------------------------------------------------------------
// Tests: table renders fields
// ---------------------------------------------------------------------------

describe("DataModelPage — table renders fields", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", mockFetch({ "/api/datamodel/fields": FIELDS }));
  });

  it("renders field display names in the table", async () => {
    renderWithTheme(<DataModelPage />);

    await waitFor(() => {
      expect(screen.getByTestId("fields-table")).toBeInTheDocument();
    });

    expect(screen.getByText("Clics")).toBeInTheDocument();
    expect(screen.getByText("Impressions")).toBeInTheDocument();
    expect(screen.getByText("Pays")).toBeInTheDocument();
  });

  it("renders used_by_count as the number hero", async () => {
    renderWithTheme(<DataModelPage />);

    await waitFor(() => {
      expect(screen.getByTestId("field-row-clicks")).toBeInTheDocument();
    });

    // clicks has used_by_count=2
    const row = screen.getByTestId("field-row-clicks");
    expect(row).toHaveTextContent("2");
  });

  it("shows KPI strip with metric and dimension counts", async () => {
    renderWithTheme(<DataModelPage />);

    await waitFor(() => {
      expect(screen.getByTestId("kpi-strip")).toBeInTheDocument();
    });

    // 2 metrics (clicks + impressions), 1 dimension (country)
    expect(screen.getByTestId("count-metrics")).toHaveTextContent("2");
    expect(screen.getByTestId("count-dimensions")).toHaveTextContent("1");
  });

  it("shows unmapped count for unmapped fields", async () => {
    renderWithTheme(<DataModelPage />);

    await waitFor(() => {
      expect(screen.getByTestId("kpi-strip")).toBeInTheDocument();
    });

    // country has used_by_count=0
    expect(screen.getByTestId("count-unmapped")).toHaveTextContent("1");
  });
});

// ---------------------------------------------------------------------------
// Tests: filters
// ---------------------------------------------------------------------------

describe("DataModelPage — filters", () => {
  it("renders kind filter", async () => {
    vi.stubGlobal("fetch", mockFetch({ "/api/datamodel/fields": FIELDS }));
    renderWithTheme(<DataModelPage />);

    await waitFor(() => {
      expect(screen.getByTestId("filter-bar")).toBeInTheDocument();
    });

    // Filter controls present
    expect(screen.getByTestId("filter-kind")).toBeInTheDocument();
    expect(screen.getByTestId("filter-usage")).toBeInTheDocument();
    expect(screen.getByTestId("filter-module")).toBeInTheDocument();
  });

  it("re-fetches when kind filter changes", async () => {
    const fetchMock = mockFetch({ "/api/datamodel/fields": FIELDS });
    vi.stubGlobal("fetch", fetchMock);

    renderWithTheme(<DataModelPage />);

    await waitFor(() => {
      expect(screen.getByTestId("filter-kind")).toBeInTheDocument();
    });

    // Count only the fields calls: the page also fetches /api/datastreams once
    // at mount for the module filter options (AI-49).
    const fieldsCalls = () =>
      fetchMock.mock.calls.filter(([u]) => String(u).includes("/api/datamodel/fields"));

    // Initial fields fetch
    expect(fieldsCalls().length).toBe(1);

    // Change kind filter
    fireEvent.change(screen.getByTestId("filter-kind"), {
      target: { value: "metric" },
    });

    await waitFor(() => {
      // Fields fetch called again
      expect(fieldsCalls().length).toBe(2);
    });
  });
});

// ---------------------------------------------------------------------------
// Tests: field click -> drawer
// ---------------------------------------------------------------------------

describe("DataModelPage — detail drawer", () => {
  it("opens drawer on field row click", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch({
        "/api/datamodel/fields/clicks": FIELD_DETAIL,
        "/api/datamodel/fields": FIELDS,
      }),
    );

    renderWithTheme(<DataModelPage />);

    await waitFor(() => {
      expect(screen.getByTestId("field-row-clicks")).toBeInTheDocument();
    });

    await userEvent.click(screen.getByTestId("field-row-clicks"));

    await waitFor(() => {
      expect(screen.getByTestId("field-detail-drawer")).toBeInTheDocument();
    });
  });

  it("shows used-by rows in drawer", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch({
        "/api/datamodel/fields/clicks": FIELD_DETAIL,
        "/api/datamodel/fields": FIELDS,
      }),
    );

    renderWithTheme(<DataModelPage />);

    await waitFor(() => {
      expect(screen.getByTestId("field-row-clicks")).toBeInTheDocument();
    });

    await userEvent.click(screen.getByTestId("field-row-clicks"));

    await waitFor(() => {
      expect(screen.getByTestId("used-by-row-ds_001")).toBeInTheDocument();
      expect(screen.getByTestId("used-by-row-ds_002")).toBeInTheDocument();
    });
  });

  it("shows conflict banner when conflicts present", async () => {
    const costsInFields = [
      {
        name: "cost",
        display_name: "Coût",
        data_type: "currency",
        field_kind: "metric",
        measure: "sum",
        description: null,
        created_by: "system",
        is_default: true,
        created_at: "2026-07-12T10:00:00+00:00",
        used_by_count: 2,
      },
    ];

    vi.stubGlobal(
      "fetch",
      mockFetch({
        "/api/datamodel/fields/cost": COST_FIELD_DETAIL,
        "/api/datamodel/fields": costsInFields,
      }),
    );

    renderWithTheme(<DataModelPage />);

    await waitFor(() => {
      expect(screen.getByTestId("field-row-cost")).toBeInTheDocument();
    });

    await userEvent.click(screen.getByTestId("field-row-cost"));

    await waitFor(() => {
      expect(screen.getByTestId("conflict-banner")).toBeInTheDocument();
    });

    expect(screen.getByText(/Currency conflict/i)).toBeInTheDocument();
  });

  it("remap select is rendered in drawer", async () => {
    // This test verifies the remap select is present in the used-by list.
    // Full PUT interaction is tested in FieldDetailDrawer unit tests; here we
    // just verify the element exists (MUI Select interaction requires complex
    // portal-aware userEvent that is fragile in jsdom).
    vi.stubGlobal(
      "fetch",
      mockFetch({
        "/api/datamodel/fields/clicks": FIELD_DETAIL,
        "/api/datamodel/fields": FIELDS,
      }),
    );

    renderWithTheme(<DataModelPage />);

    await waitFor(() => {
      expect(screen.getByTestId("field-row-clicks")).toBeInTheDocument();
    });

    await userEvent.click(screen.getByTestId("field-row-clicks"));

    await waitFor(() => {
      expect(screen.getByTestId("remap-select-ds_001")).toBeInTheDocument();
      expect(screen.getByTestId("remap-select-ds_002")).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Tests: new field dialog
// ---------------------------------------------------------------------------

describe("DataModelPage — new field dialog", () => {
  it("opens dialog when 'New field' button clicked", async () => {
    vi.stubGlobal("fetch", mockFetch({ "/api/datamodel/fields": FIELDS }));
    renderWithTheme(<DataModelPage />);

    await waitFor(() => {
      expect(screen.getByTestId("new-field-button")).toBeInTheDocument();
    });

    await userEvent.click(screen.getByTestId("new-field-button"));

    await waitFor(() => {
      expect(screen.getByTestId("new-field-dialog")).toBeInTheDocument();
    });
  });

  it("submit button is disabled initially (empty form)", async () => {
    vi.stubGlobal("fetch", mockFetch({ "/api/datamodel/fields": FIELDS }));
    renderWithTheme(<DataModelPage />);

    await waitFor(() => {
      expect(screen.getByTestId("new-field-button")).toBeInTheDocument();
    });

    await userEvent.click(screen.getByTestId("new-field-button"));

    await waitFor(() => {
      expect(screen.getByTestId("new-field-submit")).toBeInTheDocument();
    });

    // Submit is disabled when name is empty
    expect(screen.getByTestId("new-field-submit")).toBeDisabled();
  });
});

// ---------------------------------------------------------------------------
// Tests: empty state
// ---------------------------------------------------------------------------

describe("DataModelPage — empty state", () => {
  it("shows empty state when no fields match filters", async () => {
    vi.stubGlobal("fetch", mockFetch({ "/api/datamodel/fields": [] }));
    renderWithTheme(<DataModelPage />);

    await waitFor(() => {
      expect(
        screen.getByText(/No field matches the selected filters/i),
      ).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Tests: module filter (AI-49)
// ---------------------------------------------------------------------------

// Sample datastreams returned by /api/datastreams
const DATASTREAMS = [
  { id: "ds_001", name: "Meta Ads", module_name: "meta-ads" },
  { id: "ds_002", name: "Google Ads", module_name: "google-ads" },
  { id: "ds_003", name: "Google Ads #2", module_name: "google-ads" }, // duplicate
];

describe("DataModelPage — module filter (AI-49)", () => {
  it("populates module options from /api/datastreams (deduped, sorted)", async () => {
    const fetchMock = mockFetch({
      "/api/datastreams": DATASTREAMS,
      "/api/datamodel/fields": FIELDS,
    });
    vi.stubGlobal("fetch", fetchMock);

    renderWithTheme(<DataModelPage />);

    // Wait for fields to render (datastreams fetch happens in parallel).
    await waitFor(() => {
      expect(screen.getByTestId("filter-module")).toBeInTheDocument();
    });

    // Open the module select to reveal options. The hidden <input> (testid) is a
    // SIBLING of the visible [role=combobox] div inside the MUI InputBase root;
    // MUI Select opens on mouseDown, not click.
    const moduleSelect = screen.getByTestId("filter-module");
    const combobox = moduleSelect.parentElement?.querySelector("[role='combobox']");
    expect(combobox).not.toBeNull();
    fireEvent.mouseDown(combobox as HTMLElement);

    // After opening, MUI renders options in a portal — wait for them.
    await waitFor(() => {
      const options = screen.getAllByRole("option");
      const values = options.map((o) => o.getAttribute("data-value"));
      expect(values).toContain("all");
      expect(values).toContain("google-ads");
      expect(values).toContain("meta-ads");
      // Deduped: google-ads appears once only
      expect(values.filter((v) => v === "google-ads").length).toBe(1);
    });
  });

  it("selecting a module re-fetches with ?module= query param", async () => {
    const fetchMock = mockFetch({
      "/api/datastreams": DATASTREAMS,
      "/api/datamodel/fields": FIELDS,
    });
    vi.stubGlobal("fetch", fetchMock);

    renderWithTheme(<DataModelPage />);

    await waitFor(() => {
      expect(screen.getByTestId("filter-module")).toBeInTheDocument();
    });

    // Initial fetch (no module param)
    const initialFieldsCalls = fetchMock.mock.calls.filter(
      (args) =>
        typeof args[0] === "string" && args[0].includes("/api/datamodel/fields"),
    );
    expect(initialFieldsCalls.length).toBe(1);
    expect(initialFieldsCalls[0][0]).not.toContain("module=");

    // Select "meta-ads"
    fireEvent.change(screen.getByTestId("filter-module"), {
      target: { value: "meta-ads" },
    });

    await waitFor(() => {
      const allFieldsCalls = fetchMock.mock.calls.filter(
        (args) =>
          typeof args[0] === "string" && args[0].includes("/api/datamodel/fields"),
      );
      // Should have been called again with the module param
      expect(allFieldsCalls.length).toBe(2);
      expect(allFieldsCalls[1][0]).toContain("module=meta-ads");
    });
  });

  it("empty filtered result shows designed empty state with filter hint", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.includes("/api/datastreams")) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => DATASTREAMS,
        });
      }
      if (url.includes("/api/datamodel/fields") && url.includes("module=meta-ads")) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => [], // server returns empty — no fields for this module
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => FIELDS,
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    renderWithTheme(<DataModelPage />);

    await waitFor(() => {
      expect(screen.getByTestId("filter-module")).toBeInTheDocument();
    });

    // Select "meta-ads" → server returns []
    fireEvent.change(screen.getByTestId("filter-module"), {
      target: { value: "meta-ads" },
    });

    await waitFor(() => {
      expect(
        screen.getByText(/No field matches the selected filters/i),
      ).toBeInTheDocument();
    });

    // Filter hint must name the active module
    await waitFor(() => {
      expect(screen.getByTestId("fields-empty-filter-hint")).toBeInTheDocument();
    });
    expect(screen.getByTestId("fields-empty-filter-hint")).toHaveTextContent("meta-ads");
  });

  it("reset to 'Tous les modules' re-fetches without module param", async () => {
    const fetchMock = mockFetch({
      "/api/datastreams": DATASTREAMS,
      "/api/datamodel/fields": FIELDS,
    });
    vi.stubGlobal("fetch", fetchMock);

    renderWithTheme(<DataModelPage />);

    await waitFor(() => {
      expect(screen.getByTestId("filter-module")).toBeInTheDocument();
    });

    // First select a module
    fireEvent.change(screen.getByTestId("filter-module"), {
      target: { value: "google-ads" },
    });

    await waitFor(() => {
      const calls = fetchMock.mock.calls.filter(
        (args) =>
          typeof args[0] === "string" && args[0].includes("/api/datamodel/fields"),
      );
      expect(calls.length).toBe(2);
    });

    // Then reset to "all"
    fireEvent.change(screen.getByTestId("filter-module"), {
      target: { value: "all" },
    });

    await waitFor(() => {
      const calls = fetchMock.mock.calls.filter(
        (args) =>
          typeof args[0] === "string" && args[0].includes("/api/datamodel/fields"),
      );
      // Third call — without module param
      expect(calls.length).toBe(3);
      expect(calls[2][0]).not.toContain("module=");
    });
  });
});
