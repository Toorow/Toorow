"""Durable BigQuery marts access sagas -- organization scope, and Project scope.

ONE SAGA, TWO SCOPES (story 62.3). The organization saga below was already
durable: request, provider mutation, finalization, audit, idempotent replay and
a bounded attempt lease. The Project scope is the SAME saga with a different
resolved target and a different authorization seam -- not a second saga. Writing
a second one would have given the product two ways to be halfway through a
grant, and only one of them would have been repaired the day it broke.

WHAT THE PROJECT SCOPE ADDS:

  * the target is ``marts_<project_id>``, resolved by ``warehouse_tenancy``, and
    the API REFUSES when this deployment's topology cannot represent it (see
    ``warehouse_tenancy.project_marts_scope``);
  * the service account may be MINTED BY THE PRODUCT for this grant and retired
    with it -- through ``core.service_account_provider``, a seam mocked exactly
    like the BigQuery one;
  * a scratch target (``sandbox_``, ``tmp_``, a date suffix) is refused with the
    gesture that repairs it, because a dashboard bound to a name that will be
    thrown away breaks the week it is rebuilt.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.audit import ACTION_DATASET_ACCESS_REVOKED, declare_action, insert_audit_row

logger = logging.getLogger("core.admin_api")

ACTION_DATASET_ACCESS_GRANTED = declare_action("dataset_access.granted")
ACTION_DATASET_ACCESS_GRANT_FINALIZED = declare_action("dataset_access.grant_finalized")
ACTION_DATASET_ACCESS_REVOKE_FINALIZED = declare_action("dataset_access.revoke_finalized")
_IAM_ROLE = "roles/bigquery.dataViewer"
_ATTEMPT_LEASE_SECONDS = 60
_IAM_PRINCIPAL_RE = re.compile(
    r"^(user|serviceAccount|group):[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$"
)


class DatasetAccessConflict(RuntimeError):
    """The requested principal already has an incompatible active saga."""


@dataclass(frozen=True)
class ProviderVerdict:
    outcome: Literal["succeeded", "failed", "unknown"]
    error: str | None = None


async def _check_auth(*args, **kwargs):
    from core.admin_api import _check_auth as impl  # noqa: PLC0415

    return await impl(*args, **kwargs)


def _enforce_org_manage(*args, **kwargs):
    from core.admin_api import _enforce_org_manage as impl  # noqa: PLC0415

    return impl(*args, **kwargs)


def _provider_error(exc: BaseException) -> str:
    """Return bounded operational detail with compound secret keys redacted."""
    message = " ".join(str(exc).split()) or "provider rejected the request"
    message = re.sub(r"(?i)\bbearer\s+\S+", "Bearer [redacted]", message)
    message = re.sub(
        r"(?i)\b(access[_-]?token|refresh[_-]?token|client[_-]?secret|api[_-]?key|"
        r"token|password|secret|credential|authorization)\b"
        r"\s*[:=]\s*[\"']?[^,\s\"']+",
        r"\1=[redacted]",
        message,
    )
    return f"{type(exc).__name__}: {message}"[:1000]


def _ambiguous_provider_error(exc: BaseException) -> bool:
    names = {item.__name__ for item in type(exc).mro()}
    return bool(
        names
        & {
            "TimeoutError",
            "ConnectionError",
            "DeadlineExceeded",
            "ServiceUnavailable",
            "GatewayTimeout",
            "InternalServerError",
            "RetryError",
        }
    )


_RETURNING = """
    id, org_id, principal, granted_by, lifecycle_state, dataset_id, role,
    created_at, effective_at, revoked_at, last_provider_error,
    provider_error_at, revocation_state, grant_operation_id,
    revoke_operation_id, provider_attempt_started_at, updated_at,
    project_id, managed_service_account, service_account_deleted_at
"""


def _grant_payload(row) -> dict[str, Any]:
    fields = (
        "id",
        "org_id",
        "principal",
        "granted_by",
        "lifecycle_state",
        "dataset_id",
        "role",
        "created_at",
        "effective_at",
        "revoked_at",
        "last_provider_error",
        "provider_error_at",
        "revocation_state",
        "grant_operation_id",
        "revoke_operation_id",
        "provider_attempt_started_at",
        "updated_at",
        "project_id",
        "managed_service_account",
        "service_account_deleted_at",
    )
    payload = dict(zip(fields, row, strict=True))
    for key in (
        "created_at",
        "effective_at",
        "revoked_at",
        "provider_error_at",
        "provider_attempt_started_at",
        "updated_at",
        "service_account_deleted_at",
    ):
        value = payload[key]
        payload[key] = value.isoformat() if value else None
    payload["managed_service_account"] = bool(payload["managed_service_account"])
    return payload


def _hash_payload(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _operation_spec(
    *,
    action: str,
    actor: str,
    org_id: str,
    key: str,
    payload: dict,
    project_id: str | None = None,
):
    from core.operations import OperationSpec  # noqa: PLC0415

    scope = ("projects", project_id, "dataset-access") if project_id else (
        "organizations", org_id, "dataset-access"
    )
    return OperationSpec(
        command_type=action,
        actor=actor,
        effective_org_id=org_id,
        resource_path=scope,
        idempotency_key=key,
        host_context={"host": "rest", "workspace_id": "admin-console"},
        versions={"policy": "v1", "catalog": "v1", "tool": "rest-v1"},
        request_payload=payload,
        provider_references={},
        confirmation_mode="human",
        confirmation_reference=(
            f"dataset-access:{project_id or org_id}:"
            f"{payload.get('principal') or payload.get('grant_id')}"
        ),
        trace_id=None,
    )


def _reconcile_operation(conn, operation_id: str, outcome: str, payload: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.operations
            SET state = %s, outcome = %s, result = %s::jsonb,
                after_hash = %s, completed_at = CASE
                    WHEN %s = 'outcome_unknown' THEN completed_at ELSE NOW() END,
                updated_at = NOW()
            WHERE id = %s
            """,
            (
                outcome,
                outcome,
                json.dumps(payload),
                _hash_payload(payload),
                outcome,
                operation_id,
            ),
        )


def _terminal_response(payload: dict[str, Any], *, created: bool = False) -> JSONResponse:
    state = payload["lifecycle_state"]
    if state == "effective" and payload["revocation_state"] is None:
        return JSONResponse(payload, 201 if created else 200)
    if state == "revoked":
        return JSONResponse({**payload, "revoked": True}, 200)
    if state == "failed" or payload["revocation_state"] == "failed":
        return JSONResponse(
            {
                **payload,
                "code": "provider_unavailable",
                "message": payload["last_provider_error"],
            },
            503 if "not enabled" in (payload["last_provider_error"] or "") else 502,
        )
    return JSONResponse(
        {
            **payload,
            "code": "provider_outcome_unknown",
            "message": payload["last_provider_error"]
            or "Provider mutation is pending. Retry with the same Idempotency-Key.",
        },
        202,
    )


def _claim_attempt(grant_id: str, operation_id: str, operation_column: str):
    if operation_column not in {"grant_operation_id", "revoke_operation_id"}:
        raise ValueError("invalid dataset-access operation column")
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_RETURNING}
                FROM app.dataset_access_grants
                WHERE id = %s AND {operation_column} = %s
                FOR UPDATE
                """,
                (grant_id, operation_id),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("dataset-access saga row disappeared")
            payload = _grant_payload(row)
            if payload["provider_attempt_started_at"] is not None:
                cur.execute(
                    """
                    SELECT provider_attempt_started_at >
                           NOW() - make_interval(secs => %s)
                    FROM app.dataset_access_grants WHERE id = %s
                    """,
                    (_ATTEMPT_LEASE_SECONDS, grant_id),
                )
                if cur.fetchone()[0]:
                    return False, payload, None
            cur.execute(
                """
                UPDATE app.dataset_access_grants
                SET provider_attempt_started_at = NOW(), updated_at = NOW()
                WHERE id = %s
                RETURNING provider_attempt_started_at, updated_at
                """,
                (grant_id,),
            )
            attempt_at, updated_at = cur.fetchone()
            payload["provider_attempt_started_at"] = attempt_at.isoformat()
            payload["updated_at"] = updated_at.isoformat()
        conn.commit()
    return True, payload, attempt_at


def _provider_verdict(schemas, principal: str, action: str) -> ProviderVerdict:
    import core.warehouse_tenancy as wt  # noqa: PLC0415

    try:
        wt.mutate_bigquery_dataset_access(schemas, principal, action)
        return ProviderVerdict("succeeded")
    except Exception as exc:
        error = _provider_error(exc)
        if not _ambiguous_provider_error(exc):
            return ProviderVerdict("failed", error)
        try:
            observed = wt.read_bigquery_dataset_access(schemas, principal)
        except Exception as read_exc:
            return ProviderVerdict(
                "unknown",
                "Provider outcome is unknown and ACL verification failed; retry with the "
                f"same Idempotency-Key. Mutation: {error}. Verification: "
                f"{_provider_error(read_exc)}",
            )
        expected_present = action == "grant"
        if observed["present"] is expected_present:
            return ProviderVerdict("succeeded")
        return ProviderVerdict("failed", error)


def _resolve_provider_schemas(org_id: str):
    import core.warehouse_tenancy as wt  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    if not wt.org_schemas_enabled() or not wt.bq_provisioning_enabled():
        return None, (
            "BigQuery dataset access is not enabled. Enable organization schemas and "
            "BigQuery provisioning, configure GOOGLE_CLOUD_PROJECT, then retry."
        )
    try:
        wt.configured_bigquery_project()
        with get_connection() as conn:
            schemas = wt.resolve_org_schemas(org_id=org_id, conn=conn, fresh=True)
        if schemas is None:
            raise RuntimeError("organization marts dataset could not be resolved")
        return schemas, None
    except Exception as exc:
        return None, _provider_error(exc)


def _finalize_grant(
    grant_id: str,
    operation_id: str,
    actor: str,
    dataset_id: str | None,
    verdict: ProviderVerdict,
    attempt_token,
) -> dict[str, Any]:
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        with conn.cursor() as cur:
            if verdict.outcome == "succeeded":
                cur.execute(
                    f"""
                    UPDATE app.dataset_access_grants
                    SET lifecycle_state = 'effective', dataset_id = %s,
                        effective_at = COALESCE(effective_at, NOW()),
                        last_provider_error = NULL, provider_error_at = NULL,
                        provider_attempt_started_at = NULL, updated_at = NOW()
                    WHERE id = %s AND grant_operation_id = %s
                      AND provider_attempt_started_at = %s
                    RETURNING {_RETURNING}
                    """,
                    (dataset_id, grant_id, operation_id, attempt_token),
                )
                operation_outcome = "succeeded"
            elif verdict.outcome == "failed":
                cur.execute(
                    f"""
                    UPDATE app.dataset_access_grants
                    SET lifecycle_state = 'failed', dataset_id = %s,
                        effective_at = NULL, last_provider_error = %s,
                        provider_error_at = NOW(), provider_attempt_started_at = NULL,
                        updated_at = NOW()
                    WHERE id = %s AND grant_operation_id = %s
                      AND provider_attempt_started_at = %s
                    RETURNING {_RETURNING}
                    """,
                    (dataset_id, verdict.error, grant_id, operation_id, attempt_token),
                )
                operation_outcome = "failed"
            else:
                cur.execute(
                    f"""
                    UPDATE app.dataset_access_grants
                    SET lifecycle_state = 'requested', dataset_id = %s,
                        last_provider_error = %s, provider_error_at = NOW(),
                        provider_attempt_started_at = NULL, updated_at = NOW()
                    WHERE id = %s AND grant_operation_id = %s
                      AND provider_attempt_started_at = %s
                    RETURNING {_RETURNING}
                    """,
                    (dataset_id, verdict.error, grant_id, operation_id, attempt_token),
                )
                operation_outcome = "outcome_unknown"
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    f"SELECT {_RETURNING} FROM app.dataset_access_grants WHERE id = %s",
                    (grant_id,),
                )
                current = cur.fetchone()
                if current is None:
                    raise RuntimeError("dataset-access grant saga row disappeared")
                return _grant_payload(current)
            payload = _grant_payload(row)
            _reconcile_operation(conn, operation_id, operation_outcome, payload)
            if operation_outcome != "outcome_unknown":
                insert_audit_row(
                    conn,
                    identity=actor,
                    action=ACTION_DATASET_ACCESS_GRANT_FINALIZED,
                    provider_account="",
                    connection_ref="",
                    metadata={
                        "org_id": payload["org_id"],
                        "project_id": payload["project_id"],
                        "grant_id": grant_id,
                        "principal": payload["principal"],
                        "dataset_id": dataset_id,
                        "role": _IAM_ROLE,
                        "managed_service_account": payload["managed_service_account"],
                        "outcome": operation_outcome,
                    },
                )
        conn.commit()
    return payload


def _finalize_revoke(
    grant_id: str,
    operation_id: str,
    actor: str,
    verdict: ProviderVerdict,
    attempt_token,
    service_account_deleted: bool = False,
) -> dict[str, Any]:
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        with conn.cursor() as cur:
            if verdict.outcome == "succeeded":
                cur.execute(
                    f"""
                    UPDATE app.dataset_access_grants
                    SET lifecycle_state = 'revoked', revocation_state = NULL,
                        revoked_at = COALESCE(revoked_at, NOW()),
                        last_provider_error = NULL, provider_error_at = NULL,
                        provider_attempt_started_at = NULL, updated_at = NOW(),
                        service_account_deleted_at = CASE
                            WHEN %s THEN COALESCE(service_account_deleted_at, NOW())
                            ELSE service_account_deleted_at END
                    WHERE id = %s AND revoke_operation_id = %s
                      AND provider_attempt_started_at = %s
                    RETURNING {_RETURNING}
                    """,
                    (service_account_deleted, grant_id, operation_id, attempt_token),
                )
                operation_outcome = "succeeded"
            else:
                # A revoke attempt that did not succeed. WHERE the retryable state
                # lives depends on the grant's lifecycle: `revocation_state` is
                # reserved by `dataset_access_grants_revocation_ck` for `effective`
                # grants, so writing it on a `requested`/`failed` grant -- the
                # minted fall-through of the project revoke -- raises a
                # CheckViolation, retires no account and blocks the grant forever.
                # For those, the error is recorded on the lifecycle row itself and
                # the state is kept, retryable by a fresh revoke.
                cur.execute(
                    "SELECT lifecycle_state FROM app.dataset_access_grants WHERE id = %s",
                    (grant_id,),
                )
                current_state = cur.fetchone()
                is_effective = current_state is not None and current_state[0] == "effective"
                if is_effective:
                    revocation_state = "failed" if verdict.outcome == "failed" else "requested"
                    cur.execute(
                        f"""
                        UPDATE app.dataset_access_grants
                        SET revocation_state = %s, last_provider_error = %s,
                            provider_error_at = NOW(), provider_attempt_started_at = NULL,
                            updated_at = NOW()
                        WHERE id = %s AND revoke_operation_id = %s
                          AND provider_attempt_started_at = %s
                        RETURNING {_RETURNING}
                        """,
                        (revocation_state, verdict.error, grant_id, operation_id, attempt_token),
                    )
                else:
                    cur.execute(
                        f"""
                        UPDATE app.dataset_access_grants
                        SET last_provider_error = %s, provider_error_at = NOW(),
                            provider_attempt_started_at = NULL, updated_at = NOW()
                        WHERE id = %s AND revoke_operation_id = %s
                          AND provider_attempt_started_at = %s
                        RETURNING {_RETURNING}
                        """,
                        (verdict.error, grant_id, operation_id, attempt_token),
                    )
                operation_outcome = (
                    "failed" if verdict.outcome == "failed" else "outcome_unknown"
                )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    f"SELECT {_RETURNING} FROM app.dataset_access_grants WHERE id = %s",
                    (grant_id,),
                )
                current = cur.fetchone()
                if current is None:
                    raise RuntimeError("dataset-access revoke saga row disappeared")
                return _grant_payload(current)
            payload = _grant_payload(row)
            _reconcile_operation(conn, operation_id, operation_outcome, payload)
            if operation_outcome != "outcome_unknown":
                insert_audit_row(
                    conn,
                    identity=actor,
                    action=ACTION_DATASET_ACCESS_REVOKE_FINALIZED,
                    provider_account="",
                    connection_ref="",
                    metadata={
                        "org_id": payload["org_id"],
                        "project_id": payload["project_id"],
                        "grant_id": grant_id,
                        "principal": payload["principal"],
                        "dataset_id": payload["dataset_id"],
                        "role": _IAM_ROLE,
                        "managed_service_account": payload["managed_service_account"],
                        "service_account_deleted_at": payload["service_account_deleted_at"],
                        "outcome": operation_outcome,
                    },
                )
        conn.commit()
    return payload


async def _grant_dataset_access(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Bearer token required"}, 401)
    org_id = request.path_params["org_id"]
    try:
        body = json.loads(await request.body())
    except Exception:
        return JSONResponse({"code": "invalid_body", "message": "Invalid JSON body"}, 400)
    principal = str(body.get("principal") or "").strip()
    if not _validate_principal(principal):
        return JSONResponse(
            {
                "code": "invalid_input",
                "message": "principal must be user, serviceAccount or group plus an email",
            },
            422,
        )
    key = request.headers.get("Idempotency-Key", "").strip()
    if not key:
        return JSONResponse(
            {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"},
            422,
        )

    from core.db import get_connection  # noqa: PLC0415
    from core.operations import (  # noqa: PLC0415
        MutationResult,
        OperationIdempotencyConflict,
        execute_operation,
    )

    actor = identity or "anonymous"
    minted_id = _mint_dagrant_id()

    def request_grant(conn, operation_id: str) -> MutationResult:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_RETURNING}
                FROM app.dataset_access_grants
                WHERE org_id = %s AND principal = %s AND revoked_at IS NULL
                FOR UPDATE
                """,
                (org_id, principal),
            )
            row = cur.fetchone()
            if row is None:
                grant_id = minted_id
                cur.execute(
                    f"""
                    INSERT INTO app.dataset_access_grants
                        (id, org_id, principal, granted_by, lifecycle_state, role,
                         grant_operation_id)
                    VALUES (%s, %s, %s, %s, 'requested', %s, %s)
                    RETURNING {_RETURNING}
                    """,
                    (grant_id, org_id, principal, actor, _IAM_ROLE, operation_id),
                )
            else:
                existing = _grant_payload(row)
                if existing["lifecycle_state"] == "effective":
                    raise DatasetAccessConflict(
                        "principal already has effective access to this organization"
                    )
                if existing["lifecycle_state"] == "requested" and existing[
                    "grant_operation_id"
                ] is not None:
                    cur.execute(
                        """
                        SELECT last_provider_error IS NOT NULL OR (
                            provider_attempt_started_at IS NOT NULL
                            AND provider_attempt_started_at <=
                                NOW() - make_interval(secs => %s)
                        ) OR (
                            provider_attempt_started_at IS NULL
                            AND updated_at <= NOW() - make_interval(secs => %s)
                        )
                        FROM app.dataset_access_grants WHERE id = %s
                        """,
                        (
                            _ATTEMPT_LEASE_SECONDS,
                            _ATTEMPT_LEASE_SECONDS,
                            existing["id"],
                        ),
                    )
                    if not cur.fetchone()[0]:
                        raise DatasetAccessConflict(
                            "an access request is already pending; retry its Idempotency-Key"
                        )
                cur.execute(
                    f"""
                    UPDATE app.dataset_access_grants
                    SET lifecycle_state = 'requested', dataset_id = NULL,
                        effective_at = NULL, last_provider_error = NULL,
                        provider_error_at = NULL, grant_operation_id = %s,
                        provider_attempt_started_at = NULL, updated_at = NOW()
                    WHERE id = %s
                    RETURNING {_RETURNING}
                    """,
                    (operation_id, existing["id"]),
                )
            payload = _grant_payload(cur.fetchone())
        return MutationResult(
            outcome="outcome_unknown",
            before_hash=None,
            after_hash=_hash_payload(payload),
            result=payload,
            outbox_payload={
                "org_id": org_id,
                "principal": principal,
                "lifecycle_state": "requested",
                "role": _IAM_ROLE,
            },
        )

    try:
        with get_connection() as conn:
            denied = _enforce_org_manage(org_id, identity, conn, "grant_dataset_access")
            if denied is not None:
                return denied
            operation = execute_operation(
                conn,
                _operation_spec(
                    action=ACTION_DATASET_ACCESS_GRANTED,
                    actor=actor,
                    org_id=org_id,
                    key=key,
                    payload={"principal": principal},
                ),
                mutation=request_grant,
            )
            conn.commit()
    except OperationIdempotencyConflict:
        return JSONResponse(
            {"code": "conflict", "message": "Idempotency-Key is bound to another request"},
            409,
        )
    except DatasetAccessConflict as exc:
        return JSONResponse({"code": "conflict", "message": str(exc)}, 409)
    except Exception:
        logger.exception("dataset access request phase failed org=%s", org_id)
        return JSONResponse({"code": "db_error", "message": "Database error"}, 500)

    payload = operation.result
    if operation.outcome != "outcome_unknown":
        return _terminal_response({**payload, "replayed": True}, created=True)
    try:
        claimed, current, attempt_token = _claim_attempt(
            payload["id"], operation.operation_id, "grant_operation_id"
        )
        if not claimed:
            return _terminal_response({**current, "replayed": True}, created=True)
        schemas, config_error = _resolve_provider_schemas(org_id)
        verdict = (
            ProviderVerdict("failed", config_error)
            if config_error
            else _provider_verdict(schemas, principal, "grant")
        )
        final = _finalize_grant(
            payload["id"],
            operation.operation_id,
            actor,
            schemas.marts if schemas else None,
            verdict,
            attempt_token,
        )
        return _terminal_response(
            {**final, "operation_id": operation.operation_id, "replayed": operation.replayed},
            created=True,
        )
    except Exception:
        logger.exception("dataset access grant saga remains resumable org=%s", org_id)
        return JSONResponse(
            {
                **payload,
                "code": "saga_pending",
                "message": "Grant outcome is pending; retry with the same Idempotency-Key.",
            },
            202,
        )


async def _list_dataset_access_grants(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Bearer token required"}, 401)
    org_id = request.path_params["org_id"]
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.project_access import identity_has_org_access  # noqa: PLC0415

        with get_connection() as conn:
            if not identity_has_org_access(org_id, identity or "anonymous", conn):
                return JSONResponse(
                    {"code": "not_found", "message": "organization not found"}, 404
                )
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {_RETURNING} FROM app.dataset_access_grants
                    WHERE org_id = %s ORDER BY created_at ASC
                    """,
                    (org_id,),
                )
                grants = [_grant_payload(row) for row in cur.fetchall()]
    except Exception:
        logger.exception("dataset access list failed org=%s", org_id)
        return JSONResponse({"code": "db_error", "message": "Database error"}, 500)
    return JSONResponse({"grants": grants}, 200)


async def _revoke_dataset_access(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Bearer token required"}, 401)
    org_id = request.path_params["org_id"]
    grant_id = request.path_params["grant_id"]
    key = request.headers.get("Idempotency-Key", "").strip()
    if not key:
        return JSONResponse(
            {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"},
            422,
        )

    from core.db import get_connection  # noqa: PLC0415
    from core.operations import (  # noqa: PLC0415
        MutationResult,
        OperationIdempotencyConflict,
        execute_operation,
    )

    actor = identity or "anonymous"

    def request_revoke(conn, operation_id: str) -> MutationResult:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_RETURNING} FROM app.dataset_access_grants
                WHERE id = %s AND org_id = %s FOR UPDATE
                """,
                (grant_id, org_id),
            )
            row = cur.fetchone()
            if row is None:
                raise LookupError("grant not found")
            current = _grant_payload(row)
            if current["lifecycle_state"] == "revoked":
                outcome = "succeeded"
                payload = current
            elif current["lifecycle_state"] == "requested":
                safe_legacy_cancel = current["grant_operation_id"] is None
                if not safe_legacy_cancel:
                    cur.execute(
                        """
                        SELECT provider_attempt_started_at IS NULL
                            AND last_provider_error IS NULL
                            AND updated_at <= NOW() - make_interval(secs => %s)
                        FROM app.dataset_access_grants WHERE id = %s
                        """,
                        (_ATTEMPT_LEASE_SECONDS, grant_id),
                    )
                    safe_legacy_cancel = bool(cur.fetchone()[0])
                if not safe_legacy_cancel:
                    raise DatasetAccessConflict(
                        "the provider outcome must be retried before this request "
                        "can be cancelled safely"
                    )
                cur.execute(
                    f"""
                    UPDATE app.dataset_access_grants
                    SET lifecycle_state = 'revoked', revoked_at = NOW(),
                        last_provider_error = NULL, provider_error_at = NULL,
                        grant_operation_id = NULL, revoke_operation_id = %s,
                        provider_attempt_started_at = NULL,
                        updated_at = NOW()
                    WHERE id = %s RETURNING {_RETURNING}
                    """,
                    (operation_id, grant_id),
                )
                payload = _grant_payload(cur.fetchone())
                outcome = "succeeded"
            elif current["lifecycle_state"] == "failed":
                cur.execute(
                    f"""
                    UPDATE app.dataset_access_grants
                    SET lifecycle_state = 'revoked', revoked_at = NOW(),
                        last_provider_error = NULL, provider_error_at = NULL,
                        grant_operation_id = NULL, revoke_operation_id = %s,
                        provider_attempt_started_at = NULL, updated_at = NOW()
                    WHERE id = %s RETURNING {_RETURNING}
                    """,
                    (operation_id, grant_id),
                )
                payload = _grant_payload(cur.fetchone())
                outcome = "succeeded"
            else:
                if current["revocation_state"] == "requested" and current[
                    "revoke_operation_id"
                ] is not None:
                    cur.execute(
                        """
                        SELECT last_provider_error IS NOT NULL OR (
                            provider_attempt_started_at IS NOT NULL
                            AND provider_attempt_started_at <=
                                NOW() - make_interval(secs => %s)
                        ) OR (
                            provider_attempt_started_at IS NULL
                            AND updated_at <= NOW() - make_interval(secs => %s)
                        )
                        FROM app.dataset_access_grants WHERE id = %s
                        """,
                        (_ATTEMPT_LEASE_SECONDS, _ATTEMPT_LEASE_SECONDS, grant_id),
                    )
                    if not cur.fetchone()[0]:
                        raise DatasetAccessConflict(
                            "a revocation is already pending; retry its Idempotency-Key"
                        )
                cur.execute(
                    f"""
                    UPDATE app.dataset_access_grants
                    SET revocation_state = 'requested', last_provider_error = NULL,
                        provider_error_at = NULL, revoke_operation_id = %s,
                        provider_attempt_started_at = NULL, updated_at = NOW()
                    WHERE id = %s RETURNING {_RETURNING}
                    """,
                    (operation_id, grant_id),
                )
                payload = _grant_payload(cur.fetchone())
                outcome = "outcome_unknown"
        return MutationResult(
            outcome=outcome,
            before_hash=None,
            after_hash=_hash_payload(payload),
            result=payload,
            outbox_payload={
                "org_id": org_id,
                "grant_id": grant_id,
                "principal": payload["principal"],
                "revocation_state": payload["revocation_state"],
            },
        )

    try:
        with get_connection() as conn:
            denied = _enforce_org_manage(org_id, identity, conn, "revoke_dataset_access")
            if denied is not None:
                return denied
            operation = execute_operation(
                conn,
                _operation_spec(
                    action=ACTION_DATASET_ACCESS_REVOKED,
                    actor=actor,
                    org_id=org_id,
                    key=key,
                    payload={"grant_id": grant_id},
                ),
                mutation=request_revoke,
            )
            conn.commit()
    except LookupError:
        return JSONResponse({"code": "not_found", "message": "grant not found"}, 404)
    except OperationIdempotencyConflict:
        return JSONResponse(
            {"code": "conflict", "message": "Idempotency-Key is bound to another request"},
            409,
        )
    except DatasetAccessConflict as exc:
        return JSONResponse({"code": "conflict", "message": str(exc)}, 409)
    except Exception:
        logger.exception("dataset access revoke request failed org=%s", org_id)
        return JSONResponse({"code": "db_error", "message": "Database error"}, 500)

    payload = operation.result
    if operation.outcome != "outcome_unknown":
        return _terminal_response({**payload, "replayed": operation.replayed})
    try:
        claimed, current, attempt_token = _claim_attempt(
            grant_id, operation.operation_id, "revoke_operation_id"
        )
        if not claimed:
            return _terminal_response({**current, "replayed": True})
        schemas, config_error = _resolve_provider_schemas(org_id)
        verdict = (
            ProviderVerdict("failed", config_error)
            if config_error
            else _provider_verdict(schemas, payload["principal"], "revoke")
        )
        final = _finalize_revoke(
            grant_id, operation.operation_id, actor, verdict, attempt_token
        )
        return _terminal_response(
            {**final, "operation_id": operation.operation_id, "replayed": operation.replayed}
        )
    except Exception:
        logger.exception("dataset access revoke saga remains resumable org=%s", org_id)
        return JSONResponse(
            {
                **payload,
                "code": "saga_pending",
                "message": "Revocation outcome is pending; retry with the same Idempotency-Key.",
            },
            202,
        )


# ===========================================================================
# Project scope -- the same saga, on the marts of ONE project
# ===========================================================================

ACTION_PROJECT_DATASET_ACCESS_GRANTED = declare_action("dataset_access.project_granted")
ACTION_PROJECT_DATASET_ACCESS_REVOKED = declare_action("dataset_access.project_revoked")

#: Body keys that ask for the mart to be COPIED somewhere. The warehouse is one
#: (BigQuery), the door is a read on it, and there is no outbound connector --
#: so these are refused by name rather than silently ignored.
_COPY_REQUEST_KEYS = (
    "destination",
    "destination_dataset",
    "export_to",
    "copy_to",
    "sink",
    "bucket",
    "gcs_uri",
    "warehouse",
    "snowflake",
    "redshift",
    "postgres",
)

#: Body keys that ask for a grant nobody can take back.
_PERMANENCE_REQUEST_KEYS = ("permanent", "never_expires", "irrevocable")


def _refuse_unless_project_allowed(*args, **kwargs):
    from core.admin_api import _refuse_unless_project_allowed as impl  # noqa: PLC0415

    return impl(*args, **kwargs)


def _project_not_found_response():
    from core.admin_api import _project_not_found_response as impl  # noqa: PLC0415

    return impl()


def _resolve_project_target(project_id: str):
    """Return the project marts target, or the sentence that refuses it.

    A READ: it resolves names and reads flags, and writes nothing.
    """
    import core.warehouse_tenancy as wt  # noqa: PLC0415

    if not wt.bq_provisioning_enabled():
        return None, (
            "BigQuery is not enabled on this deployment, so no dataset can be "
            "opened. Enable BigQuery provisioning and set GOOGLE_CLOUD_PROJECT, "
            "then request the read again."
        )
    try:
        wt.configured_bigquery_project()
    except Exception as exc:
        return None, _provider_error(exc)
    try:
        target, refusal = wt.project_marts_scope(project_id)
    except Exception as exc:  # noqa: BLE001 -- an unresolvable name is a refusal
        return None, _provider_error(exc)
    if target is None:
        return None, refusal
    slate = wt.slate_dataset_reason(target.marts)
    if slate is not None:
        return None, slate
    return target, None


def _refuse_project_grant_body(body: dict) -> str | None:
    """Return the sentence refusing this request, or None when it is grantable."""
    copy_key = next((key for key in _COPY_REQUEST_KEYS if key in body), None)
    if copy_key is not None:
        return (
            f"{copy_key!r} asks for the mart to be copied out of BigQuery, and this "
            "product does not copy it anywhere. Name the person, service account or "
            "group that should READ the mart where it lives."
        )
    permanence_key = next((key for key in _PERMANENCE_REQUEST_KEYS if key in body), None)
    if permanence_key is not None:
        return (
            f"{permanence_key!r} asks for a read that cannot be taken back, and every "
            "read opened here is revocable in one gesture. Drop that field and request "
            "the read again."
        )
    if str(body.get("expires_at") or "").strip().lower() == "never":
        return (
            "'never' asks for a read that cannot be taken back, and every read opened "
            "here is revocable in one gesture. Drop that field and request the read again."
        )
    return None


def _managed_service_account_refusal(body: dict) -> str | None:
    """Refuse minting an account this deployment could never retire."""
    if not body.get("create_service_account"):
        return None
    from core.service_account_provider import (  # noqa: PLC0415
        managed_service_accounts_enabled,
    )

    if managed_service_accounts_enabled():
        return None
    return (
        "This deployment cannot create a service account, so it could not retire one "
        "either — and a read nobody can close is not opened. Enable managed service "
        "accounts, or name a service account that already exists."
    )


def _project_provider_verdict(target, grant_payload: dict, action: str) -> ProviderVerdict:
    """Run the provider side of ONE project-scoped transition.

    Grant order: the account is minted BEFORE the dataset is opened, so a grant
    never names a principal that does not exist.
    Revoke order: the role is removed BEFORE the account is retired, so the read
    is closed even if the account outlives the attempt by one retry.
    """
    from core import service_account_provider as sap  # noqa: PLC0415

    managed = bool(grant_payload.get("managed_service_account"))
    principal = grant_payload["principal"]
    if action == "grant":
        if managed:
            try:
                sap.create_service_account(
                    grant_payload["id"],
                    display_name=(
                        f"toorow outbound read on {grant_payload.get('project_id')}"
                    ),
                )
            except Exception as exc:
                if not _ambiguous_provider_error(exc):
                    return ProviderVerdict("failed", _provider_error(exc))
                return ProviderVerdict("unknown", _provider_error(exc))
        return _provider_verdict(target, principal, "grant")

    verdict = _provider_verdict(target, principal, "revoke")
    if verdict.outcome != "succeeded" or not managed:
        return verdict
    email = principal.split(":", 1)[-1]
    try:
        sap.delete_service_account(email)
    except Exception as exc:
        return ProviderVerdict(
            "failed",
            "The read was removed from the dataset, but the service account "
            f"{email} could not be retired. Retry the revocation to finish it. "
            f"{_provider_error(exc)}",
        )
    return ProviderVerdict("succeeded")


async def _grant_project_dataset_access(request: Request) -> Response:
    """POST /api/projects/{project_id}/dataset-access."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Bearer token required"}, 401)
    project_id = request.path_params["project_id"]
    denied = _refuse_unless_project_allowed(
        identity or "anonymous", project_id, "manage", "dataset-access"
    )
    if denied is not None:
        return denied
    try:
        body = json.loads(await request.body())
    except Exception:
        return JSONResponse({"code": "invalid_body", "message": "Invalid JSON body"}, 400)
    if not isinstance(body, dict):
        return JSONResponse({"code": "invalid_body", "message": "Invalid JSON body"}, 400)

    refusal = _refuse_project_grant_body(body)
    if refusal is None:
        refusal = _managed_service_account_refusal(body)
    if refusal is not None:
        return JSONResponse({"code": "invalid_input", "message": refusal}, 422)

    key = request.headers.get("Idempotency-Key", "").strip()
    if not key:
        return JSONResponse(
            {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"},
            422,
        )

    target, config_refusal = _resolve_project_target(project_id)
    if target is None:
        return JSONResponse(
            {"code": "project_scope_unavailable", "message": config_refusal}, 409
        )

    minted_id = _mint_dagrant_id()
    managed = bool(body.get("create_service_account"))
    if managed:
        from core.service_account_provider import managed_principal  # noqa: PLC0415

        try:
            principal = managed_principal(minted_id)
        except Exception as exc:
            return JSONResponse(
                {"code": "invalid_input", "message": _provider_error(exc)}, 422
            )
    else:
        principal = str(body.get("principal") or "").strip()
    if not _validate_principal(principal):
        return JSONResponse(
            {
                "code": "invalid_input",
                "message": (
                    "Name who reads the mart: a person, a service account or a group, "
                    "written as user:, serviceAccount: or group: plus its address — or "
                    "ask this product to create the service account for you."
                ),
            },
            422,
        )

    from core.db import get_connection  # noqa: PLC0415
    from core.operations import (  # noqa: PLC0415
        MutationResult,
        OperationIdempotencyConflict,
        execute_operation,
    )

    actor = identity or "anonymous"

    def request_grant(conn, operation_id: str) -> MutationResult:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_RETURNING}
                FROM app.dataset_access_grants
                WHERE project_id = %s AND principal = %s AND revoked_at IS NULL
                FOR UPDATE
                """,
                (project_id, principal),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    f"""
                    INSERT INTO app.dataset_access_grants
                        (id, org_id, project_id, principal, granted_by,
                         lifecycle_state, role, grant_operation_id,
                         managed_service_account)
                    VALUES (%s, %s, %s, %s, %s, 'requested', %s, %s, %s)
                    RETURNING {_RETURNING}
                    """,
                    (
                        minted_id,
                        target.org_id,
                        project_id,
                        principal,
                        actor,
                        _IAM_ROLE,
                        operation_id,
                        managed,
                    ),
                )
            else:
                existing = _grant_payload(row)
                if existing["lifecycle_state"] == "effective":
                    raise DatasetAccessConflict(
                        "this reader already reads the published marts of this project"
                    )
                cur.execute(
                    f"""
                    UPDATE app.dataset_access_grants
                    SET lifecycle_state = 'requested', dataset_id = NULL,
                        effective_at = NULL, last_provider_error = NULL,
                        provider_error_at = NULL, grant_operation_id = %s,
                        provider_attempt_started_at = NULL, updated_at = NOW()
                    WHERE id = %s
                    RETURNING {_RETURNING}
                    """,
                    (operation_id, existing["id"]),
                )
            payload = _grant_payload(cur.fetchone())
        return MutationResult(
            outcome="outcome_unknown",
            before_hash=None,
            after_hash=_hash_payload(payload),
            result=payload,
            outbox_payload={
                "project_id": project_id,
                "principal": principal,
                "lifecycle_state": "requested",
                "role": _IAM_ROLE,
            },
        )

    try:
        with get_connection() as conn:
            operation = execute_operation(
                conn,
                _operation_spec(
                    action=ACTION_PROJECT_DATASET_ACCESS_GRANTED,
                    actor=actor,
                    org_id=target.org_id,
                    key=key,
                    payload={"principal": principal, "project_id": project_id},
                    project_id=project_id,
                ),
                mutation=request_grant,
            )
            conn.commit()
    except OperationIdempotencyConflict:
        return JSONResponse(
            {"code": "conflict", "message": "Idempotency-Key is bound to another request"},
            409,
        )
    except DatasetAccessConflict as exc:
        return JSONResponse({"code": "conflict", "message": str(exc)}, 409)
    except Exception:
        logger.exception("project dataset access request phase failed project=%s", project_id)
        return JSONResponse({"code": "db_error", "message": "Database error"}, 500)

    payload = operation.result
    if operation.outcome != "outcome_unknown":
        return _terminal_response({**payload, "replayed": True}, created=True)
    try:
        claimed, current, attempt_token = _claim_attempt(
            payload["id"], operation.operation_id, "grant_operation_id"
        )
        if not claimed:
            return _terminal_response({**current, "replayed": True}, created=True)
        verdict = _project_provider_verdict(target, payload, "grant")
        final = _finalize_grant(
            payload["id"],
            operation.operation_id,
            actor,
            target.marts,
            verdict,
            attempt_token,
        )
        return _terminal_response(
            {**final, "operation_id": operation.operation_id, "replayed": operation.replayed},
            created=True,
        )
    except Exception:
        logger.exception("project dataset access grant remains resumable project=%s", project_id)
        return JSONResponse(
            {
                **payload,
                "code": "saga_pending",
                "message": "Grant outcome is pending; retry with the same Idempotency-Key.",
            },
            202,
        )


async def _list_project_dataset_access_grants(request: Request) -> Response:
    """GET /api/projects/{project_id}/dataset-access -- who reads these marts.

    A READ. The last authentication of a managed account is asked of the
    provider and returned as it is measured; it is NEVER stored, and it is
    ``null`` with a stated reason when the deployment cannot read it.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Bearer token required"}, 401)
    project_id = request.path_params["project_id"]
    denied = _refuse_unless_project_allowed(
        identity or "anonymous", project_id, "view", "dataset-access"
    )
    if denied is not None:
        return denied
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_RETURNING} FROM app.dataset_access_grants
                WHERE project_id = %s ORDER BY created_at ASC
                """,
                (project_id,),
            )
            grants = [_grant_payload(row) for row in cur.fetchall()]
    except Exception:
        logger.exception("project dataset access list failed project=%s", project_id)
        return JSONResponse({"code": "db_error", "message": "Database error"}, 500)
    return JSONResponse({"grants": [_with_last_read(grant) for grant in grants]}, 200)


def _with_last_read(grant: dict[str, Any]) -> dict[str, Any]:
    """Add the measured last authentication of a managed account, or say it is not."""
    if not grant.get("managed_service_account") or grant["lifecycle_state"] != "effective":
        return {**grant, "last_read_at": None, "last_read_state": "not_applicable"}
    from core.service_account_provider import read_last_authentication  # noqa: PLC0415

    stamp = read_last_authentication(grant["principal"].split(":", 1)[-1])
    return {
        **grant,
        "last_read_at": stamp,
        "last_read_state": "measured" if stamp else "not_measurable",
    }


async def _revoke_project_dataset_access(request: Request) -> Response:
    """DELETE /api/projects/{project_id}/dataset-access/{grant_id}."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Bearer token required"}, 401)
    project_id = request.path_params["project_id"]
    grant_id = request.path_params["grant_id"]
    denied = _refuse_unless_project_allowed(
        identity or "anonymous", project_id, "manage", "dataset-access"
    )
    if denied is not None:
        return denied
    key = request.headers.get("Idempotency-Key", "").strip()
    if not key:
        return JSONResponse(
            {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"},
            422,
        )

    from core.db import get_connection  # noqa: PLC0415
    from core.operations import (  # noqa: PLC0415
        MutationResult,
        OperationIdempotencyConflict,
        execute_operation,
    )

    actor = identity or "anonymous"
    target, config_refusal = _resolve_project_target(project_id)

    def request_revoke(conn, operation_id: str) -> MutationResult:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_RETURNING} FROM app.dataset_access_grants
                WHERE id = %s AND project_id = %s FOR UPDATE
                """,
                (grant_id, project_id),
            )
            row = cur.fetchone()
            if row is None:
                raise LookupError("grant not found")
            current = _grant_payload(row)
            if current["lifecycle_state"] == "revoked":
                return MutationResult(
                    outcome="succeeded",
                    before_hash=None,
                    after_hash=_hash_payload(current),
                    result=current,
                    outbox_payload={"project_id": project_id, "grant_id": grant_id},
                )
            minted_live = (
                current["managed_service_account"]
                and current["service_account_deleted_at"] is None
            )
            if current["lifecycle_state"] == "requested":
                # A REQUESTED grant's provider verdict may be UNKNOWN: an attempt
                # in flight or errored may have laid the dataViewer role on the
                # dataset. It is not terminal -- retrying the GRANT (same
                # Idempotency-Key) resolves it to `effective` or `failed`, and only
                # then is it safely revocable. So an unsafe requested grant is
                # refused here, never closed on a guess. A requested grant that is
                # SAFE never made a provider attempt (`grant_operation_id IS NULL`),
                # so it minted no account and laid no role: the column flip closes
                # it.
                safe = current["grant_operation_id"] is None
                if not safe:
                    cur.execute(
                        """
                        SELECT provider_attempt_started_at IS NULL
                            AND last_provider_error IS NULL
                            AND updated_at <= NOW() - make_interval(secs => %s)
                        FROM app.dataset_access_grants WHERE id = %s
                        """,
                        (_ATTEMPT_LEASE_SECONDS, grant_id),
                    )
                    safe = bool(cur.fetchone()[0])
                if not safe:
                    raise DatasetAccessConflict(
                        "the grant is still resolving with the provider; retry the "
                        "grant with its Idempotency-Key to settle it, then revoke"
                    )
                cur.execute(
                    f"""
                    UPDATE app.dataset_access_grants
                    SET lifecycle_state = 'revoked', revoked_at = NOW(),
                        last_provider_error = NULL, provider_error_at = NULL,
                        grant_operation_id = NULL, revoke_operation_id = %s,
                        provider_attempt_started_at = NULL, updated_at = NOW()
                    WHERE id = %s RETURNING {_RETURNING}
                    """,
                    (operation_id, grant_id),
                )
                payload = _grant_payload(cur.fetchone())
                return MutationResult(
                    outcome="succeeded",
                    before_hash=None,
                    after_hash=_hash_payload(payload),
                    result=payload,
                    outbox_payload={"project_id": project_id, "grant_id": grant_id},
                )
            if current["lifecycle_state"] == "failed":
                # A FAILED grant is a TERMINAL verdict: the dataset open raised, so
                # the role was never applied, and retrying the grant would only mint
                # a NEW account and orphan this one -- so it is revocable directly,
                # never a 409. If it minted a service account, that account is still
                # in the project and must be retired BEFORE the grant reads
                # `revoked`: it routes through the provider revocation (remove the
                # role -- an idempotent no-op, the role was never laid -- then retire
                # the account). With nothing on the provider, the column flip closes
                # it.
                if not minted_live:
                    cur.execute(
                        f"""
                        UPDATE app.dataset_access_grants
                        SET lifecycle_state = 'revoked', revoked_at = NOW(),
                            last_provider_error = NULL, provider_error_at = NULL,
                            grant_operation_id = NULL, revoke_operation_id = %s,
                            provider_attempt_started_at = NULL, updated_at = NOW()
                        WHERE id = %s RETURNING {_RETURNING}
                        """,
                        (operation_id, grant_id),
                    )
                    payload = _grant_payload(cur.fetchone())
                    return MutationResult(
                        outcome="succeeded",
                        before_hash=None,
                        after_hash=_hash_payload(payload),
                        result=payload,
                        outbox_payload={"project_id": project_id, "grant_id": grant_id},
                    )
                # The minted account still lives. The effective-grant revocation
                # path below is closed to a `failed` row -- the CHECK reserves
                # `revocation_state` for `effective` and a `failed` grant must keep
                # its `last_provider_error` -- so this claims a revoke attempt
                # WITHOUT touching either, and hands off to the provider
                # follow-through, which retires the account and only THEN finalises
                # the grant to `revoked`.
                cur.execute(
                    f"""
                    UPDATE app.dataset_access_grants
                    SET revoke_operation_id = %s, provider_attempt_started_at = NULL,
                        updated_at = NOW()
                    WHERE id = %s RETURNING {_RETURNING}
                    """,
                    (operation_id, grant_id),
                )
                payload = _grant_payload(cur.fetchone())
                return MutationResult(
                    outcome="outcome_unknown",
                    before_hash=None,
                    after_hash=_hash_payload(payload),
                    result=payload,
                    outbox_payload={
                        "project_id": project_id,
                        "grant_id": grant_id,
                        "principal": payload["principal"],
                    },
                )
            cur.execute(
                f"""
                UPDATE app.dataset_access_grants
                SET revocation_state = 'requested', last_provider_error = NULL,
                    provider_error_at = NULL, revoke_operation_id = %s,
                    provider_attempt_started_at = NULL, updated_at = NOW()
                WHERE id = %s RETURNING {_RETURNING}
                """,
                (operation_id, grant_id),
            )
            payload = _grant_payload(cur.fetchone())
        return MutationResult(
            outcome="outcome_unknown",
            before_hash=None,
            after_hash=_hash_payload(payload),
            result=payload,
            outbox_payload={
                "project_id": project_id,
                "grant_id": grant_id,
                "principal": payload["principal"],
                "revocation_state": payload["revocation_state"],
            },
        )

    try:
        with get_connection() as conn:
            operation = execute_operation(
                conn,
                _operation_spec(
                    action=ACTION_PROJECT_DATASET_ACCESS_REVOKED,
                    actor=actor,
                    org_id=target.org_id if target else project_id,
                    key=key,
                    payload={"grant_id": grant_id, "project_id": project_id},
                    project_id=project_id,
                ),
                mutation=request_revoke,
            )
            conn.commit()
    except LookupError:
        return _project_not_found_response()
    except OperationIdempotencyConflict:
        return JSONResponse(
            {"code": "conflict", "message": "Idempotency-Key is bound to another request"},
            409,
        )
    except DatasetAccessConflict as exc:
        return JSONResponse({"code": "conflict", "message": str(exc)}, 409)
    except Exception:
        logger.exception("project dataset access revoke failed project=%s", project_id)
        return JSONResponse({"code": "db_error", "message": "Database error"}, 500)

    payload = operation.result
    if operation.outcome != "outcome_unknown":
        return _terminal_response({**payload, "replayed": operation.replayed})
    try:
        claimed, current, attempt_token = _claim_attempt(
            grant_id, operation.operation_id, "revoke_operation_id"
        )
        if not claimed:
            return _terminal_response({**current, "replayed": True})
        verdict = (
            ProviderVerdict("failed", config_refusal)
            if target is None
            else _project_provider_verdict(target, payload, "revoke")
        )
        final = _finalize_revoke(
            grant_id,
            operation.operation_id,
            actor,
            verdict,
            attempt_token,
            service_account_deleted=(
                verdict.outcome == "succeeded" and payload["managed_service_account"]
            ),
        )
        return _terminal_response(
            {**final, "operation_id": operation.operation_id, "replayed": operation.replayed}
        )
    except Exception:
        logger.exception("project dataset access revoke remains resumable project=%s", project_id)
        return JSONResponse(
            {
                **payload,
                "code": "saga_pending",
                "message": "Revocation outcome is pending; retry with the same Idempotency-Key.",
            },
            202,
        )


def _mint_dagrant_id() -> str:
    from ulid import ULID  # noqa: PLC0415

    return f"dagrant_{ULID()}"


def _validate_principal(principal: str) -> bool:
    return bool(_IAM_PRINCIPAL_RE.fullmatch(principal.strip()))


DATASET_ACCESS_ROUTES_1 = [
    Route(
        "/api/organizations/{org_id}/dataset-access/{grant_id}",
        endpoint=_revoke_dataset_access,
        methods=["DELETE"],
    ),
    Route(
        "/api/organizations/{org_id}/dataset-access",
        endpoint=_grant_dataset_access,
        methods=["POST"],
    ),
    Route(
        "/api/organizations/{org_id}/dataset-access",
        endpoint=_list_dataset_access_grants,
        methods=["GET"],
    ),
]

PROJECT_DATASET_ACCESS_ROUTES = [
    Route(
        "/api/projects/{project_id}/dataset-access/{grant_id}",
        endpoint=_revoke_project_dataset_access,
        methods=["DELETE"],
    ),
    Route(
        "/api/projects/{project_id}/dataset-access",
        endpoint=_grant_project_dataset_access,
        methods=["POST"],
    ),
    Route(
        "/api/projects/{project_id}/dataset-access",
        endpoint=_list_project_dataset_access_grants,
        methods=["GET"],
    ),
]
