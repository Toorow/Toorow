/**
 * One declared noun, read by every surface that names an object type.
 *
 * The console had two spellings of the same answer: `OBJECT_TYPE_LABEL`
 * (`governance/contracts.ts`) for the workbench and the collection, and
 * `sentenceCase(route.objectType)` (`shell/TopBar.tsx`) for the breadcrumb. A
 * type absent from the first was printed RAW by one reader and sentence-cased by
 * the other, which is how `tracked-entity` appeared on a screen whose lens is
 * called Competitor Registry and whose object `README.md:103` calls a
 * Competitor.
 *
 * Ratifié Jean 2026-09-01 (`governance.md`, *Amendment, 2026-09-01*): the
 * displayed noun is declared ONCE, on the object's route contract in the
 * navigation registry, and the wire token stays `tracked-entity` because
 * renaming it is a migration with a backfill.
 *
 * The test that matters is not "the label is Competitor" — it is that the two
 * readers cannot disagree, and that nothing about the ADDRESS moved.
 */
import { findObjectContract, objectTypeLabel } from "../shell/navigation";
import { objectLabel } from "../governance/contracts";
import { buildPath, parsePath } from "../shell/router";

it("declares the displayed noun once, in the navigation registry", () => {
  expect(
    findObjectContract("governance", "master-data", "tracked-entity")?.label,
  ).toBe("Competitor");
  expect(objectTypeLabel("tracked-entity")).toBe("Competitor");
});

it("gives the governance readers the SAME noun the breadcrumb reads", () => {
  // `objectLabel` is what the workbench title and the collection's type column
  // call; `objectTypeLabel` is what the breadcrumb calls. One declaration.
  expect(objectLabel("tracked-entity")).toBe(objectTypeLabel("tracked-entity"));
});

// The third case of this file asserted the OPPOSITE until story 76-3: that
// `objectTypeLabel("semantic-view")` was `null` and `objectLabel("not-a-type")`
// returned its token — true while 36 of the 37 contracts declared nothing and
// `OBJECT_TYPE_LABEL` was still the second store. `console-presentation.md` §4
// deleted that store, so the assertion moved to
// `ObjectTypesDeclareTheirNoun.test.tsx`, which reads all 37 rather than one.
// It is NOT restated here: two files asserting one property is how they drift.

it("changes NOTHING about the address — the wire token stays `tracked-entity`", () => {
  const path =
    "/org/org_1/project/proj_1/governance/master-data/object/tracked-entity/ent_1/tab/coverage";
  const parsed = parsePath(path);
  expect(parsed.kind).toBe("resolved");
  if (parsed.kind !== "resolved") return;
  expect(parsed.route.objectType).toBe("tracked-entity");
  expect(parsed.route.tab).toBe("coverage");
  expect(buildPath(parsed.route)).toBe(path);
  // And no route is minted under the displayed noun.
  expect(
    parsePath(
      "/org/org_1/project/proj_1/governance/master-data/object/competitor/ent_1/tab/coverage",
    ).kind,
  ).toBe("unknown");
});
