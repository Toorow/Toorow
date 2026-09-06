"""Console and MCP are two doors of one product, proven on one plan (story 66.9).

The parity NFR (NFR11) says: given the same pinned request, both doors produce
the same Query Spec hash, the same Result and the same cells. There are two ways
to satisfy that sentence — write it twice and test that the two agree, or call
one executor from both doors. This suite proves the second, which is the only one
that stays true after the next amendment.

The defect it also pins down, measured on 2026-08-13: before this story the MCP
door handed the stored document to the SINGLE-source runner. A
`multi-source-plan.v1` version carries no `measures[]`, so that runner resolved
no member and the model was told "the source could not produce this result" for
a perfectly valid two-source analysis.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import ANY

import pytest

psycopg = pytest.importorskip("psycopg")
duckdb = pytest.importorskip("duckdb")

from core import (  # noqa: E402
    context_store,
    golden_questions,
    match_profile,
    query_execution,  # noqa: E402
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
from tests.integration.taxonomy_fixtures import insert_domain_fixture  # noqa: E402


@pytest.fixture()
def planned(live_postgres, tmp_path, monkeypatch):
    path = tmp_path / "parity.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
    con.execute("CREATE TABLE main_marts.spend (date DATE, campaign TEXT, spend_micros BIGINT)")
    con.execute(
        "CREATE TABLE main_marts.conversions "
        "(event_date DATE, campaign_key TEXT, revenue_micros BIGINT)"
    )
    con.execute("INSERT INTO main_marts.spend VALUES ('2026-08-01','A',10)")
    con.execute("INSERT INTO main_marts.spend VALUES ('2026-08-02','A',8)")
    con.execute("INSERT INTO main_marts.conversions VALUES ('2026-08-01','A',100)")
    con.execute("INSERT INTO main_marts.conversions VALUES ('2026-08-02','A',200)")
    con.close()
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(path))

    org_id, project_id = make_project(live_postgres, "Parity")
    # A FIXTURE ROW since 2026-08-25: `create_domain` refuses, and what this
    # file measures is downstream of the domain existing, never of who made it.
    domain = insert_domain_fixture(
        live_postgres,
        org_id=org_id,
        name="Growth",
        slug=f"growth-{project_id.lower()}",
        actor="tester",
    )
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
    view_id, view_version_id = make_semantic_view(
        live_postgres,
        project_id,
        business_domain_refs=(domain["id"],),
    )
    pin_relationship(live_postgres, view_version_id, key["current_version"]["id"])
    skill = context_store.create_procedure(
        live_postgres,
        project_id=project_id,
        frontmatter_yaml=(
            "name: Paid media investigation\n"
            "description: Reconcile paid media sources before interpreting the pivot.\n"
        ),
        body_md="Private playbook body that must not enter MCP discovery.",
        created_by="tester",
    )
    validated_question = golden_questions.validate_golden_question_version(
        live_postgres,
        org_id=org_id,
        project_id=project_id,
        payload={
            "business_domain_id": domain["id"],
            "business_domain_version_number": 1,
            "semantic_view_id": view_id,
            "semantic_view_version_id": view_version_id,
            "semantic_view_version_role": "baseline",
            "question": "How do spend and conversions compare by campaign?",
            "time_boundary": {},
            "expected_result": [
                {"assertion_type": "value", "member_id": "spend", "tolerance": None}
            ],
            "required_provenance": [{"link_kind": "source", "required": True}],
            "expected_ai_path": {
                "required_nodes": [],
                "forbidden_tools": [],
                "order_constraints": [],
                "alternative_paths": [],
            },
            "result_type": "comparison",
            "capability_tags": ["multi_source_analysis"],
            "severity": "major",
        },
    )
    question = golden_questions.create_golden_question(
        live_postgres,
        org_id=org_id,
        project_id=project_id,
        title="Spend and conversions by campaign",
        owner="tester",
        validated=validated_question,
        actor="tester",
    )
    golden_questions.set_lifecycle(
        live_postgres,
        org_id=org_id,
        project_id=project_id,
        golden_question_id=question["golden_question_id"],
        lifecycle="active",
        actor="tester",
    )

    # THE EVIDENCE IS MEASURED, then handed to the compiler -- what production does
    # (`multi_source_api:177`). `compile_plan` refuses every governed cross with no
    # `measured_safety`, and this fixture predated that requirement. Measuring the
    # rows the world just landed keeps the evidence current by construction, so the
    # parity this file claims is parity over a plan production could also freeze.
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
    return {
        "org_id": org_id,
        "project_id": project_id,
        "query_spec_version_id": stored["query_spec_version_id"],
        "plan_hash": compiled["content_hash"],
        "spend": spend,
        "revenue": revenue,
        "day": day,
        "campaign": campaign,
        "left": left,
        "right": right,
        "common_key_version_id": key["current_version"]["id"],
        "view_version_id": view_version_id,
        "domain_id": domain["id"],
        "golden_question_version_id": question["version_id"],
        "skill_version_id": f"{skill['id']}@1",
    }


def _payload(conn, result_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT result_schema, rows_chunk, manifest FROM app.query_result_payloads "
            "WHERE result_id = %s",
            (result_id,),
        )
        return cur.fetchone()


def _console_run(conn, planned):
    return execution.execute_plan(
        conn,
        org_id=planned["org_id"],
        project_id=planned["project_id"],
        query_spec_version_id=planned["query_spec_version_id"],
        actor="console-user",
    )


def _mcp_run(conn, planned):
    """Exactly what `_execute_analyze_query_spec` does, minus the transport.

    The tool body allocates the attempt, opens the AI Path, calls the executor
    with `defer_terminalization=True` and completes the Result once the path is
    finalized. Everything analytical is in that call; this reproduces the shape
    so the parity claim is about the EXECUTOR and not about a mocked tool.
    """
    attempt = query_execution.accept_execution(
        conn,
        org_id=planned["org_id"],
        project_id=planned["project_id"],
        query_spec_version_id=planned["query_spec_version_id"],
        actor="mcp-user",
    )
    pending = execution.execute_plan(
        conn,
        org_id=planned["org_id"],
        project_id=planned["project_id"],
        query_spec_version_id=planned["query_spec_version_id"],
        actor="mcp-user",
        attempt=attempt,
        ai_path_id=None,
        defer_terminalization=True,
    )
    return query_execution.complete_deferred_result(conn, pending)


def test_both_doors_produce_the_same_answer_from_the_same_plan(live_postgres, planned):
    console = _console_run(live_postgres, planned)
    mcp = _mcp_run(live_postgres, planned)

    assert console["result_id"] != mcp["result_id"]  # two runs, two Result identities
    console_payload = _payload(live_postgres, console["result_id"])
    mcp_payload = _payload(live_postgres, mcp["result_id"])

    # The same schema, the same rows, the same plan pinned. Not "close enough":
    # deeply equal, because both came out of one executor.
    assert console_payload[0] == mcp_payload[0]
    assert console_payload[1] == mcp_payload[1]
    assert console_payload[2]["plan_content_hash"] == mcp_payload[2]["plan_content_hash"]
    assert console_payload[2]["plan_content_hash"] == planned["plan_hash"]
    assert console_payload[2]["inclusion_policy"] == mcp_payload[2]["inclusion_policy"]


def test_the_content_hash_differs_only_by_what_the_two_doors_legitimately_differ_on(
    live_postgres, planned
):
    """Identical analytical content; the Result hash covers per-run evidence too.

    Stating this rather than asserting equality of the hashes: `terminalize`
    folds an evaluation classification into the hashed document, so two runs of
    one plan are the same ANSWER with two identities. The parity that matters --
    schema, rows, plan pin -- is asserted above, exactly.
    """
    console = _console_run(live_postgres, planned)
    mcp = _mcp_run(live_postgres, planned)
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT row_count, cell_count, outcome FROM app.query_results WHERE id = ANY(%s)",
            ([console["result_id"], mcp["result_id"]],),
        )
        rows = cur.fetchall()
    assert len({row for row in rows}) == 1


def test_the_mcp_path_writes_exactly_one_result_for_its_attempt(live_postgres, planned):
    mcp = _mcp_run(live_postgres, planned)
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.query_results WHERE attempt_id = "
            "(SELECT attempt_id FROM app.query_results WHERE id = %s)",
            (mcp["result_id"],),
        )
        assert cur.fetchone()[0] == 1


def test_a_deferred_result_is_not_written_before_it_is_completed(live_postgres, planned):
    """The deferral is the AI Path's, and it is the SAME mechanism as single-source."""
    attempt = query_execution.accept_execution(
        conn=live_postgres,
        org_id=planned["org_id"],
        project_id=planned["project_id"],
        query_spec_version_id=planned["query_spec_version_id"],
        actor="mcp-user",
    )
    pending = execution.execute_plan(
        live_postgres,
        org_id=planned["org_id"],
        project_id=planned["project_id"],
        query_spec_version_id=planned["query_spec_version_id"],
        actor="mcp-user",
        attempt=attempt,
        defer_terminalization=True,
    )
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.query_results WHERE id = %s", (attempt["result_id"],)
        )
        assert cur.fetchone()[0] == 0

    query_execution.complete_deferred_result(live_postgres, pending)
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.query_results WHERE id = %s", (attempt["result_id"],)
        )
        assert cur.fetchone()[0] == 1


def test_the_mcp_door_dispatches_on_the_contract_the_document_declares():
    """The branch exists in the tool body, and it names the plan contract."""
    import inspect

    from core import analyze_render_mcp

    source = inspect.getsource(analyze_render_mcp._execute_analyze_query_spec)
    assert "_MULTI_SOURCE_PLAN_CONTRACT" in source
    assert "multi_source_execution.execute_plan" in source
    assert analyze_render_mcp._MULTI_SOURCE_PLAN_CONTRACT == "multi-source-plan.v1"


def _mcp_services_on_connection(monkeypatch, conn, planned):
    """Bind MCP service seams to this real scoped database connection."""
    from core import analyze_render_mcp, project_access, query_specs_api

    @contextmanager
    def scoped(_identity):
        yield conn

    decision = SimpleNamespace(allowed=True, org_id=planned["org_id"])
    monkeypatch.setattr(analyze_render_mcp, "_identity", lambda: "mcp-user")
    monkeypatch.setattr(analyze_render_mcp, "_guard_project_view", lambda *_: decision)
    monkeypatch.setattr(query_specs_api, "analyze_connection", scoped)
    monkeypatch.setattr(
        project_access,
        "resolve_strict_resource_access",
        lambda *_args, **_kwargs: decision,
    )
    return analyze_render_mcp


def test_mcp_discovers_only_the_bounded_governed_catalog(
    live_postgres, planned, monkeypatch
):
    mcp = _mcp_services_on_connection(monkeypatch, live_postgres, planned)

    answer = mcp._discover_analyze_matches(planned["project_id"])

    assert answer["schema_version"] == "analyze-match-catalog.v1"
    assert answer["project_id"] == planned["project_id"]
    assert answer["matches"]
    governed = answer["matches"][0]
    assert governed["authority"] == "governed"
    assert governed["common_key"]["version_id"] == planned["common_key_version_id"]
    assert {source["datastream_id"] for source in governed["sources"]} == {
        planned["left"],
        planned["right"],
    }
    assert all(source["output_version_id"] for source in governed["sources"])
    assert "rows" not in answer
    options = answer["analysis_context_options"]
    assert options["contract_version"] == "analysis-context-options.v1"
    assert options["semantic_views"] == [
        {
            "semantic_view_version_id": planned["view_version_id"],
            "business_domains": [
                {
                    "id": planned["domain_id"],
                    "version_number": 1,
                    "version_id": f"{planned['domain_id']}:1",
                    "name": "Growth",
                }
            ],
            "golden_questions": [
                {
                    "id": ANY,
                    "version_id": planned["golden_question_version_id"],
                    "version_number": 1,
                    "title": "Spend and conversions by campaign",
                    "lifecycle": "active",
                    "business_domain_id": planned["domain_id"],
                    "business_domain_version_number": 1,
                }
            ],
        }
    ]
    assert {skill["version_id"] for skill in options["requested_skills"]} >= {
        planned["skill_version_id"]
    }
    encoded = json.dumps(answer, sort_keys=True)
    assert "Private playbook body" not in encoded
    assert answer["bounds"]["response_bytes"] == len(
        json.dumps(answer, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    )


def test_mcp_composes_and_persists_the_same_plan_plus_visualization(
    live_postgres, planned, monkeypatch
):
    mcp = _mcp_services_on_connection(monkeypatch, live_postgres, planned)
    request = {
        "members": [
            {
                "datastream_id": planned["left"],
                "measures": [{"canonical_field_id": planned["spend"]}],
            },
            {
                "datastream_id": planned["right"],
                "measures": [{"canonical_field_id": planned["revenue"]}],
            },
        ],
        "edges": [
            {
                "left": planned["left"],
                "right": planned["right"],
                "common_key_version_id": planned["common_key_version_id"],
            }
        ],
        "dimensions": [
            {"canonical_field_id": planned["day"]},
            {"canonical_field_id": planned["campaign"]},
        ],
        "grain": planned["day"],
        "pivot": {
            "rows": [{"canonical_field_id": planned["campaign"]}],
            "columns": [{"canonical_field_id": planned["day"]}],
            "values": [
                {
                    "datastream_id": planned["left"],
                    "canonical_field_id": planned["spend"],
                },
                {
                    "datastream_id": planned["right"],
                    "canonical_field_id": planned["revenue"],
                },
            ],
            "filters": [],
            "subtotals": True,
            "grand_total": "both",
        },
        "inclusion_policy": "matched_only",
        "primary_datastream_id": planned["left"],
        "analysis_context": {
            "business_domain_id": planned["domain_id"],
            "business_domain_version_number": 1,
            "golden_question_version_id": planned["golden_question_version_id"],
            "skill_version_ids": [planned["skill_version_id"]],
        },
    }

    answer = mcp._compose_analyze_pivot(
        planned["project_id"], request, family="table", name="MCP pivot"
    )

    assert answer["schema_version"] == "analyze-pivot-composition.v1"
    assert answer["next"] == {
        "execute_tool": "execute_analyze_query_spec",
        "render_tool": "render_analyze_result",
    }
    assert set(answer["result_fields"]["dimensions"]) == {
        f"k_{planned['day']}",
        f"k_{planned['campaign']}",
    }
    assert answer["pivot"] == {
        "contract_version": "pivot-request.v1",
        "rows": [f"k_{planned['campaign']}"],
        "columns": [f"k_{planned['day']}"],
        "values": [f"m_{planned['spend']}", f"m_{planned['revenue']}"],
        "filters": [],
        "subtotals": True,
        "grand_total": "both",
        "row_sort": "asc",
        "column_sort": "asc",
    }
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT spec FROM app.query_spec_versions WHERE id = %s",
            (answer["query_spec_version_id"],),
        )
        stored_plan = cur.fetchone()[0]
        cur.execute(
            "SELECT spec FROM app.visualization_spec_versions WHERE id = %s",
            (answer["visualization_spec_version_id"],),
        )
        stored_visual = cur.fetchone()[0]
    assert stored_plan["contract_version"] == "multi-source-plan.v1"
    assert all(member["output_version_id"] for member in stored_plan["members"])
    assert stored_plan["analysis_context"]["business_domain"]["id"] == planned["domain_id"]
    assert stored_plan["analysis_context"]["golden_question"]["version_id"] == planned[
        "golden_question_version_id"
    ]
    requested_skill_ids = [
        skill["version_id"]
        for skill in stored_plan["analysis_context"]["requested_skills"]
    ]
    assert requested_skill_ids == [planned["skill_version_id"]]
    assert stored_visual["bindings"]["dimension"] == answer["result_fields"]["dimensions"]
    assert stored_visual["bindings"]["measure"] == answer["result_fields"]["measures"]

    result = execution.execute_plan(
        live_postgres,
        org_id=planned["org_id"],
        project_id=planned["project_id"],
        query_spec_version_id=answer["query_spec_version_id"],
        actor="mcp-user",
    )
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT id, query_spec_version_id, outcome, truncated, content_hash "
            "FROM app.query_results WHERE id = %s",
            (result["result_id"],),
        )
        stored_result = cur.fetchone()
        cur.execute(
            "SELECT content_hash, result_schema, manifest, rows_chunk "
            "FROM app.query_result_payloads WHERE result_id = %s",
            (result["result_id"],),
        )
        stored_payload = cur.fetchone()
    sidecar = mcp._pivot_render_sidecar(
        live_postgres,
        project_id=planned["project_id"],
        result={
            "id": stored_result[0],
            "query_spec_version_id": stored_result[1],
            "outcome": stored_result[2],
            "truncated": stored_result[3],
            "content_hash": stored_result[4],
        },
        payload={
            "content_hash": stored_payload[0],
            "result_schema": stored_payload[1],
            "manifest": stored_payload[2],
            "rows_chunk": stored_payload[3],
        },
    )
    assert sidecar["schema_version"] == "analyze-pivot-render.v1"
    assert sidecar["result_id"] == result["result_id"]
    assert sidecar["matrix"]["row_fields"] == [f"k_{planned['campaign']}"]
    assert sidecar["matrix"]["column_fields"] == [f"k_{planned['day']}"]
    assert sidecar["matrix"]["grand_total"]["overall"][f"m_{planned['spend']}"]["value"] == 18
    assert sidecar["analysis_context"] == stored_plan["analysis_context"]
