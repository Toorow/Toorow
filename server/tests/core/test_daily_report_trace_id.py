"""Tests for meta.trace_id in get_daily_report (Story 5.5, AC2, AC8).

Covers:
  - test_trace_id_in_meta_when_tracing_enabled: mock current_trace_id_hex returns
    "abc123" → meta.trace_id == "abc123".
  - test_trace_id_null_when_tracing_disabled: mock returns None → meta.trace_id is None.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

_VALID_DATE_RANGE = {"start": "2026-07-01", "end": "2026-07-11"}


# ---------------------------------------------------------------------------
# test_trace_id_in_meta_when_tracing_enabled
# ---------------------------------------------------------------------------


def test_trace_id_in_meta_when_tracing_enabled():
    """mock current_trace_id_hex()='abc123' → meta.trace_id == 'abc123' in envelope."""
    from core.main import get_daily_report

    with (
        patch("core.main._resolve_project", return_value="default"),
        patch("core.db.get_connection", side_effect=RuntimeError("offline")),
        patch("core.main.warehouse.query_daily_report", return_value=[]),
        patch("core.main.warehouse.get_daily_report_asof", return_value=[]),
        patch("core.main.tracing.current_trace_id_hex", return_value="abc123"),
        patch("core.main._fetch_context_events", return_value=[]),
    ):
        result = get_daily_report(project_id="default", date_range=_VALID_DATE_RANGE)

    # get_daily_report may fail (no DB) but must still return a result with an envelope.
    # When warehouse is unavailable it returns an error result — check if we got a valid result.
    if result.is_error:
        pytest.skip("Warehouse unavailable — cannot verify meta.trace_id in full result")

    sc = result.structured_content
    assert sc is not None, "structured_content must be present"
    meta = sc.get("meta", {})
    assert "trace_id" in meta, f"trace_id not in meta keys: {list(meta.keys())}"
    assert meta["trace_id"] == "abc123"


# ---------------------------------------------------------------------------
# test_trace_id_null_when_tracing_disabled
# ---------------------------------------------------------------------------


def test_trace_id_null_when_tracing_disabled():
    """mock current_trace_id_hex()=None → meta.trace_id is None in envelope."""
    from core.main import get_daily_report

    with (
        patch("core.main._resolve_project", return_value="default"),
        patch("core.db.get_connection", side_effect=RuntimeError("offline")),
        patch("core.main.warehouse.query_daily_report", return_value=[]),
        patch("core.main.warehouse.get_daily_report_asof", return_value=[]),
        patch("core.main.tracing.current_trace_id_hex", return_value=None),
        patch("core.main._fetch_context_events", return_value=[]),
    ):
        result = get_daily_report(project_id="default", date_range=_VALID_DATE_RANGE)

    if result.is_error:
        pytest.skip("Warehouse unavailable — cannot verify meta.trace_id in full result")

    sc = result.structured_content
    assert sc is not None
    meta = sc.get("meta", {})
    assert "trace_id" in meta, f"trace_id not in meta keys: {list(meta.keys())}"
    assert meta["trace_id"] is None


# ---------------------------------------------------------------------------
# test_trace_id_key_always_present_in_meta
# ---------------------------------------------------------------------------


def test_trace_id_key_always_present_in_meta():
    """trace_id key is always set in meta (null or string) — never absent.

    This test patches both the warehouse query to succeed (returning empty rows)
    and current_trace_id_hex to return a known value, verifying the key is always
    written regardless of other meta enrichment.
    """
    from core.main import get_daily_report

    with (
        patch("core.main._resolve_project", return_value="default"),
        patch("core.db.get_connection", side_effect=RuntimeError("offline")),
        patch("core.main.warehouse.query_daily_report", return_value=[]),
        patch("core.main.warehouse.get_daily_report_asof", return_value=[]),
        patch("core.main.tracing.current_trace_id_hex", return_value="deadbeef" * 4),
        patch("core.main._fetch_context_events", return_value=[]),
    ):
        result = get_daily_report(project_id="default", date_range=_VALID_DATE_RANGE)

    assert not result.is_error, f"Expected no error but got: {result}"
    sc = result.structured_content
    assert sc is not None
    meta = sc.get("meta", {})
    assert "trace_id" in meta
    assert meta["trace_id"] == "deadbeef" * 4


def test_trace_id_null_when_no_span():
    """With warehouse mocked to empty rows + trace_id=None, meta.trace_id is null."""
    from core.main import get_daily_report

    with (
        patch("core.main._resolve_project", return_value="default"),
        patch("core.db.get_connection", side_effect=RuntimeError("offline")),
        patch("core.main.warehouse.query_daily_report", return_value=[]),
        patch("core.main.warehouse.get_daily_report_asof", return_value=[]),
        patch("core.main.tracing.current_trace_id_hex", return_value=None),
        patch("core.main._fetch_context_events", return_value=[]),
    ):
        result = get_daily_report(project_id="default", date_range=_VALID_DATE_RANGE)

    assert not result.is_error
    sc = result.structured_content
    meta = sc.get("meta", {})
    assert "trace_id" in meta
    assert meta["trace_id"] is None
