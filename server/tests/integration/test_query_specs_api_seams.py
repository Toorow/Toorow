"""Story 50.1 -- real-ASGI seams for the analytical API.

These run through `build_asgi_app()`, so they prove the routes are actually
MOUNTED. That distinction is not academic here: this repository has an orphaned
handler (`_create_datastream_mapping_version`) whose own test file asserted
behaviour for a route nobody had mounted, and every one of those tests answered
405 for months. A seam test is the only kind that catches that.

Kept deliberately small -- the ASGI seam suite is slow, and each test here buys a
distinct property rather than a variation of the same one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.testclient import TestClient

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

BASE = "/api/projects/proj_EXAMPLE/analyze"


def _fastmcp_golden_input():
    fixture = (
        Path(__file__).resolve().parents[3]
        / "ui/cards/shell/src/viz/__tests__/fixtures/analyzeRenderToolResult.json"
    )
    wire = json.loads(fixture.read_text(encoding="utf-8"))
    return wire["_meta"]["toorow.app_payload"]["render_input"]


def _client() -> TestClient:
    from core.main import build_asgi_app

    return TestClient(build_asgi_app(), raise_server_exceptions=False)


class _ScriptedCursor:
    """Answers by which table the statement names, so a handler that reads two
    columns from one query and one from another is not silently fed the wrong
    arity -- which is how a fixture invents a 500 the code never had."""

    def __init__(self, result_evidence=None, statements=None, render=None, query_specs=None):
        self._last = ""
        self._result_evidence = result_evidence
        self._statements = statements
        self._render = render
        self._query_specs = query_specs

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        self._last = sql
        if self._statements is not None:
            self._statements.append((sql, params))

    def fetchone(self):
        if "app.projects" in self._last:
            return ("org_EXAMPLE",)
        if "app.query_results" in self._last and "app.query_result_payloads" in self._last:
            return self._result_evidence
        if "FROM app.renders" in self._last:
            return self._render
        if "app.query_spec_versions" in self._last:
            return ({"measures": [], "dimensions": []}, "svv_1")
        if "app.query_specs" in self._last:
            return ("sv_1",)
        return None

    def fetchall(self):
        if self._query_specs is not None and "FROM app.query_specs" in self._last:
            return self._query_specs
        return []


def _connection(result_evidence=None, statements=None, render=None, query_specs=None):
    conn = MagicMock()
    conn.cursor = MagicMock(
        side_effect=lambda: _ScriptedCursor(result_evidence, statements, render, query_specs)
    )
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=conn)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def _authed(role_ok=True, *, result_evidence=None, statements=None, render=None, query_specs=None):
    from starlette.responses import JSONResponse

    denial = None if role_ok else JSONResponse({"code": "not_found"}, 404)
    return (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person-1"))),
        patch("core.admin_api._require_datastream_role", return_value=denial),
        patch(
            "core.db.get_connection",
            return_value=_connection(result_evidence, statements, render, query_specs),
        ),
    )


def _recorded_eligibility():
    return patch(
        "core.feedback_review.record_feedback_eligibility",
        return_value={"schema_version": "feedback-eligibility.v1"},
        create=True,
    )


def test_the_analytical_routes_are_actually_mounted():
    """A 404 route envelope, never a 405 -- the failure this file exists to catch."""
    auth, role, db = _authed()
    with auth, role, db:
        response = _client().post(
            f"{BASE}/query-specs",
            json={"semantic_view_id": "sv_1", "semantic_view_version_id": "svv_1", "spec": {}},
        )
    assert response.status_code != 405, "the route is not mounted"


def test_the_query_spec_collection_is_mounted_and_bounded():
    """`GET /query-specs` -- the route the Topics picker reads.

    Its absence is what forced the Answerable Topic catalog to ask an operator
    to paste a `qsv_…` from memory. Asserting 200 here rather than "not 405"
    also proves the LITERAL `/query-specs` was not captured by another pattern.
    """
    auth, role, db = _authed(
        query_specs=[
            ("qs_1", "Weekly clicks", "sv_1", "qsv_9", 3, None),
            ("qs_0", None, "sv_1", None, None, None),
        ]
    )
    with auth, role, db:
        response = _client().get(f"{BASE}/query-specs")

    assert response.status_code == 200
    body = response.json()
    assert body["query_specs"][0] == {
        "id": "qs_1",
        "name": "Weekly clicks",
        "semantic_view_id": "sv_1",
        # The pinnable identity and the number that names WHICH version it is.
        "current_version_id": "qsv_9",
        "current_version_number": 3,
        "created_at": None,
    }
    # An unnamed spec keeps its null: the route invents no title.
    assert body["query_specs"][1]["name"] is None
    assert body["query_specs"][1]["current_version_id"] is None
    assert body["next_cursor"] is None


def test_an_empty_project_gets_an_empty_collection_not_a_refusal():
    auth, role, db = _authed()
    with auth, role, db:
        response = _client().get(f"{BASE}/query-specs")

    assert response.status_code == 200
    assert response.json() == {"query_specs": [], "next_cursor": None}


def test_the_collection_refuses_a_bound_it_cannot_honour_rather_than_defaulting():
    """A silently coerced `limit` gives a picker a page nobody asked for, and the
    caller never learns its request was ignored."""
    auth, role, db = _authed()
    with auth, role, db:
        client = _client()
        not_a_number = client.get(f"{BASE}/query-specs?limit=all")
        too_large = client.get(f"{BASE}/query-specs?limit=5000")
        invented = client.get(f"{BASE}/query-specs?project_id=proj_OTHER")

    assert not_a_number.status_code == 422
    assert not_a_number.json()["code"] == "invalid_query"
    assert too_large.status_code == 422
    # The Project is the PATH's, never a second scope smuggled in beside it.
    assert invented.status_code == 422
    assert invented.json()["message"].endswith("project_id")


def test_a_denied_project_cannot_read_the_collection_either():
    """Same envelope as a missing one, and no count leaked (AC10)."""
    auth, role, db = _authed(
        role_ok=False, query_specs=[("qs_secret", "Secret", "sv", "qsv", 1, None)]
    )
    with auth, role, db:
        denied = _client().get(f"{BASE}/query-specs")

    assert denied.status_code == 404
    assert denied.json()["code"] == "not_found"
    assert "qs_secret" not in denied.text


def test_an_unauthenticated_caller_cannot_enumerate_the_collection():
    with patch("core.admin_api._check_auth", new=AsyncMock(return_value=(False, None))):
        response = _client().get(f"{BASE}/query-specs")
    assert response.status_code == 401


def test_an_unauthenticated_caller_is_refused_before_any_work():
    with patch("core.admin_api._check_auth", new=AsyncMock(return_value=(False, None))):
        response = _client().get(f"{BASE}/results/qr_anything")
    assert response.status_code == 401


def test_a_denied_project_answers_the_same_envelope_as_a_missing_one():
    auth, role, db = _authed(role_ok=False)
    with auth, role, db:
        denied = _client().get(f"{BASE}/results/qr_secret")
    assert denied.status_code == 404
    assert denied.json()["code"] == "not_found"
    # No count, no name, no hint about whether the Result exists (AC10).
    assert "qr_secret" not in denied.text


def test_a_half_pinned_semantic_view_is_refused_rather_than_completed():
    auth, role, db = _authed()
    with auth, role, db:
        response = _client().post(
            f"{BASE}/query-specs", json={"semantic_view_id": "sv_1", "spec": {}}
        )
    assert response.status_code == 400
    assert response.json()["code"] == "missing_field"


def test_a_semantic_refusal_reaches_the_caller_with_all_its_reasons():
    from core.query_specs import QuerySpecRefused, Refusal

    auth, role, db = _authed()
    refusal = QuerySpecRefused(
        "invalid_query_spec",
        "refused on 2 point(s)",
        [Refusal("unknown_member", "no such measure", "sc_ghost"),
         Refusal("no_measure", "needs a measure", "measures")],
    )
    with auth, role, db, patch("core.query_specs_api.validate_query_spec", side_effect=refusal):
        response = _client().post(
            f"{BASE}/query-specs",
            json={"semantic_view_id": "sv_1", "semantic_view_version_id": "svv_1", "spec": {}},
        )
    assert response.status_code == 422
    body = response.json()
    assert {r["code"] for r in body["refusals"]} == {"unknown_member", "no_measure"}
    assert body["refusals"][0]["subject"] == "sc_ghost"


def test_evidence_is_not_captured_by_the_result_id_route():
    """Literal-before-parameter regression: `/results/{id}/evidence` must win."""
    from core.query_specs_api import query_spec_routes

    paths = [r.path for r in query_spec_routes]
    evidence = paths.index("/api/projects/{project_id}/analyze/results/{result_id}/evidence")
    bare = paths.index("/api/projects/{project_id}/analyze/results/{result_id}")
    assert evidence < bare, "the evidence route must be declared before the bare Result route"

    # And prove it at the transport level: the evidence path must not be served
    # by the bare Result handler. Patching the module attribute would prove
    # nothing -- Starlette captured the function object at import time -- so the
    # proof is that the two paths return DIFFERENT shapes.
    auth, role, db = _authed()
    with auth, role, db:
        evidence = _client().get(f"{BASE}/results/qr_1/evidence")
        bare = _client().get(f"{BASE}/results/qr_1")
    assert evidence.status_code == 404 and bare.status_code == 404
    assert "evidence" not in bare.text


def test_result_evidence_carries_the_result_truth_from_one_scoped_join():
    digest = "a" * 64
    statements = []
    auth, role, db = _authed(
        result_evidence=(
            "degraded",
            2,
            False,
            digest,
            digest,
            {"fields": [{"name": "sessions"}]},
            {
                "freshness": "2026-08-10T09:00:00Z",
                "query_sql": "SELECT * FROM private",
                "physical_relation": "warehouse.secret_table",
                "renderer_options": {"series": [{"access_token": "secret"}]},
                "unavailable_reason": (
                    "404 Not found: Table acme-prod:marketing.customer_events "
                    "was not found in location EU"
                ),
                "missing_link": "acme-prod:marketing.customer_events",
                "options": {"series": [1]},
            },
            [{"sessions": 3}, {"sessions": 5}],
        ),
        statements=statements,
    )
    with auth, role, db, _recorded_eligibility():
        response = _client().get(f"{BASE}/results/qr_1/evidence")

    assert response.status_code == 200
    payload = response.json()
    feedback_context = payload.pop("feedback_context")
    assert feedback_context["schema_version"] == "exact-feedback.v1"
    assert set(feedback_context) == {
        "schema_version", "token", "interaction_ref", "expires_at"
    }
    assert payload == {
        "result_id": "qr_1",
        "content_hash": digest,
        "outcome": "degraded",
        "row_count": 2,
        "truncated": False,
        "schema": {"fields": [{"name": "sessions"}]},
        "manifest": {
            "freshness": "2026-08-10T09:00:00Z",
            "unavailable_reason": "The source could not produce this result.",
            "missing_link": "source_output",
        },
        "rows": [{"sessions": 3}, {"sessions": 5}],
    }
    sql, params = next(
        (sql, params)
        for sql, params in statements
        if "app.query_results" in sql and "app.query_result_payloads" in sql
    )
    normalized = " ".join(sql.split())
    assert (
        "ON p.result_id = r.id AND p.org_id = r.org_id AND p.project_id = r.project_id"
        in normalized
    )
    assert "WHERE r.id = %s AND r.org_id = %s AND r.project_id = %s" in normalized
    assert params == ("qr_1", "org_EXAMPLE", "proj_EXAMPLE")


def test_result_evidence_with_render_attests_exact_pins_and_visible_window(monkeypatch):
    from core.analyze_feedback import verify_feedback_context

    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    digest = "a" * 64
    render = (
        "rnd_1",
        "qr_1",
        digest,
        "vsv_1",
        "renderer@1",
        "runtime@1",
        "theme@1",
        "formatter@1",
        {"bindings": {"measure": ["running_total_micros"]}},
    )
    auth, role, db = _authed(
        result_evidence=(
            "success",
            1,
            False,
            digest,
            digest,
            {"fields": [{"name": "running_total_micros"}]},
            {},
            [{"running_total_micros": 7}],
            None,
            None,
        ),
        render=render,
    )
    with auth, role, db, _recorded_eligibility():
        response = _client().get(f"{BASE}/results/qr_1/evidence?render_id=rnd_1")

    assert response.status_code == 200
    claims = verify_feedback_context(response.json()["feedback_context"])
    assert claims["render_id"] == "rnd_1"
    assert claims["visualization_spec_version_id"] == "vsv_1"
    assert claims["delivered_rows"]["count"] == 1
    assert claims["delivered_rows"]["field_count"] == 1


def test_result_evidence_hides_a_foreign_or_mismatched_render():
    digest = "a" * 64
    auth, role, db = _authed(
        result_evidence=("success", 0, False, digest, digest, {"fields": []}, {}, [], None, None),
        render=(
            "rnd_1",
            "qr_foreign",
            digest,
            "vsv_1",
            "r",
            "rt",
            "t",
            "f",
            {"bindings": {}},
        ),
    )
    with auth, role, db:
        response = _client().get(f"{BASE}/results/qr_1/evidence?render_id=rnd_1")

    assert response.status_code == 404
    assert response.json() == {"code": "not_found", "message": "Not found"}


def test_fastmcp_golden_traverses_the_real_console_evidence_route():
    result = _fastmcp_golden_input()["result"]
    auth, role, db = _authed(
        result_evidence=(
            result["outcome"], result["row_count"], result["truncated"],
            result["content_hash"], result["content_hash"], result["schema"],
            result["manifest"], result["rows"],
        )
    )
    with auth, role, db, _recorded_eligibility():
        response = _client().get(f"{BASE}/results/{result['result_id']}/evidence")

    assert response.status_code == 200
    payload = response.json()
    feedback_context = payload.pop("feedback_context")
    assert feedback_context["schema_version"] == "exact-feedback.v1"
    assert payload == {
        "result_id": result["result_id"],
        "content_hash": result["content_hash"],
        "outcome": result["outcome"],
        "row_count": result["row_count"],
        "truncated": result["truncated"],
        "schema": result["schema"],
        "manifest": result["manifest"],
        "rows": result["rows"],
    }


def test_result_evidence_refuses_a_payload_whose_hash_diverged():
    auth, role, db = _authed(
        result_evidence=("success", 1, False, "a" * 64, "b" * 64, {}, {}, [{}])
    )
    with auth, role, db:
        response = _client().get(f"{BASE}/results/qr_1/evidence")

    assert response.status_code == 409
    assert response.json() == {
        "code": "result_identity_mismatch",
        "message": (
            "The Result payload does not match its immutable identity. "
            "Run the analysis again to create a new Result."
        ),
    }


def test_an_unavailable_outcome_is_delivered_as_an_answer_not_a_transport_error():
    auth, role, db = _authed()
    attempt = {"attempt_id": "qea_1", "result_id": "qr_1", "query_spec_version_id": "qsv_1"}
    result = {
        "result_id": "qr_1",
        "outcome": "unavailable",
        "row_count": 0,
        "truncated": False,
        "content_hash": "a" * 64,
        "ai_path": "No AI path",
    }
    with (
        auth,
        role,
        db,
        patch("core.query_specs_api.accept_execution", return_value=attempt),
        patch("core.query_specs_api.run_execution", return_value=result),
    ):
        response = _client().post(f"{BASE}/query-spec-versions/qsv_1/execute")
    assert response.status_code == 202, "an unavailable Result is still a delivered Result"
    body = response.json()
    assert body["outcome"] == "unavailable"
    assert body["ai_path"] == "No AI path"
