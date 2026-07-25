"""Real-ASGI seams for versioned Datastream intent operations (Story 12.2)."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.testclient import TestClient

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core.main import build_asgi_app  # noqa: E402

from tests.core.test_datastream_intents import _catalog, _intent  # noqa: E402


def _db_context():
    context = MagicMock()
    context.__enter__ = MagicMock(return_value=MagicMock())
    context.__exit__ = MagicMock(return_value=False)
    return context


def _client(role_allowed: bool = True):
    app = build_asgi_app()
    return (
        TestClient(app, raise_server_exceptions=True),
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "user-1"))),
        patch("core.db.get_connection", return_value=_db_context()),
        patch("core.project_access.identity_has_project_role", return_value=role_allowed),
    )


def test_list_requires_viewer_and_denies_without_membership():
    client, auth, database, role = _client(role_allowed=False)
    with (
        auth,
        database,
        role,
        patch("core.admin_api.write_audit_row"),
        patch("core.datastreams.list_datastreams") as listing,
        client,
    ):
        response = client.get("/api/datastreams", params={"project_id": "proj_a"})
    assert response.status_code == 404
    listing.assert_not_called()


def test_versioned_create_requires_member_and_idempotency_header():
    client, auth, database, role = _client(role_allowed=True)
    result = {
        "kind": "datastream",
        "id": "ds_01",
        "changed": True,
        "flow": {"schema_version": "2", "id": "ds_01"},
        "plan_version": {"id": "dsp_01", "executable": True},
        "diff": {},
    }
    body = {"project_id": "proj_a", "name": "Campaign feed", "intent": _intent()}
    with (
        auth,
        database,
        role as role_check,
        patch("core.flows.upsert_flow", return_value=result) as upsert,
        client,
    ):
        response = client.post(
            "/api/datastreams",
            json=body,
            headers={"Idempotency-Key": "request-01"},
        )
    assert response.status_code == 201
    role_check.assert_called_with("proj_a", "user-1", "member", upsert.call_args.args[3])
    definition = upsert.call_args.args[1]
    assert definition["schema_version"] == "2"
    assert definition["idempotency_key"] == "request-01"
    assert "request-01" not in response.text


def test_versioned_create_rejects_missing_idempotency_key():
    client, auth, database, role = _client(role_allowed=True)
    with auth, database, role, patch("core.flows.upsert_flow") as upsert, client:
        response = client.post(
            "/api/datastreams",
            json={"project_id": "proj_a", "name": "Campaign feed", "intent": _intent()},
        )
    assert response.status_code == 400
    assert response.json()["code"] == "missing_idempotency_key"
    upsert.assert_not_called()


def test_version_history_requires_viewer_and_is_project_scoped():
    client, auth, database, role = _client(role_allowed=True)
    versions = [{"id": "dsp_02", "version_number": 2}]
    with (
        auth,
        database,
        role as role_check,
        patch("core.datastream_intents.list_intent_versions", return_value=versions) as listing,
        client,
    ):
        response = client.get("/api/datastreams/ds_01/versions", params={"project_id": "proj_a"})
    assert response.status_code == 200
    assert response.json() == {"versions": versions}
    role_check.assert_called()
    listing.assert_called_once()


def test_validate_safe_draft_returns_422_with_stable_issues():
    from core.datastream_intents import IntentIssue, IntentValidationResult

    client, auth, database, role = _client(role_allowed=True)
    validation = IntentValidationResult(
        normalized_intent=_intent(),
        content_hash="a" * 64,
        executable=False,
        issues=(
            IntentIssue(
                code="incomplete_selection",
                path="$.source.selection",
                message="Selection incomplete.",
                repair={"action": "complete_selection"},
                details={},
            ),
        ),
        capability_contract_version=None,
        capability_fingerprint=None,
    )
    with (
        auth,
        database,
        role,
        patch("core.datastream_intents.validate_intent", return_value=validation),
        patch(
            "core.source_capabilities.get_scoped_source_capabilities",
            return_value=_catalog(),
        ),
        client,
    ):
        response = client.post(
            "/api/datastreams/ds_01/validate",
            json={"project_id": "proj_a", "intent": _intent()},
        )
    assert response.status_code == 422
    assert response.json()["issues"][0]["code"] == "incomplete_selection"


def test_production_zero_membership_fails_closed_through_real_asgi():
    app = build_asgi_app()
    client = TestClient(app, raise_server_exceptions=True)
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = (None, "active")
    conn.cursor.return_value = cur
    context = MagicMock()
    context.__enter__ = MagicMock(return_value=conn)
    context.__exit__ = MagicMock(return_value=False)
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "user-1"))),
        patch("core.db.get_connection", return_value=context),
        patch("core.admin_api.write_audit_row"),
        patch("core.datastreams.list_datastreams") as listing,
        client,
    ):
        response = client.get("/api/datastreams", params={"project_id": "proj_a"})
    assert response.status_code == 404
    listing.assert_not_called()
    assert "LEFT JOIN app.project_members" in str(cur.execute.call_args.args[0])


def test_external_bigquery_intent_requires_owner():
    client, auth, database, role = _client(role_allowed=False)
    intent = _intent()
    intent["source"] = {
        "kind": "external_bq",
        "writer_kind": "external",
        "external_object": {
            "project": "customer-project",
            "dataset": "analytics",
            "object": "daily_export",
            "writer_identity": "customer-pipeline",
        },
        "selection": {
            "selection_mode": "subset",
            "metrics": [],
            "dimensions": [],
            "grain": [],
            "filters": [],
        },
    }
    intent["destination"] = {"policy": "external_read_only"}
    with (
        auth,
        database,
        role as role_check,
        patch("core.admin_api.write_audit_row"),
        patch("core.flows.upsert_flow") as upsert,
        client,
    ):
        response = client.post(
            "/api/datastreams",
            json={"project_id": "proj_a", "name": "Customer BQ", "intent": intent},
            headers={"Idempotency-Key": "request-owner"},
        )
    assert response.status_code == 404
    assert role_check.call_args.args[2] == "owner"
    upsert.assert_not_called()


def test_revision_conflict_is_stable_and_does_not_echo_key():
    from core.flows import FlowConflictError

    client, auth, database, role = _client(role_allowed=True)
    existing = {
        "id": "ds_01",
        "project_id": "proj_a",
        "name": "Campaign feed",
        "versioned": True,
    }
    with (
        auth,
        database,
        role,
        patch("core.datastreams.get_datastream", return_value=existing),
        patch("core.flows.upsert_flow", side_effect=FlowConflictError("idempotency_conflict")),
        client,
    ):
        response = client.patch(
            "/api/datastreams/ds_01",
            json={"project_id": "proj_a", "intent": _intent()},
            headers={"Idempotency-Key": "same-key"},
        )
    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
    assert "same-key" not in response.text


def test_versioned_run_and_refetch_never_reach_legacy_queue():
    client, auth, database, role = _client(role_allowed=True)
    versioned = {
        "id": "ds_01",
        "project_id": "proj_a",
        "versioned": True,
        "connection_ref_id": "conn_01",
    }
    with (
        auth,
        database,
        role,
        patch("core.datastreams.get_datastream", return_value=versioned),
        patch("core.queue.enqueue_pull") as enqueue,
        client,
    ):
        run_response = client.post("/api/datastreams/ds_01/run", json={"project_id": "proj_a"})
        refetch_response = client.post(
            "/api/datastreams/ds_01/refetch",
            json={"project_id": "proj_a", "dates": ["2026-07-18"]},
        )
    assert run_response.status_code == 422
    assert refetch_response.status_code == 422
    assert run_response.json()["code"] == "dispatch_not_available"
    assert refetch_response.json()["code"] == "dispatch_not_available"
    enqueue.assert_not_called()


def test_nonexistent_project_id_yields_404_not_503_on_list():
    """resolve_project_role returning None (no DB row) must surface as 404, never 503."""
    app = build_asgi_app()
    client = TestClient(app, raise_server_exceptions=True)
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = None  # project row does not exist
    conn.cursor.return_value = cur
    context = MagicMock()
    context.__enter__ = MagicMock(return_value=conn)
    context.__exit__ = MagicMock(return_value=False)
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "user-1"))),
        patch("core.db.get_connection", return_value=context),
        patch("core.admin_api.write_audit_row"),
        patch("core.datastreams.list_datastreams") as listing,
        client,
    ):
        response = client.get("/api/datastreams", params={"project_id": "proj_ghost"})
    assert response.status_code == 404
    listing.assert_not_called()


def test_versioned_delete_requires_owner_and_soft_archives_with_actor():
    app = build_asgi_app()
    client = TestClient(app, raise_server_exceptions=True)
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = (0,)
    conn.cursor.return_value = cur
    context = MagicMock()
    context.__enter__ = MagicMock(return_value=conn)
    context.__exit__ = MagicMock(return_value=False)
    existing = {
        "id": "ds_01",
        "project_id": "proj_a",
        "current_plan_version_id": "dsp_01",
        "versioned": True,
    }
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "owner-1"))),
        patch("core.db.get_connection", return_value=context),
        patch("core.project_access.identity_has_project_role", return_value=True) as role,
        patch("core.datastreams.get_datastream", return_value=existing),
        patch("core.datastreams.delete_datastream", return_value=True) as delete,
        patch("core.admin_api.write_audit_row"),
        client,
    ):
        response = client.delete("/api/datastreams/ds_01", params={"project_id": "proj_a"})
    assert response.status_code == 200
    assert response.json()["status"] == "archived"
    assert role.call_args.args[2] == "owner"
    delete.assert_called_once_with("ds_01", "proj_a", conn, archived_by="owner-1")
