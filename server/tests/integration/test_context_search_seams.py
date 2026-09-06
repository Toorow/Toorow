"""Seam + ranking + scoping tests for Story 11.5 MCP tools search_context / get_procedure.

AI-56 discipline: the two CORE-owned agent-surface tools are exercised THROUGH the
FastMCP tool layer (Client(FastMCPTransport(mcp))) -- identity resolution, envelope
wrapping and the dual-channel split -- not just via direct core.context_search calls.

Two seam strategies, mirroring the established conventions:
  * DB-less seams: a SQL-dispatching fake connection feeds seeded rows so the MCP
    tool layer + ranking + AD-1 summary/detail split are asserted by VALUE without
    a live Postgres. ``core.main._resolve_project`` is patched to a passthrough so
    the tests focus on retrieval/ranking/scoping (the resolver has its own tests).
  * live_postgres seams: the real 031 schema is loaded and multi-project rows are
    seeded, proving AD-5 scoping and get_procedure-by-name against real SQL. Skipped
    locally when TEST_POSTGRES_DSN is absent (CI isolation covers it).

AD-1  : search_context summary <= 30 LINES + full detail in structuredContent (proven).
AD-2  : both tools are core-owned + un-namespaced (asserted in tools/list).
AD-5  : project sees its own + platform, never another project's (proven both seams).
AI-13 : N/A -- these tools touch NO external API (context lives in platform Postgres).
"""

from __future__ import annotations

import contextlib
import os
import uuid
from contextlib import contextmanager

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from pathlib import Path  # noqa: E402
from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402
from core.main import mcp  # noqa: E402
from fastmcp.client import Client, FastMCPTransport  # noqa: E402

from tests.support.statement_router import (  # noqa: E402
    StatementInventory,
    UnknownStatement,
    describe,
)

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "infra" / "nango" / "migrations" / "031_context_layer.sql"
requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Postgres seam skipped",
)


# ---------------------------------------------------------------------------
# DB-less seam: a SQL-dispatching fake connection.
#
# The tools issue a small, fixed set of SELECTs. This fake matches on stable SQL
# fragments and returns column tuples in the order the tool's SELECT declares.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# EVERY STATEMENT THE TWO TOOLS ISSUE, NAMED ONCE (AI-317).
#
# The chain this replaces ended in an `else` that answered `self._rows = []` --
# and `[]` is not a refusal here, it is the exact answer the product reads as
# "this project has no such topic", "this Skill designates no governed field".
# Worse, `core.agent_surface_mcp.get_procedure` turns ANY exception into a
# stable not-found and `search_context` into "the context store could not be
# read", so a statement that moved could only ever surface as an empty corpus.
#
# Declaration order is the order an `if/elif` would test in: first match wins.
# Three relations are read TWICE -- the scan, then the by-id hydration of the
# nodes reached by one graph hop -- and each pair is told apart by the clause
# that differs, never by widening one fragment until it swallows both.
# ---------------------------------------------------------------------------
_STATEMENTS = StatementInventory(
    "test_context_search_seams._FakeCursor",
    # context_search.py:1310 -- `get_procedure_by_name`. The only statement here
    # whose projection `describe` refuses (an aggregate behind an alias), so it
    # is the only one whose columns are named by hand below.
    procedure_by_name="left join app.procedures_versions",
    # context_search.py:899 then :1222 -- the topic scan, then the hydration of
    # the topics one graph hop away. Same four columns, two different WHEREs.
    topic_scan=("from app.context_topics", "order by id"),
    topic_hydration=("from app.context_topics", "id = any(%s)"),
    # context_search.py:952 -- SIX columns, `frontmatter_yaml` last (AI-154).
    procedure_scan=("from app.procedures", "order by id"),
    # context_search.py:1241 -- FIVE columns, and NOT the same five: the
    # hydration reads `frontmatter_yaml` (it re-checks the anti-triggers) and
    # never reads `body_md`. This fake answered a FOUR-column row here, which
    # `_hydrate_neighbours` unpacks into five names -- it has simply never been
    # reached, because no fixture in this file links a procedure by an edge.
    procedure_hydration=("from app.procedures", "id = any(%s)"),
    # context_search.py:1036 then :1273 -- the schema scan and its hydration.
    schema_scan=("from app.schema_context", "order by id"),
    schema_hydration=("from app.schema_context", "id = any(%s)"),
    # context_search.py:1095 -- one hop from every direct match.
    graph_edges="from app.context_graph",
    # context_search.py:659 -- what the walk WRITES: the aggregate fate of the
    # candidates it rejected (AI-157). Read back by `record_fates` itself.
    candidate_fates="insert into app.context_candidate_fates",
    # governed_field_catalogue.py:70 and :82, reached from `get_procedure` via
    # `mdm_references.resolve` (agent_surface_mcp.py:303) for the `mdm_tags` of
    # the Skill being served -- `proc_ra` carries one, so BOTH statements are
    # issued by every get_procedure test in this file. NEVER MODELLED: they fell
    # into the silent `else`, and `governed_field_catalogue._fetch` named their
    # columns from a `description` no statement of theirs had ever set.
    semantic_concepts="from app.semantic_concepts",
    field_dictionary="from app.target_fields",
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


def _fates_upserted(params: tuple | list) -> list[tuple]:
    """What `INSERT ... RETURNING` hands back -- one row per tuple written.

    Six values per row, in the order `record_fates` flattens them
    (context_search.py:648): id, project_id, candidate_id, candidate_kind,
    reason, last_query. The upsert returns five of those plus `times`, and on a
    store that has never seen the pair before `times` is 1 -- which a fresh
    `_Store` always is. Answering `[]` would say the write inserted NOTHING,
    while `record_fates` reads these very rows to decide whether a candidate has
    just crossed the recurrence threshold.
    """
    values = list(params)
    return [
        (values[i + 2], values[i + 3], values[i + 4], 1, values[i + 5])
        for i in range(0, len(values) - 5, 6)
    ]


class _FakeCursor:
    """Answers the statements above, and REFUSES every other one.

    `description` is DERIVED from the statement (`describe`) rather than spelled
    out: a hand-written column tuple is a second copy of the projection, and it
    is the copy that goes stale first, because nothing reads it. It also used to
    SURVIVE the statement that set it -- one branch assigned `_desc`, and every
    later statement on the same cursor inherited those eight column names.
    """

    def __init__(
        self, store: "_Store", scope_project: str, written: list[str] | None = None
    ) -> None:
        self._store = store
        self._scope = scope_project
        self._rows: list[tuple] = []
        self._desc: list[tuple] = []
        self._written = written if written is not None else []

    def __enter__(self):
        return self

    def __exit__(self, *a):
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
                self._rows = self._store.procedure_by_name(self._scope, params)
            case "topic_scan":
                self._rows = self._store.topics_matching(self._scope, params)
            case "topic_hydration":
                self._rows = self._store.topics_by_ids(self._scope, params)
            case "procedure_scan":
                self._rows = self._store.procedures_matching(self._scope, params)
            case "procedure_hydration":
                self._rows = self._store.procedures_by_ids(self._scope, params)
            case "schema_scan":
                self._rows = self._store.schema_matching(self._scope, params)
            case "schema_hydration":
                self._rows = self._store.schema_by_ids(self._scope, params)
            case "graph_edges":
                self._rows = self._store.edges(self._scope, params)
            case "candidate_fates":
                # AI-157 : ce que la marche ECRIT. Le faux ne le stocke pas -- il
                # en garde la trace, pour qu'un test puisse le PROUVER.
                self._written.append(" ".join(str(sql).split()))
                self._rows = _fates_upserted(params)
            case "semantic_concepts" | "field_dictionary":
                # Le catalogue gouverne, VIDE et pas absent : ce magasin ne
                # declare ni Concept ni champ de dictionnaire, donc le tag
                # `media_cost` ressort `unresolved` -- ce que le produit publie
                # deja aujourd'hui.
                self._rows = []
            case _:  # pragma: no cover -- a name in the inventory, unanswered
                raise _STATEMENTS.unknown(sql)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, store: "_Store", scope_project: str) -> None:
        self._store = store
        self._scope = scope_project
        #: Les instructions que la marche a EMISES sans les lire -- l'ecriture de
        #: `record_fates`. Gardees pour qu'un test puisse prouver que le sort des
        #: ecartes part vraiment, plutot que de le supposer.
        self.written: list[str] = []

    def cursor(self):
        return _FakeCursor(self._store, self._scope, self.written)

    def transaction(self):
        """Le point de reprise que `record_fates` prend (AI-157).

        Ce faux l'ignorait, et la marche entiere rendait VIDE : `main.py` avale
        toute exception de la recherche et repond « contexte indisponible ». Un
        faux qui ne connait pas un appel emis par le code qu'il double ne prouve
        pas moins -- il prouve FAUX.
        """
        return contextlib.nullcontext()

    def commit(self):
        pass

    def rollback(self):
        pass


def _extract_term(params: tuple | list) -> str:
    for p in params:
        if isinstance(p, str) and p.startswith("%") and p.endswith("%"):
            return p.strip("%").lower()
    return str(params[1]).strip("%").lower() if len(params) > 1 else ""


class _Store:
    """In-memory corpus. topics/procedures: (id, project_id, ...). schema: project-scoped."""

    def __init__(self):
        # topic: id, project_id, title, body_md
        self.topics = [
            ("top_roas", None, "ROAS", "Return on ad spend definition (platform).", "active"),
            ("top_cpa", "projA", "CPA", "Cost per acquisition for projA.", "active"),
            ("top_neighbor", "projA", "Budget pacing", "How pacing relates to ROAS.", "active"),
            ("top_desc", "projA", "Efficiency notes",
             "Notes mentioning ROAS in the body.", "active"),
            ("top_other", "projB", "Secret projB topic", "projB only ROAS notes.", "active"),
            ("top_archived", "projA", "ROAS old", "archived ROAS", "archived"),
        ]
        # procedure: id, project_id, name, description, frontmatter, body, status
        self.procedures = [
            ("proc_ra", None, "roas_analysis",
             "Analyse le ROAS",
             "name: roas_analysis\ndescription: Analyse le ROAS\n"
             "mdm_tags:\n  - media_cost",
             "1. compute roas\n2. compare", "active"),
            ("proc_pa", "projA", "projA_playbook",
             "Playbook projA", "name: projA_playbook\ndescription: Playbook projA",
             "do projA things", "active"),
            ("proc_pb", "projB", "projB_secret",
             "Secret projB", "name: projB_secret\ndescription: Secret projB",
             "projB body", "active"),
        ]
        # schema doc: id, project_id, relation, doc_kind, body_md
        self.schema = [
            ("sctx_a", "projA", "fact_daily_kpi", "columns", "roas column etc"),
            ("sctx_b", "projB", "fact_secret", "columns", "projB roas column"),
        ]
        # graph edge: from_id, from_type, to_id, to_type, project_id
        self.edges_data = [
            ("top_roas", "topic", "top_neighbor", "topic", None),
        ]

    def _visible_topics(self, scope):
        return [t for t in self.topics if t[4] == "active" and (t[1] is None or t[1] == scope)]

    def topics_matching(self, scope, params):
        term = _extract_term(params)
        out = []
        for tid, pid, title, body, status in self._visible_topics(scope):
            if term in (title or "").lower() or term in (body or "").lower():
                out.append((tid, pid, title, body))
        return out

    def topics_by_ids(self, scope, params):
        ids = set(params[1])
        return [
            (t[0], t[1], t[2], t[3])
            for t in self._visible_topics(scope)
            if t[0] in ids
        ]

    def _visible_procs(self, scope):
        return [p for p in self.procedures if p[6] == "active" and (p[1] is None or p[1] == scope)]

    def procedures_matching(self, scope, params):
        """⚠️ SIX colonnes, et le frontmatter est COMPARE.

        Ce faux en rendait cinq et ne comparait pas `frontmatter_yaml`, alors que
        `search_context_walk` selectionne les six depuis AI-154 (`7dbb64a2`) et
        les depaquete. Consequence mesuree le 2026-08-04 : la marche levait
        `not enough values to unpack (expected 6, got 5)`, `main.py` avalait
        l'exception, et l'outil repondait « contexte indisponible » -- deux tests
        de portee (AD-5) rouges depuis ce commit.

        La lecon est celle de tout ce fichier : un faux qui ne connait pas une
        requete emise par le code qu'il double ne prouve pas moins, il prouve
        faux.
        """
        term = _extract_term(params)
        out = []
        for pid_k, pid, name, desc, fm, body, status in self._visible_procs(scope):
            if (term in name.lower() or term in (desc or "").lower()
                    or term in (body or "").lower() or term in (fm or "").lower()):
                out.append((pid_k, pid, name, desc, body, fm))
        return out

    def procedures_by_ids(self, scope, params):
        """CINQ colonnes : `_hydrate_neighbours` relit le frontmatter.

        Le voisin d'une Skill est re-soumis a ses propres
        anti-declencheurs (context_search.py:1241), donc la requete de
        l'hydratation selectionne `frontmatter_yaml` et PAS `body_md`.
        Ce faux en rendait quatre -- la forme de l'autre lecture --
        et le produit en depaquete cinq.
        """
        ids = set(params[1])
        return [
            (p[0], p[1], p[2], p[3], p[4])
            for p in self._visible_procs(scope)
            if p[0] in ids
        ]

    def procedure_by_name(self, scope, params):
        # params: (name, project_id)
        name, proj = params[0], params[1]
        cands = [
            p for p in self.procedures
            if p[6] == "active" and p[2] == name and (p[1] is None or p[1] == proj)
        ]
        # Prefer project-scoped over platform (project_id NOT NULL first).
        cands.sort(key=lambda p: p[1] is None)
        if not cands:
            return []
        p = cands[0]
        return [(p[0], p[1], p[2], p[3], p[4], p[5], p[6], 1)]

    def schema_matching(self, scope, params):
        proj = params[0]
        term = _extract_term(params)
        out = []
        for sid, pid, rel, kind, body in self.schema:
            if pid != proj:
                continue
            if term in rel.lower() or term in (body or "").lower():
                out.append((sid, pid, rel, kind, body))
        return out

    def schema_by_ids(self, scope, params):
        proj, ids = params[0], set(params[1])
        return [
            (s[0], s[1], s[2], s[3], s[4])
            for s in self.schema
            if s[1] == proj and s[0] in ids
        ]

    def edges(self, scope, params):
        # params: (project_id, matched_ids, matched_ids)
        proj, matched = params[0], set(params[1])
        out = []
        for f_id, f_t, t_id, t_t, pid in self.edges_data:
            if pid is not None and pid != proj:
                continue
            if f_id in matched or t_id in matched:
                out.append((f_id, f_t, t_id, t_t))
        return out


def _fake_get_connection(store: _Store, scope: str):
    @contextmanager
    def _cm():
        yield _FakeConn(store, scope)

    return _cm


def _text(result):
    blocks = [c for c in (result.content or []) if getattr(c, "text", None)]
    return blocks[0].text if blocks else ""


def test_the_fake_refuses_a_statement_it_was_never_taught():
    """AI-317: an unrecognised statement NAMES itself instead of answering none.

    The `else` this replaces set `self._rows = []`, and no reader of this seam
    can tell that apart from a true absence: `search_context` would have
    reported "No context found", `get_procedure` its stable not-found -- both
    of them assertions this file makes on purpose. A read added to the walk, or
    a projection reordered in `core/context_search.py`, has to be able to fail
    LOUDLY here, naming the query that moved.
    """
    cursor = _FakeCursor(_Store(), "projA")
    with pytest.raises(UnknownStatement) as raised:
        cursor.execute(
            "SELECT id, project_id, title, body_md FROM app.context_notes "
            "WHERE status = 'active' AND project_id = %s ORDER BY id"
        )
    message = str(raised.value)
    # The statement that moved, and the inventory to compare it against.
    assert "app.context_notes" in message
    assert "topic_scan" in message

    # The two reads of `app.context_topics` really are told apart -- the scan
    # and the by-id hydration of the graph neighbours project the same four
    # columns, so only their WHERE separates them.
    assert _STATEMENTS.find(
        "SELECT id, project_id, title, body_md FROM app.context_topics"
        " WHERE status = 'active' AND (project_id IS NULL OR project_id = %s)"
        " AND id = ANY(%s)"
    ) == "topic_hydration"


# ---------------------------------------------------------------------------
# search_context -- DB-less MCP seam
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_search_context_ranking_order_title_then_desc_then_neighbor():
    """AI-56: ranking title > description > graph-neighbor, asserted by VALUE."""
    store = _Store()
    with patch("core.main._resolve_project", return_value="projA"), patch(
        "core.db.get_connection", _fake_get_connection(store, "projA")
    ):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "search_context", {"query": "ROAS", "project_id": "projA"}
            )

    assert not result.is_error, f"search_context errored: {result}"
    envelope = result.structured_content or result.data
    results = envelope["data"]["results"]
    ids = [r["id"] for r in results]

    # Direct TITLE hit (top_roas, title == 'ROAS') outranks DESCRIPTION body hits,
    # which outrank the pure graph NEIGHBOR (top_neighbor -- linked, no lexical hit
    # for its title 'Budget pacing' but its body mentions ROAS so it IS a desc hit).
    tiers = {r["id"]: r["tier"] for r in results}
    assert tiers["top_roas"] == "title"
    # top_cpa title 'CPA' has no ROAS -> only present if body matches; it does not.
    assert "top_cpa" not in ids
    # A body-only match is 'description' tier.
    assert tiers.get("top_desc") == "description"
    # Scores strictly non-increasing (sorted best-first).
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)
    # Title-tier score strictly greater than any description-tier score present.
    title_scores = [r["score"] for r in results if r["tier"] == "title"]
    desc_scores = [r["score"] for r in results if r["tier"] == "description"]
    if title_scores and desc_scores:
        assert min(title_scores) > max(desc_scores)


@pytest.mark.anyio
async def test_search_context_finds_procedure_by_mdm_tags():
    """AI-154: search_context finds procedure matching mdm_tags in frontmatter_yaml."""
    store = _Store()
    with patch("core.main._resolve_project", return_value="projA"), patch(
        "core.db.get_connection", _fake_get_connection(store, "projA")
    ):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "search_context", {"query": "media_cost", "project_id": "projA"}
            )

    assert not result.is_error, f"search_context errored: {result}"
    envelope = result.structured_content or result.data
    results = envelope["data"]["results"]
    assert len(results) == 1
    hit = results[0]
    assert hit["id"] == "proc_ra"
    assert hit["kind"] == "procedure"
    assert hit["tier"] == "description"
    assert "media_cost" in hit["snippet"]


@pytest.mark.anyio
async def test_search_context_graph_neighbor_surfaced_at_lowest_tier():
    """A pure graph neighbour (no lexical match) surfaces at the 'neighbor' tier."""
    store = _Store()
    # Make top_neighbor NOT match lexically: replace its body so 'pacing' is the term.
    store.topics = [
        ("top_seed", "projA", "Pacing seed", "term pacing here", "active"),
        ("top_link", "projA", "Linked concept", "no lexical term", "active"),
    ]
    store.procedures = []
    store.schema = []
    store.edges_data = [("top_seed", "topic", "top_link", "topic", "projA")]

    with patch("core.main._resolve_project", return_value="projA"), patch(
        "core.db.get_connection", _fake_get_connection(store, "projA")
    ):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "search_context", {"query": "pacing", "project_id": "projA"}
            )

    envelope = result.structured_content or result.data
    by_id = {r["id"]: r for r in envelope["data"]["results"]}
    assert by_id["top_seed"]["tier"] in ("title", "description")
    assert by_id["top_link"]["tier"] == "neighbor"
    assert by_id["top_link"]["score"] < by_id["top_seed"]["score"]


@pytest.mark.anyio
async def test_search_context_summary_at_most_30_lines():
    """AD-1: the LLM summary is <= 30 LINES even with many hits."""
    store = _Store()
    # Seed 100 matching topics -> forces truncation; summary must stay <= 30 lines.
    store.topics = [
        (f"top_{i}", "projA", f"ROAS metric {i}", "body ROAS", "active") for i in range(100)
    ]
    store.procedures = []
    store.schema = []
    store.edges_data = []

    with patch("core.main._resolve_project", return_value="projA"), patch(
        "core.db.get_connection", _fake_get_connection(store, "projA")
    ):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "search_context", {"query": "ROAS", "project_id": "projA"}
            )

    summary = _text(result)
    n_lines = len(summary.splitlines())
    assert n_lines <= 30, f"summary has {n_lines} lines, must be <= 30"
    # And the full detail (not truncated to the summary) is on structuredContent.
    envelope = result.structured_content or result.data
    assert envelope["data"]["count"] >= 1
    assert len(envelope["data"]["results"]) == envelope["data"]["count"]


@pytest.mark.anyio
async def test_search_context_summary_cap_holds_with_newline_in_title():
    """[title cap] A title containing embedded newlines must NOT expand one ranked
    entry into many physical lines and break the AD-1 <=30-line summary cap.

    The summary line interpolates the title; without sanitisation a single hit whose
    title has newlines would emit multiple physical lines. Seed a handful of such
    topics and assert the whole summary stays <= 30 lines.
    """
    store = _Store()
    # Titles with embedded newlines (each would blow up the summary if raw). Bodies
    # also match 'ROAS' so the rows surface.
    store.topics = [
        (f"top_nl_{i}", "projA",
         f"ROAS line-a {i}\nline-b\nline-c\nline-d", "body ROAS metric", "active")
        for i in range(10)
    ]
    store.procedures = []
    store.schema = []
    store.edges_data = []

    with patch("core.main._resolve_project", return_value="projA"), patch(
        "core.db.get_connection", _fake_get_connection(store, "projA")
    ):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "search_context", {"query": "ROAS", "project_id": "projA"}
            )

    summary = _text(result)
    n_lines = len(summary.splitlines())
    assert n_lines <= 30, f"newline-laden titles broke the cap: {n_lines} lines"
    # The title text is collapsed onto ONE line: the raw newline sequence is gone but
    # the words survive (whitespace-collapsed).
    assert "line-a" in summary and "line-b" in summary
    assert "ROAS line-a 0 line-b line-c line-d" in summary


@pytest.mark.anyio
async def test_search_context_scoping_projA_cannot_see_projB():
    """AD-5: projA sees its own + platform rows, never projB's rows."""
    store = _Store()
    with patch("core.main._resolve_project", return_value="projA"), patch(
        "core.db.get_connection", _fake_get_connection(store, "projA")
    ):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "search_context", {"query": "ROAS", "project_id": "projA"}
            )

    envelope = result.structured_content or result.data
    ids = {r["id"] for r in envelope["data"]["results"]}
    # projA sees platform (top_roas) + its own (top_desc/top_neighbor/schema).
    assert "top_roas" in ids
    # NEVER projB's rows.
    assert "top_other" not in ids
    assert "sctx_b" not in ids
    projects = {r["project_id"] for r in envelope["data"]["results"]}
    assert "projB" not in projects


@pytest.mark.anyio
async def test_search_context_indexes_schema_docs():
    """A schema relation term surfaces its schema_context doc (Story 11.5)."""
    store = _Store()
    with patch("core.main._resolve_project", return_value="projA"), patch(
        "core.db.get_connection", _fake_get_connection(store, "projA")
    ):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "search_context",
                {"query": "fact_daily_kpi", "project_id": "projA"},
            )

    envelope = result.structured_content or result.data
    hits = envelope["data"]["results"]
    schema_hits = [r for r in hits if r["kind"] == "schema_doc"]
    assert schema_hits, "schema_context doc not surfaced by its relation name"
    assert schema_hits[0]["tier"] == "title"


@pytest.mark.anyio
async def test_search_context_schema_doc_surfaces_as_graph_neighbor():
    """[F-6] A schema_doc-typed context_graph edge (allowed by migration 031/034)
    surfaces the linked schema doc as a NEIGHBOR hit.

    A topic matches the query lexically; a schema_context doc that does NOT match
    lexically is one graph hop away (edge to_type='schema_doc'). It must appear at
    the 'neighbor' tier, proving the hydration keys off the 'schema_doc' type string
    exactly as the migration enum declares (from_type/to_type CHECK includes it).
    """
    store = _Store()
    # A topic that matches 'pacing', and a schema doc that does NOT match 'pacing'.
    store.topics = [
        ("top_seed", "projA", "Pacing seed", "term pacing here", "active"),
    ]
    store.procedures = []
    store.schema = [
        ("sctx_link", "projA", "fact_budget", "columns", "no lexical term here"),
    ]
    # Graph edge from the matched topic to the schema doc (to_type='schema_doc').
    store.edges_data = [
        ("top_seed", "topic", "sctx_link", "schema_doc", "projA"),
    ]

    with patch("core.main._resolve_project", return_value="projA"), patch(
        "core.db.get_connection", _fake_get_connection(store, "projA")
    ):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "search_context", {"query": "pacing", "project_id": "projA"}
            )

    envelope = result.structured_content or result.data
    by_id = {r["id"]: r for r in envelope["data"]["results"]}
    assert "sctx_link" in by_id, "schema_doc neighbor not surfaced via context_graph"
    assert by_id["sctx_link"]["kind"] == "schema_doc"
    assert by_id["sctx_link"]["tier"] == "neighbor"
    # A pure neighbor never outranks the direct lexical match.
    assert by_id["sctx_link"]["score"] < by_id["top_seed"]["score"]


# ---------------------------------------------------------------------------
# get_procedure -- DB-less MCP seam
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_procedure_by_name_returns_frontmatter_and_body():
    """get_procedure(name) returns the right procedure -- frontmatter + body in detail."""
    store = _Store()
    with patch("core.main._resolve_project", return_value="projA"), patch(
        "core.db.get_connection", _fake_get_connection(store, "projA")
    ):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "get_procedure", {"name": "roas_analysis", "project_id": "projA"}
            )

    assert not result.is_error
    envelope = result.structured_content or result.data
    assert envelope["data"]["found"] is True
    proc = envelope["data"]["procedure"]
    assert proc["name"] == "roas_analysis"
    assert "description:" in proc["frontmatter_yaml"]
    assert proc["body_md"].strip()
    # LLM channel cites the procedure by name.
    assert "roas_analysis" in _text(result)


@pytest.mark.anyio
async def test_get_procedure_not_found_is_stable_and_non_disclosing():
    """Not-found returns found=False with a stable, non-disclosing message (AD-5)."""
    store = _Store()
    with patch("core.main._resolve_project", return_value="projA"), patch(
        "core.db.get_connection", _fake_get_connection(store, "projA")
    ):
        async with Client(FastMCPTransport(mcp)) as client:
            # projB_secret belongs to projB; projA must NOT be told it exists.
            result = await client.call_tool(
                "get_procedure", {"name": "projB_secret", "project_id": "projA"}
            )

    assert not result.is_error
    envelope = result.structured_content or result.data
    assert envelope["data"]["found"] is False
    assert envelope["data"]["procedure"] is None
    # Same wording as a truly-unknown name (no scope leak).
    # AD-34 is ratified (SPEC.md:159, directive Jean 2026-07-24): all visible
    # application copy is English. The product copy was translated in `d3ea695d`
    # / `43e7c57c`; this assertion kept matching the French it replaced. The
    # PRODUCT is right and the test was wrong -- same shape as AI-103.
    #
    # The invariant this test guards is NON-DISCLOSURE, not vocabulary: the
    # refusal must name the id the caller already supplied and reveal nothing
    # about project B. Asserted on that, plus the English wording.
    assert "not found" in _text(result).lower()
    assert "introuvable" not in _text(result).lower()


@pytest.mark.anyio
async def test_search_context_and_get_procedure_are_core_unnamespaced():
    """AD-2: both tools are core-owned and appear un-namespaced in tools/list."""
    async with Client(FastMCPTransport(mcp)) as client:
        tools = await client.list_tools()
    names = {t.name for t in tools}
    assert "search_context" in names
    assert "get_procedure" in names
    # No module prefix (e.g. 'gsc_search_context') -- core owns these.
    assert not any(n.endswith("_search_context") and n != "search_context" for n in names)


# ---------------------------------------------------------------------------
# live_postgres multi-project seams (AI-45/AI-56) -- real 031 schema + SQL.
# ---------------------------------------------------------------------------


@contextmanager
def _seeded_live(conn):
    """Load 031 + seed platform/projA/projB context rows, PUIS les reprendre.

    Ces lignes ne peuvent pas etre laissees derriere : deux d'entre elles sont
    au scope PLATEFORME (project_id NULL), donc visibles depuis tous les projets
    de tous les clients -- 14 executions avaient rempli le mindmap d'un projet
    neuf de 15 noeuds `roas_analysis_<hex>`. Le commit est indispensable (les
    seams lisent dans une autre transaction), donc le nettoyage est explicite et
    passe par un ``finally`` : un test qui echoue nettoie comme un test qui
    passe.
    """
    suffix = _seed_live(conn)
    try:
        yield suffix
    finally:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app.context_topics WHERE id = ANY(%s)",
                ([f"top_plat_{suffix}", f"top_a_{suffix}", f"top_b_{suffix}"],),
            )
            cur.execute(
                "DELETE FROM app.procedures WHERE id = ANY(%s)",
                ([f"proc_plat_{suffix}", f"proc_b_{suffix}"],),
            )
        conn.commit()


def _seed_live(conn) -> str:
    """Load 031 + seed platform/projA/projB context rows. Renvoie le suffixe.

    Appeler via ``_seeded_live`` : appele nu, il laisse ses lignes en base.
    """
    with conn.cursor() as cur:
        cur.execute(MIGRATION.read_text(encoding="utf-8"))
        suffix = uuid.uuid4().hex[:8]
        # Platform ROAS topic (visible to all).
        cur.execute(
            "INSERT INTO app.context_topics (id, project_id, title, body_md, status, created_by)"
            " VALUES (%s, NULL, 'ROAS', 'platform roas def', 'active', 't')",
            (f"top_plat_{suffix}",),
        )
        # projA topic + projB topic (same term, different scope).
        cur.execute(
            "INSERT INTO app.context_topics (id, project_id, title, body_md, status, created_by)"
            " VALUES (%s, 'projA_live', 'CPA roas', 'projA roas note', 'active', 't')",
            (f"top_a_{suffix}",),
        )
        cur.execute(
            "INSERT INTO app.context_topics (id, project_id, title, body_md, status, created_by)"
            " VALUES (%s, 'projB_live', 'ROAS secret', 'projB roas note', 'active', 't')",
            (f"top_b_{suffix}",),
        )
        # Platform procedure citable by name.
        cur.execute(
            "INSERT INTO app.procedures"
            " (id, project_id, name, description, frontmatter_yaml, body_md, status, created_by)"
            " VALUES (%s, NULL, %s, 'Analyse le ROAS',"
            " 'name: roas_analysis\ndescription: Analyse le ROAS', 'body steps', 'active', 't')",
            (f"proc_plat_{suffix}", f"roas_analysis_{suffix}"),
        )
        # projB-only procedure (must be invisible to projA by name).
        cur.execute(
            "INSERT INTO app.procedures"
            " (id, project_id, name, description, frontmatter_yaml, body_md, status, created_by)"
            " VALUES (%s, 'projB_live', %s, 'projB', 'name: pb\ndescription: projB',"
            " 'pb body', 'active', 't')",
            (f"proc_b_{suffix}", f"projB_only_{suffix}"),
        )
    conn.commit()
    return suffix


@requires_postgres
def test_live_search_context_scoping_and_ranking(live_postgres):
    """AD-5 + ranking against real SQL: projA sees platform + projA, never projB."""
    from core import context_search

    conn = live_postgres
    with _seeded_live(conn) as suffix:
        hits = context_search.search_context(conn, query="roas", project_id="projA_live")
        ids = {h.id for h in hits}
        assert f"top_plat_{suffix}" in ids  # platform visible
        assert f"top_a_{suffix}" in ids  # own project visible
        assert f"top_b_{suffix}" not in ids  # projB NEVER visible (AD-5)
        # Ranking: the title-exact platform 'ROAS' outranks projA's body/desc hit.
        scores = [h.score for h in hits]
        assert scores == sorted(scores, reverse=True)


@requires_postgres
def test_live_get_procedure_by_name_scoping(live_postgres):
    """get_procedure by name: platform resolves for projA; projB-only stays hidden."""
    from core import context_search

    conn = live_postgres
    with _seeded_live(conn) as suffix:
        found = context_search.get_procedure_by_name(
            conn, name=f"roas_analysis_{suffix}", project_id="projA_live"
        )
        assert found is not None
        assert found["name"] == f"roas_analysis_{suffix}"
        assert found["project_id"] is None  # platform scope

        hidden = context_search.get_procedure_by_name(
            conn, name=f"projB_only_{suffix}", project_id="projA_live"
        )
        assert hidden is None  # AD-5: projB procedure invisible to projA


# ---------------------------------------------------------------------------
# AI-158 -- ce qu'on REPROCHE a une Skill part avec elle, jusqu'a l'agent
#
# `context-hub.md` : « whatever the MCP tool returns for a Skill, the console
# shows for that Skill ». La console montre les remarques ouvertes ; l'agent
# etait le seul lecteur a qui l'on cachait « cette contrainte n'existe plus »
# -- et c'est LUI qui applique la procedure.
#
# Ces tests patchent la file et l'acces PLUTOT que d'etendre le faux magasin
# SQL : ce qui est prouve ici est le CABLAGE (la file est lue, avec le bon
# node_id, et elle ressort dans l'enveloppe), pas le SQL de `list_open`, qui a
# ses propres tests dans tests/core/test_context_review_proposals.py.
# ---------------------------------------------------------------------------


class _Access:
    def __init__(self, allowed=True, org_id="org_1"):
        self.allowed = allowed
        self.org_id = org_id


def _remark(rid="crr_1", *, note="This constraint no longer exists.", version=1,
            origin="human", by="reader@example.com"):
    return {
        "id": rid, "note": note, "node_version": version, "origin": origin,
        "requested_by": by,
    }


@contextmanager
def _with_remarks(rows, *, access=None, captured=None):
    """Patch la file + l'acces projet, en capturant les arguments de lecture."""
    def _list_open(_conn, *, org_id, project_id, node_id=None):
        if captured is not None:
            captured.update({"org_id": org_id, "project_id": project_id, "node_id": node_id})
        return list(rows)

    with patch("core.context_review.list_open", _list_open), patch(
        "core.project_access.resolve_strict_resource_access",
        lambda *a, **k: access if access is not None else _Access(),
    ):
        yield


async def _call_get_procedure(store, name="roas_analysis"):
    with patch("core.main._resolve_project", return_value="projA"), patch(
        "core.db.get_connection", _fake_get_connection(store, "projA")
    ):
        async with Client(FastMCPTransport(mcp)) as client:
            return await client.call_tool(
                "get_procedure", {"name": name, "project_id": "projA"}
            )


@pytest.mark.anyio
async def test_get_procedure_carries_the_open_remarks_of_that_skill():
    """L'agent lit ce qu'on reproche a la Skill qu'il s'apprete a appliquer."""
    captured: dict = {}
    with _with_remarks([_remark()], captured=captured):
        result = await _call_get_procedure(_Store())

    assert not result.is_error
    proc = (result.structured_content or result.data)["data"]["procedure"]
    assert [r["note"] for r in proc["open_remarks"]] == [
        "This constraint no longer exists."
    ]
    # Scopee A CETTE Skill : `proc_ra` est l'id de `roas_analysis` dans le
    # magasin. Une file lue sans node_id rendrait celle du projet entier.
    assert captured["node_id"] == "proc_ra"
    assert captured["project_id"] == "projA"


@pytest.mark.anyio
async def test_get_procedure_warns_in_the_llm_channel_not_only_in_the_detail():
    """Un avertissement que seul structuredContent porte peut n'etre jamais lu.

    Le canal LLM reste BORNE (AD-1) : un COMPTE, jamais le corpus des notes.
    """
    with _with_remarks([_remark("crr_1"), _remark("crr_2", version=1)]):
        result = await _call_get_procedure(_Store())

    text = _text(result)
    assert "2 open remark(s)" in text
    # Borne : la note elle-meme ne remonte pas dans le canal.
    assert "This constraint no longer exists." not in text


@pytest.mark.anyio
async def test_get_procedure_marks_a_remark_speaking_of_an_older_version():
    """« Cette contrainte n'existe plus » ne se lit pas pareil selon la version.

    L'agent ne peut pas deduire seul que le noeud a avance depuis : on le dit.
    """
    with _with_remarks([_remark(version=999)]):
        result = await _call_get_procedure(_Store())

    proc = (result.structured_content or result.data)["data"]["procedure"]
    assert proc["open_remarks"][0]["speaks_of_current_version"] is False
    assert "of an older version" in _text(result)


@pytest.mark.anyio
async def test_get_procedure_says_nothing_extra_when_the_queue_is_empty():
    """Pas de file, pas d'avertissement -- un ⚠ permanent ne veut plus rien dire."""
    with _with_remarks([]):
        result = await _call_get_procedure(_Store())

    proc = (result.structured_content or result.data)["data"]["procedure"]
    assert proc["open_remarks"] == []
    assert "open remark" not in _text(result)


@pytest.mark.anyio
async def test_get_procedure_hides_the_queue_from_a_caller_without_view_access():
    """La file suit les droits du projet, elle ne les contourne pas."""
    with _with_remarks([_remark()], access=_Access(allowed=False, org_id=None)):
        result = await _call_get_procedure(_Store())

    proc = (result.structured_content or result.data)["data"]["procedure"]
    assert proc["open_remarks"] == []


@pytest.mark.anyio
async def test_get_procedure_survives_an_unavailable_remark_queue():
    """Une file en panne fait perdre un AVERTISSEMENT, jamais l'outil.

    Une Skill qu'on ne peut plus lire parce que sa file est tombee serait un
    couplage bien pire que le manque qu'AI-158 repare.
    """
    def _boom(*_a, **_k):
        raise RuntimeError("review queue unavailable")

    with patch("core.context_review.list_open", _boom), patch(
        "core.project_access.resolve_strict_resource_access", lambda *a, **k: _Access()
    ):
        result = await _call_get_procedure(_Store())

    assert not result.is_error
    proc = (result.structured_content or result.data)["data"]["procedure"]
    assert proc["name"] == "roas_analysis"
    assert proc["body_md"].strip()
    assert proc["open_remarks"] == []
