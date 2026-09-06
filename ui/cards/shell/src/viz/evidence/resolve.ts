/**
 * Story 50.5 AC11 -- ONE mapping from a mark back to the immutable Result.
 *
 * Tooltip, selection, drill-through, exported evidence and feedback all resolve
 * through this module. A renderer may show a SHORTER summary of what it resolves;
 * it may not resolve to something else, and it may not lose the reference when a
 * profile compacts the view.
 *
 * WHERE THE EVIDENCE ACTUALLY COMES FROM, measured rather than assumed. Story
 * 50.1 captures provenance into the Result manifest at
 * `server/core/query_execution.py:340-355`: `provenance.source_system`,
 * `provenance.mapping_version_id`, `provenance.pull_id`,
 * `provenance.publication_log_id`, one `provenance.values[]` entry PER MEMBER
 * (`member_id`, `source_system`, `source_field`, `pull_id`), plus
 * `freshness.output_created_at` and the DQ evaluation ids. There is no per-datum
 * evidence map on the wire today, and this module does not invent one: a datum's
 * evidence is the member-level provenance of the fields that datum is made of,
 * scoped to the Result that returned it. That is a true statement about where the
 * number came from; a fabricated per-row citation would not be.
 *
 * A DATUM WITH NO BINDING RESOLVES TO A NAMED STATE. When the spec sets
 * `evidence.mark_binding = "none"`, or a datum's fields have no provenance entry,
 * the answer is the explicit "No evidence binding" state -- never an empty
 * tooltip and never a fabricated one.
 */

import type { CellValue, VizResult, VizSpecDocument } from "../contracts";
import { memberColumns, type CompiledVisualModel } from "../compile/dataset";

export interface MemberProvenance {
  member_id: string;
  source_system: string | null;
  source_field: string | null;
  pull_id: string | null;
}

export interface DatumEvidence {
  datumKey: string;
  bound: true;
  /** The member ids this datum is made of, in binding order. */
  fields: string[];
  /** Per-member provenance, for the fields that have one. */
  provenance: MemberProvenance[];
  resultId: string;
  contentHash: string;
  mappingVersionId: string | null;
  publicationLogId: string | null;
  freshness: string | null;
  dqEvaluationIds: string[];
  /** The values of `fields` in that datum, so the drawer shows what was clicked. */
  values: Record<string, CellValue>;
}

export interface DatumEvidenceAbsent {
  datumKey: string;
  bound: false;
  /** English, shown verbatim. Never an empty tooltip. */
  message: string;
}

export type DatumEvidenceResolution = DatumEvidence | DatumEvidenceAbsent;

const NO_BINDING =
  "No evidence binding. This Visualization Spec does not declare evidence for this mark, " +
  "so nothing is claimed about where this value came from.";

const NO_PROVENANCE =
  "No evidence binding. The Result carries no provenance for the fields behind this mark, " +
  "so nothing is claimed about where this value came from.";

/**
 * The compiler drew a mark for a (category, split) the server returned no row
 * for -- the dataset cell is null and the mark is empty. Saying so is a true
 * statement; returning an empty `values` map under `bound: true` was not.
 */
const NO_ROW =
  "No returned row backs this mark. The server returned no row for this combination, so the " +
  "mark is drawn empty and nothing is claimed about a value here.";

/**
 * ONE RESOLUTION OF A MEMBER TO ITS COLUMN, shared with the compiler.
 *
 * The Result manifest keys its provenance by MEMBER ID
 * (`query_execution.capture_evidence`), while `datumFields` names the COLUMNS the
 * datum is made of. Matching the two literally found nothing on every executed
 * Result -- every mark answered "No evidence binding", which reads as *the Spec
 * declares no evidence* and was in fact *the runtime could not join two spellings
 * of the same member*. Measured 2026-08-31 (AI-337). The map is imported, never
 * rebuilt here: two copies of it would drift apart on the next field the server
 * declares.
 */
function memberColumn(result: VizResult, memberId: string): string {
  return memberColumns(result).get(memberId) ?? memberId;
}

function readProvenance(result: VizResult): MemberProvenance[] {
  const manifest = result.manifest as unknown as Record<string, unknown> | undefined;
  const provenance = manifest?.["provenance"];
  if (!provenance || typeof provenance !== "object") return [];
  const values = (provenance as Record<string, unknown>)["values"];
  if (!Array.isArray(values)) return [];
  const out: MemberProvenance[] = [];
  for (const entry of values) {
    if (!entry || typeof entry !== "object") continue;
    const e = entry as Record<string, unknown>;
    if (typeof e["member_id"] !== "string") continue;
    out.push({
      // The column, which is the member's canonical NAME: the drawer prints this
      // and a reader cannot use a ULID.
      member_id: memberColumn(result, e["member_id"]),
      source_system: typeof e["source_system"] === "string" ? e["source_system"] : null,
      source_field: typeof e["source_field"] === "string" ? e["source_field"] : null,
      pull_id: typeof e["pull_id"] === "string" ? e["pull_id"] : null,
    });
  }
  return out;
}

function readScalar(result: VizResult, group: string, key: string): string | null {
  const manifest = result.manifest as unknown as Record<string, unknown> | undefined;
  const node = manifest?.[group];
  if (!node || typeof node !== "object") return null;
  const value = (node as Record<string, unknown>)[key];
  return typeof value === "string" ? value : null;
}

function readPinnedDatumProvenance(
  result: VizResult,
  row: Record<string, CellValue>,
): MemberProvenance[] {
  if (result.manifest?.result_shape !== "waterfall_v1") return [];
  const evidenceKey = row.evidence_key;
  if (typeof evidenceKey !== "string") return [];
  const manifest = result.manifest as unknown as Record<string, unknown>;
  const index = manifest["datum_evidence"];
  if (!index || typeof index !== "object" || Array.isArray(index)) return [];
  const raw = (index as Record<string, unknown>)[evidenceKey];
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return [];
  const entry = raw as Record<string, unknown>;
  if (typeof entry["member_id"] !== "string") return [];
  return [
    {
      member_id: memberColumn(result, entry["member_id"]),
      source_system:
        typeof entry["source_system"] === "string" ? entry["source_system"] : null,
      source_field:
        typeof entry["source_field"] === "string" ? entry["source_field"] : null,
      pull_id: typeof entry["pull_id"] === "string" ? entry["pull_id"] : null,
    },
  ];
}

/**
 * Derive the ordered datum keys and the field list behind each. The keys come
 * from the compiler, which built them from `result.content_hash` -- so a key is
 * stable for one immutable Result and cannot survive into a different one.
 */
export function resolveDatumEvidence(
  datumKey: string,
  model: CompiledVisualModel,
  result: VizResult,
  document: VizSpecDocument,
): DatumEvidenceResolution {
  if (document.evidence?.mark_binding === "none") {
    return { datumKey, bound: false, message: NO_BINDING };
  }
  const fields = model.datumFields[datumKey];
  if (!fields || fields.length === 0) {
    return { datumKey, bound: false, message: NO_BINDING };
  }

  // The values behind the mark, read POSITIONALLY from the row the compiler
  // built the datum from. `datumRowIndex` is emitted by the compiler, which knew
  // the row by its index; nothing here takes the key apart.
  //
  // AN EARLIER VERSION OF THIS FUNCTION DID `datumKey.split("|")`, AND IT WAS
  // WRONG. A campaign called `Brand|Generic` produced the key
  // `<hash>|Brand|Generic|sessions|`, whose second segment is `Brand` -- so the
  // drawer opened on a DIFFERENT row's values (11 where the mark drew 999) and
  // still reported `bound: true`. With a space (`Brand | Generic`) the segment
  // matched no row and `values` came back `{}` with no statement that it had
  // failed. The compiler had already learned this two functions away, where the
  // (category, split) lookup uses U+0001 precisely so `"A"+"BC"` cannot collide
  // with `"AB"+"C"`. `__tests__/evidence.test.tsx` holds both cases.
  const rowIndex = model.datumRowIndex[datumKey];
  const row = rowIndex === undefined || rowIndex < 0 ? undefined : result.rows[rowIndex];
  if (!row) {
    return { datumKey, bound: false, message: NO_ROW };
  }
  const pinnedDatumProvenance = readPinnedDatumProvenance(result, row);
  const provenance =
    pinnedDatumProvenance.length > 0
      ? pinnedDatumProvenance
      : readProvenance(result).filter((p) => fields.includes(p.member_id));
  if (provenance.length === 0) {
    return { datumKey, bound: false, message: NO_PROVENANCE };
  }
  const values: Record<string, CellValue> = {};
  for (const field of fields) {
    if (field in row) values[field] = row[field]!;
  }

  const manifest = result.manifest as unknown as Record<string, unknown> | undefined;
  const dqIds = manifest?.["dq_evaluation_ids"];

  return {
    datumKey,
    bound: true,
    fields,
    provenance,
    resultId: result.result_id,
    contentHash: result.content_hash,
    mappingVersionId: readScalar(result, "provenance", "mapping_version_id"),
    publicationLogId: readScalar(result, "provenance", "publication_log_id"),
    freshness: readScalar(result, "freshness", "output_created_at"),
    dqEvaluationIds: Array.isArray(dqIds) ? dqIds.filter((v): v is string => typeof v === "string") : [],
    values,
  };
}

/**
 * Hit-testing: the SAME function for pointer and keyboard.
 *
 * A chart mark is identified by `(seriesIndex, categoryIndex)` in both cases --
 * ECharts reports them on a pointer event, and the focusable mark list reports
 * them on a keyboard event. Because both go through this one derivation, a
 * keyboard user and a mouse user resolve the same datum key, which is what
 * `__tests__/evidence.test.tsx` asserts.
 *
 * IT IS ALSO THE SHAPE `resolveDatumEvidence` NOW FOLLOWS: derive the datum from
 * a POSITION, never from a string. This function was already right; the resolver
 * was not, and the two now agree.
 */
export function datumKeyAt(
  model: CompiledVisualModel,
  seriesIndex: number,
  categoryIndex: number,
): string | null {
  const seriesCount = model.series.length;
  if (seriesCount === 0) return null;
  if (seriesIndex < 0 || seriesIndex >= seriesCount) return null;
  if (categoryIndex < 0) return null;
  const index = categoryIndex * seriesCount + seriesIndex;
  return model.datumKeys[index] ?? null;
}
