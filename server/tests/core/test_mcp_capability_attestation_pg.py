"""67-16: an MCP host's grants are ATTESTED against the live row, and can be CUT.

WHAT WAS BROKEN. `mcp_profiles.visible_profiles` decided Operations/Governance/
Support from `enabled_profiles`/`endpoint_binding`/`workspace_evidence_hash` read
out of the caller's token claims and only shape-checked. The row those fields
belong to, `app.mcp_capability_contexts`, was written once by
`host_preflight.bind_host_connection` and read by no middleware at all -- and it
had no revocation, no update, no list. So the surface had exactly two states: the
`TOOROW_MCP_HIGHRISK_ENABLED` flag shut, and every governed MCP write unreachable
on every honest deployment; or the flag open, and every host claim believed.
Named by `reviews/audit-2026-08-17/10-mcp-app.md:30,57,84,100`.

WHY THESE TESTS GO THROUGH THE MIDDLEWARE AND NOT THE STORE. Asserting that
`revoked_at` lands in a column would prove the column works and prove nothing
about access. Every test below asks the middleware the same question the next
`tools/call` would ask, with the SAME token, before and after the cut. The proof
is one token answering differently -- which is what "immediately" means, and what
a claim minted at issue time cannot do.

The model is `session_revocation` (67-15d), which took it from
`render_shares.resolve_session`: the LIVE state is revalidated at every call.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

_IDENTITY = "agent@example.com"
_ENDPOINT = "https://mcp.example.com/admin"
_EVIDENCE = "a" * 64
_PRESENCE = "b" * 64


# ---------------------------------------------------------------------------
# Fixtures -- a real org and a real bound context, rolled back at teardown.
# ---------------------------------------------------------------------------


@pytest.fixture
def org_id(live_postgres):
    ident = f"org_att{uuid.uuid4().hex[:8]}"
    with live_postgres.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) "
            "VALUES (%s, %s, %s, 'system')",
            (ident, ident, f"{ident}-slug"),
        )
        cur.execute(
            "INSERT INTO app.org_members (id, org_id, identity, role, status, joined_at) "
            "VALUES (%s, %s, %s, 'owner', 'active', NOW())",
            (f"omem_{uuid.uuid4().hex[:8]}", ident, _IDENTITY),
        )
    return ident


def _bind_context(conn, *, org_id: str, profiles: list[str], presence: str | None = _PRESENCE):
    """Write the row `bind_host_connection` writes, without the preflight ceremony."""
    from core.mcp_attestation import mint_context_id

    context_id = mint_context_id()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.mcp_capability_contexts "
            "(id,org_id,host,workspace_id,workspace_type,client_id,endpoint_binding,"
            "enabled_profiles,workspace_evidence_hash,"
            "interactive_presence_evidence_hash,policy_version,catalog_version) "
            "VALUES (%s,%s,'opaque-host','ws-1','team','client-1',%s,%s::jsonb,%s,%s,'p1','c1')",
            (context_id, org_id, _ENDPOINT, json.dumps(profiles), _EVIDENCE, presence),
        )
    return context_id


@contextlib.contextmanager
def _lend(conn):
    """Hand the live test connection to code that opens its own."""
    yield conn


def _speak_as(monkeypatch, conn, claim: dict | None):
    """Make the MCP seam see a token carrying *claim* and this test's database."""
    import core.db
    import fastmcp.server.dependencies as deps
    from core import mcp_profiles

    monkeypatch.setattr(core.db, "get_connection", lambda *a, **k: _lend(conn))
    #  `request_connection` EST LENDU AUSSI, depuis le 2026-08-31, et pas pour
    #  contourner l'armement : `install_access_context` COMMITTE la session par
    #  conception (`core/db.py`, "committed immediately [...] survives a later
    #  ROLLBACK"). Sur la connexion du fixture, ce commit valide les lignes que le
    #  fixture venait d'ecrire, donc le rollback du teardown ne les reprend pas --
    #  mesure du 2026-08-31 : 9 `app.mcp_capability_contexts` survivaient a un
    #  run, et le run suivant lisait "cet endpoint resout plusieurs contextes".
    #  Ce que la couture ACQUIERT vraiment est tenu ailleurs, par un balayage a
    #  egalite stricte : `tests/conformance/test_mcp_surfaces_acquire_an_armed_
    #  connection.py`, ou `_attest` n'a plus d'exemption du tout.
    monkeypatch.setattr(core.db, "request_connection", lambda *a, **k: _lend(conn))
    monkeypatch.setattr(mcp_profiles, "_identity", lambda: _IDENTITY)
    token = None if claim is None else SimpleNamespace(claims={"capability_context": claim})
    monkeypatch.setattr(deps, "get_access_token", lambda: token)


def _register_operations_tool(mcp_profiles, mcp):
    mcp_profiles.reset_registry_for_tests()

    def handler():  # pragma: no cover -- never invoked; the guard denies first.
        return "ok"

    handler.__name__ = "retry_pull"
    mcp_profiles.register_profiled(
        mcp,
        handler,
        profile="operations",
        effect="prepare",
        data_class="operational",
        confirmation_mode="server",
    )


# ---------------------------------------------------------------------------
# (a) THE GUARD ITSELF -- a well-formed claim is not a grant.
# ---------------------------------------------------------------------------


def test_a_self_reported_claim_never_unlocks_a_high_risk_profile():
    """RED before 67-16: with TOOROW_MCP_HIGHRISK_ENABLED=1 this returned operations.

    Nothing here touches the database, and that is the point: these grants have
    the exact shape `visible_profiles` used to accept -- authenticated identity,
    non-empty endpoint, 64-hex evidence, operations opted in. What they do not
    have is `attested_context_id`, which only `_capability_context` can produce by
    reading a live row. The forged claim now buys Insights and nothing else.
    """
    from core import mcp_profiles

    forged = {
        "enabled_profiles": ["insights", "operations", "governance"],
        "endpoint_binding": _ENDPOINT,
        "workspace_evidence_hash": _EVIDENCE,
        "interactive_presence_evidence_hash": _PRESENCE,
    }
    assert mcp_profiles.visible_profiles(_IDENTITY, {"host": "h"}, forged) == frozenset(
        {"insights"}
    )
    assert mcp_profiles.interactive_presence_verified(forged) is False


def test_the_retired_deployment_flag_cannot_reopen_the_door(monkeypatch):
    """The env escape hatch is gone; setting it changes nothing at all."""
    from core import mcp_profiles

    monkeypatch.setenv("TOOROW_MCP_HIGHRISK_ENABLED", "1")
    assert not hasattr(mcp_profiles, "high_risk_profiles_enabled")
    forged = {
        "enabled_profiles": ["operations"],
        "endpoint_binding": _ENDPOINT,
        "workspace_evidence_hash": _EVIDENCE,
    }
    assert mcp_profiles.visible_profiles(_IDENTITY, {}, forged) == frozenset({"insights"})


def test_the_attestation_opens_the_database_for_the_resolved_caller(monkeypatch):
    """The DB opens for a VERIFIED identity -- README, "One authorization key".

    THE DEFECT THIS CLOSES, measured 2026-08-31. `_capability_context` resolved
    the caller through `mcp_scope.caller_identity` and then `_attest` read
    `app.mcp_capability_contexts` on a BARE `get_connection()`. The exemption
    that covered it in `test_mcp_surfaces_acquire_an_armed_connection.py` said
    the site ran "before any caller identity has been resolved", which was false
    as written: the identity exists one statement earlier. The row's Epic-36
    policy is `epic36_is_org_member(org_id)` (migration 273), so arming is a
    second barrier and not only a tidier acquisition -- a pointer naming another
    organization's context now attests to nothing.
    """
    import core.db
    import fastmcp.server.dependencies as deps
    from core import mcp_profiles

    armed_for: list[str] = []

    @contextlib.contextmanager
    def _record(identity):
        armed_for.append(identity)
        raise RuntimeError("no database in this test -- the identity is the assertion")
        yield  # pragma: no cover

    monkeypatch.setattr(core.db, "request_connection", _record)
    monkeypatch.setattr(
        core.db,
        "get_connection",
        lambda *a, **k: pytest.fail("`_attest` acquired an UNARMED connection"),
    )
    monkeypatch.setattr(mcp_profiles, "_identity", lambda: _IDENTITY)
    monkeypatch.setattr(
        deps,
        "get_access_token",
        lambda: SimpleNamespace(
            claims={"capability_context": {"capability_context_id": "mcpctx_whatever"}}
        ),
    )

    identity, host_context, grants = mcp_profiles._capability_context()

    assert armed_for == [_IDENTITY]
    #  Et l'echec de la lecture reste FERME : pas de grants, donc Insights seul.
    assert (identity, host_context, grants) == (_IDENTITY, {}, {})


# ---------------------------------------------------------------------------
# (b) ATTESTATION -- the live row is what grants, and it grants exactly itself.
# ---------------------------------------------------------------------------


def test_the_live_row_is_what_grants_the_profile(live_postgres, org_id, monkeypatch):
    from core import mcp_profiles

    context_id = _bind_context(live_postgres, org_id=org_id, profiles=["insights", "operations"])
    _speak_as(monkeypatch, live_postgres, {"capability_context_id": context_id})

    identity, host_context, grants = mcp_profiles._capability_context()
    assert identity == _IDENTITY
    # The host context comes from the ROW, not from anything the host said.
    assert host_context == {
        "host": "opaque-host",
        "workspace_id": "ws-1",
        "workspace_type": "team",
        "client_id": "client-1",
    }
    assert grants["attested_context_id"] == context_id
    assert grants["enabled_profiles"] == ["insights", "operations"]
    visible = mcp_profiles.visible_profiles(identity, host_context, grants)
    assert visible == frozenset({"insights", "operations"})
    # governance was never in the row, so no claim can add it.
    assert "governance" not in visible


def test_a_claim_cannot_widen_the_row_it_points_at(live_postgres, org_id, monkeypatch):
    """The pointer names a row; the row names the profiles. Extra claims are inert."""
    from core import mcp_profiles

    context_id = _bind_context(live_postgres, org_id=org_id, profiles=["insights"])
    _speak_as(
        monkeypatch,
        live_postgres,
        {
            "capability_context_id": context_id,
            "enabled_profiles": ["insights", "operations", "governance", "support"],
            "workspace_evidence_hash": _EVIDENCE,
        },
    )
    identity, host_context, grants = mcp_profiles._capability_context()
    assert grants["enabled_profiles"] == ["insights"]
    assert mcp_profiles.visible_profiles(identity, host_context, grants) == frozenset({"insights"})


def test_a_pointer_at_no_row_is_insights_only(live_postgres, org_id, monkeypatch):
    from core import mcp_profiles

    _bind_context(live_postgres, org_id=org_id, profiles=["insights", "operations"])
    _speak_as(monkeypatch, live_postgres, {"capability_context_id": "mcpctx_DOESNOTEXIST"})
    identity, host_context, grants = mcp_profiles._capability_context()
    assert grants == {}
    assert mcp_profiles.visible_profiles(identity, host_context, grants) == frozenset({"insights"})


def test_an_ambiguous_endpoint_refuses_to_choose(live_postgres, org_id, monkeypatch):
    """Two live rows on one endpoint is an ambiguity, not a coin flip."""
    from core import mcp_profiles
    from core.mcp_attestation import attest_capability_context

    _bind_context(live_postgres, org_id=org_id, profiles=["insights", "operations"])
    _bind_context(live_postgres, org_id=org_id, profiles=["insights", "governance"])
    assert attest_capability_context(live_postgres, endpoint_binding=_ENDPOINT) is None

    _speak_as(monkeypatch, live_postgres, {"endpoint_binding": _ENDPOINT})
    identity, host_context, grants = mcp_profiles._capability_context()
    assert mcp_profiles.visible_profiles(identity, host_context, grants) == frozenset({"insights"})


def test_the_endpoint_alone_attests_when_it_is_unambiguous(live_postgres, org_id, monkeypatch):
    from core import mcp_profiles

    context_id = _bind_context(live_postgres, org_id=org_id, profiles=["insights", "support"])
    _speak_as(monkeypatch, live_postgres, {"endpoint_binding": _ENDPOINT})
    identity, host_context, grants = mcp_profiles._capability_context()
    assert grants["attested_context_id"] == context_id
    assert mcp_profiles.visible_profiles(identity, host_context, grants) == frozenset(
        {"insights", "support"}
    )


def test_interactive_presence_comes_from_the_row_too(live_postgres, org_id, monkeypatch):
    """AD-27: a `confirmed_write` needs presence, and presence is now a column."""
    from core import mcp_profiles

    with_presence = _bind_context(live_postgres, org_id=org_id, profiles=["insights", "operations"])
    _speak_as(monkeypatch, live_postgres, {"capability_context_id": with_presence})
    _, _, grants = mcp_profiles._capability_context()
    assert mcp_profiles.interactive_presence_verified(grants) is True

    without = _bind_context(
        live_postgres, org_id=org_id, profiles=["insights", "operations"], presence=None
    )
    _speak_as(monkeypatch, live_postgres, {"capability_context_id": without})
    _, _, grants = mcp_profiles._capability_context()
    assert mcp_profiles.interactive_presence_verified(grants) is False


# ---------------------------------------------------------------------------
# (c) REVOCATION -- the same token, answering differently at the next call.
# ---------------------------------------------------------------------------


def test_revoking_cuts_the_capability_at_the_very_next_call(live_postgres, org_id, monkeypatch):
    """THE test. One token, one tool, one middleware -- before and after the cut.

    Audit open question n°3, answered: revocation cuts the sessions already
    running, because the living state is revalidated at the call. The session is
    not torn down (the MCP wire has no such verb); it drops to the Insights floor
    and every high-risk tool vanishes from discovery AND invocation.
    """
    from core import mcp_profiles
    from core.mcp_attestation import revoke_capability_context

    mcp = MagicMock()
    _register_operations_tool(mcp_profiles, mcp)
    middleware = mcp_profiles.build_middleware()
    context_id = _bind_context(live_postgres, org_id=org_id, profiles=["insights", "operations"])
    _speak_as(monkeypatch, live_postgres, {"capability_context_id": context_id})

    call = SimpleNamespace(message=SimpleNamespace(name="retry_pull"))

    async def call_next(_context):
        return []

    # BEFORE: discovery lists it and the call goes through.
    listed = asyncio.run(middleware.on_list_tools(SimpleNamespace(), _listing()))
    assert [tool.name for tool in listed] == ["retry_pull"]
    assert asyncio.run(middleware.on_call_tool(call, _returning("ok"))) == "ok"

    revoke_capability_context(
        live_postgres,
        context_id=context_id,
        reason="host decommissioned",
        actor="owner@example.com",
        idempotency_key=f"idem-{uuid.uuid4().hex}",
        host_context={},
    )

    # AFTER: same token, same tool. Gone from discovery, denied at the call.
    listed = asyncio.run(middleware.on_list_tools(SimpleNamespace(), _listing()))
    assert [tool.name for tool in listed] == []
    denied = MagicMock()
    with pytest.raises(Exception) as exc:
        asyncio.run(middleware.on_call_tool(call, denied))
    assert "Tool not found." in str(exc.value)
    denied.assert_not_called()
    assert asyncio.run(middleware.on_list_tools(SimpleNamespace(), call_next)) == []


def test_a_revoked_context_is_still_listed_with_its_cut(live_postgres, org_id):
    """A lifecycle list that hides what was cut cannot evidence a revocation."""
    from core.mcp_attestation import list_capability_contexts, revoke_capability_context

    context_id = _bind_context(live_postgres, org_id=org_id, profiles=["insights", "operations"])
    revoke_capability_context(
        live_postgres,
        context_id=context_id,
        reason="rotated",
        actor="owner@example.com",
        idempotency_key=f"idem-{uuid.uuid4().hex}",
        host_context={},
    )
    rows = list_capability_contexts(live_postgres, org_id=org_id)
    assert [row["id"] for row in rows] == [context_id]
    assert rows[0]["revoked_at"] is not None
    assert rows[0]["revoked_by"] == "owner@example.com"
    assert rows[0]["revocation_reason"] == "rotated"
    live_only = list_capability_contexts(live_postgres, org_id=org_id, include_revoked=False)
    assert live_only == []


def test_revoking_twice_does_not_move_the_cut(live_postgres, org_id):
    from core.mcp_attestation import read_capability_context, revoke_capability_context

    context_id = _bind_context(live_postgres, org_id=org_id, profiles=["insights"])
    first = revoke_capability_context(
        live_postgres,
        context_id=context_id,
        reason="first",
        actor="owner@example.com",
        idempotency_key=f"idem-{uuid.uuid4().hex}",
        host_context={},
    )
    revoke_capability_context(
        live_postgres,
        context_id=context_id,
        reason="second",
        actor="someone.else@example.com",
        idempotency_key=f"idem-{uuid.uuid4().hex}",
        host_context={},
    )
    after = read_capability_context(live_postgres, context_id=context_id)
    assert after["revoked_at"] == first["revoked_at"]
    assert after["revoked_by"] == "owner@example.com"
    assert after["revocation_reason"] == "first"


def test_a_cut_context_cannot_be_un_revoked(live_postgres, org_id):
    """Migration 283's trigger: a revocation is not something you take back."""
    import psycopg
    from core.mcp_attestation import revoke_capability_context

    context_id = _bind_context(live_postgres, org_id=org_id, profiles=["insights"])
    revoke_capability_context(
        live_postgres,
        context_id=context_id,
        reason="cut",
        actor="owner@example.com",
        idempotency_key=f"idem-{uuid.uuid4().hex}",
        host_context={},
    )
    with live_postgres.cursor() as cur:
        cur.execute("SAVEPOINT before_unrevoke")
        with pytest.raises(psycopg.errors.RaiseException) as exc:
            cur.execute(
                "UPDATE app.mcp_capability_contexts SET revoked_at=NULL WHERE id=%s",
                (context_id,),
            )
        assert "cannot be un-revoked" in str(exc.value)
        cur.execute("ROLLBACK TO SAVEPOINT before_unrevoke")


def test_revoking_writes_an_audit_row(live_postgres, org_id):
    """`execute_operation` is the seam, so the cut and its audit share a transaction."""
    from core.mcp_attestation import revoke_capability_context

    context_id = _bind_context(live_postgres, org_id=org_id, profiles=["insights", "governance"])
    revoke_capability_context(
        live_postgres,
        context_id=context_id,
        reason="policy change",
        actor="owner@example.com",
        idempotency_key=f"idem-{uuid.uuid4().hex}",
        host_context={},
    )
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT command_type,actor FROM app.operations "
            "WHERE command_type='mcp.capability_context.revoke' AND effective_org_id=%s",
            (org_id,),
        )
        rows = cur.fetchall()
    assert rows and rows[0][1] == "owner@example.com"


# ---------------------------------------------------------------------------
# Small async helpers -- FastMCP hands the middleware a `call_next` coroutine.
# ---------------------------------------------------------------------------


def _listing():
    from core import mcp_profiles

    tool = SimpleNamespace(
        name="retry_pull",
        meta={"profile": "operations", "effect": "prepare"},
        tags=set(),
    )
    assert mcp_profiles._tool_profile(tool) == "operations"

    async def call_next(_context):
        return [tool]

    return call_next


def _returning(value):
    async def call_next(_context):
        return value

    return call_next


# ---------------------------------------------------------------------------
# (e) 2026-09-04 -- the pointer is DERIVED from what Google signed.
# ---------------------------------------------------------------------------

_AUD = "989690374424-example.apps.googleusercontent.com"
_SUB = "117505563874619937900"


def _bind_for_principal(conn, *, org_id: str, profiles: list[str], presence=_PRESENCE):
    """The row the console (or a host administrator) binds for ONE principal on
    ONE endpoint: `endpoint_binding` = the token audience, `client_id` = its subject."""
    from core.mcp_attestation import mint_context_id

    context_id = mint_context_id()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.mcp_capability_contexts "
            "(id,org_id,host,workspace_id,workspace_type,client_id,endpoint_binding,"
            "enabled_profiles,workspace_evidence_hash,"
            "interactive_presence_evidence_hash,policy_version,catalog_version) "
            "VALUES (%s,%s,'toorow-e2e-harness','console','team',%s,%s,%s::jsonb,%s,%s,'p1','c1')",
            (context_id, org_id, _SUB, _AUD, json.dumps(profiles), _EVIDENCE, presence),
        )
    return context_id


def _speak_with_google_claims(monkeypatch, conn, claims: dict):
    """A verified Google ID token: `aud`, `sub`, `email` -- and NO pointer claim."""
    import core.db
    import fastmcp.server.dependencies as deps
    from core import mcp_profiles

    monkeypatch.setattr(core.db, "get_connection", lambda *a, **k: _lend(conn))
    monkeypatch.setattr(core.db, "request_connection", lambda *a, **k: _lend(conn))
    monkeypatch.setattr(mcp_profiles, "_identity", lambda: _IDENTITY)
    monkeypatch.setattr(deps, "get_access_token", lambda: SimpleNamespace(claims=claims))


def test_a_google_token_without_a_pointer_is_attested_by_its_audience_and_subject(
    live_postgres, org_id, monkeypatch
):
    """RED before 2026-09-04: no claim, no row read, Insights only -- for every host.

    The token below is the shape production verifies (issuer accounts.google.com):
    it carries what Google signed and nothing toorow minted. One live row keyed by
    its audience and subject is what makes `operations` visible; the grants are
    the row's, and the presence evidence is the row's.
    """
    from core import mcp_profiles

    context_id = _bind_for_principal(
        live_postgres, org_id=org_id, profiles=["insights", "operations"]
    )
    _speak_with_google_claims(
        monkeypatch,
        live_postgres,
        {"iss": "https://accounts.google.com", "aud": _AUD, "sub": _SUB, "email": _IDENTITY},
    )
    identity, host_context, grants = mcp_profiles._capability_context()
    assert identity == _IDENTITY
    assert grants["attested_context_id"] == context_id
    assert grants["org_id"] == org_id
    assert host_context["client_id"] == _SUB
    assert mcp_profiles.visible_profiles(identity, host_context, grants) >= {
        "insights",
        "operations",
    }
    assert mcp_profiles.interactive_presence_verified(grants) is True


def test_two_live_bindings_for_one_principal_on_one_endpoint_resolve_nothing(
    live_postgres, org_id, monkeypatch
):
    """An ambiguity is refused, never resolved by choosing -- the endpoint-only rule, kept."""
    from core import mcp_profiles

    _bind_for_principal(live_postgres, org_id=org_id, profiles=["insights", "operations"])
    _bind_for_principal(live_postgres, org_id=org_id, profiles=["insights", "governance"])
    _speak_with_google_claims(monkeypatch, live_postgres, {"aud": _AUD, "sub": _SUB})
    identity, host_context, grants = mcp_profiles._capability_context()
    assert (host_context, grants) == ({}, {})
    assert mcp_profiles.visible_profiles(identity, host_context, grants) == frozenset({"insights"})


def test_a_revoked_binding_stops_attesting_a_google_token_at_the_next_call(
    live_postgres, org_id, monkeypatch
):
    from core import mcp_profiles

    context_id = _bind_for_principal(
        live_postgres, org_id=org_id, profiles=["insights", "operations"]
    )
    _speak_with_google_claims(monkeypatch, live_postgres, {"aud": _AUD, "sub": _SUB})
    assert mcp_profiles._capability_context()[2]["attested_context_id"] == context_id
    with live_postgres.cursor() as cur:
        cur.execute(
            "UPDATE app.mcp_capability_contexts SET revoked_at=NOW(), revoked_by=%s, "
            "revocation_reason='test' WHERE id=%s",
            (_IDENTITY, context_id),
        )
    assert mcp_profiles._capability_context()[2] == {}


def test_a_list_audience_with_several_entries_names_no_endpoint():
    from core.mcp_profiles import _token_audience, _token_subject

    assert _token_audience({"aud": [_AUD]}) == _AUD
    assert _token_audience({"aud": [_AUD, "other"]}) is None
    assert _token_audience({"aud": "  "}) is None
    assert _token_subject({"sub": " 42 "}) == "42"
    assert _token_subject({}) is None
