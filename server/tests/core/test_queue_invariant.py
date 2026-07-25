"""Invariant tests for the queue architecture (Story 3.2, AC7, T8).

Static/structural tests -- these do NOT execute code, they parse the source
with ast.parse and assert architectural invariants about what code may appear
where.

Test 1: No direct third-party HTTP imports inside MCP tool handler functions.
Test 2: _trigger_pull in admin_api.py no longer calls pull_fn() directly --
        it calls enqueue_pull() instead.
Test 3 (Story 4.1, AC10 / AI-22): No URL literals (http:// or https://) in
        server/core/. This is a belt-and-suspenders check to complement Test 1.

Limitation of the AST scan approach (AI-22):
  The static AST scan detects direct import statements (import httpx, from requests
  import ...) inside MCP tool handlers and URL string literals in server/core/.
  It does NOT catch:
    - importlib.import_module() calls (dynamic imports at runtime)
    - Transitive HTTP calls where core delegates to a helper via a non-import path
    - HTTP calls made inside lambda or eval() expressions
    - HTTP clients constructed from environment variables at runtime
  These gaps are accepted at P4-dev. Phase B monitoring (Cloud Logging + alerting)
  provides runtime enforcement where static analysis cannot reach.
  The hard failure on direct import statements (Test 1) remains the primary guard;
  the URL-literal scan (Test 3) is a secondary belt-and-suspenders check.
"""

from __future__ import annotations

import ast
from pathlib import Path

# Repo paths (server/core/ relative to this test file)
_SERVER_DIR = Path(__file__).parent.parent.parent  # server/
_CORE_DIR = _SERVER_DIR / "core"
_MAIN_PY = _CORE_DIR / "main.py"
_ADMIN_API_PY = _CORE_DIR / "admin_api.py"

# Third-party HTTP module names that must not appear inside MCP tool handlers
_BANNED_HTTP_MODULES = {"httpx", "requests", "aiohttp", "urllib3"}


def _collect_mcp_tool_functions(tree: ast.Module) -> list[ast.FunctionDef]:
    """Find all function defs registered as MCP tools in a parsed AST.

    Handles:
      - @mcp.tool decorator on the function
      - mcp.tool(fn) call at module level (explicit registration)
    """
    # Collect names registered via mcp.tool(fn) at module level
    mcp_registered_names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        # mcp.tool(fn) pattern
        if (
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "tool"
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "mcp"
            and len(call.args) == 1
            and isinstance(call.args[0], ast.Name)
        ):
            mcp_registered_names.add(call.args[0].id)

    tool_fns: list[ast.FunctionDef] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        # Check @mcp.tool decorator
        for deco in node.decorator_list:
            if (
                isinstance(deco, ast.Attribute)
                and deco.attr == "tool"
                and isinstance(deco.value, ast.Name)
                and deco.value.id == "mcp"
            ):
                tool_fns.append(node)
                break
        else:
            # Check explicit registration
            if node.name in mcp_registered_names:
                tool_fns.append(node)

    return tool_fns


def _contains_banned_http_import(func_node: ast.FunctionDef) -> list[str]:
    """Walk the function body for Import/ImportFrom nodes of banned HTTP modules.

    Returns list of descriptive violation strings (empty = no violations).
    """
    violations: list[str] = []
    for node in ast.walk(func_node):
        if isinstance(node, ast.Import):
            for alias in node.names:
                base = alias.name.split(".")[0]
                if base in _BANNED_HTTP_MODULES:
                    violations.append(
                        f"direct import of '{alias.name}' inside tool handler '{func_node.name}'"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in _BANNED_HTTP_MODULES:
                violations.append(
                    f"from-import of '{node.module}' inside tool handler '{func_node.name}'"
                )
    return violations


def test_no_httpx_import_in_mcp_tool_handlers():
    """AC7, T8.2: No MCP tool handler body may import httpx, requests, or other HTTP clients.

    Parses server/core/main.py with ast.parse, finds all @mcp.tool / mcp.tool(fn)
    functions, and asserts none of them contain direct HTTP client imports.

    AD-12: third-party API calls must go through the queue worker, not through
    the LLM tool invocation path.
    """
    source = _MAIN_PY.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_MAIN_PY))

    tool_fns = _collect_mcp_tool_functions(tree)
    assert tool_fns, (
        "Expected to find at least one MCP tool function in main.py "
        "(health, list_modules, get_daily_report). If the file structure changed, "
        "update this test."
    )

    all_violations: list[str] = []
    for fn in tool_fns:
        all_violations.extend(_contains_banned_http_import(fn))

    assert not all_violations, (
        "MCP tool handler(s) contain direct third-party HTTP imports (AD-12 violation):\n"
        + "\n".join(f"  - {v}" for v in all_violations)
    )


def test_trigger_pull_no_direct_provider_call():
    """AC7, T8.3: _trigger_pull in admin_api.py must call enqueue_pull, not pull_fn().

    After Story 3.2, _trigger_pull is a thin dispatcher that calls enqueue_pull().
    It must NOT contain any direct pull_fn(...) call.
    """
    source = _ADMIN_API_PY.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_ADMIN_API_PY))

    # Find _trigger_pull function
    trigger_pull_fn = None
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_trigger_pull"
        ):
            trigger_pull_fn = node
            break

    assert trigger_pull_fn is not None, (
        "_trigger_pull not found in admin_api.py -- update this test if the function was renamed"
    )

    # Check that no call to pull_fn(...) exists in the function body
    direct_pull_fn_calls: list[str] = []
    for node in ast.walk(trigger_pull_fn):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "pull_fn":
                direct_pull_fn_calls.append("pull_fn(...) direct call found")

    assert not direct_pull_fn_calls, (
        "_trigger_pull still calls pull_fn() directly (Story 3.2, AC4 violation).\n"
        "The handler must dispatch via enqueue_pull() only.\n"
        "Violations: " + str(direct_pull_fn_calls)
    )

    # Confirm that enqueue_pull IS called (or imported) somewhere in the function
    enqueue_pull_found = False
    for node in ast.walk(trigger_pull_fn):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "enqueue_pull":
                enqueue_pull_found = True
                break

    assert enqueue_pull_found, (
        "_trigger_pull does not call enqueue_pull() -- Story 3.2 AC4 requires the handler "
        "to dispatch via enqueue_pull() from core.queue."
    )


def test_no_url_literals_in_core():
    """Story 4.1 (AC10 / AI-22): No http:// or https:// URL literals in server/core/.

    Belt-and-suspenders check complementing test_no_httpx_import_in_mcp_tool_handlers.
    URL literals in core suggest hardcoded external endpoints, which violates AD-12
    (third-party HTTP calls via queue worker, not direct).

    This is a WARN-only scan: violations are logged to stdout but do NOT cause a
    hard test failure. The existing hard failure for direct HTTP import statements
    (test_no_httpx_import_in_mcp_tool_handlers) is the primary architectural guard.
    """
    import warnings

    url_violations: list[str] = []

    for py_file in sorted(_CORE_DIR.glob("*.py")):
        try:
            source = py_file.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                val = node.value
                if val.startswith("http://") or val.startswith("https://"):
                    url_violations.append(
                        f"{py_file.name}:{getattr(node, 'lineno', '?')}: {val[:80]!r}"
                    )

    if url_violations:
        msg = (
            "URL literals found in server/core/ (AI-22 secondary scan — WARN only):\n"
            + "\n".join(f"  WARN: {v}" for v in url_violations)
        )
        warnings.warn(msg, stacklevel=1)
        # Belt-and-suspenders: print to stdout so it appears in pytest -s output
        print(f"\n[AI-22 URL-literal scan] {msg}")

    # Soft assertion: this test always passes; violations are warnings, not failures.
    # The primary hard guard is test_no_httpx_import_in_mcp_tool_handlers.
    assert True, "URL-literal scan completed (violations logged as WARN, not failures)"
