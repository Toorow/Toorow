"""`query-options` read an ALREADY-CLOSED cursor -- and 500'd on every call.

What this file guards, and why it did not exist. `with conn.cursor() as cur:`
covered only the FIRST `execute`; the `fetchone`, the second `execute` and its
`fetchone` were dedented one level. The code reads perfectly, it passes review, it
passes the linter -- and it raises `InterfaceError: the cursor is closed` on the
first real call. The route is called by the console's Explore door
(`ui/admin/src/analyze/QueryDoor.tsx`) and had NO server test at all: the only
thing that could find it was a real call, and G13-T05 found it, on production, on
2026-08-12.

TWO GUARDS, NOT ONE. The first replays the route against a cursor that behaves
like psycopg: it REFUSES to be used after it closes. A permissive double would
turn this test green on the broken code, which is exactly the trap that let the
defect through. The second re-reads all of `server/` and refuses the whole CLASS:
a cursor used outside the block that owns it, anywhere.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from core import query_specs_api
from starlette.requests import Request

PROJECT = "proj_EXAMPLE"
VIEW = "sv_01EXAMPLE0000000000000000"
VERSION = "svv_01EXAMPLE000000000000000"

MATRIX = {
    "metrics": [{"concept_id": "sc_01EXAMPLE0000000000000000", "label": "Views"}],
    "dimensions": [{"concept_id": "sc_01EXAMPLE0000000000000001", "label": "Date"}],
    "cells": [
        {
            "metric_id": "sc_01EXAMPLE0000000000000000",
            "dimension_id": "sc_01EXAMPLE0000000000000001",
            "queryable": True,
        }
    ],
}


class CursorClosed(RuntimeError):
    """What psycopg raises: a cursor does not outlive its `with`."""


class StrictCursor:
    """A cursor that refuses to be read after it closes -- like the real one.

    The whole point of the double: an accommodating fake turns the test green on
    broken code. This one returns the same verdict production does.
    """

    def __init__(self, answers: list):
        self._answers = list(answers)
        self._row = None
        self.closed = False
        self.executed = 0

    def __enter__(self) -> StrictCursor:
        return self

    def __exit__(self, *_exc) -> bool:
        self.closed = True
        return False

    def _guard(self) -> None:
        if self.closed:
            raise CursorClosed("the cursor is closed")

    def execute(self, _sql, _params=None) -> None:
        self._guard()
        self.executed += 1
        self._row = self._answers.pop(0) if self._answers else None

    def fetchone(self):
        self._guard()
        return self._row


class FakeConnection:
    def __init__(self, cursor: StrictCursor):
        self._cursor = cursor

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def cursor(self) -> StrictCursor:
        return self._cursor


def _request() -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/api/projects/{PROJECT}/analyze/query-options",
        "headers": [],
        "query_string": (
            f"semantic_view_id={VIEW}&semantic_view_version_id={VERSION}"
        ).encode(),
        "path_params": {"project_id": PROJECT},
    }
    return Request(scope)


@pytest.fixture
def cursor(monkeypatch) -> StrictCursor:
    """A published view, its compiled matrix, and authorization already granted."""
    strict = StrictCursor([("published",), (MATRIX,)])

    async def _authorized(_request, _role="viewer"):
        return ("person_01EXAMPLE00000000000000", "org_01EXAMPLE0000000000000")

    monkeypatch.setattr(query_specs_api, "_authorize", _authorized)
    monkeypatch.setattr(
        query_specs_api, "analyze_connection", lambda _identity: FakeConnection(strict)
    )
    return strict


@pytest.mark.anyio
async def test_query_options_reads_both_rows_inside_the_cursor_block(cursor):
    """BOTH reads complete: exactly what the closed cursor prevented."""
    response = await query_specs_api._query_options(_request())

    assert response.status_code == 200
    assert cursor.executed == 2, "the second read never happened"


@pytest.mark.anyio
async def test_query_options_returns_the_compiled_matrix(cursor):
    """The body carries the matrix -- not an empty envelope obtained by accident."""
    import json

    payload = json.loads((await query_specs_api._query_options(_request())).body)

    assert payload["executable"] is True
    assert payload["measures"] == MATRIX["metrics"]
    assert payload["dimensions"] == MATRIX["dimensions"]
    assert payload["pairs"] == [
        {
            "measure_id": MATRIX["cells"][0]["metric_id"],
            "dimension_id": MATRIX["cells"][0]["dimension_id"],
            "queryable": True,
        }
    ]


# ---------------------------------------------------------------------------
# The whole class, not the instance.
# ---------------------------------------------------------------------------

SERVER = pathlib.Path(__file__).resolve().parents[2]


def _cursor_uses_after_close(tree: ast.AST) -> list[tuple[int, str]]:
    """Every `cur.<...>` located AFTER the close of the `with` that bound `cur`.

    Bounded to the function that owns the `with`, and stopped by a new binding of
    the same name -- otherwise any reuse of the name `cur` in another function of
    the module would make the guard shout at healthy code.
    """
    found: list[tuple[int, str]] = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        bindings: list[tuple[str, int, int]] = []
        for node in ast.walk(func):
            if not isinstance(node, (ast.With, ast.AsyncWith)):
                continue
            for item in node.items:
                call = item.context_expr
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "cursor"
                    and isinstance(item.optional_vars, ast.Name)
                ):
                    end = max(
                        (
                            getattr(inner, "lineno", node.lineno)
                            for stmt in node.body
                            for inner in ast.walk(stmt)
                        ),
                        default=node.lineno,
                    )
                    bindings.append((item.optional_vars.id, node.lineno, end))
        for name, _start, end in bindings:
            rebound = min(
                (line for other, line, _e in bindings if other == name and line > end),
                default=10**9,
            )
            for use in ast.walk(func):
                if (
                    isinstance(use, ast.Attribute)
                    and isinstance(use.value, ast.Name)
                    and use.value.id == name
                    and end < use.lineno < rebound
                ):
                    found.append((use.lineno, f"{name}.{use.attr}"))
    return sorted(set(found))


def test_no_module_uses_a_cursor_after_its_block_closes():
    offenders: list[str] = []
    for path in sorted(SERVER.rglob("*.py")):
        if "tests" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # a deliberately non-importable file
            continue
        for line, use in _cursor_uses_after_close(tree):
            offenders.append(f"{path.relative_to(SERVER)}:{line} {use}")
    assert offenders == [], "cursor used after its block closed:\n" + "\n".join(offenders)
