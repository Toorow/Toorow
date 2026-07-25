"""toorow -- Cross-datastream money reconciliation on NORMALIZED values (Story 39.6, Epic 39).

The money-reconciliation CAPSTONE of Epic 39's Module 1 (E39-FR08 / E39-AD5). This module
adds the ONE composition rule E39-AD5 mandates and NOTHING else: when reconciliation runs on
a MONETARY metric, every per-source contribution is CONVERTED to the reporting currency FIRST
(Story 39.4), an un-nameable currency or an unresolvable FX rate is REFUSED (Story 39.3 / 39.4,
fail-closed), and ONLY THEN is the client-metric-definition-matrix method (Story 27.3
``resolve_route``: SUM / PRIORITY / DEDUP_ID / ESTIMATE / KEEP_SEPARATE) allowed to combine
them -- with the reconciled figure carrying AD-9 provenance (definition applied + per-source
contributions each with their FX conversion block).

This module COMPOSES four LANDED engines; it FORKS none of them:
  * ``metric_semantics.is_metric_monetary``      (39.1) -- which metrics are money.
  * ``currency_refusal.check_monetary_aggregation`` (39.3) -- the fail-closed unknown-currency
    guard, run BEFORE any conversion.
  * ``fx_helper.convert``                         (39.4) -- normalize each contribution to the
    reporting currency (identity when ``from == reporting``, no float drift).
  * ``metric_reconciliation.resolve_route``       (27.3) -- the matrix method / RouteDecision.

Design decision (compose, do NOT fork): ``resolve_route`` is the currency-BLIND matrix
resolver -- it reasons over emitters and methods, never over amounts or currencies, and reads
no warehouse row (invariant 1). E39-AD5 is a NEW obligation that sits BEFORE the method is
applied ("normalize first"), so it belongs in a composing layer that CALLS ``resolve_route``,
not inside it. Editing ``resolve_route`` to convert currencies would break its PASSIVE
contract and fork the money stack into the 27.3-shared surface. This separate module also
keeps E39-NFR06 provable by construction: a non-monetary metric BYPASSES this module (its
reconciliation is the literally-unchanged 27.3 path), and the single-source monetary path is a
pure identity pass-through (one contribution, ``from == reporting`` -> rate 1.0, ``amount``
echoed byte-identical), so an already-correct single-source total is bit-identical.

Why 39.6 CONVERTS a known cross-currency set instead of refusing it (subtle -- read before you
"fix" this): 39.3's ``check_monetary_aggregation`` refuses >=2 distinct KNOWN currencies
because 39.3 is the PRE-FX guard (no conversion is recorded at that layer, so summing them
would be a silent wrong total -- the "choux et carottes" bug). 39.6 is the layer that DOES
record a conversion, so for a KNOWN multi-currency set it converts each to the reporting
currency (39.4) and the sum is legitimate and provenanced. 39.6 still FAIL-CLOSES on:
  * UNKNOWN_CURRENCY_GAP  -- an un-nameable currency cannot be converted (39.3 precedence), and
  * a conversion gap (MissingFxPair / MissingAsOfRate) -- a nameable currency with no rate.
So 39.6 runs the 39.3 guard ONLY for the unknown-currency case (its precedence checks unknown
FIRST) and lets a per-contribution conversion gap refuse the known cross-currency case.

The mart boundary (invariant 1 -- no warehouse read in core): for a ``ROUTED_TO_MART``
decision (PRIORITY / DEDUP_ID / ESTIMATE), the NUMBER's home is the pre-computed dbt mart. This
core orchestrator, like ``metric_reconciliation.py``, does NOT open a warehouse cursor -- it
returns ``status=ROUTED_TO_MART`` + the ``RouteDecision`` (which mart, which priority order) +
the converted per-source contributions as the drill-down provenance; ``amount is None``. Only
a SUM of same-currency-after-conversion produces an in-core combined figure. The surface that
has warehouse access reads the mart on the normalized inputs.

Consumed downstream (do NOT build here): Epic 17 (verification dedup) and Epic 22 (media-plan
money math) call ``reconcile_monetary_metric`` -- its signature is kept clean and its
``ReconciledMoney`` self-describing so those epics compose it without a re-fork. The timezone
alignment (E39-FR14 / Stories 39.7-39.8) is Module 2: ``report_timezone`` provenance is a
documented seam NOT consumed here.

House style mirrors ``metric_reconciliation.py`` / ``fx_helper.py``:
``from __future__ import annotations``, module logger, pure function split from I/O, typed
frozen dataclasses, ASCII-only strings, and ZERO connector/provider vocabulary (AD-2 /
E39-NFR05). Metric names (``revenue``, ``cost``) are dictionary vocabulary (tolerated, like the
27.3 method table); provider names are forbidden.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from core import fx_helper, metric_reconciliation

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Reconciliation statuses (str constants, mutually exclusive). They MAP from the
# 27.3 RouteStatus so the consumer stays trivial. See the mapping table below.
# ---------------------------------------------------------------------------

# A single combined figure was produced in core (SUM of same-currency-after-conversion values).
RECONCILED = "RECONCILED"
# The metric is not monetary: this module does not apply -> the 27.3 decision is returned
# verbatim (no conversion, no combination). E39-NFR06 by construction.
PASSTHROUGH = "PASSTHROUGH"
# Exactly one contribution: identity pass-through, source amount echoed byte-identical (AC-4).
SINGLE_SOURCE = "SINGLE_SOURCE"
# The matrix routes to a pre-computed mart (PRIORITY / DEDUP_ID / ESTIMATE). The mart owns the
# number (invariant 1: no warehouse read here) -> amount is None + the RouteDecision + the
# converted per-source drill-down.
ROUTED_TO_MART = "ROUTED_TO_MART"
# 27.3 KEEP_SEPARATE: per-source series, never a total (honour the D-5 contract).
KEEP_SEPARATE = "KEEP_SEPARATE"
# A typed money refusal / FX gap -- no number returned (AC-2, fail-closed).
REFUSED = "REFUSED"
# 27.3 no-combine reasons passed through honestly (amount is None).
UNRULED_OVERLAP = "UNRULED_OVERLAP"
NOT_COMBINABLE = "NOT_COMBINABLE"
OVERRIDE_NOT_MATERIALIZED = "OVERRIDE_NOT_MATERIALIZED"

# Mapping from metric_reconciliation.RouteStatus.* to a 39.6 status, for the >=2-source
# monetary path (SINGLE_SOURCE / PASSTHROUGH / REFUSED are decided before this table).
#   RouteStatus.DIRECT_SUM               -> RECONCILED           (sum the normalized values)
#   RouteStatus.ROUTED_TO_MART           -> ROUTED_TO_MART       (mart owns the number)
#   RouteStatus.KEEP_SEPARATE            -> KEEP_SEPARATE        (series, no total)
#   RouteStatus.UNRULED_OVERLAP          -> UNRULED_OVERLAP      (honest no-combine)
#   RouteStatus.NOT_COMBINABLE           -> NOT_COMBINABLE       (honest no-combine)
#   RouteStatus.OVERRIDE_NOT_MATERIALIZED-> OVERRIDE_NOT_MATERIALIZED (honest no-combine)
_ROUTE_STATUS_TO_39_6 = {
    metric_reconciliation.RouteStatus.DIRECT_SUM: RECONCILED,
    metric_reconciliation.RouteStatus.ROUTED_TO_MART: ROUTED_TO_MART,
    metric_reconciliation.RouteStatus.KEEP_SEPARATE: KEEP_SEPARATE,
    metric_reconciliation.RouteStatus.UNRULED_OVERLAP: UNRULED_OVERLAP,
    metric_reconciliation.RouteStatus.NOT_COMBINABLE: NOT_COMBINABLE,
    metric_reconciliation.RouteStatus.OVERRIDE_NOT_MATERIALIZED: OVERRIDE_NOT_MATERIALIZED,
}

# The subset of 39.6 statuses that carry NO in-core combined figure (amount is None).
_NO_AMOUNT_STATUSES = frozenset(
    {
        ROUTED_TO_MART,
        KEEP_SEPARATE,
        REFUSED,
        UNRULED_OVERLAP,
        NOT_COMBINABLE,
        OVERRIDE_NOT_MATERIALIZED,
    }
)


# ---------------------------------------------------------------------------
# Typed result objects (frozen -- immutable, comparable, hashable-safe; mirror
# metric_reconciliation.SourceSeries / RouteDecision).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceContribution:
    """One per-source contribution WITH its 39.4 conversion block (the drill-down unit).

    ``converted`` is the ``fx_helper.ConvertedAmount`` produced by normalizing this source's
    ``source_amount``/``source_currency`` to the reporting currency: it carries the immutable
    source inputs echoed back plus the ``fx`` provenance ``{rate, as_of_date, source, tier}``.
    ``connector`` is the emitting datastream label taken from the contribution's own
    provenance (never hardcoded -- AD-2). ``on_date`` is the figure's own date when supplied
    (drives the as-of tier)."""

    connector: str
    source_amount: float
    source_currency: str
    converted: fx_helper.ConvertedAmount
    on_date: date | None = None


@dataclass(frozen=True)
class ReconciledMoney:
    """The AD-9-shaped reconciliation result (definition applied + per-source drill-down).

    ``amount is None`` <=> a refusal / gap / keep-separate / mart-routed / no-combine outcome
    (never a number when the honest answer is "cannot combine in core"). Only ``RECONCILED``
    (in-core SUM of normalized values) and ``SINGLE_SOURCE`` (identity pass-through) carry a
    number.

    ``route`` is the 27.3 ``RouteDecision`` (the client-metric-definition-matrix DECISION:
    method + target_mart + priority_order + reason). ``contributions`` is the ordered tuple of
    per-source ``SourceContribution`` (each with its ``converted.fx`` block). ``refusal`` is the
    typed money refusal / FX-gap payload on the ``REFUSED`` path (else None). ``reason`` mirrors
    ``route.reason`` for the honest no-combine paths so a consumer reads it without unpacking
    the route."""

    metric: str
    status: str
    amount: float | None
    reporting_currency: str
    route: metric_reconciliation.RouteDecision | None = None
    contributions: tuple[SourceContribution, ...] = ()
    refusal: dict | None = None
    reason: metric_reconciliation.ReconciliationReason | None = None

    def as_ad9_provenance(self) -> dict:
        """Assemble the total-level AD-9 chain WITHOUT mutating any input (E39-NFR03).

        Returns ``{metric, status, reporting_currency, amount, method, target_mart,
        priority_order, contributions:[{connector, source_amount, source_currency, fx}, ...],
        reason}`` -- one entry per source, each reusing its ``ConvertedAmount.as_ad9_provenance``
        to surface the ``fx`` sub-object. This is E39-NFR03 "full derivation chain, drillable"
        at the reconciled-figure level: a consumer drills from the total to exactly HOW each
        source was converted and WHICH rule combined them."""
        method = self.route.method if self.route is not None else None
        target_mart = self.route.target_mart if self.route is not None else None
        priority_order = list(self.route.priority_order) if self.route is not None else []
        reason = self.reason or (self.route.reason if self.route is not None else None)
        contributions = []
        for contrib in self.contributions:
            base = {
                "connector": contrib.connector,
                "source_amount": contrib.source_amount,
                "source_currency": contrib.source_currency,
            }
            # as_ad9_provenance merges the fx block into a COPY of base (no mutation).
            contributions.append(contrib.converted.as_ad9_provenance(base))
        return {
            "metric": self.metric,
            "status": self.status,
            "reporting_currency": self.reporting_currency,
            "amount": self.amount,
            "method": method,
            "target_mart": target_mart,
            "priority_order": priority_order,
            "contributions": contributions,
            "reason": _reason_as_dict(reason),
            "refusal": dict(self.refusal) if self.refusal else None,
        }


def _reason_as_dict(reason: metric_reconciliation.ReconciliationReason | None) -> dict | None:
    """Render a 27.3 ReconciliationReason as a plain dict (or None)."""
    if reason is None:
        return None
    return {
        "code": reason.code,
        "message": reason.message,
        "emitters": list(reason.emitters),
    }


# ---------------------------------------------------------------------------
# The E39-AD5 orchestrator -- reconcile_monetary_metric (the load-bearing order).
# ---------------------------------------------------------------------------


def reconcile_monetary_metric(
    project_id: str,
    metric: str,
    contributions: list[dict],
    *,
    reporting_currency: str | None = None,
    on_date: date | None = None,
    tier: str = fx_helper.TIER_FIXED,
    provider=None,
    is_monetary: bool | None = None,
    route_resolver=None,
    converter=None,
    refusal_checker=None,
    reporting_currency_resolver=None,
    is_monetary_resolver=None,
) -> ReconciledMoney:
    """Reconcile a metric across datastreams on CURRENCY-NORMALIZED values (E39-AD5).

    The ONLY correct order (E39-AD5 verbatim: "the matrix operates on values already converted
    to the reporting currency ... never on raw mixed-currency inputs"):

      0. classify        -- is *metric* monetary? (39.1). NOT monetary -> PASSTHROUGH: return
                            the 27.3 ``resolve_route`` decision VERBATIM (money-only wrapper;
                            counts/ratios reconcile via the untouched 27.3 path, E39-NFR06).
      1. reporting ccy   -- resolve per project (default EUR, never a core hardcode, AC-6).
      2. refuse-unknown  -- run the 39.3 guard for the UNKNOWN_CURRENCY_GAP case (fail-closed
                            FIRST; you cannot convert a currency you cannot name). A KNOWN
                            multi-currency set is NOT refused here -- 39.6 converts it (step 3).
      3. convert-each    -- normalize EVERY contribution to the reporting currency via 39.4
                            (identity when ``from == reporting``). A conversion gap
                            (MissingFxPair / MissingAsOfRate) -> REFUSED (no number). This is
                            the guarantee that the combination step sees ONLY reporting-ccy
                            values.
      4. single-source   -- exactly ONE contribution: skip the method, echo the single
                            converted amount (identity FX -> byte-identical, AC-4).
      5. route/combine   -- route by the matrix method (27.3). SUM -> RECONCILED (sum the
                            NORMALIZED values). ROUTED_TO_MART -> the mart owns the number
                            (amount None + drill-down, invariant 1). KEEP_SEPARATE / unruled /
                            not-combinable / override -> pass the 27.3 reason through (no total).

    Every collaborator is INJECTABLE (``route_resolver``/``converter``/``refusal_checker``/
    ``is_monetary_resolver``/``reporting_currency_resolver``, plus the 39.4 ``provider``/``tier``)
    so the orchestrator is fully unit-testable offline with fakes -- no DB, no server.

    ``contributions`` is a list of dicts shaped exactly like the 39.3 contribution
    (``{connector?, amount, currency, on_date?, source_system?, source_field?, pull_id?}``).
    """
    _convert = converter or fx_helper.convert
    _resolve_route = route_resolver or metric_reconciliation.resolve_route
    _check_refusal = refusal_checker or _default_refusal_checker
    _resolve_reporting = reporting_currency_resolver or fx_helper.resolve_reporting_currency
    _classify = is_monetary_resolver or _default_is_monetary

    # --- Step 0: classify (39.1 seam, fail-soft) -----------------------------
    monetary = is_monetary if is_monetary is not None else _classify(metric, project_id)
    if not monetary:
        # Money-only wrapper: a non-monetary metric reconciles via the UNCHANGED 27.3 path.
        route = _resolve_route(project_id, metric)
        return ReconciledMoney(
            metric=metric,
            status=PASSTHROUGH,
            amount=None,
            reporting_currency=(reporting_currency or ""),
            route=route,
            contributions=(),
            reason=route.reason,
        )

    # --- Step 1: resolve the reporting currency (per project, AC-6) ----------
    reporting = reporting_currency or _resolve_reporting(project_id)

    # --- Step 2: pre-conversion fail-closed guard (UNKNOWN_CURRENCY only) -----
    # 39.3 refuses >=2 distinct KNOWN currencies too, but 39.6 CONVERTS those (step 3), so we
    # only honour the UNKNOWN_CURRENCY_GAP refusal here -- an un-nameable currency cannot be
    # converted. A known cross-currency set falls through to per-contribution conversion.
    refusal = _check_refusal(
        metric=metric,
        contributions=contributions,
        is_monetary=True,
        reporting_currency=reporting,
    )
    if refusal is not None and refusal.get("code") == _UNKNOWN_CURRENCY_CODE:
        return _refused(metric, reporting, refusal)

    # --- Step 3: convert EVERY contribution to the reporting currency (AC-1) --
    built: list[SourceContribution] = []
    for contrib in contributions:
        contrib_on_date = contrib.get("on_date") or on_date
        source_currency = contrib.get("currency")
        source_amount = contrib.get("amount")
        try:
            converted = _convert(
                source_amount,
                source_currency,
                reporting_currency=reporting,
                on_date=contrib_on_date,
                tier=tier,
                provider=provider,
            )
        except fx_helper.FxError as gap:
            return _refused(metric, reporting, _fx_gap_payload(metric, reporting, gap))
        built.append(
            SourceContribution(
                connector=_contribution_label(contrib),
                source_amount=source_amount,
                source_currency=source_currency,
                converted=converted,
                on_date=contrib_on_date,
            )
        )
    built_tuple = tuple(built)

    # --- Step 4: single-source fast path (identity, byte-identical, AC-4) -----
    # Exactly one contribution -> no method combination. The single converted amount is the
    # source amount echoed UNCHANGED when from == reporting (39.4 identity, no ``* 1.0``).
    if len(built_tuple) == 1:
        only = built_tuple[0]
        return ReconciledMoney(
            metric=metric,
            status=SINGLE_SOURCE,
            amount=only.converted.amount,
            reporting_currency=reporting,
            route=None,
            contributions=built_tuple,
        )

    # --- Step 5: route by the client-metric-definition-matrix method (AC-1) ---
    route = _resolve_route(project_id, metric)
    status = _ROUTE_STATUS_TO_39_6.get(route.status, route.status)

    if status == RECONCILED:
        # SUM the NORMALIZED (reporting-currency) values -- never the raw mixed-currency
        # amounts. sums-then-divides: 39.6 sums the already-converted amounts as-is; any
        # micros divide is a single read-time divide owned by the caller (AC-5), 39.6 does
        # not divide per contribution.
        amount = sum(c.converted.amount for c in built_tuple)
        return ReconciledMoney(
            metric=metric,
            status=RECONCILED,
            amount=amount,
            reporting_currency=reporting,
            route=route,
            contributions=built_tuple,
            reason=route.reason,
        )

    # Every other status carries NO in-core figure: the mart owns it (ROUTED_TO_MART,
    # invariant 1) or the 27.3 reason says "do not combine" (KEEP_SEPARATE / unruled /
    # not-combinable / override). Carry the converted per-source contributions as the honest
    # drill-down + the route reason. amount is None.
    return ReconciledMoney(
        metric=metric,
        status=status,
        amount=None,
        reporting_currency=reporting,
        route=route,
        contributions=built_tuple,
        reason=route.reason,
    )


# ---------------------------------------------------------------------------
# Refusal / gap plumbing (fail-closed, typed -- never a fabricated number).
# ---------------------------------------------------------------------------

_UNKNOWN_CURRENCY_CODE = "UNKNOWN_CURRENCY_GAP"
_FX_GAP_CODE = "FX_CONVERSION_GAP"


def _default_refusal_checker(*, metric, contributions, is_monetary, reporting_currency):
    """Default 39.3 guard (imported lazily so the module composes without a hard import)."""
    from core.currency_refusal import check_monetary_aggregation  # noqa: PLC0415

    return check_monetary_aggregation(
        metric=metric,
        contributions=contributions,
        is_monetary=is_monetary,
        reporting_currency=reporting_currency,
    )


def _default_is_monetary(metric: str, project_id: str | None) -> bool:
    """Default 39.1 classifier seam (lazy import, fail-soft to False)."""
    try:
        from core.metric_semantics import is_metric_monetary  # noqa: PLC0415

        return bool(is_metric_monetary(metric, project_id=project_id))
    except Exception as exc:  # noqa: BLE001 -- fail-soft; a non-money answer bypasses money.
        logger.warning(
            "money_reconciliation: is_metric_monetary seam unavailable for %s: %s",
            metric,
            exc,
        )
        return False


def _fx_gap_payload(metric: str, reporting_currency: str, gap: fx_helper.FxError) -> dict:
    """Map a 39.4 MissingFxPair / MissingAsOfRate to a typed FX-gap refusal payload (AC-2).

    Names the pair / date / tier so a surface renders an honest message, mirroring the 39.3
    refusal shape (``code`` + ``metric`` + ``reporting_currency`` + ``message``). NO number."""
    from_ccy = getattr(gap, "from_currency", None)
    to_ccy = getattr(gap, "to_currency", None) or reporting_currency
    on_date = getattr(gap, "on_date", None)
    gap_tier = getattr(gap, "tier", None)
    return {
        "code": _FX_GAP_CODE,
        "metric": metric,
        "reporting_currency": reporting_currency,
        "from_currency": from_ccy,
        "to_currency": to_ccy,
        "on_date": on_date.isoformat() if isinstance(on_date, date) else on_date,
        "tier": gap_tier,
        "kind": type(gap).__name__,
        "resolvable_via": "fx_conversion",
        "message": str(gap),
    }


def _refused(metric: str, reporting_currency: str, refusal: dict) -> ReconciledMoney:
    """Build a REFUSED result (amount is None, typed payload carried). Fail-closed (AC-2)."""
    return ReconciledMoney(
        metric=metric,
        status=REFUSED,
        amount=None,
        reporting_currency=reporting_currency,
        route=None,
        contributions=(),
        refusal=refusal,
    )


# ---------------------------------------------------------------------------
# Contribution provenance label (AD-2: read from the row, never hardcoded).
# ---------------------------------------------------------------------------


def _contribution_label(contrib: dict) -> str:
    """Best available emitting-datastream label for a contribution (AD-2, no hardcode).

    Prefer an explicit ``connector`` (the reconciliation-series vocabulary), then the AD-9
    ``source_system`` / ``datastream_id`` provenance. Returns an empty string when none is
    present -- 39.6 never fabricates a source name."""
    for key in ("connector", "source_system", "datastream_id"):
        val = contrib.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if val not in (None, "", False):
            return str(val)
    return ""
