"""Story 50.4 -- AC10 and AC11: the routes, their ORDER, and the absence of a bypass.

WHAT THIS FILE CAN AND CANNOT PROVE ON ITS OWN. `server/core/admin_api.py` belongs
to another session (the "Reparations review 48/49" row of `SESSIONS.md`), so this
story delivers `visualization_spec_routes` and the orchestrator adds the two mount
lines. Until it does, `test_seam_the_visualization_routes_are_actually_mounted`
FAILS and the behaviour seams below skip with that exact reason. One red, stated
once, is the honest shape: a handler test alone stays green while every request
answers 404, which has happened in this repository before.

Asserting "not 405" is not a mount proof either -- an unmounted path answers 404,
not 405 -- so the probe reads a real response from the built application.

THE ORDER TEST IS NOT DECORATION. Starlette matches in declaration order. A suite
that only checks that all seven routes resolve passes on a list that has silently
been reordered, at which point `/visualizations/{id}/versions` is captured by
`/visualizations/{id}` and `versions` becomes an id.
"""

from __future__ import annotations

import inspect
import json

import pytest
from core.visualization_families import registry_payload
from core.visualization_specs import (
    PROPOSED_BY,
    VisualizationSpecRefused,
    create_visualization_spec_version,
    normalize_document,
)
from core.visualization_specs_api import visualization_spec_routes

from tests.core.test_visualization_spec_deny_list import CORPUS

PROJECT = "proj_EXAMPLE"
_BASE = f"/api/projects/{PROJECT}/analyze"


# ---------------------------------------------------------------------------
# AC11 -- exactly seven routes, in exactly this declared order.
# ---------------------------------------------------------------------------

#: Transcribed from AC11, in AC11's order. Written out rather than derived, so
#: this test can DISAGREE with the module.
EXPECTED_ORDER = (
    ("GET", f"{_BASE}/visualization-families"),
    ("GET", f"{_BASE}/visualization-options"),
    ("POST", f"{_BASE}/visualizations"),
    ("POST", f"{_BASE}/visualizations/{{visualization_id}}/versions"),
    ("GET", f"{_BASE}/visualizations/{{visualization_id}}"),
    (
        "POST",
        f"{_BASE}/visualization-spec-versions/{{visualization_spec_version_id}}/validate",
    ),
    ("GET", f"{_BASE}/visualization-spec-versions/{{visualization_spec_version_id}}"),
)


def declared() -> list[tuple[str, str]]:
    out = []
    for route in visualization_spec_routes:
        methods = sorted(m for m in route.methods if m not in {"HEAD", "OPTIONS"})
        path = route.path.replace("/api/projects/{project_id}", f"/api/projects/{PROJECT}")
        out.append((methods[0], path))
    return out


def test_the_family_exposes_exactly_seven_routes():
    assert len(visualization_spec_routes) == 7


def test_the_declared_route_order_is_the_contract():
    """The regression AC11 asks for by name."""
    assert tuple(declared()) == EXPECTED_ORDER


def test_the_specific_route_precedes_the_generic_one_in_both_pairs():
    """Stated a second way, so the reason survives a future reordering of the list."""
    paths = [path for _method, path in declared()]
    assert paths.index(f"{_BASE}/visualizations/{{visualization_id}}/versions") < paths.index(
        f"{_BASE}/visualizations/{{visualization_id}}"
    )
    assert paths.index(
        f"{_BASE}/visualization-spec-versions/{{visualization_spec_version_id}}/validate"
    ) < paths.index(
        f"{_BASE}/visualization-spec-versions/{{visualization_spec_version_id}}"
    )


def test_the_family_reuses_story_50_1s_analytical_base_rather_than_forking_it():
    from core.query_specs_api import _BASE as query_base

    for route in visualization_spec_routes:
        assert route.path.startswith(query_base), route.path


def test_the_module_reuses_story_50_1s_authorization_seam():
    """A second `_authorize` would be a second place for the role check to be wrong.

    Asserted on the IMPORTED NAMES, not on the text of one import line. The
    original spelling pinned a single-line `from ... import a, b, c, d`, so
    adding a fifth name -- `analyze_connection`, the shared RLS-floor seam --
    reflowed it and failed a test about authorization for a reason that had
    nothing to do with authorization. What matters is that these names come from
    Story 50.1's module and that this module defines no seam of its own.
    """
    import ast

    import core.visualization_specs_api as module

    source = inspect.getsource(module)
    imported = {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module == "core.query_specs_api"
        for alias in node.names
    }
    assert {"_BASE", "_NOT_FOUND", "_authorize", "_json_body"} <= imported, (
        f"the shared seam is not reused; imported from core.query_specs_api: {imported}"
    )
    assert "async def _authorize" not in source


def test_reads_authorize_viewer_and_writes_authorize_member():
    import core.visualization_specs_api as module

    source = inspect.getsource(module)
    assert source.count('_authorize(request, "member")') == 2, "the two writes"
    assert source.count('_authorize(request, "viewer")') == 5, "the five reads"


# ---------------------------------------------------------------------------
# AC10 -- there is no bypass, anywhere in the request surface.
# ---------------------------------------------------------------------------

FORBIDDEN_PARAMETERS = (
    "trusted",
    "skip_validation",
    "raw",
    "passthrough",
    "escape_hatch",
    "force",
    "unsafe",
    "bypass",
)


@pytest.mark.parametrize("parameter", FORBIDDEN_PARAMETERS)
def test_no_bypass_parameter_exists_anywhere_in_the_request_surface(parameter):
    import core.visualization_specs as service
    import core.visualization_specs_api as api

    for module in (api, service):
        source = inspect.getsource(module)
        assert f'"{parameter}"' not in source, f"{module.__name__} reads a `{parameter}` field"
        assert f"body.get('{parameter}'" not in source
        assert f"{parameter}=" not in source.replace(f"{parameter}=None", "")


def test_proposed_by_is_evidence_and_admits_exactly_two_values():
    assert PROPOSED_BY == frozenset({"person", "model"})


def test_a_third_proposed_by_value_is_refused(monkeypatch):
    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):  # pragma: no cover - never reached
            raise AssertionError("validation must refuse before any SQL")

    class _Conn:
        def cursor(self):
            return _Cursor()

    with pytest.raises(VisualizationSpecRefused) as exc:
        create_visualization_spec_version(
            _Conn(), org_id="org_EXAMPLE", project_id=PROJECT, validated=None,
            actor="test", proposed_by="connector",
        )
    assert exc.value.as_dict()["refusals"][0]["subject"] == "/proposed_by"


# ---------------------------------------------------------------------------
# AC10 -- the model path and the human path are byte-identical.
#
# WHAT THIS USED TO BE, AND WHY IT PROVED NOTHING. The first form of this test
# compared `refuse(case)` -- `normalize_document` then `check_shape_compatibility`
# -- against a verbatim re-implementation of the same two calls. It exercised no
# route, never touched `proposed_by`, and would have stayed green while a
# `proposed_by == "model"` branch skipped validation entirely. It compared a pure
# function to itself, 45 times.
#
# WHAT IT IS NOW. The SAME hostile document is driven through the real
# `_create_visualization` handler twice -- once as `person`, once as `model` --
# against a real Query Spec version in a real database, and the two 422 responses
# are compared BYTE FOR BYTE. Only two seams are substituted: `_authorize` (which
# resolves project membership and never reads an Analyze table) and
# `analyze_connection` (which acquires the connection). The validator, the
# refusal envelope, the status code and `proposed_by` are the production ones.
#
# If a model branch skipped validation, the model call would answer 201 and the
# bodies would differ. That is the failure this test can now produce.
# ---------------------------------------------------------------------------


def _post_request(project_id: str, body: dict) -> "Request":  # noqa: F821
    from starlette.requests import Request

    payload = json.dumps(body).encode("utf-8")

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/api/projects/{project_id}/analyze/visualizations",
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"",
            "path_params": {"project_id": project_id},
        },
        receive,
    )


def _drive_create(monkeypatch, chain, document: dict, proposed_by: str):
    """Run the REAL handler and return `(status, body_bytes)`."""
    import asyncio
    from contextlib import contextmanager

    import core.visualization_specs_api as module

    async def _allow(_request, _role="viewer"):
        return "tester@example.com", chain.org_id

    @contextmanager
    def _conn(_identity):
        yield chain.conn

    monkeypatch.setattr(module, "_authorize", _allow)
    monkeypatch.setattr(module, "analyze_connection", _conn)

    request = _post_request(
        chain.project_id,
        {
            "query_spec_version_id": chain.query_spec_version_id,
            "spec": document,
            "proposed_by": proposed_by,
        },
    )
    response = asyncio.run(module._create_visualization(request))
    return response.status_code, bytes(response.body)


@pytest.mark.parametrize("case", CORPUS, ids=[c.id for c in CORPUS])
def test_the_model_path_refuses_the_whole_corpus_identically_to_the_human_path(
    monkeypatch, live_postgres, case
):
    """The ENTIRE AC3 corpus, through the route, both ways."""
    from tests.core.test_visualization_specs_pg import Chain

    chain = Chain(live_postgres).build()
    human_status, human_body = _drive_create(monkeypatch, chain, case.document, "person")
    model_status, model_body = _drive_create(monkeypatch, chain, case.document, "model")

    assert human_status == 422, f"{case.id} was not refused on the human path"
    assert model_status == 422, f"{case.id} was not refused on the model path"
    assert model_body == human_body, case.id
    # And the refusal is the one the corpus names, so this stays a deny-list test
    # rather than "both paths failed somehow".
    envelope = json.loads(model_body)
    assert envelope["code"] == "visualization_spec_refused"
    assert any(
        r["code"] == case.code and r["subject"] == case.subject
        for r in envelope["refusals"]
    ), f"{case.id}: {[(r['code'], r['subject']) for r in envelope['refusals']]}"
    live_postgres.rollback()


def test_proposed_by_reaches_the_stored_version_on_the_accepting_path(
    monkeypatch, live_postgres
):
    """The other half: `proposed_by` is EVIDENCE, so it must actually be recorded.

    A refusal comparison alone would pass if `proposed_by` were dropped on the
    floor. This drives one ACCEPTED document as `model` and reads the column back.
    """
    from tests.core.test_visualization_specs_pg import Chain, bar_document

    chain = Chain(live_postgres).build()
    status, body = _drive_create(monkeypatch, chain, bar_document(), "model")
    assert status == 201, body
    created = json.loads(body)
    assert created["proposed_by"] == "model"
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT proposed_by FROM app.visualization_spec_versions WHERE id = %s",
            (created["id"],),
        )
        assert cur.fetchone()[0] == "model"
    live_postgres.rollback()


def test_the_two_paths_differ_in_exactly_one_recorded_field(monkeypatch, live_postgres):
    """Stated as a diff rather than as prose: the person and model rows of the
    same accepted document differ in `proposed_by` and in nothing else that
    carries meaning -- same family, same pin, same content hash."""
    from tests.core.test_visualization_specs_pg import Chain, bar_document

    chain = Chain(live_postgres).build()
    _s1, person = _drive_create(monkeypatch, chain, bar_document(), "person")
    _s2, model = _drive_create(monkeypatch, chain, bar_document(), "model")
    a, b = json.loads(person), json.loads(model)
    differing = {
        key
        for key in set(a) | set(b)
        if a.get(key) != b.get(key)
    }
    # `visualization_id`, `id` and `created_at` are identities and a timestamp;
    # `proposed_by` is the only field whose VALUE the two paths choose.
    assert "proposed_by" in differing
    assert differing <= {"visualization_id", "id", "created_at", "proposed_by"}, differing
    assert a["content_hash"] == b["content_hash"]
    assert a["family"] == b["family"]
    assert a["query_spec_version_id"] == b["query_spec_version_id"]
    live_postgres.rollback()


def test_a_model_proposal_carrying_a_full_echarts_option_is_refused():
    """`visualization-and-rendering.md:121-122`: generated code is not a contract."""
    proposal = {
        "spec_contract_version": "visualization-spec.v1",
        "schema_version": 1,
        "family": "bar",
        "bindings": {"measure": ["clicks"], "dimension": ["channel"]},
        "option": {
            "xAxis": {"type": "category", "data": ["a", "b"]},
            "yAxis": {"type": "value"},
            "series": [{"type": "bar", "data": [1, 2]}],
            "tooltip": {"formatter": "function (p) { return p.value }"},
        },
    }
    _n, refusals = normalize_document(proposal)
    assert "unknown_field" in [r.code for r in refusals]
    assert "/option" in [r.subject for r in refusals]


# ---------------------------------------------------------------------------
# AC11 -- `/visualization-options` cannot become a second member catalog.
# ---------------------------------------------------------------------------


def test_visualization_options_reads_the_pinned_version_and_never_the_full_matrix():
    """Structural, because it is the only assertion that survives a refactor: the
    handler's SQL selects the VERSION's own `spec`, and nothing in the module
    reads `semantic_compiled_artifacts`. A browser catalog assembled from the full
    matrix would offer members the query never selected (`50-2-...md:540`)."""
    import core.visualization_specs_api as module

    source = inspect.getsource(module)
    assert "app.query_spec_versions" in source
    assert "semantic_compiled_artifacts" not in source
    assert "queryability_matrix" not in source


def test_the_rail_metadata_read_cannot_widen_the_member_list(live_postgres):
    """AC7's five facets are served WITHOUT reopening the catalog.

    `load_member_presentation_metadata` reads `app.semantic_concept_versions`,
    which holds every concept in the Project — so the structural risk is real: a
    join written the other way round would hand the rail members the query never
    selected. It is keyed on the members it is GIVEN, and this asserts it against
    a Project that contains a concept the pinned version did not select.
    """
    from core.visualization_specs import load_member_presentation_metadata

    from tests.core.test_visualization_specs_pg import Chain, _uid

    chain = Chain(live_postgres).build()
    unselected = _uid("sc")
    with live_postgres.cursor() as cur:
        cur.execute(
            "INSERT INTO app.semantic_concepts (id, project_id, kind, name, created_by) "
            "VALUES (%s, %s, 'dimension', 'never_selected', 'test')",
            (unselected, chain.project_id),
        )

    metadata = load_member_presentation_metadata(
        live_postgres,
        project_id=chain.project_id,
        members=[{"id": "clicks", "version_id": "mv_1"}],
    )
    assert set(metadata) == {"clicks"}
    assert unselected not in metadata
    live_postgres.rollback()


def test_the_registry_payload_carries_no_renderer_vocabulary():
    """AC14: no renderer-library word may reach the client through the registry."""
    import json

    blob = json.dumps(registry_payload()).lower()
    for word in ("echarts", "d3", "xaxis", "yaxis", "dataset", "encode", "datakey",
                 "svg", "canvas", "widget", "module", "extension"):
        assert word not in blob, f"the registry leaks `{word}`"


# ---------------------------------------------------------------------------
# The mount seam.
#
# WHY THIS IS NOT AN HTTP PROBE ANY MORE, AND WHY THE EARLIER DIAGNOSIS WAS WRONG.
#
# The first form of this test asserted `status != 404` and then `status == 401`
# against a real `build_asgi_app()` response. Measured in this environment:
#
#   * an UNMOUNTED path answers Starlette's bare `404 Not Found` -- so
#     `!= 404` IS a working mount probe, and it passes;
#   * a MOUNTED path answers `500`, because `_check_auth` under the default
#     `TOOROW_AUTH_MODE=disabled` AUTHORIZES the request, which then proceeds to
#     a database this suite does not configure.
#
# So `== 401` could never pass, and the file carried seven permanent reds that
# said "unmounted" about seven routes that were mounted. A test that cannot go
# green is not a pending mount; it is a broken instrument, and it teaches the
# next reader to ignore a red.
#
# The mount question is a STRUCTURAL one -- "is this route object in the
# application's route list" -- so it is answered structurally: against
# `core.admin_api.router.routes`, by object identity. Deterministic, no database,
# no auth mode, and it cannot be satisfied by a route that merely looks alike.
# ---------------------------------------------------------------------------


def _mounted_routes():
    """The application's route list, read once from the module that assembles it."""
    from core.admin_api import router

    return list(router.routes)


def test_seam_the_probe_instrument_itself_works():
    """A route this repository already mounts (Story 50.1). If the instrument
    cannot see THIS one either, the absence it reports is its own."""
    from core.query_specs_api import query_spec_routes

    mounted = _mounted_routes()
    assert query_spec_routes, "Story 50.1 declares no routes; the fixture is wrong"
    for route in query_spec_routes:
        assert route in mounted, f"the instrument is broken: {route.path} reads as absent"


@pytest.mark.parametrize(
    "index", range(len(visualization_spec_routes)), ids=[p for _m, p in EXPECTED_ORDER]
)
def test_seam_the_visualization_routes_are_actually_mounted(index):
    """Each of the seven route OBJECTS is in the application's route list.

    Asserted by identity, not by path text: a second module declaring a
    same-looking path would satisfy a string comparison while this family stayed
    unmounted.
    """
    route = visualization_spec_routes[index]
    assert route in _mounted_routes(), (
        f"{route.path} is NOT mounted. Add to server/core/admin_api.py:\n"
        "  from core.visualization_specs_api import visualization_spec_routes\n"
        "  ... *visualization_spec_routes,"
    )


def test_seam_the_mounted_family_keeps_its_declared_order():
    """Mounting must not reorder the family. Starlette matches in declaration
    order, so `/visualizations/{id}/versions` landing after `/visualizations/{id}`
    inside the application list would make `versions` an id -- even though this
    module's own list is correct."""
    mounted = _mounted_routes()
    positions = [mounted.index(route) for route in visualization_spec_routes]
    assert positions == sorted(positions), (
        "the seven routes are mounted out of their declared order"
    )


def test_seam_the_family_is_mounted_exactly_once():
    """A second spread would shadow nothing today and everything the day one of
    the two lists changes."""
    mounted = _mounted_routes()
    for route in visualization_spec_routes:
        assert mounted.count(route) == 1, f"{route.path} is mounted more than once"
