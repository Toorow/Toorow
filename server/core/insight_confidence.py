"""toorow -- the confidence of a published daily insight, DERIVED not declared.

`proactive-assertions.md` ("Incomplete if"): *a confidence level is declared by
the author of the claim rather than derived*. Until this module, the confidence
a reader saw beside a published insight was `insight.confidence` -- one of
`low` / `medium` / `high`, chosen by the model that also wrote the prose, and
checked for nothing but enum membership. Story 53.4 made the declaration
VISIBLE (`authorship.confidence = "model_declared"`, drawn by the Overview as
"declared by the model, not derived from server evidence"), which was the honest
half of the repair and left the other half open: a claim certified by its own
author measures nothing, however loudly it says so.

WHAT IS MEASURED, AND OVER WHAT. The insight already names its evidence: a
published payload carries `evidenceRefs`, each `metric:<name>` or
`dimension:<name>`, and the publication gate has already refused any ref the
server did not measure for this project over this window
(`daily_insights_schema.evidence_universe`). So the evidence of the CLAIM is the
rows that carry the members the claim CITED, restricted to the insight's own
`period` -- not the project's rows at large, and not the run's seven-day
lookback. The three terms are then the three `core.confidence` already defines,
under the same names and the same functions:

  completeness  the share of the cited members the server actually measured over
                the insight's own period. A claim that cites four members and is
                backed by three is three-quarters supported, and says so.
  freshness     `core.confidence.freshness_term` over the SUPPORTING rows only --
                the stalest contributing source decides, as everywhere else.
  provenance    `core.confidence.provenance_term` over the same rows: the share
                carrying a `pull_id` (AD-9 traceability).

NO PRODUCT, AND NO FOURTH NUMBER. The sibling rule one line below in the same
document -- *a single score merges evidence of different natures* -- binds this
module as much as it binds `core.confidence`, and it is why `reading` is the
band of the LIMITING term and never a blend. Completeness cannot buy back
staleness here any more than it can there.

UNMEASURABLE IS A READING, AND IT IS NOT A LEVEL. An insight whose refs resolve
against nothing, whose period cannot be read, or one of whose terms is unknown
gets `unmeasurable` with the reason named. It never gets a default level: a
`medium` nobody measured is exactly the object this module was written to
remove, and `overview.md:38` already forbids its shape -- missing evidence is
`Unknown`, never Healthy.

THE MODEL'S OWN WORD IS NOT THROWN AWAY, AND IT DRIVES NOTHING. It travels as
`authorship.declaredConfidence`, labelled as what it is; the reading a surface
prints comes from here. See `daily_insights_schema.authorship_block`.
"""

from __future__ import annotations

from core.confidence import (
    TERM_READING_HIGH,
    TERM_READING_MEDIUM,
    freshness_term,
    provenance_term,
)

#: The reading of an insight whose evidence could not be measured. Deliberately
#: NOT a member of the level scale: a surface that sorts or compares readings
#: must not be able to place it between `low` and `medium`.
READING_UNMEASURABLE = "unmeasurable"

#: The bands, weakest first. One scale, and the same words the model used to
#: declare -- so a surface that printed the declared word prints the derived one
#: without a second vocabulary to teach the reader.
READING_LEVELS: tuple[str, ...] = ("low", "medium", "high")

#: The kinds of member an evidence ref may name, and where each one is found on
#: a warehouse row. Long-format rows: one row per (date, metric, breakdown).
#: `cards._available_inputs` reads exactly these two columns to build the
#: availability the publication gate resolves refs against, so a member resolves
#: here if and only if it resolved there.
_MEMBER_COLUMN = {"metric": "metric", "dimension": "breakdown_dimension"}

_TERMS_SCOPE = (
    "the rows carrying the members this insight CITED, restricted to the "
    "insight's own period AS FAR AS THE ROWS THE PUBLICATION READ REACH -- the "
    "measuredWindow beside the terms states the span actually measured, and a "
    "period wider than the publication's lookback is measured over their "
    "intersection, never silently over the whole period; completeness is the "
    "share of the cited members the server measured, freshness and provenance "
    "are read over those rows alone"
)


def term_reading(value: float | None) -> str:
    """The band one term is read at, or `unmeasurable` when it is unknown.

    Boundaries live in `core.confidence` so that this module and any later
    reader cannot each invent their own. They are INCLUSIVE at the bottom:
    exactly 0.9 reads `high`, exactly 0.6 reads `medium`.
    """

    if value is None:
        return READING_UNMEASURABLE
    if value >= TERM_READING_HIGH:
        return "high"
    if value >= TERM_READING_MEDIUM:
        return "medium"
    return "low"


def _split_ref(ref: str) -> tuple[str, str] | None:
    """`kind:name` as a pair, or None when the ref has no shape this knows.

    A malformed ref is not silently dropped -- it is returned by
    `derive_insight_confidence` under `unresolvedRefs`, so a claim that cites
    something nobody can read is unmeasurable rather than quietly measured over
    the refs that happened to parse.
    """

    kind, sep, name = str(ref).partition(":")
    if not sep or kind not in _MEMBER_COLUMN or not name:
        return None
    return kind, name


def _row_dates(rows: list[dict]) -> list[str]:
    """The readable dates of *rows*, `YYYY-MM-DD`, unsorted and possibly empty."""

    return [str(r.get("date"))[:10] for r in rows or [] if r.get("date")]


def _window_rows(rows: list[dict], date_from: str, date_to: str) -> list[dict]:
    """Rows whose date falls inside the insight's own period.

    A row with no readable date is excluded rather than assumed inside: the
    whole point of this function is that the terms describe the period the
    claim names.
    """

    kept = []
    for row in rows or []:
        raw = row.get("date")
        if not raw:
            continue
        day = str(raw)[:10]
        if date_from <= day <= date_to:
            kept.append(row)
    return kept


def _rows_supporting(rows: list[dict], kind: str, name: str) -> list[dict]:
    """The rows that carry one cited member."""

    column = _MEMBER_COLUMN[kind]
    return [row for row in rows if row.get(column) == name]


def unmeasurable(reason: str, **extra) -> dict:
    """The honest block for an insight whose evidence could not be measured.

    Every key the measured block carries is present and empty or `None`, so a
    surface reads ONE shape and never has to branch on the absence of a key to
    discover it is looking at an unmeasurable claim.
    """

    block = {
        "reading": READING_UNMEASURABLE,
        "reason": reason,
        "terms": {"completeness": None, "freshness": None, "provenance": None},
        "limitingTerm": None,
        "unknownTerms": ["completeness", "freshness", "provenance"],
        "citedMembers": [],
        "unresolvedRefs": [],
        "measuredWindow": None,
        "termsScope": _TERMS_SCOPE,
    }
    block.update(extra)
    return block


def derive_insight_confidence(
    payload: dict,
    *,
    rows: list[dict] | None = None,
    stale_since=None,
) -> dict:
    """The server's own reading of one published insight's confidence.

    Pure over (`payload`, `rows`): no database, no clock. `rows` are the
    project's warehouse rows the publication path already resolved
    (`daily_insight_mcp._resolve_daily_insight_inputs`) -- passing them rather
    than re-querying is what keeps this reading and the availability the gate
    resolved refs against measured on the SAME data.

    Never raises and never returns None: an insight always carries a reading,
    and `unmeasurable` is one. A block that could be absent would be read as a
    surface that forgot to draw it, which is how a missing measurement becomes
    an assumed one.
    """

    period = payload.get("period") if isinstance(payload.get("period"), dict) else {}
    date_from = str(period.get("dateFrom") or "")[:10]
    date_to = str(period.get("dateTo") or "")[:10]
    if not date_from or not date_to or date_from > date_to:
        return unmeasurable(
            "This insight names no readable period, so there is no window its "
            "cited evidence could be measured over."
        )

    refs = [str(ref) for ref in (payload.get("evidenceRefs") or [])]
    if not refs:
        return unmeasurable(
            "This insight cites no evidence, so nothing the server measured "
            "supports it."
        )

    window = _window_rows(rows or [], date_from, date_to)

    cited: list[str] = []
    unresolved: list[str] = []
    supporting: list[dict] = []
    for ref in refs:
        parsed = _split_ref(ref)
        if parsed is None:
            unresolved.append(ref)
            continue
        member_rows = _rows_supporting(window, *parsed)
        if member_rows:
            cited.append(ref)
            supporting.extend(member_rows)
        else:
            unresolved.append(ref)

    if not cited:
        return unmeasurable(
            "None of this insight's cited members was measured by the server "
            "over the period the insight names.",
            unresolvedRefs=sorted(unresolved),
        )

    terms = {
        "completeness": round(len(cited) / len(refs), 4),
        "freshness": freshness_term(supporting, date_to, stale_since=stale_since),
        "provenance": provenance_term(supporting),
    }
    unknown = sorted(name for name, value in terms.items() if value is None)
    if unknown:
        # One unknown term makes the reading unknown. It does NOT make it `low`:
        # a claim nobody could measure and a claim measured badly are different
        # objects, and only the second one is a judgement on the claim.
        return unmeasurable(
            "One of the terms this reading is built from could not be measured: "
            + ", ".join(unknown)
            + ".",
            terms=terms,
            unknownTerms=unknown,
            citedMembers=sorted(cited),
            unresolvedRefs=sorted(unresolved),
        )

    limiting = min(terms, key=lambda name: terms[name])
    return {
        "reading": term_reading(terms[limiting]),
        "reason": None,
        "terms": terms,
        # THE WEAKEST TERM DECIDES, and it is named. Without the name a reader
        # cannot tell a claim that is stale from one that cites members nobody
        # measured, and the two call for opposite gestures.
        "limitingTerm": limiting,
        "unknownTerms": [],
        "citedMembers": sorted(cited),
        "unresolvedRefs": sorted(unresolved),
        # The span the terms were ACTUALLY measured over. The period is the
        # claim's own window; the rows are the publication's lookback; when the
        # period is wider than the rows reach, the terms describe the
        # intersection and this says so instead of letting `termsScope` claim
        # the whole period (review of ae60c22a, residue 3).
        "measuredWindow": (
            {
                "dateFrom": min(_row_dates(supporting)),
                "dateTo": max(_row_dates(supporting)),
            }
            if _row_dates(supporting)
            else None
        ),
        "termsScope": _TERMS_SCOPE,
    }
