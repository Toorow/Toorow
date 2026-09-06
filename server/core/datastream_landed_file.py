"""The last file that ARRIVED on a pushed source, read back -- lot B1.

WHY THIS EXISTS. Amendment 6 of the 2026-08-11 review (« `Preview a sample` ne
prévisualise pas ce qui est arrivé ») names the defect in one sentence: on a
Datastream that has already received a file, the only preview the console owned
asked the person to *re-upload* one. And amendment 7 -- rightly -- removed the
connector pull axis from a `managed_feed`, which left the `Data` tab of the four
live file sources with no reading of their own data at all.

So this module answers ONE question: **what did the last file that landed on this
Datastream actually contain?** It reads, in order:

  * `app.inbound_raw_imports` -- a file ARRIVED (email or upload), with its state;
  * `app.managed_feed_import_ledger` -- an import was OPENED for it, and where it
    LANDED (`landing_relation`, `row_count`, `outcome`);
  * the warehouse relation itself -- bounded, project-scoped and masked.

THE MASKING IS NOT WRITTEN HERE. A managed landing carries the SOURCE's own
column names, exactly like the `Collected` zone, so the policy that applies is
`collected_mapped_reader`'s -- mask by default, show only a column the mapping
classified `none`. `cache_warehouse._pii_columns` matches thirteen English words
against CANONICAL names and would show `usr_mail`; that is why the day reading
inverted the rule, and a second masking here would be the third policy in the
product. `_mask` and `_masked_columns` are imported from there and nothing
equivalent is defined in this file.

EMPTY AND BROKEN SHARE NO WORD. « No file has arrived yet » is a state of the
Datastream and names the door a file comes in by; « the last file could not be
read » is a failure of the read. They are two different reasons with two
different sentences, and neither is ever a `0` or an invented row.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

logger = logging.getLogger(__name__)

#: The only mode this reading applies to. `external_bq` is pushed too, but its
#: rows are never a FILE: it names an external relation and has its own panel.
MODE_MANAGED_FEED = "managed_feed"

#: A bounded reading, on the same principle as every other evidence read of this
#: product: the ceiling is the day reading's, and the default is smaller because
#: this is a recognition aid, not a page of data.
DEFAULT_ROWS = 20

#: Why no rows are shown. Each is a DIFFERENT sentence below -- the whole point.
NOT_A_PUSHED_SOURCE = "not_a_pushed_source"
NO_FILE_YET = "no_file_yet"
ARRIVED_NOT_LANDED = "arrived_not_landed"
ARRIVAL_REFUSED = "arrival_refused"
IMPORT_NOT_LANDED = "import_not_landed"
IMPORT_FAILED = "import_failed"
LANDING_NOT_RECORDED = "landing_not_recorded"
RELATION_ABSENT = "relation_absent"
WAREHOUSE_UNAVAILABLE = "warehouse_unavailable"

#: The sentence each reason is read as. On the SERVER, like every other refusal
#: of this surface, so one wording travels whichever door asks.
_MESSAGES: dict[str, str] = {
    NOT_A_PUSHED_SOURCE: (
        "This Datastream pulls its rows from a connector, so no file is ever "
        "pushed to it and there is none to read back."
    ),
    NO_FILE_YET: "No file has arrived on this Datastream yet.",
    ARRIVED_NOT_LANDED: (
        "A file has arrived and is still on its way in: it has been received but "
        "no import has written its rows yet."
    ),
    ARRIVAL_REFUSED: (
        "The last file that arrived was refused before any row was written, so "
        "there is nothing to read back from it."
    ),
    IMPORT_NOT_LANDED: (
        "The last import is still open: it has not written its rows yet."
    ),
    IMPORT_FAILED: "The last file could not be imported, so no row of it landed.",
    LANDING_NOT_RECORDED: (
        "The last import finished without recording where its rows landed, so "
        "they cannot be read back."
    ),
    RELATION_ABSENT: (
        "The relation the last import named does not exist in the warehouse, so "
        "the last file could not be read."
    ),
    WAREHOUSE_UNAVAILABLE: "The last file could not be read: the warehouse did not answer.",
}


def message_for(reason: str | None) -> str | None:
    """The sentence of a reason, or `None` when the reading carries rows."""
    if not reason:
        return None
    return _MESSAGES.get(reason)


# ---------------------------------------------------------------------------
# Postgres: what arrived, and what was imported.
# ---------------------------------------------------------------------------

#: The arrival, whichever door it came through. Joined to `app.datastreams` for
#: the project scope: `app.inbound_raw_imports` carries `datastream_id` alone, and
#: reading it on that id would answer for a Datastream of another Project.
_ARRIVAL_SQL = """
    SELECT r.id, r.filename, r.state, r.size_bytes, r.created_at,
           r.error_code, r.import_ledger_id
    FROM app.inbound_raw_imports r
    JOIN app.datastreams d ON d.id = r.datastream_id
    WHERE r.datastream_id = %s AND d.project_id = %s
    ORDER BY r.created_at DESC, r.id DESC
    LIMIT 1
"""

#: The imports, newest first. A handful is enough: the reading wants the newest
#: one that actually WROTE, and the newest one whatever it did -- so it can tell
#: "nothing has landed yet" from "the last one failed".
_IMPORT_SQL = """
    SELECT id, execution_id, feed_format, source_metadata, landing_relation,
           row_count, rejected_row_count, outcome, error_code,
           snapshot_observed_at, created_at
    FROM app.managed_feed_import_ledger
    WHERE datastream_id = %s AND project_id = %s
    ORDER BY created_at DESC, id DESC
    LIMIT 10
"""

#: The outcomes that mean rows were written and are readable.
_LANDED_OUTCOMES = ("written", "published")


def _rows(cur) -> list[dict[str, Any]]:
    names = [description[0] for description in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def _instant(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _filename_of(record: Mapping[str, Any]) -> str | None:
    """The name of the file this import read, when the import recorded one.

    `source_metadata` is opaque JSON by contract (migration 077) and a Google
    Sheets sync carries no filename at all. `None` is "this import named no
    file", which the screen says as much -- never an invented placeholder.
    """
    metadata = record.get("source_metadata")
    if not isinstance(metadata, dict):
        return None
    name = metadata.get("filename")
    return str(name) if name else None


def _arrival_payload(record: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "raw_import_id": record.get("id"),
        "filename": record.get("filename"),
        "state": record.get("state"),
        "size_bytes": record.get("size_bytes"),
        "arrived_at": _instant(record.get("created_at")),
        "error_code": record.get("error_code"),
    }


def _import_payload(record: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "ledger_id": record.get("id"),
        "execution_id": record.get("execution_id"),
        "feed_format": record.get("feed_format"),
        "filename": _filename_of(record),
        "outcome": record.get("outcome"),
        # NEVER coalesced to 0. `row_count` is NULL until it is measured, and the
        # migration says so in as many words -- a 0 here would read as a file that
        # contained nothing.
        "row_count": record.get("row_count"),
        "rejected_row_count": record.get("rejected_row_count"),
        "error_code": record.get("error_code"),
        "observed_at": _instant(record.get("snapshot_observed_at")),
        "imported_at": _instant(record.get("created_at")),
    }


def _doors(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """By which door a file reaches this Datastream.

    `config.channels` is the Datastream's own allowlist -- the same list
    `inbound_ingest` refuses an unlisted channel against. Upload is not in it and
    is never inferred from it: an upload is a gesture of the console, always
    available on a file source, and the empty state names it.
    """
    declared = (config or {}).get("channels")
    channels = [str(channel).strip().lower() for channel in declared or [] if str(channel).strip()]
    return {"channels": channels, "upload_available": True}


# ---------------------------------------------------------------------------
# The warehouse read.
# ---------------------------------------------------------------------------


def _locations(*, project_id: str, relation: str, execution_id: str | None, mode: str):
    """Where the recorded landing may physically be, most exact first.

    The ledger records what `raw_landing.land_raw_rows` RETURNED, and that value
    is the BARE table name -- candidate-suffixed when the import ran inside a
    candidate execution, which every console import does. So three shapes exist in
    that one column and each is addressed rather than assumed:

      * `schema.table` -- qualified, as `allocate_landing_relation` composes it
        before a write; the schema is taken as given;
      * `managed_feed_<ds>__cand_<exec>` -- an isolated candidate, which lives in a
        SCHEMA of the same name derived from the raw zone (`warehouse_write.
        _open_candidate_writer`), not in the raw zone itself;
      * `managed_feed_<ds>` -- the shared landing, in the raw zone.

    Yielded lazily and probed in order, so the ordinary case costs one statement.
    """
    from core import warehouse_tenancy  # noqa: PLC0415

    if mode == "bigquery":
        zone = warehouse_tenancy.bigquery_raw_dataset(project_id)
    else:
        zone = warehouse_tenancy.raw_schema(project_id)

    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []

    def add(schema: str, table: str) -> None:
        key = (schema, table)
        if schema and table and key not in seen:
            seen.add(key)
            ordered.append(key)

    if "." in relation:
        schema, _, table = relation.rpartition(".")
        add(schema, table)
    table = relation.rpartition(".")[2]
    if "__cand_" in table and execution_id:
        from core.raw_landing import candidate_table  # noqa: PLC0415

        try:
            add(candidate_table(zone, execution_id), table)
        except Exception:  # noqa: BLE001 -- an unusable id is not a reason to fail
            logger.warning("landed_file: candidate schema unresolvable for %s", execution_id)
    add(zone, table)
    # The shared relation the candidate was promoted into, when it was. Its rows
    # are narrowed to this execution below, so publishing never widens the read.
    if "__cand_" in table:
        add(zone, table.split("__cand_", 1)[0])
    return ordered


def read_landing_rows(
    *,
    project_id: str,
    relation: str,
    execution_id: str | None = None,
    classifications: Mapping[str, str] | None = None,
    limit: int = DEFAULT_ROWS,
) -> dict[str, Any]:
    """The bounded, masked rows of ONE managed landing -- two statements at most.

    The columns are read first, because an absent relation and an empty one are
    two different answers and only the catalog can tell them apart. The rows are
    then narrowed to this Project and, when the relation carries the provenance
    column, to the execution of THIS import: a promoted candidate lives in a
    shared relation beside every earlier import, and « the last file » must not
    quietly become « everything ever landed ».
    """
    from core import collected_mapped_reader as reader  # noqa: PLC0415
    from core import warehouse  # noqa: PLC0415

    bounded = max(1, min(int(limit or DEFAULT_ROWS), reader.MAX_ROWS))
    mode = warehouse._db_mode()
    if mode not in ("duckdb", "bigquery"):
        return _no_rows(relation, WAREHOUSE_UNAVAILABLE)

    columns: list[str] = []
    located: tuple[str, str] | None = None
    for schema, table in _locations(
        project_id=project_id, relation=relation, execution_id=execution_id, mode=mode
    ):
        try:
            found = reader._relation_columns(warehouse, mode, schema, table)
        except Exception as exc:  # noqa: BLE001 -- an unreadable warehouse is named
            logger.warning(
                "landed_file: %s.%s undescribable (%s)", schema, table, type(exc).__name__
            )
            return _no_rows(relation, WAREHOUSE_UNAVAILABLE)
        if found:
            columns, located = found, (schema, table)
            break
    if located is None or not columns:
        return _no_rows(relation, RELATION_ABSENT)

    schema, table = located
    prefix = f"`{schema}`." if mode == "bigquery" else f"{reader._quote(schema, mode)}."
    projection = ", ".join(reader._quote(column, mode) for column in columns)
    # Deterministic over the columns the relation actually has, so two readings of
    # an unchanged landing return the same rows in the same order.
    order_by = ", ".join(reader._quote(column, mode) for column in columns)

    predicates: list[str] = []
    params: list[Any] = []
    for column, value in (("project_id", project_id), ("execution_id", execution_id)):
        if column in columns and value:
            mark = "?" if mode == "duckdb" else f"@p{len(params)}"
            predicates.append(f"{reader._quote(column, mode)} = {mark}")
            params.append(value)
    where = f" WHERE {' AND '.join(predicates)}" if predicates else ""

    try:
        sql = (
            f"SELECT {projection} FROM {prefix}{reader._quote(table, mode)}"  # noqa: S608
            f"{where} ORDER BY {order_by} LIMIT {bounded + 1}"
        )
        rows = (
            warehouse._query_duckdb(sql, params)
            if mode == "duckdb"
            else warehouse._query_bigquery(sql, params)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("landed_file: %s.%s unreadable (%s)", schema, table, type(exc).__name__)
        return _no_rows(relation, WAREHOUSE_UNAVAILABLE, columns=columns)

    rows = list(rows or [])
    truncated = len(rows) > bounded
    rows = rows[:bounded]
    masked = reader._masked_columns(columns, classifications or {})
    return {
        "relation": f"{schema}.{table}",
        "columns": columns,
        "rows": [
            {column: reader._mask(row.get(column), column, masked) for column in columns}
            for row in rows
        ],
        "row_count": len(rows),
        "truncated": truncated,
        "masked_fields": sorted(masked),
        "readable": True,
        "reason": None,
        "message": None,
    }


def _no_rows(
    relation: str | None, reason: str, columns: list[str] | None = None
) -> dict[str, Any]:
    return {
        "relation": relation,
        "columns": list(columns or []),
        # `None`, never `[]`: an empty list is a relation that was read and held
        # nothing, which is not what happened here.
        "rows": None,
        "row_count": None,
        "truncated": False,
        "masked_fields": [],
        "readable": False,
        "reason": reason,
        "message": message_for(reason),
    }


# ---------------------------------------------------------------------------
# The whole answer.
# ---------------------------------------------------------------------------


def read_landed_file(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    limit: int = DEFAULT_ROWS,
) -> dict[str, Any]:
    """What the last file to reach this Datastream contained, or why it did not.

    Three Postgres statements at most, and one or two warehouse statements only
    when an import actually recorded a landing. A Datastream that has never
    received anything costs the warehouse nothing.
    """
    # THE PAIR IS IN THE FIRST STATEMENT, and it is written here rather than in a
    # module constant on purpose: this read is what proves the Datastream belongs
    # to the Project the caller was authorized for, and a Datastream of another
    # Project answers exactly as one that does not exist. Everything below runs
    # only once this row came back.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_kind, config FROM app.datastreams "
            "WHERE id = %s AND project_id = %s AND archived_at IS NULL",
            (datastream_id, project_id),
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError("Datastream not found")
        source_kind, config = row[0], row[1]

    payload: dict[str, Any] = {
        "project_id": project_id,
        "datastream_id": datastream_id,
        "mode": source_kind,
        "doors": _doors(config if isinstance(config, dict) else {}),
        "arrival": None,
        "import": None,
        "relation": None,
        "columns": [],
        "rows": None,
        "row_count": None,
        "truncated": False,
        "masked_fields": [],
        "reason": None,
        "message": None,
    }
    if source_kind != MODE_MANAGED_FEED:
        return {
            **payload,
            "reason": NOT_A_PUSHED_SOURCE,
            "message": message_for(NOT_A_PUSHED_SOURCE),
        }

    with conn.cursor() as cur:
        cur.execute(_ARRIVAL_SQL, (datastream_id, project_id))
        arrivals = _rows(cur)
    with conn.cursor() as cur:
        cur.execute(_IMPORT_SQL, (datastream_id, project_id))
        imports = _rows(cur)

    arrival = arrivals[0] if arrivals else None
    landed = next(
        (record for record in imports if record.get("outcome") in _LANDED_OUTCOMES),
        None,
    )
    latest = imports[0] if imports else None
    payload["arrival"] = _arrival_payload(arrival)
    payload["import"] = _import_payload(landed or latest)

    if landed is None:
        return {**payload, **_nothing_landed(arrival, latest)}

    relation = landed.get("landing_relation")
    if not relation:
        return {
            **payload,
            "reason": LANDING_NOT_RECORDED,
            "message": message_for(LANDING_NOT_RECORDED),
        }

    reading = read_landing_rows(
        project_id=project_id,
        relation=str(relation),
        execution_id=(str(landed.get("execution_id")) if landed.get("execution_id") else None),
        classifications=_classifications(conn, project_id=project_id, datastream_id=datastream_id),
        limit=limit,
    )
    return {
        **payload,
        "relation": reading["relation"],
        "columns": reading["columns"],
        "rows": reading["rows"],
        "row_count": reading["row_count"],
        "truncated": reading["truncated"],
        "masked_fields": reading["masked_fields"],
        "reason": reading["reason"],
        "message": reading["message"],
    }


def _nothing_landed(
    arrival: Mapping[str, Any] | None, latest: Mapping[str, Any] | None
) -> dict[str, Any]:
    """WHICH silence this is. Five, and only one of them is « nothing yet ».

    The order is the order of the chain: an import that failed is a stronger
    statement than an arrival that is still in flight, because it says the file
    got further and then stopped.
    """
    if latest is not None:
        outcome = str(latest.get("outcome") or "")
        if outcome in ("failed", "rejected"):
            reason = IMPORT_FAILED
        elif outcome == "opened":
            reason = IMPORT_NOT_LANDED
        else:
            # `noop` -- the snapshot was unchanged, so this run wrote nothing and
            # no earlier run wrote either. Same silence as an open import: rows
            # exist nowhere yet.
            reason = IMPORT_NOT_LANDED
        return {"reason": reason, "message": message_for(reason)}
    if arrival is not None:
        state = str(arrival.get("state") or "")
        reason = ARRIVAL_REFUSED if state in ("REJECTED", "FAILED") else ARRIVED_NOT_LANDED
        return {"reason": reason, "message": message_for(reason)}
    return {"reason": NO_FILE_YET, "message": message_for(NO_FILE_YET)}


def _classifications(conn, *, project_id: str, datastream_id: str) -> dict[str, str]:
    """The mapping's sensitivity per column name -- the day reading's own answer.

    Read through `datastream_daily_breakdown_api`, which already resolves the
    versioned store first and the flat table second. A second resolution here
    would be a second answer to « what does this Datastream map », and the two
    would drift.
    """
    from core.datastream_daily_breakdown_api import (  # noqa: PLC0415
        classifications_of,
        read_mapping_columns,
    )

    try:
        header = read_mapping_columns(
            conn, project_id=project_id, datastream_id=datastream_id
        )
    except Exception as exc:  # noqa: BLE001 -- masking never fails OPEN
        logger.warning("landed_file: mapping columns unavailable (%s)", type(exc).__name__)
        return {}
    return classifications_of(header.get("columns") or [])
