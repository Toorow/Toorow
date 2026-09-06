"""Story 50.6 (AC13) -- `app.result_app_grants` invariants, against a real PostgreSQL.

WHY THESE CANNOT BE UNIT TESTS. Every property below is a composite foreign key, a
CHECK, a trigger or a row-security policy. A mocked cursor accepts all of them
happily -- which is exactly how a schema promise becomes a comment.

WHY THE CONNECTION MATTERS AS MUCH AS THE ASSERTION. A table owner bypasses RLS
unless the table FORCEs it, and a superuser bypasses it outright: an isolation
test run on such a connection passes vacuously. Migration 161 declares both
`ENABLE` and `FORCE`, and this suite asserts the isolation is real by checking the
policy actually hides a foreign row when the Epic-36 floor is armed.

Every test rolls back. Nothing is left behind, even on a disposable database.
"""

from __future__ import annotations

import pytest
from core.result_app_grants import GrantRefused, issue_handle, touch_handle

pytestmark = pytest.mark.usefixtures("live_postgres")

_HASH_A = "a" * 64
_HASH_B = "b" * 64


def _seed_result(conn, suffix: str = "G"):
    """Create one Query Spec version, attempt, Result and payload inside the caller's tx.

    Binds to whatever published Semantic View version the database already has,
    because the composite foreign keys refuse anything else -- which is itself part
    of what these tests prove.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT v.id, v.view_id, v.project_id, p.org_id
            FROM app.semantic_view_versions v
            JOIN app.projects p ON p.id = v.project_id
            LIMIT 1
            """
        )
        row = cur.fetchone()
        if row is None:
            pytest.skip("no Semantic View version in this database to bind a Query Spec to")
        version_id, view_id, project_id, org_id = row

        cur.execute(
            """
            INSERT INTO app.query_specs (id, org_id, project_id, semantic_view_id, created_by)
            VALUES (%s, %s, %s, %s, 'test')
            """,
            (f"qs_{suffix}", org_id, project_id, view_id),
        )
        cur.execute(
            """
            INSERT INTO app.query_spec_versions
                (id, query_spec_id, org_id, project_id, version_number, semantic_view_id,
                 semantic_view_version_id, spec, content_hash, created_by)
            VALUES (%s, %s, %s, %s, 1, %s, %s, '{}'::jsonb, %s, 'test')
            """,
            (f"qsv_{suffix}", f"qs_{suffix}", org_id, project_id, view_id, version_id, _HASH_A),
        )
        cur.execute(
            """
            INSERT INTO app.query_execution_attempts
                (id, org_id, project_id, query_spec_version_id, result_id, requested_by)
            VALUES (%s, %s, %s, %s, %s, 'test')
            """,
            (f"qea_{suffix}", org_id, project_id, f"qsv_{suffix}", f"qr_{suffix}"),
        )
        cur.execute(
            """
            INSERT INTO app.query_results
                (id, org_id, project_id, attempt_id, query_spec_version_id, outcome,
                 ai_path_absent_literal, content_hash, row_count, started_at, ended_at)
            VALUES (%s, %s, %s, %s, %s, 'success', 'No AI path', %s, 3, NOW(), NOW())
            """,
            (
                f"qr_{suffix}",
                org_id,
                project_id,
                f"qea_{suffix}",
                f"qsv_{suffix}",
                _HASH_B,
            ),
        )
        cur.execute(
            """
            INSERT INTO app.query_result_payloads
                (result_id, org_id, project_id, content_hash, result_schema, manifest, rows_chunk)
            VALUES (%s, %s, %s, %s,
                    '{"fields": [{"name": "day"}, {"name": "clicks"}]}'::jsonb,
                    '{}'::jsonb,
                    '[{"day": "2026-07-01", "clicks": 1}]'::jsonb)
            """,
            (f"qr_{suffix}", org_id, project_id, _HASH_B),
        )
    return org_id, project_id


def _handle(n: int = 0) -> str:
    """A syntactically valid handle: `rh_` + 26 Crockford base32 characters."""
    return "rh_" + f"{n:026d}".replace("8", "A").replace("9", "B")


def _insert_grant(conn, org_id, project_id, *, suffix="G", handle=None, **overrides):
    values = {
        "handle_id": handle or _handle(),
        "org_id": org_id,
        "project_id": project_id,
        "result_id": f"qr_{suffix}",
        "content_hash": _HASH_B,
        "issued_to_identity": "owner@example.com",
        "allowed_columns": ["day", "clicks"],
    }
    values.update(overrides)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.result_app_grants
                (handle_id, org_id, project_id, result_id, content_hash,
                 issued_to_identity, allowed_columns, expires_at)
            VALUES (%(handle_id)s, %(org_id)s, %(project_id)s, %(result_id)s,
                    %(content_hash)s, %(issued_to_identity)s, %(allowed_columns)s,
                    NOW() + INTERVAL '8 hours')
            """,
            values,
        )
    return values["handle_id"]


# ---------------------------------------------------------------------------
# The composite, Project-scoped foreign key.
# ---------------------------------------------------------------------------


def test_a_grant_cannot_point_at_another_projects_result(live_postgres):
    """The scope is in the KEY, not only in the WHERE clause a query might forget."""
    org_id, project_id = _seed_result(live_postgres)
    with live_postgres.cursor() as cur, pytest.raises(Exception):
        cur.execute(
            """
            INSERT INTO app.result_app_grants
                (handle_id, org_id, project_id, result_id, content_hash,
                 issued_to_identity, allowed_columns, expires_at)
            VALUES (%s, %s, 'proj_OTHER', 'qr_G', %s, 'owner@example.com',
                    ARRAY['day'], NOW() + INTERVAL '1 hour')
            """,
            (_handle(1), org_id, _HASH_B),
        )
    live_postgres.rollback()


def test_a_grant_cannot_point_at_a_result_that_does_not_exist(live_postgres):
    org_id, project_id = _seed_result(live_postgres)
    with live_postgres.cursor() as cur, pytest.raises(Exception):
        cur.execute(
            """
            INSERT INTO app.result_app_grants
                (handle_id, org_id, project_id, result_id, content_hash,
                 issued_to_identity, allowed_columns, expires_at)
            VALUES (%s, %s, %s, 'qr_NOPE', %s, 'owner@example.com',
                    ARRAY['day'], NOW() + INTERVAL '1 hour')
            """,
            (_handle(2), org_id, project_id, _HASH_B),
        )
    live_postgres.rollback()


# ---------------------------------------------------------------------------
# The shape CHECKs.
# ---------------------------------------------------------------------------


def test_a_handle_that_is_not_an_opaque_rh_ulid_is_refused(live_postgres):
    org_id, project_id = _seed_result(live_postgres)
    for bad in ("token_abc", "rh_short", "rh_" + "z" * 26, "proj_EXAMPLE"):
        with live_postgres.cursor() as cur, pytest.raises(Exception):
            _insert_grant_raw(cur, org_id, project_id, bad)
        live_postgres.rollback()
        _seed_result(live_postgres)


def _insert_grant_raw(cur, org_id, project_id, handle):
    cur.execute(
        """
        INSERT INTO app.result_app_grants
            (handle_id, org_id, project_id, result_id, content_hash,
             issued_to_identity, allowed_columns, expires_at)
        VALUES (%s, %s, %s, 'qr_G', %s, 'owner@example.com',
                ARRAY['day'], NOW() + INTERVAL '1 hour')
        """,
        (handle, org_id, project_id, _HASH_B),
    )


def test_a_grant_with_no_allowed_column_is_refused(live_postgres):
    """An empty allowlist is not a narrower grant, it is a broken one."""
    org_id, project_id = _seed_result(live_postgres)
    with live_postgres.cursor() as cur, pytest.raises(Exception):
        cur.execute(
            """
            INSERT INTO app.result_app_grants
                (handle_id, org_id, project_id, result_id, content_hash,
                 issued_to_identity, allowed_columns, expires_at)
            VALUES (%s, %s, %s, 'qr_G', %s, 'owner@example.com',
                    ARRAY[]::TEXT[], NOW() + INTERVAL '1 hour')
            """,
            (_handle(3), org_id, project_id, _HASH_B),
        )
    live_postgres.rollback()


def test_a_grant_cannot_expire_before_it_was_issued(live_postgres):
    org_id, project_id = _seed_result(live_postgres)
    with live_postgres.cursor() as cur, pytest.raises(Exception):
        cur.execute(
            """
            INSERT INTO app.result_app_grants
                (handle_id, org_id, project_id, result_id, content_hash,
                 issued_to_identity, allowed_columns, issued_at, expires_at)
            VALUES (%s, %s, %s, 'qr_G', %s, 'owner@example.com', ARRAY['day'],
                    NOW(), NOW() - INTERVAL '1 hour')
            """,
            (_handle(4), org_id, project_id, _HASH_B),
        )
    live_postgres.rollback()


# ---------------------------------------------------------------------------
# Revocation is the ONLY legitimate mutation.
# ---------------------------------------------------------------------------


def test_revocation_is_allowed(live_postgres):
    org_id, project_id = _seed_result(live_postgres)
    handle = _insert_grant(live_postgres, org_id, project_id)
    with live_postgres.cursor() as cur:
        cur.execute(
            "UPDATE app.result_app_grants SET revoked_at = NOW() WHERE handle_id = %s",
            (handle,),
        )
        assert cur.rowcount == 1
    live_postgres.rollback()


def test_the_read_timestamp_may_advance(live_postgres):
    """The idle bound needs `last_read_at` to move; nothing else may."""
    org_id, project_id = _seed_result(live_postgres)
    handle = _insert_grant(live_postgres, org_id, project_id)
    with live_postgres.cursor() as cur:
        cur.execute(
            "UPDATE app.result_app_grants SET last_read_at = NOW() WHERE handle_id = %s",
            (handle,),
        )
        assert cur.rowcount == 1
    live_postgres.rollback()


def test_repeated_issue_reuses_one_live_exact_grant(live_postgres):
    org_id, project_id = _seed_result(live_postgres)
    kwargs = {
        "org_id": org_id,
        "project_id": project_id,
        "result_id": "qr_G",
        "identity": "owner@example.com",
        "content_hash": _HASH_B,
        "result_schema": {"fields": [{"name": "day"}, {"name": "clicks"}]},
    }
    first = issue_handle(live_postgres, **kwargs)
    second = issue_handle(live_postgres, **kwargs)
    assert second == first
    with live_postgres.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM app.result_app_grants
            WHERE org_id = %s AND project_id = %s AND result_id = 'qr_G'
              AND issued_to_identity = 'owner@example.com' AND revoked_at IS NULL
            """,
            (org_id, project_id),
        )
        assert cur.fetchone()[0] == 1
    live_postgres.rollback()


def test_issue_refuses_a_schema_wider_than_the_read_contract(live_postgres):
    org_id, project_id = _seed_result(live_postgres)
    schema = {"fields": [{"name": f"column_{index}"} for index in range(101)]}
    with pytest.raises(GrantRefused) as excinfo:
        issue_handle(
            live_postgres,
            org_id=org_id,
            project_id=project_id,
            result_id="qr_G",
            identity="owner@example.com",
            content_hash=_HASH_B,
            result_schema=schema,
        )
    assert excinfo.value.code == "result_schema_too_wide"
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.result_app_grants WHERE result_id = 'qr_G'",
        )
        assert cur.fetchone()[0] == 0
    live_postgres.rollback()


def test_touch_does_not_revive_a_revoked_grant(live_postgres):
    org_id, project_id = _seed_result(live_postgres)
    handle = _insert_grant(live_postgres, org_id, project_id)
    with live_postgres.cursor() as cur:
        cur.execute(
            "UPDATE app.result_app_grants SET revoked_at = NOW() WHERE handle_id = %s",
            (handle,),
        )
    assert touch_handle(live_postgres, handle_id=handle) is False
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT last_read_at FROM app.result_app_grants WHERE handle_id = %s",
            (handle,),
        )
        assert cur.fetchone()[0] is None
    live_postgres.rollback()


def test_touch_does_not_revive_an_expired_grant(live_postgres):
    org_id, project_id = _seed_result(live_postgres)
    handle = _handle(8)
    with live_postgres.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.result_app_grants
                (handle_id, org_id, project_id, result_id, content_hash,
                 issued_to_identity, allowed_columns, issued_at, expires_at)
            VALUES (%s, %s, %s, 'qr_G', %s, 'owner@example.com', ARRAY['day'],
                    NOW() - INTERVAL '2 hours', NOW() - INTERVAL '1 hour')
            """,
            (handle, org_id, project_id, _HASH_B),
        )
    assert touch_handle(live_postgres, handle_id=handle) is False
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT last_read_at FROM app.result_app_grants WHERE handle_id = %s",
            (handle,),
        )
        assert cur.fetchone()[0] is None
    live_postgres.rollback()


def test_the_scope_of_a_grant_cannot_be_widened_after_issue(live_postgres):
    """The whole "the grant can only narrow" claim depends on this."""
    org_id, project_id = _seed_result(live_postgres)
    handle = _insert_grant(live_postgres, org_id, project_id, allowed_columns=["day"])
    for column, value in (
        ("allowed_columns", "ARRAY['day','clicks','cost']"),
        ("expires_at", "NOW() + INTERVAL '10 years'"),
        ("issued_to_identity", "'someone-else@example.com'"),
        ("result_id", "'qr_G2'"),
        ("content_hash", "'" + "c" * 64 + "'"),
    ):
        with live_postgres.cursor() as cur, pytest.raises(Exception):
            cur.execute(
                f"UPDATE app.result_app_grants SET {column} = {value} WHERE handle_id = %s",
                (handle,),
            )
        live_postgres.rollback()
        _seed_result(live_postgres)
        handle = _insert_grant(live_postgres, org_id, project_id, allowed_columns=["day"])
    live_postgres.rollback()


def test_a_revoked_grant_cannot_be_reinstated(live_postgres):
    """Otherwise revocation is a suggestion."""
    org_id, project_id = _seed_result(live_postgres)
    handle = _insert_grant(live_postgres, org_id, project_id)
    with live_postgres.cursor() as cur:
        cur.execute(
            "UPDATE app.result_app_grants SET revoked_at = NOW() WHERE handle_id = %s",
            (handle,),
        )
    with live_postgres.cursor() as cur, pytest.raises(Exception):
        cur.execute(
            "UPDATE app.result_app_grants SET revoked_at = NULL WHERE handle_id = %s",
            (handle,),
        )
    live_postgres.rollback()


def test_a_grant_cannot_be_deleted(live_postgres):
    org_id, project_id = _seed_result(live_postgres)
    handle = _insert_grant(live_postgres, org_id, project_id)
    with live_postgres.cursor() as cur, pytest.raises(Exception):
        cur.execute("DELETE FROM app.result_app_grants WHERE handle_id = %s", (handle,))
    live_postgres.rollback()


# ---------------------------------------------------------------------------
# Row Level Security -- and the proof that the connection cannot bypass it.
# ---------------------------------------------------------------------------


def test_row_security_is_enabled_and_forced(live_postgres):
    with live_postgres.cursor() as cur:
        cur.execute(
            """
            SELECT relrowsecurity, relforcerowsecurity
            FROM pg_class WHERE oid = 'app.result_app_grants'::regclass
            """
        )
        enabled, forced = cur.fetchone()
    # FORCE is what stops a table owner's connection from making every isolation
    # assertion below pass vacuously.
    assert enabled is True
    assert forced is True
    live_postgres.rollback()


def test_the_application_role_is_not_a_superuser_so_these_assertions_mean_something(
    live_postgres,
):
    with live_postgres.cursor() as cur:
        cur.execute("SELECT usesuper, usebypassrls FROM pg_user WHERE usename = current_user")
        row = cur.fetchone()
    if row is None:
        pytest.skip("current_user is not a login role in pg_user")
    usesuper, usebypassrls = row
    assert usesuper is False, "a superuser connection makes every RLS assertion vacuous"
    assert usebypassrls is False, "BYPASSRLS makes every RLS assertion vacuous"
    live_postgres.rollback()


def test_the_policy_hides_a_foreign_grant_when_the_epic36_floor_is_armed(live_postgres):
    """With the floor armed and no membership, the grant is INVISIBLE, not merely refused."""
    org_id, project_id = _seed_result(live_postgres)
    handle = _insert_grant(live_postgres, org_id, project_id)
    with live_postgres.cursor() as cur:
        cur.execute("SELECT count(*) FROM app.result_app_grants WHERE handle_id = %s", (handle,))
        assert cur.fetchone()[0] == 1, "the row must exist before the floor is armed"
        # Arm the Epic-36 floor with no access context: the policy predicate
        # becomes false and the row disappears rather than erroring.
        cur.execute("SELECT set_config('toorow.enforce_epic36', 'on', true)")
        cur.execute("SELECT set_config('app.access_org_ids', '', true)")
        cur.execute("SELECT set_config('app.access_resource_paths', '', true)")
        cur.execute("SELECT count(*) FROM app.result_app_grants WHERE handle_id = %s", (handle,))
        assert cur.fetchone()[0] == 0, "RLS did not hide the grant with the floor armed"
    live_postgres.rollback()


def test_a_grant_cannot_be_inserted_for_a_project_the_floor_denies(live_postgres):
    """WITH CHECK closes the write side; a policy that only filters reads is half a policy."""
    org_id, project_id = _seed_result(live_postgres)
    with live_postgres.cursor() as cur:
        cur.execute("SELECT set_config('toorow.enforce_epic36', 'on', true)")
        cur.execute("SELECT set_config('app.access_org_ids', '', true)")
        cur.execute("SELECT set_config('app.access_resource_paths', '', true)")
        with pytest.raises(Exception):
            _insert_grant_raw(cur, org_id, project_id, _handle(7))
    live_postgres.rollback()
