"""The AI Path reads exist as routes, and refuse in the right shapes (49.6).

`core.ai_paths` had no HTTP exposure at all: `load_path` and `list_paths` were
callable only from Python. These assert the ROUTE — registration, method, the
capability it holds and each refusal — rather than the functions behind it,
because a read reachable only from a test is the defect this repository keeps
paying for.
"""

from __future__ import annotations

import inspect
import sys
from datetime import datetime

from core.ai_paths_api import (
    AI_PATH_ROUTES,
    MAX_LIMIT,
    _authorize,
    _get_ai_path,
    _inspection_summary,
    _list_ai_paths,
    _record_inspection,
)

COLLECTION = "/api/projects/{project_id}/context/ai-paths"
OBJECT = "/api/projects/{project_id}/context/ai-paths/{path_id}"
INSPECTIONS = "/api/projects/{project_id}/context/ai-paths/{path_id}/inspections"
INSPECTION_SUMMARY = "/api/projects/{project_id}/context/ai-paths/inspections/summary"


def _route(path: str):
    return next((r for r in AI_PATH_ROUTES if r.path == path), None)


def test_both_reads_are_registered_as_gets() -> None:
    for path in (COLLECTION, OBJECT):
        route = _route(path)
        assert route is not None, f"{path} has no route: the read is unreachable"
        assert "GET" in route.methods


def test_the_path_itself_is_never_written_through_this_surface() -> None:
    """An AI Path is observed evidence. Nothing here writes one.

    Until Story 55.2 this file asserted "no POST at all", which was the right
    invariant for the wrong object: what must never happen is a route that
    CREATES OR EDITS a path or its steps. 55.2 adds exactly one POST, and it
    appends to `app.evidence_inspections` -- a record that somebody LOOKED at a
    walk, never a record of the walk. So the assertion is sharpened rather than
    dropped: no route may name the path tables in a write, and the two reads stay
    reads.
    """
    for route in AI_PATH_ROUTES:
        assert "PATCH" not in route.methods
        assert "PUT" not in route.methods
        assert "DELETE" not in route.methods

    for path in (COLLECTION, OBJECT):
        assert "POST" not in _route(path).methods

    for handler in (_list_ai_paths, _get_ai_path, _record_inspection):
        source = inspect.getsource(handler)
        for forbidden in (
            "INSERT INTO app.ai_paths",
            "INSERT INTO app.ai_path_steps",
            "UPDATE app.ai_paths",
            "DELETE FROM app.ai_paths",
            "begin_path(",
            "append_step(",
            "finalize_path(",
        ):
            assert forbidden not in source, f"{handler.__name__} writes a path: {forbidden}"


def test_the_only_write_records_an_inspection_and_nothing_else() -> None:
    """Story 55.2 AC4/AC8: the write is the observation, not a data read."""
    route = _route(INSPECTIONS)
    assert route is not None, (
        "no inspection route: the subtree would ship the exact surface "
        "`mcp_app_behavior` judges and leave it unobservable"
    )
    assert route.methods == {"POST"} or set(route.methods) >= {"POST"}
    assert "GET" not in route.methods

    source = inspect.getsource(_record_inspection)
    # The four facts AC4 names, each read from somewhere real rather than
    # invented at the call site.
    assert "insert_inspection(" in source
    assert "decision.org_id" in source          # scope, from the access decision
    assert "ai_path_id=path_id" in source       # which walk
    assert 'body.get("step_ordinal")' in source  # which step
    assert "actor=actor" in source              # who, from the authenticated identity
    # ... and no second question is asked of the warehouse on the way.
    for forbidden in ("load_path(", "list_paths(", "read_slice(", "run_query"):
        assert forbidden not in source, f"the write performs a data read: {forbidden}"


def test_the_write_holds_the_same_grant_as_the_reads() -> None:
    """One authorization question, one answer. A separate rule for the write
    would eventually diverge from the rule for the read it belongs to."""
    source = inspect.getsource(_record_inspection)
    assert "_strict_project_access" in source
    assert 'minimum_capability="view"' in source
    # Story 21.6: the floor arrives WITH the connection instead of being armed
    # by hand inside the handler. Same invariant, checked on the acquisition.
    assert "with request_connection(" in source
    assert "_denied()" in source and "_not_found()" in source


def test_the_object_route_is_ordered_before_the_collection() -> None:
    """`/ai-paths/{path_id}` and `/ai-paths` share a prefix; order decides."""
    paths = [r.path for r in AI_PATH_ROUTES]

    assert paths.index(OBJECT) < paths.index(COLLECTION)


def test_both_reads_resolve_scope_from_the_authenticated_identity() -> None:
    for handler in (_list_ai_paths, _get_ai_path):
        source = inspect.getsource(handler)
        assert "_strict_project_access" in source
        assert 'minimum_capability="view"' in source
        assert "with request_connection(" in source


def test_the_access_helper_delegates_to_the_strict_seam() -> None:
    """The wrapper exists for the disabled-auth dev bypass (live finding A1,
    2026-08-05: every ai-paths route 404'd under `make dev`); the production
    decision itself must stay the canonical strict resolver."""
    from core.ai_paths_api import _strict_project_access

    source = inspect.getsource(_strict_project_access)
    assert "resolve_strict_resource_access" in source
    assert "TOOROW_AUTH_MODE" in source
    assert "hold_access=True" in source


def test_a_missing_path_and_a_foreign_one_answer_identically() -> None:
    """Existence-hiding is inherited from the owner, not re-decided here."""
    source = inspect.getsource(_get_ai_path)

    assert "AiPathNotFound" in source
    assert "_not_found()" in source
    # And the refusal never names which of the two it was.
    assert "does not exist" not in source


def test_each_failure_mode_has_its_own_answer() -> None:
    listing = inspect.getsource(_list_ai_paths)
    reading = inspect.getsource(_get_ai_path)

    # 401 lives in the shared `_authorize` helper, which both handlers call
    # first: one gate, so an unauthenticated caller cannot reach one read
    # through a branch the other closed.
    gate = inspect.getsource(_authorize)
    assert "status_code=401" in gate and "Bearer token required" in gate
    for source in (listing, reading):
        assert "_authorize(request)" in source
        assert "_denied()" in source           # 403: known member, capability too low
        assert "_unavailable(" in source       # 503: fail closed, never a success
    assert "invalid_query" in listing          # 400: a limit outside its contract


def test_the_collection_read_is_bounded() -> None:
    """An unbounded read on evidence is how a screen becomes a tenant export."""
    source = inspect.getsource(_list_ai_paths)

    assert "MAX_LIMIT" in source
    assert MAX_LIMIT <= 200


def test_the_reads_never_recompute_the_assessment() -> None:
    """Two places deciding what a path is worth is two answers."""
    reading = inspect.getsource(_get_ai_path)

    assert 'path.get("assessment")' in reading
    assert "assess(" not in reading


def test_the_routes_are_mounted_on_the_application() -> None:
    """The last link: registered in the module AND spread into admin_api."""
    from pathlib import Path

    admin = Path(__file__).resolve().parents[2] / "core" / "admin_api.py"
    source = admin.read_text(encoding="utf-8")

    assert "from core.ai_paths_api import AI_PATH_ROUTES" in source
    assert "*AI_PATH_ROUTES," in source


# ---------------------------------------------------------------------------
# Live finding A1 (2026-08-05): disabled-auth local mode
# ---------------------------------------------------------------------------


def _mock_conn(fetchone=("org_1",)):
    from unittest.mock import MagicMock  # noqa: PLC0415

    conn = MagicMock()
    conn.__enter__.return_value = conn
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.return_value = fetchone
    return conn


def test_disabled_auth_mode_keeps_the_local_developer_workflow(monkeypatch) -> None:
    """Under `make dev` (TOOROW_AUTH_MODE=disabled) every ai-paths route answered
    404 while the rest of the Context surfaces answered -- the local Knowledge
    Graph screen was broken by default. The anonymous developer identity gets
    the same documented bypass here as everywhere else, with the org read from
    the Project row."""
    from unittest.mock import patch  # noqa: PLC0415

    from core.main import build_asgi_app  # noqa: PLC0415
    from starlette.testclient import TestClient  # noqa: PLC0415

    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")

    app = build_asgi_app()
    client = TestClient(app)

    with (
        patch("core.admin_api._check_auth", return_value=(True, "anonymous")),
        patch("core.ai_paths_api.request_connection", return_value=_mock_conn()),
        patch("core.ai_paths.list_paths", return_value=[]),
    ):
        resp = client.get("/api/projects/proj_A/context/ai-paths")

    assert resp.status_code == 200
    body = resp.json()
    assert body["schema_version"] == "ai-path-collection.v1"
    assert body["paths"] == []


def test_disabled_auth_mode_unknown_project_is_a_plain_404(monkeypatch) -> None:
    """The bypass is not an existence waiver: an unknown Project stays the
    same non-disclosing 404 the strict seam would have answered."""
    from unittest.mock import patch  # noqa: PLC0415

    from core.main import build_asgi_app  # noqa: PLC0415
    from starlette.testclient import TestClient  # noqa: PLC0415

    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")

    app = build_asgi_app()
    client = TestClient(app)

    with (
        patch("core.admin_api._check_auth", return_value=(True, "anonymous")),
        patch("core.ai_paths_api.request_connection", return_value=_mock_conn(None)),
        patch("core.ai_paths.list_paths", return_value=[]),
    ):
        resp = client.get("/api/projects/proj_GONE/context/ai-paths")

    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_an_inspection_on_an_unknown_path_is_a_404_not_a_503(monkeypatch) -> None:
    """Live finding (2026-08-05): an ai_path_id pointing at no recorded walk
    made the format CHECK / the FK bite at INSERT time, and the generic net
    turned it into a 503 'unavailable' -- which reads as an outage. It is the
    same non-disclosing 404 as the reads."""
    from unittest.mock import patch  # noqa: PLC0415

    import psycopg.errors  # noqa: PLC0415
    from core.main import build_asgi_app  # noqa: PLC0415
    from starlette.testclient import TestClient  # noqa: PLC0415

    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")

    app = build_asgi_app()
    client = TestClient(app)

    with (
        patch("core.admin_api._check_auth", return_value=(True, "anonymous")),
        patch("core.ai_paths_api.request_connection", return_value=_mock_conn()),
        patch(
            "core.evidence_inspections.insert_inspection",
            side_effect=psycopg.errors.CheckViolation("ai_path_id format check"),
        ),
    ):
        resp = client.post(
            "/api/projects/proj_A/context/ai-paths/aip_inconnu/inspections",
            json={
                "kind": "evidence_drilldown_opened",
                "surface": "console",
                "displayed_state": "branches_listed",
                "branches_listed": 2,
            },
        )

    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


# ---------------------------------------------------------------------------
# The drift aggregate: /ai-paths/stats
# ---------------------------------------------------------------------------


_STATS = {
    "window_days": 30,
    "outcomes": {"succeeded": 12, "failed": 2, "refused": 1},
    "recording": 1,
    "verdicts": {"pass": 8, "fail": 2, "unverifiable": 1},
    "verdict_window": 200,
    "verdicts_assessed": 11,
}


def test_stats_is_not_swallowed_by_the_path_id_route(monkeypatch) -> None:
    """Starlette matches in declaration order: `/ai-paths/stats` declared after
    `/{path_id}` would answer 404 "AI Path not found" forever. This pins the
    registration order AND the payload shape."""
    from unittest.mock import patch  # noqa: PLC0415

    from core.main import build_asgi_app  # noqa: PLC0415
    from starlette.testclient import TestClient  # noqa: PLC0415

    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")

    app = build_asgi_app()
    client = TestClient(app)

    with (
        patch("core.admin_api._check_auth", return_value=(True, "anonymous")),
        patch("core.ai_paths_api.request_connection", return_value=_mock_conn()),
        patch("core.ai_paths.path_stats", return_value=dict(_STATS)),
    ):
        resp = client.get("/api/projects/proj_A/context/ai-paths/stats")

    assert resp.status_code == 200
    body = resp.json()
    assert body["schema_version"] == "ai-path-stats.v1"
    assert body["outcomes"] == {"succeeded": 12, "failed": 2, "refused": 1}
    assert body["verdicts"] == {"pass": 8, "fail": 2, "unverifiable": 1}
    # Le taux de verdicts est borne et le dit : jamais un taux « all time ».
    assert body["verdict_window"] == 200


def test_the_write_is_no_longer_the_only_thing_this_table_has() -> None:
    """`app.evidence_inspections` had two writers and NO production reader.

    A usage signal stored and shown to nobody is the shape `context-hub.md`
    refuses of the sibling store -- "kept and read by nothing ... a missing link
    nobody can repair". The route is the repair, so its absence is the failure
    this asserts, not its details.
    """
    route = _route(INSPECTION_SUMMARY)
    assert route is not None, (
        "no inspection summary route: the table stays write-only and the signal "
        "two surfaces record is read by nobody"
    )
    assert "GET" in route.methods
    for forbidden in ("POST", "PATCH", "PUT", "DELETE"):
        assert forbidden not in route.methods

    source = inspect.getsource(_inspection_summary)
    # It reads. A read that writes or commits decides for a caller that did not
    # ask -- and this one sits on an append-only table.
    for forbidden in ("INSERT INTO", "insert_inspection(", "record_inspection(", "commit()"):
        assert forbidden not in source, f"the summary read writes: {forbidden}"
    # Same grant, same connection, same refusal vocabulary as its neighbours.
    assert "_strict_project_access" in source
    assert 'minimum_capability="view"' in source
    assert "with request_connection(" in source
    assert "_denied()" in source and "_not_found()" in source
    assert "invalid_query" in source


def test_the_summary_segment_is_not_swallowed_by_the_path_id_route() -> None:
    """Declaration order is the whole guarantee here: `inspections/summary`
    below `/{path_id}` would be read as a path id plus a suffix and answer
    "AI Path not found" forever."""
    paths = [r.path for r in AI_PATH_ROUTES]

    assert paths.index(INSPECTION_SUMMARY) < paths.index(OBJECT)
    assert paths.index(INSPECTION_SUMMARY) < paths.index(INSPECTIONS)


_SUMMARY = {
    "window_days": 30,
    "inspections_scanned": 7,
    "inspections_total": 7,
    "window_truncated": False,
    "scan_row_limit": 5000,
    "kinds": {"branch_subtree_expanded": 5, "evidence_drilldown_opened": 2},
    "displayed_states": {
        "branches_listed": 4,
        "no_branch_judged": 1,
        "branches_not_recorded": 1,
        "unavailable": 1,
    },
    "top_steps": [
        {"ai_path_id": "aip_01ARZ3NDEKTSV4RRFFQ69G5FAV", "step_ordinal": 2, "inspections": 3}
    ],
    "top_steps_limit": 10,
}


def test_the_summary_answers_on_its_literal_address(monkeypatch) -> None:
    """The registration order pinned above, proven through the mounted app."""
    from unittest.mock import patch  # noqa: PLC0415

    from core.main import build_asgi_app  # noqa: PLC0415
    from starlette.testclient import TestClient  # noqa: PLC0415

    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")

    app = build_asgi_app()
    client = TestClient(app)

    with (
        patch("core.admin_api._check_auth", return_value=(True, "anonymous")),
        patch("core.ai_paths_api.request_connection", return_value=_mock_conn()),
        patch(
            "core.evidence_inspections.summarize_inspections",
            return_value=dict(_SUMMARY),
        ),
    ):
        resp = client.get("/api/projects/proj_EXAMPLE/context/ai-paths/inspections/summary")

    assert resp.status_code == 200
    body = resp.json()
    assert body["schema_version"] == "evidence-inspection-summary.v1"
    # The four states travel APART: folding "we could not read it" into "there
    # was nothing" is the one thing the four-value column exists to prevent.
    assert body["displayed_states"] == {
        "branches_listed": 4,
        "no_branch_judged": 1,
        "branches_not_recorded": 1,
        "unavailable": 1,
    }
    assert body["top_steps"][0]["step_ordinal"] == 2


def test_the_summary_validates_the_window(monkeypatch) -> None:
    from unittest.mock import patch  # noqa: PLC0415

    from core.main import build_asgi_app  # noqa: PLC0415
    from starlette.testclient import TestClient  # noqa: PLC0415

    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")

    app = build_asgi_app()
    client = TestClient(app)

    with patch("core.admin_api._check_auth", return_value=(True, "anonymous")):
        resp = client.get(
            "/api/projects/proj_EXAMPLE/context/ai-paths/inspections/summary?days=0"
        )

    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_query"


def test_stats_validates_the_window(monkeypatch) -> None:
    from unittest.mock import patch  # noqa: PLC0415

    from core.main import build_asgi_app  # noqa: PLC0415
    from starlette.testclient import TestClient  # noqa: PLC0415

    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")

    app = build_asgi_app()
    client = TestClient(app)

    with patch("core.admin_api._check_auth", return_value=(True, "anonymous")):
        resp = client.get("/api/projects/proj_A/context/ai-paths/stats?days=0")

    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_query"


# ---------------------------------------------------------------------------
# The DETAIL read carries the exact owner links (Story 49.6 AC7, lot 4)
# ---------------------------------------------------------------------------
#
# What this pins, and why it is a seam test rather than a unit one: the graph
# overlay already composed owner and Event references server-side, and the
# per-path detail composed neither -- so the SAME walk, read two ways, answered
# differently. The console then rendered every owner as plain text, and a path
# opened directly lost the Data deep-link the canvas gives. These assert the
# composition on the wire, which is the only place the two reads meet.


_PATH_WITH_OWNERS = {
    "id": "aip_1",
    "lifecycle": "finalized",
    "outcome": "succeeded",
    "actor": "person_1",
    "started_at": None,
    "ended_at": None,
    "execution_correlation": None,
    "model_ref": "model_x",
    "tool_catalog_version": "v1",
    "policy_snapshot": None,
    "policy_snapshot_hash": None,
    "content_hash": "sha256:abc",
    "assessment": None,
    "steps": [
        # A Datastream: `_OWNER_ROUTES` holds it, so the reference resolves.
        {
            "ordinal": 0,
            "step_kind": "tool_call",
            "tool_name": "get_daily_report",
            "outcome": "succeeded",
            "owner_workspace": "data",
            "owner_object_type": "datastream",
            "owner_object_id": "ds_1",
            "owner_version_id": None,
        },
        # A Skill: named, governed, and NO screen route registered for that
        # owner type. It must come back `unavailable` -- never a guessed address.
        {
            "ordinal": 1,
            "step_kind": "skill_step",
            "tool_name": "get_procedure",
            "outcome": "succeeded",
            "owner_workspace": "context-hub",
            "owner_object_type": "procedure",
            "owner_object_id": "proc_1",
            "owner_version_id": None,
        },
        # A bare tool call. It reached nothing governed and stays listed.
        {
            "ordinal": 2,
            "step_kind": "tool_call",
            "tool_name": "health",
            "outcome": "succeeded",
            "owner_workspace": None,
            "owner_object_type": None,
            "owner_object_id": None,
            "owner_version_id": None,
        },
        # An Event. Data-owned: its reference is whatever its OWNER proved.
        {
            "ordinal": 3,
            "step_kind": "tool_call",
            "tool_name": "briefing_context_event",
            "outcome": "succeeded",
            "owner_workspace": "context-hub",
            "owner_object_type": "context-event",
            "owner_object_id": "evt_1",
            "owner_version_id": None,
        },
    ],
}

_EVENT_OWNER_ROUTE = {
    "surface": "project",
    "workspace": "data",
    "section": "events",
    "global_surface": None,
    "global_section": None,
    "object_type": "event-configuration",
    "object_id": "ecfg_1",
    "tab": "usage",
    "action": None,
    "version_id": "ecv_1",
    "evidence_id": None,
}


def _read_path(monkeypatch, *, resolved_events, own_reactions=None):
    from unittest.mock import patch  # noqa: PLC0415

    from core.main import build_asgi_app  # noqa: PLC0415
    from starlette.testclient import TestClient  # noqa: PLC0415

    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    client = TestClient(build_asgi_app())

    with (
        patch("core.admin_api._check_auth", return_value=(True, "anonymous")),
        patch("core.ai_paths_api.request_connection", return_value=_mock_conn()),
        patch("core.ai_paths.load_path", return_value=dict(_PATH_WITH_OWNERS)),
        patch(
            "core.event_configurations.resolve_event_observation_references",
            return_value=resolved_events,
        ),
        # The Feedback OWNER answers what this caller recorded. The read under
        # test is the composition, not the SQL: the SQL is proved on a live
        # Postgres by `test_pg_ac8_the_detail_read_gives_the_caller_back_their_
        # own_reaction`.
        patch(
            "core.feedback_review.read_actor_path_step_reactions",
            return_value=own_reactions or {},
        ),
    ):
        return client.get("/api/projects/proj_EXAMPLE/context/ai-paths/aip_1")


def test_each_step_carries_the_exact_owner_reference_or_a_typed_absence(monkeypatch) -> None:
    """AC7: "ordered steps, exact versions, exact owner/Evidence links".

    The projection used to carry `owner_workspace/type/id/version` and NO route,
    so the console had two options: print the owner as text, or join those parts
    into an address itself -- which would make it a second registry of owner
    routes. Neither is a link. The reference is composed here.
    """
    resp = _read_path(
        monkeypatch,
        resolved_events={
            "evt_1": {
                "event_id": "evt_1",
                "binding_state": "linked",
                "event_type": "release",
                "event_date": "2026-08-01",
                "owner_route": dict(_EVENT_OWNER_ROUTE),
            }
        },
    )
    assert resp.status_code == 200
    steps = resp.json()["steps"]
    # The Procedure step is GOVERNED since AI-376 (Opus F3): the console declares
    # `context-procedure`, and the route table now knows it.
    assert [step["owner_reference_state"] for step in steps] == [
        "governed",
        "governed",
        "not_governed",
        "governed",
    ]

    # The Datastream: a complete, semantic reference. No browser URL is ever
    # stored or served -- the client builds the href from its own registry.
    datastream = steps[0]["owner_reference"]
    assert datastream["surface"] == "project"
    assert datastream["workspace"] == "data"
    assert datastream["section"] == "datastreams"
    assert datastream["object_type"] == "datastream"
    assert datastream["object_id"] == "ds_1"
    assert "href" not in datastream
    assert "url" not in datastream

    # The Skill step: named and unroutable. The identity survives, the link does
    # not exist, and nothing is invented in its place.
    procedure = steps[1]["owner_reference"]
    assert procedure["object_type"] == "context-procedure" and procedure["object_id"] == "proc_1"
    assert procedure["section"] == "skills-registry"
    assert steps[1]["owner_object_id"] == "proc_1"

    # The bare call: not a broken link, because there is nothing to link to.
    assert steps[2]["owner_reference"] is None

    # The Event: the reference its DATA OWNER proved, not one composed here.
    assert steps[3]["owner_reference"] == _EVENT_OWNER_ROUTE


def test_an_owner_the_caller_cannot_reach_is_unavailable_not_invented(monkeypatch) -> None:
    """The permission answer and the "it is gone" answer are the SAME word.

    `resolve_event_observation_references` scopes by project inside its WHERE,
    so an Event of another Project, a deleted one and one whose Datastream
    binding was never proven are all simply absent from the resolution. This
    read must turn that absence into `unavailable` -- never into a link, and
    never into a different sentence per cause, which would make the walk a way
    to ask whether an Event exists.
    """
    resp = _read_path(monkeypatch, resolved_events={})
    assert resp.status_code == 200
    body = resp.json()

    assert body["steps"][3]["owner_reference"] is None
    assert body["steps"][3]["owner_reference_state"] == "unavailable"
    assert body["event_references"] == [
        {"event_id": "evt_1", "ordinals": [3], "binding_state": "unavailable"}
    ]
    # Nothing of the Event travels with the refusal: no type, no date, no
    # Datastream, no version. Those are what the resolution withheld.
    for withheld in ("event_type", "event_date", "datastream_id", "version_number"):
        assert withheld not in body["event_references"][0]


def test_the_detail_and_the_overlay_name_the_same_events_through_one_composer() -> None:
    """ONE composer, never two.

    The graph overlay decides WHICH Events a walk crossed and in which order;
    the detail read calls the same projection and the same resolver rather than
    grouping the steps a second time. Two compositions would answer differently
    the first time one of them was fixed, and a reader comparing the canvas with
    the workbench would have no way to know which one lied.
    """
    from core.ai_paths_api import _compose_event_references, _get_ai_path, _graph_overlay

    for handler in (_get_ai_path, _graph_overlay):
        source = inspect.getsource(handler)
        assert "_compose_event_references(" in source, (
            f"{handler.__name__} composes Event references some other way"
        )
        assert "resolve_event_observation_references(" not in source, (
            f"{handler.__name__} resolves Events itself instead of through the composer"
        )
    assert "resolve_event_observation_references" in inspect.getsource(
        _compose_event_references
    )


def test_the_console_is_never_handed_the_parts_to_build_an_address_from() -> None:
    """The route registry is the SERVER's, and there is one of it.

    `_step_owner_reference` goes through `evidence_index.owner_route`, the same
    table `resolve_event_observation_references` and the Evidence graph consult.
    A second mapping -- here or in the browser -- is how a renamed route freezes
    into a dead address on one surface and not the other.
    """
    from core.ai_paths_api import _step_owner_reference

    source = inspect.getsource(_step_owner_reference)
    assert "owner_route(" in source
    for invented in ('"data"', '"governance"', '"analyze"'):
        assert invented not in source, "the composer names a workspace of its own"


# ---------------------------------------------------------------------------
# Story 49.6 AC8 -- the +/- annotation on a step, and the boundary it keeps.
#
# `context-hub.md:83-85` says a path can be "annotated with positive or negative
# feedback"; the amendment of 2026-08-30 (`context-hub.md:701-764`) says who owns
# that annotation: "an annotation initiated from an AI Path uses the Test-owned
# Feedback command and its reference; the Context Hub owns neither Feedback nor
# Events and adds no editor for them" (the door is idempotent per key; a change
# of mind is a NEW row and the reader takes the latest). Test's ownership is
# `analyze-and-test.md:193`. So the assertions
# below are as much about where the write is NOT as about where it is.
# ---------------------------------------------------------------------------


def test_ac8_the_ai_path_surface_owns_no_feedback_write() -> None:
    """A second door here would make Context Hub a second Feedback owner."""
    assert not [r for r in AI_PATH_ROUTES if "feedback" in r.path]

    source = inspect.getsource(sys.modules["core.ai_paths_api"])
    for forbidden in (
        "app.feedback_annotations",
        "app.feedback_reviews",
        "submit_ai_path_step_feedback",
        "submit_exact_feedback",
    ):
        assert forbidden not in source, f"ai_paths_api writes Feedback: {forbidden}"


def test_ac8_the_step_annotation_door_lives_in_the_test_namespace() -> None:
    from core.feedback_review_api import (
        _create_ai_path_step_feedback,
        feedback_review_routes,
    )

    door = next(
        (
            r
            for r in feedback_review_routes
            if r.path == "/api/projects/{project_id}/test/feedback/ai-path-steps"
        ),
        None,
    )
    assert door is not None, (
        "no Test-owned door for a step annotation: the Context Hub control would "
        "have nowhere to post, or Context Hub would grow a second Feedback writer"
    )
    assert set(door.methods) >= {"POST"}
    assert "GET" not in door.methods

    source = inspect.getsource(_create_ai_path_step_feedback)
    assert "submit_ai_path_step_feedback(" in source
    # The caller names a path, an ordinal, a polarity and a comment. Every owner
    # behind them is re-resolved server-side, so nothing else may be accepted.
    assert '{"ai_path_id", "step_ordinal", "polarity", "comment"}' in source
    assert "Idempotency-Key" in source


def test_ac8_the_detail_read_carries_the_callers_own_reaction(monkeypatch) -> None:
    """The screen must not ask again what this person already answered, so it
    must be TOLD the standing reaction -- the latest row, comment included.

    Without this the control could only ever be in its "not asked yet" state:
    every reload re-asked a question the person had already answered, and the
    console minted a fresh key each time, so a second row was written for one
    judgement. The reaction is composed by the Feedback owner and carried per
    step -- `null` where this person did not react, and never anybody else's.
    """
    resp = _read_path(
        monkeypatch,
        resolved_events={},
        own_reactions={
            1: {
                "polarity": "negative",
                "recorded_at": datetime(2026, 8, 30, 9, 0, 0),
                "comment": "this step read the wrong Skill version",
            }
        },
    )

    assert resp.status_code == 200
    steps = resp.json()["steps"]
    assert steps[1]["my_feedback"] == {
        "polarity": "negative",
        "recorded_at": "2026-08-30T09:00:00",
        "comment": "this step read the wrong Skill version",
    }
    # Every other step says so explicitly. A missing key would read on the screen
    # as "unknown", and an unknown reaction is asked for again.
    assert [step["my_feedback"] for step in steps if step["step_order"] != 1] == [
        None,
        None,
        None,
    ]


def test_ac8_the_reaction_read_is_keyed_on_the_step_ordinal_not_its_position() -> None:
    """AI-134 again, on a new field: the walk is projected in `ordinal` order and
    the ordinal is what the write door names. Keying `my_feedback` on the array
    index would attach a reaction to the neighbouring step the day a walk is
    served with a gap in its ordinals."""
    from core.ai_paths_api import _my_feedback

    reactions = {7: {"polarity": "positive", "recorded_at": None}}
    assert _my_feedback({"step_order": 7}, reactions)["my_feedback"] == {
        "polarity": "positive",
        "recorded_at": None,
        "comment": None,
    }
    assert _my_feedback({"step_order": 0}, reactions) == {"my_feedback": None}
    # A walk whose steps carry no ordinal at all cannot be keyed, and says so
    # rather than defaulting to the first reaction it finds.
    assert _my_feedback({"step_order": None}, reactions) == {"my_feedback": None}


def test_ac8_the_reaction_read_never_breaks_the_walk_it_decorates() -> None:
    """A reader who cannot be told what they already recorded must still be able
    to read the walk. The permissiveness stops here: the WRITE door is what must
    never be lenient, and a replay there answers with the stored receipt."""
    from unittest.mock import MagicMock  # noqa: PLC0415

    from core.ai_paths_api import _my_step_reactions

    conn = MagicMock()
    conn.cursor.side_effect = RuntimeError("feedback store unavailable")
    assert (
        _my_step_reactions(
            conn, org_id="org_1", project_id="proj_EXAMPLE", actor="person-1", path_id="aip_1"
        )
        == {}
    )
    # No org resolved => no scope => no read at all, rather than a read without
    # one.
    assert (
        _my_step_reactions(
            conn, org_id=None, project_id="proj_EXAMPLE", actor="person-1", path_id="aip_1"
        )
        == {}
    )


def test_ac8_an_unknown_step_answers_the_same_404_as_the_path_read() -> None:
    """One non-disclosure rule for the read and for the write on top of it: a
    foreign walk, a deleted one and an ordinal that never existed are the same
    answer, or the annotation door becomes a way to ask whether a walk exists."""
    from core.feedback_review import _ai_path_step_authority
    from core.feedback_review_api import _create_ai_path_step_feedback

    resolver = inspect.getsource(_ai_path_step_authority)
    assert resolver.count('FeedbackNotFound("Feedback target not found")') == 2
    assert "org_id = %s" in resolver and "project_id = %s" in resolver

    door = inspect.getsource(_create_ai_path_step_feedback)
    assert "except FeedbackNotFound:" in door
    assert "return _not_found()" in door
