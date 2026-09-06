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

/**
 * ------------------------------------------------------------------ story 76-6
 *
 * The two things the fleet list owed its reader beyond the state WORDS above:
 * a key to the marks it draws, and the console's one reading of an age.
 */
import { render, screen, within } from "@testing-library/react";
import DataWorkspace from "../shell/pages/DataWorkspace";
import { DATA_STATE_MEANING } from "../data/DataCollectionLayout";
import * as dataSurface from "../data/dataSurface";

/**
 * WHY THIS IS A SOURCE RULE AND NOT ONLY A RENDER TEST.
 *
 * `formatRelative` is the console's one answer to "how long ago"
 * (`console-presentation.md` §2 and its second amendment). Two files had built
 * their own ladder — `<1h` / `26h` / `33d` — and one of them, `DataWorkspace#age`,
 * was migrated by 76-1 and named in that amendment as migrated. Its twin in
 * `WorkbenchOverviewPage#ageSince` was not, and the fleet list and the
 * Datastream it opens printed the same publication two different ways for a
 * month.
 *
 * Neither ladder calls `Intl` or `.toFixed(`, so `FormattingIsCentral` — which
 * greps for both — could not see either. This is the shape a grep gate misses:
 * a formatter written entirely in arithmetic and template strings. The ratchet
 * starts at zero over the two trees this story owns, and the second test plants
 * one so the first cannot pass by looking at nothing.
 */
const AGE_LADDER = /`\s*\$\{[^}]+\}\s*(?:h|d|m|min|s)\s*`|["'`]<\s*1\s*(?:h|m|s)["'`]/g;

const TREE = import.meta.glob(
  ["../shell/pages/Data*.tsx", "../data/**/*.tsx", "../datastreams/**/*.tsx"],
  { eager: true, query: "?raw", import: "default" },
) as Record<string, string>;

function withoutComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/^\s*\/\/.*$/gm, " ");
}

function ageLaddersIn(source: string): string[] {
  return [...withoutComments(source).matchAll(AGE_LADDER)].map((match) => match[0]);
}

describe("an age has one reading in Data, and it is the console's", () => {
  it("holds no private relative-time ladder in the Data or Datastream trees", () => {
    const offenders: string[] = [];
    for (const [path, source] of Object.entries(TREE)) {
      for (const found of ageLaddersIn(source)) offenders.push(`${path}: ${found}`);
    }
    expect(
      offenders,
      `a second relative-time vocabulary is being built by hand: ${offenders.join(", ")}. `
        + "Ask `formatRelative` — `console-presentation.md` §2, amendment 2.",
    ).toEqual([]);
    // Not vacuous: the glob must actually be reading the tree.
    expect(Object.keys(TREE).length).toBeGreaterThan(20);
  });

  it("fires on a planted ladder, so the ratchet cannot pass by reading nothing", () => {
    const planted = `
      function age(hours: number) {
        if (hours < 1) return "<1h";
        if (hours < 48) return \`\${hours}h\`;
        return \`\${Math.floor(hours / 24)}d\`;
      }
    `;
    expect(ageLaddersIn(planted).length).toBeGreaterThanOrEqual(3);
    // And a comment describing the defect is not the defect.
    expect(ageLaddersIn('// it answered `${hours}h` and "<1h"')).toEqual([]);
  });
});

/**
 * THE KEY THE FLEET DREW SIX MARKS WITHOUT — `console-presentation.md` §3:
 * « Any screen that shows three tones or more mounts it once, in its
 * `PageHeader`. »
 *
 * Every other Data collection has had one since 76-2 because they all render
 * through `DataCollectionLayout`. The fleet builds its own thirteen-column
 * table, so the fix never reached it: it is the same screen family, the same
 * marks, and no key. Amendment 23 adds the half that makes a key honest — the
 * entries are DERIVED from the rows on the page, so a healthy fleet does not
 * explain a red nobody can see.
 */
function fleetEnvelope(states: Array<Record<string, string>>) {
  return {
    status: "ready" as const,
    refreshing: false,
    envelope: {
      schema_version: "data-datastreams.v1",
      project_ref: { object_type: "project", id: "proj_EXAMPLE" },
      generated_at: "2026-07-29T10:00:00Z",
      evidence_as_of: "2026-07-29T09:45:00Z",
      items: states.map((state, index) => ({
        object_ref: { object_type: "datastream", id: `ds_EXAMPLE_${index}` },
        name: `Fleet member ${index}`,
        lens: "datastreams",
        source_kind: "connector_pull",
        connector_ref: { id: "google-analytics" },
        states: state,
        evidence: {},
        links: {},
      })),
      unavailable_reasons: [],
      allowed_actions: [],
    },
  };
}

function renderFleet(states: Array<Record<string, string>>) {
  vi.spyOn(dataSurface, "useDataSurface").mockReturnValue({
    // eslint-disable-next-line @typescript-eslint/no-explicit-any -- the hook's
    // envelope type is the wire shape; the fixture is that shape, narrowed.
    state: fleetEnvelope(states) as any,
    reload: vi.fn(),
  });
  return render(<DataWorkspace projectId="proj_EXAMPLE" />);
}

describe("the fleet says what its marks mean", () => {
  afterEach(() => { vi.restoreAllMocks(); });

  it("mounts one key, and its entries are the tones the rows actually carry", async () => {
    renderFleet([
      { lifecycle: "active", health: "healthy" },
      { lifecycle: "archived", health: "failed" },
    ]);
    const key = await screen.findByRole("list", { name: "What a Datastream mark means" });
    const entries = within(key).getAllByRole("listitem");
    const words = entries.map((entry) => entry.textContent ?? "");
    // The two tones these rows draw, in the scale's order, with the Data
    // workspace's own sentences — never a sentence written on this screen.
    expect(words.some((word) => word.includes(DATA_STATE_MEANING.success.label))).toBe(true);
    expect(words.some((word) => word.includes(DATA_STATE_MEANING.error.label))).toBe(true);
    // And nothing about a mark no row carries.
    expect(words.some((word) => word.includes(DATA_STATE_MEANING.info.label))).toBe(false);
  });

  it("spells a state with the union's word, never the stored token", async () => {
    renderFleet([{ lifecycle: "active", health: "healthy" }]);
    // `TableScroll` names the region; the `<table>` inside it carries no name.
    const fleet = await screen.findByRole("region", { name: "Datastream fleet" });
    const body = fleet.textContent ?? "";
    expect(body).toContain("Active");
    expect(body).not.toMatch(/(^|[^A-Za-z])active([^A-Za-z]|$)/);
    // A mode is the wire's word with the base's punctuation taken off, not
    // lower-cased prose: it read `connector pull` next to the wizard's
    // `Connector pull`.
    expect(body).toContain("Connector pull");
    expect(body).not.toContain("connector pull");
  });

  it("says Unknown for a state nobody sent, not Unavailable", async () => {
    // Amendment 16: `unavailable` asserts the server stated an absence. An
    // empty cell asserts nothing, and this row's lifecycle is empty.
    renderFleet([{ health: "healthy" }]);
    const fleet = await screen.findByRole("region", { name: "Datastream fleet" });
    expect(fleet.textContent ?? "").toContain("Unknown");
  });
});
