"""What a published relation can actually answer -- AI-342.

THE DEFECT THIS MODULE EXISTS FOR, and it is a class, not a connector. A
published Semantic View binds each Concept to a Datastream, and the Datastream
publishes ONE relation. Nothing asked whether that relation carries the Concept.
Measured 2026-09-01 on the reference Project: the View bound ELEVEN dimensions to
relations that materialised TWO, and « views by country » compiled, resolved,
ran, and answered zero rows. An empty answer and « this source does not carry
that breakdown » are opposite facts, and the product served the first while
meaning the second.

WHY A SHAPE AND NOT A COLUMN LIST. Four of the forty Connectors land measurements
as ROWS -- `metric`/`value` -- and a breakdown landing goes further and lands the
DIMENSION as a row too: `breakdown_dimension`/`breakdown_value`. On such a
relation `country` is not a column any more than `views` is, so a column check
answers "absent" for every member of it, and the pivot that reads it
(`query_execution.breakdown_pivot`) answers "present" for every member INCLUDING
the ones nothing ever landed. Neither is the question. The question is what KEYS
the relation carries in those two columns, and that is read from the relation.

READ, NEVER DECLARED, AND NEVER CACHED IN A TABLE. The shape of a landing is
derivable from the landing; a stored copy is wrong the day a pull lands. The
`INFORMATION_SCHEMA` half scans no table bytes on either engine. The key half is
one `DISTINCT` over one column, so it is asked ONLY where the answer decides
something -- a publication, which is a deliberate human act paid once, and a read
that is about to pivot -- never on every render.

FAIL-OPEN, DELIBERATELY. A shape that could not be read yields `readable=False`
and every question about it answers `None`. « We could not look » and « nothing is
there » are different facts, and refusing a publication on the first would let a
warehouse outage retract a governed act.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: The SAME identifier rule the reader applies part by part
#: (`query_execution._IDENTIFIER`). Spelled here rather than imported because
#: `query_execution` imports THIS module; and applied because a relation name
#: reaches the key query as an IDENTIFIER, which no bind parameter can carry. A
#: name that fails it is not read at all -- the shape is unreadable, which is the
#: fail-open answer, never a guess and never a concatenation.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: The two column pairs a long landing uses. Same pairs
#: `query_execution.long_form_pivot` / `breakdown_pivot` read, and they are here
#: because the shape and the pivot must not learn about a new one separately.
BREAKDOWN_PAIR = ("breakdown_dimension", "breakdown_value")
LONG_FORM_PAIR = ("metric", "value")

#: A landing with more distinct breakdown keys than this is not a breakdown
#: landing any more -- it is a free-text column -- and reading all of them would
#: be unbounded. The cap is generous: the widest Connector of this repository
#: declares eight.
_MAX_KEYS = 200


@dataclass(frozen=True, slots=True)
class RelationShape:
    """What one published relation carries, as the warehouse answers it."""

    relation: str
    readable: bool
    columns: frozenset[str] = frozenset()
    #: The distinct values of `breakdown_dimension`, or None when the relation is
    #: not a breakdown landing at all. An EMPTY frozenset is a real answer: the
    #: relation has the slot and nothing has landed in it.
    breakdown_keys: frozenset[str] | None = None
    #: The distinct values of `metric`, read only when a caller asked to pay for
    #: them (AI-352: the publication guard does; reads do not). None when the
    #: relation is not long-form, or when nobody asked.
    metric_keys: frozenset[str] | None = None

    @property
    def is_breakdown_landing(self) -> bool:
        return all(column in self.columns for column in BREAKDOWN_PAIR)

    @property
    def is_long_form(self) -> bool:
        return all(column in self.columns for column in LONG_FORM_PAIR)


def unreadable(relation: str) -> RelationShape:
    return RelationShape(relation=relation, readable=False)


def read(
    project_id: str, relation: str, *, dataset: str = "", with_metric_keys: bool = False
) -> RelationShape:
    """The columns of *relation*, plus its breakdown keys when it has the slot.

    *dataset* is the qualifier a bare relation name needs; a relation that
    already carries its own is left exactly as it is.
    """
    if not relation or not _readable_name(relation, dataset):
        return unreadable(relation)
    try:
        from core import warehouse  # noqa: PLC0415

        mode = warehouse._db_mode()
        columns = _columns(warehouse, mode, project_id, relation, dataset)
    except Exception as exc:  # noqa: BLE001 -- an unknown shape is never an empty one
        logger.warning("relation_shape: unreadable %s: %s", relation, exc)
        return unreadable(relation)
    if not columns:
        return unreadable(relation)
    shape = RelationShape(relation=relation, readable=True, columns=frozenset(columns))
    breakdown_keys: frozenset[str] | None = None
    if shape.is_breakdown_landing:
        try:
            breakdown_keys = frozenset(
                _distinct_keys(warehouse, mode, project_id, relation, dataset, BREAKDOWN_PAIR[0])
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("relation_shape: breakdown keys unreadable %s: %s", relation, exc)
    metric_keys: frozenset[str] | None = None
    if with_metric_keys and shape.is_long_form:
        # AI-352 (governance.md, 2026-09-02): a second DISTINCT, paid only where
        # a caller asked for it -- publication -- never on the read path.
        try:
            metric_keys = frozenset(
                _distinct_keys(warehouse, mode, project_id, relation, dataset, LONG_FORM_PAIR[0])
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("relation_shape: metric keys unreadable %s: %s", relation, exc)
    return RelationShape(
        relation=relation,
        readable=True,
        columns=shape.columns,
        breakdown_keys=breakdown_keys,
        metric_keys=metric_keys,
    )


def answers(shape: RelationShape, *, physical_field: str, concept_name: str) -> bool | None:
    """Can *shape* answer a member whose physical field is *physical_field*?

    ``True`` it can, ``False`` it provably cannot, ``None`` the shape could not be
    read and nothing may be concluded.

    THE TWO WAYS A RELATION ANSWERS, and they are the two the reader uses:
    the field IS a column of it, or the relation lands its dimensions long and
    carries the Concept's own name as a `breakdown_dimension` key. The equality on
    the name is the one this whole product runs on -- the Concept's name is what
    the mapping's `canonical_target` says and what the landing writes into the
    key column -- so nothing is invented for it here either.
    """
    if not shape.readable:
        return None
    if physical_field and physical_field in shape.columns:
        return True
    if not shape.is_breakdown_landing:
        return False
    if shape.breakdown_keys is None:
        # The slot is there and its keys could not be read: that is the
        # unreadable case again, one level down.
        return None
    return concept_name in shape.breakdown_keys


def answers_measure(
    shape: RelationShape, *, physical_field: str, concept_name: str
) -> bool | None:
    """Can *shape* answer a MEASURE named *concept_name*? (AI-352, 2026-09-02.)

    The mirror of `answers`, for the other half of the same landing: a wide
    relation answers a measure by carrying its physical column, a long-form
    landing by carrying the Concept's own name as a `metric` key. `None` when
    the shape -- or its metric keys, which are read only on request -- could not
    be read, and nothing may be concluded.
    """
    if not shape.readable:
        return None
    if physical_field and physical_field in shape.columns:
        return True
    if not shape.is_long_form:
        return False
    if shape.metric_keys is None:
        return None
    return concept_name in shape.metric_keys


def _readable_name(relation: str, dataset: str) -> bool:
    """Every part of the address is an identifier, or the shape is not read."""
    parts = relation.split(".") + ([dataset] if dataset else [])
    return all(_IDENTIFIER.match(part) for part in parts)


def _qualified(relation: str, dataset: str) -> tuple[str, str]:
    """`(schema, table)` for an INFORMATION_SCHEMA lookup."""
    if "." in relation:
        schema, _, table = relation.rpartition(".")
        return schema, table
    return dataset, relation


def _columns(warehouse, mode: str, project_id: str, relation: str, dataset: str) -> set[str]:
    schema, table = _qualified(relation, dataset)
    if mode == "bigquery":
        if not schema:
            return set()
        rows = warehouse._query_bigquery(
            f"SELECT column_name FROM `{schema}`.INFORMATION_SCHEMA.COLUMNS "  # noqa: S608
            "WHERE table_name = @p0",
            [table],
        )
        return {str(row.get("column_name")) for row in rows}
    if mode == "duckdb":
        # DuckDB HAS an information_schema, and reading it is what makes the
        # local fixture measure the same thing production measures. Before
        # AI-342 this branch did not exist: the resolver returned an EMPTY shape
        # on DuckDB, so every pivot was disabled locally and every long-landing
        # read that production serves as an empty answer blew up here as a binder
        # error instead. Two engines, two symptoms, one unread shape.
        schema = schema or warehouse._duckdb_mart_prefix(project_id).rstrip(".")
        rows = warehouse._query_duckdb(
            "SELECT column_name FROM information_schema.columns "  # noqa: S608
            "WHERE table_schema = ? AND table_name = ?",
            [schema, table],
        )
        return {str(row.get("column_name")) for row in rows}
    return set()


def _distinct_keys(
    warehouse, mode: str, project_id: str, relation: str, dataset: str, key_column: str
) -> set[str]:
    schema, table = _qualified(relation, dataset)
    if mode == "duckdb":
        schema = schema or warehouse._duckdb_mart_prefix(project_id).rstrip(".")
    # A FROM clause takes an IDENTIFIER, which no bind parameter can carry, so
    # the two parts are re-checked here rather than trusted from the caller: the
    # DuckDB schema above is derived from an org slug and never passed through
    # `_readable_name`. A part that fails reads as an unreadable shape, which
    # fails open and refuses nothing.
    if not _IDENTIFIER.match(schema or "") or not _IDENTIFIER.match(table or ""):
        return set()
    if mode == "bigquery":
        rows = warehouse._query_bigquery(
            f"SELECT DISTINCT {key_column} AS k FROM `{schema}`.{table} "  # noqa: S608
            f"WHERE {key_column} IS NOT NULL LIMIT {_MAX_KEYS}",
            [],
        )
    elif mode == "duckdb":
        rows = warehouse._query_duckdb(
            f"SELECT DISTINCT {key_column} AS k FROM {schema}.{table} "  # noqa: S608
            f"WHERE {key_column} IS NOT NULL LIMIT {_MAX_KEYS}",
            [],
        )
    else:
        return set()
    return {str(row.get("k")) for row in rows if row.get("k") is not None}
