"""A Project default must carry the provenance it earned -- AI-77 / AI-81.

Why this file exists
--------------------
The completeness ledger closed `project-settings [9]` with two proofs, and
neither touched the write path:

* `test_project_settings_constraints.py`, whose
  `test_migration_never_seeds_suggestions_as_confirmed_defaults`
  greps migration 131 for the literal ``DEFAULT 'suggestion'`` -- it asserted
  that everything is *born* labelled a suggestion, pinning the defect as a
  contract;
* `ProjectSettingsPage.test.tsx` feeds the screen a hand-written fixture
  ``{pending: "EUR", origin: "suggestion"}`` and asserts the screen renders it,
  which proves the screen renders what the test handed it.

So the tests below drive the decision that actually runs in production, and the
parameters that reach the INSERT. The regression they guard is specific: a value
nobody supplied must never be written as a `suggestion`.
"""

from __future__ import annotations

import pytest
from core.project_provenance import (
    ORIGIN_DEFAULT,
    ORIGIN_OPERATOR,
    ORIGIN_SUGGESTION,
    PLATFORM_FALLBACK_CURRENCY,
    PLATFORM_FALLBACK_TIMEZONE,
    decide_currency,
    decide_timezone,
    derive_from_connected_sources,
    preferences_insert_params,
)


def test_an_absent_currency_is_a_named_default_not_a_suggestion() -> None:
    """The exact defect AI-77 names: nothing suggested EUR."""
    decision = decide_currency(None)

    assert decision.value == PLATFORM_FALLBACK_CURRENCY
    assert decision.origin == ORIGIN_DEFAULT
    assert decision.origin != ORIGIN_SUGGESTION
    assert decision.evidence is None


def test_an_absent_timezone_is_a_named_default_not_a_suggestion() -> None:
    decision = decide_timezone(None)

    assert decision.value == PLATFORM_FALLBACK_TIMEZONE
    assert decision.origin == ORIGIN_DEFAULT


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_choice_is_no_choice(blank: str) -> None:
    """An empty string is absence, not an operator decision."""
    assert decide_currency(blank).origin == ORIGIN_DEFAULT
    assert decide_timezone(blank).origin == ORIGIN_DEFAULT


def test_an_operator_value_is_recorded_as_an_operator_value() -> None:
    decision = decide_currency("usd")

    assert decision.value == "USD"  # ISO-4217 is upper-case
    assert decision.origin == ORIGIN_OPERATOR
    assert decision.evidence is None


def test_an_operator_choice_outranks_a_suggestion() -> None:
    decision = decide_currency(
        "USD", suggestion="GBP", suggestion_evidence="ad account GB-1 declares GBP"
    )

    assert decision.value == "USD"
    assert decision.origin == ORIGIN_OPERATOR


def test_a_suggestion_is_accepted_only_with_the_source_that_made_it() -> None:
    decision = decide_currency(
        None, suggestion="GBP", suggestion_evidence="ad account GB-1 declares GBP"
    )

    assert decision.value == "GBP"
    assert decision.origin == ORIGIN_SUGGESTION
    assert decision.evidence == "ad account GB-1 declares GBP"


def test_claiming_a_suggestion_without_naming_its_source_is_refused() -> None:
    """This guard is what stops C-3 from being reintroduced by a later caller."""
    with pytest.raises(ValueError, match="must name its source"):
        decide_currency(None, suggestion="GBP")

    with pytest.raises(ValueError, match="must name its source"):
        decide_timezone(None, suggestion="Europe/Berlin", suggestion_evidence="  ")


def test_nothing_can_suggest_a_currency_yet_and_the_reason_is_returned() -> None:
    """The derivation has no evidence store; it says so instead of inventing one."""
    value, reason = derive_from_connected_sources(conn=None, project_id="proj_EXAMPLE")

    assert value is None
    assert "no store" in reason


def test_the_insert_parameters_carry_the_decided_origin_not_a_literal() -> None:
    """Pins the tuple that reaches app.project_preferences on the real path."""
    params = preferences_insert_params(
        "proj_EXAMPLE", decide_currency(None), decide_timezone("Europe/Berlin")
    )

    project_id, currency, tz, currency_origin, currency_status, tz_origin, tz_status = params

    assert project_id == "proj_EXAMPLE"
    assert (currency, currency_origin) == (PLATFORM_FALLBACK_CURRENCY, ORIGIN_DEFAULT)
    assert (tz, tz_origin) == ("Europe/Berlin", ORIGIN_OPERATOR)
    # Whatever the origin, creation never confirms.
    assert currency_status == "unconfirmed"
    assert tz_status == "unconfirmed"


def test_no_project_creation_path_writes_a_hardcoded_suggestion() -> None:
    """Class guard: the four creation paths must not re-inline the old literal.

    The defect was not one bad line, it was the same bad line copied into every
    path that creates a Project. This scans them for the exact shape that was
    removed, so a fifth path cannot reintroduce it silently.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    creation_paths = [
        root / "server" / "core" / "admin_api.py",
        root / "server" / "core" / "hosted_entry_scope.py",
        root / "server" / "core" / "self_hosted_instance_claim.py",
    ]

    offenders: list[str] = []
    for path in creation_paths:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue  # the comments explain the removed defect on purpose
            stripped = line.replace(" ", "")
            if "'suggestion','unconfirmed'" in stripped:
                offenders.append(f"{path.name}:{number}")
            if 'or"EUR"' in stripped or "or'EUR'" in stripped:
                offenders.append(f"{path.name}:{number}")
            if '"Europe/Paris")' in line and "body.get" in line:
                offenders.append(f"{path.name}:{number}")

    assert offenders == [], (
        "a Project-creation path inlines a currency/timezone default or a literal "
        f"'suggestion' origin again instead of core.project_provenance: {offenders}"
    )
