/**
 * CacheHealthCard -- the read-through DuckDB cache, on the screen that owns it.
 *
 * IT IS MOUNTED. This header carried the opposite verdict until 2026-08-17 --
 * "UNMOUNTED, DEBT, its owner is NOT NAMED" -- and that had stopped being true:
 * `shell/pages/PlatformClocks.tsx:80` imports it and `:748` renders it inside a
 * `<section aria-label="Warehouse cache health">`, under the platform clocks. So
 * the owner IS named: the operational surface that already carries the nightly
 * clocks carries the cache those clocks fill. A file that reports its own state
 * wrongly is worse than one that says nothing, because the next reader stops at
 * the header and re-opens a decision that was taken.
 *
 * What it shows: the cache state, its age, the cached tables with their windows
 * and row counts, the session hit rate WITH the number of routed queries it was
 * computed over, and the "no-cache" / "stale" / "disabled" states said out loud
 * rather than smoothed over (AD-9, invariant c/f).
 *
 *   fresh     the cache is current and queries use it
 *   stale     the nightly ran without a rebuild -- queries bypass it, and no
 *             stale row is ever served
 *   no-cache  the ephemeral file is gone (a Cloud Run restart); the origin
 *             answers, at nominal latency
 *   disabled  TOOROW_CACHE_ENABLED=false -- a switch, not a fault
 *
 * The tone and the wording of those four words are `ui/stateVocabulary`, not a
 * table in this file: `stale` was amber here and amber in five other screens by
 * coincidence, and nothing would have caught a sixth spelling it red.
 *
 * The "Rebuild" button POSTs /api/admin/cache/rebuild behind a confirmation
 * dialog. AD-8: every operation goes through the REST API, never a direct DB
 * call.
 */

import { useState } from "react";
import { ApiError, apiGet, apiPost } from "../lib/apiFetch";
import { usePolledRead } from "../lib/polledRead";
import {
  Badge,
  Button,
  ConfirmDialog,
  Metric,
  Panel,
  Skeleton,
  Spinner,
  Status,
  stateLabel,
  stateTone,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  TableScroll,
  Timestamp,
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
  type Tone,
  formatNumber,
  formatPercent,
} from "../ui";

// Inline refresh icon (no @mui/icons-material -- bundle weight, cf. Sidebar.tsx).
function _RefreshSvg() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
      <path d="M17.65 6.35A7.95 7.95 0 0 0 12 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0 1 12 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"/>
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface CacheStatus {
  cache_state: "fresh" | "stale" | "no-cache" | "disabled";
  cache_enabled: boolean;
  cache_built_at: string | null;
  age_seconds: number | null;
  min_date: string | null;
  max_date: string | null;
  tables: string[];
  row_counts: Record<string, number>;
  project_ids: string[];
  hit_rate: number | null;
  stats: Record<string, number>;
  last_rebuild_cause: string | null;
}

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

/**
 * Through the seam, not around it -- story 63.3.
 *
 * This file built its own `Authorization` header from `__TOOROW_API_KEY__`
 * alone, so it never saw the token `AuthGate` puts in `localStorage`
 * (`lib/apiFetch.ts:40-44`). It also threw `new Error("HTTP 503")`, which
 * carries no status -- and a poll that cannot read a status cannot tell a
 * refusal (stop at once) from a failure (retry, five times). `apiGet` throws a
 * typed `ApiError` that carries both.
 */
async function fetchCacheStatus(signal: AbortSignal): Promise<CacheStatus> {
  const data = await apiGet<unknown>("/api/admin/cache/status", { signal });
  // Invariant f: degrade gracefully when the payload is unexpected (tests, proxies).
  if (!data || typeof data !== "object" || !("cache_state" in data)) {
    throw new Error("Unexpected response from the cache server");
  }
  return data as CacheStatus;
}

async function triggerRebuild(): Promise<{ status: string; performed_by: string }> {
  return apiPost<{ status: string; performed_by: string }>("/api/admin/cache/rebuild");
}

// ---------------------------------------------------------------------------
// UI helpers
// ---------------------------------------------------------------------------

function _formatAge(seconds: number | null): string {
  if (seconds === null) return "—";
  if (seconds < 60) return `${Math.round(seconds)} s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return m > 0 ? `${h} h ${m} min` : `${h} h`;
}

/**
 * The state, as a chip.
 *
 * The four words and their tones used to be a table right here -- the sixth copy
 * of a map the console kept re-declaring. They are `ui/stateVocabulary` now, and
 * this card disagrees with it about nothing: an absent or a disabled cache is
 * NEUTRAL there for the same reason it was neutral here, because neither is a
 * failure and colouring one as a failure invents a verdict.
 */
function _stateChip(state: CacheStatus["cache_state"]) {
  return <Badge tone={stateTone(state)}>{stateLabel(state)}</Badge>;
}

/**
 * The population the session hit rate was computed over, said out loud.
 *
 * `stats` is the per-decision counter map (`hit`, `miss-relation`, `miss-window`,
 * `bypass-stale`, `disabled`, …) the server divides to get the rate. Three
 * readings, and the third is the one that was invisible: a rate of `null` next
 * to a non-empty map is not "no data", it is "the cache was off and every one of
 * these queries went to the origin" — a fact, not a silence.
 *
 * The counters are process-local (`warehouse.get_cache_stats`), so the sentence
 * says *this process*: a rate over 40 queries on one instance is not a fleet
 * measurement, and calling it one would be the same caveat one floor up.
 */
function _hitRatePopulation(status: CacheStatus): string {
  const decisions = Object.values(status.stats ?? {}).reduce<number>(
    (total, count) => total + (Number(count) || 0),
    0
  );
  if (decisions === 0) {
    return "No query has been routed by this process yet.";
  }
  const routed = `${formatNumber(decisions)} ${decisions === 1 ? "query" : "queries"}`;
  if (status.hit_rate === null) {
    return `${routed} routed by this process, none of them through the cache.`;
  }
  return `Over ${routed} routed by this process.`;
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

interface CacheHealthCardProps {
  /** Auto-refresh interval in milliseconds (default: 60,000 = 1 min). */
  refreshIntervalMs?: number;
}

export default function CacheHealthCard({
  refreshIntervalMs = 60_000,
}: CacheHealthCardProps) {
  /**
   * The same poll as story 63.3's, from the same module -- story 63.3.
   *
   * This card was the repository's only network poll, and it never stopped:
   * `setInterval(load, 60_000)` ran for a hidden tab, for a dead route and for
   * a `401`, forever, on a service that scales to zero. Leaving it outside the
   * convention 63.3 sets would have left the next reader two answers to "how do
   * we poll here". `usePolledRead` carries the stop: hidden tab suspended and
   * resumed by one immediate read, `401`/`403`/`404` at once, five failures
   * backing off exponentially and then silence -- plus `measuredAt`, so the
   * figures below never read as current when they are not.
   */
  const poll = usePolledRead<CacheStatus>({
    key: "cache-status",
    intervalMs: refreshIntervalMs,
    read: (signal) => fetchCacheStatus(signal),
  });
  const status = poll.value;
  // Still loading means: nothing has landed yet, either way.
  const loading = poll.measuredAt === null && poll.error === null;
  const error = poll.error?.message ?? null;

  const [rebuilding, setRebuilding] = useState(false);
  /**
   * The rebuild's outcome as a TONE and a SENTENCE, never a string parsed for a
   * word.
   *
   * It used to be one `string`, built as `Error: ${String(e)}` in the catch and
   * read back with `rebuildResult.startsWith("Error")` to pick a colour. Two
   * defects in one value: the tone rested on the spelling of a prefix this file
   * wrote to itself, so a server message beginning with anything else was drawn
   * green; and `String(e)` on the `ApiError` `apiPost` throws produces
   * `ApiError: <message>` -- the class name, in front of the sentence the server
   * actually sent, on a screen where that sentence is the only thing that says
   * what to do next.
   */
  const [rebuildOutcome, setRebuildOutcome] = useState<{ tone: Tone; message: string } | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);

  const handleRebuildClick = () => setConfirmOpen(true);

  const handleRebuildConfirm = async () => {
    setConfirmOpen(false);
    setRebuilding(true);
    setRebuildOutcome(null);
    try {
      const result = await triggerRebuild();
      setRebuildOutcome(
        result.status === "ok"
          ? { tone: "success", message: "The cache was rebuilt from the origin warehouse." }
          : {
              // The server's own word, in the console's spelling and the
              // console's colour rather than in this file's.
              tone: stateTone(result.status),
              message: `The rebuild did not complete: ${stateLabel(result.status)}.`,
            }
      );
      // Reload the status after the rebuild. A person asking is not a poll:
      // `refresh` also resumes a poll that had given up.
      poll.refresh();
    } catch (e) {
      setRebuildOutcome({
        tone: "error",
        // `ApiError` carries the `{code, message}` envelope the API returns, so
        // there IS a sentence to show. The fallback is for a throw that is not
        // one -- and it names a gesture rather than a stack.
        message:
          e instanceof ApiError || e instanceof Error
            ? e.message
            : "The rebuild could not be started. Try again, or check the service is reachable.",
      });
    } finally {
      setRebuilding(false);
    }
  };

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  return (
    <Panel>
        {/* Header */}
        <div className="mb-5 flex items-center gap-4">
          <span className="text-label font-label uppercase tracking-wider text-text">
            DuckDB cache
          </span>
          {status && _stateChip(status.cache_state)}
          <div className="flex-1" />
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                {/* The span stays: a disabled button emits no pointer events, so
                    the tooltip explaining why it is disabled would never open. */}
                <span>
                  <Button
                    variant="secondary"
                    size="sm"
                    disabled={rebuilding || loading || !status?.cache_enabled}
                    onClick={handleRebuildClick}
                    data-testid="cache-rebuild-button"
                  >
                    {rebuilding ? (
                      <Spinner size="inline" label="Rebuilding" />
                    ) : (
                      <_RefreshSvg />
                    )}
                    Rebuild
                  </Button>
                </span>
              </TooltipTrigger>
              <TooltipContent side="top">
                Trigger a manual cache rebuild
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        </div>

        {/* Initial loading. The shape IS known here -- two lines and a block --
            which is what makes a skeleton honest rather than a spinner. */}
        {loading && (
          <div className="flex flex-col gap-2" aria-busy>
            <Skeleton className="h-4 w-[200px]" />
            <Skeleton className="h-4 w-[160px]" />
            <Skeleton className="h-20 w-full" />
          </div>
        )}

        {/* Error. A poll that gave up says so, and says how to get it back --
            a refresh that silently stopped is indistinguishable from a service
            that has nothing new to report. */}
        {!loading && error && (
          <Status
            as="block"
            tone="error"
            className="mb-4"
            title="The cache status could not be read"
            data-testid="cache-error"
            action={(
              <Button variant="secondary" size="sm" onClick={poll.refresh} data-testid="cache-retry-button">
                Retry
              </Button>
            )}
          >
            <span className="flex flex-wrap items-center gap-2">
              <span>
                {error}
                {poll.stopped
                  ? ` Automatic refresh stopped after ${poll.attempts} attempt${poll.attempts > 1 ? "s" : ""}.`
                  : ""}
              </span>
            </span>
          </Status>
        )}

        {/* Rebuild result. The tone is carried WITH the sentence, so nothing
            here has to read the sentence to decide what colour it is. */}
        {rebuildOutcome && (
          <Status
            as="block"
            tone={rebuildOutcome.tone}
            title={rebuildOutcome.tone === "error" ? "The rebuild was refused" : undefined}
            className="mb-4"
            data-testid="cache-rebuild-outcome"
          >
            {rebuildOutcome.message}
          </Status>
        )}

        {/* Content */}
        {!loading && status && (
          <div className="flex flex-col gap-4">
            {/* WHEN these numbers were measured -- rendered with them, always,
                and never omitted on the path where a reload has just failed.
                An old figure shown as a current one is a lie the reader cannot
                detect; the same rule the progress poll (story 63.3) carries. */}
            {/* `Timestamp`, not `toLocaleString()`: the console renders one
                instant one way, names the zone, and emits the exact ISO value
                in a `<time dateTime>` so nothing has to be re-derived from the
                visible text. This card was one of the six hand-rolled
                formatters that primitive was written to replace. */}
            {poll.measuredAt !== null && (
              <span className="text-caption text-text-secondary" data-testid="cache-measured-at">
                {error ? (
                  <>
                    Not current: this is the last successful measurement, taken at{" "}
                    <Timestamp value={poll.measuredAt} />.
                  </>
                ) : (
                  <>
                    Measured at <Timestamp value={poll.measuredAt} />.
                  </>
                )}
              </span>
            )}

            {/* Honest state warnings (invariant c/f) */}
            {status.cache_state === "disabled" && (
              <Status as="block" tone="info" data-testid="cache-state-disabled">
                The cache is disabled ({" "}
                <code>TOOROW_CACHE_ENABLED=false</code>). Every query goes
                straight to the origin.
              </Status>
            )}
            {status.cache_state === "no-cache" && (
              <Status as="block" tone="info" data-testid="cache-state-no-cache">
                No cache present (Cloud Run restart?). The service reads from the
                origin — nominal latency until the next rebuild.
              </Status>
            )}
            {status.cache_state === "stale" && (
              <Status as="block" tone="warning" data-testid="cache-state-stale">
                Stale cache: the nightly ran without a rebuild. Queries bypass the
                cache (no stale data is ever served).
              </Status>
            )}

            {/* Key metrics. `Metric` is the primitive for exactly this pair --
                a caption over a business number -- and it carries the typeface
                rule the hand-rolled caption+h6 could not know about. */}
            <div className="flex flex-wrap gap-6">
              <Metric
                label="Age"
                value={_formatAge(status.age_seconds)}
                data-testid="cache-age"
              />
              <Metric
                label="Window"
                value={
                  status.min_date && status.max_date
                    ? `${status.min_date} → ${status.max_date}`
                    : "—"
                }
                data-testid="cache-window"
              />
              {/* A RATE AND ITS DENOMINATOR, never the rate alone.
                  `caveats-register.md`, "Incomplete if": *a number is displayed
                  without the population it was computed over*. This card
                  declared `stats` on `CacheStatus` and drew nothing with it, so
                  "0.0 %" over two routed queries and "0.0 %" over twenty
                  thousand read identically — and the first is noise while the
                  second is a broken cache. The population is not derived here:
                  the server computes the rate as `hits / sum(stats.values())`
                  (`platform_maintenance_api.py`), so summing the same map is
                  the same denominator and not a second opinion. */}
              <Metric
                label="Session hit rate"
                value={
                  // A ratio on the wire, a percentage on the screen -- and the
                  // dash for the absence, which `formatPercent` already spells.
                  formatPercent(status.hit_rate)
                }
                hint={_hitRatePopulation(status)}
                data-testid="cache-hit-rate"
              />
              <Metric
                label="Projects covered"
                value={
                  (status.project_ids ?? []).length > 0
                    ? (status.project_ids ?? []).join(", ")
                    : "—"
                }
                data-testid="cache-projects"
              />
            </div>

            {/* Table by table */}
            {(status.tables ?? []).length > 0 && (
              <div>
                <span className="mb-2 block text-caption text-text-secondary">
                  Cached tables
                </span>
                <TableScroll label="Cached tables">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Table</TableHead>
                        <TableHead className="text-right">Rows</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {(status.tables ?? []).map((t) => (
                        <TableRow key={t}>
                          <TableCell className="font-mono">{t}</TableCell>
                          <TableCell className="text-right tabular-nums">
                            {formatNumber(status.row_counts[t] ?? 0)}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </TableScroll>
              </div>
            )}

            {/* Last rebuild */}
            {status.cache_built_at && (
              <span className="text-caption text-text-secondary">
                Last build: <Timestamp value={status.cache_built_at} />
              </span>
            )}
          </div>
        )}

      {/* Rebuild confirmation dialog */}
      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title="Rebuild the cache?"
        description="This reloads the marts from the origin warehouse and writes a fresh DuckDB snapshot. It can take a few seconds."
        confirmLabel="Rebuild"
        onConfirm={handleRebuildConfirm}
        confirmTestId="cache-rebuild-confirm"
      />
    </Panel>
  );
}
