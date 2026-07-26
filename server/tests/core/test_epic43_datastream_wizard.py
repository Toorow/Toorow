"""Focused HTTP contract tests for Story 43.14 Datastream activation."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

from starlette.routing import Route, Router
from starlette.testclient import TestClient


def _activation_client(
    monkeypatch,
    locked_plan,
    *,
    existing_versioned=True,
    health_row=None,
    activation_evidence=None,
):
    import core.admin_api as admin_api
    import core.datastreams as datastreams
    import core.db as db

    async def _auth(_request):
        return True, "operator@example.com"

    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    # Return the authoritative row for every lock read and no prior activation
    # evidence for the per-key audit lookup.
    last_sql = {"value": ""}

    def _execute(sql, *args, **kwargs):
        last_sql["value"] = sql

    def _fetchone():
        if "FOR UPDATE OF d" in last_sql["value"]:
            return locked_plan
        if "metadata->>'activation_key_hash'" in last_sql["value"]:
            return activation_evidence
        return None

    cursor.execute.side_effect = _execute
    cursor.fetchone.side_effect = _fetchone
    conn = MagicMock()
    conn.cursor.return_value = cursor

    @contextmanager
    def _connection():
        yield conn

    updates: list[dict] = []

    def _get_datastream(datastream_id, project_id, _conn):
        return {
            "id": datastream_id,
            "project_id": project_id,
            "versioned": existing_versioned,
            "config": {},
            "enabled": bool(len(locked_plan) > 5 and locked_plan[5]),
            "schedule_mode": locked_plan[6] if len(locked_plan) > 6 else "manual",
        }

    def _update_datastream(datastream_id, project_id, body, _conn):
        updates.append(body)
        return {
            "id": datastream_id,
            "project_id": project_id,
            "enabled": body["enabled"],
            "schedule_mode": body["schedule_mode"],
        }

    monkeypatch.setattr(admin_api, "_check_auth", _auth)
    monkeypatch.setattr(admin_api, "_enforce_datastream_project_scope", lambda *a, **kw: None)
    monkeypatch.setattr(admin_api, "write_audit_row", lambda **kw: None)
    monkeypatch.setattr(datastreams, "get_datastream", _get_datastream)
    monkeypatch.setattr(datastreams, "update_datastream", _update_datastream)
    monkeypatch.setattr(db, "get_connection", _connection)

    app = Router(
        routes=[
            Route(
                "/api/datastreams/{id}",
                endpoint=admin_api._patch_datastream,
                methods=["PATCH"],
            )
        ]
    )
    return (
        TestClient(
            app,
            raise_server_exceptions=False,
            headers={"Idempotency-Key": "activation-key"},
        ),
        updates,
        cursor,
        conn,
    )


def test_activation_locks_and_enables_the_exact_validated_plan(monkeypatch):
    client, updates, cursor, conn = _activation_client(
        monkeypatch,
        ("dsp-validated", True, {"schedule": {"mode": "daily"}}, {}, None),
    )

    response = client.patch(
        "/api/datastreams/ds-1",
        json={
            "project_id": "proj-1",
            "enabled": True,
            "plan_version_id": "dsp-validated",
        },
    )

    assert response.status_code == 200
    assert response.json()["enabled"] is True
    assert updates == [{"enabled": True, "schedule_mode": "nightly"}]
    lock_call = next(
        call for call in cursor.execute.call_args_list if "FOR UPDATE OF d" in call.args[0]
    )
    assert lock_call.args[1] == ("ds-1", "proj-1")
    conn.commit.assert_called_once()


def test_activation_rejects_a_concurrently_revised_plan(monkeypatch):
    client, updates, _cursor, conn = _activation_client(
        monkeypatch,
        ("dsp-current", True, {"schedule": {"mode": "daily"}}, {}, None),
    )

    response = client.patch(
        "/api/datastreams/ds-1",
        json={
            "project_id": "proj-1",
            "enabled": True,
            "plan_version_id": "dsp-stale",
        },
    )

    assert response.status_code == 409
    assert response.json() == {
        "code": "stale_plan_version",
        "message": "Le plan valide n'est plus le plan courant. Revalidez le flux avant activation.",
        "details": {
            "requested_plan_version_id": "dsp-stale",
            "current_plan_version_id": "dsp-current",
        },
    }
    assert updates == []
    conn.commit.assert_not_called()


def test_activation_requires_a_plan_version_and_executable_evidence(monkeypatch):
    client, updates, cursor, _conn = _activation_client(
        monkeypatch,
        ("dsp-current", False, {"schedule": {"mode": "daily"}}, {}, None),
    )

    missing = client.patch(
        "/api/datastreams/ds-1",
        json={"project_id": "proj-1", "enabled": True},
    )
    assert missing.status_code == 422
    assert missing.json()["code"] == "missing_plan_version_id"
    assert "FOR UPDATE OF d" in cursor.execute.call_args.args[0]

    blocked = client.patch(
        "/api/datastreams/ds-1",
        json={
            "project_id": "proj-1",
            "enabled": True,
            "plan_version_id": "dsp-current",
        },
    )
    assert blocked.status_code == 422
    assert blocked.json()["code"] == "activation_not_available"
    assert updates == []

def test_activation_uses_locked_pointer_when_prelock_snapshot_looked_legacy(monkeypatch):
    client, updates, _cursor, conn = _activation_client(
        monkeypatch,
        ("dsp-concurrent", True, {"schedule": {"mode": "daily"}}, {}, None),
        existing_versioned=False,
    )

    response = client.patch(
        "/api/datastreams/ds-1",
        json={"project_id": "proj-1", "enabled": True},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "missing_plan_version_id"
    assert updates == []
    conn.commit.assert_not_called()


def test_activation_rechecks_connector_account_and_capability_fingerprint(monkeypatch):
    from types import SimpleNamespace

    import core.datastream_intents as intents
    import core.main as main
    import core.source_capabilities as source_capabilities

    normalized_intent = {
        "source": {
            "kind": "connector_pull",
            "connection_ref_id": "cref-1",
        },
        "schedule": {"mode": "daily"},
    }
    client, updates, _cursor, _conn = _activation_client(
        monkeypatch,
        ("dsp-current", True, normalized_intent, {}, "fingerprint-old"),
        health_row=("active", True, "ok"),
    )
    monkeypatch.setattr(main, "get_loaded_modules", lambda: [])
    monkeypatch.setattr(
        source_capabilities,
        "get_project_connection_state",
        lambda **kwargs: ("google-ads", "active", True, "ok"),
    )
    monkeypatch.setattr(
        source_capabilities,
        "get_scoped_source_capabilities",
        lambda **kwargs: {"contract_version": "2"},
    )
    monkeypatch.setattr(
        intents,
        "validate_intent",
        lambda *args, **kwargs: SimpleNamespace(
            executable=True,
            capability_fingerprint="fingerprint-new",
        ),
    )

    response = client.patch(
        "/api/datastreams/ds-1",
        json={
            "project_id": "proj-1",
            "enabled": True,
            "plan_version_id": "dsp-current",
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "stale_capabilities"
    assert updates == []


def test_activation_rejects_a_revoked_connector_account(monkeypatch):
    import core.source_capabilities as source_capabilities

    normalized_intent = {
        "source": {
            "kind": "connector_pull",
            "connection_ref_id": "cref-revoked",
        },
        "schedule": {"mode": "daily"},
    }
    client, updates, _cursor, _conn = _activation_client(
        monkeypatch,
        ("dsp-current", True, normalized_intent, {}, "fingerprint-old"),
        health_row=("active", True, "revoked"),
    )
    monkeypatch.setattr(
        source_capabilities,
        "get_project_connection_state",
        lambda **kwargs: ("google-ads", "active", True, "revoked"),
    )
    response = client.patch(
        "/api/datastreams/ds-1",
        json={
            "project_id": "proj-1",
            "enabled": True,
            "plan_version_id": "dsp-current",
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "provider_account_unusable"
    assert updates == []


def test_activation_replay_skips_duplicate_update_and_audit(monkeypatch):
    import core.admin_api as admin_api

    client, updates, _cursor, conn = _activation_client(
        monkeypatch,
        (
            "dsp-current",
            True,
            {"source": {"kind": "managed_feed"}, "schedule": {"mode": "daily"}},
            {},
            None,
            True,
            "nightly",
        ),
        activation_evidence=("dsp-current",),
    )
    audits: list[dict] = []
    monkeypatch.setattr(admin_api, "write_audit_row", lambda **kwargs: audits.append(kwargs))

    response = client.patch(
        "/api/datastreams/ds-1",
        json={
            "project_id": "proj-1",
            "enabled": True,
            "plan_version_id": "dsp-current",
        },
    )

    assert response.status_code == 200
    assert response.json()["enabled"] is True
    assert updates == []
    assert audits == []
    conn.commit.assert_not_called()
