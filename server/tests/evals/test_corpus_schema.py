"""T3: Meta-test for evaluation corpus schema validation (Story 14.1).

Validates server/tests/evals/corpus.yaml using validate_corpus().
Requires no database.
"""

from __future__ import annotations

import sys
from pathlib import Path

evals_dir = Path(__file__).parent
if str(evals_dir) not in sys.path:
    sys.path.insert(0, str(evals_dir))

from validate_corpus import validate_corpus  # noqa: E402


def test_corpus_schema_validation():
    corpus_path = Path(__file__).parent / "corpus.yaml"
    errors = validate_corpus(corpus_path)
    assert not errors, "Corpus schema validation failed with errors:\n" + "\n".join(errors)
