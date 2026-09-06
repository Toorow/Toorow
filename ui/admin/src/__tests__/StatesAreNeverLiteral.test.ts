/**
 * A STATE IS DRAWN BY THE SCALE, NEVER BY A CLASS TYPED AT THE CALL SITE.
 *
 * `docs/product-architecture/console-presentation.md` §3 and its `Incomplete if`:
 * « A state is coloured by a literal class, a `Badge` without `tone`, or an
 * accent-coloured ring/bar; `stateTone` accepts an `overrides` argument. »
 *
 * WHY A GREP AND NOT A REVIEW — the same reason `FormattingIsCentral.test.ts`
 * gives for numbers. Measured 2026-09-05 before 76-2: **22** literal state-tone
 * classes in 11 files, and **23** `<Badge>` elements carrying neither a `tone`
 * nor `outline`, so they rendered grey with no gravity — the epic's « badge gris
 * sans gravité », which drew `Archived` and `Live` in the same colour as a
 * country name. Every one of them was written by somebody who did not know
 * `tone.ts` answered the question.
 *
 * WHAT IS FORBIDDEN, AND WHAT IS DELIBERATELY NOT:
 *
 *   FORBIDDEN  a Tailwind class naming one of the four SEMANTIC tones —
 *              `success`, `warning`, `error`, `info` — in any of the properties
 *              a state is drawn with. Those four are the verdict scale, and
 *              `tone.ts` is where they live.
 *
 *   NOT        `primary` / the accent. §3 reserves the accent for *the
 *              recommended direction* and for *a control the person operates*,
 *              which is a link, a selected card, a next-step banner — not a
 *              state. Measurement 3 of the story counted 45 hits by including
 *              it, and 23 of those 45 were `text-primary` on an anchor or the
 *              `text-text-primary` token. A gate that refuses those would be
 *              refusing the rule's own exception.
 *
 *   FORBIDDEN  `<Badge>` with neither `tone=` nor `outline`. `badge.tsx` l.30
 *              already declares the pair: `outline` is « a label that must not
 *              read as a state », `tone` is the state. Silence between them is
 *              the defect — it renders `tone="neutral"`, which SAYS something
 *              ("nothing to see here") about a word nobody classified.
 *
 *   NOT        an `accent` `Progress` or ring. It was measured and there is
 *              nothing to hold: `components/ui/progress.tsx` defaults to
 *              `success` (l.54, reason at l.25-27), none of the five call sites
 *              passes a tone, and `ui/admin/src` contains no ring at all
 *              (`conic-gradient`/`strokeDasharray`: 0 hits). A grep asserting a
 *              rule with no possible offender is a rule nobody is holding.
 *
 * AND THE HALF A GREP ON CLASSES CANNOT SEE: A PRIVATE VOCABULARY WRITTEN AS
 * CODE. `WorkbenchDeliveryPanel.tsx:78` held a `function stateTone(state)` that
 * drew `EXPIRED` red against the union's `expired: "neutral"` -- a THIRD
 * collision, live as long as the two declared ones, and invisible to
 * `stateVocabulary.test.ts` because that file could only inspect exported MAPS.
 * A function returning a `Tone` is the same object as an override map with a
 * different syntax, so this gate reads return types: a function or arrow whose
 * return type is `Tone`, `Fill`, or a union of tone literals must either
 * DELEGATE to the shared vocabulary or be named below with the axis it maps.
 *
 * Measured 2026-09-05 across `ui/admin/src`: 20 such functions. Five live in the
 * vocabulary modules themselves, three delegate, one WAS the collision, and
 * eleven map an axis that is NOT the lifecycle state of an object -- a test
 * verdict, a gate decision, a feedback polarity, an ownership scope, an
 * execution phase. Those eleven are listed with their reason, because "it is not
 * a state" is a claim somebody has to make in writing.
 *
 * COMMENTS ARE STRIPPED FIRST, the way `FormattingIsCentral` and
 * `EmptyStatesDoNotInstructTheImpossible` learned to: this very file, `tone.ts`
 * and `badge.tsx` all EXPLAIN the rule by quoting the forbidden shapes, and a
 * matcher that reads prose as code reports the explanation as the defect.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { relative, resolve, sep } from "node:path";
import { describe, expect, it } from "vitest";

const CONSOLE_SRC = resolve(__dirname, "..");

/**
 * The four semantic tones, in every property a state is drawn with, plus the
 * three steps the token file declares for each (`-container`, `-surface`,
 * `-surface-hover`). `bg-success`, `text-warning`, `border-error/30`,
 * `hover:bg-info-surface-hover` and `dark:text-error` all match.
 */
const LITERAL_TONE_CLASS =
  /\b(?:text|bg|border|ring|stroke|fill|from|via|to|shadow|outline|decoration|divide|accent)-(?:success|warning|error|info)\b/;

/** The tag of a `<Badge …>` opening element, brace-aware so a `{cond ? a : b}`
 *  prop does not end it early. */
function badgeTags(source: string): string[] {
  const tags: string[] = [];
  for (const match of source.matchAll(/<Badge\b/g)) {
    let index = match.index + match[0].length;
    let depth = 0;
    while (index < source.length) {
      const char = source[index];
      if (char === "{") depth += 1;
      else if (char === "}") depth -= 1;
      else if (char === ">" && depth === 0) break;
      index += 1;
    }
    tags.push(source.slice(match.index, index));
  }
  return tags;
}

/** A Badge that classifies itself — as a state (`tone`) or as a label
 *  (`outline`). Spreading props counts: the caller is passing a decision on. */
const badgeIsClassified = (tag: string) =>
  /\btone=/.test(tag) || /\boutline\b/.test(tag) || /\{\.\.\./.test(tag);

/**
 * The files allowed to hold a literal tone class, each with the reason no other
 * file can. Keys are paths relative to `ui/admin/src`, with forward slashes;
 * a key ending in `/` is a directory prefix.
 */
const EXCEPTIONS: Record<string, string> = {
  "ui/tone.ts":
    "THE SCALE ITSELF. Every colour in the console is named here once, in maps Tailwind can see as whole class names — TONE_TEXT, TONE_FILL, TONE_CONTAINER, TONE_BORDER, TONE_SURFACE, TONE_DOT, FILL_BG, FILL_GRADIENT, HALO. A component reads a map; it never types a class.",
  "ui/Data.tsx":
    "`Status` — the object §3 names as the one way to render a state. It composes HALO, TONE_TEXT and the edge maps from tone.ts and adds the two SHAPE rules (the warning diamond, the neutral dotted ring), which are class literals about geometry rather than about colour; the `bg-primary-container` on an accent banner is the one tinted surface Jean ratified on 2026-07-29.",
  "ui/StatusLegend.tsx":
    "The key that explains those marks. It draws the same mark at legend size, so it repeats `Status`' two shape rules; its colour comes from HALO and `StatusLegend.test.tsx` asserts that no tone class and no hex appear in it.",
  "components/ui/":
    "The shadcn registry layer, which epic 76 leaves as it is (epic bound: « components/ui reste la couche shadcn »). FOUR hits, and they are control affordances rather than states of a domain object: the destructive Button variant (`button.tsx:87`), the destructive menu item and its checked-item tick (`dropdown-menu.tsx:98,124`), and the Select's checked tick (`select.tsx:136`). `badge.tsx`, `progress.tsx` and `switch.tsx` already read tone.ts.",
};

const isException = (path: string) =>
  path in EXCEPTIONS || Object.keys(EXCEPTIONS).some((key) => key.endsWith("/") && path.startsWith(key));

/**
 * A function or arrow that ANSWERS WITH A TONE. Matches a declared return type of
 * `Tone`, `Fill`, or a union written out as tone literals -- the three spellings
 * the console actually uses, plus the `{ label, tone }` object two screens
 * return. A PARAMETER typed `Tone` is not a match, which is the difference
 * between a component taking a tone and a function deciding one.
 */
const TONE_LITERAL = String.raw`(?:"(?:neutral|success|warning|error|info|accent)")`;
const TONE_UNION = `(?:${TONE_LITERAL}\\s*\\|\\s*)+${TONE_LITERAL}`;
const RETURNS_A_TONE = new RegExp(
  String.raw`(?:function\s+(\w+)\s*\([^)]*\)|const\s+(\w+)\s*=\s*\([^)]*\))` +
    // The `\b` belongs to the two IDENTIFIER spellings only: after the closing
    // quote of `"neutral"` a word boundary can never match, and the first
    // version of this regex silently saw none of the union-spelled functions.
    String.raw`\s*:\s*(?:\{[^}]*\btone\s*:\s*)?(?:Tone\b|Fill\b|` +
    TONE_UNION +
    ")",
  "g",
);

/**
 * A MAP FROM A WORD TO A TONE. `Record<string, Tone>`, `Record<string, "success"
 * | ...>`, and the untyped literal object whose values are nothing but tone
 * strings -- the three ways the console wrote a private vocabulary before 76-2's
 * review. Thirteen of them were live, and the review found six disagreeing with
 * the union about a word it already carried (`blocked`, `archived`, `revoked`,
 * `unavailable`, `refused`, `empty`).
 */
const TONE_RECORD_TYPE = new RegExp(
  String.raw`Record<\s*string\s*,\s*(?:Tone|Fill|` + TONE_UNION + String.raw`)\s*>`,
);

/**
 * The same object with no type annotation: `const X = { active: "success",
 * stale: "warning" } as const`. Matched on the SHAPE -- two or more `word: tone`
 * pairs in one literal -- because a map that types itself is the easy half.
 */
const TONE_OBJECT_LITERAL = new RegExp(
  // TWO PAIRS OR MORE, and the last one carries no comma -- which the first
  // version required, so `{ active: "success", stale: "warning" }` slipped
  // through and only maps of three or more were caught.
  String.raw`\{\s*(?:[\w"'-]+\s*:\s*` + TONE_LITERAL + String.raw`\s*,\s*)+[\w"'-]+\s*:\s*` + TONE_LITERAL,
);

/** A tone literal returned with `as const`, which is how a function answers with
 *  a tone without ever naming `Tone`. */
const AS_CONST_TONE = new RegExp(String.raw`return\s+` + TONE_LITERAL + String.raw`\s+as\s+const`);

/**
 * A body that ASKS THE UNION rather than deciding. A function whose whole job is
 * to forward to the shared vocabulary is not a second vocabulary, and three of
 * them exist: `Shared#outcomeTone`, `DataCollectionLayout#toneForState` and
 * `EvidenceCollection#outcomeTone`. They are recognised by WHAT THEY CALL, not
 * by their name, so renaming one does not smuggle a decision back in.
 */
const DELEGATES =
  /\b(?:stateTone|extractStatusTone|extractGapTone|bindingStateTone|outcomeTone|verdictTone)\s*\(/;

/**
 * The files whose whole subject IS the tone scale. A tone is decided in them,
 * which is the point of having them.
 */
const VOCABULARY_MODULES = [
  "ui/stateVocabulary.ts",
  "ui/tone.ts",
  "ui/CoverageBars.tsx",
  "ui/EntityMatrix.tsx",
  "ui/CapabilityCoverage.tsx",
];

/**
 * A function answering with a tone about SOMETHING THAT IS NOT THE LIFECYCLE
 * STATE OF AN OBJECT. Each key is `path#function`; each value names the axis it
 * maps and why the union cannot hold it.
 */
const NON_STATE_AXES: Record<string, string> = {
  "test/testEvidence.ts#verdictTone":
    "A test VERDICT (pass / fail / unverifiable), not a state of an object. `analyze-and-test.md:282-284` ratifies that `unverifiable` is neutral -- nothing was judged -- and the union has no word for a judgement that did not happen; adding one would let an object's lifecycle be spelled `unverifiable`.",
  "shell/pages/GoldenQuestionWorkbench.tsx#dimensionTone":
    "The same verdict scale, read per coverage dimension. DEBT, and named as such: it is a three-line second copy of `verdictTone` and the two should be one call. 76-5 owns the Test workspace pass.",
  "shell/pages/EvaluationRunWorkbench.tsx#gateTone":
    "A gate DECISION (pass / block) written as evidence for an owning workflow. It is a decision somebody recorded, not a condition an object is in -- the screen's own banner says Test writes evidence and transitions nothing.",
  "shell/pages/WidgetFeedback.tsx#polarityTone":
    "Feedback POLARITY (positive / negative / neither). A reader's opinion is not a state of the widget, and colouring it through the lifecycle scale would say a negative reading is a fault in the object.",
  "shell/pages/DataWorkspace.tsx#mappingReach":
    "HOW FAR A MAPPING REACHES, derived from evidence COUNTS (`mapping_blocking_count`, `active_mapping_version`) rather than from a state word the server sent. There is no token to look up: the function reads numbers and answers a sentence, which is the opposite of a vocabulary, and the union has nothing to say about a count.",
  "shell/pages/Sources.tsx#ownership":
    "An ownership SCOPE (this organization / delegated) and the sentence that explains it. A Source shared from elsewhere is not less healthy, it is somewhere else: the green means `you can reconnect it yourself`.",
  "data/ManualEventAnnotations.tsx#annotationState":
    "An annotation's EDITORIAL HISTORY (Live / Corrected / Withdrawn). `Corrected` is information about the record's own revisions and has no counterpart in a vocabulary of server-sent object states.",
  "datastreams/workbench/DatastreamRunLive.tsx#toneForPhase":
    "An execution PHASE, keyed by the registry's own `ExecutionPhase` type so the compiler refuses a phase that does not exist. A phase is where a run currently is, not a verdict on it, and the file quotes no run-state name at all.",
  "governance/EvidenceCollection.tsx#availabilityTone":
    "Evidence AVAILABILITY (available / owner_unavailable / retained_away / quarantined) -- where a proof physically is under the retention rules. Its neighbour `outcomeTone` in the same file DOES delegate, which is what makes the split deliberate rather than lazy.",
  "authorizations/AuthorizationsPanel.tsx#healthLabel":
    "A HEALTH PROBE result, read beside `row.status` in the same function: the lifecycle says the grant exists, the probe says whether the provider answered. Two axes on one row and only the first is a state -- AI-341 added the case where the authorization is alive and the provider still refuses.",
};

/**
 * The FILES allowed to hold a word-to-tone map, because what they map is not the
 * lifecycle state of an object. Same rule as `NON_STATE_AXES` and a separate
 * table only because the unit is a file: a map has no name a grep can trust.
 */
const MAP_AXES: Record<string, string> = {
  "datastreams/preconfiguration/SourceConnectorPull.tsx":
    "`SAFETY_TONE` maps a RECOMMENDATION (recommended / needs_choice / no_safe_recommendation) -- what the catalogue advises this operator to start from, not a condition the Connector is in. A Connector nobody recommends is not unhealthy.",
  "datastreams/preconfiguration/sourceStepShared.ts":
    "`CONFIDENCE_TONE` maps a CONFIDENCE LEVEL (high / medium / low / none) in a detection the wizard made. It grades an inference, not an object: the same Datastream can be `high` on one field and `low` on the next in the same second.",
  "governance/SemanticModelTabs.tsx":
    "`PARITY_TONE` maps a COMPARISON between two catalogues (aligned / project_override / unreadable / platform_divergence) -- a relation between a Project formula and the delivered one. `project_override` is a decision somebody made on purpose, which no lifecycle word can mean. The screen's OTHER map, the coverage one, was migrated.",
  "governance/UnresolvedValuesPanel.tsx":
    "`REASON_TONE` maps WHY a value could not be resolved (absent_at_source / unmapped / no_reference) -- a cause, paired one-to-one with `REASON_LABEL` right above it. The value has no state; the pipeline has a reason, and the row exists precisely because the object it names does not.",
  "datastreams/workbench/DatastreamRunLive.tsx":
    "`TONE_BY_PHASE` is the map behind `toneForPhase`, already named in NON_STATE_AXES: an execution PHASE keyed by the registry's own `ExecutionPhase` type, so the compiler refuses a phase that does not exist. A phase is where a run currently is, not a verdict on it.",
  "governance/GovernanceObjectWorkbench.tsx":
    "A WORKSPACE colour map (analyze / data / governance / overview) used to tint the cross-workspace links -- it says which part of the console an object belongs to, which is navigation, not condition. The file's two state maps, `versionTone` and its raw `{ref.state}` cells, were migrated.",
  "datastreams/preconfiguration/DatastreamSetupWizard.tsx":
    "`REQUIREMENT_TONE` grades HOW BADLY a wizard step is wanted (required / recommended / optional / automatic) and is the one map in the console that legitimately spends the `accent`: `recommended` IS the recommended direction, which is what §3 reserves the rose for. Its neighbour `PROPOSAL_TONE`, a real state map, was migrated.",
  "datastreams/workbench/pages/WorkbenchOverviewPage.tsx":
    "A map from an ACTION KIND to the urgency of offering it (repair / review_candidate / finish_setup / prepare_change) -- what the reader should do next, not what the Datastream is. The same Datastream carries several of these at once.",
};

/**
 * A STATE VOCABULARY THAT IS STILL PRIVATE, each with the collision it carries
 * and the story that owns the arbitration.
 *
 * This table is separate from `NON_STATE_AXES` on purpose, and calling both
 * "exceptions" would have been the lie: those map something that is not a state
 * and never will be, THESE map a state of an object and should read the union.
 * Every one of them draws a word the union already carries in a different
 * colour, so migrating is not a rename — it is an arbitration, and 76-2's list
 * of arbitrages is closed. Naming the collision is what keeps it visible until
 * somebody settles it.
 *
 * The rule that made this table necessary: a function returning a tone is an
 * override map with a different syntax. `WorkbenchDeliveryPanel#stateTone` was
 * the fourth one and it was migrated (`credential_expired` is declared in the
 * union); the three that followed it were migrated in the next review round,
 * and the table below is empty by contract.
 */
const UNMIGRATED_STATE_MAPS: Record<string, string> = {
  // EMPTY, AND IT STAYS EMPTY. The three entries this table carried for one
  // review round -- `mappingModel#bindingTone`, `connectorInstallationApi
  // #installationStateReading`, `DatastreamWorkbenchRoute#tone` -- were all
  // migrated: every word they carried is declared in `ui/stateVocabulary.ts`,
  // at the tone the arbitrages fix. An entry here is a private vocabulary
  // somebody described instead of reading the union, and describing it is not
  // closing it.
};

/** Every `.ts`/`.tsx` file under `ui/admin/src`, tests included. */
function sourceFiles(directory: string, found: string[] = []): string[] {
  for (const entry of readdirSync(directory)) {
    if (entry === "node_modules" || entry === "dist") continue;
    const full = resolve(directory, entry);
    if (statSync(full).isDirectory()) sourceFiles(full, found);
    else if (/\.tsx?$/.test(entry)) found.push(full);
  }
  return found;
}

/**
 * A test file may hold one: it is allowed to ASSERT on a class name — this file
 * and `StatusLegend.test.tsx` both do — and what a test renders is not a screen.
 */
const isTest = (path: string) => /\.test\.tsx?$/.test(path);

/** Source with comments stripped — see the header. */
function code(path: string): string {
  return readFileSync(path, "utf-8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/\/\/.*/g, "");
}

const screens = sourceFiles(CONSOLE_SRC)
  .filter((path) => !isTest(path))
  .map((path) => relative(CONSOLE_SRC, path).split(sep).join("/"))
  .filter((path) => !isException(path));

const literalOffenders = screens.filter((path) =>
  LITERAL_TONE_CLASS.test(code(resolve(CONSOLE_SRC, path))),
);

const unclassifiedBadges = screens.flatMap((path) =>
  badgeTags(code(resolve(CONSOLE_SRC, path)))
    .filter((tag) => !badgeIsClassified(tag))
    .map(() => path),
);

/**
 * The body of the function starting at `from` -- brace-matched, so a nested
 * object or arrow does not end it early.
 */
function bodyAt(source: string, from: number): string {
  // AN EXPRESSION-BODIED ARROW HAS NO BRACE, and `DataCollectionLayout`'s
  // `toneForState` is one: `= (state): Tone => stateTone(state);`. Reading to
  // the next `{` would have picked up a stranger's body and called a delegation
  // a private vocabulary. Everything up to the statement's `;` is the body.
  const arrow = source.indexOf("=>", from);
  const brace = source.indexOf("{", from);
  if (arrow >= 0 && (brace < 0 || arrow < brace)) {
    const rest = source.slice(arrow, brace < 0 ? undefined : brace + 1);
    if (!/^\s*=>\s*\{/.test(rest)) {
      const end = rest.indexOf(";");
      return end < 0 ? rest : rest.slice(0, end + 1);
    }
  }
  const open = brace;
  if (open < 0) return "";
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    else if (source[index] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(open, index + 1);
    }
  }
  return source.slice(open);
}

/**
 * Every private state MAP left: a word-to-tone table outside the vocabulary
 * modules that is not named as a non-state axis. Measured on the file, because
 * a map has no name a regex can rely on -- `OUTCOME_TONE` lived under that name
 * in two files and under `BINDING_TONE` in two others.
 */
/** How many word-to-tone maps a file holds, by the two shapes above. */
function mapCount(path: string): number {
  const source = code(resolve(CONSOLE_SRC, path));
    // A typed map matches BOTH shapes (its annotation and its body), an untyped
    // one matches the second only: the larger of the two counts is the number
    // of maps, never their sum.
  return Math.max(
    source.match(new RegExp(TONE_RECORD_TYPE.source, "g"))?.length ?? 0,
    source.match(new RegExp(TONE_OBJECT_LITERAL.source, "g"))?.length ?? 0,
  );
}

// AN AXIS EXEMPTS ONE MAP, NOT A FILE (review of 76-2): the entry names one
// table, so a second word-to-tone map added to the same file is a private
// vocabulary like any other and is refused.
const privateMaps = screens
  .filter((path) => !VOCABULARY_MODULES.includes(path))
  .filter((path) => mapCount(path) > (path in MAP_AXES ? 1 : 0));

/** An axis entry whose file holds no detectable map any more is a dead door. */
const staleAxes = Object.keys(MAP_AXES).filter((path) => mapCount(path) === 0);

/**
 * Every `return "warning" as const` outside the vocabulary modules. No file is
 * exempt: an axis entry names a MAP, and a function spelling a tone `as const`
 * is a vocabulary written as code, whatever file it lives in.
 */
const asConstTones = screens
  .filter((path) => !VOCABULARY_MODULES.includes(path))
  .filter((path) => AS_CONST_TONE.test(code(resolve(CONSOLE_SRC, path))));

/** Every function answering with a tone that neither delegates nor is named. */
const privateVocabularies = screens
  .filter((path) => !VOCABULARY_MODULES.includes(path))
  .flatMap((path) => {
    const source = code(resolve(CONSOLE_SRC, path));
    const found: string[] = [];
    for (const match of source.matchAll(RETURNS_A_TONE)) {
      const name = match[1] ?? match[2] ?? "?";
      const key = `${path}#${name}`;
      if (key in NON_STATE_AXES || key in UNMIGRATED_STATE_MAPS) continue;
      if (DELEGATES.test(bodyAt(source, match.index + match[0].length))) continue;
      found.push(`${path}#${name}`);
    }
    return found;
  });

describe("states are never literal", () => {
  it("finds no file colouring a state by hand", () => {
    // The failure prints the paths, because "one file broke the rule" without
    // saying which is a message that costs the reader the grep this test ran.
    expect(literalOffenders).toEqual([]);
  });

  it("finds no Badge that classified itself as neither a state nor a label", () => {
    expect(unclassifiedBadges).toEqual([]);
  });

  it("finds no private word-to-tone map outside the vocabulary", () => {
    // Thirteen were live before this review, six of them disagreeing with the
    // union about a word it already carried.
    expect(privateMaps).toEqual([]);
  });

  it("finds no tone returned with `as const`", () => {
    // The spelling that answers with a tone without ever naming `Tone`:
    // `ProjectOverview#tone` drew `blocked` red this way, and
    // `GovernanceObjectWorkbench#versionTone` defaulted to neutral.
    expect(asConstTones).toEqual([]);
  });

  it("keeps every file-level map exemption named with the axis it maps", () => {
    expect(staleAxes).toEqual([]);
    expect(Object.keys(MAP_AXES)).toHaveLength(8);
    for (const reason of Object.values(MAP_AXES)) {
      expect(reason.length).toBeGreaterThan(80);
    }
  });

  it("finds no function deciding a tone outside the vocabulary", () => {
    // The half a class grep cannot see. `WorkbenchDeliveryPanel#stateTone` was
    // here, and it is the reason this assertion exists.
    expect(privateVocabularies).toEqual([]);
  });

  it("keeps every non-state axis named with the axis it maps", () => {
    // Ten, measured. An eleventh is a decision -- is this really not a state? --
    // and a decision belongs in `console-presentation.md` before it belongs here.
    expect(Object.keys(NON_STATE_AXES)).toHaveLength(10);
    for (const reason of Object.values(NON_STATE_AXES)) {
      expect(reason.length).toBeGreaterThan(80);
    }
  });

  it("keeps the deferral table empty", () => {
    // It held three for one review round and holds none now. A new entry is a
    // private vocabulary somebody described instead of closing.
    expect(Object.keys(UNMIGRATED_STATE_MAPS)).toEqual([]);
  });

  it("bites on the three shapes a private vocabulary is written in", () => {
    // A typed map.
    expect(TONE_RECORD_TYPE.test('const T: Record<string, Tone> = { active: "success" };')).toBe(true);
    expect(
      TONE_RECORD_TYPE.test('const T: Record<string, "success" | "warning"> = {};'),
    ).toBe(true);
    // An untyped literal: two pairs or more.
    expect(TONE_OBJECT_LITERAL.test('const T = { active: "success", stale: "warning" };')).toBe(true);
    // One pair is a prop, not a vocabulary -- `{ tone: "warning" }` is what a
    // caller passes, and refusing it would refuse every call site.
    expect(TONE_OBJECT_LITERAL.test('return { label: "Live", tone: "neutral" };')).toBe(false);
    // A tone returned with `as const`.
    expect(AS_CONST_TONE.test('  if (s === "blocked") return "error" as const;')).toBe(true);
    expect(AS_CONST_TONE.test('  return tone as const;')).toBe(false);
    // `Record<string, string>` is a label table and none of this gate's business.
    expect(TONE_RECORD_TYPE.test("const L: Record<string, string> = {};")).toBe(false);
  });

  it("bites on a function that answers with a tone and decides for itself", () => {
    const offender = [
      "function stateTone(state: string): Tone {",
      '  if (state === "ACTIVE") return "success";',
      '  return "neutral";',
      "}",
    ].join("\n");
    const found = [...offender.matchAll(RETURNS_A_TONE)];
    expect(found).toHaveLength(1);
    expect(DELEGATES.test(bodyAt(offender, found[0].index + found[0][0].length))).toBe(false);
    // A delegation is not an offender: it asks the union.
    const forwarder = "function outcomeTone(v: string): Tone {\n  return stateTone(v);\n}";
    const forwarded = [...forwarder.matchAll(RETURNS_A_TONE)];
    expect(forwarded).toHaveLength(1);
    expect(DELEGATES.test(bodyAt(forwarder, forwarded[0].index + forwarded[0][0].length))).toBe(true);
    // The union-of-literals spelling is caught too, not only `Tone`.
    const spelled = 'function gateTone(d: string): "success" | "error" | "neutral" {';
    expect([...spelled.matchAll(RETURNS_A_TONE)]).toHaveLength(1);
    // A PARAMETER typed `Tone` is not a function answering with one.
    const param = "function LegendMark({ tone }: { tone: Tone }) {";
    expect([...param.matchAll(RETURNS_A_TONE)]).toHaveLength(0);
  });

  it("keeps every exception justified, and lets none in silently", () => {
    // Four, and each one carries a sentence. A fifth is a decision, not a fix:
    // it belongs in `console-presentation.md` before it belongs here.
    expect(Object.keys(EXCEPTIONS)).toHaveLength(4);
    for (const reason of Object.values(EXCEPTIONS)) {
      expect(reason.length).toBeGreaterThan(40);
    }
  });

  it("bites on the shapes that were live in ui/admin/src on 2026-09-05", () => {
    // Asserted on strings, so the gate's own reach is testable without leaving a
    // file behind that would then have to be remembered and deleted. Each line
    // is a real site the story migrated.
    expect(LITERAL_TONE_CLASS.test('<span className="size-2 rounded-full bg-warning" />')).toBe(true);
    expect(LITERAL_TONE_CLASS.test('className="space-y-2 px-2 py-3 text-xs text-error"')).toBe(true);
    expect(LITERAL_TONE_CLASS.test('ok: "bg-success-surface hover:bg-success-surface-hover",')).toBe(true);
    expect(LITERAL_TONE_CLASS.test('done: "border-success-container bg-success",')).toBe(true);
    expect(LITERAL_TONE_CLASS.test('className="border-error/30 text-error"')).toBe(true);
    // The accent is a control, not a state: §3 reserves it and the gate lets it
    // through. `text-text-primary` is a TOKEN and not a tone at all.
    expect(LITERAL_TONE_CLASS.test('<a className="text-primary underline" href={href}>')).toBe(false);
    expect(LITERAL_TONE_CLASS.test('<p className="mt-1 font-medium text-text-primary">')).toBe(false);
    // And a word that merely CONTAINS a tone name is not a class.
    expect(LITERAL_TONE_CLASS.test('data-testid="tree-error"')).toBe(false);
    expect(LITERAL_TONE_CLASS.test("const infoText = readInfo();")).toBe(false);
  });

  it("bites on a Badge that names neither tone nor outline", () => {
    expect(badgeIsClassified("<Badge>")).toBe(false);
    expect(badgeIsClassified('<Badge className="ml-2">')).toBe(false);
    expect(badgeIsClassified('<Badge tone={stateTone(item.status)}>')).toBe(true);
    expect(badgeIsClassified("<Badge outline>")).toBe(true);
    // Brace-aware: a ternary prop must not end the tag at its first `>`.
    expect(
      badgeIsClassified('<Badge tone={count > 0 ? "warning" : "neutral"}>'),
    ).toBe(true);
  });
});
