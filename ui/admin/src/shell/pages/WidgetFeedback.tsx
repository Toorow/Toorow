/**
 * Widget Feedback — the Level 2 review collection (Story 65.8).
 *
 * This screen replaces an Epic 14 vestige, and each of the four reasons it was
 * replaced is a rule this file now holds:
 *
 *   1. it computed `positivePct` — the raw thumbs-up percentage
 *      `analyze-and-test.md:313` forbids by name. **No percentage is rendered
 *      anywhere below without the two numbers it came from.** Every count on
 *      this screen is shown as `annotated / eligible` with its coverage state
 *      and the exact version filters that produced it; a bare figure is a figure
 *      nobody can check.
 *   2. it read the unpinned `GET /api/feedback` row, which names no Result, no
 *      Semantic View version and no AI Path. This screen reads
 *      `/test/feedback`, where every annotation pins the exact observation it
 *      judges.
 *   3. it presented a rating as if it settled something. **User polarity,
 *      human review and automated verdicts are three separate statements**,
 *      and no cell merges them. Feedback prioritizes investigation; it never
 *      proves correctness or regression alone.
 *   4. it carried per-screen classes and a local stylesheet. This file composes
 *      only from `ui/admin/src/ui/index.ts` — no stylesheet, no hex colour, no
 *      literal spacing, no per-screen prefix, no page width clamp.
 */
import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "../../lib/apiFetch";
import { Badge, Button, Cluster, EmptyState, Field, Input, NativeSelect, ObjectId, PageHeader, Panel, PanelBody, PanelHeader, Stack, stateLabel, Status, Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll } from "../../ui";
import { AGGREGATE_AXES, fetchFeedbackAggregates, fetchFeedbackCriticalNegatives, listFeedback, type AggregateAxis, type AggregateBucket, type FeedbackAggregates, type FeedbackCollectionItem, type FeedbackFilters, type OwnerLink } from "../../test/feedbackReviewClient";

type FilterDraft = FeedbackFilters;

function isoDay(offsetDays: number): string {
  const value = new Date();
  value.setUTCDate(value.getUTCDate() + offsetDays);
  return value.toISOString().slice(0, 10);
}

function defaultFilters(): FilterDraft {
  return { observed_from: isoDay(-29), observed_to: isoDay(0) };
}

function filterValue(value: unknown): string {
  if (value !== null && typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function shiftedIsoDay(value: string, offsetDays: number): string | undefined {
  const date = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(date.getTime())) return undefined;
  date.setUTCDate(date.getUTCDate() + offsetDays);
  return date.toISOString().slice(0, 10);
}

function FeedbackFiltersForm({
  value,
  update,
  apply,
  clear,
}: {
  value: FilterDraft;
  update: (patch: Partial<FilterDraft>) => void;
  apply: (event: FormEvent<HTMLFormElement>) => void;
  clear: () => void;
}) {
  return (
    <Panel flush>
      <PanelHeader
        title="Filters"
        description="Counts are recomputed by the server for one bounded observation window and exact version cohort."
      />
      <PanelBody>
        <form onSubmit={apply} className="grid gap-4" aria-label="Feedback aggregate filters">
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          <Field label="Observed from" required>
            {(field) => <Input {...field} type="date" required max={value.observed_to} min={shiftedIsoDay(value.observed_to, -89)} value={value.observed_from} onChange={(event) => update({ observed_from: event.target.value })} />}
          </Field>
          <Field label="Observed to" required>
            {(field) => <Input {...field} type="date" required min={value.observed_from} max={shiftedIsoDay(value.observed_from, 89)} value={value.observed_to} onChange={(event) => update({ observed_to: event.target.value })} />}
          </Field>
          <Field label="Source">
            {(field) => (
              <NativeSelect {...field} value={value.source ?? ""} onChange={(event) => update({ source: event.target.value })}>
                <option value="">All sources</option>
                <option value="authenticated">Authenticated</option>
                <option value="anonymous_share">Anonymous Share</option>
              </NativeSelect>
            )}
          </Field>
          <Field label="Surface">
            {(field) => (
              <NativeSelect {...field} value={value.surface ?? ""} onChange={(event) => update({ surface: event.target.value })}>
                <option value="">All surfaces</option>
                <option value="console">Console</option>
                <option value="mcp_app">MCP App</option>
                <option value="share">Share</option>
              </NativeSelect>
            )}
          </Field>
          <Field label="Target">
            {(field) => (
              <NativeSelect {...field} value={value.target_kind ?? ""} onChange={(event) => update({ target_kind: event.target.value })}>
                <option value="">All targets</option>
                <option value="answer">Answer</option>
                <option value="datum">Datum</option>
                <option value="path_step">Path step</option>
              </NativeSelect>
            )}
          </Field>
          <Field label="Result type">
            {(field) => (
              <NativeSelect {...field} value={value.result_type ?? ""} onChange={(event) => update({ result_type: event.target.value })}>
                <option value="">All result types</option>
                {['scalar', 'series', 'breakdown', 'comparison', 'table', 'narrative', 'refusal'].map((item) => <option key={item} value={item}>{item}</option>)}
              </NativeSelect>
            )}
          </Field>
          <Field label="Semantic View version">
            {(field) => <Input {...field} value={value.semantic_view_version_id ?? ""} onChange={(event) => update({ semantic_view_version_id: event.target.value })} />}
          </Field>
          <Field label="Skill version">
            {(field) => <Input {...field} value={value.skill_version_id ?? ""} onChange={(event) => update({ skill_version_id: event.target.value })} />}
          </Field>
          <Field label="Business Domain id" required={Boolean(value.business_domain_version_number)}>
            {(field) => <Input {...field} required={Boolean(value.business_domain_version_number)} value={value.business_domain_id ?? ""} onChange={(event) => update({ business_domain_id: event.target.value })} />}
          </Field>
          <Field label="Business Domain version" required={Boolean(value.business_domain_id)}>
            {(field) => <Input {...field} required={Boolean(value.business_domain_id)} inputMode="numeric" value={value.business_domain_version_number ?? ""} onChange={(event) => update({ business_domain_version_number: event.target.value })} />}
          </Field>
          <Field label="Capability">
            {(field) => <Input {...field} value={value.capability ?? ""} onChange={(event) => update({ capability: event.target.value })} />}
          </Field>
          <Field label="Visualization Spec version">
            {(field) => <Input {...field} value={value.visualization_spec_version_id ?? ""} onChange={(event) => update({ visualization_spec_version_id: event.target.value })} />}
          </Field>
          <Field label="Renderer build">
            {(field) => <Input {...field} value={value.renderer_build_id ?? ""} onChange={(event) => update({ renderer_build_id: event.target.value })} />}
          </Field>
          <Field label="Runtime build">
            {(field) => <Input {...field} value={value.runtime_build_id ?? ""} onChange={(event) => update({ runtime_build_id: event.target.value })} />}
          </Field>
          <Field label="Theme version">
            {(field) => <Input {...field} value={value.theme_version ?? ""} onChange={(event) => update({ theme_version: event.target.value })} />}
          </Field>
          <Field label="Formatter version">
            {(field) => <Input {...field} value={value.formatter_version ?? ""} onChange={(event) => update({ formatter_version: event.target.value })} />}
          </Field>
          </div>
          <Cluster>
            <Button type="submit">Apply filters</Button>
            <Button type="button" variant="secondary" onClick={clear}>Clear filters</Button>
          </Cluster>
        </form>
      </PanelBody>
    </Panel>
  );
}

type Phase =
  | { status: "loading" }
  | {
      status: "ready";
      annotations: FeedbackCollectionItem[];
      aggregates: FeedbackAggregates;
      collectionFilters: Record<string, unknown>;
      collectionTruncated: boolean;
      collectionNextCursor: string | null;
      criticalNegatives: FeedbackCollectionItem[];
      criticalTruncated: boolean;
      criticalNextCursor: string | null;
    }
  | { status: "not-found" }
  | { status: "error"; message: string };

function polarityTone(polarity: string): "success" | "error" | "neutral" {
  if (polarity === "positive") return "success";
  if (polarity === "negative") return "error";
  return "neutral";
}

/** The filters a figure was computed under, echoed so a reader can restate it. */
export function VersionFilters({ filters }: { filters: Record<string, unknown> }) {
  const entries = Object.entries(filters ?? {}).filter(([, value]) => value !== null && value !== "");
  if (entries.length === 0) {
    return (
      <span className="text-caption text-text-secondary">
        No additional version filter.
      </span>
    );
  }
  return (
    <Cluster>
      {entries.map(([name, value]) => (
        <Badge key={name} tone="neutral">
          {name}: {filterValue(value)}
        </Badge>
      ))}
    </Cluster>
  );
}

/**
 * Coverage, never on its own.
 *
 * The numerator and the denominator travel with it, and when the denominator
 * does not exist the cell reads `Unverifiable` with the server's reason rather
 * than a percentage over nothing.
 */
type CoverageFacts = Pick<
  AggregateBucket,
  "annotated_interactions" | "eligible_interactions" | "coverage_state" | "coverage_reason"
>;

function Coverage({ bucket }: { bucket: CoverageFacts }) {
  if (bucket.coverage_state !== "stated" || bucket.eligible_interactions === null) {
    return (
      <Status tone="neutral" data-testid="coverage-unverifiable">
        Unverifiable — {bucket.coverage_reason ?? "no denominator"}
      </Status>
    );
  }
  return (
    <span>
      <span className="font-numeric [font-variant-numeric:lining-nums_tabular-nums]">
        {bucket.annotated_interactions} / {bucket.eligible_interactions}
      </span>
      <span className="block text-caption text-text-secondary">
        annotated / eligible interactions
      </span>
    </span>
  );
}

function bucketName(bucket: AggregateBucket): string {
  if (!bucket.attributed || !bucket.key) return "Unattributed";
  return Object.values(bucket.key)
    .map(filterValue)
    .join(" · ");
}

function AxisTable({ label, axis }: { label: string; axis: AggregateAxis }) {
  return (
    <Panel flush>
      <PanelHeader
        title={label}
        description={
          axis.buckets_are_exclusive === false
            ? axis.buckets_overlap_note
            : "Positive and negative counts, the denominator, coverage and the version filters — together, on every row."
        }
      />
      <TableScroll label={`Feedback by ${label}`}>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>{label}</TableHead>
              <TableHead>Positive</TableHead>
              <TableHead>Negative</TableHead>
              <TableHead>Annotations</TableHead>
              <TableHead>Coverage</TableHead>
              <TableHead>Unresolved critical negatives</TableHead>
              <TableHead>Version filters</TableHead>
              <TableHead>Compatibility</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {axis.buckets.map((bucket, index) => (
              <TableRow key={`${bucketName(bucket)}-${index}`}>
                <TableCell className="font-semibold text-text">
                  {bucketName(bucket)}
                  {!bucket.attributed && bucket.unattributed_reason && (
                    <span className="block text-caption text-text-secondary">
                      {bucket.unattributed_reason}
                    </span>
                  )}
                </TableCell>
                <TableCell>
                  <span className="font-numeric [font-variant-numeric:lining-nums_tabular-nums]">
                    {bucket.positive}
                  </span>
                </TableCell>
                <TableCell>
                  <span className="font-numeric [font-variant-numeric:lining-nums_tabular-nums]">
                    {bucket.negative}
                  </span>
                </TableCell>
                <TableCell>
                  <span className="font-numeric [font-variant-numeric:lining-nums_tabular-nums]">
                    {bucket.annotations}
                  </span>
                </TableCell>
                <TableCell>
                  <Coverage bucket={bucket} />
                </TableCell>
                <TableCell>
                  {bucket.unresolved_critical_negatives > 0 ? (
                    <Status tone="error">{bucket.unresolved_critical_negatives}</Status>
                  ) : (
                    "0"
                  )}
                </TableCell>
                <TableCell>
                  <VersionFilters filters={bucket.normalized_filters} />
                </TableCell>
                <TableCell className="text-technical break-all">{bucket.compatibility_key}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableScroll>
    </Panel>
  );
}

function targetLabel(annotation: FeedbackCollectionItem): string {
  const target = annotation.target;
  if (!target) return annotation.target_schema_version ? "Unavailable" : "Historical / unpinned";
  if (target.kind === "datum") return `Datum · row ${target.row_index} · ${target.field}`;
  if (target.kind === "path_step") return `Path step · ordinal ${target.ordinal}`;
  return "Answer";
}

// ---------------------------------------------------------------------------
// The collection.
// ---------------------------------------------------------------------------

export default function WidgetFeedback({
  projectId,
  onOpenFeedback,
}: {
  projectId?: string;
  /** Supplied by the shell. Without it the rows stay readable rather than being
   *  dressed as links to a screen nothing would open. */
  onOpenFeedback?: (feedbackId: string) => void;
  /** Retained for shell/test compatibility; owner navigation lives in the exact detail workbench. */
  onOpenOwner?: (link: OwnerLink) => void;
}) {
  const [phase, setPhase] = useState<Phase>({ status: "loading" });
  const [reloadToken, setReloadToken] = useState(0);
  const [draftFilters, setDraftFilters] = useState<FilterDraft>(defaultFilters);
  const [appliedFilters, setAppliedFilters] = useState<FilterDraft>(defaultFilters);
  const [aggregateLoading, setAggregateLoading] = useState(false);
  const [criticalLoading, setCriticalLoading] = useState(false);
  const aggregateAbortRef = useRef<AbortController | null>(null);
  const criticalAbortRef = useRef<AbortController | null>(null);

  useEffect(() => () => {
    aggregateAbortRef.current?.abort();
    criticalAbortRef.current?.abort();
  }, []);

  useEffect(() => {
    aggregateAbortRef.current?.abort();
    criticalAbortRef.current?.abort();
    setAggregateLoading(false);
    setCriticalLoading(false);
    if (!projectId) return;
    const controller = new AbortController();
    let live = true;
    setPhase({ status: "loading" });
    Promise.all([
      listFeedback(projectId, appliedFilters, { signal: controller.signal }),
      fetchFeedbackAggregates(projectId, appliedFilters, { limit: 200 }, { signal: controller.signal }),
      fetchFeedbackCriticalNegatives(projectId, { limit: 50 }, { signal: controller.signal }),
    ])
      .then(([collection, aggregates, criticalNegatives]) => {
        if (!live) return;
        // The two `?? []` below already tolerated a missing list; `aggregates`
        // did not, and the screen reads `aggregates.scope` on its first line
        // after the guards. A 200 without it therefore set `ready` and threw —
        // measured 2026-08-04 in the sandbox: a blank page, zero characters.
        // An unusable answer is an error, which this screen knows how to show.
        if (!aggregates || typeof aggregates !== "object" || !aggregates.axes) {
          setPhase({ status: "error", message: "The feedback aggregates response carries no axes. Nothing was read" });
          return;
        }
        setPhase({
          status: "ready",
          annotations: collection.items ?? [],
          aggregates,
          collectionFilters: collection.normalized_filters,
          collectionTruncated: collection.truncated,
          collectionNextCursor: collection.next_cursor,
          criticalNegatives: criticalNegatives.items ?? [],
          criticalTruncated: criticalNegatives.truncated,
          criticalNextCursor: criticalNegatives.next_cursor,
        });
      })
      .catch((error: unknown) => {
        if (!live || controller.signal.aborted) return;
        // Foreign, denied and absent answer identically by design.
        if (error instanceof ApiError && (error.status === 404 || error.unauthenticated)) {
          setPhase({ status: "not-found" });
          return;
        }
        setPhase({
          status: "error",
          message: error instanceof Error ? error.message : String(error),
        });
      });
    return () => {
      live = false;
      controller.abort();
    };
  }, [projectId, reloadToken, appliedFilters]);

  const retry = useCallback(() => setReloadToken((token) => token + 1), []);
  const updateFilters = useCallback((patch: Partial<FilterDraft>) => {
    setDraftFilters((current) => ({ ...current, ...patch }));
  }, []);
  const applyFilters = useCallback((event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setAppliedFilters({ ...draftFilters });
  }, [draftFilters]);
  const clearFilters = useCallback(() => {
    const cleared = defaultFilters();
    setDraftFilters(cleared);
    setAppliedFilters(cleared);
  }, []);

  const loadNextAggregates = useCallback(async () => {
    if (phase.status !== "ready" || !phase.aggregates.next_cursor || !projectId) return;
    aggregateAbortRef.current?.abort();
    const controller = new AbortController();
    aggregateAbortRef.current = controller;
    const cursor = phase.aggregates.next_cursor;
    setAggregateLoading(true);
    try {
      const page = await fetchFeedbackAggregates(
        projectId,
        appliedFilters,
        { cursor, limit: 200 },
        { signal: controller.signal },
      );
      if (controller.signal.aborted) return;
      setPhase((current) => {
        if (current.status !== "ready" || current.aggregates.next_cursor !== cursor) return current;
        const axes = { ...current.aggregates.axes };
        for (const axis of AGGREGATE_AXES) {
          const existing = current.aggregates.axes[axis.key];
          const continuation = page.axes[axis.key];
          if (existing && continuation) {
            axes[axis.key] = {
              ...continuation,
              buckets: [...existing.buckets, ...continuation.buckets],
            };
          }
        }
        return {
          ...current,
          aggregates: { ...page, axes },
        };
      });
    } catch (error: unknown) {
      if (controller.signal.aborted) return;
      setPhase({
        status: "error",
        message: error instanceof Error ? error.message : String(error),
      });
    } finally {
      if (!controller.signal.aborted) setAggregateLoading(false);
      if (aggregateAbortRef.current === controller) aggregateAbortRef.current = null;
    }
  }, [phase, projectId, appliedFilters]);

  const loadNextCriticalNegatives = useCallback(async () => {
    if (phase.status !== "ready" || !phase.criticalNextCursor || !projectId) return;
    criticalAbortRef.current?.abort();
    const controller = new AbortController();
    criticalAbortRef.current = controller;
    const cursor = phase.criticalNextCursor;
    setCriticalLoading(true);
    try {
      const page = await fetchFeedbackCriticalNegatives(
        projectId,
        { cursor, limit: 50 },
        { signal: controller.signal },
      );
      if (controller.signal.aborted) return;
      setPhase((current) => {
        if (current.status !== "ready" || current.criticalNextCursor !== cursor) return current;
        const known = new Set(current.criticalNegatives.map((annotation) => annotation.id));
        return {
          ...current,
          criticalNegatives: [
            ...current.criticalNegatives,
            ...page.items.filter((annotation) => !known.has(annotation.id)),
          ],
          criticalTruncated: page.truncated,
          criticalNextCursor: page.next_cursor,
        };
      });
    } catch (error: unknown) {
      if (controller.signal.aborted) return;
      setPhase({
        status: "error",
        message: error instanceof Error ? error.message : String(error),
      });
    } finally {
      if (!controller.signal.aborted) setCriticalLoading(false);
      if (criticalAbortRef.current === controller) criticalAbortRef.current = null;
    }
  }, [phase, projectId]);

  const header = (
    <PageHeader
      title="Widget Feedback"
      description="Explicit user reactions, each attached to the exact Result, versions and interaction the person could see when they reacted."
    />
  );
  const filterForm = projectId ? (
    <FeedbackFiltersForm
      value={draftFilters}
      update={updateFilters}
      apply={applyFilters}
      clear={clearFilters}
    />
  ) : null;

  if (!projectId) {
    return (
      <Stack>
        {header}
        <Status as="block" tone="warning" title="Select a Project">
          Feedback is Project-scoped. Nothing has been read, and no other Project's annotations have
          been shown in its place.
        </Status>
      </Stack>
    );
  }
  if (phase.status === "loading") {
    return (
      <Stack>
        {header}
        {filterForm}
        <p role="status" aria-live="polite" className="text-body text-text-secondary">
          Reading the feedback review…
        </p>
      </Stack>
    );
  }
  if (phase.status === "not-found") {
    return (
      <Stack>
        {header}
        {filterForm}
        <Status as="block" tone="warning" title="This collection was not opened">
          This Project has no feedback capability available to you, or the Project does not exist.
          The two answer identically on purpose.
        </Status>
      </Stack>
    );
  }
  if (phase.status === "error") {
    return (
      <Stack>
        {header}
        {filterForm}
        <Status
          as="block"
          tone="error"
          title="The feedback review could not be read"
          action={
            <Button variant="secondary" onClick={retry}>
              Retry
            </Button>
          }
        >
          {phase.message}. No annotation and no figure has been fabricated to fill the screen.
        </Status>
      </Stack>
    );
  }

  const {
    annotations,
    aggregates,
    collectionFilters,
    collectionTruncated,
    collectionNextCursor,
    criticalNegatives,
    criticalTruncated,
    criticalNextCursor,
  } = phase;
  return (
    <Stack>
      {header}
      {filterForm}

      <p role="status" aria-live="polite" className="m-0 text-caption text-text-secondary">
        Feedback results loaded for the normalized filters below.
      </p>

      <Status as="block" tone="neutral" title="Feedback prioritizes; it never proves">
        {aggregates.blocking_use} User polarity, human review and automated evaluation verdicts are
        separately named on every row: a reaction is not a measurement, and this screen reports no
        thumbs-up rate.
      </Status>

      {/* THE COVERAGE OF THE WHOLE PROJECTION, and it had no home.
          `aggregates.scope` carries the numerator and the denominator the server
          computed for these exact filters, and the guard above says in so many
          words that "the screen reads `aggregates.scope` on its first line after
          the guards" — it did not. Per-bucket coverage lived in the axis tables
          only, so a projection whose axes came back empty reported feedback with
          no denominator anywhere on screen: exactly the bare figure this page
          exists to refuse. Same `Coverage` component as every bucket cell, so
          `Unverifiable` and its server-stated reason read identically here. */}
      <Panel className="grid gap-1">
        <span className="text-caption font-semibold text-text">Coverage of this projection</span>
        <Coverage bucket={aggregates.scope} />
      </Panel>

      <Panel flush>
        <PanelHeader
          title="Normalized filters"
          description="The exact server-normalized window and versions used below. No implicit grand total is computed across cohorts."
        />
        <PanelBody className="grid gap-3">
          <div>
            <span className="block text-caption font-semibold text-text">Collection</span>
            <VersionFilters filters={collectionFilters} />
          </div>
          <div>
            <span className="block text-caption font-semibold text-text">Aggregates</span>
            <VersionFilters filters={aggregates.normalized_filters} />
          </div>
        </PanelBody>
      </Panel>

      {(collectionTruncated || aggregates.truncated) && (
        <Status as="block" tone="warning" title="Partial feedback projection">
          {collectionTruncated
            ? `The annotation list is truncated. Next cursor: ${collectionNextCursor ?? "unavailable"}. `
            : ""}
          {aggregates.truncated
            ? `Aggregate cohorts are truncated. Next cursor: ${aggregates.next_cursor ?? "unavailable"}.`
            : ""}
        </Status>
      )}

      {aggregates.truncated && aggregates.next_cursor && (
        <Cluster>
          <Button variant="secondary" disabled={aggregateLoading} onClick={() => void loadNextAggregates()}>
            {aggregateLoading ? "Loading aggregate cohorts…" : "Load next aggregate cohorts"}
          </Button>
        </Cluster>
      )}

      {AGGREGATE_AXES.map((axis) =>
        aggregates.axes?.[axis.key] ? (
          <AxisTable key={axis.key} label={axis.label} axis={aggregates.axes[axis.key]} />
        ) : null,
      )}

      <Panel flush>
        <PanelHeader
          title="Unresolved critical negatives"
          description="A bounded priority queue: 50 unresolved critical negative annotations per server page, continued only by its cursor. It prioritizes review; it does not seed evaluation cases from this screen."
        />
        {criticalNegatives.length === 0 ? (
          <EmptyState
            title="No unresolved critical negative"
            description="No critical negative remains in the bounded server projection for this Project."
          />
        ) : (
          <TableScroll label="Unresolved critical negative feedback">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Feedback</TableHead>
                  <TableHead>Source & target</TableHead>
                  <TableHead>Result</TableHead>
                  <TableHead>Human review</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {criticalNegatives.map((annotation) => (
                  <TableRow key={`critical-${annotation.id}`}>
                    <TableCell>
                      {onOpenFeedback ? (
                        <Button variant="link" size="sm" onClick={() => onOpenFeedback(annotation.id)}>
                          {annotation.comment ?? annotation.id}
                        </Button>
                      ) : (
                        annotation.comment ?? annotation.id
                      )}
                    </TableCell>
                    <TableCell>{annotation.source} · {targetLabel(annotation)}</TableCell>
                    <TableCell><ObjectId value={annotation.result.id} title="Result" /></TableCell>
                    <TableCell><Badge tone="neutral">{stateLabel(annotation.review.current_state)}</Badge></TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        )}
        {criticalTruncated && criticalNextCursor && (
          <PanelBody>
            <Button
              variant="secondary"
              size="sm"
              disabled={criticalLoading}
              onClick={() => void loadNextCriticalNegatives()}
            >
              {criticalLoading ? "Loading critical negatives…" : "Load next critical negatives"}
            </Button>
          </PanelBody>
        )}
        {criticalTruncated && !criticalNextCursor && (
          <PanelBody>
            <Status tone="warning">
              The critical-negative queue is truncated, but no continuation cursor was provided.
            </Status>
          </PanelBody>
        )}
      </Panel>

      <Panel flush>
        <PanelHeader
          title="Annotations"
          description="Each row pins the exact Result and Semantic View version the person saw. Nothing here is aggregated into a score."
        />
        {annotations.length === 0 ? (
          <EmptyState
            title="No annotation in this Project"
            description="Nothing has been read from the legacy unpinned feedback table in its place: those rows name no Result, no Semantic View version and no AI Path, and would look identical to ones that do."
          />
        ) : (
          <TableScroll label="Feedback annotations">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Polarity</TableHead>
                  <TableHead>Source & target</TableHead>
                  <TableHead>Comment</TableHead>
                  <TableHead>Result</TableHead>
                  <TableHead>Semantic View version</TableHead>
                  <TableHead>AI Path</TableHead>
                  <TableHead>Human review</TableHead>
                  <TableHead>Automated verdicts</TableHead>
                  <TableHead>Observed</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {annotations.map((annotation) => (
                  <TableRow key={annotation.id}>
                    <TableCell>
                      <Status
                        tone={polarityTone(annotation.polarity)}
                        data-testid={`polarity-${annotation.id}`}
                      >
                        {annotation.polarity}
                      </Status>
                      <span className="block text-caption text-text-secondary">
                        User polarity · {annotation.observed_surface}
                      </span>
                    </TableCell>
                    <TableCell>
                      <span>{annotation.source}</span>
                      <span className="block text-caption text-text-secondary">
                        {targetLabel(annotation)}
                      </span>
                    </TableCell>
                    <TableCell>
                      {onOpenFeedback ? (
                        <Button variant="link" size="sm" onClick={() => onOpenFeedback(annotation.id)}>
                          {annotation.comment ?? "No comment"}
                        </Button>
                      ) : (
                        (annotation.comment ?? "No comment")
                      )}
                    </TableCell>
                    <TableCell><ObjectId value={annotation.result.id} title="Result" /></TableCell>
                    <TableCell className="text-technical break-all">
                      {annotation.semantic_view.version_id ?? "Unavailable"}
                    </TableCell>
                    <TableCell className="text-technical break-all">
                      {annotation.ai_path ?? "Unavailable"}
                    </TableCell>
                    {/* User polarity, human conclusion and automated evidence
                        remain three separately named cells. */}
                    <TableCell data-testid={`human-${annotation.id}`}>
                      <Badge tone="neutral">{stateLabel(annotation.review.current_state)}</Badge>
                    </TableCell>
                    <TableCell data-testid={`automated-${annotation.id}`}>
                      {annotation.automated_verdicts?.items.length ? (
                        <span>{annotation.automated_verdicts.items.length} separately stored verdict(s)</span>
                      ) : annotation.automated_verdicts?.state === "available" ? (
                        <span>No compatible automated verdict.</span>
                      ) : annotation.automated_verdicts ? (
                        <Status tone="neutral">
                          Unavailable · {annotation.automated_verdicts.reason ?? "no compatible automated verdict"}
                        </Status>
                      ) : (
                        <span>Open the exact detail to read automated verdicts.</span>
                      )}
                    </TableCell>
                    <TableCell>{annotation.observed_at ?? "Unavailable"}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        )}
      </Panel>
    </Stack>
  );
}
