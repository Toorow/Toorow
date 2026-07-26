"""Focused HTTP seam tests for the Datastream mapping read/profile repair."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.requests import Request


def _post_request(body: dict) -> Request:
    payload = json.dumps(body).encode("utf-8")
    receive = AsyncMock(
        side_effect=[
            {"type": "http.request", "body": payload, "more_body": False},
            {"type": "http.request", "body": b"", "more_body": False},
        ]
    )
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/datastreams/ds_1/mapping/profile",
            "path_params": {"id": "ds_1"},
            "query_string": b"",
            "headers": [],
        },
        receive=receive,
    )


def _get_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/datastreams/ds_1/read-model",
            "path_params": {"id": "ds_1"},
            "query_string": b"project_id=project_1",
            "headers": [],
        }
    )

def _db():
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    cursor.fetchall.return_value = []
    connection = MagicMock()
    connection.cursor.return_value = cursor

    @contextmanager
    def get_connection():
        yield connection

    return get_connection


def test_profile_resolves_capability_catalog_with_persisted_report_profile_id():
    from core.admin_api import _profile_datastream_mapping

    profile = MagicMock(return_value={"fields": [], "ambiguities": []})
    field_catalog = [
        {"field_id": "event_day", "physical_type": "date", "kind": "date"},
        {"field_id": "cost", "physical_type": "decimal", "kind": "metric"},
    ]
    datastream = {
        "id": "ds_1",
        "connection_ref_id": "conn_1",
        "report_profile_id": "report_alpha",
    }
    capabilities = {
        "reports": [
            {"id": "report_other", "field_catalog": []},
            {"id": "report_alpha", "field_catalog": field_catalog},
        ]
    }

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "operator"))),
        patch("core.admin_api._require_datastream_role", return_value=None),
        patch("core.db.get_connection", new=_db()),
        patch("core.datastreams.get_datastream", return_value=datastream),
        patch("core.main.get_loaded_modules", return_value={}),
        patch(
            "core.source_capabilities.get_scoped_source_capabilities",
            return_value=capabilities,
        ),
        patch("core.datastream_field_mapping.profile_fields", profile),
    ):
        response = asyncio.run(
            _profile_datastream_mapping(_post_request({"project_id": "project_1"}))
        )

    assert response.status_code == 200
    profile.assert_called_once_with(
        field_records=field_catalog,
        sample_data=None,
        known_target_fields=set(),
    )

def test_read_model_mapping_failure_is_unavailable_not_empty():
    from core.admin_api import _get_datastream_versions

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "viewer"))),
        patch("core.admin_api._require_datastream_role", return_value=None),
        patch("core.admin_api._resolve_datastream_route_scope", return_value="project_1"),
        patch("core.db.get_connection", new=_db()),
        patch("core.datastreams.get_datastream", return_value={"id": "ds_1"}),
        patch("core.datastream_intents.list_intent_versions", return_value=[]),
        patch(
            "core.datastream_field_mapping.list_mapping_versions",
            side_effect=RuntimeError("database unavailable"),
        ),
    ):
        response = asyncio.run(_get_datastream_versions(_get_request()))

    body = json.loads(response.body)
    assert response.status_code == 503
    assert body["code"] == "unavailable"
    assert "mapping_versions" not in body
