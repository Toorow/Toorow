"""`GET /api/datastreams/{id}/run/preview` -- what a run would collect, before it runs.

WHY THE ROUTE EXISTS. `SchedulePanel` offers the cadence `manual`, described as
"only runs when someone asks it to", and nothing in the console asked. The door
that runs one by hand -- `POST /api/datastreams/{id}/run` -- has existed since
story 8.2, but the ratified surface requires a confirmation to name the
connector, the account and THE WINDOW before anything is spent, and the window
was knowable only by running. That left two bad options: spend first and report
after, or re-implement `pull_window.resolve_window` in the console and let two
copies of the arbitration drift.

WHAT THESE TESTS PIN. Not the arithmetic of the window -- `test_pull_window.py`
owns that. They pin that the preview is the SAME answer as the POST: the same
row loader, the same resolver, the same gates, in the same order. A preview that
can disagree with the run it previews is worse than no preview, because a person
consents to one thing and pays for another.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT = "proj_EXAMPLE"
STREAM = "ds_EXAMPLE"

#: The same shape `test_admin_api_refetch.STREAM_ROW` carries, and for the same
#: reason: `gate_refusal` answers `row_incomplete` for a key the double did not
#: project, so a fixture that forgets one proves a gate that never ran.
STREAM_ROW = {
    "ds_id": STREAM,
    "project_id": PROJECT,
    "module_name": "meta-ads",
    "connection_ref_id": "cref_EXAMPLE",
    "cr_status": "active",
    "cr_enabled": True,
    "enabled": True,
    "lifecycle_state": "active",
    "archived_at": None,
    "source_kind": "connector_pull",
    "schedule_mode": "nightly",
    "refetch_days": 3,
    "date_window_days": 30,
    "window_offset_days": 1,
    "current_plan_version_id": "dsp_EXAMPLE",
    "current_mapping_version_id": "dsm_EXAMPLE",
    "module_enabled": True,
    "project_status": "active",
}


def _run(coro):
    return asyncio.run(coro)


def _request(*, project_id=PROJECT, ds_id=STREAM):
    req = MagicMock()
    req.path_params = {"id": ds_id}
    req.query_params = {"project_id": project_id} if project_id else {}
    req.headers = {"authorization": "Bearer test-token"}

    async def _body():
        return b""

    req.body = _body
    return req


def _body_of(response):
    return json.loads(response.body.decode())


def _get(*, stream=STREAM_ROW, project_id=PROJECT, scope_refusal=None):
    """Call the handler with every collaborator wired."""
    from contextlib import ExitStack

    from core.datastream_collection_api import _preview_datastream_run

    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "core.admin_api._check_auth",
                new=AsyncMock(return_value=(True, "owner@example.com")),
            )
        )
        conn_ctx = stack.enter_context(patch("core.db.get_connection"))
        conn = MagicMock()
        conn_ctx.return_value.__enter__ = lambda _s: conn
        conn_ctx.return_value.__exit__ = MagicMock(return_value=False)

        stack.enter_context(
            patch("core.datastream_dispatch.load_dispatch_row", return_value=stream)
        )
        stack.enter_context(
            patch(
                "core.admin_api._enforce_datastream_project_scope",
                return_value=scope_refusal,
            )
        )
        # The project's last complete day, pinned so the window is checkable.
        stack.enter_context(patch("core.scheduler.project_timezone", return_value="UTC"))
        stack.enter_context(
            patch("core.scheduler.project_yesterday", return_value=date(2026, 8, 17))
        )
        return _run(_preview_datastream_run(_request(project_id=project_id)))


def test_the_preview_names_the_exact_window_the_run_would_ask_for() -> None:
    """The window a person consents to, composed by the module that owns it."""
    response = _get()

    assert response.status_code == 200
    body = _body_of(response)
    # 30 days ending on the last complete day (offset 1 -> ends J-1 = 2026-08-17).
    assert body["date_from"] == "2026-07-19"
    assert body["date_to"] == "2026-08-17"
    assert body["window_days"] == 30
    assert body["refusal"] is None


def test_the_preview_and_the_run_compose_the_SAME_window() -> None:
    """The whole point, asserted rather than assumed.

    Both doors call `pull_window.resolve_window` on the same row with the same
    end reference. This binds them: if one of them ever grows its own
    arbitration, a person will have confirmed a window and paid for another.
    """
    from core import pull_window

    preview = _body_of(_get())
    what_the_run_would_use = pull_window.resolve_window(
        STREAM_ROW,
        end_reference=date(2026, 8, 17),
        cadence=STREAM_ROW["schedule_mode"],
    ).as_interval()

    assert (preview["date_from"], preview["date_to"]) == (
        what_the_run_would_use["date_from"],
        what_the_run_would_use["date_to"],
    )


def test_the_preview_says_WHICH_declaration_answered() -> None:
    """`defensive_default` is a fallback, not a setting, and it must read as one.

    A row declaring no window at all still previews three days. Presenting that
    as the Datastream's window would be the invented figure this repository keeps
    finding -- so the source of the number travels with it.
    """
    bare = {**STREAM_ROW, "date_window_days": None, "refetch_days": None}
    body = _body_of(_get(stream=bare))

    assert body["window_days"] == 3
    assert body["window_source"] == "defensive_default"

    # And a row that DOES declare one says so, or the flag above would be
    # indistinguishable from a constant.
    assert _body_of(_get())["window_source"] == "date_window_days"


def test_a_weekly_cadence_reports_the_floor_that_widened_it() -> None:
    """AI-217's floor is visible, not silently applied to what a person will pay."""
    weekly = {**STREAM_ROW, "schedule_mode": "weekly", "date_window_days": 2}
    body = _body_of(_get(stream=weekly))

    assert body["window_days"] == 7
    assert body["window_widened_for"] == "weekly"


def test_a_refusal_comes_back_200_WITH_the_gesture_that_releases_it() -> None:
    """"It will not run, and here is why" is a successful reading, not an error.

    A `422` here would make the console treat a knowable, actionable state as a
    failed request -- and the confirmation could not name it before the click.
    """
    stopped = {**STREAM_ROW, "enabled": False}
    body = _body_of(_get(stream=stopped))

    assert body["refusal"]["code"] == "not_armed"
    assert "Start it on the Schedule panel" in body["refusal"]["message"]
    # The window is STILL composed: a person who releases the gate needs to know
    # what they are about to release it for.
    assert body["date_from"] and body["date_to"]


def test_an_ARCHIVED_stream_is_told_to_be_RESTORED_not_started() -> None:
    """The two sentences the console used to be able to contradict -- 2026-08-18.

    `gate_refusal` read `lifecycle_state == 'archived'`, which nothing writes, so
    a real archive (`enabled = FALSE, archived_at` stamped) fell through to the
    stopped branch and was told to "start it on the Schedule panel" -- a panel
    which, for an archived Datastream, refuses to start anything and says to
    restore it instead.
    """
    archived = {
        **STREAM_ROW,
        "enabled": False,
        "lifecycle_state": "active",
        "archived_at": "2026-08-14T10:00:00+00:00",
    }
    body = _body_of(_get(stream=archived))

    assert body["refusal"]["code"] == "not_armed"
    assert "Restore it" in body["refusal"]["message"]
    assert "Schedule panel" not in body["refusal"]["message"]


def test_a_pushed_source_is_told_nothing_is_fetched_for_it() -> None:
    """A `managed_feed` has nothing to go and get, and the preview says so."""
    body = _body_of(_get(stream={**STREAM_ROW, "source_kind": "managed_feed"}))

    assert body["refusal"]["code"] == "pushed_source"


def test_a_missing_project_is_refused_rather_than_defaulted() -> None:
    """AD-5: the scope is required, never inferred from the id."""
    response = _get(project_id=None)

    assert response.status_code == 400
    assert _body_of(response)["code"] == "missing_param"


def test_an_unknown_datastream_answers_not_found() -> None:
    response = _get(stream=None)

    assert response.status_code == 404
    assert _body_of(response)["code"] == "not_found"


def test_the_preview_writes_nothing() -> None:
    """A lecture n'ecrit pas -- and here that is load-bearing.

    A preview that enqueued, or opened a run line, would spend on the gesture
    that exists to describe the spend. Asserted against the queue itself rather
    than by reading the handler.
    """
    from contextlib import ExitStack

    from core.datastream_collection_api import _preview_datastream_run

    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "core.admin_api._check_auth",
                new=AsyncMock(return_value=(True, "owner@example.com")),
            )
        )
        conn_ctx = stack.enter_context(patch("core.db.get_connection"))
        conn = MagicMock()
        conn_ctx.return_value.__enter__ = lambda _s: conn
        conn_ctx.return_value.__exit__ = MagicMock(return_value=False)
        stack.enter_context(
            patch("core.datastream_dispatch.load_dispatch_row", return_value=STREAM_ROW)
        )
        stack.enter_context(
            patch("core.admin_api._enforce_datastream_project_scope", return_value=None)
        )
        stack.enter_context(patch("core.scheduler.project_timezone", return_value="UTC"))
        stack.enter_context(
            patch("core.scheduler.project_yesterday", return_value=date(2026, 8, 17))
        )
        enqueue = stack.enter_context(patch("core.queue.enqueue_pull"))
        dispatch = stack.enter_context(patch("core.datastream_dispatch.dispatch_windows"))
        audit = stack.enter_context(
            patch("core.datastream_collection_api.write_audit_row")
        )

        response = _run(_preview_datastream_run(_request()))

    assert response.status_code == 200
    assert enqueue.call_count == 0
    assert dispatch.call_count == 0
    # No audit row either: reading what something would cost is not an act to
    # attribute, and an audit trail full of previews hides the runs in it.
    assert audit.call_count == 0
    assert conn.commit.call_count == 0
