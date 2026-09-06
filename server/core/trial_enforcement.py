"""toorow -- trial-governance enforcement guards (Stories 34.2 & 34.3, Epic 34).

Consumes the READ foundation delivered in 34.1
(``core.org_entitlements.resolve_entitlements(org_id)`` ->
``{"max_backfill_days": 30|None, "max_datastreams": 3|None}``; ``None`` = unlimited
for ``full``/``internal``). This module NEVER writes a plan -- it only READS the
resolved entitlements and enforces them at two seams:

  * 34.2 -- check_datastream_limit(project_id, conn): BEFORE any statement makes
    one MORE datastream active, count the org's currently-active datastreams and
    REFUSE the (max+1)-th with a typed ``TrialDatastreamLimitError`` (surface maps
    it to 409). Bounds the ACT of going live -- creating one, publishing one,
    starting a stopped one, or recovering a stopped one by rollback. Existing
    datastreams are never touched, and an act that changes no count (a
    republication, a rollback of a Datastream that is already running) skips the
    call rather than failing it.

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

from core.audit import declare_action

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12). Celles-ci n'etaient declarees NULLE PART : la valeur
# etait retapee en dur ici, parce que la liste centrale de `core/audit.py`
# etait trop loin pour valoir le detour. Mesure ce jour-la sur le journal
# vivant : 29 des 64 actions reellement ecrites -- 45 % -- etaient dans ce
# cas, et rien ne pouvait distinguer une action d'une faute de frappe.
ACTION_TRIAL_LIMIT_REACHED = declare_action("trial.limit_reached")


logger = logging.getLogger(__name__)

# Audit / signal action names (string literals -- no edit to core.audit constants).
SIGNAL_TRIAL_LIMIT_REACHED = ACTION_TRIAL_LIMIT_REACHED


# ---------------------------------------------------------------------------
# Typed refusal (symmetric to the enqueue_pull refusal dict of Story 25.5).
# ---------------------------------------------------------------------------


class TrialDatastreamLimitError(Exception):
    """Raised when activating a datastream would exceed the org's trial cap.

    Carries a stable ``code`` and an actionable message so the REST/MCP surface
    can map it to a 409 without string-matching.

    THE MESSAGE NAMES THE GESTURE THAT REPAIRS, and names it correctly. It used
    to be half French and half English ("Limite d'essai atteinte ... allowed for
    this organization. Delete a Datastream existant ...") and it asked for the
    wrong act: the counter reads ``enabled = TRUE``, so TURNING ONE OFF frees an
    allowance and DELETING is never required. It also stays a sentence rather
    than a promise of a control -- ``organization-settings.md:67`` records that
    no self-serve upgrade path exists, and a message implying a button that
    leads nowhere would be worse than the sentence saying so.
    """

    code = "trial_datastream_limit"

    def __init__(self, *, org_id: str, limit: int, current: int) -> None:
        self.org_id = org_id
        self.limit = limit
        self.current = current
        self.message = (
            f"This organization is on a trial plan and already has {current} of "
            f"{limit} Datastreams running. Turn off a Datastream you no longer "
            f"collect, or move the organization to a full plan, then start this "
            f"one again."
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
    """Return the owning org_id of a credential.

    Lu DIRECTEMENT sur le credential : `owner_org_id` porte l'organisation ou il
    est utilisable (architecture-org-tenancy 3.5). Le detour par le projet etait
    un vestige d'avant cette colonne -- il donnait la mauvaise reponse des que le
    credential servait un autre projet de la meme org, et se serait mis a rendre
    NULL le jour ou `connection_ref.project_id` sera retire.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT owner_org_id FROM app.connection_ref WHERE id = %s",
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
    org_id: str | None = None,
) -> None:
    """Refuse the act of making one MORE datastream active, past the org's cap.

    Resolves the org from *project_id*, reads ``max_datastreams`` (34.1). ``None``
    (full/internal) => no guard. Otherwise, when the org already has
    ``>= max_datastreams`` active datastreams, raise TrialDatastreamLimitError
    (the surface maps it to 409) and record a ``trial_limit_reached`` signal.

    CALLED BY EVERY WRITER OF ``enabled = TRUE``, which is the column the counter
    reads -- not by every door in front of them. The full census, taken over the
    whole tree on 2026-08-21 (``grep -rn "enabled = TRUE" --include=*.py
    --include=*.sql`` plus every ``INSERT INTO app.datastreams``, tests aside),
    is five statements and all five arrive here:

      * creation -- ``datastreams.create_datastream``, before the INSERT;
      * publish-activate -- ``datastream_activation.publish_activate_mutation``,
        the single act behind both the wizard and the MCP publication tool;
      * schedule re-arming -- ``schedule_mcp.set_schedule``;
      * dataset rollback -- ``dataset_recovery.rollback_dataset``, which restores
        ``lifecycle_state='active', enabled=TRUE`` on a recovered pointer;
      * the maintenance sweep -- ``datastreams.backfill_datastreams``, whose
        INSERT writes the flag literally.

    The last two were added on 2026-08-21. Rollback was the live hole, and the
    argument that closed it as harmless -- "the state is already counted" -- is
    false for a PAUSED Datastream: ``schedule_mcp`` stops one by writing
    ``enabled = FALSE`` while leaving ``lifecycle_state = 'active'`` and
    ``archived_at NULL``, and this counter reads ``enabled``. So three running,
    pause one, activate a fourth, roll the paused one back = four running on a
    plan of three. Guarding doors instead of acts is exactly how the cap gets
    walked around; each of the five calls sits INSIDE its mutation, after the
    lock and before the write, so a door opened tomorrow cannot pass underneath.

    Always called BEFORE the write it guards, so a refusal creates no partial
    state and existing datastreams are never altered. Callers that may be
    re-writing an ALREADY active row (a republication, a re-arm of a running
    Datastream) must skip the call rather than pass it: such an act changes no
    count, and counting it would refuse the org its own last Datastream.

    *org_id* lets a caller that ALREADY HOLDS the owning org skip the lookup.
    ``publish_activate_mutation`` locks the Datastream row with ``FOR UPDATE``
    and reads ``d.org_id`` there, so re-deriving the org from the project would
    be a second round-trip inside that lock -- and a round-trip to a different
    column: this guard counts by ``datastreams.org_id``, so the org carried by
    the row being activated is the authoritative one, not the project's.

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
        org_id = org_id or _resolve_org_for_project(project_id, conn)
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


def count_active_datastreams(org_id: str, conn=None) -> int:
    """Public read of the SAME number the refusal above compares against.

    A trial counter that displays anything other than what
    ``check_datastream_limit`` counts is worse than no counter: it says ``2/3``
    while the create is already refused, or ``3/3`` while a fourth still lands.
    So the read surface does not re-derive the count -- it calls the exact
    private counter, and the two can only ever disagree by a schema change that
    breaks both at once.

    *conn* is optional so a caller already inside a transaction reuses it
    (same posture as ``check_datastream_limit``); when omitted the function
    opens and closes its own connection.
    """
    if conn is not None:
        return _count_active_datastreams(org_id, conn)

    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as own_conn:
        return _count_active_datastreams(org_id, own_conn)


# ---------------------------------------------------------------------------
# 34.3 -- backfill window clamp.
# ---------------------------------------------------------------------------


#: The requested window ends BEFORE the org's backfill floor -- so there is no
#: day that is both asked for and entitled. It is not a reduced window, it is an
#: empty one, and it is a different fact from a clamp.
WINDOW_BEFORE_CEILING = "window_before_backfill_ceiling"


def _clamp_date_from(date_from: str, max_backfill_days: int, today: date) -> tuple[str, bool]:
    """Return (clamped_date_from, was_clamped).

    Floor = today - max_backfill_days. If the requested date_from is older than
    the floor, raise it to the floor (honest clamp). Otherwise unchanged.

    IT DOES NOT LOOK AT `date_to`, AND IT MUST NOT: that comparison belongs to
    `clamp_backfill_window`, which is the only function holding both ends of the
    window. See the invariant written there -- this helper raising `date_from`
    past a `date_to` it cannot see is exactly how the inverted rows were written.
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
    "max_backfill_days": int|None, "empty": bool, "reason": str|None,
    "floor": str|None}``. Never rejects the pull -- the caller runs on the
    (possibly) reduced window and surfaces ``clamped`` to the user. THAT PROMISE
    IS KEPT: this function still decides nothing, it only reports one more fact.

    **THE INVARIANT, added 2026-08-06: it never returns ``date_from > date_to``.**

    It used to. The floor is ``today - max_backfill_days`` and ``_clamp_date_from``
    raises ``date_from`` to it without ever seeing ``date_to``, so a trial org
    re-collecting a day older than its ceiling got a window whose start was AFTER
    its end -- measured on 2026-08-06::

        asked 2026-06-15..2026-06-15  ceiling 30d  ->  date_from=2026-07-07   INVERTED
        asked 2026-06-15..2026-06-15  ceiling  7d  ->  date_from=2026-07-30   INVERTED
        asked 2026-08-01..2026-08-01  ceiling 30d  ->  date_from=2026-08-01   fine

    That row went into ``app.pull_jobs`` and **nothing downstream refused it**:
    ``grep -rn "date_from > date_to" server/core`` returns nothing. It is not a
    reduced window, it is a line that means nothing, and a run left on it.

    So when the floor lands after ``date_to`` the intersection of "asked for" and
    "entitled to" is EMPTY, and that is a different fact from a clamp: it gets
    ``empty: True`` with ``reason`` and the ceiling's ``floor``, and **the dates
    come back exactly as they were asked for**. Nothing is moved, because there is
    nowhere honest to move them to -- a one-day window pinned on the floor would
    be a window nobody requested, and pinning it on ``date_to`` would collect a day
    the organisation is not entitled to. The CALLER decides what to do with an
    empty window; ``queue.enqueue_pull`` refuses it by name rather than queueing a
    window that cannot mean anything.

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
        # The window has no entitled day at all. `False` everywhere the ceiling
        # does not apply, so a reader never has to tell absent from false.
        "empty": False,
        "reason": None,
        "floor": None,
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

    floor = today - timedelta(days=max_days)
    result["floor"] = floor.isoformat()

    # THE GUARD. Both ends are known here and nowhere else, so this is where the
    # window is checked for meaning. A malformed `date_to` falls through to the
    # clamp below exactly as a malformed `date_from` does -- the pull path is what
    # validates dates, and a guard that raised here would turn a bad request into
    # a 500.
    try:
        if was_clamped and floor > date.fromisoformat(date_to):
            logger.info(
                "trial_enforcement: backfill_window_before_ceiling org=%s conn=%s "
                "%s..%s floor=%s (max=%dd)",
                org_id, connection_ref_id, date_from, date_to, result["floor"], max_days,
            )
            result["clamped"] = True
            result["empty"] = True
            result["reason"] = WINDOW_BEFORE_CEILING
            # The dates are the ones that were ASKED FOR. Raising `date_from` here
            # is what wrote `date_from > date_to` into `app.pull_jobs`.
            return result
    except (TypeError, ValueError):
        pass

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
