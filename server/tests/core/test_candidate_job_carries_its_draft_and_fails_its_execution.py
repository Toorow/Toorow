"""AI-321, second cause: the candidate job never carried its draft, and a dead job
left its execution alive.

G9 replayed on 2026-08-28 after the final-review repair: `final-reviews` now
succeeds, `materialize` enqueues the candidate, and the worker dies three times
on `Staged asset is no longer available for preview` -- while the asset row sat
`available`, expiring in seven days. The driver resolves the asset by
`(id, draft_id, project_id)`; the job row had `draft_id = NULL` and the context
built for the driver had no `draft_id` key at all. Then the dead-lettered job
left `app.datastream_executions` in `created`, and every later confirm of the
Datastream was refused with `concurrent_execution_active` -- rendered by the
workbench door as 503 "Datastream evidence is unavailable".

Three pins, one per link. The job row cannot carry the draft (the queue refuses
that scope for a candidate), so the materialization row is the binding.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# 1. The candidate context carries the draft -- from the job, else from the
#    materialization row the same transaction wrote.
# ---------------------------------------------------------------------------


class _Row:
    """The 18-column row `_candidate_context` selects, with the draft last."""

    def __init__(self, draft_id):
        self.values = [
            "created",  # 0 e.state
            "dsp_1",  # 1 plan
            "dsm_1",  # 2 mapping
            "{}",  # 3 projection_plan_ref
            "{}",  # 4 normalized_payload
            "{}",  # 5 mapping_payload
            '{"mode": "managed_feed", "channel": "file_upload"}',  # 6 review_snapshot
            "org_1",  # 7 org_id
            "managed_feed",  # 8 module_name
            None,  # 9 connection_ref_id
            None,  # 10 report_profile_id
            "file",  # 11 source_kind
            None,  # 12 date_window_days
            None,  # 13 refetch_days
            None,  # 14 window_offset_days
            None,  # 15 schedule_mode
            None,  # 16 external_account_id
            draft_id,  # 17 sm.draft_id
        ]


class _Conn:
    def __init__(self, row):
        self._row = row
        self.seen_sql: list[str] = []
        self._last_sql = ""

    def cursor(self):
        conn = self

        class _Cur:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def execute(self_inner, sql, params=None):
                conn.seen_sql.append(str(sql))
                conn._last_sql = str(sql)

            def fetchone(self_inner):
                if "review_snapshot" in conn._last_sql and "ORDER BY sm.id DESC" in conn._last_sql:
                    return ("dsd_last_materialization", '{"channel": "file_upload"}')
                return conn._row.values

        return _Cur()


@pytest.fixture
def worker(monkeypatch):
    from inbound import datastream_activation_worker as module

    monkeypatch.setattr(module, "_declared_window", lambda *a, **k: None)
    interval = {"date_from": "d", "date_to": "d"}
    monkeypatch.setattr(module, "_driver_interval", lambda *a, **k: interval)
    monkeypatch.setattr(module, "_active_parsing_contract", lambda *a, **k: {})
    monkeypatch.setattr(module, "selected_account_pull_arguments", lambda *a, **k: {})
    return module


def _job(draft_id):
    return {
        "id": "dsaj_1",
        "kind": "candidate_materialization",
        "project_id": "proj_EXAMPLE",
        "draft_id": draft_id,
        "datastream_id": "ds_1",
        "execution_id": "dse_1",
        "requested_by": "owner@example.com",
    }


def test_the_context_takes_the_draft_from_the_materialization_row_when_the_job_has_none(
    worker,
):
    conn = _Conn(_Row("dsd_from_row"))
    context = worker._candidate_context(conn, _job(None), {"mode": "managed_feed"})
    assert context["draft_id"] == "dsd_from_row"
    assert "sm.draft_id" in conn.seen_sql[0]


def test_a_change_candidate_without_a_review_takes_the_channel_of_the_last_review(worker):
    """`managed_feed_candidate` refuses a candidate that names no channel (AI-321, 2nd day)."""
    conn = _Conn(_Row("dsd_from_row"))
    conn._row.values[6] = "{}"  # no materialization review for a change candidate
    context = worker._candidate_context(conn, _job(None), {"mode": "managed_feed"})
    assert context["channel"] == "file_upload"


def test_a_change_candidate_takes_the_draft_of_the_last_materialization(worker):
    """No job draft, no materialization row of its own: the last one is the binding."""
    conn = _Conn(_Row(None))
    conn._row.values[6] = "{}"
    context = worker._candidate_context(conn, _job(None), {"mode": "managed_feed"})
    assert context["draft_id"] == "dsd_last_materialization"
    assert context["channel"] == "file_upload"


def test_the_job_s_own_draft_wins_over_the_row(worker):
    conn = _Conn(_Row("dsd_from_row"))
    context = worker._candidate_context(conn, _job("dsd_from_job"), {"mode": "managed_feed"})
    assert context["draft_id"] == "dsd_from_job"


def test_a_candidate_job_cannot_carry_a_draft_so_the_row_is_the_binding():
    """Why the fallback above is THE path and not a courtesy."""
    from core.queue import enqueue_activation_work

    with pytest.raises(ValueError, match="scope does not match its kind"):
        enqueue_activation_work(
            kind="candidate_materialization",
            project_id="proj_EXAMPLE",
            correlation_id="dse_1",
            payload={"mode": "managed_feed"},
            requested_by="owner@example.com",
            draft_id="dsd_1",
            datastream_id="ds_1",
            execution_id="dse_1",
            conn=object(),
        )


# ---------------------------------------------------------------------------
# 2. A dead-lettered job fails its execution, so the Datastream is not blocked
#    for ever.
# ---------------------------------------------------------------------------


def test_a_dead_lettered_candidate_job_fails_its_execution(monkeypatch):
    from core import queue

    failed: list[tuple] = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            pass

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return _Cur()

        def commit(self):
            pass

    def _boom(_name):
        def _install():
            pass

        def _execute(conn, job):
            raise LookupError("Staged asset is no longer available for preview")

        return _install if _name == "install_runtime_activation_drivers" else _execute

    monkeypatch.setattr(queue, "_max_attempts", lambda: 3)
    with (
        patch("core.inbound_seam.resolve_inbound", side_effect=_boom),
        patch("core.db.get_connection", return_value=_Conn()),
        patch(
            "core.datastream_publication._fail_execution_out_of_band",
            side_effect=lambda *args: failed.append(args),
        ),
    ):
        queue._execute_activation_job(
            {
                "id": "dsaj_1",
                "execution_id": "dse_1",
                "requested_by": "owner@example.com",
                "attempt_count": 3,
            }
        )

    assert len(failed) == 1, "the terminal attempt fails the execution exactly once"
    execution_id, actor, error_code, detail, factory = failed[0]
    assert execution_id == "dse_1"
    assert actor == "owner@example.com"
    assert error_code == "lookup_error"
    assert "Staged asset" in detail
    assert callable(factory)


def test_a_retryable_failure_leaves_the_execution_alone(monkeypatch):
    from core import queue

    failed: list[tuple] = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            pass

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return _Cur()

        def commit(self):
            pass

    def _boom(_name):
        def _install():
            pass

        def _execute(conn, job):
            raise LookupError("transient")

        return _install if _name == "install_runtime_activation_drivers" else _execute

    monkeypatch.setattr(queue, "_max_attempts", lambda: 3)
    with (
        patch("core.inbound_seam.resolve_inbound", side_effect=_boom),
        patch("core.db.get_connection", return_value=_Conn()),
        patch(
            "core.datastream_publication._fail_execution_out_of_band",
            side_effect=lambda *args: failed.append(args),
        ),
    ):
        queue._execute_activation_job(
            {"id": "dsaj_1", "execution_id": "dse_1", "attempt_count": 1}
        )

    assert failed == [], "an attempt that will come back is not a failed run"


# ---------------------------------------------------------------------------
# 3. The workbench door says which execution blocks, with a 409.
# ---------------------------------------------------------------------------


def test_the_workbench_door_names_the_blocking_execution_as_409():
    import json

    from core.datastream_publication import ConcurrentExecutionActive
    from core.datastream_workbench_api import _error

    response = _error(ConcurrentExecutionActive("dse_blocking"))
    assert response.status_code == 409
    body = json.loads(response.body)
    assert body["code"] == "concurrent_execution_active"
    assert body["blocking_execution_id"] == "dse_blocking"
    assert "confirm again" in body["message"]
