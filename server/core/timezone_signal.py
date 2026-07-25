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

NO FALSE POSITIVES (E39-NFR06): fewer than 2 DISTINCT KNOWN timezones -> ``None`` (no
advisory). Single-timezone data, or data where all compared sources share one timezone, flags
NOTHING. A stream whose ``report_timezone`` is unknown/None (Story 39.7's undetermined GAP) is
EXCLUDED from the comparison here -- it never fabricates a UTC to force (or suppress) an offset
(fail-closed, defer-to-39.7). This engine reads and reports; it never writes a figure, never
mutates a date, never re-buckets -- so E39-NFR06 ("existing totals provably unchanged") holds
BY CONSTRUCTION (this module opens no warehouse cursor and does no total math at all).

STATELESS & SOURCE-AGNOSTIC (AD-2, E39-NFR05): this module contains ZERO provider/connector
names and names NO provider field. Timezones arrive as captured provenance strings (generic
keys, resolved by the caller); the lever posture arrives as per-stream ``has_lever`` /
``lever_hint`` inputs (resolved by the caller from the connector's OWN descriptor). Core owns
"do these known IANA names differ?", "what is a best-effort offset label?", "what payload?".

DETERMINISM: ``distinct_timezones`` is sorted; ``report_timezones`` is sorted by
``datastream`` then ``report_timezone``. No wall-clock, no randomness -- byte-identical output
for identical input (tests + UI stability).

Windows/CI note: all identifiers/labels use ASCII-safe characters; the FR user message body is
UTF-8 like the CURRENCY_CONFLICT message it mirrors.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

    None (NO signal -- AC3, no false positive) when:
      * fewer than 2 streams carry a KNOWN ``report_timezone`` (nothing to compare), OR
      * every KNOWN ``report_timezone`` is identical (all sources share one timezone).
    A stream whose ``report_timezone`` is UNKNOWN/None (Story 39.7's undetermined GAP) is
    EXCLUDED from the comparison here -- its gap is 39.7's to surface; this engine never
    fabricates a UTC to force a false offset (fail-closed, defer-to-39.7, E39-NFR02).

    Advisory (returns a typed dict -- AC1) when:
      * >= 2 DISTINCT known report timezones across the compared streams.
    The payload carries per-stream tz + lever posture (AC2), the sorted distinct timezone set,
    ``realignable: False`` (constant -- the amendment), ``severity: "advisory"``, and an honest
    FR message naming the timezones. Deterministic: ``distinct_timezones`` and per-stream order
    are SORTED; no wall-clock, no randomness.

    Args:
        metric:  the canonical metric being compared (echoed into the payload), or None.
        streams: per-stream provenance dicts, each ``{datastream, report_timezone, has_lever?,
                 lever_hint?}``. ``report_timezone`` None/blank => the stream is EXCLUDED
                 (defer to 39.7's GAP). ``has_lever``/``lever_hint`` come from the caller's
                 descriptor read (AD-2 -- this engine never names a provider).

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

    # 2. NO false positives (AC3, E39-NFR06): < 2 DISTINCT known timezones flags NOTHING.
    #    Equality is on IANA NAME (not computed offset): two names sharing an offset seasonally
    #    are still distinct provenance (B.2).
    distinct = sorted({s["report_timezone"] for s in known})
    if len(distinct) < 2:
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

    # 4. Build the honest FR message naming the ACTUAL timezones + metric, and stating no
    #    realignment is possible (the amendment). Deterministic (sorted distinct set).
    tz_phrase = " vs ".join(_label_for(s) for s in _first_two_distinct(report_timezones))
    metric_phrase = f" pour la metrique '{metric}'" if metric else ""
    message = (
        f"Ce champ{metric_phrase} est alimente par des flux dont le fuseau de reporting "
        f"differe ({tz_phrase}). Les totaux journaliers peuvent etre legerement decales d'un "
        "jour a la frontiere : une 'journee' n'est pas decoupee sur la meme horloge selon la "
        "source. Aucune realignement n'est possible (grain = DATE, pas d'heure) -- c'est un "
        "signal de provenance, pas une erreur de donnee."
    )

    return {
        "code": SIGNAL_TIMEZONE_DAY_OFFSET,
        "message": message,
        "affected_streams": affected_streams,
        # --- 39.8 additive keys ---
        "severity": SEVERITY_ADVISORY,  # NOT "refusal" -- a day-offset is not a wrong total
        "report_timezones": report_timezones,  # per-stream tz + lever posture (AC1 + AC2)
        "distinct_timezones": distinct,  # sorted distinct captured tz (>=2 => signal fired)
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
