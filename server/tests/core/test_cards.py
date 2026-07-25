"""Tests for the card framework, catalog and selection contract (Story 9.1).

Registry + suggestion + get_card logic live in core.cards. Warehouse is mocked so no
live mart is required. The end-to-end proof (list -> get_card suggests kpi -> envelope
with widget uri + rendered comment) is exercised here; the REST mirror through
build_asgi_app is in test_cards_api.py (the G-14 lesson).
"""

from __future__ import annotations

import os
from unittest.mock import patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import pytest  # noqa: E402
from core import cards as cards_module  # noqa: E402


class _FakeModule:
    def __init__(self, name, manifest, reports):
        self.name = name
        self.manifest = manifest
        self.reports = reports


def _rows(metric="sessions", connector="google-analytics"):
    return [
        {
            "date": "2026-07-05",
            "connector": connector,
            "metric": metric,
            "breakdown_dimension": "device",
            "breakdown_value": "desktop",
            "value": 100.0,
            "pull_id": "pull_1",
            "loaded_at": "2026-07-05T00:00:00",
        },
        {
            "date": "2026-07-06",
            "connector": connector,
            "metric": metric,
            "breakdown_dimension": "device",
            "breakdown_value": "desktop",
            "value": 120.0,
            "pull_id": "pull_1",
            "loaded_at": "2026-07-06T00:00:00",
        },
    ]


# ---------------------------------------------------------------------------
# Registry + catalog
# ---------------------------------------------------------------------------


def test_registry_registers_kpi_as_universal_fallback():
    kpi = cards_module.get_template("kpi")
    assert kpi is not None
    assert kpi.widget_uri == "ui://core/card-kpi"
    # ANY_METRIC sentinel -> the universal fallback.
    assert cards_module.ANY_METRIC in kpi.required_metrics
    assert kpi.fallback_rank == 0


def test_list_templates_exposes_choice_inputs():
    catalog = cards_module.list_templates()
    assert isinstance(catalog, list) and catalog
    entry = catalog[0]
    for key in ("id", "answers_question", "required_metrics", "widget_uri", "fallback_rank"):
        assert key in entry
    # answers_question is FR and non-empty (the LLM reads it to choose).
    assert entry["answers_question"].strip()


# ---------------------------------------------------------------------------
# Suggestion
# ---------------------------------------------------------------------------


def test_suggest_returns_kpi_when_any_metric_available():
    best, alts = cards_module.suggest_template({"sessions"}, set())
    assert best is not None and best.id == "kpi"
    assert alts == []


def test_suggest_returns_none_when_no_metric():
    best, alts = cards_module.suggest_template(set(), set())
    assert best is None
    assert alts == []


def test_suggest_prefers_higher_fallback_rank():
    """A hypothetical specialised card outranks KPI when its inputs are satisfied.

    Proves the contract is data-driven: adding an entry changes selection with no
    code change to suggest_template.
    """
    special = cards_module.CardTemplate(
        id="keywords",
        title="Mots-cles",
        answers_question="Comment vont mes mots-cles ?",
        widget_uri="ui://core/card-keywords",
        required_metrics=("clicks", "impressions"),
        required_dimensions=("query",),
        fallback_rank=10,
    )
    with patch.object(cards_module, "CARD_TEMPLATES", cards_module.CARD_TEMPLATES + [special]):
        best, alts = cards_module.suggest_template({"clicks", "impressions", "sessions"}, {"query"})
        assert best.id == "keywords"  # outranks KPI
        assert any(a.id == "kpi" for a in alts)  # KPI still an alternative


# ---------------------------------------------------------------------------
# get_card end-to-end (mocked warehouse)
# ---------------------------------------------------------------------------


def test_get_card_suggests_kpi_and_builds_envelope():
    with patch("core.warehouse.query_daily_report", return_value=_rows()):
        summary, envelope, widget_uri = cards_module.get_card(
            [], "default", metrics=["sessions"], date_from="2026-07-01", date_to="2026-07-06"
        )
    assert widget_uri == "ui://core/card-kpi"
    assert envelope["data"]["card_id"] == "kpi"
    sel = envelope["meta"]["card_selection"]
    assert sel["chosen"] == "kpi"
    assert sel["mode"] == "suggested"
    # Delta-aware rollup present.
    assert "sessions" in envelope["data"]["metrics"]
    assert envelope["data"]["metrics"]["sessions"]["value"] == 220
    # Sparkline series present.
    assert len(envelope["data"]["series"]["sessions"]) == 2
    # Deterministic cited comment present (AD-9).
    assert envelope["data"]["rendered_comment"].strip()
    # Summary carries the choice + the question.
    assert "kpi" in summary.lower() or "Synthese" in summary


def test_get_card_explicit_template_is_honoured():
    with patch("core.warehouse.query_daily_report", return_value=_rows()):
        _s, envelope, uri = cards_module.get_card(
            [], "default", template="kpi", metrics=["sessions"]
        )
    assert uri == "ui://core/card-kpi"
    assert envelope["meta"]["card_selection"]["mode"] == "explicit"


def test_get_card_unknown_template_raises():
    with patch("core.warehouse.query_daily_report", return_value=_rows()):
        with pytest.raises(cards_module.CardTemplateNotFound):
            cards_module.get_card([], "default", template="nope", metrics=["sessions"])


def test_get_card_unsatisfiable_explicit_template_raises_with_missing():
    # The real 'keywords' card requires clicks+impressions (metrics) and page (dimension).
    # Requesting it with only 'sessions' rows must raise with the structured missing set.
    with patch("core.warehouse.query_daily_report", return_value=_rows()):
        with pytest.raises(cards_module.CardTemplateUnsatisfied) as ei:
            cards_module.get_card([], "default", template="keywords", metrics=["sessions"])
    assert "clicks" in ei.value.missing["metrics"]
    assert "impressions" in ei.value.missing["metrics"]
    assert "page" in ei.value.missing["dimensions"]


def test_get_card_empty_data_falls_back_to_kpi_empty_state():
    """No rows at all -> KPI card still chosen (fallback_empty) with an empty envelope."""
    with patch("core.warehouse.query_daily_report", return_value=[]):
        _s, envelope, uri = cards_module.get_card([], "default", metrics=["sessions"])
    assert uri == "ui://core/card-kpi"
    # metrics from the requested set are offered so KPI is suggestable even when empty.
    assert envelope["meta"]["card_selection"]["chosen"] == "kpi"


def test_get_card_carries_r6_definitions_via_report_ref():
    """report_ref path pulls R6 metric_definitions/guidelines through the flows merge."""
    report = {
        "id": "overview_daily",
        "metrics": ["sessions"],
        "dimensions": ["date", "device"],
    }
    module = _FakeModule("google-analytics", {"widget_ref": "ui://core/daily-report"}, [report])

    def _fake_r6(project_id, report_ref, loaded_modules):
        return (
            {"sessions": {"definition": "Séances.", "direction": "up_good"}},
            "Directive de commentaire de test.",
        )

    with (
        patch("core.warehouse.query_report", return_value=_rows()),
        patch.object(cards_module, "_flow_preferred_template", return_value=None),
        patch.object(cards_module, "_fetch_card_config", return_value={}),
        patch.object(cards_module, "_fetch_r6", _fake_r6),
    ):
        _s, envelope, _uri = cards_module.get_card(
            [module], "default", report_ref="google-analytics/overview_daily"
        )
    assert envelope["data"]["metric_definitions"]["sessions"]["direction"] == "up_good"
    assert envelope["data"]["llm_commentary_guidelines"] == "Directive de commentaire de test."


def test_get_card_is_source_agnostic_canonical_only():
    """AD-2: a non-GA connector's canonical sessions drives the SAME KPI card."""
    with patch("core.warehouse.query_daily_report", return_value=_rows(connector="some-crm")):
        _s, envelope, uri = cards_module.get_card([], "default", metrics=["sessions"])
    assert uri == "ui://core/card-kpi"
    assert envelope["data"]["connectors"] == ["some-crm"]


# ---------------------------------------------------------------------------
# Story 9.2c: composition in envelope
# ---------------------------------------------------------------------------


def test_kpi_template_declares_composition():
    """KPI template must declare an ordered composition with kpi_row + comment."""
    kpi = cards_module.get_template("kpi")
    assert kpi is not None
    assert len(kpi.composition) >= 2
    types = [b["type"] for b in kpi.composition]
    assert "kpi_row" in types
    assert "comment" in types


def test_get_card_envelope_contains_composition_and_card_type():
    """get_card must return data.composition (ordered blocks) and data.card_type."""
    with patch("core.warehouse.query_daily_report", return_value=_rows()):
        _s, envelope, _uri = cards_module.get_card(
            [], "default", metrics=["sessions"], date_from="2026-07-01", date_to="2026-07-06"
        )
    data = envelope["data"]
    # card_type mirrors card_id / template_id
    assert data["card_type"] == "kpi"
    # composition is a list of ordered blocks
    assert isinstance(data["composition"], list)
    assert len(data["composition"]) >= 2
    types = [b["type"] for b in data["composition"]]
    assert "kpi_row" in types
    assert "comment" in types


def test_composition_block_shape():
    """Each composition block must have 'type' and 'binding' keys."""
    kpi = cards_module.get_template("kpi")
    assert kpi is not None
    for block in kpi.composition:
        assert "type" in block
        assert "binding" in block


def test_catalog_entry_exposes_composition():
    """to_catalog_entry() must expose the composition so the LLM can read it."""
    kpi = cards_module.get_template("kpi")
    assert kpi is not None
    entry = kpi.to_catalog_entry()
    assert "composition" in entry
    assert isinstance(entry["composition"], list)


# ---------------------------------------------------------------------------
# Story 9.2c: backend tracing + structured log
# ---------------------------------------------------------------------------


def test_get_card_emits_structured_log(caplog):
    """get_card always emits a structured logger.info with card.* fields (no OTel needed)."""
    import logging

    with caplog.at_level(logging.INFO, logger="core.cards"):
        with patch("core.warehouse.query_daily_report", return_value=_rows()):
            cards_module.get_card(
                [], "proj123", metrics=["sessions"], date_from="2026-07-01", date_to="2026-07-06"
            )

    # At least one log record from core.cards at INFO level
    records = [r for r in caplog.records if r.name == "core.cards" and r.levelno == logging.INFO]
    assert records, "Expected at least one INFO log from core.cards"

    # The log message must contain card tracing fields as a string repr
    joined = " ".join(str(r.getMessage()) for r in records)
    assert "card.template_id" in joined
    assert "card.selection_mode" in joined
    assert "card.composition" in joined
    assert "card.metrics" in joined
    assert "card.row_count" in joined
    assert "proj123" in joined


def test_get_card_log_contains_no_secrets(caplog):
    """Structured log must never leak tokens/secrets (AD-3)."""
    import logging

    with caplog.at_level(logging.INFO, logger="core.cards"):
        with patch("core.warehouse.query_daily_report", return_value=_rows()):
            cards_module.get_card([], "default", metrics=["sessions"])

    joined = " ".join(str(r.getMessage()) for r in caplog.records if r.name == "core.cards")
    # None of the secret-shaped substrings should appear
    for secret_word in ("token", "secret", "password", "api_key", "credential"):
        assert secret_word.lower() not in joined.lower(), (
            f"Secret word {secret_word!r} found in log"
        )


def test_get_card_calls_record_span_attributes_when_tracing_enabled():
    """When tracing is enabled, get_card calls record_current_span_attributes with card.* keys."""
    with patch("core.warehouse.query_daily_report", return_value=_rows()):
        with patch("core.tracing.is_enabled", return_value=True):
            with patch("core.tracing.record_current_span_attributes") as mock_span:
                cards_module.get_card(
                    [],
                    "proj_trace",
                    metrics=["sessions"],
                    date_from="2026-07-01",
                    date_to="2026-07-06",
                    trace_id="0123456789abcdef0123456789abcdef",
                )

    assert mock_span.called, "record_current_span_attributes must be called when tracing is enabled"
    call_attrs = mock_span.call_args[0][0]
    assert "card.template_id" in call_attrs
    assert call_attrs["card.template_id"] == "kpi"
    assert "card.selection_mode" in call_attrs
    assert "card.composition" in call_attrs
    assert "card.metrics" in call_attrs
    assert "card.dimensions" in call_attrs
    assert "card.row_count" in call_attrs
    assert "card.pull_ids" in call_attrs
    assert "project_id" in call_attrs
    assert call_attrs["project_id"] == "proj_trace"
    # review-9-2b F-9: trace_id is forwarded to the span so the card correlates with the
    # MCP caller's trace.
    assert "trace_id" in call_attrs
    assert call_attrs["trace_id"] == "0123456789abcdef0123456789abcdef"


def test_get_card_no_record_span_when_tracing_disabled():
    """When tracing is disabled, record_current_span_attributes must NOT be called."""
    with patch("core.warehouse.query_daily_report", return_value=_rows()):
        with patch("core.tracing.is_enabled", return_value=False):
            with patch("core.tracing.record_current_span_attributes") as mock_span:
                cards_module.get_card([], "default", metrics=["sessions"])

    assert not mock_span.called, (
        "record_current_span_attributes must not be called when tracing is disabled"
    )


def test_get_card_composition_span_attribute_is_comma_joined_types():
    """card.composition attribute must be comma-joined block type tokens."""
    with patch("core.warehouse.query_daily_report", return_value=_rows()):
        with patch("core.tracing.is_enabled", return_value=True):
            with patch("core.tracing.record_current_span_attributes") as mock_span:
                cards_module.get_card([], "default", metrics=["sessions"])

    attrs = mock_span.call_args[0][0]
    composition_val = attrs["card.composition"]
    # Must be a comma-joined string of block types
    assert isinstance(composition_val, str)
    parts = composition_val.split(",")
    assert "kpi_row" in parts
    assert "comment" in parts


# ---------------------------------------------------------------------------
# Stories 9.3-9.6: the 4 business cards (registry + get_card resolved composition)
# ---------------------------------------------------------------------------


def _multi_rows(specs):
    """Build long-format rows from (metric, dim, val, value, connector) tuples."""
    rows = []
    for i, (metric, dim, val, value, connector) in enumerate(specs):
        for d in ("2026-07-05", "2026-07-06"):
            rows.append(
                {
                    "date": d,
                    "connector": connector,
                    "metric": metric,
                    "breakdown_dimension": dim,
                    "breakdown_value": val,
                    "value": float(value),
                    "pull_id": "pull_1",
                    "loaded_at": f"{d}T00:00:00",
                }
            )
    return rows


def test_all_four_business_cards_registered():
    for cid, uri in [
        ("keywords", "ui://core/card-keywords"),
        ("conversions", "ui://core/card-conversions"),
        ("usertypes", "ui://core/card-usertypes"),
        ("journey", "ui://core/card-journey"),
    ]:
        tpl = cards_module.get_template(cid)
        assert tpl is not None, cid
        assert tpl.widget_uri == uri
        # every business card outranks the KPI fallback
        assert tpl.fallback_rank > 0
        # composition declares blocks with type+binding
        assert tpl.composition
        for block in tpl.composition:
            assert "type" in block and "binding" in block


def test_keywords_average_position_is_optional_metric_not_dimension():
    """average_position is a METRIC, not a dimension filter (review-9-3 F-6)."""
    kw = cards_module.get_template("keywords")
    assert "average_position" in kw.optional_metrics
    assert "average_position" not in kw.optional_dimensions
    # The catalog entry surfaces it under optional_metrics so the LLM does not read it
    # as a dimension filter.
    entry = kw.to_catalog_entry()
    assert "average_position" in entry["optional_metrics"]
    assert "average_position" not in entry["optional_dimensions"]


def test_connector_not_in_any_required_dimensions():
    """PRECONDITION (review-9-1 F-7): 'connector' must never gate selection as a
    required dimension -- it is a row column, never a breakdown_dimension."""
    for tpl in cards_module.CARD_TEMPLATES:
        assert cards_module.DIMENSION_CONNECTOR not in tpl.required_dimensions, tpl.id


def test_get_card_adhoc_path_applies_conversions_dedup():
    """The ad-hoc metrics=[...] path dedups cross-source conversions (review-9-1 F-5).

    Two connectors report conversions for the same (project, date). Rule P keeps only
    the priority source's rows so the card total is not the raw (inflated) sum.
    """
    rows = [
        {
            "date": "2026-07-05",
            "connector": "google-analytics",
            "project_id": "default",
            "metric": "conversions",
            "breakdown_dimension": "device_category",
            "breakdown_value": "mobile",
            "value": 100.0,
            "pull_id": "pull_1",
            "loaded_at": "2026-07-05T00:00:00",
        },
        {
            "date": "2026-07-05",
            "connector": "meta-ads",
            "project_id": "default",
            "metric": "conversions",
            "breakdown_dimension": "device_category",
            "breakdown_value": "mobile",
            "value": 40.0,
            "pull_id": "pull_2",
            "loaded_at": "2026-07-05T00:00:00",
        },
    ]
    from core import metrics as metrics_module

    real_dedup = metrics_module.apply_conversions_dedup
    dedup_seen = {}

    def _spy(rs):
        result = real_dedup(rs)  # call the REAL dedup, not the patched spy (no recursion)
        kept = sorted({r.get("connector") for r in result if r.get("metric") == "conversions"})
        # dedup runs on both current and (empty) prior rows -- keep the non-empty capture.
        if kept:
            dedup_seen["kept_connectors"] = kept
        return result

    with (
        patch("core.warehouse.query_daily_report", return_value=rows),
        patch("core.metrics.apply_conversions_dedup", side_effect=_spy),
    ):
        _s, envelope, _uri = cards_module.get_card(
            [],
            "default",
            metrics=["conversions"],
            date_from="2026-07-01",
            date_to="2026-07-06",
        )
    # dedup was invoked and kept exactly one connector's conversions (priority source).
    assert dedup_seen.get("kept_connectors") is not None
    assert len(dedup_seen["kept_connectors"]) == 1
    # The rollup total reflects the single retained source, never 140 (100 + 40).
    assert envelope["data"]["metrics"]["conversions"]["value"] in (100.0, 40.0)


def test_suggest_picks_keywords_when_gsc_inputs_present():
    best, alts = cards_module.suggest_template({"clicks", "impressions"}, {"page", "query"})
    assert best.id == "keywords"
    assert any(a.id == "kpi" for a in alts)


def test_suggest_picks_conversions_when_conversions_present():
    best, _alts = cards_module.suggest_template({"conversions", "cost"}, set())
    assert best.id == "conversions"


def test_suggest_picks_usertypes_for_ga4_users():
    best, _alts = cards_module.suggest_template(
        {"active_users", "sessions"}, {"device_category", "country"}
    )
    # journey (rank 35) requires sessions+conversions; without conversions it is NOT
    # satisfied and does not compete. usertypes (rank 20) fits (active_users+sessions
    # +device_category) and wins over the KPI floor (rank 0).
    assert best.id == "usertypes"


def test_suggest_picks_journey_when_sessions_and_conversions_present():
    best, _alts = cards_module.suggest_template({"sessions", "conversions"}, set())
    assert best.id == "journey"


def test_get_card_keywords_populates_composition_blocks():
    rows = _multi_rows(
        [
            ("clicks", "query", "chaussures", 40, "gsc"),
            ("clicks", "query", "bottes", 10, "gsc"),
            ("impressions", "query", "chaussures", 900, "gsc"),
            ("impressions", "query", "bottes", 300, "gsc"),
            ("clicks", "page", "/a", 50, "gsc"),
            ("impressions", "page", "/a", 1200, "gsc"),
            ("average_position", "query", "chaussures", 5.0, "gsc"),
            ("average_position", "query", "bottes", 3.5, "gsc"),
        ]
    )
    # Prior-period rows so the movers bar has a delta to compute:
    # chaussures 9.0 -> 5.0 (mover, +4 positions), bottes 4.0 -> 3.5 (< 2, filtered).
    for metric, val, value in [
        ("average_position", "chaussures", 9.0),
        ("average_position", "bottes", 4.0),
        ("impressions", "chaussures", 800.0),
        ("impressions", "bottes", 250.0),
    ]:
        rows.append(
            {
                "date": "2026-06-27",
                "connector": "gsc",
                "metric": metric,
                "breakdown_dimension": "query",
                "breakdown_value": val,
                "value": value,
                "pull_id": "pull_0",
                "loaded_at": "2026-06-27T00:00:00",
            }
        )
    with patch("core.warehouse.query_daily_report", return_value=rows):
        _s, envelope, uri = cards_module.get_card(
            [],
            "default",
            template="keywords",
            metrics=["clicks", "impressions"],
            date_from="2026-07-01",
            date_to="2026-07-06",
        )
    assert uri == "ui://core/card-keywords"
    composition = envelope["data"]["composition"]
    comp = {b["type"]: b for b in composition}
    assert comp["kpi_row"]["data"]["metrics"]
    assert comp["bar"]["data"]["bars"][0]["label"] == "chaussures"  # top mover
    # review-epic-9-integration F-1: the keywords card now has TWO table blocks -- "Top
    # requêtes" and "Opportunités". Select by title (dict-by-type would drop the first).
    tables = {b.get("title"): b for b in composition if b["type"] == "table"}
    assert tables["Top requêtes"]["data"]["rows"]
    # Opportunités block is ALWAYS present (designed empty when no opportunity).
    assert "Opportunités" in tables
    assert "rows" in tables["Opportunités"]["data"]
    assert comp["comment"]["data"]["text"].strip()


def test_get_card_keywords_opportunities_block_populates():
    """review-epic-9-integration F-1: the keywords Opportunités table surfaces high-
    impression / weak-position queries end-to-end through get_card."""
    rows = _multi_rows(
        [
            ("clicks", "page", "/a", 50, "gsc"),
            ("impressions", "page", "/a", 1200, "gsc"),
            # weak-rank, high-impression query -> an opportunity (pos 15 > 10)
            ("average_position", "query", "chaussures ete", 15.0, "gsc"),
            ("impressions", "query", "chaussures ete", 3000, "gsc"),
            ("clicks", "query", "chaussures ete", 20, "gsc"),
            # strong-rank query -> NOT an opportunity
            ("average_position", "query", "brand", 2.0, "gsc"),
            ("impressions", "query", "brand", 5000, "gsc"),
            ("clicks", "query", "brand", 400, "gsc"),
        ]
    )
    with patch("core.warehouse.query_daily_report", return_value=rows):
        _s, envelope, uri = cards_module.get_card(
            [],
            "default",
            template="keywords",
            metrics=["clicks", "impressions"],
            date_from="2026-07-01",
            date_to="2026-07-06",
        )
    assert uri == "ui://core/card-keywords"
    tables = {b.get("title"): b for b in envelope["data"]["composition"] if b["type"] == "table"}
    opp = tables["Opportunités"]["data"]
    labels = [r["_dim"] for r in opp["rows"]]
    assert "chaussures ete" in labels  # the opportunity surfaced
    assert "brand" not in labels  # strong rank excluded


def test_get_card_conversions_donut_gauge_table():
    rows = _multi_rows(
        [
            ("conversions", "campaign_id", "c1", 8, "meta-ads"),
            ("cost", "campaign_id", "c1", 240, "meta-ads"),
            ("conversions", "device_category", "mobile", 4, "google-analytics"),
        ]
    )
    with patch("core.warehouse.query_daily_report", return_value=rows):
        _s, envelope, uri = cards_module.get_card(
            [],
            "default",
            template="conversions",
            metrics=["conversions", "cost"],
            date_from="2026-07-01",
            date_to="2026-07-06",
        )
    assert uri == "ui://core/card-conversions"
    comp = {b["type"]: b for b in envelope["data"]["composition"]}
    # review-epic-9-backend F-1 / integration F-2: the conversions donut now reads the SAME
    # Rule-P DEDUPED base as the gauge/rollup/comment (architecture 3.7 "no metric counted
    # twice" wins over "show every source raw"). google-analytics is priority-1 in
    # metric_source_priority.csv, so for these overlapping dates its conversions win and the
    # meta-ads conversions are deduped out of the per-source donut. The donut total must
    # therefore equal the gauge denominator (the deduped conversions count).
    donut = comp["donut"]["data"]
    labels = {s["label"] for s in donut["slices"]}
    assert labels == {"google-analytics"}
    # gauge CPA is a CROSS-SOURCE scalar -> conversions are deduped (Rule P: google-analytics
    # is priority-1). So conversions = 8 (GA 4 x 2 days), cost = 480 (meta-ads 240 x 2).
    # CPA = 480 / 8 = 60 (NOT the old double-counted 480/24 = 20).
    assert comp["gauge"]["data"]["value"] == 60.0
    # PROOF of coherence: the donut total (deduped conversions) == the gauge denominator.
    # gauge value = cost / conversions => conversions denominator = cost / value.
    gauge = comp["gauge"]["data"]
    gauge_denominator = 480.0 / gauge["value"]  # 480 / 60 = 8
    assert donut["total"] == gauge_denominator == 8.0
    # table by source has a cpa column
    tcols = [c["key"] for c in comp["table"]["data"]["columns"]]
    assert "cpa" in tcols


def test_get_card_usertypes_donut_by_device():
    rows = _multi_rows(
        [
            ("active_users", "device_category", "mobile", 300, "google-analytics"),
            ("active_users", "device_category", "desktop", 100, "google-analytics"),
            ("sessions", "device_category", "mobile", 500, "google-analytics"),
            ("active_users", "country", "FR", 250, "google-analytics"),
        ]
    )
    with patch("core.warehouse.query_daily_report", return_value=rows):
        _s, envelope, uri = cards_module.get_card(
            [],
            "default",
            template="usertypes",
            metrics=["active_users", "sessions"],
            date_from="2026-07-01",
            date_to="2026-07-06",
        )
    assert uri == "ui://core/card-usertypes"
    composition = envelope["data"]["composition"]
    # review-epic-10 F-2: the usertypes card now has TWO donuts (user_type LEAD block +
    # device_category). Selecting blocks by {b["type"]: b} collides the two donuts and keeps
    # only the LAST one -- fragile. Select each donut by its resolved data.dimension instead.
    donuts = {
        b["data"]["dimension"]: b["data"]
        for b in composition
        if b["type"] == "donut" and b.get("data", {}).get("dimension")
    }
    device_donut = donuts["device_category"]
    pcts = {s["label"]: s["pct"] for s in device_donut["slices"]}
    assert pcts["mobile"] == 75.0
    # The user_type donut is present but empty here (no user_type rows in this seed) -> it
    # degrades to empty slices rather than being absent (Story 10.2 degrade contract).
    donut_dims = [b["binding"]["dimensions"][0] for b in composition if b["type"] == "donut"]
    assert donut_dims == ["user_type", "device_category"]
    bar = next(b for b in composition if b["type"] == "bar")["data"]
    assert bar["dimension"] == "country"


def test_get_card_journey_funnel_rates():
    rows = _multi_rows(
        [
            ("sessions", "device_category", "mobile", 1000, "google-analytics"),
            ("active_users", "device_category", "mobile", 800, "google-analytics"),
            ("conversions", "device_category", "mobile", 200, "google-analytics"),
        ]
    )
    with patch("core.warehouse.query_daily_report", return_value=rows):
        _s, envelope, uri = cards_module.get_card(
            [],
            "default",
            template="journey",
            metrics=["sessions", "active_users", "conversions"],
            date_from="2026-07-01",
            date_to="2026-07-06",
        )
    assert uri == "ui://core/card-journey"
    comp = {b["type"]: b for b in envelope["data"]["composition"]}
    funnel = comp["funnel"]["data"]
    assert [s["label"] for s in funnel["steps"]] == ["sessions", "active_users", "conversions"]
    assert funnel["steps"][1]["rate"] == 0.8
    assert funnel["overall_rate"] == 0.2


# ---------------------------------------------------------------------------
# Live check against the dev DuckDB (GA4-driven cards). Skipped when the seed is
# absent so CI without the seed still passes.
# ---------------------------------------------------------------------------

_DEV_DUCKDB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "modules",
    "google-analytics",
    "seeds",
    "local.duckdb",
)


@pytest.mark.skipif(not os.path.exists(_DEV_DUCKDB), reason="dev DuckDB seed not present")
def test_live_usertypes_and_journey_against_dev_duckdb(monkeypatch):
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", _DEV_DUCKDB)

    # usertypes: GA4 active_users + sessions, device_category present in the seed.
    _s, env_u, uri_u = cards_module.get_card(
        [],
        "default",
        template="usertypes",
        metrics=["active_users", "sessions"],
        date_from="2026-06-11",
        date_to="2026-07-11",
    )
    assert uri_u == "ui://core/card-usertypes"
    comp_u = {b["type"]: b for b in env_u["data"]["composition"]}
    donut = comp_u["donut"]["data"]
    assert donut["slices"], "expected device_category slices from the live seed"
    # pct shares must sum to ~100 (additive share correctness)
    assert abs(sum(s["pct"] for s in donut["slices"]) - 100.0) < 1.0

    # journey: GA4 sessions + conversions -> funnel with a real pass-through rate.
    _s2, env_j, uri_j = cards_module.get_card(
        [],
        "default",
        template="journey",
        metrics=["sessions", "active_users", "conversions"],
        date_from="2026-06-11",
        date_to="2026-07-11",
    )
    assert uri_j == "ui://core/card-journey"
    comp_j = {b["type"]: b for b in env_j["data"]["composition"]}
    funnel = comp_j["funnel"]["data"]
    assert len(funnel["steps"]) >= 2
    # every pass-through rate is a valid fraction (0..1.05) or None (review-9-3 F-9/F-10):
    # sessions >= active_users >= conversions is the invariant, so a rate > ~1 signals a
    # double-count. A tiny 1.05 tolerance absorbs same-day rounding, never a 2x sum artifact.
    for step in funnel["steps"][1:]:
        assert step["rate"] is None or 0.0 <= step["rate"] <= 1.05


# ---------------------------------------------------------------------------
# Story 16.3 — Attribution card (Epic 16)
# ---------------------------------------------------------------------------


def _attribution_rows(*, with_campaign: bool = True, with_first_click: bool = True):
    """Seed rows for the attribution card tests.

    Three marginal partitions of the SAME conversions (per the 16.2 marginal identity
    invariant): session_source_medium, session_campaign, first_user_source_medium.

    KEY: if you sum all three partitions for the same day you get 3x the real total.
    The card must produce 1x by filtering on ONE breakdown_dimension per block.

    Multi-day, multi-value (AI-45 multi-day rule).
    """
    rows = []
    for d in ("2026-07-04", "2026-07-05", "2026-07-06"):
        # last-click channel partition (session_source_medium)
        for channel, conv, sess in (
            ("cpc / google", 120.0, 340.0),
            ("organic / google", 80.0, 410.0),
            ("direct / (none)", 50.0, 200.0),
        ):
            rows.append(
                {
                    "date": d,
                    "connector": "google-analytics",
                    "metric": "conversions",
                    "breakdown_dimension": "session_source_medium",
                    "breakdown_value": channel,
                    "value": conv,
                    "pull_id": "pull_16",
                    "loaded_at": f"{d}T00:00:00",
                }
            )
            rows.append(
                {
                    "date": d,
                    "connector": "google-analytics",
                    "metric": "sessions",
                    "breakdown_dimension": "session_source_medium",
                    "breakdown_value": channel,
                    "value": sess,
                    "pull_id": "pull_16",
                    "loaded_at": f"{d}T00:00:00",
                }
            )
        if with_campaign:
            # last-click campaign partition (session_campaign) —
            # SAME conversions, different axis
            campaigns = (("summer_sale", 130.0), ("brand_awareness", 90.0), ("retargeting", 30.0))
            for campaign, conv in campaigns:
                rows.append(
                    {
                        "date": d,
                        "connector": "google-analytics",
                        "metric": "conversions",
                        "breakdown_dimension": "session_campaign",
                        "breakdown_value": campaign,
                        "value": conv,
                        "pull_id": "pull_16",
                        "loaded_at": f"{d}T00:00:00",
                    }
                )
        if with_first_click:
            # first-click channel partition (first_user_source_medium) —
            # SAME conversions, different model
            first_channels = (
                ("organic / google", 140.0),
                ("cpc / google", 90.0),
                ("referral / partner", 20.0),
            )
            for channel, conv in first_channels:
                rows.append(
                    {
                        "date": d,
                        "connector": "google-analytics",
                        "metric": "conversions",
                        "breakdown_dimension": "first_user_source_medium",
                        "breakdown_value": channel,
                        "value": conv,
                        "pull_id": "pull_16",
                        "loaded_at": f"{d}T00:00:00",
                    }
                )
    return rows


def test_attribution_template_registered():
    """The attribution template is registered in CARD_TEMPLATES with the correct contract."""
    tpl = cards_module.get_template("attribution")
    assert tpl is not None
    assert tpl.id == "attribution"
    assert tpl.widget_uri == "ui://core/card-attribution"
    assert tpl.fallback_rank == 25
    # Required: only conversions (no required dimensions -> degrade-never-break)
    assert "conversions" in tpl.required_metrics
    assert tpl.required_dimensions == ()
    # Optional acquisition dimensions
    assert "session_source_medium" in tpl.optional_dimensions
    assert "session_campaign" in tpl.optional_dimensions
    assert "first_user_source_medium" in tpl.optional_dimensions
    # Composition: kpi_row + 2 bars + table + comment = 5 blocks
    assert len(tpl.composition) == 5
    types = [b["type"] for b in tpl.composition]
    assert types == ["kpi_row", "bar", "bar", "table", "comment"]
    # answers_question is French
    assert "canal" in tpl.answers_question.lower() or "clic" in tpl.answers_question.lower()


def test_attribution_get_card_resolves_template():
    """get_card with template='attribution' returns a valid envelope."""
    rows = _attribution_rows()
    with patch("core.warehouse.query_daily_report", return_value=rows):
        summary, envelope, widget_uri = cards_module.get_card(
            [],
            "default",
            template="attribution",
            metrics=["conversions", "sessions"],
            date_from="2026-07-04",
            date_to="2026-07-06",
        )
    assert widget_uri == "ui://core/card-attribution"
    data = envelope["data"]
    assert data["card_id"] == "attribution"
    assert data["card_type"] == "attribution"
    assert data["title"] == "Attribution"
    assert "composition" in data
    assert len(data["composition"]) == 5


def test_attribution_breakdown_kind_filter_prevents_double_count():
    """CRITICAL: the card must NOT double-count the three marginal partitions.

    Without the breakdown_dim_filter, summing session_source_medium + session_campaign
    + first_user_source_medium for the same days gives ~3x the real total. The kpi_row
    and bar blocks must filter to ONE breakdown_dimension per block. This test proves
    the invariant by checking that the kpi_row value matches the per-day sum of
    session_source_medium rows ONLY (not 2x or 3x).

    Per-day total of session_source_medium conversions = 120+80+50 = 250.
    With 3 days current window = 750 (prior window is 0 rows here — date_from covers all).
    """
    rows = _attribution_rows()
    with patch("core.warehouse.query_daily_report", return_value=rows):
        _, envelope, _ = cards_module.get_card(
            [],
            "default",
            template="attribution",
            metrics=["conversions", "sessions"],
            date_from="2026-07-04",
            date_to="2026-07-06",
        )

    composition = {i: b for i, b in enumerate(envelope["data"]["composition"])}
    kpi_block = composition[0]  # first block = kpi_row
    assert kpi_block["type"] == "kpi_row"

    # kpi_row must use session_source_medium ONLY (breakdown_dim_filter).
    # Per day: cpc(120) + organic(80) + direct(50) = 250. 3 days = 750.
    kpi_metrics = {m["metric"]: m["value"] for m in kpi_block["data"]["metrics"]}
    assert kpi_metrics.get("conversions") is not None
    # Value must be 750 (1x partition), never 1500 (2x) or 2250 (3x).
    assert kpi_metrics["conversions"] == pytest.approx(750.0, abs=0.1), (
        f"Double-count detected: expected 750 (one partition), got {kpi_metrics['conversions']}"
    )


def test_attribution_last_click_bar_uses_session_source_medium_only():
    """The last-click bar reads session_source_medium rows ONLY."""
    rows = _attribution_rows()
    with patch("core.warehouse.query_daily_report", return_value=rows):
        _, envelope, _ = cards_module.get_card(
            [],
            "default",
            template="attribution",
            metrics=["conversions", "sessions"],
            date_from="2026-07-04",
            date_to="2026-07-06",
        )

    # Second block = last-click bar (session_source_medium)
    last_bar_block = envelope["data"]["composition"][1]
    assert last_bar_block["type"] == "bar"
    assert last_bar_block["title"] == "Canaux — dernier clic (top-N)"
    bars = last_bar_block["data"]["bars"]
    assert len(bars) >= 1
    # Top bar = cpc / google (120 conv/day * 3 = 360)
    assert bars[0]["label"] == "cpc / google"
    assert bars[0]["value"] == pytest.approx(360.0, abs=0.1)
    # Bars should NOT contain session_campaign values (summer_sale, etc.)
    bar_labels = {b["label"] for b in bars}
    assert "summer_sale" not in bar_labels
    assert "brand_awareness" not in bar_labels


def test_attribution_first_click_bar_uses_first_user_source_medium_only():
    """The first-click bar reads first_user_source_medium rows ONLY."""
    rows = _attribution_rows()
    with patch("core.warehouse.query_daily_report", return_value=rows):
        _, envelope, _ = cards_module.get_card(
            [],
            "default",
            template="attribution",
            metrics=["conversions", "sessions"],
            date_from="2026-07-04",
            date_to="2026-07-06",
        )

    # Third block = first-click bar (first_user_source_medium)
    first_bar_block = envelope["data"]["composition"][2]
    assert first_bar_block["type"] == "bar"
    assert first_bar_block["title"] == "Canaux — premier clic (top-N)"
    bars = first_bar_block["data"]["bars"]
    assert len(bars) >= 1
    # Top first-click bar = organic / google (140 conv/day * 3 = 420)
    assert bars[0]["label"] == "organic / google"
    assert bars[0]["value"] == pytest.approx(420.0, abs=0.1)
    # Bars should NOT contain campaign values
    bar_labels = {b["label"] for b in bars}
    assert "summer_sale" not in bar_labels


def test_attribution_campaign_table_uses_session_campaign_only():
    """The campaign table reads session_campaign rows ONLY (not channel rows)."""
    rows = _attribution_rows()
    with patch("core.warehouse.query_daily_report", return_value=rows):
        _, envelope, _ = cards_module.get_card(
            [],
            "default",
            template="attribution",
            metrics=["conversions", "sessions"],
            date_from="2026-07-04",
            date_to="2026-07-06",
        )

    # Fourth block = campaign table
    table_block = envelope["data"]["composition"][3]
    assert table_block["type"] == "table"
    assert table_block["title"] == "Campagnes — dernier clic (top-N)"
    rows_data = table_block["data"]["rows"]
    assert len(rows_data) >= 1
    # Top campaign = summer_sale (130 conv/day * 3 = 390)
    assert rows_data[0]["_dim"] == "summer_sale"
    assert rows_data[0]["conversions"] == pytest.approx(390.0, abs=0.1)
    # « Part (top-N) » column must be present and sum to ~100
    parts = [r["part_pct"] for r in rows_data]
    assert sum(parts) == pytest.approx(100.0, abs=1.0)
    # Table must NOT contain channel values (cpc/google etc.)
    dim_values = {r["_dim"] for r in rows_data}
    assert "cpc / google" not in dim_values


def test_attribution_degrade_no_acquisition_data():
    """When no acquisition dimensions are present the card still renders (empty bars/table).

    This proves the degrade-never-break contract: a project without the acquisition
    datastream gets a valid attribution card with designed empty blocks.
    """
    # Only plain sessions/conversions with a device dimension — no acquisition partitions.
    plain_rows = [
        {
            "date": "2026-07-05",
            "connector": "google-analytics",
            "metric": "conversions",
            "breakdown_dimension": "device_category",
            "breakdown_value": "desktop",
            "value": 80.0,
            "pull_id": "pull_0",
            "loaded_at": "2026-07-05T00:00:00",
        },
        {
            "date": "2026-07-05",
            "connector": "google-analytics",
            "metric": "sessions",
            "breakdown_dimension": "device_category",
            "breakdown_value": "desktop",
            "value": 200.0,
            "pull_id": "pull_0",
            "loaded_at": "2026-07-05T00:00:00",
        },
    ]
    with patch("core.warehouse.query_daily_report", return_value=plain_rows):
        _, envelope, _ = cards_module.get_card(
            [],
            "default",
            template="attribution",
            metrics=["conversions", "sessions"],
            date_from="2026-07-01",
            date_to="2026-07-06",
        )

    data = envelope["data"]
    assert data["card_id"] == "attribution"
    composition = data["composition"]
    assert len(composition) == 5

    # All bar blocks must degrade to empty bars (no acquisition dims present)
    for block in composition:
        if block["type"] == "bar":
            assert block["data"].get("bars") == [], (
                f"Bar block {block.get('title')} must be empty without acquisition data"
            )
    # Campaign table must degrade to empty rows
    table_block = next(b for b in composition if b["type"] == "table")
    assert table_block["data"].get("rows") == []
    # kpi_row must not raise (value may be None or 0 — honest empty state)
    kpi_block = next(b for b in composition if b["type"] == "kpi_row")
    assert "metrics" in kpi_block["data"]


def test_attribution_comment_cites_leader_no_cause_invented():
    """build_attribution_comment cites the last-click leader; never invents a cause (AD-9)."""
    rows = _attribution_rows()
    with patch("core.warehouse.query_daily_report", return_value=rows):
        summary, envelope, _ = cards_module.get_card(
            [],
            "default",
            template="attribution",
            metrics=["conversions", "sessions"],
            date_from="2026-07-04",
            date_to="2026-07-06",
        )

    comment = envelope["data"]["rendered_comment"]
    # Must cite the last-click leader channel
    assert "cpc / google" in comment or "Canal leader" in comment
    # Must end with context-missing (no context_events supplied)
    assert "Contexte manquant" in comment
    # Must NOT state a cause (AD-9)
    assert "causé" not in comment.lower()
    assert "cause" not in comment.lower()


# ---------------------------------------------------------------------------
# Story 17.3 — Déduplication card
# ---------------------------------------------------------------------------


def _dedup_rows_ga4(*, days=3, meta_claimed_per_day=80.0, ga4_verified_per_day=100.0):
    """Seed rows for dedup_estimate (multi-day, GA4 source of truth).

    Simulates a project with:
      * Meta-ads claiming 80 conversions/day x 3 days = 240 claimed_total
      * GA4 verified = 100 conversions/day x 3 days = 300 verified_total
      * duplication_rate per day = 80/100 = 0.8 (per day; window rate = 240/300 = 0.8)
      * deduplicated_contribution = 80 * 100/80 = 100 per day = 300 window
    """
    rows = []
    dates = ["2026-07-04", "2026-07-05", "2026-07-06"][:days]
    for d in dates:
        claimed_total = meta_claimed_per_day
        verified = ga4_verified_per_day
        rate = claimed_total / verified if verified else None
        dedup_contrib = meta_claimed_per_day * verified / claimed_total if claimed_total else None
        rows.append(
            {
                "date": d,
                "channel_connector": "meta-ads",
                "claimed_conversions": meta_claimed_per_day,
                "verified_total": verified,
                "claimed_total": claimed_total,
                "duplication_rate": rate,
                "deduplicated_contribution": dedup_contrib,
                "verification_source_type": "ga4",
                "verification_source_id": "ga4_stream_01",
                "lead_event_name": "generate_lead",
                "estimate_label": "estimation",
                "pull_id": "pull_17_3",
            }
        )
    return rows


def _dedup_rows_multi_channel():
    """Multi-channel seed: Meta 80 + Google 60 + LinkedIn 20 vs 100 GA4 verified (per day).

    Exactly replicates the epic-17 AC: Meta 80/Google 60/LinkedIn 20 = 160 claimed.
    verified=100. rate=1.6.
    deduplicated: Meta=80*100/160=50, Google=60*100/160=37.5, LinkedIn=20*100/160=12.5.
    """
    rows = []
    dates = ["2026-07-04", "2026-07-05", "2026-07-06"]
    channels = [
        ("meta-ads", 80.0, 50.0),
        ("google-ads", 60.0, 37.5),
        ("linkedin-ads", 20.0, 12.5),
    ]
    for d in dates:
        for ch, claimed, dedup in channels:
            rows.append(
                {
                    "date": d,
                    "channel_connector": ch,
                    "claimed_conversions": claimed,
                    "verified_total": 100.0,
                    "claimed_total": 160.0,
                    "duplication_rate": 1.6,
                    "deduplicated_contribution": dedup,
                    "verification_source_type": "ga4",
                    "verification_source_id": "ga4_stream_01",
                    "lead_event_name": "generate_lead",
                    "estimate_label": "estimation",
                    "pull_id": "pull_17_3",
                }
            )
    return rows


def _dedup_rows_null_verified():
    """Rows where verified_total and deduplicated_contribution are NULL (stripe v1 BLOCKED)."""
    return [
        {
            "date": "2026-07-05",
            "channel_connector": "meta-ads",
            "claimed_conversions": 80.0,
            "verified_total": None,
            "claimed_total": 80.0,
            "duplication_rate": None,
            "deduplicated_contribution": None,
            "verification_source_type": "stripe",
            "verification_source_id": "stripe_acct_01",
            "lead_event_name": None,
            "estimate_label": "estimation",
            "pull_id": "pull_17_3",
        }
    ]


# --- Template registration --------------------------------------------------


def test_dedup_template_registered():
    """The dedup template is registered with the correct identity (Story 17.3 AC1)."""
    tpl = cards_module.get_template("dedup")
    assert tpl is not None
    assert tpl.id == "dedup"
    assert tpl.widget_uri == "ui://core/card-dedup"
    assert tpl.kind == cards_module.CARD_KIND_CONTEXT
    # Context cards are never auto-suggested.
    assert tpl.is_context
    # Composition: kpi_row + bar + table + comment = 4 blocks
    assert len(tpl.composition) == 4
    types = [b["type"] for b in tpl.composition]
    assert types == ["kpi_row", "bar", "table", "comment"]
    # answers_question is FR
    assert "double" in tpl.answers_question.lower() or "conversion" in tpl.answers_question.lower()


def test_dedup_not_in_suggestion_pool():
    """Context card 'dedup' must NEVER appear in suggest_template results (explicit-only)."""
    best, alts = cards_module.suggest_template({"conversions"}, set())
    all_ids = [t.id for t in ([best] if best else []) + alts]
    assert "dedup" not in all_ids


# --- get_card with GA4 seed (happy path) ------------------------------------


def test_dedup_rate_is_ratio_of_sums_not_mean_of_rates():
    """review-17-3 F-3 : taux fenêtre = ratio-of-sums, PAS la moyenne des taux.

    Jour 1 : claimed 100 / verified 50 (taux 2.0) ; jour 2 : claimed 100 /
    verified 200 (taux 0.5). Ratio-of-sums = 200/250 = 0.8 ; une moyenne des
    taux donnerait 1.25 — les deux méthodes DIFFÈRENT, le test discrimine.
    """
    rows = []
    for d, claimed, verified in (
        ("2026-07-04", 100.0, 50.0),
        ("2026-07-05", 100.0, 200.0),
    ):
        rows.append(
            {
                "date": d,
                "channel_connector": "meta-ads",
                "claimed_conversions": claimed,
                "verified_total": verified,
                "claimed_total": claimed,
                "duplication_rate": claimed / verified,
                "deduplicated_contribution": verified,
                "verification_source_type": "ga4",
                "verification_source_id": "ga4_stream_01",
                "lead_event_name": "generate_lead",
                "estimate_label": "estimation",
                "pull_id": f"pull_{d}",
            }
        )
    with patch("core.warehouse.query_dedup_estimate", return_value=rows):
        _, envelope, _ = cards_module.get_card(
            [],
            "default",
            template="dedup",
            date_from="2026-07-04",
            date_to="2026-07-05",
        )
    meta_d = envelope["data"]["dedup_meta"]
    assert meta_d["duplication_rate"] == pytest.approx(0.8, abs=0.01)
    assert meta_d["rate_coverage_days"] == 2
    assert meta_d["rate_claimed_days"] == 2


def test_dedup_rate_restricted_to_common_dates_when_verified_null_one_day():
    """review-17-3 F-2 (AD-9) : un jour avec claimed présent mais verified NULL
    n'entre PAS au numérateur du taux (axes communs uniquement) — sinon le taux
    est gonflé sur un dénominateur amputé. La couverture est exposée (2/3 jours).
    """
    rows = []
    for d, claimed, verified in (
        ("2026-07-04", 80.0, 100.0),
        ("2026-07-05", 90.0, None),  # verified NULL (trou GA4 / stripe)
        ("2026-07-06", 70.0, 100.0),
    ):
        rows.append(
            {
                "date": d,
                "channel_connector": "meta-ads",
                "claimed_conversions": claimed,
                "verified_total": verified,
                "claimed_total": claimed,
                "duplication_rate": (claimed / verified) if verified else None,
                "deduplicated_contribution": verified,
                "verification_source_type": "ga4",
                "verification_source_id": "ga4_stream_01",
                "lead_event_name": "generate_lead",
                "estimate_label": "estimation",
                "pull_id": f"pull_{d}",
            }
        )
    with patch("core.warehouse.query_dedup_estimate", return_value=rows):
        _, envelope, _ = cards_module.get_card(
            [],
            "default",
            template="dedup",
            date_from="2026-07-04",
            date_to="2026-07-06",
        )
    meta_d = envelope["data"]["dedup_meta"]
    # (80+70)/(100+100) = 0.75 — le jour 05 (claimed 90, verified NULL) est EXCLU
    # du taux ; un mélange d'axes donnerait 240/200 = 1.2 (gonflé).
    assert meta_d["duplication_rate"] == pytest.approx(0.75, abs=0.01)
    assert meta_d["rate_coverage_days"] == 2
    assert meta_d["rate_claimed_days"] == 3
    # Les TOTAUX affichés couvrent leurs axes complets (claimed 240, verified 200).
    assert meta_d["claimed_total"] == pytest.approx(240.0)
    assert meta_d["verified_total"] == pytest.approx(200.0)


def test_dedup_get_card_happy_path_ga4():
    """get_card(template='dedup') resolves the envelope end-to-end (Story 17.3 AC2)."""
    rows = _dedup_rows_ga4()
    with patch("core.warehouse.query_dedup_estimate", return_value=rows):
        summary, envelope, widget_uri = cards_module.get_card(
            [],
            "default",
            template="dedup",
            date_from="2026-07-04",
            date_to="2026-07-06",
        )
    assert widget_uri == "ui://core/card-dedup"
    data = envelope["data"]
    assert data["card_id"] == "dedup"
    assert data["card_type"] == "dedup"
    assert "composition" in data
    assert len(data["composition"]) == 4
    meta = envelope["meta"]
    assert meta["card_selection"]["chosen"] == "dedup"


def test_dedup_kpi_row_shows_rate():
    """kpi_row block carries the duplication_rate value (Story 17.3)."""
    rows = _dedup_rows_ga4(meta_claimed_per_day=80.0, ga4_verified_per_day=100.0)
    with patch("core.warehouse.query_dedup_estimate", return_value=rows):
        _, envelope, _ = cards_module.get_card(
            [],
            "default",
            template="dedup",
            date_from="2026-07-04",
            date_to="2026-07-06",
        )
    kpi_block = envelope["data"]["composition"][0]
    assert kpi_block["type"] == "kpi_row"
    metrics_data = kpi_block["data"]["metrics"]
    assert len(metrics_data) == 1
    rate_entry = metrics_data[0]
    assert rate_entry["metric"] == "duplication_rate"
    # window rate = 240/300 = 0.8
    assert rate_entry["value"] == pytest.approx(0.8, abs=0.01)
    # estimate_label MUST be present (AD-9)
    assert rate_entry.get("estimate_label") == "Estimation"


def test_dedup_multi_channel_values_match_epic_example():
    """Multi-channel: Meta=80, Google=60, LinkedIn=20 vs 100 verified => rate=1.6 (AC from epic-17).

    Window (3 days): claimed_total=480, verified_total=300, rate=480/300=1.6.
    Dedup: Meta=50/day=150 window, Google=37.5/day=112.5, LinkedIn=12.5/day=37.5
    Sum = 300 = verified (within tolerance).
    """
    rows = _dedup_rows_multi_channel()
    with patch("core.warehouse.query_dedup_estimate", return_value=rows):
        _, envelope, _ = cards_module.get_card(
            [],
            "default",
            template="dedup",
            date_from="2026-07-04",
            date_to="2026-07-06",
        )

    dedup_meta = envelope["data"]["dedup_meta"]
    # window rate = 480/300 = 1.6
    assert dedup_meta["duplication_rate"] == pytest.approx(1.6, abs=0.01)
    assert dedup_meta["verified_total"] == pytest.approx(300.0, abs=0.1)
    assert dedup_meta["claimed_total"] == pytest.approx(480.0, abs=0.1)

    # Table rows: 3 channels with correct dedup contributions
    table_block = envelope["data"]["composition"][2]
    assert table_block["type"] == "table"
    rows_data = table_block["data"]["rows"]
    assert len(rows_data) == 3
    by_ch = {r["_dim"]: r for r in rows_data}
    assert by_ch["meta-ads"]["claimed_conversions"] == pytest.approx(240.0, abs=0.1)
    assert by_ch["meta-ads"]["deduplicated_contribution"] == pytest.approx(150.0, abs=0.5)
    assert by_ch["google-ads"]["deduplicated_contribution"] == pytest.approx(112.5, abs=0.5)
    assert by_ch["linkedin-ads"]["deduplicated_contribution"] == pytest.approx(37.5, abs=0.5)

    # Sum of dedup contributions = verified_total (300)
    dedup_sum = sum(
        r["deduplicated_contribution"]
        for r in rows_data
        if r["deduplicated_contribution"] is not None
    )
    assert dedup_sum == pytest.approx(300.0, abs=1.0)


def test_dedup_part_pct_in_table():
    """Table carries part_pct column (Share) for each channel (Story 17.3)."""
    rows = _dedup_rows_multi_channel()
    with patch("core.warehouse.query_dedup_estimate", return_value=rows):
        _, envelope, _ = cards_module.get_card(
            [],
            "default",
            template="dedup",
            date_from="2026-07-04",
            date_to="2026-07-06",
        )
    table_block = envelope["data"]["composition"][2]
    rows_data = table_block["data"]["rows"]
    # Meta part = 150/300 = 50%
    meta_row = next(r for r in rows_data if r["_dim"] == "meta-ads")
    assert meta_row["part_pct"] == pytest.approx(50.0, abs=0.1)
    # All parts must be positive
    assert all(r["part_pct"] > 0 for r in rows_data if r["part_pct"] is not None)


def test_dedup_null_verified_rate_stays_null():
    """CRITICAL (AD-9): when verified is NULL, duplication_rate MUST be NULL, never 0.

    This covers stripe v1 BLOCKED (15.7) and absent régies. The NULL must propagate
    through the kpi_row and comment -- never presented as '0% duplication'.
    """
    rows = _dedup_rows_null_verified()
    with patch("core.warehouse.query_dedup_estimate", return_value=rows):
        _, envelope, _ = cards_module.get_card(
            [],
            "default",
            template="dedup",
            date_from="2026-07-05",
            date_to="2026-07-05",
        )

    dedup_meta = envelope["data"]["dedup_meta"]
    # Rate must be NULL (verified was NULL -> no honest rate)
    assert dedup_meta["duplication_rate"] is None
    # kpi_row value must also be NULL (never 0)
    kpi_block = envelope["data"]["composition"][0]
    kpi_value = kpi_block["data"]["metrics"][0]["value"]
    assert kpi_value is None, (
        "duplication_rate NULL must never be rendered as 0 (AD-9 -- jamais 0 deguise)"
    )

    # The comment must signal absence, never a rate
    comment = envelope["data"]["rendered_comment"]
    lowered = comment.lower()
    assert "indisponible" in lowered or "indetermine" in lowered or "absente" in lowered
    # Must not contain a numeric rate like '0,0x' or '0.0x'
    assert "0,0x" not in comment
    assert "0.0x" not in comment


def test_dedup_opt_out_project_designed_empty():
    """When no rows returned (project opted-out), card returns a designed empty envelope.

    The envelope must carry a legible message about missing source configuration,
    never crash, and never invent a rate (AD-9 -- Story 17.3 AC degrade).
    """
    with patch("core.warehouse.query_dedup_estimate", return_value=[]):
        _, envelope, widget_uri = cards_module.get_card(
            [],
            "default",
            template="dedup",
            date_from="2026-07-04",
            date_to="2026-07-06",
        )
    assert widget_uri == "ui://core/card-dedup"
    data = envelope["data"]
    assert data["card_id"] == "dedup"
    # dedup_meta must not contain an invented rate
    assert data["dedup_meta"]["duplication_rate"] is None
    # Comment must mention missing source
    comment = data["rendered_comment"]
    lowered = comment.lower()
    assert "source" in lowered or "verification" in lowered or "vérification" in lowered
    # Must not crash (degrade-never-raise)
    assert "composition" in data
    assert len(data["composition"]) == 4


def test_dedup_comment_cites_estimation_verbatim():
    """AD-9 non-négociable: the word 'estimation' appears in the comment (Story 17.3)."""
    rows = _dedup_rows_ga4()
    with patch("core.warehouse.query_dedup_estimate", return_value=rows):
        _, envelope, _ = cards_module.get_card(
            [],
            "default",
            template="dedup",
            date_from="2026-07-04",
            date_to="2026-07-06",
        )
    comment = envelope["data"]["rendered_comment"]
    assert "estimation" in comment.lower(), (
        "AD-9 non-négociable: 'estimation' must appear verbatim in the dedup comment"
    )
    assert "Contexte manquant" in comment


def test_dedup_comment_cites_formula_and_source():
    """The comment must cite the formula and the source of truth (AD-9)."""
    rows = _dedup_rows_ga4()
    with patch("core.warehouse.query_dedup_estimate", return_value=rows):
        _, envelope, _ = cards_module.get_card(
            [],
            "default",
            template="dedup",
            date_from="2026-07-04",
            date_to="2026-07-06",
        )
    comment = envelope["data"]["rendered_comment"]
    # Formula cited
    assert "revendiqué" in comment.lower() or "réel" in comment.lower()
    # Source cited (ga4 in this fixture)
    assert "ga4" in comment.lower()


def test_dedup_bar_block_has_series():
    """Bar block has a 'series' key with claimed and deduplicated entries (Story 17.3)."""
    rows = _dedup_rows_multi_channel()
    with patch("core.warehouse.query_dedup_estimate", return_value=rows):
        _, envelope, _ = cards_module.get_card(
            [],
            "default",
            template="dedup",
            date_from="2026-07-04",
            date_to="2026-07-06",
        )
    bar_block = envelope["data"]["composition"][1]
    assert bar_block["type"] == "bar"
    series = bar_block["data"]["series"]
    assert len(series) == 2
    metrics = {s["metric"] for s in series}
    assert "claimed_conversions" in metrics
    assert "deduplicated_contribution" in metrics
    # estimate_label in bar data (AD-9)
    assert bar_block["data"].get("estimate_label") == "Estimation"
