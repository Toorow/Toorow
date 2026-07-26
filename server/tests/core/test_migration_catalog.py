from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "check_migration_catalog.py"
SPEC = importlib.util.spec_from_file_location("check_migration_catalog", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
catalog = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(catalog)


def _write(directory: Path, name: str) -> None:
    (directory / name).write_text("BEGIN; COMMIT;\n", encoding="utf-8")


def test_repository_catalog_is_unique_and_continuous():
    migrations = catalog.validate_catalog(
        ROOT / "infra" / "nango" / "migrations", verify_manifest=True
    )

    assert migrations[0].name.startswith("001_")
    # 117/118 = epic-44 wave 5 (context_graph target_field nodes + owner column);
    # manifest regenerated via check_migration_catalog.py --write-manifest.
    assert migrations[-1].name == "118_context_owner_column.sql"
    assert len(migrations) == 118


def test_invitation_person_binding_migration_installs_immutable_trigger():
    sql = (ROOT / "infra/nango/migrations/112_invitation_canonical_person.sql").read_text(
        encoding="utf-8"
    )

    assert "CREATE TRIGGER trg_invitation_exchange_binding" in sql
    assert "BEFORE UPDATE ON app.invitation_exchange_sessions" in sql
    assert "EXECUTE FUNCTION app.protect_invitation_exchange_binding()" in sql


def test_duplicate_identifier_fails_with_both_filenames(tmp_path):
    _write(tmp_path, "001_first.sql")
    _write(tmp_path, "001_second.sql")

    with pytest.raises(catalog.MigrationCatalogError) as exc_info:
        catalog.validate_catalog(tmp_path)

    message = str(exc_info.value)
    assert "duplicate migration 001" in message
    assert "001_first.sql" in message
    assert "001_second.sql" in message


def test_missing_predecessor_fails_before_execution(tmp_path):
    _write(tmp_path, "001_first.sql")
    _write(tmp_path, "003_third.sql")

    with pytest.raises(
        catalog.MigrationCatalogError,
        match="missing migration identifiers: 002",
    ):
        catalog.validate_catalog(tmp_path)


def test_invalid_filename_is_rejected(tmp_path):
    _write(tmp_path, "001_first.sql")
    _write(tmp_path, "2_second.sql")

    with pytest.raises(
        catalog.MigrationCatalogError,
        match="invalid filename: 2_second.sql",
    ):
        catalog.validate_catalog(tmp_path)


def test_entry_migration_identifier_is_pinned(tmp_path):
    for identifier in range(1, 110):
        _write(tmp_path, f"{identifier:03d}_migration.sql")

    with pytest.raises(
        catalog.MigrationCatalogError,
        match="pinned migration 109 must be 109_entry_invitation_without_org.sql",
    ):
        catalog.validate_catalog(tmp_path)

def test_manifest_detects_reviewed_sql_drift(tmp_path):
    _write(tmp_path, "001_first.sql")
    assert catalog.main([str(tmp_path), "--write-manifest"]) == 0
    _write(tmp_path, "001_first.sql")
    (tmp_path / "001_first.sql").write_text("SELECT 2;\n", encoding="utf-8")

    with pytest.raises(catalog.MigrationCatalogError, match="migration manifest drift"):
        catalog.validate_catalog(tmp_path, verify_manifest=True)

def test_cli_reports_the_exact_catalog_error(tmp_path, capsys):
    _write(tmp_path, "001_first.sql")
    _write(tmp_path, "001_second.sql")

    assert catalog.main([str(tmp_path)]) == 1
    assert "duplicate migration 001" in capsys.readouterr().err
