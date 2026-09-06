"""AI-302 -- the populate_failed alert names the pull that raised it.

THE DEFECT THIS PINS. `app.connection_health.status = 'populate_failed'` is
sticky and closes every Datastream behind one authorization, and migration 276
records WHICH collection raised it (`populate_failed_pull_id/_verdict/_at`).
But `core.health_enrichment` -- the one producer of the `populate_failed` alert
a report surface ever shows (`ui/shell/src/WidgetShell.tsx` renders
`alert.message` verbatim) -- selected none of the three columns and said only
"Pull landed no rows for the expected window": a status with no pull to open,
which is what made the 2026-08-12 silence illegible.

These tests hold the whole in-process path: the SQL asks for the identity, the
worst-row arbitration carries it, and the alert quotes it -- pull id, verdict,
date -- next to the repair gesture, in the same vocabulary as `queue.py`'s
`connection_unhealthy` refusal.

Deliberately NOT here: whether a `partial` verdict should raise the sticky red
at all. That arbitration (same question as AI-101) is open and this file must
not close it by accident -- `partial` appears below only to check the SENTENCE
it produces once the red already exists.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

_CREATED = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
_RAISED = datetime(2026, 8, 12, 9, 58, 24, tzinfo=timezone.utc)


def _fake_db(rows, executed_sql: list | None = None):
    """A get_connection double that answers the health read with *rows*.

    Optionally records every executed statement into *executed_sql*, so a test
    can assert the QUESTION the module asks -- the only property a double is
    entitled to check (see `test_get_daily_report._make_health_db`).
    """

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, params=None):
            if executed_sql is not None:
                executed_sql.append(sql if isinstance(sql, str) else str(sql))

        def fetchall(self):
            return list(rows)

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def cursor(self):
            return FakeCursor()

    @contextmanager
    def _get_connection():
        yield FakeConn()

    return _get_connection


def _enrich(rows, executed_sql: list | None = None) -> dict:
    from core.health_enrichment import enrich_envelope_with_health

    with patch("core.db.get_connection", new=_fake_db(rows, executed_sql)):
        return enrich_envelope_with_health(
            {}, "proj_EXAMPLE", date_from="2026-08-01", date_to="2026-08-14"
        )


def _populate_failed_alerts(envelope: dict) -> list[dict]:
    return [
        a
        for a in (envelope.get("meta", {}).get("alerts") or [])
        if a.get("code") == "populate_failed"
    ]


def test_the_query_asks_for_the_pull_identity_columns():
    """The read carries migration 276's three columns, or nothing downstream can.

    This is the exact line the AI-302 blocker pointed at: the alert branch could
    not name a pull because the SELECT never fetched one.
    """
    executed: list[str] = []
    _enrich([], executed)

    health_reads = [s for s in executed if "connection_health" in s]
    assert health_reads, "the enrichment never read connection health"
    for column in (
        "populate_failed_pull_id",
        "populate_failed_verdict",
        "populate_failed_at",
    ):
        assert column in health_reads[0], (
            f"the health read does not select {column}: the red stays anonymous\n"
            + health_reads[0]
        )


def test_populate_failed_alert_names_the_pull_its_verdict_and_its_date():
    envelope = _enrich(
        [("populate_failed", None, _CREATED, "pull_01M07JFX", "empty", _RAISED)]
    )

    (alert,) = _populate_failed_alerts(envelope)
    assert alert["severity"] == "error"  # review-3-5 F-3: the shell's RED chip
    # Machine-readable identity, so a screen can link instead of parsing prose.
    assert alert["pull_id"] == "pull_01M07JFX"
    assert alert["verdict"] == "empty"
    assert alert["raised_at"] == "2026-08-12T09:58:24+00:00"
    # The sentence a person reads names the collection, its date, and the
    # gesture that repairs -- never the technical cause.
    assert "pull_01M07JFX" in alert["message"]
    assert "2026-08-12" in alert["message"]
    assert "no rows" in alert["message"]
    assert "re-verify the reporting account" in alert["message"]
    assert "populate_failed" not in alert["message"], (
        "a status token is the database's vocabulary, not the reader's"
    )


def test_a_partial_verdict_reads_too_few_rows_not_no_rows():
    """`empty` and `partial` are two different facts and two different repairs.

    Whether `partial` SHOULD raise the sticky red is the open AI-101 arbitration
    and is not decided here -- this only pins that when such a red exists, the
    sentence does not claim the window landed nothing.
    """
    envelope = _enrich(
        [("populate_failed", None, _CREATED, "pull_01PART", "partial", _RAISED)]
    )

    (alert,) = _populate_failed_alerts(envelope)
    assert alert["verdict"] == "partial"
    assert "too few rows" in alert["message"]
    assert "no rows" not in alert["message"]


def test_a_red_raised_before_migration_276_is_not_given_an_invented_pull():
    """Rows predating the migration read NULL, which is the truth about them."""
    envelope = _enrich([("populate_failed", None, _CREATED, None, None, None)])

    (alert,) = _populate_failed_alerts(envelope)
    assert "pull_id" not in alert
    assert "verdict" not in alert
    assert "raised_at" not in alert
    assert "collection (" not in alert["message"] and "None" not in alert["message"]
    # Anonymous or not, the red still names the gesture that repairs.
    assert "re-verify the reporting account" in alert["message"]


def test_a_legacy_three_column_row_still_raises_the_alert_unnamed():
    """A caller feeding the pre-AI-302 row shape loses the name, not the red."""
    envelope = _enrich([("populate_failed", None, _CREATED)])

    (alert,) = _populate_failed_alerts(envelope)
    assert "pull_id" not in alert
    assert "re-verify the reporting account" in alert["message"]


def test_the_identity_shown_is_the_worst_rows_own():
    """A healthy neighbour's empty identity must not strip the red row's name."""
    fresh = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
    envelope = _enrich(
        [
            ("ok", fresh, _CREATED, None, None, None),
            ("populate_failed", None, _CREATED, "pull_01WORST", "empty", _RAISED),
        ]
    )

    (alert,) = _populate_failed_alerts(envelope)
    assert alert["pull_id"] == "pull_01WORST"


def test_a_naive_raised_at_is_read_as_utc():
    """Postgres TIMESTAMPTZ comes back aware, but a driver or a fixture may not."""
    naive = datetime(2026, 8, 12, 9, 58, 24)
    envelope = _enrich(
        [("populate_failed", None, _CREATED, "pull_01NAIVE", "empty", naive)]
    )

    (alert,) = _populate_failed_alerts(envelope)
    assert alert["raised_at"] == "2026-08-12T09:58:24+00:00"
    assert "2026-08-12" in alert["message"]
