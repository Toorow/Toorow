"""Project-scoped Workbench API seams for Story 47.5."""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")


@contextmanager
def _connection():
    yield MagicMock()


def _client() -> TestClient:
    from core.main import build_asgi_app

    return TestClient(build_asgi_app(), raise_server_exceptions=False)


def _authorized():
    return (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_1"))),
        patch("core.admin_api._require_datastream_role", return_value=None),
        patch("core.db.get_connection", side_effect=_connection),
    )


def test_base_workbench_is_project_and_datastream_scoped() -> None:
    payload = {"schema": "datastream_workbench.header.v1", "identity": {"datastream_id": "ds_1"}}
    with (
        _authorized()[0],
        _authorized()[1] as role,
        _authorized()[2],
        patch("core.datastream_workbench_api.read_workbench", return_value=payload) as read,
    ):
        response = _client().get("/api/projects/proj_1/datastreams/ds_1/workbench")

    assert response.status_code == 200
    assert response.json() == payload
    assert read.call_args.kwargs["project_id"] == "proj_1"
    assert read.call_args.kwargs["datastream_id"] == "ds_1"
    assert role.call_args.kwargs["datastream_id"] == "ds_1"


def test_every_tab_route_passes_its_exact_server_owned_tab() -> None:
    # THE REGISTRY, NOT A COPY OF IT. This listed the six tabs by hand, so the
    # seventh -- `cost`, opened by the `tax_fees` capability (story 58.6) -- could
    # have been mounted with no route and this test would still have been green
    # on the six it knew about.
    from core.datastream_workbench import TABS

    assert "cost" in TABS
    for tab in TABS:
        payload = {"schema": f"datastream_workbench.{tab}.v1", "tab": tab}
        with (
            _authorized()[0],
            _authorized()[1],
            _authorized()[2],
            patch("core.datastream_workbench_api.read_tab", return_value=payload) as read,
        ):
            response = _client().get(f"/api/projects/proj_1/datastreams/ds_1/workbench/{tab}")
        assert response.status_code == 200
        assert response.json()["tab"] == tab
        assert read.call_args.kwargs["tab"] == tab


def test_denied_workbench_does_not_read_evidence() -> None:
    from starlette.responses import JSONResponse

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_1"))),
        patch(
            "core.admin_api._require_datastream_role",
            return_value=JSONResponse({"code": "not_found"}, 404),
        ),
        patch("core.db.get_connection", side_effect=_connection),
        patch("core.datastream_workbench_api.read_workbench") as read,
    ):
        response = _client().get("/api/projects/proj_other/datastreams/ds_other/workbench")

    assert response.status_code == 404
    read.assert_not_called()


# ---------------------------------------------------------------------------
# Story 58.6 -- the tab the capability opens.
# ---------------------------------------------------------------------------


def test_the_cost_route_is_mounted_and_answers_its_own_schema() -> None:
    from core.datastream_workbench import TAB_SCHEMAS

    payload = {
        "schema": TAB_SCHEMAS["cost"],
        "tab": "cost",
        "project_id": "proj_1",
        "datastream_id": "ds_1",
        "evidence": {"state": "capability_inactive"},
    }
    with (
        _authorized()[0],
        _authorized()[1] as role,
        _authorized()[2],
        patch("core.datastream_workbench_api.read_tab", return_value=payload) as read,
    ):
        response = _client().get("/api/projects/proj_1/datastreams/ds_1/workbench/cost")

    assert response.status_code == 200
    assert response.json()["schema"] == "datastream_workbench.cost.v1"
    assert response.json()["tab"] == "cost"
    assert read.call_args.kwargs["tab"] == "cost"
    # The route is scoped like every other tab: the guard sees the stream id.
    assert role.call_args.kwargs["datastream_id"] == "ds_1"


def test_an_unreachable_warehouse_is_an_opaque_503_and_never_an_empty_cascade() -> None:
    from core.datastream_workbench_cost import (
        COST_CASCADE_UNAVAILABLE_MESSAGE,
        CostCascadeUnavailable,
    )

    with (
        _authorized()[0],
        _authorized()[1],
        _authorized()[2],
        patch(
            "core.datastream_workbench_api.read_tab",
            side_effect=CostCascadeUnavailable(),
        ),
    ):
        response = _client().get("/api/projects/proj_1/datastreams/ds_1/workbench/cost")

    assert response.status_code == 503
    body = response.json()
    # ITS OWN SENTENCE, not the Workbench's catch-all: a reader must be able to
    # tell an unreachable warehouse from a Project that published no rule.
    assert body["message"] == COST_CASCADE_UNAVAILABLE_MESSAGE
    assert body["code"] == "cost_cascade_unavailable"
    # And nothing that could be read as a cascade with nothing in it.
    assert "evidence" not in body and "cascade" not in body


# ---------------------------------------------------------------------------
# Story 58.9 -- the FIVE capability states, on the header the console loads,
# read from real rows.
# ---------------------------------------------------------------------------

_skip_without_dsn = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Workbench header walk skipped",
)

IDENTITY = "reader@example.com"
AUTHOR = "story-58-9"


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


@pytest.fixture
def a_stream(live_postgres):
    """One org, one project, one Datastream -- AND the project/flux link.

    `app.project_flux` IS THE PRECONDITION, not a detail of the fixture.
    `_read_base_record` joins it (`datastream_workbench.py:519`), the disposable
    cluster carries zero rows in it, and without one seeded here `read_workbench`
    raises `WorkbenchNotFound` for every Datastream -- a test that never seeded it
    would pass while reading nothing at all.
    """
    conn = live_postgres
    org_id, project_id, ds_id = _id("org_"), _id("proj_"), _id("ds_")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by, status)"
            " VALUES (%s, %s, %s, %s, 'active')",
            (org_id, org_id, org_id, AUTHOR),
        )
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, created_by, org_id, status)"
            " VALUES (%s, %s, %s, %s, %s, 'active')",
            (project_id, project_id, project_id, AUTHOR, org_id),
        )
        cur.execute(
            "INSERT INTO app.datastreams (id, project_id, name, module_name,"
            " source_kind, enabled, created_by, org_id)"
            " VALUES (%s, %s, 'Stream', 'meta-ads', 'connector_pull', TRUE, %s, %s)",
            (ds_id, project_id, AUTHOR, org_id),
        )
        cur.execute(
            "INSERT INTO app.project_flux (project_id, flux_id, org_id)"
            " VALUES (%s, %s, %s)",
            (project_id, ds_id, org_id),
        )
    try:
        yield {"conn": conn, "org_id": org_id, "project_id": project_id, "ds_id": ds_id}
    finally:
        conn.rollback()


def _capability(ids, capability_key: str, state: str, availability: str) -> None:
    """Move one capability of this Project to a state.

    An UPSERT, not an INSERT: `trg_projects_seed_capabilities` already wrote the
    five rows the moment the Project was created, so an INSERT here collides on
    the primary key. That trigger is also why the `unset` case below has to
    DELETE -- a Project the control plane never wrote is not something this
    schema produces by itself any more.
    """
    with ids["conn"].cursor() as cur:
        cur.execute(
            "INSERT INTO app.project_capabilities"
            " (project_id, capability_key, availability, state)"
            " VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (project_id, capability_key) DO UPDATE"
            "   SET availability = EXCLUDED.availability, state = EXCLUDED.state",
            (ids["project_id"], capability_key, availability, state),
        )


def _workbench(ids):
    """Drive the real route against the fixture's own transaction."""
    from core.main import build_asgi_app

    connection = ids["conn"]

    @contextmanager
    def _open(*_a, **_k):
        yield connection

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, IDENTITY))),
        patch("core.admin_api._require_datastream_role", return_value=None),
        patch("core.db.get_connection", side_effect=_open),
    ):
        client = TestClient(build_asgi_app(), raise_server_exceptions=False)
        return client.get(
            f"/api/projects/{ids['project_id']}/datastreams/{ids['ds_id']}/workbench"
        )


@_skip_without_dsn
def test_every_capability_state_travels_on_the_header_the_console_loads(a_stream) -> None:
    """The tab band cannot be capability-aware without them, and cannot wait.

    A second request for the switch leaves a window in which the band is drawn
    without the answer -- and a band that draws a conditional tab for one frame
    has shown a tab the amendment forbids. Story 58.9 widened it from one entry
    to one per capability, because the Overview shows a row per capability and a
    single entry would have shown one module out of all of them.

    NO COUNT IS ASSERTED HERE, on purpose. This test named five and pinned five,
    and story 61.5 made it six -- so it went red for a change that was correct.
    The list below IS the assertion; a seventh capability adds one line to it and
    nothing else.
    """
    _capability(a_stream, "country", "ready", "optional")
    _capability(a_stream, "currency_fx", "draft", "always_present")
    _capability(a_stream, "reporting_timezone", "draft", "always_present")
    _capability(a_stream, "tax_fees", "degraded", "optional")
    _capability(a_stream, "competitors", "disabled", "optional")
    # Story 61.5. `not_applicable` everywhere is what its compiler returns while
    # no placement store exists, and `ready` is the state that follows from it.
    _capability(a_stream, "placement_mapping", "ready", "optional")
    # Epic 70 (2026-08-27) added the seventh key, `analytics_alignment`, seeded
    # `disabled` by the trigger; this list is EXACT on purpose and had not moved.

    response = _workbench(a_stream)

    assert response.status_code == 200, response.text
    tabs = response.json()["capability_tabs"]
    assert [entry["capability_key"] for entry in tabs] == [
        "country", "currency_fx", "reporting_timezone", "tax_fees", "competitors",
        "placement_mapping", "analytics_alignment",
    ]
    assert [entry["state"] for entry in tabs] == [
        "ready", "draft", "draft", "degraded", "disabled", "ready", "disabled",
    ]
    # TWO capabilities open a tab, and the others say so with `null`.
    assert [entry["tab"] for entry in tabs] == [
        None, None, None, "cost", None, "placements", None,
    ]
    assert [entry["open"] for entry in tabs] == [
        True, False, False, True, False, True, False,
    ]
    # The two the CHECK of migration 131 refuses to disable are marked as such,
    # which is what tells the console it may not draw a switch for them.
    assert [entry["availability"] for entry in tabs] == [
        "optional", "always_present", "always_present", "optional", "optional",
        "optional", "optional",
    ]


@_skip_without_dsn
def test_a_project_with_no_capability_row_reads_unset_on_every_one(a_stream) -> None:
    """No row is not `disabled`: nobody decided anything, the seed never ran."""
    with a_stream["conn"].cursor() as cur:
        cur.execute(
            "DELETE FROM app.project_capabilities WHERE project_id=%s",
            (a_stream["project_id"],),
        )

    response = _workbench(a_stream)

    assert response.status_code == 200, response.text
    tabs = response.json()["capability_tabs"]
    # Derived, never a literal: 61.5 made this six and a literal would have gone
    # red for a correct change (it did).
    from core.project_settings import CAPABILITY_SPECS

    assert len(tabs) == len(CAPABILITY_SPECS)
    assert {entry["state"] for entry in tabs} == {"unset"}
    assert all(entry["open"] is False for entry in tabs)


@_skip_without_dsn
def test_the_overview_tab_counts_the_checks_placed_on_this_datastream(a_stream) -> None:
    """Story 58.9, arbitrage 4 -- the `Check` stage's object, measured.

    Zero monitors exist on either database, so the value this walk proves is `0`
    with a real COUNT behind it; the screen renders its invitation for that, never
    the digit. An absent key would mean the count could not be read at all.
    """
    from core.datastream_workbench import read_tab

    evidence = read_tab(
        a_stream["conn"],
        project_id=a_stream["project_id"],
        datastream_id=a_stream["ds_id"],
        tab="overview",
    )["evidence"]

    assert evidence["dq_monitors"] == {"count": 0, "published": 0}

    monitor_id = "dqm_" + "0123456789ABCDEFGHJKMNPQRS"
    with a_stream["conn"].cursor() as cur:
        cur.execute(
            "INSERT INTO app.dq_monitors (id, org_id, project_id, name, label,"
            " target_kind, target_id, lifecycle_status, created_by)"
            " VALUES (%s, %s, %s, 'null_rate', 'Null rate', 'datastream', %s,"
            "         'published', %s)",
            (monitor_id, a_stream["org_id"], a_stream["project_id"],
             a_stream["ds_id"], AUTHOR),
        )

    evidence = read_tab(
        a_stream["conn"],
        project_id=a_stream["project_id"],
        datastream_id=a_stream["ds_id"],
        tab="overview",
    )["evidence"]
    assert evidence["dq_monitors"] == {"count": 1, "published": 1}


# ---------------------------------------------------------------------------
# Story 61.3 -- the two addresses the `Placements` tab gained, through the REAL
# router.
#
# 61.2 measured that no write of this Workbench had a route test outside
# `tests/core`, and named it AI-264. This section starts closing it: the
# suggestion reading and the confirmation are driven here by the mounted app, so
# "the route exists, it is scoped, and it refuses" is proven by the router rather
# than by a direct call to a function.
# ---------------------------------------------------------------------------

_SUGGESTIONS = (
    "/api/projects/proj_1/datastreams/ds_1/workbench/placements/suggestions?plan_id=plan_1"
)
_MATCHES = "/api/projects/proj_1/datastreams/ds_1/workbench/placements/matches"


def _scope(*, active: bool = True, belongs: bool = True):
    """The three checks `_placement_scope` makes, doubled one by one.

    Doubled SEPARATELY on purpose: a single switch would make every refusal below
    the same refusal, and what has to be provable is that each one fires alone.
    """
    from core.project_capability_states import CAPABILITY_STATE_UNSET

    return (
        patch(
            "core.project_capability_states.read_capability_state",
            return_value="ready" if active else CAPABILITY_STATE_UNSET,
        ),
        patch("core.plan_spend_decisions.plan_belongs_to_project", return_value=belongs),
    )


def test_the_suggestion_route_is_mounted_and_answers_its_own_schema() -> None:
    """The address `core/plan_mapping_suggest.py` never had -- story 61.3.

    That module has been an end-to-end reader since epic 27 with exactly one
    importer, its own test. This is the route that reaches it.
    """
    evidence = {"state": "available", "lines": [], "counts": {"candidates": 0}}
    capability, belongs = _scope()
    with (
        _authorized()[0],
        _authorized()[1] as role,
        _authorized()[2],
        capability,
        belongs,
        patch(
            "core.datastream_workbench_placements.read_placement_suggestions",
            return_value=evidence,
        ) as read,
    ):
        response = _client().get(_SUGGESTIONS)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema"] == "datastream_workbench.placement_suggestions.v1"
    assert body["evidence"] == evidence
    # A READ, so a viewer's right: seeing which campaigns resemble a plan line is
    # a reading of the plan and of observed spend.
    assert role.call_args.args[2] == "viewer"
    # The plan travels in the query string and reaches the reader; the connector
    # does NOT come from the caller -- it is read from `app.datastreams`.
    assert read.call_args.kwargs["plan_id"] == "plan_1"
    assert "connector" in read.call_args.kwargs


def test_the_suggestion_route_refuses_while_the_capability_is_off() -> None:
    """« Éteinte, la capacité n'apparaît nulle part » is not only about pixels.

    Mutation check: dropping the capability branch of `_placement_scope` turns
    this 404 into a 200 and this line red.
    """
    capability, belongs = _scope(active=False)
    with (
        _authorized()[0],
        _authorized()[1],
        _authorized()[2],
        capability,
        belongs,
        patch(
            "core.datastream_workbench_placements.read_placement_suggestions"
        ) as read,
    ):
        response = _client().get(_SUGGESTIONS)

    assert response.status_code == 404, response.text
    read.assert_not_called()


def test_a_suggestion_on_another_projects_plan_is_not_found() -> None:
    """`plan_id` arrives from a query string, so it is re-read from the Project.

    Refused BEFORE the sweep: a plan that exists elsewhere and a plan that never
    existed are indistinguishable from here, and neither hands back a line label.
    Mutation check: dropping the `plan_belongs_to_project` branch turns this into
    a 200 and this line red.
    """
    capability, belongs = _scope(belongs=False)
    with (
        _authorized()[0],
        _authorized()[1],
        _authorized()[2],
        capability,
        belongs,
        patch(
            "core.datastream_workbench_placements.read_placement_suggestions"
        ) as read,
    ):
        response = _client().get(_SUGGESTIONS)

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "not_found"
    read.assert_not_called()


def test_an_unreachable_mart_on_the_suggestions_keeps_its_own_sentence() -> None:
    """An empty candidate list is the exact shape of "nothing resembles anything"."""
    from core.datastream_workbench_placements import (
        PLACEMENT_EVIDENCE_UNAVAILABLE_MESSAGE,
        PlacementEvidenceUnavailable,
    )

    capability, belongs = _scope()
    with (
        _authorized()[0],
        _authorized()[1],
        _authorized()[2],
        capability,
        belongs,
        patch(
            "core.datastream_workbench_placements.read_placement_suggestions",
            side_effect=PlacementEvidenceUnavailable(),
        ),
    ):
        response = _client().get(_SUGGESTIONS)

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "placement_evidence_unavailable"
    assert body["message"] == PLACEMENT_EVIDENCE_UNAVAILABLE_MESSAGE
    assert "lines" not in body and "candidates" not in body


def test_confirming_a_match_is_a_member_gesture_and_writes_through_the_store() -> None:
    """A suggestion is a proposal until somebody named confirms it.

    `member`, like every other write of this Workbench: reading a plan is a
    viewer's right, deciding which campaign a budget line bought is not.
    """
    capability, belongs = _scope()
    with (
        _authorized()[0],
        _authorized()[1] as role,
        _authorized()[2],
        capability,
        belongs,
        patch(
            "core.datastream_workbench_placements.confirm_placement_match",
            return_value={"line_key": "line_1", "replaced": 2, "match_method": "similarity"},
        ) as confirm,
    ):
        response = _client().post(
            _MATCHES,
            json={"plan_id": "plan_1", "line_key": "line_1", "campaign_ref": "camp_EXAMPLE_1"},
        )

    assert response.status_code == 201, response.text
    assert role.call_args.args[2] == "member"
    # THE LEVEL IS NOT THE CALLER'S. The body names a plan, a line and a campaign
    # and nothing else; a caller able to state its own level could write an exact
    # code over a 0.89 resemblance.
    assert "match_method" not in confirm.call_args.kwargs
    assert "match_score" not in confirm.call_args.kwargs
    assert confirm.call_args.kwargs["actor"] == "person_1"


def test_confirming_something_nothing_proposed_answers_its_own_code() -> None:
    """`invalid_request` would say nothing about the guard that fired."""
    from core.datastream_workbench_placements import (
        MATCH_NOT_PROPOSED_MESSAGE,
        PlacementMatchNotProposed,
    )

    capability, belongs = _scope()
    with (
        _authorized()[0],
        _authorized()[1],
        _authorized()[2],
        capability,
        belongs,
        patch(
            "core.datastream_workbench_placements.confirm_placement_match",
            side_effect=PlacementMatchNotProposed(MATCH_NOT_PROPOSED_MESSAGE),
        ),
    ):
        response = _client().post(
            _MATCHES,
            json={"plan_id": "plan_1", "line_key": "line_1", "campaign_ref": "camp_INVENTED"},
        )

    assert response.status_code == 422
    assert response.json()["code"] == "match_not_proposed"
    assert response.json()["message"] == MATCH_NOT_PROPOSED_MESSAGE


def test_confirming_a_match_is_refused_while_the_capability_is_off() -> None:
    capability, belongs = _scope(active=False)
    with (
        _authorized()[0],
        _authorized()[1],
        _authorized()[2],
        capability,
        belongs,
        patch("core.datastream_workbench_placements.confirm_placement_match") as confirm,
    ):
        response = _client().post(
            _MATCHES,
            json={"plan_id": "plan_1", "line_key": "line_1", "campaign_ref": "camp_EXAMPLE_1"},
        )

    assert response.status_code == 404
    confirm.assert_not_called()


def test_a_denied_role_never_reaches_either_placement_address() -> None:
    """The guard runs before anything is read or written, on both."""
    from starlette.responses import JSONResponse

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_1"))),
        patch(
            "core.admin_api._require_datastream_role",
            return_value=JSONResponse({"code": "not_found"}, 404),
        ),
        patch("core.db.get_connection", side_effect=_connection),
        patch(
            "core.datastream_workbench_placements.read_placement_suggestions"
        ) as read,
        patch("core.datastream_workbench_placements.confirm_placement_match") as confirm,
    ):
        assert _client().get(_SUGGESTIONS).status_code == 404
        assert (
            _client()
            .post(
                _MATCHES,
                json={"plan_id": "plan_1", "line_key": "l", "campaign_ref": "c"},
            )
            .status_code
            == 404
        )

    read.assert_not_called()
    confirm.assert_not_called()


# ---------------------------------------------------------------------------
# AI-264 -- les trois ECRITURES de placement franchissent vraiment la frontiere
# ---------------------------------------------------------------------------
#
# `confirm-match` avait ses tests de route depuis 61.3. `attach`, `detach` et
# `spend-decisions` n'en avaient AUCUN : les trois gardes de `_placement_scope`
# -- capacite, Datastream, projet -- n'etaient exerces sur elles par rien. Une
# garde relue dans le code n'est pas une garde prouvee ; ce qui suit passe par
# l'application ASGI, comme un appelant.

_ATTACHMENTS = "/api/projects/proj_1/datastreams/ds_1/workbench/placements/attachments"
_DETACH = f"{_ATTACHMENTS}/pl_1?plan_id=plan_1"
_SPEND_DECISIONS = (
    "/api/projects/proj_1/datastreams/ds_1/workbench/placements/spend-decisions"
)

_ATTACH_BODY = {
    "plan_id": "plan_1",
    "line_key": "line_1",
    "campaign_ref": "camp_EXAMPLE_1",
    "breakdown_dimension": "campaign_id",
    "breakdown_value": "camp_EXAMPLE_1",
}
_SPEND_BODY = {
    "plan_id": "plan_1",
    "campaign_ref": "camp_EXAMPLE_1",
    "reason": "brand campaign, deliberately outside the plan",
}

# (nom du test, cible a doubler, appel, ce qu'il rend quand il passe)
_WRITES = (
    (
        "attach",
        "core.plan_line_placements.attach_placement",
        lambda client: client.post(_ATTACHMENTS, json=_ATTACH_BODY),
        201,
    ),
    (
        "detach",
        "core.plan_line_placements.detach_placement",
        lambda client: client.delete(_DETACH),
        200,
    ),
    (
        "spend-decision",
        "core.plan_spend_decisions.accept_unmatched_spend",
        lambda client: client.post(_SPEND_DECISIONS, json=_SPEND_BODY),
        201,
    ),
)


@pytest.mark.parametrize(("name", "target", "call", "ok_status"), _WRITES)
def test_each_placement_write_is_a_member_gesture(name, target, call, ok_status) -> None:
    """`member`, sur les trois -- lire un plan est un droit de viewer, l'ecrire non."""
    capability, belongs = _scope()
    with (
        _authorized()[0],
        _authorized()[1] as role,
        _authorized()[2],
        capability,
        belongs,
        patch(target, return_value={"ok": True}) as store,
    ):
        response = call(_client())

    assert response.status_code == ok_status, response.text
    assert role.call_args.args[2] == "member"
    store.assert_called_once()
    # L'ACTEUR VIENT DU JETON, jamais du corps. Aucun des trois corps ci-dessus ne
    # nomme d'auteur, et l'ecriture en porte un.
    assert store.call_args.kwargs["actor"] == "person_1"


@pytest.mark.parametrize(("name", "target", "call", "ok_status"), _WRITES)
def test_each_placement_write_is_refused_while_the_capability_is_off(
    name, target, call, ok_status
) -> None:
    """Eteinte, la capacite n'a pas d'onglet -- donc pas de geste non plus.

    Une ecriture qui reussit alors que l'onglet auquel elle appartient n'existe
    pas serait une SECONDE autorite sur l'etat de la capacite. Le magasin ne doit
    pas etre atteint du tout : un refus apres l'ecriture n'est pas un refus.
    """
    capability, belongs = _scope(active=False)
    with (
        _authorized()[0],
        _authorized()[1],
        _authorized()[2],
        capability,
        belongs,
        patch(target) as store,
    ):
        response = call(_client())

    assert response.status_code == 404
    store.assert_not_called()


@pytest.mark.parametrize(("name", "target", "call", "ok_status"), _WRITES)
def test_each_placement_write_refuses_another_projects_plan(
    name, target, call, ok_status
) -> None:
    """Le `plan_id` vient du client, et il ne suffit pas a nommer un plan.

    `attach_placement` scope sur le parent (ligne, campagne) et `detach_placement`
    sur (id, plan, connecteur) : aucun des deux predicats ne sait a quel Projet le
    plan appartient. Sans cette lecture, un membre d'un Projet atteindrait les
    lignes d'un autre depuis l'adresse de son propre Datastream.

    Refuse en NOT FOUND, jamais en 403 : un plan qui existe ailleurs et un plan
    qui n'a jamais existe doivent etre indiscernables d'ici.
    """
    capability, belongs = _scope(belongs=False)
    with (
        _authorized()[0],
        _authorized()[1],
        _authorized()[2],
        capability,
        belongs,
        patch(target) as store,
    ):
        response = call(_client())

    assert response.status_code == 404
    store.assert_not_called()


@pytest.mark.parametrize(("name", "target", "call", "ok_status"), _WRITES)
def test_a_denied_role_never_reaches_any_placement_write(
    name, target, call, ok_status
) -> None:
    """La garde de role court AVANT toute lecture et toute ecriture, sur les trois."""
    from starlette.responses import JSONResponse

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_1"))),
        patch(
            "core.admin_api._require_datastream_role",
            return_value=JSONResponse({"code": "not_found"}, 404),
        ),
        patch("core.db.get_connection", side_effect=_connection),
        patch(target) as store,
    ):
        response = call(_client())

    assert response.status_code == 404
    store.assert_not_called()


@contextmanager
def _connection_without_the_datastream():
    """Une connexion dont `app.datastreams` ne rend AUCUNE ligne.

    Le `MagicMock` par defaut rend une ligne pour toute requete, donc la
    troisieme garde de `_placement_scope` -- « ce Datastream existe-t-il dans ce
    Projet, non archive ? » -- ne se declenche jamais sous lui. Sans ce double,
    les tests ci-dessus prouvent deux gardes sur trois et laissent croire qu'ils
    en prouvent trois.
    """
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = None
    yield conn


@pytest.mark.parametrize(("name", "target", "call", "ok_status"), _WRITES)
def test_each_placement_write_refuses_a_datastream_of_another_project(
    name, target, call, ok_status
) -> None:
    """L'adresse porte un `datastream_id`, et la ligne doit exister DANS ce Projet.

    La lecture est `WHERE id = %s AND project_id = %s AND archived_at IS NULL` :
    un Datastream archive, ou celui d'un Projet voisin, prend la meme branche
    qu'un identifiant invente -- et rend la meme phrase.
    """
    capability, belongs = _scope()
    with (
        _authorized()[0],
        _authorized()[1],
        patch("core.db.get_connection", side_effect=_connection_without_the_datastream),
        capability,
        belongs,
        patch(target) as store,
    ):
        response = call(_client())

    assert response.status_code == 404
    store.assert_not_called()


# ---------------------------------------------------------------------------
# AMENDED 2026-08-18 -- LES DEUX ECRITURES SONT PLURIELLES, ET LE PLURIEL EST UNE
# SEULE TRANSACTION.
#
# « attacher un ou plusieurs placements a une ligne » est la ligne `Permet`
# ratifiee, et le pluriel n'avait aucune adresse : trois placements sur une
# campagne, c'etaient trois allers-retours et trois occasions de s'arreter au
# milieu. Meme constat sur `Exact code` : quarante propositions confirmees une
# par une, ce sont quarante dialogues identiques -- la maniere exacte dont on
# apprend a cliquer sans lire.
# ---------------------------------------------------------------------------

_ATTACH_MANY = {
    "plan_id": "plan_1",
    "line_key": "line_1",
    "campaign_ref": "camp_EXAMPLE_1",
    "breakdown_dimension": "campaign_id",
    "breakdown_values": ["plc_A", "plc_B", "plc_A", " "],
}


def test_attaching_several_placements_is_one_call_per_value_in_one_transaction() -> None:
    """Un appel de magasin par valeur, dedupliquee, et une seule enveloppe.

    Dedupliquee parce que l'index unique aurait fait de la seconde ecriture un
    no-op : un compte de trois qui en ecrit deux est une confirmation qui nomme
    une portee fausse.
    """
    capability, belongs = _scope()
    with (
        _authorized()[0],
        _authorized()[1],
        _authorized()[2],
        capability,
        belongs,
        patch(
            "core.plan_line_placements.attach_placement",
            side_effect=lambda _conn, **kwargs: {"id": kwargs["breakdown_value"]},
        ) as store,
    ):
        response = _client().post(_ATTACHMENTS, json=_ATTACH_MANY)

    assert response.status_code == 201, response.text
    assert response.json()["count"] == 2
    assert [call.kwargs["breakdown_value"] for call in store.call_args_list] == ["plc_A", "plc_B"]


def test_the_singular_attach_answer_is_unchanged() -> None:
    """Une valeur, un placement, et la forme que tout appelant anterieur lit."""
    capability, belongs = _scope()
    with (
        _authorized()[0],
        _authorized()[1],
        _authorized()[2],
        capability,
        belongs,
        patch("core.plan_line_placements.attach_placement", return_value={"id": "pl_1"}),
    ):
        response = _client().post(_ATTACHMENTS, json=_ATTACH_BODY)

    assert response.status_code == 201
    assert response.json() == {"id": "pl_1"}


def test_an_empty_plural_is_refused_rather_than_answering_a_success_that_wrote_nothing() -> None:
    capability, belongs = _scope()
    with (
        _authorized()[0],
        _authorized()[1],
        _authorized()[2],
        capability,
        belongs,
        patch("core.plan_line_placements.attach_placement") as store,
    ):
        response = _client().post(_ATTACHMENTS, json={**_ATTACH_MANY, "breakdown_values": []})

    assert response.status_code == 422, response.text
    store.assert_not_called()


def test_confirming_a_tier_of_matches_is_one_call_per_pair_and_never_sends_a_level() -> None:
    """Le niveau n'est PAS envoye, sur une paire comme sur quarante.

    Un appelant capable d'envoyer un lot ET son niveau pourrait estampiller
    `Exact code` sur quarante ressemblances d'une seule pression, ce qui est
    exactement ce que « no silent value fusion » refuse.
    """
    capability, belongs = _scope()
    body = {
        "plan_id": "plan_1",
        "matches": [
            {"line_key": "line_1", "campaign_ref": "camp_A"},
            {"line_key": "line_2", "campaign_ref": "camp_B"},
            {"line_key": "line_1", "campaign_ref": "camp_A"},
        ],
    }
    with (
        _authorized()[0],
        _authorized()[1] as role,
        _authorized()[2],
        capability,
        belongs,
        patch(
            "core.datastream_workbench_placements.confirm_placement_match",
            side_effect=lambda _conn, **kwargs: {"campaign_ref": kwargs["campaign_ref"]},
        ) as store,
    ):
        response = _client().post(_MATCHES, json=body)

    assert response.status_code == 201, response.text
    assert response.json()["count"] == 2
    assert role.call_args.args[2] == "member"
    for call in store.call_args_list:
        assert "match_method" not in call.kwargs
        assert "match_score" not in call.kwargs


def test_an_empty_batch_of_matches_is_refused() -> None:
    capability, belongs = _scope()
    with (
        _authorized()[0],
        _authorized()[1],
        _authorized()[2],
        capability,
        belongs,
        patch("core.datastream_workbench_placements.confirm_placement_match") as store,
    ):
        response = _client().post(_MATCHES, json={"plan_id": "plan_1", "matches": []})

    assert response.status_code == 422, response.text
    store.assert_not_called()
