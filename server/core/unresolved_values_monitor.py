"""The ninth DQ monitor: a NEW unresolved value announces itself, once.

Target: ``docs/product-architecture/unresolved-values.md``, section *Alerting --
the new value announces itself, the backlog does not* (ratified 2026-08-12 by
Jean, decided « construire » on 2026-08-31).

WHAT WAS MISSING, MEASURED 2026-08-31. The reading existed and was served
(`core.unresolved_values`, `core.unresolved_values_api`, screens S1 and S2), and
nothing announced anything: `unresolved_values` was absent from
`dq_monitor_registry.DQ_MONITORS` (nine entries now, eight then) and no writer
anywhere emitted a `dq_unresolved_values` firing. A gap a person must open a
screen to discover is a gap that is discovered late.

THE FOUR RULES THIS MODULE IS, and each one is a line of the page's own
"Incomplete if":

1. **One firing per `(datastream, dimension, reason)`, never one per value.**
   "One hundred and twenty-eight new videos open ONE issue carrying 128 [...]
   never 128 incidents nobody can acknowledge."

2. **It fires on the DELTA.** A value already announced is never announced again.
   The memory is `app.unresolved_value_sightings` (migration 326) and it holds a
   DIGEST of each value, never the value: see the migration header, section 3.

3. **The first arming seeds and fires nothing.** The backlog of a
   `(Datastream, dimension)` that has just become armed is recorded as the
   baseline, with `announced = FALSE`, and mailed to nobody. Arming the module on
   the case that paid for this page -- 516 unnamed videos -- would otherwise mail
   516 "new arrivals" on night one and be muted on night two.

4. **The address is the owner reference the console can open.** The firing
   carries `geographic_conformance.build_repair_reference`, the seam delivered on
   2026-08-31, and is therefore walked by
   `tests/conformance/test_a_dq_alert_names_a_repair_the_console_can_open.py`
   like every other DQ writer. A repair address that names a Python module reads
   like an address and opens nothing.

ONE READING, THREE PROJECTIONS -- and it is the page's acceptance criterion, not
a preference. This module takes NO reading of its own: it calls
`unresolved_values_api.read_datastream_unresolved`, which is what S1 renders and
what S2 loops over. A monitor with its own query would be the third number the
page forbids ("the Workbench and the Governance lens answer with two different
numbers for the same Project" -- and a nightly alert disagreeing with both is
worse).

WHICH DIMENSIONS ALERT. Every mapped dimension is swept for the LIST; only an
**armed** one writes a firing, and a dimension is armed when a value mapping
table is assigned to its field -- "the person has already declared they care
about that vocabulary, so no new toggle is invented to ask them twice". A
dimension with no assignment therefore gets no watch row at all: the day a table
is assigned is the day its backlog becomes the baseline, which is rule 3 doing
its job rather than a second rule.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date
from typing import Any, Mapping, Sequence

# TOP-LEVEL ON PURPOSE, and the reason is a guard rather than a style. The repair
# conformance test follows a `"repair": <call>` back to a module-level attribute
# of the module that writes the firing; behind a function-local import there is no
# attribute to follow, and the guard reports the address as unreadable -- which is
# exactly what it does to a writer that invents its own vocabulary.
from core.geographic_conformance import build_repair_reference
from core.unresolved_values import (
    REASON_ABSENT_AT_SOURCE,
    REASON_NO_REFERENCE,
    REASON_UNMAPPED,
    REASONS,
)

logger = logging.getLogger(__name__)

#: The `app.alert_firings.type` this monitor writes. Declared in
#: `dq_monitor_registry` too, which is the vocabulary; this constant is what the
#: call site passes, so `tests/test_infra_alerts.py`'s scanner can resolve it.
ALERT_TYPE = "dq_unresolved_values"

#: The check profile key. Same string as the registry's, as story 59.3 and 59.4
#: established for their own monitors.
CHECK_PROFILE = "unresolved_values"

#: What is measured, in the `metric` column: the count of distinct values that
#: newly failed to resolve.
METRIC = "unresolved_values"

#: The share of the window's rows the NEW values of one group must carry before a
#: firing is written. "One new value carrying three rows is not a reason to wake
#: anybody, and one carrying 12 % of the month's spend is."
#:
#: One per cent is a DOCUMENTED DEFAULT and it is recorded as such on every
#: firing: `app.project_preferences.min_unresolved_new_value_row_share` overrides
#: it per project, and `threshold_source` says which of the two was applied so no
#: platform-wide number is ever applied silently ("prefer per-project
#: preferences").
DEFAULT_MIN_NEW_VALUE_ROW_SHARE = 0.01

THRESHOLD_DOCUMENTED_DEFAULT = "documented_default"
THRESHOLD_PROJECT_PREFERENCE = "project_preference"

#: How many values ride on a firing. Bounded, ordered by cost, and STATED: the
#: page forbids carrying the rows, and a list that stopped at its bound in silence
#: reads as "this is everything there is".
MAX_REPORTED_VALUES = 10

#: The reasons, in the words a firing message uses. The codes never merge into one
#: count and they never merge into one sentence either: three gestures repair them.
REASON_PHRASES: dict[str, str] = {
    REASON_ABSENT_AT_SOURCE: "arrived with no value at all",
    REASON_UNMAPPED: "carry no canonical name",
    REASON_NO_REFERENCE: "point at a reference set that does not hold them",
}

#: Why an evaluation wrote nothing. Never folded into "no issue".
NOT_ARMED = "no_armed_dimension"
NO_MAPPED_DIMENSION = "no_mapped_dimension"
ASSIGNMENTS_UNREADABLE = "value_table_assignments_unreadable"
NOTHING_MEASURED = "no_dimension_could_be_read"


# ---------------------------------------------------------------------------
# Pure helpers -- no connection, no warehouse, testable offline.
# ---------------------------------------------------------------------------


def fingerprint(source_value: Any) -> str:
    """The identity of a value in the memory: SHA-256 of its UTF-8 form.

    PURE. Not salted, and the migration header says why: a salt would make the
    memory unreadable to the very sweep that writes it, while the digest is
    already keyed by Datastream and dimension.
    """
    text = "" if source_value is None else str(source_value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def finding_key(dimension: str, reason: str) -> str:
    """What this firing is ABOUT, beyond its Datastream and its day.

    PURE. `app.alert_firings` dedups a DQ firing on
    `(project_id, type, datastream_id, window_date, COALESCE(finding_key, ''))`
    since migration 326. The project, the type, the Datastream and the day are
    already in that key; what this adds is the half migration 229 could not know
    about -- the dimension and the reason, which are the grain of the finding.
    """
    return f"{dimension}::{reason}"


def resolve_threshold(preferences: Mapping[str, Any] | None) -> tuple[float, str]:
    """The governed cost threshold of one project, and WHERE it came from.

    PURE, and shaped on `datastream_projection.resolve_thresholds` deliberately:
    the pair `(value, source)` is what stops a documented default from being
    reported as a client's decision.
    """
    declared = (preferences or {}).get("min_unresolved_new_value_row_share")
    if declared is None:
        return DEFAULT_MIN_NEW_VALUE_ROW_SHARE, THRESHOLD_DOCUMENTED_DEFAULT
    try:
        share = float(declared)
    except (TypeError, ValueError):
        return DEFAULT_MIN_NEW_VALUE_ROW_SHARE, THRESHOLD_DOCUMENTED_DEFAULT
    if share < 0 or share > 1:
        return DEFAULT_MIN_NEW_VALUE_ROW_SHARE, THRESHOLD_DOCUMENTED_DEFAULT
    return share, THRESHOLD_PROJECT_PREFERENCE


def new_values(
    values: Sequence[Mapping[str, Any]], *, reason: str, known: set[str]
) -> list[Mapping[str, Any]]:
    """The values of one reason this memory has never announced. PURE.

    Order is preserved, and the reading gives it ordered by cost -- so the top of
    this list is the value worth repairing first, which is the one thing neither
    Adverity nor Funnel does.
    """
    fresh: list[Mapping[str, Any]] = []
    for row in values:
        if str(row.get("reason") or "") != reason:
            continue
        if fingerprint(row.get("source_value")) in known:
            continue
        fresh.append(row)
    return fresh


def row_share(occurrences: int, window_rows: Any) -> float | None:
    """The share of the window's rows these values carry, or `None`. PURE.

    `None` when the denominator was not read: a share that could not be computed
    is never a zero, which would read as "these values cost nothing".
    """
    try:
        total = int(window_rows or 0)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    return round(int(occurrences) / total, 6)


def build_firing(
    fresh: Sequence[Mapping[str, Any]],
    *,
    datastream_id: str,
    datastream_name: str,
    dimension: str,
    reason: str,
    window: Mapping[str, Any],
    window_rows: Any,
    threshold: float,
    threshold_source: str,
    truncated: bool = False,
    connector: str = "",
) -> dict[str, Any] | None:
    """The message and the metadata of ONE group's firing. PURE.

    `None` when there is nothing to announce -- an empty firing is noise, and the
    page's own rule is that a fabricated green is worse than silence.
    """
    if not fresh:
        return None
    occurrences = sum(int(row.get("occurrences") or 0) for row in fresh)
    share = row_share(occurrences, window_rows)
    # The address a reader can OPEN. S1, the `Map` tab of the Datastream that
    # emitted the value -- this monitor always knows its Datastream, which is
    # exactly the case `build_repair_reference` reserves the S1 form for.
    repair = build_repair_reference(datastream_id)
    return {
        "message": (
            f"Unresolved values: {len(fresh)} new value(s) of '{dimension}' on "
            f"'{datastream_name}' {REASON_PHRASES.get(reason, reason)}, over "
            f"{occurrences} row(s) of the window {window.get('start')} to "
            f"{window.get('end')}."
        ),
        "metadata": {
            "datastream_id": datastream_id,
            "datastream_name": datastream_name,
            "connector": connector or None,
            "dimension": dimension,
            "reason": reason,
            # One firing per (datastream, dimension, reason): this is the half of
            # that identity `app.alert_firings` could not express before 326.
            "finding_key": finding_key(dimension, reason),
            "window_date": str(window.get("end") or ""),
            "window_start": str(window.get("start") or ""),
            "window_end": str(window.get("end") or ""),
            "new_values": len(fresh),
            "new_value_occurrences": occurrences,
            "window_rows": window_rows,
            "new_value_row_share": share,
            "threshold": threshold,
            "threshold_source": threshold_source,
            # Bounded, ordered by cost, and SAID -- never the rows, which are
            # replayed on demand by the panel's own reading.
            "top_values": [
                {
                    "source_value": row.get("source_value"),
                    "connector": row.get("connector"),
                    "occurrences": row.get("occurrences"),
                }
                for row in fresh[:MAX_REPORTED_VALUES]
            ],
            "top_values_truncated": len(fresh) > MAX_REPORTED_VALUES,
            "reading_truncated": bool(truncated),
            "repair": repair,
        },
    }


# ---------------------------------------------------------------------------
# The memory. Two tables, migration 326.
# ---------------------------------------------------------------------------


def read_watches(conn, datastream_id: str) -> dict[str, dict[str, Any]]:
    """The `(Datastream, dimension)` pairs this monitor has already armed."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT dimension, armed_at, baseline_values "
            "FROM app.unresolved_value_watches WHERE datastream_id = %s",
            (datastream_id,),
        )
        return {
            str(row[0]): {"armed_at": row[1], "baseline_values": row[2]}
            for row in cur.fetchall()
        }


def read_sightings(conn, datastream_id: str) -> dict[tuple[str, str], set[str]]:
    """Everything already announced for one Datastream, keyed by group."""
    memory: dict[tuple[str, str], set[str]] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT dimension, reason, value_fingerprint "
            "FROM app.unresolved_value_sightings WHERE datastream_id = %s",
            (datastream_id,),
        )
        for dimension, reason, digest in cur.fetchall():
            memory.setdefault((str(dimension), str(reason)), set()).add(str(digest))
    return memory


def arm_dimension(
    conn,
    *,
    org_id: str,
    project_id: str,
    datastream_id: str,
    dimension: str,
    baseline_values: int,
) -> None:
    """Start watching a `(Datastream, dimension)`. Idempotent."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.unresolved_value_watches
                (datastream_id, dimension, org_id, project_id, baseline_values,
                 last_evaluated_at)
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (datastream_id, dimension) DO NOTHING
            """,
            (datastream_id, dimension, org_id, project_id, int(baseline_values)),
        )


def touch_watch(conn, *, datastream_id: str, dimension: str, fired: bool = False) -> None:
    """Record that the pair was evaluated tonight, and whether it spoke."""
    with conn.cursor() as cur:
        if fired:
            cur.execute(
                "UPDATE app.unresolved_value_watches "
                "SET last_evaluated_at = NOW(), last_fired_at = NOW() "
                "WHERE datastream_id = %s AND dimension = %s",
                (datastream_id, dimension),
            )
        else:
            cur.execute(
                "UPDATE app.unresolved_value_watches SET last_evaluated_at = NOW() "
                "WHERE datastream_id = %s AND dimension = %s",
                (datastream_id, dimension),
            )


def record_sightings(
    conn,
    rows: Sequence[Mapping[str, Any]],
    *,
    org_id: str,
    project_id: str,
    datastream_id: str,
    dimension: str,
    reason: str | None = None,
    announced: bool,
) -> int:
    """Remember these values, so tonight's finding is never restated tomorrow.

    `reason` is taken from each row when it is not forced, because the baseline of
    a first arming spans all three reasons at once and they never merge.

    A value already remembered keeps its `first_seen_at` and its `announced`: the
    row says when the product FIRST saw it and whether somebody was told, and an
    upsert that overwrote either would erase the only evidence that the first
    arming stayed quiet.
    """
    written = 0
    with conn.cursor() as cur:
        for row in rows:
            row_reason = reason or str(row.get("reason") or "")
            if row_reason not in REASONS:
                continue
            cur.execute(
                """
                INSERT INTO app.unresolved_value_sightings
                    (datastream_id, dimension, reason, value_fingerprint,
                     org_id, project_id, announced)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (datastream_id, dimension, reason, value_fingerprint)
                DO UPDATE SET last_seen_at = NOW()
                """,
                (
                    datastream_id,
                    dimension,
                    row_reason,
                    fingerprint(row.get("source_value")),
                    org_id,
                    project_id,
                    bool(announced),
                ),
            )
            written += 1
    return written


def read_threshold(conn, project_id: str) -> tuple[float, str]:
    """The project's declared cost threshold, or the documented default."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT min_unresolved_new_value_row_share "
                "FROM app.project_preferences WHERE project_id = %s",
                (project_id,),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 -- a preference nobody could read
        logger.warning(
            "unresolved_values_monitor: preferences unreadable project=%s: %s",
            project_id,
            exc,
        )
        return DEFAULT_MIN_NEW_VALUE_ROW_SHARE, THRESHOLD_DOCUMENTED_DEFAULT
    return resolve_threshold({"min_unresolved_new_value_row_share": row[0]} if row else None)


# ---------------------------------------------------------------------------
# The sweep.
# ---------------------------------------------------------------------------


def emit_firing(project_id: str, payload: Mapping[str, Any]) -> bool:
    """Write ONE `dq_unresolved_values` firing. Never raises.

    THE MESSAGE IS SUBSCRIPTED, NOT `.get()`-ed, and that is not a style choice.
    `tests/test_infra_alerts.py` follows the sentence a person will read back to
    the f-string that composes it -- through `payload["message"]`, the local
    `payload = build_firing(...)`, and that function's return dict. A
    `.get(..., "")` puts a `BoolOp` in the middle of that chain, the scanner
    reports the message as unreadable, and an unreadable message is one it cannot
    check for English. The same shape `geographic_conformance` already uses.
    """
    metadata = dict(payload.get("metadata") or {})
    try:
        from core import infra_alerts  # noqa: PLC0415

        infra_alerts.write_infra_firing(
            alert_type=ALERT_TYPE,
            project_id=project_id,
            metric=METRIC,
            severity="warning",
            message=str(payload["message"]),
            observed_value=metadata.get("new_values"),
            threshold=metadata.get("threshold"),
            window_date=metadata.get("window_date"),
            metadata=metadata,
        )
    except Exception as exc:  # noqa: BLE001 -- an alert never breaks the sweep
        logger.warning(
            "unresolved_values_monitor: firing failed project=%s ds=%s: %s",
            project_id,
            metadata.get("datastream_id"),
            exc,
        )
        return False
    return True


def sweep_datastream(
    conn,
    ds: Mapping[str, Any],
    *,
    today: date | None = None,
    reading: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate one Datastream: arm what is new, announce the delta, and say so.

    Answers a plain dict; `dq_monitors._check_unresolved_values` turns it into the
    `CheckVerdict` a dispatched check owes. `reading` is injectable so a test can
    replay a warehouse answer without a warehouse -- the reading itself is proved
    by `unresolved_values_api`'s own suite, and what is proved HERE is the memory.
    """
    project_id = str(ds.get("project_id") or "")
    datastream_id = str(ds.get("id") or "")
    org_id = str(ds.get("org_id") or "")
    datastream_name = str(ds.get("name") or datastream_id)
    result: dict[str, Any] = {
        "datastream_id": datastream_id,
        "project_id": project_id,
        "fired": 0,
        "armed": [],
        "recorded": 0,
        "evaluated_dimensions": 0,
        "reason": None,
    }

    if reading is None:
        from core.unresolved_values_api import read_datastream_unresolved  # noqa: PLC0415

        reading = read_datastream_unresolved(
            conn, project_id=project_id, datastream_id=datastream_id, today=today
        )

    result["window"] = dict(reading.get("window") or {})
    state = str(reading.get("state") or "")
    if state == "not_applicable":
        # No dimension is mapped yet. Nothing was measured, and that is not a pass.
        result["reason"] = str(reading.get("reason") or NO_MAPPED_DIMENSION)
        result["status"] = "not_applicable"
        return result

    from core.value_mapping_tables import read_assigned_source_fields  # noqa: PLC0415

    assigned = read_assigned_source_fields(
        conn, project_id=project_id, datastream_id=datastream_id
    )
    if assigned is None:
        # The store that says WHICH dimensions are armed could not be read. Not a
        # pass, and not a silent sweep either: firing on an unknown arming would
        # alert about a vocabulary nobody declared they cared about.
        result["reason"] = ASSIGNMENTS_UNREADABLE
        result["status"] = "unavailable"
        return result

    groups = [
        group
        for group in (reading.get("groups") or [])
        if str(group.get("state") or "") == "measured"
        and str(group.get("dimension") or "") in assigned
    ]
    if not groups:
        # Either nothing was readable, or nothing is armed. Two sentences, and the
        # page refuses to fold either into "no issue".
        result["reason"] = (
            NOT_ARMED
            if any(g.get("state") == "measured" for g in reading.get("groups") or [])
            else str(reading.get("reason") or NOTHING_MEASURED)
        )
        result["status"] = "not_applicable" if result["reason"] == NOT_ARMED else "unavailable"
        return result

    threshold, threshold_source = read_threshold(conn, project_id)
    watches = read_watches(conn, datastream_id)
    memory = read_sightings(conn, datastream_id)
    window = dict(reading.get("window") or {})
    connector = str(reading.get("connector") or "")
    result["threshold"] = threshold
    result["threshold_source"] = threshold_source

    for group in groups:
        dimension = str(group.get("dimension") or "")
        values = list(group.get("values") or [])
        result["evaluated_dimensions"] += 1

        if dimension not in watches:
            # THE FIRST ARMING. The backlog becomes the baseline and NOBODY is
            # mailed -- "otherwise arming the module mails somebody their entire
            # history as new".
            arm_dimension(
                conn,
                org_id=org_id,
                project_id=project_id,
                datastream_id=datastream_id,
                dimension=dimension,
                baseline_values=len(values),
            )
            result["recorded"] += record_sightings(
                conn,
                values,
                org_id=org_id,
                project_id=project_id,
                datastream_id=datastream_id,
                dimension=dimension,
                announced=False,
            )
            result["armed"].append(dimension)
            continue

        fired_here = False
        for reason in REASONS:
            known = memory.get((dimension, reason), set())
            fresh = new_values(values, reason=reason, known=known)
            if not fresh:
                continue
            occurrences = sum(int(row.get("occurrences") or 0) for row in fresh)
            share = row_share(occurrences, group.get("window_rows"))
            if share is not None and share < threshold:
                # BELOW THE COST THRESHOLD. No firing -- and no sighting either:
                # remembering it here would make it "already reported", and the
                # night it grows to 12 % of the window it would stay silent
                # forever. It is listed on S1 today, as the page says.
                continue
            payload = build_firing(
                fresh,
                datastream_id=datastream_id,
                datastream_name=datastream_name,
                dimension=dimension,
                reason=reason,
                window=window,
                window_rows=group.get("window_rows"),
                threshold=threshold,
                threshold_source=threshold_source,
                truncated=bool(group.get("truncated")),
                connector=connector,
            )
            if payload is None:  # pragma: no cover -- `fresh` is non-empty here
                continue
            if emit_firing(project_id, payload):
                result["fired"] += 1
                fired_here = True
            # Recorded whether or not the transport succeeded: the finding was
            # measured, and a firing this deployment could not write is a fault of
            # ours that must not turn into a nightly repetition of the same list.
            result["recorded"] += record_sightings(
                conn,
                fresh,
                org_id=org_id,
                project_id=project_id,
                datastream_id=datastream_id,
                dimension=dimension,
                reason=reason,
                announced=True,
            )
        touch_watch(conn, datastream_id=datastream_id, dimension=dimension, fired=fired_here)

    result["status"] = "evaluated"
    return result
