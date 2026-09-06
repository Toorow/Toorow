"""Story 66.6 -- the pivot is a projection of a Result, and the server owns it.

WHAT A PIVOT IS HERE. Rows, Columns, Values and Filters produce a bounded matrix
over ONE already-executed, immutable Result. It is not a second query and not a
second answer: the same Result id and content hash back the table, the pivot and
the chart, so switching presentation cannot change a number.

WHY THE SERVER AND NOT THE BROWSER. A pivot aggregates. `SUM` in JavaScript over
delivered rows would be a second semantic engine -- one with no measure contract,
no additivity rule and no test -- and it would disagree with the warehouse the
first time a ratio or a truncated page appeared. So React receives cells and
computes none.

THE RULE THAT DECIDES EVERY TOTAL. Additive measures fold with their declared
aggregation. A RATIO NEVER FOLDS: it is recomputed from its two components at
every level -- cell, subtotal, grand total -- exactly as story 53.2's amendment
requires for rollups. When a component is missing at that level the cell is null
with a stated reason, never a plausible average of the ratios above it.

NULL AND ZERO STAY APART. A cell with no contributing row is `null` and carries
`contributing_rows: 0`. A cell whose rows summed to zero is `0`. Rendering the
first as `0` is how an unmatched key becomes a business fact nobody measured.

BOUNDED BY CONSTRUCTION. Cells, rows and columns each have a cap, and a truncated
matrix says which axis was cut and how many entries were dropped. An admin page
that mounted an unbounded number of DOM cells would be the same defect one layer
up.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from contextvars import ContextVar
from typing import Any, Iterable, Mapping, Sequence

#: Caps, stated in every response. `MAX_CELLS` is what keeps a 2 000 x 500 pivot
#: from becoming a million DOM nodes; the axis caps are what make the truncation
#: explainable ("the first 200 campaigns") rather than arbitrary.
MAX_CELLS = 20_000
MAX_ROW_KEYS = 500
MAX_COLUMN_KEYS = 100

#: STORY 66.10 -- the initial response is at most this many bytes, and the rest
#: is a cursor. The cell cap alone does not bound the payload: 20 000 cells of
#: long campaign names is megabytes, and a caller that asked for one screen would
#: receive a download. The bound is on what is SENT, measured on the serialized
#: document rather than estimated from the count.
MAX_RESPONSE_BYTES = 262_144

#: Room left for the envelope the door wraps the matrix in (`project_id`, the
#: `pivot` key) so the BUDGET IS THE ONE THE CALLER MEASURES -- the whole
#: response, not the part this module happens to own.
_ENVELOPE_ALLOWANCE = 1_024

#: POURQUOI UNE PAGE EST PLUS COURTE QUE CE QU'ON A DEMANDE. Trois plafonds
#: peuvent couper, et ils ne se reparent pas de la meme facon : un axe trop long
#: se filtre, un budget en octets se pagine (le curseur est deja la), un plafond
#: de cellules se reduit en enlevant une dimension. Nommer lequel a mordu est la
#: difference entre << les 46 premieres campagnes >> et << trop gros >>.
TRUNCATED_BY_AXIS_CAP = "axis_cap"
TRUNCATED_BY_CELL_CAP = "cell_cap"
TRUNCATED_BY_BYTE_BUDGET = "byte_budget"

#: Aggregations a projection may fold. A ratio is deliberately absent: it is
#: recomputed, never folded.
FOLDABLE = frozenset({"sum", "min", "max", "count"})

#: What a cell says when it has no value, and why. Two absences that look alike
#: on screen have to be told apart in the payload.
NO_CONTRIBUTING_ROW = "no_contributing_row"
RATIO_COMPONENT_MISSING = "ratio_component_missing"
COMPARISON_VALUE_MISSING = "comparison_value_missing"
COMPARISON_VALUE_NOT_NUMERIC = "comparison_value_not_numeric"
COMPARISON_BASELINE_ZERO = "baseline_zero"


class PivotRefused(ValueError):
    """The pivot request cannot be projected onto this Result."""

    def __init__(self, code: str, message: str, *, detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


def _fields_by_role(schema: Mapping[str, Any]) -> tuple[dict[str, dict], dict[str, dict]]:
    dimensions: dict[str, dict] = {}
    measures: dict[str, dict] = {}
    for field in schema.get("fields") or []:
        if not isinstance(field, dict):
            continue
        name = str(field.get("name") or "")
        if field.get("role") == "dimension":
            dimensions[name] = dict(field)
        else:
            measures[name] = dict(field)
    return dimensions, measures


def _require(names: Sequence[Any], available: Mapping[str, dict], what: str) -> list[str]:
    resolved = []
    for raw in names:
        name = str(raw)
        if name not in available:
            raise PivotRefused(
                f"unknown_{what}",
                f"{name} is not a {what} of this Result. A pivot rearranges what the "
                "Result already answered; it does not ask a new question.",
                detail=sorted(available),
            )
        resolved.append(name)
    return resolved


#: The NFR8 instrument. `project` arms it; `_key` increments it. A ContextVar and
#: not a module global: two concurrent projections would otherwise share one
#: integer and each would report the other's work.
_row_examinations: ContextVar[list[int] | None] = ContextVar(
    "pivot_row_examinations", default=None
)


def _key(row: Mapping[str, Any], fields: Sequence[str]) -> tuple:
    # Python considers True == 1 and False == 0. A pivot key does not: JSON
    # boolean and number are different business values and must stay separate.
    counter = _row_examinations.get()
    if counter is not None:
        counter[0] += 1
    return tuple(_typed_scalar(row.get(field)) for field in fields)


def _wire_key(key: Sequence[Any]) -> list[Any]:
    """Preserve JSON scalar type on the wire; display labels belong to the UI."""
    return [value for _kind, value in key]


def _sort_token(value: tuple[str, Any]) -> tuple[int, Any]:
    kind, scalar = value
    order = {"null": 0, "boolean": 1, "number": 2, "string": 3}[kind]
    return (order, scalar if kind != "null" else "")


def _fold(values: Iterable[Any], aggregation: str) -> Any:
    numbers = [value for value in values if value is not None]
    if not numbers:
        return None
    if aggregation == "count":
        return sum(numbers)
    if aggregation == "min":
        return min(numbers)
    if aggregation == "max":
        return max(numbers)
    return sum(numbers)


def project(
    *,
    result_id: str,
    content_hash: str,
    schema: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    request: Mapping[str, Any],
    max_response_bytes: int = MAX_RESPONSE_BYTES,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Project one Result, and report how many rows it took to do it.

    THE WRAPPER EXISTS FOR THE COUNTER, and the counter exists because NFR8 is a
    cost claim -- "aggregates in one pass over Result rows and does not rescan all
    rows per subtotal or page trim" -- and a cost claim that nothing measures is
    the one kind of defect a green suite cannot see. Until 2026-08-17 the subtotal
    path rebuilt its row index per row key with a full scan; the answers were
    correct, so no assertion moved, while the work grew with the axis.

    `_key` is the row-examination primitive: every place that decides which bucket
    a row belongs to goes through it. So counting ITS calls counts row
    examinations wherever they happen, including in a comprehension a future
    change adds without telling anyone. The count travels in a `ContextVar` rather
    than a module global because two requests may project at once and a shared
    integer would blend them.
    """
    token = _row_examinations.set([0])
    try:
        return _project(
            result_id=result_id,
            content_hash=content_hash,
            schema=schema,
            rows=rows,
            request=request,
            max_response_bytes=max_response_bytes,
            project_id=project_id,
        )
    finally:
        _row_examinations.reset(token)


def _project(
    *,
    result_id: str,
    content_hash: str,
    schema: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    request: Mapping[str, Any],
    max_response_bytes: int = MAX_RESPONSE_BYTES,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Turn one Result into a bounded matrix, or refuse the request by name.

    `cursor` resumes a matrix that did not fit. It carries an offset into the
    ORDERED row axis -- which is why the axis is sorted rather than left in
    warehouse order: a cursor over an unstable order returns overlapping and
    missing pages, and neither is visible to the caller -- and it is SIGNED and
    BOUND to the Result and to the request shape, so the same failure cannot
    arrive through a changed request either.

    A bare `row_offset` / `column_offset` is still accepted for a manual walk. It
    binds nothing and it never did; what changed is that the server now hands
    back a `next_cursor` that does.
    """
    dimensions, measures = _fields_by_role(schema)

    comparison_field = next(
        (
            (name, descriptor.get("comparison"))
            for name, descriptor in dimensions.items()
            if isinstance(descriptor.get("comparison"), Mapping)
        ),
        None,
    )

    requested_rows = list(request.get("rows") or [])
    requested_columns = list(request.get("columns") or [])
    if comparison_field is not None:
        period_field = comparison_field[0]
        if period_field not in requested_rows and period_field not in requested_columns:
            requested_columns.append(period_field)
    row_fields = _require(requested_rows, dimensions, "dimension")
    column_fields = _require(requested_columns, dimensions, "dimension")
    value_fields = _require(request.get("values") or [], measures, "measure")
    if not value_fields:
        raise PivotRefused(
            "values_required",
            "A pivot needs at least one value. Rows and columns alone describe a shape "
            "with nothing in it.",
        )
    overlap = set(row_fields) & set(column_fields)
    if overlap:
        raise PivotRefused(
            "field_in_two_wells",
            "The same field cannot be both a row and a column: every cell would be its "
            "own total.",
            detail=sorted(overlap),
        )

    filtered, original_indexes, filters_applied = _apply_filters(
        rows, request.get("filters") or [], dimensions
    )

    grand_total_policy = str(request.get("grand_total") or "none")
    if grand_total_policy not in {"none", "rows", "columns", "both"}:
        raise PivotRefused(
            "unknown_grand_total_policy",
            "Grand totals are none, rows, columns or both.",
        )
    subtotals = bool(request.get("subtotals"))
    if comparison_field is not None and (subtotals or grand_total_policy != "none"):
        raise PivotRefused(
            "comparison_totals_not_supported",
            "Period comparison keeps current and baseline totals separate. Turn off "
            "subtotals and grand totals rather than combining both windows into one number.",
        )

    row_sort = str(request.get("row_sort") or "asc")
    column_sort = str(request.get("column_sort") or "asc")
    if row_sort not in {"asc", "desc"} or column_sort not in {"asc", "desc"}:
        raise PivotRefused(
            "unknown_pivot_sort", "Pivot axis order is ascending or descending."
        )
    all_row_keys = _axis_keys(filtered, row_fields, direction=row_sort)
    all_column_keys = _axis_keys(filtered, column_fields, direction=column_sort)

    if request.get("cursor") is not None:
        row_offset, column_offset = read_cursor(
            request.get("cursor"),
            project_id=project_id,
            result_id=result_id,
            content_hash=content_hash,
            request=request,
        )
        # AN EXPLICIT OFFSET BESIDE A CURSOR WINS ON ITS OWN AXIS. The offsets are
        # the token's PAYLOAD, not its identity -- they are signed but not
        # compared -- so overriding one changes nothing about what the token
        # proves. Without this, walking back on one axis restored the OTHER axis
        # as it stood when the remembered page was left: the caller lost a column
        # they never asked to leave, which is the exact failure the per-axis
        # tokens exist to prevent. The console remembers the page it left as one
        # token carrying both offsets, so the axis it is NOT moving has to be
        # stated beside it.
        if request.get("row_offset") is not None:
            row_offset = _offset(request.get("row_offset"))
        if request.get("column_offset") is not None:
            column_offset = _offset(request.get("column_offset"))
    else:
        row_offset = _offset(request.get("row_offset"))
        column_offset = _offset(request.get("column_offset"))
    row_keys = all_row_keys[row_offset:]
    column_keys = all_column_keys[column_offset:]
    comparison_column_index: int | None = None
    if comparison_field is not None:
        period_field = comparison_field[0]
        if period_field in column_fields:
            comparison_column_index = column_fields.index(period_field)
            if (
                column_offset > 0
                and column_offset < len(all_column_keys)
                and _comparison_axis_identity(
                    all_column_keys[column_offset - 1], comparison_column_index
                )
                == _comparison_axis_identity(
                    all_column_keys[column_offset], comparison_column_index
                )
            ):
                raise PivotRefused(
                    "comparison_cursor_splits_pair",
                    "A comparison cursor starts at the next complete current/baseline pair.",
                )

    truncation = {
        "rows_truncated": len(row_keys) > MAX_ROW_KEYS,
        "columns_truncated": len(column_keys) > MAX_COLUMN_KEYS,
        "rows_dropped": max(len(row_keys) - MAX_ROW_KEYS, 0),
        "columns_dropped": max(len(column_keys) - MAX_COLUMN_KEYS, 0),
        "cells_truncated": False,
        # STORY 66.6, le reste ouvert : QUEL plafond a coupe cette page.
        #
        # MESURE 2026-08-22, a `MAX_CELLS` EXACTEMENT (500 x 40 = 20 000
        # cellules) : la page servie porte 46 lignes, pas 500. Le plafond de
        # cellules n'a rien coupe (`cells_truncated` faux) -- c'est le BUDGET EN
        # OCTETS qui a mordu, a 255 440 sur 262 144. Autrement dit `max_cells`
        # est annonce dans chaque reponse et n'est JAMAIS le plafond atteint :
        # un appelant qui dimensionne sa demande sur 20 000 cellules en recoit
        # 1 840. Trois champs le laissaient DEDUIRE ; aucun ne le DISAIT, et une
        # deduction n'est pas une divulgation.
        #
        # `None` tant que rien n'est coupe. Un mot vide se lirait comme
        # << coupe, on ne sait pas par quoi >>.
        "truncation_reason": None,
    }
    row_keys = row_keys[:MAX_ROW_KEYS]
    if comparison_column_index is None:
        column_keys = column_keys[:MAX_COLUMN_KEYS]
    else:
        column_keys = _complete_comparison_groups(
            column_keys, comparison_column_index, MAX_COLUMN_KEYS
        )
    if len(row_keys) * len(column_keys) * max(len(value_fields), 1) > MAX_CELLS:
        # The cap is on the ANSWER, and the axis it cut is named: "the first N
        # rows" is repairable by a filter, "too big" is not.
        allowed_rows = max(
            MAX_CELLS // max(len(column_keys) * max(len(value_fields), 1), 1), 1
        )
        truncation["cells_truncated"] = True
        truncation["rows_dropped"] += max(len(row_keys) - allowed_rows, 0)
        row_keys = row_keys[:allowed_rows]
        truncation["truncation_reason"] = TRUNCATED_BY_CELL_CAP
    elif truncation["rows_truncated"] or truncation["columns_truncated"]:
        truncation["truncation_reason"] = TRUNCATED_BY_AXIS_CAP

    # ONE pass builds BOTH indexes. The row-key index used to not exist, and the
    # subtotal below rebuilt it per row key with a full scan of `filtered` --
    # `[i for i, row in enumerate(filtered) if _key(row, row_fields) == row_key]`,
    # once for each of up to MAX_ROW_KEYS keys. That is the rescan NFR8 forbids in
    # its own words ("does not rescan all rows per subtotal or page trim"), and at
    # 500 keys over 20 000 rows it is ten million key rebuilds to answer a question
    # the bucket loop had already answered. `bounds.row_examinations` is what makes
    # the property a number rather than an intention.
    buckets: dict[tuple, list[tuple[int, int]]] = {}
    rows_by_row_key: dict[tuple, list[int]] = {}
    rows_by_column_key: dict[tuple, list[int]] = {}
    for index, row in enumerate(filtered):
        row_key = _key(row, row_fields)
        column_key = _key(row, column_fields)
        buckets.setdefault((row_key, column_key), []).append(
            (index, original_indexes[index])
        )
        rows_by_row_key.setdefault(row_key, []).append(index)
        # The column axis costs the same nothing as the row axis: one setdefault
        # in the pass that was already running. Building it later would be the
        # very rescan NFR8 forbids, and it is why the column half was missing.
        rows_by_column_key.setdefault(column_key, []).append(index)

    cells = []
    for row_key in row_keys:
        for column_key in column_keys:
            indexed = buckets.get((row_key, column_key), [])
            indexes = [index for index, _original in indexed]
            cells.append(
                {
                    "row_key": _wire_key(row_key),
                    "column_key": _wire_key(column_key),
                    "values": {
                        field: _cell_value(field, measures, filtered, indexes)
                        for field in value_fields
                    },
                    # Evidence: which Result rows produced this cell, and which
                    # source each value came from. A cell that cannot name them is
                    # a number with no provenance.
                    "contributing_rows": len(indexes),
                    "contributing_row_indexes": [
                        original for _index, original in indexed[:50]
                    ],
                }
            )

    matrix = {
        "result_id": result_id,
        "content_hash": content_hash,
        "row_fields": row_fields,
        "column_fields": column_fields,
        "value_fields": [
            {
                "name": field,
                "canonical_field_id": measures[field].get("canonical_field_id"),
                "role": measures[field].get("role"),
                "aggregation": measures[field].get("aggregation"),
                "datastream_id": measures[field].get("datastream_id"),
                # Echoed, never decided here: the pivot is a projection of the
                # Result, so what a number means travels with it to whoever
                # draws the matrix -- Console and the MCP App alike.
                "value_type": measures[field].get("value_type"),
                "unit": measures[field].get("unit"),
            }
            for field in value_fields
        ],
        "row_keys": [_wire_key(key) for key in row_keys],
        "column_keys": [_wire_key(key) for key in column_keys],
        "cells": cells,
        "filters_applied": filters_applied,
        "sort": {"rows": row_sort, "columns": column_sort},
        "bounds": {
            "max_cells": MAX_CELLS,
            "max_row_keys": MAX_ROW_KEYS,
            "max_column_keys": MAX_COLUMN_KEYS,
            "max_response_bytes": min(max(int(max_response_bytes), 1), MAX_RESPONSE_BYTES),
            "row_offset": row_offset,
            "total_row_keys": len(all_row_keys),
            **truncation,
        },
    }

    def _totals(key: tuple, indexes: list[int], axis: str) -> dict[str, Any]:
        return {
            f"{axis}_key": _wire_key(key),
            "values": {
                field: _cell_value(field, measures, filtered, indexes)
                for field in value_fields
            },
        }

    if subtotals:
        # Read from the indexes the single pass above already built. Adds no
        # traversal, so `row_passes` is the same with one key or five hundred.
        #
        # SYMMETRIC SINCE 2026-08-21. Only the row half existed, and a matrix that
        # totals one axis and not the other is not a pivot -- it is a grouped list
        # wearing a pivot's name. `column_subtotals` had zero occurrences in the
        # repository while the ratified target named subtotals on both axes.
        matrix["row_subtotals"] = [
            _totals(row_key, rows_by_row_key.get(row_key, []), "row")
            for row_key in row_keys
        ]
        matrix["column_subtotals"] = [
            _totals(column_key, rows_by_column_key.get(column_key, []), "column")
            for column_key in column_keys
        ]

    if grand_total_policy != "none":
        # THE POLICY IS READ, NOT ECHOED. Until 2026-08-21 `rows`, `columns` and
        # `both` produced the same single number and the word was carried beside
        # it as decoration: three accepted answers, one output. A control the
        # calculation does not apply is refused when the request is written -- and
        # this one was accepted, frozen and then described.
        #
        # The policy names WHICH AXIS IS TOTALLED AWAY:
        #   `rows`    -> the rows collapse, leaving one total per COLUMN key;
        #   `columns` -> the columns collapse, leaving one total per ROW key;
        #   `both`    -> both, plus `overall`, the corner cell that needs the two.
        grand_total: dict[str, Any] = {"policy": grand_total_policy}
        if grand_total_policy in {"rows", "both"}:
            grand_total["by_column_key"] = [
                _totals(column_key, rows_by_column_key.get(column_key, []), "column")
                for column_key in column_keys
            ]
        if grand_total_policy in {"columns", "both"}:
            grand_total["by_row_key"] = [
                _totals(row_key, rows_by_row_key.get(row_key, []), "row")
                for row_key in row_keys
            ]
        if grand_total_policy == "both":
            all_indexes = list(range(len(filtered)))
            grand_total["overall"] = {
                field: _cell_value(field, measures, filtered, all_indexes)
                for field in value_fields
            }
        matrix["grand_total"] = grand_total
    # BEFORE the fit, so `response_bytes` measures a document that already carries
    # this key. Added after it, the byte budget would be short by its own length --
    # which is the guarantee story 66.10 exists to hold.
    counter = _row_examinations.get()
    if counter is not None:
        matrix["bounds"]["row_examinations"] = counter[0]
    if comparison_field is not None:
        period_field, comparison = comparison_field
        matrix["comparison"] = {
            **dict(comparison),
            "deltas": _comparison_deltas(
                cells,
                row_fields=row_fields,
                column_fields=column_fields,
                value_fields=value_fields,
                period_field=period_field,
            ),
        }
        if not matrix["comparison"]["deltas"]:
            matrix["comparison"]["unavailable_reason"] = COMPARISON_VALUE_MISSING
    return _fit_to_budget(
        matrix,
        row_offset,
        len(all_row_keys),
        column_offset,
        len(all_column_keys),
        max_response_bytes=min(max(int(max_response_bytes), 1), MAX_RESPONSE_BYTES),
        cursor_context={
            "project_id": project_id,
            "result_id": result_id,
            "content_hash": content_hash,
            "request": request,
        },
    )


#: STORY 66.10, AC 9 -- a continuation is SIGNED and BOUND, or it is not a
#: continuation.
#:
#: Until 2026-08-21 the cursor was a bare `row_offset` integer. Nothing tied page
#: 2 to the request that produced page 1, so replaying `row_offset: 24` under
#: another filter, another value set or another sort served a page of a DIFFERENT
#: matrix -- silently, with `total_row_keys` from the new shape and no refusal.
#: Overlapping and missing rows, invisible to the caller: the exact failure the
#: "cursor over an unstable order" note above forbids, arriving through the
#: request instead of through the order.
#:
#: The token binds the Result (id AND content hash), the Project, and the
#: CANONICAL SHAPE of the request -- everything that decides which rows land in
#: which page. It deliberately does NOT bind the offsets it carries; they are its
#: payload. It is not a capability: it grants nothing the caller could not ask
#: for directly, so it carries no expiry and no identity. It answers one question
#: -- "is this the next page of the page you were on?" -- and refuses when it is
#: not.
CURSOR_SCHEMA_VERSION = "analyze-pivot-cursor.v1"

#: Every request key that changes WHICH rows a page contains. `row_offset`,
#: `column_offset` and `cursor` are absent on purpose: they choose the page, not
#: the matrix. `max_response_bytes` is absent too -- a smaller budget serves fewer
#: rows of the SAME matrix, and the offsets stay meaningful.
_CURSOR_BOUND_KEYS = (
    "rows",
    "columns",
    "values",
    "filters",
    "row_sort",
    "column_sort",
    "subtotals",
    "grand_total",
)


def _canonical_cursor_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _cursor_secret() -> bytes:
    """A purpose-separated key derived from the one server sidecar secret."""
    from core.analyze_feedback import feedback_context_secret  # noqa: PLC0415

    return hmac.digest(feedback_context_secret(), CURSOR_SCHEMA_VERSION.encode(), "sha256")


def _cursor_binding(
    *,
    project_id: str | None,
    result_id: str,
    content_hash: str,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    # THE FINGERPRINT, NOT THE SHAPE. The first version wrote the request's
    # bound keys VERBATIM into the signed body, and a filter is `{field, in:
    # [...]}` with no cap on how many values the list carries -- AC 4 caps 50
    # FILTERS, nothing caps the values inside one. Measured: 100 values -> 5 124
    # byte token, 150 -> 7 391, 200 -> 9 658, past the 8 192 `read_cursor`
    # accepts. So beyond ~170 values the server minted a continuation IT WOULD
    # REFUSE ITSELF, with `malformed_cursor` -- the wrong one of the two -- about
    # a token it had just signed, and the console pager stopped working entirely
    # because it has no fallback once a page is remembered as a token.
    #
    # A digest binds exactly as hard: two different shapes give two different
    # hashes, and `read_cursor` still decides with a single `!=`. What it buys is
    # a token of FIXED size (~200 bytes), so the byte budget stops paying ~29 KiB
    # of rows for three unusable strings.
    shape = {key: request.get(key) for key in _CURSOR_BOUND_KEYS}
    return {
        "schema_version": CURSOR_SCHEMA_VERSION,
        "project_id": project_id,
        "result_id": result_id,
        "content_hash": content_hash,
        "shape_hash": hashlib.sha256(_canonical_cursor_bytes(shape)).hexdigest(),
    }


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    raw = value.encode("ascii")
    return base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))


def mint_cursor(
    *,
    project_id: str | None,
    result_id: str,
    content_hash: str,
    request: Mapping[str, Any],
    row_offset: int,
    column_offset: int,
) -> str:
    document = {
        **_cursor_binding(
            project_id=project_id,
            result_id=result_id,
            content_hash=content_hash,
            request=request,
        ),
        "row_offset": row_offset,
        "column_offset": column_offset,
    }
    body = _b64(_canonical_cursor_bytes(document))
    signature = hmac.digest(_cursor_secret(), body.encode(), "sha256")
    return body + "." + _b64(signature)


def read_cursor(
    token: Any,
    *,
    project_id: str | None,
    result_id: str,
    content_hash: str,
    request: Mapping[str, Any],
) -> tuple[int, int]:
    """The offsets this cursor carries, or a refusal that names which mismatch.

    TWO DIFFERENT REFUSALS, because they send a person to two different places.
    A token that is not one of ours is `malformed_cursor` -- the same code a
    non-numeric offset already gets. A token that IS ours but describes another
    Result or another shape is `cursor_does_not_describe_this_page`: nothing was
    tampered with, the question simply changed under it, and the gesture is to
    ask for the first page of the question being asked now.

    Neither refusal moves the current page. Coercing to zero would answer a
    question about page 3 with page 1 and say nothing.
    """
    if not isinstance(token, str) or not token or len(token) > 8_192:
        raise PivotRefused("malformed_cursor", "That continuation is not a pivot cursor.")
    try:
        body, signature = token.split(".", 1)
        expected = hmac.digest(_cursor_secret(), body.encode(), "sha256")
        if not hmac.compare_digest(_unb64(signature), expected):
            raise PivotRefused("malformed_cursor", "That continuation is not a pivot cursor.")
        document = json.loads(_unb64(body))
    except PivotRefused:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PivotRefused(
            "malformed_cursor", "That continuation is not a pivot cursor."
        ) from exc

    if not isinstance(document, dict):
        raise PivotRefused("malformed_cursor", "That continuation is not a pivot cursor.")
    binding = _cursor_binding(
        project_id=project_id,
        result_id=result_id,
        content_hash=content_hash,
        request=request,
    )
    if any(document.get(key) != value for key, value in binding.items()):
        raise PivotRefused(
            "cursor_does_not_describe_this_page",
            "That continuation belongs to a different question - another Result, another "
            "Project, or a matrix whose rows, columns, values, filters or order have "
            "changed since. Ask for the first page of the analysis you are looking at.",
        )
    return _offset(document.get("row_offset")), _offset(document.get("column_offset"))


def _offset(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        offset = int(value)
    except (TypeError, ValueError) as exc:
        raise PivotRefused("malformed_cursor", "`row_offset` is a whole number.") from exc
    if offset < 0:
        raise PivotRefused("malformed_cursor", "`row_offset` is not negative.")
    return offset


def _trim_totals(matrix: dict[str, Any], axis: str, keys: set[tuple]) -> None:
    """Drop every total whose axis key the page no longer serves.

    Both the `<axis>_subtotals` list and the grand total's `by_<axis>_key` list:
    they are the same numbers under two contracts, and a page that trimmed one
    and not the other would contradict itself inside one document.
    """
    subtotal_key = f"{axis}_subtotals"
    if subtotal_key in matrix:
        matrix[subtotal_key] = [
            entry for entry in matrix[subtotal_key] if tuple(entry[f"{axis}_key"]) in keys
        ]
    grand_total = matrix.get("grand_total")
    by_axis = f"by_{axis}_key"
    if isinstance(grand_total, dict) and by_axis in grand_total:
        grand_total[by_axis] = [
            entry for entry in grand_total[by_axis] if tuple(entry[f"{axis}_key"]) in keys
        ]


def _fit_to_budget(
    matrix: dict[str, Any],
    row_offset: int,
    total_rows: int,
    column_offset: int = 0,
    total_columns: int | None = None,
    *,
    max_response_bytes: int = MAX_RESPONSE_BYTES,
    cursor_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Drop whole ROWS until the document fits, and hand back a cursor.

    Whole rows, never half a row and never a truncated cell: a page that ended
    mid-row would make the last line of a table read as a real, smaller number.
    The cursor is an offset into the ordered axis, so the caller resumes exactly
    where this page stopped.
    """
    def _token(row: int, column: int) -> str | None:
        if cursor_context is None:
            return None
        return mint_cursor(
            project_id=cursor_context.get("project_id"),
            result_id=str(cursor_context.get("result_id") or ""),
            content_hash=str(cursor_context.get("content_hash") or ""),
            request=cursor_context.get("request") or {},
            row_offset=row,
            column_offset=column,
        )

    # THE TOKENS ARE IN THE DOCUMENT BEFORE IT IS TRIMMED, and that is not a
    # detail. Written after the fit they would be ~1.5 KiB the trim never saw,
    # and the page would ship over the very budget it was cut to respect -- the
    # same reason `bounds.row_examinations` is written before the fit. Their
    # offsets are corrected below; only a digit or two of length can move, which
    # is what `_ENVELOPE_ALLOWANCE` and the fixed-point loop already absorb.
    for key in ("cursor", "next_row_cursor", "next_column_cursor"):
        matrix["bounds"][key] = _token(row_offset, column_offset)

    served = len(matrix["row_keys"])
    budget = max_response_bytes - _ENVELOPE_ALLOWANCE
    while served > 1 and len(canonical_bytes(matrix)) > budget:
        served -= 1
        kept = matrix["row_keys"][:served]
        keys = {tuple(key) for key in kept}
        matrix["row_keys"] = kept
        matrix["cells"] = [
            cell for cell in matrix["cells"] if tuple(cell["row_key"]) in keys
        ]
        _refresh_comparison_deltas(matrix)
        _trim_totals(matrix, "row", keys)

    served_columns = len(matrix["column_keys"])
    comparison = matrix.get("comparison")
    comparison_column_index = None
    if isinstance(comparison, Mapping):
        period_field = str(comparison.get("period_field") or "")
        if period_field in matrix.get("column_fields", []):
            comparison_column_index = matrix["column_fields"].index(period_field)
    while served_columns > 1 and len(canonical_bytes(matrix)) > budget:
        if comparison_column_index is None:
            served_columns -= 1
        else:
            last_identity = _comparison_axis_identity(
                matrix["column_keys"][served_columns - 1], comparison_column_index
            )
            while served_columns > 0 and _comparison_axis_identity(
                matrix["column_keys"][served_columns - 1], comparison_column_index
            ) == last_identity:
                served_columns -= 1
            if served_columns == 0:
                break
        kept_columns = matrix["column_keys"][:served_columns]
        keys = {tuple(key) for key in kept_columns}
        matrix["column_keys"] = kept_columns
        matrix["cells"] = [
            cell for cell in matrix["cells"] if tuple(cell["column_key"]) in keys
        ]
        _refresh_comparison_deltas(matrix)
        # Trimming a column and keeping its total would print a number for a
        # column the page does not show. The row half already did this; the
        # column half had nothing to trim until the column totals existed.
        _trim_totals(matrix, "column", keys)

    delivered = row_offset + len(matrix["row_keys"])
    total_columns = len(matrix["column_keys"]) if total_columns is None else total_columns
    delivered_columns = column_offset + len(matrix["column_keys"])
    matrix["bounds"]["next_row_offset"] = delivered if delivered < total_rows else None
    matrix["bounds"]["column_offset"] = column_offset
    matrix["bounds"]["total_column_keys"] = total_columns
    matrix["bounds"]["next_column_offset"] = (
        delivered_columns if delivered_columns < total_columns else None
    )
    matrix["bounds"]["rows_truncated"] = (
        matrix["bounds"]["rows_truncated"] or delivered < total_rows
    )
    matrix["bounds"]["columns_truncated"] = (
        matrix["bounds"]["columns_truncated"] or delivered_columns < total_columns
    )
    matrix["bounds"]["rows_dropped"] = max(total_rows - delivered, 0)
    matrix["bounds"]["columns_dropped"] = max(total_columns - delivered_columns, 0)
    # WHAT THE TOTALS SPAN, SAID RATHER THAN INFERRED.
    #
    # A total is a total of the FILTERED SET, not of the page: a column total on
    # a page serving 2 of 5 row keys prints the sum over all five. That is the
    # right number and the wrong reading -- measured, a page showing cells that
    # add to 20 printed a column total of 50 -- so the matrix says which set it
    # covered and whether the page shows all of it. Same defect as a total whose
    # key the trim dropped, one step out: there the key was gone, here the key is
    # served and its ROWS are not.
    #
    # Not `page`: recomputing the totals over the served page would make the
    # figure change as a window is resized, and a subtotal that moves when
    # nothing about the data moved is worse than one that needs a label.
    matrix["bounds"]["totals_span"] = "all_filtered_rows"
    # COMPTER CE QUE LA PAGE SERT, PAS CE QUI RESTE APRÈS ELLE.
    #
    # La première version lisait `rows_dropped`, qui ne compte QUE la queue
    # coupée -- cap d'axe, cap de cellules, trim d'octets. Elle ne compte jamais
    # la TÊTE sautée par `row_offset`, et c'est précisément la tête que le pageur
    # saute. Mesuré sur le scénario même que la réparation revendiquait fermer
    # (5 clés, `row_offset: 3`) : cellules visibles 20, total de colonne 50,
    # `rows_dropped` 0, drapeau `False`. AUCUNE page de ce Result ne portait
    # l'étiquette, à aucun offset.
    #
    # `len(row_keys) < total_rows` attrape les deux : la tête sautée et la queue
    # coupée. C'est la seule question qui compte -- « cette page montre-t-elle
    # tout ce que le total compte ? ».
    matrix["bounds"]["totals_cover_unserved_rows"] = len(matrix["row_keys"]) < total_rows
    matrix["bounds"]["totals_cover_unserved_columns"] = (
        len(matrix["column_keys"]) < total_columns
    )
    # Le budget en octets ne se declare que s'il a REELLEMENT coupe ici, et il
    # n'ecrase pas une raison deja posee : un plafond de cellules qui a coupe
    # AVANT reste la premiere cause, et c'est lui que l'utilisateur doit reduire.
    if (
        matrix["bounds"].get("truncation_reason") is None
        and (delivered < total_rows or delivered_columns < total_columns)
    ):
        matrix["bounds"]["truncation_reason"] = TRUNCATED_BY_BYTE_BUDGET
    # AC 9. ONE TOKEN PER AXIS, and one for this page. The two axes page
    # INDEPENDENTLY -- the console has a Rows pager and a Columns pager side by
    # side -- so a single "next" token would advance both the moment both had a
    # next page, and the reader would lose a column they never asked to leave.
    # `cursor` names the page being SERVED, so a caller walks back to it without
    # keeping its own arithmetic.
    next_row = matrix["bounds"]["next_row_offset"]
    next_column = matrix["bounds"]["next_column_offset"]
    matrix["bounds"]["cursor"] = _token(row_offset, column_offset)
    matrix["bounds"]["next_row_cursor"] = (
        _token(next_row, column_offset) if next_row is not None else None
    )
    matrix["bounds"]["next_column_cursor"] = (
        _token(row_offset, next_column) if next_column is not None else None
    )
    # response_bytes includes itself and every cursor/truncation field. Iterate
    # to the fixed point because changing the digit count can change the bytes.
    response_bytes = 0
    for _ in range(16):
        matrix["bounds"]["response_bytes"] = response_bytes
        measured = len(canonical_bytes(matrix))
        if measured == response_bytes:
            break
        response_bytes = measured
    matrix["bounds"]["response_bytes"] = response_bytes
    if len(canonical_bytes(matrix)) > max_response_bytes:
        raise PivotRefused(
            "pivot_row_exceeds_budget",
            "One pivot row is too large to serve safely. Narrow the axes or use a "
            "shorter display value.",
            detail={"max_response_bytes": max_response_bytes},
        )
    return matrix


def canonical_bytes(value: Any) -> bytes:
    """The bytes the WIRE will carry, not an estimate of them.

    `separators=(",", ":")` is what Starlette's `JSONResponse` writes, so the
    budget is measured in the same units the caller receives. Measuring with the
    default separators would over-count by ~7 % and cut pages nobody needed cut;
    measuring by cell count would under-count without bound.
    """
    import json  # noqa: PLC0415

    return json.dumps(value, separators=(",", ":"), default=str).encode("utf-8")


def _cell_value(
    field: str,
    measures: Mapping[str, Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    indexes: Sequence[int],
) -> dict[str, Any]:
    """One value of one cell, with the reason when there is nothing to show."""
    descriptor = measures[field]
    if not indexes:
        return {"value": None, "absent_reason": NO_CONTRIBUTING_ROW}

    if descriptor.get("role") == "ratio":
        # RECOMPUTED, NEVER FOLDED. Summing or averaging the ratios of the rows
        # underneath gives a different number, and it is always the wrong one.
        numerator_id = descriptor.get("numerator_field_id")
        denominator_id = descriptor.get("denominator_field_id")
        numerator_field = _field_named(measures, numerator_id)
        denominator_field = _field_named(measures, denominator_id)
        if not numerator_field or not denominator_field:
            return {"value": None, "absent_reason": RATIO_COMPONENT_MISSING}
        numerator = _fold((rows[i].get(numerator_field) for i in indexes), "sum")
        denominator = _fold((rows[i].get(denominator_field) for i in indexes), "sum")
        if numerator is None or not denominator:
            return {"value": None, "absent_reason": RATIO_COMPONENT_MISSING}
        return {
            "value": numerator / denominator,
            "recomputed_from": [numerator_field, denominator_field],
        }

    aggregation = str(descriptor.get("aggregation") or "sum").lower()
    if aggregation not in FOLDABLE:
        return {"value": None, "absent_reason": f"aggregation_not_foldable:{aggregation}"}
    value = _fold((rows[i].get(field) for i in indexes), aggregation)
    if value is None:
        return {"value": None, "absent_reason": NO_CONTRIBUTING_ROW}
    return {"value": value}


def _comparison_deltas(
    cells: Sequence[Mapping[str, Any]],
    *,
    row_fields: Sequence[str],
    column_fields: Sequence[str],
    value_fields: Sequence[str],
    period_field: str,
) -> list[dict[str, Any]]:
    """Pair current/baseline cells after removing only the period coordinate."""
    period_axis = "row" if period_field in row_fields else "column"
    period_index = (
        row_fields.index(period_field)
        if period_axis == "row"
        else column_fields.index(period_field)
    )
    paired: dict[tuple[Any, ...], dict[str, Mapping[str, Any]]] = {}
    wire_coordinates: dict[tuple[Any, ...], tuple[list[Any], list[Any]]] = {}
    for cell in cells:
        row_key = list(cell.get("row_key") or [])
        column_key = list(cell.get("column_key") or [])
        axis = row_key if period_axis == "row" else column_key
        if period_index >= len(axis) or axis[period_index] not in {"current", "baseline"}:
            continue
        period = str(axis[period_index])
        base_row = row_key[:]
        base_column = column_key[:]
        if period_axis == "row":
            base_row.pop(period_index)
        else:
            base_column.pop(period_index)
        identity = tuple(
            _typed_scalar(value) for value in [*base_row, "|", *base_column]
        )
        paired.setdefault(identity, {})[period] = cell
        wire_coordinates[identity] = (base_row, base_column)

    deltas: list[dict[str, Any]] = []
    for identity, periods in paired.items():
        row_key, column_key = wire_coordinates[identity]
        for value_field in value_fields:
            current = ((periods.get("current") or {}).get("values") or {}).get(value_field) or {
                "value": None,
                "absent_reason": NO_CONTRIBUTING_ROW,
            }
            baseline = ((periods.get("baseline") or {}).get("values") or {}).get(value_field) or {
                "value": None,
                "absent_reason": NO_CONTRIBUTING_ROW,
            }
            current_value = current.get("value")
            baseline_value = baseline.get("value")
            entry = {
                "row_key": row_key,
                "column_key": column_key,
                "value_field": value_field,
                "current": current,
                "baseline": baseline,
            }
            if current_value is None or baseline_value is None:
                unavailable = {"value": None, "absent_reason": COMPARISON_VALUE_MISSING}
                entry["absolute_delta"] = unavailable
                entry["relative_delta"] = unavailable
            elif (
                isinstance(current_value, bool)
                or isinstance(baseline_value, bool)
                or not isinstance(current_value, (int, float))
                or not isinstance(baseline_value, (int, float))
            ):
                unavailable = {"value": None, "absent_reason": COMPARISON_VALUE_NOT_NUMERIC}
                entry["absolute_delta"] = unavailable
                entry["relative_delta"] = unavailable
            else:
                absolute = current_value - baseline_value
                entry["absolute_delta"] = {"value": absolute}
                entry["relative_delta"] = (
                    {"value": None, "absent_reason": COMPARISON_BASELINE_ZERO}
                    if baseline_value == 0
                    else {"value": absolute / abs(baseline_value)}
                )
            deltas.append(entry)
    return deltas


def _comparison_axis_identity(key: Sequence[Any], period_index: int) -> tuple[Any, ...]:
    return tuple(
        _typed_scalar(value)
        for index, value in enumerate(key)
        if index != period_index
    )


def _complete_comparison_groups(
    keys: Sequence[tuple[Any, ...]], period_index: int, maximum: int
) -> list[tuple[Any, ...]]:
    """Keep complete adjacent current/baseline groups under the column cap."""
    kept: list[tuple[Any, ...]] = []
    index = 0
    while index < len(keys):
        identity = _comparison_axis_identity(keys[index], period_index)
        end = index + 1
        while end < len(keys) and _comparison_axis_identity(keys[end], period_index) == identity:
            end += 1
        group = list(keys[index:end])
        if kept and len(kept) + len(group) > maximum:
            break
        if len(group) > maximum:
            raise PivotRefused(
                "comparison_pair_exceeds_bound",
                "One comparison group exceeds the bounded pivot column count.",
            )
        kept.extend(group)
        index = end
    return kept


def _refresh_comparison_deltas(matrix: dict[str, Any]) -> None:
    comparison = matrix.get("comparison")
    if not isinstance(comparison, dict):
        return
    period_field = str(comparison.get("period_field") or "")
    if not period_field:
        return
    comparison["deltas"] = _comparison_deltas(
        matrix.get("cells") or [],
        row_fields=matrix.get("row_fields") or [],
        column_fields=matrix.get("column_fields") or [],
        value_fields=[
            str(entry.get("name") or "")
            for entry in matrix.get("value_fields") or []
            if entry.get("name")
        ],
        period_field=period_field,
    )
    if comparison["deltas"]:
        comparison.pop("unavailable_reason", None)
    else:
        comparison["unavailable_reason"] = COMPARISON_VALUE_MISSING


def _field_named(measures: Mapping[str, Mapping[str, Any]], canonical_field_id: Any) -> str | None:
    for name, descriptor in measures.items():
        if descriptor.get("canonical_field_id") == canonical_field_id:
            return name
    return None


def _apply_filters(
    rows: Sequence[Mapping[str, Any]],
    filters: Sequence[Any],
    dimensions: Mapping[str, dict],
) -> tuple[list[Mapping[str, Any]], list[int], list[dict[str, Any]]]:
    """Presentation filters: they narrow what is SHOWN of an answer already given.

    They never re-query and never change a value. A filter that needed the
    warehouse is an analytical edit, and an analytical edit is a new Result.
    """
    applied: list[dict[str, Any]] = []
    kept = list(enumerate(rows))
    for entry in filters:
        if not isinstance(entry, dict):
            raise PivotRefused("malformed_filter", "Every pivot filter is an object.")
        field = str(entry.get("field") or "")
        if field not in dimensions:
            raise PivotRefused(
                "unknown_dimension",
                f"{field} is not a dimension of this Result.",
                detail=sorted(dimensions),
            )
        values = entry.get("in")
        if not isinstance(values, list) or not values:
            raise PivotRefused(
                "filter_needs_values",
                "A pivot filter keeps the listed values of a field.",
            )
        wanted = {_typed_scalar(value) for value in values}
        kept = [(index, row) for index, row in kept if _typed_scalar(row.get(field)) in wanted]
        applied.append({"field": field, "in": values})
    return [row for _index, row in kept], [index for index, _row in kept], applied


def _typed_scalar(value: Any) -> tuple[str, Any]:
    if value is None:
        return ("null", None)
    if isinstance(value, bool):
        return ("boolean", value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return ("number", value)
    if isinstance(value, str):
        return ("string", value)
    raise PivotRefused("filter_value_not_scalar", "Pivot filter values are JSON scalars.")


def _axis_keys(
    rows: Sequence[Mapping[str, Any]], fields: Sequence[str], *, direction: str = "asc"
) -> list[tuple]:
    """Distinct keys of one axis, in a stable order.

    Sorted on the string form so the order does not depend on the row order the
    warehouse happened to return -- two runs of one Result must lay out the same.
    """
    if not fields:
        return [()]
    seen = {_key(row, fields) for row in rows}
    return sorted(
        seen,
        key=lambda key: tuple(_sort_token(value) for value in key),
        reverse=direction == "desc",
    )
