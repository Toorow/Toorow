"""Story 75-4 -- the SIX HTTP doors of the AI settings, on a real PostgreSQL.

WHY THROUGH THE APP AND NOT THROUGH THE FUNCTIONS. `ai_settings` already has its
own pg suite, and it proves the cascade, the clearing and every refusal. What it
cannot prove is that a browser reaches any of them. `visualization_specs` shipped
seven handlers whose module tests were green while every request answered 404
because two mount lines were missing -- that is the class of defect this file
exists for. Every request below goes through `core.admin_api.router`, the object
the ASGI app is built from, so the paths, the methods and the statuses are the
ones the console receives.

WHAT THE REVIEW OF 2026-09-05 FOUND MISSING, and what is proved here:

  * the six routes exist and answer their declared verbs;
  * a caller holding organization A reaches NOTHING of organization B -- and is
    told 404, never 403, because existence is not disclosed;
  * a member who cannot manage the organization is refused the write BY A
    SENTENCE THAT NAMES THE GESTURE, and the stored settings do not move;
  * a project viewer cannot state an override;
  * a member states one and the very next GET reports `PROJECT` as its source.

EVERY TEST ROLLS BACK. The handlers call `conn.commit()`; that lands on a proxy
whose commit is a no-op, so the assertions read uncommitted rows on the same
connection and the fixture rolls them back.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from core.ai_settings import read_scope, resolve
from starlette.responses import JSONResponse
from ulid import ULID

pytestmark = pytest.mark.usefixtures("live_postgres")

ACTOR = "owner@example.com"
OUTSIDER = "stranger@example.com"


def _uid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


class _NoCommit:
    """The fixture's connection, with `commit()` swallowed so the test rolls back."""

    def __init__(self, conn):
        self._conn = conn

    def commit(self) -> None:
        return None

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


class Tenant:
    """Organization A with one project, organization B beside it, and the roles.

    `role` is the role ACTOR holds in A: the org doors read with any active
    membership and write only for an owner or an admin, so the two halves of
    that rule need two tenants to be told apart.
    """

    def __init__(self, conn, *, role: str = "owner"):
        self.conn = conn
        self.role = role
        self.org_id = _uid("org")
        self.project_id = _uid("proj")
        self.other_org_id = _uid("org")

    def build(self) -> Tenant:
        with self.conn.cursor() as cur:
            for org in (self.org_id, self.other_org_id):
                cur.execute(
                    "INSERT INTO app.organizations (id, name, slug, status, created_by) "
                    "VALUES (%s, 'Story 75-4 door fixture', %s, 'active', %s)",
                    (org, org.replace("_", "-"), ACTOR),
                )
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
                "VALUES (%s, %s, 'Story 75-4 door fixture', %s, %s)",
                (self.project_id, self.org_id, self.project_id.replace("_", "-"), ACTOR),
            )
            # ACTOR belongs to A and to A only. The membership of B is somebody
            # else's, so B exists and is simply not theirs -- which is the shape
            # the 404 has to be measured against.
            cur.execute(
                "INSERT INTO app.org_members (id, org_id, identity, role, status) "
                "VALUES (%s, %s, %s, %s, 'active')",
                (_uid("om"), self.org_id, ACTOR, self.role),
            )
            cur.execute(
                "INSERT INTO app.org_members (id, org_id, identity, role, status) "
                "VALUES (%s, %s, %s, 'owner', 'active')",
                (_uid("om"), self.other_org_id, OUTSIDER),
            )
        return self


@pytest.fixture()
def tenant(live_postgres):
    built = Tenant(live_postgres).build()
    yield built
    live_postgres.rollback()


@pytest.fixture()
def read_only_tenant(live_postgres):
    """ACTOR is a plain member of A: they may read the organization, not write it."""
    built = Tenant(live_postgres, role="member").build()
    yield built
    live_postgres.rollback()


@contextmanager
def _client(tenant, *, project_write_denied: bool = False):
    """The real router, the real service, our connection.

    THE PROJECT ROLE GATE IS THE ONE THING STUBBED, and it is stubbed at the seam
    the route module itself imports (`core.admin_api._require_datastream_role`),
    so what is exercised is the door's own ordering: denial first, then the
    project's organization, then the service. THE ORGANIZATION GATE IS NOT
    STUBBED -- it reads `app.org_members` rows this fixture wrote, because
    "another organization answers 404" is exactly the claim under test and a
    stubbed answer would prove the stub.
    """
    import core.db
    from core.admin_api import router
    from starlette.testclient import TestClient

    @contextmanager
    def _connection(*args, **kwargs):
        # A SAVEPOINT rather than a transaction of its own: a refused call rolls
        # back what the handler wrote and keeps the fixture for the assertions --
        # which is what production's connection does with an uncommitted body.
        with tenant.conn.transaction():
            yield _NoCommit(tenant.conn)

    def _role(project_id, identity, minimum_role, conn, **kwargs):
        # The real gate's own shape: a viewer passes `viewer` and is refused
        # `member`, and a refusal is a 404 rather than a 403.
        if project_write_denied and minimum_role != "viewer":
            return JSONResponse({"code": "not_found", "message": "Not found"}, 404)
        return None

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, ACTOR))),
        patch("core.api_auth.authenticate_api_request", return_value=(True, ACTOR)),
        patch("core.admin_api._require_datastream_role", side_effect=_role),
        patch.object(core.db, "get_connection", _connection),
        patch.object(core.db, "request_connection", _connection),
    ):
        with TestClient(router, raise_server_exceptions=True) as client:
            yield client


def _project_door(project_id: str) -> str:
    return f"/api/projects/{project_id}/ai-settings"


def _org_door(org_id: str) -> str:
    return f"/api/organizations/{org_id}/ai-settings"


# ---------------------------------------------------------------------------
# The six doors exist.
# ---------------------------------------------------------------------------


def test_the_six_doors_answer_the_declared_paths(tenant) -> None:
    """A 404 on a path a member holds would mean the routes are not spliced in."""
    with _client(tenant) as client:
        assert client.get(_project_door(tenant.project_id)).status_code == 200
        assert client.put(
            _project_door(tenant.project_id), json={"narrative_language": "en-GB"}
        ).status_code == 200
        assert client.request(
            "DELETE", _project_door(tenant.project_id), json={}
        ).status_code == 200

        assert client.get(_org_door(tenant.org_id)).status_code == 200
        assert client.put(
            _org_door(tenant.org_id), json={"narrative_register": "executive"}
        ).status_code == 200
        assert client.request(
            "DELETE", _org_door(tenant.org_id), json={}
        ).status_code == 200


# ---------------------------------------------------------------------------
# A member writes, and the very next read says who decided.
# ---------------------------------------------------------------------------


def test_a_member_states_an_override_and_the_next_read_names_its_scope(tenant) -> None:
    with _client(tenant) as client:
        put = client.put(
            _project_door(tenant.project_id), json={"narrative_language": "fr-FR"}
        )
        assert put.status_code == 200, put.text
        assert put.json()["sources"]["narrative_language"] == "PROJECT"

        read = client.get(_project_door(tenant.project_id))
        assert read.status_code == 200, read.text
        body = read.json()
        # THE VALUE, WHERE IT CAME FROM, AND WHAT THIS SCOPE ITSELF STATES.
        assert body["resolved"]["narrative_language"] == "fr-FR"
        assert body["sources"]["narrative_language"] == "PROJECT"
        assert body["own"]["narrative_language"] == "fr-FR"
        # Every other field is still the platform's: a PUT states an override,
        # it does not flatten the cascade.
        assert body["sources"]["query_scope"] == "PLATFORM"
        assert body["sources"]["fiscal_calendar"] == "PLATFORM"


def test_the_organization_value_reaches_the_project_door(tenant) -> None:
    """One scope up, read through the door one scope down -- source `ORG`."""
    with _client(tenant) as client:
        assert client.put(
            _org_door(tenant.org_id), json={"query_scope": "any_published_view"}
        ).status_code == 200

        body = client.get(_project_door(tenant.project_id)).json()
        assert body["resolved"]["query_scope"] == "any_published_view"
        assert body["sources"]["query_scope"] == "ORG"
        # The project states nothing of its own, and says so in one shape.
        assert body["own"] is None
        assert body["inherited"]["query_scope"] == "any_published_view"


def test_the_clearing_door_gives_the_project_back_to_its_organization(tenant) -> None:
    with _client(tenant) as client:
        client.put(_org_door(tenant.org_id), json={"narrative_language": "fr-FR"})
        client.put(_project_door(tenant.project_id), json={"narrative_language": "en-GB"})

        cleared = client.request("DELETE", _project_door(tenant.project_id), json={})
        assert cleared.status_code == 200, cleared.text
        body = cleared.json()
        assert body["resolved"]["narrative_language"] == "fr-FR"
        assert body["sources"]["narrative_language"] == "ORG"
        # NOTHING WAS DELETED: both acts are in the history of the project.
        assert [row["cleared"] for row in body["history"]] == [True, False]


# ---------------------------------------------------------------------------
# Nobody reaches another organization, and nobody writes above their role.
# ---------------------------------------------------------------------------


def test_another_organizations_settings_are_not_reachable_through_either_verb(
    tenant,
) -> None:
    """404, not 403: the door does not disclose that organization B exists."""
    with _client(tenant) as client:
        read = client.get(_org_door(tenant.other_org_id))
        assert read.status_code == 404, read.text
        assert read.json()["code"] == "not_found"

        write = client.put(
            _org_door(tenant.other_org_id), json={"narrative_language": "de-DE"}
        )
        assert write.status_code == 404, write.text

        cleared = client.request(
            "DELETE", _org_door(tenant.other_org_id), json={}
        )
        assert cleared.status_code == 404, cleared.text

    # AND NOTHING WAS WRITTEN THERE. A 404 that still stored the payload would
    # be the worst of both answers.
    assert read_scope(
        tenant.conn, scope="ORG", scope_id=tenant.other_org_id, org_id=tenant.other_org_id
    ) is None


def test_a_member_who_cannot_manage_the_organization_is_refused_by_name(
    read_only_tenant,
) -> None:
    """The read is theirs; the write names the gesture that would repair it."""
    with _client(read_only_tenant) as client:
        assert client.get(_org_door(read_only_tenant.org_id)).status_code == 200

        refused = client.put(
            _org_door(read_only_tenant.org_id), json={"narrative_language": "de-DE"}
        )
        assert refused.status_code == 403, refused.text
        message = refused.json()["message"]
        assert "owner" in message and "admin" in message
        # A sentence, not a cause: no table and no role constant of the schema.
        assert "org_members" not in message

    assert read_scope(
        read_only_tenant.conn, scope="ORG", scope_id=read_only_tenant.org_id,
        org_id=read_only_tenant.org_id,
    ) is None


def test_a_project_viewer_reads_the_settings_and_states_nothing(tenant) -> None:
    with _client(tenant, project_write_denied=True) as client:
        assert client.get(_project_door(tenant.project_id)).status_code == 200

        refused = client.put(
            _project_door(tenant.project_id), json={"narrative_language": "de-DE"}
        )
        assert refused.status_code == 404, refused.text

        cleared = client.request("DELETE", _project_door(tenant.project_id), json={})
        assert cleared.status_code == 404, cleared.text

    resolved = resolve(tenant.conn, org_id=tenant.org_id, project_id=tenant.project_id)
    assert resolved["sources"]["narrative_language"] == "PLATFORM"


# ---------------------------------------------------------------------------
# A refusal crosses the wire as a sentence naming its field.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "code", "field"),
    [
        ({"narrative_language": "klingon"}, "unknown_language", "narrative_language"),
        (
            {"fiscal_calendar": {"week_start_day": "sunday"}},
            "partial_fiscal_calendar",
            "fiscal_calendar",
        ),
        ({"query_scope": "everything"}, "unknown_query_scope", "query_scope"),
        ({}, "states_nothing", None),
    ],
)
def test_a_refused_setting_answers_422_and_names_its_field(
    tenant, payload, code, field
) -> None:
    with _client(tenant) as client:
        refused = client.put(_project_door(tenant.project_id), json=payload)
        assert refused.status_code == 422, refused.text
        body = refused.json()
        assert body["code"] == code
        assert body["field"] == field
        assert body["remedy"]

    assert read_scope(
        tenant.conn, scope="PROJECT", scope_id=tenant.project_id, org_id=tenant.org_id
    ) is None


def test_the_platform_defaults_the_door_hands_back_are_a_copy(tenant) -> None:
    """A handler that mutated them would poison every later request of the process."""
    from core.ai_settings import PLATFORM_DEFAULTS

    with _client(tenant) as client:
        body = client.get(_project_door(tenant.project_id)).json()

    body["platform_defaults"]["rules_always"].append("Never do this.")
    assert PLATFORM_DEFAULTS["rules_always"] == []
