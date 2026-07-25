"""Grep-gate: no inline 'default' project auto-bind survives in the tool paths.

Story 7.1 (AC5) replaces the scattered ``project_id or 'default'`` auto-bind with
the shared ``core.project_resolver.resolve_project_id`` helper. This gate proves
the migration is complete and does not regress:

  1. Every project-scoped tool in core.main routes through ``_resolve_project``.
  2. The bare fallback literal ``'default'`` no longer appears as an inline
     auto-bind expression (``project_id or "default"`` / ``project_id = "default"``
     assignments) anywhere in core.main or core.admin_api tool paths.

The sentinel is defined exactly once, in core.project_resolver
(``_LEGACY_DEFAULT_SENTINEL``); parameter *defaults* (``project_id: str =
"default"``) are intentionally preserved (tool signatures are unchanged per AC5)
and are NOT auto-bind expressions, so they are excluded from the scan.
"""

from __future__ import annotations

import re
from pathlib import Path

_CORE_DIR = Path(__file__).resolve().parents[2] / "core"

# Tools that accept project_id and must resolve it via the shared helper.
_RESOLVED_TOOLS = [
    "def get_daily_report(",
    "def get_report(",
    "def add_context_event(",
    "def submit_feedback(",
    "def save_notebook(",
]


def test_project_scoped_tools_call_resolver():
    """Each project-scoped tool body calls _resolve_project."""
    source = (_CORE_DIR / "main.py").read_text(encoding="utf-8")
    for marker in _RESOLVED_TOOLS:
        idx = source.index(marker)
        # Look at the ~120 lines following the def for a _resolve_project call.
        body = source[idx : idx + 6000]
        assert "_resolve_project(" in body, (
            f"{marker} does not route project_id through _resolve_project "
            "(Story 7.1 AC5 resolver gate)."
        )


def test_no_inline_default_autobind_in_tool_paths():
    """No inline 'default' auto-bind expression remains in core.main / admin_api.

    Forbidden shapes (the old auto-bind):
        project_id or "default"
        project_id = "default"   (assignment, not a parameter default)
        (body.get("project_id") or "default")
    Parameter defaults (`project_id: str = "default"`) are allowed and excluded.
    """
    # Auto-bind expressions: a 'default' string literal used as an `or` fallback
    # or as a plain assignment RHS (but not a typed parameter default which has
    # a ': type =' between the name and the value).
    forbidden = re.compile(
        r"""or\s*["']default["']"""       # `... or "default"`
        r"""|=\s*["']default["']\s*(?:#|$|\))"""  # `x = "default"` assignment
    )
    violations: list[str] = []
    for fname in ("main.py", "admin_api.py"):
        path = _CORE_DIR / fname
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            # Skip comment / docstring-prose lines (not executable auto-binds).
            if stripped.startswith("#") or stripped.startswith("*") or "``" in line:
                continue
            # Skip typed parameter defaults: `project_id: str = "default"`.
            if re.search(r""":\s*\w[\w\[\], |]*\s*=\s*["']default["']""", line):
                continue
            if forbidden.search(line):
                violations.append(f"{fname}:{i}: {stripped[:100]}")
    assert not violations, (
        "Inline 'default' project auto-bind still present (Story 7.1 AC5):\n"
        + "\n".join(violations)
    )
