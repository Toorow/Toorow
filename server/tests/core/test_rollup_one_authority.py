"""One rollup authority, and a gate that is actually traversed (story 53.2).

CAV-02 and CAV-03 were both recorded `repaired` while the repair reached one of the
two modules that aggregate and one of the six paths that publish a total.

* CAV-03 was closed in `rollup.py` (`552ffe1`). `reports._rollup` kept
  `sums[metric] / counts[metric]` -- the unweighted mean over day x connector -- and
  it is `reports._rollup` that fills the report envelope's `data["metrics"]`. On the
  commit's own example (two days of `ctr`, 5/10 then 10/1000) the repaired module
  returned 0.01485 and the unrepaired one published **0.255**, the exact number
  `test_rollup.py` forbids.
* CAV-02 was closed in `cards.py`. Deleting the wiring cost nothing: the two tests
  that named it called the helper and the parameter separately, never the path that
  joins them.

Every guard here is proven by mutation and the mutation is written beside it.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import cards as cards_module  # noqa: E402
from core import reports as reports_module  # noqa: E402
from core import rollup as rollup_module  # noqa: E402

_PROJECT = "proj_EXAMPLE"


# ---------------------------------------------------------------------------
# Fixtures -- the commit's own counter-example, and a two-source window.
# ---------------------------------------------------------------------------


def _two_days_of_ctr() -> list[dict]:
    """5 clicks / 10 impressions, then 10 / 1000.

    Period CTR = 15 / 1010 = 0.014851... The mean of the two daily ratios is
    (0.5 + 0.01) / 2 = 0.255, seventeen times larger and wearing the same unit.
    """
    return [
        {
            "date": "2026-07-12", "connector": "google-ads", "metric": "ctr",
            "breakdown_dimension": "date", "breakdown_value": "2026-07-12",
            "value": 0.5, "semantic_numerator": 5, "semantic_denominator": 10,
            "pull_id": "pull_one", "loaded_at": "2026-07-12T06:00:00",
        },
        {
            "date": "2026-07-13", "connector": "google-ads", "metric": "ctr",
            "breakdown_dimension": "date", "breakdown_value": "2026-07-13",
            "value": 0.01, "semantic_numerator": 10, "semantic_denominator": 1000,
            "pull_id": "pull_one", "loaded_at": "2026-07-13T06:00:00",
        },
    ]


def _two_sources_of_cost() -> list[dict]:
    """The same metric from two connectors on the same days.

    `cost` rather than `conversions`: the card path runs `apply_conversions_dedup`
    first, which reduces conversions to a single source and would hide the very
    overlap under test.
    """
    rows = []
    for connector, amount in (("google-ads", 100.0), ("meta-ads", 40.0)):
        for day in ("2026-07-12", "2026-07-13"):
            rows.append(
                {
                    "date": day, "connector": connector, "metric": "cost",
                    "breakdown_dimension": "date", "breakdown_value": day,
                    "value": amount, "pull_id": f"pull_{connector}",
                    "loaded_at": f"{day}T06:00:00",
                }
            )
    return rows


class _FakeModule:
    def __init__(self, name, manifest, reports):
        self.name = name
        self.manifest = manifest
        self.reports = reports


_CTR_REPORT = {
    "id": "ctr_daily",
    "display_name": "Daily click-through rate",
    "metrics": ["ctr"],
    "dimensions": ["date"],
    "layout": {"chart_type": "line", "order_by": "date"},
    "narrative_prompt": "Report on click-through rate.",
    "date_window": {"default_days": 30},
}

_COST_REPORT = {
    "id": "cost_daily",
    "display_name": "Daily cost",
    "metrics": ["cost"],
    "dimensions": ["date"],
    "layout": {"chart_type": "line", "order_by": "date"},
    "narrative_prompt": "Report on cost.",
    "date_window": {"default_days": 30},
}


def _modules():
    return [
        _FakeModule(
            "google-analytics",
            {"widget_ref": "ui://core/daily-report"},
            [_CTR_REPORT, _COST_REPORT],
        )
    ]


def _render(report_id, rows, project_id=_PROJECT):
    """Render a report with the warehouse and the Governance reads stubbed out."""
    with (
        patch("core.warehouse.query_report", return_value=rows),
        patch("core.reports._load_geography_projection", return_value=None),
    ):
        return reports_module.render_report(
            _modules(), project_id, f"google-analytics/{report_id}",
            "2026-07-12", "2026-07-13",
        )


# ---------------------------------------------------------------------------
# 1. The two authorities agree, because there is only one.
# ---------------------------------------------------------------------------


def test_the_two_rollups_return_the_same_number_for_the_same_rows():
    """MUTATION: restore `sums[m] / counts[m]` in `reports._rollup` -> red here."""
    rows = _two_days_of_ctr()

    values, refused = reports_module._rollup(
        rows, _CTR_REPORT, date_from="2026-07-12", date_to="2026-07-13"
    )
    canonical = rollup_module.compute_rollup(
        rows, ["ctr"], "2026-07-12", "2026-07-13", "", []
    )

    assert refused == {}
    assert values["ctr"] == pytest.approx(canonical["ctr"]["value"])


def test_the_report_publishes_the_period_ratio_not_the_mean_of_daily_ratios():
    """MUTATION: same. The forbidden number is 0.255, and it was published."""
    values, _ = reports_module._rollup(
        _two_days_of_ctr(), _CTR_REPORT, date_from="2026-07-12", date_to="2026-07-13"
    )

    assert values["ctr"] == pytest.approx(15 / 1010)
    assert values["ctr"] != pytest.approx(0.255)


def test_the_rendered_report_envelope_carries_the_period_ratio():
    """The whole path, not the helper: `render_report` -> `data["metrics"]`."""
    _summary, envelope, _uri = _render("ctr_daily", _two_days_of_ctr())

    assert envelope["data"]["metrics"]["ctr"] == pytest.approx(15 / 1010)


# ---------------------------------------------------------------------------
# 2. The gate is traversed -- by the card path AND by the report path.
# ---------------------------------------------------------------------------


def _refusing_route(*_a, **_k):
    return type("D", (), {"status": "UNRULED_OVERLAP"})()


def test_the_card_path_traverses_the_gate_end_to_end():
    """MUTATION: drop `route_resolver=` at the `cards.py` compute_rollup call -> red.

    This is the test the story was missing. Before it, that deletion left
    `tests/core/test_cards_api.py` + `tests/core/test_rollup.py` at 56 passed.
    """
    with (
        patch("core.warehouse.query_daily_report", return_value=_two_sources_of_cost()),
        patch("core.metric_reconciliation.resolve_route", side_effect=_refusing_route),
    ):
        _s, envelope, _u = cards_module.get_card(
            [], _PROJECT, metrics=["cost"],
            date_from="2026-07-12", date_to="2026-07-13",
        )

    entry = envelope["data"]["metrics"]["cost"]
    assert entry["value"] is None, "the card published a total the gate refused"
    assert entry["combination_refused"] == "UNRULED_OVERLAP"
    assert entry["per_source"] == {"google-ads": 200.0, "meta-ads": 80.0}


def test_the_report_path_traverses_the_gate_end_to_end():
    """MUTATION: drop `route_resolver=` at either `reports.py` call -> red."""
    with patch("core.metric_reconciliation.resolve_route", side_effect=_refusing_route):
        summary, envelope, _u = _render("cost_daily", _two_sources_of_cost())

    assert "cost" not in envelope["data"]["metrics"], "280.0 was published unchecked"
    refused = envelope["data"]["metrics_not_combinable"]["cost"]
    assert refused["status"] == "UNRULED_OVERLAP"
    assert refused["per_source"] == {"google-ads": 200.0, "meta-ads": 80.0}
    # And the text channel says it too, rather than quietly dropping the line.
    assert "UNRULED_OVERLAP" in summary
    assert "280" not in summary


def test_a_refused_metric_is_named_rather_than_silently_absent():
    """Dropping the metric would trade a wrong number for an unexplained hole."""
    with patch("core.metric_reconciliation.resolve_route", side_effect=_refusing_route):
        _s, envelope, _u = _render("cost_daily", _two_sources_of_cost())

    assert "metrics_not_combinable" in envelope["data"]
    assert envelope["data"]["metrics_not_combinable"]["cost"]["source_systems"] == [
        "google-ads", "meta-ads",
    ]


def test_nothing_refused_means_no_second_map_at_all():
    _s, envelope, _u = _render("ctr_daily", _two_days_of_ctr())

    assert "metrics_not_combinable" not in envelope["data"]


# ---------------------------------------------------------------------------
# 3. "Nobody asked" is no longer spelled the same way as "asked, and allowed".
# ---------------------------------------------------------------------------


def test_a_total_records_how_the_combination_question_was_answered():
    """MUTATION: delete the `combination_check` key from `compute_rollup` -> 4 red."""
    rows = _two_sources_of_cost()

    unasked = rollup_module.compute_rollup(
        rows, ["cost"], "2026-07-12", "2026-07-13", _PROJECT, []
    )["cost"]
    assert unasked["value"] == 280.0
    assert unasked["combination_check"] == rollup_module.CHECK_NOT_REQUESTED

    verified = rollup_module.compute_rollup(
        rows, ["cost"], "2026-07-12", "2026-07-13", _PROJECT, [],
        route_resolver=lambda _m, _s=(): "DIRECT_SUM",
    )["cost"]
    assert verified["value"] == 280.0
    assert verified["combination_check"] == rollup_module.CHECK_VERIFIED

    def _boom(_metric, _sources=()):
        raise RuntimeError("reconciliation store unavailable")

    unavailable = rollup_module.compute_rollup(
        rows, ["cost"], "2026-07-12", "2026-07-13", _PROJECT, [],
        route_resolver=_boom,
    )["cost"]
    assert unavailable["value"] == 280.0, "an outage must not take the report down"
    assert unavailable["combination_check"] == rollup_module.CHECK_UNAVAILABLE

    single = rollup_module.compute_rollup(
        [r for r in rows if r["connector"] == "google-ads"],
        ["cost"], "2026-07-12", "2026-07-13", _PROJECT, [],
        route_resolver=lambda _m, _s=(): "UNRULED_OVERLAP",
    )["cost"]
    assert single["value"] == 200.0
    assert single["combination_check"] == rollup_module.CHECK_SINGLE_SOURCE


def test_the_observed_sources_travel_with_the_question():
    """The gate answers about the connectors that produced rows, not a manifest."""
    seen = {}

    def _resolver(metric, sources=()):
        seen[metric] = sources
        return "DIRECT_SUM"

    rollup_module.compute_rollup(
        _two_sources_of_cost(), ["cost"], "2026-07-12", "2026-07-13", _PROJECT, [],
        route_resolver=_resolver,
    )

    assert seen["cost"] == ("google-ads", "meta-ads")


# ---------------------------------------------------------------------------
# 4. The gate stops saying yes by vacuity.
# ---------------------------------------------------------------------------


def test_an_unreadable_emitter_registry_is_not_a_permission():
    """MUTATION: drop the `observed_emitters` union in `resolve_route` -> red.

    `emitters_of` swallows every exception and returns `()`. With zero emitters
    `_no_rule_decision` fell to DIRECT_SUM -- a permission -- while the caller had
    just counted two connectors in the rows.
    """
    from core import metric_reconciliation as mr

    silent_registry = mr.resolve_route(
        _PROJECT, "cost",
        rule_resolver=lambda _p, _m: None,
        definition_resolver=lambda _p, _m: {"additive": True},
        emitters_source=lambda _m: (),
    )
    assert silent_registry.status == mr.RouteStatus.DIRECT_SUM  # the old, vacuous yes

    with_evidence = mr.resolve_route(
        _PROJECT, "cost",
        rule_resolver=lambda _p, _m: None,
        definition_resolver=lambda _p, _m: {"additive": True},
        emitters_source=lambda _m: (),
        observed_emitters=("google-ads", "meta-ads"),
    )
    assert with_evidence.status == mr.RouteStatus.UNRULED_OVERLAP
    assert with_evidence.reason.emitters == ("google-ads", "meta-ads")


def test_the_bound_resolver_carries_the_observed_sources_to_the_gate():
    from core import metric_reconciliation as mr

    captured = {}

    def _spy(project_id, metric, **kwargs):
        captured["project_id"] = project_id
        captured["observed"] = kwargs.get("observed_emitters")
        return type("D", (), {"status": "DIRECT_SUM"})()

    resolver = mr.route_status_resolver(_PROJECT)
    with patch("core.metric_reconciliation.resolve_route", side_effect=_spy):
        assert resolver("cost", ("google-ads", "meta-ads")) == "DIRECT_SUM"

    assert captured["project_id"] == _PROJECT
    assert captured["observed"] == ("google-ads", "meta-ads")


# ---------------------------------------------------------------------------
# 5. The report path knows which Project it is rendering.
# ---------------------------------------------------------------------------


def test_the_report_rollup_reads_the_project_declared_additivity():
    """`""` used to be hardcoded at the `compute_rollup` call in `build_summary`.

    `rollup.declared_non_additive_metrics("")` returns `frozenset()` on its first
    line, so story 60.2's per-Project declaration was dead on every report -- a
    client metric declared non-additive was summed here and refused on the card
    beside it. MUTATION: put `""` back -> red.
    """
    seen: list[str] = []

    def _spy(project_id):
        seen.append(project_id)
        return frozenset()

    with patch("core.rollup.declared_non_additive_metrics", side_effect=_spy):
        _render("ctr_daily", _two_days_of_ctr())

    assert seen, "declared_non_additive_metrics was never consulted"
    assert _PROJECT in seen, f"the report rollup was told {seen!r}, not the Project"
    assert "" not in seen


def test_the_narrative_states_the_refusal_in_french():
    """Emitted values are French-first (UX-DR10, audit C16); this line once read
    `no combined total` in English, and before that `total non combinable`."""
    from core.narrative import build_narrative

    rollup = rollup_module.compute_rollup(
        _two_sources_of_cost(), ["cost"], "2026-07-12", "2026-07-13", _PROJECT, [],
        route_resolver=lambda _m, _s=(): "UNRULED_OVERLAP",
    )
    text = build_narrative(
        project_id=_PROJECT, report_id=None, rollup=rollup,
        context_events=[], alerts=[], as_of=None, narrative_prompt=None,
    )

    assert "pas de total combiné" in text
    assert "non combinable" not in text
    assert "280" not in text
