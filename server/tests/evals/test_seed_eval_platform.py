"""The evals tree has exactly one file that WRITES, and this is what it refuses.

`Incomplete if` (analyze-and-test.md, 2026-08-24): "the seeder can write governed
rows into a database that is not disposable". Both refusals are asserted, and so
is the fact that NEITHER alone suffices -- an evaluation process pointed at a
production DSN, and a `_test` database opened by a production process, are two
different accidents.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import seed_eval_platform as S  # noqa: E402
from core.evaluation_identity import ENVIRONMENT_VARS  # noqa: E402

_DISPOSABLE = "postgresql://connector:secret@127.0.0.1:55490/toorow_test"
_PRODUCTION_SHAPED = "postgresql://connector:secret@db.example.com:5432/connector"


@pytest.fixture(autouse=True)
def _undeclared_environment(monkeypatch):
    for name in ENVIRONMENT_VARS:
        monkeypatch.delenv(name, raising=False)


def test_an_undeclared_environment_refuses_even_a_test_database(monkeypatch):
    with pytest.raises(S.EvalSeedRefused) as excinfo:
        S.assert_disposable_target(_DISPOSABLE)
    # The refusal names the GESTURE, not the cause.
    assert "TOOROW_ENVIRONMENT=evaluation" in str(excinfo.value)


def test_an_evaluation_process_still_refuses_a_database_that_is_not_disposable(monkeypatch):
    monkeypatch.setenv("TOOROW_ENVIRONMENT", "evaluation")
    with pytest.raises(S.EvalSeedRefused) as excinfo:
        S.assert_disposable_target(_PRODUCTION_SHAPED)
    assert "must end in '_test'" in str(excinfo.value)
    assert "disposable_postgres.py up" in str(excinfo.value)


def test_a_production_process_refuses_a_test_database(monkeypatch):
    monkeypatch.setenv("TOOROW_ENVIRONMENT", "production")
    with pytest.raises(S.EvalSeedRefused):
        S.assert_disposable_target(_DISPOSABLE)


def test_both_conditions_together_are_accepted(monkeypatch):
    monkeypatch.setenv("TOOROW_ENVIRONMENT", "evaluation")
    S.assert_disposable_target(_DISPOSABLE)  # does not raise


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u:p@h:5432/connector",
        "postgresql://u:p@h:5432/toorow_test_live",
        "postgresql://u:p@h:5432/toorow_test?sslmode=require&x=_test",
    ],
)
def test_a_name_that_merely_contains_test_is_not_a_test_database(monkeypatch, dsn):
    """`endswith`, never `in`: `toorow_test_live` is not disposable, and a query
    parameter is not a database name."""
    monkeypatch.setenv("TOOROW_ENVIRONMENT", "evaluation")
    if S._database_name(dsn) == "toorow_test":
        S.assert_disposable_target(dsn)  # the query-string case IS the test database
        return
    with pytest.raises(S.EvalSeedRefused):
        S.assert_disposable_target(dsn)


def test_the_corpus_and_the_seeder_name_the_SAME_governed_route():
    """The corpus expects what the seeder writes -- one declaration, not two.

    A corpus that named a domain the seeder does not create would score
    `missing_path` and read as a product defect.
    """
    import yaml

    corpus = yaml.safe_load(
        (Path(__file__).parent / "corpus.yaml").read_text(encoding="utf-8")
    )
    routes = [
        route
        for question in corpus["questions"]
        for route in question.get("expected_business_routes") or []
    ]
    assert routes, "no corpus question declares a business route any more"
    for route in routes:
        assert route["domain_id"] == S.EVAL_BUSINESS_DOMAIN_ID
        assert route["target_type"] == S.EVAL_REPORT_TARGET_TYPE
        assert route["target_id"] == S.EVAL_REPORT_TARGET_ID
