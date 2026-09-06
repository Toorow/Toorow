/**
 * ONE set of sentences for the Master Data authority's refusals.
 *
 * The authority refuses an identity command under codes that mean different
 * things and call for different gestures, and every screen that runs those
 * commands has to say the same thing about the same code. Until this module
 * existed the sentences lived in `ContextHubLayout.tsx` alone, so the second
 * caller — the Governance ▸ Master Data workbench — had the choice between
 * copying them and inventing a second wording for one refusal. Both are the
 * same defect: one product, one alert language.
 *
 * WHAT IS DELIBERATELY NOT COLLAPSED. The four named codes are shown AS THE
 * SERVER WROTE THEM, because each already names its own repair — converge this
 * organization, choose another short code, release these consumers, use the
 * authority instead of the closed legacy door. Rewriting them here would put a
 * second author between the authority and the person doing the work.
 *
 * A stale-version refusal is the opposite case: nothing in it names a gesture,
 * and it is the only one this module writes a sentence for, because it is the one
 * a screen would otherwise report as "could not be saved" — which sends the
 * curator back to press Save on the same stale edit. Both doors also read
 * `isStaleVersionRefusal` to STOP: the sentence tells the person to reload, and a
 * button that still re-POSTs contradicts it.
 */
import { ApiError } from "../lib/apiFetch";

/**
 * The codes the authority raises with a repair already named in the message.
 * `master_data_command_refused` is the impact refusal and carries the consumers
 * with it; the other three come from the creation door and the closed legacy
 * one.
 */
export const MASTER_DATA_NAMED_REFUSAL_CODES: readonly string[] = [
  "legacy_store_is_read_only",
  "master_data_organization_not_converged",
  "master_data_short_code_taken",
  "master_data_command_refused",
];

/**
 * The authority's name for "the object moved under you". It is the code
 * `context_api` has always refused a stale topic or procedure edit with, reused
 * rather than invented, so one meaning has one word across the product.
 *
 * The rename carries it since `governance.md`'s amendment of 2026-08-30: a
 * rename states the version identity it read, and this is the answer when the
 * identity has been revised since.
 */
export const MASTER_DATA_STALE_VERSION_CODE = "version_conflict";

/** True when the authority refused because the object was revised since the door
 *  read it. The door must then STOP: pressing Save again sends the same stale
 *  base, and the only repair is to reload and read what the other person wrote. */
export function isStaleVersionRefusal(error: unknown): boolean {
  return (
    error instanceof ApiError
    && (error.code === MASTER_DATA_STALE_VERSION_CODE || error.status === 409)
    && !MASTER_DATA_NAMED_REFUSAL_CODES.includes(error.code)
  );
}

/** A refusal to OVERWRITE, not a failure to save, and the two call for opposite
 *  moves. Named `version_conflict` by the authority; a bare 409 that named no
 *  repair at all reads the same way, because it means the same thing. */
export const STALE_VERSION_SENTENCE =
  "Someone else changed this entry while this dialog was open, so nothing was "
  + "overwritten and nothing you typed was lost. Close this dialog and reopen "
  + "the entry to see their version before applying your change on top of it.";

/** The fail-closed 503: the guard could not read what it needs to decide.
 *  "I could not check" must never render as "done". */
export const EVIDENCE_UNREADABLE_SENTENCE =
  "The evidence needed to decide could not be read, so nothing was changed.";

/** Every governed change on this authority carries a reason into the audit
 *  trail, and every screen asks for it in the same words. */
export const MISSING_REASON_SENTENCE =
  "Add a reason so the governance change remains auditable.";

/** One consumer the authority named in an impact refusal. */
export interface RefusedConsumer {
  consumer_id?: string;
  consumer_kind?: string;
  consumer_label?: string;
}

/** True when this refusal is the archive guard naming what would break. */
export function isImpactRefusal(error: unknown): boolean {
  return error instanceof ApiError && error.code === "master_data_command_refused";
}

/** The consumers the authority named, in the shape it named them. Never
 *  invented: an impact refusal with an unreadable list is still a refusal, and
 *  an empty array here means the server sent none. */
export function refusedConsumers(error: unknown): RefusedConsumer[] {
  if (!(error instanceof ApiError)) return [];
  const body = error.body as { impact?: { consumers?: unknown } } | null | undefined;
  const consumers = body?.impact?.consumers;
  return Array.isArray(consumers) ? (consumers as RefusedConsumer[]) : [];
}

/**
 * The sentence to show for a failed Master Data command.
 *
 * A named code answers with the authority's own message; a bare 409 answers
 * with the stale-version sentence; anything else answers with what the error
 * carried, and `fallback` only where it carried nothing.
 */
export function masterDataRefusalSentence(
  error: unknown,
  fallback = "The change could not be saved.",
): string {
  if (error instanceof ApiError && MASTER_DATA_NAMED_REFUSAL_CODES.includes(error.code)) {
    return error.message;
  }
  if (isStaleVersionRefusal(error)) {
    return STALE_VERSION_SENTENCE;
  }
  return error instanceof Error ? error.message : fallback;
}
