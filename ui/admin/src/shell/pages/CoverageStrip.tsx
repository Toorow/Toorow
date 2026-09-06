/**
 * CoverageStrip — one tick per calendar day, so gaps are visible instead of
 * being buried in a ten-row table.
 *
 * Reads GET /api/datastreams/{id}/ledger, which already returns exactly what a
 * coverage view needs: one entry per calendar day in the window, each with its
 * status, row count and pull_id. Nothing is derived, interpolated or smoothed —
 * a day with no evidence stays visibly empty.
 *
 * The status vocabulary is the ledger's own (core/extract_ledger.py) and the
 * distinction that matters is preserved rather than flattened:
 *
 *   ok             the day was pulled and verified
 *   partial        pulled, verification says incomplete
 *   empty          pulled and the provider legitimately returned nothing
 *   failed         the pull failed
 *   running        a pull is in flight
 *   never_fetched  never requested at all
 *
 * `empty` and `never_fetched` look alike on a chart and mean opposite things:
 * one is an answer, the other is an absence. Merging them would turn "we asked
 * and there was no traffic" into "we never asked", which is the kind of quiet
 * lie this project keeps finding. They get different marks and different counts.
 *
 * Refetch targets the days that can be improved, and `REPAIRABLE` is the ONE
 * place that decides which — `empty` joined them on 2026-08-06 (Jean), because a
 * provider can have filled its own hole since. `ok` and `running` stay out.
 *
 * AND IT CONFIRMS BEFORE IT SPENDS — story 58.4. Until then this screen posted
 * straight to `/refetch` on a click: no scope named, no provider account, no
 * cost said either way. The dialog is the same component the day grid opens
 * (`workbench/repullDay`), so the sentence a person reads before spending is one
 * sentence, whichever door they came through.
 */
import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../../lib/apiFetch";
import {
  Button, CoverageBars, EmptyState, Panel, PanelHeader, REPAIRABLE, Status,
  type CoverageDay, type CoverageStatus,
} from "../../ui";
import {
  RepullConfirmDialog, repullOutcomeSentence, useRepull,
} from "../../datastreams/workbench/repullDay";

interface LedgerDay {
  date: string;
  status: string;
  row_count?: number | null;
  /** Why `row_count` is absent when it is — story 58.1, the ledger's own word. */
  row_count_reason?: string | null;
  completeness_ratio?: number | null;
  /**
   * The covering window's own state — `extract_ledger._entry` publishes it on
   * every day of this payload, and this screen dropped it.
   *
   * A window the source REFUSED reports `never_fetched` at the day grain, so the
   * strip drew it as a day nobody asked for and its legend counted it as one.
   * The ledger has always sent the second half of that fact; the strip now reads
   * it, exactly like the day grid one tab away.
   */
  job_state?: string | null;
}

type State =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ok"; days: LedgerDay[] };

/**
 * The ledger's vocabulary is already the component's, one word for one word.
 * Anything unrecognised becomes `never_fetched` rather than being dropped: a
 * status this build does not know about is an absence of evidence, and the one
 * thing it must not do is silently disappear from the strip.
 */
const KNOWN: ReadonlySet<string> = new Set([
  "ok", "partial", "empty", "failed", "running", "never_fetched",
]);
function toCoverageDay(day: LedgerDay): CoverageDay {
  return {
    date: day.date,
    status: (KNOWN.has(day.status) ? day.status : "never_fetched") as CoverageStatus,
    rowCount: day.row_count ?? null,
    // A count that vanished and a count of zero look identical on a strip. The
    // ledger says WHY it has none for a day covered by a multi-day window, and
    // that reason travels to the tooltip rather than stopping here.
    rowCountReason: day.row_count_reason ?? null,
    // The window's own state travels with the verdict, never instead of it: a
    // refusal and an absence report the same `never_fetched` here.
    jobState: day.job_state ?? null,
  };
}

function isoDay(value: Date): string {
  return value.toISOString().slice(0, 10);
}

export default function CoverageStrip({
  projectId,
  datastreamId,
  connector,
  sourceAccountRef,
  days = 60,
}: {
  projectId: string;
  datastreamId: string;
  /** The provider, and the account it reads — `header.identity`, handed down by
   *  the Workbench route. The ledger payload carries neither, and a confirmation
   *  that named neither would let somebody spend on a connection they did not
   *  check. Absent, the dialog says `Not reported` rather than leaving a blank. */
  connector?: string | null;
  sourceAccountRef?: string | null;
  /** Window length. The ledger defaults to 35 days; 60 shows a fuller picture. */
  days?: number;
}) {
  const [state, setState] = useState<State>({ status: "loading" });
  const [outcome, setOutcome] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const repull = useRepull(projectId, datastreamId, (result) => {
    setOutcome(repullOutcomeSentence(result));
    setReload((n) => n + 1);
  });

  const load = useCallback(async () => {
    setState({ status: "loading" });
    const to = new Date();
    to.setUTCDate(to.getUTCDate() - 1); // the ledger's own default: through yesterday
    const from = new Date(to);
    from.setUTCDate(from.getUTCDate() - (days - 1));
    const query = new URLSearchParams({
      project_id: projectId,
      from: isoDay(from),
      to: isoDay(to),
    });
    try {
      const response = await apiFetch(
        `/api/datastreams/${encodeURIComponent(datastreamId)}/ledger?${query.toString()}`,
        { cache: "no-store" },
      );
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const body = (await response.json()) as { ledger?: LedgerDay[] };
      setState({ status: "ok", days: Array.isArray(body.ledger) ? body.ledger : [] });
    } catch (error) {
      // No substituted series: an unreadable ledger says so. A fabricated strip
      // would be read as evidence of coverage.
      setState({
        status: "error",
        message: error instanceof Error ? error.message : "Request failed",
      });
    }
  }, [projectId, datastreamId, days, reload]);

  useEffect(() => {
    void load();
  }, [load]);

  if (state.status === "loading") {
    return (
      <Panel>
        <PanelHeader title="Day-by-day coverage" />
        <p className="m-0 px-5 pb-4 text-ui text-text-secondary" role="status">
          Reading the extract ledger…
        </p>
      </Panel>
    );
  }

  if (state.status === "error") {
    return (
      <Status
        as="block"
        tone="error"
        title="Day-by-day coverage unavailable"
        action={
          <Button size="sm" variant="secondary" onClick={() => setReload((n) => n + 1)}>
            Retry
          </Button>
        }
      >
        The extract ledger could not be read ({state.message}). No coverage is shown — an
        invented strip would look like evidence.
      </Status>
    );
  }

  const coverage = state.days.map(toCoverageDay);
  const allRepairable = coverage.filter((d) => REPAIRABLE.has(d.status)).map((d) => d.date);

  return (
    <Panel flush data-testid="coverage-strip">
      <PanelHeader
        title="Day-by-day coverage"
        description="One mark per calendar day from the extract ledger. Days with no evidence stay empty — nothing here is interpolated."
        actions={
          /* Re-ask EVERY gap, without having to select a span first.
             `CoverageBars` repairs a selection, which is the precise tool; this
             is the blunt one, and dropping it in the migration would have been
             a silent loss of function — the previous screen had exactly this
             button and nothing else. Both target `REPAIRABLE`, and since 58.4
             both pass through the same confirmation before anything is spent. */
          allRepairable.length > 0 ? (
            <Button
              variant="secondary"
              size="sm"
              disabled={repull.busy}
              onClick={() => repull.ask(allRepairable)}
            >
              {repull.busy ? "Queueing…" : `Collect ${allRepairable.length} ${allRepairable.length === 1 ? "day" : "days"}`}
            </Button>
          ) : undefined
        }
      />
      <div className="p-5">
        {coverage.length === 0 ? (
          <EmptyState
            title="No day in this window"
            description="The ledger returned nothing for the requested interval."
          />
        ) : (
          <>
            {/* Selection, counts, the legend and the repairable filter all live
                in the component. This screen supplies the days and receives the
                dates to re-ask — it does not decide which ones are worth it. */}
            <CoverageBars days={coverage} onRepair={(dates) => repull.ask(dates)} />
            {outcome && (
              <p className="mt-3 mb-0 text-caption text-text-secondary" role="status">
                {outcome}
              </p>
            )}
          </>
        )}
      </div>
      {repull.days && (
        <RepullConfirmDialog
          open
          onOpenChange={() => repull.close()}
          days={repull.days}
          datastreamId={datastreamId}
          connector={connector}
          sourceAccountRef={sourceAccountRef}
          busy={repull.busy}
          error={repull.error}
          onConfirm={() => void repull.confirm()}
        />
      )}
    </Panel>
  );
}
