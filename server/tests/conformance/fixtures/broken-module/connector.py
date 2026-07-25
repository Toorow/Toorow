"""Broken module connector — test fixture for Story 1.8 (T7.3).

This connector intentionally returns data OUTSIDE the canonical envelope
to verify that layer 2 (envelope shape) catches the violation.
"""

from __future__ import annotations

from fastmcp import FastMCP

mcp_app = FastMCP("broken-module")


@mcp_app.tool()
def broken_report(project_id: str = "default") -> dict:
    """Broken report tool that returns a non-envelope response."""
    # Intentional violation: no schema_version, meta, or data keys
    return {"result": []}


def transform(raw_rows: list[dict]) -> list[dict]:
    """Transform raw rows — intentionally produces wrong output for layer 4 testing."""
    out = []
    for row in raw_rows:
        transformed = dict(row)
        # Intentional violation: active_users off by 1
        if "active_users" in transformed:
            transformed["active_users"] = transformed["active_users"] - 1
        out.append(transformed)
    return out
