"""The MCP report path reads the SAME governed geography the console reads.

Layer contract, `specs/spec-toorow/geographic-reporting.md` §Layer contract, row
"Tools and MCP". This layer had no test at all until 2026-08-22 -- and it is the
layer that failed silently: until Story 37.9, `report_mcp` read
`project_preferences.geographic_mode` / `local_markets`, a preference the Country
capability never writes back, and then derived its COVERAGE from that empty read.
A Project with a fully published Country hierarchy was described to an agent as
consolidated, in the same envelope the console was splitting by market.

So the assertions here are behavioural rather than textual: the loader is called
with a fake connection and the two failure shapes are exercised. A grep for
`governed_posture` would have passed on the day the bug shipped.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from core import report_mcp
from core.geographic_reporting import GeographicPosture, Market


@contextmanager
def _connection(_identity=None):
    yield object()


@pytest.fixture()
def fake_db(monkeypatch):
    """The loader acquires ARMED (67-1, 2026-08-24), so that is what is doubled.

    Patching `get_connection` here would have kept these tests green after the
    site moved to `request_connection` -- and green for the wrong reason: the
    real acquisition would have run against no database at all. The double
    takes the identity argument for the same reason the site passes one.
    """
    from core import db as core_db

    monkeypatch.setattr(core_db, "request_connection", _connection)
    return core_db


def _governed(markets: tuple[str, ...]) -> GeographicPosture:
    return GeographicPosture(
        mode="local_markets",
        markets=(Market(id="mk_fr", label="France", country_codes=markets),),
    )


def test_the_loader_reads_the_governed_posture_not_the_preference(fake_db, monkeypatch):
    """One authority. The retired reader must not be reachable from this path."""

    from core import country_activation, geographic_reporting

    monkeypatch.setattr(
        country_activation, "governed_posture", lambda conn, *, project_id: _governed(("FR",))
    )
    monkeypatch.setattr(
        geographic_reporting,
        "fetch_project_geographic_coverage",
        lambda project_id, posture, conn: {"status": "complete", "datastreams": ["ds_1"]},
    )

    def _refuse(*args, **kwargs):  # pragma: no cover - only runs on regression
        raise AssertionError(
            "report_mcp read `fetch_project_geographic_posture` again. That preference "
            "is empty for every Project governed through the Country capability, so the "
            "read returns Global and the agent is told a governed Project is consolidated."
        )

    monkeypatch.setattr(geographic_reporting, "fetch_project_geographic_posture", _refuse)

    context = report_mcp._load_project_geographic_posture("proj_EXAMPLE", "owner@example.com")

    assert context.posture.mode == "local_markets"
    assert [market.label for market in context.posture.markets] == ["France"]
    assert context.coverage["status"] == "complete"


def test_an_unreadable_coverage_is_declared_unavailable_never_consolidated(
    fake_db, monkeypatch
):
    """The honest degradation: a governed Project whose coverage cannot be read.

    `consolidated` here would be a lie with a market split beside it -- the split
    comes from the posture, which was read; only the coverage failed.
    """

    from core import country_activation, geographic_reporting

    monkeypatch.setattr(
        country_activation, "governed_posture", lambda conn, *, project_id: _governed(("FR",))
    )

    def _explode(project_id, posture, conn):
        raise RuntimeError("coverage relation unavailable")

    monkeypatch.setattr(
        geographic_reporting, "fetch_project_geographic_coverage", _explode
    )

    context = report_mcp._load_project_geographic_posture("proj_EXAMPLE", "owner@example.com")

    assert context.posture.mode == "local_markets"
    assert context.coverage == {"status": "unavailable", "datastreams": []}


def test_a_project_with_country_off_is_consolidated_and_says_so(fake_db, monkeypatch):
    """Global is an ANSWER here (deactivation returns reports to consolidated)."""

    from core import country_activation, geographic_reporting

    monkeypatch.setattr(
        country_activation, "governed_posture", lambda conn, *, project_id: GeographicPosture()
    )

    def _explode(project_id, posture, conn):
        raise RuntimeError("no coverage for a consolidated project")

    monkeypatch.setattr(
        geographic_reporting, "fetch_project_geographic_coverage", _explode
    )

    context = report_mcp._load_project_geographic_posture("proj_EXAMPLE", "owner@example.com")

    assert context.posture.mode == "global"
    assert context.posture.markets == ()
    assert context.coverage == {"status": "consolidated", "datastreams": []}


def test_a_dead_registry_does_not_take_the_report_down(fake_db, monkeypatch):
    """A posture read failure degrades; it never raises into `get_report`."""

    from core import country_activation

    def _explode(conn, *, project_id):
        raise RuntimeError("registry unreachable")

    monkeypatch.setattr(country_activation, "governed_posture", _explode)

    context = report_mcp._load_project_geographic_posture("proj_EXAMPLE", "owner@example.com")

    assert context.posture == GeographicPosture()
    assert context.coverage["status"] == "consolidated"
