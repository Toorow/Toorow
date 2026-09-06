"""The four international vocabularies answer the SAME way, or the seam is useless.

These run against the REAL governed seeds, never a fixture: the whole value of a
fixed vocabulary is that it holds the conventions external tools actually use, and
a stubbed registry would prove the dispatcher and not the recognition.
"""

from __future__ import annotations

import pytest
from core.vocabulary_normalization import (
    KIND_COUNTRY,
    KIND_CURRENCY,
    KIND_LANGUAGE,
    KIND_TIMEZONE,
    MATCH_ALIAS,
    MATCH_CANONICAL,
    MATCH_LINK,
    REASON_AMBIGUOUS_ABBREVIATION,
    REASON_EMPTY,
    REASON_NOT_A_STRING,
    REASON_NOT_IN_VOCABULARY,
    REASON_UNKNOWN_KIND,
    VOCABULARY_KINDS,
    normalize,
    normalize_values,
)


@pytest.mark.parametrize(
    ("kind", "raw", "canonical"),
    [
        (KIND_COUNTRY, "FR", "FR"),
        (KIND_CURRENCY, "EUR", "EUR"),
        (KIND_LANGUAGE, "fr", "fr"),
        (KIND_TIMEZONE, "Europe/Paris", "Europe/Paris"),
    ],
)
def test_every_axis_recognises_its_own_canonical_code(kind, raw, canonical):
    result = normalize(kind, raw)
    assert result.canonical_value == canonical
    assert result.matched_on == MATCH_CANONICAL
    assert result.reason is None
    assert result.resolved is True


@pytest.mark.parametrize(
    ("kind", "raw", "canonical"),
    [
        # The conventions external tools actually write. This is what the alias
        # half of the governed seeds is FOR, and why the vocabularies are fixed.
        (KIND_COUNTRY, "Andorra", "AD"),
        (KIND_COUNTRY, "and", "AD"),
        (KIND_LANGUAGE, "Afrikaans", "af"),
        (KIND_LANGUAGE, "afr", "af"),
        (KIND_CURRENCY, "dirham", "AED"),
    ],
)
def test_a_tool_convention_resolves_to_the_international_code(kind, raw, canonical):
    result = normalize(kind, raw)
    assert result.canonical_value == canonical
    assert result.matched_on == MATCH_ALIAS


def test_a_timezone_written_in_another_case_is_the_same_zone():
    """`resolve_timezone` matched the governed string EXACTLY and case-sensitively.

    A CSV routinely carries `europe/paris`. That is the SAME identifier written
    differently, not a new one, and it did not resolve while `deutschland` did --
    the asymmetry this seam exists to remove.
    """
    result = normalize(KIND_TIMEZONE, "europe/paris")
    assert result.canonical_value == "Europe/Paris"


def test_a_backward_link_resolves_and_says_it_was_a_link():
    """The canonical name is what must be stored; the operator must still be able
    to say the file used the retired one."""
    result = normalize(KIND_TIMEZONE, "Europe/Kiev")
    assert result.canonical_value == "Europe/Kyiv"
    assert result.matched_on == MATCH_LINK


@pytest.mark.parametrize(
    ("abbreviation", "canonical"),
    [("CET", "Europe/Brussels"), ("WET", "Europe/Lisbon"), ("MST", "America/Phoenix")],
)
def test_an_abbreviation_the_standard_governs_resolves_as_a_link(abbreviation, canonical):
    """Measured 2026-08-08: the tz database SHIPS these as link zones.

    So recognising them is not a mapping this repository invented -- it is the
    standard's own answer, read from the governed seed. A file that writes `CET`
    is a file every spreadsheet produces, and refusing it would have made the
    vocabulary less useful than the standard it projects. The match is reported
    as a LINK, so an operator can still see the file did not use the zone name.
    """
    result = normalize(KIND_TIMEZONE, abbreviation)
    assert result.canonical_value == canonical
    assert result.matched_on == MATCH_LINK


def test_an_abbreviation_the_standard_does_NOT_govern_is_refused_by_name():
    """`CST` is US Central AND China Standard; `IST` is India, Ireland and Israel.

    The tz database ships neither, precisely because they are ambiguous. Mapping
    one here would pick a country for the operator and silently shift every day
    boundary in the file. The refusal carries its own reason, so a screen says WHY
    rather than "unknown value" -- the two call for different repairs.
    """
    for abbreviation in ("CST", "IST"):
        result = normalize(KIND_TIMEZONE, abbreviation)
        assert result.canonical_value is None
        assert result.reason == REASON_AMBIGUOUS_ABBREVIATION


def test_an_unknown_value_carries_a_reason_rather_than_a_guess():
    result = normalize(KIND_COUNTRY, "Wakanda")
    assert result.canonical_value is None
    assert result.reason == REASON_NOT_IN_VOCABULARY
    assert result.resolved is False


@pytest.mark.parametrize(
    ("raw", "reason"),
    [(None, REASON_NOT_A_STRING), (42, REASON_NOT_A_STRING), ("   ", REASON_EMPTY)],
)
def test_a_bad_value_is_an_outcome_not_an_exception(raw, reason):
    """A preview reads a client's file. It must not raise on a blank cell."""
    result = normalize(KIND_COUNTRY, raw)
    assert result.reason == reason


def test_an_unknown_axis_is_named_and_not_silently_empty():
    result = normalize("planet", "Mars")
    assert result.reason == REASON_UNKNOWN_KIND


def test_the_four_axes_are_the_four_and_each_one_resolves():
    """The registry list and the resolver table cannot drift apart.

    A fifth vocabulary added to one and forgotten in the other would return
    `unknown_vocabulary` for a value the platform does govern -- an absence that
    reads as a bad file.
    """
    assert set(VOCABULARY_KINDS) == {
        KIND_COUNTRY,
        KIND_CURRENCY,
        KIND_LANGUAGE,
        KIND_TIMEZONE,
    }
    for kind in VOCABULARY_KINDS:
        assert normalize(kind, "zzzz").reason == REASON_NOT_IN_VOCABULARY, (
            f"{kind} does not reach its governed vocabulary"
        )


def test_a_column_sample_keeps_its_order_and_its_repetitions():
    """Deduping would hide HOW OFTEN a spelling appears -- the fact that says
    whether one row is dirty or the whole file uses another convention."""
    results = normalize_values(KIND_COUNTRY, ["FR", "Andorra", "FR", "Wakanda"])
    assert [item.canonical_value for item in results] == ["FR", "AD", "FR", None]
