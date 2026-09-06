"""toorow -- super-admin allow-list (Story 34.3).

Deny-by-default gate for the ORG-PLAN control surface (the single admin->prod
write of Epic 34). The allow-list is an environment variable so it is set per
deployment and never hard-coded in the repo:

    TOOROW_SUPER_ADMINS="alice@toorow.io,bob@toorow.io"

Contract:
  * is_super_admin(email) -> bool -- the ALLOW-LIST primitive, nothing else.
  * super_admin_keys(identity) / identity_is_super_admin(identity) -- THE ONE
    server-side resolution of "who is a super-admin", used by every gate.
  * Empty / unset env var => NOBODY is a super-admin (deny-by-default).
  * Comparison is case-insensitive and whitespace-trimmed on both sides.
  * An empty / None email is never a super-admin.

The caller (org_plan_api) turns a non-super-admin into a 404 (not 403): we do
NOT reveal that the control surface exists to a caller who is not allow-listed.

WHY THERE IS A RESOLVER AND NOT JUST THE ALLOW-LIST (audit 12, P1-2,
2026-08-17). Three surfaces answered "who is a super-admin" three different
ways, and two of them were wrong for the same reason: the allow-list is keyed by
EMAIL, while an authenticated caller is always a ``person_<ULID>``
(``api_auth.authenticate_api_request``). Comparing that
person id against a list of emails is always false -- the POST /api/admin/org-plan
human path, the three connector MCP reads and the five platform-clock tools were
all gated by a check that could never pass. The third surface (``me_api``) closed
the gap by reading the SELF-DECLARED profile email, a field the person PATCHes
themselves: an authority signal must never read what its subject writes.

The single resolution is server-side and DB-backed: a person id is translated to
its VERIFIED email through ``app.person_identities`` -- the same registry
``org_members_api`` and ``org_entitlements`` already read -- and a legacy
identity, which IS an email, is tested as it stands. Callers therefore pass ONE
key (the person id) and never assemble their own.

ASCII-only strings (Windows/CI safe). No import side effects; the DB import is
lazy so this module stays importable everywhere it already was.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_ENV_VAR = "TOOROW_SUPER_ADMINS"


def _allow_list() -> frozenset[str]:
    """Parse TOOROW_SUPER_ADMINS into a normalized set of emails.

    Read live from the environment on every call so tests (and runtime plan
    changes) do not need a process restart. Deny-by-default: unset or blank
    => empty set.
    """
    raw = os.environ.get(_ENV_VAR, "") or ""
    return frozenset(
        part.strip().lower() for part in raw.split(",") if part.strip()
    )


def is_super_admin(email: str | None) -> bool:
    """Return True iff *email* is in the TOOROW_SUPER_ADMINS allow-list.

    Deny-by-default: returns False for None, empty string, or when the env var
    is unset/blank. Case-insensitive, whitespace-trimmed.
    """
    if not email:
        return False
    return email.strip().lower() in _allow_list()


def _verified_emails(identity: str, conn=None) -> frozenset[str]:
    """Every VERIFIED email `app.person_identities` holds for *identity*.

    Never widens on failure: a missing table, a closed connection or a read error
    yields the empty set, so the caller falls back to the raw identity alone and
    the gate stays as closed as it was. The warning is logged once per failure so
    a deployment whose registry is unreadable is not silently degraded.
    """
    sql = (
        "SELECT verified_email FROM app.person_identities "
        "WHERE person_id = %s AND verified_email IS NOT NULL"
    )

    def _read(active_conn) -> frozenset[str]:
        with active_conn.cursor() as cur:
            cur.execute(sql, (identity,))
            return frozenset(str(row[0]) for row in cur.fetchall() if row and row[0])

    try:
        if conn is not None:
            return _read(conn)
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as own_conn:
            return _read(own_conn)
    except Exception as exc:  # noqa: BLE001 -- never widen the allow-list on error.
        logger.warning(
            "super_admin: verified-email resolution unavailable (%s); "
            "falling back to the raw identity key",
            type(exc).__name__,
        )
        return frozenset()


def super_admin_keys(
    identity: str | None, *, conn=None, extra: object = ()
) -> frozenset[str]:
    """The allow-list keys ONE authenticated identity legitimately answers to.

    THE single resolution. Two keys coexist in this repository and both are
    resolved here so no caller has to know that they do:

      * the identity as it stands -- a legacy membership identity IS an email;
      * its VERIFIED email(s) from ``app.person_identities`` -- the only thing a
        canonical ``person_<ULID>`` can be matched against.

    ``extra`` carries keys the caller already holds from a stronger source than
    the database (the OAuth-verified email on the token, an org owner's stored
    verified email). Nothing self-declared may ever be passed here.
    """
    keys: set[str] = set()
    if identity:
        keys.add(str(identity))
    if isinstance(extra, str):
        extra = (extra,)
    for key in extra or ():
        if key:
            keys.add(str(key))
    if identity:
        keys |= _verified_emails(str(identity), conn)
    return frozenset(keys)


def identity_is_super_admin(
    identity: str | None, *, conn=None, extra: object = ()
) -> bool:
    """True iff *identity* resolves to an allow-listed super-admin.

    The one call every gate makes. Deny-by-default in every direction: no
    identity, no allow-list, or an unreadable registry all answer False.
    """
    return any(is_super_admin(key) for key in super_admin_keys(identity, conn=conn, extra=extra))
