"""Server-side revocation of the sealed browser session ticket.

THE DEFECT THIS CLOSES. The browser session is a Fernet-sealed ticket with no
server state at all: ``browser_oidc`` mints it at callback and reopens it on
every request, and nothing in between consults the database. Logout only called
``delete_cookie`` -- it asked the browser to forget the ticket, it did not make
the ticket invalid. A copy kept serving ``/api/me`` for the full 8-hour TTL, and
removing or suspending a member cut their DB guards but never their session.
Named by ``reviews/audit-2026-08-17/12-acces-utilisateurs.md:286-290``.

THE MODEL IS ``render_shares.resolve_session`` (``render_shares.py#resolve_session``),
which revalidates the LIVE Share on every call rather than only at exchange. Its
own comment states the rule this module obeys: a revocation that only closed the
door to new sessions would leave every already-minted one running until it
expired, which is not what "revocation ends access immediately" means to the
person who clicked revoke. Here too: a revoked session is refused at the NEXT
call, not at expiry.

WHERE THE CHECK LIVES, AND WHY THERE. ``browser_oidc.get_browser_session`` is
the single place the sealed ticket is opened and validated -- all four resolvers
in ``api_auth`` funnel through it, and so does ``GET /api/auth/session``. The
check is installed inside that function rather than in ``api_auth`` for exactly
that last caller: a check placed in ``api_auth`` would leave
``/api/auth/session`` answering ``authenticated: true`` to a person whose
session was just cut. One seam, five call sites, nothing duplicated per
endpoint.

TWO SCOPES:

``session``     one exact ticket, named by its ``sid``. The real logout: this
                window closes, my other ones stay open.

``principal``   every session of one person issued BEFORE ``revoked_at`` -- a
                "not before" bound, not a list. It is the only shape that works
                at org exit, because the server keeps no registry of minted
                tickets and therefore CANNOT enumerate them. Being a temporal
                bound, a later legitimate sign-in still passes: revoking is not
                banning.

HOT-PATH COST: one query per authenticated request, two index probes under a
BitmapOr, no row read in the nominal case. Both partial indexes are declared in
migration 280 for precisely this.

FAILING CLOSED. If the store cannot be read, the session is refused. The
canonical-identity path already requires the database for every authenticated
request, so this adds no new dependency -- and an auth seam that failed OPEN on
a database blip would serve revoked tickets exactly when the operator most needs
them cut.
"""

from __future__ import annotations

import logging
import os

from ulid import ULID

from core.audit import declare_action

logger = logging.getLogger(__name__)

# --- THE ACTIONS THIS MODULE WRITES --------------------------------------
#
# AD-42: declared next to the code that writes them, never in `core/audit.py`.
ACTION_SESSION_REVOKED = declare_action("browser_session.revoked")
ACTION_SESSIONS_REVOKED_FOR_PERSON = declare_action("browser_sessions.revoked_for_person")

#: Stored in `issuer` when the revoking gate does not know which provider signed
#: the ticket. The org-exit gate holds an application identity, not an issuer.
ANY_ISSUER = "*"

_REVOKED_PROBE = """
    SELECT 1
      FROM app.revoked_browser_sessions
     WHERE (session_id = %(session_id)s)
        OR (
            scope = 'principal'
            AND subject = %(subject)s
            AND (issuer = %(issuer)s OR issuer = %(any_issuer)s)
            AND revoked_at >= %(issued_at)s
        )
     LIMIT 1
"""


def _mint_id() -> str:
    return f"rvk_{ULID()}"


def revocation_enabled() -> bool:
    """Whether the seam consults the store.

    Default ON. The switch exists so a deployment whose migration 280 has not
    run yet fails to authenticate LOUDLY through configuration rather than
    silently through a missing relation, and so the pure-unit suites that build
    no database can keep exercising `get_browser_session`.
    """
    raw = os.environ.get("TOOROW_SESSION_REVOCATION_ENABLED", "1").strip().lower()
    return raw in {"1", "true", "yes"}


def is_session_revoked(
    conn,
    *,
    session_id: str,
    issuer: str,
    subject: str,
    issued_at: int,
) -> bool:
    """Answer, against LIVE state, whether this exact ticket may still be used.

    ``issued_at`` is the ticket's own ``iat``. It is what makes a `principal`
    revocation a bound rather than a ban: a ticket minted after the bound is
    untouched by it.
    """
    from datetime import datetime, timezone  # noqa: PLC0415

    with conn.cursor() as cur:
        cur.execute(
            _REVOKED_PROBE,
            {
                "session_id": session_id,
                "subject": subject,
                "issuer": issuer,
                "any_issuer": ANY_ISSUER,
                "issued_at": datetime.fromtimestamp(issued_at, tz=timezone.utc),
            },
        )
        return cur.fetchone() is not None


def revoke_session(
    conn,
    *,
    session_id: str,
    issuer: str,
    subject: str,
    revoked_by: str,
    reason: str,
) -> str:
    """Tear up ONE ticket. Idempotent: a second logout on the same ticket is a no-op."""
    revocation_id = _mint_id()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.revoked_browser_sessions
                (id, scope, session_id, issuer, subject, revoked_by, reason)
            VALUES (%s, 'session', %s, %s, %s, %s, %s)
            ON CONFLICT (session_id) WHERE session_id IS NOT NULL DO NOTHING
            """,
            (revocation_id, session_id, issuer, subject, revoked_by, reason),
        )
    return revocation_id


def revoke_principal(
    conn,
    *,
    subject: str,
    issuer: str,
    revoked_by: str,
    reason: str,
) -> str:
    """Cut every session of one ``(issuer, subject)`` minted before now."""
    revocation_id = _mint_id()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.revoked_browser_sessions
                (id, scope, session_id, issuer, subject, revoked_by, reason)
            VALUES (%s, 'principal', NULL, %s, %s, %s, %s)
            """,
            (revocation_id, issuer, subject, revoked_by, reason),
        )
    return revocation_id


def revoke_sessions_for_identity(
    conn,
    *,
    identity: str,
    revoked_by: str,
    reason: str,
) -> list[str]:
    """Cut every session of the person an org gate names, on BOTH identity keys.

    THIS IS ABOUT THE STORED ROWS, NOT ABOUT A MODE. Authentication resolves one
    key and only one -- the canonical ``person_id``. But
    ``app.org_members.identity`` is an OLD column: rows written before the
    canonical backfill still hold the raw OIDC ``sub``, and the gate reading a
    row cannot tell which of the two it is holding. Rather than guess, this
    resolves BOTH readings and revokes every subject either one yields:

      * every ``subject`` of ``person_identities`` whose ``person_id`` matches,
      * every ``subject`` of ``person_identities`` matching the string itself,
      * the string itself, which is what a pre-backfill row holds when no
        ``person_identities`` row was ever written for it.

    It stops being needed the day no un-backfilled row remains, and not before.

    Revoking a subject that does not exist costs one dead row and cuts nothing;
    failing to revoke the one that does is the defect this module closes.
    """
    subjects: set[str] = {identity.strip()} if identity and identity.strip() else set()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT subject
              FROM app.person_identities
             WHERE person_id = %(identity)s OR subject = %(identity)s
            """,
            {"identity": identity},
        )
        for row in cur.fetchall():
            if isinstance(row[0], str) and row[0].strip():
                subjects.add(row[0].strip())

    return [
        revoke_principal(
            conn,
            subject=subject,
            issuer=ANY_ISSUER,
            revoked_by=revoked_by,
            reason=reason,
        )
        for subject in sorted(subjects)
    ]


__all__ = [
    "ACTION_SESSIONS_REVOKED_FOR_PERSON",
    "ACTION_SESSION_REVOKED",
    "ANY_ISSUER",
    "is_session_revoked",
    "revocation_enabled",
    "revoke_principal",
    "revoke_session",
    "revoke_sessions_for_identity",
]
