"""Tests for server/core/daily_insights.py (Epic 35, Story 35.3).

Offline (mocked psycopg, same pattern as test_snapshots.py). Proves:
  - atomic run + items write (one transaction, ulid ids, commit);
  - distinct states (no_insight with zero items; guards on published/non-published);
  - idempotency guards (duplicate slots, budget);
  - rollback on DB failure;
  - AD-5 scoped reads (cross-project denial returns None).

The pg-gated integration seam (real Postgres) is deferred to the warehouse phase.
ASCII-only stdout (L-3).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from core.daily_insights import (
    InsightItem,
    get_insight,
    get_run,
    record_run,
)


def _make_conn(fetchone_return=None, fetchall_return=None):
    conn = MagicMock()
    conn.commit = MagicMock()
    conn.rollback = MagicMock()
    cur = MagicMock()
    cur.fetchone = MagicMock(return_value=fetchone_return)
    cur.fetchall = MagicMock(return_value=fetchall_return or [])
    cur.description = []
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn, cur


def _col_desc(names):
    return [type("D", (), {"__getitem__": staticmethod(lambda i, c=c: c)})() for c in names]


_PAYLOAD = {
    "schemaVersion": "1",
    "slot": 0,
    "insight": {"title": "t", "summary": "s", "whyItMatters": "w", "confidence": "high"},
    "period": {"dateFrom": "2026-07-21", "dateTo": "2026-07-21"},
    "card": {"mode": "template", "template": "conversions"},
}


# ---------------------------------------------------------------------------
# record_run -- atomic write
# ---------------------------------------------------------------------------


def test_record_run_inserts_run_and_items_atomically():
    conn, cur = _make_conn(fetchone_return=None)  # no existing run
    calls = []
    cur.execute = MagicMock(side_effect=lambda sql, params=None: calls.append((sql, params)))

    run_id = record_run(
        project_id="proj_a",
        insight_date="2026-07-21",
        status="published",
        items=[
            InsightItem(slot=0, payload=_PAYLOAD, render_snapshot_id="rsn_1"),
            InsightItem(slot=1, payload={**_PAYLOAD, "slot": 1}),
        ],
        period_from="2026-07-21",
        period_to="2026-07-21",
        coverage={"datastreams": 3},
        host="claude-cowork",
        prompt_version="v1",
        contract_version="1",
        identity="user_1",
        conn=conn,
    )

    assert run_id.startswith("dir_")
    # SELECT existing, INSERT run, INSERT item x2
    assert len(calls) == 4
    assert "SELECT id FROM app.daily_insight_runs" in calls[0][0]
    assert "INSERT INTO app.daily_insight_runs" in calls[1][0]
    assert calls[2][0].strip().startswith("INSERT INTO app.daily_insights")
    assert "ON CONFLICT (project_id, insight_date, slot)" in calls[2][0]
    # payload_hash computed and passed (position 6 in the item insert params)
    assert isinstance(calls[2][1][6], str) and len(calls[2][1][6]) == 64
    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()


def test_record_run_no_insight_zero_items_is_valid_and_distinct():
    conn, cur = _make_conn(fetchone_return=None)
    calls = []
    cur.execute = MagicMock(side_effect=lambda sql, params=None: calls.append((sql, params)))

    run_id = record_run(
        project_id="proj_a",
        insight_date="2026-07-21",
        status="no_insight",
        items=[],
        conn=conn,
    )
    assert run_id.startswith("dir_")
    # SELECT + INSERT run only, no item inserts
    assert len(calls) == 2
    assert not any("INSERT INTO app.daily_insights" in c[0] for c in calls)
    conn.commit.assert_called_once()


def test_record_run_updates_existing_run_idempotent():
    conn, cur = _make_conn(fetchone_return=("dir_existing",))
    calls = []
    cur.execute = MagicMock(side_effect=lambda sql, params=None: calls.append((sql, params)))

    run_id = record_run(
        project_id="proj_a",
        insight_date="2026-07-21",
        status="blocked",
        items=[],
        conn=conn,
    )
    assert run_id == "dir_existing"
    assert "UPDATE app.daily_insight_runs" in calls[1][0]


@pytest.mark.parametrize(
    "kwargs,exc_match",
    [
        (dict(status="bogus", items=[]), "statut invalide"),
        (dict(status="published", items=[]), "au moins un insight"),
        (dict(status="blocked", items=[InsightItem(0, _PAYLOAD)]), "ne doit pas porter"),
        (
            dict(status="published", items=[InsightItem(0, _PAYLOAD), InsightItem(0, _PAYLOAD)]),
            "slots dupliques",
        ),
        (
            dict(status="published", items=[InsightItem(i, _PAYLOAD) for i in range(4)]),
            "depasse le budget",
        ),
    ],
)
def test_record_run_guards_reject_before_db(kwargs, exc_match):
    conn = MagicMock()
    with pytest.raises(ValueError, match=exc_match):
        record_run(project_id="proj_a", insight_date="2026-07-21", conn=conn, **kwargs)
    conn.commit.assert_not_called()


def test_record_run_rolls_back_on_db_failure():
    conn, cur = _make_conn(fetchone_return=None)
    # SELECT ok, INSERT run raises
    cur.execute = MagicMock(side_effect=[None, RuntimeError("db down")])

    with pytest.raises(RuntimeError, match="db down"):
        record_run(
            project_id="proj_a",
            insight_date="2026-07-21",
            status="no_insight",
            items=[],
            conn=conn,
        )
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


# ---------------------------------------------------------------------------
# Reads -- AD-5 scoping
# ---------------------------------------------------------------------------


def test_get_run_returns_run_with_items_scoped():
    conn, cur = _make_conn()
    cur.description = _col_desc(
        [
            "id",
            "project_id",
            "insight_date",
            "status",
            "period_from",
            "period_to",
            "coverage",
            "host",
            "prompt_version",
            "contract_version",
            "identity",
            "trace_id",
            "created_at",
            "updated_at",
        ]
    )
    cur.fetchone = MagicMock(
        return_value=(
            "dir_1",
            "proj_a",
            None,
            "no_insight",
            None,
            None,
            None,
            None,
            None,
            "1",
            "user_1",
            None,
            None,
            None,
        )
    )
    cur.fetchall = MagicMock(return_value=[])  # no items

    run = get_run("proj_a", "2026-07-21", conn)
    assert run is not None
    assert run["id"] == "dir_1"
    assert run["status"] == "no_insight"
    assert run["insights"] == []
    # WHERE project_id filter present -> AD-5
    sql = cur.execute.call_args_list[0][0][0]
    assert "WHERE project_id = %s" in sql


def test_get_run_cross_project_denied():
    conn, cur = _make_conn(fetchone_return=None)  # other project's date -> no row
    assert get_run("proj_b", "2026-07-21", conn) is None


def test_get_insight_cross_project_denied():
    conn, cur = _make_conn(fetchone_return=None)
    assert get_insight("din_1", "proj_b", conn) is None
    sql = cur.execute.call_args_list[0][0][0]
    assert "AND project_id = %s" in sql
