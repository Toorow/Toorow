/**
 * Story 76-8 — one function decides a verdict colour, one component states the
 * rule it applied, and one place on the card prints it.
 *
 * THE DEFECT THIS FILE WAS WRITTEN AFTER, and it was visible on the rendered
 * card. `BarChart` coloured from `entry.direction` while the legend beneath it
 * stated the convention of `semanticDirection`: on `card-keywords` (binding
 * `down_good`, average position) `+3.2` was painted GREEN above `-3.8` in RED,
 * under a sentence reading "falling is favourable". Every one of those five bars
 * read backwards to anybody who trusted the legend.
 *
 * A guard that only checked "a legend exists" would have been green throughout.
 * What is pinned here is the DECISION: `verdictTone` is the only thing on these
 * surfaces allowed to turn a change into success or error, and `VariationLegend`
 * states exactly that function's rule.
 */

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import VariationLegend from "../VariationLegend";
import { verdictTone, changeMark } from "../verdictTone";
import { variationConventions } from "../variationConventions";
import type { CardData, CompositionBlock } from "../types";

describe("verdictTone — the one decision", () => {
  it("reads the SIGNED change against the declared convention", () => {
    expect(verdictTone(3.2, "up_good")).toBe("favourable");
    expect(verdictTone(-3.8, "up_good")).toBe("unfavourable");
    // The keywords case: average position, where rising is a loss.
    expect(verdictTone(3.2, "down_good")).toBe("unfavourable");
    expect(verdictTone(-3.8, "down_good")).toBe("favourable");
  });

  it("invents no verdict where none was declared or measured", () => {
    expect(verdictTone(3.2, "neutral")).toBe("none");
    expect(verdictTone(3.2, undefined)).toBe("none");
    expect(verdictTone(null, "up_good")).toBe("none");
    expect(verdictTone(Number.NaN, "up_good")).toBe("none");
    // Nothing moved, so nothing is judged…
    expect(verdictTone(0, "up_good")).toBe("none");
    // …unless the caller compares against a target, where landing on it counts.
    expect(verdictTone(0, "up_good", { zeroIsFavourable: true })).toBe("favourable");
  });

  it("marks the change the same way it colours it", () => {
    expect(changeMark(3.2)).toBe("▲");
    expect(changeMark(-3.8)).toBe("▼");
    expect(changeMark(0)).toBe("=");
    expect(changeMark(null)).toBeNull();
  });
});

describe("VariationLegend — it states the rule the colour applied", () => {
  it("names the comparison and both marks", () => {
    render(<VariationLegend direction="up_good" comparison="the previous period" />);
    const legend = screen.getByTestId("variation-legend");
    expect(legend).toHaveTextContent("▲ up");
    expect(legend).toHaveTextContent("▼ down");
    expect(legend).toHaveTextContent("vs the previous period");
    expect(legend).toHaveTextContent("Green: favourable");
    expect(legend).toHaveTextContent("red: unfavourable");
    expect(legend).toHaveTextContent("rising is favourable");
  });

  it("says the opposite when the convention is the opposite", () => {
    render(<VariationLegend direction="down_good" />);
    expect(screen.getByTestId("variation-legend")).toHaveTextContent("falling is favourable");
  });

  it("names each convention when a card holds two", () => {
    render(
      <VariationLegend
        conventions={[
          { direction: "up_good", subject: "Clics" },
          { direction: "down_good", subject: "Position moyenne" },
        ]}
      />,
    );
    const legend = screen.getByTestId("variation-legend");
    expect(legend).toHaveTextContent("rising is favourable for Clics");
    expect(legend).toHaveTextContent("falling is favourable for Position moyenne");
    expect(legend).toHaveAttribute("data-direction", "mixed");
  });

  it("does not judge where the surface does not", () => {
    render(<VariationLegend direction="neutral" />);
    expect(screen.getByTestId("variation-legend")).toHaveTextContent("no verdict");
  });

  it("renders nothing rather than a line that teaches nothing", () => {
    const { container } = render(<VariationLegend arrows={false} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("is served in English, like every string the shell writes itself", () => {
    render(<VariationLegend direction="down_good" comparison="the previous period" />);
    const text = screen.getByTestId("variation-legend").textContent ?? "";
    expect(text).not.toMatch(/[éèêàùç]/);
  });
});

describe("variationConventions — what a card has in force", () => {
  const DATA = {
    card_id: "keywords",
    metrics: {},
    series: {},
    metric_definitions: {
      clicks: { definition: "", unit: "clics", direction: "up_good" },
    },
  } as unknown as CardData;

  it("reads a KPI row and a bar binding, and keeps both conventions", () => {
    const blocks = [
      { type: "kpi_row", data: { metrics: [{ metric: "clicks", value: 1, delta_pct: 8.8 }] } },
      {
        type: "bar",
        title: "Requetes en mouvement",
        binding: { direction: "down_good" },
        data: { bars: [] },
      },
    ] as unknown as CompositionBlock[];
    const found = variationConventions(blocks, DATA);
    expect(found).toEqual([
      { direction: "up_good", subject: "Clics" },
      { direction: "down_good", subject: "Requetes en mouvement" },
    ]);
  });

  it("states nothing for a gauge with no target — no second operand, no verdict", () => {
    const blocks = [
      { type: "gauge", title: "CPA", data: { value: 30, target: null, direction: "down_good" } },
    ] as unknown as CompositionBlock[];
    expect(variationConventions(blocks, DATA)).toEqual([]);
  });

  it("states nothing for a KPI whose delta is absent", () => {
    const blocks = [
      { type: "kpi_row", data: { metrics: [{ metric: "clicks", value: 1, delta_pct: null }] } },
    ] as unknown as CompositionBlock[];
    expect(variationConventions(blocks, DATA)).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// The guards. A second decision, or a second legend, is the defect.
// ---------------------------------------------------------------------------

const SRC = join(__dirname, "..");

function shellSources(): { key: string; text: string }[] {
  return readdirSync(SRC)
    .filter((f) => /\.tsx?$/.test(f) && !/\.test\.tsx?$/.test(f))
    .map((f) => ({ key: f, text: readFileSync(join(SRC, f), "utf8") }));
}

/**
 * Turning a change into success/error. `vizTheme.ts` DEFINES the ramp and
 * colours nothing; it is the one exemption, named rather than pattern-hidden.
 */
const DECIDES = /palette\.success\.main|palette\.error\.main|"success\.main"|"error\.main"/;
const NOT_A_RENDERER = new Set([
  "verdictTone.ts",
  "vizTheme.ts",
  // `CardFeedbackBar` colours a SUBMISSION STATE — sent, refused — which is a
  // status, not a variation. Naming it here is the honest form: widening the
  // pattern until it stopped matching would have hidden the next real one.
  "CardFeedbackBar.tsx",
]);

describe("one decision, one legend, one placement (76-8)", () => {
  it("no primitive turns a change into success or error on its own", () => {
    const offenders = shellSources()
      .filter((f) => !NOT_A_RENDERER.has(f.key) && DECIDES.test(f.text))
      .map((f) => f.key);
    expect(offenders).toEqual([]);
  });

  it("the primitives that colour a verdict go through `verdictTone`", () => {
    const users = shellSources().filter((f) => f.text.includes('from "./verdictTone"'));
    const keys = users.map((u) => u.key);
    for (const key of [
      "BarChart.tsx",
      "Gauge.tsx",
      "Funnel.tsx",
      "RankedList.tsx",
      "KpiDeltaFooter.tsx",
      "CardComposition.tsx",
    ]) {
      expect(keys, `${key} decides its own verdict`).toContain(key);
    }
  });

  it("no source writes a second legend sentence", () => {
    const offenders = shellSources()
      .filter((f) => f.key !== "VariationLegend.tsx")
      .filter((f) => /▲ up|Green: favourable|no verdict here/.test(f.text))
      .map((f) => f.key);
    expect(offenders).toEqual([]);
  });

  it("only the card shell mounts it — one placement, and it is the footer", () => {
    const mounters = shellSources()
      .filter((f) => f.key !== "VariationLegend.tsx" && f.key !== "index.ts")
      .filter((f) => f.text.includes('from "./VariationLegend"'))
      .map((f) => f.key)
      .sort();
    // `variationConventions.ts` imports the TYPE only; the renderer is CardShell.
    expect(mounters).toEqual(["CardShell.tsx", "variationConventions.ts"]);
    expect(shellSources().find((f) => f.key === "CardShell.tsx")!.text).toContain(
      "<VariationLegend",
    );
  });
});
