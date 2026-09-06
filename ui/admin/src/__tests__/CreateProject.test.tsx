/**
 * The "create your first project" door decides the same two Project defaults as
 * the two sign-up doors — and used to decide them alone.
 *
 * It posted a hardcoded EUR and the browser's zone under the `timezone` key,
 * showed neither, and `project_provenance.decide()` recorded both as operator
 * choices. The screen now asks, omits what nobody chose, sends the browser guess
 * as a suggestion, and READS ALL OF IT BACK before the button — a value asked for
 * and never shown is a value nobody agreed to.
 *
 * The control is `ReferenceSelect` (48.3 AC1), not a `<select>`: the endpoint is a
 * typeahead capped at 50 rows, so a plain list rendered 50 of 156 currencies and
 * 50 of 313 zones — alphabetically, without USD, JPY or Europe/Paris. Hence the
 * "reaches a code no truncated list could contain" test below.
 *
 * The seam is `apiFetch`, not a bare `fetch` stub: a screen that calls `/api/`
 * without the bearer is exactly what a `fetch` stub cannot see (epic 46).
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import CreateProject from "../shell/pages/CreateProject";

const { apiFetchMock } = vi.hoisted(() => ({ apiFetchMock: vi.fn() }));

vi.mock("../lib/apiFetch", () => ({
  apiFetch: apiFetchMock,
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  ApiError: class extends Error {},
}));

const ORG = { id: "org_acme", name: "Acme", branding: null, projects: [] };

/** Ranked server-side; the component never re-sorts. `q` filters here as there. */
const CURRENCIES = [
  { code: "EUR", display_name: "Euro" },
  { code: "JPY", display_name: "Yen" },
  { code: "USD", display_name: "US Dollar" },
];
const ZONES = [
  { code: "Europe/Paris", display_name: "Europe/Paris" },
  { code: "America/New_York", display_name: "America/New York" },
];

function answer(body: unknown, status = 200) {
  return Promise.resolve({
    ok: status < 400,
    status,
    json: () => Promise.resolve(body),
  } as Response);
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
    return answer({ id: "proj_first", name: "First project" }, 201);
  });
});

function postBody() {
  const post = apiFetchMock.mock.calls.find(
    ([path, init]) => path === "/api/projects" && (init as RequestInit | undefined)?.method === "POST",
  );
  expect(post).toBeTruthy();
  return JSON.parse((post![1] as RequestInit).body as string);
}

/** Type into the combobox and commit a ranked option, as a person does. */
async function choose(label: RegExp, vocabulary: string, code: string) {
  const user = userEvent.setup();
  const input = await screen.findByLabelText(label);
  await user.click(input);
  await user.type(input, code.slice(0, 3));
  const list = await screen.findByRole("listbox", { name: vocabulary });
  fireEvent.mouseDown(await within(list).findByRole("option", { name: new RegExp(code) }));
}

it("creates the project in the existing organization and returns its canonical id", async () => {
  const onCreated = vi.fn();
  render(<CreateProject org={ORG} onCreated={onCreated} />);

  const user = userEvent.setup();
  expect(screen.getByRole("heading", { name: "Create your first project" })).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Create project" }));

  await waitFor(() => {
    expect(postBody()).toEqual(
      expect.objectContaining({ name: "First project", slug: "first-project", org_id: "org_acme" }),
    );
  });
  expect(onCreated).toHaveBeenCalledWith("proj_first");
});

it("omits the currency nobody chose instead of fabricating one", async () => {
  const user = userEvent.setup();
  render(<CreateProject org={ORG} onCreated={vi.fn()} />);
  await screen.findByLabelText(/reporting currency/i);
  await user.click(screen.getByRole("button", { name: "Create project" }));

  await waitFor(() => {
    const body = postBody();
    // Absent, not "EUR". `decide_currency(None)` then records `default`, which
    // is the truth; a value here would be recorded as an operator decision.
    expect("currency" in body).toBe(false);
  });
});

it("sends the browser zone as a suggestion, never as a choice", async () => {
  const user = userEvent.setup();
  render(<CreateProject org={ORG} onCreated={vi.fn()} />);
  await screen.findByLabelText(/reporting timezone/i);
  await user.click(screen.getByRole("button", { name: "Create project" }));

  await waitFor(() => {
    const body = postBody();
    expect(body.timezone_suggestion).toBeTruthy();
    expect("timezone" in body).toBe(false);
  });
});

it("reaches a code no truncated list could contain, and records it as a choice", async () => {
  const user = userEvent.setup();
  render(<CreateProject org={ORG} onCreated={vi.fn()} />);
  // USD is the 149th ISO 4217 code alphabetically and America/New_York is not in
  // the first 50 zones: neither was selectable while these fields were `<select>`s
  // fed by a capped page. Searching is what makes them reachable.
  await choose(/reporting currency/i, "currencies", "USD");
  await choose(/reporting timezone/i, "timezones", "America/New_York");
  await user.click(screen.getByRole("button", { name: "Create project" }));

  await waitFor(() => {
    const body = postBody();
    expect(body.currency).toBe("USD");
    expect(body.timezone).toBe("America/New_York");
  });
});

it("never lets typed text become the value", async () => {
  const user = userEvent.setup();
  render(<CreateProject org={ORG} onCreated={vi.fn()} />);
  const input = await screen.findByLabelText(/reporting currency/i);
  await user.click(input);
  await user.type(input, "XXX");
  await user.keyboard("{Enter}");
  await user.click(screen.getByRole("button", { name: "Create project" }));

  await waitFor(() => {
    // A free-text field would have posted "XXX" and had it recorded as an
    // operator decision. The vocabulary is the only source of values.
    expect("currency" in postBody()).toBe(false);
  });
});

it("can be put back to unset after a choice", async () => {
  const user = userEvent.setup();
  render(<CreateProject org={ORG} onCreated={vi.fn()} />);
  await choose(/reporting currency/i, "currencies", "JPY");
  // A `<select>` had "Not set" as its first option; a combobox needs this, or a
  // person who picks by accident can never take it back.
  await user.click(screen.getByRole("button", { name: /leave the currency unset/i }));
  await user.click(screen.getByRole("button", { name: "Create project" }));

  await waitFor(() => expect("currency" in postBody()).toBe(false));
});

it("reads both defaults back in the confirmation, naming the origin each one earned", async () => {
  render(<CreateProject org={ORG} onCreated={vi.fn()} />);
  await screen.findByLabelText(/reporting currency/i);
  const summary = screen.getByLabelText("What will be created");

  // Unchosen: the platform default, NAMED as one — and named from the value the
  // server serves, not from a constant copied into the front.
  await waitFor(() =>
    expect(within(summary).getByText(/EUR — platform default/)).toBeInTheDocument(),
  );
  // The zone is a browser guess until somebody overrides it, and says so.
  expect(within(summary).getByText(/suggested by your browser/)).toBeInTheDocument();

  await choose(/reporting currency/i, "currencies", "JPY");
  expect(within(summary).getByText(/JPY — your choice/)).toBeInTheDocument();
});

it("keeps the field and says the list is unavailable when the vocabulary cannot be read", async () => {
  apiFetchMock.mockImplementation((path: string) => {
    if (path.startsWith("/api/reference/")) return Promise.reject(new Error("HTTP 503"));
    return answer({ id: "proj_first" }, 201);
  });
  const user = userEvent.setup();
  render(<CreateProject org={ORG} onCreated={vi.fn()} />);

  // The field STAYS. Hiding it reads as "this product does not ask this", which
  // is a different and wrong claim when the list simply could not be read
  // (Jean, 2026-08-04 — reverses the earlier hide-the-field behaviour).
  const input = await screen.findByLabelText(/reporting currency/i);
  await user.click(input);
  expect(await screen.findByRole("alert")).toHaveTextContent(/unavailable/i);

  // And the summary still tells the truth: a platform default applies, WITHOUT
  // inventing which code it would be.
  const summary = screen.getByLabelText("What will be created");
  expect(within(summary).getByText("Not set — the platform default applies")).toBeInTheDocument();
  expect(within(summary).getByText(/suggested by your browser/)).toBeInTheDocument();
});
