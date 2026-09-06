"""An unreadable renderer-build ledger is not an empty one (audit 2026-08-25, M4).

`_registered_families` used to answer `[]` on a failed read, under a comment that
said in so many words "an unreadable ledger is not an empty one". A caller reading
that list -- the console, the MCP door, `e2e/gates/g14_rendered_output.py` -- had
no way to tell "this deployment registers no renderer" from "the service could not
find out", and the first of those two is a reason to refuse a Render.
`visualization-and-rendering.md:209` and `analyze-and-test.md:1648` both forbid
exactly that flattening.

WHY NO POSTGRESQL HERE. What is under test is a branch, not a constraint: which
state the function reports when a read raises, when it returns nothing, and when
the registries are absent. A real database can produce the second and third
easily and the first only by breaking itself. So the connection is a stub whose
cursor is told what to do -- and the pg-gated suites keep proving the parts that
are claims about the schema.
"""

from __future__ import annotations

import pytest
from core.analyze_artifacts import (
    FAMILIES_CONTRACT_UNAVAILABLE,
    FAMILIES_READ,
    FAMILIES_UNREADABLE,
    LEDGER_UNREADABLE_REASON,
    LedgerUnreadable,
    _registered_families,
    render_contract_state,
)


class _Cursor:
    """Answers `to_regclass` probes, then the ledger read -- or refuses to."""

    def __init__(self, *, tables_exist: bool, ledger_rows, ledger_error):
        self._tables_exist = tables_exist
        self._ledger_rows = ledger_rows
        self._ledger_error = ledger_error
        self._row = None
        self._rows: list = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None):
        if "to_regclass" in sql:
            self._row = (self._tables_exist,)
            return
        if self._ledger_error is not None:
            raise self._ledger_error
        self._rows = list(self._ledger_rows)

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, *, tables_exist=True, ledger_rows=(), ledger_error=None):
        self._kwargs = {
            "tables_exist": tables_exist,
            "ledger_rows": ledger_rows,
            "ledger_error": ledger_error,
        }

    def cursor(self):
        return _Cursor(**self._kwargs)


def test_a_ledger_that_cannot_be_read_raises_instead_of_answering_nothing():
    """The read fails loud. Returning `[]` here is the defect, not the fallback."""
    with pytest.raises(LedgerUnreadable):
        _registered_families(_Conn(ledger_error=RuntimeError("connection lost")))


def test_an_unreadable_ledger_and_an_empty_one_are_two_different_answers():
    """The property, in one assertion pair: the lists match, the states do not."""
    unreadable = render_contract_state(_Conn(ledger_error=RuntimeError("connection lost")))
    empty = render_contract_state(_Conn(ledger_rows=()))

    assert unreadable["registered_families"] == empty["registered_families"] == []
    assert unreadable["registered_families_state"] == FAMILIES_UNREADABLE
    assert empty["registered_families_state"] == FAMILIES_READ


def test_the_unreadable_answer_names_the_gesture_and_never_the_driver():
    """A refusal names what to do about it (`first-figure-path.md:141-151`)."""
    state = render_contract_state(_Conn(ledger_error=RuntimeError("FATAL: too many clients")))
    reason = state["registered_families_unavailable_reason"]

    assert reason == LEDGER_UNREADABLE_REASON
    assert "Re-run" in reason and "ask an administrator" in reason
    # The exception's own words go to the log, never into the sentence.
    assert "FATAL" not in reason and "RuntimeError" not in reason
    # And the registries themselves are still there: an unreadable ledger does not
    # retract a contract the catalogue reports as present.
    assert state["available"] is True
    assert state["missing"] == []


def test_a_read_that_answered_carries_no_unavailable_reason():
    """A state that always carried a reason would make the reason meaningless."""
    rows = [
        (
            "table",
            "table/toorow-echarts-table@1.0.0",
            "+abc",
            "theme.1",
            "fmt.1",
            "toorow-echarts-table",
        )
    ]
    state = render_contract_state(_Conn(ledger_rows=rows))

    assert state["registered_families_state"] == FAMILIES_READ
    assert "registered_families_unavailable_reason" not in state
    assert [f["family"] for f in state["registered_families"]] == ["table"]
    #  AI-357: the adapter is the FIFTH build property the ledger serves. A caller
    #  filling every pin the registry offers must not still be one column short of
    #  what `ck_renders_pins_are_exact` requires.
    assert state["registered_families"][0]["renderer_adapter"] == "toorow-echarts-table"


def test_an_absent_registry_is_a_third_state_and_not_an_unreadable_one():
    """`contract_unavailable` says the ledger was never asked, and `missing` why."""
    state = render_contract_state(_Conn(tables_exist=False))

    assert state["available"] is False
    assert state["registered_families_state"] == FAMILIES_CONTRACT_UNAVAILABLE
    assert state["registered_families"] == []
    assert "registered_families_unavailable_reason" not in state
    assert {m["missing_link"] for m in state["missing"]} == {
        "app.visualization_spec_versions",
        "app.renderer_runtime_builds",
    }


def test_registered_families_offer_the_served_runtime_pins_not_the_ledger_row(
    monkeypatch, tmp_path
):
    """2026-09-04: the ledger row keeps the runtime that FIRST registered the renderer
    build; the contract must offer the runtime this deployment serves, or G14 freezes
    a pin the running runtime refuses (measured on mcp-server-00228, the day it shipped).
    The adapter and the family stay the ledger's; only the three build properties move,
    and only for a renderer build the served manifest declares."""
    import hashlib
    import json

    bundle = tmp_path / "mcp-app.html"
    bundle.write_text("<!doctype html>", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
        "runtime_build": "@toorow/card-shell/viz@0.1.0+c12d45ac065a",
        "theme_version": "viz-theme@2",
        "formatter_version": "viz-formatters@2",
        "renderers": {
            "table": {
                "renderer_build": "table/toorow-table@1.0.0",
                "schema_versions": {"min": 1, "max": 1},
                "profiles": ["console"],
            }
        },
    }
    bundle.with_name("runtime-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("TOOROW_VISUALIZATION_RUNTIME_DIST", str(bundle))
    rows = [
        (
            "table",
            "table/toorow-table@1.0.0",
            "+old",
            "viz-theme@1",
            "viz-formatters@1",
            "toorow-table",
        ),
        ("kpi", "kpi/toorow-kpi@1.0.0", "+old", "viz-theme@1", "viz-formatters@1", "toorow-kpi"),
    ]
    families = {f["family"]: f for f in _registered_families(_Conn(ledger_rows=rows))}
    # The served renderer build takes the served runtime, theme and formatter...
    assert families["table"]["runtime_build_id"] == "@toorow/card-shell/viz@0.1.0+c12d45ac065a"
    assert families["table"]["theme_version"] == "viz-theme@2"
    assert families["table"]["formatter_version"] == "viz-formatters@2"
    assert families["table"]["renderer_adapter"] == "toorow-table"
    # ...and a renderer build the manifest does not declare is served as recorded.
    assert families["kpi"]["runtime_build_id"] == "+old"
