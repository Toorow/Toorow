"""Story 50.6 (AC4, AC9) -- the model-visible payload budget, proved rather than declared.

The repository already MEASURED this number and threw it away: three tool sites
computed `payload_bytes` and passed it to `log_tool_metrics`. These tests exist to
prove the measurement is now a gate -- that a tool cannot return over budget, that
the refusal names both numbers, and that the channel split is a split (nothing
lost, remainder stated) rather than a silent trim.
"""

from __future__ import annotations

import json

import pytest
from core.model_channel import (
    APP_PAYLOAD_META_KEY,
    MODEL_CHANNEL_MAX_BYTES,
    MODEL_CHANNEL_MAX_TEXT_LINES,
    ModelChannelEvidenceMissing,
    ModelChannelOverBudget,
    app_meta,
    bounded_head,
    enforce_model_channel,
    partition_envelope,
    serialized_bytes,
)

# ---------------------------------------------------------------------------
# bounded_head -- the remainder is the point.
# ---------------------------------------------------------------------------


def test_bounded_head_always_reports_the_remainder():
    head, withheld = bounded_head(list(range(100)), 12)
    assert head == list(range(12))
    # A caller that only wanted a head could slice. What a bounded list owes its
    # reader is the size of what it is NOT showing.
    assert withheld == 88


def test_bounded_head_reports_zero_withheld_when_it_shows_everything():
    head, withheld = bounded_head(["a", "b"], 12)
    assert head == ["a", "b"]
    assert withheld == 0


def test_bounded_head_tolerates_none():
    assert bounded_head(None, 5) == ([], 0)


# ---------------------------------------------------------------------------
# The refusal names the tool and BOTH numbers.
# ---------------------------------------------------------------------------


def test_over_budget_refusal_names_the_tool_and_both_numbers():
    oversized = {"data": {"blob": "x" * (MODEL_CHANNEL_MAX_BYTES * 2)}}
    with pytest.raises(ModelChannelOverBudget) as excinfo:
        enforce_model_channel("some_tool", "one line", oversized)
    detail = excinfo.value.as_dict()
    assert detail["code"] == "payload_over_budget"
    assert detail["tool"] == "some_tool"
    assert detail["budget"] == MODEL_CHANNEL_MAX_BYTES
    assert detail["measured"] == serialized_bytes(oversized)
    # "too big" without the measurement is not actionable.
    assert str(detail["measured"]) in detail["message"]
    assert str(detail["budget"]) in detail["message"]


def test_the_text_channel_is_capped_too():
    text = "\n".join(f"line {i}" for i in range(MODEL_CHANNEL_MAX_TEXT_LINES + 5))
    with pytest.raises(ModelChannelOverBudget) as excinfo:
        enforce_model_channel("some_tool", text, {"ok": True})
    assert excinfo.value.unit == "text lines"
    assert excinfo.value.budget == MODEL_CHANNEL_MAX_TEXT_LINES


def test_a_payload_inside_the_budget_passes():
    enforce_model_channel("some_tool", "one line", {"schema_version": 1, "summary": {}})


def test_it_refuses_rather_than_truncating():
    """A trimmed payload reads as a complete answer. That is the failure mode."""
    oversized = {"rows": [{"k": "v" * 100} for _ in range(200)]}
    with pytest.raises(ModelChannelOverBudget):
        enforce_model_channel("some_tool", "x", oversized)
    # Nothing was mutated on the way out: the guard raises, it does not "fix".
    assert len(oversized["rows"]) == 200


# ---------------------------------------------------------------------------
# AC9 -- never deep-link-only.
# ---------------------------------------------------------------------------


def test_a_deep_link_without_evidence_is_refused():
    payload = {"schema_version": 1, "deep_link": {"kind": "console"}, "evidence": []}
    with pytest.raises(ModelChannelEvidenceMissing) as excinfo:
        enforce_model_channel("analyze_result", "x", payload)
    assert excinfo.value.as_dict()["code"] == "evidence_required_with_deep_link"


def test_a_deep_link_with_evidence_is_accepted():
    payload = {
        "schema_version": 1,
        "deep_link": {"kind": "console"},
        "evidence": [{"column": "clicks", "first_value": 3}],
    }
    enforce_model_channel("analyze_result", "x", payload)


def test_evidence_alone_without_a_deep_link_is_fine():
    enforce_model_channel("analyze_result", "x", {"evidence": [{"column": "c"}]})


# ---------------------------------------------------------------------------
# The channel split -- nothing lost, remainder stated.
# ---------------------------------------------------------------------------


def _legacy_envelope(n_rows: int) -> dict:
    return {
        "schema_version": "1",
        "meta": {"freshness": {"last_pull": None}, "provenance": [], "alerts": []},
        "data": {
            "report_profile": "standard_daily",
            "date_range": {"start": "2026-07-01", "end": "2026-07-31"},
            "connectors": ["example-connector"],
            "metrics": {"clicks": 120},
            "rows": [{"day": f"2026-07-{i:02d}", "clicks": i} for i in range(1, n_rows + 1)],
        },
    }


def test_the_dataset_leaves_the_model_channel_whole_and_states_what_it_left():
    envelope = _legacy_envelope(300)
    model, payload = partition_envelope(envelope, tool_name="get_daily_report")

    # Model side: a descriptor, not rows, and it says how many there were.
    descriptor = model["data"]["rows"]
    assert descriptor["withheld"] == "moved_to_app_channel"
    assert descriptor["row_count"] == 300
    assert "day" in descriptor["columns"]
    # App side: the dataset, whole. Nothing was discarded.
    assert payload["rows"] == envelope["data"]["rows"]
    assert payload["__tool__"] == "get_daily_report"


def test_the_split_brings_a_legacy_envelope_inside_the_budget():
    model, _payload = partition_envelope(_legacy_envelope(1000), tool_name="get_daily_report")
    measured = serialized_bytes(model)
    assert measured <= MODEL_CHANNEL_MAX_BYTES, measured
    # And it passes the gate for real, not just the arithmetic.
    enforce_model_channel("get_daily_report", "one line", model)


def test_freshness_and_provenance_stay_model_visible():
    """Moving them to save bytes would make every summary less honest."""
    model, _ = partition_envelope(_legacy_envelope(500), tool_name="get_daily_report")
    assert model["meta"]["freshness"] == {"last_pull": None}
    assert "alerts" in model["meta"]


def test_headline_figures_stay_model_visible():
    model, _ = partition_envelope(_legacy_envelope(500), tool_name="get_daily_report")
    assert model["data"]["metrics"] == {"clicks": 120}
    assert model["data"]["connectors"] == ["example-connector"]


def test_a_long_list_in_data_that_fits_the_budget_is_left_alone():
    """The shared guard bounds by BYTES. Bounding by COUNT is the tool's job.

    This assertion is INVERTED from what it said before the guard became
    catalog-wide, and the inversion is the point. `partition_envelope` used to
    route any list of more than twelve entries out of `data` by shape alone. That
    is defensible for one tool whose long list is warehouse rows and wrong for the
    catalog: applied to every tool, it relocated `search_context`'s own hits --
    the answer the caller asked for -- into a channel the model cannot read.

    Only the producing tool knows whether its long list is data or an answer, so
    a semantically unbounded list is bounded at the SOURCE, with a stated
    remainder (`daily_insights_tools` and `list_card_templates` both do this). The
    shared guard's job is the budget, and a list inside the budget is inside the
    budget.
    """
    envelope = {
        "schema_version": "1",
        "meta": {},
        "data": {"metrics": [f"m{i}" for i in range(200)]},
    }
    model, payload = partition_envelope(envelope, tool_name="get_card_capabilities")
    assert serialized_bytes(model) <= MODEL_CHANNEL_MAX_BYTES
    assert model["data"]["metrics"] == envelope["data"]["metrics"]
    assert payload == {}


def test_a_long_list_that_breaks_the_budget_is_routed_with_a_stated_descriptor():
    """Over budget, it leaves -- and says so. Nothing is ever silently trimmed."""
    envelope = {
        "schema_version": "1",
        "meta": {},
        "data": {"metrics": [f"metric_number_{i:04d}" for i in range(600)]},
    }
    assert serialized_bytes(envelope) > MODEL_CHANNEL_MAX_BYTES
    model, payload = partition_envelope(envelope, tool_name="get_card_capabilities")
    assert serialized_bytes(model) <= MODEL_CHANNEL_MAX_BYTES
    assert model["data"]["metrics"]["withheld"] == "moved_to_app_channel"
    assert model["data"]["metrics"]["bytes"] > MODEL_CHANNEL_MAX_BYTES
    assert payload["metrics"] == envelope["data"]["metrics"]


def test_a_per_entity_collection_in_meta_still_keeps_a_bounded_head():
    """`meta` keeps the opposite rule, and it is not an oversight.

    `provenance` is one entry per contributing connector and it is what a model
    cites to qualify an answer. Twelve entries plus a stated remainder is more
    useful than a descriptor saying "37 entries, withheld", so the honesty block
    is bounded by count even when it would fit.
    """
    envelope = {
        "schema_version": "1",
        "meta": {"provenance": [f"connector_{i}" for i in range(37)]},
        "data": {},
    }
    model, payload = partition_envelope(envelope, tool_name="get_daily_report")
    descriptor = model["meta"]["provenance"]
    assert len(descriptor["head"]) == 12
    assert descriptor["item_count"] == 37
    assert descriptor["items_withheld"] == 25
    assert payload["meta.provenance"] == envelope["meta"]["provenance"]


def test_a_stubborn_subtree_is_routed_largest_first_and_deterministically():
    envelope = {
        "schema_version": "1",
        "meta": {},
        "data": {
            "small": "x",
            "huge": {"blob": "y" * 20000},
        },
    }
    model, payload = partition_envelope(envelope, tool_name="t")
    assert model["data"]["small"] == "x"
    assert model["data"]["huge"]["withheld"] == "moved_to_app_channel"
    assert payload["huge"] == envelope["data"]["huge"]
    assert serialized_bytes(model) <= MODEL_CHANNEL_MAX_BYTES
    # Deterministic: the same envelope splits the same way every time.
    again, _ = partition_envelope(envelope, tool_name="t")
    assert json.dumps(again, sort_keys=True) == json.dumps(model, sort_keys=True)


def test_the_app_payload_travels_under_exactly_one_namespaced_key():
    _model, payload = partition_envelope(_legacy_envelope(5), tool_name="get_card")
    meta = app_meta(payload)
    assert list(meta) == [APP_PAYLOAD_META_KEY]


def test_no_app_payload_means_no_meta_at_all():
    """An envelope with nothing to route must not grow an empty `_meta`."""
    model, payload = partition_envelope(
        {"schema_version": "1", "meta": {}, "data": {"a": 1}}, tool_name="t"
    )
    assert payload == {}
    assert app_meta(payload) is None
    assert model["data"] == {"a": 1}


# ---------------------------------------------------------------------------
# The `meta` walk -- honesty fields stay, per-entity collections get a head.
# ---------------------------------------------------------------------------


def _wide_meta_envelope(n_connectors: int) -> dict:
    connectors = [f"connector-{i:02d}" for i in range(n_connectors)]
    return {
        "schema_version": "1",
        "meta": {
            "freshness": {
                "last_pull": "2026-01-30T00:00:00",
                "complete_through": "2026-01-28T00:00:00",
                "stale_since": None,
                "stale_since_evaluated": False,
                "per_connector": {c: "2026-01-30T00:00:00" for c in connectors},
            },
            "provenance": [
                {"source_system": c, "source_field": "fact_daily_kpi", "pull_id": f"pull_{c}"}
                for c in connectors
            ],
            "alerts": [],
        },
        "data": {"rows": [{"day": "2026-01-01", "clicks": 1}]},
    }


def test_the_scalar_honesty_fields_are_never_moved():
    """A model must be able to cite freshness when it qualifies an answer."""
    model, _ = partition_envelope(_wide_meta_envelope(37), tool_name="get_daily_report")
    freshness = model["meta"]["freshness"]
    assert freshness["last_pull"] == "2026-01-30T00:00:00"
    assert freshness["complete_through"] == "2026-01-28T00:00:00"
    assert freshness["stale_since_evaluated"] is False
    assert model["meta"]["alerts"] == []


def test_provenance_keeps_a_bounded_head_rather_than_disappearing():
    """A descriptor saying "37 entries, withheld" is less honest than showing twelve."""
    model, payload = partition_envelope(_wide_meta_envelope(37), tool_name="get_daily_report")
    provenance = model["meta"]["provenance"]
    assert len(provenance["head"]) == 12
    assert provenance["item_count"] == 37
    assert provenance["items_withheld"] == 25
    assert provenance["head"][0]["source_system"] == "connector-00"
    assert len(payload["meta.provenance"]) == 37


def test_a_per_entity_mapping_is_bounded_like_a_list():
    """`freshness.per_connector` is an unbounded list wearing a dict."""
    model, payload = partition_envelope(_wide_meta_envelope(37), tool_name="get_daily_report")
    per_connector = model["meta"]["freshness"]["per_connector"]
    assert len(per_connector["head"]) == 12
    assert per_connector["key_count"] == 37
    assert per_connector["keys_withheld"] == 25
    assert len(payload["meta.freshness.per_connector"]) == 37


def test_a_small_provenance_stays_whole_and_untouched():
    """Bounding must not fire on the ordinary one-or-two-connector case."""
    model, payload = partition_envelope(_wide_meta_envelope(2), tool_name="get_daily_report")
    assert isinstance(model["meta"]["provenance"], list)
    assert len(model["meta"]["provenance"]) == 2
    assert "meta.provenance" not in payload


def test_the_worst_case_37_connector_envelope_fits_the_budget():
    model, _ = partition_envelope(_wide_meta_envelope(37), tool_name="get_daily_report")
    measured = serialized_bytes(model)
    assert measured <= MODEL_CHANNEL_MAX_BYTES, measured


def test_the_data_walk_leaves_no_rows_behind_even_though_meta_keeps_a_head():
    """The two walks differ on purpose: a dataset leaves nothing, provenance leaves twelve."""
    model, _ = partition_envelope(_legacy_envelope(500), tool_name="get_daily_report")
    descriptor = model["data"]["rows"]
    assert "head" not in descriptor
    assert descriptor["row_count"] == 500
