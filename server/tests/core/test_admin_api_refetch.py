"""The re-collection route, and the only home its cases have -- story 58.4.

WHY THIS FILE EXISTS RATHER THAN A SECOND ONE. The route's cases lived in
`tests/core/test_extract_ledger.py`, which tests the extract REGISTRY: the
window grouping, the day verdicts, the row-count rules. A route and a registry
are two subjects, and the route half was invisible to anybody reading the file
name. Story 58.4 does not create a second home -- the cases MOVED here and this
is now the only one (arbitrage 8). `test_extract_ledger.py` keeps
`_group_dates_into_windows`, which is registry arithmetic and not the address.

WHAT IS PROVED HERE AND WHAT IS NOT. These are handler-level tests with doubles
for the connection, the queue and the scope guard: they prove the CONTRACT of
the answer -- which status, which code, which keys, which arguments reach
`enqueue_pull`. That the ADDRESS is mounted and that a real row comes back
through it is proved on Postgres, in
`tests/integration/test_datastream_daily_breakdown_api.py`, because a mocked
cursor can mount no route.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.responses import JSONResponse

PROJECT = "proj_EXAMPLE"
STREAM = "ds_EXAMPLE"
DAY = "2026-06-09"

#: One Datastream that passes every gate -- the DISPATCH row, which is what the
#: route reads since 2026-08-12. Every key of `DISPATCH_ROW_COLUMNS` is present
#: on purpose: `gate_refusal` answers `row_incomplete` for an absent one rather
#: than letting it pass, so a double that forgets a column fails loudly here
#: instead of proving a gate that never ran.
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
    """Run a coroutine on a FRESH event loop -- see `test_extract_ledger._run`."""
    return asyncio.run(coro)


def _request(*, path_params=None, body=None):
    """A minimal Starlette request for the handler.

    The project now travels in the PATH (story 58.4), so a request with no
    `project_id` path parameter is what an un-scoped caller looks like.
    """
    req = MagicMock()
    req.path_params = path_params if path_params is not None else {
        "project_id": PROJECT, "datastream_id": STREAM,
    }
    req.query_params = {}
    req.headers = {"authorization": "Bearer test-token"}

    payload = b"" if body is None else json.dumps(body).encode()

    async def _body():
        return payload

    req.body = _body
    return req


def _auth_ok():
    return patch(
        "core.admin_api._check_auth", new=AsyncMock(return_value=(True, "owner@example.com"))
    )


def _connection():
    """A connection double whose context manager yields a MagicMock cursor."""
    ctx = patch("core.db.get_connection")
    return ctx


class _Wired:
    """Every collaborator of the happy path, wired at once.

    Returned from `_wire()` so a test can assert on `enqueue`, `audit` and
    `scope` without repeating six `patch` lines that hide which one it is about.
    """

    def __init__(self, enqueue, audit, scope, active):
        self.enqueue = enqueue
        self.audit = audit
        self.scope = scope
        self.active = active


#: "the caller did not override this", which is NOT the same as `None` -- `None`
#: is precisely what `get_datastream` answers for a stream of another project,
#: and a default of `None` made that case silently take the happy path.
_DEFAULT = object()


def _wire(*, stream=_DEFAULT, job=None, scope_refusal=None, active_run=None):
    """The context manager stack of a handler call, with one job per window."""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(_auth_ok())

    conn_ctx = stack.enter_context(_connection())
    conn = MagicMock()
    conn_ctx.return_value.__enter__ = lambda _s: conn
    conn_ctx.return_value.__exit__ = MagicMock(return_value=False)

    stack.enter_context(
        patch(
            "core.datastream_dispatch.load_dispatch_row",
            return_value=STREAM_ROW if stream is _DEFAULT else stream,
        )
    )
    scope = stack.enter_context(
        patch(
            "core.admin_api._enforce_datastream_project_scope",
            return_value=scope_refusal,
        )
    )
    audit = stack.enter_context(patch("core.datastream_collection_api.write_audit_row"))
    # A refetch opens a RUN (story 63.1) and this file is not about that: the
    # instrumentation answers None, which is the state story 58.4 explains.
    stack.enter_context(
        patch("core.datastream_collection_api._open_refetch_run", return_value=None)
    )
    stack.enter_context(patch("core.datastream_collection_api._close_refetch_run"))
    active = stack.enter_context(
        patch("core.datastream_collection_api._read_active_run", return_value=active_run)
    )

    counter = {"n": 0}

    def _enqueue(_ref, date_from, date_to, **_kwargs):
        counter["n"] += 1
        return job or {
            "job_id": f"job_{counter['n']}",
            "pull_id": f"pull_{counter['n']}",
            "state": "queued",
        }

    enqueue = stack.enter_context(patch("core.queue.enqueue_pull", side_effect=_enqueue))
    return stack, _Wired(enqueue, audit, scope, active)


def _post(*, path_params=None, body=None, **wiring):
    """Call the handler and hand back (response, collaborators)."""
    from core.datastream_collection_api import _refetch_datastream

    stack, wired = _wire(**wiring)
    with stack:
        response = _run(_refetch_datastream(_request(path_params=path_params, body=body)))
    return response, wired


def _body_of(response):
    return json.loads(response.body)


# ---------------------------------------------------------------------------
# One day, one window, one job.
# ---------------------------------------------------------------------------


def test_a_single_day_is_one_window_and_one_job() -> None:
    """The whole gesture of 58.4: one row of the grid, one pull window."""
    response, wired = _post(body={"dates": [DAY]})

    assert response.status_code == 202
    payload = _body_of(response)
    assert len(payload["jobs"]) == 1
    job = payload["jobs"][0]
    assert (job["date_from"], job["date_to"]) == (DAY, DAY)
    assert job["state"] == "queued"
    # The window really is a day, at the seam that spends the quota.
    args, kwargs = wired.enqueue.call_args
    assert args[1] == DAY and args[2] == DAY
    assert kwargs["datastream_id"] == STREAM


def test_no_cost_travels_on_the_answer() -> None:
    """`Not measured` is the console's word BECAUSE the route has no figure.

    A key carrying points, bytes or currency would be a number nobody computes:
    `quota.state_dict()` publishes `open|closed` and its counter lives in a
    per-process singleton that no route reaches.
    """
    response, _ = _post(body={"dates": [DAY]})

    flat = json.dumps(_body_of(response))
    for forbidden in ("budget_points", "read_cost", "write_cost", "quota", "cost",
                      "bytes", "eur", "usd", "estimated_spend"):
        assert forbidden not in flat.lower(), forbidden


def test_a_window_the_queue_refused_keeps_its_code_and_its_sentence() -> None:
    """`enqueue_pull` can answer a refusal shaped like a job, and it must travel.

    An unauthorized account scope comes back as `{state: "refused", code,
    message}` -- a state `app.pull_jobs.state` may not hold. This route kept the
    state and dropped both other keys, so the console counted the entry like any
    other window and announced a spend that never happened, at the exact moment a
    person most needs the real answer.
    """
    refusal = {
        "state": "refused",
        "code": "access_denied",
        "message": "connection/account scope is not authorized for this resource",
    }
    response, _ = _post(body={"dates": [DAY]}, job=refusal)

    assert response.status_code == 202
    entry = _body_of(response)["jobs"][0]
    assert entry["code"] == "access_denied"
    assert entry["message"] == refusal["message"]
    # AND NO JOB ID. Copying `job.get("job_id")` blindly published `job_id: null`
    # on an entry that is not a job at all -- which reads as a queued window whose
    # id failed to load, not as a refusal.
    assert "job_id" not in entry
    assert "pull_id" not in entry


def test_a_real_window_still_carries_its_job_and_pull_ids() -> None:
    """The other half of the same rule: a queued window loses nothing."""
    entry = _body_of(_post(body={"dates": [DAY]})[0])["jobs"][0]

    assert entry["job_id"] and entry["pull_id"]
    assert entry["state"] == "queued"


def test_a_moved_window_carries_the_clamp_that_moved_it() -> None:
    """Story 34.3 clamps rather than rejects, and says the caller surfaces it.

    The trial ceiling raises `date_from` to `today - max_backfill_days`. This
    route dropped the key, so a person re-collecting a day older than their
    ceiling was told the day was queued while `app.pull_jobs` held a different
    window -- an inverted one, since the clamp never looks at `date_to`.
    """
    clamp = {"clamped": True, "date_from": "2026-07-07", "max_backfill_days": 30}
    response, _ = _post(
        body={"dates": [DAY]},
        job={"job_id": "job_1", "pull_id": "pull_1", "state": "queued",
             "backfill_clamp": clamp},
    )

    assert _body_of(response)["jobs"][0]["backfill_clamp"] == clamp


def test_a_deduplicated_window_says_so_rather_than_reading_as_a_second_spend() -> None:
    response, _ = _post(
        body={"dates": [DAY]},
        job={"job_id": "job_1", "pull_id": "pull_1", "state": "queued",
             "deduplicated": True},
    )

    assert _body_of(response)["jobs"][0]["deduplicated"] is True


def test_contiguous_days_become_one_window_and_a_gap_splits_them() -> None:
    response, _ = _post(body={"dates": ["2026-06-09", "2026-06-10"]})
    assert len(_body_of(response)["jobs"]) == 1

    response, _ = _post(body={"dates": ["2026-06-09", "2026-06-11"]})
    assert len(_body_of(response)["jobs"]) == 2


def test_a_range_is_expanded_to_days_then_grouped() -> None:
    response, wired = _post(body={"from": "2026-06-09", "to": "2026-06-11"})

    assert response.status_code == 202
    assert len(_body_of(response)["jobs"]) == 1
    assert wired.enqueue.call_args[0][1:3] == ("2026-06-09", "2026-06-11")


def test_every_window_writes_its_audit_row() -> None:
    _post(body={"dates": ["2026-06-09", "2026-06-11"]})
    # Two windows, two rows: an audit line per spend, not one per request.
    assert _post(body={"dates": ["2026-06-09", "2026-06-11"]})[1].audit.call_count == 2


# ---------------------------------------------------------------------------
# A run already holds the Datastream -- said, never refused.
# ---------------------------------------------------------------------------


def test_a_held_datastream_is_still_collected_and_the_answer_says_who_holds_it() -> None:
    """Jean, 2026-08-06: the console SAYS a collection is running, and lets go.

    `_open_refetch_run` answered None, so this re-collection owns no run. The
    windows are still enqueued -- refusing here would trade the collection for
    its instrumentation -- and the answer names the run that holds the lock so
    the missing progress line has a reason a person can read.
    """
    held = {
        "execution_id": "dse_EXAMPLE",
        "state": "loading",
        "since": None,
        "code": "collection_already_running",
        "message": "A collection is already running on this Datastream.",
    }
    response, wired = _post(body={"dates": [DAY]}, active_run=held)

    assert response.status_code == 202, "a held Datastream must never be a refusal"
    payload = _body_of(response)
    assert wired.enqueue.call_count == 1, "the pull is the point; it was not enqueued"
    assert payload["active_run"]["execution_id"] == "dse_EXAMPLE"
    assert payload["active_run"]["message"] == (
        "A collection is already running on this Datastream."
    )
    # And no run of its own: that absence is exactly what `active_run` explains.
    assert "execution_id" not in payload


def test_nothing_is_claimed_when_no_run_holds_the_datastream() -> None:
    """Two causes answer None, and only one of them is measurable.

    `open_collection_run` returns None for a concurrent execution AND for a
    Datastream with no usable plan/mapping version. When the row read finds no
    active run, the answer stays silent rather than naming the other cause.
    """
    response, _ = _post(body={"dates": [DAY]}, active_run=None)

    payload = _body_of(response)
    assert "active_run" not in payload
    assert "execution_id" not in payload


# ---------------------------------------------------------------------------
# The refusals.
# ---------------------------------------------------------------------------


def test_401_without_a_token() -> None:
    from core.datastream_collection_api import _refetch_datastream

    with patch("core.admin_api._check_auth", new=AsyncMock(return_value=(False, ""))):
        response = _run(_refetch_datastream(_request(body={"dates": [DAY]})))
    assert response.status_code == 401


def test_400_without_a_project_in_the_path() -> None:
    """The project moved to the path, so an un-scoped call is a missing field."""
    response, _ = _post(path_params={"datastream_id": STREAM}, body={"dates": [DAY]})
    assert response.status_code == 400
    assert _body_of(response)["code"] == "missing_field"


def test_400_without_any_date() -> None:
    response, _ = _post(body={})
    assert response.status_code == 400
    assert _body_of(response)["code"] == "missing_field"

    response, _ = _post(body={"dates": []})
    assert response.status_code == 400
    assert _body_of(response)["code"] == "missing_field"


def test_400_on_a_date_outside_the_format() -> None:
    response, _ = _post(body={"dates": ["09/06/2026"]})
    assert response.status_code == 400
    assert _body_of(response)["code"] == "invalid_date"


def test_400_on_an_inverted_range() -> None:
    response, _ = _post(body={"from": "2026-06-11", "to": "2026-06-09"})
    assert response.status_code == 400
    assert _body_of(response)["code"] == "invalid_date"


def test_400_past_the_365_day_cap() -> None:
    from datetime import date, timedelta

    start = date(2026, 1, 1)
    dates = [(start + timedelta(days=n)).isoformat() for n in range(366)]
    response, _ = _post(body={"dates": dates})
    assert response.status_code == 400
    assert _body_of(response)["code"] == "too_many_dates"


def test_404_for_a_stream_of_another_project() -> None:
    """`get_datastream(id, project_id)` answers None -- one envelope for both.

    A distinct refusal for "exists elsewhere" would teach a caller that the
    stream exists, which is the enumeration oracle the scope guard refuses.
    """
    response, wired = _post(stream=None, body={"dates": [DAY]})
    # `stream=None` means the double returns None from get_datastream.
    assert response.status_code == 404
    assert _body_of(response)["code"] == "not_found"
    assert wired.enqueue.call_count == 0


def test_422_when_the_stream_is_linked_to_no_connection() -> None:
    response, wired = _post(
        stream={**STREAM_ROW, "connection_ref_id": None}, body={"dates": [DAY]}
    )
    assert response.status_code == 422
    body = _body_of(response)
    assert body["code"] == "not_configured"
    assert "Link a connection" in body["message"], "a refusal names the gesture"
    assert wired.enqueue.call_count == 0


def test_a_versioned_stream_is_COLLECTED_and_no_longer_turned_away() -> None:
    """The refusal this case used to assert was the reason nothing ever ran.

    `422 dispatch_not_available` fired on every Datastream carrying a plan
    version -- and `_open_refetch_run` needs one, so the two conditions excluded
    each other and no re-collection could ever have a run line. Measured on the
    live base the day it was retired: **0** rows of `app.pull_jobs` carried an
    `execution_id`, and of 89 Datastreams exactly one was active, enabled and
    mapped. Now the gates of `core.datastream_dispatch` answer instead, and a
    published stream is precisely the one that passes them.
    """
    response, wired = _post(body={"dates": [DAY]})

    assert response.status_code == 202
    assert wired.enqueue.call_count == 1


def test_a_STOPPED_stream_is_still_re_collectable_because_that_is_the_repair() -> None:
    """`require_armed=False` on this door only.

    Stopping a Datastream loses days that "only come back through a day-by-day
    re-collection" (ratified surface). Refusing the re-collection because the
    stream is stopped would refuse the exact repair the screen offers.
    """
    response, wired = _post(
        stream={**STREAM_ROW, "enabled": False, "lifecycle_state": "paused"},
        body={"dates": [DAY]},
    )

    assert response.status_code == 202
    assert wired.enqueue.call_count == 1


def test_an_ARCHIVED_stream_is_refused_at_every_door() -> None:
    """The one lifecycle state `require_armed=False` never relaxes."""
    response, wired = _post(
        stream={**STREAM_ROW, "lifecycle_state": "archived"}, body={"dates": [DAY]}
    )

    assert response.status_code == 422
    body = _body_of(response)
    assert body["code"] == "not_armed"
    assert "Restore it" in body["message"]
    assert wired.enqueue.call_count == 0


def test_the_archive_THE_PRODUCT_WRITES_is_refused_and_not_only_the_word() -> None:
    """The shape a real archive leaves behind -- 2026-08-18.

    The test above passed for months against a row that CANNOT EXIST.
    `core.datastreams.delete_datastream` soft-archives with `enabled = FALSE,
    archived_at = NOW(), archived_by = ...` and never touches `lifecycle_state`;
    `grep -rn "lifecycle_state.*=.*'archived'" server/` finds no writer. So the
    gate above guarded a value nothing produces, while the row the product really
    writes fell through to the `enabled` branch -- the one branch
    `require_armed=False` deliberately relaxes for this door. An archived
    Datastream could be re-collected, and it would have called the provider and
    spent on the account.

    This case is the archive as the database holds it: the lifecycle word still
    says `active`, because that is exactly what makes the defect invisible.
    """
    response, wired = _post(
        stream={
            **STREAM_ROW,
            "enabled": False,
            "lifecycle_state": "active",
            "archived_at": "2026-08-14T10:00:00+00:00",
        },
        body={"dates": [DAY]},
    )

    assert response.status_code == 422
    body = _body_of(response)
    assert body["code"] == "not_armed"
    # And it names the gesture that releases it, which is the restore -- never
    # "start it on the Schedule panel", which that panel itself refuses to do.
    assert "Restore it" in body["message"]
    assert wired.enqueue.call_count == 0


def test_an_INACTIVE_connection_is_refused_before_a_provider_is_called() -> None:
    """A gate the clock always applied and this door never did."""
    response, wired = _post(
        stream={**STREAM_ROW, "cr_status": "revoked"}, body={"dates": [DAY]}
    )

    assert response.status_code == 422
    assert _body_of(response)["code"] == "connection_inactive"
    assert wired.enqueue.call_count == 0


def test_a_read_only_registration_has_nothing_to_collect() -> None:
    """Story 12.7, applied at the door instead of only in the dispatcher's SQL."""
    response, wired = _post(
        stream={**STREAM_ROW, "source_kind": "external_bq"}, body={"dates": [DAY]}
    )

    assert response.status_code == 422
    assert _body_of(response)["code"] == "read_only_source"
    assert wired.enqueue.call_count == 0


def test_a_viewer_may_not_spend_the_provider_quota() -> None:
    """`member` is the role that may START a run, and the guard is asked for it.

    Both halves matter: the refusal the guard produced is what the caller gets
    (nothing is enqueued), and the MINIMUM ROLE asked for is `member` -- a route
    that asked for `viewer` would let a read-only identity spend quota and would
    still pass a test that only checked the refusal was honoured.
    """
    refusal = JSONResponse({"code": "not_found", "message": "Datastream not found"}, 404)
    response, wired = _post(body={"dates": [DAY]}, scope_refusal=refusal)

    assert response is refusal
    assert wired.enqueue.call_count == 0
    assert wired.scope.call_args.kwargs["minimum_role"] == "member"
    assert wired.scope.call_args.kwargs["claimed_project_id"] == PROJECT
