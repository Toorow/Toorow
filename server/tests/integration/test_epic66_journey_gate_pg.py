"""ONE reproducible gate over the whole epic-66 journey (story 66.11).

Every other suite of this epic proves one link. This one walks the chain a person
walks, on two DIFFERENTLY SHAPED sources, with the three cases that break naive
joins deliberately present:

    a key only the left side has        -> unmatched
    a row whose key part is NULL        -> never matched, and counted
    a key repeated on one side          -> multiplies, and must not

and asserts, in order: the key is declared, the match is discovered and ranked,
its profile is measured, the plan is frozen, the execution keeps each source's
control total, the pivot rearranges without recomputing, the chart binds and
saves, the Render freezes, and the reload returns every pin.

The gate is one test function on purpose. Split into nine, a green suite would
mean nine links each work in isolation -- which is precisely what was already
true before this epic, and precisely what did not add up to a journey.
"""

from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")
duckdb = pytest.importorskip("duckdb")

from core import analyze_artifacts, match_profile, pivot_projection  # noqa: E402
from core import datastream_matches as matches  # noqa: E402
from core import mdm_common_keys as keys  # noqa: E402
from core import multi_source_execution as execution  # noqa: E402
from core import multi_source_plan as plans  # noqa: E402
from core import visualization_specs as viz  # noqa: E402

from tests.integration.epic66_fixtures import (  # noqa: E402
    make_canonical_field,
    make_datastream,
    make_project,
    make_semantic_view,
    pin_relationship,
    publish_output,
)

#: Two shapes, on purpose. `spend` is daily per campaign and repeats one key
#: twice; `conversions` is one row per campaign per day with a NULL campaign on
#: one row and a day the other source never saw. A fixture where both sides are
#: clean proves the happy path and nothing about the product.
_SPEND_ROWS = [
    ("2026-08-01", "A", 10),
    ("2026-08-01", "A", 8),      # the duplicate key
    ("2026-08-02", "B", 20),
    ("2026-08-03", "C", 30),     # C exists only here -> unmatched
]
_CONVERSION_ROWS = [
    ("2026-08-01", "A", 100),
    ("2026-08-02", "B", 40),
    ("2026-08-02", None, 7),     # the NULL key
    ("2026-08-09", "Z", 1),      # Z exists only here -> unmatched
]


@pytest.fixture()
def journey(live_postgres, tmp_path, monkeypatch):
    path = tmp_path / "journey.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
    con.execute("CREATE TABLE main_marts.spend (date DATE, campaign TEXT, spend_micros BIGINT)")
    con.execute(
        "CREATE TABLE main_marts.conversions "
        "(event_date DATE, campaign_key TEXT, revenue_micros BIGINT)"
    )
    for row in _SPEND_ROWS:
        con.execute("INSERT INTO main_marts.spend VALUES (?,?,?)", list(row))
    for row in _CONVERSION_ROWS:
        con.execute("INSERT INTO main_marts.conversions VALUES (?,?,?)", list(row))
    con.close()
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(path))

    org_id, project_id = make_project(live_postgres, "Journey")
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
    return {
        "org_id": org_id,
        "project_id": project_id,
        "left": left,
        "right": right,
        "day": day,
        "campaign": campaign,
        "spend": spend,
        "revenue": revenue,
    }


def test_the_whole_journey_holds_on_two_differently_shaped_sources(live_postgres, journey):
    conn = live_postgres
    project_id = journey["project_id"]

    # --- G66-A: identity is declared once, in MDM -------------------------------
    key = keys.create_common_key(
        conn,
        project_id=project_id,
        name="Day and Campaign",
        canonical_field_ids=[journey["day"], journey["campaign"]],
        actor="tester",
    )
    key_version_id = key["current_version"]["id"]
    coverage = keys.read_common_key(
        conn, project_id=project_id, common_key_id=key["id"]
    )["mapping_coverage"]
    assert coverage["state"] == "available"
    assert all(component["implementation_count"] == 2 for component in coverage["components"])

    # --- G66-A: a key without an approved relationship is NOT executable --------
    candidate = matches.discover_matches(conn, project_id=project_id)
    assert [m["kind"] for m in candidate["matches"]] == ["candidate_key_missing"]
    assert candidate["matches"][0]["explore_together"] is None

    _view_id, view_version_id = make_semantic_view(conn, project_id)
    pin_relationship(conn, view_version_id, key_version_id, name="spend_to_conversions")

    governed = matches.discover_matches(conn, project_id=project_id)["matches"][0]
    assert governed["authority"] == "governed"
    assert governed["observed_coverage"] == "unavailable"  # nothing measured YET
    handoff = governed["explore_together"]
    assert {entry["datastream_id"] for entry in handoff["datastreams"]} == {
        journey["left"], journey["right"]
    }

    # --- G66-B: the evidence, measured on the rows ------------------------------
    profile = match_profile.profile_match(
        conn,
        project_id=project_id,
        left_datastream_id=journey["left"],
        right_datastream_id=journey["right"],
        common_key_version_id=key_version_id,
        relationship_name="spend_to_conversions",
        view_version_id=view_version_id,
    )
    assert profile["left"]["total_rows"] == 4
    assert profile["left"]["distinct_keys"] == 3          # A, B, C
    assert profile["left"]["duplicated_keys"] == 1        # A twice
    assert profile["right"]["null_key_rows"] == 1         # the NULL campaign
    assert profile["matched"]["matched_keys"] == 2        # (01,A) and (02,B)
    assert profile["matched"]["left_unmatched_keys"] == 1  # C
    assert profile["matched"]["right_unmatched_keys"] == 1  # Z
    # Duplicated on one side only: review, not unsafe.
    assert profile["multiplication"]["worst_case_rows_per_key"] == 2
    assert profile["execution_safety"] == "review_required"

    # --- G66-A: the plan freezes every pin, or refuses --------------------------
    request = {
        "members": [
            {
                "datastream_id": journey["left"],
                "mapping_version_id": handoff["datastreams"][0]["mapping_version_id"],
                "measures": [{"canonical_field_id": journey["spend"]}],
            },
            {
                "datastream_id": journey["right"],
                "mapping_version_id": handoff["datastreams"][1]["mapping_version_id"],
                "measures": [{"canonical_field_id": journey["revenue"]}],
            },
        ],
        "edges": [
            {
                "left": journey["left"],
                "right": journey["right"],
                "common_key_version_id": key_version_id,
                "relationship_name": "spend_to_conversions",
            }
        ],
        "inclusion_policy": "matched_only",
        "primary_datastream_id": journey["left"],
        "derived_measures": [
            {
                "canonical_field_id": "mdm_roas_journey",
                "numerator_field_id": journey["revenue"],
                "denominator_field_id": journey["spend"],
            }
        ],
    }
    compiled = plans.compile_plan(
        conn, project_id=project_id, request=request, profile_lookup=lambda *_: profile
    )
    stored = plans.store_plan_version(
        conn, org_id=journey["org_id"], project_id=project_id, compiled=compiled, actor="tester"
    )

    # --- G66-C: execution keeps each source's control total ---------------------
    result = execution.execute_plan(
        conn,
        org_id=journey["org_id"],
        project_id=project_id,
        query_spec_version_id=stored["query_spec_version_id"],
        actor="tester",
    )
    assert result["outcome"] == "success"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT result_schema, rows_chunk, manifest FROM app.query_result_payloads "
            "WHERE result_id = %s",
            (result["result_id"],),
        )
        schema, rows, manifest = cur.fetchone()

    spend_column = f"m_{journey['spend']}"
    revenue_column = f"m_{journey['revenue']}"
    by_campaign = {row[f"k_{journey['campaign']}"]: row for row in rows}
    # matched_only: A and B only. C and Z and the NULL key are out.
    assert set(by_campaign) == {"A", "B"}
    # A's two spend rows were aggregated BEFORE the merge: 10 + 8 = 18, not 36.
    assert by_campaign["A"][spend_column] == 18
    assert by_campaign["A"][revenue_column] == 100
    # The ratio is recomputed from its components: 100 / 18.
    assert round(by_campaign["A"]["r_mdm_roas_journey"], 6) == round(100 / 18, 6)
    assert manifest["plan_content_hash"] == compiled["content_hash"]
    assert manifest["sql_shape"] == "aggregate_then_merge"

    # --- G66-D: the pivot rearranges; it recomputes nothing ---------------------
    matrix = pivot_projection.project(
        result_id=result["result_id"],
        content_hash=manifest and result["result_id"] and _hash_of(conn, result["result_id"]),
        schema=schema,
        rows=rows,
        request={
            "rows": [f"k_{journey['campaign']}"],
            "columns": [f"k_{journey['day']}"],
            "values": [spend_column, "r_mdm_roas_journey"],
            "subtotals": True,
            "grand_total": "both",
        },
    )
    assert matrix["result_id"] == result["result_id"]
    subtotals = {entry["row_key"][0]: entry for entry in matrix["row_subtotals"]}
    assert subtotals["A"]["values"][spend_column]["value"] == 18
    # The ratio subtotal is recomputed, never folded.
    assert subtotals["A"]["values"]["r_mdm_roas_journey"]["recomputed_from"]
    # `grand_total` collapses ONE axis per policy since 2026-08-21; `overall` is
    # the corner, and it exists only under `both`. This line read `["values"]`
    # and left this gate red for a day: the byte budget of a skipped gate is the
    # cheapest place in the repository to hide a broken contract.
    total_spend = matrix["grand_total"]["overall"][spend_column]["value"]
    # The two collapses sum back to the same corner, on the page that is served.
    assert sum(
        entry["values"][spend_column]["value"]
        for entry in matrix["grand_total"]["by_column_key"]
    ) == total_spend
    assert total_spend == 38  # 18 (A) + 20 (B): the matched perimeter, exactly
    # An empty cell keeps saying so.
    empty = [
        cell for cell in matrix["cells"]
        if cell["contributing_rows"] == 0
    ]
    assert all(
        value["value"] is None and value["absent_reason"]
        for cell in empty for value in cell["values"].values()
    )

    # --- G66-E: the chart binds, saves, freezes and reopens ---------------------
    validated = viz.validate_visualization_spec(
        conn,
        project_id=project_id,
        query_spec_version_id=stored["query_spec_version_id"],
        payload={
            "spec_contract_version": viz.VISUALIZATION_SPEC_CONTRACT_VERSION,
            "schema_version": viz.VISUALIZATION_SPEC_SCHEMA_VERSION,
            "family": "bar",
            "bindings": {
                "measure": [spend_column],
                "dimension": [f"k_{journey['campaign']}"],
            },
        },
    )
    saved = viz.create_visualization_spec_version(
        conn,
        org_id=journey["org_id"],
        project_id=project_id,
        validated=validated,
        actor="tester",
        name="Spend by campaign",
    )
    render = analyze_artifacts.create_render(
        conn,
        org_id=journey["org_id"],
        project_id=project_id,
        actor="tester",
        payload={
            "result_id": result["result_id"],
            "result_content_hash": _hash_of(conn, result["result_id"]),
            "visualization_spec_version_id": saved["id"],
            "renderer_adapter": "echarts",
            "renderer_build_id": "renderer-2026.08.13",
            "runtime_build_id": "runtime-2026.08.13",
            "theme_version": "theme-3",
            "formatter_version": "formatter-2",
            "responsive_profile": "desktop",
            "display_state": {"sort": "measure_desc"},
            "evidence_manifest": {"result_id": result["result_id"]},
            "datum_evidence_keys": [],
            "creation_surface": "explore",
            "origin_kind": "explore",
        },
    )

    # --- reload: every pin, read back from the store ----------------------------
    with conn.cursor() as cur:
        cur.execute(
            "SELECT result_id, result_content_hash, visualization_spec_version_id "
            "FROM app.renders WHERE id = %s",
            (render["id"],),
        )
        pins = cur.fetchone()
    assert pins == (
        result["result_id"], _hash_of(conn, result["result_id"]), saved["id"]
    )
    reopened = plans.load_plan_version(
        conn, project_id=project_id, query_spec_version_id=stored["query_spec_version_id"]
    )
    assert reopened["content_hash"] == compiled["content_hash"]
    assert reopened["plan"]["inclusion_policy"] == "matched_only"
    assert {member["datastream_id"] for member in reopened["plan"]["members"]} == {
        journey["left"], journey["right"]
    }


def test_the_same_journey_under_preserve_all_keeps_what_matched_only_dropped(
    live_postgres, journey
):
    """The inclusion policy is the whole difference, and it is visible in the rows."""
    conn = live_postgres
    project_id = journey["project_id"]
    key = keys.create_common_key(
        conn, project_id=project_id, name="Day and Campaign",
        canonical_field_ids=[journey["day"], journey["campaign"]], actor="tester",
    )
    _view_id, view_version_id = make_semantic_view(conn, project_id)
    pin_relationship(conn, view_version_id, key["current_version"]["id"])

    def _run(policy: str) -> list[dict]:
        current_profile = match_profile.profile_match(
            conn,
            project_id=project_id,
            left_datastream_id=journey["left"],
            right_datastream_id=journey["right"],
            common_key_version_id=key["current_version"]["id"],
            relationship_name="left_to_right",
            view_version_id=view_version_id,
        )
        compiled = plans.compile_plan(
            conn,
            project_id=project_id,
            request={
                "members": [
                    {
                        "datastream_id": journey["left"],
                        "measures": [{"canonical_field_id": journey["spend"]}],
                    },
                    {
                        "datastream_id": journey["right"],
                        "measures": [{"canonical_field_id": journey["revenue"]}],
                    },
                ],
                "edges": [
                    {
                        "left": journey["left"],
                        "right": journey["right"],
                        "common_key_version_id": key["current_version"]["id"],
                    }
                ],
                "inclusion_policy": policy,
                "primary_datastream_id": journey["left"],
            },
            profile_lookup=lambda *_: current_profile,
        )
        stored = plans.store_plan_version(
            conn, org_id=journey["org_id"], project_id=project_id, compiled=compiled,
            actor="tester",
        )
        result = execution.execute_plan(
            conn,
            org_id=journey["org_id"],
            project_id=project_id,
            query_spec_version_id=stored["query_spec_version_id"],
            actor="tester",
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT rows_chunk FROM app.query_result_payloads WHERE result_id = %s",
                (result["result_id"],),
            )
            return cur.fetchone()[0]

    matched_only = _run("matched_only")
    preserve_primary = _run("preserve_primary")
    preserve_all = _run("preserve_all")

    campaign = f"k_{journey['campaign']}"
    assert {row[campaign] for row in matched_only} == {"A", "B"}
    # The primary keeps C, which the other source never had.
    assert {row[campaign] for row in preserve_primary} == {"A", "B", "C"}
    # Everything keeps Z too. The NULL-key row is never matched and never keyed.
    assert {row[campaign] for row in preserve_all} >= {"A", "B", "C", "Z"}
    # And what the other side did not have is NULL, not zero.
    revenue = f"m_{journey['revenue']}"
    only_c = [row for row in preserve_primary if row[campaign] == "C"][0]
    assert only_c[revenue] is None


def _hash_of(conn, result_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT content_hash FROM app.query_results WHERE id = %s", (result_id,))
        return cur.fetchone()[0]
