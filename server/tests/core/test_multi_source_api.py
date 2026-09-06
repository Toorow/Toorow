"""The two acts of a cross-source analysis, at two addresses (story 66.7's door).

What is proven here is the posture, not the arithmetic: compiling and executing
are separate calls, a refusal keeps its exact code so a screen can render it
beside the field that caused it, and a plan of another contract or another
Project is a 404 rather than a hint that it exists.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

IDENTITY = "owner@example.com"
ORG = "org_EXAMPLE"
PROJECT = "proj_EXAMPLE"
PLANS = f"/api/projects/{PROJECT}/analyze/multi-source/plans"
EXECUTE = f"{PLANS}/qsv_EXAMPLE/execute"


class _Cursor:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        assert "app.projects" in sql, f"unexpected statement: {sql}"

    def fetchone(self):
        return (ORG,)


class _Connection:
    def cursor(self):
        return _Cursor()

    def commit(self):
        return None


def _serving(allowed=True):
    @contextmanager
    def _fake_connection(_identity=None):
        yield _Connection()

    return (
        patch("core.query_specs_api.analyze_connection", _fake_connection),
        patch(
            "core.admin_api._strict_project_capability_allowed",
            return_value=allowed,
        ),
    )


def _client():
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    return TestClient(build_asgi_app(), raise_server_exceptions=False)


def _auth_ok():
    return patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, IDENTITY)))


def test_the_two_acts_have_two_addresses():
    from core.admin_api import router

    paths = [getattr(route, "path", "") for route in router.routes]
    plans = "/api/projects/{project_id}/analyze/multi-source/plans"
    execute = f"{plans}/{{query_spec_version_id}}/execute"
    assert plans in paths
    assert execute in paths
    # Starlette resolves in order: the segment must be declared first.
    assert paths.index(execute) < paths.index(plans)


def test_no_authentication_is_no_answer():
    with patch("core.admin_api._check_auth", new=AsyncMock(return_value=(False, ""))):
        with _client() as client:
            assert client.post(PLANS, json={}).status_code == 401
            assert client.post(EXECUTE, json={}).status_code == 401


def test_a_refused_plan_keeps_its_exact_code_and_its_detail():
    from core.multi_source_plan import PlanRefused

    def _refuse(*_a, **_k):
        raise PlanRefused(
            "ambiguous_relationship",
            "2 approved relationships pin that key.",
            detail=["a", "b"],
        )

    connection, access = _serving()
    with _auth_ok(), connection, access, patch("core.multi_source_plan.compile_plan", _refuse):
        with _client() as client:
            response = client.post(PLANS, json={"members": []})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "ambiguous_relationship"
    assert body["detail"] == ["a", "b"]


def test_a_compiled_plan_answers_201_with_its_frozen_identity():
    connection, access = _serving()
    with _auth_ok(), connection, access, patch(
        "core.multi_source_plan.compile_plan",
        lambda *a, **k: {"plan": {"inclusion_policy": "matched_only"}, "content_hash": "a" * 64},
    ), patch(
        "core.multi_source_plan.store_plan_version",
        lambda *a, **k: {
            "query_spec_id": "qs_1",
            "query_spec_version_id": "qsv_1",
            "content_hash": "a" * 64,
            "contract_version": "multi-source-plan.v1",
        },
    ):
        with _client() as client:
            response = client.post(PLANS, json={"members": []})
    assert response.status_code == 201
    body = response.json()
    assert body["query_spec_version_id"] == "qsv_1"
    assert body["plan"]["inclusion_policy"] == "matched_only"


def test_profile_safety_cannot_be_bypassed_by_a_client_flag():
    connection, access = _serving()
    observed = {}

    def _compile(*_a, **kwargs):
        observed["profile_lookup"] = kwargs.get("profile_lookup")
        return {"plan": {"inclusion_policy": "matched_only"}, "content_hash": "a" * 64}

    with _auth_ok(), connection, access, patch(
        "core.multi_source_plan.compile_plan", _compile
    ), patch(
        "core.multi_source_plan.store_plan_version",
        return_value={
            "query_spec_id": "qs_1",
            "query_spec_version_id": "qsv_1",
            "content_hash": "a" * 64,
            "contract_version": "multi-source-plan.v1",
        },
    ):
        with _client() as client:
            response = client.post(PLANS, json={"members": [], "skip_profile": True})
    assert response.status_code == 201
    assert callable(observed["profile_lookup"])


def test_an_unavailable_profile_refuses_compilation() -> None:
    from core.match_profile import ProfileRefused

    def _compile(*_args, **kwargs):
        kwargs["profile_lookup"]("ds_a", "ds_b", "mckv_1", "rel", "svv_1")
        raise AssertionError("the unavailable profile should have refused")

    def _unavailable(*_args, **_kwargs):
        raise ProfileRefused(
            "no_published_output",
            "The output is unavailable.",
            missing_link="datastream_output_version",
        )

    connection, access = _serving()
    with _auth_ok(), connection, access, patch(
        "core.multi_source_plan.compile_plan", _compile
    ), patch("core.match_profile.profile_match", _unavailable):
        with _client() as client:
            response = client.post(PLANS, json={"members": []})

    assert response.status_code == 422
    assert response.json()["code"] == "profile_unavailable"
    assert response.json()["detail"] == {
        "code": "no_published_output",
        "missing_link": "datastream_output_version",
    }


def test_a_current_profile_receipt_skips_the_second_warehouse_profile() -> None:
    measured = {
        "left": {"state": "exact", "duplicated_keys": 0},
        "right": {"state": "exact", "duplicated_keys": 0},
        "multiplication": {"state": "exact", "worst_case_rows_per_key": 1},
        "execution_safety": "ready",
    }

    def _compile(*_args, **kwargs):
        assert kwargs["profile_lookup"]("ds_a", "ds_b", "mckv_1", "rel", "svv_1") == measured
        return {"plan": {"inclusion_policy": "matched_only"}, "content_hash": "a" * 64}

    connection, access = _serving()
    with _auth_ok(), connection, access, patch(
        "core.multi_source_plan.compile_plan", _compile
    ), patch(
        "core.multi_source_plan.store_plan_version",
        return_value={
            "query_spec_id": "qs_1",
            "query_spec_version_id": "qsv_1",
            "content_hash": "a" * 64,
            "contract_version": "multi-source-plan.v1",
        },
    ), patch("core.match_profile.reuse_profile_receipt", return_value=measured), patch(
        "core.match_profile.profile_match",
        side_effect=AssertionError("the warehouse profile must not run twice"),
    ):
        with _client() as client:
            response = client.post(PLANS, json={"members": [], "profile_receipts": ["signed"]})

    assert response.status_code == 201


def test_an_unwindowed_receipt_falls_back_to_a_windowed_profile() -> None:
    from core.match_profile import ProfileReceiptInvalid

    fresh = {
        "left": {"state": "exact", "duplicated_keys": 0},
        "right": {"state": "exact", "duplicated_keys": 0},
        "multiplication": {"state": "exact", "worst_case_rows_per_key": 1},
        "execution_safety": "ready",
    }

    def _compile(*_args, **kwargs):
        assert kwargs["profile_lookup"]("ds_a", "ds_b", "mckv_1", "rel", "svv_1") == fresh
        return {"plan": {"inclusion_policy": "matched_only"}, "content_hash": "a" * 64}

    connection, access = _serving()
    with _auth_ok(), connection, access, patch(
        "core.multi_source_plan.compile_plan", _compile
    ), patch(
        "core.multi_source_plan.store_plan_version",
        return_value={
            "query_spec_id": "qs_1",
            "query_spec_version_id": "qsv_1",
            "content_hash": "a" * 64,
            "contract_version": "multi-source-plan.v1",
        },
    ), patch(
        "core.match_profile.reuse_profile_receipt",
        side_effect=ProfileReceiptInvalid("wrong window"),
    ) as reuse, patch("core.match_profile.profile_match", return_value=fresh) as profile:
        with _client() as client:
            response = client.post(
                PLANS,
                json={
                    "members": [],
                    "profile_receipts": ["signed"],
                    "filters": [{"stage": "pre_aggregation"}],
                },
            )

    assert response.status_code == 201
    reuse.assert_called_once()
    assert profile.call_args.kwargs["window"] == [{"stage": "pre_aggregation"}]


def test_a_single_source_spec_is_404_at_the_execute_door():
    """Not 422: this door must not confirm that the version exists elsewhere."""
    from core.multi_source_plan import PlanRefused

    def _refuse(*_a, **_k):
        raise PlanRefused("not_a_multi_source_plan", "That version is a single-source request.")

    connection, access = _serving()
    with _auth_ok(), connection, access, patch(
        "core.multi_source_execution.execute_plan", _refuse
    ):
        with _client() as client:
            response = client.post(EXECUTE, json={})
    assert response.status_code == 404


def test_a_refused_execution_is_422_with_its_sentence_and_never_a_503():
    """A refusal is not an outage (AI-293 review, finding 4).

    `ExecutionRefused` fell through to the generic handler, so every named
    refusal this executor raises -- `profile_not_ready`, `profile_expired`, and
    the merge bound re-read on the frozen plan -- reached the person as
    `execution_unavailable`, 503, "the analysis could not be run". That names no
    gesture and invites a retry that cannot succeed.
    """
    from core.multi_source_execution import ExecutionRefused

    def _refuse(*_a, **_k):
        raise ExecutionRefused(
            "merge_key_finer_than_grain",
            "The two sources are matched on day, and this analysis does not show it. "
            "Show it as a dimension, or cross on a key that stops at the grain you "
            "asked for.",
            detail={"components": ["day"]},
        )

    connection, access = _serving()
    with _auth_ok(), connection, access, patch(
        "core.multi_source_execution.execute_plan", _refuse
    ):
        with _client() as client:
            response = client.post(EXECUTE, json={})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "merge_key_finer_than_grain"
    assert "Show it as a dimension" in body["message"]
    assert body["code"] != "execution_unavailable"


def test_an_executed_plan_answers_201_with_its_result():
    connection, access = _serving()
    with _auth_ok(), connection, access, patch(
        "core.multi_source_execution.execute_plan",
        lambda *a, **k: {"result_id": "qr_1", "outcome": "success", "row_count": 3},
    ):
        with _client() as client:
            response = client.post(EXECUTE, json={})
    assert response.status_code == 201
    assert response.json()["result"]["outcome"] == "success"


def test_a_project_of_another_org_is_404_at_both_addresses():
    connection, access = _serving(allowed=False)
    with _auth_ok(), connection, access, _client() as client:
        assert client.post(PLANS, json={}).status_code == 404
        assert client.post(EXECUTE, json={}).status_code == 404
