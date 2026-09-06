"""The ONE dispatch of a Datastream run -- by the clock or by hand.

WHY THIS MODULE EXISTS. A run had two entrances that did not agree.

The nightly and hourly dispatchers refuse a Datastream that is stopped, still a
draft, archived, hung off an inactive connection, registered read-only, or whose
module the project disabled; and every window they queue is first given a LINE
-- an `app.datastream_executions` row -- so the collection is visible while it is
happening (story 63.1). The manual door, `POST /api/datastreams/{id}/run`, did
none of it: `queue.enqueue_pull` straight away, no gate and no execution. So a
Datastream a person had deliberately STOPPED ran anyway and spent the source
account's quota, and the run it started appeared on no screen -- neither in the
runs list nor in the progress the Workbench reads -- because nothing had declared
it.

    A run is the same object whoever asked for it. Its gates and its
    declaration therefore live HERE, and both entrances call them.

WHAT IS NOT SHARED, ON PURPOSE. The three entrances still choose WHICH rows they
consider and WHICH window each run covers -- a set query for "who is due", one
row for "this one, now" -- because those are genuinely different questions. What
is shared is everything that happens once a row and a window are known: the
refusals, the execution, the queueing, the closing.

THE REFUSALS, in the order they are asked. Each names the gesture that repairs
it, never the column that failed:

    read_only_source    -- an `external_bq` registration is READ, never pulled (12.7)
    pushed_source       -- a `managed_feed` receives files; nothing is fetched
    not_configured      -- no connection to pull from
    not_armed           -- enabled/lifecycle_state say this Datastream is not running
    connection_inactive -- the connection is disabled or needs reconnecting
    not_published       -- no current plan or mapping version to run against
    module_disabled     -- the project turned this Connector off
    project_inactive    -- the project itself is not active

The two kinds come FIRST because they have no connection by design: asked in the
other order, the single active Datastream of the live base -- a `managed_feed` --
was told to "link a connection", which is not a gesture it has.

A gate answers from what the ROW declares. A caller that does not project a
column gets `row_incomplete` rather than a silent pass: a guard that covers half
the fleet reads exactly like a guard that covers all of it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "DISPATCH_ROW_COLUMNS",
    "DISPATCH_ROW_SELECT",
    "MISSED_RUN_REFUSALS",
    "DispatchOutcome",
    "DispatchRefusal",
    "ORIGIN_MANUAL",
    "dispatch_windows",
    "gate_refusal",
    "load_dispatch_row",
]

#: `app.datastream_executions.origin` of a run a person asked for by hand.
#: `scheduler.SCHEDULER_NIGHTLY` / `SCHEDULER_HOURLY` are the clock's.
ORIGIN_MANUAL = "manual_run"

#: Every key a gate reads. Projected identically by the three entrances so that
#: "the column is absent" and "the column says no" can never be confused.
DISPATCH_ROW_COLUMNS = (
    "ds_id",
    "project_id",
    "connection_ref_id",
    "cr_status",
    "cr_enabled",
    "enabled",
    "lifecycle_state",
    # THE COLUMN THE ARCHIVE ACTUALLY WRITES -- added 2026-08-18.
    # `gate_refusal` refused `lifecycle_state = 'archived'` and NOTHING EVER SETS
    # IT: `core.datastreams.delete_datastream` soft-archives with
    # `enabled = FALSE, archived_at = NOW()` and leaves `lifecycle_state` alone,
    # and `schedule_mcp._run_state` derives the word `archived` from
    # `archived_at is not None` for exactly that reason. Two consequences, both
    # of them real:
    #   * the manual door answered "This Datastream is stopped. Start it on the
    #     Schedule panel" for an archived one -- while the Schedule panel says an
    #     archived Datastream cannot be started there and must be restored. Two
    #     screens, one Datastream, opposite instructions;
    #   * `/refetch` calls the gate with `require_armed=False`, which relaxes the
    #     `enabled` branch -- the ONLY branch an archive trips. So an archived
    #     Datastream could be re-collected and spend at the provider, against the
    #     rule this module's own comment states ("archived: a soft-deleted
    #     Datastream is restored, not collected into").
    "archived_at",
    "source_kind",
    "current_plan_version_id",
    "current_mapping_version_id",
    "module_enabled",
    "project_status",
)

#: The SELECT list + FROM of ONE dispatchable row. Held as text because the three
#: entrances append their own WHERE: "everything due tonight", "every hourly
#: one", "this id". The JOINs are LEFT on purpose -- a missing plan version must
#: come back as `not_published` and not as an empty result set, so the door can
#: say WHICH gesture is missing instead of "introuvable".
DISPATCH_ROW_SELECT = """
    SELECT ds.id                          AS ds_id,
           ds.project_id                  AS project_id,
           ds.module_name                 AS module_name,
           ds.refetch_days                AS refetch_days,
           ds.date_window_days            AS date_window_days,
           ds.window_offset_days          AS window_offset_days,
           ds.schedule_mode               AS schedule_mode,
           ds.source_kind                 AS source_kind,
           ds.enabled                     AS enabled,
           ds.lifecycle_state             AS lifecycle_state,
           ds.archived_at                 AS archived_at,
           ds.current_plan_version_id     AS current_plan_version_id,
           ds.current_mapping_version_id  AS current_mapping_version_id,
           cr.id                          AS connection_ref_id,
           cr.status                      AS cr_status,
           cr.enabled                     AS cr_enabled,
           pm.enabled                     AS module_enabled,
           p.status                       AS project_status
      FROM app.datastreams ds
      LEFT JOIN app.projects p        ON p.id = ds.project_id
      LEFT JOIN app.connection_ref cr ON cr.id = ds.connection_ref_id
      LEFT JOIN app.project_modules pm
             ON pm.project_id = ds.project_id AND pm.module_name = ds.module_name
"""

_MISSING = object()

#: The states a REAL `app.pull_jobs` row can carry back out of `enqueue_pull`:
#: `queued` for the row it just wrote, `running` for a window already in flight
#: that deduplicated into an existing row. Anything else is a refusal wearing a
#: job's shape (AI-301) -- see `_is_refusal`.
_QUEUED_STATES = frozenset({"queued", "running"})


class DispatchRefusal(Exception):
    """A named reason this Datastream will not run, and the gesture that fixes it."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class DispatchOutcome:
    """What one dispatch produced: the run's line, and the windows it queued."""

    execution_id: str | None
    jobs: list[dict] = field(default_factory=list)
    windows: tuple[dict, ...] = ()


def _is_refusal(job: Any) -> bool:
    """Did `enqueue_pull` refuse this window instead of queueing it? (AI-301)

    `enqueue_pull` returns a job-shaped dict whose `state` is NOT a stored queue
    state when it declines -- `refused` for an authorization or topology denial,
    and the same shape for a backfill ceiling that keeps no day. The surface
    document already named this ("`enqueue_pull` répond une entrée en forme de
    job dont l'`state` n'est aucun état enregistré"); nothing read it.

    Positive test on the states a real row can hold, not a blacklist of refusal
    codes: a refusal spelt in a new way must read as a refusal here, and a
    deduplicated window -- `queued`/`running`, a real row someone else made --
    must not.
    """
    if not isinstance(job, Mapping):
        return False
    return str(job.get("state") or "") not in _QUEUED_STATES


def _declared(row: Mapping[str, Any], key: str) -> Any:
    value = row.get(key, _MISSING)
    if value is _MISSING:
        raise DispatchRefusal(
            "row_incomplete",
            f"This run could not be checked: the Datastream was read without {key}.",
        )
    return value


#: The gate refusals that mean A PULL WAS OWED TONIGHT AND DID NOT HAPPEN.
#:
#: Read POSITIVELY, which is AI-301's third rule: this names what counts as a
#: missed occurrence, so a refusal code added tomorrow is NOT silently charged to
#: every stream that carries it. The three codes here share one property -- the
#: Datastream was armed, its moment had come, and something outside it stopped
#: the collection. Someone has a gesture that would release it.
#:
#: The refusals deliberately ABSENT matter more than the ones present:
#:   * `read_only_source` and `pushed_source` -- an external BigQuery table and a
#:     managed feed owe no fetch BY DESIGN. Files are pushed to them. Counting
#:     these would mark every file-source Datastream permanently late, which is
#:     the same lie as `0` in the other direction. Measured: the nightly SELECT
#:     excludes `external_bq` but NOT `managed_feed`, so this one really arrives.
#:   * `not_armed` -- archived, draft or stopped. A stream whose clock is off has
#:     missed nothing; that is what stopping it meant.
MISSED_RUN_REFUSALS = frozenset(
    {"not_configured", "connection_inactive", "module_disabled", "project_inactive"}
)


def gate_refusal(
    row: Mapping[str, Any], *, require_armed: bool = True
) -> DispatchRefusal | None:
    """The first gate this row fails, or None when it may run.

    Pure: it reads the row and nothing else, so every entrance can call it before
    it spends a query, and every case below is offline-testable.

    `require_armed=False` for a RE-COLLECTION of named past days. Arming governs
    the CLOCK -- "whether any of this will ever happen by itself". The ratified
    surface says stopping a Datastream loses days "that only come back through a
    day-by-day re-collection", so refusing that re-collection because the stream
    is stopped would refuse the very repair the screen offers. Every other gate
    still applies: a re-collection is still a provider call.
    """
    try:
        # WHAT THIS DATASTREAM IS comes first, because the two kinds below have
        # no connection BY DESIGN and "link a connection" would be the wrong
        # sentence to hand someone. Measured on the live base: the single active
        # Datastream is a `managed_feed`, and it read `not_configured`.
        source_kind = str(_declared(row, "source_kind") or "connector_pull")
        if source_kind == "external_bq":
            return DispatchRefusal(
                "read_only_source",
                "This Datastream reads an existing BigQuery table; there is "
                "nothing to collect from a provider.",
            )
        if source_kind == "managed_feed":
            return DispatchRefusal(
                "pushed_source",
                "This Datastream receives files that are pushed to it, so there "
                "is nothing to go and fetch. Send a file, or import one from the "
                "Datastream's own page.",
            )
        if not _declared(row, "connection_ref_id"):
            return DispatchRefusal(
                "not_configured",
                "This Datastream is not linked to a connection yet. "
                "Link a connection before running it.",
            )
        lifecycle = str(_declared(row, "lifecycle_state") or "").strip()
        # TWO STATES ARE REFUSED WHATEVER THE ENTRANCE. `require_armed` relaxes
        # exactly one thing -- STOPPED -- and neither of these is it:
        #   * archived: a soft-deleted Datastream is restored, not collected into;
        #   * draft: it has never published a day, so there is no day to
        #     re-collect either. Measured on the live base, where a draft with a
        #     plan and a mapping would otherwise have been re-collectable.
        #
        # ASKED OF THE COLUMN THE ARCHIVE WRITES (2026-08-18). This read
        # `lifecycle_state == 'archived'` alone, and no writer in this repository
        # ever sets that value -- see `DISPATCH_ROW_COLUMNS` above. `archived_at`
        # is what `delete_datastream` stamps and what `_run_state` reads, so it
        # leads; the lifecycle word is still honoured, because the CHECK
        # constraint of migration 137 permits it and a later writer may use it.
        if _declared(row, "archived_at") is not None or lifecycle == "archived":
            return DispatchRefusal(
                "not_armed",
                "This Datastream is archived. Restore it before collecting "
                "anything for it.",
            )
        if lifecycle == "draft":
            return DispatchRefusal(
                "not_armed",
                "This Datastream has never been published. Publish it from the "
                "wizard before collecting anything for it.",
            )
        if require_armed and (not _declared(row, "enabled") or lifecycle != "active"):
            return DispatchRefusal(
                "not_armed",
                "This Datastream is stopped. Start it on the Schedule panel "
                "before running it.",
            )
        if _declared(row, "cr_status") != "active" or not _declared(row, "cr_enabled"):
            return DispatchRefusal(
                "connection_inactive",
                "The connection this Datastream pulls from is not active. "
                "Reconnect it from Connections.",
            )
        if not _declared(row, "current_plan_version_id") or not _declared(
            row, "current_mapping_version_id"
        ):
            return DispatchRefusal(
                "not_published",
                "This Datastream has no published plan and mapping to run "
                "against. Publish it from the wizard first.",
            )
        module_enabled = _declared(row, "module_enabled")
        if module_enabled is not None and not module_enabled:
            return DispatchRefusal(
                "module_disabled",
                "This Connector is turned off for the project. Enable it in "
                "project settings before running this Datastream.",
            )
        project_status = _declared(row, "project_status")
        if project_status is not None and project_status != "active":
            return DispatchRefusal(
                "project_inactive",
                "This project is not active, so none of its Datastreams run.",
            )
    except DispatchRefusal as incomplete:
        return incomplete
    return None


def load_dispatch_row(conn, datastream_id: str, project_id: str) -> dict | None:
    """The one row the gates read, or None when no such Datastream exists.

    Scoped by project_id (AD-5). Returns the row even when it fails a gate --
    telling someone WHY their Datastream will not run is the whole point.
    """
    with conn.cursor() as cur:
        cur.execute(
            DISPATCH_ROW_SELECT + " WHERE ds.id = %s AND ds.project_id = %s",
            (datastream_id, project_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def dispatch_windows(
    get_connection: Callable[[], Any],
    *,
    row: Mapping[str, Any],
    windows: Sequence[Mapping[str, str]],
    queue,
    actor: str,
    origin: str,
    run_key: str,
    on_enqueued: Callable[[dict], None] | None = None,
    on_missed: Callable[[str], None] | None = None,
) -> DispatchOutcome:
    """Declare the run, queue its windows, close it. The effect both doors share.

    The gates are NOT re-run here: a caller that already refused must not queue,
    and a caller that filtered in SQL has already answered them. Call
    `gate_refusal` first -- both entrances do.

    `on_enqueued` runs after each successful window; the nightly dispatcher uses
    it to advance `next_run_at`, which a manual run deliberately does NOT do: a
    run asked for by hand answers a question now, it does not move the schedule
    a person set.

    `on_missed` is its exact counterpart, called with the refusal code when a
    window did NOT become a job. The two callbacks are wired by the same caller
    and for the same reason: a scheduled occurrence must move exactly one of the
    two counters, never neither. AI-301 stopped a refusal from advancing the
    clock but left it moving nothing at all, so a refused night was still
    indistinguishable from a healthy one on the Workbench. A manual run passes
    neither callback -- it has no schedule to keep honest.
    """
    from core.scheduler import _close_collection_run, _open_collection_run  # noqa: PLC0415

    queued = [dict(w) for w in windows]
    if not queued:
        return DispatchOutcome(execution_id=None)

    # Story 63.1: the run gets a LINE before its first window is queued. A
    # failure to open one never blocks the pull -- the data lands either way and
    # only the progress line is missing, which is honest rather than fabricated.
    execution_id = _open_collection_run(
        get_connection,
        ds=dict(row),
        windows=queued,
        actor=actor,
        origin=origin,
        run_key=run_key,
    )

    jobs: list[dict] = []
    connection_ref_id = row["connection_ref_id"]
    for window in queued:
        try:
            job = queue.enqueue_pull(
                connection_ref_id,
                window["date_from"],
                window["date_to"],
                requested_by=actor,
                datastream_id=row["ds_id"],
                execution_id=execution_id,
                # AI-301: the module this row DECLARES. The gate inside
                # `enqueue_pull` used to re-read it on an RLS-armed connection
                # that could not see the row.
                module_name=row.get("module_name"),
            )
            jobs.append(job)
            # AI-301: A REFUSAL IS NOT A WINDOW. `enqueue_pull` answers a refusal
            # as a VALUE, not an exception -- so this loop counted `access_denied`
            # as work queued, called `on_enqueued`, and the nightly dispatcher
            # advanced `next_run_at`. Five days of collection were refused at this
            # exact line while `missed_run_count` stayed 0 and nothing was logged
            # above DEBUG. The refusal still does not raise -- one window must not
            # kill the rest -- but it no longer moves the schedule, and it is said.
            if _is_refusal(job):
                logger.warning(
                    "datastream_dispatch: enqueue_refused ds=%s conn=%s origin=%s "
                    "code=%s window=%s..%s",
                    row.get("ds_id"),
                    connection_ref_id,
                    origin,
                    job.get("code"),
                    window.get("date_from"),
                    window.get("date_to"),
                )
                if on_missed is not None:
                    on_missed(str(job.get("code") or "enqueue_refused"))
                continue
            if on_enqueued is not None:
                on_enqueued(job)
        except Exception as exc:  # noqa: BLE001 -- one window must not kill the rest
            logger.warning(
                "datastream_dispatch: enqueue_error ds=%s conn=%s origin=%s: %s",
                row.get("ds_id"),
                connection_ref_id,
                origin,
                exc,
            )
            # A window lost to an exception is as uncollected as a refused one.
            # Swallowing it so the other windows survive is right; leaving the
            # schedule with nothing to show for it is what this counter fixes.
            if on_missed is not None:
                on_missed("enqueue_error")

    # A run that took NO window of its own (every one deduplicated into a run
    # already in flight) is closed here and now. Left open it would hold
    # `uq_datastream_executions_active` and answer 409 to every later publish.
    _close_collection_run(get_connection, execution_id, actor)
    return DispatchOutcome(
        execution_id=execution_id, jobs=jobs, windows=tuple(queued)
    )
