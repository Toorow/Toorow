/**
 * Story 50.5 AC4/AC6/AC13 -- the registry is a closed, declarative contract, and
 * an unbuilt family is refused BY NAME rather than substituted.
 */

import { describe, expect, it } from "vitest";

import {
  KNOWN_UNBUILT_FAMILIES,
  registered,
  registeredCapabilities,
  resolveCapability,
  resolveRenderer,
} from "../registry";
import { AI_PATH_CAPABILITY_SCHEMA } from "../aiPathCapability";
import { installStandardRenderers } from "../renderers";
import { RESPONSIVE_PROFILES } from "../responsive";
import { VIZ_STATE_KINDS } from "../states";

installStandardRenderers();

describe("AC4 -- the standard visual families", () => {
  it("registers the seven Story 50.4 families plus the governed waterfall", () => {
    expect(registered().flatMap((r) => r.families).sort()).toEqual(
      ["area", "bar", "kpi", "line", "scatter", "stacked_bar", "table", "waterfall"],
    );
  });

  it("family -> renderer is a lookup, not a switch: every family resolves", () => {
    for (const family of ["table", "kpi", "line", "area", "bar", "stacked_bar", "scatter", "waterfall"]) {
      const resolution = resolveRenderer(family, 1, "console");
      expect(resolution.kind).toBe("renderer");
    }
  });

  it("the five remaining ordinary families are known-and-refused BY NAME", () => {
    for (const family of [
      "distribution",
      "heatmap",
      "calendar_heatmap",
      "gauge",
      "small_multiples",
    ]) {
      const resolution = resolveRenderer(family, 1, "console");
      expect(resolution.kind).toBe("refused");
      if (resolution.kind !== "refused") return;
      expect(resolution.code).toBe("renderer_not_built");
      expect(resolution.message).toContain(family);
      // Never silently substituted by a nearer family.
      expect(resolution.message).toContain("no nearer family is substituted");
    }
  });

  it("keeps only genuinely unbuilt specialized families in the absence inventory", () => {
    const names = KNOWN_UNBUILT_FAMILIES.map((f) => f.family);
    expect(names).toEqual(
      expect.arrayContaining([
        "timeline",
        "sankey",
        "geographic_map",
        "knowledge_graph",
        "mindmap",
      ]),
    );
    expect(names).not.toContain("ai_path");
    expect(names).toHaveLength(10);
  });

  it("declares AI Path as a capability without making it Visualization Spec selectable", () => {
    expect(registeredCapabilities().map((entry) => entry.family)).toEqual(["ai_path"]);
    expect(resolveCapability("ai_path", AI_PATH_CAPABILITY_SCHEMA)).not.toBeNull();
    expect(resolveCapability("ai_path", "observed-ai-path.v2")).toBeNull();
    expect(resolveRenderer("ai_path", 1, "console")).toMatchObject({
      kind: "refused",
      code: "capability_not_visualization_spec",
    });
  });

  it("an unknown family is refused, and the message names the families that exist", () => {
    const resolution = resolveRenderer("pie_of_pie", 1, "console");
    expect(resolution.kind).toBe("refused");
    if (resolution.kind !== "refused") return;
    expect(resolution.code).toBe("unknown_family");
    expect(resolution.message).toContain("bar");
  });
});

describe("AC6 -- every renderer declares its contract as data", () => {
  it("declares an INTEGER schema range, never `latest`", () => {
    for (const renderer of registered()) {
      expect(Number.isInteger(renderer.schemaVersions.min)).toBe(true);
      expect(Number.isInteger(renderer.schemaVersions.max)).toBe(true);
      expect(JSON.stringify(renderer.schemaVersions)).not.toContain("latest");
    }
  });

  it("declares required binding roles from the well vocabulary, not from the Result schema", () => {
    const wells = new Set([
      "measure",
      "dimension",
      "time",
      "series",
      "breakdown",
      "facet",
      "color",
      "size",
      "label",
      "detail",
    ]);
    for (const renderer of registered()) {
      expect(renderer.requiredWells.length).toBeGreaterThan(0);
      for (const well of renderer.requiredWells) expect(wells.has(well)).toBe(true);
    }
  });

  it("declares volume constraints, responsive capabilities and evidence-hit behaviour", () => {
    for (const renderer of registered()) {
      expect(renderer.volume.max_rows).toBeGreaterThan(0);
      expect(renderer.volume.max_series).toBeGreaterThan(0);
      expect(renderer.volume.max_cells).toBeGreaterThan(0);
      expect(renderer.profiles.length).toBeGreaterThan(0);
      for (const profile of renderer.profiles) {
        expect(RESPONSIVE_PROFILES as readonly string[]).toContain(profile);
      }
      expect(["datum", "series", "category", "none"]).toContain(
        renderer.evidenceHit.granularity,
      );
    }
  });

  it("an out-of-range schema_version is refused with the supported range named", () => {
    const resolution = resolveRenderer("bar", 7, "console");
    expect(resolution.kind).toBe("refused");
    if (resolution.kind !== "refused") return;
    expect(resolution.code).toBe("schema_version_unsupported");
    expect(resolution.message).toContain("1..1");
  });

  it("the registry is code: nothing in it is loaded from a table or a manifest", () => {
    for (const renderer of registered()) {
      expect(typeof renderer.component).toBe("function");
    }
  });
});

describe("AC13 -- eleven distinct runtime states", () => {
  it("declares all eleven, and empty is not unavailable", () => {
    expect(VIZ_STATE_KINDS).toHaveLength(11);
    expect(VIZ_STATE_KINDS).toEqual(
      expect.arrayContaining([
        "loading",
        "empty",
        "stale",
        "partial",
        "truncated",
        "degraded",
        "refused",
        "unavailable",
        "denied",
        "unknown",
        "errored",
      ]),
    );
  });
});
