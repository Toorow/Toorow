"""Story 42.9 -- who a credential belongs to, and what that lets you do.

Pure-function tests over ``_credential_authorship``: no database, no ASGI seam,
so the rule that decides whether a Revoke button appears is pinned in
milliseconds rather than in the slow admin_api seam suite.

The distinction under test is the one migration 101 spells out and that the old
flat connections table lost: the PERSON who consented at the provider is not the
ORGANIZATION the credential is usable in, and only the first of the two governs
update/revoke.
"""

from core.me_api import _credential_authorship


def _ref(**overrides):
    base = {
        "owner_identity": "jean",
        "owner_display_name": "Jean-Ludovic Albany",
        "owner_email": "owner@example.com",
        "caller_manages_owner": False,
    }
    base.update(overrides)
    return base


def test_author_may_update_and_revoke():
    out = _credential_authorship(_ref(), "jean", False)
    assert out["is_mine"] is True
    assert out["can_update"] is True
    assert out["can_revoke"] is True


def test_org_admin_may_revoke_but_never_update():
    """The backstop. An admin cannot re-consent at the provider on someone
    else's behalf, so offering Update would be a button that cannot work."""
    out = _credential_authorship(_ref(caller_manages_owner=True), "someone_else", False)
    assert out["is_mine"] is False
    assert out["can_update"] is False
    assert out["can_revoke"] is True


def test_plain_member_may_do_neither():
    out = _credential_authorship(_ref(), "colleague", False)
    assert out["can_update"] is False
    assert out["can_revoke"] is False
    # ... but still learns whose access feeds their reports.
    assert out["owner_display_name"] == "Jean-Ludovic Albany"


def test_granted_credential_never_discloses_the_person():
    """A beneficiary org sees the owning ORGANIZATION, never the human behind it,
    and gets no action -- even if it happens to have an admin calling."""
    out = _credential_authorship(_ref(caller_manages_owner=True), "jean", True)
    assert out["owner_identity"] is None
    assert out["owner_display_name"] is None
    assert out["is_mine"] is False
    assert out["can_update"] is False
    assert out["can_revoke"] is False


def test_display_name_falls_back_rather_than_showing_nobody():
    """A credential exists from the first token; the profile row only appears
    once its owner fills it in.

    'Connected by <nothing>' would be a worse answer -- and the raw identity is
    not the only other one. Measured 2026-08-11 on the served console, the
    Credentials screen printed `person_01KYHQX4RK24Z6QJCYWX6DYVSQ` and
    `anonymous` under "Connected by": a row key rendered to a person, which
    identifies nobody (`app.persons` carries id and timestamps only, so there is
    no directory to resolve it in). The third answer is a sentence, and the two
    absences are not the same fact."""
    no_profile = _ref(owner_display_name=None, owner_email=None)
    assert (
        _credential_authorship(no_profile, "x", False)["owner_display_name"]
        == "A member of this organization"
    )

    before_identity = _ref(owner_display_name=None, owner_email=None, owner_identity="anonymous")
    assert (
        _credential_authorship(before_identity, "x", False)["owner_display_name"]
        == "Connected before sign-in recorded a person"
    )

    email_only = _ref(owner_display_name=None)
    assert (
        _credential_authorship(email_only, "x", False)["owner_display_name"]
        == "owner@example.com"
    )


def test_anonymous_caller_is_never_the_author():
    """Identity resolution failing open would hand out the author's own rights."""
    out = _credential_authorship(_ref(owner_identity=None), None, False)
    assert out["is_mine"] is False
    assert out["can_update"] is False
