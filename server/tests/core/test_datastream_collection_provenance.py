"""The extraction instant, READ WHERE THE ROW IS -- 2026-08-12.

Jean asked to « simplement ajouter la date de l'extraction dans le report ». The
measurement that shaped this module is that nothing had to be added: 38 of the 39
connector modules already stamp `loaded_at` and `pull_id` onto every row they
land, 47 of the 54 staging models carry them, and `fact_daily_kpi` selects
`MAX(loaded_at)` in each of its 34 blocks. What was missing was the reading.

WHAT THESE TESTS HOLD is the set of ways such a reading ships green while lying:

  * an instant COMPOSED for a screen -- from `app.pull_jobs`, from a ledger, from
    `Date.now()` -- instead of read off the row a person is looking at;
  * a warehouse that could not be read served as "nothing was collected", which
    sends a person to fix a collection that is working;
  * a connector the mart never held answered with a zero instead of an absence;
  * the STRONGER promise printed: "this is when the row was read", when the
    measured grain is the collection and every row of one pull shares its value;
  * a collection field offered as SELECTABLE, which would compose a plan
    `datastream_intents` refuses as `unknown_report_field`.

DRIVEN AGAINST A REAL DUCKDB MART, on the pattern of
`tests/core/test_daily_row_counts_reader.py` -- a temp origin playing
`main_marts.*`, no Postgres. **The BigQuery branch is NOT exercised and that is
stated rather than implied**: it needs a GCP dataset and credentials, this
repository has no connector test accounts, and a mocked client would prove only
that the test can write BigQuery SQL. What holds the two dialects in step is that
they answer one shape from one function, and that shape is pinned below.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from core.datastream_collection_provenance import (
    COLLECTION_FIELDS,
    declared_collection_fields,
    read_collection_provenance,
)

# The mart's own shape. `loaded_at` and `pull_id` are TEXT because that is what
# the connectors write: `raw_landing._DUCKDB_TYPES` maps the declared `TIMESTAMP`
# onto `VARCHAR`, and every module stamps an ISO-8601 string.
_FACT_DDL = """
    CREATE TABLE main_marts.fact_daily_kpi (
        project_id          TEXT,
        date                DATE,
        connector           TEXT,
        metric              TEXT,
        breakdown_dimension TEXT,
        breakdown_value     TEXT,
        value               DOUBLE,
        pull_id             TEXT,
        loaded_at           TEXT
    )
"""

_BASE = date(2026, 7, 1)
#: TWO collections, and the second one lands rows for TWO days. That asymmetry is
#: what makes the measured grain visible: 2 distinct instants over 5 rows, so a
#: reader that claimed one instant per row would be caught by the counts.
_COLLECTIONS = (
    ("pull_A", "2026-07-01T02:06:00Z", (0,)),
    ("pull_B", "2026-07-02T02:07:31Z", (0, 1)),
)


def _seed(path: str, *, empty: bool = False, drop_columns: bool = False) -> None:
    import duckdb

    con = duckdb.connect(path)
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
        if drop_columns:
            # A mart built without the provenance columns. Not a missing row: a
            # relation that cannot answer this question, and it must say which.
            con.execute(
                "CREATE TABLE main_marts.fact_daily_kpi ("
                "project_id TEXT, date DATE, connector TEXT, metric TEXT, value DOUBLE)"
            )
            return
        con.execute(_FACT_DDL)
        if empty:
            return
        rows = []
        for pull_id, loaded_at, offsets in _COLLECTIONS:
            for offset in offsets:
                day = _BASE + timedelta(days=offset)
                for metric in ("clicks", "impressions"):
                    rows.append(
                        ("p1", day, "meta-ads", metric, "campaign_id", "c1",
                         1.0, pull_id, loaded_at)
                    )
        # AD-5 and connector isolation: another project and another connector, both
        # with instants that would be visibly wrong if either leaked.
        rows.append(("p2", _BASE, "meta-ads", "clicks", "d", "v", 1.0,
                     "pull_OTHER_PROJECT", "2020-01-01T00:00:00Z"))
        rows.append(("p1", _BASE, "google-analytics", "clicks", "d", "v", 1.0,
                     "pull_OTHER_CONNECTOR", "2019-01-01T00:00:00Z"))
        con.executemany(
            "INSERT INTO main_marts.fact_daily_kpi VALUES (?,?,?,?,?,?,?,?,?)", rows
        )
    finally:
        con.close()


@pytest.fixture
def mart(tmp_path, monkeypatch):
    origin = str(tmp_path / "origin_local.duckdb")
    _seed(origin)
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", origin)
    monkeypatch.setenv("TOOROW_CACHE_ENABLED", "false")
    return origin


# ---------------------------------------------------------------------------
# The reading.
# ---------------------------------------------------------------------------


def test_the_instant_shown_is_the_one_on_the_row_not_one_composed_elsewhere(mart) -> None:
    """The value comes off `fact_daily_kpi`, beside the row it belongs to.

    This is the whole point of the module. A completion time joined from
    `app.pull_jobs` is a different claim -- when the WORK ended, not what was
    WRITTEN on the row -- and it would render identically.
    """
    answer = read_collection_provenance(project_id="p1", connector="meta-ads")

    assert answer["state"] == "observed"
    assert answer["rows"], "a mart with rows must yield rows to read"
    instants = {row["loaded_at"] for row in answer["rows"]}
    collections = {row["pull_id"] for row in answer["rows"]}
    # Exactly the values seeded, unreformatted: no timezone assumed, no display
    # format chosen in a reader.
    assert instants <= {"2026-07-01T02:06:00Z", "2026-07-02T02:07:31Z"}
    assert collections <= {"pull_A", "pull_B"}
    # The newest collection first: a reading of five rows must show the most
    # recent one, not whichever the storage engine returned first.
    assert answer["rows"][0]["loaded_at"] == "2026-07-02T02:07:31Z"
    # Every row carries BOTH, because the pair is the answer: the instant alone
    # cannot be told apart from a per-row timestamp.
    for row in answer["rows"]:
        assert row["loaded_at"] and row["pull_id"]
        assert row["date"] and row["metric"]


def test_another_project_and_another_connector_never_leak_into_the_reading(mart) -> None:
    """Both seeded intruders carry instants from years earlier -- visible if they leak."""
    answer = read_collection_provenance(project_id="p1", connector="meta-ads")

    shown = {row["pull_id"] for row in answer["rows"]}
    assert "pull_OTHER_PROJECT" not in shown
    assert "pull_OTHER_CONNECTOR" not in shown
    # And the counts are scoped too: they are read in a separate statement, so a
    # filter forgotten there would inflate the grain sentence while the sample
    # stayed correct.
    assert answer["distinct_collections"] == 2
    assert answer["distinct_instants"] == 2


def test_the_grain_is_the_collection_and_the_sentence_is_measured_not_asserted(mart) -> None:
    """8 rows of this connector, 2 collections, 2 instants.

    Measured the same way on a real built mart (`.task-tmp-3812/ai63/ai63.duckdb`,
    6861 rows): `count(distinct loaded_at)` equalled `count(distinct pull_id)` for
    every connector. So a row carries the moment its COLLECTION ran -- the weaker
    promise -- and the sentence must be readable as that and not as "when this row
    was read".
    """
    answer = read_collection_provenance(project_id="p1", connector="meta-ads")

    assert answer["distinct_instants"] == answer["distinct_collections"] == 2
    note = answer["grain_note"]
    assert "belongs to the collection, not to the row" in note
    assert "2 distinct instant(s) over 2 collection(s)" in note


def test_a_connector_the_mart_never_held_is_an_absence_never_a_zero(mart) -> None:
    """`fact_daily_kpi` is built from a subset of the 39 connectors.

    Answering "0 instants" for a connector the mart cannot model reads as "your
    collection wrote nothing", and sends a person to repair a collection that is
    fine. The two are different states with different sentences.
    """
    answer = read_collection_provenance(project_id="p1", connector="tiktok-ads")

    assert answer["state"] == "connector_absent"
    assert answer["rows"] == []
    assert answer["distinct_instants"] is None
    assert "never carried a row for this connector" in answer["reason"]


def test_an_empty_mart_says_nothing_landed_rather_than_serving_an_instant(
    tmp_path, monkeypatch
) -> None:
    origin = str(tmp_path / "empty.duckdb")
    _seed(origin, empty=True)
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", origin)
    monkeypatch.setenv("TOOROW_CACHE_ENABLED", "false")

    answer = read_collection_provenance(project_id="p1", connector="meta-ads")

    assert answer["state"] == "connector_absent"
    assert answer["rows"] == []


def test_a_mart_without_the_provenance_columns_names_the_column_it_lacks(
    tmp_path, monkeypatch
) -> None:
    """A relation that cannot answer, distinguished from one with nothing to say."""
    origin = str(tmp_path / "old.duckdb")
    _seed(origin, drop_columns=True)
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", origin)
    monkeypatch.setenv("TOOROW_CACHE_ENABLED", "false")

    answer = read_collection_provenance(project_id="p1", connector="meta-ads")

    assert answer["state"] == "relation_absent"
    assert "loaded_at" in answer["reason"] and "pull_id" in answer["reason"]


def test_a_warehouse_that_cannot_be_read_is_never_served_as_no_row(monkeypatch) -> None:
    """The failure this module exists to avoid printing as an empty state.

    An unreadable warehouse and an empty one send a person to two different
    places, so they are two states and neither is the other's fallback.
    """
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", "/nonexistent/dir/that/cannot/open.duckdb")
    monkeypatch.setenv("TOOROW_CACHE_ENABLED", "false")

    answer = read_collection_provenance(project_id="p1", connector="meta-ads")

    assert answer["state"] in {"unreadable", "relation_absent"}
    assert answer["rows"] == []
    # Whatever it is, it is NOT the sentence that blames the collection.
    assert "never carried a row" not in (answer["reason"] or "")


def test_a_file_source_is_told_nothing_stamps_an_instant_rather_than_read_the_mart(
    monkeypatch
) -> None:
    """A `managed_feed` runs no pull, so no collection writes these columns.

    Reading the warehouse for it would be a round trip whose only possible answer
    is an absence dressed as a measurement.
    """
    def _explode(*_args, **_kwargs):  # pragma: no cover -- must never be reached
        raise AssertionError("the warehouse was opened for a source that pulls nothing")

    from core import warehouse

    monkeypatch.setattr(warehouse, "_db_mode", _explode)

    for kind in ("managed_feed", "external_bq"):
        answer = read_collection_provenance(
            project_id="p1", connector="", source_kind=kind
        )
        assert answer["state"] == "not_applicable"
        assert "pulls from no connector" in answer["reason"]


# ---------------------------------------------------------------------------
# The declaration.
# ---------------------------------------------------------------------------


def test_no_collection_field_is_ever_offered_as_selectable() -> None:
    """`datastream_intents` refuses a selection field the report does not declare.

    No manifest declares `loaded_at`, so a checkbox that added it to
    `source.selection` would compose a plan the validator answers
    `unknown_report_field` to -- a control that looks like it works and produces a
    version nothing can execute.
    """
    fields = declared_collection_fields()

    assert [field["field_id"] for field in fields] == ["loaded_at", "pull_id"]
    for field in fields:
        assert field["selectable"] is False
        assert field["always_collected"] is True
        assert field["reason"], "a refusal without its reason is a control that vanished"
        # The words are the reader's, not the column's: a person does not call a
        # moment `loaded_at`.
        assert field["label"] != field["field_id"]
        assert field["description"]


def test_the_declaration_is_free_of_every_store() -> None:
    """It reads no manifest, no warehouse and no database, and that is why it
    survives an unreadable connector registry AND an unreachable warehouse."""
    from core import warehouse

    original = warehouse._db_mode
    try:
        def _explode(*_args, **_kwargs):  # pragma: no cover
            raise AssertionError("the declaration opened the warehouse")

        warehouse._db_mode = _explode  # type: ignore[assignment]
        assert len(declared_collection_fields()) == len(COLLECTION_FIELDS)
    finally:
        warehouse._db_mode = original  # type: ignore[assignment]


def test_the_catalogue_carries_the_declaration_even_when_the_manifest_is_unreadable(
    tmp_path, monkeypatch
) -> None:
    """What the collection writes is known from the landing code, not the manifest.

    An unreadable registry makes the PROVIDER's catalogue unknown; it leaves this
    one exactly as certain, and hiding it there would be the amendment-7 defect
    (a control that silently disappears).
    """
    from core import context_seed
    from core.datastream_source_catalogue import read_source_catalogue

    monkeypatch.setattr(context_seed, "DEFAULT_MODULES_DIR", tmp_path)

    catalogue = read_source_catalogue(
        source_kind="connector_pull", module_name="no-such-connector", report_id="whatever"
    )

    assert catalogue["state"] == "connector_unreadable"
    assert [field["field_id"] for field in catalogue["collection_fields"]] == [
        "loaded_at",
        "pull_id",
    ]


def test_a_file_source_catalogue_declares_no_collection_field() -> None:
    """Nothing pulls, so nothing stamps: the group must not appear at all."""
    from core.datastream_source_catalogue import read_source_catalogue

    catalogue = read_source_catalogue(
        source_kind="managed_feed", module_name=None, report_id=None
    )

    assert catalogue["state"] == "no_module"
    assert "collection_fields" not in catalogue
