/**
 * ONE WORD PER CONCEPT, AND THE SCREENS THAT NAME ONE READ IT FROM ONE PLACE.
 *
 * Two halves, in one file on purpose. The first asserts the module — the five
 * ratified nouns, their plurals, and the refusal that makes `noun()` a decision
 * rather than a suggestion. The second is the ratchet over the console: the
 * three surfaces `console-presentation.md` §4 names — a column header, a tab or
 * lens label, an empty-state title — may not spell one of these concepts with a
 * word the same table retires. Splitting them would let the lists drift: the
 * grep reads `GLOSSARY[c].never` directly, so a spelling added to the module is
 * refused on the screens from that moment.
 *
 * WHY THOSE THREE SHAPES AND NOT EVERY SENTENCE. Measured 2026-09-05: the eleven
 * retired spellings appear on 517 lines of `ui/admin/src/**.tsx`. Nearly all are
 * wire tokens, imports, or the ordinary English verb — `glossary.md` is explicit
 * that *"the Datastreams that feed this Concept"* is correct and that *"a blind
 * sweep would have renamed those too"*. Narrowed to the three shapes where the
 * word NAMES THE OBJECT, the same census gives 23 hits, of which ten are the
 * legitimate readings listed below. A rule that reported the other 494 would be
 * switched off, and a switched-off guard is how the class came back the last
 * three times.
 *
 * WHAT THIS DELIBERATELY DOES NOT MEASURE, so that two instruments never measure
 * one thing:
 *
 *   * `Module` and `Extension` inside `shell/pages/*.tsx` and the four routing
 *     files — `server/tests/conformance/test_product_vocabulary.py:122,203-209`
 *     has owned exactly that scope since 2026-08-31. This file takes the
 *     COMPLEMENT of it, which is not a duplicate but the hole it leaves: five
 *     visible `module` copies were measured outside that scope
 *     (`DatastreamSetupWizard.tsx`, `SourceConnectorPull.tsx`,
 *     `SourceSelectionPanel.tsx`, `WorkbenchCapabilityPanel.tsx`) and nothing
 *     saw them.
 *   * A bare identifier. `ScreensDoNotPrintIdentifiersAsProse.test.tsx` refuses
 *     one, over every front package, and it is green.
 *   * A stored word inside a sentence, or a tracker reference.
 *     `ScreensDoNotSpeakTheDatabase.test.tsx` refuses those.
 *
 * THE RULE THAT TELLS THE VERB FROM THE NOUN, IN ONE SENTENCE: a retired word
 * is read as the VERB only when `to` introduces it, or when a subject that can
 * ACT stands before it AND a word follows it — so every run that ENDS on the
 * word under a determiner or a plain noun (`Data feed`, `Last pull`, `Create
 * flow`, `Every flow`) is the NOUN and is refused; a QUALIFIER (`host
 * connection`, `event stream`, `Cloud Scheduler job`) clears that word for the
 * whole run, and every occurrence in a run is read, not the first.
 *
 * WHY IT WAS REWRITTEN. The version before it asked only whether the word right
 * before was an article. `Data feed`, `Source stream`, `Last pull`, `Data flow`,
 * `Last sync`, `Create flow`, `Add feed`, `Export stream` and `Every flow` all
 * passed it — nine column headers, buttons and labels naming the object under
 * the retired word, which is the one class this file exists for. And `exec`
 * returned ONE match per string, so *"Columns feed nothing, so open the Feed
 * tab"* was cleared by its verb and its header was never read. Both are proven
 * by a probe carrying those shapes, which this rule turns red.
 *
 * The two shapes the earlier passes could not read are read now: a TEMPLATE
 * LITERAL carrying a space (`{`${n} flows`}`) and BOTH HALVES of a two-word
 * TERNARY (`{ok ? "Feed" : "Stream"}`). Restricting the template shape to a run
 * with a space is what keeps `module-${key}` and `${a}:${b}` — a `data-testid`
 * and a React key — out of a census of what a person reads.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";

import {
  GLOSSARY, GLOSSARY_CONCEPTS, REFUSED_ON_SCREEN, RETIRED_SPELLING, noun,
} from "../ui/glossary";
import { objectTypeLabel } from "../shell/navigation";

const CONSOLE = resolve(__dirname, "..");

describe("the five nouns", () => {
  it("says the word console-presentation.md §4 ratifies", () => {
    expect(noun("datastream")).toBe("Datastream");
    expect(noun("source")).toBe("Source");
    expect(noun("authorization")).toBe("Authorization");
    expect(noun("connector")).toBe("Connector");
    expect(noun("run")).toBe("Run");
  });

  it("declares the plural rather than deriving it", () => {
    expect(noun("datastream", 2)).toBe("Datastreams");
    expect(noun("run", 0)).toBe("Runs");
    // Every entry carries both, so a caller never has to guess.
    for (const concept of GLOSSARY_CONCEPTS) {
      expect(GLOSSARY[concept].singular).not.toBe("");
      expect(GLOSSARY[concept].plural).not.toBe("");
      expect(GLOSSARY[concept].means).not.toBe("");
    }
  });

  it("refuses a retired spelling, and names the word that replaces it", () => {
    for (const [word, concept] of Object.entries(RETIRED_SPELLING)) {
      expect(() => noun(word as never)).toThrow(GLOSSARY[concept].singular);
    }
    // The fifteen of the §4 table, spelled out so a deletion from a `never` map
    // is a failing test and not a silent loosening.
    expect(Object.keys(RETIRED_SPELLING).sort()).toEqual(
      [
        "auth", "connection", "connexion", "credential", "extension", "feed",
        "flow", "job", "module", "origin", "pipeline", "pull", "stream", "sync",
        "tool",
      ].sort(),
    );
  });

  it("says WHY a retired word may still appear, or refuses it on screens", () => {
    // Three spellings carry a ratified second object — `Tool` is an MCP tool,
    // an artifact's `origin` is its provenance, `auth` is a route segment. Each
    // names that object in the module, and only those three sit outside the
    // screen ratchet. An exemption with no reason written beside it would be a
    // hole nobody can audit, so the reason is what the test reads.
    const exempt = Object.entries(RETIRED_SPELLING).filter(
      ([word]) => !(word in REFUSED_ON_SCREEN),
    );
    expect(exempt.map(([word]) => word).sort()).toEqual(["auth", "origin", "tool"]);
    for (const [word, concept] of exempt) {
      const reasons: Readonly<Record<string, string | null>> = GLOSSARY[concept].never;
      expect(reasons[word]).toBeTruthy();
    }
  });

  it("refuses a concept nobody ratified, rather than inventing a word for it", () => {
    expect(() => noun("workspace" as never)).toThrow("not one of the ratified concepts");
  });

  it("agrees with the object-type registry wherever the concept IS an object type", () => {
    // Two stores would be the defect this story deletes. These four concepts are
    // also route contracts; the fifth (`authorization`) is not an addressable
    // object type, and asserting a null for it is the honest reading.
    expect(objectTypeLabel("datastream")).toBe(noun("datastream"));
    expect(objectTypeLabel("connector")).toBe(noun("connector"));
    expect(objectTypeLabel("source-account")).toBe("Source Account");
    expect(objectTypeLabel("authorization")).toBeNull();
  });
});

// --- the ratchet over the screens -------------------------------------------

/** Every `.ts`/`.tsx` of the console except the tests. */
function sources(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === "__tests__" || entry === "node_modules") continue;
      sources(full, out);
      continue;
    }
    if (!/\.tsx?$/.test(entry) || /\.test\.tsx?$/.test(entry)) continue;
    out.push(full);
  }
  return out;
}

/** Comments blanked, offsets preserved. Every one of these files EXPLAINS the
 *  copy it replaced, quoting the retired word verbatim — the trap the two guards
 *  next door hit before stripping comments was the fix for both. */
function withoutComments(source: string): string {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, (block) => block.replace(/[^\n]/g, " "))
    .split("\n")
    .map((line) => (/^\s*\/\//.test(line) ? line.replace(/./g, " ") : line))
    .join("\n");
}

/** WHAT §4 NAMES, AND IT IS NOT THREE SHAPES.
 *
 *  The first pass of this guard read `<TableHead>` text, `label:` and `title=`
 *  and called that "the three surfaces §4 names". §4's own words are wider —
 *  *"consumed by column headers, empty states and tab labels"*, and an EMPTY
 *  STATE is a title AND the sentence under it AND the control that fills it.
 *  Narrowing the criterion to what was easy to grep is how a guard reports zero
 *  on a class it is not reading: the wider read below found eleven more sites in
 *  files the first pass had already walked.
 *
 *  So: every JSX text run a person reads, every `description=` and `title=`,
 *  every declared `label:`. A text run is text BETWEEN TAGS with no expression
 *  in it — which is what a reader sees, and which no attribute, class name or
 *  `data-testid` can reach. */
const SHAPES: readonly (readonly [string, RegExp])[] = [
  // Text BETWEEN TAGS. `>` also opens a TypeScript generic, so a run carrying
  // code punctuation is not prose: `new Set<string>(["general", …])` is not a
  // sentence anybody reads. Requiring a run free of `;([="` keeps the reader's
  // text and drops the compiler's.
  ["text a person reads", />([^<>{};()[\]="]{4,})</g],
  ["a declared label", /\blabel:\s*"([^"]+)"/g],
  ["a title", /\btitle=\{?"([^"]+)"/g],
  ["a description", /\bdescription=\{?"([^"]+)"/g],
  ["a declared description", /\b(?:description|detail|purposeText|note|hint|reason|message):\s*"([^"]+)"/g],
  // An empty state is read through its `title=`, its `description=` and the
  // text run between its tags — the shapes above and below. Capturing the
  // whole opening TAG instead was tried and dropped: it dragged in `tone=`,
  // `data-testid=` and every template expression, which is noise a reader
  // never sees and an exemption list would have had to absorb.
  // A button's own words.
  ["a button", /<Button\b[^>]*>([^<>{}]{4,})</g],
  // A TEMPLATE LITERAL a person reads, restricted to one carrying a SPACE.
  // Without that restriction the shape reported `module-${key}`,
  // `stream-${stream.object_ref.id}` and `${flow.datastream_id}:${flow.column}`
  // — a `data-testid`, a DOM id and a React key, none of which anybody reads.
  // With it, the same census found `${days} days becomes ${forecast} pull…`,
  // which is a Status title on `data/SourceBackfillPanel.tsx`.
  ["a template literal a person reads", /\{`([^`\n]*\s[^`\n]*)`\}/g],
  // BOTH HALVES of a ternary that chooses between two written words —
  // `{ok ? "Feed" : "Stream"}`. Two capture groups, which is why the loop
  // below walks `match.slice(1)` and not `match[1]` alone.
  ["a ternary word", /\?\s*"([^"\n]{2,})"\s*:\s*"([^"\n]{2,})"/g],
];

/**
 * WHERE THE READ STOPS, AND WHY IT IS NOT LAZINESS.
 *
 * A first version of this guard read EVERY capitalised sentence in the console —
 * every string literal of four words or more, wherever it sat. It reported about
 * ninety findings, of which thirteen were the object under a retired name and
 * the rest were correct English: the verb (*"See what it feeds"*), the word
 * qualified by another ratified object (*host connection*, *event stream*,
 * *Cloud Scheduler job*), a delivery secret that really is a credential, an
 * `AnalyticsExplorer` "Measuring the connection…" that is a network round trip.
 *
 * `glossary.md`'s own `Incomplete if` already settled this shape once, for the
 * word **Concept**: *"a first version of the guard that tried flagged eleven
 * correct uses. A guard that cries wolf gets switched off."* The same trade
 * applies here and the same answer is taken — so this reads the surfaces §4
 * enumerates, where the word NAMES the object, and free prose is left to a
 * reader. The hole is written down rather than implied clean.
 */


/** The scope `test_product_vocabulary.py` already owns for `module`/`extension`. */
const SERVER_OWNED = [
  "shell/pages/",
  "shell/ContentRouter.tsx",
  "shell/objectSurfaces.tsx",
  "shell/collectionSurfaces.tsx",
];

/**
 * WHY A QUALIFIER RULE AND NOT A LIST OF NINETY.
 *
 * Widening the read to everything §4 names — every JSX text run, every
 * `description`, `detail`, `title` and `label`, every sentence in a plain
 * string — took the census from 23 to about 90. Thirteen were the object under
 * a retired name and were corrected. The rest are correct English, and they
 * divide into two kinds that a grep CAN tell apart:
 *
 *   * the VERB — *"Excluded columns feed nothing"*, *"See what it feeds"*,
 *     *"which Datastreams feed these Concepts"*. `glossary.md` § **Feed** says
 *     so in as many words, and that a blind sweep *"would have renamed those
 *     too"*;
 *   * the word QUALIFIED by the object it really names — *host* connection,
 *     *event* stream, *managed* feed, *Cloud Scheduler* job, *your* connection.
 *     The qualifier is the evidence: nobody writes "host connection" meaning a
 *     Source.
 *
 * Writing ninety exemptions would have been writing the answer down instead of
 * the rule, and the ninety-first would have gone unread. What is exempted BY
 * NAME below is only what neither rule catches.
 */

/** A retired word is not this concept when one of these stands right before it. */
const QUALIFIER = [
  "host", "hosts", "event", "events", "managed", "your", "network", "internet",
  "scheduler", "scheduled", "background", "import", "delivery", "provider",
  // A file-source Datastream's own noun (`glossary.md` § Feed, the Channel).
  "inbound", "email", "webhook",
];

/** The word is a VERB here, not a noun: it takes a subject and an object. */
const VERB_FORM = /^(?:feeds?|fed|feeding|pulls?|pulled|pulling|syncs?|synced|streams?|streaming|flows?|connects?|connected)$/i;

/** A word that introduces a NOUN. Whatever follows one of these is the object
 *  being NAMED, never the action — which is why `Every flow` is refused. The
 *  first version of this list held eleven articles and let `Every`, `Any`,
 *  `One` and `All` through as if they were subjects. */
const DETERMINER = /^(?:a|an|the|this|that|these|those|each|every|any|some|all|both|either|neither|no|one|another|other|first|last|next|previous|new|single|per|its|our|your|my|their|his|her)$/i;

/** A subject that can ACT, so a verb at the END of a run still has someone to
 *  do it — *"See what it feeds"*. Four of these are determiners too (`that`,
 *  `this`, `these`, `those`); ACTING WINS, because *"a report that feeds on
 *  them"* is the verb and *"this flow"* is caught by the run's other words. */
const ACTOR = /^(?:it|they|we|you|i|he|she|what|which|who|that|this|these|those|nothing|something|everything|anything|none)$/i;

/**
 * The readings neither rule catches, each with the object it really names. One
 * entry is one string on one surface — never a file, because exempting a file
 * exempts the next line somebody writes in it.
 */
const NOT_THIS_CONCEPT: Readonly<Record<string, string>> = {
  // The wizard's two source KINDS, humanised from `connector_pull` and
  // `managed_feed` — the wire tokens epic 76-6 asks to LABEL, not rename.
  "Managed feed": "the managed_feed Channel, glossary.md § Feed",
  "Connector pull": "the source KIND, humanised from the wire token connector_pull",
  // The Data Overview's diagram of the four stages, which is a process.
  "Source-to-publication flow": "the process, not the object",
  // Cloud Scheduler's own noun, on a platform surface.
  "No such job in Cloud Scheduler": "a Cloud Scheduler job",
  "In sync": "a state word, not a collection Run",
  // The delivery secret of a `managed_feed` IS a credential: it is a token
  // anyone holding can deliver with, not a grant to read a Source.
  "No credential issued yet": "the delivery secret of a managed feed",
  "Delivery address": "the delivery address, not an Authorization",
  // TWO RATIFIED DOCUMENTS DISAGREE, AND A GUARD DOES NOT ARBITRATE THAT.
  // `datastream-workbench-and-wizard.md:3685` names this panel in as many
  // words — *"son nom est `Modules`"* (amendment 11) — while `glossary.md:903`
  // retires **Module** as a product noun and `page-structure.md:1038` already
  // records the contradiction. Renaming it here would settle an arbitration by
  // editing a screen. Named, with both lines, until it is settled.
  Modules: "datastream-workbench-and-wizard.md:3685 names the panel; glossary.md:903 retires the noun",
};

function offenders(): string[] {
  const found: string[] = [];
  for (const file of sources(CONSOLE)) {
    const rel = relative(CONSOLE, file).replace(/\\/g, "/");
    // A fixture, a sandbox and the component gallery render invented rows on
    // purpose; their words are demo data, not the product's copy.
    if (/(?:^|\/)(?:sandbox|__fixtures__)\//.test(rel)) continue;
    if (/Sandbox\.tsx$|Fixtures\.ts$|ComponentSheet\.tsx$/.test(rel)) continue;
    const serverOwned = SERVER_OWNED.some((prefix) => rel.startsWith(prefix));
    const source = withoutComments(readFileSync(file, "utf-8"));
    for (const [shape, pattern] of SHAPES) {
      for (const match of source.matchAll(new RegExp(pattern.source, pattern.flags))) {
        // Every capture group, because a ternary writes TWO words.
        for (const captured of match.slice(1)) {
          if (!captured) continue;
          const text = captured.trim();
          if (NOT_THIS_CONCEPT[text]) continue;
          for (const [word, concept] of Object.entries(REFUSED_ON_SCREEN)) {
            if (serverOwned && (word === "module" || word === "extension")) continue;
            // THE QUALIFIER, READ OVER THE WHOLE RUN AND NOT ONE WORD BACK.
            // A paragraph that has once said *host connection* is still about
            // host connections in its next sentence — `orgs/McpHostsPanel.tsx`,
            // *"Revoking a connection takes effect at its next call."* Looking
            // only at the word before the occurrence made that sentence a
            // finding the moment the loop below started reading every match.
            const qualifiedInRun = new RegExp(
              String.raw`\b(?:${QUALIFIER.join("|")})[\s-]+${word}s?\b`, "i",
            ).test(text);
            if (qualifiedInRun) continue;
            // EVERY OCCURRENCE, NOT THE FIRST. `exec` returned one match per
            // string, so *"Columns feed nothing, so open the Feed tab"* was
            // cleared by its verb and its header was never read at all.
            const occurrence = new RegExp(String.raw`(\w+)?[\s-]*\b(${word}s?)\b`, "gi");
            for (const hit of text.matchAll(occurrence)) {
              const subject = hit[1] ?? "";
              // THE RULE, IN ONE SENTENCE: a retired word is the VERB only when
              // `to` introduces it, or when a subject that can ACT stands before
              // it AND a word follows it — so `Data feed`, `Last pull`, `Create
              // flow` and `Every flow`, which END their run under a determiner
              // or a plain noun, are the NOUN and are refused.
              //
              // The version before this one asked only whether the subject was
              // an article, so every one of those eleven shapes passed: `Data`
              // is not an article and `feed` is a verb form, and that was the
              // whole test. The class this guard exists for was the class it
              // could not see.
              const after = text.slice(hit.index + hit[0].length);
              const takesAnObject = /[A-Za-z0-9]/.test(after);
              const canAct = Boolean(subject)
                && (!DETERMINER.test(subject) || ACTOR.test(subject));
              const isVerb = VERB_FORM.test(hit[2])
                && (/^to$/i.test(subject)
                  || (canAct && (takesAnObject || ACTOR.test(subject))));
              if (isVerb) continue;
              if (subject && QUALIFIER.includes(subject.toLowerCase())) continue;
              found.push(
                `${rel}: ${shape} "${text.slice(0, 90)}" says ${word}; the word is `
                  + `${GLOSSARY[concept].singular}`,
              );
            }
          }
        }
      }
    }
  }
  return [...new Set(found)].sort();
}

describe("a column header, a tab label and an empty-state title speak the glossary", () => {
  it("reads the whole console, not one directory", () => {
    expect(sources(CONSOLE).length).toBeGreaterThan(300);
  });

  it("names no ratified concept with a retired word", () => {
    expect(offenders()).toEqual([]);
  });

  it("keeps every exemption alive — a dead one is a rule nobody reads", () => {
    const rendered = sources(CONSOLE)
      .map((file) => withoutComments(readFileSync(file, "utf-8")))
      .join("\n");
    for (const text of Object.keys(NOT_THIS_CONCEPT)) {
      expect(rendered).toContain(text);
    }
  });
});
