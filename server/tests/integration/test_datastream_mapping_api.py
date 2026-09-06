"""Real-ASGI seams for versioned Datastream field mapping operations (Story 12.3).

Six of these pin `POST /api/datastreams/{id}/mapping/versions`, a route that
26695dc RETIRED when it landed the six-tab Workbench. The append moved to
`workbench/mapping/changes` then `confirm` ->
`datastream_change.confirm_change`, which runs inside `execute_operation` and
calls `save_field_mapping(advance_pointer=False, commit=False)`. The Story 12.3
handler keeps the older shape -- no operation, `advance_pointer` at its `True`
default -- so it would ACTIVATE the version it appends, which the governed path
refuses.

They are marked `xfail(strict=True)` rather than deleted or left red. Deleted,
the retirement would leave no trace and the next reader would remount the route
(this session did exactly that). Left red, they would be noise. `strict` means
that if someone remounts the route they turn from expected-failure to
UNEXPECTEDLY PASSING, and the suite says so out loud.

Retargeting them at the governed command belongs to whoever owns the Workbench.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

#: The append is governed elsewhere; see the module docstring.
_retired_post_route = pytest.mark.xfail(
    strict=True,
    reason=(
        "POST /api/datastreams/{id}/mapping/versions was retired by 26695dc; the "
        "governed append is datastream_change.confirm_change"
    ),
)

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core.main import build_asgi_app  # noqa: E402


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


def test_profile_datastream_mapping_requires_member_and_returns_suggestions():
    client, auth, database, role = _client(role_allowed=True)
    field_records = [
        {"field_id": "date", "physical_type": "date", "kind": "date"},
        {"field_id": "cost", "physical_type": "decimal", "kind": "metric"},
    ]
    with (
        auth,
        database,
        role as role_check,
        patch(
            "core.datastreams.get_datastream", return_value={"id": "ds_01", "project_id": "proj_a"}
        ),
        client,
    ):
        response = client.post(
            "/api/datastreams/ds_01/mapping/profile",
            json={"project_id": "proj_a", "field_records": field_records},
        )
    assert response.status_code == 200
    res = response.json()
    assert res["mapping_contract_version"] == "1"
    assert len(res["fields"]) == 2
    role_check.assert_called()


@_retired_post_route
def test_create_mapping_version_requires_idempotency_key():
    client, auth, database, role = _client(role_allowed=True)
    mapping = {
        "mapping_contract_version": "1",
        "source_schema_hash": "a" * 64,
        "plan_version_id": "dsp_01",
        "grain": ["date"],
        "fields": [],
        "ambiguities": [],
    }
    with auth, database, role, client:
        response = client.post(
            "/api/datastreams/ds_01/mapping/versions",
            json={"project_id": "proj_a", "mapping": mapping},
        )
    assert response.status_code == 400
    assert response.json()["code"] == "missing_header"


@_retired_post_route
def test_create_mapping_version_appends_and_audits():
    client, auth, database, role = _client(role_allowed=True)
    mapping = {
        "mapping_contract_version": "1",
        "source_schema_hash": "a" * 64,
        "plan_version_id": "dsp_01",
        "grain": ["date"],
        "fields": [],
        "ambiguities": [],
    }
    saved_result = {"id": "dmap_01", "version_number": 1, "idempotent_replay": False}
    with (
        auth,
        database,
        role,
        patch(
            "core.datastream_field_mapping.save_field_mapping", return_value=saved_result
        ) as save_mock,
        client,
    ):
        response = client.post(
            "/api/datastreams/ds_01/mapping/versions",
            json={"project_id": "proj_a", "mapping": mapping},
            headers={"Idempotency-Key": "key-mapping-01"},
        )
    assert response.status_code == 201
    assert response.json()["id"] == "dmap_01"
    save_mock.assert_called_once()


def test_list_mapping_versions_requires_viewer():
    client, auth, database, role = _client(role_allowed=True)
    versions = [{"id": "dmap_01", "version_number": 1}]
    with (
        auth,
        database,
        role as role_check,
        patch("core.datastream_field_mapping.list_mapping_versions", return_value=versions),
        client,
    ):
        response = client.get(
            "/api/datastreams/ds_01/mapping/versions", params={"project_id": "proj_a"}
        )
    assert response.status_code == 200
    assert response.json() == {"versions": versions}
    assert role_check.call_args.args[2] == "viewer"


def test_get_mapping_version_returns_single_version():
    client, auth, database, role = _client(role_allowed=True)
    version = {"id": "dmap_01", "version_number": 1, "executable": True}
    with (
        auth,
        database,
        role,
        patch("core.datastream_field_mapping.get_mapping_version", return_value=version),
        client,
    ):
        response = client.get(
            "/api/datastreams/ds_01/mapping/versions/1", params={"project_id": "proj_a"}
        )
    assert response.status_code == 200
    assert response.json() == version


def test_compare_mapping_versions_reads_two_and_names_the_concept():
    """2026-08-18. The ledger could not say what one version decided.

    Through the REAL ASGI stack, because the point of this door is that `compare`
    is declared BEFORE `{ver}`: resolved the other way round, this path reaches
    the single-version handler and answers "version introuvable" forever.
    """
    client, auth, database, role = _client(role_allowed=True)
    versions = {
        "dmap_01": {
            "id": "dmap_01",
            "version_number": 1,
            "created_by": "analyst@example.com",
            "mapping_payload": {
                "fields": [{"field_id": "day", "binding": {"status": "suggested"}}]
            },
        },
        "dmap_02": {
            "id": "dmap_02",
            "version_number": 2,
            "created_by": "owner@example.com",
            "mapping_payload": {
                "fields": [{"field_id": "day", "binding": {"status": "suggested",
                                                           "mdm_target": "mdm_EXAMPLE"}}]
            },
        },
    }
    with (
        auth,
        database,
        role as role_check,
        patch(
            "core.datastream_field_mapping.get_mapping_version",
            side_effect=lambda ds, project, spec, conn: versions[spec],
        ),
        patch(
            "core.canonical_field_registry.list_visible_canonical_fields",
            return_value=[{"id": "mdm_EXAMPLE", "canonical_name": "event_date"}],
        ),
        client,
    ):
        response = client.get(
            "/api/datastreams/ds_01/mapping/versions/compare",
            params={"project_id": "proj_a", "base": "dmap_01", "against": "dmap_02"},
        )
    assert response.status_code == 200
    body = response.json()
    # Reading history is a READ, at viewer. The append stays governed elsewhere.
    assert role_check.call_args.args[2] == "viewer"
    assert body["base"]["version_number"] == 1
    assert body["against"]["created_by"] == "owner@example.com"
    # No payload echo: two whole contracts on the wire to say one reading differs.
    assert "mapping_payload" not in body["base"]
    entries = body["value_diff"]["entries"]
    assert body["value_diff"]["state"] == "composed"
    assert [entry["reading"] for entry in entries] == ["Governed target"]
    assert entries[0]["after"] == "event_date (mdm_EXAMPLE)"


def test_compare_mapping_versions_requires_both_versions():
    client, auth, database, role = _client(role_allowed=True)
    with auth, database, role, client:
        response = client.get(
            "/api/datastreams/ds_01/mapping/versions/compare",
            params={"project_id": "proj_a", "base": "dmap_01"},
        )
    assert response.status_code == 400
    assert response.json()["code"] == "missing_param"


def test_mapping_api_denies_unauthorized():
    app = build_asgi_app()
    client = TestClient(app, raise_server_exceptions=True)
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(False, None))),
        client,
    ):
        response = client.get(
            "/api/datastreams/ds_01/mapping/versions", params={"project_id": "proj_a"}
        )
    assert response.status_code == 401


def test_list_mapping_versions_returns_404_for_missing_datastream():
    client, auth, database, role = _client(role_allowed=True)
    from core.datastream_field_mapping import DatastreamMappingNotFound  # noqa: PLC0415
    with (
        auth,
        database,
        role,
        patch(
            "core.datastream_field_mapping.list_mapping_versions",
            side_effect=DatastreamMappingNotFound("Datastream not found"),
        ),
        client,
    ):
        response = client.get(
            "/api/datastreams/non_existent_ds/mapping/versions",
            params={"project_id": "proj_a"},
        )
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


@_retired_post_route
def test_viewer_append_is_denied_404_and_audited():
    """A Viewer (no member role) appending a mapping version gets a non-disclosing
    404 and a cross-scope audit row, never a silent success."""
    client, auth, database, role = _client(role_allowed=False)
    mapping = {
        "mapping_contract_version": "1",
        "source_schema_hash": "a" * 64,
        "plan_version_id": "dsp_01",
        "grain": ["date"],
        "fields": [],
        "ambiguities": [],
    }
    with (
        auth,
        database,
        role,
        patch("core.admin_api.write_audit_row") as audit_mock,
        patch("core.datastream_field_mapping.save_field_mapping") as save_mock,
        client,
    ):
        response = client.post(
            "/api/datastreams/ds_01/mapping/versions",
            json={"project_id": "proj_a", "mapping": mapping},
            headers={"Idempotency-Key": "viewer-append-01"},
        )
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    audit_mock.assert_called_once()
    save_mock.assert_not_called()


def test_cross_project_denial_is_404_and_audited():
    """A member of one project cannot append into another project's Datastream:
    the role check fails -> 404 + audit."""
    client, auth, database, role = _client(role_allowed=False)
    with (
        auth,
        database,
        role,
        patch("core.admin_api.write_audit_row") as audit_mock,
        client,
    ):
        response = client.get(
            "/api/datastreams/ds_other/mapping/versions",
            params={"project_id": "proj_foreign"},
        )
    assert response.status_code == 404
    audit_mock.assert_called_once()


def test_production_zero_membership_defaults_denied():
    """When identity_has_project_role reports no membership, access is denied."""
    client, auth, database, role = _client(role_allowed=False)
    with auth, database, role, client:
        response = client.get(
            "/api/datastreams/ds_01/mapping/versions", params={"project_id": "proj_a"}
        )
    assert response.status_code == 404


@_retired_post_route
def test_idempotent_replay_returns_original_result():
    """A same-key replay surfaces the original version with a 200 (not a new 201)."""
    client, auth, database, role = _client(role_allowed=True)
    mapping = {
        "mapping_contract_version": "1",
        "source_schema_hash": "a" * 64,
        "plan_version_id": "dsp_01",
        "grain": ["date"],
        "fields": [],
        "ambiguities": [],
    }
    replay_result = {"id": "dmap_01", "version_number": 1, "idempotent_replay": True}
    with (
        auth,
        database,
        role,
        patch(
            "core.datastream_field_mapping.save_field_mapping", return_value=replay_result
        ),
        client,
    ):
        response = client.post(
            "/api/datastreams/ds_01/mapping/versions",
            json={"project_id": "proj_a", "mapping": mapping},
            headers={"Idempotency-Key": "replay-key-01"},
        )
    assert response.status_code == 200
    assert response.json()["idempotent_replay"] is True


@_retired_post_route
def test_conflicting_payload_same_key_returns_409():
    """Reusing a key with a different payload surfaces the idempotency conflict."""
    client, auth, database, role = _client(role_allowed=True)
    from core.datastream_field_mapping import DatastreamMappingConflict  # noqa: PLC0415
    mapping = {
        "mapping_contract_version": "1",
        "source_schema_hash": "a" * 64,
        "plan_version_id": "dsp_01",
        "grain": ["date"],
        "fields": [],
        "ambiguities": [],
    }
    with (
        auth,
        database,
        role,
        patch(
            "core.datastream_field_mapping.save_field_mapping",
            side_effect=DatastreamMappingConflict("idempotency_conflict"),
        ),
        client,
    ):
        response = client.post(
            "/api/datastreams/ds_01/mapping/versions",
            json={"project_id": "proj_a", "mapping": mapping},
            headers={"Idempotency-Key": "conflict-key-01"},
        )
    assert response.status_code == 409
    assert response.json()["code"] == "conflict"


@_retired_post_route
def test_blocking_payload_response_is_not_executable():
    """A saved mapping that still holds blocking bindings returns executable=False,
    never a silent approval."""
    client, auth, database, role = _client(role_allowed=True)
    mapping = {
        "mapping_contract_version": "1",
        "source_schema_hash": "a" * 64,
        "plan_version_id": "dsp_01",
        "grain": ["date"],
        "fields": [],
        "ambiguities": [],
    }
    blocked_result = {
        "id": "dmap_02",
        "version_number": 1,
        "executable": False,
        "blocking_count": 1,
        "idempotent_replay": False,
    }
    with (
        auth,
        database,
        role,
        patch(
            "core.datastream_field_mapping.save_field_mapping", return_value=blocked_result
        ),
        client,
    ):
        response = client.post(
            "/api/datastreams/ds_01/mapping/versions",
            json={"project_id": "proj_a", "mapping": mapping},
            headers={"Idempotency-Key": "blocking-key-01"},
        )
    assert response.status_code == 201
    body = response.json()
    assert body["executable"] is False
    assert body["blocking_count"] == 1


def test_profile_defaults_sensitivity_unknown_and_flags_currency_conflict():
    """The profile seam surfaces conservative sensitivity=unknown and a
    CURRENCY_CONFLICT ambiguity when two currencies appear on measures."""
    client, auth, database, role = _client(role_allowed=True)
    field_records = [
        {"field_id": "cost", "physical_type": "decimal", "kind": "metric", "currency": "USD"},
        {"field_id": "revenue", "physical_type": "decimal", "kind": "metric", "currency": "EUR"},
    ]
    with (
        auth,
        database,
        role,
        patch(
            "core.datastreams.get_datastream",
            return_value={"id": "ds_01", "project_id": "proj_a"},
        ),
        client,
    ):
        response = client.post(
            "/api/datastreams/ds_01/mapping/profile",
            json={"project_id": "proj_a", "field_records": field_records},
        )
    assert response.status_code == 200
    res = response.json()
    codes = [a["code"] for a in res["ambiguities"]]
    assert "CURRENCY_CONFLICT" in codes
    for f in res["fields"]:
        assert f["suggestion"]["sensitivity"] == "unknown"


def test_profile_rejects_oversized_sample_and_field_records():
    """Route-level input caps: >500 sample rows and >200 field records are 400s."""
    client, auth, database, role = _client(role_allowed=True)
    with auth, database, role, client:
        big_sample = client.post(
            "/api/datastreams/ds_01/mapping/profile",
            json={"project_id": "proj_a", "sample_data": [{"a": 1}] * 501},
        )
        big_fields = client.post(
            "/api/datastreams/ds_01/mapping/profile",
            json={"project_id": "proj_a", "field_records": [{"field_id": "x"}] * 201},
        )
    assert big_sample.status_code == 400
    assert big_sample.json()["code"] == "too_many_rows"
    assert big_fields.status_code == 400
    assert big_fields.json()["code"] == "too_many_fields"

