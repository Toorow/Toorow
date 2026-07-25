"""Story 38.2 -- installation readiness as independent, audited connector state.

A platform administrator records that a named connector has been installed,
configured and verified in a given deployment environment. The state machine
(`NOT_INSTALLED` -> `DOMAIN_PENDING` -> `VERIFYING` -> `READY`; `READY` <->
`DEGRADED`; any -> `DISABLED`) is audited end-to-end through the shared
``operations.execute_operation`` spine so every transition is durable and
idempotent.

INVARIANTS (the 38.2 review is adversarial on these):

  * SOURCE-AGNOSTIC. The connector is referenced ONLY by its `connector_name`
    string. NO provider/vendor vocabulary enters this module
    (AD-2). `blocking_cause` is a SANITIZED human-facing string -- it never
    carries a secret, a credential, an API key or cross-tenant infra detail.

  * EVERY WRITE ROUTES THROUGH ``operations.execute_operation`` (atomic audit +
    outbox, idempotent replay). No parallel audit path exists here.

  * NONDISCLOSING READ MODEL. The safe projection (`state`, `safe_next_action`,
    `responsible_actor`, `blocking_cause`, `last_verified_at`) carries no secret
    and no cross-tenant infrastructure detail. It is identical whether returned by
    REST or MCP.

  * ILLEGAL TRANSITIONS ARE REJECTED at the app layer before any SQL, mirroring
    the `host_preflight` and `source_delegation` patterns.

Mirrors ``host_preflight`` conventions: ``from __future__ import annotations``,
lazy imports of the shared seams, a state machine driven only through the
operation seam, ASCII-only source.
"""

from __future__ import annotations

from typing import Any

from ulid import ULID

from core.operations import MutationResult, OperationSpec, execute_operation

# ---------------------------------------------------------------------------
# State machine definition (AC1, Task 2).
# ---------------------------------------------------------------------------

#: All valid installation states.
INSTALLATION_STATES: frozenset[str] = frozenset({
    "NOT_INSTALLED",
    "DOMAIN_PENDING",
    "VERIFYING",
    "READY",
    "DEGRADED",
    "DISABLED",
})

#: Terminal states: once DISABLED, the row may not transition further.
TERMINAL_STATES: frozenset[str] = frozenset({"DISABLED"})

# Allowed transitions: {from_state: {to_state, ...}}.
# NOT_INSTALLED -> DOMAIN_PENDING -> VERIFYING -> READY
# READY <-> DEGRADED
# any -> DISABLED
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "NOT_INSTALLED": frozenset({"DOMAIN_PENDING", "DISABLED"}),
    "DOMAIN_PENDING": frozenset({"VERIFYING", "DISABLED"}),
    "VERIFYING": frozenset({"READY", "DEGRADED", "DISABLED"}),
    "READY": frozenset({"DEGRADED", "DISABLED"}),
    "DEGRADED": frozenset({"READY", "VERIFYING", "DISABLED"}),
    "DISABLED": frozenset(),  # terminal
}

# Safe next-action descriptions keyed by state. These are human-readable,
# source-agnostic, and safe to surface in any API / MCP / UI context.
_SAFE_NEXT_ACTIONS: dict[str, str] = {
    "NOT_INSTALLED":  "platform_admin: apply installation to advance to DOMAIN_PENDING",
    "DOMAIN_PENDING": "platform_admin: configure domain routing to advance to VERIFYING",
    "VERIFYING":      "platform_admin: wait for automated verification to advance to READY",
    "READY":          "no action required -- connector is available for tenant activation",
    "DEGRADED":       "platform_admin: resolve cause, then advance to READY or VERIFYING",
    "DISABLED":       "platform_admin: re-apply installation to re-enable",
}


class ConnectorInstallationValidationError(ValueError):
    """Raised before SQL when installation inputs are unsafe or malformed."""


class ConnectorInstallationConflict(RuntimeError):
    """The installation cannot transition from its current state."""


class ConnectorInstallationUnavailable(RuntimeError):
    """The installation row does not exist or is not in a usable state."""


# ---------------------------------------------------------------------------
# Safe read-model projection (AC2, AC6). No secret, no cross-tenant detail.
# ---------------------------------------------------------------------------


def _safe_read_model(
    *,
    state: str,
    responsible_actor: str | None,
    blocking_cause: str | None,
    last_verified_at: Any,
) -> dict[str, Any]:
    """Build the secret-free read-model projection for one installation row.

    Identical shape whether returned by REST or MCP (AC2). `blocking_cause` is
    trusted to be sanitized at write time; no further transformation here.
    `last_verified_at` is serialized to ISO-8601 string or None.
    """
    lva: str | None = None
    if last_verified_at is not None:
        try:
            lva = last_verified_at.isoformat()
        except AttributeError:
            lva = str(last_verified_at)
    return {
        "state": state,
        "safe_next_action": _SAFE_NEXT_ACTIONS.get(state, "unknown state"),
        "responsible_actor": responsible_actor,
        "blocking_cause": blocking_cause,
        "last_verified_at": lva,
    }


# ---------------------------------------------------------------------------
# apply_installation -- idempotent initial install: NOT_INSTALLED -> DOMAIN_PENDING
# (AC5, Task 2).
# ---------------------------------------------------------------------------


def apply_installation(
    conn,
    *,
    environment: str,
    connector_name: str,
    responsible_actor: str | None,
    blocking_cause: str | None,
    actor: str,
    idempotency_key: str,
    host_context: dict[str, Any],
    trace_id: str | None,
) -> dict[str, Any]:
    """Idempotently create the installation row: NOT_INSTALLED -> DOMAIN_PENDING.

    If a row already exists for (environment, connector_name) the call returns the
    existing read-model. The initial apply NEVER jumps straight to READY (AC5).
    Routes the write through ``execute_operation`` (atomic audit + outbox).

    Returns the safe read-model projection -- no secret, no cross-tenant detail.
    """
    if not isinstance(environment, str) or not environment.strip():
        raise ConnectorInstallationValidationError("environment is required")
    if not isinstance(connector_name, str) or not connector_name.strip():
        raise ConnectorInstallationValidationError("connector_name is required")
    if not isinstance(actor, str) or not actor.strip():
        raise ConnectorInstallationValidationError("actor is required")

    environment = environment.strip()
    connector_name = connector_name.strip()

    # Check whether a row already exists (before the operation lock).
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, state, responsible_actor, blocking_cause, last_verified_at "
            "FROM app.connector_installations "
            "WHERE environment = %s AND connector_name = %s",
            (environment, connector_name),
        )
        existing_row = cur.fetchone()

    if existing_row is not None:
        # Row exists -- return its current read-model (idempotent: no new write).
        _id, state, resp_actor, cause, lva = existing_row
        return _safe_read_model(
            state=state,
            responsible_actor=resp_actor,
            blocking_cause=cause,
            last_verified_at=lva,
        )

    target_state = "DOMAIN_PENDING"  # Initial apply is always NOT_INSTALLED -> DOMAIN_PENDING.

    spec = OperationSpec(
        command_type="connector.install.applied",
        actor=actor,
        effective_org_id="platform",
        resource_path=(
            "platform:connector-installations",
            f"environment:{environment}",
            f"connector:{connector_name}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={"policy": "connector-installation-v1", "catalog": "connector-installation-v1",
                  "tool": "rest-v1"},
        # No random id in the hashed payload: the idempotency request_hash must be
        # a pure function of the business inputs, so two identical applies replay
        # cleanly instead of colliding on a fresh ULID (review H1). The row id is
        # generated at write time inside the mutation.
        request_payload={
            "environment": environment,
            "connector_name": connector_name,
            "target_state": target_state,
            "responsible_actor": responsible_actor,
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=f"connector-installation:{environment}:{connector_name}:apply",
        trace_id=trace_id,
    )

    def _mk_result(inst_id, state, actor_val, cause) -> MutationResult:
        from core.operations import _canonical_hash  # noqa: PLC0415

        result = {
            "installation_id": inst_id,
            "environment": environment,
            "connector_name": connector_name,
            "state": state,
            "responsible_actor": actor_val,
            "blocking_cause": cause,
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={
                "connector_name": connector_name,
                "environment": environment,
                "transition": state,
            },
        )

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        installation_id = f"cin_{ULID()}"  # generated at write time, never hashed
        row = None
        with operation_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.connector_installations "
                "(id, environment, connector_name, state, blocking_cause, "
                "responsible_actor, operation_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (environment, connector_name) DO NOTHING",
                (
                    installation_id,
                    environment,
                    connector_name,
                    target_state,
                    blocking_cause,
                    responsible_actor,
                    operation_id,
                ),
            )
            if cur.rowcount == 1:
                return _mk_result(installation_id, target_state, responsible_actor, blocking_cause)
            # A concurrent apply won the race -- reconcile to the persisted row
            # rather than fabricating a success (review H3).
            cur.execute(
                "SELECT id, state, responsible_actor, blocking_cause "
                "FROM app.connector_installations "
                "WHERE environment = %s AND connector_name = %s",
                (environment, connector_name),
            )
            row = cur.fetchone()
        if row is None:  # pragma: no cover -- row vanished between conflict and select
            raise ConnectorInstallationConflict("apply race lost and row not found")
        r_id, r_state, r_actor, r_cause = row
        return _mk_result(r_id, r_state, r_actor, r_cause)

    op_result = execute_operation(conn, spec, mutation=mutation)
    data = op_result.result or {}
    return _safe_read_model(
        state=data.get("state", target_state),
        responsible_actor=data.get("responsible_actor", responsible_actor),
        blocking_cause=data.get("blocking_cause", blocking_cause),
        last_verified_at=None,
    )


# ---------------------------------------------------------------------------
# get_installation_state -- nondisclosing read (AC2, Task 2).
# ---------------------------------------------------------------------------


def get_installation_state(
    conn,
    *,
    environment: str,
    connector_name: str,
) -> dict[str, Any] | None:
    """Return the safe read-model for one installation row, or None if absent.

    None means the connector has never had an installation record in this
    environment -- callers treat it as NOT_INSTALLED for display purposes.
    Does NOT raise for absence; existence is never disclosed to unauthorized
    callers (that is the REST layer's job).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state, responsible_actor, blocking_cause, last_verified_at "
            "FROM app.connector_installations "
            "WHERE environment = %s AND connector_name = %s",
            (environment, connector_name),
        )
        row = cur.fetchone()
    if row is None:
        return None
    state, responsible_actor, blocking_cause, last_verified_at = row
    return _safe_read_model(
        state=state,
        responsible_actor=responsible_actor,
        blocking_cause=blocking_cause,
        last_verified_at=last_verified_at,
    )


# ---------------------------------------------------------------------------
# transition_state -- audited state transition (AC1, Task 2).
# ---------------------------------------------------------------------------


def transition_state(
    conn,
    *,
    environment: str,
    connector_name: str,
    target_state: str,
    responsible_actor: str | None,
    blocking_cause: str | None,
    last_verified_at: Any,
    actor: str,
    idempotency_key: str,
    host_context: dict[str, Any],
    trace_id: str | None,
) -> dict[str, Any]:
    """Drive an installation state transition through the shared operation seam.

    Validates the transition is legal per ``_ALLOWED_TRANSITIONS`` before any SQL.
    Routes the write through ``execute_operation`` (atomic audit + outbox, AC1).
    Returns the safe read-model projection.
    """
    if target_state not in INSTALLATION_STATES:
        raise ConnectorInstallationValidationError(
            f"unknown target state: {target_state!r}"
        )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, state, responsible_actor, blocking_cause, last_verified_at "
            "FROM app.connector_installations "
            "WHERE environment = %s AND connector_name = %s "
            "FOR UPDATE",
            (environment, connector_name),
        )
        row = cur.fetchone()

    if row is None:
        raise ConnectorInstallationUnavailable(
            "connector installation row not found"
        )

    installation_id, current_state, _resp_actor, _cause, _lva = row

    if target_state not in _ALLOWED_TRANSITIONS.get(current_state, frozenset()):
        raise ConnectorInstallationConflict(
            f"transition {current_state!r} -> {target_state!r} is not allowed"
        )

    spec = OperationSpec(
        command_type="connector.state.changed",
        actor=actor,
        effective_org_id="platform",
        resource_path=(
            "platform:connector-installations",
            f"environment:{environment}",
            f"connector:{connector_name}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={"policy": "connector-installation-v1", "catalog": "connector-installation-v1",
                  "tool": "rest-v1"},
        request_payload={
            "installation_id": installation_id,
            "environment": environment,
            "connector_name": connector_name,
            "from_state": current_state,
            "target_state": target_state,
            "responsible_actor": responsible_actor,
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=(
            f"connector-installation:{installation_id}:{target_state}"
        ),
        trace_id=trace_id,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        lva_value = last_verified_at
        with operation_conn.cursor() as cur:
            cur.execute(
                "UPDATE app.connector_installations "
                "SET state = %s, blocking_cause = %s, responsible_actor = %s, "
                "    last_verified_at = %s, operation_id = %s, updated_at = NOW() "
                "WHERE id = %s AND state = %s",
                (
                    target_state,
                    blocking_cause,
                    responsible_actor,
                    lva_value,
                    operation_id,
                    installation_id,
                    current_state,
                ),
            )
            if cur.rowcount != 1:
                raise ConnectorInstallationConflict(
                    "connector installation transition race lost"
                )
        result = {
            "installation_id": installation_id,
            "environment": environment,
            "connector_name": connector_name,
            "from_state": current_state,
            "state": target_state,
            "responsible_actor": responsible_actor,
            "blocking_cause": blocking_cause,
        }
        from core.operations import _canonical_hash  # noqa: PLC0415
        return MutationResult(
            outcome="succeeded",
            before_hash=_canonical_hash({"state": current_state}),
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={
                "connector_name": connector_name,
                "environment": environment,
                "from_state": current_state,
                "transition": target_state,
            },
        )

    execute_operation(conn, spec, mutation=mutation)
    return _safe_read_model(
        state=target_state,
        responsible_actor=responsible_actor,
        blocking_cause=blocking_cause,
        last_verified_at=last_verified_at,
    )


# ---------------------------------------------------------------------------
# is_ready -- catalog / activation gate helper (AC3, AC4, Task 4).
# ---------------------------------------------------------------------------


def is_ready(conn, *, environment: str, connector_name: str) -> bool:
    """Return True iff the connector installation is in READY state.

    Used as the activation gate: tenant activation must be refused unless the
    connector is READY (AC4). Does NOT disclose the actual state to callers;
    the boolean answer is safe for any audience.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state FROM app.connector_installations "
            "WHERE environment = %s AND connector_name = %s",
            (environment, connector_name),
        )
        row = cur.fetchone()
    if row is None:
        return False
    return str(row[0]) == "READY"
