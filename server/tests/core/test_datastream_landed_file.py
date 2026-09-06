"""The last file that arrived, actually read back -- lot B1.

DRIVEN AGAINST A REAL DUCKDB WAREHOUSE for the read, and a hand-built cursor for
Postgres -- the pattern of `tests/core/test_collected_mapped_reader.py`, whose
fixture this file reuses the shape of. No Postgres is needed to prove what this
module decides, because what it decides is which of six silences a Datastream is
in and where the rows physically are.

WHAT IS PINNED HERE, and each line is a way of quietly showing the wrong thing:

  * the rows of the landing really are read, and they are the LAST import's --
    a promoted candidate sits in a shared relation beside every earlier import,
    so the execution narrows the read;
  * masking is a REFUSAL BY DEFAULT: a source-named column the mapping does not
    classify `none` never leaves the server, which is the day reading's policy
    and not `cache_warehouse`'s thirteen English patterns;
  * another Project's row on the same relation never appears;
  * « no file has arrived yet », « a file arrived and has not landed », « the
    last import failed » and « the relation is not there » are FOUR different
    reasons with four different sentences -- the empty/broken separation this
    lot exists for;
  * a `connector_pull` is refused before any warehouse statement is issued.

THE BIGQUERY BRANCH IS NOT EXERCISED, and that is stated rather than implied:
this repository has no GCP dataset and a mocked client would prove only that the
SQL can be written. What holds the dialects in step is that both answer the same
shape from the same function, and that shape is pinned below.
"""

from __future__ import annotations

from typing import Any

import pytest
from core.datastream_landed_file import (
    ARRIVED_NOT_LANDED,
    IMPORT_FAILED,
    NO_FILE_YET,
    NOT_A_PUSHED_SOURCE,
    RELATION_ABSENT,
    read_landed_file,
    read_landing_rows,
)

#: The value the masking policy exists for. If this string reaches a payload,
#: the mask fell.
_SECRET = "someone@example.com"

_LANDING_DDL = """
    CREATE TABLE main.managed_feed_ds_EXAMPLE (
        project_id   VARCHAR,
        execution_id VARCHAR,
        titre        VARCHAR,
        contact      VARCHAR,
        vues         BIGINT
    )
"""


def _seed(path: str) -> None:
    import duckdb

    con = duckdb.connect(path)
    try:
        con.execute(_LANDING_DDL)
        con.executemany(
            "INSERT INTO main.managed_feed_ds_EXAMPLE VALUES (?,?,?,?,?)",
            [
                # The last import.
                ("proj_EXAMPLE", "dse_LAST", "b", _SECRET, 20),
                ("proj_EXAMPLE", "dse_LAST", "a", _SECRET, 10),
                # An earlier import promoted into the same shared relation. It
                # must not be read as part of "the last file".
                ("proj_EXAMPLE", "dse_OLD", "old", _SECRET, 99),
                # Another Project's row. Leaking it is a breach.
                ("proj_OTHER", "dse_LAST", "other", _SECRET, 77),
            ],
        )
    finally:
        con.close()


@pytest.fixture
def warehouse_file(tmp_path, monkeypatch):
    origin = str(tmp_path / "origin_local.duckdb")
    _seed(origin)
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", origin)
    monkeypatch.setenv("TOOROW_CACHE_ENABLED", "false")
    monkeypatch.delenv("TOOROW_ORG_SCHEMAS", raising=False)
    return origin


#: What the mapping declares. `none` is the ONLY value that shows a column;
#: `contact` is absent from it on purpose, and absence masks.
_CLASSIFICATIONS = {"titre": "none", "vues": "none", "contact": "pii"}


# ---------------------------------------------------------------------------
# The warehouse read.
# ---------------------------------------------------------------------------


def test_the_landing_is_read_and_narrowed_to_this_import(warehouse_file) -> None:
    out = read_landing_rows(
        project_id="proj_EXAMPLE",
        relation="managed_feed_ds_EXAMPLE",
        execution_id="dse_LAST",
        classifications=_CLASSIFICATIONS,
    )
    assert out["readable"] is True
    assert out["relation"] == "main.managed_feed_ds_EXAMPLE"
    assert out["row_count"] == 2
    titles = sorted(str(row["titre"]) for row in out["rows"])
    # The earlier import and the other Project are both absent.
    assert titles == ["a", "b"]


def test_a_column_the_mapping_does_not_classify_none_never_leaves(warehouse_file) -> None:
    out = read_landing_rows(
        project_id="proj_EXAMPLE",
        relation="managed_feed_ds_EXAMPLE",
        execution_id="dse_LAST",
        classifications=_CLASSIFICATIONS,
    )
    assert "contact" in out["masked_fields"]
    assert _SECRET not in repr(out)


def test_no_classification_at_all_masks_everything(warehouse_file) -> None:
    out = read_landing_rows(
        project_id="proj_EXAMPLE",
        relation="managed_feed_ds_EXAMPLE",
        execution_id="dse_LAST",
    )
    assert set(out["masked_fields"]) == {
        "project_id",
        "execution_id",
        "titre",
        "contact",
        "vues",
    }


def test_the_reading_is_bounded_and_says_so(warehouse_file) -> None:
    out = read_landing_rows(
        project_id="proj_EXAMPLE",
        relation="managed_feed_ds_EXAMPLE",
        classifications=_CLASSIFICATIONS,
        limit=1,
    )
    assert out["row_count"] == 1
    assert out["truncated"] is True


def test_an_absent_relation_is_named_not_rendered_as_an_empty_file(warehouse_file) -> None:
    out = read_landing_rows(
        project_id="proj_EXAMPLE", relation="managed_feed_ds_NOWHERE"
    )
    assert out["readable"] is False
    assert out["reason"] == RELATION_ABSENT
    # `None`, never `[]`: a relation that is not there is not a file with no row.
    assert out["rows"] is None
    assert out["row_count"] is None
    assert "could not be read" in str(out["message"])


# ---------------------------------------------------------------------------
# Which silence this is.
# ---------------------------------------------------------------------------


class _Cursor:
    def __init__(self, answers: list[tuple[list[str], list[tuple]]]) -> None:
        self._answers = answers
        self.description: list[tuple[str]] = []
        self._rows: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def execute(self, _sql: str, _params: Any = None) -> None:
        names, rows = self._answers.pop(0)
        self.description = [(name,) for name in names]
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _Conn:
    """Answers the module's three statements, in the order it issues them."""

    def __init__(self, *answers: tuple[list[str], list[tuple]]) -> None:
        self._answers = list(answers)

    def cursor(self):
        return _Cursor(self._answers)


_STREAM_COLS = ["source_kind", "config"]
_ARRIVAL_COLS = [
    "id", "filename", "state", "size_bytes", "created_at", "error_code",
    "import_ledger_id",
]
_IMPORT_COLS = [
    "id", "execution_id", "feed_format", "source_metadata", "landing_relation",
    "row_count", "rejected_row_count", "outcome", "error_code",
    "snapshot_observed_at", "created_at",
]


def _read(conn, limit: int = 20) -> dict[str, Any]:
    return read_landed_file(
        conn, project_id="proj_EXAMPLE", datastream_id="ds_EXAMPLE", limit=limit
    )


def test_a_connector_pull_is_refused_before_anything_is_read() -> None:
    out = _read(_Conn((_STREAM_COLS, [("connector_pull", {})])))
    assert out["reason"] == NOT_A_PUSHED_SOURCE
    assert out["rows"] is None


def test_a_file_source_with_nothing_yet_says_so_and_names_its_door() -> None:
    out = _read(
        _Conn(
            (_STREAM_COLS, [("managed_feed", {"channels": ["email"]})]),
            (_ARRIVAL_COLS, []),
            (_IMPORT_COLS, []),
        )
    )
    assert out["reason"] == NO_FILE_YET
    assert out["message"] == "No file has arrived on this Datastream yet."
    # The door travels with the silence; the screen composes no channel of its own.
    assert out["doors"] == {"channels": ["email"], "upload_available": True}
    assert out["row_count"] is None


def test_an_arrival_with_no_import_is_not_the_same_silence() -> None:
    out = _read(
        _Conn(
            (_STREAM_COLS, [("managed_feed", {})]),
            (_ARRIVAL_COLS, [("inbraw_1", "plan.csv", "ACCEPTED", 12, None, None, None)]),
            (_IMPORT_COLS, []),
        )
    )
    assert out["reason"] == ARRIVED_NOT_LANDED
    assert out["arrival"]["filename"] == "plan.csv"
    assert out["message"] != "No file has arrived on this Datastream yet."


def test_a_failed_import_is_broken_and_never_empty() -> None:
    out = _read(
        _Conn(
            (_STREAM_COLS, [("managed_feed", {})]),
            (_ARRIVAL_COLS, []),
            (
                _IMPORT_COLS,
                [
                    (
                        "mfl_1", "dse_1", "csv", {"filename": "plan.csv"}, None,
                        None, 0, "failed", "file_source_drift", None, None,
                    )
                ],
            ),
        )
    )
    assert out["reason"] == IMPORT_FAILED
    assert out["import"]["error_code"] == "file_source_drift"
    # A count nobody measured stays `null` -- never a reassuring 0.
    assert out["import"]["row_count"] is None


def test_a_landed_import_serves_its_rows_masked(warehouse_file, monkeypatch) -> None:
    monkeypatch.setattr(
        "core.datastream_landed_file._classifications",
        lambda conn, **_kw: dict(_CLASSIFICATIONS),
    )
    out = _read(
        _Conn(
            (_STREAM_COLS, [("managed_feed", {"channels": ["upload"]})]),
            (_ARRIVAL_COLS, []),
            (
                _IMPORT_COLS,
                [
                    (
                        "mfl_2", "dse_LAST", "csv", {"filename": "catalogue.csv"},
                        "managed_feed_ds_EXAMPLE", 2, 0, "published", None, None, None,
                    )
                ],
            ),
        )
    )
    assert out["reason"] is None
    assert out["row_count"] == 2
    assert out["import"]["filename"] == "catalogue.csv"
    assert "contact" in out["masked_fields"]
    assert _SECRET not in repr(out)
    # The envelope names what was asked about -- the console refuses it otherwise.
    assert out["project_id"] == "proj_EXAMPLE"
    assert out["datastream_id"] == "ds_EXAMPLE"
