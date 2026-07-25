"""Story 14.3 -- versioned baseline + regression gate for the Golden Questions eval suite.

This gate is the AD-19 / NFR13 non-regression wall. It is LOCAL and runs BEFORE deployment
(never a CI job -- project convention: local-test-before-deploy). The gate:

  1. Runs the full corpus through ``run_evals.run()`` (inheriting 14.2 determinism);
  2. Compares every per-question accuracy + citations verdict against the committed
     ``baseline.json`` (loaded from ``server/tests/evals/baseline.json`` by default);
  3. Prints an ASCII-only report (AI-03) naming each regressed question;
  4. Exits NON-ZERO on any regression; exits zero on zero regressions or on
     ``--update-baseline`` (explicit approval gesture).

BASELINE WRITE DISCIPLINE (critical -- leçon 14.2):
  The baseline is NEVER written automatically by the gate.  Only an explicit
  ``--update-baseline`` flag writes it.  Automatic rewriting would make AD-19 decorative:
  every run would accept its own output.  Always commit the updated baseline.json in a
  dedicated PR/commit with a review note explaining the score change.

REGRESSION vs PROGRESSION vs SKIP (per story contract):
  * REGRESSION  -- a question whose accuracy or citations was PASS in the baseline and is
                   now FAIL.  Also fired when the summary accuracy_score or citation_score
                   drops strictly below the baseline value.  Causes exit != 0.
  * PROGRESSION -- a question that was FAIL (or N/A / skipped / unavailable) in the
                   baseline and is now PASS.  REPORTED but non-blocking; the baseline
                   should be updated explicitly to lock in the improvement.
  * SKIP        -- a question whose ``accuracy`` is currently ``skipped`` (replay not
                   resolved; PG/DuckDB gated) is NEVER treated as a regression, even if
                   the baseline records it as PASS.  Rationale: the gate cannot distinguish
                   "tool answer is now wrong" from "seam is temporarily offline".  A skip
                   earns a non-blocking WARNING so the author knows they should re-run with
                   the seam live before shipping.
  * MISSING     -- a question id in the baseline that is absent from the current run
                   (e.g. removed from corpus) -- reported as a WARNING (not a hard fail),
                   because the corpus evolves.
  * NEW         -- a question id in the current run that is absent from the baseline --
                   reported as INFO; it is not scored against baseline (no prior record).

LANGFUSE (AI-34) -- gated, degrades cleanly:
  The offline gate works fully WITHOUT Langfuse.  When a question record carries a
  ``trace_id`` key (emitted by a live Langfuse-instrumented seam), the regression report
  includes a trace link line per regressed question:

      Langfuse trace: https://cloud.langfuse.com/trace/<trace_id>

  The live trace-id -> score protocol (AI-34 bring-up pass):
    Once Langfuse is up (AI-34 story), the seam emits ``trace_id`` in each per-question
    record (via ``meta.langfuse_trace_id`` in the envelope, extracted in score_question).
    Plug-in point: add ``record["trace_id"] = meta.get("langfuse_trace_id")`` to
    ``score_question`` in run_evals.py.  The gate already reads ``record.get("trace_id")``
    so no gate changes are needed.  The Langfuse observation API (GET /api/public/traces/<id>)
    can then be called to pull the LLM span cost/latency for the regression report.
    Implementation of that live call is gated on AI-34 bring-up and runs as a separate pass.

Usage (from repo root):

    # Check for regressions against committed baseline:
    python scripts/eval_gate.py

    # Override paths / run with a filter:
    python scripts/eval_gate.py --baseline PATH --as-of ISO [--only TAG]

    # Accept current results as the new baseline (explicit approval gesture):
    python scripts/eval_gate.py --update-baseline

    # Pin the real baseline once PG + DuckDB seeds are up (copy this exactly):
    TOOROW_EVALS_E2E=1 TOOROW_DUCKDB_PATH=<path_to_local.duckdb> \\
        python scripts/eval_gate.py --update-baseline

Stdout is ASCII-only (AI-03). The gate READS; it never writes warehouse data.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path wiring (mirrors run_evals.py).
# ---------------------------------------------------------------------------
_SCRIPTS = Path(__file__).resolve().parent
_REPO = _SCRIPTS.parent
_SERVER = _REPO / "server"
for _p in (str(_SERVER), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import run_evals as R  # noqa: E402

EVALS_DIR = _SERVER / "tests" / "evals"
DEFAULT_BASELINE = EVALS_DIR / "baseline.json"

# Verdict shorthand aliases -- imported from run_evals so the gate shares the
# same vocabulary and cannot silently diverge.
PASS = R.PASS        # "PASS"
FAIL = R.FAIL        # "FAIL"
NA = R.NA            # "N/A"
SKIPPED = R.SKIPPED  # "skipped"
UNAVAILABLE = R.UNAVAILABLE  # "unavailable"

# Dimensions scored by the gate (accuracy + citations only; adherence is
# unavailable offline and is not baselined for now).
_SCORED_DIMS = ("accuracy", "citations")

# ---------------------------------------------------------------------------
# Baseline I/O -- pure functions (importable, no side effects unless writing).
# ---------------------------------------------------------------------------

def build_baseline(artifact: dict) -> dict:
    """Extract a stable, sorted, deterministic baseline dict from a run artifact.

    The baseline captures:
      * per_question: {id -> {accuracy, citations}} -- the accepted verdicts;
      * summary: {accuracy_score, citation_score, accuracy_tallies,
                   citations_tallies} -- the accepted aggregated scores.

    Only the dimensions that the gate enforces are included. The result is
    deterministic (sorted by id) so ``baseline.json`` diffs are readable in PRs.
    """
    results: list[dict] = artifact.get("results", [])
    s: dict = artifact.get("summary", {})

    per_question: dict[str, dict] = {}
    for rec in results:
        q_id = rec["id"]
        per_question[q_id] = {dim: rec.get(dim) for dim in _SCORED_DIMS}

    # Sort by id for deterministic JSON output.
    per_question_sorted = dict(sorted(per_question.items()))

    return {
        "schema_version": 1,
        "corpus_meta": {
            "schema_version": artifact.get("corpus", {}).get("schema_version"),
            "as_of_anchor": artifact.get("corpus", {}).get("as_of_anchor"),
            "seeds_commit": artifact.get("corpus", {}).get("seeds_commit"),
        },
        "summary": {
            "accuracy_score": s.get("accuracy_score"),
            "citation_score": s.get("citation_score"),
            "accuracy_tallies": s.get("accuracy"),
            "citations_tallies": s.get("citations"),
        },
        "per_question": per_question_sorted,
    }


def load_baseline(path: Path) -> dict:
    """Load a baseline JSON file. Raises FileNotFoundError / ValueError on problems."""
    if not path.is_file():
        raise FileNotFoundError(
            f"Baseline not found: {path}\n"
            "Run with --update-baseline after a green pass to create it:\n"
            "  TOOROW_EVALS_E2E=1 TOOROW_DUCKDB_PATH=<path> "
            "python scripts/eval_gate.py --update-baseline"
        )
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict) or "per_question" not in data:
        raise ValueError(f"Baseline at {path} is malformed (missing 'per_question').")
    return data


def write_baseline(path: Path, artifact: dict) -> dict:
    """Build + write the baseline from *artifact* to *path*. Returns the baseline dict.

    IMPORTANT: This function is ONLY called on explicit --update-baseline. Never call
    it from compare_to_baseline or inside the gate loop. (If it were called automatically,
    AD-19 would be decorative -- leçon 14.2.)
    """
    baseline = build_baseline(artifact)
    path.write_text(
        json.dumps(baseline, indent=2, sort_keys=True, ensure_ascii=True),
        encoding="utf-8",
    )
    return baseline


# ---------------------------------------------------------------------------
# Regression comparison -- pure function (no I/O, no DB, no seam).
# ---------------------------------------------------------------------------

def compare_to_baseline(artifact: dict, baseline: dict) -> dict:
    """Compare a run artifact against a baseline. Returns a result dict:

    {
      "regressions": [
          {"id": str, "dim": str, "baseline_verdict": str, "now_verdict": str,
           "trace_id": str | None}
      ],
      "progressions": [
          {"id": str, "dim": str, "baseline_verdict": str, "now_verdict": str}
      ],
      "warnings": [
          {"kind": "skip_was_pass" | "missing_from_run" | "score_drop",
           "message": str}
      ],
      "new_questions": [str],   # ids in current run not in baseline
      "verdict": "PASS" | "FAIL",
      "score_regression": bool,
    }

    Classification rules (per story contract):
      * REGRESSION  (hard, verdict=FAIL):
          - A question was PASS in the baseline and is now FAIL (accuracy or citations).
          - summary accuracy_score or citation_score drops strictly below the baseline.
      * SKIP WAS PASS (non-blocking WARNING):
          - A question whose baseline verdict was PASS but current accuracy is 'skipped'.
          - We CANNOT call this a regression because the seam may be offline.
          - Printed as a warning so the developer knows to re-run with seam live.
      * PROGRESSION (non-blocking INFO):
          - A question was FAIL (or any non-PASS) in the baseline and is now PASS.
      * MISSING (non-blocking WARNING):
          - A question id in baseline not present in current run (corpus evolved).
      * NEW (non-blocking INFO):
          - A question id in current run not in baseline.
    """
    bl_per_q: dict[str, dict] = baseline.get("per_question", {})
    bl_summary: dict[str, Any] = baseline.get("summary", {})
    current_results: list[dict] = artifact.get("results", [])

    current_by_id: dict[str, dict] = {r["id"]: r for r in current_results}

    regressions: list[dict] = []
    progressions: list[dict] = []
    warnings: list[dict] = []
    new_questions: list[str] = []

    # -- Per-question comparison --
    for q_id, bl_verdicts in sorted(bl_per_q.items()):
        if q_id not in current_by_id:
            warnings.append({
                "kind": "missing_from_run",
                "message": f"{q_id}: in baseline but absent from current run (corpus change?)",
            })
            continue

        rec = current_by_id[q_id]
        trace_id: str | None = rec.get("trace_id")  # AI-34 Langfuse seam

        for dim in _SCORED_DIMS:
            bl_v = bl_verdicts.get(dim)
            now_v = rec.get(dim)

            # SKIP WAS PASS: seam offline -> non-blocking warning, never a hard regression.
            if now_v == SKIPPED and bl_v == PASS:
                warnings.append({
                    "kind": "skip_was_pass",
                    "message": (
                        f"{q_id} [{dim}]: baseline=PASS but now=skipped "
                        "(replay seam offline -- re-run with seam live before shipping)"
                    ),
                })
                continue

            # REGRESSION: PASS -> FAIL.
            if bl_v == PASS and now_v == FAIL:
                regressions.append({
                    "id": q_id,
                    "dim": dim,
                    "baseline_verdict": bl_v,
                    "now_verdict": now_v,
                    "trace_id": trace_id,
                })
                continue

            # PROGRESSION: non-PASS in baseline, now PASS.
            if bl_v != PASS and now_v == PASS:
                progressions.append({
                    "id": q_id,
                    "dim": dim,
                    "baseline_verdict": bl_v,
                    "now_verdict": now_v,
                })

    # -- New questions (in current run, not in baseline) --
    for q_id in current_by_id:
        if q_id not in bl_per_q:
            new_questions.append(q_id)

    # -- Summary score regression: compare scores over BASELINE-KNOWN questions only.
    #
    # We re-derive the scores over only the questions present in the baseline to avoid
    # false alarms from newly-added corpus questions.  A new question that FAILs has no
    # prior baseline record; including it in the denominator would dilute the score and
    # fire a spurious score regression.  Only questions whose ids appear in bl_per_q are
    # counted here (the same set the per-question loop above operates on).
    score_regression = False
    bl_ids = set(bl_per_q.keys())
    known_records = [r for r in current_results if r["id"] in bl_ids]

    for dim, score_key in (("accuracy", "accuracy_score"), ("citations", "citation_score")):
        bl_score = bl_summary.get(score_key)
        if bl_score is None:
            continue  # no baseline score to compare against
        # Re-tally PASS/FAIL over known records only.
        n_pass = sum(1 for r in known_records if r.get(dim) == PASS)
        n_fail = sum(1 for r in known_records if r.get(dim) == FAIL)
        scored = n_pass + n_fail
        now_score = (n_pass / scored) if scored else None
        # HONESTY (14.4 review): if any baseline-PASS question for this dim is now SKIPPED,
        # the score is computed over a REDUCED denominator and is NOT a lower bound on true
        # accuracy -- a broad seam outage that skips the would-fail questions must not read as
        # a clean 100%. Surface it (non-blocking, consistent with the skip-non-blocking rule);
        # the developer must re-run with the seam live before trusting the score.
        n_skipped_was_pass = sum(
            1
            for r in known_records
            if r.get(dim) == SKIPPED and bl_per_q.get(r["id"], {}).get(dim) == PASS
        )
        if n_skipped_was_pass:
            warnings.append({
                "kind": "score_unverifiable",
                "message": (
                    f"{score_key} computed over a REDUCED denominator ({scored} scored): "
                    f"{n_skipped_was_pass} baseline-PASS question(s) skipped (seam offline?) "
                    "-- the score is NOT a clean pass; re-run with the seam live"
                ),
            })
        if now_score is None:
            continue  # cannot score (all skipped/N/A) -- skip the check
        # Use a tiny epsilon to avoid float noise causing a false alarm.
        if now_score < bl_score - 1e-9:
            score_regression = True
            warnings.append({
                "kind": "score_drop",
                "message": (
                    f"{score_key} dropped: baseline={bl_score:.4f} now={now_score:.4f} "
                    f"(delta {now_score - bl_score:+.4f}) -- this is a score regression"
                ),
            })

    verdict = FAIL if (regressions or score_regression) else PASS
    return {
        "regressions": regressions,
        "progressions": progressions,
        "warnings": warnings,
        "new_questions": new_questions,
        "verdict": verdict,
        "score_regression": score_regression,
    }


# ---------------------------------------------------------------------------
# ASCII-only regression report (AI-03).
# ---------------------------------------------------------------------------
_LANGFUSE_BASE = "https://cloud.langfuse.com/trace"


def render_gate_report(artifact: dict, comparison: dict) -> str:
    """Produce an ASCII-only regression gate report (AI-03).

    Sections (in order):
      * Header with run summary.
      * REGRESSIONS (if any) -- hard failures.
      * SCORE REGRESSIONS (if any) -- summary score drops.
      * WARNINGS -- non-blocking alerts (skip-was-pass, missing, score drop detail).
      * PROGRESSIONS -- improvements to be locked in.
      * NEW QUESTIONS -- ids not in baseline.
      * Final verdict banner.
    """
    lines: list[str] = []
    s = artifact.get("summary", {})
    comparison_verdict = comparison["verdict"]

    def _fmt_pct(v: float | None) -> str:
        return "n/a" if v is None else f"{v * 100:.1f}%"

    lines.append("=" * 68)
    lines.append("toorow eval gate (Story 14.3) -- regression check")
    lines.append("=" * 68)
    lines.append(f"accuracy_score   : {_fmt_pct(s.get('accuracy_score'))}")
    lines.append(f"citation_score   : {_fmt_pct(s.get('citation_score'))}")
    lines.append(f"total_questions  : {s.get('total_questions')}")
    lines.append(f"replay_ran       : {artifact.get('config', {}).get('replay_ran')}")
    lines.append("-" * 68)

    # Regressions (hard failures).
    reg = comparison["regressions"]
    if reg:
        lines.append(f"REGRESSIONS ({len(reg)}) -- gate will EXIT NON-ZERO:")
        for r in reg:
            lines.append(
                f"  REGRESSED  {r['id']}  [{r['dim']}]"
                f"  baseline={r['baseline_verdict']}  now={r['now_verdict']}"
            )
            # Langfuse trace link (AI-34) -- present only when seam emits trace_id.
            if r.get("trace_id"):
                lines.append(f"    Langfuse trace: {_LANGFUSE_BASE}/{r['trace_id']}")
    else:
        lines.append("REGRESSIONS: none")

    # Score regressions embedded in warnings -- already printed a summary above;
    # print the warning messages separately for completeness.
    score_warnings = [w for w in comparison["warnings"] if w["kind"] == "score_drop"]
    if score_warnings:
        lines.append("-" * 68)
        lines.append("SCORE DROPS (included in verdict):")
        for w in score_warnings:
            lines.append(f"  ! {w['message']}")

    # Non-blocking warnings.
    other_warnings = [w for w in comparison["warnings"] if w["kind"] != "score_drop"]
    if other_warnings:
        lines.append("-" * 68)
        lines.append(f"WARNINGS ({len(other_warnings)}) -- non-blocking:")
        for w in other_warnings:
            lines.append(f"  ~ {w['message']}")

    # Progressions.
    prog = comparison["progressions"]
    if prog:
        lines.append("-" * 68)
        lines.append(
            f"PROGRESSIONS ({len(prog)}) -- non-blocking "
            "(run --update-baseline to lock in):"
        )
        for p in prog:
            lines.append(
                f"  + {p['id']}  [{p['dim']}]"
                f"  was={p['baseline_verdict']}  now={p['now_verdict']}"
            )

    # New questions.
    new_qs = comparison["new_questions"]
    if new_qs:
        lines.append("-" * 68)
        lines.append(f"NEW QUESTIONS ({len(new_qs)}) -- not in baseline (add after review):")
        for q_id in new_qs:
            lines.append(f"  * {q_id}")

    lines.append("=" * 68)
    if comparison_verdict == PASS:
        lines.append("GATE: PASS -- no regressions detected.")
    else:
        lines.append("GATE: FAIL -- regressions detected (see above). Fix before deploying.")
    lines.append("=" * 68)

    text = "\n".join(lines)
    # AI-03: guarantee ASCII-only output.
    return text.encode("ascii", "replace").decode("ascii")


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Eval regression gate (Story 14.3). "
            "Runs the eval corpus and compares against the committed baseline. "
            "Exits non-zero on any regression."
        )
    )
    parser.add_argument(
        "--baseline",
        dest="baseline_path",
        default=str(DEFAULT_BASELINE),
        help="Path to the baseline JSON file (default: server/tests/evals/baseline.json).",
    )
    parser.add_argument(
        "--update-baseline",
        dest="update_baseline",
        action="store_true",
        default=False,
        help=(
            "Write the current run's results as the new baseline and exit zero. "
            "EXPLICIT APPROVAL GESTURE ONLY -- never automatic."
        ),
    )
    parser.add_argument(
        "--as-of",
        dest="as_of",
        default=None,
        help="Optional ISO-8601 datetime override (passed to run_evals).",
    )
    parser.add_argument(
        "--only",
        dest="only",
        default=None,
        help="Filter to a single tag or surface (passed to run_evals).",
    )
    args = parser.parse_args(argv)

    baseline_path = Path(args.baseline_path)

    if not R.CORPUS_PATH.is_file():
        print(f"ERROR: corpus not found at {R.CORPUS_PATH}")
        return 2

    # Run the corpus (inherits 14.2 determinism).
    corpus = R.load_corpus()
    artifact = R.run(corpus, as_of_override=args.as_of, only=args.only)

    # Print the run summary using 14.2's renderer for context.
    print(R.render_summary(artifact))
    print()

    # --update-baseline: explicit write, then exit.
    if args.update_baseline:
        baseline = write_baseline(baseline_path, artifact)
        n = len(baseline["per_question"])
        print(f"Baseline updated: {baseline_path} ({n} questions)")
        print("Commit this file in a dedicated PR with a note explaining the score change.")
        return 0

    # Load the committed baseline and compare.
    try:
        baseline = load_baseline(baseline_path)
    except FileNotFoundError as exc:
        print(str(exc))
        return 2
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2

    comparison = compare_to_baseline(artifact, baseline)
    print(render_gate_report(artifact, comparison))

    return 0 if comparison["verdict"] == PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
