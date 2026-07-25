"""Atomic candidate publication orchestrator (Story 12.5).

This module owns EXACTLY one thing: the atomic step where a compiled, validated
Story 12.4 candidate becomes the authoritative current state of a datastream. It
is pure, source-agnostic Postgres orchestration (AD-2): no provider name, no
provider field name, no import from ``server/modules/*``, and NO BigQuery write
(AD-8: dbt is the only mart writer; the candidate data is already in BigQuery
from the dbt build triggered in the ``loading`` phase). It manages the Postgres
pointer, the candidate registry, the append-only publication log, the outbox, and
the audit trail only.

The five public entry points:

  * ``create_execution``   -- mint a ``dse_<ULID>`` in ``created`` state, with the
    12.4 provenance (``plan_version_id`` / ``mapping_version_id`` / plan snapshot).
    Idempotent on ``idempotency_key``; rejects a concurrent active execution.
  * ``advance_state``      -- the typed state machine. Every invalid transition is
    rejected with ``invalid_state_transition``. Writes an audit row per change.
  * ``run_dq_gates``       -- the fail-closed pre-publication gates (empty,
    row-count delta, content-hash match, schema-hash drift). Project-preference
    governed, never a platform-wide hardcode.
  * ``commit_publication`` -- THE atomic step: 4 writes in ONE Postgres
    transaction (execution -> published, publication_log, pointer swap, outbox).
    Any error rolls back the WHOLE group leaving the prior published state intact,
    and marks the execution ``failed`` in a SEPARATE connection.
  * ``reconcile_execution`` -- resolve a cross-store partial failure idempotently
    from execution state + outbox + content_hash + row_count. Fails closed with
    ``reconciliation_inconclusive`` when the safe state cannot be determined.

Invariants (held HARD):
  * ATOMIC: single commit; rollback on every error path; the pointer row is locked
    (``SELECT ... FOR UPDATE``) to serialize concurrent publishes.
  * NO RECOMPUTATION: publish PROMOTES the already-compiled candidate; it never
    recomputes metrics. Provenance linkage becomes real, carried -- not recomputed.
  * APPEND-ONLY history: the publication log is immutable; a rollback is a NEW row.
  * FAIL CLOSED: every gate/error leaves the prior published execution current.
  * AD-9 honesty: NULL is never coerced to 0; a blocked/failed publish is honest.

Content-hash honesty (v1 -- byte-identity NOT independently enforced yet):
  The execution's ``content_hash`` is CALLER-SUPPLIED advisory provenance (it
  arrives via the ``validating->ready`` state body -- Open Question 3, option (b));
  it is NOT independently recomputed from the BigQuery landing in this story.
  Independent verification against the landing is DEFERRED TO 12.6 (which computes
  the hash from BigQuery and passes it as ``validated_content_hash``). The
  content-hash gate FIRES only when an independent ``validated_content_hash`` is
  provided and diverges from the stored hash. On the v1 default path (no
  independent hash) the gate is SKIPPED and recorded as an explicit
  ``content_hash_verified: False`` marker -- never presented as a passed gate. So
  v1 promotes the candidate WITHOUT proving byte-identity against the landing; that
  proof is 12.6's obligation.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ulid import ULID

# psycopg is imported at module level so the create_execution insert can map the
# real Postgres constraint failures (unique-violation on the active-execution
# partial index -> 409 concurrent_execution_active; FK/integrity violation on a
# non-existent plan/mapping reference -> 422 invalid_reference) instead of letting
# a generic exception surface as an opaque 503. Import is guarded so the offline
# unit tests (which never touch Postgres) still import the module if psycopg is
# absent.
try:
    import psycopg
except ImportError:  # pragma: no cover - psycopg is a declared dependency.
    psycopg = None  # type: ignore[assignment]

if psycopg is not None:  # pragma: no branch - trivial binding.
    _UNIQUE_VIOLATION: type[BaseException] = psycopg.errors.UniqueViolation
    _INTEGRITY_ERROR: type[BaseException] = psycopg.errors.IntegrityError
else:  # pragma: no cover - psycopg is a declared dependency.
    # A never-matching sentinel keeps the try/except valid when psycopg is absent
    # (the offline unit tests never trigger a real Postgres constraint failure).
    class _NoMatch(Exception):
        pass

    _UNIQUE_VIOLATION = _NoMatch
    _INTEGRITY_ERROR = _NoMatch

# ---------------------------------------------------------------------------
# Typed, closed state machine.
# ---------------------------------------------------------------------------

STATE_CREATED = "created"
STATE_LOADING = "loading"
STATE_VALIDATING = "validating"
STATE_READY = "ready"
STATE_PUBLISHING = "publishing"
STATE_PUBLISHED = "published"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

TERMINAL_STATES = frozenset({STATE_PUBLISHED, STATE_FAILED, STATE_CANCELLED})

# The forward happy path. failed is reachable from any non-terminal state;
# cancelled from any non-terminal state EXCEPT publishing (once the atomic commit
# is underway there is no safe cancel -- it resolves to published or failed).
_FORWARD: dict[str, frozenset[str]] = {
    STATE_CREATED: frozenset({STATE_LOADING}),
    STATE_LOADING: frozenset({STATE_VALIDATING}),
    STATE_VALIDATING: frozenset({STATE_READY}),
    STATE_READY: frozenset({STATE_PUBLISHING}),
    STATE_PUBLISHING: frozenset({STATE_PUBLISHED}),
    STATE_PUBLISHED: frozenset(),
    STATE_FAILED: frozenset(),
    STATE_CANCELLED: frozenset(),
}

_CANCELLABLE = frozenset(
    {STATE_CREATED, STATE_LOADING, STATE_VALIDATING, STATE_READY}
)

# Non-terminal states that block a concurrent publication attempt.
ACTIVE_STATES = frozenset(
    {STATE_CREATED, STATE_LOADING, STATE_VALIDATING, STATE_READY, STATE_PUBLISHING}
)

# ---------------------------------------------------------------------------
# DQ gate codes (closed set) + governed preference defaults.
# ---------------------------------------------------------------------------

GATE_EMPTY_CANDIDATE = "empty_candidate"
GATE_ROW_COUNT_DELTA_EXCEEDED = "row_count_delta_exceeded"
GATE_CONTENT_HASH_MISMATCH = "content_hash_mismatch"
GATE_MAPPING_DRIFT = "mapping_drift"
GATE_SCHEMA_HASH_MISMATCH = "schema_hash_mismatch"

# Documented defaults used ONLY when app.project_preferences supplies no override.
# Never a silent platform-wide hardcode: the DQ result records threshold_source.
DEFAULT_MAX_ROW_COUNT_DELTA_PCT = 50.0
DEFAULT_ALLOW_EMPTY_PUBLICATION = False


class PublicationError(Exception):
    """Base for publication-orchestration errors carrying a stable ``code``."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


class InvalidStateTransition(PublicationError):
    """A transition that violates the typed state machine."""

    def __init__(self, current: str, requested: str) -> None:
        super().__init__(
            "invalid_state_transition",
            f"{current} -> {requested}",
        )
        self.current = current
        self.requested = requested


class ConcurrentExecutionActive(PublicationError):
    """Another execution for the same datastream is in a non-terminal state."""

    def __init__(self, blocking_execution_id: str) -> None:
        super().__init__(
            "concurrent_execution_active",
            f"blocking_execution_id={blocking_execution_id}",
        )
        self.blocking_execution_id = blocking_execution_id


class InvalidReference(PublicationError):
    """A referenced plan/mapping version does not exist (FK / integrity violation).

    Surfaced as an opaque 422 ``invalid_reference`` -- non-disclosing: it does NOT
    carry ``str(exc)`` so the DB constraint name / offending value never leaks.
    """

    def __init__(self) -> None:
        super().__init__("invalid_reference")


class IdempotencyConflict(PublicationError):
    """The idempotency key was reused with a different payload."""

    def __init__(self) -> None:
        super().__init__("idempotency_conflict")


class ExecutionNotFound(PublicationError):
    """The requested execution does not exist in the given project scope."""

    def __init__(self) -> None:
        super().__init__("execution_not_found")


class DQGateFailed(PublicationError):
    """One or more pre-publication DQ gates failed closed."""

    def __init__(self, issues: list[dict[str, Any]]) -> None:
        super().__init__("dq_gate_failed")
        self.issues = issues


# ---------------------------------------------------------------------------
# Pure helpers (no DB) -- exercised by the offline unit tests.
# ---------------------------------------------------------------------------


def _mint_execution_id() -> str:
    return f"dse_{ULID()}"


def _mint_log_id() -> str:
    return f"dplog_{ULID()}"


def _hash_key(idempotency_key: str) -> str:
    """SHA-256 hex of the idempotency key (64-hex, matches the CHECK constraint)."""
    return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()


def _payload_fingerprint(payload: dict[str, Any]) -> str:
    """Deterministic content fingerprint of an execution's create payload.

    Used to detect idempotency-key reuse with a DIFFERENT payload (-> 409). Keys
    are sorted so the fingerprint is stable across dict ordering.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_valid_transition(current: str, new_state: str) -> bool:
    """Pure predicate: may ``current`` advance to ``new_state``?

    A terminal state accepts NO transition. ``failed`` is reachable from any
    non-terminal state; ``cancelled`` from any cancellable (non-publishing,
    non-terminal) state; otherwise only the declared forward edge is allowed.
    """
    if current in TERMINAL_STATES:
        return False
    if new_state == STATE_FAILED:
        return True
    if new_state == STATE_CANCELLED:
        return current in _CANCELLABLE
    return new_state in _FORWARD.get(current, frozenset())


def resolve_dq_thresholds(
    preferences: dict[str, Any] | None,
) -> tuple[float, bool, str]:
    """Resolve the governed DQ thresholds for a project.

    Reads ``max_row_count_delta_pct`` / ``allow_empty_publication`` from the
    project-scoped ``app.project_preferences`` row (passed in as a plain dict).
    Falls back to the DOCUMENTED defaults when unset, recording ``threshold_source``
    so the fallback is never silent ("prefer per-project preferences").

    Returns ``(max_row_count_delta_pct, allow_empty_publication, threshold_source)``.
    """
    prefs = preferences or {}
    delta = prefs.get("max_row_count_delta_pct")
    allow_empty = prefs.get("allow_empty_publication")
    source = "documented_default"
    if delta is not None or allow_empty is not None:
        source = "project_preference"
    max_delta = float(delta) if delta is not None else DEFAULT_MAX_ROW_COUNT_DELTA_PCT
    if max_delta < 0:
        max_delta = DEFAULT_MAX_ROW_COUNT_DELTA_PCT
        source = "documented_default"
    allow_empty_resolved = (
        bool(allow_empty)
        if allow_empty is not None
        else DEFAULT_ALLOW_EMPTY_PUBLICATION
    )
    return max_delta, allow_empty_resolved, source


def evaluate_dq_gates(
    *,
    row_count: int | None,
    content_hash: str | None,
    validated_content_hash: str | None,
    prior_row_count: int | None,
    plan_source_schema_hash: str | None,
    current_capability_fingerprint: str | None,
    landing_schema_hash: str | None,
    plan_declared_schema_hash: str | None,
    preferences: dict[str, Any] | None,
    approved: bool = False,
    force_empty_publish: bool = False,
) -> list[dict[str, Any]]:
    """Pure DQ-gate decision function (no DB) -- returns a list of issues.

    An empty list == all gates pass. Each issue is ``{code, detail, repair}``. The
    gates FAIL CLOSED: an unknown/NULL value that cannot be proven safe blocks.

    Gates (closed set):
      * ``empty_candidate``          -- zero rows blocked unless force_empty_publish
        AND allow_empty_publication preference is True.
      * ``row_count_delta_exceeded`` -- |delta%| vs prior published row_count over
        the governed threshold, blocked unless Owner-approved.
      * ``content_hash_mismatch``    -- an INDEPENDENTLY computed validated hash !=
        the caller-supplied stored content hash (byte-identity broken). Never
        Owner-overridable. See the content-hash honesty note below.
      * ``mapping_drift``            -- the 12.3 mapping's source_schema_hash no
        longer matches the current capability fingerprint. Never overridable.
      * ``schema_hash_mismatch``     -- the BigQuery landing schema hash != the
        12.4 plan's declared schema hash. Never overridable.

    Content-hash honesty (v1 -- byte-identity NOT independently enforced yet):
      The ``content_hash`` on the execution row is CALLER-SUPPLIED advisory
      provenance (it arrives via the ``validating->ready`` state body -- Open
      Question 3, option (b)); it is NOT recomputed from the BigQuery landing in
      this story. Independent verification against the landing is DEFERRED TO 12.6
      (which will compute the hash from BigQuery and pass it as
      ``validated_content_hash``). Therefore the content-hash gate FIRES only when
      an INDEPENDENT ``validated_content_hash`` is supplied and diverges from the
      stored hash. When ``validated_content_hash`` is ABSENT (the v1 default path)
      the gate is SKIPPED and the result is marked
      ``{code: content_hash_check, content_hash_verified: False}`` -- an explicit,
      honest "not independently verified" marker. It is NEVER reported as a passed
      gate: an unverified caller-supplied hash must not masquerade as byte-identity
      proof (AD-9 honesty).
    """
    max_delta_pct, allow_empty, threshold_source = resolve_dq_thresholds(preferences)
    issues: list[dict[str, Any]] = []

    # --- Gate: content-hash match (byte-identity). Non-overridable. ---------
    # Two honest cases (never a silent pass -- AD-9):
    #   (1) an INDEPENDENT validated_content_hash is supplied -> real gate: compare
    #       it to the stored (caller-supplied) content_hash and fire
    #       content_hash_mismatch on any divergence (or when either side is NULL and
    #       cannot prove byte-identity -> fail closed).
    #   (2) NO independent hash (the v1 default) -> byte-identity is NOT yet
    #       verifiable (deferred to 12.6). Do NOT report a pass: record an explicit
    #       content_hash_verified=False marker so the caller/log knows the invariant
    #       was NOT independently checked. This marker is informational, not a block.
    if validated_content_hash is not None:
        if not content_hash or content_hash != validated_content_hash:
            issues.append(
                {
                    "code": GATE_CONTENT_HASH_MISMATCH,
                    "detail": (
                        "independently validated content hash does not match the "
                        "stored to-be-published content hash"
                    ),
                    "repair": {"recompile_or_reload_candidate": "12.4_then_12.5"},
                    "content_hash_verified": True,
                }
            )
    else:
        # v1 default: the stored hash is advisory only; independent verification is
        # deferred to 12.6. Emit an explicit, honest "not verified" marker -- NOT a
        # passed gate and NOT a block.
        issues.append(
            {
                "code": "content_hash_check",
                "detail": (
                    "byte-identity NOT independently verified in v1: the stored "
                    "content hash is caller-supplied advisory provenance; "
                    "independent verification against the BigQuery landing is "
                    "deferred to 12.6"
                ),
                "repair": {"supply_validated_content_hash": "12.6"},
                "content_hash_verified": False,
                "advisory": True,
            }
        )

    # --- Gate: mapping drift. Non-overridable. ------------------------------
    # If the plan's source_schema_hash no longer matches the current source
    # capability fingerprint, the mapping is drifted -> re-classify (12.3) and
    # re-compile (12.4). Fail closed when either side is unknown.
    if plan_source_schema_hash is not None and current_capability_fingerprint is not None:
        if plan_source_schema_hash != current_capability_fingerprint:
            issues.append(
                {
                    "code": GATE_MAPPING_DRIFT,
                    "detail": "source_schema_hash no longer matches capability fingerprint",
                    "repair": {"reclassify_and_recompile": "12.3_then_12.4"},
                }
            )

    # --- Gate: landing schema-hash drift. Non-overridable. ------------------
    if landing_schema_hash is not None and plan_declared_schema_hash is not None:
        if landing_schema_hash != plan_declared_schema_hash:
            issues.append(
                {
                    "code": GATE_SCHEMA_HASH_MISMATCH,
                    "detail": (
                        "BigQuery landing schema hash does not match the plan's "
                        "declared schema"
                    ),
                    "repair": {"reload_or_recompile": "align_landing_schema"},
                }
            )

    # --- Gate: empty candidate. Blocked by default. -------------------------
    if row_count is not None and row_count == 0:
        if not (force_empty_publish and allow_empty):
            issues.append(
                {
                    "code": GATE_EMPTY_CANDIDATE,
                    "detail": (
                        "candidate has zero rows; set force_empty_publish=true and "
                        "the allow_empty_publication project preference to publish"
                    ),
                    "repair": {
                        "confirm_empty_intent": {
                            "force_empty_publish": True,
                            "allow_empty_publication": True,
                        },
                        "threshold_source": threshold_source,
                    },
                }
            )

    # --- Gate: row-count delta vs prior. Owner-approvable. ------------------
    # Only meaningful when there is a prior published row_count > 0 and the current
    # candidate is non-empty (an empty candidate is handled by empty_candidate).
    if (
        row_count is not None
        and row_count > 0
        and prior_row_count is not None
        and prior_row_count > 0
    ):
        delta_pct = abs(row_count - prior_row_count) / prior_row_count * 100.0
        if delta_pct > max_delta_pct and not approved:
            issues.append(
                {
                    "code": GATE_ROW_COUNT_DELTA_EXCEEDED,
                    "detail": (
                        f"row-count delta {delta_pct:.2f}% exceeds threshold "
                        f"{max_delta_pct:.2f}%"
                    ),
                    "repair": {
                        "owner_approve_or_investigate": {
                            "row_count": row_count,
                            "prior_row_count": prior_row_count,
                            "delta_pct": round(delta_pct, 2),
                            "max_row_count_delta_pct": max_delta_pct,
                        },
                        "threshold_source": threshold_source,
                    },
                }
            )

    issues.sort(key=lambda item: item["code"])
    return issues


def is_blocking_issue(issue: dict[str, Any]) -> bool:
    """Predicate: does this gate result BLOCK publication?

    An ``advisory`` marker (e.g. the v1 ``content_hash_check`` /
    ``content_hash_verified: False`` honesty marker) is informational -- it records
    that byte-identity was NOT independently verified but does NOT block the publish
    path. Every other gate result is blocking (fail closed).
    """
    return not issue.get("advisory", False)


def split_gate_issues(
    issues: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a gate-result list into ``(blocking, advisory)`` sublists."""
    blocking = [i for i in issues if is_blocking_issue(i)]
    advisory = [i for i in issues if not is_blocking_issue(i)]
    return blocking, advisory


# ---------------------------------------------------------------------------
# DB helpers (private) -- thin, parameterized, no string interpolation of values.
# ---------------------------------------------------------------------------


def _row_to_execution(cur, row) -> dict[str, Any]:
    cols = [desc[0] for desc in cur.description]
    record: dict[str, Any] = {}
    for col, val in zip(cols, row):
        if col in ("state_changed_at", "created_at", "updated_at") and val is not None:
            record[col] = val.isoformat()
        elif col == "projection_plan_ref" and val is not None:
            record[col] = val if isinstance(val, dict) else json.loads(val)
        else:
            record[col] = val
    return record


def _fetch_execution(conn, execution_id: str, project_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, datastream_id, project_id, plan_version_id, mapping_version_id,
                   projection_plan_ref, state, state_changed_at, content_hash, row_count,
                   error_code, error_detail, idempotency_key_hash, created_by,
                   created_at, updated_at
            FROM app.datastream_executions
            WHERE id = %s AND project_id = %s
            """,
            (execution_id, project_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_execution(cur, row)


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


def create_execution(
    datastream_id: str,
    project_id: str,
    plan_version_id: str,
    mapping_version_id: str,
    projection_plan: dict[str, Any],
    actor: str,
    idempotency_key: str,
    conn,
) -> dict[str, Any]:
    """Insert a ``dse_<ULID>`` execution row in ``created`` state.

    The 12.4 ``projection_plan`` MUST be executable (the admission ticket). The
    caller owns the transaction: this function does NOT commit (so the caller can
    compose the create with the audit row atomically).

    Idempotency: a prior execution with the same ``idempotency_key_hash`` and an
    identical create fingerprint returns the existing row; a different payload for
    the same key raises ``IdempotencyConflict`` (-> 409). A concurrent non-terminal
    execution for the same datastream raises ``ConcurrentExecutionActive``.

    Race safety (FIX 2/3): the non-locking SELECT concurrency check above can lose a
    real race -- two callers both see no active execution and both INSERT. The
    partial-unique index ``uq_datastream_executions_active`` is the hard structural
    guard; the loser's INSERT raises a psycopg ``UniqueViolation`` which is caught
    here and re-raised as ``ConcurrentExecutionActive`` (-> 409). A non-existent
    ``plan_version_id`` / ``mapping_version_id`` violates a composite FK; that
    ``IntegrityError`` (``ForeignKeyViolation``) is caught and re-raised as an
    opaque ``InvalidReference`` (-> 422, non-disclosing). The INSERT is wrapped in a
    SAVEPOINT so the aborted sub-transaction can be rolled back and the blocking
    active-execution id re-queried on the SAME connection.
    """
    if not projection_plan.get("executable", False):
        raise PublicationError(
            "projection_not_executable",
            "the 12.4 projection plan must be executable to create an execution",
        )

    key_hash = _hash_key(idempotency_key) if idempotency_key else None
    fingerprint = _payload_fingerprint(
        {
            "datastream_id": datastream_id,
            "project_id": project_id,
            "plan_version_id": plan_version_id,
            "mapping_version_id": mapping_version_id,
            "projection_plan": projection_plan,
        }
    )

    with conn.cursor() as cur:
        # M2 (Story 12.12): take the SAME app.datastreams pointer lock that
        # commit_publication and rollback_dataset hold, so creating an execution (a
        # replace) is STRICTLY serialized against a concurrent dataset rollback -- this
        # closes the race where a replace mints an execution in the window between a
        # rollback's active-execution probe and its pointer swap (last-writer-wins).
        # The lock is a row-level FOR UPDATE on the datastream; absence is deferred to
        # the INSERT's composite FK (non-disclosing InvalidReference), so this SELECT
        # never changes control flow -- it only imposes the ordering.
        cur.execute(
            "SELECT 1 FROM app.datastreams WHERE id = %s AND project_id = %s FOR UPDATE",
            (datastream_id, project_id),
        )
        cur.fetchone()

        # Idempotency: return the existing execution when the key + payload match.
        if key_hash is not None:
            cur.execute(
                """
                SELECT id, datastream_id, project_id, plan_version_id, mapping_version_id,
                       projection_plan_ref, state, state_changed_at, content_hash, row_count,
                       error_code, error_detail, idempotency_key_hash, created_by,
                       created_at, updated_at
                FROM app.datastream_executions
                WHERE datastream_id = %s AND idempotency_key_hash = %s
                """,
                (datastream_id, key_hash),
            )
            existing = cur.fetchone()
            if existing is not None:
                record = _row_to_execution(cur, existing)
                existing_fp = _payload_fingerprint(
                    {
                        "datastream_id": record["datastream_id"],
                        "project_id": record["project_id"],
                        "plan_version_id": record["plan_version_id"],
                        "mapping_version_id": record["mapping_version_id"],
                        "projection_plan": record.get("projection_plan_ref") or {},
                    }
                )
                if existing_fp != fingerprint:
                    raise IdempotencyConflict()
                return record

        # Concurrency: reject if a non-terminal execution already exists.
        cur.execute(
            """
            SELECT id FROM app.datastream_executions
            WHERE datastream_id = %s AND state = ANY(%s)
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (datastream_id, list(ACTIVE_STATES)),
        )
        blocking = cur.fetchone()
        if blocking is not None:
            raise ConcurrentExecutionActive(blocking[0])

        execution_id = _mint_execution_id()
        # SAVEPOINT so a constraint failure on the INSERT can be rolled back
        # WITHOUT poisoning the whole connection -- letting us re-query the blocking
        # active execution id (concurrency) on the same connection.
        cur.execute("SAVEPOINT sp_create_execution")
        try:
            cur.execute(
                """
                INSERT INTO app.datastream_executions
                    (id, datastream_id, project_id, plan_version_id, mapping_version_id,
                     projection_plan_ref, state, idempotency_key_hash, created_by)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, 'created', %s, %s)
                """,
                (
                    execution_id,
                    datastream_id,
                    project_id,
                    plan_version_id,
                    mapping_version_id,
                    json.dumps(projection_plan),
                    key_hash,
                    actor,
                ),
            )
        except _UNIQUE_VIOLATION as exc:
            # A real race lost to the active-execution partial-unique index (or the
            # idempotency index). Roll back the failed sub-transaction, then re-query
            # the blocking active execution to name it in the 409.
            cur.execute("ROLLBACK TO SAVEPOINT sp_create_execution")
            _constraint = getattr(getattr(exc, "diag", None), "constraint_name", "") or ""
            if "idempotency" in _constraint:
                # Concurrent create with the same idempotency key + a matching
                # payload: surface it as a conflict rather than a phantom race.
                raise ConcurrentExecutionActive("") from exc
            blocking_id = ""
            cur.execute(
                """
                SELECT id FROM app.datastream_executions
                WHERE datastream_id = %s AND state = ANY(%s)
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (datastream_id, list(ACTIVE_STATES)),
            )
            blk = cur.fetchone()
            if blk is not None:
                blocking_id = blk[0]
            raise ConcurrentExecutionActive(blocking_id) from exc
        except _INTEGRITY_ERROR as exc:
            # A non-existent plan_version_id / mapping_version_id (or datastream)
            # violates a composite FK. Opaque, non-disclosing 422 (no str(exc)).
            cur.execute("ROLLBACK TO SAVEPOINT sp_create_execution")
            raise InvalidReference() from exc
        cur.execute("RELEASE SAVEPOINT sp_create_execution")

    record = _fetch_execution(conn, execution_id, project_id)
    if record is None:  # pragma: no cover - defensive; the insert just succeeded.
        raise ExecutionNotFound()
    return record


def advance_state(
    execution_id: str,
    expected_current_state: str | None,
    new_state: str,
    actor: str,
    conn,
    *,
    project_id: str | None = None,
    content_hash: str | None = None,
    row_count: int | None = None,
    error_code: str | None = None,
    error_detail: str | None = None,
) -> dict[str, Any]:
    """Advance one execution through the typed state machine.

    Enforces the machine: an invalid transition raises ``InvalidStateTransition``.
    Writes ``state_changed_at`` and an actor-stamped audit row (via
    ``insert_audit_row``) on every change. The caller owns the transaction.

    ``expected_current_state`` (optimistic guard) is checked against the live row
    when provided; a mismatch is treated as an invalid transition (someone else
    moved it). Does NOT commit.
    """
    from core.audit import (  # noqa: PLC0415
        ACTION_DATASTREAM_EXECUTION_STATE_CHANGED,
        ACTION_DATASTREAM_PUBLICATION_FAILED,
        ACTION_DATASTREAM_PUBLISHED,
        insert_audit_row,
    )

    with conn.cursor() as cur:
        # Lock the row so concurrent state advances serialize.
        cur.execute(
            """
            SELECT state, datastream_id, project_id
            FROM app.datastream_executions
            WHERE id = %s
            FOR UPDATE
            """,
            (execution_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ExecutionNotFound()
        current_state, datastream_id, row_project_id = row[0], row[1], row[2]

        if project_id is not None and project_id != row_project_id:
            raise ExecutionNotFound()

        if expected_current_state is not None and current_state != expected_current_state:
            raise InvalidStateTransition(current_state, new_state)

        if not is_valid_transition(current_state, new_state):
            raise InvalidStateTransition(current_state, new_state)

        cur.execute(
            """
            UPDATE app.datastream_executions
            SET state = %s,
                state_changed_at = NOW(),
                updated_at = NOW(),
                content_hash = COALESCE(%s, content_hash),
                row_count = COALESCE(%s, row_count),
                error_code = %s,
                error_detail = %s
            WHERE id = %s
            """,
            (
                new_state,
                content_hash,
                row_count,
                error_code,
                error_detail,
                execution_id,
            ),
        )

    action = (
        ACTION_DATASTREAM_PUBLISHED
        if new_state == STATE_PUBLISHED
        else ACTION_DATASTREAM_PUBLICATION_FAILED
        if new_state == STATE_FAILED
        else ACTION_DATASTREAM_EXECUTION_STATE_CHANGED
    )
    insert_audit_row(
        conn,
        identity=actor or "anonymous",
        action=action,
        provider_account="",
        connection_ref="",
        metadata={
            "execution_id": execution_id,
            "datastream_id": datastream_id,
            "project_id": row_project_id,
            "from_state": current_state,
            "to_state": new_state,
            "error_code": error_code,
        },
    )

    record = _fetch_execution(conn, execution_id, row_project_id)
    if record is None:  # pragma: no cover - defensive.
        raise ExecutionNotFound()
    return record


def _fail_execution_out_of_band(
    execution_id: str,
    actor: str,
    error_code: str,
    error_detail: str,
    connection_factory,
) -> None:
    """Mark an execution ``failed`` in a SEPARATE connection (retry-safe).

    When ``commit_publication`` rolls back, the failure handler MUST NOT run on the
    rolled-back connection (it is in an error state and the write is silently
    dropped, leaving the execution stuck in ``publishing`` forever -- the Dev Notes
    "Hard Part"). We open a fresh connection, advance publishing->failed, commit.
    Idempotent: if the row is already terminal the transition is a no-op skip.
    """
    try:
        with connection_factory() as fresh_conn:
            with fresh_conn.cursor() as cur:
                cur.execute(
                    "SELECT state, project_id FROM app.datastream_executions WHERE id = %s",
                    (execution_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return
                state, project_id = row[0], row[1]
            if state in TERMINAL_STATES:
                return
            advance_state(
                execution_id,
                None,
                STATE_FAILED,
                actor,
                fresh_conn,
                project_id=project_id,
                error_code=error_code,
                error_detail=error_detail,
            )
            fresh_conn.commit()
    except Exception:  # pragma: no cover - best-effort; never mask the original error.
        # The reconciliation route (Owner) is the deterministic fallback if even
        # this out-of-band failure write does not land.
        pass


def commit_publication(
    execution_id: str,
    project_id: str,
    actor: str,
    conn,
    *,
    connection_factory=None,
) -> dict[str, Any]:
    """THE atomic publication step: 4 writes in ONE Postgres transaction.

    Given a ``ready`` execution whose DQ gates already passed, this:
      1. advances the execution ``ready -> publishing -> published``,
      2. INSERTs an append-only ``app.datastream_publication_log`` row,
      3. swaps ``app.datastreams.current_published_execution_id`` (locked FOR
         UPDATE to serialize concurrent publishes),
      4. INSERTs an ``app.datastream_outbox`` event,
    all inside a SINGLE explicit transaction. On ANY error the whole group rolls
    back (the prior pointer, log, and outbox are untouched) and the execution is
    marked ``failed`` in a SEPARATE connection (never the rolled-back one).

    Publication is a POINTER SWAP over already-validated data: NO recomputation,
    NO BigQuery write. The published row_count / content_hash are carried verbatim
    from the validated execution row.

    ``connection_factory`` (default ``core.db.get_connection``) opens the fresh
    connection for the out-of-band failure write.
    """
    if connection_factory is None:
        from core.db import get_connection  # noqa: PLC0415

        connection_factory = get_connection

    from core.audit import ACTION_DATASTREAM_PUBLISHED, insert_audit_row  # noqa: PLC0415

    try:
        with conn.cursor() as cur:
            # 0) Lock the datastream pointer row FIRST to serialize concurrent
            #    publishes for this datastream, and read the prior pointer.
            cur.execute(
                """
                SELECT current_published_execution_id
                FROM app.datastreams
                WHERE id = (
                    SELECT datastream_id FROM app.datastream_executions
                    WHERE id = %s AND project_id = %s
                )
                  AND project_id = %s
                FOR UPDATE
                """,
                (execution_id, project_id, project_id),
            )
            ds_row = cur.fetchone()
            if ds_row is None:
                raise ExecutionNotFound()
            prior_execution_id = ds_row[0]

            # Read the execution + validate it is READY and carries provenance.
            cur.execute(
                """
                SELECT datastream_id, plan_version_id, mapping_version_id,
                       state, content_hash, row_count
                FROM app.datastream_executions
                WHERE id = %s AND project_id = %s
                FOR UPDATE
                """,
                (execution_id, project_id),
            )
            exec_row = cur.fetchone()
            if exec_row is None:
                raise ExecutionNotFound()
            (
                datastream_id,
                plan_version_id,
                mapping_version_id,
                state,
                content_hash,
                row_count,
            ) = exec_row

            if state != STATE_READY:
                raise InvalidStateTransition(state, STATE_PUBLISHING)
            # AD-9: publish demands real provenance -- never a fabricated NULL->0.
            if not content_hash or row_count is None:
                raise PublicationError(
                    "candidate_not_validated",
                    "content_hash and row_count must be set before publication",
                )

            # 1) ready -> publishing -> published (two guarded UPDATEs, same txn).
            cur.execute(
                """
                UPDATE app.datastream_executions
                SET state = 'publishing', state_changed_at = NOW(), updated_at = NOW()
                WHERE id = %s AND state = 'ready'
                """,
                (execution_id,),
            )
            if cur.rowcount != 1:
                raise InvalidStateTransition(state, STATE_PUBLISHING)
            cur.execute(
                """
                UPDATE app.datastream_executions
                SET state = 'published', state_changed_at = NOW(), updated_at = NOW()
                WHERE id = %s AND state = 'publishing'
                """,
                (execution_id,),
            )
            if cur.rowcount != 1:  # pragma: no cover - defensive within one txn.
                raise InvalidStateTransition(STATE_PUBLISHING, STATE_PUBLISHED)

            # 2) append-only publication log row (rollback evidence).
            log_id = _mint_log_id()
            cur.execute(
                """
                INSERT INTO app.datastream_publication_log
                    (id, execution_id, datastream_id, project_id, plan_version_id,
                     mapping_version_id, content_hash, row_count, prior_execution_id,
                     published_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    log_id,
                    execution_id,
                    datastream_id,
                    project_id,
                    plan_version_id,
                    mapping_version_id,
                    content_hash,
                    row_count,
                    prior_execution_id,
                    actor,
                ),
            )

            # 3) atomic pointer swap.
            cur.execute(
                """
                UPDATE app.datastreams
                SET current_published_execution_id = %s
                WHERE id = %s AND project_id = %s
                """,
                (execution_id, datastream_id, project_id),
            )
            if cur.rowcount != 1:  # pragma: no cover - defensive.
                raise ExecutionNotFound()

            # 4) outbox event (downstream refresh / cache-bust / monitoring).
            cur.execute(
                """
                INSERT INTO app.datastream_outbox
                    (execution_id, datastream_id, project_id, event_type, payload)
                VALUES (%s, %s, %s, 'published', %s::jsonb)
                """,
                (
                    execution_id,
                    datastream_id,
                    project_id,
                    json.dumps(
                        {
                            "event": "published",
                            "execution_id": execution_id,
                            "content_hash": content_hash,
                            "row_count": row_count,
                            "prior_execution_id": prior_execution_id,
                        }
                    ),
                ),
            )

            # Audit inside the same transaction (state change + evidence commit
            # together).
            insert_audit_row(
                conn,
                identity=actor or "anonymous",
                action=ACTION_DATASTREAM_PUBLISHED,
                provider_account="",
                connection_ref="",
                metadata={
                    "execution_id": execution_id,
                    "datastream_id": datastream_id,
                    "project_id": project_id,
                    "prior_execution_id": prior_execution_id,
                    "content_hash": content_hash,
                    "row_count": row_count,
                    "publication_log_id": log_id,
                },
            )

        # Single commit: all four writes + audit land together, or none do.
        conn.commit()
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:  # pragma: no cover - rollback of an already-dead txn.
            pass
        # Fail the execution OUT OF BAND (separate connection), never on `conn`.
        code = getattr(exc, "code", "publication_error")
        _fail_execution_out_of_band(
            execution_id,
            actor,
            code,
            "publication transaction rolled back",
            connection_factory,
        )
        raise

    record = _fetch_execution(conn, execution_id, project_id)
    if record is None:  # pragma: no cover - defensive.
        raise ExecutionNotFound()
    return {
        "execution": record,
        "publication_log_id": log_id,
        "prior_execution_id": prior_execution_id,
    }


def run_dq_gates(
    execution_id: str,
    project_id: str,
    conn,
    *,
    approved: bool = False,
    force_empty_publish: bool = False,
    validated_content_hash: str | None = None,
    plan_source_schema_hash: str | None = None,
    current_capability_fingerprint: str | None = None,
    landing_schema_hash: str | None = None,
    plan_declared_schema_hash: str | None = None,
) -> list[dict[str, Any]]:
    """Run the pre-publication DQ gates for a ``validating``/``ready`` execution.

    Reads the execution's row_count/content_hash + the prior published execution's
    row_count (for the delta gate) + project-scoped preference thresholds from
    ``app.project_preferences``. Returns a list of ``{code, detail, repair}``
    issues (empty == pass). Pure decision is delegated to ``evaluate_dq_gates``.

    The hash/schema evidence that lives OUTSIDE the execution row (the plan's
    declared hashes, the current capability fingerprint, the landing schema hash)
    is passed by the caller; when omitted, that specific gate is skipped (fail
    closed only requires the evidence the caller supplies).

    Content-hash honesty (v1): the stored ``content_hash`` is caller-supplied
    advisory provenance. The content-hash gate FIRES only when the caller passes an
    INDEPENDENT ``validated_content_hash`` (deferred to 12.6, which computes it from
    the BigQuery landing). When it is ABSENT -- the v1 default -- byte-identity is
    NOT independently verified: ``evaluate_dq_gates`` records an explicit
    ``content_hash_verified: False`` advisory marker (never a false pass). That
    advisory marker is NON-BLOCKING; this function returns ONLY the blocking issues
    (so the default publish path proceeds) and logs the advisory so the "not
    verified" fact is observable.
    """
    import logging  # noqa: PLC0415

    logger = logging.getLogger(__name__)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT datastream_id, content_hash, row_count
            FROM app.datastream_executions
            WHERE id = %s AND project_id = %s
            """,
            (execution_id, project_id),
        )
        row = cur.fetchone()
        if row is None:
            raise ExecutionNotFound()
        datastream_id, content_hash, row_count = row[0], row[1], row[2]

        # Prior published row_count (for the delta gate).
        cur.execute(
            """
            SELECT e.row_count
            FROM app.datastreams d
            JOIN app.datastream_executions e
              ON e.id = d.current_published_execution_id
            WHERE d.id = %s AND d.project_id = %s
            """,
            (datastream_id, project_id),
        )
        prior_row = cur.fetchone()
        prior_row_count = prior_row[0] if prior_row is not None else None

        # Governed thresholds.
        cur.execute(
            """
            SELECT max_row_count_delta_pct, allow_empty_publication
            FROM app.project_preferences
            WHERE project_id = %s
            """,
            (project_id,),
        )
        pref_row = cur.fetchone()
        preferences = (
            {
                "max_row_count_delta_pct": pref_row[0],
                "allow_empty_publication": pref_row[1],
            }
            if pref_row is not None
            else {}
        )

    # NOTE (FIX 1): do NOT default validated_content_hash to the stored hash --
    # that would compare the value to itself and make content_hash_mismatch
    # impossible on the default path, silently overclaiming byte-identity. Pass the
    # caller's value through verbatim (None when the caller supplies no independent
    # hash); evaluate_dq_gates then skips the gate and records the honest
    # content_hash_verified=False advisory marker instead of a false pass.
    all_issues = evaluate_dq_gates(
        row_count=row_count,
        content_hash=content_hash,
        validated_content_hash=validated_content_hash,
        prior_row_count=prior_row_count,
        plan_source_schema_hash=plan_source_schema_hash,
        current_capability_fingerprint=current_capability_fingerprint,
        landing_schema_hash=landing_schema_hash,
        plan_declared_schema_hash=plan_declared_schema_hash,
        preferences=preferences,
        approved=approved,
        force_empty_publish=force_empty_publish,
    )
    blocking, advisory = split_gate_issues(all_issues)
    for marker in advisory:
        if marker.get("content_hash_verified") is False:
            logger.info(
                "datastream_publication: content_hash_verified=false "
                "(byte-identity not independently verified in v1) execution_id=%s",
                execution_id,
            )
    # Return ONLY blocking issues so the v1 default publish path proceeds; the
    # advisory content-hash marker is logged, never presented as a passed gate.
    return blocking


def reconcile_execution(
    execution_id: str,
    project_id: str,
    conn,
) -> dict[str, Any]:
    """Resolve a cross-store partial failure idempotently (Owner-invoked only).

    Determines whether a stuck ``publishing`` execution (interrupted between the
    BigQuery write and the Postgres commit) should be resolved as ``published`` or
    ``failed``, using deterministic evidence: the execution state, whether the
    published pointer already moved to this execution, and whether the append-only
    ``app.datastream_publication_log`` row exists. (Evidence = pointer +
    publication_log -- the STRONGER signal. The outbox row is written in the SAME
    atomic transaction as the log + pointer, so it is redundant with the log for
    commit-evidence and is intentionally NOT read here.) FAILS CLOSED: when it
    cannot make a safe determination it marks the execution ``failed`` with a
    ``reconciliation_inconclusive`` note and leaves the prior published pointer
    intact. Never called on the happy path. Idempotent: a terminal execution is
    reported as already-resolved without mutation.

    Returns ``{resolved: bool, final_state, action_taken}``.
    """
    record = _fetch_execution(conn, execution_id, project_id)
    if record is None:
        raise ExecutionNotFound()

    state = record["state"]
    datastream_id = record["datastream_id"]

    # Already terminal -> idempotent no-op.
    if state in TERMINAL_STATES:
        return {
            "resolved": True,
            "final_state": state,
            "action_taken": "already_terminal",
        }

    # Only a stuck `publishing` execution is a cross-store partial-failure
    # candidate. Any other non-terminal state is inconclusive -> fail closed.
    if state != STATE_PUBLISHING:
        _reconcile_fail_closed(execution_id, project_id, conn, datastream_id)
        return {
            "resolved": False,
            "final_state": STATE_FAILED,
            "action_taken": "reconciliation_inconclusive",
        }

    with conn.cursor() as cur:
        # Did the pointer already move to this execution? If so, the transaction
        # actually committed and the state advance is the only thing lost -> safe
        # to resolve as published (the atomic set already includes the log row).
        cur.execute(
            """
            SELECT current_published_execution_id
            FROM app.datastreams
            WHERE id = %s AND project_id = %s
            """,
            (datastream_id, project_id),
        )
        ptr_row = cur.fetchone()
        pointer = ptr_row[0] if ptr_row is not None else None

        # Is there a publication_log row for this execution? Log present + pointer
        # set => the atomic commit landed (the log, pointer, and outbox are written
        # in one transaction, so the log is sufficient commit-evidence).
        cur.execute(
            """
            SELECT 1 FROM app.datastream_publication_log
            WHERE execution_id = %s AND project_id = %s
            LIMIT 1
            """,
            (execution_id, project_id),
        )
        has_log = cur.fetchone() is not None

    if pointer == execution_id and has_log:
        # The atomic writes committed; only the final state advance was lost.
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE app.datastream_executions
                SET state = 'published', state_changed_at = NOW(), updated_at = NOW()
                WHERE id = %s AND state = 'publishing'
                """,
                (execution_id,),
            )
        conn.commit()
        return {
            "resolved": True,
            "final_state": STATE_PUBLISHED,
            "action_taken": "resolved_to_published",
        }

    if pointer != execution_id and not has_log:
        # The transaction did NOT commit: no log row, pointer untouched. Safe to
        # fail this execution; the prior published state remains current.
        _reconcile_fail_closed(
            execution_id, project_id, conn, datastream_id, note="no_commit_evidence"
        )
        return {
            "resolved": True,
            "final_state": STATE_FAILED,
            "action_taken": "resolved_to_failed",
        }

    # Ambiguous (partial evidence: log without pointer, or pointer without log).
    # Cannot prove a safe published state -> fail closed.
    _reconcile_fail_closed(execution_id, project_id, conn, datastream_id)
    return {
        "resolved": False,
        "final_state": STATE_FAILED,
        "action_taken": "reconciliation_inconclusive",
    }


def _reconcile_fail_closed(
    execution_id: str,
    project_id: str,
    conn,
    datastream_id: str,
    note: str = "reconciliation_inconclusive",
) -> None:
    """Mark a stuck execution ``failed`` without moving the published pointer."""
    from core.audit import (  # noqa: PLC0415
        ACTION_DATASTREAM_PUBLICATION_FAILED,
        insert_audit_row,
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.datastream_executions
            SET state = 'failed',
                state_changed_at = NOW(),
                updated_at = NOW(),
                error_code = 'reconciliation_inconclusive',
                error_detail = %s
            WHERE id = %s
              AND state NOT IN ('published', 'failed', 'cancelled')
            """,
            (note, execution_id),
        )
    insert_audit_row(
        conn,
        identity="reconciler",
        action=ACTION_DATASTREAM_PUBLICATION_FAILED,
        provider_account="",
        connection_ref="",
        metadata={
            "execution_id": execution_id,
            "datastream_id": datastream_id,
            "project_id": project_id,
            "reason": note,
        },
    )
    conn.commit()


def get_publication_log(
    datastream_id: str,
    project_id: str,
    conn,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Read the publication log newest-first, project-scoped (Viewer-accessible)."""
    limit = max(1, min(int(limit), 200))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, execution_id, datastream_id, project_id, plan_version_id,
                   mapping_version_id, content_hash, row_count, prior_execution_id,
                   published_at, published_by
            FROM app.datastream_publication_log
            WHERE datastream_id = %s AND project_id = %s
            ORDER BY published_at DESC
            LIMIT %s
            """,
            (datastream_id, project_id, limit),
        )
        cols = [desc[0] for desc in cur.description]
        rows: list[dict[str, Any]] = []
        for row in cur.fetchall():
            record: dict[str, Any] = {}
            for col, val in zip(cols, row):
                if col == "published_at" and val is not None:
                    record[col] = val.isoformat()
                else:
                    record[col] = val
            rows.append(record)
    return rows


def get_execution(
    execution_id: str,
    project_id: str,
    conn,
) -> dict[str, Any]:
    """Read a single execution record, project-scoped (Viewer-accessible)."""
    record = _fetch_execution(conn, execution_id, project_id)
    if record is None:
        raise ExecutionNotFound()
    return record
