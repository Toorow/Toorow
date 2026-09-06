from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
import apply_migrations as runner  # noqa: E402


def test_complete_catalog_has_safe_transaction_boundaries():
    migrations = runner.load_migrations(ROOT / "infra" / "nango" / "migrations")

    assert len(migrations) >= 115
    assert migrations[-1].identifier == len(migrations)
    assert all("\nBEGIN;" not in migration.body for migration in migrations)
    assert all("\nCOMMIT;" not in migration.body for migration in migrations)


def test_251_fresh_install_compatibility_prelude_is_narrow_and_transactional():
    migrations = runner.load_migrations(ROOT / "infra" / "nango" / "migrations")
    migration = migrations[250]

    prelude = runner._compatibility_prelude(migration)

    assert migration.identifier == 251
    assert prelude.strip() == (
        "ALTER TABLE app.render_share_feedback ADD COLUMN IF NOT EXISTS observed_surface TEXT;"
    )
    assert runner._compatibility_prelude(migrations[249]) == ""


def test_loader_strips_one_outer_transaction_pair(tmp_path):
    path = tmp_path / "001_example.sql"
    path.write_text("-- header\nBEGIN;\nSELECT 1;\nCOMMIT;\n", encoding="utf-8")

    migration = runner.load_migrations(tmp_path)[0]

    assert migration.body == "-- header\nSELECT 1;\n"


def test_loader_rejects_unbalanced_transaction_boundary(tmp_path):
    path = tmp_path / "001_example.sql"
    path.write_text("BEGIN;\nSELECT 1;\n", encoding="utf-8")

    with pytest.raises(
        runner.MigrationCatalogError,
        match="invalid transaction boundaries in 001_example.sql",
    ):
        runner.load_migrations(tmp_path)


def test_checksum_changes_when_sql_changes(tmp_path):
    path = tmp_path / "001_example.sql"
    path.write_text("SELECT 1;\n", encoding="utf-8")
    first = runner.load_migrations(tmp_path)[0]
    path.write_text("SELECT 2;\n", encoding="utf-8")
    second = runner.load_migrations(tmp_path)[0]

    assert first.checksum != second.checksum


def test_checksum_is_stable_across_line_endings(tmp_path):
    path = tmp_path / "001_example.sql"
    path.write_bytes(b"SELECT 1;\r\n")
    windows = runner.load_migrations(tmp_path)[0]
    path.write_bytes(b"SELECT 1;\n")
    unix = runner.load_migrations(tmp_path)[0]

    assert windows.checksum == unix.checksum


def test_failed_migration_checksum_may_be_retried(tmp_path):
    path = tmp_path / "001_example.sql"
    path.write_text("SELECT 1;\n", encoding="utf-8")
    migration = runner.load_migrations(tmp_path)[0]

    runner._validate_ledger(
        [migration],
        {1: (migration.filename, "0" * 64, "failed")},
    )


def test_ledger_drift_fails_closed(tmp_path):
    path = tmp_path / "001_example.sql"
    path.write_text("SELECT 1;\n", encoding="utf-8")
    migration = runner.load_migrations(tmp_path)[0]

    with pytest.raises(runner.MigrationApplyError, match="migration 001 checksum drift"):
        runner._validate_ledger(
            [migration],
            {1: (migration.filename, "0" * 64, "applied")},
        )


def test_ledger_hole_fails_closed(tmp_path):
    for identifier in range(1, 4):
        (tmp_path / f"{identifier:03d}_example.sql").write_text("SELECT 1;\n", encoding="utf-8")
    migrations = runner.load_migrations(tmp_path)
    rows = {
        1: (migrations[0].filename, migrations[0].checksum, "applied"),
        3: (migrations[2].filename, migrations[2].checksum, "applied"),
    }

    with pytest.raises(runner.MigrationApplyError, match="ledger is not continuous"):
        runner._validate_ledger(migrations, rows)


def _catalog(tmp_path, count):
    for identifier in range(1, count + 1):
        (tmp_path / f"{identifier:03d}_example.sql").write_text("SELECT 1;\n", encoding="utf-8")
    return runner.load_migrations(tmp_path)


def _row(migration, status="applied"):
    return (migration.filename, migration.checksum, status)


def test_ledger_hole_names_the_flag_that_closes_it(tmp_path):
    migrations = _catalog(tmp_path, 3)
    rows = {1: _row(migrations[0]), 3: _row(migrations[2])}

    with pytest.raises(runner.MigrationApplyError, match=r"--fill-gap 2"):
        runner._validate_ledger(migrations, rows)


def test_named_gap_lets_validation_through(tmp_path):
    migrations = _catalog(tmp_path, 3)
    rows = {1: _row(migrations[0]), 3: _row(migrations[2])}

    runner._validate_ledger(migrations, rows, fill_gaps=frozenset({2}))


def test_named_gap_does_not_excuse_the_other_holes(tmp_path):
    migrations = _catalog(tmp_path, 4)
    rows = {1: _row(migrations[0]), 4: _row(migrations[3])}

    with pytest.raises(runner.MigrationApplyError, match="missing: 003"):
        runner._validate_ledger(migrations, rows, fill_gaps=frozenset({2}))


def test_fill_gap_refuses_an_identifier_the_ledger_already_records(tmp_path):
    migrations = _catalog(tmp_path, 3)
    rows = {1: _row(migrations[0]), 2: _row(migrations[1], "failed"), 3: _row(migrations[2])}

    with pytest.raises(runner.MigrationApplyError, match="is not a gap: the ledger records it"):
        runner._validate_gap_requests({1, 2, 3}, rows, frozenset({2}))


def test_fill_gap_refuses_ordinary_pending_work(tmp_path):
    migrations = _catalog(tmp_path, 3)
    rows = {1: _row(migrations[0]), 2: _row(migrations[1])}

    with pytest.raises(runner.MigrationApplyError, match="ahead of ledger head 002"):
        runner._validate_gap_requests({1, 2, 3}, rows, frozenset({3}))


def test_fill_gap_refuses_an_identifier_outside_the_catalog():
    with pytest.raises(runner.MigrationApplyError, match="does not exist in the catalog"):
        runner._validate_gap_requests(
            {1}, {1: ("001_example.sql", "0" * 64, "applied")}, frozenset({9})
        )


def test_fill_gap_is_not_a_verification_mode(monkeypatch, capsys):
    monkeypatch.setenv("PLATFORM_DB_URL", "postgresql://example/db")

    assert runner.main(["--verify-complete", "--fill-gap", "171"]) == 2
    assert "only reads it" in capsys.readouterr().err


def test_applied_entry_after_failure_fails_closed(tmp_path):
    for identifier in range(1, 4):
        (tmp_path / f"{identifier:03d}_example.sql").write_text("SELECT 1;\n", encoding="utf-8")
    migrations = runner.load_migrations(tmp_path)
    rows = {
        1: (migrations[0].filename, migrations[0].checksum, "applied"),
        2: (migrations[1].filename, migrations[1].checksum, "failed"),
        3: (migrations[2].filename, migrations[2].checksum, "applied"),
    }

    with pytest.raises(runner.MigrationApplyError, match="applied entries after a failed"):
        runner._validate_ledger(migrations, rows)


def test_inner_transaction_like_line_is_not_stripped(tmp_path):
    path = tmp_path / "001_example.sql"
    path.write_text(
        "CREATE FUNCTION example() RETURNS void AS $$\n"
        "BEGIN;\nNULL;\nCOMMIT;\n$$ LANGUAGE plpgsql;\n",
        encoding="utf-8",
    )

    with pytest.raises(runner.MigrationCatalogError, match="expected outer BEGIN, COMMIT"):
        runner.load_migrations(tmp_path)


def test_unknown_target_fails_before_database_access(tmp_path):
    path = tmp_path / "001_example.sql"
    path.write_text("SELECT 1;\n", encoding="utf-8")
    migrations = runner.load_migrations(tmp_path)

    with pytest.raises(runner.MigrationApplyError, match="target does not exist: 002"):
        runner.apply_migrations(None, migrations, target=2)


def test_verify_complete_lists_pending_identifiers(tmp_path, monkeypatch):
    path = tmp_path / "001_example.sql"
    path.write_text("SELECT 1;\n", encoding="utf-8")
    migrations = runner.load_migrations(tmp_path)
    monkeypatch.setattr(runner, "_ledger_exists", lambda _conn: True)
    monkeypatch.setattr(runner, "_ledger_rows", lambda _conn: {})

    with pytest.raises(runner.MigrationApplyError, match="pending migrations: 001"):
        runner.verify_complete(None, migrations)


def test_cli_requires_database_url(monkeypatch, capsys):
    monkeypatch.delenv("PLATFORM_DB_URL", raising=False)
    monkeypatch.delenv("PLATFORM_DATABASE_URL", raising=False)

    assert runner.main([]) == 2
    assert "requires --dsn or PLATFORM_DB_URL" in capsys.readouterr().err


def test_a_refusal_names_what_it_lacks_and_not_only_that_it_refused():
    """Measured 2026-08-17: the runner said `failed` and nothing else.

    Migration 273 declares RLS policies on 67 tables, so it needs the schema
    OWNER. Run as the deployed application role it refused, and the only thing
    anyone was told -- on screen AND in the ledger -- was
    `migration 273 273_every_org_scoped_table_carries_a_policy.sql failed`, plus
    the bare word `InsufficientPrivilege` stored in the `error` column, because
    `_record_failure` kept `type(exc).__name__` and dropped the sentence.

    Neither channel named WHICH object was not owned, so the only way to learn it
    was to bypass the runner and apply the file by hand. A tool that refuses
    without naming what it lacks costs exactly that detour, every time.
    """
    class _Privilege(Exception):
        sqlstate = "42501"

    why = runner._why(_Privilege("must be owner of function epic36_is_org_member"))

    # The class alone is not a repair, and neither is the sentence alone.
    assert "_Privilege" in why
    assert "must be owner of function epic36_is_org_member" in why


def test_a_refusal_with_no_sentence_still_names_its_class():
    """An exception with an empty message must not produce a bare colon."""
    class _Silent(Exception):
        pass

    assert runner._why(_Silent()) == "_Silent"


def test_the_recorded_failure_keeps_the_sentence_the_ledger_column_exists_for():
    """`error` sits next to `sqlstate` in the ledger DDL; it held a class name."""
    source = Path(runner.__file__).read_text(encoding="utf-8")

    # The old shape, named so a reader knows what this refuses to come back to.
    assert "message = type(exc).__name__[:200]" not in source
    assert "message = _why(exc)[:2000]" in source
