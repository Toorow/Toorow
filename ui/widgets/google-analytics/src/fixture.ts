/**
 * Standalone dev/test fixture (Data Delivery to Widget - Dev Notes).
 *
 * GENERATED FILE - do not hand-edit (AI-54: a widget fixture mirrors what the
 * server ACTUALLY emits, never the speculated shape). Regenerate with:
 *
 *     uv run python scripts/gen_ga_widget_fixture.py
 *
 * The payload in __fixtures__/envelope.json is a snapshot of the REAL
 * get_daily_report structuredContent against the real local warehouse (seed ->
 * dbt -> fact_daily_kpi), captured by the generator script above. What the
 * widget receives at runtime is this same envelope, rehydrated from the
 * Story 50.6 model-channel split (ui/shell/src/mcpApp.ts rehydrateEnvelope).
 *
 * Used only when NO structuredContent is delivered by the host (i.e. the widget
 * is opened outside an MCP host, e.g. `vite preview` or a Node smoke render).
 */

import type { DailyReportEnvelope } from "./types";
import envelope from "./__fixtures__/envelope.json";

export const FIXTURE_ENVELOPE: DailyReportEnvelope =
  envelope as unknown as DailyReportEnvelope;
