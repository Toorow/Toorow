"""Unit tests for the generic async report flow (Story 26.1, A).

All flows use a fake monotonic clock (sleeper advances it) -- no real sleeps.
The in-memory store shares the same fake clock, so claim-TTL (F-1/F-2) and
ref-TTL (F-3) behaviour is provable without real time.
Neutral vocabulary only: no provider names appear in this file (AD-2).

Live-Postgres section (review 26.1 F-4): skipped unless TEST_POSTGRES_DSN is
set/reachable (pg_available pattern); applies migration 048 idempotently to
the disposable test database (the Supabase apply stays human-gated).
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from pathlib import Path

import pytest
from core.async_reports import (
    CLAIM_ACQUIRED,
    CLAIM_LOST,
    CLAIM_STALE_ACQUIRED,
    COMPLETED,
    EXPIRED,
    FAILED,
    PENDING,
    PROCESSING,
    REF_COMPLETED,
    REF_FAILED,
    REF_IN_FLIGHT,
    REF_SUBMITTING,
    AsyncReportFlow,
    DownloadLinkExpired,
    InMemoryReportRefStore,
    PostgresReportRefStore,
    default_deadline_seconds,
    run_async_report,
)
from core.pull_errors import ProviderTransientError

LEDGER_REF = "connref_alpha"
REQUEST_HASH = "hash_alpha_v1"

CLAIM_TTL = 900  # env default ASYNC_REPORT_CLAIM_TTL_SECONDS
REF_TTL_SECONDS = 7 * 86400  # env default ASYNC_REPORT_REF_TTL_DAYS


class FakeClock:
    """Monotonic fake clock; the injected sleeper advances it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class ScriptedFlow:
    """AsyncReportFlow factory driven by a scripted list of poll statuses."""

    def __init__(self, statuses, rows=None, download_exc=None):
        self.statuses = list(statuses)
        self.rows = rows if rows is not None else [{"col": 1}]
        self.download_exc = list(download_exc or [])
        self.submit_calls = 0
        self.poll_refs: list[str] = []
        self.download_refs: list[str] = []

    def submit(self) -> str:
        self.submit_calls += 1
        return f"ref_{self.submit_calls}"

    def poll(self, report_ref: str) -> str:
        self.poll_refs.append(report_ref)
        if self.statuses:
            return self.statuses.pop(0)
        return PROCESSING

    def download(self, report_ref: str):
        self.download_refs.append(report_ref)
        if self.download_exc:
            exc = self.download_exc.pop(0)
            if exc is not None:
                raise exc
        return self.rows

    def as_flow(self, request_hash: str = REQUEST_HASH) -> AsyncReportFlow:
        return AsyncReportFlow(
            submit=self.submit,
            poll=self.poll,
            download=self.download,
            request_hash=request_hash,
        )


def _run(scripted, *, store=None, deadline_seconds=600, clock=None):
    clock = clock or FakeClock()
    # The store shares the run's fake clock so age-based behaviour (claim
    # TTL, ref TTL) is deterministic.
    store = store if store is not None else InMemoryReportRefStore(clock=clock.clock)
    result = run_async_report(
        scripted.as_flow(),
        ledger_ref=LEDGER_REF,
        deadline_seconds=deadline_seconds,
        clock=clock.clock,
        sleeper=clock.sleep,
        store=store,
    )
    return result, store, clock


# ---------------------------------------------------------------------------
# Happy path + backoff
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_submit_poll_download_completes(self):
        scripted = ScriptedFlow([PENDING, PROCESSING, COMPLETED])
        result, store, _clock = _run(scripted)
        assert result["status"] == "completed"
        assert result["rows"] == [{"col": 1}]
        assert result["report_ref"] == "ref_1"
        assert result["resumed"] is False
        assert scripted.submit_calls == 1
        assert store.get(LEDGER_REF, REQUEST_HASH)["state"] == REF_COMPLETED

    def test_poll_backoff_is_progressive_and_capped(self):
        """Waits double from 15 s up to the 120 s cap (env defaults)."""
        scripted = ScriptedFlow([PENDING] * 6 + [COMPLETED])
        _result, _store, clock = _run(scripted, deadline_seconds=10_000)
        assert clock.sleeps == [15, 30, 60, 120, 120, 120]

    def test_completed_ref_does_not_block_a_new_run(self):
        """A completed reference is NOT resumed: a later identical request
        (e.g. a nightly re-pull of the same window) submits a fresh report."""
        scripted = ScriptedFlow([COMPLETED])
        _result, store, _clock = _run(scripted)
        scripted2 = ScriptedFlow([COMPLETED])
        result2, _store2, _clock2 = _run(scripted2, store=store)
        assert scripted2.submit_calls == 1
        assert result2["resumed"] is False


# ---------------------------------------------------------------------------
# Resumption + dedup (NEVER a blind resubmission)
# ---------------------------------------------------------------------------


class TestResumeAndDedup:
    def test_in_flight_ref_is_repolled_not_resubmitted(self):
        store = InMemoryReportRefStore()
        store.put(
            LEDGER_REF,
            REQUEST_HASH,
            report_ref="ref_persisted",
            state=REF_IN_FLIGHT,
            resubmit_count=0,
        )
        scripted = ScriptedFlow([COMPLETED])
        result, _store, _clock = _run(scripted, store=store)
        assert scripted.submit_calls == 0, "an in-flight ref must never be resubmitted"
        assert scripted.poll_refs == ["ref_persisted"]
        assert result["resumed"] is True
        assert result["report_ref"] == "ref_persisted"

    def test_deferred_run_then_second_run_repolls_same_ref(self):
        """Run 1 defers at the deadline; run 2 (same request_hash) resumes by
        re-poll -- exactly ONE submit across both runs."""
        store = InMemoryReportRefStore()
        scripted = ScriptedFlow([PROCESSING] * 50)
        result1, store, _clock1 = _run(scripted, store=store, deadline_seconds=100)
        assert result1["status"] == "deferred"
        assert scripted.submit_calls == 1

        scripted.statuses = [COMPLETED]
        result2, _store, _clock2 = _run(scripted, store=store, deadline_seconds=100)
        assert result2["status"] == "completed"
        assert result2["resumed"] is True
        assert scripted.submit_calls == 1, "run 2 must re-poll, not resubmit"


# ---------------------------------------------------------------------------
# Claim row: race-safe submission (review 26.1 F-1/F-2)
# ---------------------------------------------------------------------------


class _RaceStore:
    """Simulates the get -> claim race: our get() sees nothing, but a
    concurrent run claims + submits + persists its ref before our claim."""

    def __init__(self, inner: InMemoryReportRefStore) -> None:
        self.inner = inner
        self._first_get = True

    def get(self, ledger_ref, request_hash):
        if self._first_get:
            self._first_get = False
            # Concurrent run wins the window between our get and our claim.
            self.inner.claim(ledger_ref, request_hash, stale_after_seconds=CLAIM_TTL)
            self.inner.put(
                ledger_ref,
                request_hash,
                report_ref="ref_other_run",
                state=REF_IN_FLIGHT,
                resubmit_count=0,
            )
            return None
        return self.inner.get(ledger_ref, request_hash)

    def claim(self, *args, **kwargs):
        return self.inner.claim(*args, **kwargs)

    def put(self, *args, **kwargs):
        return self.inner.put(*args, **kwargs)


class TestClaimRow:
    def test_claim_lost_mid_submit_defers_without_submitting(self):
        """Another run holds a FRESH 'submitting' claim (no ref yet): we must
        never submit a duplicate -- deferred outcome, zero submit calls."""
        clock = FakeClock()
        store = InMemoryReportRefStore(clock=clock.clock)
        assert (
            store.claim(LEDGER_REF, REQUEST_HASH, stale_after_seconds=CLAIM_TTL)
            == CLAIM_ACQUIRED
        )  # the "other run" owns the claim
        scripted = ScriptedFlow([COMPLETED])
        result, _store, _clock = _run(scripted, store=store, clock=clock)
        assert result == {
            "status": "deferred",
            "report_ref": None,
            "request_hash": REQUEST_HASH,
            "resumed": False,
        }
        assert scripted.submit_calls == 0
        assert scripted.poll_refs == []
        assert store.get(LEDGER_REF, REQUEST_HASH)["state"] == REF_SUBMITTING

    def test_claim_lost_race_with_persisted_ref_repolls(self):
        """F-1 race window get -> submit -> put: the concurrent run persisted
        its ref between our get and our claim => re-poll it, never submit."""
        clock = FakeClock()
        store = _RaceStore(InMemoryReportRefStore(clock=clock.clock))
        scripted = ScriptedFlow([COMPLETED])
        result, _store, _clock = _run(scripted, store=store, clock=clock)
        assert scripted.submit_calls == 0, "lost claim with a ref must re-poll"
        assert scripted.poll_refs == ["ref_other_run"]
        assert result["status"] == "completed"
        assert result["resumed"] is True
        assert result["report_ref"] == "ref_other_run"

    def test_stale_submitting_claim_is_taken_over_and_logged(self, caplog):
        """F-2: a 'submitting' orphan older than the claim TTL is reclaimed
        and the resubmission is a DELIBERATE, logged decision."""
        clock = FakeClock()
        store = InMemoryReportRefStore(clock=clock.clock)
        store.claim(LEDGER_REF, REQUEST_HASH, stale_after_seconds=CLAIM_TTL)
        clock.now += CLAIM_TTL + 1  # the owner crashed; the claim went stale
        scripted = ScriptedFlow([COMPLETED])
        with caplog.at_level(logging.WARNING, logger="core.async_reports"):
            result, _store, _clock = _run(scripted, store=store, clock=clock)
        assert result["status"] == "completed"
        assert scripted.submit_calls == 1
        assert any(
            "stale_claim_taken_over" in r.getMessage() for r in caplog.records
        ), "the conscious resubmission must be logged"

    def test_fresh_submitting_claim_is_not_taken_over_before_ttl(self):
        clock = FakeClock()
        store = InMemoryReportRefStore(clock=clock.clock)
        store.claim(LEDGER_REF, REQUEST_HASH, stale_after_seconds=CLAIM_TTL)
        clock.now += CLAIM_TTL - 1
        scripted = ScriptedFlow([COMPLETED])
        result, _store, _clock = _run(scripted, store=store, clock=clock)
        assert result["status"] == "deferred"
        assert scripted.submit_calls == 0

    def test_claim_ttl_env_override(self, monkeypatch):
        monkeypatch.setenv("ASYNC_REPORT_CLAIM_TTL_SECONDS", "60")
        clock = FakeClock()
        store = InMemoryReportRefStore(clock=clock.clock)
        store.claim(LEDGER_REF, REQUEST_HASH, stale_after_seconds=60)
        clock.now += 61
        scripted = ScriptedFlow([COMPLETED])
        result, _store, _clock = _run(scripted, store=store, clock=clock)
        assert result["status"] == "completed"
        assert scripted.submit_calls == 1

    def test_submit_exception_releases_the_claim(self):
        """A typed submit failure must not lock the scope out for the TTL:
        the claim row is released (failed) and a retry can claim again."""
        from core.pull_errors import AuthExpiredError

        clock = FakeClock()
        store = InMemoryReportRefStore(clock=clock.clock)

        def _submit() -> str:
            raise AuthExpiredError(provider_status=401)

        flow = AsyncReportFlow(
            submit=_submit,
            poll=lambda ref: COMPLETED,
            download=lambda ref: [],
            request_hash=REQUEST_HASH,
        )
        with pytest.raises(AuthExpiredError):
            run_async_report(
                flow,
                ledger_ref=LEDGER_REF,
                deadline_seconds=10,
                clock=clock.clock,
                sleeper=clock.sleep,
                store=store,
            )
        assert store.get(LEDGER_REF, REQUEST_HASH)["state"] == REF_FAILED
        # A follow-up run reclaims the released row and submits normally.
        scripted = ScriptedFlow([COMPLETED])
        result, _store, _clock = _run(scripted, store=store, clock=clock)
        assert result["status"] == "completed"
        assert scripted.submit_calls == 1


# ---------------------------------------------------------------------------
# Ref TTL safety net: old in_flight refs are treated as expired (F-3)
# ---------------------------------------------------------------------------


class TestRefTtl:
    def _aged_store(self, *, resubmit_count: int, age_seconds: float):
        clock = FakeClock()
        store = InMemoryReportRefStore(clock=clock.clock)
        store.put(
            LEDGER_REF,
            REQUEST_HASH,
            report_ref="ref_persisted",
            state=REF_IN_FLIGHT,
            resubmit_count=resubmit_count,
        )
        clock.now += age_seconds
        return store, clock

    def test_old_in_flight_ref_is_treated_as_expired(self):
        """A ref older than ASYNC_REPORT_REF_TTL_DAYS is expired WITHOUT a
        provider poll -- one resubmission, then the run completes."""
        store, clock = self._aged_store(resubmit_count=0, age_seconds=REF_TTL_SECONDS + 1)
        scripted = ScriptedFlow([PENDING, COMPLETED])
        result, _store, _clock = _run(scripted, store=store, clock=clock)
        assert scripted.submit_calls == 1, "stale ref => the single resubmission"
        assert "ref_persisted" not in scripted.poll_refs, (
            "a TTL-expired ref must not be polled at the provider"
        )
        assert result["status"] == "completed"
        assert result["report_ref"] == "ref_1"
        assert store.get(LEDGER_REF, REQUEST_HASH)["resubmit_count"] == 1

    def test_old_ref_with_spent_resubmission_raises_without_submit(self):
        """TTL expiry CONSUMES the single resubmission: already spent =>
        typed transient error and zero submits."""
        store, clock = self._aged_store(resubmit_count=1, age_seconds=REF_TTL_SECONDS + 1)
        scripted = ScriptedFlow([COMPLETED])
        with pytest.raises(ProviderTransientError):
            _run(scripted, store=store, clock=clock)
        assert scripted.submit_calls == 0
        assert scripted.poll_refs == []
        assert store.get(LEDGER_REF, REQUEST_HASH)["state"] == REF_FAILED

    def test_ref_within_ttl_is_repolled_normally(self):
        store, clock = self._aged_store(resubmit_count=0, age_seconds=REF_TTL_SECONDS - 1)
        scripted = ScriptedFlow([COMPLETED])
        result, _store, _clock = _run(scripted, store=store, clock=clock)
        assert scripted.submit_calls == 0
        assert scripted.poll_refs == ["ref_persisted"]
        assert result["resumed"] is True

    def test_ref_ttl_env_override(self, monkeypatch):
        monkeypatch.setenv("ASYNC_REPORT_REF_TTL_DAYS", "1")
        store, clock = self._aged_store(resubmit_count=0, age_seconds=86400 + 1)
        scripted = ScriptedFlow([COMPLETED])
        result, _store, _clock = _run(scripted, store=store, clock=clock)
        assert scripted.submit_calls == 1, "1-day TTL => the old ref is expired"
        assert result["status"] == "completed"


# ---------------------------------------------------------------------------
# Expiry: one resubmission, then typed transient failure
# ---------------------------------------------------------------------------


class TestExpiry:
    def test_expired_status_resubmits_exactly_once_then_completes(self):
        scripted = ScriptedFlow([EXPIRED, PENDING, COMPLETED])
        result, store, _clock = _run(scripted)
        assert scripted.submit_calls == 2
        assert result["status"] == "completed"
        assert result["report_ref"] == "ref_2"
        assert store.get(LEDGER_REF, REQUEST_HASH)["resubmit_count"] == 1

    def test_second_expiry_raises_provider_transient(self):
        scripted = ScriptedFlow([EXPIRED, EXPIRED])
        with pytest.raises(ProviderTransientError):
            _run(scripted)

    def test_second_expiry_marks_store_failed(self):
        store = InMemoryReportRefStore()
        scripted = ScriptedFlow([EXPIRED, EXPIRED])
        with pytest.raises(ProviderTransientError):
            _run(scripted, store=store)
        assert store.get(LEDGER_REF, REQUEST_HASH)["state"] == REF_FAILED

    def test_dead_download_link_counts_as_expiry(self):
        """download raising DownloadLinkExpired => one resubmission."""
        scripted = ScriptedFlow(
            [COMPLETED, COMPLETED],
            download_exc=[DownloadLinkExpired("url dead"), None],
        )
        result, _store, _clock = _run(scripted)
        assert scripted.submit_calls == 2
        assert result["status"] == "completed"

    def test_dead_link_twice_raises_provider_transient(self):
        scripted = ScriptedFlow(
            [COMPLETED, COMPLETED],
            download_exc=[DownloadLinkExpired("dead"), DownloadLinkExpired("dead")],
        )
        with pytest.raises(ProviderTransientError):
            _run(scripted)

    def test_expiry_resets_the_poll_backoff(self):
        """After a resubmission the wait ladder restarts at the initial value."""
        scripted = ScriptedFlow([PENDING, PENDING, EXPIRED, PENDING, COMPLETED])
        _result, _store, clock = _run(scripted, deadline_seconds=10_000)
        # 15, 30 before expiry; ladder restarts at 15 after the resubmission.
        assert clock.sleeps == [15, 30, 15]

    def test_resumed_ref_purged_by_provider_maps_to_expired(self):
        """F-3 contract: a resumed ref the provider no longer knows is mapped
        by the module's poll to canonical 'expired' => one resubmission."""
        clock = FakeClock()
        store = InMemoryReportRefStore(clock=clock.clock)
        store.put(
            LEDGER_REF,
            REQUEST_HASH,
            report_ref="ref_purged_upstream",
            state=REF_IN_FLIGHT,
            resubmit_count=0,
        )
        scripted = ScriptedFlow([EXPIRED, PENDING, COMPLETED])
        result, _store, _clock = _run(scripted, store=store, clock=clock)
        assert scripted.poll_refs[0] == "ref_purged_upstream"
        assert scripted.submit_calls == 1
        assert result["status"] == "completed"
        assert result["report_ref"] == "ref_1"

    def test_transrun_resubmission_bound_is_enforced(self):
        """F-11: the single-resubmission bound survives across runs -- a
        store pre-populated with resubmit_count=1 plus a poll of EXPIRED
        yields the typed error with ZERO submit calls."""
        clock = FakeClock()
        store = InMemoryReportRefStore(clock=clock.clock)
        store.put(
            LEDGER_REF,
            REQUEST_HASH,
            report_ref="ref_from_previous_run",
            state=REF_IN_FLIGHT,
            resubmit_count=1,
        )
        scripted = ScriptedFlow([EXPIRED])
        with pytest.raises(ProviderTransientError):
            _run(scripted, store=store, clock=clock)
        assert scripted.submit_calls == 0, "the resubmission budget is already spent"
        assert store.get(LEDGER_REF, REQUEST_HASH)["state"] == REF_FAILED


# ---------------------------------------------------------------------------
# Deadline: non-destructive deferral
# ---------------------------------------------------------------------------


class TestDeadline:
    def test_deadline_returns_deferred_not_failure(self):
        scripted = ScriptedFlow([PROCESSING] * 100)
        result, store, _clock = _run(scripted, deadline_seconds=200)
        assert result["status"] == "deferred"
        assert result["request_hash"] == REQUEST_HASH
        assert store.get(LEDGER_REF, REQUEST_HASH)["state"] == REF_IN_FLIGHT

    def test_deadline_zero_defers_after_first_poll(self):
        scripted = ScriptedFlow([PENDING])
        result, _store, clock = _run(scripted, deadline_seconds=0)
        assert result["status"] == "deferred"
        assert clock.sleeps == []

    def test_env_default_deadline(self, monkeypatch):
        monkeypatch.delenv("ASYNC_REPORT_DEADLINE_SECONDS", raising=False)
        assert default_deadline_seconds() == 1500
        monkeypatch.setenv("ASYNC_REPORT_DEADLINE_SECONDS", "300")
        assert default_deadline_seconds() == 300
        monkeypatch.setenv("ASYNC_REPORT_DEADLINE_SECONDS", "not-a-number")
        assert default_deadline_seconds() == 1500

    def test_env_int_is_clamped_to_minimum_one(self, monkeypatch):
        """F-9: 0 or negative env integers would break the poll/TTL
        arithmetic -- clamped to 1."""
        monkeypatch.setenv("ASYNC_REPORT_DEADLINE_SECONDS", "0")
        assert default_deadline_seconds() == 1
        monkeypatch.setenv("ASYNC_REPORT_DEADLINE_SECONDS", "-5")
        assert default_deadline_seconds() == 1


# ---------------------------------------------------------------------------
# Failure + contract guards
# ---------------------------------------------------------------------------


class TestFailuresAndGuards:
    def test_failed_status_raises_provider_transient_and_marks_store(self):
        store = InMemoryReportRefStore()
        scripted = ScriptedFlow([FAILED])
        with pytest.raises(ProviderTransientError):
            _run(scripted, store=store)
        assert store.get(LEDGER_REF, REQUEST_HASH)["state"] == REF_FAILED

    def test_non_canonical_status_raises_value_error(self):
        scripted = ScriptedFlow(["Success"])  # raw provider-ish status: refused
        with pytest.raises(ValueError, match="non-canonical"):
            _run(scripted)

    def test_empty_request_hash_raises_value_error(self):
        scripted = ScriptedFlow([COMPLETED])
        with pytest.raises(ValueError, match="request_hash"):
            run_async_report(
                scripted.as_flow(request_hash=""),
                ledger_ref=LEDGER_REF,
                deadline_seconds=10,
                store=InMemoryReportRefStore(),
            )

    def test_non_callable_flow_member_raises_value_error(self):
        flow = AsyncReportFlow(
            submit=None,  # type: ignore[arg-type]
            poll=lambda ref: COMPLETED,
            download=lambda ref: [],
            request_hash=REQUEST_HASH,
        )
        with pytest.raises(ValueError, match="submit"):
            run_async_report(
                flow,
                ledger_ref=LEDGER_REF,
                deadline_seconds=10,
                store=InMemoryReportRefStore(),
            )

    def test_typed_error_from_poll_propagates_unchanged(self):
        from core.pull_errors import AuthExpiredError

        def _poll(_ref: str) -> str:
            raise AuthExpiredError(provider_status=401)

        flow = AsyncReportFlow(
            submit=lambda: "ref_x",
            poll=_poll,
            download=lambda ref: [],
            request_hash=REQUEST_HASH,
        )
        with pytest.raises(AuthExpiredError):
            run_async_report(
                flow,
                ledger_ref=LEDGER_REF,
                deadline_seconds=10,
                store=InMemoryReportRefStore(),
            )


# ---------------------------------------------------------------------------
# Live Postgres: PostgresReportRefStore round-trip (review 26.1 F-4)
# ---------------------------------------------------------------------------


def _pg_reachable() -> bool:
    if not os.environ.get("TEST_POSTGRES_DSN"):
        return False
    try:
        import psycopg  # noqa: PLC0415

        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


pg_available = pytest.mark.skipif(
    not _pg_reachable(), reason="TEST_POSTGRES_DSN not set/reachable -- skip live PG"
)

_MIGRATION_048 = (
    Path(__file__).resolve().parents[3]
    / "infra"
    / "nango"
    / "migrations"
    / "048_async_report_refs.sql"
)


def _pg_env(monkeypatch) -> None:
    """Point core.db at the disposable test database (pattern 18.1)."""
    monkeypatch.setenv("PLATFORM_DB_URL", os.environ["TEST_POSTGRES_DSN"])


def _apply_migration_048() -> None:
    """Apply 048 idempotently to the TEST database only (Supabase stays
    human-gated -- pattern of the 042 live constraint tests)."""
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_MIGRATION_048.read_text(encoding="utf-8"))
        conn.commit()


def _live_row(ledger_ref: str, request_hash: str) -> tuple:
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT report_ref, state, resubmit_count, created_at, updated_at
                FROM app.async_report_refs
                WHERE ledger_ref = %s AND request_hash = %s
                """,
                (ledger_ref, request_hash),
            )
            return cur.fetchone()


def _live_cleanup(ledger_ref: str) -> None:
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app.async_report_refs WHERE ledger_ref = %s",
                (ledger_ref,),
            )
        conn.commit()


@pg_available
def test_live_claim_insert_conflict_put_get_round_trip(monkeypatch):
    """F-4 round-trip: claim insert -> claim conflict (rowcount=0) -> put
    update -> get, checking column order and created_at preservation."""
    _pg_env(monkeypatch)
    _apply_migration_048()
    store = PostgresReportRefStore()
    ledger_ref = f"connref_live_{uuid.uuid4().hex[:12]}"
    request_hash = "hash_live_v1"
    try:
        # 1) claim insert: fresh 'submitting' row, report_ref NULL.
        assert (
            store.claim(ledger_ref, request_hash, stale_after_seconds=900)
            == CLAIM_ACQUIRED
        )
        row = store.get(ledger_ref, request_hash)
        assert row["report_ref"] is None
        assert row["state"] == REF_SUBMITTING
        assert row["resubmit_count"] == 0

        # 2) claim conflict: the row is fresh and owned -> rowcount=0 -> lost.
        assert (
            store.claim(ledger_ref, request_hash, stale_after_seconds=900)
            == CLAIM_LOST
        )

        created_before = _live_row(ledger_ref, request_hash)[3]

        # 3) put update: writes ref + state on the SAME row.
        store.put(
            ledger_ref,
            request_hash,
            report_ref="ref_live_1",
            state=REF_IN_FLIGHT,
            resubmit_count=1,
        )

        # 4) get: every field carries ITS value (column-order guard) and the
        #    age is computed against DB now().
        row = store.get(ledger_ref, request_hash)
        assert row["report_ref"] == "ref_live_1"
        assert row["state"] == REF_IN_FLIGHT
        assert row["resubmit_count"] == 1
        assert isinstance(row["age_seconds"], float)
        assert 0.0 <= row["age_seconds"] < 300.0

        # created_at preserved through the ON CONFLICT UPDATE; updated_at moved.
        raw = _live_row(ledger_ref, request_hash)
        assert raw[3] == created_before, "put must never rewrite created_at"
        assert raw[4] >= raw[3]
    finally:
        _live_cleanup(ledger_ref)


@pg_available
def test_live_stale_takeover_and_terminal_reclaim(monkeypatch):
    """Claim state machine on the real table: stale 'submitting' takeover
    (F-2) and reclaim of a terminal row, both preserving created_at."""
    _pg_env(monkeypatch)
    _apply_migration_048()
    from core.db import get_connection  # noqa: PLC0415

    store = PostgresReportRefStore()
    ledger_ref = f"connref_live_{uuid.uuid4().hex[:12]}"
    request_hash = "hash_live_v2"
    try:
        assert (
            store.claim(ledger_ref, request_hash, stale_after_seconds=900)
            == CLAIM_ACQUIRED
        )
        created_before = _live_row(ledger_ref, request_hash)[3]

        # Fresh claim is protected...
        assert (
            store.claim(ledger_ref, request_hash, stale_after_seconds=900)
            == CLAIM_LOST
        )
        # ...but an aged orphan is taken over (simulate the crash age).
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE app.async_report_refs
                    SET updated_at = now() - interval '1 hour'
                    WHERE ledger_ref = %s AND request_hash = %s
                    """,
                    (ledger_ref, request_hash),
                )
            conn.commit()
        assert (
            store.claim(ledger_ref, request_hash, stale_after_seconds=900)
            == CLAIM_STALE_ACQUIRED
        )

        # Terminal rows are reclaimable (a completed ref never blocks a
        # later identical request).
        store.put(
            ledger_ref,
            request_hash,
            report_ref="ref_live_done",
            state=REF_COMPLETED,
            resubmit_count=0,
        )
        assert (
            store.claim(ledger_ref, request_hash, stale_after_seconds=900)
            == CLAIM_ACQUIRED
        )
        row = store.get(ledger_ref, request_hash)
        assert row["state"] == REF_SUBMITTING
        assert row["report_ref"] is None
        assert _live_row(ledger_ref, request_hash)[3] == created_before
    finally:
        _live_cleanup(ledger_ref)


# ---------------------------------------------------------------------------
# AD-2 / HG-1: no provider vocabulary in the core file
# ---------------------------------------------------------------------------


class TestNoProviderVocabulary:
    def test_core_file_has_no_provider_names(self):
        source = (
            Path(__file__).parents[2] / "core" / "async_reports.py"
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
        assert not hits, f"async_reports.py must contain no provider vocabulary: {hits}"
