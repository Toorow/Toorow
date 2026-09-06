"""The DuckDB-shaped writer that lands into BigQuery.

Nineteen connector modules were converted by changing one line each -- their
landing guard -- because they all go through `open_raw_writer` and then issue
the same three statements. That only holds if the translation is exact, so what
is pinned here is mostly the REFUSALS: a translator that does something
plausible with SQL it half-understood puts wrong data in the warehouse and
reports success, which no amount of log-reading recovers afterwards.

The DDL and INSERT below are the real constants from a shipped connector, not
invented ones.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SERVER = Path(__file__).resolve().parents[2]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

from core.bigquery_raw_writer import BigQueryRawWriter  # noqa: E402
from core.raw_landing import RawLandingError  # noqa: E402

DDL = """
CREATE TABLE IF NOT EXISTS raw_example_daily (
    date       VARCHAR,
    clicks     BIGINT,
    position   DOUBLE,
    pull_id    VARCHAR,
    project_id VARCHAR
)
"""
INSERT = """
INSERT INTO raw_example_daily
    (date, clicks, position, pull_id, project_id)
VALUES (?, ?, ?, ?, ?)
"""
ROW = ("2026-07-29", 5, 1.5, "pull_EXAMPLE", "proj_EXAMPLE")


@pytest.fixture()
def landed(monkeypatch):
    """Capture what would have been landed instead of calling BigQuery."""
    calls = []

    def _fake(table, rows, *, columns, project_id, mode, backend):
        calls.append(
            {"table": table, "rows": rows, "columns": columns, "backend": backend}
        )
        return {"rows": len(rows), "backend": backend, "mode": mode, "table": table}

    monkeypatch.setattr("core.bigquery_raw_writer.land_raw_rows", _fake)
    return calls


def test_the_three_statements_round_trip_to_a_landing(landed):
    writer = BigQueryRawWriter(project_id="proj_EXAMPLE")
    writer.execute(DDL)
    writer.executemany(INSERT, [ROW])
    writer.close()

    assert len(landed) == 1
    call = landed[0]
    assert call["table"] == "raw_example_daily"
    assert call["backend"] == "bigquery"
    # Positional values mapped onto the INSERT's own column list, and the types
    # taken from the CREATE -- BIGINT is an INTEGER, DOUBLE is a FLOAT.
    assert call["rows"] == [
        {
            "date": "2026-07-29",
            "clicks": 5,
            "position": 1.5,
            "pull_id": "pull_EXAMPLE",
            "project_id": "proj_EXAMPLE",
        }
    ]
    assert call["columns"] == [
        ("date", "STRING"),
        ("clicks", "INTEGER"),
        ("position", "FLOAT"),
        ("pull_id", "STRING"),
        ("project_id", "STRING"),
    ]


def test_batches_land_once_per_table_not_once_per_batch(landed):
    writer = BigQueryRawWriter(project_id="p")
    writer.execute(DDL)
    writer.executemany(INSERT, [ROW])
    writer.executemany(INSERT, [ROW, ROW])
    writer.close()
    assert len(landed) == 1, "one load job per pull, not one per batch"
    assert len(landed[0]["rows"]) == 3


def test_add_column_extends_the_schema(landed):
    """The ALTER guards exist because tables predate later migrations."""
    writer = BigQueryRawWriter(project_id="p")
    writer.execute(DDL)
    writer.execute("ALTER TABLE raw_example_daily ADD COLUMN IF NOT EXISTS device VARCHAR")
    writer.executemany(
        "INSERT INTO raw_example_daily (date, device) VALUES (?, ?)",
        [("2026-07-29", "mobile")],
    )
    writer.close()
    assert ("device", "STRING") in landed[0]["columns"]


def test_adding_a_column_twice_does_not_duplicate_it(landed):
    writer = BigQueryRawWriter(project_id="p")
    writer.execute(DDL)
    for _ in range(2):
        writer.execute("ALTER TABLE raw_example_daily ADD COLUMN IF NOT EXISTS device VARCHAR")
    writer.executemany("INSERT INTO raw_example_daily (date) VALUES (?)", [("2026-07-29",)])
    writer.close()
    names = [name for name, _ in landed[0]["columns"]]
    assert names.count("device") == 1


def test_nothing_buffered_lands_nothing(landed):
    writer = BigQueryRawWriter(project_id="p")
    writer.execute(DDL)
    writer.close()
    assert landed == []


def test_close_clears_the_buffer_so_a_reused_writer_cannot_double_land(landed):
    writer = BigQueryRawWriter(project_id="p")
    writer.execute(DDL)
    writer.executemany(INSERT, [ROW])
    writer.close()
    writer.close()
    assert len(landed) == 1


# --------------------------------------------------------------------------
# Refusals. Each of these would otherwise be silently wrong data.
# --------------------------------------------------------------------------


def test_an_unrecognised_statement_is_refused_not_ignored():
    writer = BigQueryRawWriter(project_id="p")
    with pytest.raises(RawLandingError, match="not translatable"):
        writer.execute("DELETE FROM raw_example_daily WHERE date < '2026-01-01'")


def test_a_column_type_with_no_mapping_is_refused():
    """Guessing a type is how a number ends up stored as text."""
    writer = BigQueryRawWriter(project_id="p")
    with pytest.raises(RawLandingError, match="no BigQuery mapping"):
        writer.execute("CREATE TABLE IF NOT EXISTS raw_t (payload STRUCT(a INT))")


def test_a_value_count_mismatch_is_refused():
    writer = BigQueryRawWriter(project_id="p")
    writer.execute(DDL)
    with pytest.raises(RawLandingError, match="values"):
        writer.executemany(INSERT, [("2026-07-29", 5)])


def test_a_literal_in_the_values_list_is_refused():
    """A literal shifts the positional mapping for every row after it."""
    writer = BigQueryRawWriter(project_id="p")
    writer.execute(DDL)
    with pytest.raises(RawLandingError, match="non-parameter"):
        writer.executemany(
            "INSERT INTO raw_example_daily (date, clicks) VALUES (?, 0)",
            [("2026-07-29",)],
        )


def test_inserting_a_column_the_table_never_declared_is_refused(landed):
    """The landing would drop it silently -- short a column, with a green pull."""
    writer = BigQueryRawWriter(project_id="p")
    writer.execute(DDL)
    writer.executemany(
        "INSERT INTO raw_example_daily (date, undeclared) VALUES (?, ?)",
        [("2026-07-29", "x")],
    )
    with pytest.raises(RawLandingError, match="not in the table definition"):
        writer.close()


def test_rows_without_a_create_table_are_refused(landed):
    writer = BigQueryRawWriter(project_id="p")
    writer.executemany(INSERT, [ROW])
    with pytest.raises(RawLandingError, match="no CREATE TABLE seen"):
        writer.close()


def test_a_parameterised_type_is_not_torn_at_its_own_comma(landed):
    writer = BigQueryRawWriter(project_id="p")
    writer.execute("CREATE TABLE IF NOT EXISTS raw_t (amount DECIMAL(18, 2), tag VARCHAR)")
    writer.executemany("INSERT INTO raw_t (amount, tag) VALUES (?, ?)", [(1.5, "x")])
    writer.close()
    assert landed[0]["columns"] == [("amount", "FLOAT"), ("tag", "STRING")]


def test_a_table_constraint_is_refused_rather_than_dropped():
    writer = BigQueryRawWriter(project_id="p")
    with pytest.raises(RawLandingError, match="not translatable"):
        writer.execute("CREATE TABLE IF NOT EXISTS raw_t (id VARCHAR, PRIMARY KEY (id))")


def test_a_boolean_column_keeps_its_type_rather_than_becoming_a_string(landed):
    """BOOLEAN used to map to STRING, which type-checked and then failed on the wire.

    Ten of the connectors converted after the first nineteen declare a
    `non_additive BOOLEAN` column and put a Python `bool` in the tuple. Mapped to
    STRING, storage_write refuses that bool at proto serialisation -- "bad
    argument type for built-in operation" -- and the load path would hand a JSON
    boolean to a STRING column. So the type is carried through, not flattened.
    """
    writer = BigQueryRawWriter(project_id="p")
    writer.execute("CREATE TABLE IF NOT EXISTS raw_t (metric VARCHAR, non_additive BOOLEAN)")
    writer.executemany(
        "INSERT INTO raw_t (metric, non_additive) VALUES (?, ?)", [("ctr", True)]
    )
    writer.close()
    assert landed[0]["columns"] == [("metric", "STRING"), ("non_additive", "BOOLEAN")]
    assert landed[0]["rows"] == [{"metric": "ctr", "non_additive": True}]


def test_a_column_default_is_carried_rather_than_dropped(landed):
    """Eight shipped connectors declare `VARCHAR DEFAULT 'EUR'` and the like.

    Refusing them left those connectors unable to land at all; dropping the
    DEFAULT would have been worse -- a column the INSERT omits takes the default
    on DuckDB and would have landed NULL on BigQuery, with nothing reporting the
    difference. So the default is applied to rows that omit the column.
    """
    writer = BigQueryRawWriter(project_id="p")
    writer.execute(
        "CREATE TABLE IF NOT EXISTS raw_t "
        "(metric VARCHAR, cost_source_currency VARCHAR DEFAULT 'EUR', truncated BOOLEAN "
        "DEFAULT FALSE)"
    )
    writer.executemany("INSERT INTO raw_t (metric) VALUES (?)", [("spend",)])
    writer.close()
    assert landed[0]["columns"] == [
        ("metric", "STRING"),
        ("cost_source_currency", "STRING"),
        ("truncated", "BOOLEAN"),
    ]
    assert landed[0]["rows"] == [
        {"metric": "spend", "cost_source_currency": "EUR", "truncated": False}
    ]


def test_an_explicit_value_wins_over_the_column_default(landed):
    writer = BigQueryRawWriter(project_id="p")
    writer.execute("CREATE TABLE IF NOT EXISTS raw_t (currency VARCHAR DEFAULT 'EUR')")
    writer.executemany("INSERT INTO raw_t (currency) VALUES (?)", [("USD",)])
    writer.close()
    assert landed[0]["rows"] == [{"currency": "USD"}]


def test_a_default_added_by_alter_table_is_carried_too(landed):
    """The ALTER form used to not even parse once a DEFAULT was attached to it."""
    writer = BigQueryRawWriter(project_id="p")
    writer.execute("CREATE TABLE IF NOT EXISTS raw_t (metric VARCHAR)")
    writer.execute(
        "ALTER TABLE raw_t ADD COLUMN IF NOT EXISTS project_id VARCHAR DEFAULT 'default'"
    )
    writer.executemany("INSERT INTO raw_t (metric) VALUES (?)", [("spend",)])
    writer.close()
    assert landed[0]["rows"] == [{"metric": "spend", "project_id": "default"}]


def test_a_default_that_is_an_expression_is_refused_rather_than_evaluated():
    writer = BigQueryRawWriter(project_id="p")
    with pytest.raises(RawLandingError, match="not a literal"):
        writer.execute("CREATE TABLE IF NOT EXISTS raw_t (seen TIMESTAMP DEFAULT now())")


def test_a_json_column_stays_json_rather_than_becoming_a_string(landed):
    """monday declares `column_raw_value JSON` and puts json.dumps() output in it.

    Unmapped, the translator refused the whole connector. Flattened to STRING it
    would have landed, and the warehouse would hold JSON text in a text column --
    queryable only by re-parsing it in every query that touches it. The Storage
    Write API has no JSON wire type, so the proto side sends the JSON TEXT while
    the BigQuery column stays JSON (see _PROTO_TYPES).
    """
    writer = BigQueryRawWriter(project_id="p")
    writer.execute("CREATE TABLE IF NOT EXISTS raw_t (item_id VARCHAR, raw_value JSON)")
    writer.executemany(
        "INSERT INTO raw_t (item_id, raw_value) VALUES (?, ?)", [("1", '{"a":1}')]
    )
    writer.close()
    assert landed[0]["columns"] == [("item_id", "STRING"), ("raw_value", "JSON")]
