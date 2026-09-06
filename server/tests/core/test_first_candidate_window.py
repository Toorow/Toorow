"""The defect that stopped EVERY Datastream producing its first data.

MEASURED 2026-08-12, before this file existed. `app.datastream_activation_jobs`,
kind `candidate_materialization`: 17 jobs, 17 in `dead_letter`, zero ever `done`.
Every one died in 0.4 s, before any network call::

    TypeError: pull_audience_demographics() missing 2 required positional
               arguments: 'date_from' and 'date_to'

Three of the four callers of `enqueue_activation_work(kind=
"candidate_materialization")` pin no interval -- including the wizard's own
publish -- because the Datastream already DECLARES its window. The worker
answered `interval = None`, the driver's `context.get("interval") or {}` left
both dates None, and its final `if v is not None` dropped the two keys the pull
required. 222 pull functions across 37 of the 39 Connectors declare `date_from`
with no default, so the defect belonged to the driver and applied to everyone.

Every test below is offline: no database, no network. The doubles are written by
hand, because a MagicMock accepts every signature and the point of half of these
assertions is precisely which signature was called.
"""

from __future__ import annotations

from datetime import date

import pytest
from core import pull_window
from core.datastream_activation import ActivationValidationError

# ---------------------------------------------------------------------------
# The resolver: one arbitration, no longer four copies.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("row", "expected_days", "expected_source"),
    [
        # Rung 1 wins whenever it answers -- including over a legacy alias that
        # disagrees, which is the whole point of the precedence (AI-46).
        ({"date_window_days": 30, "refetch_days": 3}, 30, "date_window_days"),
        # Rung 2 only when rung 1 is absent or zero. The schema forbids both for
        # a real row; a pre-migration-023 row and a test mock can produce them.
        ({"date_window_days": None, "refetch_days": 5}, 5, "refetch_days"),
        ({"date_window_days": 0, "refetch_days": 5}, 5, "refetch_days"),
        # Rung 3, defensive only.
        ({"date_window_days": None, "refetch_days": None}, 3, "defensive_default"),
        ({"date_window_days": 0, "refetch_days": 0}, 3, "defensive_default"),
        # A value that does not name a day count is not a window.
        ({"date_window_days": "nonsense", "refetch_days": 4}, 4, "refetch_days"),
    ],
)
def test_the_precedence_is_the_one_that_was_arbitrated(row, expected_days, expected_source):
    length = pull_window.resolve_window_days(row)
    assert length.days == expected_days
    assert length.source == expected_source


def test_the_cadence_floor_widens_and_never_narrows():
    """AI-217, unchanged: a run never fetches less than the interval it covers."""
    widened = pull_window.resolve_window_days({"date_window_days": 3}, cadence="weekly")
    assert widened.days == 7
    assert widened.widened_for == "weekly", "a widened window must say why"

    kept = pull_window.resolve_window_days({"date_window_days": 30}, cadence="weekly")
    assert kept.days == 30, "a person who set thirty days keeps thirty"
    assert kept.widened_for is None


def test_the_offset_moves_the_END_and_the_span_is_preserved():
    """AI-145. A Connector with a three-day lag ends its window at J-3.

    The SPAN is what `date_window_days` means, so shifting the end must not
    shorten it: a source with a lag would otherwise silently fetch fewer days
    than the number on its own schedule screen.
    """
    window = pull_window.resolve_window(
        {"date_window_days": 7, "window_offset_days": 3},
        end_reference=date(2026, 8, 11),
    )
    assert window.date_to == "2026-08-09", "offset 3 ends the window two days earlier"
    assert window.date_from == "2026-08-03"
    span = date.fromisoformat(window.date_to) - date.fromisoformat(window.date_from)
    assert span.days + 1 == 7

    default_offset = pull_window.resolve_window(
        {"date_window_days": 7}, end_reference=date(2026, 8, 11)
    )
    assert default_offset.date_to == "2026-08-11", "the documented default ends at J-1"


def test_the_interval_leaves_in_ONE_spelling():
    """Three spellings exist downstream; a resolved window must be
    indistinguishable from a pinned one, so only the normalised pair leaves."""
    interval = pull_window.resolve_window(
        {"date_window_days": 2}, end_reference=date(2026, 8, 11)
    ).as_interval()
    assert interval == {"date_from": "2026-08-10", "date_to": "2026-08-11"}


def test_no_module_re_types_the_precedence_it_now_calls():
    """The regression that matters most: another copy appearing.

    SIX modules resolved this independently -- the nightly dispatcher, the hourly
    dispatcher, the first-candidate door, the schedule surface, the manual-run
    door and `Synchronize now`. The schedule surface omitted the cadence floor,
    so a weekly Datastream was dispatched with seven days while its own screen
    said three; the manual door and `Synchronize now` read `refetch_days` alone,
    so a Datastream set to thirty days was collected over three by the very
    gestures that claim to replay its schedule by hand. Each now CALLS, and this
    asserts the call is still there rather than trusting it.
    """
    from pathlib import Path

    server_root = Path(__file__).resolve().parents[2]
    for relative in (
        "core/scheduler.py",
        "core/datastream_first_candidate.py",
        "core/schedule_mcp.py",
        "core/datastream_collection_api.py",
        "core/bounded_recovery.py",
        "inbound/datastream_activation_worker.py",
    ):
        source = (server_root / relative).read_text(encoding="utf-8")
        assert "pull_window" in source, f"{relative} must resolve the window by call, not by copy"


def test_no_door_declares_a_default_window_of_its_own():
    """The shape the copies took, every time: a bare `or 3` next to a read.

    `refetch_days or 3`, `DEFAULT_SYNCHRONIZE_DAYS = 3`, `int(...) or 3` -- three
    spellings of a number that belongs to ONE module and to the column default
    behind it. A door that keeps its own is a door that will disagree with the
    schedule screen the day someone changes the other.
    """
    import re
    from pathlib import Path

    server_root = Path(__file__).resolve().parents[2]
    # Scoped to the PULL window on purpose: `_DEFAULT_LEDGER_DAYS` is how many
    # days a ledger SHOWS, which is a reading and not a collection.
    private_default = re.compile(
        r'(?:refetch_days|date_window_days)["\']?\s*\)?\s*or\s+\d'
        r'|DEFAULT_(?:SYNCHRONIZE|REFETCH|PULL|RETRIEVAL)\w*\s*=\s*\d'
    )
    for relative in (
        "core/datastream_collection_api.py",
        "core/bounded_recovery.py",
        "core/scheduler.py",
        "core/schedule_mcp.py",
    ):
        source = (server_root / relative).read_text(encoding="utf-8")
        found = private_default.findall(source)
        assert not found, f"{relative} declares its own window default: {found}"


# ---------------------------------------------------------------------------
# The driver: a NAMED refusal, never a TypeError reaching the queue.
# ---------------------------------------------------------------------------


def _pull_requiring_a_window(**recorded):
    """A pull shaped like the 222 real ones: the window has no default."""

    def pull(connection_id, project_id, pull_id, date_from: str, date_to: str, **kwargs):
        recorded["date_from"] = date_from
        recorded["date_to"] = date_to
        return {"row_count": 4}

    pull.recorded = recorded
    return pull


def test_a_candidate_with_no_window_refuses_by_name_instead_of_raising_TypeError(monkeypatch):
    """THE 17 DEAD LETTERS. This is the exact call that produced them."""
    from inbound.adapters import datastream_activation_drivers as drivers

    monkeypatch.setattr(
        "core.main.get_module_pull_fn", lambda *a, **k: _pull_requiring_a_window()
    )

    with pytest.raises(ActivationValidationError) as refusal:
        drivers.connector_pull_candidate(
            {
                "execution_id": "dse_window",
                "module": "example",
                "project_id": "proj_EXAMPLE",
                "connection_ref_id": "cr_EXAMPLE",
                # exactly what the wizard's publish enqueues: no interval.
            }
        )

    sentence = str(refusal.value)
    assert "date window" in sentence, "the refusal must name what is missing"
    assert "Retrieval window" in sentence, "and the setting that repairs it"
    assert "TypeError" not in sentence


def test_a_pull_that_gets_its_window_is_called_with_both_dates(monkeypatch):
    """The other half: a resolved window must actually REACH the Connector."""
    from inbound.adapters import datastream_activation_drivers as drivers

    pull = _pull_requiring_a_window()
    monkeypatch.setattr("core.main.get_module_pull_fn", lambda *a, **k: pull)

    result = drivers.connector_pull_candidate(
        {
            "execution_id": "dse_ok",
            "module": "example",
            "project_id": "proj_EXAMPLE",
            "connection_ref_id": "cr_EXAMPLE",
            "interval": {"date_from": "2026-07-13", "date_to": "2026-08-11"},
        }
    )

    assert pull.recorded["date_from"] == "2026-07-13"
    assert pull.recorded["date_to"] == "2026-08-11"
    assert result["row_count"] == 4


def test_a_missing_argument_that_is_NOT_the_window_names_itself_too(monkeypatch):
    """The class, not the instance: any required argument, not only the dates."""
    from inbound.adapters import datastream_activation_drivers as drivers

    def pull(connection_id, project_id, pull_id, date_from, date_to, advertiser_id):
        raise AssertionError("must not be called without advertiser_id")

    monkeypatch.setattr("core.main.get_module_pull_fn", lambda *a, **k: pull)

    with pytest.raises(ActivationValidationError, match="advertiser_id"):
        drivers.connector_pull_candidate(
            {
                "execution_id": "dse_arg",
                "module": "example",
                "project_id": "proj_EXAMPLE",
                "connection_ref_id": "cr_EXAMPLE",
                "interval": {"date_from": "2026-07-13", "date_to": "2026-08-11"},
            }
        )


def test_an_optional_argument_a_connector_never_declared_still_costs_nothing(monkeypatch):
    """The old filter's REASON survives: dropping a None is right when the
    parameter has a default, and only wrong when it does not."""
    from inbound.adapters import datastream_activation_drivers as drivers

    seen = {}

    def pull(connection_id, project_id, pull_id, date_from, date_to, dry_run=False):
        seen["dry_run"] = dry_run
        return {"row_count": 1}

    monkeypatch.setattr("core.main.get_module_pull_fn", lambda *a, **k: pull)

    drivers.connector_pull_candidate(
        {
            "execution_id": "dse_opt",
            "module": "example",
            "project_id": "proj_EXAMPLE",
            "connection_ref_id": "cr_EXAMPLE",
            "interval": {"date_from": "2026-08-10", "date_to": "2026-08-11"},
        }
    )
    assert seen["dry_run"] is False, "an undeclared optional keeps its own default"


# ---------------------------------------------------------------------------
# The worker: where a candidate with no pinned interval gets a real window.
# ---------------------------------------------------------------------------


class _TimezoneConn:
    """A connection that answers only the one question `project_timezone` asks."""

    def __init__(self, timezone_name: str = "UTC"):
        self._timezone = timezone_name

    def cursor(self):
        timezone_name = self._timezone

        class _Cursor:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def execute(self_inner, sql, params=None):
                assert "reporting_timezone" in str(sql)

            def fetchone(self_inner):
                return (timezone_name,)

        return _Cursor()


def test_a_first_candidate_collects_the_window_its_first_scheduled_run_would_have(monkeypatch):
    """The repair, at the link that was missing.

    A first candidate that shows a period no scheduled run will ever fetch would
    have the operator review a sample of nothing real, so the window is the
    Datastream's OWN declaration -- resolved by the dispatcher's resolver, ending
    on the project's last complete day.
    """
    from inbound import datastream_activation_worker as worker

    monkeypatch.setattr(
        "core.scheduler.project_yesterday", lambda _timezone: date(2026, 8, 11)
    )

    interval = worker._declared_window(
        _TimezoneConn(),
        project_id="proj_EXAMPLE",
        datastream={
            "date_window_days": 30,
            "refetch_days": 3,
            "window_offset_days": 1,
            "schedule_mode": "nightly",
        },
    )

    assert interval == {"date_from": "2026-07-13", "date_to": "2026-08-11"}
    span = date.fromisoformat(interval["date_to"]) - date.fromisoformat(interval["date_from"])
    assert span.days + 1 == 30, "the declared retrieval window, not a constant"


def test_the_declared_window_honours_the_offset_and_the_cadence(monkeypatch):
    from inbound import datastream_activation_worker as worker

    monkeypatch.setattr(
        "core.scheduler.project_yesterday", lambda _timezone: date(2026, 8, 11)
    )

    interval = worker._declared_window(
        _TimezoneConn(),
        project_id="proj_EXAMPLE",
        datastream={
            "date_window_days": 3,
            "refetch_days": 3,
            "window_offset_days": 3,
            "schedule_mode": "weekly",
        },
    )
    # Widened to the weekly floor (7), ending at J-3.
    assert interval == {"date_from": "2026-08-03", "date_to": "2026-08-09"}


def test_a_pinned_interval_is_never_overridden_by_the_declaration():
    """A caller that named a window asked for THAT window -- a bounded refetch
    must not be silently widened to the schedule's."""
    from inbound import datastream_activation_worker as worker

    pinned = worker._driver_interval({"interval": {"from": "2026-01-01", "to": "2026-01-02"}}, {})
    assert pinned == {"date_from": "2026-01-01", "date_to": "2026-01-02"}


# ---------------------------------------------------------------------------
# The queue: a dead letter that says something.
# ---------------------------------------------------------------------------


def test_an_untyped_failure_carries_its_TYPE_not_one_generic_word():
    """Half the reason this went unseen for so long.

    Seventeen dead letters all read `activation_work_failed`, so the table could
    not show that one whole class of jobs had never once succeeded.
    """
    from core.queue import _activation_error_code

    assert _activation_error_code(TypeError("missing 2 required positional arguments")) == (
        "type_error"
    )
    assert _activation_error_code(ValueError("x")) == "value_error"
    assert _activation_error_code(KeyError("datastream_id")) == "key_error"


def test_a_typed_refusal_still_wins_over_its_python_type():
    """`code` is the deliberate answer; the type is only the fallback that the
    generic used to occupy."""
    from core.queue import _activation_error_code

    assert _activation_error_code(ActivationValidationError("no window")) == (
        "invalid_datastream_activation"
    )
