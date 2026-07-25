"""Story 39.4: FX conversion helper -- fixed rate + as-of-day (historical).

The FX conversion engine of Epic 39 (E39-FR05): a stateless core helper that
converts a monetary amount from its source currency to a project's reporting
currency at either

  * a FIXED rate read from the static ``dbt/seeds/fx_rates.csv`` seed
    (deterministic, always available), or
  * an AS-OF-DAY / historical rate -- the rate effective on the FIGURE's own
    date, not today's -- read from a rate-provider behind a small Protocol that
    Story 39.5 later plugs a live rate feed into WITHOUT changing any caller
    (E39-AD4, E39-NFR07).

Every converted value carries FX provenance (rate, as-of date, source, tier)
and extends -- never forks -- the AD-9 provenance chain. A conversion that
cannot be resolved (missing pair, missing as-of rate) is surfaced as a TYPED
gap, never silently approximated (E39-NFR02). Source amount + source currency
are immutable inputs; changing a project's reporting currency RE-DERIVES the
converted value without mutating any source amount (E39-AD2).

Scope: this module is the HELPER + fixed + as-of-day interface only. It does
NOT call a live network service (that is Story 39.5). The historical tier reads
a seed/mock table behind a Protocol; the live feed is plugged in later.

House style mirrors ``server/core/metric_semantics.py`` (Story 27.1):
``from __future__ import annotations``, module logger, pure functions split
from I/O, lazy ``core.db`` import inside DB functions, typed errors, ASCII-only
strings, and ZERO connector/provider vocabulary (AD-2 / E39-NFR05). The only
source label baked here is ``"seed"``; a live service label arrives via the
39.5 provider instance, never from this file.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# FR4 / migration-008 default (app.project_preferences.canonical_currency DEFAULT
# 'EUR'). This is the documented default, NOT a policy hardcode: the real value
# is read per project by resolve_reporting_currency (E39-NFR04).
DEFAULT_REPORTING_CURRENCY = "EUR"

# The dbt seed artefact the fixed tier reads (AD-2: a filename, not a provider).
FX_RATES_SEED = "fx_rates.csv"

# Tier labels (E39-AD4: two tiers behind one interface).
TIER_FIXED = "fixed"
TIER_HISTORICAL = "historical"

# The only source provenance literal in 39.4. Story 39.5 providers supply their
# own service label (AD-2 / E39-NFR05); it is NEVER baked into this core module.
SOURCE_SEED = "seed"

# Same-currency identity policy label (mirrors the seed's ``identity`` policy).
POLICY_IDENTITY = "identity"


# ---------------------------------------------------------------------------
# Typed fail-closed gaps (callers/resolvers map these; core never raises HTTP).
# Story 39.3's refusal resolver and Story 39.6's reconciliation catch these and
# turn them into typed refusals. No silent default, no 1.0 fallback (E39-NFR02).
# ---------------------------------------------------------------------------


class FxError(Exception):
    """Base for FX conversion gaps (fail-closed, never approximated)."""


class MissingFxPair(FxError):
    """No fixed-seed rate for the requested pair inside the figure's date window.

    Carries the pair + date + tier so a surface can render an honest message.
    The identity path (from == to) is NOT a gap and never raises this.
    """

    def __init__(self, from_currency: str, to_currency: str, on_date: date | None, tier: str):
        self.from_currency = from_currency
        self.to_currency = to_currency
        self.on_date = on_date
        self.tier = tier
        super().__init__(
            f"no fixed FX rate for pair {from_currency}->{to_currency} "
            f"(on_date={on_date}, tier={tier})"
        )


class MissingAsOfRate(FxError):
    """The as-of provider has no rate at/before the figure's date for the pair.

    Fail-closed: an as-of lookup that finds no rate on or before ``on_date`` is
    an honest gap, never a fabricated amount (E39-NFR02).
    """

    def __init__(self, from_currency: str, to_currency: str, on_date: date | None, tier: str):
        self.from_currency = from_currency
        self.to_currency = to_currency
        self.on_date = on_date
        self.tier = tier
        super().__init__(
            f"no as-of FX rate at/before {on_date} for pair "
            f"{from_currency}->{to_currency} (tier={tier})"
        )


# ---------------------------------------------------------------------------
# Dataclasses (frozen, provenance-carrying).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FxSeedRow:
    """One row of dbt/seeds/fx_rates.csv (all 7 columns, typed)."""

    from_currency: str
    to_currency: str
    rate: float
    rate_date: date
    rate_policy: str
    valid_from: date
    valid_to: date


@dataclass(frozen=True, slots=True)
class RateQuote:
    """A resolved FX rate with its provenance surface.

    ``source`` is ``"seed"`` for the fixed/seed tiers in 39.4; a live provider
    (39.5) supplies its own service label. ``tier`` is ``fixed`` | ``historical``.
    """

    rate: float
    source: str
    tier: str
    as_of: date
    policy: str | None = None


@dataclass(frozen=True, slots=True)
class ConvertedAmount:
    """The result of a conversion: converted value + immutable inputs + FX block.

    ``source_amount`` / ``source_currency`` are the IMMUTABLE inputs echoed back
    (E39-AD2): conversion is a derivation, so a reporting-currency change
    re-derives ``amount`` while these stay byte-identical. ``fx`` is the
    provenance block ``{rate, as_of_date, source, tier}`` (AC3 / AD-9).
    """

    amount: float
    reporting_currency: str
    source_amount: float
    source_currency: str
    fx: dict = field(default_factory=dict)

    def as_ad9_provenance(self, base_provenance: dict | None = None) -> dict:
        """Return an AD-9-shaped dict that EXTENDS the figure provenance chain.

        AD-9 carries ``(source_system, source_field, pull_id)`` + confidence per
        figure. A converted figure augments -- never replaces -- that chain with
        an ``fx`` sub-object, so it drills down to HOW it was converted
        (E39-NFR03). This helper owns ONLY the ``fx`` sub-object; the base
        provenance belongs to the row it converts and is passed in by the caller.

        The caller-supplied ``base_provenance`` is not mutated; a shallow copy is
        returned with ``fx`` merged in.
        """
        merged = dict(base_provenance) if base_provenance else {}
        merged["fx"] = dict(self.fx)
        return merged


# ---------------------------------------------------------------------------
# Seed parsing (pure, no dbt/duckdb -- mirror metric_semantics._read_seed_rows).
# ---------------------------------------------------------------------------


def _default_seeds_dir() -> Path:
    """Locate dbt/seeds relative to this file (server/core/fx_helper.py).

    server/core/fx_helper.py -> repo root is three parents up; the seeds live at
    <root>/dbt/seeds. Tests override via the seeds_dir argument (fixtures).
    """
    return Path(__file__).resolve().parents[2] / "dbt" / "seeds"


def _parse_iso_date(value: str) -> date:
    """Parse an ISO-8601 (YYYY-MM-DD) date cell from a seed row."""
    return date.fromisoformat(value.strip())


def parse_fx_seed(seeds_dir: str | Path | None = None) -> list[FxSeedRow]:
    """Parse dbt/seeds/fx_rates.csv into typed FxSeedRow records (PURE, no DB).

    Reads with the stdlib ``csv.DictReader`` (no dbt/duckdb). Each of the 7
    columns is typed: ``rate`` as ``float``, the three date columns as ``date``.
    Rows whose ``from_currency`` is blank (e.g. a trailing newline) are skipped.
    """
    base = Path(seeds_dir) if seeds_dir is not None else _default_seeds_dir()
    path = base / FX_RATES_SEED
    rows: list[FxSeedRow] = []
    with path.open("r", newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            from_ccy = (raw.get("from_currency") or "").strip()
            to_ccy = (raw.get("to_currency") or "").strip()
            if not from_ccy or not to_ccy:
                continue
            rows.append(
                FxSeedRow(
                    from_currency=from_ccy,
                    to_currency=to_ccy,
                    rate=float((raw.get("rate") or "").strip()),
                    rate_date=_parse_iso_date(raw.get("rate_date") or ""),
                    rate_policy=(raw.get("rate_policy") or "").strip(),
                    valid_from=_parse_iso_date(raw.get("valid_from") or ""),
                    valid_to=_parse_iso_date(raw.get("valid_to") or ""),
                )
            )
    return rows


# ---------------------------------------------------------------------------
# Providers (one interface, two tiers -- the E39-AD4 seam).
# ---------------------------------------------------------------------------


@runtime_checkable
class RateProvider(Protocol):
    """Structural interface every rate tier satisfies (fixed + historical).

    ``convert()`` accepts any object with this shape, which is what makes the
    39.5 live feed a drop-in: a new provider satisfies this Protocol by shape,
    so no caller of ``convert()`` changes (E39-AD4).
    """

    def get_rate(
        self, from_ccy: str, to_ccy: str, on_date: date | None
    ) -> RateQuote | None: ...


class FixedRateProvider:
    """The fixed / seed tier: deterministic rates from dbt/seeds/fx_rates.csv.

    Rate selection mirrors the existing staging join
    (``stg_meta_ads_daily.sql`` line 106): the row where
    ``from_currency==from_ccy AND to_currency==to_ccy AND
    valid_from <= on_date <= valid_to``. Same-currency (``from == to``)
    short-circuits to an exact identity quote (rate 1.0) even with no explicit
    ``X,X`` seed row (AC7). Returns ``None`` when the pair is absent, which
    drives the typed gap in ``convert()``.

    Determinism: pure over its inputs; no clock read. When ``on_date`` is None,
    the seed's open-ended window (2020..2099) still matches, so the fixed tier is
    effectively date-agnostic (window selection only) -- documented in convert().
    """

    def __init__(self, seeds_dir: str | Path | None = None):
        self._rows = parse_fx_seed(seeds_dir)

    def get_rate(
        self, from_ccy: str, to_ccy: str, on_date: date | None
    ) -> RateQuote | None:
        # Identity: same-currency is an exact pass-through, independent of any
        # seed row (AC7 / E39-NFR01).
        if from_ccy == to_ccy:
            return RateQuote(
                rate=1.0,
                source=SOURCE_SEED,
                tier=TIER_FIXED,
                as_of=on_date if on_date is not None else date.today(),
                policy=POLICY_IDENTITY,
            )
        for row in self._rows:
            if row.from_currency != from_ccy or row.to_currency != to_ccy:
                continue
            # Window selection mirrors "date BETWEEN valid_from AND valid_to".
            # A None figure date matches the seed's open-ended window: the fixed
            # tier is date-agnostic beyond window containment.
            if on_date is not None and not (row.valid_from <= on_date <= row.valid_to):
                continue
            return RateQuote(
                rate=row.rate,
                source=SOURCE_SEED,
                tier=TIER_FIXED,
                as_of=row.rate_date,
                policy=row.rate_policy,
            )
        return None


@runtime_checkable
class AsOfRateProvider(Protocol):
    """Historical-tier plug point -- SAME signature as the fixed provider.

    This is the seam Story 39.5 implements against the live feed: 39.5 supplies a
    ``FrankfurterAsOfProvider`` satisfying this Protocol by shape, so
    ``convert(..., provider=FrankfurterAsOfProvider())`` works unchanged and no
    ``convert()`` caller changes (E39-AD4). Do NOT import the live provider here.
    """

    def get_rate(
        self, from_ccy: str, to_ccy: str, on_date: date | None
    ) -> RateQuote | None: ...


@dataclass(frozen=True, slots=True)
class _AsOfRow:
    from_currency: str
    to_currency: str
    rate: float
    rate_date: date


class SeedAsOfRateProvider:
    """A seed/mock as-of implementation of the AsOfRateProvider Protocol (no network).

    As-of semantics: for a pair, return the rate whose date is the LATEST date
    ``<= on_date`` (the rate effective ON the figure's own date), or ``None`` if
    the pair has no rate at/before that date (drives MissingAsOfRate). This is a
    story-local mock table, NOT the live feed and NOT
    ``seed_fx_conflict_resolutions.py``.

    Accepts either an iterable of ``(from_ccy, to_ccy, rate, rate_date)`` tuples
    (``rate_date`` as ``date`` or ISO string) or a CSV path with the header
    ``from_currency,to_currency,rate,rate_date``.
    """

    def __init__(self, rates: str | Path | list):
        if isinstance(rates, (str, Path)):
            self._rows = self._load_csv(Path(rates))
        else:
            self._rows = [self._coerce(r) for r in rates]

    @staticmethod
    def _coerce(row: object) -> _AsOfRow:
        from_ccy, to_ccy, rate, rate_date = row  # type: ignore[misc]
        if not isinstance(rate_date, date):
            rate_date = date.fromisoformat(str(rate_date).strip())
        return _AsOfRow(
            from_currency=str(from_ccy).strip(),
            to_currency=str(to_ccy).strip(),
            rate=float(rate),
            rate_date=rate_date,
        )

    @staticmethod
    def _load_csv(path: Path) -> list[_AsOfRow]:
        loaded: list[_AsOfRow] = []
        with path.open("r", newline="", encoding="utf-8") as handle:
            for raw in csv.DictReader(handle):
                from_ccy = (raw.get("from_currency") or "").strip()
                to_ccy = (raw.get("to_currency") or "").strip()
                if not from_ccy or not to_ccy:
                    continue
                loaded.append(
                    _AsOfRow(
                        from_currency=from_ccy,
                        to_currency=to_ccy,
                        rate=float((raw.get("rate") or "").strip()),
                        rate_date=date.fromisoformat((raw.get("rate_date") or "").strip()),
                    )
                )
        return loaded

    def get_rate(
        self, from_ccy: str, to_ccy: str, on_date: date | None
    ) -> RateQuote | None:
        # Identity short-circuit stays exact even on the historical tier (AC7).
        if from_ccy == to_ccy:
            return RateQuote(
                rate=1.0,
                source=SOURCE_SEED,
                tier=TIER_HISTORICAL,
                as_of=on_date if on_date is not None else date.today(),
                policy=POLICY_IDENTITY,
            )
        if on_date is None:
            # An as-of lookup without a figure date is meaningless; convert()
            # guards this, but the provider is defensive too.
            return None
        best: _AsOfRow | None = None
        for row in self._rows:
            if row.from_currency != from_ccy or row.to_currency != to_ccy:
                continue
            if row.rate_date > on_date:
                continue
            # Latest date <= on_date wins (as-of semantics).
            if best is None or row.rate_date > best.rate_date:
                best = row
        if best is None:
            return None
        return RateQuote(
            rate=best.rate,
            source=SOURCE_SEED,
            tier=TIER_HISTORICAL,
            as_of=best.rate_date,
            policy=None,
        )


# ---------------------------------------------------------------------------
# convert() -- the single entry point (E39-AD4: one interface, two tiers).
# ---------------------------------------------------------------------------


def convert(
    amount: float,
    from_currency: str,
    *,
    reporting_currency: str,
    on_date: date | None = None,
    tier: str = TIER_FIXED,
    provider: RateProvider | None = None,
) -> ConvertedAmount:
    """Convert *amount* from *from_currency* to *reporting_currency*.

    tier="fixed"      -> FixedRateProvider (seed), deterministic (AC1, E39-AD4).
                         ``on_date`` is optional; the seed's open-ended window
                         matches any date, so fixed is effectively date-agnostic
                         (window containment only).
    tier="historical" -> the supplied AsOfRateProvider (required), rate effective
                         ON *on_date* -- the figure's own date, not today's (AC2).
                         ``on_date`` is REQUIRED (ValueError if omitted -- you
                         cannot ask for an as-of rate without a date).

    Identity (from == reporting) -> rate 1.0, ``amount`` returned UNCHANGED (AC7,
    exact -- not ``amount * 1.0`` re-rounded; no float drift on the identity path).

    Missing pair / missing as-of rate -> MissingFxPair / MissingAsOfRate (AC3,
    fail-closed: NO 1.0 fallback, NO fabricated amount, E39-NFR02).

    Source amount + source currency are IMMUTABLE inputs echoed on the result
    (E39-AD2, AC4): a reporting-currency change re-derives ``amount`` while the
    source fields stay byte-identical.
    """
    # Exact identity pass-through, independent of any provider or seed (AC7).
    if from_currency == reporting_currency:
        return ConvertedAmount(
            amount=amount,
            reporting_currency=reporting_currency,
            source_amount=amount,
            source_currency=from_currency,
            fx={
                "rate": 1.0,
                "as_of_date": on_date.isoformat() if on_date is not None else None,
                "source": SOURCE_SEED,
                "tier": tier,
            },
        )

    if tier == TIER_FIXED:
        active_provider: RateProvider = provider if provider is not None else FixedRateProvider()
        quote = active_provider.get_rate(from_currency, reporting_currency, on_date)
        if quote is None:
            raise MissingFxPair(from_currency, reporting_currency, on_date, tier)
    elif tier == TIER_HISTORICAL:
        if provider is None:
            raise ValueError(
                "tier='historical' requires an AsOfRateProvider via provider=..."
            )
        if on_date is None:
            raise ValueError(
                "tier='historical' requires on_date (the figure's own date)"
            )
        quote = provider.get_rate(from_currency, reporting_currency, on_date)
        if quote is None:
            raise MissingAsOfRate(from_currency, reporting_currency, on_date, tier)
    else:
        raise ValueError(f"unknown tier: {tier!r} (expected 'fixed' or 'historical')")

    # Derivation: source amount * rate. The identity path above never reaches
    # here, so no exact-1.0 re-rounding concern remains.
    converted = amount * quote.rate
    return ConvertedAmount(
        amount=converted,
        reporting_currency=reporting_currency,
        source_amount=amount,
        source_currency=from_currency,
        fx={
            "rate": quote.rate,
            "as_of_date": quote.as_of.isoformat(),
            "source": quote.source,
            "tier": quote.tier,
        },
    )


# ---------------------------------------------------------------------------
# Reporting-currency resolver (E39-FR07, E39-NFR04) -- the canonical_currency
# <-> reporting_currency naming bridge. Column is NOT renamed (additive).
# ---------------------------------------------------------------------------


def resolve_reporting_currency(project_id: str) -> str:
    """Return the project's reporting currency from app.project_preferences.

    Reads ``canonical_currency`` for *project_id* (the platform column is
    historically ``canonical_currency``; the epic calls it "reporting / target
    currency" -- SAME concept). 39.4 does NOT rename the column (renaming would
    ripple into dbt / mirror / the CSV fallback -- out of scope); it bridges the
    two names HERE.

    Fail-soft to ``DEFAULT_REPORTING_CURRENCY`` ("EUR", the migration-008
    default) when the project row is absent OR the lookup fails -- never crash,
    never invent a non-default (mirrors metric_semantics._project_org_id's
    fail-to-default posture, E39-NFR04).
    """
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT canonical_currency FROM app.project_preferences "
                    "WHERE project_id = %s",
                    (project_id,),
                )
                row = cur.fetchone()
        if row and row[0]:
            return str(row[0])
        return DEFAULT_REPORTING_CURRENCY
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "fx_helper: reporting currency lookup failed project=%s: %s",
            project_id,
            exc,
        )
        return DEFAULT_REPORTING_CURRENCY
