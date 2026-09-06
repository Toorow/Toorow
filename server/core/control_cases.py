"""The Control Case lifecycle: one episode, many observations, one history.

Story 49.4 AC2. What this replaces is not a worse version of itself -- it is the
absence of an object. ``app.mapping_proposals`` was being projected as a Control
Case by :mod:`core.governance_read_model`, and the story says why that cannot
work: a proposal "lacks evidence episodes, impact and governed decision history".
It is a strong CANDIDATE artifact. A case is the thing a candidate is attached to.

Three properties carry the design:

* **Recurrence never overwrites history.** The rule is one sentence and it is
  enforced by a partial unique index rather than by convention: *at most one OPEN
  case per (Project, root cause)*. Observing the same root cause again appends an
  immutable occurrence to that case. Observing it after the case was closed opens
  a NEW case linked by ``supersedes_case_id``. Neither path edits anything.

* **The root cause is computed, not typed.** :func:`root_cause_fingerprint` is a
  pure function of the typed subject and the discriminating facts. Two producers
  that observe the same problem must land on the same case, and a producer that
  varies its wording must not create a second one -- which is exactly what
  ``dq_api``'s message-parsed identity could not guarantee.

* **A decision records what the owner actually did.** ``owner_outcome`` is stored
  alongside the decision, including ``failed``. A decision written as taken when
  the owner command failed is worse than no record, because it reads as done.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence

from core.governance_rule_sets import canonical_json, content_hash

logger = logging.getLogger(__name__)

SEVERITIES = ("blocking", "degrading", "informational")
STATUSES = ("open", "investigating", "decided", "resolved", "closed")
#: Statuses that still occupy the "one open case per root cause" slot.
OPEN_STATUSES = ("open", "investigating", "decided", "resolved")

OWNER_KINDS = ("data", "governance")
OWNER_OUTCOMES = ("succeeded", "failed", "not_applicable")


class ControlCaseError(ValueError):
    """A Control Case operation was rejected."""

    code = "invalid_control_case_operation"


class ControlCaseNotFound(ControlCaseError):
    """The addressed case does not exist in this Project."""

    code = "control_case_not_found"


class ControlCaseUnavailable(RuntimeError):
    """Evidence required to decide is unreadable; the caller must fail closed."""

    code = "control_case_evidence_unavailable"


def _require(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ControlCaseError(f"{label} is required")
    return value.strip()


def _mint(prefix: str) -> str:
    from ulid import ULID  # noqa: PLC0415

    return f"{prefix}_{ULID()}"


def root_cause_fingerprint(
    *,
    case_type: str,
    subject_kind: str,
    subject_id: str,
    discriminators: Mapping[str, Any] | None = None,
) -> str:
    """The deterministic identity of a problem, independent of how it was worded.

    PURE, so two producers observing the same thing land on the same case whether
    they run a minute or a month apart. ``discriminators`` carries the facts that
    make two problems on the same subject genuinely different -- the failing check
    name, the conflicting field -- and nothing else. Putting a timestamp, a run id
    or a message in there would mint a new case per observation, which is the
    recurrence bug in reverse.
    """

    return hashlib.sha256(
        canonical_json(
            {
                "case_type": _require(case_type, "case_type"),
                "subject_kind": _require(subject_kind, "subject_kind"),
                "subject_id": _require(subject_id, "subject_id"),
                "discriminators": dict(sorted((discriminators or {}).items())),
            }
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class CaseObservation:
    """One sighting. Immutable once written."""

    detected_by: str
    observation: Mapping[str, Any]
    evidence_refs: Sequence[Mapping[str, Any]] = ()


_CASE_COLUMNS = (
    "id",
    "org_id",
    "project_id",
    "case_type",
    "subject_kind",
    "subject_id",
    "root_cause_fingerprint",
    "severity",
    "status",
    "owner",
    "first_observed_at",
    "last_observed_at",
    "evidence_horizon_at",
    "supersedes_case_id",
    "created_by",
    "created_at",
    "updated_at",
)


def _case(row: Sequence[Any]) -> dict[str, Any]:
    return dict(zip(_CASE_COLUMNS, row))


def open_case_predicate(*, include_resolved: bool = True, alias: str | None = None) -> str:
    """The `WHERE` fragment for an open Control Case -- the ONE spelling of it.

    Two readings, one text, and the caller names its question rather than
    spelling the answer -- the same shape as
    :func:`core.dq_governance.open_issue_predicate`:

    * ``include_resolved=True`` is the RECURRENCE reading, and the one migration
      145's partial unique index enforces
      (``uq_control_cases_open_root_cause ... WHERE status <> 'closed'``): at
      most one such case per (Project, root cause). :func:`observe` must ask
      exactly this or its INSERT collides with the index, and :func:`open_cases`
      asks it too -- "a case resolved yesterday is still worth seeing, and
      hiding it is how a recurrence looks new".
    * ``include_resolved=False`` is the ATTENTION reading, which the Controls
      attention list asks because a resolved case is not something to act on
      today.

    Until 2026-08-21 those two readings were three hand-written clauses in two
    files, and the guard over them
    (`tests/core/test_data_surface.py::test_what_open_means_is_spelled_once...`)
    knew only ONE spelling of "closed" -- `NOT IN ('closed'` -- so the two
    `<> 'closed'` copies in this file were invisible to it and a rewrite to
    `!= 'closed'` would have been invisible too.

    `alias` prefixes every column reference (``alias="c"`` reads ``c.status``)
    so a reader that joins `app.control_cases` against another table carrying a
    `status` column stays unambiguous without respelling the predicate.
    """

    col = f"{alias}." if alias else ""
    if include_resolved:
        return f"{col}status <> 'closed'"
    return f"{col}status NOT IN ('closed', 'resolved')"


#: The bare-column recurrence reading, DERIVED from the builder above so the
#: module's noun and its callers can never drift apart.
OPEN_CASE_PREDICATE = open_case_predicate()


def observe(
    conn,
    *,
    org_id: str,
    project_id: str,
    case_type: str,
    subject_kind: str,
    subject_id: str,
    severity: str,
    observation: CaseObservation,
    discriminators: Mapping[str, Any] | None = None,
    actor: str = "system",
    evidence_horizon_at: Any = None,
) -> dict[str, Any]:
    """Record one sighting, opening or reopening exactly one case. Idempotent.

    Returns the case with ``occurrence_id`` and ``recurrence`` set to ``opened``,
    ``appended`` or ``replayed``. ``replayed`` means this exact observation was
    already recorded -- a producer that runs twice does not double the history.
    """

    if severity not in SEVERITIES:
        raise ControlCaseError(f"severity must be one of {list(SEVERITIES)}")
    fingerprint = root_cause_fingerprint(
        case_type=case_type,
        subject_kind=subject_kind,
        subject_id=subject_id,
        discriminators=discriminators,
    )
    occurrence_hash = content_hash(
        {
            "fingerprint": fingerprint,
            "detected_by": _require(observation.detected_by, "detected_by"),
            "observation": dict(observation.observation),
            "evidence_refs": [dict(item) for item in observation.evidence_refs],
        }
    )

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_CASE_COLUMNS)} FROM app.control_cases "
            "WHERE project_id = %s AND root_cause_fingerprint = %s "
            f"AND {open_case_predicate()}",
            (project_id, fingerprint),
        )
        row = cur.fetchone()

        if row is None:
            # No open case. If a CLOSED one exists for this root cause, the new
            # case links to the most recent -- so "this came back" is readable
            # rather than being two unrelated rows that happen to look alike.
            cur.execute(
                "SELECT id FROM app.control_cases WHERE project_id = %s "
                "AND root_cause_fingerprint = %s ORDER BY created_at DESC LIMIT 1",
                (project_id, fingerprint),
            )
            previous = cur.fetchone()
            case_id = _mint("ctc")
            cur.execute(
                """
                INSERT INTO app.control_cases
                    (id, org_id, project_id, case_type, subject_kind, subject_id,
                     root_cause_fingerprint, severity, status, evidence_horizon_at,
                     supersedes_case_id, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'open', %s, %s, %s)
                """,
                (
                    case_id,
                    _require(org_id, "org_id"),
                    _require(project_id, "project_id"),
                    _require(case_type, "case_type"),
                    _require(subject_kind, "subject_kind"),
                    _require(subject_id, "subject_id"),
                    fingerprint,
                    severity,
                    evidence_horizon_at,
                    previous[0] if previous else None,
                    _require(actor, "actor"),
                ),
            )
            recurrence = "opened"
        else:
            case_id = str(row[0])
            recurrence = "appended"
            cur.execute(
                "UPDATE app.control_cases SET last_observed_at = NOW(), "
                "severity = CASE WHEN %s = 'blocking' THEN 'blocking' ELSE severity END "
                "WHERE id = %s AND project_id = %s",
                (severity, case_id, project_id),
            )

        cur.execute(
            "SELECT id FROM app.control_case_occurrences "
            "WHERE case_id = %s AND content_hash = %s",
            (case_id, occurrence_hash),
        )
        existing = cur.fetchone()
        if existing:
            occurrence_id = str(existing[0])
            recurrence = "replayed"
        else:
            occurrence_id = _mint("ctco")
            cur.execute(
                """
                INSERT INTO app.control_case_occurrences
                    (id, case_id, project_id, detected_by, evidence_refs, observation,
                     content_hash)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
                """,
                (
                    occurrence_id,
                    case_id,
                    project_id,
                    observation.detected_by,
                    canonical_json([dict(item) for item in observation.evidence_refs]),
                    canonical_json(dict(observation.observation)),
                    occurrence_hash,
                ),
            )

        cur.execute(
            f"SELECT {', '.join(_CASE_COLUMNS)} FROM app.control_cases "
            "WHERE id = %s AND project_id = %s",
            (case_id, project_id),
        )
        stored = cur.fetchone()

    if stored is None:  # pragma: no cover -- only under a concurrent delete
        raise ControlCaseUnavailable("the case disappeared immediately after insert")
    return {**_case(stored), "occurrence_id": occurrence_id, "recurrence": recurrence}


def attach_candidate(
    conn,
    *,
    project_id: str,
    case_id: str,
    candidate_kind: str,
    owner_kind: str,
    owner_object_type: str,
    owner_object_id: str,
    snapshot: Mapping[str, Any],
    owner_version_id: str | None = None,
) -> str:
    """Point a case at an owner-held candidate. Idempotent by content.

    The candidate is REFERENCED, never copied into Governance: an immutable Data
    mapping proposal stays the Data owner's row, and this records that a case is
    about it. That distinction is what keeps Governance from becoming a second
    place where mappings live.
    """

    if owner_kind not in OWNER_KINDS:
        raise ControlCaseError(f"owner_kind must be one of {list(OWNER_KINDS)}")
    digest = content_hash(
        {
            "kind": candidate_kind,
            "owner": [owner_kind, owner_object_type, owner_object_id, owner_version_id],
            "snapshot": dict(snapshot),
        }
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.control_case_candidates WHERE case_id = %s AND content_hash = %s",
            (case_id, digest),
        )
        existing = cur.fetchone()
        if existing:
            return str(existing[0])
        candidate_id = _mint("ctcc")
        cur.execute(
            """
            INSERT INTO app.control_case_candidates
                (id, case_id, project_id, candidate_kind, owner_kind, owner_object_type,
                 owner_object_id, owner_version_id, snapshot, content_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            """,
            (
                candidate_id,
                case_id,
                project_id,
                _require(candidate_kind, "candidate_kind"),
                owner_kind,
                _require(owner_object_type, "owner_object_type"),
                _require(owner_object_id, "owner_object_id"),
                owner_version_id,
                canonical_json(dict(snapshot)),
                digest,
            ),
        )
    return candidate_id


def record_impact(
    conn,
    *,
    project_id: str,
    case_id: str,
    affected: Mapping[str, Any],
    coverage: Mapping[str, Any],
    dependency_fingerprint: str,
    candidate_id: str | None = None,
) -> str:
    """Record a SERVER-derived impact. Idempotent by content.

    The caller computes it from owner state; it is never accepted from a browser.
    An impact a client supplied describes what the client claimed, which is the
    class of defect Story 48.3 removed from the money calculators.
    """

    digest = content_hash(
        {
            "affected": dict(affected),
            "coverage": dict(coverage),
            "dependency_fingerprint": dependency_fingerprint,
            "candidate_id": candidate_id,
        }
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.control_case_impacts WHERE case_id = %s AND content_hash = %s",
            (case_id, digest),
        )
        existing = cur.fetchone()
        if existing:
            return str(existing[0])
        impact_id = _mint("ctci")
        cur.execute(
            """
            INSERT INTO app.control_case_impacts
                (id, case_id, candidate_id, project_id, affected, coverage,
                 dependency_fingerprint, content_hash)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
            """,
            (
                impact_id,
                case_id,
                candidate_id,
                project_id,
                canonical_json(dict(affected)),
                canonical_json(dict(coverage)),
                dependency_fingerprint,
                digest,
            ),
        )
    return impact_id


def decide(
    conn,
    *,
    project_id: str,
    case_id: str,
    decision_kind: str,
    actor: str,
    reason: str,
    effective_from: date,
    owner_outcome: str = "not_applicable",
    owner_result: Mapping[str, Any] | None = None,
    candidate_id: str | None = None,
    expires_at: date | None = None,
    confirmation_id: str | None = None,
    operation_id: str | None = None,
    audit_ref: str | None = None,
    new_status: str = "decided",
) -> str:
    """Append a decision and move the case. History is never rewritten.

    ``owner_outcome='failed'`` is a legitimate, recorded result: the decision was
    taken, the owner command was attempted, and it did not succeed. The case does
    NOT advance in that case -- a failed handoff that left the case looking
    decided is how a problem gets closed without being fixed.
    """

    if owner_outcome not in OWNER_OUTCOMES:
        raise ControlCaseError(f"owner_outcome must be one of {list(OWNER_OUTCOMES)}")
    if new_status not in STATUSES:
        raise ControlCaseError(f"status must be one of {list(STATUSES)}")
    if expires_at and expires_at < effective_from:
        raise ControlCaseError("expires_at cannot precede effective_from")

    decision_id = _mint("ctcd")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM app.control_cases WHERE id = %s AND project_id = %s",
            (case_id, project_id),
        )
        row = cur.fetchone()
        if row is None:
            raise ControlCaseNotFound(f"case {case_id} is not in this Project")
        cur.execute(
            """
            INSERT INTO app.control_case_decisions
                (id, case_id, project_id, decision_kind, candidate_id, actor, reason,
                 effective_from, expires_at, confirmation_id, operation_id, audit_ref,
                 owner_outcome, owner_result)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                decision_id,
                case_id,
                project_id,
                _require(decision_kind, "decision_kind"),
                candidate_id,
                _require(actor, "actor"),
                _require(reason, "reason"),
                effective_from,
                expires_at,
                confirmation_id,
                operation_id,
                audit_ref,
                owner_outcome,
                canonical_json(dict(owner_result or {})),
            ),
        )
        if owner_outcome != "failed":
            cur.execute(
                "UPDATE app.control_cases SET status = %s WHERE id = %s AND project_id = %s",
                (new_status, case_id, project_id),
            )
    return decision_id


def fetch_case(conn, *, project_id: str, case_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_CASE_COLUMNS)} FROM app.control_cases "
            "WHERE id = %s AND project_id = %s",
            (case_id, project_id),
        )
        row = cur.fetchone()
    return _case(row) if row else None


def case_history(conn, *, project_id: str, case_id: str) -> dict[str, list[dict[str, Any]]]:
    """The four append-only streams behind one case, newest first."""

    streams: dict[str, list[dict[str, Any]]] = {}
    queries = {
        "occurrences": (
            "SELECT id, observed_at, detected_by, evidence_refs, observation "
            "FROM app.control_case_occurrences WHERE case_id = %s AND project_id = %s "
            "ORDER BY observed_at DESC",
            ("id", "observed_at", "detected_by", "evidence_refs", "observation"),
        ),
        "candidates": (
            "SELECT id, candidate_kind, owner_kind, owner_object_type, owner_object_id, "
            "owner_version_id, snapshot, proposed_at "
            "FROM app.control_case_candidates WHERE case_id = %s AND project_id = %s "
            "ORDER BY proposed_at DESC",
            (
                "id",
                "candidate_kind",
                "owner_kind",
                "owner_object_type",
                "owner_object_id",
                "owner_version_id",
                "snapshot",
                "proposed_at",
            ),
        ),
        "impacts": (
            "SELECT id, candidate_id, affected, coverage, dependency_fingerprint, derived_at "
            "FROM app.control_case_impacts WHERE case_id = %s AND project_id = %s "
            "ORDER BY derived_at DESC",
            ("id", "candidate_id", "affected", "coverage", "dependency_fingerprint", "derived_at"),
        ),
        "decisions": (
            "SELECT id, decision_kind, candidate_id, actor, reason, effective_from, expires_at, "
            "owner_outcome, owner_result, decided_at "
            "FROM app.control_case_decisions WHERE case_id = %s AND project_id = %s "
            "ORDER BY decided_at DESC",
            (
                "id",
                "decision_kind",
                "candidate_id",
                "actor",
                "reason",
                "effective_from",
                "expires_at",
                "owner_outcome",
                "owner_result",
                "decided_at",
            ),
        ),
    }
    with conn.cursor() as cur:
        for name, (sql, columns) in queries.items():
            cur.execute(sql, (case_id, project_id))
            streams[name] = [dict(zip(columns, row)) for row in cur.fetchall()]
    return streams


def open_cases(
    conn, *, project_id: str, limit: int = 50
) -> list[dict[str, Any]]:
    """Unresolved cases for the Conflicts lens and the Overview attention adapter.

    Bounded, and ordered by severity then recency so the projection does not have
    to re-rank. `closed` is excluded; `resolved` is not -- a case resolved
    yesterday is still worth seeing, and hiding it is how a recurrence looks new.
    """

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {', '.join(_CASE_COLUMNS)} FROM app.control_cases
            WHERE project_id = %s AND {open_case_predicate()}
            ORDER BY CASE severity WHEN 'blocking' THEN 0 WHEN 'degrading' THEN 1 ELSE 2 END,
                     last_observed_at DESC
            LIMIT %s
            """,
            (project_id, max(1, min(limit, 200))),
        )
        return [_case(row) for row in cur.fetchall()]
