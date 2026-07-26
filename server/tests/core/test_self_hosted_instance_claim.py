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
import json
import os
from unittest.mock import patch

from core import admin_api
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
# ---------------------------------------------------------------------------


class _Req:
    """`valid_body=False` sends a deliberately unreadable body so the handler
    stops right AFTER the gates -- proving they were passed without entering the
    real creation path (whose slug-collision loop would spin against a fake DB)."""

    headers: dict = {}

    def __init__(self, valid_body: bool = True):
        self._valid = valid_body

    async def body(self):
        return json.dumps({"name": "Acme"}).encode() if self._valid else b"{not json"


def _create(
    *,
    self_hosted: bool,
    instance_orgs: int | None,
    memberships: int | None = 0,
    identity: str = "sub-123",
    email: str = "ada@example.com",
    super_admins: str = "",
    valid_body: bool = False,
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
            patch.object(admin_api, "_count_instance_organizations", lambda: instance_orgs), \
            patch.object(admin_api, "_count_active_memberships", lambda _keys: memberships):
        return asyncio.run(admin_api._create_org(_Req(valid_body)))


def _payload(response):
    return json.loads(bytes(response.body).decode())


def test_the_operator_may_claim_an_unclaimed_instance():
    resp = _create(
        self_hosted=True,
        instance_orgs=0,
        identity="ada@example.com",
        super_admins="ada@example.com",
    )
    assert resp.status_code == 404
    assert _payload(resp)["code"] == "not_found"


def test_the_operator_is_recognised_on_the_verified_email_not_only_the_subject():
    """A Google `sub` is opaque; the allow-list is a list of emails."""
    resp = _create(
        self_hosted=True,
        instance_orgs=0,
        identity="sub-opaque-999",
        email="ada@example.com",
        super_admins="ada@example.com",
    )
    assert _payload(resp)["code"] == "not_found"


def test_a_stranger_cannot_claim_an_unclaimed_instance():
    """404, not 403: an unclaimed instance must not confirm to a stranger that
    it is sitting there waiting to be claimed."""
    resp = _create(self_hosted=True, instance_orgs=0, super_admins="someone-else@example.com")
    assert resp.status_code == 404
    assert _payload(resp)["code"] == "not_found"


def test_an_empty_allow_list_claims_nothing():
    """Deny-by-default: an operator who never set TOOROW_SUPER_ADMINS has an
    instance nobody can claim -- which is safe, and fixable by setting it."""
    resp = _create(self_hosted=True, instance_orgs=0, super_admins="")
    assert resp.status_code == 404


def test_a_claimed_instance_refuses_a_second_organization_even_to_the_operator():
    """One instance, one organization. The owner is not exempt: this is the
    'bloqué à 1' rule, not a per-person cap."""
    resp = _create(
        self_hosted=True,
        instance_orgs=1,
        identity="ada@example.com",
        super_admins="ada@example.com",
    )
    assert resp.status_code == 404
    assert _payload(resp)["code"] == "not_found"


def test_an_unverifiable_instance_count_fails_closed():
    resp = _create(
        self_hosted=True,
        instance_orgs=None,
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
    onboarding is dead on production."""
    resp = _create(self_hosted=False, instance_orgs=999, memberships=0)
    assert resp.status_code == 400
    assert _payload(resp)["code"] == "invalid_body"


def test_hosted_never_counts_instance_organizations():
    """Not just "allowed" -- the query must not even run. A hosted instance has
    many organizations by design, and consulting that count would be meaningless
    work on every creation."""
    called = {"n": 0}

    def _counter():
        called["n"] += 1
        return 5

    async def _auth(_request):
        return True, "sub-123"

    async def _invite_identity(_request):
        return True, "ada@example.com"

    with patch.dict(os.environ, {"TOOROW_DEPLOYMENT_MODE": "hosted", "TOOROW_SUPER_ADMINS": ""}), \
            patch.object(admin_api, "_check_auth", _auth), \
            patch.object(admin_api, "_check_invitation_identity", _invite_identity), \
            patch.object(admin_api, "_count_instance_organizations", _counter), \
            patch.object(admin_api, "_count_active_memberships", lambda _keys: 0):
        asyncio.run(admin_api._create_org(_Req(valid_body=False)))

    assert called["n"] == 0


def test_hosted_still_caps_a_second_organization_per_person():
    """The per-person cap is untouched by the self-hosted work."""
    resp = _create(self_hosted=False, instance_orgs=0, memberships=1)
    assert resp.status_code == 409
    assert _payload(resp)["code"] == "organization_limit_reached"
