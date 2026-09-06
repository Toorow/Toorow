"""Story 50.1 AC1/AC12 -- nothing else is allowed to become a Query Spec or a Result.

This repository already contains four objects that a future contributor could
plausibly mistake for the analytical ones, and one of them literally shares the
name:

  * `app.entity_source_bindings.query_spec` -- connector configuration (mig 089);
  * `warehouse.query_report` -- the legacy report request shape;
  * `render_snapshots` -- a frozen presentation artifact;
  * notebook run envelopes -- a composition wrapper.

AC1 says the analytical model is the only one, and AC9 says the legacy report
shape "must not become a second compiler". Those are the kind of boundary that
erodes silently: someone reuses a familiar helper, the tests still pass, and two
years later there are two answers to one question. These tests make the erosion
loud instead.
"""

from __future__ import annotations

import io
import pathlib
import tokenize

import pytest

_CORE = pathlib.Path(__file__).resolve().parents[2] / "core"
_OWNED = ("query_specs.py", "query_execution.py", "query_specs_api.py")


def _source(name: str) -> str:
    """Return the module's CODE with comments and docstrings removed.

    Scanning raw text would flag the very docstrings that explain why these
    boundaries exist -- the first run of this file failed on its own prose. The
    boundary is about what the code DOES, so the prose is stripped first.
    """
    raw = (_CORE / name).read_text(encoding="utf-8")
    kept: list[str] = []
    previous = tokenize.INDENT
    for token in tokenize.generate_tokens(io.StringIO(raw).readline):
        if token.type == tokenize.COMMENT:
            continue
        # A STRING alone on a logical line is a docstring, not an operand.
        if token.type == tokenize.STRING and previous in (
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.NEWLINE,
            tokenize.NL,
        ):
            previous = token.type
            continue
        if token.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT):
            previous = token.type
        kept.append(token.string)
    return " ".join(kept)


@pytest.mark.parametrize("module", _OWNED)
def test_the_analytical_modules_never_read_connector_query_config(module):
    body = _source(module)
    assert "entity_source_bindings" not in body, (
        f"{module} reads the connector's `query_spec` column. It carries the same "
        "words and is NOT the analytical object (AC1)."
    )


@pytest.mark.parametrize("module", _OWNED)
def test_the_analytical_modules_never_call_the_legacy_report_compiler(module):
    body = _source(module)
    for legacy in ("query_report", "_build_report_query", "get_daily_report"):
        assert legacy not in body, (
            f"{module} calls `{legacy}`. AC9: the legacy report request shape is not "
            "the authority and must not become a second compiler."
        )


@pytest.mark.parametrize("module", _OWNED)
def test_a_result_is_not_a_render_a_notebook_or_a_report(module):
    body = _source(module).lower()
    for foreign in ("render_snapshot", "notebook_run", "report_artifact"):
        assert foreign not in body, (
            f"{module} touches `{foreign}`. AC12: a Result is not a render snapshot, "
            "a notebook run envelope or a report artifact."
        )


def test_execution_uses_the_existing_runners_rather_than_its_own_driver():
    """AC9 allows reuse BEHIND the service; it forbids a second warehouse client."""
    body = _source("query_execution.py")
    assert "from core import warehouse" in body
    for direct in ("import duckdb", "from google.cloud import bigquery"):
        assert direct not in body, (
            "query_execution opens its own warehouse connection. The existing runners "
            "in core.warehouse are the only ones, reused behind this service."
        )


def test_no_presentation_vocabulary_leaks_into_the_analytical_modules():
    """A chart field here would make the Query Spec a presentation contract."""
    for module in _OWNED:
        body = _source(module).lower()
        for word in ('"chart"', '"layout"', '"visualization"', '"narrative"'):
            assert word not in body, f"{module} carries the presentation key {word} (AC12)"
