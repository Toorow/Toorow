"""What a client object may carry, and whether a node honours it (Story 64.2).

Story 64.13 established that ``version_scope='node'`` gives every identity its own
``payload`` -- the owner's "a matching key and a JSON of its properties". This
module is the half that makes those properties USABLE rather than merely stored:
a declared contract per object kind, and the refusal of a node that contradicts
it.

THE VALUE TYPES ARE THE SEMANTIC LAYER'S, VERBATIM. ``VALUE_TYPES`` is imported
from :mod:`core.semantic_expressions` rather than restated. An attribute declared
here can therefore become a Concept without translation -- the road to `age_days`
(Story 64.11) -- and a second vocabulary would have needed a mapping between two
lists meaning the same thing, which is the defect this epic has already removed
twice (the landing enum, the fan-out policy).

AMENDE LE 2026-08-08 (story 64.14, arbitrage de Jean). Le CONTRAT n est plus un
magasin : `master_data_registries.attribute_contract` a ete retire par la
migration 241. Les attributs d un objet SONT les champs canoniques qui le nomment
(`mdm_canonical_fields.object_kind`), donc `validate_attribute_contract` -- qui
validait la liste JSONB -- n a plus d objet et est parti avec elle. Ce qui reste
ici est la seule moitie qui n a jamais eu de doublon : verifier qu un NOEUD
respecte le contrat, quelle que soit la source qui le porte.

WHAT THIS MODULE DELIBERATELY DOES NOT DO. It never coerces. A value the contract
says is a number and the payload spells as a string is REFUSED, not parsed: the
warehouse view surfaces the same disagreement as ``type_mismatch`` for rows that
were written before a contract tightened, and a silent coercion here would make
the two halves disagree about the same row.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from core.master_data import MasterDataError

#: The types a numeric value may be declared as. Kept beside the view's own list
#: (migration 239) because the two must agree; the conformance test compares them.
NUMERIC_TYPES = frozenset({"integer", "decimal", "money", "ratio", "percent", "duration"})
TEMPORAL_TYPES = frozenset({"date", "timestamp"})



def _matches(value: Any, value_type: str) -> bool:
    """Whether a JSON value can be held by the declared type. No coercion."""

    if value is None:
        return True  # An absent property is absent, not wrong.
    if value_type in NUMERIC_TYPES:
        # `bool` is an int in Python and would sail through a numeric check.
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if value_type in TEMPORAL_TYPES:
        return isinstance(value, str)
    if value_type == "boolean":
        return isinstance(value, bool)
    return isinstance(value, str)


def validate_node_attributes(
    attributes: Mapping[str, Any] | None,
    contract: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return the attributes a node may publish, or refuse with the reason.

    Two refusals, and the first is the one that keeps a contract worth having:

    * an UNDECLARED attribute. Accepting it would make the contract advisory, and
      the warehouse view would carry a column no Concept can ever describe. The
      repair is one declaration, and the message names it.
    * a value the declared type cannot hold. Never coerced: the view reports the
      same disagreement as ``type_mismatch`` for rows written before a contract
      tightened, and coercing here would make the two halves disagree about one
      row -- a number that is a string in Postgres and a number in the warehouse.
    """

    declared = {entry["name"]: entry["value_type"] for entry in contract}
    values = dict(attributes or {})

    unknown = sorted(set(values) - set(declared))
    if unknown:
        raise MasterDataError(
            f"attributes not declared by this object kind: {', '.join(unknown)} -- "
            "declare them in the contract, or remove them from the payload"
        )

    wrong = [
        f"{name} (expected {declared[name]})"
        for name, value in values.items()
        if not _matches(value, declared[name])
    ]
    if wrong:
        raise MasterDataError(
            "values the declared type cannot hold: " + ", ".join(sorted(wrong))
        )

    return values
