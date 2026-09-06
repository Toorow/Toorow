"""Onboarding must not fabricate a value and then call it a human decision.

Reported by Jean, verified line by line, and every clause was true:

  * `currency: "EUR"` was hardcoded in both sign-up screens;
  * the timezone came from `Intl.DateTimeFormat().resolvedOptions()`;
  * neither was ever shown on the form;
  * `project_provenance.decide()` marks ANY non-empty requested value
    `operator` — "an explicit human value", in its own docstring — so the
    fabricated pair was recorded as a human decision;
  * the creation route accepted SEVEN currencies while Project Settings offers
    the whole ISO 4217 list.

The last one is the sharpest, because provenance is what tells a reader whether
a number can be trusted. `currency_refusal.py:25` states AD-9: *a missing
currency is a surfaced GAP, never a default (EUR/USD/anything)*.

Note where the defect was NOT: all three server paths already carried the
AI-77/AI-81 comment — *"`None` means nobody chose, and it must reach the
decision as None so a fallback is never labelled as a choice"*. The server was
right for months; the console defeated it by never sending None.
"""

from __future__ import annotations

from pathlib import Path

import core.projects_api as projects_api  # AD-43 : le handler vit chez son sujet
import pytest
from core.project_provenance import (
    ORIGIN_DEFAULT,
    ORIGIN_OPERATOR,
    ORIGIN_SUGGESTION,
    decide_currency,
    decide_timezone,
)

UI = Path(__file__).resolve().parents[3] / "ui" / "admin" / "src" / "shell" / "pages"
# CreateProject.tsx joined this list on 2026-08-04. It is the THIRD door that
# creates a Project, it was never checked here, and it carried the exact pair
# this file exists to forbid -- `currency: "EUR"` and the browser zone posted as
# `timezone`. The two guards below missed it on indentation alone (its literal
# sat ten spaces in, the guards matched four and eight), which is why the zone
# guard is now structural: no screen reads the browser clock at all.
SIGNUP_SCREENS = ("CreateOrg.tsx", "ClaimInstance.tsx", "CreateProject.tsx")
SHARED_RULES = "reportingDefaults.tsx"


@pytest.mark.parametrize("screen", SIGNUP_SCREENS)
def test_no_signup_screen_fabricates_a_currency(screen: str):
    source = (UI / screen).read_text(encoding="utf-8")
    # A currency nobody typed must not travel as one somebody chose. The word may
    # appear in a comment explaining why; it must not appear as a payload value.
    assert 'currency: "EUR"' not in source
    assert 'currency: "USD"' not in source


@pytest.mark.parametrize("screen", SIGNUP_SCREENS)
def test_the_browser_zone_travels_as_a_suggestion_not_as_a_choice(screen: str):
    source = (UI / screen).read_text(encoding="utf-8")
    # Structural, not textual: a screen does not read the browser clock at all.
    # One module builds the payload, and it is the only place the guess exists,
    # so no amount of indentation can smuggle it back in as an operator value.
    assert "resolvedOptions" not in source
    assert "reportingPayload(" in source


def test_one_module_decides_how_the_two_defaults_travel():
    shared = (UI / SHARED_RULES).read_text(encoding="utf-8")
    # The browser guess leaves under the suggestion key, unconditionally.
    assert "timezone_suggestion: suggestedZone || null" in shared
    # An unchosen value is OMITTED, not sent empty: both keys are conditional on
    # somebody having actually chosen. A `timezone` key that is not guarded is
    # how a guess gets recorded as an operator decision.
    assert "...(chosenCurrency ? { currency: chosenCurrency } : {})" in shared
    assert "...(chosenTimezone ? { timezone: chosenTimezone } : {})" in shared


@pytest.mark.parametrize("screen", SIGNUP_SCREENS)
def test_a_default_collected_is_a_default_shown_back(screen: str):
    """A field asked for and never read back is a decision nobody agreed to.

    All three doors ask for the reporting currency and the reporting timezone;
    CreateOrg confirmed a scope that omitted both, ClaimInstance had no summary
    at all, and CreateProject asked for neither. The confirmation hash certifies
    what the operator reviewed (`hosted_entry_scope.confirmed_payload`), so what
    it reviewed has to contain them.
    """
    source = (UI / screen).read_text(encoding="utf-8")
    assert "currencySummary(" in source
    assert "timezoneSummary(" in source


def test_the_summaries_name_the_origin_rather_than_flattening_it():
    shared = (UI / SHARED_RULES).read_text(encoding="utf-8")
    # Three origins the server distinguishes, three sentences a person can read.
    assert "your choice" in shared
    assert "suggested by your browser" in shared
    assert "platform default" in shared


def test_the_platform_fallbacks_are_served_not_copied_into_the_front():
    """The front names the default it is about to accept — from the server.

    Hardcoding "EUR" in a screen to *display* it would recreate, in the label,
    the drift this module removed from the payload.
    """
    from core.project_provenance import PLATFORM_FALLBACK_CURRENCY, PLATFORM_FALLBACK_TIMEZONE
    from core.reference_vocabulary_api import _currencies, _timezones

    api = (
        Path(__file__).resolve().parents[2] / "core" / "reference_vocabulary_api.py"
    ).read_text(encoding="utf-8")
    assert "PLATFORM_FALLBACK_CURRENCY" in api
    assert "PLATFORM_FALLBACK_TIMEZONE" in api
    assert callable(_currencies) and callable(_timezones)

    shared = (UI / SHARED_RULES).read_text(encoding="utf-8")
    assert "platform_default" in shared
    assert f'"{PLATFORM_FALLBACK_CURRENCY}"' not in shared
    assert f'"{PLATFORM_FALLBACK_TIMEZONE}"' not in shared


def test_an_absent_currency_is_a_default_and_never_an_operator_choice():
    decision = decide_currency(None)
    assert decision.origin == ORIGIN_DEFAULT
    assert decision.origin != ORIGIN_OPERATOR


def test_a_suggested_zone_is_a_suggestion_and_names_its_source():
    decision = decide_timezone(
        None, suggestion="Europe/Paris", suggestion_evidence="browser IANA zone"
    )
    assert decision.origin == ORIGIN_SUGGESTION
    assert decision.value == "Europe/Paris"
    assert decision.evidence  # a suggestion that names nothing is refused below


def test_a_suggestion_without_evidence_is_refused_rather_than_believed():
    with pytest.raises(ValueError):
        decide_timezone(None, suggestion="Europe/Paris")


def test_a_typed_currency_is_still_an_operator_choice():
    # The repair must not swing the other way: an explicit human value keeps its
    # origin, because that is the one case where `operator` is true.
    assert decide_currency("jpy").origin == ORIGIN_OPERATOR
    assert decide_currency("jpy").value == "JPY"


def test_every_project_creation_door_reads_the_suggestion_the_same_way():
    """Three doors create a Project; the browser guess must mean one thing.

    `POST /api/projects` ignored `timezone_suggestion` entirely while the two
    ENTRY doors honoured it. So a screen that sent an honest suggestion had it
    dropped and landed on the platform fallback, and the only way to make the
    zone stick was to send it as `timezone` — a browser guess recorded as an
    operator decision, which is the defect this file exists to forbid.

    The evidence string is compared too: the same guess recorded under two
    different names is two provenances for one fact.
    """
    import inspect

    from core import hosted_entry_scope, self_hosted_instance_claim

    doors = (
        inspect.getsource(projects_api._create_project),
        inspect.getsource(hosted_entry_scope.create_hosted_entry_scope),
        inspect.getsource(self_hosted_instance_claim.claim_self_hosted_instance),
    )
    for source in doors:
        assert "timezone_suggestion" in source
        assert "suggestion_evidence" in source
        assert "browser IANA zone reported at sign-up" in source


def test_the_reference_vocabularies_publish_the_fallback_they_fall_back_to():
    import inspect

    from core.project_provenance import PLATFORM_FALLBACK_CURRENCY, PLATFORM_FALLBACK_TIMEZONE
    from core.reference_vocabulary_api import _currencies, _timezones

    currencies = inspect.getsource(_currencies)
    timezones = inspect.getsource(_timezones)
    assert '"platform_default": PLATFORM_FALLBACK_CURRENCY' in currencies
    assert '"platform_default": PLATFORM_FALLBACK_TIMEZONE' in timezones
    # And the constants stay valid members of the vocabularies they belong to.
    from core.currency_vocabulary import load_currency_vocabulary

    assert PLATFORM_FALLBACK_CURRENCY in {c.code.upper() for c in load_currency_vocabulary()}
    from zoneinfo import ZoneInfo

    assert ZoneInfo(PLATFORM_FALLBACK_TIMEZONE) is not None


def test_creation_accepts_the_whole_vocabulary_project_settings_offers():
    from core.currency_vocabulary import load_currency_vocabulary
    from core.projects_api import _project_currencies  # noqa: PLC0415

    accepted = _project_currencies()
    vocabulary = {c.code.upper() for c in load_currency_vocabulary()}
    assert accepted == vocabulary
    # The seven-code allowlist is gone: a project could otherwise be given a
    # currency in Project Settings that its own creation route refuses.
    assert len(accepted) > 7
    assert {"JPY", "SEK", "BRL"} <= accepted
