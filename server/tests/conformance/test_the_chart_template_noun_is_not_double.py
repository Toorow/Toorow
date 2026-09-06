"""Conformance -- one ratified object, one noun: `Chart Template`.

`visualization-and-rendering.md` § *Amendment, 2026-08-31*, decision D1 (Jean,
epic 72) settled a word that a ratified object had been carrying twice:

    **D1 -- the word is `Chart Template`.** It replaces *Visualization Template*
    here and in `README.md`, and *Widget templates* in `analyze-and-test.md`.

and wrote its own criterion:

    a Chart Template reaches a surface before this target is written in
    `docs/product-architecture/`, or the noun that names it is still double
    between `README.md`, `analyze-and-test.md` and `glossary.md`.

The re-pointing was done: measured today, not one of the three files USES a
retired spelling. What was missing is the ratchet. A word is re-pointed once and
drifts back for years -- the next hand writes *Visualization Template* from an
old note, in a document, and nothing anywhere goes red. One object under two
spellings is two objects as far as a model is concerned, which is the whole
reason the arbitration happened.

WHY THIS IS NOT A BARE GREP. Every one of the three files legitimately CONTAINS a
retired spelling, and must: the glossary keeps a `**Retired spellings.**`
paragraph, `analyze-and-test.md` keeps the note saying what its sentence used to
say. A grep that refused the string would go red on the very paragraphs that
close the criterion, and the only way out would be to delete the history that
makes the decision legible.

So the rule is MENTION versus USE, and it is decided per BLOCK: a retired
spelling may appear only in a block that also names the canonical noun and marks
the retirement in words. A sentence that simply uses the old word carries
neither, and that is the reintroduction this file exists to catch.

The wire token `visualization_template_version` is deliberately untouched: D1
keeps it ("a stored token is not a product word"), it is written into migration
154's CHECK, and it cannot match -- the patterns below need a space where the
token has an underscore.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
DOCS = ROOT / "docs" / "product-architecture"

#: The canonical noun, as D1 settled it.
CANONICAL = "Chart Template"

#: The three files the criterion names by hand. Declared -- not discovered -- so
#: that renaming or moving one of them fails here instead of quietly shrinking
#: the sweep to two.
THE_THREE = (
    DOCS / "README.md",
    DOCS / "analyze-and-test.md",
    DOCS / "glossary.md",
)

#: The spellings D1 retired. Both singular and plural, any case. The space is
#: load-bearing: `visualization_template_version` is a wire token D1 keeps.
RETIRED = re.compile(r"\bvisualization templates?\b|\bwidget templates?\b", re.I)

#: What makes a block a RETIREMENT NOTE rather than a use of the old word. Each
#: of these is a word that can only appear because someone is talking ABOUT the
#: spelling: naming it retired, naming what replaced it, or dating its end.
RETIREMENT_MARKER = re.compile(
    r"\bretired\b|\bre-pointed\b|\breplaces\b|\bsuperseded\b|\buntil\b", re.I
)


def _blocks(text: str) -> list[str]:
    """Paragraph blocks, with every markdown table ROW standing on its own.

    A table has no blank lines inside it, so paragraph splitting alone would make
    a forty-row table ONE block: a retired spelling used in row 30 would be
    excused by a retirement note in row 3. The row is the unit a reader reads.
    """
    blocks: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            blocks.append("\n".join(current))
            current.clear()

    for line in text.splitlines():
        if line.lstrip().startswith("|"):
            flush()
            blocks.append(line)
            continue
        if not line.strip():
            flush()
            continue
        current.append(line)
    flush()
    return blocks


def _uses(path: Path) -> list[str]:
    """Blocks of *path* that USE a retired spelling instead of naming it retired."""
    offenders = []
    for block in _blocks(path.read_text(encoding="utf-8", errors="replace")):
        if not RETIRED.search(block):
            continue
        if CANONICAL in block and RETIREMENT_MARKER.search(block):
            continue
        offenders.append(" ".join(block.split())[:160])
    return offenders


def _rel(path: Path) -> str:
    return str(path.relative_to(ROOT)).replace("\\", "/")


def test_the_three_files_the_criterion_names_are_all_there() -> None:
    """A guard that swept a file that moved would report zero and mean nothing."""
    missing = [_rel(p) for p in THE_THREE if not p.is_file()]
    assert not missing, f"the criterion names a document that is not there: {missing}"


def test_the_target_is_written_where_the_criterion_says_it_must_be() -> None:
    """The bullet's FIRST half: no surface before the target is written down.

    "a Chart Template reaches a surface before this target is written in
    `docs/product-architecture/`" is a precondition, not a preference — the
    surface shipped (stories 72.1-72.7) and the amendment that authorises it must
    outlive the story files that described it. `_bmad-output/` is not
    `docs/product-architecture/`, and only the second is ratified.
    """
    target = DOCS / "visualization-and-rendering.md"
    text = target.read_text(encoding="utf-8", errors="replace")
    for required in (
        "## Amendment, 2026-08-31 — Chart Template, the ratified target",
        "**D1 — the word is `Chart Template`.**",
    ):
        assert required in text, (
            f"{_rel(target)} no longer carries {required!r}; the Chart Template "
            "surface would then be reaching a person with no ratified target "
            "behind it"
        )


@pytest.mark.parametrize("path", THE_THREE, ids=lambda p: p.name)
def test_none_of_the_three_uses_a_retired_spelling(path: Path) -> None:
    """The exact scope of the bullet: the noun is not double between these three."""
    offenders = _uses(path)
    assert not offenders, (
        f"{_rel(path)} uses a retired spelling of {CANONICAL!r} outside a "
        f"retirement note:\n  " + "\n  ".join(offenders)
    )


def test_no_ratified_document_reintroduces_the_word() -> None:
    """The same rule over the whole ratified set, because the class is the defect.

    The bullet names three files because those were the three that disagreed on
    the day it was written. The failure it describes -- an old spelling coming
    back from an old note -- has nothing to do with which file receives it, and
    a guard bounded by the three would watch the only three places already
    repaired. Measured today: `element-control-loop.md` carries the spelling
    twice and both are retirement notes, so this passes on the repository as it
    stands.
    """
    paths = sorted(DOCS.rglob("*.md"))
    # The floor the quiet-guard census demands: an emptied ratified set must
    # not read as "no retired spelling anywhere".
    assert len(paths) >= 25, "the sources moved; this guard scanned none"
    offenders: list[str] = []
    for path in paths:
        offenders.extend(f"{_rel(path)} : {hit}" for hit in _uses(path))
    assert not offenders, (
        f"a ratified document uses a retired spelling of {CANONICAL!r}:\n  "
        + "\n  ".join(offenders)
    )


def test_each_of_the_three_names_the_canonical_noun() -> None:
    """"Not double" is two claims, and the second one is that the word is THERE.

    A file could pass the check above by dropping every mention of the object.
    That is not the target: all three of these documents describe the object, and
    the criterion is that they describe it under ONE noun.
    """
    silent = [
        _rel(path)
        for path in THE_THREE
        if CANONICAL not in path.read_text(encoding="utf-8", errors="replace")
    ]
    assert not silent, f"these documents no longer name {CANONICAL!r} at all: {silent}"


def test_the_guard_can_actually_go_red() -> None:
    """Negative control: a ratchet that cannot redden is decor.

    The offending text is synthetic on purpose -- proving a door closes must not
    require dirtying a ratified document with the word it refuses.
    """
    used = "The Visualization Template lens lists the presentations a Report may pin."
    mentioned = (
        "*Visualization Template* was retired on 2026-08-31; the noun is "
        "**Chart Template**."
    )
    assert RETIRED.search(used)
    assert not (CANONICAL in used and RETIREMENT_MARKER.search(used))
    assert RETIRED.search(mentioned)
    assert CANONICAL in mentioned and RETIREMENT_MARKER.search(mentioned)


def test_a_table_row_is_judged_on_its_own_row() -> None:
    """The block splitter, pinned: a neighbouring row must not excuse a use.

    `glossary.md` states the retirement inside ONE row of a forty-row reserved-
    words table. Paragraph splitting alone would hand that row's excuse to every
    other row of the same table.
    """
    table = (
        "| **Template** | Qualified, always: a **Chart Template** — "
        "*Widget template* is a retired spelling |\n"
        "| **Widget templates** | the presentations a Report may pin |\n"
    )
    blocks = _blocks(table)
    assert len(blocks) == 2, blocks
    offending = [b for b in blocks if RETIRED.search(b) and not (
        CANONICAL in b and RETIREMENT_MARKER.search(b)
    )]
    assert len(offending) == 1 and "Widget templates" in offending[0]
