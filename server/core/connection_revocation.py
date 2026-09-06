"""Revoking a connection, and refreshing what is known of its health.

AD-43, 2026-08-12, moved out of `admin_api.py` unchanged. Both write; neither is
a route. `_refresh_health_last` travels with them because it is THEIR throttle --
a module-level clock that only these two read and write, and a mutable left
behind in a file that no longer touches it is a mutable nobody owns.
"""

from __future__ import annotations

import logging

from starlette.responses import JSONResponse, Response

from core import nango_client
from core.audit import (
    ACTION_CONNECTION_REVOKED,
    write_audit_row,
)

logger = logging.getLogger("core.admin_api")

# AI-18: per-connection rate limit on /refresh-health (Story 3.3, AC10).
# Maps connection_ref_id -> monotonic timestamp of last successful refresh.
# TODO(Phase-B): move rate-limit state to Postgres for multi-replica safety.
_refresh_health_last: dict[str, float] = {}

def _apply_credential_revocation(
    *,
    connection_id: str,
    nango_connection_id: str,
    provider: str,
    subject: str,
    revoked_as: str,
    owner_identity: str | None,
    project_id: str | None,
) -> tuple[bool, Response | None]:
    """Carry out a revocation once it has been authorized. Returns (nango_deleted, error).

    Shared by the project-scoped endpoint (Story 7.3) and the credential-scoped one
    (Story 42.9): a revocation is an act on the CREDENTIAL, so the effects must not
    differ depending on which surface asked for it. Callers do their own gating --
    this function assumes it already passed.
    """
    # Delete at Nango (best-effort: a credential we can no longer reach is still revoked here).
    nango_deleted = False
    try:
        nango_deleted = nango_client.delete_connection(nango_connection_id, provider)
    except Exception as exc:
        logger.warning(
            "admin_api: revoke_connection nango_delete_failed conn=%s err=%s",
            connection_id,
            exc,
        )

    # Purge health poller cache (health row + in-memory) and the rate-limit entry.
    try:
        from core.health_poller import purge_connection_cache  # noqa: PLC0415

        purge_connection_cache(connection_id)
    except Exception as exc:
        logger.warning(
            "admin_api: revoke_connection cache_purge_failed conn=%s err=%s",
            connection_id,
            exc,
        )
    _refresh_health_last.pop(connection_id, None)

    # Mark revoked + invalidate what this credential exposed.
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE app.connection_ref
                    SET status = 'revoked', revoked_at = NOW(), updated_at = NOW()
                    WHERE id = %s
                    """,
                    (connection_id,),
                )
            from core.account_topology import invalidate_credential_exposures  # noqa: PLC0415

            invalidate_credential_exposures(connection_id, "revocation", conn)
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: revoke_connection update_error: %s", exc)
        return False, JSONResponse(
            {"code": "db_error", "message": f"Database error on revoke: {exc}"},
            status_code=500,
        )

    write_audit_row(
        identity=subject,
        action=ACTION_CONNECTION_REVOKED,
        provider_account=provider or "",
        connection_ref=connection_id,
        metadata={
            "project_id": project_id,
            "connection_id": connection_id,
            "via": "manual_revoke",
            # Distinguishes the owner's own decision from an administrative
            # backstop -- they must not read alike in the audit trail.
            "revoked_as": revoked_as,
            "owner_identity": owner_identity,
            "nango_deleted": nango_deleted,
        },
    )
    return nango_deleted, None

def _refresh_connection_health_now(connection_ref_id: str) -> bool:
    """Re-read one connection's health and write it. Story 56.6 (AD-36).

    Called at the moments health CHANGES -- a consent granted, a connection
    revoked -- instead of waiting for a 300 s thread that, at --min-instances=0,
    only advances while a request happens to be in flight.

    Never raises: a health refresh that fails must not fail the flow that
    triggered it. The daily sweep remains the backstop.
    """
    if not connection_ref_id:
        return False
    try:
        from datetime import datetime, timezone  # noqa: PLC0415

        from core import health_poller, nango_client  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT nango_connection_id, provider FROM app.connection_ref WHERE id = %s",
                    (connection_ref_id,),
                )
                row = cur.fetchone()
        if row is None:
            return False
        # A google_direct row has no Nango connection: the resolver accepts either
        # identifier space, and the caller hands it the one that exists.
        health = nango_client.poll_connection_health(row[0] or connection_ref_id, provider=row[1])
        health_poller._upsert_health(
            conn_ref_id=connection_ref_id,
            status=health.status,
            last_checked_at=datetime.now(tz=timezone.utc),
            last_fetched_at=health.last_fetched_at,
        )
        return True
    except Exception as exc:  # noqa: BLE001 -- see docstring
        logger.warning(
            "admin_api: health_refresh_skipped connection=%s: %s", connection_ref_id, exc
        )
        return False
