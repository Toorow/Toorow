"""Live Postgres contract for the Story 36.1 deployment gate."""

from __future__ import annotations

import os
import uuid

import pytest

_DSN = os.environ.get("TEST_POSTGRES_DSN")


def test_epic36_deployment_gate_never_silently_skips_live_postgres():
    enabled = os.environ.get("TOOROW_EPIC36_PRODUCTION_ENABLED", "false").lower()
    if enabled in {"1", "true", "yes"}:
        assert _DSN, "TEST_POSTGRES_DSN is mandatory when the Epic 36 gate is enabled"
    if not _DSN:
        pytest.skip("Epic 36 production gate is off; live Postgres is unavailable")


@pytest.mark.skipif(not _DSN, reason="Requires TEST_POSTGRES_DSN")
def test_epic36_rls_owner_floor_and_cross_org_zero_grant_denial():
    import psycopg

    suffix = uuid.uuid4().hex[:12]
    org_id = f"org_e36_{suffix}"
    project_id = f"proj_e36_{suffix}"
    owner = f"owner-e36-{suffix}"
    outsider = f"outsider-e36-{suffix}"
    with psycopg.connect(_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, created_by) "
                "VALUES (%s, %s, %s, %s)",
                (org_id, "Epic 36 RLS", f"epic-36-rls-{suffix}", owner),
            )
            cur.execute(
                "INSERT INTO app.projects "
                "(id, name, slug, status, currency, timezone, created_by, org_id) "
                "VALUES (%s, %s, %s, 'active', 'EUR', 'UTC', %s, %s)",
                (project_id, "Epic 36 RLS", f"epic-36-{suffix}", owner, org_id),
            )
            cur.execute(
                "INSERT INTO app.org_members "
                "(id, org_id, identity, role, status, joined_at) "
                "VALUES (%s, %s, %s, 'owner', 'active', NOW())",
                (f"omem_e36_{suffix}", org_id, owner),
            )
        conn.commit()

        try:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT set_config('toorow.identity', %s, true)", (owner,)
                    )
                    cur.execute(
                        "SELECT set_config('toorow.enforce_epic36', 'on', true)"
                    )
                    cur.execute("SELECT id FROM app.projects WHERE id = %s", (project_id,))
                    assert cur.fetchone() == (project_id,)

            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT set_config('toorow.identity', %s, true)", (outsider,)
                    )
                    cur.execute(
                        "SELECT set_config('toorow.enforce_epic36', 'on', true)"
                    )
                    cur.execute("SELECT id FROM app.projects WHERE id = %s", (project_id,))
                    assert cur.fetchone() is None
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('toorow.enforce_epic36', 'off', true)")
                cur.execute("DELETE FROM app.projects WHERE id = %s", (project_id,))
                cur.execute("DELETE FROM app.organizations WHERE id = %s", (org_id,))
            conn.commit()
