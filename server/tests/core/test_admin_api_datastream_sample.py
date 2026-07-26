"""HTTP contract tests for the governed Datastream sample handler."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.requests import Request
from starlette.responses import JSONResponse


def _request(query: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/datastreams/ds_1/sample",
            "path_params": {"id": "ds_1"},
            "query_string": query.encode("ascii"),
            "headers": [],
        }
    )


def _payload(response: JSONResponse) -> dict:
    return json.loads(response.body)


def _db(*rows: tuple | None):
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    cursor.fetchone.side_effect = rows
    connection = MagicMock()
    connection.cursor.return_value = cursor

    @contextmanager
    def get_connection():
        yield connection

    return get_connection


def test_sample_requires_explicit_project_before_database_or_warehouse():
    from core.admin_api import _datastream_sample

    read_sample = MagicMock()
    get_connection = MagicMock()
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "viewer"))),
        patch("core.db.get_connection", get_connection),
        patch("core.cache_warehouse.read_datastream_sample", read_sample),
    ):
        response = asyncio.run(
            _datastream_sample(
                _request("stage=published&date_from=2026-07-01&date_to=2026-07-02")
            )
        )

    assert response.status_code == 400
    assert _payload(response)["code"] == "missing_param"
    get_connection.assert_not_called()
    read_sample.assert_not_called()


def test_sample_cross_project_is_non_disclosing_and_skips_warehouse():
    from core.admin_api import _datastream_sample

    read_sample = MagicMock()
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "viewer"))),
        patch("core.db.get_connection", new=_db(None)),
        patch("core.admin_api._require_datastream_role", return_value=None),
        patch("core.cache_warehouse.read_datastream_sample", read_sample),
    ):
        response = asyncio.run(
            _datastream_sample(
                _request(
                    "project_id=project_foreign&stage=published&date_from=2026-07-01&date_to=2026-07-02"
                )
            )
        )

    assert response.status_code == 404
    assert _payload(response) == {
        "code": "not_found",
        "message": "Flux de donnees introuvable",
    }
    read_sample.assert_not_called()


def test_sample_denied_role_is_non_disclosing_and_skips_warehouse():
    from core.admin_api import _datastream_sample

    read_sample = MagicMock()
    row = ("project_owner", "meta", "connector_pull", "Real stream", True)
    denial = JSONResponse(
        {"code": "not_found", "message": "Flux de donnees introuvable"}, status_code=404
    )
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "viewer"))),
        patch("core.db.get_connection", new=_db(row)),
        patch("core.admin_api._require_datastream_role", return_value=denial),
        patch("core.cache_warehouse.read_datastream_sample", read_sample),
    ):
        response = asyncio.run(
            _datastream_sample(
                _request(
                    "project_id=project_owner&stage=published&date_from=2026-07-01&date_to=2026-07-02"
                )
            )
        )

    assert response.status_code == 404
    assert _payload(response)["code"] == "not_found"
    read_sample.assert_not_called()


def test_sample_success_serializes_scoped_first_n_and_sample_watermark():
    from core.admin_api import _datastream_sample

    sample = {
        "served_stage": "processed",
        "stage_note": "published uses the consolidated processed mart",
        "materialization_available": True,
        "sample_watermark": "2026-07-02",
        "masked_fields": ["user_email"],
        "masked_value_count": 2,
        "days": [
            {
                "date": "2026-07-02",
                "sampled_row_count": 2,
                "rejection_count": 0,
                "field_count": 3,
                "rows": [
                    {"metric": "m1", "user_email": "[MASKED]"},
                    {"metric": "m2", "user_email": "[MASKED]"},
                ],
            }
        ],
    }
    row = ("project_owner", "meta", "connector_pull", "Real stream", True)
    read_sample = MagicMock(return_value=sample)
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "viewer"))),
        patch("core.db.get_connection", new=_db(row, (1,))),
        patch("core.admin_api._require_datastream_role", return_value=None),
        patch("core.cache_warehouse.read_datastream_sample", read_sample),
    ):
        response = asyncio.run(
            _datastream_sample(
                _request(
                    "project_id=project_owner&stage=published&date_from=2026-07-01&date_to=2026-07-02&limit=5"
                )
            )
        )

    assert response.status_code == 200
    body = _payload(response)
    assert body["project_id"] == "project_owner"
    assert body["datastream"]["name"] == "Real stream"
    assert body["days"][0]["sampled_row_count"] == 2
    assert "row_count" not in body["days"][0]
    assert body["sample_watermark"] == "2026-07-02"
    assert body["masked_value_count"] == 2
    assert body["version_binding_available"] is False
    assert "published_execution_id" not in body
    assert "mapping_version_id" not in body
    read_sample.assert_called_once_with(
        project_id="project_owner",
        connector="meta",
        stage="published",
        date_from="2026-07-01",
        date_to="2026-07-02",
        limit=5,
    )

def test_sample_shared_route_reads_owner_project_but_echoes_route_project():
    from core.admin_api import _datastream_sample

    sample = {
        "served_stage": "processed",
        "stage_note": None,
        "materialization_available": True,
        "sample_watermark": None,
        "masked_fields": [],
        "masked_value_count": 0,
        "days": [],
    }
    row = ("project_owner", "meta", "connector_pull", "Shared stream", True)
    read_sample = MagicMock(return_value=sample)
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "viewer"))),
        patch("core.db.get_connection", new=_db(row, (1,))),
        patch("core.admin_api._require_datastream_role", return_value=None),
        patch("core.cache_warehouse.read_datastream_sample", read_sample),
    ):
        response = asyncio.run(
            _datastream_sample(
                _request(
                    "project_id=project_shared&stage=processed&date_from=2026-07-01&date_to=2026-07-02"
                )
            )
        )

    assert response.status_code == 200
    assert _payload(response)["project_id"] == "project_shared"
    read_sample.assert_called_once_with(
        project_id="project_owner",
        connector="meta",
        stage="processed",
        date_from="2026-07-01",
        date_to="2026-07-02",
        limit=5,
    )


def test_sample_fails_closed_when_connector_materialization_is_ambiguous():
    from core.admin_api import _datastream_sample

    row = ("project_owner", "meta", "connector_pull", "Ambiguous stream", True)
    read_sample = MagicMock()
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "viewer"))),
        patch("core.db.get_connection", new=_db(row, (2,))),
        patch("core.admin_api._require_datastream_role", return_value=None),
        patch("core.cache_warehouse.read_datastream_sample", read_sample),
    ):
        response = asyncio.run(
            _datastream_sample(
                _request(
                    "project_id=project_owner&stage=processed&date_from=2026-07-01&date_to=2026-07-02"
                )
            )
        )

    assert response.status_code == 409
    assert _payload(response)["code"] == "ambiguous_materialization"
    read_sample.assert_not_called()
