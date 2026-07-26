from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
import apply_migrations as runner  # noqa: E402


def test_complete_catalog_has_safe_transaction_boundaries():
    migrations = runner.load_migrations(ROOT / "infra" / "nango" / "migrations")

    assert len(migrations) == 115
    assert migrations[-1].identifier == 115
    assert all("\nBEGIN;" not in migration.body for migration in migrations)
    assert all("\nCOMMIT;" not in migration.body for migration in migrations)


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
