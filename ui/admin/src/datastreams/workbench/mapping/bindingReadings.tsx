/**
 * THE SEVEN READINGS OF A COLUMN, AND THE RULE THAT DECIDES WHICH ARE COLUMNS.
 *
 * Finding D-1 of the 2026-08-12 visual review, measured on ten rendered rows:
 * `Role` said **Unknown** ten times, `Treatment` **Unavailable** ten times,
 * `Example value` **unknown** ten times, `Sensitivity` **None** ten times,
 * `Aggregation` **None** eight times out of ten, and the confidence caption
 * **Unknown confidence** ten times. Six readings, sixty cells, six facts. The
 * signal a person comes for — which column blocks, what it becomes — was under
 * five columns of noise.
 *
 * « Une colonne qui dit la même chose partout n'est pas une colonne, c'est une
 * note de bas de page. »
 *
 * SO THE FOLD IS DECIDED FROM THE DATA, NEVER FROM A LIST OF COLUMN NAMES.
 * `foldedReadings` compares each reading's SIGNATURE across the rows actually
 * rendered; a reading whose signature differs on any two of them stays a column.
 * A version where `Role` genuinely varies still shows `Role`, and a hard-coded
 * "these five always fold" would have hidden it. The consequence runs the other
 * way too: declaring a join makes `Treatment` differ on the rows it names — the
 * signature carries the sentence, not only the word — so the column comes back
 * by itself, and the sentence the ratified document requires on those rows
 * cannot be folded away.
 *
 * WHAT NEVER FOLDS is not in this file: the source identity, the concept the
 * column becomes, its binding state and the acts are the subject of the screen,
 * and a subject is not a footnote however uniform it reads.
 */
import type { ReactNode } from "react";
import { formatPercent } from "../../../ui/format";
import { record, text, titleCase, type EvidenceRecord } from "../evidence";
import { stringList, TREATMENT_NOT_APPLIED } from "./mappingModel";

/** One rendered row, as both the table and the folded detail read it. */
export interface ReadingContext {
  field: EvidenceRecord;
  id: string;
  /** The per-column reading the server computed, or `null` when the whole
   *  `columns` key is absent — which is a failed read, not an absence. */
  reading: EvidenceRecord | null;
  /** True when a declared join or split names this column as a source. */
  treated: boolean;
}

export interface BindingReading {
  key: string;
  /** The column header, and the same word as the label in the folded detail:
   *  a reading must not change name when it changes place. */
  label: string;
  /** What decides sameness across rows. For a plain reading it is also what is
   *  rendered, so the two can never drift apart. */
  signature: (row: ReadingContext) => string;
  /** Only for a reading whose cell is more than its signature. */
  cell?: (row: ReadingContext) => ReactNode;
  testId?: (id: string) => string;
  className?: string;
}

/** `profile.confidence` is a NUMBER on the wire (0.9). Reading
 *  `binding.confidence`, which does not exist, printed "Unknown confidence" over
 *  a confidence that had been measured. */
function confidenceText(field: EvidenceRecord): string {
  const value = record(field.profile)?.confidence;
  return typeof value === "number"
    ? `${formatPercent(value)} confidence`
    : `${titleCase(text(value, "unknown"))} confidence`;
}

const ROLE: BindingReading = {
  key: "role",
  label: "Role",
  // `semantic_role` is where the data LIVES: measured 2026-08-12, none of the
  // ten mapping versions carries `role`, and this reading announced "Unknown"
  // over fields classed `dimension` or `measure_*`.
  signature: (row) => titleCase(text(record(row.field.suggestion)?.semantic_role, "unknown")),
};

const TREATMENT: BindingReading = {
  key: "treatment",
  label: "Treatment",
  testId: (id) => `treatment-${id}`,
  // The word ALONE is uniform on most versions; the sentences under it are not.
  // Folding on the word would take the join's own evidence off the screen.
  signature: (row) =>
    [
      row.reading ? text(row.reading.treatment, "Unavailable") : "Unavailable",
      stringList(row.reading?.joined_with).join(","),
      stringList(row.reading?.contributes_to).join(","),
      row.treated ? "not-applied" : "",
    ].join("|"),
  cell: (row) => (
    <>
      {row.reading ? text(row.reading.treatment, "Unavailable") : "Unavailable"}
      {/* A joined column KEEPS its row and names the columns it is joined with —
          hiding the sources of a join is the same defect as hiding an excluded
          column. */}
      {stringList(row.reading?.joined_with).length > 0 && (
        <span className="block text-caption text-text-secondary">
          with {stringList(row.reading?.joined_with).join(", ")}
        </span>
      )}
      {stringList(row.reading?.contributes_to).length > 1 && (
        <span className="block text-caption text-text-secondary">
          into {stringList(row.reading?.contributes_to).join(", ")}
        </span>
      )}
      {/* WHAT THE DECLARATION DOES TODAY, on the rows it names. Measured
          2026-08-11: no engine reads `column_treatments`, so a row marked
          `Joined` still lands as itself. */}
      {row.treated && (
        <span
          className="block text-caption text-text-secondary"
          data-testid={`treatment-not-applied-${row.id}`}
        >
          {TREATMENT_NOT_APPLIED}
        </span>
      )}
    </>
  ),
};

const SEMANTIC_TYPE: BindingReading = {
  key: "semantic_type",
  label: "Semantic type",
  signature: (row) => text(row.field.semantic_type ?? row.field.physical_type),
};

const SAMPLE: BindingReading = {
  key: "sample",
  label: "Example value",
  testId: (id) => `sample-${id}`,
  className: "font-mono text-caption",
  // Read from the sample already profiled into this version. Never fabricated:
  // an empty column says so in words, and a column nobody profiled says a
  // different thing again.
  signature: (row) => (row.reading ? text(row.reading.sample_value, "unknown") : "unknown"),
};

const AGGREGATION: BindingReading = {
  key: "aggregation",
  label: "Aggregation",
  signature: (row) => text(row.field.aggregation, "None"),
};

const SENSITIVITY: BindingReading = {
  key: "sensitivity",
  label: "Sensitivity",
  signature: (row) => titleCase(row.field.sensitivity),
};

/**
 * The confidence is not a column and never was: it is the second line of the
 * binding state, and it folds on exactly the same rule as the six above.
 */
export const CONFIDENCE: BindingReading = {
  key: "confidence",
  label: "Binding confidence",
  signature: (row) => confidenceText(row.field),
};

/** Before the concept, because they describe the column as it ARRIVES. */
export const READINGS_BEFORE_TARGET: BindingReading[] = [ROLE, TREATMENT, SEMANTIC_TYPE];
/** After it, because they qualify what was bound. */
export const READINGS_AFTER_TARGET: BindingReading[] = [SAMPLE, AGGREGATION, SENSITIVITY];

/** Every foldable reading, in the order the folded detail lists them. */
export const ALL_READINGS: BindingReading[] = [
  ...READINGS_BEFORE_TARGET,
  ...READINGS_AFTER_TARGET,
  CONFIDENCE,
];

/**
 * The keys that say the same thing on every rendered row.
 *
 * ONE ROW FOLDS NOTHING. A table of a single column has no "same thing on every
 * row" to speak of — every reading is trivially uniform, and folding them all
 * would leave a person looking at one identity and an unfold chevron. The
 * triage filters routinely leave one row, which is exactly when the readings are
 * wanted.
 */
export function foldedReadings(rows: ReadingContext[]): Set<string> {
  if (rows.length < 2) return new Set();
  const folded = new Set<string>();
  for (const reading of ALL_READINGS) {
    const first = reading.signature(rows[0]);
    if (rows.every((row) => reading.signature(row) === first)) folded.add(reading.key);
  }
  return folded;
}

/** What a folded reading renders, wherever it renders. */
export function readingContent(reading: BindingReading, row: ReadingContext): ReactNode {
  return reading.cell ? reading.cell(row) : reading.signature(row);
}
