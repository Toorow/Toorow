"""Story 44.10 -- target_field as a fourth knowledge-graph node type.

Covers the two server-side primitives the story adds below the API layer:

  * ``core.context_store``  -- GRAPH_NODE_TYPES / _node_exists_in_scope /
    create_graph_edge accept 'target_field', keyed by the field NAME, and
    REFUSE a soft-deleted field.
  * ``core.datamodel.list_target_field_nodes`` -- the inclusion rule
    (referenced-only vs ?include_fields=all), the never-return-deleted rule,
    and the MAX(version_number) read.

These do NOT assert "a mock was called". The fake connection below is a tiny
in-memory app.target_fields that EVALUATES the SQL it is handed: it applies the
``status != 'deleted'`` predicate only if the statement actually contains it,
applies the name / approved predicates only if the statement actually contains
them, and computes the version number from the versions table only if the
statement actually contains the MAX subquery over app.target_fields_versions.
Deleting any of those clauses from the production SQL therefore changes the
VALUES these tests assert, and the tests fail.
"""

from __future__ import annotations

import pytest
from core.context_store import (
    GRAPH_NODE_TYPES,
    _node_exists_in_scope,
    create_graph_edge,
)
from core.datamodel import list_target_field_nodes

from tests.support.statement_router import (
    StatementInventory,
    UnknownStatement,
    describe,
)

# ---------------------------------------------------------------------------
# A fake app.target_fields that really evaluates the SQL it receives.
# ---------------------------------------------------------------------------

# The ONE column list still spelled out. `describe()` refuses this projection
# on purpose -- it carries the `(SELECT COALESCE(MAX(v.version_number), 1) ...)`
# scalar subquery, and a deriver that guessed a name for it would be inventing
# the product's alias rather than reading it.
_NODE_COLS = [
    ("name",), ("display_name",), ("description",), ("field_kind",),
    ("created_by",), ("status",), ("version_number",),
]


# EVERY STATEMENT THE EXERCISED PATHS ISSUE, NAMED ONCE (AI-317). What this
# replaces ended in an `else` that set `self._rows = []` and
# `self.description = None` -- an ANSWER, and the one psycopg reserves for a
# statement that returned no result set at all.
#
# Six of the eleven below were never modelled: story 49-6 moved the write out of
# `context_store.create_graph_edge` and into `core.context_relationships`, which
# reads a duplicate, appends a relation head, a version row and a journal row.
# All six fell into that `else`, and all six happened to want "no row", so the
# file stayed green while it stopped describing the product it drives. The next
# read added to `create_relationship` would have been measured as an absence.
#
# Declaration order is the order an `if/elif` chain would test in: first match
# wins. `app.context_relationships` is separated from
# `app.context_relationship_versions` by the table name itself, and the two
# reads of `app.target_fields` by their projection -- never by widening one
# fragment until it swallows both.
_STATEMENTS = StatementInventory(
    "test_context_store_target_field_nodes._FakeCursor",
    # datamodel.py:1589 -- `list_target_field_nodes`, the whole point of the file.
    field_nodes="from app.target_fields tf",
    # context_store.py:1763 -- `_node_exists_in_scope`, target_field branch.
    field_exists="select 1 from app.target_fields",
    # context_store.py:1710 -- the same predicate, topic branch. Reached because
    # `create_graph_edge` proves BOTH endpoints before anything is written.
    topic_exists="select 1 from app.context_topics",
    # context_relationships.py:338 -- `_project_org_id`. The organization is
    # DERIVED from the project graph, never taken from the caller.
    project_org="select org_id from app.projects",
    # context_relationships.py:841 -- NEVER MODELLED. The courtesy duplicate
    # check on the authority table, before the projection is written.
    relation_exists="select status from app.context_relationships",
    # context_relationships.py:426 -- NEVER MODELLED. `_write_projection` asks
    # `uq_context_graph_edge` before the INSERT rather than catching a 23505,
    # because a violation would abort the caller's whole transaction.
    edge_exists="select 1 from app.context_graph",
    # context_relationships.py:443 -- the legacy read projection, INSERT ...
    # RETURNING the nine columns `create_graph_edge` hands back.
    edge_insert="insert into app.context_graph",
    # context_relationships.py:885 -- NEVER MODELLED. The relation head.
    relation_insert="insert into app.context_relationships",
    # context_relationships.py:742 -- NEVER MODELLED. `_append_version`, the
    # immutable version row a create always writes as version 1.
    version_insert="insert into app.context_relationship_versions",
    # context_relationships.py:764 -- NEVER MODELLED. The head pointed at the
    # version just appended.
    relation_head_update="update app.context_relationships",
    # audit.py:234 -- NEVER MODELLED. `_record`, on the caller's transaction.
    audit_insert="insert into app.audit_log",
)


class _FieldStore:
    """fields: list of dicts. versions: {name: [version_number, ...]}."""

    def __init__(
        self,
        fields: list[dict],
        versions: dict[str, list[int]] | None = None,
        topic_ids: set[str] | None = None,
    ):
        self.fields = fields
        self.versions = versions or {}
        self.topic_ids = topic_ids or set()
        self.statements: list[tuple[str, tuple | list]] = []

    # -- SELECT 1 FROM app.target_fields WHERE name = %s [AND status != 'deleted']
    def exists(self, sql: str, params) -> list[tuple]:
        name = params[0]
        rows = [f for f in self.fields if f["name"] == name]
        if "status != 'deleted'" in sql:
            rows = [f for f in rows if f["status"] != "deleted"]
        return [(1,)] if rows else []

    # -- the list_target_field_nodes projection
    def list_nodes(self, sql: str, params) -> list[tuple]:
        rows = list(self.fields)
        if "tf.status != 'deleted'" in sql:
            rows = [f for f in rows if f["status"] != "deleted"]

        params = list(params or [])
        if "tf.status = 'approved' OR tf.name = ANY(%s)" in sql:
            names = set(params[0])
            rows = [f for f in rows if f["status"] == "approved" or f["name"] in names]
        elif "tf.status = 'approved'" in sql:
            rows = [f for f in rows if f["status"] == "approved"]
        elif "tf.name = ANY(%s)" in sql:
            names = set(params[0])
            rows = [f for f in rows if f["name"] in names]

        computes_max = (
            "MAX(v.version_number)" in sql and "app.target_fields_versions" in sql
        )
        out = []
        for f in sorted(rows, key=lambda r: r["name"]):
            if computes_max:
                seen = self.versions.get(f["name"], [])
                version = max(seen) if seen else 1
            else:
                # The production SQL no longer derives the version: report the
                # floor so a regression is visible as a WRONG VALUE, not a pass.
                version = 1
            out.append((
                f["name"], f.get("display_name"), f.get("description"),
                f.get("field_kind"), f.get("created_by"), f["status"], version,
            ))
        return out


class _FakeCursor:
    def __init__(self, store: _FieldStore, inserted_edge: tuple | None):
        self._store = store
        self._inserted_edge = inserted_edge
        self._rows: list[tuple] = []
        self.description: list[tuple] | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self._store.statements.append((flat, params))
        self._rows = []
        self.description = None
        match _STATEMENTS.match(sql):
            case "field_nodes":
                self._rows = self._store.list_nodes(flat, params)
                self.description = _NODE_COLS
            case "field_exists":
                self._rows = self._store.exists(flat, params)
                self.description = describe(sql)
            case "topic_exists":
                self._rows = [(1,)] if params[0] in self._store.topic_ids else []
                self.description = describe(sql)
            case "project_org":
                # Story 49-6 AC5: the relationship authority DERIVES the
                # organization from the project graph rather than taking one from
                # the caller, so the door now asks this on the way through. A fake
                # that answered nothing would make every edge look like an
                # unreadable project.
                self._rows = [("org_1",)]
                self.description = describe(sql)
            case "relation_exists" | "edge_exists":
                # Neither a relation nor a projection pre-exists in these stores:
                # the store holds fields, versions and topic ids, and nothing has
                # ever been related. `fetchone()` is None -- which is what an empty
                # table answers, not what silence answers.
                self.description = describe(sql)
            case "edge_insert":
                self._rows = [self._inserted_edge] if self._inserted_edge else []
                # The nine columns come from the statement's own RETURNING list.
                self.description = describe(sql)
            case (
                "relation_insert"
                | "version_insert"
                | "relation_head_update"
                | "audit_insert"
            ):
                # A write with no RETURNING: psycopg reports `description = None`
                # and no rows, and the product reads neither. MODELLED rather than
                # fallen into, so the day one of them grows a RETURNING the fake
                # stops describing it and says so.
                pass

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, store: _FieldStore, inserted_edge: tuple | None = None):
        self._store = store
        self._inserted_edge = inserted_edge

    def cursor(self):
        return _FakeCursor(self._store, self._inserted_edge)

    def commit(self):
        pass

    def rollback(self):
        pass


_CLICKS = {
    "name": "clicks", "display_name": "Clicks", "description": "Ad clicks.",
    "field_kind": "metric", "created_by": "ann@toorow.com", "status": "approved",
}
_DRAFT = {
    "name": "viewable_impressions", "display_name": "Viewable impressions",
    "description": "", "field_kind": "metric", "created_by": "bob@toorow.com",
    "status": "draft",
}
_DELETED = {
    "name": "legacy_cost", "display_name": "Legacy cost", "description": "Retired.",
    "field_kind": "metric", "created_by": "ann@toorow.com", "status": "deleted",
}


# ---------------------------------------------------------------------------
# GRAPH_NODE_TYPES / _node_exists_in_scope
# ---------------------------------------------------------------------------


def test_graph_node_types_now_includes_target_field():
    """Migration 117 widened the CHECK; the Python enum must agree with it.

    Story 37.9 added `master_data_node` through migration 272, so the set is pinned
    EXACTLY rather than by containment: the Python enum and the CHECK constraint
    drifting apart is how a valid edge starts failing at INSERT with a message about
    a constraint nobody edited.
    """
    assert GRAPH_NODE_TYPES == {
        "topic",
        "procedure",
        "schema_doc",
        "target_field",
        "master_data_node",
    }


def test_node_exists_in_scope_target_field_is_name_keyed_and_platform_global():
    """A live field resolves by NAME, from ANY project scope (the dictionary is
    platform-global -- app.target_fields has no project column)."""
    conn = _FakeConn(_FieldStore([_CLICKS]))

    assert (
        _node_exists_in_scope(
            conn, node_id="clicks", node_type="target_field", project_id="proj_A"
        )
        is True
    )
    # A different project sees the very same field -- no scoping applies.
    assert (
        _node_exists_in_scope(
            conn, node_id="clicks", node_type="target_field", project_id="proj_B"
        )
        is True
    )
    # Platform scope (project_id None) too.
    assert (
        _node_exists_in_scope(
            conn, node_id="clicks", node_type="target_field", project_id=None
        )
        is True
    )


def test_node_exists_in_scope_target_field_unknown_name_is_false():
    conn = _FakeConn(_FieldStore([_CLICKS]))
    assert (
        _node_exists_in_scope(
            conn, node_id="not_a_field", node_type="target_field", project_id="proj_A"
        )
        is False
    )


def test_node_exists_in_scope_refuses_soft_deleted_field():
    """History outlives visibility (44.7), but a deleted field must not become a
    NEW edge endpoint. The fake applies `status != 'deleted'` only because the
    production SQL carries it -- drop the clause and this returns True."""
    store = _FieldStore([_DELETED])
    conn = _FakeConn(store)

    assert (
        _node_exists_in_scope(
            conn, node_id="legacy_cost", node_type="target_field", project_id="proj_A"
        )
        is False
    )
    # And the row genuinely exists in the table -- it is the STATUS that refuses it.
    assert any(f["name"] == "legacy_cost" for f in store.fields)


# ---------------------------------------------------------------------------
# create_graph_edge with target_field endpoints
# ---------------------------------------------------------------------------


def test_create_graph_edge_topic_to_target_field_is_accepted():
    """The 44.5 modal's payload shape (from_type/to_type = 'target_field') must
    round-trip through the store, not be rejected as an unknown node type."""
    store = _FieldStore([_CLICKS], topic_ids={"top_1"})
    inserted = (
        "edge_X1", "top_1", "topic", "clicks", "target_field",
        "explains", "proj_A", "ann@toorow.com", "2026-07-26T10:00:00Z",
    )
    conn = _FakeConn(store, inserted_edge=inserted)

    edge = create_graph_edge(
        conn,
        project_id="proj_A",
        from_id="top_1",
        from_type="topic",
        to_id="clicks",
        to_type="target_field",
        edge_type="explains",
        created_by="ann@toorow.com",
    )

    assert edge["from_type"] == "topic"
    assert edge["to_type"] == "target_field"
    assert edge["to_id"] == "clicks"

    insert_stmts = [
        (s, p) for s, p in store.statements if "INSERT INTO app.context_graph" in s
    ]
    assert len(insert_stmts) == 1
    # 'target_field' really reached the INSERT parameters -- it was not coerced
    # or silently dropped on the way through.
    assert "target_field" in insert_stmts[0][1]


def test_create_graph_edge_rejects_deleted_target_field_endpoint():
    store = _FieldStore([_DELETED])
    conn = _FakeConn(store)

    # The refusal message is ENGLISH. AD-34 is ratified (SPEC.md:159, directive
    # Jean 2026-07-24): all visible application copy is English, and this string
    # is visible -- `core.context_api` returns `str(exc)` verbatim as the 422
    # body. `core.context_store` was reconciled to it in `61010019`; this test
    # kept matching the French it replaced. The product is right and the test was
    # wrong, which is the same shape as AI-103.
    #
    # THE SENTENCE MOVED WITH THE OWNER (story 49-6 AC5, 2026-08-28). The door
    # no longer writes the edge: it declares a relation through
    # `core.context_relationships`, and the refusal is that module's. Two things
    # changed, both deliberate:
    #
    #   * it NAMES THE GESTURE ("Pick a source that exists ... or restore it
    #     first") where the old sentence stated a fact;
    #   * it NO LONGER PRINTS THE FIELD NAME. `ContextRelationshipRefused` is
    #     swept by `test_refusals_never_name_an_identifier`, which forbids a
    #     refusal from rendering the identifier the caller sent -- the old
    #     message escaped that sweep only by being a bare `ValueError`.
    #
    # It is still a `ValueError`, so the 422 the route returns is unchanged.
    with pytest.raises(
        ValueError,
        match=r"Pick a source that exists in this project and is not archived",
    ):
        create_graph_edge(
            conn,
            project_id="proj_A",
            from_id="legacy_cost",
            from_type="target_field",
            to_id="legacy_cost",
            to_type="target_field",
            edge_type="relates_to",
            created_by="ann@toorow.com",
        )
    assert not [s for s, _ in store.statements if "INSERT INTO app.context_graph" in s]


# ---------------------------------------------------------------------------
# list_target_field_nodes -- inclusion rule, deleted exclusion, MAX version
# ---------------------------------------------------------------------------


def test_list_target_field_nodes_no_names_and_no_optin_issues_no_query():
    """Default graph load with zero field edges must not scan the dictionary."""
    store = _FieldStore([_CLICKS, _DRAFT])
    conn = _FakeConn(store)

    assert list_target_field_nodes(conn, names=(), include_approved=False) == []
    assert store.statements == []


def test_list_target_field_nodes_returns_only_the_referenced_names():
    store = _FieldStore([_CLICKS, _DRAFT])
    conn = _FakeConn(store)

    rows = list_target_field_nodes(conn, names=["clicks"], include_approved=False)

    assert [r["name"] for r in rows] == ["clicks"]
    assert rows[0]["display_name"] == "Clicks"
    assert rows[0]["description"] == "Ad clicks."
    assert rows[0]["field_kind"] == "metric"
    assert rows[0]["created_by"] == "ann@toorow.com"
    assert rows[0]["status"] == "approved"


def test_list_target_field_nodes_include_all_adds_every_approved_field():
    """?include_fields=all widens to the approved dictionary -- and keeps the
    referenced DRAFT field, so widening the view can never make an existing
    edge dangle."""
    store = _FieldStore([_CLICKS, _DRAFT])
    conn = _FakeConn(store)

    rows = list_target_field_nodes(
        conn, names=["viewable_impressions"], include_approved=True
    )

    assert [r["name"] for r in rows] == ["clicks", "viewable_impressions"]


def test_list_target_field_nodes_include_all_without_references_is_approved_only():
    store = _FieldStore([_CLICKS, _DRAFT])
    conn = _FakeConn(store)

    rows = list_target_field_nodes(conn, names=(), include_approved=True)

    assert [r["name"] for r in rows] == ["clicks"], "a draft field is not 'all approved'"


def test_list_target_field_nodes_never_returns_a_deleted_field():
    """Even when the field is explicitly referenced by an edge and even under
    include_fields=all."""
    store = _FieldStore([_CLICKS, _DELETED])
    conn = _FakeConn(store)

    referenced = list_target_field_nodes(conn, names=["legacy_cost"], include_approved=False)
    assert referenced == []

    everything = list_target_field_nodes(
        conn, names=["legacy_cost"], include_approved=True
    )
    assert [r["name"] for r in everything] == ["clicks"]


def test_list_target_field_nodes_version_number_is_the_max_of_the_history():
    """v(clicks) = MAX(app.target_fields_versions.version_number) = 5, not the
    row count and not the first version."""
    store = _FieldStore([_CLICKS], versions={"clicks": [1, 2, 5, 3]})
    conn = _FakeConn(store)

    rows = list_target_field_nodes(conn, names=["clicks"], include_approved=False)

    assert rows[0]["version_number"] == 5
    assert isinstance(rows[0]["version_number"], int)


def test_list_target_field_nodes_version_floors_at_one_without_history():
    """Fields seeded before 44.7 have no version rows; v0 would read as
    'never saved' rather than 'no history captured yet'."""
    store = _FieldStore([_CLICKS], versions={})
    conn = _FakeConn(store)

    rows = list_target_field_nodes(conn, names=["clicks"], include_approved=False)
    assert rows[0]["version_number"] == 1


def test_list_target_field_nodes_deduplicates_and_sorts_names():
    store = _FieldStore([_CLICKS, _DRAFT])
    conn = _FakeConn(store)

    rows = list_target_field_nodes(
        conn, names=["viewable_impressions", "clicks", "clicks"], include_approved=False
    )
    assert [r["name"] for r in rows] == ["clicks", "viewable_impressions"]
    # The parameter list itself carries each name once.
    _sql, params = store.statements[-1]
    assert sorted(params[0]) == ["clicks", "viewable_impressions"]
    assert len(params[0]) == 2


def test_the_fake_refuses_a_statement_it_was_never_taught():
    """The property the whole conversion buys: silence is no longer an answer.

    `_next_version_number` (context_relationships.py:702) is the statement a
    SUPERSEDE issues and a create does not -- a create passes `version_number=1`
    rather than asking. It is exactly the shape that used to arrive here and be
    answered "no rows, no description": plausible, adjacent to a table the fake
    knows, and never taught. It must now name itself.
    """
    conn = _FakeConn(_FieldStore([_CLICKS]))
    unknown = (
        "SELECT COALESCE(MAX(version_number), 0) + 1 "
        "FROM app.context_relationship_versions WHERE relationship_id = %s"
    )

    with pytest.raises(UnknownStatement) as raised:
        with conn.cursor() as cur:
            cur.execute(unknown, ("crel_1",))

    message = str(raised.value)
    # The reader's first question is "which query moved?" ...
    assert "coalesce(max(version_number), 0) + 1" in message
    # ... and the second is "what did this fake use to answer?".
    assert "version_insert" in message
