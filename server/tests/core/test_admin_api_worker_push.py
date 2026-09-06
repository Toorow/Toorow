"""POST /internal/worker/execute-pull/{job_id} -- the Cloud Tasks push target.

Story 56.2 (AD-36). What these tests actually protect is the STATUS CODE, because
Cloud Tasks reads it as an instruction and not as information: a wrong code turns
a finished pull into an endless retry, or drops work that should have come back.

The endpoint function is exercised directly rather than through the ASGI
TestClient: the seam suite costs 30-45 s per test, and nothing here needs a real
transport. Routing is covered separately, against the real router, in
test_queue.py.
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import patch

from core import (
    admin_api,
    connection_revocation,  # AD-43 : le handler vit chez son sujet
    internal_api,  # AD-43 : le handler vit chez son sujet
)
from starlette.requests import Request

# AI-127: these helpers used to patch `_check_internal_auth`, a function with no
# caller left in the module -- the patch was inert, and the tests passed through
# whatever the real gate happened to allow. They now authorize the way a machine
# caller actually does: the shared secret, presented as a header, checked by the
# real `_authorize_internal`. Nothing is patched, so the day an endpoint stops
# calling the gate these turn red instead of staying green.
_SECRET = "test-internal-secret"


def _request(job_id: str, path: str = "/internal/worker/execute-pull") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"{path}/{job_id}",
            "path_params": {"job_id": job_id},
            "headers": [(b"x-internal-auth", _SECRET.encode())],
            "query_string": b"",
        }
    )


def _as_platform():
    return patch.dict(os.environ, {"INTERNAL_ENDPOINTS_REQUIRE_HEADER": _SECRET})


def _call(job_id: str = "job_01TEST"):
    with _as_platform():
        resp = asyncio.run(internal_api._execute_pull_internal(_request(job_id)))
    return resp.status_code, json.loads(bytes(resp.body).decode())


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _no_db():
    return patch("core.db.get_connection", lambda *a, **k: _Conn())


def test_claimed_and_executed_answers_200_terminal():
    """A job that ran is finished business: the task must not come back."""
    with (
        _no_db(),
        patch("core.queue.claim_job_by_id", return_value={"id": "job_01TEST"}),
        patch("core.queue.execute_claimed_job", return_value=True),
        patch("core.queue.get_job_status", return_value={"state": "done"}),
    ):
        status, body = _call()
    assert status == 200
    assert body["executed"] is True
    assert body["state"] == "done"


def test_a_failed_pull_is_not_a_failed_task():
    """The pull failed, the JOB is terminal -- answering 5xx would re-run it.

    `_execute_job` records the failure on the row and returns True. Cloud Tasks
    has its own retry policy; stacking it on top of the queue's attempt_count
    would multiply attempts against the provider.
    """
    with (
        _no_db(),
        patch("core.queue.claim_job_by_id", return_value={"id": "job_01TEST"}),
        patch("core.queue.execute_claimed_job", return_value=True),
        patch("core.queue.get_job_status", return_value={"state": "failed"}),
    ):
        status, body = _call()
    assert status == 200
    assert body["state"] == "failed"


def test_quota_blocked_answers_429_so_the_task_comes_back():
    """The one case where a retry is the right answer.

    `_execute_job` returns the job to 'queued' WITHOUT spending an attempt, so
    the work still has to happen -- later.
    """
    with (
        _no_db(),
        patch("core.queue.claim_job_by_id", return_value={"id": "job_01TEST"}),
        patch("core.queue.execute_claimed_job", return_value=False),
    ):
        status, body = _call()
    assert status == 429
    assert body["code"] == "quota_blocked"


def test_already_terminal_is_200_and_does_not_re_execute():
    """At-least-once delivery must not become at-least-once PULLING.

    The claim refuses anything that is not 'queued'. Without this, a redelivered
    task re-pulls a window that already landed -- and the append-only zone would
    HIDE the duplicate (supersede by pull_id) rather than reveal it.
    """
    executed = []
    with (
        _no_db(),
        patch("core.queue.claim_job_by_id", return_value=None),
        patch("core.queue.get_job_status", return_value={"state": "done"}),
        patch("core.queue.execute_claimed_job", side_effect=lambda j: executed.append(j)),
    ):
        status, body = _call()
    assert status == 200
    assert body["code"] == "already_terminal"
    assert executed == [], "a redelivered task re-ran a terminal job"


def test_already_running_answers_409_not_a_second_execution():
    """A live claim is not a reason to run the job twice.

    A genuinely stuck claim is released by recover_stale_running_jobs, which is
    the mechanism that exists for it.
    """
    with (
        _no_db(),
        patch("core.queue.claim_job_by_id", return_value=None),
        patch("core.queue.get_job_status", return_value={"state": "running"}),
    ):
        status, body = _call()
    assert status == 409
    assert body["code"] == "already_running"


def test_unknown_job_is_200_so_the_task_stops():
    """An id the ledger does not know cannot become work by being retried."""
    with (
        _no_db(),
        patch("core.queue.claim_job_by_id", return_value=None),
        patch("core.queue.get_job_status", return_value=None),
    ):
        status, body = _call()
    assert status == 200
    assert body["code"] == "unknown_job"


def test_an_unreadable_ledger_is_a_retry_not_a_drop():
    """Losing the task would lose the operation; the row is the operation."""
    with (
        _no_db(),
        patch("core.queue.claim_job_by_id", side_effect=RuntimeError("db down")),
    ):
        status, body = _call()
    assert status == 503
    assert body["code"] == "claim_failed"


def test_a_crash_mid_execution_retries_rather_than_stranding_the_work():
    with (
        _no_db(),
        patch("core.queue.claim_job_by_id", return_value={"id": "job_01TEST"}),
        patch("core.queue.execute_claimed_job", side_effect=RuntimeError("boom")),
    ):
        status, body = _call()
    assert status == 503
    assert body["code"] == "execution_error"


def test_missing_job_id_is_refused():
    with _no_db():
        status, body = _call("")
    assert status == 400
    assert body["code"] == "missing_id"


# ---------------------------------------------------------------------------
# Story 56.3 -- the SECOND queue: setup preview + candidate materialisation.
#
# Its only drainer was the daemon loop, so a push deployment without this target
# would stop materialising candidates and hang the Datastream wizard in silence.
# ---------------------------------------------------------------------------


def _call_activation(job_id: str = "dsaj_01TEST"):
    request = _request(job_id, path="/internal/worker/execute-activation")
    with _as_platform():
        resp = asyncio.run(internal_api._execute_activation_internal(request))
    return resp.status_code, json.loads(bytes(resp.body).decode())


def test_activation_executed_answers_200():
    with (
        _no_db(),
        patch("core.queue.claim_activation_job_by_id", return_value={"id": "dsaj_01TEST"}),
        patch("core.queue.execute_claimed_activation_job", return_value=None),
        patch("core.queue.get_activation_job_state", return_value="done"),
    ):
        status, body = _call_activation()
    assert status == 200
    assert body["state"] == "done"


def test_activation_failure_with_attempts_left_is_429():
    """This queue retries IN PLACE: 'failed' is claimable again under the ceiling.

    So a failure that still has attempts is work that must come back -- unlike
    the pull queue, where a failed job is terminal for its task.
    """
    with (
        _no_db(),
        patch("core.queue.claim_activation_job_by_id", return_value={"id": "dsaj_01TEST"}),
        patch("core.queue.execute_claimed_activation_job", return_value=None),
        patch("core.queue.get_activation_job_state", return_value="failed"),
    ):
        status, body = _call_activation()
    assert status == 429
    assert body["code"] == "retryable_failure"


def test_activation_dead_letter_is_200_and_stops():
    with (
        _no_db(),
        patch("core.queue.claim_activation_job_by_id", return_value={"id": "dsaj_01TEST"}),
        patch("core.queue.execute_claimed_activation_job", return_value=None),
        patch("core.queue.get_activation_job_state", return_value="dead_letter"),
    ):
        status, body = _call_activation()
    assert status == 200
    assert body["state"] == "dead_letter"


def test_activation_already_running_is_409():
    with (
        _no_db(),
        patch("core.queue.claim_activation_job_by_id", return_value=None),
        patch("core.queue.get_activation_job_state", return_value="running"),
    ):
        status, _body = _call_activation()
    assert status == 409


def test_activation_unknown_job_is_200():
    with (
        _no_db(),
        patch("core.queue.claim_activation_job_by_id", return_value=None),
        patch("core.queue.get_activation_job_state", return_value=None),
    ):
        status, body = _call_activation()
    assert status == 200
    assert body["code"] == "unknown_job"


def test_both_push_targets_are_routed():
    """The route table, not the string that built the URL (the AI-97 lesson)."""
    from core.admin_api import router
    from starlette.routing import Match

    def routed(path: str) -> bool:
        scope = {"type": "http", "method": "POST", "path": path,
                 "path_params": {}, "headers": [], "root_path": ""}
        return any(r.matches(scope)[0] is Match.FULL for r in router.routes)

    assert routed("/internal/worker/execute-pull/job_01X")
    assert routed("/internal/worker/execute-activation/dsaj_01X")
    assert not routed("/internal/worker/execute-nothing/job_01X")


def test_activation_dispatch_is_a_no_op_on_the_local_backend():
    """A dev machine must not need a Cloud Tasks project to enqueue setup work.

    The polling worker is the dispatcher under QUEUE_BACKEND=local; creating a
    task there would either fail on a missing project or address a queue nobody
    drains.
    """
    import os

    from core.queue import dispatch_activation_task

    previous = os.environ.get("QUEUE_BACKEND")
    os.environ["QUEUE_BACKEND"] = "local"
    try:
        assert dispatch_activation_task("dsaj_01TEST") is False
    finally:
        if previous is None:
            os.environ.pop("QUEUE_BACKEND", None)
        else:
            os.environ["QUEUE_BACKEND"] = previous


def test_activation_dispatch_failure_never_raises():
    """The row is committed; a lost task costs latency, not the operation."""
    import os

    from core.queue import dispatch_activation_task

    previous = os.environ.get("QUEUE_BACKEND")
    os.environ["QUEUE_BACKEND"] = "cloud_tasks"
    try:
        with patch("core.queue._create_push_task", side_effect=RuntimeError("no project")):
            assert dispatch_activation_task("dsaj_01TEST") is False
    finally:
        if previous is None:
            os.environ.pop("QUEUE_BACKEND", None)
        else:
            os.environ["QUEUE_BACKEND"] = previous


# ---------------------------------------------------------------------------
# Story 56.4 -- the reconciliation sweep, and story 56.5 -- the clock.
# ---------------------------------------------------------------------------


def _call_endpoint(endpoint, path: str, *, headers=None):
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "path_params": {},
            "headers": headers or [],
            "query_string": b"",
        }
    )
    resp = asyncio.run(endpoint(request))
    return resp.status_code, json.loads(bytes(resp.body).decode())


def test_reconcile_re_dispatches_and_never_executes():
    """The line between a reconciliation and a second worker (criterion 9)."""
    executed = []
    dispatched = []
    with (
        patch.object(admin_api, "_authorize_internal", _authorized),
        patch("core.queue.reconcile_pending_tasks",
              return_value={"pull_redispatched": 2, "activation_redispatched": 1,
                            "backend": "cloud_tasks"}),
        patch("core.queue.execute_claimed_job", side_effect=lambda j: executed.append(j)),
        patch("core.queue.execute_claimed_activation_job",
              side_effect=lambda j: executed.append(j)),
        patch("core.queue.dispatch_pull_task", side_effect=lambda *a, **k: dispatched.append(a)),
    ):
        status, body = _call_endpoint(
            internal_api._reconcile_queues_internal, "/internal/scheduler/reconcile-queues"
        )
    assert status == 200
    assert body["pull_redispatched"] == 2
    assert executed == [], "the sweep executed a job -- it has become a second worker"


def test_reconcile_is_a_no_op_on_the_local_backend():
    import os

    from core.queue import reconcile_pending_tasks

    previous = os.environ.get("QUEUE_BACKEND")
    os.environ["QUEUE_BACKEND"] = "local"
    try:
        result = reconcile_pending_tasks()
    finally:
        if previous is None:
            os.environ.pop("QUEUE_BACKEND", None)
        else:
            os.environ["QUEUE_BACKEND"] = previous
    assert result == {"pull_redispatched": 0, "activation_redispatched": 0, "backend": "local"}


async def _authorized(_request):
    return None


def test_internal_endpoints_accept_the_platform_without_a_user_token():
    """Cloud Scheduler carries no Bearer token: the two mechanisms are alternatives.

    Requiring BOTH would have answered 401 to every scheduled call -- a
    misconfiguration that reads exactly like a broken scheduler.
    """
    import os

    previous = os.environ.get("INTERNAL_ENDPOINTS_REQUIRE_HEADER")
    os.environ["INTERNAL_ENDPOINTS_REQUIRE_HEADER"] = "s3cret"
    try:
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/internal/scheduler/reconcile-queues",
                "path_params": {},
                "headers": [(b"x-internal-auth", b"s3cret")],
                "query_string": b"",
            }
        )
        # No _check_auth patch: if the conjunction came back, this would 401.
        assert asyncio.run(admin_api._authorize_internal(request)) is None
    finally:
        if previous is None:
            os.environ.pop("INTERNAL_ENDPOINTS_REQUIRE_HEADER", None)
        else:
            os.environ["INTERNAL_ENDPOINTS_REQUIRE_HEADER"] = previous


def test_push_mode_without_a_secret_is_503_not_401():
    """A config error that lets the task come back, rather than one that drops it."""
    import os

    prev_secret = os.environ.pop("INTERNAL_ENDPOINTS_REQUIRE_HEADER", None)
    prev_backend = os.environ.get("QUEUE_BACKEND")
    os.environ["QUEUE_BACKEND"] = "cloud_tasks"
    try:
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/internal/scheduler/dispatch-nightly",
                "path_params": {},
                "headers": [],
                "query_string": b"",
            }
        )
        resp = asyncio.run(admin_api._authorize_internal(request))
        assert resp is not None
        assert resp.status_code == 503
    finally:
        if prev_secret is not None:
            os.environ["INTERNAL_ENDPOINTS_REQUIRE_HEADER"] = prev_secret
        if prev_backend is None:
            os.environ.pop("QUEUE_BACKEND", None)
        else:
            os.environ["QUEUE_BACKEND"] = prev_backend


def test_the_scheduler_thread_stands_down_under_the_push_backend():
    """Two dispatchers for one clock is worse than none."""
    import os
    import threading

    from core import scheduler

    prev_backend = os.environ.get("QUEUE_BACKEND")
    prev_enabled = os.environ.get("SCHEDULER_ENABLED")
    os.environ["QUEUE_BACKEND"] = "cloud_tasks"
    os.environ["SCHEDULER_ENABLED"] = "true"
    try:
        before = {t.name for t in threading.enumerate()}
        scheduler.start_nightly_scheduler()
        after = {t.name for t in threading.enumerate()}
    finally:
        for key, value in (("QUEUE_BACKEND", prev_backend), ("SCHEDULER_ENABLED", prev_enabled)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    assert "nightly-scheduler" not in (after - before)


def test_the_scheduler_endpoints_are_routed():
    from core.admin_api import router
    from starlette.routing import Match

    def routed(path: str) -> bool:
        scope = {"type": "http", "method": "POST", "path": path,
                 "path_params": {}, "headers": [], "root_path": ""}
        return any(r.matches(scope)[0] is Match.FULL for r in router.routes)

    assert routed("/internal/scheduler/dispatch-nightly")
    assert routed("/internal/scheduler/dispatch-hourly")
    assert routed("/internal/scheduler/reconcile-queues")


# ---------------------------------------------------------------------------
# Story 56.6 -- health is checked when it CHANGES, not when a thread wakes up.
# ---------------------------------------------------------------------------


def test_a_re_consent_refreshes_health_immediately():
    """The defect this closes: `revoked` shown on a connection reconnected 8h30 ago.

    Measured 2026-07-31 in production -- connection_health.last_checked_at=11:10
    while connection_ref.updated_at=19:47 the same day.
    """
    written = {}

    class _Health:
        status = "ok"
        last_fetched_at = None

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, *_a, **_k):
            return None

        def fetchone(self):
            return (None, "google")

    class _C(_Conn):
        def cursor(self):
            return _Cur()

    with (
        patch("core.db.get_connection", lambda *a, **k: _C()),
        patch("core.nango_client.poll_connection_health", return_value=_Health()),
        patch("core.health_poller._upsert_health",
              side_effect=lambda **kw: written.update(kw)),
    ):
        assert connection_revocation._refresh_connection_health_now("conn_01TEST") is True

    assert written["conn_ref_id"] == "conn_01TEST"
    assert written["status"] == "ok"


def test_a_failing_health_refresh_never_breaks_the_flow_that_triggered_it():
    with patch("core.db.get_connection", side_effect=RuntimeError("db down")):
        assert connection_revocation._refresh_connection_health_now("conn_01TEST") is False


def test_the_health_poller_thread_stands_down_under_the_push_backend():
    import os
    import threading

    from core import health_poller

    prev = os.environ.get("QUEUE_BACKEND")
    prev_enabled = os.environ.get("HEALTH_POLLER_ENABLED")
    os.environ["QUEUE_BACKEND"] = "cloud_tasks"
    os.environ["HEALTH_POLLER_ENABLED"] = "true"
    try:
        before = {t.name for t in threading.enumerate()}
        health_poller.start_health_poller()
        after = {t.name for t in threading.enumerate()}
    finally:
        for key, value in (("QUEUE_BACKEND", prev), ("HEALTH_POLLER_ENABLED", prev_enabled)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    assert "health-poller" not in (after - before)


def test_the_health_sweep_endpoint_is_routed():
    from core.admin_api import router
    from starlette.routing import Match

    scope = {"type": "http", "method": "POST", "path": "/internal/scheduler/poll-health",
             "path_params": {}, "headers": [], "root_path": ""}
    assert any(r.matches(scope)[0] is Match.FULL for r in router.routes)


# ---------------------------------------------------------------------------
# Story 56.7 -- facts leave the outbox they have never left.
# ---------------------------------------------------------------------------


def test_no_topic_configured_is_not_an_error():
    """Every dev machine, and this suite. Failing would grow the outbox forever."""
    import os

    from core.events import publish_fact

    previous = os.environ.pop("PUBSUB_TOPIC", None)
    try:
        assert publish_fact("pull.landed", {"pull_id": "pull_1"}) is False
    finally:
        if previous is not None:
            os.environ["PUBSUB_TOPIC"] = previous


def test_a_fact_that_cannot_be_published_is_not_dropped():
    """The row is the ledger here too: it stays pending, with a pushed-out retry."""
    statements = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, sql, params=None):
            statements.append((" ".join(sql.split()), params))

        def fetchall(self):
            return [("ob_1", "datastream.published", {"a": 1}, 0)]

    class _C(_Conn):
        def cursor(self):
            return _Cur()

        def commit(self):
            return None

    from core.events import drain_outbox

    with (
        patch("core.db.get_connection", lambda *a, **k: _C()),
        patch("core.events.publish_fact", side_effect=RuntimeError("topic refused")),
    ):
        result = drain_outbox()

    assert result["failed"] == 1
    assert not any("delivered" in sql for sql, _ in statements), "a refused fact was marked sent"
    assert any("attempts = attempts + 1" in sql for sql, _ in statements)


def test_a_published_fact_is_marked_delivered():
    statements = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, sql, params=None):
            statements.append(" ".join(sql.split()))

        def fetchall(self):
            return [("ob_1", "datastream.published", {"a": 1}, 0)]

    class _C(_Conn):
        def cursor(self):
            return _Cur()

        def commit(self):
            return None

    from core.events import drain_outbox

    with (
        patch("core.db.get_connection", lambda *a, **k: _C()),
        patch("core.events.publish_fact", return_value=True),
    ):
        result = drain_outbox()

    assert result["published"] == 1
    assert any("state='delivered'" in sql for sql in statements)


def test_the_outbox_drain_endpoint_is_routed():
    from core.admin_api import router
    from starlette.routing import Match

    scope = {"type": "http", "method": "POST", "path": "/internal/scheduler/drain-outbox",
             "path_params": {}, "headers": [], "root_path": ""}
    assert any(r.matches(scope)[0] is Match.FULL for r in router.routes)


def test_a_contended_claim_is_not_reported_as_terminal():
    """`SKIP LOCKED` skips a row another delivery is claiming RIGHT NOW.

    That transaction's move to 'running' is invisible here until it commits, so
    the state still reads 'queued'. Calling that "already_terminal" put a state
    of 'queued' under a code saying the opposite -- the exact sentence an
    operator would read while debugging a stuck queue.
    """
    with (
        _no_db(),
        patch("core.queue.claim_job_by_id", return_value=None),
        patch("core.queue.get_job_status", return_value={"state": "queued"}),
    ):
        status, body = _call()
    assert status == 409
    assert body["code"] == "claim_contended"
    assert body["state"] == "queued"


def test_a_contended_activation_claim_is_not_reported_as_terminal():
    with (
        _no_db(),
        patch("core.queue.claim_activation_job_by_id", return_value=None),
        patch("core.queue.get_activation_job_state", return_value="failed"),
    ):
        status, body = _call_activation()
    assert status == 409
    assert body["code"] == "claim_contended_or_exhausted"


# ---------------------------------------------------------------------------
# Story 56.7 -- the subscribers. `pull.landed` finally has listeners.
# ---------------------------------------------------------------------------


def _push_envelope(payload: dict) -> bytes:
    import base64

    return json.dumps(
        {"message": {"data": base64.b64encode(json.dumps(payload).encode()).decode()}}
    ).encode()


def _call_fact(endpoint, body: bytes):
    from starlette.requests import Request as _Request

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = _Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/internal/facts/pull-landed/x",
            "path_params": {},
            "headers": [],
            "query_string": b"",
        },
        receive,
    )
    with patch("core.admin_api._authorize_internal", _authorized):
        resp = asyncio.run(endpoint(request))
    return resp.status_code, json.loads(bytes(resp.body).decode())


def test_a_fact_reaches_its_consumer():
    from core import facts_api

    seen = []
    def _record(payload):
        seen.append(payload)
        return "seeded"

    with patch("core.facts_api.run_context_seed_for", side_effect=_record):
        status, body = _call_fact(
            facts_api._context_seed_push,
            _push_envelope({"pull_id": "pull_1", "project_id": "proj_1", "module": "gsc"}),
        )
    assert status == 200
    assert body["outcome"] == "seeded"
    assert seen and seen[0]["module"] == "gsc"


def test_a_consumer_failure_asks_for_redelivery():
    """503, not 200: the fact is real and the work still has to happen."""
    from core import facts_api

    with patch("core.facts_api.run_verification_for", side_effect=RuntimeError("db down")):
        status, body = _call_fact(
            facts_api._verification_push, _push_envelope({"pull_id": "pull_1"})
        )
    assert status == 503
    assert body["code"] == "consumer_failed"


def test_an_undecodable_delivery_is_dropped_not_retried_forever():
    """Redelivering the same broken bytes produces the same result forever.

    This is the one case where dropping is right -- and it is logged, so the
    drop is not silent.
    """
    from core import facts_api

    status, body = _call_fact(facts_api._verification_push, b"not json at all")
    assert status == 200
    assert body["dropped"] is True


def test_the_worker_keeps_calling_its_consumers_until_one_can_be_reached():
    """Unplugging the inline hooks where no subscriber exists would silently stop
    verifying pulls -- the opposite of what story 56.7 is for."""
    import os

    from core.facts_api import facts_are_delivered

    saved = {k: os.environ.get(k) for k in ("PUBSUB_TOPIC", "QUEUE_BACKEND")}
    try:
        os.environ["PUBSUB_TOPIC"] = ""
        os.environ["QUEUE_BACKEND"] = "cloud_tasks"
        assert facts_are_delivered() is False, "a backend without a topic delivers nothing"

        os.environ["PUBSUB_TOPIC"] = "projects/x/topics/y"
        os.environ["QUEUE_BACKEND"] = "local"
        assert facts_are_delivered() is False, "a topic without the push backend has no peer"

        os.environ["QUEUE_BACKEND"] = "cloud_tasks"
        assert facts_are_delivered() is True
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_the_subscriber_endpoints_are_routed():
    from core.admin_api import router
    from starlette.routing import Match

    for path in (
        "/internal/facts/pull-landed/verification",
        "/internal/facts/pull-landed/context-seed",
    ):
        scope = {"type": "http", "method": "POST", "path": path,
                 "path_params": {}, "headers": [], "root_path": ""}
        assert any(r.matches(scope)[0] is Match.FULL for r in router.routes), path


def test_an_oidc_token_for_another_service_does_not_open_this_one():
    """The audience is what makes a Google-signed token OURS rather than valid.

    Story 56.7: Pub/Sub push cannot send `X-Internal-Auth`, so OIDC is the only
    mechanism available to it -- which makes the audience check the whole of the
    security, not a refinement of it.
    """
    import os

    from starlette.requests import Request as _Request

    saved = os.environ.get("INTERNAL_OIDC_AUDIENCE")
    os.environ["INTERNAL_OIDC_AUDIENCE"] = "https://mine.example.com"
    try:
        request = _Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/internal/facts/pull-landed/verification",
                "path_params": {},
                "headers": [(b"authorization", b"Bearer not-a-real-token")],
                "query_string": b"",
            }
        )
        assert admin_api._google_oidc_caller_is_ours(request) is False
    finally:
        if saved is None:
            os.environ.pop("INTERNAL_OIDC_AUDIENCE", None)
        else:
            os.environ["INTERNAL_OIDC_AUDIENCE"] = saved


def test_a_google_identity_that_is_not_ours_is_refused():
    """Google vouching for an identity does not make it this deployment's."""
    import os

    from starlette.requests import Request as _Request

    saved = {k: os.environ.get(k) for k in
             ("INTERNAL_OIDC_AUDIENCE", "INTERNAL_OIDC_SERVICE_ACCOUNT")}
    os.environ["INTERNAL_OIDC_AUDIENCE"] = "https://mine.example.com"
    os.environ["INTERNAL_OIDC_SERVICE_ACCOUNT"] = "push@mine.iam.gserviceaccount.com"
    try:
        request = _Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/internal/facts/pull-landed/verification",
                "path_params": {},
                "headers": [(b"authorization", b"Bearer token")],
                "query_string": b"",
            }
        )
        with patch("google.oauth2.id_token.verify_oauth2_token",
                   return_value={"email": "someone-else@other.iam.gserviceaccount.com"}):
            assert admin_api._google_oidc_caller_is_ours(request) is False
        with patch("google.oauth2.id_token.verify_oauth2_token",
                   return_value={"email": "push@mine.iam.gserviceaccount.com"}):
            assert admin_api._google_oidc_caller_is_ours(request) is True
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_no_bearer_means_another_mechanism_not_a_denial():
    from starlette.requests import Request as _Request

    request = _Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/internal/scheduler/dispatch-nightly",
            "path_params": {},
            "headers": [],
            "query_string": b"",
        }
    )
    assert admin_api._google_oidc_caller_is_ours(request) is False


def test_a_pushed_task_carries_the_credential_the_worker_demands():
    """A task with no OIDC token is a task the worker answers 401 to, forever.

    `_check_internal_auth` accepts a Google OIDC id token minted for this service
    (verified against `INTERNAL_OIDC_AUDIENCE`) or the `X-Internal-Auth` shared
    secret. `_create_push_task` built `{"http_request": {"http_method", "url"}}`
    and attached NEITHER, so every task it created was refused.

    MEASURED live 2026-08-07: two `setup_preview` jobs sat in `queued` while
    Cloud Tasks retried them; the logs show
    `POST /internal/worker/execute-activation/dsaj_... 401 Unauthorized` and
    `internal_auth_rejected ... falling back to user auth` on every attempt. The
    queue was RUNNING and the dashboards were green -- the work simply could
    never authenticate. That covers every push job: previews, activations, pulls.

    Nothing asserted the task's shape before, which is why an unauthenticated
    dispatch looked exactly like a working one.
    """
    from unittest.mock import MagicMock

    from core import queue

    client = MagicMock()
    client.queue_path.return_value = "projects/p/locations/l/queues/q"
    env = {
        "CLOUD_TASKS_PROJECT": "toorow",
        "CLOUD_TASKS_LOCATION": "europe-west1",
        "CLOUD_TASKS_QUEUE_NAME": "toorow-work",
        "CLOUD_TASKS_WORKER_URL": "https://mcp-server.example.run.app",
        "INTERNAL_OIDC_AUDIENCE": "https://mcp-server.example.run.app",
        "CLOUD_TASKS_OIDC_SERVICE_ACCOUNT": "worker@example.iam.gserviceaccount.com",
    }
    with patch.dict(os.environ, env, clear=False):
        queue._create_push_task("/internal/worker/execute-activation/dsaj_1", tasks_client=client)

    task = client.create_task.call_args.kwargs["request"]["task"]
    oidc = task["http_request"].get("oidc_token")
    assert oidc, "the task carries no credential; the worker will answer 401"
    assert oidc["service_account_email"] == "worker@example.iam.gserviceaccount.com"
    assert oidc["audience"] == "https://mcp-server.example.run.app"


def test_a_task_that_could_not_authenticate_is_never_created():
    """Fail closed, exactly like the missing-project guard above it.

    Creating a task nobody can accept is worse than creating none: Cloud Tasks
    retries it for hours, the queue looks busy, and the job stays `queued`.
    """
    from unittest.mock import MagicMock

    import pytest
    from core import queue

    client = MagicMock()
    client.queue_path.return_value = "projects/p/locations/l/queues/q"
    env = {
        "CLOUD_TASKS_PROJECT": "toorow",
        "CLOUD_TASKS_LOCATION": "europe-west1",
        "CLOUD_TASKS_QUEUE_NAME": "toorow-work",
        "CLOUD_TASKS_WORKER_URL": "https://mcp-server.example.run.app",
        "CLOUD_TASKS_OIDC_SERVICE_ACCOUNT": "",
        "INTERNAL_OIDC_AUDIENCE": "",
    }
    with patch.dict(os.environ, env, clear=False), pytest.raises(EnvironmentError):
        queue._create_push_task("/internal/worker/execute-activation/dsaj_1", tasks_client=client)
    client.create_task.assert_not_called()


# ---------------------------------------------------------------------------
# AI-302 -- the verification subscriber must carry the pull's OWN profile.
#
# Measured on production 2026-08-17: a pull of 344 rows, readable seven seconds
# before the verdict, was filed `empty`. `_count_raw_rows` resolves WHICH relation
# a pull landed in from `report_profile_id`; without it, it falls back to the
# module's single registered table -- empty for youtube-analytics -- counts 0, and
# `empty` raises a STICKY `populate_failed` that turns every later enqueue of the
# whole authorization into `access_denied`. Nine Datastreams were denied behind
# one verdict. `queue.py` passes the profile to the very same function; this
# subscriber did not, so the repair of 2026-08-12 landed on one caller of two.
# ---------------------------------------------------------------------------


def _verification_call(payload: dict) -> dict:
    """Run the subscriber and capture what it asked verification for."""
    from core import facts_api

    seen: dict = {}

    def _capture(**kwargs):
        seen.update(kwargs)

    manifest = {
        "module_kind": "connector",
        "report_profiles": [
            {"id": "channel_daily"},
            {"id": "audience_demographics"},
        ],
    }
    with (
        patch("core.queue._get_manifest_for_module", return_value=manifest),
        patch("core.verification.run_post_pull_verification", side_effect=_capture),
        patch("core.context_events.resolve_landing", return_value="raw"),
    ):
        facts_api.run_verification_for(payload)
    return seen


def test_the_subscriber_verifies_against_the_profile_the_pull_actually_used():
    """The fact carries it, and it must reach the counter."""
    seen = _verification_call(
        {
            "pull_id": "pull_1",
            "module": "youtube-analytics",
            "report_profile_id": "audience_demographics",
            "project_id": "proj_1",
        }
    )

    assert seen["report_profile_id"] == "audience_demographics", (
        "without the profile the count reads another relation, finds zero rows, "
        "and one `empty` verdict denies every Datastream of the authorization"
    )


def test_a_fact_older_than_the_field_resolves_the_profile_from_its_datastream():
    """A fact already in flight must not be counted against the wrong relation."""
    from contextlib import contextmanager

    @contextmanager
    def _no_db():
        yield object()

    with (
        patch("core.db.get_connection", _no_db),
        patch("core.queue._resolve_datastream_profile", return_value="audience_demographics"),
    ):
        seen = _verification_call(
            {
                "pull_id": "pull_2",
                "module": "youtube-analytics",
                "datastream_id": "ds_1",
                "project_id": "proj_1",
            }
        )

    assert seen["report_profile_id"] == "audience_demographics"


def test_a_pull_with_no_profile_at_all_still_verifies():
    """No profile anywhere is the legacy per-connection pull: unchanged."""
    seen = _verification_call({"pull_id": "pull_3", "module": "youtube-analytics"})

    assert seen["report_profile_id"] is None
    assert seen["pull_id"] == "pull_3"
