"""Validator for evaluation corpus YAML (Story 14.1).

Validates schema compliance, uniqueness, surface coverage, and grain-trap requirements.
Uses ASCII-only stdout per AI-03.

FIX 2026-07-20 (scoring-honesty hardening):
- card min 8 -> 10; card_catalog min 2 added.
- grain_trap SHA diff check: naive_wrong fixture_sha256 must differ from canonical_correct.
- DB-free SHA recomputation: each fixture file's actual bytes are SHA-256'd and compared
  to the declared fixture_sha256 (catches stale/wrong committed fixtures without a DB).
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

import yaml

VALID_SURFACES = {
    "daily_report",
    "expert_report",
    "card",
    "card_catalog",
    "dq",
    "as_of",
    "grain_trap",
}
VALID_DIFFICULTIES = {"easy", "medium", "hard"}
VALID_ROLES = {"canonical_correct", "naive_wrong", "supplementary"}
SHA256_REGEX = re.compile(r"^[0-9a-f]{64}$")

# Story 14.2: tool_invocation addendum validation.
VALID_TOOL_NAMES = {"get_daily_report"}
VALID_SELECTOR_KINDS = {"fact_sum", "fact_sum_by_date"}


def _validate_tool_invocation(ti: object, prefix: str) -> list[str]:
    """Validate the optional Story 14.2 tool_invocation block. Returns error strings."""
    errs: list[str] = []
    if not isinstance(ti, dict):
        return [f"{prefix}: 'tool_invocation' must be a mapping."]

    tool = ti.get("tool")
    if tool not in VALID_TOOL_NAMES:
        errs.append(
            f"{prefix}: tool_invocation.tool '{tool}' must be one of {sorted(VALID_TOOL_NAMES)}."
        )

    args = ti.get("args")
    if not isinstance(args, dict):
        errs.append(f"{prefix}: tool_invocation.args must be a mapping.")
    else:
        if args.get("project_id") != "default":
            errs.append(f"{prefix}: tool_invocation.args.project_id must be 'default'.")
        connectors = args.get("connectors")
        if not isinstance(connectors, list) or not connectors:
            errs.append(f"{prefix}: tool_invocation.args.connectors must be a non-empty list.")
        dr = args.get("date_range")
        if not isinstance(dr, dict) or "start" not in dr or "end" not in dr:
            errs.append(
                f"{prefix}: tool_invocation.args.date_range must have 'start' and 'end'."
            )

    selector = ti.get("result_selector")
    if not isinstance(selector, dict):
        errs.append(f"{prefix}: tool_invocation.result_selector must be a mapping.")
    else:
        if selector.get("kind") not in VALID_SELECTOR_KINDS:
            errs.append(
                f"{prefix}: result_selector.kind '{selector.get('kind')}' must be one of "
                f"{sorted(VALID_SELECTOR_KINDS)}."
            )
        for key in ("connector", "metric", "breakdown_dimension"):
            if not selector.get(key) or not isinstance(selector.get(key), str):
                errs.append(f"{prefix}: result_selector.{key} must be a non-empty string.")
        if "round" not in selector:
            errs.append(f"{prefix}: result_selector.round is required (int or null).")
        elif selector["round"] is not None and not isinstance(selector["round"], int):
            errs.append(f"{prefix}: result_selector.round must be an int or null.")
    return errs


def validate_corpus(corpus_path: str | Path) -> list[str]:
    """Validate corpus file at given path and return list of error strings."""
    path = Path(corpus_path)
    if not path.is_file():
        return [f"Corpus file not found: {path}"]

    try:
        content = path.read_text(encoding="utf-8")
        data = yaml.safe_load(content)
    except Exception as exc:
        return [f"YAML parsing error: {exc}"]

    if not isinstance(data, dict):
        return ["Corpus root must be a YAML mapping object."]

    errors = []

    # Top-level required fields
    for field in (
        "schema_version",
        "as_of_anchor",
        "seeds_commit",
        "dbt_build_commit",
        "created_by",
        "questions",
    ):
        if field not in data:
            errors.append(f"Missing top-level field: '{field}'")

    questions = data.get("questions")
    if not isinstance(questions, list):
        return errors + ["'questions' must be a list of question records."]

    if not (45 <= len(questions) <= 60):
        errors.append(f"Corpus question count ({len(questions)}) must be between 45 and 60.")

    seen_ids = set()
    surface_counts: dict[str, int] = {}

    for idx, q in enumerate(questions):
        prefix = f"Question [{idx}]"
        if not isinstance(q, dict):
            errors.append(f"{prefix}: Record must be a dict.")
            continue

        q_id = q.get("id")
        if not q_id or not isinstance(q_id, str):
            errors.append(f"{prefix}: Missing or invalid 'id'.")
        elif q_id in seen_ids:
            errors.append(f"{prefix}: Duplicate id '{q_id}'.")
        else:
            seen_ids.add(q_id)
            prefix = f"Question '{q_id}'"

        if not q.get("question") or not isinstance(q.get("question"), str):
            errors.append(f"{prefix}: Missing or invalid 'question' text.")

        if not q.get("as_of") or not isinstance(q.get("as_of"), str):
            errors.append(f"{prefix}: Missing or invalid 'as_of' date.")

        surface = q.get("surface")
        if surface not in VALID_SURFACES:
            errors.append(
                f"{prefix}: Invalid surface '{surface}'. Must be one of {sorted(VALID_SURFACES)}."
            )
        else:
            surface_counts[surface] = surface_counts.get(surface, 0) + 1

        if q.get("difficulty") not in VALID_DIFFICULTIES:
            diffs = sorted(VALID_DIFFICULTIES)
            errors.append(
                f"{prefix}: Invalid difficulty '{q.get('difficulty')}'. Must be one of {diffs}."
            )

        tags = q.get("tags")
        if not isinstance(tags, list) or not tags:
            errors.append(f"{prefix}: 'tags' must be a non-empty list of strings.")

        ref_queries = q.get("reference_queries")
        if not isinstance(ref_queries, list) or not ref_queries:
            errors.append(f"{prefix}: 'reference_queries' must be a non-empty list.")
        else:
            roles_found = set()
            for q_idx, query_entry in enumerate(ref_queries):
                entry_prefix = f"{prefix} query [{q_idx}]"
                if not isinstance(query_entry, dict):
                    errors.append(f"{entry_prefix}: Must be a dict.")
                    continue

                role = query_entry.get("role")
                if role:
                    if role not in VALID_ROLES:
                        roles_str = sorted(VALID_ROLES)
                        errors.append(
                            f"{entry_prefix}: Invalid role '{role}'. Must be one of {roles_str}."
                        )
                    roles_found.add(role)

                ref_sql = query_entry.get("reference_sql")
                if not ref_sql or not isinstance(ref_sql, str):
                    errors.append(f"{entry_prefix}: Missing or empty 'reference_sql'.")

                fixture = query_entry.get("expected_result_fixture")
                if not fixture or not isinstance(fixture, str):
                    errors.append(f"{entry_prefix}: Missing or empty 'expected_result_fixture'.")

                sha = query_entry.get("fixture_sha256")
                if not sha or not SHA256_REGEX.match(sha):
                    errors.append(f"{entry_prefix}: Invalid 'fixture_sha256' hex hash.")

            if surface == "grain_trap":
                if not q.get("grain_trap_note"):
                    errors.append(f"{prefix}: Surface 'grain_trap' requires 'grain_trap_note'.")
                if "naive_wrong" not in roles_found or "canonical_correct" not in roles_found:
                    msg = (
                        f"{prefix}: Surface 'grain_trap' reference_queries must contain "
                        "both 'naive_wrong' and 'canonical_correct' roles."
                    )
                    errors.append(msg)
                else:
                    # FIX 2026-07-20: grain_trap naive_wrong and canonical_correct must produce
                    # DIFFERENT fixtures (otherwise the trap does not discriminate).
                    naive_sha = next(
                        (
                            e.get("fixture_sha256")
                            for e in ref_queries
                            if e.get("role") == "naive_wrong"
                        ),
                        None,
                    )
                    canon_sha = next(
                        (
                            e.get("fixture_sha256")
                            for e in ref_queries
                            if e.get("role") == "canonical_correct"
                        ),
                        None,
                    )
                    if (
                        naive_sha
                        and canon_sha
                        and SHA256_REGEX.match(naive_sha)
                        and SHA256_REGEX.match(canon_sha)
                        and naive_sha == canon_sha
                    ):
                        errors.append(
                            f"{prefix}: grain_trap naive_wrong and canonical_correct share "
                            f"the same fixture_sha256 ({naive_sha[:12]}...) — the trap does "
                            "not discriminate."
                        )

        citations = q.get("expected_citations")
        if not isinstance(citations, list):
            errors.append(f"{prefix}: 'expected_citations' must be a list.")

        if not q.get("updated_at"):
            errors.append(f"{prefix}: Missing 'updated_at' timestamp.")

        # Story 14.2: validate the optional tool_invocation addendum when present.
        if "tool_invocation" in q and q.get("tool_invocation") is not None:
            errors.extend(_validate_tool_invocation(q["tool_invocation"], prefix))

    # Check minimum surface distribution requirements (AC G1)
    # FIX 2026-07-20: card min raised 8->10 (actual corpus has 12); card_catalog min 2 added.
    min_surfaces = {
        "daily_report": 8,
        "expert_report": 8,
        "card": 10,
        "card_catalog": 2,
        "dq": 4,
        "as_of": 4,
        "grain_trap": 8,
    }
    for surf, min_count in min_surfaces.items():
        count = surface_counts.get(surf, 0)
        if count < min_count:
            msg = (
                f"Surface coverage check failed: '{surf}' count is {count}, "
                f"minimum required is {min_count}."
            )
            errors.append(msg)

    # FIX 2026-07-20: DB-free fixture SHA recomputation.
    # Walk every committed fixture file and verify its SHA-256 matches the declared value.
    # This catches stale fixtures committed without regenerating corpus.yaml.
    corpus_dir = path.parent
    fixtures_dir = corpus_dir / "fixtures"
    if fixtures_dir.is_dir():
        for q in questions:
            if not isinstance(q, dict):
                continue
            q_id = q.get("id", "?")
            for q_entry in q.get("reference_queries", []):
                if not isinstance(q_entry, dict):
                    continue
                fixture_rel = q_entry.get("expected_result_fixture", "")
                declared_sha = q_entry.get("fixture_sha256", "")
                if not fixture_rel or not declared_sha or not SHA256_REGEX.match(declared_sha):
                    continue  # structural errors already reported above
                fixture_path = corpus_dir / fixture_rel
                if not fixture_path.is_file():
                    errors.append(
                        f"Question '{q_id}': fixture file not found: {fixture_rel}"
                    )
                    continue
                actual_bytes = fixture_path.read_bytes()
                actual_sha = hashlib.sha256(actual_bytes).hexdigest()
                if actual_sha != declared_sha:
                    errors.append(
                        f"Question '{q_id}': fixture SHA-256 mismatch for {fixture_rel}\n"
                        f"    declared: {declared_sha}\n"
                        f"    actual:   {actual_sha}\n"
                        f"    (regenerate fixtures by running build_eval_corpus_and_fixtures.py)"
                    )

    return errors


def main() -> None:
    corpus_path = sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent / "corpus.yaml"
    errors = validate_corpus(corpus_path)
    if errors:
        print(f"FAILED: Corpus validation found {len(errors)} error(s):")
        for err in errors:
            print(f"  - {err}")
        sys.exit(1)
    print("SUCCESS: Corpus schema validation passed with 0 errors.")


if __name__ == "__main__":
    main()
