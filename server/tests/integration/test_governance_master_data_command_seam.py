"""The Master Data command route exists, and its refusals are the right shape.

Story 49.2. `governance_surface_api` was read-only: the guard and the audit had
no way in. A command reachable only from a test is the defect this repository
keeps paying for, so these assert the ROUTE -- registration, method, capability
and each refusal code -- rather than the function behind it.

The capability assertion is the one that matters most: the read routes hold
`view`, and if this write reused it, anyone able to SEE a registry could retire
an identity that Data mappings, Analyze filters and Context links resolve
against.
"""

from __future__ import annotations

import inspect

from core.governance_surface_api import (
    GOVERNANCE_SURFACE_ROUTES,
    _master_data_convergence,
    _master_data_identity_create,
    _master_data_node_command,
)

COMMAND_PATH = "/api/projects/{project_id}/governance/master-data/nodes/{node_id}/commands"


def _command_route():
    return next((r for r in GOVERNANCE_SURFACE_ROUTES if r.path == COMMAND_PATH), None)


def test_the_command_route_is_registered_as_a_post() -> None:
    route = _command_route()

    assert route is not None, "the Master Data command has no route: it is unreachable"
    assert "POST" in route.methods
    assert "GET" not in route.methods


def test_the_command_route_is_ordered_before_the_read_routes_that_could_shadow_it() -> None:
    """`/objects/{object_type}/{object_id}` matches this shape; order decides."""
    paths = [r.path for r in GOVERNANCE_SURFACE_ROUTES]

    generic_object = (
        "/api/projects/{project_id}/governance/{section}"
        "/objects/{object_type}/{object_id}"
    )
    assert paths.index(COMMAND_PATH) < paths.index(generic_object)


def test_the_write_requires_edit_where_the_reads_require_view() -> None:
    source = inspect.getsource(_master_data_node_command)

    assert 'minimum_capability="edit"' in source
    assert 'minimum_capability="view"' not in source


def test_every_refusal_the_command_can_raise_has_a_distinct_answer() -> None:
    """Each failure mode is told apart. Collapsing them is how a 'could not
    check' starts rendering as a 'nothing depends on this'."""
    source = inspect.getsource(_master_data_node_command)

    # Live consumers: 409 carrying the named consumers, not a 500.
    assert "MasterDataCommandRefused" in source
    assert "status_code=409" in source
    # Unreadable evidence: fail closed, never a success.
    assert "MasterDataUnavailable" in source
    assert "master_data_evidence_unavailable" in source
    # Unknown action: 422, an input problem.
    assert "422" in source
    # Missing idempotency key: 428, because a server-minted key would make every
    # retry a new command.
    assert "428" in source
    assert "idempotency_key_required" in source
    # Missing rename precondition: 428 as well, and under its OWN code. A rename
    # that states no base is refused at the door rather than run without one
    # (`governance.md`, amendment of 2026-08-30).
    assert "expected_version_required" in source


def test_a_rename_that_states_no_base_is_refused_at_the_door() -> None:
    """The door is where "send back what you read" is made unskippable.

    Checking it only inside the command would leave the route free to default the
    field, which is how the precondition was lost the first time: a value nobody
    sent, compared against nothing, reading as protection.
    """
    source = inspect.getsource(_master_data_node_command)

    assert '"expected_version" not in body' in source
    # And it is PASSED only when stated: `body.get("expected_version")` alone
    # would turn "I forgot" into "I read no revision", which always passes for an
    # identity that has none.
    passes_only_when_stated = (
        '**({"expected_version": stated_version} if "expected_version" in body else {})'
    )
    assert passes_only_when_stated in source


def test_the_route_never_mints_its_own_idempotency_key() -> None:
    source = inspect.getsource(_master_data_node_command)

    assert "uuid" not in source.lower()
    assert "Idempotency-Key" in source


def test_the_surface_still_holds_its_three_reads() -> None:
    """Every write is an addition, never a replacement.

    Six reads: the three generic shapes, the Country workspace, the convergence
    PLAN added by the AC1 pass -- an operator must be able to see what a
    convergence would move before asking for it -- and the Semantic Model metric
    presets, added the same day by 49.3 (`554891c6`) without this count being
    told. Measured 2026-08-28 while re-pointing the fourteen readers: the count
    said five and the surface served six, so the guard had been red on a route
    ADDITION -- the one direction its own docstring says it does not exist to
    catch.

    Four writes: the Country command, the node command, the convergence command
    and -- since the cutover of 2026-08-25 -- the creation of a business
    identity. A number that goes DOWN here is a route somebody removed, which is
    the only thing this count exists to catch.
    """
    methods = [sorted(r.methods) for r in GOVERNANCE_SURFACE_ROUTES]

    assert sum(1 for m in methods if "GET" in m) == 6
    assert sum(1 for m in methods if "POST" in m) == 4


# ---------------------------------------------------------------------------
# The creation door (cutover of 2026-08-25). Until it existed, the sentence the
# legacy writers refuse with -- "converge, THEN create it in Master Data" --
# named a gesture with no address.
# ---------------------------------------------------------------------------

CREATE_PATH = "/api/projects/{project_id}/governance/master-data/nodes"


def _create_route():
    return next((r for r in GOVERNANCE_SURFACE_ROUTES if r.path == CREATE_PATH), None)


def test_the_creation_route_is_registered_as_a_post() -> None:
    route = _create_route()

    assert route is not None, "the refusal names a gesture with no route behind it"
    assert "POST" in route.methods
    assert "GET" not in route.methods


def test_the_creation_holds_edit_like_the_command_and_not_view() -> None:
    source = inspect.getsource(_master_data_identity_create)

    assert 'minimum_capability="edit"' in source
    assert 'minimum_capability="view"' not in source


def test_every_master_data_write_route_commits_what_it_wrote() -> None:
    """`db.get_connection` does not commit -- it closes, and psycopg rolls an
    open transaction back on close (`db.py:67-81`). A write route that forgets
    this answers 200 and leaves the database untouched. Measured 2026-08-25:
    the node command and the CONVERGENCE both did exactly that (`governance.md`,
    amendment of the day) -- so the convergence, the instance the defect was
    found on, is pinned here beside the other two."""
    assert "conn.commit()" in inspect.getsource(_master_data_identity_create)
    assert "conn.commit()" in inspect.getsource(_master_data_node_command)
    assert "conn.commit()" in inspect.getsource(_master_data_convergence)


def test_the_creation_refusals_keep_their_own_codes() -> None:
    """The convergence refusal and a taken short code are both 409 and are
    repaired by different people on different screens, so the console has to be
    able to tell them apart."""
    source = inspect.getsource(_master_data_identity_create)

    assert "exc.as_dict()" in source, "the code the command chose must travel"
    assert "status_code=409" in source
    assert "428" in source and "idempotency_key_required" in source
    assert "422" in source
    assert "master_data_evidence_unavailable" in source


def test_the_creation_never_trusts_a_client_supplied_organization() -> None:
    source = inspect.getsource(_master_data_identity_create)

    assert "decision.org_id" in source
    assert 'body.get("org_id")' not in source
