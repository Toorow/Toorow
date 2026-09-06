/**
 * Story 50.5 AC4 -- the toorow compiler. Bindings + Result -> an in-memory
 * dataset/encode model. PURE, synchronous, and it never computes a value.
 *
 * WHY dataset/encode AND NOT A HAND-BUILT `series.data`. ECharts' dataset model
 * lets the library map a column to a visual channel
 * (<https://echarts.apache.org/handbook/en/concepts/dataset/>). Because the
 * toorow layer never assembles a per-point array, there is nowhere for an
 * arithmetic step to hide: the numbers ECharts draws are the numbers the server
 * returned, in the order the server ranked them. AC3 is provable by grep for the
 * same reason -- the only producer of an ECharts option is
 * `viz/adapters/echarts/`, and this module produces no option at all.
 *
 * WHAT THIS FILE IS FORBIDDEN TO DO, restated because it is the whole point:
 * no sum, no average, no re-bucketing, no timezone shift, no fill of a missing
 * period, no sort. A missing period stays missing and is disclosed; filling it
 * would answer a question nobody asked.
 *
 * ROLES COME FROM THE BINDING, NEVER FROM THE DATA. Which member is a measure and
 * which is a dimension is decided by the WELL it is bound to
 * (`server/core/visualization_families.py:107`), which the Builder filled from
 * the pinned Query Spec version's members. This file never inspects a value shape
 * or a column name to decide a role: an invented role silently changes what a
 * chart claims.
 */

import type {
  CellValue,
  VizResult,
  VizSpecDocument,
  WellName,
} from "../contracts";
import { looksLikeDate } from "../theme/formatters";
import type { VizPalette } from "../../vizTheme";
import { evaluateVolume, type VolumeConstraints, type VolumeVerdict } from "./limits";

/**
 * The separator inside a compiled datum key, and inside the (category, split)
 * lookup key below.
 *
 * IT IS U+0001, NOT `|`, AND THAT IS THE WHOLE POINT. A printable separator is a
 * character a dimension value is allowed to contain: `"Brand|Generic"` is an
 * ordinary campaign name, and joining with `|` makes `"A" + "B|C"` and
 * `"A|B" + "C"` the same string. Two datums that collide are two marks that
 * resolve to one another's evidence -- a mark showing 999 opening a drawer that
 * says 11, presented as bound. U+0001 is a C0 control character: no Result value
 * a warehouse returns carries it, and `String(value)` cannot produce one. It is
 * written `String.fromCharCode(1)` rather than as a literal so it is visible to
 * a reader and to `grep` -- an invisible byte in a source file is a separator
 * nobody can review.
 *
 * A CONSUMER NEVER TAKES A KEY APART. The separator makes the key unambiguous;
 * `datumRowIndex` and `datumLabels` make parsing unnecessary. Both properties are
 * needed -- an unambiguous key that someone still splits is one refactor away
 * from the same defect.
 */
export const DATUM_KEY_SEP = String.fromCharCode(1);

/** One compiled series: a measure, optionally split by one categorical member. */
export interface CompiledSeries {
  /** Stable within one compiled model; used as the legend key and the stack key. */
  id: string;
  /** The label a reader sees. Spec `labels.override` wins, else the member id. */
  name: string;
  /** The measure member this series draws. */
  measureId: string;
  /** The split value when the family bound `series` or `breakdown`; else null. */
  splitValue: string | null;
  /** RESOLVED colour, from the live theme. Never an ECharts default. */
  color: string;
  /** The dataset column this series encodes on the value axis. */
  valueColumn: string;
  stack: string | null;
}

export interface CompiledAxis {
  scale: "linear" | "log" | "categorical" | "ordinal";
  zeroBaseline: boolean;
  tickDensity: "sparse" | "normal" | "dense";
  /** The dataset column the axis reads. Null on a value axis fed by series. */
  column: string | null;
  /** True when the axis labels are formatted as dates. Decided by the BINDING. */
  isDate: boolean;
  label: string | null;
}

export interface CompiledTableColumn {
  key: string;
  label: string;
  isDate: boolean;
  /** "dimension" or "measure": drives alignment and the formatter, nothing else. */
  role: "dimension" | "measure";
}

export interface CompiledDisclosures {
  grain: string | null;
  timeWindow: string | null;
  filters: string[];
  comparison: string | null;
  freshness: string | null;
  truncation: string | null;
  limitation: string | null;
  topN: string | null;
}

/**
 * The serialized visual model AC8 compares across the three surfaces. Every field
 * here is JSON-serializable on purpose: a function could not be deep-compared,
 * and a parity proof you cannot serialize is a parity claim.
 */
export interface CompiledVisualModel {
  family: string;
  dataset: { dimensions: string[]; source: CellValue[][] };
  /** ECharts encode, per series index: `{ x, y, seriesName }`. */
  encode: { x: string | null; y: string; seriesId: string }[];
  series: CompiledSeries[];
  axes: { x: CompiledAxis; y: CompiledAxis };
  legend: { position: string; visible: boolean; entries: { id: string; name: string; color: string }[] };
  formats: { numberStyle: string; dateStyle: string; unit: string | null };
  colors: { accent: string; semanticDirection: string; palette: string[] };
  /** Ordered datum keys, one per (row, series). */
  datumKeys: string[];
  /** datum key -> the member ids whose values it is made of. */
  datumFields: Record<string, string[]>;
  /**
   * datum key -> the INDEX of the row in `result.rows` the datum was built from,
   * or -1 when the server returned no row for that (category, split).
   *
   * THIS IS THE ONLY WAY BACK TO A ROW. It is emitted by the compiler, which
   * knows the row positionally, so no consumer ever has to take a datum key
   * apart to find the row again -- see the separator note above `DATUM_KEY_SEP`.
   */
  datumRowIndex: Record<string, number>;
  /** Closed feedback locator emitted by the compiler; datum keys stay opaque. */
  datumTargets: Record<string, { row_index: number; field: string }>;
  /** datum key -> a human label ("organic - Sessions"), for announcements. */
  datumLabels: Record<string, string>;
  disclosures: CompiledDisclosures;
  tableColumns: CompiledTableColumn[];
  volume: VolumeVerdict;
}

export interface CompileOptions {
  palette: VizPalette;
  limits: VolumeConstraints;
  /** Governed unit for the currency style, or null. Never guessed here. */
  unit?: string | null;
}

function bound(document: VizSpecDocument, well: WellName): string[] {
  const value = document.bindings?.[well];
  return Array.isArray(value) ? value.filter((v) => typeof v === "string" && v !== "") : [];
}

/**
 * A BINDING NAMES A MEMBER; A ROW IS KEYED BY A COLUMN. This resolves the one to
 * the other, from the map the server already ships.
 *
 * A Visualization Spec binds CONCEPT IDS -- that is what
 * `visualization_specs.check_shape_compatibility` validates a document against --
 * while `query_execution.shape_result_payload` keys each row by the member's
 * COLUMN NAME and declares both halves on the schema (`{id, name}`). Reading
 * `row[memberId]` therefore found nothing on every Result a warehouse actually
 * produced: a chart of nulls, a table whose every column was typed `dimension`,
 * and a legend printing a ULID. It went unseen because the only envelope this
 * runtime had ever compiled was a fixture whose member ids WERE its column names.
 *
 * Measured 2026-08-31 (AI-337), on the first captured Result.
 *
 * The map is the server's, not a guess: a field with no `id` maps to itself, so
 * an envelope that binds by column name compiles exactly as before.
 */
export function memberColumns(result: VizResult): Map<string, string> {
  const byMember = new Map<string, string>();
  for (const field of result.schema?.fields ?? []) {
    if (typeof field?.id === "string" && field.id !== "" && typeof field?.name === "string") {
      byMember.set(field.id, field.name);
    }
  }
  return byMember;
}

/**
 * The label a reader sees for one dataset column.
 *
 * `labels.override` is keyed by MEMBER ID (the grammar's own key), so the member
 * is consulted as well as the column. With neither, the column NAME is the label
 * -- which is the concept's canonical name, never its identifier.
 */
function labelFor(
  document: VizSpecDocument,
  column: string,
  memberOfColumn?: Map<string, string>,
): string {
  const direct = document.labels?.override?.[column];
  if (typeof direct === "string" && direct !== "") return direct;
  const member = memberOfColumn?.get(column);
  const viaMember = member ? document.labels?.override?.[member] : undefined;
  if (typeof viaMember === "string" && viaMember !== "") return viaMember;
  return column;
}

/**
 * The distinct values of one column, in FIRST-APPEARANCE order.
 *
 * This is a read, not a grouping: it discovers which series exist so each one
 * can be encoded on its own dataset column. It adds nothing, removes nothing and
 * reorders nothing -- the row order stays exactly the server's ranking.
 */
function distinctInOrder(rows: Record<string, CellValue>[], column: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const row of rows) {
    const raw = row[column];
    const key = raw === null || raw === undefined ? "" : String(raw);
    if (!seen.has(key)) {
      seen.add(key);
      out.push(key);
    }
  }
  return out;
}

function resultFieldNames(result: VizResult): string[] {
  const declared = result.schema?.fields;
  if (Array.isArray(declared) && declared.length > 0) return declared.map((f) => f.name);
  return result.rows.length > 0 ? Object.keys(result.rows[0]!) : [];
}

function disclosuresOf(result: VizResult, document: VizSpecDocument): CompiledDisclosures {
  const manifest = result.manifest ?? {};
  const window = manifest.time_window;
  const freshness = manifest.freshness;
  const filters = Array.isArray(manifest.filters)
    ? manifest.filters.filter((f): f is string => typeof f === "string")
    : [];
  return {
    grain: typeof manifest.grain === "string" ? manifest.grain : null,
    timeWindow:
      window && (window.start || window.end)
        ? `${window.start ?? "--"} to ${window.end ?? "--"}`
        : null,
    filters,
    comparison: typeof manifest.comparison === "string" ? manifest.comparison : null,
    freshness:
      typeof freshness === "string"
        ? freshness
        : freshness &&
            typeof freshness === "object" &&
            typeof (freshness as Record<string, unknown>).state === "string"
          ? ((freshness as Record<string, unknown>).state as string)
          : null,
    truncation:
      typeof manifest.truncation === "string"
        ? manifest.truncation
        : result.truncated
          ? "The server returned a truncated slice of this answer."
          : null,
    limitation:
      typeof manifest.unavailable_reason === "string" ? manifest.unavailable_reason : null,
    // `top_n.display_only` is the ONLY accepted value in the grammar, so this
    // string is always a display statement, never a claim about the whole.
    topN:
      document.top_n && typeof document.top_n.n === "number"
        ? `Showing the top ${document.top_n.n} of the returned rows. This is a display ` +
          `limit on rows the server already ranked; the totals below are unchanged.`
        : null,
  };
}

function compileWaterfallModel(
  result: VizResult,
  document: VizSpecDocument,
  options: CompileOptions,
): CompiledVisualModel {
  const rows = Array.isArray(result.rows) ? result.rows : [];
  const datasetDimensions = [
    "label",
    "start_total_micros",
    "running_total_micros",
    "waterfall_role",
    "value_micros",
    "is_complete",
  ];
  const source = rows.map((row) =>
    datasetDimensions.map((field) => row[field] ?? null),
  );
  const series: CompiledSeries[] = [
    {
      id: "waterfall",
      name: "Amount",
      measureId: "value_micros",
      splitValue: null,
      color: options.palette.accent,
      valueColumn: "running_total_micros",
      stack: null,
    },
  ];
  const fieldNames = resultFieldNames(result);
  const measureFields = new Set([
    "value_micros",
    "start_total_micros",
    "running_total_micros",
    "covered_row_count",
    "total_row_count",
  ]);
  const tableColumns = fieldNames.map((name) => ({
    key: name,
    label: labelFor(document, name),
    isDate: false,
    role: (measureFields.has(name) ? "measure" : "dimension") as
      | "measure"
      | "dimension",
  }));
  const datumKeys: string[] = [];
  const datumFields: Record<string, string[]> = {};
  const datumRowIndex: Record<string, number> = {};
  const datumTargets: Record<string, { row_index: number; field: string }> = {};
  const datumLabels: Record<string, string> = {};
  rows.forEach((row, index) => {
    const key = typeof row.datum_key === "string" ? row.datum_key : "";
    if (!key) return;
    datumKeys.push(key);
    datumFields[key] = fieldNames;
    datumRowIndex[key] = index;
    datumTargets[key] = { row_index: index, field: "running_total_micros" };
    datumLabels[key] =
      typeof row.label === "string" ? row.label : `Waterfall datum ${index + 1}`;
  });
  const currency =
    rows.length > 0 && typeof rows[0]!.currency === "string"
      ? rows[0]!.currency
      : (options.unit ?? null);

  return {
    family: "waterfall",
    dataset: { dimensions: datasetDimensions, source },
    encode: [
      {
        x: "label",
        y: "running_total_micros",
        seriesId: "waterfall",
      },
    ],
    series,
    axes: {
      x: {
        scale: "categorical",
        zeroBaseline: true,
        tickDensity: document.axes.x.tick_density,
        column: "label",
        isDate: false,
        label: null,
      },
      y: {
        scale: "linear",
        zeroBaseline: true,
        tickDensity: document.axes.y.tick_density,
        column: null,
        isDate: false,
        label: currency,
      },
    },
    legend: { position: "none", visible: false, entries: [] },
    formats: {
      numberStyle: document.formatting.number_style,
      dateStyle: document.formatting.date_style,
      unit: currency,
    },
    colors: {
      accent: options.palette.accent,
      semanticDirection: document.color.semantic_direction,
      palette: [options.palette.accent],
    },
    datumKeys,
    datumFields,
    datumRowIndex,
    datumTargets,
    datumLabels,
    disclosures: disclosuresOf(result, document),
    tableColumns,
    volume: evaluateVolume(
      { rows: rows.length, series: 1, cells: rows.length * fieldNames.length },
      options.limits,
    ),
  };
}

/**
 * Compile. Throws nothing: an input the compiler cannot honour has already been
 * refused by `validate.ts`, and a volume it cannot draw becomes a table fallback
 * verdict rather than an exception.
 */
export function compileVisualModel(
  result: VizResult,
  document: VizSpecDocument,
  options: CompileOptions,
): CompiledVisualModel {
  if (document.family === "waterfall") {
    return compileWaterfallModel(result, document, options);
  }
  const rows = Array.isArray(result.rows) ? result.rows : [];
  // Every binding becomes a Result COLUMN here, once, before anything reads a
  // row. Downstream this file only ever handles column keys -- which is why a
  // member id can no longer reach `row[...]` and come back undefined.
  const columnByMember = memberColumns(result);
  const columnOf = (member: string): string => columnByMember.get(member) ?? member;
  const memberOfColumn = new Map<string, string>();
  for (const [member, column] of columnByMember) memberOfColumn.set(column, member);
  const boundColumns = (well: WellName): string[] => bound(document, well).map(columnOf);
  const measures = boundColumns("measure");
  const dimensions = boundColumns("dimension");
  const splitWell: WellName | null =
    bound(document, "breakdown").length > 0
      ? "breakdown"
      : bound(document, "series").length > 0
        ? "series"
        : null;
  const splitMember = splitWell ? boundColumns(splitWell)[0]! : null;

  const categoryColumn = dimensions[0] ?? null;
  const categoryIsDate =
    categoryColumn !== null && rows.length > 0 && looksLikeDate(rows[0]![categoryColumn] ?? null);

  // ---- dataset ----------------------------------------------------------
  // One column per (measure x split value). Splitting into columns is how the
  // dataset/encode model expresses multiple series WITHOUT the toorow layer ever
  // building a data array: each row keeps the value the server put in it, in the
  // column that identifies its series.
  const splitValues = splitMember ? distinctInOrder(rows, splitMember) : [null];
  const series: CompiledSeries[] = [];
  const valueColumns: { column: string; measureId: string; splitValue: string | null }[] = [];

  let colorIndex = 0;
  for (const measureId of measures) {
    for (const splitValue of splitValues) {
      const column = splitValue === null ? measureId : `${measureId} / ${splitValue}`;
      const id = splitValue === null ? measureId : `${measureId} / ${splitValue}`;
      series.push({
        id,
        name:
          splitValue === null
            ? labelFor(document, measureId, memberOfColumn)
            : `${labelFor(document, measureId, memberOfColumn)} - ${splitValue}`,
        measureId,
        splitValue,
        color:
          document.color?.role === "single"
            ? options.palette.accent
            : options.palette.categorical[colorIndex % options.palette.categorical.length]!,
        valueColumn: column,
        stack: document.family === "stacked_bar" || document.family === "area" ? "total" : null,
      });
      valueColumns.push({ column, measureId, splitValue });
      colorIndex += 1;
    }
  }

  const datasetDimensions = [
    ...(categoryColumn ? [categoryColumn] : []),
    ...valueColumns.map((v) => v.column),
  ];

  // When the family splits by a member, one source row per CATEGORY carries the
  // value of each split in its own column. The category rows are discovered in
  // first-appearance order; no value is combined, and a category that carries no
  // value for a split stays null rather than becoming a zero.
  const source: CellValue[][] = [];
  const datumKeys: string[] = [];
  const datumFields: Record<string, string[]> = {};
  const datumRowIndex: Record<string, number> = {};
  const datumTargets: Record<string, { row_index: number; field: string }> = {};
  const datumLabels: Record<string, string> = {};
  const evidenceFields = Array.isArray(document.evidence?.datum_fields)
    ? document.evidence.datum_fields.map(columnOf)
    : [];

  const categoryKeys = categoryColumn ? distinctInOrder(rows, categoryColumn) : [""];
  // (category, split) -> the INDEX of the row in `result.rows`. An index rather
  // than the row object, because the index is what `datumRowIndex` publishes and
  // what lets `evidence/resolve.ts` reach the row without re-deriving anything.
  const rowIndexByCategoryAndSplit = new Map<string, number>();
  rows.forEach((row, index) => {
    const cat = categoryColumn
      ? row[categoryColumn] === null || row[categoryColumn] === undefined
        ? ""
        : String(row[categoryColumn])
      : "";
    const split = splitMember
      ? row[splitMember] === null || row[splitMember] === undefined
        ? ""
        : String(row[splitMember])
      : "";
    // Last write wins ONLY when the server returned the same (category, split)
    // twice, which its own GROUP BY prevents. Nothing is summed here.
    rowIndexByCategoryAndSplit.set(`${cat}${DATUM_KEY_SEP}${split}`, index);
  });

  const seriesByColumn = new Map(series.map((s) => [s.valueColumn, s]));

  for (const category of categoryKeys) {
    const line: CellValue[] = categoryColumn ? [category] : [];
    for (const v of valueColumns) {
      const key = `${category}${DATUM_KEY_SEP}${v.splitValue ?? ""}`;
      const rowIndex = rowIndexByCategoryAndSplit.get(key);
      const row = rowIndex === undefined ? undefined : rows[rowIndex];
      line.push(row ? (row[v.measureId] ?? null) : null);

      // The key is joined with U+0001, not `|`: see `DATUM_KEY_SEP`. A dimension
      // value may contain any printable character, so a printable separator makes
      // two different datums produce one key -- and then one mark's evidence is
      // another mark's row.
      const datumKey = [
        result.content_hash,
        category,
        v.measureId,
        v.splitValue ?? "",
      ].join(DATUM_KEY_SEP);
      datumKeys.push(datumKey);
      datumRowIndex[datumKey] = rowIndex ?? -1;
      if (rowIndex !== undefined) {
        datumTargets[datumKey] = { row_index: rowIndex, field: v.measureId };
      }
      const seriesName = seriesByColumn.get(v.column)?.name ?? v.measureId;
      datumLabels[datumKey] = categoryColumn ? `${category} - ${seriesName}` : seriesName;
      datumFields[datumKey] = [
        ...(categoryColumn ? [categoryColumn] : []),
        ...(splitMember ? [splitMember] : []),
        v.measureId,
        ...evidenceFields,
      ].filter((f, i, a) => a.indexOf(f) === i);
    }
    source.push(line);
  }

  // ---- axes, legend, formats -------------------------------------------
  const axes: { x: CompiledAxis; y: CompiledAxis } = {
    x: {
      scale: document.axes.x.scale,
      zeroBaseline: document.axes.x.zero_baseline,
      tickDensity: document.axes.x.tick_density,
      column: categoryColumn,
      isDate: categoryIsDate,
      label: categoryColumn ? labelFor(document, categoryColumn, memberOfColumn) : null,
    },
    y: {
      scale: document.axes.y.scale,
      zeroBaseline: document.axes.y.zero_baseline,
      tickDensity: document.axes.y.tick_density,
      column: null,
      isDate: false,
      label: measures.length === 1 ? labelFor(document, measures[0]!, memberOfColumn) : null,
    },
  };

  const fieldNames = resultFieldNames(result);
  const tableColumns: CompiledTableColumn[] = [];
  for (const name of fieldNames) {
    const isMeasure = measures.includes(name);
    tableColumns.push({
      key: name,
      label: labelFor(document, name, memberOfColumn),
      isDate: rows.length > 0 && looksLikeDate(rows[0]![name] ?? null) && !isMeasure,
      role: isMeasure ? "measure" : "dimension",
    });
  }

  const volume = evaluateVolume(
    {
      rows: rows.length,
      series: series.length,
      cells: rows.length * Math.max(1, fieldNames.length),
    },
    options.limits,
  );

  return {
    family: document.family,
    dataset: { dimensions: datasetDimensions, source },
    encode: series.map((s) => ({
      x: categoryColumn,
      y: s.valueColumn,
      seriesId: s.id,
    })),
    series,
    legend: {
      position: document.legend.position,
      visible: document.legend.visible,
      entries: series.map((s) => ({ id: s.id, name: s.name, color: s.color })),
    },
    axes,
    formats: {
      numberStyle: document.formatting.number_style,
      dateStyle: document.formatting.date_style,
      unit: document.formatting.unit_source === "semantic_view" ? (options.unit ?? null) : null,
    },
    colors: {
      accent: options.palette.accent,
      semanticDirection: document.color.semantic_direction,
      palette: series.map((s) => s.color),
    },
    datumKeys,
    datumFields,
    datumRowIndex,
    datumTargets,
    datumLabels,
    disclosures: disclosuresOf(result, document),
    tableColumns,
    volume,
  };
}
