/**
 * Vitest tests for FieldDetailDrawer's History tab (Story 44.8).
 *
 * Tests:
 *   - History tab fetches and renders the timeline from mocked
 *     GET /api/datamodel/fields/{name}/history
 *   - change_kind badges render (created / updated / approved / deleted / restored)
 *   - Restoring a non-current version fires PATCH with the snapshot's
 *     patchable fields + restored_from as a plain int (Story 44.8 finding #4)
 *   - After a successful restore, the drawer REFETCHES GET .../fields/{name}
 *     (finding #2: PATCH returns the bare row, not used_by/conflicts) and the
 *     Restore button stays enabled afterwards (no client-invented permission
 *     gate -- AC3 is deferred, see finding #1)
 *   - A non-2xx restore response surfaces the server's verbatim message next
 *     to the timeline (role="alert") and Restore remains enabled -- there is
 *     no 403-driven disabled state to fake here
 *   - The "deleted" terminal state hides Restore and shows the recreate hint
 *
 * Copy is English (v3 restyle).
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@mui/material";
import { adminTheme } from "../theme";
import FieldDetailDrawer from "../datamodel/FieldDetailDrawer";

function renderWithTheme(ui: React.ReactElement) {
  return render(<ThemeProvider theme={adminTheme}>{ui}</ThemeProvider>);
}

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Sample data
// ---------------------------------------------------------------------------

const FIELD_DETAIL = {
  name: "clicks",
  display_name: "Clicks",
  data_type: "integer",
  field_kind: "metric",
  measure: "sum",
  description: "Number of clicks",
  created_by: "system",
  is_default: false,
  created_at: "2026-07-12T10:00:00+00:00",
  used_by_count: 0,
  used_by: [],
  conflicts: [],
  status: "approved",
  approved_at: null,
  approved_by: null,
};

const HISTORY_VERSIONS = [
  {
    version_number: 3,
    change_kind: "restored",
    changed_by: "alice@toorow.io",
    changed_at: "2026-07-20T09:00:00+00:00",
    diff: {
      display_name: { before: "Clicks (v2)", after: "Clicks" },
      _restored_from: { version_number: 1 },
    },
    snapshot: {
      display_name: "Clicks",
      measure: "sum",
      description: "Number of clicks",
      status: "approved",
    },
  },
  {
    version_number: 2,
    change_kind: "updated",
    changed_by: "bob@toorow.io",
    changed_at: "2026-07-15T09:00:00+00:00",
    diff: { display_name: { before: "Clicks", after: "Clicks (v2)" } },
    snapshot: {
      display_name: "Clicks (v2)",
      measure: "sum",
      description: "Number of clicks",
      status: "approved",
    },
  },
  {
    version_number: 1,
    change_kind: "created",
    changed_by: "system",
    changed_at: "2026-07-10T09:00:00+00:00",
    diff: null,
    snapshot: {
      display_name: "Clicks",
      measure: "sum",
      description: "Number of clicks",
      status: "draft",
    },
  },
];

// Story 44.8 finding #2: PATCH /fields/{name} returns the BARE
// target_fields row -- no used_by / used_by_count / conflicts. This is the
// real shape core.datamodel.update_target_field returns; using FIELD_DETAIL
// (which carries used_by/conflicts) here would hide the very defect the
// finding is about.
const PATCH_BARE_ROW = {
  name: "clicks",
  display_name: "Clicks (v2)",
  data_type: "integer",
  field_kind: "metric",
  measure: "sum",
  description: "Number of clicks",
  created_by: "system",
  is_default: false,
  created_at: "2026-07-12T10:00:00+00:00",
  status: "approved",
  approved_at: null,
  approved_by: null,
  updated_at: "2026-07-20T09:00:00+00:00",
};

const HISTORY_VERSIONS_DELETED_CURRENT = [
  {
    version_number: 2,
    change_kind: "deleted",
    changed_by: "bob@toorow.io",
    changed_at: "2026-07-16T09:00:00+00:00",
    diff: null,
    snapshot: {
      display_name: "Clicks",
      measure: "sum",
      description: "Number of clicks",
      status: "deleted",
    },
  },
  {
    version_number: 1,
    change_kind: "created",
    changed_by: "system",
    changed_at: "2026-07-10T09:00:00+00:00",
    diff: null,
    snapshot: {
      display_name: "Clicks",
      measure: "sum",
      description: "Number of clicks",
      status: "draft",
    },
  },
];

// ---------------------------------------------------------------------------
// fetch mock helper (method + url aware)
// ---------------------------------------------------------------------------

type MockRule = {
  match: (url: string, method: string) => boolean;
  status?: number;
  body: unknown;
};

function mockFetchWithRules(rules: MockRule[]) {
  return vi.fn().mockImplementation((url: string, options?: RequestInit) => {
    const method = (options?.method || "GET").toUpperCase();
    for (const rule of rules) {
      if (rule.match(url, method)) {
        const status = rule.status ?? 200;
        return Promise.resolve({
          ok: status >= 200 && status < 300,
          status,
          json: async () => rule.body,
        });
      }
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
  });
}

async function openHistoryTab() {
  await waitFor(() => {
    expect(screen.getByTestId("field-detail-drawer")).toBeInTheDocument();
  });
  const user = userEvent.setup();
  await user.click(screen.getByTestId("field-drawer-tab-history"));
}

// ---------------------------------------------------------------------------
// Tests: timeline renders
// ---------------------------------------------------------------------------

describe("FieldDetailDrawer — History timeline", () => {
  it("renders every version from the mocked history, newest first", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetchWithRules([
        {
          match: (url, method) => method === "GET" && url.includes("/history"),
          body: { versions: HISTORY_VERSIONS },
        },
        {
          match: (url, method) =>
            method === "GET" && !url.includes("/history") && url.includes("/fields/clicks"),
          body: FIELD_DETAIL,
        },
      ]),
    );

    renderWithTheme(
      <FieldDetailDrawer
        fieldName="clicks"
        open={true}
        onClose={() => {}}
        availableFields={[]}
      />,
    );

    await openHistoryTab();

    await waitFor(() => {
      expect(screen.getByTestId("history-timeline")).toBeInTheDocument();
    });

    expect(screen.getByTestId("history-row-3")).toBeInTheDocument();
    expect(screen.getByTestId("history-row-2")).toBeInTheDocument();
    expect(screen.getByTestId("history-row-1")).toBeInTheDocument();

    // Newest first: row 3 (restored) appears before row 1 (created) in the DOM.
    const timeline = screen.getByTestId("history-timeline");
    const rows = within(timeline).getAllByText(/@toorow\.io|system/);
    expect(rows[0]).toHaveTextContent("alice@toorow.io");

    expect(within(screen.getByTestId("history-row-3")).getByText("Restored")).toBeInTheDocument();
    expect(within(screen.getByTestId("history-row-2")).getByText("Updated")).toBeInTheDocument();
    expect(within(screen.getByTestId("history-row-1")).getByText("Created")).toBeInTheDocument();
  });

  it("shows the deleted badge for a deleted version", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetchWithRules([
        {
          match: (url, method) => method === "GET" && url.includes("/history"),
          body: { versions: HISTORY_VERSIONS_DELETED_CURRENT },
        },
        {
          match: (url, method) =>
            method === "GET" && !url.includes("/history") && url.includes("/fields/clicks"),
          status: 404,
          body: { code: "not_found", message: "Champ 'clicks' introuvable" },
        },
      ]),
    );

    renderWithTheme(
      <FieldDetailDrawer
        fieldName="clicks"
        open={true}
        onClose={() => {}}
        availableFields={[]}
      />,
    );

    await openHistoryTab();

    await waitFor(() => {
      expect(screen.getByTestId("history-timeline")).toBeInTheDocument();
    });

    expect(within(screen.getByTestId("history-row-2")).getByText("Deleted")).toBeInTheDocument();
  });

  it("hides Restore and shows the recreate hint when the field is currently deleted", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetchWithRules([
        {
          match: (url, method) => method === "GET" && url.includes("/history"),
          body: { versions: HISTORY_VERSIONS_DELETED_CURRENT },
        },
        {
          match: (url, method) =>
            method === "GET" && !url.includes("/history") && url.includes("/fields/clicks"),
          status: 404,
          body: { code: "not_found", message: "Champ 'clicks' introuvable" },
        },
      ]),
    );

    renderWithTheme(
      <FieldDetailDrawer
        fieldName="clicks"
        open={true}
        onClose={() => {}}
        availableFields={[]}
      />,
    );

    await openHistoryTab();

    await waitFor(() => {
      expect(screen.getByTestId("history-deleted-banner")).toBeInTheDocument();
    });
    expect(
      screen.getByText("Deleted — recreate the field to revive its history."),
    ).toBeInTheDocument();

    // No restore button anywhere on the timeline (current row never had one anyway,
    // and the non-current "created" row is also suppressed while the field is deleted).
    expect(screen.queryByTestId("restore-button-1")).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Tests: restore
// ---------------------------------------------------------------------------

describe("FieldDetailDrawer — Restore", () => {
  it("fires PATCH with the snapshot's values and restored_from as a plain int on confirm, then refetches detail (finding #2)", async () => {
    const fetchMock = mockFetchWithRules([
      {
        match: (url, method) => method === "GET" && url.includes("/history"),
        body: { versions: HISTORY_VERSIONS },
      },
      {
        match: (url, method) =>
          method === "GET" && !url.includes("/history") && url.includes("/fields/clicks"),
        body: FIELD_DETAIL,
      },
      {
        // Story 44.8 finding #2: the REAL PATCH response shape -- bare row,
        // no used_by/used_by_count/conflicts.
        match: (url, method) => method === "PATCH" && url.includes("/fields/clicks"),
        body: PATCH_BARE_ROW,
      },
    ]);
    vi.stubGlobal("fetch", fetchMock);

    const onFieldChanged = vi.fn();
    renderWithTheme(
      <FieldDetailDrawer
        fieldName="clicks"
        open={true}
        onClose={() => {}}
        availableFields={[]}
        onFieldChanged={onFieldChanged}
      />,
    );

    await openHistoryTab();
    await waitFor(() => {
      expect(screen.getByTestId("history-timeline")).toBeInTheDocument();
    });

    const user = userEvent.setup();
    // Row 2 (version_number 2) is a non-current row -> has a Restore button.
    await user.click(screen.getByTestId("restore-button-2"));
    await user.click(await screen.findByTestId("restore-confirm-button"));

    await waitFor(() => {
      expect(onFieldChanged).toHaveBeenCalled();
    });

    const patchCall = fetchMock.mock.calls.find(
      (call) => (call[1]?.method || "").toUpperCase() === "PATCH",
    );
    expect(patchCall).toBeTruthy();
    const [, options] = patchCall!;
    const sentBody = JSON.parse(options.body as string);
    expect(sentBody).toEqual({
      display_name: "Clicks (v2)",
      measure: "sum",
      description: "Number of clicks",
      restored_from: 2,
    });

    // finding #2: a GET /fields/clicks refetch happened AFTER the PATCH (not
    // just the initial detail load) -- this is what keeps the Detail tab
    // from crashing on revisit, since the PATCH response alone lacks
    // used_by/conflicts.
    const getDetailCalls = fetchMock.mock.calls.filter(
      ([url, opts]: [string, RequestInit?]) =>
        (opts?.method || "GET").toUpperCase() === "GET" &&
        typeof url === "string" &&
        !url.includes("/history") &&
        url.includes("/fields/clicks"),
    );
    expect(getDetailCalls.length).toBeGreaterThanOrEqual(2);

    // Switching back to Detail must not crash even though PATCH returned a
    // bare row -- the refetched FIELD_DETAIL (with used_by/conflicts) is
    // what's actually in state.
    await user.click(screen.getByTestId("field-drawer-tab-detail"));
    await waitFor(() => {
      expect(screen.getByText("No datastream feeds this field yet.")).toBeInTheDocument();
    });
  });

  it("surfaces a non-2xx restore response verbatim next to the timeline, with Restore staying enabled (finding #1: AC3 deferred, no fabricated permission gate)", async () => {
    const fetchMock = mockFetchWithRules([
      {
        match: (url, method) => method === "GET" && url.includes("/history"),
        body: { versions: HISTORY_VERSIONS },
      },
      {
        match: (url, method) =>
          method === "GET" && !url.includes("/history") && url.includes("/fields/clicks"),
        body: FIELD_DETAIL,
      },
      {
        match: (url, method) => method === "PATCH" && url.includes("/fields/clicks"),
        status: 422,
        body: { code: "validation_error", message: "Version 2 introuvable pour le champ 'clicks'" },
      },
    ]);
    vi.stubGlobal("fetch", fetchMock);

    renderWithTheme(
      <FieldDetailDrawer
        fieldName="clicks"
        open={true}
        onClose={() => {}}
        availableFields={[]}
      />,
    );

    await openHistoryTab();
    await waitFor(() => {
      expect(screen.getByTestId("history-timeline")).toBeInTheDocument();
    });

    const user = userEvent.setup();
    await user.click(screen.getByTestId("restore-button-2"));
    await user.click(await screen.findByTestId("restore-confirm-button"));

    const alertEl = await screen.findByTestId("restore-error");
    expect(alertEl).toHaveTextContent("Version 2 introuvable pour le champ 'clicks'");
    expect(alertEl).toHaveAttribute("role", "alert");

    // No permission model exists -- the server's message is surfaced, but
    // Restore is never disabled as a result of it.
    expect(screen.getByTestId("restore-button-2")).not.toBeDisabled();
    expect(screen.getByTestId("restore-button-1")).not.toBeDisabled();
  });
});
