"""The dataset export: a chosen period, a chosen column set, and the same masking.

Story 43.16 left the governed ASYNCHRONOUS export unbuilt on purpose -- it needs
"a durable, authorized job/worker contract bound to project, Datastream, stage,
interval, filters and publication version", the story lists creating that under
**Ask First**, and no such substrate exists. This is the bounded, request-scoped
door instead, and these tests pin the three properties that make it safe to
offer: it refuses rather than truncates, it never drops the masking, and it
never silently ignores a column somebody asked for.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.datastructures import QueryParams


def _request(*, query: dict | None = None, path: dict | None = None):
    request = MagicMock()
    request.query_params = QueryParams(query or {})
    request.path_params = path or {}
    request.body = AsyncMock(return_value=json.dumps({}).encode())
    return request


def _conn():
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    # the datastream row, then the same-connector count used to fail closed
    cursor.fetchone.side_effect = [("p1", "search_console"), (1,)]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    return conn


def test_the_route_is_actually_mounted() -> None:
    """The lesson of 2026-08-03: a handler that exists is not a door that opens.

    A component was wired to `GET /api/datastreams/{id}/sample`, whose handler
    existed and whose Route did not. Every demonstration ran against a mocked
    fetch. This asserts the registration, not the function."""
    from core.admin_api import router

    paths = {getattr(route, "path", "") for route in router.routes}
    assert "/api/projects/{project_id}/datastreams/{ds_id}/export" in paths
    assert "/api/projects/{project_id}/datastreams/{ds_id}/export/columns" in paths


def test_exports_only_the_columns_that_were_asked_for_in_that_order() -> None:
    from core.datastream_sample_api import _export_datastream_dataset  # noqa: PLC0415

    export = {
        "columns": ["date", "clicks"],
        "rows": [{"date": "2026-08-02", "clicks": 128}, {"date": "2026-08-01", "clicks": 74}],
        "masked_fields": [],
        "truncated": False,
        "row_limit": 50_000,
    }
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person"))),
        patch("core.admin_api._require_datastream_role", return_value=None),
        patch("core.db.get_connection") as get_conn,
        patch("core.cache_warehouse.read_datastream_export", return_value=export) as reader,
    ):
        get_conn.return_value.__enter__.return_value = _conn()
        res = asyncio.run(_export_datastream_dataset(_request(
            path={"project_id": "p1", "ds_id": "ds1"},
            query={"date_from": "2026-08-01", "date_to": "2026-08-02", "columns": "date,clicks"},
        )))

    assert res.status_code == 200
    assert reader.call_args.kwargs["columns"] == ["date", "clicks"]
    assert reader.call_args.kwargs["date_from"] == "2026-08-01"
    body = res.body.decode("utf-8")
    assert "date,clicks" in body
    assert "2026-08-02,128" in body


def test_refuses_a_range_it_cannot_serve_rather_than_truncating() -> None:
    """A truncated export is a file a person will treat as complete.

    Refusing is the only outcome that cannot be mistaken for a full dataset."""
    from core.cache_warehouse import SampleReadError
    from core.datastream_sample_api import _export_datastream_dataset  # noqa: PLC0415

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person"))),
        patch("core.admin_api._require_datastream_role", return_value=None),
        patch("core.db.get_connection") as get_conn,
        patch(
            "core.cache_warehouse.read_datastream_export",
            side_effect=SampleReadError(
                "export_too_large", "this range holds more than 50,000 rows"
            ),
        ),
    ):
        get_conn.return_value.__enter__.return_value = _conn()
        res = asyncio.run(_export_datastream_dataset(_request(
            path={"project_id": "p1", "ds_id": "ds1"},
            query={"date_from": "2020-01-01", "date_to": "2026-08-02"},
        )))

    assert res.status_code == 400
    assert b"more than 50,000 rows" in res.body


def test_requires_a_period_because_an_unbounded_export_is_a_different_promise() -> None:
    from core.datastream_sample_api import _export_datastream_dataset  # noqa: PLC0415

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person"))),
        patch("core.admin_api._require_datastream_role", return_value=None),
    ):
        res = asyncio.run(_export_datastream_dataset(
            _request(path={"project_id": "p1", "ds_id": "ds1"})
        ))
    assert res.status_code == 400


# --- the reader's own safety properties ------------------------------------


def _reader_env(col_names, rows):
    """Patch the warehouse seams `read_datastream_export` reads through."""
    return (
        patch("core.cache_warehouse._sample_relation_columns", return_value=col_names),
        patch("core.cache_warehouse._export_rows", return_value=rows),
    )


def test_the_export_keeps_the_SAME_masking_as_the_sample() -> None:
    """An export that dropped the masking would be a credential-free path to
    exactly the values the sample takes care to hide. This is the property that
    makes offering the export at all defensible."""
    from core.cache_warehouse import read_datastream_export

    cols = ["date", "email", "clicks"]
    rows = [{"date": "2026-08-02", "email": "someone@example.com", "clicks": 4}]
    with _reader_env(cols, rows)[0], _reader_env(cols, rows)[1]:
        out = read_datastream_export(
            project_id="p1", connector="search_console", stage="published",
            date_from="2026-08-01", date_to="2026-08-02",
        )

    assert out["rows"][0]["email"] != "someone@example.com"
    assert "email" in out["masked_fields"]
    # The unmasked columns still carry their real values.
    assert out["rows"][0]["clicks"] == 4


def test_an_unknown_column_is_refused_and_never_silently_dropped() -> None:
    """An export missing a column somebody asked for is worse than one that
    refuses: they will not notice."""
    from core.cache_warehouse import SampleReadError, read_datastream_export

    cols = ["date", "clicks"]
    with _reader_env(cols, [])[0], _reader_env(cols, [])[1]:
        try:
            read_datastream_export(
                project_id="p1", connector="search_console", stage="published",
                date_from="2026-08-01", date_to="2026-08-02", columns=["date", "impressions"],
            )
        except SampleReadError as exc:
            assert exc.code == "unknown_column"
            assert "impressions" in str(exc)
        else:
            raise AssertionError("an unknown column must refuse")


def test_projects_to_the_requested_columns_only() -> None:
    from core.cache_warehouse import read_datastream_export

    cols = ["date", "page", "clicks"]
    rows = [{"date": "2026-08-02", "page": "/a", "clicks": 4}]
    with _reader_env(cols, rows)[0], _reader_env(cols, rows)[1]:
        out = read_datastream_export(
            project_id="p1", connector="search_console", stage="published",
            date_from="2026-08-01", date_to="2026-08-02", columns=["clicks", "date"],
        )

    assert out["columns"] == ["clicks", "date"]
    assert set(out["rows"][0]) == {"clicks", "date"}
