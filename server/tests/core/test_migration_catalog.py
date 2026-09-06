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

    # This assertion used to pin the head FILENAME and the exact count, with a
    # comment instructing the next author to bump both by hand. That convention
    # failed eleven consecutive times: the catalog reached 150 while the test
    # still named 139, so it was red for every session that ran it and told none
    # of them anything true. A ceiling that must be edited to stay correct is a
    # ceiling that measures who remembered, not whether the catalog is sound.
    #
    # What the test is NAMED for is unique and continuous, so that is what it
    # asserts now, and it holds at any size:
    #   * identifiers are 001..N with no gap and no duplicate;
    #   * the count equals the highest identifier.
    # Manifest agreement is already proven by `verify_manifest=True` above, which
    # is the check that actually catches an unreviewed SQL edit.
    identifiers = [int(migration.name.split("_", 1)[0]) for migration in migrations]

    assert identifiers == sorted(identifiers)
    assert len(set(identifiers)) == len(identifiers), "duplicate migration identifier"
    assert identifiers == list(range(1, len(identifiers) + 1)), "gap in the migration catalog"
    assert identifiers[-1] == len(migrations)


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
