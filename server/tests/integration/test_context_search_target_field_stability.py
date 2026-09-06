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

from tests.support.statement_router import (  # noqa: E402
    StatementInventory,
    UnknownStatement,
    describe,
)


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

    @staticmethod
    def _tokens(params) -> list[str]:
        """Les jetons que la VRAIE clause a mis en parametres -- Story 45.9.

        ⚠️ CE FAUX LISAIT `params[1]` COMME LE TERME, et depuis le 2026-08-05 ce
        parametre est la table d'accents (`_FOLD_FROM`) : la requete envoie, par
        colonne et par jeton, le triplet (accents_source, accents_cible,
        `%jeton%`). Le faux comparait donc « aaaaaaceeee... » au corpus, ne
        rendait RIEN, et le test lisait « top_seed absent » -- rouge depuis que
        45.9 a atterri, sur un faux en retard et non sur un defaut du produit.
        """
        out: list[str] = []
        for value in params[1:]:
            if isinstance(value, str) and value.startswith("%") and value.endswith("%"):
                token = value.strip("%")
                if token and token not in out:
                    out.append(token)
        return out

    @staticmethod
    def _hits(tokens, *values) -> bool:
        from core.context_search import fold  # noqa: PLC0415

        haystack = " ".join(fold(value) for value in values)
        return any(token in haystack for token in tokens)

    def topics_matching(self, params):
        tokens = self._tokens(params)
        return [
            (tid, pid, title, body)
            for tid, pid, title, body, _s in self._visible_topics()
            if self._hits(tokens, title, body)
        ]

    def topics_by_ids(self, params):
        ids = set(params[1])
        return [(t[0], t[1], t[2], t[3]) for t in self._visible_topics() if t[0] in ids]

    def procedures_matching(self, params):
        """Les SIX colonnes de la vraie clause, `frontmatter_yaml` compris."""
        tokens = self._tokens(params)
        return [
            (p[0], p[1], p[2], p[3], p[5], p[4])
            for p in self.procedures
            if self._hits(tokens, p[2], p[3], p[5], p[4])
        ]

    def procedures_by_ids(self, params):
        """Les CINQ colonnes de la vraie requete de voisinage (45.5, 2026-08-10)
        -- `frontmatter_yaml` en fait partie, sinon l'anti-declencheur ne peut
        pas etre lu au saut de graphe."""
        ids = set(params[1])
        return [(p[0], p[1], p[2], p[3], p[4]) for p in self.procedures if p[0] in ids]

    def procedure_by_name(self, params):
        name = params[0]
        for p in self.procedures:
            if p[6] == "active" and p[2] == name:
                return [(p[0], p[1], p[2], p[3], p[4], p[5], p[6], 1)]
        return []

    def schema_matching(self, params):
        tokens = self._tokens(params)
        return [s for s in self.schema if self._hits(tokens, s[2], s[4])]

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


# ---------------------------------------------------------------------------
# EVERY STATEMENT THE TWO TOOLS ISSUE ON THIS FIXTURE, NAMED ONCE (AI-317).
#
# The chain this replaces ended in an `else` that answered `self._rows = []`,
# and `[]` is not a refusal here: it is the exact answer the product reads as
# "this project holds no such row". The comment sitting in that `else` even
# claimed a new SELECT against `app.target_fields` would land there harmlessly
# -- which is the silence itself, written down. Both tools also wrap their read
# in `except Exception` (`agent_surface_mcp.py:182` for `search_context`,
# `:366` for `get_procedure`), so a statement that moved could only ever
# surface as "the context store could not be read" or a stable not-found.
#
# Declaration order is the order an `if/elif` would test in: first match wins.
# Three relations are read TWICE -- the lexical scan, then the by-id hydration
# of the nodes one graph hop away -- and each pair is told apart by the clause
# that differs, never by widening one fragment until it swallows both.
# ---------------------------------------------------------------------------
_STATEMENTS = StatementInventory(
    "test_context_search_target_field_stability._FakeCursor",
    # context_search.py:1310 -- `get_procedure_by_name`. Declared first because
    # it also reads `app.procedures`; its own join is what tells it apart.
    procedure_by_name="left join app.procedures_versions",
    # context_search.py:899 then :1222 -- the topic scan, then the hydration of
    # the topics one graph hop away. Same four columns, two different WHEREs.
    topic_scan=("from app.context_topics", "order by id"),
    topic_hydration=("from app.context_topics", "id = any(%s)"),
    # context_search.py:952 -- SIX columns, `frontmatter_yaml` last (AI-154).
    procedure_scan=("from app.procedures", "order by id"),
    # context_search.py:1244 -- FIVE columns, and not the same five: the
    # hydration reads `frontmatter_yaml` (it re-checks the anti-triggers) and
    # never reads `body_md`.
    procedure_hydration=("from app.procedures", "id = any(%s)"),
    # context_search.py:1036 then :1276 -- the schema scan and its hydration.
    schema_scan=("from app.schema_context", "order by id"),
    schema_hydration=("from app.schema_context", "id = any(%s)"),
    # context_search.py:1095 -- one hop from every direct match. THE statement
    # this file exists for: the target_field edges arrive through it.
    graph_edges="from app.context_graph",
    # project_resolver.py:86, reached from `ai_path_recorder._resolve_project_id`
    # (ai_path_recorder.py:248) on EVERY observed tool call, on the caller's own
    # connection -- so both tools issue it before they read anything else.
    # NEVER MODELLED: it fell into the silent `else`.
    project_status=("from app.projects", "select status"),
)

#: `get_procedure_by_name`'s projection carries `COALESCE(MAX(...)) AS
#: version_number`, which `describe` refuses to derive rather than guess.
_PROCEDURE_BY_NAME_COLUMNS = [
    (column,)
    for column in (
        "id", "project_id", "name", "description",
        "frontmatter_yaml", "body_md", "status", "version_number",
    )
]


class _FakeCursor:
    """Answers the statements above, and REFUSES every other one.

    `description` is DERIVED from the statement (`describe`) rather than spelled
    out: a hand-written column tuple is a second copy of the projection, and it
    is the copy that goes stale first, because nothing reads it. It used to be
    set by ONE branch and then survive on the cursor for every later statement.
    """

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
        statement = _STATEMENTS.match(sql)
        self._rows = []
        self._desc = (
            _PROCEDURE_BY_NAME_COLUMNS
            if statement == "procedure_by_name"
            else describe(sql)
        )
        match statement:
            case "procedure_by_name":
                self._rows = self._store.procedure_by_name(params)
            case "topic_scan":
                self._rows = self._store.topics_matching(params)
            case "topic_hydration":
                self._rows = self._store.topics_by_ids(params)
            case "procedure_scan":
                self._rows = self._store.procedures_matching(params)
            case "procedure_hydration":
                self._rows = self._store.procedures_by_ids(params)
            case "schema_scan":
                self._rows = self._store.schema_matching(params)
            case "schema_hydration":
                self._rows = self._store.schema_by_ids(params)
            case "graph_edges":
                self._rows = self._store.edges(params)
            case "project_status":
                # NO ROW, and that is this store's real shape: it declares
                # topics, procedures, schema docs and edges -- never a row in
                # `app.projects`. The path recorder reads absence exactly as it
                # reads a project the caller may not see (ai_path_recorder.py:
                # 240) and no-records the call, which is what it already does
                # here today. Answering an active project would invent one, and
                # would send the recorder on to `SELECT org_id` and to writes
                # this fixture models nothing of.
                self._rows = []
            case _:  # pragma: no cover -- a name in the inventory, unanswered
                raise _STATEMENTS.unknown(sql)

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


def test_the_fake_refuses_a_statement_it_was_never_taught():
    """AI-317: an unrecognised statement NAMES itself instead of answering none.

    This is the file's own premise made testable. The `else` this replaces said,
    in its comment, that a new SELECT against `app.target_fields` would "land
    here and return nothing" -- so the fourth lexical SELECT this file was
    written to catch would have been answered, quietly, with an empty corpus in
    BOTH runs, and the byte-identical comparison would still have passed.
    """
    cursor = _FakeCursor(_Store(_FIELD_EDGES))
    with pytest.raises(UnknownStatement) as raised:
        cursor.execute(
            "SELECT id, project_id, name, description FROM app.target_fields "
            "WHERE project_id = %s ORDER BY id"
        )
    message = str(raised.value)
    # The statement that moved, and the inventory to compare it against.
    assert "app.target_fields" in message
    assert "graph_edges" in message

    # And the two reads of `app.context_topics` really are told apart: the scan
    # and the by-id hydration of the graph neighbours project the same four
    # columns, so only their WHERE separates them.
    assert _STATEMENTS.find(
        "SELECT id, project_id, title, body_md FROM app.context_topics "
        "WHERE status = 'active' AND id = ANY(%s)"
    ) == "topic_hydration"


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
