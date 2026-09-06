"""The relationship doors at the seam (story 49-6 AC5/AC6/AC7).

WHAT THIS FILE HOLDS. Three questions that only exist once the routes are
mounted on `build_asgi_app`:

  * WHO MAY. View reads the facet, Member writes, and a caller who is neither
    gets the same non-disclosing 404 every other context route gives -- the
    reason a foreign project must never be distinguishable from an absent one.
  * SINGLE WRITER. A `app.context_graph` row with no relation behind it is a
    row some second writer put there. The DELETE door answers 410 and writes
    NOTHING, rather than destroying evidence of a store the cutover missed.
  * A FAILED FACET READ IS NOT AN EMPTY LIST. The route hands back the typed
    `unavailable` at 200 so the panel renders a sentence and the rest of the
    owner's screen keeps working.

The refusals themselves are proved in `tests/core/test_context_relationships.py`
and against a real database in `tests/integration/test_context_relationships_pg.py`.
"""

from __future__ import annotations

import os

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from unittest.mock import MagicMock, patch  # noqa: E402

from core.main import build_asgi_app  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

_HEAD_COLUMNS = [
    ("id",), ("org_id",), ("project_id",), ("source_type",), ("source_id",),
    ("target_type",), ("target_id",), ("relationship_kind",), ("status",),
    ("provenance",), ("projection_edge_id",), ("current_version_id",),
    ("created_by",), ("created_at",), ("updated_at",),
]

_RELATION_ROW = (
    "crel_01J0000000000000000000AA",
    "org_1",
    "proj_A",
    "topic",
    "top_01",
    "target_field",
    "clicks",
    "explains",
    "active",
    "console",
    "edge_A001",
    "crelv_01J0000000000000000000BB",
    "user_a",
    "2026-08-28T10:00:00Z",
    "2026-08-28T10:00:00Z",
)

_EDGE_COLUMNS = [
    ("id",), ("from_id",), ("from_type",), ("to_id",), ("to_type",),
    ("edge_type",), ("project_id",), ("created_by",), ("created_at",),
]

_EDGE_ROW = (
    "edge_A001", "top_01", "topic", "clicks", "target_field",
    "explains", "proj_A", "user_a", "2026-08-28T10:00:00Z",
)


def _mock_conn():
    conn = MagicMock()
    conn.__enter__.return_value = conn
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    return conn, cur


def _client():
    return TestClient(build_asgi_app())


# ---------------------------------------------------------------------------
# Who may
# ---------------------------------------------------------------------------


def test_the_facet_requires_authentication():
    with patch("core.admin_api._check_auth", return_value=(False, "")):
        resp = _client().get(
            "/api/context/relationships?project_id=proj_A&node_type=topic&node_id=top_01"
        )
    assert resp.status_code == 401


def test_a_viewer_reads_the_facet():
    conn, cur = _mock_conn()
    cur.fetchall.return_value = [_RELATION_ROW]
    cur.description = _HEAD_COLUMNS

    with (
        patch("core.admin_api._check_auth", return_value=(True, "viewer@example.com")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = _client().get(
            "/api/context/relationships?project_id=proj_A&node_type=topic&node_id=top_01"
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "ready"
    assert body["outgoing"][0]["other"] == {"type": "target_field", "id": "clicks"}
    # THE LINK IS THE OWNER'S OWN ADDRESS, so the panel does not have to guess.
    assert body["outgoing"][0]["owner"]["href"].startswith("/api/context/graph")


def test_a_caller_without_the_project_gets_the_non_disclosing_404():
    """Foreign, denied and absent are ONE answer, here as everywhere else."""
    conn, _cur = _mock_conn()

    with (
        patch("core.admin_api._check_auth", return_value=(True, "outsider@example.com")),
        patch("core.project_access.identity_has_project_role", return_value=False),
        patch("core.db.get_connection", return_value=conn),
        patch("core.context_api.write_audit_row") as audited,
    ):
        resp = _client().get(
            "/api/context/relationships?project_id=proj_A&node_type=topic&node_id=top_01"
        )

    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"
    audited.assert_called_once()


def test_the_facet_names_what_it_needs_rather_than_guessing():
    with patch("core.admin_api._check_auth", return_value=(True, "viewer@example.com")):
        resp = _client().get("/api/context/relationships?project_id=proj_A")
    assert resp.status_code == 422
    assert "node_type" in resp.json()["message"]


def test_a_failed_facet_read_answers_unavailable_and_not_an_empty_list():
    conn, cur = _mock_conn()
    cur.execute.side_effect = RuntimeError("the store is unreachable")

    with (
        patch("core.admin_api._check_auth", return_value=(True, "viewer@example.com")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
    ):
        resp = _client().get(
            "/api/context/relationships?project_id=proj_A&node_type=topic&node_id=top_01"
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "unavailable"
    assert "outgoing" not in body and "incoming" not in body


# ---------------------------------------------------------------------------
# One writer
# ---------------------------------------------------------------------------


def test_the_legacy_door_refuses_an_endpoint_it_could_not_hand_back():
    """A Datastream is an endpoint of the AUTHORITY, not of this route.

    This route's contract is an `app.context_graph` row, and the legacy store's
    CHECK knows five node types. Accepting a sixth would mean answering 201 with
    an empty body -- a success a caller cannot use.
    """
    conn, _cur = _mock_conn()

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
        patch("core.context_relationships.create_relationship") as writer,
    ):
        resp = _client().post(
            "/api/context/graph/edges?project_id=proj_A",
            json={
                "from_type": "topic",
                "from_id": "top_01",
                "to_type": "datastream",
                "to_id": "ds_EXAMPLE",
                "edge_type": "relates_to",
            },
        )

    assert resp.status_code == 422
    assert "datastream" in resp.json()["message"]
    writer.assert_not_called()


def test_an_edge_with_no_relation_behind_it_is_reported_not_destroyed():
    """THE SINGLE-WRITER PROOF, and the only place it can be made.

    Migration 317 carries every incumbent edge into the authority, and the
    projection is written by the authority alone. So a projection row with no
    relation behind it means something else wrote it -- and answering 204 would
    delete the one piece of evidence that a second writer exists. 410 says the
    object is no longer served through this door, and nothing is written.
    """
    conn, cur = _mock_conn()
    cur.fetchone.side_effect = [
        _EDGE_ROW,  # get_graph_edge -- the scope check reads it unfiltered
        None,       # find_by_projection_edge -- no relation holds this row
    ]
    cur.description = _EDGE_COLUMNS

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
        patch("core.context_relationships.supersede_relationship") as writer,
    ):
        resp = _client().delete("/api/context/graph/edges/edge_A001")

    assert resp.status_code == 410
    assert resp.json()["code"] == "relation_not_governed"
    writer.assert_not_called()
    conn.commit.assert_not_called()


def test_retiring_a_relation_goes_through_the_authority_and_commits_once():
    conn, cur = _mock_conn()
    cur.fetchone.side_effect = [_EDGE_ROW, _RELATION_ROW]
    cur.description = _EDGE_COLUMNS

    with (
        patch("core.admin_api._check_auth", return_value=(True, "user_a")),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", return_value=conn),
        patch("core.context_relationships.supersede_relationship") as writer,
    ):
        resp = _client().delete("/api/context/graph/edges/edge_A001")

    assert resp.status_code == 204
    assert writer.call_args.kwargs["relationship_id"] == _RELATION_ROW[0]
    assert writer.call_args.kwargs["actor"] == "user_a"
    conn.commit.assert_called_once()
