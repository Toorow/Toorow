"""A chart of a two-source analysis, saved and reopened (story 66.8).

The defect this suite exists for, measured on 2026-08-13: a Visualization Spec
resolves the members it may bind from `spec.measures[].id` /
`spec.dimensions[].id` — the `query-spec.v1` shape. A cross-source plan carries
neither, so EVERY binding over a two-source Result was refused `unknown_member`
and no chart of one could ever be saved. Nothing said so: the spec simply found
zero members and refused the request as if the caller had invented the names.

So the whole story is proven end to end on the real objects: execute a plan,
bind a chart to the columns execution actually wrote, save the immutable
Visualization Spec version, freeze the Render, and read every pin back.
"""

from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")
duckdb = pytest.importorskip("duckdb")

from core import match_profile  # noqa: E402
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


@pytest.fixture()
def analysed(live_postgres, tmp_path, monkeypatch):
    """One executed cross-source Result, with the ids the chart will bind to."""
    path = tmp_path / "viz.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
    con.execute("CREATE TABLE main_marts.spend (date DATE, campaign TEXT, spend_micros BIGINT)")
    con.execute(
        "CREATE TABLE main_marts.conversions "
        "(event_date DATE, campaign_key TEXT, revenue_micros BIGINT)"
    )
    con.execute("INSERT INTO main_marts.spend VALUES ('2026-08-01','A',10)")
    con.execute("INSERT INTO main_marts.conversions VALUES ('2026-08-01','A',100)")
    con.close()
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(path))

    org_id, project_id = make_project(live_postgres, "Chart")
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
            "derived_measures": [
                {
                    "canonical_field_id": "mdm_roas_example",
                    "numerator_field_id": revenue,
                    "denominator_field_id": spend,
                }
            ],
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
        "query_spec_version_id": stored["query_spec_version_id"],
        "result_id": result["result_id"],
        "day": day,
        "campaign": campaign,
        "spend": spend,
        "revenue": revenue,
    }


def _chart(analysed, **overrides):
    document = {
        "spec_contract_version": viz.VISUALIZATION_SPEC_CONTRACT_VERSION,
        "schema_version": viz.VISUALIZATION_SPEC_SCHEMA_VERSION,
        "family": "bar",
        "bindings": {
            "measure": [f"m_{analysed['spend']}"],
            "dimension": [f"k_{analysed['campaign']}"],
        },
    }
    document.update(overrides)
    return document


# ---------------------------------------------------------------------------
# The members a cross-source plan offers
# ---------------------------------------------------------------------------


def test_the_plan_offers_the_columns_execution_wrote(live_postgres, analysed):
    """One naming point: the chart binds what the Result actually carries."""
    pinned = viz.load_pinned_query_spec_version(
        live_postgres,
        project_id=analysed["project_id"],
        query_spec_version_id=analysed["query_spec_version_id"],
    )
    assert pinned.role_of(f"m_{analysed['spend']}") == "measure"
    assert pinned.role_of(f"m_{analysed['revenue']}") == "measure"
    assert pinned.role_of("r_mdm_roas_example") == "measure"
    assert pinned.role_of(f"k_{analysed['day']}") == "dimension"
    assert pinned.role_of(f"k_{analysed['campaign']}") == "dimension"
    # And the dimension label is the canonical NAME, not the raw id: a legend
    # reading `mdm_01KZ…` is a legend nobody can use.
    assert pinned.labels[f"k_{analysed['day']}"] == "day"


def test_the_columns_offered_are_exactly_the_columns_of_the_result(live_postgres, analysed):
    pinned = viz.load_pinned_query_spec_version(
        live_postgres,
        project_id=analysed["project_id"],
        query_spec_version_id=analysed["query_spec_version_id"],
    )
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT result_schema FROM app.query_result_payloads WHERE result_id = %s",
            (analysed["result_id"],),
        )
        schema = cur.fetchone()[0]
    assert {field["name"] for field in schema["fields"]} == set(pinned.roles)


# ---------------------------------------------------------------------------
# Saving a chart of the cross
# ---------------------------------------------------------------------------


def test_a_chart_of_two_sources_validates_and_saves(live_postgres, analysed):
    validated = viz.validate_visualization_spec(
        live_postgres,
        project_id=analysed["project_id"],
        query_spec_version_id=analysed["query_spec_version_id"],
        payload=_chart(analysed),
    )
    saved = viz.create_visualization_spec_version(
        live_postgres,
        org_id=analysed["org_id"],
        project_id=analysed["project_id"],
        validated=validated,
        actor="tester",
        name="Spend by campaign",
    )
    assert saved["version_number"] == 1
    assert saved["content_hash"] == validated.content_hash

    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT query_spec_version_id, spec FROM app.visualization_spec_versions "
            "WHERE id = %s",
            (saved["id"],),
        )
        row = cur.fetchone()
    assert row[0] == analysed["query_spec_version_id"]
    assert row[1]["bindings"]["measure"] == [f"m_{analysed['spend']}"]


def test_a_binding_the_result_does_not_carry_is_refused_by_name(live_postgres, analysed):
    with pytest.raises(viz.VisualizationSpecRefused) as excinfo:
        viz.validate_visualization_spec(
            live_postgres,
            project_id=analysed["project_id"],
            query_spec_version_id=analysed["query_spec_version_id"],
            payload=_chart(analysed, bindings={"measure": ["m_invented"], "dimension": [
                f"k_{analysed['campaign']}"
            ]}),
        )
    assert any(r.code == "unknown_member" for r in excinfo.value.refusals)


def test_a_dimension_in_the_measure_well_is_refused(live_postgres, analysed):
    with pytest.raises(viz.VisualizationSpecRefused) as excinfo:
        viz.validate_visualization_spec(
            live_postgres,
            project_id=analysed["project_id"],
            query_spec_version_id=analysed["query_spec_version_id"],
            payload=_chart(
                analysed,
                bindings={
                    "measure": [f"k_{analysed['campaign']}"],
                    "dimension": [f"k_{analysed['day']}"],
                },
            ),
        )
    assert excinfo.value.refusals


def test_a_second_version_appends_and_the_first_stays(live_postgres, analysed):
    first = viz.create_visualization_spec_version(
        live_postgres,
        org_id=analysed["org_id"],
        project_id=analysed["project_id"],
        validated=viz.validate_visualization_spec(
            live_postgres,
            project_id=analysed["project_id"],
            query_spec_version_id=analysed["query_spec_version_id"],
            payload=_chart(analysed),
        ),
        actor="tester",
    )
    second = viz.create_visualization_spec_version(
        live_postgres,
        org_id=analysed["org_id"],
        project_id=analysed["project_id"],
        validated=viz.validate_visualization_spec(
            live_postgres,
            project_id=analysed["project_id"],
            query_spec_version_id=analysed["query_spec_version_id"],
            payload=_chart(
                analysed,
                bindings={
                    "measure": [f"m_{analysed['revenue']}"],
                    "dimension": [f"k_{analysed['campaign']}"],
                },
            ),
        ),
        actor="tester",
        visualization_id=first["visualization_id"],
    )
    assert second["version_number"] == 2
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.visualization_spec_versions WHERE visualization_id = %s",
            (first["visualization_id"],),
        )
        assert cur.fetchone()[0] == 2


# ---------------------------------------------------------------------------
# The presentation edit never touches the analytical one
# ---------------------------------------------------------------------------


def test_saving_a_presentation_writes_no_new_result(live_postgres, analysed):
    """A chart is a presentation edit: the Result count does not move."""
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.query_results WHERE project_id = %s",
            (analysed["project_id"],),
        )
        before = cur.fetchone()[0]

    viz.create_visualization_spec_version(
        live_postgres,
        org_id=analysed["org_id"],
        project_id=analysed["project_id"],
        validated=viz.validate_visualization_spec(
            live_postgres,
            project_id=analysed["project_id"],
            query_spec_version_id=analysed["query_spec_version_id"],
            payload=_chart(analysed),
        ),
        actor="tester",
    )

    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.query_results WHERE project_id = %s",
            (analysed["project_id"],),
        )
        assert cur.fetchone()[0] == before


def test_a_single_source_spec_still_resolves_its_own_members(live_postgres, analysed):
    """The v1 path is untouched: the branch is additive, and this proves it."""
    from ulid import ULID

    spec_id, version_id = f"qs_{ULID()}", f"qsv_{ULID()}"
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT semantic_view_id, semantic_view_version_id FROM app.query_spec_versions "
            "WHERE id = %s",
            (analysed["query_spec_version_id"],),
        )
        view_id, view_version_id = cur.fetchone()
        cur.execute(
            "INSERT INTO app.query_specs (id, org_id, project_id, semantic_view_id, created_by) "
            "VALUES (%s,%s,%s,%s,'tester')",
            (spec_id, analysed["org_id"], analysed["project_id"], view_id),
        )
        cur.execute(
            """
            INSERT INTO app.query_spec_versions
                (id, query_spec_id, org_id, project_id, version_number, semantic_view_id,
                 semantic_view_version_id, spec, content_hash, created_by)
            VALUES (%s,%s,%s,%s,1,%s,%s,%s::jsonb,%s,'tester')
            """,
            (
                version_id,
                spec_id,
                analysed["org_id"],
                analysed["project_id"],
                view_id,
                view_version_id,
                '{"contract_version":"query-spec.v1","measures":[{"id":"clicks"}],'
                '"dimensions":[{"id":"channel"}],"grain":"day","comparison":"none",'
                '"row_limit":100}',
                "b" * 64,
            ),
        )

    pinned = viz.load_pinned_query_spec_version(
        live_postgres, project_id=analysed["project_id"], query_spec_version_id=version_id
    )
    assert pinned.role_of("clicks") == "measure"
    assert pinned.role_of("channel") == "dimension"
    assert pinned.grain == "day"
    assert pinned.row_limit == 100


# ---------------------------------------------------------------------------
# Freezing the Render, and reading every pin back
# ---------------------------------------------------------------------------


def _saved_chart(conn, analysed):
    return viz.create_visualization_spec_version(
        conn,
        org_id=analysed["org_id"],
        project_id=analysed["project_id"],
        validated=viz.validate_visualization_spec(
            conn,
            project_id=analysed["project_id"],
            query_spec_version_id=analysed["query_spec_version_id"],
            payload=_chart(analysed),
        ),
        actor="tester",
        name="Spend by campaign",
    )


def test_a_render_of_a_cross_source_result_freezes_and_reopens(live_postgres, analysed):
    """The Render machinery is the existing one, and it takes this Result as it is.

    That is the claim worth proving: story 66.8 did NOT need a second Render
    path. If it had, a cross-source analysis would have had its own lifecycle,
    its own retention and its own sharing rules -- three places to keep in step.
    """
    from core import analyze_artifacts

    saved = _saved_chart(live_postgres, analysed)
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT content_hash FROM app.query_results WHERE id = %s",
            (analysed["result_id"],),
        )
        result_hash = cur.fetchone()[0]

    render = analyze_artifacts.create_render(
        live_postgres,
        org_id=analysed["org_id"],
        project_id=analysed["project_id"],
        actor="tester",
        payload={
            "result_id": analysed["result_id"],
            "result_content_hash": result_hash,
            "visualization_spec_version_id": saved["id"],
            "renderer_adapter": "echarts",
            "renderer_build_id": "renderer-2026.08.13",
            "runtime_build_id": "runtime-2026.08.13",
            "theme_version": "theme-3",
            "formatter_version": "formatter-2",
            "responsive_profile": "desktop",
            "display_state": {"sort": "measure_desc"},
            "evidence_manifest": {"result_id": analysed["result_id"]},
            "datum_evidence_keys": [],
            "creation_surface": "explore",
            "origin_kind": "explore",
        },
    )

    with live_postgres.cursor() as cur:
        cur.execute(
            """
            SELECT result_id, result_content_hash, visualization_spec_version_id,
                   renderer_build_id, runtime_build_id, theme_version, formatter_version,
                   responsive_profile
              FROM app.renders WHERE id = %s
            """,
            (render["id"],),
        )
        pins = cur.fetchone()

    # Every replay pin, read back exactly as frozen -- including the Result hash,
    # which is what makes the chart and the table the same answer on reload.
    assert pins[0] == analysed["result_id"]
    assert pins[1] == result_hash
    assert pins[2] == saved["id"]
    assert pins[3] == "renderer-2026.08.13"
    assert pins[4] == "runtime-2026.08.13"


def test_a_render_missing_a_pin_is_refused_and_names_it(live_postgres, analysed):
    from core import analyze_artifacts

    saved = _saved_chart(live_postgres, analysed)
    with pytest.raises(analyze_artifacts.ArtifactRefused) as excinfo:
        analyze_artifacts.create_render(
            live_postgres,
            org_id=analysed["org_id"],
            project_id=analysed["project_id"],
            actor="tester",
            payload={
                "result_id": analysed["result_id"],
                "visualization_spec_version_id": saved["id"],
                "renderer_adapter": "echarts",
                "evidence_manifest": {"result_id": analysed["result_id"]},
                "creation_surface": "explore",
                "origin_kind": "explore",
            },
        )
    named = {refusal.subject for refusal in excinfo.value.refusals}
    assert "runtime_build_id" in named
    assert "result_content_hash" in named
