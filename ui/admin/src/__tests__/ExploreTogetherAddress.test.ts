/**
 * The address `Explore together` composes, end to end — data.md [24].
 *
 * The server composes the handoff correctly and pins the exact versions that
 * were read (`test_the_handoff_pins_the_exact_versions_that_were_read` asserts
 * `latest` never appears in it). What was missing was everything after: the
 * console's exit carried three identifiers, the router declared only those
 * three, and the Explorer therefore re-resolved the versions from a catalog it
 * fetched itself.
 *
 * This file walks the address once, in the order it is really travelled:
 * handoff -> six values -> what the router keeps -> the pins the Explorer reads.
 * A break anywhere along it puts the re-resolution back, silently.
 */
import { validateSectionQuery } from "../shell/navigation";
import { exploreTogetherPins, readHandoffPins } from "../analyze/explorer/handoffPins";

const HANDOFF = {
  left: { datastream_id: "ds_a" },
  right: { datastream_id: "ds_b" },
  explore_together: {
    datastreams: [
      {
        datastream_id: "ds_a",
        mapping_version_id: "dmv_a",
        published_execution_id: "dse_a",
        output_version_id: "dov_a",
      },
      {
        datastream_id: "ds_b",
        mapping_version_id: "dmv_b",
        published_execution_id: "dse_b",
        output_version_id: "dov_b",
      },
    ],
    common_key_version_id: "mckv_1",
    view_version_id: "svv_1",
    relationship_name: "spend_to_conversions",
  },
};

const PAIR = {
  left_datastream_id: "ds_a",
  right_datastream_id: "ds_b",
  common_key_version_id: "mckv_1",
};

it("takes the six versions from the handoff block the server composed", () => {
  expect(exploreTogetherPins(HANDOFF)).toEqual({
    left_mapping_version_id: "dmv_a",
    left_output_version_id: "dov_a",
    left_published_execution_id: "dse_a",
    right_mapping_version_id: "dmv_b",
    right_output_version_id: "dov_b",
    right_published_execution_id: "dse_b",
  });
});

it("pins nothing for a match that is not executable", () => {
  // A candidate carries no `explore_together`: there is no version to pin, and
  // taking one off `left` / `right` would be inventing the handoff.
  expect(exploreTogetherPins({ ...HANDOFF, explore_together: null })).toEqual({});
});

it("carries all nine parameters through the router that validates the address", () => {
  const composed = { ...PAIR, ...exploreTogetherPins(HANDOFF) };
  const { query, rejected } = validateSectionQuery("analyze", "explore", composed);

  expect(rejected).toEqual([]);
  expect(query).toEqual(composed);
});

it("refuses a version that says `latest`, like every other pin of this section", () => {
  const { query, rejected } = validateSectionQuery("analyze", "explore", {
    ...PAIR,
    ...exploreTogetherPins(HANDOFF),
    left_output_version_id: "latest",
  });

  expect(rejected.map((entry) => entry.code)).toContain("forbidden_value");
  expect(query.left_output_version_id).toBeUndefined();
  // And half a pin pins nothing: the five that remain are dropped with it.
  expect(query.right_output_version_id).toBeUndefined();
  expect(query.left_mapping_version_id).toBeUndefined();
  // The pair itself still opens the Explorer; it is the VERSIONS that are gone.
  expect(query.left_datastream_id).toBe("ds_a");
});

it("reads back exactly what was handed over", () => {
  const composed = { ...PAIR, ...exploreTogetherPins(HANDOFF) };
  const pins = readHandoffPins(composed, "ds_a", "ds_b");

  expect(pins).toEqual({
    left: {
      datastreamId: "ds_a",
      mappingVersionId: "dmv_a",
      outputVersionId: "dov_a",
      publishedExecutionId: "dse_a",
    },
    right: {
      datastreamId: "ds_b",
      mappingVersionId: "dmv_b",
      outputVersionId: "dov_b",
      publishedExecutionId: "dse_b",
    },
  });
});

it("reads no pin at all from an address that carries only the pair", () => {
  // An older shared link, or one typed by hand. `null` is not permission to
  // follow the current heads: it says nothing was ever pinned, and the Explorer
  // states which of the two it is looking at.
  expect(readHandoffPins(PAIR, "ds_a", "ds_b")).toBeNull();
});
