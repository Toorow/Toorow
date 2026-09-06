"""Placement Mapping -- THE TWO COLUMNS IT ADDS TO AN AGGREGATION.

WHAT THIS MODULE IS. `docs/product-architecture/project-settings.md` ratified
Placement Mapping an **aggregation option** on 2026-08-07, and
`docs/product-architecture/capabilities/placement-mapping.md` named the two
columns it adds: `plan_line_key` and `plan_line_label`. Until this file the two
names existed in the repository as a COMMENT and nothing else --
`core/analytics_alignment.py:84-85` cited them as the precedent for its own three
columns, and `grep -rn 'plan_line_key|plan_line_label' server dbt ui/admin/src`
returned those two lines and no third. A capability whose columns exist only as
the precedent for somebody else's columns adds nothing to any aggregation.

This module owns the two names, the gate that decides whether an aggregation
gains them at all, and the projection that fills them -- with NULL, and never a
sentinel, on a row no plan line matched. It owns no engine: the matching is
`core.plan_mapping_suggest`, the store is `app.plan_line_mappings`, the
ventilation is `dbt/models/marts/plan_vs_actual_daily.sql`, and none of the three
is re-decided here.

THE FAMILY IT BELONGS TO, and the module it mirrors. `core.analytics_alignment`
ships the same three pieces for the family's seventh member: an `ADDED_COLUMNS`
tuple, an `added_columns(state)` gate read through `capability_is_active`, and a
`columns()` projection that returns `dict.fromkeys(ADDED_COLUMNS, None)` on every
row its cascade did not match. The shapes below are that module's, deliberately,
so a reader who has met one has met both.

NULL IS THE MEASUREMENT, AND IT IS THE WHOLE POINT. `placement-mapping.md`:

    Both columns are null on a row no plan line matches, and that is a
    measurement rather than a gap: a connector reports every campaign it ran,
    and a plan covers the ones somebody planned. A `0`, an empty string or an
    `Unmapped` sentinel would each make unplanned spend look planned. Rows
    carrying null on both columns are exactly the fourth matching state, and
    they are the ones that reveal spend nobody budgeted.

So :func:`plan_line_columns` has exactly two outcomes and no third: two values,
or two NULLs. There is no branch that can emit one of the three sentinels,
because there is no branch that composes a value out of an absence -- a blank
`line_key` IS an absence and is normalised to ``None`` before anything reads it.

A NAME WITHOUT AN IDENTITY IS REFUSED, and that is the one thing this module
raises about. `plan_line_label` is the label of the line `plan_line_key` names;
a label carried beside a null key would state that a row matched something
nobody can look up, which is the same lie as `Unmapped` wearing better clothes.
The reverse is not symmetric and is not refused: a key whose line carries no
label is a plan line somebody imported without one, and the honest render of
that is the key with an absent name.

WHAT THE SECOND WORD OF EACH COLUMN IS NOT. `plan_line_key` is
`app.media_plan_lines.line_key` -- the STABLE cross-version identity -- and never
`app.media_plan_lines.id`, the versioned row id. The column survives a re-import
of the plan precisely because it is the first and not the second, and
`plan_vs_actual_daily.sql` already grains itself on the same word.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from core.plan_matching_states import (
    MATCHING_STATE_MATCHED,
    MATCHING_STATE_UNMATCHED,
)
from core.project_capability_states import (
    PLACEMENT_MAPPING_CAPABILITY_KEY,
    capability_is_active,
)

# ---------------------------------------------------------------------------
# The key, and the columns it adds.
# ---------------------------------------------------------------------------

#: Both column names are DERIVED from this prefix, on the patron
#: `analytics_alignment.COLUMN_PREFIX` states: a name that can be computed from
#: the declaration is a second place for the declaration to be contradicted.
COLUMN_PREFIX = "plan_line"

#: The STABLE `app.media_plan_lines.line_key` of the matched line -- not the
#: versioned row id, so the column survives a re-import of the plan.
PLAN_LINE_KEY_COLUMN = f"{COLUMN_PREFIX}_key"

#: `app.media_plan_lines.label` of that same line, read at the plan's ACTIVE
#: version. The name a person recognises, beside the identity a machine joins on.
PLAN_LINE_LABEL_COLUMN = f"{COLUMN_PREFIX}_label"

#: The two, in the order a reader meets them: the identity, then the name.
ADDED_COLUMNS: tuple[str, ...] = (PLAN_LINE_KEY_COLUMN, PLAN_LINE_LABEL_COLUMN)

#: The three values `placement-mapping.md` forbids these columns to carry where
#: no plan line matched. Declared so the rule is greppable and testable rather
#: than only true by construction -- and NEVER consulted to decide what to emit:
#: nothing in this module can produce one of them, which is the actual guarantee.
#: A plan line whose own `line_key` happens to BE one of these words is a match
#: and carries it; the fault the document names is a sentinel standing in for an
#: absence, not a string that looks like one.
FORBIDDEN_SENTINELS: tuple[str, ...] = ("0", "", "Unmapped")

#: Why the two columns are null, said once, where the value is composed. Carried
#: onto a payload rather than re-spelled by each surface -- the discipline
#: `datastream_workbench_placements` already follows for its own sentences.
NULL_IS_THE_MEASUREMENT = (
    "Both columns are empty on a row no plan line matched. A connector reports every "
    "campaign it ran and a plan covers the ones somebody planned, so an empty pair is "
    "spend nobody budgeted -- not a gap in the reading."
)


class PlacementMappingRefused(ValueError):
    """A projection that would state a match nobody can look up. Refused."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _text(value: Any) -> str | None:
    """The value, or ``None`` -- and a blank is an ABSENCE, never an empty string.

    This is the single line that makes `""` unreachable in either column. Every
    caller path funnels through it, so the emptiest of the three forbidden
    sentinels cannot be emitted even by a caller that hands one in.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def plan_line_columns(
    *, line_key: Any = None, label: Any = None
) -> dict[str, str | None]:
    """The two added columns for ONE row of an aggregation.

    Two outcomes and no third:

      * ``line_key`` names a plan line -> the key and its label, the label
        ``None`` when the line carries none;
      * nothing matched -> ``dict.fromkeys(ADDED_COLUMNS, None)``.

    A label handed in with no key is :class:`PlacementMappingRefused`
    (``label_without_a_plan_line``): it would state that this row matched
    something no reader can look up, which is what `0`, `""` and `Unmapped` each
    do less honestly.
    """
    key = _text(line_key)
    name = _text(label)
    if key is None:
        if name is not None:
            raise PlacementMappingRefused(
                "label_without_a_plan_line",
                "A plan line label with no line key names a match nobody can look up. "
                "A row no plan line matched carries both columns empty.",
            )
        return dict.fromkeys(ADDED_COLUMNS, None)
    return {PLAN_LINE_KEY_COLUMN: key, PLAN_LINE_LABEL_COLUMN: name}


def unmatched_columns() -> dict[str, None]:
    """The two columns of a row no plan line matched. Both null, always."""
    return dict.fromkeys(ADDED_COLUMNS, None)


def is_matched(columns: Mapping[str, Any]) -> bool:
    """Did a plan line match this row? The KEY decides, and only the key.

    Read from the key alone because the label is allowed to be absent on a real
    match: a plan imported without labels would otherwise report every one of
    its lines as unmatched spend.
    """
    return _text(columns.get(PLAN_LINE_KEY_COLUMN)) is not None


def matching_state(columns: Mapping[str, Any]) -> str:
    """`matched` or `unmatched`, in the BORROWED vocabulary and not a fifth word.

    `plan_matching_states` owns the four words of this axis and
    `placement-mapping.md` says which one a null pair is: *"Rows carrying null on
    both columns are exactly the fourth matching state"*. The other two --
    `ambiguous` and `accepted` -- are decisions taken elsewhere, on a store this
    module does not read, so they are not derivable from two columns and are not
    guessed here.
    """
    return MATCHING_STATE_MATCHED if is_matched(columns) else MATCHING_STATE_UNMATCHED


def add_plan_line_columns(
    rows: Iterable[Mapping[str, Any]],
    *,
    labels: Mapping[str, Any] | None = None,
    key_field: str = "line_key",
) -> list[dict[str, Any]]:
    """Project the two columns onto every row of an aggregation.

    ``labels`` maps a `line_key` to the label of that line AT THE PLAN'S ACTIVE
    VERSION; a key it does not name yields a matched row with an absent label,
    which is the honest render of a mapping that survives a line's re-import.
    A row whose ``key_field`` is absent or blank gains two nulls.

    Returns NEW dicts. The caller's rows are never mutated, so an aggregation can
    be read twice -- once with the capability on and once off -- and the second
    read is not contaminated by the first.
    """
    lookup = dict(labels or {})
    projected: list[dict[str, Any]] = []
    for row in rows:
        key = _text(row.get(key_field))
        projected.append(
            {
                **dict(row),
                **plan_line_columns(
                    line_key=key, label=lookup.get(key) if key is not None else None
                ),
            }
        )
    return projected


def added_columns(capability_state: Any) -> tuple[str, ...]:
    """The columns an aggregation gains, which is NOTHING while the switch is off.

    Read through `capability_is_active` and never against a literal, for the
    reason `analytics_alignment.added_columns` states in full: the lens of
    `governance_read_model` compared a state with `"enabled"` -- a word migration
    131's CHECK does not admit -- and could therefore never be true for anybody.

    « Éteinte, la capacité n'apparaît nulle part -- ni onglet, ni panneau, ni
    colonne. » An empty tuple is what makes the last of those three checkable.
    """
    return ADDED_COLUMNS if capability_is_active(capability_state) else ()


__all__ = [
    "ADDED_COLUMNS",
    "COLUMN_PREFIX",
    "FORBIDDEN_SENTINELS",
    "NULL_IS_THE_MEASUREMENT",
    "PLACEMENT_MAPPING_CAPABILITY_KEY",
    "PLAN_LINE_KEY_COLUMN",
    "PLAN_LINE_LABEL_COLUMN",
    "PlacementMappingRefused",
    "add_plan_line_columns",
    "added_columns",
    "is_matched",
    "matching_state",
    "plan_line_columns",
    "unmatched_columns",
]
