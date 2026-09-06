"""Story 70.1 -- one stored schema set, compiled to both warehouse dialects.

WHY THIS MODULE IS NOT IN `column_treatments`. That module ends its own docstring
with "Pure module: no I/O, no database, no dialect", and it means it: it answers
about ONE string and knows nothing about SQL. This is the other half -- the same
stored declaration, emitted as the SQL a warehouse runs at READ time -- and it
follows the patron `cleanup_rules` set for exactly this situation: the row stores
the declaration, the module emits both dialects from it, and a conformance test
proves the two agree (`tests/conformance/test_schema_split_dialects.py`).

APPLIED AT READ, LIKE EVERY DERIVATION BEFORE IT. `122_derived_columns.sql:23-30`
wrote the reason once and it has not changed: "changing a regex costs nothing, no
refetch, no 16-month backfill". A layout set is edited far more often than a
regular expression -- a new publisher code, a nineteenth dimension -- and every
edit here is free because the raw zone still holds the value as it was collected.

THE DETECTOR IS `MIN(offset)`, AND THE MINIMUM IS TAKEN OVER EVERY OFFSET. The
compiled CASE scans offsets 0, 1, 2 ... ascending and stops at the FIRST one
carrying an anchor token; an offset no layout declares yields NULL and stops the
scan there. That is not a shortcut for "keep looking": an anchor closer to the
left than anything declared means the value is shaped like nothing the set
describes, and the honest answer is an undetected row. Scanning past it would
find the second-best layout and produce a wrong answer that looks right --
exactly what the story's Refuse line forbids.

VALUES ARE BOUND, IDENTIFIERS ARE QUOTED BY ONE OWNER. Separators, anchor tokens
and layout names all travel as parameters; the only things formatted into the
string are identifiers quoted through `cleanup_rules.quote_identifier` and
positions, which are integers this module validated. DuckDB counts its
placeholders, so a value appearing in three fragments is bound three times and
the emission order of the fragments IS the order of `params` -- the discipline
`CompiledRule.projection_params` documents, at a larger scale.

TWO ENGINES, TWO VOCABULARIES. `SPLIT` / `str_split`, `ARRAY_LENGTH` / `len`,
`SAFE_OFFSET(n)` / `[n + 1]`, `EXCEPT` / `EXCLUDE`, and a tail that BigQuery
takes with an `UNNEST ... WITH OFFSET` subquery where DuckDB has `list_slice`.
None of that is guessed: the DuckDB half is EXECUTED by the conformance test
against a real DuckDB, and the BigQuery half is held to the same banned-token
discipline as `test_cleanup_rule_dialects.py`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from core.cleanup_rules import (
    DIALECTS,
    ExpressionError,
    placeholder,
    quote_identifier,
    validate_field,
)
from core.column_treatments import (
    provenance_columns,
    read_schema_split,
    schema_split_targets,
)

#: A relation is a name, never client text. Same bound as `cleanup_rules._relation`.
_RELATION = re.compile(r"[A-Za-z0-9_.]+")

#: The column carrying the token list between the levels of the statement. It is
#: dropped from the final projection: it is scaffolding, not a reading.
_TOKENS_SUFFIX = "__tokens"


class SchemaSplitError(ExpressionError):
    """The schema set cannot be compiled. Same family as every other refusal on
    this read path, so one screen speaks one language about a failure."""


@dataclass(frozen=True, slots=True)
class CompiledSchemaSplit:
    """One schema set, in one dialect, with the values its SQL expects bound.

    `sql` never contains a separator, an anchor token or a layout name: those are
    in `params`, in the order the driver must bind them.
    """

    dialect: str
    sql: str
    params: tuple[str, ...]
    #: The columns this projection ADDS, in emission order: the three provenance
    #: columns, the declared remainder, then the union of the layouts' targets.
    columns: tuple[str, ...] = ()


class _Bindings:
    """The placeholder allocator. It hands out refs in TEXT order and remembers
    what each one binds -- which is the only way a positional dialect and a named
    one can be served by one emission."""

    def __init__(self, dialect: str, first_param: int) -> None:
        self._dialect = dialect
        self._index = int(first_param)
        self.values: list[str] = []

    def bind(self, value: str) -> str:
        ref = placeholder(self._dialect, self._index)
        self._index += 1
        self.values.append(value)
        return ref


# ---------------------------------------------------------------------------
# The two vocabularies. Every divergence between the warehouses lives here and
# nowhere else, so a third dialect would be a third branch in five functions
# rather than a search across the module.
# ---------------------------------------------------------------------------


def _split_sql(dialect: str, value_sql: str, separator_ref: str) -> str:
    if dialect == "bigquery":
        return f"SPLIT({value_sql}, {separator_ref})"
    return f"str_split({value_sql}, {separator_ref})"


def _length_sql(dialect: str, tokens_sql: str) -> str:
    return f"ARRAY_LENGTH({tokens_sql})" if dialect == "bigquery" else f"len({tokens_sql})"


def _token_at_sql(dialect: str, tokens_sql: str, offset: int) -> str:
    """The token at a 0-based offset, NULL when the row is shorter than that.

    Both engines answer NULL rather than raising -- `SAFE_OFFSET` by name, DuckDB
    by its own list semantics -- which is what makes a short row readable instead
    of fatal.
    """
    if dialect == "bigquery":
        return f"{tokens_sql}[SAFE_OFFSET({int(offset)})]"
    return f"{tokens_sql}[{int(offset) + 1}]"


def _tail_sql(dialect: str, tokens_sql: str, offset: int, separator_ref: str) -> str:
    """Everything from `offset` on, joined back with the separator it was cut by.

    This is the `remainder`: what exceeds the last position of the chosen layout.
    It is rebuilt with the SAME separator, so the value can be read back as it
    was collected rather than as a list somebody re-punctuated.
    """
    if dialect == "bigquery":
        return (
            f"ARRAY_TO_STRING(ARRAY(SELECT tok FROM UNNEST({tokens_sql}) AS tok "
            f"WITH OFFSET tok_offset WHERE tok_offset >= {int(offset)} "
            f"ORDER BY tok_offset), {separator_ref})"
        )
    return (
        f"array_to_string(list_slice({tokens_sql}, {int(offset) + 1}, "
        f"{_length_sql(dialect, tokens_sql)}), {separator_ref})"
    )


def _string_sql(dialect: str, ref: str) -> str:
    """A bound value the engine must read as text.

    The layout CASE has branches that yield NULL, so nothing else in it says what
    type it produces. An engine left to infer it can refuse the statement, and it
    would refuse it at read time on a client's screen instead of here.
    """
    return f"CAST({ref} AS STRING)" if dialect == "bigquery" else f"CAST({ref} AS VARCHAR)"


def _drop_column_sql(dialect: str, alias: str, column_sql: str) -> str:
    """`SELECT everything except the scaffolding`, spelled by each engine."""
    keyword = "EXCEPT" if dialect == "bigquery" else "EXCLUDE"
    return f"{alias}.* {keyword} ({column_sql})"


# ---------------------------------------------------------------------------
# The emission
# ---------------------------------------------------------------------------


def compile_schema_split(
    *,
    entry: dict[str, Any],
    dialect: str,
    relation: str,
    source_column: str,
    first_param: int = 0,
) -> CompiledSchemaSplit:
    """Emit the read of ONE schema set over one relation, for one dialect.

    `source_column` is the PHYSICAL column holding the collected value. It is
    passed in rather than taken from the declaration's `source`, which is a file
    header a person typed ("Placement code") and not an identifier a warehouse
    carries -- guessing one from the other is how a compiled read names a column
    that does not exist.

    `first_param` is where this statement's placeholders start, so a caller that
    already bound a project keeps one continuous numbering for BigQuery.
    """
    if dialect not in DIALECTS:
        raise SchemaSplitError(f"unknown dialect: {dialect!r}. One of {DIALECTS}")
    if not _RELATION.fullmatch(relation or ""):
        raise SchemaSplitError("invalid relation name")
    # Validated, never escaped: an identifier that could break out of its quoting
    # is impossible by construction here, the discipline `cleanup_rules` states.
    column = validate_field(source_column)

    parsed = read_schema_split(entry)
    name = parsed["name"]
    provenance = provenance_columns(parsed)
    remainder_column = parsed["remainder"]
    targets = schema_split_targets(parsed)

    quoted = {
        "tokens": quote_identifier(dialect, f"{name}{_TOKENS_SUFFIX}"),
        "layout": quote_identifier(dialect, provenance["layout"]),
        "token_count": quote_identifier(dialect, provenance["token_count"]),
    }

    # The statement reads outside-in, so its placeholders are allocated
    # outside-in too: a positional driver binds by order of APPEARANCE, and the
    # innermost SELECT is the last text in the string.
    outer = _Bindings(dialect, first_param)
    outer_columns = _outer_columns(
        dialect, outer, parsed, quoted, provenance, remainder_column, targets
    )
    middle = _Bindings(dialect, first_param + len(outer.values))
    layout_case = _layout_case_sql(dialect, middle, parsed, quoted["tokens"])
    inner = _Bindings(dialect, first_param + len(outer.values) + len(middle.values))
    tokens_expr = _split_sql(
        dialect, quote_identifier(dialect, column), inner.bind(parsed["separator"])
    )

    level_one = (
        f"SELECT s0.*, {tokens_expr} AS {quoted['tokens']} FROM {relation} AS s0"
    )
    level_two = (
        f"SELECT s1.*, {layout_case} AS {quoted['layout']}, "
        f"{_length_sql(dialect, quoted['tokens'])} AS {quoted['token_count']} "
        f"FROM ({level_one}) AS s1"
    )
    projected = ", ".join(
        f"{sql} AS {quote_identifier(dialect, added)}" for added, sql in outer_columns
    )
    sql = (
        f"SELECT {_drop_column_sql(dialect, 's2', quoted['tokens'])}, {projected} "
        f"FROM ({level_two}) AS s2"
    )

    return CompiledSchemaSplit(
        dialect=dialect,
        sql=sql,
        params=(*outer.values, *middle.values, *inner.values),
        columns=(
            provenance["layout"],
            provenance["token_count"],
            provenance["token_count_matches_schema"],
            remainder_column,
            *targets,
        ),
    )


def _layout_case_sql(
    dialect: str, bindings: _Bindings, parsed: dict[str, Any], tokens_sql: str
) -> str:
    """`MIN(offset)` as a CASE: the first offset carrying an anchor decides.

    Offsets are scanned ascending up to the largest one any layout declares.
    An offset carrying an anchor that NO layout declares yields NULL and ends the
    scan -- the row is shaped like nothing that was declared, and naming the next
    layout would be a guess.
    """
    branches: list[str] = []
    layout_by_offset: dict[int, str] = parsed["layout_by_offset"]
    for offset in range(int(parsed["scan_limit"]) + 1):
        anchors = ", ".join(bindings.bind(token) for token in parsed["anchor_tokens"])
        layout_name = layout_by_offset.get(offset)
        answer = (
            _string_sql(dialect, bindings.bind(layout_name))
            if layout_name is not None
            else "NULL"
        )
        branches.append(
            f"WHEN {_token_at_sql(dialect, tokens_sql, offset)} IN ({anchors}) THEN {answer}"
        )
    return "CASE " + " ".join(branches) + " ELSE NULL END"


def _outer_columns(
    dialect: str,
    bindings: _Bindings,
    parsed: dict[str, Any],
    quoted: dict[str, str],
    provenance: dict[str, str],
    remainder_column: str,
    targets: list[str],
) -> list[tuple[str, str]]:
    """`(column name, SQL)` for everything computed from the chosen layout.

    They read `layout` and `token_count` by NAME rather than recomputing the
    detector, which is what keeps one row's answers consistent with each other:
    a second copy of the CASE could be edited alone and put a target value beside
    a layout that did not produce it.

    The list is built in the order it will be written into the SELECT, because
    `bindings` hands out placeholders as it goes and a positional driver binds by
    order of appearance.
    """
    layouts = parsed["layouts"]
    columns: list[tuple[str, str]] = []

    # Does the row carry the number of tokens the layout describes? A row that
    # says no is READ as such -- it is never repaired into the layout's shape.
    matches = " ".join(
        f"WHEN {bindings.bind(layout['name'])} THEN "
        f"{quoted['token_count']} = {len(layout['targets'])}"
        for layout in layouts
    )
    columns.append(
        (
            provenance["token_count_matches_schema"],
            f"CASE {quoted['layout']} {matches} ELSE NULL END",
        )
    )

    # What exceeds the last declared position, rebuilt with its own separator.
    # NULL when nothing exceeds it -- an empty string would read as "there was
    # something there and it was blank".
    remainder = " ".join(
        f"WHEN {bindings.bind(layout['name'])} THEN CASE WHEN "
        f"{quoted['token_count']} > {len(layout['targets'])} THEN "
        + _tail_sql(
            dialect,
            quoted["tokens"],
            len(layout["targets"]),
            bindings.bind(parsed["separator"]),
        )
        + " ELSE NULL END"
        for layout in layouts
    )
    columns.append(
        (remainder_column, f"CASE {quoted['layout']} {remainder} ELSE NULL END")
    )

    # One column per concept, whichever layout produced it. Two layouts naming
    # one concept at two positions are alternatives for the same row, never
    # rivals, so they share the column and the row's layout picks the position.
    for target in targets:
        branches = " ".join(
            f"WHEN {bindings.bind(layout['name'])} THEN "
            f"{_token_at_sql(dialect, quoted['tokens'], layout['targets'].index(target))}"
            for layout in layouts
            if target in layout["targets"]
        )
        columns.append((target, f"CASE {quoted['layout']} {branches} ELSE NULL END"))

    return columns


__all__ = [
    "CompiledSchemaSplit",
    "SchemaSplitError",
    "compile_schema_split",
]
