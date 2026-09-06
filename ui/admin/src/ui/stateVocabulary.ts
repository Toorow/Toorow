/**
 * The state vocabulary — one word, one tone, one sentence, for the whole console.
 *
 * WHY IT IS NOT IN `tone.ts`. That file answers "what colour is a warning"; this
 * one answers "is `stale` a warning". The two change for different reasons and
 * are ratified by different people: the tone scale is a design decision measured
 * against the mockups, and the vocabulary below is a reading of what the server
 * puts on the wire. Merging them would put a domain word in the file whose whole
 * point is that it holds none.
 *
 * WHAT IT REPLACES. Six maps said the same words in six places —
 * `data/DataCollectionLayout` (a tone map and a label map), `governance/
 * GovernanceCollection`, `analyze-artifacts/Shared`, `analyze-artifacts/
 * RenderSharing` and `cache/CacheHealthCard`. `active` was green in four of them
 * by coincidence, not by agreement, and nothing would have caught the fifth
 * spelling it amber.
 *
 * THE TWO COLLISIONS ARE CLOSED, AND `overrides` IS GONE (story 76-2,
 * `console-presentation.md` §3). Until then two screens carried a declared
 * dissent, and a `stateTone(value, overrides)` parameter existed to hold them:
 *
 *   `blocked`      Governance drew it ERROR where the five Data collections drew
 *                  it WARNING. **Warning wins everywhere.** A governed object
 *                  whose lifecycle is blocked is still repairable, which is what
 *                  `warning` means in this scale; the word that means "cannot be
 *                  used at all" is `archived`, and it is already `error`.
 *
 *   `unavailable`  Analyze drew it INFO where the Data collections drew it
 *                  NEUTRAL. **Neutral wins everywhere.** The override's own
 *                  justification did not describe the field it coloured: the
 *                  `unavailable` reaching `outcomeTone` is written by
 *                  `server/core/query_execution.py` for an unreadable identifier,
 *                  an unreadable relation or a warehouse the runner could not
 *                  reach — a silence, exactly what `StateValue` means by it. The
 *                  fact Analyze wanted to colour is a DIFFERENT field,
 *                  `ContractState.available`, and it now has a word of its own:
 *                  `not_offered`, below.
 *
 * `stateTone` and `stateLabel` take ONE argument each. A screen that disagrees
 * with a word here changes it here, in front of everybody, or declares a new
 * word — it does not pass a private map. `__tests__/stateVocabulary.test.ts`
 * pins the arity so the parameter cannot grow back.
 *
 * A WORD THIS FILE DOES NOT KNOW IS A WARNING, NOT A GREY BADGE. `README.md`
 * invariant 8: *unknown, unavailable or unverifiable is never healthy*. The
 * console used to answer `neutral` and capitalize whatever it was handed, which
 * printed a server token as if it were a product word and drew it in the colour
 * of "nothing to see". A state nobody declared is a question nobody answered:
 * `Unknown`, warning diamond. (`unavailable` — an absence the server CHOSE —
 * stays neutral. The two words are not synonyms, and that is the whole reason
 * both exist.)
 */
import type { Tone } from "./tone";

/** `Blocked`, `blocked `, `BLOCKED` are one word. */
function normalize(value: string | null | undefined): string {
  // WHITESPACE IS A SEPARATOR LIKE `_`. The Datastream header's axes arrive as
  // `change pending` and `rollback available` -- the server writes the display
  // spelling -- and `change_pending` is the same word. Folding them here is what
  // let that screen's private map be deleted instead of exempted.
  return (value ?? "").trim().toLowerCase().replaceAll(/\s+/g, "_");
}

/**
 * The union of the six maps. Everything absent from it is `warning` — see the
 * header: a word nobody declared is a question nobody answered.
 *
 * `success` and `error` are earned, not assumed. `archived` is an error because
 * Governance has always drawn it as one — an archived object cannot be bound —
 * and no other surface renders the word.
 */
export const STATE_TONE: Readonly<Record<string, Tone>> = {
  // Something is in place and working.
  active: "success",
  available: "success",
  configured: "success",
  confirmed: "success",
  executable: "success",
  fresh: "success",
  linked: "success",
  no_rejections: "success",
  observed: "success",
  pinned: "success",
  published: "success",
  ready: "success",
  recorded: "success",
  succeeded: "success",
  success: "success",
  used: "success",
  versioned: "success",
  // A Placement line whose entity was found. `accepted` is the same answer taken
  // by a person rather than by the matcher (story 61.3). Declared here because
  // `WorkbenchPlacementsPage` carried a private `stateTone` for the four words of
  // its tab — a seventh map, invisible to this file's test because it was a
  // function and not an override.
  accepted: "success",
  matched: "success",
  // A binding the compiler resolved, and a version that is the one in force.
  // Declared out of `mapping/mappingModel`, `governance/SemanticModelTabs`,
  // `governance/CountryWorkspace` and `governance/GovernanceObjectWorkbench`,
  // which held four private maps saying them four times.
  bound: "success",
  complete: "success",
  current: "success",
  // The Datastream header's own word for an axis that is in order. Declared out
  // of `DatastreamWorkbenchRoute`'s private map, at the tone it already gave it.
  healthy: "success",
  selected: "success",
  // Something is incomplete, held back, or no longer trustworthy. It is not a
  // failure: every word here names work that is still possible.
  // ONE reading, everywhere, since 76-2: a blocked authorization and a blocked
  // lifecycle are both something a person can still repair.
  blocked: "warning",
  // A delivery credential in its overlap window: the successor is live, this one
  // still resolves, and the window closes on a clock. Work in flight, which is
  // what warning means here — declared from `WorkbenchDeliveryPanel`'s private
  // map, at the tone that map already gave it, so nothing on that panel moves.
  rotating: "warning",
  // Proposed, not yet in force -- a version, a binding, a mapping awaiting a
  // person. `prepared` and `proposed` were already here; these are the same fact
  // under the spellings four other screens use.
  candidate: "warning",
  suggested: "warning",
  // Something is missing rather than wrong: an axis with holes, an object nobody
  // has configured, an answer the platform was never given.
  incomplete: "warning",
  // A step of the wizard that is owed something, and a value a person has to
  // look at. Declared out of `DatastreamSetupWizard`'s completeness map.
  missing: "warning",
  needs_review: "warning",
  // THE SIXTH WORD OF `ProposalItemStatus`, and the only one the union did not
  // carry (story 76-6). `server/core/datastream_preconfiguration.py:46` declares
  // `complete | missing | blocked | warning | not_applicable | needs_review`;
  // five were already here, so `stateLabel("warning")` fell through to the
  // `Unknown` fallback and the wizard printed « Classification · Unknown » over
  // a section whose items each carried a named, acknowledgeable warning. An
  // undeclared word does not lose its colour quietly — it loses its MEANING, and
  // says a question went unanswered where the compiler in fact answered.
  warning: "warning",
  // A preview whose Result no longer fits the Builder's plan, and one whose rows
  // were cut. Declared out of `analyze/builder/BuilderPreview`.
  truncated: "warning",
  unset: "warning",
  absent: "warning",
  not_installed: "warning",
  // A change waiting for a person, and a rollback offered. Both name work that
  // exists and nobody has done; the Datastream header drew them amber already.
  change_pending: "warning",
  rollback_available: "warning",
  // An invitation or a membership on its way: issued, delivered, not accepted.
  invited: "warning",
  pending: "warning",
  delivered: "warning",
  // A connector installation still owed something -- an address, a first check.
  domain_pending: "warning",
  verifying: "warning",
  // THE OTHER TWO STEPS OF THE ISSUE SEVERITY SCALE, declared beside `blocking`
  // out of `DatastreamIssueBadge` and `RunAnomalies`, which held the same three
  // words twice. An issue that degrades is amber; one that is only worth knowing
  // carries no verdict at all.
  degrading: "warning",
  // A Placement line the matcher could not settle. `ambiguous` is a question
  // somebody has to answer now: nothing is wrong, but nothing moves either.
  ambiguous: "warning",
  degraded: "warning",
  draft: "warning",
  empty: "warning",
  partial: "warning",
  paused: "warning",
  // A Render Share that a second role holder has not authorized yet
  // (`proactive-assertions.md` decision 2). Warning and not neutral: it is work
  // somebody still has to do, and nothing has left the platform until they do.
  pending_confirmation: "warning",
  prepared: "warning",
  proposed: "warning",
  rejected_rows: "warning",
  stale: "warning",
  testing: "warning",
  unconfigured: "warning",
  // Something ended badly, or was refused.
  archived: "error",
  // THE THIRD COLLISION, CLOSED BY DECLARING A WORD RATHER THAN BENDING ONE
  // (76-2, orchestrator's arbitrage). `WorkbenchDeliveryPanel` held a private
  // map drawing `EXPIRED` as an error, against `expired: "neutral"` below. The
  // server settles which fact it is: `server/core/inbound_credentials.py:96-97`
  //
  //     #: Terminal states: a credential in one of these is permanently invalid.
  //     _TERMINAL_STATES: frozenset[str] = frozenset({"REVOKED", "EXPIRED"})
  //
  // and `_effective_state` (l.209-220) projects the word at the exact boundary.
  // A delivery credential that expired is in the SAME frozenset as `revoked`,
  // which this map already draws `error`: senders fail closed, deliveries stop,
  // and somebody has to issue a new one. That is not the `expired` below, which
  // is a life that ended on schedule and costs the reader nothing. Two facts,
  // two words — never one word with a screen-local exception.
  credential_expired: "error",
  denied: "error",
  error: "error",
  // The fifth word of the work-queue vocabulary
  // (`server/core/dq_api.py:35`: `queued | running | done | failed |
  // dead_letter`). Added 2026-09-05 by story 76-3: routing
  // `{progress.materialization.state}` through `stateLabel` made it read
  // `Unknown`, which is invariant 8 doing its job — a word nobody declared —
  // and the answer to invariant 8 is to DECLARE the word, never to bypass it.
  // A dead letter is terminal and nothing retries it: error, like `failed`.
  // THE SIX WORDS OF A FEEDBACK REVIEW, `server/core/feedback_review.py:104`
  // (`REVIEW_STATES`). Four of them were undeclared and reached three screens
  // as `Unknown` the moment 76-3 routed `{annotation.review.current_state}`
  // through `stateLabel` — invariant 8 naming a gap rather than hiding it.
  // `accepted` and `rejected` were already declared, by the two objects that
  // share the words; nothing about them changes.
  unreviewed: "neutral",
  triaged: "info",
  duplicate: "neutral",
  resolved: "success",
  dead_letter: "error",
  failed: "error",
  refused: "error",
  // The pinned Result cannot be drawn by this plan at all.
  incompatible: "error",
  rejected: "error",
  revoked: "error",
  // An invitation the transport could not deliver. Nothing arrived, and only a
  // new invitation repairs it.
  delivery_failed: "error",
  // `blocking` IS NOT `blocked`, and keeping them apart is what let four screens
  // agree. `blocked` is what an object IS -- held up, repairable, amber.
  // `blocking` is what a thing DOES to everything downstream: a binding that
  // stops a Datastream, an issue whose severity is `blocking`. Three files drew
  // it red already (`mapping/mappingModel`, `DatastreamIssueBadge`,
  // `RunAnomalies`) and they were right; what they lacked was one place to say so.
  blocking: "error",
  // An outcome the ledger recorded without a verdict: worth a look, not a fault.
  outcome_unknown: "warning",
  // Neither good nor bad — an absence, a switch that is off, a life that ended
  // on schedule. Written down rather than left to the default, so a reader of
  // this file can see that the silence was chosen.
  disabled: "neutral",
  // A life that ended on schedule and asks nothing of anybody — a lapsed Render
  // Share confirmation (`analyze-artifacts/client.ts:772`), a cached reading
  // past its window. A credential that expired is NOT this word: see
  // `credential_expired` above.
  expired: "neutral",
  "no-cache": "neutral",
  unavailable: "neutral",
  // A Placement line nobody has matched yet. Neutral and not warning: it is the
  // WORK of that tab, not a fault in it, and a tab whose every row shouts has
  // stopped telling the reader which row to open (story 61.3's own reading,
  // declared here instead of living in a private map).
  unmatched: "neutral",
  informational: "neutral",
  // Replaced on purpose, deliberately left out, or paused by a person: three
  // absences somebody chose, which is what `neutral` is for.
  superseded: "neutral",
  excluded: "neutral",
  suspended: "neutral",
  // No mapping at all, and no recommendation at all. Declared out of
  // `governance/DimensionLineageTab`.
  none: "neutral",
  // The question does not arise here. Not an absence and not a fault.
  not_applicable: "neutral",
  // The one word 76-2 ADDED, and the reason `unavailable` could stop being two
  // things at once. The server said this deployment does not carry the contract
  // at all — `ContractState.available === false`, or a presentation kind with no
  // registry behind it. That is a statement about the deployment, which is what
  // `info` is for, and it is NOT the silence `unavailable` names.
  not_offered: "info",
  // The task ran and found nothing worth saying. `ProjectSettings` had this
  // right in a private map and wrote down why: a quiet day is a healthy day, and
  // colouring it amber teaches a reader to ignore the colour.
  no_insight: "info",
  // Reading, right now. A statement about the deployment's progress, which is
  // what `info` is for, and never a verdict on the object.
  loading: "info",

  // ------------------------------------------------------------------------
  // DECLARED BY STORY 76-5, for the Overview and the seven Test screens.
  //
  // Every word below was reaching `stateLabel` UNDECLARED, so it read `Unknown`
  // in the warning colour -- which is the honest default and, for these nine,
  // the wrong answer: the server names each of them deliberately and three
  // distinct words were collapsing into one reading. Declaring them is the
  // path §3 offers ("a screen that disagrees changes the word here, in front of
  // everybody"); the alternative was five private maps, which is what 76-2 spent
  // itself removing.
  // ------------------------------------------------------------------------

  // `project_overview.py:798` -- `data_coverage.active.state` is
  // `summary["published_trust"]`, one of `trusted` / `attention` / `unknown` /
  // `no_data` (`derive_project_trust`). The three that are not already answered
  // here were all reading `Unknown`, so an Overview whose publications are
  // verified said the same word as one whose publications are late.
  trusted: "success",
  attention: "warning",
  // Nothing published yet. An absence the server CHOSE to state, which is what
  // `neutral` means in this scale -- and NOT the same fact as `unknown`.
  no_data: "neutral",
  // `project_overview.py` `PostureState`: the reading exists and this actor may
  // not see it. A question unanswered FOR THIS READER is still a question
  // unanswered, so it keeps the warning diamond it already had as an undeclared
  // word; what changes is that it now has a sentence instead of `Unknown`.
  permission_limited: "warning",

  // `golden_questions.py:106` -- `LIFECYCLES = ("draft", "active",
  // "deprecated", "archived")`. `draft`, `active` and `archived` were already
  // declared; `deprecated` is the fourth and it is a warning rather than an
  // error, because `LIFECYCLE_TRANSITIONS` (l.114) lets it go back to `active`:
  // it is held up, not ended. `archived` is the terminal one and is already
  // `error`.
  deprecated: "warning",

  // `evaluation_runs.py:807,1084-1092` -- an Evaluation Run and a Trace
  // Observation are `recording` until `finalized`. `recording` is a run in
  // flight (a statement about progress, `info`, like `loading`); `finalized` is
  // a run whose evidence is closed and citable, which is a verdict and earns
  // `success`.
  recording: "info",
  finalized: "success",
};

/**
 * The same closed set, read as a person would say it.
 *
 * `replaceAll("_", " ")` is not a translation: it turned `no_rejections` into
 * "no rejections" and `rejected_rows` into "rejected rows", so every state cell
 * of the console printed a database value in lower case. A word is spelled out
 * here only when the fallback would get it wrong or when the product has a
 * better name for it — `Ready to run` for `executable`, `In use` for `used`,
 * `Absent` for `no-cache`. Everything else the fallback capitalizes correctly
 * and is deliberately not repeated.
 */
export const STATE_LABEL: Readonly<Record<string, string>> = {
  active: "Active",
  available: "Available",
  blocked: "Blocked",
  collecting: "Collecting",
  current: "Current",
  degraded: "Degraded",
  discarded: "Discarded",
  executable: "Ready to run",
  unreviewed: "Unreviewed",
  triaged: "Triaged",
  duplicate: "Duplicate",
  resolved: "Resolved",
  dead_letter: "Dead letter",
  failed: "Failed",
  fresh: "Fresh",
  installed: "Installed",
  linked: "Linked",
  "no-cache": "Absent",
  no_rejections: "No rejections",
  not_current: "Not current",
  // States the fact, not the gesture: the word a person reads is still
  // "Expired", and what it adds is that nothing will be accepted with it again.
  credential_expired: "Expired — no longer accepted",
  // Not "Not offered": the sentence has to say WHERE, because the same artifact
  // is offered in another deployment and a reader who has seen it there needs to
  // know the difference is the deployment and not the object.
  not_offered: "Not offered here",
  unmatched: "Not matched",
  observed: "Observed",
  paused: "Paused",
  pending_confirmation: "Awaiting confirmation",
  pinned: "Pinned",
  published: "Published",
  ready: "Ready",
  rejected: "Rejected",
  rejected_rows: "Rejected rows",
  revoked: "Revoked",
  rotating: "Rotating",
  stale: "Stale",
  unavailable: "Unavailable",
  // The words whose fallback would be a database value or plainly worse than
  // what a screen had already written. Everything else the fallback spells.
  absent: "Nothing reported",
  change_pending: "Change pending",
  domain_pending: "Waiting for its delivery address",
  delivery_failed: "Delivery failed",
  no_insight: "Nothing worth saying",
  none: "Not mapped",
  not_installed: "Not set up",
  not_applicable: "Not applicable",
  // THE USER'S WORD, NOT THE COLUMN'S. `ConnectorsCatalog` said "Turned off"
  // and the fallback would have said "Disabled" -- CLAUDE.md is explicit that
  // the vocabulary is the user's, so the better word wins for every screen at
  // once instead of for the one that happened to write it.
  disabled: "Turned off",
  needs_review: "Needs review",
  // Not « Warning »: the tone already says that, and a label that repeats its
  // own colour tells the reader nothing. What the compiler means by the word is
  // that the item is usable and somebody has to accept it first — which is the
  // gesture the Schedule step then asks for, one acknowledgement per warning.
  warning: "Needs acknowledgement",
  rollback_available: "Rollback available",
  unset: "Never configured",
  verifying: "Never checked",
  unused: "Not used yet",
  used: "In use",
  versioned: "Versioned",

  // Story 76-5. Only the words whose fallback would be worse than the sentence
  // a reader needs; `trusted`, `deprecated`, `recording` and `finalized` the
  // fallback spells correctly and are deliberately not repeated.
  //
  // "Attention" alone is a noun and reads as a heading, not as a verdict.
  attention: "Needs attention",
  // Not "No data": the fact is that nothing has been PUBLISHED yet, and a
  // reader who sees rows in the workbench would read the shorter word as a
  // contradiction.
  no_data: "Nothing published yet",
  // States the fact and says whose limit it is. The fallback would say
  // "Permission limited", which reads as a property of the object.
  permission_limited: "Permission limited",
};

/** What an undeclared state reads as, in both halves of the answer. */
export const UNKNOWN_STATE_TONE: Tone = "warning";
export const UNKNOWN_STATE_LABEL = "Unknown";

/**
 * The tone a state reads as. ONE argument — see the header.
 *
 * A word this file does not declare is a `warning`, never a `neutral`. The
 * previous default drew every unread word in the colour of "nothing to see", so
 * a server that started sending a new state shipped it silently and grey.
 */
export function stateTone(value: string | null | undefined): Tone {
  return STATE_TONE[normalize(value)] ?? UNKNOWN_STATE_TONE;
}

/**
 * The sentence a state reads as. ONE argument.
 *
 * THE FALLBACK IS FOR DECLARED WORDS ONLY. `STATE_LABEL` deliberately does not
 * repeat the words `replaceAll("_", " ")` already capitalizes correctly, so a
 * word `STATE_TONE` declares still gets its sentence from the fallback —
 * `outcome_unknown` reads "Outcome unknown" and no second list has to be kept in
 * step. A word DECLARED NOWHERE is different: it used to read "Half written" for
 * `half_written`, which looks like a product word and is a database value, so a
 * reader could not tell a state the console understands from one it has never
 * heard of. It reads `Unknown` now, in the warning colour `stateTone` gives it.
 *
 * Empty, `null` and whitespace take that same answer, and no longer
 * `Unavailable`: a missing state is a question nobody answered, not the absence
 * the server would have named had it meant one.
 */
export function stateLabel(value: string | null | undefined): string {
  const key = normalize(value);
  const known = STATE_LABEL[key];
  if (known) return known;
  if (!(key in STATE_TONE)) return UNKNOWN_STATE_LABEL;
  const spaced = key.replaceAll(/[_-]+/g, " ").trim();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}
