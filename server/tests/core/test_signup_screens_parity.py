"""The doors that create a Project must ask the same questions.

Jean, 2026-08-03: *"il doit etre identique oui"*. `/onboarding` (CreateOrg) and
the self-hosted instance claim (ClaimInstance) post the SAME payload to the same
kind of confirmation route. A field on one and not the other means the product
answers a person differently depending on which door they came through — and the
one that lacks it silently records a platform default as a human decision, which
is the defect this pair of screens has just been repaired for.

2026-08-04 — a THIRD door was never in this list: `CreateProject`, reached when
an organization exists with no project. It asked neither question and decided
both by itself, which is exactly what parity between the other two was written
to prevent. It is asserted here now.

The parity is no longer textual per screen. The two fields, the payload rules and
the confirmation wording live in ONE module (`reportingDefaults.tsx`), so drifting
apart means deleting an import rather than forgetting a line. What is asserted
per door is that it mounts those fields and confirms them; what is asserted once
is what they say.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PAGES = Path(__file__).resolve().parents[3] / "ui" / "admin" / "src" / "shell" / "pages"
SCREENS = ("CreateOrg.tsx", "ClaimInstance.tsx", "CreateProject.tsx")
SHARED = "reportingDefaults.tsx"


def _source(screen: str) -> str:
    return (PAGES / screen).read_text(encoding="utf-8")


def _shared() -> str:
    return (PAGES / SHARED).read_text(encoding="utf-8")


def _code(source: str) -> str:
    """The source with comments removed.

    These files explain the defect they were repaired for, so the forbidden
    construct appears in the prose that forbids it. A guard that cannot tell a
    quotation from a call fires on its own explanation -- which is how a real
    guard gets weakened into a looser pattern.
    """
    without_blocks = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", without_blocks, flags=re.MULTILINE)


@pytest.mark.parametrize("screen", SCREENS)
def test_every_door_mounts_the_two_shared_fields(screen: str):
    source = _source(screen)
    assert "<ReportingCurrencyField" in source
    assert "<ReportingTimezoneField" in source


@pytest.mark.parametrize("screen", SCREENS)
def test_no_door_rolls_its_own_control_for_these_vocabularies(screen: str):
    """The truncation defect came from a hand-rolled `<select>` per screen.

    `/api/reference/*` is a typeahead capped at 50 rows: a `<select>` filled from
    it renders 50 of 156 currencies and 50 of 313 zones, alphabetically. The
    ratified searchable control is mounted once, in the shared module, so no
    screen can quietly go back to a capped page of the vocabulary.
    """
    code = _code(_source(screen))
    assert "/api/reference/" not in code
    assert "<select" not in code


def test_the_shared_module_uses_the_ratified_searchable_control():
    shared = _shared()
    assert "ReferenceSelect" in shared
    assert "/api/reference/currencies" in shared
    assert "/api/reference/timezones" in shared
    # A `<select>` here would defeat the whole point, exactly as it did per screen.
    assert "<select" not in _code(shared)


def test_the_two_questions_are_worded_once():
    shared = _shared()
    assert "Reporting currency" in shared
    assert "Reporting timezone" in shared
    assert "Suggested by your browser" in shared


@pytest.mark.parametrize("screen", SCREENS)
def test_no_door_sends_an_unchosen_value(screen: str):
    # Omitted, never sent empty: the server reads an ABSENT key as a platform
    # default, and an empty string would be a third shape to interpret. The rule
    # is applied in one place, so a door can only get it wrong by not calling it.
    assert "reportingPayload(" in _source(screen)


@pytest.mark.parametrize("screen", SCREENS)
def test_every_door_offers_the_browser_zone_as_a_suggestion(screen: str):
    # The guess is handed to the field so it can be SHOWN as a guess, and to the
    # payload builder so it travels as `timezone_suggestion`.
    assert "suggestedZone={suggestedZone}" in _source(screen)


@pytest.mark.parametrize("screen", SCREENS)
def test_every_door_confirms_what_it_asked_for(screen: str):
    """A question asked and never read back is a decision nobody agreed to."""
    source = _source(screen)
    # The summary block lost its `scope-summary` class name when the console
    # moved to utilities (console-visual wave 2, 59aee226): it is now the
    # `SCOPE_SUMMARY` constant each door composes. Either spelling is the block;
    # what is measured is that the block exists and reads both answers back.
    assert 'className="scope-summary"' in source or "className={SCOPE_SUMMARY}" in source
    assert "currencySummary(" in source
    assert "timezoneSummary(" in source
