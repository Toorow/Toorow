"""Real-ASGI seam for the progress route -- story 63.2.

Through `build_asgi_app()`, because a unit test on the reader proves arithmetic
and not that an address exists. What is proven here: the route is MOUNTED, the
guard is called with the `datastream_id` (its audit metadata is the only place
that id lands), a refusal reads NOTHING, the three named errors are the three
named errors, the envelope carries its flux even when nothing runs, and a native
`datetime` in the payload renders 200 instead of the bare 500 the stock
`JSONResponse` produces inside `render()`.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

PROGRESS_URL = "/api/projects/proj_a/datastreams/ds_1/progress"

_RUNNING = {
    "execution_id": "dse_01J8ZC4Q0N7R2K3W5X6Y7Z8A9B",
    "state": "loading",
    "step": "Collect",
    "day_in_progress": date(2026, 7, 12),
    "days_done": 12,
    "days_total": 30,
    "windows_done": 1,
    "windows_total": 24,
    "window_in_progress": {
        "date_from": date(2026, 7, 1),
        "date_to": date(2026, 7, 31),
        "days": 31,
    },
    "rows_written": 4218,
    "started_at": datetime(2026, 8, 5, 2, 15, tzinfo=timezone.utc),
    "progress_updated_at": datetime(2026, 8, 5, 2, 41, tzinfo=timezone.utc),
    "plan_version_id": "dsp_1",
    "mapping_version_id": "dmap_1",
    "estimate": {
        "armed": True,
        "observations": 4,
        "minimum_observations": 3,
        "precision": "point",
        "seconds_remaining": 2940,
        "seconds_remaining_low": 1470,
        "seconds_remaining_high": 4410,
        "spread_ratio": 2.0,
        "behind_by_seconds": 0,
        "measured_at": datetime(2026, 8, 5, 2, 51, tzinfo=timezone.utc),
        "reason": None,
        "sentence": "About 49 minutes left.",
    },
}

_IDLE_SUCCEEDED = {
    "reason": "last_run_succeeded",
    "execution_id": "dse_01J8ZC4Q0N7R2K3W5X6Y7Z8A9C",
    "state": "collected",
    "ended_at": datetime(2026, 8, 5, 3, 2, tzinfo=timezone.utc),
    "error_code": None,
}

_IDLE_FAILED = {
    "reason": "last_run_failed",
    "execution_id": "dse_01J8ZC4Q0N7R2K3W5X6Y7Z8A9D",
    "state": "failed",
    "ended_at": datetime(2026, 8, 5, 3, 2, tzinfo=timezone.utc),
    "error_code": "collection_window_failed",
}

_IDLE_NEVER = {
    "reason": "never_ran",
    "execution_id": None,
    "state": None,
    "ended_at": None,
    "error_code": None,
}

#: Story 63.6. A run somebody STOPPED, carrying what it kept -- after the stop
#: the run is terminal, so `progress` is null and this is the only payload that
#: still says how many days and rows were collected before it ended.
_IDLE_STOPPED = {
    "reason": "last_run_stopped",
    "execution_id": "dse_01J8ZC4Q0N7R2K3W5X6Y7Z8A9E",
    "state": "cancelled",
    "ended_at": datetime(2026, 8, 5, 3, 2, tzinfo=timezone.utc),
    "error_code": "collection_run_stopped",
    "days_done": 31,
    "days_total": 730,
    "rows_written": 18_420,
}

STOP_URL = "/api/projects/proj_a/datastreams/ds_1/runs/dse_stop_1/stop"

_STOP_ANSWER = {
    "execution_id": "dse_stop_1",
    "state": "cancelled",
    "windows_refused": 2,
    "window_in_flight": {"date_from": date(2026, 6, 1), "date_to": date(2026, 6, 30)},
    "days_kept": 31,
    "rows_kept": 18_420,
    "stopped_at": datetime(2026, 8, 6, 10, 4, tzinfo=timezone.utc),
}


@contextmanager
def _connection():
    yield MagicMock()


def _client() -> TestClient:
    from core.main import build_asgi_app

    # `raise_server_exceptions=False`: a render-time TypeError must be OBSERVED
    # as a 500, not re-raised into the test -- that is the failure this route
    # has to prove it does not have.
    return TestClient(build_asgi_app(), raise_server_exceptions=False)


def _authenticated():
    return patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_1")))


def _database():
    return patch("core.db.get_connection", side_effect=_connection)


def _guard(denied=None):
    return patch("core.admin_api._require_datastream_role", return_value=denied)


# ---------------------------------------------------------------------------
# The address exists.
# ---------------------------------------------------------------------------


def test_the_route_is_mounted_on_the_application() -> None:
    from core import admin_api

    assert any(
        getattr(route, "path", "")
        == "/api/projects/{project_id}/datastreams/{datastream_id}/progress"
        for route in admin_api.router.routes
    )


# ---------------------------------------------------------------------------
# What it answers.
# ---------------------------------------------------------------------------


def test_a_running_flux_answers_its_progress_under_the_envelope() -> None:
    with (
        _authenticated(),
        _guard() as guard,
        _database(),
        patch(
            "core.datastream_progress_api.read_active_progress", return_value=_RUNNING
        ) as read,
        patch("core.datastream_progress_api.read_idle_reason") as idle,
    ):
        response = _client().get(PROGRESS_URL)

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema"] == "datastream_progress.v1"
    assert payload["project_id"] == "proj_a"
    assert payload["datastream_id"] == "ds_1"
    assert payload["progress"]["execution_id"] == _RUNNING["execution_id"]
    assert payload["progress"]["days_done"] == 12
    assert payload["progress"]["days_total"] == 30
    assert payload["idle"] is None
    # The tick that repeats costs ONE read: nothing asks why a running run is
    # not running.
    assert idle.call_count == 0
    # The guard receives the datastream id, and the read receives the pair.
    assert guard.call_args.kwargs["datastream_id"] == "ds_1"
    assert guard.call_args.args[2] == "viewer"
    assert read.call_args.kwargs == {"project_id": "proj_a", "datastream_id": "ds_1"}


def test_a_plateau_is_explained_by_the_window_in_flight() -> None:
    """A day count that has not moved for twenty minutes must be readable as work."""
    with (
        _authenticated(),
        _guard(),
        _database(),
        patch(
            "core.datastream_progress_api.read_active_progress",
            return_value={
                **_RUNNING,
                "days_done": 0,
                "days_total": 730,
                "windows_done": 0,
                "windows_total": 24,
            },
        ),
        patch("core.datastream_progress_api.read_idle_reason"),
    ):
        response = _client().get(PROGRESS_URL)

    progress = response.json()["progress"]
    assert (progress["days_done"], progress["days_total"]) == (0, 730)
    assert (progress["windows_done"], progress["windows_total"]) == (0, 24)
    assert progress["window_in_progress"] == {
        "date_from": "2026-07-01",
        "date_to": "2026-07-31",
        "days": 31,
    }


def test_how_much_longer_travels_in_the_payload_and_not_on_a_screen() -> None:
    """Story 63.4, and the reason it is server-side: the MCP reads this body too.

    A duration derived in the console would be a number the tools cannot see --
    two surfaces, two answers to the same question, which is what story 53.9
    refuses. The instant the estimate was taken travels with it, so nothing
    downstream has to reach for a browser clock to age it.
    """
    with (
        _authenticated(),
        _guard(),
        _database(),
        patch("core.datastream_progress_api.read_active_progress", return_value=_RUNNING),
        patch("core.datastream_progress_api.read_idle_reason"),
    ):
        response = _client().get(PROGRESS_URL)

    estimate = response.json()["progress"]["estimate"]
    assert estimate["armed"] is True
    assert estimate["seconds_remaining"] == 2940
    assert estimate["sentence"] == "About 49 minutes left."
    assert estimate["measured_at"] == "2026-08-05T02:51:00Z"
    # The bound is disclosed with the measurement, never only with the silence.
    assert (estimate["observations"], estimate["minimum_observations"]) == (4, 3)
    # And so is the DISPERSION of the sample the number came from: a point off
    # three runs that agreed and one off three that disagreed by a factor of 120
    # are not the same claim, and nothing else in this repository says which.
    assert estimate["spread_ratio"] == 2.0
    assert estimate["precision"] == "point"
    assert estimate["seconds_remaining_low"] < estimate["seconds_remaining"]
    assert estimate["seconds_remaining"] < estimate["seconds_remaining_high"]


def test_an_estimate_that_declines_still_carries_a_sentence() -> None:
    """A block that says nothing must say WHY -- never an absent line, never a 0."""
    from core.datastream_progress_estimate import ESTIMATE_NOT_ENOUGH_HISTORY

    silent = {
        **_RUNNING,
        "estimate": {
            "armed": False,
            "observations": 1,
            "minimum_observations": 3,
            "precision": None,
            "seconds_remaining": None,
            "seconds_remaining_low": None,
            "seconds_remaining_high": None,
            "spread_ratio": None,
            "behind_by_seconds": None,
            "measured_at": datetime(2026, 8, 5, 2, 51, tzinfo=timezone.utc),
            "reason": ESTIMATE_NOT_ENOUGH_HISTORY,
            "sentence": (
                "Not enough finished runs of this Datastream to estimate — "
                "1 of 3 measured."
            ),
        },
    }
    with (
        _authenticated(),
        _guard(),
        _database(),
        patch("core.datastream_progress_api.read_active_progress", return_value=silent),
        patch("core.datastream_progress_api.read_idle_reason"),
    ):
        response = _client().get(PROGRESS_URL)

    estimate = response.json()["progress"]["estimate"]
    assert estimate["armed"] is False
    assert estimate["seconds_remaining"] is None
    assert estimate["reason"] == ESTIMATE_NOT_ENOUGH_HISTORY
    assert "1 of 3" in estimate["sentence"]


def test_a_timestamptz_and_a_date_render_200_and_not_a_bare_500() -> None:
    """The defect `SafeJSONResponse` exists for: `TypeError` inside `render()`.

    It is raised AFTER the handler returned and OUTSIDE its `try/except`, so the
    503 branch never sees it and the client gets a 500 with nothing in the log.
    """
    with (
        _authenticated(),
        _guard(),
        _database(),
        patch("core.datastream_progress_api.read_active_progress", return_value=_RUNNING),
    ):
        response = _client().get(PROGRESS_URL)

    assert response.status_code == 200
    assert response.json()["progress"]["started_at"] == "2026-08-05T02:15:00Z"
    assert response.json()["progress"]["day_in_progress"] == "2026-07-12"


def test_nothing_running_answers_a_named_envelope_and_not_a_bare_null() -> None:
    with (
        _authenticated(),
        _guard(),
        _database(),
        patch("core.datastream_progress_api.read_active_progress", return_value=None),
        patch(
            "core.datastream_progress_api.read_idle_reason", return_value=_IDLE_SUCCEEDED
        ) as idle,
    ):
        response = _client().get(PROGRESS_URL)

    assert response.status_code == 200
    assert response.json() == {
        "schema": "datastream_progress.v1",
        "project_id": "proj_a",
        "datastream_id": "ds_1",
        "progress": None,
        "idle": {
            "reason": "last_run_succeeded",
            "execution_id": "dse_01J8ZC4Q0N7R2K3W5X6Y7Z8A9C",
            "state": "collected",
            "ended_at": "2026-08-05T03:02:00Z",
            "error_code": None,
        },
    }
    assert idle.call_args.kwargs == {"project_id": "proj_a", "datastream_id": "ds_1"}


@pytest.mark.parametrize(
    ("idle", "reason"),
    [
        (_IDLE_SUCCEEDED, "last_run_succeeded"),
        (_IDLE_FAILED, "last_run_failed"),
        (_IDLE_NEVER, "never_ran"),
        (_IDLE_STOPPED, "last_run_stopped"),
    ],
)
def test_the_three_reasons_a_poll_stops_are_three_different_bodies(idle, reason) -> None:
    """One silence for "finished well", "broke" and "never started" is the defect."""
    with (
        _authenticated(),
        _guard(),
        _database(),
        patch("core.datastream_progress_api.read_active_progress", return_value=None),
        patch("core.datastream_progress_api.read_idle_reason", return_value=idle),
    ):
        response = _client().get(PROGRESS_URL)

    assert response.status_code == 200
    body = response.json()
    assert body["progress"] is None
    assert body["idle"]["reason"] == reason
    assert body["idle"]["state"] == idle["state"]
    assert body["idle"]["error_code"] == idle["error_code"]


# ---------------------------------------------------------------------------
# What it refuses.
# ---------------------------------------------------------------------------


def test_an_unauthenticated_call_is_401_and_never_reaches_the_database() -> None:
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(False, None))),
        _database() as connect,
        patch("core.datastream_progress_api.read_active_progress") as read,
    ):
        response = _client().get(PROGRESS_URL)

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"
    assert connect.call_count == 0
    assert read.call_count == 0


def test_a_refused_role_reads_nothing_at_all() -> None:
    """A refusal that still ran the SELECT would be a scope leak with a 404 on top."""
    from starlette.responses import JSONResponse

    denied = JSONResponse({"code": "not_found", "message": "Flux de donnees introuvable"}, 404)
    with (
        _authenticated(),
        _guard(denied=denied),
        _database(),
        patch("core.datastream_progress_api.read_active_progress") as read,
    ):
        response = _client().get(PROGRESS_URL)

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert read.call_count == 0


def test_a_flux_of_another_project_is_a_non_disclosing_404() -> None:
    from core.datastream_progress_api import DatastreamNotFound

    with (
        _authenticated(),
        _guard(),
        _database(),
        patch(
            "core.datastream_progress_api.read_active_progress",
            side_effect=DatastreamNotFound("ds_1"),
        ),
    ):
        response = _client().get(PROGRESS_URL)

    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "not_found"
    # Nothing in the body confirms the flux exists somewhere else.
    assert "proj" not in body["message"]
    assert set(body) == {"code", "message"}


def test_an_unavailable_database_is_503_and_leaves_a_typed_trace(caplog) -> None:
    with (
        _authenticated(),
        _guard(),
        _database(),
        patch(
            "core.datastream_progress_api.read_active_progress",
            side_effect=RuntimeError("connection reset"),
        ),
        caplog.at_level("ERROR", logger="core.datastream_progress_api"),
    ):
        response = _client().get(PROGRESS_URL)

    assert response.status_code == 503
    assert response.json()["code"] == "unavailable"
    assert "RuntimeError" in caplog.text


# ---------------------------------------------------------------------------
# The stop route -- story 63.6. The first WRITE of this address family.
# ---------------------------------------------------------------------------


def _run_belongs():
    """The scope proof the handler runs before it writes: the run IS this one."""
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (1,)

    @contextmanager
    def _open():
        yield connection

    return patch("core.db.get_connection", side_effect=_open), connection


def _run_missing():
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = None

    @contextmanager
    def _open():
        yield connection

    return patch("core.db.get_connection", side_effect=_open), connection


def test_the_stop_route_is_mounted_on_the_application() -> None:
    from core import admin_api

    assert any(
        getattr(route, "path", "")
        == "/api/projects/{project_id}/datastreams/{datastream_id}"
           "/runs/{execution_id}/stop"
        for route in admin_api.router.routes
    )


def test_a_stop_answers_what_it_refused_and_what_it_kept() -> None:
    database, connection = _run_belongs()
    with (
        _authenticated(),
        _guard(),
        database,
        patch(
            "core.datastream_progress_api.stop_collection_run",
            return_value=_STOP_ANSWER,
        ),
    ):
        response = _client().post(STOP_URL)

    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "datastream_run_stop.v1"
    assert body["windows_refused"] == 2
    # The window in flight is NAMED, because it is not interrupted.
    assert body["window_in_flight"] == {"date_from": "2026-06-01", "date_to": "2026-06-30"}
    assert body["days_kept"] == 31
    assert body["rows_kept"] == 18_420
    # A write, so it commits -- unlike every read of this module.
    assert connection.commit.called


def test_the_stop_demands_the_role_that_may_start_a_run() -> None:
    """No new role and no asymmetry: whoever spends the quota may stop spending it.

    `member` is what the refetch endpoint requires to enqueue windows.
    """
    database, _ = _run_belongs()
    with (
        _authenticated(),
        patch("core.admin_api._require_datastream_role", return_value=None) as guard,
        database,
        patch(
            "core.datastream_progress_api.stop_collection_run",
            return_value=_STOP_ANSWER,
        ),
    ):
        _client().post(STOP_URL)

    assert guard.call_args.args[2] == "member"
    # And the pair is PROVEN here, never claimed: this is a write reached by a
    # click, not the polled read next to it.
    assert guard.call_args.kwargs.get("pair_proven_by_read") is not True
    assert guard.call_args.kwargs["datastream_id"] == "ds_1"


def test_a_refused_role_writes_nothing_at_all() -> None:
    from starlette.responses import JSONResponse

    denial = JSONResponse({"code": "forbidden", "message": "no"}, status_code=403)
    database, connection = _run_belongs()
    with (
        _authenticated(),
        _guard(denied=denial),
        database,
        patch("core.datastream_progress_api.stop_collection_run") as stop,
    ):
        response = _client().post(STOP_URL)

    assert response.status_code == 403
    assert not stop.called
    assert not connection.commit.called


def test_a_run_of_another_stream_is_a_non_disclosing_404() -> None:
    """The role guard proves (stream, project); this proves (run, stream).

    A run of project A and a stream of project A do not make that run this
    stream's, and a refusal that says which is which leaks it.
    """
    database, _ = _run_missing()
    with (
        _authenticated(),
        _guard(),
        database,
        patch("core.datastream_progress_api.stop_collection_run") as stop,
    ):
        response = _client().post(STOP_URL)

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert not stop.called


@pytest.mark.parametrize(
    ("code", "status"),
    [("run_not_running", 409), ("not_a_collection_run", 409), ("not_found", 404)],
)
def test_each_refusal_answers_its_own_code_and_its_own_status(code, status) -> None:
    """Three situations a person can act on: three codes, three sentences."""
    from core.execution_progress import StopRefused

    database, connection = _run_belongs()
    with (
        _authenticated(),
        _guard(),
        database,
        patch(
            "core.datastream_progress_api.stop_collection_run",
            side_effect=StopRefused(code),
        ),
    ):
        response = _client().post(STOP_URL)

    assert response.status_code == status
    body = response.json()
    assert body["code"] == code
    assert body["message"]
    assert not connection.commit.called


def test_an_unauthenticated_stop_is_401_and_never_reaches_the_database() -> None:
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(False, None))),
        patch("core.db.get_connection", side_effect=AssertionError("must not open")),
    ):
        response = _client().post(STOP_URL)

    assert response.status_code == 401
