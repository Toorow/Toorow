"""Seam tests for the Controls & Quality command routes (Story 49.4).

Mounted on `build_asgi_app()` rather than a hand-assembled router, because the
question these answer is "is the door actually there?" and a hand-built router
answers it about a router nobody serves.

Story 49.4 asks the production seam to prove route specificity, access and
non-disclosure, and that a consequential confirmation is a different authority
from a draft. Drift, expiry, replay and the single-use token are properties of
the service and are proved against a real database in
`test_controls_quality.py`; this file proves they are REACHABLE and correctly
guarded.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

_ROOT = "/api/projects/proj_seam/governance/controls-quality/change-sets"
_MONITORS = "/api/projects/proj_seam/governance/controls-quality/dq-monitors"
_RULE_SETS = "/api/projects/proj_seam/governance/controls-quality/rule-sets"


def _client(*, authorized=True, identity="seam@test"):
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    app = build_asgi_app()
    return TestClient(app, raise_server_exceptions=False), patch(
        "core.admin_api._check_auth", new=AsyncMock(return_value=(authorized, identity))
    )


@pytest.fixture()
def client():
    test_client, auth = _client()
    with auth, test_client as ready:
        yield ready


@pytest.fixture()
def client_unauth():
    test_client, auth = _client(authorized=False, identity="")
    with auth, test_client as ready:
        yield ready


ROUTES = (
    ("POST", _ROOT),
    ("GET", f"{_ROOT}/ccs_x"),
    ("POST", f"{_ROOT}/ccs_x/prepare"),
    ("POST", f"{_ROOT}/ccs_x/confirm"),
    # The fifth route: manual evaluation. Its predecessor, POST /api/dq/evaluate,
    # was unmounted in the same story; if this one is absent there is no manual
    # evaluation at all, which is why it is asserted here and not assumed.
    ("POST", f"{_MONITORS}/dqm_x/evaluations"),
    # The authoring door, one per act (`governance.md`, "A Rule Set version is
    # drafted, then published"). Absent, five of the eight Rule Set families are
    # readable and unwritable, which is completeness criterion [24].
    ("GET", f"{_RULE_SETS}/grs_x/versions"),
    ("POST", f"{_RULE_SETS}/grs_x/versions"),
    ("POST", f"{_RULE_SETS}/grs_x/versions/grsv_x/publication"),
)


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_every_route_is_mounted_and_is_not_a_404_shaped_hole(client, method, path):
    """A route that answers 404 for METHOD reasons is indistinguishable from absent."""
    response = client.request(method, path, json={})
    assert response.status_code != 405, f"{method} {path} is not accepted by its own route"
    # Any answer other than "no such route" proves the door exists. The specific
    # code depends on access resolution, which the next tests pin down.
    assert response.status_code in {200, 201, 401, 403, 404, 409, 422, 503}


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_every_route_requires_authentication(client_unauth, method, path):
    assert client_unauth.request(method, path, json={}).status_code == 401


def test_prepare_is_not_swallowed_by_the_identifier_route():
    """Route specificity, asserted rather than assumed.

    `/{change_set_id}` is declared AFTER `/{change_set_id}/prepare`. Reversed, a
    POST to `.../prepare` would match the identifier route with the literal id
    "ccs_x" and never reach prepare.
    """
    from core.controls_quality_api import _ROOT as TEMPLATE
    from core.controls_quality_api import CONTROLS_QUALITY_ROUTES

    paths = [route.path for route in CONTROLS_QUALITY_ROUTES]
    identifier = paths.index(f"{TEMPLATE}/{{change_set_id}}")
    for specific in (
        f"{TEMPLATE}/{{change_set_id}}/prepare",
        f"{TEMPLATE}/{{change_set_id}}/confirm",
    ):
        assert paths.index(specific) < identifier, f"{specific} must be declared first"


def test_confirm_requires_manage_while_drafting_requires_edit():
    """Visibility and action authority are separate (AC12).

    Read is `view`, drafting and preparing are `edit`, and confirming -- the one
    that publishes -- is `manage`. Collapsing them would let anyone who can draft
    also activate.
    """
    import inspect

    from core import controls_quality_api as api

    source = inspect.getsource(api)
    assert 'return await _with_project(request, "view", handler)' in source
    # Drafting, preparing, running an evaluation, adopting a prepared rule set,
    # composing a Rule Set version and putting one in force: six `edit`
    # authorities. Confirmation of a change set remains the only step that reads
    # a confirmation token and therefore keeps the stronger `manage` floor.
    assert source.count('return await _with_project(request, "edit", handler)') == 6
    assert 'return await _with_project(request, "manage", handler)' in source


def test_no_response_body_can_carry_the_confirmation_hash():
    """The stored hash is never selected, so it cannot leak through a read."""
    import inspect

    from core import controls_change_sets, controls_quality_api

    serialized = inspect.getsource(controls_quality_api._serialize)
    assert "confirmation_token_hash" not in serialized
    # `read_change_set` selects an explicit column list that excludes it.
    assert "confirmation_token_hash" not in str(controls_change_sets._COLUMNS)


def test_evidence_responses_are_no_store(client):
    """A cached impact is a stale impact, and a stale impact is a wrong one."""
    response = client.get(f"{_ROOT}/ccs_missing")
    assert response.headers.get("Cache-Control") == "no-store"


def test_manual_evaluation_is_not_a_background_thread():
    """AC7, asserted against the code rather than against a comment.

    The retired route submitted to a `ThreadPoolExecutor` and answered 202 before
    anything ran. The replacement must go through `execute_operation`, and must
    pass the SCHEDULER's evaluator rather than defining a second one.
    """
    import inspect

    from core import controls_quality_api, dq_governance

    route_source = inspect.getsource(controls_quality_api._evaluate)
    # The CALL, not the word: the docstring names the thing it replaced.
    assert "ThreadPoolExecutor(" not in route_source
    assert "governed_evaluator" in route_source, "manual evaluation must use the shared evaluator"

    service_source = inspect.getsource(dq_governance.evaluate_monitor)
    assert "execute_operation" in service_source


def test_the_retired_dq_evaluate_route_has_no_door_left():
    """The replacement is only a replacement if the original is gone.

    Asserted on `core.admin_api.router`, which is the object `core.routing`
    forwards every `/api/*` request to -- not on a locally assembled router,
    which would answer the question about something nobody serves.
    """
    from core.admin_api import router

    leftovers = sorted(
        path
        for path in (getattr(route, "path", "") for route in router.routes)
        if path.startswith("/api/dq/")
    )
    assert leftovers == [], leftovers


def test_confirming_through_the_production_route_runs_an_owner_command():
    """The defect this closes was invisible from every route test.

    Every seam assertion above passed while `confirm_change_set` was called
    WITHOUT its `apply` callable: the route existed, required `manage`, refused
    an unauthenticated caller and answered no-store -- and published nothing.
    A door is not a command.
    """
    import inspect

    from core import controls_quality_api

    source = inspect.getsource(controls_quality_api._confirm)
    assert "apply=owner_command(" in source, "confirm runs no owner command"

    # And the refusal path is separated from an outage: an intent the owner
    # rejects is 422, not the 503 the generic handler would have produced.
    module_source = inspect.getsource(controls_quality_api)
    assert "except OwnerCommandError as exc:" in module_source
    assert module_source.index("except OwnerCommandError") < module_source.index(
        "except Exception as exc:"
    ), "the generic handler would swallow an owner refusal into a 503"


def test_every_governed_object_type_has_an_owner_command():
    """A change set that can be opened for a type but never applied is a trap."""
    from core.controls_change_sets import OBJECT_TYPES
    from core.controls_owner_commands import _HANDLERS

    assert set(OBJECT_TYPES) == set(_HANDLERS), (
        f"openable: {sorted(OBJECT_TYPES)}, appliable: {sorted(_HANDLERS)}"
    )


def test_every_rule_set_profile_is_reachable_from_the_production_route():
    """The registry is populated by IMPORT, so a route that imports nothing sees none.

    Measured before this was fixed: importing `core.controls_quality_api` -- the
    exact module the app mounts -- and calling `registered_profiles()` returned
    `()`. The confirm route was mounted, authorized, drift-checked, and would
    have refused every Rule Set publication with "no Rule Set profile is
    registered" for profiles that exist in the tree.

    Run in a subprocess so the assertion is about a COLD import. In-process, any
    earlier test that touched a profile module would have populated the registry
    and made this pass for the wrong reason.
    """
    import subprocess
    import sys

    probe = (
        "import core.controls_quality_api;"
        "from core.governance_rule_sets import registered_profiles;"
        "print(','.join(sorted(p.family for p in registered_profiles())))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    )
    families = set(result.stdout.strip().split(","))
    assert {"tax_fee", "dq_policy", "metric_reconciliation"} <= families, families


def test_every_profile_module_is_listed_in_the_loader():
    """A new profile module that forgets to join the list fails HERE, not in production.

    Without this, the omission surfaces as a 422 for one family only, on one
    route, for the one Project that tried -- which is the hardest possible way
    to find it.
    """
    import pathlib
    import re

    from core.governance_rule_sets import _PROFILE_MODULES

    core_dir = pathlib.Path(__file__).resolve().parents[2] / "core"
    registering = set()
    for path in core_dir.glob("*.py"):
        source = path.read_text(encoding="utf-8", errors="replace")
        # A CALL to register_profile, not the definition and not an import line.
        if re.search(r"^\s*register_profile\(", source, re.MULTILINE):
            registering.add(f"core.{path.stem}")

    assert registering == set(_PROFILE_MODULES), (
        f"registering: {sorted(registering)}, listed: {sorted(_PROFILE_MODULES)}"
    )
