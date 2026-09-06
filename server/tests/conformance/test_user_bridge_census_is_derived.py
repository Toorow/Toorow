"""The four matrices of `user-bridge.md` §5 against the code they were derived from.

WHY THIS FILE EXISTS. §1.3 of that document states the rule the product is judged
by — *a capability exists on four planes, and green on three it delivers
nothing* — and §5 applies it to eighteen gestures, each with the command that
established its row. Then the matrix froze. `completeness-ledger.json`,
`user-bridge[1]`, records the gap in the words of the session that measured it:
the count *"still lives only in the document"* and no script computes it.

`scripts/user_bridge_census.py` is that standing measure. This guard is the seam
between the written matrix and the derivation — the shape of
`test_element_inventory_is_derived.py`, one file over: it compares, it never
regenerates. §5 itself explains why the seam has to exist: seven of its earlier
citations had rotted into lines that no longer resolved, and *"une dérivation
dont la citation ne résout plus n'est plus une dérivation — c'est une
affirmation"*.

THE FLOOR TESTS ARE NOT CEREMONY. A parse that finds no matrix reports a
perfect one, and every comparison below passes on two empty sets. That is
criterion 13 of `module-boundaries.md`, so the parse carries floors that are
false on an empty read, and the MCP plane carries a discrimination test: a plane
that is green for every row measures nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import user_bridge_census  # noqa: E402


@pytest.fixture(scope="module")
def census():
    """The whole census, booted once. The MCP plane costs the real app (~15 s)
    and it is half of every state §5 defines, so it cannot be skipped here the
    way `element_inventory`'s guard skips its tool plane."""
    return user_bridge_census.build(with_tools=True)


def test_the_four_matrices_were_actually_read(census) -> None:
    """A census that parsed nothing reports a matrix with nothing wrong."""
    collapsed = census.collapsed()
    assert not collapsed, "\n".join(collapsed)
    counts = census.counts()
    for plane, floor in user_bridge_census.FLOORS.items():
        assert counts[plane] >= floor, counts


def test_every_row_of_the_matrix_still_cites_a_surface_the_code_carries(census) -> None:
    """The gate, held as a test: no NEW row has come loose from the code.

    The ratchet turns one way. A row repaired in the document must be deleted
    from `ACCEPTED_ON_2026_09_02`, and a row that comes loose is refused at the
    moment it comes loose rather than the day someone re-reads §5.
    """
    keys = census.finding_keys()
    baseline = user_bridge_census.ACCEPTED_ON_2026_09_02
    new = sorted(keys - baseline)
    assert not new, (
        "rows of `user-bridge.md` §5 no longer match the code:\n  "
        + "\n  ".join(new)
        + "\n\nRun `python scripts/user_bridge_census.py` for the four planes of every "
        "gesture. Repair the row against the code, or the code against the row — never "
        "the census, which only compares them."
    )
    gone = sorted(baseline - keys)
    assert not gone, (
        "these disagreements no longer hold and the ratchet must be turned:\n  "
        + "\n  ".join(gone)
        + "\n\nDelete them from ACCEPTED_ON_2026_09_02 in scripts/user_bridge_census.py."
    )


def test_the_written_counts_agree_with_the_tables(census) -> None:
    """Each lot's count line, and the total that closes §5."""
    disagreements = census.count_disagreements()
    assert not disagreements, "\n".join(disagreements)


def test_the_four_planes_are_four_distinct_measurements(census) -> None:
    """Each plane must be able to be missing, or it is not measuring anything.

    The MCP plane is the one this whole document is about — §5's own finding is
    that ten of eighteen gestures cannot be performed by a model — so a run where
    every row is green on it means the derivation stopped reading the catalog,
    not that the gap closed. If the gap really closes, that is the day to record
    it in `docs/product-architecture/user-bridge.md` and delete this assertion
    rather than weaken it.
    """
    rows = census.rows
    assert rows, "no gesture reached the census; the §5 parse returned nothing"
    planes = {name for row in rows for name in row.planes}
    assert planes == {"component", "api", "ui", "mcp"}, planes

    green = census.plane_counts()
    assert 0 < green["mcp"] < len(rows), (
        f"the MCP plane derives green for {green['mcp']} of {len(rows)} gestures. "
        "Neither extreme is a measurement: all-green means the catalog was not read, "
        "all-missing means the boot failed."
    )
    assert green["ui"] > 0 and green["api"] > 0 and green["component"] > 0, green


def test_the_tools_the_shape_cannot_see_are_the_ones_that_were_pinned(census) -> None:
    """The census reads a tool citation out of a prose cell two ways: the catalog
    knows the name, or the name carries `_TOOL_NAME`'s shape and the catalog does
    not — the drift case. A ONE-SEGMENT tool retired from the catalog falls
    between the two and is not reported. That hole has a size, it is one tool
    today, and this pins it so its growth is a finding rather than a silence.
    """
    declared = census.sources.declared_tools
    assert declared, "no MCP declaration reached the census; the app boot returned nothing"
    unmatched = {n for n in declared if not user_bridge_census._TOOL_NAME.fullmatch(n)}
    assert unmatched == user_bridge_census.SINGLE_SEGMENT_TOOLS, (
        "the registered tools `_TOOL_NAME` cannot see have changed: the catalog holds "
        f"{sorted(unmatched)}, the pin holds "
        f"{sorted(user_bridge_census.SINGLE_SEGMENT_TOOLS)}.\n"
        "Each name in that set is a tool a matrix row could cite and lose without the "
        "census saying so. Update SINGLE_SEGMENT_TOOLS in scripts/user_bridge_census.py "
        "deliberately, and prefer naming the tool with two segments."
    )


def test_the_states_the_document_writes_are_the_three_it_defines(census) -> None:
    """§5 defines `delivered`, `partial`, `absent` and no fourth."""
    for row in census.rows:
        assert row.written_state in ("delivered", "partial", "absent"), (
            f"{row.identity}: the state cell reads {row.written_state!r}"
        )
