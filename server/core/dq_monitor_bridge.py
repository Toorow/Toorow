"""From a DQ check's verdict to a governed object -- once, for every monitor.

Story 59.5. Stories 59.3 and 59.4 each bridged ONE check into
`app.dq_evaluations` / `app.dq_issues`, and each wrote its own `derive_monitor`
and its own `record_verdict`: the same forty lines, twice, differing only in a
label and a fingerprint. Story 59.5 bridges two more checks (`volume` and
`arrival_timeliness`), and writing them a third and a fourth time is how four
monitors end up with four opinions about what an evaluation of an
unmeasurable window looks like.

WHAT IS SHARED, AND WHY EACH PIECE HAS TO BE.

* **The name and the label** come from `core.dq_monitor_registry`, so a monitor
  cannot be created under a name one reader knows and another does not.
* **`_COUNTS_FOR_OUTCOME`** satisfies `ck_dq_evaluations_counts` and
  `ck_dq_evaluations_empty_is_not_a_pass` BY CONSTRUCTION rather than by care at
  each call site. `not_applicable` is the only zero denominator: nothing was
  eligible. `unverifiable` keeps its denominator at 1 -- the Datastream WAS
  eligible and the check could not be performed on it, which is a different
  sentence from "there was nothing to check".
* **One transaction, on its own connection.** The nightly sweep shares one
  connection across every Datastream; a governed write that aborted it would take
  the remaining streams down with it. And an issue whose event points at an
  evaluation that was rolled back is a dangling reference.
* **The evaluation is written on EVERY outcome, not only on a firing.**
  `app.dq_issues.execution_id` moves with `last_seen_at`, so the run that saw the
  anomaly the night before is overwritten; the append-only evaluation of that
  earlier window is the row that keeps it.

WHAT IS NOT SHARED. The fingerprint's EXTRA members: `null_rate` opens one issue
per field, the others one per Datastream. The caller names them, this module
hashes them.

`dq_null_rate` and `dq_zero_rows` keep their own copies of these two functions
for now -- they are covered by their own pg-gated bridge tests and story 59.5
does not re-open a proven path to save duplication. That residual is named in
the story's Completion Notes rather than left to be rediscovered.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Mapping

from core import dq_monitor_registry

logger = logging.getLogger(__name__)

#: A DQ finding breaks a day, never the load: the rows that are there stay
#: readable. `blocking` would halt work no DQ monitor has ever been allowed to
#: halt (`dq_monitors.py:4`, "never halt load in v1").
DEFAULT_SEVERITY = "degrading"

#: `(total_eligible, passed, failed, unavailable)` per outcome.
_COUNTS_FOR_OUTCOME = {
    "pass": (1, 1, 0, 0),
    "fail": (1, 0, 1, 0),
    "not_applicable": (0, 0, 0, 0),
    "unverifiable": (1, 0, 0, 1),
}


def root_cause_fingerprint(monitor_id: str, **members: Any) -> str:
    """`(monitor, *members)` -- what `uq_dq_issues_open_root_cause` binds on.

    The window date is deliberately NOT a member, for any monitor: adding it would
    open one issue per night and make that unique index bound nothing. Which run
    the issue speaks of is answered by `open_issue`, which moves the run with the
    sighting.
    """
    from core.governance_rule_sets import content_hash  # noqa: PLC0415

    payload: dict[str, str] = {"monitor_id": str(monitor_id)}
    for key in sorted(members):
        payload[str(key)] = str(members[key])
    return content_hash(payload)


def published_baseline(
    key: str, *, project_id: str, datastream_id: str
) -> tuple[dict[str, Any] | None, str | None]:
    """The frozen baseline of the published monitor of ONE Datastream, for ONE check.

    Answers ``(baseline, absence)``. Exactly one side is ever filled:

    * ``({...}, None)`` -- a published version exists and this is its baseline;
    * ``(None, "no_published_monitor")`` -- this Datastream carries no published
      monitor for this check, so nothing has ever been frozen for it;
    * ``(None, "governed_baseline_unreadable")`` -- the store could not be read.

    THE TWO ABSENCES ARE NOT THE SAME SENTENCE AND THE WHOLE FUNCTION EXISTS TO
    KEEP THEM APART. A caller that read a plain ``None`` would treat an
    unreadable store as "nothing frozen yet" and freeze whatever the source looks
    like tonight -- which is the auto-reset Story 49.4 removed, coming back
    through a failed query instead of through a successful drift.

    IT EXISTS SO A CHECK NEEDING A REFERENCE HAS ONE PLACE TO READ IT. `schema`
    kept its own answer -- `app.dq_baselines`, one mutable row per Datastream,
    keyed on the Datastream alone with no monitor, no version and no decision
    date. Migration 145 refused to apply while that table held a single row, on
    the ground that adopting such a row would invent the governed identity it
    never had; the nightly sweep then re-created exactly those rows every night.
    A store the migration declared un-adoptable and the runtime kept refilling is
    a parallel evidence store, whatever the layer it is called.

    The lookup is by the registry's NAME for (key, Datastream), the same name
    :func:`derive_monitor` creates under, so the reader and the writer cannot
    disagree about which monitor answers for a check.
    """
    name = dq_monitor_registry.monitor_name(key, datastream_id)
    if not (name and project_id and datastream_id):
        return None, "no_published_monitor"
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT v.baseline FROM app.dq_monitors m "
                    "JOIN app.dq_monitor_versions v ON v.id = m.current_version_id "
                    "WHERE m.project_id = %s AND m.name = %s "
                    "AND m.lifecycle_status = 'published' AND v.status = 'published'",
                    (project_id, name),
                )
                row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "dq_monitor_bridge: published_baseline_unreadable key=%s ds=%s: %s",
            key,
            datastream_id,
            exc,
        )
        return None, "governed_baseline_unreadable"
    if row is None:
        return None, "no_published_monitor"
    return dict(row[0] or {}), None


def derive_monitor(
    key: str,
    *,
    org_id: str,
    project_id: str,
    datastream_id: str,
    datastream_name: str,
    parameters: Mapping[str, Any] | None = None,
    baseline: Mapping[str, Any] | None = None,
    severity: str = DEFAULT_SEVERITY,
    window_days: int = 1,
) -> dict[str, str] | None:
    """Get-or-create the published monitor of ONE Datastream, for ONE check.

    Only for a Datastream that can actually be evaluated -- the caller decides
    what that means for its check and calls this only then. A monitor that could
    never evaluate anything is an empty governed object, and a registry full of
    those is how "watched and quiet" stops meaning anything.

    Returns ``{"monitor_id", "monitor_version_id"}``. The VERSION is returned and
    not only the head, because `app.dq_evaluations.monitor_version_id` is NOT NULL.

    Never raises: a monitor that cannot be derived is not a crash, and the check
    that called it still has its own answer to give.
    """
    entry = dq_monitor_registry.BY_KEY.get(key)
    if entry is None:
        logger.warning("dq_monitor_bridge: unknown_check_profile key=%s", key)
        return None
    if entry.target_kind != dq_monitor_registry.TARGET_DATASTREAM:
        # `app.dq_monitors.target_kind` has no `project` value (migration
        # 145:306-307). Naming the scope is the registry's job; pretending the
        # column can hold it would be a fabricated governed object.
        logger.debug("dq_monitor_bridge: not_datastream_scoped key=%s", key)
        return None
    profile = dq_monitor_registry.published_profile(key)
    if not profile:
        # Nothing a published version could legally carry: a monitor that cannot
        # be published cannot be governed, and pretending otherwise would write a
        # version `publish_version` refuses anyway.
        logger.warning("dq_monitor_bridge: unpublishable_check key=%s", key)
        return None
    name = dq_monitor_registry.monitor_name(key, datastream_id)
    if not name:
        logger.warning("dq_monitor_bridge: unnameable_monitor key=%s ds=%s", key, datastream_id)
        return None
    if not (org_id and project_id and datastream_id):
        return None
    label = dq_monitor_registry.instance_label(key, datastream_name or datastream_id)
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
                target_kind=dq_monitor_registry.TARGET_DATASTREAM,
                target_id=datastream_id,
                actor="system",
            )
            version = publish_version(
                conn,
                project_id=project_id,
                monitor_id=head["id"],
                check_profile=profile,
                severity=severity,
                actor="system",
                parameters=dict(parameters or {}),
                # `volume` and `schema` are refused without one
                # (`controls_quality.DQ_CHECKS_REQUIRING_BASELINE`): a check whose
                # verdict needs a reference cannot be published without freezing it.
                baseline=dict(baseline or {}),
                window_days=window_days,
            )
            conn.commit()
        return {"monitor_id": str(head["id"]), "monitor_version_id": str(version["id"])}
    except Exception as exc:  # noqa: BLE001 -- a monitor that cannot be derived is not a crash
        logger.warning(
            "dq_monitor_bridge: monitor_underivable key=%s ds=%s: %s", key, datastream_id, exc
        )
        return None


def record_verdict(
    key: str,
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
    fingerprint_members: Mapping[str, Any] | None = None,
    severity: str = DEFAULT_SEVERITY,
) -> dict[str, Any]:
    """ONE transaction: the night's evaluation, then the issue that points at it.

    At most ONE issue per call: the fingerprint is the monitor plus whatever the
    caller adds, so the second bad window of the same Datastream is a RECURRENCE
    that moves its run rather than a second row.

    Never raises: a firing must survive an unwritable verdict. A `world A` alert
    that vanished because a governance write failed would be worse than the
    missing evidence.
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
                    # The profile the VERSION carries, and the check that actually
                    # ran. They differ for `arrival_timeliness`, governed as
                    # `timeliness`, and an evidence row that named only one of the
                    # two could not say which of the two branches spoke.
                    "check_profile": dq_monitor_registry.published_profile(key) or key,
                    "monitor_key": key,
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
                    root_cause_fingerprint=root_cause_fingerprint(
                        monitor_id, **dict(fingerprint_members or {})
                    ),
                    severity=severity,
                    actor="system",
                    evaluation_id=evaluation_id,
                    datastream_id=datastream_id,
                    execution_id=execution_id,
                )
                result["issues"].append(issue)
            conn.commit()
    except Exception as exc:  # noqa: BLE001 -- a firing must survive an unwritable verdict
        logger.warning(
            "dq_monitor_bridge: verdict_unwritable key=%s ds=%s: %s", key, datastream_id, exc
        )
    return result
