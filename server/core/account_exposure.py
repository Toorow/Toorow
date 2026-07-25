"""Credential-account exposure domain command shared by REST and future MCP adapters."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from core.operations import MutationResult, OperationResult, OperationSpec, execute_operation


class AccountExposureConflict(RuntimeError):
    """The exact account is already actively exposed to the beneficiary."""


def _state_hash(value: dict[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def expose_account(
    conn,
    *,
    grant_id: str,
    credential_id: str,
    external_account_id: str,
    owner_org_id: str,
    grantee_org_id: str,
    actor: str,
    idempotency_key: str,
    host_context: dict,
    versions: dict,
    confirmation_reference: str | None,
    trace_id: str | None,
) -> OperationResult:
    """Atomically activate one exact exposure through the Story 36.2 seam."""

    def mutation(operation_conn, _operation_id: str) -> MutationResult:
        with operation_conn.cursor() as cur:
            cur.execute(
                "SELECT id, status FROM app.credential_account_grants "
                "WHERE credential_id = %s AND external_account_id = %s "
                "AND grantee_org_id = %s FOR UPDATE",
                (credential_id, external_account_id, grantee_org_id),
            )
            existing = cur.fetchone()
            before = {
                "status": existing[1] if existing else "absent",
                "credential_id": credential_id,
                "account_id": external_account_id,
                "grantee_org_id": grantee_org_id,
            }
            if existing is not None and existing[1] == "active":
                raise AccountExposureConflict("account already granted to this org")
            if existing is not None:
                cur.execute(
                    """
                    UPDATE app.credential_account_grants
                    SET status = 'active', granted_by = %s, invalidated_at = NULL,
                        invalidation_reason = NULL,
                        exposure_version = exposure_version + 1
                    WHERE id = %s
                    RETURNING id, credential_id, external_account_id, grantee_org_id,
                              granted_by, created_at
                    """,
                    (actor, existing[0]),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO app.credential_account_grants
                        (id, credential_id, external_account_id, grantee_org_id, granted_by)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id, credential_id, external_account_id, grantee_org_id,
                              granted_by, created_at
                    """,
                    (grant_id, credential_id, external_account_id, grantee_org_id, actor),
                )
            row = cur.fetchone()
        created_at = row[5]
        result = {
            "id": row[0],
            "credential_id": row[1],
            "external_account_id": row[2],
            "grantee_org_id": row[3],
            "granted_by": row[4],
            "created_at": (
                created_at.isoformat() if isinstance(created_at, datetime) else created_at
            ),
        }
        after = {**before, "status": "active", "grant_id": row[0]}
        return MutationResult(
            outcome="succeeded",
            before_hash=_state_hash(before),
            after_hash=_state_hash(after),
            result=result,
            outbox_payload={
                "grant_id": row[0],
                "credential_id": credential_id,
                "account_id": external_account_id,
                "grantee_org_id": grantee_org_id,
            },
        )

    return execute_operation(
        conn,
        OperationSpec(
            command_type="credential.account.expose",
            actor=actor,
            effective_org_id=owner_org_id,
            resource_path=(
                f"organization:{owner_org_id}",
                f"credential:{credential_id}",
                f"account:{external_account_id}",
                f"beneficiary:{grantee_org_id}",
            ),
            idempotency_key=idempotency_key,
            host_context=host_context,
            versions=versions,
            request_payload={"grantee_org_id": grantee_org_id},
            provider_references={
                "connection_ref": credential_id,
                "account_id": external_account_id,
            },
            confirmation_mode="server",
            confirmation_reference=confirmation_reference,
            trace_id=trace_id,
        ),
        mutation=mutation,
    )
