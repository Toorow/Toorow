"""The completeness ledger's own rule, held by a test instead of by goodwill.

`docs/product-architecture/completeness-ledger.json` states it in its `_README`:
*"Evidence is the command, route or screen that produced the verdict — not an
assertion."* `finished_work_audit.py` reads the same file and asks only whether
the verdict is `false` and the evidence string is non-empty, so until now the
rule was written down and enforced by nothing.

`scripts/ledger_verdict_census.py` classifies every recorded verdict by the form
of its evidence and carries the ratchet. This file is the gate around it: one
test that the ratchet holds, and three that the classifier itself has not gone
soft — a classifier that answered `command` to everything would keep the ratchet
green forever while the ledger filled with prose.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ledger_verdict_census as census  # noqa: E402


def test_no_new_verdict_closes_a_criterion_on_prose_alone() -> None:
    verdicts = census.census()
    prose = {v.identity for v in verdicts if v.form == census.PROSE}
    new = sorted(prose - census.PROSE_VERDICTS_AT_2026_08_31)
    gone = sorted(census.PROSE_VERDICTS_AT_2026_08_31 - prose)

    assert not new, (
        f"{len(new)} verdict(s) close a ratified criterion with no command "
        f"anyone can re-run:\n  " + "\n  ".join(new) + "\n\n"
        "The ledger's own rule asks for the command, route or screen that "
        "produced the verdict. Write it, or record the verdict as still open."
    )
    assert not gone, (
        f"{len(gone)} baselined prose verdict(s) now carry a command — the debt "
        "went down and the list must follow it:\n  " + "\n  ".join(gone) + "\n\n"
        "Delete these lines from PROSE_VERDICTS_AT_2026_08_31 in "
        "scripts/ledger_verdict_census.py. The ratchet turns one way."
    )


def test_the_census_really_read_the_ledger() -> None:
    """A floor: an empty read would make every assertion above vacuously true.

    This floor used to include `len(PROSE_VERDICTS_AT_2026_08_31) >= 1`, written
    when the baseline held 165 identities and emptying it was a distant idea. On
    2026-09-01 AI-334 emptied it, and that assertion turned the ratchet's own
    SUCCESS into a red — a guard that forbids the state it exists to reach.

    The floor it was standing in for is kept, and now says what it means: the
    census must have read the real file (700+ verdicts over 25+ surfaces) AND
    still be telling the forms apart on it. A classifier that collapsed to one
    answer would satisfy the counts and is refused here; that it can still
    produce PROSE at all is held by
    `test_the_classifier_separates_a_command_from_a_sentence` below, which does
    not depend on the ledger containing any.
    """
    verdicts = census.census()
    assert len(verdicts) >= 700, len(verdicts)
    surfaces = {v.surface for v in verdicts}
    assert len(surfaces) >= 25, sorted(surfaces)
    forms = {v.form for v in verdicts}
    assert {census.COMMAND, census.TRAVERSAL} <= forms, sorted(forms)


def test_the_classifier_separates_a_command_from_a_sentence() -> None:
    """The three forms, on their smallest honest examples."""
    assert census.classify(
        "cd server && uv run pytest tests/core/test_platform_clocks.py -q -> 9 passed"
    ) == census.COMMAND
    assert census.classify(
        "python scripts/object_coverage_audit.py --gate -> 0"
    ) == census.COMMAND
    assert census.classify(
        "The panel reads the authority and the write goes through the governed seam."
    ) == census.PROSE
    assert census.classify(
        "curl -s -o /dev/null -w '%{http_code}' https://mcp-server-abc-ew.a.run.app"
        "/api/projects/proj_EXAMPLE/datastreams -> 401, revision mcp-server-00223-km2"
    ) == census.TRAVERSAL


def test_a_sentence_about_production_is_not_a_traversal_of_it() -> None:
    """The blind spot this classifier refuses to have.

    Six recorded verdicts use the word `production` to say the opposite of a
    traversal. Matching the word would file them as proof of a path nobody
    walked, so an ADDRESS is required, and a result read back from it.
    """
    for sentence in (
        "Both work queues are drained one unit per invocation, and the loop is "
        "dead in production.",
        "The `.py` path is NOT armed in production -- no deployment declares the runner.",
        "nothing here is a claim about production; the runtime is the local one.",
    ):
        assert census.classify(sentence) != census.TRAVERSAL, sentence
    # And an address with nothing read back from it is still not a traversal.
    assert census.classify(
        "The console is served from https://toorow-app.web.app."
    ) != census.TRAVERSAL


def test_a_verdict_recorded_against_no_criterion_is_reported() -> None:
    """`organization-settings[0_note]` closes nothing and looked like a verdict.

    `finished_work_audit.evaluate` reads `str(index)` only, so a key of any other
    shape is invisible to the audit while sitting in the ledger among real
    verdicts. The census flags them; this pins that it keeps doing so.
    """
    verdicts = census.census()
    addressed = [v for v in verdicts if v.addresses_a_criterion]
    assert len(addressed) >= 700, len(addressed)
    for verdict in verdicts:
        if not verdict.key.isdigit():
            assert not verdict.addresses_a_criterion, verdict.identity
