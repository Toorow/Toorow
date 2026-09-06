"""Focused CAP-16 contract over the canonical Golden Questions harness.

This file does not duplicate the dataset. It proves that the real corpus and the
shared runner/gate keep SQL accuracy, pull provenance and AD-18 adherence wired.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import yaml

REPO = Path(__file__).resolve().parents[3]
for path in (REPO / "server", REPO / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import eval_gate as G  # noqa: E402
import run_evals as R  # noqa: E402

CORPUS_PATH = Path(__file__).with_name("corpus.yaml")

_CONTRACT_DDLS = (
    """CREATE TABLE marts.anomalies_daily (
        project_id VARCHAR, connector VARCHAR, metric VARCHAR, date VARCHAR,
        observed_value DOUBLE, expected_value DOUBLE, rolling_stddev DOUBLE, zscore DOUBLE
    )""",
    """CREATE TABLE marts.cross_source_conversions (
        project_id VARCHAR, date VARCHAR, conversions_total DOUBLE,
        attribution_source VARCHAR, pull_id VARCHAR
    )""",
    """CREATE TABLE marts.dedup_estimate (
        project_id VARCHAR, date VARCHAR, channel_connector VARCHAR,
        claimed_conversions DOUBLE, verified_total DOUBLE, claimed_total DOUBLE,
        verification_source_type VARCHAR, verification_source_id VARCHAR,
        lead_event_name VARCHAR, duplication_rate DOUBLE,
        deduplicated_contribution DOUBLE, estimate_label VARCHAR, pull_id VARCHAR
    )""",
    """CREATE TABLE marts.fact_daily_kpi (
        project_id VARCHAR, date VARCHAR, connector VARCHAR, metric VARCHAR,
        breakdown_dimension VARCHAR, breakdown_value VARCHAR, value DOUBLE,
        native_value DECIMAL(38,9), native_currency VARCHAR, native_unit VARCHAR,
        money_gap_code VARCHAR, fx_rate DECIMAL(38,9), fx_as_of_date DATE,
        fx_source VARCHAR, fx_tier VARCHAR, fx_method VARCHAR,
        pull_id VARCHAR, loaded_at VARCHAR
    )""",
    """CREATE TABLE marts.metric_baselines (
        project_id VARCHAR, connector VARCHAR, metric VARCHAR, date VARCHAR,
        metric_value DOUBLE, rolling_mean DOUBLE, rolling_stddev DOUBLE,
        observation_count BIGINT
    )""",
    """CREATE TABLE marts.semantic_avg_position (
        project_id VARCHAR, date VARCHAR, connector VARCHAR,
        breakdown_dimension VARCHAR, breakdown_value VARCHAR,
        average_position DOUBLE, impressions_weight HUGEINT,
        semantic_weight HUGEINT, pull_id VARCHAR, loaded_at VARCHAR
    )""",
    """CREATE TABLE marts.semantic_ctr (
        project_id VARCHAR, date VARCHAR, connector VARCHAR,
        breakdown_dimension VARCHAR, breakdown_value VARCHAR,
        ctr DOUBLE, pull_id VARCHAR
    )""",
    # The entity cross-read (epics 68-69). Shape taken from the BUILT view on
    # 2026-08-24 -- `information_schema.columns` over
    # `dbt/models/marts/semantic_fact_by_entity_attribute.sql` -- and not from the
    # model's text: `date` is DATE here and VARCHAR nowhere, which is the exact
    # distinction AI-303 found the hard way. These DDLs are a HAND-KEPT copy of
    # the marts contract, so a column renamed in dbt makes the question below fail
    # here first; that is the point of the file, not a defect of it.
    """CREATE TABLE marts.semantic_fact_by_entity_attribute (
        project_id VARCHAR, date DATE, connector VARCHAR, metric VARCHAR,
        entity_dimension VARCHAR, entity_key VARCHAR, object_kind VARCHAR,
        node_id VARCHAR, resolution_state VARCHAR, attribute VARCHAR,
        attribute_value VARCHAR, attribute_origin VARCHAR,
        rule_set_version_id VARCHAR, value DOUBLE, pull_id VARCHAR
    )""",
)


def _contract_connection():
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA marts")
    for ddl in _CONTRACT_DDLS:
        conn.execute(ddl)
    return conn


def _record(question_id: str, *, adherence: str) -> dict:
    return {
        "id": question_id,
        "surface": "daily_report",
        "ground_truth": R.PASS,
        "accuracy": R.PASS,
        "citations": R.PASS,
        "adherence": adherence,
        "tool_replay": "done",
        "notes": [],
    }


def _artifact(records: list[dict]) -> dict:
    return {
        "corpus": {
            "schema_version": "1",
            "as_of_anchor": "2026-07-15",
            "seeds_commit": "fixture",
        },
        "summary": R._summarise(records),
        "results": records,
    }


def test_canonical_corpus_is_the_single_50_question_source():
    corpus = yaml.safe_load(CORPUS_PATH.read_text(encoding="utf-8"))
    questions = corpus["questions"]
    conn = _contract_connection()

    assert len(questions) >= 50
    assert len({question["id"] for question in questions}) == len(questions)

    fact_surface_count = 0
    pull_required_count = 0
    for question in questions:
        canonical = R.canonical_query(question)
        assert canonical is not None, f"{question['id']} has no canonical reference SQL"
        assert canonical["reference_sql"].strip()
        assert canonical["fixture_sha256"].strip()
        conn.execute(canonical["reference_sql"]).fetchall()
        if "fact_daily_kpi" in canonical["reference_sql"]:
            fact_surface_count += 1

        citations = question.get("expected_citations")
        assert isinstance(citations, list), f"{question['id']} has no citation contract"
        for citation in citations:
            assert citation.get("source_system")
            assert citation.get("source_field")
            assert isinstance(citation.get("pull_id_required", False), bool)
        if any(citation.get("pull_id_required") for citation in citations):
            pull_required_count += 1
    conn.close()

    assert fact_surface_count >= 40
    assert pull_required_count >= 30


def test_sql_provenance_and_adherence_use_evidence_not_claims():
    sql_verdict, expected, actual = R.compare_result([{"total": 42}], 42, "fact_sum", None)
    assert (sql_verdict, expected, actual) == (R.PASS, 42, 42)

    citation_contract = [
        {
            "source_system": "google-analytics",
            "source_field": "fact_daily_kpi",
            "pull_id_required": True,
        }
    ]
    missing_pull, _ = R.score_citations(
        [
            {
                "source_system": "google-analytics",
                "source_field": "fact_daily_kpi",
                "pull_id": None,
            }
        ],
        citation_contract,
    )
    cited, _ = R.score_citations(
        [
            {
                "source_system": "google-analytics",
                "source_field": "fact_daily_kpi",
                "pull_id": "pull_01",
            }
        ],
        citation_contract,
    )
    assert missing_pull == R.FAIL
    assert cited == R.PASS

    claimed, claimed_detail = R.score_adherence({"gate": {"adherent": True}})
    verified, verified_detail = R.score_adherence(
        {"gate": {"adherent": True, "context_tool": "search_context"}}
    )
    assert claimed == R.FAIL
    assert claimed_detail["reported_adherent"] is True
    assert claimed_detail["adherent"] is False
    assert verified == R.PASS
    assert verified_detail["context_tool"] == "search_context"


def test_adherence_regression_blocks_and_missing_evidence_never_turns_green():
    baseline = G.build_baseline(_artifact([_record("q1", adherence=R.PASS)]))

    regressed = G.compare_to_baseline(_artifact([_record("q1", adherence=R.FAIL)]), baseline)
    assert regressed["verdict"] == G.FAIL
    assert any(
        item["id"] == "q1" and item["dim"] == "adherence" for item in regressed["regressions"]
    )

    unverifiable = G.compare_to_baseline(
        _artifact([_record("q1", adherence=R.UNAVAILABLE)]), baseline
    )
    assert unverifiable["verdict"] == G.UNVERIFIABLE
    warning_kinds = {warning["kind"] for warning in unverifiable["warnings"]}
    assert "evidence_unavailable" in warning_kinds
    assert "score_unverifiable" in warning_kinds
