"""Story 75-1 -- `propose_calculated_field`, THE TOOL, on a real Postgres.

WHY THE TOOL AND NOT THE FUNCTION. AI-345 measured what a function test proves
about an MCP door: nothing about the door. The render tool refused its own spec
in production while its composer stayed green. These tests call
`propose_calculated_field` by name through `Client(FastMCPTransport(...))`, so
the argument shapes, the `ToolError` envelopes and the `structured_content` a
host receives are the ones a host receives.

WHY A REAL DATABASE. Every property is a row or a transaction: the proposal
marked `agent` and attributed to the CALLING identity, the type inferred by the
server, and -- the load-bearing one -- a formula refused leaving no row behind.

Every test rolls back: the tool's own `conn.commit()` lands on a proxy whose
commit is a no-op, so the assertions read uncommitted rows on the same
connection and the fixture rolls them back.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

import pytest
from core import calculated_field_proposals_mcp as door
from fastmcp import Client, FastMCP
from fastmcp.client.transports import FastMCPTransport
from fastmcp.exceptions import ToolError

from tests.core.test_analyze_artifacts_pg import Chain
from tests.core.test_calculated_field_proposals_pg import _metric, _ratio, _uid

pytestmark = [pytest.mark.usefixtures("live_postgres"), pytest.mark.anyio]

IDENTITY = "agent@example.com"


class _NoCommit:
    """The fixture's connection, with `commit()` swallowed so the test rolls back."""

    def __init__(self, conn):
        self._conn = conn

    def commit(self) -> None:
        return None

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


@pytest.fixture()
def chain(live_postgres):
    built = Chain(live_postgres).build()
    yield built
    live_postgres.rollback()


@pytest.fixture()
def pins(chain):
    return {
        "clicks": _metric(chain, "clicks", "integer"),
        "spend": _metric(chain, "spend", "money", unit="EUR"),
    }


@pytest.fixture()
def wired(chain, monkeypatch):
    """The real tool on a bare FastMCP, with the caller resolved and the connection ours."""
    import core.db
    import core.mcp_scope

    monkeypatch.setattr(core.mcp_scope, "caller_identity", lambda: IDENTITY)
    scope_calls: list[tuple] = []

    def _scope(project_id, identity, *, minimum_capability="view"):
        scope_calls.append((project_id, identity, minimum_capability))

    monkeypatch.setattr(core.mcp_scope, "refuse_unless_project_scope", _scope)

    @contextmanager
    def _request_connection(identity):
        # Production's `request_connection` discards uncommitted work when the
        # body raises; here the same fact is a SAVEPOINT, so a refused call
        # rolls back the tool's rows and keeps the seeded chain for the
        # assertions.
        assert identity == IDENTITY
        with chain.conn.transaction():
            yield _NoCommit(chain.conn)

    monkeypatch.setattr(core.db, "request_connection", _request_connection)
    target = FastMCP("story-75-1")
    door.register(target)
    return target, scope_calls


async def _call(target: FastMCP, **arguments):
    async with Client(FastMCPTransport(target)) as client:
        return await client.call_tool("propose_calculated_field", arguments)


def _envelope(exc: ToolError) -> dict[str, Any]:
    text = str(exc)
    return json.loads(text[text.index("{") : text.rindex("}") + 1])


def _count(conn, project_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.calculated_field_proposals WHERE project_id = %s",
            (project_id,),
        )
        return int(cur.fetchone()[0])


# ---------------------------------------------------------------------------
# The door opens, and what it files is marked as a machine's
# ---------------------------------------------------------------------------


async def test_the_tool_files_an_agent_proposal_the_server_typed_and_pinned(
    chain, pins, wired
):
    target, scope_calls = wired
    result = await _call(
        target,
        project_id=chain.project_id,
        name="cost_per_click",
        expression=_ratio(pins),
        result_id=chain.result_id,
        description="Spend divided by clicks, found while reading the August result.",
    )

    # A WRITE whose acceptance opens a change-set, so `edit` -- not `view`.
    assert scope_calls == [(chain.project_id, IDENTITY, "edit")]

    envelope = result.structured_content
    assert envelope["schema_version"] == "1"
    data = envelope["data"]
    assert data["id"].startswith("cfp_")
    assert data["status"] == "open"
    # MARKED AS MACHINE, AND NAMED: the queue records WHICH agent said it.
    assert data["origin"] == "agent"
    assert data["requested_by"] == IDENTITY
    # INFERRED by the server: the caller declared no type.
    assert data["value_type"] == "ratio"
    assert {(d["concept_id"], d["version_id"]) for d in data["dependencies"]} == {
        pins["spend"], pins["clicks"]
    }
    # The exploration is named, and the plan version was derived from the Result.
    assert data["provenance"]["result_id"] == chain.result_id
    assert data["provenance"]["query_spec_version_id"] == chain.query_spec_version_id

    # The row is real, and it says the same things.
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT origin, requested_by, value_type, status FROM "
            "app.calculated_field_proposals WHERE id = %s",
            (data["id"],),
        )
        assert cur.fetchone() == ("agent", IDENTITY, "ratio", "open")

    # The text says the identities, and promises nothing it does not do.
    text = result.content[0].text
    assert data["id"] in text and "publishes nothing" in text
    whole = json.dumps({"text": text, "structured": envelope})
    assert "http" not in whole


async def test_the_tool_refuses_a_formula_by_name_and_writes_nothing(chain, pins, wired):
    target, _scope = wired
    with pytest.raises(ToolError) as excinfo:
        await _call(
            target,
            project_id=chain.project_id,
            name="anything_at_all",
            expression={"op": "raw_sql", "sql": "SELECT 1"},
            result_id=chain.result_id,
        )

    body = _envelope(excinfo.value)
    assert body["code"] == "invalid_expression"
    assert [r["code"] for r in body["refusals"]] == ["unknown_operation"]
    assert _count(chain.conn, chain.project_id) == 0


async def test_the_tool_refuses_an_exploration_this_project_does_not_hold(
    chain, pins, wired
):
    target, _scope = wired
    with pytest.raises(ToolError) as excinfo:
        await _call(
            target,
            project_id=chain.project_id,
            name="cost_per_click",
            expression=_ratio(pins),
            result_id=_uid("qr"),
        )

    assert _envelope(excinfo.value)["code"] == "unknown_provenance"
    assert _count(chain.conn, chain.project_id) == 0


# ---------------------------------------------------------------------------
# A foreign Project -- refused THROUGH THE TOOL, before any work
# ---------------------------------------------------------------------------


async def test_a_foreign_project_is_refused_through_the_tool_before_any_work(
    chain, pins, monkeypatch
):
    """The scope seam is NOT stubbed here: the real one runs, is denied, and the
    refusal must fall before the queue is touched. A tripwire on `propose`
    proves 'before', not merely 'eventually'.

    `request_connection` is deliberately left alone: the seam itself opens one
    to resolve the access decision, and a tripwire there would make the guard
    fail CLOSED for the wrong reason -- the test would pass on an exception it
    caused rather than on the refusal it claims to measure.
    """
    import core.calculated_field_proposals
    import core.mcp_scope

    monkeypatch.setattr(core.mcp_scope, "caller_identity", lambda: IDENTITY)
    monkeypatch.setattr(
        "core.project_access.resolve_strict_resource_access",
        lambda *a, **k: _denied(),
    )

    reached: list[str] = []

    def _tripwire(*_a, **_k):
        reached.append("propose")
        raise AssertionError("the queue was reached despite the refusal")

    monkeypatch.setattr(core.calculated_field_proposals, "propose", _tripwire)

    target = FastMCP("story-75-1-stranger")
    door.register(target)

    with pytest.raises(ToolError) as excinfo:
        await _call(
            target,
            project_id="proj_EXAMPLE",
            name="cost_per_click",
            expression=_ratio(pins),
            result_id=chain.result_id,
        )

    from core.project_resolver import PROJECT_NOT_FOUND_CODE

    assert PROJECT_NOT_FOUND_CODE in str(excinfo.value), (
        "a distinct envelope for 'forbidden' is an enumeration oracle wearing "
        "the word security"
    )
    assert not reached
    assert _count(chain.conn, chain.project_id) == 0


def _denied():
    from types import SimpleNamespace

    return SimpleNamespace(
        allowed=False, org_id=None, reason="grant_required", capability=None
    )
