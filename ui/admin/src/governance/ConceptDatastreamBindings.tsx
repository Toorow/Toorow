/**
 * Which Datastream supplies each Concept of a Semantic View.
 *
 * WHY THIS STEP EXISTS. A View published without these bindings is composable
 * and NOT executable: `query_execution` answers `unavailable` with
 * `missing_link: semantic_view_version_bindings`, a sentence that names a table
 * and no gesture. Four console files send semantic change sets and not one of
 * them carried `bindings`, so every View a person could build from the screen
 * was a View that would never answer. Measured 2026-08-12 in production, where
 * the bindings had to be written through the API by hand.
 *
 * IT PROPOSES RATHER THAN ASKS. The product already knows which Datastream feeds
 * which canonical field -- `GET /api/datamodel/fields/{name}` returns `used_by`,
 * the same read the Data model's "Fed by" panel shows. So a Concept fed by one
 * Datastream is bound without a question, and only a Concept fed by several is
 * an arbitration. `views` is fed by seven feeds on a YouTube project; `country`
 * by one. Asking both would be asking a question that has no second answer.
 *
 * CHANTIER B -- SEVERAL FEEDS ARE BOUND, AND THE QUESTION CHANGED. This screen
 * used to ask, of a Concept fed by several Datastreams, "which one measures it",
 * and bind that one. Measured in production on 2026-08-14: eight of the ten
 * Datastreams with an active mapping carry `views`, and the View held one
 * binding -- so "views by country" was refused as a cross-source question while
 * the country Datastream carried the measure and the dimension by itself.
 *
 * Every feed is bound now. What is asked instead is the one thing that cannot be
 * derived and that the product must never decide alone: **which feed holds the
 * total**. That answer is a Governance declaration of its own -- it outlives this
 * View -- and it is what lets a breakdown state its distance from the total
 * instead of quietly standing in for it.
 *
 * AND IT DOES NOT PIN THE MAPPING VERSION. The screen names the Datastream; the
 * server pins its current published mapping inside the same transaction. A
 * console that pinned the version it read a moment earlier would bind a View to
 * a version nobody chose.
 */
import { useEffect, useRef, useState } from "react";
import { apiGet } from "../lib/apiFetch";
import { Label, NativeSelect, Status } from "../ui";

export interface ConceptChoice {
  /** `<concept_id>|<version_id>`, the value the picker carries. */
  value: string;
  /** The Concept's own name -- the canonical field the mapping targets. */
  name: string;
  label: string;
}

export interface FeedOption {
  datastream_id: string;
  datastream_name: string;
}

/** `concept_id` -> the `datastream_id` that holds this Concept's TOTAL.
 *
 *  It is no longer what the intent's `bindings` are built from -- every feed is
 *  bound (see `ConceptFeeds`). This is the one answer a person still owes, and
 *  only for a Concept several Datastreams feed. */
export type BindingChoice = Record<string, string>;

/** `concept_id` -> every `datastream_id` that feeds it. The View binds them all. */
export type ConceptFeeds = Record<string, string[]>;

type FeedsState =
  | { status: "loading" }
  | { status: "ready"; byConcept: Record<string, FeedOption[]> }
  | { status: "error"; message: string };

interface UsedByEntry {
  datastream_id: string;
  datastream_name: string;
}

export default function ConceptDatastreamBindings({
  projectId,
  concepts,
  value,
  onChange,
  onPendingChange,
  onFeedsChange,
  disabled,
}: {
  projectId: string;
  concepts: ConceptChoice[];
  value: BindingChoice;
  onChange: (next: BindingChoice) => void;
  /** The Concepts that have SEVERAL feeds and no choice yet -- the only ones a
   *  refusal may name. A Concept the dictionary feeds with nothing is stated in
   *  place and never blocks: it is a fact about the Project, and other paths
   *  bind their Datastreams elsewhere (an MDM pair names them on its
   *  relationship, not here). */
  onPendingChange?: (conceptIds: string[]) => void;
  /** Every feed of every Concept, so the caller binds them all rather than one.
   *  Reported only when it changes, for the reason `onPendingChange` gives. */
  onFeedsChange?: (feeds: ConceptFeeds) => void;
  disabled: boolean;
}) {
  const [feeds, setFeeds] = useState<FeedsState>({ status: "loading" });
  const key = concepts.map((concept) => concept.name).join(",");

  useEffect(() => {
    if (!projectId || concepts.length === 0) {
      setFeeds({ status: "ready", byConcept: {} });
      return;
    }
    const controller = new AbortController();
    setFeeds({ status: "loading" });
    Promise.all(
      concepts.map((concept) =>
        apiGet<{ used_by?: UsedByEntry[] }>(
          `/api/datamodel/fields/${encodeURIComponent(concept.name)}` +
            `?project_id=${encodeURIComponent(projectId)}`,
          { signal: controller.signal },
        )
          .then((detail) => [concept.name, detail.used_by ?? []] as const)
          // A field the dictionary does not know yet is not an error: it is a
          // Concept nothing feeds, and the panel says exactly that below.
          .catch(() => [concept.name, [] as UsedByEntry[]] as const),
      ),
    )
      .then((pairs) => {
        if (controller.signal.aborted) return;
        const byConcept: Record<string, FeedOption[]> = {};
        for (const [name, rows] of pairs) {
          const seen = new Set<string>();
          byConcept[name] = rows
            .filter((row) => row.datastream_id && !seen.has(row.datastream_id)
              && seen.add(row.datastream_id) !== undefined)
            .map((row) => ({
              datastream_id: row.datastream_id,
              datastream_name: row.datastream_name,
            }));
        }
        setFeeds({ status: "ready", byConcept });
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        setFeeds({
          status: "error",
          message: reason instanceof Error ? reason.message : "The request failed.",
        });
      });
    return () => controller.abort();
  }, [projectId, key, concepts.length]);

  // EVERY FEED IS REPORTED, so the caller binds every one of them. Same
  // change-only guard as `onPendingChange` below: the caller rebuilds
  // `concepts` on each render, and an unconditional callback loops.
  const lastFeeds = useRef<string>("");
  useEffect(() => {
    if (!onFeedsChange || feeds.status !== "ready") return;
    const next: ConceptFeeds = {};
    for (const concept of concepts) {
      const conceptId = concept.value.split("|")[0];
      next[conceptId] = (feeds.byConcept[concept.name] ?? []).map(
        (option) => option.datastream_id,
      );
    }
    const signature = JSON.stringify(next);
    if (signature === lastFeeds.current) return;
    lastFeeds.current = signature;
    onFeedsChange(next);
  }, [feeds, concepts, onFeedsChange]);

  // A single feed is not a decision. Binding it silently is what keeps the
  // question count down to the arbitrations that are real.
  useEffect(() => {
    if (feeds.status !== "ready") return;
    const next: BindingChoice = { ...value };
    let changed = false;
    for (const concept of concepts) {
      const options = feeds.byConcept[concept.name] ?? [];
      const conceptId = concept.value.split("|")[0];
      if (options.length === 1 && !next[conceptId]) {
        next[conceptId] = options[0].datastream_id;
        changed = true;
      }
    }
    if (changed) onChange(next);
  }, [feeds, concepts, value, onChange]);

  // REPORTED ONLY WHEN IT CHANGES. The caller rebuilds the `concepts` array on
  // every render, so an effect that called back unconditionally re-rendered the
  // caller, which rebuilt the array, which ran the effect again -- the dialog
  // hung instead of failing, which is worse.
  const lastPending = useRef<string>("");
  useEffect(() => {
    if (!onPendingChange) return;
    const pending =
      feeds.status !== "ready"
        ? []
        : concepts
            .filter((concept) => (feeds.byConcept[concept.name] ?? []).length > 1)
            .map((concept) => concept.value.split("|")[0])
            .filter((conceptId) => !value[conceptId]);
    const signature = pending.join(",");
    if (signature === lastPending.current) return;
    lastPending.current = signature;
    onPendingChange(pending);
  }, [feeds, concepts, value, onPendingChange]);

  if (concepts.length === 0) return null;

  return (
    <div className="space-y-1.5">
      <Label htmlFor="view-bindings">Where each Concept is measured</Label>
      {feeds.status === "loading" && (
        <p className="mb-0 text-caption text-text-secondary">
          Reading which Datastreams feed these Concepts…
        </p>
      )}
      {feeds.status === "error" && (
        <Status as="block" tone="error" title="The Datastreams could not be read">
          {feeds.message} Without them a View can be published and will answer
          nothing, so the step stops here rather than binding a guess.
        </Status>
      )}
      {feeds.status === "ready" && (
        <div className="grid gap-2">
          {concepts.map((concept) => {
            const conceptId = concept.value.split("|")[0];
            const options = feeds.byConcept[concept.name] ?? [];
            if (options.length === 0) {
              return (
                <Status
                  key={concept.value}
                  as="block"
                  tone="warning"
                  title={`No Datastream feeds ${concept.label}`}
                >
                  This Project collects nothing that maps to <code>{concept.name}</code>.
                  Map it on a Datastream, or leave this Concept out of the View.
                </Status>
              );
            }
            if (options.length === 1) {
              return (
                <p key={concept.value} className="mb-0 text-body">
                  <strong>{concept.label}</strong> — {options[0].datastream_name}
                </p>
              );
            }
            return (
              <div key={concept.value} className="space-y-1">
                <Label htmlFor={`bind-${conceptId}`}>
                  {concept.label} — which Datastream holds the total?
                </Label>
                <p className="mb-0 text-caption text-text-secondary">
                  All {options.length} Datastreams that measure it are bound, so it
                  can be asked at each of their grains. Name the one that holds the
                  whole figure: the others are then read as breakdowns of it, and
                  each states its distance from it.
                </p>
                <NativeSelect
                  id={`bind-${conceptId}`}
                  value={value[conceptId] ?? ""}
                  disabled={disabled}
                  onChange={(event) =>
                    onChange({ ...value, [conceptId]: event.target.value })
                  }
                >
                  <option value="">Choose the Datastream that holds the total…</option>
                  {options.map((option) => (
                    <option key={option.datastream_id} value={option.datastream_id}>
                      {option.datastream_name}
                    </option>
                  ))}
                </NativeSelect>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
