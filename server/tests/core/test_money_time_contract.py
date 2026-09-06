"""Story 48.3: the money and time contract, proved offline.

Every test here is a property the eleven completeness criteria turn on, written so
it fails if the defect it describes comes back. They need no database and no
network: the vocabularies are local snapshots, the arithmetic is pure, and the two
comparison engines are pure functions over evidence.

The database-bound halves (policy publication, FX ingestion, evidence writes, the
two compilers) are exercised by `test_money_time_governance_pg.py`, which is
gated on a live PostgreSQL.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from core import currency_vocabulary as ccy
from core import timezone_vocabulary as tzv
from core.governance_rule_sets import RuleSetError, get_profile
from core.money import (
    MoneyAdapterError,
    convert_micros,
    exact_decimal,
    micros_to_decimal,
    to_canonical_micros,
)
from core.money_evidence import (
    GAP_MIXED_CURRENCY,
    GAP_NO_ADAPTER,
    GAP_NO_CURRENCY,
    GAP_NO_UNIT,
    classify_gaps,
)
from core.money_policy import (
    PROFILE_MONEY,
    PROFILE_TIMEZONE,
    MoneyPolicy,
    TimezonePolicy,
)
from core.time_boundary import (
    GAP_DATE_ONLY,
    GAP_DST_GAP,
    GAP_NO_SOURCE_ZONE,
    GAP_TZDB_MISMATCH,
    GRAIN_DATE_ONLY,
    GRAIN_TIMESTAMP,
    ORIGIN_DECLARATION,
    ORIGIN_PULL_METADATA,
    BoundaryEvidence,
    compare_boundaries,
    derive_reporting_date,
)

# ---------------------------------------------------------------------------
# Fixtures: policies built directly, so a test never depends on a database.
# ---------------------------------------------------------------------------


def _money_policy(**overrides) -> MoneyPolicy:
    defaults = dict(
        rule_set_id="grs_TEST",
        version_id="grsv_TEST",
        content_hash="0" * 64,
        reporting_currency="EUR",
        reporting_currency_minor_unit=2,
        rounding="half_even",
        rate_source_priority=("reference_bank",),
        allow_triangulation=False,
        triangulation_pivot=None,
        allow_carry_forward=False,
        max_staleness_days=4,
    )
    defaults.update(overrides)
    return MoneyPolicy(**defaults)


def _timezone_policy(**overrides) -> TimezonePolicy:
    defaults = dict(
        rule_set_id="grs_TZ",
        version_id="grsv_TZ",
        content_hash="1" * 64,
        reporting_timezone="Europe/Paris",
        tzdb_version=tzv.tzdb_version(),
        timestamp_derivation="derive_when_sufficient",
        dst_gap_policy="refuse",
        dst_overlap_policy="first_occurrence",
        assumptions=(),
    )
    defaults.update(overrides)
    return TimezonePolicy(**defaults)


def _boundary(**overrides) -> BoundaryEvidence:
    defaults = dict(
        datastream_id="ds_TEST",
        execution_id="exec_1",
        grain=GRAIN_TIMESTAMP,
        timestamp_sufficiency="sufficient",
        observed_report_timezone="America/New_York",
        evidence_origin=ORIGIN_PULL_METADATA,
        confidence="observed",
        assumed=False,
        assumptions=(),
        adjustment_lever={"available": False},
        tzdb_version=tzv.tzdb_version(),
        gap_code=None,
    )
    defaults.update(overrides)
    return BoundaryEvidence(**defaults)


# ---------------------------------------------------------------------------
# The governed vocabularies. "a monetary value exists without source currency and
# unit" needs a currency set that can be checked, and a minor unit that is data.
# ---------------------------------------------------------------------------


def test_minor_unit_is_read_never_defaulted_to_two():
    """JPY has 0 decimals, BHD has 3, and XAU has none at all.

    Defaulting an unknown minor unit to 2 is how a yen total acquires two
    meaningless decimals and a gold quotation acquires a rounding step the
    standard never assigned. `None` is a real answer here.
    """
    assert ccy.minor_unit("JPY") == 0
    assert ccy.minor_unit("EUR") == 2
    assert ccy.minor_unit("BHD") == 3
    assert ccy.minor_unit("XAU") is None
    assert ccy.minor_unit("not-a-currency") is None


def test_reporting_currency_is_narrower_than_the_vocabulary():
    """A metal, a fund code and XXX resolve as NATIVE currencies and are not
    reporting currencies. A Project does not report in Palladium."""
    selectable = {item.code for item in ccy.selectable_reporting_currencies()}
    assert "EUR" in selectable and "JPY" in selectable
    for code in ("XAU", "XPD", "XDR", "XXX", "XTS", "CLF"):
        assert ccy.resolve_currency(code) is not None, f"{code} must still resolve natively"
        assert code not in selectable, f"{code} must not be selectable as a reporting currency"


def test_currency_aliases_resolve_without_guessing():
    assert ccy.resolve_currency("usd").code == "USD"
    assert ccy.resolve_currency("$").code == "USD"
    assert ccy.resolve_currency("  eur  ").code == "EUR"
    assert ccy.resolve_currency("EURO").code == "EUR"
    # No fuzzy matching: an unknown token is unknown, not the nearest thing.
    assert ccy.resolve_currency("EURR") is None
    assert ccy.resolve_currency("") is None
    assert ccy.resolve_currency(None) is None


def test_timezone_canonicality_comes_from_the_database_not_from_sorting():
    """`Europe/Monaco` sorts before `Europe/Paris` and `Europe/Kiev` before
    `Europe/Kyiv`. A lexicographic rule therefore promotes the LINK and demotes the
    real zone; `zone1970.tab` is the authority and gets both right."""
    assert tzv.canonical_zone("Europe/Monaco") == "Europe/Paris"
    assert tzv.canonical_zone("Europe/Kiev") == "Europe/Kyiv"
    assert tzv.canonical_zone("Europe/Paris") == "Europe/Paris"
    assert tzv.canonical_zone("UTC") == "UTC"
    assert tzv.canonical_zone("Not/AZone") is None


def test_tzdb_version_is_readable_and_never_a_placeholder():
    """A derivation that cannot name its tzdb release is not reproducible, so the
    reader raises rather than returning "unknown"."""
    version = tzv.tzdb_version()
    assert version and version != "unknown"
    assert version[:4].isdigit()


def test_offset_only_identifiers_are_not_reporting_boundaries():
    for zone in ("Etc/GMT+3", "Etc/UTC"):
        assert tzv.resolve_timezone(zone) is not None
        assert not tzv.resolve_timezone(zone).selectable
    assert tzv.resolve_timezone("UTC").selectable, "reporting in UTC is a legitimate choice"


# ---------------------------------------------------------------------------
# Exact arithmetic. "a cross-currency total succeeds without explicit FX evidence"
# has an arithmetic twin: a total that is right to the cent.
# ---------------------------------------------------------------------------


def test_decimal_roundtrip_is_exact_for_values_a_float_cannot_hold():
    for text in ("0.1", "1.005", "999999.995", "12345678.901234", "0.000001"):
        micros = to_canonical_micros(text, "decimal")
        assert micros_to_decimal(micros, minor_unit=6) == Decimal(text), text


def test_float_input_uses_the_number_that_was_sent_not_the_binary_one():
    """`Decimal(0.1)` is 0.1000000000000000055511151231257827..., which is the
    double. `Decimal(repr(0.1))` is 0.1, which is what the provider sent."""
    assert to_canonical_micros(0.1, "decimal") == 100_000
    assert exact_decimal(0.1) == Decimal("0.1")
    assert exact_decimal(Decimal("0.1")) == Decimal("0.1")


def test_conversion_rounds_once_to_the_currency_own_precision():
    hundred = 100_000_000  # 100.000000 in canonical micros
    assert micros_to_decimal(
        convert_micros(hundred, Decimal("0.925"), minor_unit=2), minor_unit=2
    ) == Decimal("92.50")
    # JPY has no decimals: a yen total lands on a whole yen.
    assert micros_to_decimal(
        convert_micros(hundred, Decimal("163.42"), minor_unit=0), minor_unit=0
    ) == Decimal("16342")
    # BHD has three: a dinar total lands on a fils.
    assert micros_to_decimal(
        convert_micros(hundred, Decimal("0.4093"), minor_unit=3), minor_unit=3
    ) == Decimal("40.930")


def test_conversion_refuses_a_non_positive_or_unusable_rate():
    for bad in (Decimal("0"), Decimal("-1")):
        with pytest.raises(MoneyAdapterError):
            convert_micros(1_000_000, bad, minor_unit=2)
    with pytest.raises(MoneyAdapterError):
        exact_decimal(float("nan"))
    with pytest.raises(MoneyAdapterError):
        to_canonical_micros("10", "furlongs")


# ---------------------------------------------------------------------------
# The Money Policy profile. The refusals are the point: a policy that cannot be
# published wrong is a policy no code has to defend against.
# ---------------------------------------------------------------------------


def test_money_policy_refuses_a_currency_that_is_not_legal_tender():
    validate = get_profile(PROFILE_MONEY).validate
    base = {
        "reporting_currency": "EUR",
        "rate_source_priority": ["reference_bank"],
        "max_staleness_days": 4,
    }
    assert validate(base)["reporting_currency"] == "EUR"
    for bad in ("XAU", "XDR", "XXX"):
        with pytest.raises(RuleSetError):
            validate({**base, "reporting_currency": bad})
    with pytest.raises(RuleSetError):
        validate({**base, "reporting_currency": "not-a-code"})


def test_money_policy_refuses_a_missing_or_absurd_staleness_bound():
    validate = get_profile(PROFILE_MONEY).validate
    base = {"reporting_currency": "EUR", "rate_source_priority": ["reference_bank"]}
    with pytest.raises(RuleSetError):
        validate(base)  # no max_staleness_days at all
    with pytest.raises(RuleSetError):
        validate({**base, "max_staleness_days": -1})
    with pytest.raises(RuleSetError):
        validate({**base, "max_staleness_days": 1000})


def test_money_policy_refuses_triangulation_without_a_pivot():
    validate = get_profile(PROFILE_MONEY).validate
    base = {
        "reporting_currency": "EUR",
        "rate_source_priority": ["reference_bank"],
        "max_staleness_days": 4,
    }
    with pytest.raises(RuleSetError):
        validate({**base, "allow_triangulation": True})
    resolved = validate({**base, "allow_triangulation": True, "triangulation_pivot": "usd"})
    assert resolved["triangulation_pivot"] == "USD"


def test_timezone_policy_pins_a_tzdb_version_and_refuses_a_compatibility_zone():
    validate = get_profile(PROFILE_TIMEZONE).validate
    resolved = validate({"reporting_timezone": "Europe/Monaco"})
    # Canonicalised, so two Projects on the same boundary cannot look different.
    assert resolved["reporting_timezone"] == "Europe/Paris"
    assert resolved["tzdb_version"] == tzv.tzdb_version()
    # The DATE rule is a constant, not a setting: at DATE grain there is no
    # sub-day data, so "shift it" is not a choice a Project could make correctly.
    assert resolved["date_only_policy"] == "signal_never_shift"
    with pytest.raises(RuleSetError):
        validate({"reporting_timezone": "Etc/GMT+3"})
    with pytest.raises(RuleSetError):
        validate({"reporting_timezone": "Mars/Olympus"})


# ---------------------------------------------------------------------------
# Observed money evidence. Each gap names a DIFFERENT repair.
# ---------------------------------------------------------------------------


def test_each_money_gap_names_a_different_missing_thing():
    gaps = classify_gaps(
        [
            {"canonical_field": "cost", "native_unit": "decimal", "adapter": "money.v1"},
            {
                "canonical_field": "revenue",
                "native_currency": "EUR",
                "adapter": "money.v1",
            },
            {"canonical_field": "fees", "native_currency": "EUR", "native_unit": "decimal"},
            {
                "canonical_field": "refunds",
                "native_unit": "decimal",
                "adapter": "money.v1",
                "distinct_currencies": ["EUR", "USD"],
            },
        ]
    )
    codes = {gap["code"] for gap in gaps}
    assert GAP_NO_CURRENCY in codes  # cost published no currency
    assert GAP_NO_UNIT in codes  # revenue published no unit
    assert GAP_NO_ADAPTER in codes  # fees records no exact adapter
    assert GAP_MIXED_CURRENCY in codes  # refunds mixed two currencies in one field


def test_a_fully_evidenced_field_has_no_gap():
    assert (
        classify_gaps(
            [
                {
                    "canonical_field": "cost",
                    "native_currency": "USD",
                    "native_unit": "decimal",
                    "adapter": "money.v1",
                    "distinct_currencies": ["USD"],
                }
            ]
        )
        == []
    )


# ---------------------------------------------------------------------------
# Reporting-date derivation. "a preference change mutates native timestamps or
# facts" and "a Datastream silently assumes UTC or the Project timezone".
# ---------------------------------------------------------------------------


def test_a_source_date_is_never_shifted():
    """There is no sub-day data to re-slice, so any "re-aligned" date would be
    manufactured. The source date is echoed and the gap is typed."""
    result = derive_reporting_date(
        policy=_timezone_policy(),
        evidence=_boundary(
            grain=GRAIN_DATE_ONLY,
            timestamp_sufficiency="insufficient_no_timestamp",
        ),
        source_date=date(2026, 3, 14),
    )
    assert result.reporting_date is None
    assert result.time_boundary_gap_code == GAP_DATE_ONLY
    assert result.source_date == date(2026, 3, 14), "the native date must survive untouched"


def test_a_timestamp_derives_a_reporting_date_and_pins_its_versions():
    policy = _timezone_policy()
    # 2026-01-05 22:30 in New York is 2026-01-06 in Paris: a real day change.
    result = derive_reporting_date(
        policy=policy,
        evidence=_boundary(),
        source_timestamp=datetime(2026, 1, 5, 22, 30),
    )
    assert result.reporting_date == date(2026, 1, 6)
    assert result.time_boundary_gap_code is None
    assert result.timezone_policy_version_id == policy.version_id
    assert result.tzdb_version == policy.tzdb_version
    assert result.source_report_timezone == "America/New_York"
    # The native instant is echoed, never rewritten.
    assert result.source_timestamp == datetime(2026, 1, 5, 22, 30)


def test_changing_the_reporting_timezone_re_derives_without_touching_the_source():
    # 10:00 in New York is 16:00 the same day in Paris and 00:00 the NEXT day in
    # Tokyo -- the two reporting currencies of time, so to speak.
    source = datetime(2026, 1, 5, 10, 0)
    evidence = _boundary()
    paris = derive_reporting_date(
        policy=_timezone_policy(reporting_timezone="Europe/Paris"),
        evidence=evidence,
        source_timestamp=source,
    )
    tokyo = derive_reporting_date(
        policy=_timezone_policy(reporting_timezone="Asia/Tokyo", version_id="grsv_TZ2"),
        evidence=evidence,
        source_timestamp=source,
    )
    assert paris.reporting_date != tokyo.reporting_date, "a new preference re-derives"
    assert paris.source_timestamp == tokyo.source_timestamp == source
    assert paris.timezone_policy_version_id != tokyo.timezone_policy_version_id


def test_an_unknown_source_zone_refuses_rather_than_assuming_utc():
    result = derive_reporting_date(
        policy=_timezone_policy(),
        evidence=_boundary(observed_report_timezone=None),
        source_timestamp=datetime(2026, 1, 5, 22, 30),
    )
    assert result.reporting_date is None
    assert result.time_boundary_gap_code == GAP_NO_SOURCE_ZONE


def test_a_tzdb_mismatch_refuses_because_it_is_not_reproducible():
    result = derive_reporting_date(
        policy=_timezone_policy(tzdb_version="1999z"),
        evidence=_boundary(tzdb_version="2026c"),
        source_timestamp=datetime(2026, 1, 5, 22, 30),
    )
    assert result.reporting_date is None
    assert result.time_boundary_gap_code == GAP_TZDB_MISMATCH


def test_a_dst_gap_refuses_or_shifts_according_to_the_confirmed_policy():
    """02:30 on the US spring-forward Sunday does not exist in New York."""
    spring_forward = datetime(2026, 3, 8, 2, 30)
    refused = derive_reporting_date(
        policy=_timezone_policy(dst_gap_policy="refuse"),
        evidence=_boundary(),
        source_timestamp=spring_forward,
    )
    assert refused.reporting_date is None
    assert refused.time_boundary_gap_code == GAP_DST_GAP
    assert refused.dst_note

    allowed = derive_reporting_date(
        policy=_timezone_policy(dst_gap_policy="shift_forward"),
        evidence=_boundary(),
        source_timestamp=spring_forward,
    )
    assert allowed.reporting_date is not None
    assert allowed.time_boundary_gap_code is None


def test_a_dst_overlap_resolves_to_the_fold_the_policy_names():
    """01:30 on the US fall-back Sunday happens twice in New York."""
    fall_back = datetime(2026, 11, 1, 1, 30)
    first = derive_reporting_date(
        policy=_timezone_policy(dst_overlap_policy="first_occurrence"),
        evidence=_boundary(),
        source_timestamp=fall_back,
    )
    refused = derive_reporting_date(
        policy=_timezone_policy(dst_overlap_policy="refuse"),
        evidence=_boundary(),
        source_timestamp=fall_back,
    )
    assert first.reporting_date is not None
    assert refused.reporting_date is None
    assert refused.time_boundary_gap_code == GAP_DST_GAP


def test_a_policy_that_never_derives_is_honoured():
    result = derive_reporting_date(
        policy=_timezone_policy(timestamp_derivation="never_derive"),
        evidence=_boundary(),
        source_timestamp=datetime(2026, 1, 5, 22, 30),
    )
    assert result.reporting_date is None


# ---------------------------------------------------------------------------
# Comparison. "cross-source daily reconciliation ignores different day boundaries"
# is exactly what an EXCLUDED unknown stream produced.
# ---------------------------------------------------------------------------


def test_two_agreeing_streams_and_three_unplaceable_is_not_comparable():
    policy = _timezone_policy()
    result = compare_boundaries(
        [
            _boundary(datastream_id="a", observed_report_timezone="Europe/Paris"),
            _boundary(datastream_id="b", observed_report_timezone="Europe/Paris"),
            _boundary(datastream_id="c", observed_report_timezone=None),
            _boundary(datastream_id="d", observed_report_timezone=None),
            _boundary(datastream_id="e", grain=GRAIN_DATE_ONLY),
        ],
        policy=policy,
    )
    assert result["comparable"] is False
    assert result["unplaced_count"] == 2
    assert result["date_only_count"] == 1
    codes = {signal["code"] for signal in result["signals"]}
    assert GAP_NO_SOURCE_ZONE in codes
    assert GAP_DATE_ONLY in codes
    # Every stream stays in the payload; none is silently dropped.
    assert len(result["streams"]) == 5


def test_all_placed_and_aligned_is_comparable():
    result = compare_boundaries(
        [
            _boundary(datastream_id="a", observed_report_timezone="Europe/Paris"),
            _boundary(datastream_id="b", observed_report_timezone="Europe/Paris"),
        ],
        policy=_timezone_policy(),
    )
    assert result["comparable"] is True
    assert result["signals"] == []


def test_different_clocks_are_signalled_and_never_realigned():
    result = compare_boundaries(
        [
            _boundary(datastream_id="a", observed_report_timezone="Europe/Paris"),
            _boundary(datastream_id="b", observed_report_timezone="America/New_York"),
        ],
        policy=_timezone_policy(),
    )
    assert result["comparable"] is False
    assert result["distinct_source_timezones"] == ["America/New_York", "Europe/Paris"]
    signal = next(s for s in result["signals"] if s["code"] == "cross_source_day_boundary_differs")
    assert signal["severity"] == "advisory"
    assert "never corrected" in signal["message"]


def test_a_declaration_is_not_an_observation():
    """AC6 in one assertion: `is_observed` is false for a declared boundary, and
    the compiler keys `complete` on it."""
    declared = _boundary(evidence_origin=ORIGIN_DECLARATION)
    observed = _boundary(evidence_origin=ORIGIN_PULL_METADATA)
    assert declared.is_observed is False
    assert observed.is_observed is True


# ---------------------------------------------------------------------------
# English copy on every string that reaches a screen (AC11).
# ---------------------------------------------------------------------------


def test_boundary_messages_are_english():
    import pathlib

    from core import report_timezone, timezone_signal

    french = ("aucun", "flux", "fuseau", "journee", "metrique", "donnee", "decale")
    for module in (report_timezone, timezone_signal):
        text = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        # Only quoted message bodies, not identifiers: `_gap_result` is fine.
        for word in french:
            assert f" {word} " not in text.lower(), f"French copy left in {module.__name__}"


# ---------------------------------------------------------------------------
# AI-167 -- "not re-alignable" must not silence "different clocks"
#
# `compare_boundaries` bucketed exclusively: a DATE-grain stream never reached `known`, so
# its zone never reached `zones`. Since the reporting grain is DATE everywhere, that was
# EVERY stream -- the `cross_source_day_boundary_differs` branch was unreachable by any
# real comparison, in the very function written to stop "cross-source daily reconciliation
# ignores different day boundaries" from happening.
# ---------------------------------------------------------------------------


def _date_grain_evidence(datastream_id, zone):
    from core.time_boundary import GRAIN_DATE_ONLY, BoundaryEvidence

    return BoundaryEvidence(
        datastream_id=datastream_id,
        execution_id="pull_1",
        grain=GRAIN_DATE_ONLY,
        timestamp_sufficiency="insufficient_no_timestamp",
        observed_report_timezone=zone,
        evidence_origin="pull_metadata",
        confidence="observed",
        assumed=False,
        assumptions=(),
        adjustment_lever={},
        tzdb_version="2026c",
        gap_code=None,
    )


def test_date_grain_streams_on_different_clocks_are_still_signalled():
    """The product's only grain must not be the grain that hides the divergence."""
    from core.time_boundary import compare_boundaries

    result = compare_boundaries([
        _date_grain_evidence("ds_a", "Europe/Paris"),
        _date_grain_evidence("ds_b", "America/New_York"),
    ])
    assert result["distinct_source_timezones"] == ["America/New_York", "Europe/Paris"]
    codes = [signal["code"] for signal in result["signals"]]
    assert "cross_source_day_boundary_differs" in codes
    # And the non-realignability is still reported -- it is a fact, just not a silencer.
    assert "date_grain_not_realignable" in codes
    assert result["comparable"] is False


def test_date_grain_streams_agreeing_on_a_clock_ARE_comparable():
    """A source DATE does not stop two days from being equivalent.

    Keeping `date_only` in the comparability condition made `comparable` constantly false
    in a DATE-grain product: flagging everything says nothing.
    """
    from core.time_boundary import compare_boundaries

    result = compare_boundaries([
        _date_grain_evidence("ds_a", "Europe/Paris"),
        _date_grain_evidence("ds_b", "Europe/Paris"),
    ])
    assert result["comparable"] is True
    assert "cross_source_day_boundary_differs" not in [s["code"] for s in result["signals"]]
    assert "date_grain_not_realignable" in [s["code"] for s in result["signals"]]


def test_an_unobserved_stream_is_still_named_rather_than_dropped():
    """Story 48.3's own correction, preserved: an unplaceable stream is retained."""
    from core.time_boundary import compare_boundaries

    result = compare_boundaries([
        _date_grain_evidence("ds_a", "Europe/Paris"),
        _date_grain_evidence("ds_b", None),
    ])
    assert result["unplaced_count"] == 1
    assert result["comparable"] is False
    unplaced = next(s for s in result["signals"] if s["code"] == "source_report_timezone_unknown")
    assert unplaced["datastream_ids"] == ["ds_b"]
