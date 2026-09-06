"""The read-only Overview projection of unresolved control work (Story 49.4, AC10).

"Material unresolved cases, DQ issues and exceptions project to Overview with
cause, impact, scope, status/recency, evidence horizon, next action, deterministic
root-cause deduplication and exact Governance owner route. Overview neither copies
nor mutates the owner record."

Three properties, and each one is a way this could have gone wrong:

* **Read only.** Nothing here writes, and nothing returns an owner record --
  only a reference to it. Overview showing a copy of a case is how two places
  start disagreeing about whether it is resolved.
* **The root-cause key is the OWNER's**, not a new one minted here. A case and
  the DQ issue that escalated into it deduplicate to one attention item because
  they share a fingerprint, which is what `rank_attention_items` needs to collapse
  them. Minting a key from the row id would show the same problem twice.
* **Material only.** An informational case, a resolved issue and an expired
  exception are not attention. A list that shows everything is a list nobody
  reads, and then the blocking item is missed.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: `rank_attention_items` sorts on this. Blocking control work sits with the
#: operational failures (1-3) rather than with readiness projections (4), because
#: a refused publication and a blocking conflict cost the same thing.
PRIORITY_BLOCKING = 2
PRIORITY_DEGRADING = 4
PRIORITY_INFORMATIONAL = 5

_PRIORITY_BY_SEVERITY = {
    "blocking": PRIORITY_BLOCKING,
    "degrading": PRIORITY_DEGRADING,
    "informational": PRIORITY_INFORMATIONAL,
}

#: Severities that reach Overview at all. An informational case is real and is
#: not attention: it belongs in the Conflicts lens, where someone went looking.
MATERIAL_SEVERITIES = frozenset({"blocking", "degrading"})


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def control_attention_items(conn, *, project_id: str) -> list[dict[str, Any]]:
    """Unresolved cases, open DQ issues and in-force exceptions, as attention items.

    Fail-soft and BOUNDED. An unreadable control store returns an empty list
    rather than raising: Overview is a summary, and taking the whole page down
    because one projection could not be read would hide the other five. The
    failure is logged, and the Controls & Quality lenses are where the authoritative
    answer lives.
    """

    try:
        return [
            *_case_items(conn, project_id),
            *_issue_items(conn, project_id),
            *_exception_items(conn, project_id),
        ]
    except Exception as exc:  # noqa: BLE001 -- a summary must not take the page down
        logger.warning(
            "controls_attention: projection failed project=%s: %s", project_id, exc
        )
        return []


def _owner(section: str, object_type: str, object_id: str) -> dict[str, Any]:
    from core.project_overview import owner_reference  # noqa: PLC0415

    return owner_reference(
        "governance", section, object_type=object_type, object_id=object_id
    )


def _case_items(conn, project_id: str) -> list[dict[str, Any]]:
    # A Control Case has its OWN "open", owned by `control_cases` and consumed
    # here rather than respelled -- the same rule AI-235 applied to anomalies,
    # applied to the object next to them. This list asks the ATTENTION reading:
    # a resolved case is not something to act on today. `open_cases` asks the
    # recurrence reading. Two questions, one text, and the caller names which.
    from core.control_cases import open_case_predicate  # noqa: PLC0415

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT c.id, c.case_type, c.subject_kind, c.subject_id, c.severity, c.status,
                   c.root_cause_fingerprint, c.first_observed_at, c.last_observed_at,
                   c.evidence_horizon_at,
                   (SELECT owner_outcome FROM app.control_case_decisions
                     WHERE case_id = c.id ORDER BY decided_at DESC LIMIT 1) AS latest_outcome
            FROM app.control_cases c
            WHERE c.project_id = %s
              AND {open_case_predicate(include_resolved=False, alias="c")}
              AND c.severity = ANY(%s)
            ORDER BY c.last_observed_at DESC
            LIMIT 25
            """,  # noqa: S608 -- the interpolation is a module-owned literal
            (project_id, sorted(MATERIAL_SEVERITIES)),
        )
        rows = cur.fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        (
            case_id,
            case_type,
            subject_kind,
            subject_id,
            severity,
            status,
            fingerprint,
            first_seen,
            last_seen,
            horizon,
            latest_outcome,
        ) = row
        cause = f"{case_type} on {subject_kind} {subject_id}"
        if latest_outcome == "failed":
            # The one state worth promoting: someone decided, the owner command
            # failed, and the case is neither open-and-untouched nor resolved.
            cause = f"{cause} -- a decision was taken and the owner command failed"
        items.append(
            {
                "id": f"control-case:{case_id}",
                # The OWNER's fingerprint, so a case and the DQ issue that
                # escalated into it collapse into one attention item.
                "root_cause_key": str(fingerprint),
                "priority_class": _PRIORITY_BY_SEVERITY.get(
                    str(severity), PRIORITY_DEGRADING
                ),
                "cause": cause,
                "impact": "Governed data decisions are blocked until this is decided.",
                "scope": [str(subject_kind)],
                "status": str(status),
                "first_observed_at": _iso(first_seen),
                "last_observed_at": _iso(last_seen),
                "evidence_horizon": _iso(horizon),
                "owner": _owner("controls-quality", "control-case", str(case_id)),
                "action": {"label": "Open the case", "permitted": True},
            }
        )
    return items


def _issue_items(conn, project_id: str) -> list[dict[str, Any]]:
    # "Still open" is dq_governance's ONE sentence, consumed rather than
    # respelled: AI-235 measured two readers disagreeing on whether a suppressed
    # anomaly is open, and the disagreement leaned toward the side that alarms.
    from core.dq_governance import open_issue_predicate  # noqa: PLC0415

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT i.id, i.monitor_id, i.severity, i.status, i.root_cause_fingerprint,
                   i.first_seen_at, i.last_seen_at, m.label, m.target_kind, m.target_id
            FROM app.dq_issues i
            JOIN app.dq_monitors m ON m.id = i.monitor_id AND m.project_id = i.project_id
            WHERE i.project_id = %s
              AND {open_issue_predicate(alias="i")}
              AND i.severity = ANY(%s)
            ORDER BY i.last_seen_at DESC
            LIMIT 25
            """,  # noqa: S608 -- the interpolation is a module-owned literal
            (project_id, sorted(MATERIAL_SEVERITIES)),
        )
        rows = cur.fetchall()

    return [
        {
            "id": f"dq-issue:{issue_id}",
            "root_cause_key": str(fingerprint),
            "priority_class": _PRIORITY_BY_SEVERITY.get(str(severity), PRIORITY_DEGRADING),
            "cause": f"{label} is failing on {target_kind} {target_id}",
            "impact": "Published figures from this target may be wrong or incomplete.",
            "scope": [str(target_kind)],
            "status": str(status),
            "first_observed_at": _iso(first_seen),
            "last_observed_at": _iso(last_seen),
            "evidence_horizon": _iso(last_seen),
            "owner": _owner("controls-quality", "dq-monitor", str(monitor_id)),
            "action": {"label": "Open the monitor", "permitted": True},
        }
        for (
            issue_id,
            monitor_id,
            severity,
            status,
            fingerprint,
            first_seen,
            last_seen,
            label,
            target_kind,
            target_id,
        ) in rows
    ]


def _exception_items(conn, project_id: str) -> list[dict[str, Any]]:
    """Rule Set exceptions IN FORCE.

    An exception is a decision someone took, so it is not a failure -- but a rule
    that is not applying somewhere is exactly the thing an operator should be able
    to see without going looking, and one that has quietly been in force for a
    year is the reason this projection exists.
    """

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.id, e.rule_set_id, e.subject_kind, e.subject_id, e.reason_code,
                   e.reason, e.effective_from, e.expires_at, s.label
            FROM app.governance_rule_set_exceptions e
            JOIN app.governance_rule_sets s
              ON s.id = e.rule_set_id AND s.project_id = e.project_id
            WHERE e.project_id = %s
              AND e.effective_from <= CURRENT_DATE
              AND (e.expires_at IS NULL OR e.expires_at >= CURRENT_DATE)
            ORDER BY e.effective_from
            LIMIT 25
            """,
            (project_id,),
        )
        rows = cur.fetchall()

    return [
        {
            "id": f"rule-set-exception:{exception_id}",
            "root_cause_key": f"rule-set-exception:{rule_set_id}:{subject_kind}:{subject_id}",
            # An in-force exception is informational by nature: it is a decision,
            # not a defect. It ranks last and is still visible.
            "priority_class": PRIORITY_INFORMATIONAL,
            "cause": f"{label} does not apply to {subject_kind} {subject_id} ({reason_code})",
            "impact": reason,
            "scope": [str(subject_kind)],
            "status": "in_force",
            "first_observed_at": _iso(effective_from),
            "last_observed_at": _iso(effective_from),
            # An exception with no expiry has no horizon, and saying so is the
            # point: it is the one that will still be in force next year.
            "evidence_horizon": _iso(expires_at),
            "owner": _owner("controls-quality", "rule-set", str(rule_set_id)),
            "action": {"label": "Open the rule set", "permitted": True},
        }
        for (
            exception_id,
            rule_set_id,
            subject_kind,
            subject_id,
            reason_code,
            reason,
            effective_from,
            expires_at,
            label,
        ) in rows
    ]
