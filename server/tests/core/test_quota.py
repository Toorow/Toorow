"""Unit tests for server/core/quota.py (Story 3.3, AC8).

All tests inject a synthetic clock via the ``clock`` parameter.
No test calls time.sleep() (HG-2).

Coverage:
  TokenBucket:
    - spend within budget
    - spend exhausts budget
    - window expiry resets balance (advance clock past window_seconds)
    - can_spend vs spend distinction
  CircuitBreaker:
    - trip / is_open / reset lifecycle
    - recovery after window_seconds (advance clock)
  QuotaEngine:
    - pre_check returns (True, "no_quota") for unregistered platform
    - pre_check blocked when breaker open
    - pre_check blocked when budget exhausted
    - record_rate_limit trips breaker
    - all_breaker_states returns list
  RateLimitError:
    - attributes (platform, retry_after)
"""

from __future__ import annotations

import os

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_clock(start: float = 0.0):
    """Return a mutable list [t] and a clock callable that reads list[0]."""
    t = [start]

    def clock():
        return t[0]

    return t, clock


# ---------------------------------------------------------------------------
# TokenBucket tests
# ---------------------------------------------------------------------------


class TestTokenBucket:
    def test_spend_within_budget(self):
        """spend() returns True and reduces remaining() when within budget."""
        from core.quota import TokenBucket

        t, clock = _make_clock(0.0)
        bucket = TokenBucket(window_seconds=60, budget_points=10)
        bucket._window_start = 0.0  # anchor to t=0

        assert bucket.spend(5, clock) is True
        assert bucket.remaining(clock) == 5

    def test_spend_exhausts_budget(self):
        """spend() returns False when insufficient budget."""
        from core.quota import TokenBucket

        t, clock = _make_clock(0.0)
        bucket = TokenBucket(window_seconds=60, budget_points=10)
        bucket._window_start = 0.0

        assert bucket.spend(10, clock) is True
        assert bucket.spend(1, clock) is False

    def test_window_expiry_resets_balance(self):
        """Advancing clock past window_seconds resets balance to budget_points."""
        from core.quota import TokenBucket

        t, clock = _make_clock(0.0)
        bucket = TokenBucket(window_seconds=60, budget_points=10)
        bucket._window_start = 0.0

        # Exhaust the budget
        assert bucket.spend(10, clock) is True
        assert bucket.remaining(clock) == 0

        # Advance clock past window
        t[0] = 61.0
        assert bucket.remaining(clock) == 10
        # Can spend again
        assert bucket.spend(10, clock) is True

    def test_can_spend_does_not_mutate_state(self):
        """can_spend() does not deduct from balance."""
        from core.quota import TokenBucket

        t, clock = _make_clock(0.0)
        bucket = TokenBucket(window_seconds=60, budget_points=10)
        bucket._window_start = 0.0

        assert bucket.can_spend(5, clock) is True
        # Balance must be unchanged
        assert bucket.remaining(clock) == 10

    def test_can_spend_returns_false_when_insufficient(self):
        """can_spend() returns False when points exceed remaining balance."""
        from core.quota import TokenBucket

        t, clock = _make_clock(0.0)
        bucket = TokenBucket(window_seconds=60, budget_points=10)
        bucket._window_start = 0.0

        bucket.spend(8, clock)
        assert bucket.can_spend(5, clock) is False

    def test_reset_at_returns_window_end(self):
        """reset_at() returns window_start + window_seconds."""
        from core.quota import TokenBucket

        t, clock = _make_clock(0.0)
        bucket = TokenBucket(window_seconds=60, budget_points=10)
        bucket._window_start = 0.0

        assert bucket.reset_at(clock) == 60.0

    def test_can_spend_resets_view_after_window(self):
        """can_spend() treats full budget as available when window has expired."""
        from core.quota import TokenBucket

        t, clock = _make_clock(0.0)
        bucket = TokenBucket(window_seconds=60, budget_points=5)
        bucket._window_start = 0.0

        # Exhaust
        bucket.spend(5, clock)
        assert bucket.can_spend(5, clock) is False

        # Advance past window -- can_spend should see full budget without mutating
        t[0] = 61.0
        assert bucket.can_spend(5, clock) is True
        # But because can_spend doesn't mutate, the balance is still stale until
        # we call spend() or remaining() which triggers _maybe_reset
        # (this is acceptable per the spec: can_spend uses peek logic)


# ---------------------------------------------------------------------------
# CircuitBreaker tests
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_initial_state_closed(self):
        """A new CircuitBreaker is closed."""
        from core.quota import CircuitBreaker

        t, clock = _make_clock(0.0)
        cb = CircuitBreaker("test-platform", window_seconds=60)
        assert cb.is_open(clock) is False

    def test_trip_opens_breaker(self):
        """trip() opens the breaker."""
        from core.quota import CircuitBreaker

        t, clock = _make_clock(0.0)
        cb = CircuitBreaker("test-platform", window_seconds=60)
        cb.trip(clock)
        assert cb.is_open(clock) is True

    def test_breaker_open_within_window(self):
        """is_open() returns True before the recovery window elapses."""
        from core.quota import CircuitBreaker

        t, clock = _make_clock(0.0)
        cb = CircuitBreaker("test-platform", window_seconds=60)
        cb.trip(clock)

        t[0] = 59.0
        assert cb.is_open(clock) is True

    def test_breaker_closes_after_window(self):
        """is_open() returns False once the recovery window has elapsed."""
        from core.quota import CircuitBreaker

        t, clock = _make_clock(0.0)
        cb = CircuitBreaker("test-platform", window_seconds=60)
        cb.trip(clock)

        t[0] = 60.0  # exactly at window boundary
        assert cb.is_open(clock) is False

    def test_reset_closes_breaker_immediately(self):
        """reset() closes the breaker regardless of time."""
        from core.quota import CircuitBreaker

        t, clock = _make_clock(0.0)
        cb = CircuitBreaker("test-platform", window_seconds=60)
        cb.trip(clock)
        assert cb.is_open(clock) is True

        cb.reset(clock)
        assert cb.is_open(clock) is False

    def test_state_dict_closed(self):
        """state_dict() returns closed state with None timestamps when not tripped."""
        from core.quota import CircuitBreaker

        t, clock = _make_clock(0.0)
        cb = CircuitBreaker("test-platform", window_seconds=60)
        sd = cb.state_dict(clock)

        assert sd["platform"] == "test-platform"
        assert sd["state"] == "closed"
        assert sd["tripped_at"] is None
        assert sd["recovers_at"] is None

    def test_state_dict_open(self):
        """state_dict() returns open state with correct timestamps when tripped."""
        from core.quota import CircuitBreaker

        t, clock = _make_clock(100.0)
        cb = CircuitBreaker("test-platform", window_seconds=60)
        cb.trip(clock)
        sd = cb.state_dict(clock)

        assert sd["state"] == "open"
        assert sd["tripped_at"] == 100.0
        assert sd["recovers_at"] == 160.0

    def test_trip_with_retry_after_extends_window(self):
        """trip() with retry_after > window_seconds extends recovery window."""
        from core.quota import CircuitBreaker

        t, clock = _make_clock(0.0)
        cb = CircuitBreaker("test-platform", window_seconds=60)
        cb.trip(clock, retry_after=120)  # longer than window_seconds

        # Should be open until t=120
        t[0] = 119.0
        assert cb.is_open(clock) is True

        t[0] = 120.0
        assert cb.is_open(clock) is False


# ---------------------------------------------------------------------------
# QuotaEngine tests
# ---------------------------------------------------------------------------


class TestQuotaEngine:
    def _make_engine(self):
        from core.quota import QuotaEngine

        return QuotaEngine()

    def test_pre_check_no_quota_unregistered_platform(self):
        """pre_check returns (True, 'no_quota') for unregistered platform."""
        engine = self._make_engine()
        t, clock = _make_clock(0.0)
        can_proceed, reason = engine.pre_check("unknown-platform", 1, clock)
        assert can_proceed is True
        assert reason == "no_quota"

    def test_pre_check_blocked_when_breaker_open(self):
        """pre_check returns (False, 'breaker_open') when breaker is open."""
        engine = self._make_engine()
        t, clock = _make_clock(0.0)

        engine.register_platform("test-p", 60, 100, 1, 3)
        engine._breakers["test-p"].trip(clock)

        can_proceed, reason = engine.pre_check("test-p", 1, clock)
        assert can_proceed is False
        assert reason == "breaker_open"

    def test_pre_check_blocked_when_budget_exhausted(self):
        """pre_check returns (False, 'budget_exhausted') when budget is gone."""
        engine = self._make_engine()
        t, clock = _make_clock(0.0)

        engine.register_platform("test-p", 60, 5, 1, 3)
        engine._buckets["test-p"]._window_start = 0.0
        # Exhaust the budget
        engine._buckets["test-p"].spend(5, clock)

        can_proceed, reason = engine.pre_check("test-p", 1, clock)
        assert can_proceed is False
        assert reason == "budget_exhausted"

    def test_pre_check_ok(self):
        """pre_check returns (True, 'ok') when all checks pass."""
        engine = self._make_engine()
        t, clock = _make_clock(0.0)

        engine.register_platform("test-p", 60, 100, 1, 3)
        engine._buckets["test-p"]._window_start = 0.0

        can_proceed, reason = engine.pre_check("test-p", 1, clock)
        assert can_proceed is True
        assert reason == "ok"

    def test_record_rate_limit_trips_breaker(self):
        """record_rate_limit() opens the breaker for the platform."""
        engine = self._make_engine()
        t, clock = _make_clock(0.0)

        engine.register_platform("test-p", 60, 100, 1, 3)
        assert engine._breakers["test-p"].is_open(clock) is False

        engine.record_rate_limit("test-p", None, clock)
        assert engine._breakers["test-p"].is_open(clock) is True

    def test_record_rate_limit_unregistered_platform_no_crash(self):
        """record_rate_limit() on unregistered platform logs warning, does not crash."""
        engine = self._make_engine()
        t, clock = _make_clock(0.0)
        # Must not raise
        engine.record_rate_limit("unknown-platform", 30, clock)

    def test_get_breaker_state_unregistered_returns_closed_placeholder(self):
        """get_breaker_state() returns closed placeholder for unregistered platform."""
        engine = self._make_engine()
        t, clock = _make_clock(0.0)

        sd = engine.get_breaker_state("unknown", clock)
        assert sd["platform"] == "unknown"
        assert sd["state"] == "closed"
        assert sd["tripped_at"] is None
        assert sd["recovers_at"] is None

    def test_all_breaker_states_returns_list_for_registered_platforms(self):
        """all_breaker_states() returns one entry per registered platform."""
        engine = self._make_engine()
        t, clock = _make_clock(0.0)

        engine.register_platform("platform-a", 60, 100, 1, 3)
        engine.register_platform("platform-b", 300, 60, 1, 3)

        states = engine.all_breaker_states(clock)
        assert len(states) == 2
        platform_names = {s["platform"] for s in states}
        assert platform_names == {"platform-a", "platform-b"}

    def test_record_spend_deducts_from_bucket(self):
        """record_spend() deducts points from the bucket."""
        engine = self._make_engine()
        t, clock = _make_clock(0.0)

        engine.register_platform("test-p", 60, 100, 1, 3)
        engine._buckets["test-p"]._window_start = 0.0

        engine.record_spend("test-p", 10, clock)
        assert engine._buckets["test-p"].remaining(clock) == 90

    def test_record_spend_noop_for_unregistered(self):
        """record_spend() is a no-op for unregistered platform."""
        engine = self._make_engine()
        t, clock = _make_clock(0.0)
        # Must not raise
        engine.record_spend("unknown", 5, clock)


# ---------------------------------------------------------------------------
# RateLimitError tests
# ---------------------------------------------------------------------------


class TestRateLimitError:
    def test_attributes(self):
        """RateLimitError carries platform and retry_after attributes."""
        from core.quota import RateLimitError

        exc = RateLimitError("some-platform", retry_after=60)
        assert exc.platform == "some-platform"
        assert exc.retry_after == 60
        assert isinstance(exc, Exception)

    def test_retry_after_defaults_to_none(self):
        """RateLimitError.retry_after defaults to None."""
        from core.quota import RateLimitError

        exc = RateLimitError("some-platform")
        assert exc.retry_after is None

    def test_str_representation(self):
        """RateLimitError str contains platform and retry_after."""
        from core.quota import RateLimitError

        exc = RateLimitError("test-platform", 30)
        assert "test-platform" in str(exc)
        assert "30" in str(exc)


# ---------------------------------------------------------------------------
# Module-level singleton delegation tests
# ---------------------------------------------------------------------------


class TestModuleLevelSingleton:
    def test_module_functions_delegate_to_engine(self):
        """Module-level pre_check/record_spend/etc. delegate to _engine."""
        import core.quota as quota_module

        # Create a fresh engine to avoid polluting shared state
        original_engine = quota_module._engine
        from core.quota import QuotaEngine

        quota_module._engine = QuotaEngine()

        try:
            t, clock = _make_clock(0.0)
            # Unregistered platform -> no_quota
            can_proceed, reason = quota_module.pre_check("x", 1, clock)
            assert can_proceed is True
            assert reason == "no_quota"

            # Register and check state
            quota_module.register_platform("x", 60, 10, 1, 1)
            quota_module._engine._buckets["x"]._window_start = 0.0
            states = quota_module.all_breaker_states(clock)
            assert any(s["platform"] == "x" for s in states)

            # record_rate_limit -> trips breaker
            quota_module.record_rate_limit("x", None, clock)
            state = quota_module.get_breaker_state("x", clock)
            assert state["state"] == "open"
        finally:
            quota_module._engine = original_engine
