"""Exact contracts for the reproducible MCP surface instrument."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[3] / "scripts"))

import mcp_tool_surface_report  # noqa: E402


def test_report_separates_host_routing_from_model_skill_projection():
    report = mcp_tool_surface_report._collect()

    assert report["host_routed_app_only_tools"] == [
        "app_read_result_slice",
        "submit_feedback",
    ]
    assert report["callable_by_name_app_only_tools"] == [
        "app_read_result_manifest",
        "app_record_evidence_inspection",
        "submit_analyze_feedback",
    ]
    assert report["model_skill_app_only_tools"] == []
    for name in ("app_read_result_slice", "submit_feedback"):
        assert report["wire_tool_names"].count(name) == 1
        assert report["model_skill_tool_names"].count(name) == 0
