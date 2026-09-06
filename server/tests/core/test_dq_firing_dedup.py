"""One fact is one alert -- AI-226, 2026-08-08.

`toorow-run-dq-monitors` fires every fifteen minutes, and `DQ_TIMELINESS_DUE_HOUR`
opens the gate at 09:00. From nine to midnight that is sixty ticks, and a
Datastream still missing yesterday is still missing it at every one of them. THAT
is what the dedup is for.

The disposable base holds 2421 `dq_timeliness` rows for 590 distinct Datastreams,
all one day, all `never_fetched` -- and story 59.4 re-measured them rather than
assume: `date_trunc('minute', fired_at)` gives thirteen distinct minutes, all on
2026-08-05, NONE on a quarter hour. Those rows are a harness driving the endpoint
by hand, NOT the tick. They illustrate the shape the dedup prevents; they are not
evidence of it, and a comment that made them the tick's output would be inventing
a cause for a number.

The dedup key is (project, kind, WHICH Datastream, which day), and the third term
was never a column -- it travelled inside `metadata`, concatenated into `message`.
Migration 229 gives it one and indexes the four together, partially.

These tests cover the two halves that live in Python: the writer must RESOLVE the
subject out of the metadata every check already fills, and it must treat a
suppressed duplicate as a normal outcome rather than an error. The index itself is
proved against a real Postgres in `test_dq_firing_dedup_pg.py`.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest


def _cursor(returning=("fire_NEW",)):
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone = MagicMock(return_value=returning)
    return cur


def _connection(cur):
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cur)
    conn.commit = MagicMock()
    return conn


def _write(metadata, returning=("fire_NEW",), **kwargs):
    from core import infra_alerts

    cur = _cursor(returning)
    conn = _connection(cur)
    with patch("core.db.get_connection") as get_connection:
        get_connection.return_value.__enter__ = MagicMock(return_value=conn)
        get_connection.return_value.__exit__ = MagicMock(return_value=False)
        infra_alerts.write_infra_firing(
            alert_type="dq_timeliness",
            project_id="proj_EXAMPLE",
            metric="timeliness",
            message="Timeliness: no valid extraction",
            metadata=metadata,
            **kwargs,
        )
    assert cur.execute.called, "the writer never reached the INSERT"
    sql, params = cur.execute.call_args[0]
    return sql, params


class TestTheSubjectBecomesAColumn:
    def test_the_datastream_is_read_from_the_metadata_every_check_fills(self):
        _sql, params = _write({"datastream_id": "flux_EXAMPLE", "window_date": "2026-08-04"})
        assert "flux_EXAMPLE" in params, (
            "the subject stayed inside the message; the dedup key cannot name it"
        )

    def test_an_infra_event_carries_no_subject_and_is_never_deduplicated(self):
        # `meta_alert` and `nango_revoke_failed` have no Datastream. NULL keeps them
        # out of the partial index entirely, which is the intent: their dedup is a
        # different question.
        _sql, params = _write(None)
        assert params[-1] is None

    @pytest.mark.parametrize("value", [{"nested": "dict"}, 42, "", "   ", None, []])
    def test_a_subject_that_is_not_an_identifier_is_refused_not_coerced(self, value):
        """A key built from a stray value silently stops deduplicating."""
        _sql, params = _write({"datastream_id": value})
        assert params[-1] is None


class TestARestatedFactIsNotAnError:
    def test_the_insert_yields_rather_than_raises_on_a_known_fact(self):
        sql, _params = _write({"datastream_id": "flux_EXAMPLE"})
        assert "ON CONFLICT DO NOTHING" in sql, (
            "a second tick would abort the transaction instead of yielding"
        )
        assert "RETURNING id" in sql, "nothing could tell a write from a suppression"

    def test_a_suppressed_duplicate_is_reported_as_such_and_never_as_a_failure(self, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="core.infra_alerts"):
            # `fetchone()` answering None is exactly what ON CONFLICT DO NOTHING does.
            _write({"datastream_id": "flux_EXAMPLE", "window_date": "2026-08-04"}, returning=None)

        messages = [record.getMessage() for record in caplog.records]
        assert any("firing_already_stated" in line for line in messages), messages
        assert not any(
            "write_infra_firing_failed" in line for line in messages
        ), "a re-stated fact was reported as a failed write"

    def test_a_written_firing_names_its_subject_and_its_day_in_the_log(self, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="core.infra_alerts"):
            _write({"datastream_id": "flux_EXAMPLE", "window_date": "2026-08-04"})

        written = [m for m in (r.getMessage() for r in caplog.records) if "write_infra_firing" in m]
        assert written, "the successful write said nothing"
        assert "flux_EXAMPLE" in written[0]
        assert "2026-08-04" in written[0]


def test_the_day_is_the_observed_one_not_today():
    """The dedup key is only stable if the date is the finding's, not the writer's."""
    _sql, params = _write({"datastream_id": "flux_EXAMPLE", "window_date": "2026-08-04"})
    assert date(2026, 8, 4) in params, (
        "the key would move every day and deduplicate nothing"
    )
