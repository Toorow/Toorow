/**
 * Sharing a published daily insight — the ONE door, and the three states a row
 * can be in (AI-294, last remnant).
 *
 * WHY THIS FILE EXISTS. `proactive-assertions.md` (decision 2, amended
 * 2026-08-17) closed the design question this waited on: publishing an insight
 * DERIVES a governed Query Spec from the card contract and executes it, so a
 * published insight names a real Result — or names, by hand, the link that was
 * missing. Migration 281 stores that lineage and `daily_insights_api` serves it.
 * Nothing in `ui/` read it, so the person still had no way from an insight to a
 * share.
 *
 * THE DOOR IS NOT NEW, AND THIS FILE OPENS NO SECOND ONE. The same document
 * refuses an insight-specific share path in the same paragraph that grants the
 * Result: "a Share still opens an `app.renders` row over a real Result
 * (`analyze_artifacts.create_render` → `render_shares.create_share`). No
 * insight-specific share creation exists, and adding one would reopen the
 * parallel-store clause this answer refused." So a shareable insight is opened
 * AT ITS RESULT, where the console's one chain already lives — freeze the view
 * you want to send (`VisualizationBuilder`), then create the revocable link over
 * that frozen Render (`analyze-artifacts/RenderSharing.tsx`). This module
 * creates nothing, posts nothing and mints no link of its own.
 *
 * WHY IT DOES NOT FREEZE THE RENDER HERE. `analyze_artifacts.create_render`
 * refuses pin by pin, and one of the ten pins is a Visualization Spec version —
 * a PRESENTATION. Choosing one for an insight card is a design decision no
 * ratified document has taken, and taking it here to make a button feel complete
 * would be inventing a decision and presenting it as wiring.
 *
 * THREE STATES, AND NOT ONE OF THEM IS A DEAD CONTROL:
 *   1. the insight names a Result   -> the door opens, at that Result;
 *   2. the insight names a reason   -> the reason is said, and the gesture with
 *      it. No button: publication succeeded, sharing is what the refusal cost;
 *   3. the insight names neither    -> published before migration 281. Nothing
 *      is inferred, and above all nothing is invented: an old row is not a
 *      refused one.
 *
 * Composed only from `ui/admin/src/ui/index.ts`, and it reads through the API
 * seam (`apiGet`) so the bearer cannot be forgotten.
 */
import { useState } from "react";

import { apiGet, apiPost } from "../lib/apiFetch";
import { Button, Stack, Status } from "../ui";
import {
  declaredConfidenceReading,
  derivedConfidenceReading,
  ModelAuthored,
  type InsightAuthorship,
} from "./InsightProvenance";

/** One `app.daily_insights` row as `GET /api/daily-insights/runs/{date}` serves it.
 *
 *  `result_id` and `result_unavailable_reason` are exclusive by DB CHECK
 *  (migration 281); this type does not restate that as a union, because a row
 *  that broke it must still be rendered as SOMETHING rather than crash a panel. */
export interface DailyInsightRow {
  id: string;
  slot?: number | null;
  /** The five prose fields are declared because they are DRAWN — see
   *  `MODEL_AUTHORED_PROSE` below. Until this lot only `title` was, so four of
   *  the five things the model wrote reached no reader at all, and the one that
   *  did was drawn in the typography of a server-composed label. */
  payload?: {
    insight?: {
      title?: string | null;
      summary?: string | null;
      whyItMatters?: string | null;
      recommendedAction?: string | null;
      limitations?: string[] | null;
      confidence?: string | null;
    } | null;
    authorship?: InsightAuthorship | null;
  } | null;
  result_id?: string | null;
  result_unavailable_reason?: string | null;
  /** Migration 321: an audited withdrawal, never a delete. The three columns move
   *  together or not at all (DB CHECK), so one of them present means all three. */
  retracted_at?: string | null;
  retracted_by?: string | null;
  retracted_reason?: string | null;
}

/** The five model-authored prose fields, in reading order, each with the payload
 *  path the server's `authorship.modelAuthored` names it by.
 *
 *  ORDER IS EDITORIAL, MEMBERSHIP IS NOT. Which of the five a given insight
 *  actually carries is the server's answer (`modelAuthored`), never this list:
 *  a field absent from the block draws no marker, and a field the server names
 *  and this list forgot would be drawn as evidence — which is the defect. */
export const MODEL_AUTHORED_PROSE = [
  { field: "insight.summary", label: "Summary" },
  { field: "insight.whyItMatters", label: "Why it matters" },
  { field: "insight.recommendedAction", label: "Recommended action" },
  { field: "insight.limitations", label: "Limitations" },
] as const;

/** One prose field as a string, whatever shape it has on the wire. `limitations`
 *  is an array; joining it here keeps the renderer to one branch. */
export function proseText(insight: DailyInsightRow["payload"], field: string): string {
  const editorial = insight?.insight ?? {};
  const key = field.replace(/^insight\./, "") as keyof typeof editorial;
  const value = editorial[key];
  if (Array.isArray(value)) return value.filter(Boolean).join(" · ");
  return typeof value === "string" ? value.trim() : "";
}

export type InsightShareState =
  | { kind: "shareable"; resultId: string; sentence: string }
  | { kind: "not_shareable"; reason: string; gesture: string }
  | { kind: "unrecorded"; sentence: string };

/** What the door does, said before it is opened: the person is about to leave
 *  this page, and a control that does not say where it goes is a surprise. */
export const SHAREABLE_SENTENCE =
  "A share link is created over a frozen view of the result this insight was " +
  "published from — the same sharing mechanism as every other reading in " +
  "toorow. Open the result to freeze the view you want to send, and create the " +
  "link there.";

export const OPEN_RESULT_LABEL = "Open this insight's result";

/** The reason itself comes from the server and names the missing link; what this
 *  adds is the only thing the server cannot know — that the insight was
 *  published anyway, and that repairing it changes the NEXT publication. */
export const NOT_SHAREABLE_GESTURE =
  "Publishing never waited for this, so the insight itself is here either way. " +
  "Repair what the sentence above names, and the next publication of this card " +
  "can be shared.";

export const UNRECORDED_SENTENCE =
  "This insight was published before toorow recorded what a card was measured " +
  "from, so nothing here can say whether it can be shared. The next run records it.";

/**
 * Which of the three states one insight row is in. Pure, and it never guesses:
 * an absent reason on a row with no Result is state 3, not a refusal.
 */
export function insightShareState(insight: DailyInsightRow): InsightShareState {
  const resultId = (insight.result_id ?? "").trim();
  if (resultId) return { kind: "shareable", resultId, sentence: SHAREABLE_SENTENCE };
  const reason = (insight.result_unavailable_reason ?? "").trim();
  if (reason) return { kind: "not_shareable", reason, gesture: NOT_SHAREABLE_GESTURE };
  return { kind: "unrecorded", sentence: UNRECORDED_SENTENCE };
}

/** The title the model authored, or the slot when it authored none. Never a
 *  fabricated headline: the slot is a fact, an invented title is not. */
export function insightHeading(insight: DailyInsightRow): string {
  const title = (insight.payload?.insight?.title ?? "").trim();
  if (title) return title;
  return typeof insight.slot === "number" ? `Insight ${insight.slot + 1}` : "Insight";
}

/** What one day's read hands back: the rows, and whether THIS caller may withdraw
 *  one of them. The permission is the server's answer, never a guess here: a
 *  control drawn on a guess either hides the gesture from someone entitled to it
 *  or offers one that answers 404. Absent (an older server) reads as `false` --
 *  the honest default for a write. */
export interface RunInsightsRead {
  insights: DailyInsightRow[];
  canRetract: boolean;
}

/**
 * The published insights of one day, read through the seam.
 *
 * A 200 that carries no `insights` array is an ERROR, not an empty day: the
 * panel below would otherwise say "this run published nothing" on a version skew.
 */
export function fetchRunInsights(
  projectId: string,
  insightDate: string,
  init?: RequestInit,
): Promise<RunInsightsRead> {
  return apiGet<{ insights?: DailyInsightRow[]; canRetract?: boolean }>(
    `/api/daily-insights/runs/${encodeURIComponent(insightDate)}` +
      `?project_id=${encodeURIComponent(projectId)}`,
    init,
  ).then((body) => {
    if (!body || !Array.isArray(body.insights)) {
      throw new Error("The response carries no insights. Nothing was read.");
    }
    return { insights: body.insights, canRetract: body.canRetract === true };
  });
}

/** Withdraw one published claim. POST, and there is no DELETE anywhere on this
 *  path: `proactive-assertions.md` decision 4 makes a retraction an AUDITED STATE
 *  TRANSITION, because the row is the evidence that the claim was made. */
export function retractInsight(
  projectId: string,
  insightId: string,
  reason: string,
): Promise<DailyInsightRow> {
  return apiPost<DailyInsightRow>(
    `/api/daily-insights/insights/${encodeURIComponent(insightId)}/retract` +
      `?project_id=${encodeURIComponent(projectId)}`,
    { reason },
  );
}

export const RETRACT_LABEL = "Retract this insight";

/** Said before the control is used, because the person cannot undo it afterwards
 *  and nothing on the next screen would tell them so. */
export const RETRACT_WARNING =
  "The insight stays here, shown as withdrawn with your reason — it is the record " +
  "that the claim was made. A retraction cannot be undone: to say something else, " +
  "publish it as a new insight of the same day.";

export const RETRACT_REASON_REQUIRED =
  "Say why this insight is withdrawn. Without a reason, the next reader cannot tell " +
  "a wrong insight from an inconvenient one.";

/** What a withdrawn row reads as. Who, when and why -- the three the database
 *  refuses to separate, said in the same breath. */
export function retractionSentence(insight: DailyInsightRow): string | null {
  const at = (insight.retracted_at ?? "").trim();
  if (!at) return null;
  const who = (insight.retracted_by ?? "").trim() || "someone in this project";
  const why = (insight.retracted_reason ?? "").trim();
  const when = at.slice(0, 10);
  return why
    ? `Withdrawn by ${who} on ${when}: ${why}`
    : `Withdrawn by ${who} on ${when}.`;
}

/**
 * One day's insights and, on each, what it takes to share it.
 *
 * ON DEMAND. A fortnight of runs on one screen would mean fourteen reads on
 * mount for a day most visits never open, and the button says what it will do.
 */
export function RunInsights({
  projectId,
  insightDate,
  onOpenResult,
}: {
  projectId: string;
  insightDate: string;
  /** Absent when this surface is mounted without a way to navigate. The
   *  sentence still shows; the control does not, because a button that opens
   *  nothing is worse than no button. */
  onOpenResult?: (resultId: string) => void;
}) {
  const [state, setState] = useState<
    | { status: "idle" }
    | { status: "loading" }
    | { status: "ready"; insights: DailyInsightRow[]; canRetract: boolean }
    | { status: "failed"; message: string }
  >({ status: "idle" });
  /** Which row has its withdrawal form open, and what the person has typed. One
   *  at a time: a retraction is not a bulk gesture. */
  const [draft, setDraft] = useState<{ id: string; reason: string } | null>(null);
  const [refusal, setRefusal] = useState<{ id: string; message: string } | null>(null);

  if (state.status === "idle") {
    return (
      <Button
        type="button"
        variant="ghost"
        data-testid={`daily-insight-open-${insightDate}`}
        onClick={() => {
          setState({ status: "loading" });
          fetchRunInsights(projectId, insightDate)
            .then((read) =>
              setState({
                status: "ready",
                insights: read.insights,
                canRetract: read.canRetract,
              }),
            )
            .catch((reason: unknown) =>
              setState({
                status: "failed",
                message:
                  reason instanceof Error
                    ? reason.message
                    : "This day's insights could not be read",
              }),
            );
        }}
      >
        Show this day&apos;s insights
      </Button>
    );
  }

  if (state.status === "loading") {
    return <p className="m-0 text-caption text-text-secondary">Reading this day&apos;s insights…</p>;
  }

  if (state.status === "failed") {
    /* A day that could not be READ is not a day that published NOTHING. */
    return (
      <Status tone="warning" data-testid={`daily-insight-items-error-${insightDate}`}>
        {state.message}
      </Status>
    );
  }

  if (state.insights.length === 0) {
    return (
      <p className="m-0 text-caption text-text-secondary">
        This run recorded no insight for this day.
      </p>
    );
  }

  return (
    <ul
      className="m-0 mt-2 grid list-none gap-3 p-0"
      data-testid={`daily-insight-items-${insightDate}`}
    >
      {state.insights.map((insight) => {
        const share = insightShareState(insight);
        const withdrawn = retractionSentence(insight);
        const authorship = insight.payload?.authorship ?? null;
        const declaredWord = insight.payload?.insight?.confidence ?? null;
        const derived = derivedConfidenceReading(authorship, declaredWord);
        const declared = declaredConfidenceReading(authorship, declaredWord);
        return (
          <li key={insight.id} data-testid={`insight-share-${insight.id}`}>
            {/* EVERY MODEL-AUTHORED FIELD CARRIES THE MARKER, and nothing else does.
                `proactive-assertions.md` ("Incomplete if"): model-authored prose is
                not visibly distinguishable from cited server data. The heading, the
                four prose blocks and the model's own confidence word are the
                model's; the derived confidence, the terms it is built from and the
                share sentences below are the server's, and they stay bare. */}
            <p className="m-0 text-ui text-text">
              <ModelAuthored authorship={authorship} field="insight.title">
                {insightHeading(insight)}
              </ModelAuthored>
            </p>
            {/* A WITHDRAWN CLAIM DOES NOT DISAPPEAR FROM THE DAY IT WAS MADE ON.
                `proactive-assertions.md` decision 4 makes the retraction an audited
                state transition precisely so a reader can tell "never said" from
                "said, and later withdrawn". Hiding the row here would deliver the
                delete that decision refuses, one layer above the database. It is
                drawn FIRST, above the prose, because everything below it is a claim
                that no longer stands. */}
            {withdrawn ? (
              <Status
                as="block"
                tone="warning"
                title="Withdrawn"
                data-testid={`insight-retracted-${insight.id}`}
              >
                <p className="m-0">{withdrawn}</p>
              </Status>
            ) : null}
            {MODEL_AUTHORED_PROSE.map(({ field, label }) => {
              const text = proseText(insight.payload, field);
              if (!text) return null;
              return (
                <p key={field} className="m-0 mt-1 text-caption text-text-secondary">
                  <span className="text-text">{label}: </span>
                  <ModelAuthored authorship={authorship} field={field}>
                    {text}
                  </ModelAuthored>
                </p>
              );
            })}
            {derived ? (
              <p
                className="m-0 mt-1 text-caption text-text-secondary"
                data-testid={`insight-confidence-derived-${insight.id}`}
              >
                Confidence: {derived}
              </p>
            ) : null}
            {declared ? (
              <p
                className="m-0 text-caption text-text-secondary"
                data-testid={`insight-confidence-declared-${insight.id}`}
              >
                Model&apos;s own estimate:{" "}
                <ModelAuthored authorship={authorship} field="insight.confidence" authored>
                  {declared}
                </ModelAuthored>
              </p>
            ) : null}
            {share.kind === "shareable" ? (
              <Stack>
                <p className="m-0 text-caption text-text-secondary">{share.sentence}</p>
                {onOpenResult ? (
                  <div>
                    <Button
                      type="button"
                      variant="ghost"
                      data-testid={`insight-open-result-${insight.id}`}
                      onClick={() => onOpenResult(share.resultId)}
                    >
                      {OPEN_RESULT_LABEL}
                    </Button>
                  </div>
                ) : null}
              </Stack>
            ) : null}
            {share.kind === "not_shareable" ? (
              /* INFO, not error: the insight published, and it is worth reading.
                 What the refusal cost is the link, and only the link. */
              <Status
                as="block"
                tone="info"
                title="Not shareable"
                data-testid={`insight-not-shareable-${insight.id}`}
              >
                <Stack>
                  <p className="m-0">{share.reason}</p>
                  <p className="m-0 text-caption">{share.gesture}</p>
                </Stack>
              </Status>
            ) : null}
            {share.kind === "unrecorded" ? (
              <p
                className="m-0 text-caption text-text-secondary"
                data-testid={`insight-share-unrecorded-${insight.id}`}
              >
                {share.sentence}
              </p>
            ) : null}
            {/* THE GESTURE. Drawn only where the server said this caller may make
                it, and only on a claim that still stands -- a retracted row has no
                second withdrawal, and the door would answer 409. */}
            {!withdrawn && state.canRetract ? (
              draft?.id === insight.id ? (
                <Stack>
                  <p className="m-0 text-caption text-text-secondary">{RETRACT_WARNING}</p>
                  <label
                    className="text-caption text-text-secondary"
                    htmlFor={`retract-reason-${insight.id}`}
                  >
                    Why is this insight withdrawn?
                  </label>
                  <textarea
                    id={`retract-reason-${insight.id}`}
                    className="w-full rounded-control border border-divider-base p-2 text-ui text-text"
                    rows={2}
                    value={draft.reason}
                    data-testid={`insight-retract-reason-${insight.id}`}
                    onChange={(event) => setDraft({ id: insight.id, reason: event.target.value })}
                  />
                  {refusal?.id === insight.id ? (
                    <Status tone="warning" data-testid={`insight-retract-refused-${insight.id}`}>
                      {refusal.message}
                    </Status>
                  ) : null}
                  <div className="flex gap-2">
                    <Button
                      type="button"
                      data-testid={`insight-retract-confirm-${insight.id}`}
                      onClick={() => {
                        const typed = draft.reason.trim();
                        if (!typed) {
                          setRefusal({ id: insight.id, message: RETRACT_REASON_REQUIRED });
                          return;
                        }
                        setRefusal(null);
                        retractInsight(projectId, insight.id, typed)
                          .then((row) => {
                            setDraft(null);
                            setState((previous) =>
                              previous.status === "ready"
                                ? {
                                    ...previous,
                                    insights: previous.insights.map((item) =>
                                      item.id === row.id ? { ...item, ...row } : item,
                                    ),
                                  }
                                : previous,
                            );
                          })
                          .catch((error: unknown) =>
                            setRefusal({
                              id: insight.id,
                              message:
                                error instanceof Error
                                  ? error.message
                                  : "This insight could not be withdrawn.",
                            }),
                          );
                      }}
                    >
                      Confirm withdrawal
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      data-testid={`insight-retract-cancel-${insight.id}`}
                      onClick={() => {
                        setDraft(null);
                        setRefusal(null);
                      }}
                    >
                      Keep it published
                    </Button>
                  </div>
                </Stack>
              ) : (
                <Button
                  type="button"
                  variant="ghost"
                  data-testid={`insight-retract-${insight.id}`}
                  onClick={() => setDraft({ id: insight.id, reason: "" })}
                >
                  {RETRACT_LABEL}
                </Button>
              )
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

export default RunInsights;
