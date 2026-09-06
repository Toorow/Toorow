"""Story 41.6 -- the Tax & Fees warehouse read is allowlisted and parameterized.

I4 (testable acceptance): a relation name outside ``_FEE_TAX_BRIDGE_RELATIONS``
raises ``ValueError`` BEFORE any SQL string is built. The precedent is
``_build_plan_pacing_query`` (``server/core/warehouse.py``, Story 22.4): the
relation is a hard-coded constant and every caller-supplied value is a bound
parameter (HG-3).

These tests build SQL strings only -- they never open DuckDB or BigQuery, so they
run offline. They prove the *shape* of the query, not that the relations exist:
``fee_tax_ladder_daily`` has never executed in any environment available today
(commit 1c78a532's measurement paragraph), which is why the 503 path is the one
the seam test exercises.
"""

from __future__ import annotations

import pytest
from core import warehouse


def test_allowlist_names_exactly_the_four_relations_this_story_reads():
    assert warehouse._FEE_TAX_BRIDGE_RELATIONS == (
        "fee_tax_ladder_rollup",
        "fee_tax_ladder_daily",
        "fee_tax_verification_allocation",
        "fee_tax_revenue_alignment_daily",
    )


@pytest.mark.parametrize(
    "relation",
    [
        "fact_daily_kpi",
        "fee_tax_ladder_rollup; DROP TABLE",
        "fee_tax_rules",
        "",
    ],
)
def test_a_relation_outside_the_allowlist_is_refused(relation):
    with pytest.raises(ValueError):
        warehouse._build_fee_tax_query(
            "main_marts.", relation, "proj_EXAMPLE", "2026-01-01", "2026-01-31"
        )


def test_every_caller_value_is_a_bound_parameter_never_interpolated():
    sql, params = warehouse._build_fee_tax_query(
        "main_marts.",
        "fee_tax_ladder_rollup",
        "proj_EXAMPLE",
        "2026-01-01",
        "2026-01-31",
        rollup_kind="country",
        rollup_key="__unresolved__",
    )
    assert "proj_EXAMPLE" not in sql
    assert "__unresolved__" not in sql
    assert "2026-01-01" not in sql
    assert params == [
        "proj_EXAMPLE",
        "2026-01-01",
        "2026-01-31",
        "country",
        "__unresolved__",
    ]
    assert sql.count("?") == len(params)


def test_bigquery_arm_uses_named_placeholders_in_order():
    sql, params = warehouse._build_fee_tax_query(
        "",
        "fee_tax_ladder_rollup",
        "proj_EXAMPLE",
        "2026-01-01",
        "2026-01-31",
        rollup_kind="global",
        placeholder="@",
    )
    assert "@p0" in sql and "@p1" in sql and "@p2" in sql and "@p3" in sql
    assert params == ["proj_EXAMPLE", "2026-01-01", "2026-01-31", "global"]


def test_rollup_selector_is_only_applied_to_the_rollup_relation():
    sql, params = warehouse._build_fee_tax_query(
        "main_marts.",
        "fee_tax_ladder_daily",
        "proj_EXAMPLE",
        "2026-01-01",
        "2026-01-31",
        rollup_kind="country",
        rollup_key="FR",
    )
    # fee_tax_ladder_daily has no rollup_kind column: the selector must not leak
    # into its WHERE clause (it would be a SQL error, i.e. an opaque 503 for a
    # caller error).
    assert "rollup_kind" not in sql
    assert params == ["proj_EXAMPLE", "2026-01-01", "2026-01-31"]


def test_verification_allocation_keeps_its_null_date_reason_rows():
    """`rule_without_base` rows carry date = NULL on purpose.

    ``fee_tax_verification_allocation.sql``: "it priced nothing on any day, so it
    has no day". A plain BETWEEN would drop exactly the disclosure AC6 requires.
    """
    sql, _params = warehouse._build_fee_tax_query(
        "main_marts.",
        "fee_tax_verification_allocation",
        "proj_EXAMPLE",
        "2026-01-01",
        "2026-01-31",
    )
    assert "date IS NULL" in sql


def test_order_by_comes_from_a_hard_coded_map_per_relation():
    for relation in warehouse._FEE_TAX_BRIDGE_RELATIONS:
        assert relation in warehouse._FEE_TAX_ORDER_BY
        sql, _ = warehouse._build_fee_tax_query(
            "main_marts.", relation, "proj_EXAMPLE", "2026-01-01", "2026-01-31"
        )
        assert f"ORDER BY {warehouse._FEE_TAX_ORDER_BY[relation]}" in sql


def test_missing_duckdb_file_raises_warehouse_unavailable(monkeypatch):
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", "")
    with pytest.raises(warehouse.WarehouseUnavailable):
        warehouse.query_fee_tax_bridge(
            "proj_EXAMPLE", "2026-01-01", "2026-01-31", rollup_kind="global"
        )
    with pytest.raises(warehouse.WarehouseUnavailable):
        warehouse.query_fee_tax_alignment("proj_EXAMPLE", "2026-01-01", "2026-01-31")
    with pytest.raises(warehouse.WarehouseUnavailable):
        warehouse.query_fee_tax_components(
            "proj_EXAMPLE", "2026-01-01", "2026-01-31", component="agency_fee"
        )


def test_unknown_db_mode_is_a_value_error_not_a_silent_empty(monkeypatch):
    monkeypatch.setenv("TOOROW_DB_MODE", "sqlite")
    with pytest.raises(ValueError):
        warehouse.query_fee_tax_bridge(
            "proj_EXAMPLE", "2026-01-01", "2026-01-31", rollup_kind="global"
        )
