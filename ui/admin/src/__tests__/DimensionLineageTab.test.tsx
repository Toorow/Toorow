/**
 * The `lineage` tab of a Canonical Field (audit 2026-08-17, P2-7).
 *
 * WHAT THIS FILE HOLDS. `core/dimension_lineage.py` shipped with four routes and
 * the audit measured that no `.tsx` anywhere read `dimension-lineage`: three of
 * the four planes built, the screen absent, and no ratified posture saying it
 * should be. These tests hold the screen half:
 *
 *   1. it reads the fed-by address, keyed on the STABLE canonical name and not
 *      on the display label;
 *   2. a metric is told lineage is a dimension reading, rather than shown an
 *      empty table that would read as "nothing feeds it";
 *   3. `gaps[]` are rendered as loudly as the rows -- `get_fed_by` is fail-soft,
 *      so a short list may be an unread plan and never a small dimension;
 *   4. a failed read draws NO table: empty is not broken;
 *   5. the client label travels, and its absence is stated rather than hidden.
 *
 * `fetch` is stubbed and `apiFetch` is not: the seam guard is what proves the
 * bearer is attached, and stubbing the seam would prove nothing.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { DimensionLineageTab } from "../governance/DimensionLineageTab";

const PROJECT = "proj_EXAMPLE";

vi.mock("../shell/router", () => ({
  useRoute: () => ({ route: { projectId: PROJECT, organizationId: "org_EXAMPLE" } }),
}));

function ok(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

function fail(status: number, code: string): Response {
  return {
    ok: false,
    status,
    json: async () => ({ code, message: `refused: ${code}` }),
    text: async () => JSON.stringify({ code, message: `refused: ${code}` }),
  } as unknown as Response;
}

function serve(routes: Array<[RegExp, Response | (() => Response)]>) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      calls.push(url);
      for (const [pattern, answer] of routes) {
        if (pattern.test(url)) {
          return Promise.resolve(typeof answer === "function" ? answer() : answer);
        }
      }
      return Promise.resolve(fail(404, "not_found"));
    }),
  );
  return calls;
}

function detail(overrides: Record<string, unknown> = {}) {
  const summary = {
    canonical_name: "audience_language",
    concept_kind: "dimension",
    ...((overrides.summary as Record<string, unknown>) ?? {}),
  };
  return {
    object_ref: { id: "cf_1", label: "Audience Language", type: "canonical-field" },
    summary,
    ...overrides,
    // `summary` must win over a spread `overrides.summary`.
    ...(overrides.summary ? { summary } : {}),
  } as never;
}

function envelope(overrides: Record<string, unknown> = {}) {
  return {
    canonical_dimension: "audience_language",
    display_label: "Audience Language",
    label_scope: "PROJECT",
    label_source: "client",
    scope: { project_id: PROJECT, org_id: "org_EXAMPLE" },
    fed_by: [
      {
        connector: "example-ads",
        report_id: "daily",
        source_field: "browserLanguage",
        schema_link: "manifest",
        datastreams: [],
        mapping: { status: "confirmed" },
      },
    ],
    gaps: [],
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("DimensionLineageTab", () => {
  it("reads the fed-by address keyed on the stable canonical name, not the label", async () => {
    const calls = serve([[/dimension-lineage/, ok(envelope())]]);

    render(<DimensionLineageTab detail={detail()} />);
    await screen.findByTestId("dimension-lineage");

    expect(calls[0]).toBe(
      `/api/dimension-lineage/fed-by?project_id=${PROJECT}&canonical_dimension=audience_language`,
    );
    // The display label "Audience Language" must never be the key.
    expect(calls[0]).not.toContain("Audience%20Language");
  });

  it("lists every source column the published plans bind to the dimension", async () => {
    serve([[/dimension-lineage/, ok(envelope())]]);

    render(<DimensionLineageTab detail={detail()} />);

    await screen.findByTestId("lineage-row-example-ads-browserLanguage");
    expect(screen.getByText("daily")).toBeInTheDocument();
    expect(screen.getByText("Declared in the manifest")).toBeInTheDocument();
  });

  it("tells a metric that lineage is a dimension reading, and reads nothing", async () => {
    const calls = serve([[/dimension-lineage/, ok(envelope())]]);

    render(<DimensionLineageTab detail={detail({ summary: { concept_kind: "metric" } })} />);

    await screen.findByText("Lineage is a dimension reading");
    // An empty table would read as "nothing feeds it"; no call is made at all.
    expect(calls).toHaveLength(0);
  });

  it("renders the gaps as loudly as the rows, and says the list is partial", async () => {
    serve([
      [
        /dimension-lineage/,
        ok(
          envelope({
            gaps: [{ connector: "other-ads", reason: "no_plan_version" }],
          }),
        ),
      ],
    ]);

    render(<DimensionLineageTab detail={detail()} />);

    await screen.findByText(/1 connector\(s\) could not be read/);
    expect(
      screen.getByText(/no published plan version, so its columns are unknown/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/list below is therefore partial/i)).toBeInTheDocument();
  });

  it("draws NO table when the read fails: empty is not broken", async () => {
    serve([[/dimension-lineage/, fail(500, "server_error")]]);

    render(<DimensionLineageTab detail={detail()} />);

    await screen.findByText("The lineage could not be read");
    expect(screen.getByText(/not a dimension with no sources/i)).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByTestId("dimension-lineage")).toBeNull();
  });

  it("treats a 200 of the wrong shape as a failed read, not a reading", async () => {
    serve([[/dimension-lineage/, ok({ unexpected: true })]]);

    render(<DimensionLineageTab detail={detail()} />);

    await screen.findByText("The lineage could not be read");
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("says when no client named the dimension, and carries the gesture that names it", async () => {
    serve([
      [
        /dimension-lineage/,
        ok(
          envelope({
            display_label: null,
            label_scope: null,
            label_source: "fallback_identifier",
          }),
        ),
      ],
    ]);

    render(<DimensionLineageTab detail={detail()} />);

    await screen.findByTestId("dimension-lineage");
    expect(screen.getByTestId("dimension-label-unnamed")).toBeInTheDocument();
    // The gap is stated AND closeable here: before 2026-08-24 this tab printed
    // the absence and offered nothing.
    expect(screen.getByTestId("dimension-label")).toBeInTheDocument();
  });

  it("shows the client name and its scope when one was chosen", async () => {
    serve([[/dimension-lineage/, ok(envelope())]]);

    render(<DimensionLineageTab detail={detail()} />);

    await screen.findByTestId("dimension-label-named");
    expect(screen.getByText(/Named for this project/i)).toBeInTheDocument();
  });

  it("names the gesture that fills an empty lineage", async () => {
    serve([[/dimension-lineage/, ok(envelope({ fed_by: [] }))]]);

    render(<DimensionLineageTab detail={detail()} />);

    await screen.findByText(/No published plan declares a column feeding this dimension/);
    await waitFor(() =>
      expect(screen.getByText(/Bind one on the Datastream's Map tab/i)).toBeInTheDocument(),
    );
  });
});
