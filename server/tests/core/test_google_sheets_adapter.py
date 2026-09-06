"""The closure that joins a connection to the Sheets reader.

Both halves existed and never met: `run_sync` calls its adapter with
`(connection_id, spreadsheet_id, sheet_range)`, and
`modules/google-sheets/connector._fetch_sheet_values` takes
`(token, spreadsheet_id, sheet_range)`. Nobody wrote the six lines between, so
both live callers passed `sheets_adapter=None` and every Sheets sync raised
before reading a cell.
"""

from __future__ import annotations

import sys
import types

import pytest
from inbound.adapters.google_sheets_adapter import google_sheets_values_adapter


@pytest.fixture()
def fake_connector(monkeypatch):
    """Stand in for `modules.google-sheets.connector`, whose name is not an identifier."""
    module = types.ModuleType("modules.google-sheets.connector")
    calls: list[tuple] = []

    def _fetch_sheet_values(token, spreadsheet_id, sheet_range, **kwargs):
        calls.append((token, spreadsheet_id, sheet_range))
        return [["day", "clicks"], ["2026-07-01", "5"]]

    module._fetch_sheet_values = _fetch_sheet_values
    monkeypatch.setitem(sys.modules, "modules.google-sheets.connector", module)
    return calls


def test_the_adapter_resolves_the_token_and_returns_the_sheet_rows(monkeypatch, fake_connector):
    monkeypatch.setattr("core.nango_client.get_fresh_token", lambda cid, provider=None: "tok-123")

    values = google_sheets_values_adapter()("conn_1", "sheet_1", "Tab!A:B")

    assert values == [["day", "clicks"], ["2026-07-01", "5"]]
    assert fake_connector == [("tok-123", "sheet_1", "Tab!A:B")], (
        "the connection resolves to a token here, and only here"
    )


def test_the_adapter_matches_the_shape_run_sync_calls(monkeypatch, fake_connector):
    """`run_sync` calls `sheets_adapter(connection_id, spreadsheet_id, sheet_range)`.

    Asserted positionally on purpose: the whole defect was a signature mismatch
    between two functions that each worked.
    """
    monkeypatch.setattr("core.nango_client.get_fresh_token", lambda cid, provider=None: "t")

    adapter = google_sheets_values_adapter()

    assert adapter("c", "s", "r") == [["day", "clicks"], ["2026-07-01", "5"]]


def test_a_failing_fetch_propagates_untouched(monkeypatch, fake_connector):
    """`run_sync` classifies adapter errors and re-raises rate limits itself.

    Catching here would swallow the distinction it depends on -- a rate limit
    must reach the worker breaker, not become a safe-fail.
    """
    monkeypatch.setattr("core.nango_client.get_fresh_token", lambda cid, provider=None: "t")

    boom = RuntimeError("sheets exploded")
    sys.modules["modules.google-sheets.connector"]._fetch_sheet_values = (
        lambda *a, **k: (_ for _ in ()).throw(boom)
    )

    with pytest.raises(RuntimeError, match="sheets exploded"):
        google_sheets_values_adapter()("c", "s", "r")


def test_the_scheduler_no_longer_passes_none():
    """The regression that mattered: a live caller that injects nothing.

    Read from the AST, not by substring. A substring check also matches the
    docstring that EXPLAINS the old `sheets_adapter=None` -- so it would fail on
    prose describing the fix, and would pass on a call site that quietly went
    back to None inside a differently-spelled expression. What matters is the
    keyword argument actually passed.
    """
    import ast  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    tree = ast.parse(
        (Path(__file__).resolve().parents[3] / "server" / "core" / "scheduler.py").read_text(
            "utf-8"
        )
    )

    injected: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "sheets_adapter":
                continue
            assert not (
                isinstance(keyword.value, ast.Constant) and keyword.value.value is None
            ), "a scheduled Sheets sync with no adapter raises before reading a cell"
            injected.append(ast.dump(keyword.value))

    assert injected, "the scheduler no longer passes sheets_adapter at all"
    # The source-specific factory now crosses the source-agnostic inbound seam;
    # the scheduler receives the resolved factory rather than importing it.
    assert any("values_adapter_factory" in dumped for dumped in injected)


def test_the_sync_now_route_no_longer_passes_none():
    """The OTHER live caller: POST .../managed-feed/sync-now.

    Same AST discipline as the scheduler test above: the route used to pass
    `sheets_adapter=None` with a PHASE_B_LIVE_BLOCKED comment and map the
    resulting NotImplementedError to a 503 -- a sync-now button that could
    never read a cell.
    """
    import ast  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    tree = ast.parse(
        (Path(__file__).resolve().parents[3] / "server" / "core" / "file_import_api.py").read_text(
            "utf-8"
        )
    )

    injected: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "sheets_adapter":
                continue
            assert not (
                isinstance(keyword.value, ast.Constant) and keyword.value.value is None
            ), "a sync-now with no adapter raises before reading a cell"
            injected.append(ast.dump(keyword.value))

    assert injected, "the sync-now route no longer passes sheets_adapter at all"
    # Resolved through the same inbound capability the scheduler uses.
    assert any("managed_feed_values_adapter_factory" in dumped for dumped in injected)
