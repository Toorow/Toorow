/**
 * Story 50.4 — the Builder: three parts, ten wells, and honest states.
 *
 * The fixtures below are the shapes `core.visualization_specs_api` really
 * returns, including the two this repository really produces today: a registry
 * whose `time` and `classification` roles are UNAVAILABLE with a reason, and an
 * `unavailable` Result whose missing link is `datastream_output_versions`.
 *
 * Testing those is the point. A Builder that has only ever been shown a happy
 * path is a Builder that renders an empty Time well as if it were waiting for
 * input, and the person using it concludes their Semantic View has no dates.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../analyze/visualizationClient", async () => {
  const actual = await vi.importActual<typeof import("../analyze/visualizationClient")>(
    "../analyze/visualizationClient",
  );
  return {
    ...actual,
    fetchVisualizationFamilies: vi.fn(),
    fetchVisualizationOptions: vi.fn(),
    fetchVisualization: vi.fn(),
    fetchVisualizationSpecVersion: vi.fn(),
    validateVisualizationSpecVersion: vi.fn(),
    createVisualization: vi.fn(),
    createCanonicalRender: vi.fn(),
    addVisualizationVersion: vi.fn(),
    fetchResultPin: vi.fn(),
  };
});

vi.mock("../analyze/queryClient", async () => {
  const actual = await vi.importActual<typeof import("../analyze/queryClient")>(
    "../analyze/queryClient",
  );
  return { ...actual, fetchResultEvidence: vi.fn() };
});

import { ApiError } from "../lib/apiFetch";
import VisualizationBuilder from "../analyze/builder/VisualizationBuilder";
import {
  createCanonicalRender,
  createVisualization,
  fetchVisualization,
  fetchVisualizationFamilies,
  fetchVisualizationOptions,
  fetchVisualizationSpecVersion,
  fetchResultPin,
  validateVisualizationSpecVersion,
} from "../analyze/visualizationClient";
import { fetchResultEvidence } from "../analyze/queryClient";

const SCOPE = { organizationId: "org_EXAMPLE", projectId: "proj_EXAMPLE" };
const PIN = "qsv_EXAMPLE";

const REASON =
  "Time and classification roles are not yet carried by the compiled Semantic View, " +
  "so this group cannot be populated. The semantic compiler owns that field.";
const OWNER = "Semantic compiler (Epics 47 / 49)";

/** The ten wells, exactly as `visualization-and-rendering.md:98-99` names them. */
const WELL_LABELS = [
  "Measure",
  "Dimension",
  "Time",
  "Series",
  "Breakdown",
  "Facet",
  "Color",
  "Size",
  "Label",
  "Detail",
];

function well(name: string, label: string, accepts: string[], required = false, max = 1) {
  const available = accepts.some((r) => r === "measure" || r === "dimension");
  return {
    name,
    label,
    accepts,
    required,
    max_members: max,
    max_cardinality: accepts.includes("dimension") ? 50 : null,
    available,
    unavailable_reason: available ? null : REASON,
    unavailable_owner: available ? null : OWNER,
  };
}

const BAR = {
  id: "bar",
  label: "Bar",
  description: "A measure compared across the members of one dimension.",
  wells: [
    well("measure", "Measure", ["measure"], true, 3),
    well("dimension", "Dimension", ["dimension"], true, 1),
    well("time", "Time", ["time"]),
    well("series", "Series", ["dimension"]),
    well("color", "Color", ["dimension"]),
    well("label", "Label", ["dimension", "measure"], false, 2),
  ],
  requires_time_grain: false,
  requires_comparison: false,
  max_marks: 400,
  capabilities: ["categorical_axis"],
  table_fallback_wells: ["dimension", "series", "measure"],
  table_fallback: "required",
};

const LINE = { ...BAR, id: "line", label: "Line", requires_time_grain: true };

const REGISTRY = {
  families: [BAR, LINE],
  wells: WELL_LABELS.map((label) => ({
    name: label.toLowerCase(),
    label,
    accepts:
      label === "Time"
        ? ["time"]
        : label === "Measure" || label === "Size"
          ? ["measure"]
          : ["dimension"],
  })),
  roles: [
    { role: "measure", available: true, reason: null, owner: null },
    { role: "dimension", available: true, reason: null, owner: null },
    { role: "time", available: false, reason: REASON, owner: OWNER },
    { role: "classification", available: false, reason: REASON, owner: OWNER },
  ],
  deferred_families: [{ id: "heatmap", reason: "needs a two-dimensional cell grid" }],
  max_inline_rows: 1000,
  spec_contract_version: "visualization-spec.v1",
  schema_version: 1,
  responsive_profiles: ["console", "mcp-inline", "mcp-fullscreen", "mcp-pip", "share"],
  grammar_keys: ["spec_contract_version", "schema_version", "family", "bindings"],
};

/** AC7's five facets, in the server's order. */
const FACETS = ["definition", "grain", "additivity", "quality_state", "provenance_hint"];

const FACET_OWNERS: Record<string, string> = {
  definition: "Semantic Model - concept definition (Epics 47 / 49)",
  grain: "Semantic Model - allowed grains (Epics 47 / 49)",
  additivity: "Semantic Model - additivity class (Epics 47 / 49)",
  quality_state: "Data Quality monitors (Epic 33)",
  provenance_hint: "Semantic Model - concept provenance (Epics 47 / 49)",
};

/** Every facet either carries a value or an explicit, owned absence. */
function metadata(values: Record<string, string | null> = {}) {
  return Object.fromEntries(
    FACETS.map((facet) => [facet, { value: values[facet] ?? null, owner: FACET_OWNERS[facet] }]),
  );
}

const OPTIONS = {
  query_spec_id: "qs_EXAMPLE",
  query_spec_version_id: PIN,
  semantic_view_id: "sv_EXAMPLE",
  semantic_view_version_id: "svv_EXAMPLE",
  measures: [
    {
      id: "clicks",
      version_id: "mv_1",
      label: "clicks",
      role: "measure",
      metadata: metadata({
        definition: "Clicks recorded on a governed placement.",
        grain: "day, week",
        additivity: "additive",
        quality_state: "degraded",
        provenance_hint: "recorded_by: fixture",
      }),
    },
  ],
  dimensions: [
    {
      id: "channel",
      version_id: "dv_1",
      label: "channel",
      role: "dimension",
      // Nothing recorded: five absences, each naming its owner.
      metadata: metadata(),
    },
    { id: "date", version_id: "dv_2", label: "date", role: "dimension", metadata: metadata() },
  ],
  time: [],
  classifications: [],
  roles: REGISTRY.roles,
  member_metadata_facets: FACETS,
  unavailable_reason: REASON,
  unavailable_owner: OWNER,
  grain: "day",
  comparison: "none",
  row_limit: 1000,
  analysis_context: {
    contract_version: "analysis-context.v1",
    semantic_view_version_id: "svv_EXAMPLE",
    business_domain: {
      id: "bd_paid_media",
      version_number: 4,
      version_id: "bd_paid_media:4",
      name: "Paid media",
    },
    golden_question: {
      id: "gq_roas",
      version_id: "gqv_roas_3",
      version_number: 3,
      title: "Are campaign investments producing qualified conversions?",
      content_hash: "a".repeat(64),
    },
    requested_skills: [
      {
        procedure_id: "skill_paid_media",
        version_number: 7,
        version_id: "skill_paid_media@7",
        name: "Paid media investigation",
      },
    ],
  },
};

/** The state this repository really produces today. */
const UNAVAILABLE_EVIDENCE = {
  result_id: "qr_EXAMPLE",
  content_hash: "h".repeat(64),
  schema: { fields: [] },
  manifest: {
    outcome: "unavailable",
    unavailable_reason: "this Datastream has no published output to query yet",
    missing_link: "datastream_output_versions",
  },
  rows: [],
};

function builder(overrides: Record<string, unknown> = {}) {
  return render(
    <VisualizationBuilder
      scope={SCOPE}
      visualizationId={null}
      visualizationSpecVersionId={null}
      resultId={null}
      querySpecVersionId={PIN}
      tab="build"
      onNavigateTab={() => {}}
      tabHref={(tab) => `/build/${tab}`}
      exploreHref="/explore"
      resultHref={(id) => `/result/${id}`}
      {...overrides}
    />,
  );
}

beforeEach(() => {
  vi.mocked(fetchVisualizationFamilies).mockResolvedValue(REGISTRY as never);
  vi.mocked(fetchVisualizationOptions).mockResolvedValue(OPTIONS as never);
  vi.mocked(fetchResultEvidence).mockResolvedValue(UNAVAILABLE_EVIDENCE as never);
  vi.mocked(fetchVisualization).mockReset();
  vi.mocked(fetchVisualizationSpecVersion).mockReset();
  vi.mocked(validateVisualizationSpecVersion).mockReset();
  vi.mocked(createVisualization).mockReset();
  vi.mocked(createCanonicalRender).mockReset();
  vi.mocked(fetchResultPin).mockReset();
});

// ---------------------------------------------------------------------------
// AC7 — three visibly separate parts.
// ---------------------------------------------------------------------------

describe("the three-part composition", () => {
  it("renders the field catalog, the wells and the presentation rail", async () => {
    builder();
    expect(await screen.findByRole("heading", { name: "Field catalog" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Binding wells" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Presentation" })).toBeInTheDocument();
  });

  it("carries exactly one H1 and a main landmark", async () => {
    builder();
    await screen.findByRole("heading", { name: "Field catalog" });
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("main")).toBeInTheDocument();
  });

  it("uses route-backed tabs that stay copyable", async () => {
    builder();
    await screen.findByRole("heading", { name: "Field catalog" });
    const tabs = screen.getByRole("navigation", { name: "Visualization" });
    expect(within(tabs).getByRole("link", { name: "Build" })).toHaveAttribute("href", "/build/build");
    expect(within(tabs).getByRole("link", { name: "Versions" })).toHaveAttribute(
      "href",
      "/build/versions",
    );
  });
});

// ---------------------------------------------------------------------------
// AC7 — the four rail groups, two of them empty WITH THEIR REASON.
// ---------------------------------------------------------------------------

describe("the governed field catalog", () => {
  it("renders all four groups", async () => {
    builder();
    await screen.findByRole("heading", { name: "Field catalog" });
    for (const group of ["Metrics", "Dimensions", "Time", "Classifications"]) {
      expect(screen.getByRole("heading", { name: group })).toBeInTheDocument();
    }
  });

  it("renders Time and Classifications empty, and says why and who owns it", async () => {
    builder();
    await screen.findByRole("heading", { name: "Field catalog" });
    for (const group of ["time", "classifications"]) {
      const banner = screen.getByTestId(`catalog-${group}-unavailable`);
      expect(banner).toHaveTextContent(REASON);
      expect(banner).toHaveTextContent(OWNER);
    }
  });

  it("never invents a time member out of a member whose name reads like a date", async () => {
    builder();
    await screen.findByRole("heading", { name: "Field catalog" });
    // `date` exists — as a DIMENSION, which is what the server said it is.
    const member = screen.getByTestId("catalog-member-date");
    expect(member).toHaveTextContent("dimension");
    // And the Time group is still empty.
    expect(screen.getByTestId("catalog-time-unavailable")).toBeInTheDocument();
  });

  it("says adding a member is a query change and links to Explore", async () => {
    builder();
    await screen.findByRole("heading", { name: "Field catalog" });
    expect(screen.getByText(/Adding a member is a query change/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Open Explore/ })).toHaveAttribute("href", "/explore");
  });

  it("states plainly when Explore is not mounted instead of linking to a 404", async () => {
    builder({ exploreHref: null });
    await screen.findByRole("heading", { name: "Field catalog" });
    expect(screen.getByText(/Explore is not mounted in this build/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Open Explore/ })).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// AC7 — the wells, named by semantic role and by nothing else.
// ---------------------------------------------------------------------------

describe("the binding wells", () => {
  it("renders all ten wells with exactly the semantic-role labels", async () => {
    builder();
    await screen.findByRole("heading", { name: "Binding wells" });
    for (const label of WELL_LABELS) {
      expect(screen.getByTestId(`well-${label.toLowerCase()}`)).toHaveTextContent(label);
    }
  });

  it("uses no renderer-library vocabulary anywhere in the rendered copy", async () => {
    const { container } = builder();
    await screen.findByRole("heading", { name: "Binding wells" });
    const copy = container.textContent?.toLowerCase() ?? "";
    for (const word of ["xaxis", "yaxis", "encode", "dataset", "datakey", "echarts", "d3."]) {
      expect(copy).not.toContain(word);
    }
  });

  it("uses none of the retired glossary words", async () => {
    const { container } = builder();
    await screen.findByRole("heading", { name: "Binding wells" });
    const copy = container.textContent?.toLowerCase() ?? "";
    // `Widget` is a presentation surface and is never a name for a Visualization;
    // `Module` and `Extension` are retired (glossary.md:156-158).
    for (const word of ["widget", "extension"]) {
      expect(copy).not.toContain(word);
    }
  });

  it("renders the Time well disabled, with its reason in its accessible description", async () => {
    builder();
    await screen.findByRole("heading", { name: "Binding wells" });
    const disabled = screen.getByTestId("well-time");
    expect(disabled).toHaveAttribute("aria-disabled", "true");
    const describedBy = disabled.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    expect(document.getElementById(describedBy!)).toHaveTextContent(REASON);
  });

  it("gives a disabled well no control at all, by pointer or by keyboard", async () => {
    builder();
    await screen.findByRole("heading", { name: "Binding wells" });
    const timeWell = screen.getByTestId("well-time");
    expect(within(timeWell).queryByRole("combobox")).toBeNull();
    expect(within(timeWell).queryByRole("button")).toBeNull();
    // Nothing inside it is reachable by keyboard either.
    expect(timeWell.querySelectorAll("select, input, button, [tabindex]")).toHaveLength(0);
  });

  // The parity clause of `visualization-and-rendering.md`, *Composition is
  // direct manipulation*: the drag exists, and NOTHING depends on it. This test
  // fires no drag event at all.
  it("binds a member from the keyboard alone, with no drag interaction", async () => {
    const user = userEvent.setup();
    builder();
    await screen.findByRole("heading", { name: "Binding wells" });
    await user.click(within(screen.getByTestId("catalog-member-clicks")).getByRole("button"));
    await user.click(screen.getByTestId("well-measure-place"));
    await waitFor(() =>
      expect(
        within(screen.getByTestId("well-measure")).getByRole("button", {
          name: "Remove clicks from Measure",
        }),
      ).toBeInTheDocument(),
    );
    // And it is gone from no other well: one member, one binding, one place.
    expect(screen.getByTestId("well-dimension")).not.toHaveTextContent("clicks");
  });

  it("offers only the members whose server-declared role the well accepts", async () => {
    const user = userEvent.setup();
    builder();
    await screen.findByRole("heading", { name: "Binding wells" });

    // A measure in the air: Measure offers to take it, Dimension refuses it in
    // the words of the role it does take.
    await user.click(within(screen.getByTestId("catalog-member-clicks")).getByRole("button"));
    expect(screen.getByTestId("well-measure-place")).toBeInTheDocument();
    expect(screen.queryByTestId("well-dimension-place")).toBeNull();
    expect(screen.getByTestId("well-dimension-refusal")).toHaveTextContent(
      "Dimension takes dimension, and clicks is a measure.",
    );

    // A dimension in the air: exactly the other way round.
    await user.click(within(screen.getByTestId("catalog-member-channel")).getByRole("button"));
    expect(screen.getByTestId("well-dimension-place")).toBeInTheDocument();
    expect(screen.queryByTestId("well-measure-place")).toBeNull();
    expect(screen.getByTestId("well-measure-refusal")).toHaveTextContent(
      "Measure takes measure, and channel is a dimension.",
    );
  });

  it("puts a carried member back down on Escape, binding nothing", async () => {
    const user = userEvent.setup();
    builder();
    await screen.findByRole("heading", { name: "Binding wells" });
    await user.click(within(screen.getByTestId("catalog-member-clicks")).getByRole("button"));
    expect(screen.getByTestId("well-measure-place")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByTestId("well-measure-place")).toBeNull();
    expect(
      within(screen.getByTestId("well-measure")).queryByRole("button", {
        name: "Remove clicks from Measure",
      }),
    ).toBeNull();
  });

  it("marks a well the family does not declare as unavailable rather than hiding it", async () => {
    builder();
    await screen.findByRole("heading", { name: "Binding wells" });
    // `bar` declares no Facet well; the vocabulary entry is still rendered.
    expect(screen.getByTestId("well-facet")).toHaveTextContent(
      "The Bar family declares no Facet well.",
    );
  });
});

// ---------------------------------------------------------------------------
// AC4 / AC7 — a refusal lands on its own well, with its remedy.
// ---------------------------------------------------------------------------

describe("refusals", () => {
  it("attaches each refusal to the well it is about, not to one page banner", async () => {
    const user = userEvent.setup();
    vi.mocked(createVisualization).mockRejectedValue(
      new ApiError(422, "visualization_spec_refused", "refused", {
        code: "visualization_spec_refused",
        message: "the presentation was refused on 2 point(s)",
        refusals: [
          {
            code: "role_mismatch",
            subject: "/bindings/dimension/0",
            message: "`clicks` is a measure; the Dimension well accepts dimension.",
            remedy: "Bind `channel` here, or choose the KPI family.",
          },
          {
            code: "missing_binding",
            subject: "/bindings/measure",
            message: "the Bar family needs a member in Measure",
            remedy: "Bind one of the members the Query Spec selected into Measure.",
          },
        ],
      }),
    );
    builder();
    await screen.findByRole("heading", { name: "Binding wells" });
    await user.click(screen.getByRole("button", { name: /Save a new version/ }));

    await waitFor(() => {
      expect(screen.getByTestId("well-dimension")).toHaveTextContent("`clicks` is a measure");
    });
    expect(screen.getByTestId("well-dimension")).toHaveTextContent("Bind `channel` here");
    expect(screen.getByTestId("well-measure")).toHaveTextContent("needs a member in Measure");
    // The dimension refusal did NOT also land on the measure well.
    expect(screen.getByTestId("well-measure")).not.toHaveTextContent("is a measure;");
  });

  it("announces the refusal count through a bounded live region", async () => {
    const user = userEvent.setup();
    vi.mocked(createVisualization).mockRejectedValue(
      new ApiError(422, "visualization_spec_refused", "refused", {
        code: "visualization_spec_refused",
        message: "refused",
        refusals: [
          { code: "unknown_member", subject: "/bindings/measure/0", message: "no", remedy: "do" },
        ],
      }),
    );
    builder();
    await screen.findByRole("heading", { name: "Binding wells" });
    await user.click(screen.getByRole("button", { name: /Save a new version/ }));
    await waitFor(() =>
      expect(screen.getByText(/refused on 1 point/)).toBeInTheDocument(),
    );
  });
});

// ---------------------------------------------------------------------------
// AC9 — the states are distinct, and `unavailable` is proved against the real shape.
// ---------------------------------------------------------------------------

describe("preview states", () => {
  it("explains that a draft must be saved before the shared renderer mounts", async () => {
    const { container } = builder();
    await screen.findByTestId("no-renderer");
    expect(screen.getByTestId("no-renderer")).toHaveTextContent("Save to render this draft");
    expect(container.querySelector("svg.chart, canvas")).toBeNull();
  });

  it("renders `unknown` when no Result is pinned, and not `empty`", async () => {
    builder();
    expect(await screen.findByTestId("preview-state-unknown")).toBeInTheDocument();
    expect(screen.queryByTestId("preview-state-empty")).toBeNull();
  });

  it("renders `unavailable` with the exact reason and missing link the manifest carries", async () => {
    builder({ resultId: "qr_EXAMPLE" });
    expect(await screen.findByTestId("preview-state-unavailable")).toBeInTheDocument();
    expect(screen.getByTestId("unavailable-reason")).toHaveTextContent(
      "this Datastream has no published output to query yet",
    );
    expect(screen.getByTestId("missing-link")).toHaveTextContent("datastream_output_versions");
    // `empty` and `unavailable` are NOT the same state.
    expect(screen.queryByTestId("preview-state-empty")).toBeNull();
  });

  it("renders `empty` differently from `unavailable`", async () => {
    vi.mocked(fetchResultEvidence).mockResolvedValue({
      ...UNAVAILABLE_EVIDENCE,
      manifest: { outcome: "empty" },
    } as never);
    builder({ resultId: "qr_EXAMPLE" });
    expect(await screen.findByTestId("preview-state-empty")).toBeInTheDocument();
    expect(screen.getByText("No rows matched")).toBeInTheDocument();
    expect(screen.queryByTestId("unavailable-reason")).toBeNull();
  });

  it("renders `denied` indistinguishably from nonexistent", async () => {
    vi.mocked(fetchVisualization).mockRejectedValue(new ApiError(404, "not_found", "Not found"));
    builder({ visualizationId: "vis_FOREIGN" });
    expect(await screen.findByRole("heading", { name: "Not found" })).toBeInTheDocument();
  });

  it("shows an unknown tab as Unknown rather than falling back to build", async () => {
    builder({ tab: "chart" });
    expect(await screen.findByRole("heading", { name: "Unknown tab" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Binding wells" })).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// AC8 — the accessible table fallback, and the disclosures beside it.
// ---------------------------------------------------------------------------

describe("the accessible table fallback", () => {
  const ROWS = {
    ...UNAVAILABLE_EVIDENCE,
    schema: { fields: [{ name: "channel" }, { name: "clicks" }] },
    manifest: { outcome: "success", truncated: true },
    rows: [
      { channel: "paid", clicks: 12 },
      { channel: "organic", clicks: 7 },
    ],
  };

  it("renders a native table inside a keyboard-focusable scroller", async () => {
    vi.mocked(fetchResultEvidence).mockResolvedValue(ROWS as never);
    builder({ resultId: "qr_EXAMPLE" });
    await screen.findByRole("heading", { name: "Accessible table" });
    const scroller = screen.getByRole("region", { name: "Accessible table fallback" });
    expect(scroller).toHaveAttribute("tabindex", "0");
    expect(within(scroller).getByRole("table")).toBeInTheDocument();
    expect(within(scroller).getAllByRole("row")).toHaveLength(3);
  });

  it("states the freshness field is absent rather than rendering it as fresh", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchResultEvidence).mockResolvedValue(ROWS as never);
    vi.mocked(fetchVisualization).mockResolvedValue({
      id: "vis_EXAMPLE",
      query_spec_id: "qs_EXAMPLE",
      name: null,
      current_version_id: "vsv_EXAMPLE",
      created_by: "owner@example.com",
      created_at: null,
      updated_at: null,
      versions: [
        {
          id: "vsv_EXAMPLE",
          version_number: 1,
          family: "bar",
          content_hash: "c".repeat(64),
          query_spec_version_id: PIN,
          predecessor_version_id: null,
          proposed_by: "person",
          created_by: "owner@example.com",
          created_at: null,
        },
      ],
    } as never);
    vi.mocked(fetchVisualizationSpecVersion).mockResolvedValue({
      id: "vsv_EXAMPLE",
      visualization_id: "vis_EXAMPLE",
      version_number: 1,
      query_spec_id: "qs_EXAMPLE",
      query_spec_version_id: PIN,
      spec_contract_version: "visualization-spec.v1",
      schema_version: 1,
      family: "bar",
      spec: {
        spec_contract_version: "visualization-spec.v1",
        schema_version: 1,
        family: "bar",
        bindings: { measure: ["clicks"], dimension: ["channel"] },
      },
      content_hash: "c".repeat(64),
      predecessor_version_id: null,
      proposed_by: "person",
      created_by: "owner@example.com",
      created_at: null,
    } as never);
    vi.mocked(validateVisualizationSpecVersion).mockResolvedValue({
      visualization_spec_version_id: "vsv_EXAMPLE",
      query_spec_version_id: PIN,
      family: "bar",
      shape: { compatible: true, refusals: [] },
      result_disclosures: {
        compatible: true,
        outcome: "success",
        returned_row_count: 2,
        truncated: true,
        marks: 2,
        cardinalities: {},
        family_max_marks: 400,
        refusals: [],
        table_fallback: "required",
        table_fallback_columns: ["dimension", "measure"],
      },
      result_id: "qr_EXAMPLE",
    } as never);
    vi.mocked(createCanonicalRender).mockResolvedValue({
      id: "rnd_EXAMPLE",
      result_id: "qr_EXAMPLE",
      content_hash: "r".repeat(64),
      created_at: null,
    });

    builder({ visualizationId: "vis_EXAMPLE", resultId: "qr_EXAMPLE" });
    await screen.findByTestId("freshness");
    expect(screen.getByTestId("freshness")).toHaveTextContent(
      "the Result manifest does not carry a freshness field yet",
    );
    expect(screen.getByTestId("returned-row-count")).toHaveTextContent("2");
    expect(screen.getByTestId("truncated-flag")).toHaveTextContent("yes");
    await user.click(screen.getByRole("button", { name: "Freeze this Render" }));
    await waitFor(() => expect(createCanonicalRender).toHaveBeenCalledTimes(1));
    expect(createCanonicalRender).toHaveBeenCalledWith(
      SCOPE.projectId,
      expect.objectContaining({
        result_id: "qr_EXAMPLE",
        result_content_hash: ROWS.content_hash,
        visualization_spec_version_id: "vsv_EXAMPLE",
        responsive_profile: "console",
        creation_surface: "explore",
      }),
    );
    expect(screen.getByText("rnd_EXAMPLE")).toBeInTheDocument();
  });

  it("shows the business intent inherited from the pinned Query Spec", async () => {
    builder();

    const context = await screen.findByTestId("builder-analysis-context");
    expect(within(context).getByText("Paid media")).toBeInTheDocument();
    expect(
      within(context).getByText("Are campaign investments producing qualified conversions?"),
    ).toBeInTheDocument();
    expect(within(context).getByText("Paid media investigation · v7")).toBeInTheDocument();
    expect(context).toHaveTextContent("Inherited from this exact Query Spec version");
  });
});

// ---------------------------------------------------------------------------
// AC13 — the evidence drawer gives the keyboard back.
// ---------------------------------------------------------------------------

describe("the evidence drawer", () => {
  it("restores focus to its invoker when it closes", async () => {
    const user = userEvent.setup();
    builder();
    const invoker = await screen.findByRole("button", { name: "Show the saved plan" });
    await user.click(invoker);
    await screen.findByRole("dialog");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(invoker).toHaveFocus());
  });

  it("says plainly when the Result workbench is not mounted", async () => {
    builder({ resultId: "qr_EXAMPLE", resultHref: null });
    await screen.findByRole("heading", { name: "Inspect the evidence" });
    expect(
      screen.getByText(/The Result workbench is not mounted in this build/),
    ).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// AC7 — recommendations are deterministic and come from the server's rules.
// ---------------------------------------------------------------------------

describe("family suitability", () => {
  it("marks a family that needs a grain as incompatible when the query pinned none", async () => {
    vi.mocked(fetchVisualizationOptions).mockResolvedValue({
      ...OPTIONS,
      grain: null,
    } as never);
    builder();
    const select = await screen.findByLabelText("Family");
    expect(within(select).getByRole("option", { name: /Line/ })).toHaveTextContent(
      "needs a time grain; this query pinned none",
    );
    expect(within(select).getByRole("option", { name: /^Bar$/ })).toBeInTheDocument();
  });

  it("keeps the deliberately absent families visible as remaining work", async () => {
    builder();
    await screen.findByLabelText("Family");
    expect(
      screen.getByText(/families named by the architecture are\s+not in this release/),
    ).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// AC7 — the rail's five facets per member, SERVED or explicitly absent.
//
// The rail used to print `{role} · pinned version …` and nothing else: AC7's
// definition, grain, additivity, quality state and provenance hint were neither
// implemented nor declared absent. Silence is the failure mode this whole story
// spends its `Time` and `Classifications` clauses forbidding, one level down.
// ---------------------------------------------------------------------------

describe("the rail's per-member metadata", () => {
  it("renders every facet the server declares, for every member", async () => {
    builder();
    await screen.findByRole("heading", { name: "Field catalog" });
    for (const member of ["clicks", "channel", "date"]) {
      for (const facet of FACETS) {
        expect(screen.getByTestId(`member-${member}-${facet}`)).toBeInTheDocument();
      }
    }
  });

  it("shows the values the server read", async () => {
    builder();
    await screen.findByRole("heading", { name: "Field catalog" });
    expect(screen.getByTestId("member-clicks-definition")).toHaveTextContent(
      "Clicks recorded on a governed placement.",
    );
    expect(screen.getByTestId("member-clicks-grain")).toHaveTextContent("day, week");
    expect(screen.getByTestId("member-clicks-additivity")).toHaveTextContent("additive");
    expect(screen.getByTestId("member-clicks-quality_state")).toHaveTextContent("degraded");
    expect(screen.getByTestId("member-clicks-provenance_hint")).toHaveTextContent(
      "recorded_by: fixture",
    );
  });

  it("states an unrecorded facet as an absence that names its owner", async () => {
    builder();
    await screen.findByRole("heading", { name: "Field catalog" });
    const cell = screen.getByTestId("member-channel-definition");
    expect(cell).toHaveTextContent("Not recorded");
    expect(cell).toHaveTextContent("Semantic Model - concept definition (Epics 47 / 49)");
    // A blank cell would read as "nothing to see here" rather than as "another
    // surface has not written this yet".
    expect(cell.textContent?.trim()).not.toBe("");
  });

  it("never invents a quality state for an unmonitored member", async () => {
    builder();
    await screen.findByRole("heading", { name: "Field catalog" });
    const cell = screen.getByTestId("member-channel-quality_state");
    expect(cell).not.toHaveTextContent("healthy");
    expect(cell).toHaveTextContent("Data Quality monitors (Epic 33)");
  });

  it("renders the SERVER's facet list, not a list retyped in the browser", async () => {
    // A sixth facet must reach the rail without a client change; a facet the
    // server stops serving must stop being rendered.
    vi.mocked(fetchVisualizationOptions).mockResolvedValue({
      ...OPTIONS,
      member_metadata_facets: ["definition", "stewardship"],
      measures: [
        {
          ...OPTIONS.measures[0],
          metadata: {
            definition: { value: "A definition.", owner: "Semantic Model" },
            stewardship: { value: "Owned by Growth.", owner: "Governance" },
          },
        },
      ],
    } as never);
    builder();
    await screen.findByRole("heading", { name: "Field catalog" });
    expect(screen.getByTestId("member-clicks-stewardship")).toHaveTextContent("Owned by Growth.");
    expect(screen.queryByTestId("member-clicks-additivity")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// AC12 — the Builder resolves its pin, so the create path is reachable.
//
// `ContentRouter` mounts this screen with `querySpecVersionId={null}`. Before this
// the pin was then null for any Visualization without a version, `POST
// /visualizations` was unreachable from a browser, and Save was permanently
// disabled on exactly the path that creates one.
// ---------------------------------------------------------------------------

describe("the Query Spec version pin", () => {
  it("resolves the pin from the Result the address names, and enables Save", async () => {
    vi.mocked(fetchResultPin).mockResolvedValue({
      id: "qr_EXAMPLE",
      query_spec_version_id: PIN,
      outcome: "unavailable",
      content_hash: "h".repeat(64),
    } as never);

    builder({ querySpecVersionId: null, resultId: "qr_EXAMPLE" });

    await waitFor(() => expect(fetchResultPin).toHaveBeenCalledWith(
      "proj_EXAMPLE",
      "qr_EXAMPLE",
      expect.anything(),
    ));
    await waitFor(() =>
      expect(fetchVisualizationOptions).toHaveBeenCalledWith(
        "proj_EXAMPLE",
        PIN,
        expect.anything(),
      ),
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Save a new version" })).toBeEnabled(),
    );
    expect(screen.queryByText("No Query Spec version is pinned")).toBeNull();
  });

  it("does not ask a Result for a pin it was already given", async () => {
    vi.mocked(fetchResultPin).mockResolvedValue({
      id: "qr_EXAMPLE",
      query_spec_version_id: "qsv_OTHER",
      outcome: "unavailable",
      content_hash: "h".repeat(64),
    } as never);
    builder({ resultId: "qr_EXAMPLE" });
    await screen.findByRole("heading", { name: "Field catalog" });
    // The address's own pin wins: a Result read must never quietly re-point a
    // Builder the address already pinned.
    expect(fetchResultPin).not.toHaveBeenCalled();
    expect(fetchVisualizationOptions).toHaveBeenCalledWith(
      "proj_EXAMPLE",
      PIN,
      expect.anything(),
    );
  });

  it("says plainly that nothing is pinned when no source supplies one", async () => {
    builder({ querySpecVersionId: null, resultId: null });
    expect(
      await screen.findByText("No Query Spec version is pinned"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save a new version" })).toBeDisabled();
  });
});

// ---------------------------------------------------------------------------
// Story 66.8 — the Builder mounted on a CROSS-SOURCE Result.
//
// The label regression this guards was measured in production twice. `b96cd6a9`
// removed the identifiers from the rail on 2026-08-12 for the `query-spec.v1`
// contract; the `multi-source-plan.v1` branch added afterwards reintroduced
// them, and the rail printed `mdm_01KZ…` again (`f3ca3d50`). The server side is
// guarded by `test_visualization_options_labels`. Nothing MOUNTED the Builder on
// a crossed Result, so nothing proved that what the server now names, the screen
// draws — and the whole point of the repair is what a person reads.
// ---------------------------------------------------------------------------

/** A minted `mdm_` ULID, the exact expression `canonical_field_registry` mints. */
const MDM_SPEND = "mdm_01KZQ8N4V7C3RJ2M6PH0TB9XWE";
const MDM_ROAS = "mdm_01KZQ8N4V7C3RJ2M6PH0TB9XWF";
const MDM_DAY = "mdm_01KZQ8N4V7C3RJ2M6PH0TB9XWG";

/**
 * The options a `multi-source-plan.v1` version produces.
 *
 * `version_id` is EMPTY on purpose and this fixture keeps it so: a cross-source
 * plan pins no semantic concept version, so there is nothing for the five
 * presentation facets to read. Stating the absence is honest; inventing a
 * version id would not be.
 */
const CROSSED_OPTIONS = {
  ...OPTIONS,
  measures: [
    {
      id: `m_${MDM_SPEND}`,
      version_id: "",
      label: "Ad spend",
      role: "measure",
      metadata: metadata(),
    },
    {
      id: `r_${MDM_ROAS}`,
      version_id: "",
      label: "Return on ad spend",
      role: "measure",
      metadata: metadata(),
    },
  ],
  dimensions: [
    { id: `k_${MDM_DAY}`, version_id: "", label: "Day", role: "dimension", metadata: metadata() },
  ],
};

describe("the Builder on a cross-source Result", () => {
  it("names every crossed member, and prints no identifier in the rail", async () => {
    vi.mocked(fetchVisualizationOptions).mockResolvedValue(CROSSED_OPTIONS as never);
    vi.mocked(fetchResultPin).mockResolvedValue({
      id: "qr_CROSSED",
      query_spec_version_id: PIN,
      outcome: "success",
      content_hash: "c".repeat(64),
    } as never);

    builder({ querySpecVersionId: null, resultId: "qr_CROSSED" });

    // The words the server resolved, drawn.
    expect(await screen.findByText("Ad spend")).toBeInTheDocument();
    expect(screen.getByText("Return on ad spend")).toBeInTheDocument();
    expect(screen.getByText("Day")).toBeInTheDocument();

    // And not one identifier anywhere on the SCREEN — not only in the rail,
    // because the wells, the preview legend and the presentation rail draw the
    // same members and any of them printing the ULID is the same defect.
    //
    // SCOPED TO RESOLVED MEMBERS, and deliberately. When the vocabulary knows no
    // name the identifier REMAINS the label — ugly and true,
    // `visualization-and-rendering.md:189`, pinned by
    // `test_visualization_options_labels`. This fixture's three members are all
    // resolved, so the pattern must not appear; a member the vocabulary cannot
    // name is allowed to draw its ULID and this assertion does not speak to it.
    expect(document.body.textContent).not.toMatch(/mdm_[0-9A-HJKMNP-TV-Z]{26}/);
  });

  it("says the pinned version is not recorded rather than printing a blank", async () => {
    // A cross-source plan pins no concept version. `not recorded` is the honest
    // word; an empty space would read as a value that failed to load.
    vi.mocked(fetchVisualizationOptions).mockResolvedValue(CROSSED_OPTIONS as never);
    builder({ resultId: "qr_CROSSED" });

    const entry = await screen.findByTestId(`catalog-member-m_${MDM_SPEND}`);
    expect(entry).toHaveTextContent("measure · pinned version not recorded");
  });
});
