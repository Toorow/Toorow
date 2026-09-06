"""Story 60.3 -- one stored pattern, two warehouse dialects.

WHY THIS FILE EXISTS. The fixture chain of this repository is DuckDB and
production is BigQuery. The repository already polices the mixture in one place:
`tests/core/test_fee_tax_geo_bridge.py:1178` refuses `regexp_full_match` (DuckDB)
where BigQuery wants `REGEXP_CONTAINS`. A cleanup rule compiled to only one of
the two would work in a local fixture and fail in production, or the reverse --
and nobody would learn which until a client did.

So the rule stores a PATTERN, and this file proves the same stored pattern
becomes valid SQL on both sides:

  * the DuckDB half is EXECUTED, against a real in-memory DuckDB carrying the
    mart's own columns. A string assertion cannot tell a function that exists
    from one that does not;
  * the BigQuery half is checked against the same banned-token discipline as the
    guard above, because no BigQuery engine can be reached from a test.

It also proves the negative that matters: neither emission contains the other
dialect's vocabulary.
"""

from __future__ import annotations

import os
import re

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import cleanup_rules as store  # noqa: E402

#: Patterns a client could really type, one per kind of thing a cleanup rule is
#: for: a nomenclature marker, an anchored prefix, a UTM tail, a character class.
STORED_PATTERNS = ("_TEST_", "^BRAND[-_]", r"\?utm_.*$", "[0-9]{4}")

#: What each dialect must NEVER contain: the other one's vocabulary. Same shape
#: as the banned list of `test_fee_tax_geo_bridge.py:1166-1183`.
_FOREIGN_TO_BIGQUERY: tuple[tuple[str, str], ...] = (
    (r"\bregexp_matches\b", "regexp_matches is DuckDB; BigQuery has REGEXP_CONTAINS"),
    (r"\bregexp_full_match\b", "regexp_full_match is DuckDB"),
    (r"\bregexp_replace\b", "the lowercase regexp_replace is DuckDB; BigQuery has REGEXP_REPLACE"),
    (r'"', "double-quoted identifiers are DuckDB/Postgres; BigQuery quotes with backticks"),
)

_FOREIGN_TO_DUCKDB: tuple[tuple[str, str], ...] = (
    (r"\bREGEXP_CONTAINS\b", "REGEXP_CONTAINS is BigQuery; DuckDB has regexp_matches"),
    (r"`", "backticks are BigQuery; DuckDB quotes with double quotes"),
    (r"@p\d", "@pN named parameters are BigQuery; DuckDB counts with ?"),
)


def _offenders(sql: str, banned: tuple[tuple[str, str], ...]) -> list[str]:
    return [why for pattern, why in banned if re.search(pattern, sql)]


@pytest.mark.parametrize("pattern", STORED_PATTERNS)
@pytest.mark.parametrize("rule_kind", store.RULE_KINDS)
def test_one_stored_pattern_compiles_in_both_dialects(rule_kind, pattern):
    """The same row, read twice, becomes two valid emissions and never mixes them."""
    emissions = {
        dialect: store.compile_rule(
            rule_kind=rule_kind,
            source_field="campaign_name",
            pattern=pattern,
            dialect=dialect,
        )
        for dialect in store.DIALECTS
    }
    assert set(emissions) == {"bigquery", "duckdb"}

    bigquery = emissions["bigquery"]
    duckdb_sql = emissions["duckdb"]

    assert _offenders(bigquery.match_sql, _FOREIGN_TO_BIGQUERY) == []
    assert _offenders(duckdb_sql.match_sql, _FOREIGN_TO_DUCKDB) == []
    assert "REGEXP_CONTAINS" in bigquery.match_sql
    assert "regexp_matches" in duckdb_sql.match_sql

    # The pattern itself is bound, not written, in BOTH -- so a pattern with a
    # quote cannot end a literal that does not exist.
    assert bigquery.match_params == duckdb_sql.match_params == (pattern,)
    assert pattern not in bigquery.match_sql
    assert pattern not in duckdb_sql.match_sql
    # A rule that rewrites a field binds the pattern a SECOND time, because the
    # projection is a second fragment and DuckDB counts its placeholders.
    expected = (pattern,) if rule_kind == "strip_match" else ()
    assert bigquery.projection_params == duckdb_sql.projection_params == expected


@pytest.mark.parametrize("rule_kind", store.RULE_KINDS)
def test_the_full_statements_carry_no_foreign_vocabulary_either(rule_kind):
    """The fragment is not the whole story: the dry run, the preview and the
    effect count are three statements, and each is emitted per dialect."""
    for dialect, banned in (("bigquery", _FOREIGN_TO_BIGQUERY), ("duckdb", _FOREIGN_TO_DUCKDB)):
        statements = [
            store.build_dry_run_sql(
                rule_kind=rule_kind,
                source_field="campaign_name",
                pattern="_TEST_",
                dialect=dialect,
                mart_prefix="main_marts.",
            )[0],
            store.build_preview_sql(
                rule_kind=rule_kind,
                source_field="campaign_name",
                pattern="_TEST_",
                dialect=dialect,
                mart_prefix="main_marts.",
                project_id="proj_EXAMPLE",
            )[0],
            store.build_effect_sql(
                rule_kind=rule_kind,
                source_field="campaign_name",
                pattern="_TEST_",
                dialect=dialect,
                mart_prefix="main_marts.",
                project_id="proj_EXAMPLE",
                start_date="2026-07-10",
                end_date="2026-08-08",
            )[0],
        ]
        for sql in statements:
            assert _offenders(sql, banned) == [], f"{dialect}: {sql}"


def test_bigquery_numbers_its_parameters_continuously_across_a_whole_statement():
    """BigQuery binds by NAME. A fragment restarting at @p0 inside a statement that
    already bound @p0 would silently reuse the wrong value -- the failure a
    positional dialect cannot have and a named one can."""
    sql, params = store.build_effect_sql(
        rule_kind="exclude_row",
        source_field="campaign_name",
        pattern="_TEST_",
        dialect="bigquery",
        mart_prefix="main_marts.",
        project_id="proj_EXAMPLE",
        start_date="2026-07-10",
        end_date="2026-08-08",
    )
    names = re.findall(r"@p(\d+)", sql)
    assert [int(name) for name in names] == list(range(len(params)))


# ---------------------------------------------------------------------------
# The DuckDB half, EXECUTED. A string assertion cannot tell a function that
# exists from one that does not.
# ---------------------------------------------------------------------------


@pytest.fixture
def duckdb_mart():
    duckdb = pytest.importorskip("duckdb")
    connection = duckdb.connect(":memory:")
    connection.execute(
        "CREATE TABLE fact_daily_kpi ("
        "  project_id VARCHAR, date DATE, connector VARCHAR, metric VARCHAR,"
        "  breakdown_dimension VARCHAR, breakdown_value VARCHAR, value DOUBLE)"
    )
    connection.execute(
        "INSERT INTO fact_daily_kpi VALUES "
        "('proj_EXAMPLE', DATE '2026-08-01', 'example_connector', 'clicks',"
        " 'campaign_name', 'BRAND-summer', 10.0),"
        "('proj_EXAMPLE', DATE '2026-08-01', 'example_connector', 'clicks',"
        " 'campaign_name', 'internal_TEST_run', 3.0),"
        "('proj_EXAMPLE', DATE '2026-08-02', 'example_connector', 'clicks',"
        " 'campaign_name', 'BRAND-autumn?utm_source=x', 7.0)"
    )
    try:
        yield connection
    finally:
        connection.close()


@pytest.mark.parametrize("rule_kind", store.RULE_KINDS)
def test_the_duckdb_emission_is_accepted_by_a_real_duckdb(duckdb_mart, rule_kind):
    """The dry run reads no row and still proves the statement parses."""
    sql, params = store.build_dry_run_sql(
        rule_kind=rule_kind,
        source_field="campaign_name",
        pattern="_TEST_",
        dialect="duckdb",
        mart_prefix="",
    )
    assert duckdb_mart.execute(sql, params).fetchall() == []


def test_the_effect_counted_by_a_real_engine_is_the_number_of_rows_removed(duckdb_mart):
    """Three rows, one of them a `_TEST_` campaign: the rule removes exactly one.

    This is the number the surface calls the measured effect, and it exists only
    because the rule applies at READ -- a row a provider filtered out at
    collection would never have reached this relation to be counted.
    """
    sql, params = store.build_effect_sql(
        rule_kind="exclude_row",
        source_field="campaign_name",
        pattern="_TEST_",
        dialect="duckdb",
        mart_prefix="",
        project_id="proj_EXAMPLE",
        start_date="2026-08-01",
        end_date="2026-08-31",
    )
    assert duckdb_mart.execute(sql, params).fetchall() == [(1,)]

    # The opposite rule keeps the same one row, so it removes the other two.
    sql, params = store.build_effect_sql(
        rule_kind="keep_row",
        source_field="campaign_name",
        pattern="_TEST_",
        dialect="duckdb",
        mart_prefix="",
        project_id="proj_EXAMPLE",
        start_date="2026-08-01",
        end_date="2026-08-31",
    )
    assert duckdb_mart.execute(sql, params).fetchall() == [(2,)]


def test_a_strip_rule_rewrites_the_value_and_a_real_engine_shows_what_it_becomes(
    duckdb_mart,
):
    sql, params = store.build_preview_sql(
        rule_kind="strip_match",
        source_field="campaign_name",
        pattern=r"\?utm_.*$",
        dialect="duckdb",
        mart_prefix="",
        project_id="proj_EXAMPLE",
        limit=10,
    )
    rows = {row[0]: row[2] for row in duckdb_mart.execute(sql, params).fetchall()}
    assert rows["BRAND-autumn?utm_source=x"] == "BRAND-autumn"
    # A value the pattern does not touch comes back unchanged -- nothing is
    # rewritten by accident.
    assert rows["BRAND-summer"] == "BRAND-summer"
