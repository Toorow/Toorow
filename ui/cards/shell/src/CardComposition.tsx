/**
 * CardComposition — maps `data.composition` blocks -> viz primitives in order (Story 9.2c).
 *
 * A card template declares `composition: [{type, binding, title?}, ...]`. The server
 * (Stories 9.3-9.6) resolves each block and attaches a typed payload under `block.data`.
 * This renderer reads `block.data` for EVERY block type and passes the correct typed props
 * to its primitive:
 *   kpi_row -> KpiRow  (from block.data.metrics[] or fallback to data.metrics for KPI card)
 *   line    -> LineChart  (block.data.series[{name,points:[{x,y}]}] -> [{label,points:[{index,value}]}])
 *   bar     -> BarChart   (block.data.bars[{label,value}], orientation)
 *   gauge   -> Gauge      (block.data.value/target/direction/unit/label)
 *   donut   -> Donut      (block.data.slices[{label,value}])
 *   funnel  -> Funnel     (block.data.steps[{label,value}])
 *   table   -> DataTable  (block.data.columns/rows — pre-existing path)
 *   comment -> CommentBlock (block.data.text ?? data.rendered_comment)
 *   unknown -> designed empty state, never throws
 *
 * Empty-safe: missing or empty block.data falls through to the primitive's own designed
 * empty state (empty arrays / null value). The outer try/catch catches any runtime error.
 *
 * Design rules: never throws, French-first, light+dark via MUI theme.
 */

import Box from "@mui/material/Box";
import Typography from "@mui/material/Typography";
import { alpha, useTheme } from "@mui/material/styles";
import type {
  CompositionBlock,
  CardData,
  MetricDefinition,
  DataTableColumn,
  KpiRowBlockData,
  LineBlockData,
  BarBlockData,
  DonutBlockData,
  GaugeBlockData,
  FunnelBlockData,
  TableBlockData,
  CommentBlockData,
} from "./types";
import DataTable from "./DataTable";
import LineChart from "./LineChart";
import BarChart from "./BarChart";
import Gauge from "./Gauge";
import Donut from "./Donut";
import Funnel from "./Funnel";

// KpiRow is re-implemented inline here to avoid circular dependency on ui/cards/kpi.
// It mirrors the essential shape of KpiMetricTile from KpiCardBody.tsx.
import { Sparkline, metricLabel } from "./index";
import type { MetricRollup, SeriesPoint } from "./types";

export interface CardCompositionProps {
  /** Ordered composition blocks from data.composition (server-resolved). */
  blocks: CompositionBlock[];
  /** The full card data (metrics, series, rendered_comment, metric_definitions). */
  data: CardData;
}

// ---------------------------------------------------------------------------
// KpiRow — inline hero tile for kpi_row blocks.
// 9.3-9.6: reads block.data (KpiRowBlockData.metrics[]) for server-shaped payloads.
// KPI card fallback: reads data.metrics / data.series (backward compat).
// ---------------------------------------------------------------------------

function KpiRowFromBlockData({
  blockMetrics,
  metricDefinitions,
}: {
  blockMetrics: KpiRowBlockData["metrics"];
  metricDefinitions?: Record<string, MetricDefinition>;
}) {
  const theme = useTheme();

  if (!blockMetrics || blockMetrics.length === 0) {
    return (
      <Box
        sx={{ py: 3, textAlign: "center", color: "text.secondary" }}
        data-testid="composition-kpi-row-empty"
      >
        <Typography variant="body2">Aucune métrique disponible.</Typography>
      </Box>
    );
  }

  return (
    <Box
      sx={{ display: "flex", flexWrap: "wrap", gap: 3, rowGap: 2.5 }}
      data-testid="composition-kpi-row"
    >
      {blockMetrics.map((m) => {
        const def = metricDefinitions?.[m.metric];
        const dir = m.direction ?? def?.direction ?? "up_good";
        const deltaPct = m.delta_pct ?? null;
        let color = theme.palette.text.secondary;
        if (deltaPct !== null && dir !== "neutral") {
          const isPositive = deltaPct >= 0;
          const isGood = dir === "up_good" ? isPositive : !isPositive;
          color = isGood ? theme.palette.success.main : theme.palette.error.main;
        }
        const deltaText =
          deltaPct === null ? "—" : `${deltaPct >= 0 ? "+" : ""}${deltaPct.toFixed(1)} %`;
        const value = m.value ?? 0;

        return (
          <Box
            key={m.metric}
            sx={{ flex: "1 1 180px", minWidth: 160, display: "flex", flexDirection: "column", gap: 0.5 }}
            data-testid="composition-kpi-tile"
            data-metric={m.metric}
          >
            <Typography variant="overline" color="text.secondary" sx={{ lineHeight: 1.2 }}>
              {metricLabel(m.metric)}
              {def?.unit ? ` (${def.unit})` : ""}
            </Typography>
            <Typography
              variant="h3"
              component="p"
              sx={{ fontWeight: 700, fontVariantNumeric: "lining-nums tabular-nums", lineHeight: 1.1 }}
              data-testid="composition-kpi-value"
            >
              {value.toLocaleString("fr-FR")}
            </Typography>
            <Typography
              variant="caption"
              sx={{ color, fontVariantNumeric: "lining-nums tabular-nums" }}
              data-testid="composition-kpi-delta"
            >
              {deltaText}
            </Typography>
          </Box>
        );
      })}
    </Box>
  );
}

function KpiRow({
  metrics,
  series,
  metricDefinitions,
}: {
  metrics: Record<string, MetricRollup>;
  series: Record<string, SeriesPoint[]>;
  metricDefinitions?: Record<string, MetricDefinition>;
}) {
  const theme = useTheme();
  const metricKeys = Object.keys(metrics);

  if (metricKeys.length === 0) {
    return (
      <Box
        sx={{ py: 3, textAlign: "center", color: "text.secondary" }}
        data-testid="composition-kpi-row-empty"
      >
        <Typography variant="body2">Aucune métrique disponible.</Typography>
      </Box>
    );
  }

  return (
    <Box
      sx={{ display: "flex", flexWrap: "wrap", gap: 3, rowGap: 2.5 }}
      data-testid="composition-kpi-row"
    >
      {metricKeys.map((metric) => {
        const rollup = metrics[metric];
        const points = series[metric] ?? [];
        const def = metricDefinitions?.[metric];
        const deltaPct = rollup.delta_pct ?? null;
        const dir = def?.direction ?? "up_good";
        let color = theme.palette.text.secondary;
        if (deltaPct !== null && dir !== "neutral") {
          const isPositive = deltaPct >= 0;
          const isGood = dir === "up_good" ? isPositive : !isPositive;
          color = isGood ? theme.palette.success.main : theme.palette.error.main;
        }
        const deltaText =
          deltaPct === null ? "—" : `${deltaPct >= 0 ? "+" : ""}${deltaPct.toFixed(1)} %`;

        return (
          <Box
            key={metric}
            sx={{ flex: "1 1 180px", minWidth: 160, display: "flex", flexDirection: "column", gap: 0.5 }}
            data-testid="composition-kpi-tile"
            data-metric={metric}
          >
            <Typography variant="overline" color="text.secondary" sx={{ lineHeight: 1.2 }}>
              {metricLabel(metric)}
              {def?.unit ? ` (${def.unit})` : ""}
            </Typography>
            <Typography
              variant="h3"
              component="p"
              sx={{ fontWeight: 700, fontVariantNumeric: "lining-nums tabular-nums", lineHeight: 1.1 }}
              data-testid="composition-kpi-value"
            >
              {rollup.value.toLocaleString("fr-FR")}
            </Typography>
            <Box sx={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 1 }}>
              <Typography
                variant="caption"
                sx={{ color, fontVariantNumeric: "lining-nums tabular-nums" }}
                data-testid="composition-kpi-delta"
              >
                {deltaText}
              </Typography>
              <Sparkline points={points} ariaLabel={`Tendance ${metricLabel(metric)}`} />
            </Box>
          </Box>
        );
      })}
    </Box>
  );
}

// ---------------------------------------------------------------------------
// CommentBlock — renders the cited comment slot inline in the composition.
// ---------------------------------------------------------------------------

function CommentBlock({ text }: { text: string }) {
  const theme = useTheme();
  if (!text) {
    return (
      <Box sx={{ py: 2, textAlign: "center", color: "text.secondary" }} data-testid="composition-comment-empty">
        <Typography variant="caption">Aucun commentaire disponible.</Typography>
      </Box>
    );
  }
  return (
    <Box
      sx={{
        pt: 1.5,
        borderTop: `1px solid ${alpha(theme.palette.text.primary, 0.08)}`,
      }}
      data-testid="composition-comment"
    >
      <Typography variant="overline" color="text.secondary" sx={{ lineHeight: 1, display: "block", mb: 0.5 }}>
        Commentaire
      </Typography>
      <Typography
        variant="body2"
        color="text.secondary"
        component="div"
        sx={{ whiteSpace: "pre-line", lineHeight: 1.6 }}
      >
        {text}
      </Typography>
    </Box>
  );
}

// ---------------------------------------------------------------------------
// UnknownBlock — designed empty state for unrecognised / future block types.
// ---------------------------------------------------------------------------

function UnknownBlock({ type }: { type: string }) {
  return (
    <Box
      sx={{ py: 2, textAlign: "center", color: "text.disabled" }}
      data-testid="composition-unknown-block"
      aria-label={`Bloc de type inconnu : ${type}`}
    >
      <Typography variant="caption">{`Bloc « ${type} » non disponible.`}</Typography>
    </Box>
  );
}

// ---------------------------------------------------------------------------
// TableBlock — renders a DataTable from block.data.rows/columns when present.
// ---------------------------------------------------------------------------

function TableBlock({
  block,
  title,
}: {
  block: CompositionBlock;
  title?: string;
}) {
  const d = block.data as TableBlockData | undefined;
  const rows = (d?.rows ?? []) as Record<string, unknown>[];
  const columns = (d?.columns ?? []) as DataTableColumn[];

  return (
    <Box data-testid="composition-table">
      {title && (
        <Typography variant="overline" color="text.secondary" sx={{ display: "block", mb: 0.5 }}>
          {title}
        </Typography>
      )}
      <DataTable
        rows={rows}
        columns={columns}
        ariaLabel={title ?? "Tableau"}
        sortable
      />
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Main CardComposition renderer
// ---------------------------------------------------------------------------

export default function CardComposition({ blocks, data }: CardCompositionProps) {
  if (!blocks || blocks.length === 0) {
    return (
      <Box
        sx={{ py: 4, textAlign: "center", color: "text.secondary" }}
        data-testid="card-composition-empty"
      >
        <Typography variant="body2">Composition non disponible.</Typography>
      </Box>
    );
  }

  return (
    <Box
      sx={{ display: "flex", flexDirection: "column", gap: 2 }}
      data-testid="card-composition"
    >
      {blocks.map((block, idx) => {
        const key = `${block.type}-${idx}`;
        try {
          switch (block.type) {
            case "kpi_row": {
              // 9.3-9.6: server attaches block.data = KpiRowBlockData {metrics:[...]}
              // KPI card fallback: no block.data at all -> use data.metrics/series (backward compat)
              // When block.data is present but metrics is empty, render the designed empty state.
              const kd = block.data as KpiRowBlockData | undefined;
              // Use server block.data path when block.data is explicitly present (even if empty).
              // Use the legacy data.metrics path ONLY when block.data is entirely absent.
              const hasServerPayload = kd !== undefined && kd.metrics !== undefined;
              return (
                <Box key={key} data-testid="composition-block-kpi_row">
                  {block.title && (
                    <Typography variant="overline" color="text.secondary" sx={{ display: "block", mb: 0.5 }}>
                      {block.title}
                    </Typography>
                  )}
                  {hasServerPayload ? (
                    <KpiRowFromBlockData
                      blockMetrics={kd!.metrics}
                      metricDefinitions={data.metric_definitions}
                    />
                  ) : (
                    <KpiRow
                      metrics={data.metrics}
                      series={data.series}
                      metricDefinitions={data.metric_definitions}
                    />
                  )}
                </Box>
              );
            }

            case "comment": {
              // 9.3-9.6: block.data = CommentBlockData {text}
              // Fallback: data.rendered_comment (KPI card backward compat)
              const cd = block.data as CommentBlockData | undefined;
              const text = cd?.text ?? data.rendered_comment ?? "";
              return (
                <Box key={key} data-testid="composition-block-comment">
                  <CommentBlock text={text} />
                </Box>
              );
            }

            case "line": {
              // block.data = LineBlockData {series:[{name, points:[{x:date, y:value}]}]}
              // Adapt {x,y} -> LineChart {index,value} shape.
              const ld = block.data as LineBlockData | undefined;
              const chartSeries =
                ld?.series && ld.series.length > 0
                  ? ld.series.map((s) => ({
                      label: s.name,
                      points: s.points.map((p) => ({ index: p.x, value: p.y })),
                    }))
                  : Object.keys(data.series).length > 0
                    ? [
                        {
                          label:
                            block.binding?.metrics !== "*"
                              ? String(block.binding?.metrics ?? "")
                              : Object.keys(data.series)[0] ?? "",
                          points: (
                            block.binding?.metrics !== "*"
                              ? (data.series[String(block.binding?.metrics)] ?? [])
                              : (data.series[Object.keys(data.series)[0]] ?? [])
                          ).map((p) => ({ index: p.date, value: p.value })),
                        },
                      ]
                    : [];
              return (
                <Box key={key} data-testid="composition-block-line">
                  {block.title && (
                    <Typography variant="overline" color="text.secondary" sx={{ display: "block", mb: 0.5 }}>
                      {block.title}
                    </Typography>
                  )}
                  <LineChart
                    series={chartSeries}
                    ariaLabel={block.title ?? "Courbe"}
                  />
                </Box>
              );
            }

            case "bar": {
              // block.data = BarBlockData {orientation, dimension, bars:[{label, value, direction?}]}
              // block.binding.direction (e.g. "down_good") is forwarded as semanticDirection so
              // BarChart can apply correct success/error coloring for movers bars (F-1 + F-3).
              const bd = block.data as BarBlockData | undefined;
              const entries = (bd?.bars ?? []).map((b) => ({
                label: b.label,
                // Pass |value| as value and keep direction for width scaling by |value| (F-1).
                value: Math.abs(b.value),
                direction: b.direction,
              }));
              const variant = bd?.orientation === "vertical" ? "vertical" : "horizontal";
              const bindingDir = (block.binding as Record<string, unknown> | undefined)?.direction;
              const semanticDirection =
                bindingDir === "down_good" ? "down_good"
                : bindingDir === "up_good" ? "up_good"
                : bindingDir === "neutral" ? "neutral"
                : undefined;
              return (
                <Box key={key} data-testid="composition-block-bar">
                  {block.title && (
                    <Typography variant="overline" color="text.secondary" sx={{ display: "block", mb: 0.5 }}>
                      {block.title}
                    </Typography>
                  )}
                  <BarChart
                    entries={entries}
                    variant={variant}
                    ariaLabel={block.title ?? "Barres"}
                    semanticDirection={semanticDirection as "up_good" | "down_good" | "neutral" | undefined}
                  />
                </Box>
              );
            }

            case "gauge": {
              // block.data = GaugeBlockData {value, target, direction, unit, label}
              // value may be null (zero-division -> Gauge empty state)
              const gd = block.data as GaugeBlockData | undefined;
              return (
                <Box key={key} data-testid="composition-block-gauge">
                  {block.title && (
                    <Typography variant="overline" color="text.secondary" sx={{ display: "block", mb: 0.5 }}>
                      {block.title}
                    </Typography>
                  )}
                  <Gauge
                    value={gd?.value ?? null}
                    target={gd?.target ?? undefined}
                    direction={gd?.direction ?? "neutral"}
                    unit={gd?.unit ?? ""}
                    label={gd?.label ?? block.title}
                    ariaLabel={block.title ?? "Jauge"}
                  />
                </Box>
              );
            }

            case "donut": {
              // block.data = DonutBlockData {total, dimension, slices:[{label, value, pct}]}
              const dd = block.data as DonutBlockData | undefined;
              const slices = (dd?.slices ?? []).map((s) => ({ label: s.label, value: s.value }));
              return (
                <Box key={key} data-testid="composition-block-donut">
                  {block.title && (
                    <Typography variant="overline" color="text.secondary" sx={{ display: "block", mb: 0.5 }}>
                      {block.title}
                    </Typography>
                  )}
                  <Donut
                    slices={slices}
                    unit={dd?.dimension ?? undefined}
                    ariaLabel={block.title ?? "Répartition"}
                  />
                </Box>
              );
            }

            case "funnel": {
              // block.data = FunnelBlockData {steps:[{label, value, rate}], overall_rate}
              // overall_rate is rendered as a NumberHero above the funnel bars per the
              // Journey card spec ("Funnel + NumberHero (overall rate)") — F-3.
              const fd = block.data as FunnelBlockData | undefined;
              const steps = (fd?.steps ?? []).map((s) => ({ label: s.label, value: s.value }));
              const overallRate = fd?.overall_rate;
              return (
                <Box key={key} data-testid="composition-block-funnel">
                  {block.title && (
                    <Typography variant="overline" color="text.secondary" sx={{ display: "block", mb: 0.5 }}>
                      {block.title}
                    </Typography>
                  )}
                  {overallRate !== null && overallRate !== undefined && (
                    <Box sx={{ mb: 1.5 }} data-testid="funnel-overall-rate">
                      <Typography
                        variant="h3"
                        component="p"
                        sx={{ fontWeight: 700, fontVariantNumeric: "lining-nums tabular-nums", lineHeight: 1.1 }}
                        data-testid="funnel-overall-rate-value"
                      >
                        {(overallRate * 100).toLocaleString("fr-FR", { maximumFractionDigits: 1 })} %
                      </Typography>
                      <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.25 }}>
                        Taux de conversion global
                      </Typography>
                    </Box>
                  )}
                  <Funnel steps={steps} ariaLabel={block.title ?? "Entonnoir"} />
                </Box>
              );
            }

            case "table":
              return (
                <TableBlock key={key} block={block} title={block.title} />
              );

            default:
              return <UnknownBlock key={key} type={String(block.type)} />;
          }
        } catch {
          // Last-resort: if a primitive throws, render a safe empty state (never crash).
          return <UnknownBlock key={key} type={String(block.type)} />;
        }
      })}
    </Box>
  );
}
