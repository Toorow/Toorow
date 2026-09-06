/**
 * The reporting currency is asked at sign-up, beside the first project.
 *
 * Jean, 2026-08-03: *"faut l'ajouter sur l'écran là où on demande le nom du
 * projet"*. The currency is a PROJECT default, so it sits with the project
 * field rather than with the organization identity above it — the order this
 * file pins is the placement decision, and a reordering has to break a test
 * rather than pass unnoticed.
 *
 * What must not come back: `currency: "EUR"` was posted on every sign-up and
 * shown nowhere, and `project_provenance.decide()` records any value it
 * receives as an operator choice. So the field defaults to NOT SET, and an
 * unset currency is omitted from the payload entirely — the value then travels
 * as a platform default, which is what it is.
 *
 * 2026-08-04 — the control became `ReferenceSelect` (48.3 AC1). A `<select>` fed
 * by a typeahead endpoint capped at 50 rows offered 50 of 156 currencies and 50
 * of 313 zones, alphabetically: USD, JPY, Europe/Paris and America/New_York were
 * not selectable. Two behaviours changed with it, both asserted below: a value
 * can be searched for, and an unreadable vocabulary no longer HIDES the field.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import CreateOrg from "../shell/pages/CreateOrg";

const { apiFetchMock } = vi.hoisted(() => ({ apiFetchMock: vi.fn() }));

vi.mock("../lib/apiFetch", () => ({
  apiFetch: apiFetchMock,
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  ApiError: class extends Error {},
}));

// `platform_default` is what the server writes when nobody chooses
// (`project_provenance.PLATFORM_FALLBACK_*`), served so the screen can name the
// default it is about to accept without copying the constant.
const CURRENCIES = [
  { code: "EUR", display_name: "Euro" },
  { code: "JPY", display_name: "Yen" },
  { code: "SEK", display_name: "Swedish krona" },
];
const ZONES = [
  { code: "Europe/Paris", display_name: "Europe/Paris" },
  { code: "America/New_York", display_name: "America/New York" },
];

function answer(body: unknown, ok = true) {
  return Promise.resolve({ ok, json: () => Promise.resolve(body) } as Response);
}

function match(items: { code: string }[], url: string) {
  const q = (new URL(url, "http://x").searchParams.get("q") ?? "").toUpperCase();
  return items.filter((item) => !q || item.code.toUpperCase().includes(q));
}

beforeEach(() => {
  apiFetchMock.mockReset();
  apiFetchMock.mockImplementation((path: string) => {
    if (path.startsWith("/api/reference/currencies"))
      return answer({ platform_default: "EUR", items: match(CURRENCIES, path) });
    if (path.startsWith("/api/reference/timezones"))
      return answer({ platform_default: "Europe/Paris", items: match(ZONES, path) });
    // The submit stays disabled while the name gate is loading, and the gate is
    // a profile read. Answering it is a precondition of reaching the payload,
    // not part of what is under test.
    if (path.includes("/profile")) return answer({ display_name: "Jean Example" });
    return answer({ id: "org_EXAMPLE", next_url: "/" });
  });
});

/** Type into the combobox and commit a ranked option, as a person does. */
async function choose(label: RegExp, vocabulary: string, code: string) {
  const user = userEvent.setup();
  const input = await screen.findByLabelText(label);
  await user.click(input);
  await user.type(input, code.slice(0, 3));
  const list = await screen.findByRole("listbox", { name: vocabulary });
  fireEvent.mouseDown(await within(list).findByRole("option", { name: new RegExp(code) }));
}

function confirmationBody() {
  const post = apiFetchMock.mock.calls.find(([p]) => String(p).includes("/confirmation"));
  expect(post).toBeTruthy();
  return JSON.parse((post![1] as RequestInit).body as string);
}

it("asks the currency with the project, not with the organization identity", async () => {
  render(<CreateOrg onCreated={vi.fn()} />);
  const field = await screen.findByLabelText(/reporting currency/i);
  const project = screen.getByLabelText(/first project/i);

  // Order is the placement decision: project name, then its currency.
  expect(project.compareDocumentPosition(field) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  // And it comes before the immutable warehouse slug block, which closes the form.
  expect(screen.getByText(/warehouse slug/i)).toBeInTheDocument();
});

it("searches the whole vocabulary instead of rendering a capped page of it", async () => {
  const user = userEvent.setup();
  render(<CreateOrg onCreated={vi.fn()} />);
  const input = await screen.findByLabelText(/reporting currency/i);
  await user.click(input);
  await user.type(input, "SEK");

  // The query reaches the server, which ranks. The old `<select>` asked for 200
  // rows, got the alphabetical first 50, and never sent a query at all.
  await waitFor(() =>
    expect(apiFetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/reference/currencies?q=SEK"),
      expect.anything(),
    ),
  );
  const list = await screen.findByRole("listbox", { name: "currencies" });
  expect(await within(list).findByRole("option", { name: /SEK/ })).toBeInTheDocument();
});

it("defaults to NOT SET, because choosing nothing is a real answer", async () => {
  render(<CreateOrg onCreated={vi.fn()} />);
  const input = (await screen.findByLabelText(/reporting currency/i)) as HTMLInputElement;
  expect(input.value).toBe("");
  // And the unset state is readable without opening anything: it names the
  // fallback it is about to accept.
  await waitFor(() => expect(input.placeholder).toMatch(/Not set — EUR/));
});

it("omits the currency from the payload when nobody chose one", async () => {
  const user = userEvent.setup();
  render(<CreateOrg onCreated={vi.fn()} />);
  await screen.findByLabelText(/reporting currency/i);
  // The submit stays disabled without an organization name — filling it is part
  // of reaching the payload at all, not part of what is under test.
  await user.type(screen.getByLabelText(/organization name/i), "Example Org");
  await user.click(screen.getByRole("button", { name: /create organization/i }));

  await waitFor(() => {
    const body = confirmationBody();
    // Absent, not "EUR", not "". `decide_currency(None)` then records `default`.
    expect("currency" in body).toBe(false);
    expect(body.timezone_suggestion).toBeTruthy();
  });
});

it("sends the currency once it is actually chosen", async () => {
  const user = userEvent.setup();
  render(<CreateOrg onCreated={vi.fn()} />);
  await screen.findByLabelText(/reporting currency/i);
  await user.type(screen.getByLabelText(/organization name/i), "Example Org");
  await choose(/reporting currency/i, "currencies", "JPY");
  await user.click(screen.getByRole("button", { name: /create organization/i }));

  await waitFor(() => {
    // NOW it is an operator choice, and the provenance saying so is true.
    expect(confirmationBody().currency).toBe("JPY");
  });
});

it("never lets typed text become the value", async () => {
  const user = userEvent.setup();
  render(<CreateOrg onCreated={vi.fn()} />);
  const input = await screen.findByLabelText(/reporting currency/i);
  await user.type(screen.getByLabelText(/organization name/i), "Example Org");
  await user.click(input);
  await user.type(input, "XXX");
  await user.keyboard("{Enter}");
  await user.click(screen.getByRole("button", { name: /create organization/i }));

  await waitFor(() => expect("currency" in confirmationBody()).toBe(false));
});

it("keeps the field and reports the outage when the vocabulary is unreadable", async () => {
  // Reversed on 2026-08-04 (Jean): the field used to disappear. A missing field
  // reads as "this product does not ask this", which is a different and wrong
  // claim when the list simply could not be read — the same reasoning
  // ReferenceSelect gives for refusing to show "No matches" on an outage.
  apiFetchMock.mockImplementation((path: string) => {
    if (path.startsWith("/api/reference/")) return Promise.reject(new Error("HTTP 503"));
    if (path.includes("/profile")) return answer({ display_name: "Jean Example" });
    return answer({ id: "org_EXAMPLE", next_url: "/" });
  });
  const user = userEvent.setup();
  render(<CreateOrg onCreated={vi.fn()} />);

  const input = await screen.findByLabelText(/reporting currency/i);
  await user.click(input);
  expect(await screen.findByRole("alert")).toHaveTextContent(/unavailable/i);
  // The value stays unset, so the summary says what that earns without naming a
  // fallback it could not read.
  const summary = screen.getByLabelText("What will be created");
  expect(within(summary).getByText("Not set — the platform default applies")).toBeInTheDocument();
});

// ---------------------------------------------------------------------------
// The timezone: a suggestion that is SHOWN, and a search to override it
// ---------------------------------------------------------------------------

it("shows the browser suggestion instead of hiding it behind a silent default", async () => {
  render(<CreateOrg onCreated={vi.fn()} />);
  const input = (await screen.findByLabelText(/reporting timezone/i)) as HTMLInputElement;
  // Kept by default, and NAMED. The old screen sent this value invisibly and had
  // it recorded as an operator choice.
  expect(input.value).toBe("");
  expect(input.placeholder).toMatch(/suggested by your browser/i);
});

it("keeps the zone a suggestion when it is not overridden", async () => {
  const user = userEvent.setup();
  render(<CreateOrg onCreated={vi.fn()} />);
  await screen.findByLabelText(/reporting timezone/i);
  await user.type(screen.getByLabelText(/organization name/i), "Example Org");
  await user.click(screen.getByRole("button", { name: /create organization/i }));

  await waitFor(() => {
    const body = confirmationBody();
    // A suggestion travels as one; `timezone` stays absent so nothing claims a
    // human picked it.
    expect(body.timezone_suggestion).toBeTruthy();
    expect("timezone" in body).toBe(false);
  });
});

it("turns an overridden zone into an operator decision", async () => {
  const user = userEvent.setup();
  render(<CreateOrg onCreated={vi.fn()} />);
  await screen.findByLabelText(/reporting timezone/i);
  await user.type(screen.getByLabelText(/organization name/i), "Example Org");
  // Not in the first 50 zones alphabetically: unreachable before this control.
  await choose(/reporting timezone/i, "timezones", "America/New_York");
  await user.click(screen.getByRole("button", { name: /create organization/i }));

  await waitFor(() => expect(confirmationBody().timezone).toBe("America/New_York"));
});

// ---------------------------------------------------------------------------
// And both of them read back where it matters: the confirmation step
// ---------------------------------------------------------------------------

/**
 * The form asked for three things and its "Before you create" summary listed
 * one. The two it omitted are precisely the two whose whole point is that a
 * value must not be carried without the person seeing which origin it earned —
 * and the confirmation hash certifies what the operator reviewed
 * (`hosted_entry_scope.confirmed_payload`), so what it reviewed has to contain
 * them.
 */
it("reads the currency and the timezone back before the irreversible step", async () => {
  render(<CreateOrg onCreated={vi.fn()} />);
  await screen.findByLabelText(/reporting currency/i);
  const summary = screen.getByLabelText("What will be created");

  expect(within(summary).getByText("Reporting currency")).toBeInTheDocument();
  expect(within(summary).getByText("Reporting timezone")).toBeInTheDocument();
  // Unchosen values are shown WITH the origin they will be recorded under, not
  // as bare values that look like decisions.
  await waitFor(() =>
    expect(within(summary).getByText(/EUR — platform default/)).toBeInTheDocument(),
  );
  expect(within(summary).getByText(/suggested by your browser/)).toBeInTheDocument();
});

it("says 'your choice' only once it actually is one", async () => {
  render(<CreateOrg onCreated={vi.fn()} />);
  await choose(/reporting currency/i, "currencies", "SEK");
  await choose(/reporting timezone/i, "timezones", "America/New_York");

  const summary = screen.getByLabelText("What will be created");
  expect(within(summary).getByText(/SEK — your choice/)).toBeInTheDocument();
  expect(within(summary).getByText(/America\/New York — your choice/)).toBeInTheDocument();
  expect(within(summary).queryByText(/platform default/)).not.toBeInTheDocument();
});

it("omits both rows in organization-only mode, where no project is created", async () => {
  render(<CreateOrg onCreated={vi.fn()} creationMode="organization-only" />);
  await screen.findByLabelText(/organization name/i);
  const summary = screen.getByLabelText("What will be created");
  // Neither field is asked here, so neither is confirmed: a summary row for a
  // decision the screen never took would be the same defect facing the other way.
  expect(within(summary).queryByText("Reporting currency")).not.toBeInTheDocument();
  expect(within(summary).queryByText("Reporting timezone")).not.toBeInTheDocument();
});
