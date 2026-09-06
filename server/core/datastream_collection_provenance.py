"""WHEN A ROW WAS COLLECTED, read where the row is -- never composed for a screen.

Jean, 2026-08-12: « ou par exemple simplement ajouter la date de l'extraction dans
le report ». Measured the same day, and the measurement is the whole design of
this module:

MEASURE 1 -- THE INSTANT IS ALREADY LANDED, ON EVERY ROW.
    38 of the 39 `server/modules/*/connector.py` stamp `loaded_at` onto the rows
    they land (`github` is the one that does not). It is one
    `datetime.now(timezone.utc)` per `pull()` call, written identically onto
    every row of that pull.

MEASURE 2 -- IT SURVIVES TO THE MART.
    47 of the 54 staging models carry it, and `dbt/models/marts/fact_daily_kpi.sql`
    selects `MAX(loaded_at) AS loaded_at` in each of its 34 blocks. Read on a
    built mart the same day: 6861 rows, 6861 with `loaded_at` not null.

MEASURE 3 -- ITS GRAIN IS THE PULL, NOT THE ROW, AND THAT IS A WEAKER PROMISE.
    On that same mart, per connector, `count(distinct loaded_at)` equalled
    `count(distinct pull_id)` exactly -- 1/1, 2/2, 3/3. So a row does not carry
    the moment IT was read; it carries the moment ITS COLLECTION ran, and every
    row of one collection shares it. This module publishes that ratio as a
    measurement on every answer rather than asserting the stronger claim, because
    "when this row was collected" and "when the collection that brought this row
    ran" are two different sentences and only the second is true.

MEASURE 4 -- IT CANNOT BE SELECTED, AND OFFERING A CHECKBOX WOULD BE A LIE.
    `core/datastream_intents.py` (the `unknown_report_field` issue) refuses any
    `source.selection` field the pinned report does not declare, and
    `exact_bundle_required` beside it refuses any selection that is not the
    COMPLETE declared bundle -- 115 of the 140 declared reports. `loaded_at` is
    declared by no manifest, so ticking it into a selection would compose a plan
    the validator refuses. There is nothing to add: it is already there, on every
    row, unconditionally. The screen's job is to SHOW it and say so.

WHY THIS IS A SEPARATE MODULE FROM `datastream_source_catalogue`.
    That module reads one manifest file and promises no query, so the `Processing`
    tab pays nothing to render its catalogue. This one opens the WAREHOUSE, which
    is a round trip and can be down independently. Keeping them apart is what lets
    the declaration ship intact when the observation cannot be read -- and those
    are two different sentences too.

AD-2: source-agnostic. No provider names, no imports from `server/modules`.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The mart every reading of a Datastream's rows goes through (AD-12). Named
#: from `cache_warehouse` rather than retyped, so this reader cannot drift from
#: the one beside it the day the relation moves.
_MART_RELATION = "fact_daily_kpi"

#: How many real rows are shown. A reading, not an export: enough to see the
#: value and see that it repeats across a collection, bounded so opening a tab
#: never becomes a scan.
_ROW_LIMIT = 5

#: WHY NO CHECKBOX IS OFFERED, in the validator's own terms. Written once here
#: and rendered verbatim, so the screen never invents a softer reason than the
#: refusal that would actually happen.
NOT_SELECTABLE = (
    "No provider declares this field, so it cannot be added to a selection -- a plan "
    "naming a field its report does not declare is refused. It needs no adding: every "
    "row already carries it."
)

#: THE VOCABULARY OF WHAT THE COLLECTION ITSELF WRITES, IN CODE.
#:
#: A catalogue shipped with the product does not belong in a table. These two
#: columns are written by `core/raw_landing.py`'s callers on every landing, not
#: configured per org, so a row in a database would be a copy free to disagree
#: with the connectors that write them.
#:
#: The words are the reader's, not the column's: `loaded_at` is a column name and
#: nobody outside this repository calls a moment that.
COLLECTION_FIELDS: tuple[dict[str, str], ...] = (
    {
        "field_id": "loaded_at",
        "label": "Collected at",
        "description": (
            "The moment the collection that brought this row in finished. Written by "
            "toorow when the row landed -- the provider sent no such value."
        ),
    },
    {
        "field_id": "pull_id",
        "label": "Collection reference",
        "description": (
            "Which collection this row came from. Every row of one collection carries "
            "the same reference, and the same instant beside it."
        ),
    },
)


def declared_collection_fields() -> list[dict[str, Any]]:
    """What the collection writes onto every row, as the screen states it.

    Pure: no manifest, no warehouse, no database. It is the same answer for every
    Datastream that pulls from a connector, because the writer is the landing code
    and not the provider.
    """
    return [
        {**field, "always_collected": True, "selectable": False, "reason": NOT_SELECTABLE}
        for field in COLLECTION_FIELDS
    ]


def _empty(state: str, reason: str) -> dict[str, Any]:
    return {
        "state": state,
        "reason": reason,
        "rows": [],
        "row_count": 0,
        "distinct_instants": None,
        "distinct_collections": None,
        "grain_note": None,
    }


#: One sentence per outcome, and none of them is another's fallback. "The mart
#: never carried this connector" sends a person to the collection; "the warehouse
#: could not be read" sends them nowhere and says so.
_NO_CONNECTOR = (
    "This project's mart has never carried a row for this connector, so there is no "
    "collected row to read an instant from yet."
)
_NO_RELATION = (
    "No mart has been built for this project yet, so no collected row can be read."
)
_NOT_APPLICABLE = (
    "This Datastream pulls from no connector, so nothing here stamps a collection "
    "instant onto its rows."
)


def _grain_note(instants: int, collections: int) -> str:
    """What the two counts MEAN, said from the counts and not from a belief.

    Equal counts are the measured case and the weaker promise: the instant belongs
    to the collection. Fewer instants than collections would mean two collections
    finished on the same recorded moment -- still per collection. MORE instants
    than collections cannot happen while one pull stamps one value, and if it ever
    does this says so rather than printing the sentence that has stopped being
    true.
    """
    if collections <= 0:
        return "No collection has landed a row here yet."
    if instants > collections:
        return (
            f"{instants} distinct instants over {collections} collections: these rows "
            "carry more instants than collections, which this reading cannot explain."
        )
    return (
        f"{instants} distinct instant(s) over {collections} collection(s): the instant "
        "belongs to the collection, not to the row -- every row of one collection "
        "carries the same one."
    )


def read_collection_provenance(
    *,
    project_id: str,
    connector: str,
    source_kind: str | None = None,
) -> dict[str, Any]:
    """Real rows from the mart, carrying the instant their collection landed.

    THE VALUE IS READ WHERE THE ROW IS. `app.pull_jobs` also holds a completion
    time, and joining it to a row would be a different claim wearing this one's
    name: a job's completion is when the WORK ended, `loaded_at` is what was
    WRITTEN onto the row. They can differ, and only one of them is on the row a
    person is looking at.

    TWO STATEMENTS, NEVER ONE PER ROW. The counts answer the grain for the whole
    connector in one `GROUP BY`; the sample takes the newest `_ROW_LIMIT` rows.
    Both are bounded, so the tab costs the same whether the mart holds a thousand
    rows or a billion.

    Returns ``{"state", "reason", "rows", "row_count", "distinct_instants",
    "distinct_collections", "grain_note"}``. Every state is a different sentence:

      `not_applicable`  -- this Datastream pulls from no connector.
      `relation_absent` -- no mart is built for this project.
      `connector_absent`-- the mart exists and has never held this connector.
      `unreadable`      -- the warehouse answered with an error. NEVER "no row".
      `observed`        -- real rows, with the grain they were measured at.
    """
    kind = str(source_kind or "").strip()
    module = str(connector or "").strip()
    if (kind and kind != "connector_pull") or not module:
        return _empty("not_applicable", _NOT_APPLICABLE)

    from core import warehouse  # noqa: PLC0415
    from core.cache_warehouse import (  # noqa: PLC0415
        SampleReadError,
        _sample_relation_columns,
    )

    mode = warehouse._db_mode()
    try:
        columns = _sample_relation_columns(warehouse, mode, project_id, _MART_RELATION)
    except Exception as exc:  # noqa: BLE001 -- a discovery failure is not an absence
        logger.warning("datastream_collection_provenance: discovery failed: %s", exc)
        return _empty("unreadable", f"The warehouse could not be read ({type(exc).__name__}).")
    if not columns:
        return _empty("relation_absent", _NO_RELATION)
    # The mart could be built without the provenance columns -- an older build, or
    # a backend that dropped them. That is not "no row": it is a mart that cannot
    # answer this question, and it says which column it is missing.
    missing = [field["field_id"] for field in COLLECTION_FIELDS if field["field_id"] not in columns]
    if missing:
        return _empty(
            "relation_absent",
            f"The mart of this project carries no {', '.join(missing)} column, so no "
            "collection instant can be read from its rows.",
        )

    try:
        if mode == "duckdb":
            prefix = warehouse._duckdb_mart_prefix(project_id)
            counts = warehouse._query_duckdb(
                "SELECT count(DISTINCT loaded_at) AS instants, "  # noqa: S608
                "count(DISTINCT pull_id) AS collections "
                f"FROM {prefix}{_MART_RELATION} WHERE project_id = ? AND connector = ?",
                [project_id, module],
            )
            sample = warehouse._query_duckdb(
                "SELECT date AS day, metric, breakdown_dimension, loaded_at, pull_id "  # noqa: S608
                f"FROM {prefix}{_MART_RELATION} WHERE project_id = ? AND connector = ? "
                f"ORDER BY loaded_at DESC, date DESC, metric LIMIT {_ROW_LIMIT}",
                [project_id, module],
            )
        elif mode == "bigquery":
            from core import warehouse_tenancy  # noqa: PLC0415

            dataset = warehouse_tenancy.bigquery_marts_dataset(project_id)
            counts = warehouse._query_bigquery(
                "SELECT count(DISTINCT loaded_at) AS instants, "  # noqa: S608
                "count(DISTINCT pull_id) AS collections "
                f"FROM `{dataset}`.{_MART_RELATION} "
                "WHERE project_id = @p0 AND connector = @p1",
                [project_id, module],
            )
            sample = warehouse._query_bigquery(
                "SELECT date AS day, metric, breakdown_dimension, loaded_at, pull_id "  # noqa: S608
                f"FROM `{dataset}`.{_MART_RELATION} "
                "WHERE project_id = @p0 AND connector = @p1 "
                f"ORDER BY loaded_at DESC, date DESC, metric LIMIT {_ROW_LIMIT}",
                [project_id, module],
            )
        else:
            return _empty("unreadable", f"The warehouse mode {mode!r} cannot be read.")
    except SampleReadError as exc:
        logger.warning("datastream_collection_provenance: warehouse refused: %s", exc)
        return _empty("unreadable", "The warehouse could not be read.")
    except Exception as exc:  # noqa: BLE001 -- an error is never served as an absence
        logger.warning("datastream_collection_provenance: read failed: %s", exc)
        return _empty("unreadable", f"The warehouse could not be read ({type(exc).__name__}).")

    head = (counts or [{}])[0]
    instants = int(head.get("instants") or 0)
    collections = int(head.get("collections") or 0)
    if collections == 0 and not sample:
        return _empty("connector_absent", _NO_CONNECTOR)

    return {
        "state": "observed",
        "reason": None,
        "rows": [_row(entry) for entry in sample or []],
        "row_count": len(sample or []),
        "distinct_instants": instants,
        "distinct_collections": collections,
        "grain_note": _grain_note(instants, collections),
    }


def _row(entry: dict[str, Any]) -> dict[str, Any]:
    """One mart row, in the words the screen shows -- values passed through as read."""
    day = entry.get("day")
    return {
        "date": _text(day),
        "metric": entry.get("metric"),
        "breakdown_dimension": entry.get("breakdown_dimension"),
        "loaded_at": _text(entry.get("loaded_at")),
        "pull_id": _text(entry.get("pull_id")),
    }


def _text(value: Any) -> str | None:
    """A value the JSON layer can carry, without reformatting what was read.

    BigQuery hands back `datetime`/`date` objects where DuckDB hands back the
    string the connector wrote. `isoformat()` on the first and the string itself
    on the second is the ONLY transformation applied: no timezone is assumed, no
    display format is chosen here, and nothing is defaulted when the column is
    NULL -- a missing instant stays missing.
    """
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
