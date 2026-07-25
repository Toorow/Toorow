"""toorow -- Background health poller (Story 2.5, AC1, T2).

Periodically polls Nango for each registered connection's health and writes
the result to app.connection_health in platform Postgres.

Design decisions:
  - threading.Thread(daemon=True): simpler than asyncio background task at P2;
    Python GIL is fine here (poller is I/O-bound). No Cloud Scheduler needed
    until Epic 3.
  - HEALTH_POLLER_ENABLED env var (default "true"): set to "false" in CI/tests
    to suppress the background thread.
  - HEALTH_POLL_INTERVAL_SECONDS (default 300): configurable poll interval.
  - All log strings are ASCII-only (L-3 rule).

AD-2: source-agnostic -- no module-specific names.
AD-3: no token columns written to Postgres.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _upsert_health(
    conn_ref_id: str,
    status: str,
    last_checked_at: datetime,
    last_fetched_at: "datetime | None",
) -> None:
    """Write one health row to app.connection_health (upsert pattern).

    Uses psycopg v3 sync via core.db.get_connection().
    """
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.connection_health
                    (connection_ref_id, status, last_checked_at, last_fetched_at)
                VALUES (%(id)s, %(status)s, %(last_checked_at)s, %(last_fetched_at)s)
                ON CONFLICT (connection_ref_id) DO UPDATE
                    -- review-3-5 F-1: populate_failed is STICKY -- the Nango
                    -- poll only knows auth state, so it must not clear a data
                    -- red flag. Cleared by verification writing 'ok' after a
                    -- successful verified pull; 'revoked' (auth dead) trumps.
                    SET status = CASE
                            WHEN app.connection_health.status = 'populate_failed'
                                 AND EXCLUDED.status <> 'revoked'
                            THEN 'populate_failed'
                            ELSE EXCLUDED.status
                        END,
                        last_checked_at = EXCLUDED.last_checked_at,
                        last_fetched_at = EXCLUDED.last_fetched_at
                """,
                {
                    "id": conn_ref_id,
                    "status": status,
                    "last_checked_at": last_checked_at,
                    "last_fetched_at": last_fetched_at,
                },
            )
        conn.commit()


def _get_all_connection_refs() -> list[dict]:
    """Read all connection_ref rows from platform Postgres.

    Returns a list of dicts with at minimum: id, nango_connection_id, provider.
    Returns empty list on DB error (poller is best-effort).
    """
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, nango_connection_id, provider
                    FROM app.connection_ref
                    ORDER BY created_at
                    """
                )
                cols = [desc[0] for desc in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception:
        logger.exception("health_poller: failed to read connection_ref rows")
        return []


def _run_one_poll_cycle() -> int:
    """Poll health for all registered connections.

    Returns count of connections polled.
    """
    from core import nango_client  # noqa: PLC0415

    refs = _get_all_connection_refs()
    if not refs:
        logger.info("health_poller: no connection_ref rows found -- skipping cycle")
        return 0

    now = datetime.now(tz=timezone.utc)
    polled = 0

    for ref in refs:
        conn_ref_id = ref["id"]
        nango_connection_id = ref["nango_connection_id"]
        provider = ref.get("provider")

        try:
            health = nango_client.poll_connection_health(
                nango_connection_id, provider=provider
            )
            _upsert_health(
                conn_ref_id=conn_ref_id,
                status=health.status,
                last_checked_at=now,
                last_fetched_at=health.last_fetched_at,
            )
            polled += 1
        except Exception:
            logger.exception(
                "health_poller: failed to poll connection_ref_id=%s", conn_ref_id
            )

    return polled


def _poll_loop() -> None:
    """Infinite poll loop -- runs in a background daemon thread."""
    interval = int(os.environ.get("HEALTH_POLL_INTERVAL_SECONDS", "300"))
    logger.info("health_poller: loop started (interval=%ss)", interval)

    while True:
        try:
            n = _run_one_poll_cycle()
            logger.info("health_poller: checked %d connections", n)
        except Exception:
            logger.exception("health_poller: poll cycle failed")
        time.sleep(interval)


def purge_connection_cache(connection_ref_id: str) -> None:
    """Remove any in-memory health cache entry for a connection (Story 7.3, AC4).

    The health poller writes connection health to app.connection_health (Postgres)
    and does not maintain a separate in-memory cache keyed by connection_ref_id.
    The in-process rate-limit cache in admin_api.py (_refresh_health_last) is the
    only in-memory state that needs clearing on revocation.

    This function is a hook point called during connection revocation so that:
    1. The architecture records a deliberate "no in-memory cache" fact.
    2. Future in-memory caches (Phase B: Redis, in-process TTL dict) can be
       added here without touching callers.
    3. Tests can verify the call happened (AC8: test_revoke_connection_clears_health_cache).

    For completeness we also delete the Postgres health row so the revoked
    connection no longer appears as healthy in the admin console.
    """
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM app.connection_health WHERE connection_ref_id = %s",
                    (connection_ref_id,),
                )
            conn.commit()
    except Exception as exc:
        # Best-effort: log warning but do not block revocation
        logger.warning(
            "health_poller: purge_connection_cache_failed conn=%s err=%s",
            connection_ref_id,
            exc,
        )


def start_health_poller() -> None:
    """Start the background health poller thread (call once at server startup).

    Reads HEALTH_POLLER_ENABLED env var (default "true"). Set to "false" in CI
    and test environments to suppress the background thread.

    Do NOT call at module level -- the thread must NOT start on import.
    """
    enabled = os.environ.get("HEALTH_POLLER_ENABLED", "true").lower()
    if enabled != "true":
        logger.info("health_poller: disabled via HEALTH_POLLER_ENABLED=%s", enabled)
        return

    # review-2-5 F-03: double-start guard — build_asgi_app() may be called
    # more than once (tests, reload); one poller thread is enough.
    if any(t.name == "health-poller" for t in threading.enumerate()):
        logger.info("health_poller: already running -- not starting a second thread")
        return

    t = threading.Thread(target=_poll_loop, daemon=True, name="health-poller")
    t.start()
    logger.info(
        "health_poller: started (interval=%ss)",
        os.environ.get("HEALTH_POLL_INTERVAL_SECONDS", "300"),
    )
