/**
 * The repair drawer — S3 and the import half of S4 of `unresolved-values.md`.
 *
 * ONE COMPONENT FOR BOTH SURFACES, and the target says so in its own heading:
 * "S3 — the repair drawer, **one component for both surfaces**". S1 (a
 * Datastream's `Map` tab) and S2 (the project-wide `Value Tables` lens) open the
 * same drawer on the same row shape, because two drawers would be two places
 * free to disagree about what a rule reaches and what it would touch.
 *
 * THE FIVE SECTIONS ARE IN THE DOCUMENT'S ORDER, and the order is the argument:
 *
 *   1. the value, its occurrences, its window — what is being repaired;
 *   2. the destination — the table already assigned, or the choice of one, or
 *      `Create a table` inline. A drawer that made you leave to create a table
 *      would be the defect CLAUDE.md names: "un écran qui renvoie ailleurs pour
 *      l'action qu'il réclame est inachevé";
 *   3. the match mode and, live, **the reach of the rule** — "this rule also
 *      matches 42 of the 128 remaining values", list foldable. The target gives
 *      the reason in one line: that sentence is what turns 200 pairs into one
 *      rule, and it must be visible BEFORE the confirmation;
 *   4. the impact — what depends on the table, and the version this write would
 *      mint. An impact that could not be read prints `unknown` and DISABLES the
 *      confirm: "I could not check" is not "nothing depends on this";
 *   5. `Confirm`, then WHAT THE SAME READING ANSWERS WHEN IT IS TAKEN AGAIN.
 *
 * POINT 5 CHANGED ON 2026-08-30, and the measurement is why. The drawer used to
 * print "It applies at the next read of this window" the instant the write
 * returned, and offer to "Reprocess the window". Neither was true. The pairs go
 * into `app.value_mapping_entries`; the list that showed the gap resolves
 * `app.dimension_value_mappings`, and no module in this repository carries a row
 * from one store to the other — which is the whole of the ratified page's Open
 * question 4, an arbitration this component does not take. So the drawer now
 * asks the panel for THE SAME READING again and prints what it answered: still
 * listed, no longer listed, or could not be read. The reprocess button is gone
 * rather than wired: a replay cannot make a reading resolve a store it never
 * consults, so the button was a gesture that repaired nothing, and a button that
 * does nothing is the defect.
 *
 * A RULE IS UNFOLDED, NOT STORED. `app.value_mapping_entries` holds a
 * `source_value` and a canonical value, and no match mode (migration 235:117).
 * So `contains` and `regex` are not persisted as patterns: at the confirmation
 * the server's reach hands back every matched value and one pair is written per
 * value, as a single import — one act, one version. The drawer says this rather
 * than letting a person believe a value arriving tomorrow will be caught.
 *
 * EVERY SENTENCE THAT REFUSES COMES FROM THE SERVER. The reach sentence, the
 * invalid-pattern message, the write mode, the note about the destination that
 * is not built — all arrive in the payload. A sentence composed here is a second
 * answer free to drift from the first, which is the defect this whole surface
 * was written to avoid.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Badge,
  Button,
  Field,
  Input,
  NativeSelect,
  Separator,
  SheetInline,
  Status,
} from "../ui";
import { ApiError, apiPost } from "../lib/apiFetch";
import type { UnresolvedEnvelope, UnresolvedGroup, UnresolvedRow } from "./UnresolvedValuesPanel";

export interface ValueMappingTableSummary {
  id: string;
  name: string;
  scope_level: string;
  entry_count: number | null;
  assignment_count: number | null;
  datastream_count: number | null;
  current_version_id: string | null;
}

interface Reach {
  valid: boolean;
  message: string | null;
  sentence: string;
  also_matches: number | null;
  remaining: number;
  occurrences: number | null;
  sample: { source_value: string; occurrences: number | null }[];
  sample_truncated: boolean;
  matched_values: string[];
  corpus_truncated: boolean;
}

interface ImportPreview {
  would_import_count: number;
  rejected_count: number;
  would_import: { source_value: string; canonical_value: string }[];
  rejected: { line: number; reason: string; content: string }[];
  over_limit: boolean;
  limit: number;
  impact: {
    impact_state: "known" | "unknown";
    datastream_count: number | null;
    assignment_count: number | null;
    message?: string;
  } | null;
}

/** The rejection vocabulary of `parse_pairs`, read from a map. A screen that
 *  printed `empty_canonical_value` at somebody would be speaking the parser. */
const REJECTION_LABEL: Record<string, string> = {
  not_two_columns: "This line does not carry exactly two columns",
  empty_canonical_value: "The canonical value is empty",
  duplicate_source_value: "This source value appears twice in the file",
  already_present: "The table already carries this source value — it is not overwritten",
};

/** `unknown`, never `0` — the rule the whole surface is built on. */
function known(value: number | null | undefined): string {
  return typeof value === "number" ? String(value) : "unknown";
}

/** What a write may claim, decided by RE-READING and never by the write's own
 *  `200`. Exported because the import dialog writes into the same store through
 *  the same route: two copies of this rule would be two screens free to disagree
 *  about whether one repair landed. */
export type WriteVerdict = "still_listed" | "cleared" | "unknown";

/**
 * Does the reading, taken again, still carry the values that were just written?
 *
 * `unknown` is a real answer and it is the safe one: a reading that could not be
 * taken, a dimension that could not be read, or a list that stopped at its bound
 * cannot tell the difference between "the value went away" and "the value was
 * not looked at". Reading a bounded list as `cleared` would be the fabricated
 * green this whole surface exists to refuse.
 */
export function verdictAfterWrite(
  reading: UnresolvedEnvelope | null,
  datastreamId: string,
  dimension: string,
  written: string[],
): WriteVerdict {
  if (!reading || reading.state !== "measured") return "unknown";
  const mine = (reading.groups ?? []).filter(
    (one) => one.datastream_id === datastreamId && one.dimension === dimension,
  );
  if (mine.some((one) => one.state !== "measured" || one.truncated)) return "unknown";
  const wanted = new Set(written);
  const listed = mine.some((one) =>
    (one.values ?? []).some((value) => wanted.has(value.source_value)),
  );
  return listed ? "still_listed" : "cleared";
}

export default function UnresolvedRepairDrawer({
  projectId,
  row,
  group,
  payload,
  tables,
  onClose,
  onRepaired,
  onCreateTable,
}: {
  projectId: string;
  row: UnresolvedRow;
  group: UnresolvedGroup;
  payload: UnresolvedEnvelope;
  /** The tables this project may write into, read once by the panel. */
  tables: ValueMappingTableSummary[];
  onClose: () => void;
  /** The panel re-reads after a write and HANDS THAT READING BACK: the drawer
   *  never patches the list it came from, or the two would disagree about what
   *  is left — and it never claims the write landed, it waits for this answer.
   *  `null` means the re-read did not happen, which is not a success. */
  onRepaired: (summary: {
    written: number;
    tableId: string;
  }) => Promise<UnresolvedEnvelope | null>;
  /** `Create a table` inline — the drawer asks the panel to mint one and hand
   *  it back, rather than composing a second creation path of its own. */
  onCreateTable: (name: string) => Promise<ValueMappingTableSummary>;
}) {
  const assigned = group.destination_table?.table_id ?? "";
  const [tableId, setTableId] = useState<string>(assigned);
  const [creating, setCreating] = useState(false);
  const [newTableName, setNewTableName] = useState("");
  const [canonical, setCanonical] = useState("");
  const [mode, setMode] = useState<string>("exact");
  const [pattern, setPattern] = useState<string>(row.source_value);
  const [reach, setReach] = useState<Reach | null>(null);
  const [reachPending, setReachPending] = useState(false);
  const [reachBroken, setReachBroken] = useState<string | null>(null);
  const [showMatches, setShowMatches] = useState(false);
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [broken, setBroken] = useState<string | null>(null);
  const [done, setDone] = useState<{ written: number; verdict: WriteVerdict } | null>(
    null,
  );

  const modes = payload.repair_drawer.match_modes ?? [{ value: "exact", label: "exact" }];
  const table = tables.find((one) => one.id === tableId) ?? null;

  /**
   * THE REACH, ASKED WHILE THE PERSON TYPES — and debounced, because it reads
   * the whole unresolved set on the server. `latest` guards the ordering: two
   * keystrokes in flight can land out of order, and a stale answer painted over
   * a fresh one is a number the person can act on that is not about their rule.
   */
  const latest = useRef(0);
  useEffect(() => {
    if (!group.datastream_id) return;
    const token = ++latest.current;
    setReachPending(true);
    const timer = setTimeout(() => {
      void (async () => {
        try {
          const answer = await apiPost<Reach>(
            `/api/projects/${encodeURIComponent(projectId)}/datastreams/${encodeURIComponent(group.datastream_id)}/unresolved-values/reach`,
            {
              dimension: group.dimension,
              pattern,
              match_mode: mode,
              exclude: row.source_value,
            },
          );
          if (token !== latest.current) return;
          setReach(answer);
          setReachBroken(null);
        } catch (error) {
          if (token !== latest.current) return;
          // A reach that could not be read leaves NO number behind: a stale
          // count under a new pattern is worse than no count at all.
          setReach(null);
          setReachBroken(
            error instanceof ApiError && error.body && typeof error.body === "object"
              ? String(
                  (error.body as { message?: string; sentence?: string }).sentence ??
                    (error.body as { message?: string }).message ??
                    "The reach of this rule could not be read.",
                )
              : "The reach of this rule could not be read.",
          );
        } finally {
          if (token === latest.current) setReachPending(false);
        }
      })();
    }, 250);
    return () => clearTimeout(timer);
  }, [projectId, group.datastream_id, group.dimension, pattern, mode, row.source_value]);

  /** Every value this confirmation would write a pair for: the one on screen,
   *  plus whatever the rule reaches. Sorted and de-duplicated here so the
   *  preview and the write see the same file. */
  const wouldWrite = useMemo(() => {
    const all = new Set<string>([row.source_value]);
    for (const value of reach?.matched_values ?? []) all.add(value);
    return [...all].sort();
  }, [row.source_value, reach]);

  const fileText = useMemo(
    () =>
      ["source_value,canonical_value", ...wouldWrite.map((v) => `${v},${canonical}`)].join("\n"),
    [wouldWrite, canonical],
  );

  const askPreview = useCallback(async () => {
    if (!tableId || !canonical.trim()) return;
    setBusy(true);
    setBroken(null);
    try {
      const answer = await apiPost<ImportPreview>(
        `/api/projects/${encodeURIComponent(projectId)}/value-mapping-tables/${encodeURIComponent(tableId)}/import/preview`,
        { text: fileText },
      );
      setPreview(answer);
    } catch (error) {
      setPreview(null);
      setBroken(
        error instanceof ApiError
          ? error.message
          : "This repair could not be checked, so nothing was written.",
      );
    } finally {
      setBusy(false);
    }
  }, [projectId, tableId, canonical, fileText]);

  const confirm = useCallback(async () => {
    if (!tableId || !preview) return;
    setBusy(true);
    setBroken(null);
    try {
      const answer = await apiPost<{ imported_count: number }>(
        `/api/projects/${encodeURIComponent(projectId)}/value-mapping-tables/${encodeURIComponent(tableId)}/import`,
        { text: fileText },
      );
      // THE WRITE'S OWN `200` IS NOT THE ANSWER. What a person came here to
      // learn is whether the list that showed the gap still shows it, so the
      // same list is read again and its verdict is what gets printed.
      const reading = await onRepaired({ written: answer.imported_count, tableId });
      setDone({
        written: answer.imported_count,
        verdict: verdictAfterWrite(
          reading,
          group.datastream_id,
          group.dimension,
          wouldWrite,
        ),
      });
    } catch (error) {
      setBroken(
        error instanceof ApiError
          ? error.message
          : "Nothing was written. Try again, or check the destination table.",
      );
    } finally {
      setBusy(false);
    }
  }, [projectId, tableId, fileText, preview, onRepaired, group, wouldWrite]);

  /**
   * WHAT HOLDS THE CONFIRM, in the order a person meets it. One reason at a
   * time, and each names the gesture: "Choose a destination" is actionable,
   * "invalid state" is not.
   */
  const blocker: string | null = !tableId
    ? "Choose the table this pair goes into, or create one."
    : !canonical.trim()
      ? "Type the canonical value these source values should read as."
      : reachBroken
        ? reachBroken
        : reach && !reach.valid
          ? (reach.message ?? "This rule cannot run.")
          : !preview
            ? "Check this repair first — nothing is written before you have seen what it would do."
            : preview.over_limit
              ? `This repair carries more than ${preview.limit} pairs. Narrow the rule.`
              : preview.impact?.impact_state === "unknown"
                ? (preview.impact.message ??
                  "What depends on this table could not be read, so this repair is held.")
                : preview.would_import_count === 0
                  ? "This repair would write nothing: every value it reaches is already in the table."
                  : null;

  return (
    // 1 — LA VALEUR, SES OCCURRENCES, SA FENETRE : c'est le titre du panneau,
    // pas une ligne de plus dedans. `SheetInline` porte son propre `title` et
    // son `description`, et un second en-tete ecrit a la main serait un
    // deuxieme endroit libre de nommer la valeur autrement.
    <SheetInline
      title={row.source_value || "(a blank value)"}
      description={`${known(row.occurrences)} occurrences in ${group.dimension}, between ${payload.window.start} and ${payload.window.end}.`}
      onClose={onClose}
      data-testid="unresolved-repair-drawer"
    >
      <div className="flex flex-col gap-4 p-5">

        {/* 2 — the destination. */}
        <Field label="Destination table">
          {(field) =>
            creating ? (
              <div className="flex gap-2">
                <Input
                  {...field}
                  value={newTableName}
                  placeholder="Name this table"
                  data-testid="repair-new-table-name"
                  onChange={(event) => setNewTableName(event.target.value)}
                />
                <Button
                  size="sm"
                  disabled={!newTableName.trim() || busy}
                  data-testid="repair-create-table"
                  onClick={() => {
                    setBusy(true);
                    void onCreateTable(newTableName.trim())
                      .then((created) => {
                        setTableId(created.id);
                        setCreating(false);
                        setNewTableName("");
                      })
                      .catch((error: unknown) =>
                        setBroken(
                          error instanceof ApiError
                            ? error.message
                            : "That table was not created.",
                        ),
                      )
                      .finally(() => setBusy(false));
                  }}
                >
                  Create
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setCreating(false)}>
                  Cancel
                </Button>
              </div>
            ) : (
              <div className="flex gap-2">
                <NativeSelect
                  {...field}
                  value={tableId}
                  data-testid="repair-destination"
                  onChange={(event) => {
                    setTableId(event.target.value);
                    setPreview(null);
                  }}
                >
                  <option value="">Choose a table…</option>
                  {tables.map((one) => (
                    <option key={one.id} value={one.id}>
                      {one.name}
                    </option>
                  ))}
                </NativeSelect>
                <Button
                  size="sm"
                  variant="secondary"
                  data-testid="repair-create-inline"
                  onClick={() => setCreating(true)}
                >
                  Create a table
                </Button>
              </div>
            )
          }
        </Field>

        <Field label="Canonical value">
          {(field) => (
            <Input
              {...field}
              value={canonical}
              placeholder="What this value should read as"
              data-testid="repair-canonical"
              onChange={(event) => {
                setCanonical(event.target.value);
                setPreview(null);
              }}
            />
          )}
        </Field>

        <Separator />

        {/* 3 — the match mode, and the reach of the rule. */}
        <Field label="Match mode">
          {(field) => (
            <NativeSelect
              {...field}
              value={mode}
              data-testid="repair-match-mode"
              onChange={(event) => {
                setMode(event.target.value);
                setPreview(null);
              }}
            >
              {modes.map((one) => (
                <option key={one.value} value={one.value}>
                  {one.label}
                </option>
              ))}
            </NativeSelect>
          )}
        </Field>

        {mode !== "exact" && (
          <Field label="Pattern">
            {(field) => (
              <Input
                {...field}
                value={pattern}
                data-testid="repair-pattern"
                onChange={(event) => {
                  setPattern(event.target.value);
                  setPreview(null);
                }}
              />
            )}
          </Field>
        )}

        {/* THE SENTENCE. Live, before the confirmation, in the server's words. */}
        <div aria-live="polite" data-testid="repair-reach">
          {reachBroken ? (
            <Status as="block" tone="error" title="The reach of this rule is unknown">
              {reachBroken}
            </Status>
          ) : reachPending && !reach ? (
            <p className="text-caption text-text-secondary">Reading what this rule reaches…</p>
          ) : reach ? (
            <>
              <p className={reach.valid ? "text-body-sm" : "text-body-sm text-status-error"}>
                {reach.sentence}
              </p>
              {reach.valid && (reach.also_matches ?? 0) > 0 && (
                <>
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-expanded={showMatches}
                    data-testid="repair-toggle-matches"
                    onClick={() => setShowMatches((open) => !open)}
                  >
                    {showMatches ? "Hide the values it matches" : "Show the values it matches"}
                  </Button>
                  {showMatches && (
                    <ul className="mt-2 max-h-48 overflow-y-auto text-caption" data-testid="repair-matches">
                      {reach.sample.map((one) => (
                        <li key={one.source_value}>
                          {one.source_value} — {known(one.occurrences)} occurrences
                        </li>
                      ))}
                      {reach.sample_truncated && (
                        <li className="text-text-secondary">
                          …and {(reach.also_matches ?? 0) - reach.sample.length} more. All of
                          them are repaired; only this list is cut.
                        </li>
                      )}
                    </ul>
                  )}
                </>
              )}
              {reach.valid && (reach.occurrences ?? 0) > 0 && (
                <p className="text-caption text-text-secondary">
                  {known(reach.occurrences)} further occurrences would be repaired with it.
                </p>
              )}
            </>
          ) : null}
        </div>

        {/* A rule is UNFOLDED, and the person is told so rather than assuming
            a pattern is kept. */}
        {mode !== "exact" && (
          <p className="text-caption text-text-secondary" data-testid="repair-unfold-note">
            {payload.repair_drawer.note}
          </p>
        )}

        <Separator />

        {/* 4 — the impact, read before the click and never inferred. */}
        {preview ? (
          <div data-testid="repair-impact">
            <p className="text-body-sm">
              {preview.would_import_count} pair{preview.would_import_count === 1 ? "" : "s"} would
              be written into {table?.name ?? "this table"}.
            </p>
            {preview.impact?.impact_state === "known" ? (
              <p className="text-caption text-text-secondary">
                {known(preview.impact.datastream_count)} Datastream
                {preview.impact.datastream_count === 1 ? "" : "s"} read this table
                {table?.current_version_id
                  ? ". This write mints the next version of it."
                  : ". This write mints its first version."}
              </p>
            ) : preview.impact ? (
              <Status as="block" tone="warning" title="Impact unknown">
                {preview.impact.message}
              </Status>
            ) : null}
            <p className="text-caption text-text-secondary">{payload.import.write_mode_note}</p>
            {preview.rejected_count > 0 && (
              <ul className="mt-2 text-caption" data-testid="repair-rejected">
                {preview.rejected.map((one) => (
                  <li key={`${one.line}-${one.reason}`}>
                    Line {one.line}: {REJECTION_LABEL[one.reason] ?? one.reason}
                  </li>
                ))}
              </ul>
            )}
          </div>
        ) : (
          <p className="text-caption text-text-secondary">
            Nothing is written until you have seen what this repair would do.
          </p>
        )}

        {broken && (
          <Status as="block" tone="error" title="Nothing was written" data-testid="repair-broken">
            {broken}
          </Status>
        )}

        {/* 5 — confirm, then WHAT THE SAME READING ANSWERED. Three verdicts,
            three titles, and the sentence under each is the server's. There is
            no fourth branch and no default congratulation: "written" is a fact
            about a table, and this panel is about a list. */}
        {done ? (
          <div data-testid="repair-done">
            <Status
              as="block"
              tone={
                done.verdict === "cleared"
                  ? "success"
                  : done.verdict === "still_listed"
                    ? "warning"
                    : "warning"
              }
              title={
                done.verdict === "cleared"
                  ? "Written, and the list no longer carries it"
                  : done.verdict === "still_listed"
                    ? "Written, and the list still carries it"
                    : "Written, and the list could not be read again"
              }
            >
              {done.written} pair{done.written === 1 ? "" : "s"} written into{" "}
              {table?.name ?? "the value table"}.{" "}
              {done.verdict === "cleared"
                ? payload.repair_drawer.after_write.cleared_note
                : done.verdict === "still_listed"
                  ? payload.repair_drawer.after_write.still_listed_note
                  : payload.repair_drawer.after_write.unknown_note}
            </Status>
            <Button size="sm" className="mt-3" onClick={onClose}>
              Close
            </Button>
          </div>
        ) : (
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              variant="secondary"
              disabled={busy || !tableId || !canonical.trim()}
              data-testid="repair-check"
              onClick={() => void askPreview()}
            >
              Check this repair
            </Button>
            <Button
              size="sm"
              disabled={blocker !== null || busy}
              title={blocker ?? undefined}
              data-testid="repair-confirm"
              onClick={() => void confirm()}
            >
              Confirm
            </Button>
            <Button size="sm" variant="ghost" onClick={onClose}>
              Cancel
            </Button>
          </div>
        )}
        {blocker && !done && (
          <p className="text-caption text-text-secondary" data-testid="repair-blocker">
            {blocker}
          </p>
        )}

        {/* The destination S4 names and this drawer does not serve. */}
        <Badge tone="neutral">{payload.import.destination}</Badge>
        <p className="text-caption text-text-secondary" data-testid="repair-other-destination">
          {payload.import.other_destination_note}
        </p>
      </div>
    </SheetInline>
  );
}
