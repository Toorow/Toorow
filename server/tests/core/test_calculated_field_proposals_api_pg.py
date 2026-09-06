"""Story 75-2 -- the THREE HTTP doors the console calls, on a real Postgres.

WHY THROUGH THE APP AND NOT THROUGH THE FUNCTIONS. `calculated_field_proposals`
already has its own pg suite; what it cannot prove is that a browser reaches
those rules. `visualization_specs` shipped seven handlers whose module tests were
green while every request answered 404 because two mount lines were missing --
that is the class of defect this file exists for. Every request below goes
through `core.admin_api.router`, the object the ASGI app is built from, so the
paths, the methods, the statuses and the envelope keys are the ones the console
receives.

WHY A REAL DATABASE. `applied_ref` naming a change-set that stands in `prepare`,
`value_type` inferred by the walk rather than declared by the caller, a
provenance row verified in THIS Project, and a refused proposal leaving nothing
written -- a mocked cursor accepts all of that happily.

EVERY TEST ROLLS BACK. The handlers call `conn.commit()`; that lands on a proxy
whose commit is a no-op, so the assertions read uncommitted rows on the same
connection and the fixture rolls them back.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from core import calculated_field_proposals as proposals
from starlette.responses import JSONResponse

from tests.core.test_analyze_artifacts_pg import Chain
from tests.core.test_calculated_field_proposals_pg import _metric, _ratio, _uid

pytestmark = pytest.mark.usefixtures("live_postgres")

ACTOR = "owner@example.com"


class _NoCommit:
    """The fixture's connection, with `commit()` swallowed so the test rolls back."""

    def __init__(self, conn):
        self._conn = conn

    def commit(self) -> None:
        return None

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


@pytest.fixture()
def chain(live_postgres):
    """The seeded chain, PLUS the membership the Epic-36 floor resolves against.

    The doors arm that floor (`_proposal_connection` -> `arm_access_floor`), so
    without an `app.org_members` row and a `view` grant, `app.query_results` is
    invisible to this identity and every provenance check answers
    `unknown_provenance`. That is the floor doing its job -- it just has to be
    given a member to work with, exactly as `test_analyze_rls_floor_pg` does.
    """
    built = Chain(live_postgres).build()
    with live_postgres.cursor() as cur:
        cur.execute(
            "INSERT INTO app.org_members (id, org_id, identity, role, status) "
            "VALUES (%s, %s, %s, 'member', 'active')",
            (_uid("om"), built.org_id, ACTOR),
        )
        cur.execute(
            "INSERT INTO app.resource_grants "
            "(id, org_id, identity, scope_type, scope_id, capability, granted_by) "
            "VALUES (%s, %s, %s, 'project', %s, 'edit', 'test')",
            (_uid("rg"), built.org_id, ACTOR, built.project_id),
        )
    yield built
    live_postgres.rollback()


@pytest.fixture()
def pins(chain):
    """Two exact Concept versions a promotion may reference."""
    return {
        "clicks": _metric(chain, "clicks", "integer"),
        "spend": _metric(chain, "spend", "money", unit="EUR"),
    }


@contextmanager
def _client(chain, *, role_denies: bool = False, roles: list[str] | None = None):
    """The real router, the real service, our connection.

    The role gate is the ONE thing stubbed, and it is stubbed at the seam the
    route module itself imports (`core.admin_api._require_datastream_role`), so
    what is exercised below is the door's own ordering: denial first, then the
    Project read, then the service.

    `roles` RECORDS WHAT EACH DOOR ASKED FOR. The stub used to ignore
    `minimum_role` entirely, so a door that asked `viewer` for the gesture that
    opens a change-set -- the exact departure the module docstring argues for --
    would have passed every test in this file. Pass a list and the rank of every
    call lands in it, in order.
    """
    import core.db
    from core.admin_api import router
    from starlette.testclient import TestClient

    @contextmanager
    def _connection():
        # A SAVEPOINT rather than a transaction of its own: a refused call rolls
        # back what the handler wrote and keeps the seeded chain for the
        # assertions -- which is exactly what production's connection does with
        # an uncommitted body.
        with chain.conn.transaction():
            yield _NoCommit(chain.conn)

    def _role(project_id, identity, minimum_role, conn, **kwargs):
        if roles is not None:
            roles.append(minimum_role)
        if role_denies:
            return JSONResponse({"code": "not_found", "message": "Not found"}, 404)
        return None

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, ACTOR))),
        patch("core.api_auth.authenticate_api_request", return_value=(True, ACTOR)),
        patch("core.admin_api._refuse_unless_project_allowed", return_value=None),
        patch("core.admin_api._require_datastream_role", side_effect=_role),
        patch.object(core.db, "get_connection", _connection),
    ):
        with TestClient(router, raise_server_exceptions=True) as client:
            yield client


def _base(project_id: str) -> str:
    return f"/api/projects/{project_id}/calculated-field-proposals"


def _body(pins, **overrides) -> dict:
    payload = {
        "name": "cost_per_click",
        "expression": _ratio(pins),
        "provenance": {},
        "description": "Spend divided by clicks, found while reading the result.",
    }
    payload.update(overrides)
    return payload


def _count(conn, project_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.calculated_field_proposals WHERE project_id = %s",
            (project_id,),
        )
        return int(cur.fetchone()[0])


# ---------------------------------------------------------------------------
# Create -> list -> resolve, the whole rail through HTTP
# ---------------------------------------------------------------------------


def test_the_three_doors_answer_the_declared_paths(chain, pins):
    """A 404 here would mean the routes are not spliced into the router."""
    with _client(chain) as client:
        created = client.post(
            _base(chain.project_id),
            json=_body(pins, provenance={"result_id": chain.result_id}),
        )
        assert created.status_code == 201, created.text
        filed = created.json()["proposal"]
        assert filed["id"].startswith("cfp_")
        assert filed["status"] == proposals.OPEN_STATUS
        # THE DOOR STAMPS THE ORIGIN. The body never carried one.
        assert filed["origin"] == "human"
        assert filed["requested_by"] == ACTOR
        # INFERRED by the walk, not declared by the caller.
        assert filed["value_type"] == "ratio"
        assert {(dep["concept_id"], dep["version_id"]) for dep in filed["dependencies"]} == {
            pins["spend"], pins["clicks"]
        }
        # The plan version was DERIVED from the Result: the caller sent none.
        assert filed["provenance"]["result_id"] == chain.result_id
        assert filed["provenance"]["query_spec_version_id"] == chain.query_spec_version_id
        assert filed["provenance"]["origin"] == proposals.PROVENANCE_ORIGIN

        listed = client.get(f"{_base(chain.project_id)}?status=open")
        assert listed.status_code == 200, listed.text
        page = listed.json()
        assert page["status"] == "open"
        assert page["proposals_total"] == 1
        assert [row["id"] for row in page["proposals"]] == [filed["id"]]

        resolved = client.post(
            f"{_base(chain.project_id)}/{filed['id']}/resolve",
            json={"status": "accepted"},
        )
        assert resolved.status_code == 200, resolved.text
        verdict = resolved.json()["proposal"]
        assert verdict["status"] == "accepted"
        assert verdict["resolved_by"] == ACTOR
        change_set_id = verdict["applied_ref"]
        assert change_set_id

        # THE CHANGE-SET EXISTS AND STANDS PREPARED -- never confirmed.
        with chain.conn.cursor() as cur:
            cur.execute(
                "SELECT state, object_type, intent FROM app.semantic_change_sets WHERE id = %s",
                (change_set_id,),
            )
            row = cur.fetchone()
        assert row is not None, "the acceptance named a change-set that does not exist"
        assert row[0] == proposals.PREPARED_STATE
        assert row[1] == "semantic-concept"
        assert row[2]["action"] == "create_concept"
        assert row[2]["provenance"]["proposal_id"] == filed["id"]
        assert row[2]["provenance"]["result_id"] == chain.result_id

        # AND WHAT IT STILL REFUSES IS RETURNED, so the console can say what is
        # left to declare before anyone can confirm.
        assert "undeclared_aggregation" in {
            item.get("code") for item in verdict["prepare_refusals"]
        }

        # The queue is what is LEFT to decide.
        empty = client.get(f"{_base(chain.project_id)}?status=open")
        assert empty.json()["proposals_total"] == 0


def test_declining_writes_a_verdict_and_opens_nothing(chain, pins):
    with _client(chain) as client:
        filed = client.post(
            _base(chain.project_id),
            json=_body(pins, provenance={"result_id": chain.result_id}),
        ).json()["proposal"]
        resolved = client.post(
            f"{_base(chain.project_id)}/{filed['id']}/resolve",
            json={"status": "declined"},
        )
    assert resolved.status_code == 200, resolved.text
    verdict = resolved.json()["proposal"]
    assert verdict["status"] == "declined"
    assert verdict["applied_ref"] is None
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.semantic_change_sets WHERE project_id = %s",
            (chain.project_id,),
        )
        assert int(cur.fetchone()[0]) == 0


def test_a_second_verdict_is_a_conflict_not_an_absence(chain, pins):
    with _client(chain) as client:
        filed = client.post(
            _base(chain.project_id),
            json=_body(pins, provenance={"result_id": chain.result_id}),
        ).json()["proposal"]
        client.post(
            f"{_base(chain.project_id)}/{filed['id']}/resolve", json={"status": "declined"}
        )
        again = client.post(
            f"{_base(chain.project_id)}/{filed['id']}/resolve", json={"status": "accepted"}
        )
    assert again.status_code == 409, again.text
    assert again.json()["code"] == "proposal_already_resolved"


def test_an_unknown_proposal_is_a_404_and_not_a_conflict(chain, pins):
    with _client(chain) as client:
        answer = client.post(
            f"{_base(chain.project_id)}/{_uid('cfp')}/resolve", json={"status": "accepted"}
        )
    assert answer.status_code == 404, answer.text
    assert answer.json()["code"] == "not_found"


# ---------------------------------------------------------------------------
# The refusals, by NAME, with the status the neighbours use
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        # An operation outside `ALLOWED_OPERATIONS` -- `raw_sql` included.
        ({"expression": {"op": "raw_sql", "sql": "SELECT 1"}}, "invalid_expression"),
        ({"expression": {}}, "empty_expression"),
        ({"name": "Cost Per Click"}, "invalid_name"),
        ({"name": ""}, "missing_name"),
        # A promotion that cannot name the exploration that produced it.
        ({"provenance": {}}, "missing_provenance"),
    ],
)
def test_a_refused_proposal_is_named_and_writes_nothing(chain, pins, overrides, code):
    payload = _body(pins, provenance={"result_id": chain.result_id})
    payload.update(overrides)
    with _client(chain) as client:
        answer = client.post(_base(chain.project_id), json=payload)
    assert answer.status_code == 422, answer.text
    assert answer.json()["code"] == code
    assert _count(chain.conn, chain.project_id) == 0


def test_a_reference_that_follows_latest_is_refused_by_name(chain, pins):
    """`unresolved_reference` -- a `concept_name` leaf parses so a workbench can
    show pending work, and may never be promoted."""
    with _client(chain) as client:
        answer = client.post(
            _base(chain.project_id),
            json=_body(
                pins,
                provenance={"result_id": chain.result_id},
                expression={
                    "op": "ratio",
                    "numerator": {"op": "concept_name", "name": "spend"},
                    "denominator": {
                        "op": "concept_ref",
                        "concept_id": pins["clicks"][0],
                        "version_id": pins["clicks"][1],
                    },
                    "zero_denominator": "null",
                },
            ),
        )
    assert answer.status_code == 422, answer.text
    assert answer.json()["code"] == "unresolved_reference"
    assert _count(chain.conn, chain.project_id) == 0


def test_a_result_of_another_project_is_refused_by_name(chain, pins):
    """`unknown_provenance` -- the rows are verified in THIS Project."""
    with _client(chain) as client:
        answer = client.post(
            _base(chain.project_id),
            json=_body(pins, provenance={"result_id": _uid("qr")}),
        )
    assert answer.status_code == 422, answer.text
    assert answer.json()["code"] == "unknown_provenance"
    assert _count(chain.conn, chain.project_id) == 0


@pytest.mark.parametrize(
    "overrides",
    [
        # `.strip()` on a dict raised `AttributeError` inside the service and the
        # blanket handler answered `500 db_error` -- "The promotion queue is
        # unavailable" about a queue that was up, for a typo in the caller's body.
        {"description": {"text": "a paragraph in an object"}},
        {"description": 12},
        {"name": ["cost", "per", "click"]},
        {"expression": "SELECT spend / clicks"},
        {"provenance": "qr_01EXAMPLE"},
    ],
)
def test_a_body_of_the_wrong_shape_is_the_callers_defect_not_an_outage(chain, pins, overrides):
    payload = _body(pins, provenance={"result_id": chain.result_id})
    payload.update(overrides)
    with _client(chain) as client:
        answer = client.post(_base(chain.project_id), json=payload)
    assert answer.status_code == 400, answer.text
    body = answer.json()
    assert body["code"] == "invalid_body"
    # The sentence names the GESTURE, and never the platform.
    assert body["message"].startswith("Send ")
    assert "unavailable" not in body["message"]
    assert _count(chain.conn, chain.project_id) == 0


def test_a_null_optional_field_is_not_a_shape_defect(chain, pins):
    """`description: null` is the console's own body: absent is not malformed."""
    with _client(chain) as client:
        answer = client.post(
            _base(chain.project_id),
            json=_body(pins, provenance={"result_id": chain.result_id}, description=None),
        )
    assert answer.status_code == 201, answer.text
    assert answer.json()["proposal"]["description"] is None


# ---------------------------------------------------------------------------
# The rank each door asks for, READ FROM THE CALL rather than from the source
# ---------------------------------------------------------------------------


def test_each_door_asks_for_the_rank_its_gesture_costs(chain, pins):
    """`member` to file, `viewer` to read, `member` to resolve.

    The module docstring argues this rail parts company with
    `context_api._request_review` precisely here: filing carries a typed payload
    whose acceptance opens a change-set on the Project's semantic model, so it
    costs `member` and not `view`. Nothing measured it -- the stub swallowed
    `minimum_role` -- so the argument was a comment.
    """
    seen: list[str] = []
    with _client(chain, roles=seen) as client:
        filed = client.post(
            _base(chain.project_id),
            json=_body(pins, provenance={"result_id": chain.result_id}),
        )
        assert filed.status_code == 201, filed.text
        assert seen == ["member"]

        listed = client.get(f"{_base(chain.project_id)}?status=open")
        assert listed.status_code == 200, listed.text
        assert seen == ["member", "viewer"]

        proposal_id = filed.json()["proposal"]["id"]
        verdict = client.post(
            f"{_base(chain.project_id)}/{proposal_id}/resolve", json={"status": "declined"}
        )
        assert verdict.status_code == 200, verdict.text
        assert seen == ["member", "viewer", "member"]


def test_the_queue_door_serves_open_and_says_so_for_any_other_word(chain):
    with _client(chain) as client:
        answer = client.get(f"{_base(chain.project_id)}?status=accepted")
    assert answer.status_code == 422, answer.text
    assert answer.json()["code"] == "invalid_param"


# ---------------------------------------------------------------------------
# Non-disclosure: foreign, denied and absent all answer the same envelope
# ---------------------------------------------------------------------------


def test_a_project_this_caller_cannot_reach_answers_the_one_envelope(chain, pins):
    with _client(chain, role_denies=True) as client:
        created = client.post(
            _base(chain.project_id),
            json=_body(pins, provenance={"result_id": chain.result_id}),
        )
        listed = client.get(f"{_base(chain.project_id)}?status=open")
    assert created.status_code == 404, created.text
    assert listed.status_code == 404, listed.text
    assert created.json() == {"code": "not_found", "message": "Not found"}
    # The denial answered BEFORE any work was done.
    assert _count(chain.conn, chain.project_id) == 0


def test_a_project_that_does_not_exist_answers_the_same_envelope(chain, pins):
    foreign = _uid("proj")
    with _client(chain) as client:
        created = client.post(
            _base(foreign), json=_body(pins, provenance={"result_id": chain.result_id})
        )
        listed = client.get(f"{_base(foreign)}?status=open")
    assert created.status_code == 404, created.text
    assert listed.status_code == 404, listed.text
    assert created.json() == {"code": "not_found", "message": "Not found"}
