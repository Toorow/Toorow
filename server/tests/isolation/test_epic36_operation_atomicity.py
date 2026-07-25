"""Live Postgres transaction/constraint gate for Story 36.2."""

from __future__ import annotations

import os
import uuid

import pytest

_DSN = os.environ.get("TEST_POSTGRES_DSN")


def test_operation_deployment_gate_never_silently_skips_live_postgres():
    enabled = os.environ.get("TOOROW_EPIC36_PRODUCTION_ENABLED", "false").lower()
    if enabled in {"1", "true", "yes"}:
        assert _DSN, "TEST_POSTGRES_DSN is mandatory when the Epic 36 gate is enabled"
    if not _DSN:
        pytest.skip("Epic 36 production gate is off; live Postgres is unavailable")


@pytest.mark.skipif(not _DSN, reason="Requires TEST_POSTGRES_DSN")
def test_live_operation_mutation_audit_outbox_are_atomic_and_idempotent():
    import psycopg
    from core.operations import MutationResult, OperationSpec, execute_operation

    suffix = uuid.uuid4().hex[:12]
    org_id = f"org_op_{suffix}"
    with psycopg.connect(_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, created_by) "
                "VALUES (%s, %s, %s, %s)",
                (org_id, "Operation atomicity", f"operation-{suffix}", "owner-1"),
            )

        calls = 0

        def mutation(operation_conn, _operation_id):
            nonlocal calls
            calls += 1
            with operation_conn.cursor() as cur:
                cur.execute(
                    "UPDATE app.organizations SET billing_ref = %s WHERE id = %s",
                    ("changed", org_id),
                )
            return MutationResult(
                "succeeded", None, "a" * 64, {"changed": True}, {"changed": True}
            )

        spec = OperationSpec(
            command_type="test.atomic",
            actor="owner-1",
            effective_org_id=org_id,
            resource_path=(f"organization:{org_id}",),
            idempotency_key="same-key",
            host_context={"host": "test"},
            versions={"policy": "p1", "catalog": "c1", "tool": "t1"},
            request_payload={"change": True},
            provider_references={},
            confirmation_mode="server",
            confirmation_reference="confirmation",
            trace_id=None,
        )
        first = execute_operation(conn, spec, mutation=mutation)
        second = execute_operation(conn, spec, mutation=mutation)
        assert first.operation_id == second.operation_id
        assert second.replayed is True
        assert calls == 1
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM app.operations WHERE effective_org_id = %s", (org_id,)
            )
            assert cur.fetchone()[0] == 1
            cur.execute(
                "SELECT COUNT(*) FROM app.audit_log WHERE operation_id = %s",
                (first.operation_id,),
            )
            assert cur.fetchone()[0] == 1
            cur.execute(
                "SELECT COUNT(*) FROM app.operation_outbox WHERE operation_id = %s",
                (first.operation_id,),
            )
            assert cur.fetchone()[0] == 1
        conn.rollback()

        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM app.organizations WHERE id = %s", (org_id,))
            assert cur.fetchone() is None
