"""Both managed-feed landing paths write through the one raw seam.

There were two landers. `csv_excel_import.run_import` had none at all -- its
physical write was a `# Phase B ... goes here` comment, so an upload counted its
rows and landed nothing. `google_sheets_sync._write_landing_rows` had its own
DuckDB CREATE/INSERT, which worked but was narrower than the shared seam in two
ways that mattered: it could not reach BigQuery, and it ignored an active
`candidate_execution` scope entirely.

That second one is why the Sheets channel could not produce an isolated
candidate: the isolation is a property of `raw_landing`, and a writer that does
not go through it lands straight into the shared relation staging reads.

These tests pin the seam rather than the implementation. A future writer that
opens its own connection again fails here, which is the only way this stays true
-- the two paths drifted for exactly as long as nothing compared them.
"""

from __future__ import annotations

from core.google_sheets_sync import _write_landing_rows
from core.raw_landing import candidate_execution

#: AD-7 provenance every landing call must carry -- the values the run itself
#: produced, never invented by the writer.
_PROVENANCE = {
    "execution_id": "dse_01",
    "plan_version_id": "dsp_01",
    "mapping_version_id": "dmap_01",
}


def _capture(monkeypatch) -> dict:
    seen: dict = {}

    def fake_land(table, rows, *, columns, project_id=None, **kwargs):
        from core.raw_landing import active_candidate_execution

        seen["table"] = table
        seen["rows"] = rows
        seen["columns"] = columns
        seen["project_id"] = project_id
        seen["isolated_as"] = active_candidate_execution()
        return {"rows": len(rows), "table": table}

    monkeypatch.setattr("core.raw_landing.land_raw_rows", fake_land)
    return seen


def test_the_sheets_landing_goes_through_the_shared_raw_seam(monkeypatch):
    seen = _capture(monkeypatch)

    written = _write_landing_rows(
        [{"day": "2026-07-01", "clicks": "5"}],
        landing_relation="raw_project.managed_feed_ds1",
        datastream_id="ds_1",
        project_id="proj_EXAMPLE",
        **_PROVENANCE,
        duckdb_path=None,
    )

    assert written == 1
    # The bare table name: the seam resolves the dataset from the Project, and a
    # caller that composed the location itself is the AD-8 defect.
    assert seen["table"] == "managed_feed_ds1"
    assert seen["project_id"] == "proj_EXAMPLE"


def test_the_sheets_landing_honours_an_active_candidate_isolation(monkeypatch):
    """The property the Sheets channel needs before it can have a candidate."""
    seen = _capture(monkeypatch)

    with candidate_execution("dse_sheets"):
        _write_landing_rows(
            [{"day": "2026-07-01"}],
            landing_relation="raw_project.managed_feed_ds1",
            datastream_id="ds_1",
            project_id="proj_EXAMPLE",
            **_PROVENANCE,
            duckdb_path=None,
        )

    assert seen["isolated_as"] == "dse_sheets", (
        "a writer that ignores the scope lands straight into the relation staging reads"
    )


def test_a_column_only_later_rows_carry_is_not_dropped(monkeypatch):
    """Deriving columns from the first row alone shifts the rest of the sheet."""
    seen = _capture(monkeypatch)

    _write_landing_rows(
        [{"day": "2026-07-01"}, {"day": "2026-07-02", "clicks": "9"}],
        landing_relation="raw_project.t",
        datastream_id="ds_1",
        project_id="proj_EXAMPLE",
        **_PROVENANCE,
        duckdb_path=None,
    )

    assert [name for name, _ in seen["columns"]][:2] == ["day", "clicks"]
    assert seen["rows"][0]["day"] == "2026-07-01"
    assert seen["rows"][0]["clicks"] == ""


def test_an_empty_sheet_writes_nothing_and_says_so(monkeypatch):
    seen = _capture(monkeypatch)

    assert (
        _write_landing_rows(
            [],
            landing_relation="raw_project.t",
            datastream_id="ds_1",
            project_id="proj_EXAMPLE",
            **_PROVENANCE,
            duckdb_path=None,
        )
        == 0
    )
    assert seen == {}, "an empty write must not reach the warehouse at all"


def test_landed_rows_carry_the_import_provenance(monkeypatch):
    """AD-7: the same four columns import_runner lands, in the same order.

    Without them the landed rows are invisible to every scoped reader
    (collected_mapped_reader filters WHERE project_id = ...) and the
    superseding reads MAX(execution_id) as NULL -- the measured state of the
    Sheets channel before this writer carried the run's provenance.
    """
    seen = _capture(monkeypatch)

    _write_landing_rows(
        [{"day": "2026-07-01", "clicks": "5"}],
        landing_relation="raw_project.managed_feed_ds1",
        datastream_id="ds_1",
        project_id="proj_EXAMPLE",
        **_PROVENANCE,
        duckdb_path=None,
    )

    # Provenance is appended AFTER the sheet's own columns, in import_runner's
    # order: execution, plan, mapping, project.
    assert [name for name, _ in seen["columns"]] == [
        "day",
        "clicks",
        "execution_id",
        "plan_version_id",
        "mapping_version_id",
        "project_id",
    ]
    assert seen["rows"][0] == {
        "day": "2026-07-01",
        "clicks": "5",
        "execution_id": "dse_01",
        "plan_version_id": "dsp_01",
        "mapping_version_id": "dmap_01",
        "project_id": "proj_EXAMPLE",
    }


def test_a_sheet_column_named_like_a_provenance_column_loses(monkeypatch):
    """A sheet header colliding with a provenance name cannot shadow the run's."""
    seen = _capture(monkeypatch)

    _write_landing_rows(
        [{"day": "2026-07-01", "project_id": "sheet_value"}],
        landing_relation="raw_project.t",
        datastream_id="ds_1",
        project_id="proj_EXAMPLE",
        **_PROVENANCE,
        duckdb_path=None,
    )

    assert [name for name, _ in seen["columns"]].count("project_id") == 1
    assert seen["rows"][0]["project_id"] == "proj_EXAMPLE"


def test_the_provenance_reaches_the_landed_relation(tmp_path, monkeypatch):
    """Same assertion as above, against the REAL relation the seam writes.

    The capture tests prove what crosses the seam; this one proves what a
    scoped reader would actually SELECT -- the defect was measured on the
    relation, so the fix is verified on the relation (pattern:
    test_raw_landing.test_duckdb_round_trips_the_rows).
    """
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "t.duckdb"))
    monkeypatch.delenv("TOOROW_ORG_SCHEMAS", raising=False)

    _write_landing_rows(
        [{"day": "2026-07-01", "clicks": "5"}],
        landing_relation="raw_project.managed_feed_ds1",
        datastream_id="ds_1",
        project_id="proj_EXAMPLE",
        **_PROVENANCE,
        duckdb_path=None,
    )

    import duckdb  # noqa: PLC0415

    con = duckdb.connect(str(tmp_path / "t.duckdb"))
    try:
        row = con.execute(
            "SELECT day, clicks, execution_id, plan_version_id, "
            "mapping_version_id, project_id FROM managed_feed_ds1"
        ).fetchone()
    finally:
        con.close()

    assert row == ("2026-07-01", "5", "dse_01", "dsp_01", "dmap_01", "proj_EXAMPLE")
