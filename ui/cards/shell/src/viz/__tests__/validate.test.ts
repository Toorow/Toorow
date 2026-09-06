/**
 * Story 50.5 AC2/AC3 -- one refusal test per bullet, plus the raw-option refusal,
 * plus the proof that the public entry has no second overload.
 */

import { describe, expect, it } from "vitest";

import { validateRenderInput, SUPPORTED_SCHEMA_VERSIONS } from "../validate";
import { resolveRenderer } from "../registry";
import { installStandardRenderers } from "../renderers";
import { prepareVisualization } from "../Runtime";
import { renderInput, specDocument } from "./fixtures";
import * as runtimeModule from "../Runtime";

installStandardRenderers();

function rendererFor(family: string) {
  const resolution = resolveRenderer(family, 1, "console");
  return resolution.kind === "renderer" ? resolution.renderer : null;
}

describe("AC2 -- the runtime has exactly one validated input", () => {
  it("accepts the exact five-field envelope", () => {
    const { refusals } = validateRenderInput(renderInput(), rendererFor("bar"));
    expect(refusals).toEqual([]);
  });

  it("refuses an absent pin, naming the field", () => {
    const input = renderInput();
    // @ts-expect-error deliberately removing a required pin
    delete input.pins.theme_version;
    const { refusals } = validateRenderInput(input, rendererFor("bar"));
    expect(refusals.map((r) => r.field)).toContain("pins.theme_version");
    expect(refusals[0]!.message).toMatch(/required/);
  });

  it("refuses an empty pin", () => {
    const input = renderInput({ pins: { runtime_build: "   " } as never });
    const { refusals } = validateRenderInput(input, rendererFor("bar"));
    expect(refusals.map((r) => r.field)).toContain("pins.runtime_build");
  });

  it("refuses a placeholder pin", () => {
    const input = renderInput({ pins: { theme_version: "latest" } as never });
    const { refusals } = validateRenderInput(input, rendererFor("bar"));
    const refusal = refusals.find((r) => r.field === "pins.theme_version");
    expect(refusal?.code).toBe("placeholder_pin");
  });

  it("refuses a renderer_build the runtime does not know", () => {
    const input = renderInput({ pins: { renderer_build: "bar/somebody-elses@9.9.9" } as never });
    const { refusals } = validateRenderInput(input, rendererFor("bar"));
    expect(refusals.map((r) => r.code)).toContain("unknown_renderer_build");
  });

  it("ranges over the INTEGER schema_version and refuses out of range", () => {
    const input = renderInput();
    input.spec.schema_version = SUPPORTED_SCHEMA_VERSIONS.max + 1;
    const { refusals } = validateRenderInput(input, rendererFor("bar"));
    expect(refusals.map((r) => r.code)).toContain("schema_version_unsupported");
  });

  it("refuses a non-integer schema_version -- a range over the string literal is a type error", () => {
    const input = renderInput();
    // The string contract version handed to the integer key: the exact confusion
    // AC2 names. It must be a refusal, not a silently passing comparison.
    (input.spec as unknown as Record<string, unknown>).schema_version = "visualization-spec.v1";
    const { refusals } = validateRenderInput(input, rendererFor("bar"));
    expect(refusals.map((r) => r.code)).toContain("schema_version_not_integer");
  });

  it("asserts the string contract version for EQUALITY", () => {
    const input = renderInput();
    (input.spec as unknown as Record<string, unknown>).spec_contract_version =
      "visualization-spec.v2";
    const { refusals } = validateRenderInput(input, rendererFor("bar"));
    expect(refusals.map((r) => r.code)).toContain("spec_contract_version_mismatch");
  });

  it("refuses a spec that references a field the Result projection does not carry", () => {
    const input = renderInput({
      spec: {
        visualization_spec_version_id: "vsv_EXAMPLE_0001",
        spec_contract_version: "visualization-spec.v1",
        schema_version: 1,
        document: specDocument({
          bindings: { dimension: ["channel"], measure: ["revenue_that_is_absent"] },
        }),
      },
    });
    const { refusals } = validateRenderInput(input, rendererFor("bar"));
    const refusal = refusals.find((r) => r.code === "field_absent_from_result");
    expect(refusal?.field).toBe("spec.document.bindings.measure[0]");
    expect(refusal?.message).toContain("revenue_that_is_absent");
    // The message names the fields that DO exist, so the reader can act.
    expect(refusal?.message).toContain("channel");
  });

  it("refuses a display key no renderer declared", () => {
    const input = renderInput({ display: { rebucket: "week" } as never });
    const { refusals } = validateRenderInput(input, rendererFor("bar"));
    const refusal = refusals.find((r) => r.code === "undeclared_display_key");
    expect(refusal?.field).toBe("display.rebucket");
  });

  it("refuses a profile outside Story 50.4's enum", () => {
    const input = renderInput({ profile: "compact" as never });
    const { refusals } = validateRenderInput(input, rendererFor("bar"));
    expect(refusals.map((r) => r.code)).toContain("invalid_profile");
  });

  it("refuses a sixth envelope field", () => {
    const input = { ...renderInput(), theme: { primary: "#FF99C8" } };
    const { refusals } = validateRenderInput(input, rendererFor("bar"));
    expect(refusals.map((r) => r.field)).toContain("theme");
  });
});

describe("AC3 -- no raw renderer configuration is ever accepted", () => {
  it("refuses a bare ECharts option object handed to the entry", () => {
    const { refusals } = validateRenderInput({ option: { series: [{ type: "bar" }] } }, null);
    // It is not an envelope at all, and the refusal says so.
    expect(refusals.map((r) => r.field)).toContain("option");
  });

  it("refuses an `option` key smuggled inside the spec", () => {
    const input = renderInput() as unknown as Record<string, unknown>;
    (input.spec as Record<string, unknown>).option = { series: [{ type: "bar" }] };
    const { refusals } = validateRenderInput(input, rendererFor("bar"));
    expect(refusals.map((r) => r.code)).toContain("raw_renderer_configuration");
  });

  it("refuses a function anywhere in the spec", () => {
    const input = renderInput() as unknown as Record<string, unknown>;
    (input.spec as Record<string, unknown>).labelMaker = () => "x";
    const { refusals } = validateRenderInput(input, rendererFor("bar"));
    expect(refusals.map((r) => r.code)).toContain("executable_value");
  });

  it("refuses a URL in a label override", () => {
    const input = renderInput({
      spec: {
        visualization_spec_version_id: "vsv_EXAMPLE_0001",
        spec_contract_version: "visualization-spec.v1",
        schema_version: 1,
        document: specDocument({
          labels: { override: { channel: "https://example.com/logo.png" } },
        }),
      },
    });
    const { refusals } = validateRenderInput(input, rendererFor("bar"));
    expect(refusals.map((r) => r.code)).toContain("url_value");
  });

  it("refuses markup in a label override", () => {
    const input = renderInput({
      spec: {
        visualization_spec_version_id: "vsv_EXAMPLE_0001",
        spec_contract_version: "visualization-spec.v1",
        schema_version: 1,
        document: specDocument({
          labels: { override: { channel: "<script>alert(1)</script>" } },
        }),
      },
    });
    const { refusals } = validateRenderInput(input, rendererFor("bar"));
    expect(refusals.map((r) => r.code)).toContain("markup_value");
  });

  it("the public entry takes ONE argument -- there is no options-object overload", () => {
    // A second overload would show up as an entry that tolerates a bare bag.
    // `prepareVisualization` refuses it instead of preparing anything.
    const prepared = prepareVisualization({ option: { series: [] } } as never);
    expect(prepared.model).toBeNull();
    expect(prepared.refusals.length).toBeGreaterThan(0);
    // React components take exactly one props object; the runtime's non-React
    // entries take (input, themeScope) and nothing else.
    expect(runtimeModule.default.length).toBe(1);
    expect(runtimeModule.prepareVisualization.length).toBe(2);
    expect(runtimeModule.serializeVisualModel.length).toBe(2);
  });
});
