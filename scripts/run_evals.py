"""Story 14.2 -- deterministic, offline evaluation runner for the Golden Questions.

This is the MEASUREMENT INSTRUMENT of the AD-19 / NFR13 non-regression gate. It is
offline and deterministic (NO LLM): for each Golden Question in
``server/tests/evals/corpus.yaml`` it

  1. establishes GROUND TRUTH by executing the ``canonical_correct`` reference SQL on
     the seeded DuckDB warehouse and re-verifying the committed ``fixture_sha256``
     (fails loudly if the corpus itself drifted);
  2. when a deterministic ``tool_invocation`` is encoded, drives the REAL in-process
     FastMCP tool seam (``get_daily_report``), reads the AD-1 envelope, applies the
     question's ``result_selector`` to extract the comparable value(s), and scores
     accuracy by EXECUTED RESULTS (never SQL strings);
  3. scores citations from ``meta.provenance`` (AD-9) and adherence from ``meta.gate``
     (AD-18) -- reporting ``unavailable`` HONESTLY when the seam does not expose it,
     never a false 100%;
  4. counts questions with no ``tool_invocation`` as ``tool_replay: skipped`` SEPARATELY
     -- never folded into the implicit green (AI-53 lesson).

The scoring / comparison helpers are pure functions (``compare_result``,
``score_citations``, ``score_adherence``, ``select_from_envelope``) importable and
unit-testable WITHOUT the live DB or MCP server; only the end-to-end path is
DuckDB / seam gated (mirrors ``server/tests/evals/test_reference_sql_green.py``).

Determinism: the corpus ``as_of`` is imposed (never a wall-clock relative window);
two runs on the same seeds produce bit-identical scores.

Usage (from repo root):
    python scripts/run_evals.py [--as-of ISO] [--json PATH] [--only TAG|SURFACE]
                                [--fail-under FLOAT]

Stdout is ASCII-only (AI-03). Seam usage only (AI-45/AI-56): tools are called through
the MCP client, never via private core functions. The runner READS; it never writes
warehouse data.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Paths + import wiring (must precede any core.* import).
# ---------------------------------------------------------------------------
REPO = Path(__file__).resolve().parent.parent
SERVER = REPO / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

EVALS_DIR = SERVER / "tests" / "evals"
CORPUS_PATH = EVALS_DIR / "corpus.yaml"
DEFAULT_DUCKDB = SERVER / "modules" / "google-analytics" / "seeds" / "local.duckdb"

# Env hygiene: never spin background threads when importing core.main (mirrors the
# integration-test header). Set BEFORE importing core.main anywhere below.
os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("TOOROW_DB_MODE", "duckdb")

# Float tolerance for weighted / ROUND(.,N) scalars. Deliberately TIGHT so a grain
# error (which changes a total by an integer multiple) can never be swallowed. Whole
# aggregates use EXACT equality (tolerance ignored -- see compare_result).
_FLOAT_ABS_TOL = 0.01

# Dimension verdict vocabulary (kept ASCII, stable for the JSON artifact).
PASS = "PASS"
FAIL = "FAIL"
NA = "N/A"
SKIPPED = "skipped"
UNAVAILABLE = "unavailable"


# ---------------------------------------------------------------------------
# DuckDB ground-truth execution (reuse the exact test_reference_sql_green pattern).
# ---------------------------------------------------------------------------
def get_duckdb_path() -> Path | None:
    env_path = os.environ.get("TOOROW_DUCKDB_PATH")
    if env_path:
        p = Path(env_path)
        return p if p.is_file() else None
    return DEFAULT_DUCKDB if DEFAULT_DUCKDB.is_file() else None


# The corpus reference SQL addresses marts via the ``marts.`` alias (created as views
# in the green test's read-WRITE connection). We open the seed file READ-ONLY instead
# (so the in-process get_daily_report tool can open the SAME file concurrently -- a
# read-write connect would exclusively lock it on Windows and the tool's read fails),
# and rewrite ``marts.`` -> ``main_marts.`` (dbt-duckdb's real schema). This is a pure
# schema-name rewrite; the executed rows + sha are byte-identical to the green test.
_MARTS_PREFIX_RE = re.compile(r"\bmarts\.", re.IGNORECASE)


def open_marts_connection(db_path: Path):
    """Open the seed DuckDB file READ-ONLY (no locks, no writes)."""
    import duckdb  # noqa: PLC0415

    return duckdb.connect(str(db_path), read_only=True)


def _rewrite_marts_prefix(sql: str) -> str:
    """Rewrite the ``marts.`` alias to the real ``main_marts.`` schema (read-only path)."""
    return _MARTS_PREFIX_RE.sub("main_marts.", sql)


def execute_reference_sql(conn, sql: str) -> list[dict[str, Any]]:
    """Execute SQL and return row dicts (ISO-serialising dates), exactly as the builder."""
    rel = conn.execute(_rewrite_marts_prefix(sql))
    cols = [desc[0] for desc in rel.description] if rel.description else []
    rows: list[dict[str, Any]] = []
    for r in rel.fetchall():
        rd: dict[str, Any] = {}
        for col, val in zip(cols, r):
            if hasattr(val, "isoformat"):
                val = val.isoformat()
            rd[col] = val
        rows.append(rd)
    return rows


def fixture_sha256_of(rows: list[dict[str, Any]]) -> str:
    """Canonical serialisation + sha256, byte-identical to the builder / green test."""
    payload = json.dumps(rows, sort_keys=True, indent=2, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Pure scoring helpers (DB-free, seam-free -- unit-tested in test_run_evals.py).
# ---------------------------------------------------------------------------
def canonical_query(question: dict) -> dict | None:
    """Return the canonical_correct reference query entry, or None."""
    refs = question.get("reference_queries") or []
    canon = next((r for r in refs if r.get("role") == "canonical_correct"), None)
    if canon is None and len(refs) == 1 and refs[0].get("role") in (None, "canonical_correct"):
        canon = refs[0]
    return canon


def reference_scalar(rows: list[dict]) -> Any:
    """Extract the single aggregate cell from a one-row, one-value-column reference result.

    The corpus canonical fact_sum queries SELECT one aggregate column (aliased). A
    fact_sum reference is exactly one row with one column. Returns that cell (may be
    None for an out-of-range SUM). Raises ValueError if the shape is not a scalar --
    the caller must not silently treat a non-scalar as a scalar.
    """
    if len(rows) != 1:
        raise ValueError(f"expected exactly 1 reference row for a scalar, got {len(rows)}")
    vals = list(rows[0].values())
    if len(vals) != 1:
        raise ValueError(f"expected exactly 1 value column for a scalar, got {len(vals)}")
    return vals[0]


def reference_by_date(rows: list[dict]) -> list[tuple[str, Any]]:
    """Extract [(date, value), ...] from a `date, SUM(value)` reference result.

    The reference rows carry a 'date' column plus one aggregate column. Ordered as the
    reference SQL produced them (the corpus queries all ORDER BY date).
    """
    out: list[tuple[str, Any]] = []
    for r in rows:
        if "date" not in r:
            raise ValueError("fact_sum_by_date reference row missing 'date' column")
        value_cols = [k for k in r.keys() if k != "date"]
        if len(value_cols) != 1:
            raise ValueError(
                "fact_sum_by_date reference row must have exactly one value column, "
                f"got {value_cols}"
            )
        out.append((str(r["date"]), r[value_cols[0]]))
    return out


def select_from_envelope(envelope: dict, selector: dict) -> Any:
    """Apply a result_selector to an AD-1 get_daily_report envelope.

    get_daily_report's envelope carries the RAW fact_daily_kpi rows under
    ``data.rows`` (date, connector, metric, breakdown_dimension, breakdown_value,
    value, pull_id, loaded_at). We re-aggregate on the SAME grain the reference SQL
    pinned (connector + metric + breakdown_dimension) so a naive sum over ALL
    breakdowns can never masquerade as correct.

    Returns:
      * fact_sum          -> a scalar (float) or None when NO matching rows exist
                             (mirrors SUM over an empty set -> NULL);
      * fact_sum_by_date  -> ordered list[(date, float)] grouped by date.
    """
    kind = selector["kind"]
    connector = selector["connector"]
    metric = selector["metric"]
    breakdown = selector["breakdown_dimension"]
    round_to = selector.get("round")

    rows = ((envelope or {}).get("data") or {}).get("rows") or []

    def _matches(row: dict) -> bool:
        return (
            row.get("connector") == connector
            and row.get("metric") == metric
            and row.get("breakdown_dimension") == breakdown
        )

    matched = [r for r in rows if _matches(r)]

    if kind == "fact_sum":
        if not matched:
            return None
        total = 0.0
        for r in matched:
            v = r.get("value")
            if v is not None:
                total += float(v)
        return round(total, round_to) if round_to is not None else total

    if kind == "fact_sum_by_date":
        by_date: dict[str, float] = {}
        for r in matched:
            d = str(r.get("date"))
            v = r.get("value")
            by_date[d] = by_date.get(d, 0.0) + (float(v) if v is not None else 0.0)
        ordered = sorted(by_date.items(), key=lambda kv: kv[0])
        if round_to is not None:
            ordered = [(d, round(v, round_to)) for d, v in ordered]
        return ordered

    raise ValueError(f"unknown result_selector kind: {kind!r}")


def _num_equal(expected: Any, actual: Any, round_to: int | None) -> bool:
    """Right-vs-wrong numeric equality with a tolerance that kills float-reduction noise
    but can NEVER swallow a grain-multiple or off-by-one error.

    A None expected matches ONLY a None actual (an out-of-range/empty aggregate). A NaN
    never equals anything.

    For whole aggregates (round_to is None) the ground truth is DuckDB ``SUM(value)`` while
    the runner re-sums the envelope rows in Python; the two reductions add in different
    orders, so fractional metrics (e.g. cost) diverge in the IEEE-754 trailing bits
    (19358.474399999974 vs ...977). A tight rel/abs tolerance (1e-9 / 1e-6) absorbs that
    noise yet stays orders of magnitude below any real error: an off-by-one unit (diff 1)
    or a 2x/3x grain inflation both FAIL. Integer metrics compare equal within it too.
    For explicitly-rounded scalars (round_to set) the reference is ROUND(.,N); the coarser
    absolute tolerance keeps a hair-beyond-rounding difference failing.
    """
    if expected is None or actual is None:
        return expected is None and actual is None
    try:
        e = float(expected)
        a = float(actual)
    except (TypeError, ValueError):
        return expected == actual
    if e != e or a != a:  # NaN guard
        return False
    if round_to is None:
        return math.isclose(e, a, rel_tol=1e-9, abs_tol=1e-6)
    return abs(e - a) <= _FLOAT_ABS_TOL


def compare_result(expected_rows: list[dict], actual_value: Any, kind: str,
                   round_to: int | None = None) -> tuple[str, Any, Any]:
    """Compare the tool-extracted value against the executed reference result.

    Returns (verdict, expected_repr, actual_repr). verdict is PASS or FAIL. This is the
    discrimination heart of the harness: feeding a naive_wrong reference result where a
    canonical_correct value is expected MUST return FAIL (see test_run_evals.py).
    """
    if kind == "fact_sum":
        expected = reference_scalar(expected_rows)
        ok = _num_equal(expected, actual_value, round_to)
        return (PASS if ok else FAIL, expected, actual_value)

    if kind == "fact_sum_by_date":
        expected_pairs = reference_by_date(expected_rows)
        actual_pairs = actual_value if isinstance(actual_value, list) else []
        if len(expected_pairs) != len(actual_pairs):
            return (FAIL, expected_pairs, actual_pairs)
        for (ed, ev), (ad, av) in zip(expected_pairs, actual_pairs):
            if str(ed) != str(ad) or not _num_equal(ev, av, round_to):
                return (FAIL, expected_pairs, actual_pairs)
        return (PASS, expected_pairs, actual_pairs)

    raise ValueError(f"unknown compare kind: {kind!r}")


def score_citations(
    provenance: list[dict], expected_citations: list[dict]
) -> tuple[str, list[str]]:
    """Score AD-9 citations from meta.provenance against expected_citations.

    Returns (verdict, notes). verdict:
      * N/A  when expected_citations is empty (write-ack / catalog) -- NOT 0;
      * PASS when EVERY expected citation is present (source_system + source_field
        match, and a non-null pull_id when pull_id_required);
      * FAIL when any expected citation is missing/mismatched, or a required pull_id
        is missing.
    """
    if not expected_citations:
        return (NA, ["no citations expected (write-ack / catalog)"])

    prov = provenance or []
    notes: list[str] = []
    ok = True
    for exp in expected_citations:
        want_system = exp.get("source_system")
        want_field = exp.get("source_field")
        pull_required = bool(exp.get("pull_id_required"))
        match = next(
            (
                p for p in prov
                if p.get("source_system") == want_system
                and p.get("source_field") == want_field
            ),
            None,
        )
        if match is None:
            ok = False
            notes.append(f"MISSING citation {want_system}/{want_field}")
            continue
        if pull_required and not match.get("pull_id"):
            ok = False
            notes.append(f"MISSING pull_id for {want_system}/{want_field}")
        else:
            notes.append(f"ok {want_system}/{want_field}")
    return (PASS if ok else FAIL, notes)


def score_adherence(meta: dict) -> tuple[str, dict | None]:
    """Read AD-18 adherence from the envelope meta.gate. HONEST: absent -> unavailable.

    Returns (verdict, detail). verdict is one of {PASS, FAIL, unavailable}:
      * unavailable when meta.gate is absent (the offline seam did not expose it) --
        NEVER a false 100%;
      * PASS when gate.adherent is True; FAIL when explicitly False.
    """
    gate = (meta or {}).get("gate")
    if not isinstance(gate, dict) or "adherent" not in gate:
        return (UNAVAILABLE, None)
    detail = {
        "adherent": bool(gate.get("adherent")),
        "context_tool": gate.get("context_tool"),
        "session_kind": gate.get("session_kind"),
    }
    return (PASS if detail["adherent"] else FAIL, detail)


# ---------------------------------------------------------------------------
# In-process MCP seam (AI-45/AI-56): call tools through the FastMCP client.
# ---------------------------------------------------------------------------
def _envelope_from_result(result: Any) -> dict:
    """Extract the AD-1 structuredContent envelope from a FastMCP tool result."""
    env = getattr(result, "structured_content", None)
    if env is None:
        env = getattr(result, "data", None)
    return env or {}


async def _call_tool_async(tool: str, args: dict) -> tuple[dict, dict | None, bool]:
    """Call one MCP tool in-process; return (envelope, wire_meta, is_error)."""
    from core.main import mcp  # noqa: PLC0415
    from fastmcp.client import Client, FastMCPTransport  # noqa: PLC0415

    async with Client(FastMCPTransport(mcp)) as client:
        result = await client.call_tool(tool, args)
    envelope = _envelope_from_result(result)
    wire_meta = getattr(result, "meta", None) or getattr(result, "_meta", None)
    return envelope, wire_meta, bool(getattr(result, "is_error", False))


def call_tool(tool: str, args: dict) -> tuple[dict, dict | None, bool]:
    """Synchronous wrapper around the in-process async tool call."""
    return asyncio.run(_call_tool_async(tool, args))


# ---------------------------------------------------------------------------
# Runner core.
# ---------------------------------------------------------------------------
def load_corpus(path: Path = CORPUS_PATH) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _question_matches_filter(question: dict, only: str | None) -> bool:
    if not only:
        return True
    if question.get("surface") == only:
        return True
    return only in (question.get("tags") or [])


def _apply_as_of_override(tool_invocation: dict, as_of_override: str | None) -> dict:
    """Return a copy of tool_invocation with an OPTIONAL --as-of override applied.

    The corpus as_of is imposed by default (determinism). --as-of only overrides the
    tool's as_of arg WHEN the invocation already carries one (replay surfaces); it does
    NOT invent a window for non-replay questions.
    """
    if not as_of_override:
        return tool_invocation
    ti = json.loads(json.dumps(tool_invocation))  # deep copy, deterministic
    if "as_of" in ti.get("args", {}):
        ti["args"]["as_of"] = as_of_override
    return ti


def score_question(question: dict, conn, run_replay: bool,
                   as_of_override: str | None) -> dict:
    """Score one question across all dimensions. Returns a machine record."""
    q_id = question["id"]
    surface = question.get("surface")
    canon = canonical_query(question)

    record: dict[str, Any] = {
        "id": q_id,
        "surface": surface,
        "ground_truth": PASS,          # sha of executed reference vs committed fixture
        "accuracy": SKIPPED,           # executed-result equivalence via the tool seam
        "citations": NA,
        "adherence": UNAVAILABLE,
        "tool_replay": SKIPPED,
        "skip_reason": None,
        "notes": [],
        "expected": None,
        "actual": None,
    }

    if canon is None:
        record["ground_truth"] = FAIL
        record["notes"].append("no canonical_correct reference query")
        record["skip_reason"] = "no_canonical_reference"
        return record

    # (1) GROUND TRUTH -- execute reference SQL, verify committed fixture sha256.
    if conn is not None:
        try:
            rows = execute_reference_sql(conn, canon["reference_sql"])
            actual_sha = fixture_sha256_of(rows)
            expected_sha = canon.get("fixture_sha256")
            if actual_sha != expected_sha:
                record["ground_truth"] = FAIL
                record["notes"].append(
                    "corpus drift: fixture sha mismatch "
                    f"(expected {expected_sha}, got {actual_sha})"
                )
            record["_reference_rows"] = rows
        except Exception as exc:  # noqa: BLE001
            record["ground_truth"] = FAIL
            record["notes"].append(f"reference SQL execution failed: {exc}")
    else:
        record["ground_truth"] = SKIPPED
        record["notes"].append("DuckDB seeds unavailable -- ground truth skipped")

    # (2) tool replay + accuracy + citations + adherence (only when encoded + seam-on).
    tool_invocation = question.get("tool_invocation")
    if not tool_invocation:
        record["tool_replay"] = SKIPPED
        record["skip_reason"] = "no_tool_invocation"
        record["notes"].append(
            "no deterministic tool_invocation encoded -- replay skipped (not counted green)"
        )
        record.pop("_reference_rows", None)
        return record

    if not run_replay:
        record["tool_replay"] = SKIPPED
        record["skip_reason"] = "seam_unavailable"
        record["notes"].append("MCP seam / DuckDB unavailable -- replay skipped")
        record.pop("_reference_rows", None)
        return record

    ti = _apply_as_of_override(tool_invocation, as_of_override)
    selector = tool_invocation["result_selector"]
    try:
        envelope, _wire_meta, is_error = call_tool(ti["tool"], ti["args"])
    except Exception as exc:  # noqa: BLE001
        record["tool_replay"] = FAIL
        record["accuracy"] = FAIL
        record["notes"].append(f"tool call raised: {exc}")
        record.pop("_reference_rows", None)
        return record

    record["tool_replay"] = "done"
    meta = (envelope or {}).get("meta") or {}
    provenance = meta.get("provenance") or []

    # AI-68 / AI-34 seam: carry the Langfuse trace id when the envelope exposes it, so the
    # regression report (eval_gate) can link a failing question to its trace. The offline
    # seam does not emit it (value stays None -> no link); the live trace-id->score pass is
    # gated on the Langfuse bring-up. This is the single plug-in point promised in 14.3.
    record["trace_id"] = meta.get("langfuse_trace_id")

    # Accuracy: a tool error is TERMINAL -- FAIL, never overwritten by a subsequent
    # compare. An error envelope carries no data.rows, so the selector would return None;
    # if the reference is also None that would false-PASS the error as a green. Citations
    # and adherence are still scored below from whatever meta the error envelope carries.
    if is_error:
        record["accuracy"] = FAIL
        record["notes"].append("tool returned is_error=True")
    else:
        reference_rows = record.get("_reference_rows")
        if reference_rows is None:
            record["accuracy"] = SKIPPED
            record["notes"].append(
                "no reference rows (ground truth unavailable) -- accuracy skipped"
            )
        else:
            try:
                actual_value = select_from_envelope(envelope, selector)
                verdict, expected_repr, actual_repr = compare_result(
                    reference_rows, actual_value, selector["kind"], selector.get("round")
                )
                record["accuracy"] = verdict
                record["expected"] = expected_repr
                record["actual"] = actual_repr
            except Exception as exc:  # noqa: BLE001
                record["accuracy"] = FAIL
                record["notes"].append(f"selector/compare failed: {exc}")

    # Citations (AD-9).
    cit_verdict, cit_notes = score_citations(provenance, question.get("expected_citations") or [])
    record["citations"] = cit_verdict
    record["notes"].extend(cit_notes)

    # Adherence (AD-18).
    adh_verdict, adh_detail = score_adherence(meta)
    record["adherence"] = adh_verdict
    if adh_detail is not None:
        record["adherence_detail"] = adh_detail

    record.pop("_reference_rows", None)
    return record


def run(corpus: dict, *, as_of_override: str | None = None, only: str | None = None) -> dict:
    """Run the full corpus and return the machine artifact dict."""
    db_path = get_duckdb_path()
    conn = open_marts_connection(db_path) if db_path else None
    run_replay = conn is not None  # the seam reads the SAME seeds; gate replay on them.

    questions = [q for q in corpus.get("questions", []) if _question_matches_filter(q, only)]

    results: list[dict] = []
    try:
        for q in questions:
            results.append(score_question(q, conn, run_replay, as_of_override))
    finally:
        if conn is not None:
            conn.close()

    summary = _summarise(results)
    return {
        "corpus": {
            "schema_version": corpus.get("schema_version"),
            "as_of_anchor": corpus.get("as_of_anchor"),
            "seeds_commit": corpus.get("seeds_commit"),
        },
        "config": {
            "as_of_override": as_of_override,
            "only": only,
            "duckdb_available": conn is not None or db_path is not None,
            "replay_ran": run_replay,
        },
        "summary": summary,
        "results": results,
    }


def _summarise(results: list[dict]) -> dict:
    """Aggregate per-dimension tallies. Skips are counted SEPARATELY (never green)."""
    def _tally(dim: str) -> dict:
        t = {PASS: 0, FAIL: 0, NA: 0, SKIPPED: 0, UNAVAILABLE: 0, "done": 0}
        for r in results:
            v = r.get(dim)
            if v in t:
                t[v] += 1
        return t

    accuracy = _tally("accuracy")
    citations = _tally("citations")
    adherence = _tally("adherence")
    replay = _tally("tool_replay")
    ground = _tally("ground_truth")

    # Accuracy score: PASS / (PASS + FAIL). Skips and N/A excluded from the denominator.
    scored = accuracy[PASS] + accuracy[FAIL]
    accuracy_score = (accuracy[PASS] / scored) if scored else None

    cit_scored = citations[PASS] + citations[FAIL]
    citation_score = (citations[PASS] / cit_scored) if cit_scored else None

    skipped_ids = [r["id"] for r in results if r.get("tool_replay") == SKIPPED]

    return {
        "total_questions": len(results),
        "ground_truth": ground,
        "accuracy": accuracy,
        "accuracy_score": accuracy_score,
        "citations": citations,
        "citation_score": citation_score,
        "adherence": adherence,
        "tool_replay": replay,
        "replay_skipped_ids": skipped_ids,
    }


# ---------------------------------------------------------------------------
# ASCII-only human summary (AI-03).
# ---------------------------------------------------------------------------
def _fmt_pct(score: float | None) -> str:
    return "n/a" if score is None else f"{score * 100:.1f}%"


def render_summary(artifact: dict) -> str:
    s = artifact["summary"]
    cfg = artifact["config"]
    lines: list[str] = []
    lines.append("=" * 68)
    lines.append("toorow eval runner (Story 14.2) -- offline, deterministic")
    lines.append("=" * 68)
    lines.append(f"corpus schema_version : {artifact['corpus'].get('schema_version')}")
    lines.append(f"as_of_anchor          : {artifact['corpus'].get('as_of_anchor')}")
    lines.append(f"seeds_commit          : {artifact['corpus'].get('seeds_commit')}")
    lines.append(f"duckdb_available      : {cfg.get('duckdb_available')}")
    lines.append(f"replay_ran            : {cfg.get('replay_ran')}")
    if cfg.get("as_of_override"):
        lines.append(f"as_of_override        : {cfg['as_of_override']}")
    if cfg.get("only"):
        lines.append(f"only filter           : {cfg['only']}")
    lines.append("-" * 68)
    lines.append(f"total questions       : {s['total_questions']}")
    g = s["ground_truth"]
    lines.append(
        f"ground truth (fixture): PASS={g[PASS]} FAIL={g[FAIL]} skipped={g[SKIPPED]}"
    )
    a = s["accuracy"]
    lines.append(
        f"accuracy (results)    : PASS={a[PASS]} FAIL={a[FAIL]} "
        f"skipped={a[SKIPPED]} score={_fmt_pct(s['accuracy_score'])}"
    )
    c = s["citations"]
    lines.append(
        f"citations (AD-9)      : PASS={c[PASS]} FAIL={c[FAIL]} "
        f"N/A={c[NA]} score={_fmt_pct(s['citation_score'])}"
    )
    ad = s["adherence"]
    lines.append(
        f"adherence (AD-18)     : PASS={ad[PASS]} FAIL={ad[FAIL]} "
        f"unavailable={ad[UNAVAILABLE]}"
    )
    r = s["tool_replay"]
    lines.append(
        f"tool replay           : done={r['done']} skipped={r[SKIPPED]} "
        f"(skips excluded from the green total)"
    )
    if s["replay_skipped_ids"]:
        lines.append("-" * 68)
        lines.append("SKIPPED (no tool_invocation / seam) -- counted separately:")
        for qid in s["replay_skipped_ids"]:
            lines.append(f"  - {qid}")
    # Per-question FAIL detail (accuracy or citation), so a red is never silent.
    fails = [
        r_ for r_ in artifact["results"]
        if r_.get("accuracy") == FAIL or r_.get("citations") == FAIL
        or r_.get("ground_truth") == FAIL
    ]
    if fails:
        lines.append("-" * 68)
        lines.append("FAILURES:")
        for r_ in fails:
            reasons = []
            if r_.get("ground_truth") == FAIL:
                reasons.append("ground_truth")
            if r_.get("accuracy") == FAIL:
                reasons.append("accuracy")
            if r_.get("citations") == FAIL:
                reasons.append("citations")
            lines.append(f"  - {r_['id']} [{','.join(reasons)}]")
    lines.append("=" * 68)
    # Guarantee ASCII-only (AI-03): drop any stray non-ASCII defensively.
    text = "\n".join(lines)
    return text.encode("ascii", "replace").decode("ascii")


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic offline eval runner (Story 14.2)."
    )
    parser.add_argument("--as-of", dest="as_of", default=None,
                        help="Optional ISO-8601 datetime override for replay tool as_of args.")
    parser.add_argument("--json", dest="json_path", default=None,
                        help="Write the machine JSON artifact to this path.")
    parser.add_argument("--only", dest="only", default=None,
                        help="Filter to a single tag or surface.")
    parser.add_argument("--fail-under", dest="fail_under", type=float, default=None,
                        help="Exit non-zero when the accuracy score is below this fraction (0..1).")
    args = parser.parse_args(argv)

    if not CORPUS_PATH.is_file():
        print(f"ERROR: corpus not found at {CORPUS_PATH}")
        return 2

    corpus = load_corpus()
    artifact = run(corpus, as_of_override=args.as_of, only=args.only)

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(artifact, indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )

    print(render_summary(artifact))

    # --fail-under gate (for 14.3). When accuracy could not be scored (no replay), a
    # requested gate cannot be satisfied -> fail loudly rather than a false green.
    if args.fail_under is not None:
        score = artifact["summary"]["accuracy_score"]
        if score is None:
            print(
                "FAIL-UNDER: accuracy unscored (replay did not run); "
                f"gate {args.fail_under} not met."
            )
            return 1
        if score < args.fail_under:
            print(f"FAIL-UNDER: accuracy {score:.4f} < {args.fail_under}.")
            return 1
        print(f"FAIL-UNDER: accuracy {score:.4f} >= {args.fail_under} -- OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
