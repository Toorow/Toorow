"""Story 37.9: the MDM bridge for geography.

Covers the repair path (unmapped -> governed suggestion -> confirmed -> reclassified
at read time with no fact rewrite), cross-project isolation of a client decision,
the DQ surfacing, and the anti-hardcode rule.
"""

from __future__ import annotations

from core import geographic_conformance as gc
from core.country_vocabulary import CANONICAL_COUNTRY_DIMENSION, normalize_country_value
from core.geographic_reporting import GeographicPosture, Market
from core.geographic_semantics import UNKNOWN_MARKET, group_market_reporting_rows

LOCAL = "local_markets"


def _posture() -> GeographicPosture:
    return GeographicPosture(
        mode=LOCAL,
        markets=(Market(id="hexagone", label="Hexagone", country_codes=("FR",)),),
    )


def _row(value: str, connector: str = "acme-source") -> dict:
    return {
        "project_id": "prj_1",
        "date": "2026-07-01",
        "connector": connector,
        "metric": "clicks",
        "breakdown_dimension": "country",
        "breakdown_value": value,
        "value": 10,
        "pull_id": "pull_1",
        "loaded_at": "2026-07-02",
    }


# ---------------------------------------------------------------------------
# The dead end and its repair.
# ---------------------------------------------------------------------------


def test_unknown_spelling_is_not_resolved_by_the_shared_seed() -> None:
    """The premise: the seed does not know one client's provider spelling."""

    assert normalize_country_value("Zzz Not A Country") is None


def test_unmapped_value_stays_unknown_and_emits_repairable_evidence() -> None:
    result = group_market_reporting_rows([_row("Zzz Not A Country")], _posture())

    assert [row["market_id"] for row in result.rows] == [UNKNOWN_MARKET]
    assert len(result.data_quality) == 1
    evidence = result.data_quality[0]
    assert evidence["code"] == "country_value_unmapped"
    # The evidence now names WHERE the repair happens instead of dead-ending.
    assert evidence["repair_path"] == "dimension_conformance"
    assert evidence["canonical_dimension"] == CANONICAL_COUNTRY_DIMENSION


def test_confirmed_client_mapping_reclassifies_at_read_without_touching_facts() -> None:
    """A resolution repaired ONCE reclassifies retained rows on the next read."""

    facts = [_row("Frankreich")]
    before = group_market_reporting_rows(facts, _posture())
    assert [row["market_id"] for row in before.rows] == [UNKNOWN_MARKET]

    resolver = gc.make_country_resolver(
        conformance={("acme-source", "Frankreich"): "FR"}
    )
    after = group_market_reporting_rows(facts, _posture(), resolver=resolver)

    assert [row["market_id"] for row in after.rows] == ["hexagone"]
    assert after.data_quality == ()
    # No fact rewrite: the input rows are untouched.
    assert facts == [_row("Frankreich")]


def test_client_decision_is_scoped_and_does_not_leak_to_another_project() -> None:
    """Project A's spelling decision is invisible to project B (cross-project isolation)."""

    project_a = gc.make_country_resolver(conformance={("acme-source", "Frankreich"): "FR"})
    project_b = gc.make_country_resolver(conformance={})

    assert project_a("Frankreich", "acme-source") == "FR"
    assert project_b("Frankreich", "acme-source") is None


def test_resolution_is_per_connector_not_global() -> None:
    resolver = gc.make_country_resolver(conformance={("source-a", "Frankreich"): "FR"})
    assert resolver("Frankreich", "source-a") == "FR"
    assert resolver("Frankreich", "source-b") is None


def test_confirmed_mapping_outside_the_iso_set_is_refused() -> None:
    """The seed owns the LEGAL set; a client mapping cannot invent a value."""

    resolver = gc.make_country_resolver(
        conformance={("acme-source", "Frankreich"): "Freedonia"}
    )
    assert resolver("Frankreich", "acme-source") is None


def test_seed_always_wins_over_a_client_mapping() -> None:
    """A client cannot silently re-point a value the platform already knows."""

    resolver = gc.make_country_resolver(conformance={("acme-source", "FR"): "DE"})
    assert resolver("FR", "acme-source") == "FR"


# ---------------------------------------------------------------------------
# Suggestions: evidence-backed, never invented, never auto-confirmed.
# ---------------------------------------------------------------------------


def test_a_near_spelling_produces_a_scored_candidate() -> None:
    candidates = gc.score_country_candidates("Frnce")
    assert candidates
    assert candidates[0].canonical_code == "FR"
    assert 0.0 < candidates[0].score <= 1.0


def test_a_value_with_no_evidence_produces_no_candidate() -> None:
    assert gc.score_country_candidates("Zzzqqq Wxyv") == ()
    assert gc.score_country_candidates("") == ()
    assert gc.score_country_candidates(None) == ()


def test_suggestions_are_only_built_for_evidenced_values() -> None:
    items = gc.aggregate_unmapped_evidence(
        [
            {"raw_value": "Frnce", "connector": "acme-source"},
            {"raw_value": "Zzzqqq Wxyv", "connector": "acme-source"},
        ]
    )
    states = {item.source_value: item.state for item in items}
    assert states["Frnce"] == gc.STATE_SUGGESTED
    assert states["Zzzqqq Wxyv"] == gc.STATE_UNRESOLVED

    suggestions = gc.build_country_suggestions(items)
    # The value with no evidence produces NOTHING -- we never fabricate a canonical.
    assert [s.source_value for s in suggestions] == ["Frnce"]
    assert suggestions[0].canonical_value == "FR"


def test_persisted_suggestions_are_project_scoped_and_never_confirmed(monkeypatch) -> None:
    captured: dict = {}

    def _fake_persist(suggestions, **kwargs):
        captured["suggestions"] = suggestions
        captured["kwargs"] = kwargs
        return {"proposed": len(suggestions), "skipped": 0, "unchanged": 0}

    import core.dimension_conformance as dcm

    monkeypatch.setattr(dcm, "persist_suggestions", _fake_persist)

    items = gc.aggregate_unmapped_evidence([{"raw_value": "Frnce", "connector": "acme"}])
    counts = gc.register_unmapped_country_values("prj_1", items)

    assert counts["proposed"] == 1
    assert captured["kwargs"]["scope_level"] == "PROJECT"
    assert captured["kwargs"]["project_id"] == "prj_1"
    assert captured["kwargs"]["org_id"] is None
    assert captured["kwargs"]["canonical_dimension"] == CANONICAL_COUNTRY_DIMENSION
    # persist_suggestions is the ONLY writer, and it writes status='proposed'.
    # Nothing in this module ever calls confirm_mapping / set_mapping_manual.
    assert not hasattr(gc, "confirm_mapping")


def test_evidence_aggregation_counts_occurrences() -> None:
    items = gc.aggregate_unmapped_evidence(
        [
            {"raw_value": "Frnce", "connector": "acme"},
            {"raw_value": "Frnce", "connector": "acme"},
            {"raw_value": "Frnce", "connector": "other", "occurrences": 5},
        ]
    )
    by_key = {(i.connector, i.source_value): i.occurrences for i in items}
    assert by_key[("acme", "Frnce")] == 2
    assert by_key[("other", "Frnce")] == 5


# ---------------------------------------------------------------------------
# DQ supervision surfacing.
# ---------------------------------------------------------------------------


def test_dq_firing_payload_is_none_when_nothing_is_unmapped() -> None:
    assert gc.build_dq_firing_payload(()) is None


def test_dq_firing_payload_carries_the_gap_and_the_repair_surface() -> None:
    items = gc.aggregate_unmapped_evidence(
        [
            {"raw_value": "Frnce", "connector": "acme", "occurrences": 3},
            {"raw_value": "Zzzqqq Wxyv", "connector": "acme", "occurrences": 1},
        ]
    )
    payload = gc.build_dq_firing_payload(items, window_date="2026-07-25")
    assert payload is not None
    meta = payload["metadata"]
    assert meta["evidence_code"] == "country_value_unmapped"
    assert meta["distinct_unmapped_values"] == 2
    assert meta["unmapped_row_count"] == 4
    assert meta["without_candidate"] == 1
    assert meta["repair"]["surface"] == "dimension_conformance"
    assert meta["repair"]["scope_level"] == "PROJECT"


def test_geography_alert_type_is_registered_on_the_dq_surfaces() -> None:
    """Epic 13 monitors and get_data_quality_report filter on this registry."""

    from core import dq_api

    assert gc.DQ_GEOGRAPHY_ALERT_TYPE.startswith("dq_")
    assert gc.DQ_GEOGRAPHY_ALERT_TYPE in dq_api._DQ_TYPES
    assert gc.DQ_GEOGRAPHY_ALERT_TYPE in dq_api._MONITOR_LABELS


def test_emit_country_dq_firing_writes_one_dq_row(monkeypatch) -> None:
    written: list[dict] = []

    from core import infra_alerts

    monkeypatch.setattr(
        infra_alerts,
        "write_infra_firing",
        lambda **kwargs: written.append(kwargs),
    )

    items = gc.aggregate_unmapped_evidence([{"raw_value": "Frnce", "connector": "acme"}])
    assert gc.emit_country_dq_firing("prj_1", items) is True
    assert len(written) == 1
    assert written[0]["alert_type"] == gc.DQ_GEOGRAPHY_ALERT_TYPE
    assert written[0]["project_id"] == "prj_1"
    assert written[0]["metric"] == "country_value_unmapped"


def test_record_evidence_can_skip_the_firing_for_the_read_path(monkeypatch) -> None:
    """A report may render many times a day; one alert per render would be noise."""

    monkeypatch.setattr(gc, "register_unmapped_country_values", lambda *a, **k: {})
    fired: list = []
    monkeypatch.setattr(gc, "emit_country_dq_firing", lambda *a, **k: fired.append(1))

    out = gc.record_unmapped_country_evidence(
        "prj_1", [{"raw_value": "Frnce", "connector": "acme"}], emit_firing=False
    )
    assert out["fired"] is False
    assert fired == []


def test_record_evidence_is_a_no_op_without_evidence() -> None:
    out = gc.record_unmapped_country_evidence("prj_1", [])
    assert out["items"] == ()
    assert out["fired"] is False


def test_conformance_load_failure_degrades_to_seed_only(monkeypatch) -> None:
    """An unavailable MDM layer means more Unknown -- never a guess, never a crash."""

    import core.dimension_conformance as dcm

    def _boom(*_a, **_k):
        raise RuntimeError("store down")

    monkeypatch.setattr(dcm, "resolve_dimension_conformance", _boom)
    resolver = gc.make_country_resolver(project_id="prj_1")
    assert resolver("FR", "acme") == "FR"           # seed still works
    assert resolver("Frankreich", "acme") is None    # honest Unknown
