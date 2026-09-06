"""67-15d: a revoked browser session is refused at the NEXT call, not at expiry.

WHAT WAS BROKEN. The browser session is a Fernet-sealed ticket with no server
state: nothing between minting and reopening it consulted the database. Logout
only deleted the cookie, and removing or suspending a member cut their DB guards
while the ticket kept serving every read that does not touch `org_members` --
for the full 8-hour TTL. Named by
`reviews/audit-2026-08-17/12-acces-utilisateurs.md:286-290`.

WHY THESE TESTS GO THROUGH `get_browser_session` AND NOT THE STORE. Asserting
that a row lands in `app.revoked_browser_sessions` would prove the store works
and prove nothing about access, which is the only thing anybody cares about.
Every test here mints a REAL sealed ticket, puts it in a REAL cookie header, and
asks the seam the same question the next HTTP request would ask. The proof is
the same cookie answering differently before and after -- which is exactly what
"immediately" means, and what a TTL-bound design cannot do.

The model is `render_shares.resolve_session`, which revalidates the live Share on
every call for the same reason.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

from tests.core.test_org_enforcement import (
    _AUTH,
    _drop_org,
    _enrol,
    _member_mgmt_request,
    _new_org,
    pg_available,
)

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

_ISSUER = "https://issuer.example"


def _configure_oidc(monkeypatch) -> None:
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.setenv("TOOROW_BROWSER_AUTH_MODE", "oidc")
    monkeypatch.setenv("TOOROW_DEPLOYMENT_MODE", "self_hosted")
    monkeypatch.setenv("TOOROW_OIDC_ISSUER", _ISSUER)
    monkeypatch.setenv("TOOROW_OIDC_CLIENT_ID", "toorow-browser")
    monkeypatch.setenv(
        "TOOROW_OIDC_REDIRECT_URI",
        "http://localhost/api/auth/oidc/callback",
    )
    monkeypatch.setenv("TOOROW_OIDC_SESSION_SECRET", "s" * 32)
    monkeypatch.setenv("TOOROW_OIDC_COOKIE_SECURE", "0")
    monkeypatch.setenv("TOOROW_OIDC_PROVIDER_NAME", "Example SSO")
    monkeypatch.setenv("TOOROW_SESSION_REVOCATION_ENABLED", "1")


def _mint_ticket(subject: str, *, issued_at: int | None = None) -> tuple[str, str]:
    """Seal a ticket exactly as `oidc_callback` does. Returns (cookie, sid)."""
    import secrets  # noqa: PLC0415

    from core.browser_oidc import _load_oidc_settings, _seal  # noqa: PLC0415

    settings = _load_oidc_settings()
    now = issued_at if issued_at is not None else int(time.time())
    sid = secrets.token_urlsafe(24)
    ticket = _seal(
        settings,
        {
            "exp": now + settings.session_ttl_seconds,
            "iat": now,
            "sid": sid,
            "iss": settings.issuer,
            "sub": subject,
            "claims": {"email": subject, "email_verified": True},
        },
    )
    return f"toorow_browser_session={ticket}", sid


def _ask_the_seam(cookie: str):
    """Exactly what the next authenticated HTTP request would do."""
    from core.browser_oidc import get_browser_session  # noqa: PLC0415
    from starlette.requests import Request  # noqa: PLC0415

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/me/profile",
            "headers": [(b"cookie", cookie.encode())],
        }
    )
    return get_browser_session(request)


def _db_now():
    """The DATABASE's clock, which is the one that stamps `revoked_at`.

    Bracketing the gesture with `datetime.now()` would compare a Python clock to
    a Postgres one and fail on any skew -- proving the harness, not the product.
    """
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT NOW()")
            return cur.fetchone()[0]


def _attributions(subject: str) -> list[dict]:
    """What the store says about every cut made on this person, oldest first."""
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT scope, reason, revoked_by, revoked_at
                  FROM app.revoked_browser_sessions
                 WHERE subject = %s
                 ORDER BY revoked_at, id
                """,
                (subject,),
            )
            return [
                {"scope": r[0], "reason": r[1], "revoked_by": r[2], "revoked_at": r[3]}
                for r in cur.fetchall()
            ]


def _audit_rows(action: str, identity: str) -> list[dict]:
    """The audit ledger's own record of an act, by action and by actor."""
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT identity, metadata, created_at
                  FROM app.audit_log
                 WHERE action = %s AND identity = %s
                 ORDER BY created_at
                """,
                (action, identity),
            )
            return [
                {"identity": r[0], "metadata": r[1], "created_at": r[2]}
                for r in cur.fetchall()
            ]


def _forget_revocations(subject: str) -> None:
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app.revoked_browser_sessions WHERE subject = %s",
                (subject,),
            )
        conn.commit()


@pg_available
def test_a_live_session_passes(monkeypatch):
    """The control. Without it, a seam that refused EVERYTHING would look correct."""
    _configure_oidc(monkeypatch)
    subject = f"live-{uuid.uuid4().hex[:8]}@example.com"
    cookie, sid = _mint_ticket(subject)
    try:
        session = _ask_the_seam(cookie)
        assert session is not None, "an unrevoked ticket must still open the session"
        assert session.subject == subject
        assert session.session_id == sid, "the ticket must name itself, or nothing can cut it"
    finally:
        _forget_revocations(subject)


@pg_available
def test_a_revoked_session_is_refused_at_the_next_call(monkeypatch):
    """THE claim of 67-15d, and the one a TTL-bound design cannot make.

    The SAME cookie is presented twice with only a revocation in between. It
    opens the first time and is refused the second -- no expiry, no clock move,
    no new request shape.
    """
    from core.db import get_connection  # noqa: PLC0415
    from core.session_revocation import revoke_session  # noqa: PLC0415

    _configure_oidc(monkeypatch)
    subject = f"cut-{uuid.uuid4().hex[:8]}@example.com"
    cookie, sid = _mint_ticket(subject)
    try:
        assert _ask_the_seam(cookie) is not None, "precondition: the ticket opened"

        with get_connection() as conn:
            revoke_session(
                conn,
                session_id=sid,
                issuer=_ISSUER,
                subject=subject,
                revoked_by=subject,
                reason="logout",
            )
            conn.commit()

        assert _ask_the_seam(cookie) is None, (
            "the same cookie must be refused at the very next call -- a session "
            "that only dies at expiry is the defect this closes"
        )
    finally:
        _forget_revocations(subject)


@pg_available
def test_revoking_a_session_leaves_my_other_windows_open(monkeypatch):
    """Logout closes THE window it was clicked in, not every window I have."""
    from core.db import get_connection  # noqa: PLC0415
    from core.session_revocation import revoke_session  # noqa: PLC0415

    _configure_oidc(monkeypatch)
    subject = f"one-{uuid.uuid4().hex[:8]}@example.com"
    laptop, laptop_sid = _mint_ticket(subject)
    phone, _ = _mint_ticket(subject)
    try:
        with get_connection() as conn:
            revoke_session(
                conn,
                session_id=laptop_sid,
                issuer=_ISSUER,
                subject=subject,
                revoked_by=subject,
                reason="logout",
            )
            conn.commit()

        assert _ask_the_seam(laptop) is None
        assert _ask_the_seam(phone) is not None, (
            "a scope='session' revocation names ONE ticket; cutting every window "
            "on a logout click would be a different gesture"
        )
    finally:
        _forget_revocations(subject)


@pg_available
def test_signing_out_everywhere_is_a_bound_and_not_a_ban(monkeypatch):
    """A principal revocation cuts what was minted BEFORE it, and nothing after.

    That distinction is the whole reason the ticket carries `iat`: a person who
    signs out everywhere and then signs back in must be let through.
    """
    from core.db import get_connection  # noqa: PLC0415
    from core.session_revocation import ANY_ISSUER, revoke_principal  # noqa: PLC0415

    _configure_oidc(monkeypatch)
    subject = f"all-{uuid.uuid4().hex[:8]}@example.com"
    # Minted two minutes ago, so it is unambiguously before the bound.
    older, _ = _mint_ticket(subject, issued_at=int(time.time()) - 120)
    try:
        assert _ask_the_seam(older) is not None, "precondition: the old ticket opened"

        with get_connection() as conn:
            revoke_principal(
                conn,
                subject=subject,
                issuer=ANY_ISSUER,
                revoked_by=subject,
                reason="signed out everywhere",
            )
            conn.commit()

        assert _ask_the_seam(older) is None, "every ticket minted before the bound is cut"

        # Signing in again mints a ticket AFTER the bound.
        fresh, _ = _mint_ticket(subject, issued_at=int(time.time()) + 1)
        assert _ask_the_seam(fresh) is not None, (
            "revoking is not banning -- a later sign-in must work, or the person "
            "who signed out everywhere has locked themselves out for 8 hours"
        )
    finally:
        _forget_revocations(subject)


@pg_available
@pytest.mark.anyio
async def test_a_member_removed_from_the_org_loses_their_session(monkeypatch):
    """The chain report 12 said was broken: the role revokes, the session does not.

    Driven through the REAL route, not through the store, because the defect was
    never that revocation was impossible -- it was that removal did not call it.
    """
    from unittest.mock import patch  # noqa: PLC0415

    from core.org_members_api import _remove_org_member  # noqa: PLC0415

    _configure_oidc(monkeypatch)
    subject = f"exit-{uuid.uuid4().hex[:8]}@example.com"
    cookie, _ = _mint_ticket(subject)
    slug = f"rev-{uuid.uuid4().hex[:8]}"
    org_id = None
    try:
        assert _ask_the_seam(cookie) is not None, "precondition: the member's ticket opened"

        with patch(_AUTH, return_value=(True, "mgr@example.com")):
            org_id = await _new_org("REV", slug, "mgr@example.com")
            # SEEDED, not enrolled through the route: `POST .../members`
            # answers 409 `invitation_required` to every caller since
            # 2026-08-24, so this decor stopped existing and the three tests
            # died on their fixture rather than on their subject.
            _enrol(org_id, subject, "admin")
            removed = await _remove_org_member(_member_mgmt_request(org_id, subject))
        assert removed.status_code == 200, removed.body

        assert _ask_the_seam(cookie) is None, (
            "a person removed from the organization keeps browsing on the sealed "
            "ticket until it expires unless removal cuts the session too"
        )
    finally:
        _forget_revocations(subject)
        if org_id:
            _drop_org(org_id)


@pg_available
@pytest.mark.anyio
async def test_a_suspended_member_loses_their_session(monkeypatch):
    """Report 12 named suspension explicitly: it cut the guards, never the ticket."""
    from unittest.mock import patch  # noqa: PLC0415

    from core.org_members_api import (  # noqa: PLC0415
        _update_org_member,
    )

    _configure_oidc(monkeypatch)
    subject = f"susp-{uuid.uuid4().hex[:8]}@example.com"
    cookie, _ = _mint_ticket(subject)
    slug = f"sus-{uuid.uuid4().hex[:8]}"
    org_id = None
    try:
        assert _ask_the_seam(cookie) is not None, "precondition: the member's ticket opened"

        with patch(_AUTH, return_value=(True, "mgr@example.com")):
            org_id = await _new_org("SUS", slug, "mgr@example.com")
            # SEEDED, not enrolled through the route: `POST .../members`
            # answers 409 `invitation_required` to every caller since
            # 2026-08-24, so this decor stopped existing and the three tests
            # died on their fixture rather than on their subject.
            _enrol(org_id, subject, "admin")
            updated = await _update_org_member(
                _member_mgmt_request(org_id, subject, {"status": "suspended"})
            )
        assert updated.status_code == 200, updated.body

        assert _ask_the_seam(cookie) is None
    finally:
        _forget_revocations(subject)
        if org_id:
            _drop_org(org_id)


@pg_available
@pytest.mark.anyio
async def test_every_revocation_says_who_cut_it_and_when(monkeypatch):
    """`organization-settings.md:159`: "Every revocation is registered with the
    audit: who cut what, and when." Until now nothing read that back.

    The seven tests above assert who is REFUSED. That is a different claim: a
    write path could pass any `revoked_by` it liked -- or the column could be
    filled with the subject rather than the actor -- and every one of them would
    still be green. Attribution asserted only by the argument the caller passes
    is not attribution.

    So this drives the THREE ratified gestures through their own routes and reads
    the store afterwards:

      * signing out of this window     -> `browser_logout`, actor = the person;
      * signing out everywhere         -> `_revoke_my_sessions`, actor = the
        person's APPLICATION identity, which is deliberately not their OIDC
        subject here: a row that merely copied the subject would pass a weaker
        test and fail this one;
      * ending a membership            -> `_remove_org_member`, actor = the
        ADMINISTRATOR, never the person who was cut.

    And "when" is bracketed by the database's own clock around each gesture, so
    the instant is the instant of the act rather than a column that is merely
    not null.
    """
    from unittest.mock import patch  # noqa: PLC0415

    from core.browser_oidc import browser_logout  # noqa: PLC0415
    from core.me_api import _revoke_my_sessions  # noqa: PLC0415
    from core.org_members_api import _remove_org_member  # noqa: PLC0415
    from starlette.requests import Request  # noqa: PLC0415

    _configure_oidc(monkeypatch)
    subject = f"attr-{uuid.uuid4().hex[:8]}@example.com"
    # The application identity `_check_auth` resolves for this person. Distinct
    # from the OIDC subject on purpose -- see the docstring.
    person = f"person_{uuid.uuid4().hex[:8]}"
    manager = "mgr@example.com"
    slug = f"att-{uuid.uuid4().hex[:8]}"
    org_id = None

    def _request(path: str, cookie: str) -> Request:
        # `_session_origin_allowed` refuses a cookie-authorized mutation without
        # the exact public origin of the configured redirect URI -- and it is
        # enforced INSIDE `get_browser_session`, so a request without it does not
        # merely fail CSRF, it opens no session at all. A browser sends it; a
        # test that omitted it would silently exercise a different branch.
        headers = [(b"cookie", cookie.encode()), (b"origin", b"http://localhost")]
        return Request({"type": "http", "method": "POST", "path": path, "headers": headers})

    try:
        # --- Gesture one: this window, and only this window ------------------
        window, _ = _mint_ticket(subject)
        before_logout = _db_now()
        logout = await browser_logout(_request("/api/auth/logout", window))
        after_logout = _db_now()
        assert logout.status_code == 204, logout.body

        # --- Gesture two: every window, posted as a bound on the person ------
        # A fresh ticket: the one above is torn up, and a session the seam
        # refuses would send the route down its identity branch instead.
        everywhere, _ = _mint_ticket(subject)
        before_all = _db_now()
        with patch(_AUTH, return_value=(True, person)):
            revoked_all = await _revoke_my_sessions(
                _request("/api/me/sessions/revoke", everywhere)
            )
        after_all = _db_now()
        assert revoked_all.status_code == 200, revoked_all.body

        # --- Gesture three: an administrator ends the membership -------------
        before_removal = _db_now()
        with patch(_AUTH, return_value=(True, manager)):
            org_id = await _new_org("ATT", slug, manager)
            # SEEDED, not enrolled through the route: `POST .../members`
            # answers 409 `invitation_required` to every caller since
            # 2026-08-24, so this decor stopped existing and the three tests
            # died on their fixture rather than on their subject.
            _enrol(org_id, subject, "admin")
            removed = await _remove_org_member(_member_mgmt_request(org_id, subject))
        after_removal = _db_now()
        assert removed.status_code == 200, removed.body

        rows = _attributions(subject)
        by_reason = {row["reason"]: row for row in rows}
        assert set(by_reason) == {
            "logout",
            "signed out everywhere",
            "removed from organization",
        }, f"each gesture must leave its own trace, got {rows}"

        window_cut = by_reason["logout"]
        assert window_cut["scope"] == "session", "one window is one ticket"
        assert window_cut["revoked_by"] == subject, (
            "the person signed themselves out of this window; the store must "
            "name them and not the deployment"
        )
        assert before_logout <= window_cut["revoked_at"] <= after_logout

        all_cut = by_reason["signed out everywhere"]
        assert all_cut["scope"] == "principal", "a bound on the person, not a ticket"
        assert all_cut["revoked_by"] == person, (
            "the actor is the identity the request authenticated as -- a row "
            "carrying the OIDC subject instead would attribute the act to the "
            "ticket rather than to the person"
        )
        assert before_all <= all_cut["revoked_at"] <= after_all

        membership_cut = by_reason["removed from organization"]
        assert membership_cut["scope"] == "principal"
        assert membership_cut["revoked_by"] == manager, (
            "an administrator cut this session; recording the removed person as "
            "the actor would say they signed themselves out"
        )
        assert membership_cut["revoked_by"] != subject
        assert before_removal <= membership_cut["revoked_at"] <= after_removal

        # And the AUDIT LEDGER, which is the word the amendment uses. The store
        # answers "may this ticket still be used"; the ledger answers "what was
        # done, by whom" to someone who was never holding the ticket.
        from core.org_members_api import ACTION_ORG_MEMBER_REMOVED  # noqa: PLC0415
        from core.session_revocation import (  # noqa: PLC0415
            ACTION_SESSION_REVOKED,
            ACTION_SESSIONS_REVOKED_FOR_PERSON,
        )

        window_audit = _audit_rows(ACTION_SESSION_REVOKED, subject)
        assert len(window_audit) == 1, "one window cut, one line in the ledger"
        assert before_logout <= window_audit[0]["created_at"] <= after_logout

        all_audit = _audit_rows(ACTION_SESSIONS_REVOKED_FOR_PERSON, person)
        assert len(all_audit) == 1, (
            "signing out everywhere must be attributable to the person who asked "
            "for it, under their application identity"
        )
        assert before_all <= all_audit[0]["created_at"] <= after_all

        removal_audit = _audit_rows(ACTION_ORG_MEMBER_REMOVED, manager)
        assert [
            row for row in removal_audit if row["metadata"].get("member_identity") == subject
        ], "the removal is recorded against the administrator who made it"
        cut_counts = [
            row["metadata"].get("sessions_revoked")
            for row in removal_audit
            if row["metadata"].get("member_identity") == subject
        ]
        assert cut_counts == [1], (
            "the ledger records what was actually cut, not what was intended: a "
            "zero here would mean the membership ended and the session did not"
        )
    finally:
        _forget_revocations(subject)
        if org_id:
            _drop_org(org_id)


@pg_available
def test_the_seam_fails_closed_when_the_store_cannot_be_read(monkeypatch):
    """A database blip must not quietly restore the behaviour we just removed."""
    _configure_oidc(monkeypatch)
    subject = f"blip-{uuid.uuid4().hex[:8]}@example.com"
    cookie, _ = _mint_ticket(subject)

    def _explode():
        raise RuntimeError("store unreachable")

    monkeypatch.setattr("core.db.get_connection", lambda: _explode())
    assert _ask_the_seam(cookie) is None, (
        "an auth seam that failed OPEN would serve revoked tickets exactly when "
        "the operator most needs them cut"
    )


@pg_available
def test_a_ticket_that_cannot_be_named_cannot_be_honoured(monkeypatch):
    """A pre-67-15d ticket carries no `sid`, so no revocation could ever cut it."""
    from core.browser_oidc import _load_oidc_settings, _seal  # noqa: PLC0415

    _configure_oidc(monkeypatch)
    settings = _load_oidc_settings()
    now = int(time.time())
    legacy = _seal(
        settings,
        {
            "exp": now + settings.session_ttl_seconds,
            "iss": settings.issuer,
            "sub": "legacy@example.com",
            "claims": {"email": "legacy@example.com", "email_verified": True},
        },
    )
    assert _ask_the_seam(f"toorow_browser_session={legacy}") is None, (
        "an unnameable ticket is an uncuttable ticket -- it is refused so the "
        "person signs in again and gets one that can be revoked"
    )
