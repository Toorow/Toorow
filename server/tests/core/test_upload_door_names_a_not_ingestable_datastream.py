"""The governed upload door names a Datastream that cannot ingest -- AI-321.

2026-08-29, G9: `DatastreamNotIngestable` raised by the dispatch-bundle
resolver inside the scan escaped `_process_one_attachment`'s typed handling and
fell into the door's catch-all: 503 "Import unavailable", the exception's type
in the journal and nothing else. It is a rule that can say what to configure,
so it is a 422 with its own code and its sentence.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

from starlette.requests import Request


def _request(body: dict) -> Request:
    payload = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/datastreams/ds_1/imports",
            "path_params": {"id": "ds_1"},
            "headers": [(b"content-type", b"application/json"), (b"idempotency-key", b"k1")],
            "query_string": b"",
        },
        receive,
    )


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self):
        pass

    def rollback(self):
        pass


def test_a_not_ingestable_datastream_is_a_named_422_not_a_503():
    from core import file_import_api as api
    from core.inbound_ingest import DatastreamNotIngestable

    async def _auth(request):
        return True, "owner@example.com"

    def _refuse(*args, **kwargs):
        raise DatastreamNotIngestable(
            "the pinned plan/mapping bundle has incomplete fingerprint evidence"
        )

    with (
        patch.object(api, "_check_auth", _auth),
        patch.object(api, "_require_datastream_role", lambda *a, **k: None),
        patch("core.db.get_connection", return_value=_Conn()),
        patch("core.inbound_processing.process_authorized_upload", side_effect=_refuse),
    ):
        response = asyncio.run(
            api._confirm_csv_excel_import(
                _request({"project_id": "proj_EXAMPLE", "file_base64": "YSxiCjEsMg==",
                          "filename": "sample.csv"})
            )
        )

    assert response.status_code == 422
    body = json.loads(response.body)
    assert body["code"] == "datastream_not_ingestable"
    assert "fingerprint evidence" in body["message"]


def test_the_upload_verdict_carries_its_sentence_and_says_when_it_is_a_replay():
    """AI-321 (2026-08-29): the persisted `error_detail` never reached the response,
    and a replay's terminal envelope (`attachments: []`) was read as a second
    import with different counts."""
    from core import file_import_api as api

    async def _auth(request):
        return True, "owner@example.com"

    outcome = {
        "receipt_id": "inbrx_1",
        "status": "failed",
        "attachments": [
            {
                "raw_import_id": "inbraw_1",
                "error_code": "file_source_drift",
                "error_detail": "required source columns disappeared: datastream_id",
            }
        ],
        "dispatch_result": None,
        "replayed": False,
    }
    with (
        patch.object(api, "_check_auth", _auth),
        patch.object(api, "_require_datastream_role", lambda *a, **k: None),
        patch("core.db.get_connection", return_value=_Conn()),
        patch("core.inbound_processing.process_authorized_upload", return_value=outcome),
    ):
        response = asyncio.run(
            api._confirm_csv_excel_import(
                _request({"project_id": "proj_EXAMPLE", "file_base64": "YSxiCjEsMg==",
                          "filename": "sample.csv"})
            )
        )
    body = json.loads(response.body)
    assert body["error_code"] == "file_source_drift"
    assert "datastream_id" in body["error_detail"]
    assert body["replayed"] is False

    replay = {**outcome, "attachments": [], "replayed": True}
    with (
        patch.object(api, "_check_auth", _auth),
        patch.object(api, "_require_datastream_role", lambda *a, **k: None),
        patch("core.db.get_connection", return_value=_Conn()),
        patch("core.inbound_processing.process_authorized_upload", return_value=replay),
    ):
        response = asyncio.run(
            api._confirm_csv_excel_import(
                _request({"project_id": "proj_EXAMPLE", "file_base64": "YSxiCjEsMg==",
                          "filename": "sample.csv"})
            )
        )
    body = json.loads(response.body)
    assert body["replayed"] is True
    assert body["raw_import_id"] is None
