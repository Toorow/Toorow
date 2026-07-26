"""Story 44.10 -- GET /api/context/graph includes target_field nodes.

Seam tests for the 44.3 bundle handler's fourth node type. They pin the
INCLUSION RULE (the whole dictionary is never drawn by default), the payload
projection (title/excerpt/owner/version/scope/field_kind), and the interaction
with the pre-existing dangling-edge filter.

The mocked cursor here plays back rows the way the sibling 44.3 seam file does,
but every assertion is on the RESPONSE BODY or on the SQL actually issued --
never on "a mock was called".
"""

from __future__ import annotations

import os

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from unittest.mock import MagicMock, patch  # noqa: E402

from core.main import build_asgi_app  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

_TOPIC_COLS = [
    ("id",), ("project_id",), ("title",), ("body_md",), ("status",), ("owner",),
    ("created_by",), ("created_at",), ("updated_at",), ("version_number",),
]
_PROC_COLS = [
    ("id",), ("project_id",), ("name",), ("description",), ("frontmatter_yaml",),
    ("body_md",), ("status",), ("owner",), ("created_by",), ("created_at",), ("updated_at",),
    ("version_number",),
]
_SCHEMA_DOC_COLS = [
    ("id",), ("project_id",), ("relation",), ("doc_kind",), ("body_md",),
    ("generated_at",), ("created_at",), ("version_number",),
]
_EDGE_COLS = [
    ("id",), ("from_id",), ("from_type",), ("to_id",), ("to_type",),
    ("edge_type",), ("project_id",), ("created_by",), ("created_at",),
]
_FIELD_COLS = [
    ("name",), ("display_name",), ("description",), ("field_kind",),
    ("created_by",), ("status",), ("version_number",),
]

_TOPIC_ROW = (
    "top_a", "proj_A", "ROAS policy", "how roas is computed", "active", None,
    "ann@toorow.com", "2026-07-20T10:00:00Z", "2026-07-20T10:00:00Z", 2,
)
_EDGE_TOPIC_TO_FIELD = (
    "edge_F1", "top_a", "topic", "clicks", "target_field",
    "explains", "proj_A", "ann@toorow.com", "2026-07-26T10:00:00Z",
)
_FIELD_CLICKS = (
    "clicks", "Clicks", "Ad clicks, deduplicated.", "metric",
    "ann@toorow.com", "approved", 5,
)
_FIELD_SESSIONS = (
    "sessions", "Sessions", "GA4 sessions.", "metric",
    "bob@toorow.com", "approved", 1,
)


def _client_with(fetchall_sequence):
    """Build (client, cursor) wired so each fetchall() plays the next batch and
    cur.description tracks the table the statement names."""
    conn = MagicMock()
    conn.__enter__.return_value = conn
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    def _description_by_query(sql, *_a, **_k):
        if "app.context_topics" in sql:
            cur.description = _TOPIC_COLS
        elif "app.procedures" in sql:
            cur.description = _PROC_COLS
        elif "app.schema_context" in sql:
            cur.description = _SCHEMA_DOC_COLS
        elif "app.context_graph" in sql:
            cur.description = _EDGE_COLS
        elif "app.target_fields" in sql:
            cur.description = _FIELD_COLS

    cur.execute.side_effect = _description_by_query
    cur.fetchall.side_effect = fetchall_sequence
    return conn, cur


def _get(conn, url):
    app = build_asgi_app()
    client = TestClient(app)
    with (
        patch("core.admin_api._check_auth", return_value=(True, "ann@toorow.com")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
    ):
        return client.get(url)


def _field_sql(cur):
    """Every statement issued against app.target_fields (whitespace-collapsed)."""
    return [
        " ".join(call.args[0].split())
        for call in cur.execute.call_args_list
        if "app.target_fields" in call.args[0]
    ]


# ---------------------------------------------------------------------------
# Inclusion rule
# ---------------------------------------------------------------------------


def test_referenced_field_becomes_a_node_and_its_edge_survives():
    """An edge topic -> target_field pulls the field into the bundle, with the
    dictionary projection the story specifies, and the edge is NOT dropped by
    the dangling-edge filter."""
    conn, cur = _client_with([
        [_TOPIC_ROW],            # topics
        [],                      # procedures
        [],                      # schema docs
        [_EDGE_TOPIC_TO_FIELD],  # edges
        [_FIELD_CLICKS],         # target fields
    ])

    resp = _get(conn, "/api/context/graph?project_id=proj_A")

    assert resp.status_code == 200
    data = resp.json()
    by_id = {n["id"]: n for n in data["nodes"]}
    assert set(by_id) == {"top_a", "clicks"}

    field = by_id["clicks"]
    assert field["node_type"] == "target_field"
    assert field["title"] == "Clicks", "title is display_name"
    assert field["excerpt"] == "Ad clicks, deduplicated.", "excerpt is the description, verbatim"
    assert field["owner"] == "ann@toorow.com", "owner is created_by"
    assert field["version_number"] == 5, "version is MAX(target_fields_versions)"
    assert field["scope"] == "platform", "the dictionary is platform-global"
    assert field["status"] == "approved"
    assert field["field_kind"] == "metric"
    # doc_kind belongs to schema docs only.
    assert "doc_kind" not in field

    assert [e["id"] for e in data["edges"]] == ["edge_F1"]

    # Only the referenced name was asked for -- no whole-dictionary scan.
    sql = _field_sql(cur)
    assert len(sql) == 1
    assert "tf.name = ANY(%s)" in sql[0]
    assert "tf.status = 'approved'" not in sql[0]
    field_call = next(
        c for c in cur.execute.call_args_list if "app.target_fields" in c.args[0]
    )
    assert field_call.args[1] == [["clicks"]], "only the referenced name is requested"


def test_no_field_edges_means_the_dictionary_is_not_queried_at_all():
    """The default graph load of a project with no field links must not touch
    app.target_fields -- hundreds of unlinked fields would drown the map (and
    cost a scan on every load)."""
    plain_edge = (
        "edge_P1", "top_a", "topic", "top_a", "topic",
        "relates_to", "proj_A", "ann@toorow.com", "2026-07-26T10:00:00Z",
    )
    conn, cur = _client_with([[_TOPIC_ROW], [], [], [plain_edge]])

    resp = _get(conn, "/api/context/graph?project_id=proj_A")

    assert resp.status_code == 200
    assert {n["id"] for n in resp.json()["nodes"]} == {"top_a"}
    assert _field_sql(cur) == []


def test_include_fields_all_adds_every_approved_field_without_any_edge():
    """?include_fields=all is the opt-in that draws the approved dictionary."""
    conn, cur = _client_with([
        [_TOPIC_ROW],
        [],
        [],
        [],                                 # no edges at all
        [_FIELD_CLICKS, _FIELD_SESSIONS],   # target fields
    ])

    resp = _get(conn, "/api/context/graph?project_id=proj_A&include_fields=all")

    assert resp.status_code == 200
    nodes = {n["id"]: n for n in resp.json()["nodes"]}
    assert set(nodes) == {"top_a", "clicks", "sessions"}
    assert nodes["sessions"]["node_type"] == "target_field"

    sql = _field_sql(cur)
    assert len(sql) == 1
    assert "tf.status = 'approved'" in sql[0]


def test_include_fields_other_value_is_not_the_opt_in():
    """Only the exact value 'all' opts in -- a typo must not silently widen the
    map to the whole dictionary."""
    conn, cur = _client_with([[_TOPIC_ROW], [], [], []])

    resp = _get(conn, "/api/context/graph?project_id=proj_A&include_fields=yes")

    assert resp.status_code == 200
    assert {n["id"] for n in resp.json()["nodes"]} == {"top_a"}
    assert _field_sql(cur) == []


# ---------------------------------------------------------------------------
# Deleted / missing fields
# ---------------------------------------------------------------------------


def test_edge_to_a_field_that_no_longer_resolves_is_dropped_as_dangling():
    """A soft-deleted field is excluded by list_target_field_nodes, so its edge
    has no node to attach to and must be dropped rather than left dangling for
    React Flow -- the same rule 44.3 already applies to archived topics."""
    edge_to_deleted = (
        "edge_D1", "top_a", "topic", "legacy_cost", "target_field",
        "explains", "proj_A", "ann@toorow.com", "2026-07-26T10:00:00Z",
    )
    conn, cur = _client_with([
        [_TOPIC_ROW],
        [],
        [],
        [edge_to_deleted],
        [],  # the dictionary read returns nothing: the field is deleted
    ])

    resp = _get(conn, "/api/context/graph?project_id=proj_A")

    assert resp.status_code == 200
    data = resp.json()
    assert {n["id"] for n in data["nodes"]} == {"top_a"}
    assert data["edges"] == []

    # The exclusion is enforced in SQL, not by the fixture happening to be empty.
    assert "tf.status != 'deleted'" in _field_sql(cur)[0]


def test_field_to_field_edge_pulls_in_both_endpoints():
    """Both ends of a target_field -> target_field edge must be requested; only
    collecting one side would make the edge dangle."""
    field_to_field = (
        "edge_FF", "clicks", "target_field", "sessions", "target_field",
        "relates_to", "proj_A", "ann@toorow.com", "2026-07-26T10:00:00Z",
    )
    conn, cur = _client_with([
        [], [], [], [field_to_field], [_FIELD_CLICKS, _FIELD_SESSIONS],
    ])

    resp = _get(conn, "/api/context/graph?project_id=proj_A")

    assert resp.status_code == 200
    data = resp.json()
    assert {n["id"] for n in data["nodes"]} == {"clicks", "sessions"}
    assert [e["id"] for e in data["edges"]] == ["edge_FF"]

    field_call = next(
        c for c in cur.execute.call_args_list if "app.target_fields" in c.args[0]
    )
    assert sorted(field_call.args[1][0]) == ["clicks", "sessions"]


def test_field_without_display_name_falls_back_to_its_name():
    """display_name is nullable; the node title is then the field NAME, never a
    blank card and never a fabricated label."""
    unnamed = ("cost_micros", None, None, "metric", "ann@toorow.com", "draft", 1)
    edge = (
        "edge_U1", "top_a", "topic", "cost_micros", "target_field",
        "explains", "proj_A", "ann@toorow.com", "2026-07-26T10:00:00Z",
    )
    conn, _cur = _client_with([[_TOPIC_ROW], [], [], [edge], [unnamed]])

    resp = _get(conn, "/api/context/graph?project_id=proj_A")

    assert resp.status_code == 200
    node = next(n for n in resp.json()["nodes"] if n["id"] == "cost_micros")
    assert node["title"] == "cost_micros"
    assert node["excerpt"] == "", "a description-less field gets an empty excerpt, not a summary"
    assert node["status"] == "draft", "a referenced DRAFT field still renders"
