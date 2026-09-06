/**
 * The fleet list owns no vocabulary of its own — story 58.8, acceptance 5.
 *
 * WHAT THIS GUARDS, AND IT IS A CLASS AND NOT A SCREEN. There are two registries
 * of state words in this console and exactly two:
 *
 *   - `datastreams/workbench/executionStates.ts` — the fourteen states of a RUN,
 *     compared entry by entry against `server/core/execution_states.py` by
 *     `server/tests/conformance/test_execution_state_registry.py`;
 *   - `ui/CoverageBars.tsx` — the six states of a DAY, with the shape that keeps
 *     `empty` and `never_fetched` apart.
 *
 * A screen that types one of those words instead of asking for it becomes an
 * eighth opinion about what a state means. That has already happened once:
 * `WorkbenchRunsPage.phaseState` carried three local `if` lines, migration 218
 * added `collected`, and a run that had collected every window was painted amber
 * and "waiting" while the server considered it finished.
 *
 * WHY THE COLUMN HEADINGS ARE EXCLUDED, deliberately and in writing. `Published`
 * is a column of this table — the publication pointer — and it is also the label
 * of a run state. A heading names an AXIS, not a value; the two collide as
 * strings and never as facts. The header row is therefore removed before the
 * scan, and nothing else is.
 *
 * The second test is the one that gives the first its teeth: it runs the same
 * detector over a planted source and requires it to fire. A guard that cannot
 * fail is a guard that proves nothing, and this one would silently pass if its
 * vocabulary were emptied.
 */
import { describe, expect, it } from "vitest";
import { EXECUTION_STATES, executionStateLabel } from "../datastreams/workbench/executionStates";
import { EXTRACT_STATUS_LABEL } from "../ui";

/*
 * Read through Vite's `import.meta.glob` rather than node:fs, for the reason
 * `apiSeamGuard.test.ts` already gives: this workspace declares
 * `types: ["vitest/globals"]` and carries no @types/node, so the fs route would
 * not typecheck.
 */
const SOURCES = import.meta.glob("../shell/pages/DataWorkspace.tsx", {
  eager: true,
  query: "?raw",
  import: "default",
}) as Record<string, string>;

const FLEET_LIST = "../shell/pages/DataWorkspace.tsx";

/** The one file this guard is about. A missing entry is a moved screen, and it
 *  must fail loudly rather than scan an empty string and pass. */
function fleetSource(): string {
  const source = SOURCES[FLEET_LIST];
  if (!source) throw new Error(`${FLEET_LIST} was not found: the fleet list has moved.`);
  return source;
}

/** Every word a person could read that belongs to one of the two registries. */
function governedVocabulary(): string[] {
  return [
    ...EXECUTION_STATES.map((entry) => executionStateLabel(entry.name)),
    ...Object.values(EXTRACT_STATUS_LABEL),
  ];
}

/**
 * What this file could actually SHOW someone: string literals and JSX text.
 *
 * Comments are stripped first — prose naming a state is documentation, and the
 * headers of these screens are long on purpose. Column headings are stripped for
 * the reason written above.
 */
function displayStrings(source: string): string[] {
  const stripped = source
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/^\s*\/\/.*$/gm, " ")
    .replace(/<TableHead>[\s\S]*?<\/TableHead>/g, " ");
  const quoted = [...stripped.matchAll(/"([^"\\\n]*)"|'([^'\\\n]*)'|`([^`$\\\n]*)`/g)].map(
    (match) => match[1] ?? match[2] ?? match[3] ?? "",
  );
  const jsxText = [...stripped.matchAll(/>([^<>{}]+)</g)].map((match) => match[1]);
  return [...quoted, ...jsxText].map((value) => value.replace(/\s+/g, " ").trim()).filter(Boolean);
}

function governedWordsIn(source: string): string[] {
  const forbidden = new Set(governedVocabulary());
  return displayStrings(source).filter((value) => forbidden.has(value));
}

describe("the fleet list writes no state vocabulary of its own", () => {
  it("shows no run-state or extract-state word it did not ask a registry for", () => {
    const found = governedWordsIn(fleetSource());
    expect(
      found,
      `DataWorkspace.tsx spells a governed state word itself: ${found.join(", ")}. `
        + "Ask executionStateLabel() or EXTRACT_STATUS_LABEL for it instead.",
    ).toEqual([]);
  });

  it("fires on a planted literal, so it cannot pass by having no vocabulary", () => {
    // Both registries are represented: emptying either one must break this.
    const planted = `
      export function Row() {
        return <td><span>${executionStateLabel("loading")}</span><b>${EXTRACT_STATUS_LABEL.never_fetched}</b></td>;
      }
      const table = { failed: "${executionStateLabel("failed")}" };
    `;
    const found = governedWordsIn(planted);
    expect(found).toContain(executionStateLabel("loading"));
    expect(found).toContain(executionStateLabel("failed"));
    expect(found).toContain(EXTRACT_STATUS_LABEL.never_fetched);
    // And the vocabulary itself is not empty — the failure mode this test exists
    // to make impossible.
    expect(governedVocabulary().length).toBeGreaterThan(15);
  });

  it("reaches the run registry through the components that own it", () => {
    const source = fleetSource();
    // The badge resolves the label; a registry reading resolves whether the run
    // is doing anything. Both read `executionStates.ts`; neither lets this file
    // hold an opinion.
    //
    // THE ASSERTION IS THE INTENT, NOT ONE SYMBOL — corrected 2026-08-12. It
    // pinned `isRunMoving` by name, and on that day the fleet moved to
    // `isRunProgressing`: `active` means the run OCCUPIES the Datastream, which
    // is why `created` carries it, and this column was calling every such run a
    // collection. The registry is still the authority — which is all this test
    // exists to protect — so it now accepts either reading and keeps refusing a
    // local table under any name.
    expect(source).toContain("DatastreamRunStateBadge");
    expect(source).toMatch(/isRun(Moving|Progressing)\(/);
    // No local table of run states, under any of the names one would be given.
    expect(source).not.toMatch(/EXECUTION_STATES\s*[:=]/);
    expect(source).not.toMatch(/RUN_STATE_LABELS?\s*[:=]/);
  });
});
