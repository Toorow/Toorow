/**
 * The five nouns of the ingestion chain, and the words that are not them.
 *
 * `console-presentation.md` §4 ratifies one table — a configured source of rows
 * is a **Datastream**, what it reads from is a **Source**, the grant that lets a
 * Source be read is an **Authorization**, the code that knows a Source is a
 * **Connector**, one collection run is a **Run** — and `glossary.md` § *The rule*
 * says why a synonym is not a style choice: *"a second object as far as a model
 * is concerned"*.
 *
 * WHY A MODULE AND NOT A CONVENTION. The audit of 2026-09-05 measured 517 lines
 * of `ui/admin/src/**.tsx` carrying one of the eleven retired spellings. Most are
 * wire tokens, imports or the ordinary English verb — `"the Datastreams that feed
 * this Concept"` is correct English and the glossary says so explicitly. What is
 * wrong is the retired word standing where the OBJECT is named: a column header,
 * a tab label, an empty-state title. Those three are what §4 lists, they are the
 * three this module serves, and `__tests__/glossary.test.ts` is
 * what keeps them honest.
 *
 * ENGLISH ONLY. The epic's draft asked for an EN/FR declension; §1 of the same
 * document already ratified that the console is English, and a second language
 * in the one store of words would be the second store this story exists to
 * delete. A card's NARRATIVE follows its reader (amendment of 2026-09-05,
 * evening); a card's chrome does not, and neither does this.
 *
 * WHAT THIS IS NOT. It is not the object-type registry. A Datastream's DISPLAYED
 * noun as an object type is declared on its route contract
 * (`shell/navigation/data.ts`), read by `objectTypeLabel()`; that answers "what
 * do we call this address". This answers "what do we call this concept in a
 * sentence a person reads", which is the wider question and the one a column
 * header asks. The two agree by construction for the four concepts that are also
 * object types, and `glossary.test.ts` asserts that agreement rather than
 * hoping for it.
 */

export interface GlossaryEntry {
  /** The word, as it is written on a screen. */
  readonly singular: string;
  /** The same word for several. Declared, never derived: a rule that appends
   *  `s` would be right five times out of five here and wrong the first time a
   *  concept is added, and this file is the place where a spelling is DECIDED. */
  readonly plural: string;
  /** What this concept is, in the words of `glossary.md`. Read by a screen that
   *  needs to say what an empty list would hold. */
  readonly means: string;
  /** The spellings §4 retires for THIS concept, each mapped to the OTHER
   *  ratified object it still legitimately names — or `null` when it names none
   *  and may not appear at all.
   *
   *  Two facts, one table, because they were never two. `noun()` refuses every
   *  key: whatever else the word means, it does not mean this concept, and a
   *  caller asking for it has the wrong word. The SCREEN ratchet
   *  (`__tests__/glossary.test.ts`) judges only the `null` ones, because a word
   *  with a ratified second meaning is decidable by a reader and not by a grep —
   *  `glossary.md` reserves **Tool** for an MCP tool and
   *  `test_product_vocabulary.py` says in as many words that it is *"checked
   *  contextually rather than by spelling"*. Splitting these into two lists is
   *  how one of them would stop being maintained. */
  readonly never: Readonly<Record<string, string | null>>;
}

export const GLOSSARY = {
  datastream: {
    singular: "Datastream",
    plural: "Datastreams",
    means: "a configured source of rows",
    never: { flow: null, feed: null, stream: null, pipeline: null },
  },
  source: {
    singular: "Source",
    plural: "Sources",
    means: "what a Datastream reads from",
    never: {
      connection: null,
      // An Analyze artifact's ORIGIN is where a Render or a Report came from —
      // a Result, a Notebook — and that is not a data Source. Measured on five
      // column headers of `analyze-artifacts/`, every one of them correct.
      origin: "the provenance of an Analyze artifact",
    },
  },
  authorization: {
    singular: "Authorization",
    plural: "Authorizations",
    means: "the grant that lets a Source be read",
    never: {
      connexion: null,
      credential: null,
      // `auth` is the prefix of a route and of a wire field long before it is a
      // word on a screen, and four letters inside a sentence is not a noun.
      auth: "a route segment or a wire field",
    },
  },
  connector: {
    singular: "Connector",
    plural: "Connectors",
    means: "the code that knows a Source",
    never: {
      module: null,
      extension: null,
      // Reserved, not retired: `glossary.md` § *Words reserved for one meaning
      // only* gives **Tool** to an MCP tool. Measured 2026-09-05: eight of the
      // console's nine visible `Tool`s are MCP tools — the AI Path grid, the
      // Trace Observation lens, a Golden Question's forbidden list, a Skill's
      // bindings. A grep would have renamed all eight.
      tool: "an MCP tool",
    },
  },
  run: {
    singular: "Run",
    plural: "Runs",
    means: "one collection run",
    never: { pull: null, job: null, sync: null },
  },
} as const satisfies Record<string, GlossaryEntry>;

export type GlossaryConcept = keyof typeof GLOSSARY;

export const GLOSSARY_CONCEPTS = Object.keys(GLOSSARY) as GlossaryConcept[];

/** Every retired spelling, mapped to the concept that owns it.
 *
 *  Derived from the table above rather than written twice: a word added to a
 *  `never` list is refused from that moment, and there is no second place where
 *  someone could forget to add it. */
export const RETIRED_SPELLING: Readonly<Record<string, GlossaryConcept>> = Object.fromEntries(
  GLOSSARY_CONCEPTS.flatMap((concept) =>
    Object.keys(GLOSSARY[concept].never).map((word) => [word, concept] as const),
  ),
);

/** The retired spellings that name NOTHING else, so a screen may not carry one.
 *
 *  The complement — a word with a ratified second meaning — is judged by a
 *  reader, and the reason it is exempt is written beside it in `GLOSSARY`
 *  rather than inside the guard: one place to read, one place to change. */
export const REFUSED_ON_SCREEN: Readonly<Record<string, GlossaryConcept>> = Object.fromEntries(
  GLOSSARY_CONCEPTS.flatMap((concept) =>
    Object.entries(GLOSSARY[concept].never)
      .filter(([, otherwise]) => otherwise === null)
      .map(([word]) => [word, concept] as const),
  ),
);

/**
 * The word for a concept, singular or plural.
 *
 * `noun("datastream")` → `Datastream`; `noun("datastream", 2)` → `Datastreams`;
 * `noun("datastream", 0)` → `Datastreams`, because "0 Datastreams" is how a
 * count reads and a screen asking for a count already has the number.
 *
 * A RETIRED SPELLING THROWS, and that is the point of routing through a
 * function. `noun("flow")` does not quietly return `"flow"`: it raises, naming
 * the word that replaces it. The refusal is what makes the module a decision
 * rather than a suggestion — a screen cannot half-adopt it.
 */
export function noun(concept: GlossaryConcept, count = 1): string {
  const retiredFor = RETIRED_SPELLING[String(concept).toLowerCase()];
  if (retiredFor) {
    throw new Error(
      `"${concept}" is a retired spelling of ${GLOSSARY[retiredFor].singular} `
        + `(console-presentation.md §4). Say "${GLOSSARY[retiredFor].singular}".`,
    );
  }
  const entry = GLOSSARY[concept];
  if (!entry) {
    throw new Error(
      `"${concept}" is not one of the ratified concepts `
        + `(${GLOSSARY_CONCEPTS.join(", ")}). Add it to glossary.md first.`,
    );
  }
  return count === 1 ? entry.singular : entry.plural;
}

/**
 * A stored token, spelled the way a person reads it.
 *
 * WHAT THIS IS AND — MORE IMPORTANT — WHAT IT IS NOT. It is **de-tokenisation**,
 * never invention: `feeds_report` becomes `Feeds report`, the SAME word with the
 * base's punctuation taken off. It does not decide that `feeds_report` should be
 * called something else; deciding that is a product arbitration and belongs in
 * `glossary.md`, not in a function.
 *
 * WHY IT EXISTS. Measured 2026-09-05: 42 screen positions rendered a stored word
 * straight through — `{selectedNode.status}`, `{comparison.comparison_kind}`,
 * `{project.status}` — which `console-presentation.md` §4 refuses ("vocabulary of
 * the user, not the base"). Eleven of them were STATES and go through
 * `stateLabel`, which is the declared vocabulary with its tones. The rest are
 * kinds, types, roles and severities, and for those there are exactly three
 * honest answers, in this order:
 *
 *   1. a ratified product noun — `topic` reads **Knowledge**, `procedure` reads
 *      **Skill**; those come from the navigation registry through
 *      `objectTypeLabel()`, never from a map beside it;
 *   2. a DECLARED label map, next to the enum's TS union, quoting the server
 *      file that enumerates it — for the enums whose product word is not the
 *      token (`visualization_spec_version` reads *Visualization Spec version*);
 *   3. this, for a token whose word IS the token and only its spelling was of
 *      the base.
 *
 * WHAT IT REFUSES TO DO. It does not capitalise every word: `tool_catalog`
 * becomes `Tool catalog`, not `Tool Catalog`. Title Case is reserved for the
 * ratified nouns above, so a reader can tell a named object from a stored word
 * at a glance — the same split `navigation.ts` already draws between an object
 * noun and a tab label.
 */
export function wireWord(token: string | null | undefined): string {
  if (token === null || token === undefined) return "";
  const spaced = String(token).replaceAll(/[_-]+/g, " ").trim();
  if (!spaced) return "";
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}
