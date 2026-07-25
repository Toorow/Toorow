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
        "id": "q_err", "surface": "daily_report", "expected_citations": [],
        "reference_queries": [
            {"role": "canonical_correct", "reference_sql": "SELECT 1", "fixture_sha256": "x"}
        ],
        "tool_invocation": {
            "tool": "get_daily_report",
            "args": {"project_id": "default", "connectors": ["gsc"],
                     "date_range": {"start": "2026-06-01", "end": "2026-06-02"}},
            "result_selector": {"kind": "fact_sum", "connector": "gsc", "metric": "clicks",
                                "breakdown_dimension": "page", "round": None},
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
        rows.append({"date": d, "connector": "google-analytics", "metric": "sessions",
                     "breakdown_dimension": "country", "breakdown_value": "FR",
                     "value": 100.0, "pull_id": "p1"})
        rows.append({"date": d, "connector": "google-analytics", "metric": "sessions",
                     "breakdown_dimension": "device_category", "breakdown_value": "mobile",
                     "value": 100.0, "pull_id": "p1"})
    return {"data": {"rows": rows}, "meta": {}}


def test_selector_fact_sum_pins_breakdown():
    env = _fact_rows()
    selector = {"kind": "fact_sum", "connector": "google-analytics",
                "metric": "sessions", "breakdown_dimension": "country", "round": None}
    # country-only sum = 100+100 = 200 (NOT 400 across both breakdowns).
    assert R.select_from_envelope(env, selector) == 200.0


def test_selector_fact_sum_by_date_pins_breakdown():
    env = _fact_rows()
    selector = {"kind": "fact_sum_by_date", "connector": "google-analytics",
                "metric": "sessions", "breakdown_dimension": "country", "round": None}
    assert R.select_from_envelope(env, selector) == [
        ("2026-06-01", 100.0), ("2026-06-02", 100.0)
    ]


def test_selector_fact_sum_empty_is_none():
    env = {"data": {"rows": []}, "meta": {}}
    selector = {"kind": "fact_sum", "connector": "gsc", "metric": "clicks",
                "breakdown_dimension": "page", "round": None}
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


def test_adherence_reports_verdict_when_gate_present():
    verdict, detail = R.score_adherence({"gate": {"adherent": False, "context_tool": None,
                                                  "session_kind": "anonymous"}})
    assert verdict == R.FAIL
    assert detail["adherent"] is False
    verdict2, _ = R.score_adherence({"gate": {"adherent": True}})
    assert verdict2 == R.PASS


# ---------------------------------------------------------------------------
# Skip accounting -- a null tool_invocation is counted 'skipped', excluded from green.
# ---------------------------------------------------------------------------
def test_skip_accounting_excludes_null_tool_invocation_from_green():
    q_skip = {
        "id": "q_skip", "surface": "dq",
        "expected_citations": [],
        "reference_queries": [
            {"role": "canonical_correct", "reference_sql": "SELECT 1",
             "fixture_sha256": "deadbeef"}
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
        {"id": "a", "accuracy": R.PASS, "citations": R.PASS, "adherence": R.UNAVAILABLE,
         "tool_replay": "done", "ground_truth": R.PASS},
        {"id": "b", "accuracy": R.FAIL, "citations": R.NA, "adherence": R.UNAVAILABLE,
         "tool_replay": "done", "ground_truth": R.PASS},
        {"id": "c", "accuracy": R.SKIPPED, "citations": R.NA, "adherence": R.UNAVAILABLE,
         "tool_replay": R.SKIPPED, "ground_truth": R.PASS},
    ]
    s = R._summarise(recs)
    assert s["accuracy_score"] == pytest.approx(0.5)  # 1 pass / (1 pass + 1 fail); skip excluded
    assert s["citation_score"] == pytest.approx(1.0)  # 1 pass / 1 scored; N/A excluded


# ---------------------------------------------------------------------------
# Corpus wiring: tool_invocation shape is valid for every resolved question.
# ---------------------------------------------------------------------------
def test_corpus_tool_invocations_are_well_formed():
    corpus = R.load_corpus()
    resolved = 0
    for q in corpus["questions"]:
        ti = q.get("tool_invocation")
        if ti is None:
            continue
        resolved += 1
        assert ti["tool"] == "get_daily_report"
        args = ti["args"]
        assert args["project_id"] == "default"
        assert isinstance(args["connectors"], list) and args["connectors"]
        assert set(args["date_range"]) == {"start", "end"}
        sel = ti["result_selector"]
        assert sel["kind"] in ("fact_sum", "fact_sum_by_date")
        for key in ("connector", "metric", "breakdown_dimension"):
            assert sel[key], f"{q['id']}: selector missing {key}"
    assert resolved >= 15, f"expected many resolved tool_invocations, got {resolved}"


# ---------------------------------------------------------------------------
# End-to-end (DuckDB + in-process MCP seam gated).
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not _e2e_enabled(),
                    reason="e2e opt-in off (set TOOROW_EVALS_E2E=1 + seed) -- seam replay skipped")
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


@pytest.mark.skipif(not _e2e_enabled(),
                    reason="e2e opt-in off (set TOOROW_EVALS_E2E=1 + seed) -- seam replay skipped")
def test_end_to_end_render_summary_is_ascii_only():
    corpus = R.load_corpus()
    art = R.run(corpus)
    text = R.render_summary(art)
    # AI-03: stdout must be strictly ASCII.
    text.encode("ascii")  # raises if any non-ASCII leaked
