"""Governed DQ: stable monitors, immutable evaluations, stable issues.

Story 49.4 AC5 and AC6. Three defects are removed here, and each was a way for a
quality surface to look healthy while measuring nothing.

**The baseline advanced itself.** ``dq_monitors._write_dq_baseline`` OVERWROTE the
stored column set after detecting a schema drift, so the next run compared the
source against what the source had just become and passed. A drift check that
agrees with the drift is not a check. The baseline now lives inside an immutable
``dq_monitor_versions`` row: advancing it is publishing a new version, with a date
and an actor, and :func:`propose_baseline_change` returns a candidate rather than
performing the change.

**Health was inferred from alerts.** ``dq_api`` derived state from alert firings
and pull days, so "ran and passed" and "never ran" were the same absence of an
alert. :data:`OUTCOMES` makes them different immutable rows, and the database
refuses to store a pass over zero eligible members at all
(``ck_dq_evaluations_empty_is_not_a_pass``).

**Issue identity was parsed out of a message.** An incident that exists only as
prose cannot be acknowledged twice, cannot be reopened, and vanishes when the
wording changes. An issue here has a stable id and a root-cause fingerprint, and
every workflow action is an append-only event with an actor and a reason.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence

from core.governance_rule_sets import canonical_json, content_hash

logger = logging.getLogger(__name__)

OUTCOME_PASS = "pass"
OUTCOME_FAIL = "fail"
OUTCOME_ERROR = "error"
OUTCOME_UNVERIFIABLE = "unverifiable"
OUTCOME_NOT_APPLICABLE = "not_applicable"
OUTCOMES = (
    OUTCOME_PASS,
    OUTCOME_FAIL,
    OUTCOME_ERROR,
    OUTCOME_UNVERIFIABLE,
    OUTCOME_NOT_APPLICABLE,
)

#: Outcomes that do NOT mean "this target is fine". Kept as a set because the
#: distinction is asserted in three places and getting it wrong in one of them is
#: how `unavailable` starts rendering as `healthy`.
NOT_HEALTHY = frozenset({OUTCOME_FAIL, OUTCOME_ERROR, OUTCOME_UNVERIFIABLE})

RUNTIME_STATES = (
    "healthy",
    "degraded",
    "failing",
    "unavailable",
    "not_applicable",
    "paused",
)

TARGET_KINDS = ("datastream", "output", "semantic_concept", "semantic_view")

EVALUATOR_VERSION = "dq-evaluator-1"


class DqGovernanceError(ValueError):
    """A governed DQ operation was rejected."""

    code = "invalid_dq_operation"


class DqMonitorNotFound(DqGovernanceError):
    code = "dq_monitor_not_found"


def _require(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DqGovernanceError(f"{label} is required")
    return value.strip()


def _mint(prefix: str) -> str:
    from ulid import ULID  # noqa: PLC0415

    return f"{prefix}_{ULID()}"


# ---------------------------------------------------------------------------
# Monitors and their immutable versions.
# ---------------------------------------------------------------------------


def ensure_monitor(
    conn,
    *,
    org_id: str,
    project_id: str,
    name: str,
    label: str,
    target_kind: str,
    target_id: str,
    actor: str,
) -> dict[str, Any]:
    """Return the (Project, name) monitor head, creating a draft one if absent."""

    if target_kind not in TARGET_KINDS:
        raise DqGovernanceError(f"target_kind must be one of {list(TARGET_KINDS)}")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, target_kind, target_id, lifecycle_status, runtime_state, "
            "current_version_id, pending_version_id, last_known_good_version_id "
            "FROM app.dq_monitors WHERE project_id = %s AND name = %s "
            "AND lifecycle_status <> 'archived'",
            (project_id, _require(name, "name")),
        )
        row = cur.fetchone()
        if row is not None:
            return _monitor(row)
        monitor_id = _mint("dqm")
        cur.execute(
            """
            INSERT INTO app.dq_monitors
                (id, org_id, project_id, name, label, target_kind, target_id, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, name, target_kind, target_id, lifecycle_status, runtime_state,
                      current_version_id, pending_version_id, last_known_good_version_id
            """,
            (
                monitor_id,
                _require(org_id, "org_id"),
                _require(project_id, "project_id"),
                name.strip(),
                _require(label, "label"),
                target_kind,
                _require(target_id, "target_id"),
                _require(actor, "actor"),
            ),
        )
        return _monitor(cur.fetchone())


_MONITOR_COLUMNS = (
    "id",
    "name",
    "target_kind",
    "target_id",
    "lifecycle_status",
    "runtime_state",
    "current_version_id",
    "pending_version_id",
    "last_known_good_version_id",
)


def _monitor(row: Sequence[Any]) -> dict[str, Any]:
    return dict(zip(_MONITOR_COLUMNS, row))


def publish_version(
    conn,
    *,
    project_id: str,
    monitor_id: str,
    check_profile: str,
    severity: str,
    actor: str,
    baseline: Mapping[str, Any] | None = None,
    parameters: Mapping[str, Any] | None = None,
    schedule: Mapping[str, Any] | None = None,
    window_days: int = 1,
    applicability: Mapping[str, Any] | None = None,
    target_selectors: Mapping[str, Any] | None = None,
    rule_set_id: str | None = None,
    rule_set_version_id: str | None = None,
) -> dict[str, Any]:
    """Freeze a monitor version and make it current. Idempotent by content.

    Ordering is the same as every other version pointer in this repository and it
    matters for the same reason: ``last_known_good`` is set from what was current
    BEFORE this call, so a later rollback lands on something that worked rather
    than on the version that just replaced it.
    """

    from core.controls_quality import PROFILE_DQ  # noqa: PLC0415
    from core.governance_rule_sets import get_profile  # noqa: PLC0415

    # The payload is validated by the SAME profile a Rule Set version would use,
    # so a monitor and a published DQ policy cannot disagree about what a legal
    # check is.
    normalized = get_profile(PROFILE_DQ).validate(
        {
            "check": check_profile,
            "severity": severity,
            "window_days": window_days,
            "baseline": dict(baseline or {}),
            "thresholds": dict((parameters or {}).get("thresholds") or {}),
        }
    )

    digest = content_hash(
        {
            "monitor_id": monitor_id,
            "profile": normalized["check"],
            "parameters": dict(parameters or {}),
            "baseline": normalized["baseline"],
            "schedule": dict(schedule or {}),
            "window_days": normalized["window_days"],
            "severity": normalized["severity"],
            "applicability": dict(applicability or {}),
            "target_selectors": dict(target_selectors or {}),
            "rule_set_version_id": rule_set_version_id,
        }
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, status FROM app.dq_monitor_versions "
            "WHERE monitor_id = %s AND content_hash = %s",
            (monitor_id, digest),
        )
        existing = cur.fetchone()
        if existing and existing[1] == "published":
            return {"id": str(existing[0]), "replayed": True}

        cur.execute(
            "SELECT current_version_id FROM app.dq_monitors WHERE id = %s AND project_id = %s",
            (monitor_id, project_id),
        )
        head = cur.fetchone()
        if head is None:
            raise DqMonitorNotFound(f"monitor {monitor_id} is not in this Project")
        previous = head[0]

        if existing:
            version_id = str(existing[0])
            cur.execute(
                "UPDATE app.dq_monitor_versions SET status = 'published' WHERE id = %s",
                (version_id,),
            )
        else:
            cur.execute(
                "SELECT COALESCE(MAX(version_number), 0) + 1 FROM app.dq_monitor_versions "
                "WHERE monitor_id = %s",
                (monitor_id,),
            )
            version_number = int(cur.fetchone()[0])
            version_id = _mint("dqmv")
            cur.execute(
                """
                INSERT INTO app.dq_monitor_versions
                    (id, monitor_id, project_id, version_number, status, rule_set_id,
                     rule_set_version_id, check_profile, parameters, baseline, schedule,
                     window_days, severity, applicability, target_selectors, content_hash,
                     created_by)
                VALUES (%s, %s, %s, %s, 'published', %s, %s, %s, %s::jsonb, %s::jsonb,
                        %s::jsonb, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
                """,
                (
                    version_id,
                    monitor_id,
                    project_id,
                    version_number,
                    rule_set_id,
                    rule_set_version_id,
                    normalized["check"],
                    canonical_json(dict(parameters or {})),
                    canonical_json(normalized["baseline"]),
                    canonical_json(dict(schedule or {})),
                    normalized["window_days"],
                    normalized["severity"],
                    canonical_json(dict(applicability or {})),
                    canonical_json(dict(target_selectors or {})),
                    digest,
                    _require(actor, "actor"),
                ),
            )

        if previous and previous != version_id:
            cur.execute(
                "UPDATE app.dq_monitor_versions SET status = 'superseded' "
                "WHERE id = %s AND status = 'published'",
                (previous,),
            )
        cur.execute(
            """
            UPDATE app.dq_monitors
            SET current_version_id = %s,
                last_known_good_version_id = COALESCE(%s, %s),
                pending_version_id = NULL,
                lifecycle_status = 'published',
                updated_at = NOW()
            WHERE id = %s AND project_id = %s
            """,
            (version_id, previous, version_id, monitor_id, project_id),
        )
    return {"id": version_id, "replayed": False}


def propose_baseline_change(
    conn, *, project_id: str, monitor_id: str, observed_baseline: Mapping[str, Any]
) -> dict[str, Any]:
    """Describe the baseline change a drift implies. Performs NOTHING.

    This is the replacement for ``_write_dq_baseline``, and the difference is the
    whole point: that function silently made the drift the new truth. This one
    returns the proposed version content so a governed decision can accept it,
    and until someone does, the monitor keeps failing against the frozen baseline.
    """

    with conn.cursor() as cur:
        cur.execute(
            "SELECT v.id, v.baseline, v.check_profile FROM app.dq_monitors m "
            "JOIN app.dq_monitor_versions v ON v.id = m.current_version_id "
            "WHERE m.id = %s AND m.project_id = %s",
            (monitor_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise DqMonitorNotFound(f"monitor {monitor_id} has no published version")
    current_version_id, current_baseline, check_profile = row
    return {
        "monitor_id": monitor_id,
        "current_version_id": str(current_version_id),
        "check_profile": str(check_profile),
        "current_baseline": current_baseline or {},
        "proposed_baseline": dict(observed_baseline),
        "applied": False,
        "reason": (
            "A baseline change is a governed decision. Until a new version is published, "
            "this monitor keeps failing against the frozen baseline rather than agreeing "
            "with the drift it just detected."
        ),
    }


# ---------------------------------------------------------------------------
# Evaluations.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvaluationCounts:
    """The denominator, and why an empty one cannot be a pass."""

    total_eligible: int = 0
    evaluated: int = 0
    passed: int = 0
    failed: int = 0
    unavailable: int = 0


def record_evaluation(
    conn,
    *,
    project_id: str,
    monitor_id: str,
    monitor_version_id: str,
    outcome: str,
    window_start: date,
    window_end: date,
    counts: EvaluationCounts,
    dependency_refs: Mapping[str, Any],
    observed: Mapping[str, Any] | None = None,
    evidence_refs: Sequence[Mapping[str, Any]] = (),
    operation_id: str | None = None,
    evaluator_version: str = EVALUATOR_VERSION,
    datastream_id: str | None = None,
    execution_id: str | None = None,
) -> str:
    """Write one immutable evaluation. Idempotent by content.

    Refuses a ``pass`` over zero eligible members in Python as well as in SQL. The
    duplication is deliberate: the database constraint is the guarantee, and this
    check is what turns it into a message a caller can act on rather than an
    integrity error.

    THIS IS THE SETTLED HALF OF MIGRATION 222, and it is what makes
    :func:`open_issue`'s moving run safe. The issue carries the run that saw the
    anomaly LAST and overwrites the previous one; the evaluation carries the run
    of ITS OWN window and is append-only, so every night that was ever measured
    keeps a row naming its Datastream and its run. Without this writer the
    overwrite would destroy the earlier run with no survivor, and the sentence
    "nothing is lost" would be a claim nothing proves.

    Both are optional and both default to NULL, because a project-scoped monitor
    watches no single Datastream and a scheduled evaluation may ride no
    collection -- migration 222 made them nullable for exactly those two cases.
    ``execution_id`` without ``datastream_id`` is refused here as it is on the
    issue: the composite foreign key is MATCH SIMPLE and would skip its check.
    """

    if outcome not in OUTCOMES:
        raise DqGovernanceError(f"outcome must be one of {list(OUTCOMES)}")
    if execution_id and not datastream_id:
        raise DqGovernanceError(
            "an execution_id without a datastream_id is not a weaker statement, it is "
            "an unreadable one: the composite foreign key would be skipped outright."
        )
    if window_end < window_start:
        raise DqGovernanceError("window_end cannot precede window_start")
    if counts.total_eligible == 0 and outcome in {OUTCOME_PASS, OUTCOME_FAIL}:
        raise DqGovernanceError(
            "zero eligible members is not a pass and not a failure: record "
            "not_applicable, so a dashboard cannot go green over nothing."
        )

    dependency_fingerprint = content_hash(dict(dependency_refs))
    digest = content_hash(
        {
            "monitor_version_id": monitor_version_id,
            "outcome": outcome,
            "window": [window_start.isoformat(), window_end.isoformat()],
            "counts": [
                counts.total_eligible,
                counts.evaluated,
                counts.passed,
                counts.failed,
                counts.unavailable,
            ],
            "dependency_fingerprint": dependency_fingerprint,
        }
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.dq_evaluations WHERE monitor_version_id = %s "
            "AND window_start = %s AND window_end = %s AND content_hash = %s",
            (monitor_version_id, window_start, window_end, digest),
        )
        existing = cur.fetchone()
        if existing:
            return str(existing[0])
        evaluation_id = _mint("dqe")
        cur.execute(
            """
            INSERT INTO app.dq_evaluations
                (id, project_id, monitor_id, monitor_version_id, outcome, window_start,
                 window_end, dependency_refs, dependency_fingerprint, total_eligible,
                 evaluated_count, passed_count, failed_count, unavailable_count,
                 observed, evidence_refs, operation_id, evaluator_version, content_hash,
                 datastream_id, execution_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s)
            """,
            (
                evaluation_id,
                project_id,
                monitor_id,
                monitor_version_id,
                outcome,
                window_start,
                window_end,
                canonical_json(dict(dependency_refs)),
                dependency_fingerprint,
                counts.total_eligible,
                counts.evaluated,
                counts.passed,
                counts.failed,
                counts.unavailable,
                canonical_json(dict(observed or {})),
                canonical_json([dict(item) for item in evidence_refs]),
                operation_id,
                evaluator_version,
                digest,
                datastream_id,
                execution_id,
            ),
        )
        cur.execute(
            "UPDATE app.dq_monitors SET runtime_state = %s, updated_at = NOW() "
            "WHERE id = %s AND project_id = %s",
            (runtime_state_for(outcome), monitor_id, project_id),
        )
    return evaluation_id


def _located_target(
    dependency_refs: Mapping[str, Any], observed: Mapping[str, Any] | None
) -> dict[str, str | None]:
    """WHERE this evaluation looked, read from what the evaluator already returned.

    `dependency_refs["target"]` is `{"kind": ..., "id": ...}` and is written by
    every evaluator of the dispatch table; `observed["notes"][]` is where a check
    that could name a run puts it. Neither is a new contract: reading them is what
    lets migration 222's evaluation half be filled without widening the four-tuple
    every check profile answers.

    A target that is not a Datastream locates nothing, and says so with two NULLs
    rather than with an id borrowed from another kind.
    """
    target = dependency_refs.get("target")
    if not isinstance(target, Mapping) or target.get("kind") != "datastream":
        return {"datastream_id": None, "execution_id": None}
    datastream_id = target.get("id")
    datastream_id = str(datastream_id) if datastream_id else None
    if not datastream_id:
        return {"datastream_id": None, "execution_id": None}

    execution_id = None
    for note in (observed or {}).get("notes") or []:
        if not isinstance(note, Mapping):
            continue
        if str(note.get("datastream_id") or "") != datastream_id:
            continue
        run = note.get("execution_id")
        if run:
            execution_id = str(run)
            break
    return {"datastream_id": datastream_id, "execution_id": execution_id}


def runtime_state_for(outcome: str) -> str:
    """The monitor's runtime state implied by its latest evaluation.

    ``error`` and ``unverifiable`` map to ``unavailable``, NOT to ``healthy`` and
    not to ``failing``: the check could not be performed, which is a different
    fact from the target being broken and a very different one from it being fine.
    """
    if outcome == OUTCOME_PASS:
        return "healthy"
    if outcome == OUTCOME_FAIL:
        return "failing"
    if outcome == OUTCOME_NOT_APPLICABLE:
        return "not_applicable"
    return "unavailable"


def latest_evaluation(conn, *, project_id: str, monitor_id: str) -> dict[str, Any] | None:
    columns = (
        "id",
        "monitor_version_id",
        "outcome",
        "window_start",
        "window_end",
        "total_eligible",
        "evaluated_count",
        "passed_count",
        "failed_count",
        "unavailable_count",
        "observed",
        "evaluated_at",
    )
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(columns)} FROM app.dq_evaluations "
            "WHERE project_id = %s AND monitor_id = %s ORDER BY evaluated_at DESC LIMIT 1",
            (project_id, monitor_id),
        )
        row = cur.fetchone()
    return dict(zip(columns, row)) if row else None


# ---------------------------------------------------------------------------
# Issues.
# ---------------------------------------------------------------------------


def open_issue(
    conn,
    *,
    project_id: str,
    monitor_id: str,
    root_cause_fingerprint: str,
    severity: str,
    actor: str = "system",
    evaluation_id: str | None = None,
    control_case_id: str | None = None,
    datastream_id: str | None = None,
    execution_id: str | None = None,
) -> dict[str, Any]:
    """Open or reopen the one issue for this (monitor, root cause).

    Same deterministic recurrence rule as a Control Case, and enforced the same
    way: a partial unique index, not a convention. Reopening appends an event; it
    never rewrites the first sighting.

    WHERE THE RUN GOES, AND WHY THE ANSWER IS "IT MOVES". ``execution_id`` is the
    run that observed this anomaly LAST, and it travels with ``last_seen_at``.
    Until story 59.3 the recurrence branch updated ``last_seen_at`` alone, so an
    issue stayed pinned to the FIRST run that ever saw it: the per-run read
    (``idx_dq_issues_execution``, ``WHERE (project_id, datastream_id,
    execution_id)``) then lost it on every later night, and a monitor firing every
    night rendered on one run and on no other.

    An open issue is a CURRENT problem, so it belongs to the most recent run that
    saw it. THE OVERWRITE IS ONLY SAFE BECAUSE THE EARLIER RUN SURVIVES ELSEWHERE,
    and that is a property of the CALLER, not of this function:

    * ``app.dq_evaluations`` carries its own ``datastream_id`` / ``execution_id``
      (migration 222, written by :func:`record_evaluation`) -- one append-only
      evaluation per window, which is the grain of history. A caller that
      overwrites the run here without writing one destroys the previous sighting
      with no survivor;
    * ``app.dq_issue_events`` is append-only and receives one ``observed`` per
      recurrence. That table has NO run column of its own, so the only thing that
      ties a sighting to a run is the ``evaluation_id`` passed here -- which is
      why it is passed, and why a bridge that omits it keeps no history at all.

    Two halves of one migration, two grains: the current one on the issue, the
    settled one on the evaluation, joined by the event between them. Written here
    because migration 222 is applied and cannot be re-edited, and because a
    semantics nobody wrote down is a semantics the next writer invents.

    ``execution_id`` without ``datastream_id`` is refused in Python as well as by
    ``ck_dq_issues_execution_needs_datastream``: the composite foreign key is
    MATCH SIMPLE, so a NULL Datastream would skip the check entirely and store a
    run nothing can resolve. An unresolvable run is written as NULL, never as a
    fabricated id.
    """

    if execution_id and not datastream_id:
        raise DqGovernanceError(
            "an execution_id without a datastream_id is not a weaker statement, it is "
            "an unreadable one: the composite foreign key would be skipped outright."
        )

    with conn.cursor() as cur:
        # The ROW-IDENTITY question, not the "is it still alarming" one: this
        # lookup must agree with migration 145's partial unique index or the
        # INSERT below collides with it. `unclosed_issue_predicate` is that
        # sentence, spelled once and named for what it is.
        cur.execute(
            "SELECT id, status FROM app.dq_issues WHERE project_id = %s AND monitor_id = %s "
            f"AND root_cause_fingerprint = %s AND {unclosed_issue_predicate()}",  # noqa: S608
            (project_id, monitor_id, root_cause_fingerprint),
        )
        row = cur.fetchone()
        if row is not None:
            issue_id, status = str(row[0]), str(row[1])
            cur.execute(
                "UPDATE app.dq_issues "
                "SET last_seen_at = NOW(), datastream_id = %s, execution_id = %s "
                "WHERE id = %s",
                (datastream_id, execution_id, issue_id),
            )
            _append_event(cur, issue_id, project_id, "observed", actor, None, evaluation_id)
            return {"id": issue_id, "status": status, "recurrence": "appended"}
        issue_id = _mint("dqi")
        cur.execute(
            """
            INSERT INTO app.dq_issues
                (id, project_id, monitor_id, root_cause_fingerprint, severity, control_case_id,
                 datastream_id, execution_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                issue_id,
                project_id,
                monitor_id,
                root_cause_fingerprint,
                severity,
                control_case_id,
                datastream_id,
                execution_id,
            ),
        )
        _append_event(cur, issue_id, project_id, "observed", actor, None, evaluation_id)
    return {"id": issue_id, "status": "open", "recurrence": "opened"}


#: The status each workflow event moves an issue to -- the ONE vocabulary.
#:
#: Lifted out of :func:`transition_issue` by story 59.1 because a caller now has
#: to refuse an unknown event BEFORE it opens a transaction, and a second list
#: written at that call site would be a second vocabulary to keep in step. The
#: keys are exactly `app.dq_issue_events.event_kind`'s CHECK minus `observed`,
#: which is written by :func:`open_issue` and by nothing a person clicks.
ISSUE_STATUS_FOR_EVENT = {
    "acknowledged": "acknowledged",
    "investigating": "investigating",
    "suppressed": "suppressed",
    "resolved": "resolved",
    "reopened": "open",
    "closed": "closed",
}


def transition_issue(
    conn,
    *,
    project_id: str,
    issue_id: str,
    event_kind: str,
    actor: str,
    reason: str,
    suppressed_until: date | None = None,
) -> None:
    """Move an issue and append the event. No issue is ever hard-deleted.

    A suppression must carry an end date. A permanent one is a silent hole with a
    friendly name, and the schema refuses it too.
    """

    status_for = ISSUE_STATUS_FOR_EVENT
    if event_kind not in status_for:
        raise DqGovernanceError(f"event_kind must be one of {sorted(status_for)}")
    if event_kind == "suppressed" and suppressed_until is None:
        raise DqGovernanceError(
            "a suppression needs an end date: a permanent one is a silent hole"
        )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.dq_issues WHERE id = %s AND project_id = %s",
            (issue_id, project_id),
        )
        if cur.fetchone() is None:
            raise DqGovernanceError(f"issue {issue_id} is not in this Project")
        cur.execute(
            "UPDATE app.dq_issues SET status = %s, suppressed_until = %s WHERE id = %s",
            (status_for[event_kind], suppressed_until, issue_id),
        )
        _append_event(cur, issue_id, project_id, event_kind, actor, reason, None)


def _append_event(
    cur,
    issue_id: str,
    project_id: str,
    event_kind: str,
    actor: str,
    reason: str | None,
    evaluation_id: str | None,
) -> None:
    cur.execute(
        """
        INSERT INTO app.dq_issue_events
            (id, issue_id, project_id, event_kind, actor, reason, evaluation_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (_mint("dqie"), issue_id, project_id, event_kind, actor, reason, evaluation_id),
    )


# WHAT MAKES AN ISSUE STILL OPEN -- the one predicate, written once.
#
# `open_issues` below, `controls_attention._issue_items`, the Governance monitor
# card (`governance_read_model._GOVERNED_DQ_MONITORS`) and (since story 59.2)
# the Datastream fleet badge all answer "is this issue still live". Several
# spellings of it would be several answers the first time one of them grew a
# clause -- and two readers DID disagree until AI-235: spelled without the
# suppression half, a live-suppressed anomaly counted as open on the Governance
# monitor card and in change-set impact while `open_issues` excluded it, and the
# disagreement leaned toward the side that alarms.
#
# An EXPIRED suppression is not a suppression -- an end date that quietly
# extends itself is the same silent hole with extra steps, which is why the
# suppression line reads `<` and not `>=`.
def open_issue_predicate(
    *, include_acknowledged: bool = True, alias: str | None = None
) -> str:
    """The `WHERE` fragment for open issues -- the ONE spelling of "still open".

    `include_acknowledged=True` is the Governance reading: an acknowledged issue
    is still unresolved and still owed a fix. `False` is the "what is left to
    review" reading the Datastream badge asks. Both are legitimate questions and
    neither is a second predicate -- the caller names its question, the text
    stays one.

    `alias` prefixes every column reference (`alias="i"` reads `i.status`), so a
    reader that joins `app.dq_issues` against another table stays unambiguous
    without respelling the predicate. `None` leaves the columns bare for a
    single-table `WHERE`.
    """

    col = f"{alias}." if alias else ""
    text = (
        f"{col}status NOT IN ('closed', 'resolved')\n"
        f"              AND ({col}status <> 'suppressed' OR {col}suppressed_until < CURRENT_DATE)"
    )
    if not include_acknowledged:
        # Story 59.2, arbitrage 1: a badge that does not move when someone
        # clicks « Mark as reviewed » -- the gesture 59.1 shipped through
        # :func:`transition_issue` -- lies about what that person just did. But
        # writing this clause at a call site would be a second predicate, so it
        # lives here and the caller declares the question it is asking instead
        # of spelling the answer.
        text = f"{text}\n              AND {col}status <> 'acknowledged'"
    return text


#: The bare-column reading of the predicate above, kept as the module's noun for
#: docstrings and single-table callers. It is DERIVED, never respelled.
OPEN_ISSUE_PREDICATE = open_issue_predicate()


def unclosed_issue_predicate(*, alias: str | None = None) -> str:
    """NOT the predicate above, and the whole point of giving it a name.

    `app.dq_issues` carries a SECOND sentence about an issue, written into the
    schema rather than into a reader: migration 145's partial unique index
    ``uq_dq_issues_open_root_cause ... WHERE status <> 'closed'``. That index
    decides how many ROWS may exist for one (project, monitor, root cause), and
    by that rule a `resolved` issue and a live-suppressed one both still occupy
    the slot -- neither of which :func:`open_issue_predicate` calls open.

    The two are not in competition and neither is wrong. "Is this row still the
    one this root cause writes to?" and "is this anomaly still alarming?" are
    different questions with different answers, and :func:`open_issue` must ask
    the FIRST one or its INSERT collides with the index. What was wrong until
    2026-08-21 was that only one of them had a name: the other was hand-spelled
    at the call site, three characters away from a predicate that means
    something else, with nothing saying which was intended.

    The index name says "open" and this function does not, deliberately. The
    index is applied (ledger `toorow_meta.schema_migrations`, id 145, applied
    2026-07-30) and is never re-edited; a renaming would be a following
    migration and a product decision, not a repair.
    """

    col = f"{alias}." if alias else ""
    return f"{col}status <> 'closed'"


#: The bare-column reading of the row-identity predicate, DERIVED like the one
#: above so the two nouns of this module come from their own builders.
UNCLOSED_ISSUE_PREDICATE = unclosed_issue_predicate()


#: The three values `app.dq_issues.severity` is CHECK-constrained to (migration
#: 145), worst first -- the order `open_issues` sorts by and the order a badge
#: picks its highest from. The console holds no copy of this list: it renders the
#: word the server sends.
ISSUE_SEVERITIES: tuple[str, ...] = ("blocking", "degrading", "informational")


def open_issue_counts_by_datastream(
    conn,
    *,
    project_id: str,
    include_acknowledged: bool = True,
) -> dict[str, dict[str, Any]]:
    """Open issues per Datastream, and whether anything is watching it at all.

    ONE GROUPED READ FOR A WHOLE PROJECT, never one subquery per row. Measured on
    preprod 2026-08-08 with the predicate above: `GroupAggregate … Buffers: shared
    hit=5 … Execution Time: 0.148 ms`, served by `uq_dq_issues_open_root_cause`
    on `(project_id, …)`. The per-row shape would have needed a new index to be
    bounded -- `idx_dq_issues_execution` is partial on `execution_id IS NOT NULL`
    and no index carries `(project_id, datastream_id)` for the others -- so the
    grouped read is also the one that costs no migration.

    IT IS INDEPENDENT OF `execution_id`, and that is the whole point. An issue
    whose run could not be named (`app.pull_jobs.execution_id` was NULL on 130
    rows of 130 on the disposable base and 6 of 6 on preprod) is the MAJORITY
    case; a badge derived from the per-run read
    (:func:`core.datastream_workbench._run_anomalies`) would count zero for it.

    THREE FACTS COME BACK, NEVER TWO. `monitored` says a published monitor
    targets this Datastream -- which is why the monitors are read in the same
    statement rather than inferred from the issues. "Nothing has looked at this
    flux" and "something looked and found nothing" are two different sentences,
    and preprod instantiates the second one today (2 published monitors, 0
    issues, on `datastream` targets).

    A Datastream absent from the result carries no monitor and no issue: the
    caller supplies the measured-zero entry, because a missing key here means the
    aggregate was never read, and those two must never render for each other.
    """

    counts = ",\n                   ".join(
        f"COUNT(*) FILTER (WHERE kind = 'issue' AND severity = '{severity}')"
        f" AS {severity}_count"
        for severity in ISSUE_SEVERITIES
    )
    query = f"""
            SELECT datastream_id,
                   COUNT(*) FILTER (WHERE kind = 'monitor') AS monitor_count,
                   {counts},
                   (array_agg(
                       execution_id
                       ORDER BY CASE severity
                           WHEN 'blocking' THEN 0
                           WHEN 'degrading' THEN 1
                           ELSE 2
                       END,
                       last_seen_at DESC
                   ) FILTER (
                       WHERE kind = 'issue' AND execution_id IS NOT NULL
                   ))[1] AS faulty_execution_id
              FROM (
                    SELECT target_id AS datastream_id, 'monitor' AS kind,
                           NULL::text AS severity,
                           NULL::text AS execution_id,
                           NULL::timestamptz AS last_seen_at
                      FROM app.dq_monitors
                     WHERE project_id = %(project_id)s
                       AND target_kind = 'datastream'
                       AND lifecycle_status = 'published'
                    UNION ALL
                    SELECT datastream_id, 'issue' AS kind, severity,
                           execution_id,
                           last_seen_at
                      FROM app.dq_issues
                     WHERE project_id = %(project_id)s
                       AND datastream_id IS NOT NULL
                       AND {open_issue_predicate(include_acknowledged=include_acknowledged)}
                   ) observation
             GROUP BY datastream_id
    """  # noqa: S608 -- both interpolations are module-owned literals
    summaries: dict[str, dict[str, Any]] = {}
    with conn.cursor() as cur:
        cur.execute(query, {"project_id": project_id})
        for row in cur.fetchall():
            datastream_id = str(row[0])
            by_severity = {
                severity: int(row[index] or 0)
                for index, severity in enumerate(ISSUE_SEVERITIES, start=2)
            }
            faulty_execution_id = row[2 + len(ISSUE_SEVERITIES)]
            summaries[datastream_id] = issue_summary(
                monitored=int(row[1] or 0) > 0,
                by_severity=by_severity,
                faulty_execution_id=faulty_execution_id,
            )
    return summaries


def issue_summary(
    *,
    monitored: bool,
    by_severity: Mapping[str, int] | None = None,
    faulty_execution_id: str | None = None,
) -> dict[str, Any]:
    """The shape a badge reads, built in one place so two readers cannot differ.

    `highest_severity` is `None` when nothing is open -- never `"informational"`
    standing in for "nothing", and never a word the console invented.
    """

    counts = {
        severity: int((by_severity or {}).get(severity) or 0) for severity in ISSUE_SEVERITIES
    }
    total = sum(counts.values())
    highest = next((severity for severity in ISSUE_SEVERITIES if counts[severity]), None)
    return {
        "monitored": bool(monitored),
        "count": total,
        "by_severity": counts,
        "highest_severity": highest,
        "faulty_execution_id": faulty_execution_id,
    }


#: What a Datastream the grouped read did not name carries: a MEASURED zero, on
#: a flux nothing watches. Distinct from an absent key, which means the read
#: itself failed and is the fourth state the screens draw.
NO_ISSUE_SUMMARY = issue_summary(monitored=False)

#: The savepoint :func:`read_open_issue_counts` marks before the aggregate.
_ISSUE_COUNT_SAVEPOINT = "dq_open_issue_counts"


def _savepoint(conn, statement: str) -> bool:
    """Run one savepoint statement, best effort. `False` if it did not take.

    Never raises. `ROLLBACK TO SAVEPOINT` is one of the two statements PostgreSQL
    still accepts on a poisoned transaction, so this is exactly the call that has
    to work when everything else has stopped working; and `SAVEPOINT` outside a
    transaction block is refused, which is the autocommit case where there is
    nothing to poison in the first place.
    """

    try:
        with conn.cursor() as cur:
            cur.execute(statement)
    except Exception as exc:  # noqa: BLE001 -- a savepoint is a precaution, not a result
        logger.debug("dq_governance: %s unavailable: %s", statement, exc)
        return False
    return True


def read_open_issue_counts(
    conn,
    *,
    project_id: str,
    include_acknowledged: bool = True,
) -> dict[str, dict[str, Any]] | None:
    """The counts, or `None` -- AND A CONNECTION THAT IS STILL USABLE EITHER WAY.

    This is the fail-soft entry point, and it exists because catching the Python
    exception is not enough. A statement that fails puts its transaction in the
    aborted state, and every later statement on that connection then raises
    `InFailedSqlTransaction` -- so a caller that swallowed the error still took
    down whatever it read NEXT. Measured on the disposable base, role `postgres`:
    with the bare `try/except`, the `datastreams` lens degraded correctly and
    `compose_data_surface(project_id, "overview")` -- which reads the six lenses
    on one connection -- raised, i.e. the swallow protected the one lens that
    would have degraded anyway and broke the one that composes all of them.

    A SAVEPOINT is what un-poisons it: rolling back to it discards the failed
    statement and leaves the surrounding transaction alive. The other five lenses
    then compose, and the Datastream rows carry NO key -- "the count could not be
    read", which is a fourth state and never a `0`.

    `None` is that fourth state. It is deliberately different from `{}`, which is
    a read that succeeded and found no monitor and no issue anywhere.
    """

    marked = _savepoint(conn, f"SAVEPOINT {_ISSUE_COUNT_SAVEPOINT}")
    try:
        counts = open_issue_counts_by_datastream(
            conn, project_id=project_id, include_acknowledged=include_acknowledged
        )
    except Exception as exc:  # noqa: BLE001 -- one unreadable column takes down no screen
        logger.warning(
            "dq_governance: open issue counts unavailable project=%s: %s", project_id, exc
        )
        if marked:
            _savepoint(conn, f"ROLLBACK TO SAVEPOINT {_ISSUE_COUNT_SAVEPOINT}")
        return None
    if marked:
        _savepoint(conn, f"RELEASE SAVEPOINT {_ISSUE_COUNT_SAVEPOINT}")
    return counts


def open_issues(
    conn, *, project_id: str, limit: int = 50, include_acknowledged: bool = True
) -> list[dict[str, Any]]:
    """Unresolved issues for the Data Quality lens and the attention projection.

    A suppressed issue whose suppression has EXPIRED comes back: an end date that
    quietly extends itself is the same silent hole with extra steps.

    `include_acknowledged` is story 59.2's parameter, and it is a parameter
    rather than a second query on purpose -- see :data:`OPEN_ISSUE_PREDICATE`.
    """

    columns = (
        "id",
        "monitor_id",
        "status",
        "severity",
        "suppressed_until",
        "control_case_id",
        "first_seen_at",
        "last_seen_at",
    )
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {', '.join(columns)} FROM app.dq_issues
            WHERE project_id = %s
              AND {open_issue_predicate(include_acknowledged=include_acknowledged)}
            ORDER BY CASE severity WHEN 'blocking' THEN 0 WHEN 'degrading' THEN 1 ELSE 2 END,
                     last_seen_at DESC
            LIMIT %s
            """,  # noqa: S608 -- columns and predicate are module-owned literals
            (project_id, max(1, min(limit, 200))),
        )
        return [dict(zip(columns, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Manual evaluation, on the same durable operation as scheduled work.
# ---------------------------------------------------------------------------

MANUAL_EVALUATION_COMMAND = "dq.monitor.evaluate"


def evaluate_monitor(
    conn,
    *,
    project_id: str,
    org_id: str,
    monitor_id: str,
    actor: str,
    idempotency_key: str,
    window_start: date,
    window_end: date,
    evaluator,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Run one monitor now, through a durable operation. Returns the operation result.

    This is the replacement for `/api/dq/evaluate`, which submitted its work to a
    request-scoped ``ThreadPoolExecutor(max_workers=1)``. Three things were wrong
    with that and are fixed by construction here:

    * **Nothing could observe it.** A thread that outlives its request has no id,
      no state and no audit row, so "did my evaluation run?" had no answer.
    * **Nothing could replay it.** Two identical requests started two threads.
      `execute_operation` arbitrates on the idempotency key and returns the FIRST
      result rather than evaluating twice.
    * **It was a second evaluator.** AC7 requires manual and scheduled work to use
      the same one; ``evaluator`` is injected and is the same callable the
      scheduler passes, so a manual run cannot disagree with a nightly one.

    The evaluator returns ``(outcome, EvaluationCounts, dependency_refs, observed)``.
    Its verdict is written through :func:`record_evaluation`, so the same refusal
    applies: a pass over zero eligible members is not storable.
    """

    from core.operations import MutationResult, OperationSpec, execute_operation  # noqa: PLC0415

    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_version_id FROM app.dq_monitors "
            "WHERE id = %s AND project_id = %s AND lifecycle_status = 'published'",
            (monitor_id, project_id),
        )
        row = cur.fetchone()
    if row is None or not row[0]:
        raise DqMonitorNotFound(
            f"monitor {monitor_id} has no published version to evaluate against"
        )
    monitor_version_id = str(row[0])

    def mutation(inner_conn, operation_id: str) -> MutationResult:
        outcome, counts, dependency_refs, observed = evaluator(
            inner_conn,
            project_id=project_id,
            monitor_id=monitor_id,
            monitor_version_id=monitor_version_id,
            window_start=window_start,
            window_end=window_end,
        )
        located = _located_target(dependency_refs, observed)
        evaluation_id = record_evaluation(
            inner_conn,
            project_id=project_id,
            monitor_id=monitor_id,
            monitor_version_id=monitor_version_id,
            outcome=outcome,
            window_start=window_start,
            window_end=window_end,
            counts=counts,
            dependency_refs=dependency_refs,
            observed=observed,
            # The evaluation carries its operation, so "who asked for this run
            # and when" is answerable FROM the evidence row rather than by
            # correlating two tables on a timestamp.
            operation_id=operation_id,
            # AND it carries what it looked at. Both come from what the evaluator
            # ALREADY returned -- the target of `dependency_refs` and the run
            # named in `observed["notes"]` -- so the shared four-tuple contract
            # every check profile answers is untouched.
            datastream_id=located["datastream_id"],
            execution_id=located["execution_id"],
        )
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=content_hash({"evaluation_id": evaluation_id, "outcome": outcome}),
            result={
                "evaluation_id": evaluation_id,
                "outcome": outcome,
                "monitor_version_id": monitor_version_id,
            },
            outbox_payload={"project_id": project_id, "monitor_id": monitor_id},
        )

    spec = OperationSpec(
        command_type=MANUAL_EVALUATION_COMMAND,
        actor=_require(actor, "actor"),
        effective_org_id=org_id,
        resource_path=(f"organization:{org_id}", f"project:{project_id}", f"monitor:{monitor_id}"),
        idempotency_key=_require(idempotency_key, "idempotency_key"),
        host_context={},
        # `versions` is a CLOSED vocabulary (policy/catalog/tool) in
        # `core.operations`. The monitor version is the policy this run was
        # judged against and the evaluator is the tool that judged it, so both
        # fit -- widening the envelope for one command family would have made
        # every other operation's version block mean something looser.
        versions={"policy": monitor_version_id, "tool": EVALUATOR_VERSION},
        request_payload={
            "monitor_id": monitor_id,
            # Pinned in the REQUEST as well: replaying an idempotency key after
            # the monitor was republished is a different request, and the
            # request-hash comparison must be able to say so.
            "monitor_version_id": monitor_version_id,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
        },
        provider_references={},
        # An evaluation READS; it changes no governed decision, so it needs no
        # human confirmation. What it must be is observable and replayable.
        confirmation_mode="none",
        confirmation_reference=None,
        trace_id=trace_id,
    )
    operation = execute_operation(conn, spec, mutation=mutation)
    return {
        "operation_id": operation.operation_id,
        "outcome": operation.outcome,
        "result": operation.result,
        "replayed": operation.replayed,
    }
