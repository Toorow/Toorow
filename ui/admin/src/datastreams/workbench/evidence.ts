import { formatNumber } from "../../ui/format";
import { formatTimestamp } from "../../ui/Timestamp";

export type EvidenceRecord = Record<string, unknown>;

export function record(value: unknown): EvidenceRecord | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as EvidenceRecord
    : null;
}

/**
 * The record ONLY when it actually carries something.
 *
 * `record({})` returns `{}`, which is truthy — so
 * `record(x) ? <EvidenceRows source={x}/> : <Status>not persisted</Status>`
 * took the first branch for an empty object, and `EvidenceRows` returns `null`
 * when it has no rows. The result was silent blank space exactly where the
 * honest "no evidence was persisted for this stage" belonged. Two independent
 * review lenses found it on the same file:line on 2026-08-03.
 *
 * `record()` is kept as it is: `{}` is a legitimate VALUE in places (a
 * capability's `impact: {}` means "no impact", which is information). What was
 * wrong was using a type check to answer a presence question. This answers the
 * presence question.
 */
export function filledRecord(value: unknown): EvidenceRecord | null {
  const asRecord = record(value);
  return asRecord && Object.keys(asRecord).length > 0 ? asRecord : null;
}

export function records(value: unknown): EvidenceRecord[] {
  return Array.isArray(value) ? value.map(record).filter((item): item is EvidenceRecord => item !== null) : [];
}

export function text(value: unknown, fallback = "Unavailable"): string {
  return typeof value === "string" && value.trim() ? value : fallback;
}

export function nullableText(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

export function numberText(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value)
    ? formatNumber(value)
    : "Unavailable";
}

/**
 * ONE instant, ONE rendering — `Timestamp.tsx`, and no longer a sixth spelling
 * of it. This function held `02 Sep 2026, 14:05`, which named no zone: two
 * people reading two screens could not tell whether the evidence disagreed or
 * the formatting did. `Unavailable` stays this file's word for an absence,
 * because every other reader here answers `Unavailable` too.
 */
export function dateTime(value: unknown): string {
  if (typeof value !== "string" || !value) return "Unavailable";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "Unavailable" : formatTimestamp(parsed);
}

export function titleCase(value: unknown): string {
  return text(value, "Unknown").replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

/**
 * An amount and the currency it is in, or the reason it cannot be one — 61.4.
 *
 * IT REFUSES BEFORE IT FORMATS, and that order is the whole point. The
 * `Placements` tab drew a line's budget and a campaign's spend side by side with
 * no currency on either, and the two were in different scales: the budget in
 * `media_plans.currency`, the spend already converted into the Project's. A
 * number whose currency nobody can name is not a smaller truth than a labelled
 * one — it is a different statement, and printing it bare invites the reader to
 * supply the missing half themselves.
 *
 * `currency` null means the server could not name one, so nothing is printed but
 * the absence. It is NEVER `0`, and never a bare figure with the code implied.
 */
export function moneyText(
  value: unknown,
  currency: string | null,
  absent = "No amount",
): string {
  const amount =
    typeof value === "number" && Number.isFinite(value)
      ? formatNumber(value)
      : typeof value === "string" && value.trim()
        ? value.trim()
        : null;
  if (amount === null) return absent;
  if (!currency) return "Amount in an unnamed currency";
  return `${amount} ${currency}`;
}
