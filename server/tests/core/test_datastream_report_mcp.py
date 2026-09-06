"""`list_datastreams` / `get_datastream_report` -- the pair that replaced 39 tools.

AD-42, `docs/product-architecture/mcp-tool-surface.md`. Each connector used to
publish its own `get_<provider>_report`; the core mounted the lot under provider
namespaces and every host paid for all thirty-nine at session start. They were the
same `fact_daily_kpi` roll-up with a different literal in the WHERE clause.

What is asserted here is what the replacement must not get wrong:

  * a project that collects nothing says so, and names the gesture that fixes it --
    in BOTH tools, with the SAME sentence, because a caller must not have to work
    out that an empty list and a refusal are the same situation;
  * the mart has no Datastream discriminator, so two live Datastreams on one
    connector make the slice unattributable and the tool REFUSES rather than
    showing one stream's numbers under the other's name;
  * a caller can quote either the id or the name it just read.

Offline: the warehouse and the platform DB are stubbed. What is under test is the
resolution and the refusals, not psycopg.
"""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

_ALPHA = {
    "id": "ds_alpha",
    "name": "Paid search daily",
    "connector": "example-ads",
    "data_project_id": "proj_EXAMPLE",
    "enabled": True,
    "schedule_mode": "nightly",
    "report_profile": "campaign_daily",
}


@pytest.fixture
def wired(monkeypatch):
    """Bind the module's four seams to stubs; return a knobs object."""
    from core import datastream_report_mcp as mod
    from core import db as core_db
    from core import main as core_main
    from core import mcp_scope

    knobs = {"streams": [], "ambiguous": False, "rows": []}

    @contextmanager
    def _fake_connection(_identity=None):
        yield object()

    monkeypatch.setattr(core_db, "request_connection", _fake_connection)
    monkeypatch.setattr(
        core_main, "_resolve_project", lambda pid, identity=None: pid or "proj_EXAMPLE"
    )
    monkeypatch.setattr(mcp_scope, "caller_identity", lambda: "owner@example.com")
    monkeypatch.setattr(
        mcp_scope, "refuse_unless_project_scope", lambda *a, **k: None
    )
    monkeypatch.setattr(
        mod, "_project_datastreams", lambda _conn, _pid: list(knobs["streams"])
    )
    monkeypatch.setattr(
        mod, "_is_ambiguous", lambda _conn, _proj, _conn_name: knobs["ambiguous"]
    )

    from core import warehouse

    monkeypatch.setattr(
        warehouse, "query_daily_report", lambda **_kwargs: list(knobs["rows"])
    )
    return knobs


def _refusal(excinfo) -> dict:
    """The canonical `{code, message, next_step}` a ToolError carries."""
    return json.loads(str(excinfo.value))


# ---------------------------------------------------------------------------
# A project that collects nothing.
# ---------------------------------------------------------------------------


def test_an_empty_project_says_why_and_names_the_gesture(wired):
    from core.datastream_report_mcp import list_datastreams

    envelope = list_datastreams("proj_EXAMPLE")

    data = envelope["data"]
    assert data["datastreams"] == []
    assert data["count"] == 0
    assert data["empty_because"], "an empty list must say why it is empty"
    assert "Data > Datastreams" in data["next_step"]


def test_the_empty_list_and_the_refusal_say_the_same_thing(wired):
    """One situation, one sentence. Two wordings would read as two problems."""
    from core.datastream_report_mcp import (
        _NO_DATASTREAM_NEXT_STEP,
        get_datastream_report,
        list_datastreams,
    )

    listed = list_datastreams("proj_EXAMPLE")["data"]["next_step"]

    with pytest.raises(Exception) as excinfo:
        get_datastream_report("anything", project_id="proj_EXAMPLE")

    refusal = _refusal(excinfo)
    assert refusal["code"] == "no_datastream"
    assert refusal["next_step"] == listed == _NO_DATASTREAM_NEXT_STEP


# ---------------------------------------------------------------------------
# Resolution.
# ---------------------------------------------------------------------------


def test_a_datastream_resolves_by_id_and_by_name(wired):
    from core.datastream_report_mcp import get_datastream_report

    wired["streams"] = [_ALPHA]

    by_id = get_datastream_report("ds_alpha", project_id="proj_EXAMPLE")
    by_name = get_datastream_report("Paid search daily", project_id="proj_EXAMPLE")

    assert by_id["data"]["datastream"]["id"] == "ds_alpha"
    assert by_name["data"]["datastream"]["id"] == "ds_alpha"


def test_an_unknown_reference_points_back_at_the_list(wired):
    from core.datastream_report_mcp import get_datastream_report

    wired["streams"] = [_ALPHA]

    with pytest.raises(Exception) as excinfo:
        get_datastream_report("ds_that_does_not_exist", project_id="proj_EXAMPLE")

    refusal = _refusal(excinfo)
    assert refusal["code"] == "datastream_not_found"
    assert "list_datastreams" in refusal["next_step"]


# ---------------------------------------------------------------------------
# The fail-closed rule the mart forces.
# ---------------------------------------------------------------------------


def test_two_datastreams_on_one_connector_refuse_rather_than_mix(wired):
    """`fact_daily_kpi` is keyed (project, date, connector, ...) and nothing else.

    Showing one stream's rows under the other's name is a wrong number nobody can
    see is wrong, so the tool refuses. Same rule the console applies.
    """
    from core.datastream_report_mcp import get_datastream_report

    wired["streams"] = [_ALPHA]
    wired["ambiguous"] = True

    with pytest.raises(Exception) as excinfo:
        get_datastream_report("ds_alpha", project_id="proj_EXAMPLE")

    assert _refusal(excinfo)["code"] == "ambiguous_materialization"


def test_the_list_warns_before_the_call_refuses(wired):
    """A caller learns which streams it can read numbers for, up front."""
    from core.datastream_report_mcp import list_datastreams

    wired["streams"] = [_ALPHA]
    wired["ambiguous"] = True

    listed = list_datastreams("proj_EXAMPLE")["data"]["datastreams"]

    assert listed[0]["report_is_attributable"] is False


def test_a_datastream_without_a_connector_is_told_to_finish_setup(wired):
    from core.datastream_report_mcp import get_datastream_report

    wired["streams"] = [{**_ALPHA, "connector": ""}]

    with pytest.raises(Exception) as excinfo:
        get_datastream_report("ds_alpha", project_id="proj_EXAMPLE")

    assert _refusal(excinfo)["code"] == "datastream_has_no_connector"


# ---------------------------------------------------------------------------
# The answer itself.
# ---------------------------------------------------------------------------


def test_rows_come_back_grouped_by_metric_with_their_provenance(wired):
    from core.datastream_report_mcp import get_datastream_report

    wired["streams"] = [_ALPHA]
    wired["rows"] = [
        {
            "date": "2026-08-01",
            "metric": "clicks",
            "breakdown_dimension": "campaign_id",
            "breakdown_value": "c1",
            "value": 12,
            "pull_id": "pull_2",
            "loaded_at": "2026-08-02T03:00:00Z",
        },
        {
            "date": "2026-08-01",
            "metric": "cost",
            "breakdown_dimension": None,
            "breakdown_value": None,
            "value": 4.5,
            "pull_id": "pull_1",
            "loaded_at": "2026-08-02T02:00:00Z",
        },
    ]

    envelope = get_datastream_report(
        "ds_alpha", project_id="proj_EXAMPLE", date_from="2026-08-01", date_to="2026-08-01"
    )

    assert sorted(envelope["data"]["metrics"]) == ["clicks", "cost"]
    assert envelope["meta"]["provenance"]["pull_id"] == "pull_2"
    assert envelope["meta"]["provenance"]["source_system"] == "example-ads"
    assert envelope["meta"]["alerts"] == []


def test_an_empty_window_is_stated_not_shown_as_zero(wired):
    """No row is not the same fact as a zero, and must not read like one."""
    from core.datastream_report_mcp import get_datastream_report

    wired["streams"] = [_ALPHA]
    wired["rows"] = []

    envelope = get_datastream_report("ds_alpha", project_id="proj_EXAMPLE")

    assert envelope["data"]["metrics"] == {}
    assert envelope["data"]["empty_because"] == "no row landed in this window"
    assert envelope["meta"]["alerts"][0]["level"] == "info"


def test_the_project_scope_guard_runs_before_anything_is_read(monkeypatch, wired):
    """A tool that reads first and checks after is a tool with no check."""
    from core import datastream_report_mcp as mod
    from core import mcp_scope

    calls: list[str] = []

    def _refuse(*_a, **_k):
        calls.append("guard")
        raise RuntimeError("refused")

    monkeypatch.setattr(mcp_scope, "refuse_unless_project_scope", _refuse)
    monkeypatch.setattr(
        mod,
        "_project_datastreams",
        lambda *_a, **_k: calls.append("read") or [],
    )

    with pytest.raises(RuntimeError):
        mod.list_datastreams("proj_EXAMPLE")

    assert calls == ["guard"]
