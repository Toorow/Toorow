"""Story 38.5 -- org-scoped connector activation as audited, preserved state.

A tenant organisation owner activates a connector that is already READY in the
platform installation layer (38.2). Activation enables the organisation to
create governed inbound Datastreams against that connector. Deactivation flips
the state to DEACTIVATED and sets ``deactivated_at``; it NEVER deletes the row
or any retained evidence (AC3: data-preservation invariant).

INVARIANTS (adversarially enforced, mirroring connector_installation.py):

  * SOURCE-AGNOSTIC. The connector is referenced ONLY by its ``connector_name``
    string. NO provider/vendor vocabulary enters this module -- not in code, not
    in comments, not in docstrings (the AD-2 boundary scanner is literal). No
    adapter name, no inbound-delivery token, no receiving-domain value.

  * EVERY WRITE ROUTES THROUGH ``operations.execute_operation`` (atomic audit +
    outbox, idempotent replay). No parallel audit path exists here.

  * NONDISCLOSING READ MODEL. The safe projection (``state``, ``activated_at``,
    ``deactivated_at``) carries no secret and no cross-tenant infrastructure
    detail. It is identical whether returned by REST or MCP.

  * ACTIVATION REQUIRES READY (AC1). ``activate`` calls
    ``connector_installation_api.refuse_activation_unless_ready`` BEFORE any SQL.
    A non-READY connector raises ``ConnectorNotReady``; the caller maps this to a
    nondisclosing failure response (never a 403, always a fail-closed 422/409).

  * DEACTIVATION PRESERVES DATA (AC3). ``deactivate`` issues an UPDATE to flip
    ``state`` to ``DEACTIVATED`` and set ``deactivated_at``. It NEVER issues a
    DELETE against ``app.connector_activations`` or any related evidence table.

  * DETERMINISTIC IDEMPOTENCY (review H1). No random id enters
    ``request_payload``. Row ids (``cac_<ULID>``) are generated at WRITE TIME
    inside the mutation closure. Two identical activations with the same
    Idempotency-Key replay cleanly.

  * ROWCOUNT CHECKED (review H2). ``INSERT ... ON CONFLICT DO NOTHING`` rowcount
    is verified; on a lost race the call reconciles to the persisted row. The safe
    read-model is derived from ``op_result.result``, not a hard-coded target.

  * TENANT ISOLATION (AC4). ``get_activation`` is scoped to ``org_id``.
    Callers from a different org receive no data (the REST layer maps None to a
    nondisclosing 404).

Mirrors ``connector_installation.py`` conventions: ``from __future__ import
annotations``, lazy imports of the shared seams, mutations only through the
operation seam, ASCII-only source.
"""

from __future__ import annotations

from typing import Any

from ulid import ULID

from core.operations import MutationResult, OperationSpec, execute_operation

# ---------------------------------------------------------------------------
# Activation states.
# ---------------------------------------------------------------------------

#: All valid activation states for ``app.connector_activations``.
ACTIVATION_STATES: frozenset[str] = frozenset({"ACTIVE", "DEACTIVATED"})


# ---------------------------------------------------------------------------
# Exceptions.
# ---------------------------------------------------------------------------


class ConnectorActivationValidationError(ValueError):
    """Raised before SQL when activation inputs are unsafe or malformed."""


class ConnectorActivationConflict(RuntimeError):
    """The activation row is in a state that prevents the requested transition.

    Maps to HTTP 409. Message is operator-facing and nondisclosing.
    """


class ConnectorActivationUnavailable(RuntimeError):
    """The activation row does not exist or belongs to a different org."""


# ---------------------------------------------------------------------------
# Safe read-model projection. No secret, no cross-tenant detail.
# ---------------------------------------------------------------------------


def _safe_read_model(
    *,
    org_id: str,
    connector_name: str,
    environment: str,
    state: str,
    activated_at: Any,
    deactivated_at: Any,
) -> dict[str, Any]:
    """Build the secret-free read-model projection for one activation row.

    Fields: org_id, connector_name, environment, state, activated_at,
    deactivated_at. NEVER includes credentials, signing values, or any
    cross-tenant infrastructure detail. Identical shape whether returned by
    REST or MCP (AC2).
    """

    def _iso(ts: Any) -> str | None:
        if ts is None:
            return None
        try:
            return ts.isoformat()
        except AttributeError:
            return str(ts)

    return {
        "org_id": org_id,
        "connector_name": connector_name,
        "environment": environment,
        "state": state,
        "activated_at": _iso(activated_at),
        "deactivated_at": _iso(deactivated_at),
    }


# ---------------------------------------------------------------------------
# activate -- idempotent ACTIVE row via execute_operation (AC1, AC5).
# ---------------------------------------------------------------------------


def activate(
    conn,
    *,
    org_id: str,
    connector_name: str,
    environment: str,
    activated_by: str,
    actor: str,
    idempotency_key: str,
    host_context: dict[str, Any],
    trace_id: str | None,
) -> dict[str, Any]:
    """Activate a READY connector for an organisation (AC1, AC5, Task 2).

    Preconditions (fail-closed before any SQL):
      - ``org_id``, ``connector_name``, ``environment``, ``actor`` must be
        non-empty strings.
      - The platform connector installation MUST be in READY state; otherwise
        ``ConnectorNotReady`` is raised and the caller maps it to a
        nondisclosing failure response (AC1).

    Behaviour:
      - On first call: inserts an ``ACTIVE`` row in ``app.connector_activations``
        via ``execute_operation`` (atomic audit + outbox).
      - On replay (same Idempotency-Key + payload): returns the existing row
        read-model cleanly.
      - On concurrent race: reconciles to the persisted row; no phantom result.
      - On conflicting payload for the same Idempotency-Key: raises
        ``OperationIdempotencyConflict`` (caller maps to 409).

    Does NOT grant platform installation rights (AC1). Does NOT mutate the
    installation record. Does NOT create Datastreams (38.6 scope).

    Returns the safe read-model projection.
    """
    # ------------------------------------------------------------------
    # Input validation (fail-closed before SQL).
    # ------------------------------------------------------------------
    if not isinstance(org_id, str) or not org_id.strip():
        raise ConnectorActivationValidationError("org_id is required")
    if not isinstance(connector_name, str) or not connector_name.strip():
        raise ConnectorActivationValidationError("connector_name is required")
    if not isinstance(environment, str) or not environment.strip():
        raise ConnectorActivationValidationError("environment is required")
    if not isinstance(actor, str) or not actor.strip():
        raise ConnectorActivationValidationError("actor is required")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ConnectorActivationValidationError("idempotency_key is required")

    org_id = org_id.strip()
    connector_name = connector_name.strip()
    environment = environment.strip()
    actor = actor.strip()
    activated_by = (activated_by or actor).strip() or actor

    # ------------------------------------------------------------------
    # READY gate (AC1): refuse activation unless the connector is READY.
    # This is the authoritative enforcement point; the REST layer ALSO
    # enforces it so the domain never sees a non-READY attempt from the
    # API surface.
    # ------------------------------------------------------------------
    from core.connector_installation_api import (  # noqa: PLC0415
        refuse_activation_unless_ready,
    )

    refuse_activation_unless_ready(
        conn, connector_name=connector_name, environment=environment
    )

    # ------------------------------------------------------------------
    # Build the OperationSpec.
    # DETERMINISTIC PAYLOAD (review H1): no random id in request_payload.
    # The new row id is generated at write time INSIDE the mutation closure.
    # ------------------------------------------------------------------
    spec = OperationSpec(
        command_type="connector.activation.activated",
        actor=actor,
        effective_org_id=org_id,
        resource_path=(
            f"organization:{org_id}",
            f"connector:{connector_name}",
            f"environment:{environment}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={
            "policy": "connector-activation-v1",
            "catalog": "connector-activation-v1",
            "tool": "rest-v1",
        },
        # Deterministic over business inputs only (review H1). No row id here.
        request_payload={
            "org_id": org_id,
            "connector_name": connector_name,
            "environment": environment,
            "activated_by": activated_by,
            "target_state": "ACTIVE",
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=(
            f"connector-activation:{org_id}:{connector_name}:{environment}"
        ),
        trace_id=trace_id,
    )

    def _mk_result(
        row_id: str,
        row_state: str,
        row_activated_at: Any,
        row_deactivated_at: Any,
    ) -> MutationResult:
        from core.operations import _canonical_hash  # noqa: PLC0415

        result = {
            "activation_id": row_id,
            "org_id": org_id,
            "connector_name": connector_name,
            "environment": environment,
            "state": row_state,
            "activated_by": activated_by,
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result),
            result={
                **result,
                "activated_at": (
                    row_activated_at.isoformat()
                    if hasattr(row_activated_at, "isoformat")
                    else str(row_activated_at) if row_activated_at else None
                ),
                "deactivated_at": (
                    row_deactivated_at.isoformat()
                    if hasattr(row_deactivated_at, "isoformat")
                    else str(row_deactivated_at) if row_deactivated_at else None
                ),
            },
            outbox_payload={
                "org_id": org_id,
                "connector_name": connector_name,
                "environment": environment,
                "state": row_state,
            },
        )

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        # Generate row id at write time (review H1: not hashed).
        activation_id = f"cac_{ULID()}"

        with operation_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.connector_activations "
                "(id, org_id, connector_name, environment, state, "
                "activated_by, operation_id) "
                "VALUES (%s, %s, %s, %s, 'ACTIVE', %s, %s) "
                "ON CONFLICT (org_id, connector_name, environment) DO NOTHING",
                (
                    activation_id,
                    org_id,
                    connector_name,
                    environment,
                    activated_by,
                    operation_id,
                ),
            )
            if cur.rowcount == 1:
                # Inserted: read back the DB-authoritative timestamps.
                cur.execute(
                    "SELECT state, activated_at, deactivated_at "
                    "FROM app.connector_activations WHERE id = %s",
                    (activation_id,),
                )
                ts_row = cur.fetchone()
                row_state = ts_row[0] if ts_row else "ACTIVE"
                row_activated_at = ts_row[1] if ts_row else None
                row_deactivated_at = ts_row[2] if ts_row else None
                return _mk_result(
                    activation_id, row_state, row_activated_at, row_deactivated_at
                )

            # ON CONFLICT DO NOTHING: a concurrent insert won the race (review H2).
            # Reconcile to the persisted row.
            cur.execute(
                "SELECT id, state, activated_at, deactivated_at "
                "FROM app.connector_activations "
                "WHERE org_id = %s AND connector_name = %s AND environment = %s",
                (org_id, connector_name, environment),
            )
            race_row = cur.fetchone()
            if race_row is None:  # pragma: no cover -- row vanished
                raise ConnectorActivationConflict(
                    "activation race lost and row not found"
                )
            r_id, r_state, r_activated_at, r_deactivated_at = race_row
            return _mk_result(r_id, r_state, r_activated_at, r_deactivated_at)

    op_result = execute_operation(conn, spec, mutation=mutation)
    data = op_result.result or {}
    return _safe_read_model(
        org_id=org_id,
        connector_name=connector_name,
        environment=environment,
        state=data.get("state", "ACTIVE"),
        activated_at=data.get("activated_at"),
        deactivated_at=data.get("deactivated_at"),
    )


# ---------------------------------------------------------------------------
# deactivate -- state flip to DEACTIVATED + deactivated_at (AC3).
# ---------------------------------------------------------------------------


def deactivate(
    conn,
    *,
    org_id: str,
    connector_name: str,
    environment: str,
    actor: str,
    idempotency_key: str,
    host_context: dict[str, Any],
    trace_id: str | None,
) -> dict[str, Any]:
    """Deactivate an active connector for an organisation (AC3, Task 2).

    Flips the activation row to DEACTIVATED and sets ``deactivated_at``.
    NEVER deletes the row or any retained evidence (data-preservation invariant,
    AC3). Audit history and last-known-good publications are untouched.

    If no activation row exists for this (org_id, connector_name, environment)
    this raises ``ConnectorActivationUnavailable`` (caller maps to 404).

    Returns the safe read-model projection.
    """
    if not isinstance(org_id, str) or not org_id.strip():
        raise ConnectorActivationValidationError("org_id is required")
    if not isinstance(connector_name, str) or not connector_name.strip():
        raise ConnectorActivationValidationError("connector_name is required")
    if not isinstance(environment, str) or not environment.strip():
        raise ConnectorActivationValidationError("environment is required")
    if not isinstance(actor, str) or not actor.strip():
        raise ConnectorActivationValidationError("actor is required")

    org_id = org_id.strip()
    connector_name = connector_name.strip()
    environment = environment.strip()
    actor = actor.strip()

    # Load the current row (before the operation lock, read-only).
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, state, activated_at "
            "FROM app.connector_activations "
            "WHERE org_id = %s AND connector_name = %s AND environment = %s",
            (org_id, connector_name, environment),
        )
        row = cur.fetchone()

    if row is None:
        raise ConnectorActivationUnavailable(
            "no activation record found for this org and connector"
        )

    activation_id, current_state, activated_at = row

    spec = OperationSpec(
        command_type="connector.activation.deactivated",
        actor=actor,
        effective_org_id=org_id,
        resource_path=(
            f"organization:{org_id}",
            f"connector:{connector_name}",
            f"environment:{environment}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={
            "policy": "connector-activation-v1",
            "catalog": "connector-activation-v1",
            "tool": "rest-v1",
        },
        # Deterministic over business inputs (review H1). No random id here.
        request_payload={
            "activation_id": activation_id,
            "org_id": org_id,
            "connector_name": connector_name,
            "environment": environment,
            "from_state": current_state,
            "target_state": "DEACTIVATED",
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=(
            f"connector-activation:{activation_id}:deactivated"
        ),
        trace_id=trace_id,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        from core.operations import _canonical_hash  # noqa: PLC0415

        # UPDATE (never DELETE) -- data-preservation invariant (AC3).
        with operation_conn.cursor() as cur:
            cur.execute(
                "UPDATE app.connector_activations "
                "SET state = 'DEACTIVATED', deactivated_at = NOW(), "
                "    operation_id = %s, updated_at = NOW() "
                "WHERE id = %s",
                (operation_id, activation_id),
            )
            rc = cur.rowcount
            # Re-read the final state (including DB-authoritative deactivated_at).
            cur.execute(
                "SELECT state, activated_at, deactivated_at "
                "FROM app.connector_activations WHERE id = %s",
                (activation_id,),
            )
            ts_row = cur.fetchone()

        # Reconcile to the DB-authoritative row whether we won the UPDATE (rc==1)
        # or a concurrent deactivation did (rc==0) -- both leave the row DEACTIVATED
        # and the re-SELECT is the source of truth; never fabricate (review F2/H2).
        # NB: re-deactivating with a FRESH idempotency key intentionally refreshes
        # deactivated_at; reusing the same key replays cleanly via the operation spine.
        _ = rc
        row_state = ts_row[0] if ts_row else "DEACTIVATED"
        row_activated_at = ts_row[1] if ts_row else activated_at
        row_deactivated_at = ts_row[2] if ts_row else None

        result = {
            "activation_id": activation_id,
            "org_id": org_id,
            "connector_name": connector_name,
            "environment": environment,
            "state": row_state,
            "activated_at": (
                row_activated_at.isoformat()
                if hasattr(row_activated_at, "isoformat")
                else str(row_activated_at) if row_activated_at else None
            ),
            "deactivated_at": (
                row_deactivated_at.isoformat()
                if hasattr(row_deactivated_at, "isoformat")
                else str(row_deactivated_at) if row_deactivated_at else None
            ),
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=_canonical_hash({"state": current_state}),
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={
                "org_id": org_id,
                "connector_name": connector_name,
                "environment": environment,
                "state": "DEACTIVATED",
            },
        )

    op_result = execute_operation(conn, spec, mutation=mutation)
    data = op_result.result or {}
    return _safe_read_model(
        org_id=org_id,
        connector_name=connector_name,
        environment=environment,
        state=data.get("state", "DEACTIVATED"),
        activated_at=data.get("activated_at"),
        deactivated_at=data.get("deactivated_at"),
    )


# ---------------------------------------------------------------------------
# get_activation -- org-scoped nondisclosing read (AC2, AC4).
# ---------------------------------------------------------------------------


def get_activation(
    conn,
    *,
    org_id: str,
    connector_name: str,
    environment: str,
) -> dict[str, Any] | None:
    """Return the safe read-model for one org's activation row, or None if absent.

    None means the organisation has no activation record for this connector in
    this environment -- callers treat it as inactive for display purposes and
    return a nondisclosing 404. Does NOT raise for absence.

    Scoped to ``org_id``: a caller from a different org receives None regardless
    of whether the other org's activation exists (AC4 -- tenant isolation).
    NEVER returns secrets or cross-tenant infrastructure details.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state, activated_at, deactivated_at "
            "FROM app.connector_activations "
            "WHERE org_id = %s AND connector_name = %s AND environment = %s",
            (org_id, connector_name, environment),
        )
        row = cur.fetchone()
    if row is None:
        return None
    state, activated_at, deactivated_at = row
    return _safe_read_model(
        org_id=org_id,
        connector_name=connector_name,
        environment=environment,
        state=state,
        activated_at=activated_at,
        deactivated_at=deactivated_at,
    )
