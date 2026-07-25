/**
 * App — signature heatmap widget assembly (AC1–AC9 / Story 1.6) with
 * View Tools (Story 1.7 T6).
 *
 * Layout (top → bottom):
 *   0. <ViewTools toolbar>  — granularity toggle + connector filter + metric switch (1.7)
 *   1. <KpiTileRow>         — delta-first KPI tiles (AC2)
 *   2. <CalendarHeatmap>    — 90-day calendar heatmap (AC1)
 *   3. <SmallMultiples>     — one mini-chart per connector, shared time axis (AC3)
 *   4. <DayDetail>          — day-click detail with provenance (AC4/AC5), conditional
 *
 * Everything is wrapped in <WidgetShell meta={...}> (AC9) — the shell owns the
 * MD3 theme, hostContext light/dark re-theming, stale badge, alerts, and the
 * transparent background. This widget adds NO ThemeProvider / background of its
 * own (AD-11 / NFR11).
 *
 * View Tools (Story 1.7):
 *   - granularity: "day" | "week" | "month" — controls in-DOM aggregation
 *   - selectedConnectors: string[] — connector filter (data-driven)
 *   - activeMetric: string — metric switch
 *   All three are local React state; ZERO network calls are made on any change (AD-10).
 *
 * review-global-gaps G-10:
 *   - feedbackContext and exportContext are now wired from envelope meta/data.
 *   - feedbackContext: projectId from meta.project_id, traceId from meta.trace_id,
 *     reportRef built from date range, module "google-analytics".
 *   - exportContext: targetRef to the widget root Box, filename built from date range.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import WidgetShell from "@toorow/shell";
import type { WidgetMeta } from "@toorow/shell";
import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

import KpiTileRow from "./KpiTileRow";
import CalendarHeatmap, { type ContextEvent } from "./CalendarHeatmap";
import SmallMultiples from "./SmallMultiples";
import BreakdownBars from "./BreakdownBars";
import DayDetail from "./DayDetail";
import {
  aggregateByDate,
  getConnectors,
  getDateDomain,
  getBreakdownDimensions,
} from "./dataUtils";
import {
  GranularityToggle,
  ConnectorFilter,
  MetricSwitch,
  DimensionSwitch,
  dimensionLabel,
  filterRows,
  aggregateRows,
} from "./ViewTools";
import type { Granularity } from "./ViewTools";
import { type DailyReportEnvelope, type Row, type ContextEventMeta } from "./types";
import {
  filterEvents,
  uniqueCategories,
  uniquePlatforms,
  type EventCategory,
} from "./eventMarkers";

interface AppProps {
  envelope: DailyReportEnvelope;
  adminConsoleUrl?: string;
}

export default function App({ envelope, adminConsoleUrl = "/admin" }: AppProps) {
  const meta = envelope.meta as unknown as WidgetMeta;
  const data = envelope.data;
  const allRows: Row[] = data?.rows ?? [];
  const dateRange = data?.date_range ?? { start: "", end: "" };

  // Story 4.4 AC7: context event markers from meta.context_events.
  // AD-11: data arrives in the envelope — no external fetch. Cast through unknown
  // because WidgetMeta (shell) doesn't know about context_events (core extension).
  const contextEvents: ContextEventMeta[] =
    (envelope.meta as unknown as { context_events?: ContextEventMeta[] }).context_events ?? [];

  // Story 31.5: derive available categories/platforms from the events for filter chips.
  const availableEventCategories = useMemo(() => uniqueCategories(contextEvents), [contextEvents]);
  const availableEventPlatforms = useMemo(() => uniquePlatforms(contextEvents), [contextEvents]);

  // Story 31.5: active event filters (empty = no filter = all events shown).
  const [activeEventCategories, setActiveEventCategories] = useState<EventCategory[]>([]);
  const [activeEventPlatforms, setActiveEventPlatforms] = useState<string[]>([]);

  // Story 31.5: filtered context events for overlay (category + platform filter).
  const filteredContextEvents = useMemo(
    () =>
      filterEvents(contextEvents, {
        categories: activeEventCategories.length > 0 ? activeEventCategories : undefined,
        platforms: activeEventPlatforms.length > 0 ? activeEventPlatforms : undefined,
      }),
    [contextEvents, activeEventCategories, activeEventPlatforms],
  );

  // CalendarHeatmap still uses ContextEvent shape (id, event_date, type, label).
  // ContextEventMeta is a superset, so the cast is safe.
  const contextEventsForHeatmap: ContextEvent[] = filteredContextEvents as unknown as ContextEvent[];

  // G-10: ref to the widget root for ExportButton capture.
  const widgetRootRef = useRef<HTMLDivElement | null>(null);

  // ---------------------------------------------------------------------------
  // View Tools state (Story 1.7 T6.1) — all ephemeral UI state, zero server calls.
  // ---------------------------------------------------------------------------
  const [granularity, setGranularity] = useState<Granularity>("day");

  // Derive the full connector list from data.connectors or from rows.
  const availableConnectors = useMemo(
    () => (data?.connectors?.length ? data.connectors : getConnectors(allRows)),
    [data?.connectors, allRows],
  );

  // Default: all connectors selected.
  const [selectedConnectors, setSelectedConnectors] = useState<string[]>(
    () => availableConnectors,
  );

  // review-1-7 F-03: if the payload is re-delivered with NEW connectors, they
  // must join the selection (the lazy useState initializer runs only once).
  const knownConnectors = useRef<string[]>(availableConnectors);
  useEffect(() => {
    const fresh = availableConnectors.filter((c) => !knownConnectors.current.includes(c));
    if (fresh.length > 0) {
      setSelectedConnectors((sel) => [...sel, ...fresh]);
    }
    knownConnectors.current = availableConnectors;
  }, [availableConnectors]);

  // Derive available metrics from loaded rows (data-driven, not hard-coded).
  const availableMetrics = useMemo(
    () => [...new Set(allRows.map((r) => r.metric))].sort(),
    [allRows],
  );

  const [activeMetric, setActiveMetric] = useState<string>(
    () => availableMetrics[0] ?? "sessions",
  );

  // Story 8.11 (R5): available breakdown dimensions for the active metric,
  // INCLUDING composite sub-dimension splits ('country>device'). Data-driven.
  const availableDimensions = useMemo(
    () => getBreakdownDimensions(allRows, activeMetric),
    [allRows, activeMetric],
  );

  const [activeDimension, setActiveDimension] = useState<string>(
    () => availableDimensions[0] ?? "",
  );

  // Keep the active dimension valid when the metric (and thus its dimensions)
  // changes: if the current selection disappears, fall back to the first one.
  useEffect(() => {
    if (availableDimensions.length === 0) return;
    if (!availableDimensions.includes(activeDimension)) {
      setActiveDimension(availableDimensions[0]);
    }
  }, [availableDimensions, activeDimension]);

  // ---------------------------------------------------------------------------
  // Derived display dataset (T6.2 / T6.3) — re-derives on every state change
  // with NO network call. Pure in-DOM aggregation over the preloaded rows.
  // ---------------------------------------------------------------------------
  const displayRows = useMemo(() => {
    const filtered = filterRows(allRows, selectedConnectors);
    return aggregateRows(filtered, granularity);
  }, [allRows, selectedConnectors, granularity]);

  // For heatmap: aggregate by date for the active metric.
  const heatmapValues = useMemo(
    () => aggregateByDate(displayRows as Row[], activeMetric),
    [displayRows, activeMetric],
  );

  // For small multiples: shared date domain from display rows.
  const dateDomain = useMemo(
    () => getDateDomain(displayRows as Row[], activeMetric, dateRange),
    [displayRows, activeMetric, dateRange],
  );

  const activeMetricLabel = activeMetric
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");

  // Day detail is only relevant in day-granularity view.
  const [selectedDay, setSelectedDay] = useState<string | null>(null);

  // Connector filter should update if available connectors change (e.g. data reload).
  // Keep selection in sync: remove any selected connector no longer available.
  const validSelected = useMemo(
    () =>
      selectedConnectors.filter((c) => availableConnectors.includes(c)).length > 0
        ? selectedConnectors.filter((c) => availableConnectors.includes(c))
        : availableConnectors,
    [selectedConnectors, availableConnectors],
  );

  // Empty state copy (UX-DR10).
  const hasData = displayRows.length > 0;

  // G-10: feedbackContext — sourced from envelope meta + data.
  const metaExt = envelope.meta as unknown as {
    project_id?: string | null;
    trace_id?: string | null;
  };
  const feedbackContext = {
    projectId: metaExt.project_id ?? "default",
    reportRef: `get_daily_report:${dateRange.start}:${dateRange.end}`,
    module: "google-analytics",
  };

  // G-10: exportContext — targetRef points at widgetRootRef; filename derived from date range.
  const exportContext = {
    targetRef: widgetRootRef as React.RefObject<HTMLElement | null>,
    filename: `google-analytics-${dateRange.start}-${dateRange.end}`,
  };

  return (
    <WidgetShell
      meta={meta}
      adminConsoleUrl={adminConsoleUrl}
      feedbackContext={feedbackContext}
      exportContext={exportContext}
    >
      <Box
        ref={widgetRootRef}
        sx={{ display: "flex", flexDirection: "column", gap: 2, p: 1 }}
      >
        <Typography variant="h6">Rapport quotidien</Typography>

        {/* Story 1.7 T6.4 — View Tools toolbar (above heatmap) */}
        <Stack direction="row" spacing={1} sx={{ flexWrap: "wrap" }}>
          {/* T2 — Granularity toggle */}
          <GranularityToggle value={granularity} onChange={setGranularity} />

          {/* T3 — Connector filter (data-driven; only when > 1 connector or AC2 requires it) */}
          {availableConnectors.length >= 1 && (
            <ConnectorFilter
              connectors={availableConnectors}
              selected={validSelected}
              onChange={setSelectedConnectors}
            />
          )}

          {/* T4 — Metric switch (data-driven from available metrics) */}
          {availableMetrics.length >= 1 && (
            <MetricSwitch
              metrics={availableMetrics}
              value={activeMetric}
              onChange={(m) => {
                setActiveMetric(m);
                // Reset day selection when metric changes.
                setSelectedDay(null);
              }}
            />
          )}

          {/* Story 8.11 (R5) — breakdown dimension switch incl. composite splits.
              Only shown when there is more than one dimension to choose from. */}
          {availableDimensions.length > 1 && (
            <DimensionSwitch
              dimensions={availableDimensions}
              value={activeDimension}
              onChange={setActiveDimension}
            />
          )}
        </Stack>

        {/* AC2 — delta-first KPI tiles (use displayRows so connector filter applies) */}
        {/* review-1-7 F-01: KPI deltas are computed on day-grain rows in ALL
            granularities — week/month bucket start-dates fall outside the
            comparison windows and would silently corrupt the deltas. Tiles
            stay anchored to the original day window; only the charts follow
            the granularity toggle. Connector filter still applies. */}
        {/* Story 8.8 (R6): pass metric_definitions for tooltip/direction affordances. */}
        <KpiTileRow
          rows={filterRows(allRows, validSelected)}
          dateRange={dateRange}
          metricDefinitions={data?.metric_definitions}
        />

        {/* Story 31.5 — Event annotation filter (category + platform chips).
            Only shown when there are context events. Zero network calls (AD-10). */}
        {contextEvents.length > 0 && (
          <Box>
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 0.5 }}>
              Filtrer les événements :
            </Typography>
            <Stack direction="row" spacing={0.5} sx={{ flexWrap: "wrap", gap: 0.5 }}>
              {availableEventCategories.map((cat) => {
                const active = activeEventCategories.includes(cat);
                return (
                  <Chip
                    key={cat}
                    label={cat}
                    size="small"
                    variant={active ? "filled" : "outlined"}
                    color={active ? "primary" : "default"}
                    onClick={() =>
                      setActiveEventCategories((prev) =>
                        active ? prev.filter((c) => c !== cat) : [...prev, cat],
                      )
                    }
                  />
                );
              })}
              {availableEventPlatforms.map((plat) => {
                const active = activeEventPlatforms.includes(plat);
                return (
                  <Chip
                    key={plat}
                    label={plat}
                    size="small"
                    variant={active ? "filled" : "outlined"}
                    color={active ? "secondary" : "default"}
                    onClick={() =>
                      setActiveEventPlatforms((prev) =>
                        active ? prev.filter((p) => p !== plat) : [...prev, plat],
                      )
                    }
                  />
                );
              })}
            </Stack>
          </Box>
        )}

        {/* AC1 — calendar heatmap */}
        {hasData ? (
          <CalendarHeatmap
            values={heatmapValues}
            dateRange={dateRange}
            onDayClick={(date) => {
              // Only allow day-click drill-down in day granularity view.
              if (granularity === "day") setSelectedDay(date);
            }}
            metricLabel={activeMetricLabel}
            contextEvents={contextEventsForHeatmap}
          />
        ) : (
          <Typography variant="body2" color="text.secondary">
            Aucune donnée pour la sélection actuelle.
          </Typography>
        )}

        {/* AC3 — small multiples per connector + Story 31.5 event overlay. */}
        <SmallMultiples
          rows={displayRows as Row[]}
          connectors={validSelected}
          dateDomain={dateDomain}
          metricLabel={activeMetricLabel}
          metric={activeMetric}
          contextEvents={filteredContextEvents}
        />

        {/* Story 8.11 (R5) — breakdown panel for the active dimension.
            Renders composite splits ('country>device') as « FR > mobile ». Uses
            the connector-filtered day-grain rows and pins ONE dimension so
            composite/single series are never mixed (no double count). */}
        {activeDimension && (
          <BreakdownBars
            rows={filterRows(allRows, validSelected)}
            metric={activeMetric}
            dimension={activeDimension}
            metricLabel={activeMetricLabel}
            dimensionLabel={dimensionLabel(activeDimension)}
            dateRange={dateRange}
          />
        )}

        {/* AC4 / AC5 — day detail with provenance drill-down (day granularity only) */}
        {granularity === "day" && (
          <DayDetail
            date={selectedDay}
            rows={allRows}
            meta={envelope.meta}
            onClose={() => setSelectedDay(null)}
          />
        )}
      </Box>
    </WidgetShell>
  );
}
