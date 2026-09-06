"""Read and adjust WHEN a Datastream runs — from the model, not from a cron file.

AI-119. Jean, 2026-08-01: *"your scheduler must have a link with Postgres for
editing, and allow writing and modifying at what moment it executes, and that
must be linked with the admin, and accessible by MCP."*

WHAT WAS WRONG. The moment lived in an environment variable
(`SCHEDULER_NIGHTLY_HOUR`, default 2, `SCHEDULER_TIMEZONE`, default
Europe/Paris): one hour, one timezone, for every project on the platform.
Meanwhile `app.datastream_schedule_state.next_run_at` was computed and written at
activation and read by NOBODY -- the string did not appear once in
`core/scheduler.py`. The column that should carry "when" existed, was joined by
the dispatch query, and was ignored.

WHAT THIS CHANGES. `next_run_at` is now authoritative for eligibility, so writing
an instant into Postgres IS scheduling. These tools are the model's door onto
that; the REST seam (`PATCH /api/datastreams/{id}`) is the console's. Both write
the same row, which is the point -- there is one schedule, not three.

TWO DIMENSIONS, AND THEY ARE NOT THE SAME KNOB (AI-118):
  * `cadence`  -- how OFTEN a run happens (`schedule_mode`: nightly, weekly,
    hourly, manual). `weekly` was refused by the CHECK constraint when this
    paragraph was first written; migration 204 added it.
  * `window`   -- how much history EACH run fetches (`date_window_days`). Pulling
    seven days every night to rewrite the week is expressible and correct: the
    raw zone is append-only and staging supersedes by `pull_id`, so exactly one
    row per business key survives.

AND A THIRD, ADDED BY STORY 57.8, WHICH IS NEITHER OF THEM:
  * `arrival_hour` -- at what hour of the project's local day the daily pull is
    expected to ARRIVE. It does not change how often a run happens and it does
    not change how much it fetches; it names the moment. It only applies to the
    cadences that run once per period (`nightly`, `weekly`): an intraday cadence
    re-pulls the accumulating day and has no single moment of arrival.

AND A FOURTH, WHICH DECIDES WHETHER ANY OF THE OTHER THREE MEANS ANYTHING
(lot D1, issue #68). `enabled` was READ by this module and put on the payload
from the first day, and no door ever wrote it and no screen ever drew it. The
consequence was measured on the live base: a person could set a frequency, an
arrival hour, a retrieval window, an extraction offset and a next run on a
Datastream whose `enabled` is FALSE, and nothing anywhere said it would never
run. The dispatcher's own predicate is the definition -- `_dispatch_nightly_
datastreams` selects `d.enabled = TRUE AND d.lifecycle_state = 'active'` -- so
two columns, not one, decide whether a schedule is armed. This module derives
that composite ONCE, as `run_state`, so the console, the seam and the model all
say the same word about the same row instead of each recombining two booleans.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

#: The cadences `app.datastreams.schedule_mode` accepts. Mirrors the CHECK
#: constraint rather than restating a looser list: a value this tool accepts and
#: the database refuses would surface as an opaque write error to the model.
CADENCES = ("nightly", "weekly", "hourly", "manual")

#: The cadences an arrival hour means something for (A3). An hourly Datastream
#: runs every hour and a manual one runs when asked: neither has an hour to name.
ARRIVAL_HOUR_CADENCES = ("nightly", "weekly")

#: What a failed pull leads to, per cadence. Story 57.8, arbitrages A4 and A5:
#: exactly ONE extra attempt, at the next hour, for the cadences that run once a
#: period. Said out loud rather than implied, because a screen that promises a
#: retry a dispatcher never makes is worse than a screen that promises nothing.
_ON_FAILURE = {
    "nightly": "retry_at_next_hour",
    "weekly": "retry_at_next_hour",
    "hourly": "next_hourly_run",
    "manual": "nothing_is_retried",
}

#: The refusal `set_schedule` makes for any write that needs the schedule state
#: row activation creates. One message, one meaning: `next_run_at` and
#: `arrival_hour` both depend on that row existing.
NOT_ACTIVATED = (
    "this Datastream has no schedule state for its current plan version -- it has "
    "not been activated, so there is no run to move. Activate it first, or set the "
    "cadence only."
)

#: The four words this module uses for "will this Datastream run". DERIVED from
#: `enabled` and `lifecycle_state`, never stored: the pair is what the dispatcher
#: reads, and a stored third column would be free to disagree with both.
RUN_STATES = ("running", "paused", "not_activated", "archived")

#: Arming is refused on a Datastream nobody activated. `publish_activate_mutation`
#: (`core/datastream_activation.py`) is the single writer that sets
#: `lifecycle_state='active'` together with `enabled=TRUE`, and it does it against
#: a reviewed execution, a plan version and a mapping version. Setting `enabled`
#: alone here would produce a row that READS armed and that the dispatcher skips
#: anyway on `d.lifecycle_state = 'active'` -- a screen lying about a clock, which
#: is the exact defect this lot exists to remove.
NOT_ACTIVATED_FOR_RUNNING = (
    "this Datastream has never been activated, so there is nothing to start or "
    "stop. Publishing it from the setup wizard is what activates it, and "
    "activation starts it."
)

#: Archiving sets `enabled = FALSE` and `archived_at` (`core/datastreams.py`,
#: soft archive). Re-arming through this door would restart a flow somebody
#: retired without ever passing through the gesture that retires it.
ARCHIVED_CANNOT_RUN = (
    "this Datastream is archived. Restoring it is what makes it runnable again; "
    "its schedule cannot be started from here."
)


def _run_state(*, enabled, lifecycle_state, archived_at) -> str:
    """The one derivation of "will this Datastream run", shared by three doors."""
    if archived_at is not None:
        return "archived"
    if lifecycle_state != "active":
        return "not_activated"
    return "running" if enabled else "paused"


class _Unset:
    """"Not mentioned" — which is a different statement from "cleared".

    `arrival_hour=None` has to mean ERASE, because a person removing an hour from
    the console is making a decision and the surface tells them so before it
    writes. An optional argument defaulting to `None` can express one of the two
    meanings, never both — and the one it silently dropped was the erasure: the
    console announced "06:00 → not set", omitted the key, and reported success
    while the row kept 06:00.
    """

    def __repr__(self) -> str:  # pragma: no cover -- diagnostics only
        return "UNSET"


#: The default for `set_schedule(arrival_hour=...)`. Exported because the REST
#: seam and the MCP tool both have to say "the caller did not mention this".
UNSET = _Unset()


def _validated_arrival_hour(value) -> int:
    hour = int(value)
    if hour < 0 or hour > 23:
        raise ValueError(
            "arrival_hour must be an hour of the project's local day, between 0 and 23"
        )
    return hour


def read_schedule(conn, *, project_id: str, datastream_id: str) -> dict | None:
    """Return the full schedule of one Datastream, or None when unknown."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.schedule_mode, d.enabled, d.date_window_days, d.refetch_days,
                   d.window_offset_days, d.lifecycle_state, s.next_run_at,
                   s.last_committed_watermark,
                   (SELECT max(j.completed_at) FROM app.pull_jobs j
                     WHERE j.datastream_id = d.id AND j.state = 'done'),
                   d.arrival_hour_local, s.retry_count,
                   (SELECT pp.reporting_timezone FROM app.project_preferences pp
                     WHERE pp.project_id = d.project_id),
                   d.archived_at
            FROM app.datastreams d
            LEFT JOIN app.datastream_schedule_state s
                   ON s.plan_version_id = d.current_plan_version_id
                  AND s.datastream_id = d.id AND s.project_id = d.project_id
            WHERE d.id = %s AND d.project_id = %s
            """,
            (datastream_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    (mode, enabled, window, refetch, window_offset, lifecycle, next_run, watermark, last_run,
     arrival_hour, retry_count, project_zone, archived_at) = row
    # THE NUMBER THE SCREEN SHOWS IS THE NUMBER THE DISPATCHER FETCHES. This read
    # `window if window else (refetch if refetch else 3)` -- the same precedence,
    # re-typed, MINUS the cadence floor (AI-217). So a weekly Datastream carrying
    # a three-day window was dispatched with seven days and reported here as
    # three, and nothing on the screen could explain the four days it did not
    # account for. Resolved by the dispatcher's own resolver, cadence included.
    from core.pull_window import resolve_window_days  # noqa: PLC0415

    _length = resolve_window_days(
        {"date_window_days": window, "refetch_days": refetch}, cadence=mode
    )
    effective_window = _length.days
    effective_offset = window_offset if window_offset is not None else 1
    # The zone the arrival hour is READ IN, and where it came from. An hour
    # without its zone is not a moment, and the console must not resolve it
    # against the reader's browser -- two people in two countries would see two
    # different schedules for one row. Mirrors `scheduler.project_timezone`
    # exactly, including its deployment fallback, so the screen names the zone
    # the dispatcher will actually use.
    timezone_name = str(project_zone) if project_zone else os.environ.get(
        "SCHEDULER_TIMEZONE", "Europe/Paris"
    )
    return {
        "datastream_id": datastream_id,
        "cadence": mode,
        "enabled": bool(enabled),
        "lifecycle_state": lifecycle,
        "archived": archived_at is not None,
        # The composite the dispatcher actually reads, derived once. `enabled`
        # and `lifecycle_state` both stay on the payload -- flattening a draft
        # into a paused Datastream would lose the only fact that tells the two
        # apart, and they take different gestures to repair.
        "run_state": _run_state(
            enabled=enabled, lifecycle_state=lifecycle, archived_at=archived_at
        ),
        "window_days": effective_window,
        "window_offset_days": effective_offset,
        # Read from the resolver rather than re-derived, so the origin and the
        # number can never name two different rungs.
        "window_source": {
            "date_window_days": "date_window_days",
            "refetch_days": "refetch_days (legacy)",
            "defensive_default": "default",
        }[_length.source],
        # NAMED when the cadence widened it: a person who set three days and is
        # shown seven is owed the reason, on the screen, not in a log.
        "window_widened_for_cadence": _length.widened_for,
        # NULL stays NULL. `0` is a legal arrival hour (local midnight), so
        # answering `0` for "nobody chose one" would erase both statements and
        # leave the console unable to say which it is looking at.
        "arrival_hour_local": int(arrival_hour) if arrival_hour is not None else None,
        "arrival_hour_source": "datastream" if arrival_hour is not None else "unset",
        "timezone": timezone_name,
        "timezone_source": "project_preference" if project_zone else "deployment default",
        "on_failure": _ON_FAILURE.get(mode, "nothing_is_retried"),
        "retry_count": int(retry_count) if retry_count is not None else 0,
        "next_run_at": next_run.isoformat() if next_run else None,
        "last_run_at": last_run.isoformat() if last_run else None,
        "never_ran": last_run is None,
        "last_committed_watermark": (
            watermark.isoformat() if hasattr(watermark, "isoformat") else watermark
        ),
        "runs_when": (
            "on demand only" if mode == "manual" else
            (f"when next_run_at is reached, then every "
             f"{'hour' if mode == 'hourly' else ('week' if mode == 'weekly' else 'day')}")
            if next_run else
            f"at the platform {mode} tick (next_run_at unset)"
        ),
    }


def set_schedule(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    cadence: str | None = None,
    window_days: int | None = None,
    window_offset_days: int | None = None,
    next_run_at: str | None = None,
    arrival_hour: int | None | _Unset = UNSET,
    enabled: bool | None = None,
    identity: str = "system",
) -> dict:
    """Write the schedule. Returns the resulting schedule, or raises ValueError.

    Every argument is optional and only what is given is written: a caller that
    wants to move the next run without touching the cadence must not have to
    restate the cadence and risk overwriting it with a stale value.

    `enabled` is the fourth dimension (lot D1) and the only one with a
    precondition outside this module: it may only move a Datastream that
    activation has already made `active`, and starting one consumes the same
    trial allowance creating one does.
    """
    if cadence is not None and cadence not in CADENCES:
        raise ValueError(
            f"cadence must be one of {', '.join(CADENCES)}. "
            "Use window_days (e.g. 7) to set lookback history depth."
        )
    if window_days is not None and (int(window_days) < 1 or int(window_days) > 365):
        raise ValueError("window_days must be between 1 and 365")
    if window_offset_days is not None and not 1 <= int(window_offset_days) <= 90:
        raise ValueError("window_offset_days must be between 1 and 90")
    if enabled is not None and not isinstance(enabled, bool):
        raise ValueError(
            "enabled must be true (start collecting) or false (stop collecting)"
        )
    # `UNSET` means the caller said nothing; `None` means the caller cleared it.
    arrival_hour_given = not isinstance(arrival_hour, _Unset)
    if arrival_hour_given and arrival_hour is not None:
        arrival_hour = _validated_arrival_hour(arrival_hour)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM app.datastreams WHERE id = %s AND project_id = %s",
            (datastream_id, project_id),
        )
        if cur.fetchone() is None:
            raise ValueError("unknown datastream for this project")

        # STARTING AND STOPPING IS NOT A SETTING LIKE THE OTHERS (lot D1).
        # Asked in its own SELECT, and only when the caller mentions it, so a
        # call about the cadence keeps the shape -- and the cost -- it had.
        if enabled is not None:
            cur.execute(
                "SELECT enabled, lifecycle_state, archived_at FROM app.datastreams "
                "WHERE id = %s AND project_id = %s",
                (datastream_id, project_id),
            )
            already_enabled, lifecycle_state, archived_at = cur.fetchone()
            if archived_at is not None:
                raise ValueError(ARCHIVED_CANNOT_RUN)
            if lifecycle_state != "active":
                # THIS DOOR CANNOT ACTIVATE. `publish_activate_mutation` writes
                # `lifecycle_state='active'` and `enabled=TRUE` together against
                # a reviewed execution; a schedule write that set `enabled` on a
                # draft would be a second activation authority, and a dishonest
                # one -- the dispatcher would still skip the row.
                raise ValueError(NOT_ACTIVATED_FOR_RUNNING)
            if enabled and not already_enabled:
                # The org's trial allowance counts `enabled = TRUE` rows
                # (`trial_enforcement._count_active_datastreams`). Creating the
                # fourth is refused; re-arming a third one that was paused is
                # the same act and is refused by the same guard, which raises
                # `TrialDatastreamLimitError` -- caught as a 409 by the seam,
                # never flattened into this module's ValueError.
                from core.trial_enforcement import check_datastream_limit  # noqa: PLC0415

                check_datastream_limit(project_id, conn, identity=identity)

        # BEFORE any write, not after. An arrival hour is only ever read by
        # `scheduler._advance_next_run`, which looks exclusively at the schedule
        # state row activation creates: writing the hour onto a Datastream that
        # has no such row reports success for a setting nothing will honour.
        # Checked first because this function is handed a connection it does not
        # own -- refusing after the UPDATE would leave the outcome to whether
        # the caller happens to roll back.
        if arrival_hour_given:
            cur.execute(
                """
                SELECT 1 FROM app.datastream_schedule_state ss
                  JOIN app.datastreams d
                    ON d.id = ss.datastream_id AND d.project_id = ss.project_id
                 WHERE d.id = %s AND d.project_id = %s
                   AND ss.plan_version_id = d.current_plan_version_id
                """,
                (datastream_id, project_id),
            )
            if cur.fetchone() is None:
                raise ValueError(NOT_ACTIVATED)

        sets, params = [], []
        if cadence is not None:
            sets.append("schedule_mode = %s")
            params.append(cadence)
        if enabled is not None:
            sets.append("enabled = %s")
            params.append(bool(enabled))
        if arrival_hour_given:
            # `None` is written through: clearing the hour returns the row to
            # local midnight, which is a choice a person is allowed to make.
            sets.append("arrival_hour_local = %s")
            params.append(arrival_hour)
        if window_days is not None:
            sets.append("date_window_days = %s")
            params.append(int(window_days))
        if window_offset_days is not None:
            sets.append("window_offset_days = %s")
            params.append(int(window_offset_days))
        if sets:
            params += [datastream_id, project_id]
            cur.execute(
                f"UPDATE app.datastreams SET {', '.join(sets)}, updated_at = NOW() "  # noqa: S608
                "WHERE id = %s AND project_id = %s",
                params,
            )

        if next_run_at is not None:
            # UPDATE, never INSERT. The row is keyed by plan_version_id and is
            # created by activation together with the plan version it belongs to.
            # Inventing one here would mean choosing a plan version on the
            # model's behalf -- and a schedule attached to no version is a
            # schedule the dispatcher will never see.
            cur.execute(
                """
                UPDATE app.datastream_schedule_state ss
                SET next_run_at = %s::timestamptz, updated_at = NOW()
                FROM app.datastreams d
                WHERE d.id = %s AND d.project_id = %s
                  AND ss.plan_version_id = d.current_plan_version_id
                  AND ss.datastream_id = d.id AND ss.project_id = d.project_id
                """,
                (next_run_at, datastream_id, project_id),
            )
            if cur.rowcount != 1:
                raise ValueError(NOT_ACTIVATED)
    conn.commit()
    return read_schedule(conn, project_id=project_id, datastream_id=datastream_id)


def get_datastream_schedule(project_id: str, datastream_id: str):
    """Lit QUAND un Datastream s'execute : cadence, fenetre, prochaine execution.

    Rend aussi `window_source`, qui dit d'ou vient la fenetre effective --
    `date_window_days` choisi, `refetch_days` herite, ou le defaut -- ainsi
    que `arrival_hour_local` (l'heure locale d'arrivee choisie, `null` quand
    personne n'en a choisi), `timezone` / `timezone_source` (le fuseau dans
    lequel cette heure se lit), `on_failure` et `retry_count`.

    Et `run_state`, qui dit si cet horaire tourne : `running` (arme),
    `paused` (configure et arrete), `not_activated` (jamais publie, donc
    aucun horaire a armer) ou `archived`. Un horaire complet sur un
    Datastream `paused` ne declenchera jamais rien.
    """
    from core.db import request_connection  # noqa: PLC0415
    from core.mcp_scope import caller_identity, refuse_unless_project_scope  # noqa: PLC0415

    # Story 53.1. Trouve par la garde d'AC7 le jour ou elle a cesse de ne
    # lire que `main.py` : cet outil prenait un `project_id` et n'a jamais
    # demande si l'appelant y avait droit.
    identity = caller_identity()
    refuse_unless_project_scope(project_id, identity)

    with request_connection(identity) as conn:
        result = read_schedule(conn, project_id=project_id, datastream_id=datastream_id)
    if result is None:
        return {"error": "unknown_datastream", "datastream_id": datastream_id}
    return result


def set_datastream_schedule(
    project_id: str,
    datastream_id: str,
    cadence: str | None = None,
    window_days: int | None = None,
    window_offset_days: int | None = None,
    next_run_at: str | None = None,
    arrival_hour: int | None = None,
    clear_arrival_hour: bool = False,
    enabled: bool | None = None,
):
    """Modifie QUAND un Datastream s'execute. Ecrit dans PostgreSQL.

    - `enabled` : `true` demarre la collecte sur cet horaire, `false`
      l'arrete. Un Datastream arrete garde tous ses reglages et n'execute
      rien. Refuse tant que le Datastream n'est pas active (la publication
      par l'assistant est ce qui l'active) et sur un Datastream archive ;
      le demarrer consomme l'allocation d'essai de l'organisation comme
      une creation.
    - `cadence` : `nightly`, `weekly`, `hourly` ou `manual`.
    - `window_days` : combien de jours d'historique CHAQUE execution recupere.
    - `window_offset_days` : decalage de la fenetre en jours par rapport a aujourd'hui
      (1 = hier/J-1, 3 = J-3 pour les sources en retard d'indexation).
    - `next_run_at` : instant ISO-8601 de la prochaine execution.
    - `arrival_hour` : heure (0-23) du jour LOCAL du projet a laquelle le tirage
      doit arriver. S'applique aux cadences `nightly` et `weekly` uniquement ;
      elle survit aux echecs et aux changements d'heure, ce que `next_run_at`
      ne fait pas.
    - `clear_arrival_hour` : efface l'heure d'arrivee choisie ; le tirage
      revient a minuit local. Un argument distinct parce qu'omettre
      `arrival_hour` et l'effacer sont deux demandes differentes, et un seul
      `None` ne peut pas dire les deux.

    Ne passer que ce qu'on veut changer.
    """
    from core.db import request_connection  # noqa: PLC0415
    from core.mcp_scope import caller_identity, refuse_unless_project_scope  # noqa: PLC0415

    # Story 53.1, AC5 : une ECRITURE, donc `edit`. Deplacer l'heure du
    # tirage d'un Datastream voisin depense son quota fournisseur et fait
    # bouger des donnees dont quelqu'un d'autre repond.
    identity = caller_identity()
    refuse_unless_project_scope(project_id, identity, minimum_capability="edit")

    if clear_arrival_hour:
        requested_hour: object = None
    elif arrival_hour is not None:
        requested_hour = arrival_hour
    else:
        requested_hour = UNSET

    try:
        with request_connection(identity) as conn:
            return set_schedule(
                conn,
                project_id=project_id,
                datastream_id=datastream_id,
                cadence=cadence,
                window_days=window_days,
                window_offset_days=window_offset_days,
                next_run_at=next_run_at,
                arrival_hour=requested_hour,
                enabled=enabled,
                identity=str(identity),
            )
    except ValueError as exc:
        return {"error": "invalid_schedule", "message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        # The trial allowance refusal is typed and carries its own numbers.
        # Letting it escape as a stack trace would tell the model "the tool
        # broke" about a governance rule it can explain to a person.
        from core.trial_enforcement import TrialDatastreamLimitError  # noqa: PLC0415

        if isinstance(exc, TrialDatastreamLimitError):
            return {"error": exc.code, **exc.to_dict()}
        raise


def register(mcp) -> None:
    """Register the schedule tools (AI-119).

    `get_datastream_schedule` is a read. `set_datastream_schedule` is a write and
    is declared `confirmation_mode="human"`: changing when a pipeline runs -- or
    how much history it rewrites -- spends provider quota and moves data a person
    is accountable for. It is not a preference toggle.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp, get_datastream_schedule,
        profile="operations", effect="read",
        data_class="operational", confirmation_mode="none",
    )
    register_profiled(
        mcp, set_datastream_schedule,
        profile="operations", effect="confirmed_write",
        data_class="operational", confirmation_mode="human",
    )
