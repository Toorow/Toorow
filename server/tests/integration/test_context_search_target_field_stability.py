"""Story 44.10 hard AC -- target_field nodes do NOT enter the MCP corpus.

The mindmap gained a fourth node type; ``search_context`` and ``get_procedure``
did not. Those two tools are the agent-facing contract (AD-1/AD-2), and adding
dictionary fields to their corpus in a "graph page" story would silently change
what every LLM session retrieves.

This file proves the tool is INERT with respect to the new node type: the same
corpus is searched twice through the real FastMCP tool layer, once without any
target_field edge and once with them present, and BOTH channels of the result
(the <=30-line LLM summary and the structuredContent envelope) must be
BYTE-IDENTICAL.

Because the fake connection below serves the target_field edges from
app.context_graph exactly as Postgres would, a regression that starts
hydrating them (a `target_field` bucket in
``core.context_search._hydrate_neighbours``, or a fourth lexical SELECT) makes
the second run return an extra hit and the comparison fails.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402
from core.main import mcp  # noqa: E402
from fastmcp.client import Client, FastMCPTransport  # noqa: E402


class _Store:
    """Minimal corpus: topics + procedures + schema docs + graph edges."""

    def __init__(self, edges):
        # (id, project_id, title, body_md, status)
        self.topics = [
            ("top_seed", "projA", "Pacing seed", "term pacing here", "active"),
            ("top_link", "projA", "Linked concept", "no lexical term", "active"),
        ]
        # (id, project_id, name, description, frontmatter, body, status)
        self.procedures = [
            ("proc_pace", "projA", "pacing_playbook", "Pacing playbook",
             "name: pacing_playbook\ndescription: Pacing playbook",
             "steps", "active"),
        ]
        # (id, project_id, relation, doc_kind, body_md)
        self.schema = [
            ("sctx_a", "projA", "fact_budget", "columns", "budget columns"),
        ]
        # (from_id, from_type, to_id, to_type, project_id)
        self.edges_data = edges

    def _visible_topics(self):
        return [t for t in self.topics if t[4] == "active"]

    def topics_matching(self, params):
        term = params[1].strip("%").lower()
        return [
            (tid, pid, title, body)
            for tid, pid, title, body, _s in self._visible_topics()
            if term in (title or "").lower() or term in (body or "").lower()
        ]

    def topics_by_ids(self, params):
        ids = set(params[1])
        return [(t[0], t[1], t[2], t[3]) for t in self._visible_topics() if t[0] in ids]

    def procedures_matching(self, params):
        term = params[1].strip("%").lower()
        return [
            (p[0], p[1], p[2], p[3], p[5])
            for p in self.procedures
            if term in p[2].lower() or term in (p[3] or "").lower() or term in (p[5] or "").lower()
        ]

    def procedures_by_ids(self, params):
        ids = set(params[1])
        return [(p[0], p[1], p[2], p[3]) for p in self.procedures if p[0] in ids]

    def procedure_by_name(self, params):
        name = params[0]
        for p in self.procedures:
            if p[6] == "active" and p[2] == name:
                return [(p[0], p[1], p[2], p[3], p[4], p[5], p[6], 1)]
        return []

    def schema_matching(self, params):
        term = params[1].strip("%").lower()
        return [
            s for s in self.schema
            if term in s[2].lower() or term in (s[4] or "").lower()
        ]

    def schema_by_ids(self, params):
        ids = set(params[1])
        return [s for s in self.schema if s[0] in ids]

    def edges(self, params):
        matched = set(params[1])
        return [
            (f_id, f_t, t_id, t_t)
            for f_id, f_t, t_id, t_t, _pid in self.edges_data
            if f_id in matched or t_id in matched
        ]


class _FakeCursor:
    def __init__(self, store: _Store) -> None:
        self._store = store
        self._rows: list[tuple] = []
        self._desc: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    @property
    def description(self):
        return self._desc

    def execute(self, sql: str, params=None):
        params = params or ()
        s = " ".join(sql.split())
        if "FROM app.context_topics" in s and "id = ANY" not in s:
            self._rows = self._store.topics_matching(params)
        elif "FROM app.procedures" in s and "procedures_versions" not in s and "id = ANY" not in s:
            self._rows = self._store.procedures_matching(params)
        elif "FROM app.schema_context" in s and "id = ANY" not in s:
            self._rows = self._store.schema_matching(params)
        elif "FROM app.context_graph" in s:
            self._rows = self._store.edges(params)
        elif "FROM app.context_topics" in s and "id = ANY" in s:
            self._rows = self._store.topics_by_ids(params)
        elif "FROM app.procedures" in s and "id = ANY" in s:
            self._rows = self._store.procedures_by_ids(params)
        elif "FROM app.schema_context" in s and "id = ANY" in s:
            self._rows = self._store.schema_by_ids(params)
        elif "procedures_versions" in s:
            self._rows = self._store.procedure_by_name(params)
            self._desc = [
                (c,) for c in (
                    "id", "project_id", "name", "description",
                    "frontmatter_yaml", "body_md", "status", "version_number",
                )
            ]
        else:
            # Notably: any NEW select against app.target_fields would land here
            # and return nothing -- but the two runs would still diverge,
            # because the extra edge would then have produced a hit.
            self._rows = []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, store: _Store) -> None:
        self._store = store

    def cursor(self):
        return _FakeCursor(self._store)

    def commit(self):
        pass

    def rollback(self):
        pass


def _fake_get_connection(store: _Store):
    @contextmanager
    def _cm():
        yield _FakeConn(store)

    return _cm


def _text(result) -> str:
    blocks = [c for c in (result.content or []) if getattr(c, "text", None)]
    return blocks[0].text if blocks else ""


async def _run_search(store: _Store, query: str):
    with patch("core.main._resolve_project", return_value="projA"), patch(
        "core.db.get_connection", _fake_get_connection(store)
    ):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "search_context", {"query": query, "project_id": "projA"}
            )
    assert not result.is_error, f"search_context errored: {result}"
    envelope = result.structured_content or result.data
    return _text(result), json.dumps(envelope, sort_keys=True, ensure_ascii=False)


# The knowledge half of the corpus, identical in both runs.
_BASE_EDGES = [
    ("top_seed", "topic", "top_link", "topic", "projA"),
]

# ...plus dictionary-field edges of every shape 44.10 allows.
_FIELD_EDGES = _BASE_EDGES + [
    ("top_seed", "topic", "clicks", "target_field", "projA"),
    ("sessions", "target_field", "top_seed", "topic", "projA"),
    ("clicks", "target_field", "sessions", "target_field", "projA"),
]


@pytest.mark.anyio
async def test_search_context_is_byte_identical_with_and_without_field_edges():
    """The hard AC: adding target_field edges to app.context_graph changes
    NEITHER the LLM summary NOR the structuredContent envelope."""
    without_summary, without_json = await _run_search(_Store(_BASE_EDGES), "pacing")
    with_summary, with_json = await _run_search(_Store(_FIELD_EDGES), "pacing")

    assert with_summary == without_summary
    assert with_json == without_json

    # And the run is not vacuous: the corpus really did return hits, including
    # the topic-to-topic graph neighbour, so "identical" is not "both empty".
    payload = json.loads(without_json)
    ids = {r["id"] for r in payload["data"]["results"]}
    assert "top_seed" in ids
    assert "top_link" in ids


@pytest.mark.anyio
async def test_search_context_never_surfaces_a_target_field_endpoint():
    """Explicit form of the same rule: no result carries a dictionary field id
    or a 'target_field' kind, even though three such edges touch a matched
    node."""
    _summary, payload_json = await _run_search(_Store(_FIELD_EDGES), "pacing")
    payload = json.loads(payload_json)
    results = payload["data"]["results"]

    ids = {r["id"] for r in results}
    assert "clicks" not in ids
    assert "sessions" not in ids
    assert {r["kind"] for r in results} <= {"topic", "procedure", "schema_doc"}


@pytest.mark.anyio
async def test_get_procedure_is_unaffected_by_field_edges():
    """get_procedure's contract is untouched too (it never reads the graph, but
    the story names both tools, so both are pinned)."""
    async def _call(store):
        with patch("core.main._resolve_project", return_value="projA"), patch(
            "core.db.get_connection", _fake_get_connection(store)
        ):
            async with Client(FastMCPTransport(mcp)) as client:
                return await client.call_tool(
                    "get_procedure", {"name": "pacing_playbook", "project_id": "projA"}
                )

    before = await _call(_Store(_BASE_EDGES))
    after = await _call(_Store(_FIELD_EDGES))

    assert _text(before) == _text(after)
    before_json = json.dumps(
        before.structured_content or before.data, sort_keys=True, ensure_ascii=False
    )
    after_json = json.dumps(
        after.structured_content or after.data, sort_keys=True, ensure_ascii=False
    )
    assert before_json == after_json
    # Not vacuous: the procedure really was found in both runs.
    assert json.loads(before_json)["data"]["found"] is True
