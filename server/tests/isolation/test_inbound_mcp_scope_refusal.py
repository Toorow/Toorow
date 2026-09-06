"""Story 38.15 / AD-5 -- an inbound MCP tool refuses a Datastream it may not read.

WHY THIS FILE EXISTS, measured rather than asserted. On 2026-08-10 the AD-5
scope check in `inbound_mcp._readable_datastream` was replaced by `pass` and the
suite was run:

    tests/core/test_inbound_mcp.py + routing_test + health -> 64 passed, 0 red
    tests/core + tests/conformance (mcp/inbound/scope/redaction/access)
      mutant : 18 failed, 1263 passed, 2 errors
      base   : 18 failed, 1263 passed, 2 errors      <- IDENTICAL

Six tools could read another tenant's deliveries, inbox, timeline, mapping
context and versions without a line going red. The reason was not subtle:
every existing test patches `_readable_datastream` ITSELF, so the guard inside
it is never executed. A double of the thing under test proves the caller, never
the guard.

THE PROPERTY UNDER TEST IS A REFUSAL, so it is measured through the tool
function with an identity attached -- never with `inspect.getsource`. The
structural half (a NEW tool arriving unguarded) lives in
`tests/conformance/test_datastream_readers_carry_project_scope.py`; the two are
complementary and neither replaces the other.

NO POSTGRES. The decision point is `resolve_strict_resource_access`, doubled
here to refuse. What is under test is not access resolution -- that has its own
pg-gated tests -- but that these six tools CALL it before they read.

ASCII-only source (AI-03).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import core.inbound_mcp as inbound_mcp
import pytest

#: The one project every double answers with. `proj_EXAMPLE` is the prescribed
#: placeholder -- never a production identifier.
_PROJECT = "proj_EXAMPLE"
_DATASTREAM = "ds_EXAMPLE"


class _Cursor:
    """A cursor that answers the scope lookup and records every statement."""

    def __init__(self, statements: list[str], row):
        self._statements = statements
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None):
        self._statements.append(" ".join(str(sql).split()))
        del params

    def fetchone(self):
        return self._row

    def fetchall(self):
        return []


class _Connection:
    """The connection `_readable_datastream` opens, with its statements visible."""

    def __init__(self, row=(_PROJECT,)):
        self.statements: list[str] = []
        self.closed = False
        self.committed = False
        self._row = row

    def cursor(self):
        return _Cursor(self.statements, self._row)

    def close(self):
        self.closed = True

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        # AI-292 -- THE FAKE MUST DO WHAT THE REAL MANAGER DOES, and it did not.
        # `core.db.get_connection` is a @contextmanager whose `finally` calls
        # `conn.close()` (`db.py:98-100`); this returned False and closed nothing.
        # `_readable_datastream` releases through the MANAGER on every refusal
        # branch (`inbound_mcp.py:159,163,170`) -- deliberately, so the day that
        # `finally` also frees a pool slot the six callers do not skip it. The
        # production path was therefore correct, and six tests read as a connection
        # leak that never existed.
        #
        # A fake that answers differently from the thing it stands in for measures
        # itself. It closes now, like the manager it replaces.
        self.close()
        return False


@pytest.fixture()
def a_stranger(monkeypatch):
    """An AUTHENTICATED identity that does not hold the Datastream's project.

    The token matters: without it `_caller_identity` returns `anonymous`, and
    the test would measure the anonymous carve-out rather than the guard.
    """
    token = SimpleNamespace(claims={"sub": "person_stranger"}, client_id="cli")
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_access_token", lambda: token, raising=False
    )
    return token


def _denied_decision(*_args, **_kwargs):
    return SimpleNamespace(
        allowed=False, org_id=None, reason="grant_required", capability=None
    )


def _granted_decision(*_args, **_kwargs):
    return SimpleNamespace(
        allowed=True, org_id="org_EXAMPLE", reason=None, capability="view"
    )


#: The six tools, each called with the MINIMUM valid arguments. What is measured
#: is that the refusal lands before any work, so the business arguments need not
#: be realistic -- only the Datastream id is load-bearing.
_TOOLS = {
    "get_inbound_health": lambda: inbound_mcp.get_inbound_health(_DATASTREAM),
    "list_inbound_attachments": lambda: inbound_mcp.list_inbound_attachments(_DATASTREAM),
    "get_inbound_delivery": lambda: inbound_mcp.get_inbound_delivery(
        _DATASTREAM, "inbrx_EXAMPLE"
    ),
    "get_inbound_mapping_context": lambda: inbound_mcp.get_inbound_mapping_context(
        _DATASTREAM, "inbraw_EXAMPLE"
    ),
    "prepare_inbound_reprocess": lambda: inbound_mcp.prepare_inbound_reprocess(
        _DATASTREAM, "inbraw_EXAMPLE"
    ),
    # AI-281 -- la porte de PORTEE. Elle prend le meme chemin de garde que sa
    # soeur unitaire, donc elle doit refuser un etranger de la meme facon : sans
    # cette entree, un outil neuf entrerait dans l inventaire sans jamais etre
    # balaye ici.
    "prepare_inbound_reprocess_scope": lambda: inbound_mcp.prepare_inbound_reprocess_scope(
        _DATASTREAM, ["inbraw_EXAMPLE"]
    ),
    "test_inbound_routing": lambda: inbound_mcp.test_inbound_routing(_DATASTREAM),
}

#: WHAT THE ACQUISITION SPENDS BEFORE THE SURFACE RUNS AT ALL -- 2026-08-21.
#: `_readable_datastream` acquires through `core.db.request_connection`, which
#: installs the access context on the connection it hands back: `SELECT
#: current_user`, `SET ROLE`, two `set_config`, and the read-back that refuses a
#: transaction-mode pooler. None of them names a Datastream, a project or a
#: tenant, and they are byte-identical for every caller -- so counting them among
#: "the statements a refusal spent" would make this file assert the shape of
#: `core/db.py` instead of the refusal it exists for. They are named, then set
#: aside; every OTHER statement is still measured exactly as before.
_ACCESS_CONTEXT_MARKERS = ("current_user", "set role", "set_config", "toorow.")


def _business_statements(connection) -> list[str]:
    """Everything the connection ran that was not the access context install."""
    return [
        sql
        for sql in connection.statements
        if not any(marker in sql.lower() for marker in _ACCESS_CONTEXT_MARKERS)
    ]


#: Where each tool would do its work. A refusal that still reaches one of these
#: has spent the query it exists to withhold.
_WORK = (
    "core.inbound_health.get_inbound_health",
    "core.inbound_health.get_attachment_inbox",
    "core.inbound_health.get_delivery_timeline",
    "core.inbound_mapping_entry.get_mapping_repair_context",
    "core.inbound_reprocess.prepare_reprocess",
    "core.inbound_routing_test.run_datastream_routing_test",
)


def test_the_tool_inventory_under_test_is_the_registered_one():
    """A tool added to the surface and not to this file would be untested.

    The list above is hand-written -- calling a tool needs arguments a
    derivation cannot invent -- so it is pinned to the declared tuple instead.
    """
    from core.inbound_mcp import INBOUND_MCP_TOOLS

    assert sorted(_TOOLS) == sorted(INBOUND_MCP_TOOLS), (
        "the inbound MCP surface changed and this refusal suite did not follow:\n"
        f"  registered but untested : {sorted(set(INBOUND_MCP_TOOLS) - set(_TOOLS))}\n"
        f"  tested but unregistered : {sorted(set(_TOOLS) - set(INBOUND_MCP_TOOLS))}"
    )


@pytest.mark.parametrize("tool_name", sorted(_TOOLS))
def test_a_stranger_is_refused_by_every_inbound_tool(tool_name, a_stranger, monkeypatch):
    """THE test the mutation survived: refuse, and refuse with the one envelope."""
    connection = _Connection()
    monkeypatch.setattr("core.db.get_connection", lambda *a, **k: connection)
    monkeypatch.setattr(
        "core.project_access.resolve_strict_resource_access", _denied_decision
    )

    answer = _TOOLS[tool_name]()

    assert answer == inbound_mcp._denied("not_readable"), (
        f"{tool_name} answered a Datastream outside the caller's readable "
        f"projects: {answer!r}. Removing the AD-5 check in "
        "`_readable_datastream` left 0 lines red on 2026-08-10 -- this is the "
        "line that must now fall."
    )
    assert connection.closed, f"{tool_name} refused but leaked its connection"


@pytest.mark.parametrize("tool_name", sorted(_TOOLS))
def test_the_refusal_lands_before_any_read(tool_name, a_stranger, monkeypatch):
    """A refusal that queries first has already answered the question it refuses.

    The load and the response time of a read that is performed then thrown away
    are themselves an answer about a Datastream the caller may not name.
    """
    connection = _Connection()
    monkeypatch.setattr("core.db.get_connection", lambda *a, **k: connection)
    monkeypatch.setattr(
        "core.project_access.resolve_strict_resource_access", _denied_decision
    )

    reached: list[str] = []

    def _tripwire(name):
        def _boom(*_a, **_k):
            reached.append(name)
            raise AssertionError(f"{tool_name} read {name} despite refusing")

        return _boom

    with (
        patch(_WORK[0], _tripwire(_WORK[0])),
        patch(_WORK[1], _tripwire(_WORK[1])),
        patch(_WORK[2], _tripwire(_WORK[2])),
        patch(_WORK[3], _tripwire(_WORK[3])),
        patch(_WORK[4], _tripwire(_WORK[4])),
        patch(_WORK[5], _tripwire(_WORK[5])),
    ):
        answer = _TOOLS[tool_name]()

    assert answer == inbound_mcp._denied("not_readable")
    assert not reached, f"{tool_name} reached {reached} before/despite its refusal"
    # The ONLY statement a refusal may spend is the scope lookup itself.
    business = _business_statements(connection)
    assert all("app.datastreams" in sql for sql in business), (
        f"{tool_name} ran a statement that is not the scope lookup before "
        f"refusing: {business}"
    )


@pytest.mark.parametrize("tool_name", sorted(_TOOLS))
def test_refused_and_unavailable_are_the_same_answer(tool_name, a_stranger, monkeypatch):
    """Two refusals that differ teach the caller that a Datastream exists.

    The easiest difference to produce is an outage, which the guard answers by
    failing closed -- so the two must be byte-identical from outside.
    """
    connection = _Connection()
    monkeypatch.setattr("core.db.get_connection", lambda *a, **k: connection)
    monkeypatch.setattr(
        "core.project_access.resolve_strict_resource_access", _denied_decision
    )
    refused = _TOOLS[tool_name]()

    def _explode(*_a, **_k):
        raise RuntimeError("the database is unreachable")

    monkeypatch.setattr("core.project_access.resolve_strict_resource_access", _explode)
    unavailable = _TOOLS[tool_name]()

    assert refused == unavailable, (
        f"{tool_name} distinguishes refused from unavailable:\n"
        f"  refused     : {refused}\n"
        f"  unavailable : {unavailable}"
    )


@pytest.mark.parametrize("tool_name", sorted(_TOOLS))
def test_an_absent_datastream_is_indistinguishable_from_a_refused_one(
    tool_name, a_stranger, monkeypatch
):
    """The other half of AD-5: existence must not leak through the answer."""
    absent = _Connection(row=None)
    monkeypatch.setattr("core.db.get_connection", lambda *a, **k: absent)
    monkeypatch.setattr(
        "core.project_access.resolve_strict_resource_access", _granted_decision
    )
    missing = _TOOLS[tool_name]()

    present_but_foreign = _Connection()
    monkeypatch.setattr("core.db.get_connection", lambda *a, **k: present_but_foreign)
    monkeypatch.setattr(
        "core.project_access.resolve_strict_resource_access", _denied_decision
    )
    foreign = _TOOLS[tool_name]()

    assert missing == foreign, (
        f"{tool_name} answers an absent Datastream differently from a foreign "
        f"one:\n  absent  : {missing}\n  foreign : {foreign}\n"
        "Comparing the two enumerates other tenants' Datastreams."
    )


def test_a_holder_of_the_project_is_not_refused(a_stranger, monkeypatch):
    """The guard isolates; it does not close the door on everybody.

    Without this, an unconditional refusal would satisfy every test above --
    and a tool that refuses its rightful operator is broken, not secure.
    """
    connection = _Connection()
    monkeypatch.setattr("core.db.get_connection", lambda *a, **k: connection)
    monkeypatch.setattr(
        "core.project_access.resolve_strict_resource_access", _granted_decision
    )

    served = {"items": [], "count": 0}
    with patch("core.inbound_health.get_attachment_inbox", return_value=[]):
        answer = inbound_mcp.list_inbound_attachments(_DATASTREAM)

    assert answer == served, (
        "a holder of the Datastream's project was refused its own inbox: "
        f"{answer!r}"
    )
    assert answer != inbound_mcp._denied("not_readable")


def test_the_scope_lookup_binds_the_datastream_the_caller_named(
    a_stranger, monkeypatch
):
    """The project authorized must be the one derived FROM this Datastream.

    This is the epic-53 lesson in its inbound form: a guard that authorizes some
    project while the read serves another proves nothing. The statement that
    resolves the pair is asserted here so it cannot quietly become a lookup of
    something else.
    """
    connection = _Connection()
    monkeypatch.setattr("core.db.get_connection", lambda *a, **k: connection)
    seen: list[tuple] = []

    def _record(identity, conn, **kwargs):
        seen.append((identity, kwargs.get("project_id")))
        return _denied_decision()

    monkeypatch.setattr("core.project_access.resolve_strict_resource_access", _record)
    inbound_mcp.list_inbound_attachments(_DATASTREAM)

    business = _business_statements(connection)
    assert business, "no scope lookup was issued at all"
    lookup = business[0]
    assert "project_id" in lookup and "app.datastreams" in lookup and "id = %s" in lookup, (
        f"the scope lookup no longer derives the project from the Datastream: {lookup}"
    )
    assert seen == [("person_stranger", _PROJECT)], (
        "the authorization did not name the identity from the token and the "
        f"project derived from the Datastream: {seen}"
    )
