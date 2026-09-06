"""One write, three doors -- story 63.6, epic 63.

THE ARBITRAGE THIS FILE HOLDS. Stopping a run exists on the console, on REST and
on the MCP, and all three go through `execution_progress.stop_collection_run`. A
gesture that lives on one door only is a gesture the model cannot perform; three
implementations of it would be three answers to "what does stopping mean", and
the second reader would believe the wrong one.

So the tests below pin the DECLARATION (a confirmed write, human confirmation)
and the SEAM (the same function, the same refusals as the route) -- never the
arithmetic, which `test_execution_stop.py` owns.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from core import stop_run_mcp
from core.execution_progress import STOP_FIELDS

SOURCE = Path(stop_run_mcp.__file__).read_text(encoding="utf-8")


class _Recorder:
    """Captures what `register_profiled` was asked to declare."""

    def __init__(self):
        self.declared: list[dict] = []
        self.tools: dict[str, object] = {}

    def record(self, mcp, handler, **kwargs):
        self.declared.append({"handler": handler, **kwargs})
        self.tools[handler.__name__] = handler
        return handler


def _register(monkeypatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(
        "core.mcp_profiles.register_profiled", recorder.record, raising=True
    )
    stop_run_mcp.register(object())
    return recorder


def test_the_tool_is_a_confirmed_write_that_a_person_confirms(monkeypatch) -> None:
    """Not a preference toggle: it ends work under way and gives days back.

    The precedent is `schedule_mcp.set_datastream_schedule`, declared the same
    way for the same reason -- it spends or stops spending provider quota.
    """
    recorder = _register(monkeypatch)
    assert len(recorder.declared) == 1
    declared = recorder.declared[0]
    assert declared["effect"] == "confirmed_write"
    assert declared["confirmation_mode"] == "human"
    assert declared["profile"] == "operations"
    assert declared["data_class"] == "operational"


def test_it_writes_through_the_one_function_and_holds_no_sql_of_its_own(
    monkeypatch,
) -> None:
    """Three doors, ONE write. A second UPDATE here would be a second meaning."""
    recorder = _register(monkeypatch)
    handler = recorder.tools["stop_datastream_run"]
    body = inspect.getsource(handler)
    assert "stop_collection_run(" in body
    # The only statement it issues is the scope proof -- no write of its own.
    assert "UPDATE" not in SOURCE
    assert "DELETE" not in SOURCE


def test_it_proves_the_run_belongs_to_this_stream_and_this_project(
    monkeypatch,
) -> None:
    """A run of another project is the envelope of an ABSENT run.

    `stop_collection_run` proves (run, project); it cannot prove (run, stream),
    and a run of project A plus a stream of project A do not make that run this
    stream's.
    """
    recorder = _register(monkeypatch)
    body = " ".join(inspect.getsource(recorder.tools["stop_datastream_run"]).split())
    assert "WHERE id = %s AND project_id = %s AND datastream_id = %s" in body
    assert '"error": "not_found"' in body


def test_the_refusals_are_the_route_s_refusals(monkeypatch) -> None:
    """One code per situation, and the SAME codes both doors answer.

    A door with its own vocabulary lets a model report a refusal a person never
    sees, or the other way round.
    """
    from core import execution_progress

    recorder = _register(monkeypatch)
    body = inspect.getsource(recorder.tools["stop_datastream_run"])
    assert "except StopRefused" in body
    assert "exc.code" in body
    # Every refusal the core can raise is documented in the tool's own words,
    # which is what the model reads before it calls.
    doc = recorder.tools["stop_datastream_run"].__doc__ or ""
    for code in execution_progress.STOP_REFUSALS:
        if code == execution_progress.STOP_FORBIDDEN:
            continue  # the role is the transport's, never the tool's own answer
        assert code in doc, code


def test_the_tool_never_promises_an_interruption_it_cannot_perform() -> None:
    """A model told that a stop interrupts everything reports a false state.

    `_execute_job` is synchronous and the stale sweep is at 5400 s: the window in
    flight finishes. The docstring is the model's contract, so it says so.
    """
    doc = None
    tree = ast.parse(SOURCE)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "stop_datastream_run":
            doc = ast.get_docstring(node)
    assert doc is not None
    assert "NOT interrupted" in doc
    assert "append-only" in doc
    # And it says what does NOT happen next: nothing re-collects the refused days.
    assert "re-collected automatically" in " ".join(doc.split())


def test_the_answer_is_the_same_shape_the_console_reads(monkeypatch) -> None:
    """One payload for both doors, so neither can describe the stop differently."""
    doc = (
        _register(monkeypatch).tools["stop_datastream_run"].__doc__ or ""
    )
    for field in ("windows_refused", "window_in_flight", "days_kept", "rows_kept",
                  "stopped_at"):
        assert field in doc, field
        assert field in STOP_FIELDS, field


def test_the_tool_is_registered_at_boot() -> None:
    """A tool nobody registers is a door that does not exist."""
    main_source = (
        Path(stop_run_mcp.__file__).with_name("main.py").read_text(encoding="utf-8")
    )
    assert "from core.stop_run_mcp import register" in main_source
    assert "_register_stop_run_mcp(mcp)" in main_source
