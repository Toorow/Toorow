"""The values a mapped Datastream carries that the reading cannot name.

Target: ``docs/product-architecture/unresolved-values.md``.

THE CASE THAT PAID FOR THIS MODULE, measured on the deployment on 2026-08-12.
A YouTube channel's performance flux published 16 130 rows over 2026-07-13 →
2026-08-09 carrying **519 distinct video ids**. The flux that names those videos
published **3 rows** -- the three uploads that happened inside the window. So 516
videos had views, watch time and comments, and no name anywhere in the product.
Nothing measured that, nothing listed it, and no screen offered the gesture that
closes it.

THREE REASONS, AND THEY NEVER MERGE INTO ONE COUNT. They look identical on a
screen -- a value with nothing behind it -- and they are repaired in three
different places:

  * ``absent_at_source``  the field arrived empty. NO pair repairs this: the fix
                          is the collection or the field mapping. It is also the
                          reason a naive reading gets wrong, because the emptiness
                          is spelled ``''`` at least as often as ``NULL``.
  * ``unmapped``          the value is there and nothing gives it a canonical
                          name. This is the one a value mapping table closes.
  * ``no_reference``      the value is named and points at a set that does not
                          contain it -- a plan line, a catalogue, an entity. The
                          516 videos are here.

WHAT THIS MODULE REFUSES TO DO. It never invents a canonical value; it never
answers `0` for something it could not read (an unreadable relation is
``unavailable``, and a caller that turned that into an empty list would publish a
green nobody measured); it never carries the rows, only the values and their
weight; and it holds NO connector vocabulary -- the dimension, the resolver and
the reference set all arrive from the caller (AD-2).

THE RANKING IS THE PRODUCT DECISION. Adverity and Funnel both list unmapped
values alphabetically, so the value carrying 40 % of the month sits between two
values carrying three rows. Here the order is weight, so the first line of the
list is the one worth repairing.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

#: The value arrived empty: NULL, or a text form that trims to nothing, or one of
#: the absence sentinels the CALLER declares (the warehouse writes
#: ``__country_absent__`` for one axis; this module knows no such word of its own).
REASON_ABSENT_AT_SOURCE = "absent_at_source"

#: The value is present and no confirmed mapping gives it a canonical name.
REASON_UNMAPPED = "unmapped"

#: The value is present and the reference set consulted does not hold it.
REASON_NO_REFERENCE = "no_reference"

#: Always all three, always in this order. A caller that iterated a dict of only
#: the non-zero reasons would print two chips where three were measured, and
#: "zero of this kind" is a result.
REASONS: tuple[str, ...] = (
    REASON_ABSENT_AT_SOURCE,
    REASON_UNMAPPED,
    REASON_NO_REFERENCE,
)

#: The gesture each reason is repaired by. The screen renders the sentence; the
#: server owns it, so both surfaces say the same thing.
REPAIR = {
    REASON_ABSENT_AT_SOURCE: (
        "This column arrived with no value, so no mapping would fill it. "
        "Repair the collection or the field mapping."
    ),
    REASON_UNMAPPED: (
        "This value has no canonical name yet. Add the pair in the value mapping "
        "table assigned to this field."
    ),
    REASON_NO_REFERENCE: (
        "This value is not in the set that names it. Add it to that set, or "
        "attach it to an entry that already exists."
    ),
}

#: The header a two-column extract carries -- the exact pair
#: ``value_mapping_tables.parse_pairs`` recognises and skips, so a file taken out
#: of this module goes back in through the import route without being re-keyed.
PAIR_HEADER = ("source_value", "canonical_value")


@dataclass(frozen=True, slots=True)
class UnresolvedValue:
    """One value a reading could not name, with what makes it actionable."""

    dimension: str
    source_value: str
    connector: str
    occurrences: int
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "source_value": self.source_value,
            "connector": self.connector,
            "occurrences": self.occurrences,
            "reason": self.reason,
            "repair": REPAIR.get(self.reason),
        }


def is_absent(value: Any, absence_sentinels: Iterable[str] = ()) -> bool:
    """PURE: did this value arrive empty?

    NULL is not the only spelling, and the deployment proved it rather than a
    review guessing it: a live grain column held ``''`` on 602 rows out of 602
    with not one NULL among them.
    """
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    return text in {str(sentinel) for sentinel in absence_sentinels}


def classify(
    value: Any,
    *,
    resolved: bool,
    in_reference: bool | None = None,
    absence_sentinels: Iterable[str] = (),
) -> str | None:
    """PURE: the reason this value is unresolved, or ``None`` when it is fine.

    ORDER IS THE ARBITRAGE, and none of the four steps is cosmetic.

    1. Absence first, because an empty string can be "mapped" by a careless pair
       and would then read as resolved forever.
    2. A confirmed pair wins next: it is the person's own explicit answer about
       this value, and a set that does not hold it no longer makes it nameless.
    3. Then the reference. ``True`` means the set names it -- nothing is missing,
       which is the 3 videos of 519 that had a title.
    4. ``False`` means the set was consulted and does not hold it: the 516 others.
       ``None`` means no set was consulted at all -- silence, not a finding -- and
       the gap falls back to ``unmapped``, which a pair would close.
    """
    if is_absent(value, absence_sentinels):
        return REASON_ABSENT_AT_SOURCE
    if resolved:
        return None
    if in_reference is True:
        return None
    if in_reference is False:
        return REASON_NO_REFERENCE
    return REASON_UNMAPPED


def aggregate(
    rows: Sequence[Mapping[str, Any]],
    *,
    dimension: str,
    resolver: Callable[[Any, str], Any] | None = None,
    reference: Callable[[Any, str], bool | None] | None = None,
    absence_sentinels: Iterable[str] = (),
    connector: str = "",
) -> list[UnresolvedValue]:
    """PURE: turn observed ``{value, row_count}`` rows into unresolved values.

    ``rows`` is exactly what ``collected_mapped_reader.read_distinct_values``
    answers, already ordered heaviest first; the order is preserved so the caller
    never re-sorts and never loses the ranking the reading paid for.

    ``resolver`` and ``reference`` are supplied, never built here: this module
    holds no store and no vocabulary. A missing resolver means nothing is
    resolved; a missing reference means none was consulted.
    """
    unresolved: list[UnresolvedValue] = []
    for row in rows:
        value = row.get("value")
        row_connector = str(row.get("connector") or connector or "")
        resolved = False
        if resolver is not None and not is_absent(value, absence_sentinels):
            try:
                resolved = resolver(value, row_connector) is not None
            except Exception:  # noqa: BLE001 -- a broken resolver resolves nothing
                logger.warning("unresolved_values: resolver failed on one value")
                resolved = False
        in_reference: bool | None = None
        if reference is not None and not is_absent(value, absence_sentinels):
            try:
                in_reference = reference(value, row_connector)
            except Exception:  # noqa: BLE001 -- and neither does a broken reference
                logger.warning("unresolved_values: reference failed on one value")
                in_reference = None
        reason = classify(
            value,
            resolved=resolved,
            in_reference=in_reference,
            absence_sentinels=absence_sentinels,
        )
        if reason is None:
            continue
        unresolved.append(
            UnresolvedValue(
                dimension=dimension,
                source_value="" if value is None else str(value),
                connector=row_connector,
                occurrences=int(row.get("row_count") or 0),
                reason=reason,
            )
        )
    return unresolved


def summarize(
    values: Sequence[UnresolvedValue],
    *,
    observed_distinct: int | None = None,
    complete: bool = True,
) -> dict[str, Any]:
    """PURE: the counts a header prints -- three reasons, never folded into one.

    ``observed_distinct`` is what the reading saw in total. It travels so the
    header can say "198 of 519" instead of a bare number, and it stays ``None``
    when it was not measured rather than becoming a zero.

    ``complete`` IS NOT DECORATION. The listing is bounded, so on the deployment's
    own case -- 519 distinct videos, 3 named -- a page of 200 counts 198 while the
    true number is 516. A summary that published `198` without saying it was
    counting a page would be a silent cap, which reads as "we looked at
    everything". The caller says which of the two it is holding.
    """
    by_reason = {reason: 0 for reason in REASONS}
    occurrences = {reason: 0 for reason in REASONS}
    for item in values:
        if item.reason not in by_reason:
            continue
        by_reason[item.reason] += 1
        occurrences[item.reason] += int(item.occurrences or 0)
    return {
        "unresolved": len(values),
        "observed_distinct": observed_distinct,
        "complete": bool(complete),
        "by_reason": by_reason,
        "occurrences_by_reason": occurrences,
    }


def repairable(values: Sequence[UnresolvedValue]) -> list[UnresolvedValue]:
    """PURE: the values a PAIR would repair -- `unmapped`, and nothing else.

    An `absent_at_source` row in an extract would ask somebody to give a name to
    nothing, and the file would come back with a pair mapping `''` to a value,
    which is the fabrication this whole page exists to prevent.
    """
    return [item for item in values if item.reason == REASON_UNMAPPED]


def build_extract(
    values: Sequence[UnresolvedValue],
    *,
    headers: Sequence[str] = PAIR_HEADER,
    key_column: str | None = None,
) -> str:
    """PURE: the CSV a person fills, IN THE SHAPE OF THE DESTINATION.

    The default is the two columns the value-mapping importer accepts, so the file
    round-trips with no re-keying. Any other destination -- a client mapping file
    behind a Template -- passes its OWN header row and the column its key lives
    in; every other column is written empty for the person to fill. Handing a
    nine-column file back as two columns is how a mapping file acquires its next
    gap.

    Only `unmapped` values are written (see :func:`repairable`).
    """
    columns = [str(column) for column in headers] or list(PAIR_HEADER)
    key = str(key_column or columns[0])
    if key not in columns:
        raise ValueError(f"the key column {key!r} is not one of the headers {columns!r}")
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for item in repairable(values):
        writer.writerow(
            [item.source_value if column == key else "" for column in columns]
        )
    return buffer.getvalue()


def read_unresolved_set(
    *,
    project_id: str,
    relation: str,
    zone: str,
    dimension: str,
    start: str,
    end: str,
    classifications: Mapping[str, str] | None = None,
    resolver: Callable[[Any, str], Any] | None = None,
    reference: Callable[[Any, str], bool | None] | None = None,
    absence_sentinels: Iterable[str] = (),
    connector: str = "",
    limit: int | None = None,
) -> dict[str, Any]:
    """The unresolved set of ONE dimension of ONE relation over a window.

    Answers a payload that always says WHICH of the three it is:

      * ``state='measured'`` with the values, their reasons and the summary;
      * ``state='unavailable'`` with the reason the reading could not be taken --
        never an empty list, because "we could not look" and "there is nothing"
        are two reports and a screen must not be handed one for the other;
      * ``state='not_listable'`` when the column is classified: the counts are
        real, the values stay in the warehouse.
    """
    from core import collected_mapped_reader  # noqa: PLC0415

    described = collected_mapped_reader.describe_relation(
        project_id=project_id, relation=relation, zone=zone
    )
    read = collected_mapped_reader.read_distinct_values(
        description=described,
        project_id=project_id,
        start=start,
        end=end,
        field=dimension,
        classifications=classifications,
        limit=limit or collected_mapped_reader.MAX_VALUES,
    )
    base = {
        "dimension": dimension,
        "relation": read.get("relation") or relation,
        "zone": zone,
        "window": {"start": start, "end": end},
    }
    reason = read.get("reason")
    if reason == collected_mapped_reader.FIELD_MASKED:
        return {
            **base,
            "state": "not_listable",
            "reason": reason,
            "message": collected_mapped_reader.message_for(reason),
            "values": [],
            "summary": summarize([], observed_distinct=None),
            "truncated": False,
        }
    if reason or not read.get("readable"):
        return {
            **base,
            "state": "unavailable",
            "reason": reason or collected_mapped_reader.WAREHOUSE_UNAVAILABLE,
            "message": collected_mapped_reader.message_for(
                reason or collected_mapped_reader.WAREHOUSE_UNAVAILABLE
            ),
            "values": [],
            "summary": summarize([], observed_distinct=None),
            "truncated": False,
        }

    values = aggregate(
        read.get("values") or [],
        dimension=dimension,
        resolver=resolver,
        reference=reference,
        absence_sentinels=absence_sentinels,
        connector=connector,
    )
    return {
        **base,
        "state": "measured",
        "reason": None,
        "message": None,
        "values": [item.as_dict() for item in values],
        "summary": summarize(
            values,
            observed_distinct=read.get("distinct_count"),
            complete=not read.get("truncated"),
        ),
        # Stated, never silent: a listing that stopped at its bound and said
        # nothing would read as "this is everything there is".
        "truncated": bool(read.get("truncated")),
        "window_rows": read.get("row_count"),
    }
