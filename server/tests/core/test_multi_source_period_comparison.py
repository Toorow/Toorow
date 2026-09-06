"""One frozen comparison window, one Result, and server-owned deltas."""

from __future__ import annotations

from datetime import date

import duckdb
import pytest
from core.multi_source_execution import build_sql, result_schema
from core.multi_source_plan import PlanRefused, _compile_comparison
from core.pivot_projection import _complete_comparison_groups, project


def _filters(start: str = "2026-08-01", end: str = "2026-08-07") -> list[dict]:
    return [
        {
            "stage": "pre_aggregation",
            "datastream_id": source,
            "canonical_field_id": "day",
            "operator": operator,
            "value": value,
        }
        for source in ("ds_a", "ds_b")
        for operator, value in (("gte", start), ("lte", end))
    ]


def _comparison_plan() -> dict:
    comparison = _compile_comparison(
        "previous_period",
        grain_field_id="day",
        grain_value_type="date",
        filters=_filters(),
        member_ids={"ds_a", "ds_b"},
    )
    return {
        "primary_datastream_id": "ds_a",
        "inclusion_policy": "matched_only",
        "grain_canonical_field_id": "day",
        "members": [
            {
                "datastream_id": "ds_a",
                "name": "Spend",
                "measures": [{
                    "canonical_field_id": "spend",
                    "physical_field": "spend",
                    "aggregation": "sum",
                    "result_field": "m_spend",
                }],
            },
            {
                "datastream_id": "ds_b",
                "name": "Orders",
                "measures": [{
                    "canonical_field_id": "orders",
                    "physical_field": "orders",
                    "aggregation": "sum",
                    "result_field": "m_orders",
                }],
            },
        ],
        "edges": [{
            "left": "ds_a",
            "right": "ds_b",
            "components": [{"canonical_field_id": "day", "canonical_name": "Day"}],
            "key_paths": [{
                "canonical_field_id": "day",
                "left_field": "day",
                "right_field": "day",
            }],
        }],
        "dimensions": [{"canonical_field_id": "day"}],
        "filters": _filters(),
        "comparison": comparison,
        "bounds": {"row_limit": 100},
    }


def test_previous_period_and_year_freeze_exact_inclusive_dates() -> None:
    previous = _compile_comparison(
        "previous_period",
        grain_field_id="day",
        grain_value_type="date",
        filters=_filters(),
        member_ids={"ds_a", "ds_b"},
    )
    year = _compile_comparison(
        "previous_year",
        grain_field_id="day",
        grain_value_type="date",
        filters=_filters("2024-02-29", "2024-03-02"),
        member_ids={"ds_a", "ds_b"},
    )

    assert previous == {
        "contract_version": "period-comparison.v1",
        "kind": "previous_period",
        "canonical_field_id": "day",
        "period_field": "k_comparison_period",
        "current": {"start": "2026-08-01", "end": "2026-08-07"},
        "baseline": {"start": "2026-07-25", "end": "2026-07-31"},
    }
    assert year["baseline"] == {"start": "2023-02-28", "end": "2023-03-02"}


@pytest.mark.parametrize(
    ("kind", "grain", "value_type", "filters", "code"),
    [
        ("previous_period", None, "date", _filters(), "comparison_grain_required"),
        ("previous_period", "day", "string", _filters(), "comparison_temporal_grain_required"),
        ("previous_period", "day", "date", _filters()[:-1], "comparison_window_required"),
        ("next_period", "day", "date", _filters(), "unknown_comparison"),
        (False, "day", "date", _filters(), "unknown_comparison"),
    ],
)
def test_comparison_refuses_an_unfrozen_or_unknown_window(
    kind: object, grain: str | None, value_type: str, filters: list[dict], code: str
) -> None:
    with pytest.raises(PlanRefused) as excinfo:
        _compile_comparison(
            kind,
            grain_field_id=grain,
            grain_value_type=value_type,
            filters=filters,
            member_ids={"ds_a", "ds_b"},
        )
    assert excinfo.value.code == code


def test_comparison_refuses_conflicting_filters_overlap_and_date_underflow() -> None:
    conflicts = [
        *_filters(),
        {
            "stage": "post_aggregation",
            "datastream_id": None,
            "canonical_field_id": "day",
            "operator": "eq",
            "value": "2026-08-01",
        },
    ]
    with pytest.raises(PlanRefused) as conflict:
        _compile_comparison(
            "previous_period",
            grain_field_id="day",
            grain_value_type="date",
            filters=conflicts,
            member_ids={"ds_a", "ds_b"},
        )
    assert conflict.value.code == "comparison_window_conflict"

    with pytest.raises(PlanRefused) as overlap:
        _compile_comparison(
            "previous_year",
            grain_field_id="day",
            grain_value_type="date",
            filters=_filters("2024-01-01", "2026-01-01"),
            member_ids={"ds_a", "ds_b"},
        )
    assert overlap.value.code == "comparison_windows_overlap"

    with pytest.raises(PlanRefused) as underflow:
        _compile_comparison(
            "previous_period",
            grain_field_id="day",
            grain_value_type="date",
            filters=_filters("0001-01-01", "0001-01-01"),
            member_ids={"ds_a", "ds_b"},
        )
    assert underflow.value.code == "comparison_window_required"


def test_execution_keeps_periods_apart_through_the_join() -> None:
    plan = _comparison_plan()
    sql, params = build_sql(plan, {"ds_a": "spend_rows", "ds_b": "order_rows"})
    con = duckdb.connect()
    con.execute("CREATE TABLE spend_rows(day DATE, spend DOUBLE)")
    con.execute("CREATE TABLE order_rows(day DATE, orders BIGINT)")
    con.executemany(
        "INSERT INTO spend_rows VALUES (?, ?)",
        [(date(2026, 8, 1), 100), (date(2026, 7, 25), 80)],
    )
    con.executemany(
        "INSERT INTO order_rows VALUES (?, ?)",
        [(date(2026, 8, 1), 10), (date(2026, 7, 25), 8)],
    )

    rows = con.execute(sql, params).fetchall()

    assert "k_comparison_period" in sql
    assert "m0.k_comparison_period = m1.k_comparison_period" in sql
    assert rows == [
        (date(2026, 7, 25), "baseline", 80.0, 8),
        (date(2026, 8, 1), "current", 100.0, 10),
    ]
    comparison_field = result_schema(plan)["fields"][1]
    assert comparison_field["name"] == "k_comparison_period"
    assert comparison_field["comparison"] == plan["comparison"]


def test_pivot_returns_server_owned_deltas_and_never_invents_zero_percent() -> None:
    plan = _comparison_plan()
    schema = result_schema(plan)
    schema["fields"].insert(
        1,
        {"name": "k_campaign", "canonical_field_id": "campaign", "role": "dimension"},
    )
    matrix = project(
        result_id="qr_comparison",
        content_hash="a" * 64,
        schema=schema,
        rows=[
            {
                "k_day": "2026-08-01",
                "k_campaign": "A",
                "k_comparison_period": "current",
                "m_spend": 100,
            },
            {
                "k_day": "2026-07-25",
                "k_campaign": "A",
                "k_comparison_period": "baseline",
                "m_spend": 80,
            },
            {
                "k_day": "2026-08-02",
                "k_campaign": "B",
                "k_comparison_period": "current",
                "m_spend": 5,
            },
            {
                "k_day": "2026-07-26",
                "k_campaign": "B",
                "k_comparison_period": "baseline",
                "m_spend": 0,
            },
        ],
        request={"rows": ["k_campaign"], "columns": [], "values": ["m_spend"]},
    )

    assert matrix["column_fields"] == ["k_comparison_period"]
    assert matrix["comparison"]["current"] == plan["comparison"]["current"]
    deltas = matrix["comparison"]["deltas"]
    assert deltas[0]["absolute_delta"] == {"value": 20}
    assert deltas[0]["relative_delta"] == {"value": 0.25}
    assert deltas[1]["absolute_delta"] == {"value": 5}
    assert deltas[1]["relative_delta"] == {
        "value": None,
        "absent_reason": "baseline_zero",
    }


def test_comparison_keeps_period_pairs_together_and_names_empty_evidence() -> None:
    keys = [
        ("A", "baseline"),
        ("A", "current"),
        ("B", "baseline"),
        ("B", "current"),
    ]
    assert _complete_comparison_groups(keys, 1, 3) == keys[:2]

    plan = _comparison_plan()
    matrix = project(
        result_id="qr_empty_comparison",
        content_hash="b" * 64,
        schema=result_schema(plan),
        rows=[],
        request={"rows": [], "columns": [], "values": ["m_spend"]},
    )
    assert matrix["comparison"]["deltas"] == []
    assert matrix["comparison"]["unavailable_reason"] == "comparison_value_missing"
