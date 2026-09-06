"""A Connector preview fetches without landing -- and no module has to know it.

`496d59a` left `connector_pull` previews unmounted, reasoning that "a preview would
publish numbers before any confirmation": every Connector lands into one raw table
shared by all its pulls, and all staging models supersede on `pull_id DESC` with no
notion of an execution, so rows reach the marts the moment they land.

That is true of a CANDIDATE. A candidate must land, and it must be isolated by
execution -- the open warehouse problem, unchanged. It is NOT true of a preview.

WHAT CHANGED, AND WHY THESE TESTS MOVED WITH IT. The guarantee used to rest on a
`dry_run` parameter each module had to accept. Measured 2026-08-11: THREE modules
of thirty-nine had it, so thirty-six Connectors could not be previewed -- and a
Datastream cannot be activated without a preview. The guarantee now rests on
`core.raw_landing.preview_capture`, a scope that diverts every raw write, so a
module that never heard of previews is previewed anyway and still lands nothing.

The invariant is unchanged and now stronger. These tests pin it where it is
actually enforced:
  * a preview returns rows and lands nothing;
  * a Connector that never accepted `dry_run` is previewed, not refused;
  * a Connector that IGNORES the flag and tries to land is stopped by the scope --
    nothing reaches the warehouse, which is better than refusing its output after
    the fact.
"""

from __future__ import annotations

from unittest.mock import patch

from inbound.adapters import datastream_activation_drivers as drivers

CONTEXT = {
    "module": "gsc",
    "project_id": "proj_EXAMPLE",
    "connection_ref_id": "conn_EXAMPLE",
    "interval": {"from": "2026-07-01", "to_exclusive": "2026-07-02"},
}


def test_preview_returns_rows_and_lands_nothing():
    landed: list[object] = []

    def fake_pull(**kwargs):
        # `gsc` DOES accept the flag, so the driver still passes it: the two
        # mechanisms are not in conflict and the capture is the guarantee.
        assert kwargs["dry_run"] is True, "a module that accepts the flag still gets it"
        return {
            "pull_id": None,
            "dry_run": True,
            "row_count": 2,
            "rows": [
                {"date": "2026-07-01", "clicks": 10, "impressions": 100},
                {"date": "2026-07-01", "clicks": 4, "impressions": 40},
            ],
            "schema": ["clicks", "date", "impressions"],
        }

    with (
        patch("core.main.get_module_pull_fn", return_value=fake_pull),
        patch.object(drivers, "_remote_activation", lambda *a, **k: landed.append(a)),
    ):
        evidence = drivers.connector_pull_preview(dict(CONTEXT))

    assert evidence["adapter_verified"] is True
    assert evidence["placeholder"] is False
    assert evidence["adapter_ref"] == "connector_pull.gsc.preview.v1"
    assert len(evidence["rows"]) == 2
    assert [field["field_id"] for field in evidence["schema"]] == ["clicks", "date", "impressions"]
    assert evidence["coverage"] == {"schema": "available", "values": "available"}
    # The remote service is not involved at all: that is the point.
    assert landed == []


def test_an_empty_window_is_evidence_not_an_error():
    def fake_pull(**_kwargs):
        return {"dry_run": True, "row_count": 0, "rows": [], "schema": []}

    with patch("core.main.get_module_pull_fn", return_value=fake_pull):
        evidence = drivers.connector_pull_preview(dict(CONTEXT))

    assert evidence["coverage"] == {"schema": "unavailable", "values": "empty_window"}
    assert evidence["row_count_bucket"] == "0"


def test_a_connector_that_never_accepted_dry_run_is_previewed_not_refused():
    """Thirty-six of thirty-nine modules are in this case. Refusing them refused
    their activation, because there is no activation without a preview."""
    from core.raw_landing import land_raw_rows

    # An EXPLICIT signature with no `dry_run`, which is what thirty-six modules
    # really look like -- a `**kwargs` fake would accept the flag and prove
    # nothing about them.
    def legacy_pull(connection_id, date_from, date_to, project_id, pull_id=""):
        land_raw_rows(
            "raw_probe",
            [{"date": "2026-07-01", "clicks": 10}, {"date": "2026-07-01", "clicks": 4}],
            columns=[("date", "DATE"), ("clicks", "INTEGER")],
            project_id="proj_EXAMPLE",
        )
        return {"pull_id": "pull_1", "row_count": 2}

    with patch("core.main.get_module_pull_fn", return_value=legacy_pull):
        evidence = drivers.connector_pull_preview(dict(CONTEXT))

    assert evidence["adapter_verified"] is True
    assert len(evidence["rows"]) == 2, "the rows come back through the capture"
    assert [field["field_id"] for field in evidence["schema"]] == ["clicks", "date"]


def test_a_connector_that_ignores_dry_run_lands_nothing_anyway():
    """It cannot land: the scope diverts the write, whatever the module intends."""
    from core.raw_landing import land_raw_rows

    def landing_pull(**_kwargs):
        outcome = land_raw_rows(
            "raw_probe",
            [{"date": "2026-07-01", "clicks": 7}],
            columns=[("date", "DATE"), ("clicks", "INTEGER")],
            project_id="proj_EXAMPLE",
        )
        # The module believes it landed; the backend says where it really went.
        assert outcome["backend"] == "preview", "the write must not reach a warehouse"
        return {"pull_id": "pull_1", "row_count": 1}      # no dry_run marker

    with patch("core.main.get_module_pull_fn", return_value=landing_pull):
        evidence = drivers.connector_pull_preview(dict(CONTEXT))

    assert evidence["rows"] == [{"date": "2026-07-01", "clicks": 7}]


def test_the_gsc_connector_accepts_the_non_landing_fetch():
    """The contract change is real in a real Connector, not only in the driver."""
    import inspect

    from modules.gsc import connector

    parameters = inspect.signature(connector.pull).parameters
    assert "dry_run" in parameters
    assert parameters["dry_run"].default is False, "landing stays the default"
