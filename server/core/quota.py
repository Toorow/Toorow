"""toorow -- Quota engine with circuit breaker (Story 3.3, AC2, AC4, AC7).

Exports:
  class TokenBucket    -- rolling-window token bucket (injectable clock)
  class CircuitBreaker -- per-platform breaker (CLOSED/OPEN states)
  class QuotaEngine    -- orchestrates bucket + breaker per platform
  class RateLimitError -- raised by pull functions on HTTP 429

Module-level singleton + public function wrappers (same pattern as health_poller.py):
  register_platform(platform, window_seconds, budget_points, read_cost, write_cost)
  pre_check(platform, estimated_points, clock=...) -> tuple[bool, str]
  record_spend(platform, points, clock=...)
  record_rate_limit(platform, retry_after_seconds, clock=...)
  get_breaker_state(platform, clock=...) -> dict
  all_breaker_states(clock=...) -> list[dict]

Design decisions:
  - All methods accept an injectable ``clock`` parameter (default time.monotonic)
    so tests can control time without sleeping (HG-2, AC8).
  - TokenBucket uses a rolling window anchored to the first spend in the window.
    Window resets the first time spend() is called after the window expires.
  - CircuitBreaker opens for max(retry_after, window_seconds) so the breaker
    stays open at least as long as the platform's own rate-limit window.
  - QuotaEngine is platform-agnostic (AD-2 / HG-1): no provider-specific strings
    or numeric budgets appear in this file. All config comes from register_platform().
  - ASCII-only log strings (L-3, AI-03).

AD-2 / HG-1: this file MUST NOT contain any provider-specific names or numeric quota values.
HG-5: no GCP imports.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RateLimitError exception (AC7)
# ---------------------------------------------------------------------------


class RateLimitError(Exception):
    """Raised by module pull functions on HTTP 429 responses.

    The worker catches this and calls quota.record_rate_limit() then re-queues.

    Attributes:
        platform: The platform/provider key (e.g. from manifest "name" field).
        retry_after: Seconds from the Retry-After header, or None if absent.
    """

    def __init__(self, platform: str, retry_after: int | None = None):
        self.platform = platform
        self.retry_after = retry_after
        super().__init__(f"rate_limited: platform={platform} retry_after={retry_after}")


# ---------------------------------------------------------------------------
# TokenBucket (AC2 -- T2.1)
# ---------------------------------------------------------------------------


class TokenBucket:
    """Rolling-window token bucket with injectable clock (HG-2).

    The window is anchored to the first spend (or construction time).
    On each spend()/can_spend() call: if clock() - window_start >= window_seconds,
    the bucket resets to full budget and window_start advances to clock().

    Args:
        window_seconds: Duration of each rolling window in seconds.
        budget_points: Maximum points available per window.
        clock: Callable returning a monotonic float timestamp. Default: time.monotonic.
            Story 4.1 (AI-23): injectable so tests can control time without sleeping.
            Pass a lambda (e.g. ``lambda: synthetic_time``) to freeze or advance time.
            Existing tests that reset ``_window_start = 0.0`` after construction still
            work; the clock parameter removes the need for that workaround going forward.
    """

    def __init__(
        self,
        window_seconds: int,
        budget_points: int,
        clock=time.monotonic,
    ) -> None:
        self.window_seconds = window_seconds
        self.budget_points = budget_points
        self._balance: int = budget_points
        self._clock = clock  # AI-23: injected clock (default time.monotonic)
        self._window_start: float = clock()

    def _maybe_reset(self, clock) -> None:
        """Reset balance if the current window has expired."""
        now = clock()
        if now - self._window_start >= self.window_seconds:
            self._balance = self.budget_points
            self._window_start = now

    def can_spend(self, points: int, clock=time.monotonic) -> bool:
        """Return True if ``points`` can be debited without exceeding budget.

        Does NOT mutate state.
        """
        now = clock()
        if now - self._window_start >= self.window_seconds:
            # Window would reset -- full budget available
            return points <= self.budget_points
        return points <= self._balance

    def spend(self, points: int, clock=time.monotonic) -> bool:
        """Check and deduct ``points`` if available. Returns True on success."""
        self._maybe_reset(clock)
        if points <= self._balance:
            self._balance -= points
            return True
        return False

    def remaining(self, clock=time.monotonic) -> int:
        """Return current remaining balance (after window reset if expired)."""
        self._maybe_reset(clock)
        return self._balance

    def reset_at(self, clock=time.monotonic) -> float:
        """Return the monotonic timestamp when the current window expires."""
        self._maybe_reset(clock)
        return self._window_start + self.window_seconds


# ---------------------------------------------------------------------------
# CircuitBreaker (AC2 -- T2.2)
# ---------------------------------------------------------------------------


class CircuitBreaker:
    """Per-platform circuit breaker with CLOSED/OPEN states (injectable clock).

    States:
        CLOSED: normal operation.
        OPEN:   platform is paused; is_open() returns True until recovery window elapses.

    The recovery window is set at trip() time and equals
    ``max(retry_after_seconds or 0, window_seconds)``.

    Args:
        platform: Platform key (used only for logging).
        window_seconds: Default recovery window in seconds.
    """

    def __init__(self, platform: str, window_seconds: int) -> None:
        self.platform = platform
        self.window_seconds = window_seconds
        self._tripped_at: float | None = None
        self._recovery_window: int = window_seconds

    def trip(self, clock=time.monotonic, *, retry_after: int | None = None) -> None:
        """Open the breaker. Records trip timestamp and computes recovery window.

        recovery_window = max(retry_after or 0, window_seconds)

        Logs at WARNING with ASCII-only message (AI-03).
        """
        now = clock()
        self._tripped_at = now
        self._recovery_window = max(retry_after or 0, self.window_seconds)
        recovers_at = now + self._recovery_window
        logger.warning(
            "quota_breaker_tripped: platform=%s tripped_at=%.0f recovers_at=%.0f",
            self.platform,
            now,
            recovers_at,
        )

    def is_open(self, clock=time.monotonic) -> bool:
        """Return True if the breaker is open AND the recovery window has not elapsed."""
        if self._tripped_at is None:
            return False
        return clock() < self._tripped_at + self._recovery_window

    def reset(self, clock=time.monotonic) -> None:  # noqa: ARG002
        """Close the breaker (called after successful window-boundary recovery)."""
        self._tripped_at = None

    def state_dict(self, clock=time.monotonic) -> dict:
        """Return the breaker state as a dict.

        Shape: {"platform": str, "state": "open"|"closed",
                "tripped_at": float|None, "recovers_at": float|None}
        """
        tripped_at = self._tripped_at
        recovers_at = (tripped_at + self._recovery_window) if tripped_at is not None else None
        state = "open" if self.is_open(clock) else "closed"
        return {
            "platform": self.platform,
            "state": state,
            "tripped_at": tripped_at,
            "recovers_at": recovers_at,
        }


# ---------------------------------------------------------------------------
# QuotaEngine (AC2 -- T2.3)
# ---------------------------------------------------------------------------


class QuotaEngine:
    """Orchestrates TokenBucket + CircuitBreaker per platform.

    Platform-agnostic (AD-2 / HG-1): no provider-specific strings or numbers
    appear in this class. All config is provided via register_platform().
    """

    def __init__(self) -> None:
        self._buckets: dict[str, TokenBucket] = {}
        self._breakers: dict[str, CircuitBreaker] = {}
        self._configs: dict[str, dict] = {}
        # Coarse lock guarding all mutations and reads of _buckets / _breakers.
        # State is process-local and resets on restart (known limitation; Phase B
        # will persist breaker trips to Postgres so they survive rolling restarts).
        self._lock = threading.Lock()

    def get_config(self, platform: str) -> dict | None:
        """Return the registered quota config for *platform* (None if absent)."""
        with self._lock:
            return self._configs.get(platform)

    def register_platform(
        self,
        platform: str,
        window_seconds: int,
        budget_points: int,
        read_cost: int,
        write_cost: int,
    ) -> None:
        """Register quota parameters for a platform.

        Called by loader.py after manifest validation (AC6).
        Safe to call multiple times; later calls overwrite earlier registration.
        """
        with self._lock:
            self._configs[platform] = {
                "window_seconds": window_seconds,
                "budget_points": budget_points,
                "read_cost": read_cost,
                "write_cost": write_cost,
            }
            self._buckets[platform] = TokenBucket(window_seconds, budget_points)
            self._breakers[platform] = CircuitBreaker(platform, window_seconds)
        logger.info(
            "quota: registered platform=%s window=%ds budget=%d read_cost=%d write_cost=%d",
            platform,
            window_seconds,
            budget_points,
            read_cost,
            write_cost,
        )

    def pre_check(
        self, platform: str, estimated_points: int, clock=time.monotonic
    ) -> tuple[bool, str]:
        """Return (can_proceed, reason).

        Reasons:
          "no_quota"        -- platform not registered; pass-through.
          "breaker_open"    -- breaker is open; pause the job.
          "budget_exhausted"-- insufficient budget; pause the job.
          "ok"              -- all checks passed.
        """
        with self._lock:
            if platform not in self._configs:
                return True, "no_quota"

            breaker = self._breakers[platform]
            if breaker.is_open(clock):
                return False, "breaker_open"

            bucket = self._buckets[platform]
            if not bucket.can_spend(estimated_points, clock):
                return False, "budget_exhausted"

            return True, "ok"

    def record_spend(self, platform: str, points: int, clock=time.monotonic) -> None:
        """Deduct ``points`` from the bucket. No-op if platform not registered."""
        with self._lock:
            if platform not in self._buckets:
                return
            self._buckets[platform].spend(points, clock)

    def record_rate_limit(
        self,
        platform: str,
        retry_after_seconds: int | None,
        clock=time.monotonic,
    ) -> None:
        """Record a 429 response: trip the breaker and log at WARNING (AC5)."""
        with self._lock:
            if platform not in self._breakers:
                logger.warning(
                    "quota_rate_limit_unregistered: platform=%s retry_after=%s",
                    platform,
                    retry_after_seconds,
                )
                return
            self._breakers[platform].trip(clock, retry_after=retry_after_seconds)

    def get_breaker_state(self, platform: str, clock=time.monotonic) -> dict:
        """Return state_dict() for the named platform, or a closed placeholder."""
        with self._lock:
            if platform not in self._breakers:
                return {
                    "platform": platform,
                    "state": "closed",
                    "tripped_at": None,
                    "recovers_at": None,
                }
            return self._breakers[platform].state_dict(clock)

    def all_breaker_states(self, clock=time.monotonic) -> list[dict]:
        """Return state_dict() for all registered platforms."""
        with self._lock:
            return [b.state_dict(clock) for b in self._breakers.values()]


# ---------------------------------------------------------------------------
# Module-level singleton + public API wrappers (same pattern as health_poller.py)
# ---------------------------------------------------------------------------

_engine = QuotaEngine()


def register_platform(
    platform: str,
    window_seconds: int,
    budget_points: int,
    read_cost: int,
    write_cost: int,
) -> None:
    """Register quota parameters for *platform* (delegates to module singleton)."""
    _engine.register_platform(platform, window_seconds, budget_points, read_cost, write_cost)


def get_read_cost(platform: str) -> int:
    """Public accessor for a platform's declared read cost (0 when no quota).

    review-3-3 F-2: callers must not reach into private engine attributes.
    """
    cfg = _engine.get_config(platform)
    return int(cfg.get("read_cost", 0)) if cfg else 0


def pre_check(
    platform: str, estimated_points: int, clock=time.monotonic
) -> tuple[bool, str]:
    """Return (can_proceed, reason) for *platform* (delegates to module singleton)."""
    return _engine.pre_check(platform, estimated_points, clock)


def record_spend(platform: str, points: int, clock=time.monotonic) -> None:
    """Deduct *points* from *platform*'s bucket (delegates to module singleton)."""
    _engine.record_spend(platform, points, clock)


def record_rate_limit(
    platform: str, retry_after_seconds: int | None, clock=time.monotonic
) -> None:
    """Record a 429 response for *platform* (delegates to module singleton)."""
    _engine.record_rate_limit(platform, retry_after_seconds, clock)


def get_breaker_state(platform: str, clock=time.monotonic) -> dict:
    """Return breaker state dict for *platform* (delegates to module singleton)."""
    return _engine.get_breaker_state(platform, clock)


def all_breaker_states(clock=time.monotonic) -> list[dict]:
    """Return breaker state for all registered platforms (delegates to module singleton)."""
    return _engine.all_breaker_states(clock)
