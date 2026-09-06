/**
 * MdmConflictResolutionDialog — the body it posts is the body the route requires.
 *
 * The component existed for months with no test and no import, and posted
 * `{field_name, conflict_code, strategy, override_value, notes}` to a route that
 * answers 400 `target_field is required` to exactly that. These tests pin the two
 * gestures the server actually serves, so the mismatch cannot come back quietly.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import MdmConflictResolutionDialog, {
  type MdmConflict,
} from "../governance/MdmConflictResolutionDialog";

function response(status: number, body: unknown): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

const CURRENCIES = {
  items: [
    { code: "EUR", display_name: "Euro", minor_unit: 2 },
    { code: "SEK", display_name: "Swedish Krona", minor_unit: 2 },
  ],
};

function currencyConflict(overrides: Partial<MdmConflict> = {}): MdmConflict {
  return {
    fieldName: "revenue",
    fieldLabel: "Revenue",
    code: "CURRENCY_CONFLICT",
    message: "Two sources report this field in different currencies.",
    severity: "blocking",
    connectors: ["meta-ads"],
    resolvedByConnector: { "meta-ads": null },
    ...overrides,
  };
}

function stubFetch() {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith("/api/reference/currencies")) return Promise.resolve(response(200, CURRENCIES));
    return Promise.resolve(response(200, { ok: true }));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

beforeEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
  localStorage.setItem("api_token", "test-token");
});

afterEach(() => vi.unstubAllGlobals());

test("a currency conflict binds ONE source currency, with the body the route requires", async () => {
  const fetchMock = stubFetch();
  const onResolved = vi.fn();
  const user = userEvent.setup();

  render(
    <MdmConflictResolutionDialog
      open
      projectId="project/acme"
      conflict={currencyConflict()}
      onClose={vi.fn()}
      onResolved={onResolved}
    />,
  );

  // One source is not a choice: it is shown, not offered.
  expect(screen.getByDisplayValue("meta-ads")).toBeInTheDocument();

  const combobox = screen.getByRole("combobox");
  await user.click(combobox);
  await user.type(combobox, "SE");
  fireEvent.mouseDown(await screen.findByRole("option", { name: /SEK/ }));

  await user.click(screen.getByRole("button", { name: "Bind currency" }));

  await waitFor(() => expect(onResolved).toHaveBeenCalled());
  const post = fetchMock.mock.calls.find(([url]) => String(url) === "/api/mdm/conflicts/resolutions");
  expect(post).toBeDefined();
  const [, init] = post as unknown as [unknown, RequestInit];
  expect(init.method).toBe("POST");
  expect(JSON.parse(String(init.body))).toEqual({
    project_id: "project/acme",
    target_field: "revenue",
    source_module: "meta-ads",
    resolved_source_currency: "SEK",
    note: null,
  });
});

test("nothing is posted until a currency is chosen", async () => {
  stubFetch();
  render(
    <MdmConflictResolutionDialog
      open
      projectId="project/acme"
      conflict={currencyConflict()}
      onClose={vi.fn()}
      onResolved={vi.fn()}
    />,
  );
  expect(screen.getByRole("button", { name: "Bind currency" })).toBeDisabled();
});

test("a conflict with no source names the gesture that unblocks it, and offers no writer", () => {
  stubFetch();
  render(
    <MdmConflictResolutionDialog
      open
      projectId="project/acme"
      conflict={currencyConflict({ connectors: [], resolvedByConnector: {} })}
      onClose={vi.fn()}
      onResolved={vi.fn()}
    />,
  );
  expect(screen.getByText(/Publish a mapping version/i)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Bind currency" })).not.toBeInTheDocument();
});

test("MEASURE_NULL patches the measure, and says it is platform-wide before the click", async () => {
  const fetchMock = stubFetch();
  const onResolved = vi.fn();
  const user = userEvent.setup();

  render(
    <MdmConflictResolutionDialog
      open
      projectId="project/acme"
      conflict={currencyConflict({
        code: "MEASURE_NULL",
        message: "This metric declares no aggregation.",
        connectors: [],
        resolvedByConnector: {},
      })}
      onClose={vi.fn()}
      onResolved={onResolved}
    />,
  );

  expect(screen.getByText(/platform-wide/i)).toBeInTheDocument();
  await user.selectOptions(screen.getByRole("combobox"), "average");
  await user.click(screen.getByRole("button", { name: "Declare measure" }));

  await waitFor(() => expect(onResolved).toHaveBeenCalled());
  const [url, init] = fetchMock.mock.calls.find(([candidate]) =>
    String(candidate).startsWith("/api/mdm/conflicts/measure/"),
  ) as unknown as [string, RequestInit];
  expect(url).toBe("/api/mdm/conflicts/measure/revenue");
  expect(init.method).toBe("PATCH");
  expect(JSON.parse(String(init.body))).toEqual({ measure: "average" });
});

test("a conflict this screen does not close names its lever instead of a dead control", () => {
  stubFetch();
  render(
    <MdmConflictResolutionDialog
      open
      projectId="project/acme"
      conflict={currencyConflict({
        code: "TIMEZONE_DAY_OFFSET",
        message: "These sources draw their day on different clocks.",
        severity: "advisory",
        connectors: [],
        resolvedByConnector: {},
      })}
      onClose={vi.fn()}
      onResolved={vi.fn()}
    />,
  );
  expect(screen.getByText(/Data › Datastreams/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Bind currency|Declare measure/ })).not.toBeInTheDocument();
});

test("the server's refusal is shown, and the dialog stays open", async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith("/api/reference/currencies")) return Promise.resolve(response(200, CURRENCIES));
    return Promise.resolve(response(422, { code: "validation_error", message: "Currency is not selectable." }));
  });
  vi.stubGlobal("fetch", fetchMock);
  const onResolved = vi.fn();
  const user = userEvent.setup();

  render(
    <MdmConflictResolutionDialog
      open
      projectId="project/acme"
      conflict={currencyConflict({ code: "MEASURE_NULL", connectors: [], resolvedByConnector: {} })}
      onClose={vi.fn()}
      onResolved={onResolved}
    />,
  );

  await user.click(screen.getByRole("button", { name: "Declare measure" }));
  expect(await screen.findByText("Currency is not selectable.")).toBeInTheDocument();
  expect(onResolved).not.toHaveBeenCalled();
});
