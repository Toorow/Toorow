"""Story 14.3 -- Regression gate self-tests for scripts/eval_gate.py.

These tests exercise the PURE FUNCTIONS (build_baseline, load_baseline,
compare_to_baseline, render_gate_report) OFFLINE: no DB, no seam, no network.
They mirror the test discipline established in test_run_evals.py.

Contracts proven here:
  * REGRESSION DETECTION   -- a question that was PASS in the baseline and is
                               now FAIL causes verdict=FAIL and is named in the report.
  * PROGRESSION NON-BLOCK  -- a question that was FAIL and is now PASS is reported
                               but does NOT cause a FAIL verdict.
  * SKIP IS NOT REGRESSION -- a question that is currently 'skipped' (seam offline)
                               is NEVER a hard regression, even if baseline=PASS.
                               Rationale: we cannot distinguish "wrong answer" from
                               "seam temporarily offline". Emitted as a WARNING only.
  * BASELINE NEVER AUTO-WRITTEN -- calling compare_to_baseline (the gate) without
                                   --update-baseline leaves the file untouched.
                                   Proven via a tmp file assertion.
  * DETERMINISM            -- comparing the same artifact twice yields the same verdict.
  * ASCII-ONLY REPORT      -- render_gate_report output must encode to ASCII without
                               errors (AI-03).
  * LANGFUSE TRACE LINK    -- when a record carries a trace_id, the regression report
                               includes the Langfuse URL. When absent, no URL line.
                               (AI-34: the live Langfuse client call is gated on AI-34
                               bring-up; this test covers the static link formatting only.)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path wiring (mirrors test_run_evals.py).
# ---------------------------------------------------------------------------
_REPO = Path(__file__).resolve().parents[3]
for _p in (str(_REPO / "server"), str(_REPO / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import eval_gate as G  # noqa: E402  # type: ignore[import]
import run_evals as R  # noqa: E402  # type: ignore[import]

# ---------------------------------------------------------------------------
# Synthetic artifact / baseline factories.
# ---------------------------------------------------------------------------

def _make_record(
    q_id: str,
    accuracy: str = R.PASS,
    citations: str = R.NA,
    ground_truth: str = R.PASS,
    tool_replay: str = "done",
    trace_id: str | None = None,
) -> dict:
    """Build a minimal per-question record matching run_evals.score_question's output."""
    rec: dict = {
        "id": q_id,
        "surface": "daily_report",
        "ground_truth": ground_truth,
        "accuracy": accuracy,
        "citations": citations,
        "adherence": R.UNAVAILABLE,
        "tool_replay": tool_replay,
        "skip_reason": None,
        "notes": [],
        "expected": None,
        "actual": None,
    }
    if trace_id is not None:
        rec["trace_id"] = trace_id
    return rec


def _make_artifact(records: list[dict]) -> dict:
    """Build a minimal run artifact (same structure as run_evals.run() returns)."""
    summary = R._summarise(records)
    return {
        "corpus": {
            "schema_version": "1",
            "as_of_anchor": "2026-06-07",
            "seeds_commit": "abc123",
        },
        "config": {
            "as_of_override": None,
            "only": None,
            "duckdb_available": False,
            "replay_ran": False,
        },
        "summary": summary,
        "results": records,
    }


def _make_baseline_from_records(records: list[dict]) -> dict:
    """Build a baseline dict directly from records (without an artifact round-trip)."""
    artifact = _make_artifact(records)
    return G.build_baseline(artifact)


# ---------------------------------------------------------------------------
# Test: REGRESSION DETECTION -- PASS -> FAIL causes verdict=FAIL.
# ---------------------------------------------------------------------------

class TestRegressionDetected:
    """A question that was PASS in the baseline and is now FAIL is a regression."""

    def _setup(self):
        baseline_records = [
            _make_record("q1", accuracy=R.PASS, citations=R.PASS),
            _make_record("q2", accuracy=R.PASS, citations=R.NA),
        ]
        baseline = _make_baseline_from_records(baseline_records)

        # q1 accuracy has REGRESSED; q2 stays PASS.
        current_records = [
            _make_record("q1", accuracy=R.FAIL, citations=R.PASS),
            _make_record("q2", accuracy=R.PASS, citations=R.NA),
        ]
        artifact = _make_artifact(current_records)
        return artifact, baseline

    def test_verdict_is_fail(self):
        artifact, baseline = self._setup()
        result = G.compare_to_baseline(artifact, baseline)
        assert result["verdict"] == G.FAIL

    def test_regression_named(self):
        artifact, baseline = self._setup()
        result = G.compare_to_baseline(artifact, baseline)
        ids = [r["id"] for r in result["regressions"]]
        assert "q1" in ids

    def test_regression_dimension_named(self):
        artifact, baseline = self._setup()
        result = G.compare_to_baseline(artifact, baseline)
        reg = next(r for r in result["regressions"] if r["id"] == "q1")
        assert reg["dim"] == "accuracy"
        assert reg["baseline_verdict"] == R.PASS
        assert reg["now_verdict"] == R.FAIL

    def test_non_regressed_question_not_in_regressions(self):
        artifact, baseline = self._setup()
        result = G.compare_to_baseline(artifact, baseline)
        ids = [r["id"] for r in result["regressions"]]
        assert "q2" not in ids

    def test_report_names_regressed_question(self):
        artifact, baseline = self._setup()
        comparison = G.compare_to_baseline(artifact, baseline)
        report = G.render_gate_report(artifact, comparison)
        assert "q1" in report
        assert "REGRESSED" in report

    def test_report_shows_fail_banner(self):
        artifact, baseline = self._setup()
        comparison = G.compare_to_baseline(artifact, baseline)
        report = G.render_gate_report(artifact, comparison)
        assert "GATE: FAIL" in report

    def test_citations_regression_also_detected(self):
        """Citations PASS -> FAIL is also a regression."""
        baseline_records = [_make_record("qc", accuracy=R.PASS, citations=R.PASS)]
        baseline = _make_baseline_from_records(baseline_records)

        current_records = [_make_record("qc", accuracy=R.PASS, citations=R.FAIL)]
        artifact = _make_artifact(current_records)

        result = G.compare_to_baseline(artifact, baseline)
        assert result["verdict"] == G.FAIL
        reg_dims = [(r["id"], r["dim"]) for r in result["regressions"]]
        assert ("qc", "citations") in reg_dims


# ---------------------------------------------------------------------------
# Test: PROGRESSION -- FAIL -> PASS is non-blocking.
# ---------------------------------------------------------------------------

class TestProgressionNonBlocking:
    """A question that improves (FAIL -> PASS) is reported but does NOT fail the gate."""

    def _setup(self):
        baseline_records = [
            _make_record("qp", accuracy=R.FAIL, citations=R.NA),
        ]
        baseline = _make_baseline_from_records(baseline_records)

        current_records = [
            _make_record("qp", accuracy=R.PASS, citations=R.NA),
        ]
        artifact = _make_artifact(current_records)
        return artifact, baseline

    def test_verdict_is_pass(self):
        artifact, baseline = self._setup()
        result = G.compare_to_baseline(artifact, baseline)
        assert result["verdict"] == G.PASS

    def test_progression_reported(self):
        artifact, baseline = self._setup()
        result = G.compare_to_baseline(artifact, baseline)
        ids = [p["id"] for p in result["progressions"]]
        assert "qp" in ids

    def test_progression_not_in_regressions(self):
        artifact, baseline = self._setup()
        result = G.compare_to_baseline(artifact, baseline)
        reg_ids = [r["id"] for r in result["regressions"]]
        assert "qp" not in reg_ids

    def test_report_shows_progression(self):
        artifact, baseline = self._setup()
        comparison = G.compare_to_baseline(artifact, baseline)
        report = G.render_gate_report(artifact, comparison)
        assert "qp" in report
        # The progression section heading must be present.
        assert "PROGRESSION" in report

    def test_report_shows_pass_banner(self):
        artifact, baseline = self._setup()
        comparison = G.compare_to_baseline(artifact, baseline)
        report = G.render_gate_report(artifact, comparison)
        assert "GATE: PASS" in report


# ---------------------------------------------------------------------------
# Test: SKIP IS NOT A HARD REGRESSION (story contract; non-blocking warning).
# ---------------------------------------------------------------------------

class TestSkipNotRegression:
    """A skipped question (seam offline) that was PASS in baseline must NOT be a
    hard regression. It becomes a non-blocking WARNING.

    Design choice: we treat skip-from-PASS as a WARNING (not a hard fail) because
    the gate cannot distinguish "the answer is now wrong" from "the seam is offline".
    Emitting a warning means the developer is informed and must re-run with the seam
    live before shipping -- but the gate does not block a deploy where the skip is
    purely environmental.
    """

    def _setup(self):
        baseline_records = [
            _make_record("qs", accuracy=R.PASS, citations=R.PASS),
        ]
        baseline = _make_baseline_from_records(baseline_records)

        # Now the seam is offline -> both accuracy and citations are skipped.
        current_records = [
            _make_record(
                "qs",
                accuracy=R.SKIPPED,
                citations=R.NA,   # citations N/A when seam offline
                tool_replay=R.SKIPPED,
                ground_truth=R.SKIPPED,
            ),
        ]
        artifact = _make_artifact(current_records)
        return artifact, baseline

    def test_verdict_is_pass_not_fail(self):
        """Skipped-from-PASS must never be a hard regression -> gate verdict PASS."""
        artifact, baseline = self._setup()
        result = G.compare_to_baseline(artifact, baseline)
        assert result["verdict"] == G.PASS

    def test_not_in_regressions(self):
        artifact, baseline = self._setup()
        result = G.compare_to_baseline(artifact, baseline)
        reg_ids = [r["id"] for r in result["regressions"]]
        assert "qs" not in reg_ids

    def test_warning_emitted(self):
        """A WARNING must be emitted so the developer knows to re-run with seam live."""
        artifact, baseline = self._setup()
        result = G.compare_to_baseline(artifact, baseline)
        warn_kinds = [w["kind"] for w in result["warnings"]]
        assert "skip_was_pass" in warn_kinds

    def test_warning_names_question(self):
        artifact, baseline = self._setup()
        result = G.compare_to_baseline(artifact, baseline)
        warn_msgs = [w["message"] for w in result["warnings"]]
        assert any("qs" in m for m in warn_msgs)

    def test_report_shows_warning(self):
        artifact, baseline = self._setup()
        comparison = G.compare_to_baseline(artifact, baseline)
        report = G.render_gate_report(artifact, comparison)
        assert "WARNING" in report or "skip" in report.lower()


# ---------------------------------------------------------------------------
# Test: BASELINE NEVER AUTO-WRITTEN (gate without --update-baseline is read-only).
# ---------------------------------------------------------------------------

class TestBaselineNeverAutoWritten:
    """compare_to_baseline is a pure function that never writes the baseline file.
    The only write path is write_baseline(), called explicitly by --update-baseline.
    """

    def test_compare_does_not_write_file(self, tmp_path):
        """Calling compare_to_baseline with an existing baseline leaves the file
        byte-identical (not rewritten, not updated)."""
        baseline_records = [_make_record("qx", accuracy=R.PASS)]
        baseline = _make_baseline_from_records(baseline_records)

        baseline_file = tmp_path / "baseline.json"
        baseline_file.write_text(
            json.dumps(baseline, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        mtime_before = baseline_file.stat().st_mtime
        content_before = baseline_file.read_text(encoding="utf-8")

        # Run the gate comparison (no --update-baseline).
        current_records = [_make_record("qx", accuracy=R.FAIL)]
        artifact = _make_artifact(current_records)
        G.compare_to_baseline(artifact, baseline)

        mtime_after = baseline_file.stat().st_mtime
        content_after = baseline_file.read_text(encoding="utf-8")

        assert content_after == content_before, (
            "compare_to_baseline must NOT rewrite baseline.json"
        )
        assert mtime_after == mtime_before, (
            "baseline.json modification time changed -- auto-write detected"
        )

    def test_write_baseline_only_when_explicitly_called(self, tmp_path):
        """write_baseline writes the file; calling build_baseline alone does not."""
        baseline_file = tmp_path / "b.json"
        assert not baseline_file.exists()

        records = [_make_record("qy", accuracy=R.PASS)]
        artifact = _make_artifact(records)

        # build_baseline is pure -- no side effect.
        G.build_baseline(artifact)
        assert not baseline_file.exists(), "build_baseline must not write any file"

        # write_baseline writes it.
        G.write_baseline(baseline_file, artifact)
        assert baseline_file.exists()

        loaded = json.loads(baseline_file.read_text(encoding="utf-8"))
        assert loaded["per_question"]["qy"]["accuracy"] == R.PASS


# ---------------------------------------------------------------------------
# Test: DETERMINISM -- two comparisons on the same inputs yield the same verdict.
# ---------------------------------------------------------------------------

class TestDeterminism:
    def test_same_artifact_twice_same_verdict(self):
        """Two calls to compare_to_baseline with the same inputs must be identical."""
        baseline_records = [
            _make_record("d1", accuracy=R.PASS, citations=R.PASS),
            _make_record("d2", accuracy=R.FAIL, citations=R.NA),
        ]
        baseline = _make_baseline_from_records(baseline_records)

        current_records = [
            _make_record("d1", accuracy=R.FAIL, citations=R.PASS),
            _make_record("d2", accuracy=R.PASS, citations=R.NA),
        ]
        artifact = _make_artifact(current_records)

        result1 = G.compare_to_baseline(artifact, baseline)
        result2 = G.compare_to_baseline(artifact, baseline)

        assert result1["verdict"] == result2["verdict"]
        assert result1["regressions"] == result2["regressions"]
        assert result1["progressions"] == result2["progressions"]

    def test_build_baseline_is_deterministic(self):
        """build_baseline on the same artifact is always byte-identical (sorted keys)."""
        records = [
            _make_record("z2", accuracy=R.PASS),
            _make_record("z1", accuracy=R.FAIL),
            _make_record("z3", accuracy=R.PASS),
        ]
        artifact = _make_artifact(records)

        bl1 = json.dumps(G.build_baseline(artifact), sort_keys=True)
        bl2 = json.dumps(G.build_baseline(artifact), sort_keys=True)
        assert bl1 == bl2

    def test_per_question_sorted_by_id(self):
        """Baseline per_question keys are sorted by id (readable PR diffs)."""
        records = [
            _make_record("c", accuracy=R.PASS),
            _make_record("a", accuracy=R.PASS),
            _make_record("b", accuracy=R.PASS),
        ]
        artifact = _make_artifact(records)
        bl = G.build_baseline(artifact)
        keys = list(bl["per_question"].keys())
        assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# Test: ASCII-ONLY REPORT (AI-03).
# ---------------------------------------------------------------------------

class TestAsciiOnlyReport:
    def test_render_regression_report_is_ascii(self):
        """render_gate_report output must be strictly ASCII (AI-03 compliance)."""
        baseline_records = [_make_record("qa", accuracy=R.PASS)]
        baseline = _make_baseline_from_records(baseline_records)
        current_records = [_make_record("qa", accuracy=R.FAIL)]
        artifact = _make_artifact(current_records)
        comparison = G.compare_to_baseline(artifact, baseline)
        report = G.render_gate_report(artifact, comparison)
        # Will raise UnicodeEncodeError if any non-ASCII byte sneaks in.
        report.encode("ascii")

    def test_render_pass_report_is_ascii(self):
        baseline_records = [_make_record("qb", accuracy=R.PASS)]
        baseline = _make_baseline_from_records(baseline_records)
        current_records = [_make_record("qb", accuracy=R.PASS)]
        artifact = _make_artifact(current_records)
        comparison = G.compare_to_baseline(artifact, baseline)
        report = G.render_gate_report(artifact, comparison)
        report.encode("ascii")


# ---------------------------------------------------------------------------
# Test: LANGFUSE TRACE LINK (AI-34 -- static format only; live call gated).
# ---------------------------------------------------------------------------

class TestLangfuseTraceLink:
    """When a record carries a trace_id, the regression report includes the link.
    The live Langfuse client call (AI-34) is gated on AI-34 bring-up; this test
    covers only the static URL formatting to avoid a hard Langfuse dependency.
    """

    def test_trace_link_in_report_when_trace_id_present(self):
        trace_id = "abc-def-0123"
        baseline_records = [_make_record("qt", accuracy=R.PASS)]
        baseline = _make_baseline_from_records(baseline_records)

        current_records = [_make_record("qt", accuracy=R.FAIL, trace_id=trace_id)]
        artifact = _make_artifact(current_records)
        comparison = G.compare_to_baseline(artifact, baseline)
        report = G.render_gate_report(artifact, comparison)

        assert trace_id in report
        assert "langfuse" in report.lower() or "Langfuse" in report

    def test_no_trace_link_when_trace_id_absent(self):
        baseline_records = [_make_record("qu", accuracy=R.PASS)]
        baseline = _make_baseline_from_records(baseline_records)

        # record has no trace_id key.
        current_records = [_make_record("qu", accuracy=R.FAIL)]
        artifact = _make_artifact(current_records)
        comparison = G.compare_to_baseline(artifact, baseline)
        report = G.render_gate_report(artifact, comparison)

        # No trace link must appear when trace_id is absent.
        assert "langfuse.com/trace/" not in report


# ---------------------------------------------------------------------------
# Test: MISSING / NEW QUESTIONS handling.
# ---------------------------------------------------------------------------

class TestMissingAndNewQuestions:
    def test_missing_from_run_is_warning_not_fail(self):
        """A question in the baseline but absent from the current run is a WARNING."""
        baseline_records = [
            _make_record("qm1", accuracy=R.PASS),
            _make_record("qm2", accuracy=R.PASS),
        ]
        baseline = _make_baseline_from_records(baseline_records)

        # qm2 removed from corpus.
        current_records = [_make_record("qm1", accuracy=R.PASS)]
        artifact = _make_artifact(current_records)
        result = G.compare_to_baseline(artifact, baseline)

        assert result["verdict"] == G.PASS
        warn_kinds = [w["kind"] for w in result["warnings"]]
        assert "missing_from_run" in warn_kinds

    def test_new_question_not_in_baseline_is_info_not_fail(self):
        """A question in the current run but not in the baseline is INFO."""
        baseline_records = [_make_record("qn1", accuracy=R.PASS)]
        baseline = _make_baseline_from_records(baseline_records)

        current_records = [
            _make_record("qn1", accuracy=R.PASS),
            _make_record("qn2_new", accuracy=R.FAIL),  # new, not in baseline
        ]
        artifact = _make_artifact(current_records)
        result = G.compare_to_baseline(artifact, baseline)

        # Only qn1 is compared; qn2_new is new (no prior baseline) -> not a regression.
        assert result["verdict"] == G.PASS
        assert "qn2_new" in result["new_questions"]


# ---------------------------------------------------------------------------
# Test: load_baseline / build_baseline round-trip.
# ---------------------------------------------------------------------------

class TestBaselineRoundTrip:
    def test_load_raises_on_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            G.load_baseline(tmp_path / "nonexistent.json")

    def test_load_raises_on_malformed_file(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text('{"schema_version": 1}', encoding="utf-8")
        with pytest.raises(ValueError, match="malformed"):
            G.load_baseline(bad)

    def test_write_then_load_round_trip(self, tmp_path):
        records = [
            _make_record("rt1", accuracy=R.PASS, citations=R.PASS),
            _make_record("rt2", accuracy=R.FAIL, citations=R.NA),
        ]
        artifact = _make_artifact(records)
        path = tmp_path / "baseline.json"

        G.write_baseline(path, artifact)
        loaded = G.load_baseline(path)

        assert loaded["per_question"]["rt1"]["accuracy"] == R.PASS
        assert loaded["per_question"]["rt1"]["citations"] == R.PASS
        assert loaded["per_question"]["rt2"]["accuracy"] == R.FAIL
        assert loaded["schema_version"] == 1

    def test_build_baseline_captures_summary_scores(self):
        records = [
            _make_record("s1", accuracy=R.PASS, citations=R.PASS),
            _make_record("s2", accuracy=R.PASS, citations=R.FAIL),
        ]
        artifact = _make_artifact(records)
        bl = G.build_baseline(artifact)
        # accuracy_score = 2/2 = 1.0; citation_score = 1/2 = 0.5.
        assert bl["summary"]["accuracy_score"] == pytest.approx(1.0)
        assert bl["summary"]["citation_score"] == pytest.approx(0.5)

    def test_baseline_json_is_ascii_only(self, tmp_path):
        """baseline.json must be ASCII-safe (no non-ASCII bytes in ids or verdicts)."""
        records = [_make_record("ascii_id", accuracy=R.PASS)]
        artifact = _make_artifact(records)
        path = tmp_path / "baseline.json"
        G.write_baseline(path, artifact)
        content = path.read_bytes()
        content.decode("ascii")  # raises if non-ASCII


# ---------------------------------------------------------------------------
# Test: SCORE REGRESSION (summary scores).
# ---------------------------------------------------------------------------

class TestScoreRegression:
    def test_score_drop_causes_fail_verdict(self):
        """A drop in accuracy_score below the baseline triggers a FAIL verdict."""
        # Baseline: 2 PASS questions => accuracy_score = 1.0.
        baseline_records = [
            _make_record("sq1", accuracy=R.PASS),
            _make_record("sq2", accuracy=R.PASS),
        ]
        baseline = _make_baseline_from_records(baseline_records)

        # Current: 1 PASS, 1 FAIL => accuracy_score = 0.5.
        current_records = [
            _make_record("sq1", accuracy=R.PASS),
            _make_record("sq2", accuracy=R.FAIL),
        ]
        artifact = _make_artifact(current_records)
        result = G.compare_to_baseline(artifact, baseline)

        # sq2 PASS->FAIL is already a per-question regression.
        # Additionally the score_regression flag must be set.
        assert result["score_regression"] is True
        assert result["verdict"] == G.FAIL

    def test_score_improvement_not_a_fail(self):
        """An improvement in accuracy_score (score went UP) is not a fail."""
        # Baseline: 1/2 = 0.5.
        baseline_records = [
            _make_record("si1", accuracy=R.PASS),
            _make_record("si2", accuracy=R.FAIL),
        ]
        baseline = _make_baseline_from_records(baseline_records)

        # Current: 2/2 = 1.0.
        current_records = [
            _make_record("si1", accuracy=R.PASS),
            _make_record("si2", accuracy=R.PASS),
        ]
        artifact = _make_artifact(current_records)
        result = G.compare_to_baseline(artifact, baseline)
        assert result["verdict"] == G.PASS
        assert result["score_regression"] is False


class TestScoreUnverifiableWhenBaselinePassSkips:
    """14.4 review (MEDIUM): a baseline-PASS question that SKIPs now shrinks the score
    denominator; the aggregate must not read as a clean pass. Non-blocking, but surfaced."""

    def test_skip_of_baseline_pass_emits_score_unverifiable_warning(self):
        baseline_records = [
            _make_record("u1", accuracy=R.PASS),
            _make_record("u2", accuracy=R.PASS),
        ]
        baseline = _make_baseline_from_records(baseline_records)
        # u2 replay skipped (seam offline); u1 still PASS -> naive score = 1/1 = 1.0.
        current_records = [
            _make_record("u1", accuracy=R.PASS),
            _make_record("u2", accuracy=R.SKIPPED, tool_replay=R.SKIPPED),
        ]
        artifact = _make_artifact(current_records)
        result = G.compare_to_baseline(artifact, baseline)
        kinds = [w["kind"] for w in result["warnings"]]
        assert "score_unverifiable" in kinds, "shrunk-denominator score must be flagged"
        # Still non-blocking (skip is not a hard regression).
        assert result["verdict"] == G.PASS


class TestGateMainExitCode:
    """14.4 review (HIGH): the load-bearing link is main()'s exit code -- it is what blocks
    a deploy. The pure compare test is necessary but not sufficient. We drive main() with a
    mocked run (no DB/seam) and a tmp baseline, asserting the exit code end to end."""

    def _patch_run(self, monkeypatch, current_records):
        monkeypatch.setattr(R, "load_corpus", lambda: {"questions": []})
        monkeypatch.setattr(R, "run", lambda corpus, **kw: _make_artifact(current_records))
        monkeypatch.setattr(R, "render_summary", lambda art: "")

    def _write_baseline(self, tmp_path, records):
        baseline = _make_baseline_from_records(records)
        bl_path = tmp_path / "baseline.json"
        bl_path.write_text(json.dumps(baseline), encoding="utf-8")
        return bl_path

    def test_regression_returns_nonzero(self, monkeypatch, tmp_path):
        bl = self._write_baseline(tmp_path, [_make_record("q1", accuracy=R.PASS, citations=R.PASS)])
        self._patch_run(monkeypatch, [_make_record("q1", accuracy=R.FAIL, citations=R.PASS)])
        assert G.main(["--baseline", str(bl)]) == 1

    def test_clean_returns_zero(self, monkeypatch, tmp_path):
        bl = self._write_baseline(tmp_path, [_make_record("q1", accuracy=R.PASS, citations=R.PASS)])
        self._patch_run(monkeypatch, [_make_record("q1", accuracy=R.PASS, citations=R.PASS)])
        assert G.main(["--baseline", str(bl)]) == 0

    def test_missing_baseline_returns_two(self, monkeypatch, tmp_path):
        self._patch_run(monkeypatch, [_make_record("q1", accuracy=R.PASS, citations=R.PASS)])
        assert G.main(["--baseline", str(tmp_path / "nope.json")]) == 2
