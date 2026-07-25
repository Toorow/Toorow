"""Live Postgres lifecycle/race gates for Story 36.5."""

from __future__ import annotations

import os
import uuid

import pytest

_DSN = os.environ.get("TEST_POSTGRES_DSN")


def test_invitation_lifecycle_deployment_gate_never_silently_skips_postgres():
    enabled = os.environ.get("TOOROW_EPIC36_PRODUCTION_ENABLED", "false").lower()
    if enabled in {"1", "true", "yes"}:
        assert _DSN, "TEST_POSTGRES_DSN is mandatory when the Epic 36 gate is enabled"
    if not _DSN:
        pytest.skip("Epic 36 production gate is off; live Postgres is unavailable")


@pytest.mark.skipif(not _DSN, reason="Requires TEST_POSTGRES_DSN")
def test_live_accepted_invitation_is_immutable():
    import psycopg

    suffix = uuid.uuid4().hex[:12]
    org_id = f"org_inv_life_{suffix}"
    operation_id = f"op_inv_life_{suffix}"
    invitation_id = f"invite_life_{suffix}"
    with psycopg.connect(_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('app.invitation_exchange_sessions')")
            assert cur.fetchone()[0], "migrations 062-064 must be applied"
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, created_by) "
                "VALUES (%s, %s, %s, %s)",
                (org_id, "Invitation lifecycle", f"inv-life-{suffix}", "owner-1"),
            )
            cur.execute(
                """
                INSERT INTO app.operations
                    (id, effective_org_id, command_type, actor, resource_path,
                     host_context, versions, request_hash, provider_references,
                     confirmation_mode, idempotency_key_hash, state)
                VALUES (%s, %s, 'test.invitation', 'owner-1', '[]'::jsonb,
                        '{}'::jsonb, '{}'::jsonb, %s, '{}'::jsonb,
                        'server', %s, 'succeeded')
                """,
                (operation_id, org_id, "a" * 64, "b" * 64),
            )
            cur.execute(
                """
                INSERT INTO app.invitations
                    (id, invited_identity_hash, org_id, invited_role, grant_bindings,
                     issuer, policy_version, bearer_hash, state, expires_at,
                     operation_id, accepted_at)
                VALUES (%s, %s, %s, 'member', '[]'::jsonb, 'owner-1', 'p1',
                        %s, 'accepted', NOW() + INTERVAL '1 hour', %s, NOW())
                """,
                (invitation_id, "c" * 64, org_id, "d" * 64, operation_id),
            )
        conn.commit()
        try:
            with pytest.raises(psycopg.errors.RaiseException, match="accepted invitation"):
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE app.invitations SET state = 'revoked' WHERE id = %s",
                            (invitation_id,),
                        )
        finally:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM app.invitations WHERE id = %s", (invitation_id,))
                cur.execute("DELETE FROM app.operations WHERE id = %s", (operation_id,))
                cur.execute("DELETE FROM app.organizations WHERE id = %s", (org_id,))
            conn.commit()
