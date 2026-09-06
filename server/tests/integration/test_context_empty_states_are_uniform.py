"""One unknown project, one answer, on every Context Hub read (live finding C1).

MEASURED BEFORE THIS TEST EXISTED, in the default local config
(`TOOROW_AUTH_MODE=disabled`, identity `anonymous`):

    /api/context/topics?project_id=proj_UNKNOWN            -> 200 {"topics": []}
    /api/context/procedures?project_id=proj_UNKNOWN        -> 200 {"procedures": []}
    /api/context/graph/edges?project_id=proj_UNKNOWN       -> 200 {"edges": []}
    /api/context/graph?project_id=proj_UNKNOWN             -> 200 {"nodes": [], ...}
    /api/context/business-taxonomy?project_id=proj_UNKNOWN -> 404
    /api/context/business-links?project_id=proj_UNKNOWN    -> 404
    /api/context/review-requests?project_id=proj_UNKNOWN   -> 404
    /api/context/recurrent-fates?project_id=proj_UNKNOWN   -> 200 {"fates": []}

Five of the eight invented the project. A console reading `200 {"topics": []}`
cannot tell "this project has no governed knowledge yet" from "this project id
does not exist", and it renders the empty state of a project that is not there.
The taxonomy routes disagreed only because they happen to SELECT the org row,
which is the existence check the others skipped.

The rule this file pins: the disabled-auth developer bypass grants every
CAPABILITY on a project; it never grants the project's EXISTENCE. An unknown id
is a 404 in `make dev` exactly as it is under OAuth.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import pytest
from core.main import build_asgi_app
from starlette.testclient import TestClient

#: Every Context Hub read that takes a ?project_id=. A route added here without
#: its existence gate fails this test, which is the point: the defect was a
#: MISSING check on five routes, so the guard has to be the whole family.
CONTEXT_READS = (
    "/api/context/topics",
    "/api/context/procedures",
    "/api/context/graph/edges",
    "/api/context/graph",
    "/api/context/business-taxonomy",
    "/api/context/business-links",
    "/api/context/review-requests",
    "/api/context/recurrent-fates",
)


def _conn(*, project_exists: bool):
    """A connection whose every lookup is empty, except the existence probe."""
    conn = MagicMock()
    conn.__enter__.return_value = conn
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.return_value = (1,) if project_exists else None
    cur.fetchall.return_value = []
    cur.description = []
    return conn


@pytest.mark.parametrize("path", CONTEXT_READS)
def test_an_unknown_project_is_404_on_every_context_read(monkeypatch, path):
    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    client = TestClient(build_asgi_app())

    with (
        patch("core.admin_api._check_auth", return_value=(True, "anonymous")),
        patch("core.db.get_connection", side_effect=lambda *a, **k: _conn(project_exists=False)),
    ):
        response = client.get(f"{path}?project_id=proj_UNKNOWN")

    assert response.status_code == 404, f"{path} invented the project"
    assert response.json()["code"] == "not_found"


@pytest.mark.parametrize("path", CONTEXT_READS)
def test_a_known_but_empty_project_still_answers_200(monkeypatch, path):
    """The other half of the rule. An existence gate that also refuses a REAL
    empty project would trade one wrong answer for another -- an empty Context
    Hub is a legitimate state, and it is the state every new project starts in.
    """
    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    client = TestClient(build_asgi_app())

    with (
        patch("core.admin_api._check_auth", return_value=(True, "anonymous")),
        patch("core.db.get_connection", side_effect=lambda *a, **k: _conn(project_exists=True)),
    ):
        response = client.get(f"{path}?project_id=proj_EXAMPLE")

    assert response.status_code == 200, f"{path} refused an existing empty project"


@pytest.mark.parametrize("path", CONTEXT_READS)
def test_an_unreachable_store_is_a_500_and_never_an_empty_project(monkeypatch, path):
    """The third answer the family used to mix up. An outage must not be
    reported as "nothing is defined here" (context-hub.md, `Incomplete if`) --
    and must not be reported as a missing project either. It is a 500."""
    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    client = TestClient(build_asgi_app())

    with (
        patch("core.admin_api._check_auth", return_value=(True, "anonymous")),
        patch("core.db.get_connection", side_effect=RuntimeError("store down")),
    ):
        response = client.get(f"{path}?project_id=proj_EXAMPLE")

    assert response.status_code == 500, f"{path} disguised an outage"
    assert response.json()["code"] == "db_error"
