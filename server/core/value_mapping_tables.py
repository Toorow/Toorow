"""toorow -- client-authored value mapping tables (Story 60.1).

A value mapping table is the client's OWN vocabulary: a name, a description, and
a list of `source_value -> canonical_value` pairs, assigned to Datastreams on a
named raw column. It is not the governed conformance store of migration 052 and
it does not replace it -- see the header of migration 235 for the arbitrage and
for the debt it names. WHICH STORE WINS AT RENDER TIME IS STILL AI-238 AND STILL
OPEN: story 60.5 gave this object a history, not an application, and it wrote the
measured precedence it should be closed with in
`core/rule_versions.AI_238_PRECEDENCE` so its owner does not re-derive it.

Four contracts this module exists to hold:

1. **An impact read that fails RAISES.** :func:`assess_table_impact` raises
   :class:`ValueMappingUnavailable` rather than returning an empty tuple, exactly
   like ``master_data.assess_node_impact`` (`master_data.py:745-749`): "I could
   not check" and "nothing depends on this" are different facts and only one of
   them is safe to act on. Every mutation that could break a consumer reads the
   impact FIRST and refuses without an explicit acknowledgement.
2. **PLATFORM is not a scope here.** ORG and PROJECT only, in the CHECK of the
   migration and in :data:`VALID_SCOPES`. Seeds are the platform authority.
3. **An import is never partial in silence.** :func:`parse_pairs` returns the
   accepted pairs AND every rejected line with its line number and a typed
   reason; the caller reports both.
4. **No write leaves without a version.** Story 60.5: every mutation of a table
   or of its pairs calls :func:`record_table_version` on the SAME connection, so
   what the table was before the change stays readable. One version per ACT --
   an import of five hundred pairs is one row, not five hundred.

No vocabulary of any client, connector or dimension appears in this file: the
same interdiction `geographic_conformance.py:20-22` states for aliases.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from typing import Any

from core.metric_semantics import (  # the socle: id minting + the shared audit
    _mint_id,
    _write_semantics_audit,
)

logger = logging.getLogger(__name__)

ID_PREFIX_TABLE = "vmt_"
ID_PREFIX_ENTRY = "vment_"
ID_PREFIX_ASSIGNMENT = "vmasg_"

SCOPE_ORG = "ORG"
SCOPE_PROJECT = "PROJECT"
#: PLATFORM is deliberately absent, both here and in the CHECK of migration 235.
VALID_SCOPES = frozenset({SCOPE_ORG, SCOPE_PROJECT})

ENTITY_TABLE = "value_mapping_table"
ENTITY_ENTRY = "value_mapping_entry"
ENTITY_ASSIGNMENT = "value_mapping_assignment"

#: Bound on one import. A file larger than this is refused whole rather than
#: truncated: a partially imported vocabulary is worse than none, because the
#: rows that did land look like a complete answer.
MAX_IMPORT_ROWS = 5000

_DELIMITERS = (",", ";", "\t")
_HEADER = ("source_value", "canonical_value")


# ---------------------------------------------------------------------------
# Typed errors. The API layer maps these to status codes; core raises no HTTP.
# ---------------------------------------------------------------------------


class ValueMappingError(Exception):
    """Base for every refusal of this store."""


class ValueMappingNotFound(ValueMappingError):
    """The table, entry or assignment does not exist in the guarded org."""


class ValueMappingConflict(ValueMappingError):
    """A unique constraint refused the write (duplicate name, source or triplet)."""


class InvalidValueMappingScope(ValueMappingError):
    """The scope triplet is inconsistent, or PLATFORM was asked for."""


class ValueMappingUnavailable(ValueMappingError):
    """The impact store could not be read. Fail closed -- never a zero."""


class ImpactNotAcknowledged(ValueMappingError):
    """Live consumers block a change nobody acknowledged.

    Carries the impact that was READ, so the caller shows the same evidence the
    guard used rather than re-reading it after the refusal.
    """

    def __init__(self, message: str, *, impact: dict[str, Any] | None = None):
        super().__init__(message)
        self.impact = impact or {}


# ---------------------------------------------------------------------------
# PURE helpers -- no I/O, offline-testable.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RejectedRow:
    """One import line that was not accepted, named rather than dropped."""

    line: int
    reason: str
    raw: str

    def as_dict(self) -> dict[str, Any]:
        return {"line": self.line, "reason": self.reason, "raw": self.raw}


def _detect_delimiter(sample: str) -> str:
    """The separator of the first non-empty line, counted rather than sniffed.

    `csv.Sniffer` guesses on ambiguous input and raises on a single-column file,
    which is precisely the malformed case this module has to REPORT rather than
    crash on.
    """
    for line in sample.splitlines():
        if not line.strip():
            continue
        counts = {delimiter: line.count(delimiter) for delimiter in _DELIMITERS}
        best = max(counts, key=lambda key: counts[key])
        return best if counts[best] else ","
    return ","


def parse_pairs(text: str) -> tuple[list[tuple[str, str]], list[RejectedRow]]:
    """Split a two-column import into accepted pairs and NAMED rejections.

    A line that does not carry exactly two non-empty columns is rejected with its
    line number and a reason -- never skipped, and never allowed to make the rest
    of the file look complete. A source value repeated inside the same file is
    rejected too: the store holds one live pair per source value, so accepting
    the last one would silently discard the first.
    """
    accepted: list[tuple[str, str]] = []
    rejected: list[RejectedRow] = []
    seen: set[str] = set()

    delimiter = _detect_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    for index, row in enumerate(reader, start=1):
        raw = delimiter.join(row)
        cells = [cell.strip() for cell in row]
        if not any(cells):
            continue  # a blank line is not a row; nothing was claimed by it
        if index == 1 and len(cells) == 2 and tuple(c.lower() for c in cells) == _HEADER:
            continue  # the header names the two columns; it is not a pair
        if len(cells) != 2:
            rejected.append(RejectedRow(index, "not_two_columns", raw))
            continue
        source, canonical = cells
        if not source:
            rejected.append(RejectedRow(index, "empty_source_value", raw))
            continue
        if not canonical:
            rejected.append(RejectedRow(index, "empty_canonical_value", raw))
            continue
        if source in seen:
            rejected.append(RejectedRow(index, "duplicate_source_value", raw))
            continue
        seen.add(source)
        accepted.append((source, canonical))
    return accepted, rejected


def validate_scope(scope_level: str, org_id: str | None, project_id: str | None) -> None:
    """Raise :class:`InvalidValueMappingScope` on an inconsistent triplet."""
    if scope_level == "PLATFORM":
        raise InvalidValueMappingScope("PLATFORM is not a scope of a client value table")
    if scope_level not in VALID_SCOPES:
        raise InvalidValueMappingScope(f"unknown scope_level: {scope_level!r}")
    if not org_id:
        raise InvalidValueMappingScope("org_id is required")
    if scope_level == SCOPE_ORG and project_id is not None:
        raise InvalidValueMappingScope("ORG scope requires a NULL project_id")
    if scope_level == SCOPE_PROJECT and not project_id:
        raise InvalidValueMappingScope("PROJECT scope requires project_id")


def _required(value: str | None, name: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _rows(cur) -> list[dict[str, Any]]:
    columns = [description[0] for description in cur.description]
    return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def _is_unique_violation(exc: Exception) -> bool:
    """A duplicate is a CONFLICT, not a server error.

    Matched on the SQLSTATE when psycopg exposes one and on the message
    otherwise, so an offline fake store can raise the same refusal.
    """
    sqlstate = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
    if sqlstate == "23505":
        return True
    text = str(exc).lower()
    return "duplicate key" in text or "unique constraint" in text


# ---------------------------------------------------------------------------
# The impact -- read BEFORE any change that could break a consumer.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TableImpact:
    """Who this table is applied to right now.

    An empty ``assignments`` means the store answered and there are none. It
    never means the store could not be read: :func:`assess_table_impact` raises
    in that case rather than returning an encouraging zero.
    """

    table_id: str
    assignments: tuple[dict[str, Any], ...]

    @property
    def is_clear(self) -> bool:
        return not self.assignments

    @property
    def datastream_count(self) -> int:
        return len({str(a["datastream_id"]) for a in self.assignments})

    def describe(self) -> str:
        count = self.datastream_count
        return f"{count} Datastream{'' if count == 1 else 's'}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "impact_state": "known",
            "datastream_count": self.datastream_count,
            "assignment_count": len(self.assignments),
            "assignments": [dict(a) for a in self.assignments],
        }


def assess_table_impact(conn, *, table_id: str) -> TableImpact:
    """Live assignments of one table, or an outage -- never a reassuring zero.

    Raises:
        ValueMappingUnavailable: the assignment store could not be read. Every
            caller must fail closed; the surface says "impact unknown".
    """
    table_id = _required(table_id, "table_id")
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.id, a.datastream_id, a.source_field, a.created_at,
                       d.name AS datastream_name
                  FROM app.value_mapping_assignments a
                  LEFT JOIN app.datastreams d ON d.id = a.datastream_id
                 WHERE a.table_id = %s
                 ORDER BY a.datastream_id, a.source_field
                """,
                (table_id,),
            )
            rows = _rows(cur)
    except Exception as exc:  # noqa: BLE001 -- any read failure is an outage here
        raise ValueMappingUnavailable(
            f"the assignment store could not be read for table {table_id}: {type(exc).__name__}"
        ) from exc
    return TableImpact(
        table_id=table_id,
        assignments=tuple(
            {
                "assignment_id": str(row["id"]),
                "datastream_id": str(row["datastream_id"]),
                "datastream_name": row.get("datastream_name"),
                "source_field": row["source_field"],
            }
            for row in rows
        ),
    )


#: Story 60.6. Named so the rollback below names the same point it opened.
_ASSIGNED_FIELDS_SAVEPOINT = "value_mapping_assigned_fields"


def _savepoint(conn, statement: str) -> bool:
    """Run one savepoint statement, best effort. `False` if it did not take.

    Never raises. `ROLLBACK TO SAVEPOINT` is one of the two statements PostgreSQL
    still accepts on a poisoned transaction, so it is exactly the call that has
    to work when everything else has stopped working; and `SAVEPOINT` outside a
    transaction block is refused, which is the autocommit case where there is
    nothing to poison. Same shape as `dq_governance._savepoint`, and for the same
    measured reason.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(statement)
    except Exception as exc:  # noqa: BLE001 -- a savepoint is a precaution, not a result
        logger.debug("value_mapping: %s unavailable: %s", statement, exc)
        return False
    return True


def read_assigned_source_fields(
    conn, *, project_id: str, datastream_id: str
) -> frozenset[str] | None:
    """Which raw columns of one Datastream a value table is assigned to, or None.

    Story 60.6: the one input the per-column treatment reading cannot derive from
    a mapping payload, because an assignment is a Datastream-scoped row and not a
    property of any mapping version.

    THIS IS THE FAIL-SOFT ENTRY POINT, and it lives HERE rather than in the
    reader that needs it. Catching the Python exception is not enough: a failed
    statement leaves its transaction aborted, so a caller that swallowed the
    error would take down whatever it read next on the same connection. A
    SAVEPOINT is what un-poisons it -- the same measured reason
    `dq_governance.read_open_issue_counts` states at length.

    `None` means the store could not be read; `frozenset()` means it answered and
    no table is assigned. They are two facts, and only one of them is safe to
    render as "this column is not resolved by a list".
    """
    marked = _savepoint(conn, f"SAVEPOINT {_ASSIGNED_FIELDS_SAVEPOINT}")
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.source_field
                  FROM app.value_mapping_assignments a
                  JOIN app.datastreams d ON d.id = a.datastream_id
                 WHERE a.datastream_id = %s AND d.project_id = %s
                """,
                (datastream_id, project_id),
            )
            rows = cur.fetchall() or []
    except Exception as exc:  # noqa: BLE001 -- one unreadable column takes down no screen
        logger.warning(
            "value_mapping: assigned source fields unavailable ds=%s: %s", datastream_id, exc
        )
        if marked:
            _savepoint(conn, f"ROLLBACK TO SAVEPOINT {_ASSIGNED_FIELDS_SAVEPOINT}")
        return None
    if marked:
        _savepoint(conn, f"RELEASE SAVEPOINT {_ASSIGNED_FIELDS_SAVEPOINT}")
    return frozenset(str(row[0]) for row in rows if row and row[0])


def read_field_assignments(
    conn, *, datastream_ids: list[str], source_field: str
) -> list[dict[str, Any]]:
    """The live assignments of SEVERAL Datastreams on ONE exact raw column.

    AI-260: the read the render-time bridge (`value_table_resolution.py`) needs
    and neither :func:`assess_table_impact` (per table) nor
    :func:`read_assigned_source_fields` (per Datastream, fields only) provides.
    It lives HERE so the store's SQL stays in its owner -- the same discipline
    `test_value_mapping_precedence.test_only_their_owners_name_the_two_stores`
    polices with a grep.

    ``source_field`` is matched by STRING EQUALITY, never translated: the
    correspondence between a raw collected column and what a read consumes is
    stated by the client when the assignment names the field. Guessing it is
    the resolver AI-260 forbids.

    Raises whatever the store raises; the bridge is the fail-soft layer.
    """
    ids = [str(value) for value in datastream_ids if value]
    if not ids:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.datastream_id, a.table_id, t.name AS table_name,
                   d.name AS datastream_name
              FROM app.value_mapping_assignments a
              JOIN app.value_mapping_tables t ON t.id = a.table_id
              LEFT JOIN app.datastreams d ON d.id = a.datastream_id
             WHERE a.datastream_id = ANY(%s) AND a.source_field = %s
             ORDER BY a.datastream_id, a.table_id
            """,
            (ids, _required(source_field, "source_field")),
        )
        rows = _rows(cur)
    return [
        {
            "datastream_id": str(row["datastream_id"]),
            "datastream_name": row.get("datastream_name"),
            "table_id": str(row["table_id"]),
            "table_name": row.get("table_name"),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# The immutable history (Story 60.5, migration 242).
# ---------------------------------------------------------------------------


def record_table_version(
    conn, *, table_id: str, org_id: str, identity: str
) -> dict[str, Any] | None:
    """Record where this table now stands, in the SAME transaction as the write.

    One version per ACT, never one per pair: the body carries the name, the
    description, the scope and every pair sorted by source value, so an import of
    five hundred lines is ONE row of `app.value_mapping_table_versions`.

    Returns the version, or ``None`` when the table was already at this exact
    body -- an edit that changed nothing records nothing, which is not a failure.

    It is called AFTER the mutation and reads the table back, so what is recorded
    is what the store now holds rather than what the caller believed it was
    writing.
    """
    from core.rule_versions import (  # noqa: PLC0415 -- one ledger authority, imported late
        KIND_VALUE_TABLE,
        RuleVersionUnchanged,
        record_version,
        value_table_body,
    )

    table = get_table(conn, table_id=table_id, org_id=org_id)
    pairs = [
        (entry["source_value"], entry["canonical_value"])
        for entry in list_entries(conn, table_id=table["id"])
    ]
    try:
        return record_version(
            conn,
            kind=KIND_VALUE_TABLE,
            object_id=table["id"],
            org_id=org_id,
            project_id=table["project_id"],
            body=value_table_body(table, pairs),
            identity=identity,
        )
    except RuleVersionUnchanged:
        return None


def _guard_impact(conn, table_id: str, acknowledge_impact: bool, verb: str) -> TableImpact:
    impact = assess_table_impact(conn, table_id=table_id)
    if not impact.is_clear and not acknowledge_impact:
        raise ImpactNotAcknowledged(
            f"This table is applied to {impact.describe()}. "
            f"To {verb} it without acknowledgement would change what those "
            "Datastreams render. Confirm by acknowledging the impact shown.",
            impact=impact.as_dict(),
        )
    return impact


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

def _table_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "org_id": str(row["org_id"]),
        "project_id": row.get("project_id"),
        "scope_level": row["scope_level"],
        "name": row["name"],
        "description": row.get("description"),
        "created_by": row.get("created_by"),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        # The head of the table's own history (Story 60.5, migration 242). NULL
        # only for a table written before that migration -- nothing backfills it,
        # because inventing a version 1 would date a change nobody observed.
        "current_version_id": row.get("current_version_id"),
        "entry_count": int(row["entry_count"]) if row.get("entry_count") is not None else None,
        "assignment_count": (
            int(row["assignment_count"]) if row.get("assignment_count") is not None else None
        ),
        "datastream_count": (
            int(row["datastream_count"]) if row.get("datastream_count") is not None else None
        ),
        "sample_source_field": row.get("sample_source_field"),
    }


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


#: The list read. The counts are computed by the SAME query as the rows, so a
#: table can never be listed beside a count that came from another moment.
LIST_TABLES_SQL = """
    SELECT t.id, t.org_id, t.project_id, t.scope_level, t.name, t.description,
           t.created_by, t.created_at, t.updated_at, t.current_version_id,
           e.entry_count, a.assignment_count, a.datastream_count, a.sample_source_field
      FROM app.value_mapping_tables t
      LEFT JOIN LATERAL (
          SELECT COUNT(*) AS entry_count
            FROM app.value_mapping_entries en
           WHERE en.table_id = t.id
      ) e ON TRUE
      LEFT JOIN LATERAL (
          SELECT COUNT(*) AS assignment_count,
                 COUNT(DISTINCT asg.datastream_id) AS datastream_count,
                 MIN(asg.source_field) AS sample_source_field
            FROM app.value_mapping_assignments asg
           WHERE asg.table_id = t.id
      ) a ON TRUE
     WHERE t.org_id = %(org_id)s
       AND (t.scope_level = 'ORG' OR t.project_id = %(project_id)s)
     ORDER BY lower(t.name), t.id
"""

#: The single-table read. Deliberately the same projection as the list above,
#: with the selector swapped: a table read alone and the same table read in the
#: list must not disagree about its own counts.
GET_TABLE_SQL = LIST_TABLES_SQL.replace(
    "WHERE t.org_id = %(org_id)s\n       "
    "AND (t.scope_level = 'ORG' OR t.project_id = %(project_id)s)",
    "WHERE t.org_id = %(org_id)s AND t.id = %(table_id)s",
)


def list_tables(conn, *, org_id: str, project_id: str | None) -> list[dict[str, Any]]:
    """Every table this Project can see: its own, plus the org-scoped ones."""
    org_id = _required(org_id, "org_id")
    with conn.cursor() as cur:
        cur.execute(LIST_TABLES_SQL, {"org_id": org_id, "project_id": project_id})
        rows = _rows(cur)
    return [_table_row(row) for row in rows]


def get_table(conn, *, table_id: str, org_id: str) -> dict[str, Any]:
    """One exact table of the guarded org, or :class:`ValueMappingNotFound`.

    It carries the SAME counts as the list read, from the same query shape: a
    table read on its own and a table read in the list must never disagree about
    how many pairs and how many Datastreams it carries.
    """
    table_id = _required(table_id, "table_id")
    org_id = _required(org_id, "org_id")
    with conn.cursor() as cur:
        cur.execute(GET_TABLE_SQL, {"table_id": table_id, "org_id": org_id})
        rows = _rows(cur)
    if not rows:
        raise ValueMappingNotFound(table_id)
    return _table_row(rows[0])


def create_table(
    conn,
    *,
    org_id: str,
    project_id: str | None,
    scope_level: str,
    name: str,
    description: str | None,
    identity: str,
) -> dict[str, Any]:
    """Create one named table. ORG or PROJECT scope; PLATFORM is refused."""
    org_id = _required(org_id, "org_id")
    name = _required(name, "name")
    identity = _required(identity, "identity")
    scope_level = (scope_level or "").strip().upper()
    scoped_project = project_id if scope_level == SCOPE_PROJECT else None
    validate_scope(scope_level, org_id, scoped_project)

    table_id = _mint_id(ID_PREFIX_TABLE)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.value_mapping_tables
                    (id, org_id, project_id, scope_level, name, description, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (table_id, org_id, scoped_project, scope_level, name, description, identity),
            )
    except Exception as exc:  # noqa: BLE001
        if _is_unique_violation(exc):
            raise ValueMappingConflict(
                "A value mapping table already carries this name in this scope."
            ) from exc
        raise
    _write_semantics_audit(
        conn,
        identity=identity,
        action="created",
        entity_type=ENTITY_TABLE,
        entity_id=table_id,
        scope_level=scope_level,
        org_id=org_id,
        project_id=scoped_project,
        before=None,
        after={"name": name, "description": description, "scope_level": scope_level},
    )
    # Version 1 is written HERE and not on the first edit: without it, the first
    # edit would have no predecessor to read and "what it was yesterday" would
    # start one change too late.
    record_table_version(conn, table_id=table_id, org_id=org_id, identity=identity)
    return get_table(conn, table_id=table_id, org_id=org_id)


def update_table(
    conn,
    *,
    table_id: str,
    org_id: str,
    name: str | None,
    description: str | None,
    identity: str,
    acknowledge_impact: bool = False,
) -> dict[str, Any]:
    """Rename or re-describe a table, AFTER its impact has been acknowledged."""
    before = get_table(conn, table_id=table_id, org_id=org_id)
    _guard_impact(conn, before["id"], acknowledge_impact, "change")

    new_name = (name or "").strip() or before["name"]
    new_description = description if description is not None else before["description"]
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.value_mapping_tables SET name = %s, description = %s "
                "WHERE id = %s AND org_id = %s",
                (new_name, new_description, before["id"], org_id),
            )
    except Exception as exc:  # noqa: BLE001
        if _is_unique_violation(exc):
            raise ValueMappingConflict(
                "A value mapping table already carries this name in this scope."
            ) from exc
        raise
    _write_semantics_audit(
        conn,
        identity=identity,
        action="upserted",
        entity_type=ENTITY_TABLE,
        entity_id=before["id"],
        scope_level=before["scope_level"],
        org_id=org_id,
        project_id=before["project_id"],
        before={"name": before["name"], "description": before["description"]},
        after={"name": new_name, "description": new_description},
    )
    record_table_version(conn, table_id=before["id"], org_id=org_id, identity=identity)
    return get_table(conn, table_id=before["id"], org_id=org_id)


def delete_table(
    conn, *, table_id: str, org_id: str, identity: str, acknowledge_impact: bool = False
) -> dict[str, Any]:
    """Remove a table and everything hanging off it, impact acknowledged first."""
    before = get_table(conn, table_id=table_id, org_id=org_id)
    impact = _guard_impact(conn, before["id"], acknowledge_impact, "delete")
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM app.value_mapping_tables WHERE id = %s AND org_id = %s",
            (before["id"], org_id),
        )
    _write_semantics_audit(
        conn,
        identity=identity,
        action="deleted",
        entity_type=ENTITY_TABLE,
        entity_id=before["id"],
        scope_level=before["scope_level"],
        org_id=org_id,
        project_id=before["project_id"],
        before={"name": before["name"], "assignments": list(impact.assignments)},
        after=None,
    )
    return {"deleted": True, "table_id": before["id"], "impact": impact.as_dict()}


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------


def list_entries(conn, *, table_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, table_id, source_value, canonical_value, created_by, "
            "created_at, updated_at FROM app.value_mapping_entries "
            "WHERE table_id = %s ORDER BY lower(source_value), id",
            (_required(table_id, "table_id"),),
        )
        rows = _rows(cur)
    return [
        {
            "id": str(row["id"]),
            "table_id": str(row["table_id"]),
            "source_value": row["source_value"],
            "canonical_value": row["canonical_value"],
            "created_at": _iso(row.get("created_at")),
            "updated_at": _iso(row.get("updated_at")),
        }
        for row in rows
    ]


def add_entry(
    conn,
    *,
    table_id: str,
    org_id: str,
    source_value: str,
    canonical_value: str,
    identity: str,
    record_version: bool = True,
) -> dict[str, Any]:
    """Add one pair, and record the table's new body as a version.

    `record_version=False` exists for ONE caller: :func:`import_pairs`, which is
    a single act and records a single version once every accepted line has landed.
    Versioning per line would give the ledger five hundred rows for one import --
    exactly what "a version per table, never per pair" refuses.
    """
    table = get_table(conn, table_id=table_id, org_id=org_id)
    source_value = _required(source_value, "source_value")
    canonical_value = _required(canonical_value, "canonical_value")
    entry_id = _mint_id(ID_PREFIX_ENTRY)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.value_mapping_entries "
                "(id, table_id, source_value, canonical_value, created_by) "
                "VALUES (%s, %s, %s, %s, %s)",
                (entry_id, table["id"], source_value, canonical_value, identity),
            )
    except Exception as exc:  # noqa: BLE001
        if _is_unique_violation(exc):
            raise ValueMappingConflict(
                "This source value already carries a mapping in this table."
            ) from exc
        raise
    _write_semantics_audit(
        conn,
        identity=identity,
        action="created",
        entity_type=ENTITY_ENTRY,
        entity_id=entry_id,
        scope_level=table["scope_level"],
        org_id=org_id,
        project_id=table["project_id"],
        before=None,
        after={"source_value": source_value, "canonical_value": canonical_value},
    )
    if record_version:
        record_table_version(conn, table_id=table["id"], org_id=org_id, identity=identity)
    return {
        "id": entry_id,
        "table_id": table["id"],
        "source_value": source_value,
        "canonical_value": canonical_value,
    }


def update_entry(
    conn, *, table_id: str, org_id: str, entry_id: str, canonical_value: str, identity: str
) -> dict[str, Any]:
    """Change what one source value becomes. The source value itself is stable:
    editing it would be a different pair, and the surface creates one."""
    table = get_table(conn, table_id=table_id, org_id=org_id)
    canonical_value = _required(canonical_value, "canonical_value")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_value, canonical_value FROM app.value_mapping_entries "
            "WHERE id = %s AND table_id = %s",
            (_required(entry_id, "entry_id"), table["id"]),
        )
        rows = _rows(cur)
    if not rows:
        raise ValueMappingNotFound(entry_id)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.value_mapping_entries SET canonical_value = %s "
            "WHERE id = %s AND table_id = %s",
            (canonical_value, entry_id, table["id"]),
        )
    _write_semantics_audit(
        conn,
        identity=identity,
        action="upserted",
        entity_type=ENTITY_ENTRY,
        entity_id=entry_id,
        scope_level=table["scope_level"],
        org_id=org_id,
        project_id=table["project_id"],
        before=dict(rows[0]),
        after={"source_value": rows[0]["source_value"], "canonical_value": canonical_value},
    )
    record_table_version(conn, table_id=table["id"], org_id=org_id, identity=identity)
    return {
        "id": entry_id,
        "table_id": table["id"],
        "source_value": rows[0]["source_value"],
        "canonical_value": canonical_value,
    }


def delete_entry(
    conn, *, table_id: str, org_id: str, entry_id: str, identity: str
) -> dict[str, Any]:
    table = get_table(conn, table_id=table_id, org_id=org_id)
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM app.value_mapping_entries WHERE id = %s AND table_id = %s "
            "RETURNING source_value, canonical_value",
            (_required(entry_id, "entry_id"), table["id"]),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueMappingNotFound(entry_id)
    _write_semantics_audit(
        conn,
        identity=identity,
        action="deleted",
        entity_type=ENTITY_ENTRY,
        entity_id=entry_id,
        scope_level=table["scope_level"],
        org_id=org_id,
        project_id=table["project_id"],
        before={"source_value": row[0], "canonical_value": row[1]},
        after=None,
    )
    record_table_version(conn, table_id=table["id"], org_id=org_id, identity=identity)
    return {"deleted": True, "entry_id": entry_id}


def preview_import(
    conn, *, table_id: str, org_id: str, text: str
) -> dict[str, Any]:
    """Classify a file EXACTLY as `import_pairs` would, and write nothing.

    `unresolved-values.md` S4: *"`Import a filled file` previews before writing:
    accepted rows, and every rejected row with its line number and reason
    (`empty_canonical_value`, `not_two_columns`, `duplicate_source_value`). Never
    a partial write in silence."* `import_pairs` reports its rejections honestly
    -- but it reports them AFTER writing the accepted half, so a person who
    misread their own file learns it once the table has changed.

    THE CLASSIFICATION IS NOT RE-IMPLEMENTED, AND THAT IS THE WHOLE POINT. A
    second copy of "which lines would be refused" is a second answer, and the day
    the two disagree the preview becomes a lie told with a straight face. This
    calls the same `parse_pairs`, applies the same `MAX_IMPORT_ROWS` ceiling and
    the same `already_present` rule against the same `list_entries`, in the same
    order. `test_the_preview_classifies_exactly_what_the_import_writes` plays both
    against one file and compares the verdicts line by line.

    It takes a connection because `already_present` is a fact about the table,
    not about the file -- and it only reads.
    """
    table = get_table(conn, table_id=table_id, org_id=org_id)
    accepted, rejected = parse_pairs(text or "")
    over_limit = len(accepted) > MAX_IMPORT_ROWS

    existing = {entry["source_value"] for entry in list_entries(conn, table_id=table["id"])}
    would_import: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, (source, canonical) in enumerate(accepted, start=1):
        if source in existing or source in seen:
            rejected.append(RejectedRow(index, "already_present", f"{source} -> {canonical}"))
            continue
        seen.add(source)
        would_import.append({"source_value": source, "canonical_value": canonical})

    return {
        "table_id": table["id"],
        "table_name": table.get("name"),
        # `would_*`, never `imported_*`: the two shapes must not be confusable at
        # the call site, or a screen renders a preview as a receipt.
        "would_import_count": len(would_import),
        "rejected_count": len(rejected),
        "would_import": would_import,
        "rejected": [row.as_dict() for row in rejected],
        # The refusal `import_pairs` RAISES, stated here as a field so the drawer
        # can disable its confirm instead of discovering it at the click.
        "over_limit": over_limit,
        "limit": MAX_IMPORT_ROWS,
        "impact": _impact_or_unknown(conn, table["id"]) if would_import else None,
    }


def _impact_or_unknown(conn, table_id: str) -> dict[str, Any]:
    """The impact, or the WORD unknown -- never an encouraging zero.

    `unresolved-values.md` S3.4: *"Unreadable impact reads `unknown` and disables
    the confirm -- 'I could not check' is not 'nothing depends on this'."*
    `assess_table_impact` raises rather than returning zero, exactly so this
    branch has something to catch; swallowing it into `datastream_count: 0` is
    the failure the docstring at :230 was written to prevent.
    """
    try:
        return assess_table_impact(conn, table_id=table_id).as_dict()
    except Exception as exc:  # noqa: BLE001
        logger.warning("value_mapping_tables: impact unreadable for %s: %s", table_id, exc)
        return {
            "impact_state": "unknown",
            "datastream_count": None,
            "assignment_count": None,
            "assignments": [],
            "message": (
                "What depends on this table could not be read, so this import is "
                "held. Reload the screen; if it stays unreadable the warehouse "
                "link is down and nothing here can be confirmed safely."
            ),
        }


def import_pairs(
    conn, *, table_id: str, org_id: str, text: str, identity: str
) -> dict[str, Any]:
    """Import a two-column file. Rejections are NAMED, never silently dropped.

    Every accepted pair is written; every refused line comes back with its line
    number and reason, and the count of each is reported. A source value the
    table already holds is reported as `already_present` rather than overwritten:
    an import must not rewrite a decision the client made by hand without saying
    so.
    """
    table = get_table(conn, table_id=table_id, org_id=org_id)
    accepted, rejected = parse_pairs(text or "")
    if len(accepted) > MAX_IMPORT_ROWS:
        raise ValueMappingConflict(
            f"This import carries {len(accepted)} pairs, beyond the limit of "
            f"{MAX_IMPORT_ROWS}. NOTHING has been written."
        )

    existing = {entry["source_value"] for entry in list_entries(conn, table_id=table["id"])}
    imported: list[dict[str, Any]] = []
    for index, (source, canonical) in enumerate(accepted, start=1):
        if source in existing:
            rejected.append(RejectedRow(index, "already_present", f"{source} -> {canonical}"))
            continue
        imported.append(
            add_entry(
                conn,
                table_id=table["id"],
                org_id=org_id,
                source_value=source,
                canonical_value=canonical,
                identity=identity,
                # ONE import is ONE act, so it is ONE version -- recorded below,
                # after the last accepted line. Story 60.5, Arbitrage 2.
                record_version=False,
            )
        )
        existing.add(source)
    if imported:
        record_table_version(conn, table_id=table["id"], org_id=org_id, identity=identity)
    return {
        "table_id": table["id"],
        "imported_count": len(imported),
        "rejected_count": len(rejected),
        "imported": imported,
        "rejected": [row.as_dict() for row in rejected],
    }


# ---------------------------------------------------------------------------
# Assignments
# ---------------------------------------------------------------------------


def create_assignment(
    conn,
    *,
    table_id: str,
    org_id: str,
    datastream_id: str,
    source_field: str,
    identity: str,
) -> dict[str, Any]:
    """Apply a table to one Datastream on one RAW column.

    `source_field` carries no foreign key to `app.datastream_mappings`, for the
    reason `122_derived_columns.sql:62-64` states: a rule may read a column that
    was never mapped, and a value table exists to normalise a column BEFORE
    anything maps it.
    """
    table = get_table(conn, table_id=table_id, org_id=org_id)
    datastream_id = _required(datastream_id, "datastream_id")
    source_field = _required(source_field, "source_field")
    assignment_id = _mint_id(ID_PREFIX_ASSIGNMENT)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.value_mapping_assignments "
                "(id, table_id, datastream_id, org_id, source_field, created_by) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (assignment_id, table["id"], datastream_id, org_id, source_field, identity),
            )
    except Exception as exc:  # noqa: BLE001
        if _is_unique_violation(exc):
            raise ValueMappingConflict(
                "This table is already applied to that Datastream on that field."
            ) from exc
        raise
    _write_semantics_audit(
        conn,
        identity=identity,
        action="created",
        entity_type=ENTITY_ASSIGNMENT,
        entity_id=assignment_id,
        scope_level=table["scope_level"],
        org_id=org_id,
        project_id=table["project_id"],
        before=None,
        after={
            "table_id": table["id"],
            "datastream_id": datastream_id,
            "source_field": source_field,
        },
    )
    return {
        "assignment_id": assignment_id,
        "table_id": table["id"],
        "datastream_id": datastream_id,
        "source_field": source_field,
    }


def delete_assignment(
    conn, *, table_id: str, org_id: str, assignment_id: str, identity: str
) -> dict[str, Any]:
    table = get_table(conn, table_id=table_id, org_id=org_id)
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM app.value_mapping_assignments WHERE id = %s AND table_id = %s "
            "RETURNING datastream_id, source_field",
            (_required(assignment_id, "assignment_id"), table["id"]),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueMappingNotFound(assignment_id)
    _write_semantics_audit(
        conn,
        identity=identity,
        action="deleted",
        entity_type=ENTITY_ASSIGNMENT,
        entity_id=assignment_id,
        scope_level=table["scope_level"],
        org_id=org_id,
        project_id=table["project_id"],
        before={"datastream_id": row[0], "source_field": row[1]},
        after=None,
    )
    return {"deleted": True, "assignment_id": assignment_id}


__all__ = [
    "ENTITY_ASSIGNMENT",
    "ENTITY_ENTRY",
    "ENTITY_TABLE",
    "ID_PREFIX_ASSIGNMENT",
    "ID_PREFIX_ENTRY",
    "ID_PREFIX_TABLE",
    "GET_TABLE_SQL",
    "LIST_TABLES_SQL",
    "MAX_IMPORT_ROWS",
    "SCOPE_ORG",
    "SCOPE_PROJECT",
    "VALID_SCOPES",
    "ImpactNotAcknowledged",
    "InvalidValueMappingScope",
    "RejectedRow",
    "TableImpact",
    "ValueMappingConflict",
    "ValueMappingError",
    "ValueMappingNotFound",
    "ValueMappingUnavailable",
    "add_entry",
    "assess_table_impact",
    "create_assignment",
    "create_table",
    "delete_assignment",
    "delete_entry",
    "delete_table",
    "get_table",
    "import_pairs",
    "list_entries",
    "list_tables",
    "parse_pairs",
    "read_assigned_source_fields",
    "read_field_assignments",
    "record_table_version",
    "update_entry",
    "update_table",
]
