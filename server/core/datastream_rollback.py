"""Exact confirmed restoration of a compatible Datastream publication set."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from ulid import ULID


class DatastreamRollbackError(ValueError):
    code = "invalid_datastream_rollback"


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def prepare_rollback(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    target_execution_id: str | None,
    actor: str,
) -> dict[str, Any]:
    from core.dataset_recovery import preview_rollback  # noqa: PLC0415

    preview = preview_rollback(
        conn,
        datastream_id=datastream_id,
        project_id=project_id,
        target_execution_id=target_execution_id,
    )
    if preview.get("available") is not True:
        raise DatastreamRollbackError(str(preview.get("reason") or "Rollback is unavailable"))
    target = str(preview["target_execution_id"])
    with conn.cursor() as cur:
        cur.execute(
            """SELECT d.org_id,d.current_published_execution_id,e.plan_version_id,
                      e.mapping_version_id,p.normalized_payload,e.content_hash,e.row_count
                 FROM app.datastreams d
                 JOIN app.datastream_executions e
                   ON e.id=%s AND e.datastream_id=d.id AND e.project_id=d.project_id
                 JOIN app.datastream_plan_versions p ON p.id=e.plan_version_id
                WHERE d.id=%s AND d.project_id=%s FOR UPDATE""",
            (target, datastream_id, project_id),
        )
        row = cur.fetchone()
        if row is None or not row[1]:
            raise DatastreamRollbackError("Rollback target evidence changed")
        plan = row[4] if isinstance(row[4], dict) else json.loads(row[4])
        review = {
            "project_id": project_id,
            "datastream_id": datastream_id,
            "expected_current_execution_id": row[1],
            "target_execution_id": target,
            "target_plan_version_id": row[2],
            "target_mapping_version_id": row[3],
            "target_schedule": plan.get("schedule"),
            "target_content_hash": row[5],
            "target_row_count": row[6],
            "rollback_deadline": preview.get("rollback_deadline"),
            "consequence": (
                "Atomically restore publication, plan, mapping, lifecycle and schedule pointers"
            ),
        }
        secret = secrets.token_urlsafe(32)
        preparation_id = f"dsrp_{ULID()}"
        cur.execute(
            """INSERT INTO app.datastream_rollback_preparations
               (id,org_id,project_id,datastream_id,expected_current_execution_id,
                target_execution_id,review,review_hash,confirmation_secret_hash,
                prepared_by,expires_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)""",
            (
                preparation_id,
                row[0],
                project_id,
                datastream_id,
                row[1],
                target,
                json.dumps(review, sort_keys=True, default=str),
                _hash(review),
                hashlib.sha256(secret.encode()).hexdigest(),
                actor,
                datetime.now(UTC) + timedelta(minutes=10),
            ),
        )
    return {
        "preparation_id": preparation_id,
        "confirmation_secret": secret,
        "review": review,
        "expires_in_seconds": 600,
    }


def confirm_rollback(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    preparation_id: str,
    confirmation_secret: str,
    actor: str,
) -> dict[str, Any]:
    from core.dataset_recovery import rollback_dataset  # noqa: PLC0415
    from core.operations import MutationResult, OperationSpec, execute_operation  # noqa: PLC0415

    with conn.cursor() as cur:
        cur.execute(
            """SELECT r.expected_current_execution_id,r.target_execution_id,
                      r.confirmation_secret_hash,r.state,r.expires_at,r.org_id,
                      r.review_hash
                 FROM app.datastream_rollback_preparations r
                WHERE r.id=%s AND r.project_id=%s AND r.datastream_id=%s FOR UPDATE""",
            (preparation_id, project_id, datastream_id),
        )
        row = cur.fetchone()
        if row is None:
            raise DatastreamRollbackError("Rollback preparation was not found")
        if row[3] != "prepared" or row[4] <= datetime.now(UTC):
            raise DatastreamRollbackError("Rollback preparation is no longer confirmable")
        if hashlib.sha256(confirmation_secret.encode()).hexdigest() != row[2]:
            raise DatastreamRollbackError("Rollback confirmation is invalid")
        cur.execute(
            "SELECT current_published_execution_id FROM app.datastreams "
            "WHERE id=%s AND project_id=%s FOR UPDATE",
            (datastream_id, project_id),
        )
        current = cur.fetchone()
        if current is None or current[0] != row[0]:
            raise DatastreamRollbackError("Current changed after rollback review")

    def mutation(tx, operation_id: str) -> MutationResult:
        result = rollback_dataset(
            tx,
            datastream_id=datastream_id,
            project_id=project_id,
            actor=actor,
            target_execution_id=row[1],
            manage_transaction=False,
        )
        with tx.cursor() as cur:
            cur.execute(
                """UPDATE app.datastream_rollback_preparations
                      SET state='confirmed',confirmed_at=NOW(),publication_log_id=%s,
                          operation_id=%s
                    WHERE id=%s AND state='prepared'""",
                (result.get("publication_log_id"), operation_id, preparation_id),
            )
        operation_result = {
            "preparation_id": preparation_id,
            "operation_id": operation_id,
            **result,
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=_hash({"execution_id": row[0]}),
            after_hash=_hash({"execution_id": row[1]}),
            result=operation_result,
            outbox_payload={"event": "datastream.rollback.confirmed", **operation_result},
        )

    operation = execute_operation(
        conn,
        OperationSpec(
            command_type="datastream.rollback",
            actor=actor,
            effective_org_id=row[5],
            resource_path=("projects", project_id, "datastreams", datastream_id),
            idempotency_key=preparation_id,
            host_context={},
            versions={},
            request_payload={
                "preparation_id": preparation_id,
                "review_hash": row[6],
                "expected_current_execution_id": row[0],
                "target_execution_id": row[1],
            },
            provider_references={},
            confirmation_mode="human",
            confirmation_reference=confirmation_secret,
            trace_id=None,
        ),
        mutation=mutation,
    )
    return {**operation.result, "replayed": operation.replayed}
