"""The null rate of a REQUIRED field, and the bridge from a firing to governance.

Story 59.3, epic 59. Two things live here because they are one gesture: measuring
the rate, and making the anomaly an object the console can acknowledge instead of
a sentence parsed out of an alert message.

WHAT "REQUIRED" MEANS, AND THE TWO CANDIDATES THAT WERE REFUSED. A required field
is a member of the active mapping version's ``grain`` -- the list of fields the
rows are keyed by -- and nothing else.

* ``fields[].profile.nullable`` was refused because it is an OBSERVATION, not a
  decision: ``datastream_field_mapping`` sets it from a discovery sample, so a
  field that happened to be non-null five times would become "required" without
  anybody deciding it, and this monitor would then fire on that accident. The
  noise epic 59 exists to avoid is exactly a threshold applied to a field nobody
  declared.
* ``parameters.required_fields`` was refused because it has no writer anywhere in
  the repository. A rule read from a key nothing fills is a rule that never applies.

The ``grain`` is declared, chosen by a person, and already read by a delivered
screen (``datastream_daily_breakdown_api``'s ``is_key_column``). A mapping version
with an empty grain therefore answers ``not_applicable``, honestly, and that was
390 of the 681 versions when this was written.

NULL IS NOT THE ONLY SPELLING OF ABSENT, and the deployment is what said so.
Measured on 2026-08-12 on a live Datastream whose grain is
``(channel_id, date, video)``: **602 rows over 2026-07-13 → 2026-08-10, 0 null on
``video`` and 602 missing** -- every landed row carried ``''``. This monitor
answered ``0 / 602`` and passed, on the exact column somebody opened the console
to ask about. What is judged is therefore the MISSING rate -- NULL or blank -- and
the two components stay published apart (``null_counts``, ``blank_counts``)
because a null comes from the source and a blank usually comes from what the
extraction wrote in its place: two gestures, two places.

WHERE THE RATE IS TAKEN. Through ``collected_mapped_reader`` -- one
``describe_relation`` and one aggregate -- and never through a hand-rolled DuckDB
connection. See ``collected_mapped_reader.count_null_rows`` for the whole reason,
and ``_missing_sql`` for the single predicate the count and the replay share.

WHAT THE FIRING CARRIES. The field, the counts, the rate, the threshold, the
window and the run. NEVER the faulty rows: they are replayed on demand (Jean,
2026-08-07), which is what migration 145 already wrote of ``observed`` --
"bounded, masked, and NOT the rows themselves".

Nothing in this module raises. Every entry point answers with a named absence,
because a DQ monitor that takes a nightly sweep down with it is worse than a
monitor that says it could not measure.
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
#: `dq_monitors.CHECK_PROFILES` (what can be evaluated) -- because a monitor in
#: one and not the other is either unpublishable or `unverifiable`.
CHECK_PROFILE = "null_rate"

#: The world-A firing type. Same family as its six siblings, so one alert list
#: reads them all.
ALERT_TYPE = "dq_null_rate"

#: A missing key breaks the grain rather than the whole load: the day is still
#: readable, its rows just cannot be keyed. `blocking` would halt work this
#: monitor has never been allowed to halt.
DEFAULT_SEVERITY = "degrading"

#: The rate above which a field fires. `0` -- any NULL in a key column is an
#: anomaly, exactly as `DQ_REJECTED_ROWS_THRESHOLD` defaults to firing on any
#: rejection. A governed version may raise it through `parameters.thresholds`.
THRESHOLD_ENV = "DQ_NULL_RATE_THRESHOLD"
DEFAULT_THRESHOLD = 0.0

#: The three things a measurement can be, and they are three because two of them
#: must never render as a pass.
STATUS_MEASURED = "measured"
STATUS_NOT_APPLICABLE = "not_applicable"
STATUS_UNAVAILABLE = "unavailable"

#: Why a measurement could not be taken, or measured nothing.
NO_REQUIRED_FIELD = "no_required_field"
NO_ELIGIBLE_ROW = "no_eligible_row"
NO_MEASURABLE_FIELD = "no_measurable_field"

_MESSAGES = {
    NO_REQUIRED_FIELD: (
        "No field of this Datastream is declared required: the active mapping version "
        "carries an empty grain, so there is no key column whose absence would be an "
        "anomaly."
    ),
    NO_ELIGIBLE_ROW: (
        "No row of this Datastream covers this day, so no rate could be taken. An "
        "empty window is a subject of its own and never a pass."
    ),
    NO_MEASURABLE_FIELD: (
        "None of the required fields is a column of this relation, so nothing could be "
        "counted. A column that is not there is not a column that is never null."
    ),
}

def message_for(reason: str | None) -> str | None:
    """The sentence of a reason of this module, or `None`."""
    if not reason:
        return None
    return _MESSAGES.get(reason)


def threshold_from_env() -> float:
    """The firing threshold, read at call time so a test can move it."""
    try:
        return max(0.0, float(os.environ.get(THRESHOLD_ENV, DEFAULT_THRESHOLD)))
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD


# ---------------------------------------------------------------------------
# What is required.
# ---------------------------------------------------------------------------


def required_fields(payload: Mapping[str, Any] | None) -> list[str]:
    """The grain of a mapping payload, in its declared order, deduplicated.

    Read exactly as `datastream_daily_breakdown_api._versioned_columns` reads it,
    so "key column" means one thing in the product and not two.
    """
    if not isinstance(payload, Mapping):
        return []
    grain = payload.get("grain")
    if not isinstance(grain, (list, tuple)):
        return []
    fields: list[str] = []
    for entry in grain:
        if not isinstance(entry, (str, int)):
            continue
        name = str(entry).strip()
        if name and name not in fields:
            fields.append(name)
    return fields


_ACTIVE_MAPPING_SQL = """
    SELECT v.mapping_payload
    FROM app.datastream_mapping_versions v
    JOIN app.datastreams d
      ON d.id = v.datastream_id AND d.project_id = v.project_id
    WHERE d.id = %s AND d.project_id = %s AND v.id = d.current_mapping_version_id
"""


def read_required_fields(conn, *, project_id: str, datastream_id: str) -> list[str]:
    """The required fields of the CURRENT mapping version, or `[]`.

    Strictly the current version, never the highest version number: a version that
    is not current is not the one this Datastream is collecting against, and
    judging today's rows by a mapping nobody activated would fire on a decision
    that was never taken.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(_ACTIVE_MAPPING_SQL, (datastream_id, project_id))
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 -- unreadable is not "nothing required"
        logger.warning(
            "dq_null_rate: mapping_unreadable ds=%s: %s", datastream_id, exc
        )
        return []
    if row is None:
        return []
    return required_fields(_payload_of(row[0]))


def _payload_of(value: Any) -> dict[str, Any]:
    """A `jsonb` column, whichever way the driver handed it over."""
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        import json  # noqa: PLC0415

        try:
            decoded = json.loads(value)
        except ValueError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


# ---------------------------------------------------------------------------
# Where the rows are, and what they hold.
# ---------------------------------------------------------------------------


def resolve_collected_relation(
    *, connector: str | None, report_profile_id: str | None
) -> dict[str, Any]:
    """The `Collected` relation of a Datastream, or the resolver's named absence.

    Never a composed name: the address comes from the manifest of the module that
    lands the rows, and a Datastream that names no report profile has no address
    at all rather than the address of the profile beside it.
    """
    from core import stage_relation_resolver  # noqa: PLC0415

    try:
        pair = stage_relation_resolver.resolve_stage_relations(
            connector=connector, report_profile_id=report_profile_id
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("dq_null_rate: relation_unresolvable connector=%s: %s", connector, exc)
        return {"relation": None, "reason": "relation_unresolvable", "message": None}
    return {
        "relation": pair.get("collected_relation"),
        "reason": pair.get("reason"),
        "message": pair.get("message"),
    }


def measure_null_rates(
    *,
    project_id: str,
    relation: str,
    fields: Sequence[str],
    window_date: date,
) -> dict[str, Any]:
    """The rate of each required field over ONE day, or the reason there is none.

    Three answers and never two: `measured` with its counts, `not_applicable` when
    the window holds no eligible row or no required field is a column of the
    relation, and `unavailable` when the relation cannot be read -- naming the
    relation. `unavailable` is never rendered as a pass; that confusion is the one
    the governed outcomes exist to remove.
    """
    from core import collected_mapped_reader  # noqa: PLC0415

    day = window_date.isoformat()
    wanted = [str(field).strip() for field in fields if str(field).strip()]
    if not wanted:
        return _unmeasured(STATUS_NOT_APPLICABLE, NO_REQUIRED_FIELD, relation)

    try:
        description = collected_mapped_reader.describe_relation(
            project_id=project_id,
            relation=relation,
            zone=collected_mapped_reader.ZONE_COLLECTED,
        )
    except Exception as exc:  # noqa: BLE001 -- never raises out of a monitor
        logger.warning("dq_null_rate: describe_failed relation=%s: %s", relation, exc)
        return _unmeasured(STATUS_UNAVAILABLE, "relation_undescribable", relation)

    if not description.get("readable"):
        return _unmeasured(
            STATUS_UNAVAILABLE,
            str(description.get("reason") or "relation_unreadable"),
            relation,
            message=description.get("message"),
        )

    try:
        counted = collected_mapped_reader.count_null_rows(
            description=description,
            project_id=project_id,
            start=day,
            end=day,
            fields=wanted,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("dq_null_rate: count_failed relation=%s: %s", relation, exc)
        return _unmeasured(STATUS_UNAVAILABLE, "relation_uncountable", relation)

    if not counted.get("readable"):
        return _unmeasured(
            STATUS_UNAVAILABLE,
            str(counted.get("reason") or "relation_unreadable"),
            relation,
            message=counted.get("message"),
        )

    measured_fields = list(counted.get("measured_fields") or [])
    absent_fields = list(counted.get("absent_fields") or [])
    row_count = int(counted.get("row_count") or 0)

    if not measured_fields:
        result = _unmeasured(STATUS_NOT_APPLICABLE, NO_MEASURABLE_FIELD, relation)
        result["absent_fields"] = absent_fields
        result["row_count"] = row_count
        return result
    if row_count == 0:
        # NOT a pass. `ck_dq_evaluations_empty_is_not_a_pass` says the same thing
        # one layer down, and story 59.4 owns the empty window itself.
        result = _unmeasured(STATUS_NOT_APPLICABLE, NO_ELIGIBLE_ROW, relation)
        result["absent_fields"] = absent_fields
        result["measured_fields"] = measured_fields
        result["row_count"] = 0
        return result

    null_counts = {
        field: int((counted.get("null_counts") or {}).get(field) or 0)
        for field in measured_fields
    }
    blank_counts = {
        field: int((counted.get("blank_counts") or {}).get(field) or 0)
        for field in measured_fields
    }
    missing_counts = {
        field: int(
            (counted.get("missing_counts") or {}).get(field, null_counts[field]) or 0
        )
        for field in measured_fields
    }
    return {
        "status": STATUS_MEASURED,
        "relation": relation,
        "reason": None,
        "message": None,
        "row_count": row_count,
        "null_counts": null_counts,
        "blank_counts": blank_counts,
        "missing_counts": missing_counts,
        "null_rates": {
            field: count / row_count for field, count in null_counts.items()
        },
        # What the monitor JUDGES. The two components stay published beside it so a
        # reader can tell a column the provider omits from one the extraction
        # blanked -- they are repaired in different places.
        "missing_rates": {
            field: count / row_count for field, count in missing_counts.items()
        },
        "measured_fields": measured_fields,
        "absent_fields": absent_fields,
    }


def _unmeasured(
    status: str, reason: str, relation: str | None, message: str | None = None
) -> dict[str, Any]:
    return {
        "status": status,
        "relation": relation,
        "reason": reason,
        "message": message or message_for(reason),
        "row_count": None,
        "null_counts": {},
        "blank_counts": {},
        "missing_counts": {},
        "null_rates": {},
        "missing_rates": {},
        "measured_fields": [],
        "absent_fields": [],
    }


def findings_over_threshold(
    measurement: Mapping[str, Any], threshold: float
) -> list[dict[str, Any]]:
    """One finding per field whose MISSING rate exceeds *threshold*, in field order.

    The judged rate is `missing` -- NULL or blank -- and not `null` alone. A live
    Datastream measured on 2026-08-12 held 602 rows whose `video` key was `''` on
    every one of them: judging nulls answered `0 / 602` and passed, on the very
    column somebody was asking about. Both components travel on the finding, so a
    firing can say "0 null, 602 blank" rather than a single number that hides
    which of the two gestures repairs it.
    """
    if measurement.get("status") != STATUS_MEASURED:
        return []
    row_count = int(measurement.get("row_count") or 0)
    findings: list[dict[str, Any]] = []
    for field in measurement.get("measured_fields") or []:
        null_count = int((measurement.get("null_counts") or {}).get(field) or 0)
        missing_count = int(
            (measurement.get("missing_counts") or {}).get(field, null_count) or 0
        )
        rate = float(
            (measurement.get("missing_rates") or {}).get(
                field, (measurement.get("null_rates") or {}).get(field) or 0.0
            )
            or 0.0
        )
        if rate <= threshold:
            continue
        findings.append(
            {
                "field": field,
                # `null_count` / `null_rate` keep their names and their meaning:
                # they are the NULL half, and every reader that already prints them
                # keeps printing the same number it printed before.
                "null_count": null_count,
                "blank_count": max(0, missing_count - null_count),
                "missing_count": missing_count,
                "row_count": row_count,
                "null_rate": float((measurement.get("null_rates") or {}).get(field) or 0.0),
                "missing_rate": rate,
                "threshold": threshold,
            }
        )
    return findings


# ---------------------------------------------------------------------------
# Which run a day belongs to.
# ---------------------------------------------------------------------------

def resolve_execution_id(
    conn, *, project_id: str, datastream_id: str, window_date: date
) -> str | None:
    """The run the LEDGER attributes *window_date* to, or `None`.

    REALIGNED BY STORY 59.4, ARBITRAGE 4, and the realignment is the point. This
    function used to carry its own query, ordered `completed_at DESC NULLS LAST,
    id DESC`. That column is populated on preprod (6 rows of 6) and NULL on the
    whole disposable base (130 of 130), so on the base its own tests ran against
    the order degenerated to `id DESC`: the test proved less than it appeared to,
    and -- worse -- story 59.4's zero-row monitor reads the run straight off the
    ledger entry it already holds, which orders by `enqueued_at DESC`. Two
    monitors of one epic answering "which run saw this" differently is the next
    divergence story 59.1 would have to read through.

    One resolver, in :func:`core.extract_ledger.execution_for_day`, which is the
    order every screen already displays.

    `project_id` is kept in the signature and is the caller's scope statement: the
    ledger addresses a Datastream by its own globally unique id, and every caller
    here obtained that id from a project-scoped read.

    `None` is a real answer and is written as NULL: `app.pull_jobs.execution_id`
    was NULL on 130 rows of 130 (disposable base) and 6 of 6 (preprod) when this
    was written -- the column has been filled by `queue.py` since story 63.1 and
    those rows are simply older. Inventing an id to fill the column would put an
    anomaly under a collection that never produced it.
    """
    from core.extract_ledger import execution_for_day  # noqa: PLC0415

    return execution_for_day(conn, datastream_id, window_date)


# ---------------------------------------------------------------------------
# The governed object, and the issue.
# ---------------------------------------------------------------------------


def monitor_name(datastream_id: str) -> str | None:
    """`null_rate_<datastream>` folded to the name CHECK of migration 145:299.

    Minted by the ONE registry since story 59.5: four monitors now derive a
    governed object, and four private naming schemes is how two of them end up
    watching the same Datastream under two names.
    """
    return dq_monitor_registry.monitor_name(CHECK_PROFILE, datastream_id)


def root_cause_fingerprint(monitor_id: str, field: str) -> str:
    """`(monitor, field)` -- one issue per field, and one per field only.

    Adding the window date would open a new issue every night and make
    `uq_dq_issues_open_root_cause` a constraint that bounds nothing. Which run the
    issue speaks of is answered by `open_issue`, which moves the run with the
    sighting.
    """
    from core.governance_rule_sets import content_hash  # noqa: PLC0415

    return content_hash({"monitor_id": str(monitor_id), "field": str(field)})


def derive_monitor(
    *,
    org_id: str,
    project_id: str,
    datastream_id: str,
    datastream_name: str,
    threshold: float,
    severity: str = DEFAULT_SEVERITY,
) -> dict[str, str] | None:
    """Get-or-create the published `null_rate` monitor of ONE Datastream.

    Returns ``{"monitor_id", "monitor_version_id"}``. The VERSION is returned and
    not only the head, because the nightly sweep has to write an evaluation of its
    own and ``app.dq_evaluations.monitor_version_id`` is NOT NULL: without it the
    only evidence row this monitor could ever produce would be the manual route's,
    and the issue's moving run would have no survivor.

    ONLY for a Datastream that can actually be evaluated -- one whose active
    mapping version declares a grain. Deriving one for every enabled Datastream
    would have created up to 894 governed objects of which 682 could never
    evaluate anything, which is filling a governance registry with objects that
    assert nothing.

    No change set is needed and none is possible: `ensure_monitor` is a
    get-or-create on `(project_id, name)`, and
    `controls_owner_commands._apply_dq_monitor` refuses a change set that does not
    name a monitor that ALREADY exists. Creation had no other door.

    Its own connection, committed here: the nightly sweep shares one connection
    across every Datastream, and a governed write that aborted it would take the
    remaining streams down with it.
    """
    name = monitor_name(datastream_id)
    if not name:
        logger.warning("dq_null_rate: unnameable_monitor ds=%s", datastream_id)
        return None
    if not (org_id and project_id and datastream_id):
        return None
    # Story 59.5: the monitor's display name comes from the ONE registry. This is
    # the only monitor name that reaches a person -- `dq_issue_rows.py:324` selects
    # it as `monitor_label` and `RunAnomalies.tsx:237` renders it.
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
                parameters={"thresholds": {CHECK_PROFILE: threshold}},
                window_days=1,
            )
            conn.commit()
        return {"monitor_id": str(head["id"]), "monitor_version_id": str(version["id"])}
    except Exception as exc:  # noqa: BLE001 -- a monitor that cannot be derived is not a crash
        logger.warning("dq_null_rate: monitor_underivable ds=%s: %s", datastream_id, exc)
        return None


#: `(total_eligible, passed, failed, unavailable)` per outcome, so that
#: `ck_dq_evaluations_counts` (`passed + failed + unavailable <= total_eligible`)
#: and `ck_dq_evaluations_empty_is_not_a_pass` are satisfied by construction
#: rather than by care at each call site.
#:
#: `not_applicable` is the only zero denominator: nothing was eligible, so nothing
#: can be counted under it. `unverifiable` keeps its denominator at 1 -- the
#: Datastream WAS eligible and the check could not be performed on it, which is a
#: different sentence from "there was nothing to check".
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
    window_date: date,
    outcome: str,
    observed: Mapping[str, Any] | None = None,
    findings: Sequence[Mapping[str, Any]] = (),
    severity: str = DEFAULT_SEVERITY,
) -> dict[str, Any]:
    """ONE transaction: the night's evaluation, then the issues that point at it.

    THE EVALUATION IS WRITTEN ON EVERY NIGHT, NOT ONLY ON A FIRING, and that is
    the point. `app.dq_issues.execution_id` moves with `last_seen_at`, so the run
    that saw the anomaly the night before is overwritten; the append-only
    evaluation of that earlier window is the row that keeps it. Before this
    function, `record_evaluation` was reachable only from the manual route
    (`controls_quality_api`), so the nightly path produced no evidence at all and
    the overwrite destroyed the earlier run with nothing left behind.

    The `evaluation_id` then travels into `open_issue`, which writes it on the
    `observed` event -- `app.dq_issue_events` has no run column of its own, so
    that reference is the only thing tying a sighting to the run that made it.

    Both writes share one connection and one commit: an issue whose event points
    at an evaluation that was rolled back would be a dangling reference, and this
    connection is deliberately NOT the nightly sweep's shared one -- a governed
    write must never abort the loop that is still walking the other Datastreams.
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
                window_start=window_date,
                window_end=window_date,
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
            for finding in findings:
                issue = open_issue(
                    conn,
                    project_id=project_id,
                    monitor_id=monitor_id,
                    root_cause_fingerprint=root_cause_fingerprint(
                        monitor_id, str(finding.get("field"))
                    ),
                    severity=severity,
                    actor="system",
                    evaluation_id=evaluation_id,
                    datastream_id=datastream_id,
                    execution_id=execution_id,
                )
                result["issues"].append({"field": str(finding.get("field")), **issue})
            conn.commit()
    except Exception as exc:  # noqa: BLE001 -- a firing must survive an unwritable verdict
        logger.warning("dq_null_rate: verdict_unwritable ds=%s: %s", datastream_id, exc)
    return result
