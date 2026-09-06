"""Tests for Story 39.4 -- FX conversion helper (fixed + as-of-day).

Offline (no DB): seed parse, fixed conversion, identity exactness, as-of
selection, typed gaps, provenance shape, re-derivation, AD-2 grep, provider-swap
seam (the 39.5 live-feed plug point).

Pg-gated (skipped when TEST_POSTGRES_DSN is unset): resolve_reporting_currency
against the Project's ACTIVE configuration version.

WHERE THE REPORTING CURRENCY LIVES, and why these three tests were rewritten.
Story 39.4 read `app.project_preferences.canonical_currency` and fell soft to
`"EUR"` for anything it could not find (E39-NFR04). Both halves of that are
gone. `resolve_reporting_currency` now reads
`app.project_configuration_versions.posture -> defaults -> reporting_currency`
of the version `app.projects.active_configuration_version_id` points at, and
returns `None` when no confirmed configuration names one.

The amendment is ratified, in two places that say the same thing:

  * `docs/product-architecture/capabilities/placement-mapping.md` -- the gap
    table names `reporting_currency_unresolved` ("nothing names the currency the
    conversion targeted") as a TYPED gap that suppresses the amount, and records
    under `money_policy_unconfirmed` that *"Story 48.3 removed its `'EUR'`
    default"*. An unresolved currency is a refusal, not a default.
  * `docs/product-architecture/capabilities/currency-fx.md` -- "Unsafe
    mixed-currency aggregation fails closed instead of producing a plausible but
    false total", with "a cross-currency total succeeds without explicit FX
    evidence" listed under *Incomplete if*.

A soft `"EUR"` is exactly the plausible-but-false label those two forbid: it
would state an amount under a currency nobody confirmed.
`DEFAULT_REPORTING_CURRENCY` survives as the documented platform default a
person is OFFERED, never as an answer the resolver invents.

Harness header + _pg_reachable gate calqués sur test_dataset_access_grants.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import date
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core.fx_helper import (  # noqa: E402
    DEFAULT_REPORTING_CURRENCY,
    FixedRateProvider,
    FxSeedRow,
    MissingAsOfRate,
    MissingFxPair,
    RateQuote,
    SeedAsOfRateProvider,
    convert,
    parse_fx_seed,
    resolve_reporting_currency,
)

_FX_MODULE_PATH = Path(__file__).resolve().parents[2] / "core" / "fx_helper.py"
_AS_OF_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "fx_as_of_sample.csv"

_D_2026 = date(2026, 7, 1)


# ---------------------------------------------------------------------------
# Postgres availability check (calqué sur test_dataset_access_grants.py)
# ---------------------------------------------------------------------------


def _pg_reachable() -> bool:
    if not os.environ.get("TEST_POSTGRES_DSN"):
        return False
    try:
        import psycopg

        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


pg_available = pytest.mark.skipif(
    not _pg_reachable(), reason="platform Postgres not reachable"
)


def _as_of_provider() -> SeedAsOfRateProvider:
    return SeedAsOfRateProvider(_AS_OF_FIXTURE)


# ---------------------------------------------------------------------------
# Offline -- seed parse & fixed tier (AC1, AC7)
# ---------------------------------------------------------------------------


def _seed_usd_eur_rate() -> float:
    """The USD->EUR rate the seed actually carries (d734a294 moved it 0.92->0.85).

    Asserting a literal here re-created the drift d7cd5986 repaired in the dbt
    twin: the property under test is "the helper reads the seed", never "the
    seed holds 0.92".
    """
    rows = parse_fx_seed()
    return next(
        r.rate for r in rows if (r.from_currency, r.to_currency) == ("USD", "EUR")
    )


def test_parse_fx_seed_reads_typed_rows():
    """Test 1: parse_fx_seed reads the seed rows, all 7 columns typed."""
    rows = parse_fx_seed()
    by_pair = {(r.from_currency, r.to_currency): r for r in rows}

    assert ("USD", "EUR") in by_pair
    assert ("EUR", "EUR") in by_pair

    usd = by_pair[("USD", "EUR")]
    assert isinstance(usd, FxSeedRow)
    assert usd.rate == pytest.approx(_seed_usd_eur_rate())
    assert isinstance(usd.rate, float)
    assert usd.rate_date == date(2026, 7, 1)
    assert usd.rate_policy == "static_dev_rate"
    assert usd.valid_from == date(2020, 1, 1)
    assert usd.valid_to == date(2099, 12, 31)

    eur = by_pair[("EUR", "EUR")]
    assert eur.rate == pytest.approx(1.00)
    assert eur.rate_policy == "identity"


def test_fixed_provider_selects_row_in_window():
    """Test 2: FixedRateProvider returns the seed rate when the date is in window."""
    quote = FixedRateProvider().get_rate("USD", "EUR", _D_2026)
    assert quote is not None
    assert quote.rate == pytest.approx(_seed_usd_eur_rate())
    assert quote.source == "seed"
    assert quote.tier == "fixed"


def test_fixed_provider_out_of_window_returns_none():
    """Test 3: a date outside every window for the pair -> None (drives gap)."""
    rows = [
        FxSeedRow(
            from_currency="USD",
            to_currency="EUR",
            rate=0.92,
            rate_date=date(2026, 7, 1),
            rate_policy="static_dev_rate",
            valid_from=date(2026, 1, 1),
            valid_to=date(2026, 12, 31),
        )
    ]

    class _Provider(FixedRateProvider):
        def __init__(self):
            self._rows = rows

    assert _Provider().get_rate("USD", "EUR", date(2020, 1, 1)) is None


def test_convert_fixed_is_deterministic_value():
    """Test 4: convert(100 USD -> EUR, fixed) == 92.0, exact within 1e-9."""
    result = convert(
        100.0, "USD", reporting_currency="EUR", tier="fixed", on_date=_D_2026
    )
    assert result.amount == pytest.approx(100.0 * _seed_usd_eur_rate(), abs=1e-9)
    assert result.reporting_currency == "EUR"
    assert result.source_amount == 100.0
    assert result.source_currency == "USD"


def test_convert_fixed_determinism_full_object():
    """Test 5: two identical convert() calls return equal ConvertedAmount + fx."""
    a = convert(100.0, "USD", reporting_currency="EUR", tier="fixed", on_date=_D_2026)
    b = convert(100.0, "USD", reporting_currency="EUR", tier="fixed", on_date=_D_2026)
    assert a == b
    assert a.fx == b.fx


def test_identity_is_exact_passthrough():
    """Test 6: same-currency -> amount unchanged, rate 1.0, no re-rounding (AC7)."""
    result = convert(100.0, "EUR", reporting_currency="EUR")
    assert result.amount == 100.0
    assert result.amount is result.source_amount  # exact object, no float op
    assert result.fx["rate"] == 1.0
    # Works even without an explicit EUR,EUR row: use a currency absent from seed.
    other = convert(37.5, "XYZ", reporting_currency="XYZ")
    assert other.amount == 37.5
    assert other.fx["rate"] == 1.0


# ---------------------------------------------------------------------------
# Offline -- as-of / historical tier (AC2, AC5)
# ---------------------------------------------------------------------------


def test_as_of_selects_latest_before_figure_date():
    """Test 7: get_rate on 2024-08-15 -> the 2024-01-02 row (0.90), not 2026."""
    quote = _as_of_provider().get_rate("USD", "EUR", date(2024, 8, 15))
    assert quote is not None
    assert quote.rate == pytest.approx(0.90)
    assert quote.as_of == date(2024, 1, 2)
    assert quote.tier == "historical"


def test_convert_historical_uses_past_rate():
    """Test 8: past figure converts at the past rate (AC2)."""
    result = convert(
        100.0,
        "USD",
        reporting_currency="EUR",
        tier="historical",
        on_date=date(2024, 8, 15),
        provider=_as_of_provider(),
    )
    assert result.amount == pytest.approx(90.0)
    assert result.fx["tier"] == "historical"
    assert result.fx["as_of_date"] == "2024-01-02"


def test_historical_is_run_day_independent():
    """Test 9: the historical call is identical regardless of 'today' (no clock)."""
    kwargs = dict(
        reporting_currency="EUR",
        tier="historical",
        on_date=date(2024, 8, 15),
        provider=_as_of_provider(),
    )
    first = convert(100.0, "USD", **kwargs)
    second = convert(100.0, "USD", **kwargs)
    assert first == second
    assert first.amount == pytest.approx(90.0)


def test_historical_without_date_raises_value_error():
    """Test 10: tier='historical' with on_date=None -> ValueError."""
    with pytest.raises(ValueError):
        convert(
            100.0,
            "USD",
            reporting_currency="EUR",
            tier="historical",
            provider=_as_of_provider(),
        )


def test_provider_swap_seam_flows_source_label():
    """Test 11: a fake AsOfRateProvider is accepted unchanged; its source flows through.

    Proves the 39.5 seam: a live provider plugs in without touching convert().
    """

    class _FakeLiveProvider:
        def get_rate(self, from_ccy, to_ccy, on_date):
            return RateQuote(
                rate=0.88,
                source="fake-svc",
                tier="historical",
                as_of=date(2025, 1, 1),
            )

    result = convert(
        100.0,
        "USD",
        reporting_currency="EUR",
        tier="historical",
        on_date=date(2025, 3, 1),
        provider=_FakeLiveProvider(),
    )
    assert result.amount == pytest.approx(88.0)
    assert result.fx["source"] == "fake-svc"
    assert result.fx["tier"] == "historical"


# ---------------------------------------------------------------------------
# Offline -- provenance & fail-closed (AC3, E39-NFR02, E39-NFR03)
# ---------------------------------------------------------------------------


def test_ad2_no_provider_vocabulary_in_source():
    """Test 12: fx_helper.py source contains no connector/provider name (AD-2)."""
    source = _FX_MODULE_PATH.read_text(encoding="utf-8").lower()
    forbidden = [
        "frankfurter",
        "ecb",
        "meta",
        "google",
        "ga4",
        "tiktok",
        "linkedin",
        "supermetrics",
        "adverity",
        "nango",
        "openexchangerates",
        "fixer",
        "currencylayer",
        "requests",
        "httpx",
        "urllib",
    ]
    hits = [name for name in forbidden if re.search(rf"\b{name}\b", source)]
    assert hits == [], f"provider/network vocabulary leaked into fx_helper.py: {hits}"


def test_fx_block_has_exact_keys_on_fixed_path():
    """Test 13: ConvertedAmount.fx == {rate, as_of_date, source, tier}; seed/fixed."""
    result = convert(
        100.0, "USD", reporting_currency="EUR", tier="fixed", on_date=_D_2026
    )
    assert set(result.fx.keys()) == {"rate", "as_of_date", "source", "tier"}
    assert result.fx["source"] == "seed"
    assert result.fx["tier"] == "fixed"


def test_source_fields_echoed_unchanged():
    """Test 14: source_amount / source_currency echoed unchanged (E39-AD2)."""
    result = convert(
        250.0, "USD", reporting_currency="EUR", tier="fixed", on_date=_D_2026
    )
    assert result.source_amount == 250.0
    assert result.source_currency == "USD"


def test_as_ad9_provenance_extends_not_replaces():
    """Test 15: as_ad9_provenance merges an fx sub-object into the base chain."""
    base = {"source_system": "x", "source_field": "cost", "pull_id": "p1"}
    result = convert(
        100.0, "USD", reporting_currency="EUR", tier="fixed", on_date=_D_2026
    )
    prov = result.as_ad9_provenance(base)
    assert prov["source_system"] == "x"
    assert prov["source_field"] == "cost"
    assert prov["pull_id"] == "p1"
    assert prov["fx"]["rate"] == pytest.approx(_seed_usd_eur_rate())
    assert prov["fx"]["source"] == "seed"
    # Base dict is not mutated.
    assert "fx" not in base


def test_missing_pair_raises_typed_gap():
    """Test 16: unknown pair -> MissingFxPair, no 1.0 fallback (E39-NFR02)."""
    with pytest.raises(MissingFxPair) as exc:
        convert(100.0, "GBP", reporting_currency="EUR", tier="fixed")
    assert exc.value.from_currency == "GBP"
    assert exc.value.to_currency == "EUR"


def test_missing_as_of_rate_raises_typed_gap():
    """Test 17: no rate at/before the date -> MissingAsOfRate (fail-closed)."""
    with pytest.raises(MissingAsOfRate) as exc:
        convert(
            100.0,
            "USD",
            reporting_currency="EUR",
            tier="historical",
            on_date=date(2020, 1, 1),
            provider=_as_of_provider(),
        )
    assert exc.value.from_currency == "USD"
    assert exc.value.to_currency == "EUR"


# ---------------------------------------------------------------------------
# Offline -- re-derivation on reporting-currency change (AC4, E39-AD2)
# ---------------------------------------------------------------------------


def test_rederivation_on_reporting_currency_change():
    """Test 18: same source, different reporting currency -> different amount,
    identical source fields (the Python analogue of test_currency_rederivation)."""
    _vf, _vt = date(2020, 1, 1), date(2099, 12, 31)
    seed_rows = [
        FxSeedRow("USD", "EUR", 0.92, _D_2026, "static_dev_rate", _vf, _vt),
        FxSeedRow("USD", "GBP", 0.80, _D_2026, "static_dev_rate", _vf, _vt),
    ]

    class _Provider(FixedRateProvider):
        def __init__(self):
            self._rows = seed_rows

    provider = _Provider()
    to_eur = convert(
        100.0, "USD", reporting_currency="EUR", tier="fixed", on_date=_D_2026, provider=provider
    )
    to_gbp = convert(
        100.0, "USD", reporting_currency="GBP", tier="fixed", on_date=_D_2026, provider=provider
    )
    assert to_eur.amount != to_gbp.amount
    assert to_eur.amount == pytest.approx(92.0)
    assert to_gbp.amount == pytest.approx(80.0)
    # Source is immutable: byte-identical across the two derivations.
    assert to_eur.source_amount == to_gbp.source_amount == 100.0
    assert to_eur.source_currency == to_gbp.source_currency == "USD"


def test_rederivation_on_rate_change(tmp_path):
    """Test 19: changing the seed row's rate re-derives; no source mutation."""
    seed_dir = tmp_path / "seeds"
    seed_dir.mkdir()
    seed_file = seed_dir / "fx_rates.csv"

    seed_file.write_text(
        "from_currency,to_currency,rate,rate_date,rate_policy,valid_from,valid_to\n"
        "USD,EUR,0.92,2026-07-01,static_dev_rate,2020-01-01,2099-12-31\n",
        encoding="utf-8",
    )
    before = convert(
        100.0, "USD", reporting_currency="EUR", tier="fixed", on_date=_D_2026,
        provider=FixedRateProvider(seed_dir),
    )
    assert before.amount == pytest.approx(92.0)

    seed_file.write_text(
        "from_currency,to_currency,rate,rate_date,rate_policy,valid_from,valid_to\n"
        "USD,EUR,0.50,2026-07-01,static_dev_rate,2020-01-01,2099-12-31\n",
        encoding="utf-8",
    )
    after = convert(
        100.0, "USD", reporting_currency="EUR", tier="fixed", on_date=_D_2026,
        provider=FixedRateProvider(seed_dir),
    )
    assert after.amount == pytest.approx(50.0)
    # Source amount unchanged across the re-derivation.
    assert before.source_amount == after.source_amount == 100.0


# ---------------------------------------------------------------------------
# Pg-gated -- resolve_reporting_currency against the ACTIVE configuration version
#
# The seeding writes the two rows the resolver joins, and nothing else: a
# Project and the configuration version `active_configuration_version_id` names.
# `app.project_preferences` is deliberately NOT written -- a legacy non-null
# preference must not be able to answer for a currency nobody confirmed, and a
# fixture that wrote one would prove the opposite of what the module docstring
# quotes.
# ---------------------------------------------------------------------------


def _seed_confirmed_currency(project_id: str, currency: str) -> None:
    """A Project whose ACTIVE configuration version confirms *currency*."""
    from core.db import get_connection

    version_id = f"pcv_{uuid.uuid4().hex[:12]}"
    posture = json.dumps({"defaults": {"reporting_currency": currency}}, sort_keys=True)
    # Both columns are CHECKed against `^[0-9a-f]{64}$` (migration 131), so the
    # fixture hashes what it writes rather than passing a placeholder.
    content_hash = hashlib.sha256(posture.encode("utf-8")).hexdigest()
    fingerprint = hashlib.sha256(version_id.encode("utf-8")).hexdigest()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.projects (id, name, slug, created_by, org_id)
                VALUES (%s, %s, %s, 'test', 'org_test_fixture')
                ON CONFLICT (id) DO NOTHING
                """,
                (project_id, project_id, project_id),
            )
            cur.execute(
                """
                INSERT INTO app.project_configuration_versions
                    (id, project_id, version_number, posture,
                     dependency_fingerprint, content_hash, activated_by)
                VALUES (%s, %s, 1, %s::jsonb, %s, %s, 'test')
                """,
                (version_id, project_id, posture, fingerprint, content_hash),
            )
            cur.execute(
                "UPDATE app.projects SET active_configuration_version_id = %s WHERE id = %s",
                (version_id, project_id),
            )
        conn.commit()


def _retire_project(project_id: str) -> None:
    """Unpoint the Project from its version. It cannot be deleted, and that is the design.

    `trg_project_configuration_versions_immutable` (migration 131) refuses UPDATE
    and DELETE on a configuration version outright -- a confirmed configuration is
    a record of a decision, not a row -- and `project_configuration_versions
    .project_id` is `ON DELETE RESTRICT`, so the Project cannot go either. The
    same shape as `app.nightly_step_runs`: the fixture mints a FRESH id per test
    rather than pretending it can clean up, and leaves the two rows where the
    guard insists they stay.
    """
    from core.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.projects SET active_configuration_version_id = NULL, "
                "status = 'archived' WHERE id = %s",
                (project_id,),
            )
        conn.commit()


@pg_available
def test_resolve_reporting_currency_reads_the_confirmed_configuration():
    """Test 20: the active configuration version's `defaults.reporting_currency`."""
    pid = f"proj_fx_{uuid.uuid4().hex[:8]}"
    _seed_confirmed_currency(pid, "USD")
    try:
        assert resolve_reporting_currency(pid) == "USD"
    finally:
        _retire_project(pid)


@pg_available
def test_an_unconfirmed_project_resolves_to_no_currency_at_all():
    """Test 21: unknown / unconfirmed Project -> None, never a soft `EUR`.

    `reporting_currency_unresolved` is a typed gap that SUPPRESSES the amount
    (placement-mapping.md). Answering `DEFAULT_REPORTING_CURRENCY` here would
    label a figure with a currency nobody chose, which is the plausible-but-false
    total currency-fx.md refuses -- so the constant stays a documented offer and
    is asserted NOT to be the resolver's answer.
    """
    assert resolve_reporting_currency(f"does-not-exist-{uuid.uuid4().hex[:8]}") is None
    assert DEFAULT_REPORTING_CURRENCY == "EUR"


@pg_available
def test_resolve_then_convert_composes():
    """Test 22: reporting-currency read + conversion compose end-to-end.

    The current seed lacks an EUR,USD pair (only USD,EUR + EUR,EUR), so
    converting EUR -> USD (a project whose reporting currency is USD) honestly
    raises MissingFxPair -- proving fail-closed without editing the shared seed.
    """
    pid = f"proj_fx_{uuid.uuid4().hex[:8]}"
    _seed_confirmed_currency(pid, "USD")
    try:
        reporting = resolve_reporting_currency(pid)
        assert reporting == "USD"
        with pytest.raises(MissingFxPair):
            convert(100.0, "EUR", reporting_currency=reporting, tier="fixed", on_date=_D_2026)
    finally:
        _retire_project(pid)
