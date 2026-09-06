"""The LIVE negative control of the declared evaluation identity (AI-305).

`server/tests/core/test_evaluation_identity.py` proves the refusal with a
connection that raises if touched -- which proves the door closes, and nothing
about what a real database would have said. That is exactly the gap this file
fills, and the reason it exists rather than being folded into the unit test: the
dangerous version of the defect is a REAL membership row for `person_EVALUATION`
sitting in a real database, being honoured because auth was on and the
environment was never consulted.

So every case below runs against a live schema with the membership row PRESENT:

  * production, row present  -> refused, `production_identity_required`;
  * undeclared, row present  -> refused (an undeclared deployment is production);
  * evaluation, row present  -> allowed, and the reason names the GRANT, so the
    capability floor is proven to still be read;
  * evaluation, row REMOVED  -> refused `not_found`, the same refusal a stranger
    gets. Admission is a door, not a grant.

The last two are the mutation pair: without them, "allowed in evaluation" could
be a resolver that stopped asking.
"""

from __future__ import annotations

import os

import psycopg
import pytest
from core.evaluation_identity import EVALUATION_IDENTITY
from core.project_access import resolve_strict_resource_access

_DSN = os.environ.get("TEST_POSTGRES_DSN", "")

pytestmark = pytest.mark.skipif(not _DSN, reason="Requires TEST_POSTGRES_DSN")

_ORG = "org_eval_identity_test"
_PROJECT = "proj_eval_identity_test"


@pytest.fixture(autouse=True)
def _undeclared_environment(monkeypatch):
    from core.evaluation_identity import ENVIRONMENT_VARS

    for name in ENVIRONMENT_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def conn():
    connection = psycopg.connect(_DSN)
    try:
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, created_by) "
                "VALUES (%s, 'evaluation identity', %s, 'test') "
                "ON CONFLICT (id) DO NOTHING",
                (_ORG, _ORG),
            )
            cur.execute(
                "INSERT INTO app.projects (id, name, slug, created_by, org_id) "
                "VALUES (%s, 'evaluation identity', %s, 'test', %s) "
                "ON CONFLICT (id) DO NOTHING",
                (_PROJECT, _PROJECT, _ORG),
            )
            # THE ROW THE WHOLE FILE IS ABOUT: a real, active membership for the
            # evaluation identity, plus the exact grant a non-owner needs.
            cur.execute(
                "INSERT INTO app.org_members (id, org_id, identity, role, status, joined_at) "
                "VALUES (%s, %s, %s, 'viewer', 'active', NOW()) "
                "ON CONFLICT (org_id, identity) DO NOTHING",
                ("omem_eval_identity_test", _ORG, EVALUATION_IDENTITY),
            )
            cur.execute(
                "INSERT INTO app.resource_grants "
                "(id, org_id, identity, scope_type, scope_id, capability, granted_by) "
                "VALUES (%s, %s, %s, 'project', %s, 'view', 'test') "
                "ON CONFLICT (org_id, identity, scope_type, scope_id) DO NOTHING",
                ("rgrant_eval_identity_test", _ORG, EVALUATION_IDENTITY, _PROJECT),
            )
        connection.commit()
        yield connection
    finally:
        with connection.cursor() as cur:
            cur.execute(
                "DELETE FROM app.resource_grants WHERE org_id = %s AND identity = %s",
                (_ORG, EVALUATION_IDENTITY),
            )
            cur.execute(
                "DELETE FROM app.org_members WHERE org_id = %s AND identity = %s",
                (_ORG, EVALUATION_IDENTITY),
            )
            # The project and the organization are LEFT. Creating a project fires
            # the triggers that populate `app.project_capabilities` (and its
            # neighbours), so deleting it here raises a foreign-key violation in
            # teardown -- an error that hides whichever test actually ran. The two
            # rows are inserted `ON CONFLICT DO NOTHING` under ids only this file
            # uses, so leaving them is idempotent; the suite-wide pg scrub in
            # `server/tests/conftest.py` owns the rest.
        connection.commit()
        connection.close()


@pytest.mark.parametrize("auth_mode", ["oauth", "static", "disabled"])
def test_production_refuses_it_even_with_a_live_membership_row(conn, monkeypatch, auth_mode):
    monkeypatch.setenv("TOOROW_ENVIRONMENT", "production")
    monkeypatch.setenv("TOOROW_AUTH_MODE", auth_mode)
    decision = resolve_strict_resource_access(
        EVALUATION_IDENTITY, conn, project_id=_PROJECT, minimum_capability="view"
    )
    assert decision.allowed is False
    assert decision.reason == "production_identity_required"


def test_an_undeclared_deployment_refuses_it_with_the_row_present(conn, monkeypatch):
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    decision = resolve_strict_resource_access(
        EVALUATION_IDENTITY, conn, project_id=_PROJECT, minimum_capability="view"
    )
    assert decision.allowed is False
    assert decision.reason == "production_identity_required"


def test_the_evaluation_environment_admits_it_through_its_grant(conn, monkeypatch):
    monkeypatch.setenv("TOOROW_ENVIRONMENT", "evaluation")
    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    decision = resolve_strict_resource_access(
        EVALUATION_IDENTITY, conn, project_id=_PROJECT, minimum_capability="view"
    )
    assert decision.allowed is True
    # `explicit_grant`, never `owner_floor`: the capability floor was READ.
    assert decision.reason == "explicit_grant"
    assert decision.capability == "view"
    assert decision.org_id == _ORG


def test_it_cannot_edit_what_it_may_only_view(conn, monkeypatch):
    """A read identity stays a read identity: admission granted nothing extra."""
    monkeypatch.setenv("TOOROW_ENVIRONMENT", "evaluation")
    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    decision = resolve_strict_resource_access(
        EVALUATION_IDENTITY, conn, project_id=_PROJECT, minimum_capability="edit"
    )
    assert decision.allowed is False
    assert decision.reason == "insufficient_capability"


def test_without_the_membership_row_the_evaluation_environment_refuses_it(conn, monkeypatch):
    """THE MUTATION. Remove the row and the same call must go red."""
    monkeypatch.setenv("TOOROW_ENVIRONMENT", "evaluation")
    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM app.org_members WHERE org_id = %s AND identity = %s",
            (_ORG, EVALUATION_IDENTITY),
        )
    conn.commit()
    decision = resolve_strict_resource_access(
        EVALUATION_IDENTITY, conn, project_id=_PROJECT, minimum_capability="view"
    )
    assert decision.allowed is False
    assert decision.reason == "not_found"


def test_no_other_identity_gains_anything_from_the_declaration(conn, monkeypatch):
    """The environment admits ONE identity, not a class of them."""
    monkeypatch.setenv("TOOROW_ENVIRONMENT", "evaluation")
    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    decision = resolve_strict_resource_access(
        "person_01KYHQX4RK24Z6QJCYWX6DYVSQ", conn, project_id=_PROJECT
    )
    assert decision.allowed is False
    assert decision.reason == "production_identity_required"
