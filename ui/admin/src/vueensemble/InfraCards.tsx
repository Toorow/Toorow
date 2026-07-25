/**
 * InfraCards -- compact bottom-row cards: mirror sync + circuit breakers (Story 8.4).
 *
 * Rule 4: Breaker states use subtle tinted StatusDots -- no solid MUI green chips.
 * Rule 1: One accent; breaker dot colors are semantic (not accent).
 * Rule 2: #EBEBF3 borders / tints only.
 * Rule 5: Generous padding; two cards side-by-side with gap 16.
 *
 * Reuses: nothing from PipelinePanel (that panel has its own MUI Chip-based display;
 * we build a clean replacement here per the 6 design rules).
 *
 * UX-DR10: French-first copy.
 */
import { Box, Skeleton, Tooltip, Typography } from "@mui/material";
import type { BreakerInfo } from "./types";

// ---------------------------------------------------------------------------
// Design tokens
// ---------------------------------------------------------------------------
const CARD_BG = "background.paper";
const BORDER = "rgba(235,235,243,0.30)";
const CARD_RADIUS = 1.5; // MUI borderRadius units (= 12px)
const HEADER_COLOR = "rgba(1,0,10,0.45)";

// ---------------------------------------------------------------------------
// Breaker state -> dot color mapping
// ---------------------------------------------------------------------------
const BREAKER_DOT: Record<string, string> = {
  closed: "#2E7D32",
  half_open: "#F57C00",
  open: "#E53935",
};
const BREAKER_LABEL: Record<string, string> = {
  closed: "Closed (OK)",
  half_open: "Half-open",
  open: "Open (blocked)",
};

function BreakerDot({ state }: { state: string }) {
  const color = BREAKER_DOT[state] ?? "rgba(235,235,243,0.40)";
  const label = BREAKER_LABEL[state] ?? state;
  return (
    <Tooltip title={label} arrow placement="top">
      <span
        aria-label={label}
        style={{
          display: "inline-block",
          width: 8,
          height: 8,
          borderRadius: "50%",
          backgroundColor: color,
          flexShrink: 0,
        }}
      />
    </Tooltip>
  );
}

// ---------------------------------------------------------------------------
// Time ago helper
// ---------------------------------------------------------------------------
function timeAgo(isoStr: string | null): string {
  if (!isoStr) return "Never";
  try {
    const diff = (Date.now() - new Date(isoStr).getTime()) / 1000;
    if (diff < 60) return "Less than a minute ago";
    if (diff < 3600) return `${Math.floor(diff / 60)} mins ago`;
    if (diff < 86400) {
      const h = Math.floor(diff / 3600);
      const m = Math.floor((diff % 3600) / 60);
      return m > 0 ? `${h}h ${m}m ago` : `${h}h ago`;
    }
    return `${Math.floor(diff / 86400)}d ago`;
  } catch {
    return isoStr;
  }
}

// ---------------------------------------------------------------------------
// CompactCard shell
// ---------------------------------------------------------------------------
function CompactCard({ title, children, testId }: {
  title: string;
  children: React.ReactNode;
  testId?: string;
}) {
  return (
    <Box
      data-testid={testId}
      sx={{
        flex: "1 1 220px",
        minWidth: 180,
        bgcolor: CARD_BG,
        borderRadius: CARD_RADIUS,
        border: `1px solid ${BORDER}`,
        boxShadow: "0 2px 12px rgba(1,0,10,0.06)",
        p: 3,
        display: "flex",
        flexDirection: "column",
        gap: 1.5,
      }}
    >
      <Typography
        variant="caption"
        sx={{
          color: HEADER_COLOR,
          fontWeight: 500,
          textTransform: "uppercase",
          letterSpacing: "0.06em",
        }}
      >
        {title}
      </Typography>
      {children}
    </Box>
  );
}

// ---------------------------------------------------------------------------
// MirrorCard
// ---------------------------------------------------------------------------
interface MirrorCardProps {
  lastSync: string | null;
  loading?: boolean;
}

export function MirrorCard({ lastSync, loading }: MirrorCardProps) {
  return (
    <CompactCard title="Mirror Sync" testId="mirror-card">
      {loading ? (
        <Skeleton variant="text" width="70%" sx={{ bgcolor: "#EBEBF3" }} />
      ) : (
        <Box>
          <Typography
            variant="body2"
            sx={{
              color: lastSync ? "text.primary" : "text.disabled",
              fontVariantNumeric: "lining-nums tabular-nums",
            }}
          >
            {timeAgo(lastSync)}
          </Typography>
          {lastSync && (
            <Typography variant="caption" sx={{ color: "text.secondary", opacity: 0.6 }}>
              {new Intl.DateTimeFormat("en-US", {
                dateStyle: "short",
                timeStyle: "short",
              }).format(new Date(lastSync))}
            </Typography>
          )}
        </Box>
      )}
    </CompactCard>
  );
}

// ---------------------------------------------------------------------------
// BreakersCard
// ---------------------------------------------------------------------------
interface BreakersCardProps {
  breakers: BreakerInfo[];
  loading?: boolean;
}

export function BreakersCard({ breakers, loading }: BreakersCardProps) {
  return (
    <CompactCard title="Circuit Breakers" testId="breakers-card">
      {loading ? (
        <Box sx={{ display: "flex", flexDirection: "column", gap: 1 }}>
          {[0, 1].map((i) => (
            <Skeleton key={i} variant="text" width="80%" sx={{ bgcolor: "#EBEBF3" }} />
          ))}
        </Box>
      ) : breakers.length === 0 ? (
        <Typography variant="body2" sx={{ color: "text.disabled" }}>
          No circuit breakers registered.
        </Typography>
      ) : (
        <Box component="ul" sx={{ m: 0, p: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 1 }}>
          {breakers.map((b) => (
            <Box
              component="li"
              key={b.platform}
              sx={{ display: "flex", alignItems: "center", gap: 1.25 }}
              data-testid={`breaker-${b.platform}`}
            >
              <BreakerDot state={b.state} />
              <Typography variant="body2" sx={{ flex: 1, color: "text.primary" }}>
                {b.platform}
              </Typography>
              {b.budget_remaining !== null && (
                <Typography
                  variant="caption"
                  sx={{
                    color: "text.secondary",
                    fontVariantNumeric: "lining-nums tabular-nums",
                  }}
                >
                  {b.budget_remaining.toLocaleString("fr-FR")}
                </Typography>
              )}
            </Box>
          ))}
        </Box>
      )}
    </CompactCard>
  );
}

// ---------------------------------------------------------------------------
// Composite InfraCards (both cards side by side)
// ---------------------------------------------------------------------------

interface InfraCardsProps {
  mirrorLastSync: string | null;
  breakers: BreakerInfo[];
  loading?: boolean;
}

export default function InfraCards({ mirrorLastSync, breakers, loading }: InfraCardsProps) {
  return (
    <Box data-testid="infra-cards" sx={{ display: "flex", flexWrap: "wrap", gap: 2 }}>
      <MirrorCard lastSync={mirrorLastSync} loading={loading} />
      <BreakersCard breakers={breakers} loading={loading} />
    </Box>
  );
}
