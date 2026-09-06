"""A window that returned nothing where the source has been producing.

Story 59.4, epic 59. `empty` is not a new word: `verification.py:576-578` writes
``verdict = "empty"`` when ``actual_rows == 0``, `extract_ledger.py:424-425`
raises it to a day status, and `CoverageBars.tsx:157` already renders it -- "Empty
-- the provider returned nothing". Those surfaces say WHAT IS. This monitor adds
the only sentence they cannot: *and this window should not have been empty*.

WHAT MAKES A ZERO ANOMALOUS, AND WHAT MAKES IT ORDINARY
-------------------------------------------------------
Three days with status ``ok`` among the last 31 days of the ledger -- the SAME
window `_check_volume` already reads ("31 days of ledger (yesterday + 30 prior
days)"). Reusing it is a consistency; inventing a second lookback rule would put
two lookback vocabularies in one file. Three separate days carrying rows in the
last month prove a source produces. One day is a fluke, and firing on it is how a
monitor teaches people to ignore it.

Below that evidence the answer is ``not_applicable`` NAMING THE NUMBER OF DAYS
FOUND -- never a pass, never a firing, never silence. `epic-59:114-115` requires
"no anomaly" and "no monitor ran" to stay distinguishable, and a monitor that
says nothing at all collapses them.

THE SUBJECT IS THE WINDOW, NOT THE DAY
--------------------------------------
The measurement is taken per pull (`verification.py:576-578`) and 123 of the
repository's 130 windows cover 3 or 5 days. One finding per DAY would multiply a
single observation by three or by five. The check therefore judges one day -- the
Datastream's last fetchable one -- and names the PULL that judged it, so an empty
five-day window yields one finding, one `dq_issues` row and one alert.

WHAT IT NEVER READS
-------------------
``row_count``. It is published only when the covering window is exactly one day
wide (`extract_ledger.py:448-458`, story 58.1 arbitrage 9) and NO window in the
repository is one day wide, so it is ``None`` on 100% of ledger days. A monitor
comparing ``row_count == 0`` would measure ``None`` and answer "fine" forever.
Emptiness is read from the verdict, which is a statement about the window and
therefore true of each of its days.

WHAT IT NEVER FIRES ON
----------------------
``never_fetched``, ``failed``, ``running``, ``cancelled`` and ``superseded``. The
last two report ``never_fetched`` at the ledger (`pull_job_states.py:94-116`), and
a stream a person stopped is not a source anomaly. Nor a day inside
``window_offset_days``: the dispatch never asked for it
(`scheduler.py:1335-1340`), so it is not empty, it is unrequested. That guard is
`dq_monitors.effective_window_end`, shared with `_check_timeliness`.

Nothing here raises. Every entry point answers with a named absence: a DQ monitor
that takes the nightly sweep down with it is worse than one that says it could
not measure.
"""

from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any, Mapping, Sequence

from core import dq_monitor_registry

logger = logging.getLogger(__name__)

#: The `check_profile` this monitor publishes under. Present in BOTH vocabularies
#: -- `controls_quality.DQ_CHECKS` (what may be published) and
#: `dq_monitors.CHECK_PROFILES` (what can be evaluated) -- because a profile in
#: one and not the other is either unpublishable or `unverifiable`. The final
#: wording of the seven monitor names is story 59.5's.
CHECK_PROFILE = "zero_rows"

#: The world-A firing type. Same family as its six siblings, so one alert list
#: reads them all.
ALERT_TYPE = "dq_zero_rows"

#: An empty window breaks the day, not the load: the rows that are there are
#: still readable. `blocking` would halt work this monitor has never been allowed
#: to halt.
DEFAULT_SEVERITY = "degrading"

#: The ledger window, and it is `_check_volume`'s: "31 days of ledger (yesterday +
#: 30 prior days)". Thirty days BEFORE the judged day, plus the judged day.
LOOKBACK_DAYS = 30

#: How many `ok` days in that window prove a producing source. Three, and each on
#: a separate day: one day is a fluke.
MIN_ACTIVE_DAYS_ENV = "DQ_ZERO_ROWS_MIN_ACTIVE_DAYS"
DEFAULT_MIN_ACTIVE_DAYS = 3

#: The one day status that counts as evidence of production. `partial` is
#: deliberately NOT counted: the arbitrage names `ok`, and a window the verifier
#: judged incomplete is a poor witness for "this source produces".
ACTIVE_STATUS = "ok"

#: The day status this monitor is about, and the only one it fires on.
EMPTY_STATUS = "empty"

#: Why nothing was measured, or why an empty day is not an anomaly.
INSIDE_EXTRACTION_OFFSET = "inside_extraction_offset"
NO_LEDGER_HISTORY = "no_ledger_history"
NO_LEDGER_DAY = "no_ledger_day"
NOT_HISTORICALLY_ACTIVE = "not_historically_active"
LEDGER_UNREADABLE = "ledger_unreadable"
NOT_A_COLLECTED_DAY = "not_a_collected_day"

_MESSAGES = {
    INSIDE_EXTRACTION_OFFSET: (
        "This day falls inside the Datastream's extraction offset, so no collection was "
        "ever asked for it. A day nobody requested is not an empty day."
    ),
    NO_LEDGER_HISTORY: (
        "This Datastream has no completed collection in the last 31 days, so there is no "
        "history against which an empty window could be judged unusual."
    ),
    NO_LEDGER_DAY: (
        "The ledger holds no row for this day, so there is no window whose emptiness "
        "could be measured."
    ),
    NOT_HISTORICALLY_ACTIVE: (
        "This window returned no rows, but the source has not proven that it produces: "
        "fewer than the required number of days carried rows in the last 31. An empty "
        "window on an unproven source is not yet an anomaly."
    ),
    LEDGER_UNREADABLE: (
        "The extract ledger could not be read, so nothing was measured. An unreadable "
        "ledger is never a pass."
    ),
    NOT_A_COLLECTED_DAY: (
        "No collection landed on this day -- it was never fetched, it failed, it is "
        "still running, or a person stopped it. None of those is a source returning "
        "nothing."
    ),
}

def message_for(reason: str | None) -> str | None:
    """The sentence of a reason of this module, or `None`."""
    if not reason:
        return None
    return _MESSAGES.get(reason)


def min_active_days() -> int:
    """The activity threshold, read at call time so a test can move it."""
    try:
        value = int(os.environ.get(MIN_ACTIVE_DAYS_ENV, DEFAULT_MIN_ACTIVE_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_MIN_ACTIVE_DAYS
    return value if value >= 1 else DEFAULT_MIN_ACTIVE_DAYS


# ---------------------------------------------------------------------------
# What the ledger says about the last 31 days.
# ---------------------------------------------------------------------------

#: The day statuses that prove a collection LANDED, whatever it landed. They are
#: what makes a Datastream evaluable at all -- and what bounds the number of
#: governed objects this monitor derives to the streams that have ever collected
#: (45 of 1421 today), rather than to every enabled one (894).
COLLECTED_STATUSES = ("ok", "partial", EMPTY_STATUS)


def read_window(conn, *, datastream_id: str, window_date: date) -> list[dict]:
    """The 31 ledger days ending at *window_date*, or `[]` when unreadable.

    `get_extract_ledger` fail-softs internally and answers `never_fetched` days
    rather than raising, so the caller cannot tell "no history" from "no ledger"
    by its return alone -- which is why :func:`activity_evidence` counts what
    LANDED and never counts an absence as an answer.
    """
    from datetime import timedelta  # noqa: PLC0415

    from core.extract_ledger import get_extract_ledger  # noqa: PLC0415

    return get_extract_ledger(
        datastream_id,
        (window_date - timedelta(days=LOOKBACK_DAYS)).isoformat(),
        window_date.isoformat(),
        conn,
    )


def activity_evidence(
    entries: Sequence[Mapping[str, Any]],
    window_date: date,
    min_active: int | None = None,
) -> dict[str, Any]:
    """Has this source proven it produces, and on how many days?

    ``active_days`` counts DISTINCT days with status ``ok`` other than the judged
    day itself: the judged day is the one under suspicion, and a day cannot be its
    own evidence. ``collected_days`` counts every day a collection landed on,
    whatever it landed -- that is the eligibility question ("can this Datastream be
    evaluated at all"), which is a different question from ("does this source
    produce").

    *min_active* is the published version's threshold when a governed evaluation
    supplied one, and the environment's default otherwise. It travels WITH the
    evidence so that the verdict, the evaluation and the alert all quote the number
    they were judged by rather than the number that happens to be set when someone
    reads them.
    """
    judged = window_date.isoformat()
    active: list[str] = []
    collected = 0
    for entry in entries or ():
        status = str(entry.get("status") or "")
        day = str(entry.get("date") or "")
        if status in COLLECTED_STATUSES:
            collected += 1
        if status == ACTIVE_STATUS and day and day != judged:
            active.append(day)
    active = sorted(set(active))
    return {
        "active_days": len(active),
        "active_dates": active,
        "collected_days": collected,
        "lookback_days": LOOKBACK_DAYS + 1,
        "min_active_days": (
            min_active if isinstance(min_active, int) and min_active >= 1 else min_active_days()
        ),
    }


def pull_window(
    entries: Sequence[Mapping[str, Any]], pull_id: str | None
) -> tuple[str | None, str | None]:
    """The first and last day of the ledger that *pull_id* covers, within the read.

    The finding's subject is the WINDOW, and this is how it is named without a
    second query: the days a pull covers are the days the ledger already attributed
    to it. Bounded by the 31 days read, so a window reaching further back reports
    the part that was looked at -- which is what was measured.
    """
    if not pull_id:
        return (None, None)
    days = sorted(
        str(entry.get("date"))
        for entry in entries or ()
        if entry.get("pull_id") == pull_id and entry.get("date")
    )
    if not days:
        return (None, None)
    return (days[0], days[-1])


# ---------------------------------------------------------------------------
# The governed object, and the issue.
# ---------------------------------------------------------------------------


def monitor_name(datastream_id: str) -> str | None:
    """`zero_rows_<datastream>` folded to the name CHECK of migration 145:299.

    Minted by the ONE registry since story 59.5, for the reason `dq_null_rate`
    states: four monitors derive a governed object and one naming scheme keeps
    them from watching the same Datastream twice under two names.
    """
    return dq_monitor_registry.monitor_name(CHECK_PROFILE, datastream_id)


def root_cause_fingerprint(monitor_id: str) -> str:
    """`(monitor,)` -- one issue per flux, and the second empty window recurs into it.

    `uq_dq_issues_open_root_cause` is `(project_id, monitor_id, root_cause_fingerprint)`
    and the flux is carried by the MONITOR, so a fingerprint of the monitor alone
    gives exactly one open issue per Datastream. Adding the window date would open
    one issue per date, grow `open` without bound and make that unique index bind
    nothing -- which story 59.3 already refused for the same reason. Which run the
    issue speaks of is answered by `open_issue`, which moves the run with the
    sighting.

    Unlike 59.3's, this fingerprint carries no field: the finding has no field. Its
    subject is a window, and the window is not part of the key.
    """
    from core.governance_rule_sets import content_hash  # noqa: PLC0415

    return content_hash({"monitor_id": str(monitor_id)})


def derive_monitor(
    *,
    org_id: str,
    project_id: str,
    datastream_id: str,
    datastream_name: str,
    severity: str = DEFAULT_SEVERITY,
) -> dict[str, str] | None:
    """Get-or-create the published `zero_rows` monitor of ONE Datastream.

    ONLY for a Datastream that can actually be evaluated -- one the ledger shows
    has completed at least one collection. That is 45 Datastreams of 1421 today,
    where every ENABLED one would be 894: a monitor for a stream that has never
    collected is a governed object that asserts nothing, and a registry full of
    those is how "watched and quiet" stops meaning anything.

    Returns ``{"monitor_id", "monitor_version_id"}``. The VERSION is returned and
    not only the head, because `app.dq_evaluations.monitor_version_id` is NOT NULL
    and the nightly sweep has to write an evaluation of its own.

    Its own connection, committed here: the nightly sweep shares one connection
    across every Datastream, and a governed write that aborted it would take the
    remaining streams down with it.
    """
    name = monitor_name(datastream_id)
    if not name:
        logger.warning("dq_zero_rows: unnameable_monitor ds=%s", datastream_id)
        return None
    if not (org_id and project_id and datastream_id):
        return None
    # Story 59.5: the monitor's display name comes from the ONE registry, and it is
    # the only monitor name that reaches a person (`RunAnomalies.tsx:237`).
    label = dq_monitor_registry.instance_label(CHECK_PROFILE, datastream_name or datastream_id)
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.dq_governance import ensure_monitor, publish_version  # noqa: PLC0415

        with get_connection() as conn:
            head = ensure_monitor(
                conn,
                org_id=org_id,
                project_id=project_id,
                name=name,
                label=label,
                target_kind="datastream",
                target_id=datastream_id,
                actor="system",
            )
            version = publish_version(
                conn,
                project_id=project_id,
                monitor_id=head["id"],
                check_profile=CHECK_PROFILE,
                severity=severity,
                actor="system",
                parameters={"thresholds": {CHECK_PROFILE: float(min_active_days())}},
                window_days=1,
            )
            conn.commit()
        return {"monitor_id": str(head["id"]), "monitor_version_id": str(version["id"])}
    except Exception as exc:  # noqa: BLE001 -- a monitor that cannot be derived is not a crash
        logger.warning("dq_zero_rows: monitor_underivable ds=%s: %s", datastream_id, exc)
        return None


#: `(total_eligible, passed, failed, unavailable)` per outcome, so that
#: `ck_dq_evaluations_counts` and `ck_dq_evaluations_empty_is_not_a_pass` are
#: satisfied by construction rather than by care at each call site. Shared shape
#: with `dq_null_rate._COUNTS_FOR_OUTCOME`, and the same reasons.
_COUNTS_FOR_OUTCOME = {
    "pass": (1, 1, 0, 0),
    "fail": (1, 0, 1, 0),
    "not_applicable": (0, 0, 0, 0),
    "unverifiable": (1, 0, 0, 1),
}


def record_verdict(
    *,
    project_id: str,
    monitor_id: str,
    monitor_version_id: str,
    datastream_id: str,
    execution_id: str | None,
    window_start: date,
    window_end: date,
    outcome: str,
    observed: Mapping[str, Any] | None = None,
    fired: bool = False,
    severity: str = DEFAULT_SEVERITY,
) -> dict[str, Any]:
    """ONE transaction: the night's evaluation, then the issue that points at it.

    THE EVALUATION IS WRITTEN ON EVERY NIGHT, NOT ONLY ON A FIRING.
    `app.dq_issues.execution_id` moves with `last_seen_at`, so the run that saw the
    anomaly the night before is overwritten; the append-only evaluation of that
    earlier window is the row that keeps it. Writing evidence solely on the nights
    that fired would leave the quiet nights unrecorded and the moved run
    unrecoverable.

    AT MOST ONE ISSUE, and that is arbitrage 7: the fingerprint is the monitor, so
    the second empty window of the same Datastream is a RECURRENCE that moves its
    run rather than a second row.

    The window is a pair and not a day: the pull that returned nothing covered 3 or
    5 days on 123 of 130 windows, and an evaluation reporting only its last day
    would describe a narrower measurement than the one taken.

    Both writes share one connection and one commit, and that connection is
    deliberately NOT the nightly sweep's shared one -- a governed write must never
    abort the loop still walking the other Datastreams.
    """
    result: dict[str, Any] = {"evaluation_id": None, "issues": []}
    if not (project_id and monitor_id and monitor_version_id and datastream_id):
        return result
    eligible, passed, failed, unavailable = _COUNTS_FOR_OUTCOME.get(outcome, (0, 0, 0, 0))
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.dq_governance import (  # noqa: PLC0415
            EvaluationCounts,
            open_issue,
            record_evaluation,
        )

        with get_connection() as conn:
            evaluation_id = record_evaluation(
                conn,
                project_id=project_id,
                monitor_id=monitor_id,
                monitor_version_id=monitor_version_id,
                outcome=outcome,
                window_start=window_start,
                window_end=window_end,
                counts=EvaluationCounts(
                    total_eligible=eligible,
                    evaluated=passed + failed,
                    passed=passed,
                    failed=failed,
                    unavailable=unavailable,
                ),
                dependency_refs={
                    "target": {"kind": "datastream", "id": datastream_id},
                    "check_profile": CHECK_PROFILE,
                },
                observed=dict(observed or {}),
                datastream_id=datastream_id,
                execution_id=execution_id,
            )
            result["evaluation_id"] = evaluation_id
            if fired:
                issue = open_issue(
                    conn,
                    project_id=project_id,
                    monitor_id=monitor_id,
                    root_cause_fingerprint=root_cause_fingerprint(monitor_id),
                    severity=severity,
                    actor="system",
                    evaluation_id=evaluation_id,
                    datastream_id=datastream_id,
                    execution_id=execution_id,
                )
                result["issues"].append(issue)
            conn.commit()
    except Exception as exc:  # noqa: BLE001 -- a firing must survive an unwritable verdict
        logger.warning("dq_zero_rows: verdict_unwritable ds=%s: %s", datastream_id, exc)
    return result
