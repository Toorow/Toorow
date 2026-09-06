"""AI-260 second half -- the woven campaign-spend statement, two dialects.

Story 60.3's obligation, inherited in full: the fixture chain of this repository
is DuckDB and production is BigQuery, and the cleanup fragments now travel
INSIDE a served statement (`warehouse._campaign_spend_sql`). A weave valid in
one dialect only would work in a local fixture and fail in production, or the
reverse -- and nobody would learn which until a client did.

So, exactly like `test_cleanup_rule_dialects.py`:

  * the DuckDB emission is EXECUTED, against a real in-memory DuckDB carrying
    the mart's own columns -- including the connector-scoped CASE whose
    parameter DUPLICATION only a counting driver can refuse;
  * the BigQuery emission is checked against the same banned-token discipline,
    because no BigQuery engine can be reached from a test -- plus the
    continuous-numbering rule, because BigQuery binds by NAME and a fragment
    restarting at @p0 inside a statement that already bound @p0 would silently
    reuse the wrong value.
"""

from __future__ import annotations

import os
import re

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core.cleanup_rule_application import (  # noqa: E402
    CleanupRuleApplication,
    compile_predicates,
    compile_projection,
)
from core.warehouse import _campaign_spend_sql  # noqa: E402

#: Same banned lists as `test_cleanup_rule_dialects.py`: what each dialect must
#: NEVER contain -- the other one's vocabulary.
_FOREIGN_TO_BIGQUERY: tuple[tuple[str, str], ...] = (
    (r"\bregexp_matches\b", "regexp_matches is DuckDB; BigQuery has REGEXP_CONTAINS"),
    (r"\bregexp_full_match\b", "regexp_full_match is DuckDB"),
    (r"\bregexp_replace\b", "the lowercase regexp_replace is DuckDB; BigQuery has REGEXP_REPLACE"),
    (r'"', "double-quoted identifiers are DuckDB/Postgres; BigQuery quotes with backticks"),
)

_FOREIGN_TO_DUCKDB: tuple[tuple[str, str], ...] = (
    (r"\bREGEXP_CONTAINS\b", "REGEXP_CONTAINS is BigQuery; DuckDB has regexp_matches"),
    (r"\bREGEXP_REPLACE\b", "the uppercase REGEXP_REPLACE two-argument form is BigQuery"),
    (r"`", "backticks are BigQuery; DuckDB quotes with double quotes"),
    (r"@p\d", "@pN named parameters are BigQuery; DuckDB counts with ?"),
)


def _offenders(sql: str, banned: tuple[tuple[str, str], ...]) -> list[str]:
    return [why for pattern, why in banned if re.search(pattern, sql)]


def _unit(rule_kind, pattern, *, connector=None):
    return {
        "scope": "project" if connector is None else "connector",
        "connector": connector,
        "rule_kind": rule_kind,
        "pattern": pattern,
        "rules": [{"rule_id": "crule_x", "name": "Rule under test"}],
    }


#: One application exercising every shape at once: a project-wide row rule, a
#: connector-scoped row rule, a project-wide strip and a connector-scoped strip
#: -- the last one is the parameter-duplication case only a real DuckDB refuses
#: when it is wrong.
_MIXED = CleanupRuleApplication(
    state="applied",
    project_id="proj_EXAMPLE",
    source_field="campaign_id",
    rules=(
        _unit("exclude_row", "_TEST_"),
        _unit("keep_row", "^BRAND", connector="example_connector"),
        _unit("strip_match", r"\?utm_.*$"),
        _unit("strip_match", "[0-9]{4}", connector="example_connector"),
    ),
)


def _statement(placeholder: str) -> tuple[str, list]:
    return _campaign_spend_sql(
        "",
        placeholder,
        project_id="proj_EXAMPLE",
        start_date="2026-03-01",
        end_date="2026-03-31",
        daily=False,
        application=_MIXED,
    )


def test_neither_emission_carries_the_other_dialects_vocabulary():
    bigquery_sql, _ = _statement("@")
    duckdb_sql, _ = _statement("?")
    assert _offenders(bigquery_sql, _FOREIGN_TO_BIGQUERY) == [], bigquery_sql
    assert _offenders(duckdb_sql, _FOREIGN_TO_DUCKDB) == [], duckdb_sql
    assert "REGEXP_CONTAINS" in bigquery_sql
    assert "regexp_matches" in duckdb_sql


def test_no_pattern_and_no_connector_value_is_ever_inlined():
    """Every client value travels as a bound parameter, in BOTH dialects --
    `warehouse._build_query`'s own sentence."""
    for placeholder in ("@", "?"):
        sql, params = _statement(placeholder)
        for value in ("_TEST_", "^BRAND", r"\?utm_.*$", "[0-9]{4}", "example_connector"):
            assert value in params
            assert value not in sql


def test_bigquery_numbers_one_continuous_sequence_and_binds_each_name_once():
    """Duplicated CASE text may REUSE a name (BigQuery binds by name); what it
    must never do is skip or exceed the params list."""
    sql, params = _statement("@")
    names = {int(name) for name in re.findall(r"@p(\d+)", sql)}
    assert names == set(range(len(params)))


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
        "('proj_EXAMPLE', DATE '2026-03-01', 'example_connector', 'cost',"
        " 'campaign_id', 'BRAND-2026?utm_source=x', 10.0),"
        "('proj_EXAMPLE', DATE '2026-03-02', 'example_connector', 'cost',"
        " 'campaign_id', 'BRAND-', 5.0),"
        "('proj_EXAMPLE', DATE '2026-03-02', 'example_connector', 'cost',"
        " 'campaign_id', 'internal_TEST_run', 3.0),"
        "('proj_EXAMPLE', DATE '2026-03-03', 'example_connector', 'cost',"
        " 'campaign_id', 'other-campaign', 7.0),"
        "('proj_EXAMPLE', DATE '2026-03-03', 'another_connector', 'cost',"
        " 'campaign_id', 'other-campaign-1234', 7.0)"
    )
    try:
        yield connection
    finally:
        connection.close()


def test_the_duckdb_emission_is_accepted_and_right_on_a_real_engine(duckdb_mart):
    """The whole mixed weave, executed. What must come back:

    * `internal_TEST_run` is excluded (project-wide exclude);
    * on `example_connector`, only `BRAND…` rows survive (connector keep) and
      lose their UTM tail and their year (project strip, then connector strip),
      MERGING the two raw identities into one served `BRAND-`;
    * `another_connector` keeps its row untouched -- keep rule and second strip
      are scoped away from it; only the project-wide strip reaches it.
    """
    sql, params = _statement("?")
    rows = duckdb_mart.execute(sql, params).fetchall()
    served = {(row[0], row[1]): row[2] for row in rows}
    assert served == {
        ("example_connector", "BRAND-"): 15_000_000,
        ("another_connector", "other-campaign-1234"): 7_000_000,
    }


def test_the_fragment_compilers_agree_with_the_statement_on_parameter_counts():
    """The seam the statement builder relies on: projection params first, then
    the base three, then the predicate params -- in both dialects."""
    for dialect in ("duckdb", "bigquery"):
        projection, proj_params = compile_projection(_MIXED, dialect=dialect)
        predicate, pred_params = compile_predicates(
            _MIXED, dialect=dialect, first_param=len(proj_params) + 3
        )
        assert projection is not None and predicate is not None
        placeholder = "?" if dialect == "duckdb" else "@"
        _, params = _statement(placeholder)
        assert len(params) == len(proj_params) + 3 + len(pred_params)
    # DuckDB duplicates the inner expression's parameters inside the CASE; the
    # named dialect binds each of its names once. The counts differ, and BOTH
    # are correct -- that is the fact this assertion pins.
    _, duck = compile_projection(_MIXED, dialect="duckdb")
    _, big = compile_projection(_MIXED, dialect="bigquery")
    assert len(duck) > len(big)
