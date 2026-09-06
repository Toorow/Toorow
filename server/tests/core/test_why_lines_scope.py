"""The third proactive path: the "Why" of a daily report attaches SCOPED context.

`docs/product-architecture/proactive-assertions.md` is incomplete if *"a context
event is attached to a claim without being scoped to that claim's metric,
connector and date"*. Two of the three surfaces were repaired by story 53.8:
`briefing.context_event_walk` and
`anomaly_alerts._fetch_context_events_for_anomaly` both join on the claim's own
date and disqualify an event that declares another platform.

The third was not, and it is the one the morning briefing is READ on
(`get_daily_report`). `reporting_mcp` fetched every annotation of the reporting
window and handed the whole list to `narrative._why_lines`, which rendered them
as the "Why" of the claims above — an attachment on no basis but co-occurrence
in a date range. Treating two instances of a class and leaving the third is how
this repository keeps re-finding the same defect.

Migration 322 supplies the missing column; these tests hold the wire.
"""

from __future__ import annotations

import os
from unittest.mock import patch

from core import briefing, narrative

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

_WINDOW = {"start": "2026-07-01", "end": "2026-07-11"}


def _rows(metric="sessions", connector="google-analytics"):
    return [
        {
            "date": "2026-07-05",
            "connector": connector,
            "metric": metric,
            "breakdown_dimension": "device",
            "breakdown_value": "desktop",
            "value": 100.0,
            "pull_id": "pull_1",
            "loaded_at": "2026-07-05T00:00:00",
        },
        {
            "date": "2026-07-06",
            "connector": connector,
            "metric": metric,
            "breakdown_dimension": "device",
            "breakdown_value": "desktop",
            "value": 120.0,
            "pull_id": "pull_1",
            "loaded_at": "2026-07-06T00:00:00",
        },
    ]


_EVENT_ABOUT_COST = {
    "id": "evt_cost",
    "event_date": "2026-07-05",
    "type": "business",
    "label": "Bid raised on the main campaign",
    "metric": "cost",
}
_EVENT_ABOUT_EVERYTHING = {
    "id": "evt_outage",
    "event_date": "2026-07-05",
    "type": "incident",
    "label": "Site outage",
}


# ---------------------------------------------------------------------------
# `_why_lines` — the attach point itself
# ---------------------------------------------------------------------------


def test_why_lines_says_what_the_attachment_was_checked_on():
    scope = {
        "basis": [briefing.CONTEXT_BASIS_CLAIM_WINDOW, briefing.CONTEXT_BASIS_METRIC],
        "unscoped_dimensions": [briefing.CONTEXT_DIM_CONNECTOR],
        "claim_window": _WINDOW,
    }
    lines = narrative._why_lines([_EVENT_ABOUT_COST], [], None, scope)
    disclosure = lines[-1]
    assert briefing.CONTEXT_BASIS_METRIC in disclosure
    assert briefing.CONTEXT_DIM_CONNECTOR in disclosure


def test_the_disclosure_prints_no_empty_half_when_everything_was_compared():
    scope = {
        "basis": [briefing.CONTEXT_BASIS_CLAIM_WINDOW],
        "unscoped_dimensions": [],
        "claim_window": _WINDOW,
    }
    line = narrative.context_scope_line(scope)
    assert briefing.CONTEXT_BASIS_CLAIM_WINDOW in line
    assert "Non comparé" not in line


def test_a_caller_that_scoped_nothing_gets_the_untouched_section():
    """`core.reports` and `core.cards` answer a question a person asked.

    They are not proactive surfaces and this document does not bind them, so the
    absence of a descriptor leaves Story 6.4's section exactly as it was. The
    guarantee this file makes is about the path that VOLUNTEERS a claim, and the
    test below holds that this path always passes one.
    """
    lines = narrative._why_lines([_EVENT_ABOUT_COST], [], None)
    assert not any("Rattachement du contexte" in line for line in lines)


def test_build_narrative_carries_the_scope_through_to_the_section():
    text = narrative.build_narrative(
        project_id="proj_EXAMPLE",
        report_id=None,
        rollup={},
        context_events=[_EVENT_ABOUT_COST],
        alerts=[],
        as_of=None,
        narrative_prompt=None,
        context_scope={
            "basis": [briefing.CONTEXT_BASIS_CLAIM_WINDOW],
            "unscoped_dimensions": [briefing.CONTEXT_DIM_METRIC],
            "claim_window": _WINDOW,
        },
    )
    assert briefing.CONTEXT_DIM_METRIC in text


# ---------------------------------------------------------------------------
# `get_daily_report` — the feeder, end to end
# ---------------------------------------------------------------------------


def _daily_report(context_events):
    from core.main import get_daily_report

    with (
        patch("core.main._resolve_project", return_value="default"),
        patch("core.db.get_connection", side_effect=RuntimeError("offline")),
        patch("core.warehouse.query_daily_report", return_value=_rows()),
        patch("core.main.warehouse.get_daily_report_asof", return_value=[]),
        patch("core.main._fetch_context_events", return_value=context_events),
    ):
        return get_daily_report(project_id="default", date_range=_WINDOW)


def test_the_daily_report_does_not_narrate_an_event_about_another_metric():
    """The report claims `sessions`; the annotation says it is about `cost`."""
    result = _daily_report([_EVENT_ABOUT_COST])
    summary = "\n".join(
        block.text for block in result.content if getattr(block, "text", None)
    )
    # The same channel the test below finds an admissible event in -- so this is
    # an absence from a section that exists, never an absent section.
    assert summary.strip(), "the narrative channel must carry something to assert on"
    assert "Bid raised on the main campaign" not in summary, (
        "an event about `cost` must not be the Why of a `sessions` claim"
    )


def test_the_daily_report_keeps_an_event_about_every_metric():
    """A null metric is admissible everywhere -- that is the point of nullable."""
    result = _daily_report([_EVENT_ABOUT_EVERYTHING])
    summary = "\n".join(
        block.text for block in result.content if getattr(block, "text", None)
    )
    assert "Site outage" in summary


def test_the_overlay_still_carries_every_annotation_of_the_window():
    """`meta.context_events` is the OVERLAY, not an attachment to a claim.

    Narrowing it would be the opposite defect: a marker on a chart says "this
    happened that day", asserts no relationship, and is exactly what a reader
    needs in order to disagree with the attachment the Why section made.
    """
    result = _daily_report([_EVENT_ABOUT_COST, _EVENT_ABOUT_EVERYTHING])
    overlay = (result.structured_content or {}).get("meta", {}).get("context_events")
    assert overlay is not None
    assert {row["id"] for row in overlay} == {"evt_cost", "evt_outage"}


def test_the_feeder_scopes_before_it_narrates():
    """The wire, pinned at the source: no unscoped list reaches `build_narrative`.

    A behavioural test alone would pass again the day someone reintroduces the
    unscoped call for the empty-rows branch, so the call site itself is held.
    """
    import inspect

    from core import reporting_mcp

    source = inspect.getsource(reporting_mcp.get_daily_report)
    assert "context_events_in_claim_scope" in source
    assert "context_events=_scoped_events" in source
    assert "context_scope=_context_scope" in source


# ---------------------------------------------------------------------------
# AI-344 (2026-09-01) -- "not read" is not "none". The Why section under a
# claim says the context is UNKNOWN when the events could not be read from any
# store, and keeps the verbatim AD-9 absence line for a window that was read.
# ---------------------------------------------------------------------------


def test_the_why_section_says_unknown_not_missing_when_the_events_were_not_read():
    from core import narrative
    from core.narrative_phrases import phrase

    unavailable = {"reason": "not read", "repair": "retry"}
    said = narrative.build_narrative(
        project_id="proj_EXAMPLE",
        report_id=None,
        rollup={},
        context_events=[],
        alerts=[],
        as_of=None,
        narrative_prompt=None,
        context_unavailable=unavailable,
    )
    assert phrase("context_unavailable") in said
    assert phrase("context_missing") not in said

    read_and_empty = narrative.build_narrative(
        project_id="proj_EXAMPLE",
        report_id=None,
        rollup={},
        context_events=[],
        alerts=[],
        as_of=None,
        narrative_prompt=None,
    )
    assert phrase("context_missing") in read_and_empty
    assert phrase("context_unavailable") not in read_and_empty
