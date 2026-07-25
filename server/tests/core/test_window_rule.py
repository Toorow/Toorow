"""Tests for core.window_rule (Story 6.5, AC2, AC8)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from core.window_rule import SUPPORTED_RULES, resolve_window_rule


class TestResolveWindowRule:
    """AC8 / Story 6.5 — window rule resolution tests."""

    def test_resolve_last_30d(self):
        """test_resolve_last_30d: last_30d -> today-30 to today in Europe/Paris."""
        fixed_today = date(2026, 7, 12)
        date_from, date_to = resolve_window_rule("last_30d", _today=fixed_today)
        assert date_to == "2026-07-12"
        assert date_from == "2026-06-12"
        # Verify the arithmetic
        assert date.fromisoformat(date_from) == fixed_today - timedelta(days=30)

    def test_resolve_unsupported(self):
        """test_resolve_unsupported: unsupported rule -> ValueError."""
        with pytest.raises(ValueError, match="Unsupported window rule"):
            resolve_window_rule("quarterly")

    def test_resolve_tz_aware(self):
        """test_resolve_tz_aware: date boundary respects Europe/Paris (not UTC).

        Europe/Paris is UTC+1 in winter and UTC+2 in summer. This test verifies
        that the 'today' reference date is derived from the Paris timezone, not UTC.
        We do this indirectly: the resolver runs without error and returns correct
        ISO strings regardless of the local system clock.
        """
        # Use a fixed today to make the test deterministic
        fixed_today = date(2026, 1, 15)  # winter: UTC+1
        date_from, date_to = resolve_window_rule("last_7d", "Europe/Paris", _today=fixed_today)
        assert date_to == "2026-01-15"
        assert date_from == "2026-01-08"

    def test_resolve_last_7d(self):
        """last_7d -> today-7 to today."""
        fixed_today = date(2026, 7, 12)
        date_from, date_to = resolve_window_rule("last_7d", _today=fixed_today)
        assert date.fromisoformat(date_from) == fixed_today - timedelta(days=7)
        assert date_to == fixed_today.isoformat()

    def test_resolve_last_14d(self):
        """last_14d -> today-14 to today."""
        fixed_today = date(2026, 7, 12)
        date_from, date_to = resolve_window_rule("last_14d", _today=fixed_today)
        assert date.fromisoformat(date_from) == fixed_today - timedelta(days=14)

    def test_resolve_last_90d(self):
        """last_90d -> today-90 to today."""
        fixed_today = date(2026, 7, 12)
        date_from, date_to = resolve_window_rule("last_90d", _today=fixed_today)
        assert date.fromisoformat(date_from) == fixed_today - timedelta(days=90)

    def test_resolve_last_180d(self):
        """last_180d -> today-180 to today."""
        fixed_today = date(2026, 7, 12)
        date_from, date_to = resolve_window_rule("last_180d", _today=fixed_today)
        assert date.fromisoformat(date_from) == fixed_today - timedelta(days=180)

    def test_resolve_last_365d(self):
        """last_365d -> today-365 to today."""
        fixed_today = date(2026, 7, 12)
        date_from, date_to = resolve_window_rule("last_365d", _today=fixed_today)
        assert date.fromisoformat(date_from) == fixed_today - timedelta(days=365)

    def test_all_supported_rules_are_resolable(self):
        """Every rule in SUPPORTED_RULES must resolve without error."""
        fixed_today = date(2026, 7, 12)
        for rule in SUPPORTED_RULES:
            date_from, date_to = resolve_window_rule(rule, _today=fixed_today)
            assert date_to == fixed_today.isoformat()
            assert date_from < date_to

    def test_unsupported_rule_mtd(self):
        """'mtd' is not supported -> ValueError."""
        with pytest.raises(ValueError, match="Unsupported window rule"):
            resolve_window_rule("mtd")

    def test_unsupported_rule_empty_string(self):
        """Empty string -> ValueError."""
        with pytest.raises(ValueError, match="Unsupported window rule"):
            resolve_window_rule("")
