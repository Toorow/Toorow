"""The two readings of one day: `Collected` and `Mapped` -- story 58.3, epic 58.

WHY THIS READER IS NOT IN `cache_warehouse`. That module refuses raw relations in
its own words -- "AD-12: reads marts only (never raw_* tables, CSV, or direct
APIs)" -- and the sentence over-states its source. AD-12
(`ARCHITECTURE-SPINE.md`, decision « Ingestion and read path ») says the
OPPOSITE on this point: every pull "lands in BigQuery raw first and is read back
through the semantic layer". What is really ratified, one section further down,
is that the EPHEMERAL DuckDB CACHE holds the allowlisted marts and nothing else --
a policy of that cache, decided in its place. So the refusal stands where it was
written, the comment there now cites the rule it actually comes from, and this
reader lives beside it rather than inside it.

WHAT IT COSTS, AND WHY THAT IS THE FIRST THING SAID. Two statements per relation
-- the column list, then the rows -- whatever the width of the window. The reader
next door (`read_datastream_sample`) runs `for day in days:` and issues one
warehouse query per day, 92 at its ceiling; copying that here would have made
opening a day the most expensive gesture of the surface. The count is ASSERTED by
`tests/core/test_collected_mapped_reader.py`, on one day and on ninety-two.

MASKING IS A REFUSAL BY DEFAULT, and this is the one place in the product where
that had to change. `cache_warehouse._pii_columns` matches thirteen English words
(`email|token|password|…`) against CANONICAL column names. A raw relation carries
the SOURCE's names: `usr_mail`, `tel`, `cust_id` match nothing, and a policy by
pattern leaks by omission. So the rule here is `datastream_activation._mask_rows`'s
-- a value is shown only when its column is classified `none`, and every column
without a matching classification is masked. It is the product's existing policy,
applied where the names are least trustworthy.

WHAT IT REFUSES TO OPEN. A candidate execution lands in a relation of its own
(`raw_landing.candidate_table`), and "staging names the shared table, so a
candidate is invisible to it BY CONSTRUCTION". This reader opens the SHARED
relation only. A day whose sole run is an unpublished candidate therefore reads
empty here, and the emptiness is NAMED (`candidate_not_published`) rather than
resolved: opening one relation per execution, at a hashed name, is a cost nobody
has measured.

AND THE TWO SIDES NOW PAIR, THROUGH THE MAPPING AND NOT THROUGH A NAME. Amendment
12 of the 2026-08-11 review: the key between `collected` and `mapped` is the active
mapping's own `source -> target`, and `collected_mapped_pairing` is where it is
laid. This module keeps its unmasked rows just long enough to hand them over --
`_RAW_ROWS`, stripped from everything published -- because a join computed on the
sentinel would match every masked row against every other. The masking policy stays
here, and there is still exactly one of it.

AND WHAT IT CANNOT PROVE. The BigQuery branch is not exercised by any test of
this repository -- no GCP dataset, no connector test account, and a mocked client
would prove only that the SQL can be written. What holds the two dialects in step
is that they answer the same shape from the same function, and that shape is
pinned. Said here rather than left to be discovered, exactly as
`read_daily_row_counts` said it of itself.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Mapping, Sequence

from core.collected_mapped_pairing import RAW_ROWS as _RAW_ROWS

logger = logging.getLogger(__name__)

#: The two zones this reader addresses, in pipeline order.
ZONE_COLLECTED = "collected"
ZONE_MAPPED = "mapped"

#: What a masked value is replaced by -- the sentinel the product already uses.
MASK_SENTINEL = "[MASKED]"

#: A bounded reading. The day grid above it is bounded at 92 days and this is one
#: day of it; an unbounded row list would be a different object (an export) and
#: this surface already has one.
MAX_ROWS = 50

#: Why a side of the reading carries no row.
RELATION_ABSENT = "relation_absent"
PROJECT_SCOPE_ABSENT = "project_scope_absent"
DATE_COLUMN_ABSENT = "date_column_absent"
WAREHOUSE_UNAVAILABLE = "warehouse_unavailable"
#: Story 59.1: the field a replay was asked to test for NULL is not a column of
#: this relation. Refused rather than filtered on, because a `WHERE <absent> IS
#: NULL` would match every row and hand back the whole day as "faulty".
FIELD_ABSENT = "field_absent"

#: The values of this column may not be listed, because its classification does
#: not allow a value to leave the warehouse. The COUNTS still travel: how many
#: distinct values a column holds is not one of them.
FIELD_MASKED = "field_masked"

#: How many distinct values one listing may carry. A dimension of video ids held
#: 519 distinct values over four weeks on the deployment (2026-08-12); a
#: dimension of urls or user ids holds orders of magnitude more, and a screen
#: that asked for all of them would pay for a scan nobody bounded. The listing is
#: ordered by weight, so the truncated tail is the cheap end.
MAX_VALUES = 200

#: Notes on a reading that DID run. Not failures: facts about what was read.
#:
#: `NO_ROW_IN_SHARED_RELATION` says WHAT WAS MEASURED and then says what this
#: reading cannot tell apart. It replaces a `candidate_not_published` that was
#: emitted on every empty day: nothing here observes a candidate execution, so
#: announcing one to a person whose flux has simply never been pulled was a
#: reason invented to fill a silence. Arbitrage 5 asks for the absence to be
#: NAMED, not supposed -- and when two causes cannot be told apart, the honest
#: answer says so instead of choosing the more interesting one.
NO_ROW_IN_SHARED_RELATION = "no_row_in_shared_relation"
SUPERSEDED_BY_PULL = "superseded_by_pull"

#: The sentence each of them is read as. Written here, on the server, because the
#: screen must hold none of its own -- one wording, whichever door it comes
#: through.
_MESSAGES = {
    RELATION_ABSENT: (
        "This relation does not exist in the warehouse yet, so nothing has ever "
        "landed in it."
    ),
    PROJECT_SCOPE_ABSENT: (
        "This relation carries no project column, so its rows cannot be scoped to "
        "this project and none are read."
    ),
    DATE_COLUMN_ABSENT: (
        "This relation carries no date column, so its rows cannot be read one day "
        "at a time."
    ),
    WAREHOUSE_UNAVAILABLE: "The warehouse could not be read.",
    FIELD_ABSENT: (
        "This field is not a column of this relation, so no row of it can be "
        "replayed. A column that is not there is not a column that is null."
    ),
    FIELD_MASKED: (
        "The values of this column are classified, so they are counted here and "
        "never listed. Classify the column as carrying no personal data to read "
        "its values."
    ),
    "invalid_range": "The window asked for is not a range of calendar days.",
    NO_ROW_IN_SHARED_RELATION: (
        "No row for this day is in the shared relation. This reading cannot say "
        "whether nothing was ever collected for it or whether the only run that "
        "covered it is still an unpublished candidate, which lands in a relation "
        "of its own that this reading does not open."
    ),
    SUPERSEDED_BY_PULL: (
        "Several pulls cover this day. The mapped reading keeps the latest pull per "
        "grain, so the rows of the earlier ones have no counterpart there."
    ),
}

#: The column every relation is filtered on. 52 of the 52 declared raw relations
#: carry it, and a relation that did not would be refused rather than read wide.
_PROJECT_COLUMN = "project_id"

#: The day axis. NEVER guessed from a neighbour: 7 of the 52 declared relations
#: have no `date` column at all (`raw_x_ads_daily` carries `interval_start`,
#: `raw_strava_club_daily` a `snapshot_date`), and reading one of those on a
#: column that "looks like" a date would publish a day that was never measured.
_DATE_COLUMN = "date"

#: The provenance column the supersession note is read from, when it exists.
_PULL_COLUMN = "pull_id"


class StageReadError(Exception):
    """A reading could not be performed. Carries the code the payload publishes."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def message_for(reason: str | None) -> str | None:
    """The sentence of a reason or a note, or `None`."""
    if not reason:
        return None
    return _MESSAGES.get(reason)


def _window_days(start: str, end: str) -> tuple[str, str]:
    try:
        first = date.fromisoformat(start)
        last = date.fromisoformat(end)
    except (TypeError, ValueError) as exc:
        raise StageReadError("invalid_range", f"invalid date: {exc}") from exc
    if last < first:
        raise StageReadError("invalid_range", "end is before start")
    return first.isoformat(), last.isoformat()


def _quote(name: str, mode: str) -> str:
    """Validate an identifier, then quote it in the dialect that is being spoken.

    `_quote_ident` is the repository's single validator and it emits the SQL
    standard's double quotes, which BigQuery reads as a STRING. So the check is
    shared -- an identifier that fails it never reaches either backend -- and the
    quoting is per dialect.
    """
    from core.schema_context_gen import _quote_ident  # noqa: PLC0415

    quoted = _quote_ident(name)
    return quoted if mode == "duckdb" else f"`{name}`"


def _zone_names(project_id: str, zone: str, mode: str) -> tuple[str, str]:
    """`(schema-or-dataset, query prefix)` of one zone, from the single naming point.

    BOTH come from `warehouse_tenancy`, and neither is composed here. The catalog
    lookup needs the bare schema (`information_schema.columns.table_schema`) and
    the `FROM` needs the prefix, so the module that owns the topology answers
    both questions -- `f"{schema}."` written at this call site would be the
    inline composition that module exists to prevent.
    """
    from core import warehouse_tenancy  # noqa: PLC0415

    if mode == "bigquery":
        dataset = (
            warehouse_tenancy.bigquery_raw_dataset(project_id)
            if zone == ZONE_COLLECTED
            else warehouse_tenancy.bigquery_staging_dataset(project_id)
        )
        return dataset, f"`{dataset}`."
    if zone == ZONE_COLLECTED:
        return (
            warehouse_tenancy.raw_schema(project_id),
            warehouse_tenancy.raw_prefix(project_id),
        )
    return (
        warehouse_tenancy.staging_schema(project_id),
        warehouse_tenancy.staging_prefix(project_id),
    )


def _mask(value: Any, column: str, masked: frozenset[str]) -> Any:
    if column in masked:
        return MASK_SENTINEL
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _missing_sql(field: str, mode: str) -> str:
    """The ONE predicate for "this key column carries no value", in both dialects.

    NULL IS NOT THE ONLY SPELLING OF ABSENT, and the deployment proved it rather
    than a review guessing it. On 2026-08-12 a live Datastream whose grain is
    `(channel_id, date, video)` held 602 rows over 29 days with `video = ''` on
    every single one: zero nulls, one hundred percent empty. The null-rate monitor
    measured `0 / 602` and passed, on the exact column a person opened the console
    to ask about.

    A key column is missing when it is NULL **or** when its text form trims to
    nothing. `CAST(... AS STRING)` never turns a number or a date into an empty
    string, so applying the same predicate to every declared key column is safe:
    only a text column can be blank, and a blank text key is not a key.

    It lives here, once, because the COUNT and the REPLAY must agree. They did not
    have to before: the count filtered `IS NULL` and the replay narrowed on
    `IS NULL`, so both were wrong in the same direction and nobody could see it.
    """
    quoted = _quote(field, mode)
    text_type = "VARCHAR" if mode == "duckdb" else "STRING"
    return f"({quoted} IS NULL OR TRIM(CAST({quoted} AS {text_type})) = '')"


def _masked_columns(columns: list[str], classifications: Mapping[str, str]) -> frozenset[str]:
    """Every column NOT classified `none`. Absence of a classification masks.

    The inversion is the whole policy: a name-based allowance would show
    `usr_mail` because no English pattern matches it.
    """
    return frozenset(
        column for column in columns if str(classifications.get(column, "")) != "none"
    )


def describe_relation(*, project_id: str, relation: str, zone: str) -> dict[str, Any]:
    """CAN this relation be read one day at a time, and if not, WHY -- ONE statement.

    WHY THIS IS A SEPARATE PASS, AND WHY IT RUNS BEFORE ANY CLICK. Whether
    `Collected` can be served is not a property of the relation's NAME. Measured
    2026-08-06: 7 of the 52 declared raw relations carry no `date` column at all,
    which is 14 report profiles over 7 connectors -- `raw_x_ads_daily` (its window
    is `interval_start`/`interval_end`), `raw_strava_club_daily` (`snapshot_date`),
    `raw_taboola_history`, `raw_monday_board_snapshot`, `raw_gbp_review`,
    `raw_gbp_search_keyword_monthly`, `raw_linkedin_company_pages_daily`. Deciding
    availability on the declaration alone offers those fourteen an ENABLED control
    that refuses once pressed, which is exactly what arbitrage 2 forbids: the
    reason has to be there before the click.

    So the column list is read for the availability, and the SAME list is handed to
    `read_rows` -- the check costs one statement and is never paid twice.
    """
    from core import warehouse  # noqa: PLC0415

    mode = warehouse._db_mode()
    if mode not in ("duckdb", "bigquery"):
        return _absent(relation, None, WAREHOUSE_UNAVAILABLE)
    zone_name, prefix = _zone_names(project_id, zone, mode)
    try:
        columns = _relation_columns(warehouse, mode, zone_name, relation)
    except Exception as exc:  # noqa: BLE001
        # NEVER raised out of here: the strip of days lives in Postgres and a
        # warehouse that cannot answer must not take it down.
        logger.warning(
            "collected_mapped_reader: %s undescribable (%s)", relation, type(exc).__name__
        )
        return _absent(relation, zone_name, WAREHOUSE_UNAVAILABLE)

    if not columns:
        return _absent(relation, zone_name, RELATION_ABSENT)
    if _PROJECT_COLUMN not in columns:
        return _absent(relation, zone_name, PROJECT_SCOPE_ABSENT, columns=columns)
    if _DATE_COLUMN not in columns:
        return _absent(relation, zone_name, DATE_COLUMN_ABSENT, columns=columns)
    return {
        "relation": relation,
        "zone": zone_name,
        "prefix": prefix,
        "mode": mode,
        "columns": columns,
        "readable": True,
        "reason": None,
        "message": None,
    }


def describe_pair(*, project_id: str, pair: Mapping[str, Any]) -> dict[str, Any]:
    """Both sides described -- one statement per relation the resolver could name.

    A side the manifest does not address costs nothing: there is no relation to
    look for, and the resolver's own reason travels instead.
    """
    described: dict[str, Any] = {}
    for zone, key in (
        (ZONE_COLLECTED, "collected_relation"),
        (ZONE_MAPPED, "mapped_relation"),
    ):
        relation = pair.get(key)
        if not relation:
            described[zone] = _undeclared(pair)
        else:
            described[zone] = describe_relation(
                project_id=project_id, relation=str(relation), zone=zone
            )
    return described


def read_stage_rows(
    *,
    project_id: str,
    relation: str,
    zone: str,
    start: str,
    end: str,
    classifications: Mapping[str, str] | None = None,
    limit: int = MAX_ROWS,
) -> dict[str, Any]:
    """The rows of ONE relation over `[start, end]`, masked, project-scoped.

    Two statements: the column list, then the rows -- and never one per day.
    """
    return read_rows(
        description=describe_relation(
            project_id=project_id, relation=relation, zone=zone
        ),
        project_id=project_id,
        start=start,
        end=end,
        classifications=classifications,
        limit=limit,
    )


def read_rows(
    *,
    description: Mapping[str, Any],
    project_id: str,
    start: str,
    end: str,
    classifications: Mapping[str, str] | None = None,
    limit: int = MAX_ROWS,
    null_field: str | None = None,
) -> dict[str, Any]:
    """The rows of an ALREADY DESCRIBED relation, masked and PUBLISHABLE.

    The unmasked rows `_read_side` keeps for the pairing are dropped here: every
    caller of this function hands its answer to a browser, and a key that exists
    only to be joined on inside the server has no business travelling with it.
    """
    side = _read_side(
        description=description,
        project_id=project_id,
        start=start,
        end=end,
        classifications=classifications,
        limit=limit,
        null_field=null_field,
    )
    side.pop(_RAW_ROWS, None)
    return side


def _read_side(
    *,
    description: Mapping[str, Any],
    project_id: str,
    start: str,
    end: str,
    classifications: Mapping[str, str] | None = None,
    limit: int = MAX_ROWS,
    null_field: str | None = None,
) -> dict[str, Any]:
    """The rows of an ALREADY DESCRIBED relation -- ONE statement, whatever the window.

    The description carries the columns, so this never asks the catalog again: the
    availability the screen was shown and the rows it then reads come from one and
    the same measurement of the relation.

    `null_field` NARROWS THE READING TO THE ROWS A `null_rate` ISSUE IS ABOUT --
    story 59.1. The predicate belongs in the statement and not in Python: this
    reading is bounded at `MAX_ROWS`, so filtering fifty already-fetched rows
    would answer "nothing matches this condition now" on a day whose fifty-first
    row is null. A field that is not a column of the relation is REFUSED
    (`FIELD_ABSENT`), never filtered on.
    """
    if not description.get("readable"):
        return dict(description)
    if null_field is not None and null_field not in list(description["columns"]):
        return _absent(
            str(description["relation"]),
            description["zone"],
            FIELD_ABSENT,
            columns=list(description["columns"]),
        )

    first, last = _window_days(start, end)
    classifications = classifications or {}
    bounded = max(1, min(int(limit or MAX_ROWS), MAX_ROWS))

    from core import warehouse  # noqa: PLC0415

    relation = str(description["relation"])
    zone_name = description["zone"]
    prefix = description["prefix"]
    mode = description["mode"]
    columns = list(description["columns"])

    projection = ", ".join(_quote(column, mode) for column in columns)
    # Deterministic: the day, then the pull that landed it, then every remaining
    # column. Two readings of an unchanged relation return the same rows in the
    # same order, which is what makes a side-by-side comparable at all.
    ordering = [_DATE_COLUMN] + ([_PULL_COLUMN] if _PULL_COLUMN in columns else [])
    ordering += [column for column in columns if column not in ordering]
    order_by = ", ".join(_quote(column, mode) for column in ordering)
    scope = _quote(_PROJECT_COLUMN, mode)
    day = _quote(_DATE_COLUMN, mode)
    # Quoted through the one validator, exactly like every other identifier here,
    # and empty when no replay asked for it. THE SAME predicate the count uses --
    # a replay that selected only `IS NULL` would answer "nothing matches now" on
    # a firing raised by blanks, which is a lie the console would print.
    narrowing = f" AND {_missing_sql(null_field, mode)}" if null_field else ""

    try:
        if mode == "duckdb":
            rows = warehouse._query_duckdb(
                f"SELECT {projection} FROM {prefix}{_quote(relation, mode)} "  # noqa: S608
                f"WHERE {scope} = ? AND {day} >= ? AND {day} <= ?{narrowing} "
                f"ORDER BY {order_by} LIMIT {bounded + 1}",
                [project_id, first, last],
            )
        else:
            # `CAST(... AS STRING)` on the day, and it is not decoration: the raw
            # zone stores `date` as a STRING (every connector's DDL declares it
            # so, and `raw_landing` maps DATE -> STRING for both backends) while a
            # staging model may have cast it to a DATE. `_query_bigquery` binds
            # every parameter as a STRING, so one comparison has to work against
            # both column types, and only this one does.
            rows = warehouse._query_bigquery(
                f"SELECT {projection} FROM {prefix}{_quote(relation, mode)} "  # noqa: S608
                f"WHERE {scope} = @p0 "
                f"AND CAST({day} AS STRING) >= @p1 AND CAST({day} AS STRING) <= @p2"
                f"{narrowing} "
                f"ORDER BY {order_by} LIMIT {bounded + 1}",
                [project_id, first, last],
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "collected_mapped_reader: %s unreadable (%s)", relation, type(exc).__name__
        )
        return _absent(relation, zone_name, WAREHOUSE_UNAVAILABLE, columns=columns)

    rows = list(rows or [])
    truncated = len(rows) > bounded
    rows = rows[:bounded]
    masked = _masked_columns(columns, classifications)

    note = None
    if not rows:
        # WHAT WAS MEASURED: nothing for this day in the SHARED relation. Not
        # "a candidate exists" -- no statement here looked for one, and a flux
        # that has never been pulled would have been told a story about a run
        # that does not exist.
        note = NO_ROW_IN_SHARED_RELATION
    elif _PULL_COLUMN in columns and len({row.get(_PULL_COLUMN) for row in rows}) > 1:
        # Stated over the rows that were READ, and only over them. Every staging
        # model supersedes with `QUALIFY ROW_NUMBER() … ORDER BY pull_id DESC`, so
        # more than one pull on one day means some of these rows have no mapped
        # counterpart. Which ones is a question about the relation's grain, which
        # this reader does not know and will not guess.
        note = SUPERSEDED_BY_PULL

    return {
        "relation": relation,
        "zone": zone_name,
        "columns": columns,
        "rows": [
            {column: _mask(row.get(column), column, masked) for column in columns}
            for row in rows
        ],
        # THE SAME ROWS, UNMASKED, AT THE SAME INDEX -- and they leave this server
        # nowhere. `collected_mapped_pairing` is the only reader: a join computed on
        # `[MASKED]` would match every masked row against every other, which is a
        # wrong pairing, and a wrong pairing served as a fact is the defect amendment
        # 12 was written against. `_INTERNAL_KEYS` and `read_rows` both strip it.
        _RAW_ROWS: rows,
        "row_count": len(rows),
        "truncated": truncated,
        "masked_fields": sorted(masked),
        "reason": None,
        "message": None,
        "note": note,
        "note_message": message_for(note),
    }


def count_null_rows(
    *,
    description: Mapping[str, Any],
    project_id: str,
    start: str,
    end: str,
    fields: Sequence[str],
) -> dict[str, Any]:
    """HOW MANY ROWS THE WINDOW HOLDS, AND HOW MANY ARE NULL PER FIELD -- ONE statement.

    WHY THE COUNT LIVES HERE AND NOT IN THE MONITOR THAT NEEDS IT.
    `dq_monitors._check_duplication` opens `duckdb.connect(path, read_only=True)` by
    hand: it returns in silence when `TOOROW_DUCKDB_PATH` is unset, and in
    `TOOROW_DB_MODE=bigquery` it can never measure anything at all. A monitor whose
    verdict depends on an environment variable lies by omission. So the null rate is
    taken through the SAME address, the same two dialects and the same advance
    refusals as the reading a person sees on the screen -- including the 7 of 52
    declared raw relations that carry no `date` column and are refused before any
    statement is issued.

    AND IT IS AN AGGREGATE, NEVER A PAGE OF ROWS. `read_stage_rows` is bounded at
    `MAX_ROWS = 50` and masks every value whose column is not classified `none`; a
    rate over fifty masked rows is not a rate. Nothing leaves the database here
    except counts, which is also why a firing can carry the measurement and never
    the faulty rows.

    A field the relation does not carry is REPORTED (`absent_fields`), never counted
    as zero nulls: "this column is not there" and "this column is never null" are two
    different sentences, and only one of them is a pass.
    """
    wanted = list(dict.fromkeys(str(field).strip() for field in fields if str(field).strip()))
    if not description.get("readable"):
        unreadable = dict(description)
        unreadable.update(
            {"null_counts": {}, "blank_counts": {}, "missing_counts": {},
             "measured_fields": [], "absent_fields": wanted}
        )
        return unreadable

    first, last = _window_days(start, end)
    columns = list(description["columns"])
    measured = [field for field in wanted if field in columns]
    absent_fields = [field for field in wanted if field not in columns]

    from core import warehouse  # noqa: PLC0415

    relation = str(description["relation"])
    zone_name = description["zone"]
    prefix = description["prefix"]
    mode = description["mode"]
    scope = _quote(_PROJECT_COLUMN, mode)
    day = _quote(_DATE_COLUMN, mode)
    # `n0..nN` rather than the field names: an alias is an identifier too, and the
    # source names are the least trustworthy in the product.
    try:
        if mode == "duckdb":
            aggregates = "".join(
                f", COUNT(*) FILTER (WHERE {_quote(field, mode)} IS NULL) AS n{index}"
                f", COUNT(*) FILTER (WHERE {_missing_sql(field, mode)}) AS m{index}"
                for index, field in enumerate(measured)
            )
            rows = warehouse._query_duckdb(
                f"SELECT COUNT(*) AS row_count{aggregates} "  # noqa: S608
                f"FROM {prefix}{_quote(relation, mode)} "
                f"WHERE {scope} = ? AND {day} >= ? AND {day} <= ?",
                [project_id, first, last],
            )
        else:
            aggregates = "".join(
                f", COUNTIF({_quote(field, mode)} IS NULL) AS n{index}"
                f", COUNTIF({_missing_sql(field, mode)}) AS m{index}"
                for index, field in enumerate(measured)
            )
            # `CAST(... AS STRING)` on the day for the same reason `read_rows` does
            # it: the raw zone stores `date` as a STRING and every parameter binds
            # as one.
            rows = warehouse._query_bigquery(
                f"SELECT COUNT(*) AS row_count{aggregates} "  # noqa: S608
                f"FROM {prefix}{_quote(relation, mode)} "
                f"WHERE {scope} = @p0 "
                f"AND CAST({day} AS STRING) >= @p1 AND CAST({day} AS STRING) <= @p2",
                [project_id, first, last],
            )
    except Exception as exc:  # noqa: BLE001 -- an unreadable relation is not a pass
        logger.warning(
            "collected_mapped_reader: %s uncountable (%s)", relation, type(exc).__name__
        )
        unreadable = _absent(relation, zone_name, WAREHOUSE_UNAVAILABLE, columns=columns)
        unreadable.update(
            {"null_counts": {}, "blank_counts": {}, "missing_counts": {},
             "measured_fields": [], "absent_fields": wanted}
        )
        return unreadable

    row = (list(rows or []) or [{}])[0]
    null_counts = {
        field: int(row.get(f"n{index}") or 0) for index, field in enumerate(measured)
    }
    missing_counts = {
        field: int(row.get(f"m{index}") or 0) for index, field in enumerate(measured)
    }
    return {
        "relation": relation,
        "zone": zone_name,
        "columns": columns,
        "readable": True,
        "row_count": int(row.get("row_count") or 0),
        "null_counts": null_counts,
        # The two spellings stay APART all the way up. "The source sent no value"
        # and "the source sent an empty string" are repaired in the same place but
        # they are not the same fact, and a screen that folded them would lose the
        # only clue that distinguishes a column the provider omits from a column
        # the extraction blanked.
        "blank_counts": {
            field: max(0, missing_counts[field] - null_counts[field]) for field in measured
        },
        "missing_counts": missing_counts,
        "measured_fields": measured,
        "absent_fields": absent_fields,
        "reason": None,
        "message": None,
    }


def _no_values(base: Mapping[str, Any], field: str) -> dict[str, Any]:
    """The shape of a listing that could not list, with its counts unclaimed."""
    answer = dict(base)
    answer.update(
        {
            "field": field,
            "values": [],
            "distinct_count": None,
            "row_count": None,
            "truncated": False,
        }
    )
    return answer


def read_distinct_values(
    *,
    description: Mapping[str, Any],
    project_id: str,
    start: str,
    end: str,
    field: str,
    classifications: Mapping[str, str] | None = None,
    limit: int = MAX_VALUES,
) -> dict[str, Any]:
    """The distinct values of ONE column over the window, heaviest first.

    THIS IS THE READING AN UNRESOLVED SET IS MADE OF, and it lives here rather
    than in the module that needs it for the reason `count_null_rows` gives one
    screen up: the same address, the same two dialects, the same advance refusals.
    A hand-rolled connection would answer on `TOOROW_DB_MODE=duckdb` and measure
    nothing at all in BigQuery.

    ONE STATEMENT, and the total comes back with it. `COUNT(*) OVER ()` after the
    grouping counts the GROUPS, so the caller learns how many distinct values
    exist even when it only receives the first `limit` of them, and `truncated`
    says which of the two it is holding. A listing that silently stopped at 200
    would read as "this dimension has 200 values".

    ORDERED BY WEIGHT, then by value. The tail that gets cut is the cheap end, and
    two readings of an unchanged relation return the same page.

    CLASSIFIED COLUMNS ARE COUNTED, NEVER LISTED. The masking policy of this
    module is inversion -- every column not classified `none` is masked -- and a
    list of values is exactly what that policy exists to withhold. So a classified
    column answers `field_masked` with no values, and the caller says so instead
    of rendering 200 sentinels.
    """
    if not description.get("readable"):
        return _no_values(description, field)
    columns = list(description["columns"])
    if field not in columns:
        return _no_values(
            _absent(
                str(description["relation"]),
                description["zone"],
                FIELD_ABSENT,
                columns=columns,
            ),
            field,
        )
    if str((classifications or {}).get(field, "")) != "none":
        return _no_values(
            _absent(
                str(description["relation"]),
                description["zone"],
                FIELD_MASKED,
                columns=columns,
            ),
            field,
        )

    first, last = _window_days(start, end)
    relation = str(description["relation"])
    zone_name = description["zone"]
    prefix = description["prefix"]
    mode = description["mode"]
    bounded = max(1, min(int(limit or MAX_VALUES), MAX_VALUES))
    quoted = _quote(field, mode)
    scope = _quote(_PROJECT_COLUMN, mode)
    day = _quote(_DATE_COLUMN, mode)

    from core import warehouse  # noqa: PLC0415

    select = (
        f"SELECT {quoted} AS value, COUNT(*) AS row_count, "
        f"COUNT(*) OVER () AS distinct_count, "
        f"SUM(COUNT(*)) OVER () AS window_rows "
    )
    tail = f"GROUP BY {quoted} ORDER BY row_count DESC, value LIMIT {bounded + 1}"
    try:
        if mode == "duckdb":
            rows = warehouse._query_duckdb(
                f"{select}FROM {prefix}{_quote(relation, mode)} "  # noqa: S608
                f"WHERE {scope} = ? AND {day} >= ? AND {day} <= ? {tail}",
                [project_id, first, last],
            )
        else:
            rows = warehouse._query_bigquery(
                f"{select}FROM {prefix}{_quote(relation, mode)} "  # noqa: S608
                f"WHERE {scope} = @p0 "
                f"AND CAST({day} AS STRING) >= @p1 AND CAST({day} AS STRING) <= @p2 {tail}",
                [project_id, first, last],
            )
    except Exception as exc:  # noqa: BLE001 -- an unreadable relation is not "no values"
        logger.warning(
            "collected_mapped_reader: %s values unreadable (%s)", relation, type(exc).__name__
        )
        return _no_values(
            _absent(relation, zone_name, WAREHOUSE_UNAVAILABLE, columns=columns), field
        )

    rows = list(rows or [])
    distinct_count = int(rows[0].get("distinct_count") or 0) if rows else 0
    window_rows = int(rows[0].get("window_rows") or 0) if rows else 0
    truncated = len(rows) > bounded
    return {
        "relation": relation,
        "zone": zone_name,
        "columns": columns,
        "readable": True,
        "field": field,
        "values": [
            {"value": row.get("value"), "row_count": int(row.get("row_count") or 0)}
            for row in rows[:bounded]
        ],
        "distinct_count": distinct_count,
        "row_count": window_rows,
        "truncated": truncated,
        "reason": None,
        "message": None,
    }


def _absent(
    relation: str, zone_name: str | None, reason: str, columns: list[str] | None = None
) -> dict[str, Any]:
    return {
        "relation": relation,
        "zone": zone_name,
        "columns": list(columns or []),
        "rows": None,
        "row_count": None,
        "truncated": False,
        "masked_fields": [],
        # STATED, not implied: this is both a description that refuses and a
        # reading that carries none, and the availability of the mode is read off
        # this one key.
        "readable": False,
        "reason": reason,
        "message": message_for(reason),
        "note": None,
        "note_message": None,
    }


def _relation_columns(warehouse, mode: str, zone_name: str, relation: str) -> list[str]:
    """The column names of *relation*, or `[]` when it does not exist.

    ONE statement, and it is the reader's own absence probe: `[]` says "this
    relation is not there", which is a different sentence from "it is there and
    this window is empty" -- exactly the separation `read_daily_row_counts` pays
    a second statement for.
    """
    if mode == "duckdb":
        rows = warehouse._query_duckdb(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
            [zone_name, relation],
        )
    else:
        rows = warehouse._query_bigquery(
            f"SELECT column_name FROM `{zone_name}`.INFORMATION_SCHEMA.COLUMNS "  # noqa: S608
            "WHERE table_name = @p0 ORDER BY ordinal_position",
            [relation],
        )
    return [str(row.get("column_name")) for row in (rows or [])]


def _undeclared(pair: Mapping[str, Any]) -> dict[str, Any]:
    """A side the manifest never addressed: the RESOLVER's reason, unread."""
    return {
        "relation": None,
        "zone": None,
        "prefix": None,
        "mode": None,
        "columns": [],
        "rows": None,
        "row_count": None,
        "truncated": False,
        "masked_fields": [],
        "readable": False,
        "reason": pair.get("reason"),
        "message": pair.get("message"),
        "note": None,
        "note_message": None,
    }


def read_collected_and_mapped(
    *,
    project_id: str,
    pair: Mapping[str, Any],
    start: str,
    end: str,
    classifications: Mapping[str, str] | None = None,
    limit: int = MAX_ROWS,
    description: Mapping[str, Any] | None = None,
    fields: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Both readings of the same window, each with its rows or its named absence.

    BOTH, ALWAYS, from one call: the screen's three positions -- `Collected`,
    `Mapped`, `Side by side` -- are then a choice of what to show and not a reason
    to ask the server again. Switching position costs nothing and cannot move the
    window, the day grid or the opened day, which is what the story asks of it.

    `description` is the SAME one the availability was computed from, handed down
    rather than taken again: the control a person pressed and the rows they then
    see come from one measurement of the relation, so the two can never disagree.

    `fields` IS THE KEY BETWEEN THE TWO SIDES -- the active mapping's own
    `source -> target` pairs, as the route already read them. Handed here and not
    guessed: the pairing has to run where the unmasked rows still exist, and it must
    pair nothing the mapping does not pair. Absent, the pairing refuses by name
    rather than falling back to a name that happens to match.
    """
    described = description or describe_pair(project_id=project_id, pair=pair)
    sides = {
        zone: _read_side(
            description=described[zone],
            project_id=project_id,
            start=start,
            end=end,
            classifications=classifications,
            limit=limit,
        )
        for zone in (ZONE_COLLECTED, ZONE_MAPPED)
    }
    from core import collected_mapped_pairing  # noqa: PLC0415

    pairing = collected_mapped_pairing.pair_readings(
        collected=sides[ZONE_COLLECTED],
        mapped=sides[ZONE_MAPPED],
        fields=fields,
        bounded_at=max(1, min(int(limit or MAX_ROWS), MAX_ROWS)),
    )
    return {
        "window": {"start": start, "end": end},
        "collected": _published(sides[ZONE_COLLECTED]),
        "mapped": _published(sides[ZONE_MAPPED]),
        # ONE ROW, BOTH VALUES -- amendment 12. Never a missing key: a screen that
        # cannot find this block and one that found it refusing are two different
        # sentences, and the screen says both.
        "pairing": pairing,
    }


#: Internal bookkeeping of a description that never reaches a browser: the
#: connection mode and the SQL prefix say how this server is wired, and
#: `_RAW_ROWS` holds the values the masking policy just decided to withhold.
_INTERNAL_KEYS = ("prefix", "mode", "readable", _RAW_ROWS)


def _published(side: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in side.items() if key not in _INTERNAL_KEYS}
