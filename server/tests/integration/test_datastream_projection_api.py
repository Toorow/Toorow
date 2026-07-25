"""Real-ASGI seams for the safe KPI projection compile route (Story 12.4, AI-56).

Through build_asgi_app(): Member compile success, non-additive rejection 422,
cardinality-over-limit 422 with the named expensive field, Viewer denial on the
Member route, cross-project non-disclosing 404, and production zero-membership
default-deny. The route is pure metadata over the immutable mapping version +
governed preferences -- it NEVER publishes and performs no BigQuery write.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.testclient import TestClient

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core.main import build_asgi_app  # noqa: E402


def _db_context(pref_row=None):
    """A get_connection() context whose cursor().fetchone() returns pref_row.

    The projection route fetches the project preference row via a cursor; the
    mapping-version reads are patched at the module boundary, so the cursor only
    needs to answer the preference SELECT.
    """
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = pref_row
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cursor)
    context = MagicMock()
    context.__enter__ = MagicMock(return_value=conn)
    context.__exit__ = MagicMock(return_value=False)
    return context


def _client(role_allowed=True, pref_row=None):
    app = build_asgi_app()
    return (
        TestClient(app, raise_server_exceptions=True),
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "user-1"))),
        patch("core.db.get_connection", return_value=_db_context(pref_row)),
        patch("core.project_access.identity_has_project_role", return_value=role_allowed),
    )


def _field(field_id, *, physical_type, semantic_role, aggregation, non_additive,
           cardinality_signal="low", canonical_target=None):
    return {
        "field_id": field_id,
        "physical_type": physical_type,
        "profile": {
            "nullable": False, "unique": False,
            "cardinality_signal": cardinality_signal,
            "sample_values": [], "confidence": 0.9,
        },
        "suggestion": {
            "semantic_role": semantic_role, "aggregation": aggregation,
            "non_additive": non_additive, "currency": "unknown",
            "sensitivity": "none", "status": "suggested", "evidence": [],
        },
        "binding": {
            "canonical_target": canonical_target, "mdm_target": None,
            "status": "confirmed", "blocking_reason": None,
            "confirmed_by": "u", "confirmed_reason": "r",
        },
    }


def _mapping_version(fields, grain, executable=True):
    return {
        "id": "dmap_01",
        "plan_version_id": "dsp_01",
        "source_schema_hash": "a" * 64,
        "capability_fingerprint": "b" * 64,
        "executable": executable,
        "mapping_payload": {
            "mapping_contract_version": "1",
            "source_schema_hash": "a" * 64,
            "plan_version_id": "dsp_01",
            "capability_fingerprint": "b" * 64,
            "grain": grain,
            "fields": fields,
            "ambiguities": [],
        },
    }


def _additive_mapping():
    return _mapping_version(
        [
            _field("date", physical_type="date", semantic_role="primary_date",
                   aggregation="none", non_additive=False),
            _field("country", physical_type="string", semantic_role="dimension",
                   aggregation="none", non_additive=False, canonical_target="country"),
            _field("sessions", physical_type="integer", semantic_role="measure",
                   aggregation="sum", non_additive=False, canonical_target="sessions"),
        ],
        ["country", "date"],
    )


def test_compile_projection_member_success():
    client, auth, database, role = _client(role_allowed=True)
    with (
        auth, database, role as role_check,
        patch("core.datastream_field_mapping.list_mapping_versions",
              return_value=[_additive_mapping()]),
        client,
    ):
        response = client.post(
            "/api/datastreams/ds_01/projection/compile",
            json={
                "project_id": "proj_a",
                "dimension_projection": "country",
                # 'country' projected as a parallel series: the connector's current
                # canonical ('brand') sorts strictly before it, so MIN never
                # re-pins the projected series (governed_dim_shadows_canonical guard).
                "connector_canonical_breakdown": "brand",
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["executable"] is True
    assert body["additive_measures"][0]["field_id"] == "sessions"
    assert body["governed_dimension_projection"]["breakdown_dimension"] == "country"
    role_check.assert_called()


def test_compile_projection_non_additive_rejected_422():
    client, auth, database, role = _client(role_allowed=True)
    mv = _mapping_version(
        [
            _field("date", physical_type="date", semantic_role="primary_date",
                   aggregation="none", non_additive=False),
            _field("average_position", physical_type="float", semantic_role="measure",
                   aggregation="avg", non_additive=True, canonical_target="average_position"),
        ],
        ["date"],
    )
    with (
        auth, database, role,
        patch("core.datastream_field_mapping.list_mapping_versions", return_value=[mv]),
        client,
    ):
        response = client.post(
            "/api/datastreams/ds_01/projection/compile",
            json={"project_id": "proj_a"},
        )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "projection_rejected"
    codes = {i["code"] for i in body["issues"]}
    assert "non_additive_measure_projected" in codes


def test_compile_projection_cardinality_over_limit_422_names_field():
    # Preference row sets a tiny cardinality limit -> over-limit, field named.
    client, auth, database, role = _client(
        role_allowed=True, pref_row=(10, 10**18)
    )
    mv = _mapping_version(
        [
            _field("date", physical_type="date", semantic_role="primary_date",
                   aggregation="none", non_additive=False),
            _field("user_id", physical_type="string", semantic_role="dimension",
                   aggregation="none", non_additive=False, cardinality_signal="unique"),
        ],
        ["date", "user_id"],
    )
    with (
        auth, database, role,
        patch("core.datastream_field_mapping.list_mapping_versions", return_value=[mv]),
        client,
    ):
        response = client.post(
            "/api/datastreams/ds_01/projection/compile",
            json={"project_id": "proj_a"},
        )
    assert response.status_code == 422
    body = response.json()
    card_issue = next(
        i for i in body["issues"] if i["code"] == "cardinality_over_limit"
    )
    assert "user_id" in card_issue["field_ids"]
    assert body["plan"]["estimate"]["threshold_source"] == "project_preference"


def test_compile_projection_viewer_denied_on_member_route():
    client, auth, database, role = _client(role_allowed=False)
    with auth, database, role, client:
        response = client.post(
            "/api/datastreams/ds_01/projection/compile",
            json={"project_id": "proj_a"},
        )
    # Non-disclosing 404 (fail-closed), not a 200/403.
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_compile_projection_missing_project_id_400():
    client, auth, database, role = _client(role_allowed=True)
    with auth, database, role, client:
        response = client.post(
            "/api/datastreams/ds_01/projection/compile", json={}
        )
    assert response.status_code == 400
    assert response.json()["code"] == "missing_field"


def test_compile_projection_no_mapping_version_404():
    client, auth, database, role = _client(role_allowed=True)
    with (
        auth, database, role,
        patch("core.datastream_field_mapping.list_mapping_versions", return_value=[]),
        client,
    ):
        response = client.post(
            "/api/datastreams/ds_01/projection/compile",
            json={"project_id": "proj_a"},
        )
    assert response.status_code == 404
