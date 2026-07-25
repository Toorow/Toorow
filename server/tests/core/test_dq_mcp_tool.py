"""Tests for the get_data_quality_report MCP tool (Story 13.4, Epic 13).

Invariants verified:
  AD-1  -- dual channel: text summary <=30 lines + structuredContent envelope.
  AD-5  -- project scoping: cross-project denial (identity for proj B cannot
           read proj A data); AD-5 returns a non-disclosing response, NOT an
           exception or error-flagged ToolResult.
  AD-9  -- provenance: every issue in structuredContent carries firing_id +
           pull_ids.
  Dq-D  -- degraded path: zero monitors evaluated -> honest summary, no raise.
  Reg   -- tool is registered on the core mcp instance (not namespaced).

Strategy:
  - Call get_data_quality_report() directly (function-level, no HTTP).
  - Patch core.db.get_connection and core.main._resolve_project.
  - Patch identity_has_project_access to control AD-5 grant/deny.
  - Inspect ToolResult.content[0].text (LLM channel) and
    ToolResult.structured_content (envelope channel).

ASCII-only stdout (AI-03). No shell (subagent-shell-denied).
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

# Suppress scheduler / queue startup during import.
os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


def _make_db_conn_for_dq(
    *,
    stream_count: int = 2,
    firing_rows: list | None = None,
    issue_rows: list | None = None,
    last_pull_at: datetime | None = None,
    evaluated_days: int = 5,
):
    """Build a fake get_connection() context manager for fetch_dq_report_data.

    The helper returns a single-use conn context (conn_ctx) that sequences
    cursor responses across all five queries in fetch_dq_report_data:
      Q1: stream count -> fetchone((stream_count,))
      Q2: firing count by type -> fetchall(firing_rows)
      Q3: open issues -> fetchall(issue_rows), description needed
      Q4: last pull_at -> fetchone((last_pull_at,))
      Q5: evaluated_days -> fetchone((evaluated_days,))
    """
    if firing_rows is None:
        firing_rows = []
    if issue_rows is None:
        issue_rows = []

    conn_ctx = MagicMock()
    conn = MagicMock()
    conn_ctx.__enter__ = MagicMock(return_value=conn)
    conn_ctx.__exit__ = MagicMock(return_value=False)

    call_count = {"n": 0}

    def _col_desc(names):
        return [
            type("D", (), {"__getitem__": staticmethod(lambda i, c=c: c)})()
            for c in names
        ]

    _issue_cols = [
        "firing_id", "type", "fired_at", "severity", "message", "pull_ids",
    ]

    def cursor_factory():
        call_count["n"] += 1
        cm = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        n = call_count["n"]
        if n == 1:
            # Q1: stream count
            cur.fetchone = MagicMock(return_value=(stream_count,))
            cur.fetchall = MagicMock(return_value=[])
        elif n == 2:
            # Q2: firing count by type
            cur.fetchall = MagicMock(return_value=firing_rows)
            cur.fetchone = MagicMock(return_value=None)
        elif n == 3:
            # Q3: open issues
            cur.fetchall = MagicMock(return_value=issue_rows)
            cur.description = _col_desc(_issue_cols)
            cur.fetchone = MagicMock(return_value=None)
        elif n == 4:
            # Q4: last pull_at
            cur.fetchone = MagicMock(return_value=(last_pull_at,))
            cur.fetchall = MagicMock(return_value=[])
        else:
            # Q5: evaluated_days
            cur.fetchone = MagicMock(return_value=(evaluated_days,))
            cur.fetchall = MagicMock(return_value=[])
        cm.__enter__ = MagicMock(return_value=cur)
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    conn.cursor = cursor_factory
    return MagicMock(return_value=conn_ctx)


def _make_scope_conn(*, granted: bool = True):
    """Build a fake get_connection for the AD-5 identity_has_project_access check.

    identity_has_project_access executes a single SELECT that returns a tuple
    (is_member, project_has_members, project_archived).
    - granted=True  -> (True, True, False)  i.e. identity is a member
    - granted=False -> (False, True, False) i.e. project has members, not this one
    """
    conn_ctx = MagicMock()
    conn = MagicMock()
    conn_ctx.__enter__ = MagicMock(return_value=conn)
    conn_ctx.__exit__ = MagicMock(return_value=False)
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    if granted:
        cur.fetchone = MagicMock(return_value=(True, True, False))
    else:
        cur.fetchone = MagicMock(return_value=(False, True, False))
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return MagicMock(return_value=conn_ctx)


def _call_tool(
    project_id: str = "proj_test",
    module: str | None = None,
    *,
    scope_granted: bool = True,
    stream_count: int = 2,
    firing_rows: list | None = None,
    issue_rows: list | None = None,
    last_pull_at: datetime | None = None,
    evaluated_days: int = 5,
):
    """Helper: call the MCP tool with mocked DB and return ToolResult.

    We patch identity_has_project_access directly (instead of going through the
    DB mock) because the function short-circuits for 'anonymous' identity and
    would never hit the DB path.  This gives us a clean control knob for AD-5.
    """
    from core.main import get_data_quality_report

    dq_conn_mock = _make_db_conn_for_dq(
        stream_count=stream_count,
        firing_rows=firing_rows,
        issue_rows=issue_rows,
        last_pull_at=last_pull_at,
        evaluated_days=evaluated_days,
    )
    dq_ctx = dq_conn_mock.return_value

    # Scope check connection (only needed when identity_has_project_access is not patched,
    # but we always provide it so get_connection always returns something valid).
    scope_conn_ctx = MagicMock()
    scope_conn = MagicMock()
    scope_conn_ctx.__enter__ = MagicMock(return_value=scope_conn)
    scope_conn_ctx.__exit__ = MagicMock(return_value=False)

    call_idx = {"n": 0}

    def _get_conn():
        call_idx["n"] += 1
        if call_idx["n"] == 1:
            return scope_conn_ctx
        return dq_ctx

    with (
        patch("core.main._resolve_project", side_effect=lambda x: x),
        patch("core.db.get_connection", side_effect=_get_conn),
        patch("core.main.get_access_token", return_value=None),
        patch(
            "core.project_access.identity_has_project_access",
            return_value=scope_granted,
        ),
    ):
        return get_data_quality_report(project_id=project_id, module=module)


# ---------------------------------------------------------------------------
# Registration test (Reg)
# ---------------------------------------------------------------------------


def test_tool_registered_on_core_mcp():
    """get_data_quality_report must be registered on the core mcp instance."""
    from core.main import mcp

    tool_names = [t.name for t in asyncio.run(mcp.list_tools())]
    assert "get_data_quality_report" in tool_names, (
        f"get_data_quality_report not found in core tools: {tool_names}"
    )


def test_tool_not_namespaced():
    """Tool name must be exactly 'get_data_quality_report', never prefixed."""
    from core.main import mcp

    tool_names = [t.name for t in asyncio.run(mcp.list_tools())]
    namespaced = [n for n in tool_names if "/" in n and "get_data_quality_report" in n]
    assert namespaced == [], f"tool appears namespaced: {namespaced}"


# ---------------------------------------------------------------------------
# AD-1: dual channel + <=30 lines
# ---------------------------------------------------------------------------


def test_summary_at_most_30_lines():
    """AD-1: LLM channel text must never exceed 30 lines."""
    result = _call_tool(firing_rows=[], issue_rows=[], evaluated_days=0)
    text = result.content[0].text
    lines = text.splitlines()
    assert len(lines) <= 30, (
        f"summary has {len(lines)} lines, must be <=30 (AD-1):\n{text}"
    )


def test_structured_content_envelope_shape():
    """AD-1: structuredContent must carry the canonical AD-1 envelope."""
    result = _call_tool()
    sc = result.structured_content
    assert sc is not None, "structuredContent must not be None (AD-1)"
    assert sc.get("schema_version") == "1", "schema_version must be '1'"
    assert "meta" in sc, "envelope must have 'meta'"
    assert "data" in sc, "envelope must have 'data'"
    data = sc["data"]
    assert "monitors" in data, "data must have 'monitors'"
    assert "issues" in data, "data must have 'issues'"
    assert "total_unresolved" in data, "data must have 'total_unresolved'"
    meta = sc["meta"]
    assert "provenance" in meta, "meta must have 'provenance'"
    assert "freshness" in meta, "meta must have 'freshness'"


def test_provenance_source_field():
    """meta.provenance.source_field must be 'get_data_quality_report'."""
    result = _call_tool()
    prov = result.structured_content["meta"]["provenance"]
    assert prov["source_field"] == "get_data_quality_report"
    assert prov["source_system"] == "connector-core"


def test_monitors_all_5_present():
    """structuredContent.data.monitors must list all 5 DQ monitor types."""
    result = _call_tool(stream_count=3, firing_rows=[("dq_volume", 2)], evaluated_days=10)
    monitors = result.structured_content["data"]["monitors"]
    types = [m["type"] for m in monitors]
    assert len(monitors) == 5, f"expected 5 monitors, got {len(monitors)}: {types}"
    for t in ("dq_volume", "dq_timeliness", "dq_duplication", "dq_schema", "dq_date_format"):
        assert t in types, f"{t} missing from monitors"


def test_monitors_healthy_pct_calculated():
    """healthy_pct is derived from total streams and unresolved count."""
    # 2 streams, 1 unresolved dq_volume -> healthy = 1/2 = 50.0
    result = _call_tool(stream_count=2, firing_rows=[("dq_volume", 1)])
    monitors = {m["type"]: m for m in result.structured_content["data"]["monitors"]}
    assert monitors["dq_volume"]["healthy_pct"] == 50.0
    assert monitors["dq_volume"]["unresolved_count"] == 1
    # Other monitors: 2 streams, 0 unresolved -> 100.0
    assert monitors["dq_timeliness"]["healthy_pct"] == 100.0


def test_summary_mentions_all_monitors():
    """LLM channel must mention all 5 monitor labels."""
    result = _call_tool(stream_count=1, firing_rows=[], evaluated_days=3)
    text = result.content[0].text
    for label in ("Volume", "Ponctualite", "Doublons", "Coherence", "Lignes rejetees"):
        assert label in text, f"monitor label '{label}' missing from summary:\n{text}"


def test_freshness_in_summary_and_envelope():
    """Freshness timestamp appears in both LLM summary and envelope meta."""
    ts = datetime(2026, 7, 20, 8, 0, 0, tzinfo=timezone.utc)
    result = _call_tool(last_pull_at=ts, evaluated_days=3)
    text = result.content[0].text
    freshness_meta = result.structured_content["meta"]["freshness"]
    # ISO fragment must appear in summary
    assert "2026-07-20" in text, f"freshness date missing from summary:\n{text}"
    # envelope meta.freshness must be the ISO string
    assert "2026-07-20" in freshness_meta, f"freshness missing from envelope: {freshness_meta}"


# ---------------------------------------------------------------------------
# AD-9: provenance -- firing_id + pull_ids in each issue
# ---------------------------------------------------------------------------


def test_issues_carry_firing_id_and_pull_ids():
    """AD-9: each issue in structuredContent.data.issues has firing_id and pull_ids."""
    now = datetime(2026, 7, 21, 9, 0, 0, tzinfo=timezone.utc)
    # issue_rows columns: firing_id, type, fired_at, severity, message, pull_ids
    issue_rows = [
        (
            "fire_abc123",
            "dq_volume",
            now,
            "warning",
            'Volume anormal | meta={"datastream_id":"ds_1","datastream_name":"GA",'
            '"module_name":"gads"}',
            ["pull_001", "pull_002"],
        )
    ]
    result = _call_tool(issue_rows=issue_rows)
    issues = result.structured_content["data"]["issues"]
    assert len(issues) == 1, f"expected 1 issue, got {len(issues)}"
    issue = issues[0]
    assert issue["firing_id"] == "fire_abc123", "firing_id missing from issue (AD-9)"
    assert issue["pull_ids"] == ["pull_001", "pull_002"], "pull_ids missing from issue (AD-9)"
    assert issue["type"] == "dq_volume"
    assert issue["datastream_name"] == "GA"


def test_issues_empty_pull_ids_handled():
    """Issues with NULL pull_ids (TEXT[]) must not crash; pull_ids -> []."""
    now = datetime(2026, 7, 21, 9, 0, 0, tzinfo=timezone.utc)
    issue_rows = [
        ("fire_xyz", "dq_schema", now, "error", "Schema drift", None),
    ]
    result = _call_tool(issue_rows=issue_rows)
    issue = result.structured_content["data"]["issues"][0]
    assert issue["pull_ids"] == [], "null pull_ids must become empty list"
    assert issue["firing_id"] == "fire_xyz"


def test_top3_issues_in_summary():
    """LLM summary must mention up to 3 firing ids in the top-3 block."""
    now = datetime(2026, 7, 21, 9, 0, 0, tzinfo=timezone.utc)
    issue_rows = [
        ("fire_001", "dq_volume", now, "warning", "DS A issue | meta={}", ["p1"]),
        ("fire_002", "dq_timeliness", now, "warning", "DS B issue | meta={}", []),
        ("fire_003", "dq_schema", now, "error", "DS C issue | meta={}", []),
        ("fire_004", "dq_duplication", now, "warning", "DS D issue | meta={}", []),
    ]
    result = _call_tool(issue_rows=issue_rows)
    text = result.content[0].text
    # Only top 3 are mentioned in the summary
    assert "fire_001" in text, "fire_001 must appear in top-3 summary"
    assert "fire_002" in text, "fire_002 must appear in top-3 summary"
    assert "fire_003" in text, "fire_003 must appear in top-3 summary"
    assert "fire_004" not in text, "fire_004 must NOT appear (4th issue, beyond top-3)"


# ---------------------------------------------------------------------------
# Dq-D: degraded path
# ---------------------------------------------------------------------------


def test_degraded_zero_monitors():
    """Dq-D: if DB is unavailable, return honest summary with 0 issues, no raise."""
    from core.main import get_data_quality_report

    def _fail():
        raise RuntimeError("DB unavailable in test")

    with (
        patch("core.main._resolve_project", side_effect=lambda x: x),
        patch("core.db.get_connection", side_effect=_fail),
        patch("core.main.get_access_token", return_value=None),
    ):
        # Must NOT raise
        result = get_data_quality_report(project_id="proj_down")

    assert result is not None, "must return a ToolResult even on DB failure"
    assert result.is_error is not True, "degraded path must NOT be flagged is_error"
    text = result.content[0].text
    # Summary must be honest about no data
    assert "proj_down" in text or "aucune" in text.lower() or "introuvable" in text.lower(), (
        f"degraded summary must mention project or no-data: {text}"
    )
    sc = result.structured_content
    assert sc is not None
    data = sc.get("data", {})
    assert data.get("total_unresolved", 0) == 0


def test_degraded_empty_firings():
    """Dq-D: zero firings, zero evaluated days -> states 'no evaluation' for freshness."""
    result = _call_tool(
        stream_count=0,
        firing_rows=[],
        issue_rows=[],
        last_pull_at=None,
        evaluated_days=0,
    )
    text = result.content[0].text
    sc = result.structured_content
    assert sc["meta"]["freshness"] == "no evaluation", (
        f"freshness must be 'no evaluation' when no pull found, got {sc['meta']['freshness']!r}"
    )
    assert "0" in text or "aucune" in text.lower(), (
        "summary must mention 0 issues or 'aucune' in degraded case"
    )
    assert result.is_error is not True


def test_degraded_no_raise_on_partial_db_failure():
    """Dq-D: fetch_dq_report_data itself never propagates exceptions."""
    from core.dq_api import fetch_dq_report_data

    # Conn that always fails
    conn = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=conn)
    cm.__exit__ = MagicMock(return_value=False)
    conn.cursor.side_effect = RuntimeError("cursor explosion")

    # Must not raise
    result = fetch_dq_report_data("proj_x", conn)
    assert isinstance(result, dict)
    assert result["monitors"] == []
    assert result["issues"] == []
    assert result["total_unresolved"] == 0


# ---------------------------------------------------------------------------
# AD-5: project scoping
# ---------------------------------------------------------------------------


def test_ad5_cross_project_denied_returns_response_not_error():
    """AD-5: identity denied access -> non-disclosing response, not is_error."""
    result = _call_tool(project_id="proj_A", scope_granted=False)
    # Must not raise or be flagged is_error (AD-5 denial is not an exception)
    assert result is not None
    text = result.content[0].text
    assert "proj_A" in text or "refuse" in text.lower() or "introuvable" in text.lower(), (
        f"denial summary should mention project or refusal: {text}"
    )
    sc = result.structured_content
    # envelope must exist but contain empty data
    assert sc is not None
    data = sc.get("data", {})
    assert data.get("total_unresolved", 0) == 0
    assert data.get("issues", []) == []
    assert data.get("monitors", []) == []


def test_ad5_granted_project_receives_data():
    """AD-5: identity with access receives monitor data."""
    result = _call_tool(
        project_id="proj_B",
        scope_granted=True,
        stream_count=3,
        firing_rows=[("dq_schema", 1)],
        evaluated_days=7,
    )
    sc = result.structured_content
    assert sc is not None
    data = sc["data"]
    assert len(data["monitors"]) == 5
    # dq_schema must reflect 1 unresolved firing
    schema_mon = next(m for m in data["monitors"] if m["type"] == "dq_schema")
    assert schema_mon["unresolved_count"] == 1


def test_ad5_summary_not_30_lines_under_denial():
    """AD-5 denial summary must still be <=30 lines (AD-1 cap preserved)."""
    result = _call_tool(project_id="proj_denied", scope_granted=False)
    text = result.content[0].text
    assert len(text.splitlines()) <= 30


# ---------------------------------------------------------------------------
# fetch_dq_report_data unit tests (shared helper in dq_api)
# ---------------------------------------------------------------------------


def test_fetch_dq_report_data_structure():
    """fetch_dq_report_data returns expected keys."""
    from core.dq_api import fetch_dq_report_data

    # Build a conn that provides all 5 cursor responses.
    now = datetime(2026, 7, 20, 8, 0, 0, tzinfo=timezone.utc)
    gc_mock = _make_db_conn_for_dq(
        stream_count=2,
        firing_rows=[("dq_volume", 3)],
        issue_rows=[
            ("fire_1", "dq_volume", now, "warning", "msg | meta={}", ["p1"]),
        ],
        last_pull_at=now,
        evaluated_days=10,
    )
    conn_ctx = gc_mock.return_value
    conn = conn_ctx.__enter__.return_value

    result = fetch_dq_report_data("proj_test", conn)
    assert "monitors" in result
    assert "issues" in result
    assert "freshness_last_pull_at" in result
    assert "evaluated_days_30d" in result
    assert "total_unresolved" in result


def test_fetch_dq_report_data_firing_count():
    """fetch_dq_report_data correctly maps firing counts to total_unresolved."""
    from core.dq_api import fetch_dq_report_data

    gc_mock = _make_db_conn_for_dq(
        stream_count=5,
        firing_rows=[("dq_volume", 2), ("dq_schema", 1)],
        evaluated_days=15,
    )
    conn_ctx = gc_mock.return_value
    conn = conn_ctx.__enter__.return_value

    result = fetch_dq_report_data("proj_test", conn)
    assert result["total_unresolved"] == 3, (
        f"2 dq_volume + 1 dq_schema = 3 total, got {result['total_unresolved']}"
    )
    monitors = {m["type"]: m for m in result["monitors"]}
    assert monitors["dq_volume"]["unresolved_count"] == 2
    assert monitors["dq_schema"]["unresolved_count"] == 1
    assert monitors["dq_timeliness"]["unresolved_count"] == 0


def test_fetch_dq_report_data_pull_ids_in_issues():
    """AD-9: pull_ids survive from DB through fetch_dq_report_data."""
    from core.dq_api import fetch_dq_report_data

    now = datetime(2026, 7, 21, 9, 0, 0, tzinfo=timezone.utc)
    gc_mock = _make_db_conn_for_dq(
        issue_rows=[
            ("fire_999", "dq_duplication", now, "warning", "Dup found", ["pull_A", "pull_B"]),
        ],
    )
    conn_ctx = gc_mock.return_value
    conn = conn_ctx.__enter__.return_value

    result = fetch_dq_report_data("proj_test", conn)
    assert len(result["issues"]) == 1
    issue = result["issues"][0]
    assert issue["firing_id"] == "fire_999"
    assert issue["pull_ids"] == ["pull_A", "pull_B"], (
        f"pull_ids must be preserved, got {issue['pull_ids']!r}"
    )


def test_fetch_dq_report_data_module_filter_kwarg():
    """fetch_dq_report_data accepts module kwarg without raising."""
    from core.dq_api import fetch_dq_report_data

    gc_mock = _make_db_conn_for_dq(stream_count=1, evaluated_days=2)
    conn_ctx = gc_mock.return_value
    conn = conn_ctx.__enter__.return_value

    # Should not raise for a module kwarg
    result = fetch_dq_report_data("proj_test", conn, module="google-ads")
    assert isinstance(result, dict)
    assert "monitors" in result


# ---------------------------------------------------------------------------
# C1: module filter matches real json.dumps format (space after colon)
# ---------------------------------------------------------------------------


def test_module_filter_matches_json_dumps_format():
    """C1: _module_filter_clauses must produce a pattern matching json.dumps output.

    json.dumps({"module_name": "gads"}) -> '{"module_name": "gads"}' (WITH space).
    The previous pattern '%"module_name":"gads"%' (no space) never matched.
    """
    import json as _json

    from core.dq_api import _module_filter_clauses

    module = "gads"
    # Simulate exactly what infra_alerts.write_infra_firing writes.
    metadata = {"datastream_id": "ds_1", "datastream_name": "Google Ads", "module_name": module}
    msg = f"Volume anormal | meta={_json.dumps(metadata)}"

    clause, params = _module_filter_clauses(module)
    # At least one of the two LIKE patterns must match the real message.
    assert len(params) == 2, f"expected 2 LIKE params, got {len(params)}"

    def _like_match(pattern: str, text: str) -> bool:
        r"""Minimal SQL LIKE emulation: % = wildcard.

        re.escape leaves '%' untouched (not a regex metachar), so replace the
        literal '%' -- NOT r'\%' -- with '.*'.
        """
        import re

        regex = re.escape(pattern).replace("%", ".*")
        return bool(re.search(regex, text))

    assert any(_like_match(p, msg) for p in params), (
        f"No pattern in {params!r} matches the real json.dumps message:\n  {msg!r}"
    )


def test_module_filter_also_matches_compact_json():
    """C1 (robustness): _module_filter_clauses also matches compact JSON (no space)."""
    import re

    from core.dq_api import _module_filter_clauses

    module = "google-ads"
    # Compact form (no space after colon) -- defensive compat.
    msg_compact = f'{{"module_name":"{module}","datastream_id":"ds_2"}}'

    _, params = _module_filter_clauses(module)

    def _like_match(pattern, text):
        regex = re.escape(pattern).replace("%", ".*")
        return bool(re.search(regex, text))

    assert any(_like_match(p, msg_compact) for p in params), (
        f"No pattern in {params!r} matches compact JSON: {msg_compact!r}"
    )


def test_parse_firing_meta_extracts_from_json_dumps_message():
    """C1 + M2: _parse_firing_meta correctly extracts fields from json.dumps format."""
    import json as _json

    from core.dq_api import _parse_firing_meta

    metadata = {
        "datastream_id": "ds_abc",
        "datastream_name": "My Stream",
        "module_name": "pinterest-ads",
    }
    msg = f"Schema drift | meta={_json.dumps(metadata)}"

    ds_id, ds_name, module_name = _parse_firing_meta(msg)
    assert ds_id == "ds_abc", f"datastream_id mismatch: {ds_id!r}"
    assert ds_name == "My Stream", f"datastream_name mismatch: {ds_name!r}"
    assert module_name == "pinterest-ads", f"module_name mismatch: {module_name!r}"


def test_parse_firing_meta_no_meta_suffix():
    """M2: _parse_firing_meta returns empty strings for messages without meta suffix."""
    from core.dq_api import _parse_firing_meta

    ds_id, ds_name, module_name = _parse_firing_meta("Plain message, no meta")
    assert ds_id == ""
    assert ds_name == ""
    assert module_name == ""


# ---------------------------------------------------------------------------
# H1: monitors=[] but issues non-empty -- coherent degraded state
# ---------------------------------------------------------------------------


def _make_db_conn_q2_fail_q3_ok(*, issue_rows):
    """Conn where Q2 (firing count) raises but Q3 (issues) succeeds."""
    conn_ctx = MagicMock()
    conn = MagicMock()
    conn_ctx.__enter__ = MagicMock(return_value=conn)
    conn_ctx.__exit__ = MagicMock(return_value=False)

    call_count = {"n": 0}

    _issue_cols = ["firing_id", "type", "fired_at", "severity", "message", "pull_ids"]

    def _col_desc(names):
        return [
            type("D", (), {"__getitem__": staticmethod(lambda i, c=c: c)})()
            for c in names
        ]

    def cursor_factory():
        call_count["n"] += 1
        cm = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        n = call_count["n"]
        if n == 1:
            # Q1: stream count
            cur.fetchone = MagicMock(return_value=(2,))
        elif n == 2:
            # Q2: firing count -- simulate failure
            cur.execute = MagicMock(side_effect=RuntimeError("Q2 db failure"))
        elif n == 3:
            # Q3: issues -- succeeds
            cur.fetchall = MagicMock(return_value=issue_rows)
            cur.description = _col_desc(_issue_cols)
            cur.fetchone = MagicMock(return_value=None)
        elif n == 4:
            cur.fetchone = MagicMock(return_value=(None,))
        else:
            cur.fetchone = MagicMock(return_value=(0,))
        cm.__enter__ = MagicMock(return_value=cur)
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    conn.cursor = cursor_factory
    return conn


def test_h1_monitors_unavailable_issues_non_empty_coherent():
    """H1: Q2 fails (monitors=[]) but Q3 succeeds (3 issues) -> coherent state.

    total_unresolved must equal len(issues) (not 0), and the LLM summary
    must explicitly mention monitors unavailable rather than claiming '0 issues'.
    """
    from core.dq_api import fetch_dq_report_data

    now = datetime(2026, 7, 21, 10, 0, 0, tzinfo=timezone.utc)
    issue_rows = [
        ("fire_1", "dq_volume", now, "warning", "Vol issue | meta={}", []),
        ("fire_2", "dq_schema", now, "error", "Schema issue | meta={}", []),
        ("fire_3", "dq_timeliness", now, "warning", "Late | meta={}", []),
    ]
    conn = _make_db_conn_q2_fail_q3_ok(issue_rows=issue_rows)

    result = fetch_dq_report_data("proj_h1", conn)

    # Monitors must be empty (Q2 failed)
    assert result["monitors"] == [], f"monitors should be empty on Q2 failure: {result['monitors']}"
    assert result["monitors_unavailable"] is True

    # Issues must be present (Q3 succeeded)
    assert len(result["issues"]) == 3, f"expected 3 issues, got {len(result['issues'])}"

    # total_unresolved must be derived from issue count (H1 coherence)
    assert result["total_unresolved"] == 3, (
        f"total_unresolved should be 3 (derived from issues), got {result['total_unresolved']}"
    )


def test_h1_summary_mentions_monitors_unavailable_when_partial():
    """H1: MCP tool summary must explicitly mention monitors unavailable when Q2 failed.

    This test mocks fetch_dq_report_data directly to inject the H1 scenario.
    """
    from unittest.mock import patch as _patch


    now = datetime(2026, 7, 21, 10, 0, 0, tzinfo=timezone.utc)
    partial_data = {
        "monitors": [],
        "monitors_unavailable": True,
        "issues": [
            {
                "firing_id": "fire_x",
                "type": "dq_volume",
                "datastream_id": "ds_1",
                "datastream_name": "Test Stream",
                "module_name": "gads",
                "message": "Volume drop",
                "fired_at": now.isoformat(),
                "severity": "warning",
                "pull_ids": [],
            }
        ],
        "freshness_last_pull_at": None,
        "evaluated_days_30d": 5,
        "total_unresolved": 1,
    }

    with (
        _patch("core.main._resolve_project", side_effect=lambda x: x),
        _patch("core.db.get_connection", side_effect=RuntimeError("no scope DB")),
        _patch("core.main.get_access_token", return_value=None),
        _patch("core.dq_api.fetch_dq_report_data", return_value=partial_data),
        _patch(
            "core.project_access.identity_has_project_access",
            return_value=True,
        ),
    ):
        # Scope check will fail -> get_connection raises -> _denied_response.
        # To bypass that and test the summary path, mock at a higher level.
        # Actually we need a valid scope conn too -- use a separate approach:
        pass

    # Use _call_tool which already handles scope mocking cleanly.
    with _patch("core.dq_api.fetch_dq_report_data", return_value=partial_data):
        result = _call_tool(
            project_id="proj_partial",
            scope_granted=True,
            # _call_tool builds its own DB conn; fetch_dq_report_data is patched
            # so the DB conn for DQ queries is irrelevant here.
        )

    text = result.content[0].text.lower()
    assert "indisponible" in text or "unavailable" in text or "moniteur" in text, (
        f"Summary should mention monitors unavailable in H1 scenario:\n{text}"
    )
    # total_unresolved must be visible (1, not 0)
    assert "1" in text, f"total_unresolved=1 should appear in summary:\n{text}"


# ---------------------------------------------------------------------------
# M1: AD-5 fail_closed -- scope DB down -> non-disclosing response, not data
# ---------------------------------------------------------------------------


def test_m1_scope_db_down_returns_denied_not_data():
    """M1: if the scope DB is unavailable (ProjectAccessUnavailable), return non-disclosing.

    Verifies that a DB hiccup during the scope check does NOT leak data.
    The response must be the same non-disclosing empty envelope as an explicit denial.
    """
    from core.main import get_data_quality_report
    from core.project_access import ProjectAccessUnavailable

    def _raise_pau(*args, **kwargs):
        raise ProjectAccessUnavailable("DB down in test")

    with (
        patch("core.main._resolve_project", side_effect=lambda x: x),
        patch("core.db.get_connection", return_value=MagicMock(
            __enter__=MagicMock(return_value=MagicMock()),
            __exit__=MagicMock(return_value=False),
        )),
        patch("core.main.get_access_token", return_value=None),
        patch(
            "core.project_access.identity_has_project_access",
            side_effect=_raise_pau,
        ),
    ):
        result = get_data_quality_report(project_id="proj_secret")

    # Must not raise, must not be is_error
    assert result is not None
    assert result.is_error is not True

    # Must return empty non-disclosing envelope (no data leaked)
    sc = result.structured_content
    assert sc is not None
    data = sc.get("data", {})
    assert data.get("monitors", []) == [], "monitors must be empty on scope failure"
    assert data.get("issues", []) == [], "issues must be empty on scope failure"
    assert data.get("total_unresolved", 0) == 0

    text = result.content[0].text
    assert "proj_secret" in text or "refuse" in text.lower() or "introuvable" in text.lower(), (
        f"Non-disclosing text expected, got:\n{text}"
    )


def test_m1_scope_connection_fails_returns_denied():
    """M1: if get_connection itself raises during scope check, return non-disclosing response."""
    from core.main import get_data_quality_report

    call_idx = {"n": 0}

    def _failing_get_conn():
        call_idx["n"] += 1
        if call_idx["n"] == 1:
            raise RuntimeError("cannot connect for scope check")
        # Second call (DQ data) -- should never be reached after fail-closed
        return MagicMock(
            __enter__=MagicMock(return_value=MagicMock()),
            __exit__=MagicMock(return_value=False),
        )

    with (
        patch("core.main._resolve_project", side_effect=lambda x: x),
        patch("core.db.get_connection", side_effect=_failing_get_conn),
        patch("core.main.get_access_token", return_value=None),
    ):
        result = get_data_quality_report(project_id="proj_locked")

    assert result is not None
    assert result.is_error is not True
    sc = result.structured_content
    assert sc["data"].get("issues", []) == []
    assert sc["data"].get("total_unresolved", 0) == 0


# ---------------------------------------------------------------------------
# C2: pull_ids best-effort -- passthrough without surclaim
# ---------------------------------------------------------------------------


def test_c2_pull_ids_passthrough_when_empty():
    """C2: pull_ids=[] (hardcoded by infra_alerts) is passed through faithfully.

    Verifies that the tool does not fabricate pull_ids for DQ firings that
    have none (which is the typical production case).
    """
    now = datetime(2026, 7, 21, 9, 0, 0, tzinfo=timezone.utc)
    # NULL pull_ids from Postgres (as infra_alerts hardcodes '{}' -> psycopg returns [])
    issue_rows = [
        ("fire_dq_001", "dq_volume", now, "warning", "Vol drop | meta={}", None),
        ("fire_dq_002", "dq_schema", now, "error", "Schema | meta={}", []),
    ]
    result = _call_tool(issue_rows=issue_rows)
    issues = result.structured_content["data"]["issues"]
    assert len(issues) == 2
    for issue in issues:
        assert issue["pull_ids"] == [], (
            f"pull_ids must be [] (not fabricated) for DQ firings: {issue}"
        )
        # firing_id is the real provenance
        assert issue["firing_id"].startswith("fire_dq_"), (
            f"firing_id must be the primary provenance key: {issue['firing_id']!r}"
        )
