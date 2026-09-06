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

from core.audit import declare_action
from core.operations import (
    MutationResult,
    OperationIdempotencyConflict,
    OperationSpec,
    execute_operation,
    prepare_operation,
)

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12) : declarees ICI, a cote du code qui les ecrit, et non
# dans `core/audit.py`. Ce fichier etait un carrefour -- 43 editions de 29
# sujets depuis juin, dont 34 n'ajoutaient qu'une constante -- et 45 % des
# actions reellement ecrites en production n'y etaient meme pas declarees,
# parce que la liste etait trop loin pour valoir le detour. `write_audit_row`
# refuse desormais une action que personne n'a declaree.
ACTION_CONNECTOR_INSTALL_APPLIED = declare_action("connector.install.applied")
ACTION_CONNECTOR_STATE_CHANGED = declare_action("connector.state.changed")


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

# Public, source-agnostic safety vocabulary. Values stored in the installation
# read model are classifications, never identities or upstream error prose.
RESPONSIBLE_ACTOR_CLASSES: frozenset[str] = frozenset({
    "automated",
    "platform_admin",
    "platform_support",
})

BLOCKING_CAUSE_CODES: frozenset[str] = frozenset({
    "connector_disabled",
    "dependency_unavailable",
    "domain_configuration_pending",
    "domain_route_misconfigured",
    "installation_not_applied",
    "synthetic_verification_failed",
    "verification_failed",
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
    # Says what applying DOES, not which state it lands in: since AI-206 the next
    # state depends on whether the connector has a domain to configure, and a
    # message that promised DOMAIN_PENDING to a connector that skips it named a
    # step the operator would then look for and not find.
    "NOT_INSTALLED":  "platform_admin: apply installation to begin",
    "DOMAIN_PENDING": "platform_admin: configure domain routing to advance to VERIFYING",
    "VERIFYING":      "platform_admin: wait for automated verification to advance to READY",
    "READY":          "no action required -- connector is available for tenant activation",
    "DEGRADED":       "platform_admin: resolve cause, then advance to READY or VERIFYING",
    "DISABLED":       "platform_admin: contact platform support; installation is disabled",
}


class ConnectorInstallationValidationError(ValueError):
    """Raised before SQL when installation inputs are unsafe or malformed."""


class ConnectorInstallationConflict(RuntimeError):
    """The installation cannot transition from its current state."""


class ConnectorInstallationUnavailable(RuntimeError):
    """The installation row does not exist or is not in a usable state."""

def normalize_responsible_actor(
    value: str | None, *, default: str
) -> str:
    """Return one closed responsible-actor class, never a subject identity."""
    candidate = default if value is None else value
    if not isinstance(candidate, str):
        raise ConnectorInstallationValidationError(
            "responsible_actor must be an actor class"
        )
    actor_class = candidate.strip()
    if actor_class not in RESPONSIBLE_ACTOR_CLASSES:
        raise ConnectorInstallationValidationError(
            "responsible_actor must be one of the supported actor classes"
        )
    return actor_class


def validate_blocking_cause_code(
    value: str | None, *, default: str | None = None
) -> str | None:
    """Validate a safe cause code supplied by a trusted caller."""
    candidate = default if value is None else value
    if candidate is None:
        return None
    if not isinstance(candidate, str):
        raise ConnectorInstallationValidationError(
            "blocking_cause must be a safe cause code"
        )
    cause = candidate.strip()
    if cause not in BLOCKING_CAUSE_CODES:
        raise ConnectorInstallationValidationError(
            "blocking_cause must be one of the supported safe cause codes"
        )
    return cause


def sanitize_blocking_cause(
    value: Any, *, fallback: str = "dependency_unavailable"
) -> str:
    """Collapse arbitrary upstream prose to a bounded, secret-free cause code."""
    if isinstance(value, str) and value.strip() in BLOCKING_CAUSE_CODES:
        return value.strip()
    if fallback not in BLOCKING_CAUSE_CODES:
        raise ValueError("fallback must be a supported blocking cause code")
    return fallback


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
    """Build a defensive secret-free projection, including for legacy rows.

    The write path accepts only closed actor/cause classifications. The read path
    also collapses historical free text so an older row can never disclose it.
    """
    lva: str | None = None
    if last_verified_at is not None:
        try:
            lva = last_verified_at.isoformat()
        except AttributeError:
            lva = str(last_verified_at)

    safe_actor = (
        responsible_actor
        if responsible_actor in RESPONSIBLE_ACTOR_CLASSES
        else "platform_support"
    )
    safe_cause = (
        blocking_cause
        if blocking_cause in BLOCKING_CAUSE_CODES
        else "dependency_unavailable" if blocking_cause else None
    )
    return {
        "state": state,
        "safe_next_action": _SAFE_NEXT_ACTIONS.get(state, "unknown state"),
        "responsible_actor": safe_actor,
        "blocking_cause": safe_cause,
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
    requires_domain: bool = True,
) -> dict[str, Any]:
    """Idempotently create the installation row. The initial apply NEVER goes READY.

    If a row already exists for (environment, connector_name) the call returns the
    existing read-model. Routes the write through ``execute_operation`` (atomic
    audit + outbox).

    ``requires_domain`` (AI-206) IS THE SECOND STEP, AND IT IS NOT UNIVERSAL. This
    function used to send every connector to ``DOMAIN_PENDING`` with the next
    action « configure domain routing to advance to VERIFYING ». Measured
    2026-08-16 against the 39 module manifests: not one declares a domain, a
    receipt adapter or a transport. `app.connector_domain_configs` carries
    `domain`, `provider_adapter`, `webhook_endpoint_version` and `dns_evidence_*`
    -- inbound-email notions. So EVERY module connector was parked on a gesture
    that does not exist for it, forever, and the screen told the operator to
    perform it.

    The domain step belongs to the inbound transport family, whose one member is
    `managed_feed` -- and `managed_feed` is not a module (`ls server/modules`
    confirms it). The caller decides, because the caller is what knows the loaded
    modules; this function stays free of module vocabulary (AD-2). It DEFAULTS to
    True so an existing caller that has not been taught the distinction keeps the
    stricter path rather than silently skipping a step it may owe.

    Returns the safe read-model projection -- no secret, no cross-tenant detail.
    """
    if not isinstance(environment, str) or not environment.strip():
        raise ConnectorInstallationValidationError("environment is required")
    if not isinstance(connector_name, str) or not connector_name.strip():
        raise ConnectorInstallationValidationError("connector_name is required")
    if not isinstance(actor, str) or not actor.strip():
        raise ConnectorInstallationValidationError("actor is required")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ConnectorInstallationValidationError("idempotency_key is required")
    if len(idempotency_key.strip()) > 255:
        raise ConnectorInstallationValidationError("idempotency_key is too long")

    environment = environment.strip()
    connector_name = connector_name.strip()
    actor = actor.strip()
    idempotency_key = idempotency_key.strip()
    responsible_actor = normalize_responsible_actor(
        responsible_actor, default="platform_admin"
    )
    # A connector with no domain to configure cannot be blocked on configuring
    # one. Naming `domain_configuration_pending` there would put an impossible
    # gesture in front of the operator, which is the defect AI-206 measured.
    #
    # And NO cause replaces it, rather than a new code: a connector in VERIFYING
    # is not blocked, it is in progress. A blocking cause names something that
    # must be RESOLVED, and waiting for an automated check is not that --
    # `_SAFE_NEXT_ACTIONS["VERIFYING"]` already says « wait for automated
    # verification ». Inventing `verification_pending` would have made a normal
    # step read as an obstacle on every screen that lists blocked connectors.
    blocking_cause = validate_blocking_cause_code(
        blocking_cause,
        default="domain_configuration_pending" if requires_domain else None,
    )

    # Always enter the shared operation seam. Its idempotency lookup must see an
    # existing key before the row-level uniqueness reconciliation runs.
    target_state = "DOMAIN_PENDING" if requires_domain else "VERIFYING"

    spec = OperationSpec(
        command_type=ACTION_CONNECTOR_INSTALL_APPLIED,
        actor=actor,
        effective_org_id=None,
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
            "blocking_cause": blocking_cause,
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=f"connector-installation:{environment}:{connector_name}:apply",
        trace_id=trace_id,
    )

    def _mk_result(
        inst_id, state, actor_val, cause, last_verified_at
    ) -> MutationResult:
        from core.operations import _canonical_hash  # noqa: PLC0415

        serialized_lva = (
            last_verified_at.isoformat()
            if hasattr(last_verified_at, "isoformat")
            else str(last_verified_at) if last_verified_at else None
        )
        result = {
            "installation_id": inst_id,
            "environment": environment,
            "connector_name": connector_name,
            "state": state,
            "responsible_actor": actor_val,
            "blocking_cause": cause,
            "last_verified_at": serialized_lva,
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
                return _mk_result(
                    installation_id,
                    target_state,
                    responsible_actor,
                    blocking_cause,
                    None,
                )
            # A concurrent apply won the race -- reconcile to the persisted row
            # rather than fabricating a success (review H3).
            cur.execute(
                "SELECT id, state, responsible_actor, blocking_cause, last_verified_at "
                "FROM app.connector_installations "
                "WHERE environment = %s AND connector_name = %s",
                (environment, connector_name),
            )
            row = cur.fetchone()
        if row is None:  # pragma: no cover -- row vanished between conflict and select
            raise ConnectorInstallationConflict("apply race lost and row not found")
        r_id, r_state, r_actor, r_cause, r_lva = row
        return _mk_result(r_id, r_state, r_actor, r_cause, r_lva)

    op_result = execute_operation(conn, spec, mutation=mutation)
    data = op_result.result or {}
    return _safe_read_model(
        state=data.get("state", target_state),
        responsible_actor=data.get("responsible_actor", responsible_actor),
        blocking_cause=data.get("blocking_cause", blocking_cause),
        last_verified_at=data.get("last_verified_at"),
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


def _replayed_transition_result(conn, spec: OperationSpec) -> dict[str, Any] | None:
    """Return an existing operation result without inserting an illegal no-op."""
    prepared = prepare_operation(spec)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT result, request_hash FROM app.operations "
            "WHERE effective_org_id IS NOT DISTINCT FROM %s "
            "AND command_type = %s AND idempotency_key_hash = %s",
            (
                spec.effective_org_id,
                spec.command_type,
                prepared.idempotency_key_hash,
            ),
        )
        row = cur.fetchone()
    if row is None:
        return None
    if row[1] != prepared.request_hash:
        raise OperationIdempotencyConflict(
            "idempotency key is already bound to a different request"
        )
    return row[0] or {}


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
    """Drive one audited transition, with replay checked before state legality.

    The current row is locked for the whole operation. A retry whose transition
    already committed can therefore return its original operation result without
    creating a pending no-op operation.
    """
    if target_state not in INSTALLATION_STATES:
        raise ConnectorInstallationValidationError(
            f"unknown target state: {target_state!r}"
        )
    for value, name in (
        (environment, "environment"),
        (connector_name, "connector_name"),
        (actor, "actor"),
        (idempotency_key, "idempotency_key"),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ConnectorInstallationValidationError(f"{name} is required")
    if len(idempotency_key.strip()) > 255:
        raise ConnectorInstallationValidationError("idempotency_key is too long")

    environment = environment.strip()
    connector_name = connector_name.strip()
    actor = actor.strip()
    idempotency_key = idempotency_key.strip()

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

    installation_id, current_state, current_actor, _current_cause, current_lva = row
    responsible_actor = normalize_responsible_actor(
        responsible_actor,
        default=(
            current_actor
            if current_actor in RESPONSIBLE_ACTOR_CLASSES
            else "platform_support"
        ),
    )

    default_cause = {
        "DEGRADED": "verification_failed",
        "DISABLED": "connector_disabled",
    }.get(target_state)
    blocking_cause = validate_blocking_cause_code(
        blocking_cause, default=default_cause
    )


    next_lva = last_verified_at if target_state == "READY" else current_lva
    serialized_lva = (
        next_lva.isoformat()
        if hasattr(next_lva, "isoformat")
        else str(next_lva) if next_lva else None
    )

    spec = OperationSpec(
        command_type=ACTION_CONNECTOR_STATE_CHANGED,
        actor=actor,
        effective_org_id=None,
        resource_path=(
            "platform:connector-installations",
            f"environment:{environment}",
            f"connector:{connector_name}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={
            "policy": "connector-installation-v1",
            "catalog": "connector-installation-v1",
            "tool": "rest-v1",
        },
        request_payload={
            "installation_id": installation_id,
            "environment": environment,
            "connector_name": connector_name,
            "last_verified_at": serialized_lva,
            "target_state": target_state,
            "responsible_actor": responsible_actor,
            "blocking_cause": blocking_cause,
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=(
            f"connector-installation:{installation_id}:{target_state}"
        ),
        trace_id=trace_id,
    )

    if target_state not in _ALLOWED_TRANSITIONS.get(current_state, frozenset()):
        replayed = _replayed_transition_result(conn, spec)
        if replayed is not None:
            return _safe_read_model(
                state=replayed.get("state", target_state),
                responsible_actor=replayed.get(
                    "responsible_actor", responsible_actor
                ),
                blocking_cause=replayed.get("blocking_cause", blocking_cause),
                last_verified_at=replayed.get(
                    "last_verified_at", serialized_lva
                ),
            )
        raise ConnectorInstallationConflict(
            f"transition {current_state!r} -> {target_state!r} is not allowed"
        )
    if target_state == "READY":
        if blocking_cause is not None:
            raise ConnectorInstallationValidationError(
                "READY cannot carry a blocking cause"
            )
        if last_verified_at is None or not hasattr(last_verified_at, "isoformat"):
            raise ConnectorInstallationValidationError(
                "READY requires verified timestamp evidence"
            )
    elif target_state == "DEGRADED" and blocking_cause is None:
        raise ConnectorInstallationValidationError(
            "DEGRADED requires a blocking cause"
        )

    def mutation(operation_conn, operation_id: str) -> MutationResult:
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
                    next_lva,
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
            "last_verified_at": serialized_lva,
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

    op_result = execute_operation(conn, spec, mutation=mutation)
    data = op_result.result or {}
    return _safe_read_model(
        state=data.get("state", target_state),
        responsible_actor=data.get("responsible_actor", responsible_actor),
        blocking_cause=data.get("blocking_cause", blocking_cause),
        last_verified_at=data.get("last_verified_at", serialized_lva),
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
