/**
 * What the Mapping tab READS off a mapping version, and what it PROPOSES back.
 *
 * Extracted from `WorkbenchMappingPage.tsx` (2026-08-11) for one reason: that
 * file was 974 lines against a 1 000-line ceiling, and three amendments of
 * `datastream-workbench-and-wizard.md` land on it at once. Nothing here changed
 * meaning in the move except `withTreatments`, which now also carries the
 * canonical/MDM binding the screen could read and never write.
 */
import type { EvidenceRecord } from "../evidence";
import { record, records, text } from "../evidence";
import { stateTone, type Tone } from "../../../ui";

/**
 * Binding status decides the tone, AND THE UNION DECIDES THE TONE (76-2).
 *
 * The private map here drew `blocked` and `blocking` as errors -- the fourth
 * place in the console to disagree about `blocked`, after Governance's withdrawn
 * override. The arbitration stands: a blocked binding is repairable, which is
 * what `warning` means; `error` is reserved for what cannot be used at all.
 * `bound`, `complete` and `blocking` are declared words now, so a reader can see
 * this tab's whole vocabulary in one file. `confidence` still comes from the
 * compiler, not from here.
 */
export function bindingTone(status: string): Tone {
  return stateTone(status);
}

export function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.map((entry) => String(entry)) : [];
}

/**
 * The four readings of a field binding, and the triage they make possible.
 *
 * A source report brings tens to hundreds of physical fields, and the table
 * listed every one of them in wire order — the raw list the target calls out.
 * The operator's question is never "show me all 180 fields"; it is "which ones
 * stop me, and which ones reach governance". Neither was answerable without
 * reading the whole table.
 *
 * `ungoverned` is the reading this surface exists for: an INCLUDED field with
 * neither a canonical target nor an MDM target lands in the warehouse as a
 * column nothing else in the platform can join, compare or aggregate against.
 * It is not an error — the compiler does not block on it — which is exactly why
 * it needs to be countable: nothing else on the screen would ever mention it.
 */
export const FIELD_FILTERS = ["all", "blocking", "warning", "ungoverned", "excluded"] as const;
export type FieldFilter = (typeof FIELD_FILTERS)[number];

export const FILTER_LABEL: Record<FieldFilter, string> = {
  all: "All fields",
  blocking: "Blocking",
  warning: "Needs review",
  ungoverned: "No governed target",
  excluded: "Excluded",
};

export function bindingStatus(field: EvidenceRecord): string {
  return text(record(field.binding)?.status, "unknown");
}

/**
 * Story 60.6, arbitrage 2: ONE flag, and it is the binding's own status.
 *
 * This screen read `field.included === false`, and nothing else in the platform
 * did. The compiler's three gates, the file import and the fail-closed MDM all
 * read `binding.status === "excluded"`, so a column could read `Excluded` here
 * and land anyway — or the reverse.
 */
export function isExcluded(field: EvidenceRecord): boolean {
  return bindingStatus(field) === "excluded";
}

/** The concept a field names, whichever key of the payload carries it.
 *
 *  Measured on the server side (`datastream_daily_breakdown_api.py`): 105 of 708
 *  bindings leave `mdm_target` null and put the SAME registry identity under
 *  `canonical_target`. Reading one key alone reports a bound field as unmapped. */
export function governedTarget(field: EvidenceRecord): string {
  const binding = record(field.binding) ?? {};
  return (
    text(binding.mdm_target ?? field.mdm_target, "")
    || text(binding.canonical_target ?? field.canonical_target, "")
  );
}

/** A field is governed when it reaches EITHER a canonical metric/dimension or an
 *  MDM entity. Excluded fields are not counted: they never land. */
export function isGoverned(field: EvidenceRecord): boolean {
  return governedTarget(field) !== "";
}

export const FILTER_PREDICATE: Record<FieldFilter, (field: EvidenceRecord) => boolean> = {
  all: () => true,
  blocking: (field) => ["blocking", "blocked"].includes(bindingStatus(field)),
  warning: (field) => ["warning", "ambiguous"].includes(bindingStatus(field)),
  ungoverned: (field) => !isExcluded(field) && !isGoverned(field),
  excluded: isExcluded,
};

/** A join being composed on screen, in the shape the mapping version stores. */
export interface DraftJoin {
  target: string;
  sources: string[];
  separator: string;
}

/** A split being composed on screen. ONE entry carries all N targets. */
export interface DraftSplit {
  source: string;
  pattern: string;
  targets: Array<{ group: string; target: string }>;
}

export function declaredJoins(mapping: EvidenceRecord | null): DraftJoin[] {
  return records(record(mapping?.column_treatments)?.joins).map((join) => ({
    target: text(join.target, ""),
    sources: stringList(join.sources),
    separator: typeof join.separator === "string" ? join.separator : "",
  }));
}

export function declaredSplits(mapping: EvidenceRecord | null): DraftSplit[] {
  return records(record(mapping?.column_treatments)?.splits).map((split) => ({
    source: text(split.source, ""),
    pattern: text(split.pattern, ""),
    targets: records(split.targets).map((entry) => ({
      group: text(entry.group, ""),
      target: text(entry.target, ""),
    })),
  }));
}

/**
 * WHAT A DECLARED JOIN OR SPLIT DOES TODAY, said in one sentence, in one place.
 *
 * Measured 2026-08-11 (amendment 12's neighbour, the heaviest finding of the
 * review): a join or a split declared here is written into the mapping version
 * as `column_treatments` and NO ENGINE EXECUTES IT. `grep -rn
 * "produced_columns|column_treatments" dbt/` returns 0;
 * `datastream_projection.py` computes `plan["produced_columns"]` and nothing
 * reads it back; the only path that turns a mapping into warehouse columns —
 * `import_landing.py::_apply_governed_mapping` — binds strictly one source to
 * one `binding.canonical_target` out of `mapping_payload["fields"]` and never
 * looks at `column_treatments`. It is structural, not an oversight:
 * `column_treatments.py::_claim_target` refuses a join target already claimed by
 * a field binding, so a join's target is by construction never a
 * `canonical_target`, which is the only thing the lander writes.
 *
 * So the screen states the fact and nothing more. No date, no plan, no promise
 * about when an engine will exist — inventing one here would be a decision
 * presented as a repair.
 *
 * ONE constant because the composer and the affected rows must not drift: an
 * amendment that reaches one of its sites and not the others is the fault this
 * surface named three times in a single day.
 */
export const TREATMENT_NOT_APPLIED =
  "Recorded, not applied: nothing executes this declaration yet, so the source "
  + "columns still land as themselves and no new column appears in the warehouse.";

/**
 * Every concept already spoken for, so the screen refuses a second claim BEFORE
 * the import rather than after it.
 *
 * `csv_excel_import` refuses two source columns landing on one canonical target
 * (`dispatch_mapping_collision`), and `column_treatments.normalize_treatments`
 * refuses it at the append — but both of those refuse a person who has already
 * walked away. The story's own Refuse line puts it on the screen, first.
 *
 * `pendingTargets` is part of the answer, not a detail: a concept a person just
 * picked in the row above is claimed, and offering it again two rows down would
 * compose a mapping the append refuses.
 */
export function claimedConcepts(
  mapping: EvidenceRecord | null,
  joins: DraftJoin[],
  splits: DraftSplit[],
  pendingTargets: Record<string, string | null> = {},
): Set<string> {
  const claimed = new Set<string>();
  for (const field of records(mapping?.fields)) {
    if (isExcluded(field)) continue;
    const id = text(field.field_id, "");
    const pending = id in pendingTargets ? pendingTargets[id] : undefined;
    const target = pending === undefined ? governedTarget(field) : (pending ?? "");
    if (target) claimed.add(target);
  }
  for (const join of joins) if (join.target) claimed.add(join.target);
  for (const split of splits) {
    for (const entry of split.targets) if (entry.target) claimed.add(entry.target);
  }
  return claimed;
}

/**
 * The proposed mapping contract for one prepared change, whole.
 *
 * Built HERE and handed to the confirmation as it stands, so none of the four
 * verbs passes through the raw JSON textarea that used to be the only way to
 * reach any of them. Nothing else in the payload is touched, and it rewrites no
 * history — publishing appends (`datastream-workbench-and-wizard.md:987`).
 *
 * A column being RE-INCLUDED returns to `suggested`, not to a confirmation
 * nobody gave: its earlier binding was thrown away when it was excluded, and
 * inventing one back would be a review this person did not do.
 *
 * A column being BOUND writes `binding.mdm_target`, which is the key the append
 * validates against `app.mdm_canonical_fields` — fail-closed, with the repair
 * `register_or_pick_canonical_field` (`datastream_field_mapping.py`). Writing
 * the registry identity under `canonical_target` instead would land an opaque id
 * where the rest of the platform reads a name.
 *
 * `column_treatments` is written only when there is something to write: a
 * mapping that declares no join and no split keeps NO key, because an empty
 * declaration and an absent one are not the same statement.
 *
 * A DECLARED SCHEMA SET IS CARRIED THROUGH, NEVER REWRITTEN HERE (story 70.1).
 * This function composes the whole `column_treatments` object from the two
 * families this screen edits, so a third family it does not edit would be
 * DELETED by a person who only wanted to add a join. `schema_splits` is read
 * from the version being amended and written back untouched: the two controls
 * of this table are the joins and the splits, and a control must not destroy
 * what it cannot show.
 */
export function withTreatments(
  payload: EvidenceRecord,
  pending: Record<string, boolean>,
  joins: DraftJoin[],
  splits: DraftSplit[],
  pendingTargets: Record<string, string | null> = {},
): EvidenceRecord {
  const proposed: EvidenceRecord = {
    ...payload,
    fields: records(payload.fields).map((field) => {
      const id = text(field.field_id, "");
      const touchesInclusion = id in pending;
      const touchesTarget = id in pendingTargets;
      if (!touchesInclusion && !touchesTarget) return field;
      const binding: EvidenceRecord = { ...(record(field.binding) ?? {}) };
      if (touchesInclusion) {
        binding.status = pending[id] ? "excluded" : "suggested";
        binding.blocking_reason = null;
      }
      if (touchesTarget) {
        binding.mdm_target = pendingTargets[id];
        // The same identity travels under BOTH keys on part of the estate. A
        // stale `canonical_target` left behind would keep naming the concept the
        // person just replaced, and `isGoverned` would read the old one.
        if (typeof binding.canonical_target === "string") binding.canonical_target = null;
      }
      return { ...field, binding };
    }),
  };
  const schemaSplits = records(record(payload.column_treatments)?.schema_splits);
  if (joins.length === 0 && splits.length === 0 && schemaSplits.length === 0) {
    delete proposed.column_treatments;
    return proposed;
  }
  proposed.column_treatments =
    schemaSplits.length > 0 ? { joins, splits, schema_splits: schemaSplits } : { joins, splits };
  return proposed;
}
