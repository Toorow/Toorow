/**
 * The shape of `datastream_daily_breakdown.v1`, as the route publishes it.
 *
 * EXTRACTED, NOT REWRITTEN — lot B2. `DateBreakdownGrid.tsx` was 1088 lines and
 * the repository refuses a file over 1000, so the contract moved out before the
 * pairing moved in. Every declaration below is the one that was there, and
 * `DateBreakdownGrid` re-exports them so no importer had to be touched.
 *
 * NOT ONE OF THESE FIELDS IS COMPOSED IN A BROWSER. Every `reason` is a machine
 * name and every `message` a sentence written on the server; the screen renders
 * them verbatim, because the same wording has to appear on a greyed control and
 * in the `422` a forced call earns.
 */

export interface BreakdownColumn {
  source_field: string;
  /** Nullable BY DESIGN: an unbound field is a state of the mapping, and it is
   *  the line a person opens this tab to repair. */
  target_field: string | null;
  is_key_column: boolean;
  binding_status?: string | null;
}

export interface BreakdownDay {
  date: string;
  extract_status: string | null;
  job_state: string | null;
  extract_count: number | null;
  row_count: number | null;
  row_count_reason: string | null;
  rows: number | null;
  rows_reason: string | null;
  execution_id: string | null;
  /**
   * AI-307 — present only on a day whose window is `prevented`, and the two
   * halves of one fact.
   *
   * `prevented_reason` is the connector's machine token for the gate that
   * refused; `prevented_message` is its sentence, and the sentence is the point:
   * « did not allow this collection » is a state, and a person needs the gesture
   * — « Request the reviews allowlist for this project, then re-ask these
   * dates ». It is authored in the connector module, carried on the window and
   * rendered VERBATIM, exactly like every other `message` on this payload.
   */
  prevented_reason?: string | null;
  prevented_message?: string | null;
}

/**
 * One position of the reading selector, as the SERVER decides it — story 58.3,
 * arbitrage 2.
 *
 * `reason` is a SENTENCE, written server-side, and it is rendered verbatim. Not
 * one word of it may be composed here: the same sentence has to appear on the
 * greyed control and in the `422` a forced call earns, and two wordings of one
 * refusal is how a person ends up quoting the wrong one back.
 */
export interface ViewModeOption {
  mode: string;
  available: boolean;
  reason: string | null;
  relation: string | null;
}

/**
 * Which column of a reading is an AMOUNT, and what explains it — story 58.7.
 *
 * THE COMPONENT DESIGNATES NOTHING. The server names the columns, the roles and
 * the sentences; the screen matches a column name against the list it was sent and
 * draws what came with it. A cell that looked monetary because of its spelling
 * would be a classification invented in a browser, and story 48.3 already removed
 * the same reasoning from the server (`unit IS NOT NULL` called most of the
 * catalogue money).
 *
 * The role names are the mart's — `native_currency`, `fx_rate`, `fx_as_of_date` —
 * and the values are the relation's own column names. `reporting_currency` is
 * NEVER on this payload and never composed here: 0 project of 18 has confirmed
 * one, and the amount on screen is the source's own. The line says the door that
 * confirms it instead of naming a currency nobody chose.
 */
export interface MoneyColumn {
  column: string;
  native_currency: string | null;
  native_value: string | null;
  fx_rate: string | null;
  fx_as_of_date: string | null;
}

/** One row's provenance, per designated column — already masked server-side. */
export interface MoneyRowValues {
  native_currency: string | null;
  native_value: string | null;
  fx_rate: string | null;
  fx_as_of_date: string | null;
  money_gap_code: string | null;
  money_gap_message: string | null;
}

export interface MoneyProvenance {
  columns: MoneyColumn[];
  /** Why nothing is designated. `null` when something is. */
  reason: string | null;
  message: string | null;
  /** The HEADING of that reason, from the same authority as its sentence. Two of
   *  the five codes mean the amount IS present and its rate is not, so a heading
   *  written here contradicted the body under it for half the vocabulary. */
  title: string | null;
  /** Absent by decision, not by omission — never rendered as a currency. */
  reporting_currency: string | null;
  reporting_currency_reason: string | null;
  reporting_currency_gate: string | null;
  rows: Record<string, MoneyRowValues>[];
}

/** One side of the reading: the rows, or the named absence that replaces them. */
export interface ReadingSide {
  relation: string | null;
  zone: string | null;
  columns: string[];
  rows: Record<string, unknown>[] | null;
  row_count: number | null;
  truncated: boolean;
  masked_fields: string[];
  reason: string | null;
  message: string | null;
  note: string | null;
  note_message: string | null;
  /** Story 58.7. `undefined` is a route that sent no provenance at all, which is
   *  a DIFFERENT sentence from a provenance that came back empty. */
  money_provenance?: MoneyProvenance | null;
}

/**
 * One paired column of a day's reading — amendment 12, lot B2.
 *
 * The pair comes from the ACTIVE MAPPING and from nowhere else: `source_field` is
 * a column of the collected relation, `target_field` a column of the mapped one,
 * and the mapping is what says the first becomes the second. Two names that happen
 * to match are not a pair here, and never were.
 */
export interface PairedColumn {
  source_field: string;
  target_field: string;
  is_key_column: boolean;
}

/** One cell: the raw value, the value it becomes, and whether that changed it. */
export interface PairedCell extends PairedColumn {
  /** Already masked server-side, exactly as every other value of this reading. */
  raw: unknown;
  mapped: unknown;
  /** `null` on a row that has only one side: "unchanged" and "nothing to compare
   *  it with" are two different sentences and only one is an observation. */
  changed: boolean | null;
  /**
   * The currency, the rate and the rate's date under each half — lot B3.
   *
   * ABSENT, NOT `null`, WHEN `currency_fx` IS OFF: the server writes these keys
   * only for a capability the Project turned on, so nothing here holds a space
   * open for a projection nobody asked for. `null` with the capability ON means
   * that side of this cell has no amount to explain — an unpaired row, or a
   * column no Concept designates.
   */
  raw_money?: MoneyRowValues | null;
  mapped_money?: MoneyRowValues | null;
}

export interface PairedRow {
  /** `null` when the row IS paired; otherwise the side it came from, so a row
   *  with no counterpart never reads as a row whose other half is empty. */
  side: "collected" | "mapped" | null;
  reason: string | null;
  message: string | null;
  key: { field: string; value: unknown }[];
  /** Which row of each side this row is — lot B3. The index the join already
   *  knows, published so every block travelling parallel to a side's rows reaches
   *  the same row without matching on a value a second time. */
  collected_index?: number | null;
  mapped_index?: number | null;
  cells: PairedCell[];
}

/** A column of one relation that stayed on its own side, and why. */
export interface UnpairedColumn {
  name: string;
  side: "collected" | "mapped";
  reason: string;
  message: string | null;
}

/**
 * The two readings put on ONE ROW — amendment 12, lot B2.
 *
 * `available: false` is an ANSWER, not a hole: it carries the server's sentence
 * saying which of the refusals it is — no mapping, no bound field, no column both
 * relations carry, no key column — and the screen renders that sentence and
 * composes none of its own.
 */
export interface ReadingPairing {
  available: boolean;
  reason: string | null;
  message: string | null;
  columns: PairedColumn[];
  key_columns: { source_field: string; target_field: string }[];
  unpaired_columns: UnpairedColumn[];
  rows: PairedRow[];
  row_count: number;
  paired_row_count: number;
  unpaired_row_count: number;
  truncated: boolean;
  bounded_at: number | null;
}

/**
 * What an ACTIVE Project capability does to this reading — lot B3, amendment 11.
 *
 * « Une capacité activée AJOUTE UN ONGLET, OU COLORE LE CHAMP DANS L'APERÇU —
 * jamais une liste de modules. » So this is never rendered as a row saying what a
 * capability is called and what state it is in: `effect` says WHAT it does to the
 * data and `fields` says WHICH columns of each side it lands on. Off, the
 * capability produces no entry at all and the screen has nothing to draw.
 *
 * THE STATE IS THE SERVER'S. `ready` and `degraded` are the only two that get
 * here — the same threshold that opens the `Cost` tab — and the browser never
 * derives it. `reason` is set when the capability is on and has nothing to land
 * on, and it names the gesture that would give it something.
 */
export type ReadingCapabilityEffect =
  | "money_line"
  | "field_marked"
  | "day_boundary_signal";

export interface ReadingCapability {
  key: string;
  state: string;
  degraded: boolean;
  effect: ReadingCapabilityEffect | string;
  title: string;
  message: string | null;
  reason: string | null;
  fields: { collected: string[]; mapped: string[] };
  /** Only on `reporting_timezone`, and only when a Policy is confirmed. The
   *  module SIGNALS the boundary and re-aligns no day — no value of this reading
   *  moves because this is present. */
  project_reporting_timezone?: string | null;
}

export interface DayReading {
  day: string;
  collected: ReadingSide;
  mapped: ReadingSide;
  /** `undefined` is a route that sent no pairing block at all — a different
   *  sentence from a pairing that refused, and the screen says both. */
  pairing?: ReadingPairing | null;
  /** Empty when no capability of this Project touches this reading, which is the
   *  answer on every live project today. `undefined` is a route that does not
   *  answer the question at all. */
  capabilities?: ReadingCapability[] | null;
}

/**
 * One value of a day's country unfolding — story 58.5.
 *
 * `kind` is what keeps the no-country bucket from being read as a place, and
 * `label` is the ONLY thing this screen draws: the mart's identity for the absence
 * (`__country_absent__`) must never reach a person, and rendering `value` anywhere
 * is how it would.
 */
export interface CountryValue {
  value: string;
  kind: string;
  label: string;
  rows: number;
}

/** The country capability, as the SERVER decides it — never derived here. */
export interface CountrySplit {
  capability_state: string;
  active: boolean;
  degraded?: boolean;
  /** Why there is no split: the capability, the connector, or the mart. */
  reason?: string | null;
  /** The one refusal that owns a ratified sentence (ambiguous materialisation). */
  note?: string | null;
  bounded_at?: number;
  /** Absent — never empty — when nothing was measured. `country_count` counts NAMED
   *  countries and is `0` on a day whose rows all landed in the absence bucket; the
   *  rows of that day live on the entry beside it, and the two are never one
   *  number. */
  days?: Record<string, { values: CountryValue[]; country_count: number }> | null;
}

export interface DailyBreakdown {
  connector: string | null;
  window: { start: string; end: string; bounded_at: number; bound_reached: boolean };
  columns: BreakdownColumn[];
  columns_reason: string | null;
  /** Which of the two mapping stores answered — `read_mapping_columns`. */
  columns_source: string | null;
  /**
   * Can the FIELDS be drawn as columns over the DAY GRID — and no, they cannot:
   * the mart is long-form and its `metric` is a dbt literal.
   *
   * IT SAYS NOTHING ABOUT THE READING. Reading this flag as "the collected and
   * mapped readings of a day cannot be paired" is what shipped an explanation in
   * place of the epic's central promise; that question is answered by
   * `DayReading.pairing`, whose key is the mapping's own `source → target`.
   */
  column_row_join_available: boolean;
  column_row_join_reason: string | null;
  days: BreakdownDay[];
  reason: string | null;
  rows_note?: string | null;
  /** The country capability and, when it is on, the split — story 58.5. */
  country?: CountrySplit | null;
  /** The four stages with their verdict and, when refused, their sentence. */
  view_mode?: { requested: string; served: string; available: ViewModeOption[] } | null;
  stage_relations?: {
    report_profile_id: string | null;
    collected_relation: string | null;
    mapped_relation: string | null;
    reason: string | null;
    message: string | null;
  } | null;
}
