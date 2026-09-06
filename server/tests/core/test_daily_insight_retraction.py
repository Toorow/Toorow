"""A published insight is RETRACTED, never deleted (migration 321).

`proactive-assertions.md`, decision 4 and its `Incomplete if` clause: "a retraction
is delivered as a delete rather than an audited state transition". Nothing existed
on any of the three proactive stores before this file's subject was written.

TWO HALVES, AND NEITHER PROVES THE OTHER.

  - the OFFLINE half asks whether the write path refuses what it says it refuses,
    and whether it writes an UPDATE rather than a DELETE. A mocked cursor cannot
    refuse anything, so every claim here is a claim about Python;
  - the PG half replays the CHECK and the trigger against a live database, which
    is where a retraction is actually made immutable, and pins the one thing a
    guard on this table must never do -- close the RGPD erasure hatch.

Trap encoded, and already paid for elsewhere in this suite: a refused statement
aborts the whole transaction in psycopg 3, so every refusal below runs inside its
own `conn.transaction()` savepoint. Without it the SECOND `pytest.raises` passes on
`InFailedSqlTransaction` and proves nothing about the constraint it names.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from core.daily_insights import (
    ACTION_DAILY_INSIGHT_RETRACTED,
    DailyInsightRefusal,
    InsightItem,
    record_run,
    retract_insight,
)

psycopg = pytest.importorskip("psycopg")

_PAYLOAD = {
    "schemaVersion": "1",
    "slot": 0,
    "insight": {"title": "t", "summary": "s", "whyItMatters": "w", "confidence": "high"},
    "period": {"dateFrom": "2026-07-21", "dateTo": "2026-07-21"},
    "card": {"mode": "template", "template": "conversions"},
}


def _conn(*, fetchone_returns=None, fetchall_return=None):
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone = MagicMock(side_effect=list(fetchone_returns or [None]))
    cur.fetchall = MagicMock(return_value=fetchall_return or [])
    cur.description = []
    conn.cursor.return_value.__enter__ = lambda _s: cur
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn, cur


# ---------------------------------------------------------------------------
# The refusals -- each one names the gesture, never the constraint
# ---------------------------------------------------------------------------


def test_a_retraction_without_a_reason_is_refused_before_the_database():
    """A withdrawal nobody can judge later is the same object as an erasure."""
    conn, cur = _conn()

    with pytest.raises(DailyInsightRefusal) as excinfo:
        retract_insight(
            project_id="proj_a",
            insight_id="din_1",
            retracted_by="user_1",
            reason="   ",
            conn=conn,
        )

    assert excinfo.value.code == "missing_reason"
    assert "why" in excinfo.value.message.lower()
    cur.execute.assert_not_called()


def test_an_insight_of_another_project_is_absent_rather_than_forbidden():
    """AD-5: a row outside the scope is NOT FOUND, which discloses no existence."""
    conn, _cur = _conn(fetchone_returns=[None])

    with pytest.raises(DailyInsightRefusal) as excinfo:
        retract_insight(
            project_id="proj_other",
            insight_id="din_1",
            retracted_by="user_1",
            reason="wrong reading",
            conn=conn,
        )

    assert excinfo.value.code == "not_found"
    assert excinfo.value.status == 404
    conn.rollback.assert_called_once()


def test_a_second_retraction_is_refused_and_names_who_filed_the_first():
    """A retraction is never unmade, so it is never filed twice either."""
    conn, _cur = _conn(
        fetchone_returns=[("2026-07-23T09:00:00+00:00", "owner@example.com", 0, "2026-07-23")]
    )

    with pytest.raises(DailyInsightRefusal) as excinfo:
        retract_insight(
            project_id="proj_a",
            insight_id="din_1",
            retracted_by="user_1",
            reason="again",
            conn=conn,
        )

    assert excinfo.value.code == "already_retracted"
    assert excinfo.value.status == 409
    assert "owner@example.com" in excinfo.value.message
    # The repair is named, and it is not "un-retract it".
    assert "free slot" in excinfo.value.message


# ---------------------------------------------------------------------------
# What the write path actually writes
# ---------------------------------------------------------------------------


def test_the_retraction_is_an_update_and_never_a_delete(monkeypatch):
    audited: list[dict] = []
    import core.daily_insights as store

    monkeypatch.setattr(
        "core.audit.insert_audit_row",
        lambda conn, **kwargs: audited.append(kwargs) or "aud_1",
    )
    assert store.ACTION_DAILY_INSIGHT_RETRACTED == "daily_insight.retracted"

    conn, cur = _conn(
        fetchone_returns=[
            (None, None, 0, "2026-07-23"),  # the lock read: not retracted
            ("din_1",),  # the UPDATE ... RETURNING
        ]
    )
    cur.description = []

    retract_insight(
        project_id="proj_a",
        insight_id="din_1",
        retracted_by="user_1",
        reason="the figure was read on the wrong window",
        conn=conn,
    )

    statements = [call.args[0] for call in cur.execute.call_args_list]
    assert any("FOR UPDATE" in sql for sql in statements)
    assert any("UPDATE app.daily_insights" in sql for sql in statements)
    assert not any("DELETE" in sql.upper() for sql in statements)
    conn.commit.assert_called_once()

    assert len(audited) == 1
    assert audited[0]["action"] == ACTION_DAILY_INSIGHT_RETRACTED
    assert audited[0]["identity"] == "user_1"
    assert audited[0]["metadata"]["reason"].startswith("the figure was read")


def test_publication_refuses_a_retracted_slot_and_names_a_truly_free_one():
    """A retracted slot is not a free slot -- and neither is a STANDING one.

    Publication is idempotent per (project, date, slot). Overwriting a withdrawn
    claim would leave the retraction standing over prose it never judged -- the new
    claim would be born retracted. The trigger refuses the write; this refuses it
    first, with the gesture instead of a constraint name.

    THE DAY BELOW IS THE KILLER CASE (review of ae60c22a, R1): slot 0 retracted,
    slot 1 carrying a standing published claim. The first spelling computed "free"
    from the REQUESTED slots only and named slot 2 (index 1) -- the standing claim;
    obeying its own gesture would have upserted over a published assertion. Free
    means: no row of any kind on that day.
    """
    conn, cur = _conn(fetchall_return=[(0, True), (1, False)])

    with pytest.raises(DailyInsightRefusal) as excinfo:
        record_run(
            project_id="proj_a",
            insight_date="2026-07-23",
            status="published",
            items=[InsightItem(slot=0, payload=_PAYLOAD)],
            conn=conn,
        )

    assert excinfo.value.code == "slot_retracted"
    assert "slot 3 of that day is free" in excinfo.value.message
    conn.commit.assert_not_called()
    conn.rollback.assert_called_once()


def test_a_fully_occupied_day_says_no_slot_is_free():
    """When every slot holds a row, the refusal must not invent a destination."""
    from core.daily_insights import MAX_INSIGHTS_PER_DAY

    day = [(0, True)] + [(n, False) for n in range(1, MAX_INSIGHTS_PER_DAY)]
    conn, cur = _conn(fetchall_return=day)

    with pytest.raises(DailyInsightRefusal) as excinfo:
        record_run(
            project_id="proj_a",
            insight_date="2026-07-23",
            status="published",
            items=[InsightItem(slot=0, payload=_PAYLOAD)],
            conn=conn,
        )

    assert excinfo.value.code == "slot_retracted"
    assert "no slot of that day is free" in excinfo.value.message


# ---------------------------------------------------------------------------
# Replayed against a live PostgreSQL -- the half a mock cannot prove
# ---------------------------------------------------------------------------


def _uid(prefix: str) -> str:
    from ulid import ULID  # noqa: PLC0415

    return f"{prefix}_{ULID()}"


def _published_day(conn) -> tuple[str, str, str]:
    """org + project + one published run carrying one insight, in this transaction."""
    org_id, project_id = _uid("org"), _uid("proj")
    run_id, insight_id = _uid("dir"), _uid("din")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) "
            "VALUES (%s, %s, %s, %s)",
            (org_id, "Migration 321", org_id.lower(), "tester"),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
            "VALUES (%s, %s, %s, %s, %s)",
            (project_id, org_id, "Migration 321", project_id.lower(), "tester"),
        )
        cur.execute(
            "INSERT INTO app.daily_insight_runs (id, project_id, insight_date, status) "
            "VALUES (%s, %s, %s, 'published')",
            (run_id, project_id, "2026-07-23"),
        )
        cur.execute(
            """
            INSERT INTO app.daily_insights
                (id, run_id, project_id, insight_date, slot, payload, payload_hash)
            VALUES (%s, %s, %s, %s, 0, %s::jsonb, %s)
            """,
            (insight_id, run_id, project_id, "2026-07-23", "{}", "0" * 64),
        )
    return project_id, run_id, insight_id


def _retract(conn, insight_id: str, *, by: str = "tester", reason: str = "wrong window"):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.daily_insights SET retracted_at = now(), retracted_by = %s, "
            "retracted_reason = %s WHERE id = %s",
            (by, reason, insight_id),
        )


@pytest.mark.pg_owner
def test_the_three_retraction_columns_move_together_or_not_at_all(live_postgres):
    _project_id, _run_id, insight_id = _published_day(live_postgres)

    for column in ("retracted_at = now()", "retracted_by = 'tester'"):
        with pytest.raises(psycopg.errors.CheckViolation):
            with live_postgres.transaction():
                with live_postgres.cursor() as cur:
                    cur.execute(
                        f"UPDATE app.daily_insights SET {column} WHERE id = %s",  # noqa: S608
                        (insight_id,),
                    )

    # A blank reason is the same absence wearing a value.
    with pytest.raises(psycopg.errors.CheckViolation):
        with live_postgres.transaction():
            _retract(live_postgres, insight_id, reason="   ")


@pytest.mark.pg_owner
def test_a_retraction_is_never_unmade(live_postgres):
    _project_id, _run_id, insight_id = _published_day(live_postgres)
    _retract(live_postgres, insight_id)

    for statement in (
        "UPDATE app.daily_insights SET retracted_at = NULL, retracted_by = NULL, "
        "retracted_reason = NULL WHERE id = %s",
        "UPDATE app.daily_insights SET retracted_by = 'someone-else' WHERE id = %s",
        "UPDATE app.daily_insights SET retracted_reason = 'a nicer reason' WHERE id = %s",
    ):
        with pytest.raises(psycopg.errors.CheckViolation):
            with live_postgres.transaction():
                with live_postgres.cursor() as cur:
                    cur.execute(statement, (insight_id,))


@pytest.mark.pg_owner
def test_the_claim_under_a_retraction_is_frozen_with_it(live_postgres):
    """Otherwise a republication swaps the prose beneath the withdrawal.

    Migration 324 widened 321's freeze from `payload`/`payload_hash` to the WHOLE
    row: the adversarial review of ae60c22a proved an identical-payload upsert
    could rewrite `run_id`/`identity`/`result_id` on a withdrawn claim -- a
    retraction silently re-attributed. After the withdrawal is filed, the row IS
    the record of it; nothing on it changes. (A standing row still moves freely:
    `record_run`'s upserts and `result_id` backfills all happen before any
    retraction exists.)
    """
    _project_id, _run_id, insight_id = _published_day(live_postgres)
    _retract(live_postgres, insight_id)

    with pytest.raises(psycopg.errors.CheckViolation):
        with live_postgres.transaction():
            with live_postgres.cursor() as cur:
                cur.execute(
                    "UPDATE app.daily_insights SET payload = %s::jsonb WHERE id = %s",
                    ('{"insight": {"title": "a different claim"}}', insight_id),
                )

    # Migration 324: EVERY column is frozen with the withdrawal, provenance
    # included -- not only the prose.
    with pytest.raises(psycopg.errors.CheckViolation):
        with live_postgres.transaction():
            with live_postgres.cursor() as cur:
                cur.execute(
                    "UPDATE app.daily_insights SET result_unavailable_reason = %s WHERE id = %s",
                    ("no published Semantic View covers this card", insight_id),
                )


@pytest.mark.pg_owner
def test_a_row_is_never_born_retracted(live_postgres):
    """An INSERT carrying the triple would file a withdrawal nobody performed."""
    project_id, run_id, _insight_id = _published_day(live_postgres)

    with pytest.raises(psycopg.errors.CheckViolation):
        with live_postgres.transaction():
            with live_postgres.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.daily_insights
                        (id, run_id, project_id, insight_date, slot, payload,
                         payload_hash, identity, retracted_at, retracted_by,
                         retracted_reason)
                    VALUES (%s, %s, %s, '2026-07-23', 4, '{}'::jsonb, 'h',
                            'tester', now(), 'tester', 'forged at birth')
                    """,
                    (_uid("din"), run_id, project_id),
                )


@pytest.mark.pg_owner
def test_the_erasure_hatch_survives_the_retraction_guard(live_postgres):
    """The guard is BEFORE UPDATE and adds NO delete guard -- deliberately.

    An append-only DELETE trigger here would have put this table inside the scope
    of `test_the_erasure_hatch_is_a_privilege_too.py` and made a right-to-erasure
    request depend on a DELETE privilege migration 061 never granted. A retraction
    is an editorial state; erasure is a different, audited operation.
    """
    _project_id, _run_id, insight_id = _published_day(live_postgres)
    _retract(live_postgres, insight_id)

    with live_postgres.cursor() as cur:
        cur.execute(
            """
            SELECT bool_or((t.tgtype & 8) <> 0) AS has_delete_trigger
            FROM pg_trigger t
            WHERE t.tgrelid = 'app.daily_insights'::regclass AND NOT t.tgisinternal
            """
        )
        assert cur.fetchone()[0] is not True

        # A retracted row is still erasable, by the cascade the org purge uses.
        cur.execute("DELETE FROM app.daily_insights WHERE id = %s", (insight_id,))
        assert cur.rowcount == 1
