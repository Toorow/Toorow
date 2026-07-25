"""toorow -- Org plan & entitlements: read/write foundation (Story 34.1).

Stateless core of the trial-governance epic (Epic 34). Three public functions:

  * get_org_plan(org_id)       -- fetch current plan dict; returns trial default when
                                   no row exists in app.org_plan (derived default, no INSERT).
  * resolve_entitlements(org_id) -- map plan to effective limits (trial bounded; full/internal
                                   unlimited = None).
  * set_org_plan(org_id, plan, entitlements, granted_by)
                                -- upsert app.org_plan + INSERT app.org_plan_history in the
                                   SAME transaction; refuses invalid plan or unknown org_id.

ADDITIF STRICT (34.1): module is not wired onto the runtime. Enforcement guards land in
Stories 34.2/34.3. Zero impact on existing connectors or data flows.

REDUCERS PURE: get_org_plan and resolve_entitlements are tested without Postgres by
mocking _fetch_org_plan_row(). DB access is isolated behind _fetch_org_plan_row() and
_write_org_plan() so tests can stub I/O cleanly.

TRUST CONTRACT: this module has NO authorization guard. Its functions assume their
arguments are ALREADY authorized -- authorization is the responsibility of the SURFACE
(REST route / MCP tool). Any surface that exposes these functions MUST apply its own
org-read / org-manage guard before calling in.

Design mirrors metric_semantics.py: ``from __future__ import annotations``, module logger,
lazy ``core.db`` imports inside DB functions (noqa: PLC0415), prefixed-ULID helper, pure
functions kept separate from I/O.

Windows/CI note: all log/message strings use ASCII-safe characters only.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants -- source of truth for trial limits (sync with migration 056 enum).
# ---------------------------------------------------------------------------

PLAN_TRIAL = "trial"
PLAN_FULL = "full"
PLAN_INTERNAL = "internal"
_VALID_PLANS = frozenset({PLAN_TRIAL, PLAN_FULL, PLAN_INTERNAL})

DEFAULT_TRIAL_ENTITLEMENTS: dict[str, int] = {
    "max_backfill_days": 30,
    "max_datastreams": 3,
}

# Org-plan-history id prefix (prefixed-ULID pattern).
_OPLH_PREFIX = "oplh_"


# ---------------------------------------------------------------------------
# ULID-lite helper (same pattern as metric_semantics.py: time-prefixed random).
# ---------------------------------------------------------------------------


def _new_id(prefix: str = _OPLH_PREFIX) -> str:
    """Generate a prefixed pseudo-ULID: prefix + hex(ms timestamp) + hex(random 8 bytes)."""
    import os  # noqa: PLC0415

    ts_hex = format(int(time.time() * 1000), "013x")
    rand_hex = os.urandom(8).hex()
    return f"{prefix}{ts_hex}{rand_hex}"


# ---------------------------------------------------------------------------
# DB I/O (isolated -- mockable in tests).
# ---------------------------------------------------------------------------


def _fetch_org_plan_row(org_id: str) -> dict | None:
    """Return the raw app.org_plan row for org_id, or None if absent.

    Isolated DB call so unit tests can mock this function to test the pure
    resolution logic without a live Postgres connection.
    """
    from core import db  # noqa: PLC0415

    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT plan, entitlements, granted_by, granted_at, updated_at
                FROM app.org_plan
                WHERE org_id = %s
                """,
                (org_id,),
            )
            row = cur.fetchone()
    if row is None:
        return None
    return {
        "plan": row[0],
        "entitlements": row[1] if row[1] is not None else {},
        "granted_by": row[2],
        "granted_at": row[3],
        "updated_at": row[4],
    }


def _org_exists(org_id: str, cur: Any) -> bool:
    """Return True if app.organizations contains a row with id = org_id."""
    cur.execute("SELECT 1 FROM app.organizations WHERE id = %s", (org_id,))
    return cur.fetchone() is not None


def _write_org_plan(
    org_id: str,
    plan: str,
    entitlements: dict,
    granted_by: str | None,
) -> None:
    """Upsert app.org_plan + INSERT app.org_plan_history in ONE transaction.

    Raises:
        ValueError: if plan is not in _VALID_PLANS or org_id does not exist.
        psycopg.Error: on DB failure (transaction is rolled back automatically).
    """
    from core import db  # noqa: PLC0415

    history_id = _new_id()

    with db.get_connection() as conn:
        with conn.cursor() as cur:
            # Validate org existence BEFORE any write (refuse without partial state).
            if not _org_exists(org_id, cur):
                raise ValueError(f"org_id not found in app.organizations: {org_id!r}")

            # Upsert org_plan (INSERT ... ON CONFLICT DO UPDATE).
            cur.execute(
                """
                INSERT INTO app.org_plan (org_id, plan, entitlements, granted_by, granted_at)
                VALUES (%s, %s, %s::jsonb, %s, now())
                ON CONFLICT (org_id) DO UPDATE
                    SET plan         = EXCLUDED.plan,
                        entitlements = EXCLUDED.entitlements,
                        granted_by   = EXCLUDED.granted_by,
                        granted_at   = now()
                """,
                (org_id, plan, _jsonb(entitlements), granted_by),
            )

            # Append one history row (append-only table -- triggers block UPDATE/DELETE).
            cur.execute(
                """
                INSERT INTO app.org_plan_history (id, org_id, plan, entitlements, granted_by, at)
                VALUES (%s, %s, %s, %s::jsonb, %s, now())
                """,
                (history_id, org_id, plan, _jsonb(entitlements), granted_by),
            )

        conn.commit()
        logger.info(
            "set_org_plan: org=%s plan=%s history_id=%s granted_by=%s",
            org_id,
            plan,
            history_id,
            granted_by,
        )


def _jsonb(d: dict) -> str:
    """Serialize a dict to a JSON string suitable for a JSONB parameter."""
    import json  # noqa: PLC0415

    return json.dumps(d)


# ---------------------------------------------------------------------------
# Public API -- pure resolution (testable without Postgres via mock of
# _fetch_org_plan_row).
# ---------------------------------------------------------------------------


def get_org_plan(org_id: str) -> dict:
    """Return the current plan record for org_id.

    If no row exists in app.org_plan, returns the derived trial default (no INSERT
    performed -- a new org is implicitly trial until explicitly upgraded).

    Returns:
        dict with keys: org_id, plan, entitlements, granted_by (may be None),
        granted_at (may be None for derived default).
    """
    row = _fetch_org_plan_row(org_id)
    if row is None:
        logger.debug("get_org_plan: no row for org=%s -- returning derived trial default", org_id)
        return {
            "org_id": org_id,
            "plan": PLAN_TRIAL,
            "entitlements": dict(DEFAULT_TRIAL_ENTITLEMENTS),
            "granted_by": None,
            "granted_at": None,
            "updated_at": None,
        }
    return {
        "org_id": org_id,
        "plan": row["plan"],
        "entitlements": row["entitlements"],
        "granted_by": row["granted_by"],
        "granted_at": row["granted_at"],
        "updated_at": row["updated_at"],
    }


def resolve_entitlements(org_id: str) -> dict[str, int | None]:
    """Return effective entitlement limits for org_id.

    Resolution:
      trial    => DEFAULT_TRIAL_ENTITLEMENTS (bounded limits).
      full     => all limits None (unlimited; 34.2/34.3 guards treat None as no cap).
      internal => all limits None (same as full).

    Args:
        org_id: organization id (TEXT, prefixed ULID 'org_').

    Returns:
        dict with keys max_backfill_days and max_datastreams (int or None).
    """
    plan_record = get_org_plan(org_id)
    plan = plan_record["plan"]

    if plan == PLAN_TRIAL:
        return dict(DEFAULT_TRIAL_ENTITLEMENTS)

    if plan in (PLAN_FULL, PLAN_INTERNAL):
        # Unlimited: None signals "no cap" to the enforcement guards in 34.2/34.3.
        return {k: None for k in DEFAULT_TRIAL_ENTITLEMENTS}

    # Defensive fallback (should never reach here given DB CHECK constraint).
    logger.warning(
        "resolve_entitlements: unknown plan %r for org=%s -- defaulting to trial", plan, org_id
    )
    return dict(DEFAULT_TRIAL_ENTITLEMENTS)


def set_org_plan(
    org_id: str,
    plan: str,
    entitlements: dict,
    granted_by: str | None = None,
) -> None:
    """Upsert the plan for an org and record the change in history.

    Performs BOTH the org_plan upsert AND the org_plan_history insert in a single
    Postgres transaction. If either fails, the whole transaction rolls back -- no
    partial state is committed.

    Args:
        org_id:       Organization id (must exist in app.organizations).
        plan:         New plan value; must be one of 'trial', 'full', 'internal'.
        entitlements: Entitlement dict to store (e.g. DEFAULT_TRIAL_ENTITLEMENTS or
                      {} for full/internal where limits are Python-side None).
        granted_by:   Identity subject of the super-admin performing the change (AD-14).
                      May be None for system/automation calls.

    Raises:
        ValueError: if plan is not in {'trial', 'full', 'internal'} or if org_id is
                    not found in app.organizations. No partial state committed.
        psycopg.Error: on DB error (transaction rolled back).
    """
    if plan not in _VALID_PLANS:
        raise ValueError(
            f"Invalid plan {plan!r}. Must be one of: {sorted(_VALID_PLANS)}"
        )

    _write_org_plan(org_id, plan, entitlements, granted_by)
