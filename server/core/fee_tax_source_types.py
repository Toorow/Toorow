"""The E41-AD8 ``source_type`` resolution ladder (Epic 41, Story 41.2, contract C.4).

What kind of source is this datastream? The answer decides whether its spend enters the
COST cascade at all, so getting it wrong in the permissive direction would drag a
verification feed or a revenue feed into net media.

The ladder, in order:

1. **explicit declaration** -- ``app.datastream_source_types`` (migration 119, 41.1);
2. **derivation** -- the manifest ``public_catalog.category`` AND
   ``app.datastreams.data_role`` must AGREE. Two signals, both weak on their own;
3. **``UNKNOWN``** -- a TYPED unknown, excluded from the cost cascade and **never
   silently treated as ``PAID_MEDIA``**.

Why an agreement table and not a lookup on either signal alone (C.3):

* ``data_role`` has no writer. Its values are whatever migration 093's one-time regex
  guessed, and that regex misses several real DSPs;
* the manifest ``category`` is about the PRODUCT, not about what the datastream costs:
  a verification feed sits in ``paid_media`` and emits no cost metric at all.

Requiring agreement turns each signal's blind spot into an honest ``UNKNOWN`` plus a
one-click declaration, instead of a plausible-but-wrong ``PAID_MEDIA``. The known
residue is accepted and tracked (C.8/9, D8): verification feeds and the DSPs the 093
backfill regex does not match resolve to ``UNKNOWN`` by design. Widening that backfill is
Epic 42/43 fleet territory, not Epic 41's.

``LEAD_GEN_MEDIA`` and ``DIRECT_SERVICE_COST`` are DECLARATION-ONLY and are derived for
no pair at all: a production/influencer/retainer cost has no connector, and a lead-file
feed is indistinguishable from any other managed file by category alone.

AD-2 / E41-NFR05: no connector name, no provider vocabulary and no ``if module == ...``
anywhere in this module. The six ``source_type`` values are Epic 41's own vocabulary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The closed vocabulary. A ONE-FOR-ONE mirror of core.fee_tax_rules.SOURCE_TYPES and of
# the CHECK on app.datastream_source_types (migration 119). A story-local test asserts
# the two agree so they cannot drift.
# ---------------------------------------------------------------------------

PAID_MEDIA = "PAID_MEDIA"
LEAD_GEN_MEDIA = "LEAD_GEN_MEDIA"
DIRECT_SERVICE_COST = "DIRECT_SERVICE_COST"
COMMERCE_REVENUE = "COMMERCE_REVENUE"
ORGANIC_ANALYTICS = "ORGANIC_ANALYTICS"
UNKNOWN = "UNKNOWN"

ALL_SOURCE_TYPES = frozenset(
    {
        PAID_MEDIA,
        LEAD_GEN_MEDIA,
        DIRECT_SERVICE_COST,
        COMMERCE_REVENUE,
        ORGANIC_ANALYTICS,
        UNKNOWN,
    }
)

# The three types whose spend the COST cascade composes. Everything else -- revenue,
# organic analytics and the typed UNKNOWN -- is excluded.
COST_CASCADE_SOURCE_TYPES = frozenset({PAID_MEDIA, LEAD_GEN_MEDIA, DIRECT_SERVICE_COST})

# Provenance of one resolution.
RESOLVED_BY_DECLARATION = "declared"
RESOLVED_BY_DERIVATION = "derived"
RESOLVED_BY_DEFAULT = "default"

# Why a resolved value does not enter the cost cascade. Rendered as-is by 41.6/41.7, so
# every string is a sentence an operator can act on.
_EXCLUSION_REASONS: dict[str, str] = {
    UNKNOWN: (
        "source type is unknown: the declared and derived signals do not agree, so this "
        "source is excluded from the cost cascade until someone declares its type"
    ),
    COMMERCE_REVENUE: (
        "commerce revenue is not a media cost: the revenue side is composed separately"
    ),
    ORGANIC_ANALYTICS: (
        "organic analytics carries no media cost, so no cost rule applies to it"
    ),
}

# ---------------------------------------------------------------------------
# The derivation table (C.3). PURE data: a (manifest category, data_role) pair that is
# not listed here resolves to UNKNOWN. Both signals must agree; a NULL on either side is
# a disagreement, never a hint.
# ---------------------------------------------------------------------------

_AGREEMENT: dict[tuple[str, str], str] = {
    ("paid_media", "Spend"): PAID_MEDIA,
    ("paid_media", "Performance"): PAID_MEDIA,
    ("commerce_billing", "Revenue & conversions"): COMMERCE_REVENUE,
    ("analytics_product", "Context"): ORGANIC_ANALYTICS,
    ("analytics_product", "Performance"): ORGANIC_ANALYTICS,
}


class InvalidSourceType(ValueError):
    """A stored or supplied ``source_type`` is outside the closed vocabulary.

    Raised, never downgraded: silently mapping an unrecognised declaration onto
    ``PAID_MEDIA`` (or onto ``UNKNOWN``) would hide a corrupted control-plane row behind
    a plausible number.
    """


@dataclass(frozen=True, slots=True)
class SourceTypeResolution:
    """One datastream's resolved ``source_type`` plus how it was resolved."""

    value: str
    source: str
    declared_by: str | None = None
    excluded_from_cost_cascade: bool = False
    exclusion_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.value,
            "resolved_by": self.source,
            "declared_by": self.declared_by,
            "excluded_from_cost_cascade": self.excluded_from_cost_cascade,
            "exclusion_reason": self.exclusion_reason,
        }


def is_in_cost_cascade(value: object) -> bool:
    """True only for the three cost-bearing types. ``UNKNOWN`` is False, always."""
    return value in COST_CASCADE_SOURCE_TYPES


def exclusion_reason_for(value: str) -> str:
    """A renderable sentence for a value the cost cascade excludes; ``''`` otherwise."""
    if is_in_cost_cascade(value):
        return ""
    return _EXCLUSION_REASONS.get(value, _EXCLUSION_REASONS[UNKNOWN])


def _resolution(value: str, source: str, declared_by: str | None = None) -> SourceTypeResolution:
    excluded = not is_in_cost_cascade(value)
    return SourceTypeResolution(
        value=value,
        source=source,
        declared_by=declared_by,
        excluded_from_cost_cascade=excluded,
        exclusion_reason=exclusion_reason_for(value) if excluded else "",
    )


def derive_source_type(category: object, data_role: object) -> str:
    """The pure C.3 agreement table. No I/O, no DB, no filesystem, no clock.

    Both signals must agree. Anything else -- a missing manifest, a NULL ``data_role``, a
    pair that is merely plausible -- is ``UNKNOWN``. That is a TYPED unknown excluded from
    the cost cascade, not a downgrade and not a guess: ``PAID_MEDIA`` is returned for the
    two enumerated agreement pairs and for nothing else.
    """
    if not isinstance(category, str) or not isinstance(data_role, str):
        return UNKNOWN
    return _AGREEMENT.get((category.strip(), data_role.strip()), UNKNOWN)


# ---------------------------------------------------------------------------
# The DB layer. Every read is FAIL-SOFT (an unavailable store must not raise into a
# reporting path); every DECISION taken without the data is FAIL-CLOSED (a typed
# UNKNOWN, never a silent PAID_MEDIA).
# ---------------------------------------------------------------------------

_DECLARATION_SQL = """
    SELECT source_type, declared_by
    FROM app.datastream_source_types
    WHERE datastream_id = %s
"""

_DATASTREAM_SIGNALS_SQL = """
    SELECT module_name, data_role
    FROM app.datastreams
    WHERE id = %s AND project_id = %s
"""


def fetch_declared_source_type(conn: object, datastream_id: str) -> tuple[str, str | None] | None:
    """Read the explicit declaration, or ``None`` when there is none / it is unreadable.

    Unreadable (migration 119 not applied on this database) is deliberately reported the
    same as absent: the ladder simply continues to derivation. Raising here would take a
    reporting read down over a control-plane table that may legitimately not exist yet.
    """
    try:
        with conn.cursor() as cur:  # type: ignore[attr-defined]
            cur.execute(_DECLARATION_SQL, (datastream_id,))
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "fee_tax_source_types: declaration unreadable datastream=%s: %s", datastream_id, exc
        )
        return None
    if not row or not row[0]:
        return None
    return str(row[0]), (str(row[1]) if row[1] else None)


def fetch_derivation_signals(
    conn: object,
    *,
    datastream_id: str,
    project_id: str,
    modules_dir: object = None,
) -> tuple[str | None, str | None]:
    """Return ``(manifest category, data_role)``. Either may be ``None``.

    ``None`` on either side is a DISAGREEMENT for ``derive_source_type``, never a hint:
    an unreadable manifest resolves to ``UNKNOWN``, it does not fall back to the other
    signal alone.
    """
    from core.context_seed import load_registry_entry  # noqa: PLC0415

    try:
        with conn.cursor() as cur:  # type: ignore[attr-defined]
            cur.execute(_DATASTREAM_SIGNALS_SQL, (datastream_id, project_id))
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "fee_tax_source_types: datastream signals unreadable datastream=%s: %s",
            datastream_id,
            exc,
        )
        return None, None
    if not row:
        return None, None

    module_name = str(row[0]) if row[0] else ""
    data_role = str(row[1]) if row[1] else None
    if not module_name:
        # managed_feed / external_bq datastreams carry no module: there is no manifest to
        # read, so the category signal is genuinely absent (migration 030).
        return None, data_role

    try:
        entry = load_registry_entry(module_name, modules_dir=modules_dir)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "fee_tax_source_types: registry entry unreadable module=%s: %s", module_name, exc
        )
        return None, data_role
    if not entry:
        return None, data_role

    manifest = entry.get("manifest") or {}
    public_catalog = manifest.get("public_catalog") or {}
    category = public_catalog.get("category")
    return (str(category) if isinstance(category, str) and category else None), data_role


def resolve_source_type(
    datastream_id: str,
    project_id: str,
    conn: object,
    *,
    modules_dir: object = None,
) -> SourceTypeResolution:
    """Run the C.4 ladder for one datastream: declaration -> derivation -> ``UNKNOWN``.

    Raises ``InvalidSourceType`` when a STORED declaration is outside the six-value
    vocabulary -- a corrupted control-plane row is an error, not something to round down
    to a plausible type.
    """
    declared = fetch_declared_source_type(conn, datastream_id)
    if declared is not None:
        value, declared_by = declared
        if value not in ALL_SOURCE_TYPES:
            raise InvalidSourceType(
                f"declared source_type is outside the closed vocabulary: {value!r}"
            )
        return _resolution(value, RESOLVED_BY_DECLARATION, declared_by)

    category, data_role = fetch_derivation_signals(
        conn, datastream_id=datastream_id, project_id=project_id, modules_dir=modules_dir
    )
    derived = derive_source_type(category, data_role)
    if derived != UNKNOWN:
        return _resolution(derived, RESOLVED_BY_DERIVATION)
    return _resolution(UNKNOWN, RESOLVED_BY_DEFAULT)
