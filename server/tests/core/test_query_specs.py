"""Story 50.1 -- Query Spec validation proofs.

The fixtures below are NOT invented. `_MATRIX` is the shape of the real
`queryability_matrix` produced by `semantic_compiler` and read out of
`app.semantic_compiled_artifacts` on 2026-07-31: two metrics (Clicks,
Impressions), three dimensions (Date, Country, Page), six proven pairs. Member
ids are shortened for readability, but every key, nesting level and value type is
the compiler's own.

That matters: a test written against an imagined matrix proves the validator
agrees with the test author, not with the compiler.
"""

from __future__ import annotations

import json

import pytest
from core.query_specs import (
    QuerySpecNotFound,
    QuerySpecRefused,
    canonical_hash,
    validate_query_spec,
)
from core.semantic_compiler import COMPILER_VERSION

CLICKS, CLICKS_V = "sc_clicks", "scv_clicks"
IMPRESSIONS, IMPRESSIONS_V = "sc_impressions", "scv_impressions"
DATE, DATE_V = "sc_date", "scv_date"
COUNTRY, COUNTRY_V = "sc_country", "scv_country"
PAGE, PAGE_V = "sc_page", "scv_page"

_MATRIX = {
    "metrics": [
        {"concept_id": CLICKS, "version_id": CLICKS_V, "label": "Clicks"},
        {"concept_id": IMPRESSIONS, "version_id": IMPRESSIONS_V, "label": "Impressions"},
    ],
    "dimensions": [
        {"concept_id": DATE, "version_id": DATE_V, "label": "Date"},
        {"concept_id": COUNTRY, "version_id": COUNTRY_V, "label": "Country"},
        {"concept_id": PAGE, "version_id": PAGE_V, "label": "Page"},
    ],
    "cells": [
        {
            "metric_id": m,
            "metric_version_id": mv,
            "dimension_id": d,
            "dimension_version_id": dv,
            "queryable": True,
            "join_path": [],
        }
        for m, mv in ((CLICKS, CLICKS_V), (IMPRESSIONS, IMPRESSIONS_V))
        for d, dv in ((DATE, DATE_V), (COUNTRY, COUNTRY_V), (PAGE, PAGE_V))
    ],
    "compiler_version": COMPILER_VERSION,
}


#: "the test did not say" -- distinct from a stored version that is empty or NULL,
#: which is exactly the case the staleness guard must refuse.
_CURRENT = object()


class _Cursor:
    """Answers the two SELECTs `_load_pinned_view` issues, in order."""

    def __init__(
        self,
        status: str,
        view_found: bool,
        compiled: bool,
        policy=None,
        compiler_version: str | None = _CURRENT,
    ):
        self._status = status
        self._view_found = view_found
        self._compiled = compiled
        self._policy = policy or {}
        self._compiler_version = (
            COMPILER_VERSION if compiler_version is _CURRENT else compiler_version
        )
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, _params=None):
        self._last = sql

    def fetchone(self):
        if "semantic_view_versions" in self._last:
            if not self._view_found:
                return None
            return ("sv_ver", "sv_view", "proj_EXAMPLE", 1, self._status, self._policy)
        if "semantic_compiled_artifacts" in self._last:
            return (_MATRIX, self._compiler_version) if self._compiled else None
        return None


class _Conn:
    def __init__(
        self,
        status="published",
        view_found=True,
        compiled=True,
        policy=None,
        compiler_version=_CURRENT,
    ):
        self._args = (status, view_found, compiled, policy, compiler_version)

    def cursor(self):
        return _Cursor(*self._args)


def _validate(payload, **conn_kwargs):
    return validate_query_spec(
        _Conn(**conn_kwargs),
        project_id="proj_EXAMPLE",
        semantic_view_id="sv_view",
        semantic_view_version_id="sv_ver",
        payload=payload,
    )


def _codes(exc: QuerySpecRefused) -> set[str]:
    return {r.code for r in exc.refusals}


def test_a_legal_request_validates_and_carries_the_compiler_join_path():
    result = _validate({"measures": [CLICKS], "dimensions": [DATE], "row_limit": 100})
    assert result.spec["measures"] == [{"id": CLICKS, "version_id": CLICKS_V}]
    assert result.spec["dimensions"] == [{"id": DATE, "version_id": DATE_V}]
    # The member versions are resolved from the compiled matrix, not echoed back
    # from the request: that is what pins meaning to the published version.
    assert result.spec["row_limit"] == 100
    assert len(result.join_paths) == 1
    assert result.join_paths[0]["metric_id"] == CLICKS


def test_the_hash_is_stable_across_equivalent_requests_but_not_across_meaning():
    a = _validate({"measures": [CLICKS], "dimensions": [DATE]})
    b = _validate({"dimensions": [DATE], "measures": [{"id": CLICKS}], "comparison": "none"})
    assert a.content_hash == b.content_hash, (
        "key order and default-vs-absent must not change meaning"
    )
    c = _validate({"measures": [CLICKS], "dimensions": [COUNTRY]})
    assert a.content_hash != c.content_hash
    assert a.content_hash == canonical_hash(a.spec)


def test_a_server_policy_may_authorize_waterfall_v1_without_accepting_a_client_descriptor():
    policy = {
        "result_shapes": {
            "waterfall_v1": {
                "components": [{"component": "net", "member_id": CLICKS, "waterfall_role": "base"}]
            }
        }
    }
    result = _validate(
        {
            "measures": [CLICKS],
            "dimensions": [DATE],
            "result_shape": "waterfall_v1",
            "result_shape_descriptor": {"components": [{"member_id": "attacker"}]},
        },
        policy=policy,
    )
    assert result.spec["result_shape"] == "waterfall_v1"
    assert result.spec["result_shape_descriptor"] == policy["result_shapes"]["waterfall_v1"]


def test_result_shape_policy_requires_every_auxiliary_member_to_be_selected():
    policy = {
        "result_shapes": {
            "waterfall_v1": {
                "components": [{"component": "net", "member_id": CLICKS, "waterfall_role": "base"}],
                "currency": {"member_id": "sc_currency"},
                "covered_row_count": {"member_id": "sc_covered"},
                "manifest_pins": {
                    "rule_version_id": {"member_id": "sc_rule_version", "required": True}
                },
            }
        }
    }
    with pytest.raises(QuerySpecRefused) as exc:
        _validate(
            {"measures": [CLICKS], "dimensions": [DATE], "result_shape": "waterfall_v1"},
            policy=policy,
        )
    assert "result_shape_member_not_selected" in _codes(exc.value)
    assert any("sc_currency" in refusal.message for refusal in exc.value.refusals)


def test_required_capability_is_copied_from_policy_and_client_input_is_ignored():
    policy = {"required_capability": {"key": "tax_fees"}}
    result = _validate(
        {
            "measures": [CLICKS],
            "dimensions": [DATE],
            "required_capability": {"key": "attacker_override"},
        },
        policy=policy,
    )
    assert result.spec["required_capability"] == {"key": "tax_fees"}


def test_a_result_shape_the_pinned_semantic_view_did_not_publish_is_refused():
    with pytest.raises(QuerySpecRefused) as exc:
        _validate({"measures": [CLICKS], "dimensions": [DATE], "result_shape": "waterfall_v1"})
    assert "result_shape_not_allowed" in _codes(exc.value)


def test_tabular_v1_remains_the_default_for_existing_query_specs():
    result = _validate({"measures": [CLICKS], "dimensions": [DATE]})
    assert result.spec["result_shape"] == "tabular_v1"
    assert "result_shape_descriptor" not in result.spec


def test_an_unknown_member_is_refused_and_never_repaired():
    with pytest.raises(QuerySpecRefused) as exc:
        _validate({"measures": ["sc_does_not_exist"], "dimensions": [DATE]})
    assert "unknown_member" in _codes(exc.value)
    # The refusal names the subject so a UI can attach it to the right control,
    # and offers NO near-miss suggestion -- accepting one would be the semantic
    # substitution AC3 forbids.
    subjects = {r.subject for r in exc.value.refusals}
    assert "sc_does_not_exist" in subjects
    assert all("did you mean" not in r.message.lower() for r in exc.value.refusals)


def test_a_member_pinned_to_another_version_is_refused_not_silently_advanced():
    with pytest.raises(QuerySpecRefused) as exc:
        _validate(
            {
                "measures": [{"id": CLICKS, "version_id": "scv_from_another_world"}],
                "dimensions": [DATE],
            }
        )
    assert "version_mismatch" in _codes(exc.value)


def test_an_unpublished_view_version_cannot_execute():
    for status in ("draft", "superseded"):
        with pytest.raises(QuerySpecRefused) as exc:
            _validate({"measures": [CLICKS], "dimensions": [DATE]}, status=status)
        assert exc.value.code == "version_not_executable"


def test_a_published_version_without_a_compiled_artifact_refuses_rather_than_guessing():
    with pytest.raises(QuerySpecRefused) as exc:
        _validate({"measures": [CLICKS], "dimensions": [DATE]}, compiled=False)
    assert exc.value.code == "not_compiled"


def test_a_foreign_or_missing_view_is_the_same_non_disclosing_answer():
    with pytest.raises(QuerySpecNotFound):
        _validate({"measures": [CLICKS], "dimensions": [DATE]}, view_found=False)


def test_an_incompatible_pair_is_refused_with_both_members_named():
    matrix_cells = [
        c for c in _MATRIX["cells"] if not (c["metric_id"] == CLICKS and c["dimension_id"] == PAGE)
    ]
    original = _MATRIX["cells"]
    _MATRIX["cells"] = matrix_cells
    try:
        with pytest.raises(QuerySpecRefused) as exc:
            _validate({"measures": [CLICKS], "dimensions": [PAGE]})
        assert "incompatible_pair" in _codes(exc.value)
        assert f"{CLICKS}|{PAGE}" in {r.subject for r in exc.value.refusals}
    finally:
        _MATRIX["cells"] = original


def test_a_request_without_a_measure_is_not_an_analytical_query():
    with pytest.raises(QuerySpecRefused) as exc:
        _validate({"measures": [], "dimensions": [DATE]})
    assert "no_measure" in _codes(exc.value)


def test_every_refusal_is_reported_at_once_not_one_per_round_trip():
    with pytest.raises(QuerySpecRefused) as exc:
        _validate(
            {
                "measures": ["sc_ghost_a"],
                "dimensions": ["sc_ghost_b"],
                "comparison": "sideways",
                "row_limit": 0,
            }
        )
    codes = _codes(exc.value)
    assert {"unknown_member", "unknown_comparison", "invalid_limit"} <= codes
    assert len(exc.value.refusals) >= 4


def test_sorting_by_a_member_that_is_not_selected_is_a_meaning_change_and_is_refused():
    with pytest.raises(QuerySpecRefused) as exc:
        _validate(
            {
                "measures": [CLICKS],
                "dimensions": [DATE],
                "sort": [{"member_id": COUNTRY, "direction": "asc"}],
            }
        )
    assert "sort_not_selected" in _codes(exc.value)


def test_row_limit_above_the_ceiling_is_refused_rather_than_clamped():
    with pytest.raises(QuerySpecRefused) as exc:
        _validate({"measures": [CLICKS], "dimensions": [DATE], "row_limit": 10_000_000})
    assert "limit_exceeded" in _codes(exc.value)


def test_a_filter_operator_and_its_value_must_agree():
    with pytest.raises(QuerySpecRefused) as exc:
        _validate(
            {
                "measures": [CLICKS],
                "dimensions": [DATE],
                "filters": [{"member_id": COUNTRY, "operator": "eq"}],
            }
        )
    assert "value_required" in _codes(exc.value)

    with pytest.raises(QuerySpecRefused) as exc:
        _validate(
            {
                "measures": [CLICKS],
                "dimensions": [DATE],
                "filters": [{"member_id": COUNTRY, "operator": "is_null", "value": "x"}],
            }
        )
    assert "value_not_allowed" in _codes(exc.value)


def test_classification_pins_travel_with_their_filter():
    result = _validate(
        {
            "measures": [CLICKS],
            "dimensions": [DATE],
            "filters": [
                {
                    "member_id": COUNTRY,
                    "operator": "in",
                    "value": ["FR", "MC"],
                    "classification_object_id": "mkt_EXAMPLE",
                    "hierarchy_version_id": "hv_EXAMPLE",
                }
            ],
        }
    )
    stored = result.spec["filters"][0]
    # A classification value only means something under the hierarchy version
    # that defined it, so the pin is part of the hashed intent.
    assert stored["classification_object_id"] == "mkt_EXAMPLE"
    assert stored["hierarchy_version_id"] == "hv_EXAMPLE"


def test_the_spec_carries_no_presentation_field():
    result = _validate({"measures": [CLICKS], "dimensions": [DATE]})
    forbidden = {"chart", "chart_type", "visualization", "layout", "title", "narrative", "render"}
    assert not (forbidden & set(result.spec)), "a Query Spec is not a presentation contract (AC12)"


# ---------------------------------------------------------------------------
# Story 27.8 -- the incommensurability guard, on the surface that assembles a request.
#
# `core.language_dimensions` declares itself "the contract of every aggregation
# surface: call this first". Until 2026-08-01 nothing called it outside its own tests.
# These proofs pin the call HERE, and pin what it is allowed to judge on.
# ---------------------------------------------------------------------------

AUDIENCE_LANG, AUDIENCE_LANG_V = "sc_audience_language", "scv_audience_language"
TARGETING_LANG, TARGETING_LANG_V = "sc_targeting_language", "scv_targeting_language"

#: Same shape as `_MATRIX`, plus the `name` the compiler publishes beside `label`.
#: The two members are DIFFERENT concepts sharing one word -- that is the whole point.
_LANGUAGE_MATRIX = {
    "metrics": [{"concept_id": CLICKS, "version_id": CLICKS_V, "label": "Clicks"}],
    "dimensions": [
        {"concept_id": DATE, "version_id": DATE_V, "name": "date", "label": "Date"},
        {
            "concept_id": AUDIENCE_LANG,
            "version_id": AUDIENCE_LANG_V,
            "name": "audience_language",
            "label": "Langue de l'audience",
        },
        {
            "concept_id": TARGETING_LANG,
            "version_id": TARGETING_LANG_V,
            "name": "targeting_language",
            "label": "Langue ciblee",
        },
    ],
    "cells": [
        {
            "metric_id": CLICKS,
            "metric_version_id": CLICKS_V,
            "dimension_id": d,
            "dimension_version_id": dv,
            "queryable": True,
            "join_path": [],
        }
        for d, dv in (
            (DATE, DATE_V),
            (AUDIENCE_LANG, AUDIENCE_LANG_V),
            (TARGETING_LANG, TARGETING_LANG_V),
        )
    ],
    "compiler_version": COMPILER_VERSION,
}


class _MatrixCursor(_Cursor):
    """`_Cursor`, but answering with a matrix chosen by the test."""

    def __init__(self, matrix, compiler_version=_CURRENT):
        super().__init__("published", True, True, compiler_version=compiler_version)
        self._matrix = matrix

    def fetchone(self):
        if "semantic_compiled_artifacts" in self._last:
            return (self._matrix, self._compiler_version)
        return super().fetchone()


class _MatrixConn:
    def __init__(self, matrix, compiler_version=_CURRENT):
        self._matrix = matrix
        self._compiler_version = compiler_version

    def cursor(self):
        return _MatrixCursor(self._matrix, self._compiler_version)


def _validate_with(matrix, payload, compiler_version=_CURRENT):
    return validate_query_spec(
        _MatrixConn(matrix, compiler_version),
        project_id="proj_EXAMPLE",
        semantic_view_id="sv_view",
        semantic_view_version_id="sv_ver",
        payload=payload,
    )


def test_two_members_of_one_incommensurable_family_cannot_split_the_same_result():
    with pytest.raises(QuerySpecRefused) as exc:
        _validate_with(
            _LANGUAGE_MATRIX,
            {"measures": [CLICKS], "dimensions": [AUDIENCE_LANG, TARGETING_LANG]},
        )
    assert "incommensurable_dimensions" in _codes(exc.value)
    # The refusal must name BOTH members and both natures: a caller told only "no"
    # learns nothing, and this refusal exists to teach that the question was two
    # questions. The wording comes from core.language_dimensions, not from here.
    message = next(r.message for r in exc.value.refusals if r.code == "incommensurable_dimensions")
    assert "audience_language" in message
    assert "targeting_language" in message
    assert "observed_on_person" in message
    assert "declared_intent" in message


def test_one_member_of_the_family_alone_is_perfectly_legal():
    # The guard owns MIXING, not the family. A series of audience languages is
    # homogeneous and summable within itself; refusing it would be the opposite error.
    result = _validate_with(
        _LANGUAGE_MATRIX, {"measures": [CLICKS], "dimensions": [AUDIENCE_LANG, DATE]}
    )
    assert {d["id"] for d in result.spec["dimensions"]} == {AUDIENCE_LANG, DATE}


def test_the_guard_judges_the_stable_name_never_the_renamable_label():
    # 27.9: the LABEL belongs to the client and changes; the NAME does not. A guard
    # reading the label would stop working the day a project renames its dimension.
    renamed = json.loads(json.dumps(_LANGUAGE_MATRIX))
    for dimension in renamed["dimensions"]:
        dimension["label"] = "Langue"  # all three now read identically to a human
    with pytest.raises(QuerySpecRefused) as exc:
        _validate_with(
            renamed, {"measures": [CLICKS], "dimensions": [AUDIENCE_LANG, TARGETING_LANG]}
        )
    assert "incommensurable_dimensions" in _codes(exc.value)


def test_an_artifact_compiled_before_the_name_was_published_does_not_execute_at_all():
    """27.8 repair: the stale artifact is REFUSED, not quietly judged by an absent guard.

    Until 2026-08-22 this asserted the opposite -- that such a request validated -- and
    it was green, because `_dimension_names` abstains when the matrix carries no `name`.
    Abstaining is the honest answer for a pure function handed an incomplete document.
    It is the wrong answer for the one place that decides whether a question runs: the
    two dimensions the guard exists to keep apart went through, on a matrix that simply
    predated the guard. The compiler version is what says so, and it now says it.
    """
    legacy = json.loads(json.dumps(_LANGUAGE_MATRIX))
    for dimension in legacy["dimensions"]:
        dimension.pop("name")
    with pytest.raises(QuerySpecRefused) as exc:
        _validate_with(
            legacy,
            {"measures": [CLICKS], "dimensions": [AUDIENCE_LANG, TARGETING_LANG]},
            compiler_version="semantic-compiler.v1",
        )
    assert "stale_compiled_artifact" in _codes(exc.value)
    # The refusal names the gesture that repairs it, never the internal cause alone.
    assert "publish it again" in str(exc.value)


def test_a_stale_artifact_is_refused_even_when_the_request_itself_is_impeccable():
    """The staleness is a property of the ARTIFACT, not of the question asked of it."""
    with pytest.raises(QuerySpecRefused) as exc:
        _validate_with(
            _LANGUAGE_MATRIX,
            {"measures": [CLICKS], "dimensions": [DATE]},
            compiler_version="semantic-compiler.v1",
        )
    assert "stale_compiled_artifact" in _codes(exc.value)


def test_an_artifact_with_no_recorded_compiler_is_stale_rather_than_trusted():
    with pytest.raises(QuerySpecRefused) as exc:
        _validate_with(
            _LANGUAGE_MATRIX,
            {"measures": [CLICKS], "dimensions": [DATE]},
            compiler_version=None,
        )
    assert "stale_compiled_artifact" in _codes(exc.value)


# ---------------------------------------------------------------------------
# The four governed time controls: executed, or refused at the door.
#
# Until 2026-08-17 all four were accepted, frozen into the immutable Spec and
# described by the Definitions lens, while `build_sql` read only `start` and
# `end`. A control that is accepted and then ignored is a half-truth the product
# does not allow, so each one is now either compiled into the read or refused
# here with the limit named.
# ---------------------------------------------------------------------------


def _window(**overrides):
    payload = {
        "measures": [CLICKS],
        "dimensions": [DATE],
        "comparison": "previous_period",
        "time": {"member_id": DATE, "start": "2026-07-01", "end": "2026-07-31"},
    }
    payload.update(overrides)
    return payload


def test_a_previous_period_comparison_freezes_both_windows_in_the_spec():
    spec = _validate(_window()).spec
    windows = spec["comparison_windows"]
    assert windows["current"] == {"start": "2026-07-01", "end": "2026-07-31"}
    # 31 days, ending the day before the current window opens.
    assert windows["baseline"] == {"start": "2026-05-31", "end": "2026-06-30"}
    assert windows["period_field"] == "comparison_period"


def test_a_previous_year_comparison_freezes_the_same_calendar_window_one_year_back():
    windows = _validate(_window(comparison="previous_year")).spec["comparison_windows"]
    assert windows["baseline"] == {"start": "2025-07-01", "end": "2025-07-31"}


def test_a_comparison_without_a_window_is_refused_rather_than_accepted_and_ignored():
    with pytest.raises(QuerySpecRefused) as exc:
        _validate(_window(time={"member_id": DATE}))
    assert "comparison_window_required" in _codes(exc.value)


def test_a_comparison_on_a_date_the_request_did_not_select_is_refused():
    # The read labels each row from the date column the physical plan resolved,
    # and the plan resolves only selected members. Accepting this would compile a
    # label the execution cannot produce -- and then drop it in silence.
    with pytest.raises(QuerySpecRefused) as exc:
        _validate(_window(dimensions=[COUNTRY]))
    assert "comparison_time_member_not_selected" in _codes(exc.value)


def test_each_comparison_precondition_refuses_with_the_sentence_it_declares():
    """Story 67.9 -- one refusal, one sentence, and the screen reads that one.

    The three refusals are asserted by MESSAGE and not only by code, because the
    Explore door disables the comparison control with these exact sentences,
    served from `COMPARISON_PRECONDITIONS`. A code test would stay green while
    the door and the screen drifted apart word by word -- which is what happened.
    """
    from core.query_specs import COMPARISON_PRECONDITIONS

    unmet = {
        # No time member at all.
        "time_member_required": _window(time={}),
        # A time member the request did not select as a dimension.
        "time_member_not_selected": _window(dimensions=[COUNTRY]),
        # A selected time member, but no From/To to compare.
        "window_required": _window(time={"member_id": DATE}),
    }
    assert set(unmet) == {p.condition for p in COMPARISON_PRECONDITIONS}

    for precondition in COMPARISON_PRECONDITIONS:
        with pytest.raises(QuerySpecRefused) as exc:
            _validate(unmet[precondition.condition])
        spoken = {r.message for r in exc.value.refusals if r.code == precondition.code}
        assert precondition.message in spoken, precondition.condition


def test_a_comparison_whose_windows_would_overlap_is_refused():
    with pytest.raises(QuerySpecRefused) as exc:
        _validate(
            _window(
                comparison="previous_year",
                time={"member_id": DATE, "start": "2024-01-01", "end": "2026-01-01"},
            )
        )
    assert "comparison_windows_overlap" in _codes(exc.value)


def test_no_comparison_freezes_no_window_at_all():
    spec = _validate({"measures": [CLICKS], "dimensions": [DATE]}).spec
    assert spec["comparison"] == "none"
    assert "comparison_windows" not in spec


def test_an_as_of_that_is_not_a_date_is_refused_before_it_reaches_the_warehouse():
    with pytest.raises(QuerySpecRefused) as exc:
        _validate(
            {
                "measures": [CLICKS],
                "dimensions": [DATE],
                "time": {"member_id": DATE, "as_of": "last tuesday"},
            }
        )
    assert "invalid_as_of" in _codes(exc.value)


def test_an_as_of_date_is_kept_exactly_as_it_was_asked_for():
    spec = _validate(
        {
            "measures": [CLICKS],
            "dimensions": [DATE],
            "time": {"member_id": DATE, "as_of": "2026-07-15"},
        }
    ).spec
    assert spec["time"]["as_of"] == "2026-07-15"


def test_a_time_zone_is_refused_because_this_path_never_applies_one():
    with pytest.raises(QuerySpecRefused) as exc:
        _validate(
            {
                "measures": [CLICKS],
                "dimensions": [DATE],
                "time": {"member_id": DATE, "timezone": "Europe/Paris"},
            }
        )
    assert "timezone_not_executed" in _codes(exc.value)
    message = next(r.message for r in exc.value.refusals if r.code == "timezone_not_executed")
    assert "governed grains are dates" in message


def test_a_reporting_boundary_is_refused_because_none_is_executed_on_this_path():
    with pytest.raises(QuerySpecRefused) as exc:
        _validate(
            {
                "measures": [CLICKS],
                "dimensions": [DATE],
                "time": {"member_id": DATE, "reporting_boundary_id": "rb_EXAMPLE"},
            }
        )
    assert "reporting_boundary_not_executed" in _codes(exc.value)


def test_a_spec_can_no_longer_freeze_a_time_zone_or_a_reporting_boundary():
    """Freezing an unexecuted control is what made the Definitions lens lie.

    The keys survive so the canonical serialization -- and every `content_hash`
    already written under `query-spec.v1` -- keeps its shape; the values cannot.
    """
    spec = _validate({"measures": [CLICKS], "dimensions": [DATE]}).spec
    assert spec["time"]["timezone"] is None
    assert spec["time"]["reporting_boundary_id"] is None
