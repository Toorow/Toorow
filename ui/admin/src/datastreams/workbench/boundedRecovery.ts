/**
 * The bounded-recovery ceremony — prepare, read, confirm — written once.
 *
 * AI-144. Two surfaces now need it: `DatastreamRecoveryDialog`, which recovers
 * the scope of ONE selected run, and `DatastreamReloadPanel`, which reloads a
 * range a person chooses. The ceremony is identical in both and must stay so:
 * `POST /bounded/prepare` freezes an immutable proposal and dispatches NOTHING,
 * the operator reads the frozen scope, and `POST /bounded/confirm` turns exactly
 * that proposal into one durable operation.
 *
 * Copying those thirty lines into the second surface is how this console ended
 * up with one object under several names — so they live here instead.
 *
 * WHAT THIS DELIBERATELY DOES NOT DO: decide. It carries no verb, no interval
 * and no default. The caller states the scope; this module only guarantees that
 * a preparation is read before it is confirmed, and that an uncertain outcome is
 * never reported as a success.
 */
import { useCallback, useState } from "react";
import { apiFetch } from "../../lib/apiFetch";
import { record, text, type EvidenceRecord } from "./evidence";
import { RUN_ORIGINS, type RunOriginEntry } from "./runOrigins";

/**
 * WHERE A VERB STANDS, and the three answers are not interchangeable.
 *
 * `covered` — the product performs this work, under another name, through a
 * gesture that is delivered, bounded and costed. The verb is retired: a second
 * door onto the same collection is not a feature, it is a second scope model and
 * a second confirmation, one keystroke from drifting apart from the first.
 *
 * `planned` — the product intends to have it and does not. Nothing else covers
 * it, so the refusal stays on screen with its reason: a person cannot ask for
 * what they cannot see, and an absent verb reads as a product that never thought
 * of it.
 */
export type RecoveryStanding = "covered" | "planned";

export interface RecoveryVerb {
  /** The registry entry — the label and the provider question come from it. */
  readonly origin: RunOriginEntry;
  /** What the verb would do, in the words of the person asking for it. */
  readonly does: string;
  /** Retired because something else does it, or coming because nothing does. */
  readonly standing: RecoveryStanding;
  /**
   * Where the delivered gesture lives (`covered`), or why this one is missing
   * and what stands in for it meanwhile (`planned`). Never a deferral: it is
   * the only line that leaves a person somewhere to go.
   */
  readonly instead: string;
}

/**
 * WHY THE COPY IS KEYED ON THE REGISTRY AND NOT LISTED BESIDE IT.
 *
 * `runOrigins.ts` is compared entry by entry against the Python registry, and
 * that registry is what measures whether anything in this build would carry an
 * execution of a given origin to an end. Retyping the three verbs here would
 * create a second list with a second opinion, and the day an engine lands the
 * screen would keep refusing a verb the server had started granting. So the
 * words live here, the FACTS live there, and `unbuiltRecoveryVerbs()` joins
 * them — a verb whose engine appears simply leaves this panel on its own.
 */
const VERB_COPY: Record<string, { does: string; standing: RecoveryStanding; instead: string }> = {
  bounded_synchronize: {
    does: "Collect days this Datastream has never collected.",
    standing: "covered",
    instead:
      "The schedule already collects new days on its own, and the days it missed " +
      "are the empty marks on Day-by-day coverage, just above: pick them there " +
      "and collect them. This verb is retired rather than planned — it would be " +
      "a second way to ask for a collection the product already performs.",
  },
  bounded_reload: {
    does: "Collect days again that came back wrong, empty or incomplete.",
    standing: "covered",
    instead:
      "Day-by-day coverage, just above, does exactly this: pick the days that " +
      "failed and collect them again. Its confirmation names the connector, the " +
      "source account and every day before anything is spent. This verb is " +
      "retired for the same reason as the one above it.",
  },
  bounded_reprocess: {
    does:
      "Apply a chosen mapping again to data already kept, without asking the " +
      "source for it a second time.",
    standing: "planned",
    instead:
      "Nothing in the product does this yet, and it is the only one of the three " +
      "that is a real gap. A run that collected correctly and then failed while " +
      "mapping can be repaired today only by collecting those days again and " +
      "paying the source for rows it already delivered. The nearest gesture is " +
      "changing the mapping on Mapping, which rebuilds from data already kept — " +
      "but only when the mapping actually changes, which is not this case.",
  },
};

/**
 * The one sentence about money, derived rather than typed per verb.
 *
 * A person deciding between three refusals is deciding about spend before
 * anything else, so it is stated in full on each — never as a badge that only
 * one of them carries.
 */
export function verbCost(origin: RunOriginEntry): string {
  return origin.reads_provider_windows
    ? "Would call the source and spend on the account it reads."
    : "Would call nothing and spend nothing: it reads data the product already keeps.";
}

/**
 * The bounded verbs this build cannot grant, in registry order.
 *
 * Derived from `has_engine`, which is the server's measurement of its own build.
 * An engine that lands flips the registry, the conformance test holds the mirror
 * in step, and the verb disappears from every surface that calls this — no
 * second edit to remember.
 */
export function unbuiltRecoveryVerbs(): RecoveryVerb[] {
  return RUN_ORIGINS.filter((origin) => !origin.has_engine && VERB_COPY[origin.key]).map(
    (origin) => ({ origin, ...VERB_COPY[origin.key] }),
  );
}

/** A refusal that names its reason beats a generic failure — the server's
 *  bounded-recovery errors each say what to do (an over-wide interval, a
 *  missing retention, an ineligible target). */
export async function boundedJson(response: Response, fallback: string): Promise<EvidenceRecord> {
  const value = await response.json().catch(() => null);
  const parsed = record(value);
  if (!response.ok || !parsed) {
    throw new Error(typeof parsed?.message === "string" ? parsed.message : fallback);
  }
  return parsed;
}

/** What a confirmation actually did. `unknown` is NOT a failure and NOT a
 *  success: the durable outcome could not be established, and the only correct
 *  next action is to reconcile — never a blind retry. */
export type ConfirmOutcome = "completed" | "unknown" | "failed";

export interface Outcome {
  text: string;
  tone: "success" | "error";
}

export interface BoundedRecovery {
  /** The frozen proposal, once prepared. Null until then, and after a confirm. */
  preparation: EvidenceRecord | null;
  busy: boolean;
  message: Outcome | null;
  setMessage: (message: Outcome | null) => void;
  /** Freeze a proposal. `body` carries the verb and the scope; `project_id` is added. */
  prepare: (body: Record<string, unknown>) => Promise<boolean>;
  /** Turn the frozen proposal into exactly one durable operation. */
  confirm: () => Promise<ConfirmOutcome>;
  reset: () => void;
}

export function useBoundedRecovery(projectId: string, datastreamId: string): BoundedRecovery {
  const [preparation, setPreparation] = useState<EvidenceRecord | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<Outcome | null>(null);
  const base = `/api/datastreams/${encodeURIComponent(datastreamId)}/bounded`;

  const prepare = useCallback(
    async (body: Record<string, unknown>) => {
      setBusy(true);
      setMessage(null);
      try {
        const response = await apiFetch(`${base}/prepare`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ project_id: projectId, ...body }),
        });
        setPreparation(await boundedJson(response, "Recovery review could not be prepared."));
        return true;
      } catch (reason) {
        setMessage({
          text: reason instanceof Error ? reason.message : "Recovery review could not be prepared.",
          tone: "error",
        });
        return false;
      } finally {
        setBusy(false);
      }
    },
    [base, projectId],
  );

  const confirm = useCallback(async (): Promise<ConfirmOutcome> => {
    if (!preparation) return "failed";
    setBusy(true);
    try {
      const response = await apiFetch(`${base}/confirm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: projectId, preparation_id: preparation.preparation_id }),
      });
      const result = await boundedJson(response, "Recovery confirmation failed.");
      if (result.outcome === "outcome_unknown") {
        // The preparation is KEPT: the operator has something to reconcile
        // against. Clearing it here would erase the only handle on the doubt.
        setMessage({
          text: "The durable outcome is unknown. Reconcile before taking another action.",
          tone: "error",
        });
        return "unknown";
      }
      setMessage({
        text: `Operation ${text(result.operation_id)} completed.`,
        tone: "success",
      });
      setPreparation(null);
      return "completed";
    } catch (reason) {
      setMessage({
        text:
          reason instanceof Error
            ? reason.message
            : "The outcome is unknown. Reconcile before retrying.",
        tone: "error",
      });
      return "failed";
    } finally {
      setBusy(false);
    }
  }, [base, projectId, preparation]);

  const reset = useCallback(() => {
    setPreparation(null);
    setMessage(null);
  }, []);

  return { preparation, busy, message, setMessage, prepare, confirm, reset };
}
