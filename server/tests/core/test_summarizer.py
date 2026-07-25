"""Tests for core.summarizer — Story 1.5, T3.5.

Covers:
  * 90-day dataset produces ≤30 lines.
  * Empty dataset produces the empty-state message.
  * Line count assertion on realistic fixture.
  * French metric labels are present in the output.
  * Summary never contains raw JSON.
"""

from __future__ import annotations

from core.summarizer import _MAX_LINES, build_daily_report_summary

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_fixture(
    days: int = 90,
    metrics: list[str] | None = None,
    connectors: list[str] | None = None,
    devices: list[str] | None = None,
    countries: list[str] | None = None,
) -> list[dict]:
    """Generate a realistic fact_daily_kpi row fixture."""
    if metrics is None:
        metrics = ["sessions", "active_users", "conversions"]
    if connectors is None:
        connectors = ["my-connector"]
    if devices is None:
        devices = ["desktop", "mobile", "tablet"]
    if countries is None:
        countries = ["France", "Germany", "Spain", "Italy", "UK"]

    from datetime import date, timedelta

    start = date(2026, 1, 1)
    rows = []
    for d in range(days):
        current_date = (start + timedelta(days=d)).isoformat()
        for connector in connectors:
            for metric in metrics:
                for device in devices:
                    for country in countries:
                        rows.append(
                            {
                                "date": current_date,
                                "connector": connector,
                                "metric": metric,
                                "breakdown_dimension": "device_category",
                                "breakdown_value": device,
                                "value": 100.0,
                                "pull_id": "pull_01TEST",
                                "loaded_at": "2026-04-01T00:00:00",
                            }
                        )
    return rows


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_summary_within_30_lines_for_90_day_dataset():
    """90-day dataset must produce ≤30 lines (NFR1 P1 gate, AC2)."""
    rows = _make_fixture(days=90)
    date_range = {"start": "2026-01-01", "end": "2026-03-31"}
    summary = build_daily_report_summary(rows, date_range, ["my-connector"])
    lines = summary.splitlines()
    assert len(lines) <= _MAX_LINES, (
        f"Summary exceeded {_MAX_LINES} lines: {len(lines)}\n---\n{summary}"
    )


def test_empty_dataset_produces_empty_state_message():
    """Empty rows → empty-state summary ≤5 lines (T3.4)."""
    summary = build_daily_report_summary(
        [], {"start": "2026-01-01", "end": "2026-01-31"}, [], project_id="proj_x"
    )
    lines = summary.splitlines()
    assert len(lines) <= 5
    assert "Aucune donnée" in summary
    assert "proj_x" in summary
    assert "2026-01-01" in summary
    assert "2026-01-31" in summary


def test_summary_contains_french_labels():
    """French metric labels must appear in the summary (UX-DR10)."""
    rows = _make_fixture(days=7)
    date_range = {"start": "2026-01-01", "end": "2026-01-07"}
    summary = build_daily_report_summary(rows, date_range, ["my-connector"])
    # French labels for core metrics
    assert "Sessions" in summary
    assert "Utilisateurs actifs" in summary
    assert "Conversions" in summary


def test_summary_does_not_contain_raw_json():
    """Summary must never contain raw JSON rows (AD-1 / NFR1)."""
    rows = _make_fixture(days=30)
    date_range = {"start": "2026-01-01", "end": "2026-01-30"}
    summary = build_daily_report_summary(rows, date_range, ["my-connector"])
    # Check that individual row dict representations are not in the summary
    assert "breakdown_dimension" not in summary
    assert "breakdown_value" not in summary
    # No JSON array markers containing rows
    assert '"date":' not in summary


def test_summary_line_count_realistic_fixture():
    """Realistic 810-row fixture (90d × 3m × 3d) must produce ≤30 lines."""
    rows = _make_fixture(days=90, metrics=["sessions", "active_users", "conversions"],
                         devices=["desktop", "mobile", "tablet"], countries=["France"])
    date_range = {"start": "2026-01-01", "end": "2026-03-31"}
    summary = build_daily_report_summary(rows, date_range, ["my-connector"])
    line_count = len(summary.splitlines())
    assert line_count <= _MAX_LINES, (
        f"Line count {line_count} exceeds {_MAX_LINES}"
    )


def test_summary_truncates_many_connectors():
    """Summary with >5 connectors must still respect the 30-line ceiling."""
    connectors = [f"connector-{i:02d}" for i in range(10)]
    rows = _make_fixture(days=5, connectors=connectors)
    date_range = {"start": "2026-01-01", "end": "2026-01-05"}
    summary = build_daily_report_summary(rows, date_range, connectors)
    lines = summary.splitlines()
    assert len(lines) <= _MAX_LINES, (
        f"Summary with many connectors exceeded {_MAX_LINES} lines: {len(lines)}"
    )


def test_summary_header_contains_date_range():
    """Summary header must mention the date range (readability)."""
    rows = _make_fixture(days=7)
    date_range = {"start": "2026-01-01", "end": "2026-01-07"}
    summary = build_daily_report_summary(rows, date_range, ["my-connector"])
    assert "2026-01-01" in summary
    assert "2026-01-07" in summary


def test_summary_pull_id_present():
    """Summary must include a Pull ID line (provenance, AD-9)."""
    rows = _make_fixture(days=7)
    date_range = {"start": "2026-01-01", "end": "2026-01-07"}
    summary = build_daily_report_summary(rows, date_range, ["my-connector"])
    assert "Pull ID" in summary
    assert "pull_01TEST" in summary


# ---------------------------------------------------------------------------
# Story 4.3 — Context events in summary (AC6 / HG-1)
# ---------------------------------------------------------------------------


def test_summary_with_context_events():
    """Events present -> section appears in output and total <= 30 lines (HG-5)."""
    rows = _make_fixture(days=7)
    date_range = {"start": "2026-01-01", "end": "2026-01-07"}
    events = [
        {"event_date": "2026-01-04", "type": "business", "label": "Lancement campagne ete"},
        {"event_date": "2026-01-01", "type": "incident", "label": "Panne serveur"},
    ]
    summary = build_daily_report_summary(rows, date_range, ["my-connector"], context_events=events)
    lines = summary.splitlines()
    # Line count must still be <=30
    assert len(lines) <= _MAX_LINES, f"Summary exceeded {_MAX_LINES} lines: {len(lines)}"
    # Events section header appears
    assert "Evenements de contexte" in summary
    # Event content appears
    assert "Lancement campagne ete" in summary
    assert "Panne serveur" in summary
    # The "aucun evenement connu" line must NOT appear when events are present
    assert "aucun evenement" not in summary.lower()


def test_summary_context_missing_when_none():
    """context_events=None -> 'aucun evenement connu' line present (HG-1 / AD-9)."""
    rows = _make_fixture(days=7)
    date_range = {"start": "2026-01-01", "end": "2026-01-07"}
    summary = build_daily_report_summary(rows, date_range, ["my-connector"], context_events=None)
    # HG-1: explicit "context missing" line must appear
    assert "aucun evenement connu" in summary.lower()
    # The events section header must NOT appear
    assert "Evenements de contexte" not in summary


def test_summary_context_empty_list():
    """context_events=[] -> same 'aucun evenement connu' line as None case (HG-1)."""
    rows = _make_fixture(days=7)
    date_range = {"start": "2026-01-01", "end": "2026-01-07"}
    summary = build_daily_report_summary(rows, date_range, ["my-connector"], context_events=[])
    # HG-1: must still have the "context missing" line
    assert "aucun evenement connu" in summary.lower()
    # Events header must NOT appear
    assert "Evenements de contexte" not in summary


def test_summary_context_events_capped_at_5():
    """More than 5 events: only 5 are shown, with 'et N autre(s)' note."""
    rows = _make_fixture(days=7)
    date_range = {"start": "2026-01-01", "end": "2026-01-07"}
    events = [
        {"event_date": f"2026-01-0{i}", "type": "business", "label": f"Event {i}"}
        for i in range(1, 9)  # 8 events
    ]
    summary = build_daily_report_summary(rows, date_range, ["my-connector"], context_events=events)
    lines = summary.splitlines()
    # Must still be <=30 lines
    assert len(lines) <= _MAX_LINES
    # Only 5 event lines shown (each starts with "- ")
    event_lines = [line for line in lines if line.startswith("- ") and "Event" in line]
    assert len(event_lines) == 5
    # Truncation note appears
    assert "autre" in summary.lower()


def test_summary_context_missing_in_empty_state():
    """Empty rows + no context events -> empty-state summary has 'aucun evenement connu'."""
    summary = build_daily_report_summary(
        [], {"start": "2026-01-01", "end": "2026-01-31"}, [], project_id="proj_x",
        context_events=None,
    )
    assert "aucun evenement connu" in summary.lower()
    assert "proj_x" in summary


def test_summary_context_events_in_empty_state():
    """Empty rows + context events -> empty-state summary shows event labels."""
    events = [
        {"event_date": "2026-01-10", "type": "deployment", "label": "v2.0.0 deploye"},
    ]
    summary = build_daily_report_summary(
        [], {"start": "2026-01-01", "end": "2026-01-31"}, [], project_id="proj_x",
        context_events=events,
    )
    assert "v2.0.0 deploye" in summary
    assert "aucun evenement connu" not in summary.lower()
