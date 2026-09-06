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


def _ddl_dsn() -> str:
    """The DSN a DDL fixture must use, or an honest skip.

    Every test in this file applies migration 056 and then inspects what it
    created. That is DDL, and DDL needs the SCHEMA OWNER: connected as the
    application role, `_apply_migration` dies with
    `InsufficientPrivilege: must be owner of table org_plan` -- ten failures that
    say nothing about migration 056 and everything about which role was dialled.

    `conftest.py:535` already carries this rule for tests that take the
    `live_postgres` fixture, and skips them with a message naming the role. This
    file opens its own connections, so the marker never fired and the tests failed
    instead of skipping. `scripts/disposable_postgres.py env` exposes
    `TEST_POSTGRES_OWNER_DSN` for exactly this -- nothing consumed it until now.

    Prefer the owner DSN; otherwise run only if the app role happens to own the
    schema, and skip saying so if it does not. A skip is the honest outcome: the
    test was not run, and that is not the same as passing.
    """
    import psycopg  # noqa: PLC0415

    owner = os.environ.get("TEST_POSTGRES_OWNER_DSN")
    if owner:
        return owner
    dsn = os.environ["TEST_POSTGRES_DSN"]
    with psycopg.connect(dsn, connect_timeout=2) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT current_user, pg_has_role(current_user, "
                "  (SELECT tableowner FROM pg_tables "
                "    WHERE schemaname='app' AND tablename='organizations'), 'MEMBER')"
            )
            role, owns = cur.fetchone()
    if not owns:
        pytest.skip(
            f"needs an owning role for its DDL fixture; connected as {role!r}. "
            "Set TEST_POSTGRES_OWNER_DSN (disposable_postgres.py env exports it) "
            "or point TEST_POSTGRES_DSN at the schema owner for this file."
        )
    return dsn


pg_available = pytest.mark.skipif(not _pg_reachable(), reason="platform Postgres not reachable")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MIGRATION_056 = (
    _REPO_ROOT / "infra" / "nango" / "migrations" / "056_org_plan_entitlements.sql"
)


# ---------------------------------------------------------------------------
# Helper: apply migration 056 on the test DB (idempotent).
# ---------------------------------------------------------------------------


def _apply_migration(conn) -> None:
    """Ensure migration 056's objects exist -- WITHOUT replaying it over a
    database that has moved past it.

    This re-executed `056_org_plan_entitlements.sql` at the top of every test.
    On an empty database that is harmless; on a migrated one it is a **silent
    downgrade**. 056 contains `CREATE OR REPLACE FUNCTION
    app.org_plan_history_block_mutation`, and migration 098 later rewrote that
    same function to honour `app.rgpd_erasure` -- the hatch `core/org_purge.py`
    relies on to erase a tenant. Replaying 056 put the pre-098 body back, so the
    org teardown was refused by a trigger the repository had already fixed, and
    four tests failed on a defect they had themselves re-introduced seconds
    earlier.

    A migration is not idempotent with respect to HISTORY: re-running an early
    one on a database at head reverts every later edit to the objects it names.
    The ledger is the authority on what is applied (`toorow_meta.schema_migrations`),
    so this now checks rather than replays, and skips honestly if the objects are
    genuinely absent -- a database that has not been migrated is not something a
    test should repair on the fly.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass('app.org_plan'), to_regclass('app.org_plan_history')"
        )
        plan, history = cur.fetchone()
    if plan and history:
        return
    pytest.skip(
        "app.org_plan / app.org_plan_history are absent: this database has not "
        "been migrated. Run scripts/apply_migrations.py rather than replaying "
        "056 here -- replaying it reverts every later fix to the objects it names."
    )


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
    """Remove the test org and its plan rows.

    "CASCADE handles plan + history" was true of the foreign keys and false of the
    triggers: `app.org_plan_history` is APPEND-ONLY (Story 34.1), and its trigger
    refuses a DELETE whoever issues it -- including the one arriving through the
    cascade from `app.organizations`. So this teardown raised
    `RaiseException: append-only ... DELETE blocked`, in a `finally`, which then
    poisoned the transaction for the rest of the file. Four tests failed on it
    while the behaviour they assert was correct all along.

    Migration 098 gives append-only ledgers one way out, and exactly one:
    `app.rgpd_erasure`, the flag an erasure sets on itself. `purge_org_tree` uses
    it for the same reason. `SET LOCAL` scopes it to this transaction, so it
    disappears on commit AND on rollback and cannot leak into a pooled session.
    """
    from tests.conftest import purge_fixture_org  # noqa: PLC0415

    # `purge_fixture_org` walks the whole FK tree AND sets `app.rgpd_erasure`
    # itself. A hand-rolled DELETE names a handful of children out of 177 and is
    # refused by the RESTRICT ones -- `mdm_business_domains_org_id_fkey` here,
    # exactly the constraint `test_org_enforcement.py::_drop_org` documents.
    # Third file to meet this; the answer has not changed.
    purge_fixture_org(conn, org_id)
    conn.commit()


# ---------------------------------------------------------------------------
# GROUP 1: Migration schema + append-only triggers (pg-gated)
# ---------------------------------------------------------------------------


@pg_available
def test_migration_056_tables_exist():
    """AC1: migration 056 creates app.org_plan and app.org_plan_history."""
    import psycopg  # noqa: PLC0415

    with psycopg.connect(_ddl_dsn()) as conn:
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

    with psycopg.connect(_ddl_dsn()) as conn:
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

    with psycopg.connect(_ddl_dsn()) as conn:
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

    with psycopg.connect(_ddl_dsn()) as conn:
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

    with psycopg.connect(_ddl_dsn()) as conn:
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


def test_trial_org_owned_by_a_platform_admin_has_no_cap():
    """Directive Jean, 2026-08-05 : pas de quota pour l'admin plateforme.

    Le plan `internal` existait deja, mais il fallait le poser a la main sur
    chaque organisation -- donc la premiere org creee ensuite retombait sous le
    plafond d'essai, et il fallait redemander. Le plafond suit desormais le
    PROPRIETAIRE, pas une exception accordee a une organisation.
    """
    with (
        patch.object(oe, "_fetch_org_plan_row", return_value=None),
        patch.object(oe, "_owned_by_platform_admin", return_value=True),
    ):
        limits = oe.resolve_entitlements("org_owned_by_admin")

    assert limits["max_datastreams"] is None
    assert limits["max_backfill_days"] is None


def test_trial_org_owned_by_anyone_else_keeps_its_cap():
    """La portee reste etroite : sans proprietaire admin plateforme, rien ne change."""
    with (
        patch.object(oe, "_fetch_org_plan_row", return_value=None),
        patch.object(oe, "_owned_by_platform_admin", return_value=False),
    ):
        limits = oe.resolve_entitlements("org_client")

    assert limits["max_datastreams"] == 3
    assert limits["max_backfill_days"] == 30


def test_an_unreadable_owner_check_keeps_the_cap():
    """FAIL-CLOSED : une lecture ratee refuse un flux, elle n'ouvre pas le plan complet."""
    with patch.object(oe, "_fetch_org_plan_row", return_value=None):
        # `get_connection` n'est pas joignable dans ce test : la sonde leve, et
        # `_owned_by_platform_admin` doit rendre False plutot que de propager.
        assert oe._owned_by_platform_admin("org_unreadable") is False
        limits = oe.resolve_entitlements("org_unreadable")

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

    with psycopg.connect(_ddl_dsn()) as conn:
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

    with psycopg.connect(_ddl_dsn()) as conn:
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

    with psycopg.connect(_ddl_dsn()) as conn:
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

    with psycopg.connect(_ddl_dsn()) as conn:
        _apply_migration(conn)
        # Second apply must be a no-op (idempotent).
        _apply_migration(conn)


@pg_available
def test_migration_056_plan_check_constraint():
    """AC5: plan column rejects values outside the enum (DB-level CHECK)."""
    import psycopg  # noqa: PLC0415

    with psycopg.connect(_ddl_dsn()) as conn:
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
