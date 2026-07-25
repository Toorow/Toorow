from __future__ import annotations

import copy

import pytest
from core.geographic_reporting import LOCAL_MARKETS, GeographicPosture
from core.geographic_semantics import (
    OTHER_MARKETS,
    UNKNOWN_MARKET,
    GeographicAggregationError,
    group_market_reporting_rows,
)


def _row(country: object, value: float, *, metric: str = "cost", **extra: object) -> dict:
    return {
        "date": "2026-07-01",
        "connector": "example",
        "metric": metric,
        "breakdown_dimension": "country",
        "breakdown_value": country,
        "value": value,
        "pull_id": "pull-1",
        **extra,
    }


def test_groups_tracked_other_and_unknown_without_mutating_source_rows() -> None:
    source = [
        _row("FR", 10),
        _row("Germany", 20),
        _row("US", 30),
        _row("Atlantis", 4),
        _row(None, 6),
    ]
    original = copy.deepcopy(source)

    result = group_market_reporting_rows(
        source,
        GeographicPosture(mode=LOCAL_MARKETS, country_codes=("FR", "DE")),
    )

    assert source == original
    values = {row["breakdown_value"]: row["value"] for row in result.rows}
    assert values == {"FR": 10.0, "DE": 20.0, OTHER_MARKETS: 30.0, UNKNOWN_MARKET: 10.0}
    assert sum(values.values()) == sum(float(row["value"]) for row in source)
    assert result.country_partition == "country"
    assert {item["raw_value"] for item in result.data_quality} == {"Atlantis", None}


def test_aliases_normalize_before_grouping_and_tracked_codes_remain_bindable() -> None:
    result = group_market_reporting_rows(
        [_row("France", 1), _row("USA", 2)],
        GeographicPosture(mode=LOCAL_MARKETS, country_codes=("FR",)),
    )

    tracked, other = result.rows
    assert tracked["breakdown_value"] == "FR"
    assert tracked["market_code"] == "FR"
    assert tracked["market_kind"] == "tracked"
    assert other["breakdown_value"] == OTHER_MARKETS
    assert other["market_code"] is None
    assert other["market_kind"] == "other"


def test_country_partition_is_pinned_and_parallel_dimensions_are_not_summed() -> None:
    rows = [
        _row("FR", 10),
        {**_row("mobile", 999), "breakdown_dimension": "device"},
        {**_row("FR>mobile", 888), "breakdown_dimension": "country>device"},
    ]
    result = group_market_reporting_rows(
        rows,
        GeographicPosture(mode=LOCAL_MARKETS, country_codes=("FR",)),
    )

    assert len(result.rows) == 1
    assert result.rows[0]["value"] == 10
    assert result.excluded_parallel_rows == 2


def test_non_additive_ratio_uses_declared_components_after_grouping() -> None:
    rows = [
        _row("US", 0.5, metric="ctr", semantic_numerator=10, semantic_denominator=20),
        _row("CA", 0.1, metric="ctr", semantic_numerator=10, semantic_denominator=100),
    ]
    result = group_market_reporting_rows(
        rows,
        GeographicPosture(mode=LOCAL_MARKETS, country_codes=("FR",)),
    )

    assert len(result.rows) == 1
    assert result.rows[0]["breakdown_value"] == OTHER_MARKETS
    assert result.rows[0]["value"] == pytest.approx(20 / 120)
    assert result.rows[0]["semantic_aggregation_rule"] == "ratio"


def test_non_additive_metric_fails_closed_without_required_semantic_evidence() -> None:
    with pytest.raises(GeographicAggregationError, match="semantic_numerator"):
        group_market_reporting_rows(
            [_row("US", 0.5, metric="ctr")],
            GeographicPosture(mode=LOCAL_MARKETS, country_codes=("FR",)),
        )


def test_max_rule_fails_closed_when_all_grouped_values_are_absent(monkeypatch) -> None:
    from core import geographic_semantics

    monkeypatch.setattr(
        geographic_semantics.report_dictionary, "aggregation_rule", lambda metric: "max"
    )
    rows = [
        {**_row("US", 0, metric="peak"), "value": None},
        {**_row("CA", 0, metric="peak"), "value": None},
    ]
    with pytest.raises(GeographicAggregationError, match="requires value"):
        group_market_reporting_rows(
            rows,
            GeographicPosture(mode=LOCAL_MARKETS, country_codes=("FR",)),
        )


def test_changing_tracked_set_reclassifies_same_retained_rows() -> None:
    source = [_row("US", 7)]
    before = group_market_reporting_rows(
        source,
        GeographicPosture(mode=LOCAL_MARKETS, country_codes=("FR",)),
    )
    after = group_market_reporting_rows(
        source,
        GeographicPosture(mode=LOCAL_MARKETS, country_codes=("US",)),
    )

    assert before.rows[0]["breakdown_value"] == OTHER_MARKETS
    assert after.rows[0]["breakdown_value"] == "US"
    assert source[0]["breakdown_value"] == "US"


def test_country_vocabulary_rejects_cross_country_alias_collision(tmp_path) -> None:
    from core.country_vocabulary import CountryVocabularyError, load_country_vocabulary

    seed = tmp_path / "dim_country.csv"
    seed.write_text(
        "iso_code,display_name,aliases\nFR,France,France|shared|FR\nDE,Germany,Germany|SHARED|DE\n",
        encoding="utf-8",
    )

    with pytest.raises(CountryVocabularyError, match="maps to both FR and DE"):
        load_country_vocabulary(seed)


@pytest.mark.parametrize(
    ("metric", "evidence_columns"),
    [
        ("ctr", ("semantic_numerator", "semantic_denominator")),
        ("roas", ("semantic_numerator", "semantic_denominator")),
        ("cpa", ("semantic_numerator", "semantic_denominator")),
        ("average_position", ("semantic_weight",)),
    ],
)
def test_non_additive_warehouse_query_carries_grouping_evidence(
    metric: str, evidence_columns: tuple[str, ...]
) -> None:
    from core.warehouse import _build_semantic_view_query

    sql, _params = _build_semantic_view_query(
        "analytics_", "project-1", "example", metric, ["country"], "2026-07-01", "2026-07-31"
    )

    for column in evidence_columns:
        assert column in sql
