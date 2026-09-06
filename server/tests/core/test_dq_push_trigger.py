"""The six DQ monitors had no push trigger, so none of them ever fired.

They ran in a daemon thread inside the Cloud Run process, which is deployed
`--min-instances=0` without `--no-cpu-throttling`: between two requests the
instance is throttled, and at zero instances it does not exist. The loop had
nowhere to run. The only other way to reach the monitors was
`POST /api/dq/evaluate`, a rate-limited human action.

`min-instances=0` is the cost posture and is not negotiable, so the answer is
the one the six sibling endpoints already take: a dumb, frequent external tick
that asks "what is due?". This file pins that the tick's target exists, is
authorized like its siblings, and reports what it did.

The arrival monitor of AI-113 is the concrete thing that was unreachable: an
operator sets "a file every day", the file stops coming, and nothing told them.
"""

from __future__ import annotations

import pytest
from core import internal_api  # AD-43 : le handler vit chez son sujet


def test_the_trigger_is_mounted_beside_its_six_siblings():
    """A trigger nothing routes to is a loop that still never runs."""
    from core.admin_api import router

    scheduler_paths = {
        route.path
        for route in router.routes
        if getattr(route, "path", "").startswith("/internal/scheduler/")
    }
    assert "/internal/scheduler/run-dq-monitors" in scheduler_paths
    # The family it belongs to, so a reader sees it is not a one-off shape.
    assert {
        "/internal/scheduler/dispatch-nightly",
        "/internal/scheduler/dispatch-hourly",
        "/internal/scheduler/poll-health",
    } <= scheduler_paths


def test_the_trigger_only_accepts_post():
    from core.admin_api import router

    methods = {
        method
        for route in router.routes
        if getattr(route, "path", None) == "/internal/scheduler/run-dq-monitors"
        for method in (route.methods or set())
        if method in ("GET", "POST")
    }
    assert methods == {"POST"}


@pytest.mark.anyio
async def test_an_unauthorized_tick_is_refused_before_any_sweep(monkeypatch):
    """The guard runs FIRST: a rejected call must not evaluate anything.

    A sweep that ran and then answered 401 would still write alerts on behalf of
    a caller the platform refused.
    """
    import core.admin_api as admin_api
    import core.dq_monitors as dqm
    from starlette.responses import JSONResponse

    swept: list[str] = []
    monkeypatch.setattr(
        dqm, "run_dq_monitors", lambda **kw: swept.append("ran") or {}
    )

    async def _deny(_request):
        return JSONResponse({"code": "unauthorized"}, status_code=401)

    monkeypatch.setattr(admin_api, "_authorize_internal", _deny)

    class _Request:
        query_params: dict = {}

    response = await internal_api._run_dq_monitors_internal(_Request())

    assert response.status_code == 401
    assert swept == [], "the sweep ran for a caller the platform refused"


@pytest.mark.anyio
async def test_an_authorized_tick_sweeps_every_project_and_reports(monkeypatch):
    """No project_id means the whole platform -- what a tick wants."""
    import json

    import core.admin_api as admin_api
    import core.dq_monitors as dqm

    seen: dict = {}

    def _run(**kwargs):
        seen.update(kwargs)
        return {"evaluated": 3, "timeliness_issues": 1, "total_issues": 1}

    monkeypatch.setattr(dqm, "run_dq_monitors", _run)
    monkeypatch.setattr(admin_api, "_authorize_internal", _allow)

    class _Request:
        query_params: dict = {}

    response = await internal_api._run_dq_monitors_internal(_Request())

    assert response.status_code == 200
    assert seen == {"project_id": None}
    # The summary travels back, so the tick's own logs carry what happened
    # rather than a bare 200 proving only that something answered.
    body = json.loads(bytes(response.body).decode())
    assert body["timeliness_issues"] == 1


@pytest.mark.anyio
async def test_a_targeted_tick_scopes_to_one_project(monkeypatch):
    import core.admin_api as admin_api
    import core.dq_monitors as dqm

    seen: dict = {}
    monkeypatch.setattr(
        dqm, "run_dq_monitors", lambda **kw: seen.update(kw) or {"evaluated": 1}
    )
    monkeypatch.setattr(admin_api, "_authorize_internal", _allow)

    class _Request:
        query_params = {"project_id": "proj-1"}

    await internal_api._run_dq_monitors_internal(_Request())
    assert seen == {"project_id": "proj-1"}


@pytest.mark.anyio
async def test_a_raising_sweep_answers_503_so_the_tick_returns(monkeypatch):
    """503, not 500: the work waits rather than dies.

    `run_dq_monitors` documents itself as never raising. Trusting that would
    make a scheduled call fail silently forever the day it stops being true.
    """
    import core.admin_api as admin_api
    import core.dq_monitors as dqm

    def _boom(**_kwargs):
        raise RuntimeError("warehouse unreachable")

    monkeypatch.setattr(dqm, "run_dq_monitors", _boom)
    monkeypatch.setattr(admin_api, "_authorize_internal", _allow)

    class _Request:
        query_params: dict = {}

    response = await internal_api._run_dq_monitors_internal(_Request())
    assert response.status_code == 503


async def _allow(_request):
    """The authorized case: the guard returns None."""
    return None


@pytest.fixture
def anyio_backend():
    return "asyncio"
