"""A raw OIDC subject is nobody's identity -- and membership knows it.

WHAT THIS MODULE REPAIRS, measured on production 2026-08-12. MCP tools derive
their caller from `token.claims["sub"]`: a Google number, e.g.
`117505563874619937900`. Membership (`app.org_members.identity`) and the RLS floor
(`app.epic36_has_resource_access` -> `app.epic36_identity()`) both key on the
CANONICAL identity, `person_<ULID>`, introduced by epic 43 and already resolved by
the HTTP path (`api_auth` returns `principal.person_id`).

So the two halves of the product named the same human two ways, and only one of
those exists in the membership registry. Measured, same project, same instant,
through `resolve_strict_resource_access`:

    117505563874619937900              -> allowed=False  reason=not_found
    person_01KYHQX4RK24Z6QJCYWX6DYVSQ  -> allowed=True   reason=owner_floor

In other words: the organization's owner is a stranger on the MCP. `get_skills`
answered `project_not_found` for an active project, and `analyze_result`
`not_found` for a Result the HTTP API serves without blinking -- which puts the
whole Skill -> View -> render -> AI path assembly out of reach of its own owner.

THREE PROPERTIES, NONE OF THEM NEGOTIABLE.

1. **It translates, it never creates.** This module creates no person and writes
   nothing. An unknown subject comes back UNCHANGED, and the access decision that
   follows refuses exactly as it did before. Creating the person here would
   fabricate the very membership being checked.
2. **What is already canonical does not move.** A `person_…` identity passes
   without a read: the HTTP path, which already resolves, pays nothing and its
   behaviour does not change.
3. **An ambiguous match is not a match.** Two issuers may carry the same subject;
   two rows return the identity unchanged, hence refused. A bridge that guesses
   would open somebody else's project.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The prefix of canonical identities. An identity carrying it is already
#: resolved -- the one test that costs no read.
CANONICAL_PREFIX = "person_"

#: The bridge maps an APPEND-ONLY registry: a `person_identities` row is never
#: reassigned. The cache is therefore safe, and it exists because this module is
#: called on every MCP tool call. It is bounded so a flood of unknown subjects
#: cannot grow it without end.
_CACHE: dict[str, str] = {}
_CACHE_LIMIT = 512


def _remember(subject: str, person_id: str) -> None:
    if len(_CACHE) >= _CACHE_LIMIT:
        _CACHE.clear()
    _CACHE[subject] = person_id


def reset_cache() -> None:
    """Empty the bridge -- for suites, never for production."""
    _CACHE.clear()


def canonical_identity(identity: str, conn: Any) -> str:
    """The canonical identity of *identity*, or *identity* unchanged.

    `conn` is an open connection the caller owns. The read happens BEFORE the RLS
    floor is armed on that connection: `person_identities` is the registry that
    says WHO the caller is, and arming first would ask a caller to prove its
    identity for the right to resolve it.
    """
    resolved = (identity or "").strip()
    if not resolved or resolved == "anonymous" or resolved.startswith(CANONICAL_PREFIX):
        return resolved
    cached = _CACHE.get(resolved)
    if cached is not None:
        return cached
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT person_id FROM app.person_identities
                WHERE subject = %s
                LIMIT 2
                """,
                (resolved,),
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 -- a mute bridge refuses, it never opens
        logger.warning("identity_bridge: unresolved subject (%s)", type(exc).__name__)
        return resolved
    if len(rows) != 1 or not isinstance(rows[0][0], str):
        return resolved
    person_id = str(rows[0][0])
    _remember(resolved, person_id)
    return person_id


def canonical_identity_standalone(identity: str) -> str:
    """Like `canonical_identity`, opening its own short-lived connection.

    For callers that must know the identity BEFORE opening the request
    connection -- typically the MCP scope gate, which hands the same identity to
    the acquisition AND to the access decision.
    """
    resolved = (identity or "").strip()
    if not resolved or resolved == "anonymous" or resolved.startswith(CANONICAL_PREFIX):
        return resolved
    cached = _CACHE.get(resolved)
    if cached is not None:
        return cached
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            return canonical_identity(resolved, conn)
    except Exception as exc:  # noqa: BLE001
        logger.warning("identity_bridge: no connection to resolve (%s)", type(exc).__name__)
        return resolved
