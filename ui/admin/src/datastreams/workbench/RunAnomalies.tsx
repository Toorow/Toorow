/**
 * THE ANOMALY, UNFOLDED INSIDE ITS RUN -- story 59.1.
 *
 * 58.10 laid the region and read the count; this fills it with the issues, the
 * replayed rows, the export and the acknowledgement. It stays a `Collapsible`
 * INSIDE the run block and never a dialog: the ratified surface says "une
 * anomalie se déplie dans son run. Pas de modale."
 * (`datastream-workbench-and-wizard.md:922`), and a portal would move the
 * content out of the run's subtree.
 *
 * THE ROWS ARE REPLAYED, NOT STORED (Jean, 2026-08-07), and two consequences are
 * on this screen rather than in a comment:
 *
 *   1. unfolding an issue COSTS A READ of the warehouse, so it is an explicit
 *      click per issue and never automatic. Nothing here fetches on mount;
 *   2. the rows are TODAY'S, not the detection's. `REPLAYED_NOW` says it above
 *      the table, because a table with no such sentence implies a snapshot this
 *      product does not have.
 *
 * AND THE SCREEN HOLDS NO LIST OF PROFILE NAMES. Whether an issue has rows to
 * replay travels on the issue (`replayable_rows`, decided by
 * `core.dq_issue_rows.REPLAY_PROFILES`). A `["zero_rows", "timeliness"]` typed
 * here would give the eighth profile the wrong default in silence -- the class
 * defect story 59.4 paid for five times. Without replayable rows: the profile's
 * own sentence, NO empty table under a heading that promises rows, and NO
 * `Download` -- an export that can export nothing is a button that lies.
 */
import { useState } from "react";
import { apiFetch } from "../../lib/apiFetch";
import {
  ObjectId,
  Badge, Button, Collapsible, CollapsibleContent, CollapsibleTrigger, Status,
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll,
  formatNumber,
  stateLabel,
  stateTone,
  Retry,
} from "../../ui";
import { dateTime, record, records, text } from "./evidence";

/** No evaluation named this run. TRUE OF THE RUN, and it says nothing else. */
export const NO_MONITOR_EVALUATED = "No monitor has been evaluated on this run.";
/** Evaluated, and nothing was found. A different fact, and a different sentence. */
export const NO_ANOMALY_ON_RUN = "No anomaly was found on this run.";

/**
 * The fourth fact of arbitrage 7, AT THE GRAIN OF THE DATASTREAM.
 *
 * An evaluation with no `execution_id` belongs to no run, so the run's sentence
 * above stays literally true — what a person must not conclude from it is "the
 * quality was never checked". Changing the run's sentence would fabricate an
 * attribution the data does not carry, which is the more expensive of the two
 * defects; so the missing fact is stated once, here, where its subject is.
 *
 * `app.pull_jobs.execution_id` is what would fill it, and it was NULL on 130
 * rows of 130 (disposable base) and 6 of 6 (preprod) when this was written — so
 * this is the majority case, not an edge one.
 */
export const EVALUATIONS_WITHOUT_RUN_ABSENCE =
  "A monitor names its run through app.pull_jobs.execution_id, which is null on every job " +
  "measured on both bases. An evaluation that could not name one is written without a run " +
  "rather than under a fabricated one, so it appears under no run above.";

/** The rows are today's. Said, or the table implies a snapshot it is not. */
export const REPLAYED_NOW =
  "These rows are read now, not kept from the detection: the condition is replayed over " +
  "the window this run was judged on, so a row repaired at the source since is no longer here.";

/** A replay that ran and matched nothing. NEVER the same as a reading that failed. */
export const NOTHING_MATCHES_NOW = "nothing_matches_now";

type Issue = {
  id: string;
  monitor_id: string;
  monitor_label: string | null;
  check_profile: string | null;
  status: string;
  severity: string;
  first_seen_at: unknown;
  last_seen_at: unknown;
  replayable_rows: boolean;
  rows_absence_message: string | null;
};

function issuesOf(counts: Record<string, unknown> | null): Issue[] {
  return records(counts?.issues).map((issue) => ({
    id: text(issue.id, ""),
    monitor_id: text(issue.monitor_id, ""),
    monitor_label: typeof issue.monitor_label === "string" ? issue.monitor_label : null,
    check_profile: typeof issue.check_profile === "string" ? issue.check_profile : null,
    status: text(issue.status, "unknown"),
    severity: text(issue.severity, "unknown"),
    first_seen_at: issue.first_seen_at,
    last_seen_at: issue.last_seen_at,
    // NEVER defaulted to `true`: a payload that does not say is a payload that
    // must not grow a `Download` button on a guess.
    replayable_rows: issue.replayable_rows === true,
    rows_absence_message:
      typeof issue.rows_absence_message === "string" ? issue.rows_absence_message : null,
  }));
}

/*
 * THE SEVERITY MAP IS GONE (76-2 review). The same three words as
 * `DatastreamIssueBadge` -- two files agreeing by coincidence -- are declared
 * in `ui/stateVocabulary.ts`, at the same tones.
 */

function rowsPath(
  projectId: string,
  datastreamId: string,
  runId: string,
  issueId: string,
): string {
  return (
    `/api/projects/${encodeURIComponent(projectId)}` +
    `/datastreams/${encodeURIComponent(datastreamId)}` +
    `/workbench/runs/${encodeURIComponent(runId)}` +
    `/anomalies/${encodeURIComponent(issueId)}/rows`
  );
}

type Replay =
  | { state: "idle" }
  | { state: "loading" }
  | { state: "failed"; message: string }
  | { state: "read"; payload: Record<string, unknown> };

/** ONE issue: what the monitor said, since when, and — on demand — its rows. */
function IssueBlock({
  issue,
  runId,
  projectId,
  datastreamId,
}: {
  issue: Issue;
  runId: string;
  projectId: string;
  datastreamId: string;
}) {
  const [replay, setReplay] = useState<Replay>({ state: "idle" });
  const [open, setOpen] = useState(false);
  const [transition, setTransition] = useState<string | null>(null);
  const [status, setStatus] = useState(issue.status);
  const [failure, setFailure] = useState<string | null>(null);

  // EXPLICIT, AND ONCE. The read is paid on the click that asks for it, and a
  // second click that folds the region back does not pay for it again.
  async function unfold(next: boolean) {
    setOpen(next);
    if (!next || replay.state !== "idle") return;
    setReplay({ state: "loading" });
    try {
      const response = await apiFetch(rowsPath(projectId, datastreamId, runId, issue.id));
      const body = (await response.json().catch(() => null)) as Record<string, unknown> | null;
      if (!response.ok) {
        setReplay({
          state: "failed",
          // THE SERVER'S OWN CODE AND SENTENCE. A screen that replaces them with
          // a friendly word makes a failure this build has never seen
          // undiagnosable.
          message:
            typeof body?.message === "string"
              ? body.message
              : `The rows could not be replayed (HTTP ${response.status}).`,
        });
        return;
      }
      setReplay({ state: "read", payload: body ?? {} });
    } catch {
      setReplay({ state: "failed", message: "The replay could not be reached." });
    }
  }

  async function download() {
    setFailure(null);
    try {
      const response = await apiFetch(
        `${rowsPath(projectId, datastreamId, runId, issue.id)}?format=csv`,
      );
      if (!response.ok) {
        const body = (await response.json().catch(() => null)) as { message?: string } | null;
        setFailure(body?.message ?? `The export was refused (HTTP ${response.status}).`);
        return;
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `anomaly_${issue.id}_rows.csv`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch {
      setFailure("The export could not be reached.");
    }
  }

  async function markReviewed() {
    setTransition("acknowledged");
    setFailure(null);
    try {
      const response = await apiFetch(
        `/api/projects/${encodeURIComponent(projectId)}` +
          `/datastreams/${encodeURIComponent(datastreamId)}` +
          `/workbench/anomalies/${encodeURIComponent(issue.id)}/transitions`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ event_kind: "acknowledged", reason: "reviewed from its run" }),
        },
      );
      const body = (await response.json().catch(() => null)) as
        | { status?: string; message?: string }
        | null;
      if (!response.ok) {
        setFailure(body?.message ?? `The acknowledgement was refused (HTTP ${response.status}).`);
        return;
      }
      // THE STATUS THE SERVER READ BACK, never a word composed here.
      if (typeof body?.status === "string") setStatus(body.status);
    } catch {
      setFailure("The acknowledgement could not be reached.");
    } finally {
      setTransition(null);
    }
  }

  const payload = replay.state === "read" ? replay.payload : null;
  const rows = payload && Array.isArray(payload.rows) ? (payload.rows as Record<string, unknown>[]) : null;
  const columns =
    payload && Array.isArray(payload.columns) ? (payload.columns as string[]) : [];

  return (
    <div
      className="rounded-large border border-divider-base p-4"
      data-testid="run-anomaly"
      data-issue-id={issue.id}
    >
      <div className="flex flex-wrap items-center gap-3">
        <Badge tone={stateTone(issue.severity)}>{stateLabel(issue.severity)}</Badge>
        {/* THE MONITOR REGISTRY IS THE ONLY AUTHORITY ON THIS WORD.
            `datastream_workbench._monitor_profiles` says so in as many words --
            "a monitor row that could not be read is NAMED, never filled with a
            plausible label" -- and serves `monitor_label: null` for it. Printing
            `dqm_<ULID>` here undid exactly that: the anomaly then claimed a
            monitor by an address nobody can look up, instead of saying the
            monitor behind it could not be read. */}
        <span className="text-ui text-text" data-testid="run-anomaly-monitor">
          {issue.monitor_label ?? "Monitor could not be read"}
        </span>
        <Badge tone="neutral" data-testid="run-anomaly-status">{status}</Badge>
      </div>
      <dl className="m-0 mt-2 grid grid-cols-3 gap-x-4 gap-y-1 max-lg:grid-cols-1">
        <div>
          <dt className="text-caption text-text-secondary">First seen</dt>
          <dd className="m-0 text-caption text-text">{dateTime(issue.first_seen_at)}</dd>
        </div>
        <div>
          <dt className="text-caption text-text-secondary">Last seen</dt>
          <dd className="m-0 text-caption text-text">{dateTime(issue.last_seen_at)}</dd>
        </div>
        <div>
          <dt className="text-caption text-text-secondary">Monitor</dt>
          <dd className="m-0"><ObjectId value={issue.monitor_id} title="DQ Monitor" /></dd>
        </div>
      </dl>

      {issue.replayable_rows ? (
        <Collapsible open={open} onOpenChange={(next) => void unfold(next)} className="mt-3">
          <CollapsibleTrigger
            className="rounded-pill border border-divider-base px-4 py-1.5 text-label font-label text-text"
            data-testid="run-anomaly-rows-trigger"
          >
            {open ? "Hide the rows" : "Show the rows as they are now"}
          </CollapsibleTrigger>
          <CollapsibleContent data-testid="run-anomaly-rows">
            <div className="mt-3 grid gap-2">
              <p className="m-0 text-caption text-text-secondary">{REPLAYED_NOW}</p>
              {replay.state === "loading" && (
                <p className="m-0 text-caption text-text-secondary" role="status">
                  Replaying this condition…
                </p>
              )}
              {replay.state === "failed" && (
                <Status
                  as="block"
                  tone="error"
                  title="The rows could not be read"
                  action={<Retry onClick={() => { setReplay({ state: "idle" }); void unfold(true); }} />}
                >
                  {replay.message}
                </Status>
              )}
              {payload && rows === null && (
                // A READING THAT NEVER RAN. `rows: null` is not `rows: []`, and
                // the two must not share a sentence.
                <Status as="block" tone="warning" title="No reading was taken">
                  {text(payload.message ?? payload.rows_absence_message, "")}
                </Status>
              )}
              {rows !== null && rows.length === 0 && (
                <p className="m-0 text-caption text-text-secondary" data-testid="run-anomaly-empty">
                  {text(payload?.note_message, "")}
                </p>
              )}
              {rows !== null && rows.length > 0 && (
                <>
                  <TableScroll label="Rows matching this condition now">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          {columns.map((column) => (
                            <TableHead key={column}>{column}</TableHead>
                          ))}
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {rows.map((row, index) => (
                          <TableRow key={index}>
                            {columns.map((column) => (
                              <TableCell key={column} className="font-mono text-caption">
                                {row[column] === null || row[column] === undefined
                                  ? "—"
                                  : String(row[column])}
                              </TableCell>
                            ))}
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </TableScroll>
                  {payload?.truncated === true && (
                    <p className="m-0 text-caption text-text-secondary">
                      {`This reading is bounded at ${text(payload.row_limit, "its limit")} rows; more match.`}
                    </p>
                  )}
                  <div>
                    <Button variant="secondary" size="sm" onClick={() => void download()}>
                      Download
                    </Button>
                  </div>
                </>
              )}
            </div>
          </CollapsibleContent>
        </Collapsible>
      ) : (
        // NO TABLE AND NO `Download`, because there is nothing either could
        // carry. The sentence is the profile's own -- arbitrage 8.
        <p
          className="m-0 mt-3 text-caption text-text-secondary"
          data-testid="run-anomaly-no-rows"
        >
          {issue.rows_absence_message ??
            "This monitor did not say whether it has faulty rows to show."}
        </p>
      )}

      {failure && (
        <div className="mt-3">
          <Status as="block" tone="error" title="Refused">
            {failure}
          </Status>
        </div>
      )}

      <div className="mt-3">
        <Button
          variant="secondary"
          size="sm"
          disabled={transition !== null}
          onClick={() => void markReviewed()}
          data-testid="run-anomaly-acknowledge"
        >
          {transition ? "Marking…" : "Mark as reviewed"}
        </Button>
      </div>
    </div>
  );
}

/**
 * The PLACE the anomaly unfolds in -- story 58.10, filled by 59.1.
 *
 * A run with no anomaly gets NO region: an empty panel is a promise of content
 * that does not exist. And the two silences are two sentences -- "no monitor has
 * been evaluated on this run" and "this run was checked and nothing was found"
 * are different facts, and `app.dq_evaluations` is what tells them apart
 * (migration 222 gave it the run).
 */
export default function RunAnomalies({
  run,
  projectId,
  datastreamId,
}: {
  run: Record<string, unknown>;
  projectId: string;
  datastreamId: string;
}) {
  const [open, setOpen] = useState(false);
  const counts = record(run.anomalies);
  const found = typeof counts?.anomalies === "number" ? counts.anomalies : 0;
  const evaluated = typeof counts?.evaluations === "number" ? counts.evaluations : 0;
  const issues = issuesOf(counts);

  if (found === 0) {
    return (
      <p className="m-0 text-caption text-text-secondary" data-testid="run-anomalies-none">
        {evaluated === 0 ? NO_MONITOR_EVALUATED : NO_ANOMALY_ON_RUN}
      </p>
    );
  }

  return (
    <Collapsible open={open} onOpenChange={setOpen} data-testid="run-anomalies">
      <CollapsibleTrigger
        className="rounded-pill border border-divider-base px-4 py-1.5 text-label font-label text-text"
        data-testid="run-anomalies-trigger"
      >
        {open ? "Hide" : "Show"} {formatNumber(found)} anomal
        {found === 1 ? "y" : "ies"} found on this run
      </CollapsibleTrigger>
      <CollapsibleContent data-testid="run-anomalies-content">
        <div className="mt-3 grid gap-2">
          <Status as="block" tone="warning" title={`${formatNumber(found)} to review`}>
            {`${formatNumber(evaluated)} evaluation(s) ran on this run.`}
          </Status>
          {issues.length === 0 ? (
            // COUNTED AND NOT DETAILED. Never a `0`, and never an empty table:
            // the count came from the same read as the detail, so a mismatch is
            // a fact about the payload and is said as one.
            <p className="m-0 text-caption text-text-secondary">
              This run carries anomalies whose detail did not travel with the payload.
            </p>
          ) : (
            issues.map((issue) => (
              <IssueBlock
                key={issue.id}
                issue={issue}
                runId={text(run.id, "")}
                projectId={projectId}
                datastreamId={datastreamId}
              />
            ))
          )}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}

/**
 * The evaluations that could name no run — ONCE, and outside the run blocks.
 *
 * Absent key means the count was not read, and that is NOT a zero: the region
 * simply does not appear rather than asserting a measurement nobody took.
 */
export function EvaluationsWithoutRun({ count }: { count: unknown }) {
  if (typeof count !== "number" || count <= 0) return null;
  return (
    <p
      className="m-0 mt-2 text-caption text-text-secondary"
      data-testid="evaluations-without-run"
    >
      {`${formatNumber(count)} evaluation(s) of this Datastream could not name a run. `}
      {EVALUATIONS_WITHOUT_RUN_ABSENCE}
    </p>
  );
}
