/**
 * Story 50.4 F4 — the one act that turns a Result into a Visualization identity.
 *
 * WHY IT EXISTS AT ALL. `shell/router.tsx` refuses to build an object address
 * without an identifier, so there is no `/object/visualization/new`. The identity
 * therefore has to be created BEFORE the address, and this module is that call.
 *
 * WHAT THESE TESTS GUARD. That the seed is a PROJECTION of the server's registry
 * and the server's member list, not a rule of its own. The moment this file
 * starts choosing a family from cardinality, or filling an optional well because
 * it looks good, the browser has become a second compatibility authority.
 */
import { describe, expect, it, vi } from "vitest";

vi.mock("../analyze/visualizationClient", async () => {
  const actual = await vi.importActual<typeof import("../analyze/visualizationClient")>(
    "../analyze/visualizationClient",
  );
  return {
    ...actual,
    fetchVisualizationFamilies: vi.fn(),
    fetchVisualizationOptions: vi.fn(),
    createVisualization: vi.fn(),
  };
});

import {
  createVisualizationFromResult,
  NoSeedableFamily,
  FALLBACK_FAMILY,
  seedBindings,
  seedDocument,
} from "../analyze/builder/seedVisualization";
import {
  createVisualization,
  fetchVisualizationFamilies,
  fetchVisualizationOptions,
} from "../analyze/visualizationClient";

function wellOf(name: string, accepts: string[], required: boolean, max: number) {
  return {
    name,
    label: name[0].toUpperCase() + name.slice(1),
    accepts,
    required,
    max_members: max,
    max_cardinality: null,
    available: true,
    unavailable_reason: null,
    unavailable_owner: null,
  };
}

const TABLE = {
  id: "table",
  label: "Table",
  description: "Every returned row and column.",
  wells: [
    wellOf("dimension", ["dimension"], true, 8),
    wellOf("measure", ["measure"], true, 12),
    { ...wellOf("time", ["time"], false, 1), available: false },
    wellOf("label", ["dimension", "measure"], false, 4),
  ],
  requires_time_grain: false,
  requires_comparison: false,
  max_marks: 1000,
  capabilities: ["row_scroll"],
  table_fallback_wells: ["dimension", "measure"],
  table_fallback: "required",
};

const REGISTRY = {
  families: [TABLE],
  wells: [],
  roles: [],
  deferred_families: [],
  max_inline_rows: 1000,
  spec_contract_version: "visualization-spec.v1",
  schema_version: 1,
  responsive_profiles: ["console", "mcp-inline", "mcp-fullscreen", "mcp-pip", "share"],
  grammar_keys: [],
};

const OPTIONS = {
  query_spec_id: "qs_EXAMPLE",
  query_spec_version_id: "qsv_EXAMPLE",
  semantic_view_id: "sv_EXAMPLE",
  semantic_view_version_id: "svv_EXAMPLE",
  measures: [
    { id: "clicks", version_id: "mv_1", label: "clicks", role: "measure" },
    { id: "cost", version_id: "mv_2", label: "cost", role: "measure" },
  ],
  dimensions: [{ id: "channel", version_id: "dv_1", label: "channel", role: "dimension" }],
  time: [],
  classifications: [],
  roles: [],
  unavailable_reason: "",
  unavailable_owner: "",
  grain: "day",
  comparison: "none",
  row_limit: 1000,
};

describe("seeding a Visualization from a Result", () => {
  it("falls back to the family every visual owes a fallback to, not a chosen one", () => {
    // Renamed from `SEED_FAMILY` by story 72.5, AC22, and the rename IS the
    // assertion: this constant is the last resort, not the starting point. The
    // starting point is a Chart Template when one fits the Result.
    expect(FALLBACK_FAMILY).toBe("table");
  });

  it("fills exactly the wells the SERVER declares required, with the roles it accepts", () => {
    const bindings = seedBindings(TABLE as never, OPTIONS as never);
    expect(Object.keys(bindings).sort()).toEqual(["dimension", "measure"]);
    expect(bindings.dimension).toEqual(["channel"]);
    expect(bindings.measure).toEqual(["clicks", "cost"]);
    // An optional well is left EMPTY. Filling one because it would look better
    // is a presentation decision the person has not made.
    expect(bindings.label).toBeUndefined();
    expect(bindings.time).toBeUndefined();
  });

  it("never binds more members than the well declares it takes", () => {
    const narrow = {
      ...TABLE,
      wells: [wellOf("dimension", ["dimension"], true, 8), wellOf("measure", ["measure"], true, 1)],
    };
    expect(seedBindings(narrow as never, OPTIONS as never).measure).toEqual(["clicks"]);
  });

  it("refuses in English when the query selected nothing for a required well", () => {
    const noDimension = { ...OPTIONS, dimensions: [] };
    expect(() => seedBindings(TABLE as never, noDimension as never)).toThrow(NoSeedableFamily);
    try {
      seedBindings(TABLE as never, noDimension as never);
    } catch (error) {
      expect((error as Error).message).toContain("Add one in Explore");
    }
  });

  it("carries both version keys from the server's registry, never a literal", () => {
    const document = seedDocument(REGISTRY as never, TABLE as never, OPTIONS as never);
    expect(document.spec_contract_version).toBe("visualization-spec.v1");
    expect(document.schema_version).toBe(1);
    expect(document.family).toBe("table");
    // No presentation key is invented: axes, legend, colour and the rest are the
    // server's declared defaults, applied by the server's normalizer.
    expect(Object.keys(document).sort()).toEqual([
      "bindings",
      "family",
      "schema_version",
      "spec_contract_version",
    ]);
  });

  it("creates the identity against the Result's own Query Spec version", async () => {
    vi.mocked(fetchVisualizationFamilies).mockResolvedValue(REGISTRY as never);
    vi.mocked(fetchVisualizationOptions).mockResolvedValue(OPTIONS as never);
    vi.mocked(createVisualization).mockResolvedValue({ visualization_id: "vis_1" } as never);

    const id = await createVisualizationFromResult("proj_EXAMPLE", "qsv_EXAMPLE");

    expect(id).toBe("vis_1");
    expect(createVisualization).toHaveBeenCalledWith("proj_EXAMPLE", {
      query_spec_version_id: "qsv_EXAMPLE",
      spec: {
        spec_contract_version: "visualization-spec.v1",
        schema_version: 1,
        family: "table",
        bindings: { dimension: ["channel"], measure: ["clicks", "cost"] },
      },
    });
  });

  it("carries one AbortSignal through discovery and the final create mutation", async () => {
    vi.mocked(fetchVisualizationFamilies).mockResolvedValue(REGISTRY as never);
    vi.mocked(fetchVisualizationOptions).mockResolvedValue(OPTIONS as never);
    vi.mocked(createVisualization).mockResolvedValue({ visualization_id: "vis_1" } as never);
    const controller = new AbortController();

    await createVisualizationFromResult(
      "proj_EXAMPLE",
      "qsv_EXAMPLE",
      { signal: controller.signal },
    );

    expect(fetchVisualizationFamilies).toHaveBeenCalledWith(
      "proj_EXAMPLE",
      { signal: controller.signal },
    );
    expect(fetchVisualizationOptions).toHaveBeenCalledWith(
      "proj_EXAMPLE",
      "qsv_EXAMPLE",
      { signal: controller.signal },
    );
    expect(createVisualization).toHaveBeenCalledWith(
      "proj_EXAMPLE",
      expect.any(Object),
      { signal: controller.signal },
    );
  });

  it("does not retry with a quieter document when the server refuses", async () => {
    vi.mocked(fetchVisualizationFamilies).mockResolvedValue(REGISTRY as never);
    vi.mocked(fetchVisualizationOptions).mockResolvedValue(OPTIONS as never);
    vi.mocked(createVisualization).mockReset();
    vi.mocked(createVisualization).mockRejectedValue(new Error("refused"));

    await expect(createVisualizationFromResult("proj_EXAMPLE", "qsv_EXAMPLE")).rejects.toThrow(
      "refused",
    );
    // A presentation the server refused is not one to smuggle past it with fewer
    // bindings: exactly one attempt, and the refusal reaches the reader.
    expect(vi.mocked(createVisualization)).toHaveBeenCalledTimes(1);
  });
});
