"""Story 50.2 -- what the Explore facets and the six Result lenses must guarantee.

The fixtures are not invented. `_MATRIX` is the same shape Story 50.1's own tests
read out of `app.semantic_compiled_artifacts`, and the Result record below is the
exact column tuple `load_result_record` selects -- including the `unavailable`
outcome with `missing_link=datastream_output_versions`, which is the REAL answer
this repository returns today because no Datastream has published an output yet.

Testing against the real unavailable state matters more than testing against a
convenient success: the lens code that only ever sees rows is the code that
renders "0" for "we could not ask".
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from core.analyze_workbench import (
    LENSES,
    UnknownLens,
    WorkbenchNotFound,
    compose_query_facets,
    compose_result_lens,
    load_query_spec_version,
    load_result_record,
)

CLICKS, CLICKS_V = "sc_clicks", "scv_clicks"
DATE, DATE_V = "sc_date", "scv_date"
COUNTRY, COUNTRY_V = "sc_country", "scv_country"
RESULT_ID = "qr_01EXAMPLE"
SPEC_VERSION_ID = "qsv_01EXAMPLE"
SPEC_ID = "qs_01EXAMPLE"
VIEW_ID, VIEW_VERSION_ID = "sv_EXAMPLE", "svv_EXAMPLE"
AT = datetime(2026, 7, 31, 9, 0, tzinfo=timezone.utc)

_MATRIX = {
    "metrics": [{"concept_id": CLICKS, "version_id": CLICKS_V, "label": "Clicks"}],
    "dimensions": [
        {"concept_id": DATE, "version_id": DATE_V, "label": "Date"},
        {"concept_id": COUNTRY, "version_id": COUNTRY_V, "label": "Country"},
    ],
    "cells": [
        {"metric_id": CLICKS, "dimension_id": DATE, "queryable": True, "join_path": []},
        {
            "metric_id": CLICKS,
            "dimension_id": COUNTRY,
            "queryable": False,
            "reason": "no conformed path from Clicks to Country",
        },
    ],
}

_SPEC = {
    "contract_version": "query-spec.v1",
    "semantic_view_id": VIEW_ID,
    "semantic_view_version_id": VIEW_VERSION_ID,
    "measures": [{"id": CLICKS, "version_id": CLICKS_V}],
    "dimensions": [{"id": DATE, "version_id": DATE_V}],
    "filters": [
        {
            "member_id": COUNTRY,
            "operator": "eq",
            "value": "FR",
            "classification_object_id": "mdc_EXAMPLE",
            "hierarchy_id": "mdd_EXAMPLE",
            "hierarchy_version_id": "mdc_EXAMPLE@3",
        }
    ],
    "sort": [{"member_id": CLICKS, "direction": "desc"}],
    "comparison": "previous_period",
    "grain": "day",
    "row_limit": 100,
    "time": {
        "member_id": DATE,
        "start": "2026-07-01",
        "end": "2026-07-31",
        "timezone": "UTC",
        "as_of": "2026-07-31",
        "reporting_boundary_id": None,
    },
}

_CONCEPT_ROWS = [
    (
        CLICKS_V, CLICKS, "metric", "clicks", "Clicks", "Confirmed ad clicks", "integer", None,
        {"op": "sum", "field": "clicks"}, "sum", "additive", [], None, None, "count", [],
        None, None, 4, "published",
    ),
    (
        DATE_V, DATE, "dimension", "date", "Date", "Reporting date", "date", None,
        None, None, None, [], None, "event_time", "time", ["day", "week"],
        None, None, 2, "published",
    ),
    (
        COUNTRY_V, COUNTRY, "dimension", "country", "Country", "Market", "string", None,
        None, None, None, [], None, None, "geo", [],
        None, {"classification_type": "market"}, 1, "published",
    ),
]


class _Cursor:
    """Answers by the table the statement names.

    Matching on the table rather than on call order is deliberate: a cursor that
    answers positionally silently feeds the wrong arity the day a handler adds a
    query, and the test then proves a 500 the code never had.
    """

    def __init__(self, state: dict):
        self.state = state
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        self._last = sql
        self.state.setdefault("statements", []).append(sql)

    def fetchone(self):
        s = self._last
        # THE MOST SPECIFIC STATEMENT FIRST. The Result record read joins
        # `app.semantic_view_versions` since 2026-08-21 (to serve the view's NAME
        # instead of its id), and a dispatch ordered by generality answered it
        # with a view row -- twelve columns of the wrong shape, silently zipped.
        if "app.query_result_payloads" in s:
            return None if not self.state.get("result_found", True) else self.state["result_row"]
        if "app.semantic_view_versions" in s and "app.semantic_views" in s:
            if not self.state.get("view_found", True):
                return None
            return (
                VIEW_VERSION_ID, self.state.get("status", "published"),
                self.state.get("query_policy", {}), "Search performance", "search_performance",
                3, ["bd_EXAMPLE"], "Search performance",
            )
        if "app.semantic_view_versions" in s:
            return ("Search performance", "search_performance", 3, "published", "Search perf")
        if "app.semantic_compiled_artifacts" in s:
            if not self.state.get("compiled", True):
                return None
            return (self.state.get("matrix", _MATRIX),)
        if "app.query_spec_versions" in s and "app.query_specs" in s:
            return self.state.get("spec_version_row")
        return None

    def fetchall(self):
        s = self._last
        if "app.semantic_concept_versions" in s:
            return _CONCEPT_ROWS
        if "app.mdm_business_classifications" in s:
            return self.state.get("classifications", [])
        if "app.dq_evaluations" in s:
            return self.state.get("dq_rows", [])
        if "predecessor_result_id = " in s:
            return self.state.get("successor_rows", [])
        if "app.query_results" in s:
            return self.state.get("result_rows", [])
        return []


class _Conn:
    def __init__(self, **state):
        self.state = dict(state)

    def cursor(self):
        return _Cursor(self.state)


def _result_row(
    outcome="unavailable",
    rows=None,
    manifest=None,
    truncated=False,
    ai_path_id=None,
    predecessor=None,
    result_schema=None,
):
    rows = rows if rows is not None else []
    manifest = manifest if manifest is not None else {
        "semantic_view_version_id": VIEW_VERSION_ID,
        "query_spec_version_id": SPEC_VERSION_ID,
        "grain": "day",
        "row_limit": 100,
        "unavailable_reason": "this Datastream has no published output to query yet",
        "missing_link": "datastream_output_versions",
    }
    return (
        RESULT_ID, "qea_01EXAMPLE", SPEC_VERSION_ID, outcome, ai_path_id,
        None if ai_path_id else "No AI path", "h" * 64, len(rows), sum(len(r) for r in rows),
        len(json.dumps(rows)), truncated, predecessor, AT, AT,
        result_schema
        if result_schema is not None
        else (
            {"fields": [{"name": "clicks", "type": "integer"}, {"name": "date", "type": "date"}]}
            if rows else {"fields": []}
        ),
        manifest, rows, SPEC_ID, 1, VIEW_ID, VIEW_VERSION_ID, _SPEC, "s" * 64, AT,
        # The WORDS behind the two pins, served rather than inferred.
        "Search performance", "search_performance", 3,
    )


#: The manifest a Result gets when the execution reached `capture_evidence`
#: (`core.query_execution`, Story 50.1). Copied from what that function returns,
#: NOT invented: `values[].source_field` is the physical column `build_sql` emits
#: as `SUM(col) AS col`, which is why it is `clicks` and not `sc_clicks`.
_CAPTURED_MANIFEST = {
    "semantic_view_version_id": VIEW_VERSION_ID,
    "query_spec_version_id": SPEC_VERSION_ID,
    "grain": "day",
    "row_limit": 100,
    "relation": "gsc_daily",
    "provenance": {
        "source_system": "google-search-console",
        "datastream_id": "ds_EXAMPLE",
        "mapping_version_id": "dmap_EXAMPLE",
        "relation": "gsc_daily",
        "pull_id": "dse_EXAMPLE",
        "publication_log_id": "dpl_EXAMPLE",
        "values": [
            {
                "member_id": CLICKS,
                "source_system": "google-search-console",
                "source_field": "clicks",
                "pull_id": "dse_EXAMPLE",
            },
            {
                "member_id": DATE,
                "source_system": "google-search-console",
                "source_field": "date",
                "pull_id": "dse_EXAMPLE",
            },
        ],
    },
    "freshness": {"output_created_at": "2026-07-31T08:00:00+00:00"},
    "dq_evaluation_ids": [],
    "dq_unavailable_reason": None,
}


def _conn_with_result(**kwargs):
    return _Conn(result_row=_result_row(**kwargs))


def _lens(name, **kwargs):
    return compose_result_lens(
        _conn_with_result(**kwargs),
        org_id="org_EXAMPLE",
        project_id="proj_EXAMPLE",
        result_id=RESULT_ID,
        lens=name,
    )


# ---------------------------------------------------------------------------
# AC3 -- the governed option bundle.
# ---------------------------------------------------------------------------


def _facets(**state):
    return compose_query_facets(
        _Conn(**state),
        org_id="org_EXAMPLE",
        project_id="proj_EXAMPLE",
        semantic_view_id=VIEW_ID,
        semantic_view_version_id=VIEW_VERSION_ID,
    )


def test_facets_carry_the_compiler_refusal_reason_rather_than_hiding_the_pair():
    bundle = _facets()
    by_pair = {(p["measure_id"], p["dimension_id"]): p for p in bundle["pairs"]}
    assert by_pair[(CLICKS, DATE)]["queryable"] is True
    refused = by_pair[(CLICKS, COUNTRY)]
    assert refused["queryable"] is False
    # The compiler's own words. A UI writing its own reason would be guessing why
    # the join failed, and would keep saying it after the compiler changed.
    assert refused["reason"] == "no conformed path from Clicks to Country"


def test_facets_carry_the_semantics_a_safe_choice_needs():
    bundle = _facets()
    clicks = next(m for m in bundle["measures"] if m["concept_id"] == CLICKS)
    assert clicks["aggregation"] == "sum"
    assert clicks["additivity_class"] == "additive"
    assert clicks["definition"] == "Confirmed ad clicks"
    assert clicks["owner_ref"]["workspace"] == "governance"


def test_time_members_are_derived_from_the_model_not_from_the_label():
    bundle = _facets()
    ids = {m["concept_id"] for m in bundle["time"]["members"]}
    # Date declares grains and a time semantic type; Country declares neither and
    # must not become a time member because a human would read its label as one.
    assert ids == {DATE}
    assert bundle["time"]["grains"] == ["day", "week"]
    assert bundle["time"]["grains_unavailable_reason"] is None


def test_an_absent_grain_vocabulary_is_empty_with_a_reason_never_invented():
    # A published view whose only dimension declares no grain. The tempting
    # answer here is `["day", "week", "month"]` -- plausible, universal, and a
    # fabrication that would offer a grain the compiler never proved.
    bundle = _facets(
        matrix={
            "metrics": _MATRIX["metrics"],
            "dimensions": [{"concept_id": COUNTRY, "version_id": COUNTRY_V, "label": "Country"}],
            "cells": [],
        }
    )
    assert bundle["time"]["members"] == []
    assert bundle["time"]["grains"] == []
    assert "declares an allowed grain" in bundle["time"]["grains_unavailable_reason"]


def test_the_control_vocabulary_comes_from_the_validator_so_it_cannot_drift():
    from core.query_specs import _COMPARISONS, _FILTER_OPERATORS, MAX_MEASURES

    bundle = _facets()
    assert set(bundle["time"]["comparisons"]) == set(_COMPARISONS)
    assert set(bundle["filter_operators"]) == set(_FILTER_OPERATORS)
    assert bundle["limits"]["max_measures"] == MAX_MEASURES


def test_the_comparison_preconditions_carry_the_validators_own_sentences():
    """Story 67.9 -- the screen must be able to stop asking, in the door's words.

    Not `in`, not "contains": the whole list, in the declared order, sentence for
    sentence. The defect this replaces was three ADAPTED copies of these
    sentences typed into `QueryDoor.tsx` -- close enough to look right, different
    enough to say something else, and guarded by nothing.
    """
    from core.query_specs import COMPARISON_PRECONDITIONS

    assert _facets()["time"]["comparison_preconditions"] == [
        {"condition": precondition.condition, "message": precondition.message}
        for precondition in COMPARISON_PRECONDITIONS
    ]


def test_a_query_policy_ceiling_lowers_the_offered_limit():
    bundle = _facets(query_policy={"max_row_limit": 250})
    assert bundle["limits"]["max_row_limit"] == 250
    assert bundle["limits"]["default_row_limit"] == 250


def test_a_superseded_version_is_readable_but_states_it_cannot_execute():
    bundle = _facets(status="superseded")
    assert bundle["executable"] is False
    codes = {r["code"] for r in bundle["unavailable_reasons"]}
    assert "version_not_executable" in codes
    # It is still returned in full. Hiding it would make a pinned historical
    # address show nothing, which is not the same as showing history.
    assert bundle["measures"]


def test_a_published_but_uncompiled_version_is_not_executable():
    bundle = _facets(compiled=False)
    assert bundle["executable"] is False
    assert "not_compiled" in {r["code"] for r in bundle["unavailable_reasons"]}


def test_a_missing_view_version_raises_the_one_nondisclosing_error():
    with pytest.raises(WorkbenchNotFound):
        _facets(view_found=False)


def test_a_facet_with_no_approved_classification_is_unavailable_not_an_empty_picker():
    bundle = _facets(classifications=[])
    assert bundle["classification_facets"] == []
    unavailable = bundle["classification_facets_unavailable"]
    assert [u["facet"] for u in unavailable] == ["market"]
    assert "no active `market` classification" in unavailable[0]["reason"]


def test_an_approved_classification_becomes_a_facet_with_other_and_unknown_kept_explicit():
    bundle = _facets(
        classifications=[("mdc_EXAMPLE", "france", "France", "mdd_EXAMPLE", None, 3)]
    )
    facet = bundle["classification_facets"][0]
    assert facet["facet"] == "market"
    assert facet["members"][0]["classification_object_id"] == "mdc_EXAMPLE"
    assert facet["members"][0]["hierarchy_version_id"] == "mdc_EXAMPLE@3"
    assert facet["reserved_members"] == ["Other", "Unknown"]


# ---------------------------------------------------------------------------
# AC5/AC13 -- lens dispatch and non-disclosure.
# ---------------------------------------------------------------------------


def test_an_unknown_lens_is_refused_and_never_repaired_into_view():
    with pytest.raises(UnknownLens):
        _lens("summary")


def test_every_declared_lens_composes_over_the_real_unavailable_result():
    for lens in LENSES:
        body = _lens(lens)
        assert body["lens"] == lens
        assert body["result_id"] == RESULT_ID
        assert body["outcome"] == "unavailable"
        assert body["lenses"] == list(LENSES)


def test_a_foreign_or_absent_result_raises_the_same_error():
    conn = _Conn(result_found=False)
    with pytest.raises(WorkbenchNotFound):
        load_result_record(
            conn, org_id="org_EXAMPLE", project_id="proj_EXAMPLE", result_id=RESULT_ID
        )


def test_scope_is_in_the_where_clause_not_applied_after_the_read():
    conn = _Conn(result_row=_result_row())
    load_result_record(conn, org_id="org_EXAMPLE", project_id="proj_EXAMPLE", result_id=RESULT_ID)
    join = next(s for s in conn.state["statements"] if "app.query_result_payloads" in s)
    assert "r.org_id = %s" in join and "r.project_id = %s" in join


# ---------------------------------------------------------------------------
# AC6 -- View is bounded and pre-empts nothing.
# ---------------------------------------------------------------------------


def test_view_carries_no_visualization_contract():
    body = _lens("view", outcome="success", rows=[{"clicks": 7, "date": "2026-07-01"}])["view"]
    forbidden = {"chart", "chart_type", "encoding", "option", "series", "visualization_spec"}
    assert forbidden.isdisjoint(body.keys())


def test_view_keeps_the_server_totals_visible_next_to_the_returned_slice():
    rows = [{"clicks": i, "date": "2026-07-01"} for i in range(5)]
    body = _lens("view", outcome="success", rows=rows, truncated=True)["view"]
    assert body["server_row_count"] == 5
    assert body["returned_row_count"] == 5
    assert body["truncated"] is True
    assert "new Result" in body["local_interaction_scope"]


def test_every_displayed_figure_carries_a_stable_datum_key():
    rows = [{"clicks": 7, "date": "2026-07-01"}, {"clicks": 9, "date": "2026-07-02"}]
    first = _lens("view", outcome="success", rows=rows)["view"]
    second = _lens("view", outcome="success", rows=rows)["view"]
    keys = [c["datum_key"] for r in first["values"] for c in r["cells"]]
    assert keys == [c["datum_key"] for r in second["values"] for c in r["cells"]]
    assert keys[0] == f"{RESULT_ID}:0:clicks"
    assert len(set(keys)) == len(keys)


def test_view_of_an_unavailable_result_shows_no_values_and_says_why():
    body = _lens("view")["view"]
    assert body["values"] == []
    limitation_codes = {lim["code"] for lim in body["quality_summary"]["limitations"]}
    assert "datastream_output_versions" in limitation_codes


def test_view_of_an_unavailable_result_reports_no_count_at_all_never_zero():
    # The defect this pins: `Rows returned 0` and `Truncated No` rendered for a
    # query that was never asked. Both tiles read as an answer.
    body = _lens("view")["view"]
    assert body["returned_row_count"] is None
    assert body["server_row_count"] is None
    assert body["truncated"] is None
    assert "could not be asked" in body["counts_unavailable_reason"]


def test_a_cell_is_labelled_measure_through_the_manifest_map_not_by_guessing():
    # `measure_ids` holds concept ids (`sc_clicks`); a payload field is a physical
    # column (`clicks`). Testing one against the other is always false, which is
    # how every cell came to be labelled `dimension` with a green suite.
    body = _lens(
        "view",
        outcome="success",
        rows=[{"clicks": 7, "date": "2026-07-01"}],
        manifest=_CAPTURED_MANIFEST,
    )["view"]
    by_field = {cell["field"]: cell["kind"] for cell in body["values"][0]["cells"]}
    assert by_field == {"clicks": "measure", "date": "dimension"}
    assert body["kind_unavailable_reason"] is None


def test_without_the_manifest_map_a_cell_kind_is_unknown_with_a_reason():
    # Not `dimension`. A plausible wrong label is worse than an admitted gap,
    # because Stories 50.4 and 50.6 read this field and would trust it.
    body = _lens(
        "view",
        outcome="success",
        rows=[{"clicks": 7, "date": "2026-07-01"}],
        manifest={"row_limit": 100},
    )["view"]
    assert {cell["kind"] for cell in body["values"][0]["cells"]} == {"unknown"}
    assert "no member-to-column map" in body["kind_unavailable_reason"]


def test_data_of_an_unavailable_result_reports_no_row_cell_or_byte_count():
    body = _lens("data")["data"]
    assert body["row_count"] is None
    assert body["cell_count"] is None
    # Bytes especially: the payload really does occupy bytes, and printing them
    # beside "Rows 0" makes the empty answer look measured.
    assert body["byte_count"] is None
    assert body["truncated"] is None
    assert "could not be asked" in body["counts_unavailable_reason"]


# ---------------------------------------------------------------------------
# AC7 -- Data discloses the exact returned evidence.
# ---------------------------------------------------------------------------


def test_data_types_are_read_from_the_schema_and_unknown_when_absent():
    body = _lens("data", outcome="success", rows=[{"clicks": 7, "date": "2026-07-01"}])["data"]
    assert {f["name"]: f["type"] for f in body["schema"]} == {
        "clicks": "integer",
        "date": "date",
    }


#: A plan that froze what its numbers MEAN. `multi_source_execution.result_schema`
#: writes exactly these two keys beside every measure, and `pivot_projection`
#: reads them to fill the matrix the MCP App draws -- so the lens has to echo the
#: same two, from the same place, or the Console and the App state one amount two
#: ways (amendment 2026-08-15).
_GOVERNED_SCHEMA = {
    "fields": [
        {
            "name": "cost",
            "type": "integer",
            "role": "measure",
            "value_type": "money",
            "unit": "EUR",
        },
        {"name": "date", "type": "date", "role": "dimension"},
    ]
}


def test_data_schema_echoes_what_the_plan_froze_about_each_number():
    # The defect: the Data lens carried the STORAGE type (`integer`) and nothing
    # else, so a reader had to decide for itself that 124000000 was 124 EUR.
    body = _lens(
        "data",
        outcome="success",
        rows=[{"cost": 124_000_000, "date": "2026-07-01"}],
        result_schema=_GOVERNED_SCHEMA,
    )["data"]
    cost = next(f for f in body["schema"] if f["name"] == "cost")
    assert cost["type"] == "integer"
    assert cost["value_type"] == "money"
    assert cost["unit"] == "EUR"


def test_a_result_frozen_before_the_plan_stated_units_acquires_none():
    # Silence is not a currency. An older Result says nothing about units and is
    # read as the plain number it is -- the lens does not backfill from today's
    # vocabulary, and no unit is ever inferred from a column name.
    body = _lens("data", outcome="success", rows=[{"clicks": 7, "date": "2026-07-01"}])["data"]
    clicks = next(f for f in body["schema"] if f["name"] == "clicks")
    assert "value_type" not in clicks
    assert "unit" not in clicks


def test_view_field_semantics_carry_the_governed_value_type_for_the_cell():
    # `field_semantics` is what the Console table reads to print a cell. It
    # carried `unit` and not `value_type`, so the one formatter shared with the
    # MCP App could not divide even when it was called.
    body = _lens(
        "view",
        outcome="success",
        rows=[{"cost": 124_000_000, "date": "2026-07-01"}],
        manifest=_CAPTURED_MANIFEST,
    )["view"]
    assert body["field_semantics"]["clicks"]["value_type"] == "integer"


def test_the_frozen_plan_outranks_the_concept_when_both_state_the_type():
    # The plan is immutable and the pivot reads it; a later edit of the semantic
    # vocabulary must not restate what a frozen Result's numbers meant.
    schema = {
        "fields": [
            {"name": "clicks", "type": "integer", "role": "measure",
             "value_type": "money", "unit": "EUR"},
        ]
    }
    body = _lens(
        "view",
        outcome="success",
        rows=[{"clicks": 124_000_000}],
        manifest=_CAPTURED_MANIFEST,
        result_schema=schema,
    )["view"]
    assert body["field_semantics"]["clicks"]["value_type"] == "money"
    assert body["field_semantics"]["clicks"]["unit"] == "EUR"


def test_data_of_an_empty_result_keeps_an_inspectable_manifest_and_an_explanation():
    body = _lens("data", outcome="empty", manifest={"relation": "gsc_daily", "row_limit": 100})[
        "data"
    ]
    assert body["rows"] == []
    assert body["manifest"]["relation"] == "gsc_daily"
    assert "nothing matched" in body["no_rows_explanation"]


def test_data_of_an_unavailable_result_explains_that_we_could_not_ask():
    body = _lens("data")["data"]
    assert "no published output" in body["no_rows_explanation"]
    assert body["manifest"]["missing_link"] == "datastream_output_versions"


def test_data_carries_the_analytical_controls_and_both_hashes():
    body = _lens("data")["data"]
    context = body["query_context"]
    assert context["grain"] == "day"
    assert context["time"]["as_of"] == "2026-07-31"
    assert context["comparison"] == "previous_period"
    assert body["integrity"]["result_content_hash"] == "h" * 64
    assert body["integrity"]["query_spec_content_hash"] == "s" * 64


# ---------------------------------------------------------------------------
# AC8 -- Definitions pin meaning to the exact version.
# ---------------------------------------------------------------------------


def test_definitions_resolve_the_pinned_member_versions_with_their_owner_links():
    body = _lens("definitions")["definitions"]
    clicks = next(m for m in body["members"] if m["concept_id"] == CLICKS)
    assert clicks["version_id"] == CLICKS_V
    assert clicks["expression"] == {"op": "sum", "field": "clicks"}
    assert clicks["additivity_class"] == "additive"
    assert clicks["resolved"] is True
    assert clicks["owner_ref"]["version_id"] == CLICKS_V
    assert clicks["owner_ref"]["tab"] == "versions"


def test_an_unresolvable_member_version_says_so_and_does_not_show_the_current_one():
    spec = json.loads(json.dumps(_SPEC))
    spec["measures"] = [{"id": CLICKS, "version_id": "scv_retired"}]
    row = list(_result_row())
    row[21] = spec
    body = compose_result_lens(
        _Conn(result_row=tuple(row)),
        org_id="org_EXAMPLE",
        project_id="proj_EXAMPLE",
        result_id=RESULT_ID,
        lens="definitions",
    )["definitions"]
    member = body["members"][0]
    assert member["resolved"] is False
    assert "has NOT been shown in its place" in member["unresolved_reason"]
    assert member["expression"] is None


def test_definitions_carry_the_classification_pins_from_the_spec():
    body = _lens("definitions")["definitions"]
    pin = body["classification_pins"][0]
    assert pin["classification_object_id"] == "mdc_EXAMPLE"
    assert pin["hierarchy_version_id"] == "mdc_EXAMPLE@3"
    assert pin["owner_ref"]["workspace"] == "governance"


def test_definitions_do_not_describe_a_comparison_that_was_never_executed():
    """The lens may not lend the new behaviour's words to an older Result.

    `_SPEC` is a Result written BEFORE period comparison was executed on this
    path: it asks for `previous_period` and carries no frozen windows, because
    nothing froze any. The lens used to answer "the same window immediately
    before this one" for exactly that Result, while its rows covered one window.
    """
    body = _lens("definitions")["definitions"]
    assert body["source_comparison"]["requested"] == "previous_period"
    assert body["source_comparison"]["executed"] is False
    assert "before period comparison was executed" in body["source_comparison"]["meaning"]
    assert "immediately before" not in body["source_comparison"]["meaning"]


def test_definitions_name_the_two_windows_a_comparison_actually_labelled():
    spec = json.loads(json.dumps(_SPEC))
    spec["comparison_windows"] = {
        "kind": "previous_period",
        "period_field": "comparison_period",
        "member_id": DATE,
        "current": {"start": "2026-07-01", "end": "2026-07-31"},
        "baseline": {"start": "2026-05-31", "end": "2026-06-30"},
    }
    row = list(_result_row())
    row[21] = spec
    body = compose_result_lens(
        _Conn(result_row=tuple(row)),
        org_id="org_EXAMPLE",
        project_id="proj_EXAMPLE",
        result_id=RESULT_ID,
        lens="definitions",
    )["definitions"]
    comparison = body["source_comparison"]
    assert comparison["executed"] is True
    assert comparison["current_window"] == {"start": "2026-07-01", "end": "2026-07-31"}
    assert comparison["baseline_window"] == {"start": "2026-05-31", "end": "2026-06-30"}
    assert "2026-05-31" in comparison["meaning"]
    assert "never summed together" in comparison["meaning"]


def test_definitions_describe_nothing_this_path_does_not_execute():
    """The lens carries no reporting boundary and no time zone -- at all.

    Both are refused when a Query Spec is written, so nothing can arrive carrying
    one. Describing a control the read never applies is the defect this repair
    removes, and a key that is present but always null is still a description.
    """
    body = _lens("definitions")["definitions"]
    assert "reporting_boundary" not in body
    assert "timezone" not in body


def test_the_data_lens_does_not_echo_the_refused_time_controls():
    time_context = _lens("data")["data"]["query_context"]["time"]
    # `_SPEC` still carries `timezone: UTC` -- a Result written before the
    # control was refused. The lens must not print it back as if it applied.
    assert "timezone" not in time_context
    assert "reporting_boundary_id" not in time_context
    assert time_context["as_of"] == "2026-07-31"


# ---------------------------------------------------------------------------
# AC9 -- Quality is honest and owner-linked.
# ---------------------------------------------------------------------------


def test_quality_never_reports_an_unavailable_result_as_healthy_or_as_zero():
    body = _lens("quality")["quality"]
    assert body["outcome"] == "unavailable"
    assert body["unavailable_reason"]
    assert body["missing_link"] == "datastream_output_versions"
    # NOT `0`. The stored count is zero because the payload is empty, and a tile
    # reading "Rows 0" for a question nobody could ask says the campaigns
    # produced nothing. `None` forces every consumer to print "Unavailable".
    assert body["completeness"]["row_count"] is None
    assert body["completeness"]["cell_count"] is None
    assert body["completeness"]["truncated"] is None
    assert "could not be asked" in body["completeness"]["counts_unavailable_reason"]
    # Zero rows and "we could not ask" are different facts, and the limitation is
    # what keeps them apart when a screen shows the count.
    assert any(lim["code"] == "datastream_output_versions" for lim in body["limitations"])


def test_a_refused_result_withholds_its_counts_for_the_same_reason():
    body = _lens("quality", outcome="refused", manifest={"refused_reason": "no safe answer"})[
        "quality"
    ]
    assert body["completeness"]["row_count"] is None
    assert "refused" in body["completeness"]["counts_unavailable_reason"]


def test_a_measured_outcome_still_reports_its_counts():
    # The guard must not swallow real measurements: an `empty` Result really did
    # return zero rows, and that zero is an answer.
    body = _lens("quality", outcome="empty", manifest={"row_limit": 100})["quality"]
    assert body["completeness"]["row_count"] == 0
    assert body["completeness"]["truncated"] is False
    assert body["completeness"]["counts_unavailable_reason"] is None


def test_empty_and_unavailable_produce_different_limitations():
    empty = _lens("quality", outcome="empty", manifest={"row_limit": 100})["quality"]
    unavailable = _lens("quality")["quality"]
    assert {lim["code"] for lim in empty["limitations"]} == {"empty"}
    assert "empty" not in {lim["code"] for lim in unavailable["limitations"]}


def test_truncation_is_a_limitation_of_its_own():
    body = _lens("quality", outcome="success", rows=[{"clicks": 1}], truncated=True)["quality"]
    assert "truncated" in {lim["code"] for lim in body["limitations"]}


# ---------------------------------------------------------------------------
# CHANTIER 67-15c -- the combination verdict reaches the ONE route a live screen
# calls. `GET /api/cards` carries it and has zero call sites in `ui/admin/src`;
# the report envelope that carries `metrics_not_combinable` has no REST route at
# all. The Result lens is what the Result workbench actually fetches.
# ---------------------------------------------------------------------------


def _combination(manifest=_CAPTURED_MANIFEST, **kwargs):
    body = _lens("quality", outcome="success", rows=[{"clicks": 1}], manifest=manifest, **kwargs)
    return {e["member_id"]: e for e in body["quality"]["metric_combination"]}


def test_quality_says_how_the_combination_question_was_answered_for_each_measure():
    """MUTATION: drop `metric_combination` from `_lens_quality` -> this goes red.

    Before this key the lens answered nothing at all about the gate, and the only
    REST route that did -- `GET /api/cards` -- was fetched by no screen.
    """
    entry = _combination()[CLICKS]
    # One connector contributed: there was nothing to reconcile, and saying so is
    # NOT the same as saying nobody asked.
    assert entry["check"] == "single_source"
    assert entry["refused"] is None
    assert entry["source_systems"] == ["google-search-console"]
    assert entry["label"] == "Clicks"
    assert entry["unavailable_reason"] is None


def test_a_refused_measure_is_named_on_the_lens_rather_than_reaching_it_as_a_hole():
    """`analyze-and-test.md:617` -- a refused metric must not arrive as an absence."""
    two_sources = json.loads(json.dumps(_CAPTURED_MANIFEST))
    two_sources["provenance"]["values"].append(
        {
            "member_id": CLICKS,
            "source_system": "google-ads",
            "source_field": "clicks",
            "pull_id": "dse_OTHER",
        }
    )

    class _Refusing:
        status = "UNRULED_OVERLAP"

    with patch("core.metric_reconciliation.resolve_route", return_value=_Refusing()):
        entry = _combination(manifest=two_sources)[CLICKS]

    assert entry["refused"] == "UNRULED_OVERLAP"
    assert entry["check"] == "refused"
    assert entry["source_systems"] == ["google-ads", "google-search-console"]


def test_the_gate_is_asked_with_the_governed_name_and_the_observed_connectors():
    """MUTATION: pass the concept id instead of the name -> this goes red.

    `resolve_route` routes on the metric's governed NAME. Asked with `sc_clicks`
    it resolves the cascade for a metric that does not exist and answers a
    permission by vacuity -- the exact failure story 53.2 closed on the mart side.
    """
    two_sources = json.loads(json.dumps(_CAPTURED_MANIFEST))
    two_sources["provenance"]["values"].append(
        {"member_id": CLICKS, "source_system": "google-ads", "source_field": "clicks"}
    )
    with patch("core.metric_reconciliation.resolve_route") as resolve:
        resolve.return_value.status = "DIRECT_SUM"
        _combination(manifest=two_sources)

    assert resolve.call_args.args[1] == "clicks"
    assert resolve.call_args.kwargs["observed_emitters"] == (
        "google-ads",
        "google-search-console",
    )


def test_a_measure_with_no_recorded_source_is_not_reported_as_single_source():
    """The reassuring answer is the one that must not be invented.

    `combination_refusal` answers `single_source` for an empty list, which is right
    for a caller that counted its rows and saw one connector and a LIE for a Result
    whose manifest never recorded one.
    """
    entry = _combination(manifest={"row_limit": 100})[CLICKS]
    assert entry["check"] is None
    assert entry["refused"] is None
    assert entry["source_systems"] == []
    assert "has NOT been read as one source" in entry["unavailable_reason"]


def test_the_lens_asks_the_one_gate_authority_rather_than_re_deriving_it():
    """MUTATION: inline the `_COMBINATION_REFUSED` comparison here -> red.

    Two readings of one gate is how the console and the model channel start
    disagreeing about whether a number was verified.
    """
    with patch("core.rollup.combination_refusal", return_value=(None, "verified")) as authority:
        entry = _combination()[CLICKS]

    assert authority.called
    assert entry["check"] == "verified"


def test_quality_references_dq_evidence_and_never_copies_a_verdict():
    manifest = {"dq_evaluation_ids": ["dqe_EXAMPLE"], "row_limit": 100}
    conn = _Conn(
        result_row=_result_row(outcome="success", rows=[{"clicks": 1}], manifest=manifest),
        dq_rows=[
            ("dqe_EXAMPLE", "dqm_EXAMPLE", "dqmv_EXAMPLE", "failed", AT, AT, AT, 10, 10, 8, 2, 0)
        ],
    )
    body = compose_result_lens(
        conn, org_id="org_EXAMPLE", project_id="proj_EXAMPLE", result_id=RESULT_ID, lens="quality"
    )["quality"]
    evaluation = body["dq_evaluations"][0]
    assert evaluation["outcome"] == "failed"
    assert evaluation["owner_ref"]["section"] == "controls-quality"
    # No re-judgement: the lens carries the owner's outcome and adds no verdict
    # field of its own.
    assert "verdict" not in evaluation


def test_no_referenced_dq_evidence_is_stated_with_its_owner_not_left_blank():
    body = _lens("quality")["quality"]
    assert body["dq_evaluations"] == []
    assert "Story 50.1" in body["dq_unavailable_reason"]


# ---------------------------------------------------------------------------
# AC10 -- Provenance reaches the exact owner chain, and names its gaps.
# ---------------------------------------------------------------------------


#: The six links `analyze-and-test.md:114` contracts, in its order. Written out
#: rather than derived from the response, because the failure this pins is a link
#: that is ABSENT FROM THE LIST -- and a list compared against itself can never
#: catch that.
CONTRACTED_CHAIN = [
    "source",
    "pull",
    "mapping",
    "published_output_relation",
    "semantic_view_version",
    "query_spec_version",
    "result",
]


def test_the_chain_carries_every_contracted_link_even_when_none_is_recorded():
    # The docstring promises exactly two states, `recorded` and `not_recorded`.
    # A missing entry is a silent third one, and it was the state `source`, `pull`
    # and `mapping` were in.
    body = _lens("provenance")["provenance"]
    assert [entry["link"] for entry in body["chain"]] == CONTRACTED_CHAIN
    assert {entry["status"] for entry in body["chain"]} <= {"recorded", "not_recorded"}


def test_provenance_marks_each_link_recorded_or_not_recorded_with_a_reason():
    body = _lens("provenance")["provenance"]
    by_link = {entry["link"]: entry for entry in body["chain"]}
    assert by_link["semantic_view_version"]["status"] == "recorded"
    assert by_link["semantic_view_version"]["owner_ref"]["version_id"] == VIEW_VERSION_ID
    assert by_link["published_output_relation"]["status"] == "not_recorded"
    assert "no published output" in by_link["published_output_relation"]["reason"]


def test_an_unrecorded_source_pull_or_mapping_names_the_module_that_writes_it():
    body = _lens("provenance")["provenance"]
    by_link = {entry["link"]: entry for entry in body["chain"]}
    for link in ("source", "pull", "mapping"):
        assert by_link[link]["status"] == "not_recorded"
        assert by_link[link]["identity"] is None
        assert by_link[link]["owner_ref"] is None
        # Its real owner, by name -- not "unknown", and not a value looked up
        # from today's bindings.
        assert "core.query_execution.capture_evidence" in by_link[link]["reason"]
        assert "NOT been" in by_link[link]["reason"]


def test_a_captured_execution_records_source_pull_and_mapping_with_their_owners():
    body = _lens(
        "provenance",
        outcome="success",
        rows=[{"clicks": 7, "date": "2026-07-01"}],
        manifest=_CAPTURED_MANIFEST,
    )["provenance"]
    by_link = {entry["link"]: entry for entry in body["chain"]}
    assert by_link["source"]["identity"] == "ds_EXAMPLE"
    assert by_link["source"]["owner_ref"]["section"] == "datastreams"
    assert by_link["source"]["owner_ref"]["tab"] == "overview"
    assert by_link["pull"]["identity"] == "dse_EXAMPLE"
    assert by_link["pull"]["owner_ref"]["tab"] == "runs"
    assert by_link["mapping"]["identity"] == "dmap_EXAMPLE"
    assert by_link["mapping"]["owner_ref"]["tab"] == "mapping"
    assert by_link["published_output_relation"]["identity"] == "gsc_daily"
    assert body["source_system"] == "google-search-console"


def test_the_per_value_source_tuple_gap_is_named_with_its_owner_not_fabricated():
    body = _lens("provenance")["provenance"]
    assert body["value_source_tuples"] == []
    gap = body["value_source_unavailable"]
    assert gap["code"] == "manifest_carries_no_source_tuple"
    assert gap["owner"] == "core.query_execution.capture_evidence"


def test_per_value_tuples_are_read_from_the_manifest_when_the_execution_captured_them():
    body = _lens(
        "provenance",
        outcome="success",
        rows=[{"clicks": 7, "date": "2026-07-01"}],
        manifest=_CAPTURED_MANIFEST,
    )["provenance"]
    assert body["value_source_unavailable"] is None
    assert {t["member_id"]: t["source_field"] for t in body["value_source_tuples"]} == {
        CLICKS: "clicks",
        DATE: "date",
    }
    assert {t["pull_id"] for t in body["value_source_tuples"]} == {"dse_EXAMPLE"}


def test_the_retry_chain_reaches_both_directions():
    conn = _Conn(
        result_row=_result_row(predecessor="qr_01PREVIOUS"),
        successor_rows=[("qr_01NEXT", "success", AT)],
    )
    body = compose_result_lens(
        conn,
        org_id="org_EXAMPLE",
        project_id="proj_EXAMPLE",
        result_id=RESULT_ID,
        lens="provenance",
    )["provenance"]
    assert body["predecessor"]["result_id"] == "qr_01PREVIOUS"
    assert body["successors"][0]["result_id"] == "qr_01NEXT"
    assert body["successors"][0]["owner_ref"]["object_type"] == "result"


# ---------------------------------------------------------------------------
# AC11 -- AI Path is exact evidence, or the exact literal.
# ---------------------------------------------------------------------------


def test_a_human_only_result_says_exactly_no_ai_path():
    body = _lens("ai-path")["ai_path"]
    assert body == {
        "schema_version": "observed-ai-path.v1",
        "state": "human_absent",
        "literal": "No AI path",
    }


def test_the_literal_matches_the_owner_module_character_for_character():
    from core.ai_paths import NO_AI_PATH
    from core.query_execution import NO_AI_PATH as EXECUTION_LITERAL

    assert _lens("ai-path")["ai_path"]["literal"] == NO_AI_PATH == EXECUTION_LITERAL


def test_an_ai_assisted_result_reaches_the_immutable_path_and_its_context_hub_owner(monkeypatch):
    import core.ai_paths as ai_paths

    monkeypatch.setattr(
        ai_paths,
        "load_path",
        lambda conn, *, path_id, project_id, **_kwargs: {
            "id": path_id,
            "lifecycle": "finalized",
            "outcome": "succeeded",
            "actor": "agent",
            "model_ref": "model-x",
            "started_at": AT,
            "ended_at": AT,
            "w3c_trace_id": None,
            "policy_snapshot_hash": "p" * 64,
            "assessment": {"verdict": "pass"},
            "steps": [
                {
                    "id": "aps_1",
                    "ordinal": 1,
                    "step_kind": "knowledge_read",
                    "owner_workspace": "context-hub",
                    "owner_object_type": "context-topic",
                    "owner_object_id": "ctx_EXAMPLE",
                    "owner_version_id": "ctxv_EXAMPLE",
                    "outcome": "succeeded",
                    "observed_at": AT,
                }
            ],
        },
    )
    body = _lens("ai-path", ai_path_id="aip_01EXAMPLE")["ai_path"]
    assert set(body) == {"schema_version", "state", "path_id", "lifecycle", "outcome", "steps"}
    assert body["state"] == "completed"
    assert body["path_id"] == "aip_01EXAMPLE"
    assert body["steps"][0]["owner"]["object_id"] == "ctx_EXAMPLE"
    encoded = json.dumps(body, sort_keys=True)
    for forbidden in ("actor", "trace", "policy", "assessment", "model"):
        assert forbidden not in encoded


@pytest.mark.parametrize(
    ("workspace", "object_type"),
    [
        ("context-hub", "context-topic"),
        ("context-hub", "context-procedure"),
        ("context-hub", "skill"),
        ("governance", "semantic-view"),
        ("data", "datastream"),
        ("analyze", "result"),
        ("test", "golden-question"),
    ],
)
def test_a_step_owner_carries_its_raw_triple_and_invents_no_section(
    monkeypatch, workspace, object_type
):
    """Migration 150 permits five owner workspaces; one hardcoded section served one.

    `section: null` is the answer, not `"knowledge-library"`. Which console
    section holds an object type is owned by `shell/navigation.ts`, and a second
    table here would make procedures and skills -- filed under `skills-registry`
    -- unreachable exactly as they were.
    """
    import core.ai_paths as ai_paths

    monkeypatch.setattr(
        ai_paths,
        "load_path",
        lambda conn, *, path_id, project_id, **_kwargs: {
            "id": path_id,
            "lifecycle": "finalized",
            "outcome": "succeeded",
            "actor": "agent",
            "model_ref": None,
            "started_at": AT,
            "ended_at": AT,
            "w3c_trace_id": None,
            "policy_snapshot_hash": None,
            "assessment": None,
            "steps": [
                {
                    "id": "aps_1",
                    "ordinal": 1,
                    "step_kind": "skill_step",
                    "owner_workspace": workspace,
                    "owner_object_type": object_type,
                    "owner_object_id": "obj_EXAMPLE",
                    "owner_version_id": "objv_EXAMPLE",
                    "outcome": "succeeded",
                    "observed_at": AT,
                }
            ],
        },
    )
    owner = _lens("ai-path", ai_path_id="aip_01EXAMPLE")["ai_path"]["steps"][0]["owner"]
    assert owner["workspace"] == workspace
    assert owner["object_type"] == object_type
    assert owner["object_id"] == "obj_EXAMPLE"
    assert owner["version_id"] == "objv_EXAMPLE"


def test_an_unreadable_pinned_path_says_so_and_opens_nothing_else(monkeypatch):
    import core.ai_paths as ai_paths

    def _raise(conn, *, path_id, project_id):
        raise ai_paths.AiPathNotFound("nope")

    monkeypatch.setattr(ai_paths, "load_path", _raise)
    body = _lens("ai-path", ai_path_id="aip_01FOREIGN")["ai_path"]
    assert body == {
        "schema_version": "observed-ai-path.v1",
        "state": "unavailable",
        "reason": "unavailable",
    }


def test_mcp_and_workbench_delegate_the_same_projection_without_remapping(monkeypatch):
    from core import ai_paths, analyze_render_mcp
    from core.analyze_workbench import _lens_ai_path

    canonical = {
        "schema_version": "observed-ai-path.v1",
        "state": "failed",
        "path_id": "aip_EXAMPLE",
        "lifecycle": "finalized",
        "outcome": "failed",
        "steps": [],
    }
    calls = []

    def project(conn, *, project_id, ai_path):
        calls.append((conn, project_id, ai_path))
        return canonical

    monkeypatch.setattr(ai_paths, "project_observed_ai_path", project)
    conn = object()
    assert analyze_render_mcp._load_ai_path_walk(
        conn, project_id="proj_EXAMPLE", ai_path="aip_EXAMPLE"
    ) is canonical
    assert _lens_ai_path(
        conn, {"ai_path_id": "aip_EXAMPLE"}, "proj_EXAMPLE"
    ) is canonical
    assert calls == [
        (conn, "proj_EXAMPLE", "aip_EXAMPLE"),
        (conn, "proj_EXAMPLE", "aip_EXAMPLE"),
    ]


# ---------------------------------------------------------------------------
# The Query Spec version read.
# ---------------------------------------------------------------------------


def test_the_query_spec_version_read_returns_the_immutable_spec_and_its_results():
    conn = _Conn(
        spec_version_row=(
            SPEC_VERSION_ID, SPEC_ID, 1, VIEW_ID, VIEW_VERSION_ID, _SPEC, "s" * 64, None, AT,
            "person-1", "Clicks by day", SPEC_VERSION_ID,
        ),
        result_rows=[(RESULT_ID, "unavailable", 0, False, AT, AT, "h" * 64)],
    )
    body = load_query_spec_version(
        conn,
        org_id="org_EXAMPLE",
        project_id="proj_EXAMPLE",
        query_spec_version_id=SPEC_VERSION_ID,
    )
    assert body["spec"]["measures"] == [{"id": CLICKS, "version_id": CLICKS_V}]
    assert body["is_current_version"] is True
    assert body["results"][0]["owner_ref"]["tab"] == "view"


def test_a_foreign_query_spec_version_raises_the_nondisclosing_error():
    with pytest.raises(WorkbenchNotFound):
        load_query_spec_version(
            _Conn(spec_version_row=None),
            org_id="org_EXAMPLE",
            project_id="proj_EXAMPLE",
            query_spec_version_id=SPEC_VERSION_ID,
        )


# ---------------------------------------------------------------------------
# analyze-and-test.md:424-426 -- the two Analyze surfaces disclose each other
# ---------------------------------------------------------------------------


def test_the_quality_lens_names_the_other_analytical_path():
    """The criterion is incomplete while NEITHER surface discloses the other.

    `core.envelope` has named this path from the mart side since CAV-17. Until
    the constant under test, a person reading a Result in the Console had no way
    to learn that the same question answered in chat reads a different relation
    and may return a different number.
    """
    from core.analyze_workbench import _parallel_path_disclosure
    from core.envelope import ANALYTICAL_PATH_MART

    disclosure = _parallel_path_disclosure()

    assert disclosure["this_path"]["governed_result"] is True
    assert disclosure["other_path"]["governed_result"] is False
    assert disclosure["other_path"]["relation"] == "fact_daily_kpi"
    # Not "the two agree" and not "one supersedes the other": no rule resolves
    # the overlap, and saying so is the whole point.
    assert disclosure["reconciled"] is False
    for side in ("this_path", "other_path"):
        assert "disagree" in disclosure[side]["note"]

    # The mart identity is IMPORTED, not retyped. Two hand-written copies is how
    # a disclosure starts describing a relation that has since moved.
    assert disclosure["other_path"] == dict(ANALYTICAL_PATH_MART)


def test_the_disclosure_does_not_claim_the_paths_are_converged():
    """CAV-17 says converging them is architecture still owned by Epic 50.

    A disclosure that reads as a repair is worse than none: it would close a
    criterion the code does not satisfy. This pins the wording apart.
    """
    from core.analyze_workbench import ANALYTICAL_PATH_GOVERNED

    note = ANALYTICAL_PATH_GOVERNED["note"].lower()
    assert "neither reconciles against the other" in note
    assert "converged" not in note
    assert "reconciled" not in note.replace("neither reconciles", "")
