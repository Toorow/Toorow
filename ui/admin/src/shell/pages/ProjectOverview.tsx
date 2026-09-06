import { useEffect, useState } from "react";
import { ApiError, apiGet } from "../../lib/apiFetch";
import { wireWord,
  Button, Cluster, EmptyState, Failure, formatCount, formatDate, formatNumber, formatTimestamp,
  Loading, Metric, NO_VALUE, ObjectId,
  PageFrame, PageHeader, Panel, PanelHeader, ProjectNotFound, Retry, Stack, Status, StatusLegend,
  stateLabel, stateTone, Timestamp, type StatusLegendEntry, type Tone,
} from "../../ui";
import {
  declaredConfidenceReading,
  derivedConfidenceReading,
  ModelAuthored,
  type InsightAuthorship,
} from "../../daily-insights/InsightProvenance";

export interface OwnerReference {
  surface: "project" | "global";
  workspace: string | null;
  section: string | null;
  global_surface: string | null;
  global_section: string | null;
  /** The collection lens inside the section, when the reference names one.
   *
   *  Story 58.9: a reference that stopped at `controls-quality` landed on that
   *  section's declared default lens, `conflicts` — a different collection from
   *  the one the caller asked for. Optional: every server-composed reference
   *  predates it and carries none. */
  lens?: string | null;
  object_type: string | null;
  object_id: string | null;
  tab: string | null;
  action: string | null;
  version_id: string | null;
  evidence_id: string | null;
}

/** The contract's vocabulary, in full. Missing, failed or unauthorized evidence
 *  reads as unknown / unavailable / permission limited / not applicable -- never
 *  as healthy, and never as a bare "0/0". */
type PostureState =
  | "ready" | "degraded" | "blocked" | "unknown"
  | "unavailable" | "permission_limited" | "not_applicable" | "empty";
interface PostureDimension { state: PostureState; explanation: string; evidence_horizon: string | null; owner: OwnerReference }
interface ProjectOverviewEnvelope {
  schema_version: "project-overview.v1";
  project: { id: string; name: string; organization: { id: string; name: string } | null; business_domains: Array<{ id: string; name: string }>; active_configuration_version_id: string | null; as_of: string };
  posture: { operational_health: PostureDimension; trust_readiness: PostureDimension; business_signals: PostureDimension; limiting_dimension: string | null };
  next_action: { label: string; permitted: boolean; handoff: string | null; cause: string; owner: OwnerReference } | null;
  attention: { items: Array<{ id: string; cause: string; impact: string[]; scope: string[]; status: PostureState; first_observed_at: string | null; last_observed_at: string | null; evidence_horizon: string | null; owner: OwnerReference; action: { label: string; permitted: boolean } }>; total: number; has_more: boolean };
  coverage: Array<{ kind: string; key: string; state: PostureState; status?: string; denominator: number; complete: number; gaps: string[]; evidence_horizon: string | null; owner: OwnerReference; active: { state: string; version_id: string | null } | null; pending: { state: string; version_id: string | null } | null }>;
  outcomes: { status: string; items: OutcomeItem[]; insight_silence?: InsightSilence | null; insight_retractions?: InsightRetraction[]; alert_window_hours?: number };
  changes: { status: string; items: ChangeItem[] };
  /** The setup posture `overview.md:140` asks Overview to summarize. The server
   *  composed it on every request and NO screen declared it, so it was thrown
   *  away; `components` names the order, so a sixth one needs no edit here. */
  readiness?: { version: string; components: string[] } & Record<string, ReadinessComponent | string | string[]>;
}
interface ReadinessComponent { state: PostureState; owner: OwnerReference; evidence_ref: string | null }
/** Why the last daily-insight run published nothing, when it disclosed a reason.
 *  `proactive-assertions.md`: silence is a state and it is disclosed. An empty panel
 *  that only says "nothing here" reads as a quiet Project, and one of its sources may
 *  in fact have been unable to look. */
interface InsightSilence {
  /** `blocked` | `no_insight` | `failed` — a run that recorded something — plus
   *  `never_ran`, the ABSENT row, and `retracted`, a day that spoke and unsaid it.
   *  `execution-substrate.md` forbids collapsing any pair of them. */
  state: string;
  insight_date: string | null;
  explanation: string;
  reason: string | null;
  /** Only the absent run carries one: it is the single silence here a person can
   *  act on, and the act is in their own LLM host, never in a deployment. */
  gesture?: string | null;
}
/** One published claim its author withdrew (migration 321). It leaves the signal
 *  list — Overview volunteers what it lists — and arrives here instead, because
 *  dropping it silently would be the delete `proactive-assertions.md` refuses. */
interface InsightRetraction {
  id: string;
  title: string;
  insight_date: string | null;
  retracted_at: string | null;
  retracted_by: string | null;
  reason: string | null;
}
interface OutcomeItem {
  id?: string;
  kind?: string;
  period?: string | { start?: string; end?: string } | null;
  freshness?: string | null;
  limitations?: string[];
  provenance?: { kind?: string; id?: string } | null;
  /** The word the MODEL declared. Read only as the model's own estimate, next to
   *  `authorship`; the reading a person sees comes from `authorship.derivedConfidence`. */
  confidence?: string;
  authorship?: InsightAuthorship | null;
  owner?: OwnerReference;
  [key: string]: unknown;
}
interface ChangeItem {
  id?: string;
  kind?: string;
  state?: string;
  summary?: string;
  occurred_at?: string | null;
  version_id?: string | null;
  owner?: OwnerReference;
  [key: string]: unknown;
}
type LoadState = { kind: "loading"; projectId: string } | { kind: "ready"; projectId: string; data: ProjectOverviewEnvelope } | { kind: "denied"; projectId: string } | { kind: "error"; projectId: string };
const POSTURE_LABELS = { operational_health: "Operational health", trust_readiness: "Trust & readiness", business_signals: "Business signals" } as const;

/**
 * THE POSTURE MARK, FROM THE UNION (76-2).
 *
 * It was four `as const` returns, and one of them was the fifth disagreement in
 * the console about `blocked`: red here, amber in the five Data collections, red
 * again in Governance until 76-2 withdrew that override. The arbitration holds
 * everywhere -- a blocked signal is repairable and the owning route is named
 * right beside it, which is exactly what `warning` promises. `ready`,
 * `degraded` and every posture word the server can send are declared; a word
 * declared nowhere is `Unknown` in the warning colour, which for a posture
 * signal is the honest reading.
 */
function tone(state: PostureState): Tone {
  return stateTone(state);
}

const POSTURE_MEANING: Record<Tone, { label: string; meaning: string }> = {
  success: { label: "Ready", meaning: "measured, and inside its threshold" },
  warning: { label: "Needs attention", meaning: "degraded or blocked — the owning route beside it can repair it" },
  error: { label: "Refused", meaning: "nothing downstream can proceed until this is cleared" },
  neutral: { label: "Not measured", meaning: "the server stated an absence, or the reading does not apply here" },
  info: { label: "Not offered here", meaning: "this deployment does not carry the signal" },
};

/** Only the marks this project is drawing: the attention items and the
 *  coverage cards are the two places a posture tone reaches the page. */
function postureLegend(data: ProjectOverviewEnvelope): StatusLegendEntry[] {
  const shown = new Set<Tone>();
  for (const item of data.attention?.items ?? []) shown.add(tone(item.status));
  for (const item of data.coverage ?? []) shown.add(tone(item.state));
  return [...shown].map((mark) => ({ tone: mark, ...POSTURE_MEANING[mark] }));
}

/** What a coverage row REPORTS, as the VALUE of a KPI (76-5).
 *
 *  Zero applicable objects is "Not applicable", never "0/0" read as a complete
 *  denominator -- and never 100%. The two readings a zero denominator can have
 *  are spelled out rather than mapped: the server either could not read the
 *  projection (`status: "unavailable"`, `project_overview.py:236,292,325`) or
 *  read it and found nothing applicable. Both words come from the declared
 *  vocabulary, so this screen holds no private state map any more -- the
 *  `STATE_LABELS` it did hold answered `Not applicable` for FIVE different
 *  posture words, which is a second vocabulary nobody could see.
 *
 *  When there IS a denominator the value is the numerator alone and the
 *  population moves into the hint: that is the whole of this story's title.
 *  "3" over "of 12 applicable" is the same fact as "3/12" and says which
 *  number is the population. */
function coverageReading(item: { state: PostureState; status?: string; denominator: number; complete: number }): string {
  if (item.denominator === 0) {
    const read = item.status ?? item.state;
    return stateLabel(read === "unavailable" ? "unavailable" : "not_applicable");
  }
  return formatNumber(item.complete);
}

/** The confidence a reader sees is the SERVER's, or it is `unmeasurable`.
 *
 *  `overview.md:277`: a surfaced signal must not state a confidence level that no
 *  server evidence backs. Story 53.4 made the defect visible — the word came from
 *  the model that also wrote the prose, and this function said so out loud. Saying
 *  it was the honest half; `proactive-assertions.md` ("Incomplete if": *a
 *  confidence level is declared by the author of the claim rather than derived*)
 *  asks for the other. `core.insight_confidence` now measures it from the rows
 *  carrying the members the insight CITED, over the insight's own period, and the
 *  block travels on `authorship.derivedConfidence`.
 *
 *  The model's word is not promoted into a gap: an insight with no derivation
 *  reads `unmeasurable`, never a level, and the declared word is reported on its
 *  own line as the model's estimate (`declaredConfidenceReading`). */
function confidenceReading(item: OutcomeItem): string | null {
  return derivedConfidenceReading(item.authorship, item.confidence ?? null);
}

function periodText(period: OutcomeItem["period"]): string | null {
  if (!period) return null;
  if (typeof period === "string") return period;
  if (period.start || period.end) return `${period.start ?? "?"} to ${period.end ?? "?"}`;
  return null;
}

/** The empty outcomes panel says WHY it is empty when a source disclosed a reason.
 *  This sentence used to be a hardcoded twin of the server's posture explanation, so
 *  the disclosure landed in one of the two places that assert the same thing. */
function emptyOutcomesDescription(silence: InsightSilence | null | undefined): string {
  const base = "No persisted business signal is available for this project.";
  return silence?.explanation ? `${base} ${silence.explanation}` : base;
}

/** THE FIFTH STATE GETS ITS OWN SENTENCE, not a suffix on the fourth.
 *
 *  `execution-substrate.md` `Incomplete if` 11: the silence of a unit scheduled in
 *  someone else's host must not read as a run that happened. "No recent outcomes"
 *  followed by a reason is the shape for a run that REPORTED something; an absent
 *  run reported nothing, was never installed, and is the one case on this panel a
 *  person can repair — so it is titled after the absence and described by the
 *  gesture. Nothing here infers that a run happened, and nothing back-fills a day. */
function outcomesEmptyState(silence: InsightSilence | null | undefined): { title: string; description: string } {
  if (silence?.state === "never_ran") {
    return {
      title: "The daily insight task has never run",
      description: silence.explanation,
    };
  }
  return { title: "No recent outcomes", description: emptyOutcomesDescription(silence) };
}

/** The withdrawn claims of the latest run, shown as withdrawn.
 *
 *  Separate from the signal list on purpose: a retracted assertion put back among
 *  the assertions with a label is an assertion a hurried reader still reads. */
function RetractedInsights({ retractions }: { retractions: InsightRetraction[] }) {
  return <div className="border-t border-divider-base p-5" data-testid="insight-retractions">
    <Status as="block" tone="warning" title={retractions.length > 1 ? `${retractions.length} withdrawn insights` : "Withdrawn insight"}>
      <ul className="m-0 grid list-none gap-2 p-0">{retractions.map((item) => <li key={item.id}>
        <strong className="block text-ui text-text">{item.title}</strong>
        <span className="text-caption text-text-secondary">
          Withdrawn by {item.retracted_by ?? "someone in this project"}
          {item.retracted_at ? ` on ${item.retracted_at.slice(0, 10)}` : ""}
          {item.reason ? `: ${item.reason}` : "."}
        </span>
      </li>)}</ul>
      <p className="mt-2 mb-0 text-caption text-text-secondary">
        A withdrawn insight is kept and is no longer counted as a business signal. It
        cannot be reinstated; a different reading is published as a new insight.
      </p>
    </Status>
  </div>;
}

function isOwner(value: unknown): value is OwnerReference {
  if (!value || typeof value !== "object") return false;
  const owner = value as Record<string, unknown>;
  return (owner.surface === "project" || owner.surface === "global")
    && (owner.workspace === null || typeof owner.workspace === "string")
    && (owner.section === null || typeof owner.section === "string")
    && (owner.global_surface === null || typeof owner.global_surface === "string")
    && (owner.global_section === null || typeof owner.global_section === "string")
    && (owner.object_type === null || typeof owner.object_type === "string")
    && (owner.object_id === null || typeof owner.object_id === "string")
    && (owner.tab === null || typeof owner.tab === "string")
    && (owner.action === null || typeof owner.action === "string")
    && (owner.version_id === null || typeof owner.version_id === "string")
    && (owner.evidence_id === null || typeof owner.evidence_id === "string");
}

function isEnvelope(value: unknown): value is ProjectOverviewEnvelope {
  if (!value || typeof value !== "object") return false;
  const envelope = value as Record<string, unknown>;
  const project = envelope.project as Record<string, unknown> | undefined;
  const posture = envelope.posture as Record<string, unknown> | undefined;
  const next = envelope.next_action as Record<string, unknown> | null | undefined;
  return envelope.schema_version === "project-overview.v1" && typeof project?.id === "string" && typeof project?.name === "string"
    && Boolean(posture?.operational_health) && Boolean(posture?.trust_readiness) && Boolean(posture?.business_signals)
    && (next == null || isOwner(next.owner));
}

/** The window a bounded zone was read over, in the reader's units.
 *  `overview.md:123` -- "Every count names its denominator and window". The
 *  governed-alert window lived only in a server comment that CLAIMED it was
 *  stated; an empty list over seven days and one over seven minutes read alike.
 *
 *  `day(s)` was this screen's own plural. `formatCount` agrees the noun with the
 *  number and is the one place that decides (console-presentation.md §2). */
function windowText(hours: number | undefined): string | null {
  if (!hours || hours <= 0) return null;
  return hours % 24 === 0 ? `last ${formatCount(hours / 24, "day")}` : `last ${formatCount(hours, "hour")}`;
}

/** THE POPULATION AND THE CUT-OFF OF ONE COVERAGE KPI (76-5, arbitrages 1 and 3).
 *
 *  « what · window · cut-off », the sentence pattern Controls & Quality's monitor
 *  description was the console's single instance of. `what` is the tile's own
 *  label; this is the other two halves.
 *
 *  THE NOUN IS NOT COMPOSED HERE, and that is a server boundary rather than a
 *  choice. `project_overview.py` sends `denominator` and `complete` per coverage
 *  row and NO word for what is being counted -- Datastreams for `data:publication`
 *  (`:798`), active context objects for `context` (`:312`), evaluation-run cases
 *  for `test` (`:345`), unresolved quality issues for `governance` (`:259`).
 *  Composing that noun in the browser would make the console a second authority
 *  on the vocabulary, which is the defect `ScreensDoNotPrintIdentifiersAsProse`
 *  refuses in the same words. `applicable` is the server's own word
 *  (`_capability_coverage`, `coverage.applicable`) and is true of every row. The
 *  field that would carry the noun is named in the story record. */
function coverageHint(item: { denominator: number; evidence_horizon: string | null }): string {
  // THE DATE GRAIN, not the instant. `evidence_horizon` is `complete_through`
  // or a run date; rendering it through `<Timestamp>` would print a midnight
  // with a timezone and claim a precision the server never sent.
  const cutoff = item.evidence_horizon
    ? `evidence to ${formatDate(item.evidence_horizon)}`
    : "no evidence horizon recorded";
  return item.denominator === 0
    ? `Nothing applicable to measure · ${cutoff}`
    : `of ${formatNumber(item.denominator)} applicable · ${cutoff}`;
}

/** One component of the shared readiness projection, or null when the envelope
 *  carries a key that is not a component object. */
function readinessComponent(readiness: ProjectOverviewEnvelope["readiness"], key: string): ReadinessComponent | null {
  const value = readiness?.[key];
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const component = value as ReadinessComponent;
  return isOwner(component.owner) ? component : null;
}
/** The keys a signal's headline may come from, in order of preference. */
const LABEL_KEYS = ["label", "name", "title", "summary", "description"] as const;

/** Which key the headline was taken from, or null when nothing matched.
 *
 *  It exists because the headline of a daily insight is MODEL PROSE — `title`,
 *  or `summary` when there is no title — and until the marker below, this
 *  function drew it in exactly the typography a server-composed label gets.
 *  `proactive-assertions.md` ("Incomplete if"): *model-authored prose is not
 *  visibly distinguishable from cited server data*. The card cannot mark what it
 *  cannot name, so the key travels beside the text. */
function itemLabelKey(item: Record<string, unknown>): string | null {
  return LABEL_KEYS.find((key) => typeof item[key] === "string") ?? null;
}

/** The payload path `authorship.modelAuthored` would name for that key, or `""`.
 *
 *  `""` for a key the block never names (`label`, `name`, `description`): a
 *  server-composed label must not be markable by accident, and the server's list
 *  stays the only authority on what the model wrote. */
function itemLabelField(item: Record<string, unknown>): string {
  const key = itemLabelKey(item);
  return key === "title" || key === "summary" ? `insight.${key}` : "";
}

function itemLabel(item: Record<string, unknown>, fallback: string) {
  const key = itemLabelKey(item);
  return key ? (item[key] as string) : fallback;
}
function OwnerButton({ owner, label, onOpenOwner, variant = "secondary" }: { owner: OwnerReference; label: string; onOpenOwner?: (owner: OwnerReference) => void; variant?: "default" | "secondary" | "ghost" }) {
  return onOpenOwner ? <Button variant={variant} onClick={() => onOpenOwner(owner)}>{label}</Button> : null;
}
function PostureCard({ label, dimension, onOpenOwner }: { label: string; dimension: PostureDimension; onOpenOwner?: (owner: OwnerReference) => void }) {
  return <Status as="block" tone={tone(dimension.state)} title={label} action={<OwnerButton owner={dimension.owner} label="Open owner" onOpenOwner={onOpenOwner} variant="ghost" />}>
    <p className="m-0">{dimension.explanation}</p>
    <p className="mt-1 mb-0 text-caption text-text-secondary">Evidence horizon: {formatDate(dimension.evidence_horizon)}</p>
  </Status>;
}

function ProjectOverviewReady({ data, onOpenOwner }: { data: ProjectOverviewEnvelope; onOpenOwner?: (owner: OwnerReference) => void }) {
  const postureEntries = Object.entries(POSTURE_LABELS) as Array<[keyof typeof POSTURE_LABELS, string]>;
  const alertWindow = windowText(data.outcomes.alert_window_hours);
  const readinessRows = (data.readiness?.components ?? [])
    .map((key) => ({ key, component: readinessComponent(data.readiness, key) }))
    .filter((row): row is { key: string; component: ReadinessComponent } => row.component !== null);
  // `PageFrame`, not a second `<main>`: `ApplicationShell` already opens the one
  // this page renders inside, two of them make the landmark ambiguous, and the
  // `p-6 lg:p-8` it carried stacked a second gutter on the shell's — 64px here
  // against 32px on Project settings, on two screens one click apart.
  return <PageFrame>
    <PageHeader
      eyebrow={data.project.organization?.name ?? "Project"}
      title={data.project.name}
      description={<>Current project posture as of <Timestamp value={data.project.as_of} />. Each signal keeps its evidence horizon and owning route.</>}
      /* The posture marks THIS project is actually showing (§3), derived from
         the signals below rather than declared: an Overview whose every signal
         is ready must not explain what a refusal looks like. */
      legend={<StatusLegend label="What a posture mark means" entries={postureLegend(data)} />}
    />
    <Stack>
      <Panel flush><PanelHeader title="Project posture" description="Operational health, trust readiness, and business signals remain separate." />
        <dl className="m-0 grid gap-4 border-b border-divider-base p-5 text-ui sm:grid-cols-2 xl:grid-cols-4">
          {/* NOT KPIs, and deliberately not `Metric`: these four are the project's
              IDENTITY, and `Metric`'s own contract is "a single number with its
              label". What they owed §2 and §4 is the rest: one dash for an
              absence, `ObjectId` for the version ULID that was drawn in the
              business-number typeface, and `Timestamp` for the instant. */}
          <div><dt className="text-label text-text-secondary">Organization</dt><dd className="m-0 mt-1 text-text">{data.project.organization?.name ?? stateLabel("permission_limited")}</dd></div>
          <div><dt className="text-label text-text-secondary">Business Domains</dt><dd className="m-0 mt-1 text-text">{data.project.business_domains.map((domain) => domain.name).join(", ") || NO_VALUE}</dd></div>
          <div><dt className="text-label text-text-secondary">Active Configuration</dt><dd className="m-0 mt-1 text-text">{data.project.active_configuration_version_id ? <ObjectId value={data.project.active_configuration_version_id} title="Active configuration version" /> : NO_VALUE}</dd></div>
          <div><dt className="text-label text-text-secondary">Evidence as of</dt><dd className="m-0 mt-1 text-text"><Timestamp value={data.project.as_of} /></dd></div>
        </dl>
        <div className="grid gap-4 p-5 lg:grid-cols-3">{postureEntries.map(([key, label]) => <PostureCard key={key} label={label} dimension={data.posture[key]} onOpenOwner={onOpenOwner} />)}</div>
      </Panel>
      <Panel flush><PanelHeader title="Next action & attention" description="One ranked action per root cause, with an exact semantic owner." />
        {/* THE QUEUE'S SIZE IS A KPI AND IT SAYS ITS POPULATION (76-5 arbitrage 1).
            `attention.total` is the WHOLE ranked queue, not the eight rows below,
            and it was reachable on this screen only through the truncation
            footnote — so a project with eight problems and a project with eighty
            both read "eight cards". The population is every root cause the server
            ranked for this project and the cut-off is the envelope's own `as_of`. */}
        <div className="border-b border-divider-base">
          <Metric
            label="Root causes needing attention"
            value={formatNumber(data.attention.total)}
            hint={`across this project · as at ${formatTimestamp(data.project.as_of)}`}
            data-testid="attention-total"
          />
        </div>
        <div className="space-y-4 p-5">
          {data.next_action ? <Status as="block" tone="accent" title={data.next_action.cause} action={data.next_action.permitted ? <OwnerButton owner={data.next_action.owner} label={data.next_action.label} onOpenOwner={onOpenOwner} variant="default" /> : null}>
            {data.next_action.permitted ? "Recommended next step" : data.next_action.handoff ?? "Ask an authorized project editor."}
          </Status> : null}
          {data.attention.items.length === 0 ? <EmptyState title="Nothing needs attention" description="No persisted root cause currently requires action." /> : data.attention.items
            .filter((item) => !data.next_action || item.cause !== data.next_action.cause)
            .map((item) => <Status key={item.id} as="block" tone={tone(item.status)} title={item.cause} action={item.action.permitted && data.next_action?.label !== item.action.label ? <OwnerButton owner={item.owner} label={item.action.label} onOpenOwner={onOpenOwner} /> : null}>
            <p className="m-0">{item.impact.join(" ")}</p><p className="mt-1 mb-0 text-caption text-text-secondary">Scope: {item.scope.join(", ") || "Project"} - Evidence horizon: {formatDate(item.evidence_horizon)}</p>
            <p className="mt-1 mb-0 text-caption text-text-secondary">Observed: <Timestamp value={item.first_observed_at} absentMeaning="First observation not recorded" /> to <Timestamp value={item.last_observed_at} absentMeaning="Last observation not recorded" /></p>
          </Status>)}
          {/* A BOUNDED QUEUE SAYS IT IS BOUNDED.
              The server already sends `total` and `has_more`; the screen showed
              neither, so a truncated queue read exactly like a complete one and
              a person could close the page believing they had seen everything
              that needs attention. The count is the whole point: "12 of 47" is a
              different fact from "12".
              `has_more` decides, not `total > items.length`: the filter above
              also removes the next-action row, so comparing lengths here would
              claim truncation on a queue the server sent whole. */}
          {data.attention.has_more ? (
            <p className="m-0 text-caption text-text-secondary" data-testid="attention-truncated">
              Showing {formatNumber(data.attention.items.length)} of {formatNumber(data.attention.total)} items
              that need attention. The rest are not on this page.
            </p>
          ) : null}
        </div>
      </Panel>
      <Panel flush><PanelHeader title="Coverage & readiness" description="Denominators, gaps, active state, and pending state are explicit." />
        {data.coverage.length === 0
          ? <EmptyState
              title="No coverage evidence yet"
              description="Coverage is measured over the Datastreams this project reads, and it has none yet. The Add Datastream action above opens the one wizard that creates the first."
            />
          /* THE COVERAGE CARD IS THE OVERVIEW'S KPI, and until 76-5 it was the
             three classes `Metric` owns -- `font-numeric text-metric font-metric`
             -- copied onto a `<p>`, with the population glued to the numerator by
             a slash and no cut-off anywhere on the card. It is a `Metric` now, its
             `hint` names the population and the evidence cut-off (arbitrages 1 and
             3), and the posture mark keeps its own line so `postureLegend` still
             has marks to explain. */
          : <div className="grid gap-4 p-5 md:grid-cols-2 xl:grid-cols-3">{data.coverage.map((item) => <div key={`${item.kind}:${item.key}`} className="rounded-control border border-divider-base">
          <Metric
            label={wireWord(item.key)}
            value={coverageReading(item)}
            hint={coverageHint(item)}
            data-testid={`coverage-${item.kind}-${item.key}`}
          />
          <div className="flex flex-wrap items-center justify-between gap-2 px-5 pb-3">
            <Status tone={tone(item.state)}>{stateLabel(item.state)}</Status>
            <OwnerButton owner={item.owner} label="Open owner" onOpenOwner={onOpenOwner} variant="ghost" />
          </div>
          <p className="m-0 px-5 pb-2 text-caption text-text-secondary">{item.gaps.length ? `Gaps: ${item.gaps.join(", ")}` : "No persisted gaps"}</p>
          {item.active || item.pending ? <p className="m-0 px-5 pb-4 text-caption text-text-secondary">
            {/* The version was a ULID inside a parenthesis in a sentence, and the
                state beside it was the wire word raw. Both take the console's one
                answer: the declared label, then `ObjectId`. */}
            Active: {item.active ? <>{stateLabel(item.active.state)}{item.active.version_id ? <> <ObjectId value={item.active.version_id} title="Active version" /></> : null}</> : NO_VALUE}
            {" — "}
            Pending: {item.pending ? <>{stateLabel(item.pending.state)}{item.pending.version_id ? <> <ObjectId value={item.pending.version_id} title="Pending version" /></> : null}</> : NO_VALUE}
          </p> : null}
        </div>)}</div>}
      </Panel>
      {/* SETUP POSTURE — `overview.md:133-141`. Overview "summarizes current setup
          posture, the blocking cause and the next relevant step, then deep-links to
          Getting Started or the owning workbench". Each component carries its own
          owner, so every row opens the workbench that can actually close it. */}
      {readinessRows.length ? <Panel flush><PanelHeader title="Setup readiness" description="The same readiness object Getting Started reads, component by component." />
        <ul className="m-0 grid list-none gap-3 p-5 sm:grid-cols-2 xl:grid-cols-3">{readinessRows.map(({ key, component }) => <li key={key}>
          <Status as="block" tone={tone(component.state)} title={wireWord(key)} action={<OwnerButton owner={component.owner} label="Open owner" onOpenOwner={onOpenOwner} variant="ghost" />}>
            <p className="m-0">{stateLabel(component.state)}</p>
            <p className="mt-1 mb-0 text-caption text-text-secondary">Evidence: {component.evidence_ref ? <ObjectId value={component.evidence_ref} title={`${wireWord(key)} evidence`} /> : NO_VALUE}</p>
          </Status>
        </li>)}</ul>
      </Panel> : null}
      <Panel flush><PanelHeader title="Recent outcomes & signals" description={`Persisted outcomes only; empty remains unknown rather than healthy.${alertWindow ? ` Governed alerts cover the ${alertWindow}.` : ""}`} />
        {data.outcomes.items.length === 0 ? <EmptyState {...outcomesEmptyState(data.outcomes.insight_silence)} /> : <ul className="m-0 grid list-none gap-3 p-5">{data.outcomes.items.map((item, index) => <li key={String(item.id ?? index)} className="rounded-control border border-divider-base p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              {/* THE HEADLINE OF AN INSIGHT IS MODEL PROSE, and it is marked as such —
                  the one marker, `ModelAuthored`, drawn from the server's own list.
                  A render or a governed alert keeps its bare heading: `modelAuthored`
                  does not name it, so nothing is added. */}
              <ModelAuthored authorship={item.authorship} field={itemLabelField(item)}>
                <strong className="text-ui text-text">{itemLabel(item, `Outcome ${index + 1}`)}</strong>
              </ModelAuthored>
              {item.kind ? <span className="block text-caption uppercase tracking-wide text-text-secondary">{wireWord(item.kind)}</span> : null}
            </div>
            {item.owner ? <OwnerButton owner={item.owner} label="Open evidence" onOpenOwner={onOpenOwner} variant="ghost" /> : null}
          </div>
          <dl className="m-0 mt-2 grid gap-1 text-caption text-text-secondary sm:grid-cols-2">
            {/* FOUR CELLS, FOUR SPELLINGS OF ONE ABSENCE — "Not stated", "Unknown",
                "None recorded" — and each of them ASSERTED something the server had
                not said. `console-presentation.md` §3 amendment 16: a value nobody
                sent is not a value somebody withheld. One dash, everywhere. */}
            <div><dt className="inline">Period: </dt><dd className="m-0 inline">{periodText(item.period) ?? NO_VALUE}</dd></div>
            <div><dt className="inline">Freshness: </dt><dd className="m-0 inline"><Timestamp value={item.freshness} absentMeaning="No freshness recorded" data-testid="outcome-freshness" /></dd></div>
            <div><dt className="inline">Limitations: </dt><dd className="m-0 inline">{item.limitations?.length ? item.limitations.join(", ") : NO_VALUE}</dd></div>
            <div><dt className="inline">Provenance: </dt><dd className="m-0 inline">{item.provenance?.kind ? wireWord(item.provenance.kind) : NO_VALUE}{item.provenance?.id ? <> <ObjectId value={item.provenance.id} title="Provenance record" /></> : null}</dd></div>
            {/* The SERVER's reading first, and it is the only one that decides. */}
            {confidenceReading(item) ? <div className="sm:col-span-2" data-testid="outcome-confidence-derived"><dt className="inline">Confidence: </dt><dd className="m-0 inline">{confidenceReading(item)}</dd></div> : null}
            {/* The model's own estimate, kept and demoted — marked like every other
                thing the model wrote, so it cannot be mistaken for the line above. */}
            {declaredConfidenceReading(item.authorship, item.confidence ?? null) ? <div className="sm:col-span-2" data-testid="outcome-confidence-declared"><dt className="inline">Model&apos;s own estimate: </dt><dd className="m-0 inline"><ModelAuthored authorship={item.authorship} field="insight.confidence" authored>{declaredConfidenceReading(item.authorship, item.confidence ?? null)}</ModelAuthored></dd></div> : null}
          </dl>
        </li>)}</ul>}
        {data.outcomes.insight_retractions?.length ? <RetractedInsights retractions={data.outcomes.insight_retractions} /> : null}
      </Panel>
      <Panel flush><PanelHeader title="Recent changes" description="Project-scoped persisted change records." />
        {data.changes.items.length === 0 ? <EmptyState title="No recent changes" description="No governed project change is recorded in the current evidence horizon." /> : <ul className="m-0 grid list-none gap-3 p-5">{data.changes.items.map((item, index) => <li key={String(item.id ?? index)} className="rounded-control border border-divider-base p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <strong className="block text-ui text-text">{itemLabel(item, `Change ${index + 1}`)}</strong>
              {/* The kind was the wire token in monospace UPPERCASE and the state was
                  the wire word raw beside it — a database row read out loud. The kind
                  is a word now, the state is the declared one with its tone. */}
              <Cluster className="mt-0.5">
                <span className="text-caption uppercase tracking-wide text-text-secondary">{item.kind ? wireWord(item.kind) : NO_VALUE}</span>
                {item.state ? <Status tone={stateTone(item.state)}>{stateLabel(item.state)}</Status> : null}
              </Cluster>
            </div>
            {item.owner ? <OwnerButton owner={item.owner} label="Open record" onOpenOwner={onOpenOwner} variant="ghost" /> : null}
          </div>
          <p className="m-0 mt-2 text-caption text-text-secondary">
            Occurred: <Timestamp value={item.occurred_at} absentMeaning="No occurrence time recorded" data-testid="change-occurred" />
            {item.version_id ? <> — version <ObjectId value={item.version_id} title="Change version" /></> : null}
          </p>
        </li>)}</ul>}
      </Panel>
    </Stack>
  </PageFrame>;
}

export default function ProjectOverview({ projectId, onOpenOwner }: { projectId: string; onOpenOwner?: (owner: OwnerReference) => void }) {
  const [state, setState] = useState<LoadState>({ kind: "loading", projectId });
  // `Retry` needs something to change for the effect to run again. A counter is
  // the whole mechanism: the request is already idempotent and already scoped by
  // `projectId`, so asking again is the entire repair.
  const [reload, setReload] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setState({ kind: "loading", projectId });
    // Through `apiGet`, never a bare fetch: it is the one seam that attaches the
    // bearer, and a bare call answers 401 in oauth mode — which this page would
    // then render as "posture unavailable" forever (finding F-010).
    apiGet<unknown>(`/api/projects/${encodeURIComponent(projectId)}/overview`, { signal: controller.signal })
      .then((data) => {
        if (!isEnvelope(data)) throw new Error("invalid overview envelope");
        setState({ kind: "ready", projectId, data });
      })
      .catch((error: unknown) => {
        // `apiJson` wraps a rejected fetch, so an abort arrives as ApiError(0)
        // rather than a DOMException: ask the controller, not the error.
        if (controller.signal.aborted) return;
        // A non-disclosing 404 is the server's answer for denied or foreign
        // scope; it is not an outage.
        if (error instanceof ApiError && error.status === 404) { setState({ kind: "denied", projectId }); return; }
        setState({ kind: "error", projectId });
      });
    return () => controller.abort();
  }, [projectId, reload]);
  const visible = state.projectId === projectId ? state : { kind: "loading" as const, projectId };
  // THREE DEAD ENDS BECAME THREE ANSWERS (76-5, on 76-4's primitives). The
  // loading region was a bare `<p>`; the denied and the failed read were a
  // `PageHeader` and nothing else — no error mark, no action, and a person who
  // reached either had only the browser's reload, which loses their place.
  // `console-presentation.md` §5: an error block offers one fallback action.
  if (visible.kind === "loading") return <PageFrame><Loading label="Project Overview" /></PageFrame>;
  // NOT a `Failure`: a non-disclosing 404 is the server's answer for a project
  // that does not exist OR that is not yours, and the two answer identically on
  // purpose. The control that changes project is the shell's switcher, on screen
  // at this moment, so this block names it rather than mounting a second one.
  if (visible.kind === "denied") return <PageFrame><ProjectNotFound /></PageFrame>;
  if (visible.kind === "error") return <PageFrame><Failure
    what="The project posture"
    message="The authoritative posture for this project could not be read. Nothing is shown in its place."
    action={<Retry onClick={() => setReload((count) => count + 1)} />}
  /></PageFrame>;
  return <ProjectOverviewReady data={visible.data} onOpenOwner={onOpenOwner} />;
}
