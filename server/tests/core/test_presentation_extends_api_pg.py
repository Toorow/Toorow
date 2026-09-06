"""Story 75-6 -- the FOUR HTTP doors of the presentation rail, on a real Postgres.

WHY THROUGH THE APP AND NOT THROUGH THE FUNCTIONS. `presentation_extends` already
has its own pg suite; what it cannot prove is that a browser reaches those rules.
`visualization_specs` shipped seven handlers whose module tests were green while
every request answered 404 because two mount lines were missing -- that is the
class of defect this file exists for. Every request below goes through
`core.admin_api.router`, the object the ASGI app is built from, so the paths, the
methods, the statuses and the envelope keys are the ones the console receives.

WHAT IT MEASURES THAT NO MODULE TEST CAN. The store has NO authorization guard by
design (its docstring says so); the guard is HERE, and it is the whole of the
tenant boundary:

  * a member of organization A reading organization B gets the SAME answer as one
    reading an organization nobody created -- one envelope, so two answers side by
    side cannot enumerate the platform;
  * a project of another organization, named beside an organization the caller IS
    a member of, is that same answer;
  * choosing how a number is shown is an owner's or an admin's gesture: a plain
    member reads, and is refused on PUT and on DELETE with the one refusal that
    names its gesture -- reached only by a PROVEN member, so it discloses nothing;
  * PLATFORM is refused at the door as well as by the CHECK constraint.

EVERY TEST ROLLS BACK. The handlers call `conn.commit()`; that lands on a proxy
whose commit is a no-op, so the assertions read uncommitted rows on the same
connection and the fixture rolls them back.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from ulid import ULID

from tests.core.test_presentation_extends_pg import Scope

pytestmark = pytest.mark.usefixtures("live_postgres")

OWNER = "owner@example.com"
MEMBER = "member@example.com"
STRANGER = "stranger@example.com"

_PATH = "/api/presentation-extends"
_HISTORY = "/api/presentation-extends/history"


class _NoCommit:
    """The fixture's connection, with `commit()` swallowed so the test rolls back."""

    def __init__(self, conn):
        self._conn = conn

    def commit(self) -> None:
        return None

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


@pytest.fixture()
def scope(live_postgres):
    """The two organizations, their projects, the concept -- plus the memberships.

    The guard asks `core.project_access.identity_has_org_access` and
    `identity_can_manage_org`, and both read `app.org_members` for real here: a
    patched role gate would prove the handler's ORDER and not the boundary.
    """
    built = Scope(live_postgres).build()
    with live_postgres.cursor() as cur:
        for identity, role in ((OWNER, "owner"), (MEMBER, "member")):
            cur.execute(
                "INSERT INTO app.org_members (id, org_id, identity, role, status) "
                "VALUES (%s, %s, %s, %s, 'active')",
                (f"om_{ULID()}", built.org_id, identity, role),
            )
        # The stranger is a real member -- of the OTHER organization. A caller who
        # belongs nowhere would prove less: what is measured is that belonging to
        # one organization buys nothing in another.
        cur.execute(
            "INSERT INTO app.org_members (id, org_id, identity, role, status) "
            "VALUES (%s, %s, %s, 'owner', 'active')",
            (f"om_{ULID()}", built.other_org_id, STRANGER),
        )
    yield built
    live_postgres.rollback()


@contextmanager
def _client(scope, identity: str):
    """The real router, the real store, our connection.

    Nothing about the rail is stubbed: only `_check_auth`, which answers WHO is
    calling, and the two connection seams, which answer WHICH database. The guard,
    the store and the SQL below them are the shipped ones.
    """
    import core.db
    from core.admin_api import router
    from starlette.testclient import TestClient

    @contextmanager
    def _connection(*_args, **_kwargs):
        # A SAVEPOINT rather than a transaction of its own: a refused call rolls
        # back what the handler wrote and keeps the seeded scope for the
        # assertions -- exactly what production's connection does with an
        # uncommitted body.
        with scope.conn.transaction():
            yield _NoCommit(scope.conn)

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, identity))),
        patch("core.api_auth.authenticate_api_request", return_value=(True, identity)),
        patch.object(core.db, "get_connection", _connection),
        patch.object(core.db, "request_connection", _connection),
    ):
        with TestClient(router, raise_server_exceptions=True) as client:
            yield client


def _concept_params(scope, **extra) -> dict:
    params = {"object_type": "semantic_concept", "object_id": scope.concept_id}
    params.update(extra)
    return params


def _body(scope, **overrides) -> dict:
    payload = {
        "scope_level": "ORG",
        "org_id": scope.org_id,
        "object_type": "semantic_concept",
        "object_id": scope.concept_id,
        "display": {"format": {"kind": "percent"}},
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# 1. The read a member is entitled to -- and what it says about what is served.
# ---------------------------------------------------------------------------


def test_a_member_reads_the_resolved_block_and_what_of_it_is_applied(scope):
    with _client(scope, MEMBER) as client:
        response = client.get(_PATH, params=_concept_params(scope, org_id=scope.org_id))
    assert response.status_code == 200
    body = response.json()
    assert body["object_id"] == scope.concept_id
    assert body["resolved"]["display"]["format"]["kind"] == "currency"
    assert body["resolved"]["sources"]["format.kind"] == "PLATFORM"
    assert body["own"] is None

    # THE MARKER THIS FILE WAS ASKED FOR. A caller must never have to infer from
    # silence whether a value it stored is on its charts.
    application = body["application"]
    assert application["color"]["applied"] is False
    assert application["color"]["reason"]
    assert application["default_filter"]["applied"] is False
    assert application["default_filter"]["reason"]
    # Nobody overrode anything, so the definition's own format is a baseline the
    # caller reads -- not a style the product applies behind them.
    assert application["format"]["applied"] is False


def test_a_stored_colour_is_read_back_and_still_says_it_reaches_no_figure(scope):
    with _client(scope, OWNER) as client:
        written = client.put(
            _PATH,
            json=_body(scope, display={"color": {"series": "#1F77B4"}}),
        )
        assert written.status_code == 200, written.text
        response = client.get(
            _PATH, params=_concept_params(scope, org_id=scope.org_id, scope_level="ORG")
        )
    body = response.json()
    assert body["resolved"]["display"]["color"]["series"] == "#1f77b4"
    assert body["resolved"]["sources"]["color.series"] == "ORG"
    assert body["own"]["version"]["version_number"] == 1
    assert body["application"]["color"]["applied"] is False


# ---------------------------------------------------------------------------
# 2. Organization A is not organization B, and the answer never says which.
# ---------------------------------------------------------------------------


def test_a_member_of_one_organization_cannot_read_another(scope):
    with _client(scope, MEMBER) as client:
        response = client.get(
            _PATH, params=_concept_params(scope, org_id=scope.other_org_id)
        )
    assert response.status_code == 404
    absent_org = _unreachable_envelope(scope, MEMBER)
    # ONE envelope for "you may not" and for "it does not exist": two of them side
    # by side would be an oracle over the platform's organizations.
    assert response.json() == absent_org


def _unreachable_envelope(scope, identity: str) -> dict:
    with _client(scope, identity) as client:
        return client.get(
            _PATH, params=_concept_params(scope, org_id=f"org_{ULID()}")
        ).json()


def test_a_project_of_another_organization_is_unreachable(scope):
    with _client(scope, MEMBER) as client:
        response = client.get(
            _PATH,
            params=_concept_params(
                scope, org_id=scope.org_id, project_id=scope.other_project_id
            ),
        )
    assert response.status_code == 404


def test_a_write_onto_a_project_of_another_organization_is_unreachable(scope):
    with _client(scope, OWNER) as client:
        response = client.put(
            _PATH,
            json=_body(
                scope,
                scope_level="PROJECT",
                org_id=scope.org_id,
                project_id=scope.other_project_id,
            ),
        )
    assert response.status_code == 404


def test_a_stranger_cannot_dress_a_concept_of_the_organization_he_is_not_in(scope):
    with _client(scope, STRANGER) as client:
        response = client.put(_PATH, json=_body(scope))
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 3. The manage gate: reading is a member's, choosing is an owner's.
# ---------------------------------------------------------------------------


def test_a_plain_member_is_refused_the_write_and_told_the_gesture(scope):
    with _client(scope, MEMBER) as client:
        response = client.put(_PATH, json=_body(scope))
    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "forbidden"
    # The one refusal that is not an existence question, so it may name its remedy.
    assert "owner" in body["message"] and "admin" in body["message"]


def test_a_plain_member_is_refused_the_clear(scope):
    with _client(scope, OWNER) as client:
        assert client.put(_PATH, json=_body(scope)).status_code == 200
    with _client(scope, MEMBER) as client:
        response = client.delete(
            _PATH, params=_concept_params(scope, scope_level="ORG", org_id=scope.org_id)
        )
    assert response.status_code == 403


def test_an_owner_sets_then_clears_and_the_history_holds_both(scope):
    with _client(scope, OWNER) as client:
        assert client.put(_PATH, json=_body(scope)).status_code == 200
        cleared = client.delete(
            _PATH, params=_concept_params(scope, scope_level="ORG", org_id=scope.org_id)
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["cleared"] is True
        history = client.get(
            _HISTORY,
            params=_concept_params(scope, scope_level="ORG", org_id=scope.org_id),
        )
    assert history.status_code == 200
    versions = history.json()["versions"]
    assert [(v["version_number"], v["cleared"]) for v in versions] == [(2, True), (1, False)]


def test_clearing_a_scope_that_never_spoke_is_a_named_not_found(scope):
    with _client(scope, OWNER) as client:
        response = client.delete(
            _PATH, params=_concept_params(scope, scope_level="ORG", org_id=scope.org_id)
        )
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


# ---------------------------------------------------------------------------
# 4. What the door refuses before the store is ever reached.
# ---------------------------------------------------------------------------


def test_the_platform_scope_is_refused_at_the_door(scope):
    with _client(scope, OWNER) as client:
        response = client.put(_PATH, json=_body(scope, scope_level="PLATFORM"))
    assert response.status_code == 403
    assert "platform" in response.json()["message"].lower()


def test_an_unknown_object_type_is_refused_by_name(scope):
    with _client(scope, OWNER) as client:
        response = client.put(_PATH, json=_body(scope, object_type="dimension"))
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_param"


def test_a_refused_display_names_every_reason_and_writes_nothing(scope):
    with _client(scope, OWNER) as client:
        response = client.put(
            _PATH,
            json=_body(scope, display={"format": {"kind": "loud", "decimals": 12}}),
        )
        assert response.status_code == 422
        codes = {r["code"] for r in response.json()["refusals"]}
        assert {"invalid_format_kind", "invalid_decimals"} <= codes
        after = client.get(
            _PATH, params=_concept_params(scope, org_id=scope.org_id, scope_level="ORG")
        )
    assert after.json()["own"] is None


def test_a_metric_override_travels_the_same_doors(scope):
    """The second object type, through HTTP -- and it says it dresses no figure."""
    name = f"story_75_6_http_{ULID()}"
    scope.metric(name, format_text="currency")
    with _client(scope, OWNER) as client:
        written = client.put(
            _PATH,
            json=_body(
                scope,
                object_type="metric",
                object_id=name,
                display={"format": {"kind": "percent"}},
            ),
        )
        assert written.status_code == 200, written.text
        read = client.get(
            _PATH,
            params={
                "object_type": "metric",
                "object_id": name,
                "org_id": scope.org_id,
                "scope_level": "ORG",
            },
        )
    body = read.json()
    assert body["resolved"]["display"]["format"]["kind"] == "percent"
    assert body["resolved"]["sources"]["format.kind"] == "ORG"
    assert body["application"]["format"]["applied"] is False
