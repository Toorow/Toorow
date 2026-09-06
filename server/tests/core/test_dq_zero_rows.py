"""Story 59.4: a window that returned nothing, and the four times that is not news.

The check alone, on the pattern of `test_dq_monitors.py` (`:11-14`, "all DB calls
mocked"): the ledger is the only input, so mocking it is mocking the world.

The five cases the story's test plan names, in its order:

1. `empty` with enough history -> fires, once, naming the window;
2. `empty` without enough history -> `not_applicable` NAMING the days found, and
   no firing -- never a pass;
3. `never_fetched` -> never;
4. an empty ledger -> never;
5. `window_offset_days = 3` and a date inside the offset shadow -> never. That is
   the case the story exists to honour, and it is unreachable from data: the
   column holds 1 on 1421 rows of 1421, so only a fixture reaches it.

Then the three the acceptance adds: an unreadable ledger is `unavailable` and
never a pass; the firing carries the four values `write_infra_firing` used to
invent; and `row_count` is never read -- it is `None` on 100% of ledger days
(`extract_ledger.py:448-458`), so a check that read it would answer "fine"
forever.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("DQ_MONITORS_ENABLED", "true")

WINDOW = date(2026, 8, 6)
#: 2026-08-07 in Europe/Paris, so an offset of 1 makes WINDOW the last fetchable
#: day and the guard of case 5 is the only thing that can move it.
NOW = datetime(2026, 8, 7, 10, 0, tzinfo=timezone.utc)

MONITOR = {"monitor_id": "dqm_test", "monitor_version_id": "dqmv_test"}


def _ds(**overrides) -> dict:
    ds = {
        "id": "ds_zero_rows_fixture",
        "project_id": "proj_EXAMPLE",
        "org_id": "org_EXAMPLE",
        "module_name": "ga4",
        "name": "Zero rows fixture",
        "window_offset_days": 1,
        "config": {},
    }
    ds.update(overrides)
    return ds


def _entry(day: date, status: str, **overrides) -> dict:
    """One ledger day, shaped as `extract_ledger._day_to_ledger_entry` shapes it.

    `row_count` is `None` and `row_count_reason` is `measured_per_window` on
    purpose: that is what 100% of the repository's ledger days hold, because no
    window is one day wide.
    """
    entry = {
        "date": day.isoformat(),
        "status": status,
        "row_count": None,
        "row_count_reason": "measured_per_window",
        "expected_rows": None,
        "completeness_ratio": None,
        "pull_id": None if status == "never_fetched" else f"pull_{day.isoformat()}",
        "loaded_at": None,
        "job_state": None if status == "never_fetched" else "done",
        "execution_id": None if status == "never_fetched" else "dse_test_run",
        "extract_count": 0 if status == "never_fetched" else 1,
        "provenance": None,
    }
    entry.update(overrides)
    return entry


def _ledger(*, empty_day: bool = True, active_days: int = 5) -> list[dict]:
    """31 days ending at WINDOW: *active_days* `ok` days, then the judged day."""
    entries = [
        _entry(WINDOW - timedelta(days=offset), "never_fetched")
        for offset in range(30, 0, -1)
    ]
    for index in range(active_days):
        entries[index] = _entry(WINDOW - timedelta(days=30 - index), "ok")
    entries.append(
        _entry(WINDOW, "empty" if empty_day else "ok", pull_id="pull_judged")
    )
    return entries


def _run(ds: dict, entries, **kwargs):
    """Call the check with the ledger mocked, and hand back what it wrote."""
    from core import dq_monitors

    firings: list[dict] = []
    recorded: list[dict] = []

    def _record(**call):
        recorded.append(call)
        return {"evaluation_id": "dqe_test", "issues": [{"id": "dqi_test"}]}

    ledger = (
        MagicMock(side_effect=entries)
        if callable(entries)
        else MagicMock(return_value=entries)
    )
    with (
        patch("core.extract_ledger.get_extract_ledger", ledger),
        patch("core.dq_zero_rows.derive_monitor", return_value=MONITOR) as derive,
        patch("core.dq_zero_rows.record_verdict", side_effect=_record),
        patch(
            "core.infra_alerts.write_infra_firing",
            side_effect=lambda **call: firings.append(call),
        ),
    ):
        verdict = dq_monitors._check_zero_rows(
            ds, MagicMock(), kwargs.pop("window_date", WINDOW), NOW, **kwargs
        )
    return verdict, firings, recorded, derive


# ---------------------------------------------------------------------------
# 1. Empty, on a source that has been producing.
# ---------------------------------------------------------------------------


def test_an_empty_window_on_a_producing_source_fires():
    from core import dq_monitors

    verdict, firings, recorded, _ = _run(_ds(), _ledger(active_days=5))

    assert bool(verdict) is True
    assert verdict.status == dq_monitors.STATUS_EVALUATED
    assert verdict.detail["active_days"] == 5
    assert len(firings) == 1, "one measurement, one finding -- never one per day"
    assert firings[0]["alert_type"] == "dq_zero_rows"
    assert recorded[0]["outcome"] == "fail"
    assert recorded[0]["fired"] is True


def test_three_active_days_are_enough_and_two_are_not():
    """The arbitrage's threshold, at its exact boundary.

    Three separate days carrying rows in the last month prove a source produces;
    one day is a fluke. The boundary is where a threshold is either a rule or a
    number somebody typed.
    """
    fired_at_three, firings_three, _, _ = _run(_ds(), _ledger(active_days=3))
    fired_at_two, firings_two, _, _ = _run(_ds(), _ledger(active_days=2))

    assert bool(fired_at_three) is True
    assert len(firings_three) == 1
    assert bool(fired_at_two) is False
    assert firings_two == []
    assert fired_at_two.detail["active_days"] == 2


def test_the_judged_day_is_never_its_own_evidence():
    """An `ok` day at the window date would count itself if nobody excluded it."""
    from core import dq_zero_rows

    entries = _ledger(active_days=3)
    evidence = dq_zero_rows.activity_evidence(entries, WINDOW)
    assert evidence["active_days"] == 3
    assert WINDOW.isoformat() not in evidence["active_dates"]


def test_the_finding_names_the_window_and_never_the_rows():
    """Arbitrage 5, and the four values `write_infra_firing` used to invent.

    `observed_value = 0`, `threshold = 0`, `pull_ids = '{}'` and above all
    `window_date = date.today()` were written in the SQL literal
    (`infra_alerts.py:335,346`). A finding about the 6th, written on the 8th, said
    the 8th -- false, not approximate.
    """
    _verdict, firings, _recorded, _ = _run(_ds(), _ledger(active_days=5))

    firing = firings[0]
    assert firing["window_date"] == WINDOW
    assert firing["observed_value"] == 0
    assert firing["threshold"] == 1
    assert firing["pull_ids"] == ["pull_judged"]
    # The measurement and the evidence, never the rows -- there are none by nature.
    assert firing["metadata"]["active_days"] == 5
    assert firing["metadata"]["min_active_days"] == 3
    assert firing["metadata"]["lookback_days"] == 31
    assert firing["metadata"]["execution_id"] == "dse_test_run"
    assert "rows" not in firing["metadata"]


def test_the_run_comes_from_the_ledger_entry_and_costs_no_query():
    """Arbitrage 4: `entry["execution_id"]` (`extract_ledger.py:469,473`)."""
    entries = _ledger(active_days=5)
    entries[-1]["execution_id"] = "dse_the_run_that_saw_it"

    verdict, firings, recorded, _ = _run(_ds(), entries)

    assert verdict.detail["execution_id"] == "dse_the_run_that_saw_it"
    assert recorded[0]["execution_id"] == "dse_the_run_that_saw_it"
    assert firings[0]["metadata"]["execution_id"] == "dse_the_run_that_saw_it"


def test_an_unresolvable_run_is_null_and_never_a_fabricated_id():
    entries = _ledger(active_days=5)
    entries[-1]["execution_id"] = None

    verdict, _firings, recorded, _ = _run(_ds(), entries)

    assert verdict.detail["execution_id"] is None
    assert recorded[0]["execution_id"] is None


def test_row_count_is_never_read():
    """`row_count` is `None` on 100% of ledger days (`extract_ledger.py:448-458`).

    No window in the repository is one day wide, so a check comparing
    `row_count == 0` would compare `None` and answer "fine" forever. Emptiness is
    read from the verdict, which is a statement about the window and therefore
    true of each of its days.
    """
    entries = _ledger(active_days=5)
    assert all(entry["row_count"] is None for entry in entries)

    verdict, firings, _recorded, _ = _run(_ds(), entries)

    assert bool(verdict) is True
    assert len(firings) == 1


# ---------------------------------------------------------------------------
# 2. Empty, with no proof the source produces.
# ---------------------------------------------------------------------------


def test_an_empty_window_without_history_is_not_applicable_and_names_its_days():
    from core import dq_monitors, dq_zero_rows

    verdict, firings, recorded, _ = _run(_ds(), _ledger(active_days=1))

    assert bool(verdict) is False
    assert verdict.status == dq_monitors.STATUS_NOT_APPLICABLE
    assert verdict.detail["reason"] == dq_zero_rows.NOT_HISTORICALLY_ACTIVE
    # NAMING the number found: "no anomaly" and "no monitor ran" have to stay
    # distinguishable (`epic-59:114-115`).
    assert verdict.detail["active_days"] == 1
    assert verdict.detail["min_active_days"] == 3
    assert firings == []
    assert recorded[0]["outcome"] == "not_applicable"
    assert recorded[0]["fired"] is False


def test_a_datastream_that_never_collected_derives_no_monitor():
    """Arbitrage 6, and what bounds the registry to 45 rather than 894.

    A monitor for a stream that has never collected is a governed object that
    asserts nothing, and a registry full of those is how "watched and quiet" stops
    meaning anything.
    """
    from core import dq_zero_rows

    never = [
        _entry(WINDOW - timedelta(days=offset), "never_fetched")
        for offset in range(30, -1, -1)
    ]
    verdict, firings, recorded, derive = _run(_ds(), never)

    assert bool(verdict) is False
    assert verdict.detail["reason"] == dq_zero_rows.NO_LEDGER_HISTORY
    assert verdict.detail["collected_days"] == 0
    assert firings == []
    assert recorded == [], "no governed evaluation for a stream that cannot evaluate"
    derive.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Every status that is not `empty`.
# ---------------------------------------------------------------------------


def test_never_fetched_never_fires():
    from core import dq_monitors, dq_zero_rows

    entries = _ledger(active_days=5)
    entries[-1] = _entry(WINDOW, "never_fetched")

    verdict, firings, recorded, _ = _run(_ds(), entries)

    assert bool(verdict) is False
    assert verdict.status == dq_monitors.STATUS_NOT_APPLICABLE
    assert verdict.detail["reason"] == dq_zero_rows.NOT_A_COLLECTED_DAY
    assert firings == []
    assert recorded[0]["outcome"] == "not_applicable"


def test_failed_running_and_a_stopped_window_never_fire():
    """`cancelled` and `superseded` reach the ledger as `never_fetched`.

    `pull_job_states.py:94-116`, and a stream a person stopped is not a source
    anomaly. `failed` is not a source returning nothing either: it is a collection
    that did not happen.
    """
    from core import dq_zero_rows

    for status in ("failed", "running", "never_fetched"):
        entries = _ledger(active_days=5)
        entries[-1] = _entry(WINDOW, status)
        verdict, firings, _recorded, _ = _run(_ds(), entries)

        assert bool(verdict) is False, status
        assert verdict.detail["reason"] == dq_zero_rows.NOT_A_COLLECTED_DAY, status
        assert firings == [], status


def test_a_window_that_carried_rows_is_the_only_pass():
    from core import dq_monitors

    for status in ("ok", "partial"):
        entries = _ledger(active_days=5)
        entries[-1] = _entry(WINDOW, status)
        verdict, firings, recorded, _ = _run(_ds(), entries)

        assert bool(verdict) is False, status
        assert verdict.status == dq_monitors.STATUS_EVALUATED, status
        assert firings == [], status
        assert recorded[0]["outcome"] == "pass", status


# ---------------------------------------------------------------------------
# 4. An empty ledger, and an unreadable one.
# ---------------------------------------------------------------------------


def test_an_empty_ledger_never_fires():
    from core import dq_monitors, dq_zero_rows

    verdict, firings, recorded, derive = _run(_ds(), [])

    assert bool(verdict) is False
    assert verdict.status == dq_monitors.STATUS_NOT_APPLICABLE
    assert verdict.detail["reason"] == dq_zero_rows.NO_LEDGER_DAY
    assert firings == []
    assert recorded == []
    derive.assert_not_called()


def test_an_unreadable_ledger_is_unavailable_and_never_a_pass():
    """A check that could not run is not a check that passed.

    `ck_dq_evaluations_empty_is_not_a_pass` says the same thing one layer down.
    And it never raises: one flux must not take the nightly sweep down.
    """
    from core import dq_monitors, dq_zero_rows

    def _explode(*_args, **_kwargs):
        raise RuntimeError("relation app.pull_jobs does not exist")

    verdict, firings, recorded, _ = _run(_ds(), _explode)

    assert bool(verdict) is False
    assert verdict.status == dq_monitors.STATUS_UNAVAILABLE
    assert verdict.detail["reason"] == dq_zero_rows.LEDGER_UNREADABLE
    assert firings == []
    assert recorded == []


# ---------------------------------------------------------------------------
# 5. A day inside the extraction offset. THE case the story exists to honour.
# ---------------------------------------------------------------------------


def test_a_date_inside_the_offset_shadow_never_fires():
    """`epic-59:121-123`, and the guard is `effective_window_end`.

    A Datastream at offset 3 has NO row for WINDOW by construction:
    `scheduler.py:1335-1340` sets `end_date = yesterday - (offset - 1)`, which is
    2026-08-04 here. Firing on it would be firing on a day the product
    deliberately never asked for -- exactly what migration 206 repaired.

    Unreachable from data (the column holds 1 on 1421 rows of 1421), so the
    fixture is the only way to prove the branch is alive.
    """
    from core import dq_monitors, dq_zero_rows

    verdict, firings, recorded, derive = _run(
        _ds(window_offset_days=3), _ledger(active_days=5)
    )

    assert bool(verdict) is False
    assert verdict.status == dq_monitors.STATUS_NOT_APPLICABLE
    assert verdict.detail["reason"] == dq_zero_rows.INSIDE_EXTRACTION_OFFSET
    assert verdict.detail["last_fetchable_date"] == "2026-08-04"
    assert firings == []
    assert recorded == []
    derive.assert_not_called()


def test_the_same_offset_stream_is_judged_on_its_own_last_day():
    """The guard refuses the shadow, it does not silence the Datastream.

    An offset-3 stream still gets a verdict -- on 2026-08-04, the day the dispatch
    actually asked for.
    """
    entries = [
        _entry(date(2026, 8, 4) - timedelta(days=offset), "never_fetched")
        for offset in range(30, 0, -1)
    ]
    for index in range(5):
        entries[index] = _entry(date(2026, 8, 4) - timedelta(days=30 - index), "ok")
    entries.append(_entry(date(2026, 8, 4), "empty", pull_id="pull_judged"))

    verdict, firings, _recorded, _ = _run(
        _ds(window_offset_days=3), entries, window_date=date(2026, 8, 4)
    )

    assert bool(verdict) is True
    assert firings[0]["window_date"] == date(2026, 8, 4)


# ---------------------------------------------------------------------------
# The subject is the WINDOW, and the threshold is the published one.
# ---------------------------------------------------------------------------


def test_the_finding_names_the_pulls_whole_window_and_not_one_day():
    """Arbitrage 2: 123 of 130 windows cover 3 or 5 days.

    One finding per DAY would multiply one observation by three or by five. The
    window is derived from the days the ledger already attributed to the pull, so
    naming it costs no query.
    """
    entries = _ledger(active_days=5)
    for offset in (2, 1):
        entries[-1 - offset] = _entry(
            WINDOW - timedelta(days=offset), "empty", pull_id="pull_judged"
        )

    verdict, firings, recorded, _ = _run(_ds(), entries)

    assert len(firings) == 1
    assert firings[0]["metadata"]["window_start"] == "2026-08-04"
    assert firings[0]["metadata"]["window_end"] == WINDOW.isoformat()
    assert recorded[0]["window_start"] == date(2026, 8, 4)
    assert recorded[0]["window_end"] == WINDOW


def test_a_published_threshold_beats_the_environment_and_never_leaks():
    """The governed version's policy, and it must not become the next stream's.

    The sweep walks every Datastream in one process; a threshold parked in
    `os.environ` by one governed evaluation would silently judge the rest.
    """
    from core import dq_monitors, dq_zero_rows

    before = os.environ.get(dq_zero_rows.MIN_ACTIVE_DAYS_ENV)
    with (
        patch("core.extract_ledger.get_extract_ledger", return_value=_ledger(active_days=4)),
        patch("core.dq_zero_rows.derive_monitor", return_value=MONITOR),
        patch("core.dq_zero_rows.record_verdict", return_value={"evaluation_id": None}),
        patch("core.infra_alerts.write_infra_firing"),
    ):
        strict = dq_monitors._adapt_zero_rows(
            _ds(),
            MagicMock(),
            WINDOW,
            NOW,
            {"parameters": {"thresholds": {"zero_rows": 9}}},
        )

    assert bool(strict) is False, "4 active days is under a published threshold of 9"
    assert strict.detail["min_active_days"] == 9
    assert os.environ.get(dq_zero_rows.MIN_ACTIVE_DAYS_ENV) == before


def test_the_profile_is_in_both_vocabularies():
    """A profile in one and not the other is unpublishable or `unverifiable`.

    `controls_quality._validate_dq_payload:218-219` refuses publication of a check
    absent from `DQ_CHECKS`; `governed_evaluator:1205-1211` answers `unverifiable`
    for one absent from `CHECK_PROFILES`.
    """
    from core import controls_quality, dq_monitors, dq_zero_rows

    assert dq_zero_rows.CHECK_PROFILE in controls_quality.DQ_CHECKS
    assert dq_zero_rows.CHECK_PROFILE in dq_monitors.CHECK_PROFILES


def test_the_fingerprint_is_the_monitor_alone():
    """Arbitrage 7: one open issue per flux, the second empty window a recurrence.

    `uq_dq_issues_open_root_cause` is `(project_id, monitor_id,
    root_cause_fingerprint)` and the flux is carried by the monitor. Adding the
    window date would open one issue per date and make that index bind nothing --
    which story 59.3 already refused.
    """
    from core import dq_zero_rows

    first = dq_zero_rows.root_cause_fingerprint("dqm_a")
    again = dq_zero_rows.root_cause_fingerprint("dqm_a")
    other = dq_zero_rows.root_cause_fingerprint("dqm_b")

    assert first == again, "the same monitor keeps one fingerprint across windows"
    assert first != other
