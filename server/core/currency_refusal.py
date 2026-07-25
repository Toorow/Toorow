"""toorow -- Cross-currency aggregation refusal engine (Story 39.3, Epic 39).

The REFUSAL half of the money contract. Where ``datamodel._detect_conflicts`` emits a
read-time FIELD-LEVEL warning ("these sources MAY disagree on currency"), this module
answers the COMPUTATION-TIME question of AC1: *given a set of monetary contributions each
tagged with a source currency, is summing them legal?* -- and returns a TYPED REFUSAL
(never a silent wrong sum) when it is not.

Two refusal codes:
  * CROSS_CURRENCY_REFUSAL (AC1): >=2 DISTINCT known currencies, no recorded FX conversion.
  * UNKNOWN_CURRENCY_GAP   (AC2): a contribution whose currency is unknown/missing.

Precedence (decided): UNKNOWN_CURRENCY_GAP is checked FIRST. An unknown currency is a
deeper failure than a cross-currency conflict -- you cannot classify a value's
currency-conflict status if you do not know its currency. This makes AC2 fail-closed
unconditionally and prevents a value with unknown currency from being silently excluded so
the remaining values "happen to" agree (which would be a silent wrong sum in disguise).

Design decisions:
  - PURE + STATELESS: no DB, no wall-clock, no randomness. Deterministic sorted output.
  - AD-2: ZERO connector/provider names. Currencies come from row provenance; the ISO
    recognizer is the shared ``_VALID_CURRENCIES`` subset (conflict_resolutions.py) -- no
    second currency list. The provisional money-name proxy in ``_is_monetary_metric`` is
    metric-DICTIONARY vocabulary (config), not provider vocabulary.
  - AD-9: a missing currency is a surfaced GAP, never a default (EUR/USD/anything).
  - 39.1 seam: ``_is_monetary_metric`` is the single choke point for "is this metric
    money?" -- it delegates to Story 39.1's ``is_metric_monetary`` classifier and falls
    back to a documented provisional proxy only if that seam is unavailable.
  - 39.3 never divides or sums a value: ``amount`` may be micros OR display units --
    irrelevant to a refusal, which reasons only over CURRENCIES, not magnitudes.

ASCII-only stdout (AI-03). No private framework attributes (AI-02).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Refusal codes
# ---------------------------------------------------------------------------

REFUSAL_CROSS_CURRENCY = "CROSS_CURRENCY_REFUSAL"   # AC1 -- >=2 distinct currencies, no conversion
REFUSAL_UNKNOWN_CURRENCY = "UNKNOWN_CURRENCY_GAP"   # AC2 -- a contribution with unknown currency

# The lever a refusal is resolved by (Epic 13 binds a source currency; Story 39.4 converts).
_RESOLVABLE_VIA = "fx_conversion"


# ---------------------------------------------------------------------------
# ISO recognizer -- REUSE the shared subset (no second currency list, §A.4)
# ---------------------------------------------------------------------------
# Imported from conflict_resolutions (Epic 13's ISO subset). This is the ONLY currency
# recognizer in the money stack: a code outside it is treated as unknown (fail-closed),
# never silently accepted. Kept as a module-level import (no cycle: conflict_resolutions
# does not import currency_refusal).
from core.conflict_resolutions import _VALID_CURRENCIES  # noqa: E402,PLC0415

# ---------------------------------------------------------------------------
# 39.1 classifier seam -- _is_monetary_metric
# ---------------------------------------------------------------------------


def _is_monetary_metric(metric: str, *, definition: dict | None = None) -> bool:
    """Whether *metric* is monetary. THE single seam onto Story 39.1's classifier.

    39.1 HAS LANDED: this delegates to ``core.metric_semantics.is_metric_monetary`` -- the
    platform-wide single source of truth (E39-FR01), resolved through the
    PROJECT > ORG > PLATFORM cascade when a project is in context (the API route resolves
    ``definition``/monetary itself and passes ``is_monetary`` to the engine; this predicate
    is the offline fallback + the field-detector's classifier).

    Call sites do NOT change if 39.1's surface evolves -- this is the only choke point.

    Provisional proxy (fail-soft): if the 39.1 seam is unavailable (import/DB hiccup), fall
    back to a name-based money check that MIRRORS ``_classify_monetary`` so the engine and
    the field-level warning agree. The proxy list is metric-DICTIONARY vocabulary (money
    nouns), NOT provider names -- AD-2 grep tolerates it exactly as it tolerates the ratio
    name sets. Delete the fallback branch if 39.1's classifier is ever made a hard dep.
    """
    if not metric:
        return False
    # Prefer an explicit stored flag when the caller already resolved the definition.
    if definition is not None:
        flag = definition.get("monetary")
        if flag is not None:
            return bool(flag)
        fmt = definition.get("format")
        if isinstance(fmt, str) and fmt.strip().lower() == "currency":
            return True
    try:
        from core.metric_semantics import is_metric_monetary  # noqa: PLC0415

        return bool(is_metric_monetary(metric))
    except Exception as exc:  # noqa: BLE001 -- fail-soft to the provisional name proxy.
        logger.warning(
            "currency_refusal: is_metric_monetary seam unavailable for %s, using proxy: %s",
            metric, exc,
        )
        return _monetary_name_proxy(metric)


# Provisional money-name proxy -- metric-dictionary vocabulary (config), NOT provider names.
# Mirrors metric_semantics._MONETARY_EXACT_NAMES / _MONETARY_TOKEN_RE so the fallback agrees
# with the platform classifier. To be DELETED once the 39.1 seam is a hard dependency.
_MONEY_EXACT_NAMES = frozenset(
    {"cost", "spend", "revenue", "fee", "refund", "ad_revenue", "all_revenue",
     "conversions_value"}
)
_MONEY_TOKENS = ("_revenue", "_cost", "_spend", "_fee", "_refund", "_amount", "_value")


def _monetary_name_proxy(metric: str) -> bool:
    """Fallback name check when the 39.1 classifier seam is unavailable (see above)."""
    normalized = metric.strip().lower()
    if not normalized:
        return False
    if normalized in _MONEY_EXACT_NAMES:
        return True
    return any(normalized.endswith(tok) for tok in _MONEY_TOKENS)


# ---------------------------------------------------------------------------
# Currency recognizer
# ---------------------------------------------------------------------------


def _normalize_currency(currency: object) -> str | None:
    """Return the upper-cased ISO code if *currency* is a recognized ISO-4217 code, else None.

    Unknown (fail-closed, AD-9) when: None, non-string, empty/whitespace, or not in the
    shared ISO subset. A code outside the known subset is unknown, NOT silently accepted.
    """
    if not isinstance(currency, str):
        return None
    normalized = currency.strip().upper()
    if not normalized:
        return None
    if normalized not in _VALID_CURRENCIES:
        return None
    return normalized


class MoneyAggregationRefused(Exception):
    """Raised when a caller wants a monetary aggregation refusal surfaced as an exception.

    Carries the typed refusal payload (``.refusal``) so callers surface {code, message, ...}
    rather than a naked number. NEVER raised for non-monetary metrics (the guard no-ops).
    The primary API of this module returns the payload as a dict; this exception is provided
    for call sites that prefer raise-based control flow.
    """

    def __init__(self, refusal: dict):
        self.refusal = refusal
        super().__init__(refusal.get("message", "monetary aggregation refused"))


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


def _stream_label(contrib: dict) -> str | None:
    """Best available stream identifier for a contribution (AC1 "datastreams")."""
    for key in ("datastream_id", "source_system"):
        val = contrib.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if val not in (None, "", False):
            return str(val)
    return None


def check_monetary_aggregation(
    *,
    metric: str,
    contributions: list[dict],
    is_monetary: bool,
    reporting_currency: str | None = None,
) -> dict | None:
    """Return a typed refusal dict, or None when the aggregation is legal.

    Legal (returns None):
      * ``is_monetary`` is False                                  -> no money rule applies.
      * ``contributions`` is empty                               -> nothing to sum.
      * every contribution shares ONE known currency             -> single-currency sum, OK.
      * every contribution shares ONE known currency == reporting -> OK (already normalized).

    Refused (returns a typed dict, NEVER a silent sum):
      * ANY contribution has an unknown/missing currency -> UNKNOWN_CURRENCY_GAP (AC2,
        fail-closed, takes PRECEDENCE -- you cannot denominate a value you cannot classify).
      * >=2 DISTINCT known currencies and no recorded conversion -> CROSS_CURRENCY_REFUSAL (AC1).

    See the module docstring for precedence and the refusal payload shape (§A.4).
    """
    # (1) Non-monetary metric: no money rule applies, no refusal.
    if not is_monetary:
        return None

    # (2) Nothing to sum: trivially legal.
    if not contributions:
        return None

    # A single known currency (even if != reporting_currency) is NOT a refusal -- it is simply
    # convertible by Story 39.4; only cross-currency or unknown-currency are refusals here.
    # --- Precedence: UNKNOWN_CURRENCY_GAP checked FIRST (AC2, fail-closed) ---
    unknown_sources: list[dict] = []
    known: set[str] = set()
    streams: set[str] = set()

    for contrib in contributions:
        label = _stream_label(contrib)
        if label is not None:
            streams.add(label)
        code = _normalize_currency(contrib.get("currency"))
        if code is None:
            unknown_sources.append(
                {
                    "source_system": contrib.get("source_system"),
                    "source_field": contrib.get("source_field"),
                    "pull_id": contrib.get("pull_id"),
                }
            )
        else:
            known.add(code)

    offending_streams = sorted(streams)

    if unknown_sources:
        # AC2 -- an unknown currency is the deepest failure; return it before anything else,
        # never a partial cross-currency sum excluding the unknown value.
        first = unknown_sources[0]
        src = first.get("source_system") or "?"
        fld = first.get("source_field") or "?"
        message = (
            f"Ecart de devise : un montant de la metrique '{metric}' arrive sans devise "
            f"connue (source '{src}'/champ '{fld}'). Fail-closed : la valeur n'est ni "
            f"sommee ni convertie tant que sa devise n'est pas declaree."
        )
        return {
            "code": REFUSAL_UNKNOWN_CURRENCY,
            "metric": metric,
            "conflicting_currencies": sorted(known),
            "unknown_currency_sources": unknown_sources,
            "offending_streams": offending_streams,
            "resolvable_via": _RESOLVABLE_VIA,
            "reporting_currency": reporting_currency,
            "message": message,
        }

    # --- All currencies known: cross-currency check (AC1) ---
    distinct = sorted(known)
    if len(distinct) >= 2:
        pair = " et ".join(distinct)
        message = (
            f"Refus d'agregation : la metrique '{metric}' combine des montants en {pair} "
            f"sans conversion explicite. Sommer des devises differentes produirait un total "
            f"faux ('choux et carottes'). Convertissez vers la devise de reporting avant "
            f"d'agreger."
        )
        return {
            "code": REFUSAL_CROSS_CURRENCY,
            "metric": metric,
            "conflicting_currencies": distinct,
            "unknown_currency_sources": [],
            "offending_streams": offending_streams,
            "resolvable_via": _RESOLVABLE_VIA,
            "reporting_currency": reporting_currency,
            "message": message,
        }

    # Exactly one known currency (or zero known + no unknowns, impossible here) -> legal.
    # single-currency == reporting is also legal; single-currency != reporting is STILL legal
    # (a single homogeneous currency needs no conversion to be summed -- only cross-currency
    # or unknown-source-currency refuse). §B.2.
    return None
