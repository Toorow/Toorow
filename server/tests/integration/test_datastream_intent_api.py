"""Real-ASGI seams for versioned Datastream intent operations (Story 12.2)."""

from __future__ import annotations

import os
from datetime import date
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
        patch("core.datastreams_api.write_audit_row"),
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
    # `SELECT p.org_id, o.status, m.role, m.status` with no membership row: the
    # strict resolver sees an org whose caller has no ACTIVE membership.
    cur.fetchone.return_value = ("org_a", "active", None, None)
    conn.cursor.return_value = cur
    context = MagicMock()
    context.__enter__ = MagicMock(return_value=conn)
    context.__exit__ = MagicMock(return_value=False)
    with (
        # Without a production auth mode the strict resolver refuses before it
        # queries anything, so the SQL below would never run and the test would
        # pass while proving nothing about WHERE authority comes from.
        patch.dict(os.environ, {"TOOROW_AUTH_MODE": "oauth"}),
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "user-1"))),
        patch("core.db.get_connection", return_value=context),
        patch("core.datastreams_api.write_audit_row"),
        patch("core.datastreams.list_datastreams") as listing,
        client,
    ):
        response = client.get("/api/datastreams", params={"project_id": "proj_a"})
    assert response.status_code == 404
    listing.assert_not_called()
    # Story 46.4 retired `app.project_members`; authority is an ACTIVE
    # `app.org_members` row plus an exact `app.resource_grants` row, and this
    # caller has neither.
    executed = " ".join(str(call.args[0]) for call in cur.execute.call_args_list)
    assert "app.project_members" not in executed
    assert "app.org_members" in executed


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
        patch("core.datastreams_api.write_audit_row"),
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


def test_versioned_run_and_refetch_BOTH_reach_the_queue_through_the_shared_gates():
    """The two dispatch doors of a versioned stream, at THEIR OWN addresses.

    The re-collection moved to `datastream_collection_api.REFETCH_ROUTE_PATH` in story 58.4; the
    address is taken from the constant the router itself mounts, so the next move
    cannot leave this testing a door that is not there.

    REVERSED 2026-08-12. This case asserted `422 dispatch_not_available` on both
    doors -- the refusal that made this build unable to collect anything at all:
    the dispatchers only select `nightly|weekly|hourly`, and on the live base the
    single active, enabled, mapped Datastream is `manual`. So no clock would ever
    pick it up and both manual doors turned it away. A run is now the same object
    whoever asks for it: same gates, same declaration, same window resolver
    (`core.datastream_dispatch`).
    """
    from core.datastream_collection_api import REFETCH_ROUTE_PATH

    client, auth, database, role = _client(role_allowed=True)
    dispatchable = {
        "ds_id": "ds_01",
        "project_id": "proj_a",
        "module_name": "meta-ads",
        "connection_ref_id": "conn_01",
        "cr_status": "active",
        "cr_enabled": True,
        "enabled": True,
        "archived_at": None,
        "lifecycle_state": "active",
        "source_kind": "connector_pull",
        "schedule_mode": "manual",
        "date_window_days": 30,
        "refetch_days": 3,
        "window_offset_days": 1,
        "current_plan_version_id": "dsp_01",
        "current_mapping_version_id": "dsm_01",
        "module_enabled": True,
        "project_status": "active",
    }
    refetch_url = REFETCH_ROUTE_PATH.format(project_id="proj_a", datastream_id="ds_01")
    job = {"job_id": "job_01", "pull_id": "pull_01", "state": "queued"}
    with (
        auth,
        database,
        role,
        patch("core.datastream_dispatch.load_dispatch_row", return_value=dispatchable),
        patch("core.queue.enqueue_pull", return_value=job) as enqueue,
        # The RUN LINE is story 63.1's subject and not this case's: against a
        # MagicMock cursor the instrumentation can only answer "no run", which
        # is exactly what it answers in production when one is already in flight.
        patch("core.datastream_collection_api._read_active_run", return_value=None),
        client,
    ):
        run_response = client.post("/api/datastreams/ds_01/run", json={"project_id": "proj_a"})
        refetch_response = client.post(refetch_url, json={"dates": ["2026-07-18"]})
    assert run_response.status_code == 202, run_response.text
    assert refetch_response.status_code == 202, refetch_response.text
    assert enqueue.call_count == 2
    # The manual run covers the DECLARED retrieval window (30 days), never the
    # legacy alias standing next to it (3).
    run_args = enqueue.call_args_list[0].args
    span = date.fromisoformat(run_args[2]) - date.fromisoformat(run_args[1])
    assert span.days + 1 == 30


def test_a_stopped_datastream_is_refused_by_hand_exactly_as_by_the_clock():
    """The gate this door never had. A person stops a Datastream; `Run now`
    used to collect anyway and spend the source account's quota."""
    client, auth, database, role = _client(role_allowed=True)
    stopped = {
        "ds_id": "ds_01",
        "project_id": "proj_a",
        "module_name": "meta-ads",
        "connection_ref_id": "conn_01",
        "cr_status": "active",
        "cr_enabled": True,
        "enabled": False,
        "archived_at": None,
        "lifecycle_state": "paused",
        "source_kind": "connector_pull",
        "schedule_mode": "nightly",
        "date_window_days": 30,
        "refetch_days": 3,
        "window_offset_days": 1,
        "current_plan_version_id": "dsp_01",
        "current_mapping_version_id": "dsm_01",
        "module_enabled": True,
        "project_status": "active",
    }
    with (
        auth,
        database,
        role,
        patch("core.datastream_dispatch.load_dispatch_row", return_value=stopped),
        patch("core.queue.enqueue_pull") as enqueue,
        client,
    ):
        response = client.post("/api/datastreams/ds_01/run", json={"project_id": "proj_a"})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "not_armed"
    assert "Schedule" in body["message"], "a refusal names the gesture that repairs it"
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
        patch("core.datastreams_api.write_audit_row"),
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
        patch("core.datastreams.delete_datastream", return_value="archived") as delete,
        patch("core.datastreams_api.write_audit_row"),
        client,
    ):
        response = client.delete("/api/datastreams/ds_01", params={"project_id": "proj_a"})
    assert response.status_code == 200
    assert response.json()["status"] == "archived"
    assert role.call_args.args[2] == "owner"
    delete.assert_called_once_with("ds_01", "proj_a", conn, archived_by="owner-1")


def test_the_route_reports_the_disposition_it_was_given_not_one_it_guessed():
    """AI-200, the half the first fix left open.

    `delete_datastream` archives on THREE conditions -- a pull job, an inbound
    receipt, or a published plan version. The route used to count `app.pull_jobs`
    itself and infer the answer from that ONE condition, so a datastream whose
    only history was an inbound receipt was archived and reported as `deleted`.
    The row survived, the caller was told it was gone, and the QA harness that
    trusted the word kept trying to clean up something that was still there.

    The fixture below is exactly that case: no pull job, no plan version, and an
    archive decision coming back from the owner of the decision.
    """
    app = build_asgi_app()
    client = TestClient(app, raise_server_exceptions=True)
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = (0,)  # zero pull jobs: the old inference said "deleted"
    conn.cursor.return_value = cur
    context = MagicMock()
    context.__enter__ = MagicMock(return_value=conn)
    context.__exit__ = MagicMock(return_value=False)
    existing = {
        "id": "ds_01",
        "project_id": "proj_a",
        "current_plan_version_id": None,  # ... and no plan version either
        "versioned": False,
    }
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "owner-1"))),
        patch("core.db.get_connection", return_value=context),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.datastreams.get_datastream", return_value=existing),
        patch("core.datastreams.delete_datastream", return_value="archived"),
        patch("core.datastreams_api.write_audit_row") as audit,
        client,
    ):
        response = client.delete("/api/datastreams/ds_01", params={"project_id": "proj_a"})

    assert response.status_code == 200
    assert response.json()["status"] == "archived", (
        "the route re-derived the disposition instead of reporting the one it was "
        "given: a datastream held by an inbound receipt is archived, not deleted"
    )
    # The audit row must carry the same word, or the trail records a disposition
    # that never happened.
    assert audit.call_args.kwargs["metadata"]["disposition"] == "archived"


def test_a_datastream_with_no_history_is_reported_deleted():
    """The other side of the same coin, so the fix above cannot become blanket."""
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
        "current_plan_version_id": None,
        "versioned": False,
    }
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "owner-1"))),
        patch("core.db.get_connection", return_value=context),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.datastreams.get_datastream", return_value=existing),
        patch("core.datastreams.delete_datastream", return_value="deleted"),
        patch("core.datastreams_api.write_audit_row"),
        client,
    ):
        response = client.delete("/api/datastreams/ds_01", params={"project_id": "proj_a"})

    assert response.status_code == 200
    assert response.json()["status"] == "deleted"


def test_a_missing_datastream_is_still_a_404_after_the_return_type_changed():
    """`None` replaced `False`; a falsy-but-different value must not become a 200."""
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
    existing = {"id": "ds_01", "project_id": "proj_a", "current_plan_version_id": None}
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "owner-1"))),
        patch("core.db.get_connection", return_value=context),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.datastreams.get_datastream", return_value=existing),
        patch("core.datastreams.delete_datastream", return_value=None),
        patch("core.datastreams_api.write_audit_row") as audit,
        client,
    ):
        response = client.delete("/api/datastreams/ds_01", params={"project_id": "proj_a"})

    assert response.status_code == 404
    audit.assert_not_called()
