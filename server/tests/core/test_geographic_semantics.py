"""Read-layer grouping against a governed hierarchy version (Story 48.2).

These tests replace the ones written for ``project_preferences.local_markets``
and the fixed ``__other_markets__`` bucket. The properties that mattered then
still matter and are re-proven here -- facts are never mutated, the country
partition is pinned, non-additive metrics fail closed -- and the ones the old
model could not express are proven for the first time: a Region resolves, the
residual drills down, and the three buckets reconcile exactly.
"""

from __future__ import annotations

import copy
from datetime import date

import pytest
from core.country_registry import (
    BUCKET_ASSIGNED,
    BUCKET_REST_OF_WORLD,
    MARKET,
    REGION,
    REST_OF_WORLD,
    build_projection,
    rest_of_world_payload,
)
from core.geographic_semantics import (
    COUNTRY_ABSENT_BUCKET_ID,
    COUNTRY_ABSENT_BUCKET_KIND,
    COUNTRY_ABSENT_BUCKET_LABEL,
    COUNTRY_ABSENT_FINDING,
    UNKNOWN_BUCKET_ID,
    UNKNOWN_BUCKET_LABEL,
    GeographicAggregationError,
    drill_rest_of_world,
    geography_bucket_descriptors,
    group_geography_reporting_rows,
    is_country_absent_bucket,
    reconciliation,
)
from core.master_data import Membership

FRANCE, DACH, EMEA, ROW = "mdnode_FR", "mdnode_DA", "mdnode_EM", "mdnode_RW"
VERSION = "mdver_PINNED"

NODES = [
    {"id": FRANCE, "label": "France", "node_kind": MARKET},
    {"id": DACH, "label": "DACH", "node_kind": MARKET},
    {"id": EMEA, "label": "EMEA", "node_kind": REGION},
    {"id": ROW, "label": "Rest of World", "node_kind": REST_OF_WORLD},
]
VALUES = ("FR", "RE", "DE", "US", "CA", "BR")


def _projection(*, tracked: str = "FR", version_id: str = VERSION):
    """A hierarchy where *tracked* sits in France, inside EMEA."""

    return build_projection(
        hierarchy_version_id=version_id,
        vocabulary_version_id="mdvoc_1",
        registry_id="mdreg_1",
        memberships=[
            Membership(parent_node_id=FRANCE, child_value=tracked),
            Membership(parent_node_id=EMEA, child_node_id=FRANCE),
        ],
        nodes=NODES,
        canonical_values=VALUES,
        rest_of_world=rest_of_world_payload(node_id=ROW),
        as_of=date(2026, 7, 30),
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


# ---------------------------------------------------------------------------
# Facts are inputs. Meaning is a version.
# ---------------------------------------------------------------------------


def test_grouping_never_mutates_the_retained_rows() -> None:
    source = [_row("FR", 10), _row("US", 4), _row("not-a-country", 1)]
    before = copy.deepcopy(source)
    group_geography_reporting_rows(source, _projection())
    assert source == before


def test_every_semantic_row_names_the_hierarchy_version_that_produced_it() -> None:
    result = group_geography_reporting_rows([_row("FR", 10), _row("US", 4)], _projection())
    assert result.hierarchy_version_id == VERSION
    assert {row["geography_hierarchy_version_id"] for row in result.rows} == {VERSION}


def test_regrouping_reclassifies_the_same_retained_rows_without_a_pull() -> None:
    source = [_row("US", 7)]
    before = group_geography_reporting_rows(source, _projection(tracked="FR"))
    after = group_geography_reporting_rows(source, _projection(tracked="US", version_id="mdver_2"))
    assert before.rows[0]["geography_bucket_kind"] == BUCKET_REST_OF_WORLD
    assert after.rows[0]["geography_bucket_kind"] == BUCKET_ASSIGNED
    assert after.rows[0]["market_id"] == FRANCE
    assert source[0]["breakdown_value"] == "US"


# ---------------------------------------------------------------------------
# The three buckets, and the difference between the last two.
# ---------------------------------------------------------------------------


def test_assigned_rows_carry_market_and_region() -> None:
    result = group_geography_reporting_rows([_row("FR", 10)], _projection())
    row = result.rows[0]
    assert row["geography_bucket_kind"] == BUCKET_ASSIGNED
    assert (row["market_id"], row["market_label"]) == (FRANCE, "France")
    assert (row["region_id"], row["region_label"]) == (EMEA, "EMEA")


def test_a_valid_untracked_country_is_rest_of_world_and_produces_no_dq() -> None:
    result = group_geography_reporting_rows([_row("BR", 3)], _projection())
    assert result.rows[0]["geography_bucket_kind"] == BUCKET_REST_OF_WORLD
    assert result.data_quality == ()


def test_an_unresolvable_value_stays_dq_without_a_third_primary_segment() -> None:
    result = group_geography_reporting_rows([_row("Republic of Nowhere", 2)], _projection())
    assert result.rows == ()
    assert len(result.data_quality) == 1
    finding = result.data_quality[0]
    assert finding["code"] == "country_value_unmapped"
    assert finding["repair_path"] == "dimension_conformance"
    assert finding["source_row"]["breakdown_value"] == "Republic of Nowhere"
    assert finding["source_row"]["value"] == 2
    assert finding["geography_hierarchy_version_id"] == VERSION


def test_unknown_stays_out_of_other_and_remains_separate_dq_evidence() -> None:
    result = group_geography_reporting_rows(
        [_row("BR", 3), _row("nowhere", 5)], _projection()
    )
    assert len(result.rows) == 1
    assert result.rows[0]["geography_bucket_kind"] == BUCKET_REST_OF_WORLD
    assert result.rows[0]["value"] == 3
    assert result.data_quality[0]["source_row"]["value"] == 5


def test_aliases_normalize_before_grouping() -> None:
    result = group_geography_reporting_rows([_row("France", 6), _row("FR", 4)], _projection())
    assert len(result.rows) == 1
    assert result.rows[0]["market_id"] == FRANCE
    assert result.rows[0]["value"] == 10


# ---------------------------------------------------------------------------
# Rest of World is drillable, which is what makes it a grouping and not a loss.
# ---------------------------------------------------------------------------


def test_rest_of_world_drills_to_country_rows_without_a_provider_pull() -> None:
    rows = [_row("FR", 10), _row("US", 4), _row("BR", 3), _row("nowhere", 1)]
    detail = drill_rest_of_world(rows, _projection())
    assert {row["country_id"] for row in detail} == {"US", "BR"}
    assert all(row["geography_bucket_kind"] == BUCKET_REST_OF_WORLD for row in detail)
    assert all(row["geography_hierarchy_version_id"] == VERSION for row in detail)


def test_the_drill_preserves_the_raw_source_value_as_provenance() -> None:
    detail = drill_rest_of_world([_row("United States", 4)], _projection())
    assert detail[0]["country_id"] == "US"
    assert detail[0]["source_country_value"] == "United States"


def test_rest_of_world_is_not_bindable_and_unknown_is_a_bucket_not_a_place() -> None:
    descriptors = {item["id"]: item for item in geography_bucket_descriptors(_projection())}
    assert descriptors[FRANCE]["bindable"] is True
    assert descriptors[ROW]["bindable"] is False
    assert descriptors[UNKNOWN_BUCKET_ID]["bindable"] is False
    assert descriptors[UNKNOWN_BUCKET_ID]["primary_report_segment"] is False


def test_neither_absence_bucket_reads_as_a_country() -> None:
    """The class, closed. Story 58.5.

    The two buckets of this module that are NOT countries used to speak in two
    voices: the new one stated an absence (`No country reported`) while the older
    one was the bare word `Unknown`, which a reader scanning a list of country names
    cannot tell from a country. They are neighbours in one file; a person reading
    either one is asking the same question.

    What is asserted is the property, not the wording: a label that could be taken
    for a place, and a label the vocabulary would resolve, both fail here.
    """
    from core.country_vocabulary import normalize_country_value

    for label in (UNKNOWN_BUCKET_LABEL, COUNTRY_ABSENT_BUCKET_LABEL):
        # More than one word: no ISO name in the 250-row seed is a lone adjective,
        # and a lone word in a column of country names reads as one of them.
        assert len(label.split()) > 1, f"{label!r} reads as a country name"
        assert normalize_country_value(label) is None
        assert label.lower() != "unknown"


# ---------------------------------------------------------------------------
# AC5 -- selected Markets + Other = total; Unknown remains a disclosed subset.
# ---------------------------------------------------------------------------


def test_selected_markets_plus_other_reconcile_to_the_country_grain_total() -> None:
    rows = [_row("FR", 10), _row("US", 4), _row("BR", 3), _row("nowhere", 1)]
    result = group_geography_reporting_rows(rows, _projection())
    proof = reconciliation(result, metric="cost")
    assert proof["additive"] is True
    assert proof["assigned"] == 10
    assert proof["rest_of_world"] == 7
    assert proof["unknown_contribution"] == 1
    assert proof["assigned"] + proof["rest_of_world"] == proof["resolved_total"]
    assert proof["source_total"] == sum(row["value"] for row in rows)
    assert proof["geography_hierarchy_version_id"] == VERSION


# ---------------------------------------------------------------------------
# THE ROW THE SOURCE GAVE NO COUNTRY FOR -- story 58.5, arbitrage 1.
#
# The mart used to answer this fact twice, in opposite ways, and both were latent:
# `fact_daily_kpi` turned `breakdown_value` NULL by concatenation and broke the
# nightly build, `int_country_daily_kpi` deleted the row and let the total by country
# drift away from the total of the day in silence. It is now ONE declared bucket,
# written by `dbt/macros/country_absence.sql` and read here. These tests hold the two
# things that make it safe: it is not a country, and it is not `Unknown`.
# ---------------------------------------------------------------------------


def _macro_source() -> str:
    from pathlib import Path

    macro = (
        Path(__file__).resolve().parents[3] / "dbt" / "macros" / "country_absence.sql"
    )
    assert macro.exists(), f"the mart-side declaration is missing: {macro}"
    return macro.read_text(encoding="utf-8")


def test_the_mart_and_the_reader_spell_the_bucket_the_same_way() -> None:
    """One sentinel, two engines. A drift here makes the bucket unfindable.

    The value is written by dbt and read by Python; nothing in either language can
    check the other. Reading the macro is what closes it -- rename the literal on
    either side and this is the test that reddens, instead of a country block that
    quietly stops showing a bucket that is right there in the table.
    """
    assert f"'{COUNTRY_ABSENT_BUCKET_ID}'" in _macro_source()


def test_the_bucket_is_not_shaped_like_a_country_and_never_could_be() -> None:
    """`country_vocabulary` refuses anything that is not two capitals (AC of 37.7).

    So this identity cannot enter the ISO set even by accident, and the seed of 250
    codes cannot contain it. The label is checked too: `Unknown` on its own reads
    exactly like a place, which is what the plan forbids for this bucket.
    """
    from core.country_vocabulary import normalize_country_value

    assert normalize_country_value(COUNTRY_ABSENT_BUCKET_ID) is None
    assert len(COUNTRY_ABSENT_BUCKET_ID) != 2
    assert not COUNTRY_ABSENT_BUCKET_ID.isalpha()
    # The label states an ABSENCE. A reader must not be able to take it for a
    # country name, and `No country reported` cannot be mistaken for one.
    assert "no country" in COUNTRY_ABSENT_BUCKET_LABEL.lower()
    assert COUNTRY_ABSENT_BUCKET_KIND != "country"


def test_is_country_absent_bucket_answers_on_the_value_and_not_on_a_shape() -> None:
    assert is_country_absent_bucket(COUNTRY_ABSENT_BUCKET_ID) is True
    for other in ("FR", "", None, 42, UNKNOWN_BUCKET_ID, "__country_absent__ "):
        assert is_country_absent_bucket(other) is False


def test_an_absent_country_is_its_own_finding_and_not_a_mapping_to_repair() -> None:
    """`unknown` sends a person to the conformance surface. Here there is nothing there.

    A value that did not resolve is repaired by a governed conformance mapping and the
    raw value is the evidence for it. A value that was never reported has no evidence
    and no lever on that surface: filing it under `country_value_unmapped` would queue
    work on a desk that cannot do it, against a value that does not exist.
    """
    result = group_geography_reporting_rows(
        [_row("FR", 10), _row(COUNTRY_ABSENT_BUCKET_ID, 4)], _projection()
    )

    # It is NOT a reporting segment: one row out, the assigned one.
    assert [row["market_id"] for row in result.rows] == [FRANCE]
    assert result.rows[0]["value"] == 10

    finding = result.data_quality[0]
    assert finding["code"] == COUNTRY_ABSENT_FINDING
    assert finding["code"] != "country_value_unmapped"
    assert finding["repair_path"] is None
    assert finding["raw_value"] is None
    assert finding["source_row"]["value"] == 4


def test_the_absent_bucket_is_reconciled_apart_and_the_total_still_holds() -> None:
    """Nothing is lost, and the two absences are not one number.

    The identity that matters is that every country row is still accounted for. What
    changed is that "we could not read this value" and "there was no value" no longer
    share a figure -- they are two different amounts of work.
    """
    rows = [
        _row("FR", 10),
        _row("BR", 3),
        _row("nowhere", 1),
        _row(COUNTRY_ABSENT_BUCKET_ID, 4),
    ]
    proof = reconciliation(group_geography_reporting_rows(rows, _projection()), metric="cost")

    assert proof["assigned"] == 10
    assert proof["rest_of_world"] == 3
    assert proof["unknown_contribution"] == 1
    assert proof["country_absent_contribution"] == 4
    assert proof["source_total"] == sum(row["value"] for row in rows)


def test_the_absent_bucket_is_never_drilled_as_a_country_of_rest_of_world() -> None:
    detail = drill_rest_of_world(
        [_row("BR", 3), _row(COUNTRY_ABSENT_BUCKET_ID, 4)], _projection()
    )
    assert {row["country_id"] for row in detail} == {"BR"}


def test_a_non_additive_metric_is_named_rather_than_summed_into_a_false_total() -> None:
    rows = [
        _row("FR", 0.5, metric="ctr", semantic_numerator=5, semantic_denominator=10),
        _row("BR", 0.2, metric="ctr", semantic_numerator=2, semantic_denominator=10),
    ]
    proof = reconciliation(group_geography_reporting_rows(rows, _projection()), metric="ctr")
    assert proof["additive"] is False


# ---------------------------------------------------------------------------
# The partition is pinned, and non-additive rules fail closed.
# ---------------------------------------------------------------------------


def test_parallel_breakdown_dimensions_are_excluded_not_summed() -> None:
    rows = [
        _row("FR", 10),
        {**_row("FR", 99), "breakdown_dimension": "device"},
    ]
    result = group_geography_reporting_rows(rows, _projection())
    assert result.excluded_parallel_rows == 1
    assert sum(row["value"] for row in result.rows) == 10


def test_non_additive_ratio_uses_declared_components_after_grouping() -> None:
    rows = [
        _row("US", 0.5, metric="ctr", semantic_numerator=5, semantic_denominator=10),
        _row("CA", 0.1, metric="ctr", semantic_numerator=1, semantic_denominator=10),
    ]
    result = group_geography_reporting_rows(rows, _projection())
    assert len(result.rows) == 1
    assert result.rows[0]["value"] == pytest.approx(0.3)
    assert result.rows[0]["semantic_aggregation_rule"] == "ratio"


def test_non_additive_metric_fails_closed_without_required_semantic_evidence() -> None:
    with pytest.raises(GeographicAggregationError, match="semantic_numerator"):
        group_geography_reporting_rows([_row("US", 0.5, metric="ctr")], _projection())


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
        group_geography_reporting_rows(rows, _projection())


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
