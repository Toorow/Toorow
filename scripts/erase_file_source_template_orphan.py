"""Inspect or erase one exact orphaned file-source Template.

Dry-run is the default. Execution requires an exact approval token and records
the deletion through the operations/audit/outbox path before committing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template-id", required=True)
    parser.add_argument("--expected-project-id", required=True)
    parser.add_argument("--expected-org-id", required=True)
    parser.add_argument("--expected-template-code", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--dsn-env", default="TOOROW_DATABASE_URL")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approval-token")
    return parser


def _read_exact(conn, args: argparse.Namespace, *, lock: bool = False) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.id, t.project_id, t.template_code, t.version, t.content_hash,
                   (p.id IS NULL) AS project_missing
            FROM app.file_source_templates AS t
            LEFT JOIN app.projects AS p ON p.id = t.project_id
            WHERE t.id = %s
              AND t.project_id = %s
              AND t.template_code = %s
            """ + (" FOR UPDATE OF t" if lock else ""),
            (
                args.template_id,
                args.expected_project_id,
                args.expected_template_code,
            ),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "project_id": row[1],
        "template_code": row[2],
        "version": row[3],
        "content_hash": row[4],
        "project_missing": bool(row[5]),
    }


def main() -> int:
    args = _parser().parse_args()
    dsn = os.environ.get(args.dsn_env, "").strip()
    if not dsn:
        raise SystemExit(f"{args.dsn_env} is required")

    import psycopg  # noqa: PLC0415

    with psycopg.connect(dsn) as conn:
        candidate = _read_exact(conn, args)
        if candidate is None:
            print(json.dumps({"matched": 0, "executed": False}))
            return 2
        if not candidate["project_missing"]:
            print(json.dumps({"matched": 1, "orphan": False, "executed": False}))
            return 3
        if not args.execute:
            print(
                json.dumps(
                    {
                        "matched": 1,
                        "orphan": True,
                        "executed": False,
                        "candidate": candidate,
                        "approval_required": True,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.approval_token != f"ERASE:{args.template_id}":
            raise SystemExit("exact --approval-token ERASE:<template-id> is required")

        from core.operations import (  # noqa: PLC0415
            MutationResult,
            OperationSpec,
            _canonical_hash,
            execute_operation,
        )

        def mutation(operation_conn, operation_id: str) -> MutationResult:
            locked = _read_exact(operation_conn, args, lock=True)
            if locked is None or not locked["project_missing"]:
                raise RuntimeError("the exact orphan changed after dry-run")
            with operation_conn.cursor() as cur:
                cur.execute("SET LOCAL app.rgpd_erasure = 'on'")
                cur.execute(
                    "SELECT set_config('app.file_source_erasure_operation_id', %s, true)",
                    (operation_id,),
                )
                cur.execute(
                    """
                    DELETE FROM app.file_source_templates
                    WHERE id = %s AND project_id = %s AND template_code = %s
                    RETURNING id
                    """,
                    (
                        args.template_id,
                        args.expected_project_id,
                        args.expected_template_code,
                    ),
                )
                deleted = cur.fetchone()
            if deleted is None:
                raise RuntimeError("the exact orphan was not deleted")
            with operation_conn.cursor() as cur:
                cur.execute(
                    "ALTER TABLE app.file_source_templates "
                    "VALIDATE CONSTRAINT fk_file_source_template_project"
                )
            result = {
                "template_id": args.template_id,
                "project_id": args.expected_project_id,
                "template_code": args.expected_template_code,
                "deleted": True,
            }
            return MutationResult(
                outcome="succeeded",
                before_hash=_canonical_hash(locked),
                after_hash=None,
                result=result,
                outbox_payload=result,
            )

        spec = OperationSpec(
            command_type="file_source.template.orphan_erased",
            actor=args.actor,
            effective_org_id=args.expected_org_id,
            resource_path=(
                "platform:toorow",
                f"organization:{args.expected_org_id}",
                f"project:{args.expected_project_id}",
                f"file_source_template:{args.template_id}",
            ),
            idempotency_key=(
                f"erase-file-source-orphan:{args.template_id}:{candidate['content_hash']}"
            ),
            host_context={},
            versions={"policy": "file-source-orphan-erasure-v1"},
            request_payload={
                "template_id": args.template_id,
                "expected_project_id": args.expected_project_id,
                "expected_org_id": args.expected_org_id,
                "expected_template_code": args.expected_template_code,
                "content_hash": candidate["content_hash"],
            },
            provider_references={},
            confirmation_mode="human",
            confirmation_reference=f"ERASE:{args.template_id}",
        )
        operation = execute_operation(conn, spec, mutation=mutation)
        conn.commit()
        print(
            json.dumps(
                {
                    **operation.result,
                    "operation_id": operation.operation_id,
                    "replayed": operation.replayed,
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
