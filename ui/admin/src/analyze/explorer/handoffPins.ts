/**
 * The versions `Explore together` hands over, and the address that carries them.
 *
 * data.md, *Which Datastreams can usefully be crossed* (ratified 2026-08-13):
 * `Explore together` opens the Analyze Explorer with **the exact Datastream and
 * Output versions that were on the screen**, not their current heads — "a handoff
 * that re-resolves `latest` hands off a different question than the one that was
 * read".
 *
 * WHY THIS IS ONE MODULE AND NOT TWO HALVES. The composer lives in the shell
 * (`objectSurfaces.tsx`, the Datastream Workbench's exit) and the reader lives in
 * the Explore page; the parameter names are the same grammar seen from both
 * sides, and a grammar spelled twice is a grammar that drifts. `vocabulary.ts`
 * declares the same six names to the router, which is what makes the address
 * survive being shared.
 *
 * WHAT IS NOT HERE. Nothing composes an address: the router does that. This
 * module only turns a match into the six values, and six values back into a pair
 * of pinned sides.
 */

/** The exact versions one side of the cross was read at. */
export interface HandoffSidePins {
  datastreamId: string;
  mappingVersionId: string;
  outputVersionId: string;
  publishedExecutionId: string;
}

export interface HandoffPins {
  left: HandoffSidePins;
  right: HandoffSidePins;
}

/** The shape the handoff arrives in, narrowed to what a pin needs. */
interface HandoffSource {
  left: { datastream_id: string };
  right: { datastream_id: string };
  explore_together: {
    datastreams: {
      datastream_id: string;
      mapping_version_id: string;
      published_execution_id: string;
      output_version_id: string;
    }[];
  } | null;
}

/** One version the address pinned and the catalog no longer agrees with. */
export interface PinDisagreement {
  side: "left" | "right";
  datastreamId: string;
  what: "mapping version" | "Output version" | "published run";
  pinned: string;
  current: string;
}

const PARAMETERS = [
  "left_mapping_version_id",
  "left_output_version_id",
  "left_published_execution_id",
  "right_mapping_version_id",
  "right_output_version_id",
  "right_published_execution_id",
] as const;

/**
 * The six query values a match hands over, or `{}` when it hands over none.
 *
 * A match with no `explore_together` block is a candidate, and a candidate is not
 * executable at all — it carries no versions to pin, and inventing them from
 * `left` / `right` would be the re-resolution this file exists to prevent.
 */
export function exploreTogetherPins(match: HandoffSource): Record<string, string> {
  const handoff = match.explore_together;
  if (!handoff) return {};
  const left = handoff.datastreams.find(
    (entry) => entry.datastream_id === match.left.datastream_id,
  );
  const right = handoff.datastreams.find(
    (entry) => entry.datastream_id === match.right.datastream_id,
  );
  // Half a pin pins nothing: the router refuses an incomplete group anyway, so
  // an address that could only carry one side carries neither and says the pair.
  if (!left || !right) return {};
  return {
    left_mapping_version_id: left.mapping_version_id,
    left_output_version_id: left.output_version_id,
    left_published_execution_id: left.published_execution_id,
    right_mapping_version_id: right.mapping_version_id,
    right_output_version_id: right.output_version_id,
    right_published_execution_id: right.published_execution_id,
  };
}

/**
 * The pins an Explore address carries, or `null` when it carries none.
 *
 * `null` is not "current heads are fine": it is an address that never pinned
 * anything — a link typed by hand, or one shared before the pins were carried.
 * What the Explorer does with each case is its own decision, stated there.
 */
export function readHandoffPins(
  query: Record<string, string> | undefined,
  leftDatastreamId: string,
  rightDatastreamId: string,
): HandoffPins | null {
  if (!query) return null;
  if (PARAMETERS.some((name) => !query[name])) return null;
  return {
    left: {
      datastreamId: leftDatastreamId,
      mappingVersionId: query.left_mapping_version_id,
      outputVersionId: query.left_output_version_id,
      publishedExecutionId: query.left_published_execution_id,
    },
    right: {
      datastreamId: rightDatastreamId,
      mappingVersionId: query.right_mapping_version_id,
      outputVersionId: query.right_output_version_id,
      publishedExecutionId: query.right_published_execution_id,
    },
  };
}

/** The side of a catalog match a pin belongs to, matched by Datastream. */
type PinnableSide = {
  datastream_id: string;
  mapping_version_id: string;
  published_execution_id: string;
  output_version_id: string;
};

type PinnableMatch = {
  left: PinnableSide;
  right: PinnableSide;
  explore_together: {
    datastreams: PinnableSide[];
    [key: string]: unknown;
  } | null;
};

function sideFor(pins: HandoffPins, datastreamId: string): HandoffSidePins | null {
  if (pins.left.datastreamId === datastreamId) return pins.left;
  if (pins.right.datastreamId === datastreamId) return pins.right;
  return null;
}

function pinSide<T extends PinnableSide>(side: T, pins: HandoffPins): T {
  const pin = sideFor(pins, side.datastream_id);
  if (!pin) return side;
  return {
    ...side,
    mapping_version_id: pin.mappingVersionId,
    output_version_id: pin.outputVersionId,
    published_execution_id: pin.publishedExecutionId,
  };
}

/**
 * The catalog's match, wearing the versions the handoff pinned.
 *
 * THE WHOLE POINT OF THE FILE. The Explorer fetches its own catalog, and every
 * request it composes is built from the sides of the selected match. Applied
 * here, once, the pinned versions travel into `members` — and into the plan the
 * server compiles — without the request builder needing to know a handoff
 * happened. The alternative, patching the versions at request time, leaves every
 * OTHER reader of the match (the graph panel, the profile call) describing a
 * different reading from the one being executed.
 *
 * Both the sides and the `explore_together` block are moved together: a match
 * whose two halves disagreed about which version it names would be a new
 * ambiguity in place of the one this repairs.
 */
export function applyHandoffPins<T extends PinnableMatch>(match: T, pins: HandoffPins): T {
  return {
    ...match,
    left: pinSide(match.left, pins),
    right: pinSide(match.right, pins),
    explore_together: match.explore_together
      ? {
          ...match.explore_together,
          datastreams: match.explore_together.datastreams.map((side) => pinSide(side, pins)),
        }
      : null,
  };
}

/**
 * Where the catalog read now disagrees with what the address pinned.
 *
 * An empty list means the two agree — never "we did not look". The Explorer
 * shows this rather than resolving it: the pinned versions are what was on the
 * screen, and choosing the newer ones for the person is exactly the silent
 * re-resolution the amendment forbids.
 */
export function describePinDisagreements(
  match: PinnableMatch,
  pins: HandoffPins,
): PinDisagreement[] {
  const found: PinDisagreement[] = [];
  for (const side of ["left", "right"] as const) {
    const current = match[side];
    const pin = sideFor(pins, current.datastream_id);
    if (!pin) continue;
    const compared: [PinDisagreement["what"], string, string][] = [
      ["mapping version", pin.mappingVersionId, current.mapping_version_id],
      ["Output version", pin.outputVersionId, current.output_version_id],
      ["published run", pin.publishedExecutionId, current.published_execution_id],
    ];
    for (const [what, pinned, catalogued] of compared) {
      if (pinned !== catalogued) {
        found.push({
          side,
          datastreamId: current.datastream_id,
          what,
          pinned,
          current: catalogued,
        });
      }
    }
  }
  return found;
}
