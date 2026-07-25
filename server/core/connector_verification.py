"""Story 38.4 -- continuous domain verification and synthetic test delivery.

A platform operator runs verification to evaluate the evidence that the active
domain config is correctly routing deliveries. Each run is immutable evidence
appended to ``app.connector_verification_runs`` and drives the installation state
machine: a passing run advances DOMAIN_PENDING/VERIFYING -> READY; a failing run
on a READY installation moves it to DEGRADED (with first-seen carried forward from
the prior degraded run, if any).

A synthetic test delivery exercises the receipt-adapter authentication seam at
auth-only level and is explicitly flagged ``synthetic_delivery = TRUE``. It writes
NO durable receipt, quarantine object, mapping row, or publication -- none of those
stages exist yet in 38.4. Its outcome is recorded as a verification evidence row,
not as a tenant import.

INVARIANTS (adversarially enforced, mirroring connector_installation.py):

  * SOURCE-AGNOSTIC. No provider/adapter/vendor vocabulary enters this module --
    not in code, not in comments, not in docstrings (the AD-2 boundary scanner
    is literal). Check functions are INJECTABLE: they are passed in by the caller
    so provider-specific logic lives outside core (AD-2). Synthetic delivery calls
    the receipt-adapter seam through an injectable runner function that the caller
    supplies; core only defines the shape of that call.

  * EVERY WRITE ROUTES THROUGH ``operations.execute_operation`` (atomic audit +
    outbox, idempotent replay). No parallel audit path exists here.

  * SECRET-FREE READ MODEL. ``get_verification_state`` and every return value
    carry NO DNS token, NO signing secret, NO raw credential. ``evidence_hash``
    is a sha256 digest of evidence class + timestamp -- safe for storage.

  * DETERMINISTIC IDEMPOTENCY (review H1). No random id enters
    ``request_payload``. Row ids are generated at WRITE TIME inside the mutation
    closure. Two identical runs with the same Idempotency-Key replay cleanly.

  * ROWCOUNT CHECKED (review H2). INSERT uses ``ON CONFLICT DO NOTHING``; the
    rowcount is verified; on a lost race the call reconciles to the persisted row.
    The safe read-model is derived from ``op_result.result``, not a hard-coded target.

  * SYNTHETIC DELIVERY SHORT-CIRCUITS (AC3). The injectable delivery runner MUST
    NOT write a quarantine object, raw import, or publication. The mutation asserts
    ``synthetic_delivery = TRUE`` on the appended run row. No Datastream import
    path is called here.

  * CARRY FORWARD first_seen_at (AC2). When a READY installation degrades, the
    ``first_seen_at`` is carried forward from the prior degraded run (if any) so
    operators see the original onset time, not the most recent detection.

Mirrors ``connector_installation.py`` and ``connector_domain.py`` conventions:
``from __future__ import annotations``, lazy imports of the shared seams,
mutations only through the operation seam, ASCII-only source.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from ulid import ULID

from core.operations import MutationResult, OperationSpec, execute_operation

# ---------------------------------------------------------------------------
# Constants / evidence classes (AC1, Task 2).
# ---------------------------------------------------------------------------

#: Outcome values matching the CHECK constraint in migration 085.
OUTCOME_PASSED = "passed"
OUTCOME_FAILED = "failed"
OUTCOME_DEGRADED = "degraded"
OUTCOME_NOT_CONFIGURED = "not_configured"

#: Evidence class tags. Opaque strings only -- no provider vocabulary.
EVIDENCE_CLASS_ROUTING = "routing_check"
EVIDENCE_CLASS_AUTH = "auth_check"
EVIDENCE_CLASS_SYNTHETIC = "synthetic_delivery_auth"
EVIDENCE_CLASS_NO_CHECKS = "no_checks"

# States from which a passing verification can advance to READY.
# Includes DEGRADED (F2): DEGRADED -> READY is an allowed transition and must
# be driven on all-pass so a recovered installation can return to READY.
_ADVANCEABLE_TO_READY: frozenset[str] = frozenset({"DOMAIN_PENDING", "VERIFYING", "DEGRADED"})

# States that can degrade to DEGRADED on a failing check.
_DEGRADABLE: frozenset[str] = frozenset({"READY"})

# Default re-check interval TTL in seconds (1 hour).
_DEFAULT_TTL_SECONDS = 3600


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ConnectorVerificationError(ValueError):
    """Raised before SQL when verification inputs are unsafe or malformed."""


class ConnectorVerificationUnavailable(RuntimeError):
    """The installation or domain config row is missing; verification cannot run."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _evidence_hash(evidence_class: str, timestamp_isoformat: str) -> str:
    """Produce a sha256 digest of evidence class + timestamp (safe for storage).

    Never hashes a secret, DNS token, or signing value -- only the opaque
    evidence class string and the ISO-8601 wall-clock string.
    """
    payload = json.dumps(
        {"evidence_class": evidence_class, "sampled_at": timestamp_isoformat},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_iso(ts: Any) -> str | None:
    """Serialize a timestamp to ISO-8601, or None."""
    if ts is None:
        return None
    try:
        return ts.isoformat()
    except AttributeError:
        return str(ts)


# ---------------------------------------------------------------------------
# Safe read-model projection (AC4). No secret, no DNS token, no credential.
# ---------------------------------------------------------------------------

_SAFE_NEXT_ACTIONS: dict[str, str] = {
    "passed":   "no action required -- verification passed; connector is READY",
    "failed":   "platform_admin: resolve blocking reason, then re-run verification",
    "degraded": "platform_admin: resolve blocking reason, then re-run verification",
}


def _safe_run_read_model(
    *,
    connector_name: str,
    environment: str,
    state: str,
    outcome: str | None,
    evidence_class: str | None,
    first_seen_at: Any,
    created_at: Any,
    blocking_reason: str | None,
    synthetic_delivery: bool,
) -> dict[str, Any]:
    """Build the secret-free read-model for a verification run.

    NEVER includes evidence_hash, signing secrets, DNS tokens, or any raw
    credential (AC4, E38-NFR03).
    """
    return {
        "connector_name": connector_name,
        "environment": environment,
        "installation_state": state,
        "last_outcome": outcome,
        "evidence_class": evidence_class,
        "first_seen_at": _safe_iso(first_seen_at),
        "last_run_at": _safe_iso(created_at),
        "blocking_reason": blocking_reason,
        "synthetic_delivery": synthetic_delivery,
        "safe_next_action": _SAFE_NEXT_ACTIONS.get(outcome or "", "run verification"),
    }


# ---------------------------------------------------------------------------
# get_verification_state -- nondisclosing read (AC4, Task 2).
# ---------------------------------------------------------------------------


def get_verification_state(
    conn,
    *,
    environment: str,
    connector_name: str,
) -> dict[str, Any] | None:
    """Return the safe read-model for the latest verification run, or None.

    None means no verification run has ever been recorded for this connector
    in this environment. Callers treat None as unverified. Does NOT raise for
    absence; existence is never disclosed to unauthorized callers (that is the
    REST layer's responsibility).

    NEVER returns evidence_hash, signing secrets, DNS tokens, or any raw
    credential (AC4, E38-NFR03).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.outcome, r.evidence_class, r.first_seen_at, r.created_at, "
            "       r.blocking_reason, r.synthetic_delivery, i.state "
            "FROM app.connector_verification_runs r "
            "JOIN app.connector_installations i "
            "     ON i.id = r.installation_id "
            "WHERE r.environment = %s AND r.connector_name = %s "
            "ORDER BY r.created_at DESC "
            "LIMIT 1",
            (environment, connector_name),
        )
        row = cur.fetchone()
    if row is None:
        return None
    outcome, ev_class, first_seen, last_run, reason, synthetic, inst_state = row
    return _safe_run_read_model(
        connector_name=connector_name,
        environment=environment,
        state=inst_state,
        outcome=outcome,
        evidence_class=ev_class,
        first_seen_at=first_seen,
        created_at=last_run,
        blocking_reason=reason,
        synthetic_delivery=bool(synthetic),
    )


# ---------------------------------------------------------------------------
# run_verification -- evaluate checks and drive state (AC1, AC2, Task 2).
# ---------------------------------------------------------------------------


def run_verification(
    conn,
    *,
    environment: str,
    connector_name: str,
    checks: list[Callable[[], tuple[bool, str, str]]],
    actor: str,
    idempotency_key: str,
    host_context: dict[str, Any],
    trace_id: str | None,
) -> dict[str, Any]:
    """Evaluate injectable checks, append a run, and drive the installation state.

    Parameters
    ----------
    checks:
        A list of zero-argument callables, each returning
        ``(passed: bool, evidence_class: str, reason: str)``.
        All checks are evaluated; the first failure wins.
        The callables are OPAQUE to this module (AD-2): they contain any
        provider-specific logic the caller requires, but this module never
        inspects or names them.

    Returns the safe read-model (secret-free).

    State transitions (AC1):
      * DOMAIN_PENDING / VERIFYING -> READY on all-pass.
      * READY -> DEGRADED on any blocking check failing; first_seen_at carried
        forward from the most recent prior DEGRADED run (AC2).

    Each run is an immutable append to ``app.connector_verification_runs``
    through ``execute_operation`` (AC5).
    """
    # ------------------------------------------------------------------
    # Validation.
    # ------------------------------------------------------------------
    if not isinstance(environment, str) or not environment.strip():
        raise ConnectorVerificationError("environment is required")
    if not isinstance(connector_name, str) or not connector_name.strip():
        raise ConnectorVerificationError("connector_name is required")
    if not isinstance(actor, str) or not actor.strip():
        raise ConnectorVerificationError("actor is required")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ConnectorVerificationError("idempotency_key is required")

    environment = environment.strip()
    connector_name = connector_name.strip()

    # ------------------------------------------------------------------
    # Load the installation row.
    # ------------------------------------------------------------------
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, state "
            "FROM app.connector_installations "
            "WHERE environment = %s AND connector_name = %s",
            (environment, connector_name),
        )
        inst_row = cur.fetchone()

    if inst_row is None:
        raise ConnectorVerificationUnavailable(
            "connector installation row not found -- apply installation first"
        )
    installation_id, current_state = inst_row

    # ------------------------------------------------------------------
    # Load the active domain config id (optional -- may be None if unconfigured).
    # ------------------------------------------------------------------
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.connector_domain_configs "
            "WHERE environment = %s AND connector_name = %s "
            "AND superseded_by IS NULL",
            (environment, connector_name),
        )
        dc_row = cur.fetchone()

    domain_config_id: str | None = dc_row[0] if dc_row is not None else None

    # ------------------------------------------------------------------
    # F5: Zero checks must NOT silently pass.
    # When no checks are configured at all, return a distinct not_configured
    # outcome WITHOUT writing a DB row (the CHECK constraint only allows
    # 'passed'/'failed'/'degraded'; 'not_configured' is a read-model-only
    # sentinel). No state advance. Surfaced in the API response via
    # checks_evaluated=0 and outcome="not_configured".
    # ------------------------------------------------------------------
    if not checks:
        rm = _safe_run_read_model(
            connector_name=connector_name,
            environment=environment,
            state=current_state,
            outcome=OUTCOME_NOT_CONFIGURED,
            evidence_class=EVIDENCE_CLASS_NO_CHECKS,
            first_seen_at=None,
            created_at=None,
            blocking_reason=None,
            synthetic_delivery=False,
        )
        rm["checks_evaluated"] = 0
        return rm

    # ------------------------------------------------------------------
    # Evaluate checks (all checks run; first failure short-circuits final
    # outcome determination after the loop).
    # ------------------------------------------------------------------
    checks_evaluated = len(checks)
    all_pass = True
    fail_class = EVIDENCE_CLASS_ROUTING
    fail_reason: str | None = None
    check_results: list[tuple[bool, str, str]] = []
    for check_fn in checks:
        try:
            passed, ev_class, reason = check_fn()
        except Exception as exc:  # noqa: BLE001
            passed, ev_class, reason = False, EVIDENCE_CLASS_ROUTING, f"check_error: {exc}"
        check_results.append((passed, ev_class, reason))
        if not passed and all_pass:
            all_pass = False
            fail_class = ev_class
            fail_reason = reason

    # ------------------------------------------------------------------
    # Determine outcome and state transition.
    # ------------------------------------------------------------------
    if all_pass:
        outcome = OUTCOME_PASSED
        evidence_class = EVIDENCE_CLASS_ROUTING
        blocking_reason = None
        first_seen_at: Any = None

        # State advance: DOMAIN_PENDING / VERIFYING / DEGRADED -> READY (F2).
        target_state: str | None = None
        if current_state in _ADVANCEABLE_TO_READY:
            target_state = "READY"
        # READY stays READY -- just record the evidence (no transition needed).
    else:
        evidence_class = fail_class
        blocking_reason = fail_reason

        if current_state in _DEGRADABLE:
            outcome = OUTCOME_DEGRADED
            target_state = "DEGRADED"
            # Carry forward first_seen_at from the prior DEGRADED run (AC2).
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT first_seen_at FROM app.connector_verification_runs "
                    "WHERE environment = %s AND connector_name = %s "
                    "AND outcome = 'degraded' "
                    "ORDER BY created_at DESC LIMIT 1",
                    (environment, connector_name),
                )
                prior_deg = cur.fetchone()
            # F6: On first-ever degradation (no prior degraded run), set first_seen_at
            # to NOW() via SQL in the INSERT (DB-authoritative timestamp). We signal
            # this by using the sentinel value "SET_TO_NOW" so the mutation uses
            # NOW() in SQL rather than a Python timestamp.
            first_seen_at = prior_deg[0] if prior_deg is not None else "SET_TO_NOW"
        else:
            outcome = OUTCOME_FAILED
            target_state = None
            first_seen_at = None

    # ------------------------------------------------------------------
    # Build OperationSpec (deterministic idempotency -- no random id in payload).
    # ------------------------------------------------------------------
    spec = OperationSpec(
        command_type="connector.verification.ran",
        actor=actor,
        effective_org_id="platform",
        resource_path=(
            "platform:connector-verification-runs",
            f"environment:{environment}",
            f"connector:{connector_name}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={
            "policy": "connector-verification-v1",
            "catalog": "connector-verification-v1",
            "tool": "rest-v1",
        },
        # Deterministic over business inputs only (review H1). No row id here.
        request_payload={
            "environment": environment,
            "connector_name": connector_name,
            "outcome": outcome,
            "evidence_class": evidence_class,
            "synthetic_delivery": False,
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=(
            f"connector-verification:{environment}:{connector_name}:{idempotency_key}"
        ),
        trace_id=trace_id,
    )

    # Capture first_seen_at in closure for the mutation (avoids mutation arg).
    # The sentinel "SET_TO_NOW" means the DB supplies the timestamp via NOW().
    _first_seen = first_seen_at

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        # Generate row id at write time (review H1: not hashed).
        run_id = f"cvr_{ULID()}"

        # Compute evidence hash over class + current wall-clock string.
        import datetime as _dt  # noqa: PLC0415
        sampled_at = _dt.datetime.utcnow().isoformat()
        ev_hash = _evidence_hash(evidence_class, sampled_at)

        # F6: When first_seen_at sentinel is "SET_TO_NOW", use NOW() in SQL so
        # the timestamp is DB-authoritative (onset = this run's created_at).
        if _first_seen == "SET_TO_NOW":
            with operation_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.connector_verification_runs "
                    "(id, installation_id, domain_config_id, environment, "
                    "connector_name, outcome, evidence_class, evidence_hash, "
                    "blocking_reason, first_seen_at, ttl_seconds, "
                    "synthetic_delivery, operation_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s, FALSE, %s) "
                    "ON CONFLICT DO NOTHING",
                    (
                        run_id,
                        installation_id,
                        domain_config_id,
                        environment,
                        connector_name,
                        outcome,
                        evidence_class,
                        ev_hash,
                        blocking_reason,
                        _DEFAULT_TTL_SECONDS,
                        operation_id,
                    ),
                )
                if cur.rowcount != 1:
                    # Reconcile to the persisted row (review H2).
                    cur.execute(
                        "SELECT id, outcome, evidence_class, blocking_reason, "
                        "first_seen_at, created_at "
                        "FROM app.connector_verification_runs "
                        "WHERE environment = %s AND connector_name = %s "
                        "ORDER BY created_at DESC LIMIT 1",
                        (environment, connector_name),
                    )
                    race_row = cur.fetchone()
                    if race_row is None:  # pragma: no cover
                        from core.operations import _canonical_hash  # noqa: PLC0415
                        result = {"run_id": run_id, "outcome": outcome,
                                  "evidence_class": evidence_class}
                        return MutationResult(
                            outcome="succeeded", before_hash=None,
                            after_hash=_canonical_hash(result), result=result,
                            outbox_payload=result,
                        )
                    r_id, r_outcome, r_ev_class, r_reason, r_first, r_created = race_row
                    from core.operations import _canonical_hash  # noqa: PLC0415
                    result = {"run_id": r_id, "outcome": r_outcome,
                              "evidence_class": r_ev_class,
                              "first_seen_at": r_first.isoformat() if r_first else None}
                    return MutationResult(
                        outcome="succeeded", before_hash=None,
                        after_hash=_canonical_hash(result), result=result,
                        outbox_payload=result,
                    )

            from core.operations import _canonical_hash  # noqa: PLC0415
            result = {
                "run_id": run_id,
                "environment": environment,
                "connector_name": connector_name,
                "outcome": outcome,
                "evidence_class": evidence_class,
                "blocking_reason": blocking_reason,
                "first_seen_at": "now",  # sentinel: actual value set by DB NOW()
            }
            return MutationResult(
                outcome="succeeded",
                before_hash=None,
                after_hash=_canonical_hash(result),
                result=result,
                outbox_payload={
                    "connector_name": connector_name,
                    "environment": environment,
                    "outcome": outcome,
                },
            )

        with operation_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.connector_verification_runs "
                "(id, installation_id, domain_config_id, environment, "
                "connector_name, outcome, evidence_class, evidence_hash, "
                "blocking_reason, first_seen_at, ttl_seconds, "
                "synthetic_delivery, operation_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s) "
                "ON CONFLICT DO NOTHING",
                (
                    run_id,
                    installation_id,
                    domain_config_id,
                    environment,
                    connector_name,
                    outcome,
                    evidence_class,
                    ev_hash,
                    blocking_reason,
                    _first_seen,
                    _DEFAULT_TTL_SECONDS,
                    operation_id,
                ),
            )
            if cur.rowcount != 1:
                # Reconcile to the persisted row (review H2).
                cur.execute(
                    "SELECT id, outcome, evidence_class, blocking_reason, "
                    "first_seen_at, created_at "
                    "FROM app.connector_verification_runs "
                    "WHERE environment = %s AND connector_name = %s "
                    "ORDER BY created_at DESC LIMIT 1",
                    (environment, connector_name),
                )
                race_row = cur.fetchone()
                if race_row is None:  # pragma: no cover
                    from core.operations import _canonical_hash  # noqa: PLC0415
                    result = {"run_id": run_id, "outcome": outcome,
                              "evidence_class": evidence_class}
                    return MutationResult(
                        outcome="succeeded", before_hash=None,
                        after_hash=_canonical_hash(result), result=result,
                        outbox_payload=result,
                    )
                (
                    r_id, r_outcome, r_ev_class, r_reason, r_first, r_created,
                ) = race_row
                from core.operations import _canonical_hash  # noqa: PLC0415
                result = {"run_id": r_id, "outcome": r_outcome,
                          "evidence_class": r_ev_class}
                return MutationResult(
                    outcome="succeeded", before_hash=None,
                    after_hash=_canonical_hash(result), result=result,
                    outbox_payload=result,
                )

        from core.operations import _canonical_hash  # noqa: PLC0415
        result = {
            "run_id": run_id,
            "environment": environment,
            "connector_name": connector_name,
            "outcome": outcome,
            "evidence_class": evidence_class,
            "blocking_reason": blocking_reason,
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={
                "connector_name": connector_name,
                "environment": environment,
                "outcome": outcome,
            },
        )

    op_result = execute_operation(conn, spec, mutation=mutation)

    # ------------------------------------------------------------------
    # F1: Drive the installation state transition via the LEGAL walk.
    #
    # The allowed path to READY from DOMAIN_PENDING is:
    #   DOMAIN_PENDING -> VERIFYING  (hop 1, key + ":to_verifying")
    #   VERIFYING      -> READY      (hop 2, key + ":to_ready")
    # From VERIFYING or DEGRADED a single hop suffices:
    #   VERIFYING  -> READY          (key + ":to_ready")
    #   DEGRADED   -> READY          (key + ":to_ready")
    # From READY no transition is needed (already there).
    #
    # Each hop is its own transition_state call with a derived idempotency key
    # so replays are clean. ConnectorInstallationConflict is only benign when
    # the current state already equals the hop target (idempotent / race);
    # otherwise it is a real error and must propagate.
    #
    # F4: After the walk, RE-READ the persisted installation state and build
    # the read-model from THAT actual value, never the intended target.
    # ------------------------------------------------------------------
    if target_state is not None:
        from core.connector_installation import (  # noqa: PLC0415
            ConnectorInstallationConflict,
            get_installation_state,
            transition_state,
        )

        def _hop(from_state: str, to_state: str, suffix: str) -> None:
            """Drive one hop; treat ConnectorInstallationConflict as benign only if
            the installation already reached the hop target (idempotent / race won)."""
            import datetime as _dt2  # noqa: PLC0415
            try:
                transition_state(
                    conn,
                    environment=environment,
                    connector_name=connector_name,
                    target_state=to_state,
                    responsible_actor=actor,
                    blocking_cause=blocking_reason,
                    last_verified_at=(
                        _dt2.datetime.now(_dt2.UTC) if to_state == "READY" else None
                    ),
                    actor=actor,
                    idempotency_key=f"{idempotency_key}{suffix}",
                    host_context=host_context,
                    trace_id=trace_id,
                )
            except ConnectorInstallationConflict as exc:
                # Benign only if the installation already reached this hop target
                # (concurrent run won the race); otherwise it is a real error.
                current = get_installation_state(
                    conn, environment=environment, connector_name=connector_name,
                )
                actual = (current or {}).get("state", "")
                if actual != to_state:
                    raise ConnectorInstallationConflict(
                        f"state hop {from_state!r}->{to_state!r} conflict: "
                        f"actual state is {actual!r}, not {to_state!r}"
                    ) from exc

        if current_state == "DOMAIN_PENDING" and target_state == "READY":
            # Two-hop walk: DOMAIN_PENDING -> VERIFYING -> READY.
            _hop("DOMAIN_PENDING", "VERIFYING", ":to_verifying")
            _hop("VERIFYING", "READY", ":to_ready")
        elif target_state == "READY":
            # Single hop: VERIFYING -> READY or DEGRADED -> READY.
            _hop(current_state, "READY", ":to_ready")
        else:
            # Single hop to DEGRADED (READY -> DEGRADED).
            _hop(current_state, target_state, f":state:{target_state}")

    # F4: Re-read the actual persisted installation state; never report the intended
    # target as the final state. Fall back to current_state only if the row is gone.
    from core.connector_installation import (  # noqa: PLC0415
        get_installation_state,
    )
    actual_inst = get_installation_state(
        conn, environment=environment, connector_name=connector_name,
    )
    actual_state = (actual_inst or {}).get("state", current_state)

    data = op_result.result or {}
    rm = _safe_run_read_model(
        connector_name=connector_name,
        environment=environment,
        state=actual_state,
        outcome=data.get("outcome", outcome),
        evidence_class=data.get("evidence_class", evidence_class),
        first_seen_at=(None if first_seen_at == "SET_TO_NOW" else first_seen_at),
        created_at=None,
        blocking_reason=data.get("blocking_reason", blocking_reason),
        synthetic_delivery=False,
    )
    rm["checks_evaluated"] = checks_evaluated
    return rm


# ---------------------------------------------------------------------------
# run_synthetic_delivery -- auth-only test delivery (AC3, Task 2).
# ---------------------------------------------------------------------------


def run_synthetic_delivery(
    conn,
    *,
    environment: str,
    connector_name: str,
    delivery_runner: Callable[[], tuple[bool, str]],
    actor: str,
    idempotency_key: str,
    host_context: dict[str, Any],
    trace_id: str | None,
) -> dict[str, Any]:
    """Exercise the auth-only path and record a synthetic run (AC3, Task 2).

    Parameters
    ----------
    delivery_runner:
        A zero-argument callable that exercises the receipt-adapter
        authentication seam and returns ``(auth_passed: bool, reason: str)``.

        The runner is OPAQUE to this module (AD-2): it may call any
        adapter seam the caller chooses, but this module never names or
        imports it. The caller is responsible for ensuring the runner
        DOES NOT write a quarantine object, raw import, mapping row, or
        publication (AC3: synthetic = auth-only).

    Returns the safe read-model (secret-free). The run is explicitly
    flagged ``synthetic_delivery = TRUE`` in the DB and in the response.
    """
    # ------------------------------------------------------------------
    # Validation.
    # ------------------------------------------------------------------
    if not isinstance(environment, str) or not environment.strip():
        raise ConnectorVerificationError("environment is required")
    if not isinstance(connector_name, str) or not connector_name.strip():
        raise ConnectorVerificationError("connector_name is required")
    if not isinstance(actor, str) or not actor.strip():
        raise ConnectorVerificationError("actor is required")
    # F8: validate idempotency_key (mirrors run_verification guard).
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ConnectorVerificationError("idempotency_key is required")

    environment = environment.strip()
    connector_name = connector_name.strip()

    # ------------------------------------------------------------------
    # Load the installation row.
    # ------------------------------------------------------------------
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, state "
            "FROM app.connector_installations "
            "WHERE environment = %s AND connector_name = %s",
            (environment, connector_name),
        )
        inst_row = cur.fetchone()

    if inst_row is None:
        raise ConnectorVerificationUnavailable(
            "connector installation row not found -- apply installation first"
        )
    installation_id, current_state = inst_row

    # ------------------------------------------------------------------
    # Load the active domain config id (optional).
    # ------------------------------------------------------------------
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.connector_domain_configs "
            "WHERE environment = %s AND connector_name = %s "
            "AND superseded_by IS NULL",
            (environment, connector_name),
        )
        dc_row = cur.fetchone()

    domain_config_id: str | None = dc_row[0] if dc_row is not None else None

    # ------------------------------------------------------------------
    # Run the synthetic delivery via the injectable runner (auth-only).
    # The runner MUST NOT write any durable receipt, quarantine, import or
    # publication. This module never calls any import path (AC3).
    # ------------------------------------------------------------------
    try:
        auth_passed, auth_reason = delivery_runner()
    except Exception as exc:  # noqa: BLE001
        auth_passed, auth_reason = False, f"runner_error: {exc}"

    outcome = OUTCOME_PASSED if auth_passed else OUTCOME_FAILED
    evidence_class = EVIDENCE_CLASS_SYNTHETIC
    blocking_reason: str | None = None if auth_passed else auth_reason

    # ------------------------------------------------------------------
    # Build OperationSpec (deterministic idempotency -- no random id in payload).
    # ------------------------------------------------------------------
    spec = OperationSpec(
        command_type="connector.verification.ran",
        actor=actor,
        effective_org_id="platform",
        resource_path=(
            "platform:connector-verification-runs",
            f"environment:{environment}",
            f"connector:{connector_name}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={
            "policy": "connector-verification-v1",
            "catalog": "connector-verification-v1",
            "tool": "rest-v1",
        },
        # Deterministic over business inputs only (review H1). No row id here.
        request_payload={
            "environment": environment,
            "connector_name": connector_name,
            "outcome": outcome,
            "evidence_class": evidence_class,
            "synthetic_delivery": True,
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=(
            f"connector-verification:{environment}:{connector_name}:{idempotency_key}:synthetic"
        ),
        trace_id=trace_id,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        # Generate row id at write time (review H1: not hashed).
        run_id = f"cvr_{ULID()}"

        import datetime as _dt  # noqa: PLC0415
        sampled_at = _dt.datetime.utcnow().isoformat()
        ev_hash = _evidence_hash(evidence_class, sampled_at)

        with operation_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.connector_verification_runs "
                "(id, installation_id, domain_config_id, environment, "
                "connector_name, outcome, evidence_class, evidence_hash, "
                "blocking_reason, first_seen_at, ttl_seconds, "
                "synthetic_delivery, operation_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s, TRUE, %s) "
                "ON CONFLICT DO NOTHING",
                (
                    run_id,
                    installation_id,
                    domain_config_id,
                    environment,
                    connector_name,
                    outcome,
                    evidence_class,
                    ev_hash,
                    blocking_reason,
                    _DEFAULT_TTL_SECONDS,
                    operation_id,
                ),
            )
            if cur.rowcount != 1:
                # Reconcile to the persisted row (review H2).
                cur.execute(
                    "SELECT id, outcome, evidence_class "
                    "FROM app.connector_verification_runs "
                    "WHERE environment = %s AND connector_name = %s "
                    "AND synthetic_delivery = TRUE "
                    "ORDER BY created_at DESC LIMIT 1",
                    (environment, connector_name),
                )
                race_row = cur.fetchone()
                if race_row is None:  # pragma: no cover
                    from core.operations import _canonical_hash  # noqa: PLC0415
                    result = {"run_id": run_id, "outcome": outcome,
                              "synthetic_delivery": True}
                    return MutationResult(
                        outcome="succeeded", before_hash=None,
                        after_hash=_canonical_hash(result), result=result,
                        outbox_payload=result,
                    )
                r_id, r_outcome, r_ev_class = race_row
                from core.operations import _canonical_hash  # noqa: PLC0415
                result = {"run_id": r_id, "outcome": r_outcome,
                          "evidence_class": r_ev_class, "synthetic_delivery": True}
                return MutationResult(
                    outcome="succeeded", before_hash=None,
                    after_hash=_canonical_hash(result), result=result,
                    outbox_payload=result,
                )

        from core.operations import _canonical_hash  # noqa: PLC0415
        result = {
            "run_id": run_id,
            "environment": environment,
            "connector_name": connector_name,
            "outcome": outcome,
            "evidence_class": evidence_class,
            "blocking_reason": blocking_reason,
            "synthetic_delivery": True,
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={
                "connector_name": connector_name,
                "environment": environment,
                "outcome": outcome,
                "synthetic_delivery": True,
            },
        )

    op_result = execute_operation(conn, spec, mutation=mutation)
    data = op_result.result or {}
    return _safe_run_read_model(
        connector_name=connector_name,
        environment=environment,
        state=current_state,
        outcome=data.get("outcome", outcome),
        evidence_class=data.get("evidence_class", evidence_class),
        first_seen_at=None,
        created_at=None,
        blocking_reason=data.get("blocking_reason", blocking_reason),
        synthetic_delivery=True,
    )
