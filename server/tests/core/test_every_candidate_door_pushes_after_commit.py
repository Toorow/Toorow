"""Every door that mints a candidate pushes it AFTER the commit -- AI-321.

Measured on G9, 2026-08-29: the wizard's doors push the candidate they commit
(`_dispatch_activation_task`, story 56.3); the workbench change door and the
project-settings door (Country fan-out) did not. Their candidates sat `queued`
with the execution `created` until the reconciliation sweep, and a person who
had just confirmed a mapping watched a run that never started. The gate waited
120 s and called it a defect; it was one, just not the one it named.

Three pins: the helper never raises and pushes each id; the change door pushes
the candidate it minted; the settings door pushes every fan-out candidate.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

from starlette.requests import Request


def _request(path_params: dict, body: dict, path: str = "/x") -> Request:
    payload = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "path_params": path_params,
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"",
        },
        receive,
    )


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def transaction(self):
        return self

    def commit(self):
        pass

    def rollback(self):
        pass


# ---------------------------------------------------------------------------


def test_the_helper_pushes_each_id_and_survives_a_failing_dispatch():
    from core import queue

    pushed: list[str] = []

    def _dispatch(job_id, **_):
        if job_id == "dsaj_boom":
            raise RuntimeError("cloud tasks down")
        pushed.append(job_id)
        return True

    with patch.object(queue, "dispatch_activation_task", side_effect=_dispatch):
        queue.dispatch_after_commit("dsaj_1", None, "", "dsaj_boom", "dsaj_2")

    assert pushed == ["dsaj_1", "dsaj_2"], "every real id is pushed, the failure is logged"


def test_the_workbench_change_door_pushes_the_candidate_it_minted():
    from core import datastream_workbench_api as api

    pushed: list[str] = []

    async def _authorize(request, level):
        return "owner@example.com"

    with (
        patch.object(api, "_authorize", _authorize),
        patch("core.datastream_change.require_confirming_host", lambda *a, **k: None),
        patch(
            "core.datastream_change.confirm_change",
            return_value={"candidate_job_id": "dsaj_change", "candidate_execution_id": "dse_1"},
        ),
        patch("core.db.get_connection", return_value=_Conn()),
        patch("core.queue.dispatch_activation_task", side_effect=lambda j, **_: pushed.append(j)),
    ):
        response = asyncio.run(
            api._confirm_change(
                _request(
                    {"project_id": "proj_EXAMPLE", "datastream_id": "ds_1", "preparation_id": "p1"},
                    {"confirmation_secret": "s"},
                )
            )
        )

    assert response.status_code == 200
    assert pushed == ["dsaj_change"]


def test_the_workbench_change_door_pushes_nothing_when_no_candidate_was_minted():
    from core import datastream_workbench_api as api

    pushed: list[str] = []

    async def _authorize(request, level):
        return "owner@example.com"

    with (
        patch.object(api, "_authorize", _authorize),
        patch("core.datastream_change.require_confirming_host", lambda *a, **k: None),
        patch(
            "core.datastream_change.confirm_change",
            return_value={"candidate_job_id": None, "no_candidate_reason": "not executable"},
        ),
        patch("core.db.get_connection", return_value=_Conn()),
        patch("core.queue.dispatch_activation_task", side_effect=lambda j, **_: pushed.append(j)),
    ):
        response = asyncio.run(
            api._confirm_change(
                _request(
                    {"project_id": "proj_EXAMPLE", "datastream_id": "ds_1", "preparation_id": "p1"},
                    {"confirmation_secret": "s"},
                )
            )
        )

    assert response.status_code == 200
    assert pushed == []


def test_the_settings_door_pushes_every_country_fan_out_candidate():
    """Source-level on the door's own text, because the settings confirm handler
    composes six authorities; the push line is what this pins."""
    import inspect

    from core import project_settings_api

    source = inspect.getsource(project_settings_api._confirm_change_set)
    after_commit = source[source.index("conn.commit()") :]
    assert "dispatch_after_commit(" in after_commit
    assert 'country_plan_fan_out' in after_commit
    assert '"candidates"' in after_commit
