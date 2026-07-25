"""Tests for Story 34.1 -- org plan & entitlements foundation (Epic 34).

Five test groups matching the five AC groups in 34-1-org-plan-entitlements.md:

  Group 1 (pg-gated): migration 056 schema + triggers append-only on org_plan_history.
  Group 2 (offline):  get_org_plan returns derived trial default when no row exists.
  Group 3 (offline):  resolve_entitlements returns None limits for full/internal.
  Group 4 (pg-gated): set_org_plan transactional -- upsert + history in same transaction,
                       invalid plan / unknown org_id rejected without partial state.
  Group 5 (pg-gated): migration idempotence (re-apply does not fail).

Offline tests mock _fetch_org_plan_row so pure logic is testable without Postgres.
pg-gated tests are skipped when TEST_POSTGRES_DSN is unset (same pattern as
test_metric_semantics.py / test_dataset_access_grants.py).

AD-14: identity subjects are opaque TEXT strings throughout.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

# Suppress background workers so importing the server does not start polling loops.
os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import org_entitlements as oe  # noqa: E402

# ---------------------------------------------------------------------------
# Postgres availability check (calqued on test_metric_semantics.py)
# ---------------------------------------------------------------------------


def _pg_reachable() -> bool:
    if not os.environ.get("TEST_POSTGRES_DSN"):
        return False
    try:
        import psycopg  # noqa: PLC0415

        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


pg_available = pytest.mark.skipif(not _pg_reachable(), reason="platform Postgres not reachable")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MIGRATION_056 = (
    _REPO_ROOT / "infra" / "nango" / "migrations" / "056_org_plan_entitlements.sql"
)


# ---------------------------------------------------------------------------
# Helper: apply migration 056 on the test DB (idempotent).
# ---------------------------------------------------------------------------


def _apply_migration(conn) -> None:
    sql = _MIGRATION_056.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def _create_test_org(conn, org_id: str) -> None:
    """Insert a minimal organization row for FK-safe tests."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.organizations (id, name, slug, status, created_by)
            VALUES (%s, %s, %s, 'active', 'test')
            ON CONFLICT (id) DO NOTHING
            """,
            (org_id, f"Test Org {org_id}", f"slug-{org_id}"),
        )
    conn.commit()


def _cleanup_org(conn, org_id: str) -> None:
    """Remove test org and its plan rows (CASCADE handles plan + history)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM app.organizations WHERE id = %s", (org_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# GROUP 1: Migration schema + append-only triggers (pg-gated)
# ---------------------------------------------------------------------------


@pg_available
def test_migration_056_tables_exist():
    """AC1: migration 056 creates app.org_plan and app.org_plan_history."""
    import psycopg  # noqa: PLC0415

    with psycopg.connect(os.environ["TEST_POSTGRES_DSN"]) as conn:
        _apply_migration(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'app'
                  AND table_name IN ('org_plan', 'org_plan_history')
                ORDER BY table_name
                """
            )
            found = {row[0] for row in cur.fetchall()}
        assert "org_plan" in found, "app.org_plan missing after migration 056"
        assert "org_plan_history" in found, "app.org_plan_history missing after migration 056"


@pg_available
def test_migration_056_org_plan_columns():
    """AC1: app.org_plan has the required columns with correct types/defaults."""
    import psycopg  # noqa: PLC0415

    with psycopg.connect(os.environ["TEST_POSTGRES_DSN"]) as conn:
        _apply_migration(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name, data_type, column_default, is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'app' AND table_name = 'org_plan'
                ORDER BY ordinal_position
                """
            )
            cols = {row[0]: {"type": row[1], "default": row[2], "nullable": row[3]}
                    for row in cur.fetchall()}

        assert "org_id" in cols
        assert "plan" in cols
        assert "entitlements" in cols
        assert "granted_by" in cols
        assert "granted_at" in cols
        assert "updated_at" in cols

        # plan has DEFAULT 'trial'
        assert cols["plan"]["default"] is not None and "trial" in cols["plan"]["default"]
        # granted_by is nullable
        assert cols["granted_by"]["nullable"] == "YES"


@pg_available
def test_org_plan_history_append_only_update_blocked():
    """AC1: UPDATE on app.org_plan_history raises (append-only trigger fires)."""
    import psycopg  # noqa: PLC0415

    with psycopg.connect(os.environ["TEST_POSTGRES_DSN"]) as conn:
        _apply_migration(conn)
        org_id = f"org_test_{uuid.uuid4().hex[:8]}"
        _create_test_org(conn, org_id)
        try:
            # Insert a history row directly.
            history_id = oe._new_id()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.org_plan_history (id, org_id, plan, entitlements, granted_by)
                    VALUES (%s, %s, 'trial', '{}', 'test')
                    """,
                    (history_id, org_id),
                )
            conn.commit()

            # Attempt UPDATE -- trigger must raise.
            with pytest.raises(psycopg.errors.RaiseException):
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE app.org_plan_history SET plan = 'full' WHERE id = %s",
                        (history_id,),
                    )
                conn.commit()
            conn.rollback()
        finally:
            _cleanup_org(conn, org_id)


@pg_available
def test_org_plan_history_append_only_delete_blocked():
    """AC1: DELETE on app.org_plan_history raises (append-only trigger fires)."""
    import psycopg  # noqa: PLC0415

    with psycopg.connect(os.environ["TEST_POSTGRES_DSN"]) as conn:
        _apply_migration(conn)
        org_id = f"org_test_{uuid.uuid4().hex[:8]}"
        _create_test_org(conn, org_id)
        try:
            history_id = oe._new_id()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.org_plan_history (id, org_id, plan, entitlements, granted_by)
                    VALUES (%s, %s, 'trial', '{}', 'test')
                    """,
                    (history_id, org_id),
                )
            conn.commit()

            with pytest.raises(psycopg.errors.RaiseException):
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM app.org_plan_history WHERE id = %s", (history_id,)
                    )
                conn.commit()
            conn.rollback()
        finally:
            _cleanup_org(conn, org_id)


@pg_available
def test_org_plan_history_append_only_truncate_blocked():
    """AC1: TRUNCATE on app.org_plan_history raises (statement-level trigger fires)."""
    import psycopg  # noqa: PLC0415

    with psycopg.connect(os.environ["TEST_POSTGRES_DSN"]) as conn:
        _apply_migration(conn)
        with pytest.raises(psycopg.errors.RaiseException):
            with conn.cursor() as cur:
                cur.execute("TRUNCATE app.org_plan_history")
            conn.commit()
        conn.rollback()


# ---------------------------------------------------------------------------
# GROUP 2: Derived trial default -- offline (mocked _fetch_org_plan_row)
# ---------------------------------------------------------------------------


def test_get_org_plan_returns_trial_default_when_no_row():
    """AC2: get_org_plan returns derived trial default (no Postgres needed)."""
    with patch.object(oe, "_fetch_org_plan_row", return_value=None):
        result = oe.get_org_plan("org_abc123")

    assert result["org_id"] == "org_abc123"
    assert result["plan"] == oe.PLAN_TRIAL
    assert result["entitlements"] == oe.DEFAULT_TRIAL_ENTITLEMENTS
    assert result["granted_by"] is None
    assert result["granted_at"] is None


def test_get_org_plan_trial_entitlements_are_correct_values():
    """AC2: DEFAULT_TRIAL_ENTITLEMENTS has max_backfill_days=30 and max_datastreams=3."""
    assert oe.DEFAULT_TRIAL_ENTITLEMENTS["max_backfill_days"] == 30
    assert oe.DEFAULT_TRIAL_ENTITLEMENTS["max_datastreams"] == 3


def test_resolve_entitlements_trial_default_bounded():
    """AC2: resolve_entitlements on a no-row org returns bounded trial limits."""
    with patch.object(oe, "_fetch_org_plan_row", return_value=None):
        limits = oe.resolve_entitlements("org_new")

    assert limits["max_backfill_days"] == 30
    assert limits["max_datastreams"] == 3


def test_get_org_plan_trial_row_explicit():
    """AC2: get_org_plan with an explicit trial row returns those values (not None)."""
    import datetime  # noqa: PLC0415

    fake_row = {
        "plan": "trial",
        "entitlements": {"max_backfill_days": 30, "max_datastreams": 3},
        "granted_by": "admin_user",
        "granted_at": datetime.datetime(2026, 1, 1),
        "updated_at": None,
    }
    with patch.object(oe, "_fetch_org_plan_row", return_value=fake_row):
        result = oe.get_org_plan("org_xyz")

    assert result["plan"] == "trial"
    assert result["entitlements"]["max_backfill_days"] == 30
    assert result["granted_by"] == "admin_user"


# ---------------------------------------------------------------------------
# GROUP 3: Full / internal => unlimited (offline, mocked)
# ---------------------------------------------------------------------------


def test_resolve_entitlements_full_is_unlimited():
    """AC3: resolve_entitlements for full plan returns None for all limits."""
    fake_row = {
        "plan": "full",
        "entitlements": {},
        "granted_by": "super_admin",
        "granted_at": None,
        "updated_at": None,
    }
    with patch.object(oe, "_fetch_org_plan_row", return_value=fake_row):
        limits = oe.resolve_entitlements("org_paid")

    assert limits["max_backfill_days"] is None
    assert limits["max_datastreams"] is None


def test_resolve_entitlements_internal_is_unlimited():
    """AC3: resolve_entitlements for internal plan returns None for all limits."""
    fake_row = {
        "plan": "internal",
        "entitlements": {},
        "granted_by": "system",
        "granted_at": None,
        "updated_at": None,
    }
    with patch.object(oe, "_fetch_org_plan_row", return_value=fake_row):
        limits = oe.resolve_entitlements("org_internal")

    assert limits["max_backfill_days"] is None
    assert limits["max_datastreams"] is None


def test_resolve_entitlements_full_none_signals_no_cap():
    """AC3: None values signal no cap to enforcement guards (34.2/34.3)."""
    fake_row = {
        "plan": "full",
        "entitlements": {},
        "granted_by": None,
        "granted_at": None,
        "updated_at": None,
    }
    with patch.object(oe, "_fetch_org_plan_row", return_value=fake_row):
        limits = oe.resolve_entitlements("org_full")

    # Verify that EVERY key that DEFAULT_TRIAL_ENTITLEMENTS defines is None for full.
    for key in oe.DEFAULT_TRIAL_ENTITLEMENTS:
        assert limits[key] is None, f"Expected None for {key!r} on full plan"


# ---------------------------------------------------------------------------
# GROUP 4: set_org_plan transactional + validation (pg-gated + offline)
# ---------------------------------------------------------------------------


def test_set_org_plan_invalid_plan_raises_before_db():
    """AC4: set_org_plan with an invalid plan raises ValueError without touching DB."""
    # No DB call should happen; if _write_org_plan is called, the test would need Postgres.
    with patch.object(oe, "_write_org_plan") as mock_write:
        with pytest.raises(ValueError, match="Invalid plan"):
            oe.set_org_plan("org_x", "premium", {}, "admin")
        mock_write.assert_not_called()


def test_set_org_plan_valid_plans_accepted_offline():
    """AC4 (offline): all three valid plans pass the guard (DB call is mocked)."""
    for plan in (oe.PLAN_TRIAL, oe.PLAN_FULL, oe.PLAN_INTERNAL):
        with patch.object(oe, "_write_org_plan") as mock_write:
            oe.set_org_plan("org_x", plan, {}, "admin")
            mock_write.assert_called_once_with("org_x", plan, {}, "admin")


@pg_available
def test_set_org_plan_upsert_and_history_written():
    """AC4 (pg-gated): set_org_plan writes org_plan + one history row in same transaction."""
    import psycopg  # noqa: PLC0415

    with psycopg.connect(os.environ["TEST_POSTGRES_DSN"]) as conn:
        _apply_migration(conn)
        org_id = f"org_test_{uuid.uuid4().hex[:8]}"
        _create_test_org(conn, org_id)
        try:
            # Patch get_connection so it uses our test connection's DSN.
            # Easier: set TEST_POSTGRES_DSN as PLATFORM_DB_URL for the call.
            original_url = os.environ.get("PLATFORM_DB_URL")
            os.environ["PLATFORM_DB_URL"] = os.environ["TEST_POSTGRES_DSN"]
            try:
                oe.set_org_plan(
                    org_id,
                    "full",
                    {"max_backfill_days": None, "max_datastreams": None},
                    "super_admin_test",
                )
            finally:
                if original_url is None:
                    os.environ.pop("PLATFORM_DB_URL", None)
                else:
                    os.environ["PLATFORM_DB_URL"] = original_url

            # Verify org_plan row.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT plan, granted_by FROM app.org_plan WHERE org_id = %s", (org_id,)
                )
                row = cur.fetchone()
            assert row is not None, "org_plan row not written"
            assert row[0] == "full"
            assert row[1] == "super_admin_test"

            # Verify history row exists.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM app.org_plan_history WHERE org_id = %s", (org_id,)
                )
                count = cur.fetchone()[0]
            assert count == 1, f"Expected 1 history row, got {count}"

        finally:
            _cleanup_org(conn, org_id)


@pg_available
def test_set_org_plan_upsert_writes_second_history_row():
    """AC4 (pg-gated): calling set_org_plan twice writes 2 history rows (audit trail)."""
    import psycopg  # noqa: PLC0415

    with psycopg.connect(os.environ["TEST_POSTGRES_DSN"]) as conn:
        _apply_migration(conn)
        org_id = f"org_test_{uuid.uuid4().hex[:8]}"
        _create_test_org(conn, org_id)
        try:
            original_url = os.environ.get("PLATFORM_DB_URL")
            os.environ["PLATFORM_DB_URL"] = os.environ["TEST_POSTGRES_DSN"]
            try:
                oe.set_org_plan(org_id, "trial", oe.DEFAULT_TRIAL_ENTITLEMENTS, "admin1")
                oe.set_org_plan(org_id, "full", {}, "admin2")
            finally:
                if original_url is None:
                    os.environ.pop("PLATFORM_DB_URL", None)
                else:
                    os.environ["PLATFORM_DB_URL"] = original_url

            # org_plan should reflect the latest plan.
            with conn.cursor() as cur:
                cur.execute("SELECT plan FROM app.org_plan WHERE org_id = %s", (org_id,))
                plan_row = cur.fetchone()
            assert plan_row[0] == "full"

            # Two history rows.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT plan FROM app.org_plan_history WHERE org_id = %s ORDER BY at",
                    (org_id,),
                )
                history = [r[0] for r in cur.fetchall()]
            assert history == ["trial", "full"], f"Unexpected history: {history}"

        finally:
            _cleanup_org(conn, org_id)


@pg_available
def test_set_org_plan_unknown_org_id_raises():
    """AC4 (pg-gated): set_org_plan rejects unknown org_id without partial DB state."""
    import psycopg  # noqa: PLC0415

    with psycopg.connect(os.environ["TEST_POSTGRES_DSN"]) as conn:
        _apply_migration(conn)

    nonexistent_org = f"org_ghost_{uuid.uuid4().hex}"
    original_url = os.environ.get("PLATFORM_DB_URL")
    os.environ["PLATFORM_DB_URL"] = os.environ["TEST_POSTGRES_DSN"]
    try:
        with pytest.raises(ValueError, match="org_id not found"):
            oe.set_org_plan(nonexistent_org, "full", {}, "admin")
    finally:
        if original_url is None:
            os.environ.pop("PLATFORM_DB_URL", None)
        else:
            os.environ["PLATFORM_DB_URL"] = original_url

    # Verify nothing was written (re-use the psycopg import from above).
    with psycopg.connect(os.environ.get("TEST_POSTGRES_DSN", "")) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM app.org_plan_history WHERE org_id = %s",
                (nonexistent_org,),
            )
            count = cur.fetchone()[0]
    assert count == 0, f"History row was written for nonexistent org: {count}"


# ---------------------------------------------------------------------------
# GROUP 5: Migration idempotence (pg-gated)
# ---------------------------------------------------------------------------


@pg_available
def test_migration_056_idempotent():
    """AC5: applying migration 056 twice does not raise (IF NOT EXISTS everywhere)."""
    import psycopg  # noqa: PLC0415

    with psycopg.connect(os.environ["TEST_POSTGRES_DSN"]) as conn:
        _apply_migration(conn)
        # Second apply must be a no-op (idempotent).
        _apply_migration(conn)


@pg_available
def test_migration_056_plan_check_constraint():
    """AC5: plan column rejects values outside the enum (DB-level CHECK)."""
    import psycopg  # noqa: PLC0415

    with psycopg.connect(os.environ["TEST_POSTGRES_DSN"]) as conn:
        _apply_migration(conn)
        org_id = f"org_test_{uuid.uuid4().hex[:8]}"
        _create_test_org(conn, org_id)
        try:
            with pytest.raises(psycopg.errors.CheckViolation):
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO app.org_plan (org_id, plan, entitlements)
                        VALUES (%s, 'premium', '{}')
                        """,
                        (org_id,),
                    )
                conn.commit()
            conn.rollback()
        finally:
            _cleanup_org(conn, org_id)
