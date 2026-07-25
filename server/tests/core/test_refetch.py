"""Unit tests for the tiered refetch ladder (Story 26.1, C).

Neutral vocabulary only: no provider names appear in this file (AD-2).
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from core.refetch import (
    CADENCE_MONTHLY,
    CADENCE_NIGHTLY,
    CADENCE_WEEKLY,
    REFETCH_KEY,
    compute_refetch_windows,
    get_refetch,
    get_refetch_for_provider,
    resolve_refetch_cadence,
    validate_refetch,
    windows_for_nightly_dispatch,
)

LADDER = {"nightly_days": 3, "weekly_days": 14, "monthly_days": 45}

# Reference dates (checked against a 2026 calendar):
#   2026-07-21 is a Tuesday      -> nightly
#   2026-07-20 is a Monday       -> weekly
#   2026-07-01 is the 1st (Wed)  -> monthly
#   2026-06-01 is BOTH a Monday and the 1st -> monthly wins
TUESDAY = date(2026, 7, 21)
MONDAY = date(2026, 7, 20)
FIRST_OF_MONTH = date(2026, 7, 1)
MONDAY_FIRST = date(2026, 6, 1)


# ---------------------------------------------------------------------------
# Manifest contract
# ---------------------------------------------------------------------------


class TestContract:
    def test_valid_ladder_has_no_errors(self):
        assert validate_refetch(LADDER) == []

    def test_non_object_is_refused(self):
        assert validate_refetch([3, 14, 45]) == ["refetch must be an object"]

    def test_missing_key_is_refused(self):
        errors = validate_refetch({"nightly_days": 3, "weekly_days": 14})
        assert any("monthly_days" in e for e in errors)

    def test_non_integer_is_refused(self):
        errors = validate_refetch({**LADDER, "nightly_days": "3"})
        assert any("nightly_days" in e for e in errors)

    def test_bool_is_not_an_integer(self):
        errors = validate_refetch({**LADDER, "nightly_days": True})
        assert any("nightly_days" in e for e in errors)

    def test_out_of_range_is_refused(self):
        assert validate_refetch({**LADDER, "monthly_days": 0})
        assert validate_refetch({**LADDER, "monthly_days": 366})

    def test_decreasing_ladder_is_refused(self):
        errors = validate_refetch(
            {"nightly_days": 30, "weekly_days": 14, "monthly_days": 45}
        )
        assert any("non-decreasing" in e for e in errors)

    def test_get_refetch_absent_block_returns_none(self):
        assert get_refetch({"name": "neutral"}) is None
        assert get_refetch(None) is None

    def test_get_refetch_valid_block_returned(self):
        manifest = {"name": "neutral", REFETCH_KEY: LADDER}
        assert get_refetch(manifest) == LADDER

    def test_get_refetch_invalid_block_returns_none_and_warns(self, caplog):
        import logging

        manifest = {"name": "neutral", REFETCH_KEY: {"nightly_days": 3}}
        with caplog.at_level(logging.WARNING, logger="core.refetch"):
            assert get_refetch(manifest) is None
        assert any("invalid contract" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Cadence resolution
# ---------------------------------------------------------------------------


class TestCadence:
    def test_plain_day_is_nightly(self):
        assert resolve_refetch_cadence(TUESDAY) == CADENCE_NIGHTLY

    def test_monday_is_weekly(self):
        assert resolve_refetch_cadence(MONDAY) == CADENCE_WEEKLY

    def test_first_of_month_is_monthly(self):
        assert resolve_refetch_cadence(FIRST_OF_MONTH) == CADENCE_MONTHLY

    def test_monday_first_of_month_resolves_monthly(self):
        """The deepest rung wins when the 1st falls on a Monday."""
        assert MONDAY_FIRST.weekday() == 0
        assert resolve_refetch_cadence(MONDAY_FIRST) == CADENCE_MONTHLY


# ---------------------------------------------------------------------------
# Window computation
# ---------------------------------------------------------------------------


class TestWindows:
    def test_nightly_single_window_ending_yesterday(self):
        windows = compute_refetch_windows(TUESDAY, LADDER)
        assert windows == [{"date_from": "2026-07-18", "date_to": "2026-07-20"}]

    def test_weekly_widens_to_weekly_days(self):
        windows = compute_refetch_windows(MONDAY, LADDER)
        assert windows == [{"date_from": "2026-07-06", "date_to": "2026-07-19"}]

    def test_monthly_splits_into_31_day_windows_oldest_first(self):
        windows = compute_refetch_windows(FIRST_OF_MONTH, LADDER)
        # 45 days ending 2026-06-30: [2026-05-17..2026-06-16][2026-06-17..2026-06-30]
        assert windows == [
            {"date_from": "2026-05-17", "date_to": "2026-06-16"},
            {"date_from": "2026-06-17", "date_to": "2026-06-30"},
        ]

    def test_windows_are_contiguous_and_cover_exactly_the_span(self):
        windows = compute_refetch_windows(TUESDAY, {**LADDER, "monthly_days": 100},
                                          cadence=CADENCE_MONTHLY)
        total = 0
        previous_to = None
        for window in windows:
            d_from = date.fromisoformat(window["date_from"])
            d_to = date.fromisoformat(window["date_to"])
            assert d_from <= d_to
            assert (d_to - d_from).days + 1 <= 31
            if previous_to is not None:
                assert (d_from - previous_to).days == 1
            previous_to = d_to
            total += (d_to - d_from).days + 1
        assert total == 100
        assert previous_to == TUESDAY.replace(day=20)  # yesterday

    def test_explicit_cadence_overrides_date_resolution(self):
        windows = compute_refetch_windows(TUESDAY, LADDER, cadence=CADENCE_WEEKLY)
        assert windows[0]["date_from"] == "2026-07-07"
        assert windows[-1]["date_to"] == "2026-07-20"

    def test_unknown_cadence_raises_value_error(self):
        with pytest.raises(ValueError, match="unknown refetch cadence"):
            compute_refetch_windows(TUESDAY, LADDER, cadence="hourly")


# ---------------------------------------------------------------------------
# Provider resolution + the scheduler hook
# ---------------------------------------------------------------------------


class TestProviderResolution:
    def _loaded(self, name: str, manifest: dict):
        loaded = MagicMock()
        loaded.name = name
        loaded.manifest = manifest
        return loaded

    def test_provider_with_ladder_resolves(self):
        modules = [self._loaded("neutral-module", {"name": "neutral-module",
                                                   REFETCH_KEY: LADDER})]
        with patch("core.main.get_loaded_modules", return_value=modules):
            assert get_refetch_for_provider("neutral-module") == LADDER

    def test_unknown_provider_returns_none(self):
        with patch("core.main.get_loaded_modules", return_value=[]):
            assert get_refetch_for_provider("neutral-module") is None

    def test_registry_failure_returns_none(self):
        with patch("core.main.get_loaded_modules", side_effect=RuntimeError("boom")):
            assert get_refetch_for_provider("neutral-module") is None


class TestNightlyDispatchHook:
    DEFAULT = {"date_from": "2026-07-19", "date_to": "2026-07-20"}

    def test_absent_block_returns_exactly_the_default_window(self):
        """AD-22: no manifest block => bit-identical dispatch behaviour."""
        with patch("core.refetch.get_refetch_for_provider", return_value=None):
            windows = windows_for_nightly_dispatch("neutral-module", TUESDAY, self.DEFAULT)
        assert windows == [self.DEFAULT]
        assert windows[0] is self.DEFAULT

    def test_declared_block_returns_ladder_windows(self):
        with patch("core.refetch.get_refetch_for_provider", return_value=LADDER):
            windows = windows_for_nightly_dispatch("neutral-module", TUESDAY, self.DEFAULT)
        assert windows == [{"date_from": "2026-07-18", "date_to": "2026-07-20"}]

    def test_monthly_run_returns_multiple_windows(self):
        with patch("core.refetch.get_refetch_for_provider", return_value=LADDER):
            windows = windows_for_nightly_dispatch(
                "neutral-module", FIRST_OF_MONTH, self.DEFAULT
            )
        assert len(windows) == 2

    def test_resolution_failure_falls_back_to_default(self):
        with patch(
            "core.refetch.get_refetch_for_provider", side_effect=RuntimeError("boom")
        ):
            windows = windows_for_nightly_dispatch("neutral-module", TUESDAY, self.DEFAULT)
        assert windows == [self.DEFAULT]

    # -- Review 26.1 F-7 (product decision): the ladder WIDENS, never narrows --

    def test_user_window_deeper_than_rung_keeps_default_identity(self):
        """A per-datastream window (AI-46 date_window_days) deeper than the
        rung wins: effective = max(user, ladder) and the returned window is
        the IDENTICAL object (no rewrite of a user config)."""
        wide_default = {"date_from": "2026-06-11", "date_to": "2026-07-20"}  # 40 days
        with patch("core.refetch.get_refetch_for_provider", return_value=LADDER):
            windows = windows_for_nightly_dispatch("neutral-module", TUESDAY, wide_default)
        assert windows == [wide_default]
        assert windows[0] is wide_default

    def test_user_window_equal_to_rung_keeps_default_identity(self):
        three_days = {"date_from": "2026-07-18", "date_to": "2026-07-20"}
        with patch("core.refetch.get_refetch_for_provider", return_value=LADDER):
            windows = windows_for_nightly_dispatch("neutral-module", TUESDAY, three_days)
        assert windows == [three_days]
        assert windows[0] is three_days

    def test_weekly_rung_never_narrows_a_wider_user_window(self):
        """Monday resolves weekly (14 d); a 20-day user window stays."""
        wide_default = {"date_from": "2026-06-30", "date_to": "2026-07-19"}  # 20 days
        with patch("core.refetch.get_refetch_for_provider", return_value=LADDER):
            windows = windows_for_nightly_dispatch("neutral-module", MONDAY, wide_default)
        assert windows == [wide_default]
        assert windows[0] is wide_default

    def test_monthly_rung_never_narrows_a_wider_user_window(self):
        """1st of month resolves monthly (45 d); a 60-day user window stays."""
        wide_default = {"date_from": "2026-05-02", "date_to": "2026-06-30"}  # 60 days
        with patch("core.refetch.get_refetch_for_provider", return_value=LADDER):
            windows = windows_for_nightly_dispatch(
                "neutral-module", FIRST_OF_MONTH, wide_default
            )
        assert windows == [wide_default]
        assert windows[0] is wide_default

    def test_rung_widens_a_narrower_user_window(self):
        """The other side of the max(): weekly 14 d > user 5 d => ladder."""
        narrow_default = {"date_from": "2026-07-15", "date_to": "2026-07-19"}  # 5 days
        with patch("core.refetch.get_refetch_for_provider", return_value=LADDER):
            windows = windows_for_nightly_dispatch("neutral-module", MONDAY, narrow_default)
        assert windows == [{"date_from": "2026-07-06", "date_to": "2026-07-19"}]


class TestSchedulerBranchPoint:
    """The scheduler's datastream dispatch consumes the hook (Story 26.1 C)."""

    def _ds_rows(self, date_window_days=None):
        return [
            {
                "ds_id": "ds_1",
                "project_id": "proj_1",
                "module_name": "neutral-module",
                "refetch_days": 3,
                "date_window_days": date_window_days,
                "connection_ref_id": "conn_1",
                "cr_status": "active",
                "cr_enabled": True,
            }
        ]

    def _mock_get_connection(self, rows):
        cur = MagicMock()
        cur.description = [(k,) for k in rows[0]]
        cur.fetchall.return_value = [tuple(r.values()) for r in rows]
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=conn)
        ctx.__exit__ = MagicMock(return_value=False)
        get_connection = MagicMock(return_value=ctx)
        return get_connection

    def test_ladder_module_enqueues_one_job_per_window(self):
        from core.scheduler import _dispatch_nightly_datastreams

        queue = MagicMock()
        queue.enqueue_pull.return_value = {"job_id": "j", "pull_id": "p", "state": "queued"}
        get_connection = self._mock_get_connection(self._ds_rows())

        with patch("core.refetch.get_refetch_for_provider", return_value=LADDER):
            jobs, count = _dispatch_nightly_datastreams(
                FIRST_OF_MONTH, "scheduler", queue, get_connection
            )
        assert count == 1
        assert queue.enqueue_pull.call_count == 2  # monthly ladder: 45 d -> 2 windows
        first_call = queue.enqueue_pull.call_args_list[0]
        assert first_call.args == ("conn_1", "2026-05-17", "2026-06-16")

    def test_module_without_ladder_keeps_single_window(self):
        from core.scheduler import _dispatch_nightly_datastreams

        queue = MagicMock()
        queue.enqueue_pull.return_value = {"job_id": "j", "pull_id": "p", "state": "queued"}
        get_connection = self._mock_get_connection(self._ds_rows())

        with patch("core.refetch.get_refetch_for_provider", return_value=None):
            jobs, count = _dispatch_nightly_datastreams(
                TUESDAY, "scheduler", queue, get_connection
            )
        assert count == 1
        assert queue.enqueue_pull.call_count == 1
        call = queue.enqueue_pull.call_args
        # refetch_days=3 legacy window: [yesterday-2, yesterday] -- unchanged.
        assert call.args == ("conn_1", "2026-07-18", "2026-07-20")
        assert call.kwargs == {"requested_by": "scheduler", "datastream_id": "ds_1"}

    def test_user_date_window_days_wider_than_ladder_wins(self):
        """Review 26.1 F-7 end-to-end at the dispatch: a per-datastream
        date_window_days (AI-46) of 60 beats the 45-day monthly rung --
        one job with the user's 60-day window, no ladder split."""
        from core.scheduler import _dispatch_nightly_datastreams

        queue = MagicMock()
        queue.enqueue_pull.return_value = {"job_id": "j", "pull_id": "p", "state": "queued"}
        get_connection = self._mock_get_connection(self._ds_rows(date_window_days=60))

        with patch("core.refetch.get_refetch_for_provider", return_value=LADDER):
            jobs, count = _dispatch_nightly_datastreams(
                FIRST_OF_MONTH, "scheduler", queue, get_connection
            )
        assert count == 1
        assert queue.enqueue_pull.call_count == 1
        call = queue.enqueue_pull.call_args
        # 60 days ending yesterday (2026-06-30): [2026-05-02 .. 2026-06-30].
        assert call.args == ("conn_1", "2026-05-02", "2026-06-30")


# ---------------------------------------------------------------------------
# AD-2 / HG-1: no provider vocabulary in the core file
# ---------------------------------------------------------------------------


class TestNoProviderVocabulary:
    def test_core_file_has_no_provider_names(self):
        source = (
            Path(__file__).parents[2] / "core" / "refetch.py"
        ).read_text(encoding="utf-8").lower()
        forbidden = [
            "meta",
            "facebook",
            "google",
            "ga4",
            "gsc",
            "tiktok",
            "shopify",
            "stripe",
            "hubspot",
            "klaviyo",
            "linkedin",
            "github",
            "bing",
            "microsoft",
            "amazon",
            "pinterest",
        ]
        hits = [w for w in forbidden if re.search(rf"\b{re.escape(w)}\b", source)]
        assert not hits, f"refetch.py must contain no provider vocabulary: {hits}"
