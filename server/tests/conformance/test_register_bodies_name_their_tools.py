"""Every MCP registration NAMES its tool -- criterion 10 of `module-boundaries.md`.

WHY THIS FILE EXISTS. Three instruments read a tool's identity out of the source
AST, all three the same way -- the second positional argument of
`register_profiled`, taken as `call.args[1].id`:

  * `scripts/check_legacy_analytics_migration.py` -- the producer census, which
    turns `<module>#tool:<name>` locators into the inventory's evidence;
  * `server/tests/conformance/test_mcp_tools_resolve_project_scope.py` -- the
    project-scope ratchet;
  * `scripts/division_candidates.py::registrations` -- criterion 10's own
    measurement, which this test imports rather than re-deriving.

A `for handler in (a, b, c):` loop around the call hands all three the loop
VARIABLE. The tools it binds still reach the wire -- nothing breaks at runtime --
but they reach every instrument as the anonymous `handler`, so a census that
should have named three producers names one that does not exist, and reports
compliance. Both failures were measured, not imagined: the legacy-analytics
census on 2026-08-30 (`daily_insight_mcp`, whose body now carries the note), and
the project-scope ratchet on 2026-08-28 (`project_capabilities_mcp`, which
carries its own). Fifteen such bindings existed across twelve bodies on
2026-08-31; unrolling them changed no declaration -- `registered_declarations()`
came back byte-identical -- and made 42 tools visible to the three readers.

WHAT THIS REFUSES, AND WHAT IT DOES NOT. It refuses a binding whose handler is
not a plain, literally-written name: a loop, a comprehension, a local alias, an
attribute, a call. It does NOT refuse a body that binds several tools of one
surface -- `context_hub` binds seven and that is a legitimate surface, measured
and reported by the same instrument but left to AD-42's own arbitration. This
test is the half that is a defect rather than a judgement.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parents[3]
if str(_REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))

division_candidates = pytest.importorskip("division_candidates")

_MEASURE = "  python scripts/division_candidates.py --registrations"


def test_no_registration_hides_its_tool_name_from_the_census():
    """The whole class, over every binder the instrument sweeps."""
    rows = division_candidates.registrations()

    assert rows, (
        "the instrument found no `register_profiled` binding at all -- it has "
        f"stopped measuring the code rather than gone green.\n{_MEASURE}"
    )

    offenders = [
        f"  {row['file']}:{defect['line']} :: {row['binder']} -- {defect['why']}"
        for row in rows
        for defect in row["defects"]
    ]

    assert not offenders, (
        "these registrations do not name their tool, so the producer census, the "
        "project-scope ratchet and criterion 10 all read them as anonymous:\n"
        + "\n".join(offenders)
        + "\n\nWrite one literal `register_profiled(mcp, <function_name>, ...)` "
        "call per tool. Behaviour is unchanged -- only the name becomes readable."
        f"\n{_MEASURE}"
    )


def test_the_instrument_still_refuses_a_loop_registration(tmp_path, monkeypatch):
    """A guard that cannot fail proves nothing -- so make it fail on purpose.

    Written against a throwaway tree rather than the real one: the rule must be
    refused wherever it appears, and pinning this to a real module would make the
    guard silently pass the day that module is repaired.
    """
    module = tmp_path / "server" / "core" / "example_mcp.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "def alpha():\n"
        "    return None\n"
        "\n"
        "\n"
        "def beta():\n"
        "    return None\n"
        "\n"
        "\n"
        "def register(mcp):\n"
        "    from core.mcp_profiles import register_profiled\n"
        "\n"
        "    for handler in (alpha, beta):\n"
        "        register_profiled(\n"
        "            mcp,\n"
        "            handler,\n"
        '            profile="insights",\n'
        '            effect="read",\n'
        '            data_class="operational",\n'
        '            confirmation_mode="none",\n'
        "        )\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(division_candidates, "REPO", tmp_path)

    rows = division_candidates.registrations()

    assert len(rows) == 1, rows
    assert [defect["why"] for defect in rows[0]["defects"]] == [
        "registered inside a loop -- every tool it binds reaches the census as 'handler'"
    ]
    assert rows[0]["tools"] == ["handler"], (
        "the loop variable is what the census would have recorded as the tool's "
        "name -- that is the whole defect"
    )


def test_a_literal_registration_is_accepted():
    """The other side of the same rule, so it cannot be satisfied by refusing everything."""
    rows = division_candidates.registrations()

    posture = [row for row in rows if row["file"].endswith("project_posture_mcp.py")]

    assert posture, (
        "`project_posture_mcp.register` is the one-file-one-tool reference this "
        "rule is written around and the instrument no longer sees it."
    )
    assert posture[0]["tools"] == ["get_project_posture"]
    assert posture[0]["defects"] == []
