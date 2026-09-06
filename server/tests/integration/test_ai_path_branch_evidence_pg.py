"""Real Postgres/FastMCP proof for Story 65.7 branch evidence."""

from __future__ import annotations

import json

import pytest

from tests.fixture_generators.ai_path_branch_evidence import build_fixture, run_scenario

pytestmark = [pytest.mark.anyio, pytest.mark.pg]


async def test_real_search_http_rls_hash_immutability_and_rollback(live_postgres):
    proof = await run_scenario(live_postgres.info.dsn)

    assert proof["mcp_succeeded"] is True
    assert proof["mcp_invalid_token_refused"] is True
    assert proof["http_invalid_token_refused"] is True
    assert proof["runtime_role"] == {
        "superuser": False,
        "bypass_rls": False,
        "paths_rls": True,
        "paths_force_rls": True,
        "steps_rls": True,
        "steps_force_rls": True,
    }
    assert proof["own_list_status"] == 200
    assert proof["owner_list_discovers_exact_path"] is True
    assert proof["foreign_list_is_exactly_empty"] is True
    assert proof["own_detail_status"] == 200
    assert proof["api_detail"]["schema_version"] == "ai-path.v2"
    assert proof["observed_projection"]["schema_version"] == "observed-ai-path.v1"
    assert proof["branch_bytes_equal"] is True
    evidence = proof["api_detail"]["steps"][0]["branch_evidence"]
    assert evidence["walk"]["selected_count"] == 20
    assert evidence["walk"]["rejected_count"] == 1
    assert {branch["fate"] for branch in evidence["branches"]} == {"selected", "rejected"}
    assert proof["foreign_matches_missing"] is True
    assert proof["foreign_and_missing_are_exact_nondisclosing_404s"] is True
    assert proof["foreign_secret_absent"] is True
    assert proof["content_hash_recomputed"] is True
    assert proof["content_hash_recomputed_independently"] is True
    assert proof["append_only_refused"] is True
    assert proof["stored_detail_unchanged"] is True
    assert proof["rollback_left_no_path"] is True
    assert proof["rollback_payload_byte_equal"] is True
    assert proof["every_branch_schema_valid"] is True
    assert proof["branch_forbidden_fields_absent"] is True
    assert all("detail" not in step for step in proof["api_detail"]["steps"])
    assert "candidate_ids" not in json.dumps(proof["api_detail"], sort_keys=True)


async def test_generated_fixture_is_only_the_two_real_public_projections(live_postgres):
    fixture = await build_fixture(live_postgres.info.dsn)

    assert set(fixture) == {"schema_version", "api_detail", "observed_projection"}
    assert fixture["schema_version"] == "ai-path-branch-parity.v1"
    assert fixture["api_detail"]["schema_version"] == "ai-path.v2"
    assert fixture["observed_projection"]["schema_version"] == "observed-ai-path.v1"
    api_branches = [
        step.get("branch_evidence") for step in fixture["api_detail"]["steps"]
    ]
    observed_branches = [
        step.get("branch_evidence") for step in fixture["observed_projection"]["steps"]
    ]
    assert api_branches == observed_branches
