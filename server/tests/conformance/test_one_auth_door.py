"""Every HTTP module learns who is asking through ONE door -- README "One authorization key".

2026-08-30: the anonymous-probe repair (6744599b) closed
`api_auth._authenticate_canonical_api_request` and left
`admin_api._check_canonical_principal` opening its own connection first, so the
entry, claim, invitation and project-settings routes kept the defect (review
finding F1). A second door is exactly what lets a class survive its own repair,
so this guard refuses one: the resolver that needs a connection is called by
`core.api_auth` and nowhere else, and no module of `server/core` opens a
connection to then ask for a principal.
"""

from __future__ import annotations

import re
from pathlib import Path

SERVER = Path(__file__).resolve().parents[2]
CORE = SERVER / "core"

#: The connection-bound resolvers: only `api_auth` may call them.
_BOUND_RESOLVERS = (
    "authenticate_canonical_principal(",
    "_resolve_browser_canonical_principal(",
    "bind_verified_credential(",
)
#: Every spelling of "who is calling?" a module may use -- the door and its
#: aliases. The second review (2026-08-30) showed the aliases are what the
#: entry, claim and project-settings modules actually write.
#:
#: `caller_identity` AND `_identity` JOINED THE LIST ON 2026-08-31. They are the
#: MCP side's spelling of the same question -- `core.mcp_scope.caller_identity`
#: is what `mcp_profiles._identity` delegates to -- and this scan could not see
#: them at all, so a surface that opened a connection and then asked who was
#: calling was invisible to this file whatever it did next.
_AUTH_CALLS = (
    "authenticate_canonical_principal|resolve_request_principal|authenticate_api_request"
    "|_check_canonical_principal|_check_auth|_check_invitation_principal"
    "|_check_invitation_identity|authenticate_invitation_identity"
    "|caller_identity|_identity"
)
#: THE ALTERNATION IS ANCHORED ON A NON-WORD BOUNDARY, and that is not tidying.
#: Matched as a bare substring, `_identity` fires inside `canonical_identity(`
#: and `revoke_sessions_for_identity(` -- four false offenders measured on
#: 2026-08-31, one of them `core/db.py:336`, the acquisition seam itself. A
#: guard that names the repair as the defect is one people learn to widen.
_GET_CONNECTION_THEN_AUTH = re.compile(
    r"with get_connection\(\)(?: as \w+)?[^\n]*:\s*\n(?:[^\n]*\n){0,8}[^\n]*"
    rf"(?<![\w.])(?:{_AUTH_CALLS})\(",
)


def _product_files() -> list[Path]:
    """Every product module under server/ -- not only core/*.py (review, 2026-08-30)."""
    files = []
    for root in (CORE, SERVER / "inbound", SERVER / "modules"):
        files += [
            p for p in root.rglob("*.py")
            if "tests" not in p.parts and "scratch_profiles" not in p.parts
        ]
    return sorted(files)


def test_only_api_auth_calls_a_connection_bound_resolver():
    offenders: list[str] = []
    for path in _product_files():
        if path.name == "api_auth.py":
            continue
        text = path.read_text(encoding="utf-8")
        for needle in _BOUND_RESOLVERS:
            for match in re.finditer(re.escape(needle), text):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"core/{path.name}:{line} calls {needle[:-1]}")
    assert not offenders, (
        "a module resolves the principal through its own connection -- walk "
        "`api_auth.resolve_request_principal` instead:\n  " + "\n  ".join(offenders)
    )


def test_the_guard_recognises_the_historical_defect():
    """The regex must fire on the exact shape of c0f04c70~1's second door."""
    sample = (
        "async def _check_canonical_principal(request):\n"
        "    with get_connection() as conn:\n"
        "        ok, principal = await authenticate_canonical_principal(request, conn)\n"
    )
    assert _GET_CONNECTION_THEN_AUTH.search(sample)
    alias = "    with get_connection() as conn, tracing():\n        x = 1\n        y = 2\n" \
            "        ok, p = await _check_canonical_principal(request)\n"
    assert _GET_CONNECTION_THEN_AUTH.search(alias)


def test_the_guard_reads_the_mcp_spelling_of_the_same_question():
    """`caller_identity()` is "who is calling?" too, and it was unreadable here.

    The shape below is the MCP one: a surface opens a connection and only then
    asks whom it is for. It is the mirror of the defect `mcp_profiles._attest`
    carried until 2026-08-31 -- there the identity was resolved FIRST and the
    connection opened bare, which is the half
    `test_mcp_surfaces_acquire_an_armed_connection.py` owns and now holds with
    no exemption at all.
    """
    mcp = (
        "def _grants():\n"
        "    with get_connection() as conn:\n"
        "        who = caller_identity()\n"
    )
    assert _GET_CONNECTION_THEN_AUTH.search(mcp)


def test_the_guard_does_not_fire_on_a_name_that_merely_ends_in_identity():
    """`canonical_identity(conn)` on the acquisition seam is not a second door."""
    innocent = (
        "    with get_connection() as conn:\n"
        "        install_access_context(conn, canonical_identity(identity, conn))\n"
    )
    assert not _GET_CONNECTION_THEN_AUTH.search(innocent)


def test_no_module_opens_a_connection_to_then_ask_who_is_calling():
    offenders: list[str] = []
    for path in _product_files():
        text = path.read_text(encoding="utf-8")
        for match in _GET_CONNECTION_THEN_AUTH.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            offenders.append(f"core/{path.name}:{line}")
    assert not offenders, (
        "a connection is opened BEFORE the request is authenticated -- the database is "
        "opened for a verified identity, never to look for one:\n  " + "\n  ".join(offenders)
    )
