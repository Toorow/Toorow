"""Exercise the 106 -> current migration upgrade while preserving tenant rows."""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import psycopg
from apply_migrations import apply_migrations, load_migrations
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo


def _upgrade_dsn(admin_dsn: str, database: str) -> str:
    params = conninfo_to_dict(admin_dsn)
    params["dbname"] = database
    return make_conninfo(**params)


def _seed_fixture(conn: psycopg.Connection) -> tuple[str, ...]:
    fixture = (
        "org_upgrade_fixture",
        "omem_upgrade_fixture",
        "proj_upgrade_fixture",
        "pmem_upgrade_fixture",
        "op_upgrade_fixture",
        "inv_upgrade_fixture",
    )
    org_id, org_member_id, project_id, project_member_id, operation_id, invitation_id = fixture
    with conn.transaction():
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO app.organizations (id, name, slug, created_by) "
                "VALUES (%s, %s, %s, %s)",
                (org_id, "Upgrade Fixture", "upgrade-fixture", "fixture-subject"),
            )
            cursor.execute(
                "INSERT INTO app.org_members "
                "(id, org_id, identity, role, status, joined_at) "
                "VALUES (%s, %s, %s, 'owner', 'active', now())",
                (org_member_id, org_id, "fixture-subject"),
            )
            cursor.execute(
                "INSERT INTO app.projects "
                "(id, org_id, name, slug, created_by) VALUES (%s, %s, %s, %s, %s)",
                (project_id, org_id, "Upgrade Project", "upgrade-project", "fixture-subject"),
            )
            cursor.execute(
                "INSERT INTO app.project_members (id, project_id, identity, role) "
                "VALUES (%s, %s, %s, 'owner')",
                (project_member_id, project_id, "fixture-subject"),
            )
            cursor.execute(
                """
                INSERT INTO app.operations
                    (id, effective_org_id, command_type, actor, resource_path,
                     host_context, versions, request_hash, confirmation_mode,
                     idempotency_key_hash, state)
                VALUES (%s, %s, 'invitation.issue', %s, '[]'::jsonb,
                        '{}'::jsonb, '{}'::jsonb, %s, 'none', %s, 'succeeded')
                """,
                (operation_id, org_id, "fixture-subject", "a" * 64, "b" * 64),
            )
            cursor.execute(
                """
                INSERT INTO app.invitations
                    (id, invited_identity_hash, org_id, invited_role, grant_bindings,
                     issuer, policy_version, bearer_hash, operation_id, expires_at)
                VALUES (%s, %s, %s, 'member', '[]'::jsonb, %s, 'fixture-v1',
                        %s, %s, now() + interval '1 day')
                """,
                (invitation_id, "c" * 64, org_id, "fixture-subject", "d" * 64, operation_id),
            )
    return fixture


def _assert_fixture(conn: psycopg.Connection, fixture: tuple[str, ...]) -> None:
    org_id, org_member_id, project_id, project_member_id, operation_id, invitation_id = fixture
    checks = (
        ("organizations", org_id),
        ("org_members", org_member_id),
        ("projects", project_id),
        ("project_members", project_member_id),
        ("operations", operation_id),
        ("invitations", invitation_id),
    )
    with conn.cursor() as cursor:
        for table, identifier in checks:
            cursor.execute(
                sql.SQL("SELECT count(*) FROM app.{} WHERE id = %s").format(sql.Identifier(table)),
                (identifier,),
            )
            if cursor.fetchone()[0] != 1:
                raise RuntimeError(
                    f"upgrade fixture row changed or missing: app.{table}/{identifier}"
                )
        cursor.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
        if cursor.fetchone()[0] != org_id:
            raise RuntimeError("upgrade fixture project changed organization")
        cursor.execute("SELECT org_id FROM app.invitations WHERE id = %s", (invitation_id,))
        if cursor.fetchone()[0] != org_id:
            raise RuntimeError("upgrade fixture invitation changed organization")


def main() -> int:
    admin_dsn = os.getenv("PLATFORM_DB_URL") or os.getenv("PLATFORM_DATABASE_URL")
    if not admin_dsn:
        print("upgrade verification requires PLATFORM_DB_URL", file=sys.stderr)
        return 2
    database = f"connector_upgrade_{uuid.uuid4().hex[:10]}"
    migrations = load_migrations(Path("infra/nango/migrations"), verify_manifest=True)
    upgrade_dsn = _upgrade_dsn(admin_dsn, database)

    with psycopg.connect(admin_dsn, autocommit=True, connect_timeout=10) as admin:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        try:
            with psycopg.connect(upgrade_dsn, autocommit=True, connect_timeout=10) as conn:
                applied, _ = apply_migrations(conn, migrations, target=106)
                if applied != list(range(1, 107)):
                    raise RuntimeError("upgrade fixture did not apply exactly migrations 001..106")
                fixture = _seed_fixture(conn)
                applied, skipped = apply_migrations(conn, migrations)
                if applied != [107, 108, 109, 110, 111, 112, 113, 114, 115] or len(skipped) != 106:
                    raise RuntimeError("upgrade fixture did not apply exactly migrations 107..115")
                _assert_fixture(conn, fixture)
                applied, skipped = apply_migrations(conn, migrations)
                if applied or len(skipped) != 115:
                    raise RuntimeError("second migration run was not a complete no-op")
        finally:
            with admin.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(database)
                    )
                )
    print("migration upgrade fixture OK: 106 -> 115, tenant rows preserved, rerun no-op")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
