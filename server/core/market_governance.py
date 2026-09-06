"""Used-by guard on project markets (Story 37.9).

A market is project master data, and budgets, objectives and saved reports bind
to its **stable id** (Story 37.8's ``resolve_market_binding``).  Two changes are
therefore not free:

* **deleting** a market orphans everything bound to it;
* **moving a country out of** a market silently changes what an already-published
  figure meant, while the binding still resolves and looks healthy.

Relabelling is explicitly NOT one of them: binding on the id is precisely what
makes a relabel safe, and the guard must not pretend otherwise.

The guard's job is to **say what depends on the market before the change is
accepted**, inside the governed preview -> confirm lifecycle that already exists
(``core.country_workspace_commands`` and ``core.governance_surface_api``, which
replaced the retired ``core.geographic_change``), with the market diff recorded in
the append-only audit. It never decides for the operator: an acknowledged impact proceeds, an
unacknowledged one is refused with the dependent list attached.

AD-2: no binding kind is enumerated here. The registry (``app.market_bindings``,
migration 104) stores whatever the binding surface declared.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from core.geographic_reporting import GeographicPosture, market_diff

logger = logging.getLogger(__name__)

# Why a bound market is impacted. Ordered from most to least destructive.
IMPACT_MARKET_REMOVED = "market_removed"
IMPACT_COUNTRY_REMOVED = "country_removed_from_market"
IMPACT_COUNTRY_ADDED = "country_added_to_market"
IMPACT_MARKET_RELABELLED = "market_relabelled"

# Impacts that change the MEANING of already-published figures. A relabel does
# not: the binding is on the id.
BLOCKING_IMPACTS = frozenset({IMPACT_MARKET_REMOVED, IMPACT_COUNTRY_REMOVED, IMPACT_COUNTRY_ADDED})


class MarketUsageBlocked(RuntimeError):
    """A market change would silently re-mean bound, already-published figures."""

    code = "market_usage_blocked"

    def __init__(self, message: str, impacts: Sequence["MarketImpact"] = ()) -> None:
        super().__init__(message)
        self.impacts = tuple(impacts)


class MarketUsageUnavailable(RuntimeError):
    """The used-by evidence cannot be read, so no change may be authorized.

    Distinct from :class:`MarketUsageBlocked` on purpose: that one means "we
    looked and found dependents"; this one means "we could not look". Collapsing
    them would let a caller report a confident empty dependency list built from
    an outage -- the exact defect Story 48.2 removes.
    """

    code = "market_usage_evidence_unavailable"


@dataclass(frozen=True, slots=True)
class MarketBinding:
    """One declared dependency on a market id."""

    market_id: str
    binding_kind: str
    binding_id: str
    binding_label: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "market_id": self.market_id,
            "binding_kind": self.binding_kind,
            "binding_id": self.binding_id,
            "binding_label": self.binding_label,
        }


@dataclass(frozen=True, slots=True)
class MarketImpact:
    """What a proposed change does to one bound market."""

    market_id: str
    reason: str
    detail: dict[str, object]
    bindings: tuple[MarketBinding, ...]

    @property
    def blocking(self) -> bool:
        return self.reason in BLOCKING_IMPACTS and bool(self.bindings)

    def as_dict(self) -> dict[str, object]:
        return {
            "market_id": self.market_id,
            "reason": self.reason,
            "detail": self.detail,
            "blocking": self.blocking,
            "bindings": [binding.as_dict() for binding in self.bindings],
            "binding_count": len(self.bindings),
        }


# ---------------------------------------------------------------------------
# PURE assessment (offline-testable; the DB only supplies the binding rows).
# ---------------------------------------------------------------------------


def index_bindings(
    bindings: Iterable[MarketBinding | Mapping[str, object]],
) -> dict[str, tuple[MarketBinding, ...]]:
    """Group declared bindings by market id (PURE). Ids compare case-insensitively."""

    grouped: dict[str, list[MarketBinding]] = {}
    for raw in bindings:
        if isinstance(raw, MarketBinding):
            binding = raw
        elif isinstance(raw, Mapping):
            market_id = str(raw.get("market_id") or "").strip()
            binding_kind = str(raw.get("binding_kind") or "").strip()
            binding_id = str(raw.get("binding_id") or "").strip()
            if not market_id or not binding_kind or not binding_id:
                continue
            label = raw.get("binding_label")
            binding = MarketBinding(
                market_id=market_id,
                binding_kind=binding_kind,
                binding_id=binding_id,
                binding_label=str(label) if isinstance(label, str) and label else None,
            )
        else:
            continue
        grouped.setdefault(binding.market_id.lower(), []).append(binding)
    return {
        key: tuple(sorted(items, key=lambda b: (b.binding_kind, b.binding_id)))
        for key, items in grouped.items()
    }


def assess_market_change(
    previous: GeographicPosture,
    target: GeographicPosture,
    bindings: Iterable[MarketBinding | Mapping[str, object]] = (),
) -> tuple[MarketImpact, ...]:
    """Say what the change does to each BOUND market (PURE, no I/O).

    Built on Story 37.8's ``market_diff`` rather than re-deriving the diff, so
    the guard and the audited diff can never disagree.  Markets nobody binds
    produce no impact: the guard reports dependencies, it does not police
    composition.
    """

    index = index_bindings(bindings)
    if not index:
        return ()
    diff = market_diff(previous, target)
    impacts: list[MarketImpact] = []

    for removed in diff.get("removed_markets", []) or []:
        market_id = str(removed.get("id") or "")
        bound = index.get(market_id.lower(), ())
        if not bound:
            continue
        impacts.append(
            MarketImpact(
                market_id=market_id,
                reason=IMPACT_MARKET_REMOVED,
                detail={
                    "label": removed.get("label"),
                    "country_codes": removed.get("country_codes", []),
                },
                bindings=bound,
            )
        )

    for recomposed in diff.get("recomposed_markets", []) or []:
        market_id = str(recomposed.get("id") or "")
        bound = index.get(market_id.lower(), ())
        if not bound:
            continue
        removed_codes = list(recomposed.get("removed_country_codes", []) or [])
        added_codes = list(recomposed.get("added_country_codes", []) or [])
        # A removal is reported first: it is the case where a published figure
        # loses volume it used to contain.
        if removed_codes:
            impacts.append(
                MarketImpact(
                    market_id=market_id,
                    reason=IMPACT_COUNTRY_REMOVED,
                    detail={
                        "label": recomposed.get("label"),
                        "removed_country_codes": removed_codes,
                    },
                    bindings=bound,
                )
            )
        if added_codes:
            impacts.append(
                MarketImpact(
                    market_id=market_id,
                    reason=IMPACT_COUNTRY_ADDED,
                    detail={
                        "label": recomposed.get("label"),
                        "added_country_codes": added_codes,
                    },
                    bindings=bound,
                )
            )

    for relabelled in diff.get("relabelled_markets", []) or []:
        market_id = str(relabelled.get("id") or "")
        bound = index.get(market_id.lower(), ())
        if not bound:
            continue
        impacts.append(
            MarketImpact(
                market_id=market_id,
                reason=IMPACT_MARKET_RELABELLED,
                detail={
                    "previous_label": relabelled.get("previous_label"),
                    "new_label": relabelled.get("new_label"),
                },
                bindings=bound,
            )
        )

    return tuple(impacts)


def usage_report(impacts: Sequence[MarketImpact]) -> dict[str, object]:
    """Render the guard's answer for a preview payload (PURE)."""

    blocking = [impact for impact in impacts if impact.blocking]
    return {
        "impacts": [impact.as_dict() for impact in impacts],
        "impact_count": len(impacts),
        "blocking_impact_count": len(blocking),
        "acknowledgement_required": bool(blocking),
        "bound_market_ids": sorted({impact.market_id for impact in impacts}),
        "statement": (
            "Markets below are bound by budgets, objectives or saved reports. "
            "Removing one, or moving a country out of one, changes what already "
            "published figures mean; acknowledge the impact to proceed."
            if blocking
            else "No bound market changes meaning."
        ),
    }


def assert_market_change_acknowledged(
    impacts: Sequence[MarketImpact], acknowledged: bool
) -> None:
    """Refuse a meaning-changing regroup that the operator has not acknowledged."""

    blocking = [impact for impact in impacts if impact.blocking]
    if blocking and not acknowledged:
        described = ", ".join(
            f"{impact.market_id} ({impact.reason}, {len(impact.bindings)} binding(s))"
            for impact in blocking
        )
        raise MarketUsageBlocked(
            "market change refused: dependents must be acknowledged first -- " + described,
            blocking,
        )


# ---------------------------------------------------------------------------
# Registry I/O (app.market_bindings, migration 104).
# ---------------------------------------------------------------------------


def fetch_market_bindings(project_id: str, conn: object) -> tuple[MarketBinding, ...]:
    """Read what depends on this Project's markets, or refuse to answer.

    Story 48.2 (AC8) reversed this function's failure mode, and the reversal is
    the point rather than a detail. It used to swallow the exception and return
    ``()``, documented as "fail-soft on the READ only". But its one caller is a
    guard that asks *is anything depending on this market?* before a destructive
    change -- so an empty tuple from an outage is indistinguishable from a
    genuine "nothing depends on it", and the guard waves the change through.
    A used-by check that cannot read its store has not found zero dependents;
    it has failed, and it now says so.

    Reads the generic Master Data used-by contract, which replaces the mutable
    ``app.market_bindings`` registry as the authority.
    """

    from core.master_data import (  # noqa: PLC0415
        MasterDataUnavailable,
        fetch_registry,
        fetch_used_by,
    )

    try:
        registry = fetch_registry(conn, project_id=project_id, object_kind="country")
    except Exception as exc:  # noqa: BLE001
        raise MarketUsageUnavailable(
            "the Country registry is unreadable; the used-by guard cannot decide"
        ) from exc
    if registry is None:
        # No registry is a real, readable answer: this Project governs no
        # markets, so nothing can be bound to one.
        return ()
    try:
        references = fetch_used_by(conn, project_id=project_id, registry_id=registry["id"])
    except MasterDataUnavailable as exc:
        logger.warning(
            "market_governance: used-by store unreadable project=%s: %s", project_id, exc
        )
        raise MarketUsageUnavailable(
            "the used-by store is unreadable; the change is blocked rather than allowed"
        ) from exc
    return tuple(
        MarketBinding(
            market_id=reference.node_id,
            binding_kind=reference.consumer_kind,
            binding_id=reference.consumer_id,
            binding_label=reference.consumer_label,
        )
        for reference in references
    )


def register_market_binding(
    *,
    project_id: str,
    market_id: str,
    binding_kind: str,
    binding_id: str,
    binding_label: str | None,
    conn: object,
    identity: str = "system",
) -> None:
    """Declare that something binds a market id (idempotent, caller-owned txn).

    ``binding_kind`` is caller data (AD-2): budgets, objectives and saved reports
    each declare their own, and the platform enumerates none.

    Story 48.2: this writes the generic Master Data used-by contract. There is
    one writer and one store; ``app.market_bindings`` is no longer either.
    """

    from core.master_data import (  # noqa: PLC0415
        UsedByReference,
        register_used_by,
        require_registry,
    )

    registry = require_registry(conn, project_id=project_id, object_kind="country")
    register_used_by(
        conn,
        project_id=project_id,
        registry_id=registry["id"],
        reference=UsedByReference(
            node_id=market_id,
            consumer_kind=binding_kind,
            consumer_id=binding_id,
            consumer_label=binding_label,
            hierarchy_version_id=registry["current_version_id"],
        ),
        actor=identity,
    )


def unregister_market_binding(
    *,
    project_id: str,
    market_id: str,
    binding_kind: str,
    binding_id: str,
    conn: object,
) -> bool:
    """Release a declared binding (caller-owned transaction).

    Story 48.2: released rather than deleted. A published hierarchy version
    froze its used-by snapshot, and a row that vanishes makes that snapshot
    unexplainable; ``released_at`` keeps the history readable.
    """

    from core.master_data import release_used_by  # noqa: PLC0415

    release_used_by(
        conn,
        project_id=project_id,
        node_id=market_id,
        consumer_kind=binding_kind,
        consumer_id=binding_id,
    )
    return True


def collect_market_usage(
    project_id: str,
    previous: GeographicPosture,
    target: GeographicPosture,
    conn: object,
) -> tuple[tuple[MarketImpact, ...], dict[str, object]]:
    """Read the registry and assess the change. Returns (impacts, report)."""

    bindings = fetch_market_bindings(project_id, conn)
    impacts = assess_market_change(previous, target, bindings)
    return impacts, usage_report(impacts)
