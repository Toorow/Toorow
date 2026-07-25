"""Story 26.2 -- catalog_driven execution: pull_catalog_daily builds the GAQL
FROM the selection (zero hardcoded non-structure fields) and refuses known
incompatibilities BEFORE any API call via the core 26.1 engine
(core.field_compat.enforce_selection, strict resource context -- review 26.2
F-1). Also covers the review-REJECT fixes: default-selection pruning
exclusions (F-3), non-numeric metric refusal (F-4), FROM-resource whitelist +
data_level routing (F-5), GAQL injection refusals (F-6), excluded-exposure
refusal (F-8). respx-mocked; live probe deferred (AI-13/25.6).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest
import respx
from core.catalog_contract import validate_selection as core_validate_selection
from core.pull_errors import InvalidRequestError

from .conftest import API_BASE

_CID = "9861234567"
_SEARCH_URL = f"{API_BASE}/customers/{_CID}/googleAds:search"

_CATALOG_RESPONSE = {
    "results": [
        {
            "customer": {"id": _CID},
            "campaign": {"id": "22334455", "name": "Brand - Search - FR"},
            "metrics": {"costMicros": "118400000", "clicks": "640"},
            "segments": {"date": "2026-07-01", "device": "MOBILE"},
        }
    ]
}


def _selection(metrics, dimensions, catalog):
    """Resolve a selection exactly as core would (source_fields from catalog)."""
    resolved, issues = core_validate_selection(
        catalog, {"metrics": metrics, "dimensions": dimensions}
    )
    assert issues == [], issues
    return resolved


@respx.mock
def test_catalog_pull_builds_gaql_from_selection(
    connector, catalog, tmp_path, monkeypatch
):
    """Selection {cost_micros, clicks / date, campaign_id, device} -> the GAQL
    carries exactly those tokens (+ structure); no field is hardcoded."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "cat.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    route = respx.post(_SEARCH_URL).mock(
        return_value=httpx.Response(200, json=_CATALOG_RESPONSE)
    )
    selection = _selection(
        ["cost_micros", "clicks"], ["date", "campaign_id", "device"], catalog
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        result = connector.pull_catalog_daily(
            connection_id="c",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="p",
            pull_id="pull_cat_1",
            selection=selection,
            customer_id=_CID,
        )

    query = json.loads(route.calls.last.request.content)["query"]
    for token in (
        "segments.date", "customer.id", "campaign.id", "campaign.name",
        "metrics.cost_micros", "metrics.clicks", "segments.device",
    ):
        assert token in query, f"missing {token} in GAQL: {query}"
    # Nothing beyond the selection + structure tokens (no hardcoded fields).
    assert "metrics.impressions" not in query
    assert "metrics.conversions" not in query
    assert "FROM campaign" in query
    assert "WHERE segments.date BETWEEN '2026-07-01' AND '2026-07-01'" in query
    assert result["row_count"] == 2  # cost + clicks landed long

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(
        "SELECT metric, value_num, segments_json FROM raw_google_ads_daily "
        "WHERE pull_id = 'pull_cat_1' ORDER BY metric"
    ).fetchall()
    con.close()
    by_metric = {r[0]: r for r in rows}
    # Micros rule at landing: canonical 'cost' in currency units.
    assert by_metric["cost"][1] == pytest.approx(118.4)
    assert by_metric["clicks"][1] == 640
    # The selected segment landed in the canonical segments_json grain column.
    assert json.loads(by_metric["cost"][2]) == {"device": "MOBILE"}


def test_catalog_pull_refuses_shopping_segment_on_campaign_before_api(
    connector, catalog
):
    """segments.product_item_id is scoped to shopping_performance_view: refused
    pre-call by the CORE 26.1 engine (no respx mock active -- an HTTP attempt
    would raise ConnectError, not InvalidRequestError, proving the refusal
    happens BEFORE the call). Review 26.2 F-1: the violation carries the
    generated rule id and the field list in provider_payload."""
    selection = _selection(
        ["cost_micros"], ["date", "campaign_id", "product_item_id"], catalog
    )
    with pytest.raises(InvalidRequestError) as exc:
        connector.pull_catalog_daily(
            connection_id="c",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="p",
            pull_id="pull_cat_bad",
            selection=selection,
            customer_id=_CID,
        )
    assert exc.value.error_class == "invalid_request"
    assert "product_item_id" in str(exc.value)
    assert "gaql-selectable-set-campaign" in str(exc.value)
    violations = exc.value.provider_payload["violations"]
    assert any("product_item_id" in v["fields"] for v in violations)


def test_catalog_pull_refuses_foreign_resource_attribute_before_api(
    connector, catalog
):
    """keyword_text (ad_group_criterion.*) is not selectable FROM campaign."""
    selection = _selection(
        ["cost_micros"], ["date", "campaign_id", "keyword_text"], catalog
    )
    with pytest.raises(InvalidRequestError) as exc:
        connector.pull_catalog_daily(
            connection_id="c",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="p",
            pull_id="pull_cat_attr",
            selection=selection,
            customer_id=_CID,
        )
    assert "keyword_text" in str(exc.value)
    assert "gaql-selectable-set-campaign" in str(exc.value)


def test_module_rules_are_the_core_26_1_contract(connector, catalog):
    """Review 26.2 F-1: the module ships a schema-'1' field_compatibility
    block that the CORE engine loads, validates and enforces -- no module-
    local validator remains."""
    from core import field_compat

    from .conftest import MODULE_DIR

    rules = field_compat.load_module_rules(MODULE_DIR)
    assert rules is not None, "block must opt in (schema_version marker)"
    assert rules["schema_version"] == "1"
    assert {r["kind"] for r in rules["rules"]} == {"selectable_set"}
    assert {r["scope"]["resource"] for r in rules["rules"]} == {
        "campaign", "ad_group", "ad_group_ad", "keyword_view", "search_term_view",
    }
    # The core engine flags a scoped segment on the wrong resource...
    bad = _selection(["cost_micros"], ["date", "campaign_id", "hotel_city"], catalog)
    violations = field_compat.validate_selection(
        bad, rules, context={"resource": "campaign"}
    )
    assert any("hotel_city" in v.fields for v in violations)
    # ... a legal selection passes cleanly ...
    ok = _selection(["cost_micros"], ["date", "campaign_id", "device"], catalog)
    assert field_compat.validate_selection(
        ok, rules, context={"resource": "campaign"}
    ) == []
    # ... and the context is STRICT by default (26.1 F-6 -- wanted here too).
    with pytest.raises(ValueError):
        field_compat.validate_selection(ok, rules)


def test_catalog_pull_refuses_excluded_field_before_api(connector, catalog):
    """Review 26.2 F-8: exposure=excluded catalog fields are refused pre-call
    with the field id in the typed message."""
    selection = _selection(
        ["auction_insight_search_impression_share"], ["date", "campaign_id"], catalog
    )
    with pytest.raises(InvalidRequestError) as exc:
        connector.pull_catalog_daily(
            connection_id="c",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="p",
            pull_id="pull_cat_excl",
            selection=selection,
            customer_id=_CID,
        )
    assert exc.value.error_class == "invalid_request"
    assert "auction_insight_search_impression_share" in str(exc.value)
    assert "excluded" in str(exc.value)


def test_catalog_pull_refuses_non_numeric_metric_before_api(connector, catalog):
    """Review 26.2 F-4: an EXPLICITLY selected non-numeric metric (enum/array/
    string) is a typed pre-call refusal LISTING the fields -- never a silent
    drop at landing."""
    selection = _selection(
        ["historical_creative_quality_score", "cost_micros"],
        ["date", "campaign_id"],
        catalog,
    )
    with pytest.raises(InvalidRequestError) as exc:
        connector.pull_catalog_daily(
            connection_id="c",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="p",
            pull_id="pull_cat_nonnum",
            selection=selection,
            customer_id=_CID,
        )
    assert exc.value.error_class == "invalid_request"
    assert "historical_creative_quality_score" in str(exc.value)
    assert "non-numeric" in str(exc.value)
    # The numeric co-selected metric is NOT blamed.
    assert "cost_micros" not in str(exc.value)


def test_catalog_pull_refuses_unknown_resource(connector, catalog):
    """Review 26.2 F-5: FROM-resource whitelist -- unknown resource is a typed
    refusal, never a half-validated query."""
    selection = _selection(["cost_micros"], ["date", "campaign_id"], catalog)
    with pytest.raises(InvalidRequestError) as exc:
        connector.pull_catalog_daily(
            connection_id="c",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="p",
            pull_id="pull_cat_res",
            selection=selection,
            customer_id=_CID,
            resource="shopping_performance_view",
        )
    assert "shopping_performance_view" in str(exc.value)
    assert "campaign" in str(exc.value)  # supported list included


@respx.mock
def test_catalog_pull_resource_routes_data_level(
    connector, catalog, tmp_path, monkeypatch
):
    """Review 26.2 F-5: rows land at the resource's data_level (ad_group ->
    AD_GROUP), and the GAQL FROM matches."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "cat_ag.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    route = respx.post(_SEARCH_URL).mock(
        return_value=httpx.Response(200, json=_CATALOG_RESPONSE)
    )
    selection = _selection(
        ["cost_micros"], ["date", "campaign_id", "ad_group_id"], catalog
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        connector.pull_catalog_daily(
            connection_id="c",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="p",
            pull_id="pull_cat_ag",
            selection=selection,
            customer_id=_CID,
            resource="ad_group",
        )
    query = json.loads(route.calls.last.request.content)["query"]
    assert "FROM ad_group" in query

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    levels = con.execute(
        "SELECT DISTINCT data_level FROM raw_google_ads_daily "
        "WHERE pull_id = 'pull_cat_ag'"
    ).fetchall()
    con.close()
    assert levels == [("AD_GROUP",)]


def test_catalog_pull_refuses_malformed_dates(connector, catalog):
    """Review 26.2 F-6: date bounds are fromisoformat-parsed -- an injection
    payload in date_from never reaches the GAQL string."""
    selection = _selection(["cost_micros"], ["date", "campaign_id"], catalog)
    with pytest.raises(InvalidRequestError) as exc:
        connector.pull_catalog_daily(
            connection_id="c",
            date_from="2026-07-01' OR campaign.name != '",
            date_to="2026-07-01",
            project_id="p",
            pull_id="pull_cat_inj1",
            selection=selection,
            customer_id=_CID,
        )
    assert "date_from" in str(exc.value)


def test_catalog_pull_refuses_non_catalog_gaql_token(connector):
    """Review 26.2 F-6: every SELECT token must be lexically safe AND declared
    in the catalog source_fields -- a tampered source_fields mapping is a
    typed refusal."""
    selection = {
        "metrics": ["clicks"],
        "dimensions": ["date"],
        "source_fields": {
            "clicks": "metrics.clicks, campaign.name FROM ad_group--",
            "date": "segments.date",
        },
    }
    with pytest.raises(InvalidRequestError) as exc:
        connector.pull_catalog_daily(
            connection_id="c",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="p",
            pull_id="pull_cat_inj2",
            selection=selection,
            customer_id=_CID,
        )
    assert "GAQL token" in str(exc.value)


@respx.mock
def test_catalog_pull_none_selection_uses_tier_core_default(
    connector, tmp_path, monkeypatch
):
    """A None selection resolves the catalog tier-core default and still pulls.

    Review 26.2 F-3 EXCLUSION assertions: the default GAQL must contain
    NEITHER segments.hour NOR segments.click_type (nor any other TIME segment
    beyond segments.date, nor a row-restricting segment, nor a non-numeric
    metric) -- the naive tier-core span would silently reshape the daily
    grain and the row totals."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "cat_def.duckdb"))

    route = respx.post(_SEARCH_URL).mock(
        return_value=httpx.Response(200, json=_CATALOG_RESPONSE)
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        connector.pull_catalog_daily(
            connection_id="c",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="p",
            pull_id="pull_cat_def",
            selection=None,
            customer_id=_CID,
        )
    assert route.called
    query = json.loads(route.calls.last.request.content)["query"]
    # Tier-core catalog metrics are in the default selection.
    assert "metrics.cost_micros" in query
    assert "metrics.impressions" in query
    assert "metrics.clicks" in query
    # (a) TIME reduced to segments.date alone.
    assert "segments.date" in query
    for time_token in (
        "segments.hour", "segments.day_of_week", "segments.week",
        "segments.month", "segments.quarter", "segments.year",
    ):
        assert time_token not in query, f"default must not carry {time_token}"
    # (b) row-restricting segments excluded from the DEFAULT.
    assert "segments.click_type" not in query
    assert "segments.keyword" not in query
    # (c) non-numeric metrics excluded from the DEFAULT (array-typed).
    assert "metrics.interaction_event_types" not in query
    # (d) foreign-resource structure attributes pruned (FROM campaign).
    assert "ad_group.id" not in query
    assert "ad_group_criterion" not in query
