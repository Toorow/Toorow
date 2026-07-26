from __future__ import annotations

from unittest.mock import patch

from core import reports
from core.geographic_reporting import (
    LOCAL_MARKETS,
    GeographicPosture,
    GeographicReportContext,
)
from core.geographic_semantics import OTHER_MARKETS, UNKNOWN_MARKET


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


def test_local_report_envelope_exposes_accessible_split_and_dq_evidence() -> None:
    source = [
        _row("country", "FR", 10),
        _row("country", "US", 20),
        _row("country", "invalid", 3),
        _row("device", "mobile", 999),
    ]
    posture = GeographicReportContext(
        posture=GeographicPosture(mode=LOCAL_MARKETS, country_codes=("FR",)),
        coverage={"status": "partial", "datastreams": []},
    )

    with patch("core.warehouse.query_report", return_value=source):
        _summary, envelope, _widget = reports.render_report(
            [_Module()],
            "project-1",
            "example/market",
            "2026-07-01",
            "2026-07-01",
            geographic_posture=posture,
        )

    geography = envelope["data"]["geography"]
    assert geography["mode"] == LOCAL_MARKETS
    assert geography["country_partition"] == "country"
    assert geography["excluded_parallel_rows"] == 1
    assert geography["coverage"]["status"] == "partial"
    assert [bucket["kind"] for bucket in geography["buckets"]] == [
        "tracked",
        "other",
        "unknown",
    ]
    grouped = {row["breakdown_value"]: row for row in envelope["data"]["rows"]}
    assert grouped["FR"]["market_code"] == "FR"
    assert grouped[OTHER_MARKETS]["market_code"] is None
    assert grouped[UNKNOWN_MARKET]["market_kind"] == "unknown"
    assert envelope["data"]["metrics"]["cost"] == 33
    assert envelope["meta"]["alerts"][0]["code"] == "country_value_unmapped"


def test_global_report_is_byte_shape_backward_compatible() -> None:
    source = [_row("country", "US", 20)]
    with patch("core.warehouse.query_report", return_value=source):
        _summary, envelope, _widget = reports.render_report(
            [_Module()],
            "project-1",
            "example/market",
            "2026-07-01",
            "2026-07-01",
            geographic_posture=GeographicPosture(),
        )

    assert envelope["data"]["rows"] == source
    assert "geography" not in envelope["data"]
    assert envelope["meta"]["alerts"] == []


def test_local_report_envelope_exposes_client_defined_markets() -> None:
    """Story 37.8: the split is markets (id + label + members), not raw codes."""

    from core.geographic_reporting import Market

    source = [
        _row("country", "FR", 10),
        _row("country", "MC", 5),
        _row("country", "DE", 20),
        _row("country", "US", 30),
        _row("country", "invalid", 3),
    ]
    posture = GeographicReportContext(
        posture=GeographicPosture(
            mode=LOCAL_MARKETS,
            markets=(
                Market(id="fr", label="France", country_codes=("FR", "MC")),
                Market(id="dach", label="DACH", country_codes=("DE",)),
            ),
        ),
        coverage={"status": "complete", "datastreams": []},
    )

    with patch("core.warehouse.query_report", return_value=source):
        _summary, envelope, _widget = reports.render_report(
            [_Module()],
            "project-1",
            "example/market",
            "2026-07-01",
            "2026-07-01",
            geographic_posture=posture,
        )

    geography = envelope["data"]["geography"]
    assert geography["markets"] == [
        {"id": "dach", "label": "DACH", "country_codes": ["DE"]},
        {"id": "fr", "label": "France", "country_codes": ["FR", "MC"]},
    ]
    assert geography["bindable_market_ids"] == ["dach", "fr"]
    assert OTHER_MARKETS not in geography["bindable_market_ids"]
    assert UNKNOWN_MARKET not in geography["bindable_market_ids"]

    grouped = {row["breakdown_value"]: row for row in envelope["data"]["rows"]}
    assert grouped["fr"]["value"] == 15
    assert grouped["fr"]["market_label"] == "France"
    assert grouped["fr"]["market_country_codes"] == ["FR", "MC"]
    assert grouped[OTHER_MARKETS]["value"] == 30
    assert grouped[UNKNOWN_MARKET]["value"] == 3
    # Exact additive reconciliation against the canonical country total.
    assert envelope["data"]["metrics"]["cost"] == 68


def test_narrative_cites_market_labels_and_never_a_bare_code() -> None:
    from core import narrative

    buckets = [
        {"market_id": "fr", "market_label": "France", "market_kind": "tracked", "value": 15},
        {"market_id": OTHER_MARKETS, "market_kind": "other", "value": 30},
        {"market_id": UNKNOWN_MARKET, "market_kind": "unknown", "value": 3},
    ]

    lines = narrative.build_market_split_lines(buckets=buckets, pull_ids=["pull-1"])

    assert "« France »" in lines[0]
    assert "Marché" in lines[0]
    # The synthetic groupings are never presented as a country/market name.
    assert "Marché" not in lines[1]
    assert "Marché" not in lines[2]
    assert OTHER_MARKETS not in lines[1]
    assert UNKNOWN_MARKET not in lines[2]
    assert narrative.is_country_market_bucket(buckets[0]) is True
    assert narrative.is_country_market_bucket(buckets[1]) is False
    assert narrative.is_country_market_bucket(buckets[2]) is False
    # A market with no label never falls back to its member ISO code.
    assert narrative.market_display_label({"market_id": "fr", "market_code": "FR"}) == "?"
