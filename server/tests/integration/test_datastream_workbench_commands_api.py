"""Project-scoped Workbench command seam proofs for Story 47.5."""

from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.testclient import TestClient

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")


@contextmanager
def _connection():
    connection = MagicMock()
    connection.transaction.return_value.__enter__.return_value = connection
    yield connection


def _client() -> TestClient:
    from core.main import build_asgi_app

    return TestClient(build_asgi_app(), raise_server_exceptions=False)


def _authorized():
    return (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person-1"))),
        patch("core.admin_api._require_datastream_role", return_value=None),
        patch("core.db.get_connection", side_effect=_connection),
    )


def test_mapping_prepare_forwards_exact_scope_payload_and_idempotency() -> None:
    prepared = {
        "preparation_id": "dscp-1",
        "confirmation_secret": "secret",
        "review": {"expected_plan_version_id": "dsp-1"},
    }
    with (
        _authorized()[0],
        _authorized()[1] as role,
        _authorized()[2],
        patch("core.datastream_change.prepare_change", return_value=prepared) as prepare,
    ):
        response = _client().post(
            "/api/projects/proj-1/datastreams/ds-1/workbench/mapping/changes",
            headers={"Idempotency-Key": "idem-1"},
            json={"proposed_payload": {"fields": [{"field_id": "date"}]}},
        )

    assert response.status_code == 201
    assert prepare.call_args.kwargs == {
        "project_id": "proj-1",
        "datastream_id": "ds-1",
        "kind": "mapping",
        "proposed_payload": {"fields": [{"field_id": "date"}]},
        "actor": "person-1",
        "idempotency_key": "idem-1",
        "raw_import_id": None,
    }
    assert role.call_args.args[2] == "member"


def test_mapping_prepare_forwards_the_retained_input_that_opened_the_repair() -> None:
    with (
        _authorized()[0],
        _authorized()[1],
        _authorized()[2],
        patch("core.datastream_change.prepare_change", return_value={}) as prepare,
    ):
        response = _client().post(
            "/api/projects/proj-1/datastreams/ds-1/workbench/mapping/changes",
            headers={"Idempotency-Key": "idem-file-1"},
            json={
                "proposed_payload": {"fields": [{"field_id": "date"}]},
                "raw_import_id": "inbraw_01EXAMPLE0000000000000000",
            },
        )

    assert response.status_code == 201
    assert prepare.call_args.kwargs["raw_import_id"] == "inbraw_01EXAMPLE0000000000000000"


def test_rollback_confirm_forwards_one_time_exact_preparation() -> None:
    result = {
        "preparation_id": "dsrp-1",
        "operation_id": "op-1",
        "rolled_back_to": "dse-lkg",
        "replayed": False,
    }
    with (
        _authorized()[0],
        _authorized()[1],
        _authorized()[2],
        patch("core.datastream_rollback.confirm_rollback", return_value=result) as confirm,
    ):
        response = _client().post(
            "/api/projects/proj-1/datastreams/ds-1/workbench/outputs/"
            "rollback-preparations/dsrp-1/confirm",
            json={"confirmation_secret": "secret"},
        )

    assert response.status_code == 200
    assert response.json()["operation_id"] == "op-1"
    assert confirm.call_args.kwargs == {
        "project_id": "proj-1",
        "datastream_id": "ds-1",
        "preparation_id": "dsrp-1",
        "confirmation_secret": "secret",
        "actor": "person-1",
    }


def test_denied_change_does_not_prepare_any_mutation() -> None:
    from starlette.responses import JSONResponse

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person-1"))),
        patch(
            "core.admin_api._require_datastream_role",
            return_value=JSONResponse({"code": "not_found"}, 404),
        ),
        patch("core.db.get_connection", side_effect=_connection),
        patch("core.datastream_change.prepare_change") as prepare,
    ):
        response = _client().post(
            "/api/projects/foreign/datastreams/hidden/workbench/processing/changes",
            headers={"Idempotency-Key": "idem-1"},
            json={"proposed_payload": {}},
        )

    assert response.status_code == 404
    prepare.assert_not_called()


# ---------------------------------------------------------------------------
# Story 38.17 AC5 -- an unauthorized or noninteractive host cannot commit, and
# is handed the console instead of a dead end.
# ---------------------------------------------------------------------------

_CONFIRM_ROUTE = "/api/projects/proj-1/datastreams/ds-1/workbench/changes/dscp-1/confirm"


def test_a_noninteractive_host_cannot_commit_and_is_handed_the_console() -> None:
    """AC5. Before this, `_confirm_change` checked `_authorize(request, "member")`
    and nothing else: `grep -cE "noninteractive|console|host_context"
    server/core/datastream_change.py` returned 1, and it was `host_context={}`.

    The proof is not that a key is present -- it is that the WRITER IS NEVER
    REACHED. A refusal that still calls `confirm_change` has refused nothing.
    """
    with (
        _authorized()[0],
        _authorized()[1],
        _authorized()[2],
        patch("core.datastream_change.confirm_change") as confirm,
    ):
        response = _client().post(
            _CONFIRM_ROUTE,
            headers={"X-Toorow-Host": "mcp"},
            json={"confirmation_secret": "secret"},
        )

    assert response.status_code == 403
    confirm.assert_not_called()
    body = response.json()
    assert body["code"] == "host_cannot_confirm"
    assert body["confirmation_available"] is False
    # A destination, not a sentence about one: semantic route data the console
    # resolves through its own navigation registry.
    console = body["console"]
    assert console["requires_authenticated_session"] is True
    assert console["owner_reference"]["object_id"] == "ds-1"
    assert console["owner_reference"]["object_type"] == "datastream"
    assert console["owner_reference"]["tab"] == "mapping"
    # The secret is NOT echoed back on the way out.
    assert "secret" not in response.text


def test_an_undeclared_host_is_the_console_session_and_still_confirms() -> None:
    """The control. Without it, a gate that refused EVERY caller would pass the
    test above while removing the only surface on which a change can be made.
    """
    result = {
        "preparation_id": "dscp-1",
        "operation_id": "op-1",
        "candidate_execution_id": "dse-1",
        "replayed": False,
        "outcome": "succeeded",
    }
    with (
        _authorized()[0],
        _authorized()[1],
        _authorized()[2],
        patch("core.datastream_change.confirm_change", return_value=result) as confirm,
    ):
        response = _client().post(_CONFIRM_ROUTE, json={"confirmation_secret": "secret"})

    assert response.status_code == 200
    assert response.json()["operation_id"] == "op-1"
    confirm.assert_called_once()


def test_an_unauthorized_caller_never_reaches_the_confirmation_writer() -> None:
    """AC5, the `Unauthorized` half. 404 and not 403: outside the member grant,
    a refusal must not disclose that this Datastream exists.
    """
    from starlette.responses import JSONResponse

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person-1"))),
        patch(
            "core.admin_api._require_datastream_role",
            return_value=JSONResponse({"code": "not_found"}, 404),
        ),
        patch("core.db.get_connection", side_effect=_connection),
        patch("core.datastream_change.confirm_change") as confirm,
    ):
        response = _client().post(_CONFIRM_ROUTE, json={"confirmation_secret": "secret"})

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    confirm.assert_not_called()
