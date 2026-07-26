from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock

from starlette.requests import Request


def test_production_project_list_is_filtered_by_caller_authority(monkeypatch):
    from core import admin_api, db

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.setenv("TOOROW_EPIC36_PRODUCTION_ENABLED", "true")
    monkeypatch.setattr(
        admin_api,
        "_check_auth",
        AsyncMock(return_value=(True, "person-1")),
    )
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.description = [
        (name,)
        for name in (
            "id",
            "name",
            "slug",
            "status",
            "currency",
            "timezone",
            "org_id",
            "created_at",
            "updated_at",
        )
    ]
    cur.fetchall.return_value = []
    conn = MagicMock()
    conn.cursor.return_value = cur

    @contextmanager
    def get_connection():
        yield conn

    monkeypatch.setattr(db, "get_connection", get_connection)
    request = Request(
        {"type": "http", "method": "GET", "path": "/api/projects", "headers": []}
    )

    response = asyncio.run(admin_api._list_projects(request))

    assert response.status_code == 200
    assert json.loads(response.body) == {"projects": []}
    sql, params = cur.execute.call_args.args
    assert "JOIN app.org_members" in sql
    assert "app.resource_grants" in sql
    assert params == ("person-1", "person-1")
