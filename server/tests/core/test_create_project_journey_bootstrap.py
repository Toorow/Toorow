"""`POST /api/projects` reaches its journey bootstrap with the resolved org.

Why this file exists, because it is the point rather than a detail: Story 46.4's
Getting Started bootstrap was called as
`bootstrap_project_journey(conn, org_id=org_id, ...)`, and no `org_id` exists in
`_create_project` -- it binds `org_id_in` and `org_id_for_project`. Every call
raised `NameError`, the enclosing handler DELETED the project row it had just
inserted, and the API answered `500 key_provision_failed`, an error naming the
wrong cause. **No project could be created, in any environment, from 2026-07-29
until the Epic 46 review found it.**

`ruff` had been reporting it as F821 the whole time. What no test could report is
the reason this file exists: `server/tests/core/test_projects_api.py` is twelve
tests and twelve skips (pg-gated), so project creation had NO offline coverage at
all. One identifier was fixed; without a test at this seam the next rename lands
exactly the same way.

So this pins the CONTRACT, not the SQL: the handler resolves the organization,
inserts the project, and hands the bootstrap the org it resolved. It runs against
the fake connection the rest of `server/tests/core` uses, so it needs no Postgres
and cannot write anywhere.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import core.projects_api as projects_api  # AD-43 : le handler vit chez son sujet
import pytest

IDENTITY = "owner@example.com"
ORG = "org_EXAMPLE"


def _request(body: dict) -> MagicMock:
    request = MagicMock()
    request.body = AsyncMock(return_value=json.dumps(body).encode())
    request.query_params = {}
    request.path_params = {}
    request.headers = {}
    return request


class _Cursor:
    """Answers the reads `_create_project` makes, in the order it makes them.

    `slug_free` -> the collision probe finds nothing. `member` -> the caller is an
    active member of the requested org. The INSERT returns one row plus a
    description, because the handler builds its response from them.
    """

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self.description = [("id",), ("name",), ("slug",), ("org_id",), ("status",)]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        sql = self.executed[-1][0] if self.executed else ""
        if "FROM app.projects WHERE slug" in sql:
            return None                       # slug is free
        if "FROM app.org_members" in sql:
            return (1,)                       # caller is an active member
        # `ensure_active_configuration_version` : un Projet naît avec sa
        # version 1 de configuration, et la lit FOR UPDATE juste avant le
        # bootstrap. Sans cette réponse, le handler lève « Project not found »
        # et l'assertion sur le bootstrap accuse le mauvais coupable.
        if "active_configuration_version_id FROM app.projects" in sql:
            return (None,)                    # aucune version active encore
        if "COALESCE(MAX(version_number), 0) + 1" in sql:
            return (1,)                       # la première version
        if sql.startswith("INSERT INTO app.projects"):
            return ("proj_EXAMPLE", "Acme", "acme", ORG, "active")
        return None

    def fetchall(self):
        return []


def _conn() -> MagicMock:
    cursor = _Cursor()
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cursor)
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn._cursor = cursor
    return conn


@pytest.mark.anyio
async def test_create_project_hands_the_bootstrap_the_org_it_resolved():
    from core import admin_api  # noqa: F401 -- registers the served router before patching it

    conn = _conn()

    @contextmanager
    def get_connection():
        yield conn

    bootstrap = MagicMock(return_value="journey_EXAMPLE")
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, IDENTITY))),
        patch("core.db.get_connection", new=get_connection),
        patch("core.getting_started.bootstrap_project_journey", bootstrap),
        patch("core.projects_api.write_audit_row"),
        patch("core.tenant_keys.get_tenant_key_backend", return_value=MagicMock()),
        patch("core.tenant_keys.write_key_audit_row"),
    ):
        response = await projects_api._create_project(
            _request({"name": "Acme", "org_id": ORG})
        )

    # The bootstrap ran, and it received the RESOLVED organization -- the whole
    # defect was passing a name that did not exist in this scope.
    assert bootstrap.called, (
        "the Getting Started journey was never bootstrapped; if this fails with a "
        "NameError the C-6 regression is back"
    )
    assert bootstrap.call_args.kwargs["org_id"] == ORG
    minted = bootstrap.call_args.kwargs["project_id"]
    assert minted.startswith("proj_") and len(minted) > 5, minted
    assert bootstrap.call_args.kwargs["actor_identity"] == IDENTITY

    # And the caller is not told "failed to provision tenant key" for a mistake
    # that has nothing to do with keys.
    body = json.loads(response.body)
    assert response.status_code < 400, body
    assert body.get("code") != "key_provision_failed"


@pytest.mark.anyio
async def test_create_project_refuses_a_foreign_organization_before_inserting():
    """The membership check comes before the row exists, and says so without disclosing."""
    from core import admin_api  # noqa: F401 -- registers the served router before patching it

    conn = _conn()
    conn._cursor.fetchone = lambda: None      # slug free, and NOT a member

    @contextmanager
    def get_connection():
        yield conn

    bootstrap = MagicMock()
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, IDENTITY))),
        patch("core.db.get_connection", new=get_connection),
        patch("core.getting_started.bootstrap_project_journey", bootstrap),
        patch("core.projects_api.write_audit_row"),
        patch("core.tenant_keys.get_tenant_key_backend", return_value=MagicMock()),
        patch("core.tenant_keys.write_key_audit_row"),
    ):
        response = await projects_api._create_project(
            _request({"name": "Acme", "org_id": "org_SOMEONE_ELSE"})
        )

    assert response.status_code == 403
    assert json.loads(response.body)["code"] == "forbidden"
    bootstrap.assert_not_called()
    assert not any(
        sql.startswith("INSERT INTO app.projects") for sql, _ in conn._cursor.executed
    ), "the refusal must come before the project row exists"
