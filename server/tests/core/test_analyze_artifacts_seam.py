"""Seam tests for the Story 50.3 Report / Notebook / Render routes.

Mounted on `build_asgi_app()` rather than a hand-assembled router, because the
question these answer is "is the door actually there?" -- and a hand-built router
answers it about a router nobody serves.

The lifecycle properties (immutability, idempotency, scoped foreign keys, RLS) are
proved against a real PostgreSQL in `test_analyze_artifacts_pg.py`. This file
proves those routes are REACHABLE, correctly guarded, correctly ordered, and that
none of them emits a bearer.
"""

from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock, patch

import pytest

from tests.support.navigation_source import navigation_source  # noqa: F401

_BASE = "/api/projects/proj_EXAMPLE/analyze"


def _client(*, authorized=True, identity="seam@example.com"):
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    app = build_asgi_app()
    return TestClient(app, raise_server_exceptions=False), patch(
        "core.admin_api._check_auth", new=AsyncMock(return_value=(authorized, identity))
    )


@pytest.fixture()
def client():
    test_client, auth = _client()
    with auth, test_client as ready:
        yield ready


@pytest.fixture()
def client_unauth():
    test_client, auth = _client(authorized=False, identity="")
    with auth, test_client as ready:
        yield ready


ROUTES = (
    ("GET", f"{_BASE}/artifact-migration"),
    ("GET", f"{_BASE}/reports"),
    ("POST", f"{_BASE}/reports"),
    ("GET", f"{_BASE}/reports/rep_x"),
    ("POST", f"{_BASE}/reports/rep_x/versions"),
    ("POST", f"{_BASE}/reports/rep_x/archive"),
    ("POST", f"{_BASE}/report-versions/repv_x/run"),
    ("GET", f"{_BASE}/notebooks"),
    ("POST", f"{_BASE}/notebooks"),
    ("GET", f"{_BASE}/notebooks/legacy"),
    ("GET", f"{_BASE}/notebooks/nbk_x"),
    ("POST", f"{_BASE}/notebooks/nbk_x/versions"),
    ("PUT", f"{_BASE}/notebooks/nbk_x/schedule"),
    ("POST", f"{_BASE}/notebooks/nbk_x/runs"),
    ("POST", f"{_BASE}/notebooks/nbk_x/archive"),
    ("GET", f"{_BASE}/notebook-runs/nbkrun_x"),
    ("GET", f"{_BASE}/renders"),
    ("POST", f"{_BASE}/renders"),
    ("GET", f"{_BASE}/renders/legacy"),
    ("GET", f"{_BASE}/renders/rnd_x"),
)


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_every_route_is_mounted_and_requires_authentication(client_unauth, method, path):
    """One assertion proves both, which is why they are not two tests.

    An UNMOUNTED path answers 404 to an anonymous request; a mounted one answers
    401, because `_authorize` denies before any work. Asserting `!= 404` on an
    AUTHENTICATED request would prove nothing at all -- 404 is also the honest
    not-found envelope for a Project that does not exist, so that test passes
    just as happily when nothing is mounted. This one cannot.
    """
    assert client_unauth.request(method, path, json={}).status_code == 401, (
        f"{method} {path} is not mounted, or does not deny before doing work"
    )


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_no_route_answers_405_for_its_own_method(client, method, path):
    """A route that rejects its own verb is indistinguishable from absent."""
    assert client.request(method, path, json={}).status_code != 405


def test_literal_segments_are_declared_before_parameterized_ones():
    """Route order is load-bearing, so it is asserted rather than trusted.

    `/renders/legacy` after `/renders/{render_id}` would make the legacy browser
    unreachable, and `/notebooks/{id}` before `/notebook-runs/{id}` would be a
    silently wrong object. Both mistakes are invisible in review and obvious here.
    """
    from core.analyze_artifacts_api import _BASE as TEMPLATE
    from core.analyze_artifacts_api import ROUTES as MOUNTED

    paths = [route.path for route in MOUNTED]
    assert paths.index(f"{TEMPLATE}/renders/legacy") < paths.index(
        f"{TEMPLATE}/renders/{{render_id}}"
    )
    assert paths.index(f"{TEMPLATE}/reports/{{report_id}}/versions") < paths.index(
        f"{TEMPLATE}/reports/{{report_id}}"
    )
    assert paths.index(f"{TEMPLATE}/notebooks/{{notebook_id}}/versions") < paths.index(
        f"{TEMPLATE}/notebooks/{{notebook_id}}"
    )
    assert paths.index(f"{TEMPLATE}/notebooks/{{notebook_id}}/runs") < paths.index(
        f"{TEMPLATE}/notebooks/{{notebook_id}}"
    )
    assert paths.index(f"{TEMPLATE}/notebooks/{{notebook_id}}/schedule") < paths.index(
        f"{TEMPLATE}/notebooks/{{notebook_id}}"
    )
    # The archive gesture, added 2026-08-17. `/reports/{id}/archive` captured by
    # `/reports/{id}` would answer the read handler to a write, i.e. a button
    # that reports success and retires nothing.
    assert paths.index(f"{TEMPLATE}/reports/{{report_id}}/archive") < paths.index(
        f"{TEMPLATE}/reports/{{report_id}}"
    )
    assert paths.index(f"{TEMPLATE}/notebooks/{{notebook_id}}/archive") < paths.index(
        f"{TEMPLATE}/notebooks/{{notebook_id}}"
    )
    # `/notebooks/legacy` after `/notebooks/{id}` would make the legacy Notebook
    # browser answer 404 for a Notebook called `legacy` -- the same mistake as
    # `/renders/legacy`, one route family later.
    assert paths.index(f"{TEMPLATE}/notebooks/legacy") < paths.index(
        f"{TEMPLATE}/notebooks/{{notebook_id}}"
    )


def test_reads_require_view_and_writes_require_membership():
    """Visibility and write authority are separate authorities (AC13)."""
    import inspect

    from core import analyze_artifacts_api as api

    source = inspect.getsource(api)
    # Ten reads, TEN write/execute handlers -- counted, so a new handler cannot
    # be added with the wrong authority without this failing. The two added on
    # 2026-08-17 are `_archive_report` and `_archive_notebook`: retiring an
    # artifact is a write, and a viewer must not be able to empty someone's list.
    assert source.count('await _authorize(request, "viewer")') == 10
    assert source.count('await _authorize(request, "member")') == 10


def test_no_route_can_create_a_share_or_return_a_bearer():
    """AC15: this story creates no raw path token, and no list response leaks one.

    Checked over the handler bodies rather than the whole module, because the
    module docstring names `app.render_snapshot_shares` in order to explain what
    this story refuses to repeat -- and a check that forbade naming the mistake
    would push the explanation out of the file.
    """
    import inspect

    from core import analyze_artifacts_api as api

    bodies = "\n".join(
        inspect.getsource(obj)
        for name, obj in vars(api).items()
        if name.startswith("_") and inspect.isfunction(obj)
    )
    for forbidden in ("share_token", "bearer", "token=", "shares"):
        assert forbidden not in bodies, f"`{forbidden}` must not appear in a handler"


def test_the_service_never_reads_a_latest_pointer():
    """AC15/AC6: no export, schedule or share path may follow `latest` evidence."""
    import inspect

    from core import analyze_artifacts as svc

    source = inspect.getsource(svc)
    # `LIMIT 1` ordered by time appears exactly twice, both in the COLLECTION
    # projection that shows a "last run" badge. Nothing else may resolve evidence
    # by recency -- a Run, a Result and a Render are always addressed by identity.
    assert source.count("ORDER BY r2.accepted_at DESC LIMIT 1") == 1
    assert source.count("ORDER BY r3.accepted_at DESC LIMIT 1") == 1
    assert "'latest'" not in source.replace("_FORBIDDEN_PIN_VALUES", "")


def test_the_ten_replay_pins_are_declared_and_complete():
    """`visualization-and-rendering.md:318-322`, enumerated in code, not in prose."""
    from core.analyze_artifacts import RENDER_REPLAY_PINS

    assert [name for name, _ in RENDER_REPLAY_PINS] == [
        "result_identity",
        "retained_result_data",
        "visualization_spec_version",
        "renderer_build",
        "runtime_build",
        "theme_version",
        "formatter_version",
        "responsive_profile",
        "local_display_state",
        "evidence_manifest",
    ]
    assert len(RENDER_REPLAY_PINS) == 10


def test_the_scheduler_dispatches_through_this_service_and_no_other():
    """AC7: `NOTEBOOK_DISPATCHER` names a call site, so the claim is checkable.

    The schedule table used to have exactly three references in the repository, all
    three inside `analyze_artifacts.py`, while `scheduler.py` scanned
    `FROM app.notebooks` and the Notebook panel rendered a green "Enabled: Yes".
    Nothing ever ran. This test is what stops that from being true again quietly:
    it reads the scheduler's own source and requires the call.
    """
    import inspect

    from core import scheduler
    from core.analyze_artifacts import NOTEBOOK_DISPATCHER

    source = inspect.getsource(scheduler)
    assert "dispatch_due_notebook_schedules" in source, (
        "the canonical Notebook dispatch is not wired to the nightly step"
    )
    module_path, _, function_name = NOTEBOOK_DISPATCHER.partition("::")
    assert module_path == "server/core/scheduler.py"
    assert f"def {function_name}(" in source
    # And it is reached FROM the nightly step, not merely defined in the file.
    assert "_dispatch_due_canonical_notebooks()" in inspect.getsource(
        getattr(scheduler, function_name)
    )


def test_the_notebook_dispatch_reuses_the_manual_run_service():
    """One execution path. A second one is how scheduled evidence gets weaker."""
    import inspect

    from core import analyze_artifacts as svc

    source = inspect.getsource(svc.dispatch_due_notebook_schedules)
    assert "run_notebook(" in source
    assert 'dispatch_source="scheduled"' in source
    # The idempotency key is the calendar period, so a nightly step that fires
    # twice completes the Run it already accepted.
    assert "schedule_period_key(" in source


def test_analyze_has_no_widgets_destination():
    """AC10: Widgets is not a Level 2 screen (`analyze-and-test.md:42-46`)."""
    # AD-42 : chaque espace declare ses sections dans son fichier. Decouper le
    # texte sur la cle voisine ne marche plus -- et ne marcherait plus EN
    # SILENCE : `split` leverait, mais un `not in` repondrait simplement False.
    analyze = (
        pathlib.Path(__file__).resolve().parents[3]
        / "ui" / "admin" / "src" / "shell" / "navigation" / "analyze.ts"
    ).read_text(encoding="utf-8")
    assert 'section("widgets"' not in analyze
    for expected in ("explore", "reports", "notebooks", "renders"):
        assert f'section("{expected}"' in analyze
