"""toorow -- trial-governance enforcement guards (Stories 34.2 & 34.3, Epic 34).

Consumes the READ foundation delivered in 34.1
(``core.org_entitlements.resolve_entitlements(org_id)`` ->
``{"max_backfill_days": 30|None, "max_datastreams": 3|None}``; ``None`` = unlimited
for ``full``/``internal``). This module NEVER writes a plan -- it only READS the
resolved entitlements and enforces them at two seams:

  * 34.2 -- check_datastream_limit(project_id, conn): BEFORE a NEW datastream row
    becomes active, count the org's currently-active datastreams and REFUSE the
    (max+1)-th with a typed ``TrialDatastreamLimitError`` (surface maps it to 409).
    Bounds the CREATION only -- existing datastreams are never touched.

  * 34.3 -- clamp_backfill_window(connection_ref_id, date_from, date_to, conn):
    HONESTLY clamp the requested backfill window to ``today - max_backfill_days``
    for a bounded org. Never rejects the pull -- returns the (possibly) reduced
    window plus a ``clamped`` flag the caller signals to the user.

DESIGN (mirrors org_entitlements.py):
  * ``from __future__ import annotations``, module logger, lazy ``core.db`` imports.
  * DB access isolated behind ``_resolve_org_for_project`` /
    ``_count_active_datastreams`` / ``_resolve_org_for_connection`` so unit tests
    stub I/O cleanly (no live Postgres needed for the pure logic).

FAIL-CLOSED: an org with no plan resolves to trial via org_entitlements (34.1).
NON-REGRESSION: full/internal resolve to ``None`` limits -> both guards are no-ops.

ASCII-only strings (Windows/CI safe).
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# Audit / signal action names (string literals -- no edit to core.audit constants).
SIGNAL_TRIAL_LIMIT_REACHED = "trial.limit_reached"


# ---------------------------------------------------------------------------
# Typed refusal (symmetric to the enqueue_pull refusal dict of Story 25.5).
# ---------------------------------------------------------------------------


class TrialDatastreamLimitError(Exception):
    """Raised when creating a datastream would exceed the org's trial cap.

    Carries a stable ``code`` and an actionable FR message so the REST/MCP
    surface can map it to a 409 without string-matching.
    """

    code = "trial_datastream_limit"

    def __init__(self, *, org_id: str, limit: int, current: int) -> None:
        self.org_id = org_id
        self.limit = limit
        self.current = current
        self.message = (
            f"Limite d'essai atteinte : {current} flux de donnees actifs sur "
            f"{limit} autorises pour cette organisation. Supprimez un flux "
            f"existant ou passez au plan complet pour en creer davantage."
        )
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "org_id": self.org_id,
            "limit": self.limit,
            "current": self.current,
        }


# ---------------------------------------------------------------------------
# DB I/O (isolated -- mockable in tests).
# ---------------------------------------------------------------------------


def _resolve_org_for_project(project_id: str, conn) -> str | None:
    """Return the owning org_id for *project_id*, or None if unknown."""
    with conn.cursor() as cur:
        cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
        row = cur.fetchone()
    if row is None:
        return None
    return row[0]


def _count_active_datastreams(org_id: str, conn) -> int:
    """Count the org's currently-active (enabled) datastreams.

    Active/published = enabled = TRUE (drafts with enabled=FALSE do not consume
    the quota -- default retained in the 34.2 spec). Archived rows are excluded
    when the column exists; the COALESCE keeps this working on schemas where
    archived_at is always NULL.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM app.datastreams
            WHERE org_id = %s
              AND enabled = TRUE
              AND archived_at IS NULL
            """,
            (org_id,),
        )
        row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _resolve_org_for_connection(connection_ref_id: str, conn) -> str | None:
    """Return the owning org_id for a connection_ref via its project."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.org_id
            FROM app.connection_ref r
            JOIN app.projects p ON p.id = r.project_id
            WHERE r.id = %s
            """,
            (connection_ref_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return row[0]


def _record_trial_limit_signal(org_id: str, metadata: dict) -> None:
    """Persist a ``trial_limit_reached`` signal to the audit sink (DB).

    V1 sink = app.audit_log via write_audit_row (additive, no new table). Never
    raises -- write_audit_row already swallows its own failures.
    """
    from core.audit import write_audit_row  # noqa: PLC0415

    write_audit_row(
        identity=metadata.get("identity", "system"),
        action=SIGNAL_TRIAL_LIMIT_REACHED,
        provider_account="",
        connection_ref="",
        metadata={"org_id": org_id, **metadata},
    )


# ---------------------------------------------------------------------------
# 34.2 -- datastream count guard.
# ---------------------------------------------------------------------------


def check_datastream_limit(
    project_id: str,
    conn,
    *,
    identity: str = "system",
) -> None:
    """Refuse creation of a new ACTIVE datastream past the org's trial cap.

    Resolves the org from *project_id*, reads ``max_datastreams`` (34.1). ``None``
    (full/internal) => no guard. Otherwise, when the org already has
    ``>= max_datastreams`` active datastreams, raise TrialDatastreamLimitError
    (the surface maps it to 409) and record a ``trial_limit_reached`` signal.

    Bounds the CREATION only: this is called BEFORE the INSERT, so no partial
    state is created and existing datastreams are never altered.

    Fail-open on resolution errors (unknown project, DB hiccup): a governance
    read failure must not block a legitimate create -- the same posture as the
    topology guard in queue.py.
    """
    from core.org_entitlements import resolve_entitlements  # noqa: PLC0415

    # Resolve org + limit + current count under a fail-OPEN guard: a governance
    # read failure (DB hiccup, unknown project) must NEVER block a legitimate
    # create. The intentional refusal below is raised OUTSIDE this try so it is
    # never swallowed.
    try:
        org_id = _resolve_org_for_project(project_id, conn)
        if not org_id:
            # Unknown / org-less project (legacy 'default' before backfill): no guard.
            logger.debug(
                "trial_enforcement: no org for project=%s -- datastream guard skipped",
                project_id,
            )
            return

        limit = resolve_entitlements(org_id).get("max_datastreams")
        if limit is None:
            # full/internal -> unlimited.
            return

        current = _count_active_datastreams(org_id, conn)
    except Exception as exc:  # noqa: BLE001  (fail-open governance read)
        logger.warning(
            "trial_enforcement: datastream_guard_skipped project=%s: %s", project_id, exc
        )
        return

    if current >= limit:
        logger.info(
            "trial_enforcement: datastream_limit_reached org=%s current=%d limit=%d",
            org_id,
            current,
            limit,
        )
        _record_trial_limit_signal(
            org_id,
            {
                "kind": "datastream",
                "limit": limit,
                "current": current,
                "project_id": project_id,
                "identity": identity,
            },
        )
        raise TrialDatastreamLimitError(org_id=org_id, limit=limit, current=current)


# ---------------------------------------------------------------------------
# 34.3 -- backfill window clamp.
# ---------------------------------------------------------------------------


def _clamp_date_from(date_from: str, max_backfill_days: int, today: date) -> tuple[str, bool]:
    """Return (clamped_date_from, was_clamped).

    Floor = today - max_backfill_days. If the requested date_from is older than
    the floor, raise it to the floor (honest clamp). Otherwise unchanged.
    """
    floor = today - timedelta(days=max_backfill_days)
    requested = date.fromisoformat(date_from)
    if requested < floor:
        return floor.isoformat(), True
    return date_from, False


def clamp_backfill_window(
    connection_ref_id: str,
    date_from: str,
    date_to: str,
    conn,
    *,
    today: date | None = None,
) -> dict:
    """Clamp the requested backfill window to the org's trial ceiling.

    Reads ``max_backfill_days`` (34.1) for the connection's owning org. ``None``
    (full/internal) => no clamp (non-regression). A trial org's ``date_from`` is
    raised to ``today - max_backfill_days`` when it reaches further back; a recent
    pull (< max_backfill_days) is left untouched.

    Returns a dict: ``{"date_from", "date_to", "clamped": bool,
    "max_backfill_days": int|None}``. Never rejects the pull -- the caller runs on
    the (possibly) reduced window and surfaces ``clamped`` to the user.

    Reuses account_topology's BACKFILL_* invariants as the shared source of the
    windowing constants (no second backfill engine).
    """
    from core import account_topology  # noqa: PLC0415  (shared BACKFILL_* invariants)
    from core.org_entitlements import resolve_entitlements  # noqa: PLC0415

    today = today or date.today()
    result = {
        "date_from": date_from,
        "date_to": date_to,
        "clamped": False,
        "max_backfill_days": None,
    }

    org_id = _resolve_org_for_connection(connection_ref_id, conn)
    if not org_id:
        return result

    max_days = resolve_entitlements(org_id).get("max_backfill_days")
    if max_days is None:
        # full/internal -> unlimited.
        return result

    # Defensive: keep the ceiling within account_topology's supported span.
    max_days = min(int(max_days), account_topology.BACKFILL_MAX_DAYS)
    result["max_backfill_days"] = max_days

    try:
        new_from, was_clamped = _clamp_date_from(date_from, max_days, today)
    except (TypeError, ValueError):
        # Malformed date_from: don't touch it (the pull path validates dates itself).
        return result

    if was_clamped:
        logger.info(
            "trial_enforcement: backfill_clamped org=%s conn=%s %s -> %s (max=%dd)",
            org_id,
            connection_ref_id,
            date_from,
            new_from,
            max_days,
        )
        result["date_from"] = new_from
        result["clamped"] = True
    return result
