"""toorow -- the bridge that lets a client value mapping table reach a served read (AI-260).

A value table assignment is written on ``(datastream_id, source_field)`` (migration
235). ``fact_daily_kpi`` carries NO ``datastream_id`` -- two marts state it in their
own headers (`fee_tax_country_resolution.sql`, `fee_tax_rules_effective.sql`) -- so
every read over the fact keys on **connector**. This module is the bridge between the
two keys, and it is the SAME bridge the warehouse already crossed for source types:
a connector is resolved to its Datastreams through the datastream dim, with the rule
that comes with it, quoted from `fee_tax_country_resolution.sql:304-305` because it
is ratified there:

    "two datastreams of one connector that do not resolve to the SAME source type
    is an AMBIGUITY -- a gap, never a pick."

Transposed to value tables, verbatim in structure: two Datastreams of one connector
that do not resolve to the SAME table on the named field is an ambiguity. So is a
table assigned on SOME Datastreams of the connector and not on others -- applying it
to connector-keyed rows would translate rows of the unassigned Datastream, which is a
pick, and not applying it would ignore what the client stated. Both cases come back
NAMED (which Datastreams, which tables), never chosen between. That mirrors the
mart's own ``declared_count < datastream_count`` rung exactly.

THE RELATION READ IS THE AUTHORITY OF THE MIRROR. `mirror.datastreams_dim` is
`app.datastreams_dim_v` copied verbatim into the warehouse (`mirror_sync.py:131`,
migration 119:532). This module runs on the control plane, so it reads the view the
mirror is made from -- the same rows, the same live-only filter -- and emits NO
warehouse SQL at all. The two-dialect obligation `test_cleanup_rule_dialects` holds
60.3 to binds SQL compiled toward the warehouse; nothing here compiles any, which is
why it does not bind here (the resolution applies in Python at the matrix layer,
the same locus where 27.5 already reproduces the mart's ventilation).

WHAT IS FORBIDDEN, ratified under AI-260: "a resolver that guesses the
correspondence between a raw collected column and a breakdown value". This module
never translates a field name. An assignment applies to a read if and only if its
``source_field`` EQUALS -- string equality, the same exact-match rule rung 1 of the
country ladder applies to its partition -- the field the read actually consumes.
The correspondence is stated by the client when the assignment names the field; it
is never inferred here.

THE PRECEDENCE APPLIED is `core.rule_versions.AI_238_PRECEDENCE`, decided under
AI-238: a client value mapping table wins on the exact field its assignment names;
the governed conformance store of migration 052 wins everywhere else. Because that
is "a precedence and not a merge", a value the winning table does not name stays
UNCONFORMED on that field -- it does not fall through to 052, which would merge the
two stores on one field. :meth:`ValueTableResolution.answer` encodes exactly that.

AN OUTAGE IS NOT "NO TABLE". When the stores cannot be read the state is
``unavailable``, never ``none``: "I could not check" and "nothing depends on this"
are different facts (`value_mapping_tables.py`, contract 1). The caller decides what
an outage renders -- the plan-versus-actual matrix falls back to the governed store
and NAMES the outage in its output, because suppressing 052 over a question that
could not be asked would degrade a read on unknowable information. An AMBIGUITY, by
contrast, is a measured disagreement: it renders as a gap, never as either store.

Read-only, org-unguarded like `dimension_conformance.conform_value` (S-4): the
calling surface has checked access; the Datastreams read are the project's own.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

logger = logging.getLogger(__name__)

#: The four states a resolution can land in. ``none`` means the stores answered
#: and no table names this field -- migration 052 wins everywhere else.
STATE_APPLIED = "applied"
STATE_NONE = "none"
STATE_AMBIGUOUS = "ambiguous"
STATE_UNAVAILABLE = "unavailable"

#: Typed reasons, one word each, so a surface can branch without parsing prose.
#: `no_datastream_for_connector` reuses the mart's own gap word
#: (`fee_tax_country_resolution.sql:564`) -- no reason may mean something
#: different in the two engines (AI-254).
REASON_NO_DATASTREAM = "no_datastream_for_connector"
REASON_NO_ASSIGNMENT = "no_assignment_on_field"
REASON_TABLES_DISAGREE = "datastreams_disagree_on_table"
REASON_PARTIAL_ASSIGNMENT = "table_not_assigned_on_every_datastream"

#: Same shape and same measured reason as
#: `value_mapping_tables._ASSIGNED_FIELDS_SAVEPOINT`: a failed statement poisons
#: its transaction, and a caller that swallowed the Python exception would take
#: down whatever it reads next on the same connection.
_RESOLUTION_SAVEPOINT = "value_table_resolution"


@dataclass(frozen=True, slots=True)
class ValueTableResolution:
    """What one connector's Datastreams say about one exact field.

    ``datastreams`` names EVERY live Datastream of the connector with the table
    (if any) it assigns on the field, so an ambiguity is checkable by the person
    reading it rather than asserted at them.
    """

    state: str
    project_id: str
    connector: str
    source_field: str
    reason: str = ""
    table_id: str | None = None
    table_name: str | None = None
    #: source_value -> canonical_value of the resolved table. Empty unless applied.
    pairs: Mapping[str, str] = field(default_factory=dict)
    datastreams: tuple[dict[str, Any], ...] = ()

    def answer(self, source_value: str) -> tuple[bool, str | None]:
        """(handled, canonical) for one value of the resolved field.

        * ``applied`` -> (True, the table's canonical or None). On the field its
          assignment names the table ALONE answers: a value it does not name
          stays unconformed, and 052 is NOT consulted -- AI_238_PRECEDENCE is
          "a precedence and not a merge".
        * ``ambiguous`` -> (True, None). A measured disagreement is a gap, never
          a pick -- neither store answers on this field.
        * ``none`` / ``unavailable`` -> (False, None). Not handled here: the
          caller's governed path (052) answers, and an outage is NAMED by the
          caller rather than silently equated with "no table".
        """
        if self.state == STATE_APPLIED:
            return (True, self.pairs.get(source_value))
        if self.state == STATE_AMBIGUOUS:
            return (True, None)
        return (False, None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "connector": self.connector,
            "source_field": self.source_field,
            "state": self.state,
            "reason": self.reason,
            "table_id": self.table_id,
            "table_name": self.table_name,
            "datastreams": [dict(entry) for entry in self.datastreams],
        }


def _savepoint(conn, statement: str) -> bool:
    """Best-effort savepoint statement; `False` if it did not take. Never raises."""
    try:
        with conn.cursor() as cur:
            cur.execute(statement)
    except Exception as exc:  # noqa: BLE001 -- a savepoint is a precaution, not a result
        logger.debug("value_table_resolution: %s unavailable: %s", statement, exc)
        return False
    return True


def _connector_datastreams(conn, *, project_id: str, connector: str) -> list[str]:
    """The live Datastreams of one connector, from the relation the mirror mirrors.

    `app.datastreams_dim_v` (migration 119:532) is exactly what `mirror_sync`
    copies into `mirror.datastreams_dim`: live rows only, ``connector`` being
    ``module_name`` aliased. Reading the view rather than the base table keeps
    this bridge and the mart's bridge resolving the SAME set of Datastreams.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT datastream_id FROM app.datastreams_dim_v "
            "WHERE project_id = %s AND connector = %s "
            "ORDER BY datastream_id",
            (project_id, connector),
        )
        rows = cur.fetchall() or []
    return [str(row[0]) for row in rows if row and row[0]]


def resolve_value_table(
    conn, *, project_id: str, connector: str, source_field: str
) -> ValueTableResolution:
    """Resolve which client value table (if any) governs one field of one connector.

    The bridge, then the ratified rule:

    1. connector -> its live Datastreams through the dim the mirror is made from;
    2. those Datastreams' assignments on the EXACT ``source_field`` (string
       equality, never a translation), read through the store's own module;
    3. every Datastream assigns the same table -> ``applied``, with the table's
       pairs loaded; any disagreement -- two tables, or a table missing on some
       Datastream of the connector -- -> ``ambiguous``, NAMED, never picked;
       no assignment at all -> ``none`` (052 wins everywhere else).

    Fail-soft to ``unavailable`` on any read failure, with the transaction
    un-poisoned through a savepoint so the caller's next read on this connection
    survives. ``unavailable`` is never reported as ``none``.
    """

    def _refused(reason: str) -> ValueTableResolution:
        return ValueTableResolution(
            state=STATE_UNAVAILABLE,
            project_id=project_id,
            connector=connector,
            source_field=source_field,
            reason=reason,
        )

    marked = _savepoint(conn, f"SAVEPOINT {_RESOLUTION_SAVEPOINT}")
    try:
        datastream_ids = _connector_datastreams(
            conn, project_id=project_id, connector=connector
        )
        if not datastream_ids:
            resolution = ValueTableResolution(
                state=STATE_NONE,
                project_id=project_id,
                connector=connector,
                source_field=source_field,
                reason=REASON_NO_DATASTREAM,
            )
        else:
            resolution = _classify(
                conn,
                project_id=project_id,
                connector=connector,
                source_field=source_field,
                datastream_ids=datastream_ids,
            )
    except Exception as exc:  # noqa: BLE001 -- one unreadable store takes down no matrix
        logger.warning(
            "value_table_resolution: unavailable project=%s connector=%s field=%s: %s",
            project_id,
            connector,
            source_field,
            exc,
        )
        if marked:
            _savepoint(conn, f"ROLLBACK TO SAVEPOINT {_RESOLUTION_SAVEPOINT}")
        return _refused(f"store unreadable: {type(exc).__name__}")
    if marked:
        _savepoint(conn, f"RELEASE SAVEPOINT {_RESOLUTION_SAVEPOINT}")
    return resolution


def _classify(
    conn,
    *,
    project_id: str,
    connector: str,
    source_field: str,
    datastream_ids: list[str],
) -> ValueTableResolution:
    """Apply the ratified ambiguity rule to the assignments actually found."""
    from core.value_mapping_tables import (  # noqa: PLC0415 -- the store's SQL stays in its owner
        list_entries,
        read_field_assignments,
    )

    assignments = read_field_assignments(
        conn, datastream_ids=datastream_ids, source_field=source_field
    )
    by_datastream = {row["datastream_id"]: row for row in assignments}
    datastreams = tuple(
        {
            "datastream_id": datastream_id,
            "datastream_name": by_datastream.get(datastream_id, {}).get("datastream_name"),
            "table_id": by_datastream.get(datastream_id, {}).get("table_id"),
            "table_name": by_datastream.get(datastream_id, {}).get("table_name"),
        }
        for datastream_id in datastream_ids
    )

    common = {
        "project_id": project_id,
        "connector": connector,
        "source_field": source_field,
        "datastreams": datastreams,
    }

    if not assignments:
        return ValueTableResolution(
            state=STATE_NONE, reason=REASON_NO_ASSIGNMENT, **common
        )

    table_ids = sorted({row["table_id"] for row in assignments})
    if len(table_ids) >= 2:
        # Two Datastreams of one connector that do not resolve to the SAME table
        # on this field: an ambiguity -- a gap, never a pick.
        return ValueTableResolution(
            state=STATE_AMBIGUOUS, reason=REASON_TABLES_DISAGREE, **common
        )
    if len(by_datastream) < len(datastream_ids):
        # The one table is stated on SOME Datastreams of the connector and not on
        # others. Connector-keyed rows merge them all, so applying it would
        # translate rows of a Datastream the client never assigned -- a pick.
        return ValueTableResolution(
            state=STATE_AMBIGUOUS, reason=REASON_PARTIAL_ASSIGNMENT, **common
        )

    table_id = table_ids[0]
    pairs = {
        entry["source_value"]: entry["canonical_value"]
        for entry in list_entries(conn, table_id=table_id)
    }
    return ValueTableResolution(
        state=STATE_APPLIED,
        table_id=table_id,
        table_name=assignments[0].get("table_name"),
        pairs=pairs,
        **common,
    )


__all__ = [
    "REASON_NO_ASSIGNMENT",
    "REASON_NO_DATASTREAM",
    "REASON_PARTIAL_ASSIGNMENT",
    "REASON_TABLES_DISAGREE",
    "STATE_AMBIGUOUS",
    "STATE_APPLIED",
    "STATE_NONE",
    "STATE_UNAVAILABLE",
    "ValueTableResolution",
    "resolve_value_table",
]
