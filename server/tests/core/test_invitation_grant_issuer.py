"""A grant records who GAVE the access, not who received it.

Found by reading `server/core/invitations.py`, not by running it.

`039_resource_grants.sql:60` documents `granted_by` as the granting identity.
The acceptance path passed `membership_identity` — the invitee — so every grant
created by accepting an invitation named the person who RECEIVED the access as
the person who gave it. "Who gave this access" is the whole question an access
audit asks, and nothing rewrites `granted_by` afterwards: a wrong value is
permanent.

The issuer was already at hand and discarded: `:573` selects `i.issuer` into the
row and `row[14]` is read nowhere in the file. The sibling `org_members` INSERT
reads it straight from `app.invitations` in its own statement; the grant INSERT
now does the same, so the two cannot drift apart.

These are source-level assertions on purpose. The behaviour they pin lives in one
SQL statement, and the layer that would execute it skips itself without
`TEST_POSTGRES_DSN` — which is exactly how it stayed wrong. A test that only runs
with a database would not have caught it either.
"""

from __future__ import annotations

import inspect
import re

from core import invitations


def _accept_source() -> str:
    return inspect.getsource(invitations)


def _grant_insert() -> str:
    """The INSERT into `app.resource_grants`, with the values it supplies."""
    source = _accept_source()
    start = source.index("INSERT INTO app.resource_grants")
    # Up to the end of the `cur.execute(...)` call that carries it.
    return source[start : source.index("cur.execute", start + 1)]


def test_the_grant_takes_its_granter_from_the_invitation_not_the_invitee():
    block = _grant_insert()
    # The issuer comes from the row being accepted, in the same statement.
    assert "issuer" in block
    assert "FROM app.invitations WHERE id = %s" in block


def test_the_invitee_is_no_longer_named_as_their_own_granter():
    block = _grant_insert()
    # `membership_identity` still appears once — as the grant's SUBJECT, which is
    # correct. It must not appear a second time as the granter.
    assert block.count("membership_identity") == 1, (
        "the invitee is supplied twice: once as `identity` (right) and once as "
        "`granted_by` (wrong — that is the defect this test exists for)"
    )


def test_membership_and_grant_agree_on_where_the_issuer_comes_from():
    # Both INSERTs read the issuer from `app.invitations` rather than from a
    # parameter, so no caller can supply a granter the invitation never named.
    source = _accept_source()
    members = source[source.index("INSERT INTO app.org_members") :][:600]
    assert "issuer" in members
    assert re.search(r"FROM app\.invitations WHERE id = %s", members)


def test_the_role_is_read_from_the_invitation_and_never_from_the_caller():
    # The property the whole flow rests on, pinned while we are here: an accepted
    # invitation grants `invited_role` as stored, so a client cannot ask for one.
    source = _accept_source()
    members = source[source.index("INSERT INTO app.org_members") :][:600]
    assert "invited_role" in members
