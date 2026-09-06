/**
 * Which words on a proactive surface the MODEL wrote, and which the server measured.
 *
 * `proactive-assertions.md` ("Incomplete if"): *model-authored prose is not
 * visibly distinguishable from cited server data*. Decision 1 of the same page
 * grants free internal publication and makes exactly one thing mandatory in
 * exchange — "what stays mandatory is that the model-authored prose is
 * **visibly distinguishable** from cited server data".
 *
 * Story 53.4 built the contract and stopped there: every published payload
 * carries an `authorship` block naming its model-authored fields, and
 * `grep -rn modelAuthored ui/admin/src` found two hits, one of them a TypeScript
 * type. Nothing DREW it. A thousand characters of model prose sat in the same
 * heading style as a figure the warehouse produced.
 *
 * ONE MARKER, AND IT IS NOT AN ALERT. `CLAUDE.md`: one language of alert per
 * product. Model authorship is not a state — the insight is not degraded for
 * having been written by a model, it is the whole point — so it is drawn with
 * the primitive the design system reserves for exactly that: `Badge outline`,
 * documented in `components/ui/badge.tsx` as "a label that names a thing rather
 * than a state". Tones stay free for health, and a reader never has to wonder
 * whether the chip beside a headline means something is wrong.
 *
 * THE LIST OF FIELDS IS NEVER MAINTAINED HERE. It is `authorship.modelAuthored`,
 * derived server-side by `daily_insights_schema.authorship_block` from the
 * payload itself. A component holding its own copy is how the two drift, and
 * drift on this list means prose drawn as evidence.
 */
import type { ReactNode } from "react";

import { Badge } from "../ui";

/** What the chip says. One string, so two surfaces cannot label it differently. */
export const MODEL_AUTHORED_LABEL = "Model-authored";

/** The `authorship` envelope as every read path serves it (`authorship_of`). */
export interface InsightAuthorship {
  modelAuthored?: string[];
  /** Always `"derived"` since this lot: where the confidence a reader sees comes from. */
  confidence?: string;
  /** The level the MODEL declared about its own claim. Kept, labelled, and it
   *  drives no reading — see `derivedConfidenceReading`. */
  declaredConfidence?: string | null;
  derivedConfidence?: DerivedConfidence | null;
  evidenceRefs?: string[];
}

/** The server's measurement, as `core.insight_confidence` produces it. */
export interface DerivedConfidence {
  reading: string;
  reason?: string | null;
  terms?: { completeness?: number | null; freshness?: number | null; provenance?: number | null };
  limitingTerm?: string | null;
  unknownTerms?: string[];
  citedMembers?: string[];
  unresolvedRefs?: string[];
}

/** Does the server say this payload field is model-authored?
 *
 *  `false` for an absent block rather than `true`: a surface must not brand a
 *  server figure as prose because an envelope predates the contract. The
 *  confidence line says the authorship is unstated instead — that disclosure
 *  belongs to one sentence, not to every chip on the card. */
export function isModelAuthored(
  authorship: InsightAuthorship | null | undefined,
  field: string,
): boolean {
  return Boolean(authorship?.modelAuthored?.includes(field));
}

/**
 * Draw one field with the marker when the server says the model wrote it.
 *
 * The marker is ADDITIVE: the field renders identically either way, plus a chip.
 * Nothing is hidden and nothing is restyled, so the distinction cannot be read
 * as a downgrade of the prose.
 */
export function ModelAuthored({
  authorship,
  field,
  authored,
  children,
}: {
  authorship: InsightAuthorship | null | undefined;
  /** The payload path, e.g. `insight.summary` — the same names the server derives. */
  field: string;
  /** Escape hatch for a value the server has ALREADY labelled as the model's, and
   *  which `modelAuthored` therefore never names: `authorship.declaredConfidence`
   *  is by definition the model's own word, but it is not prose, so it is not on
   *  `MODEL_AUTHORED_FIELDS`. Never a way to guess: pass `true` only where the
   *  server states the authorship in the field's own name. */
  authored?: boolean;
  children: ReactNode;
}) {
  if (!(authored ?? isModelAuthored(authorship, field))) return <>{children}</>;
  return (
    <span className="inline-flex flex-wrap items-baseline gap-2">
      {children}
      <Badge outline data-slot="model-authored" data-model-authored={field}>
        {MODEL_AUTHORED_LABEL}
      </Badge>
    </span>
  );
}

function termsText(derived: DerivedConfidence): string {
  const terms = derived.terms ?? {};
  const parts = (["completeness", "freshness", "provenance"] as const)
    .map((name) => {
      const value = terms[name];
      return `${name} ${typeof value === "number" ? value : "unmeasured"}`;
    })
    .join(", ");
  return parts;
}

/**
 * The confidence sentence a person reads, from the SERVER's measurement.
 *
 * Never from `insight.confidence`: that word is the model's own estimate of its
 * own claim, which `proactive-assertions.md` refuses as a confidence level at
 * all. It is still said — under `declaredConfidence`, named as what it is — but
 * it never decides the reading, and it never fills in for a missing one.
 *
 * `null` when the signal carries no confidence question at all (a render, a
 * governed alert): a line saying "unmeasurable" on an object that never claimed
 * a confidence is noise, and noise is how a real disclosure stops being read.
 */
export function derivedConfidenceReading(
  authorship: InsightAuthorship | null | undefined,
  declaredFallback?: string | null,
): string | null {
  const derived = authorship?.derivedConfidence;
  if (derived && typeof derived.reading === "string") {
    if (derived.reading === "unmeasurable") {
      return `Unmeasurable — ${derived.reason ?? "the server could not measure this insight's cited evidence."}`;
    }
    const limiter = derived.limitingTerm
      ? `, limited by ${derived.limitingTerm}`
      : "";
    return `${derived.reading} — derived by the server from the evidence this insight cites${limiter} (${termsText(derived)}).`;
  }
  const declared = (declaredFallback ?? authorship?.declaredConfidence ?? "").trim();
  if (!declared) return null;
  /* An insight published before the server derived confidence. It is not a
     low-confidence insight — nobody measured it — and the model's word is
     reported as the model's word rather than promoted into the gap. */
  return (
    "Unmeasurable — this insight was published before toorow derived confidence " +
    `from cited evidence. The model declared “${declared}”, which is not evidence.`
  );
}

/** The model's own estimate, said as such. Drawn beside the derived reading, never
 *  instead of it, and only when the model actually declared one. */
export function declaredConfidenceReading(
  authorship: InsightAuthorship | null | undefined,
  declaredFallback?: string | null,
): string | null {
  const declared = (authorship?.declaredConfidence ?? declaredFallback ?? "").trim();
  if (!declared) return null;
  return `${declared} — the model's own estimate of its claim; it decides nothing here.`;
}
