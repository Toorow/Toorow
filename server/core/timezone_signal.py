"""toorow -- the cross-source day-offset SIGNAL engine (Story 39.8).

This is the SIGNAL half of Epic 39's report-timezone module. Story 39.7 owns CAPTURE (each
datastream's exact ``report_timezone`` landed as immutable provenance); 39.8 owns what the
platform DOES with two or more captured timezones when figures are compared/reconciled at
DATE grain. It does exactly three things and only three things:

  1. It SIGNALS a possible cross-source day-offset. When figures from datastreams that carry
     DIFFERENT captured report timezones are compared at DATE grain, this engine emits a typed
     ADVISORY -- ``TIMEZONE_DAY_OFFSET`` -- that names the timezones and explains the possible
     drift as PROVENANCE. It is an advisory, NOT a refusal (unlike ``CURRENCY_CONFLICT``, which
     refuses an illegal sum: a day-offset does not make a total WRONG, only a comparison
     POSSIBLY MISALIGNED).
  2. It NEVER realigns. The reporting grain is DATE everywhere, never hourly -- there is NO
     sub-day data to re-slice a source onto the "right" day. A realignment engine would be an
     ILLUSION (it would fabricate an aligned day from data that does not exist). So this engine
     EXPLAINS the offset and does NOT manufacture an aligned day, a shifted total, or a
     re-bucketed figure. ``realignable`` is a HARDCODED constant ``False``: there is no branch
     that ever sets it True, and no code path in this module mutates a figure's date or value.
  3. It SAYS whether an adjustment lever exists at the source. A few sources expose a
     report-timezone setting in their dimension filters (rare). When such a lever is declared
     for a stream (``has_lever=True`` + a ``lever_hint``), the signal points the user to act at
     the source; when no lever is declared, the stream is reported as "fixed on the detected
     timezone, no lever available" (the honest posture).

NO FALSE POSITIVES (E39-NFR06): a single known timezone, with nothing unplaceable, flags
NOTHING. This engine reads and reports; it never writes a figure, never mutates a date, never
re-buckets -- so E39-NFR06 ("existing totals provably unchanged") holds BY CONSTRUCTION (this
module opens no warehouse cursor and does no total math at all).

STORY 48.3 CORRECTION. A stream whose ``report_timezone`` was unknown used to be EXCLUDED
from the comparison, and the function returned ``None`` when fewer than two known zones
remained. Two streams agreeing and three unplaceable therefore rendered as "no offset" -- an
incomplete comparison presented as a healthy one, which is the Reporting Timezone criterion
"cross-source daily reconciliation ignores different day boundaries" arriving through the back
door. Unplaceable streams are now RETAINED in ``unplaced_streams`` and reported. It still
never fabricates a UTC to force or suppress an offset: unknown stays unknown, it is simply
visible now.

STATELESS & SOURCE-AGNOSTIC (AD-2, E39-NFR05): this module contains ZERO provider/connector
names and names NO provider field. Timezones arrive as captured provenance strings (generic
keys, resolved by the caller); the lever posture arrives as per-stream ``has_lever`` /
``lever_hint`` inputs (resolved by the caller from the connector's OWN descriptor). Core owns
"do these known IANA names differ?", "what is a best-effort offset label?", "what payload?".

DETERMINISM: ``distinct_timezones`` is sorted; ``report_timezones`` is sorted by
``datastream`` then ``report_timezone``. No wall-clock, no randomness -- byte-identical output
for identical input (tests + UI stability).

Windows/CI note: all identifiers/labels use ASCII-safe characters; the user message body is
UTF-8 like the CURRENCY_CONFLICT message it mirrors.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core import time_boundary as _tb

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Codes (the signal + the two lever postures -- AC1 / AC2).
# ---------------------------------------------------------------------------

SIGNAL_TIMEZONE_DAY_OFFSET = "TIMEZONE_DAY_OFFSET"  # AC1 -- >=2 distinct captured report tz
LEVER_NONE = "fixed_no_lever"  # AC2 -- "fixed on the detected timezone, no lever available"
LEVER_SOURCE_FILTER = "source_report_tz_filter"  # AC2 -- source exposes a report-tz filter

# The severity that makes this a SHOW-not-REFUSE signal (contrast CURRENCY_CONFLICT's
# "refusal"). Load-bearing: list_conflicts / any surface renders provenance, never gates a sum.
SEVERITY_ADVISORY = "advisory"


# ---------------------------------------------------------------------------
# utc_offset_label -- best-effort, honest (zoneinfo, PURE). NOT an alignment input.
# ---------------------------------------------------------------------------


def _utc_offset_label(zone: str) -> str | None:
    """Return a human offset label ("UTC+1") for an IANA *zone*, or None if unresolvable.

    Best-effort (B.2): uses the stdlib ``zoneinfo`` to compute a REPRESENTATIVE offset. When
    ``zoneinfo`` cannot resolve the name, returns None (NEVER invents an offset, AD-9). A
    DST-varying zone's offset is inherently approximate at DATE grain -- this label is a human
    hint ONLY and MUST NOT be treated as an alignment input (there is no alignment). The
    equality decision that fires the signal is on IANA NAMES, never on this computed offset.
    """
    if not isinstance(zone, str) or not zone.strip():
        return None
    try:
        tz = ZoneInfo(zone.strip())
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None
    # A fixed reference instant (no wall-clock -> deterministic). The label is representative;
    # a DST-varying zone is approximate at DATE grain by nature, which is honest here.
    ref = datetime(2021, 1, 1, tzinfo=timezone.utc).astimezone(tz)
    offset = ref.utcoffset()
    if offset is None:
        return None
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    hours, minutes = divmod(total_minutes, 60)
    if minutes:
        return f"UTC{sign}{hours}:{minutes:02d}"
    return f"UTC{sign}{hours}"


# ---------------------------------------------------------------------------
# check_cross_source_day_offset -- the pure decision engine (AC1 / AC2 / AC3).
# ---------------------------------------------------------------------------


def check_cross_source_day_offset(
    *,
    metric: str | None,
    streams: list[dict],
) -> dict | None:
    """Return a typed ``TIMEZONE_DAY_OFFSET`` advisory dict, or None when no offset applies.

    None (NO signal -- AC3, no false positive) ONLY when every stream carries a KNOWN
    ``report_timezone`` and they are all identical. That is the single case in which the
    comparison is both complete and aligned.

    Advisory (returns a typed dict -- AC1) when EITHER:
      * >= 2 DISTINCT known report timezones across the compared streams, OR
      * at least one stream has no resolvable timezone (Story 48.3): an unplaceable stream is
        exactly the one whose day might not line up, so hiding it is the defect.
    The payload carries per-stream tz + lever posture (AC2), the sorted distinct timezone set,
    ``unplaced_streams``, ``realignable: False`` (constant -- the amendment),
    ``severity: "advisory"`` and an English message. Deterministic: ``distinct_timezones`` and
    per-stream order are SORTED; no wall-clock, no randomness.

    Args:
        metric:  the canonical metric being compared (echoed into the payload), or None.
        streams: per-stream provenance dicts, each ``{datastream, report_timezone, has_lever?,
                 lever_hint?}``. ``report_timezone`` None/blank => the stream is reported in
                 ``unplaced_streams`` rather than dropped. ``has_lever``/``lever_hint`` come
                 from the caller's descriptor read (AD-2 -- this engine never names a provider).

    Returns:
        A typed ``TIMEZONE_DAY_OFFSET`` advisory dict, or None when the signal does not apply.
    """
    if not isinstance(streams, list):
        return None

    # 1. Normalize + EXCLUDE unknown-timezone streams (defer to 39.7's GAP; never fabricate a
    #    UTC). Only streams that carry a resolvable known IANA name participate.
    known: list[dict] = []
    for s in streams:
        if not isinstance(s, dict):
            continue
        raw_tz = s.get("report_timezone")
        if not isinstance(raw_tz, str) or not raw_tz.strip():
            continue  # unknown/None => EXCLUDED (39.7's gap), never coerced to UTC
        tz = raw_tz.strip()
        datastream = s.get("datastream")
        # has_lever / lever_hint come from the caller's descriptor read (AC2, AD-2).
        has_lever = bool(s.get("has_lever"))
        lever_hint = s.get("lever_hint")
        if not isinstance(lever_hint, str) or not lever_hint.strip():
            lever_hint = None
        else:
            lever_hint = lever_hint.strip()
        # A stream flagged has_lever must carry a hint to be actionable; without one, the
        # honest posture is "no lever" (do not point the user at nothing).
        if not has_lever:
            lever_hint = None
        known.append(
            {
                "datastream": datastream,
                "report_timezone": tz,
                "utc_offset_label": _utc_offset_label(tz),
                "has_lever": has_lever,
                "lever_hint": lever_hint,
            }
        )

    # 1b. Story 48.3: the excluded streams are RETAINED and reported.
    #     Dropping them was the defect. With two known streams agreeing and three
    #     unplaceable, this function used to return None -- "no offset" -- and an
    #     incomplete comparison rendered as a healthy one. A stream that cannot be
    #     placed on a clock is exactly the stream whose day might not line up.
    unplaced = [
        {"datastream": s.get("datastream"), "reason": "source_report_timezone_unknown"}
        for s in streams
        if isinstance(s, dict)
        and not (isinstance(s.get("report_timezone"), str) and s["report_timezone"].strip())
    ]

    # 2. Equality is on IANA NAME (not computed offset): two names sharing an offset
    #    seasonally are still distinct provenance (B.2). Fewer than 2 distinct known
    #    zones AND nothing unplaceable is the one case that flags nothing.
    distinct = sorted({s["report_timezone"] for s in known})
    if len(distinct) < 2 and not unplaced:
        return None

    # 3. Deterministic ordering of the per-stream payload (datastream, then timezone). None
    #    datastream sorts stably last via the (is-None, value) key.
    report_timezones = sorted(
        known,
        key=lambda s: (
            s["datastream"] is None,
            str(s["datastream"]) if s["datastream"] is not None else "",
            s["report_timezone"],
        ),
    )
    affected_streams = [s["datastream"] for s in report_timezones]

    # 4. Build the honest message naming the ACTUAL timezones + metric, and stating no
    #    realignment is possible (the amendment). Deterministic (sorted distinct set).
    #    English: this string reaches a screen, and Story 48.3 AC11 makes that the rule.
    metric_phrase = f" for '{metric}'" if metric else ""
    parts: list[str] = []
    if len(distinct) > 1:
        tz_phrase = " vs ".join(_label_for(s) for s in _first_two_distinct(report_timezones))
        parts.append(
            f"This field{metric_phrase} is fed by streams that draw their reporting day on "
            f"different clocks ({tz_phrase}). Daily totals can be one day out at the boundary. "
            "No realignment is possible -- the grain is DATE, there is no hour to re-slice -- "
            "so this is a provenance signal, not a data error."
        )
    if unplaced:
        parts.append(
            f"{len(unplaced)} stream(s) have no resolvable reporting timezone, so their days "
            "cannot be declared equivalent to any other stream's. They are listed rather than "
            "excluded: an unplaceable stream is exactly the one whose day might not line up."
        )

    # AI-132: SAY the lever posture, do not merely carry it.
    #
    # `report_timezones[]` has carried `has_lever`/`lever_hint` since 39.8 and the message
    # never mentioned them. But the mapping screen renders only `code`, `message` and
    # `affected_streams`: the lever was travelling in a key nobody displays, so the
    # criterion "the user cannot tell whether the source offers an adjustment lever"
    # stayed true even once the flag had a producer.
    #
    # The ratified AC requires both faces: "the platform says the adjustment lever exists
    # and points the user to act at the source; a source with no such lever is reported as
    # 'fixed on the detected timezone, no lever available'". The absence of a lever is
    # therefore a SENTENCE, not a silence -- without it, "nobody told me" and "there is
    # nothing to do" read the same.
    actionable = sorted(
        (s for s in known if s["has_lever"]),
        key=lambda s: (s["datastream"] is None, str(s["datastream"] or ""), s["report_timezone"]),
    )
    if actionable:
        for stream in actionable:
            # Name the STREAM, not just its zone: the stream is what one opens at the
            # source in order to act.
            who = stream["datastream"] or _label_for(stream)
            parts.append(
                f"'{who}' reports in {_label_for(stream)} and that can be changed at the "
                f"source: {stream['lever_hint']}"
            )
    if len(actionable) < len(known):
        stuck = len(known) - len(actionable)
        parts.append(
            f"{stuck} stream(s) are fixed on the detected timezone, no lever available at the "
            "source."
        )

    # AI-167. ONE code covered TWO different problems, and they are repaired by two
    # different gestures. « These sources draw their day on different clocks » is
    # settled by a policy decision; « we could not place this source on any clock »
    # is settled by RUNNING the Datastream so its publication records one. The
    # message already said which -- measured 2026-08-16, both cases read correctly
    # -- but a message is prose, and a consumer that switches on `code` saw the
    # same value for both and could offer only one gesture.
    #
    # The vocabulary is `core.time_boundary`'s, not a third one invented here.
    # `compare_boundaries` was written to answer this exact question with a typed
    # verdict and had no caller: rather than wire a second comparison beside the
    # live one -- two engines answering one question, which is the shape this
    # repository keeps paying for -- its REASON CODES become the typed half of
    # the signal that is already wired. There is still one comparison.
    reasons: list[str] = []
    if len(distinct) > 1:
        reasons.append(_tb.SIGNAL_BOUNDARY_DIFFERS)
    if unplaced:
        reasons.append(_tb.GAP_NO_SOURCE_ZONE)

    return {
        "code": SIGNAL_TIMEZONE_DAY_OFFSET,
        # BOTH can hold at once -- some sources on different clocks AND others
        # unplaceable -- so this is a list. Collapsing it to the "worst" one would
        # hide a repair the operator can actually perform today.
        "reasons": reasons,
        "message": " ".join(parts),
        "affected_streams": affected_streams + [item["datastream"] for item in unplaced],
        # --- 39.8 additive keys ---
        "severity": SEVERITY_ADVISORY,  # NOT "refusal" -- a day-offset is not a wrong total
        "report_timezones": report_timezones,  # per-stream tz + lever posture (AC1 + AC2)
        "distinct_timezones": distinct,  # sorted distinct captured tz
        # Story 48.3: the streams that could NOT be placed, retained rather than dropped.
        "unplaced_streams": unplaced,
        "realignable": False,  # ALWAYS false -- grain=DATE, no sub-day data (amendment)
        "metric": metric,  # the canonical metric involved
    }


# ---------------------------------------------------------------------------
# Message helpers (pure, deterministic).
# ---------------------------------------------------------------------------


def _label_for(stream: dict) -> str:
    """Render a stream's timezone with its best-effort offset label for the message body."""
    tz = stream["report_timezone"]
    label = stream.get("utc_offset_label")
    return f"'{tz}' {label}" if label else f"'{tz}'"


def _first_two_distinct(report_timezones: list[dict]) -> list[dict]:
    """Return the first stream for each of the first two DISTINCT timezones (for the message).

    Deterministic (input is already sorted). Keeps the message readable when many streams share
    the same two zones -- the signal still names the divergence with a representative pair.
    """
    seen: set[str] = set()
    picked: list[dict] = []
    for s in report_timezones:
        tz = s["report_timezone"]
        if tz not in seen:
            seen.add(tz)
            picked.append(s)
        if len(picked) == 2:
            break
    return picked
