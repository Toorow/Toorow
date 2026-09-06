"""What a rendered report says about geography, and what it refuses to say.

Story 48.2 moved the split from ``project_preferences`` to the published
hierarchy version, so these tests pin the projection loader rather than a
posture argument. The envelope contract they check is stronger than the one
they replace: it now names the version that produced the split and carries the
reconciliation, so a reader can verify the numbers instead of trusting them.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

from core import reports
from core.country_registry import (
    MARKET,
    REGION,
    REST_OF_WORLD,
    build_projection,
    rest_of_world_payload,
)
from core.geographic_semantics import UNKNOWN_BUCKET_ID
from core.master_data import Membership

FR_NODE, DACH_NODE, EMEA_NODE, ROW_NODE = "mdnode_FR", "mdnode_DA", "mdnode_EM", "mdnode_RW"
VERSION = "mdver_RENDER"


class _Module:
    name = "example"
    manifest = {"widget_ref": "ui://core/daily-report"}
    reports = [
        {
            "id": "market",
            "metrics": ["cost"],
            "dimensions": ["country", "device"],
            "layout": {},
            "date_window": {"default_days": 30},
        }
    ]


def _row(dimension: str, value: object, amount: float) -> dict:
    return {
        "date": "2026-07-01",
        "connector": "example",
        "metric": "cost",
        "breakdown_dimension": dimension,
        "breakdown_value": value,
        "value": amount,
        "pull_id": "pull-1",
        "loaded_at": "2026-07-02T00:00:00Z",
    }


def _projection(*, memberships=None):
    return build_projection(
        hierarchy_version_id=VERSION,
        vocabulary_version_id="mdvoc_1",
        registry_id="mdreg_1",
        memberships=memberships
        if memberships is not None
        else [Membership(parent_node_id=FR_NODE, child_value="FR")],
        nodes=[
            {"id": FR_NODE, "label": "France", "node_kind": MARKET},
            {"id": DACH_NODE, "label": "DACH", "node_kind": MARKET},
            {"id": EMEA_NODE, "label": "EMEA", "node_kind": REGION},
            {"id": ROW_NODE, "label": "Rest of World", "node_kind": REST_OF_WORLD},
        ],
        canonical_values=("FR", "MC", "DE", "US"),
        rest_of_world=rest_of_world_payload(node_id=ROW_NODE),
        as_of=date(2026, 7, 30),
    )


def _render(source: list[dict], projection):
    with (
        patch("core.warehouse.query_report", return_value=source),
        patch.object(reports, "_load_geography_projection", return_value=projection),
    ):
        return reports.render_report(
            [_Module()],
            "project-1",
            "example/market",
            "2026-07-01",
            "2026-07-01",
        )


def test_the_envelope_names_the_version_that_produced_the_split() -> None:
    source = [
        _row("country", "FR", 10),
        _row("country", "US", 20),
        _row("country", "invalid", 3),
        _row("device", "mobile", 999),
    ]
    _summary, envelope, _widget = _render(source, _projection())

    geography = envelope["data"]["geography"]
    assert geography["geography_hierarchy_version_id"] == VERSION
    assert geography["vocabulary_version_id"] == "mdvoc_1"
    assert geography["country_partition"] == "country"
    assert geography["excluded_parallel_rows"] == 1
    assert envelope["data"]["metrics"]["cost"] == 30
    assert geography["reconciliation"]["resolved_total"] == 30
    assert geography["reconciliation"]["source_total"] == 33
    assert geography["reconciliation"]["unknown_contribution"] == 3
    alert = envelope["meta"]["alerts"][0]
    assert alert["code"] == "country_value_unmapped"
    assert alert["raw_value"] == "invalid"
    assert alert["source_row"]["value"] == 3


def test_a_project_with_no_published_version_gets_its_rows_back_untouched() -> None:
    """No governed meaning is stated as absence, never as a default grouping."""

    source = [_row("country", "US", 20)]
    _summary, envelope, _widget = _render(source, None)

    assert envelope["data"]["rows"] == source
    assert "geography" not in envelope["data"]
    assert envelope["meta"]["alerts"] == []


def test_the_split_is_governed_markets_and_regions_never_raw_codes() -> None:
    source = [
        _row("country", "FR", 10),
        _row("country", "MC", 5),
        _row("country", "DE", 20),
        _row("country", "US", 30),
        _row("country", "invalid", 3),
    ]
    projection = _projection(
        memberships=[
            Membership(parent_node_id=FR_NODE, child_value="FR"),
            Membership(parent_node_id=FR_NODE, child_value="MC"),
            Membership(parent_node_id=DACH_NODE, child_value="DE"),
            Membership(parent_node_id=EMEA_NODE, child_node_id=FR_NODE),
            Membership(parent_node_id=EMEA_NODE, child_node_id=DACH_NODE),
        ]
    )
    _summary, envelope, _widget = _render(source, projection)

    geography = envelope["data"]["geography"]
    assert geography["markets"] == [
        {"id": DACH_NODE, "label": "DACH"},
        {"id": FR_NODE, "label": "France"},
    ]
    assert geography["bindable_market_ids"] == [DACH_NODE, FR_NODE]
    assert ROW_NODE not in geography["bindable_market_ids"]
    assert UNKNOWN_BUCKET_ID not in geography["bindable_market_ids"]

    grouped = {row["breakdown_value"]: row for row in envelope["data"]["rows"]}
    assert grouped[FR_NODE]["value"] == 15
    assert grouped[FR_NODE]["market_label"] == "France"
    assert grouped[FR_NODE]["region_label"] == "EMEA"
    assert grouped[ROW_NODE]["value"] == 30
    assert UNKNOWN_BUCKET_ID not in grouped
    assert envelope["data"]["metrics"]["cost"] == 65
    proof = geography["reconciliation"]
    assert proof["resolved_total"] == 65
    assert proof["source_total"] == 68
    assert proof["unknown_contribution"] == 3


def test_the_envelope_carries_the_rest_of_world_drill_list_and_the_reconciliation() -> None:
    source = [
        _row("country", "FR", 10),
        _row("country", "US", 30),
        _row("country", "invalid", 3),
    ]
    _summary, envelope, _widget = _render(source, _projection())
    geography = envelope["data"]["geography"]

    assert geography["rest_of_world"]["id"] == ROW_NODE
    assert geography["rest_of_world"]["label"] == "Rest of World"
    assert geography["rest_of_world"]["default_drill"] == "country"
    # The exact countries it stands for -- so the reader can drill without a pull.
    assert geography["rest_of_world"]["country_codes"] == ["DE", "MC", "US"]

    proof = geography["reconciliation"]
    assert proof["additive"] is True
    assert (proof["assigned"], proof["rest_of_world"], proof["unknown"]) == (10, 30, 3)
    assert proof["total"] == 43


def test_narrative_cites_labels_and_never_presents_a_grouping_as_a_market() -> None:
    from core import narrative

    buckets = [
        {
            "market_id": FR_NODE,
            "market_label": "France",
            "geography_bucket_kind": "assigned",
            "value": 15,
        },
        {
            "market_id": ROW_NODE,
            "market_label": "Rest of World",
            "geography_bucket_kind": "rest_of_world",
            "value": 30,
        },
        {"market_id": UNKNOWN_BUCKET_ID, "geography_bucket_kind": "unknown", "value": 3},
    ]

    lines = narrative.build_market_split_lines(buckets=buckets, pull_ids=["pull-1"])

    assert "« France »" in lines[0]
    assert "Marché" in lines[0]
    assert "Marché" not in lines[1]
    assert "Marché" not in lines[2]
    assert ROW_NODE not in lines[1]
    assert UNKNOWN_BUCKET_ID not in lines[2]
    assert narrative.is_country_market_bucket(buckets[0]) is True
    assert narrative.is_country_market_bucket(buckets[1]) is False
    assert narrative.is_country_market_bucket(buckets[2]) is False


def test_a_renamed_rest_of_world_is_cited_by_its_governed_label() -> None:
    from core import narrative

    bucket = {
        "market_id": ROW_NODE,
        "market_label": "Everywhere else",
        "geography_bucket_kind": "rest_of_world",
    }
    assert narrative.market_display_label(bucket) == "Everywhere else"


def test_an_unlabelled_market_never_falls_back_to_a_member_iso_code() -> None:
    from core import narrative

    assert narrative.market_display_label({"market_id": FR_NODE, "market_code": "FR"}) == "?"

class _CapabilityCursor:
    def __init__(self, state):
        self.state = state

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, _sql, _params):
        return None

    def fetchone(self):
        return None if self.state is None else (self.state,)


class _CapabilityConnection:
    def __init__(self, state):
        self.state = state

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return _CapabilityCursor(self.state)


def test_report_projection_is_suppressed_when_country_capability_is_off() -> None:
    with (
        patch("core.db.get_connection", return_value=_CapabilityConnection("off")),
        patch("core.country_registry.load_projection") as load,
    ):
        assert reports._load_geography_projection("project-1") is None

    load.assert_not_called()


def test_report_projection_is_loaded_only_for_an_active_country_capability() -> None:
    marker = object()
    with (
        patch("core.db.get_connection", return_value=_CapabilityConnection("ready")),
        patch("core.country_registry.load_projection", return_value=marker) as load,
    ):
        assert reports._load_geography_projection("project-1") is marker

    load.assert_called_once()
