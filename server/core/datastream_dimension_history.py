"""What each collected day of a Datastream was ASKED to carry -- amendment 14.

RATIFIED TARGET: `datastream-workbench-and-wizard.md`, section « Choisir ce qu'on
collecte, et ce que ce choix doit au passé », amendment 14 (ratified 2026-08-11):
« Ajouter une dimension déclare une DETTE D'HISTORIQUE, et l'écran la nomme ».

WHERE THE PER-DIMENSION TRUTH LIVES, MEASURED 2026-08-12 BEFORE ANY LINE OF THIS
MODULE WAS WRITTEN. The amendment asks for a measure « lue de ce que l'extrait a
réellement rendu ». That reading does not exist in this repository, and the whole
value of this module is that it says so instead of composing a number that looks
like it:

  * `app.pull_verifications` (migration 007) counts `expected_rows`,
    `actual_rows` and a verdict. It records no column, no field and no schema.
  * No table anywhere records the columns a pull brought back:
    `grep -rn "observed_columns|landed_columns|column_names|schema_snapshot"`
    over `server/` and `infra/` returns only local variables of two file-import
    modules, never a persisted set.
  * `core/collected_mapped_reader.py` reads the column list of the SHARED raw
    relation -- one list for the whole relation, not one per day. A column added
    to it today exists, as NULL, on every past row, and a NULL dimension is not
    distinguishable from an uncollected one.

WHAT DOES EXIST, AND IT IS A REAL PER-DAY FACT. Every plan version is immutable
(`app.datastream_plan_versions`, migration 030) and carries its own
`source.selection.dimensions`; every run pins the version it executed
(`app.datastream_executions.plan_version_id`, migration 042, `NOT NULL`); and
every collected window names its run (`app.pull_jobs.execution_id`, migration
218). So the chain

    day -> covering pull -> execution -> plan version -> declared dimensions

answers « what was this day asked for », per day, from the run and NEVER from the
active plan. That is a different fact from « what came back », and this module
publishes the difference under `basis` and `unmeasurable` rather than letting a
screen blur the two.

AND ONE CASE IS EXACT WITH NO CHAIN AT ALL. A dimension that NO plan version has
ever declared cannot have been collected on any day, whatever the runs did. That
is the case of the gesture amendment 14 is about -- a dimension being added for
the first time -- and it is proved by the plan ledger alone, which is why the
common answer here is a measurement and not an estimate.

THE PROVIDER BOUND IS READ, AND IT IS EMPTY. Amendment 14: « `max_provider_
backfill_days` porte déjà cette borne et personne ne la lit pour cette question ».
It is read here. Measured 2026-08-12 over `server/modules/*/manifest.json`: 140
declared reports, **0** of them declare the key. So recoverability is published as
UNKNOWN -- `null`, with `backfill_bound_evidence: "unavailable"` -- and never as
"recoverable". Assuming a bound nobody declared would be the invention this whole
review exists to remove.

THE DAY-LEVEL ANSWER IS SHARED, THE PER-DIMENSION COUNT IS NOT COMPUTED HERE.
A connector may declare 765 dimensions (GAM); publishing one day list per
dimension would be 765 x 92 entries for a panel that needs the count of the two
or three a person just ticked. So this module publishes the days once, with the
plan version each resolves to, and the dimension lists of the versions actually
referenced. Intersecting the two is a set membership test with no freedom to
invent, and `ui/admin/src/datastreams/workbench/dimensionDebt.ts` is where it is
done and tested.

COST: three statements, whatever the width of the window and whatever the number
of dimensions -- the ledger (which is itself one batch query), one lookup of the
referenced executions, one read of this Datastream's plan versions.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any

from core.cache_warehouse import SAMPLE_MAX_DAYS

logger = logging.getLogger(__name__)

#: The window this measure covers, in days ending yesterday. It is the product's
#: existing ceiling for a bounded day window (`cache_warehouse.SAMPLE_MAX_DAYS`,
#: the bound of the daily breakdown), imported rather than re-chosen so the debt
#: and the day grid speak of the same history.
WINDOW_DAYS = SAMPLE_MAX_DAYS

#: The window ends YESTERDAY and not today. A day is collected once it has ended;
#: counting today would add one certain "missing" day to every debt, on every
#: Datastream, forever -- a number that is wrong by construction.
_WINDOW_ENDS_DAYS_AGO = 1

#: What a day of the window is, for this question. Only `landed` can owe a
#: dimension: the others carry no row that could have been missing one, and
#: proposing to re-collect them for a dimension would spend for nothing.
DAY_LANDED = "landed"
DAY_LANDED_EMPTY = "landed_empty"
DAY_NOT_LANDED = "not_landed"
DAY_IN_FLIGHT = "in_flight"
DAY_NEVER_COLLECTED = "never_collected"

_STATUS_TO_DAY_STATE = {
    "ok": DAY_LANDED,
    "partial": DAY_LANDED,
    "empty": DAY_LANDED_EMPTY,
    "failed": DAY_NOT_LANDED,
    "running": DAY_IN_FLIGHT,
    "never_fetched": DAY_NEVER_COLLECTED,
}

#: What this module measures, said in the payload so no screen may over-claim it.
BASIS = (
    "what the plan version each run executed under asked the provider for, read "
    "from the run and never from the plan in force today"
)

#: And what it does not, said in the same breath. This is the sentence amendment
#: 14 makes possible: an absence stated beats a day count nobody measured.
UNMEASURABLE = (
    "what a provider actually returned for a past day is recorded nowhere: a "
    "pull is verified by its row count, not by its columns, so a day asked for a "
    "dimension is not proof the day received one"
)

#: Why there is no window to measure. Never rendered as "no dimension is missing".
NO_HISTORY = "no_history"
NOT_APPLICABLE = "not_applicable"
UNREADABLE = "unreadable"
MEASURED = "measured"


def _refetch_path(project_id: str, datastream_id: str) -> str | None:
    """The re-collection address, or `None` when either half is missing.

    The same rule the retired `geographic_change._refetch_path` held, and the
    same reason:
    `/api/projects/None/...` looks like an address and answers 404, and a caller
    holding a string cannot tell it from a real one.
    """
    if not project_id or not datastream_id:
        return None
    return f"/api/projects/{project_id}/datastreams/{datastream_id}/refetch"


def _selection_dimensions(payload: Any) -> list[str]:
    """`source.selection.dimensions` of one plan version, or an empty list."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return []
    if not isinstance(payload, dict):
        return []
    source = payload.get("source")
    if not isinstance(source, dict):
        return []
    selection = source.get("selection")
    if not isinstance(selection, dict):
        return []
    dimensions = selection.get("dimensions")
    if not isinstance(dimensions, list):
        return []
    return [str(item) for item in dimensions if isinstance(item, (str, int))]


def _plan_versions(conn, project_id: str, datastream_id: str) -> list[dict[str, Any]]:
    """Every version of this plan, oldest first, with what it declared.

    Ordered by `version_number` and not by `created_at`: the number is the
    ledger's own order (`uq_datastream_plan_version`), and two versions minted in
    the same transaction would sort arbitrarily by timestamp.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, version_number, created_at, normalized_payload
                 FROM app.datastream_plan_versions
                WHERE project_id = %s AND datastream_id = %s
                ORDER BY version_number ASC""",
            (project_id, datastream_id),
        )
        rows = cur.fetchall()
    return [
        {
            "id": str(row[0]),
            "version_number": int(row[1]),
            "created_at": row[2].isoformat() if row[2] is not None else None,
            "dimensions": _selection_dimensions(row[3]),
        }
        for row in rows
    ]


def _executions_plan_versions(
    conn, project_id: str, datastream_id: str, execution_ids: list[str]
) -> dict[str, str]:
    """`execution_id -> plan_version_id`, scoped to this project AND stream.

    Scoped on both because `pull_jobs.execution_id` carries no composite key to
    the pair: reading it unscoped would let a run of another Datastream name the
    dimensions of this one's day.
    """
    if not execution_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, plan_version_id
                 FROM app.datastream_executions
                WHERE project_id = %s AND datastream_id = %s AND id = ANY(%s)""",
            (project_id, datastream_id, list(execution_ids)),
        )
        return {str(row[0]): str(row[1]) for row in cur.fetchall()}


def read_dimension_history(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    source_kind: str | None,
    max_provider_backfill_days: Any = None,
    window_days: int = WINDOW_DAYS,
    today: date | None = None,
) -> dict[str, Any]:
    """What each day of the bounded window was asked to carry.

    The returned `state` is what the screen renders on:

      `not_applicable` -- this mode asks a provider for nothing, so adding a
                          field to it owes no history.
      `no_history`     -- nothing has ever landed in the window: there is no
                          past for a new dimension to be missing from.
      `unreadable`     -- the ledger or the plan versions could not be read.
                          NEVER reported as "no day is missing".
      `measured`       -- the days, and the plan version each one resolves to.
    """
    today = today or date.today()
    window_days = max(1, min(int(window_days or WINDOW_DAYS), WINDOW_DAYS))
    window_to = today - timedelta(days=_WINDOW_ENDS_DAYS_AGO)
    window_from = window_to - timedelta(days=window_days - 1)
    window = {
        "from": window_from.isoformat(),
        "to": window_to.isoformat(),
        "days": window_days,
    }

    bound = (
        int(max_provider_backfill_days)
        if isinstance(max_provider_backfill_days, int)
        and not isinstance(max_provider_backfill_days, bool)
        and max_provider_backfill_days > 0
        else None
    )
    # Measured 2026-08-12: 0 of the 140 declared reports fill this key, so this
    # branch is the live one. `None` means UNKNOWN and is published as such --
    # the screen says how far back a re-collection may reach cannot be known,
    # rather than promising every day back or refusing every day.
    recovery = {
        "max_provider_backfill_days": bound,
        "backfill_bound_evidence": "provider_capability" if bound else "unavailable",
        "earliest_recoverable": (
            (today - timedelta(days=bound)).isoformat() if bound else None
        ),
    }

    base = {
        "window": window,
        "basis": BASIS,
        "unmeasurable": UNMEASURABLE,
        "refetch_path": _refetch_path(project_id, datastream_id),
        **recovery,
    }

    if str(source_kind or "").strip() != "connector_pull":
        return {
            **base,
            "state": NOT_APPLICABLE,
            "reason": (
                "This Datastream asks a provider for nothing, so a column added "
                "to it owes no past pull."
            ),
        }

    try:
        from core.extract_ledger import get_extract_ledger  # noqa: PLC0415

        ledger = get_extract_ledger(
            datastream_id, window["from"], window["to"], conn
        )
        versions = _plan_versions(conn, project_id, datastream_id)
    except Exception as exc:  # noqa: BLE001 -- an unreadable history is not "no debt"
        logger.warning(
            "datastream_dimension_history: unreadable ds=%s: %s", datastream_id, exc
        )
        return {
            **base,
            "state": UNREADABLE,
            "reason": (
                "The collection history of this Datastream could not be read, so "
                "what the days already collected carry is unknown."
            ),
        }

    days: list[dict[str, Any]] = []
    execution_ids: list[str] = []
    for entry in ledger:
        state = _STATUS_TO_DAY_STATE.get(str(entry.get("status")), DAY_NEVER_COLLECTED)
        execution_id = entry.get("execution_id")
        if state == DAY_LANDED and execution_id:
            execution_ids.append(str(execution_id))
        days.append(
            {
                "date": str(entry.get("date")),
                "state": state,
                "execution_id": str(execution_id) if execution_id else None,
                # When the run is unknown, WHEN the day landed is still known, and
                # it is enough to prove a dimension absent: a version declaring it
                # that did not exist yet cannot have been executed.
                "landed_at": entry.get("loaded_at"),
                "plan_version_id": None,
            }
        )

    landed = [day for day in days if day["state"] == DAY_LANDED]
    if not landed:
        return {
            **base,
            "state": NO_HISTORY,
            "reason": (
                "No day of this window has landed a row, so there is no history "
                "for a new dimension to be missing from."
            ),
            "counts": _counts(days),
        }

    try:
        by_execution = _executions_plan_versions(
            conn, project_id, datastream_id, sorted(set(execution_ids))
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "datastream_dimension_history: executions unreadable ds=%s: %s",
            datastream_id,
            exc,
        )
        by_execution = {}
    for day in days:
        if day["execution_id"]:
            day["plan_version_id"] = by_execution.get(day["execution_id"])

    # Only the versions a day actually resolves to. Publishing all of them would
    # grow with the ledger of a long-lived plan for no reader.
    referenced = {day["plan_version_id"] for day in days if day["plan_version_id"]}
    return {
        **base,
        "state": MEASURED,
        "days": days,
        "counts": _counts(days),
        "plan_dimensions": {
            version["id"]: version["dimensions"]
            for version in versions
            if version["id"] in referenced
        },
        # WHEN each dimension entered the plan, and the fact that carries the
        # exact answer: a dimension absent from this map was declared by NO
        # version, so no day can have been asked for it.
        "first_declared": _first_declared(versions),
    }


def _counts(days: list[dict[str, Any]]) -> dict[str, int]:
    """How many days of each state. Five buckets, never one "missing" total."""
    counts = {
        DAY_LANDED: 0,
        DAY_LANDED_EMPTY: 0,
        DAY_NOT_LANDED: 0,
        DAY_IN_FLIGHT: 0,
        DAY_NEVER_COLLECTED: 0,
    }
    for day in days:
        counts[day["state"]] = counts.get(day["state"], 0) + 1
    return counts


def _first_declared(versions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The FIRST plan version that declared each dimension, and when.

    The versions arrive oldest first, so the first sighting wins. A dimension
    later removed and re-added keeps its first entry: the question this answers
    is "could a day this old have carried it at all", and the answer for every
    day older than the first declaration is no, whatever happened afterwards.
    """
    first: dict[str, dict[str, Any]] = {}
    for version in versions:
        for field in version["dimensions"]:
            if field not in first:
                first[field] = {
                    "plan_version_id": version["id"],
                    "version_number": version["version_number"],
                    "created_at": version["created_at"],
                }
    return first
