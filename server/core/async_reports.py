"""toorow -- Generic async report flow: submit -> poll -> download (Story 26.1, A).

Several reporting APIs do not answer a data request synchronously: the caller
submits a report request, receives an opaque reference, polls until the
provider finishes generating the report (minutes to hours), then downloads the
result from a short-lived URL. This module owns that mechanic ONCE, in core,
so every connector that needs it supplies only three callables and a request
hash -- core owns the loop, the persistence, the resumption and the dedup.

Exports:
  AsyncReportFlow           -- the callables + request_hash contract a module supplies
  run_async_report(...)     -- the submit -> poll -> download driver
  DownloadLinkExpired       -- raised BY a module's download callable when the
                               short-lived download URL is dead
  PENDING/PROCESSING/COMPLETED/FAILED/EXPIRED -- canonical poll statuses
  ASYNC_REPORT_STATUSES     -- the full canonical status tuple
  CLAIM_ACQUIRED/CLAIM_STALE_ACQUIRED/CLAIM_LOST -- store claim outcomes
  InMemoryReportRefStore    -- process-local store (tests / local dev)
  PostgresReportRefStore    -- app.async_report_refs-backed store (default)

Hard invariants (epic-26 "Contraintes transverses" 1):
  - NO BLIND RESUBMISSION: a pull re-run with the same ``request_hash`` while a
    report reference is still in flight RE-POLLS the persisted reference
    instead of submitting a new report (some providers reject duplicate
    submissions outright; all of them waste quota on one).
  - CLAIM-ROW SUBMIT (review 26.1 F-1/F-2): before calling ``submit()`` the
    driver CLAIMS the (ledger_ref, request_hash) row with an
    ``INSERT ... ON CONFLICT DO NOTHING`` of a ``state='submitting'`` row
    (``report_ref`` NULL). A lost claim means another run owns the row: the
    driver re-reads and re-polls if a reference is present, otherwise returns
    a DEFERRED outcome -- two concurrent runs can never double-submit through
    the get -> submit -> put window. After a successful ``submit()`` the claim
    row is UPDATED with the reference and ``state='in_flight'``.
  - ORPHANED CLAIMS: a ``submitting`` row whose owner crashed between claim
    and submit (or between submit and the reference write) is taken over by a
    later claim once it is older than ASYNC_REPORT_CLAIM_TTL_SECONDS (default
    900 s). That takeover is a DELIBERATE, logged resubmission decision:
    there is a residual window in which the crashed owner had already
    submitted at the provider but died before persisting the reference -- the
    provider-side report is then orphaned (wasted quota, no data loss). This
    is accepted and bounded by the TTL; shrinking it further would require a
    provider-side idempotency key core cannot assume (AD-2).
  - Expired reference / dead download link => ONE resubmission, then a typed
    ``ProviderTransientError`` (retryable -- the queue's normal policy applies).
  - REF TTL SAFETY NET (review 26.1 F-3): on resumption, an ``in_flight``
    reference not updated for more than ASYNC_REPORT_REF_TTL_DAYS (default 7
    days) is treated as canonical ``expired`` WITHOUT polling the provider --
    it consumes the single allowed resubmission. This catches providers that
    purge old report references with an "unknown ref" error instead of an
    expired status.
  - Bounded deadline: past ``deadline_seconds`` the run returns a DEFERRED
    outcome while the provider still says pending/processing. The reference
    stays persisted in flight, so the NEXT run resumes by re-poll. A slow
    provider is never a definitive failure.
  - Progressive poll backoff: first wait ASYNC_REPORT_POLL_INITIAL_SECONDS
    (default 15 s), doubling up to ASYNC_REPORT_POLL_MAX_SECONDS (default 120 s).

Environment variables:
  ASYNC_REPORT_DEADLINE_SECONDS      default "1500" -- per-run polling budget
  ASYNC_REPORT_POLL_INITIAL_SECONDS  default "15"   -- first poll wait
  ASYNC_REPORT_POLL_MAX_SECONDS      default "120"  -- poll wait ceiling
  ASYNC_REPORT_CLAIM_TTL_SECONDS     default "900"  -- orphaned 'submitting'
                                     claim takeover threshold (F-1/F-2)
  ASYNC_REPORT_REF_TTL_DAYS          default "7"    -- in_flight ref age past
                                     which resumption treats it as expired (F-3)
  (All env integers are clamped to >= 1 -- review 26.1 F-9.)

Design constraints (AD-2 / HG-1):
  - ZERO provider names and ZERO provider-specific status strings in this file.
    The module's ``poll`` callable maps the provider's raw statuses to the
    canonical set below; core never sees provider vocabulary.
  - ``clock`` / ``sleeper`` / ``store`` are injectable so the whole state
    machine is provable with a fake clock (no real sleeps in tests).
  - ASCII-only log/message strings (AI-03).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Callable

from core.pull_errors import ProviderTransientError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Canonical poll statuses (the module's poll callable maps provider statuses
# onto these five values -- core knows nothing else).
# ---------------------------------------------------------------------------

PENDING = "pending"
PROCESSING = "processing"
COMPLETED = "completed"
FAILED = "failed"
EXPIRED = "expired"

ASYNC_REPORT_STATUSES = (PENDING, PROCESSING, COMPLETED, FAILED, EXPIRED)

# Persisted reference states (mirrors the CHECK constraint in migration 048).
REF_SUBMITTING = "submitting"  # claim row: owned, submit() not yet persisted
REF_IN_FLIGHT = "in_flight"
REF_COMPLETED = "completed"
REF_FAILED = "failed"

# Store claim outcomes (review 26.1 F-1/F-2).
CLAIM_ACQUIRED = "acquired"  # fresh row or reclaimed terminal row
CLAIM_STALE_ACQUIRED = "stale_acquired"  # took over an orphaned 'submitting' row
CLAIM_LOST = "lost"  # another run owns the row right now

# One resubmission allowed after an expiry, then typed transient failure.
MAX_RESUBMISSIONS = 1

_DEADLINE_DEFAULT = 1500
_POLL_INITIAL_DEFAULT = 15
_POLL_MAX_DEFAULT = 120
_CLAIM_TTL_DEFAULT = 900
_REF_TTL_DAYS_DEFAULT = 7


def _env_int(name: str, default: int) -> int:
    """Env integer, clamped to >= 1 (review 26.1 F-9: 0 or negative values
    would break the poll/backoff/TTL arithmetic silently)."""
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(1, value)


def default_deadline_seconds() -> int:
    """Per-run polling budget (ASYNC_REPORT_DEADLINE_SECONDS, default 1500 s)."""
    return _env_int("ASYNC_REPORT_DEADLINE_SECONDS", _DEADLINE_DEFAULT)


def _poll_initial_seconds() -> int:
    return _env_int("ASYNC_REPORT_POLL_INITIAL_SECONDS", _POLL_INITIAL_DEFAULT)


def _poll_max_seconds() -> int:
    return _env_int("ASYNC_REPORT_POLL_MAX_SECONDS", _POLL_MAX_DEFAULT)


def _claim_ttl_seconds() -> int:
    """Orphaned 'submitting' claim takeover threshold (F-1/F-2, default 900 s)."""
    return _env_int("ASYNC_REPORT_CLAIM_TTL_SECONDS", _CLAIM_TTL_DEFAULT)


def _ref_ttl_seconds() -> int:
    """in_flight reference age past which resumption treats it as expired (F-3)."""
    return _env_int("ASYNC_REPORT_REF_TTL_DAYS", _REF_TTL_DAYS_DEFAULT) * 86400


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


class DownloadLinkExpired(Exception):
    """Raised by a module's ``download`` callable when the short-lived download
    URL is dead. Core treats it exactly like a poll status of ``expired``:
    one resubmission, then ProviderTransientError."""


@dataclass
class AsyncReportFlow:
    """The three callables + request hash a connector supplies (Story 26.1, A).

    Attributes:
        submit:       () -> report_ref (opaque provider reference string).
        poll:         (report_ref) -> one of ASYNC_REPORT_STATUSES.
                      The module maps provider statuses to the canonical set;
                      a provider-side error payload should instead raise a
                      typed core.pull_errors error which propagates unchanged.
                      CONTRACT (review 26.1 F-3): a report reference the
                      provider no longer knows (purged, "report not found",
                      "invalid report id" on a previously valid ref) MUST be
                      mapped to the canonical ``expired`` status -- core then
                      applies the single-resubmission policy instead of
                      failing the pull on an orphaned reference.
        download:     (report_ref) -> rows (any payload the connector expects).
                      Raises DownloadLinkExpired when the URL is dead.
        request_hash: Deterministic hash of the FULL request shape (report
                      type, columns, window, account scope, ...). The dedup /
                      resumption key: same hash + in-flight ref => re-poll.
    """

    submit: Callable[[], str]
    poll: Callable[[str], str]
    download: Callable[[str], object]
    request_hash: str


# ---------------------------------------------------------------------------
# Report-reference stores (persistence of report_ref + request_hash).
#
# Decision (story left the choice open): a dedicated ``app.async_report_refs``
# table (migration 048) rather than a ledger metadata column -- the extract
# ledger is derived per-day from pull_jobs/pull_verifications and has no
# writable row of its own to hang a reference on, while resumption needs a
# keyed UPSERT on (ledger_ref, request_hash) that survives worker restarts.
#
# Store contract (identical for both implementations):
#   get(ledger_ref, request_hash) -> None | {"report_ref": str | None,
#       "state": str, "resubmit_count": int, "age_seconds": float}
#     ``age_seconds`` is the row's age since its last update, computed against
#     the STORE's own clock (DB now() for Postgres, injectable clock for the
#     in-memory store) so the driver never mixes clock domains.
#   claim(ledger_ref, request_hash, *, stale_after_seconds) -> one of
#       CLAIM_ACQUIRED / CLAIM_STALE_ACQUIRED / CLAIM_LOST
#     Atomically acquires the submit right for the scope: inserts a
#     'submitting' row (report_ref NULL), or reclaims a terminal
#     (completed/failed) row, or takes over an orphaned 'submitting' row
#     older than ``stale_after_seconds``. CLAIM_LOST = another live run owns
#     the row. Reclaims preserve created_at.
#   put(ledger_ref, request_hash, *, report_ref, state, resubmit_count)
#     UPSERT of the living row (created_at preserved on update).
# ---------------------------------------------------------------------------


class InMemoryReportRefStore:
    """Process-local store. Used by unit tests and available for local dev.

    ``clock`` is injectable (epoch-seconds-like monotonic source) so claim-TTL
    and ref-TTL behaviour is provable with a fake clock.
    """

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._rows: dict[tuple[str, str], dict] = {}

    def get(self, ledger_ref: str, request_hash: str) -> dict | None:
        row = self._rows.get((ledger_ref, request_hash))
        if row is None:
            return None
        return {
            "report_ref": row["report_ref"],
            "state": row["state"],
            "resubmit_count": row["resubmit_count"],
            "age_seconds": max(0.0, self._clock() - row["_updated_at"]),
        }

    def claim(self, ledger_ref: str, request_hash: str, *, stale_after_seconds: int) -> str:
        now = self._clock()
        key = (ledger_ref, request_hash)
        row = self._rows.get(key)
        if row is None:
            self._rows[key] = {
                "report_ref": None,
                "state": REF_SUBMITTING,
                "resubmit_count": 0,
                "_created_at": now,
                "_updated_at": now,
            }
            return CLAIM_ACQUIRED
        if row["state"] in (REF_COMPLETED, REF_FAILED):
            row.update(
                report_ref=None,
                state=REF_SUBMITTING,
                resubmit_count=0,
                _updated_at=now,
            )
            return CLAIM_ACQUIRED
        if row["state"] == REF_SUBMITTING and now - row["_updated_at"] >= stale_after_seconds:
            row.update(report_ref=None, resubmit_count=0, _updated_at=now)
            return CLAIM_STALE_ACQUIRED
        return CLAIM_LOST

    def put(
        self,
        ledger_ref: str,
        request_hash: str,
        *,
        report_ref: str | None,
        state: str,
        resubmit_count: int,
    ) -> None:
        now = self._clock()
        key = (ledger_ref, request_hash)
        existing = self._rows.get(key)
        self._rows[key] = {
            "report_ref": report_ref,
            "state": state,
            "resubmit_count": resubmit_count,
            "_created_at": existing["_created_at"] if existing else now,
            "_updated_at": now,
        }


class PostgresReportRefStore:
    """app.async_report_refs-backed store (migration 048). Default at runtime.

    One living row per (ledger_ref, request_hash): claim, resubmission and
    state transitions UPSERT the same row (the audit trail is the worker log).
    """

    def get(self, ledger_ref: str, request_hash: str) -> dict | None:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT report_ref, state, resubmit_count,
                           EXTRACT(EPOCH FROM (now() - updated_at))::float8
                    FROM app.async_report_refs
                    WHERE ledger_ref = %s AND request_hash = %s
                    """,
                    (ledger_ref, request_hash),
                )
                row = cur.fetchone()
        if row is None:
            return None
        return {
            "report_ref": row[0],
            "state": row[1],
            "resubmit_count": row[2],
            "age_seconds": max(0.0, float(row[3])),
        }

    def claim(self, ledger_ref: str, request_hash: str, *, stale_after_seconds: int) -> str:
        from ulid import ULID  # noqa: PLC0415

        from core.db import get_connection  # noqa: PLC0415

        ref_id = f"aref_{ULID()}"
        with get_connection() as conn:
            with conn.cursor() as cur:
                # 1) Fresh claim: the F-1 race gate. Exactly one concurrent
                #    run wins the INSERT; every other run sees rowcount=0.
                cur.execute(
                    """
                    INSERT INTO app.async_report_refs
                        (id, ledger_ref, request_hash, report_ref, state,
                         resubmit_count, created_at, updated_at)
                    VALUES (%s, %s, %s, NULL, 'submitting', 0, now(), now())
                    ON CONFLICT (ledger_ref, request_hash) DO NOTHING
                    """,
                    (ref_id, ledger_ref, request_hash),
                )
                if cur.rowcount == 1:
                    conn.commit()
                    return CLAIM_ACQUIRED
                # 2) Reclaim a terminal row (a completed reference must not
                #    block a later identical request; created_at preserved).
                cur.execute(
                    """
                    UPDATE app.async_report_refs
                    SET report_ref = NULL, state = 'submitting',
                        resubmit_count = 0, updated_at = now()
                    WHERE ledger_ref = %s AND request_hash = %s
                      AND state IN ('completed', 'failed')
                    """,
                    (ledger_ref, request_hash),
                )
                if cur.rowcount == 1:
                    conn.commit()
                    return CLAIM_ACQUIRED
                # 3) Take over an orphaned 'submitting' claim (owner crashed
                #    between claim and the reference write -- F-2).
                cur.execute(
                    """
                    UPDATE app.async_report_refs
                    SET report_ref = NULL, resubmit_count = 0, updated_at = now()
                    WHERE ledger_ref = %s AND request_hash = %s
                      AND state = 'submitting'
                      AND updated_at < now() - make_interval(secs => %s)
                    """,
                    (ledger_ref, request_hash, float(stale_after_seconds)),
                )
                if cur.rowcount == 1:
                    conn.commit()
                    return CLAIM_STALE_ACQUIRED
            conn.commit()
            return CLAIM_LOST

    def put(
        self,
        ledger_ref: str,
        request_hash: str,
        *,
        report_ref: str | None,
        state: str,
        resubmit_count: int,
    ) -> None:
        from ulid import ULID  # noqa: PLC0415

        from core.db import get_connection  # noqa: PLC0415

        ref_id = f"aref_{ULID()}"
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.async_report_refs
                        (id, ledger_ref, request_hash, report_ref, state,
                         resubmit_count, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, now(), now())
                    ON CONFLICT (ledger_ref, request_hash) DO UPDATE
                        SET report_ref     = EXCLUDED.report_ref,
                            state          = EXCLUDED.state,
                            resubmit_count = EXCLUDED.resubmit_count,
                            updated_at     = now()
                    """,
                    (ref_id, ledger_ref, request_hash, report_ref, state, resubmit_count),
                )
            conn.commit()


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------


def _validate_flow(flow: AsyncReportFlow) -> None:
    """Loud contract check -- a malformed flow is a module bug, never degraded."""
    for attr in ("submit", "poll", "download"):
        if not callable(getattr(flow, attr, None)):
            raise ValueError(f"AsyncReportFlow.{attr} must be callable")
    if not isinstance(flow.request_hash, str) or not flow.request_hash:
        raise ValueError("AsyncReportFlow.request_hash must be a non-empty string")


def run_async_report(
    flow: AsyncReportFlow,
    *,
    ledger_ref: str,
    deadline_seconds: int | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    store=None,
) -> dict:
    """Drive one submit -> poll -> download cycle with resumption + dedup.

    Args:
        flow:             The module-supplied AsyncReportFlow.
        ledger_ref:       Opaque scope key for persistence (typically the
                          pull's connection_ref_id or datastream id). Core
                          stores and compares it, never interprets it (AD-2).
        deadline_seconds: Per-run polling budget. None -> env default
                          (ASYNC_REPORT_DEADLINE_SECONDS, 1500 s).
        clock:            Monotonic seconds source (injectable; fake in tests).
        sleeper:          Sleep function (injectable; advances fake clock).
        store:            Report-ref store (injectable; default Postgres).

    Submission is race-safe (review 26.1 F-1/F-2): the driver first CLAIMS the
    (ledger_ref, request_hash) row (state 'submitting', report_ref NULL) and
    only then calls ``flow.submit()``; a lost claim re-reads the row and
    re-polls the other run's reference when present, or defers when the other
    run is still mid-submit. Residual window (documented, accepted): an owner
    crashing AFTER a successful provider submit but BEFORE persisting the
    reference leaves a 'submitting' row that a later run takes over after
    ASYNC_REPORT_CLAIM_TTL_SECONDS and DELIBERATELY resubmits (logged as
    ``stale_claim_taken_over``) -- the crashed owner's provider-side report is
    orphaned (quota waste only, never duplicate data: the new reference
    replaces the row).

    Returns:
        {"status": "completed", "rows": <download payload>,
         "report_ref": str, "resumed": bool}
      or, when the deadline elapses while the provider still reports
      pending/processing (non-destructive -- the next run re-polls):
        {"status": "deferred", "report_ref": str,
         "request_hash": str, "resumed": bool}
      or, when another concurrent run holds the submit claim and has not yet
      persisted a reference (F-1: never double-submit):
        {"status": "deferred", "report_ref": None,
         "request_hash": str, "resumed": False}

    Raises:
        ProviderTransientError: canonical ``failed`` status, or a second
            expiry after the single allowed resubmission (retryable class --
            the queue's standard attempt/backoff policy applies).
        ValueError: malformed flow, or a poll status outside the canonical
            set (a module contract violation must fail loudly, mirroring the
            classify_http_error 429 guard).
        Any typed error raised by the flow's callables propagates unchanged.
    """
    _validate_flow(flow)
    if store is None:
        store = PostgresReportRefStore()
    if deadline_seconds is None:
        deadline_seconds = default_deadline_seconds()

    poll_initial = _poll_initial_seconds()
    poll_max = _poll_max_seconds()

    # ------------------------------------------------------------------ #
    # Resumption / dedup: an in-flight reference for the same request     #
    # hash is RE-POLLED -- never resubmitted blindly. New submissions go  #
    # through the claim row so a concurrent run can never double-submit.  #
    # ------------------------------------------------------------------ #
    resumed = False
    resubmit_count = 0
    stale_in_flight = False
    existing = store.get(ledger_ref, flow.request_hash)
    if (
        existing is not None
        and existing.get("state") == REF_IN_FLIGHT
        and existing.get("report_ref")
    ):
        report_ref = existing["report_ref"]
        resubmit_count = int(existing.get("resubmit_count") or 0)
        resumed = True
        age_seconds = existing.get("age_seconds")
        if age_seconds is not None and age_seconds > _ref_ttl_seconds():
            # F-3 safety net: a reference this old has likely been purged by
            # the provider. Treat it as canonical expired WITHOUT polling --
            # this consumes the single allowed resubmission.
            stale_in_flight = True
            logger.warning(
                "async_reports: in_flight_ref_ttl_exceeded ledger_ref=%s "
                "request_hash=%s age_seconds=%d -- treating as expired",
                ledger_ref,
                flow.request_hash,
                int(age_seconds),
            )
        else:
            logger.info(
                "async_reports: resume_by_repoll ledger_ref=%s request_hash=%s",
                ledger_ref,
                flow.request_hash,
            )
    else:
        claim_outcome = store.claim(
            ledger_ref,
            flow.request_hash,
            stale_after_seconds=_claim_ttl_seconds(),
        )
        if claim_outcome == CLAIM_LOST:
            # Another run owns the row RIGHT NOW (won the get -> claim race).
            current = store.get(ledger_ref, flow.request_hash)
            if (
                current is not None
                and current.get("state") == REF_IN_FLIGHT
                and current.get("report_ref")
            ):
                # The other run already persisted its reference: re-poll it.
                report_ref = current["report_ref"]
                resubmit_count = int(current.get("resubmit_count") or 0)
                resumed = True
                logger.info(
                    "async_reports: claim_lost_repoll ledger_ref=%s request_hash=%s",
                    ledger_ref,
                    flow.request_hash,
                )
            else:
                # The other run is mid-submit: NEVER submit a duplicate.
                # Defer; the next run re-polls (or takes over after the TTL).
                logger.info(
                    "async_reports: claim_lost_deferred ledger_ref=%s request_hash=%s",
                    ledger_ref,
                    flow.request_hash,
                )
                return {
                    "status": "deferred",
                    "report_ref": None,
                    "request_hash": flow.request_hash,
                    "resumed": False,
                }
        else:
            if claim_outcome == CLAIM_STALE_ACQUIRED:
                # F-2: deliberate resubmission decision over an orphaned
                # claim (see the residual-window note in the docstring).
                logger.warning(
                    "async_reports: stale_claim_taken_over ledger_ref=%s "
                    "request_hash=%s -- deliberate resubmission after "
                    "claim TTL (crashed owner may have submitted; its "
                    "provider-side report is orphaned)",
                    ledger_ref,
                    flow.request_hash,
                )
            try:
                report_ref = flow.submit()
            except Exception:
                # Release the claim so a typed submit failure does not lock
                # the scope out for the full claim TTL; the queue's retry
                # policy re-runs through a fresh claim.
                store.put(
                    ledger_ref,
                    flow.request_hash,
                    report_ref=None,
                    state=REF_FAILED,
                    resubmit_count=0,
                )
                raise
            store.put(
                ledger_ref,
                flow.request_hash,
                report_ref=report_ref,
                state=REF_IN_FLIGHT,
                resubmit_count=0,
            )
            logger.info(
                "async_reports: submitted ledger_ref=%s request_hash=%s",
                ledger_ref,
                flow.request_hash,
            )

    started = clock()
    wait_seconds = poll_initial

    def _expire_and_maybe_resubmit() -> str:
        """Expired ref / dead link: one resubmission, then typed transient."""
        nonlocal resubmit_count, wait_seconds
        if resubmit_count >= MAX_RESUBMISSIONS:
            store.put(
                ledger_ref,
                flow.request_hash,
                report_ref=report_ref,
                state=REF_FAILED,
                resubmit_count=resubmit_count,
            )
            raise ProviderTransientError(
                message=(
                    "async report expired again after the single allowed "
                    "resubmission (request_hash=" + flow.request_hash + ")"
                ),
            )
        resubmit_count += 1
        new_ref = flow.submit()
        store.put(
            ledger_ref,
            flow.request_hash,
            report_ref=new_ref,
            state=REF_IN_FLIGHT,
            resubmit_count=resubmit_count,
        )
        wait_seconds = poll_initial  # fresh report: restart the backoff ladder
        logger.warning(
            "async_reports: resubmitted_after_expiry ledger_ref=%s resubmit_count=%d",
            ledger_ref,
            resubmit_count,
        )
        return new_ref

    if stale_in_flight:
        # F-3: the resumed reference outlived ASYNC_REPORT_REF_TTL_DAYS --
        # expired without a provider round-trip (single resubmission applies).
        report_ref = _expire_and_maybe_resubmit()

    while True:
        status = flow.poll(report_ref)
        if status not in ASYNC_REPORT_STATUSES:
            raise ValueError(
                f"poll returned non-canonical status {status!r}; "
                f"expected one of {ASYNC_REPORT_STATUSES}"
            )

        if status == COMPLETED:
            try:
                rows = flow.download(report_ref)
            except DownloadLinkExpired:
                report_ref = _expire_and_maybe_resubmit()
                continue
            store.put(
                ledger_ref,
                flow.request_hash,
                report_ref=report_ref,
                state=REF_COMPLETED,
                resubmit_count=resubmit_count,
            )
            return {
                "status": "completed",
                "rows": rows,
                "report_ref": report_ref,
                "resumed": resumed,
            }

        if status == FAILED:
            store.put(
                ledger_ref,
                flow.request_hash,
                report_ref=report_ref,
                state=REF_FAILED,
                resubmit_count=resubmit_count,
            )
            raise ProviderTransientError(
                message=(
                    "async report generation failed at the provider "
                    "(request_hash=" + flow.request_hash + ")"
                ),
            )

        if status == EXPIRED:
            report_ref = _expire_and_maybe_resubmit()
            continue

        # pending | processing: bounded wait, then progressive backoff.
        if clock() - started >= deadline_seconds:
            # Non-destructive deferral: the ref stays in flight so the next
            # run (same request_hash) resumes by re-poll, never resubmits.
            store.put(
                ledger_ref,
                flow.request_hash,
                report_ref=report_ref,
                state=REF_IN_FLIGHT,
                resubmit_count=resubmit_count,
            )
            logger.info(
                "async_reports: deadline_deferred ledger_ref=%s request_hash=%s",
                ledger_ref,
                flow.request_hash,
            )
            return {
                "status": "deferred",
                "report_ref": report_ref,
                "request_hash": flow.request_hash,
                "resumed": resumed,
            }
        sleeper(wait_seconds)
        wait_seconds = min(wait_seconds * 2, poll_max)
