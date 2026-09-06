"""A posed FX rate is refused before it is stored -- the validation half.

Every case here is a refusal the amendment of 2026-08-17 implies but does not
spell out. The point of a first-class `fixed` method is that a posed number is
DISTINGUISHABLE from a measured one; a door that accepted anything would give the
label to numbers nobody could defend, which is the defect wearing a new word.

Pure: no database. The storage half is proven against a real Postgres in
`tests/integration/test_fx_fixed_rate_postgres.py` -- the immutability triggers,
the CHECK that admits `fixed` and the activation pointer are SQL, and a double of
any of them would prove the test author's beliefs.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from core.fx_fixed_rates import (
    CONDITION_KEYS,
    MAX_WINDOW_YEARS,
    FixedRateRefused,
    _canonical_rate_text,
    _resolve_conditions,
    _resolve_pair,
    _resolve_rate,
    _resolve_window,
    _specificity,
)


def test_a_currency_outside_the_governed_vocabulary_is_refused():
    with pytest.raises(FixedRateRefused) as caught:
        _resolve_pair("USD", "XXZ")
    assert caught.value.code == "unknown_currency"
    # The refusal names the repair, not the cause.
    assert "three-letter code" in str(caught.value)


def test_a_pair_resolves_to_its_canonical_codes():
    assert _resolve_pair("usd", "eur") == ("USD", "EUR")


def test_a_non_tender_code_cannot_carry_a_posed_rate():
    """`XAU` is gold, `XDR` a basket. Neither is money a Project reports in."""
    with pytest.raises(FixedRateRefused) as caught:
        _resolve_pair("XAU", "EUR")
    assert caught.value.code == "not_legal_tender"


def test_a_binary_float_is_refused_rather_than_rounded():
    """The whole money contract: a float cannot hold a quotation exactly.

    Accepting `0.92` as a float and storing `Decimal(0.92)` would write
    0.9199999999999999289457264239899814128875732421875 and the refusal that
    exists everywhere else in this repository would have one hole, at the exact
    place a human types a number.
    """
    with pytest.raises(FixedRateRefused) as caught:
        _resolve_rate(0.92, "USD", "EUR")
    assert caught.value.code == "float_rate"


def test_a_string_rate_is_stored_exactly():
    assert _resolve_rate("0.92", "USD", "EUR") == Decimal("0.92")
    assert str(_resolve_rate("0.9200", "USD", "EUR")) == "0.9200"


@pytest.mark.parametrize("raw", ["0", "-1", "not a rate", ""])
def test_a_rate_that_is_not_a_positive_number_is_refused(raw):
    with pytest.raises(FixedRateRefused) as caught:
        _resolve_rate(raw, "USD", "EUR")
    assert caught.value.code in {"non_positive_rate", "unparsable_rate"}


def test_a_window_with_no_end_is_refused_by_name():
    """The seed's `2099-12-31` is the defect this method replaces.

    A rate posed to the end of time is what let 0.92 apply to every day from 2020
    to 2099 without anyone ever being asked again.
    """
    with pytest.raises(FixedRateRefused) as caught:
        _resolve_window("2026-01-01", None)
    assert caught.value.code == "missing_window"
    assert "2020 to 2099" in str(caught.value)


def test_an_inverted_window_is_refused():
    with pytest.raises(FixedRateRefused) as caught:
        _resolve_window("2026-06-01", "2026-01-01")
    assert caught.value.code == "window_inverted"


def test_a_window_longer_than_the_bound_is_refused():
    with pytest.raises(FixedRateRefused) as caught:
        _resolve_window("2026-01-01", f"{2026 + MAX_WINDOW_YEARS + 1}-01-01")
    assert caught.value.code == "window_too_long"


def test_a_window_is_returned_as_dates():
    assert _resolve_window("2026-01-01", "2026-12-31") == (
        date(2026, 1, 1),
        date(2026, 12, 31),
    )


def test_the_fixed_method_is_the_governed_word_and_not_direct():
    """The one-line assertion the whole change exists for."""
    from core.fx_rate_sets import METHOD_DIRECT, METHOD_FIXED

    assert METHOD_FIXED == "fixed"
    assert METHOD_FIXED != METHOD_DIRECT


# ---------------------------------------------------------------------------
# The conditional rule (migration 288) -- the spreadsheet `IF` half of the
# amendment. Validation only; resolution is proven against Postgres in
# tests/integration/test_fx_conditional_rate_postgres.py, because precedence
# between two stored rules is a query and a double would agree with whatever the
# author believed.
# ---------------------------------------------------------------------------


def test_no_condition_is_the_ordinary_fixed_rate():
    """The unconditional rate is the DEGENERATE case, not a separate kind.

    This is the whole reason there is one tool, one writer and one precedence rule
    rather than two of each.
    """
    assert _resolve_conditions(None) == {}
    assert _resolve_conditions({}) == {}
    assert _specificity({}) == 0


def test_the_condition_vocabulary_is_borrowed_from_fee_tax_not_invented():
    """Every FX condition key that fee/tax also has must MEAN the same thing.

    If these two sets drift apart, a person who learned to write `{"country": [...]}`
    for a fee rule silently writes something else for a rate. The three shared words
    are asserted here so a rename on either side has to be deliberate.
    """
    from core.fee_tax_rules import CONDITION_KEYS as FEE_TAX_KEYS

    assert {"country", "market", "connector"} <= CONDITION_KEYS
    assert {"country", "market", "connector"} <= FEE_TAX_KEYS
    # `datastream` is the one FX addition: fee/tax carries it as a `scope_kind`
    # column, and this table has no scope column to carry it in.
    assert CONDITION_KEYS - FEE_TAX_KEYS == {"datastream"}


def test_a_key_that_qualifies_a_fee_and_not_a_rate_is_refused_by_name():
    """`tax_code` is real one table over, so "unknown key" would send a person hunting
    for a typo they did not make."""
    with pytest.raises(FixedRateRefused) as caught:
        _resolve_conditions({"tax_code": ["VAT20"]})
    assert caught.value.code == "condition_key_not_for_rates"
    assert "qualifies a fee" in str(caught.value)


def test_an_unknown_condition_key_is_refused_rather_than_ignored():
    """The fee/tax rule, kept verbatim: an unknown key is UNRESOLVED, never ignored.

    Dropping it would apply the rate far more widely than the person asked, and
    every figure it touched would still look governed.
    """
    with pytest.raises(FixedRateRefused) as caught:
        _resolve_conditions({"contry": ["FR"]})
    assert caught.value.code == "unknown_condition_key"
    # The refusal names the repair.
    assert "country" in str(caught.value)


def test_a_condition_value_must_be_a_list_even_when_there_is_one():
    with pytest.raises(FixedRateRefused) as caught:
        _resolve_conditions({"country": "FR"})
    assert caught.value.code == "condition_value_not_a_list"


def test_a_key_admitting_no_value_is_refused():
    """It would match nothing, so the rate could never apply -- a rule that is inert
    and looks governed."""
    with pytest.raises(FixedRateRefused) as caught:
        _resolve_conditions({"country": []})
    assert caught.value.code == "empty_condition_value"


def test_conditions_are_canonicalised_so_one_rule_is_one_rule():
    """Order and duplicates must not mint a rival authority.

    Migration 288 keys uniqueness on a digest of the canonical jsonb, so without this
    `["FR","BE"]` and `["BE","FR"]` would be two rules that match together and tie
    forever afterwards.
    """
    assert _resolve_conditions({"country": ["FR", "BE"]}) == {"country": ["BE", "FR"]}
    assert _resolve_conditions({"country": ["FR", "FR", " FR "]}) == {"country": ["FR"]}


def test_specificity_counts_the_keys_a_rule_declares():
    """The count IS the precedence rank -- see resolve_rate."""
    assert _specificity({"country": ["FR"]}) == 1
    assert _specificity({"country": ["FR"], "connector": ["google_ads"]}) == 2


def test_a_rate_hashes_the_same_before_and_after_a_round_trip_through_storage():
    """The replay bug this helper exists for, pinned.

    `rate` is NUMERIC(38, 18): 0.95 goes in and 0.950000000000000000 comes back. When
    the content hash used `str()`, a rate that had been carried forward into a later
    version stopped matching itself, so re-posting it identically minted a rival
    version instead of replaying -- the exact duplication the hash exists to prevent.
    """
    assert _canonical_rate_text("0.95") == _canonical_rate_text(
        Decimal("0.950000000000000000")
    )
    assert _canonical_rate_text(Decimal("0.9200")) == _canonical_rate_text("0.92")
