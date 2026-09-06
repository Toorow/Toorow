"""One refusal, one sentence, one owner -- story 67.9.

THE DEFECT THIS EXISTS TO CATCH. `_comparison_windows` refuses a period
comparison on three conditions. The Explore door must know those three
conditions before the round trip, because a question that cannot be answered
must not be asked -- and on 2026-08-22 it was given them by RETYPING the three
sentences into `ui/admin/src/analyze/QueryDoor.tsx`, tails adapted. The commit
that did it claimed, in its own comment, that "the sentences are the server's,
not translations of them". They were translations. From that day the door said
*"Choose them, or set comparison to none"* and the screen said *"Choose them
first"* about the same refusal, and nothing in the repository would have gone
red the day the validator changed its words.

WHY A FILE AND NOT A REVIEW. The defect is not in either side; it is in the
RELATION between them, and a relation is not read by anyone editing one file.
The same shape is already guarded for the capability labels
(`test_capability_labels_have_one_spelling.py`) and for the same reason.

WHAT IS CHECKED, AND WHY EACH PART IS NEEDED:

  1. the console holds NO copy of any of the sentences -- the copy is the defect;
  2. the console names EXACTLY the conditions the validator declares -- a screen
     testing a fourth condition, or missing one, silently reopens the question;
  3. the facets response, the one transport that carries the sentences to the
     console, composes them from the declaration rather than retyping them.

Test 1 alone would pass on a console that had simply stopped explaining itself.
"""

from __future__ import annotations

import re
from pathlib import Path

from core.query_specs import COMPARISON_PRECONDITIONS

ROOT = Path(__file__).resolve().parents[3]
VALIDATOR = ROOT / "server" / "core" / "query_specs.py"
TRANSPORT = ROOT / "server" / "core" / "analyze_workbench.py"
CONSOLE = ROOT / "ui" / "admin" / "src"
SCREEN = CONSOLE / "analyze" / "QueryDoor.tsx"

#: The head of each sentence, short enough to survive a reflow of the literal in
#: either language and long enough that no other sentence in the product starts
#: with it. A whole-sentence search would miss a copy that had already drifted --
#: and a copy that has drifted is exactly what is being hunted.
_SENTENCE_HEAD = "A period comparison"


def _console_sources() -> list[Path]:
    return [
        path
        for path in CONSOLE.rglob("*.ts*")
        if "__tests__" not in path.parts and "node_modules" not in path.parts
    ]


def test_the_console_phrases_no_comparison_refusal_of_its_own() -> None:
    """No source file of the console writes one of these sentences.

    Test files are exempt: a fixture STANDS IN for the server payload, and one
    that carried no sentence could not prove the screen reads it.
    """
    offenders = [
        str(path.relative_to(ROOT)).replace("\\", "/")
        for path in _console_sources()
        if _SENTENCE_HEAD in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], (
        "these console files phrase a comparison refusal themselves; the sentence "
        f"belongs to `query_specs.COMPARISON_PRECONDITIONS`: {offenders}"
    )


def _slice(text: str, opening: str) -> str:
    """The declaration that starts at `opening`, up to its terminating `;`."""
    start = text.index(opening)
    return text[start : text.index(";", start)]


def test_the_screen_tests_exactly_the_conditions_the_validator_declares() -> None:
    """The screen's condition names are the declared ones, no more and no less.

    Read out of the ONE declaration that computes them, not out of the whole
    file: a set gathered from every literal in a 500-line component would match
    by accident and stop being a measurement.
    """
    declared = {precondition.condition for precondition in COMPARISON_PRECONDITIONS}
    body = _slice(SCREEN.read_text(encoding="utf-8"), "const unmetComparisonCondition")
    named = set(re.findall(r'"([a-z_]+)"', body))
    assert named == declared, (
        "the Explore door and the validator disagree about which conditions stop a "
        f"period comparison: screen={sorted(named)} validator={sorted(declared)}"
    )


def test_the_facets_response_serves_the_declaration_rather_than_a_copy() -> None:
    """`analyze_workbench` reads the table; it does not retype the sentences."""
    text = TRANSPORT.read_text(encoding="utf-8")
    assert "COMPARISON_PRECONDITIONS" in text
    assert _SENTENCE_HEAD not in text


def test_the_refusing_function_carries_no_sentence_of_its_own() -> None:
    """A second copy inside the validator is the same defect, one file earlier.

    `_comparison_windows` is where the three sentences used to be typed. It now
    refuses through `_comparison_refusal`, so the only place these words exist is
    the declaration -- and a fourth copy re-appearing here turns this red.
    """
    text = VALIDATOR.read_text(encoding="utf-8")
    start = text.index("def _comparison_windows(")
    body = text[start : text.index("\ndef ", start + 1)]
    assert _SENTENCE_HEAD not in body
