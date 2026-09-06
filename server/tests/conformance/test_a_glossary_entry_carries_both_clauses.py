"""Conformance -- a glossary entry names its question AND what it is not.

`glossary.md` states the rule twice. Once in its own `Incomplete if` list:

    An entry states what an object is without stating what it is **not**, or
    without naming the question it answers -- those two lines are what let an
    agent choose the right object instead of the nearest-sounding one.

and once, as a clause of its own, in the § *Derived 2026-08-31* note, where it
adds the half that the list alone does not carry:

    A mention is not an entry: this page's own `Incomplete if` asks for the
    question an object answers *and* for what it is not, and a subordinate clause
    in another object's paragraph carries neither.

Both criteria were unmeasured. Nothing read the page, so an entry could ship with
one clause -- or an object could be introduced as a sentence inside a neighbour's
paragraph -- and the only instrument was somebody remembering.

WHAT AN ENTRY IS, AND WHY IT IS DECLARED FROM THE OTHER SIDE. The obvious guard
reads every `###` block that contains `**What it is.**` and checks the other two
clauses. It has a hole exactly the size of the defect: an entry written without
`**What it is.**` is not judged at all, so the cheapest way past the guard is to
write less. So the page's NON-entries are the declared list, and every other `###`
heading is an entry that owes all three clauses.

WHY THE CLAUSE IS MATCHED BY SHAPE AND NOT BY STRING. Measured 2026-09-02: the
27 entries write the negative clause four ways -- `**What it is NOT.**`,
`**What it is not.**`, and `Alert destination`'s `**And it is NOT called
`Channel`, nor `Webhook` on its own.**`. A literal `**What it is NOT.**` search
answers "one entry is missing its clause" and sends the next person to add a
clause that is already there, under a heading that already reads well. The guard
therefore asks for a BOLD LEAD THAT NEGATES -- the shape a reader scans for --
and not for one editor's phrasing of it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
GLOSSARY = ROOT / "docs" / "product-architecture" / "glossary.md"

#: `### <title>` -- the heading level this page gives one object.
_ENTRY_HEADING = re.compile(r"^###\s+(?P<title>\S.*?)\s*$")
_ANY_HEADING = re.compile(r"^#{1,3}\s+\S")

#: A bolded lead sentence: what a reader's eye lands on when scanning an entry.
_BOLD_LEAD = re.compile(r"\*\*(?P<lead>[^*\n]+)\*\*")

#: The three clauses, matched on the lead rather than on one exact phrasing.
_STATES_WHAT_IT_IS = re.compile(r"^what it is\b(?!\s+not\b)", re.I)
_NAMES_THE_QUESTION = re.compile(r"\bquestion it answers\b", re.I)
_STATES_WHAT_IT_IS_NOT = re.compile(r"\bit is\s+not\b", re.I)

#: The `###` headings of this page that are PROSE, not objects: they settle a
#: rule or narrate a measurement, and they own no object. Declared by title so a
#: new entry can never slip through by leaving a clause out -- everything not
#: named here is an entry and owes all three clauses.
PROSE_SECTIONS = frozenset(
    {
        "The rule this settles",
        "The Connector's identifier is spelled three ways, and only one of them is the token",
    }
)

#: The three objects the page itself names as having lived as a subordinate
#: clause inside somebody else's entry (§ *Derived 2026-08-31*). They are the
#: worked example of "a mention is not an entry", so they are pinned by name.
ONCE_ONLY_MENTIONED = ("Query Spec", "Visualization", "Value Mapping Table")


def _entries() -> dict[str, str]:
    """`### <title>` -> the text of that block, up to the next heading of any level."""
    lines = GLOSSARY.read_text(encoding="utf-8", errors="replace").splitlines()
    starts = [
        (i, m.group("title"))
        for i, line in enumerate(lines)
        if (m := _ENTRY_HEADING.match(line))
    ]
    out: dict[str, str] = {}
    for index, title in starts:
        end = len(lines)
        for k in range(index + 1, len(lines)):
            if _ANY_HEADING.match(lines[k]):
                end = k
                break
        assert title not in out, f"two `### {title}` blocks -- an entry is not unique"
        out[title] = "\n".join(lines[index + 1 : end])
    return out


def _leads(body: str) -> list[str]:
    return [m.group("lead") for m in _BOLD_LEAD.finditer(body)]


def _object_entries() -> dict[str, str]:
    return {t: b for t, b in _entries().items() if t not in PROSE_SECTIONS}


def _missing_clauses(body: str) -> list[str]:
    leads = _leads(body)
    missing = []
    if not any(_STATES_WHAT_IT_IS.search(lead) for lead in leads):
        missing.append("what it is")
    if not any(_NAMES_THE_QUESTION.search(lead) for lead in leads):
        missing.append("the question it answers")
    if not any(_STATES_WHAT_IT_IS_NOT.search(lead) for lead in leads):
        missing.append("what it is NOT")
    return missing


def test_the_declared_prose_sections_are_all_still_there() -> None:
    """An exemption naming a heading that moved would silently widen itself."""
    titles = set(_entries())
    stale = sorted(PROSE_SECTIONS - titles)
    assert not stale, (
        "PROSE_SECTIONS exempts a heading this page no longer has -- re-read it "
        f"before trusting this file: {stale}"
    )


def test_the_page_still_has_the_entries_this_guard_is_for() -> None:
    """A parametrized sweep over an empty set is a green that measured nothing."""
    entries = _object_entries()
    assert len(entries) >= 20, f"only {len(entries)} object entries parsed -- parser drift"


@pytest.mark.parametrize("title", sorted(_object_entries()))
def test_every_object_entry_carries_all_three_clauses(title: str) -> None:
    """What it is, the question it answers, and what it is NOT -- each one bolded.

    The failure names the missing clause, not the rule: the repair is to write the
    line, and the person reading this needs to know which line.
    """
    missing = _missing_clauses(_object_entries()[title])
    assert not missing, (
        f"glossary entry `{title}` states no {' and no '.join(missing)}. "
        "An entry that says only what an object is lets a model pick the "
        "nearest-sounding object instead of the right one."
    )


@pytest.mark.parametrize("name", ONCE_ONLY_MENTIONED)
def test_a_mention_inside_another_entry_is_not_an_entry(name: str) -> None:
    """These three lived as a subordinate clause in a neighbour's paragraph.

    The page says so itself and says why it is not enough. Each now owns a
    heading, and its clauses are read from ITS block -- the parser bounds a block
    at the next heading, so a clause written in the host entry can never be
    counted for the guest.
    """
    entries = _object_entries()
    assert name in entries, (
        f"`{name}` has no `### {name}` entry of its own; a mention inside another "
        "object's paragraph carries neither the question it answers nor what it "
        "is not"
    )
    assert not _missing_clauses(entries[name])


def test_the_guard_can_actually_go_red() -> None:
    """Negative control, on synthetic text: a rule that cannot fail is decor.

    Both halves are proved -- the entry missing its negative clause is caught,
    and the entry whose clause is only spelled differently is NOT, because a
    guard that fails on `Alert destination` sends the next person to rewrite a
    heading that is already correct.
    """
    half = "**What it is.** A thing.\n\n**The question it answers.** *Which?*"
    assert _missing_clauses(half) == ["what it is NOT"]

    canonical = half + "\n\n**What it is NOT.** Another thing."
    assert _missing_clauses(canonical) == []

    variant = half + "\n\n**And it is NOT called `Channel`.** One word, one meaning."
    assert _missing_clauses(variant) == []

    silent = "**What it is.** A thing."
    assert _missing_clauses(silent) == ["the question it answers", "what it is NOT"]
