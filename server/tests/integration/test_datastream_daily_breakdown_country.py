"""The country split, walked end to end on real rows -- story 58.5, epic 58.

THROUGH `build_asgi_app()` AND AGAINST POSTGRES, because what this file exists to
prove cannot be proved anywhere else: that a real row of `app.project_capabilities`
reaches the payload, and that the read is SCOPED to the project that asked. The
in-memory double of `tests/core/test_datastream_daily_breakdown.py` routes on the
substring `app.project_capabilities` and answers whatever the fixture felt like -- it
cannot execute the statement, so it can neither prove the SQL is valid nor prove the
`WHERE project_id = %s` is there. A capability read that ignored the project would
turn one project's decision into every project's column.

THE FIXTURE FABRICATES THE ON STATE, AND THAT IS THE POINT. Measured on the
disposable cluster 2026-08-07:

    select capability_key, state, count(*) from app.project_capabilities group by 1,2
    -> country 'disabled' 1891 | tax_fees 'disabled' 1891 | competitors 'disabled' 1891
       currency_fx 'draft' 1891 | reporting_timezone 'draft' 1891

`country` is `disabled` on 1891 projects out of 1891. NO project of this estate shows
the split, so the branch that does is exercised by a row this fixture writes and by
nothing else. The honest render of story 58.5 across the whole product today is no
column anywhere; that is a state of the estate, not of the code.

WHAT THE FIXTURE DELIBERATELY DOES NOT DO. It never commits: everything happens inside
the connection's transaction and is rolled back.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

IDENTITY = "reader@example.com"
AUTHOR = "story-58-5"

#: A window entirely in the past, so nothing a scheduler does can move it.
DAY = "2026-06-15"
WINDOW = {"start": "2026-06-14", "end": "2026-06-16"}


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


@pytest.fixture
def a_stream(live_postgres, monkeypatch):
    """One org, one project, one connection, one Datastream.

    THE CAPABILITY ROWS ARRIVE ON THEIR OWN, and that is a fact this file measured
    rather than assumed. `app.seed_project_capabilities_after_insert` (migration 131)
    is an AFTER INSERT trigger on `app.projects`: every project is born with its five
    rows, `country` among them at `disabled`. That is why 1891 projects out of 1891
    carry one, and why a test that INSERTED its own would collide on the primary key.
    The tests that want another state UPDATE the row the control plane wrote.
    """
    monkeypatch.setenv("TOOROW_AUTH_MODE", "static")
    monkeypatch.setenv("TOOROW_STATIC_TOKEN", "daily-breakdown-country-local-token")
    conn = live_postgres
    org_id, project_id = _id("org_"), _id("proj_")
    ds_id, connection_ref_id = _id("ds_"), _id("cref_")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by, status)"
            " VALUES (%s, %s, %s, %s, 'active')",
            (org_id, org_id, org_id, AUTHOR),
        )
        cur.execute(
            "INSERT INTO app.org_members (id, org_id, identity, role, status)"
            " VALUES (%s, %s, %s, 'owner', 'active')",
            (_id("mem_"), org_id, IDENTITY),
        )
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, created_by, org_id, status)"
            " VALUES (%s, %s, %s, %s, %s, 'active')",
            (project_id, project_id, project_id, AUTHOR, org_id),
        )
        cur.execute(
            "INSERT INTO app.connection_ref"
            " (id, provider, nango_connection_id, project_id, status, enabled,"
            "  owner_org_id, owner_identity)"
            " VALUES (%s, 'google-analytics', %s, %s, 'active', TRUE, %s, %s)",
            (connection_ref_id, connection_ref_id, project_id, org_id, IDENTITY),
        )
        cur.execute(
            "INSERT INTO app.datastreams (id, project_id, name, module_name,"
            " source_kind, connection_ref_id, enabled, created_by, org_id)"
            " VALUES (%s, %s, 'Stream', 'google-analytics', 'connector_pull', %s,"
            "         TRUE, %s, %s)",
            (ds_id, project_id, connection_ref_id, AUTHOR, org_id),
        )
    try:
        yield {
            "conn": conn,
            "org_id": org_id,
            "project_id": project_id,
            "ds_id": ds_id,
            "connection_ref_id": connection_ref_id,
        }
    finally:
        conn.rollback()


def _capability(ids, state, *, project_id=None, capability_key="country"):
    """Move the switch story 58.5 reads, on the row the control plane already wrote.

    An UPDATE and not an INSERT: the trigger of migration 131 got there first, and
    `active_version_id` stays NULL, which is the state of every one of the 1891 rows
    measured on the disposable cluster.
    """
    with ids["conn"].cursor() as cur:
        cur.execute(
            "UPDATE app.project_capabilities SET state = %s"
            " WHERE project_id = %s AND capability_key = %s",
            (state, project_id or ids["project_id"], capability_key),
        )
        assert cur.rowcount == 1, (
            f"no {capability_key!r} capability row to move -- migration 131 seeds one "
            "per project on insert, so this fixture is describing a database it is not "
            "running against"
        )


def _forget_capability(ids, *, capability_key="country"):
    """Delete the row, which is the ONLY way to reach the reader's `unset` branch.

    Said plainly: no project of this estate is in that state and none can arrive there
    by being created -- the trigger sees to it. The branch exists because a reader that
    fetched nothing must still answer something, and answering `disabled` would be
    reporting a decision nobody took.
    """
    with ids["conn"].cursor() as cur:
        cur.execute(
            "DELETE FROM app.project_capabilities"
            " WHERE project_id = %s AND capability_key = %s",
            (ids["project_id"], capability_key),
        )


def _other_project(ids):
    """A SECOND project in the same org, so a scope defect has somewhere to leak from."""
    project_id = _id("proj_")
    with ids["conn"].cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, created_by, org_id, status)"
            " VALUES (%s, %s, %s, %s, %s, 'active')",
            (project_id, project_id, project_id, AUTHOR, ids["org_id"]),
        )
    return project_id


def _get(ids, *, params=None):
    """Drive the real application against the fixture's own transaction."""
    from core.main import build_asgi_app

    connection = ids["conn"]

    @contextmanager
    def _open(*_a, **_k):
        yield connection

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, IDENTITY))),
        patch("core.db.get_connection", side_effect=_open),
    ):
        client = TestClient(build_asgi_app(), raise_server_exceptions=False)
        return client.get(
            f"/api/projects/{ids['project_id']}"
            f"/datastreams/{ids['ds_id']}/daily-breakdown",
            params=params or WINDOW,
        )


def _mart(counts=None, *, country=None, dimension_present=True):
    """The warehouse, doubled -- and it is the ONLY thing doubled here.

    The disposable cluster carries no mart, and this file is about Postgres. The two
    warehouse readers are exercised for real in `tests/core/test_cache_warehouse*`.
    """
    rows = patch(
        "core.cache_warehouse.read_daily_row_counts",
        return_value={"connector_present": counts is not None, "counts": counts or {}},
    )
    split = patch(
        "core.cache_warehouse.read_daily_country_counts",
        return_value={"dimension_present": dimension_present, "counts": country or {}},
    )
    return rows, split


@contextmanager
def _warehouse(counts=None, *, country=None, dimension_present=True):
    rows, split = _mart(counts, country=country, dimension_present=dimension_present)
    with rows, split:
        yield


# ---------------------------------------------------------------------------
# The state travels, off a real row.
# ---------------------------------------------------------------------------


def test_the_measured_state_of_the_estate_reaches_the_payload(a_stream) -> None:
    """`disabled`, which is what 1891 projects out of 1891 carry -- and what this
    project was born with, without this test writing anything."""
    with _warehouse({DAY: 4}):
        response = _get(a_stream)

    assert response.status_code == 200
    block = response.json()["country"]
    assert block["capability_state"] == "disabled"
    assert block["active"] is False
    assert block["reason"] == "capability_not_active"
    # No measurement of any kind: not a list, not a null, not a zero.
    assert "days" not in block
    assert "bounded_at" not in block


def test_a_capability_row_that_does_not_exist_is_unset_and_not_guessed(a_stream) -> None:
    """The reader's fail-closed answer, exercised by DELETING the row.

    It cannot be reached by creating a project -- the trigger of migration 131 writes
    the five rows on insert -- so this test manufactures the state and says so. What
    it holds is that a reader which fetched nothing does not report `disabled`, which
    would be publishing a decision nobody took.
    """
    _forget_capability(a_stream)
    with _warehouse({DAY: 4}):
        block = _get(a_stream).json()["country"]

    assert block["capability_state"] == "unset"
    assert block["active"] is False
    assert "days" not in block


def test_the_capability_read_is_scoped_to_the_project_that_asked(a_stream) -> None:
    """THE STATEMENT IS EXECUTED HERE, so the `WHERE project_id` is proved here.

    Another project of the same org turns the capability ON. If the read were scoped
    to the org, or to nothing, this Datastream would grow a country column off a
    decision its project never took.
    """
    _capability(a_stream, "ready", project_id=_other_project(a_stream))
    with _warehouse({DAY: 4}, country={DAY: {"FR": 3}}):
        block = _get(a_stream).json()["country"]

    assert block["capability_state"] == "disabled"
    assert block["active"] is False
    assert "days" not in block


def test_another_capability_of_the_same_project_is_not_read_as_country(a_stream) -> None:
    """Five capability keys share this table. Keying on the project alone would let
    `tax_fees` decide whether a country column exists."""
    _capability(a_stream, "ready", capability_key="tax_fees")
    with _warehouse({DAY: 4}, country={DAY: {"FR": 3}}):
        block = _get(a_stream).json()["country"]

    assert block["capability_state"] == "disabled"
    assert block["active"] is False


# ---------------------------------------------------------------------------
# The ON state, fabricated -- and the file says so.
# ---------------------------------------------------------------------------


def test_a_ready_capability_unfolds_the_day_by_country(a_stream) -> None:
    """The state no project of this estate holds. `ready` is written by the line
    above and by nothing else in this database."""
    _capability(a_stream, "ready")
    with _warehouse({DAY: 9}, country={DAY: {"FR": 5, "DE": 3}}):
        payload = _get(a_stream).json()

    block = payload["country"]
    assert block["capability_state"] == "ready"
    assert block["active"] is True
    assert [entry["value"] for entry in block["days"][DAY]["values"]] == ["FR", "DE"]
    # And the strip of days is served exactly as it was before this story.
    assert payload["schema"] == "datastream_daily_breakdown.v1"


def test_the_absence_bucket_arrives_qualified_and_never_as_a_country(a_stream) -> None:
    """Arbitrage 1, on the wire. The identity the mart writes must reach the screen
    carrying its kind and a label that states an absence -- a bare code in that list
    would be read as a place."""
    from core.geographic_semantics import (
        COUNTRY_ABSENT_BUCKET_ID,
        COUNTRY_ABSENT_BUCKET_LABEL,
    )

    _capability(a_stream, "ready")
    with _warehouse({DAY: 9}, country={DAY: {"FR": 2, COUNTRY_ABSENT_BUCKET_ID: 7}}):
        values = _get(a_stream).json()["country"]["days"][DAY]["values"]

    assert values[0]["kind"] == "country"
    assert values[-1]["kind"] == "country_absent"
    assert values[-1]["label"] == COUNTRY_ABSENT_BUCKET_LABEL
    # It is not ranked among the countries however large it is.
    assert values[-1]["rows"] == 7


def test_a_flux_whose_connector_reports_no_country_says_so_with_the_capability_on(
    a_stream,
) -> None:
    """Arbitrage 7, end to end: two sentences, and this one is about the FLUX."""
    _capability(a_stream, "ready")
    with _warehouse({DAY: 4}, dimension_present=False):
        block = _get(a_stream).json()["country"]

    assert block["active"] is True
    assert block["reason"] == "connector_reports_no_country"
    assert "days" not in block


def test_the_days_survive_a_mart_that_cannot_answer_the_split(a_stream) -> None:
    """The registry lives in Postgres and the split lives in the mart. Neither may
    take the other down -- the rule 58.1 settled, applied to the block this story
    adds."""
    _capability(a_stream, "ready")
    with _warehouse(None):  # the connector is not modelled in the mart
        payload = _get(a_stream).json()

    assert payload["country"]["reason"] == "connector_not_in_mart"
    assert "days" not in payload["country"]
    assert payload["days"] == [] or isinstance(payload["days"], list)
    assert payload["reason"] in (None, "no_run_in_window")
