"""The executor consumes the frozen relationship tree, not a sorted member list."""

from __future__ import annotations

import pytest
from core.multi_source_execution import (
    MAX_MULTI_SOURCE_ROWS_BYTES,
    ExecutionRefused,
    _fit_result_rows,
    build_sql,
    result_schema,
)


def _edge(left: str, right: str, field: str, left_column: str, right_column: str) -> dict:
    return {
        "left": left,
        "right": right,
        "common_key_version_id": f"mckv_{field}",
        "components": [{"canonical_field_id": field, "canonical_name": field}],
        "key_paths": [
            {
                "canonical_field_id": field,
                "left_field": left_column,
                "right_field": right_column,
            }
        ],
    }


def _member(source: str, field: str, result_field: str | None = None) -> dict:
    measure = {
        "canonical_field_id": field,
        "physical_field": field,
        "aggregation": "sum",
    }
    if result_field:
        measure["result_field"] = result_field
    return {"datastream_id": source, "name": source, "measures": [measure]}


def _plan() -> dict:
    return {
        "primary_datastream_id": "ds_a",
        "inclusion_policy": "matched_only",
        "members": [
            _member("ds_a", "spend"),
            _member("ds_b", "sessions"),
            _member("ds_c", "revenue"),
        ],
        "edges": [
            _edge("ds_b", "ds_c", "campaign", "campaign_b", "campaign_c"),
            _edge("ds_a", "ds_b", "day", "day_a", "day_b"),
        ],
        "dimensions": [
            {"canonical_field_id": "day"},
            {"canonical_field_id": "campaign"},
        ],
        "grain": "day",
        "grain_canonical_field_id": "day",
        "filters": [],
        "bounds": {"row_limit": 100},
    }


def test_a_measure_is_refused_at_a_foreign_multi_hop_grain() -> None:
    with pytest.raises(ExecutionRefused) as excinfo:
        build_sql(
            _plan(), {"ds_a": "a_rows", "ds_b": "b_rows", "ds_c": "c_rows"}
        )

    assert excinfo.value.code == "measure_not_defined_at_output_grain"
    assert excinfo.value.detail == {
        "datastream_id": "ds_a",
        "missing_dimensions": ["campaign"],
    }


def test_explicit_dimensions_are_the_only_join_keys_exposed_by_the_result() -> None:
    plan = _plan()
    plan["dimensions"] = [{"canonical_field_id": "campaign"}]

    names = [field["name"] for field in result_schema(plan)["fields"]]

    assert names[:2] == ["k_day", "k_campaign"]


def test_duplicate_canonical_measures_keep_source_qualified_result_fields() -> None:
    plan = _plan()
    plan["dimensions"] = [{"canonical_field_id": "day"}]
    plan["members"][0] = _member("ds_a", "revenue", "m_revenue__source_a")
    plan["members"][1] = _member("ds_b", "revenue", "m_revenue__source_b")
    plan["members"][2]["measures"] = []

    sql, _params = build_sql(
        plan, {"ds_a": "a_rows", "ds_b": "b_rows", "ds_c": "c_rows"}
    )
    fields = result_schema(plan)["fields"]

    assert "AS m_revenue__source_a" in sql
    assert "AS m_revenue__source_b" in sql
    assert [field["name"] for field in fields].count("m_revenue__source_a") == 1
    assert [field["name"] for field in fields].count("m_revenue__source_b") == 1
    assert {field.get("datastream_id") for field in fields if field["role"] == "measure"} == {
        "ds_a",
        "ds_b",
    }


def test_result_payload_budget_keeps_whole_rows_and_refuses_one_oversized_row() -> None:
    rows = [{"label": "a" * 800_000}, {"label": "b" * 800_000}]

    fitted, truncated = _fit_result_rows(rows)

    assert fitted == rows[:1]
    assert truncated is True
    with pytest.raises(ExecutionRefused) as excinfo:
        _fit_result_rows([{"label": "x" * MAX_MULTI_SOURCE_ROWS_BYTES}])
    assert excinfo.value.code == "result_row_exceeds_budget"


def test_post_aggregation_filter_runs_on_the_unambiguous_merged_projection() -> None:
    plan = _plan()
    plan["dimensions"] = [{"canonical_field_id": "day"}]
    plan["members"][2]["measures"] = []
    plan["filters"] = [{
        "stage": "post_aggregation",
        "canonical_field_id": "day",
        "operator": "eq",
        "value": "2026-08-13",
    }]

    sql, params = build_sql(
        plan, {"ds_a": "a_rows", "ds_b": "b_rows", "ds_c": "c_rows"}
    )

    assert ", merged AS (SELECT " in sql
    assert "SELECT * FROM merged WHERE k_day = ?" in sql
    assert params == ["2026-08-13"]
    assert "LIMIT 101" in sql


def test_a_long_landing_member_is_read_as_the_single_source_path_reads_it() -> None:
    """execution-substrate.md, 2026-09-05. Measured on the reference project: the
    first governed crossing died on `Unrecognized name: estimated_minutes_watched`
    because the member was read as `SUM(<field>) FROM <candidate table>`. With the
    member's shape, the measure that landed as a ROW is summed under a condition
    on its own name, the read supersedes earlier pulls, and the shared landing is
    restricted to this profile's grain -- and a member without a shape is read
    plainly, as before."""
    plan = _plan()
    plan["dimensions"] = [{"canonical_field_id": "day"}]
    plan["members"][0] = _member("ds_a", "minutes")
    plan["members"][2]["measures"] = []
    relations = {"ds_a": "raw_ds.raw_youtube_daily", "ds_b": "b_rows", "ds_c": "c_rows"}
    shapes = {
        "ds_a": {
            "present_columns": ["channel_id", "day_a", "loaded_at", "metric", "pull_id", "value", "video"],
            "long_form": ("metric", "value"),
            "grain_columns": [],
            "grain_restrictions": [("video", "=")],
        }
    }

    sql, params = build_sql(plan, relations, shapes)

    assert "SUM(IF(metric = ?, value, NULL)) AS m_minutes" in sql
    assert "SUM(minutes)" not in sql
    assert "QUALIFY ROW_NUMBER() OVER (PARTITION BY channel_id, day_a, metric, video ORDER BY loaded_at DESC) = 1" in sql
    assert "video = ?" in sql
    assert params[:2] == ["minutes", ""]  # the SELECT's bind, then the WHERE's, in text order
    assert "SUM(sessions) AS m_sessions" in sql  # a member without a shape is unchanged
    assert "FROM b_rows" in sql
