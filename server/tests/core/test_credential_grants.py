"""Tests for Story 21.3 -- credential ownership + per-account cross-org grants.

Offline: input validation (handlers reject before touching the DB).
Live-Postgres (skipped when TEST_POSTGRES_DSN is unset): the STRUCTURAL isolation
property -- a grant targets a (credential_id, external_account_id) that must exist
in credential_accounts, so you cannot grant a whole/absent credential -- plus the
owner_org_id backfill and the offboarding revoke, on the real schema (migration 037).
"""

from __future__ import annotations

import json
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


def _pg_reachable() -> bool:
    if not os.environ.get("TEST_POSTGRES_DSN"):
        return False
    try:
        import psycopg

        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


pg_available = pytest.mark.skipif(not _pg_reachable(), reason="platform Postgres not reachable")

_AUTH = ("core.admin_api._check_auth", (True, "tester@example.com"))


def _post(credential_id: str, body: dict, **path) -> MagicMock:
    """A credential POST with REAL headers.

    A bare `MagicMock()` answers `headers.get("Idempotency-Key")` with another
    MagicMock -- truthy, and `.strip()` on it returns a MagicMock too. So the
    guard that refuses a request without the header sees one, the mock travels
    into the durable-operation layer, and the route answers **500**. The test
    then reads "the endpoint is broken" from a request no client would ever send.

    `_create_account_grant` is one of the 23 routes that refuse a missing
    `Idempotency-Key` -- the same header whose omission made the console's Expose
    button answer 422 on every click (AI-188). This helper predates the guard.
    """
    req = MagicMock()
    req.path_params = {"credential_id": credential_id, **path}
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    req.headers = {"Idempotency-Key": f"test-{uuid.uuid4().hex[:12]}"}
    return req


def _get(credential_id: str, **path) -> MagicMock:
    req = MagicMock()
    req.path_params = {"credential_id": credential_id, **path}
    return req


# --- live fixture helpers (direct SQL; avoids the heavy create-project flow) ----


def _setup_chain(suffix: str) -> dict:
    """Create org A (owner) + org B (grantee) + a project + a credential (connection_ref).

    Returns the ids. Teardown via _teardown_chain (FK-safe order).
    """
    from core.db import get_connection

    org_a = f"org_a_{suffix}"
    org_b = f"org_b_{suffix}"
    proj = f"proj_{suffix}"
    cred = f"conn_{suffix}"
    with get_connection() as conn:
        with conn.cursor() as cur:
            for oid, nm in ((org_a, "OwnerOrg"), (org_b, "GranteeOrg")):
                cur.execute(
                    "INSERT INTO app.organizations (id, name, slug, created_by) "
                    "VALUES (%s, %s, %s, 'system')",
                    (oid, nm, f"{oid}"),
                )
            cur.execute(
                "INSERT INTO app.projects (id, name, slug, created_by, org_id) "
                "VALUES (%s, %s, %s, 'system', %s)",
                (proj, "P", f"p-{suffix}", org_a),
            )
            cur.execute(
                "INSERT INTO app.connection_ref "
                "(id, provider, nango_connection_id, project_id, owner_org_id, owner_identity) "
                "VALUES (%s, %s, %s, %s, %s, 'tester@example.com')",
                (cred, "google-analytics", f"nango-{suffix}", proj, org_a),
            )
            # ENROL THE ACTOR. This fixture created two organizations and put
            # nobody in either, then called manage-gated routes and expected them
            # to work -- which they did, under the *default-open-until-enrolled*
            # rule of Epic 21: an org with zero members treated any caller as its
            # owner. Story 46.4 removed that rule (see
            # `test_org_enforcement.py::test_zero_members_opens_nothing`), so the
            # seed now has to say who may act, and `_enforce_org_manage` answers
            # 403 rather than silently promoting a passer-by.
            cur.execute(
                "INSERT INTO app.org_members (id, org_id, identity, role, status, joined_at) "
                "VALUES (%s, %s, 'tester@example.com', 'owner', 'active', NOW())",
                (f"omem_{uuid.uuid4().hex[:12]}", org_a),
            )
        conn.commit()
    return {"org_a": org_a, "org_b": org_b, "proj": proj, "cred": cred}


def _teardown_chain(ids: dict) -> None:
    """Erase through the product's own purge, not by hand.

    This deleted `connection_ref`, then `projects`, then `organizations` -- three
    tables out of the 177 the tenant tree actually spans. It was refused by
    `project_capabilities_project_id_fkey` (RESTRICT) the moment a test gave the
    project a capability row, and since the refusal happens in a `finally`, every
    later test in the module inherited the poisoned transaction. Seven failures
    from one teardown, all of them reading as defects of the code under test.

    `test_org_enforcement.py::_drop_org` met this exact defect and fixed it the
    same way; the reasoning in its docstring applies verbatim. `purge_org_tree`
    is the ONE function that knows the whole tree, and it is what
    `DELETE /api/organizations` runs -- so a teardown built on it also exercises
    the deletion path a user actually takes, instead of a private one that can go
    green while the real one is broken.
    """
    from core.db import get_connection

    from tests.conftest import purge_fixture_org

    with get_connection() as conn:
        for org in (ids["org_a"], ids["org_b"]):
            purge_fixture_org(conn, org)
        conn.commit()


# ---------------------------------------------------------------------------
# Offline validation
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_register_account_requires_external_id_422():
    from core.credential_accounts_api import _register_credential_account  # noqa: PLC0415

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _register_credential_account(_post("conn_x", {"label": "no id"}))
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_create_grant_requires_grantee_org_422():
    from core.credential_accounts_api import _create_account_grant  # noqa: PLC0415

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _create_account_grant(
            _post("conn_x", {}, external_account_id="acct_1")
        )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_list_accounts_requires_auth_401():
    from core.credential_accounts_api import _list_credential_accounts  # noqa: PLC0415

    with patch(_AUTH[0], return_value=(False, "")):
        resp = await _list_credential_accounts(_get("conn_x"))
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Live Postgres -- structural isolation
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_expose_one_account_and_list_grants():
    from core.credential_accounts_api import (  # noqa: PLC0415
        _create_account_grant,
        _list_credential_grants,
        _register_credential_account,
    )

    suffix = uuid.uuid4().hex[:8]
    ids = _setup_chain(suffix)
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            for acct in ("acct_12", "acct_37"):
                r = await _register_credential_account(
                    _post(ids["cred"], {"external_account_id": acct, "label": acct})
                )
                assert r.status_code == 201
            # Expose ONLY acct_12 to org B.
            g = await _create_account_grant(
                _post(ids["cred"], {"grantee_org_id": ids["org_b"]}, external_account_id="acct_12")
            )
            assert g.status_code == 201

            grants = json.loads(
                (await _list_credential_grants(_get(ids["cred"]))).body
            )["grants"]
        assert len(grants) == 1
        assert grants[0]["external_account_id"] == "acct_12"
        assert grants[0]["grantee_org_id"] == ids["org_b"]
    finally:
        _teardown_chain(ids)


@pg_available
@pytest.mark.anyio
async def test_grant_on_unregistered_account_404():
    from core.credential_accounts_api import _create_account_grant  # noqa: PLC0415

    suffix = uuid.uuid4().hex[:8]
    ids = _setup_chain(suffix)
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            resp = await _create_account_grant(
                _post(
                    ids["cred"],
                    {"grantee_org_id": ids["org_b"]},
                    external_account_id="never_registered",
                )
            )
        assert resp.status_code == 404
    finally:
        _teardown_chain(ids)


@pg_available
@pytest.mark.anyio
async def test_grant_to_missing_org_404_and_duplicate_409():
    from core.credential_accounts_api import (  # noqa: PLC0415
        _create_account_grant,
        _register_credential_account,
    )

    suffix = uuid.uuid4().hex[:8]
    ids = _setup_chain(suffix)
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            await _register_credential_account(
                _post(ids["cred"], {"external_account_id": "acct_1"})
            )
            miss = await _create_account_grant(
                _post(ids["cred"], {"grantee_org_id": "org_nope"}, external_account_id="acct_1")
            )
            assert miss.status_code == 404

            ok = await _create_account_grant(
                _post(ids["cred"], {"grantee_org_id": ids["org_b"]}, external_account_id="acct_1")
            )
            assert ok.status_code == 201
            dup = await _create_account_grant(
                _post(ids["cred"], {"grantee_org_id": ids["org_b"]}, external_account_id="acct_1")
            )
            assert dup.status_code == 409
    finally:
        _teardown_chain(ids)


@pg_available
@pytest.mark.anyio
async def test_revoke_grant_offboarding():
    from core.credential_accounts_api import (  # noqa: PLC0415
        _create_account_grant,
        _list_credential_grants,
        _register_credential_account,
        _revoke_account_grant,
    )

    suffix = uuid.uuid4().hex[:8]
    ids = _setup_chain(suffix)
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            await _register_credential_account(
                _post(ids["cred"], {"external_account_id": "acct_1"})
            )
            await _create_account_grant(
                _post(ids["cred"], {"grantee_org_id": ids["org_b"]}, external_account_id="acct_1")
            )
            rv = await _revoke_account_grant(
                _get(ids["cred"], external_account_id="acct_1", grantee_org_id=ids["org_b"])
            )
            assert rv.status_code == 200
            grants = json.loads(
                (await _list_credential_grants(_get(ids["cred"]))).body
            )["grants"]
            assert grants == []
            # Second revoke -> 404 (nothing left).
            rv2 = await _revoke_account_grant(
                _get(ids["cred"], external_account_id="acct_1", grantee_org_id=ids["org_b"])
            )
            assert rv2.status_code == 404
    finally:
        _teardown_chain(ids)


@pg_available
def test_grant_fk_rejects_whole_or_absent_credential():
    """AC4: the composite FK forbids a grant on an account not in credential_accounts.

    This is the structural guarantee that you cannot grant a whole/absent credential
    -- a grant MUST name an account that exists in credential_accounts.
    """
    import psycopg
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    ids = _setup_chain(suffix)
    try:
        with get_connection() as conn:
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO app.credential_account_grants "
                        "(id, credential_id, external_account_id, grantee_org_id, granted_by) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (
                            f"cgrant_{suffix}",
                            ids["cred"],
                            "account_never_registered",
                            ids["org_b"],
                            "system",
                        ),
                    )
            conn.rollback()
    finally:
        _teardown_chain(ids)


@pg_available
def test_the_legacy_null_owner_org_can_no_longer_exist():
    """The backfill this tested has nothing left to repair, and cannot have.

    AC5 shipped a backfill -- `UPDATE connection_ref SET owner_org_id = p.org_id
    ... WHERE cr.owner_org_id IS NULL` -- for credentials created before the owner
    org existed. `connection_ref.owner_org_id` is now NOT NULL **and** carries
    `fk_connection_ref_owner_org` to `app.organizations`, so no row can be in the
    state the backfill repairs.

    The test could not be left as it was: forced by the NOT NULL, an earlier
    session replaced the intended `NULL` with the literal `'org_test_fixture'`,
    which makes the backfill's `WHERE ... IS NULL` match nothing. It then asserted
    the row had been rewritten to the test's own org and failed comparing
    `'org_test_fixture'` to `'org_bf_...'`. The premise had been edited out from
    under the assertion -- the same way three tests in
    `test_org_enforcement.py` were.

    Inverted rather than deleted: what retired the backfill is the constraint, so
    the constraint is what gets asserted. If it is ever relaxed, this fails and
    the backfill becomes a live question again.
    """
    import psycopg
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org = f"org_bf_{suffix}"
    proj = f"proj_bf_{suffix}"
    cred = f"conn_bf_{suffix}"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.organizations (id, name, slug, created_by) "
                    "VALUES (%s, %s, %s, 'system')",
                    (org, "BF", f"bf-{suffix}"),
                )
                cur.execute(
                    "INSERT INTO app.projects (id, name, slug, created_by, org_id) "
                    "VALUES (%s, %s, %s, 'system', %s)",
                    (proj, "BFP", f"bfp-{suffix}", org),
                )
            conn.commit()
            # The legacy shape: a credential with no owner organization.
            with pytest.raises(psycopg.errors.NotNullViolation):
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO app.connection_ref "
                        "(id, provider, nango_connection_id, project_id, owner_org_id, "
                        " owner_identity) "
                        "VALUES (%s, %s, %s, %s, NULL, 'tester@example.com')",
                        (cred, "google-analytics", f"nango-{suffix}", proj),
                    )
            conn.rollback()
            # And it cannot point at an organization that does not exist either.
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO app.connection_ref "
                        "(id, provider, nango_connection_id, project_id, owner_org_id, "
                        " owner_identity) "
                        "VALUES (%s, %s, %s, %s, 'org_does_not_exist', 'tester@example.com')",
                        (cred, "google-analytics", f"nango-{suffix}", proj),
                    )
            conn.rollback()
    finally:
        from tests.conftest import purge_fixture_org  # noqa: PLC0415

        with get_connection() as conn:
            purge_fixture_org(conn, org)
            conn.commit()


@pg_available
@pytest.mark.anyio
async def test_list_accounts_scoped_to_owner_org_members():
    """Reads scoping: once the credential's owner org is enrolled (>=1 active
    member), a member lists its accounts (200) but a stranger gets 404."""
    from core.credential_accounts_api import _list_credential_accounts  # noqa: PLC0415
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    ids = _setup_chain(suffix)
    try:
        # Close org_a (owner of the credential) by enrolling an active member.
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.org_members "
                    "(id, org_id, identity, role, status, joined_at) "
                    "VALUES (%s, %s, %s, 'owner', 'active', NOW())",
                    (f"omem_{suffix}", ids["org_a"], "insider@a"),
                )
            conn.commit()
        with patch("core.admin_api._check_auth", return_value=(True, "insider@a")):
            assert (await _list_credential_accounts(_get(ids["cred"]))).status_code == 200
        with patch("core.admin_api._check_auth", return_value=(True, "stranger@z")):
            assert (await _list_credential_accounts(_get(ids["cred"]))).status_code == 404
    finally:
        _teardown_chain(ids)
