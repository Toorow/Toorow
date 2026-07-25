"""toorow -- Context retrieval for the agent surface (Story 11.5).

Data layer for the two CORE-owned MCP tools ``search_context`` and
``get_procedure`` (registered, un-namespaced, in ``core.main`` -- AD-2). This
module owns the retrieval + ranking logic; ``core.main`` owns only the thin
dual-channel envelope wrapping (AD-1).

Ranking algorithm (search_context) -- documented and test-proven
----------------------------------------------------------------
A single lexical + graph pass over the context corpus, scored in three tiers so
the agent always sees the most on-topic fragment first (Epic 11 Architecture
bullet: "titre > description > voisins de graphe"). Vector recall is a non-goal
for v1 (Epic 11 Non-goals); this is deterministic lexical rank + a graph hop.

  TIER_TITLE       (3.0) -- the query term appears in a topic TITLE, a procedure
                            NAME, or a schema_context RELATION name. The strongest
                            signal: the author titled the fragment for this term.
  TIER_DESCRIPTION (2.0) -- the query term appears in the BODY of a topic /
                            schema doc, or a procedure DESCRIPTION/body. The
                            fragment is about the term but is not titled for it.
  TIER_NEIGHBOR    (1.0) -- the fragment is a context_graph neighbour (one hop)
                            of a fragment that matched at TITLE or DESCRIPTION
                            tier, but did not itself match the query lexically.
                            Surfaced so a definition linked to a matched concept
                            is not lost, but always ranked below a direct match.

Results are sorted by (score DESC, then a stable secondary key: kind order
topic < procedure < schema_doc, then id) so ordering is deterministic and
assertable by value in tests. Ties never depend on DB row order.

Scoping (AD-5)
--------------
Every query is scoped to the caller's project: platform rows (``project_id IS
NULL`` for topics/procedures) are visible to everyone; a project sees ITS OWN
project rows plus platform rows, and NEVER another project's rows. schema_context
rows are always project-scoped (``project_id`` is NOT NULL) so they filter on
equality only. Current versions only: topics/procedures filter ``status =
'active'``; schema_context holds the current doc (history lives in
schema_context_versions and is never surfaced here).
"""

from __future__ import annotations

from typing import Any

# Ranking tiers (documented above). Kept as module constants so tests assert on
# the exact ordering contract rather than magic numbers.
TIER_TITLE = 3.0
TIER_DESCRIPTION = 2.0
TIER_NEIGHBOR = 1.0

# Deterministic secondary sort within a score tier: topics first, then
# procedures, then schema docs, then id. Keeps ordering reproducible (AI-56:
# assertions on value, never on incidental DB row order).
_KIND_ORDER = {"topic": 0, "procedure": 1, "schema_doc": 2}

# Cap on the number of ranked hits carried in the detail channel. The summary is
# separately capped at <=30 lines by core.main (AD-1).
DEFAULT_LIMIT = 20


class ContextHit:
    """One ranked context fragment. Plain data holder (JSON-serialised by main)."""

    __slots__ = ("id", "kind", "title", "snippet", "score", "tier", "project_id", "matched")

    def __init__(
        self,
        *,
        id: str,
        kind: str,
        title: str,
        snippet: str,
        score: float,
        tier: str,
        project_id: str | None,
        matched: bool,
    ) -> None:
        self.id = id
        self.kind = kind
        self.title = title
        self.snippet = snippet
        self.score = score
        self.tier = tier
        self.project_id = project_id
        self.matched = matched  # True = direct lexical hit; False = graph neighbour

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "snippet": self.snippet,
            "score": self.score,
            "tier": self.tier,
            "project_id": self.project_id,
        }


def _snippet(text: str, *, limit: int = 160) -> str:
    """Single-line snippet: collapse whitespace, trim to *limit* chars."""
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _tier_name(score: float) -> str:
    if score >= TIER_TITLE:
        return "title"
    if score >= TIER_DESCRIPTION:
        return "description"
    return "neighbor"


def search_context(
    conn: Any,
    *,
    query: str,
    project_id: str | None,
    limit: int = DEFAULT_LIMIT,
) -> list[ContextHit]:
    """Rank the context corpus for *query*, scoped to *project_id* (AD-5).

    Returns ranked ``ContextHit`` objects (highest score first). Matches topics,
    procedures and schema_context docs at the TITLE / DESCRIPTION tiers, then adds
    one-hop context_graph neighbours of matched fragments at the NEIGHBOR tier.
    Current versions only (topics/procedures ``status='active'``; schema_context
    is current by construction). Empty/blank query -> empty result.
    """
    term = (query or "").strip()
    if not term:
        return []

    like = f"%{term}%"
    hits: dict[str, ContextHit] = {}

    with conn.cursor() as cur:
        # --- Topics (platform + project scope, active only) ------------------
        cur.execute(
            """
            SELECT id, project_id, title, body_md
            FROM app.context_topics
            WHERE status = 'active'
              AND (project_id IS NULL OR project_id = %s)
              AND (title ILIKE %s OR body_md ILIKE %s)
            """,
            (project_id, like, like),
        )
        for row in cur.fetchall():
            tid, pid, title, body = row
            title_hit = term.lower() in (title or "").lower()
            score = TIER_TITLE if title_hit else TIER_DESCRIPTION
            hits[tid] = ContextHit(
                id=tid,
                kind="topic",
                title=title or "",
                snippet=_snippet(title if title_hit else body),
                score=score,
                tier=_tier_name(score),
                project_id=pid,
                matched=True,
            )

        # --- Procedures (platform + project scope, active only) --------------
        cur.execute(
            """
            SELECT id, project_id, name, description, body_md
            FROM app.procedures
            WHERE status = 'active'
              AND (project_id IS NULL OR project_id = %s)
              AND (name ILIKE %s OR description ILIKE %s OR body_md ILIKE %s)
            """,
            (project_id, like, like, like),
        )
        for row in cur.fetchall():
            pid_key, pid, name, desc, body = row
            name_hit = term.lower() in (name or "").lower()
            score = TIER_TITLE if name_hit else TIER_DESCRIPTION
            hits[pid_key] = ContextHit(
                id=pid_key,
                kind="procedure",
                title=name or "",
                snippet=_snippet(name if name_hit else (desc or body)),
                score=score,
                tier=_tier_name(score),
                project_id=pid,
                matched=True,
            )

        # --- Schema context docs (project-scoped; a schema TERM surfaces its
        #     doc -- Story 11.5 "also indexes schema_context docs"). -----------
        if project_id is not None:
            cur.execute(
                """
                SELECT id, project_id, relation, doc_kind, body_md
                FROM app.schema_context
                WHERE project_id = %s
                  AND (relation ILIKE %s OR body_md ILIKE %s)
                """,
                (project_id, like, like),
            )
            for row in cur.fetchall():
                sid, pid, relation, doc_kind, body = row
                relation_hit = term.lower() in (relation or "").lower()
                score = TIER_TITLE if relation_hit else TIER_DESCRIPTION
                hits[sid] = ContextHit(
                    id=sid,
                    kind="schema_doc",
                    title=f"{relation} ({doc_kind})",
                    snippet=_snippet(relation if relation_hit else body),
                    score=score,
                    tier=_tier_name(score),
                    project_id=pid,
                    matched=True,
                )

        # --- Graph neighbours (one hop from any direct match) ----------------
        # Surfaced at TIER_NEIGHBOR so a linked definition is not lost, but never
        # outranks a direct lexical hit. Only edges visible in scope (platform
        # edges or the caller's project edges) are followed (AD-5).
        matched_ids = list(hits.keys())
        if matched_ids:
            cur.execute(
                """
                SELECT from_id, from_type, to_id, to_type
                FROM app.context_graph
                WHERE (project_id IS NULL OR project_id = %s)
                  AND (from_id = ANY(%s) OR to_id = ANY(%s))
                """,
                (project_id, matched_ids, matched_ids),
            )
            neighbour_refs: dict[str, str] = {}
            for from_id, from_type, to_id, to_type in cur.fetchall():
                if from_id in hits and to_id not in hits:
                    neighbour_refs.setdefault(to_id, to_type)
                if to_id in hits and from_id not in hits:
                    neighbour_refs.setdefault(from_id, from_type)

            _hydrate_neighbours(cur, neighbour_refs, hits, project_id)

    ranked = sorted(
        hits.values(),
        key=lambda h: (-h.score, _KIND_ORDER.get(h.kind, 9), h.id),
    )
    return ranked[:limit]


def _hydrate_neighbours(
    cur: Any,
    neighbour_refs: dict[str, str],
    hits: dict[str, ContextHit],
    project_id: str | None,
) -> None:
    """Resolve neighbour ids to display rows and add them at TIER_NEIGHBOR."""
    if not neighbour_refs:
        return

    topic_ids = [nid for nid, t in neighbour_refs.items() if t == "topic"]
    proc_ids = [nid for nid, t in neighbour_refs.items() if t == "procedure"]
    schema_ids = [nid for nid, t in neighbour_refs.items() if t == "schema_doc"]

    if topic_ids:
        cur.execute(
            """
            SELECT id, project_id, title, body_md
            FROM app.context_topics
            WHERE status = 'active'
              AND (project_id IS NULL OR project_id = %s)
              AND id = ANY(%s)
            """,
            (project_id, topic_ids),
        )
        for tid, pid, title, body in cur.fetchall():
            if tid not in hits:
                hits[tid] = ContextHit(
                    id=tid, kind="topic", title=title or "",
                    snippet=_snippet(body or title), score=TIER_NEIGHBOR,
                    tier="neighbor", project_id=pid, matched=False,
                )

    if proc_ids:
        cur.execute(
            """
            SELECT id, project_id, name, description
            FROM app.procedures
            WHERE status = 'active'
              AND (project_id IS NULL OR project_id = %s)
              AND id = ANY(%s)
            """,
            (project_id, proc_ids),
        )
        for pid_key, pid, name, desc in cur.fetchall():
            if pid_key not in hits:
                hits[pid_key] = ContextHit(
                    id=pid_key, kind="procedure", title=name or "",
                    snippet=_snippet(desc or name), score=TIER_NEIGHBOR,
                    tier="neighbor", project_id=pid, matched=False,
                )

    if schema_ids and project_id is not None:
        cur.execute(
            """
            SELECT id, project_id, relation, doc_kind, body_md
            FROM app.schema_context
            WHERE project_id = %s AND id = ANY(%s)
            """,
            (project_id, schema_ids),
        )
        for sid, pid, relation, doc_kind, body in cur.fetchall():
            if sid not in hits:
                hits[sid] = ContextHit(
                    id=sid, kind="schema_doc", title=f"{relation} ({doc_kind})",
                    snippet=_snippet(body or relation), score=TIER_NEIGHBOR,
                    tier="neighbor", project_id=pid, matched=False,
                )


def get_procedure_by_name(
    conn: Any,
    *,
    name: str,
    project_id: str | None,
) -> dict[str, Any] | None:
    """Fetch the current (active) procedure by NAME, scoped to *project_id* (AD-5).

    A procedure is citable by name (Story 11.5). Project scope wins over platform
    when both define the same name: we prefer the project-scoped row so a project
    override of a platform procedure resolves to the project's version. Returns
    None when no active procedure matches in scope (the caller renders a stable,
    non-disclosing not-found result -- it never reveals whether the name exists in
    another project's scope).
    """
    clean = (name or "").strip()
    if not clean:
        return None

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                p.id, p.project_id, p.name, p.description,
                p.frontmatter_yaml, p.body_md, p.status,
                COALESCE(MAX(v.version_number), 1) AS version_number
            FROM app.procedures p
            LEFT JOIN app.procedures_versions v ON p.id = v.procedure_id
            WHERE p.status = 'active'
              AND p.name = %s
              AND (p.project_id IS NULL OR p.project_id = %s)
            GROUP BY
                p.id, p.project_id, p.name, p.description,
                p.frontmatter_yaml, p.body_md, p.status
            -- Prefer the project-scoped row over the platform row (NULLs last).
            ORDER BY (p.project_id IS NULL) ASC
            LIMIT 1
            """,
            (clean, project_id),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
