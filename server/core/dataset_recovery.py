"""toorow -- Safe whole-dataset replace / append / rollback (Story 12.12, Epic 12).

THE GAP THIS MODULE FILLS
=========================
Story 12.5 (``datastream_publication``) already owns REPLACE end to end: the
atomic 4-write ``commit_publication`` pointer swap over
``app.datastreams.current_published_execution_id`` (locked ``SELECT ... FOR
UPDATE``, rollback-on-error, NEVER truncate-first), the ``create_execution``
concurrency guard (one non-terminal execution per datastream), the append-only
``app.datastream_publication_log`` (which records the prior pointer as rollback
EVIDENCE), and the DQ gates. Story 12.12 adds the THREE genuinely missing pieces
and REUSES 12.5 for everything else:

  1. **Dataset-pointer ROLLBACK** -- re-point ``current_published_execution_id``
     back to a RETAINED healthy prior published execution (the evidence lives in
     ``datastream_publication_log.prior_execution_id``). It is a SINGLE
     transaction: lock the pointer ``FOR UPDATE``, verify the same semantic + DQ
     gates still pass for the target, swap the pointer BACK, and INSERT a NEW
     append-only publication-log row (a rollback is a NEW row -- never a mutation
     of history). A rollback DEADLINE/WINDOW (migration 081) DISABLES the action
     once expired, with a clear explanation. This is the piece 12.5 did not have.

  2. **APPEND availability gate** -- Append is UNAVAILABLE unless a stable key +
     dedup contract + compatible schema exist (``datastreams.append_stable_key``).
     When unavailable, Replace is presented as the safe supported action (mirrors
     the 12.8/12.9 replace-only default). This module NEVER performs an unsafe
     append -- it only decides availability and routes to Replace.

  3. **RBAC owner-floor** on destination-policy operations (ownership / access /
     retention / irreversible deletion): Viewers are read-only, Members may
     perform recoverable actions (Replace, Append-when-available, Rollback), and
     Owners ALONE change those specific policies. Enforced through the strict AD-5
     seam (``project_access.resolve_strict_resource_access`` with
     ``minimum_capability='manage'``) plus the legacy per-project role floor.

CRITICAL DISTINCTION (do NOT confuse with Story 36.18)
------------------------------------------------------
``governed_publication.rollback_publication`` (Story 36.18, migration 073) rolls
back the MAPPING-version pointer (``current_mapping_version_id`` /
``datastream_mapping_publication_log``). THIS module rolls back the DATASET
EXECUTION pointer (``current_published_execution_id`` /
``datastream_publication_log``). They are different pointers, different logs,
different concerns. This module NEVER touches the mapping pointer, and 36.18 never
touches the execution pointer. The tests assert the two are distinct.

INVARIANTS (held HARD, asserted by the tests)
---------------------------------------------
  * ATOMIC pointer swap-BACK: the rollback locks the pointer row ``FOR UPDATE`` and
    swaps + logs in ONE transaction; any error rolls back the whole group leaving
    the prior published pointer intact (never truncate-first). Reuses the exact
    contract of ``commit_publication``.
  * GATED (M1 -- honest scope): the rollback target is run through
    ``datastream_publication.evaluate_dq_gates`` before the pointer moves, but on a
    pointer-swap rollback the ONLY gate that BLOCKS is ``empty_candidate`` (a zero-row
    target is refused; non-overridable). The row-count-delta gate is evaluated but
    APPROVED (a rollback to an already-validated prior version is Owner-authorized), and
    the content-hash gate is the advisory-only v1 marker. The mapping-drift and
    landing-schema gates do NOT fire because a rollback deliberately does not re-probe
    the live source (AD-8) -- there is no honest "current" fingerprint to compare, so we
    do not fake a self-comparison. A stored mapping version is still REQUIRED (the
    provenance guard fails closed on a target lacking one). Fail closed.
  * DEADLINE (C1): a rollback past the target's EFFECTIVE deadline is DISABLED
    (``rollback_window_expired``) with an honest explanation. The effective deadline is
    the stored ``rollback_deadline`` when present ELSE ``published_at`` + the RESOLVED
    project window (``COALESCE(rollback_deadline, published_at + window)``). Because the
    reused 12.5 forward-publish path leaves ``rollback_deadline`` NULL on every normal
    publish, the window is enforced via the resolved fallback -- it is NEVER a no-op
    that leaves data "rollbackable forever". Fails CLOSED when neither a stored deadline
    nor a resolvable ``published_at`` exists. The window is project-preference governed
    with a documented default; ``deadline_source`` names the value that actually gated.
  * IDEMPOTENCY (H1): a default (target-less) rollback is idempotent. When the current
    pointer already equals the resolved target the call short-circuits to a stable
    "already at target" no-op (no pointer swap, no new log row), so a RETRY cannot
    oscillate the pointer by re-publishing the version just rolled away.
  * APPEND SAFE-BY-DEFAULT: no stable-key contract => Append UNAVAILABLE => Replace.
  * EMPTY REPLACE: blocked by default; an intentional empty publication needs a
    SECOND explicit confirmation AND the ``allow_empty_publication`` preference
    (reuses the 12.5/12.8 empty gate).
  * CONCURRENCY (M2 -- honest scope): the rollback locks the pointer row ``FOR
    UPDATE`` and rejects when a NON-terminal execution is active, so a rollback races
    neither a concurrent rollback (both serialize on the pointer lock) nor an in-flight
    replace/append that has already MINTED an execution. RESIDUAL WINDOW: a replace/
    append whose ``create_execution`` has NOT yet inserted its active-state row is not
    blocked by our active-execution probe (``create_execution`` does not itself take
    the ``app.datastreams`` row lock). The structural ``uq_datastream_executions_active``
    index still prevents two concurrent active executions, but a replace that mints its
    execution in the microsecond AFTER our probe and BEFORE our pointer swap is a
    best-effort case, not a hard serialization. The proper fix is to make
    ``create_execution`` take ``SELECT ... FOR UPDATE`` on the ``app.datastreams``
    pointer row -- flagged in the INTEGRATION SPEC (Phase-B), NOT overclaimed here.
  * APPEND-ONLY history: the rollback writes a NEW ``datastream_publication_log``
    row; it never mutates the row that originally published the rolled-back-from
    version.
  * AD-2 source-agnostic: no provider name / field name / ``server/modules/*``
    import. Pure Postgres orchestration over the execution pointer + preferences.

Mirrors the datastream_publication / datastream_recovery conventions:
``from __future__ import annotations``, module logger, lazy ``core.*`` imports
inside function bodies (no import cycle), pure helpers separated from I/O so the
invariants are offline-testable.
"""

from __future__ import annotations

import logging
from typing import Any

from ulid import ULID

logger = logging.getLogger(__name__)

# The documented default rollback window when the project sets no preference.
# 30 days == 720 hours. NEVER a silent platform-wide hardcode: the recovery result
# records ``deadline_source`` so the fallback is observable ("prefer per-project
# preferences"). Mirrors 12.5's DEFAULT_MAX_ROW_COUNT_DELTA_PCT pattern.
DEFAULT_ROLLBACK_WINDOW_HOURS = 720

# The two recoverable mutation kinds this module reasons about. Rollback is a
# DISTINCT operation namespace from a forward replace so an idempotent retry of one
# never collides with the other (mirrors governed_publication's COMMAND split).
ACTION_REPLACE = "dataset.replace"
ACTION_APPEND = "dataset.append"
ACTION_ROLLBACK = "dataset.rollback"

# L1: a DISTINCT audit verb for a dataset rollback so it is queryable separately from
# a forward publish (app.audit_log.action is free-text -- no CHECK constraint -- so a
# new verb is cheap and safe). The forward publish uses core.audit's
# ACTION_DATASTREAM_PUBLISHED ("datastream.published"); a rollback is a different event.
AUDIT_ACTION_DATASET_ROLLED_BACK = "datastream.rolled_back"

# Destination-policy operations that require the Owner floor (AC: Owners ALONE
# change ownership / access / retention / irreversible deletion policy). Recoverable
# data actions (replace/append/rollback) require only Member.
OWNER_FLOOR_OPERATIONS = frozenset(
    {
        "change_ownership",
        "change_access",
        "change_retention",
        "irreversible_deletion",
    }
)


class DatasetRecoveryError(Exception):
    """Base for dataset-recovery errors carrying a stable ``code``."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


class RollbackWindowExpired(DatasetRecoveryError):
    """The rollback deadline has passed -- the action is DISABLED (with reason)."""

    def __init__(self, deadline: str | None, deadline_source: str) -> None:
        super().__init__(
            "rollback_window_expired",
            f"rollback deadline {deadline} has passed (source={deadline_source})",
        )
        self.deadline = deadline
        self.deadline_source = deadline_source


class RollbackTargetInvalid(DatasetRecoveryError):
    """The requested rollback target is not a retained, healthy prior published one."""

    def __init__(self, detail: str = "") -> None:
        super().__init__("rollback_target_invalid", detail)


class RollbackGateFailed(DatasetRecoveryError):
    """The rollback target did not pass the semantic + DQ gates (fail closed)."""

    def __init__(self, issues: list[dict[str, Any]]) -> None:
        super().__init__("rollback_gate_failed")
        self.issues = issues


class AppendUnavailable(DatasetRecoveryError):
    """Append is unavailable (no stable-key contract) -- Replace is the safe action."""

    def __init__(self, reason: str) -> None:
        super().__init__("append_unavailable", reason)
        self.reason = reason


class EmptyReplacementBlocked(DatasetRecoveryError):
    """An empty replacement was blocked (needs 2nd confirm + empty-publication pref)."""

    def __init__(self) -> None:
        super().__init__(
            "empty_replacement_blocked",
            "an empty replacement requires a second explicit confirmation AND the "
            "allow_empty_publication project preference",
        )


class ConcurrentMutationActive(DatasetRecoveryError):
    """Another mutation on the same destination is active -- serialized/rejected."""

    def __init__(self, blocking_execution_id: str, lock_reason: str) -> None:
        super().__init__(
            "concurrent_mutation_active",
            f"blocking_execution_id={blocking_execution_id} lock_reason={lock_reason}",
        )
        self.blocking_execution_id = blocking_execution_id
        self.lock_reason = lock_reason


class OwnerFloorRequired(DatasetRecoveryError):
    """A destination-policy operation was attempted below the Owner floor."""

    def __init__(self, operation: str) -> None:
        super().__init__(
            "owner_floor_required",
            f"operation '{operation}' changes destination ownership/access/retention/"
            "deletion policy and requires the Owner role",
        )
        self.operation = operation


class RollbackTargetNotFound(DatasetRecoveryError):
    """No retained healthy prior published execution exists to roll back to."""

    def __init__(self) -> None:
        super().__init__(
            "rollback_target_not_found",
            "no retained healthy prior published execution is available",
        )


def _mint_log_id() -> str:
    return f"dplog_{ULID()}"


# ---------------------------------------------------------------------------
# PURE decision helpers (no DB) -- exercised by the offline unit tests.
# ---------------------------------------------------------------------------


def resolve_rollback_window_hours(
    preferences: dict[str, Any] | None,
) -> tuple[int, str]:
    """Resolve the project-governed rollback window in hours + its source.

    Reads ``rollback_window_hours`` from the project-scoped preference dict. Falls
    back to the DOCUMENTED default (720h) when unset, recording the ``window_source``
    so the fallback is never silent ("prefer per-project preferences"). ``0`` is a
    valid preference: it means "no rollback window" (rollback disabled immediately).

    Returns ``(window_hours, window_source)`` where ``window_source`` is
    ``"project_preference"`` or ``"documented_default"``. The overall
    ``deadline_source`` reported to callers is composed by ``_deadline_source`` (it is
    ``"stored_deadline"`` when a row carries an explicit deadline, else
    ``"resolved_window:<window_source>"``).
    """
    prefs = preferences or {}
    window = prefs.get("rollback_window_hours")
    if window is None:
        return DEFAULT_ROLLBACK_WINDOW_HOURS, "documented_default"
    try:
        window_int = int(window)
    except (TypeError, ValueError):
        return DEFAULT_ROLLBACK_WINDOW_HOURS, "documented_default"
    if window_int < 0:
        return DEFAULT_ROLLBACK_WINDOW_HOURS, "documented_default"
    return window_int, "project_preference"


def append_availability(
    *,
    append_stable_key: Any,
    target_schema_hash: str | None,
    candidate_schema_hash: str | None,
) -> dict[str, Any]:
    """Decide whether Append is available (PURE). Safe-by-default => unavailable.

    Append is AVAILABLE only when ALL three hold (Story 12.12):
      * a non-empty stable-key / dedup contract is declared on the destination,
      * the candidate schema hash is known, and
      * it is COMPATIBLE with the destination schema hash (equal here; a superset
        rule is a Phase-B extension -- see the module Phase-B note).

    Otherwise Append is UNAVAILABLE and Replace is the safe supported action. The
    returned dict always carries ``available`` + ``fallback_action`` + ``reason``.
    """
    fallback = {"available": False, "fallback_action": ACTION_REPLACE}
    if not append_stable_key or not isinstance(append_stable_key, (list, tuple)):
        return {**fallback, "reason": "no_stable_key_contract"}
    if not candidate_schema_hash or not target_schema_hash:
        return {**fallback, "reason": "schema_unknown"}
    if candidate_schema_hash != target_schema_hash:
        return {**fallback, "reason": "incompatible_schema"}
    return {
        "available": True,
        "fallback_action": None,
        "reason": "stable_key_and_compatible_schema",
        "stable_key": list(append_stable_key),
    }


def empty_replacement_allowed(
    *,
    row_count: int | None,
    force_empty_publish: bool,
    allow_empty_publication: bool,
) -> bool:
    """PURE: may a (possibly empty) replacement proceed w.r.t. the empty gate?

    A non-empty candidate is always allowed by this gate. An empty (zero-row)
    candidate is allowed ONLY when BOTH the 2nd explicit confirmation
    (``force_empty_publish``) AND the ``allow_empty_publication`` preference are set
    (reuses the 12.5/12.8 empty-dataset policy). An unknown (None) row count fails
    closed (treated as "cannot prove non-empty").
    """
    if row_count is None:
        return False
    if row_count > 0:
        return True
    return bool(force_empty_publish and allow_empty_publication)


def requires_owner_floor(operation: str) -> bool:
    """PURE: does *operation* change a destination policy that only Owners may change?"""
    return operation in OWNER_FLOOR_OPERATIONS


def _deadline_source(target: dict[str, Any], window_source: str) -> str:
    """PURE: name the value that ACTUALLY governed the target's expiry decision.

    C1 honesty: the reported ``deadline_source`` must match the value that gated the
    rollback -- a stored ``rollback_deadline`` (rare; forward publishes leave it NULL)
    OR the resolved window (the common path), whose own provenance is *window_source*
    (``project_preference`` or ``documented_default``). Reported as
    ``resolved_window:<window_source>`` so both facets are observable.
    """
    if target.get("stored_deadline_present"):
        return "stored_deadline"
    return f"resolved_window:{window_source}"


# ---------------------------------------------------------------------------
# RBAC -- the owner-floor enforcement for destination-policy operations.
# ---------------------------------------------------------------------------


def enforce_owner_floor(
    conn,
    *,
    operation: str,
    identity: str,
    project_id: str,
    datastream_id: str | None = None,
    auth_mode: str | None = None,
) -> None:
    """Raise ``OwnerFloorRequired`` unless *identity* is an Owner of the destination.

    Story 12.12 RBAC: Viewers are read-only, Members may perform recoverable actions
    (replace/append/rollback), and Owners ALONE change destination ownership, access,
    retention, or irreversible deletion policy. This function is the enforcement point
    for that OWNER FLOOR -- it is only called for operations in
    ``OWNER_FLOOR_OPERATIONS`` (recoverable data actions use the Member floor at the
    API seam and do NOT call this).

    It reuses BOTH access layers so it holds in legacy single-tenant AND strict AD-5
    production deployments:
      * the legacy per-project role floor (``identity_has_project_role(... 'owner')``)
        -- keeps single-tenant / disabled-auth green (anonymous => owner), and
      * the strict AD-5 seam (``resolve_strict_resource_access(...,
        minimum_capability='manage')``) when Epic-36 production access is enabled --
        so a non-owner grant cannot reach a destination-policy op in production.
    """
    from core.project_access import (  # noqa: PLC0415
        ProjectAccessUnavailable,
        epic36_production_access_enabled,
        identity_has_project_role,
        resolve_strict_resource_access,
    )

    if operation not in OWNER_FLOOR_OPERATIONS:  # pragma: no cover - guarded by caller
        return

    subject = identity or "anonymous"

    # Strict AD-5 production floor (only when production access is enabled). A
    # destination-policy op demands the 'manage' capability, which
    # resolve_strict_resource_access grants only to owner (owner_floor) or an admin
    # manage grant -- a member/viewer is refused.
    if epic36_production_access_enabled(auth_mode=auth_mode):
        decision = resolve_strict_resource_access(
            subject,
            conn,
            project_id=project_id if datastream_id is None else None,
            datastream_id=datastream_id,
            minimum_capability="manage",
            auth_mode=auth_mode,
        )
        if not decision.allowed:
            raise OwnerFloorRequired(operation)
        return

    # Legacy per-project floor (single-tenant / disabled-auth compatible).
    try:
        is_owner = identity_has_project_role(
            project_id, subject, "owner", conn, auth_mode=auth_mode
        )
    except ProjectAccessUnavailable as exc:
        raise DatasetRecoveryError(
            "access_unavailable", "destination role could not be verified"
        ) from exc
    if not is_owner:
        raise OwnerFloorRequired(operation)


# ---------------------------------------------------------------------------
# I/O helpers (private) -- thin, parameterized. Project-scoped.
# ---------------------------------------------------------------------------


def _load_datastream_facts(conn, datastream_id: str, project_id: str) -> dict[str, Any] | None:
    """Read the live destination facts (pointer + append contract). READ ONLY."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT current_published_execution_id, append_stable_key
            FROM app.datastreams
            WHERE id = %s AND project_id = %s
            """,
            (datastream_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "current_published_execution_id": row[0],
        "append_stable_key": row[1],
    }


def _load_project_preferences(conn, project_id: str) -> dict[str, Any]:
    """Read the project-scoped DQ + rollback-window preferences (empty dict if none)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT max_row_count_delta_pct, allow_empty_publication, rollback_window_hours
            FROM app.project_preferences
            WHERE project_id = %s
            """,
            (project_id,),
        )
        row = cur.fetchone()
    if row is None:
        return {}
    return {
        "max_row_count_delta_pct": row[0],
        "allow_empty_publication": row[1],
        "rollback_window_hours": row[2],
    }


def _active_execution(conn, datastream_id: str, project_id: str) -> str | None:
    """Return the id of a non-terminal (active) execution for the datastream, or None.

    Reuses the SAME ACTIVE_STATES set as create_execution's concurrency guard so the
    concurrency reason is consistent with 12.5's structural
    ``uq_datastream_executions_active`` index.
    """
    from core.datastream_publication import ACTIVE_STATES  # noqa: PLC0415

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM app.datastream_executions
            WHERE datastream_id = %s AND project_id = %s AND state = ANY(%s)
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (datastream_id, project_id, list(ACTIVE_STATES)),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _load_rollback_target(
    conn,
    datastream_id: str,
    project_id: str,
    target_execution_id: str,
    resolved_window_hours: int,
) -> dict[str, Any] | None:
    """Load the publication-log evidence for *target_execution_id* as a rollback target.

    A valid rollback target is a PRIOR PUBLISHED execution that:
      * has at least one publication-log row (it was actually published),
      * is currently RETAINED, and
      * is still inside the rollback WINDOW.

    THE C1 FIX -- expiry is computed from the EFFECTIVE deadline, which is the stored
    ``rollback_deadline`` when present ELSE the RESOLVED window applied to the row's
    own ``published_at`` (``published_at + resolved_window_hours``). The reused 12.5
    ``commit_publication`` forward-publish path inserts log rows WITHOUT a
    ``rollback_deadline`` (every normal publish is NULL), so a bare
    ``rollback_deadline IS NOT NULL AND ... <= NOW()`` test would make the window a
    NO-OP (NULL => never expired => rollbackable forever). We COALESCE to the resolved
    window instead, and FAIL CLOSED (expired) when NEITHER a stored deadline NOR a
    resolvable ``published_at`` exists so a target we cannot prove to be in-window is
    refused rather than silently rollbackable.

    ``resolved_window_hours`` is the project-governed window (``0`` == no window ==
    rollback disabled immediately). The returned ``deadline_source`` names WHICH value
    governed the decision so the reported source matches the value that actually
    gated it (stored deadline vs resolved window vs fail-closed).

    Returns the newest log row's facts (execution_id, retained, effective_deadline,
    expired, deadline_source, content_hash, row_count).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                execution_id,
                retained,
                rollback_deadline,
                published_at,
                COALESCE(
                    rollback_deadline,
                    published_at + make_interval(hours => %s)
                ) AS effective_deadline,
                CASE
                    -- Fail CLOSED: no stored deadline AND no resolvable published_at
                    -- => cannot prove in-window => treat as expired.
                    WHEN rollback_deadline IS NULL AND published_at IS NULL THEN TRUE
                    ELSE COALESCE(
                        rollback_deadline,
                        published_at + make_interval(hours => %s)
                    ) <= NOW()
                END AS expired,
                content_hash,
                row_count
            FROM app.datastream_publication_log
            WHERE datastream_id = %s AND project_id = %s AND execution_id = %s
            ORDER BY published_at DESC
            LIMIT 1
            """,
            (
                resolved_window_hours,
                resolved_window_hours,
                datastream_id,
                project_id,
                target_execution_id,
            ),
        )
        row = cur.fetchone()
    if row is None:
        return None
    stored_deadline = row[2]
    effective_deadline = row[4]
    # The reported source reflects which value actually governed the expiry decision.
    deadline_source = "stored_deadline" if stored_deadline is not None else "resolved_window"
    return {
        "execution_id": row[0],
        "retained": bool(row[1]),
        "rollback_deadline": (
            effective_deadline.isoformat()
            if hasattr(effective_deadline, "isoformat")
            else effective_deadline
        ),
        "stored_deadline_present": stored_deadline is not None,
        "expired": bool(row[5]),
        "deadline_source": deadline_source,
        "content_hash": row[6],
        "row_count": row[7],
    }


def _current_pointer_published_at(
    conn, datastream_id: str, project_id: str, current_execution_id: str | None
):
    """Return the CURRENT pointer's FIRST (oldest) publication ``published_at`` (or None).

    This is the anchor for the MONOTONIC default-rollback resolver. It deliberately
    takes the OLDEST (``ORDER BY published_at ASC``) log row for the current pointer,
    i.e. the moment this execution was ORIGINALLY published -- NOT the newest, which
    after a rollback would be the recent rollback row and would break monotonicity
    (letting the resolver pick the version we just rolled away from and oscillate). The
    default target must be strictly OLDER than this original publication, so a retried
    default rollback walks strictly backward in the ORIGINAL publish order and cannot
    oscillate: after rolling current->prior, nothing is older than prior's own original
    publish, so the resolver returns None (an idempotent no-op).
    """
    if not current_execution_id:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT published_at
            FROM app.datastream_publication_log
            WHERE datastream_id = %s AND project_id = %s AND execution_id = %s
            ORDER BY published_at ASC
            LIMIT 1
            """,
            (datastream_id, project_id, current_execution_id),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _latest_retained_prior(
    conn,
    datastream_id: str,
    project_id: str,
    exclude_execution_id: str | None,
    before_published_at=None,
) -> str | None:
    """Return the newest RETAINED prior published execution id (rollback candidate).

    H1 -- MONOTONIC default resolution: the candidate must be a DIFFERENT execution
    from the current pointer (``exclude_execution_id``) AND, when *before_published_at*
    is supplied, must have been published STRICTLY BEFORE that instant (the current
    pointer's own publication). This makes a retried DEFAULT (target-less) rollback
    non-oscillating: after rolling current->prior, the current pointer is ``prior``
    whose publication predates ``current``; there is no retained execution older than
    ``prior``'s own publish, so the default resolver returns None (the mutating path
    then treats a target-less "nothing older" as an idempotent no-op -- NOT a re-swap
    back to ``current``). Read only.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT execution_id
            FROM app.datastream_publication_log
            WHERE datastream_id = %s AND project_id = %s
              AND retained
              AND (%s IS NULL OR execution_id <> %s)
              AND (%s::timestamptz IS NULL OR published_at < %s::timestamptz)
            ORDER BY published_at DESC
            LIMIT 1
            """,
            (
                datastream_id,
                project_id,
                exclude_execution_id,
                exclude_execution_id,
                before_published_at,
                before_published_at,
            ),
        )
        row = cur.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# 1) Append availability (read-only) + Replace fallback presentation.
# ---------------------------------------------------------------------------


def resolve_append_availability(
    conn,
    *,
    datastream_id: str,
    project_id: str,
    candidate_schema_hash: str | None = None,
    target_schema_hash: str | None = None,
) -> dict[str, Any]:
    """Return whether Append is available for the destination, else Replace fallback.

    Reads the destination's declared ``append_stable_key`` contract and delegates the
    decision to the PURE ``append_availability``. When Append is unavailable this
    presents Replace as the safe supported action (mirrors 12.8/12.9 replace-only
    default). Read only -- performs NO mutation.
    """
    facts = _load_datastream_facts(conn, datastream_id, project_id)
    if facts is None:
        # Existence-hiding: an out-of-scope datastream is simply "not available".
        return {"available": False, "fallback_action": ACTION_REPLACE, "reason": "not_found"}
    return append_availability(
        append_stable_key=facts.get("append_stable_key"),
        candidate_schema_hash=candidate_schema_hash,
        target_schema_hash=target_schema_hash,
    )


# ---------------------------------------------------------------------------
# 2) Dataset-pointer ROLLBACK -- the genuinely missing 12.12 piece.
# ---------------------------------------------------------------------------


def preview_rollback(
    conn,
    *,
    datastream_id: str,
    project_id: str,
    target_execution_id: str | None = None,
) -> dict[str, Any]:
    """Assemble a READ-ONLY rollback preview: is a rollback available and to what?

    Resolves the default target via the same MONOTONIC resolver as the mutating path
    (newest retained execution strictly OLDER than the current pointer's original
    publish), or validates the caller's ``target_execution_id``, computes the EFFECTIVE
    deadline state (C1: ``COALESCE(rollback_deadline, published_at + resolved_window)``)
    from the log + project-scoped window, and reports availability WITHOUT mutating.

    This is the seam the API caller uses to RESOLVE the default target ONCE and then
    pass the SAME resolved id to ``rollback_dataset`` across retries (operations-
    idempotency model -- H1), so a retry short-circuits to a no-op instead of swapping.

    Returns ``{available, target_execution_id, current_execution_id, retained,
    rollback_deadline, deadline_source, window_source, rollback_window_hours, expired,
    reason}``.
    """
    facts = _load_datastream_facts(conn, datastream_id, project_id)
    if facts is None:
        return {"available": False, "reason": "not_found"}
    current = facts["current_published_execution_id"]

    prefs = _load_project_preferences(conn, project_id)
    window_hours, window_source = resolve_rollback_window_hours(prefs)

    if target_execution_id is None:
        anchor = _current_pointer_published_at(conn, datastream_id, project_id, current)
        target_execution_id = _latest_retained_prior(
            conn, datastream_id, project_id, current, anchor
        )
    if not target_execution_id:
        return {
            "available": False,
            "current_execution_id": current,
            "reason": "rollback_target_not_found",
            "window_source": window_source,
            "rollback_window_hours": window_hours,
        }

    target = _load_rollback_target(
        conn, datastream_id, project_id, target_execution_id, window_hours
    )
    if target is None:
        return {
            "available": False,
            "current_execution_id": current,
            "target_execution_id": target_execution_id,
            "reason": "rollback_target_invalid",
            "window_source": window_source,
            "rollback_window_hours": window_hours,
        }

    # deadline_source is the value that actually governed expiry: the stored deadline
    # if present, else the resolved window (whose own source is window_source).
    deadline_source = _deadline_source(target, window_source)

    available = target["retained"] and not target["expired"]
    reason = "available"
    if not target["retained"]:
        reason = "target_not_retained"
    elif target["expired"]:
        reason = "rollback_window_expired"
    return {
        "available": available,
        "current_execution_id": current,
        "target_execution_id": target_execution_id,
        "retained": target["retained"],
        "rollback_deadline": target["rollback_deadline"],
        "deadline_source": deadline_source,
        "window_source": window_source,
        "rollback_window_hours": window_hours,
        "expired": target["expired"],
        "reason": reason,
    }


def rollback_dataset(
    conn,
    *,
    datastream_id: str,
    project_id: str,
    actor: str,
    target_execution_id: str | None = None,
    connection_factory=None,
) -> dict[str, Any]:
    """Roll the DATASET pointer back to a RETAINED healthy prior published execution.

    THE distinct 12.12 piece (not the 36.18 mapping rollback). In ONE transaction:

      1. Lock the datastream pointer row ``FOR UPDATE`` (serialize concurrent
         mutations -- mirrors ``commit_publication``).
      2. Reject if a NON-terminal execution is active (a rollback while a
         replace/append is in flight would race -- ``concurrent_mutation_active``
         with the blocking id + lock reason). Reuses 12.5's ACTIVE_STATES.
      3. Resolve the rollback TARGET (caller-supplied or newest retained prior) and
         verify it is retained + within the rollback DEADLINE (project-window
         governed). An expired deadline DISABLES the action (``rollback_window_expired``).
      4. Re-run the SAME semantic + DQ gates on the target's stored row_count /
         content_hash (empty / hash / schema / drift). Fail closed on any breach.
      5. Swap ``current_published_execution_id`` BACK to the target and INSERT a NEW
         append-only ``datastream_publication_log`` row (a rollback is a NEW row --
         never a mutation of history), all in the SAME transaction, then COMMIT.

    On ANY error the whole group rolls back (the prior pointer + log are intact) and
    an audit row records the failure. NEVER truncates first; NEVER recomputes data --
    it is a pointer swap over already-validated, retained data.

    ``connection_factory`` is accepted for symmetry with ``commit_publication`` (it is
    unused on the happy path; the rollback runs entirely inside *conn*). The caller
    may pass its own; a rollback failure never opens a second connection because the
    swap-back has no cross-store partial-failure window (Postgres-only).

    Returns ``{execution: {...}, rolled_back_from, rolled_back_to,
    publication_log_id, deadline_source}``. When the current pointer already equals
    the resolved target (an idempotent retry -- H1) it returns
    ``{already_at_target: True, ...}`` with ``publication_log_id: None`` and NO
    mutation (no pointer swap, no new log row).
    """
    from core.audit import insert_audit_row  # noqa: PLC0415
    from core.datastream_publication import (  # noqa: PLC0415
        ACTIVE_STATES,
        evaluate_dq_gates,
        split_gate_issues,
    )

    try:
        with conn.cursor() as cur:
            # 1) Lock the pointer row FIRST (serialize concurrent mutations).
            cur.execute(
                """
                SELECT current_published_execution_id
                FROM app.datastreams
                WHERE id = %s AND project_id = %s
                FOR UPDATE
                """,
                (datastream_id, project_id),
            )
            ds_row = cur.fetchone()
            if ds_row is None:
                raise RollbackTargetInvalid("datastream not found in project scope")
            current_execution_id = ds_row[0]

            # 2) Reject if a mutation (replace/append) is already active. This is the
            #    serialize-or-reject-with-lock-reason contract (reuses ACTIVE_STATES).
            cur.execute(
                """
                SELECT id FROM app.datastream_executions
                WHERE datastream_id = %s AND project_id = %s AND state = ANY(%s)
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (datastream_id, project_id, list(ACTIVE_STATES)),
            )
            active = cur.fetchone()
            if active is not None:
                raise ConcurrentMutationActive(active[0], "execution_in_flight")

            # 3) Resolve + validate the rollback target (retained + within window).
            prefs = _load_project_preferences(conn, project_id)
            window_hours, window_source = resolve_rollback_window_hours(prefs)

            # H1 -- resolve the target. An EXPLICIT target is used verbatim (the caller,
            # e.g. admin_api, resolves it ONCE via preview_rollback and passes the same
            # id across retries -- the operations-idempotency model). A target-less
            # (DEFAULT) request uses the MONOTONIC resolver anchored on the current
            # pointer's publication instant so a retry walks strictly backward and
            # cannot oscillate.
            explicit_target = target_execution_id is not None
            resolved_target_id = target_execution_id
            if resolved_target_id is None:
                anchor = _current_pointer_published_at(
                    conn, datastream_id, project_id, current_execution_id
                )
                resolved_target_id = _latest_retained_prior(
                    conn, datastream_id, project_id, current_execution_id, anchor
                )

            # A DEFAULT request with no strictly-older retained target means we are
            # already at the oldest retained version -> idempotent NO-OP (never an
            # error, never a re-swap). An EXPLICIT missing target is a real error.
            if not resolved_target_id:
                if explicit_target:
                    raise RollbackTargetNotFound()
                conn.commit()
                return _already_at_target_result(
                    datastream_id, project_id, current_execution_id
                )

            # H1 IDEMPOTENCY: if the (explicit or resolved) target ALREADY equals the
            # current pointer, this is a stable no-op -- NO pointer swap, NO new log
            # row. Short-circuit BEFORE any mutation and BEFORE the deadline check (a
            # no-op is always safe). This is what makes a RETRY that passes the same
            # resolved target idempotent.
            if resolved_target_id == current_execution_id:
                conn.commit()
                return _already_at_target_result(
                    datastream_id, project_id, current_execution_id
                )

            target = _load_rollback_target(
                conn, datastream_id, project_id, resolved_target_id, window_hours
            )
            if target is None:
                raise RollbackTargetInvalid("no publication-log evidence for target")
            if not target["retained"]:
                raise RollbackTargetInvalid("target dataset is no longer retained")
            deadline_source = _deadline_source(target, window_source)
            if target["expired"]:
                raise RollbackWindowExpired(target["rollback_deadline"], deadline_source)

            # 4) Re-run the semantic + DQ gates against the target's stored validated
            #    facts (fail closed). A rollback to an empty/hash-broken target is
            #    refused just like a forward publish.
            #
            # M1 HONESTY -- which gates actually fire here:
            #   * empty_candidate  : FIRES on the target's stored row_count (a
            #                        zero-row target is refused; non-overridable).
            #   * row_count_delta  : evaluated vs the current pointer's row_count but
            #                        APPROVED (approved=True) -- a rollback to an
            #                        already-validated prior version is an Owner-
            #                        authorized recovery, so this gate is advisory here.
            #   * mapping_drift    : does NOT fire on a rollback. This gate compares the
            #                        plan's source_schema_hash against the CURRENT LIVE
            #                        capability fingerprint. A pointer-swap rollback
            #                        deliberately does NOT re-probe the live source
            #                        (AD-8: never recompute / re-classify), so there is
            #                        no honest "current" fingerprint to compare against.
            #                        Feeding the target's stored hash to BOTH sides would
            #                        be theatre (always equal, can never fire). It is
            #                        left None (skipped) rather than pretended. Live
            #                        drift re-detection is the forward 12.3->12.4 path;
            #                        the INTEGRATION SPEC notes optional re-classification
            #                        before rollback as a Phase-B enhancement.
            #   * schema_hash_mismatch : does NOT fire on rollback either -- there is no
            #                        stored landing-schema hash on this path (the plan
            #                        version carries no declared landing-schema column in
            #                        migration 030). Left None, documented, not faked.
            #   * content_hash     : the v1 advisory marker only (byte-identity is
            #                        deferred to 12.6; not a block) -- unchanged.
            #
            # HONEST SUMMARY: on a rollback the ONLY gate that actually BLOCKS is
            # empty_candidate (a zero-row target is refused; non-overridable). The
            # row-count-delta gate is evaluated but APPROVED, and the content-hash gate
            # is the advisory-only v1 marker. This is narrower than "the same DQ +
            # semantic gates" -- the story/docstring now say exactly this (M1). A stored
            # mapping version is still REQUIRED (the provenance guard below fails closed
            # when the target lacks a mapping version), so a target with corrupted /
            # missing provenance is refused even though the drift gate itself is skipped.
            prior_row_count = None
            if current_execution_id:
                with conn.cursor() as pc:
                    pc.execute(
                        "SELECT row_count FROM app.datastream_executions "
                        "WHERE id = %s AND project_id = %s",
                        (current_execution_id, project_id),
                    )
                    prow = pc.fetchone()
                    prior_row_count = prow[0] if prow else None

            all_issues = evaluate_dq_gates(
                row_count=target["row_count"],
                content_hash=target["content_hash"],
                validated_content_hash=None,
                prior_row_count=prior_row_count,
                # See the HONEST SUMMARY above: drift/schema gates are intentionally
                # None (skipped) on a pointer-swap rollback -- no live probe, no faked
                # self-comparison. Only empty_candidate blocks here.
                plan_source_schema_hash=None,
                current_capability_fingerprint=None,
                landing_schema_hash=None,
                plan_declared_schema_hash=None,
                preferences={
                    "max_row_count_delta_pct": prefs.get("max_row_count_delta_pct"),
                    "allow_empty_publication": prefs.get("allow_empty_publication"),
                },
                # A rollback to a previously-published healthy version is an Owner-
                # authorized recovery: the row-count delta gate is approved (the
                # target already passed its own gates when first published), but the
                # non-overridable gates (empty/hash/schema/drift) still fire.
                approved=True,
                force_empty_publish=False,
            )
            blocking, _advisory = split_gate_issues(all_issues)
            if blocking:
                raise RollbackGateFailed(blocking)

            # 5) Atomic swap-BACK + append-only log row (a rollback is a NEW row).
            #    Read the target's provenance for the log row (plan/mapping versions).
            with conn.cursor() as ec:
                ec.execute(
                    """
                    SELECT plan_version_id, mapping_version_id, content_hash, row_count,
                           state
                    FROM app.datastream_executions
                    WHERE id = %s AND project_id = %s
                    """,
                    (resolved_target_id, project_id),
                )
                erow = ec.fetchone()
            if erow is None or not erow[2] or erow[3] is None:
                # AD-9: never fabricate provenance for the rollback log row.
                raise RollbackTargetInvalid("target execution lacks validated provenance")
            plan_version_id, mapping_version_id, content_hash, row_count, target_state = erow
            # L4: re-verify the target execution is still PUBLISHED at swap time. A
            # target that was superseded/failed/retired since the log row was written
            # must not become the live pointer (fail closed).
            if target_state != "published":
                raise RollbackTargetInvalid(
                    f"target execution is not in a published state (state={target_state})"
                )

            cur.execute(
                """
                UPDATE app.datastreams
                SET current_published_execution_id = %s
                WHERE id = %s AND project_id = %s
                """,
                (resolved_target_id, datastream_id, project_id),
            )
            if cur.rowcount != 1:  # pragma: no cover - defensive within one txn.
                raise RollbackTargetInvalid("pointer swap-back affected no row")

            log_id = _mint_log_id()
            cur.execute(
                """
                INSERT INTO app.datastream_publication_log
                    (id, execution_id, datastream_id, project_id, plan_version_id,
                     mapping_version_id, content_hash, row_count, prior_execution_id,
                     published_by, rollback_deadline, retained)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        NOW() + make_interval(hours => %s), TRUE)
                """,
                (
                    log_id,
                    resolved_target_id,
                    datastream_id,
                    project_id,
                    plan_version_id,
                    mapping_version_id,
                    content_hash,
                    row_count,
                    current_execution_id,  # rollback evidence: the version we left
                    actor,
                    window_hours,
                ),
            )

            # Outbox event (downstream refresh / cache-bust / monitoring).
            cur.execute(
                """
                INSERT INTO app.datastream_outbox
                    (execution_id, datastream_id, project_id, event_type, payload)
                VALUES (%s, %s, %s, 'published', %s::jsonb)
                """,
                (
                    resolved_target_id,
                    datastream_id,
                    project_id,
                    _json_dumps(
                        {
                            "event": "rolled_back",
                            "action": ACTION_ROLLBACK,
                            "execution_id": resolved_target_id,
                            "rolled_back_from": current_execution_id,
                            "content_hash": content_hash,
                            "row_count": row_count,
                        }
                    ),
                ),
            )

            insert_audit_row(
                conn,
                identity=actor or "anonymous",
                # L1: a distinct rollback audit action (not the forward
                # datastream.published) so a dataset rollback is queryable on its own.
                # app.audit_log.action is free-text (no CHECK constraint), so this is
                # a cheap, safe distinct verb.
                action=AUDIT_ACTION_DATASET_ROLLED_BACK,
                provider_account="",
                connection_ref="",
                metadata={
                    "action": ACTION_ROLLBACK,
                    "datastream_id": datastream_id,
                    "project_id": project_id,
                    "rolled_back_from": current_execution_id,
                    "rolled_back_to": resolved_target_id,
                    "publication_log_id": log_id,
                    "content_hash": content_hash,
                    "row_count": row_count,
                    "rollback_deadline_source": deadline_source,
                },
            )

        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:  # pragma: no cover - rollback of an already-dead txn.
            pass
        raise

    return {
        "execution": {
            "id": resolved_target_id,
            "datastream_id": datastream_id,
            "project_id": project_id,
            "state": "published",
            "content_hash": content_hash,
            "row_count": row_count,
        },
        "rolled_back_from": current_execution_id,
        "rolled_back_to": resolved_target_id,
        "publication_log_id": log_id,
        "deadline_source": deadline_source,
    }


def _already_at_target_result(
    datastream_id: str, project_id: str, current_execution_id: str | None
) -> dict[str, Any]:
    """Stable H1 no-op result: the pointer is already at the target -- nothing changed."""
    return {
        "already_at_target": True,
        "execution": {
            "id": current_execution_id,
            "datastream_id": datastream_id,
            "project_id": project_id,
            "state": "published",
        },
        "rolled_back_from": current_execution_id,
        "rolled_back_to": current_execution_id,
        "publication_log_id": None,
        "deadline_source": None,
    }


def _json_dumps(value: Any) -> str:
    import json  # noqa: PLC0415

    return json.dumps(value, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# 3) Replace pre-flight -- the empty gate + concurrency reason (Replace itself is
#    12.5's create_execution + commit_publication; this validates the 12.12
#    preconditions BEFORE the caller mints the execution).
# ---------------------------------------------------------------------------


def preflight_replace(
    conn,
    *,
    datastream_id: str,
    project_id: str,
    candidate_row_count: int | None,
    force_empty_publish: bool = False,
) -> dict[str, Any]:
    """Validate the 12.12 preconditions for a Replace BEFORE an execution is minted.

    Checks (fail closed):
      * CONCURRENCY: reject with ``concurrent_mutation_active`` (+ blocking id + lock
        reason) when a non-terminal execution already targets this destination
        (reuses 12.5's ACTIVE_STATES; the hard structural guard is the
        ``uq_datastream_executions_active`` index at create_execution time).
      * EMPTY REPLACE: an empty candidate is blocked unless the 2nd explicit
        confirmation (``force_empty_publish``) AND the ``allow_empty_publication``
        preference are both set (reuses the 12.5/12.8 empty policy).

    Returns ``{ok: True}`` when the replace may proceed; raises otherwise. This does
    NOT itself create the execution -- the caller composes ``create_execution`` ->
    (load/validate) -> ``run_dq_gates`` -> ``commit_publication`` (all 12.5) for the
    actual atomic replace. The empty gate here is a fast pre-check so the operator is
    told UP FRONT; the 12.5 ``empty_candidate`` DQ gate is the authoritative block at
    publish time.
    """
    active = _active_execution(conn, datastream_id, project_id)
    if active is not None:
        raise ConcurrentMutationActive(active, "execution_in_flight")

    prefs = _load_project_preferences(conn, project_id)
    allow_empty = bool(prefs.get("allow_empty_publication"))
    if not empty_replacement_allowed(
        row_count=candidate_row_count,
        force_empty_publish=force_empty_publish,
        allow_empty_publication=allow_empty,
    ):
        raise EmptyReplacementBlocked()
    return {"ok": True, "action": ACTION_REPLACE}
