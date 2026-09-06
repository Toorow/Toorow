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
    """The route resolves a report's declared fields from the connector's own.

    This test previously built a `capabilities` fixture whose reports carried a
    `field_catalog` key, and asserted the route read it. That key exists in no
    connector manifest and in no `$def` of `source-capabilities.schema.json`, so
    the test was green while the route returned an empty field universe for all
    129 report profiles of all 38 connectors -- which is why 42 Datastreams
    reached a plan and none reached a mapping. The fixture below is the shape the
    contract actually defines: connector-level `fields`, and a report naming them
    through `metrics` and `dimensions`.
    """
    from core.datastream_mapping_api import _profile_datastream_mapping

    profile = MagicMock(return_value={"fields": [], "ambiguities": []})
    event_day = {"field_id": "event_day", "physical_type": "date", "kind": "date"}
    cost = {"field_id": "cost", "physical_type": "decimal", "kind": "metric"}
    datastream = {
        "id": "ds_1",
        "connection_ref_id": "conn_1",
        "report_profile_id": "report_alpha",
    }
    capabilities = {
        "fields": [cost, event_day, {"field_id": "unused", "kind": "metric"}],
        "reports": [
            {"id": "report_other", "dimensions": [], "metrics": []},
            {"id": "report_alpha", "dimensions": ["event_day"], "metrics": ["cost"]},
        ],
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
        # Dimensions before metrics, and the field the report does not name is
        # absent: a report profiles what it exposes, not the whole connector.
        field_records=[event_day, cost],
        sample_data=None,
        known_target_fields=set(),
    )


def test_a_report_exposing_no_resolvable_field_is_refused_not_answered_empty():
    """An empty profiling response reads as "this source has nothing to map".

    The route used to return exactly that, with status 200, whenever capability
    resolution produced nothing -- including when it raised, because the call was
    wrapped in `except Exception: pass`. Silence with a success code is the
    failure mode that hid this bug for the whole life of the mapping surface.
    """
    from core.datastream_mapping_api import _profile_datastream_mapping

    datastream = {
        "id": "ds_1",
        "connection_ref_id": "conn_1",
        "report_profile_id": "report_ghost",
    }
    capabilities = {"fields": [], "reports": []}

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
    ):
        response = asyncio.run(
            _profile_datastream_mapping(_post_request({"project_id": "project_1"}))
        )

    body = json.loads(response.body)
    assert response.status_code == 422
    assert body["code"] == "no_profilable_fields"
    assert body["report_profile_id"] == "report_ghost"


def test_unresolvable_capabilities_are_unavailable_not_an_empty_profile():
    from core.datastream_mapping_api import _profile_datastream_mapping

    datastream = {
        "id": "ds_1",
        "connection_ref_id": "conn_1",
        "report_profile_id": "report_alpha",
    }

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "operator"))),
        patch("core.admin_api._require_datastream_role", return_value=None),
        patch("core.db.get_connection", new=_db()),
        patch("core.datastreams.get_datastream", return_value=datastream),
        patch("core.main.get_loaded_modules", return_value={}),
        patch(
            "core.source_capabilities.get_scoped_source_capabilities",
            side_effect=RuntimeError("capabilities unavailable"),
        ),
    ):
        response = asyncio.run(
            _profile_datastream_mapping(_post_request({"project_id": "project_1"}))
        )

    body = json.loads(response.body)
    assert response.status_code == 503
    assert body["code"] == "capabilities_unavailable"

def test_read_model_mapping_failure_is_unavailable_not_empty():
    from core.datastreams_api import _get_datastream_versions

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
