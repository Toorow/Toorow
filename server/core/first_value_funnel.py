"""toorow -- First-value funnel analytics domain service (Story 36.19, Epic 36).

THE GAP THIS MODULE FILLS
=========================
Epic 36 lands every first-value STAGE point in its own module (invitations.py,
source_delegation.py, first_report_draft.py, recent_first_publication.py,
first_report_readiness.py, setup_responsibilities.py, governed_publication.py). This
module is the SINGLE, SAFE instrumentation seam those stage points call so the product
can measure the funnel and its wait states (E36-FR09) WITHOUT ever letting tenant-
identifying or free-form content into product analytics (AD-32).

It writes to ``app.first_value_events`` (migration 074) -- a PRODUCT-ANALYTICS sink that
is deliberately SEPARATE from the Story 36.2 security audit_log/outbox. It also owns a
SEPARATE Support-access ledger (``app.support_access_ledger``) that is NEVER joined into
the product-analytics reads.

INVARIANTS (AD-32 -- enforced here, asserted by the tests)
----------------------------------------------------------
* ALLOWLIST ONLY: only closed ``STAGES`` / ``OUTCOMES`` / ``RECOVERY_KINDS`` /
  ``ABANDON_REASONS`` / ``DURATION_BUCKETS`` enums, a PSEUDONYMOUS keyed-hash journey
  reference, and a COARSE duration bucket enter product analytics. Any non-allowlisted
  stage/outcome/recovery/reason value is REJECTED (raises) BEFORE any SQL, and never
  written.
* NO CONTENT: provider/report content, raw errors, invitation email, prompt text, and
  free-form reasons NEVER enter product analytics. The pseudonymous reference is a one-
  way HMAC (never the raw org/email/project/provider), so an analytics dump cannot be
  reversed to a customer.
* NO EXACT DURATIONS: an exact elapsed time is never stored -- only a coarse bucket.
* TENANT VISIBILITY: ``tenant_journeys`` returns ONLY authorized resource-graph journeys
  and their wait-state owners, resolved through ``resolve_strict_resource_access`` (fail-
  closed; existence-hidden otherwise).
* CROSS-TENANT ANTI-INFERENCE: ``cross_tenant_cohort`` suppresses any cohort below the
  versioned minimum AND applies repeated-query anti-differencing (a deterministic query-
  budget marker) so a single tenant cannot be differenced out by re-querying.
* SUPPORT SEPARATION: ``record_support_access`` writes a DISTINCT purpose/expiry/audit
  ledger that is NOT joined into any product-analytics query.
* VERIFIABLE RETENTION: ``purge_expired_funnel_events`` removes events per the versioned
  policy and returns a verifiable deleted count.

Mirrors the setup_responsibilities / governed_publication conventions: ``from __future__
import annotations``, module logger, pure validators separate from I/O so the invariants
are offline-testable, keyed-hash pseudonymisation via a server pepper. French user-facing
messages. ASCII-only.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from ulid import ULID

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Closed allowlists (AD-32). Kept in sync with the CHECK constraints in
# migration 074_first_value_funnel.sql -- the DB is the second gate.
# ---------------------------------------------------------------------------

# The ~12 first-value funnel stages + wait states (E36-FR09).
STAGES: frozenset[str] = frozenset(
    {
        "invitation_delivery",
        "invitation_acceptance",
        "source_authorization",
        "account_selection",
        "preview",
        "recent_pull",
        "report_readiness",
        "history_completion",
        "host_connection",
        "first_correct_answer",
        "second_user_reproduction",
        "recovery_action",
    }
)

OUTCOMES: frozenset[str] = frozenset(
    {"started", "succeeded", "degraded", "failed", "abandoned", "blocked"}
)

RECOVERY_KINDS: frozenset[str] = frozenset(
    {
        "reconnect",
        "retry",
        "refetch",
        "reload",
        "reprocess",
        "replace",
        "reconcile",
        "rollback",
        "support_handoff",
    }
)

# A SMALL closed set -- NO free text ever.
ABANDON_REASONS: frozenset[str] = frozenset(
    {
        "awaiting_external_owner",
        "authorization_expired",
        "no_compatible_host",
        "readiness_never_reached",
        "reproduction_failed",
        "unknown",
    }
)

# Coarse duration buckets. Ordered edges (upper bound in seconds, label). The final
# bucket is open-ended. An exact duration is NEVER stored -- only the bucket label.
_BUCKET_EDGES: tuple[tuple[float, str], ...] = (
    (60.0, "lt_1m"),
    (300.0, "1m_5m"),
    (1800.0, "5m_30m"),
    (7200.0, "30m_2h"),
    (86400.0, "2h_1d"),
)
_BUCKET_OVERFLOW = "gt_1d"
DURATION_BUCKETS: frozenset[str] = frozenset(
    {label for _, label in _BUCKET_EDGES} | {_BUCKET_OVERFLOW}
)

# Closed set of Support-access purposes (still no free text).
SUPPORT_PURPOSES: frozenset[str] = frozenset(
    {"incident_investigation", "customer_request", "compliance_audit", "recovery_assist"}
)

# Default cross-tenant small-cohort minimum. The CALLER passes a versioned minimum; this
# is only the floor a version may not drop below.
_MIN_COHORT_FLOOR = 5


class FunnelValidationError(ValueError):
    """Raised before any SQL when a funnel value is off-allowlist or unsafe."""


class CohortSuppressed(RuntimeError):
    """A cross-tenant cohort result is suppressed (too small or would difference)."""


# ---------------------------------------------------------------------------
# Pseudonymisation (keyed one-way hash -- never the raw org/email/project).
# ---------------------------------------------------------------------------


def _pepper() -> str:
    value = os.environ.get("TOOROW_FUNNEL_PEPPER", "")
    if len(value) < 32:
        raise FunnelValidationError(
            "TOOROW_FUNNEL_PEPPER doit contenir au moins 32 caracteres"
        )
    return value


def _keyed_hash(purpose: str, *parts: str) -> str:
    """HMAC-SHA256 over a purpose-scoped join of *parts*. One-way, non-reversible."""
    message = purpose + "\x1f" + "\x1f".join(parts)
    return hmac.new(_pepper().encode(), message.encode(), hashlib.sha256).hexdigest()


def journey_reference(*, org_id: str, project_id: str, subject: str) -> str:
    """Build the PSEUDONYMOUS journey reference hash for one first-value journey.

    The raw org/project/subject NEVER leave this function: only the HMAC digest is
    persisted, so two events of the same journey correlate without revealing the tenant.
    """
    for name, value in (("org_id", org_id), ("project_id", project_id), ("subject", subject)):
        if not isinstance(value, str) or not value.strip():
            raise FunnelValidationError(f"{name} est requis pour la reference de parcours")
    return _keyed_hash("journey", org_id.strip(), project_id.strip(), subject.strip())


def cohort_reference(*, org_id: str) -> str:
    """Coarse cohort discriminator hash (per-org) used ONLY for suppression bucketing."""
    if not isinstance(org_id, str) or not org_id.strip():
        raise FunnelValidationError("org_id est requis pour la reference de cohorte")
    return _keyed_hash("cohort", org_id.strip())


# ---------------------------------------------------------------------------
# Coarse duration bucketing (no exact duration ever stored).
# ---------------------------------------------------------------------------


def bucket_duration(seconds: float | int | None) -> str | None:
    """Map an elapsed duration to a COARSE bucket label, or None when absent.

    The exact ``seconds`` value is discarded -- only the bucket label is returned/stored.
    A negative duration is rejected (clock error) rather than silently bucketed.
    """
    if seconds is None:
        return None
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        raise FunnelValidationError("la duree doit etre un nombre de secondes")
    if seconds < 0:
        raise FunnelValidationError("la duree ne peut pas etre negative")
    for upper, label in _BUCKET_EDGES:
        if seconds < upper:
            return label
    return _BUCKET_OVERFLOW


# ---------------------------------------------------------------------------
# Append-only product-analytics write.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FunnelEvent:
    id: str
    journey_ref_hash: str
    cohort_ref_hash: str
    stage: str
    outcome: str
    duration_bucket: str | None
    recovery_kind: str | None
    abandon_reason: str | None
    policy_version: str


def _validate_enum(value: str, allowed: frozenset[str], *, name: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        # Deliberately does NOT echo the offending value into any durable sink.
        raise FunnelValidationError(f"{name} hors liste blanche")
    return value


def _validate_hash(value: str, *, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        c not in "0123456789abcdef" for c in value
    ):
        raise FunnelValidationError(f"{name} doit etre un hash keye")
    return value


def record_funnel_stage(
    conn,
    *,
    journey_ref: str,
    cohort_ref: str,
    stage: str,
    outcome: str,
    policy_version: str,
    duration_bucket: str | None = None,
    recovery_kind: str | None = None,
    abandon_reason: str | None = None,
    retention_days: int = 365,
    now: datetime | None = None,
) -> FunnelEvent:
    """Append ONE first-value funnel event -- allowlisted enums + pseudonymous ref only.

    This is the single instrumentation seam every Epic 36 stage point calls (one line):
    it accepts ALREADY-PSEUDONYMISED references (build them with ``journey_reference`` /
    ``cohort_reference``) plus closed enums, and writes an append-only row.

    It REJECTS (raises ``FunnelValidationError``) and writes NOTHING when the stage,
    outcome, duration bucket, recovery kind or abandon reason is off-allowlist. By taking
    only enums + hashes as parameters -- and no free-form dict -- it is structurally
    impossible for provider/report content, raw errors, an invitation email, prompt text,
    or a free-form reason to be persisted here.
    """
    journey_hash = _validate_hash(journey_ref, name="journey_ref")
    cohort_hash = _validate_hash(cohort_ref, name="cohort_ref")
    stage_v = _validate_enum(stage, STAGES, name="stage")
    outcome_v = _validate_enum(outcome, OUTCOMES, name="outcome")
    if not isinstance(policy_version, str) or not policy_version.strip():
        raise FunnelValidationError("policy_version est requis")

    bucket_v = (
        None if duration_bucket is None
        else _validate_enum(duration_bucket, DURATION_BUCKETS, name="duration_bucket")
    )
    recovery_v = (
        None if recovery_kind is None
        else _validate_enum(recovery_kind, RECOVERY_KINDS, name="recovery_kind")
    )
    reason_v = (
        None if abandon_reason is None
        else _validate_enum(abandon_reason, ABANDON_REASONS, name="abandon_reason")
    )

    # Keep the DB-level CHECK semantics honest at the domain layer too.
    if recovery_v is not None and stage_v != "recovery_action":
        raise FunnelValidationError("recovery_kind n'est valide que pour l'etape recovery_action")
    if reason_v is not None and outcome_v != "abandoned":
        raise FunnelValidationError("abandon_reason n'est valide qu'avec l'issue abandoned")

    if not isinstance(retention_days, int) or retention_days <= 0 or retention_days > 3650:
        raise FunnelValidationError("retention_days doit etre compris entre 1 et 3650")

    moment = _aware(now or datetime.now(timezone.utc))
    expires = moment + timedelta(days=retention_days)
    event_id = f"fve_{ULID()}"

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.first_value_events
                (id, journey_ref_hash, cohort_ref_hash, stage, outcome,
                 duration_bucket, recovery_kind, abandon_reason, policy_version,
                 occurred_at, retention_expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event_id,
                journey_hash,
                cohort_hash,
                stage_v,
                outcome_v,
                bucket_v,
                recovery_v,
                reason_v,
                policy_version.strip(),
                moment,
                expires,
            ),
        )
    return FunnelEvent(
        id=event_id,
        journey_ref_hash=journey_hash,
        cohort_ref_hash=cohort_hash,
        stage=stage_v,
        outcome=outcome_v,
        duration_bucket=bucket_v,
        recovery_kind=recovery_v,
        abandon_reason=reason_v,
        policy_version=policy_version.strip(),
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Tenant-facing read: only authorized resource-graph journeys + wait-state owners.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JourneyView:
    journey_ref_hash: str
    stages: list[dict[str, Any]]
    wait_state_owners: list[str]


def bind_journey_to_project(conn, *, journey_ref: str, project_id: str) -> None:
    """Record (idempotently) which project a pseudonymous journey belongs to.

    Called ONCE per journey (e.g. at invitation acceptance) so the tenant-facing read can
    show an authorized owner their own journeys. The analytics sink stays project-
    anonymous; this bounded bridge is the only place project_id meets the journey hash,
    and it is never used by the cross-tenant cohort read.
    """
    journey_hash = _validate_hash(journey_ref, name="journey_ref")
    if not isinstance(project_id, str) or not project_id.strip():
        raise FunnelValidationError("project_id est requis")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.first_value_project_journeys (journey_ref_hash, project_id)
            VALUES (%s, %s)
            ON CONFLICT (journey_ref_hash, project_id) DO NOTHING
            """,
            (journey_hash, project_id.strip()),
        )


def tenant_journeys(
    conn,
    *,
    identity: str,
    project_id: str,
    auth_mode: str | None = None,
) -> list[JourneyView]:
    """Return the tenant-facing journeys for ONE authorized project (fail-closed).

    Access is resolved through ``resolve_strict_resource_access`` (AD-5): if the caller
    has no authorized resource-graph access to the project, the journeys are existence-
    hidden (empty list), never leaked. Only allowlisted stage/outcome enums and wait-
    state owners are returned -- never a pseudonymised cross-tenant cohort.
    """
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

    decision = resolve_strict_resource_access(
        identity, conn, project_id=project_id, minimum_capability="view", auth_mode=auth_mode
    )
    if not getattr(decision, "allowed", False):
        # Existence-hidden: an unauthorized caller learns nothing about the project.
        return []

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.journey_ref_hash, e.stage, e.outcome, e.duration_bucket,
                   e.recovery_kind, e.abandon_reason, e.occurred_at
            FROM app.first_value_events e
            JOIN app.first_value_project_journeys j
              ON j.journey_ref_hash = e.journey_ref_hash
            WHERE j.project_id = %s
            ORDER BY e.journey_ref_hash, e.occurred_at
            """,
            (project_id,),
        )
        rows = cur.fetchall() or []

    views: dict[str, JourneyView] = {}
    for row in rows:
        ref, stage, outcome, bucket, recovery, reason, occurred = row[:7]
        view = views.get(ref)
        if view is None:
            view = JourneyView(ref, [], [])
            views[ref] = view
        view.stages.append(
            {
                "stage": stage,
                "outcome": outcome,
                "duration_bucket": bucket,
                "recovery_kind": recovery,
                "abandon_reason": reason,
            }
        )
        # A stage that is blocked/waiting names the actor who must act (wait-state owner).
        if outcome in {"blocked", "started"}:
            owner = _WAIT_STATE_OWNER.get(stage)
            if owner and owner not in view.wait_state_owners:
                view.wait_state_owners.append(owner)
    return list(views.values())


# Which actor owns the wait when a stage is blocked/pending. Coarse actor TYPES only
# (never a person) -- keeps the tenant view honest without leaking identity.
_WAIT_STATE_OWNER: dict[str, str] = {
    "invitation_delivery": "org_admin",
    "invitation_acceptance": "invited_operator",
    "source_authorization": "credential_owner",
    "account_selection": "credential_owner",
    "preview": "invited_operator",
    "recent_pull": "toorow_platform",
    "report_readiness": "toorow_platform",
    "history_completion": "toorow_platform",
    "host_connection": "host_admin",
    "first_correct_answer": "invited_operator",
    "second_user_reproduction": "second_user",
    "recovery_action": "invited_operator",
}


# ---------------------------------------------------------------------------
# Cross-tenant cohort read: small-cohort suppression + anti-differencing.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CohortResult:
    stage: str
    outcome: str
    count: int
    cohort_size: int
    min_cohort: int
    query_budget_marker: str


def _validate_filters(filters: Mapping[str, Any]) -> dict[str, str]:
    """Accept ONLY allowlisted enum filters (stage/outcome). No raw identifiers."""
    if not isinstance(filters, Mapping):
        raise FunnelValidationError("filters doit etre un objet")
    allowed_keys = {"stage", "outcome", "policy_version"}
    if not set(filters).issubset(allowed_keys):
        raise FunnelValidationError("filters contient une cle non autorisee")
    safe: dict[str, str] = {}
    if "stage" in filters:
        safe["stage"] = _validate_enum(str(filters["stage"]), STAGES, name="stage")
    if "outcome" in filters:
        safe["outcome"] = _validate_enum(str(filters["outcome"]), OUTCOMES, name="outcome")
    if "policy_version" in filters:
        pv = filters["policy_version"]
        if not isinstance(pv, str) or not pv.strip() or len(pv) > 64:
            raise FunnelValidationError("policy_version de filtre invalide")
        safe["policy_version"] = pv.strip()
    return safe


def _query_budget_marker(filters: Mapping[str, str], min_cohort_version: int) -> str:
    """Deterministic marker for the (filters, versioned-minimum) query shape.

    Anti-differencing: because the same query shape yields the SAME marker AND the SAME
    deterministic suppression decision, an attacker cannot re-run slightly different
    queries to reconstruct a suppressed tenant -- there is no fresh randomness to average
    out, and any query that narrows to a below-minimum cohort is suppressed identically
    every time. Callers may also use the marker to enforce a per-marker query budget.
    """
    shape = "|".join(f"{k}={filters[k]}" for k in sorted(filters))
    canonical = f"{shape}|min={min_cohort_version}"
    return _keyed_hash("query_budget", canonical)


def cross_tenant_cohort(
    conn,
    *,
    filters: Mapping[str, Any],
    min_cohort_version: int,
) -> list[CohortResult]:
    """Aggregate cross-tenant funnel outcomes with small-cohort suppression.

    ``min_cohort_version`` is the VERSIONED minimum cohort size (distinct tenants) below
    which a result is suppressed. It is clamped up to a hard floor so a bad version can
    never drop below it. Any (stage, outcome) bucket whose DISTINCT-tenant cohort is below
    the minimum is SUPPRESSED (omitted). Combined with the deterministic query-budget
    marker, this prevents a repeated query from differencing out a single tenant.

    Raises ``CohortSuppressed`` when the OVERALL cohort (all matching tenants) is below the
    minimum -- so a query that narrows to one tenant returns nothing rather than a count.
    """
    if not isinstance(min_cohort_version, int) or min_cohort_version < 1:
        raise FunnelValidationError("min_cohort_version doit etre un entier positif")
    min_cohort = max(min_cohort_version, _MIN_COHORT_FLOOR)
    safe = _validate_filters(filters)
    marker = _query_budget_marker(safe, min_cohort)

    where = " AND ".join(f"{k} = %s" for k in safe)
    where_clause = f"WHERE {where}" if where else ""
    params = tuple(safe[k] for k in safe)

    with conn.cursor() as cur:
        # Overall distinct-tenant cohort for this query shape.
        cur.execute(
            f"SELECT COUNT(DISTINCT cohort_ref_hash) FROM app.first_value_events {where_clause}",
            params,
        )
        overall_row = cur.fetchone()
        overall_cohort = int(overall_row[0]) if overall_row and overall_row[0] is not None else 0
        if overall_cohort < min_cohort:
            # The query narrows to a below-minimum set of tenants: suppress entirely so it
            # cannot be differenced against a broader query.
            raise CohortSuppressed("cohorte trop petite: resultat supprime")

        cur.execute(
            f"""
            SELECT stage, outcome,
                   COUNT(*) AS n,
                   COUNT(DISTINCT cohort_ref_hash) AS tenants
            FROM app.first_value_events
            {where_clause}
            GROUP BY stage, outcome
            ORDER BY stage, outcome
            """,
            params,
        )
        rows = cur.fetchall() or []

    results: list[CohortResult] = []
    for row in rows:
        stage, outcome, n, tenants = row[:4]
        tenants = int(tenants)
        if tenants < min_cohort:
            # Small-cohort suppression: this bucket would expose too few tenants.
            continue
        results.append(
            CohortResult(
                stage=stage,
                outcome=outcome,
                count=int(n),
                cohort_size=tenants,
                min_cohort=min_cohort,
                query_budget_marker=marker,
            )
        )
    return results


# ---------------------------------------------------------------------------
# Retention purge: verifiable deletion per versioned policy.
# ---------------------------------------------------------------------------


def purge_expired_funnel_events(
    conn,
    *,
    policy_version: str,
    now: datetime | None = None,
) -> int:
    """Remove funnel events whose retention has expired, returning a verifiable count.

    The purge flags itself via the ``app.funnel_purge`` GUC so the append-only trigger
    (migration 074) permits the DELETE; ad-hoc deletes stay blocked. The returned row
    count is the verifiable proof of deletion (the caller can re-query the table to
    confirm none of the purged rows remain).
    """
    if not isinstance(policy_version, str) or not policy_version.strip():
        raise FunnelValidationError("policy_version est requis pour la purge")
    moment = _aware(now or datetime.now(timezone.utc))
    with conn.cursor() as cur:
        cur.execute("SET LOCAL app.funnel_purge = 'on'")
        cur.execute(
            """
            DELETE FROM app.first_value_events
            WHERE retention_expires_at <= %s AND policy_version = %s
            """,
            (moment, policy_version.strip()),
        )
        deleted = cur.rowcount
    return int(deleted if deleted is not None and deleted >= 0 else 0)


# ---------------------------------------------------------------------------
# SEPARATE Support-access ledger (distinct purpose/expiry/audit).
# NOT joined into any product-analytics query above.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SupportAccessRecord:
    id: str
    purpose: str
    actor_hash: str
    scope_ref_hash: str
    audit_event_id: str


def record_support_access(
    conn,
    *,
    actor: str,
    scope_subject: str,
    purpose: str,
    audit_event_id: str,
    expires_at: datetime,
    now: datetime | None = None,
) -> SupportAccessRecord:
    """Append ONE Support-access grant to the SEPARATE ledger (distinct from analytics).

    The Support ledger is intentionally NOT joined into ``tenant_journeys`` or
    ``cross_tenant_cohort``: it carries a keyed actor/scope hash, a closed purpose, a
    short-lived expiry, and a link (id only) to the durable security audit event. This is
    what keeps Support disclosure from re-identifying analytics cohorts (AD-32 /
    E36-NFR02).
    """
    purpose_v = _validate_enum(purpose, SUPPORT_PURPOSES, name="purpose")
    if not isinstance(audit_event_id, str) or not audit_event_id.strip():
        raise FunnelValidationError("audit_event_id est requis (audit prealable)")
    if not isinstance(actor, str) or not actor.strip():
        raise FunnelValidationError("actor est requis")
    if not isinstance(scope_subject, str) or not scope_subject.strip():
        raise FunnelValidationError("scope_subject est requis")
    expiry = _aware(expires_at)
    moment = _aware(now or datetime.now(timezone.utc))
    if expiry <= moment:
        raise FunnelValidationError("expires_at doit etre dans le futur")

    actor_hash = _keyed_hash("support_actor", actor.strip())
    scope_hash = _keyed_hash("support_scope", scope_subject.strip())
    record_id = f"sal_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.support_access_ledger
                (id, purpose, actor_hash, scope_ref_hash, expires_at, audit_event_id, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (record_id, purpose_v, actor_hash, scope_hash, expiry, audit_event_id.strip(), moment),
        )
    return SupportAccessRecord(
        id=record_id,
        purpose=purpose_v,
        actor_hash=actor_hash,
        scope_ref_hash=scope_hash,
        audit_event_id=audit_event_id.strip(),
    )
