"""Every governed data question is measured by the pre-query gate (AD-18).

`docs/product-architecture/analyze-and-test.md`, amendment of 2026-09-01.

WHY THIS FILE EXISTS. `core/adherence.py` held ``DATA_TOOLS = ("get_daily_report",
"get_report", "get_card")`` for four months, and `record_data_query` was called
from exactly those three bodies. Meanwhile the Analyze door -- the path every
current Skill sends a caller through -- produced Results nothing measured.
Measured 2026-09-01 on the deployed server: three real sessions ran
`get_procedure` -> `get_knowledge` -> `execute_analyze_query_spec`, each with a
Result and an observed AI Path, and `get_context_adherence` answered "No
adherence observation". The measure was not broken; its perimeter was a
hand-written list, and a hand-written list cannot notice a tool it was never
told about. This file derives the perimeter from the inventory instead.

THE RULE, STATED ONCE. A *governed data question* is a registered MCP tool,
declared ``effect="read"``, whose call closure over `core/` reaches a FIGURE
COMPOSER -- a function that produces a Result, serves one, or renders the
figures of a report, a card or a Datastream roll-up for the model. The
composers are named in `_FIGURE_COMPOSERS` below, each with the sentence that
says what it composes. Three `read` tools reach a composer and answer with NO
figure (a catalogue, a readiness state, a validation verdict); they are named in
`_READS_THE_MART_WITHOUT_ANSWERING_A_FIGURE` with that sentence, so that a
fourth tool in their situation costs a sentence rather than a silence.
`confirmed_write` tools that produce a Result (`run_report_version`,
`run_notebook`) are operations, not questions: the Result they produce is
measured the moment a caller reads it through `analyze_result`.

WHAT IS HELD, IN BOTH DIRECTIONS.

  * the derived data questions, minus the named non-questions, EQUAL
    `adherence.DATA_TOOLS` -- a data tool added without its hook goes red here
    with its name, and a name kept in the tuple after its tool left goes red too;
  * every tool of `DATA_TOOLS` reaches `adherence.record_data_query`, and every
    registered tool that reaches it is listed -- nothing is measured under a
    name the reader is not told about;
  * the same two-way property between `adherence.CONTEXT_TOOLS` and
    `adherence.mark_context_call`: the consults that open an exchange are the
    ones the Skills actually direct a caller through, `get_knowledge` included.

THE RESOLVER IS IMPORTED, NOT COPIED. `scripts/mcp_tool_surface_report.py`
already resolves the call graph of `core/` exactly -- function bindings and
module bindings, aliases included, a name that resolves to no `core/*.py`
function DROPPED rather than matched by spelling. `test_mcp_tool_surface.py`
imports it for the append census; this file imports it for the same reason:
two derivations of one call graph is how one question gets two answers.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))

census = pytest.importorskip("mcp_tool_surface_report")

_MEASURE = "  python -m pytest tests/conformance/test_every_data_question_is_measured.py -q"

#: The functions that hand the model figures, keyed exactly as the resolver keys
#: `core/` -- `(file name, function name)`. A composer renamed or moved goes red
#: in `test_every_figure_composer_still_exists` rather than silently shrinking
#: the perimeter.
_FIGURE_COMPOSERS: dict[tuple[str, str], str] = {
    ("query_execution.py", "run_execution"): (
        "produces one Result from one single-source Query Spec version"
    ),
    ("multi_source_execution.py", "execute_plan"): (
        "produces one Result from one multi-source plan version"
    ),
    ("analyze_render_mcp.py", "_compose_answer_on_connection"): (
        "serves the compact answer of one immutable Result -- outcome, headline "
        "counts, bounded evidence -- to the model"
    ),
    ("summarizer.py", "build_daily_report_summary"): (
        "composes the daily report the model reads"
    ),
    ("reports.py", "render_report"): "renders a report envelope",
    ("cards.py", "get_card"): "renders a card envelope",
    ("warehouse.py", "query_daily_report"): (
        "reads the `fact_daily_kpi` roll-up -- the figures of one or more "
        "Datastreams over a window"
    ),
}

#: `read` tools that REACH a composer and answer with no figure. One way: a name
#: leaves when its tool starts answering with figures (and then it must be
#: measured), and adding one costs the sentence that says what the tool answers
#: with instead. `test_the_non_question_ledger_cannot_go_stale` makes a stale
#: entry loud.
_READS_THE_MART_WITHOUT_ANSWERING_A_FIGURE: dict[str, str] = {
    "get_card_capabilities": (
        "Reads the roll-up to learn WHICH metrics and dimensions this Project "
        "measured in the window and answers with that catalogue -- names and "
        "bounds, never a value."
    ),
    "get_daily_insight_readiness": (
        "Reads the roll-up to know whether a window is fresh enough to publish "
        "against and answers with a readiness state and its blocking reasons."
    ),
    "preview_daily_insight": (
        "Reads the roll-up to validate a candidate insight against what exists "
        "and answers with `{ok, reasonCode}` -- a verdict on the caller's "
        "document, never a figure of the Project."
    ),
}

#: The two seams of the gate, keyed as the resolver keys them.
_RECORD = ("adherence.py", "record_data_query")
_MARK = ("adherence.py", "mark_context_call")


def _declarations(trees: dict) -> dict[tuple[str, str], dict]:
    """Every `register_profiled(mcp, <name>, profile=..., effect=...)` in `core/`.

    Read off the AST rather than the assembled app: this suite must run without
    booting `core.main`, and criterion 10 of `module-boundaries.md` (held by
    `test_register_bodies_name_their_tools.py`) guarantees each registration
    names its tool literally, so the second positional argument is the tool.
    """
    declared: dict[tuple[str, str], dict] = {}
    for module, tree in trees.items():
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "register_profiled"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Name)
            ):
                continue
            keywords = {
                kw.arg: kw.value.value
                for kw in node.keywords
                if isinstance(kw.value, ast.Constant)
            }
            declared[(module, node.args[1].id)] = keywords
    return declared


@pytest.fixture(scope="module")
def graph():
    index = census._core_index()
    declared = _declarations(index["trees"])
    assert declared, "no `register_profiled` call found in core/ -- the reader is broken"
    return index, declared


def _reaching(index: dict, seeds: set[tuple[str, str]]) -> set[tuple[str, str]]:
    """Every `core/` function whose bounded call closure reaches one of *seeds*."""
    grown = census._closure(index, {seed: {seed} for seed in seeds})
    return set(grown)


def _derived_data_questions(index: dict, declared: dict) -> dict[str, tuple[str, str]]:
    """Tool name -> (module, composer names) for every `read` tool reaching a composer."""
    reach = census._closure(
        index, {composer: {composer} for composer in _FIGURE_COMPOSERS}
    )
    found: dict[str, tuple[str, str]] = {}
    for (module, tool), keywords in declared.items():
        if keywords.get("effect") != "read":
            continue
        composers = reach.get((module, tool))
        if composers:
            found[tool] = (module, ", ".join(sorted(name for _m, name in composers)))
    return found


def test_every_figure_composer_still_exists(graph):
    """A renamed composer must shrink nothing silently."""
    index, _declared = graph
    missing = sorted(f"{m}:{f}" for (m, f) in _FIGURE_COMPOSERS if (m, f) not in index["defs"])
    assert missing == [], (
        "these figure composers no longer exist under that name; the perimeter of "
        f"the gate was derived from them: {missing}\n{_MEASURE}"
    )
    for seam in (_RECORD, _MARK):
        assert seam in index["defs"], f"{seam[0]}:{seam[1]} is gone -- the gate has no seam"


def test_the_measured_data_tools_are_exactly_the_registered_data_questions(graph):
    """The clause: a data tool exists in the inventory that the gate does not measure."""
    from core.adherence import DATA_TOOLS  # noqa: PLC0415

    index, declared = graph
    derived = _derived_data_questions(index, declared)
    questions = set(derived) - set(_READS_THE_MART_WITHOUT_ANSWERING_A_FIGURE)

    unmeasured = sorted(questions - set(DATA_TOOLS))
    assert unmeasured == [], (
        "these `read` tools hand the model figures and the gate does not measure "
        "them: "
        + "; ".join(f"{t} ({derived[t][0]}, via {derived[t][1]})" for t in unmeasured)
        + ". Add each to `adherence.DATA_TOOLS` AND call `record_data_query` from "
        "it on the `reporting_mcp._apply_pre_query_gate` pattern -- or, if it "
        "answers with no figure, name it in "
        "`_READS_THE_MART_WITHOUT_ANSWERING_A_FIGURE` with the sentence that says "
        f"so.\n{_MEASURE}"
    )
    stale = sorted(set(DATA_TOOLS) - questions)
    assert stale == [], (
        "`adherence.DATA_TOOLS` names tools that are not registered `read` tools "
        f"reaching a figure composer: {stale}. The measured list must equal the "
        f"inventory, not exceed it.\n{_MEASURE}"
    )


def test_every_measured_data_tool_calls_record_data_query(graph):
    """Being listed is not being measured: the hook must be reachable from the body."""
    from core.adherence import DATA_TOOLS  # noqa: PLC0415

    index, declared = graph
    recording = _reaching(index, {_RECORD})
    by_tool = {tool: module for (module, tool) in declared}

    silent = sorted(
        tool for tool in DATA_TOOLS
        if (by_tool.get(tool), tool) not in recording
    )
    assert silent == [], (
        "these tools are in `adherence.DATA_TOOLS` and `record_data_query` is not "
        f"reachable from their registered body: {silent}. A name in the list without "
        f"the call is a measure that reads healthy on nothing.\n{_MEASURE}"
    )


def test_nothing_is_measured_under_a_name_the_reader_is_not_told(graph):
    """`measured_data_tools` in `adherence_overview` is `DATA_TOOLS`; it must be
    the whole truth."""
    from core.adherence import DATA_TOOLS  # noqa: PLC0415

    index, declared = graph
    recording = _reaching(index, {_RECORD})
    unlisted = sorted(
        tool for (module, tool) in declared
        if (module, tool) in recording and tool not in DATA_TOOLS
    )
    assert unlisted == [], (
        f"these registered tools record adherence and are absent from "
        f"`adherence.DATA_TOOLS`: {unlisted}. The reader would never know they are "
        f"measured.\n{_MEASURE}"
    )


def test_the_context_tools_are_exactly_the_registered_tools_that_open_an_exchange(graph):
    """The consults that count are the ones that call `mark_context_call` -- and
    all of them. `get_knowledge` was the one that did not, and it is the second
    call of every observed Analyze session."""
    from core.adherence import CONTEXT_TOOLS  # noqa: PLC0415

    index, declared = graph
    marking = _reaching(index, {_MARK})
    opening = sorted(tool for (module, tool) in declared if (module, tool) in marking)

    assert sorted(CONTEXT_TOOLS) == opening, (
        "`adherence.CONTEXT_TOOLS` and the registered tools that reach "
        f"`mark_context_call` disagree.\n  listed : {sorted(CONTEXT_TOOLS)}\n"
        f"  opening: {opening}\nA consult the Skills direct a caller through must "
        f"open the exchange, and the list must say so.\n{_MEASURE}"
    )
    assert "get_knowledge" in opening, (
        "`get_knowledge` no longer opens an exchange -- the Analyze Skill's second "
        "call would read as no consult at all"
    )


def test_the_non_question_ledger_cannot_go_stale(graph):
    """A ledger entry must still be a registered `read` tool that reaches a
    composer; otherwise it is a sentence about nothing."""
    index, declared = graph
    derived = _derived_data_questions(index, declared)

    stale = sorted(
        name for name in _READS_THE_MART_WITHOUT_ANSWERING_A_FIGURE if name not in derived
    )
    assert stale == [], (
        "these names in `_READS_THE_MART_WITHOUT_ANSWERING_A_FIGURE` are no longer "
        f"registered `read` tools reaching a figure composer: {stale}. Remove the "
        f"entry; a ledger that outlives its subject is a promise.\n{_MEASURE}"
    )
    for name, sentence in _READS_THE_MART_WITHOUT_ANSWERING_A_FIGURE.items():
        assert len(sentence.split()) >= 12, f"{name}: the reason must be a sentence"
