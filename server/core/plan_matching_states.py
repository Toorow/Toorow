"""The ONE word the media-plan match uses for its states -- story 61.2.

WHY A MODULE AND NOT A FIFTH SET OF STRINGS. Four status vocabularies already
existed when this file was written, and none of them was the one epic 61 asked
for:

  * `active | orphaned` -- `app.plan_line_mappings.status` (migration 041) and,
    by deliberate reuse, `app.plan_line_placement_mappings.status`
    (migration 244). It is a LIFECYCLE: does the `line_key` still exist in the
    plan's active version;
  * `complete | partial | unavailable | excluded | pending | not_applicable` --
    `capability_proposals.COVERAGE_STATES`, the coverage of a CAPABILITY on a
    Datastream;
  * `matched | ambiguous | unmatched` -- `file_source_recognizer.py:134`, the
    match of a SOURCE COLUMN to a canonical field, consumed by
    `column_treatments.py`.

The third one already carries three of the four words epic 61 names, they are
written and they are read, so story 61.2 REUSES them (arbitrage A1) instead of
inventing `mapped` / `unmapped_plan_line` / `unmapped_source_placement`. The
price of reuse is an homonym -- one word for a source column and for a plan line
-- and that price is paid in `docs/product-architecture/glossary.md`, which is
where this repository has already arbitrated `Excluded` (four senses) and
`Cleanup rule` (two senses taken). Without that entry the collision is reopened,
not closed.

THE FOURTH WORD IS `accepted`, AND IT COMES FROM A ROW. Spend a plan never
covered, once a person has accepted it as unplanned
(`app.plan_unmatched_spend_decisions`, migration 245), is no longer `unmatched`:
nobody is waiting on it. It is the only one of the four that is stored rather
than derived, because an acceptance is a dated human act.

`ambiguous` IS EMITTED SINCE STORY 61.3, AND ONLY WITH ITS CANDIDATES. Story 61.2
reserved the word and said in the payload why nothing produced it: the one engine
that can compute a candidate set, `core/plan_mapping_suggest.py`, had no importer
outside its own test. Story 61.3 gave it a route, so the word now has a source --
but it is still never drawn from a tab load. Calling the engine on every open
would pay a line x campaign sweep for a state nobody asked about, so the
candidates are computed ON DEMAND and the badge exists only on the payload that
carries them (`AMBIGUITY_IS_COMPUTED_ON_DEMAND` says so where the set is absent).
An ambiguity whose candidates are not named beside it is a decoration, and that
has not changed.

AND IT HAS TWO SIDES -- story 61.3, amendment to the ratified document. A plan
line claimed by several candidate campaigns is the side
`docs/product-architecture/capabilities/placement-mapping.md` describes. The
engine is N:M in BOTH directions (`plan_mapping_suggest.py:151-153`), so a
CAMPAIGN claimed by several plan lines is already computed, and naming only the
first side would have left half of the arbitrations invisible while the machine
had them in hand. The two are different acts -- one picks a campaign for a line,
the other decides which line pays for a campaign -- so they carry different
labels, and neither is `split_weight`: a campaign DELIBERATELY shared between two
lines is a ventilation, not an ambiguity, which is why the campaign side is read
from the CANDIDATES and never from the stored matches.

HOW A MATCH WAS OBTAINED IS A SECOND AXIS, AND ITS WORDS ARE BORROWED TOO.
`exact | normalized | similarity | manual` is the CHECK of migration 052 and the
four constants of `dimension_conformance.py:83-86`; they are IMPORTED below, not
respelled, which is the fourth reuse of an existing vocabulary in this batch.
What is new here is only what each is CALLED to a person, and the refusal of two
words the plan uses: `epic-61:41` says "regle regex" where the repository has a
FIXED normalisation pipeline nobody configures, and "proposition IA floue" where
the repository has `difflib` at 0.88. `AI` is not a label of this module.
"""

from __future__ import annotations

from typing import Any

from core.dimension_conformance import (  # the 27.4 vocabulary, imported not copied
    DEFAULT_SIMILARITY_THRESHOLD,
    METHOD_EXACT,
    METHOD_MANUAL,
    METHOD_NORMALIZED,
    METHOD_SIMILARITY,
)

#: A plan line this connector is matched to, or spend a plan line ventilates.
MATCHING_STATE_MATCHED = "matched"

#: Several candidates and nobody has arbitrated. On a plan line: several campaigns
#: of this connector resemble it. On a campaign: several plan lines claim it.
#: Emitted since story 61.3, and ONLY on a payload that names the candidates.
MATCHING_STATE_AMBIGUOUS = "ambiguous"

#: Nothing on the other side. On a plan line: no campaign of this connector is
#: ventilating it. On spend: no plan line of this plan matches it.
MATCHING_STATE_UNMATCHED = "unmatched"

#: Out-of-plan spend a named person accepted as unplanned, on a dated row.
MATCHING_STATE_ACCEPTED = "accepted"

#: The closed vocabulary. Closed rather than open because the fault this story
#: exists to close is a FIFTH set of words, and a tuple somebody has to edit is
#: the moment that decision becomes visible.
PLAN_MATCHING_STATES: tuple[str, ...] = (
    MATCHING_STATE_MATCHED,
    MATCHING_STATE_AMBIGUOUS,
    MATCHING_STATE_UNMATCHED,
    MATCHING_STATE_ACCEPTED,
)

#: What a plan line's state is CALLED to a person. `ambiguous` joined the three
#: others in story 61.3, when a payload started carrying the candidates that
#: justify it -- "To arbitrate" and not "Ambiguous", because the word a person
#: needs is the act they owe, not the property of the data.
PLAN_LINE_STATE_LABELS: dict[str, str] = {
    MATCHING_STATE_MATCHED: "Matched",
    MATCHING_STATE_AMBIGUOUS: "To arbitrate",
    MATCHING_STATE_UNMATCHED: "Nothing observed",
}

#: What a CANDIDATE CAMPAIGN's state is called -- the second side of the
#: ambiguity, and the amendment story 61.3 writes. Only one word is named: a
#: campaign that one line alone claims is not in a state, it is simply not
#: contested, and `campaign_state_label` answers `None` for it rather than
#: printing a reassurance nobody needs.
CAMPAIGN_STATE_LABELS: dict[str, str] = {
    MATCHING_STATE_AMBIGUOUS: "Claimed by several plan lines",
}

#: What an out-of-plan spend row's state is called. Two words for two situations
#: a person acts on differently: one is waiting, the other has been answered.
SPEND_STATE_LABELS: dict[str, str] = {
    MATCHING_STATE_UNMATCHED: "Outside the plan",
    MATCHING_STATE_ACCEPTED: "Accepted as unplanned",
}

#: `app.plan_line_mappings.status` said to a person. The raw words never reach a
#: screen -- `orphaned` is a database lifecycle, and the person reading needs to
#: know what it costs them, which is that the line stopped ventilating.
MAPPING_STATUS_LABELS: dict[str, str] = {
    "active": "Ventilating spend",
    "orphaned": "No longer in the plan's active version",
}

#: Why a payload that names no candidate draws no ambiguity, carried ON that
#: payload so a tab can be read without opening this file. Story 61.2 said "no
#: engine calls it"; that measurement expired the day story 61.3 gave the engine a
#: route, and this sentence is what replaced it -- the cost, not the absence.
AMBIGUITY_IS_COMPUTED_ON_DEMAND = (
    "No plan line is reported as an ambiguity here: the candidate campaigns that would "
    "justify one are computed on request (core/plan_mapping_suggest.py sweeps every line "
    "against every campaign of this connector over the plan's window), and paying that "
    "sweep on every open would charge it to people who never asked. Ask for the matches to "
    "see the candidates and the arbitrations. Several placements on one line is the normal "
    "case and is never an ambiguity."
)

#: The gesture that computes them, named ONCE so the button, the route's payload
#: and the empty sentence cannot drift into three spellings.
SUGGEST_MATCHES_LABEL = "Suggest matches"


# ---------------------------------------------------------------------------
# The second axis -- HOW a match was obtained. Story 61.3.
# ---------------------------------------------------------------------------

#: The closed vocabulary of the level, in the order of the cascade, safest first.
#: The values come from `dimension_conformance`; only the ORDER is stated here,
#: and it is the order `plan_mapping_suggest` applies stage by stage.
MATCH_METHODS: tuple[str, ...] = (
    METHOD_EXACT,
    METHOD_NORMALIZED,
    METHOD_SIMILARITY,
    METHOD_MANUAL,
)

#: What each level is CALLED to a person -- one word, never the stored token, on
#: the precedent of `WorkbenchMappingPage.tsx` rendering a binding's confidence.
#:
#: `Name similarity` AND NOT `AI proposal`. The plan calls this tier a fuzzy AI
#: proposal; it is `difflib.SequenceMatcher` over two normalised strings at the
#: 0.88 threshold below. Calling a string comparison an intelligence is a promise
#: the code does not keep, and it is the most expensive kind of sentence this
#: repository has written.
#:
#: `Normalized name` AND NOT `Rule`. The plan's second tier is a regex rule a
#: client writes; what exists is `dimension_conformance.normalize_value`, a FIXED
#: pipeline (bracket tags, datestamps, diacritics, case, separators, affixes)
#: nobody configures. The ordered matching rules two ratified documents place in
#: Governance are carried by no table, and this label does not pretend otherwise.
MATCH_METHOD_LABELS: dict[str, str] = {
    METHOD_EXACT: "Exact code",
    METHOD_NORMALIZED: "Normalized name",
    METHOD_SIMILARITY: "Name similarity",
    METHOD_MANUAL: "Matched by hand",
}

#: What a match written before migration 246 is called. A NAMED ABSENCE and never
#: `Matched by hand`: nothing measured that a person typed those 49 rows, and
#: filling an unknown with the one level that implies a human act is the fault
#: this label exists to refuse.
MATCH_METHOD_UNRECORDED_LABEL = "Level not recorded"

#: And why, said beside it. The absence is dated: it belongs to every match older
#: than the columns.
MATCH_METHOD_UNRECORDED_REASON = (
    "This match was made before the level was recorded, so how it was obtained is not "
    "known. It is not a manual match: nothing states who or what proposed it."
)

#: The threshold the `similarity` stage applies, re-exported so a payload can say
#: over what a score was judged without importing the engine.
SIMILARITY_THRESHOLD = DEFAULT_SIMILARITY_THRESHOLD


def match_method_label(method: Any) -> str | None:
    """The product word for a level, the named absence for none, `None` for a stranger.

    Three answers because there are three situations, and collapsing any two of
    them loses the one thing this axis exists to say. An absent level is a match
    older than the columns and says so; a level this module has never heard of
    renders as nothing rather than leaking a database token onto a screen.
    """
    token = str(method or "").strip()
    if not token:
        return MATCH_METHOD_UNRECORDED_LABEL
    return MATCH_METHOD_LABELS.get(token)


def display_match_score(method: Any, score: Any) -> float | None:
    """The score a screen may print: the `similarity` ratio, and nothing else.

    `exact` and `normalized` score 1.0 BY CONSTRUCTION -- the first on an equality
    of raw strings, the second on an equality of normalised ones
    (`plan_mapping_suggest.py:180`, `:126`) -- so printing that 1.0 would dress a
    tautology as a measurement, and a reader comparing it with a real 0.89 would
    be comparing two different kinds of thing. `manual` has no score at all, which
    migration 246 enforces.
    """
    if str(method or "").strip() != METHOD_SIMILARITY:
        return None
    if score is None:
        return None
    return float(score)


def line_matching_state(
    campaigns: list[dict[str, Any]], *, candidate_count: int | None = None
) -> str:
    """What a plan line is, in one word, from what ventilates it and what claims it.

    THE STATUS THAT DECIDES, AND ONLY IT. `dbt/models/marts/plan_vs_actual_daily.sql`
    ventilates `FROM mirror.plan_line_mappings m ... WHERE m.status = 'active'`, so a
    line whose every match is `orphaned` receives no money -- reading it as
    "matched" because a row exists would tell a person a budget is covered when
    the mart gives it nothing.

    `candidate_count` is the second axis, and it is OPTIONAL because most readers
    of this function have no candidate set at all: `None` means "nobody computed
    one", which is not the same as "there are none" and must never be read as it.
    With a set in hand, a line nothing ventilates and TWO OR MORE campaigns
    resemble is `ambiguous` -- there is an arbitration owed. One candidate is not
    an ambiguity, it is a proposal; and an already matched line is not one either,
    because somebody has arbitrated.
    """
    for campaign in campaigns:
        if str(campaign.get("status") or "") == "active":
            return MATCHING_STATE_MATCHED
    if candidate_count is not None and candidate_count >= 2:
        return MATCHING_STATE_AMBIGUOUS
    return MATCHING_STATE_UNMATCHED


def campaign_matching_state(claiming_line_count: int) -> str | None:
    """`ambiguous` when several plan lines claim one campaign, else `None`.

    THE SIDE THE RATIFIED DOCUMENT DOES NOT NAME, and story 61.3 amends it. The
    engine pairs plan x actual in both directions, so this set is already computed
    every time the other one is; leaving it silent would have hidden half of the
    arbitrations behind a reading that looked complete.

    It is NOT `split_weight`. A campaign two lines DELIBERATELY share is a
    ventilation, governed by `SUM(split_weight) = 1.0` and settled. This word is
    about CANDIDATES nobody has arbitrated yet, which is why it is never derived
    from `app.plan_line_mappings`.
    """
    return MATCHING_STATE_AMBIGUOUS if claiming_line_count >= 2 else None


def mapping_status_label(status: Any) -> str | None:
    """A product sentence for `active`/`orphaned`, or `None` for a word nobody named.

    `None` and never the raw value: printing an unknown database word is exactly
    what this function exists to stop, and a screen that receives `None` renders
    an absence instead of leaking a schema.
    """
    return MAPPING_STATUS_LABELS.get(str(status or ""))


def campaign_state_label(state: Any) -> str | None:
    """The product word for a candidate campaign's state, or `None`."""
    return CAMPAIGN_STATE_LABELS.get(str(state or ""))


__all__ = [
    "AMBIGUITY_IS_COMPUTED_ON_DEMAND",
    "CAMPAIGN_STATE_LABELS",
    "MAPPING_STATUS_LABELS",
    "MATCHING_STATE_ACCEPTED",
    "MATCHING_STATE_AMBIGUOUS",
    "MATCHING_STATE_MATCHED",
    "MATCHING_STATE_UNMATCHED",
    "MATCH_METHODS",
    "MATCH_METHOD_LABELS",
    "MATCH_METHOD_UNRECORDED_LABEL",
    "MATCH_METHOD_UNRECORDED_REASON",
    "METHOD_EXACT",
    "METHOD_MANUAL",
    "METHOD_NORMALIZED",
    "METHOD_SIMILARITY",
    "PLAN_LINE_STATE_LABELS",
    "PLAN_MATCHING_STATES",
    "SIMILARITY_THRESHOLD",
    "SPEND_STATE_LABELS",
    "SUGGEST_MATCHES_LABEL",
    "campaign_matching_state",
    "campaign_state_label",
    "display_match_score",
    "line_matching_state",
    "mapping_status_label",
    "match_method_label",
]
