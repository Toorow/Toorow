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
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# The pull window arbitration (AI-46 / AI-145 / AI-217) that this module used to
# own inline, twice. Moved rather than re-decided; see `core.pull_window`.
from core import datastream_dispatch, pull_window

# Story 63.6: the states of a pull job have ONE owner. This module used to type
# two of its sets as literals, and the difference between them decides whether a
# stopped run restarts by itself within the hour.
from core import pull_job_states as _pull_job_states

# Story 63.7: the two dispatch loops name their origin from the ONE registry.
# They wrote it as a bare literal, which is how three vocabularies for "why is
# this running" ended up on one table.
from core.run_origins import SCHEDULER_HOURLY, SCHEDULER_NIGHTLY

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
                    -- AI-306: NULL is PLATFORM SCOPE (migration 291). This wrote
                    -- the literal 'default', a Project id from before the
                    -- multi-project layer; production has no such row, so the
                    -- foreign key refused EVERY meta-alert and the `except`
                    -- below turned each one into a log line. Measured on Cloud
                    -- Run 2026-08-18T00:32:25Z:
                    -- `violates foreign key constraint "fk_alert_firings_project"`.
                    VALUES (%s, NULL, 'meta_alert', NULL, 'scheduler_health', %s,
                            0, 0, '{}', %s, 'error', %s)
                    """,
                    (firing_id, datetime.now(tz=timezone.utc), window_date, message),
                )
            conn.commit()
        logger.warning("scheduler: meta_alert_inserted step=%s reason=%s", step, reason)
    except Exception as exc:  # noqa: BLE001
        # The exception CLASS, not only its text. `meta_alert_insert_failed` was
        # read off the first real nightly and the FK name in its message is what
        # closed AI-306; the class makes the next one readable the same way,
        # without a second production round trip.
        logger.warning(
            "scheduler: meta_alert_insert_failed step=%s: %s: %s",
            step,
            type(exc).__name__,
            exc,
        )


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
            # AI-306: the nightly scheduler is one process serving every Project.
            # Its health belongs to no Project -- NULL, not a sentinel that names
            # a row production does not have.
            project_id=None,
            metric="scheduler_health",
            severity="error",
            message=f"Nightly scheduler degraded: steps={','.join(degraded_steps)}",
            metadata={"degraded_steps": degraded_steps},
        )
    except Exception as exc:  # noqa: BLE001
        # AT WARNING, not debug. This is the alert that says the nightly run
        # degraded; if writing it fails, the failure of the failure-report is
        # the last thing left to say, and a debug line says it to nobody. Its
        # sibling `_insert_meta_alert` has always logged at WARNING -- which is
        # the only reason the 2026-08-18 nightly could be read at all.
        logger.warning(
            "scheduler: scheduler_step_degraded_alert_failed steps=%s: %s: %s",
            ",".join(degraded_steps),
            type(exc).__name__,
            exc,
        )


# ---------------------------------------------------------------------------
# THE PER-STEP RUN LEDGER -- `execution-substrate.md` "Incomplete if" 2, the
# THIRD locus: "a scheduled run that does not happen leaves no record that it
# did not happen".
#
# The two other loci were already closed. A Datastream occurrence that was
# stepped over is CHARGED (`_advance_next_run`); a platform clock that did not
# fire is OBSERVED (`app.platform_clocks.observed_last_attempt_status`). The
# one INSIDE the run was not: `_run_isolated_step` deliberately never re-raises,
# so a step that silently never runs -- an exception before its call site, an
# early return, a name added to the sequence whose call site is dead, a
# container killed mid-night -- left nothing behind at all. A log stream is
# evidence of what DID happen; it cannot be queried for what did not.
#
# `app.nightly_step_runs` (migration 325) is that record, and the shape is what
# makes it one: the WHOLE declared sequence is written at dispatch, before any
# step runs. A step that never began is then an OPEN ROW rather than an absence
# -- something a person can see, count and sort without having to already know
# what the list was supposed to contain.
# ---------------------------------------------------------------------------

#: The nightly sequence, in order. THIS IS THE LIST WRITTEN AT DISPATCH, and it
#: must be the same list `run_nightly_steps` actually calls -- a name here with
#: no call site would leave an open row every night and read as a step that
#: never runs, while a call site missing from here would run unrecorded. Neither
#: is allowed to happen quietly:
#: `tests/core/test_nightly_step_ledger.py::test_the_declared_sequence_is_the_one_that_is_called`
#: reads the call sites out of `run_nightly_steps`'s own source and compares.
#:
#: The sequence is code, not a table: a catalogue shipped with the product is
#: not read from the database.
NIGHTLY_STEPS: tuple[str, ...] = (
    "recompile_semantic_artifacts",
    "dispatch_nightly",
    "dbt_per_project",
    "rebuild_cache",
    "schema_context_gen",
    "alert_check",
    "business_alert_check",
    "anomaly_alert_check",
    "mediaplan_alert_check",
    "dq_monitors",
    "run_due_notebooks",
    "run_due_briefings",
)

_DECLARE_STEPS_SQL = """
    INSERT INTO app.nightly_step_runs (run_id, as_of_date, step_name, step_ordinal)
    SELECT %s, %s, name, ordinality - 1
      FROM unnest(%s::text[]) WITH ORDINALITY AS declared(name, ordinality)
    ON CONFLICT (run_id, step_name) DO NOTHING
"""

_START_STEP_SQL = """
    UPDATE app.nightly_step_runs
       SET started_at = NOW()
     WHERE run_id = %s AND step_name = %s AND started_at IS NULL
"""

_CLOSE_STEP_SQL = """
    UPDATE app.nightly_step_runs
       SET ended_at = NOW(), outcome = %s, error_class = %s
     WHERE run_id = %s AND step_name = %s AND ended_at IS NULL
"""

#: The shape `ck_nightly_step_runs_error_class_is_a_class` accepts. Checked here
#: rather than discovered as a constraint violation, because a violated write is
#: a write that does not happen, and this table exists to be written.
_ERROR_CLASS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,120}$")


def _error_class(error: BaseException) -> str:
    """The exception CLASS name, never its message.

    A step's message can carry a connector response, a row of customer data or a
    fragment of a credential, and this table is platform-scoped with no org --
    exactly where none of that may land. The message keeps going to the log and
    to `app.alert_firings`, which is where it already was.
    """
    name = type(error).__name__
    return name if _ERROR_CLASS_RE.match(name) else "Exception"


class _NightlyStepLedger:
    """One night's per-step ledger. Every write is best-effort and never raises.

    NEVER RAISES, on purpose and with the cost stated: a ledger that could take
    the night down would be worse than the silence it replaces. A write that
    fails is logged at WARNING with the exception CLASS -- the same posture, for
    the same measured reason, as `_insert_meta_alert`, whose class-in-the-log is
    the only thing that made AI-306 readable without a second production run.

    A failed write is also not invisible in the table: `declare` writes the whole
    sequence, so a `started`/`closed` that never lands leaves the row open, which
    is exactly the state that says "this step did not report finishing".
    """

    def __init__(self, run_id: str, as_of_date: date) -> None:
        self.run_id = run_id
        self.as_of_date = as_of_date

    def _write(self, sql: str, params: tuple, *, what: str) -> bool:
        try:
            from core.db import get_connection  # noqa: PLC0415
            from core.platform_clocks import arm_platform_clock_access  # noqa: PLC0415

            with get_connection() as conn:
                # No org is in scope here, so the Epic-36 membership gate cannot
                # decide access: migration 325 carries the same platform-operator
                # policy as `app.platform_clocks`, and a session that forgets to
                # arm it writes nothing.
                arm_platform_clock_access(conn)
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                conn.commit()
            return True
        except Exception as exc:  # noqa: BLE001 -- see the class docstring.
            logger.warning(
                "scheduler: step_ledger_write_failed what=%s run_id=%s: %s: %s",
                what,
                self.run_id,
                type(exc).__name__,
                exc,
            )
            return False

    def declare(self, step_names: tuple[str, ...] = NIGHTLY_STEPS) -> bool:
        """Write the whole sequence as OPEN rows, before the first step runs."""
        return self._write(
            _DECLARE_STEPS_SQL,
            (self.run_id, self.as_of_date, list(step_names)),
            what="declare",
        )

    def started(self, step_name: str) -> bool:
        """Stamp the moment the step began -- BEFORE it is executed.

        Its own committed transaction, so a crash inside the step cannot roll it
        back. What survives is a row with `started_at` set and `ended_at` NULL,
        and that open row IS the record that the step did not finish.
        """
        return self._write(_START_STEP_SQL, (self.run_id, step_name), what="start")

    def closed(self, step_name: str, error: BaseException | None) -> bool:
        """Close the row with the verdict the boundary can actually observe."""
        outcome = "failed" if error is not None else "succeeded"
        return self._write(
            _CLOSE_STEP_SQL,
            (
                outcome,
                _error_class(error) if error is not None else None,
                self.run_id,
                step_name,
            ),
            what="close",
        )


def _run_isolated_step(
    step_name: str,
    fn,
    *args,
    _degraded: list[str] | None = None,
    _ledger: "_NightlyStepLedger | None" = None,
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

    THE RUN LEDGER (`_ledger`, migration 325 -- `Incomplete if` 2, third locus):
        when a ledger is supplied, the step's row is stamped `started_at` in its
        OWN COMMITTED TRANSACTION immediately before `fn` is called, and closed
        with `succeeded`/`failed` after it returns. The order is the point:

          * a step that RAISES still leaves a closed `failed` row naming the
            exception class, because the `except` below is reached;
          * a step that kills the process -- an OOM, a SIGKILL, a `BaseException`
            this `except` deliberately does not catch -- leaves the row OPEN,
            and there is no `finally` here that would close it on the way out.
            The open row is not a leak; it IS the record that the step did not
            finish, and closing it politely would erase exactly that.

        A step whose ROW WAS NEVER STARTED at all is the third state, and it is
        already on the table: `run_nightly_steps` declares the whole sequence
        before the first step runs.

    NEVER re-raises: the caller's loop continues to the next step.

    Returns the step's return value on success, else None.
    """
    timeout = _alert_timeout_seconds()

    if _ledger is not None:
        _ledger.started(step_name)

    t0 = time.monotonic()
    value = None
    error: Exception | None = None
    try:
        value = fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        error = exc
    elapsed_ms = round((time.monotonic() - t0) * 1000)

    if _ledger is not None:
        _ledger.closed(step_name, error)

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
        logger.warning("scheduler: nightly_step_failed step=%s error=%s", step_name, error)
        _insert_meta_alert(step_name, str(error))
        if _degraded is not None and step_name not in _degraded:
            _degraded.append(step_name)
        return None

    return value


def _run_due_notebooks() -> None:
    """Le pas nocturne des Notebooks. UN seul modele depuis le 2026-08-22.

    LA BASCULE AC12, faite ici parce que c'est ici qu'elle etait attendue. Le
    docstring de `_dispatch_due_canonical_notebooks`, juste en dessous, la nommait
    en toutes lettres : << the legacy path writes `app.notebooks` /
    `app.notebook_runs` and stays until the AC12 cutover [...] Two loops, one of
    which is dated, is honest. One loop pretending they are the same model would
    not be. >> Il n'y a plus qu'une boucle, et ce n'est pas un faux-semblant :
    l'autre modele n'a plus d'ecrivain.

    CE QUI ETAIT ICI, et pourquoi il part. Cette fonction lisait
    `app.notebooks WHERE scheduled = TRUE`, un magasin que la porte MCP etait le
    SEUL a remplir -- il n'existe aucune route REST de creation -- et que les
    ecrans canoniques ne lisent pas. `save_notebook` et `run_notebook` sont
    passes au magasin gouverne dans le meme commit, donc cette boucle tirait
    desormais sur une table que plus rien n'alimente : mesure de production du
    2026-08-22, `app.notebooks` **0 ligne**.

    Elle emportait aussi un SECOND moteur d'execution -- `run_notebook_direct`
    resolvait une `window_rule` plate, rendait un report et inserait son propre
    run -- pendant que le service gouverne epingle la version AVANT qu'un bloc
    s'execute et rend un Run idempotent. Deux moteurs, dont un seul porte ces
    deux garanties, est exactement ce que << one execution path >> refuse.

    Le nom de la fonction ne change pas : `NOTEBOOK_DISPATCHER`
    (`analyze_artifacts.py`) le publie au panneau Notebook comme le site d'appel
    verifiable, et `test_the_scheduler_dispatches_through_this_service_and_no_other`
    lit la source pour l'exiger. Renommer le site aurait fait mentir le panneau.
    """
    _dispatch_due_canonical_notebooks()


def _dispatch_due_canonical_notebooks() -> None:
    """Dispatch the canonical Story 50.3 Notebooks whose schedule is due.

    THE ONE CALL SITE. `core.analyze_artifacts.run_notebook` is the same service a
    person's manual run goes through, so a scheduled Run pins the exact composition
    version, creates a real Result per block and records the same immutable
    evidence -- AC7's "dispatch creates the same immutable block Result/Render
    evidence as a manual run", which cannot be true while two code paths exist.

    Everything else lives in the service: due selection, the per-period idempotency
    key, per-Notebook SAVEPOINT isolation and advancing `next_due_at`. This
    function exists to be called by the nightly step, and to fail quietly enough
    that it cannot take the briefing step down with it.

    LA BASCULE AC12 EST FAITE (2026-08-22, story 67.23). Ce docstring disait
    << the legacy path [...] stays until the AC12 cutover >> ; la boucle heritee
    au-dessus n'existe plus, parce que la porte MCP -- son SEUL ecrivain, il n'y
    a aucune route REST de creation -- ecrit desormais le magasin gouverne.
    Il n'y a plus deux boucles, et ce n'est pas un faux-semblant : il n'y a plus
    qu'un modele.
    """
    try:
        from core.analyze_artifacts import dispatch_due_notebook_schedules  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        logger.warning("scheduler: dispatch_canonical_notebooks: import_error: %s", exc)
        return

    try:
        with get_connection() as conn:
            report = dispatch_due_notebook_schedules(conn)
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("scheduler: dispatch_canonical_notebooks: failed: %s", exc)
        _insert_meta_alert("dispatch_canonical_notebooks", f"dispatch failed: {exc}")
        return

    for failure in report["failed"]:
        _insert_meta_alert(
            "dispatch_canonical_notebooks",
            f"notebook {failure['notebook_id']} failed: {failure['reason']}",
        )
    logger.info(
        "scheduler: canonical_notebooks_dispatched: considered=%d dispatched=%d failed=%d",
        report["considered"],
        len(report["dispatched"]),
        len(report["failed"]),
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

    BRIEFING_ENABLED guard (default "true"): the step selects the projects that
    have an enabled connection, so a platform with none builds nothing. The flag
    is an off-switch, never a thing to remember at install.
    """
    if os.environ.get("BRIEFING_ENABLED", "true").lower() != "true":
        logger.debug("scheduler: briefings_skipped -- BRIEFING_ENABLED set false")
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
            _mediaplan_firings = _ma.fetch_recent_mediaplan_firings(project_id, conn, hours=24)
        alert_firings = _biz_firings + _anomaly_firings + _mediaplan_firings
    except Exception as exc:
        logger.debug("scheduler: briefing_alert_fetch_failed: project_id=%s: %s", project_id, exc)
        # Continue with empty alert_firings (briefing still built from rollup)

    # Fetch rollup via warehouse query for the project's daily data
    rollup: dict = {}
    try:
        from core import warehouse as _wh  # noqa: PLC0415
        from core.metric_reconciliation import route_status_resolver  # noqa: PLC0415
        from core.rollup import compute_rollup  # noqa: PLC0415

        yesterday = (date.today() - timedelta(days=1)).isoformat()
        two_days_ago = (date.today() - timedelta(days=2)).isoformat()
        rows = _wh.query_daily_report(project_id, two_days_ago, yesterday, None)
        if rows:
            metrics = sorted({r.get("metric") for r in rows if r.get("metric")})
            pull_ids = sorted({r.get("pull_id") for r in rows if r.get("pull_id")})
            # CAV-02 (story 53.2): the briefing is the sixth producer of cross-source
            # totals and the only one the epic journal never named. It is also the one
            # nobody asked for -- it arrives -- so an unchecked total here is read
            # without the reader having chosen to look at it.
            rollup = compute_rollup(
                rows, metrics, two_days_ago, yesterday, project_id, pull_ids,
                route_resolver=route_status_resolver(project_id),
            )
    except Exception as exc:
        logger.debug("scheduler: briefing_rollup_failed: project_id=%s: %s", project_id, exc)
        # Continue with empty rollup (briefing still built from alerts)

    # Fetch context_events (last 7 days). AI-344 (2026-09-01): the read serves
    # from the mirror or the record; this is a background path with no caller,
    # so it hands in the connection it already holds rather than opening a scoped
    # one. When neither store can serve, the briefing is still built and the
    # absence TRAVELS WITH IT: the `{reason, repair}` goes into the insights the
    # row carries, not only into a log line -- a briefing built on an unread
    # window must not go out looking like one built on a quiet week, and a log is
    # not a reader (ledger context-hub[75], [78]).
    context_events: list[dict] = []
    context_events_unavailable: dict | None = None
    try:
        from core.context_events import (  # noqa: PLC0415
            ContextEventsUnavailable,
            fetch_context_events,
        )

        seven_days_ago = (date.today() - timedelta(days=7)).isoformat()
        today_str = date.today().isoformat()
        try:
            with get_connection() as _events_conn:
                context_events = fetch_context_events(
                    project_id, seven_days_ago, today_str, conn=_events_conn
                )
        except ContextEventsUnavailable as unavailable:
            context_events_unavailable = unavailable.payload
            logger.warning(
                "scheduler: briefing_context_events_unavailable: project_id=%s: %s "
                "repair: %s",
                project_id, unavailable.reason, unavailable.repair,
            )
    except Exception as exc:
        logger.debug(
            "scheduler: briefing_context_events_failed: project_id=%s: %s", project_id, exc
        )

    # Story 53.8 (CAV-13): the detector's readiness for the evaluated day.
    detector_readiness: list[dict] | None = None
    try:
        from core import anomaly_alerts as _aa_readiness  # noqa: PLC0415

        detector_readiness = _aa_readiness.fetch_detector_readiness(
            project_id=project_id,
            evaluation_date=date.today() - timedelta(days=1),
        )
    except Exception as exc:
        logger.debug(
            "scheduler: briefing_readiness_failed: project_id=%s: %s", project_id, exc
        )

    # Build briefing (pure function -- no DB/warehouse)
    insights_json = build_briefing(
        project_id=project_id,
        briefing_date=briefing_date,
        alert_firings=alert_firings,
        rollup=rollup,
        context_events=context_events,
        nightly_run_id=nightly_run_id,
        detector_readiness=detector_readiness,
        context_events_unavailable=context_events_unavailable,
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
def _run_recompile_semantic_artifacts() -> None:
    """Nightly step (AI-346): re-derive every stale compiled Semantic View artifact.

    A version the sweep could not recompile is NOT this step's failure -- it is
    recorded per version and named by the refusal a person reads. A sweep that
    could not RUN is: the step raises so the ledger closes it `failed`.
    """
    from core.semantic_artifact_sweep import run_sweep  # noqa: PLC0415

    report = run_sweep("nightly")
    if report is None:
        raise RuntimeError("semantic artifact sweep did not run; see the warning above")


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
                cur.execute("SELECT pg_try_advisory_lock(%s)", (_NIGHTLY_ADVISORY_LOCK_KEY,))
                row = cur.fetchone()
            # No commit needed -- advisory locks survive the transaction.
        return bool(row[0]) if row is not None else None
    except Exception as exc:
        logger.warning("scheduler: advisory_lock_unavailable: %s -- proceeding without lock", exc)
        return None


def _release_advisory_lock() -> None:
    """Release pg_advisory_unlock(_NIGHTLY_ADVISORY_LOCK_KEY). Never raises."""
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (_NIGHTLY_ADVISORY_LOCK_KEY,))
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
        #
        # NO STEP-LEDGER ROW IS WRITTEN HERE, and that is not the silence clause
        # 2 forbids: the instance holding the lock is writing the night's rows.
        # A second dispatch declaring the same sequence under its own run id
        # would put a whole ghost night in the ledger -- eleven steps that never
        # began because they were never meant to -- and an operator reading the
        # panel would see a failure that is really a correctly refused duplicate.
        logger.info("scheduler: nightly_skipped: another instance holds the advisory lock")
        return

    # lock_acquired is True (we hold it) or None (Postgres unavailable -- proceed).
    try:
        # Mint a nightly_run_id for this run (audit + briefing provenance).
        from ulid import ULID  # noqa: PLC0415

        nightly_run_id = f"nrun_{ULID()}"
        logger.info("scheduler: nightly_run_started: run_id=%s date=%s", nightly_run_id, as_of_date)

        # AI-32 (c): track degraded steps (failed or soft-timeout) for the post-run
        # consolidated meta-alert.  _degraded is passed to every _run_isolated_step call.
        _degraded: list[str] = []

        # `Incomplete if` 2, THIRD LOCUS (migration 325). The whole declared
        # sequence is written NOW, before the first step runs, so that a step
        # which never begins is an OPEN ROW rather than an absence nobody can
        # query. Best-effort: a ledger that could take the night down would be
        # worse than the silence it replaces.
        _ledger = _NightlyStepLedger(nightly_run_id, as_of_date)
        _ledger.declare()

        # AI-346: FIRST, before anything asks a question of a Semantic View
        # tonight. A compiled artifact is derived; when the compiler moved, the
        # derivation is repeated here (and at process start) rather than left
        # to a person who would be told to publish a version nothing required.
        _run_isolated_step(
            "recompile_semantic_artifacts",
            _run_recompile_semantic_artifacts,
            _degraded=_degraded,
            _ledger=_ledger,
        )
        _run_isolated_step(
            "dispatch_nightly",
            dispatch_nightly,
            _degraded=_degraded,
            _ledger=_ledger,
            as_of_date=as_of_date,
        )
        # Story 24.4 (AC3), retargeted per PROJECT by AI-166: materialise the marts
        # AFTER dispatch (fresh raw) and AFTER the central mirror sync (AD-8),
        # BEFORE rebuild_cache so the cache sees fresh marts. Isolated (AI-32) +
        # DBT_NIGHTLY_ENABLED guarded (default off) -- a failed build never blocks
        # the remaining steps.
        _run_isolated_step(
            "dbt_per_project", _run_dbt_per_project, _degraded=_degraded, _ledger=_ledger
        )
        # Story 19.1 (AD-22): rebuild the read-through cache right after dispatch/dbt,
        # so downstream reads hit a fresh snapshot. Isolated (AI-32) + TOOROW_CACHE_ENABLED
        # guarded (default off) -- a failed rebuild never blocks the remaining steps.
        _run_isolated_step(
            "rebuild_cache", _run_rebuild_cache, _degraded=_degraded, _ledger=_ledger
        )
        # Story 11.2: regenerate schema-context docs from the freshly consolidated
        # marts, AFTER dispatch/dbt/rebuild_cache and BEFORE alert checks. Isolated
        # (AI-32) + SCHEMA_CONTEXT_ENABLED guarded (default off) -- a failed run
        # never blocks the alert/notebook/briefing steps.
        _run_isolated_step(
            "schema_context_gen", _run_schema_context_gen, _degraded=_degraded, _ledger=_ledger
        )
        _run_isolated_step(
            "alert_check", _run_alert_check, _degraded=_degraded, _ledger=_ledger
        )
        _run_isolated_step(
            "business_alert_check",
            _run_business_alert_check,
            _degraded=_degraded,
            _ledger=_ledger,
        )
        _run_isolated_step(
            "anomaly_alert_check",
            _run_anomaly_alert_check,
            _degraded=_degraded,
            _ledger=_ledger,
        )
        # Story 22.6 (FR9): mediaplan pacing alert check after anomaly checks.
        _run_isolated_step(
            "mediaplan_alert_check",
            _run_mediaplan_alert_check,
            _degraded=_degraded,
            _ledger=_ledger,
        )
        # Story 8.6 (AC1): DQ monitors run after anomaly checks, before notebooks.
        _run_isolated_step(
            "dq_monitors", _run_dq_monitors_check, _degraded=_degraded, _ledger=_ledger
        )
        # Story 6.6 (AC1): scheduled notebooks run after alerts.
        _run_isolated_step(
            "run_due_notebooks", _run_due_notebooks, _degraded=_degraded, _ledger=_ledger
        )
        # Story 6.7 (AC4): morning briefing is ALWAYS the LAST step.
        # Runs after notebooks so fresh alert_firings and notebook outputs are available.
        _run_isolated_step(
            "run_due_briefings",
            _run_due_briefings,
            nightly_run_id,
            _degraded=_degraded,
            _ledger=_ledger,
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


def project_timezone(conn, project_id: str, _cache: dict | None = None) -> str:
    """The IANA timezone a project's DAY is measured in. AI-117.

    `SCHEDULER_TIMEZONE` (default Europe/Paris) was a DEPLOYMENT constant standing
    in for this, and its own comment said so: "yesterday is the PROJECT's day, not
    the server's -- per-project tz arrives with Epic 4". It never arrived, while
    `app.project_preferences.reporting_timezone` was already carrying the answer.

    A project in New York therefore had its "yesterday" decided in Paris: between
    18:00 and 00:00 New York time the two calendars disagree, so the nightly
    window asked a provider for a day the project had not finished living.

    Falls back to the deployment constant, then UTC -- a project without a
    preference keeps exactly the behaviour it had.
    """
    if _cache is not None and project_id in _cache:
        return _cache[project_id]
    resolved = os.environ.get("SCHEDULER_TIMEZONE", "Europe/Paris")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT reporting_timezone FROM app.project_preferences WHERE project_id = %s",
                (project_id,),
            )
            row = cur.fetchone()
        if row and row[0]:
            resolved = str(row[0])
    except Exception as exc:  # noqa: BLE001 -- a missing preference is not a failure
        logger.debug("scheduler: project_timezone_fallback project=%s: %s", project_id, exc)
    if _cache is not None:
        _cache[project_id] = resolved
    return resolved


def project_yesterday(timezone_name: str) -> date:
    """The last COMPLETE day in *timezone_name*.

    Deliberately computed from the clock rather than from a shared `as_of_date`:
    two projects in different timezones do not have the same yesterday, and a
    single value for all of them is the defect this replaces.
    """
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        return (datetime.now(tz=ZoneInfo(timezone_name)).date()) - timedelta(days=1)
    except Exception as exc:  # noqa: BLE001 -- an unknown tz must not stop a dispatch
        logger.warning(
            "scheduler: unknown timezone %r (%s) -- falling back to the server day",
            timezone_name,
            exc,
        )
        return date.today() - timedelta(days=1)


#: How far the advance moves, per cadence. `weekly` became a legal
#: `schedule_mode` with migration 204 and was landing here as one day, which is
#: what "not in this mapping" used to mean.
_ADVANCE_STEPS = {"hourly": "1 hour", "weekly": "7 days"}


def _dispatch_tick_key() -> str:
    """Story 63.1: an identifier for THIS dispatch tick, to the second.

    Not the run's identity -- the run's identity is its `dse_` id. This only
    separates one tick's idempotency key from the next one's, so two ticks never
    share a run and never push a closed run's window count past the total it
    declared.
    """
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def _open_collection_run(
    get_connection,
    *,
    ds: dict,
    windows: list[dict],
    actor: str,
    origin: str,
    run_key: str,
) -> str | None:
    """Story 63.1: mint the execution this dispatch's windows belong to.

    Returns its id, or None when no run could be opened -- a run is already in
    flight for this datastream, or the row has no plan/mapping version. None is
    never a reason to skip the pull: the windows are enqueued either way and the
    data lands either way; only the progress line is missing, which is the honest
    state and not a fabricated one.

    The idempotency key is (origin, datastream, first window, last window, tick):
    ONE run per dispatch tick. It deliberately does NOT collapse two ticks into
    one run -- a run's set of windows is fixed when it opens, and letting a later
    tick bind more windows to a closed run would push `days_done` past the
    `days_total` that run declared, which `ck_datastream_executions_progress_bounds`
    refuses. Two ticks that genuinely overlap are handled by the concurrency
    index instead: the second one opens nothing and says so.
    """
    if not windows:
        return None
    first = str(windows[0].get("date_from"))
    last = str(windows[-1].get("date_to"))
    try:
        from core.execution_progress import open_collection_run  # noqa: PLC0415

        with get_connection() as conn:
            execution = open_collection_run(
                conn,
                datastream_id=str(ds["ds_id"]),
                project_id=str(ds["project_id"]),
                plan_version_id=str(ds.get("current_plan_version_id") or ""),
                mapping_version_id=str(ds.get("current_mapping_version_id") or ""),
                windows=windows,
                actor=actor,
                idempotency_key=f"{origin}:{ds['ds_id']}:{first}:{last}:{run_key}",
                origin=origin,
            )
            if execution is None:
                conn.rollback()
                return None
            conn.commit()
            return str(execution["id"])
    except Exception as exc:  # noqa: BLE001 -- instrumentation never blocks a pull
        logger.warning(
            "scheduler: open_collection_run_failed ds=%s: %s", ds.get("ds_id"), exc
        )
        return None


def _close_collection_run(get_connection, execution_id: str | None, actor: str) -> None:
    """Story 63.1: close a run whose windows are all terminal (or absent)."""
    if not execution_id:
        return
    try:
        from core.execution_progress import (  # noqa: PLC0415
            close_collection_run_if_complete,
        )

        with get_connection() as conn:
            close_collection_run_if_complete(
                conn, execution_id=execution_id, actor=actor
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 -- instrumentation never blocks a pull
        logger.warning(
            "scheduler: close_collection_run_failed execution=%s: %s", execution_id, exc
        )


def _advance_next_run(
    get_connection,
    datastream_id: str,
    project_id: str,
    cadence: str,
    *,
    now_override: str | None = None,
) -> None:
    """Move `next_run_at` past this dispatch so a frequent tick does not re-fire.

    AI-119. `next_run_at` is what makes the moment EDITABLE: an operator, the
    admin surface or an MCP tool writes an instant into Postgres and the next
    tick honours it. That only works if the dispatcher also ADVANCES it --
    otherwise a tick every ten minutes would re-enqueue the same datastream six
    times an hour, and the dedup index would hide the damage rather than prevent
    the intent.

    RECALCULATES, it does not accumulate (story 57.8, arbitrage A1). It used to
    add one period to the PREVIOUS value, which anchored a schedule to the
    instant a person chose -- right up to the first catch-up. The moment a
    failure writes `next_run_at = NOW() + 1 hour`, that off-hour instant became
    the anchor for every following day, and the chosen hour was lost by the
    mechanism meant to preserve it. The anchor is now
    `app.datastreams.arrival_hour_local`: a VALUE the advance reads, so a
    catch-up hands the schedule back to tomorrow's arrival hour rather than
    walking it forward an hour a day.

    The recomputation is done in the LOCAL calendar and converted afterwards,
    which is what keeps 02:00 at 02:00 across a daylight-saving change: adding
    24 hours on a UTC session moves it to 01:00 or 03:00, where it stays.

    A row that names no arrival hour keeps the previous behaviour exactly: the
    fallback is the local time of day the row already carries, so the candidate
    equals the previous value and the advance lands one period later.

    IT ALWAYS LANDS IN THE FUTURE, in ONE advance. The reference is the LATER of
    the previous value and now, so a row overdue by ten days jumps to its next
    due occurrence instead of walking forward one period per tick. Walking was
    not a catch-up: the intermediate windows are not fetched, they are dropped —
    every one of those dispatches computed the SAME window (this project's
    yesterday). And it cannot be waved away with the queue's dedup index, which
    is PARTIAL (`022_pull_jobs_dedup_index.sql`: `WHERE state IN ('queued',
    'running')`): once a window has finished, the identical window is enqueued
    again. With the frequent tick reading nightly rows, that cost went from one
    duplicate a day to one an hour.

    AND IT COUNTS WHAT IT STEPPED OVER. The jump above is not free: the
    occurrences between the old `next_run_at` and now are due occurrences that
    nothing collected, and this UPDATE is the last place in the system that can
    still see them -- afterwards the clock reads as if it had always been on
    time. `missed_run_count` therefore rises by
    `floor((now - next_run_at) / step)` in the same statement: zero on a healthy
    tick (a nightly row a few minutes late floors to 0), three on a container
    that was down for three days. A NULL `next_run_at` adds nothing, because a
    row that was never armed has missed nothing.

    This is `Incomplete if` clause 2 of `execution-substrate.md` — "a scheduled
    run that does not happen leaves no record that it did not happen" — and it
    is the honest direction of that rule. The document forbids inferring that a
    run HAPPENED from silence; recording that one did NOT is the opposite act,
    and it is arithmetic on the clock's own ledger rather than a guess.

    IT NEVER TOUCHES `retry_count`. The single extra attempt (A4) is closed by a
    SUCCESSFUL pull and by nothing else — see `_reschedule_failed_pulls`. An
    earlier version cleared the counter here whenever the advanced value sat on
    the arrival hour, which is trivially true for every row that names no arrival
    hour: the catch-up's own dispatch re-armed the allowance, and one extra
    attempt became an hourly loop for the whole majority class of rows.

    Never raises: a pull that was enqueued must not be undone by a bookkeeping
    failure. A missed advance re-fires at the next tick.

    `now_override` exists so a test can cross a daylight-saving boundary
    deterministically; production never passes it and the database clock decides.
    """
    step = _ADVANCE_STEPS.get(cadence, "1 day")
    # A3: an arrival hour only means something for the cadences that run once a
    # period. An hourly row keeps advancing from where it was.
    arrival_applies = cadence in ("nightly", "weekly")
    try:
        with get_connection() as conn:
            tz_name = project_timezone(conn, project_id)
            with conn.cursor() as cur:
                # Scoped to the datastream's CURRENT plan version, because that
                # is what the dispatch query joins on. The table is keyed by
                # plan_version_id, so a datastream can carry several rows across
                # its versions -- advancing them all would move schedules that
                # belong to versions nothing dispatches.
                cur.execute(
                    f"""
                    WITH clock AS (
                        SELECT COALESCE(%s::timestamptz, NOW()) AS now_utc
                    ),
                    anchored AS (
                        SELECT ss.plan_version_id,
                               -- The LATER of where the row sits and now: one
                               -- advance lands in the future however far behind
                               -- the row had fallen.
                               GREATEST(
                                   COALESCE(ss.next_run_at, clock.now_utc),
                                   clock.now_utc
                               ) AT TIME ZONE %s AS reference_local,
                               CASE WHEN %s THEN d.arrival_hour_local END AS arrival_hour,
                               COALESCE(ss.next_run_at, clock.now_utc) AT TIME ZONE %s
                                   AS previous_local,
                               -- The occurrences this ONE advance steps over.
                               -- The row was due at `next_run_at` and should have
                               -- fired once per `step` since; it fires once, here.
                               -- The difference is what nothing collected, and it
                               -- is the only moment the system can still see it.
                               GREATEST(
                                   FLOOR(
                                       EXTRACT(EPOCH FROM (
                                           clock.now_utc
                                           - COALESCE(ss.next_run_at, clock.now_utc)
                                       ))
                                       / EXTRACT(EPOCH FROM INTERVAL '{step}')
                                   )::int,
                                   0
                               ) AS skipped
                          FROM app.datastream_schedule_state ss
                          JOIN app.datastreams d
                            ON d.id = ss.datastream_id
                           AND d.project_id = ss.project_id
                           AND ss.plan_version_id = d.current_plan_version_id
                          CROSS JOIN clock
                         WHERE d.id = %s AND d.project_id = %s
                           AND (ss.next_run_at IS NULL OR ss.next_run_at <= clock.now_utc)
                    ),
                    computed AS (
                        SELECT plan_version_id, reference_local, skipped,
                               date_trunc('day', reference_local)
                                 + COALESCE(
                                     make_interval(hours => arrival_hour),
                                     -- No arrival hour named: the row keeps the
                                     -- local time of day it already carried.
                                     previous_local - date_trunc('day', previous_local)
                                   ) AS arrival_local
                          FROM anchored
                    )
                    UPDATE app.datastream_schedule_state ss
                    SET next_run_at = (
                            CASE WHEN c.arrival_local > c.reference_local
                                 THEN c.arrival_local
                                 ELSE c.arrival_local + interval '{step}'
                            END
                        ) AT TIME ZONE %s,
                        -- Incomplete if n.2: the advance is the ONLY place that
                        -- still holds both where the clock was and where it is.
                        -- Dropping the intermediate windows silently is what made
                        -- `missed_run_count = 0` read like health.
                        missed_run_count = ss.missed_run_count + c.skipped,
                        updated_at = NOW()
                    FROM computed c
                    WHERE ss.plan_version_id = c.plan_version_id
                    """,  # noqa: S608 -- `step` is a literal chosen above, never input
                    (
                        now_override, tz_name, arrival_applies, tz_name,
                        datastream_id, project_id, tz_name,
                    ),
                )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 -- see docstring
        logger.warning(
            "scheduler: next_run_advance_failed ds=%s: %s -- the tick will re-evaluate",
            datastream_id,
            exc,
        )


def _record_missed_run(
    get_connection,
    datastream_id: str,
    project_id: str,
    *,
    reason: str,
    count: int = 1,
) -> None:
    """A due occurrence that was REFUSED leaves its mark. `Incomplete if` n.2.

    The sibling of `_advance_next_run`, and deliberately its opposite number: one
    is called when a window became a job, this one when a window that was due did
    not. Between them every due occurrence now moves exactly one counter.

    WHY THIS EXISTS. AI-301, measured 2026-08-12 to 2026-08-17: every window of
    the whole platform was refused `access_denied` at the enqueue, `next_run_at`
    advanced every night anyway, `missed_run_count` stayed at 0 and nothing was
    logged above DEBUG. The refusal is now said and the clock no longer moves --
    but a refusal that stops the clock still left the counter reading like
    health, because a refused window was simply absent from every ledger.

    A REFUSAL IS AN OBSERVATION, not an inference. This is called only where the
    code has the row in hand and has just decided not to run it: the gate refusal
    in `_dispatch_nightly_datastreams` and the enqueue refusal in
    `datastream_dispatch.dispatch_windows`. It is never called from silence --
    silence is the advance's arithmetic, above.

    Scoped to the CURRENT plan version, exactly as the advance and the dispatch
    query are: the table is keyed by `plan_version_id`, and a datastream carries
    a row per version it has had.

    Never raises. A bookkeeping failure must not take down a dispatch loop that
    still has other datastreams to walk; the reason is logged at WARNING, which
    is the level AI-301 established for a refusal that costs a collection.
    """
    if count <= 0:
        return
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE app.datastream_schedule_state ss
                       SET missed_run_count = ss.missed_run_count + %s,
                           updated_at = NOW()
                      FROM app.datastreams d
                     WHERE d.id = ss.datastream_id
                       AND d.project_id = ss.project_id
                       AND ss.plan_version_id = d.current_plan_version_id
                       AND d.id = %s
                       AND d.project_id = %s
                    """,
                    (count, datastream_id, project_id),
                )
                written = cur.rowcount
            conn.commit()
        if written:
            logger.warning(
                "scheduler: missed_run_recorded ds=%s project=%s reason=%s count=%s",
                datastream_id,
                project_id,
                reason,
                count,
            )
    except Exception as exc:  # noqa: BLE001 -- see docstring
        logger.warning(
            "scheduler: missed_run_record_failed ds=%s reason=%s: %s",
            datastream_id,
            reason,
            exc,
        )


def _reschedule_failed_pulls(get_connection) -> int:
    """A pull that did not arrive catches up at the next hour, once. Story 57.8.

    Jean, 2026-08-05: *"on propose un retry l'heure d'apres si le pull n'a pas
    marche."*

    WHAT WAS WRONG. `_advance_next_run` runs immediately after a successful
    ENQUEUE, so the schedule moved to the next period before anyone knew whether
    the pull worked. A job that then failed became `FAILED`, or `DEAD_LETTER` at
    the attempt ceiling, and the queue's reconciliation sweep only re-dispatches
    `WHERE state = 'queued'`: nobody retried, and `next_run_at` already pointed
    at tomorrow. `app.datastream_schedule_state.retry_count` had existed since
    migration 030 with exactly one reader and no writer at all.

    WHAT THIS DOES. Two statements, deliberately not one:

      * a Datastream whose LAST terminal pull failed gets `next_run_at` an hour
        from now and its counter incremented -- capped at one attempt (A4),
        because "the next hour" is an hour, not a retry policy, and an hourly
        loop against a broken source spends provider quota all night;
      * a Datastream whose last terminal pull SUCCEEDED has its counter cleared.

    THE SUCCESS IS THE ONLY THING THAT RE-ARMS THE ALLOWANCE, and that is the
    whole bound. Nothing in the advance clears the counter: an earlier version
    did, whenever the advanced value sat on the arrival hour, and that condition
    is trivially true for every row whose `arrival_hour_local` is NULL -- the
    majority class, since migration 217 only backfills rows that were already
    anchored. The catch-up's own dispatch re-armed the allowance and the next
    sweep armed another, hourly, for ever. Bounding on the counter makes the
    limit structural: it does not depend on comparing two local hours.

    The consequence is deliberate: a source that fails every day gets ONE
    catch-up until it succeeds once, not one per day. `retry_count` stays at 1
    and is displayed, so the state is visible rather than silently re-tried.

    A5: `dead_letter` counts as a failure. A job that exhausted its attempts
    moved no rows either, and calling that terminal-and-therefore-fine hides the
    one outcome an operator most needs to see.

    A3: `nightly` and `weekly` only. An hourly Datastream's next hour is already
    its next run, and a manual one has no clock to move.

    Never raises: this is bookkeeping in front of a dispatch, and a failure here
    must not stop the dispatch behind it. Returns the number of rows moved.
    """
    moved = 0
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # `DISTINCT ON` keeps the LAST terminal job per Datastream. Pull
                # history is append-only: every Datastream has old failures, and
                # reading anything but the latest would arm a catch-up for one a
                # success has already superseded. The two-day horizon keeps the
                # scan bounded -- a failure older than that has been overtaken by
                # at least one scheduled run.
                # The two state sets are the REGISTRY's, generated into the
                # statement (story 63.6). They used to be typed here, and the
                # difference between them is the whole safety of this sweep: a
                # window a person STOPPED must be in neither, or the catch-up
                # would restart within the hour the run that was just stopped.
                latest = f"""
                    WITH latest AS (
                        SELECT DISTINCT ON (j.datastream_id)
                               j.datastream_id, j.state, j.completed_at
                          FROM app.pull_jobs j
                         WHERE j.datastream_id IS NOT NULL
                           AND j.state IN ({_pull_job_states.sql_literals(
                               _pull_job_states.ATTEMPTED_JOB_STATES)})
                           AND j.completed_at IS NOT NULL
                           AND j.completed_at > NOW() - interval '2 days'
                         ORDER BY j.datastream_id, j.completed_at DESC
                    )
                """
                cur.execute(
                    latest + f"""
                    UPDATE app.datastream_schedule_state ss
                    SET next_run_at = NOW() + interval '1 hour',
                        retry_count = ss.retry_count + 1,
                        updated_at = NOW()
                    FROM latest
                    JOIN app.datastreams d ON d.id = latest.datastream_id
                    WHERE ss.datastream_id = d.id
                      AND ss.project_id = d.project_id
                      AND ss.plan_version_id = d.current_plan_version_id
                      AND d.enabled = TRUE
                      AND d.lifecycle_state = 'active'
                      AND d.schedule_mode IN ('nightly', 'weekly')
                      AND latest.state IN ({_pull_job_states.sql_literals(
                          _pull_job_states.FAILED_JOB_STATES)})
                      -- A4, and it is the whole bound: one extra attempt until
                      -- a pull SUCCEEDS. Structural, so it holds identically for
                      -- a row that names no arrival hour.
                      AND ss.retry_count = 0
                      -- The sweep runs every tick and the failure it reads does
                      -- not change. Without this, the same terminal job would be
                      -- re-read hour after hour.
                      AND latest.completed_at > ss.updated_at
                    """
                )
                moved = cur.rowcount or 0
                cur.execute(
                    latest + """
                    UPDATE app.datastream_schedule_state ss
                    SET retry_count = 0,
                        updated_at = NOW()
                    FROM latest
                    JOIN app.datastreams d ON d.id = latest.datastream_id
                    WHERE ss.datastream_id = d.id
                      AND ss.project_id = d.project_id
                      AND ss.plan_version_id = d.current_plan_version_id
                      AND latest.state = 'done'
                      AND ss.retry_count > 0
                    """
                )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 -- see docstring
        logger.warning("scheduler: reschedule_failed_pulls_failed: %s", exc)
        return 0
    if moved:
        logger.info("scheduler: reschedule_failed_pulls: armed=%d catch-up runs", moved)
    return moved


def _dispatch_nightly_datastreams(
    as_of_date: date | None,
    requested_by: str,
    queue,
    get_connection,
) -> tuple[list[dict], int]:
    """Story 8.2: dispatch the ONCE-A-PERIOD cadences, iterating ENABLED datastreams.

    AI-217: `nightly` AND `weekly`. Migration 204 made `weekly` a legal
    `schedule_mode`, `schedule_mcp.CADENCES` accepted it, the Workbench offered
    it, and story 57.8 taught the advance (seven days) and the catch-up sweep
    (`schedule_mode IN ('nightly','weekly')`) to speak it -- feeding a dispatcher
    that did not exist. `grep "ds.schedule_mode = "` returned `'nightly'` here and
    `'hourly'` in the sibling, and nothing else. An operator could choose a weekly
    cadence, save it, see it displayed, and the collection would never fire.

    It belongs HERE and not in a third dispatcher, because eligibility is already
    decided by the right predicate: `next_run_at IS NULL OR next_run_at <= NOW()`
    selects a due weekly row exactly as it selects a due nightly one. The two
    cadences differ in how far the advance moves afterwards, which is a value the
    row carries -- so the cadence travels from the selected row into
    `_advance_next_run` rather than being named by the caller. The hourly sibling
    could not have hosted it: it has no due guard and calls no advance at all, so
    a weekly row placed there would be pulled every hour for ever.

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
    # AI-117: kept as the FALLBACK only. Each datastream's window is now measured
    # in its own project's timezone -- two projects do not share a yesterday, and
    # a single value for all of them asked a provider for a day one of them had
    # not finished living. `as_of_date` still decides when a caller pins the run
    # (tests, a replay), so an explicit date is never overridden.
    fallback_yesterday = (as_of_date or date.today()) - timedelta(days=1)
    timezone_cache: dict[str, str] = {}
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
                        ds.window_offset_days,
                        ds.source_kind,
                        ds.schedule_mode,
                        -- Story 63.1: the versions the run's execution is minted
                        -- against. Both are already JOINed and NOT NULL-filtered
                        -- below; only the projection was missing.
                        ds.current_plan_version_id,
                        ds.current_mapping_version_id,
                        -- Projected even though the WHERE below already filters
                        -- them: `datastream_dispatch.gate_refusal` reads what the
                        -- ROW declares, and an absent key is not a "yes". The
                        -- manual door runs the very same gates on the very same
                        -- columns -- that is the point of projecting them.
                        ds.enabled          AS enabled,
                        ds.lifecycle_state  AS lifecycle_state,
                        -- 2026-08-18: the column the archive really writes, for
                        -- the same reason as the two above -- `gate_refusal`
                        -- reads the row, and an unprojected key is refused as
                        -- `row_incomplete` rather than passed.
                        ds.archived_at      AS archived_at,
                        pm.enabled          AS module_enabled,
                        p.status            AS project_status,
                        cr.id               AS connection_ref_id,
                        cr.status           AS cr_status,
                        cr.enabled          AS cr_enabled
                    FROM app.datastreams ds
                    JOIN app.projects p
                        ON p.id = ds.project_id AND p.status = 'active'
                    JOIN app.datastream_plan_versions pv
                        ON pv.id = ds.current_plan_version_id
                       AND pv.datastream_id = ds.id AND pv.project_id = ds.project_id
                    JOIN app.datastream_mapping_versions mv
                        ON mv.id = ds.current_mapping_version_id
                       AND mv.datastream_id = ds.id AND mv.project_id = ds.project_id
                    JOIN app.datastream_schedule_state ss
                        ON ss.plan_version_id = ds.current_plan_version_id
                       AND ss.datastream_id = ds.id AND ss.project_id = ds.project_id
                    LEFT JOIN app.connection_ref cr
                        ON cr.id = ds.connection_ref_id
                    LEFT JOIN app.project_modules pm
                        ON pm.project_id = ds.project_id
                        AND pm.module_name = ds.module_name
                    WHERE ds.enabled = TRUE
                      AND ds.lifecycle_state = 'active'
                      AND ds.archived_at IS NULL
                      AND ds.current_plan_version_id IS NOT NULL
                      AND ds.current_mapping_version_id IS NOT NULL
                      -- AI-217: the two cadences that run once a period. `weekly`
                      -- was legal (migration 204), offered, saved and displayed,
                      -- and selected by nothing -- so it never ran.
                      AND ds.schedule_mode IN ('nightly', 'weekly')
                      -- AI-119: the MOMENT lives in Postgres, not in an env var.
                      -- `datastream_schedule_state.next_run_at` was written at
                      -- activation and read by NOBODY: `next_run_at` did not appear
                      -- once in this file, so every active datastream fired at
                      -- SCHEDULER_NIGHTLY_HOUR -- one hour, one timezone, for every
                      -- project on the platform. It is authoritative when SET, and
                      -- NULL keeps the previous behaviour exactly, so a datastream
                      -- activated before this line still runs.
                      --
                      -- This is also what lets the Cloud Scheduler tick be dumb and
                      -- FREQUENT (AD-36): "dispatch whatever is due" re-read from the
                      -- ledger, instead of a cron that encodes the answer.
                      AND (ss.next_run_at IS NULL OR ss.next_run_at <= NOW())
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
        logger.warning("scheduler: dispatch_nightly: db_error reading datastreams: %s", exc)
        return [], 0

    for ds in datastream_rows:
        ds_id = ds["ds_id"]

        # THE GATES, shared with the manual door (`core.datastream_dispatch`).
        # They used to be typed here as two inline `continue`s, so a run asked
        # for by hand passed none of them.
        refusal = datastream_dispatch.gate_refusal(ds)
        if refusal is not None:
            logger.debug(
                "scheduler: datastream_not_dispatchable ds_id=%s code=%s",
                ds_id,
                refusal.code,
            )
            # The SELECT above already required `enabled` AND `lifecycle_state
            # = 'active'` AND a due `next_run_at`, so a row reaching this gate is
            # ARMED and its moment has come. But "armed and refused" is not yet
            # "owed and missed": a managed feed arrives here every night and owes
            # no fetch at all. Only the codes that name a collection someone
            # could release count -- see `MISSED_RUN_REFUSALS`.
            if refusal.code in datastream_dispatch.MISSED_RUN_REFUSALS:
                _record_missed_run(
                    get_connection, ds_id, ds["project_id"], reason=refusal.code
                )
            continue

        # AI-117: THIS project's yesterday, not the deployment's. Resolved per
        # project and cached for the dispatch, so a fleet in one timezone costs
        # one query rather than one per datastream.
        with get_connection() as _tz_conn:
            _tz = project_timezone(_tz_conn, ds["project_id"], timezone_cache)
        yesterday = project_yesterday(_tz) if as_of_date is None else fallback_yesterday

        # AI-46 (precedence) + AI-145 (offset) + AI-217 (cadence floor). The
        # arbitration used to be TYPED HERE and re-typed in two other modules,
        # so a first candidate could cover a different window than every night
        # after it. It now lives in `core.pull_window`, unchanged, and this is a
        # call -- see that module's docstring for the rule and its history.
        cadence = ds.get("schedule_mode") or "nightly"
        window = pull_window.resolve_window(ds, end_reference=yesterday, cadence=cadence)
        window_days = window.length.days
        date_from = window.date_from
        date_to = window.date_to
        logger.debug(
            "scheduler: ds=%s window=%d days from %s (offset=%d)",
            ds_id,
            window_days,
            window.length.source,
            window.offset_days,
        )
        if window.length.widened_for:
            logger.info(
                "scheduler: ds=%s window widened to %d days for the %s cadence (a shorter "
                "window drops the days between two runs)",
                ds_id,
                window_days,
                window.length.widened_for,
            )

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
        # Story 63.1: the run gets a LINE before its first window is queued, and
        # the windows go through the ONE dispatch a manual run also takes.
        #
        # Measured on preprod 2026-08-05: `app.datastream_executions` held 0 rows
        # for 6 `app.pull_jobs` -- this dispatch created N jobs and no execution,
        # so a collection in flight was written nowhere a screen could read it.
        outcome = datastream_dispatch.dispatch_windows(
            get_connection,
            row=ds,
            windows=windows,
            queue=queue,
            actor=requested_by,
            origin=SCHEDULER_NIGHTLY,
            run_key=_dispatch_tick_key(),
            # AI-217: the row's OWN cadence. The literal `"nightly"` that stood
            # here moved a weekly row one day, and it would have been
            # re-dispatched every night -- seven times the provider quota its
            # cadence asks for. A MANUAL run passes no callback: it answers a
            # question now, it does not move the schedule a person set.
            on_enqueued=lambda _job: _advance_next_run(
                get_connection, ds_id, ds["project_id"], cadence
            ),
            # The counterpart of the line above, and wired here for the same
            # reason: this door owns a schedule. A window refused at the enqueue
            # moves no clock (AI-301) -- so without this it moved nothing at all,
            # and the Workbench read a refused night as a quiet one.
            on_missed=lambda code: _record_missed_run(
                get_connection, ds_id, ds["project_id"], reason=code
            ),
        )
        all_jobs.extend(outcome.jobs)

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
      * WHERE ds.schedule_mode = 'hourly' (migration 110 widened the CHECK).
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
                        -- Story 63.1: an hourly run is a run too. The defect is
                        -- the class's, not the nightly path's.
                        ds.current_plan_version_id,
                        ds.current_mapping_version_id,
                        -- Same columns as its nightly sibling and as the manual
                        -- door, because the same `gate_refusal` reads all three.
                        ds.enabled          AS enabled,
                        ds.lifecycle_state  AS lifecycle_state,
                        -- 2026-08-18: the column the archive really writes. The
                        -- WHERE below already excludes it, and the gate reads
                        -- what the ROW declares -- an absent key is `row_incomplete`,
                        -- not a "no". Same reason the two above are projected.
                        ds.archived_at      AS archived_at,
                        pm.enabled          AS module_enabled,
                        p.status            AS project_status,
                        cr.id               AS connection_ref_id,
                        cr.status           AS cr_status,
                        cr.enabled          AS cr_enabled
                    FROM app.datastreams ds
                    JOIN app.projects p
                        ON p.id = ds.project_id AND p.status = 'active'
                    JOIN app.datastream_plan_versions pv
                        ON pv.id = ds.current_plan_version_id
                       AND pv.datastream_id = ds.id AND pv.project_id = ds.project_id
                    JOIN app.datastream_mapping_versions mv
                        ON mv.id = ds.current_mapping_version_id
                       AND mv.datastream_id = ds.id AND mv.project_id = ds.project_id
                    JOIN app.datastream_schedule_state ss
                        ON ss.plan_version_id = ds.current_plan_version_id
                       AND ss.datastream_id = ds.id AND ss.project_id = ds.project_id
                    LEFT JOIN app.connection_ref cr
                        ON cr.id = ds.connection_ref_id
                    LEFT JOIN app.project_modules pm
                        ON pm.project_id = ds.project_id
                        AND pm.module_name = ds.module_name
                    WHERE ds.enabled = TRUE
                      AND ds.lifecycle_state = 'active'
                      AND ds.archived_at IS NULL
                      AND ds.current_plan_version_id IS NOT NULL
                      AND ds.current_mapping_version_id IS NOT NULL
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
        refusal = datastream_dispatch.gate_refusal(ds)
        if refusal is not None:
            logger.debug(
                "scheduler: hourly_datastream_not_dispatchable ds_id=%s code=%s",
                ds_id,
                refusal.code,
            )
            continue

        # The SAME precedence as the nightly sibling, now by call rather than by
        # copy (`core.pull_window`). Only the LENGTH is shared: this path keeps
        # its own end date on purpose -- see below.
        window_days = pull_window.resolve_window_days(ds, cadence="hourly").days
        # Today-inclusive window (hourly re-pull of the current accumulating day).
        # `window_offset_days` is deliberately NOT applied here: an hourly run
        # exists to re-read the day still in progress, and shifting its end back
        # would make it re-read a day it already finished.
        date_from = (today - timedelta(days=window_days - 1)).isoformat()
        date_to = today.isoformat()

        datastream_count += 1
        outcome = datastream_dispatch.dispatch_windows(
            get_connection,
            row=ds,
            windows=[{"date_from": date_from, "date_to": date_to}],
            queue=queue,
            actor=requested_by,
            origin=SCHEDULER_HOURLY,
            run_key=_dispatch_tick_key(),
        )
        all_jobs.extend(outcome.jobs)

    return all_jobs, datastream_count


def dispatch_hourly(
    as_of_date: date | None = None,
    *,
    requested_by: str = "scheduler",
) -> list[dict]:
    """The FREQUENT tick: everything that is due right now. Story 57.8, A2.

    Enqueues hourly pull jobs per enabled hourly datastream (Story 12.6 debt),
    AND the `nightly` rows whose `next_run_at` has been reached.

    WHY THE SECOND HALF EXISTS. `dispatch-nightly` fires at 02:00 and was the
    only clock that looked at a nightly row, so an arrival hour of 06:00 would
    first be SEEN at 02:00 the following day, and a catch-up an hour after a
    failure could not exist at all. The nightly selection was already written as
    "whatever is due" (`ss.next_run_at IS NULL OR ss.next_run_at <= NOW()`) and
    says so in its own comment (AD-36, "dumb and FREQUENT") -- only its cron
    strangled it. `dispatch-nightly` stays, as the net.

    THE DOUBLE DISPATCH IS PREVENTED, NOT UNLIKELY. Two clocks now read the same
    rows. What stops one window being enqueued twice is that
    `_advance_next_run` runs immediately after each successful enqueue, so the
    due guard no longer selects the row -- measured in
    `tests/integration/test_schedule_advance_pg.py`. `enqueue_pull` deduplicating
    a pending window is the second line, never the first: it would hide the
    duplicate intent rather than prevent it.

    Idempotent; AD-2/AD-7/AD-12 as nightly. No per-connection fallback -- that is
    a nightly backfill concept and it stays in `dispatch_nightly`.
    """
    # `None` is not `date.today()` here (AI-117): a pinned date means a replay,
    # an unpinned one means each project measures its own yesterday. Resolving it
    # before the nightly call would erase the distinction.
    pinned_as_of = as_of_date
    if as_of_date is None:
        as_of_date = date.today()
    from core import queue  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    all_jobs, _count = _dispatch_hourly_datastreams(as_of_date, requested_by, queue, get_connection)
    due_nightly, _nightly_count = _dispatch_nightly_datastreams(
        pinned_as_of, requested_by, queue, get_connection
    )
    return all_jobs + due_nightly


def _run_managed_feed_syncs() -> None:
    """Story 12.10 dispatch hook: run enabled daily/hourly managed-feed sync schedules.

    Env-guarded (MANAGED_FEED_SYNC_ENABLED, default off). Reads
    app.managed_feed_sync_schedule and calls google_sheets_sync.dispatch_managed_feed_sync
    per schedule, each isolated.

    The Sheets adapter is INJECTED now. This paragraph used to say the injection
    was deferred and that `sheets_adapter=None` made `run_sync` raise
    NotImplementedError "caught here per schedule" -- which is an accurate
    description of a sync that never read a cell, phrased as a plan. The 15.6
    reader existed throughout; only the closure joining it to a connection was
    missing (google_sheets_adapter).

    Still deferred, and genuinely: the atomic pointer swap needs live-warehouse
    rows (AI-08).
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

    from core.inbound_seam import resolve_inbound  # noqa: PLC0415

    values_adapter_factory = resolve_inbound("managed_feed_values_adapter_factory")

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
                    # Was `None` with a "PHASE_B_LIVE_BLOCKED: inject the 15.6
                    # adapter" note, which made every scheduled Sheets sync raise
                    # NotImplementedError before reading a cell. The 15.6 reader
                    # existed the whole time; only the closure joining it to a
                    # connection was missing.
                    sheets_adapter=values_adapter_factory(),
                    last_committed_watermark=sched.get("last_watermark"),
                )
                conn.commit()
        except Exception as exc:  # per-schedule isolation
            logger.warning(
                "scheduler: managed_feed_sync failed ds=%s: %s",
                sched.get("datastream_id"),
                exc,
            )


def run_hourly_steps(as_of_date: date | None = None) -> None:
    """Run the recurring HOURLY steps, each isolated (mirrors run_nightly_steps).

    Steps: reschedule_failed_pulls (Story 57.8) + dispatch_hourly (Story 12.6,
    widened to the due nightly rows by 57.8) + managed_feed_syncs (Story 12.10,
    env-guarded). Double-fire protected by the shared advisory lock; a step
    failure/timeout is logged and meta-alerted but never blocks the other steps.

    The catch-up sweep runs FIRST, so a Datastream whose pull failed since the
    last tick carries an accurate `retry_count` before anything is dispatched.
    It belongs on this tick and nowhere else: a catch-up an hour after a failure
    is unreachable from a step that runs once a night.
    """
    if as_of_date is None:
        as_of_date = date.today()

    lock_acquired = _try_advisory_lock()
    if lock_acquired is False:
        logger.info("scheduler: hourly_skipped: another instance holds the advisory lock")
        return
    try:
        _degraded: list[str] = []
        from core.db import get_connection  # noqa: PLC0415

        _run_isolated_step(
            "reschedule_failed_pulls",
            _reschedule_failed_pulls,
            _degraded=_degraded,
            get_connection=get_connection,
        )
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
    pinned_as_of = as_of_date
    if as_of_date is None:
        as_of_date = date.today()

    from core import queue  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    # ---------------------------------------------------------------------------
    # Story 8.2: Primary path -- dispatch per enabled datastream.
    # ---------------------------------------------------------------------------
    # AI-117: the datastream path receives the caller's ORIGINAL intent. Resolving
    # `date.today()` before this call erased the distinction between "run for a
    # pinned date" (a replay, a test) and "run for now" -- and only the second may
    # measure yesterday in each project's own timezone.
    all_jobs, datastream_count = _dispatch_nightly_datastreams(
        pinned_as_of, requested_by, queue, get_connection
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
        logger.warning("scheduler: dispatch_nightly: db_error reading legacy connections: %s", exc)
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

#: The dbt project as the RUNTIME sees it. `server/core/scheduler.py` ->
#: parents[2] is the repo root locally and `/app` in the served image -- the same
#: derivation the six core modules use for `dbt/seeds`, and the reason the
#: Dockerfile copies the models, macros and `dbt_project.yml` next to them.
#: Without `--project-dir`, `dbt build` looks in the process CWD (`/app`), finds
#: no `dbt_project.yml` and exits before touching a warehouse.
_DBT_PROJECT_DIR = Path(__file__).resolve().parents[2] / "dbt"


def _dbt_location_args() -> list[str]:
    """``--project-dir`` / ``--profiles-dir`` when this checkout carries them.

    Both are appended only when the file that makes them meaningful exists, so a
    machine that drives dbt some other way (DBT_PROFILES_DIR, a CWD inside dbt/)
    keeps its behaviour instead of being pointed at a directory that is not there.

    THAT SENTENCE WAS ONLY HALF TRUE, and the half that was false cost a
    diagnosis (2026-08-24). `--profiles-dir` on the command line OVERRIDES
    `DBT_PROFILES_DIR`, so a machine that set the variable was silently ignored:
    replaying a failing production build locally ran against the DuckDB profile
    the local loop regenerates, answered `Catalog Error` -- DuckDB's wording --
    and looked like an answer about production. The flag is now withheld when the
    caller has named a profiles directory, which is what dbt's own precedence
    expects. The container sets no such variable, so the served behaviour does
    not move by one character.
    """
    args: list[str] = []
    if (_DBT_PROJECT_DIR / "dbt_project.yml").is_file():
        args += ["--project-dir", str(_DBT_PROJECT_DIR)]
    if (os.environ.get("DBT_PROFILES_DIR") or "").strip():
        return args
    profiles_dir = _DBT_PROJECT_DIR / "profiles"
    if (profiles_dir / "profiles.yml").is_file():
        args += ["--profiles-dir", str(profiles_dir)]
    return args


#: What dbt calls a failure, in its own words. Matched case-insensitively on a
#: whole line so a model named `error_budget` does not become a diagnosis.
_DBT_ERROR_MARKERS = (
    "compilation error",
    "database error",
    "runtime error",
    "parsing error",
    "failure in model",
    "failure in test",
    "error:",
    "traceback",
)

#: How much of a failing build reaches a person. Five lines is what fits in a
#: log line and an alert without either becoming a dump nobody reads; the rest
#: stays in the container's own dbt log.
_DBT_ERROR_LINES = 5
_DBT_ERROR_LINE_CHARS = 220


def _dbt_error_lines(stdout: str, stderr: str) -> list[str]:
    """The lines of a failing dbt run that say WHY, bounded.

    stderr first: a crash prints there and prints nothing to stdout, so a
    stdout-only reader would report an empty cause for the loudest failure.
    Falls back to the tail of stdout when no line matches a marker -- an
    unrecognised failure must still arrive with something rather than with an
    empty list that reads like "no reason given".
    """
    matched: list[str] = []
    for stream in (stderr, stdout):
        lines = [line.strip() for line in stream.splitlines()]
        for index, text in enumerate(lines):
            if not text:
                continue
            if not any(marker in text.lower() for marker in _DBT_ERROR_MARKERS):
                continue
            # THE MARKER NAMES THE MODEL; THE NEXT LINE SAYS WHY. dbt prints
            # "Database Error in model X (path)" and puts the message underneath,
            # so a capture that took marker lines alone named five broken models
            # and not one cause -- measured on the first real use of this capture
            # (2026-08-24: three models named, zero reasons).
            follow = next(
                (
                    candidate
                    for candidate in lines[index + 1 : index + 4]
                    if candidate
                    and not any(m in candidate.lower() for m in _DBT_ERROR_MARKERS)
                ),
                "",
            )
            entry = f"{text} -- {follow}" if follow else text
            matched.append(entry[:_DBT_ERROR_LINE_CHARS])
            if len(matched) >= _DBT_ERROR_LINES:
                return matched
    if matched:
        return matched
    tail = [line.strip() for line in (stderr or stdout).splitlines() if line.strip()]
    return [line[:_DBT_ERROR_LINE_CHARS] for line in tail[-_DBT_ERROR_LINES:]]


#: The word a GUARDED model prints, and the whole contract between a dbt model
#: and this runner (`toorow_log_source_absent`, dbt/macros/relation_present.sql).
#: A model whose source is not in the warehouse is BUILT EMPTY rather than
#: failing the build -- which means dbt exits 0 having built models, and without
#: this line the Project would be counted `ok` while everything governed it
#: produced is empty.
_DBT_SOURCE_ABSENT_MARKER = "TOOROW_SOURCE_ABSENT"

#: How many guarded models travel with the summary. The names matter (they say
#: WHICH half of the warehouse is missing); the full list of twenty-one is a dump.
_DBT_GUARDED_MODELS = 8


def _dbt_guarded_models(stdout: str) -> list[str]:
    """The models dbt built EMPTY because a declared source is not in the warehouse.

    Read off the marker line rather than inferred from a row count: a model can
    legitimately be empty (a Project with no plan has no plan rows), and the two
    facts are repaired by different people. Only the guard knows the difference,
    so only the guard says it.
    """
    found: list[str] = []
    for line in stdout.splitlines():
        if _DBT_SOURCE_ABSENT_MARKER not in line:
            continue
        for token in line.split():
            if token.startswith("model="):
                name = token.split("=", 1)[1]
                if name and name not in found:
                    found.append(name)
    return sorted(found)


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

    cmd = ["dbt", *(args or ["run"]), *_dbt_location_args()]
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
        stderr = getattr(completed, "stderr", "") or ""
        errors = _dbt_error_lines(stdout, stderr) if exit_code != 0 else []
        guarded = _dbt_guarded_models(stdout)
        span.set("dbt.exit_code", exit_code)
        span.set("dbt.models_run", models_run)
        span.set("dbt.latency_ms", latency_ms)
        span.set("dbt.guarded_models", len(guarded))

    return {
        "exit_code": exit_code,
        "models_run": models_run,
        "latency_ms": latency_ms,
        # WHAT WAS BUILT EMPTY, AND WHY -- see `_dbt_guarded_models`. An exit code
        # of 0 over a warehouse missing half its inputs is a true statement about
        # dbt and a false one about the Project.
        "guarded_models": guarded,
        # WHAT DBT SAID, and it used to say it to nobody. `capture_output=True`
        # has always held the message; this function returned three integers and
        # dropped the rest, so a failing nightly logged `dbt exit=1` and a person
        # had NOTHING to act on -- measured on the first nightly that ran to
        # completion (2026-08-24: two projects failed, and the only trace of why
        # was an exit code). Bounded on purpose: the full log belongs to the
        # container, one readable handful belongs to whoever is woken up.
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# One dbt build per ACTIVE PROJECT (AI-166, 2026-08-17)
# ---------------------------------------------------------------------------
#
# WHY NOT PER ORG, which is what this step did until today. Measured against the
# live warehouse on 2026-08-17: `org_toorow_raw` and `org_toorow_marts` exist and
# hold ZERO tables, while `raw_proj_01KZGCRS...` carries the landed relations. The
# per-org runner passed `raw_schema=org_<wslug>_raw`, so enabling it would have
# built FROM an empty dataset INTO an empty one and exited 0 -- a green nightly
# reporting success over nothing. `TOOROW_ORG_SCHEMAS` stays OFF (correctly: with
# it off the resolvers address `raw_<project_id>` / `marts_<project_id>`, which is
# the shape the live datasets carry), and the builder now addresses the same zones
# the readers do, resolved by the same functions.


def _raw_zone_is_populated(dataset: str) -> bool:
    """True when the project's RAW dataset exists and holds at least one table.

    Asked BEFORE dbt, because "this project has no data yet" and "the build
    broke" are different facts and a nightly that cannot tell them apart either
    alerts on every new project or stays silent on a real regression. The probe
    is metadata only (``get_dataset`` + one page of ``list_tables``): no bytes
    scanned, nothing billed.

    Outside BigQuery mode there is nothing to probe -- the local DuckDB loop has
    a single file and dbt itself reports what it found -- so the answer is True
    and the classification below still separates "built nothing" from "failed".
    """
    if (os.environ.get("TOOROW_DB_MODE") or "").strip().lower() != "bigquery":
        return True

    from google.cloud import bigquery  # noqa: PLC0415 -- optional dep

    from core.raw_landing import resolve_warehouse_project  # noqa: PLC0415

    client = bigquery.Client(project=resolve_warehouse_project())
    ref = f"{client.project}.{dataset}"
    try:
        client.get_dataset(ref)
    except Exception:  # noqa: BLE001 -- NotFound and permission errors both mean "no zone"
        return False
    return any(True for _ in client.list_tables(ref, max_results=1))


#: A connector owns everything under `server/modules/<name>/dbt/` -- its staging
#: models AND its own marts. The unit of exclusion is the CONNECTOR, not the
#: staging model: excluding `stg_gbp_location_daily` alone left
#: `fact_gbp_location_daily` pointing at a relation nobody built.
_MODULES_DIR = _DBT_PROJECT_DIR.parent / "server" / "modules"

_SOURCE_CALL = re.compile(r"source\(\s*['\"]([a-z0-9_]+)['\"]\s*,\s*['\"]([a-z0-9_]+)['\"]")


def _raw_zone_tables(dataset: str) -> set[str]:
    """Every table name in the project's RAW zone, or an empty set outside BigQuery.

    Metadata only, like `_raw_zone_is_populated` beside it: no bytes scanned.
    """
    if (os.environ.get("TOOROW_DB_MODE") or "").strip().lower() != "bigquery":
        return set()

    from google.cloud import bigquery  # noqa: PLC0415 -- optional dep

    from core.raw_landing import resolve_warehouse_project  # noqa: PLC0415

    client = bigquery.Client(project=resolve_warehouse_project())
    try:
        return {table.table_id for table in client.list_tables(f"{client.project}.{dataset}")}
    except Exception:  # noqa: BLE001 -- NotFound and permission errors both mean "no zone"
        return set()


def _models_of_absent_connectors(raw_tables: set[str]) -> list[str]:
    """Everything this Project cannot build, because it landed nothing for it.

    THE DEFECT THIS ANSWERS, measured 2026-08-18. The nightly ran a bare
    `dbt build`, which builds EVERY model of the catalogue against the raw zone
    of ONE Project. The only production Project carrying data reads YouTube and
    nothing else, so the models of fifty other connectors pointed at tables it
    has never landed and the build returned 53 `Catalog Error: Table with name
    raw_<connector>_daily does not exist!`. `fact_daily_kpi` is downstream of all
    of them, so it was never built and every card reading it answered
    `warehouse_not_ready` -- on a Project holding 34 640 raw rows and 547 videos.

    A Project that does not use a connector is not a broken Project. Three things
    are therefore excluded, and the third is the one a first pass missed:

      * every model a connector owns (`server/modules/<name>/dbt/**`) -- its
        staging AND its own marts, because excluding `stg_gbp_location_daily`
        alone left `fact_gbp_location_daily` pointing at nothing;
      * the connector's declared SOURCES, so their `not_null` source tests do not
        run against a table that is not there;
      * the SHARED marts that exist to serve those connectors alone --
        `semantic_avg_position` is GSC's, `transaction_reconciliation` is
        Shopify's. A shared model is excluded only when EVERY staging model it
        reads belongs to an absent connector, which is why `fact_daily_kpi`
        itself is never excluded: one present connector keeps it.

    The transverse facts drop the matching branches through
    `toorow_model_present` / `toorow_source_present`
    (dbt/macros/relation_present.sql). The two halves go together: excluding
    alone would make dbt SKIP the facts as missing dependencies; guarding alone
    would still fail on the absent source.

    Read off the model files rather than from a list kept here: a connector added
    tomorrow declares its `source()` in its own directory, and a second inventory
    in this file would be the thing that goes stale. A module that declares no
    `source()` is never excluded -- absence of evidence about a module is not
    evidence that the Project does not use it.
    """
    if not raw_tables or not _MODULES_DIR.is_dir():
        return []

    excluded: list[str] = []
    absent_staging: set[str] = set()
    for module in sorted(_MODULES_DIR.iterdir()):
        dbt_dir = module / "dbt"
        if not dbt_dir.is_dir():
            continue
        models = sorted(dbt_dir.glob("**/*.sql"))
        declared: set[tuple[str, str]] = set()
        for path in models:
            try:
                declared |= set(_SOURCE_CALL.findall(path.read_text(encoding="utf-8")))
            except OSError:
                continue
        tables = {table for _name, table in declared}
        if not tables or (tables & raw_tables):
            continue
        excluded.extend(path.stem for path in models)
        excluded.extend(f"source:{name}" for name, _table in declared)
        absent_staging |= {path.stem for path in models if path.stem.startswith("stg_")}

    shared = _DBT_PROJECT_DIR / "models"
    if shared.is_dir() and absent_staging:
        for path in sorted(shared.glob("**/*.sql")):
            try:
                body = path.read_text(encoding="utf-8")
            except OSError:
                continue
            staged = set(re.findall(r"ref\(\s*['\"](stg_[a-z0-9_]+)['\"]", body))
            if staged and staged <= absent_staging:
                excluded.append(path.stem)

    return sorted(set(excluded))



def run_dbt_per_project(
    *,
    trace_id: str | None = None,
    _runner=None,
    _list_projects=None,
    _raw_populated=None,
) -> dict:
    """Run ``dbt build`` ONCE per active project, each into its ``marts_<project_id>``.

    Project list and zone names come from ``warehouse_tenancy.list_active_projects``
    -- the single Python naming point, which composes them with the very resolvers
    the read layer calls. Nothing is composed here (epic-24 invariant).

    Each build gets ``--vars '{"project": "<project_id>", "raw_schema": "<raw>"}'``:
      * ``project``    -- consumed by ``generate_schema_name`` for staging/marts.
      * ``raw_schema`` -- the fully-qualified raw dataset, consumed by
        ``schema: "{{ var('raw_schema', 'main') }}"`` in every module schema.yml.

    Isolation: a failing project is logged + meta-alerted but never aborts the
    others. The mirror is NOT synced here (AD-8: ``mirror_sync`` stays the single
    central Postgres->warehouse path).

    A ZERO-ROW RUN IS NOT A SUCCESS, and that is the point of this step's rewrite.
    Five outcomes, four of which are not "ok":
      * ``no_raw_data``   -- the project has no raw dataset, or an empty one. dbt
        is not invoked at all; no alert, because a project that has not pulled yet
        is an ordinary state, not a regression.
      * ``nothing_built`` -- raw IS populated and dbt exited 0 having built no
        model. That is a real defect (sources present, marts absent) and it is
        meta-alerted; it is never counted as ``ok``.
      * ``source_absent`` -- dbt exited 0 having built models, and some of them
        were built EMPTY because a declared source is not in this warehouse
        (AI-314: `mirror_sync` defers its BigQuery writes, so no `mirror` dataset
        exists in production and every governed mart is guarded to empty). The
        build no longer FAILS -- that is the repair -- but a Project whose
        governed marts are all empty has not built what it was asked to build,
        and saying ``ok`` over it would hide exactly the thing that needs doing.
        Meta-alerted, and the alert NAMES the absent source.
      * ``failed``        -- non-zero exit, or the runner raised. Meta-alerted.
      * ``ok``            -- raw populated, exit 0, at least one model built, and
        nothing built empty for want of a source.

    Guard: ``DBT_NIGHTLY_ENABLED`` (default off). When the flag is off OR there is
    no active project this is a no-op.

    ``_runner`` / ``_list_projects`` / ``_raw_populated`` are injectable for tests.

    Returns a summary dict::
        {"status": "ok"|"disabled"|"skipped"|"source_absent"|"nothing_built",
         "projects": N, "ok": k, "nothing_built": n, "no_raw_data": z,
         "source_absent": g, "failed": m,
         "results": [{"project_id": p, "marts": d, "exit_code": int|None,
                      "models_run": int, "guarded_models": [...], "status": ...}]}
    """
    empty = {
        "projects": 0,
        "ok": 0,
        "nothing_built": 0,
        "no_raw_data": 0,
        "source_absent": 0,
        "failed": 0,
        "results": [],
    }
    if os.environ.get("DBT_NIGHTLY_ENABLED", "false").lower() != "true":
        logger.info("scheduler: dbt_per_project_disabled -- DBT_NIGHTLY_ENABLED not true")
        return {"status": "disabled", **empty}

    from core import warehouse_tenancy  # noqa: PLC0415

    list_projects = _list_projects or warehouse_tenancy.list_active_projects
    projects = list_projects()
    if not projects:
        logger.info("scheduler: dbt_per_project_no_active_project")
        return {"status": "skipped", **empty}

    raw_populated = _raw_populated or _raw_zone_is_populated

    results: list[dict] = []
    counts = {
        "ok": 0,
        "nothing_built": 0,
        "no_raw_data": 0,
        "source_absent": 0,
        "failed": 0,
    }
    for zones in projects:
        entry: dict = {
            "project_id": zones.project_id,
            "marts": zones.marts,
            "exit_code": None,
            "models_run": 0,
            "guarded_models": [],
        }
        try:
            populated = bool(raw_populated(zones.raw))
        except Exception as exc:  # noqa: BLE001 -- one project failure != abort the others
            logger.warning(
                "scheduler: dbt_per_project_probe_error project_id=%s raw=%s: %s",
                zones.project_id,
                zones.raw,
                exc,
            )
            _insert_meta_alert("dbt_per_project", f"{zones.project_id}: raw probe failed: {exc}")
            counts["failed"] += 1
            results.append({**entry, "status": "failed"})
            continue

        if not populated:
            logger.info(
                "scheduler: dbt_per_project_no_raw_data project_id=%s raw=%s "
                "-- nothing built (the project has landed no raw relation yet)",
                zones.project_id,
                zones.raw,
            )
            counts["no_raw_data"] += 1
            results.append({**entry, "status": "no_raw_data"})
            continue

        vars_json = json.dumps({"project": zones.project_id, "raw_schema": zones.raw})
        # A Project builds the connectors it HAS. Without this the nightly ran
        # every staging model of the catalogue against one Project's raw zone and
        # failed on each absent table -- see `_staging_models_without_source`.
        try:
            skipped_models = _models_of_absent_connectors(_raw_zone_tables(zones.raw))
        except Exception as exc:  # noqa: BLE001 -- a probe failure must not lose the build
            logger.warning(
                "scheduler: dbt_per_project_exclusion_unresolved project_id=%s: %s",
                zones.project_id,
                exc,
            )
            skipped_models = []
        build_args = ["build", "--vars", vars_json]
        if skipped_models:
            logger.info(
                "scheduler: dbt_per_project_excludes project_id=%s count=%d models=%s",
                zones.project_id,
                len(skipped_models),
                ",".join(skipped_models),
            )
            build_args += ["--exclude", *skipped_models]
        try:
            res = run_dbt(build_args, trace_id=trace_id, _runner=_runner)
            exit_code = int(res.get("exit_code", 0) or 0)
            models_run = int(res.get("models_run", 0) or 0)
            dbt_errors = list(res.get("errors") or [])
            guarded_models = list(res.get("guarded_models") or [])
        except Exception as exc:  # noqa: BLE001 -- one project failure != abort the others
            logger.warning(
                "scheduler: dbt_per_project_error project_id=%s: %s", zones.project_id, exc
            )
            _insert_meta_alert("dbt_per_project", f"{zones.project_id}: {exc}")
            counts["failed"] += 1
            results.append({**entry, "status": "failed"})
            continue

        entry["exit_code"] = exit_code
        entry["models_run"] = models_run
        entry["guarded_models"] = guarded_models
        if exit_code != 0:
            counts["failed"] += 1
            # THE EXIT CODE IS NOT THE REASON. `dbt exit=1` was the whole of what
            # a failing nightly said, in the log AND in the alert, so nobody woken
            # by it could do anything but re-run it by hand. dbt's own words now
            # travel with the failure -- bounded, and the first of them is what
            # the alert carries.
            logger.warning(
                "scheduler: dbt_per_project_nonzero project_id=%s marts=%s exit=%d errors=%s",
                zones.project_id,
                zones.marts,
                exit_code,
                " | ".join(dbt_errors) if dbt_errors else "(dbt said nothing)",
            )
            first_error = dbt_errors[0] if dbt_errors else "no message captured"
            _insert_meta_alert(
                "dbt_per_project",
                f"{zones.project_id}: dbt exit={exit_code} -- {first_error}",
            )
            results.append({**entry, "status": "failed"})
        elif models_run == 0:
            counts["nothing_built"] += 1
            logger.warning(
                "scheduler: dbt_per_project_nothing_built project_id=%s raw=%s marts=%s "
                "-- raw carries relations and dbt exited 0 having built no model",
                zones.project_id,
                zones.raw,
                zones.marts,
            )
            _insert_meta_alert(
                "dbt_per_project",
                f"{zones.project_id}: dbt exited 0 and built no model over a populated "
                f"{zones.raw}",
            )
            results.append({**entry, "status": "nothing_built"})
        elif guarded_models:
            # BUILT, AND NOT BUILT. dbt exited 0 and produced relations, so the
            # only thing that stood between this and the word "ok" was the
            # guard's own line -- see `_dbt_guarded_models`. The names travel
            # into the alert because "the mirror is absent" and "this Project has
            # no plan" are repaired by different people, and only the first is a
            # platform defect.
            counts["source_absent"] += 1
            named = ",".join(guarded_models[:_DBT_GUARDED_MODELS])
            more = len(guarded_models) - _DBT_GUARDED_MODELS
            if more > 0:
                named = f"{named} (+{more})"
            logger.warning(
                "scheduler: dbt_per_project_source_absent project_id=%s raw=%s marts=%s "
                "built=%d guarded=%d models=%s "
                "-- built EMPTY for want of a source that is not in this warehouse",
                zones.project_id,
                zones.raw,
                zones.marts,
                models_run,
                len(guarded_models),
                named,
            )
            _insert_meta_alert(
                "dbt_per_project",
                f"{zones.project_id}: {len(guarded_models)} model(s) built EMPTY -- "
                f"a declared source is not in this warehouse: {named}",
            )
            results.append({**entry, "status": "source_absent"})
        else:
            counts["ok"] += 1
            results.append({**entry, "status": "ok"})

    logger.info(
        "scheduler: dbt_per_project_complete projects=%d ok=%d nothing_built=%d "
        "no_raw_data=%d source_absent=%d failed=%d",
        len(projects),
        counts["ok"],
        counts["nothing_built"],
        counts["no_raw_data"],
        counts["source_absent"],
        counts["failed"],
    )
    # The summary status refuses to say "ok" when no mart was built. That single
    # word is what a reader takes away from the nightly, and it was the defect.
    #
    # AND IT REFUSES IT AGAIN when every Project that built anything built it
    # EMPTY for want of a source (AI-314). The order below is the order of
    # severity: one Project that genuinely built its marts makes the night "ok";
    # failing that, `source_absent` says the builds ran and produced nothing
    # governed, which is a different repair from `nothing_built` (dbt built no
    # model at all) and must not be spelled the same.
    if counts["ok"]:
        summary_status = "ok"
    elif counts["source_absent"]:
        summary_status = "source_absent"
    else:
        summary_status = "nothing_built"
    return {
        "status": summary_status,
        "projects": len(projects),
        **counts,
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
                    now.date(),
                    now.hour,
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

    Reads ALERTS_ENABLED env var (default "true"). The evaluator reads counts
    out of Postgres: with nothing to count it breaches nothing.
    Catches all exceptions to ensure the scheduler thread never crashes.
    Runs synchronously in the scheduler thread -- NOT a new thread.
    """
    if os.environ.get("ALERTS_ENABLED", "true").lower() != "true":
        logger.debug("scheduler: alerts_check_skipped -- ALERTS_ENABLED set false")
        return

    try:
        from core import infra_alerts  # noqa: PLC0415

        breaches = infra_alerts.evaluate_alerts()
        if breaches:
            channels = infra_alerts.build_channels()
            for breach in breaches:
                infra_alerts.notify_alert(breach, channels)
            logger.info("scheduler: alert_check_complete: breaches=%d", len(breaches))
        else:
            logger.debug("scheduler: alert_check_complete: all clear")
    except Exception as exc:
        logger.warning("scheduler: alert_check_error: %s", exc)

    # STORY 59.6 -- THE BRIDGE, and it is one function.
    #
    # The signals above are computed in memory and reach the console; the
    # firings the nine DQ monitors and the business, anomaly and mediaplan
    # evaluators PERSIST reached nothing at all. They are two different objects,
    # which is why this is a second call and not a wider `evaluate_alerts`: the
    # signals have no `project_id` and destinations are project-scoped.
    #
    # Its own try/except: a destination that refuses must not cost the nightly
    # step the alert evaluation that already ran above.
    try:
        from core import alert_destinations  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            summary = alert_destinations.deliver_pending_firings(conn)
        if summary.get("attempted"):
            logger.info("scheduler: alert_delivery_complete: %s", summary)
    except Exception as exc:  # noqa: BLE001
        logger.warning("scheduler: alert_delivery_error: %s", exc)


# ---------------------------------------------------------------------------
# Story 5.3 (AC6) -- _run_business_alert_check (scheduler piggyback)
# ---------------------------------------------------------------------------


def _run_business_alert_check() -> None:
    """Run the business threshold alert evaluator after nightly dispatch (Story 5.3, AC6).

    Reads BUSINESS_ALERTS_ENABLED env var (default "true"). With no threshold
    declared, the evaluator returns an empty list.
    Catches all exceptions to ensure the scheduler thread never crashes.
    Runs synchronously in the scheduler thread -- NOT a new thread.

    SCHEDULER_ENABLED=false in CI means this function is never called (the scheduler
    thread never starts), satisfying T6.3.
    """
    if os.environ.get("BUSINESS_ALERTS_ENABLED", "true").lower() != "true":
        logger.debug(
            "scheduler: business_alerts_check_skipped -- BUSINESS_ALERTS_ENABLED set false"
        )
        return

    try:
        from core import business_alerts  # noqa: PLC0415

        firings = business_alerts.evaluate_business_alerts()
        if firings:
            logger.info("scheduler: business_alert_check_complete: firings=%d", len(firings))
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


def _run_dbt_per_project() -> None:
    """Isolated nightly wrapper for run_dbt_per_project (AI-166).

    Guard: DBT_NIGHTLY_ENABLED (default off) is re-checked inside
    run_dbt_per_project, which NEVER raises past its per-project isolation.
    Catches defensively anyway to honour the scheduler's failure-isolation
    contract (a failed build must not break the nightly run).

    The line it logs carries the five counts, not a verdict: "ok=0
    nothing_built=0 no_raw_data=3" is the honest sentence for a platform whose
    projects have landed nothing, and it cannot be mistaken for a mart.
    `source_absent=N` is the fifth (AI-314): N projects whose build RAN and whose
    governed marts came out empty because a declared source is not in the
    warehouse.
    """
    if os.environ.get("DBT_NIGHTLY_ENABLED", "false").lower() != "true":
        # VISIBLE, not debug. A deployment whose nightly runs with the flag off
        # has marts SILENTLY absent -- no error anywhere, just no marts. The
        # scheduler only runs because an operator enabled it, so a disabled
        # build here is a configuration to surface, not a state to whisper:
        # one platform-scope warning firing per nightly, until the flag is set
        # (audit 2026-08-20, C7).
        logger.warning(
            "scheduler: dbt_per_project_disabled -- DBT_NIGHTLY_ENABLED is not true; "
            "marts will NOT be rebuilt tonight"
        )
        try:
            from core import infra_alerts  # noqa: PLC0415

            infra_alerts.write_infra_firing(
                alert_type="dbt_nightly_disabled",
                project_id=None,
                metric="scheduler_health",
                severity="warning",
                message=(
                    "Nightly dbt build is disabled (DBT_NIGHTLY_ENABLED is not "
                    "true): marts were not rebuilt. Set DBT_NIGHTLY_ENABLED=true "
                    "if this deployment is expected to serve marts."
                ),
                metadata={"remediation": "set DBT_NIGHTLY_ENABLED=true"},
            )
        except Exception as exc:  # noqa: BLE001 -- never break the nightly run
            logger.warning(
                "scheduler: dbt_nightly_disabled_firing_failed: %s: %s",
                type(exc).__name__,
                exc,
            )
        return
    try:
        result = run_dbt_per_project()
        logger.info(
            "scheduler: dbt_per_project_step_complete status=%s projects=%d ok=%d "
            "nothing_built=%d no_raw_data=%d source_absent=%d failed=%d",
            result.get("status"),
            result.get("projects", 0),
            result.get("ok", 0),
            result.get("nothing_built", 0),
            result.get("no_raw_data", 0),
            result.get("source_absent", 0),
            result.get("failed", 0),
        )
    except Exception as exc:  # noqa: BLE001 -- never break the nightly run
        logger.warning("scheduler: dbt_per_project_step_error: %s", exc)


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
        logger.debug("scheduler: schema_context_gen_skipped -- SCHEMA_CONTEXT_ENABLED not true")
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
            logger.info("scheduler: mediaplan_alert_check_complete: firings=%d", len(firings))
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

    Reads ANOMALY_ALERTS_ENABLED env var (default "true"). It reads the
    anomalies_daily mart: an absent or empty mart yields nothing.
    Catches all exceptions to ensure the scheduler thread never crashes.
    Runs synchronously in the scheduler thread -- NOT a new thread.
    Called AFTER _run_business_alert_check() (dbt run has already completed).

    SCHEDULER_ENABLED=false in CI means this function is never called (the scheduler
    thread never starts), satisfying HG-5.
    """
    if os.environ.get("ANOMALY_ALERTS_ENABLED", "true").lower() != "true":
        logger.debug("scheduler: anomaly_alerts_check_skipped -- ANOMALY_ALERTS_ENABLED set false")
        return

    try:
        from core import anomaly_alerts  # noqa: PLC0415

        firings = anomaly_alerts.evaluate_anomalies()
        if firings:
            logger.info("scheduler: anomaly_alert_check_complete: firings=%d", len(firings))
            for firing in firings:
                logger.warning(
                    "scheduler: anomaly_fired: metric=%s zscore=%.4f severity=%s firing_id=%s",
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

    # Story 56.5 (AD-36): under the push backend the clock lives in Cloud
    # Scheduler, which calls /internal/scheduler/dispatch-nightly and
    # dispatch-hourly. Keeping the thread as well would give the same clock two
    # dispatchers -- and this one cannot be relied on anyway: at
    # --min-instances=0 Cloud Run allocates no CPU between requests, so the
    # sleep/compare loop only advances while a request happens to be in flight.
    # That is why nothing has ever run nightly. Same guard as start_queue_worker.
    backend_env = os.environ.get("QUEUE_BACKEND", "local")
    if backend_env == "cloud_tasks":
        logger.info("scheduler: skipped for QUEUE_BACKEND=%s (Cloud Scheduler owns the clock)",
                    backend_env)
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
