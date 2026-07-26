"""The platform learns WHO somebody is when they accept their invitation.

Jean, 2026-07-26: « invitation -> on te demande ton nom et ton prénom (si pas
dispo dans l'oath) -> Tu accedes à une organisation (ou ca te permet d'en creer
une nouvelle) ». Acceptance is the one moment every arrival passes through --
joining an organization or about to create their own -- so that is where the
name is resolved, and the console asks only when the token has none.

Nothing upstream ever collected a name: the marketing form has one field
(email), the CRM stored contacts with name=None, and the platform's Google
DATA flow deliberately requests no identity scope. But the console's own bearer
IS a Google ID token, and those carry `name` / `given_name` / `family_name` --
we were throwing that away and would have addressed invitations to
"jeanludovic.albany@gmail.com invited you to join Acme Media".

The load-bearing test here is IDENTITY_KEY: app.user_profiles and
/api/me/profile key on the token SUBJECT, while invitation acceptance matches on
the verified EMAIL, and a Google `sub` is opaque. Writing the name under the
email would store it where nothing reads it -- "saved" and still asked for, on
every single login.
"""

from __future__ import annotations

from core.user_profiles import display_name_from_claims

# ---------------------------------------------------------------------------
# Reading a name out of whatever the provider sent
# ---------------------------------------------------------------------------


def test_prefers_the_providers_own_display_form():
    assert display_name_from_claims({"name": "Jean Albany"}) == "Jean Albany"


def test_recomposes_given_and_family_when_there_is_no_name():
    claims = {"given_name": "Jean", "family_name": "Albany"}
    assert display_name_from_claims(claims) == "Jean Albany"


def test_accepts_a_single_part_name():
    """Plenty of people have one name; refusing it would send them to a form to
    retype what the provider already told us."""
    assert display_name_from_claims({"given_name": "Prince"}) == "Prince"
    assert display_name_from_claims({"family_name": "Curie"}) == "Curie"


def test_returns_none_when_there_is_nothing_usable():
    """None means "ask", and must be distinguishable from a name. A placeholder
    here would silently suppress the question forever."""
    assert display_name_from_claims({}) is None
    assert display_name_from_claims({"email": "ada@example.com"}) is None
    assert display_name_from_claims({"name": "   "}) is None
    assert display_name_from_claims({"name": ""}) is None


def test_survives_claims_that_are_not_a_dict_or_not_strings():
    """This runs on a path that must never fail an acceptance, so it is total."""
    assert display_name_from_claims(None) is None
    assert display_name_from_claims("not-a-dict") is None
    assert display_name_from_claims(["nope"]) is None
    assert display_name_from_claims({"name": 42}) is None
    assert display_name_from_claims({"given_name": None, "family_name": None}) is None


def test_truncates_rather_than_refusing_an_over_long_name():
    """display_name is capped at 255 by the API. Refusing would fail the write
    downstream; truncating keeps the person out of a form."""
    result = display_name_from_claims({"name": "Ada " * 200})
    assert result is not None
    assert len(result) <= 255
    assert result.startswith("Ada")
    assert not result.endswith(" ")


def test_truncation_prefers_a_word_boundary():
    claims = {"name": ("Jean " * 50) + ("x" * 10)}
    result = display_name_from_claims(claims)
    assert result is not None
    assert len(result) <= 255
    # Cut between words, not through one.
    assert not result.endswith("Jea")


# ---------------------------------------------------------------------------
# IDENTITY_KEY -- the failure this whole file exists to prevent
# ---------------------------------------------------------------------------


def test_the_profile_key_is_the_subject_the_profile_endpoints_read():
    """A structural guard, because getting this wrong is invisible at runtime.

    `_accept_invitation` must write the profile under the SUBJECT
    (authenticate_subject_and_name), not under the verified email
    (_check_invitation_identity) that it uses to match the invitation. Both are
    in scope in that function, they are both strings, and swapping them raises
    nothing -- it just stores the name where /api/me/profile never looks, so the
    console asks for it again at every login.
    """
    import inspect

    from core import admin_api

    source = inspect.getsource(admin_api._accept_invitation)
    head, _, tail = source.partition("authenticate_subject_and_name")
    assert tail, "acceptance no longer resolves the profile subject"

    # The upsert must be fed the subject returned by that resolver.
    assert "ok_subject, subject, token_name = await authenticate_subject_and_name(request)" in tail
    assert "upsert_user_profile(subject," in tail
    # ...and never the invitation-matching identity, whatever it is named there.
    upsert_call = tail.split("upsert_user_profile(", 1)[1].split(")", 1)[0]
    assert "identity" not in upsert_call, (
        "the profile is being written under the invitation identity (the verified "
        "email) instead of the token subject: /api/me/profile would never read it"
    )


def test_acceptance_never_overwrites_a_name_the_person_typed():
    """A self-chosen name outranks whatever the provider carries."""
    import inspect

    from core import admin_api

    source = inspect.getsource(admin_api._accept_invitation)
    assert "if existing:" in source
    # The provider name is only used in the else-branch.
    existing_branch = source.split("if existing:", 1)[1].split("elif token_name:", 1)[0]
    assert "upsert_user_profile" not in existing_branch


def test_a_profile_failure_cannot_change_the_acceptance_outcome():
    """Acceptance has already COMMITTED by then. The name is a nicety."""
    import inspect

    from core import admin_api

    source = inspect.getsource(admin_api._accept_invitation)
    block = source.split("profile_name: str | None = None", 1)[1].split("response =", 1)[0]
    assert "try:" in block and "except Exception" in block
    # No early return / raise inside the block: the response is built regardless.
    assert "return " not in block, "a profile failure must not short-circuit acceptance"


def test_needs_name_is_the_exact_negation_of_having_one():
    """The console asks if and only if this is true. Any drift between the two
    produces either a lost question or a question asked forever."""
    import inspect

    from core import admin_api

    source = inspect.getsource(admin_api._accept_invitation)
    assert '"profile": {"display_name": profile_name, "needs_name": not profile_name}' in source
