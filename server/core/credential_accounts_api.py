"""Les comptes d un identifiant, et les droits qui les exposent.

AD-43, 2026-08-13. Cinq routes : enregistrer un compte, les lister, accorder un
droit sur l un d eux, le revoquer, lister les droits. Le partage inter-org de
comptes est le sujet -- pas l identifiant lui-meme, dont le cycle de vie vit
ailleurs.
"""

from __future__ import annotations

import json
import logging
import os

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.audit import (
    ACTION_CROSS_SCOPE_ATTEMPT,
    declare_action,
    write_audit_row,
)

logger = logging.getLogger("core.admin_api")

# --- le joint qui reste dans admin_api -----------------------------------
async def _check_auth(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _check_auth as _impl  # noqa: PLC0415
    return await _impl(*args, **kwargs)

def _enforce_credential_org_read(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _enforce_credential_org_read as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

def _enforce_org_manage(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _enforce_org_manage as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

# --- les handlers, deplaces TELS QUELS -----------------------------------

ACTION_ACCOUNT_EXPOSED = declare_action("credential_account_exposed")

ACTION_ACCOUNT_GRANT_REVOKED = declare_action("credential_account_grant_revoked")

async def _revoke_account_grant(request: Request) -> Response:
    """DELETE /api/credentials/{cred}/accounts/{acct}/grants/{org} -- revoke (AC3).

    Offboarding: invalidates the grant. 404 if no active grant. Audited.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    credential_id = request.path_params["credential_id"]
    external_account_id = request.path_params["external_account_id"]
    grantee_org_id = request.path_params["grantee_org_id"]
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                # Story 21.5: only an owner/admin of the credential's OWNER org may
                # revoke a grant. FIX 4: a credential with NO owner org (owner_org_id
                # NULL, legacy/un-backfilled) is NOT manageable -- DENY with a
                # non-disclosing 404 until a backfill assigns an owner org.
                cur.execute(
                    "SELECT owner_org_id FROM app.connection_ref WHERE id = %s",
                    (credential_id,),
                )
                owner_row = cur.fetchone()
                owner_org_id = owner_row[0] if owner_row is not None else None
                if owner_org_id is None:
                    write_audit_row(
                        identity=identity or "anonymous",
                        action=ACTION_CROSS_SCOPE_ATTEMPT,
                        provider_account="",
                        connection_ref=credential_id,
                        metadata={
                            "credential_id": credential_id,
                            "operation": "revoke_account_grant",
                            "reason": "credential_owner_org_null",
                        },
                    )
                    return JSONResponse(
                        {"code": "not_found", "message": "grant not found"},
                        status_code=404,
                    )
                denied = _enforce_org_manage(owner_org_id, identity, conn, "revoke_account_grant")
                if denied is not None:
                    return denied
                cur.execute(
                    "UPDATE app.credential_account_grants "
                    "SET status = 'invalidated', invalidated_at = NOW(), "
                    "invalidation_reason = 'revocation' "
                    "WHERE credential_id = %s AND external_account_id = %s "
                    "AND grantee_org_id = %s AND status = 'active'",
                    (credential_id, external_account_id, grantee_org_id),
                )
                deleted = cur.rowcount
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: revoke_account_grant db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    if not deleted:
        return JSONResponse(
            {"code": "not_found", "message": "grant not found"},
            status_code=404,
        )
    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_ACCOUNT_GRANT_REVOKED,
        provider_account="",
        connection_ref=credential_id,
        metadata={
            "credential_id": credential_id,
            "external_account_id": external_account_id,
            "grantee_org_id": grantee_org_id,
        },
    )
    return JSONResponse({"revoked": True}, status_code=200)

async def _create_account_grant(request: Request) -> Response:
    """POST /api/credentials/{cred}/accounts/{acct}/grants -- expose to an org (AC3).

    Body: {"grantee_org_id": str}. 404 if the account is not in credential_accounts
    or the org does not exist; 409 on a duplicate grant. The ONLY cross-org bridge.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    credential_id = request.path_params["credential_id"]
    external_account_id = request.path_params["external_account_id"]
    try:
        body: dict = json.loads(await request.body())
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )
    grantee_org_id = (body.get("grantee_org_id") or "").strip()
    if not grantee_org_id:
        return JSONResponse(
            {"code": "invalid_input", "message": "grantee_org_id is required"},
            status_code=422,
        )
    grant_id = _mint_grant_id()
    granted_by = identity or "anonymous"
    operation_result = None
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM app.credential_accounts "
                    "WHERE credential_id = %s AND external_account_id = %s",
                    (credential_id, external_account_id),
                )
                if cur.fetchone() is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "credential account not found"},
                        status_code=404,
                    )
                # Story 21.5: only an owner/admin of the credential's OWNER org may
                # expose an account. FIX 4: a credential with NO owner org
                # (owner_org_id NULL, legacy/un-backfilled) is NOT exposable -- DENY
                # with a non-disclosing 404 (consistent with cross-scope denials)
                # until a backfill assigns an owner org. Default-open org -> owner.
                cur.execute(
                    "SELECT owner_org_id FROM app.connection_ref WHERE id = %s",
                    (credential_id,),
                )
                owner_row = cur.fetchone()
                owner_org_id = owner_row[0] if owner_row is not None else None
                if owner_org_id is None:
                    write_audit_row(
                        identity=identity or "anonymous",
                        action=ACTION_CROSS_SCOPE_ATTEMPT,
                        provider_account="",
                        connection_ref=credential_id,
                        metadata={
                            "credential_id": credential_id,
                            "operation": "create_account_grant",
                            "reason": "credential_owner_org_null",
                        },
                    )
                    return JSONResponse(
                        {"code": "not_found", "message": "credential account not found"},
                        status_code=404,
                    )
                denied = _enforce_org_manage(owner_org_id, identity, conn, "create_account_grant")
                if denied is not None:
                    return denied
                cur.execute("SELECT 1 FROM app.organizations WHERE id = %s", (grantee_org_id,))
                if cur.fetchone() is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "grantee organization not found"},
                        status_code=404,
                    )

                idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
                if not idempotency_key:
                    return JSONResponse(
                        {
                            "code": "missing_idempotency_key",
                            "message": "Idempotency-Key is required",
                        },
                        status_code=422,
                    )
                from core import tracing  # noqa: PLC0415
                from core.account_exposure import (  # noqa: PLC0415
                    AccountExposureConflict,
                    expose_account,
                )
                from core.operations import OperationIdempotencyConflict  # noqa: PLC0415

                try:
                    operation_result = expose_account(
                        conn,
                        grant_id=grant_id,
                        credential_id=credential_id,
                        external_account_id=external_account_id,
                        owner_org_id=owner_org_id,
                        grantee_org_id=grantee_org_id,
                        actor=granted_by,
                        idempotency_key=idempotency_key,
                        host_context={
                            "host": "rest",
                            "workspace_id": (
                                request.headers.get("X-Workspace-Id") or "console"
                            )[:256],
                        },
                        versions={
                            "policy": os.environ.get("TOOROW_POLICY_VERSION", "v1"),
                            "catalog": os.environ.get("TOOROW_CATALOG_VERSION", "v1"),
                            "tool": "rest-v1",
                        },
                        confirmation_reference=request.headers.get("X-Confirmation-Reference"),
                        trace_id=tracing.current_trace_id_hex(),
                    )
                except (AccountExposureConflict, OperationIdempotencyConflict):
                    return JSONResponse(
                        {
                            "code": "conflict",
                            "message": "operation conflicts with existing state",
                        },
                        status_code=409,
                    )
                r = operation_result.result
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: create_account_grant db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    if operation_result is None:
        write_audit_row(
            identity=granted_by,
            action=ACTION_ACCOUNT_EXPOSED,
            provider_account="",
            connection_ref=credential_id,
            metadata={
                "credential_id": credential_id,
                "external_account_id": external_account_id,
                "grantee_org_id": grantee_org_id,
            },
        )
        payload = {
            "id": r[0],
            "credential_id": r[1],
            "external_account_id": r[2],
            "grantee_org_id": r[3],
            "granted_by": r[4],
            "created_at": r[5].isoformat() if r[5] else None,
        }
    else:
        payload = dict(r)
        payload["operation_id"] = operation_result.operation_id
        payload["audit_event_id"] = operation_result.audit_event_id
        payload["replayed"] = operation_result.replayed
    return JSONResponse(payload, status_code=201)

async def _list_credential_accounts(request: Request) -> Response:
    """GET /api/credentials/{credential_id}/accounts -- accounts of a credential (AC2)."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    credential_id = request.path_params["credential_id"]
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            denied = _enforce_credential_org_read(credential_id, identity, conn)
            if denied is not None:
                return denied
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT credential_id, external_account_id, label, discovered_at
                    FROM app.credential_accounts
                    WHERE credential_id = %s
                    ORDER BY external_account_id ASC
                    """,
                    (credential_id,),
                )
                accounts = [
                    {
                        "credential_id": r[0],
                        "external_account_id": r[1],
                        "label": r[2],
                        "discovered_at": r[3].isoformat() if r[3] else None,
                    }
                    for r in cur.fetchall()
                ]
    except Exception as exc:
        logger.error("admin_api: list_credential_accounts db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    return JSONResponse({"accounts": accounts}, status_code=200)

async def _register_credential_account(request: Request) -> Response:
    """POST /api/credentials/{credential_id}/accounts -- upsert an account (AC2).

    Discovery stub: body {"external_account_id": str, "label"?: str}. Upserts into
    credential_accounts. 404 if the credential does not exist.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    credential_id = request.path_params["credential_id"]
    try:
        body: dict = json.loads(await request.body())
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )
    external_account_id = (body.get("external_account_id") or "").strip()
    if not external_account_id or len(external_account_id) > 255:
        return JSONResponse(
            {"code": "invalid_input", "message": "external_account_id is required"},
            status_code=422,
        )
    label = body.get("label")
    label = str(label).strip() if label else None
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                # FIX 5: gate account registration on the credential's OWNER-org
                # manage role (same enforcement the grant path uses). A missing
                # credential -> 404; a NULL owner_org_id (legacy/un-backfilled) is
                # NOT registerable -> non-disclosing 404 until backfill.
                cur.execute(
                    "SELECT owner_org_id FROM app.connection_ref WHERE id = %s",
                    (credential_id,),
                )
                cred_row = cur.fetchone()
                if cred_row is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "credential not found"},
                        status_code=404,
                    )
                owner_org_id = cred_row[0]
                if owner_org_id is None:
                    write_audit_row(
                        identity=identity or "anonymous",
                        action=ACTION_CROSS_SCOPE_ATTEMPT,
                        provider_account="",
                        connection_ref=credential_id,
                        metadata={
                            "credential_id": credential_id,
                            "operation": "register_credential_account",
                            "reason": "credential_owner_org_null",
                        },
                    )
                    return JSONResponse(
                        {"code": "not_found", "message": "credential not found"},
                        status_code=404,
                    )
                denied = _enforce_org_manage(
                    owner_org_id, identity, conn, "register_credential_account"
                )
                if denied is not None:
                    return denied
                # `source_account_id` is NOT NULL since migration 133:14, and this
                # INSERT never learned to write it -- so registering an account by
                # hand has raised NotNullViolation for every caller since. The mint
                # is imported from `account_topology` rather than re-spelled: two
                # spellings of one identity is the defect the column exists to stop.
                #
                # ON CONFLICT PRESERVES the existing identity instead of taking
                # EXCLUDED's. Re-registering an account must relabel it, never
                # re-identify it: the identity is stable by contract (133:1) and a
                # silently rotated one would orphan everything pinned to it.
                from core.account_topology import _mint_source_account_id  # noqa: PLC0415

                cur.execute(
                    """
                    INSERT INTO app.credential_accounts
                        (source_account_id, credential_id, external_account_id, label)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (credential_id, external_account_id)
                    DO UPDATE SET
                        source_account_id = app.credential_accounts.source_account_id,
                        label = EXCLUDED.label
                    RETURNING credential_id, external_account_id, label, discovered_at
                    """,
                    (_mint_source_account_id(), credential_id, external_account_id, label),
                )
                r = cur.fetchone()
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: register_credential_account db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    return JSONResponse(
        {
            "credential_id": r[0],
            "external_account_id": r[1],
            "label": r[2],
            "discovered_at": r[3].isoformat() if r[3] else None,
        },
        status_code=201,
    )

async def _list_credential_grants(request: Request) -> Response:
    """GET /api/credentials/{credential_id}/grants -- grants (account -> org) (AC3)."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    credential_id = request.path_params["credential_id"]
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            denied = _enforce_credential_org_read(credential_id, identity, conn)
            if denied is not None:
                return denied
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, credential_id, external_account_id, grantee_org_id,
                           granted_by, created_at
                    FROM app.credential_account_grants
                    WHERE credential_id = %s AND status = 'active'
                    ORDER BY external_account_id ASC, created_at ASC
                    """,
                    (credential_id,),
                )
                grants = [
                    {
                        "id": r[0],
                        "credential_id": r[1],
                        "external_account_id": r[2],
                        "grantee_org_id": r[3],
                        "granted_by": r[4],
                        "created_at": r[5].isoformat() if r[5] else None,
                    }
                    for r in cur.fetchall()
                ]
    except Exception as exc:
        logger.error("admin_api: list_credential_grants db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    return JSONResponse({"grants": grants}, status_code=200)

def _mint_grant_id() -> str:
    """Mint a prefixed ULID 'cgrant_<ULID>' for a credential account grant."""
    from ulid import ULID  # noqa: PLC0415

    return f"cgrant_{ULID()}"


# --- LES ROUTES, dans l'ordre exact ou elles etaient declarees ------------
#
# Starlette resout dans l'ordre de declaration ; chaque collection est epissee
# a la position que ses routes occupaient. La preuve est un dump avant/apres.

CREDENTIAL_ACCOUNTS_ROUTES_1 = [
    # Story 21.3: credential accounts + per-account cross-org grants. Most
    # specific (grants under an account) declared before the shorter shapes.
    Route(
        "/api/credentials/{credential_id}/accounts/{external_account_id}/grants/{grantee_org_id}",
        endpoint=_revoke_account_grant,
        methods=["DELETE"],
    ),
    Route(
        "/api/credentials/{credential_id}/accounts/{external_account_id}/grants",
        endpoint=_create_account_grant,
        methods=["POST"],
    ),
    Route(
        "/api/credentials/{credential_id}/accounts",
        endpoint=_list_credential_accounts,
        methods=["GET"],
    ),
    Route(
        "/api/credentials/{credential_id}/accounts",
        endpoint=_register_credential_account,
        methods=["POST"],
    ),
    Route(
        "/api/credentials/{credential_id}/grants",
        endpoint=_list_credential_grants,
        methods=["GET"],
    ),
]
