"""A self-hosted instance is claimed ONCE, by its operator.

On somebody else's stack there is no waitlist and no CRM, so no ENTRY invitation
can be issued -- and the hosted rule therefore leaves NOTHING standing in front
of POST /api/organizations. Whoever found a public instance URL first and signed
in with any Google account would create the first organization and own the
instance. That is the hole this closes.

The current rule is stricter: the legacy organization endpoint never claims a
self-hosted instance. Only the one-time bootstrap exchange and atomic
/api/instance/claim command may create its first organization and project.

The load-bearing tests here are the HOSTED ones: this gate must be invisible to
toorow Cloud, where many tenants share one instance and a newcomer who just
accepted an entry invitation must still be able to create their own
organization. Getting that wrong turns production single-tenant, silently.

Unit tests deliberately: the ASGI seam suite costs 30-45s per test.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from unittest.mock import patch

from core import (
    admin_api,
    organizations_api,  # AD-43 : le handler vit chez son sujet
)
from core.deployment_mode import deployment_mode, is_self_hosted

# ---------------------------------------------------------------------------
# The mode switch itself
# ---------------------------------------------------------------------------


def test_hosted_is_the_default_for_anything_unset_or_unrecognised():
    """Guessing wrong in this direction would silently make production
    single-tenant, so only an explicit value flips it."""
    for raw in ("", "   ", "hosted", "cloud", "prod", "SELF HOSTED", "true", "1"):
        with patch.dict(os.environ, {"TOOROW_DEPLOYMENT_MODE": raw}):
            assert is_self_hosted() is False, raw
            assert deployment_mode() == "hosted"


def test_self_hosted_is_recognised_in_the_spellings_an_operator_will_write():
    for raw in ("self_hosted", "self-hosted", "selfhosted", "  Self-Hosted  ", "SELF_HOSTED"):
        with patch.dict(os.environ, {"TOOROW_DEPLOYMENT_MODE": raw}):
            assert is_self_hosted() is True, raw
            assert deployment_mode() == "self_hosted"


def test_the_variable_is_read_live():
    with patch.dict(os.environ, {"TOOROW_DEPLOYMENT_MODE": "self_hosted"}):
        assert is_self_hosted() is True
    with patch.dict(os.environ, {"TOOROW_DEPLOYMENT_MODE": "hosted"}):
        assert is_self_hosted() is False


# ---------------------------------------------------------------------------
# The claim gate
#
# NAMES REWRITTEN, ASSERTIONS UNTOUCHED. Three tests below asserted 404
# `not_found` under names that promised the opposite -- "the operator MAY claim
# an unclaimed instance", "the operator IS RECOGNISED on the verified email",
# "an unverifiable instance count FAILS CLOSED". All three describe a claim path
# that `POST /api/organizations` no longer has: the handler refuses every
# self-hosted caller at `server/core/organizations_api.py#_create_org`, before the allow-list is
# consulted and before the instance count is read. Claiming moved to the
# one-time bootstrap exchange and `/api/instance/claim` (module docstring above).
#
# A test whose name states a capability the endpoint does not have is worse than
# a red one: a reader greps for the guarantee, finds a green test, and believes
# it. Same repair as AI-103 -- the product is right, and the name was the part
# that lied.
#
# NB: the unreachable claim block this note used to report (`instance_already_claimed`
# 409, the `is_super_admin` allow-list check, the 500 on an unverifiable count)
# has been DELETED (AI-129): it sat behind the unconditional self-hosted 404,
# which is the deliberate rule -- claiming lives in /api/instance/claim. The
# `_count_instance_organizations` helper went with it (its only caller was the
# dead block), so these tests no longer patch it.
# ---------------------------------------------------------------------------


class _Req:
    """`valid_body=False` sends a deliberately unreadable body.

    THE STOPPING POINT MOVED, AND THAT IS WHY EIGHT TESTS HERE WENT RED.

    The trick used to be: send an unreadable body, and the handler stops right
    AFTER the gates -- proving they were passed without entering the real
    creation path (whose slug-collision loop would spin against a fake DB).
    `_create_org` now parses the body FIRST, deliberately: the comment at
    `server/core/organizations_api.py#_create_org` records why -- "this validation used to sit
    after the caps, so a blank name reached the org-membership count and
    answered 500 'db_error' when Postgres was not reachable -- an input error
    reported as an outage."

    So an unreadable body no longer stops after the gates; it stops BEFORE them,
    at 400 `invalid_body`, and a test using it asserts nothing about any gate.
    Every test whose subject IS a gate now sends a valid body (`_create` does so
    by default) and stops on the gate's own verdict instead:

      * self-hosted -> 404 `not_found`, refused for everyone including the
        operator, immediately after slug validation and before any DB read;
      * hosted, cap reached -> 409 `organization_limit_reached`.

    Neither reaches the database, so the reason the trick existed is gone.
    """

    headers: dict = {}

    def __init__(self, valid_body: bool = True):
        self._valid = valid_body

    async def body(self):
        return json.dumps({"name": "Acme"}).encode() if self._valid else b"{not json"


def _create(
    *,
    self_hosted: bool,
    memberships: int | None = 0,
    identity: str = "sub-123",
    email: str = "ada@example.com",
    super_admins: str = "",
    valid_body: bool = True,
):
    async def _auth(_request):
        return True, identity

    async def _invite_identity(_request):
        return (True, email) if email else (False, "")

    env = {
        "TOOROW_SUPER_ADMINS": super_admins,
        "TOOROW_DEPLOYMENT_MODE": "self_hosted" if self_hosted else "hosted",
    }
    with patch.dict(os.environ, env), \
            patch.object(admin_api, "_check_auth", _auth), \
            patch.object(admin_api, "_check_invitation_identity", _invite_identity), \
            patch.object(organizations_api, "_count_active_memberships", lambda _keys: memberships):
        return asyncio.run(organizations_api._create_org(_Req(valid_body)))


def _payload(response):
    return json.loads(bytes(response.body).decode())


def test_the_legacy_endpoint_refuses_the_operator_too():
    resp = _create(
        self_hosted=True,
        identity="ada@example.com",
        super_admins="ada@example.com",
    )
    assert resp.status_code == 404
    assert _payload(resp)["code"] == "not_found"


def test_the_verified_email_does_not_open_the_legacy_endpoint_either():
    """A Google `sub` is opaque and the allow-list is a list of emails -- but
    neither is consulted here any more: the refusal comes first."""
    resp = _create(
        self_hosted=True,
        identity="sub-opaque-999",
        email="ada@example.com",
        super_admins="ada@example.com",
    )
    assert _payload(resp)["code"] == "not_found"


def test_a_stranger_cannot_claim_an_unclaimed_instance():
    """404, not 403: an unclaimed instance must not confirm to a stranger that
    it is sitting there waiting to be claimed."""
    resp = _create(self_hosted=True, super_admins="someone-else@example.com")
    assert resp.status_code == 404
    assert _payload(resp)["code"] == "not_found"


def test_an_empty_allow_list_claims_nothing():
    """Deny-by-default: an operator who never set TOOROW_SUPER_ADMINS has an
    instance nobody can claim -- which is safe, and fixable by setting it."""
    resp = _create(self_hosted=True, super_admins="")
    assert resp.status_code == 404


def test_a_claimed_instance_refuses_a_second_organization_even_to_the_operator():
    """One instance, one organization. The owner is not exempt: this is the
    'bloqué à 1' rule, not a per-person cap."""
    resp = _create(
        self_hosted=True,
        identity="ada@example.com",
        super_admins="ada@example.com",
    )
    assert resp.status_code == 404
    assert _payload(resp)["code"] == "not_found"


def test_an_unverifiable_instance_count_is_refused_before_it_is_read():
    resp = _create(
        self_hosted=True,
        identity="ada@example.com",
        super_admins="ada@example.com",
    )
    assert resp.status_code == 404
    assert _payload(resp)["code"] == "not_found"


# ---------------------------------------------------------------------------
# HOSTED must not notice any of this
# ---------------------------------------------------------------------------


def test_hosted_lets_a_newcomer_create_their_own_organization():
    """The nominal toorow Cloud path: somebody who just accepted an ENTRY
    invitation has zero memberships and is on no allow-list. If this ever fails,
    onboarding is dead on production.

    This test had gone VACUOUS and still passed. It asserted 400 `invalid_body`
    -- which, once body parsing moved ahead of the gates, is the FIRST thing the
    handler does. It would have stayed green with every gate refusing every
    newcomer, i.e. with production onboarding dead, which is precisely the
    failure it advertises itself as catching.

    So it now sends a real body and asserts the NEGATIVE that actually carries
    the guarantee: the newcomer is not refused by any gate.

    IT MUST HOLD WITH OR WITHOUT A DATABASE, and that is what broke it on
    2026-08-05. "This unit test deliberately does not have a database" was true
    of the author's shell and false of anyone who exports TEST_POSTGRES_DSN to run
    the pg-gated suites: `get_connection()` then reaches a real Postgres, the org
    is genuinely CREATED, the 201 body carries no `code`, and reading
    `_payload(resp)["code"]` raised KeyError -- the nominal onboarding path
    reported as a failure precisely because it WORKED.

    A 201 is the strongest possible evidence of "not refused", so it is a pass,
    and the org it really created is erased below.
    """
    resp = _create(self_hosted=False, memberships=0)

    refusals = {"not_found", "entry_scope_required", "organization_limit_reached", "invalid_body"}
    code = _payload(resp).get("code")
    assert code not in refusals, f"a hosted newcomer was refused with {resp.status_code} {code}"
    assert resp.status_code not in (400, 404, 409), resp.status_code

    created_id = _payload(resp).get("id")
    if resp.status_code == 201 and created_id:
        from core.db import get_connection

        from tests.conftest import purge_fixture_org

        with get_connection() as conn:
            try:
                purge_fixture_org(conn, created_id)
                conn.commit()
            except Exception:
                conn.rollback()
                raise


def test_hosted_never_counts_instance_organizations():
    """The instance-organization count does not exist any more, anywhere.

    The dead self-hosted claim block was its only caller (deleted under AI-129,
    behind the unconditional self-hosted 404 that is the deliberate rule). This
    pins the absence so a remount of the old gate -- or of the helper -- trips
    here instead of passing silently.
    """
    source = inspect.getsource(admin_api)
    assert not hasattr(admin_api, "_count_instance_organizations")
    assert "_count_instance_organizations" not in source


def test_hosted_still_caps_a_second_organization_per_person():
    """The per-person cap is untouched by the self-hosted work."""
    resp = _create(self_hosted=False, memberships=1)
    assert resp.status_code == 409
    assert _payload(resp)["code"] == "organization_limit_reached"
