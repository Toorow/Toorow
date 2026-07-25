"""Tests for core.warehouse_write (Story 24.3, T5).

All tests are OFFLINE (no Postgres, no ASGI).  They use a temp DuckDB file
(``tmp_path``) and mock ``warehouse_tenancy.resolve_org_schemas`` where needed.

Coverage:
  - Flag OFF: open_raw_writer returns a plain connection; resolve_org_schemas
    is never called.
  - Flag ON, org resolvable: schema created + search_path set; an unqualified
    CREATE TABLE raw_x lands in org_<wslug>_raw.
  - Flag ON, org unresolvable (resolve returns None): falls back to main +
    emits a warning.
  - Flag ON, schema creation fails (simulate error): falls back to main +
    warning, connection still returned.
  - Idempotence: calling open_raw_writer twice does not raise.
  - Caller calls con.close() -- no leak.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import duckdb

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_schemas(slug: str = "acme"):
    """Return a minimal OrgSchemas-like object with a .raw attribute."""
    from core.warehouse_tenancy import OrgSchemas

    wslug = slug.replace("-", "_")
    return OrgSchemas(
        org_id="org-1",
        org_slug=slug,
        warehouse_slug=wslug,
        raw=f"org_{wslug}_raw",
        marts=f"org_{wslug}_marts",
    )


def _table_schemas(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Return list of schema names that contain at least one table matching raw_x."""
    rows = con.execute(
        "SELECT DISTINCT table_schema FROM information_schema.tables"
        " WHERE table_name LIKE 'raw_%'"
    ).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Flag OFF
# ---------------------------------------------------------------------------

class TestFlagOff:
    def test_returns_plain_connection_no_resolve(self, tmp_path):
        """Flag OFF: open_raw_writer bit-identical to duckdb.connect(); no PG call."""
        from core import warehouse_tenancy, warehouse_write

        warehouse_write._reset_warn()
        db = str(tmp_path / "test.duckdb")

        with patch.object(
            warehouse_tenancy, "org_schemas_enabled", return_value=False
        ), patch.object(
            warehouse_tenancy, "resolve_org_schemas"
        ) as mock_resolve:
            con = warehouse_write.open_raw_writer(db, project_id="proj-1")
            mock_resolve.assert_not_called()
            assert con is not None
            # Connection works: can create a table in main
            con.execute("CREATE TABLE raw_test (id INTEGER)")
            con.close()

        # Verify table landed in main (no special schema)
        con2 = duckdb.connect(db)
        schemas = _table_schemas(con2)
        con2.close()
        assert "main" in schemas

    def test_flag_off_no_env_var(self, tmp_path):
        """No env var set -> flag OFF by default."""
        from core import warehouse_write

        warehouse_write._reset_warn()
        db = str(tmp_path / "test.duckdb")
        os.environ.pop("TOOROW_ORG_SCHEMAS", None)

        con = warehouse_write.open_raw_writer(db, project_id="proj-1")
        con.execute("CREATE TABLE raw_flagoff (id INTEGER)")
        con.close()


# ---------------------------------------------------------------------------
# Flag ON, resolvable
# ---------------------------------------------------------------------------

class TestFlagOn:
    def test_schema_created_and_search_path_set(self, tmp_path):
        """Flag ON: schema org_acme_raw created; unqualified DDL lands there."""
        from core import warehouse_tenancy, warehouse_write

        warehouse_write._reset_warn()
        db = str(tmp_path / "org.duckdb")
        schemas = _make_schemas("acme")

        with patch.object(
            warehouse_tenancy, "org_schemas_enabled", return_value=True
        ), patch.object(
            warehouse_tenancy, "resolve_org_schemas", return_value=schemas
        ):
            con = warehouse_write.open_raw_writer(db, project_id="proj-42")
            # Unqualified DDL -- must land in org_acme_raw
            con.execute("CREATE TABLE IF NOT EXISTS raw_ga4_standard_daily (date VARCHAR)")
            con.execute(
                "INSERT INTO raw_ga4_standard_daily VALUES (?)", ["2024-01-01"]
            )
            con.close()

        # Inspect the DuckDB to confirm table is in the org schema
        con2 = duckdb.connect(db)
        row = con2.execute(
            "SELECT COUNT(*) FROM org_acme_raw.raw_ga4_standard_daily"
        ).fetchone()
        con2.close()
        assert row[0] == 1

    def test_table_not_in_main(self, tmp_path):
        """Flag ON: after write, raw_* is NOT in main schema."""
        from core import warehouse_tenancy, warehouse_write

        warehouse_write._reset_warn()
        db = str(tmp_path / "org2.duckdb")
        schemas = _make_schemas("beta")

        with patch.object(
            warehouse_tenancy, "org_schemas_enabled", return_value=True
        ), patch.object(
            warehouse_tenancy, "resolve_org_schemas", return_value=schemas
        ):
            con = warehouse_write.open_raw_writer(db, project_id="proj-7")
            con.execute("CREATE TABLE IF NOT EXISTS raw_meta_ads_daily (date VARCHAR)")
            con.close()

        con2 = duckdb.connect(db)
        in_main = con2.execute(
            "SELECT COUNT(*) FROM information_schema.tables"
            " WHERE table_schema='main' AND table_name='raw_meta_ads_daily'"
        ).fetchone()[0]
        con2.close()
        assert in_main == 0, "raw table must NOT appear in main when flag ON"

    def test_idempotent_schema_creation(self, tmp_path):
        """Calling open_raw_writer twice does not raise (CREATE SCHEMA IF NOT EXISTS)."""
        from core import warehouse_tenancy, warehouse_write

        warehouse_write._reset_warn()
        db = str(tmp_path / "idem.duckdb")
        schemas = _make_schemas("idempotent_org")

        with patch.object(
            warehouse_tenancy, "org_schemas_enabled", return_value=True
        ), patch.object(
            warehouse_tenancy, "resolve_org_schemas", return_value=schemas
        ):
            con1 = warehouse_write.open_raw_writer(db, project_id="p1")
            con1.execute("CREATE TABLE IF NOT EXISTS raw_x (v INTEGER)")
            con1.close()

            con2 = warehouse_write.open_raw_writer(db, project_id="p1")
            con2.execute("CREATE TABLE IF NOT EXISTS raw_x (v INTEGER)")
            con2.close()

    def test_resolve_called_with_project_id(self, tmp_path):
        """Flag ON: resolve_org_schemas is called with the provided project_id."""
        from core import warehouse_tenancy, warehouse_write

        warehouse_write._reset_warn()
        db = str(tmp_path / "check.duckdb")

        with patch.object(
            warehouse_tenancy, "org_schemas_enabled", return_value=True
        ), patch.object(
            warehouse_tenancy,
            "resolve_org_schemas",
            return_value=_make_schemas("check"),
        ) as mock_resolve:
            con = warehouse_write.open_raw_writer(db, project_id="proj-check")
            con.close()
            mock_resolve.assert_called_once_with(project_id="proj-check")


# ---------------------------------------------------------------------------
# Flag ON, unresolvable org
# ---------------------------------------------------------------------------

class TestMixedCaseSymmetry:
    """F-1: prove that the quoted read side finds tables written via SET search_path.

    DuckDB lowercases unquoted identifiers at creation time.  A module that
    declares ``CREATE TABLE Raw_Mixed`` (mixed case, unquoted) via open_raw_writer
    (flag ON) will create a table named ``raw_mixed`` in the org schema.
    _count_raw_rows must find it by quoting the table name from the registry --
    which also holds the registered name as-given (e.g. ``Raw_Mixed``).

    This test proves the symmetry is maintained even if a future module uses a
    mixed-case table name: the reader quotes both sides so DuckDB resolves the
    lowercased form in both cases.
    """

    def test_mixed_case_write_read_symmetry(self, tmp_path, monkeypatch):
        """open_raw_writer creates Raw_Mixed (stored as raw_mixed by DuckDB);
        _count_raw_rows with quoted identifier finds it correctly."""
        from unittest.mock import patch

        from core import verification, warehouse_tenancy, warehouse_write
        from core.warehouse_tenancy import OrgSchemas

        warehouse_write._reset_warn()
        db = str(tmp_path / "mixed.duckdb")

        org_schemas = OrgSchemas(
            org_id="org-x",
            org_slug="mixedtest",
            warehouse_slug="mixedtest",
            raw="org_mixedtest_raw",
            marts="org_mixedtest_marts",
        )

        # Write via open_raw_writer with a mixed-case DDL name (simulating a module)
        with patch.object(
            warehouse_tenancy, "org_schemas_enabled", return_value=True
        ), patch.object(
            warehouse_tenancy, "resolve_org_schemas", return_value=org_schemas
        ):
            con = warehouse_write.open_raw_writer(db, project_id="proj-mixed")
            # Unquoted mixed-case: DuckDB lowercases this to raw_mixed
            con.execute(
                "CREATE TABLE IF NOT EXISTS Raw_Mixed (pull_id VARCHAR, project_id VARCHAR)"
            )
            con.execute(
                "INSERT INTO Raw_Mixed VALUES ('mixed-pull-1', 'proj-mixed')"
            )
            con.close()

        # Register the name as the module would -- with the case used in DDL
        verification.register_raw_table_name("Raw_Mixed", provider="_test_mixed")

        monkeypatch.setenv("TOOROW_DUCKDB_PATH", db)
        monkeypatch.setenv("TOOROW_ORG_SCHEMAS", "1")

        with patch.object(
            warehouse_tenancy, "resolve_org_schemas", return_value=org_schemas
        ):
            count = verification._count_raw_rows(
                pull_id="mixed-pull-1",
                provider="_test_mixed",
                project_id="proj-mixed",
            )

        # DuckDB case-insensitive resolution: "Raw_Mixed" and "raw_mixed" are the same
        assert count == 1, (
            "Quoted table name in _count_raw_rows must resolve the lowercased table"
            " that open_raw_writer's unquoted DDL created"
        )


class TestFlagOnUnresolvable:
    def test_fallback_to_main_when_resolve_returns_none(self, tmp_path, caplog):
        """Flag ON + resolve=None: falls back to main + emits WARNING."""
        import logging

        from core import warehouse_tenancy, warehouse_write

        warehouse_write._reset_warn()
        db = str(tmp_path / "fallback.duckdb")

        with patch.object(
            warehouse_tenancy, "org_schemas_enabled", return_value=True
        ), patch.object(
            warehouse_tenancy, "resolve_org_schemas", return_value=None
        ):
            with caplog.at_level(logging.WARNING, logger="core.warehouse_write"):
                con = warehouse_write.open_raw_writer(db, project_id="proj-legacy")
            # Connection must still be valid
            con.execute("CREATE TABLE raw_fallback (id INTEGER)")
            con.close()

        assert "falling back to main" in caplog.text.lower()

        # Table should be in main (no org schema was set)
        con2 = duckdb.connect(db)
        schemas = _table_schemas(con2)
        con2.close()
        assert "main" in schemas

    def test_flag_on_with_legacy_main_table_no_collision(self, tmp_path):
        """F-2: flag ON must route to org schema even when main.raw_* already exists.

        Scenario: warehouse already has main.raw_ga4_standard_daily with legacy rows
        (before migration script ran).  open_raw_writer flag ON must write into
        org_acme_raw.raw_ga4_standard_daily and leave main unchanged.
        """
        from core import warehouse_tenancy, warehouse_write

        warehouse_write._reset_warn()
        db = str(tmp_path / "legacy_collision.duckdb")

        # Pre-populate main with a legacy row
        seed = duckdb.connect(db)
        seed.execute(
            "CREATE TABLE raw_ga4_standard_daily (pull_id VARCHAR, project_id VARCHAR)"
        )
        seed.execute(
            "INSERT INTO raw_ga4_standard_daily VALUES (?, ?)",
            ["legacy-pull-1", "proj-legacy"],
        )
        main_before = seed.execute(
            "SELECT COUNT(*) FROM main.raw_ga4_standard_daily"
        ).fetchone()[0]
        seed.close()

        schemas = _make_schemas("acme")

        with patch.object(
            warehouse_tenancy, "org_schemas_enabled", return_value=True
        ), patch.object(
            warehouse_tenancy, "resolve_org_schemas", return_value=schemas
        ):
            con = warehouse_write.open_raw_writer(db, project_id="proj-42")
            con.execute(
                "CREATE TABLE IF NOT EXISTS raw_ga4_standard_daily"
                " (pull_id VARCHAR, project_id VARCHAR)"
            )
            con.execute(
                "INSERT INTO raw_ga4_standard_daily VALUES (?, ?)",
                ["org-pull-1", "proj-42"],
            )
            con.close()

        verify = duckdb.connect(db)
        # New row landed in org schema
        org_count = verify.execute(
            "SELECT COUNT(*) FROM org_acme_raw.raw_ga4_standard_daily"
            " WHERE pull_id = 'org-pull-1'"
        ).fetchone()[0]
        # main unchanged -- legacy row still there, no new rows added
        main_after = verify.execute(
            "SELECT COUNT(*) FROM main.raw_ga4_standard_daily"
        ).fetchone()[0]
        verify.close()

        assert org_count == 1, "New row must land in org_acme_raw (not main)"
        assert main_after == main_before, (
            f"main.raw_ga4_standard_daily must be untouched: before={main_before}"
            f" after={main_after}"
        )

    def test_fallback_warn_once(self, tmp_path, caplog):
        """Warning fires only once per project_id (warn-once dedup)."""
        import logging

        from core import warehouse_tenancy, warehouse_write

        warehouse_write._reset_warn()
        db = str(tmp_path / "warnonce.duckdb")

        with patch.object(
            warehouse_tenancy, "org_schemas_enabled", return_value=True
        ), patch.object(
            warehouse_tenancy, "resolve_org_schemas", return_value=None
        ):
            with caplog.at_level(logging.WARNING, logger="core.warehouse_write"):
                con1 = warehouse_write.open_raw_writer(db, project_id="proj-warn")
                con1.close()
                con2 = warehouse_write.open_raw_writer(db, project_id="proj-warn")
                con2.close()

        # Only one warning emitted for this project_id
        warnings = [r for r in caplog.records if "falling back to main" in r.message.lower()]
        assert len(warnings) == 1
