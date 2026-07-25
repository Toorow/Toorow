"""toorow -- Nightly pull scheduler (Story 3.4, AC1, AC2).

Dispatches pull jobs for every enabled connection across a rolling 8-day window:
  - Re-pull window: yesterday-7 through yesterday-1 (7 days; captures late corrections)
  - Fresh window:   yesterday through yesterday (1 day; the new data)

Together they cover 8 calendar days per nightly run. Each window is a separate
call to enqueue_pull() so the queue worker can parallelise and retry independently.

Environment variables
---------------------
SCHEDULER_ENABLED         default "false" -- set to "true" to start the daemon thread
SCHEDULER_NIGHTLY_HOUR    default "2"     -- local hour (0-23) to fire
SCHEDULER_NIGHTLY_MINUTE  default "0"     -- local minute (0-59) to fire

Windows / local dev
-------------------
Set SCHEDULER_ENABLED=true in .env to activate the in-process daemon thread.
The thread checks every 60 seconds and fires once per calendar day when
hour:minute matches. No cron, Task Scheduler, or external tool required.

Production (GCP, Phase B)
--------------------------
When QUEUE_BACKEND=cloud_tasks, Cloud Scheduler sends a POST to
POST /internal/scheduler/dispatch-nightly on the Cloud Run service.
The in-process thread can remain enabled or disabled independently.

Hard gates
----------
HG-1: no live GCP at P3-dev -- Cloud Scheduler endpoint is stub-only.
HG-2 (AD-2): never imports from server/modules/*, never names a provider.
HG-3 (AD-7): pull_id is minted by enqueue_pull(), not here.
HG-5: SCHEDULER_ENABLED defaults to false -- thread never starts in CI/tests.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AI-32 (Story 6.1, AC10) -- nightly piggyback failure isolation.
#
# Each nightly step (dispatch_nightly + the three alert-check piggybacks) runs
# inside _run_isolated_step: per-step duration logging, a timeout guard driven by
# ALERT_TIMEOUT_SECONDS (default 60), and a meta-alert row inserted into
# app.alert_firings on failure/timeout so the next alert-delivery cycle surfaces
# a "scheduler_health" alert. One step failing NEVER prevents the others running.
# ---------------------------------------------------------------------------


def _alert_timeout_seconds() -> float:
    """Return the per-step soft-timeout in seconds (ALERT_TIMEOUT_SECONDS, default 60).

    AI-32 (b): env-configurable soft timeout for nightly steps.  Read the same way
    as other scheduler env vars (os.environ.get with a default string).
    """
    try:
        return float(os.environ.get("ALERT_TIMEOUT_SECONDS", "60"))
    except ValueError:
        return 60.0


def _insert_meta_alert(step: str, reason: str) -> None:
    """Insert a type='meta_alert' row into app.alert_firings (AI-32, AC10.3).

    Surfaced by the normal alert-delivery path (fetch_recent_alert_firings picks
    up meta_alert rows) on the next cycle. Never raises -- graceful degradation.
    """
    try:
        from ulid import ULID  # noqa: PLC0415

        from core.db import get_connection  # noqa: PLC0415

        firing_id = f"fire_{ULID()}"
        window_date = date.today()
        message = f"Nightly step '{step}' failed: {reason}"
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.alert_firings
                        (id, definition_id, type, project_id, metric, fired_at,
                         observed_value, threshold, pull_ids, window_date, severity, message)
                    VALUES (%s, NULL, 'meta_alert', 'default', 'scheduler_health', %s,
                            0, 0, '{}', %s, 'error', %s)
                    """,
                    (firing_id, datetime.now(tz=timezone.utc), window_date, message),
                )
            conn.commit()
        logger.warning("scheduler: meta_alert_inserted step=%s reason=%s", step, reason)
    except Exception as exc:  # noqa: BLE001
        logger.warning("scheduler: meta_alert_insert_failed step=%s: %s", step, exc)


def _write_scheduler_step_degraded_alert(degraded_steps: list[str]) -> None:
    """Write ONE type='scheduler_step_degraded' firing after a nightly run (AI-32, c).

    Called by run_nightly_steps when at least one step failed or exceeded
    ALERT_TIMEOUT_SECONDS.  Lists all affected step names in the message so a
    single glance at the alert table reveals the scope of the degradation.

    Never raises -- best-effort, logs at debug on failure.
    """
    try:
        from core import infra_alerts  # noqa: PLC0415

        infra_alerts.write_infra_firing(
            alert_type="scheduler_step_degraded",
            project_id="default",
            metric="scheduler_health",
            severity="error",
            message=f"Nightly scheduler degraded: steps={','.join(degraded_steps)}",
            metadata={"degraded_steps": degraded_steps},
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("scheduler: scheduler_step_degraded_alert_failed: %s", exc)


def _run_isolated_step(
    step_name: str,
    fn,
    *args,
    _degraded: list[str] | None = None,
    **kwargs,
):
    """Run one nightly step with duration logging + soft-timeout guard (AI-32).

    AI-32 (a) -- per-step duration logging:
        Logs ``scheduler: step=<name> duration_ms=<int>`` at INFO for every step
        (success or failure), using time.monotonic.

    AI-32 (b) -- ALERT_TIMEOUT_SECONDS soft timeout (Windows-safe):
        The step runs synchronously (no inner thread -- no signal, no thread kill).
        After the call returns, the elapsed duration is compared against
        ALERT_TIMEOUT_SECONDS. If exceeded: logs a WARNING and records the step
        name in *_degraded* for the post-run meta-alert (AI-32 c).

    AI-32 (c) -- degraded tracking:
        *_degraded* is a mutable list owned by run_nightly_steps.  This function
        appends the step name when the step failed (raised) or exceeded the soft
        timeout, so the caller can write ONE consolidated alert after all steps.

    NEVER re-raises: the caller's loop continues to the next step.

    Returns the step's return value on success, else None.
    """
    timeout = _alert_timeout_seconds()

    t0 = time.monotonic()
    value = None
    error: Exception | None = None
    try:
        value = fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        error = exc
    elapsed_ms = round((time.monotonic() - t0) * 1000)

    # AI-32 (a): duration log -- always, success or failure.
    logger.info("scheduler: step=%s duration_ms=%d", step_name, elapsed_ms)

    # AI-32 (b): soft-timeout check (measurement only; step already finished).
    timed_out = elapsed_ms > timeout * 1000
    if timed_out:
        logger.warning(
            "scheduler: step=%s exceeded ALERT_TIMEOUT_SECONDS=%d duration_ms=%d",
            step_name,
            int(timeout),
            elapsed_ms,
        )
        if _degraded is not None:
            _degraded.append(step_name)

    if error is not None:
        logger.warning(
            "scheduler: nightly_step_failed step=%s error=%s", step_name, error
        )
        _insert_meta_alert(step_name, str(error))
        if _degraded is not None and step_name not in _degraded:
            _degraded.append(step_name)
        return None

    return value


def _run_due_notebooks() -> None:
    """Run all notebooks with scheduled=TRUE and schedule_rule='nightly' (Story 6.6, AC1).

    Called as the LAST step in run_nightly_steps so dispatch + alert checks
    always complete regardless of notebook failures.

    Each notebook is individually isolated: one failure does not prevent others
    from running.  A meta-alert row is inserted for each failed notebook (AI-32
    / AD-13 pattern).

    Dedup guarantee: this function inserts a new run on every nightly call.
    The dedup (one scheduled run per notebook per night) is enforced by the
    caller pattern -- run_nightly_steps fires at most once per calendar day
    (guarded by _last_fired_date in _scheduler_loop).
    """
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.main import run_notebook_direct  # noqa: PLC0415
    except Exception as exc:
        logger.warning("scheduler: run_due_notebooks: import_error: %s", exc)
        return

    # Query all notebooks that are scheduled for nightly runs
    due_notebooks: list[dict] = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, title
                    FROM app.notebooks
                    WHERE scheduled = TRUE AND schedule_rule = 'nightly'
                    """
                )
                cols = [d[0] for d in cur.description]
                due_notebooks = [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:
        logger.warning("scheduler: run_due_notebooks: db_query_error: %s", exc)
        _insert_meta_alert("run_due_notebooks", f"DB query failed: {exc}")
        return

    success_count = 0
    for nb in due_notebooks:
        nb_id = nb["id"]
        try:
            run_notebook_direct(notebook_id=nb_id, as_of=None)
            success_count += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "scheduler: nightly_notebook_failed: notebook_id=%s error=%s", nb_id, exc
            )
            _insert_meta_alert("run_due_notebooks", f"notebook {nb_id} failed: {exc}")

    logger.info(
        "scheduler: nightly_notebooks_run: count=%d total=%d",
        success_count,
        len(due_notebooks),
    )


def _run_due_briefings(nightly_run_id: str) -> None:
    """Build morning briefings for all active projects (Story 6.7, AC4).

    Called as the LAST step in run_nightly_steps (after run_due_notebooks) so that
    fresh alert_firings from the nightly alert checks feed the briefing builder.

    For each project with at least one enabled connection:
      1. Determine today's briefing_date in the project's reporting timezone.
      2. Skip if a briefing already exists for (project_id, briefing_date) -- idempotent.
      3. Fetch alert_firings (last 24h, type != 'meta_alert').
      4. Fetch rollup via compute_rollup() for the project's ad-hoc daily data.
      5. Fetch context_events (last 7 days).
      6. Call build_briefing() -- pure, no DB/warehouse inside.
      7. INSERT INTO app.morning_briefings ON CONFLICT DO NOTHING.

    Failure of one project does not block others (per-project try/except).
    Logs briefings_built count + per-failure project_id.

    BRIEFING_ENABLED guard (default "false") -- consistent with other step guards.
    """
    if os.environ.get("BRIEFING_ENABLED", "false").lower() != "true":
        logger.debug("scheduler: briefings_skipped -- BRIEFING_ENABLED not true")
        return

    try:
        from core.briefing import build_briefing  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415
    except Exception as exc:
        logger.warning("scheduler: run_due_briefings: import_error: %s", exc)
        return

    # Fetch all distinct project_ids with at least one connection
    project_ids: list[str] = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT project_id
                    FROM app.connection_ref
                    WHERE project_id IS NOT NULL
                    """
                )
                project_ids = [row[0] for row in cur.fetchall()]
    except Exception as exc:
        logger.warning("scheduler: run_due_briefings: db_query_projects_error: %s", exc)
        _insert_meta_alert("run_due_briefings", f"DB projects query failed: {exc}")
        return

    if not project_ids:
        logger.info("scheduler: run_due_briefings: no_projects_found")
        return

    briefings_built = 0

    for project_id in project_ids:
        try:
            _build_project_briefing(
                project_id=project_id,
                nightly_run_id=nightly_run_id,
                get_connection=get_connection,
                build_briefing=build_briefing,
            )
            briefings_built += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "scheduler: briefing_failed: project_id=%s error=%s",
                project_id,
                exc,
            )

    logger.info(
        "scheduler: briefings_built: count=%d total_projects=%d",
        briefings_built,
        len(project_ids),
    )


def _build_project_briefing(
    project_id: str,
    nightly_run_id: str,
    get_connection,
    build_briefing,
) -> None:
    """Build and store one morning briefing row for *project_id*.

    Raises on any error (caller catches per-project).
    """
    import json as _json  # noqa: PLC0415

    from ulid import ULID  # noqa: PLC0415

    # Determine today's briefing_date (project timezone -- default Europe/Paris per FR4).
    tz_name = os.environ.get("SCHEDULER_TIMEZONE", "Europe/Paris")
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        briefing_date = datetime.now(tz=ZoneInfo(tz_name)).date().isoformat()
    except Exception:
        briefing_date = date.today().isoformat()

    # Check for existing briefing (idempotency: ON CONFLICT DO NOTHING, but skip early)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM app.morning_briefings
                WHERE project_id = %s AND briefing_date = %s
                """,
                (project_id, briefing_date),
            )
            if cur.fetchone() is not None:
                logger.debug(
                    "scheduler: briefing_already_exists: project_id=%s date=%s",
                    project_id,
                    briefing_date,
                )
                return

    # Fetch alert_firings (last 24h, type != 'meta_alert')
    alert_firings: list[dict] = []
    try:
        from core import business_alerts as _ba  # noqa: PLC0415

        with get_connection() as conn:
            # fetch_recent_alert_firings returns type='business_threshold' rows
            _biz_firings = _ba.fetch_recent_alert_firings(project_id, conn, hours=24)
        from core import anomaly_alerts as _aa  # noqa: PLC0415

        with get_connection() as conn:
            _anomaly_firings = _aa.fetch_recent_anomaly_firings(project_id, conn, hours=24)
        from core import mediaplan_alerts as _ma  # noqa: PLC0415

        with get_connection() as conn:
            _mediaplan_firings = _ma.fetch_recent_mediaplan_firings(
                project_id, conn, hours=24
            )
        alert_firings = _biz_firings + _anomaly_firings + _mediaplan_firings
    except Exception as exc:
        logger.debug(
            "scheduler: briefing_alert_fetch_failed: project_id=%s: %s", project_id, exc
        )
        # Continue with empty alert_firings (briefing still built from rollup)

    # Fetch rollup via warehouse query for the project's daily data
    rollup: dict = {}
    try:
        from core import warehouse as _wh  # noqa: PLC0415
        from core.rollup import compute_rollup  # noqa: PLC0415

        yesterday = (date.today() - timedelta(days=1)).isoformat()
        two_days_ago = (date.today() - timedelta(days=2)).isoformat()
        rows = _wh.query_daily_report(project_id, two_days_ago, yesterday, None)
        if rows:
            metrics = sorted({r.get("metric") for r in rows if r.get("metric")})
            pull_ids = sorted({r.get("pull_id") for r in rows if r.get("pull_id")})
            rollup = compute_rollup(rows, metrics, two_days_ago, yesterday, project_id, pull_ids)
    except Exception as exc:
        logger.debug(
            "scheduler: briefing_rollup_failed: project_id=%s: %s", project_id, exc
        )
        # Continue with empty rollup (briefing still built from alerts)

    # Fetch context_events (last 7 days)
    context_events: list[dict] = []
    try:
        from core.context_events import fetch_context_events  # noqa: PLC0415

        seven_days_ago = (date.today() - timedelta(days=7)).isoformat()
        today_str = date.today().isoformat()
        context_events = fetch_context_events(project_id, seven_days_ago, today_str)
    except Exception as exc:
        logger.debug(
            "scheduler: briefing_context_events_failed: project_id=%s: %s", project_id, exc
        )

    # Build briefing (pure function -- no DB/warehouse)
    insights_json = build_briefing(
        project_id=project_id,
        briefing_date=briefing_date,
        alert_firings=alert_firings,
        rollup=rollup,
        context_events=context_events,
        nightly_run_id=nightly_run_id,
    )

    # INSERT ON CONFLICT DO NOTHING (idempotency)
    brief_id = f"brief_{ULID()}"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.morning_briefings
                    (id, project_id, briefing_date, insights, built_at, nightly_run_id)
                VALUES (%s, %s, %s, %s::jsonb, NOW(), %s)
                ON CONFLICT (project_id, briefing_date) DO NOTHING
                """,
                (
                    brief_id,
                    project_id,
                    briefing_date,
                    _json.dumps(insights_json),
                    nightly_run_id,
                ),
            )
        conn.commit()

    logger.info(
        "scheduler: briefing_built: project_id=%s date=%s run_id=%s",
        project_id,
        briefing_date,
        nightly_run_id,
    )


# ---------------------------------------------------------------------------
# Advisory lock constant for nightly double-fire protection (G-scheduler)
# Hash of "connector_atlas_nightly_scheduler" reduced to a positive int64.
# Hardcoded so it is stable across deployments and does not rely on DB data.
# Value: int("connector_atlas_nightly_scheduler".encode().hex(), 16) % (2**63)
# = 5765169104872814411 (computed offline, documented here for auditability).
# ---------------------------------------------------------------------------
_NIGHTLY_ADVISORY_LOCK_KEY: int = 5765169104872814411


def _try_advisory_lock() -> bool | None:
    """Attempt to acquire pg_try_advisory_lock(_NIGHTLY_ADVISORY_LOCK_KEY).

    Returns:
        True  -- lock acquired; caller holds it until pg_advisory_unlock.
        False -- lock not acquired; another instance holds it.
        None  -- Postgres unavailable; caller should proceed without the lock
                 so a local single-instance deployment stays functional.

    Never raises.
    """
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_try_advisory_lock(%s)", (_NIGHTLY_ADVISORY_LOCK_KEY,)
                )
                row = cur.fetchone()
            # No commit needed -- advisory locks survive the transaction.
        return bool(row[0]) if row is not None else None
    except Exception as exc:
        logger.warning(
            "scheduler: advisory_lock_unavailable: %s -- proceeding without lock", exc
        )
        return None


def _release_advisory_lock() -> None:
    """Release pg_advisory_unlock(_NIGHTLY_ADVISORY_LOCK_KEY). Never raises."""
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_unlock(%s)", (_NIGHTLY_ADVISORY_LOCK_KEY,)
                )
            conn.commit()
    except Exception as exc:
        logger.warning("scheduler: advisory_lock_release_failed: %s", exc)


def run_nightly_steps(as_of_date: date) -> None:
    """Run all nightly steps in order, each isolated (AI-32, AC10).

    dispatch_nightly -> infra alert check -> business alert check -> anomaly check
    -> mediaplan pacing check (Story 22.6, FR9)
    -> run_due_notebooks (Story 6.6, AC1)
    -> run_due_briefings (Story 6.7, AC4 -- LAST step, always after notebooks).

    A failure/timeout in any one step is logged + meta-alerted but does NOT stop
    the subsequent steps (failure isolation).

    AI-32 (c): after all steps, if any step degraded (failed or soft-timeout),
    ONE consolidated type='scheduler_step_degraded' firing is written via
    _write_scheduler_step_degraded_alert (best-effort, never raises).

    Double-fire protection (G-scheduler): acquires a Postgres advisory lock at
    the start.  If another instance already holds the lock, logs at INFO and
    returns early (skip).  If Postgres is unavailable, proceeds WITHOUT the lock
    so a local single-instance deployment stays functional.

    Step order (Story 6.7 / 19.1, Dev Notes):
        1. dispatch_nightly      (data pulls)
        1b. _run_rebuild_cache   (read-through cache snapshot -- Story 19.1, after dispatch/dbt)
        1c. _run_schema_context_gen (schema-context docs -- Story 11.2, after rebuild_cache)
        2. _run_alert_check      (infra alerts)
        3. _run_business_alert_check  (business thresholds)
        4. _run_anomaly_alert_check   (anomalies)
        4b. _run_mediaplan_alert_check (pacing mediaplan -- Story 22.6, FR9)
        5. _run_dq_monitors_check     (DQ monitors -- Story 8.6)
        6. _run_due_notebooks    (scheduled notebooks -- Story 6.6)
        7. _run_due_briefings    (morning briefing -- Story 6.7, LAST)
    """
    # G-scheduler: advisory lock prevents double-fire across multiple instances.
    lock_acquired = _try_advisory_lock()
    if lock_acquired is False:
        # Another instance is running nightly steps right now.
        logger.info(
            "scheduler: nightly_skipped: another instance holds the advisory lock"
        )
        return

    # lock_acquired is True (we hold it) or None (Postgres unavailable -- proceed).
    try:
        # Mint a nightly_run_id for this run (audit + briefing provenance).
        from ulid import ULID  # noqa: PLC0415

        nightly_run_id = f"nrun_{ULID()}"
        logger.info(
            "scheduler: nightly_run_started: run_id=%s date=%s", nightly_run_id, as_of_date
        )

        # AI-32 (c): track degraded steps (failed or soft-timeout) for the post-run
        # consolidated meta-alert.  _degraded is passed to every _run_isolated_step call.
        _degraded: list[str] = []

        _run_isolated_step(
            "dispatch_nightly", dispatch_nightly, _degraded=_degraded, as_of_date=as_of_date
        )
        # Story 24.4 (AC3): materialise per-org marts AFTER dispatch (fresh raw) and
        # AFTER the central mirror sync (AD-8), BEFORE rebuild_cache so the cache sees
        # fresh marts. Isolated (AI-32) + DBT_NIGHTLY_ENABLED guarded (default off) --
        # a failed per-org build never blocks the remaining steps.
        _run_isolated_step("dbt_per_org", _run_dbt_per_org, _degraded=_degraded)
        # Story 19.1 (AD-22): rebuild the read-through cache right after dispatch/dbt,
        # so downstream reads hit a fresh snapshot. Isolated (AI-32) + TOOROW_CACHE_ENABLED
        # guarded (default off) -- a failed rebuild never blocks the remaining steps.
        _run_isolated_step("rebuild_cache", _run_rebuild_cache, _degraded=_degraded)
        # Story 11.2: regenerate schema-context docs from the freshly consolidated
        # marts, AFTER dispatch/dbt/rebuild_cache and BEFORE alert checks. Isolated
        # (AI-32) + SCHEMA_CONTEXT_ENABLED guarded (default off) -- a failed run
        # never blocks the alert/notebook/briefing steps.
        _run_isolated_step("schema_context_gen", _run_schema_context_gen, _degraded=_degraded)
        _run_isolated_step("alert_check", _run_alert_check, _degraded=_degraded)
        _run_isolated_step("business_alert_check", _run_business_alert_check, _degraded=_degraded)
        _run_isolated_step("anomaly_alert_check", _run_anomaly_alert_check, _degraded=_degraded)
        # Story 22.6 (FR9): mediaplan pacing alert check after anomaly checks.
        _run_isolated_step(
            "mediaplan_alert_check", _run_mediaplan_alert_check, _degraded=_degraded
        )
        # Story 8.6 (AC1): DQ monitors run after anomaly checks, before notebooks.
        _run_isolated_step("dq_monitors", _run_dq_monitors_check, _degraded=_degraded)
        # Story 6.6 (AC1): scheduled notebooks run after alerts.
        _run_isolated_step("run_due_notebooks", _run_due_notebooks, _degraded=_degraded)
        # Story 6.7 (AC4): morning briefing is ALWAYS the LAST step.
        # Runs after notebooks so fresh alert_firings and notebook outputs are available.
        _run_isolated_step(
            "run_due_briefings", _run_due_briefings, nightly_run_id, _degraded=_degraded
        )

        # AI-32 (c): emit ONE consolidated meta-alert if any step degraded.
        if _degraded:
            _write_scheduler_step_degraded_alert(_degraded)
    finally:
        if lock_acquired is True:
            _release_advisory_lock()


# ---------------------------------------------------------------------------
# AC1 -- compute_nightly_work
# ---------------------------------------------------------------------------


def compute_nightly_work(connection_ref_id: str, as_of_date: date) -> list[dict]:
    """Compute the pull-window list for one connection for a nightly run.

    Returns exactly two window dicts:
      [0] re-pull window: yesterday-7 through yesterday-1 (7-day re-pull)
      [1] fresh window:   yesterday through yesterday (new data)

    ULID ordering guarantees the new pull_ids supersede older ones in the
    dbt QUALIFY dedup in the staging layer (AD-7, Story 3.4).

    Args:
        connection_ref_id: The connection ref ID (used only for logging here;
                           enqueue_pull handles DB look-up).
        as_of_date:        The reference date (usually date.today()).

    Returns:
        [{"date_from": str, "date_to": str}, {"date_from": str, "date_to": str}]
    """
    yesterday = as_of_date - timedelta(days=1)
    # Re-pull window: 7 days PRIOR to yesterday (exclusive of yesterday itself)
    repull_from = yesterday - timedelta(days=7)
    repull_to = yesterday - timedelta(days=1)
    # Fresh window: yesterday only
    fresh_from = yesterday
    fresh_to = yesterday

    windows = [
        {"date_from": repull_from.isoformat(), "date_to": repull_to.isoformat()},
        {"date_from": fresh_from.isoformat(), "date_to": fresh_to.isoformat()},
    ]

    logger.debug(
        "scheduler: compute_nightly: conn=%s windows=%s",
        connection_ref_id,
        windows,
    )
    return windows


# ---------------------------------------------------------------------------
# AC1 -- dispatch_nightly
# ---------------------------------------------------------------------------


def _dispatch_nightly_datastreams(
    as_of_date: date,
    requested_by: str,
    queue,
    get_connection,
) -> tuple[list[dict], int]:
    """Story 8.2: dispatch nightly jobs iterating ENABLED datastreams.

    AI-46: Pull window precedence (per-stream, highest to lowest):
        1. date_window_days  -- per-stream override; set explicitly in the UI/API
                                (migration 023; column exists since Epic 8, default 30).
                                When non-NULL and >0 this is used as the window length.
        2. refetch_days      -- legacy per-stream fallback; used when date_window_days IS
                                NULL or 0 (schema NOT NULL DEFAULT 3 so NULL only in tests
                                or rows inserted without the column before migration 023).
        3. Global default 3  -- if both are NULL or 0 (defensive only, schema prevents it
                                for real rows but unit test mocks may omit columns).

    Window formula: [yesterday - (window_days - 1), yesterday]
    The window size is exactly window_days calendar days.

    Current behaviour when both columns are at their schema defaults:
        date_window_days=30, refetch_days=3  ->  window = 30 days  (AI-46 activates)
    Pre-AI-46 behaviour (refetch_days only):
        refetch_days=3  ->  window = 3 days
    To preserve old 3-day behaviour: set date_window_days=NULL in the datastream row
    (or omit it in test mocks that only provide refetch_days).

    Respects module enablement (app.project_modules) and connection status.
    Passes datastream_id to enqueue_pull.

    Returns (all_jobs, datastream_count).
    """
    yesterday = as_of_date - timedelta(days=1)
    all_jobs: list[dict] = []
    datastream_count = 0

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        ds.id               AS ds_id,
                        ds.project_id,
                        ds.module_name,
                        ds.refetch_days,
                        ds.date_window_days,
                        ds.source_kind,
                        cr.id               AS connection_ref_id,
                        cr.status           AS cr_status,
                        cr.enabled          AS cr_enabled
                    FROM app.datastreams ds
                    JOIN app.projects p
                        ON p.id = ds.project_id AND p.status = 'active'
                    LEFT JOIN app.connection_ref cr
                        ON cr.id = ds.connection_ref_id
                    LEFT JOIN app.project_modules pm
                        ON pm.project_id = ds.project_id
                        AND pm.module_name = ds.module_name
                    WHERE ds.enabled = TRUE
                      AND ds.schedule_mode = 'nightly'
                      -- Story 12.7: never dispatch a provider pull for an external_bq
                      -- (read-only) registration. Filtered EXPLICITLY in the app layer
                      -- below on source_kind (migration 030, always present) so the
                      -- always-running scheduler does not hard-depend on migration 076.
                      AND COALESCE(ds.source_kind, 'connector_pull') <> 'external_bq'
                      AND (pm.enabled IS NULL OR pm.enabled = TRUE)
                    """
                )
                cols = [d[0] for d in cur.description]
                datastream_rows = [dict(zip(cols, row)) for row in cur.fetchall()]
                # Belt-and-braces (MEDIUM-1): the app-layer filter is now effective
                # because source_kind is selected above -- an EXPLICIT invariant, not the
                # accidental side effect of external_bq rows lacking a connection_ref.
                from core.external_bq_registration import (  # noqa: PLC0415
                    exclude_external_bq_dispatch,
                )

                datastream_rows = exclude_external_bq_dispatch(datastream_rows)
    except Exception as exc:
        logger.warning(
            "scheduler: dispatch_nightly: db_error reading datastreams: %s", exc
        )
        return [], 0

    for ds in datastream_rows:
        ds_id = ds["ds_id"]
        conn_id = ds["connection_ref_id"]
        cr_status = ds.get("cr_status")
        cr_enabled = ds.get("cr_enabled")

        if conn_id is None:
            # Datastream not yet linked to a connection -- skip silently.
            logger.debug(
                "scheduler: datastream_no_connection ds_id=%s", ds_id
            )
            continue

        if cr_status != "active" or not cr_enabled:
            logger.debug(
                "scheduler: datastream_connection_inactive ds_id=%s conn=%s",
                ds_id,
                conn_id,
            )
            continue

        # AI-46: resolve pull window length from per-stream columns.
        # Precedence: date_window_days > refetch_days > global default 3.
        raw_date_window = ds.get("date_window_days")
        raw_refetch = ds.get("refetch_days")
        if raw_date_window is not None and int(raw_date_window) > 0:
            window_days = int(raw_date_window)
            logger.debug(
                "scheduler: ds=%s using date_window_days=%d", ds_id, window_days
            )
        elif raw_refetch is not None and int(raw_refetch) > 0:
            window_days = int(raw_refetch)
            logger.debug(
                "scheduler: ds=%s using refetch_days=%d (date_window_days absent)",
                ds_id, window_days,
            )
        else:
            window_days = 3
            logger.debug(
                "scheduler: ds=%s using global_default=3 (both columns absent)",
                ds_id,
            )
        # Window: [yesterday - (window_days - 1), yesterday]
        date_from = (yesterday - timedelta(days=window_days - 1)).isoformat()
        date_to = yesterday.isoformat()

        # Story 26.1 (C): refetch ladder. When the module's manifest declares a
        # "refetch" block, the nightly dispatch enqueues the ladder windows for
        # the resolved cadence (nightly/weekly/monthly) instead of the single
        # default window. Absent block (every current module) returns exactly
        # [default window] -- behaviour bit-identical (AD-22); any resolution
        # failure also falls back to the default window (never blocks dispatch).
        from core import refetch as _refetch  # noqa: PLC0415

        windows = _refetch.windows_for_nightly_dispatch(
            ds["module_name"],
            as_of_date,
            {"date_from": date_from, "date_to": date_to},
        )

        datastream_count += 1
        for window in windows:
            try:
                job = queue.enqueue_pull(
                    conn_id,
                    window["date_from"],
                    window["date_to"],
                    requested_by=requested_by,
                    datastream_id=ds_id,
                )
                all_jobs.append(job)
            except Exception as exc:
                logger.warning(
                    "scheduler: dispatch_nightly: enqueue_error ds=%s conn=%s: %s",
                    ds_id,
                    conn_id,
                    exc,
                )

    return all_jobs, datastream_count


def _dispatch_hourly_datastreams(
    as_of_date: date,
    requested_by: str,
    queue,
    get_connection,
) -> tuple[list[dict], int]:
    """Story 12.6 (Phase-B debt close): dispatch RECURRING HOURLY datastreams.

    A lean sibling of _dispatch_nightly_datastreams (NOT a refactor of it -- the
    nightly path's per-connection fallback + refetch ladder are nightly concerns).
    Differences from nightly:
      * WHERE ds.schedule_mode = 'hourly' (migration 087 widened the CHECK).
      * The window is anchored to TODAY inclusive ([today - (window-1), today]) so an
        hourly run captures the day's accumulating data (date-grain invariant: still a
        DATE window, never an hour -- a more frequent re-pull of the same daily grain).
      * No per-connection fallback (that is a nightly backfill concept).
    Provider calls still go through the SHARED queue (quota/pagination/retry/circuit-
    breaker); raw lands with pull_id (AD-7). external_bq is excluded (Story 12.7).

    Returns (all_jobs, datastream_count).
    """
    today = as_of_date
    all_jobs: list[dict] = []
    datastream_count = 0

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        ds.id               AS ds_id,
                        ds.project_id,
                        ds.module_name,
                        ds.refetch_days,
                        ds.date_window_days,
                        ds.source_kind,
                        cr.id               AS connection_ref_id,
                        cr.status           AS cr_status,
                        cr.enabled          AS cr_enabled
                    FROM app.datastreams ds
                    JOIN app.projects p
                        ON p.id = ds.project_id AND p.status = 'active'
                    LEFT JOIN app.connection_ref cr
                        ON cr.id = ds.connection_ref_id
                    LEFT JOIN app.project_modules pm
                        ON pm.project_id = ds.project_id
                        AND pm.module_name = ds.module_name
                    WHERE ds.enabled = TRUE
                      AND ds.schedule_mode = 'hourly'
                      AND COALESCE(ds.source_kind, 'connector_pull') <> 'external_bq'
                      AND (pm.enabled IS NULL OR pm.enabled = TRUE)
                    """
                )
                cols = [d[0] for d in cur.description]
                datastream_rows = [dict(zip(cols, row)) for row in cur.fetchall()]
                from core.external_bq_registration import (  # noqa: PLC0415
                    exclude_external_bq_dispatch,
                )

                datastream_rows = exclude_external_bq_dispatch(datastream_rows)
    except Exception as exc:
        logger.warning("scheduler: dispatch_hourly: db_error reading datastreams: %s", exc)
        return [], 0

    for ds in datastream_rows:
        ds_id = ds["ds_id"]
        conn_id = ds["connection_ref_id"]
        if conn_id is None:
            logger.debug("scheduler: hourly_datastream_no_connection ds_id=%s", ds_id)
            continue
        if ds.get("cr_status") != "active" or not ds.get("cr_enabled"):
            logger.debug("scheduler: hourly_datastream_connection_inactive ds_id=%s", ds_id)
            continue

        raw_date_window = ds.get("date_window_days")
        raw_refetch = ds.get("refetch_days")
        if raw_date_window is not None and int(raw_date_window) > 0:
            window_days = int(raw_date_window)
        elif raw_refetch is not None and int(raw_refetch) > 0:
            window_days = int(raw_refetch)
        else:
            window_days = 3
        # Today-inclusive window (hourly re-pull of the current accumulating day).
        date_from = (today - timedelta(days=window_days - 1)).isoformat()
        date_to = today.isoformat()

        datastream_count += 1
        try:
            job = queue.enqueue_pull(
                conn_id,
                date_from,
                date_to,
                requested_by=requested_by,
                datastream_id=ds_id,
            )
            all_jobs.append(job)
        except Exception as exc:
            logger.warning(
                "scheduler: dispatch_hourly: enqueue_error ds=%s conn=%s: %s",
                ds_id, conn_id, exc,
            )

    return all_jobs, datastream_count


def dispatch_hourly(
    as_of_date: date | None = None,
    *,
    requested_by: str = "scheduler",
) -> list[dict]:
    """Enqueue hourly pull jobs per enabled hourly datastream (Story 12.6 debt).

    Idempotent (enqueue_pull dedups a pending window); AD-2/AD-7/AD-12 as nightly.
    No per-connection fallback (hourly is opt-in per datastream).
    """
    if as_of_date is None:
        as_of_date = date.today()
    from core import queue  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    all_jobs, _count = _dispatch_hourly_datastreams(
        as_of_date, requested_by, queue, get_connection
    )
    return all_jobs


def _run_managed_feed_syncs() -> None:
    """Story 12.10 dispatch hook: run enabled daily/hourly managed-feed sync schedules.

    Env-guarded (MANAGED_FEED_SYNC_ENABLED, default off). Reads
    app.managed_feed_sync_schedule and calls google_sheets_sync.dispatch_managed_feed_sync
    per schedule, each isolated. PHASE B: the live Google Sheets adapter injection is
    deferred (sheets_adapter=None -> run_sync raises NotImplementedError, caught here per
    schedule) and the atomic pointer swap needs live-warehouse rows (AI-08). The loop,
    table read, cadence filter, and per-stream isolation are wired + testable now.
    """
    import os  # noqa: PLC0415

    if os.environ.get("MANAGED_FEED_SYNC_ENABLED", "false").lower() != "true":
        logger.debug("scheduler: managed_feed_syncs_skipped -- flag not true")
        return

    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT datastream_id, project_id, connection_id, spreadsheet_id,
                       sheet_range, sheet_name, column_mapping, cadence_mode,
                       cadence_policy, quota_profile, last_watermark
                FROM app.managed_feed_sync_schedule
                WHERE enabled = TRUE AND cadence_mode <> 'manual'
                """
            )
            cols = [d[0] for d in cur.description]
            due = [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:
        logger.warning("scheduler: managed_feed_syncs: db_error reading schedules: %s", exc)
        return

    from core.google_sheets_sync import dispatch_managed_feed_sync  # noqa: PLC0415

    for sched in due:
        try:
            with get_connection() as conn:
                dispatch_managed_feed_sync(
                    datastream_id=sched["datastream_id"],
                    project_id=sched["project_id"],
                    connection_id=sched["connection_id"],
                    spreadsheet_id=sched["spreadsheet_id"],
                    sheet_range=sched["sheet_range"],
                    sheet_name=sched.get("sheet_name") or "",
                    column_mapping=sched.get("column_mapping") or {},
                    plan_version_id="",
                    mapping_version_id="",
                    projection_plan={},
                    cadence_policy=sched.get("cadence_policy"),
                    quota_profile=sched.get("quota_profile"),
                    actor="scheduler",
                    conn=conn,
                    sheets_adapter=None,  # PHASE_B_LIVE_BLOCKED: inject the 15.6 adapter
                    last_committed_watermark=sched.get("last_watermark"),
                )
                conn.commit()
        except Exception as exc:  # per-schedule isolation
            logger.warning(
                "scheduler: managed_feed_sync failed ds=%s: %s",
                sched.get("datastream_id"), exc,
            )


def run_hourly_steps(as_of_date: date | None = None) -> None:
    """Run the recurring HOURLY steps, each isolated (mirrors run_nightly_steps).

    Steps: dispatch_hourly (Story 12.6) + managed_feed_syncs (Story 12.10, env-guarded).
    Double-fire protected by the shared advisory lock; a step failure/timeout is logged
    and meta-alerted but never blocks the other step.
    """
    if as_of_date is None:
        as_of_date = date.today()

    lock_acquired = _try_advisory_lock()
    if lock_acquired is False:
        logger.info("scheduler: hourly_skipped: another instance holds the advisory lock")
        return
    try:
        _degraded: list[str] = []
        _run_isolated_step(
            "dispatch_hourly", dispatch_hourly, _degraded=_degraded, as_of_date=as_of_date
        )
        _run_isolated_step("managed_feed_syncs", _run_managed_feed_syncs, _degraded=_degraded)
        if _degraded:
            _write_scheduler_step_degraded_alert(_degraded)
    finally:
        if lock_acquired is True:
            _release_advisory_lock()


def dispatch_nightly(
    as_of_date: date | None = None,
    *,
    requested_by: str = "scheduler",
) -> list[dict]:
    """Enqueue nightly pull jobs per enabled datastream (Story 8.2).

    Story 8.2 change: iterates ENABLED datastreams of active projects (primary path).
    Fallback: for projects that have ZERO datastreams (pre-backfill or new projects
    without any configured streams), falls back to the legacy per-connection dispatch
    so nothing breaks before the backfill runs.

    The fallback path uses the original 8-day rolling window
    (compute_nightly_work: yesterday-7..yesterday-1 + yesterday).
    The datastream path uses refetch_days from each datastream's config.

    AD-2: never imports from server/modules/*.
    AD-7: pull_id is minted inside enqueue_pull(); this function never mints IDs.
    AD-12: never calls any third-party API -- it only enqueues.

    idempotent: re-dispatching an already-queued (pending) window is safe --
    enqueue_pull() returns the existing job with deduplicated=true (review-3-2 F-2).

    Args:
        as_of_date:    Reference date. Defaults to date.today() when None.
        requested_by:  Identity string written to audit rows (default "scheduler").

    Returns:
        List of all job dicts returned by enqueue_pull().
    """
    if as_of_date is None:
        as_of_date = date.today()

    from core import queue  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    # ---------------------------------------------------------------------------
    # Story 8.2: Primary path -- dispatch per enabled datastream.
    # ---------------------------------------------------------------------------
    all_jobs, datastream_count = _dispatch_nightly_datastreams(
        as_of_date, requested_by, queue, get_connection
    )

    # ---------------------------------------------------------------------------
    # Fallback: legacy per-connection dispatch for projects with ZERO datastreams.
    #
    # Determines which project_ids are covered by at least one enabled datastream,
    # then fetches connections for the uncovered projects and enqueues the legacy
    # 8-day rolling windows for them.
    # ---------------------------------------------------------------------------
    try:
        with get_connection() as conn:
            # Active connections for projects NOT yet covered (legacy fallback).
            # Story 7.2 (AC3): module enablement filter preserved.
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT cr.id, cr.provider, cr.project_id
                    FROM app.connection_ref cr
                    JOIN app.projects p ON p.id = cr.project_id AND p.status = 'active'
                    LEFT JOIN app.project_modules pm
                        ON pm.project_id = cr.project_id
                        AND pm.module_name = cr.provider
                    WHERE cr.status = 'active'
                      AND cr.enabled = TRUE
                      AND (pm.enabled IS NULL OR pm.enabled = TRUE)
                      AND cr.project_id NOT IN (
                          SELECT DISTINCT project_id FROM app.datastreams WHERE enabled = TRUE
                      )
                    """
                )
                cols = [desc[0] for desc in cur.description]
                legacy_rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:
        logger.warning(
            "scheduler: dispatch_nightly: db_error reading legacy connections: %s", exc
        )
        legacy_rows = []

    legacy_count = 0
    for ref in legacy_rows:
        conn_id = ref["id"]
        windows = compute_nightly_work(conn_id, as_of_date)
        for window in windows:
            try:
                job = queue.enqueue_pull(
                    conn_id,
                    window["date_from"],
                    window["date_to"],
                    requested_by=requested_by,
                )
                all_jobs.append(job)
                legacy_count += 1
            except Exception as exc:
                logger.warning(
                    "scheduler: dispatch_nightly: enqueue_error conn=%s window=%s: %s",
                    conn_id,
                    window,
                    exc,
                )

    logger.info(
        "scheduler: dispatch_nightly: date=%s datastreams=%d legacy_conns=%d jobs=%d",
        as_of_date,
        datastream_count,
        len(legacy_rows),
        len(all_jobs),
    )

    # Legacy-only log line (kept for backward compat with log parsers expecting the old format).
    connection_count = datastream_count + len(legacy_rows)
    logger.debug(
        "scheduler: dispatch_nightly: date=%s connections=%d jobs=%d",
        as_of_date,
        connection_count,
        len(all_jobs),
    )

    # AC8 (Story 4.4): run mirror sync after extraction dispatch.
    # Guard with SYNC_ENABLED env var (default "true"). Set SYNC_ENABLED=false in CI
    # environments without Postgres.
    # HG-3: when SYNC_ENABLED=false, sync_tables() is NOT called.
    if os.environ.get("SYNC_ENABLED", "true").lower() != "false":
        try:
            from core import mirror_sync  # noqa: PLC0415

            sync_result = mirror_sync.sync_tables()
            synced = sync_result.get("synced", {})
            lag = sync_result.get("lag_seconds", 0.0)
            logger.info(
                "nightly_mirror_sync: %s lag=%.2fs",
                " ".join(f"{k}={v}" for k, v in synced.items()),
                lag,
            )
        except Exception as exc:
            logger.warning("scheduler: mirror_sync_error: %s", exc)
    else:
        logger.debug("scheduler: mirror_sync skipped -- SYNC_ENABLED=false")

    return all_jobs


# ---------------------------------------------------------------------------
# Story 5.1 (AC6) -- dbt-run span
# ---------------------------------------------------------------------------


def run_dbt(
    args: list[str] | None = None,
    *,
    trace_id: str | None = None,
    _runner=None,
) -> dict:
    """Run ``dbt run`` (or ``dbt <args>``) as a subprocess inside a trace span.

    Emits a span carrying ``dbt.exit_code``, ``dbt.models_run`` and
    ``dbt.latency_ms``, linked to the triggering job's *trace_id* when available
    (AC6). No-op tracing when TRACING_ENABLED=false. Never raises on tracing
    failure -- only a real subprocess error propagates.

    ``_runner`` is injectable for tests (defaults to ``subprocess.run``) so the span
    attribute recording can be verified without a live dbt install.

    Returns ``{"exit_code": int, "models_run": int, "latency_ms": int}``.
    """
    import subprocess  # noqa: PLC0415
    import time as _time  # noqa: PLC0415

    from core import tracing  # noqa: PLC0415

    cmd = ["dbt", *(args or ["run"])]
    runner = _runner or subprocess.run

    parent_meta = None
    if trace_id:
        tp = tracing.traceparent_from_trace_id(trace_id)
        if tp:
            parent_meta = {"traceparent": tp}

    t0 = _time.perf_counter()
    with tracing.worker_span("dbt.run", parent_meta=parent_meta) as span:
        completed = runner(cmd, capture_output=True, text=True)
        latency_ms = int((_time.perf_counter() - t0) * 1000)
        exit_code = int(getattr(completed, "returncode", 0) or 0)
        # dbt prints a summary line like "Completed successfully ... N models"; we
        # count "OK ... " lines as a portable models_run estimate (best-effort).
        stdout = getattr(completed, "stdout", "") or ""
        models_run = sum(
            1 for line in stdout.splitlines() if " OK " in line or line.startswith("OK")
        )
        span.set("dbt.exit_code", exit_code)
        span.set("dbt.models_run", models_run)
        span.set("dbt.latency_ms", latency_ms)

    return {"exit_code": exit_code, "models_run": models_run, "latency_ms": latency_ms}


# ---------------------------------------------------------------------------
# Story 24.4 (AC3) -- one dbt build per ACTIVE org (org-partitioned marts)
# ---------------------------------------------------------------------------


def run_dbt_per_org(
    *,
    trace_id: str | None = None,
    _runner=None,
    _list_orgs=None,
) -> dict:
    """Run ``dbt build`` ONCE per active org, each into its ``org_<wslug>_marts``.

    Story 24.4 (AC3): the raw already lives per org (24.3) and the sources read
    ``org_<wslug>_raw`` under ``--vars org=<wslug>`` (the generate_schema_name /
    raw_source_schema macros). This orchestrates ONE isolated dbt run per active
    org so each client's marts materialise in ITS schema.

    Org list & slug come from ``warehouse_tenancy.list_active_orgs`` -- the single
    naming point (never composed here). The BARE ``warehouse_slug`` is passed as
    ``--vars '{"org": "<wslug>"}'``; the ``org_..._marts`` composition lives ONLY
    in the dbt macro (invariant epic-24).

    Isolation (AC3): a failing org is logged + meta-alerted but does NOT abort the
    other orgs' runs (per-org failure isolation). The mirror is NOT synced here
    (AD-8: ``mirror_sync`` stays the single central Postgres->warehouse path).

    Guard: ``DBT_NIGHTLY_ENABLED`` (default off, consistent with the other nightly
    ``_ENABLED`` steps). When the flag is off OR there is no active org, this is a
    no-op returning ``{"status": "disabled"|"skipped", "orgs": 0}``.

    ``_runner`` / ``_list_orgs`` are injectable for tests (defaults:
    ``subprocess.run`` via ``run_dbt`` and ``warehouse_tenancy.list_active_orgs``).

    Returns a summary dict::
        {"status": "ok"|"disabled"|"skipped", "orgs": N, "ok": k, "failed": m,
         "results": [{"warehouse_slug": s, "exit_code": int|None,
                      "status": "ok"|"failed"}]}
    """
    if os.environ.get("DBT_NIGHTLY_ENABLED", "false").lower() != "true":
        logger.debug("scheduler: dbt_per_org_skipped -- DBT_NIGHTLY_ENABLED not true")
        return {"status": "disabled", "orgs": 0, "ok": 0, "failed": 0, "results": []}

    from core import warehouse_tenancy  # noqa: PLC0415

    list_orgs = _list_orgs or warehouse_tenancy.list_active_orgs
    orgs = list_orgs()
    if not orgs:
        logger.info("scheduler: dbt_per_org_no_active_org")
        return {"status": "skipped", "orgs": 0, "ok": 0, "failed": 0, "results": []}

    results: list[dict] = []
    ok = failed = 0
    for schemas in orgs:
        wslug = schemas.warehouse_slug
        # dbt build --vars '{"org": "<wslug>"}' -- the BARE slug; the macro composes
        # org_<wslug>_marts / _staging and routes raw_* to org_<wslug>_raw.
        vars_json = json.dumps({"org": wslug})
        try:
            res = run_dbt(
                ["build", "--vars", vars_json],
                trace_id=trace_id,
                _runner=_runner,
            )
            exit_code = int(res.get("exit_code", 0) or 0)
        except Exception as exc:  # noqa: BLE001 -- one org failure != abort the others
            logger.warning(
                "scheduler: dbt_per_org_error org_slug=%s wslug=%s: %s",
                schemas.org_slug,
                wslug,
                exc,
            )
            _insert_meta_alert("dbt_per_org", f"{schemas.org_slug}: {exc}")
            results.append({"warehouse_slug": wslug, "exit_code": None, "status": "failed"})
            failed += 1
            continue

        if exit_code == 0:
            ok += 1
            results.append({"warehouse_slug": wslug, "exit_code": 0, "status": "ok"})
        else:
            failed += 1
            logger.warning(
                "scheduler: dbt_per_org_nonzero org_slug=%s wslug=%s exit=%d",
                schemas.org_slug,
                wslug,
                exit_code,
            )
            _insert_meta_alert(
                "dbt_per_org", f"{schemas.org_slug}: dbt exit={exit_code}"
            )
            results.append(
                {"warehouse_slug": wslug, "exit_code": exit_code, "status": "failed"}
            )

    logger.info(
        "scheduler: dbt_per_org_complete orgs=%d ok=%d failed=%d",
        len(orgs),
        ok,
        failed,
    )
    return {
        "status": "ok",
        "orgs": len(orgs),
        "ok": ok,
        "failed": failed,
        "results": results,
    }


# ---------------------------------------------------------------------------
# AC2 -- _scheduler_loop (daemon thread body)
# ---------------------------------------------------------------------------


def _scheduler_loop(_check_interval_seconds: int = 60) -> None:
    """Infinite loop -- runs in the nightly-scheduler daemon thread.

    Checks the wall clock every _check_interval_seconds (default 60) and
    fires dispatch_nightly() once per calendar day when the local hour:minute
    matches SCHEDULER_NIGHTLY_HOUR:SCHEDULER_NIGHTLY_MINUTE.

    The _last_fired_date guard prevents double-firing if the sleep slips by
    a few seconds into the next minute.

    ASCII-only log strings (AI-03).
    """
    nightly_hour = int(os.environ.get("SCHEDULER_NIGHTLY_HOUR", "2"))
    nightly_minute = int(os.environ.get("SCHEDULER_NIGHTLY_MINUTE", "0"))
    hourly_minute = int(os.environ.get("SCHEDULER_HOURLY_MINUTE", "0"))
    _last_fired_date: date | None = None
    _last_hourly_key: tuple[date, int] | None = None

    while True:
        time.sleep(_check_interval_seconds)
        # review-3-4 F-2: "yesterday" is the PROJECT's day, not the server's.
        # Default Europe/Paris per FR4 (per-project tz arrives with Epic 4).
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        tz_name = os.environ.get("SCHEDULER_TIMEZONE", "Europe/Paris")
        now = datetime.now(tz=ZoneInfo(tz_name))
        # Story 12.6 (Phase-B debt): fire the recurring HOURLY steps at the top of each
        # hour, once per (date, hour). Env-guarded (SCHEDULER_HOURLY_ENABLED, default off)
        # so existing deployments are unchanged until hourly cadence is turned on.
        if (
            os.environ.get("SCHEDULER_HOURLY_ENABLED", "false").lower() == "true"
            and now.minute == hourly_minute
        ):
            hourly_key = (now.date(), now.hour)
            if _last_hourly_key != hourly_key:
                _last_hourly_key = hourly_key
                logger.info(
                    "scheduler: hourly_dispatch_fired: date=%s hour=%s",
                    now.date(), now.hour,
                )
                run_hourly_steps(now.date())
        if now.hour == nightly_hour and now.minute == nightly_minute:
            today = now.date()
            if _last_fired_date != today:
                _last_fired_date = today
                logger.info("scheduler: nightly_dispatch_fired: date=%s", today)
                # AI-32 (Story 6.1, AC10): each step runs isolated with duration
                # logging + timeout guard + meta-alert on failure. One failing step
                # never blocks the others. Order preserved:
                #   dispatch_nightly -> infra alerts (5.2) -> business alerts (5.3)
                #   -> anomaly alerts (5.4). Each piggyback still honours its own
                #   *_ENABLED guard internally.
                run_nightly_steps(today)


# ---------------------------------------------------------------------------
# Story 5.2 (AC3) -- _run_alert_check (scheduler piggyback)
# ---------------------------------------------------------------------------


def _run_alert_check() -> None:
    """Run the infra alert evaluator after nightly dispatch (Story 5.2, AC3).

    Reads ALERTS_ENABLED env var (default "false"). Only runs when true.
    Catches all exceptions to ensure the scheduler thread never crashes.
    Runs synchronously in the scheduler thread -- NOT a new thread.
    """
    if os.environ.get("ALERTS_ENABLED", "false").lower() != "true":
        logger.debug("scheduler: alerts_check_skipped -- ALERTS_ENABLED not true")
        return

    try:
        from core import infra_alerts  # noqa: PLC0415

        breaches = infra_alerts.evaluate_alerts()
        if breaches:
            channels = infra_alerts.build_channels()
            for breach in breaches:
                infra_alerts.notify_alert(breach, channels)
            logger.info(
                "scheduler: alert_check_complete: breaches=%d", len(breaches)
            )
        else:
            logger.debug("scheduler: alert_check_complete: all clear")
    except Exception as exc:
        logger.warning("scheduler: alert_check_error: %s", exc)


# ---------------------------------------------------------------------------
# Story 5.3 (AC6) -- _run_business_alert_check (scheduler piggyback)
# ---------------------------------------------------------------------------


def _run_business_alert_check() -> None:
    """Run the business threshold alert evaluator after nightly dispatch (Story 5.3, AC6).

    Reads BUSINESS_ALERTS_ENABLED env var (default "false"). Only runs when true.
    Catches all exceptions to ensure the scheduler thread never crashes.
    Runs synchronously in the scheduler thread -- NOT a new thread.

    SCHEDULER_ENABLED=false in CI means this function is never called (the scheduler
    thread never starts), satisfying T6.3.
    """
    if os.environ.get("BUSINESS_ALERTS_ENABLED", "false").lower() != "true":
        logger.debug(
            "scheduler: business_alerts_check_skipped -- BUSINESS_ALERTS_ENABLED not true"
        )
        return

    try:
        from core import business_alerts  # noqa: PLC0415

        firings = business_alerts.evaluate_business_alerts()
        if firings:
            logger.info(
                "scheduler: business_alert_check_complete: firings=%d", len(firings)
            )
            for firing in firings:
                logger.warning(
                    "scheduler: business_alert_fired: metric=%s observed=%.4f"
                    " op=%s threshold=%.4f firing_id=%s",
                    firing.get("metric"),
                    float(firing.get("observed_value", 0)),
                    firing.get("operator"),
                    float(firing.get("threshold", 0)),
                    firing.get("firing_id"),
                )
        else:
            logger.debug("scheduler: business_alert_check_complete: all clear")
    except Exception as exc:
        logger.warning("scheduler: business_alert_check_error: %s", exc)


# ---------------------------------------------------------------------------
# Story 5.4 (AC7) -- _run_anomaly_alert_check (scheduler piggyback)
# ---------------------------------------------------------------------------


def _run_dbt_per_org() -> None:
    """Isolated nightly wrapper for run_dbt_per_org (Story 24.4, AC3).

    Guard: DBT_NIGHTLY_ENABLED (default off) is re-checked inside run_dbt_per_org,
    which NEVER raises past its per-org isolation. Catches defensively anyway to
    honour the scheduler's failure-isolation contract (a per-org build failure must
    not break the nightly run).
    """
    if os.environ.get("DBT_NIGHTLY_ENABLED", "false").lower() != "true":
        logger.debug("scheduler: dbt_per_org_skipped -- DBT_NIGHTLY_ENABLED not true")
        return
    try:
        result = run_dbt_per_org()
        logger.info(
            "scheduler: dbt_per_org_step_complete status=%s orgs=%d ok=%d failed=%d",
            result.get("status"),
            result.get("orgs", 0),
            result.get("ok", 0),
            result.get("failed", 0),
        )
    except Exception as exc:  # noqa: BLE001 -- never break the nightly run
        logger.warning("scheduler: dbt_per_org_step_error: %s", exc)


def _run_rebuild_cache() -> None:
    """Rebuild the read-through DuckDB cache after nightly dispatch (Story 19.1, AD-22).

    Runs as an isolated nightly step (AI-32) AFTER dispatch_nightly/run_dbt so the
    cache is a snapshot of the freshly consolidated marts. Delegates to the reusable
    ``cache_warehouse.rebuild_cache`` on-demand entry point (also consumed by 19.3).

    Guard: TOOROW_CACHE_ENABLED (default "false", prudent -- the service runs fine
    with the cache off). ``rebuild_cache`` itself re-checks the flag and NEVER raises
    (invariant f), so a failed/absent-origin rebuild degrades to the BigQuery read
    path without breaking the nightly run or the service. Catches defensively anyway
    to honour the scheduler's failure-isolation contract.
    """
    if os.environ.get("TOOROW_CACHE_ENABLED", "false").lower() != "true":
        logger.debug("scheduler: rebuild_cache_skipped -- TOOROW_CACHE_ENABLED not true")
        return

    try:
        from core import cache_warehouse  # noqa: PLC0415

        result = cache_warehouse.rebuild_cache()
        logger.info(
            "scheduler: rebuild_cache_complete: status=%s tables=%d projects=%d",
            result.get("status"),
            len(result.get("tables", []) or []),
            len(result.get("project_ids", []) or []),
        )
    except Exception as exc:  # noqa: BLE001 -- invariant f: never break the nightly run
        logger.warning("scheduler: rebuild_cache_error: %s", exc)


def _run_schema_context_gen() -> None:
    """Regenerate schema-context docs after nightly dispatch (Story 11.2).

    Runs as an isolated nightly step (AI-32) AFTER dispatch_nightly / run_dbt /
    rebuild_cache so the profiled marts reflect the freshly consolidated data,
    and BEFORE the alert checks.

    Guard: SCHEMA_CONTEXT_ENABLED (default "false", prudent -- the service runs
    fine with schema-context generation off). Mirrors the _run_rebuild_cache
    pattern. Catches all exceptions so a failed run NEVER aborts the nightly run
    (failure isolation contract).

    AD-8: opens the warehouse connection read-only (structural enforcement).
    AD-17: delegates to the single-writer generator (schema_context_gen).

    Iterates all active projects with at least one connection and regenerates
    their schema context; per-project failures are logged but do not abort the
    remaining projects.
    """
    if os.environ.get("SCHEMA_CONTEXT_ENABLED", "false").lower() != "true":
        logger.debug(
            "scheduler: schema_context_gen_skipped -- SCHEMA_CONTEXT_ENABLED not true"
        )
        return

    try:
        from core.db import get_connection, get_warehouse_connection  # noqa: PLC0415
        from core.schema_context_gen import generate_schema_context  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        logger.warning("scheduler: schema_context_gen: import_error: %s", exc)
        return

    # Fetch active projects that have at least one connection.
    project_ids: list[str] = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT project_id
                    FROM app.connection_ref
                    WHERE project_id IS NOT NULL
                    """
                )
                project_ids = [row[0] for row in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001
        logger.warning("scheduler: schema_context_gen: db_query_error: %s", exc)
        return

    generated = 0
    for project_id in project_ids:
        try:
            with get_connection() as conn:
                with get_warehouse_connection(read_only=True) as warehouse_conn:
                    result = generate_schema_context(
                        conn,
                        project_id=project_id,
                        warehouse_conn=warehouse_conn,
                        changed_by="scheduler",
                    )
            logger.info(
                "scheduler: schema_context_gen: project=%s processed=%d updated=%d skipped=%d",
                project_id,
                result.get("processed", 0),
                result.get("updated", 0),
                result.get("skipped", 0),
            )
            generated += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "scheduler: schema_context_gen: project_failed project=%s error=%s",
                project_id,
                exc,
            )

    logger.info(
        "scheduler: schema_context_gen_complete: projects=%d total=%d",
        generated,
        len(project_ids),
    )


def _run_dq_monitors_check() -> None:
    """Run data quality monitors after nightly dispatch (Story 8.6).

    Reads DQ_MONITORS_ENABLED env var (default "true"). Skips when set to "false".
    Guards against CI/test runs via SCHEDULER_ENABLED (the thread never starts when false).
    Catches all exceptions to ensure the scheduler thread never crashes.
    Runs synchronously in the scheduler thread -- NOT a new thread.
    Called AFTER _run_anomaly_alert_check(), before notebooks.
    """
    if os.environ.get("DQ_MONITORS_ENABLED", "true").lower() == "false":
        logger.debug("scheduler: dq_monitors_skipped -- DQ_MONITORS_ENABLED=false")
        return

    try:
        from core import dq_monitors  # noqa: PLC0415

        summary = dq_monitors.run_dq_monitors()
        logger.info(
            "scheduler: dq_monitors_complete: evaluated=%d total_issues=%d errors=%d",
            summary.get("evaluated", 0),
            summary.get("total_issues", 0),
            summary.get("errors", 0),
        )
    except Exception as exc:
        logger.warning("scheduler: dq_monitors_error: %s", exc)


def _run_mediaplan_alert_check() -> None:
    """Evaluer les alertes pacing médiaplan après le run nightly (Story 22.6, FR9).

    Lit MEDIAPLAN_ALERTS_ENABLED (défaut "true" -- peu risqué, lecture seule des marts).
    Isole les erreurs : une exception n'interrompt jamais le nightly (AI-32).
    Branche sur le même chemin alert_firings que 5.3/5.4 -- pas de moteur parallèle.
    """
    if os.environ.get("MEDIAPLAN_ALERTS_ENABLED", "true").lower() != "true":
        logger.debug(
            "scheduler: mediaplan_alerts_check_skipped -- MEDIAPLAN_ALERTS_ENABLED not true"
        )
        return

    try:
        from core import mediaplan_alerts  # noqa: PLC0415

        firings = mediaplan_alerts.evaluate_mediaplan_alerts()
        if firings:
            logger.info(
                "scheduler: mediaplan_alert_check_complete: firings=%d", len(firings)
            )
            for firing in firings:
                logger.warning(
                    "scheduler: mediaplan_alert_fired: kind=%s level=%s plan=%s"
                    " pace=%.4f threshold=%.4f firing_id=%s",
                    firing.get("kind"),
                    firing.get("level"),
                    firing.get("plan_id"),
                    float(firing.get("pace", 0)),
                    float(firing.get("threshold", 0)),
                    firing.get("firing_id"),
                )
        else:
            logger.debug("scheduler: mediaplan_alert_check_complete: all clear")
    except Exception as exc:
        logger.warning("scheduler: mediaplan_alert_check_error: %s", exc)


def _run_anomaly_alert_check() -> None:
    """Run the anomaly surveillance evaluator after nightly dispatch (Story 5.4, AC7).

    Reads ANOMALY_ALERTS_ENABLED env var (default "false"). Only runs when true.
    Catches all exceptions to ensure the scheduler thread never crashes.
    Runs synchronously in the scheduler thread -- NOT a new thread.
    Called AFTER _run_business_alert_check() (dbt run has already completed).

    SCHEDULER_ENABLED=false in CI means this function is never called (the scheduler
    thread never starts), satisfying HG-5.
    """
    if os.environ.get("ANOMALY_ALERTS_ENABLED", "false").lower() != "true":
        logger.debug(
            "scheduler: anomaly_alerts_check_skipped -- ANOMALY_ALERTS_ENABLED not true"
        )
        return

    try:
        from core import anomaly_alerts  # noqa: PLC0415

        firings = anomaly_alerts.evaluate_anomalies()
        if firings:
            logger.info(
                "scheduler: anomaly_alert_check_complete: firings=%d", len(firings)
            )
            for firing in firings:
                logger.warning(
                    "scheduler: anomaly_fired: metric=%s zscore=%.4f"
                    " severity=%s firing_id=%s",
                    firing.get("metric"),
                    float(firing.get("zscore", 0)),
                    firing.get("severity"),
                    firing.get("firing_id"),
                )
        else:
            logger.debug("scheduler: anomaly_alert_check_complete: all clear")
    except Exception as exc:
        logger.warning("scheduler: anomaly_alert_check_error: %s", exc)


# ---------------------------------------------------------------------------
# AC2 -- start_nightly_scheduler (called from build_asgi_app)
# ---------------------------------------------------------------------------


def start_nightly_scheduler() -> None:
    """Start the nightly-scheduler daemon thread (call once at server startup).

    Reads SCHEDULER_ENABLED env var (default "false"). Set to "true" to activate.
    HG-5: MUST NOT start by default -- CI and unit tests run with the scheduler off.

    Includes a double-start guard (same pattern as health_poller.py and queue.py).
    """
    if os.environ.get("SCHEDULER_ENABLED", "false").lower() != "true":
        logger.debug("scheduler: disabled -- SCHEDULER_ENABLED not true")
        return

    # Double-start guard
    if any(t.name == "nightly-scheduler" for t in threading.enumerate()):
        logger.info("scheduler: already running -- not starting a second thread")
        return

    t = threading.Thread(target=_scheduler_loop, daemon=True, name="nightly-scheduler")
    t.start()
    logger.info(
        "scheduler: nightly_scheduler_started: hour=%s minute=%s",
        os.environ.get("SCHEDULER_NIGHTLY_HOUR", "2"),
        os.environ.get("SCHEDULER_NIGHTLY_MINUTE", "0"),
    )
