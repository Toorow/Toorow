/**
 * Story 50.5 AC8/AC9/AC14 -- Console, MCP App and Share are provably the same
 * runtime, and a profile changes layout only.
 */

import { describe, expect, it } from "vitest";

import { serializeForConsole } from "../entries/console";
import { serializeForMcpApp } from "../entries/mcpApp";
import { serializeForShare } from "../entries/share";
import { serializeVisualModel } from "../Runtime";
import { profileLayout, resolveProfile, RESPONSIVE_PROFILES } from "../responsive";
import { checkBuildIdentity, RUNTIME_BUILD } from "../buildInfo";
import { installStandardRenderers } from "../renderers";
import { renderInput } from "./fixtures";

installStandardRenderers();

describe("AC8 -- the three entries produce the same visual model", () => {
  const input = renderInput();

  it("deep-equal serialized models across Console, MCP App and Share", () => {
    const console_ = serializeForConsole(input);
    const mcp = serializeForMcpApp(input, "inline");
    const share = serializeForShare(input);

    expect(console_).toEqual(mcp);
    expect(console_).toEqual(share);
  });

  it("the same ordered datum keys and the same datum-key -> field map", () => {
    const console_ = serializeForConsole(input);
    const share = serializeForShare(input);
    expect(console_.datumKeys).toEqual(share.datumKeys);
    expect(console_.datumKeys.length).toBeGreaterThan(0);
    expect(console_.datumFields).toEqual(share.datumFields);
  });

  it("the same disclosed filters, comparison, freshness and truncation strings", () => {
    const console_ = serializeForConsole(input);
    const mcp = serializeForMcpApp(input, "fullscreen");
    expect(console_.disclosures).toEqual(mcp.disclosures);
    expect(console_.disclosures.filters).toEqual(["country = FR"]);
  });

  it("changing profile changes NO value, colour, format or evidence key", () => {
    const models = RESPONSIVE_PROFILES.map((profile) =>
      serializeVisualModel({ ...input, profile }),
    );
    const reference = models[0]!;
    for (const model of models.slice(1)) {
      expect(model.dataset).toEqual(reference.dataset);
      expect(model.colors).toEqual(reference.colors);
      expect(model.formats).toEqual(reference.formats);
      expect(model.datumKeys).toEqual(reference.datumKeys);
      expect(model.datumFields).toEqual(reference.datumFields);
      expect(model.disclosures).toEqual(reference.disclosures);
      expect(model.series).toEqual(reference.series);
    }
  });

  it("a profile may only rearrange layout -- the layout fields ARE what differs", () => {
    const wide = profileLayout("console", 1200);
    const inline = profileLayout("mcp-inline", 1200);
    expect(inline.legendCollapsed).toBe(true);
    expect(wide.legendCollapsed).toBe(false);
    // And a narrower container reflows at the ONE shared threshold.
    expect(profileLayout("console", 400).legendCollapsed).toBe(true);
  });
});

describe("AC9 -- the responsive profile is Story 50.4's enum", () => {
  it("has exactly the five server values and no `compact`", () => {
    expect([...RESPONSIVE_PROFILES]).toEqual([
      "console",
      "mcp-inline",
      "mcp-fullscreen",
      "mcp-pip",
      "share",
    ]);
    expect(RESPONSIVE_PROFILES as readonly string[]).not.toContain("compact");
  });

  it("an unknown profile falls back to console and says the value was not a profile", () => {
    const resolution = resolveProfile("phone");
    expect(resolution.profile).toBe("console");
    expect(resolution.substitution).toContain("phone");
  });
});

/**
 * Criterion [14] of `visualization-and-rendering.md`: the ratified Surface parity
 * table gives the MCP layout as inline, fullscreen AND picture-in-picture, and it
 * was reported as delivered while PiP was only substituted by inline.
 *
 * PiP is now drawn. What that has to mean, to be worth anything, is that it is the
 * SAME runtime with a different layout -- not a fourth entry, not a second engine,
 * not a summary that quietly drops something. Each `it` below is one half of that.
 */
describe("mcp-pip is a drawn mode of the ONE runtime, not a substitution", () => {
  it("is resolved, never substituted -- nothing is stated because nothing was swapped", () => {
    const resolution = resolveProfile("mcp-pip");
    expect(resolution.profile).toBe("mcp-pip");
    expect(resolution.substitution).toBeNull();
  });

  it("the host's `pip` display mode reaches the runtime as `mcp-pip`", () => {
    const model = serializeForMcpApp(renderInput(), "pip");
    expect(model.profileSubstitution).toBeNull();
    expect(model.rendererId).toBe(serializeForConsole(renderInput()).rendererId);
  });

  it("the three entries produce ONE visual model at the PiP profile", () => {
    // The same assertion AC8 makes for inline, made for the mode that used not to
    // exist: a profile that produced its own model would be a second renderer
    // wearing the runtime's name.
    const input = { ...renderInput(), profile: "mcp-pip" as const };
    const console_ = serializeForConsole(input);
    const mcp = serializeForMcpApp(input, "pip");
    const share = serializeForShare(input);
    expect(mcp).toEqual(console_);
    expect(share).toEqual(console_);
  });

  it("changes NO value, colour, format, datum key, evidence map or disclosure", () => {
    const input = renderInput();
    const pip = serializeVisualModel({ ...input, profile: "mcp-pip" });
    const inline = serializeVisualModel({ ...input, profile: "mcp-inline" });
    expect(pip.dataset).toEqual(inline.dataset);
    expect(pip.colors).toEqual(inline.colors);
    expect(pip.formats).toEqual(inline.formats);
    expect(pip.datumKeys).toEqual(inline.datumKeys);
    expect(pip.datumFields).toEqual(inline.datumFields);
    expect(pip.datumTargets).toEqual(inline.datumTargets);
    expect(pip.disclosures).toEqual(inline.disclosures);
    expect(pip.series).toEqual(inline.series);
    expect(pip.tableColumns).toEqual(inline.tableColumns);
  });

  it("what it DOES change is layout, and it is the smallest surface", () => {
    // A branch that returned inline's numbers would satisfy every assertion above
    // and deliver nothing: "PiP is built" would then be a rename of the
    // substitution it replaced.
    const pip = profileLayout("mcp-pip", 1200);
    const inline = profileLayout("mcp-inline", 1200);
    expect(pip.height).toBeLessThan(inline.height);
    expect(pip.legendCollapsed).toBe(true);
    expect(profileLayout("mcp-pip", 400).height).toBeLessThan(pip.height);
  });

  it("no profile drops the table fallback -- the full view stays in the same surface", () => {
    // "A compact profile can summarize the view only when the full frozen view
    // remains reachable in the same surface." The renderer's table toggle is what
    // makes that true, and `tableFallbackOpen` seeds it OPEN or CLOSED; no profile
    // may remove it. Asserted over the whole enum, so a sixth profile inherits it.
    for (const profile of RESPONSIVE_PROFILES) {
      expect(typeof profileLayout(profile, 1200).tableFallbackOpen).toBe("boolean");
    }
  });

  it("every profile the enum names has its own branch -- none falls to the default", () => {
    // `profileLayout` ends in `case "console": default:`, so a value added to the
    // enum and forgotten here would silently be drawn as the CONSOLE, at console
    // height, inside a picture-in-picture window. The console layout is claimed by
    // exactly one profile.
    const consoleLayout = JSON.stringify(profileLayout("console", 1200));
    const alsoConsole = RESPONSIVE_PROFILES.filter(
      (profile) => JSON.stringify(profileLayout(profile, 1200)) === consoleLayout,
    );
    expect(alsoConsole).toEqual(["console"]);
  });
});

describe("AC14 -- build identity is pinned, and replay refuses on a mismatch", () => {
  it("the runtime build is not a placeholder and carries package, semver and sha", () => {
    expect(RUNTIME_BUILD).toMatch(
      /^@toorow\/card-shell\/viz@\d+\.\d+\.\d+\+[0-9a-f]{7,}$/,
    );
    expect(RUNTIME_BUILD).not.toContain("unbuilt");
  });

  it("a renderer build names family, renderer and semver", () => {
    const model = serializeForConsole(renderInput());
    expect(model.rendererBuild).toMatch(/^bar\/toorow-echarts-bar@\d+\.\d+\.\d+$/);
  });

  it("refuses -- showing BOTH identities -- rather than redrawing through another build", () => {
    const mismatches = checkBuildIdentity(
      {
        runtime_build: "@toorow/card-shell/viz@0.0.9+deadbee",
        renderer_build: "bar/x@1.0.0",
        theme_version: "viz-theme@1",
        formatter_version: "viz-formatters@1",
      },
      {
        runtime_build: RUNTIME_BUILD,
        renderer_build: "bar/toorow-echarts-bar@1.0.0",
        theme_version: "viz-theme@1",
        formatter_version: "viz-formatters@1",
      },
    );
    expect(mismatches).toHaveLength(2);
    expect(mismatches[0]!.message).toContain("@toorow/card-shell/viz@0.0.9+deadbee");
    expect(mismatches[0]!.message).toContain(RUNTIME_BUILD);
    expect(mismatches[0]!.message).toContain("not");
  });

  it("matching pins produce no mismatch", () => {
    expect(
      checkBuildIdentity(
        {
          runtime_build: RUNTIME_BUILD,
          renderer_build: "bar/toorow-echarts-bar@1.0.0",
          theme_version: "viz-theme@1",
          formatter_version: "viz-formatters@1",
        },
        {
          runtime_build: RUNTIME_BUILD,
          renderer_build: "bar/toorow-echarts-bar@1.0.0",
          theme_version: "viz-theme@1",
          formatter_version: "viz-formatters@1",
        },
      ),
    ).toEqual([]);
  });

  it("refuses an unavailable theme or formatter instead of replaying with current policy", () => {
    const mismatches = checkBuildIdentity(
      {
        runtime_build: RUNTIME_BUILD,
        renderer_build: "bar/toorow-echarts-bar@1.0.0",
        theme_version: "viz-theme@0",
        formatter_version: "viz-formatters@0",
      },
      {
        runtime_build: RUNTIME_BUILD,
        renderer_build: "bar/toorow-echarts-bar@1.0.0",
        theme_version: "viz-theme@1",
        formatter_version: "viz-formatters@1",
      },
    );
    expect(mismatches.map((m) => m.pin)).toEqual(["theme_version", "formatter_version"]);
    expect(mismatches.every((m) => m.message.includes("deploy the version"))).toBe(true);
  });
});
