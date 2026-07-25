"""Story 39.5: live as-of-day FX provider (Frankfurter / ECB) behind the 39.4 seam.

The live-tier implementation of Epic 39 (E39-FR06): a ``FrankfurterAsOfRateProvider``
that satisfies the ``AsOfRateProvider`` structural Protocol shipped by Story 39.4
(``server/core/fx_helper.py``) and plugs into
``convert(..., tier="historical", provider=...)`` WITHOUT changing ``convert()`` or
any of its callers (E39-AD4). It fetches EUR-based daily rates from the free ECB
feed (``api.frankfurter.dev`` -- no key, no quota, as-of-day historical since 1999),
with three robustness rules:

1. **Carry-forward over non-publishing days (FEED-DRIVEN).** ECB publishes on TARGET
   business days only. The provider does NOT hard-code a holiday calendar: it sends
   the requested date, and Frankfurter echoes the effective publishing day at/before
   it in ``payload["date"]``. When that echoed date is earlier than the requested one
   the provider records the carry-forward as provenance (``RateQuote.as_of`` = the
   effective date; ``policy = "carry_forward"``) -- honest, never a silent backfill.
2. **EUR-triangulation for cross-rates.** The ECB base is EUR, so ``rates[X]`` is
   EUR->X. A non-EUR->non-EUR pair (USD->GBP) is computed as (EUR->GBP)/(EUR->USD).
3. **Fail-closed on a truly uncovered pair.** A currency outside ECB's ~30 majors
   (a missing triangulation leg) or an exhausted-retry fetch makes the provider
   return ``None`` -- 39.4's ``convert()`` then raises ``MissingAsOfRate`` (a typed
   gap). NEVER a silent ``1.0``, NEVER a fabricated amount (E39-NFR02).

House style mirrors ``server/core/fx_helper.py`` (39.4) and ``server/core/nango_client.py``
(httpx as the house HTTP library, ``_DEFAULT_TIMEOUT`` module constant, env-read-at-call-
time base URL, a typed error class): ``from __future__ import annotations``, module logger,
pure math split from I/O, ASCII-only strings.

AD-2 nuance (E39-NFR05): the generic seam ``fx_helper.py`` stays Frankfurter-free (this
module never imports back into it beyond the Protocol symbols). Naming the feed HERE is
the module's purpose (this IS the provider, exactly as ``server/modules/*/connector.py``
name their provider). The ``RateQuote.source`` label is the standard-body ``"ecb"`` (the
rates ARE ECB reference rates; Frankfurter is transport, and a self-host mirror could
serve the same data) -- not a vendor lock (MEMORY: reference-links-are-direction).
"""

from __future__ import annotations

import logging
import os
from datetime import date

import httpx

from core.fx_helper import (  # the ONLY integration point (39.4, committed)
    POLICY_IDENTITY,
    TIER_HISTORICAL,
    RateQuote,
)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Module constants (defined HERE, never in the generic fx_helper.py seam).
# --------------------------------------------------------------------------- #

# The free ECB reference feed (AD-2: a standard-body feed, not a vendor lock).
FRANKFURTER_BASE_URL = "https://api.frankfurter.dev"
# Override at call time so the live pass / a self-host mirror can repoint the feed
# without a code change (E39-NFR04; mirror nango_client._nango_base_url).
FX_FEED_BASE_URL_ENV = "FX_FEED_BASE_URL"

# Bounded timeout per call (mirror nango_client._DEFAULT_TIMEOUT).
_DEFAULT_FX_TIMEOUT = 10.0
# Bounded retry on transient failures (5xx / network / timeout).
_MAX_ATTEMPTS = 3

# Standard-body source label stamped on RateQuote.source (AD-2 -- NOT a vendor name).
SOURCE_ECB = "ecb"
# Provenance marker recorded when the feed echoed an earlier publishing day (AC2).
POLICY_CARRY_FORWARD = "carry_forward"

# The ECB base currency: rates[X] means EUR->X.
_EUR = "EUR"


class FxFeedUnavailable(Exception):
    """Transient feed failure after bounded retries.

    The provider maps this to ``None`` (fail-closed via ``convert()`` ->
    ``MissingAsOfRate``); it NEVER yields a fabricated rate (E39-NFR02).
    """


# --------------------------------------------------------------------------- #
# HTTP surface -- the injectable fetcher (how tests avoid the network).
# --------------------------------------------------------------------------- #


class RateFeedClient:
    """A thin sync ``httpx``-backed fetcher for the ECB feed.

    ``base_url`` defaults to ``FRANKFURTER_BASE_URL``, overridable via the
    ``FX_FEED_BASE_URL`` env var read at construction (E39-NFR04). ``transport`` is
    injectable so a test can pass a raw ``httpx.MockTransport``; ``respx`` also
    intercepts at the transport level with no ``transport`` argument at all (pattern:
    ``server/modules/google-ad-manager/tests/test_pull_gam.py``). No live host is
    ever contacted in the test suite (E39-NFR07).
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float = _DEFAULT_FX_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
        max_attempts: int = _MAX_ATTEMPTS,
    ):
        self._base_url = (
            base_url
            if base_url is not None
            else os.environ.get(FX_FEED_BASE_URL_ENV, FRANKFURTER_BASE_URL)
        ).rstrip("/")
        self._timeout = timeout
        self._transport = transport
        self._max_attempts = max(1, int(max_attempts))

    def fetch_rates(self, on_date: date, *, base: str = _EUR, symbols: list[str]) -> dict:
        """GET ``/v1/{YYYY-MM-DD}?base=&symbols=`` with bounded retry; parsed JSON.

        Returns the ``{"amount","base","date","rates"}`` payload. Raises
        ``FxFeedUnavailable`` on exhausted transient failures (5xx / network /
        timeout). A 4xx (e.g. a bad currency code) is NOT retried -- it surfaces as
        an empty/absent ``rates`` leg, which the provider treats as uncovered.
        """
        url = f"{self._base_url}/v1/{on_date:%Y-%m-%d}"
        params: dict[str, str] = {"base": base}
        if symbols:
            params["symbols"] = ",".join(symbols)

        last_exc: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                with httpx.Client(
                    timeout=self._timeout, transport=self._transport
                ) as client:
                    resp = client.get(url, params=params)
                # AD-3/logging: minimal line, no secrets exist here (no key).
                logger.debug(
                    "fx_feed: date=%s symbols=%s status=%s attempt=%s",
                    on_date,
                    ",".join(symbols),
                    resp.status_code,
                    attempt,
                )
                if resp.status_code >= 500:
                    last_exc = FxFeedUnavailable(
                        f"fx feed {resp.status_code} on {on_date} (attempt {attempt})"
                    )
                    continue
                # 4xx -> not transient: return the (possibly rate-less) body so the
                # provider fails closed on the missing leg rather than retrying.
                if resp.status_code >= 400:
                    return {"base": base, "date": on_date.isoformat(), "rates": {}}
                return resp.json()
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                logger.debug(
                    "fx_feed: transient failure date=%s attempt=%s: %s",
                    on_date,
                    attempt,
                    exc,
                )
                continue

        raise FxFeedUnavailable(
            f"fx feed unavailable for {on_date} after {self._max_attempts} attempts"
        ) from last_exc


# --------------------------------------------------------------------------- #
# Pure math -- EUR-triangulation + carry-forward (no I/O).
# --------------------------------------------------------------------------- #


def _eur_rate(rates: dict, ccy: str) -> float | None:
    """Return EUR->``ccy`` from the payload, ``1.0`` for EUR, ``None`` if absent.

    A ``None`` means the currency is outside ECB coverage (or the feed omitted it):
    an honest uncovered leg, never approximated.
    """
    if ccy == _EUR:
        return 1.0
    value = rates.get(ccy)
    if value is None:
        return None
    return float(value)


def _triangulate(rates: dict, from_ccy: str, to_ccy: str) -> float | None:
    """Pure cross-rate via the EUR base: ``from->to = (EUR->to) / (EUR->from)``.

    EUR on either side degenerates correctly (EUR->X = rates[X];
    X->EUR = 1/rates[X]). Any missing leg -> ``None`` (uncovered, fail-closed).
    No re-rounding beyond the single division (E39-NFR01 spirit).
    """
    eur_from = _eur_rate(rates, from_ccy)
    eur_to = _eur_rate(rates, to_ccy)
    if eur_from is None or eur_to is None or eur_from == 0.0:
        return None
    return eur_to / eur_from


def _carry_forward_marker(requested: date, effective: date) -> str | None:
    """Return ``POLICY_CARRY_FORWARD`` when the feed echoed an earlier publishing day.

    ``effective`` is ``payload["date"]`` (the ECB day the rate is actually from). If
    it precedes the requested date, the rate was carried forward over a non-publishing
    day; the provider records that as provenance rather than computing a holiday
    calendar (FEED-DRIVEN; E39-NFR04 -- no platform-hardcoded holiday table).
    """
    if effective < requested:
        return POLICY_CARRY_FORWARD
    return None


# --------------------------------------------------------------------------- #
# The provider -- the E39-AD4 drop-in for convert(tier="historical", provider=).
# --------------------------------------------------------------------------- #


class FrankfurterAsOfRateProvider:
    """Live as-of-day provider satisfying ``fx_helper.AsOfRateProvider`` (structural).

    Plugged via ``convert(..., tier="historical", provider=FrankfurterAsOfRateProvider())``.
    ``convert()`` and its callers are UNCHANGED (E39-AD4). It returns ``None`` for a
    truly uncovered pair or an exhausted-retry fetch, so ``convert()`` raises
    ``MissingAsOfRate`` (fail-closed, E39-NFR02) -- never a ``1.0`` fallback, never a
    fabricated amount.

    A request-scoped ``self._cache`` (keyed by ``on_date``) serves a batch of
    same-date multi-pair conversions from one HTTP round trip (AC7).
    """

    def __init__(
        self,
        *,
        client: RateFeedClient | None = None,
        base_currency: str = _EUR,
    ):
        self._client = client if client is not None else RateFeedClient()
        self._base = base_currency
        self._cache: dict[date, dict] = {}

    def _payload_for(self, on_date: date) -> dict | None:
        """Fetch (cached per date) the FULL EUR-base table; ``None`` if feed unavailable.

        We request the WHOLE table (no ``symbols`` filter) so the per-date cache covers ANY
        later pair on the same date -- otherwise a same-date multi-pair batch (AC7) would reuse
        a payload that only carried the first call's symbols and fabricate a false uncovered gap
        (MissingAsOfRate) for a pair ECB actually covers. Frankfurter returns ~30 majors when
        ``symbols`` is omitted; one round trip per date still holds.
        """
        cached = self._cache.get(on_date)
        if cached is not None:
            return cached
        try:
            payload = self._client.fetch_rates(on_date, base=self._base, symbols=[])
        except FxFeedUnavailable as exc:
            # Exhausted retries -> fail closed (AC7): convert() -> MissingAsOfRate.
            logger.warning("fx_feed: unavailable for %s -> None (%s)", on_date, exc)
            return None
        self._cache[on_date] = payload
        return payload

    def get_rate(
        self, from_ccy: str, to_ccy: str, on_date: date | None
    ) -> RateQuote | None:
        # 1. Identity short-circuit -- exact, no network (mirror SeedAsOfRateProvider).
        if from_ccy == to_ccy:
            return RateQuote(
                rate=1.0,
                source=SOURCE_ECB,
                tier=TIER_HISTORICAL,
                as_of=on_date if on_date is not None else date.today(),
                policy=POLICY_IDENTITY,
            )
        # 2. An as-of lookup without a figure date is meaningless (convert() guards
        #    this too, but the provider is defensive).
        if on_date is None:
            return None

        # 3. Fetch (cached) the FULL EUR-base table (covers both legs + any later same-date pair).
        payload = self._payload_for(on_date)
        if payload is None:
            return None

        rates = payload.get("rates") or {}
        # 4. EUR-triangulate; a missing leg -> None (uncovered -> MissingAsOfRate).
        rate = _triangulate(rates, from_ccy, to_ccy)
        if rate is None:
            return None

        # 5. Carry-forward provenance from the feed's echoed effective date.
        effective = self._effective_date(payload, on_date)
        policy = _carry_forward_marker(on_date, effective)

        # 6. as_of = the EFFECTIVE (post carry-forward) date -- what convert() stamps
        #    into fx["as_of_date"] (AC2 / AD-9 provenance).
        return RateQuote(
            rate=rate,
            source=SOURCE_ECB,
            tier=TIER_HISTORICAL,
            as_of=effective,
            policy=policy,
        )

    @staticmethod
    def _effective_date(payload: dict, requested: date) -> date:
        """Parse ``payload["date"]`` (the ECB effective day); fall back to requested."""
        raw = payload.get("date")
        if not raw:
            return requested
        try:
            return date.fromisoformat(str(raw).strip())
        except ValueError:
            logger.warning("fx_feed: unparseable date echo %r -> using requested", raw)
            return requested
