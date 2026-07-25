"""toorow -- health-aware envelope enrichment (Story 2.5; extracted 5.1/AI-27).

Extracted verbatim from ``core.main`` in Story 5.1 (AI-27 decomposition). Pure
behaviour-preserving move: ``main.py`` re-exports ``_enrich_envelope_with_health``
for backward compatibility with existing imports/tests.

Reads connection health from ``app.connection_health`` (Postgres cache) and maps it
onto the canonical envelope's ``meta`` (freshness + alerts). No-op when the DB is
unreachable or no connection_ref rows exist (Epic 1 backward compat).

AD-2: source-agnostic -- no module-specific strings.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from core.constants import AUTH_EXPIRED_CODE

logger = logging.getLogger(__name__)

_STALE_CADENCE_HOURS_DEFAULT = 26  # generous for daily pulls that run at night


def enrich_envelope_with_health(envelope: dict, project_id: str) -> dict:
    """Enrich the canonical envelope's meta with connection health (Story 2.5, T4).

    Reads health from app.connection_health (Postgres cache) -- does NOT call Nango.
    This is a no-op when:
      - PLATFORM_DB_URL is not set / DB is unreachable
      - No connection_ref rows exist for project_id (Epic 1 backward compat)

    Health -> freshness mapping (AC5, AC7):
      ok:      stale_since absent (unless cadence check fires -- AC7)
      stale:   meta.freshness.stale_since = last_fetched_at (Nango proxy)
      revoked: meta.alerts[] += auth_expired entry

    AC7 cadence check (independent of Nango stale status):
      If last_fetched_at is older than STALE_CADENCE_HOURS, set stale_since anyway.
      If last_fetched_at is None and connection is older than 2h, also set stale_since.
      TODO(Epic-3): replace with manifest cadence_hours per connector
    """
    cadence_hours = int(os.environ.get("STALE_CADENCE_HOURS", str(_STALE_CADENCE_HOURS_DEFAULT)))

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT h.status, h.last_fetched_at, r.created_at
                    FROM app.connection_ref r
                    LEFT JOIN app.connection_health h ON h.connection_ref_id = r.id
                    WHERE r.project_id = %s
                    ORDER BY r.created_at
                    LIMIT 1
                    """,
                    (project_id,),
                )
                row = cur.fetchone()
    except Exception:
        # DB unreachable or table absent -- silently no-op (Epic 1 backward compat)
        logger.debug("health_enrich: db unavailable for project_id=%s -- skipping", project_id)
        return envelope

    if row is None:
        # No connection_ref for this project -- no-op (Epic 1 backward compat)
        return envelope

    status, last_fetched_at, conn_created_at = row

    # If health row does not exist yet (poller has not run), status is None
    if status is None:
        # No health cache yet -- conservative: no stale_since, no alert
        return envelope

    now = datetime.now(tz=timezone.utc)
    meta = envelope.setdefault("meta", {})
    freshness = meta.setdefault("freshness", {})
    alerts: list = meta.setdefault("alerts", [])

    if status == "revoked":
        # AC6, AC8: auth_expired alert
        alerts.append(
            {
                "code": AUTH_EXPIRED_CODE,
                "severity": "error",
                "message": (
                    "Token de connexion expire -- reconnectez dans la console admin."
                ),
            }
        )

    elif status == "populate_failed":
        # Story 3.5 (AC6): populate_failed alert (HG-2: distinct from auth_expired)
        alerts.append(
            {
                "code": "populate_failed",
                "severity": "error",  # review-3-5 F-3: shell needs it for the RED chip
                "message": "Pull landed no rows for the expected window",
            }
        )

    elif status == "stale":
        # AC5: stale_since from Nango last_fetched_at (P2 proxy)
        # TODO(Epic-3): replace with pull ledger cadence
        if last_fetched_at is not None:
            lf = last_fetched_at
            if lf.tzinfo is None:
                lf = lf.replace(tzinfo=timezone.utc)
            freshness["stale_since"] = lf.isoformat()
        elif conn_created_at is not None:
            # review-2-5 F-02: a None stale_since is falsy in the shell and the
            # badge silently disappears -- anchor on the connection's creation
            # time (never fetched => stale since it exists).
            ca = conn_created_at
            if ca.tzinfo is None:
                ca = ca.replace(tzinfo=timezone.utc)
            freshness["stale_since"] = ca.isoformat()
        else:
            freshness["stale_since"] = now.isoformat()

    elif status == "ok":
        # AC7: cadence check independent of Nango's own stale classification
        stale_cadence_seconds = cadence_hours * 3600
        if last_fetched_at is not None:
            lf = last_fetched_at
            if lf.tzinfo is None:
                lf = lf.replace(tzinfo=timezone.utc)
            age_seconds = (now - lf).total_seconds()
            if age_seconds > stale_cadence_seconds:
                # TODO(Epic-3): replace with manifest cadence_hours per connector
                freshness["stale_since"] = lf.isoformat()
        else:
            # Never pulled -- if connection is older than 2h, mark stale
            # TODO(Epic-3): replace with manifest cadence_hours
            if conn_created_at is not None:
                ca = conn_created_at
                if ca.tzinfo is None:
                    ca = ca.replace(tzinfo=timezone.utc)
                if (now - ca).total_seconds() > 2 * 3600:
                    # review-2-5 F-02: truthy anchor -- stale since creation+2h
                    from datetime import timedelta  # noqa: PLC0415

                    freshness["stale_since"] = (ca + timedelta(hours=2)).isoformat()

    return envelope
