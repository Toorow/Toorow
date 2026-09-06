"""AI-113 -- an arrival expectation that was written and read by nobody.

`app.datastream_arrival_monitors` is written at activation for the delivered
channels: "this Datastream expects a file every N minutes". Until now exactly
one statement in the repository touched that table -- the INSERT. Nothing read
it, so the expectation an operator set reached no surface at all.

And the monitor that DID run on those Datastreams was the wrong one. A delivered
feed has no extract ledger, on purpose: activation sets `schedule_mode='manual'`
so the nightly puller's `WHERE ds.schedule_mode = 'nightly'` never selects it.
The pull-based timeliness check therefore found nothing for yesterday and fired
"no valid extraction" -- every day, for every inbound Datastream, forever. A
monitor that cannot be satisfied trains people to ignore the channel it fires
on.

These tests pin both halves: the false alarm is gone, and the real expectation
is measured.
"""

from __future__ import annotations

import datetime as dt

import pytest


class _Cursor:
    def __init__(self, rows):
        self._rows = list(rows)
        self.queries: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=()):
        self.queries.append(sql)

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None


class _Conn:
    def __init__(self, rows):
        self._cursor = _Cursor(rows)

    def cursor(self):
        return self._cursor

    @property
    def queries(self):
        return self._cursor.queries


NOW = dt.datetime(2026, 8, 1, 12, 0, tzinfo=dt.timezone.utc)


def _fired(monkeypatch) -> list[dict]:
    """Capture what reaches the alert spine."""
    from core import infra_alerts

    calls: list[dict] = []
    monkeypatch.setattr(
        infra_alerts, "write_infra_firing", lambda **kwargs: calls.append(kwargs)
    )
    return calls


# ---------------------------------------------------------------------------
# The table is read at last.
# ---------------------------------------------------------------------------


def test_the_arrival_expectation_is_read():
    from core.dq_monitors import _arrival_monitor

    conn = _Conn([(1440, "owner@example.com")])
    monitor = _arrival_monitor(conn, "ds-1", "proj-1")

    assert monitor == {
        "expected_interval_minutes": 1440,
        "owner_person_id": "owner@example.com",
    }
    assert "datastream_arrival_monitors" in conn.queries[0]
    assert "state = 'active'" in conn.queries[0]


def test_a_datastream_without_an_expectation_reads_as_none():
    from core.dq_monitors import _arrival_monitor

    assert _arrival_monitor(_Conn([None]), "ds-1", "proj-1") is None


def test_an_unreadable_table_is_not_an_expectation():
    """Fail towards "no monitor", never towards a fabricated one."""
    from core.dq_monitors import _arrival_monitor

    class _Down:
        def cursor(self):
            raise RuntimeError("database unreachable")

    assert _arrival_monitor(_Down(), "ds-1", "proj-1") is None


# ---------------------------------------------------------------------------
# The false alarm is gone.
# ---------------------------------------------------------------------------


def test_a_delivered_feed_is_not_judged_against_the_pull_ledger(monkeypatch):
    """The defect this branch removes.

    `_check_timeliness` would call `get_extract_ledger`, find nothing -- because
    nothing pulls a delivered feed -- and fire every day. The branch must divert
    BEFORE that call, so the proof is that the pull ledger is never consulted.
    """
    import core.dq_monitors as dqm
    import core.extract_ledger as ledger

    consulted: list[str] = []
    monkeypatch.setattr(
        ledger, "get_extract_ledger", lambda *a, **kw: consulted.append("pull") or []
    )
    calls = _fired(monkeypatch)

    # An expectation exists, and a file arrived one minute ago.
    conn = _Conn([
        (1440, "owner@example.com"),                       # the monitor
        (NOW - dt.timedelta(minutes=1),),                  # last receipt
    ])
    fired = dqm._check_timeliness(
        "ds-1", "proj-1", "managed_feed", "Panel", conn,
        dt.date(2026, 7, 31), now_utc=NOW,
    )

    assert fired is False
    assert consulted == [], "the pull ledger was consulted for a delivered feed"
    assert calls == []


# ---------------------------------------------------------------------------
# The real expectation is measured.
# ---------------------------------------------------------------------------


def test_a_late_delivery_fires_with_the_evidence_that_explains_it(monkeypatch):
    import core.dq_monitors as dqm

    calls = _fired(monkeypatch)
    conn = _Conn([
        (60, "owner@example.com"),                          # hourly expectation
        (NOW - dt.timedelta(minutes=300),),                 # five hours ago
    ])

    fired = dqm._check_timeliness(
        "ds-1", "proj-1", "managed_feed", "Panel", conn,
        dt.date(2026, 7, 31), now_utc=NOW,
    )

    assert fired is True
    assert len(calls) == 1
    payload = calls[0]
    assert payload["alert_type"] == "dq_timeliness"
    metadata = payload["metadata"]
    assert metadata["monitor_kind"] == "arrival"
    assert metadata["expected_interval_minutes"] == 60
    assert metadata["minutes_since_last_delivery"] == 300
    # The owner travels with the alert: an alert nobody owns is one nobody acts on.
    assert metadata["owner_person_id"] == "owner@example.com"


def test_ordinary_lateness_inside_the_grace_does_not_fire(monkeypatch):
    """An alert that fires on ordinary variance is an alert people mute.

    One full interval of grace: a daily feed an hour late is not an incident.
    """
    import core.dq_monitors as dqm

    calls = _fired(monkeypatch)
    conn = _Conn([
        (1440, "owner@example.com"),
        (NOW - dt.timedelta(minutes=1500),),   # 25h on a daily feed
    ])

    assert dqm._check_timeliness(
        "ds-1", "proj-1", "managed_feed", "Panel", conn,
        dt.date(2026, 7, 31), now_utc=NOW,
    ) is False
    assert calls == []


def test_a_never_delivered_feed_is_measured_from_activation(monkeypatch):
    """A Datastream activated five minutes ago is not late."""
    import core.dq_monitors as dqm

    calls = _fired(monkeypatch)
    conn = _Conn([
        (1440, "owner@example.com"),
        (None,),                                  # never delivered
        (NOW - dt.timedelta(minutes=5),),         # activated five minutes ago
    ])

    assert dqm._check_timeliness(
        "ds-1", "proj-1", "managed_feed", "Panel", conn,
        dt.date(2026, 7, 31), now_utc=NOW,
    ) is False
    assert calls == []


def test_a_feed_that_never_delivered_and_is_long_past_due_fires(monkeypatch):
    import core.dq_monitors as dqm

    calls = _fired(monkeypatch)
    conn = _Conn([
        (60, "owner@example.com"),
        (None,),
        (NOW - dt.timedelta(minutes=600),),       # activated ten hours ago
    ])

    assert dqm._check_timeliness(
        "ds-1", "proj-1", "managed_feed", "Panel", conn,
        dt.date(2026, 7, 31), now_utc=NOW,
    ) is True
    assert "no file has ever been delivered" in calls[0]["message"]


@pytest.mark.parametrize("interval", [0, None])
def test_a_zero_expectation_never_fires(interval, monkeypatch):
    """Zero would fire the instant a Datastream was activated."""
    import core.dq_monitors as dqm

    calls = _fired(monkeypatch)
    conn = _Conn([(interval, "owner@example.com")])

    assert dqm._check_timeliness(
        "ds-1", "proj-1", "managed_feed", "Panel", conn,
        dt.date(2026, 7, 31), now_utc=NOW,
    ) is False
    assert calls == []


def test_a_pulled_datastream_keeps_the_pull_check(monkeypatch):
    """The branch must not swallow the behaviour it was added beside."""
    import core.dq_monitors as dqm
    import core.extract_ledger as ledger

    consulted: list[str] = []
    monkeypatch.setattr(
        ledger,
        "get_extract_ledger",
        lambda *a, **kw: consulted.append("pull") or [{"status": "ok"}],
    )
    calls = _fired(monkeypatch)

    # No arrival monitor: this is an ordinary pulled Datastream.
    conn = _Conn([None])
    fired = dqm._check_timeliness(
        "ds-1", "proj-1", "shopify", "Orders", conn,
        dt.date(2026, 7, 31), now_utc=NOW,
    )

    assert consulted == ["pull"]
    assert bool(fired) is False
    assert calls == []
