/**
 * KpiStrip -- 4 KPI cards for the control-tower home (Story 8.4).
 *
 * Rule 3: The number is the hero. Big, bold, tabular-nums.
 *         Delta is small and muted below.
 * Rule 1: One accent (#FF99C8) -- only used on delta positive indicator.
 * Rule 4: No default MUI look; no raised blue buttons; custom card shells.
 * Rule 5: Generous padding (24px card padding, 8px grid).
 *
 * UX-DR10: French-first copy.
 */
import { Box, Skeleton, Typography } from "@mui/material";
import type { OverviewKpis } from "./types";

// ---------------------------------------------------------------------------
// Design tokens (Epic 8 Part B)
// ---------------------------------------------------------------------------
const ACCENT = "#FF99C8";
// Note: NEAR_BLACK removed — use theme token color: "text.primary" instead
const NEUTRAL_TINT = "rgba(235,235,243,0.12)";
const CARD_RADIUS = 12;
const HERO_SX = {
  fontVariantNumeric: "lining-nums tabular-nums",
  fontWeight: 700,
  fontSize: "clamp(28px, 3vw, 40px)",
  lineHeight: 1.1,
  color: "text.primary",
  letterSpacing: "-0.02em",
} as const;

// ---------------------------------------------------------------------------
// KPI card definition
// ---------------------------------------------------------------------------
interface KpiCardDef {
  label: string;
  value: number;
  delta?: number;       // only for extracts_today_ok
  deltaLabel?: string;  // "vs hier"
  accentOnPositive?: boolean;
  testId: string;
}

function KpiCard({ label, value, delta, deltaLabel, accentOnPositive, testId }: KpiCardDef) {
  const showDelta = delta !== undefined && deltaLabel !== undefined;
  const deltaPositive = (delta ?? 0) >= 0;
  const deltaColor = accentOnPositive
    ? deltaPositive
      ? ACCENT
      : "#E53935"
    : "rgba(1,0,10,0.45)";

  return (
    <Box
      data-testid={testId}
      sx={{
        flex: "1 1 180px",
        minWidth: 140,
        maxWidth: 280,
        bgcolor: "background.paper",
        borderRadius: CARD_RADIUS / 8,
        p: 3,
        border: `1px solid ${NEUTRAL_TINT}`,
        boxShadow: "0 2px 12px rgba(1,0,10,0.06)",
        display: "flex",
        flexDirection: "column",
        gap: 0.5,
      }}
    >
      <Typography
        variant="caption"
        sx={{ color: "text.secondary", fontWeight: 500, textTransform: "uppercase", letterSpacing: "0.06em" }}
      >
        {label}
      </Typography>

      <Box sx={HERO_SX} aria-label={`${label}: ${value}`}>
        {value.toLocaleString("fr-FR")}
      </Box>

      {showDelta && (
        <Typography
          variant="caption"
          sx={{ color: deltaColor, fontVariantNumeric: "lining-nums tabular-nums", mt: 0.5 }}
          aria-label={`${deltaLabel}: ${delta! >= 0 ? "+" : ""}${delta}`}
        >
          {delta! >= 0 ? "+" : ""}
          {delta!.toLocaleString("fr-FR")} {deltaLabel}
        </Typography>
      )}
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Loading skeleton for one card
// ---------------------------------------------------------------------------
function KpiCardSkeleton() {
  return (
    <Box
      sx={{
        flex: "1 1 180px",
        minWidth: 140,
        maxWidth: 280,
        bgcolor: "background.paper",
        borderRadius: CARD_RADIUS / 8,
        p: 3,
        border: `1px solid ${NEUTRAL_TINT}`,
      }}
    >
      <Skeleton variant="text" width="60%" sx={{ bgcolor: "#EBEBF3" }} />
      <Skeleton variant="rectangular" height={40} sx={{ mt: 1, bgcolor: "#EBEBF3", borderRadius: 1 }} />
      <Skeleton variant="text" width="40%" sx={{ mt: 0.5, bgcolor: "#EBEBF3" }} />
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Public component
// ---------------------------------------------------------------------------

interface KpiStripProps {
  kpis?: OverviewKpis;
  loading?: boolean;
}

export default function KpiStrip({ kpis, loading }: KpiStripProps) {
  if (loading || !kpis) {
    return (
      <Box
        data-testid="kpi-strip-loading"
        sx={{ display: "flex", flexWrap: "wrap", gap: 2 }}
      >
        {[0, 1, 2, 3].map((i) => <KpiCardSkeleton key={i} />)}
      </Box>
    );
  }

  const cards: KpiCardDef[] = [
    {
      label: "Successful Extracts Today",
      value: kpis.extracts_today_ok,
      delta: kpis.vs_yesterday_ok_delta,
      deltaLabel: "vs yesterday",
      accentOnPositive: true,
      testId: "kpi-extracts-ok",
    },
    {
      label: "Failures Today",
      value: kpis.extracts_today_failed,
      testId: "kpi-extracts-failed",
    },
    {
      label: "In Progress",
      value: kpis.extracts_running,
      testId: "kpi-extracts-running",
    },
    {
      label: "Dead Letters (24h)",
      value: (kpis as OverviewKpis & { deadletters_24h?: number }).deadletters_24h ?? 0,
      testId: "kpi-dead-letters",
    },
  ];

  return (
    <Box
      data-testid="kpi-strip"
      sx={{ display: "flex", flexWrap: "wrap", gap: 2 }}
    >
      {cards.map((card) => (
        <KpiCard key={card.testId} {...card} />
      ))}
    </Box>
  );
}
