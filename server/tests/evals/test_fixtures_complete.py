"""T4: Meta-test for evaluation fixture completeness (Story 14.1).

Asserts every expected_result_fixture in corpus.yaml exists under server/tests/evals/
and that no orphaned fixture files exist in server/tests/evals/fixtures/.
Requires no database.
"""

from __future__ import annotations

from pathlib import Path

import yaml


def test_fixtures_completeness():
    evals_dir = Path(__file__).parent
    corpus_path = evals_dir / "corpus.yaml"
    fixtures_dir = evals_dir / "fixtures"

    assert corpus_path.is_file(), "corpus.yaml not found"
    assert fixtures_dir.is_dir(), "fixtures directory not found"

    data = yaml.safe_load(corpus_path.read_text(encoding="utf-8"))
    questions = data.get("questions", [])

    expected_fixture_paths = set()
    for q in questions:
        for q_entry in q.get("reference_queries", []):
            fixture_rel = q_entry.get("expected_result_fixture")
            assert fixture_rel, f"Question {q.get('id')} query missing expected_result_fixture"
            fixture_full = (evals_dir / fixture_rel).resolve()
            assert fixture_full.is_file(), (
                f"Fixture file missing for question '{q.get('id')}': {fixture_full}"
            )
            expected_fixture_paths.add(fixture_full)

    existing_fixture_files = set(f.resolve() for f in fixtures_dir.glob("*.json"))
    orphan_fixtures = existing_fixture_files - expected_fixture_paths
    assert not orphan_fixtures, (
        f"Found orphan fixture files not referenced in corpus.yaml: {orphan_fixtures}"
    )
