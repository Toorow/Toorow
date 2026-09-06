/**
 * The manual annotations of a Project — written, corrected and withdrawn here.
 *
 * WHY THIS EXISTS. `app.context_events` holds two kinds of row. What a Connector
 * emits is owned by its Datastream's Event Configuration and is listed by the
 * table above this one. The other kind is the MANUAL annotation: a person saying
 * what happened on a day that the data alone does not explain — a price change,
 * a launch, an outage.
 *
 * That half had NO screen at all. `POST /api/context-events` and
 * `GET /api/context-events` were reachable and called by nothing (audit
 * 2026-08-17, P2 "routes orphelines"), and creation lived only in the MCP tool
 * `add_context_event`. Meanwhile `narrative`, `summarizer` and every "Why" built
 * on top read these rows as CAUSES — so a wrong date entered once was repeated
 * in every narration built afterwards, with no way for anyone to correct it.
 *
 * The rule the amendment of 2026-08-17 states: a table the product cites as a
 * cause carries a human path of correction. That path is list, correct, withdraw
 * — and withdrawing is a supersede, never a delete, because narrations already
 * built on an annotation must stay explainable.
 *
 * WHAT THIS SCREEN REFUSES TO OFFER. A Connector-emitted row is derived: the next
 * pull overwrites anything typed over it. The server refuses such a correction
 * and names the gesture that works; this screen does not render the button in the
 * first place, reading `capabilities.can_correct` per row rather than guessing
 * from `source`.
 */
import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../lib/apiFetch";
import { Button, ConfirmDialog, EmptyState, Input, Status, Textarea, Timestamp } from "../ui";

interface ManualEventRow {
  id: string;
  project_id: string;
  event_date: string;
  type: string;
  label: string;
  description: string | null;
  metric: string | null;
  created_by: string;
  created_at: string;
  source: string;
  retired_at: string | null;
  retired_by: string | null;
  retired_reason: string | null;
  /** How many earlier wordings this annotation has (migration 328). */
  revision_count?: number;
  capabilities?: { can_write?: boolean; can_correct?: boolean; version_history?: boolean };
}

/**
 * One wording this annotation had before a correction.
 *
 * The server drops the `previous_` prefix: a revision is READ in the vocabulary
 * of an event, so the same words render the superseded annotation and the
 * current one.
 */
interface RevisionRow {
  id: string;
  revision_no: number;
  superseded_at: string;
  corrected_by: string;
  corrected_fields: string[];
  event_date: string;
  type: string;
  label: string;
  description: string | null;
  metric: string | null;
}

type HistoryState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; revisions: RevisionRow[] };

/**
 * The three states an annotation can be in, in the words the page uses.
 *
 * `Live` is not decoration: without it, the only labelled states are the
 * exceptions, and a reader has to infer the ordinary one from the absence of a
 * badge. `Corrected` and `Withdrawn` are not exclusive — a withdrawn annotation
 * that was corrected first keeps both, because both happened.
 */
function annotationState(event: ManualEventRow): { label: string; tone: "neutral" | "info" | "warning" } {
  if (event.retired_at) return { label: "Withdrawn", tone: "warning" };
  if ((event.revision_count ?? 0) > 0) return { label: "Corrected", tone: "info" };
  // The ordinary state wears the ordinary tone: an annotation that is simply
  // being read as a cause is not news, and a console where everything signals
  // is a console where nothing does.
  return { label: "Live", tone: "neutral" };
}

type ListState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; events: ManualEventRow[]; canWrite: boolean };

interface Draft {
  mode: "create" | "correct";
  id: string | null;
  event_date: string;
  type: string;
  label: string;
  description: string;
  /** "" is not a missing answer here — it is the answer "every metric". */
  metric: string;
}

/** A governed metric of this Project, as `/api/datamodel/fields` returns it. */
interface GovernedMetric {
  name: string;
  display_name?: string;
}

async function readMessage(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { message?: string };
    return body.message ?? `HTTP ${response.status}`;
  } catch {
    return `HTTP ${response.status}`;
  }
}

const EMPTY_DRAFT: Draft = {
  mode: "create", id: null, event_date: "", type: "business", label: "", description: "", metric: "",
};

/**
 * The one word for "this annotation is about no metric in particular".
 *
 * It is the DEFAULT, and it is a real answer rather than an empty one: an
 * outage, a holiday, a price change across the board concerns every metric, and
 * such an annotation stays readable as a cause under every claim. Naming a
 * metric NARROWS where it will ever be offered — which is the point, and the
 * reason the option says so instead of leaving a blank line at the top of a
 * list.
 */
const EVERY_METRIC_LABEL = "Every metric";

/**
 * How the control names what is behind it.
 *
 * The count is on the control rather than in a badge because it is what decides
 * whether opening it is worth a click, and "1 earlier wording" is a different
 * promise from "4 earlier wordings".
 */
function earlierWordings(count: number): string {
  return count === 1 ? "1 earlier wording" : `${count} earlier wordings`;
}

/**
 * A row is an annotation, or it is not rendered as one.
 *
 * An annotation without an id cannot be corrected or withdrawn, and one without
 * a date is not about a day — so rendering it would put a line on screen that
 * looks like evidence and answers none of the questions this panel exists to
 * answer. Half-reading a shape is how a corpus ages without anyone seeing it:
 * the reader believes the list is what the project observed.
 */
function isAnnotation(value: unknown): value is ManualEventRow {
  if (!value || typeof value !== "object") return false;
  const row = value as Record<string, unknown>;
  return typeof row.id === "string" && row.id !== ""
    && typeof row.event_date === "string" && row.event_date !== ""
    && typeof row.label === "string";
}

export default function ManualEventAnnotations({ projectId }: { projectId?: string }) {
  const scope = (projectId ?? "").trim();
  const [state, setState] = useState<ListState>({ status: "loading" });
  const [includeRetired, setIncludeRetired] = useState(false);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [metrics, setMetrics] = useState<GovernedMetric[]>([]);
  const [metricNotice, setMetricNotice] = useState("");
  const [history, setHistory] = useState<Record<string, HistoryState>>({});
  const [openHistory, setOpenHistory] = useState<string | null>(null);
  const [pendingWithdraw, setPendingWithdraw] = useState<ManualEventRow | null>(null);
  const [withdrawReason, setWithdrawReason] = useState("");
  const [withdrawing, setWithdrawing] = useState(false);
  const [withdrawError, setWithdrawError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!scope) {
      setState({ status: "error", message: "Choose a Project to read its annotations." });
      return;
    }
    setState({ status: "loading" });
    try {
      const response = await apiFetch(
        `/api/context-events?project_id=${encodeURIComponent(scope)}`
        + (includeRetired ? "&include_retired=true" : ""),
        { cache: "no-store" },
      );
      if (!response.ok) {
        setState({ status: "error", message: await readMessage(response) });
        return;
      }
      const body = (await response.json()) as {
        events?: ManualEventRow[];
        capabilities?: { can_write?: boolean };
      };
      const received = Array.isArray(body.events) ? body.events : [];
      const annotations = received.filter(isAnnotation);
      // A LIST THAT DROPPED ROWS IS NOT AN EMPTY LIST. Silently keeping the
      // well-formed half would let this panel claim a project observed nothing
      // when what actually happened is that we could not read what it observed.
      if (received.length > 0 && annotations.length === 0) {
        setState({
          status: "error",
          message:
            "The annotations of this Project came back in a shape this screen could not read, "
            + "so none is shown rather than a partial list. Reopen this page, and report it if it persists.",
        });
        return;
      }
      setState({
        status: "ready",
        events: annotations,
        canWrite: body.capabilities?.can_write === true,
      });
    } catch (error) {
      setState({ status: "error", message: error instanceof Error ? error.message : String(error) });
    }
  }, [scope, includeRetired]);

  useEffect(() => { void load(); }, [load]);

  // The governed metrics this Project may name, read from the SAME catalogue the
  // server validates the write against (`/api/datamodel/fields?kind=metric`,
  // which the Skill editor already picks from). A picker offering a name the
  // door refuses is a picker that teaches people to distrust it.
  useEffect(() => {
    if (!scope) return;
    const controller = new AbortController();
    void (async () => {
      try {
        const response = await apiFetch(
          `/api/datamodel/fields?project_id=${encodeURIComponent(scope)}&kind=metric`,
          { signal: controller.signal, cache: "no-store" },
        );
        if (!response.ok) {
          setMetricNotice("The governed metrics could not be read, so an annotation written now is about every metric.");
          return;
        }
        const body = (await response.json()) as GovernedMetric[] | null;
        const list = Array.isArray(body) ? body.filter((field) => typeof field?.name === "string" && field.name !== "") : [];
        setMetrics(list);
        // AN EMPTY LIST SAYS WHY, and names what it still allows: writing the
        // annotation about every metric is a complete, correct answer here.
        setMetricNotice(list.length ? "" : "This Project governs no metric yet, so an annotation written now is about every metric.");
      } catch (error) {
        if (!controller.signal.aborted) {
          setMetricNotice("The governed metrics could not be read, so an annotation written now is about every metric.");
        }
      }
    })();
    return () => controller.abort();
  }, [scope]);

  const submitDraft = async () => {
    if (!draft) return;
    setSaving(true);
    setSaveError(null);
    try {
      const response = draft.mode === "create"
        ? await apiFetch(`/api/context-events?project_id=${encodeURIComponent(scope)}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              project_id: scope,
              event_date: draft.event_date,
              type: draft.type.trim(),
              label: draft.label.trim(),
              description: draft.description,
              // "" travels as null: the server reads both as "every metric",
              // and null is the word this API uses for a cleared field.
              metric: draft.metric.trim() || null,
            }),
          })
        : await apiFetch(
            `/api/context-events/${encodeURIComponent(draft.id ?? "")}?project_id=${encodeURIComponent(scope)}`,
            {
              method: "PATCH",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                event_date: draft.event_date,
                type: draft.type.trim(),
                label: draft.label.trim(),
                description: draft.description,
                metric: draft.metric.trim() || null,
              }),
            },
          );
      if (!response.ok) {
        // The server's own sentence: a refusal here NAMES the gesture that
        // repairs (edit the Event Configuration, the event is withdrawn).
        // Replacing it with a generic message would lose the only part a
        // person can act on.
        setSaveError(await readMessage(response));
        return;
      }
      // A correction files a new revision, so the history read a moment ago is
      // now short by one. Dropping it is honest; keeping it would show a
      // complete-looking list missing the wording just superseded.
      if (draft.mode === "correct" && draft.id) {
        const corrected = draft.id;
        setHistory((current) => {
          const next = { ...current };
          delete next[corrected];
          return next;
        });
      }
      setDraft(null);
      await load();
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : String(error));
    } finally {
      setSaving(false);
    }
  };

  /**
   * The earlier wordings of ONE annotation, read when a person asks for them.
   *
   * Not with the list: a history is a second question, and fetching every
   * annotation's history to render a badge would spend a request per row on
   * something nobody opened. `revision_count` already answers whether there is
   * anything behind the control.
   */
  const openRevisions = async (event: ManualEventRow) => {
    if (openHistory === event.id) {
      setOpenHistory(null);
      return;
    }
    setOpenHistory(event.id);
    if (history[event.id]?.status === "ready") return;
    setHistory((current) => ({ ...current, [event.id]: { status: "loading" } }));
    try {
      const response = await apiFetch(
        `/api/context-events/${encodeURIComponent(event.id)}/revisions`
        + `?project_id=${encodeURIComponent(scope)}`,
        { cache: "no-store" },
      );
      if (!response.ok) {
        const message = await readMessage(response);
        setHistory((current) => ({
          ...current,
          [event.id]: { status: "error", message },
        }));
        return;
      }
      const body = (await response.json()) as { revisions?: RevisionRow[] };
      setHistory((current) => ({
        ...current,
        [event.id]: {
          status: "ready",
          revisions: Array.isArray(body.revisions) ? body.revisions : [],
        },
      }));
    } catch (error) {
      setHistory((current) => ({
        ...current,
        [event.id]: {
          status: "error",
          message: error instanceof Error ? error.message : String(error),
        },
      }));
    }
  };

  const withdraw = async () => {
    if (!pendingWithdraw) return;
    if (!withdrawReason.trim()) {
      setWithdrawError("Add a reason — a withdrawal nobody can judge later is an anonymous erasure.");
      return;
    }
    setWithdrawing(true);
    setWithdrawError(null);
    try {
      const response = await apiFetch(
        `/api/context-events/${encodeURIComponent(pendingWithdraw.id)}/retire?project_id=${encodeURIComponent(scope)}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ reason: withdrawReason.trim() }),
        },
      );
      if (!response.ok) {
        setWithdrawError(await readMessage(response));
        return;
      }
      setPendingWithdraw(null);
      setWithdrawReason("");
      await load();
    } catch (error) {
      setWithdrawError(error instanceof Error ? error.message : String(error));
    } finally {
      setWithdrawing(false);
    }
  };

  return (
    <section className="mt-8 flex flex-col gap-4" data-testid="manual-annotations">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-label font-label text-text">Manual annotations</h2>
          <p className="m-0 text-caption text-text-secondary">
            What happened on a day that the data alone does not explain. These are read as causes
            when this Project's results are narrated.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-2 text-caption text-text-secondary">
            <Input
              type="checkbox"
              checked={includeRetired}
              onChange={(event) => setIncludeRetired(event.target.checked)}
              data-testid="manual-annotations-include-retired"
            />
            Show withdrawn
          </label>
          {state.status === "ready" && state.canWrite && (
            <Button
              type="button"
              variant="secondary"
              data-testid="manual-annotations-add"
              onClick={() => { setSaveError(null); setDraft({ ...EMPTY_DRAFT }); }}
            >
              Add an annotation
            </Button>
          )}
        </div>
      </header>

      {state.status === "loading" && <p role="status">Reading annotations…</p>}
      {state.status === "error" && (
        <Status as="block" tone="error" data-testid="manual-annotations-error">
          {state.message}{" "}
          <Button type="button" variant="ghost" onClick={() => void load()}>Retry</Button>
        </Status>
      )}

      {state.status === "ready" && state.events.length === 0 && (
        // AN EMPTY LIST NAMES THE GESTURE THAT FILLS IT. Both branches say
        // "add one", because that is what fills this list; the live-only branch
        // ADDS that withdrawn rows are hidden, since there an empty list has two
        // possible causes and a reader cannot tell them apart.
        <div data-testid="manual-annotations-empty">
          <EmptyState
            title={includeRetired ? "No annotation on this Project" : "No annotation is live on this Project"}
            description={
              includeRetired
                ? "Nobody has written down what happened on a day here yet. Add one when a price change, a launch or an outage explains a movement the data does not."
                : "Nothing is currently read as a cause here. Add an annotation when a price change, a launch or an outage explains a movement the data does not — and turn on Show withdrawn to check whether one was retracted."
            }
          />
        </div>
      )}

      {state.status === "ready" && state.events.length > 0 && (
        <ul className="flex flex-col gap-2" data-testid="manual-annotations-list">
          {state.events.map((event) => (
            <li
              key={event.id}
              className="flex flex-wrap items-start justify-between gap-3 rounded-lg border border-border p-3"
              data-testid={`manual-annotation-${event.id}`}
            >
              <div className="flex min-w-0 flex-col gap-1">
                <span className="flex flex-wrap items-center gap-2">
                  <strong className="text-ui text-text">{event.label}</strong>
                  {/* THE STATE IS DRAWN, NOT INFERRED. A withdrawal is shown
                      rather than vanished, and a corrected annotation says so
                      beside the wording it now carries -- otherwise a reader
                      comparing it with a narration written last week has no way
                      to know the two ever differed. */}
                  <Status
                    tone={annotationState(event).tone}
                    data-testid={`manual-annotation-state-${event.id}`}
                  >
                    {annotationState(event).label}
                  </Status>
                </span>
                <span className="text-caption text-text-secondary">
                  {/* The scope is shown, not inferred: it decides which claims
                      this annotation is ever read under, and an annotation
                      narrowed to the wrong metric goes silent with no symptom. */}
                  {event.event_date} · {event.type} · {event.metric ? `about ${event.metric}` : EVERY_METRIC_LABEL.toLowerCase()}
                  {/* The source is the fact that decides whether a person may
                      correct this row at all, so it is shown rather than
                      inferred from a greyed-out button. */}
                  {event.source !== "manual" && ` · emitted by ${event.source}`}
                </span>
                {event.description && <span className="text-caption text-text-secondary">{event.description}</span>}
                {event.retired_at && (
                  <span className="text-caption text-text-secondary" data-testid={`manual-annotation-retired-${event.id}`}>
                    Withdrawn <Timestamp value={event.retired_at} /> by {event.retired_by}
                    {event.retired_reason ? ` — ${event.retired_reason}` : ""}
                  </span>
                )}
                {(event.revision_count ?? 0) > 0 && (
                  <Button
                    type="button"
                    variant="ghost"
                    className="self-start px-0 text-caption"
                    data-testid={`manual-annotation-history-toggle-${event.id}`}
                    aria-expanded={openHistory === event.id}
                    onClick={() => void openRevisions(event)}
                  >
                    {openHistory === event.id ? "Hide earlier wordings" : earlierWordings(event.revision_count ?? 0)}
                  </Button>
                )}
                {openHistory === event.id && (
                  <div
                    className="mt-1 flex flex-col gap-2 border-l border-border pl-3"
                    data-testid={`manual-annotation-history-${event.id}`}
                  >
                    {history[event.id]?.status === "loading" && (
                      <span className="text-caption text-text-secondary" role="status">
                        Reading earlier wordings…
                      </span>
                    )}
                    {history[event.id]?.status === "error" && (
                      <Status as="block" tone="error" data-testid={`manual-annotation-history-error-${event.id}`}>
                        {(history[event.id] as { message: string }).message}{" "}
                        <Button type="button" variant="ghost" onClick={() => void openRevisions(event)}>Retry</Button>
                      </Status>
                    )}
                    {history[event.id]?.status === "ready"
                      && (history[event.id] as { revisions: RevisionRow[] }).revisions.map((revision) => (
                        <div
                          key={revision.id}
                          className="flex flex-col"
                          data-testid={`manual-annotation-revision-${revision.id}`}
                        >
                          {/* WHAT IT SAID, then who changed it and when. The
                              wording comes first because it is the reason
                              anyone opens this: a narration cites the wording,
                              never the revision number. */}
                          <span className="text-caption text-text">
                            {revision.label} · {revision.event_date}
                            {revision.metric ? ` · about ${revision.metric}` : ""}
                          </span>
                          {revision.description && (
                            <span className="text-caption text-text-secondary">{revision.description}</span>
                          )}
                          <span className="text-caption text-text-secondary">
                            Corrected <Timestamp value={revision.superseded_at} /> by {revision.corrected_by}
                            {revision.corrected_fields.length
                              ? ` — ${revision.corrected_fields.join(", ")}`
                              : ""}
                          </span>
                        </div>
                      ))}
                  </div>
                )}
              </div>
              {event.capabilities?.can_correct && (
                <div className="flex flex-wrap gap-2">
                  <Button
                    type="button"
                    variant="secondary"
                    data-testid={`manual-annotation-correct-${event.id}`}
                    onClick={() => {
                      setSaveError(null);
                      setDraft({
                        mode: "correct",
                        id: event.id,
                        event_date: event.event_date,
                        type: event.type,
                        label: event.label,
                        description: event.description ?? "",
                        metric: event.metric ?? "",
                      });
                    }}
                  >
                    Correct
                  </Button>
                  <Button
                    type="button"
                    variant="secondary"
                    data-testid={`manual-annotation-withdraw-${event.id}`}
                    onClick={() => { setWithdrawError(null); setWithdrawReason(""); setPendingWithdraw(event); }}
                  >
                    Withdraw
                  </Button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      {draft && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-6" role="presentation">
          <section
            className="flex w-full max-w-[560px] flex-col gap-4 rounded-lg border border-border bg-surface p-6 shadow-lg"
            role="dialog"
            aria-modal="true"
            aria-label={draft.mode === "create" ? "Add an annotation" : "Correct this annotation"}
          >
            <h3 className="m-0">{draft.mode === "create" ? "Add an annotation" : "Correct this annotation"}</h3>
            <label>Date
              <Input
                type="date"
                data-testid="manual-annotation-date"
                value={draft.event_date}
                onChange={(event) => setDraft({ ...draft, event_date: event.target.value })}
                disabled={saving}
              />
            </label>
            <label>What happened
              <Input
                data-testid="manual-annotation-label"
                value={draft.label}
                onChange={(event) => setDraft({ ...draft, label: event.target.value })}
                disabled={saving}
                placeholder="e.g. Price increased on the main plan"
              />
            </label>
            <label>Kind
              <Input
                data-testid="manual-annotation-type"
                value={draft.type}
                onChange={(event) => setDraft({ ...draft, type: event.target.value })}
                disabled={saving}
              />
            </label>
            <label>About which metric
              <select
                className="w-full rounded-md border border-border bg-surface p-2 text-ui text-text"
                data-testid="manual-annotation-metric"
                value={draft.metric}
                onChange={(event) => setDraft({ ...draft, metric: event.target.value })}
                disabled={saving}
              >
                <option value="">{EVERY_METRIC_LABEL}</option>
                {/* A metric the row already names but the catalogue no longer
                    lists is kept as an option rather than silently rewritten to
                    "every metric" by opening the correction dialog. */}
                {draft.metric && !metrics.some((metric) => metric.name === draft.metric) && (
                  <option value={draft.metric}>{draft.metric}</option>
                )}
                {metrics.map((metric) => (
                  <option key={metric.name} value={metric.name}>
                    {metric.display_name || metric.name}
                  </option>
                ))}
              </select>
            </label>
            {metricNotice && (
              <p className="m-0 text-caption text-text-secondary" data-testid="manual-annotation-metric-notice">
                {metricNotice}
              </p>
            )}
            <label>Detail
              <Textarea
                rows={3}
                data-testid="manual-annotation-description"
                value={draft.description}
                onChange={(event) => setDraft({ ...draft, description: event.target.value })}
                disabled={saving}
              />
            </label>
            {saveError && <Status as="block" tone="error" data-testid="manual-annotation-save-error">{saveError}</Status>}
            <div className="flex justify-end gap-3">
              <Button type="button" variant="secondary" onClick={() => setDraft(null)} disabled={saving}>Cancel</Button>
              <Button
                type="button"
                data-testid="manual-annotation-save"
                onClick={() => void submitDraft()}
                disabled={saving || !draft.label.trim() || !draft.event_date}
              >
                {saving ? "Saving…" : "Save"}
              </Button>
            </div>
          </section>
        </div>
      )}

      {/* WITHDRAWING IS NOT DELETING, and the confirmation says so in words
          rather than leaving a person to assume the usual. The reason is
          required by the server and asked for here, so the refusal never has
          to be the way someone learns it. */}
      <ConfirmDialog
        open={pendingWithdraw !== null}
        onOpenChange={(open) => { if (!open) { setPendingWithdraw(null); setWithdrawError(null); } }}
        title={pendingWithdraw ? `Withdraw “${pendingWithdraw.label}”?` : "Withdraw this annotation?"}
        description={
          pendingWithdraw ? (
            <>
              <span>
                It stops being read as a cause from now on. It is not deleted: narrations already
                built on it stay explainable, and this withdrawal cannot be undone — write the
                annotation again if it turns out to be right.
              </span>
              <label className="mt-4 flex flex-col gap-1.5 text-label font-label text-text">
                Reason
                <Textarea
                  value={withdrawReason}
                  onChange={(event) => setWithdrawReason(event.target.value)}
                  rows={2}
                  placeholder="Why this stops being read as a cause"
                  data-testid="manual-annotation-withdraw-reason"
                />
              </label>
            </>
          ) : ""
        }
        confirmLabel="Withdraw annotation"
        destructive
        busy={withdrawing}
        error={withdrawError}
        onConfirm={() => void withdraw()}
        data-testid="manual-annotation-withdraw-confirm"
        cancelTestId="manual-annotation-withdraw-cancel"
        confirmTestId="manual-annotation-withdraw-accept"
      />
    </section>
  );
}
