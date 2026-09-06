"""Execution isolation: a candidate must not be readable before it is published.

This is C-8 on Story 47.5, and it was never a configuration problem. A Connector
lands every pull into ONE shared raw table, and all 137 staging models supersede
on `pull_id DESC` — so a candidate written there is live the instant it lands,
publishing numbers nobody confirmed. That is why candidate materialization was
refused in every mode, and why setting an env var could not have fixed it.

The isolation here is a separate RELATION, not a tag on the rows. A tag has to be
honoured by every reader, and one staging model that forgets it publishes the
candidate; staging names the shared table, so a different relation is invisible
to it by construction — and no dbt model changes.

What these pin is the property the marts depend on, stated the way it fails:
after landing a candidate, the shared table must contain NOTHING of it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SERVER = Path(__file__).resolve().parents[2]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

from core.raw_landing import (  # noqa: E402
    RawLandingError,
    candidate_table,
    discard_candidate,
    land_raw_rows,
    promote_candidate,
)

TABLE = "raw_example_daily"
COLUMNS = [("date", "STRING"), ("value", "FLOAT"), ("pull_id", "STRING")]
EXEC = "exec_EXAMPLE_01"


def _rows(tag, n=1):
    return [{"date": "2026-07-29", "value": float(i), "pull_id": tag} for i in range(n)]


@pytest.fixture()
def duck(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "w.duckdb"))
    monkeypatch.delenv("TOOROW_ORG_SCHEMAS", raising=False)
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")

    import duckdb

    def read(table):
        con = duckdb.connect(str(tmp_path / "w.duckdb"))
        try:
            return con.execute(f"SELECT pull_id FROM {table} ORDER BY pull_id").fetchall()
        except duckdb.CatalogException:
            return None  # the relation does not exist
        finally:
            con.close()

    return read


def test_a_candidate_is_absent_from_the_table_staging_reads(duck):
    """The property the marts depend on. If this fails, numbers publish unconfirmed."""
    land_raw_rows(TABLE, _rows("published"), columns=COLUMNS, backend="duckdb")
    land_raw_rows(TABLE, _rows("candidate", 3), columns=COLUMNS, backend="duckdb",
                  execution_id=EXEC)

    assert duck(TABLE) == [("published",)], "the candidate leaked into the shared table"
    assert len(duck(candidate_table(TABLE, EXEC))) == 3


def test_publication_appends_the_candidate_and_retires_its_relation(duck):
    land_raw_rows(TABLE, _rows("published"), columns=COLUMNS, backend="duckdb")
    land_raw_rows(TABLE, _rows("candidate", 2), columns=COLUMNS, backend="duckdb",
                  execution_id=EXEC)

    result = promote_candidate(TABLE, EXEC, columns=COLUMNS, backend="duckdb")

    assert result["promoted"] == 2
    assert duck(TABLE) == [("candidate",), ("candidate",), ("published",)]
    # Append-only (AD-7): the rows keep their own pull_id, so the staging QUALIFY
    # decides what wins exactly as it would for any pull. Publication does not
    # rewrite history, it stops hiding it.
    assert duck(candidate_table(TABLE, EXEC)) is None


def test_publication_retry_is_idempotent_after_candidate_drop(duck):
    land_raw_rows(
        TABLE,
        _rows("candidate", 2),
        columns=COLUMNS,
        backend="duckdb",
        execution_id=EXEC,
    )

    first = promote_candidate(
        TABLE,
        EXEC,
        columns=COLUMNS,
        backend="duckdb",
        idempotency_column="pull_id",
        idempotency_value="candidate",
        expected_rows=2,
    )
    replay = promote_candidate(
        TABLE,
        EXEC,
        columns=COLUMNS,
        backend="duckdb",
        idempotency_column="pull_id",
        idempotency_value="candidate",
        expected_rows=2,
    )

    assert first["promoted"] == replay["promoted"] == 2
    assert replay["replayed"] is True
    assert duck(TABLE) == [("candidate",), ("candidate",)]


def test_candidate_inspection_reads_materialized_rows_and_schema(duck):
    from core.raw_landing import (
        candidate_rows_fingerprint,
        candidate_schema_fingerprint,
        inspect_candidate,
    )

    rows = _rows("candidate", 2)
    land_raw_rows(
        TABLE,
        rows,
        columns=COLUMNS,
        backend="duckdb",
        execution_id=EXEC,
    )
    observed = inspect_candidate(TABLE, EXEC, columns=COLUMNS, backend="duckdb")

    assert observed["row_count"] == 2
    assert observed["content_fingerprint"] == candidate_rows_fingerprint(
        rows, columns=COLUMNS
    )
    assert observed["schema_fingerprint"] == candidate_schema_fingerprint(COLUMNS)


def test_rollback_drops_the_candidate_and_leaves_the_published_rows(duck):
    land_raw_rows(TABLE, _rows("published"), columns=COLUMNS, backend="duckdb")
    land_raw_rows(TABLE, _rows("candidate", 5), columns=COLUMNS, backend="duckdb",
                  execution_id=EXEC)

    discard_candidate(TABLE, EXEC, backend="duckdb")

    assert duck(candidate_table(TABLE, EXEC)) is None
    assert duck(TABLE) == [("published",)]


def test_two_candidates_do_not_see_each_other(duck):
    """"Isolated by execution" has to mean per execution, not per candidate."""
    land_raw_rows(TABLE, _rows("a", 2), columns=COLUMNS, backend="duckdb", execution_id="exec_A")
    land_raw_rows(TABLE, _rows("b", 3), columns=COLUMNS, backend="duckdb", execution_id="exec_B")

    assert len(duck(candidate_table(TABLE, "exec_A"))) == 2
    assert len(duck(candidate_table(TABLE, "exec_B"))) == 3
    assert duck(TABLE) is None, "no candidate should have created the shared table"


def test_publishing_one_candidate_leaves_the_other_isolated(duck):
    land_raw_rows(TABLE, _rows("a", 2), columns=COLUMNS, backend="duckdb", execution_id="exec_A")
    land_raw_rows(TABLE, _rows("b", 3), columns=COLUMNS, backend="duckdb", execution_id="exec_B")

    promote_candidate(TABLE, "exec_A", columns=COLUMNS, backend="duckdb")

    assert duck(TABLE) == [("a",), ("a",)]
    assert len(duck(candidate_table(TABLE, "exec_B"))) == 3


def test_the_isolated_relation_is_derived_never_supplied():
    """The execution id reaches a relation NAME; it cannot reach the SQL itself."""
    assert candidate_table(TABLE, EXEC) == f"{TABLE}__cand_{EXEC}"
    # Anything that is not a bare identifier is hashed, so nothing an id contains
    # is ever interpolated into a statement.
    hostile = candidate_table(TABLE, "a; DROP TABLE t")
    assert hostile.startswith(f"{TABLE}__cand_h")
    assert ";" not in hostile and " " not in hostile
    with pytest.raises(RawLandingError):
        candidate_table("bad table", EXEC)
    with pytest.raises(RawLandingError, match="requires an execution_id"):
        candidate_table(TABLE, "")


@pytest.mark.parametrize(
    ("left", "right"),
    [("exec-A", "exec_A"), ("...", "///"), ("a.b", "a-b"), ("x/y", "x:y")],
)
def test_distinct_executions_never_share_a_relation(left, right):
    """The first version replaced illegal characters, and that is a collision.

    `re.sub(r"[^A-Za-z0-9_]", "_", ...)` maps `exec-A` and `exec_A` onto the SAME
    name -- so two candidates would land in one relation and the isolation this
    whole mechanism exists to provide would be gone, silently, with every test
    above still green.
    """
    assert candidate_table(TABLE, left) != candidate_table(TABLE, right)


# --------------------------------------------------------------------------
# The ambient scope, which is what makes 24 connectors candidate-capable
# without any of them being edited.
# --------------------------------------------------------------------------


def test_a_real_connector_landing_is_isolated_by_the_scope_alone(duck, tmp_path, monkeypatch):
    """gsc._insert_raw_rows knows nothing about candidates, and is isolated anyway.

    This is the property that made the 24-module edit unnecessary -- and the one
    that matters: a module that never heard of candidates cannot leak one,
    because WHERE it writes is decided by the scope, not by the module.
    """
    import sys

    sys.path.insert(0, str(_SERVER / "modules"))
    from core.raw_landing import candidate_execution
    from gsc import connector as gsc

    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    row = {
        "date": "2026-07-29", "page": "https://example.com/a", "country": "fra",
        "device": "MOBILE", "query": "example", "search_type": "web",
        "clicks": 5, "impressions": 100, "average_position": 1.5,
    }
    path = str(tmp_path / "w.duckdb")

    with candidate_execution("exec_CANDIDATE"):
        gsc._insert_raw_rows([row], "pull_c", "2026-07-29T00:00:00Z", "proj_EXAMPLE",
                             "duckdb", path)

    import duckdb

    con = duckdb.connect(path)
    try:
        # The shared table staging names must not exist at all yet.
        with pytest.raises(duckdb.CatalogException):
            con.execute("SELECT * FROM main.raw_gsc_daily").fetchall()
        isolated_schema = candidate_table("raw", "exec_CANDIDATE")
        isolated_table = candidate_table("raw_gsc_daily", "exec_CANDIDATE")
        assert con.execute(
            f'SELECT clicks FROM "{isolated_schema}"."{isolated_table}"'
        ).fetchall() == [(5,)]
    finally:
        con.close()


def test_the_scope_does_not_leak_past_its_block(duck):
    from core.raw_landing import active_candidate_execution, candidate_execution

    assert active_candidate_execution() is None
    with candidate_execution("exec_X"):
        assert active_candidate_execution() == "exec_X"
    assert active_candidate_execution() is None, "a leaked scope isolates a REAL pull"


def test_a_nested_scope_restores_the_outer_one(duck):
    from core.raw_landing import active_candidate_execution, candidate_execution

    with candidate_execution("outer"), candidate_execution("inner"):
        assert active_candidate_execution() == "inner"
    assert active_candidate_execution() is None
