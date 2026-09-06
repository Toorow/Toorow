"""Every site a ratified amendment names still reaches THE rule, not a copy.

WHY THIS FILE EXISTS. `docs/product-architecture/` ratifies amendments that bind
several sites to one rule. Two of them are pinned by a shared constant --
`capability_is_active` for "AND OFF IS ABSENT, NOT GREY", `DATA_ROLES` for "le
`<select>` n'offre que les sept valeurs" -- and until 2026-08-31 nothing measured
whether the sites still reached it. The class was SAMPLED: one hand-written test
per amendment, each holding its own idea of which sites mattered, and a sample is
silent about the site nobody sampled.

The measurement is `scripts/check_amendment_agreement.py`, imported here rather
than re-derived -- the same shape as
`test_register_bodies_name_their_tools`, which imports `division_candidates`
instead of re-walking the syntax trees. One reader of one rule; a test that
re-implemented the parse would be a second answer to the question it is asking.

AND A SECOND CLASS, 2026-09-02. An amendment can pin an ORDER instead of a value,
and then the import class above is blind by construction: a TypeScript array and
a Python tuple cannot import one another, so the rule is spelled once per language
and nothing holds the spellings together. « `Mapping` vient avant `Data` » was
spelled in five places; three of them still carried the order that amendment
reversed, four weeks after story 58.2 applied it to the other two. Nothing was red
-- the server tuple is read as a SET, a union has no order at runtime, and the
document's tab table is prose. `ORDERED_AMENDMENTS` declares those sites, each
site says WHERE its spelling starts, and every spelling is read from its own file
and compared with the ratified order.

WHAT THIS TEST IS HONEST ABOUT. It covers amendments pinned by a shared CONSTANT
or by a ratified ORDER, and nothing else. An amendment whose rule lives in prose
alone cannot be measured this way -- there is no symbol for a site to import and
no sequence to compare -- and the scope test below holds the instrument to saying
so out loud, because an instrument whose scope is not stated is read as covering
everything.

    python scripts/check_amendment_agreement.py
    python scripts/check_amendment_agreement.py --scope
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))

check_amendment_agreement = pytest.importorskip("check_amendment_agreement")

_MEASURE = "  python scripts/check_amendment_agreement.py"


def test_every_named_site_imports_the_constant_its_amendment_pins():
    """The whole declared class, in one assertion."""
    declared = check_amendment_agreement.AMENDMENTS
    assert declared, (
        "the instrument declares no amendment at all -- it has stopped measuring "
        f"rather than gone green.\n{_MEASURE}"
    )

    found = check_amendment_agreement.offenders()

    assert not found, (
        "these sites hold their own copy of a rule their amendment declares "
        "once, so the document and the code can drift without anything saying "
        "so:\n  " + "\n  ".join(found) + f"\n{_MEASURE}"
    )


def test_the_instrument_still_refuses_a_site_that_re_spells_the_rule(tmp_path, monkeypatch):
    """A guard that cannot fail proves nothing -- so make it fail on purpose.

    Against a throwaway tree, never the real one: pinning the mutation to a real
    module would make this guard pass silently the day that module is repaired,
    which is the defect `an instrument must not measure its own copy` names.
    """
    module = tmp_path / "server" / "core" / "example_rule.py"
    module.parent.mkdir(parents=True)
    module.write_text("EXAMPLE_STATES = ('ready', 'degraded')\n", encoding="utf-8")

    honest = tmp_path / "server" / "core" / "example_reader.py"
    honest.write_text(
        "from core.example_rule import EXAMPLE_STATES\n"
        "\n"
        "\n"
        "def reads(state):\n"
        "    return state in EXAMPLE_STATES\n",
        encoding="utf-8",
    )

    # The offender: the same rule, spelled again. It is CORRECT today, which is
    # precisely why nothing else catches it.
    copy = tmp_path / "server" / "core" / "example_copy.py"
    copy.write_text(
        "def reads(state):\n"
        "    return state in ('ready', 'degraded')\n",
        encoding="utf-8",
    )

    document = tmp_path / "docs" / "example.md"
    document.parent.mkdir(parents=True)
    document.write_text("An amendment.\n", encoding="utf-8")

    amendment = check_amendment_agreement.Amendment(
        name="an example amendment",
        document="docs/example.md",
        module="core.example_rule",
        constant="EXAMPLE_STATES",
        quote="the same threshold everywhere",
        sites=("server/core/example_reader.py", "server/core/example_copy.py"),
    )
    monkeypatch.setattr(check_amendment_agreement, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(check_amendment_agreement, "AMENDMENTS", (amendment,))

    # The CONSTANT class alone: the ordered class is declared against the real
    # repository and would answer for a tree that does not contain it.
    found = check_amendment_agreement._constant_offenders()

    assert len(found) == 1, found
    assert "example_copy.py" in found[0]
    assert "EXAMPLE_STATES" in found[0]
    # And the honest reader is NOT reported: a guard that flags both is not
    # measuring the difference it claims to.
    assert "example_reader.py" not in found[0]


def test_a_renamed_constant_turns_the_instrument_red_rather_than_vacuous(tmp_path, monkeypatch):
    """The failure mode that would make every site compliant at once.

    If the declaring module stops carrying the constant, "does this site import
    it" is False everywhere -- but the honest report is not "every site is an
    offender", it is "the rule this instrument holds no longer exists". One line
    naming the constant, so a person re-reads the amendment instead of editing
    four files.
    """
    module = tmp_path / "server" / "core" / "example_rule.py"
    module.parent.mkdir(parents=True)
    module.write_text("RENAMED_STATES = ('ready',)\n", encoding="utf-8")
    reader = tmp_path / "server" / "core" / "example_reader.py"
    reader.write_text("from core.example_rule import RENAMED_STATES\n", encoding="utf-8")
    document = tmp_path / "docs" / "example.md"
    document.parent.mkdir(parents=True)
    document.write_text("An amendment.\n", encoding="utf-8")

    amendment = check_amendment_agreement.Amendment(
        name="an example amendment",
        document="docs/example.md",
        module="core.example_rule",
        constant="EXAMPLE_STATES",
        quote="the same threshold everywhere",
        sites=("server/core/example_reader.py",),
    )
    monkeypatch.setattr(check_amendment_agreement, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(check_amendment_agreement, "AMENDMENTS", (amendment,))

    # The CONSTANT class alone: the ordered class is declared against the real
    # repository and would answer for a tree that does not contain it.
    found = check_amendment_agreement._constant_offenders()

    assert len(found) == 1, found
    assert "no longer declares" in found[0]
    assert "EXAMPLE_STATES" in found[0]


def test_every_site_of_an_ordered_amendment_spells_the_ratified_order():
    """The second class, in one assertion -- and its declaration is not empty.

    `offenders()` already carries it, so this test would pass on the strength of
    the first class alone the day `ORDERED_AMENDMENTS` were emptied. Hence the
    first assertion: a declaration that measures nothing has stopped measuring,
    not gone green.
    """
    declared = check_amendment_agreement.ORDERED_AMENDMENTS
    assert declared, (
        "no amendment is declared as pinned by an order -- the class that caught "
        f"« `Mapping` vient avant `Data` » no longer measures anything.\n{_MEASURE}"
    )
    for amendment in declared:
        assert len(amendment.sites) > 1, (
            f"{amendment.name} declares {len(amendment.sites)} site: an order "
            "that is spelled once cannot disagree with anything, so this entry "
            "proves nothing"
        )
        assert (_REPO_ROOT / amendment.document).exists()

    found = check_amendment_agreement._ordered_offenders()

    assert not found, (
        "these sites spell a ratified order differently, so two regions of one "
        "screen state opposite things:\n  " + "\n  ".join(found) + f"\n{_MEASURE}"
    )


def _ordered_tree(tmp_path, first: str, second: str) -> None:
    """A throwaway repository spelling one order in a tuple, an array and a table."""
    module = tmp_path / "server" / "core" / "example_tabs.py"
    module.parent.mkdir(parents=True)
    module.write_text(f'\nTABS = (\n    "{first}", "{second}",\n)\n', encoding="utf-8")

    screen = tmp_path / "ui" / "exampleTabs.ts"
    screen.parent.mkdir(parents=True)
    screen.write_text(
        "// A comment carrying a bracket [ that must not be read as the list.\n"
        f'export const EXAMPLE_TABS: readonly Tab[] = [\n  "{first}", "{second}",\n];\n',
        encoding="utf-8",
    )

    document = tmp_path / "docs" / "example.md"
    document.parent.mkdir(parents=True)
    document.write_text(
        "| Tab | Content |\n|---|---|\n"
        f"| **{first.title()}** | first |\n| **{second.title()}** | second |\n"
        "\n## Next section\n\n| **Ignored** | past the heading |\n",
        encoding="utf-8",
    )


def _ordered_amendment(sites) -> object:
    return check_amendment_agreement.OrderedAmendment(
        name="an example ordered amendment",
        document="docs/example.md",
        quote="the order is `alpha` then `beta`",
        order=("alpha", "beta"),
        sites=sites,
    )


def test_the_ordered_class_reads_each_language_and_refuses_a_reversed_spelling(
    tmp_path, monkeypatch
):
    """Green when the three spellings agree, red the moment one is reversed.

    Against a throwaway tree, never the real one: an instrument that measures its
    own copy passes silently the day the copy is repaired.
    """
    _ordered_tree(tmp_path, "alpha", "beta")
    sites = (
        check_amendment_agreement.OrderedSite(
            path="docs/example.md", anchor="| Tab | Content |", reader="table"
        ),
        check_amendment_agreement.OrderedSite(
            path="server/core/example_tabs.py", anchor="\nTABS = "
        ),
        check_amendment_agreement.OrderedSite(
            path="ui/exampleTabs.ts", anchor="export const EXAMPLE_TABS: readonly Tab[] ="
        ),
    )
    monkeypatch.setattr(check_amendment_agreement, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        check_amendment_agreement, "ORDERED_AMENDMENTS", (_ordered_amendment(sites),)
    )

    # All three read the same order -- and each reader actually read something,
    # which a bare "no offenders" would not prove.
    for site in sites:
        assert check_amendment_agreement._sequence_at(
            tmp_path / site.path, site
        ) == ("alpha", "beta"), site.path
    assert check_amendment_agreement._ordered_offenders() == []

    # Now reverse ONE spelling -- the server tuple, the site whose order no test
    # and no screen reads, which is exactly how the real defect survived.
    (tmp_path / "server" / "core" / "example_tabs.py").write_text(
        '\nTABS = (\n    "beta", "alpha",\n)\n', encoding="utf-8"
    )

    found = check_amendment_agreement._ordered_offenders()

    assert len(found) == 1, found
    assert "example_tabs.py" in found[0]
    assert "beta, alpha" in found[0]
    assert "alpha, beta" in found[0]


def test_an_ordered_site_whose_anchor_is_gone_or_doubled_is_reported_not_skipped(
    tmp_path, monkeypatch
):
    """An unreadable site must be an offender, never a silent pass.

    A reader that returns nothing when it cannot find its anchor turns a moved
    declaration into agreement, which is the failure mode that makes a guard
    worse than none.
    """
    _ordered_tree(tmp_path, "alpha", "beta")
    module = tmp_path / "server" / "core" / "example_tabs.py"
    monkeypatch.setattr(check_amendment_agreement, "REPO_ROOT", tmp_path)

    gone = check_amendment_agreement.OrderedSite(
        path="server/core/example_tabs.py", anchor="\nRENAMED_TABS = "
    )
    monkeypatch.setattr(
        check_amendment_agreement, "ORDERED_AMENDMENTS", (_ordered_amendment((gone,)),)
    )
    found = check_amendment_agreement._ordered_offenders()
    assert len(found) == 1 and "occurs 0 times" in found[0], found

    module.write_text(
        '\nTABS = (\n    "alpha", "beta",\n)\n\nTABS = (\n    "alpha", "beta",\n)\n',
        encoding="utf-8",
    )
    doubled = check_amendment_agreement.OrderedSite(
        path="server/core/example_tabs.py", anchor="\nTABS = "
    )
    monkeypatch.setattr(
        check_amendment_agreement,
        "ORDERED_AMENDMENTS",
        (_ordered_amendment((doubled,)),),
    )
    found = check_amendment_agreement._ordered_offenders()
    assert len(found) == 1 and "occurs 2 times" in found[0], found


def test_the_instrument_states_what_it_does_not_cover():
    """An unstated scope is read as total coverage, and this one is not total.

    Two things must survive here: the sentence that prose-only amendments are NOT
    measured, and the per-site debt for a declared site in another language --
    a TypeScript screen cannot import a Python tuple, and the entry names the
    test that pins it instead rather than pretending the site is covered.
    """
    lines = check_amendment_agreement.scope_lines()
    joined = "\n".join(lines)

    assert "NOT COVERED AT ALL" in joined, (
        "the instrument no longer says which class of amendment it is silent "
        f"about:\n{joined}"
    )
    assert "prose" in joined
    assert "pinned by an ORDER" in joined, (
        "the second class is measured and not announced, so `--scope` under-states "
        f"what the instrument covers:\n{joined}"
    )
    for amendment in check_amendment_agreement.ORDERED_AMENDMENTS:
        for site in amendment.sites:
            assert (_REPO_ROOT / site.path).exists(), (
                f"{site.path} is declared as spelling a ratified order and is gone"
            )
            assert site.path in joined

    uncovered = [
        pair
        for amendment in check_amendment_agreement.AMENDMENTS
        for pair in amendment.not_covered
    ]
    for site, pinned_by in uncovered:
        assert (_REPO_ROOT / site).exists(), f"{site} is declared uncovered and is gone"
        assert (_REPO_ROOT / pinned_by).exists(), (
            f"{site} names {pinned_by} as what pins it, and that test is gone -- "
            "the debt is now unpinned and unmeasured"
        )
        assert site in joined
