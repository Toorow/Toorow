"""Focused P0 security proof for Epic 43.21 governance evidence routes."""

from __future__ import annotations

import json
from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.datastructures import QueryParams


def _request(*, query: dict | None = None, path: dict | None = None, body: dict | None = None):
    request = MagicMock()
    request.query_params = QueryParams(query or {})
    request.path_params = path or {}
    request.body = AsyncMock(return_value=json.dumps(body or {}).encode())
    return request


def _cursor_cm(cursor: MagicMock) -> MagicMock:
    cm = MagicMock()
    cm.__enter__.return_value = cursor
    cm.__exit__.return_value = False
    return cm


def test_audit_query_scopes_before_order_limit_and_omits_raw_metadata():
    from core.audit import query_audit_log

    cursor = MagicMock()
    cursor.description = [("id",), ("identity",), ("action",), ("provider_account",),
                          ("connection_ref",), ("outcome",), ("trace_id",),
                          ("resource",), ("created_at",)]
    cursor.fetchall.return_value = []
    conn = MagicMock()
    conn.cursor.return_value = _cursor_cm(cursor)
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False

    with patch("core.audit.psycopg.connect", return_value=conn):
        assert query_audit_log(project_id="project_a", limit=50) == []

    sql, params = cursor.execute.call_args.args
    scope_at = sql.index("WHERE metadata->>'project_id' = %s")
    assert scope_at < sql.index("ORDER BY created_at DESC") < sql.index("LIMIT %s")
    assert params == ["project_a", 50]
    assert "metadata," not in sql
    assert "AS outcome" in sql and "AS trace_id" in sql and "AS resource" in sql


@pytest.mark.anyio
async def test_audit_endpoint_denies_before_query():
    from core.routing import audit_endpoint

    query = MagicMock()
    with (
        patch("core.api_auth.authenticate_api_request", new=AsyncMock(return_value=(True, "p1"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=False),
        patch("core.db.get_connection", return_value=nullcontext(MagicMock())),
        patch("core.audit.query_audit_log", query),
    ):
        response = await audit_endpoint(_request(query={"project_id": "project_b"}))

    assert response.status_code == 404
    query.assert_not_called()


@pytest.mark.anyio
async def test_dq_acknowledge_requires_edit_before_update():
    from core.dq_api import _dq_acknowledge

    conn = MagicMock()
    guard = MagicMock(return_value=False)
    with (
        patch("core.dq_api._check_auth", new=AsyncMock(return_value=(True, "p1"))),
        patch("core.dq_api._enforce_project_scope", guard),
        patch("core.db.get_connection", return_value=nullcontext(conn)),
    ):
        response = await _dq_acknowledge(
            _request(query={"project_id": "project_b"}, path={"firing_id": "fire_1"})
        )

    assert response.status_code == 404
    assert guard.call_args.kwargs["minimum_capability"] == "edit"
    conn.cursor.assert_not_called()


@pytest.mark.anyio
async def test_dq_summary_zero_evaluations_is_unknown_not_healthy():
    from core.dq_api import _dq_summary

    total = MagicMock()
    total.fetchone.return_value = (3,)
    evaluated = MagicMock()
    evaluated.fetchone.return_value = (0,)
    firings = MagicMock()
    firings.description = [("type",), ("fired_at",), ("acknowledged_at",)]
    firings.fetchall.return_value = []
    conn = MagicMock()
    conn.cursor.side_effect = [_cursor_cm(total), _cursor_cm(evaluated), _cursor_cm(firings)]

    with (
        patch("core.dq_api._check_auth", new=AsyncMock(return_value=(True, "p1"))),
        patch("core.dq_api._enforce_project_scope", return_value=True),
        patch("core.db.get_connection", return_value=nullcontext(conn)),
    ):
        response = await _dq_summary(_request(query={"project_id": "project_a"}))

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["total_streams"] == 3
    assert payload["evaluated_streams"] == 0
    assert all(item["healthy_pct"] is None for item in payload["monitors"])


# ---------------------------------------------------------------------------
# PUT /api/datamodel/mappings -- REWRITTEN 2026-08-25.
#
# WHAT THESE THREE TESTS USED TO PIN, AND WHY THAT IS NOW WRONG. They proved the
# owner check of a write door: an owner mismatch denies before the capability
# guard, an unreadable access store fails closed, and an allowed edit reaches the
# store at the `edit` rank. Story 49.3 AC1 RETIRED the door (`b1dcc943`): it wrote
# one row of `app.datastream_mappings` with no version and no review, so a binding
# could change under a published Datastream with nothing recording it. It answers
# 409 `legacy_store_is_read_only` now, and `governance.md` (amendment of
# 2026-08-25) ratifies both the refusal and the reason it is not a 404 -- an
# unmounted write tells a caller its object does not exist and sends it looking.
#
# Three tests asserting 404 on a door that answers 409 are not a stricter proof of
# anything: they pin a behaviour the product decided against. What replaces them
# proves the property that SURVIVES the retirement and is stronger than the one
# they held -- the door reaches NEITHER the guard NOR the store, for any caller,
# so there is no owner mismatch left to exploit.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_retired_mapping_door_refuses_without_guard_or_mutation():
    """409, and the store is never reached -- whoever asks, and whatever they own."""
    from core.datamodel_api import _upsert_mapping

    guard = MagicMock()
    mutation = MagicMock()
    with (
        patch("core.datamodel_api._check_auth", new=AsyncMock(return_value=(True, "p1"))),
        patch("core.datamodel_api._project_access_allowed", guard),
        patch("core.db.get_connection", return_value=nullcontext(MagicMock())),
        patch("core.datamodel.upsert_mapping", mutation),
    ):
        response = await _upsert_mapping(
            _request(body={"project_id": "project_a", "datastream_id": "ds_1",
                           "source_field": "clicks", "target_field": "clicks"})
        )

    assert response.status_code == 409
    payload = json.loads(response.body)
    assert payload["code"] == "legacy_store_is_read_only"
    guard.assert_not_called()
    mutation.assert_not_called()


@pytest.mark.anyio
async def test_the_refusal_names_a_gesture_and_never_a_table():
    """A refusal that hands back a table name teaches the caller to work around it.

    `CLAUDE.md`: an error message names the gesture that repairs, not the cause.
    The amendment says the same for these eight doors, so the message must send a
    person to the Datastream's Mapping tab, not to `app.datastream_mappings`.
    """
    from core.datamodel_api import _upsert_mapping

    with (
        patch("core.datamodel_api._check_auth", new=AsyncMock(return_value=(True, "p1"))),
        patch("core.db.get_connection", return_value=nullcontext(MagicMock())),
    ):
        response = await _upsert_mapping(_request(body={}))

    message = json.loads(response.body)["message"]
    assert "Mapping" in message
    # "a source column" is the user's word for what they bound, and it stays.
    # What must never appear is the STORE: a table name is the cause, and a caller
    # handed one goes looking for another way into it.
    for leaked in ("app.datastream_mappings", "app.target_fields", "app.metric_definitions"):
        assert leaked not in message, (
            f"the refusal names {leaked!r}: it describes the cause instead of the "
            "gesture that works"
        )
