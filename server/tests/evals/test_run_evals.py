"""Story 14.2 -- HARNESS self-tests for scripts/run_evals.py.

These tests prove the runner is HONEST: they exercise the pure scoring functions
offline (no DB, no MCP server) for the discrimination / citation / adherence / skip
guarantees, and DuckDB/seam-gate the end-to-end path exactly like
test_reference_sql_green.py (TOOROW_DUCKDB_PATH gate).

Contract proven here:
  * determinism        -- two runner passes produce identical scores;
  * discrimination     -- feeding a naive_wrong result where canonical_correct is
                          expected returns FAIL (the anti-tolerance proof), at the
                          pure-function level (no live warehouse);
  * citation           -- a provenance list missing an expected citation FAILs;
                          a missing REQUIRED pull_id FAILs; expected_citations=[] is
                          N/A (not 0);
  * adherence          -- absent meta.gate -> 'unavailable' (never a false 100%);
  * skip accounting    -- a question with tool_invocation=null is counted 'skipped',
                          listed, and excluded from the green total.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

# Wire imports: the runner lives under repo scripts/, core.* under server/.
_REPO = Path(__file__).resolve().parents[3]
for _p in (str(_REPO / "server"), str(_REPO / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import run_evals as R  # noqa: E402


# ---------------------------------------------------------------------------
# DuckDB gate helpers (mirror test_reference_sql_green.py).
# ---------------------------------------------------------------------------
def _duckdb_available() -> bool:
    return R.get_duckdb_path() is not None


def _e2e_enabled() -> bool:
    """The end-to-end seam pass is EXPLICIT opt-in, not auto-run.

    It drives the real in-process MCP tool (get_daily_report), which touches Postgres
    (briefing / module-enablement / project resolve) -- absent a live PG stack those
    connects each wait out a TCP timeout, so a bare ``pytest server/tests/evals`` would
    crawl. It also asserts zero corpus fixture drift, which only holds when the local
    seed matches the committed fixtures (seeds_commit in corpus.yaml). So it runs only
    when TOOROW_EVALS_E2E=1 AND the seed is present -- a documented local pre-deploy
    pass (same discipline as the repo's pg-gated suites), never in the offline default.
    """
    return os.environ.get("TOOROW_EVALS_E2E") == "1" and _duckdb_available()


# ---------------------------------------------------------------------------
# Pure-function: compare_result -- accuracy + DISCRIMINATION (anti-tolerance).
# ---------------------------------------------------------------------------
def test_compare_fact_sum_exact_pass():
    # Canonical scalar reference row.
    ref = [{"total_sessions": 1234}]
    verdict, expected, actual = R.compare_result(ref, 1234.0, "fact_sum", None)
    assert verdict == R.PASS
    assert expected == 1234


def test_compare_fact_sum_naive_wrong_scores_fail():
    """DISCRIMINATION: a naive_wrong result (grain-inflated multiple) must score FAIL
    when compared as if it were the canonical answer. Proven WITHOUT the live warehouse.
    """
    canonical = [{"total_sessions": 1000}]
    naive_wrong_value = 3000.0  # e.g. 3x inflation from summing all breakdown series
    verdict, _, _ = R.compare_result(canonical, naive_wrong_value, "fact_sum", None)
    assert verdict == R.FAIL, "naive_wrong (grain-inflated) must NOT pass as canonical"


def test_compare_fact_sum_tolerance_cannot_swallow_grain_error():
    """The float tolerance is far smaller than any grain multiple: an off-by-one-unit
    difference on a rounded scalar fails, and a 2x inflation certainly fails."""
    canonical = [{"avg_position": 4.55}]
    # within tight tolerance -> pass
    assert R.compare_result(canonical, 4.55, "fact_sum", 2)[0] == R.PASS
    # a hair beyond tolerance -> fail
    assert R.compare_result(canonical, 4.60, "fact_sum", 2)[0] == R.FAIL
    # grain doubling -> fail
    assert R.compare_result(canonical, 9.10, "fact_sum", 2)[0] == R.FAIL


def test_num_equal_absorbs_reduction_noise_but_not_grain_or_offbyone():
    """REVIEW-CRITICAL fix: DuckDB SUM(value) (ground truth) and the runner's Python
    re-sum reduce in different orders -> IEEE-754 trailing-bit noise on fractional
    metrics. That noise must be absorbed (else a CORRECT answer scores a false red), but
    the tolerance must still fail an off-by-one unit and any grain multiple."""
    # same value, summation-order noise (observed on the real seed for cost) -> equal.
    assert R._num_equal(19358.474399999974, 19358.474399999977, None)
    # integer metric sums exactly -> equal.
    assert R._num_equal(12345, 12345, None)
    # off-by-one unit -> FAIL (tolerance is orders of magnitude below 1).
    assert not R._num_equal(1000.0, 1001.0, None)
    # 2x grain inflation -> FAIL.
    assert not R._num_equal(1000.0, 2000.0, None)
    # None still matches only None (not 0).
    assert not R._num_equal(None, 0.0, None)


def test_error_envelope_accuracy_is_terminal_fail(monkeypatch):
    """REVIEW-MEDIUM fix: a tool is_error=True must be a TERMINAL accuracy FAIL, never
    overwritten by a subsequent compare. An error envelope has no data.rows (selector ->
    None); if the reference were also None the old fall-through false-PASSed the error."""
    q = {
        "id": "q_err",
        "surface": "daily_report",
        "expected_citations": [],
        "reference_queries": [
            {"role": "canonical_correct", "reference_sql": "SELECT 1", "fixture_sha256": "x"}
        ],
        "tool_invocation": {
            "tool": "get_daily_report",
            "args": {
                "project_id": "default",
                "connectors": ["gsc"],
                "date_range": {"start": "2026-06-01", "end": "2026-06-02"},
            },
            "result_selector": {
                "kind": "fact_sum",
                "connector": "gsc",
                "metric": "clicks",
                "breakdown_dimension": "page",
                "round": None,
            },
        },
    }
    # Error envelope, no data.rows; conn=None so the reference is None too (the false-PASS trap).
    monkeypatch.setattr(R, "call_tool", lambda tool, args: ({"meta": {}}, None, True))
    rec = R.score_question(q, conn=None, run_replay=True, as_of_override=None)
    assert rec["accuracy"] == R.FAIL, "is_error must be terminal FAIL, never PASS/SKIPPED"


def test_compare_fact_sum_none_matches_only_none():
    """An out-of-range SUM (None) matches only None -- not 0, not any value."""
    ref = [{"total_clicks": None}]
    assert R.compare_result(ref, None, "fact_sum", None)[0] == R.PASS
    assert R.compare_result(ref, 0.0, "fact_sum", None)[0] == R.FAIL


def test_compare_fact_sum_by_date_series():
    ref = [
        {"date": "2026-06-01", "sessions": 10},
        {"date": "2026-06-02", "sessions": 20},
    ]
    good = [("2026-06-01", 10.0), ("2026-06-02", 20.0)]
    assert R.compare_result(ref, good, "fact_sum_by_date", None)[0] == R.PASS
    # wrong value on one day
    bad = [("2026-06-01", 10.0), ("2026-06-02", 21.0)]
    assert R.compare_result(ref, bad, "fact_sum_by_date", None)[0] == R.FAIL
    # missing a day
    short = [("2026-06-01", 10.0)]
    assert R.compare_result(ref, short, "fact_sum_by_date", None)[0] == R.FAIL


# ---------------------------------------------------------------------------
# Pure-function: select_from_envelope -- re-aggregates on the PINNED grain only,
# so a naive over-all-breakdowns sum is impossible.
# ---------------------------------------------------------------------------
def _fact_rows():
    # Two parallel session series for the SAME days: 'country' (canonical) and
    # 'device_category' (a second full copy). A grain-naive sum would double.
    rows = []
    for d in ("2026-06-01", "2026-06-02"):
        rows.append(
            {
                "date": d,
                "connector": "google-analytics",
                "metric": "sessions",
                "breakdown_dimension": "country",
                "breakdown_value": "FR",
                "value": 100.0,
                "pull_id": "p1",
            }
        )
        rows.append(
            {
                "date": d,
                "connector": "google-analytics",
                "metric": "sessions",
                "breakdown_dimension": "device_category",
                "breakdown_value": "mobile",
                "value": 100.0,
                "pull_id": "p1",
            }
        )
    return {"data": {"rows": rows}, "meta": {}}


def test_selector_fact_sum_pins_breakdown():
    env = _fact_rows()
    selector = {
        "kind": "fact_sum",
        "connector": "google-analytics",
        "metric": "sessions",
        "breakdown_dimension": "country",
        "round": None,
    }
    # country-only sum = 100+100 = 200 (NOT 400 across both breakdowns).
    assert R.select_from_envelope(env, selector) == 200.0


def test_selector_fact_sum_by_date_pins_breakdown():
    env = _fact_rows()
    selector = {
        "kind": "fact_sum_by_date",
        "connector": "google-analytics",
        "metric": "sessions",
        "breakdown_dimension": "country",
        "round": None,
    }
    assert R.select_from_envelope(env, selector) == [("2026-06-01", 100.0), ("2026-06-02", 100.0)]


def test_the_selector_reads_the_rows_from_the_app_channel_when_they_moved():
    """The model channel routes datasets into `_meta`; the selector follows.

    Measured 2026-08-23 against the real seam: `get_daily_report` answered with
    `data.rows = {"withheld": "moved_to_app_channel", "row_count": 5540, ...}`
    and the 5540 rows on the wire meta. The selector read the descriptor, hit
    `'str' object has no attribute 'get'`, and the runner scored accuracy FAIL on
    all 20 replayed questions -- a red about the instrument, printed as a red
    about the product.
    """
    withheld = {"data": {"rows": {"withheld": "moved_to_app_channel", "row_count": 4}}}
    app_payload = {"rows": _fact_rows()["data"]["rows"], "__tool__": "get_daily_report"}
    selector = {
        "kind": "fact_sum",
        "connector": "google-analytics",
        "metric": "sessions",
        "breakdown_dimension": "country",
        "round": None,
    }

    assert R.select_from_envelope(withheld, selector, app_payload) == 200.0


def test_rows_that_moved_with_no_app_payload_are_UNREACHABLE_not_empty():
    """The difference between "could not read" and "the product returned nothing".

    Returning an empty list here would score as a wrong answer. The note is what
    lets `score_question` mark the dimension unavailable instead.
    """
    withheld = {"data": {"rows": {"withheld": "moved_to_app_channel", "row_count": 4}}}

    rows, note = R.rows_for_selection(withheld, None)

    assert rows == []
    assert note is not None
    assert "moved_to_app_channel" in note and "row_count=4" in note


def test_rows_still_in_the_envelope_are_read_where_they_are():
    """The split is conditional, so the old channel must keep working."""
    rows, note = R.rows_for_selection(_fact_rows(), None)

    assert note is None
    assert len(rows) == 4


def test_an_unexpected_rows_shape_is_named_rather_than_treated_as_empty():
    rows, note = R.rows_for_selection({"data": {"rows": "nonsense"}}, None)

    assert rows == []
    assert note == "unexpected rows shape: str"


def test_the_runner_consults_context_BEFORE_the_data_query():
    """AD-18 is a property of a session, so the runner has to be one.

    Measured 2026-08-23: with no consult, `gate.adherent` was false on every
    replayed question and adherence scored 0% -- a red that said the HARNESS
    consulted no context. The order is the assertion: a consult after the data
    query proves nothing about a gate that already answered.
    """
    calls: list[str] = []

    def _record(tool, args):
        calls.append(tool)
        if tool == R.CONTEXT_TOOL_FOR_ADHERENCE:
            return ({}, None, False)
        return (
            {"data": {"rows": []}, "meta": {"gate": {"adherent": True, "context_tool": tool}}},
            None,
            False,
        )

    question = _question_with_expected_business_path()
    with mock.patch.object(R, "call_tool", _record):
        R.score_question(question, conn=None, run_replay=True, as_of_override=None)

    assert calls[0] == R.CONTEXT_TOOL_FOR_ADHERENCE, f"consult must come first, got {calls}"
    assert calls[1] == "get_daily_report"


def test_a_consult_that_cannot_run_leaves_adherence_UNAVAILABLE_not_FAIL():
    """Without the consult the gate is RIGHT to say `adherent: false`.

    Scoring that as FAIL blames the product for the runner's silence -- the exact
    inversion this dimension was producing.
    """
    def _record(tool, args):
        if tool == R.CONTEXT_TOOL_FOR_ADHERENCE:
            raise RuntimeError("context layer unreachable")
        return (
            {"data": {"rows": []}, "meta": {"gate": {"adherent": False, "context_tool": None}}},
            None,
            False,
        )

    question = _question_with_expected_business_path()
    with mock.patch.object(R, "call_tool", _record):
        record = R.score_question(question, conn=None, run_replay=True, as_of_override=None)

    assert record["adherence"] == R.UNAVAILABLE
    assert any("context consult raised" in n for n in record["notes"])


def test_the_consult_tool_is_one_the_gate_actually_accepts():
    """The pair must not drift: a consult tool outside the accepted set would make
    the dimension permanently red while looking like it was being measured."""
    assert R.CONTEXT_TOOL_FOR_ADHERENCE in R.ADHERENCE_CONTEXT_TOOLS


def test_a_required_pull_id_is_NA_when_the_answer_has_no_rows():
    """No pull contributed a row, so there is no pull to name.

    Two questions of the shipped corpus are pinned to an EMPTY fixture and also
    require a `pull_id` (`daily_report_freshness_edge`, `as_of_meta_cost_replay`).
    That is a contradiction inside the corpus, not a defect of the product, and
    scoring it FAIL teaches a reader to ignore the citation column.
    """
    expected = [
        {"source_system": "gsc", "source_field": "fact_daily_kpi", "pull_id_required": True}
    ]
    provenance = [{"source_system": "gsc", "source_field": "fact_daily_kpi", "pull_id": None}]

    verdict, notes = R.score_citations(provenance, expected, answer_is_empty=True)

    assert verdict == R.NA
    assert any("no rows" in n for n in notes)


def test_a_missing_pull_id_on_a_NON_empty_answer_is_still_a_FAIL():
    """The exemption is about emptiness, and must not become a way out."""
    expected = [
        {"source_system": "gsc", "source_field": "fact_daily_kpi", "pull_id_required": True}
    ]
    provenance = [{"source_system": "gsc", "source_field": "fact_daily_kpi", "pull_id": None}]

    verdict, _ = R.score_citations(provenance, expected, answer_is_empty=False)

    assert verdict == R.FAIL


def test_an_empty_answer_still_FAILs_a_citation_that_is_absent_entirely():
    """Emptiness excuses an unnameable pull, never a missing source."""
    expected = [{"source_system": "gsc", "source_field": "fact_daily_kpi"}]

    verdict, _ = R.score_citations([], expected, answer_is_empty=True)

    assert verdict == R.FAIL


def test_a_degraded_run_says_WHICH_store_is_missing_and_how_to_start_one():
    """"Offline" was only half true, and a reader could not tell the halves apart.

    The seam drives the real tool, which reads a platform Postgres for the
    briefing, the module filter, the branding and the adherence gate. A run
    without one still produces numbers -- and they mean less than they say.
    """
    artifact = {
        "summary": R._summarise([], []),
        "results": [],
        "corpus": {"schema_version": "1"},
        "config": {
            "duckdb_available": True,
            "replay_ran": True,
            "platform_db_reachable": False,
            "only": None,
            "as_of_override": None,
        },
    }

    text = R.render_summary(artifact)

    assert "platform_db_reachable : False" in text
    assert "disposable_postgres.py up" in text, "an unreachable store must name the gesture"


def test_a_reachable_store_is_stated_without_the_remedy():
    """The line stays -- a green run must say what it was green AGAINST."""
    artifact = {
        "summary": R._summarise([], []),
        "results": [],
        "corpus": {"schema_version": "1"},
        "config": {
            "duckdb_available": True,
            "replay_ran": True,
            "platform_db_reachable": True,
            "only": None,
            "as_of_override": None,
        },
    }

    text = R.render_summary(artifact)

    assert "platform_db_reachable : True" in text
    assert "disposable_postgres.py up" not in text


def test_a_run_with_no_replay_claims_nothing_about_the_platform_store():
    """No tool call was made, so its reachability is not a fact this run has."""
    artifact = {
        "summary": R._summarise([], []),
        "results": [],
        "corpus": {"schema_version": "1"},
        "config": {
            "duckdb_available": False,
            "replay_ran": False,
            "platform_db_reachable": False,
            "only": None,
            "as_of_override": None,
        },
    }

    assert "platform_db_reachable" not in R.render_summary(artifact)


def test_selector_fact_sum_empty_is_none():
    env = {"data": {"rows": []}, "meta": {}}
    selector = {
        "kind": "fact_sum",
        "connector": "gsc",
        "metric": "clicks",
        "breakdown_dimension": "page",
        "round": None,
    }
    assert R.select_from_envelope(env, selector) is None


# ---------------------------------------------------------------------------
# Pure-function: score_citations (AD-9).
# ---------------------------------------------------------------------------
def test_citation_pass_when_present_with_pull_id():
    prov = [{"source_system": "gsc", "source_field": "fact_daily_kpi", "pull_id": "pull_9"}]
    exp = [{"source_system": "gsc", "source_field": "fact_daily_kpi", "pull_id_required": True}]
    assert R.score_citations(prov, exp)[0] == R.PASS


def test_citation_fail_when_missing():
    prov = [{"source_system": "meta-ads", "source_field": "fact_daily_kpi", "pull_id": "x"}]
    exp = [{"source_system": "gsc", "source_field": "fact_daily_kpi", "pull_id_required": False}]
    assert R.score_citations(prov, exp)[0] == R.FAIL


def test_citation_fail_when_required_pull_id_missing():
    prov = [{"source_system": "gsc", "source_field": "fact_daily_kpi", "pull_id": None}]
    exp = [{"source_system": "gsc", "source_field": "fact_daily_kpi", "pull_id_required": True}]
    assert R.score_citations(prov, exp)[0] == R.FAIL


def test_citation_fail_when_wrong_source_field():
    prov = [{"source_system": "gsc", "source_field": "fact_daily_kpi", "pull_id": "x"}]
    exp = [
        {"source_system": "gsc", "source_field": "semantic_avg_position", "pull_id_required": False}
    ]
    assert R.score_citations(prov, exp)[0] == R.FAIL


def test_citation_na_when_no_citations_expected():
    verdict, _ = R.score_citations([], [])
    assert verdict == R.NA, "expected_citations=[] must be N/A, not 0/FAIL"


# ---------------------------------------------------------------------------
# Pure-function: score_adherence (AD-18) -- honest 'unavailable'.
# ---------------------------------------------------------------------------
def test_adherence_unavailable_when_gate_absent():
    verdict, detail = R.score_adherence({"provenance": []})
    assert verdict == R.UNAVAILABLE
    assert detail is None


def test_adherence_requires_a_verified_context_tool():
    verdict, detail = R.score_adherence(
        {
            "gate": {
                "adherent": False,
                "context_tool": None,
                "session_kind": "anonymous",
            }
        }
    )
    assert verdict == R.FAIL
    assert detail["adherent"] is False

    bare_verdict, bare_detail = R.score_adherence({"gate": {"adherent": True}})
    assert bare_verdict == R.FAIL
    assert bare_detail["reported_adherent"] is True
    assert bare_detail["adherent"] is False

    verified_verdict, verified_detail = R.score_adherence(
        {"gate": {"adherent": True, "context_tool": "search_context"}}
    )
    assert verified_verdict == R.PASS
    assert verified_detail["context_tool"] == "search_context"


# ---------------------------------------------------------------------------
# Skip accounting -- a null tool_invocation is counted 'skipped', excluded from green.
# ---------------------------------------------------------------------------
def test_skip_accounting_excludes_null_tool_invocation_from_green():
    q_skip = {
        "id": "q_skip",
        "surface": "dq",
        "expected_citations": [],
        "reference_queries": [
            {"role": "canonical_correct", "reference_sql": "SELECT 1", "fixture_sha256": "deadbeef"}
        ],
        # no tool_invocation key -> must be skipped
    }
    # conn=None (no DB), run_replay False -> ground truth skipped, replay skipped.
    rec = R.score_question(q_skip, conn=None, run_replay=False, as_of_override=None)
    assert rec["tool_replay"] == R.SKIPPED
    assert rec["skip_reason"] == "no_tool_invocation"
    assert rec["accuracy"] == R.SKIPPED

    artifact_summary = R._summarise([rec])
    # The skip is listed and does NOT contribute to accuracy PASS/FAIL denominator.
    assert "q_skip" in artifact_summary["replay_skipped_ids"]
    assert artifact_summary["accuracy_score"] is None
    assert artifact_summary["accuracy"][R.SKIPPED] == 1
    assert artifact_summary["accuracy"][R.PASS] == 0


def test_summary_accuracy_score_math():
    recs = [
        {
            "id": "a",
            "accuracy": R.PASS,
            "citations": R.PASS,
            "adherence": R.UNAVAILABLE,
            "tool_replay": "done",
            "ground_truth": R.PASS,
        },
        {
            "id": "b",
            "accuracy": R.FAIL,
            "citations": R.NA,
            "adherence": R.UNAVAILABLE,
            "tool_replay": "done",
            "ground_truth": R.PASS,
        },
        {
            "id": "c",
            "accuracy": R.SKIPPED,
            "citations": R.NA,
            "adherence": R.UNAVAILABLE,
            "tool_replay": R.SKIPPED,
            "ground_truth": R.PASS,
        },
    ]
    s = R._summarise(recs)
    assert s["accuracy_score"] == pytest.approx(0.5)  # 1 pass / (1 pass + 1 fail); skip excluded
    assert s["citation_score"] == pytest.approx(1.0)  # 1 pass / 1 scored; N/A excluded
    assert s["adherence_score"] is None  # unavailable evidence is never scored


def test_business_path_evidence_is_scored_and_aggregated(monkeypatch):
    question = {
        "id": "q_path",
        "surface": "expert_report",
        "expected_citations": [],
        "expected_business_routes": [
            {
                "domain_id": "bdm_sales",
                "target_type": "report_view",
                "target_id": "ga4/acquisition",
                "baseline_path_key": "ctxp_current",
            }
        ],
        "reference_queries": [
            {
                "role": "canonical_correct",
                "reference_sql": "SELECT 1",
                "fixture_sha256": "deadbeef",
            }
        ],
        "tool_invocation": {
            "tool": "get_daily_report",
            "args": {
                "project_id": "default",
                "connectors": ["google-analytics"],
                "date_range": {"start": "2026-06-01", "end": "2026-06-02"},
            },
            "result_selector": {
                "kind": "fact_sum",
                "connector": "google-analytics",
                "metric": "sessions",
                "breakdown_dimension": "country",
                "round": None,
            },
        },
    }
    path = {
        "path_key": "ctxp_current",
        "target": {"type": "report_view", "id": "ga4/acquisition"},
        "ordered_path": [
            {
                "kind": "node",
                "node_type": "business_domain",
                "id": "bdm_sales",
                "version_number": 1,
            }
        ],
    }
    envelope = {
        "meta": {
            "business_context_paths": [path],
            "business_context_state": "resolved",
            "trace_id": "trace_01",
            "provenance": [],
        },
        "data": {"rows": []},
    }
    monkeypatch.setattr(R, "call_tool", lambda tool, args: (envelope, None, False))

    record = R.score_question(question, conn=None, run_replay=True, as_of_override=None)
    assert record["business_path"]["outcome"] == "pass"
    assert record["observed_path_keys"] == ["ctxp_current"]
    assert record["trace_id"] == "trace_01"
    summary = R._summarise([record])
    assert summary["business_paths"]["pass"] == 1
    assert summary["business_paths"]["coverage_pct"] == 100.0


def _question_with_expected_business_path() -> dict:
    question = dict(R.load_corpus()["questions"][0])
    question["expected_business_routes"] = [
        {
            "domain_id": "bdm_sales",
            "target_type": "report_view",
            "target_id": "ga4/acquisition",
        }
    ]
    return question


def test_business_path_without_replay_is_unverifiable():
    question = _question_with_expected_business_path()
    question.pop("tool_invocation", None)

    record = R.score_question(question, conn=None, run_replay=False, as_of_override=None)

    assert record["business_path"]["outcome"] == "unverifiable"
    assert record["business_path"]["observed_state"] == "unavailable"
    assert record["business_path"]["missing_path_count"] == 0


def test_malformed_business_paths_are_unverifiable(monkeypatch):
    question = _question_with_expected_business_path()
    envelope = {
        "meta": {
            "business_context_paths": ["not-an-object"],
            "business_context_state": "resolved",
            "provenance": [],
        },
        "data": {"rows": []},
    }
    monkeypatch.setattr(R, "call_tool", lambda tool, args: (envelope, None, False))

    record = R.score_question(question, conn=None, run_replay=True, as_of_override=None)

    assert record["business_path"]["outcome"] == "unverifiable"
    assert record["business_path"]["observed_state"] == "unavailable"
    assert record["observed_path_keys"] == []
    assert "business context evidence unavailable or malformed" in record["notes"]


# ---------------------------------------------------------------------------
# AI-305: a dimension that measures nothing SAYS SO, and names the gesture.
# ---------------------------------------------------------------------------
def _summary_lines(questions: list[dict]) -> list[str]:
    """Render the human summary for `questions` without touching DuckDB or the seam."""
    results = [
        {
            "id": q["id"],
            "surface": q["surface"],
            "ground_truth": R.PASS,
            "accuracy": R.SKIPPED,
            "citations": R.NA,
            "adherence": R.UNAVAILABLE,
            "tool_replay": R.SKIPPED,
        }
        for q in questions
    ]
    artifact = {
        "summary": R._summarise(results, questions),
        "results": results,
        "corpus": {"schema_version": "1", "as_of_anchor": "2026-07-15"},
        "config": {"as_of_anchor": "2026-07-15", "duckdb_available": False, "only": None},
    }
    return R.render_summary(artifact).splitlines()


def test_an_unasked_business_path_dimension_says_so_instead_of_saying_nothing():
    """The branch used to print NOTHING when no question declared a route.

    A summary that omits the line cannot be told from one where every path passed.
    Measured 2026-08-22 on the shipped corpus: 0 of its questions carry
    `expected_business_routes`, so this was the branch every run took.
    """
    questions = [
        {"id": "q1", "surface": "daily_report", "tool_invocation": {"tool": "get_daily_report"}},
        {"id": "q2", "surface": "card"},
    ]

    lines = [line for line in _summary_lines(questions) if "business paths" in line]

    assert lines, "the business-path dimension printed nothing at all"
    assert "not measured" in lines[0]
    assert "0/2 questions declare expected_business_routes" in lines[0]


def test_it_names_the_SECOND_silence_when_no_question_reaches_an_emitting_surface():
    """Declaring a route is not enough when no encoded tool can emit the evidence.

    This is the half AI-305 did not state: `report_mcp` resolves the governed
    routes inside `get_report`, and the corpus's only encoded tool is
    `get_daily_report`. Declaring routes there would score `unverifiable`, not a
    coverage -- a second mute dimension reached by more work.
    """
    questions = [
        {"id": "q1", "surface": "daily_report", "tool_invocation": {"tool": "get_daily_report"}}
    ]

    text = "\n".join(_summary_lines(questions))

    assert "0 question invokes a tool that emits meta.business_context_paths" in text
    assert "would not light it either" in text


def test_a_question_on_an_emitting_surface_is_told_to_declare_a_route():
    """The other empty state, and it names a DIFFERENT gesture."""
    questions = [
        {"id": "q1", "surface": "expert_report", "tool_invocation": {"tool": "get_report"}}
    ]

    text = "\n".join(_summary_lines(questions))

    assert "1/1 questions are on a tool that emits the evidence" in text
    assert "declare a route on one" in text


def test_the_emitting_tool_list_is_re_derived_from_the_product_not_trusted():
    """`BUSINESS_PATH_EMITTING_TOOLS` must not rot the day a second tool emits.

    The set is re-derived from `core/report_mcp.py` itself: every function that
    assigns `meta["business_context_paths"]` is an emitter. An instrument that
    only agreed with its own constant would measure its own copy.
    """
    import ast

    source = (_REPO / "server" / "core" / "report_mcp.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    emitters: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Subscript):
                continue
            index = inner.slice
            if isinstance(index, ast.Constant) and index.value == "business_context_paths":
                emitters.add(node.name)
                break

    assert emitters, "no function assigns business_context_paths -- the AST probe broke"
    assert emitters == set(R.BUSINESS_PATH_EMITTING_TOOLS), (
        f"the product emits business_context_paths from {sorted(emitters)}, the runner "
        f"believes {sorted(R.BUSINESS_PATH_EMITTING_TOOLS)}. Update the constant -- the "
        "summary tells a reader which surface to encode a question on."
    )


# ---------------------------------------------------------------------------
# Who the run was, and which warehouse it read (AI-305, ratified 2026-08-24).
# ---------------------------------------------------------------------------
def _summary_with_config(config: dict) -> str:
    artifact = {
        "summary": R._summarise([], []),
        "results": [],
        "corpus": {"schema_version": "1", "as_of_anchor": "2026-07-15"},
        "config": {"duckdb_available": True, "only": None, **config},
    }
    return R.render_summary(artifact)


def test_the_summary_names_the_identity_the_run_was_green_as():
    """`Incomplete if`: "a run's summary does not say which identity produced it"."""
    text = _summary_with_config(
        {
            "identity": R.EVALUATION_IDENTITY,
            "environment": "evaluation",
            "identity_admitted": True,
        }
    )

    assert R.EVALUATION_IDENTITY in text
    assert "environment           : evaluation" in text
    assert "identity admitted: True" in text


def test_a_refused_run_says_it_was_refused_instead_of_going_quiet():
    text = _summary_with_config(
        {
            "identity": R.EVALUATION_IDENTITY,
            "environment": "production",
            "identity_admitted": False,
        }
    )

    assert "this environment refuses the evaluation identity" in text


def test_the_summary_reports_the_warehouse_the_SEAM_opens():
    """The instrument printed `duckdb_available: True` while the seam read `:memory:`.

    Story 69.5, 2026-08-24: the runner resolved its own seed file, the tool seam
    resolved `TOOROW_DUCKDB_PATH`, and with that variable unset the run scored
    9.5% accuracy under a header announcing the warehouse as available.
    """
    text = _summary_with_config(
        {
            "warehouse_seam_path": ":memory:",
            "warehouse_ground_truth_path": "/seeds/local.duckdb",
        }
    )

    assert "warehouse (seam)      : :memory:" in text
    assert "the two DISAGREE" in text


def test_two_spellings_of_one_file_are_not_reported_as_two_warehouses():
    """A divergence warning that fires on a path separator teaches readers to ignore it."""
    seed = _REPO / "server" / "modules" / "google-analytics" / "seeds" / "local.duckdb"
    text = _summary_with_config(
        {
            "warehouse_seam_path": str(seed).replace("\\", "/"),
            "warehouse_ground_truth_path": str(seed),
        }
    )

    assert "the two DISAGREE" not in text


def test_declaring_the_evaluation_environment_does_not_leak_out_of_the_run():
    """A module-level `setdefault` would declare a whole pytest session evaluation.

    The tests that prove production refuses the identity run in the same process
    as this one; if the declaration leaked, they would be asking an evaluation
    environment whether it behaves like production.
    """
    before = os.environ.get("TOOROW_ENVIRONMENT")
    with R.declared_evaluation_environment() as environment:
        assert environment == (before.strip().lower() if before else "evaluation")
    assert os.environ.get("TOOROW_ENVIRONMENT") == before


def test_an_operator_declared_environment_is_never_overridden(monkeypatch):
    monkeypatch.setenv("TOOROW_ENVIRONMENT", "production")
    with R.declared_evaluation_environment() as environment:
        assert environment == "production"
        assert R.evaluation_identity_admitted(R.EVALUATION_IDENTITY) is False


def test_a_single_provenance_mapping_is_read_as_one_citation():
    """`get_report` emits ONE mapping where `get_daily_report` emits a list.

    Iterating the mapping yields its keys, so `p.get(...)` raised
    `'str' object has no attribute 'get'` and the runner crashed on the first
    report question rather than scoring it (measured 2026-08-24).
    """
    entry = {"source_system": "google-analytics", "source_field": "fact_daily_kpi",
             "pull_id": "pull_EXAMPLE"}

    assert R.normalise_provenance(entry) == [entry]
    assert R.normalise_provenance([entry]) == [entry]
    assert R.normalise_provenance(None) == []

    verdict, _notes = R.score_citations(
        R.normalise_provenance(entry),
        [{"source_system": "google-analytics", "source_field": "fact_daily_kpi",
          "pull_id_required": True}],
    )
    assert verdict == R.PASS


# ---------------------------------------------------------------------------
# Corpus wiring: tool_invocation shape is valid for every resolved question.
# ---------------------------------------------------------------------------
def test_corpus_tool_invocations_are_well_formed():
    """Shape asked of the VALIDATOR, not restated here.

    This test carried its own copy of the argument rules and asserted
    `tool == "get_daily_report"` on every question. The day `get_report` joined
    the corpus (AI-305, 2026-08-24) the copy was the only thing that went red --
    the corpus and the validator agreed perfectly. A second statement of a rule
    fails on the rule's SUCCESSOR, which is the opposite of a guard.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    from validate_corpus import _validate_tool_invocation  # noqa: PLC0415

    corpus = R.load_corpus()
    resolved = 0
    tools: set[str] = set()
    for q in corpus["questions"]:
        ti = q.get("tool_invocation")
        if ti is None:
            continue
        resolved += 1
        tools.add(ti["tool"])
        assert _validate_tool_invocation(ti, q["id"]) == []
        sel = ti["result_selector"]
        assert sel["kind"] in ("fact_sum", "fact_sum_by_date")
        for key in ("connector", "metric", "breakdown_dimension"):
            assert sel[key], f"{q['id']}: selector missing {key}"
    assert resolved >= 15, f"expected many resolved tool_invocations, got {resolved}"
    # WITHOUT THIS LINE THE BUSINESS-PATH DIMENSION SILENTLY GOES BACK TO MUTE.
    # `get_report` is the only tool whose envelope carries
    # `meta.business_context_paths`; if the corpus stops encoding one, every
    # declared route scores `unverifiable` and the summary reports a dimension
    # that measures nothing -- the exact state AI-305 opened on.
    assert R.BUSINESS_PATH_EMITTING_TOOLS & tools, (
        "no corpus question invokes a tool that emits meta.business_context_paths "
        f"({sorted(R.BUSINESS_PATH_EMITTING_TOOLS)}); the business-path dimension is mute"
    )


# ---------------------------------------------------------------------------
# End-to-end (DuckDB + in-process MCP seam gated).
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    not _e2e_enabled(),
    reason="e2e opt-in off (set TOOROW_EVALS_E2E=1 + seed) -- seam replay skipped",
)
def test_end_to_end_run_is_deterministic_and_scores():
    """Two full passes on the same seeds produce identical scores (determinism), and at
    least some resolved questions pass accuracy through the REAL MCP seam."""
    os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
    os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
    os.environ.setdefault("SCHEDULER_ENABLED", "false")

    corpus = R.load_corpus()
    art1 = R.run(corpus)
    art2 = R.run(corpus)

    # Determinism: identical per-question verdicts + summary tallies.
    assert art1["summary"] == art2["summary"]
    v1 = [(r["id"], r["accuracy"], r["citations"], r["ground_truth"]) for r in art1["results"]]
    v2 = [(r["id"], r["accuracy"], r["citations"], r["ground_truth"]) for r in art2["results"]]
    assert v1 == v2

    s = art1["summary"]
    # Ground truth for the whole corpus must hold (no corpus drift).
    assert s["ground_truth"][R.FAIL] == 0, "corpus fixture drift detected"
    # Replay ran and produced at least some scored accuracy.
    assert s["accuracy"][R.PASS] > 0
    # Skips are listed separately, never inflating the green.
    assert isinstance(s["replay_skipped_ids"], list)


@pytest.mark.skipif(
    not _e2e_enabled(),
    reason="e2e opt-in off (set TOOROW_EVALS_E2E=1 + seed) -- seam replay skipped",
)
def test_end_to_end_render_summary_is_ascii_only():
    corpus = R.load_corpus()
    art = R.run(corpus)
    text = R.render_summary(art)
    # AI-03: stdout must be strictly ASCII.
    text.encode("ascii")  # raises if any non-ASCII leaked
