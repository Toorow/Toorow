"""The pivot door over a real executed Result (story 66.6).

The projection itself is proven pure. What needs a real database is the join
this door makes: a Result row, its payload, and the scope that decides whether
this caller may see either. And one property no unit test can state -- the pivot
of a Result produced by 66.5 has the columns 66.5 wrote, because both read the
same schema.
"""

from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")
duckdb = pytest.importorskip("duckdb")

from core import (  # noqa: E402
    match_profile,  # noqa: E402
    pivot_api,
    pivot_projection,
)
from core import mdm_common_keys as keys  # noqa: E402
from core import multi_source_execution as execution  # noqa: E402
from core import multi_source_plan as plans  # noqa: E402

from tests.integration.epic66_fixtures import (  # noqa: E402
    make_canonical_field,
    make_datastream,
    make_project,
    make_semantic_view,
    pin_relationship,
    publish_output,
)


@pytest.fixture()
def executed(live_postgres, tmp_path, monkeypatch):
    """One real cross-source Result, produced by story 66.5's engine."""
    path = tmp_path / "pivot.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
    con.execute("CREATE TABLE main_marts.spend (date DATE, campaign TEXT, spend_micros BIGINT)")
    con.execute(
        "CREATE TABLE main_marts.conversions "
        "(event_date DATE, campaign_key TEXT, revenue_micros BIGINT)"
    )
    for row in [("2026-08-01", "A", 10), ("2026-08-02", "A", 8), ("2026-08-01", "B", 40)]:
        con.execute("INSERT INTO main_marts.spend VALUES (?,?,?)", list(row))
    for row in [("2026-08-01", "A", 100), ("2026-08-02", "A", 200), ("2026-08-01", "B", 40)]:
        con.execute("INSERT INTO main_marts.conversions VALUES (?,?,?)", list(row))
    con.close()
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(path))

    org_id, project_id = make_project(live_postgres, "Pivot")
    day = make_canonical_field(live_postgres, project_id, "day", value_type="date")
    campaign = make_canonical_field(live_postgres, project_id, "campaign_id")
    spend = make_canonical_field(
        live_postgres, project_id, "spend", kind="metric", value_type="money"
    )
    revenue = make_canonical_field(
        live_postgres, project_id, "revenue", kind="metric", value_type="money"
    )
    left = make_datastream(
        live_postgres, org_id, project_id, "Campaign spend",
        bindings={
            day: ("date", "confirmed"),
            campaign: ("campaign", "confirmed"),
            spend: ("spend_micros", "confirmed"),
        },
    )
    right = make_datastream(
        live_postgres, org_id, project_id, "Conversions",
        bindings={
            day: ("event_date", "confirmed"),
            campaign: ("campaign_key", "confirmed"),
            revenue: ("revenue_micros", "confirmed"),
        },
    )
    publish_output(live_postgres, org_id, project_id, left, "spend")
    publish_output(live_postgres, org_id, project_id, right, "conversions")
    key = keys.create_common_key(
        live_postgres, project_id=project_id, name="Day and Campaign",
        canonical_field_ids=[day, campaign], actor="tester",
    )
    _view_id, view_version_id = make_semantic_view(live_postgres, project_id)
    pin_relationship(live_postgres, view_version_id, key["current_version"]["id"])

    # THE EVIDENCE IS MEASURED, then handed to the compiler -- what production does
    # (`multi_source_api:177`). `compile_plan` refuses every governed cross with no
    # `measured_safety`, and this fixture predated that requirement. Measuring the
    # rows the world just landed keeps the evidence current by construction.
    profile = match_profile.profile_match(
        live_postgres,
        project_id=project_id,
        left_datastream_id=left,
        right_datastream_id=right,
        common_key_version_id=key["current_version"]["id"],
        relationship_name="left_to_right",
        view_version_id=view_version_id,
    )
    compiled = plans.compile_plan(
        live_postgres,
        project_id=project_id,
        profile_lookup=lambda *_: profile,
        request={
            "members": [
                {"datastream_id": left, "measures": [{"canonical_field_id": spend}]},
                {"datastream_id": right, "measures": [{"canonical_field_id": revenue}]},
            ],
            "edges": [
                {
                    "left": left,
                    "right": right,
                    "common_key_version_id": key["current_version"]["id"],
                }
            ],
            "inclusion_policy": "matched_only",
            "primary_datastream_id": left,
        },
    )
    stored = plans.store_plan_version(
        live_postgres, org_id=org_id, project_id=project_id, compiled=compiled, actor="tester"
    )
    result = execution.execute_plan(
        live_postgres,
        org_id=org_id,
        project_id=project_id,
        query_spec_version_id=stored["query_spec_version_id"],
        actor="tester",
    )
    return {
        "org_id": org_id,
        "project_id": project_id,
        "result_id": result["result_id"],
        "day": day,
        "campaign": campaign,
        "spend": spend,
        "revenue": revenue,
    }


def _payload(conn, executed):
    return pivot_api.load_result_payload(
        conn,
        org_id=executed["org_id"],
        project_id=executed["project_id"],
        result_id=executed["result_id"],
    )


def test_the_pivot_of_a_real_result_carries_its_identity_and_its_cells(live_postgres, executed):
    payload = _payload(live_postgres, executed)
    matrix = pivot_projection.project(
        result_id=executed["result_id"],
        content_hash=payload["content_hash"],
        schema=payload["schema"],
        rows=payload["rows"],
        request={
            "rows": [f"k_{executed['campaign']}"],
            "columns": [f"k_{executed['day']}"],
            "values": [f"m_{executed['spend']}", f"m_{executed['revenue']}"],
            "subtotals": True,
            "grand_total": "both",
        },
    )

    assert matrix["content_hash"] == payload["content_hash"]
    assert matrix["row_keys"] == [["A"], ["B"]]
    # Campaign A over two days: 10 + 8 = 18 spend, 100 + 200 = 300 revenue.
    subtotal = {entry["row_key"][0]: entry for entry in matrix["row_subtotals"]}["A"]
    assert subtotal["values"][f"m_{executed['spend']}"]["value"] == 18
    assert subtotal["values"][f"m_{executed['revenue']}"]["value"] == 300
    # Grand total over everything: 58 and 340.
    assert matrix["grand_total"]["overall"][f"m_{executed['spend']}"]["value"] == 58
    # Both axes collapse, and they sum back to the same corner.
    assert sum(
        entry["values"][f"m_{executed['spend']}"]["value"]
        for entry in matrix["grand_total"]["by_column_key"]
    ) == 58
    assert [entry["column_key"] for entry in matrix["column_subtotals"]] == matrix["column_keys"]


def test_the_pivot_columns_are_exactly_what_execution_wrote(live_postgres, executed):
    """One schema, two readers. A second naming convention would drift silently."""
    payload = _payload(live_postgres, executed)
    names = {field["name"] for field in payload["schema"]["fields"]}
    assert f"k_{executed['day']}" in names
    assert f"m_{executed['spend']}" in names
    assert all(row.keys() <= names for row in payload["rows"])


def test_a_result_of_another_project_does_not_resolve(live_postgres, executed):
    _other_org, other_project = make_project(live_postgres, "Elsewhere")
    with pytest.raises(LookupError):
        pivot_api.load_result_payload(
            live_postgres,
            org_id=executed["org_id"],
            project_id=other_project,
            result_id=executed["result_id"],
        )


def test_the_door_is_mounted_at_the_project_scoped_address():
    from core.admin_api import router

    paths = {getattr(route, "path", "") for route in router.routes}
    assert "/api/projects/{project_id}/analyze/results/{result_id}/pivot" in paths
