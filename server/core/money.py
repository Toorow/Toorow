"""toorow -- the per-source money ADAPTER + canonical-micros read helper (Story 39.2).

The platform's ONE canonical internal money representation is **micros, currency
attached -- for everyone** (E39-AD1, refined by the Amendment 2026-07-23). A connector
does not choose the canonical unit; it *declares* its NATIVE money encoding
(``native_unit in {micros, decimal, cents}``) and the platform ADAPTER normalizes that
native encoding into canonical micros at the capture/staging boundary. The ``/1e6``
division back to display units happens EXACTLY ONCE, at read.

This module is the Python home of the adapter (two faces):

  * ``to_canonical_micros`` -- the ADAPTER NORMALIZATION: INPUT = a value in its declared
    native encoding, OUTPUT = always canonical micros (integer-exact). ``micros`` is a
    no-op (already canonical); ``decimal`` is ``round(value * 1_000_000)``; ``cents`` is
    ``round(value * 10_000)``. Unknown unit ==> typed error (fail-CLOSED, never guess).
  * ``read_units`` -- the READ-TIME DIVISION: the mart value is canonical micros regardless
    of ``native_unit`` (the adapter already normalized it), so read divides by ``1e6``
    exactly once. The caller attaches the currency tag; the helper never invents currency.
    An unknown metric ==> fail-SOFT: treat as already-display, do NOT divide (dividing a
    non-canonical value would fabricate a wrong number).
  * ``native_roundtrip`` -- ``read_once(to_canonical_micros(value, native_unit))``; proves
    AC6 (the decimal round-trip is exact, so the adapter subsumes the incumbent decimal
    path WITHOUT moving its total).
  * ``load_units`` -- parse ``dbt/seeds/money_metric_units.csv`` (the canonical-unit map the
    read layer keys on) with the stdlib ``csv.DictReader`` (mirrors ``core.audit`` /
    ``core.metric_semantics``). Returns ``{canonical_metric -> canonical_native_unit}``.

STATELESS & SOURCE-AGNOSTIC (AD-2): this module contains **ZERO** provider/connector
names. Canonical metric names come from the seed (``money_metric_units.csv``), never from
code. The seed FILE NAME is a dbt artefact, not a provider name.

SCOPE (Story 39.2): UNIT only. This module does **not** convert currency, does not apply
FX, does not name a currency. Currency comparability is 39.3 (refusal) + 39.4 (conversion).
The FX-locus reconciliation is deferred to 39.9. The adapter attaches/preserves the source
currency as an opaque tag owned by the caller; it never converts.

Design mirrors ``server/core/audit.py`` (csv-stdlib parse, stateless module, env read at
call time) and ``server/core/metric_semantics.py`` (pure functions kept separate from I/O
so exactness / round-trip / idempotence are testable offline without a warehouse).

Windows/CI note: all message strings use ASCII-safe characters only.
"""

from __future__ import annotations

import csv
import logging
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The canonical internal unit + the accepted NATIVE encodings.
#
# The platform's canonical internal unit is fixed platform-wide = micros. Only the
# *native input* is declared per source. A native ``decimal`` value scaled by 1_000_000
# and a native ``cents`` value scaled by 10_000 both reach canonical micros exactly (both
# scale factors are integer, so the multiply is exact on an exact-in-DOUBLE magnitude).
# ---------------------------------------------------------------------------

MICROS_PER_UNIT = 1_000_000  # 1 display unit = 1_000_000 micros (canonical)
MICROS_PER_CENT = 10_000  # 1 cent = 1/100 unit = 10_000 micros

NATIVE_MICROS = "micros"
NATIVE_DECIMAL = "decimal"
NATIVE_CENTS = "cents"

# The native encodings the adapter knows how to normalize. Extending this set is an
# additive change (add the token + its scale in _NATIVE_SCALE_TO_MICROS).
ACCEPTED_NATIVE_UNITS = frozenset({NATIVE_MICROS, NATIVE_DECIMAL, NATIVE_CENTS})

# Seed file name (dbt artefact -- NOT a provider name, AD-2).
MONEY_METRIC_UNITS_SEED = "money_metric_units.csv"


# ---------------------------------------------------------------------------
# Typed error (callers/endpoints map this; core never raises HTTP here). Mirrors the
# MetricSemanticsError pattern in metric_semantics.py.
# ---------------------------------------------------------------------------


class MoneyAdapterError(Exception):
    """Raised when the adapter is asked to normalize an unknown native encoding.

    Fail-CLOSED: the adapter never guesses a scale for an undeclared unit -- guessing
    would silently emit a wrong-magnitude canonical value. The caller must declare the
    native_unit (in the connector's source_capabilities + the seed) first.
    """


# ---------------------------------------------------------------------------
# The ADAPTER: native encoding -> canonical micros (INPUT -> OUTPUT). PURE.
# ---------------------------------------------------------------------------


def to_canonical_micros(value: Decimal | int | str | float, native_unit: str) -> int:
    """Normalize a value in its declared *native_unit* to canonical micros (PURE).

    The adapter's OUTPUT is ALWAYS canonical micros (an integer), regardless of the input
    encoding:

      * ``micros``  -> the value itself           (already canonical; a no-op scale of 1)
      * ``decimal`` -> ``value * 1_000_000``      (1 display unit = 1_000_000 micros)
      * ``cents``   -> ``value * 10_000``         (1 cent = 10_000 micros)

    Story 48.3 made this arithmetic EXACT. It used to be ``round(value * scale)`` on a
    ``float``: for the magnitudes marketing spend actually reaches, a binary float cannot
    hold the input, so the "exact integer micro count" the previous docstring promised was
    off by a micro or two on values that had been rounded on the way in. A ``str`` or
    ``Decimal`` input is now converted with no binary step at all, and a ``float`` input is
    converted through its shortest exact decimal representation -- which is the number the
    provider actually sent, rather than the binary approximation Python parsed it into.

    Rounding is HALF-EVEN at the micro, stated rather than inherited: half-up would bias
    every tie in the same direction, and a mart summing millions of rows accumulates that
    bias into a visible number.

    Unknown ``native_unit`` raises ``MoneyAdapterError`` (fail-closed).

    Returns:
        int: the value expressed in canonical micros.
    """
    scale = _NATIVE_SCALE_TO_MICROS.get(native_unit)
    if scale is None:
        raise MoneyAdapterError(
            f"unknown native_unit {native_unit!r}; "
            f"accepted: {sorted(ACCEPTED_NATIVE_UNITS)} (fail-closed, never guessed)"
        )
    return int((exact_decimal(value) * scale).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def exact_decimal(value: Decimal | int | str | float) -> Decimal:
    """The exact decimal a caller meant, whatever type carried it here (PURE).

    ``Decimal(0.1)`` is ``0.1000000000000000055511151231257827...`` -- the binary double,
    faithfully. ``Decimal(repr(0.1))`` is ``0.1``. When a provider sent ``0.1`` and a JSON
    parser turned it into a double, the second is the number that was sent, so ``repr`` is
    the correct bridge and not a rounding shortcut.

    A non-finite value is a typed refusal: ``NaN`` and ``Infinity`` are not amounts, and
    letting either through produces a total that is silently meaningless.
    """
    if isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    elif isinstance(value, float):
        candidate = Decimal(repr(value))
    else:
        try:
            candidate = Decimal(str(value).strip())
        except InvalidOperation as exc:
            raise MoneyAdapterError(f"not a decimal amount: {value!r}") from exc
    if not candidate.is_finite():
        raise MoneyAdapterError(f"not a finite amount: {value!r}")
    return candidate


def convert_micros(
    native_micros: int,
    rate: Decimal | str,
    *,
    minor_unit: int,
    rounding: str = ROUND_HALF_EVEN,
) -> int:
    """Convert canonical micros to canonical micros in another currency (PURE, EXACT).

    The one conversion arithmetic in the platform. Two decisions are load-bearing:

    * **The product is computed in full precision, then rounded ONCE**, to the reporting
      currency's own minor unit expressed in micros. Rounding to micros and again to the
      display unit compounds two errors; rounding to the minor unit directly means a JPY
      total lands on a whole yen and a BHD total on a fils, which is what those currencies
      mean by "an amount".
    * **``minor_unit`` is required.** There is no default of 2. A currency whose ISO minor
      unit is unknown never reaches here -- the Money Policy profile refuses to publish
      with one, precisely so this function can never guess a precision.
    """
    if not isinstance(native_micros, int) or isinstance(native_micros, bool):
        raise MoneyAdapterError("convert_micros takes canonical micros as an integer")
    if minor_unit < 0:
        raise MoneyAdapterError("minor_unit cannot be negative")
    exact_rate = exact_decimal(rate)
    if exact_rate <= 0:
        raise MoneyAdapterError(f"an FX rate must be positive, got {rate!r}")
    # The rounding step in MICROS: a 2-decimal currency rounds to 10_000 micros, a
    # 0-decimal one to 1_000_000, a 3-decimal one to 1_000.
    step = Decimal(MICROS_PER_UNIT).scaleb(-minor_unit)
    product = Decimal(native_micros) * exact_rate
    return int((product / step).quantize(Decimal(1), rounding=rounding) * step)


def micros_to_decimal(canonical_micros: int, *, minor_unit: int) -> Decimal:
    """Canonical micros as an exact display amount at the currency's own precision (PURE).

    The exact counterpart of :func:`read_once`, which returns a ``float`` and therefore
    cannot be the authoritative read for a value someone will add up.
    """
    return (Decimal(canonical_micros) / MICROS_PER_UNIT).quantize(
        Decimal(1).scaleb(-minor_unit), rounding=ROUND_HALF_EVEN
    )


# The integer scale each native unit multiplies by to reach canonical micros. Keyed off
# ACCEPTED_NATIVE_UNITS -- adding a unit is additive (add token here + to the frozenset).
_NATIVE_SCALE_TO_MICROS: dict[str, int] = {
    NATIVE_MICROS: 1,
    NATIVE_DECIMAL: MICROS_PER_UNIT,
    NATIVE_CENTS: MICROS_PER_CENT,
}


def read_once(canonical_micros: float) -> float:
    """Divide a canonical-micros magnitude by 1e6 EXACTLY ONCE -> display units (PURE).

    The single canonical read-time division. The input MUST already be canonical micros
    (the adapter guaranteed that at staging). Caller attaches the currency tag afterwards;
    this helper never invents currency.
    """
    return canonical_micros / MICROS_PER_UNIT


# ---------------------------------------------------------------------------
# The READ helper: seed-driven, divide-once IFF the metric is canonical micros. PURE.
# ---------------------------------------------------------------------------


def read_units(value: float, canonical_metric: str, units_map: dict[str, str]) -> float:
    """Return the DISPLAY-unit value for *canonical_metric* from the mart *value* (PURE).

    Divide once (/1e6) IFF the seed declares the metric ``native_unit == 'micros'`` -- i.e. the
    mart actually holds canonical micros for it. This MATCHES the SQL read layer exactly
    (semantic_roas/semantic_cpa divide only on ``is_canonical_micros``). Semantics keyed on
    ``units_map`` (from ``load_units``):

      * metric declared ``micros``   -> the mart holds canonical micros -> divide once (/1e6).
      * metric declared ``decimal``  -> the mart already holds display units (the incumbent
        AD-6 staging-normalized value, adapter not yet adopted) -> do NOT divide. Dividing it
        would fabricate a wrong number (the 39.4 footgun this gate closes).
      * metric ABSENT from the map    -> fail-SOFT: not governed money -> do NOT divide.

    When a connector adopts the adapter and lands canonical micros into the mart for a metric,
    flip that metric's seed row to ``micros`` and the divide activates here and in the SQL views
    together. The caller attaches the currency tag; this helper never invents currency (AD-2).
    """
    if units_map.get(canonical_metric) == "micros":
        return read_once(value)
    return value


def native_roundtrip(value: float, native_unit: str) -> float:
    """``read_once(to_canonical_micros(value, native_unit))`` -- the adapter round-trip (PURE).

    Proves AC6: for ``native_unit='decimal'`` (the incumbent encoding), normalizing to
    canonical micros then dividing back by 1e6 returns the EXACT original decimal value, so
    the adapter can subsume the incumbent decimal path WITHOUT moving its total (the
    E39-NFR06 guarantee, at the unit level). Unknown unit raises ``MoneyAdapterError``.
    """
    return read_once(to_canonical_micros(value, native_unit))


# ---------------------------------------------------------------------------
# Seed loader (stdlib csv, offline). Mirrors metric_semantics._read_seed_rows.
# ---------------------------------------------------------------------------


def _default_seeds_dir() -> Path:
    """Locate dbt/seeds relative to this file (server/core/money.py).

    server/core/money.py -> repo root is three parents up; the seeds live at
    <root>/dbt/seeds. Tests override via the ``seeds_dir`` argument (fixtures).
    """
    return Path(__file__).resolve().parents[2] / "dbt" / "seeds"


def load_units(seeds_dir: str | Path | None = None) -> dict[str, str]:
    """Parse ``money_metric_units.csv`` into ``{canonical_metric -> canonical_native_unit}``.

    PURE / offline (no DB, no dbt): stdlib ``csv.DictReader``, the pattern in
    ``core.audit`` / ``core.metric_semantics``. ``canonical_native_unit`` is the native
    unit the platform expects that canonical metric to ARRIVE in before the adapter
    normalizes it (the adapter OUTPUT is always micros; this column records the INPUT the
    adapter must handle, so the read layer knows whether a ``/1e6`` is owed).

    A blank/comment-empty row (no ``canonical_metric``) is skipped defensively. A duplicate
    canonical_metric is a seed error caught by the seed's grain-unique dbt test; here the
    last row wins (deterministic).
    """
    base = Path(seeds_dir) if seeds_dir is not None else _default_seeds_dir()
    path = base / MONEY_METRIC_UNITS_SEED
    units: dict[str, str] = {}
    with path.open("r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            metric = (row.get("canonical_metric") or "").strip()
            unit = (row.get("canonical_native_unit") or "").strip()
            if not metric:
                continue
            units[metric] = unit
    return units
