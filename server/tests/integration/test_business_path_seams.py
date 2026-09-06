"""Traceable business path and MCP proofs for Story 45.1."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core.business_taxonomy import (
    build_path_key,
    resolve_business_path,
    resolve_report_paths,
)
from core.main import mcp


def _path():
    return [
        {"kind": "node", "node_type": "business_domain", "id": "bdm_sales", "version_number": 2},
        {"kind": "edge", "id": "blink_01", "edge_type": "explains", "link_origin": "direct"},
        {"kind": "node", "node_type": "report_view", "id": "ga4/acquisition", "version_number": 1},
    ]


def test_path_key_is_deterministic_and_version_sensitive():
    first = build_path_key("proj_01", _path())
    assert first == build_path_key("proj_01", _path())
    changed = _path()
    changed[0]["version_number"] = 3
    assert first != build_path_key("proj_01", changed)
    assert first.startswith("ctxp_")


def test_resolved_path_snapshot_is_persisted_with_trace_and_purpose():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    with patch("core.business_taxonomy._load_direct_path", return_value=_path()):
        result = resolve_business_path(
            conn,
            org_id="org_01",
            project_id="proj_01",
            target_type="report_view",
            target_id="ga4/acquisition",
            purpose="evaluation",
            actor="eval-runner",
            trace_id="trace_01",
        )
    assert result is not None
    assert result["path_key"] == build_path_key("proj_01", _path())
    insert = next(
        call
        for call in cur.execute.call_args_list
        if "INSERT INTO app.context_path_resolutions" in call.args[0]
    )
    params = insert.args[1]
    assert "evaluation" in params
    assert "trace_01" in params
    assert result["ordered_path"] == _path()


def test_report_paths_reuse_report_chain_and_mark_inherited_routes_as_derived():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    field_path = [
        {
            "kind": "node",
            "node_type": "business_domain",
            "id": "bdm_sales",
            "version_number": 2,
        },
        {
            "kind": "edge",
            "id": "blink_field",
            "edge_type": "defines",
            "link_origin": "direct",
        },
        {
            "kind": "node",
            "node_type": "target_field",
            "id": "sessions",
            "version_number": 4,
        },
    ]
    chain = {
        "report_id": "ga4/acquisition",
        "metrics": [
            {"metric": "sessions", "target_field": {"name": "sessions"}},
            {"metric": "unknown", "target_field": None},
        ],
    }
    with (
        patch("core.business_taxonomy._load_direct_path") as load_path,
        patch("core.report_chain.get_report_chain", return_value=chain),
    ):
        load_path.side_effect = [None, field_path]
        results = resolve_report_paths(
            conn,
            org_id="org_01",
            project_id="proj_01",
            report_id="ga4/acquisition",
            actor="report-runner",
            trace_id="trace_01",
        )

    assert len(results) == 1
    result = results[0]
    assert result["link_origin"] == "derived"
    assert result["target"] == {"type": "report_view", "id": "ga4/acquisition"}
    assert result["ordered_path"][-1]["node_type"] == "report_view"
    assert result["ordered_path"][-2]["edge_type"] == "feeds_report"
    insert = next(
        call
        for call in cur.execute.call_args_list
        if "INSERT INTO app.context_path_resolutions" in call.args[0]
    )
    assert "derived" in insert.args[1]


@pytest.mark.anyio
async def test_resolve_business_path_mcp_is_bounded_and_records_trace_evidence():
    resolved = {
        "id": "path_01",
        "path_key": "ctxp_abc123",
        "purpose": "llm",
        "trace_id": "a" * 32,
        "target": {"type": "report_view", "id": "ga4/acquisition"},
        "link_origin": "direct",
        "ordered_path": _path(),
        "source_versions": [
            {"node_type": "business_domain", "id": "bdm_sales", "version_number": 2}
        ],
    }
    conn = MagicMock()
    conn.__enter__.return_value = conn
    with (
        patch("core.main._resolve_project", return_value="proj_01"),
        patch("core.db.get_connection", return_value=conn),
        patch("core.project_access.resolve_strict_resource_access") as access,
        patch("core.business_taxonomy.resolve_business_path", return_value=resolved),
        patch("core.tracing.record_current_span_attributes") as trace_attrs,
    ):
        access.return_value.allowed = True
        access.return_value.org_id = "org_01"
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "resolve_business_path",
                {
                    "target_type": "report_view",
                    "target_id": "ga4/acquisition",
                    "project_id": "proj_01",
                },
            )
    assert not result.is_error
    envelope = result.structured_content or result.data
    assert envelope["data"]["path_key"] == "ctxp_abc123"
    text = next(block.text for block in result.content if getattr(block, "text", None))
    assert len(text.splitlines()) <= 4
    trace_attrs.assert_called()


@pytest.mark.anyio
async def test_resolve_business_path_is_core_owned_and_unnamespaced():
    async with Client(FastMCPTransport(mcp)) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert "resolve_business_path" in names
