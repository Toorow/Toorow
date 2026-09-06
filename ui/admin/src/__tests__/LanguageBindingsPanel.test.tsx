/**
 * "Language" is three dimensions — the declaration gesture, on screen.
 *
 * Four things are held here, and each of them is a sentence the target owes:
 *
 *   1. the three dimensions come from the SERVER, never re-spelled in the
 *      console — a second copy of the family is a second thing free to drift
 *      from the guard that refuses to compare them;
 *   2. a declaration carries the whole key `(connector, report_id, source_field,
 *      canonical_dimension)`, and the control is NOT offered when the report is
 *      unknown — a control that writes an identifier a person did not choose is
 *      worse than an absent one;
 *   3. a column somebody already decided shows the decision and the way to take
 *      it back, never a second "Declare";
 *   4. a read that fails says so and draws no table underneath.
 *
 * `fetch` is stubbed and `apiFetch` is not: the seam guard is what proves the
 * bearer is attached, and stubbing the seam would prove nothing.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import LanguageBindingsPanel from "../governance/LanguageBindingsPanel";

const PROJECT = "proj_EXAMPLE";
const CONNECTOR = "google-analytics";
const REPORT = "catalog_daily";

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

const FAMILY = [
  {
    dimension: "audience_language",
    nature: "observed_on_person",
    definition: "Language OBSERVED on the person reached.",
  },
  {
    dimension: "content_language",
    nature: "property_of_asset",
    definition: "Language of the ASSET actually served.",
  },
  {
    dimension: "targeting_language",
    nature: "declared_intent",
    definition: "Language DECLARED as targeted.",
  },
];

function envelope(bindings: unknown[] = []) {
  return {
    schema: "language_bindings.v1",
    project_id: PROJECT,
    family: FAMILY,
    bindings,
    count: bindings.length,
  };
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

function mount(props: Partial<React.ComponentProps<typeof LanguageBindingsPanel>> = {}) {
  return render(
    <LanguageBindingsPanel
      projectId={PROJECT}
      connector={CONNECTOR}
      reportId={REPORT}
      columns={["language", "device_category"]}
      {...props}
    />,
  );
}

it("names the three dimensions the SERVER sent, and none of its own", async () => {
  serve(() => ok(envelope()));
  mount();
  await screen.findByTestId("language-bindings");
  for (const member of FAMILY) {
    expect(await screen.findAllByText(member.dimension)).not.toHaveLength(0);
  }
  // The stored nature token is never printed at a person; the sentence is.
  expect(screen.queryByText("observed_on_person")).toBeNull();
  expect(
    screen.getAllByText("Measured on the person reached.").length,
  ).toBeGreaterThan(0);
});

it("declares the WHOLE key, so the report is part of what was decided", async () => {
  const calls = serve((_url, init) =>
    init?.method === "POST" ? ok({ ok: true }) : ok(envelope()),
  );
  mount();
  await screen.findByTestId("language-row-language");

  await userEvent.click(screen.getAllByRole("radio", { name: /audience_language/ })[0]!);
  await userEvent.click(screen.getByTestId("language-declare-language"));

  await waitFor(() => {
    expect(calls.some((call) => call.init?.method === "POST")).toBe(true);
  });
  const post = calls.find((call) => call.init?.method === "POST")!;
  expect(post.url).toContain(`/api/projects/${PROJECT}/language-bindings`);
  expect(JSON.parse(String(post.init?.body))).toEqual({
    connector: CONNECTOR,
    report_id: REPORT,
    source_field: "language",
    canonical_dimension: "audience_language",
  });
});

it("offers NO declaration when the report is unknown, and says why", async () => {
  serve(() => ok(envelope()));
  mount({ reportId: null });
  const notice = await screen.findByTestId("language-bindings-incomplete-key");
  expect(notice.textContent).toContain("names no report");
  expect(screen.queryByTestId("language-declare-language")).toBeNull();
});

it("offers NO declaration when the Datastream names no connector", async () => {
  serve(() => ok(envelope()));
  mount({ connector: null });
  const notice = await screen.findByTestId("language-bindings-incomplete-key");
  expect(notice.textContent).toContain("names no connector");
  expect(screen.queryByTestId("language-declare-language")).toBeNull();
});

it("shows a decision already taken, and the way to take it back", async () => {
  serve(() =>
    ok(
      envelope([
        {
          id: "dlb_EXAMPLE",
          connector: CONNECTOR,
          report_id: REPORT,
          source_field: "language",
          canonical_dimension: "targeting_language",
          status: "confirmed",
          scope_level: "PROJECT",
        },
      ]),
    ),
  );
  mount();
  await screen.findByTestId("language-row-language");
  expect(screen.getByTestId("language-retire-language")).toBeTruthy();
  expect(screen.queryByTestId("language-declare-language")).toBeNull();
  // The column nobody decided still offers the gesture: the state is per column.
  expect(screen.getByTestId("language-declare-device_category")).toBeTruthy();
});

it("a proposal nobody confirmed is not a decision", async () => {
  serve(() =>
    ok(
      envelope([
        {
          id: "dlb_EXAMPLE",
          connector: CONNECTOR,
          report_id: REPORT,
          source_field: "language",
          canonical_dimension: "audience_language",
          status: "proposed",
        },
      ]),
    ),
  );
  mount();
  await screen.findByTestId("language-row-language");
  expect(screen.getByTestId("language-declare-language")).toBeTruthy();
  expect(screen.queryByTestId("language-retire-language")).toBeNull();
});

it("offers a column the shipped catalog does not describe", async () => {
  // THE TIKTOK CASE, and the reason this panel closes it. `target_languages` exists
  // only in `modules/tiktok-ads/catalog_sources/fusion-report.json`, as a bare NAME
  // in a list: no description, no type. The classifier judges on the provider's own
  // words, so with no words it can propose nothing, and the 2026-08-01 review
  // recorded the field as outside the proposal flow entirely.
  //
  // The columns offered here are the MAPPING's, never the catalog's — what this
  // Datastream actually binds. So a person can decide what an undescribed column
  // carries, which is exactly what a client binding is for. Filtering this list on
  // the catalog would close that door again, silently.
  const calls = serve((_url, init) =>
    init?.method === "POST" ? ok({ ok: true }) : ok(envelope()),
  );
  mount({ connector: "tiktok-ads", columns: ["target_languages"] });
  await screen.findByTestId("language-row-target_languages");

  await userEvent.click(
    screen.getAllByRole("radio", { name: /targeting_language/ })[0]!,
  );
  await userEvent.click(screen.getByTestId("language-declare-target_languages"));

  await waitFor(() => {
    expect(calls.some((call) => call.init?.method === "POST")).toBe(true);
  });
  expect(
    JSON.parse(String(calls.find((c) => c.init?.method === "POST")!.init?.body)),
  ).toMatchObject({
    connector: "tiktok-ads",
    source_field: "target_languages",
    canonical_dimension: "targeting_language",
  });
});

it("a read that failed says so and draws no table", async () => {
  serve(() => fail(500, "server_error", "Server error."));
  mount();
  expect(await screen.findByText("Not readable")).toBeTruthy();
  expect(screen.queryByTestId("language-row-language")).toBeNull();
});

it("a 200 that carries no family is a failed read, not an empty one", async () => {
  serve(() => ok({ schema: "language_bindings.v1", project_id: PROJECT }));
  mount();
  expect(await screen.findByText("Not readable")).toBeTruthy();
});
