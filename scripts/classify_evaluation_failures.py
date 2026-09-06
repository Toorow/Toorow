#!/usr/bin/env python
"""Name, per case of a finalized Evaluation Run, the gap that would fill it.

WHY THIS SCRIPT EXISTS. Epic 75 story 75-7 asks for a before/after cohort: run
the golden questions, classify the failures, fill the gaps through the rails of
75-1 / 75-3 / 75-4 / 75-5, run again, report the delta. A run already states six
verdicts per case and their counts per dimension -- it has never stated WHAT
WOULD HAVE TO CHANGE for a failing case to pass. So a delta between two runs
could say that a number moved and never which gap the repair filled.

WHERE THE RULES LIVE. Not here. `server/core/evaluation_failure_classes.py`
holds them as pure functions over dicts, and this script only loads rows and
calls it; the tests call the same functions. A second copy of a rule is how one
class starts meaning two things depending on who asked.

WHAT IT REFUSES.

  * A run that is still `recording` -- by name. A run that can still grow would
    have its classification silently invalidated by its next case.
  * A comparison between two runs whose `question_set_fingerprint` differs --
    by name, with both fingerprints. `analyze-and-test.md` holds the question
    set constant across a comparison; a delta between two different cohorts is
    not a measurement.
  * A comparison between two runs whose ENVIRONMENT moved -- by name, with the
    pins that differ. The same document holds the Semantic View version, the
    Context Version Set, the model, the host capability profile, the tool
    catalog version, the data snapshot and `as_of` constant; a delta over a
    changed environment measures the environment and calls it a repair.

READ-ONLY, AND IT SAYS SO TO THE DATABASE. One transaction, opened
`SET TRANSACTION READ ONLY`, rolled back at the end. This instrument reads
immutable evidence and writes nothing -- not a row, not a verdict, not an
annotation.

Usage:
    python scripts/classify_evaluation_failures.py --dsn "$TEST_POSTGRES_DSN" \
        --run-id erun_... [--project proj_...] [--markdown | --json]
    python scripts/classify_evaluation_failures.py --dsn "$TEST_POSTGRES_DSN" \
        --run-id erun_AFTER --compare erun_BEFORE --markdown
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

_SERVER = Path(__file__).resolve().parent.parent / "server"
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

from core.evaluation_failure_classes import (  # noqa: E402
    DIMENSIONS,
    ENVIRONMENT_PINS,
    LIFECYCLE_FINALIZED,
    RULE_ORDER,
    VERDICTS,
    EnvironmentDrift,
    FingerprintMismatch,
    classification_report,
    compare_runs,
)


class Refused(RuntimeError):
    """A named refusal, printed as one sentence and exited on."""


# ---------------------------------------------------------------------------
# Loading. Every query is a SELECT.
# ---------------------------------------------------------------------------


def _connect(dsn: str):
    import psycopg  # noqa: PLC0415 -- optional at import time, required to run

    return psycopg.connect(dsn, connect_timeout=15)


def _load_run(conn, *, run_id: str, project_id: str | None) -> dict[str, Any]:
    """The run, its lifecycle and the SEVEN environment pins a comparison holds.

    The pins are read here rather than at comparison time because they are
    columns of `app.evaluation_runs` (migration 153, section ENVIRONMENT): a
    delta that did not read them could hold the question set constant, let the
    Semantic View version or the model move underneath, and report the change of
    environment as a repair.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, org_id, project_id, lifecycle, question_set_fingerprint,
                   started_at, ended_at,
                   semantic_view_id, semantic_view_version_id, context_version_set_id,
                   model_ref, host_capability_profile, tool_catalog_version,
                   data_snapshot_ref, data_snapshot_hash, as_of
              FROM app.evaluation_runs
             WHERE id = %s
            """,
            (run_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise Refused(
            f"no Evaluation Run `{run_id}` in this database. Check the run id, or "
            "open one through POST /api/projects/{project_id}/test/evaluation-runs."
        )
    run = {
        "run_id": row[0],
        "org_id": row[1],
        "project_id": row[2],
        "lifecycle": row[3],
        "question_set_fingerprint": row[4],
        "started_at": row[5],
        "ended_at": row[6],
        "semantic_view_id": row[7],
        "environment": {
            "semantic_view_version_id": row[8],
            "context_version_set_id": row[9],
            "model_ref": row[10],
            "host_capability_profile": row[11],
            "tool_catalog_version": row[12],
            "data_snapshot_hash": row[14],
            "as_of": row[15],
        },
        "data_snapshot_ref": row[13],
    }
    if project_id and run["project_id"] != project_id:
        raise Refused(
            f"Evaluation Run `{run_id}` belongs to project `{run['project_id']}`, not "
            f"`{project_id}`. Name the Project that opened it, or drop --project."
        )
    if run["lifecycle"] != LIFECYCLE_FINALIZED:
        raise Refused(
            f"Evaluation Run `{run_id}` is `{run['lifecycle']}`, not `finalized`: a run "
            "that can still grow would have its classification invalidated by its next "
            "case. Finalize it (POST .../evaluation-runs/{run_id}/finalize) or unroll "
            "it with the executor, then classify."
        )
    return run


def _load_cases(conn, *, run: dict[str, Any]) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, c.golden_question_version_id, c.result_id, c.ai_path_id,
                   c.ai_path_absent_literal, c.capability_key, c.result_type,
                   c.unresolved_pins, c.render_ref,
                   v.question, v.time_boundary, v.severity, v.semantic_view_id,
                   v.semantic_view_version_id, v.contract_version, v.expected_ai_path,
                   q.title
              FROM app.evaluation_run_cases c
              JOIN app.golden_question_versions v
                ON v.id = c.golden_question_version_id AND v.org_id = c.org_id
               AND v.project_id = c.project_id
              JOIN app.golden_questions q
                ON q.id = v.golden_question_id AND q.org_id = v.org_id
               AND q.project_id = v.project_id
             WHERE c.run_id = %s AND c.org_id = %s AND c.project_id = %s
             ORDER BY c.created_at, c.id
            """,
            (run["run_id"], run["org_id"], run["project_id"]),
        )
        rows = cur.fetchall()
    names = (
        "case_id",
        "golden_question_version_id",
        "result_id",
        "ai_path_id",
        "ai_path_absent_literal",
        "capability_key",
        "result_type",
        "unresolved_pins",
        "render_ref",
        "question",
        "time_boundary",
        "severity",
        "semantic_view_id",
        "semantic_view_version_id",
        "contract_version",
        "expected_ai_path",
        "title",
    )
    return [dict(zip(names, row, strict=False)) for row in rows]


def _load_verdicts(conn, *, run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.case_id, d.dimension, d.verdict, d.reason_code, d.evidence_refs
              FROM app.evaluation_case_dimension_verdicts d
              JOIN app.evaluation_run_cases c
                ON c.id = d.case_id AND c.org_id = d.org_id AND c.project_id = d.project_id
             WHERE c.run_id = %s AND d.org_id = %s AND d.project_id = %s
             ORDER BY d.case_id, d.dimension
            """,
            (run["run_id"], run["org_id"], run["project_id"]),
        )
        rows = cur.fetchall()
    out: dict[str, dict[str, Any]] = {}
    for case_id, dimension, verdict, reason, evidence in rows:
        out.setdefault(case_id, {})[dimension] = {
            "verdict": verdict,
            "reason_code": reason,
            "evidence_refs": evidence,
        }
    return out


def _load_path_comparisons(conn, *, run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.case_id, p.evidence_state, p.path_verdict, p.required_missing,
                   p.forbidden_present, p.order_violations, p.version_mismatches
              FROM app.evaluation_path_comparisons p
              JOIN app.evaluation_run_cases c
                ON c.id = p.case_id AND c.org_id = p.org_id AND c.project_id = p.project_id
             WHERE c.run_id = %s AND p.org_id = %s AND p.project_id = %s
            """,
            (run["run_id"], run["org_id"], run["project_id"]),
        )
        rows = cur.fetchall()
    return {
        row[0]: {
            "evidence_state": row[1],
            "path_verdict": row[2],
            "required_missing": row[3],
            "forbidden_present": row[4],
            "order_violations": row[5],
            "version_mismatches": row[6],
        }
        for row in rows
    }


def _load_assertion_results(conn, *, run: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.case_id, a.assertion_ordinal, a.assertion_type, a.verdict,
                   a.reason_code, a.evidence_refs
              FROM app.evaluation_assertion_results a
              JOIN app.evaluation_run_cases c
                ON c.id = a.case_id AND c.org_id = a.org_id AND c.project_id = a.project_id
             WHERE c.run_id = %s AND a.org_id = %s AND a.project_id = %s
             ORDER BY a.case_id, a.assertion_ordinal
            """,
            (run["run_id"], run["org_id"], run["project_id"]),
        )
        rows = cur.fetchall()
    out: dict[str, list[dict[str, Any]]] = {}
    for case_id, ordinal, kind, verdict, reason, evidence in rows:
        out.setdefault(case_id, []).append(
            {
                "assertion_ordinal": ordinal,
                "assertion_type": kind,
                "verdict": verdict,
                "reason_code": reason,
                "evidence_refs": evidence,
            }
        )
    return out


def _settings_sources(conn, *, run: dict[str, Any]) -> dict[str, str | None]:
    """Which scope the cascade resolved each AI setting from -- `None` when unread.

    A fact nobody could read must not fire a rule, so a failure here yields
    `None` and the fiscal / scope class simply does not trigger, instead of
    triggering on an assumption.
    """
    try:
        from core.ai_settings import resolve  # noqa: PLC0415

        with conn.transaction():
            resolved = resolve(conn, org_id=run["org_id"], project_id=run["project_id"])
    except Exception:  # noqa: BLE001 -- an unread fact is `None`, never a default
        return {"fiscal_calendar": None, "query_scope": None}
    sources = resolved.get("sources") or {}
    return {
        "fiscal_calendar": sources.get("fiscal_calendar"),
        "query_scope": sources.get("query_scope"),
    }


def _exemplars_active(conn, *, run: dict[str, Any], semantic_view_id: str | None) -> int | None:
    """How many exemplars story 75-3 would serve for this case's View, or `None`.

    `core.exemplars.list_exemplars` is the ONE selector: counting here with a
    query of my own would be a second definition of "an active exemplar of this
    subject", and the two would drift.
    """
    if not semantic_view_id:
        return None
    try:
        from core.exemplars import list_exemplars  # noqa: PLC0415

        with conn.transaction():
            served = list_exemplars(
                conn,
                org_id=run["org_id"],
                project_id=run["project_id"],
                subject_type="view",
                subject_id=semantic_view_id,
            )
    except Exception:  # noqa: BLE001 -- see `_settings_sources`
        return None
    return int(served.get("served") or 0) + int(served.get("omitted_for_budget") or 0)


def load_report(conn, *, run_id: str, project_id: str | None) -> dict[str, Any]:
    """Load one finalized run and classify it. Every statement here is a SELECT."""
    run = _load_run(conn, run_id=run_id, project_id=project_id)
    cases = _load_cases(conn, run=run)
    verdicts = _load_verdicts(conn, run=run)
    comparisons = _load_path_comparisons(conn, run=run)
    assertions = _load_assertion_results(conn, run=run)
    sources = _settings_sources(conn, run=run)

    enriched: list[dict[str, Any]] = []
    exemplar_cache: dict[str, int | None] = {}
    for case in cases:
        view_id = case.get("semantic_view_id")
        if view_id not in exemplar_cache:
            exemplar_cache[view_id] = _exemplars_active(
                conn, run=run, semantic_view_id=view_id
            )
        enriched.append(
            {
                **case,
                "verdicts": verdicts.get(case["case_id"], {}),
                "path_comparison": comparisons.get(case["case_id"]),
                "assertion_results": assertions.get(case["case_id"], []),
                "exemplars_active": exemplar_cache[view_id],
                "fiscal_calendar_source": sources["fiscal_calendar"],
                "query_scope_source": sources["query_scope"],
            }
        )

    report = classification_report(
        run_id=run["run_id"],
        project_id=run["project_id"],
        lifecycle=run["lifecycle"],
        question_set_fingerprint=run["question_set_fingerprint"],
        cases=enriched,
        environment=run["environment"],
    )
    report["org_id"] = run["org_id"]
    return report


# ---------------------------------------------------------------------------
# Printing. The command that produced a number is printed beside it.
# ---------------------------------------------------------------------------


def _short(value: Any, width: int) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 3] + "..."


def _print_markdown(report: dict[str, Any], *, command: str) -> None:
    print(f"# Failure classification -- run `{report['run_id']}`")
    print()
    print(f"- Project: `{report['project_id']}`")
    print(f"- Lifecycle: `{report['lifecycle']}`")
    print(f"- Question set fingerprint: `{report['question_set_fingerprint']}`")
    print(f"- Cases: {report['case_count']}")
    print(f"- Produced by: `{command}`")
    print()
    print("| Question | Verdicts | Class | Story | Gesture |")
    print("| --- | --- | --- | --- | --- |")
    for item in report["cases"]:
        passed = len(item["passed_dimensions"])
        failed = len(item["failed_dimensions"])
        print(
            f"| {_short(item['question'], 60)} "
            f"| {passed} pass, {failed} fail "
            f"| `{item['class']}` | {item['story']} | {_short(item['gesture'], 90)} |"
        )
    print()
    print("## Cases per class")
    print()
    print("| Class | Cases |")
    print("| --- | --- |")
    for name in RULE_ORDER:
        print(f"| `{name}` | {report['class_counts'][name]} |")
    print()
    print("## Verdicts per dimension")
    print()
    print("| Dimension | " + " | ".join(VERDICTS) + " |")
    print("| --- |" + " --- |" * len(VERDICTS))
    for dimension in DIMENSIONS:
        row = report["dimension_counts"][dimension]
        print(
            f"| `{dimension}` | " + " | ".join(str(row[v]) for v in VERDICTS) + " |"
        )
    print()
    print("## Signals, per case")
    print()
    for item in report["cases"]:
        print(f"- **{_short(item['question'], 80)}** -- `{item['class']}`")
        for signal in item["signals"]:
            print(f"  - {signal}")
        print(f"  - door: `{item['door']}`")


def _print_human(report: dict[str, Any], *, command: str) -> None:
    print(f"EVALUATION RUN {report['run_id']}  ({report['lifecycle']})")
    print(f"    project {report['project_id']}   cases {report['case_count']}")
    print(f"    question set fingerprint {report['question_set_fingerprint']}")
    print(f"    produced by: {command}")
    print()
    header = f"{'QUESTION':<52} {'VERDICTS':<16} {'CLASS':<24} STORY"
    print(header)
    print("-" * len(header))
    for item in report["cases"]:
        passed = len(item["passed_dimensions"])
        failed = len(item["failed_dimensions"])
        print(
            f"{_short(item['question'], 52):<52} "
            f"{f'{passed} pass, {failed} fail':<16} {item['class']:<24} {item['story']}"
        )
    print()
    print("CASES PER CLASS")
    for name in RULE_ORDER:
        print(f"    {name:<24} {report['class_counts'][name]}")
    print()
    print("VERDICTS PER DIMENSION (every verdict listed, so the denominator travels)")
    print(f"    {'dimension':<24} " + " ".join(f"{v:>14}" for v in VERDICTS))
    for dimension in DIMENSIONS:
        row = report["dimension_counts"][dimension]
        print(f"    {dimension:<24} " + " ".join(f"{row[v]:>14}" for v in VERDICTS))
    print()
    print("WHAT WOULD FILL EACH CASE")
    for item in report["cases"]:
        print(f"    [{item['class']}] {_short(item['question'], 70)}")
        for signal in item["signals"]:
            print(f"        signal: {signal}")
        print(f"        gesture: {item['gesture']}")
        print(f"        door:    {item['door']}")


def _print_delta_markdown(delta: dict[str, Any], *, command: str) -> None:
    print("# Before / after -- the measured loop (story 75-7)")
    print()
    print(f"- Before: `{delta['before_run_id']}` ({delta['case_count_before']} cases)")
    print(f"- After: `{delta['after_run_id']}` ({delta['case_count_after']} cases)")
    print(f"- Question set fingerprint (identical, or this refuses): "
          f"`{delta['question_set_fingerprint']}`")
    print(f"- Produced by: `{command}`")
    print()
    print("## The environment both runs held (identical, or this refuses)")
    print()
    print("| Pin | Value |")
    print("| --- | --- |")
    for pin in ENVIRONMENT_PINS:
        print(f"| `{pin}` | `{_short(delta['environment'].get(pin), 90)}` |")
    print()
    print("## Pass counts per dimension")
    print()
    print("| Dimension | pass before | pass after | delta | fail before | fail after | delta |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for dimension in DIMENSIONS:
        row = delta["dimensions"][dimension]
        print(
            f"| `{dimension}` | {row['pass_before']} | {row['pass_after']} | "
            f"{row['pass_delta']:+d} | {row['fail_before']} | {row['fail_after']} | "
            f"{row['fail_delta']:+d} |"
        )
    print()
    print("## Cases per class")
    print()
    print("| Class | before | after | delta |")
    print("| --- | --- | --- | --- |")
    for name in RULE_ORDER:
        row = delta["classes"][name]
        print(f"| `{name}` | {row['before']} | {row['after']} | {row['delta']:+d} |")
    print()
    print(
        "No precision percentage and no pass rate: a cohort that answers better on "
        "one dimension and worse on another has not improved by n %."
    )


def _print_delta_human(delta: dict[str, Any], *, command: str) -> None:
    print(
        f"BEFORE {delta['before_run_id']} ({delta['case_count_before']} cases)  ->  "
        f"AFTER {delta['after_run_id']} ({delta['case_count_after']} cases)"
    )
    print(f"    same question set fingerprint {delta['question_set_fingerprint']}")
    print(f"    produced by: {command}")
    print()
    print("THE ENVIRONMENT BOTH RUNS HELD (identical, or this refuses)")
    for pin in ENVIRONMENT_PINS:
        print(f"    {pin:<28} {_short(delta['environment'].get(pin), 80)}")
    print()
    print("PASS / FAIL PER DIMENSION")
    print(f"    {'dimension':<24} {'pass':>18} {'fail':>18}")
    for dimension in DIMENSIONS:
        row = delta["dimensions"][dimension]
        passes = "{}->{} ({:+d})".format(row["pass_before"], row["pass_after"], row["pass_delta"])
        fails = "{}->{} ({:+d})".format(row["fail_before"], row["fail_after"], row["fail_delta"])
        print(f"    {dimension:<24} {passes:>18} {fails:>18}")
    print()
    print("CASES PER CLASS")
    for name in RULE_ORDER:
        row = delta["classes"][name]
        print(f"    {name:<24} {row['before']} -> {row['after']} ({row['delta']:+d})")
    print()
    print(
        "No precision percentage and no pass rate: a cohort that answers better on "
        "one dimension and worse on another has not improved by n %."
    )


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dsn",
        default=os.environ.get("PLATFORM_DB_URL") or os.environ.get("TEST_POSTGRES_DSN"),
        help="Postgres DSN to read (default: PLATFORM_DB_URL, then TEST_POSTGRES_DSN)",
    )
    parser.add_argument("--run-id", required=True, help="the finalized Evaluation Run to classify")
    parser.add_argument("--project", help="refuse the run if it belongs to another Project")
    parser.add_argument(
        "--compare",
        metavar="RUN_ID",
        help="the BEFORE run to compare this one against (same question set, or refused)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--markdown", action="store_true", help="markdown, for a report file")
    args = parser.parse_args(argv)

    if args.json and args.markdown:
        print("choose one of --json and --markdown", file=sys.stderr)
        return 2
    if not args.dsn:
        print(
            "no DSN: pass --dsn, or export PLATFORM_DB_URL / TEST_POSTGRES_DSN",
            file=sys.stderr,
        )
        return 2

    # The command a reader can re-run. The DSN is named, never printed: a
    # connection string in a committed report is a credential in a repository.
    parts = ["python scripts/classify_evaluation_failures.py", '--dsn "$PLATFORM_DB_URL"']
    parts.append(f"--run-id {args.run_id}")
    if args.project:
        parts.append(f"--project {args.project}")
    if args.compare:
        parts.append(f"--compare {args.compare}")
    if args.markdown:
        parts.append("--markdown")
    if args.json:
        parts.append("--json")
    command = " ".join(parts)

    conn = _connect(args.dsn)
    try:
        with conn.cursor() as cur:
            # Said to the database, not merely intended: this instrument reads
            # immutable evidence and a write from here would corrupt it.
            cur.execute("SET TRANSACTION READ ONLY")
        try:
            report = load_report(conn, run_id=args.run_id, project_id=args.project)
            before = (
                load_report(conn, run_id=args.compare, project_id=args.project)
                if args.compare
                else None
            )
        except Refused as refusal:
            print(f"REFUSED: {refusal}", file=sys.stderr)
            return 2

        if before is None:
            if args.json:
                print(json.dumps(report, indent=2, sort_keys=True, default=str))
            elif args.markdown:
                _print_markdown(report, command=command)
            else:
                _print_human(report, command=command)
            return 0

        try:
            delta = compare_runs(before, report)
        except (FingerprintMismatch, EnvironmentDrift) as mismatch:
            print(f"REFUSED: {mismatch}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(delta, indent=2, sort_keys=True, default=str))
        elif args.markdown:
            _print_delta_markdown(delta, command=command)
        else:
            _print_delta_human(delta, command=command)
        return 0
    finally:
        conn.rollback()
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
