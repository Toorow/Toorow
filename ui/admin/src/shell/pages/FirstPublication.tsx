/**
 * FirstPublication — the first-publication lifecycle state of a Datastream.
 *
 * Visual lineage:
 *   _bmad-output/planning-artifacts/ux-designs/ux-connector-2026-07-23/
 *     mockups/first-publication.html
 *
 * The application shell (ApplicationShell.tsx) renders the frame, sidebar,
 * topbar, and <main className="main">. Like DatastreamOverview, this screen owns
 * an object header and a local-tabs bar above the page body, so it renders the
 * whole main content as a fragment inside the shell <main>.
 *
 * Two tracks:
 *   1. Publication readiness — the versioned evidence for the version consumers
 *      can use NOW.
 *   2. First pull — the named phase timeline, where recent value and historical
 *      coverage progress SEPARATELY.
 *
 * Data:
 *   GET /api/projects/{projectId}/datastreams/{datastreamId}/first-report/readiness
 *   (verified: registered in server/core/admin_api.py) returning the same
 *   FirstReportReadiness read-model RapportPretCard drives.
 *
 * WHAT WAS REMOVED
 * ----------------
 * This screen used to fall back to the mockup's literals for every value the
 * read-model does not carry, and — worse — for values it DOES carry when the API
 * was silent. A muted API produced "186,420 published rows", "22 minutes"
 * freshness, "8 / 8 checks passed", "mapping_v17", "plan_v12", "pub_01J4A8F2",
 * a "1–21 Jul 2026" published interval, a "31% complete" backfill bar, a fixed
 * five-step timeline with clock times (09:02, 09:06 …) and "Acme Ads" as the
 * authorized account. A publication that never happened read as a measured one.
 *
 * Now: a failed load says so and shows NOTHING else; a value the read-model does
 * not carry renders as "Not reported"; the timeline is built from the phases[]
 * the server actually returns; and the historical-backfill progress bar is gone
 * because no completion percentage exists to draw it from.
 */
import { useEffect, useRef, useState } from "react";
import type {
  FirstReportReadiness,
  PhaseStateValue,
  ReadinessPhase,
} from "../../datastreams/RapportPretCard";
import { apiFetch } from "../../lib/apiFetch";
import "../application.css";
import "./first-publication.css";

interface FirstPublicationProps {
  projectId?: string;
  datastreamId?: string;
  /** Wired by the shell when an MCP host connection flow exists. The button is
   *  rendered only when a handler is provided, and follows the server's
   *  host_cta — the UI never re-infers it. */
  onConnectHost?: () => void;
}

type FetchState =
  | { status: "loading" }
  | { status: "ok"; readiness: FirstReportReadiness }
  | { status: "stale"; readiness: FirstReportReadiness; message: string }
  | { status: "error"; message: string };

/** What a field renders when the read-model does not carry it. Never a
 *  plausible-looking number. */
const NOT_REPORTED = "Not reported";

/** Named phases, English. Unknown keys fall back to the raw phase name so a new
 *  server-side phase is surfaced rather than dropped. */
const PHASE_LABELS: Record<string, string> = {
  selection: "Report selection",
  recent_pull: "Recent extraction",
  verification: "Verification",
  publication: "Publish recent data",
  data_quality: "Data quality",
  history: "Historical backfill",
};

const PHASE_STATE_LABELS: Record<PhaseStateValue, string> = {
  waiting: "Waiting",
  running: "Running",
  succeeded: "Done",
  degraded: "Degraded",
  failed: "Failed",
  blocked: "Blocked",
};

const PHASE_STATES = new Set<PhaseStateValue>([
  "waiting",
  "running",
  "succeeded",
  "degraded",
  "failed",
  "blocked",
]);
const PHASE_NAMES = new Set(Object.keys(PHASE_LABELS));
const COVERAGE_STATES = new Set(["pending", "loading", "covered", "degraded", "failed"]);
const VERIFICATION_VERDICTS = new Set([
  "ok",
  "passed",
  "verified",
  "partial",
  "empty",
  "invalid",
  "failed",
]);
const POLL_INITIAL_MS = 4_000;
const POLL_MAX_MS = 30_000;
const POLL_MAX_ATTEMPTS = 20;

type JsonObject = Record<string, unknown>;

function invalidReadiness(detail: string): never {
  throw new Error(`invalid readiness response (${detail})`);
}

function asObject(value: unknown, field: string): JsonObject {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    invalidReadiness(`${field} must be an object`);
  }
  return value as JsonObject;
}

function asString(value: unknown, field: string): string;
function asString(value: unknown, field: string, nullable: true): string | null;
function asString(value: unknown, field: string, nullable = false): string | null {
  if (nullable && value === null) return null;
  if (typeof value !== "string" || value.length === 0) {
    invalidReadiness(`${field} must be a non-empty string`);
  }
  return value as string;
}

function asStringArray(value: unknown, field: string): string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string" || !item)) {
    invalidReadiness(`${field} must be an array of non-empty strings`);
  }
  return value as string[];
}

function asNonNegativeInteger(
  value: unknown,
  field: string,
  nullable = false,
): number | null {
  if (nullable && value === null) return null;
  if (typeof value !== "number" || !Number.isInteger(value) || value < 0) {
    invalidReadiness(`${field} must be a non-negative integer`);
  }
  return value as number;
}

function asNullableObject(value: unknown, field: string): JsonObject | null {
  return value === null ? null : asObject(value, field);
}

function assertNullableIso(value: unknown, field: string): void {
  const iso = asString(value, field, true);
  if (iso !== null && Number.isNaN(Date.parse(iso))) {
    invalidReadiness(`${field} must be an ISO timestamp`);
  }
}

function validateCoverage(value: unknown, field: string): void {
  const coverage = asNullableObject(value, field);
  if (!coverage) return;
  const state = asString(coverage.state, `${field}.state`);
  if (!COVERAGE_STATES.has(state)) invalidReadiness(`${field}.state is unsupported`);
}

function validateReadiness(
  value: unknown,
  expectedProjectId: string,
  expectedDatastreamId: string,
): FirstReportReadiness {
  const root = asObject(value, "readiness");
  if (root.schema_version !== "1") invalidReadiness("schema_version is unsupported");
  if (
    typeof root.readiness_version !== "string" ||
    !/^[a-f0-9]{32}$/i.test(root.readiness_version)
  ) {
    invalidReadiness("readiness_version must be a 32-character hex identifier");
  }
  if (root.project_id !== expectedProjectId || root.datastream_id !== expectedDatastreamId) {
    invalidReadiness("response scope does not match the requested Datastream");
  }

  const overallValues = new Set(["ready", "degraded", "blocked"]);
  const hostValues = new Set(["enabled", "degraded", "disabled"]);
  const overall = asString(root.overall, "overall");
  const hostCta = asString(root.host_cta, "host_cta");
  if (!overallValues.has(overall)) invalidReadiness("overall is unsupported");
  if (!hostValues.has(hostCta)) invalidReadiness("host_cta is unsupported");
  const expectedHost = {
    ready: "enabled",
    degraded: "degraded",
    blocked: "disabled",
  }[overall];
  if (hostCta !== expectedHost) invalidReadiness("overall and host_cta disagree");
  asString(root.headline, "headline");

  const publication = asNullableObject(root.current_publication, "current_publication");
  if (overall !== "blocked" && publication === null) {
    invalidReadiness(`${overall} requires a current_publication`);
  }
  if (publication) {
    asString(publication.execution_id, "current_publication.execution_id");
    asNonNegativeInteger(publication.row_count, "current_publication.row_count", true);
    assertNullableIso(publication.published_at, "current_publication.published_at");
  }

  const selected = asObject(root.selected_objects, "selected_objects");
  asString(selected.report_id, "selected_objects.report_id", true);
  asStringArray(selected.metrics, "selected_objects.metrics");
  asStringArray(selected.dimensions, "selected_objects.dimensions");
  asStringArray(selected.grain, "selected_objects.grain");
  asString(selected.timezone, "selected_objects.timezone", true);
  asString(selected.currency, "selected_objects.currency", true);

  validateCoverage(root.recent_coverage, "recent_coverage");
  validateCoverage(root.historical_coverage, "historical_coverage");

  const freshness = asNullableObject(root.freshness, "freshness");
  if (freshness) {
    assertNullableIso(freshness.last_pull_at, "freshness.last_pull_at");
    asString(freshness.connection_health, "freshness.connection_health", true);
  }

  const verification = asNullableObject(root.verification, "verification");
  if (verification) {
    const verdict = asString(verification.verdict, "verification.verdict", true);
    if (verdict !== null && !VERIFICATION_VERDICTS.has(verdict)) {
      invalidReadiness("verification.verdict is unsupported");
    }
    asNonNegativeInteger(verification.row_count, "verification.row_count", true);
  }

  const dq = asNullableObject(root.dq, "dq");
  if (dq) {
    asNonNegativeInteger(dq.total_unresolved, "dq.total_unresolved");
    if (typeof dq.monitors_unavailable !== "boolean" || typeof dq.degraded !== "boolean") {
      invalidReadiness("dq boolean flags are invalid");
    }
    if (dq.evaluated_days_30d !== undefined) {
      asNonNegativeInteger(dq.evaluated_days_30d, "dq.evaluated_days_30d");
    }
    if (dq.issue_count !== undefined) {
      asNonNegativeInteger(dq.issue_count, "dq.issue_count");
    }
  }

  const mapping = asNullableObject(root.mapping, "mapping");
  if (mapping) {
    asString(mapping.mapping_version_id, "mapping.mapping_version_id");
    asNonNegativeInteger(mapping.version_number, "mapping.version_number");
    asString(mapping.mapping_contract_version, "mapping.mapping_contract_version");
  }

  const provenance = asNullableObject(root.provenance, "provenance");
  if (provenance) {
    asString(provenance.content_hash, "provenance.content_hash", true);
    asString(provenance.source_schema_hash, "provenance.source_schema_hash", true);
  }

  if (!Array.isArray(root.exclusions)) invalidReadiness("exclusions must be an array");
  for (const [index, item] of root.exclusions.entries()) {
    const exclusion = asObject(item, `exclusions[${index}]`);
    asString(exclusion.kind, `exclusions[${index}].kind`);
    asString(exclusion.reason, `exclusions[${index}].reason`, true);
  }

  const lastPull = asNullableObject(root.last_successful_pull, "last_successful_pull");
  if (lastPull) {
    asString(lastPull.execution_id, "last_successful_pull.execution_id", true);
    asNonNegativeInteger(lastPull.row_count, "last_successful_pull.row_count", true);
    assertNullableIso(lastPull.at, "last_successful_pull.at");
  }

  if (!Array.isArray(root.phases)) invalidReadiness("phases must be an array");
  const seenPhases = new Set<string>();
  for (const [index, item] of root.phases.entries()) {
    const phase = asObject(item, `phases[${index}]`);
    const name = asString(phase.phase, `phases[${index}].phase`);
    if (!PHASE_NAMES.has(name) || seenPhases.has(name)) {
      invalidReadiness(`phases[${index}].phase is unsupported or duplicated`);
    }
    seenPhases.add(name);
    const phaseState = asString(phase.state, `phases[${index}].state`);
    if (!PHASE_STATES.has(phaseState as PhaseStateValue)) {
      invalidReadiness(`phases[${index}].state is unsupported`);
    }
    const interval = asNullableObject(phase.interval, `phases[${index}].interval`);
    if (interval) {
      if (interval.start !== undefined) assertNullableIso(interval.start, "phase.interval.start");
      if (interval.end_exclusive !== undefined) {
        assertNullableIso(interval.end_exclusive, "phase.interval.end_exclusive");
      }
    }
    asNonNegativeInteger(phase.rows, `phases[${index}].rows`, true);
    asNonNegativeInteger(phase.attempts, `phases[${index}].attempts`);
    asString(
      phase.live_publication_execution_id,
      `phases[${index}].live_publication_execution_id`,
      true,
    );
    const nextAction = asNullableObject(phase.next_action, `phases[${index}].next_action`);
    if (nextAction) {
      asString(nextAction.label, `phases[${index}].next_action.label`);
      asString(nextAction.owner, `phases[${index}].next_action.owner`);
    }
  }

  return root as unknown as FirstReportReadiness;
}

function isTerminal(readiness: FirstReportReadiness): boolean {
  if (readiness.overall === "ready") return true;
  return !readiness.phases.some(
    (phase) => phase.state === "waiting" || phase.state === "running",
  );
}

async function fetchReadiness(
  projectId: string,
  datastreamId: string,
  signal: AbortSignal,
): Promise<FirstReportReadiness> {
  const resp = await apiFetch(
    `/api/projects/${encodeURIComponent(projectId)}/datastreams/${encodeURIComponent(datastreamId)}/first-report/readiness`,
    { method: "GET", cache: "no-store", signal },
  );
  if (!resp.ok) {
    throw new Error(
      resp.status === 404
        ? "this Datastream is unknown, or you do not have access to it"
        : `HTTP ${resp.status}`,
    );
  }
  return validateReadiness(await resp.json(), projectId, datastreamId);
}

/** e.g. 186420 -> "186,420" (tabular); null when the API is silent. */
function groupNum(n: number | null | undefined): string | null {
  if (n == null) return null;
  return n.toLocaleString("en-US");
}

/** Freshness age from an ISO instant, e.g. "22 minutes" / "3 hours". */
function freshnessAge(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return null;
  const mins = Math.max(0, Math.round((Date.now() - then) / 60_000));
  if (mins < 60) return `${mins} minute${mins === 1 ? "" : "s"}`;
  const hours = Math.round(mins / 60);
  if (hours < 48) return `${hours} hour${hours === 1 ? "" : "s"}`;
  return `${Math.round(hours / 24)} days`;
}

/** Truncate a long identifier so the mono chip stays compact. */
function shortId(id: string | null | undefined): string | null {
  if (!id) return null;
  return id.length > 14 ? id.slice(0, 14) : id;
}

/** A phase interval -> a compact label, e.g. "1–21 Jul 2026"; null when absent.
 *  end_exclusive is stepped back one day so the label reads as a closed range. */
function intervalLabel(
  interval: { start?: string; end_exclusive?: string } | null | undefined,
): string | null {
  if (!interval?.start) return null;
  const start = new Date(interval.start);
  if (Number.isNaN(start.getTime())) return null;
  const dayFmt = new Intl.DateTimeFormat("en-GB", { day: "numeric", timeZone: "UTC" });
  const fullFmt = new Intl.DateTimeFormat("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
  if (!interval.end_exclusive) return fullFmt.format(start);
  const endExcl = new Date(interval.end_exclusive);
  if (Number.isNaN(endExcl.getTime())) return fullFmt.format(start);
  const endIncl = new Date(endExcl.getTime() - 86_400_000);
  const sameMonth =
    start.getUTCFullYear() === endIncl.getUTCFullYear() &&
    start.getUTCMonth() === endIncl.getUTCMonth();
  return sameMonth
    ? `${dayFmt.format(start)}-${fullFmt.format(endIncl)}`
    : `${fullFmt.format(start)} - ${fullFmt.format(endIncl)}`;
}

/** One readiness cell. `value === null` means the read-model did not carry it. */
function ReadinessItem({
  label,
  value,
  note,
  mono,
}: {
  label: string;
  value: string | null;
  note?: string | null;
  mono?: boolean;
}) {
  const reported = value != null;
  return (
    <div className="readiness-item">
      <span>{label}</span>
      <strong className={`${mono && reported ? "mono " : ""}${reported ? "" : "fp-unreported"}`}>
        {reported ? value : NOT_REPORTED}
      </strong>
      {reported && note ? <small>{note}</small> : null}
    </div>
  );
}

function phaseDetail(phase: ReadinessPhase): string | null {
  const parts: string[] = [];
  const interval = intervalLabel(phase.interval);
  if (interval) parts.push(interval);
  const rows = groupNum(phase.rows);
  if (rows) parts.push(`${rows} rows`);
  if (phase.attempts > 1) parts.push(`${phase.attempts} attempts`);
  if (phase.next_action?.label) parts.push(`Next: ${phase.next_action.label}`);
  return parts.length > 0 ? parts.join(" / ") : null;
}

export default function FirstPublication({
  projectId,
  datastreamId,
  onConnectHost,
}: FirstPublicationProps) {
  const [state, setState] = useState<FetchState>({ status: "loading" });
  const [retryKey, setRetryKey] = useState(0);
  const generationRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    const generation = ++generationRef.current;
    let disposed = false;
    let attempts = 0;
    let delayMs = POLL_INITIAL_MS;
    let lastGood: FirstReportReadiness | null = null;

    if (timerRef.current !== null) clearTimeout(timerRef.current);
    abortRef.current?.abort();
    setState({ status: "loading" });

    const scheduleNext = () => {
      if (disposed || generation !== generationRef.current || attempts >= POLL_MAX_ATTEMPTS) {
        return;
      }
      timerRef.current = setTimeout(() => void poll(), delayMs);
      delayMs = Math.min(delayMs * 2, POLL_MAX_MS);
    };

    async function poll(): Promise<void> {
      if (
        disposed ||
        generation !== generationRef.current ||
        attempts >= POLL_MAX_ATTEMPTS ||
        !projectId ||
        !datastreamId
      ) {
        return;
      }
      attempts += 1;
      const controller = new AbortController();
      abortRef.current = controller;
      try {
        const readiness = await fetchReadiness(projectId, datastreamId, controller.signal);
        if (disposed || controller.signal.aborted || generation !== generationRef.current) return;
        lastGood = readiness;
        setState({ status: "ok", readiness });
        if (!isTerminal(readiness)) scheduleNext();
      } catch (err) {
        if (disposed || controller.signal.aborted || generation !== generationRef.current) return;
        const message = err instanceof Error ? err.message : String(err);
        setState(
          lastGood
            ? { status: "stale", readiness: lastGood, message }
            : { status: "error", message },
        );
        scheduleNext();
      } finally {
        if (abortRef.current === controller) abortRef.current = null;
      }
    }

    if (!projectId || !datastreamId) {
      setState({ status: "error", message: "no Datastream is scoped" });
    } else {
      void poll();
    }

    return () => {
      disposed = true;
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      abortRef.current?.abort();
      abortRef.current = null;
    };
  }, [projectId, datastreamId, retryKey]);

  const r = state.status === "ok" || state.status === "stale" ? state.readiness : null;
  const pub = r?.current_publication ?? null;

  // Values come from the validated read-model and are publication evidence only
  // when the server provides a current publication pointer.
  const publishedRows = pub ? groupNum(pub.row_count) : null;
  const freshness = pub
    ? freshnessAge(r?.freshness?.last_pull_at) ?? freshnessAge(pub.published_at)
    : null;
  const mappingLabel =
    pub && r?.mapping?.version_number != null ? `mapping_v${r.mapping.version_number}` : null;
  const grain = pub && r?.selected_objects.grain.length ? r.selected_objects.grain.join(" ") : null;
  const currency = pub ? (r?.selected_objects.currency ?? null) : null;
  const meaningSub = [grain, currency].filter(Boolean).join(" / ") || null;

  const dqUnresolved = pub ? (r?.dq?.total_unresolved ?? null) : null;
  const dqMonitorsUnavailable = pub ? (r?.dq?.monitors_unavailable ?? false) : false;
  const dqNotEvaluated = pub ? r?.dq?.evaluated_days_30d === 0 : false;
  const dqLabel = dqMonitorsUnavailable
    ? "Monitors unavailable"
    : dqNotEvaluated
      ? "Not evaluated"
      : dqUnresolved == null
        ? null
        : dqUnresolved === 0
          ? "No unresolved findings"
          : `${dqUnresolved} unresolved`;
  const dqSub = dqMonitorsUnavailable
    ? "Quality monitors were unavailable for this publication"
    : dqNotEvaluated
      ? "No quality-evaluation day was recorded in the last 30 days"
      : dqUnresolved != null && dqUnresolved > 0
        ? "Review before relying on this publication"
        : null;

  const exclusion = pub ? (r?.exclusions[0] ?? null) : null;
  const knownLimitation = exclusion?.kind ?? null;
  const knownLimitationReason = exclusion?.reason ?? "Declared by the source";
  const publicationId = shortId(pub?.execution_id);

  const phases = r?.phases ?? [];
  const recentPhase = phases.find((phase) => phase.phase === "recent_pull") ?? null;
  const publishedInterval =
    pub && recentPhase?.live_publication_execution_id === pub.execution_id
      ? intervalLabel(recentPhase.interval)
      : null;

  const headline =
    r == null || pub == null
      ? r == null
        ? null
        : "Not published"
      : r.overall === "ready"
        ? "Published"
        : r.overall === "degraded"
          ? "Published - degraded"
          : "Published - blocked";
  return (
    <>
      {/* Object header — the datastream identity. The readiness read-model does
          not carry a display name or a provider, so the identifier is shown as
          the identifier it is, rather than a fabricated account name and logo. */}
      <div className="topbar-left ds-object-header">
        <div className="object">
          <div>
            <strong className="mono">{datastreamId ?? "No Datastream scoped"}</strong>
            <span>First publication</span>
          </div>
        </div>
      </div>

      {/* Local tabs — only the sections that have a target. */}
      <nav className="local-tabs" aria-label="Datastream">
        <a className="local-tab active" href="#overview" aria-current="page">
          Overview
        </a>
        <a className="local-tab" href="#data">
          Data
        </a>
        <a className="local-tab" href="#mapping">
          Mapping
        </a>
        <a className="local-tab" href="#runs">
          Runs
        </a>
      </nav>

      <div className="first-publication full-main">
        <div className="page-header">
          <div>
            <h1>First publication</h1>
            <p>
              Recent publication and historical backfill are reported as separate tracks.
            </p>
          </div>
          <div className="header-actions">
            {onConnectHost && (
              <>
                <button
                  className="secondary-button"
                  type="button"
                  disabled={r == null || r.host_cta === "disabled"}
                  onClick={() => onConnectHost()}
                >
                  Connect MCP host
                </button>
                {r?.host_cta === "degraded" && (
                  <small role="status">
                    The server reports degraded readiness; review the limitations before connecting.
                  </small>
                )}
              </>
            )}
            {pub && (
              <a className="primary-button action-link primary" href="#data">
                View published data
              </a>
            )}
          </div>
        </div>

        {state.status === "loading" && (
          <p className="fp-status" role="status">
            Loading the publication readiness...
          </p>
        )}

        {state.status === "error" && (
          <div className="fp-load-error" role="alert">
            <span className="signal-label error">
              <span className="signal-mark" />
              Could not load the publication readiness
            </span>
            <p>
              {state.message}. No publication evidence is being reported below - this is a
              loading failure, not a published result.
            </p>
            <button
              className="secondary-button"
              type="button"
              onClick={() => setRetryKey((value) => value + 1)}
            >
              Retry
            </button>
          </div>
        )}

        {state.status === "stale" && (
          <div className="fp-load-error" role="status">
            <span className="signal-label warning">
              <span className="signal-mark" />
              Readiness refresh failed
            </span>
            <p>
              {state.message}. Showing the last validated response for this Datastream; it may
              be stale.
            </p>
          </div>
        )}
        {r && (
          <>
            <section className="publication-grid">
              {/* Track 1 — Publication readiness. */}
              <article className="panel readiness-panel">
                <div className="readiness-head">
                  <div>
                    <h2>Publication readiness</h2>
                    <p>Validated evidence for the current server-recorded publication.</p>
                  </div>
                  <div className="readiness-score">
                    <strong>{headline}</strong>
                    <span>{r.headline}</span>
                  </div>
                </div>
                <div className="readiness-grid">
                  <ReadinessItem
                    label="Published interval"
                    value={publishedInterval}
                    note="Recent-first publication"
                  />
                  <ReadinessItem label="Freshness" value={freshness} note="Since the last pull" />
                  <ReadinessItem label="Published rows" value={publishedRows} />
                  <ReadinessItem label="Data quality" value={dqLabel} note={dqSub} />
                  <ReadinessItem label="Meaning" value={mappingLabel} note={meaningSub} mono />
                  <ReadinessItem
                    label="Known limitation"
                    value={knownLimitation}
                    note={knownLimitationReason}
                  />
                </div>
                <div className="readiness-foot">
                  <span>
                    {pub ? (
                      r.provenance?.content_hash ? (
                        <>
                          Content hash <code>{shortId(r.provenance.content_hash)}</code>
                        </>
                      ) : (
                        "No content hash recorded for the current publication."
                      )
                    ) : (
                      "No current publication is recorded."
                    )}
                  </span>
                  {publicationId ? <code>{publicationId}</code> : null}
                </div>
              </article>

              {/* Track 2 — the named phase timeline, straight from phases[]. */}
              <article className="panel phase-panel">
                <h2>First pull</h2>
                <p>Recent value and historical coverage progress separately.</p>
                {phases.length === 0 ? (
                  <p className="fp-status">No phase has been reported for this Datastream yet.</p>
                ) : (
                  <ol className="phase-list">
                    {phases.map((phase) => {
                      const detail = phaseDetail(phase);
                      const running = phase.state === "running" || phase.state === "waiting";
                      return (
                        <li
                          key={phase.phase}
                          className={`phase${running ? " running" : ""}`}
                          data-state={phase.state}
                        >
                          <span className="phase-mark" />
                          <div className="phase-copy">
                            <strong>{PHASE_LABELS[phase.phase] ?? phase.phase}</strong>
                            {detail ? <small>{detail}</small> : null}
                          </div>
                          <span className="phase-time">
                            {PHASE_STATE_LABELS[phase.state] ?? phase.state}
                          </span>
                        </li>
                      );
                    })}
                  </ol>
                )}
              </article>
            </section>

            {pub && (
              <div className="degraded-note">
                <span className="signal-label warning">
                  <span className="signal-mark" />
                </span>
                <div>
                  <strong>Last-known-good evidence</strong>
                  Current publication: <code>{publicationId}</code>. Later extraction and
                  historical-backfill states are reported separately in the phase timeline.
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </>
  );
}