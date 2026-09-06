"""Epic 37 country/market bridge for the fee & tax cascade (Epic 41, Story 41.2).

THIS MODULE HAS NO PRODUCTION CALLER, AND THAT IS ITS JOB -- DO NOT "CLEAN IT UP".
Measured 2026-08-17: zero imports outside tests. Read alone, that number says dead
code, and the audit of the same day listed it beside three modules that really
were. It is not one of them. Production resolves this question in SQL
(``dbt/models/marts/fee_tax_country_resolution.sql``); this module is the ORACLE
that SQL is checked against.

``server/tests/integration/test_fee_tax_bridge_engine_agreement.py`` compiles the
real dbt model, executes it verbatim against DuckDB over the same five mirror
relations production mirrors, feeds one scenario matrix to BOTH engines and
demands either the same five answers or a DECLARED divergence -- and it fails a
declared divergence that has stopped happening, so the list cannot rot into
fiction. Deleting this file would not remove a duplicate: it would remove the only
executable check on the production tax cascade, and the SQL would be left agreeing
with itself. It is also what the audit's own P2-9 repair asks for, already built.

The rule that follows: a change to the ladder below is a change to a SPECIFICATION.
It lands here and in the SQL model together, or the agreement test says so.

This module is a BRIDGE, never a capture. Epic 41 owns no country column, pulls no
country from any connector, emits no ``breakdown_dimension`` and writes no fact row.
It answers exactly one question per spend row: *which country, and which market, is
this row in -- and how do we know?*

The ladder (B1 + C5), applied in order and never re-ordered:

1. ``row_dimension``     -- the row's own ``breakdown_dimension = 'country'`` value,
                            canonicalised through the SHARED vocabulary / the 37.9 MDM
                            resolver. Real data always outranks any inference.
2. ``declared_binding``  -- the datastream's DECLARED Epic 37 market binding
                            (``app.market_bindings``, ``binding_kind='datastream'``).
3. ``project_posture``   -- a DECLARED INFERENCE: when the project's PUBLISHED Country
                            hierarchy tracks EXACTLY ONE country, every row of that
                            project is attributed to it. Reached only from a SILENCE
                            (nothing declared, or the registry was unreadable) -- never
                            after a CONTRADICTION (an ambiguous binding, or a binding to
                            the governed catch-all). An inference may fill a silence; it
                            may never overrule a contradiction.
4. a typed GAP           -- ``country_code`` is ``None``, ``resolution_source`` is ``''``
                            and the composed total is flagged incomplete.

The distinction the whole epic turns on, restated in every docstring below so nobody
"simplifies" the branches into one:

* a condition that is **KNOWN FALSE** yields ``NO_MATCH`` -> the rule contributes
  **+0 micros** and the cascade continues (E41-AD9);
* an attribute that is **UNRESOLVABLE** yields ``UNRESOLVED`` -> the rule does NOT fire,
  a typed gap is emitted, the composed total is flagged **incomplete**, and **no zero is
  fabricated** (E41-NFR02 / AD-9).

Production reality today, and the reason rung 3 exists (A.5 / A.6 of the story):
no paid-media connector emits a ``country`` row, so rung 1 never fires there; migration
104 is NOT applied and nothing writes a datastream binding, so rung 2 resolves nothing.
A **single-country** project therefore resolves on rung 3 -- today, with no migration --
and a **multi-country** project degrades to a **gap**. Both are the specified behaviour.
We never guess between two countries.

STORY 37.9 -- WHERE THE GEOGRAPHY COMES FROM, AND WHY IT MOVED. Until 2026-08-17 rungs
2 and 3 read a ``GeographicPosture`` built from ``app.project_preferences``
(``geographic_mode`` / ``local_markets``). Those are the columns ``country_registry.py``
says it "replaces outright", and the ratified Country capability confirmation
(``project_settings`` -> ``country_activation``) derives a posture IN MEMORY to compile
plans and writes NOTHING back to them -- while Analyze
(``reports._load_geography_projection``) and the Tax MCP proposal path
(``capability_compilers._tax_fee_preset_proposals``) both read the PUBLISHED hierarchy
version through ``country_registry.load_projection``.

So Tax was the second geographic mapping ``capabilities/country.md`` forbids, and the
failure was SILENT: a Project governed through the ratified capability presented an
EMPTY posture, every spend row came back "posture global", and the composed total was
reported incomplete while its geography was fully published. Both engines now read the
published projection -- this module through :class:`GovernedGeography`, the SQL twin
through ``mirror.country_market_projection`` (migration 269).

NO PUBLISHED MEANING IS NOT "GLOBAL". ``load_projection`` returns ``None`` for a Project
that has published no Country version and its docstring requires every caller to treat
that as "cannot decide". :meth:`GovernedGeography.absent` is that state here, and it
reaches its own rung-4 reason rather than borrowing the one that used to name a posture
nobody can set any more.

AD-2 / E41-NFR05: this module names no connector, no provider and no ISO code. The legal
country set and every alias come from the governed seed at runtime.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from core.country_vocabulary import normalize_country_value
from core.geographic_semantics import COUNTRY_ABSENT_BUCKET_ID, COUNTRY_PARTITION

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps this module DB-free
    from core.country_registry import GeographyProjection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Gap codes. COUNTRY_UNRESOLVED is the code 41.3 already expects (41-3 D.3.3);
# MARKET_UNRESOLVED arrives with the `market` condition key (D3).
# ---------------------------------------------------------------------------

GAP_COUNTRY_UNRESOLVED = "COUNTRY_UNRESOLVED"
GAP_MARKET_UNRESOLVED = "MARKET_UNRESOLVED"

# ---------------------------------------------------------------------------
# The provenance vocabulary. LOAD-BEARING (AD-9, C5): it is how a surface answers
# "how was this country known" -- read off the row, declared by the operator, or
# inferred from a single-country project posture. Never a debug field, never omitted.
#
# FROZEN: these three literals plus '' are the accepted values of the
# `resolution_source` column of dbt/models/marts/fee_tax_country_resolution.sql,
# which Story 41.3 consumes sight unseen. Renaming one is a breaking change.
# ---------------------------------------------------------------------------

SOURCE_ROW_DIMENSION = "row_dimension"  # rung 1
SOURCE_DECLARED_BINDING = "declared_binding"  # rung 2
SOURCE_PROJECT_POSTURE = "project_posture"  # rung 3 (C5) -- a DECLARED INFERENCE

RESOLUTION_SOURCES = frozenset(
    {SOURCE_ROW_DIMENSION, SOURCE_DECLARED_BINDING, SOURCE_PROJECT_POSTURE}
)

# ---------------------------------------------------------------------------
# Gap reasons. Every one of them is a GAP; none of them is a zero.
# ---------------------------------------------------------------------------

REASON_VALUE_UNMAPPED = "country_value_unmapped"  # the code 37.9 already emits
# Story 58.5. The provider reported NO country: the mart keeps the row under the
# declared no-country bucket (`dbt/macros/country_absence.sql`) and this is what that
# row's gap is called. It is NOT `country_value_unmapped`, whose repair surface is
# dimension conformance -- there is no value to conform here, so sending an operator
# there is sending them to a screen with nothing on it. Both engines must know the
# word: `tests/core/test_fee_tax_geo_bridge.py` holds the SQL vocabulary to a strict
# subset of this one, and the SQL side emits it from
# `fee_tax_country_resolution.sql`'s `gap_country_absent_at_source` rung.
#
# The branch is load-bearing: the absence sentinel is evidence from the source, but
# there is no value an operator can map.  It therefore blocks posture inference and
# reaches its own rung after an explicit market binding has had its chance, exactly like
# the SQL twin.
REASON_COUNTRY_ABSENT = "country_absent_at_source"
REASON_OUTSIDE_TRACKED = "country_outside_tracked_markets"
REASON_REGISTRY_UNAVAILABLE = "binding_registry_unavailable"
REASON_AMBIGUOUS = "binding_ambiguous_for_connector"
REASON_NOT_SINGLE_COUNTRY = "market_not_single_country"  # D3: COUNTRY gap only
REASON_NOT_BINDABLE = "binding_not_bindable"  # governed catch-all, not budgetable
# Rung-3 non-firing (C5). These REPLACE a bare "no_binding": with the inference rung in
# place, "nothing declared" alone is never the final answer -- either the Project's
# governed geography resolves the row, or one of these two says which condition blocked
# it.
#
# Story 37.9 renamed both. They used to be `no_binding_and_posture_global` and
# `no_binding_and_posture_multi_country`, and the first had become FALSE: there is no
# posture to be Global any more, and an operator sent to look at one would find a
# preference no surface writes. The truthful statement is that the Project's Country
# MODEL gives us nothing to infer from -- either no version is published, or the
# published one defines no tracked market -- and the repair is that model.
REASON_NO_BINDING_COUNTRY_MODEL_ABSENT = "no_binding_and_country_model_absent"
REASON_NO_BINDING_MULTI_COUNTRY = "no_binding_and_multiple_countries_governed"

GAP_REASONS = frozenset(
    {
        REASON_VALUE_UNMAPPED,
        REASON_COUNTRY_ABSENT,
        REASON_OUTSIDE_TRACKED,
        REASON_REGISTRY_UNAVAILABLE,
        REASON_AMBIGUOUS,
        REASON_NOT_SINGLE_COUNTRY,
        REASON_NOT_BINDABLE,
        REASON_NO_BINDING_COUNTRY_MODEL_ABSENT,
        REASON_NO_BINDING_MULTI_COUNTRY,
    }
)

# The repair surface 37.9 already owns. Do NOT invent a second one: the bridge names the
# same surface the country-conformance monitor raises evidence against, so an operator
# following a gap and an operator following a DQ finding land in the same place.
REPAIR_SURFACE_DIMENSION_CONFORMANCE = "dimension_conformance"
# Rung 2 is declared through the Epic 37 binding registry; that is the surface to fix.
REPAIR_SURFACE_MARKET_BINDING = "market_binding"
# Rung 3 is blocked by the Project's published Country model -- the markets and their
# members, edited in Governance > Master Data and published as a version. Story 37.9
# renamed this from "geographic_posture": that surface was
# `project_preferences.geographic_mode`, which nothing writes any more, so the gap was
# pointing an operator at a screen that could not repair it.
REPAIR_SURFACE_COUNTRY_MODEL = "country_model"

_REPAIR_SURFACE_BY_REASON: dict[str, str] = {
    REASON_VALUE_UNMAPPED: REPAIR_SURFACE_DIMENSION_CONFORMANCE,
    REASON_COUNTRY_ABSENT: "source_data",
    REASON_OUTSIDE_TRACKED: REPAIR_SURFACE_COUNTRY_MODEL,
    REASON_REGISTRY_UNAVAILABLE: REPAIR_SURFACE_MARKET_BINDING,
    REASON_AMBIGUOUS: REPAIR_SURFACE_MARKET_BINDING,
    REASON_NOT_SINGLE_COUNTRY: REPAIR_SURFACE_MARKET_BINDING,
    REASON_NOT_BINDABLE: REPAIR_SURFACE_MARKET_BINDING,
    # The binding is a legitimate repair for both, but a Project with NO governed
    # Country model has nothing to bind TO -- the model comes first.
    REASON_NO_BINDING_COUNTRY_MODEL_ABSENT: REPAIR_SURFACE_COUNTRY_MODEL,
    REASON_NO_BINDING_MULTI_COUNTRY: REPAIR_SURFACE_MARKET_BINDING,
}

# Binding kind convention introduced by Epic 41. Migration 104 keeps binding_kind free
# TEXT on purpose (AD-2, its header ships NO enumeration of binding kinds), so
# 'datastream' is a convention this epic is the first to use -- nothing writes it today.
BINDING_KIND_DATASTREAM = "datastream"


class ConditionOutcome(str, Enum):
    """Three-valued condition logic. A ``str`` Enum so it serialises identically in the
    gap payload, in JSON and in the dbt contract.

    ``NO_MATCH`` and ``UNRESOLVED`` are DISTINCT and must never collapse into one
    another: the first is +0 micros with the cascade intact, the second is a typed gap
    with the total flagged incomplete.
    """

    MATCH = "match"
    NO_MATCH = "no_match"  # condition KNOWN FALSE -> +0 micros (E41-AD9)
    UNRESOLVED = "unresolved"  # attribute UNKNOWABLE -> gap (E41-NFR02 / AD-9)


# ---------------------------------------------------------------------------
# Typed payloads.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolutionGap:
    """One unresolvable attribute on one row.

    A gap is NOT a zero and NOT a "no match": it means the cascade cannot honestly
    evaluate a rule constrained on this attribute, so the rule does not fire and the
    composed total is flagged incomplete (E41-NFR02 / AD-9).
    """

    code: str
    reason: str
    connector: str = ""
    breakdown_dimension: str = ""
    datastream_id: str | None = None
    repair: dict[str, str] = field(default_factory=dict)
    flags_total_incomplete: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "reason": self.reason,
            "connector": self.connector,
            "breakdown_dimension": self.breakdown_dimension,
            "datastream_id": self.datastream_id,
            "repair": dict(self.repair),
            "flags_total_incomplete": self.flags_total_incomplete,
        }


@dataclass(frozen=True, slots=True)
class GeoResolution:
    """Country and market resolve INDEPENDENTLY (arbitration D3).

    A datastream bound to a multi-country market (a market may hold several countries)
    yields a ``market_id`` with ``market_gap is None`` AND ``country_code is None`` with
    ``country_gap.reason == 'market_not_single_country'``. A rule conditioned on
    ``market`` fires there; a rule conditioned on ``country`` is a gap. That asymmetry is
    the point, not an oversight.

    ``country_code`` is ``None`` on EVERY unresolved path. There is no default country,
    ever.

    ``source`` is the load-bearing provenance of the COUNTRY resolution (AD-9 / AC14):
    it is non-empty if and only if the country resolved.
    """

    country_code: str | None = None
    country_gap: ResolutionGap | None = None
    market_id: str | None = None
    market_gap: ResolutionGap | None = None
    source: str | None = None

    @property
    def is_country_resolved(self) -> bool:
        return self.country_code is not None and self.country_gap is None

    @property
    def is_market_resolved(self) -> bool:
        return self.market_id is not None and self.market_gap is None

    @property
    def resolution_source(self) -> str:
        """The frozen dbt-contract value: one of the three labels, or ``''`` for a gap."""
        return self.source or ""

    def gaps(self) -> tuple[ResolutionGap, ...]:
        return tuple(gap for gap in (self.country_gap, self.market_gap) if gap is not None)

    def gap_codes(self) -> tuple[str, ...]:
        """Sorted, distinct gap codes -- the Python twin of the view's ``gap_code``."""
        return tuple(sorted({gap.code for gap in self.gaps()}))

    def as_dict(self) -> dict[str, Any]:
        return {
            "country_code": self.country_code,
            "market_id": self.market_id,
            "resolution_source": self.resolution_source,
            "country_gap": self.country_gap.as_dict() if self.country_gap else None,
            "market_gap": self.market_gap.as_dict() if self.market_gap else None,
        }


# ---------------------------------------------------------------------------
# The governed geography this bridge reasons from (Story 37.9).
#
# It is built from `country_registry.GeographyProjection` -- the SAME object Analyze and
# the Tax MCP proposal path read -- and holds NO connection: the bridge stays pure and
# testable, and the read lives with the caller that already owns a transaction.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GovernedMarket:
    """One TRACKED reporting market of the published hierarchy version."""

    id: str
    label: str
    country_codes: tuple[str, ...]

    @property
    def is_single_country(self) -> bool:
        return len(self.country_codes) == 1

    @property
    def single_country_code(self) -> str | None:
        return self.country_codes[0] if self.is_single_country else None


@dataclass(frozen=True, slots=True)
class GovernedGeography:
    """The Project's published country -> market meaning, or its ABSENCE.

    ``markets`` holds TRACKED markets only. Rest of World is a governed catch-all over
    countries not assigned to an explicit reporting market (``capabilities/country.md``),
    so its members are resolved geographically but are not a tracked market: a row landing
    there says ``country_outside_tracked_markets``, and a binding naming it is not
    bindable. That is the same boundary the retired posture drew with its synthetic ids,
    now drawn by a governed node kind.

    An empty instance is "this Project has published no Country meaning we can infer
    from". It is NOT Global -- ``load_projection`` returns ``None`` for the same case and
    forbids that substitution -- and it reaches its own rung-4 reason.
    """

    hierarchy_version_id: str | None = None
    markets: tuple[GovernedMarket, ...] = ()

    @classmethod
    def absent(cls) -> "GovernedGeography":
        """The honest empty: no published meaning, and no claim that there is one."""
        return cls()

    @classmethod
    def from_projection(cls, projection: "GeographyProjection | None") -> "GovernedGeography":
        if projection is None:
            return cls.absent()

        # Imported here, not at module scope, so the single source of the node-kind word
        # stays `country_registry` while this module keeps its light import graph.
        from core.country_registry import MARKET  # noqa: PLC0415

        by_market: dict[str, list[str]] = {}
        for country_code, market_id in projection.market_of_value.items():
            node_id = str(market_id)
            if projection.kinds.get(node_id) != MARKET:
                # Rest of World, or a node this version does not describe as a market.
                continue
            code = str(country_code).strip().upper()
            if code:
                by_market.setdefault(node_id, []).append(code)
        markets = tuple(
            GovernedMarket(
                id=market_id,
                label=str(projection.labels.get(market_id) or market_id),
                country_codes=tuple(sorted(set(codes))),
            )
            # One canonical order everywhere, exactly as the posture aggregate did.
            # Ordering is presentation determinism, never a statement about importance.
            for market_id, codes in sorted(by_market.items(), key=lambda item: item[0].lower())
        )
        return cls(
            hierarchy_version_id=projection.hierarchy_version_id,
            markets=markets,
        )

    @property
    def is_governed(self) -> bool:
        """True when there is a tracked market to reason from. Absence is not Global."""
        return bool(self.markets)

    @property
    def country_codes(self) -> tuple[str, ...]:
        """The UNION of every tracked market's members -- never the market count.

        One market may hold several countries, and counting markets would resolve a
        multi-country project to an arbitrary code (AC14).
        """
        return tuple(sorted({code for market in self.markets for code in market.country_codes}))

    def market_by_id(self, market_id: object) -> GovernedMarket | None:
        """Resolve a BINDING by market id, never by country code.

        Binding on the id is what makes a relabel non-breaking. ``None`` covers the
        governed catch-all, an archived market and an id this version no longer knows --
        all three are "not budgetable", which is one branch rather than a name list.
        """
        wanted = str(market_id or "").strip().lower()
        if not wanted:
            return None
        return next((market for market in self.markets if market.id.lower() == wanted), None)

    def market_for_country(self, code: object) -> GovernedMarket | None:
        wanted = str(code or "").strip().upper()
        if not wanted:
            return None
        return next(
            (market for market in self.markets if wanted in market.country_codes), None
        )


# ---------------------------------------------------------------------------
# The evaluators -- the two functions review reads first.
# ---------------------------------------------------------------------------


def _normalise_country_list(values: Sequence[str] | None) -> frozenset[str]:
    return frozenset(str(item).strip().upper() for item in (values or ()) if str(item).strip())


def _normalise_market_list(values: Sequence[str] | None) -> frozenset[str]:
    # Market ids are opaque operator handles; Epic 37 compares them case-insensitively
    # (GeographicPosture.market_by_id), so the bridge must too or a relabelled-case id
    # would silently stop matching.
    return frozenset(str(item).strip().lower() for item in (values or ()) if str(item).strip())


def evaluate_country_condition(
    rule_countries: Sequence[str] | None,
    resolution: GeoResolution,
) -> ConditionOutcome:
    """Evaluate a rule's ``conditions.country`` against one resolved row.

    * no country condition       -> ``MATCH``      (an unconditioned rule is not poisoned
                                                    by an unknown attribute -- otherwise
                                                    activating the module on a connector
                                                    with no country grain would gap out
                                                    every rule)
    * resolved, code in the list -> ``MATCH``
    * resolved, code NOT in it   -> ``NO_MATCH``   (KNOWN FALSE: +0 micros, the cascade
                                                    continues, nothing is flagged)
    * country unresolved         -> ``UNRESOLVED`` (GAP: the rule does not fire, the total
                                                    is flagged incomplete, NO zero is
                                                    fabricated)

    ``UNRESOLVED`` is never reported as ``NO_MATCH``.
    """
    wanted = _normalise_country_list(rule_countries)
    if not wanted:
        return ConditionOutcome.MATCH
    if not resolution.is_country_resolved:
        return ConditionOutcome.UNRESOLVED
    if resolution.country_code in wanted:
        return ConditionOutcome.MATCH
    return ConditionOutcome.NO_MATCH


def evaluate_market_condition(
    rule_markets: Sequence[str] | None,
    resolution: GeoResolution,
) -> ConditionOutcome:
    """Same three outcomes, read off ``resolution.market_id`` (D3).

    Reads a DIFFERENT field of the same ``GeoResolution`` than
    ``evaluate_country_condition``, which is exactly what makes the D3 asymmetry true: a
    multi-country market resolves HERE (``MATCH``) while the country stays a gap
    (``UNRESOLVED``) on the very same row.

    Known false (+0 micros) and unresolvable (typed gap) stay distinct here too.
    """
    wanted = _normalise_market_list(rule_markets)
    if not wanted:
        return ConditionOutcome.MATCH
    if not resolution.is_market_resolved:
        return ConditionOutcome.UNRESOLVED
    market_id = (resolution.market_id or "").strip().lower()
    return ConditionOutcome.MATCH if market_id in wanted else ConditionOutcome.NO_MATCH


def combine_outcomes(*outcomes: ConditionOutcome) -> ConditionOutcome:
    """``UNRESOLVED > NO_MATCH > MATCH`` -- the Python twin of 41.3 section D.3.2.

    ``UNRESOLVED`` beats ``NO_MATCH`` deliberately: if we cannot evaluate one clause, we
    cannot honestly claim the conjunction is false. Asserting "false" would silently skip
    a rule that might have applied, and turn a gap into a fabricated +0.

    No outcomes at all -> ``MATCH`` (an unconditioned rule matches every row).
    """
    if any(outcome is ConditionOutcome.UNRESOLVED for outcome in outcomes):
        return ConditionOutcome.UNRESOLVED
    if any(outcome is ConditionOutcome.NO_MATCH for outcome in outcomes):
        return ConditionOutcome.NO_MATCH
    return ConditionOutcome.MATCH


# ---------------------------------------------------------------------------
# The declared-binding index (rung 2) and its honest degradation.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BindingIndex:
    """Declared datastream -> market bindings for one project, keyed by connector.

    ``registry_available`` is NOT cosmetic. ``market_governance.fetch_market_bindings``
    collapses "the registry says there is no binding" and "the registry could not be
    read" into the same empty tuple; this flag is what keeps them apart, so the final gap
    reason can say which one happened. Both fall through to rung 3 -- the posture rung
    needs no registry -- but they carry different reasons when rung 3 declines.
    """

    registry_available: bool = False
    market_ids_by_connector: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def market_ids_for(self, connector: object) -> frozenset[str]:
        key = str(connector or "").strip()
        if not key:
            return frozenset()
        return self.market_ids_by_connector.get(key, frozenset())


# Story 48.2: the used-by authority moved from app.market_bindings (mutable,
# unversioned) to the shared Master Data contract. The probe follows it.
_REGCLASS_PROBE_SQL = "SELECT to_regclass('app.master_data_used_by') IS NOT NULL"

# consumer_id IS the datastream id and connector IS module_name: the used-by store has
# NEITHER a connector column NOR a datastream_id column -- it carries only the untyped
# (consumer_kind, consumer_id) pair, exactly as its predecessor did. This mirrors, in
# Python, what mirror_sync's datastream_country_binding_dim projection does in SQL, so
# the two engines cannot drift.
_DATASTREAM_CONNECTOR_SQL = """
    SELECT id, module_name
    FROM app.datastreams
    WHERE project_id = %s
      AND archived_at IS NULL
"""


def load_datastream_binding_index(project_id: str, conn: object) -> BindingIndex:
    """Build the rung-2 index. NEVER raises.

    This is deliberately the OPPOSITE contract from
    ``market_governance.fetch_market_bindings``, which Story 48.2 made raise. The
    difference is what the answer authorizes: that one guards a destructive
    change, where "I could not read" must block; this one enriches a reporting
    read, where "I could not read" is an honest ``registry_available=False`` that
    simply stops rung 2 from firing. Neither ever reports a confident zero.
    """
    from core.market_governance import fetch_market_bindings  # noqa: PLC0415

    try:
        with conn.cursor() as cur:  # type: ignore[attr-defined]
            cur.execute(_REGCLASS_PROBE_SQL)
            row = cur.fetchone()
        if not (row and row[0]):
            logger.info(
                "fee_tax_geo_bridge: market binding registry absent project=%s "
                "(migration 104 not applied) -- rung 2 cannot fire",
                project_id,
            )
            return BindingIndex(registry_available=False)

        bindings = fetch_market_bindings(project_id, conn)

        with conn.cursor() as cur:  # type: ignore[attr-defined]
            cur.execute(_DATASTREAM_CONNECTOR_SQL, (project_id,))
            datastream_rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "fee_tax_geo_bridge: binding index unreadable project=%s: %s", project_id, exc
        )
        return BindingIndex(registry_available=False)

    connector_by_datastream = {
        str(row[0]): str(row[1]).strip()
        for row in (datastream_rows or ())
        if row and row[0] and row[1]
    }

    index: dict[str, set[str]] = {}
    for binding in bindings:
        if binding.binding_kind != BINDING_KIND_DATASTREAM:
            continue
        connector = connector_by_datastream.get(str(binding.binding_id))
        if not connector:
            # A binding whose binding_id is not a LIVE datastream of this project is
            # dropped, exactly like the mirror's INNER JOIN. Not an error: binding_kind
            # is free TEXT and the registry may bind anything at all.
            continue
        index.setdefault(connector, set()).add(str(binding.market_id))

    return BindingIndex(
        registry_available=True,
        market_ids_by_connector={key: frozenset(value) for key, value in index.items()},
    )


# ---------------------------------------------------------------------------
# The ladder.
# ---------------------------------------------------------------------------


def _gap_pair(
    reason: str,
    *,
    connector: str,
    breakdown_dimension: str,
    datastream_id: str | None,
) -> tuple[ResolutionGap, ResolutionGap]:
    """Both axes unresolved for the same reason."""
    repair = {"surface": _REPAIR_SURFACE_BY_REASON.get(reason, REPAIR_SURFACE_MARKET_BINDING)}
    return (
        ResolutionGap(
            code=GAP_COUNTRY_UNRESOLVED,
            reason=reason,
            connector=connector,
            breakdown_dimension=breakdown_dimension,
            datastream_id=datastream_id,
            repair=dict(repair),
        ),
        ResolutionGap(
            code=GAP_MARKET_UNRESOLVED,
            reason=reason,
            connector=connector,
            breakdown_dimension=breakdown_dimension,
            datastream_id=datastream_id,
            repair=dict(repair),
        ),
    )


def _single_gap(
    code: str,
    reason: str,
    *,
    connector: str,
    breakdown_dimension: str,
    datastream_id: str | None,
) -> ResolutionGap:
    return ResolutionGap(
        code=code,
        reason=reason,
        connector=connector,
        breakdown_dimension=breakdown_dimension,
        datastream_id=datastream_id,
        repair={"surface": _REPAIR_SURFACE_BY_REASON.get(reason, REPAIR_SURFACE_MARKET_BINDING)},
    )


def resolve_row_geography(
    row: Mapping[str, Any],
    *,
    geography: GovernedGeography,
    resolver: Any = None,
    binding_index: BindingIndex | None = None,
) -> GeoResolution:
    """Resolve one spend row's country and market. Captures NOTHING.

    ``row`` needs ``connector``, ``breakdown_dimension`` and ``breakdown_value``; an
    optional ``datastream_id`` is carried into the gap payload when the caller knows it.

    ``geography`` is the Project's PUBLISHED country meaning
    (``GovernedGeography.from_projection(country_registry.load_projection(...))``). An
    empty one means "no published Country meaning", which reaches its own rung-4 reason
    and is never read as Global -- see the module docstring.

    ``resolver`` is a ``geographic_conformance.CountryResolver``
    (``(raw_value, connector) -> ISO | None``). When none is injected the bridge falls
    back to the shared seed through ``country_vocabulary.normalize_country_value`` -- it
    NEVER implements its own normalisation.

    Forward-compatible with Story 37.6 by construction (AC5): the function branches on
    ``breakdown_dimension``, the binding index and the governed geography ONLY. There is
    no connector name anywhere, so the day paid-media rows carry a real country fanout
    they resolve at rung 1 -- and they automatically OUTRANK the inference, because rung 1
    is first.

    Known false (+0 micros) is a matter for the evaluators; this function only ever
    produces a resolved attribute or a TYPED GAP. It never produces a zero.
    """
    connector = str(row.get("connector") or "")
    breakdown_dimension = str(row.get("breakdown_dimension") or "")
    raw_datastream_id = row.get("datastream_id")
    datastream_id = str(raw_datastream_id) if raw_datastream_id else None
    index = binding_index if binding_index is not None else BindingIndex()

    # -------------------------------------------------- RUNG 1: the row's own country
    # EXACT match on the canonical country partition: a composite sub-dimension such as
    # 'country>device' is NOT a country row (geographic_semantics A.2), and treating it
    # as one would read a composite value as an ISO code.
    row_value_unmapped = False
    row_country_absent = False
    if breakdown_dimension == COUNTRY_PARTITION:
        raw_value = row.get("breakdown_value")
        row_country_absent = (
            isinstance(raw_value, str)
            and raw_value.strip().casefold() == COUNTRY_ABSENT_BUCKET_ID.casefold()
        )
        if resolver is not None:
            code = resolver(raw_value, connector)
        else:
            code = normalize_country_value(raw_value)
        if code:
            market = geography.market_for_country(code)
            if market is not None:
                return GeoResolution(
                    country_code=code,
                    market_id=market.id,
                    source=SOURCE_ROW_DIMENSION,
                )
            # The country IS resolved; only the market is not. The code sits in the
            # governed Rest of World catch-all, or in no market at all -- either way it
            # belongs to no tracked reporting market and is not bindable.
            return GeoResolution(
                country_code=code,
                market_gap=_single_gap(
                    GAP_MARKET_UNRESOLVED,
                    REASON_OUTSIDE_TRACKED,
                    connector=connector,
                    breakdown_dimension=breakdown_dimension,
                    datastream_id=datastream_id,
                ),
                source=SOURCE_ROW_DIMENSION,
            )
        # The row DOES carry a country the platform could not canonicalise. That is
        # evidence, not silence: the inference must not paper over it (rung 3 is blocked
        # below). A DECLARED binding may still answer -- an operator declaration outranks
        # an inference -- but if nothing is declared the honest reason is the one 37.9
        # already emits, pointing at the dimension_conformance repair surface.
        row_value_unmapped = not row_country_absent

    # ------------------------------------------------- RUNG 2: the declared binding
    registry_unavailable = not index.registry_available
    if geography.is_governed and index.registry_available:
        market_ids = index.market_ids_for(connector)
        if len(market_ids) >= 2:
            # A CONTRADICTION. Never pick one, and never let rung 3 rescue it.
            country_gap, market_gap = _gap_pair(
                REASON_AMBIGUOUS,
                connector=connector,
                breakdown_dimension=breakdown_dimension,
                datastream_id=datastream_id,
            )
            return GeoResolution(country_gap=country_gap, market_gap=market_gap)
        if len(market_ids) == 1:
            market_id = next(iter(market_ids))
            market = geography.market_by_id(market_id)
            if market is None:
                # A binding to the governed Rest of World catch-all, to an archived
                # market, or to an id this published version no longer knows. Also a
                # CONTRADICTION: no rung-3 rescue.
                country_gap, market_gap = _gap_pair(
                    REASON_NOT_BINDABLE,
                    connector=connector,
                    breakdown_dimension=breakdown_dimension,
                    datastream_id=datastream_id,
                )
                return GeoResolution(country_gap=country_gap, market_gap=market_gap)
            if market.is_single_country:
                return GeoResolution(
                    country_code=market.single_country_code,
                    market_id=market.id,
                    source=SOURCE_DECLARED_BINDING,
                )
            # D3, encoded: the MARKET resolves, the COUNTRY does not. A rule on `market`
            # fires; a rule on `country` is a gap. `resolution_source` stays '' because it
            # is the provenance of the COUNTRY resolution (AC14 / test 12i).
            return GeoResolution(
                market_id=market.id,
                country_gap=_single_gap(
                    GAP_COUNTRY_UNRESOLVED,
                    REASON_NOT_SINGLE_COUNTRY,
                    connector=connector,
                    breakdown_dimension=breakdown_dimension,
                    datastream_id=datastream_id,
                ),
            )
        # Nothing declared for this connector -> a SILENCE. Fall through to rung 3.

    # ------------------------------- RUNG 3: the single governed country (C5, AC14)
    # The operator has PUBLISHED a Country model tracking EXACTLY ONE country;
    # attributing that project's spend to it is a DECLARED INFERENCE, and
    # resolution_source='project_posture' is what makes it inspectable. This is the AD-9
    # posture, not a silent default.
    #
    # THE LITERAL 'project_posture' IS FROZEN (see SOURCE_PROJECT_POSTURE) and Story 37.9
    # deliberately did NOT rename it. The rung means what it always meant -- an inference
    # from the Project's own single tracked country -- only its authority moved from a
    # mutable preference to a published, versioned hierarchy.
    #
    # Independent of migration 104: this rung reads only the published hierarchy, so a
    # single-country project resolves with no binding declared. That is why it exists.
    #
    # It fires on the UNION of country codes across every tracked market -- NOT on the
    # number of markets. One market may hold several countries, and counting markets would
    # resolve a multi-country project to an arbitrary code. We never guess between two.
    tracked_codes = geography.country_codes
    if not row_value_unmapped and not row_country_absent and len(tracked_codes) == 1:
        code = tracked_codes[0]
        market = geography.market_for_country(code)
        return GeoResolution(
            country_code=code,
            market_id=market.id if market is not None else None,
            source=SOURCE_PROJECT_POSTURE,
        )

    # ---------------------------------------------------------------- RUNG 4: the gap
    if row_country_absent:
        reason = REASON_COUNTRY_ABSENT
    elif row_value_unmapped:
        reason = REASON_VALUE_UNMAPPED
    elif registry_unavailable:
        # "Cannot read" is NOT "nothing declared". The probe is independent of the
        # geography, so it is reported even for a Project with no published Country model
        # where rung 2 could not have fired anyway: it is the truthful primary blocker.
        reason = REASON_REGISTRY_UNAVAILABLE
    elif not geography.is_governed:
        # NOT "the posture is Global": either no Country version is published, or the
        # published one defines no tracked market. Both send the operator to the Country
        # model, which is the only thing that can repair either.
        reason = REASON_NO_BINDING_COUNTRY_MODEL_ABSENT
    else:
        reason = REASON_NO_BINDING_MULTI_COUNTRY

    country_gap, market_gap = _gap_pair(
        reason,
        connector=connector,
        breakdown_dimension=breakdown_dimension,
        datastream_id=datastream_id,
    )
    return GeoResolution(country_gap=country_gap, market_gap=market_gap)
