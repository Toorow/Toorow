/**
 * Naming a dimension in the client's own words -- the console door, on screen.
 *
 * `governance.md:1950` ratifies TWO doors onto one state, and until 2026-08-24
 * only the model door had a caller: the three console routes shipped
 * (`core/dimension_lineage_api.py:392-401`) and `grep -rn "dimension-labels"
 * ui/admin/src` was empty. What is held here is the door, and each test is a
 * sentence the target owes:
 *
 *   1. an unnamed dimension SAYS SO and names the identifier a reader is seeing
 *      instead -- the first clause of the "A client label reaches every surface"
 *      list is that nobody should read `audience_language` in a chart;
 *   2. one question at a time: the word does not exist until the scope is
 *      answered, and the scope decides what the word starts as;
 *   3. the proposal derived from the identifier is PRE-FILLED and never stored
 *      on its own -- nothing is written until a person presses the button;
 *   4. the whole key travels, and `PLATFORM` is never among the scopes offered
 *      (`governance.md:1956`: writable by nobody, through either door);
 *   5. taking a name back asks first and names what becomes visible again;
 *   6. a refusal names the gesture that repairs it, never the server's cause.
 *
 * `fetch` is stubbed and `apiFetch` is not: the seam guard is what proves the
 * bearer is attached, and stubbing the seam would prove nothing.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import DimensionLabelPanel, { proposedLabel } from "../governance/DimensionLabelPanel";

const PROJECT = "proj_EXAMPLE";
const ORG = "org_EXAMPLE";
const DIMENSION = "audience_language";

function ok(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

function fail(status: number, code: string, message = "refused"): Response {
  return {
    ok: false,
    status,
    json: async () => ({ code, message }),
    text: async () => code,
  } as Response;
}

function serve(handler: (url: string, init?: RequestInit) => Response) {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init?: RequestInit) => {
      calls.push({ url, init });
      return Promise.resolve(handler(url, init));
    }),
  );
  return calls;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function mount(
  props: Partial<React.ComponentProps<typeof DimensionLabelPanel>> = {},
) {
  const onChanged = vi.fn();
  const view = render(
    <DimensionLabelPanel
      canonicalDimension={DIMENSION}
      projectId={PROJECT}
      organizationId={ORG}
      displayLabel={null}
      labelScope={null}
      labelSource="fallback_identifier"
      onChanged={onChanged}
      {...props}
    />,
  );
  return { ...view, onChanged };
}

it("derives a word from an identifier, and only from its shape", () => {
  expect(proposedLabel("audience_language")).toBe("Audience language");
  expect(proposedLabel("device_category")).toBe("Device category");
  // No underscore, nothing to repair: `country` is already the word.
  expect(proposedLabel("country")).toBe("Country");
});

it("says nobody named it, and names the word a reader is seeing instead", () => {
  serve(() => ok({}));
  mount();
  const notice = screen.getByTestId("dimension-label-unnamed");
  expect(notice.textContent).toContain(DIMENSION);
  expect(screen.queryByTestId("dimension-label-named")).toBeNull();
});

it("asks ONE question first: no word exists until the scope is answered", async () => {
  serve(() => ok({}));
  mount();
  expect(screen.queryByTestId("dimension-label-word")).toBeNull();
  expect(screen.queryByTestId("dimension-label-save")).toBeNull();

  await userEvent.click(screen.getByRole("radio", { name: "This project" }));
  expect(await screen.findByTestId("dimension-label-word")).toBeTruthy();
});

it("pre-fills the proposal derived from the identifier, and stores nothing yet", async () => {
  const calls = serve(() => ok({}));
  mount();
  await userEvent.click(screen.getByRole("radio", { name: "This project" }));

  const input = (await screen.findByTestId(
    "dimension-label-input",
  )) as HTMLInputElement;
  expect(input.value).toBe("Audience language");
  // A proposal is not a decision: nothing has been written.
  expect(calls.some((call) => call.init?.method === "POST")).toBe(false);
});

it("names it for the project with the whole key, and never for the platform", async () => {
  const calls = serve(() => ok({ canonical_dimension: DIMENSION }));
  const { onChanged } = mount();

  // The platform scope is writable by nobody through either door, so it is not
  // an option a person can even reach for.
  expect(screen.queryByRole("radio", { name: /platform/i })).toBeNull();

  await userEvent.click(screen.getByRole("radio", { name: "This project" }));
  const input = await screen.findByTestId("dimension-label-input");
  await userEvent.clear(input);
  await userEvent.type(input, "Reader language");
  await userEvent.click(screen.getByTestId("dimension-label-save"));

  await waitFor(() =>
    expect(calls.some((call) => call.init?.method === "POST")).toBe(true),
  );
  const post = calls.find((call) => call.init?.method === "POST")!;
  expect(post.url).toBe("/api/dimension-lineage/labels");
  expect(JSON.parse(String(post.init?.body))).toEqual({
    scope_level: "PROJECT",
    canonical_dimension: DIMENSION,
    display_label: "Reader language",
    project_id: PROJECT,
  });
  await waitFor(() => expect(onChanged).toHaveBeenCalled());
});

it("names it for the organization with the organization's identifier", async () => {
  const calls = serve(() => ok({}));
  mount();

  await userEvent.click(
    screen.getByRole("radio", { name: "Your whole organization" }),
  );
  await userEvent.click(await screen.findByTestId("dimension-label-save"));

  await waitFor(() =>
    expect(calls.some((call) => call.init?.method === "POST")).toBe(true),
  );
  expect(
    JSON.parse(String(calls.find((c) => c.init?.method === "POST")!.init?.body)),
  ).toMatchObject({ scope_level: "ORG", org_id: ORG });
});

it("offers no organization scope when the address names no organization", () => {
  serve(() => ok({}));
  mount({ organizationId: "" });
  expect(screen.getByRole("radio", { name: "This project" })).toBeTruthy();
  expect(
    screen.queryByRole("radio", { name: "Your whole organization" }),
  ).toBeNull();
});

it("shows the name in force with its scope, and renames rather than re-names", async () => {
  serve(() => ok({}));
  mount({
    displayLabel: "Langue",
    labelScope: "PROJECT",
    labelSource: "client",
  });

  expect(screen.getByTestId("dimension-label-named").textContent).toContain(
    "this project",
  );

  await userEvent.click(screen.getByRole("radio", { name: "This project" }));
  const input = (await screen.findByTestId(
    "dimension-label-input",
  )) as HTMLInputElement;
  // The scope that already carries a name starts from that name, never from a
  // proposal that would quietly undo it.
  expect(input.value).toBe("Langue");
  expect(screen.getByTestId("dimension-label-save").textContent).toContain(
    "Rename it",
  );
});

it("says what a project name would override, when the organization named it", async () => {
  serve(() => ok({}));
  mount({ displayLabel: "Langue", labelScope: "ORG", labelSource: "client" });

  await userEvent.click(screen.getByRole("radio", { name: "This project" }));
  const input = (await screen.findByTestId(
    "dimension-label-input",
  )) as HTMLInputElement;
  expect(input.value).toBe("Audience language");
  expect(screen.getByText(/Your organization calls it/i)).toBeTruthy();
});

it("offers nothing to take back on a platform name, and says why", () => {
  serve(() => ok({}));
  mount({
    displayLabel: "Audience language",
    labelScope: "PLATFORM",
    labelSource: "client",
  });
  expect(screen.queryByTestId("dimension-label-restore")).toBeNull();
  expect(screen.getByTestId("dimension-label-platform").textContent).toContain(
    "changed by a release",
  );
});

it("asks before taking a name back, and deletes at the scope in force", async () => {
  const calls = serve(() => ok({ deleted: true }));
  const { onChanged } = mount({
    displayLabel: "Langue",
    labelScope: "PROJECT",
    labelSource: "client",
  });

  await userEvent.click(screen.getByTestId("dimension-label-restore"));
  // The consequence is stated before the click, not after it.
  expect(screen.getByText(/becomes visible again/i)).toBeTruthy();
  await userEvent.click(
    screen.getByTestId("dimension-label-restore-confirm-yes"),
  );

  await waitFor(() =>
    expect(calls.some((call) => call.init?.method === "DELETE")).toBe(true),
  );
  const del = calls.find((call) => call.init?.method === "DELETE")!;
  expect(del.url).toContain(
    `/api/dimension-lineage/labels/${DIMENSION}?scope_level=PROJECT`,
  );
  expect(del.url).toContain(`project_id=${PROJECT}`);
  await waitFor(() => expect(onChanged).toHaveBeenCalled());
});

it("a refusal names the gesture that repairs it, not the server's cause", async () => {
  serve((_url, init) =>
    init?.method === "POST"
      ? fail(403, "forbidden", "Droits insuffisants.")
      : ok({}),
  );
  mount();

  await userEvent.click(screen.getByRole("radio", { name: "This project" }));
  await userEvent.click(await screen.findByTestId("dimension-label-save"));

  const refusal = await screen.findByTestId("dimension-label-refusal");
  expect(refusal.textContent).toContain("owner's or an admin's gesture");
  // The server's own words never reach the reader.
  expect(screen.queryByText(/Droits insuffisants/)).toBeNull();
});


/*
 * THE SCOPE BEING EDITED, NOT THE ONE THAT WINS (2026-08-31).
 *
 * Every case above has AT MOST ONE stored word, and with one word the two
 * questions -- "what does the cascade show?" and "what does this scope carry?"
 * -- have the same answer. So the panel could pre-fill from the winner and pass.
 *
 * The case that separates them was never written: an ORGANIZATION that named a
 * dimension AND a project that overrode it. Choosing "your whole organization"
 * then offered the word DERIVED from the identifier over that organization's
 * own chosen name, and the button said "Name it" for a gesture that was a
 * rename. `governance.md`: *"a scope that already carries a name pre-fills
 * anything but that name"*.
 */

const SCOPES = { PROJECT: "This project", ORG: "Your whole organization" };

it("pre-fills a NON-WINNING scope with its own word, not with a proposal", async () => {
  serve(() => ok({}));
  // The project overrides the organization: the cascade shows "Appareil", and
  // the organization still carries "Terminal".
  mount({
    displayLabel: "Appareil",
    labelScope: "PROJECT",
    labelSource: "client",
    scopeLabels: {
      PLATFORM: null,
      ORG: { display_label: "Terminal" },
      PROJECT: { display_label: "Appareil" },
    },
  });

  await userEvent.click(screen.getByRole("radio", { name: SCOPES.ORG }));
  const input = (await screen.findByTestId(
    "dimension-label-input",
  )) as HTMLInputElement;
  expect(input.value).toBe("Terminal");
  expect(input.value).not.toBe(proposedLabel(DIMENSION));
  // And the gesture is a rename, because a word is already there.
  expect(screen.getByTestId("dimension-label-save").textContent).toContain(
    "Rename",
  );
});

it("pre-fills the winning scope with its own word too", async () => {
  serve(() => ok({}));
  mount({
    displayLabel: "Appareil",
    labelScope: "PROJECT",
    labelSource: "client",
    scopeLabels: {
      PLATFORM: null,
      ORG: { display_label: "Terminal" },
      PROJECT: { display_label: "Appareil" },
    },
  });

  await userEvent.click(screen.getByRole("radio", { name: SCOPES.PROJECT }));
  const input = (await screen.findByTestId(
    "dimension-label-input",
  )) as HTMLInputElement;
  expect(input.value).toBe("Appareil");
});

it("proposes the derived word only where the scope carries none", async () => {
  serve(() => ok({}));
  mount({
    displayLabel: "Terminal",
    labelScope: "ORG",
    labelSource: "client",
    scopeLabels: {
      PLATFORM: null,
      ORG: { display_label: "Terminal" },
      PROJECT: null,
    },
  });

  await userEvent.click(screen.getByRole("radio", { name: SCOPES.PROJECT }));
  const input = (await screen.findByTestId(
    "dimension-label-input",
  )) as HTMLInputElement;
  expect(input.value).toBe(proposedLabel(DIMENSION));
  // Naming here OVERRIDES the organization, and the panel says so with the word
  // that is being overridden -- never silently.
  expect(screen.getByTestId("dimension-label-word").textContent).toContain(
    "Terminal",
  );
  expect(screen.getByTestId("dimension-label-save").textContent).toContain(
    "Name it",
  );
});

it("falls back to the winner when the envelope carries no per-scope answer", async () => {
  // An older envelope (or a read that could not resolve them) must not make the
  // panel WORSE than it was: for the winning scope the two questions coincide.
  serve(() => ok({}));
  mount({
    displayLabel: "Terminal",
    labelScope: "ORG",
    labelSource: "client",
    scopeLabels: null,
  });

  await userEvent.click(screen.getByRole("radio", { name: SCOPES.ORG }));
  const input = (await screen.findByTestId(
    "dimension-label-input",
  )) as HTMLInputElement;
  expect(input.value).toBe("Terminal");
});

it("still stores nothing until the button is pressed, whatever the scope carries", async () => {
  const calls = serve(() => ok({}));
  mount({
    displayLabel: "Appareil",
    labelScope: "PROJECT",
    labelSource: "client",
    scopeLabels: {
      PLATFORM: null,
      ORG: { display_label: "Terminal" },
      PROJECT: { display_label: "Appareil" },
    },
  });

  await userEvent.click(screen.getByRole("radio", { name: SCOPES.ORG }));
  await screen.findByTestId("dimension-label-input");
  expect(calls.some((call) => call.init?.method === "POST")).toBe(false);
});
