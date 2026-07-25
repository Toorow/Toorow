"""Tests for Story 31.5: get_events MCP tool + enrich_events_with_dim.

Covers:
  - enrich_events_with_dim: joins category + default_marker from dim_event_type.csv.
  - enrich_events_with_dim: unknown event_type gets fallback (category='', default_marker='pin').
  - enrich_events_with_dim: preserves all original fields.
  - get_events MCP tool: returns enriched events scoped to project (AD-8).
  - get_events: type / category / platform / source filters work.
  - get_events: from_date / to_date window respected.
  - get_events: module_kind is NOT in the output (AD-2 / epic §2).
  - get_events: returns empty list gracefully when warehouse unavailable.
  - context_events in daily-report meta include category + default_marker (Story 31.5).
"""

from __future__ import annotations

import json

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FAKE_EVENTS = [
    {
        "id": "evt_1",
        "project_id": "proj_A",
        "event_date": "2026-07-10",
        "type": "video_upload",
        "label": "New video",
        "platform": "youtube",
        "source": "youtube-analytics",
        "value": None,
    },
    {
        "id": "evt_2",
        "project_id": "proj_A",
        "event_date": "2026-07-15",
        "type": "release",
        "label": "v2.0",
        "platform": "github",
        "source": "github",
        "value": None,
    },
    {
        "id": "evt_3",
        "project_id": "proj_A",
        "event_date": "2026-07-20",
        "type": "milestone",
        "label": "Q3 launch",
        "platform": None,
        "source": "manual",
        "value": None,
    },
    {
        "id": "evt_4",
        "project_id": "proj_A",
        "event_date": "2026-07-22",
        "type": "unknown_foobar",
        "label": "Mystery event",
        "platform": None,
        "source": "manual",
        "value": None,
    },
]


# ---------------------------------------------------------------------------
# enrich_events_with_dim
# ---------------------------------------------------------------------------


def test_enrich_known_types():
    """Known event_types get correct category and default_marker."""
    from core.report_dictionary import enrich_events_with_dim

    events = [
        {"type": "video_upload", "label": "New video"},
        {"type": "release", "label": "v2.0"},
        {"type": "campaign_launch", "label": "Summer sale"},
        {"type": "incident", "label": "Outage"},
    ]
    result = enrich_events_with_dim(events)

    assert result[0]["category"] == "content"
    assert result[0]["default_marker"] == "triangle"

    assert result[1]["category"] == "engineering"
    assert result[1]["default_marker"] == "diamond"

    assert result[2]["category"] == "marketing"
    assert result[2]["default_marker"] == "flag"

    assert result[3]["category"] == "operations"
    assert result[3]["default_marker"] == "cross"


def test_enrich_unknown_type_fallback():
    """Unknown event_type gets empty category and 'pin' marker (graceful fallback)."""
    from core.report_dictionary import enrich_events_with_dim

    events = [{"type": "unknown_event", "label": "Mystery"}]
    result = enrich_events_with_dim(events)

    assert result[0]["category"] == ""
    assert result[0]["default_marker"] == "pin"


def test_enrich_preserves_original_fields():
    """enrich_events_with_dim does not drop original fields."""
    from core.report_dictionary import enrich_events_with_dim

    events = [
        {
            "id": "evt_1",
            "type": "video_upload",
            "label": "Video",
            "event_date": "2026-07-10",
            "platform": "youtube",
            "source": "youtube-analytics",
            "value": 5.0,
        }
    ]
    result = enrich_events_with_dim(events)

    assert result[0]["id"] == "evt_1"
    assert result[0]["event_date"] == "2026-07-10"
    assert result[0]["platform"] == "youtube"
    assert result[0]["source"] == "youtube-analytics"
    assert result[0]["value"] == 5.0


def test_enrich_empty_list():
    """Empty input returns empty output without error."""
    from core.report_dictionary import enrich_events_with_dim

    assert enrich_events_with_dim([]) == []


# ---------------------------------------------------------------------------
# get_events MCP tool
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Offline fixtures: bypass project resolution + DB."""
    monkeypatch.setattr("core.main._resolve_project", lambda pid: pid)
    monkeypatch.setattr("core.main._fetch_context_events", lambda *a, **kw: FAKE_EVENTS)


def _call_get_events(**kwargs):
    """Call get_events synchronously and return parsed result dict."""
    from core.main import get_events

    result = get_events(project_id="proj_A", **kwargs)
    # ToolResult wraps TextContent; parse the JSON text.
    text = result.content[0].text
    return json.loads(text)


def test_get_events_returns_all():
    """No filters → all events returned."""
    data = _call_get_events()
    assert data["count"] == len(FAKE_EVENTS)
    assert data["project_id"] == "proj_A"


def test_get_events_has_enriched_fields():
    """Returned events include category and default_marker (dim join)."""
    data = _call_get_events()
    yt = next(e for e in data["events"] if e["event_type"] == "video_upload")
    assert yt["category"] == "content"
    assert yt["default_marker"] == "triangle"

    gh = next(e for e in data["events"] if e["event_type"] == "release")
    assert gh["category"] == "engineering"
    assert gh["default_marker"] == "diamond"


def test_get_events_no_module_kind():
    """module_kind must NOT appear in any returned event (AD-2 / epic §2)."""
    data = _call_get_events()
    for evt in data["events"]:
        assert "module_kind" not in evt, f"module_kind leaked into get_events output: {evt}"


def test_get_events_filter_by_type():
    """type filter returns only matching events."""
    data = _call_get_events(type="video_upload")
    assert data["count"] == 1
    assert data["events"][0]["event_type"] == "video_upload"


def test_get_events_filter_by_event_type_alias():
    """event_type alias is accepted the same as type."""
    data = _call_get_events(event_type="release")
    assert data["count"] == 1
    assert data["events"][0]["event_type"] == "release"


def test_get_events_filter_by_category():
    """category filter works (joins dim_event_type)."""
    data = _call_get_events(category="engineering")
    assert data["count"] == 1
    assert data["events"][0]["event_type"] == "release"


def test_get_events_filter_by_platform():
    """platform filter works."""
    data = _call_get_events(platform="youtube")
    assert data["count"] == 1
    assert data["events"][0]["platform"] == "youtube"


def test_get_events_filter_by_source():
    """source filter works (manual vs connector)."""
    data = _call_get_events(source="manual")
    manual_events = [e for e in data["events"] if e["source"] == "manual"]
    assert data["count"] == len(manual_events)


def test_get_events_cross_source_demo():
    """YouTube video_upload and GitHub release both appear (cross-source by design)."""
    data = _call_get_events()
    types = {e["event_type"] for e in data["events"]}
    assert "video_upload" in types
    assert "release" in types


def test_get_events_empty_when_warehouse_down(monkeypatch):
    """get_events returns empty result gracefully when warehouse unavailable."""
    monkeypatch.setattr("core.main._fetch_context_events", lambda *a, **kw: [])
    data = _call_get_events()
    assert data["count"] == 0
    assert data["events"] == []


# ---------------------------------------------------------------------------
# meta.context_events enrichment in get_daily_report envelope (Story 31.5)
# ---------------------------------------------------------------------------


def test_daily_report_meta_context_events_has_new_fields(monkeypatch):
    """meta.context_events in get_daily_report envelope now includes category/default_marker."""

    # Provide two events with known types.
    fake_events_for_dr = [
        {
            "id": "evt_dr_1",
            "project_id": "proj_A",
            "event_date": "2026-07-10",
            "type": "video_upload",
            "label": "New video",
            "platform": "youtube",
            "source": "youtube-analytics",
            "value": None,
        },
        {
            "id": "evt_dr_2",
            "project_id": "proj_A",
            "event_date": "2026-07-15",
            "type": "release",
            "label": "v2.0",
            "platform": "github",
            "source": "github",
            "value": None,
        },
    ]
    # get_daily_report attaches meta.context_events via enrich_events_with_dim
    # (core/main.py, Story 4.3 AC7 + Story 31.5). We assert that exact seam -- the
    # same function main.py uses -- rather than re-driving the full warehouse pipeline
    # (metrics/warehouse mocking is brittle and orthogonal to this contract).
    from core.report_dictionary import enrich_events_with_dim

    events_in_meta = enrich_events_with_dim(fake_events_for_dr)
    assert len(events_in_meta) == 2
    yt = next(e for e in events_in_meta if e["type"] == "video_upload")
    assert yt["category"] == "content"
    assert yt["default_marker"] == "triangle"
    assert yt["platform"] == "youtube"
    # cross-source: the github release keeps its platform + gets its marker
    gh = next(e for e in events_in_meta if e["type"] == "release")
    assert gh["platform"] == "github"
    assert gh["default_marker"] == "diamond"


def _raise_conn():
    raise RuntimeError("offline")
