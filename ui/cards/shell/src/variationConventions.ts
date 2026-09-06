/**
 * WHICH COLOUR CONVENTIONS A CARD HAS IN FORCE — derived from the composition
 * the server sent, so the one legend in the card footer states them all.
 *
 * WHY THE FOOTER AND NOT EACH BLOCK. Arbitrage 5 of story 76-8 places the legend
 * in the card footer. A legend under every block would repeat four or five
 * near-identical grey sentences down one card, and the reader would stop reading
 * them — which is the same as not having one.
 *
 * WHY A LIST AND NOT ONE VALUE. `card-keywords` holds both: clicks and
 * impressions read `up_good`, average position reads `down_good`. Collapsing
 * them into a single sentence would be false for half the figures on the card,
 * so each convention is named with what it governs, and the legend prints both.
 *
 * IT READS ONLY WHAT IS ALREADY DECLARED — the member definition's `direction`,
 * or the binding's. Nothing here infers a convention from a metric name.
 */

import type { CardData, CompositionBlock, GaugeBlockData, KpiRowBlockData } from "./types";
import { metricLabel } from "./types";
import type { VariationConvention } from "./VariationLegend";
import type { VerdictDirection } from "./verdictTone";

function asDirection(value: unknown): VerdictDirection | null {
  return value === "up_good" || value === "down_good" || value === "neutral" ? value : null;
}

/** Conventions in force, deduplicated, in the order the blocks declare them. */
export function variationConventions(
  blocks: CompositionBlock[] | undefined,
  data: CardData,
): VariationConvention[] {
  const found: VariationConvention[] = [];
  const seen = new Set<string>();

  function add(direction: VerdictDirection | null, subject?: string) {
    if (!direction || direction === "neutral") return;
    const key = `${direction}|${subject ?? ""}`;
    if (seen.has(key)) return;
    seen.add(key);
    found.push(subject ? { direction, subject } : { direction });
  }

  for (const block of blocks ?? []) {
    switch (block.type) {
      case "kpi_row": {
        const kd = block.data as KpiRowBlockData | undefined;
        if (kd?.metrics) {
          for (const m of kd.metrics) {
            if (m.delta_pct === null || m.delta_pct === undefined) continue;
            const declared =
              asDirection(m.direction) ??
              asDirection(data.metric_definitions?.[m.metric]?.direction) ??
              "up_good";
            add(declared, metricLabel(m.metric));
          }
          break;
        }
        // Backward-compatible path: the KPI card reads `data.metrics`.
        for (const [metric, rollup] of Object.entries(data.metrics ?? {})) {
          if (rollup.delta_pct === null || rollup.delta_pct === undefined) continue;
          add(
            asDirection(data.metric_definitions?.[metric]?.direction) ?? "up_good",
            metricLabel(metric),
          );
        }
        break;
      }
      case "bar": {
        const binding = block.binding as Record<string, unknown> | undefined;
        const declared = asDirection(binding?.direction);
        // The bars only take a verdict colour when the binding declares one.
        if (declared) add(declared, block.title ?? undefined);
        break;
      }
      case "gauge": {
        const gd = block.data as GaugeBlockData | undefined;
        // No target, no verdict (CAV-08): nothing is coloured, nothing to state.
        if (gd?.target === null || gd?.target === undefined) break;
        add(asDirection(gd?.direction), block.title ?? undefined);
        break;
      }
      case "funnel":
        // A funnel colours its passage rate against a threshold: a higher rate
        // is the favourable one, whatever the metric underneath.
        add("up_good", "funnel steps");
        break;
      default:
        break;
    }
  }

  return found;
}
