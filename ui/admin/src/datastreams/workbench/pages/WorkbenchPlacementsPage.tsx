/**
 * The `Placements` tab — a media plan met by THIS connector's observed spend.
 *
 * IT EXISTS BECAUSE THE CAPABILITY OPENED IT. `datastream-workbench-and-wizard.md`,
 * amendment « Une capacité activée AJOUTE son onglet » — named, never numbered,
 * because that document's line numbers moved in the commit that applied its
 * predecessor. When `placement_mapping` is off there is no tab, no panel and no
 * reserved width: this component is not mounted, and its address does not resolve.
 *
 * EVERY LINE OF THE PLAN IS DRAWN, matched or not. `app.media_plan_lines` carries
 * no connector; the connector is named by the MATCH. So "only the lines that name
 * this connector" is a reading, not a filter — each row says whether this
 * connector is attached, and the unmatched ones are exactly what a person opens
 * this tab to fix.
 *
 * THREE LEVELS, AND THE THIRD ONLY WHERE IT CAN EXIST: line → campaign →
 * placements. A placement is `(breakdown_dimension, breakdown_value)` of a
 * dimension the connector DECLARES, and 37 of the 39 declare none. On those the
 * panel says so and offers no control, rather than an empty picker that implies
 * something could be chosen.
 *
 * EVERY NUMBER IS THE SERVER'S. No count is recomposed here, no total is summed,
 * and an absent count is rendered as its reason — never as `0`.
 *
 * EVERY STATE WORD IS THE SERVER'S TOO — story 61.2. This screen used to compose
 * its own reading out of a boolean (`attached`), a count (`line_counts`), a
 * separate panel and nothing at all, so four states existed on screen and none of
 * them had a name anywhere. They now arrive named and labelled from
 * `core/plan_matching_states.py`, and every sentence below — the reason a spend
 * row is here, the two emptinesses, the refusal of an acceptance with no reason —
 * comes down the wire rather than being written twice.
 *
 * NO RAW DATABASE WORD REACHES A PERSON. `active` and `orphaned` are a lifecycle
 * of `app.plan_line_mappings`; what a reader needs is what it costs them, which is
 * `status_label`.
 *
 * EVERY MATCH SAYS HOW IT WAS OBTAINED — story 61.3. `Exact code`, `Normalized
 * name`, `Name similarity`, `Matched by hand`: the four words of
 * `core/plan_matching_states.py`, labelled by the server, never composed here. A
 * NUMBER appears beside one of them only — `Name similarity` — because the other
 * two are 1.0 by construction and printing a tautology as a measurement is worse
 * than printing nothing. A match written before migration 246 has no level and
 * says so; it is never drawn as `Matched by hand`, which would claim somebody
 * typed it.
 *
 * AND THE WORD `AI` IS NOT ON THIS SCREEN. `epic-61:41` calls the fuzzy tier a
 * fuzzy AI proposal; behind it is `difflib` at 0.88, with no model and no network
 * call. Naming a string comparison an intelligence is a promise nothing here
 * keeps.
 *
 * THE FOURTH STATE IS ASKED FOR, NOT PAID FOR ON EVERY OPEN — story 61.3,
 * arbitrage A4. `Suggest matches` sweeps every line of the plan against every
 * campaign of this connector over the plan's window; that sweep is a cost, and
 * charging it to everybody who opens the tab would buy a badge most of them never
 * look at. So the tab says so, and `To arbitrate` is drawn only on the payload
 * that carries the candidates beside it.
 *
 * ---------------------------------------------------------------------------
 * AMENDED 2026-08-18 — THE WORKSHOP HAD NO WAY TO NARROW, AND NO WAY TO ACT
 * TWICE.
 *
 * FOUR CHANGES, none of which adds a number this screen composes.
 *
 *   1. THE PLAN LINES NARROW. A plan is every line of its active version, matched
 *      and unmatched alike — that is the ratified reading and it is not touched —
 *      but "show me the ones nobody has matched" was not askable, so a plan of
 *      eighty lines answered the question this tab exists for only by scrolling.
 *      The chips are the SERVER'S state words (`matching_state_label`), drawn
 *      only for the states the lines in hand actually carry: this table is the
 *      whole plan, so its states ARE the collection's, and a chip here cannot
 *      narrow a sample the way the run list's could.
 *   2. THE BUDGET SORTS. `SortableHead`, and `SortScopeNote` beside it — the
 *      plan arrives whole, but a budget with no amount is an ABSENCE and
 *      `sortRows` keeps it at the bottom whichever way the arrow points rather
 *      than promoting it as a zero.
 *   3. ATTACHING IS PLURAL. « attacher un ou plusieurs placements » is the
 *      ratified `Permet` line; the screen offered one picker and one button, so
 *      Feed + Marketplace + Search results was three round trips and three
 *      chances to stop half way. The route now takes `breakdown_values` in ONE
 *      transaction (`_placement_values`), so a refusal on the third attaches
 *      none of the three.
 *   4. `Exact code` CONFIRMS AS A SET, under ONE confirmation that names the
 *      count BEFORE the act. Forty identical dialogs is how people learn to
 *      click past a dialog; one dialog naming forty is a decision. It is offered
 *      for `Exact code` ALONE — the tier that is an equality of raw strings, not
 *      a resemblance — and the softer tiers stay one at a time, deliberately.
 *
 * AND WHAT IS STILL NOT HERE, VERIFIED RATHER THAN ASSUMED: nothing withdraws an
 * acceptance. `core/plan_spend_decisions.py` exposes `list_decisions` and
 * `accept_unmatched_spend` and no third function; the store is
 * `ON CONFLICT DO NOTHING` precisely so the FIRST decision and its author stand.
 * There is no route to call, so the dialog says so and names no gesture — a
 * screen that sent somebody to undo this somewhere else would be sending them
 * nowhere.
 *
 * ---------------------------------------------------------------------------
 * AMENDED 2026-08-24 — THE TAB SENT PEOPLE AWAY FOR THE ONE THING IT DOES.
 *
 * Story 67.26 repaired the branch where a Project has NO plan and stopped there.
 * As soon as a plan existed, three places on this same tab still named a
 * Governance screen:
 *
 *   * a header button, `Open media plans in Governance`, built from
 *     `governance_owner_reference` — which resolves to `governance` >
 *     `master-data`. That section is real and holds SIX lenses (Business
 *     Domains, Classifications, Products, Activities, Registries, Competitor
 *     Registry, `shell/navigation/governance.ts`); not one of them is a media
 *     plan, and `grep -rln "mediaplan" ui/admin/src/governance/` still returns
 *     no file. The button opened a screen that cannot answer the sentence on it;
 *   * the `no_line_names_this_connector` banner — « Matching a campaign to a
 *     line is done in Governance »;
 *   * the empty state of an unfolded line — « Matching a campaign to a plan line
 *     is a Governance decision ».
 *
 * BOTH SENTENCES WERE FALSE ABOUT THIS TAB, not only about Governance. Matching
 * IS a gesture of this page and has been since story 61.3: `askForMatches`
 * reads `GET …/placements/suggestions`, `confirmMatch` and `confirmBulk` write
 * `POST …/placements/matches`. The screen was sending somebody elsewhere for the
 * act its own two buttons perform — the exact defect `analyze-and-test.md:1581`
 * already names in the other direction (« says so in its empty state rather than
 * offering a door that does not exist »), and CLAUDE.md's « Ce que la personne
 * vient faire doit pouvoir se faire ici ».
 *
 * SO THE DOOR IS GONE AND THE GESTURE IS NAMED WHERE IT IS ASKED FOR. The empty
 * state of a line with no campaign carries the `Suggest matches` control itself:
 * a person who unfolds a line to match it now matches it there, instead of
 * reading that somebody else does it. `governance_owner_reference` is still on
 * the payload and this screen no longer reads it — the reference is not wrong,
 * it simply has no reading to open, and inventing one here would be the same
 * defect with a new address.
 */
import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import {
  Badge,
  Button,
  Checkbox,
  ChoiceGroup,
  ConfirmDialog,
  CopyButton,
  EmptyState,
  Input,
  Metric,
  NativeSelect,
  ObjectId,
  Panel,
  PanelHeader,
  SortScopeNote,
  SortableHead,
  Status,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  TableScroll,
  Textarea,
  sortRows,
  useTableSort,
  stateTone,
  Retry,
} from "../../../ui";
import { apiDelete, apiGet, apiPost } from "../../../lib/apiFetch";
import { moneyText, nullableText, numberText, record, records, text } from "../evidence";
import type { WorkbenchTabPayload } from "../workbenchTypes";
import type { OwnerReference } from "../../../shell/pages/ProjectSettings";
import {
  NO_CARRIER_DESCRIPTION,
  NO_MEDIA_PLAN_DESCRIPTION,
  NO_MEDIA_PLAN_TITLE,
  carrierDoorLabel,
  carrierSentence,
} from "../../../analyze-artifacts/mediaPlanAbsence";

type Row = Record<string, unknown>;

interface Props {
  payload: WorkbenchTabPayload;
  projectId: string;
  datastreamId: string;
  /**
   * The connector's name, FOR READING. It is a label: the identity that scopes
   * the payload and both writes is `module_name`, and the server reads it from
   * `app.datastreams` on every call rather than trusting anything sent from here.
   */
  connector: string | null;
  onOpenOwner?: (owner: OwnerReference) => void;
}

function base(projectId: string, datastreamId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/datastreams/${encodeURIComponent(datastreamId)}/workbench`;
}

/**
 * The key under which one campaign's chosen placement is held, DEFINED ONCE.
 *
 * `\u0000` WRITTEN AS THE SOURCE ESCAPE, never as the byte itself. The separator
 * has to be a character no `line_key` and no `campaign_ref` can contain — the
 * same reason `DatastreamWorkbenchRoute` composes its reload key with it — but a
 * raw NUL in the file makes `ripgrep` declare it binary, and every repository
 * sweep built on `rg` (`check_line_citations.py`, `scripts/screens.py`) then
 * stops reading this file entirely. Same key, same guarantee, still greppable.
 *
 * It is a function because the reader and the writer both need it: the picker
 * stores under it and `attach` reads from it, and two spellings of one key is a
 * control that silently stops finding what it stored.
 */
function chosenKey(lineKey: string, campaignRef: string): string {
  return `${lineKey}\u0000${campaignRef}`;
}

/** The pending detach, held with the count it will change. */
interface PendingDetach {
  id: string;
  value: string;
  lineLabel: string;
  campaignRef: string;
  siblings: number;
}

/**
 * The pending acceptance, held with the count of rows still waiting BEFORE it.
 *
 * Story 61.2. It is not a destructive gesture — no row is removed, no amount
 * moves — but it is one nothing on this tab can withdraw, so it is confirmed with
 * the same rule the detach follows: the scope is named with the count taken
 * before the change, never with what is left after it.
 */
interface PendingAcceptance {
  campaignRef: string;
  spend: string;
  awaiting: string;
}

/**
 * The pending confirmation of a proposed match — story 61.3.
 *
 * It carries `matchesToday` because the write REPLACES the whole set of matches
 * that plan line carries, across every connector (`set_line_mappings` deletes and
 * re-inserts). The count is taken BEFORE the act, like the detach above: a
 * confirmation naming what is left afterwards describes a scope nobody was asked
 * about.
 */
interface PendingConfirm {
  lineKey: string;
  lineLabel: string;
  campaignRef: string;
  levelLabel: string | null;
  score: number | null;
  matchesToday: string;
  contested: string | null;
}

/**
 * The pending bulk confirmation — amended 2026-08-18.
 *
 * It holds the pairs it will write and the count of plan lines they touch,
 * because those are two different numbers: forty proposals can land on
 * thirty-eight lines, and confirming REPLACES every match each of those lines
 * carries. Naming only the proposal count would describe half the scope.
 */
interface PendingBulk {
  pairs: Array<{ lineKey: string; campaignRef: string }>;
  lines: number;
  levelLabel: string;
}

/** The chip that selects no matching state at all. Never a state name. */
export const ALL_LINES = "all";

/**
 * The chips over the plan lines — the SERVER'S words, and only the ones present.
 *
 * `matching_state_label` comes down the wire from `core/plan_matching_states.py`;
 * composing a label here would be the fifth vocabulary that module exists
 * against. A state no line of this plan carries gets no chip, for the reason the
 * run list gives: a filter that empties the screen is a filter nobody uses twice.
 */
export function lineStateChoices(lines: Array<Record<string, unknown>>) {
  const present = new Map<string, string>();
  for (const line of lines) {
    const state = text(line.matching_state, "");
    if (state && !present.has(state)) present.set(state, text(line.matching_state_label, state));
  }
  return [
    { value: ALL_LINES, label: "All lines" },
    ...[...present.entries()].map(([value, label]) => ({ value, label })),
  ];
}

/*
 * THE PRIVATE TONE MAP THAT USED TO LIVE HERE IS GONE (story 76-2). It was a
 * seventh state map — a function, so `stateVocabulary.test.ts` could not see it
 * — and its four words are DECLARED in `ui/stateVocabulary.ts` now, with the
 * reading this file had written down kept intact: `matched` and `accepted` are
 * answers (success); `unmatched` is a question and a question is not an error —
 * it is the work of this tab (neutral); `ambiguous` is a question somebody has
 * to answer NOW, nothing wrong but nothing moving (warning).
 */

/**
 * How a match was obtained, in one badge — story 61.3.
 *
 * THE NUMBER IS PART OF THE LABEL AND ONLY FOR ONE LEVEL. The server sends
 * `match_score` as `null` for everything but `Name similarity`, so this reads it
 * rather than deciding it: `exact` and `normalized` are 1.0 by construction, and
 * a 1.0 printed beside a real 0.89 invites a comparison between a tautology and a
 * measurement. A level the server did not name renders as NOTHING — never the raw
 * token, never a fabricated word.
 */
function matchLevel(source: Row): string | null {
  const label = nullableText(source.match_method_label);
  if (!label) return null;
  return typeof source.match_score === "number"
    ? `${label} ${numberText(source.match_score)}`
    : label;
}

export default function WorkbenchPlacementsPage({
  payload,
  projectId,
  datastreamId,
  connector,
  onOpenOwner,
}: Props) {
  /**
   * The evidence ON SCREEN, which is the server's — the tab payload at first,
   * then whatever a re-read returned.
   *
   * A LOCAL RE-READ AND NOT A ROUTE RELOAD, because both gestures of this tab
   * change one plan's matches and nothing else on the Workbench: reloading the
   * route would re-read the header, the axes and the issue badge to redraw a
   * table. The plan selector travels the same way, for the same reason.
   */
  const [evidence, setEvidence] = useState<Row>(payload.evidence as Row);
  const [reading, setReading] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [openLine, setOpenLine] = useState<string | null>(null);
  const [pending, setPending] = useState<PendingDetach | null>(null);
  const [attaching, setAttaching] = useState<string | null>(null);
  /** The placements ticked for one campaign. A LIST per (line, campaign) since
   *  2026-08-18: a single string could not express « un ou plusieurs ». */
  const [chosen, setChosen] = useState<Record<string, string[]>>({});
  /** The two narrowings over the plan lines, and the order of the page. */
  const [lineState, setLineState] = useState<string>(ALL_LINES);
  const [lineSearch, setLineSearch] = useState("");
  const { sort, toggleSort } = useTableSort(null);
  const [bulk, setBulk] = useState<PendingBulk | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkError, setBulkError] = useState<string | null>(null);
  const [accepting, setAccepting] = useState<PendingAcceptance | null>(null);
  const [acceptReason, setAcceptReason] = useState("");
  const [acceptBusy, setAcceptBusy] = useState(false);
  const [acceptError, setAcceptError] = useState<string | null>(null);
  /**
   * The candidate set — story 61.3. `null` until somebody ASKS for it, which is
   * the whole arbitrage: the sweep is lines × campaigns over the plan's window,
   * and it is paid by the person who wants it and by nobody else.
   */
  const [proposals, setProposals] = useState<Row | null>(null);
  const [suggesting, setSuggesting] = useState(false);
  const [suggestFailure, setSuggestFailure] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<PendingConfirm | null>(null);
  const [confirmBusy, setConfirmBusy] = useState(false);
  const [confirmError, setConfirmError] = useState<string | null>(null);

  useEffect(() => setEvidence(payload.evidence as Row), [payload]);

  /** How this connector is CALLED on screen.
   *
   *  `null` when the header carried neither a display name nor a module, which is
   *  a Datastream whose source is unknown — "this connector" is then the honest
   *  word, and printing `null` or an empty string in six places is not. */
  const label = text(connector, "this connector");

  const capability = record(evidence.capability) ?? {};
  const plans = records(evidence.plans);
  const selectedPlan = record(evidence.selected_plan);
  const planId = text(selectedPlan?.id, "");
  const dimension = record(evidence.placement_dimension) ?? {};
  const grain = record(evidence.grain);
  const counts = record(evidence.line_counts);
  const observed = record(evidence.observed_placements) ?? {};
  const unmapped = record(evidence.unmapped);
  const unmappedCounts = record(unmapped?.counts);
  const lines = records(evidence.lines);
  /** Which currency each amount below is in — story 61.4. Composed by the server,
   *  never here: three currencies exist (the plan's, the one the warehouse
   *  converted the spend into, and the confirmed Money Policy's) and deciding
   *  between them on a screen would be a second authority on the question the
   *  marts already answer. */
  const money = record(evidence.money);
  const planCurrency = nullableText(money?.plan_currency);
  const spendCurrency = nullableText(money?.spend_currency);
  const analyzeReference = record(money?.analyze_reference);
  const ambiguity = record(evidence.ambiguity);
  const proposalLines = records(proposals?.lines);
  const proposalWindow = record(proposals?.window);
  /**
   * EVERY `Exact code` PROPOSAL NOBODY HAS ARBITRATED, as pairs.
   *
   * THE TOKEN, NOT THE LABEL. `match_method` is what the server stores;
   * `match_method_label` is what it calls it to a person, and matching on the
   * label would break the day somebody improves the wording. Only this tier is
   * offered as a set: `exact` is an equality of raw provider codes, so a whole
   * page of them is one decision — « are these our codes » — whereas a page of
   * 0.89 resemblances is forty different decisions wearing one badge.
   *
   * `already_matched` candidates are excluded: confirming one writes nothing and
   * would inflate the count the dialog names.
   */
  const exactProposals = useMemo(() => {
    const pairs: Array<{ lineKey: string; campaignRef: string }> = [];
    const lineKeys = new Set<string>();
    for (const line of proposalLines) {
      for (const candidate of records(line.candidates)) {
        if (candidate.match_method !== "exact" || candidate.already_matched === true) continue;
        pairs.push({
          lineKey: text(line.line_key),
          campaignRef: text(candidate.campaign_ref),
        });
        lineKeys.add(text(line.line_key));
      }
    }
    return { pairs, lines: lineKeys.size };
  }, [proposalLines]);
  /** Does any match on this plan carry no level at all? Then the reason is said
   *  once, at the top, rather than beside every row that has none. */
  const unrecordedLevels = lines.some((line) =>
    records(line.campaigns).some((campaign) => campaign.match_method === null),
  );

  /** The chips this plan's lines can actually be narrowed by — the server's words. */
  const lineChoices = useMemo(() => lineStateChoices(lines), [lines]);
  const narrowedLines = lineState !== ALL_LINES || lineSearch.trim().length > 0;
  /**
   * The lines DRAWN: narrowed, then ordered.
   *
   * The search is on the LABEL and on the line key, which are the two strings a
   * person can have been handed elsewhere. It is case-insensitive and a plain
   * containment — a plan line label is a phrase somebody typed into a
   * spreadsheet, not an identifier, so anchoring it would answer nothing.
   *
   * A budget that is absent stays at the bottom of BOTH directions: no amount is
   * not the smallest amount, and reversing the column must not promote every
   * line nobody has costed to the top.
   */
  const visibleLines = useMemo(() => {
    const needle = lineSearch.trim().toLowerCase();
    const kept = lines.filter((line) => {
      if (lineState !== ALL_LINES && text(line.matching_state, "") !== lineState) return false;
      if (!needle) return true;
      const label = `${text(line.label, "")} ${text(line.line_key, "")}`.toLowerCase();
      return label.includes(needle);
    });
    return sortRows(kept, sort, (line, key) => {
      if (key !== "budget") return null;
      const raw = Number(line.budget);
      return Number.isFinite(raw) ? raw : null;
    });
  }, [lines, lineSearch, lineState, sort]);

  const reread = useCallback(
    async (nextPlanId: string) => {
      setReading(true);
      setFailure(null);
      // A CANDIDATE SET BELONGS TO THE PLAN IT WAS COMPUTED ON. Keeping it across
      // a plan change would show the arbitrations of one plan over the lines of
      // another, which is a screen that looks right and is false.
      setProposals(null);
      setSuggestFailure(null);
      try {
        // `apiGet`, never a bare `fetch`: it is the one seam that attaches the
        // bearer, and it throws an `ApiError` carrying the SERVER'S sentence —
        // `placement_evidence_unavailable` says "we could not look", which must
        // never be rendered as "there is nothing".
        const body = await apiGet<{ evidence?: Row }>(
          `${base(projectId, datastreamId)}/placements?plan_id=${encodeURIComponent(nextPlanId)}`,
        );
        setEvidence((body?.evidence ?? {}) as Row);
      } catch (error: unknown) {
        setFailure(error instanceof Error ? error.message : "Placement evidence could not be read.");
      } finally {
        setReading(false);
      }
    },
    [datastreamId, projectId],
  );

  const observedValues = useMemo(
    () => records(observed.values).map((value) => text(value.breakdown_value, "")),
    [observed.values],
  );

  /** The observed values NOT already attached to this campaign.
   *
   *  Offering an attached placement again would be a control whose only outcome
   *  is a no-op — the unique index reactivates the row and nothing changes on
   *  screen — and a control that does nothing reads as a broken one. */
  const attachable = useCallback(
    (attached: Row[]): string[] => {
      const already = new Set(attached.map((placement) => text(placement.breakdown_value, "")));
      return observedValues.filter((value) => value && !already.has(value));
    },
    [observedValues],
  );

  // THE CAPABILITY IS OFF AND THIS COMPONENT IS NOT MOUNTED. If it ever is — a
  // stale payload, a hand-built mount — it says the one true thing and draws no
  // plan, rather than an empty table that reads as "nothing is planned".
  if (capability.active !== true) {
    return (
      <Status as="block" tone="neutral" title="Placement Mapping is not active on this Project">
        {text(evidence.reason, "No media plan is read for this Datastream.")}
      </Status>
    );
  }

  /**
   * Attach every placement ticked for this campaign, in ONE act.
   *
   * `breakdown_values` and not `breakdown_value`: the route wraps the loop in a
   * single transaction, so three values either all hang from the campaign or
   * none does. Three separate calls could leave a person with the first two
   * attached and a refusal on the third — a line in a state nobody chose, and
   * one they would have to undo by hand.
   */
  async function attach(lineKey: string, campaignRef: string) {
    const key = chosenKey(lineKey, campaignRef);
    const values = chosen[key] ?? [];
    if (values.length === 0) return;
    setAttaching(key);
    setFailure(null);
    try {
      await apiPost(`${base(projectId, datastreamId)}/placements/attachments`, {
        plan_id: planId,
        line_key: lineKey,
        campaign_ref: campaignRef,
        breakdown_dimension: dimension.dimension,
        breakdown_values: values,
      });
      // The tick marks belong to the list that has just changed; keeping them
      // would leave a checkbox on a value that is now attached above it.
      setChosen((current) => ({ ...current, [key]: [] }));
      await reread(planId);
    } catch (error: unknown) {
      setFailure(error instanceof Error ? error.message : "The placement could not be attached.");
    } finally {
      setAttaching(null);
    }
  }

  /** Tick or untick one observed value for one campaign. */
  function toggleChosen(key: string, value: string) {
    setChosen((current) => {
      const held = current[key] ?? [];
      return {
        ...current,
        [key]: held.includes(value) ? held.filter((v) => v !== value) : [...held, value],
      };
    });
  }

  /**
   * Confirm a whole tier of proposals — story 61.3 extended 2026-08-18.
   *
   * ONE request, one transaction, and the level is STILL not sent: the server
   * asks the engine again for each pair. A console able to send forty pairs AND
   * their level could stamp `Exact code` over forty resemblances in one press,
   * which is the exact promise `no silent value fusion` refuses.
   */
  async function confirmBulk() {
    if (!bulk) return;
    setBulkBusy(true);
    setBulkError(null);
    try {
      await apiPost(`${base(projectId, datastreamId)}/placements/matches`, {
        plan_id: planId,
        matches: bulk.pairs.map((pair) => ({
          line_key: pair.lineKey,
          campaign_ref: pair.campaignRef,
        })),
      });
      setBulk(null);
      setProposals(null);
      await reread(planId);
    } catch (error: unknown) {
      setBulkError(
        error instanceof Error ? error.message : "These matches could not be confirmed.",
      );
    } finally {
      setBulkBusy(false);
    }
  }

  /**
   * Ask for the matches — the gesture that arms `core/plan_mapping_suggest.py`.
   *
   * `apiGet`, never a bare `fetch`: an unreachable mart answers 503
   * `placement_evidence_unavailable`, whose sentence is "we could not look", and
   * it must never be rendered as the empty one, which says "nothing resembles
   * anything closely enough".
   */
  async function askForMatches() {
    setSuggesting(true);
    setSuggestFailure(null);
    try {
      const body = await apiGet<{ evidence?: Row }>(
        `${base(projectId, datastreamId)}/placements/suggestions?plan_id=${encodeURIComponent(planId)}`,
      );
      setProposals((body?.evidence ?? {}) as Row);
    } catch (error: unknown) {
      setProposals(null);
      setSuggestFailure(
        error instanceof Error ? error.message : "The proposed matches could not be read.",
      );
    } finally {
      setSuggesting(false);
    }
  }

  async function confirmMatch() {
    if (!confirming) return;
    setConfirmBusy(true);
    setConfirmError(null);
    try {
      // THE LEVEL IS NOT SENT. The server asks the engine again and records what
      // it computed; a console able to state a level could write `Exact code`
      // over a 0.89 resemblance, and the number beside `Name similarity` would
      // stop being worth reading.
      await apiPost(`${base(projectId, datastreamId)}/placements/matches`, {
        plan_id: planId,
        line_key: confirming.lineKey,
        campaign_ref: confirming.campaignRef,
      });
      setConfirming(null);
      await reread(planId);
    } catch (error: unknown) {
      setConfirmError(
        error instanceof Error ? error.message : "This match could not be confirmed.",
      );
    } finally {
      setConfirmBusy(false);
    }
  }

  async function confirmAcceptance() {
    if (!accepting) return;
    // THE REFUSAL IS THE SERVER'S SENTENCE, shown before the round trip rather
    // than written a second time here. An acceptance with no reason is
    // indistinguishable from a row somebody clicked past, and the database
    // refuses it too.
    if (!acceptReason.trim()) {
      setAcceptError(text(unmapped?.reason_required_message, "A reason is required."));
      return;
    }
    setAcceptBusy(true);
    setAcceptError(null);
    try {
      await apiPost(`${base(projectId, datastreamId)}/placements/spend-decisions`, {
        plan_id: planId,
        campaign_ref: accepting.campaignRef,
        reason: acceptReason.trim(),
      });
      setAccepting(null);
      setAcceptReason("");
      await reread(planId);
    } catch (error: unknown) {
      setAcceptError(
        error instanceof Error ? error.message : "This spend could not be accepted.",
      );
    } finally {
      setAcceptBusy(false);
    }
  }

  async function confirmDetach() {
    if (!pending) return;
    setFailure(null);
    try {
      await apiDelete(
        `${base(projectId, datastreamId)}/placements/attachments/${encodeURIComponent(pending.id)}?plan_id=${encodeURIComponent(planId)}`,
      );
      setPending(null);
      await reread(planId);
    } catch (error: unknown) {
      setFailure(error instanceof Error ? error.message : "The placement could not be detached.");
    }
  }

  if (evidence.state === "no_plan") {
    /* LA MEME PHRASE QUE `PacingReport`, ET LA MEME PORTE — amendé le
       2026-08-24, parce que c'est une classe et non un écran.

       Le bouton qui était ici ouvrait « media plans in Governance » : mesuré le
       2026-08-22, `grep -rln "mediaplan" ui/admin/src/governance/` ne rendait
       AUCUN fichier, et il a été retiré. Ce qui manquait n'était pas la porte,
       c'était la décision : où un plan se fabrique. Elle est rendue — le
       Workbench du Datastream porteur — donc la porte revient, vers un écran qui
       existe cette fois, et le serveur en donne l'adresse (`carriers`).

       ET SI LE PROJET N'A AUCUN PORTEUR, aucune porte n'est dessinée : la phrase
       nomme alors le geste qui en crée un, et cet onglet-ci ne crée pas de
       Datastream. Inventer une adresse serait remettre le même défaut. */
    const carriers = records(evidence.carriers);
    const door = carriers[0];
    return (
      <div className="grid gap-4">
        <Panel flush>
          <PanelHeader title={`Placements · ${label}`} description={text(grain?.reason, "")} />
          <div className="p-5">
            <EmptyState
              title={text(evidence.reason, NO_MEDIA_PLAN_TITLE)}
              description={
                door
                  ? `${NO_MEDIA_PLAN_DESCRIPTION} ${carrierSentence({ name: text(door.name), plan_name: nullableText(door.plan_name) })}`
                  : `${NO_MEDIA_PLAN_DESCRIPTION} ${NO_CARRIER_DESCRIPTION}`
              }
              action={
                door && onOpenOwner ? (
                  <Button
                    data-testid="placements-open-carrier"
                    onClick={() =>
                      /* LES ONZE CLÉS, TOUTES POSÉES. `resolveOwnerReference`
                         refuse une référence dont `surface` n'est pas
                         `"project"` et lit `object_type` / `tab` contre le
                         contrat de navigation : une référence partielle serait
                         refusée à voix haute, ce qui est mieux qu'un clic mort
                         mais reste une porte qui n'ouvre pas. */
                      onOpenOwner({
                        surface: "project",
                        workspace: "data",
                        section: "datastreams",
                        global_surface: null,
                        global_section: null,
                        object_type: "datastream",
                        object_id: text(door.datastream_id),
                        tab: "data",
                        action: null,
                        version_id: null,
                      } as unknown as OwnerReference)
                    }
                  >
                    {carrierDoorLabel(text(door.name))}
                  </Button>
                ) : undefined
              }
            />
          </div>
        </Panel>
      </div>
    );
  }

  return (
    <div className="grid gap-4">
      <Panel flush>
        <PanelHeader
          title={`Placements · ${label}`}
          description={text(grain?.reason, "")}
          actions={
            <div className="flex items-center gap-3">
              {/* THE PLAN SELECTOR — arbitrage A5. No Project designates an
                  active plan (only a VERSION carries `is_active`), so which plan
                  is read is a choice, and concatenating them would destroy the
                  meaning of "1 line of 3". */}
              <NativeSelect
                aria-label="Media plan"
                value={planId}
                disabled={reading}
                onChange={(event) => void reread(event.target.value)}
              >
                {plans.map((plan) => (
                  <option key={text(plan.id)} value={text(plan.id)}>
                    {text(plan.name, text(plan.id))}
                  </option>
                ))}
              </NativeSelect>
              {/* AND NOTHING BESIDE IT. `Open media plans in Governance` stood
                  here until 2026-08-24: it was built from
                  `governance_owner_reference`, which resolves to `governance` >
                  `master-data` — six lenses, none of them a media plan. A
                  control that opens a screen unable to answer the sentence on
                  it is worse than an absence, because it makes somebody look. */}
            </div>
          }
        />
      </Panel>

      {failure && (
        <Status as="block" tone="error" title="Placement evidence unavailable"
          action={<Retry onClick={() => void reread(planId)} />}
        >
          {failure}
        </Status>
      )}

      {/* WHICH CURRENCY THE AMOUNTS BELOW ARE IN — and where the comparison of
          them lives, which is NOT here.

          61.4 adds NO budget-versus-actual panel to this tab. Two ratified
          documents place that reading in Analyze — `analyze-and-test.md`
          ("Pacing ... is an Analyze reading. It is not an ingestion surface") and
          the `Data` row of `placement-mapping.md`, which lists what this tab must
          contain and names no budget-versus-actual. A second place to read one
          figure is a second answer to one question.

          What this panel carries instead is the thing the tab genuinely owed: the
          currency of each amount it already draws, and the refusal when two of
          them are not the same one. The sentence is the SERVER'S — the same
          module the marts read their rule from — so the screen cannot invent a
          seventh way of saying it. */}
      {money && (
        <Status
          as="block"
          tone={money.comparable === true ? "neutral" : "warning"}
          title={
            money.comparable === true
              ? "One currency"
              : "The budget and the observed spend are not in the same currency"
          }
          action={
            analyzeReference && onOpenOwner ? (
              <Button
                variant="secondary"
                size="sm"
                onClick={() => onOpenOwner(analyzeReference as unknown as OwnerReference)}
              >
                {text(money.analyze_label, "Open Analyze")}
              </Button>
            ) : undefined
          }
        >
          <span className="grid gap-2">
            <span>{text(money.message)}</span>
            <span className="text-text-secondary">{text(money.analyze_reason)}</span>
            {/* NAMED AS NON-EXISTENT, NOT SILENTLY OMITTED. The plan asks for a
                KPI variance; `app.media_plan_lines` carries eleven columns and
                none of them is a KPI target, so there is nothing to vary
                against. Saying it with the count is what stops it being asked
                for again next quarter. */}
            <span className="text-text-secondary">{text(money.kpi_variance_reason)}</span>
          </span>
        </Status>
      )}

      <Panel
        flush
        role="group"
        aria-label="Placement matching summary"
        className="grid grid-cols-3 divide-x divide-divider-base max-lg:grid-cols-1"
      >
        <Metric
          label="Plan lines matched"
          value={counts ? `${numberText(counts.matched)} / ${numberText(counts.total)}` : "Unavailable"}
          // `matched` and not `attached` — story 61.2. A line whose every match is
          // no longer in the plan's active version receives nothing from the mart
          // (`plan_vs_actual_daily.sql` ventilates on `status = 'active'` alone),
          // so counting it as attached would report a budget as covered when it
          // gets no money.
          hint={`Lines of this plan a ${label} campaign is actively ventilating, out of every line of its active version.`}
        />
        {/* EACH SENTENCE IS WRITTEN ONCE. `grain.reason` and
            `placement_dimension.reason` are the server's words and they belong to
            the panel header and the banner below; repeating them in these hints
            printed one finding as if it were two, and a reader could not tell
            whether the screen was saying the same thing or two things. */}
        <Metric
          label="Datastreams on this connector"
          value={
            typeof grain?.datastreams_on_connector === "number"
              ? numberText(grain.datastreams_on_connector)
              : <span className="text-text-secondary">Not counted</span>
          }
          hint="The slice below is this connector's, shared by every Datastream that collects from it."
        />
        <Metric
          label="Observed placements"
          value={
            observed.state === "available"
              ? numberText(observedValues.length)
              : <span className="text-text-secondary">None observable</span>
          }
          hint={`Distinct ${text(dimension.dimension, "placement")} values reported inside the plan's window.`}
        />
      </Panel>

      {dimension.declared !== true && (
        <Status as="block" tone="neutral" title="This connector observes no placement">
          {text(dimension.reason)}
        </Status>
      )}

      {/* THE EMPTINESS NAMES THE GESTURE THAT IS HERE — amended 2026-08-24. It
          used to end « Matching a campaign to a line is done in Governance »,
          reading `evidence.owner`. Two things were wrong with it: no Governance
          screen matches a campaign to a plan line, and this tab does — the
          control below is `askForMatches`, and confirming one writes
          `POST …/placements/matches`. `evidence.owner` is deliberately NOT read
          any more: a payload word cannot make a door exist.

          IT CARRIES NO BUTTON OF ITS OWN, and that is the same rule: the control
          is one banner below, and a second `Suggest matches` stacked on the
          first is the screen asking twice for one thing. The sentence names it
          by the SERVER'S label so the two cannot drift apart. */}
      {evidence.empty_code === "no_line_names_this_connector" && (
        <Status as="block" tone="neutral" title={text(evidence.reason)}>
          {`Every line below belongs to this plan; none of them is matched to a ${label} campaign yet. Matching one is a gesture of this tab: use ${text(ambiguity?.action_label, "Suggest matches")} below, then confirm the campaign that is right for a line.`}
        </Status>
      )}

      {/* A LEVEL NOBODY RECORDED IS SAID ONCE — story 61.3. Every match written
          before migration 246 has none, and the badge on the row says so; this
          says WHY, and it is not `Matched by hand`: nothing measured that a
          person typed those rows. */}
      {unrecordedLevels && (
        <Status as="block" tone="neutral" title="Some matches do not say how they were made">
          {text(evidence.match_level_unrecorded_reason)}
        </Status>
      )}

      {/* THE FOURTH STATE IS ONE GESTURE AWAY, AND THE GESTURE IS HERE — story
          61.3. Not a badge drawn on load: `To arbitrate` needs a set of candidate
          campaigns, computing one sweeps every line against every campaign of
          this connector over the plan's window, and a badge with no candidates
          behind it is a decoration. So the sentence is the server's, the control
          is beside it, and the candidates arrive with the badge. */}
      {ambiguity?.available === false && proposals === null && (
        <Status
          as="block"
          tone="neutral"
          title="Ambiguities are computed when you ask for them"
          action={
            <Button size="sm" disabled={suggesting} onClick={() => void askForMatches()}>
              {text(ambiguity.action_label, "Suggest matches")}
            </Button>
          }
        >
          {text(ambiguity.reason)}
        </Status>
      )}

      {suggestFailure && (
        <Status
          as="block"
          tone="error"
          title="Placement evidence unavailable"
          action={<Retry onClick={() => void askForMatches()} />}
        >
          {suggestFailure}
        </Status>
      )}

      {proposals && (
        <Panel flush>
          <PanelHeader
            title="Proposed matches"
            description={[
              text(record(proposals.writes)?.reason, ""),
              proposalWindow
                ? `Read over ${text(proposalWindow.start)} → ${text(proposalWindow.end)}, the window of this plan's active version, keeping every pair at or above ${numberText(proposals.similarity_threshold)}.`
                : "",
            ]
              .filter(Boolean)
              .join(" ")}
            actions={
              <div className="flex flex-wrap items-center gap-3">
                {/* ONE DECISION FOR A WHOLE TIER — and it is offered only when
                    the tier has something in it. A control that would confirm
                    nothing is a control that reads as broken. */}
                {exactProposals.pairs.length > 0 && (
                  <Button
                    size="sm"
                    data-testid="placements-confirm-exact"
                    onClick={() => {
                      setBulkError(null);
                      setBulk({
                        pairs: exactProposals.pairs,
                        lines: exactProposals.lines,
                        levelLabel: "Exact code",
                      });
                    }}
                  >
                    {`Confirm ${exactProposals.pairs.length} Exact code match(es)`}
                  </Button>
                )}
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={suggesting}
                  onClick={() => void askForMatches()}
                >
                  {text(ambiguity?.action_label, "Suggest matches")}
                </Button>
              </div>
            }
          />
          {proposalLines.length === 0 ? (
            <div className="p-5">
              <EmptyState
                // A MEASUREMENT, NOT A BREAKDOWN. The threshold and the window are
                // in the description above, which is what makes it one.
                title={text(proposals.empty_message, "")}
                description={text(record(proposals.ambiguity)?.reason, "")}
              />
            </div>
          ) : (
            <div className="grid gap-4 p-5" role="group" aria-label="Proposed matches">
              {proposalLines.map((line) => {
                const candidates = records(line.candidates);
                return (
                  <div key={text(line.line_key)} className="grid gap-2">
                    <div className="flex flex-wrap items-center gap-3 text-ui">
                      <span className="font-medium">{text(line.label, text(line.line_key))}</span>
                      <Badge tone={stateTone(text(line.matching_state))}>
                        {text(line.matching_state_label)}
                      </Badge>
                    </div>
                    <ul className="m-0 grid gap-2 p-0">
                      {candidates.map((candidate) => (
                        <li
                          key={text(candidate.campaign_ref)}
                          className="flex flex-wrap items-center gap-3 text-ui"
                        >
                          <span className="font-mono">{text(candidate.campaign_ref)}</span>
                          {matchLevel(candidate) ? (
                            <Badge tone="neutral">{matchLevel(candidate)}</Badge>
                          ) : null}
                          {/* THE OTHER SIDE OF THE AMBIGUITY — a campaign several
                              plan lines claim. The engine pairs both ways, so
                              naming only the line side would hide half of the
                              arbitrations. */}
                          {candidate.campaign_state_label ? (
                            <Badge tone="warning">{text(candidate.campaign_state_label)}</Badge>
                          ) : null}
                          {candidate.already_matched === true ? (
                            <span className="text-text-secondary">Already matched</span>
                          ) : (
                            <Button
                              variant="secondary"
                              size="sm"
                              onClick={() => {
                                setConfirmError(null);
                                setConfirming({
                                  lineKey: text(line.line_key),
                                  lineLabel: text(line.label, text(line.line_key)),
                                  campaignRef: text(candidate.campaign_ref),
                                  levelLabel: matchLevel(candidate),
                                  score:
                                    typeof candidate.match_score === "number"
                                      ? candidate.match_score
                                      : null,
                                  matchesToday: numberText(line.matches_today),
                                  contested:
                                    nullableText(candidate.campaign_state_label),
                                });
                              }}
                            >
                              Confirm match
                            </Button>
                          )}
                        </li>
                      ))}
                    </ul>
                  </div>
                );
              })}
            </div>
          )}
        </Panel>
      )}

      <Panel flush>
        <PanelHeader
          title="Plan lines"
          description={[
            "Every line of this plan's active version. Unfold a line to read the campaigns of this connector attached to it, and the placements attached to each campaign.",
            text(counts?.nothing_awaiting_message, ""),
          ]
            .filter(Boolean)
            .join(" ")}
        />
        {/* THE NARROWING — amended 2026-08-18. The table still holds every line
            of the plan; these decide which of them are DRAWN, and the count
            below says how many of how many, so nobody reads a narrowing as a
            plan that shrank. */}
        <div className="grid gap-3 px-5 pb-4">
          <ChoiceGroup
            aria-label="Filter plan lines by matching state"
            choices={lineChoices}
            value={lineState}
            onValueChange={setLineState}
            variant="pill"
          />
          <div className="flex flex-wrap items-end gap-3">
            <label className="grid gap-1 text-caption text-text-secondary">
              <span>Search lines</span>
              <Input
                aria-label="Search plan lines by label"
                placeholder="Line label"
                value={lineSearch}
                data-testid="placements-line-search"
                onChange={(event) => setLineSearch(event.target.value)}
              />
            </label>
            {narrowedLines && (
              <Button
                variant="secondary"
                size="sm"
                data-testid="placements-clear-narrowing"
                onClick={() => {
                  setLineState(ALL_LINES);
                  setLineSearch("");
                }}
              >
                Clear narrowing
              </Button>
            )}
            <p className="m-0 text-caption text-text-secondary" data-testid="placements-line-count">
              {`${visibleLines.length} of ${lines.length} line(s) drawn.`}
            </p>
          </div>
          {/* The plan arrives whole, so an order here covers every line — but the
              sentence is mounted all the same: it is the one primitive rule for
              a sortable table, and a reader who has learnt it on the run history
              must not have to wonder whether it applies here. */}
          <SortScopeNote />
        </div>
        <TableScroll label="Plan lines and their matches">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Line</TableHead>
                <TableHead>Channel</TableHead>
                <SortableHead sortKey="budget" sort={sort} onSort={toggleSort} numeric>
                  Budget
                </SortableHead>
                <TableHead>Window</TableHead>
                <TableHead>{label}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {visibleLines.map((line) => {
                const lineKey = text(line.line_key);
                const campaigns = records(line.campaigns);
                const open = openLine === lineKey;
                return (
                  <Fragment key={lineKey}>
                    <TableRow>
                      <TableCell>
                        <button
                          type="button"
                          className="text-left text-primary underline"
                          aria-expanded={open}
                          onClick={() => setOpenLine(open ? null : lineKey)}
                        >
                          {text(line.label, lineKey)}
                        </button>
                      </TableCell>
                      <TableCell>{text(line.channel, "No channel")}</TableCell>
                      {/* THE BUDGET SAYS WHICH CURRENCY IT IS IN — story 61.4.
                          It arrives EXACT (`_decimal_text`: a NUMERIC as its
                          string, never a float), and until this story it was
                          drawn bare, two columns away from a spend figure in a
                          different currency. `selected_plan.currency` had been on
                          the payload since 61.1 and nothing read it. */}
                      <TableCell>{moneyText(line.budget, planCurrency, "No budget")}</TableCell>
                      <TableCell>
                        {`${text(line.start_date, "No start")} → ${text(line.end_date, "No end")}`}
                      </TableCell>
                      <TableCell>
                        {/* ONE WORD, THE SERVER'S — story 61.2. It used to be a
                            badge composed here from a boolean and a length, which
                            is how four states came to exist on screen with no
                            name anywhere. An unmatched line is NOT hidden and NOT
                            an error: it is the work, and it is why this table
                            lists every line rather than the matched ones. */}
                        <Badge tone={stateTone(text(line.matching_state))}>
                          {text(line.matching_state_label)}
                        </Badge>
                      </TableCell>
                    </TableRow>
                    {open && (
                      <TableRow>
                        <TableCell colSpan={5}>
                          {campaigns.length === 0 ? (
                            /* THE LINE SAYS WHAT TO DO NEXT, AND IT IS DONE ON
                               THIS TAB — amended 2026-08-24. It read « Matching
                               a campaign to a plan line is a Governance
                               decision », which sent the person who unfolded
                               this line away for the exact act two controls of
                               this page perform (`askForMatches`, then
                               `confirmMatch` on `POST …/placements/matches`).
                               The order is the real one: a placement hangs from
                               a campaign, so the match comes first. The control
                               is NOT repeated in this cell — it is at the top of
                               the same screen, and one gesture with two buttons
                               is one gesture asked for twice. */
                            <EmptyState
                              title={`No ${label} campaign is attached to this line`}
                              description={`Match one first, on this tab: ${text(ambiguity?.action_label, "Suggest matches")} at the top of the screen reads this connector's campaigns against every line of the plan, and confirming one attaches it here. Placements are attached afterwards, to a campaign that is already matched.`}
                            />
                          ) : (
                            <div
                              className="grid gap-4"
                              role="group"
                              aria-label={`Campaigns attached to ${text(line.label, lineKey)}`}
                            >
                              {campaigns.map((campaign) => {
                                const campaignRef = text(campaign.campaign_ref);
                                const placements = records(campaign.placements);
                                const key = chosenKey(lineKey, campaignRef);
                                return (
                                  <div key={campaignRef} className="grid gap-2">
                                    <div className="flex flex-wrap items-center gap-3 text-ui">
                                      {/* THE CAMPAIGN REFERENCE IS AN IMMUTABLE
                                          ID, AND IT IS RENDERED AS ONE — amended
                                          2026-08-18. It was a bare `font-mono`
                                          span: `DESIGN.md:96` reserves the mono
                                          face for exactly this, and `ObjectId`
                                          is where that decision lives, with the
                                          full value in `title` when the cell is
                                          narrow. There is no name to put first —
                                          `app.plan_line_mappings` carries the
                                          ref and nothing else, so inventing a
                                          display name here would be inventing
                                          data. The reference is the handle
                                          somebody takes to the provider's own
                                          console, so it is copyable. */}
                                      <ObjectId value={campaignRef} title="Campaign" />
                                      <CopyButton
                                        value={campaignRef}
                                        label="Copy campaign reference"
                                        size="xs"
                                      />
                                      <Badge tone="neutral">
                                        {`split ${text(campaign.split_weight, "unknown")}`}
                                      </Badge>
                                      {/* THE LABEL, NEVER THE RAW WORD — story
                                          61.2. `active`/`orphaned` is the
                                          lifecycle of `app.plan_line_mappings`;
                                          what a person needs is what it costs
                                          them, which is whether the line is
                                          still being given money. A status the
                                          server did not name renders as nothing
                                          rather than leaking a schema. */}
                                      {campaign.status_label ? (
                                        <Badge tone={campaign.status === "active" ? "success" : "warning"}>
                                          {text(campaign.status_label)}
                                        </Badge>
                                      ) : null}
                                      {/* HOW THIS MATCH WAS OBTAINED — story
                                          61.3. The label is the server's, the
                                          number comes with it for `Name
                                          similarity` alone, and a level the
                                          server did not name draws nothing at
                                          all rather than leaking a token. */}
                                      {matchLevel(campaign) ? (
                                        <Badge tone="neutral">{matchLevel(campaign)}</Badge>
                                      ) : null}
                                    </div>
                                    {placements.length === 0 ? (
                                      <p className="m-0 text-ui text-text-secondary">
                                        No placement is attached to this campaign on this line.
                                      </p>
                                    ) : (
                                      <ul className="m-0 grid gap-2 p-0">
                                        {placements.map((placement) => (
                                          <li
                                            key={text(placement.id)}
                                            className="flex flex-wrap items-center gap-3 text-ui"
                                          >
                                            {/* The observed value is the
                                                provider's own token for this
                                                placement — the string somebody
                                                pastes into the provider's
                                                console to see what it is. Same
                                                treatment as the campaign
                                                reference, and for the same
                                                reason. */}
                                            <ObjectId
                                              value={text(placement.breakdown_value)}
                                              title={text(placement.breakdown_dimension)}
                                            />
                                            <CopyButton
                                              value={text(placement.breakdown_value)}
                                              label="Copy placement"
                                              size="xs"
                                            />
                                            <span className="text-text-secondary">
                                              {text(placement.breakdown_dimension)}
                                            </span>
                                            <Button
                                              variant="secondary"
                                              size="sm"
                                              onClick={() =>
                                                setPending({
                                                  id: text(placement.id),
                                                  value: text(placement.breakdown_value),
                                                  lineLabel: text(line.label, lineKey),
                                                  campaignRef,
                                                  siblings: placements.length,
                                                })
                                              }
                                            >
                                              Detach
                                            </Button>
                                          </li>
                                        ))}
                                      </ul>
                                    )}
                                    {/* THE ATTACH CONTROL, and it exists only where
                                        a placement can exist: the candidates are
                                        the values OBSERVED on the connector's
                                        declared dimension over this plan's window.
                                        With no declared dimension there is nothing
                                        to choose and no control is drawn. */}
                                    {dimension.declared !== true ? null : attachable(placements).length > 0 ? (
                                      /* SEVERAL AT ONCE — amended 2026-08-18.
                                         « Une ligne de plan porte PLUSIEURS
                                         placements (Feed + Marketplace + Search
                                         results) : c'est le cas normal », says
                                         the ratified amendment, and the control
                                         was a one-value picker: the normal case
                                         cost three round trips. Checkboxes and
                                         not a multi-`select`, because a native
                                         multiple select requires a modifier key
                                         nobody is told about and shows its
                                         selection only while it has focus. */
                                      <fieldset className="m-0 grid gap-2 border-0 p-0">
                                        <legend className="text-caption text-text-secondary">
                                          {`Observed ${text(dimension.dimension, "placement")} values to attach to this campaign`}
                                        </legend>
                                        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
                                          {attachable(placements).map((value) => (
                                            <label
                                              key={value}
                                              className="flex items-center gap-2 text-ui"
                                            >
                                              <Checkbox
                                                checked={(chosen[key] ?? []).includes(value)}
                                                onCheckedChange={() => toggleChosen(key, value)}
                                                aria-label={`Attach ${value} to ${campaignRef}`}
                                              />
                                              <span className="font-mono text-caption">{value}</span>
                                            </label>
                                          ))}
                                        </div>
                                        <div>
                                          <Button
                                            size="sm"
                                            disabled={
                                              (chosen[key] ?? []).length === 0 || attaching === key
                                            }
                                            onClick={() => void attach(lineKey, campaignRef)}
                                          >
                                            {/* THE COUNT IS ON THE CONTROL, not
                                                only in a confirmation after it:
                                                attaching is not destructive and
                                                asks for no dialog, so the button
                                                itself has to say the scope. */}
                                            {`Attach ${(chosen[key] ?? []).length || ""} placement(s)`.replace(
                                              "  ",
                                              " ",
                                            )}
                                          </Button>
                                        </div>
                                      </fieldset>
                                    ) : (
                                      // The dimension IS declared and there is
                                      // nothing left to attach — either nothing
                                      // was observed on the plan's window, or
                                      // every observed value is already attached
                                      // here. The undeclared case is not repeated:
                                      // the banner above already carries the
                                      // server's sentence for it, once.
                                      <p className="m-0 text-ui text-text-secondary">
                                        {text(
                                          observed.reason,
                                          observedValues.length === 0
                                            ? `No ${text(dimension.dimension)} value was observed on this connector inside the plan's window, so there is nothing to attach.`
                                            : "Every placement observed on this window is already attached to this campaign.",
                                        )}
                                      </p>
                                    )}
                                  </div>
                                );
                              })}
                            </div>
                          )}
                        </TableCell>
                      </TableRow>
                    )}
                  </Fragment>
                );
              })}
            </TableBody>
          </Table>
        </TableScroll>
      </Panel>

      {/* THE FOURTH STATE IN ITS OWN PANEL — spend this connector reported that
          no match ventilates. It is the one that reveals unplanned spend, and it
          is a decision to take rather than an error to clear. */}
      <Panel flush>
        <PanelHeader
          title="Spend with no plan line"
          description={text(unmapped?.reason, "")}
        />
        {records(unmapped?.rows).length === 0 ? (
          <div className="p-5">
            <EmptyState
              // AN EMPTY STATE IS A MEASUREMENT, and the panel keeps the window it
              // read. The sentence is the server's, and it shares no word with
              // "The plan-versus-actual reading could not be completed".
              title={text(unmapped?.empty_message, "")}
              description={
                unmapped?.window
                  ? `Read over ${text(record(unmapped.window)?.start)} → ${text(record(unmapped.window)?.end)}, the window of this plan's active version.`
                  : "This plan's active version has no dated line, so no window was read."
              }
            />
          </div>
        ) : (
          <TableScroll label="Unmatched spend">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Campaign</TableHead>
                  <TableHead>Spend</TableHead>
                  <TableHead>Why it is here</TableHead>
                  <TableHead>Decision</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {records(unmapped?.rows).map((row) => {
                  const decision = record(row.decision);
                  return (
                    <TableRow key={text(row.campaign_ref)}>
                      <TableCell>
                        {/* An immutable reference, rendered as one and takeable
                            — the same treatment the matched campaigns get above.
                            This is the panel where somebody is most likely to
                            need it elsewhere: the campaign is spending money no
                            plan line covers, and the next step is looking it up
                            in the provider's console. */}
                        <span className="flex flex-wrap items-center gap-2">
                          <ObjectId value={text(row.campaign_ref)} title="Campaign" />
                          <CopyButton
                            value={text(row.campaign_ref)}
                            label="Copy campaign reference"
                            size="xs"
                          />
                        </span>
                      </TableCell>
                      {/* AND SO DOES THE SPEND, under the currency that PRODUCED
                          it — the one the warehouse converted `fact_daily_kpi`
                          into, never the plan's. When the server could not name
                          one, the cell says so and prints no figure: a number
                          whose currency is unknown is a different statement from
                          a number, not a rounder one. */}
                      <TableCell>{moneyText(row.spend, spendCurrency)}</TableCell>
                      {/* THE SENTENCE COMES DOWN THE WIRE. This cell used to
                          compare the payload value literally against
                          `sans_mapping`, which is how a French token stayed on
                          the wire for a year with nothing able to see it. */}
                      <TableCell>{text(row.reason_label)}</TableCell>
                      <TableCell>
                        <div className="flex flex-wrap items-center gap-3">
                          <Badge tone={stateTone(text(row.matching_state))}>
                            {text(row.matching_state_label)}
                          </Badge>
                          {decision ? (
                            <span className="text-text-secondary">
                              {`${text(decision.decided_by)} — ${text(decision.reason)}`}
                            </span>
                          ) : (
                            // THE SECOND HALF OF THE `Permet` LINE, and it reaches
                            // a route: « la rattacher OU l'accepter comme telle ».
                            <Button
                              variant="secondary"
                              size="sm"
                              onClick={() => {
                                setAcceptReason("");
                                setAcceptError(null);
                                setAccepting({
                                  campaignRef: text(row.campaign_ref),
                                  spend: moneyText(row.spend, spendCurrency),
                                  awaiting: numberText(unmappedCounts?.awaiting_decision),
                                });
                              }}
                            >
                              Accept as unplanned
                            </Button>
                          )}
                        </div>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </TableScroll>
        )}
      </Panel>

      {/* THE ACCEPTANCE, CONFIRMED WITH WHAT IT DOES NOT CHANGE. It moves no
          money — `app.plan_unmatched_spend_decisions` carries no amount, no mart
          reads it, and the ventilation stays on `plan_line_mappings.status =
          'active'` — and the campaign STAYS in this panel with its spend. It also
          says what nothing here can undo, because an irreversible gesture that
          reads as reversible is one people take by accident. */}
      <ConfirmDialog
        open={accepting !== null}
        onOpenChange={(open) => {
          if (!open) {
            setAccepting(null);
            setAcceptError(null);
          }
        }}
        title="Accept this spend as unplanned?"
        description={
          accepting ? (
            <span className="grid gap-3">
              <span>
                {/* THE LIMIT IS MEASURED, NOT GUESSED — amended 2026-08-18.
                    `core/plan_spend_decisions.py` exposes two functions,
                    `list_decisions` and `accept_unmatched_spend`, and the insert
                    is `ON CONFLICT DO NOTHING` so that the FIRST decision and
                    its author stand. There is no withdrawal in the store and no
                    route to one anywhere in the product, so this sentence names
                    NO gesture: sending somebody to undo it elsewhere would be
                    sending them nowhere, which is the worse defect. */}
                {`${accepting.campaignRef} carries ${accepting.spend} of spend no line of this plan matches, and ${accepting.awaiting} campaign(s) of this connector are waiting for a decision. Accepting changes no amount: the campaign stays listed here with its spend, and the plan-versus-actual reading is unaffected. It is recorded once, under your name and today's date, and nothing in the product withdraws it — a later decision on the same campaign returns this one rather than replacing it.`}
              </span>
              <Textarea
                aria-label="Why this spend is accepted as unplanned"
                placeholder="Why is this spend accepted as unplanned?"
                value={acceptReason}
                onChange={(event) => setAcceptReason(event.target.value)}
              />
            </span>
          ) : (
            ""
          )
        }
        confirmLabel="Accept as unplanned"
        busy={acceptBusy}
        error={acceptError}
        onConfirm={() => void confirmAcceptance()}
      />

      {/* THE CONFIRMATION THAT MAKES A PROPOSAL A DECISION — story 61.3.
          A level never applies itself, not even `Exact code`: `no silent value
          fusion` (AD-9) governs the safest one as much as the fuzziest. And the
          scope is named with the count taken BEFORE the write, because confirming
          rewrites every match that line already carries. */}
      <ConfirmDialog
        open={confirming !== null}
        onOpenChange={(open) => {
          if (!open) {
            setConfirming(null);
            setConfirmError(null);
          }
        }}
        title="Confirm this match?"
        description={
          confirming
            ? [
                `${confirming.lineLabel} carries ${confirming.matchesToday} match(es) today; confirming rewrites them and attaches ${confirming.campaignRef}.`,
                confirming.levelLabel
                  ? `It is recorded as obtained by: ${confirming.levelLabel}.`
                  : "",
                confirming.contested
                  ? `${confirming.campaignRef} is ${confirming.contested.toLowerCase()}, so its spend will be shared between them.`
                  : "",
              ]
                .filter(Boolean)
                .join(" ")
            : ""
        }
        confirmLabel="Confirm match"
        busy={confirmBusy}
        error={confirmError}
        onConfirm={() => void confirmMatch()}
      />

      {/* ONE CONFIRMATION FOR A WHOLE TIER — amended 2026-08-18.
          It names THREE numbers, and each answers a different question: how many
          matches will be written, how many plan lines they land on, and that
          confirming REPLACES the matches those lines already carry (which is
          what `set_line_mappings` does, across every connector). A dialog that
          named only the first would describe a third of the scope. */}
      <ConfirmDialog
        open={bulk !== null}
        onOpenChange={(open) => {
          if (!open) {
            setBulk(null);
            setBulkError(null);
          }
        }}
        title={bulk ? `Confirm ${bulk.pairs.length} ${bulk.levelLabel} match(es)?` : ""}
        description={
          bulk
            ? `${bulk.pairs.length} proposal(s) obtained by ${bulk.levelLabel} land on ${bulk.lines} plan line(s). Confirming rewrites the matches those lines carry today, across every connector, and records each one at the level the engine computes when it is written — never the level shown here. Nothing else on this plan changes.`
            : ""
        }
        confirmLabel="Confirm these matches"
        busy={bulkBusy}
        error={bulkError}
        onConfirm={() => void confirmBulk()}
      />

      {/* THE COUNT BEFORE, NOT AFTER. A confirmation that names what is left once
          the deletion has happened describes a scope nobody was asked about. */}
      <ConfirmDialog
        open={pending !== null}
        onOpenChange={(open) => !open && setPending(null)}
        title="Detach this placement?"
        description={
          pending
            ? `${pending.lineLabel} carries ${pending.siblings} placement(s) on campaign ${pending.campaignRef}. Detaching removes ${pending.value} and leaves the campaign, its split and its spend untouched.`
            : ""
        }
        confirmLabel="Detach"
        destructive
        onConfirm={() => void confirmDetach()}
      />
    </div>
  );
}
