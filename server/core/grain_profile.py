"""Measured cardinality for a mapping's grain columns -- read on the landed rows, never assumed.

WHAT WAS MEASURED, 2026-09-05. Every connector flow of the reference project
carried `cardinality_signal: unknown` on every field (their mappings were minted
without samples), and the projection compiler prices an unknown grain column at
1 000 distinct values: two such columns are a million-row grain, three a
billion, and `cardinality_over_limit` / `scan_over_limit` refuse every plan --
so no mapping change on those flows could ever mint a candidate, including the
pin of a shared identity that adds no row to the scan. The repair the refusal
named, `profile_the_grain_columns`, had no door: nothing in the product measured
a column the flow had already landed.

THE RULE. When a mapping change is prepared, each grain column whose signal is
unknown is COUNTED on the flow's published relation (`COUNT(DISTINCT col)`),
and the count is written back as the signal of the version being prepared,
in the vocabulary the schema already holds (`low`, `medium`, `high`, `unique`).
Measured, not declared; part of the version and of its hash. A relation that
cannot be read leaves the signal unknown and the refusal keeps naming the
repair -- a failed read never invents a small number.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

#: A distinct count is classed with the nominal bands the projection compiler
#: prices (`datastream_projection._CARDINALITY_SIGNAL_DISTINCT`): at or under the
#: band's nominal value, the band. One scale, the compiler's, never a second one.
_BANDS: tuple[tuple[int, str], ...] = ((20, "low"), (500, "medium"), (10_000, "high"))
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
UNKNOWN = "unknown"
#: A domain this small is kept as VALUES, so the compiler prices its exact
#: count; beyond it the count alone is classed into a band. The mapping schema
#: bounds `profile.allowed_values` to the same number.
MAX_DOMAIN_VALUES = 64


def signal_for(distinct: int) -> str:
    """The cardinality signal a measured distinct count belongs to."""
    for ceiling, name in _BANDS:
        if distinct <= ceiling:
            return name
    return "unique"


def unknown_grain_columns(payload: Mapping[str, Any]) -> list[str]:
    """The grain columns whose profile carries no measured signal."""
    fields = {
        str(f.get("field_id")): f for f in (payload.get("fields") or []) if isinstance(f, dict)
    }
    columns: list[str] = []
    for column in payload.get("grain") or []:
        field = fields.get(str(column))
        if field is None:
            continue
        profile = field.get("profile") if isinstance(field.get("profile"), dict) else {}
        signal = str(profile.get("cardinality_signal") or UNKNOWN).strip().lower()
        allowed = field.get("allowed_values") or profile.get("allowed_values")
        if signal == UNKNOWN and not allowed:
            columns.append(str(column))
    return columns


def enrich_unknown_grain_signals(
    payload: dict[str, Any], measure: Callable[[list[str]], Mapping[str, Any]]
) -> dict[str, str]:
    """Write measured signals into the payload's unknown grain profiles. Returns what changed.

    `measure` receives the columns to count and returns `{column: distinct}` for
    those it could count; a column it does not return stays unknown.
    """
    columns = unknown_grain_columns(payload)
    if not columns:
        return {}
    try:
        counted = dict(measure(columns))
    except Exception as exc:  # noqa: BLE001 -- a failed read never invents a small number
        logger.warning("grain_profile: measurement failed, signals stay unknown: %s", exc)
        return {}
    written: dict[str, str] = {}
    for field in payload.get("fields") or []:
        if not isinstance(field, dict):
            continue
        column = str(field.get("field_id"))
        if column not in counted:
            continue
        measured = counted[column]
        domain: list[str] | None = None
        if isinstance(measured, (list, tuple, set, frozenset)):
            # An exact domain: the values themselves, which the projection
            # compiler prices at their exact count (`allowed_values`), never at
            # a band's nominal value -- `channel_id` with ONE value is 1, not 20.
            domain = sorted({str(v) for v in measured if v is not None})[:MAX_DOMAIN_VALUES]
            distinct = len(domain)
        else:
            try:
                distinct = int(measured)
            except (TypeError, ValueError):
                continue
        if distinct < 0:
            continue
        profile = field.setdefault("profile", {})
        profile["cardinality_signal"] = signal_for(distinct)
        if domain:
            profile["allowed_values"] = domain
        written[column] = profile["cardinality_signal"]
    return written


def published_relation(conn, *, project_id: str, datastream_id: str) -> str | None:
    """The relation the flow last published into, from the output ledger."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT relation_ref
              FROM app.datastream_output_versions
             WHERE project_id = %s AND datastream_id = %s AND relation_ref IS NOT NULL
             ORDER BY created_at DESC, id DESC
             LIMIT 1
            """,
            (project_id, datastream_id),
        )
        row = cur.fetchone()
    return str(row[0]) if row and row[0] else None


def measure_distinct(project_id: str, relation: str, columns: Iterable[str]) -> dict[str, Any]:
    """COUNT(DISTINCT col) for each grain column, on whichever warehouse serves the relation.

    Two shapes of landing, one answer. A column the relation carries is counted
    as a column. A grain dimension the relation does NOT carry as a column may
    still be there as rows of a BREAKDOWN landing (`breakdown_dimension`,
    `breakdown_value` -- `relation_shape.BREAKDOWN_PAIR`): the reference
    project's `raw_youtube_breakdown` carries `country`, `age_group`,
    `device_type` that way, and asking BigQuery for the column answered
    `Unrecognized name` and voided the whole measurement (2026-09-05). Each
    dimension is counted in its own statement so one absent name never costs
    the others their count.
    """
    from core import relation_shape, warehouse  # noqa: PLC0415
    from core.query_execution import _dataset_for  # noqa: PLC0415
    from core.raw_landing import promoted_relation  # noqa: PLC0415

    relation = promoted_relation(relation)
    if "." in relation:
        schema, _, table = relation.rpartition(".")
    else:
        schema, table = _dataset_for(project_id, relation), relation
    wanted = [c for c in columns if _IDENTIFIER.match(c)]
    if not wanted or not _IDENTIFIER.match(table or ""):
        return {}
    mode = warehouse._db_mode()  # noqa: SLF001 -- the same seam relation_shape reads through
    if mode == "duckdb":
        schema = schema or warehouse._duckdb_mart_prefix(project_id).rstrip(".")  # noqa: SLF001
    if not schema or not _IDENTIFIER.match(schema):
        return {}
    present = relation_shape._columns(warehouse, mode, project_id, table, schema)  # noqa: SLF001
    qualified = f"`{schema}`.{table}" if mode == "bigquery" else f"{schema}.{table}"

    def run(sql: str, params: list[Any]) -> list[dict[str, Any]]:
        if mode == "bigquery":
            return warehouse._query_bigquery(sql, params)  # noqa: SLF001
        if mode == "duckdb":
            return warehouse._query_duckdb(sql, params)  # noqa: SLF001
        return []

    counted: dict[str, Any] = {}
    as_columns = [c for c in wanted if c in present]
    if as_columns:
        selects = ", ".join(f"COUNT(DISTINCT {c}) AS {c}" for c in as_columns)
        rows = run(f"SELECT {selects} FROM {qualified}", [])  # noqa: S608
        row = rows[0] if rows else {}
        counted.update({c: int(row[c]) for c in as_columns if row.get(c) is not None})
        # A small domain is kept as its VALUES (exact count for the compiler).
        for c in as_columns:
            if 0 < counted.get(c, 0) <= MAX_DOMAIN_VALUES:
                values = run(
                    f"SELECT DISTINCT {c} AS v FROM {qualified} WHERE {c} IS NOT NULL "  # noqa: S608
                    f"LIMIT {MAX_DOMAIN_VALUES + 1}",
                    [],
                )
                domain = [r.get("v") for r in values if r.get("v") is not None]
                if 0 < len(domain) <= MAX_DOMAIN_VALUES:
                    counted[c] = [str(v) for v in domain]
    dimension_column, value_column = relation_shape.BREAKDOWN_PAIR
    if dimension_column in present and value_column in present:
        placeholder = "@p0" if mode == "bigquery" else "?"
        for c in wanted:
            if c in present:
                continue
            rows = run(
                f"SELECT DISTINCT {value_column} AS v FROM {qualified} "  # noqa: S608
                f"WHERE {dimension_column} = {placeholder} AND {value_column} IS NOT NULL "
                f"LIMIT {MAX_DOMAIN_VALUES + 1}",
                [c],
            )
            domain = [r.get("v") for r in rows if r.get("v") is not None]
            if not domain:
                continue
            if len(domain) <= MAX_DOMAIN_VALUES:
                counted[c] = [str(v) for v in domain]
            else:
                rows = run(
                    f"SELECT COUNT(DISTINCT {value_column}) AS n FROM {qualified} "  # noqa: S608
                    f"WHERE {dimension_column} = {placeholder}",
                    [c],
                )
                n = (rows[0] if rows else {}).get("n")
                if n is not None and int(n) > 0:
                    counted[c] = int(n)
    return counted


def measure_grain_signals(conn, *, project_id: str, datastream_id: str, columns: list[str]) -> dict[str, Any]:
    """The measurer a change preparation hands to `enrich_unknown_grain_signals`."""
    relation = published_relation(conn, project_id=project_id, datastream_id=datastream_id)
    if not relation:
        return {}
    return measure_distinct(project_id, relation, columns)
