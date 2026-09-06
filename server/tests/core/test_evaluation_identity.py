"""The declared evaluation identity, and the door it does NOT open.

Ratified 2026-08-24, `docs/product-architecture/analyze-and-test.md`. Two of that
section's `Incomplete if` clauses are asserted here -- "the evaluation identity
can open anything in production", and "admitted by the environment alone, with no
membership read". The third (a run's summary names the identity) is asserted in
`server/tests/evals/test_run_evals.py`.

These are DB-FREE on purpose: the refusal happens BEFORE any connection is used,
and the connection handed in RAISES if touched, so a green here cannot be a
membership row answering by accident. The live negative control -- a real
membership row in a real database, still refused in production -- is
`server/tests/integration/test_evaluation_identity_pg.py`.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from core.evaluation_identity import (
    ENVIRONMENT_VARS,
    EVALUATION_IDENTITY,
    declared_environment,
    evaluation_identity_admitted,
    is_evaluation_identity,
)


@pytest.fixture(autouse=True)
def _undeclared_environment(monkeypatch):
    """Start every case from an environment that declares NOTHING."""
    for name in ENVIRONMENT_VARS:
        monkeypatch.delenv(name, raising=False)


def _conn_that_must_not_be_used():
    """A connection whose every use raises -- so a pass cannot come from the database."""
    conn = MagicMock()
    conn.cursor.side_effect = AssertionError(
        "the production-identity door must refuse before any read"
    )
    return conn


def test_an_undeclared_environment_is_production():
    assert declared_environment() == "production"
    assert evaluation_identity_admitted(EVALUATION_IDENTITY) is False


@pytest.mark.parametrize("variable", ENVIRONMENT_VARS)
@pytest.mark.parametrize("value", ["production", "prod", "staging", "preprod", "", "  "])
def test_no_non_evaluation_environment_admits_the_identity(monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    assert evaluation_identity_admitted(EVALUATION_IDENTITY) is False


@pytest.mark.parametrize("variable", ENVIRONMENT_VARS)
def test_every_declared_evaluation_alias_admits_it(monkeypatch, variable):
    monkeypatch.setenv(variable, "evaluation")
    assert evaluation_identity_admitted(EVALUATION_IDENTITY) is True


@pytest.mark.parametrize(
    "identity",
    ["anonymous", "", None, "person_01KYHQX4RK24Z6QJCYWX6DYVSQ", "PERSON_EVALUATION"],
)
def test_the_declaration_admits_nobody_else(monkeypatch, identity):
    """The environment is not a general amnesty: it admits ONE named identity."""
    monkeypatch.setenv("TOOROW_ENVIRONMENT", "evaluation")
    assert is_evaluation_identity(identity) is False
    assert evaluation_identity_admitted(identity) is False


def test_production_refuses_the_evaluation_identity_before_reading_anything(monkeypatch):
    """THE NEGATIVE CONTROL. Production answers `production_identity_required`."""
    from core.project_access import resolve_strict_resource_access

    monkeypatch.setenv("TOOROW_ENVIRONMENT", "production")
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    decision = resolve_strict_resource_access(
        EVALUATION_IDENTITY, _conn_that_must_not_be_used(), project_id="proj_EXAMPLE"
    )
    assert decision.allowed is False
    assert decision.reason == "production_identity_required"


def test_an_undeclared_deployment_refuses_it_too(monkeypatch):
    """A deployment that sets no environment variable is production. Fail-closed."""
    from core.project_access import resolve_strict_resource_access

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    decision = resolve_strict_resource_access(
        EVALUATION_IDENTITY, _conn_that_must_not_be_used(), project_id="proj_EXAMPLE"
    )
    assert decision.reason == "production_identity_required"


def test_the_evaluation_environment_opens_the_door_and_then_asks_membership(monkeypatch):
    """Admitted is not granted: past the door, the membership read still decides.

    The connection answers `None` to the scope+membership join -- i.e. no row --
    and the decision must be `not_found`, the SAME refusal any stranger gets.
    Were admission a grant, this would be allowed.
    """
    from core.project_access import resolve_strict_resource_access

    monkeypatch.setenv("TOOROW_ENVIRONMENT", "evaluation")
    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = None
    conn.cursor.return_value = cur

    decision = resolve_strict_resource_access(
        EVALUATION_IDENTITY, conn, project_id="proj_EXAMPLE"
    )
    assert decision.allowed is False
    assert decision.reason == "not_found"


def test_anonymous_is_still_refused_inside_the_evaluation_environment(monkeypatch):
    """The declaration does not resurrect the anonymous caller it replaced."""
    from core.project_access import resolve_strict_resource_access

    monkeypatch.setenv("TOOROW_ENVIRONMENT", "evaluation")
    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    decision = resolve_strict_resource_access(
        "anonymous", _conn_that_must_not_be_used(), project_id="proj_EXAMPLE"
    )
    assert decision.reason == "production_identity_required"
