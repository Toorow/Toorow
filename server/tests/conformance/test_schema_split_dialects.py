"""Story 70.1 -- one stored schema set, two warehouse dialects, one answer.

WHY THIS FILE EXISTS, AND WHY IT IS A TWIN. `test_cleanup_rule_dialects.py` was
written because a pattern compiled to only one dialect works in a local fixture
and fails in production, and nobody learns which until a client does. A declared
schema set is the same stored declaration compiled the same two ways, so it gets
the same two proofs:

  * the DuckDB half is EXECUTED, against a real in-memory DuckDB carrying real
    placement codes. A string assertion cannot tell a function that exists from
    one that does not, and `list_slice` / `array_to_string` are exactly the kind
    of function a compiler can get wrong in a way that still reads well;
  * the BigQuery half is held to the same banned-token discipline, because no
    BigQuery engine can be reached from a test.

AND IT PROVES ONE THING THE CLEANUP TWIN DID NOT HAVE TO. A schema set is read
TWICE in this repository: `column_treatments.split_row_by_declared_schema`
answers about one string for the screen, and `schema_split_compiler` emits the
SQL the warehouse runs. Two readings of one immutable declaration are two
opinions about rows nobody can re-derive, so the last test here runs both over
the same values and refuses any disagreement.

The codes below are generic on purpose: no client identifier belongs in this
repository, fixtures included.
"""

from __future__ import annotations

import os
import re

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import schema_split_compiler as compiler  # noqa: E402
from core.cleanup_rules import DIALECTS  # noqa: E402
from core.column_treatments import (  # noqa: E402
    read_schema_split,
    split_row_by_declared_schema,
)

#: The reference case's shape, with nothing of the reference case in it: a
#: contract, a fiscal year, a market, an objective, then the publisher code that
#: anchors the detection, then delivery. Two widths, two anchor offsets.
SCHEMA_SET = {
    "name": "placement_code",
    "source": "Placement code",
    "separator": "_",
    "remainder": "placement_remainder",
    "detection": {
        "method": "anchor_min_offset",
        "anchor_tokens": ["PUBALPHA", "PUBBETA"],
    },
    "layouts": [
        {
            "name": "full",
            "anchor_offset": 4,
            "targets": [
                "contract",
                "fiscal_year",
                "market",
                "objective",
                "publisher",
                "channel",
                "creative_format",
            ],
        },
        {
            "name": "short",
            "anchor_offset": 2,
            "targets": ["contract", "market", "publisher", "channel"],
        },
    ],
}

#: One row per thing a schema set has to be able to say.
PLACEMENT_CODES = {
    # The wide layout, exactly as declared.
    "full_exact": "CON_FY26_EU_AWARENESS_PUBALPHA_DISPLAY_BANNER",
    # The narrow layout, in the same column, detected by its own anchor offset.
    "short_exact": "CON_EU_PUBBETA_VIDEO",
    # Wider than the layout: the overflow is the declared remainder, and the row
    # SAYS its count does not match.
    "full_overflow": "CON_FY26_EU_AWARENESS_PUBALPHA_DISPLAY_BANNER_WAVE2_RETARGET",
    # No anchor anywhere: undetected, and still counted.
    "no_anchor": "LEGACY_CODE_WITHOUT_A_PUBLISHER",
    # An anchor closer to the left than any layout declares. The minimum offset
    # designates no layout, so this is undetected -- NOT the next-best layout.
    "anchor_too_early": "PUBALPHA_CON_EU_DISPLAY",
}

_FOREIGN_TO_BIGQUERY: tuple[tuple[str, str], ...] = (
    (r"\bstr_split\b", "str_split is DuckDB; BigQuery has SPLIT"),
    (r"\blist_slice\b", "list_slice is DuckDB; BigQuery slices with UNNEST ... WITH OFFSET"),
    (
        r"\barray_to_string\b",
        "the lowercase array_to_string is DuckDB; BigQuery has ARRAY_TO_STRING",
    ),
    (r"\blen\(", "len() is DuckDB; BigQuery has ARRAY_LENGTH"),
    (r"\bEXCLUDE\b", "EXCLUDE is DuckDB; BigQuery drops a column with EXCEPT"),
    (r"\bAS VARCHAR\b", "VARCHAR is DuckDB; BigQuery casts to STRING"),
    (r'"', "double-quoted identifiers are DuckDB/Postgres; BigQuery quotes with backticks"),
)

_FOREIGN_TO_DUCKDB: tuple[tuple[str, str], ...] = (
    (r"\bSPLIT\(", "SPLIT is BigQuery; DuckDB has str_split"),
    (r"\bSAFE_OFFSET\b", "SAFE_OFFSET is BigQuery; DuckDB indexes a list from 1"),
    (r"\bARRAY_LENGTH\b", "ARRAY_LENGTH is BigQuery; DuckDB has len"),
    (r"\bARRAY_TO_STRING\b", "the upper-case ARRAY_TO_STRING is BigQuery"),
    (r"\bEXCEPT\b", "EXCEPT is BigQuery; DuckDB drops a column with EXCLUDE"),
    (r"\bAS STRING\b", "STRING is BigQuery; DuckDB casts to VARCHAR"),
    (r"`", "backticks are BigQuery; DuckDB quotes with double quotes"),
    (r"@p\d", "@pN named parameters are BigQuery; DuckDB counts with ?"),
)


def _offenders(sql: str, banned: tuple[tuple[str, str], ...]) -> list[str]:
    return [why for pattern, why in banned if re.search(pattern, sql)]


def _compile(dialect: str, *, relation: str = "placements"):
    return compiler.compile_schema_split(
        entry=SCHEMA_SET,
        dialect=dialect,
        relation=relation,
        source_column="placement_code",
    )


# ---------------------------------------------------------------------------
# The two emissions, and the negative that matters
# ---------------------------------------------------------------------------


def test_one_stored_schema_set_compiles_in_both_dialects():
    """The same declaration, read twice, becomes two valid emissions and never
    mixes their vocabularies."""
    emissions = {dialect: _compile(dialect) for dialect in DIALECTS}
    assert set(emissions) == {"bigquery", "duckdb"}

    bigquery = emissions["bigquery"]
    duckdb_sql = emissions["duckdb"]

    assert _offenders(bigquery.sql, _FOREIGN_TO_BIGQUERY) == []
    assert _offenders(duckdb_sql.sql, _FOREIGN_TO_DUCKDB) == []
    assert "SPLIT(" in bigquery.sql
    assert "str_split(" in duckdb_sql.sql

    # The same declaration produces the same columns whichever engine reads it:
    # a warehouse that named its columns differently per dialect would break
    # every reader on the day the fixture chain and production disagreed.
    assert bigquery.columns == duckdb_sql.columns
    assert bigquery.columns == (
        "placement_code_layout",
        "placement_code_token_count",
        "placement_code_token_count_matches_schema",
        "placement_remainder",
        "contract",
        "fiscal_year",
        "market",
        "objective",
        "publisher",
        "channel",
        "creative_format",
    )


def test_no_declared_value_is_ever_written_into_the_sql_string():
    """Separators, anchor tokens and layout names are BOUND, in both dialects.

    They are declaration text a person typed, so a value carrying a quote must
    not be able to end a literal that does not exist.
    """
    declared = {
        SCHEMA_SET["separator"],
        *SCHEMA_SET["detection"]["anchor_tokens"],
        *(layout["name"] for layout in SCHEMA_SET["layouts"]),
    }
    for dialect in DIALECTS:
        compiled = _compile(dialect)
        assert set(compiled.params) == declared
        for value in SCHEMA_SET["detection"]["anchor_tokens"]:
            assert value not in compiled.sql


def test_bigquery_numbers_its_parameters_continuously_across_the_whole_statement():
    """BigQuery binds by NAME, and this statement has three nested levels.

    A level restarting at @p0 would silently reuse the value another level bound
    -- the failure a positional dialect cannot have and a named one can.
    """
    compiled = _compile("bigquery")
    names = re.findall(r"@p(\d+)", compiled.sql)
    assert [int(name) for name in names] == list(range(len(compiled.params)))


def test_a_caller_that_already_bound_values_keeps_one_continuous_numbering():
    compiled = _compile("bigquery")
    shifted = compiler.compile_schema_split(
        entry=SCHEMA_SET,
        dialect="bigquery",
        relation="placements",
        source_column="placement_code",
        first_param=4,
    )
    names = [int(name) for name in re.findall(r"@p(\d+)", shifted.sql)]
    assert names == list(range(4, 4 + len(compiled.params)))


def test_an_unknown_dialect_and_a_broken_relation_are_refused():
    with pytest.raises(compiler.SchemaSplitError):
        compiler.compile_schema_split(
            entry=SCHEMA_SET,
            dialect="sqlite",
            relation="placements",
            source_column="placement_code",
        )
    with pytest.raises(compiler.SchemaSplitError):
        _compile("duckdb", relation="placements; DROP TABLE placements")
    with pytest.raises(Exception):
        compiler.compile_schema_split(
            entry=SCHEMA_SET,
            dialect="duckdb",
            relation="placements",
            source_column='placement_code" --',
        )


# ---------------------------------------------------------------------------
# The DuckDB half, EXECUTED
# ---------------------------------------------------------------------------


@pytest.fixture
def duckdb_placements():
    duckdb = pytest.importorskip("duckdb")
    connection = duckdb.connect(":memory:")
    connection.execute(
        "CREATE TABLE placements (row_key VARCHAR, project_id VARCHAR, placement_code VARCHAR)"
    )
    connection.executemany(
        "INSERT INTO placements VALUES (?, 'proj_EXAMPLE', ?)",
        [[key, code] for key, code in PLACEMENT_CODES.items()],
    )
    try:
        yield connection
    finally:
        connection.close()


def _read(connection) -> dict[str, dict]:
    compiled = _compile("duckdb")
    cursor = connection.execute(compiled.sql, list(compiled.params))
    columns = [description[0] for description in cursor.description]
    return {
        row[columns.index("row_key")]: dict(zip(columns, row, strict=True))
        for row in cursor.fetchall()
    }


def test_two_rows_of_one_column_take_two_different_layouts(duckdb_placements):
    """The whole point of the seventh word: the layout is decided per ROW."""
    rows = _read(duckdb_placements)

    wide = rows["full_exact"]
    assert wide["placement_code_layout"] == "full"
    assert wide["placement_code_token_count"] == 7
    assert wide["placement_code_token_count_matches_schema"] is True
    assert wide["contract"] == "CON"
    assert wide["fiscal_year"] == "FY26"
    assert wide["publisher"] == "PUBALPHA"
    assert wide["creative_format"] == "BANNER"

    narrow = rows["short_exact"]
    assert narrow["placement_code_layout"] == "short"
    assert narrow["placement_code_token_count"] == 4
    assert narrow["placement_code_token_count_matches_schema"] is True
    assert narrow["contract"] == "CON"
    assert narrow["market"] == "EU"
    assert narrow["publisher"] == "PUBBETA"
    # A concept the narrow layout does not describe is empty, not borrowed from
    # the wide one: the row took ONE layout.
    assert narrow["fiscal_year"] is None
    assert narrow["creative_format"] is None


def test_what_exceeds_the_last_position_lands_in_the_declared_remainder(duckdb_placements):
    """From the LEFT, and nothing is truncated in silence."""
    row = _read(duckdb_placements)["full_overflow"]

    assert row["placement_code_layout"] == "full"
    assert row["placement_code_token_count"] == 9
    # The row says its count is not the layout's. It is not repaired into it.
    assert row["placement_code_token_count_matches_schema"] is False
    assert row["placement_remainder"] == "WAVE2_RETARGET"
    # The seven declared positions still hold what they hold.
    assert row["creative_format"] == "BANNER"


def test_a_row_no_layout_detects_stays_whole_and_is_still_counted(duckdb_placements):
    """A default layout would be a wrong answer that looks right.

    Both undetected shapes answer the same way: no layout, no derived value, the
    collected value untouched, and a token count that makes the row listable
    against the ones the set does describe.
    """
    rows = _read(duckdb_placements)

    for key, expected_count in (("no_anchor", 5), ("anchor_too_early", 4)):
        row = rows[key]
        assert row["placement_code_layout"] is None, key
        assert row["placement_code_token_count"] == expected_count, key
        assert row["placement_code_token_count_matches_schema"] is None, key
        assert row["placement_remainder"] is None, key
        assert row["contract"] is None, key
        # The column as it was collected is still there, unchanged.
        assert row["placement_code"] == PLACEMENT_CODES[key], key


def test_the_undetected_rows_are_countable_as_a_group(duckdb_placements):
    """"Comptable" is a number, and this is the query that gives it."""
    compiled = _compile("duckdb")
    sql = (
        "SELECT COUNT(*) FROM (" + compiled.sql + ") AS split "
        'WHERE "placement_code_layout" IS NULL'
    )
    assert duckdb_placements.execute(sql, list(compiled.params)).fetchall() == [(2,)]


def test_the_scaffolding_column_never_reaches_the_reader(duckdb_placements):
    """The token list is how the statement works, not something a person reads."""
    assert "placement_code__tokens" not in next(iter(_read(duckdb_placements).values()))


# ---------------------------------------------------------------------------
# The two readings of one declaration must never disagree
# ---------------------------------------------------------------------------


def test_the_python_reading_and_the_warehouse_reading_agree_row_for_row(duckdb_placements):
    """One immutable declaration, one answer.

    The screen previews a schema set through `split_row_by_declared_schema` and
    the warehouse derives it through the compiled SQL. If those two ever
    diverged, the person would approve one derivation and the marts would carry
    another -- over rows that are already published and cannot be re-derived.
    """
    parsed = read_schema_split(SCHEMA_SET)
    warehouse = _read(duckdb_placements)

    for key, code in PLACEMENT_CODES.items():
        local = split_row_by_declared_schema(parsed, code)
        row = warehouse[key]

        assert row["placement_code_layout"] == local["layout"], key
        assert row["placement_code_token_count"] == local["token_count"], key
        assert (
            row["placement_code_token_count_matches_schema"]
            == local["token_count_matches_schema"]
        ), key
        assert row["placement_remainder"] == local["remainder"], key
        for target in (
            "contract",
            "fiscal_year",
            "market",
            "objective",
            "publisher",
            "channel",
            "creative_format",
        ):
            assert row[target] == local["values"].get(target), (key, target)
