/**
 * The console's mirror of `server/core/run_origins.py` -- story 63.7.
 *
 * WHY A MIRROR AND NOT A LOCAL GUESS. A long treatment whose origin has been
 * forgotten reads as an anomaly: somebody opens the Workbench, sees a run in
 * flight on a Datastream nobody scheduled, and goes hunting. Five paths mint an
 * execution -- two collections, a day re-collection, a mapping or plan change,
 * and the first candidate of a Datastream -- and until this story none of them
 * reached a screen: `datastream_workbench` popped `projection_plan_ref` off the
 * payload and the progress route never carried the field at all.
 *
 * The table below is compared, entry by entry, against the Python registry by
 * `server/tests/conformance/test_run_origin_registry.py`. Adding an origin on
 * one side without the other is a red test, not a screen that lies.
 *
 * AND NOTHING HERE DECIDES. `has_engine` is the server's measurement of its own
 * build -- whether anything would carry an execution of that origin to a
 * terminal state -- and the console only reads it. The refusal sentence a person
 * sees when they press Synchronize, Reload or Reprocess is written by the
 * server, in `run_origins.refusal_message`, so the tools and the screen answer
 * the same words.
 */
import { dateTime } from "./evidence";

export type RunOriginEntry = {
  /** What `projection_plan_ref.origin` holds. Never shown to a person. */
  readonly key: string;
  /** The English words a person reads. */
  readonly label: string;
  /** Does a run of this origin pull windows from a provider? */
  readonly reads_provider_windows: boolean;
  /** Does anything in this build carry such an execution to a terminal state? */
  readonly has_engine: boolean;
};

export const RUN_ORIGINS: readonly RunOriginEntry[] = [
  { key: "scheduler_nightly", label: "Nightly collection", reads_provider_windows: true, has_engine: true },
  { key: "scheduler_hourly", label: "Hourly collection", reads_provider_windows: true, has_engine: true },
  { key: "refetch", label: "Day re-collection", reads_provider_windows: true, has_engine: true },
  { key: "manual_run", label: "Manual run", reads_provider_windows: true, has_engine: true },
  { key: "mapping_change", label: "Mapping change", reads_provider_windows: false, has_engine: true },
  { key: "plan_change", label: "Plan change", reads_provider_windows: false, has_engine: true },
  { key: "first_candidate", label: "First candidate", reads_provider_windows: false, has_engine: true },
  { key: "country_activation", label: "Country activation", reads_provider_windows: false, has_engine: true },
  { key: "bounded_synchronize", label: "Synchronize", reads_provider_windows: true, has_engine: false },
  { key: "bounded_reload", label: "Reload", reads_provider_windows: true, has_engine: false },
  // BUILT 2026-08-17 (chantier 67-15b). The panel that names the three verbs
  // reads `has_engine` from this mirror, so Reprocess leaves the refusal list by
  // itself -- which is why the panel was written to read the registry rather
  // than type the verbs out. Synchronize and Reload stay refused: they are
  // RETIRED, not unbuilt.
  { key: "bounded_reprocess", label: "Reprocess", reads_provider_windows: false, has_engine: true },
];

const BY_KEY = new Map(RUN_ORIGINS.map((entry) => [entry.key, entry]));

export function runOrigin(value: unknown): RunOriginEntry | undefined {
  return BY_KEY.get(String(value ?? ""));
}

/**
 * What a person reads for this origin, or `null` when there is nothing to read.
 *
 * `null` covers the two absences a screen must keep apart from each other only
 * in tone, never in truth: a run minted before this story, which carries no
 * origin at all, and an origin this build has never heard of. The first reads
 * *Not measured*; the second is shown exactly as it arrived, UNPAINTED -- a
 * console older than its server may not fold an unknown reason onto a
 * neighbouring one.
 */
export function originLabel(value: unknown): string | null {
  return runOrigin(value)?.label ?? null;
}

/** The raw key, when there is one -- what an unknown origin is shown as. */
export function originKey(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

/**
 * Would a run of this origin pull provider windows?
 *
 * `false` for anything unknown, which is silence rather than a claim: a screen
 * that assumed an unrecognised origin reads no window would explain a missing
 * progress bar with a reason it made up.
 */
export function readsProviderWindows(value: unknown): boolean {
  return runOrigin(value)?.reads_provider_windows ?? false;
}

/** Is this an origin the registry knows? Decides painted vs shown as received. */
export function isKnownOrigin(value: unknown): boolean {
  return runOrigin(value) !== undefined;
}

/**
 * The one sentence that says why this treatment is running, and since when.
 *
 * WRITTEN ONCE, because two surfaces show it side by side: the Workbench object
 * header and the `Runs` tab render from one component, and
 * `DatastreamRunLiveParity.test.tsx` compares their text character for
 * character.
 *
 * IT IS AN INSTANT, NOT "4 MINUTES AGO", AND THAT IS DELIBERATE. `started_at`
 * is a SERVER timestamp; the only clock this file could subtract it from is the
 * browser's. `datastreamProgress.ts` refuses that arithmetic for the estimate
 * for the same reason -- the moment one clock runs ahead, the screen
 * manufactures a duration nobody measured. The age this band DOES show is the
 * age of the READING, whose two ends are both browser instants.
 */
export function originSentence(origin: unknown, startedAt: unknown): string | null {
  const key = originKey(origin);
  if (key === null) return null;
  const label = originLabel(key) ?? key;
  // A RUN THAT HAS NOT STARTED IS NOT A RUN « STARTED Unavailable » -- amendment
  // 8 of the 2026-08-11 review, and the half of it this file was missing.
  //
  // `started_at` is written by `open_collection_run` (migration 218), so it is
  // NULL by construction on every run still in `created`. `dateTime(null)` is
  // the string `Unavailable`, and gluing it into this clause produced « First
  // candidate, started Unavailable » -- which reads as a measurement that
  // FAILED, on a run whose start simply has not happened. Jean read exactly that
  // line and could not tell what it meant.
  //
  // The clause goes; the label stays. Why the run exists is known whether or not
  // it has begun, and the band below already says it has not.
  const started = dateTime(startedAt);
  if (started === "Unavailable") return label;
  return `${label}, started ${started}`;
}
