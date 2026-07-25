"""toorow -- Shared reshape primitive: deterministic daily spread (Story 22.9).

Extracted verbatim (no behaviour change) from ``core.mediaplan_store`` so that
ONE cent-exact, sum-preserving daily-spread engine serves both the media-plan
import (Story 22.1) and the file-source ingestion producer (Epic 22 Phase B,
AD-5). A ``.py`` adaptation CALLS this vetted primitive; it never reinvents the
spread.

The library is self-contained: it defines its own typed validation error and
cent quantum and imports nothing from the media-plan domain (one-way dependency:
``mediaplan_store`` consumes ``reshape``, never the reverse). ``mediaplan_store``
re-raises :class:`ReshapeValidationError` as its own ``MediaPlanValidationError``
so the API contract (typed 4xx) is preserved unchanged.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

# One cent as an exact Decimal quantum. Single source of truth for the whole
# reshape engine (and, by re-export, for the media-plan domain).
CENT = Decimal("0.01")


class ReshapeError(ValueError):
    """Base class for typed reshape errors (carries a stable code)."""

    code = "reshape_error"


class ReshapeValidationError(ReshapeError):
    """A caller-supplied value is invalid (the domain maps it to 422)."""

    code = "invalid_param"


def compute_spread(
    budget: Decimal, start_date: date, end_date: date
) -> list[tuple[date, Decimal]]:
    """Spread ``budget`` linearly across [start_date, end_date] at the cent.

    Deterministic largest-remainder distribution:
      * work in integer CENTS to avoid any float / fractional cent;
      * base = floor(budget_cents / n_days) cents per day;
      * remainder = budget_cents - base * n_days cents, handed out +1 cent to the
        first ``remainder`` days in CHRONOLOGICAL order.

    Guarantees:
      * SUM of returned amounts == budget EXACTLY (no cent created or lost);
      * two identical calls return byte-identical results (deterministic);
      * handles 1 day, multi-month ranges, and leap years (29 Feb) uniformly;
      * Decimal throughout -- never float.

    Raises ReshapeValidationError on non-Decimal / non-finite / negative budget
    or start_date > end_date.
    """
    if not isinstance(budget, Decimal):
        # Defensive: refuse silent float coercion (would break cent-exactness).
        raise ReshapeValidationError("Le budget doit être un Decimal.")
    if not budget.is_finite():
        # NaN/Infinity compare False to 0 and would blow up in quantize/divmod.
        raise ReshapeValidationError("Le budget doit être un nombre fini.")
    if budget < 0:
        raise ReshapeValidationError("Le budget ne peut pas être négatif.")
    if start_date > end_date:
        raise ReshapeValidationError(
            "La date de début doit précéder la date de fin."
        )

    n_days = (end_date - start_date).days + 1

    # Convert budget to an exact integer number of cents.
    budget_cents_dec = (budget / CENT).to_integral_value()
    budget_cents = int(budget_cents_dec)

    base, remainder = divmod(budget_cents, n_days)

    out: list[tuple[date, Decimal]] = []
    for i in range(n_days):
        cents = base + (1 if i < remainder else 0)
        day = start_date + timedelta(days=i)
        out.append((day, Decimal(cents) * CENT))
    return out
