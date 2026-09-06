"""The raw value and the value it becomes, ON ONE ROW -- lot B2, amendment 12.

WHY THIS EXISTS, IN THE WORDS THAT ORDERED IT. Epic 58 asked for "une colonne par
champ du mapping actif avec sa valeur brute ET sa valeur mappee", and what shipped
was two tables that do not join plus a paragraph explaining why. The paragraph was
TRUE -- `datastream_mappings.target_field` has no key to `fact_daily_kpi.metric`,
which is a literal typed into a dbt model -- and it was an answer to a different
question. `fact_daily_kpi` is the MART; the two readings a person compares here are
`collected` and `mapped`, and the key between THOSE is one this product already
owns: the active mapping says `source -> target`, the collected relation carries
the source columns, the mapped relation carries the target columns.

SO THE KEY IS THE MAPPING'S OWN PAIRS, AND NOTHING ELSE. Not a name that happens to
match on both sides, not an ordinal position in two differently ordered readings,
not the mart's metric literal. A column the mapping does not bind is not paired,
and it SAYS which side it came from -- inventing a pairing on spelling is the exact
defect the banner this replaces was written to avoid.

AND THE ROW KEY IS THE MAPPING'S GRAIN. Pairing columns is not pairing rows: a day
holds many rows and something has to say which row of one reading became which row
of the other. The mapping already answers it -- its key columns ARE the grain, the
fields the rows are keyed by -- so the join runs on the key pairs and refuses,
by name, when the mapping declares none.

THE JOIN RUNS ON THE VALUES THE DATABASE RETURNED, THE SCREEN GETS THE MASKED ONES.
`collected_mapped_reader._masked_columns` / `._mask` own the masking policy and
this module writes no second one: it reads the unmasked rows to compute a key and
an equality, and every value it publishes is taken from the ALREADY MASKED list
beside them, at the same index. Joining on the masked values instead would have
matched every row of a masked key against every other -- one sentinel, fifty rows --
which is a wrong join, and a wrong join served as a fact is what this lot exists to
remove.

WHAT IT NEVER DOES: read the warehouse (it is handed two readings that were already
bounded at `collected_mapped_reader.MAX_ROWS`), widen them, or compose a sentence
the screen could have composed. Every refusal here carries its own words because
the screen must hold none of its own.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

#: The key the reader hands its UNMASKED rows over on. Declared here rather than in
#: the reader so the two modules share ONE spelling of it: the reader produces it,
#: this module is the only consumer, and it never reaches a browser --
#: `collected_mapped_reader._INTERNAL_KEYS` strips it on the way out.
RAW_ROWS = "_raw_rows"

#: The two sides, named as the reading names them.
ZONE_COLLECTED = "collected"
ZONE_MAPPED = "mapped"

#: Why the two readings cannot be put on one row. Each is a different door.
SIDE_UNREADABLE = "side_unreadable"
NO_MAPPING_FIELD = "no_mapping_field"
NO_BOUND_FIELD = "no_bound_field"
NO_COLUMN_IN_BOTH_RELATIONS = "no_column_in_both_relations"
NO_KEY_COLUMN = "no_key_column"

#: Why ONE row stayed on its own side.
ROW_NO_COUNTERPART = "no_counterpart"
ROW_KEY_NOT_UNIQUE = "key_not_unique"

#: Why ONE column stayed unpaired.
COLUMN_UNBOUND = "unbound"
COLUMN_SOURCE_ABSENT = "source_absent_in_collected"
COLUMN_TARGET_ABSENT = "target_absent_in_mapped"
COLUMN_NAMED_BY_NO_FIELD = "named_by_no_mapping_field"

#: The sentence of each refusal, written on the server because the screen holds
#: none of its own -- one wording, whichever door it comes through. Each one names
#: the GESTURE that repairs it rather than the mechanism that failed.
_MESSAGES = {
    SIDE_UNREADABLE: (
        "One of the two readings could not be read, so there is nothing to pair it "
        "with. The reading that refused says why, in its own words."
    ),
    NO_MAPPING_FIELD: (
        "This Datastream has no mapping field, so nothing says which collected "
        "column becomes which mapped column. Bind the fields on Mapping and the two "
        "readings pair themselves here."
    ),
    NO_BOUND_FIELD: (
        "No field of the active mapping names a target, so no collected column can "
        "be said to become a mapped one. Bind a field to a canonical target on "
        "Mapping and its raw value and its mapped value appear on one row."
    ),
    NO_COLUMN_IN_BOTH_RELATIONS: (
        "No field of the active mapping names a column that both relations carry, so "
        "there is no pair of columns to put on one row. Point the fields at the "
        "columns these two relations really hold, on Mapping."
    ),
    NO_KEY_COLUMN: (
        "The active mapping names no key column that both relations carry, so a row "
        "of one reading cannot be said to be the row of the other. Mark the fields "
        "that key a row on Mapping; until then the two readings are shown one after "
        "the other and nothing is paired."
    ),
    ROW_NO_COUNTERPART: "No row of the other reading carries this key.",
    ROW_KEY_NOT_UNIQUE: (
        "Several rows carry this key, so none of them can be said to be the one that "
        "became the other."
    ),
}

#: And the sentence of an unpaired COLUMN, which needs the name to be worth reading.
_COLUMN_MESSAGES = {
    COLUMN_UNBOUND: "{name} is bound to no target, so it becomes nothing here.",
    COLUMN_SOURCE_ABSENT: (
        "{name} is named by the mapping and the collected relation does not carry it."
    ),
    COLUMN_TARGET_ABSENT: (
        "{name} is named by the mapping and the mapped relation does not carry it."
    ),
    COLUMN_NAMED_BY_NO_FIELD: (
        "{name} is a column of this relation that no mapping field names."
    ),
}


def message_for(reason: str | None) -> str | None:
    """The sentence of a refusal or of a row note, or `None`."""
    if not reason:
        return None
    return _MESSAGES.get(reason)


def column_message(reason: str, name: str) -> str | None:
    template = _COLUMN_MESSAGES.get(reason)
    return template.format(name=name) if template else None


def _text(value: Any) -> str:
    """One spelling of a value, so two dialects of the same day compare equal.

    The raw zone stores `date` as a STRING and staging may have cast it to a DATE;
    `"2026-07-01"` and `date(2026, 7, 1)` are the same day and a join that said
    otherwise would report every row of every flux as unpaired.
    """
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value).strip()


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _comparable(value: Any) -> Any:
    """The join key of one value. `None` is a value here, and it is its own."""
    if value is None:
        return None
    if _is_number(value):
        return float(value)
    return _text(value)


def _same(left: Any, right: Any) -> bool:
    """Did the mapping CHANGE this value -- decided on what the database returned.

    Numbers compare as numbers: `20.0` and `Decimal("20.000000000")` are one amount
    and two spellings, and calling that a transformation would highlight every
    staging cast in the product as a change nobody made.
    """
    if left is None or right is None:
        return left is None and right is None
    if _is_number(left) and _is_number(right):
        return float(left) == float(right)
    return _text(left) == _text(right)


def _unpaired_column(name: str, side: str, reason: str) -> dict[str, Any]:
    return {
        "name": name,
        "side": side,
        "reason": reason,
        "message": column_message(reason, name),
    }


def _refused(reason: str, **extra: Any) -> dict[str, Any]:
    """The whole shape, refusing. NEVER a missing key: a screen that cannot find
    `columns` and one that found it empty must not have to tell each other apart."""
    refusal: dict[str, Any] = {
        "available": False,
        "reason": reason,
        "message": message_for(reason),
        "columns": [],
        "key_columns": [],
        "unpaired_columns": [],
        "rows": [],
        "row_count": 0,
        "paired_row_count": 0,
        "unpaired_row_count": 0,
        "truncated": False,
        "bounded_at": None,
    }
    refusal.update(extra)
    return refusal


def _cells(
    columns: Sequence[Mapping[str, Any]],
    *,
    collected_shown: Mapping[str, Any] | None,
    mapped_shown: Mapping[str, Any] | None,
    collected_raw: Mapping[str, Any] | None,
    mapped_raw: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """One cell per paired column: the raw value, the mapped value, and whether the
    mapping changed it.

    `changed` is `None` on a row that has only one side -- "this did not change" and
    "there is nothing to compare it with" are two different sentences and only one of
    them is an observation.
    """
    cells = []
    for column in columns:
        source = column["source_field"]
        target = column["target_field"]
        changed: bool | None = None
        if collected_raw is not None and mapped_raw is not None:
            changed = not _same(collected_raw.get(source), mapped_raw.get(target))
        cells.append(
            {
                "source_field": source,
                "target_field": target,
                "is_key_column": column["is_key_column"],
                "raw": None if collected_shown is None else collected_shown.get(source),
                "mapped": None if mapped_shown is None else mapped_shown.get(target),
                "changed": changed,
            }
        )
    return cells


def pair_readings(
    *,
    collected: Mapping[str, Any] | None,
    mapped: Mapping[str, Any] | None,
    fields: Sequence[Mapping[str, Any]] | None,
    bounded_at: int | None = None,
) -> dict[str, Any]:
    """The two readings of one day, paired through the mapping's own `source -> target`.

    `fields` is the header the route already read -- `{source_field, target_field,
    is_key_column}` -- and it is the ONLY thing allowed to decide what pairs with
    what.
    """
    collected = collected or {}
    mapped = mapped or {}

    # A side that carries no rows is not a side that carries none for this day: the
    # reader answers `rows: None` when the relation refused and `rows: []` when it
    # answered and was empty. Only the first is a reason not to pair.
    if collected.get("rows") is None or mapped.get("rows") is None:
        return _refused(SIDE_UNREADABLE)

    declared = [
        field
        for field in (fields or [])
        if isinstance(field, Mapping) and field.get("source_field")
    ]
    if not declared:
        return _refused(NO_MAPPING_FIELD)

    bound = [field for field in declared if field.get("target_field")]
    if not bound:
        return _refused(NO_BOUND_FIELD, declared_field_count=len(declared))

    collected_columns = [str(name) for name in (collected.get("columns") or [])]
    mapped_columns = [str(name) for name in (mapped.get("columns") or [])]
    collected_set = set(collected_columns)
    mapped_set = set(mapped_columns)

    paired_columns: list[dict[str, Any]] = []
    unpaired: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def _note(name: str, side: str, reason: str) -> None:
        if (name, side) in seen:
            return
        seen.add((name, side))
        unpaired.append(_unpaired_column(name, side, reason))

    for field in declared:
        source = str(field["source_field"])
        target = field.get("target_field")
        if not target:
            _note(source, ZONE_COLLECTED, COLUMN_UNBOUND)
            continue
        target = str(target)
        if source not in collected_set:
            _note(source, ZONE_COLLECTED, COLUMN_SOURCE_ABSENT)
            continue
        if target not in mapped_set:
            _note(target, ZONE_MAPPED, COLUMN_TARGET_ABSENT)
            continue
        paired_columns.append(
            {
                "source_field": source,
                "target_field": target,
                "is_key_column": bool(field.get("is_key_column")),
            }
        )

    named_source = {column["source_field"] for column in paired_columns}
    named_target = {column["target_field"] for column in paired_columns}
    for name in collected_columns:
        if name not in named_source:
            _note(name, ZONE_COLLECTED, COLUMN_NAMED_BY_NO_FIELD)
    for name in mapped_columns:
        if name not in named_target:
            _note(name, ZONE_MAPPED, COLUMN_NAMED_BY_NO_FIELD)

    if not paired_columns:
        return _refused(
            NO_COLUMN_IN_BOTH_RELATIONS,
            unpaired_columns=unpaired,
            bound_field_count=len(bound),
        )

    keys = [column for column in paired_columns if column["is_key_column"]]
    if not keys:
        # THE COLUMNS PAIR AND THE ROWS CANNOT. Said with the columns that DID
        # pair, so the answer is a state of the mapping and not a silence.
        return _refused(
            NO_KEY_COLUMN, columns=paired_columns, unpaired_columns=unpaired
        )

    collected_shown = list(collected.get("rows") or [])
    mapped_shown = list(mapped.get("rows") or [])
    # The rows as the database returned them, same order and same truncation as the
    # masked ones beside them. Absent only if a caller assembled a side by hand, in
    # which case the join falls back to what it has and stays honest about it.
    collected_raw = list(collected.get(RAW_ROWS) or []) or collected_shown
    mapped_raw = list(mapped.get(RAW_ROWS) or []) or mapped_shown

    def _key(row: Mapping[str, Any], field: str) -> tuple:
        return tuple(_comparable(row.get(column[field])) for column in keys)

    collected_keys = [_key(row, "source_field") for row in collected_raw]
    mapped_keys = [_key(row, "target_field") for row in mapped_raw]
    collected_counts = Counter(collected_keys)
    mapped_counts = Counter(mapped_keys)
    # Only a key that is unique on BOTH sides can name one row. A duplicate is not
    # a pairing problem to be resolved by taking the first: it is two rows that the
    # declared grain does not tell apart, and choosing between them would be the
    # invention this module exists to refuse.
    mapped_index = {
        key: index
        for index, key in enumerate(mapped_keys)
        if mapped_counts[key] == 1
    }

    def _display_key(row: Mapping[str, Any] | None, field: str) -> list[dict[str, Any]]:
        if row is None:
            return []
        return [
            {"field": column[field], "value": row.get(column[field])}
            for column in keys
        ]

    rows: list[dict[str, Any]] = []
    matched_on_the_mapped_side: set[int] = set()
    for index, key in enumerate(collected_keys):
        shown = collected_shown[index] if index < len(collected_shown) else {}
        raw = collected_raw[index] if index < len(collected_raw) else {}
        counterpart = mapped_index.get(key) if collected_counts[key] == 1 else None
        if counterpart is None:
            reason = (
                ROW_KEY_NOT_UNIQUE
                if collected_counts[key] > 1 or mapped_counts.get(key, 0) > 1
                else ROW_NO_COUNTERPART
            )
            rows.append(
                {
                    "side": ZONE_COLLECTED,
                    "reason": reason,
                    "message": message_for(reason),
                    "key": _display_key(shown, "source_field"),
                    # WHICH ROW OF EACH SIDE THIS ROW IS -- lot B3. Every block that
                    # travels PARALLEL to a side's rows (the money provenance today,
                    # whatever a later capability adds) has to reach the same row
                    # here, and the only honest way to reach it is the index the
                    # join already knows. Matching on a value would re-do the join
                    # in a second, weaker way, and a second join is how two lines of
                    # one screen end up showing two rates for one amount.
                    "collected_index": index,
                    "mapped_index": None,
                    "cells": _cells(
                        paired_columns,
                        collected_shown=shown,
                        mapped_shown=None,
                        collected_raw=raw,
                        mapped_raw=None,
                    ),
                }
            )
            continue
        matched_on_the_mapped_side.add(counterpart)
        rows.append(
            {
                "side": None,
                "reason": None,
                "message": None,
                "key": _display_key(shown, "source_field"),
                "collected_index": index,
                "mapped_index": counterpart,
                "cells": _cells(
                    paired_columns,
                    collected_shown=shown,
                    mapped_shown=mapped_shown[counterpart]
                    if counterpart < len(mapped_shown)
                    else {},
                    collected_raw=raw,
                    mapped_raw=mapped_raw[counterpart],
                ),
            }
        )

    for index, key in enumerate(mapped_keys):
        if index in matched_on_the_mapped_side:
            continue
        shown = mapped_shown[index] if index < len(mapped_shown) else {}
        raw = mapped_raw[index] if index < len(mapped_raw) else {}
        reason = (
            ROW_KEY_NOT_UNIQUE
            if mapped_counts[key] > 1 or collected_counts.get(key, 0) > 1
            else ROW_NO_COUNTERPART
        )
        rows.append(
            {
                "side": ZONE_MAPPED,
                "reason": reason,
                "message": message_for(reason),
                "key": _display_key(shown, "target_field"),
                "collected_index": None,
                "mapped_index": index,
                "cells": _cells(
                    paired_columns,
                    collected_shown=None,
                    mapped_shown=shown,
                    collected_raw=None,
                    mapped_raw=raw,
                ),
            }
        )

    return {
        "available": True,
        "reason": None,
        "message": None,
        "columns": paired_columns,
        "key_columns": [
            {"source_field": column["source_field"], "target_field": column["target_field"]}
            for column in keys
        ],
        "unpaired_columns": unpaired,
        "rows": rows,
        "row_count": len(rows),
        "paired_row_count": sum(1 for row in rows if row["side"] is None),
        "unpaired_row_count": sum(1 for row in rows if row["side"] is not None),
        # A truncated side makes a truncated pairing: a row whose counterpart was
        # cut off at the bound reads as a row with no counterpart, and the screen
        # has to be able to say so.
        "truncated": bool(collected.get("truncated")) or bool(mapped.get("truncated")),
        "bounded_at": bounded_at,
    }
