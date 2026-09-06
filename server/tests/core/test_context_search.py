"""The walk reports the branches it took AND the ones it dropped (Story 54.2).

Scope of this file: Half A of Story 54.2 -- ``core.context_search`` only, no MCP
layer. The seam tests for the tool envelope live in
``tests/integration/test_context_search_seams.py`` and stay untouched, which is
itself part of the acceptance: ``search_context``'s return shape, ranking and
scoping must be exactly what they were.

Two instruments, on purpose. The fake connection below dispatches on the same
stable SQL fragments as the integration seam and applies the AD-5 scope
predicates itself -- fast, and a scoping regression shows up as a leaked row
rather than as a green test over a fake that ignores scope. But a fake that
implements the predicate can only ever prove the fake, so the last two tests
re-run the same two claims through real PostgreSQL (skipped without
``TEST_POSTGRES_DSN``).

What is proven:

* AC3 -- the candidates ranked past ``limit`` survive, in ONE pass (the number of
  SQL statements is asserted, so "richer reporting" cannot become "search twice");
* AC3 -- ranking, scoping and the existing return shape are unchanged;
* AC4 -- a node that is relevant but lexically unreachable is NOT reported as
  rejected, and the payload says never-reached nodes are not enumerated;
* AC5 -- the mode (lexical), the depth (one graph hop) and the cap travel as
  fields of the payload.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from contextlib import contextmanager

import pytest
from core import candidate_fate, context_search

from tests.conftest import purge_fixture_project

requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Postgres proof skipped",
)

# ---------------------------------------------------------------------------
# A SQL-dispatching fake connection (no DB, no MCP).
# ---------------------------------------------------------------------------


class _Store:
    """In-memory corpus. Rows carry their scope so the fake can apply AD-5."""

    def __init__(self) -> None:
        # (id, project_id, title, body_md, status)
        self.topics: list[tuple] = []
        # (id, project_id, name, description, frontmatter, body, status)
        self.procedures: list[tuple] = []
        # (id, project_id, relation, doc_kind, body_md)
        self.schema: list[tuple] = []
        # (from_id, from_type, to_id, to_type, project_id)
        self.edges: list[tuple] = []

    # -- scope helpers (mirror the SQL predicates exactly) ------------------
    @staticmethod
    def _in_scope(row_project: str | None, scope: str | None) -> bool:
        return row_project is None or row_project == scope

    @staticmethod
    def _tokens(params) -> list[str]:
        """Les jetons que la vraie clause a mis en parametres -- Story 45.9.

        La requete envoie, par colonne et par jeton, le triplet
        (accents_source, accents_cible, `%jeton%`). Ce faux les relit plutot que
        de les deviner : un faux qui reconstruit la requete a sa facon finit par
        prouver sa propre reconstruction.
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
        """Au moins un jeton dans au moins une valeur, accents replies."""
        from core.context_search import fold

        haystack = " ".join(fold(value) for value in values)
        return any(token in haystack for token in tokens)

    @staticmethod
    def _bound(rows, params):
        """La borne SQL de la clause SUR-ENSEMBLE, appliquee comme la vraie.

        `ORDER BY id LIMIT %s` : le dernier parametre EST la borne. Un faux qui
        rendrait tout laisserait passer une recuperation non bornee sans qu'une
        ligne rougisse -- c'est exactement le trou de 45.9.
        """
        limit = params[-1] if params and isinstance(params[-1], int) else None
        ordered = sorted(rows, key=lambda row: row[0])
        return ordered if limit is None else ordered[:limit]

    def topics_matching(self, params):
        scope, tokens = params[0], self._tokens(params)
        return self._bound([
            (tid, pid, title, body)
            for tid, pid, title, body, status in self.topics
            if status == "active"
            and self._in_scope(pid, scope)
            and self._hits(tokens, title, body)
        ], params)

    def topics_by_ids(self, params):
        scope, ids = params[0], set(params[1])
        return [
            (tid, pid, title, body)
            for tid, pid, title, body, status in self.topics
            if status == "active" and self._in_scope(pid, scope) and tid in ids
        ]

    def procedures_matching(self, params):
        """Les SIX colonnes de la vraie requete, frontmatter compris.

        Ce faux en rendait CINQ et ne comparait pas `frontmatter_yaml`, alors que
        la clause reelle le fait depuis AI-154. Un faux en retard sur la requete
        ne prouve pas moins : il prouve autre chose.
        """
        scope, tokens = params[0], self._tokens(params)
        return self._bound([
            (pk, pid, name, desc, body, fm)
            for pk, pid, name, desc, fm, body, status in self.procedures
            if status == "active"
            and self._in_scope(pid, scope)
            and self._hits(tokens, name, desc, body, fm)
        ], params)

    def procedures_by_ids(self, params):
        """Les CINQ colonnes de la vraie requete de voisinage, frontmatter compris.

        Ce faux en rendait QUATRE, comme la requete d'avant 45.5 -- et c'est
        precisement pourquoi la garde manquante sur le saut de graphe pouvait
        rester verte : un anti-declencheur que le faux ne rend pas est un
        anti-declencheur qu'aucun test ne peut lire.
        """
        scope, ids = params[0], set(params[1])
        return [
            (pk, pid, name, desc, fm)
            for pk, pid, name, desc, fm, _body, status in self.procedures
            if status == "active" and self._in_scope(pid, scope) and pk in ids
        ]

    def schema_matching(self, params):
        scope, tokens = params[0], self._tokens(params)
        return self._bound([
            (sid, pid, relation, kind, body)
            for sid, pid, relation, kind, body in self.schema
            if pid == scope and self._hits(tokens, relation, body)
        ], params)

    def schema_by_ids(self, params):
        scope, ids = params[0], set(params[1])
        return [row for row in self.schema if row[1] == scope and row[0] in ids]

    def edges_touching(self, params):
        scope, matched = params[0], set(params[1])
        return [
            (f_id, f_t, t_id, t_t)
            for f_id, f_t, t_id, t_t, pid in self.edges
            if self._in_scope(pid, scope) and (f_id in matched or t_id in matched)
        ]


class _FakeCursor:
    def __init__(self, store: _Store, statements: list[str]) -> None:
        self._store = store
        self._statements = statements
        self._rows: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def execute(self, sql: str, params=None):
        params = params or ()
        s = " ".join(sql.split())
        self._statements.append(s)
        if "FROM app.context_topics" in s and "id = ANY" not in s:
            self._rows = self._store.topics_matching(params)
        elif "FROM app.procedures" in s and "id = ANY" not in s:
            self._rows = self._store.procedures_matching(params)
        elif "FROM app.schema_context" in s and "id = ANY" not in s:
            self._rows = self._store.schema_matching(params)
        elif "FROM app.context_graph" in s:
            self._rows = self._store.edges_touching(params)
        elif "FROM app.context_topics" in s:
            self._rows = self._store.topics_by_ids(params)
        elif "FROM app.procedures" in s:
            self._rows = self._store.procedures_by_ids(params)
        elif "FROM app.schema_context" in s:
            self._rows = self._store.schema_by_ids(params)
        elif "INSERT INTO app.context_candidate_fates" in s:
            # AI-157 : la marche ECRIT le sort des ecartes. Ce faux l'ignorait --
            # `record_fates` echouait sur `conn.transaction()` absent et rendait
            # 0 en silence, donc les tests ci-dessous comptaient des instructions
            # que le code n'emettait plus vraiment.
            self._rows = []
        else:  # pragma: no cover -- an unexpected statement must be visible
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchall(self):
        return list(self._rows)


class _FakeConn:
    def __init__(self, store: _Store) -> None:
        self.store = store
        self.statements: list[str] = []

    def cursor(self):
        return _FakeCursor(self.store, self.statements)

    def transaction(self):
        """Le point de reprise que `record_fates` prend (AI-157).

        Sans lui, `record_fates` levait un `AttributeError`, l'avalait, et
        rendait 0 -- l'ecriture n'apparaissait dans AUCUNE assertion de ce
        fichier alors que le code la fait. Un faux muet sur un appel reel ne
        prouve pas moins : il prouve faux.
        """
        return contextlib.nullcontext()


# ---------------------------------------------------------------------------
# Corpora
# ---------------------------------------------------------------------------


def _crowded_corpus(n: int = 25, *, scope: str = "projA") -> _Store:
    """*n* topics that all match 'pacing' -- more than DEFAULT_LIMIT on purpose."""
    store = _Store()
    store.topics = [
        (f"top_{i:02d}", scope, f"Pacing note {i:02d}", "body about pacing", "active")
        for i in range(n)
    ]
    return store


def _unreachable_node_corpus() -> _Store:
    """T6 / AC4: one node is RELEVANT to the query and lexically UNREACHABLE.

    * ``top_pacing`` matches 'pacing' by title -- the only direct hit.
    * ``top_alert`` does not match lexically; it is ONE hop away, so the walk
      reaches it as a graph neighbour.
    * ``top_smoothing`` is about the same subject in plain words -- "daily budget
      delivery is spread evenly across the month" IS pacing -- but the term
      appears nowhere in it, and it sits TWO hops from the only match (linked to
      ``top_alert``, not to ``top_pacing``).

    A lexical, one-hop walk cannot reach it. It was therefore never judged, and
    the payload must not claim it was.
    """
    store = _Store()
    store.topics = [
        ("top_pacing", "projA", "Pacing policy",
         "How campaigns spend their budget over time.", "active"),
        ("top_alert", "projA", "Delivery alerts",
         "Alerts raised when delivery drifts.", "active"),
        ("top_smoothing", "projA", "Spend smoothing",
         "Daily budget delivery is spread evenly across the month.", "active"),
    ]
    store.edges = [
        ("top_pacing", "topic", "top_alert", "topic", "projA"),
        ("top_alert", "topic", "top_smoothing", "topic", "projA"),
    ]
    return store


# ---------------------------------------------------------------------------
# AC3 -- what fell below the cap survives, and nothing else changes
# ---------------------------------------------------------------------------


def test_search_context_return_shape_is_unchanged():
    """Existing callers keep getting the survivors, capped, as ContextHit objects."""
    conn = _FakeConn(_crowded_corpus(25))
    hits = context_search.search_context(conn, query="pacing", project_id="projA")

    assert isinstance(hits, list)
    assert len(hits) == context_search.DEFAULT_LIMIT
    assert all(isinstance(h, context_search.ContextHit) for h in hits)
    # The dict a caller serialises is untouched: no fate, no rank, no new key.
    assert set(hits[0].as_dict()) == {
        "id", "kind", "title", "snippet", "score", "tier", "project_id",
    }


def test_the_walk_keeps_the_candidates_that_fell_below_the_cap():
    """AC3: 25 reached, 20 kept, 5 reported as rejected -- not discarded."""
    conn = _FakeConn(_crowded_corpus(25))
    walk = context_search.search_context_walk(conn, query="pacing", project_id="projA")

    assert len(walk.ranked) == 25
    assert len(walk.selected) == context_search.DEFAULT_LIMIT
    assert len(walk.dropped) == 5

    candidates = walk.candidates
    assert len(candidates) == 25
    kept = [c for c in candidates if c.fate == candidate_fate.FATE_SELECTED]
    lost = [c for c in candidates if c.fate == candidate_fate.FATE_REJECTED]
    assert len(kept) == 20
    assert len(lost) == 5
    assert {c.reason for c in lost} == {candidate_fate.REASON_BELOW_CUTOFF}
    assert {c.reason for c in kept} == {None}
    # Ranks are 1-based, contiguous, and the dropped ones carry the tail ranks.
    assert [c.rank for c in candidates] == list(range(1, 26))
    assert [c.rank for c in lost] == [21, 22, 23, 24, 25]


def test_the_walk_does_not_change_the_ranking_or_the_survivors():
    """AC3: reporting is richer; the answer's evidence is byte-for-byte the same."""
    corpus = _crowded_corpus(25)
    from_plain = context_search.search_context(
        _FakeConn(corpus), query="pacing", project_id="projA"
    )
    walk = context_search.search_context_walk(
        _FakeConn(corpus), query="pacing", project_id="projA"
    )

    assert [h.as_dict() for h in walk.selected] == [h.as_dict() for h in from_plain]
    # And the full ranking is monotonically non-increasing, dropped ones included.
    scores = [c.hit.score for c in walk.candidates]
    assert scores == sorted(scores, reverse=True)


def test_one_retrieval_not_two():
    """AC3: 'adding a second search path is a defect'. Measured, not asserted in prose.

    The wrapper and the full report issue the SAME statements, in the same order.
    A second retrieval path would show up here as extra SQL.
    """
    plain_conn = _FakeConn(_crowded_corpus(25))
    walk_conn = _FakeConn(_crowded_corpus(25))
    context_search.search_context(plain_conn, query="pacing", project_id="projA")
    context_search.search_context_walk(walk_conn, query="pacing", project_id="projA")

    assert plain_conn.statements == walk_conn.statements
    # Not vacuous: the pass really did query the corpus.
    assert len(plain_conn.statements) >= 3

    # AI-157 : la marche garde le sort des ecartes, et elle le fait en UNE
    # instruction. La mesure du 2026-08-04 (corpus reel, 80 rejets) : boucle de N
    # `execute` 8.29 ms, instruction unique 2.62 ms -- le facteur 3 est celui des
    # ALLERS-RETOURS, pas de l'ecriture. Un `for` reintroduit ici serait invisible
    # sans ce compte.
    writes = [s for s in walk_conn.statements
              if "INSERT INTO app.context_candidate_fates" in s]
    assert len(writes) == 1


def test_a_dropped_candidate_never_carries_another_projects_row():
    """AD-5 is not negotiable: the scope filter runs BEFORE anything is kept."""
    store = _crowded_corpus(25, scope="projA")
    store.topics += [
        ("top_secret_1", "projB", "Pacing secret 1", "projB pacing notes", "active"),
        ("top_secret_2", "projB", "Pacing secret 2", "projB pacing notes", "active"),
    ]
    walk = context_search.search_context_walk(
        _FakeConn(store), query="pacing", project_id="projA"
    )

    projects = {c.hit.project_id for c in walk.candidates}
    assert projects == {"projA"}
    ids = {c.hit.id for c in walk.candidates}
    assert "top_secret_1" not in ids
    assert "top_secret_2" not in ids
    # Platform rows stay visible -- the filter is scope, not silence.
    store.topics.append(("top_plat", None, "Pacing platform", "platform", "active"))
    walk2 = context_search.search_context_walk(
        _FakeConn(store), query="pacing", project_id="projA"
    )
    assert "top_plat" in {c.hit.id for c in walk2.candidates}


# ---------------------------------------------------------------------------
# AC4 / T6 -- never reached is not rejected
# ---------------------------------------------------------------------------


def test_a_relevant_but_lexically_unreachable_node_is_not_reported_as_rejected():
    """T6, the proof on a fixture: 'not judged' must not be dressed as 'judged'."""
    walk = context_search.search_context_walk(
        _FakeConn(_unreachable_node_corpus()), query="pacing", project_id="projA"
    )

    reached = {c.hit.id for c in walk.candidates}
    rejected = {
        c.hit.id for c in walk.candidates if c.fate == candidate_fate.FATE_REJECTED
    }

    # The walk did work: the direct match and its one-hop neighbour are there.
    assert "top_pacing" in reached
    assert "top_alert" in reached
    # The relevant node two hops away, with no lexical trace of the term, is
    # absent from BOTH lists. It was never examined, so it cannot be a rejection.
    assert "top_smoothing" not in rejected
    assert "top_smoothing" not in reached
    # Nothing anywhere in the payload claims otherwise.
    payload = walk.as_dict()
    assert all(c["id"] != "top_smoothing" for c in payload["candidates"])


def test_the_payload_states_that_unreached_nodes_are_not_enumerated():
    """AC4: the third state is declared, not left to be inferred from an absence."""
    walk = context_search.search_context_walk(
        _FakeConn(_unreachable_node_corpus()), query="pacing", project_id="projA"
    )
    descriptor = walk.retrieval_descriptor()

    assert descriptor["not_reached_enumerated"] is False
    assert context_search.NOT_REACHED_ENUMERATED is False
    # `reached_count` counts what the walk touched, never the corpus.
    assert descriptor["reached_count"] == len(walk.ranked) == 2


def test_the_one_hop_boundary_is_real():
    """The depth the descriptor announces is the depth the walk has."""
    walk = context_search.search_context_walk(
        _FakeConn(_unreachable_node_corpus()), query="pacing", project_id="projA"
    )
    by_id = {c.hit.id: c for c in walk.candidates}

    # One hop from the only direct match: reached, and reached by the graph.
    assert by_id["top_pacing"].hit.matched is True
    assert by_id["top_alert"].hit.matched is False
    # Two hops: not in the walk at all.
    assert "top_smoothing" not in by_id
    assert walk.retrieval_descriptor()["graph_hop_depth"] == 1


def test_a_pure_graph_neighbour_is_reached_but_not_matched():
    """`matched` stays meaningful: reached directly vs reached by the hop."""
    store = _Store()
    store.topics = [
        ("top_seed", "projA", "Pacing seed", "term pacing here", "active"),
        ("top_link", "projA", "Linked concept", "no lexical term", "active"),
    ]
    store.edges = [("top_seed", "topic", "top_link", "topic", "projA")]

    walk = context_search.search_context_walk(
        _FakeConn(store), query="pacing", project_id="projA"
    )
    by_id = {c.hit.id: c for c in walk.candidates}

    assert by_id["top_seed"].hit.matched is True
    assert by_id["top_link"].hit.matched is False
    assert by_id["top_link"].hit.tier == "neighbor"
    assert by_id["top_link"].as_dict()["matched"] is False


# ---------------------------------------------------------------------------
# AC5 -- the mode and the depth are fields
# ---------------------------------------------------------------------------


def test_the_retrieval_descriptor_states_what_the_walk_actually_is():
    """AC5: a surface reading this cannot draw an exploration that did not happen."""
    walk = context_search.search_context_walk(
        _FakeConn(_crowded_corpus(25)), query="pacing", project_id="projA"
    )
    descriptor = walk.retrieval_descriptor()

    assert descriptor["mode"] == "lexical"
    assert descriptor["graph_hop_depth"] == 1
    assert descriptor["semantic_recall"] is False
    assert descriptor["limit"] == context_search.DEFAULT_LIMIT
    assert descriptor["tiers"] == {"title": 3.0, "description": 2.0, "neighbor": 1.0}
    assert descriptor["selected_count"] == 20
    assert descriptor["rejected_count"] == 5
    assert descriptor["reached_count"] == 25
    # The descriptor travels with the candidates, not beside them.
    assert walk.as_dict()["retrieval"] == descriptor


def test_the_descriptor_is_present_even_when_nothing_matched():
    """A blank or fruitless query still says what kind of walk found nothing."""
    empty = context_search.search_context_walk(
        _FakeConn(_crowded_corpus(3)), query="   ", project_id="projA"
    )
    assert empty.candidates == []
    assert empty.selected == []
    assert empty.retrieval_descriptor()["mode"] == "lexical"
    assert empty.retrieval_descriptor()["reached_count"] == 0
    # And the plain tool contract for a blank query is unchanged.
    assert context_search.search_context(
        _FakeConn(_crowded_corpus(3)), query="   ", project_id="projA"
    ) == []


# ---------------------------------------------------------------------------
# The shape a downstream emitter will carry (AC1 groundwork for T3)
# ---------------------------------------------------------------------------


def test_a_candidate_carries_node_score_tier_matched_and_fate():
    walk = context_search.search_context_walk(
        _FakeConn(_crowded_corpus(25)), query="pacing", project_id="projA"
    )
    first = walk.candidates[0].as_dict()

    assert set(first) == {
        "id", "kind", "title", "score", "tier", "matched", "project_id",
        "rank", "fate", "reason",
    }
    assert first["fate"] in candidate_fate.FATES
    assert first["kind"] == "topic"
    assert isinstance(first["score"], float)


def test_a_candidate_is_a_pointer_not_a_second_copy_of_the_content():
    """`context-hub.md:40-50` -- a path is trace evidence, not a content store."""
    walk = context_search.search_context_walk(
        _FakeConn(_crowded_corpus(25)), query="pacing", project_id="projA"
    )
    for candidate in walk.candidates:
        assert "snippet" not in candidate.as_dict()
        assert "body" not in candidate.as_dict()


def test_every_emitted_reason_is_one_the_walk_can_genuinely_produce():
    """The walk may not borrow a reason belonging to another emitter."""
    walk = context_search.search_context_walk(
        _FakeConn(_crowded_corpus(25)), query="pacing", project_id="projA"
    )
    emitted = {c.reason for c in walk.candidates if c.reason is not None}
    assert emitted <= set(candidate_fate.TREE_WALK_REASONS)


def test_out_of_scope_is_never_emitted_by_this_walk():
    """AD-5 filters in SQL, so another project's row is never a REJECTED candidate.

    Reporting it would invent a judgement and leak the row's existence at once.
    """
    store = _crowded_corpus(5, scope="projA")
    store.topics += [("top_secret", "projB", "Pacing secret", "projB", "active")]
    walk = context_search.search_context_walk(
        _FakeConn(store), query="pacing", project_id="projA"
    )
    assert candidate_fate.REASON_OUT_OF_SCOPE not in {c.reason for c in walk.candidates}


# ---------------------------------------------------------------------------
# The same two claims against REAL SQL
#
# The fake above applies the AD-5 predicates itself, so on its own it proves the
# fake and not the query ("mesurer avec un instrument que je pollue"). These two
# run the identical assertions through PostgreSQL: the cap really drops rows the
# same SELECT returned, and the scope filter really runs before anything is kept.
# ---------------------------------------------------------------------------


@contextmanager
def _seeded_walk_corpus(conn):
    """25 in-scope topics + 2 out-of-scope ones, on a term nothing else uses.

    Every row is Project-scoped (never platform) and every row is deleted in a
    ``finally``: a test that fails cleans up like a test that passes. Leaving
    context rows behind is how a neighbouring suite once filled a fresh Project's
    mindmap with 15 phantom nodes.
    """
    suffix = uuid.uuid4().hex[:8]
    term = f"pacingterm{suffix}"
    own = f"proj_54_2_a_{suffix}"
    other = f"proj_54_2_b_{suffix}"
    ids = [f"top_54_2_{suffix}_{i:02d}" for i in range(25)]
    ids += [f"top_54_2_{suffix}_other_{i}" for i in range(2)]

    with conn.cursor() as cur:
        for i in range(25):
            cur.execute(
                "INSERT INTO app.context_topics"
                " (id, project_id, title, body_md, status, created_by)"
                " VALUES (%s, %s, %s, 'body', 'active', 't')",
                (ids[i], own, f"{term} note {i:02d}"),
            )
        for i in range(2):
            cur.execute(
                "INSERT INTO app.context_topics"
                " (id, project_id, title, body_md, status, created_by)"
                " VALUES (%s, %s, %s, 'body', 'active', 't')",
                (ids[25 + i], other, f"{term} secret {i}"),
            )
    conn.commit()
    try:
        yield term, own, other
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.context_topics WHERE id = ANY(%s)", (ids,))
        conn.commit()


@requires_postgres
def test_live_the_cap_drops_rows_the_same_select_returned(live_postgres):
    """AC3 against real SQL: 25 reached, 20 kept, 5 rejected as below_cutoff."""
    conn = live_postgres
    with _seeded_walk_corpus(conn) as (term, own, _other):
        walk = context_search.search_context_walk(conn, query=term, project_id=own)

        assert len(walk.ranked) == 25
        assert len(walk.selected) == context_search.DEFAULT_LIMIT
        lost = [c for c in walk.candidates if c.fate == candidate_fate.FATE_REJECTED]
        assert len(lost) == 5
        assert {c.reason for c in lost} == {candidate_fate.REASON_BELOW_CUTOFF}
        # The survivors are still exactly what the tool returns.
        plain = context_search.search_context(conn, query=term, project_id=own)
        assert [h.id for h in plain] == [h.id for h in walk.selected]


@requires_postgres
def test_live_a_dropped_candidate_is_never_another_projects_row(live_postgres):
    """AD-5 against real SQL, on the branch this story ADDS: the dropped ones."""
    conn = live_postgres
    with _seeded_walk_corpus(conn) as (term, own, other):
        walk = context_search.search_context_walk(conn, query=term, project_id=own)

        assert {c.hit.project_id for c in walk.candidates} == {own}
        assert all(other not in c.hit.id for c in walk.candidates)
        # ...and the other Project sees its own two, never the twenty-five.
        theirs = context_search.search_context_walk(conn, query=term, project_id=other)
        assert {c.hit.project_id for c in theirs.candidates} == {other}
        assert len(theirs.candidates) == 2


# ---------------------------------------------------------------------------
# AI-157 -- le sort des ecartes est GARDE, et il fallait une VRAIE ligne projet
# ---------------------------------------------------------------------------


@contextmanager
def _seeded_real_project(conn, org_id):
    """Un projet qui EXISTE VRAIMENT, sous l'organisation de socle, + 25 Knowledge.

    ⚠️ POURQUOI PAS `_seeded_walk_corpus`. Ses `proj_54_2_a_*` ne sont dans
    AUCUNE ligne de `app.projects` -- `context_topics.project_id` ne porte pas de
    cle etrangere, `context_candidate_fates.project_id` en porte une. Une marche
    sur un projet fantome VOIT donc ses candidats ecartes et n'en ecrit aucun :
    `record_fates` avale l'echec de cle etrangere par construction. Un test ecrit
    dessus serait vert sans rien prouver -- exactement la classe de defaut que
    AI-157 repare.

    ⚠️ ET PAS D'ORGANISATION NEUVE NON PLUS. En creer une declenche son propre
    amorcage (`mdm_business_domains`, entre autres) et la defaire demande alors
    de parcourir l'arbre de cles etrangeres -- c'est le travail de
    `scripts/org_purge.py`, pas d'une fixture. `test_org` existe pour cela, et
    n'est jamais supprimee : c'est une donnee de socle.
    """
    suffix = uuid.uuid4().hex[:8]
    term = f"fateterm{suffix}"
    project_id = f"proj_fate_{suffix}"
    ids = [f"top_fate_{suffix}_{i:02d}" for i in range(25)]

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, status, created_by)"
            " VALUES (%s, %s, 'AI-157 fate fixture', %s, 'active', 't')"
            " ON CONFLICT (id) DO NOTHING",
            (project_id, org_id, f"ai157-fate-{suffix}"),
        )
        for i in range(25):
            cur.execute(
                "INSERT INTO app.context_topics"
                " (id, project_id, title, body_md, status, created_by)"
                " VALUES (%s, %s, %s, 'body', 'active', 't')",
                (ids[i], project_id, f"{term} note {i:02d}"),
            )
    conn.commit()
    try:
        yield term, project_id
    finally:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app.context_candidate_fates WHERE project_id = %s",
                (project_id,),
            )
            # Story 45.8 : franchir le seuil DEPOSE une remarque `origin='agent'`
            # sur le noeud. Sans ce nettoyage la fixture casse sur
            # `context_review_requests_project_id_fkey` -- et elle ne cassait pas
            # avant le 2026-08-10 parce que le producteur levait avant d'ecrire.
            cur.execute(
                "DELETE FROM app.context_review_requests WHERE project_id = %s",
                (project_id,),
            )
            cur.execute("DELETE FROM app.context_topics WHERE id = ANY(%s)", (ids,))
            # `trg_projects_seed_capabilities` pose une ligne de capacites A
            # L'INSERTION du projet. Sans ce DELETE, le nettoyage echoue sur une
            # cle etrangere et le test laisse derriere lui un projet fantome --
            # comment une suite voisine a un jour rempli le mindmap d'un Projet
            # neuf de quinze noeuds.
            cur.execute(
                "DELETE FROM app.project_capabilities WHERE project_id = %s",
                (project_id,),
            )
            # AI-291: le graphe prend le relais si une table gouvernee
            # ajoutee depuis retient le projet en ON DELETE RESTRICT.
            purge_fixture_project(cur.connection, project_id)
        conn.commit()


@requires_postgres
def test_live_the_walk_keeps_the_fate_of_what_it_dropped(live_postgres, test_org):
    """AI-157 : la marche ECRIT le sort des ecartes -- prouve en base.

    Avant le 2026-08-04, `record_fates` existait, etait teste, et AUCUNE
    recherche ne l'appelait : le signal etait calcule a chaque marche et conserve
    a aucune. Ce test tient le branchement, pas la fonction.
    """
    conn = live_postgres
    with _seeded_real_project(conn, test_org) as (term, project_id):
        walk = context_search.search_context_walk(conn, query=term, project_id=project_id)
        conn.commit()  # `search_context_walk` n'engage rien -- c'est voulu.

        dropped = [c for c in walk.candidates if c.fate == candidate_fate.FATE_REJECTED]
        assert len(dropped) == 5

        rows = context_search.recurrent_fates(conn, project_id=project_id, minimum=1)
        assert len(rows) == 5
        assert {r["reason"] for r in rows} == {candidate_fate.REASON_BELOW_CUTOFF}
        assert {r["candidate_id"] for r in rows} == {c.hit.id for c in dropped}
        assert {r["times"] for r in rows} == {1}
        assert {r["last_query"] for r in rows} == {term}


@requires_postgres
def test_live_a_second_walk_counts_rather_than_piles_up(live_postgres, test_org):
    """AGREGAT, PAS JOURNAL -- et c'est la propriete qui borne le volume.

    Ce qui compte est COMBIEN DE FOIS un candidat a ete ecarte, pas quand. Une
    ligne par (projet, candidat, raison) : le volume est borne par la taille du
    corpus, jamais par le trafic. Un journal de chaque rejet de chaque recherche
    serait ingerable et n'apporterait rien de plus.
    """
    conn = live_postgres
    with _seeded_real_project(conn, test_org) as (term, project_id):
        for _ in range(3):
            context_search.search_context_walk(conn, query=term, project_id=project_id)
        conn.commit()

        rows = context_search.recurrent_fates(conn, project_id=project_id, minimum=1)
        assert len(rows) == 5           # cinq lignes, pas quinze
        assert {r["times"] for r in rows} == {3}

        # Et le seuil de `recurrent_fates` est bien un parametre, pas un decor :
        # au-dessus de trois, la liste est vide.
        assert context_search.recurrent_fates(conn, project_id=project_id, minimum=4) == []


@requires_postgres
def test_live_a_failed_fate_write_does_not_poison_the_callers_transaction(
    live_postgres, test_org
):
    """Le point de reprise, et pourquoi « ne leve jamais » ne suffisait pas.

    En psycopg 3 une instruction qui echoue AVORTE la transaction entiere. Un
    projet inexistant fait echouer la cle etrangere de `context_candidate_fates`
    ; sans le `transaction()` de `record_fates`, tout ce que l'appelant tenterait
    ENSUITE echouerait avec « current transaction is aborted » -- une recherche
    casserait le travail d'a cote pour une mesure.
    """
    conn = live_postgres
    with _seeded_real_project(conn, test_org) as (term, project_id):
        walk = context_search.search_context_walk(conn, query=term, project_id=project_id)
        ghost = walk.__class__(
            query=term, project_id=f"proj_does_not_exist_{uuid.uuid4().hex[:8]}",
            limit=walk.limit, ranked=walk.ranked,
        )
        assert context_search.record_fates(conn, ghost) == 0

        # La transaction est TOUJOURS utilisable -- c'est tout l'enjeu.
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone() == (1,)
        conn.commit()


# ---------------------------------------------------------------------------
# Story 45.5 -- une Skill declare quand elle ne doit PAS se declencher
# ---------------------------------------------------------------------------


def _skill_corpus(anti: str | None) -> _Store:
    """Une Skill large qui gagne « facturation » -- avec ou sans anti-declencheur."""
    store = _Store()
    frontmatter = 'name: "broad"\ndescription: "d"\n'
    if anti is not None:
        frontmatter += f'anti_triggers:\n  - "{anti}"\n'
    store.procedures.append((
        "proc_broad", "projA", "Broad reporting skill",
        "Answers anything about facturation and pacing.",
        frontmatter, "body", "active",
    ))
    return store


def test_a_skill_that_declared_the_question_is_not_its_own_is_dropped():
    """Le manque le plus couteux du 2026-08-03 : rien ne pouvait contredire une
    Skill a description large. Elle est atteinte, puis ecartee, et ca se dit."""
    conn = _FakeConn(_skill_corpus("facturation"))
    walk = context_search.search_context_walk(
        conn, query="facturation", project_id="projA"
    )

    assert walk.selected == []
    assert [hit.id for hit in walk.refused] == ["proc_broad"]

    refused = [c for c in walk.candidates if c.reason == candidate_fate.REASON_ANTI_TRIGGER]
    assert len(refused) == 1
    assert refused[0].fate == candidate_fate.FATE_REJECTED
    # Atteint : le compte le dit, et il le dit A PART du plafond.
    descriptor = walk.retrieval_descriptor()
    assert descriptor["reached_count"] == 1
    assert descriptor["refused_count"] == 1
    assert descriptor["selected_count"] == 0


def test_without_the_declaration_the_same_query_still_wins_the_skill():
    """Le controle negatif : sans anti-declencheur, RIEN ne bouge. Les 50 Skills
    ecrites avant cette story rendent exactement ce qu'elles rendaient."""
    conn = _FakeConn(_skill_corpus(None))
    walk = context_search.search_context_walk(
        conn, query="facturation", project_id="projA"
    )

    assert [hit.id for hit in walk.selected] == ["proc_broad"]
    assert walk.refused == []
    assert walk.retrieval_descriptor()["refused_count"] == 0


def test_an_anti_trigger_does_not_exclude_another_question():
    """Un anti-declencheur retire UNE question, pas la Skill."""
    conn = _FakeConn(_skill_corpus("facturation"))
    walk = context_search.search_context_walk(conn, query="pacing", project_id="projA")

    assert [hit.id for hit in walk.selected] == ["proc_broad"]
    assert walk.refused == []


def test_a_declared_drop_never_reaches_the_recurring_rejection_aggregate():
    """L'agregat sert a faire remonter des LIENS MANQUANTS. Un retrait que
    l'auteur a ecrit est l'inverse -- et la CHECK de la table le refuse."""
    conn = _FakeConn(_skill_corpus("facturation"))
    walk = context_search.search_context_walk(
        conn, query="facturation", project_id="projA"
    )

    assert context_search.record_fates(conn, walk) == 0
    assert not [s for s in conn.statements if "INSERT INTO app.context_candidate_fates" in s]


def test_the_anti_trigger_is_read_the_way_the_ranking_matches():
    """Sous-chaine, insensible a la casse -- comme la clause qui a fait entrer le
    candidat. Une regle qui exclurait autrement produirait des retraits que
    personne ne peut predire."""
    front = 'name: "s"\ndescription: "d"\nanti_triggers:\n  - "Facturation"\n'
    assert context_search._anti_triggered(front, "facturation fournisseur") is True
    assert context_search._anti_triggered(front, "FACTURATION") is True
    assert context_search._anti_triggered(front, "pacing") is False
    # Un frontmatter illisible ne fait pas tomber la recherche de tout le monde.
    assert context_search._anti_triggered("name: [unclosed", "facturation") is False
    assert context_search._anti_triggered(None, "facturation") is False


# ---------------------------------------------------------------------------
# Story 45.9 -- la requete est lue comme des mots, et la marche elargit
# ---------------------------------------------------------------------------


def _worded_corpus() -> _Store:
    store = _Store()
    store.topics.append((
        "top_windows", "projA", "Attribution windows per source",
        "How each platform counts a conversion.", "active",
    ))
    store.topics.append((
        "top_screen", "projA", "L'ecran de pilotage",
        "Ce que la personne vient y faire.", "active",
    ))
    store.topics.append((
        "top_attr", "projA", "Attribution", "Post-click only.", "active",
    ))
    return store


def test_the_words_of_a_query_are_compared_and_not_the_whole_phrase() -> None:
    """La mesure du 2026-08-05 : `%attribution window%` ne trouvait QUE ce qui
    contenait cette chaine. Deux mots dans l'autre ordre ne trouvaient rien."""
    conn = _FakeConn(_worded_corpus())
    walk = context_search.search_context_walk(
        conn, query="window attribution", project_id="projA"
    )

    assert "top_windows" in [hit.id for hit in walk.selected]
    descriptor = walk.retrieval_descriptor()
    assert descriptor["tokens"] == ["window", "attribution"]
    assert descriptor["match_mode"] == "all_tokens"


def test_an_accent_is_not_a_different_word() -> None:
    """Repli des DEUX cotes, sans extension : `unaccent` ferait mieux et
    couterait un engagement d'exploitation permanent pour un corpus d'une ligne."""
    conn = _FakeConn(_worded_corpus())
    assert [hit.id for hit in context_search.search_context(
        conn, query="ecran", project_id="projA")] == ["top_screen"]

    conn = _FakeConn(_worded_corpus())
    assert [hit.id for hit in context_search.search_context(
        conn, query="ÉCRAN", project_id="projA")] == ["top_screen"]


def test_a_single_word_query_behaves_exactly_as_before() -> None:
    """LE CONTROLE NEGATIF DE LA STORY. Avec un seul mot, « tous les mots » et
    « au moins un » sont la meme chose : rien ne peut bouger."""
    conn = _FakeConn(_worded_corpus())
    walk = context_search.search_context_walk(conn, query="attribution", project_id="projA")

    assert [hit.id for hit in walk.selected] == ["top_attr", "top_windows"]
    assert [hit.score for hit in walk.selected] == [
        context_search.TIER_TITLE, context_search.TIER_TITLE,
    ]
    assert walk.retrieval_descriptor()["match_mode"] == "all_tokens"
    assert walk.retrieval_descriptor()["widened_count"] == 0


def test_a_partial_match_is_not_the_answer_when_a_complete_one_exists() -> None:
    """« attribution window » ne doit pas rendre tout ce qui parle d'attribution :
    la precision d'abord, sinon elargir ne veut plus rien dire."""
    conn = _FakeConn(_worded_corpus())
    walk = context_search.search_context_walk(
        conn, query="attribution windows", project_id="projA"
    )

    assert [hit.id for hit in walk.selected] == ["top_windows"]
    assert walk.retrieval_descriptor()["widened_count"] == 0


def test_when_nothing_carries_every_word_the_walk_widens_and_says_so() -> None:
    """Et c'est le repli : plutot que « aucun contexte », ce qui porte une partie
    des mots -- annonce comme tel."""
    conn = _FakeConn(_worded_corpus())
    walk = context_search.search_context_walk(
        conn, query="attribution unicorn", project_id="projA"
    )

    descriptor = walk.retrieval_descriptor()
    assert descriptor["match_mode"] == "any_token"
    assert descriptor["widened_count"] == len(walk.ranked) > 0
    assert all(hit.widened for hit in walk.ranked)
    # Un elargi n'entre jamais dans le contrat rendu a l'outil MCP.
    assert set(walk.selected[0].as_dict()) == {
        "id", "kind", "title", "snippet", "score", "tier", "project_id",
    }


def test_a_widened_candidate_never_outranks_a_direct_one() -> None:
    store = _worded_corpus()
    # Un noeud qui ne porte QU'UN des deux mots, avec le meilleur tier possible.
    store.topics.append(("top_win", "projA", "Windows", "Only one word.", "active"))
    conn = _FakeConn(store)

    walk = context_search.search_context_walk(
        conn, query="attribution windows", project_id="projA"
    )
    ids = [hit.id for hit in walk.ranked]
    assert ids[0] == "top_windows"
    assert "top_win" not in ids  # une passe directe a repondu : les partiels sortent


def test_a_query_with_no_comparable_word_finds_nothing_rather_than_everything() -> None:
    """Une chaine vide passee a un `LIKE '%%'` aurait rendu TOUT le corpus."""
    conn = _FakeConn(_worded_corpus())
    for query in ("?", "  ", "a"):
        walk = context_search.search_context_walk(conn, query=query, project_id="projA")
        assert walk.ranked == []
        assert walk.retrieval_descriptor()["tokens"] == []


def test_the_token_count_is_bounded() -> None:
    many = " ".join(f"word{index}" for index in range(40))
    assert len(context_search.tokenise(many)) == context_search.MAX_TOKENS
    # Dedupe, ordre conserve, mots trop courts ecartes.
    assert context_search.tokenise("Attribution, attribution a window!") == [
        "attribution", "window",
    ]


# --- sorties de relecture adversariale du 2026-08-05 -----------------------


def _skill_with_anti(declared: str) -> _Store:
    store = _Store()
    store.procedures.append((
        "proc_broad", "projA", "Broad reporting skill",
        "Answers anything.",
        f'name: "broad"\ndescription: "d"\nanti_triggers:\n  - "{declared}"\n',
        "body", "active",
    ))
    return store


@pytest.mark.parametrize(
    ("declared", "query"),
    [
        ("media plan", "media plan"),
        ("media plan", "media plan import"),
        ("réconciliation", "réconciliation"),
        ("réconciliation", "reconciliation"),
        ("facturation", "facturation fournisseur"),
    ],
)
def test_an_anti_trigger_in_two_words_or_with_accents_still_excludes(declared, query) -> None:
    """REJECT du 2026-08-05, et c'est le pire cas possible : compare JETON PAR
    JETON, un anti-declencheur en deux mots ou accentue ne correspondait plus a
    rien -- et la Skill remontait alors PAR LE MOT MEME qui l'exclut, puisque le
    frontmatter est compare depuis AI-154."""
    walk = context_search.search_context_walk(
        _FakeConn(_skill_with_anti(declared)), query=query, project_id="projA"
    )
    assert walk.selected == []
    assert [hit.id for hit in walk.refused] == ["proc_broad"]


def test_a_multi_word_query_does_not_re_tier_what_already_worked() -> None:
    """REJECT du 2026-08-05. Le tier TITRE gagne par UN mot faisait remonter un
    titre qui n'en porte qu'un au-dessus de celui qui porte la phrase entiere :
    une requete qui marchait rendait un autre ordre, et un autre extrait."""
    store = _Store()
    store.topics.append((
        "top_zz", "projA", "Attribution window", "Nothing else here.", "active",
    ))
    store.topics.append((
        "top_aa", "projA", "Window sizing", "the attribution window is 7 days", "active",
    ))

    walk = context_search.search_context_walk(
        _FakeConn(store), query="attribution window", project_id="projA"
    )
    ordered = [(hit.id, hit.score) for hit in walk.selected]
    assert ordered == [
        ("top_zz", context_search.TIER_TITLE),
        ("top_aa", context_search.TIER_DESCRIPTION),
    ]


def test_the_frontmatter_excerpt_does_not_depend_on_set_ordering() -> None:
    """L'extrait etait tire d'un `set` : le hachage des chaines varie d'un
    processus a l'autre, donc le contenu rendu etait un tirage."""
    store = _Store()
    store.procedures.append((
        "proc_x", "projA", "Unrelated name", "Unrelated description",
        'name: "x"\ndescription: "d"\nkeywords:\n  - "pacing"\n  - "budget"\n',
        "body without the words", "active",
    ))
    seen = {
        context_search.search_context(
            _FakeConn(store), query="pacing budget", project_id="projA"
        )[0].snippet
        for _ in range(5)
    }
    assert len(seen) == 1


def test_a_neighbour_of_a_discarded_partial_is_not_kept() -> None:
    """Sortie de relecture : les voisins etaient hydrates AVANT l'elagage des
    partiels, donc la marche gardait un candidat au tier VOISIN dont l'ancre
    n'apparait nulle part -- ni classee, ni refusee, ni rejetee."""
    store = _Store()
    store.topics.append((
        "top_full", "projA", "Attribution window", "both words here", "active",
    ))
    store.topics.append((
        "top_part", "projA", "Window sizing", "only one word", "active",
    ))
    store.topics.append((
        "top_orph", "projA", "Orphan", "nothing to do with the query", "active",
    ))
    store.edges.append(("top_part", "context_topic", "top_orph", "context_topic", "projA"))

    walk = context_search.search_context_walk(
        _FakeConn(store), query="attribution window", project_id="projA"
    )
    ids = [hit.id for hit in walk.ranked]
    assert ids == ["top_full"]
    assert "top_orph" not in ids


def test_in_widened_mode_a_neighbour_is_not_presented_as_direct() -> None:
    """Un voisin ne porte AUCUN des mots demandes. En mode elargi il n'y a pas de
    passe directe : le laisser passer devant les elargis, non marque, ferait lire
    comme ferme un resultat qui n'a rien a voir avec la question."""
    store = _Store()
    store.topics.append((
        "top_seed", "projA", "Attribution", "only one of the two words", "active",
    ))
    store.topics.append((
        "top_link", "projA", "A linked definition", "nothing of the query", "active",
    ))
    store.edges.append(("top_seed", "context_topic", "top_link", "context_topic", "projA"))

    walk = context_search.search_context_walk(
        _FakeConn(store), query="attribution unicorn", project_id="projA"
    )
    assert walk.retrieval_descriptor()["match_mode"] == "any_token"
    assert all(hit.widened for hit in walk.ranked)
    # Et le compte le dit : tout ce qui remonte est elargi, voisin compris.
    assert walk.retrieval_descriptor()["widened_count"] == len(walk.ranked)


# ---------------------------------------------------------------------------
# Reparations du 2026-08-10 -- les trous nommes par la relecture du 2026-08-05
# ---------------------------------------------------------------------------


def _refused_skill_behind_an_edge() -> _Store:
    """Une Skill qui declare l'exclusion, atteignable AUSSI par une arete.

    Le controle negatif de 45.5 n'avait qu'une procedure et AUCUNE arete : il ne
    pouvait donc rien dire du saut de graphe, et c'est exactement la ou le refus
    ne tenait pas.
    """
    store = _Store()
    store.topics.append((
        "top_anchor", "projA", "Facturation policy", "How invoices are handled.",
        "active",
    ))
    store.procedures.append((
        "proc_broad", "projA", "Broad reporting skill",
        "Answers anything about facturation and pacing.",
        'name: "broad"\ndescription: "d"\nanti_triggers:\n  - "facturation"\n',
        "body", "active",
    ))
    store.edges.append(("top_anchor", "topic", "proc_broad", "procedure", "projA"))
    return store


def test_the_declared_refusal_holds_on_the_graph_hop_too() -> None:
    """REJECT 45.5 du 2026-08-05, mesure : `_hydrate_neighbours` reselectionnait
    `app.procedures` SANS `frontmatter_yaml` et n'appelait jamais
    `_anti_triggered`. La Skill refusee n'etant pas dans `hits`, la garde
    `if pid_key not in hits` la faisait rentrer au tier VOISIN --

        selected = [('top_1','title',3.0), ('proc_broad','neighbor',1.0)]
        refused  = ['proc_broad']

    Le meme noeud refuse ET servi, et `reached_count = 3` pour deux noeuds.
    Un refus qui ne tient qu'au premier tour est un detour.
    """
    walk = context_search.search_context_walk(
        _FakeConn(_refused_skill_behind_an_edge()),
        query="facturation", project_id="projA",
    )

    assert [hit.id for hit in walk.selected] == ["top_anchor"]
    assert "proc_broad" not in [hit.id for hit in walk.ranked]
    # UNE seule ligne de refus : un candidat porteur de deux sorts
    # contradictoires est l'autre moitie du meme defaut.
    assert [hit.id for hit in walk.refused] == ["proc_broad"]
    fates = [(c.hit.id, c.fate, c.reason) for c in walk.candidates]
    assert fates == [
        ("top_anchor", candidate_fate.FATE_SELECTED, None),
        ("proc_broad", candidate_fate.FATE_REJECTED,
         candidate_fate.REASON_ANTI_TRIGGER),
    ]
    # Deux noeuds atteints, et le compte dit deux.
    assert walk.retrieval_descriptor()["reached_count"] == 2


def test_a_skill_reached_only_by_its_own_exclusion_line_is_not_reached() -> None:
    """REJECT 45.5 : `frontmatter_yaml` est compare par ILIKE depuis AI-154, donc
    une Skill dont le seul << billing >> vit sous `anti_triggers:` remontait sur
    << bill >> -- et l'extrait rendu au lecteur ETAIT la ligne d'exclusion. Le
    mot qui exclut faisait entrer.
    """
    store = _Store()
    store.procedures.append((
        "proc_broad", "projA", "Pacing skill", "Answers pacing questions.",
        'name: "broad"\ndescription: "d"\nanti_triggers:\n  - "billing"\n',
        "body about pacing", "active",
    ))
    walk = context_search.search_context_walk(
        _FakeConn(store), query="bill", project_id="projA"
    )
    # Ni servie, ni refusee : elle n'a jamais correspondu. L'annoncer comme un
    # refus inventerait un examen.
    assert walk.ranked == []
    assert walk.refused == []
    # Et la ligne d'exclusion ne peut plus etre l'extrait, par construction.
    front = 'name: "b"\ndescription: "d"\nanti_triggers:\n  - "billing"\n'
    assert "billing" not in context_search._searchable_frontmatter(front)
    assert context_search._frontmatter_excerpt(front, "billing") == ""


def test_a_reordered_multi_word_anti_trigger_still_excludes() -> None:
    """REJECT 45.9 : la reparation de 45.5 comparait la CHAINE declaree dans la
    requete entiere pendant que l'inclusion etait devenue des jetons SANS ORDRE.
    Declare << media plan >>, tape << plan media >> -> la Skill remontait, avec
    pour extrait la ligne d'exclusion. Les cinq cas parametres au-dessus n'en
    contiennent aucun reordonne.
    """
    for query in ("plan media", "the plan for media", "media import plan"):
        walk = context_search.search_context_walk(
            _FakeConn(_skill_with_anti("media plan")), query=query, project_id="projA"
        )
        assert walk.selected == [], query
        assert [hit.id for hit in walk.refused] == ["proc_broad"], query


def test_the_widened_pass_ranks_by_matched_words_and_not_by_identifier() -> None:
    """REJECT 45.9 (C-3). En mode elargi AUCUN candidat ne peut atteindre le tier
    TITRE (il exige tous les mots), donc la cle de tri s'effondrait sur
    genre -> id -- un ULID. Sur 101 lignes le seul noeud portant les deux mots
    porteurs sortait 101e et tombait sous le plafond, pendant que le canal LLM
    annonce << most relevant first >>. `matched_tokens` etait calcule a trois
    endroits et lu nulle part.
    """
    store = _Store()
    # L'identifiant le PLUS GRAND porte les deux mots : sous l'ancienne cle il
    # sortait dernier.
    store.topics.append((
        "top_zzz", "projA", "Attribution window notes", "both words", "active",
    ))
    for index in range(5):
        store.topics.append((
            f"top_a{index:02d}", "projA", f"Window sizing {index}",
            "only one of the words", "active",
        ))

    walk = context_search.search_context_walk(
        _FakeConn(store), query="attribution window unicorn", project_id="projA"
    )
    descriptor = walk.retrieval_descriptor()
    assert descriptor["match_mode"] == "any_token"
    assert [hit.id for hit in walk.ranked][0] == "top_zzz"
    assert walk.ranked[0].matched_tokens == ("attribution", "window")


def test_the_superset_retrieval_is_bounded_and_says_when_it_was() -> None:
    """REJECT 45.9 : aucune borne SQL ne tenait le SUR-ENSEMBLE, qui tourne a
    CHAQUE recherche. `DEFAULT_LIMIT` ne borne que `selected`. Mesure du
    2026-08-05 : 101 atteintes pour 20 gardees, et 81 lignes ecrites dans
    `app.context_candidate_fates` par requete de bruit.
    """
    store = _crowded_corpus(context_search.SUPERSET_ROW_LIMIT + 40)
    walk = context_search.search_context_walk(
        _FakeConn(store), query="pacing", project_id="projA"
    )
    descriptor = walk.retrieval_descriptor()
    assert len(walk.ranked) == context_search.SUPERSET_ROW_LIMIT
    assert descriptor["scan_row_limit"] == context_search.SUPERSET_ROW_LIMIT
    # Borne ATTEINTE : le descripteur le dit. Un balayage borne muet se lit
    # comme un balayage complet.
    assert descriptor["scan_truncated"] is True

    small = context_search.search_context_walk(
        _FakeConn(_crowded_corpus(5)), query="pacing", project_id="projA"
    )
    assert small.retrieval_descriptor()["scan_truncated"] is False


def test_the_stop_words_do_not_eat_the_bound() -> None:
    """REJECT 45.9 : `MAX_TOKENS=8` etait atteint par << what is the for >> et
    `campaigns` et `france` -- les deux mots qui portent la question -- tombaient.
    Une borne qui garde le bruit et jette le signal est pire qu'une absence.
    """
    query = "what is the attribution window for the paid search campaigns in France"
    tokens = context_search.tokenise(query)
    assert "campaigns" in tokens
    assert "france" in tokens
    assert "the" not in tokens and "what" not in tokens
    assert len(tokens) <= context_search.MAX_TOKENS
    # Une requete qui n'est QUE des mots vides reste une requete : la reduire a
    # zero jeton rendrait << aucun contexte >> sans avoir interroge le corpus.
    assert context_search.tokenise("what is it") == ["what", "is", "it"]
