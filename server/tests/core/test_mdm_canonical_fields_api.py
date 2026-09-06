"""Lot A1 (issue #68) -- the read surface of the MDM canonical vocabulary.

WHAT IS PROVEN HERE, and why it is proven WITHOUT a database. The value of this
route is four answers a screen has to be able to tell apart, and none of them
needs a live Postgres to be exercised:

  * **401** with no identity -- no answer at all, not an empty one;
  * **404** on a Project the guarded org does not own, never a 403: a 403 would
    confirm the Project exists (lesson F-3 of story 27.2);
  * **200 with an empty list AND a reason** when the vocabulary holds nothing.
    This is the state EVERY Project is in today (`app.mdm_canonical_fields` held
    zero rows at both scopes, measured 2026-08-08), so a 404 here would tell a
    person the page does not exist when what is true is that nobody has declared
    a field yet;
  * **503 with NO list** when the read fails. "I could not read the vocabulary"
    and "the vocabulary is empty" are different facts, and only one of them
    invites a person to start declaring.

And the fifth thing, which is the one lot A1 exists for: the `scope` a row
carries is DERIVED from `project_id IS NULL` and is not a stored word. A platform
row and a project row of the same name may both exist -- the schema says so with
two partial unique indexes -- and a caller that could not tell them apart would
be looking at the shared vocabulary believing it was its own.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

IDENTITY = "owner@example.com"
ORG = "org_EXAMPLE"
PROJECT = "proj_EXAMPLE"
BASE = f"/api/projects/{PROJECT}/mdm/canonical-fields"

#: The SELECT of `list_visible_canonical_fields`, in column order.
_COLUMNS = (
    "id",
    "project_id",
    "canonical_name",
    "concept_kind",
    "value_type",
    "aggregation",
    "non_additive",
    "unit",
    "object_kind",
    "description",
    "dictionary_field_name",
    "status",
)


def _row(**overrides):
    base = {
        "id": "mdm_0000000000000000000000000A",
        "project_id": None,
        "canonical_name": "cost",
        "concept_kind": "metric",
        "value_type": "money",
        "aggregation": "sum",
        "non_additive": False,
        "unit": None,
        "object_kind": None,
        "description": None,
        "dictionary_field_name": None,
        "status": "active",
    }
    base.update(overrides)
    return tuple(base[column] for column in _COLUMNS)


class _Cursor:
    """Answers the two statements this route runs, by the text it was given.

    Dispatching on the SQL rather than on call order keeps the fake honest: a
    handler that reordered its queries would silently pass a positional fake.
    """

    def __init__(self, org_row, field_rows, raise_on_fields=False):
        self._org_row = org_row
        self._field_rows = field_rows
        self._raise = raise_on_fields
        self._pending = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        if "app.projects" in sql:
            self._pending = "org"
            return
        if "app.mdm_canonical_fields" in sql:
            if self._raise:
                raise RuntimeError("the vocabulary store did not answer")
            self._pending = "fields"
            return
        raise AssertionError(f"unexpected statement: {sql}")

    def fetchone(self):
        assert self._pending == "org"
        return self._org_row

    def fetchall(self):
        assert self._pending == "fields"
        return list(self._field_rows)


class _Connection:
    def __init__(self, org_row, field_rows, raise_on_fields=False):
        self._args = (org_row, field_rows, raise_on_fields)

    def cursor(self):
        return _Cursor(*self._args)


def _serving(org_row=(ORG,), field_rows=(), raise_on_fields=False):
    """Patch the connection factory and the access check the guard consults."""

    @contextmanager
    def _fake_connection():
        yield _Connection(org_row, field_rows, raise_on_fields)

    return (
        patch("core.db.get_connection", _fake_connection),
        patch("core.project_access.identity_has_org_access", return_value=True),
    )


def _client():
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    return TestClient(build_asgi_app(), raise_server_exceptions=False)


def _auth_ok():
    return patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, IDENTITY)))


def _get(**kwargs):
    connection, access = _serving(**kwargs)
    with _auth_ok(), connection, access, _client() as client:
        return client.get(BASE)


# ===========================================================================
# The route exists at the address the screen asks for
# ===========================================================================


def test_the_route_is_mounted_under_the_project_address():
    from core.admin_api import router

    paths = {getattr(route, "path", "") for route in router.routes}
    assert "/api/projects/{project_id}/mdm/canonical-fields" in paths


def test_no_authentication_is_no_answer():
    with patch("core.admin_api._check_auth", new=AsyncMock(return_value=(False, ""))):
        with _client() as client:
            response = client.get(BASE)
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


# ===========================================================================
# The empty vocabulary, which is what every Project has today
# ===========================================================================


def test_an_empty_vocabulary_is_200_with_a_reason_and_never_a_404():
    response = _get(field_rows=())
    assert response.status_code == 200
    body = response.json()
    assert body["fields"] == []
    assert body["scope_counts"] == {"platform": 0, "project": 0}
    assert body["empty_reason"] is not None
    assert body["empty_reason"]["code"] == "no_canonical_field_declared"


def test_the_empty_reason_speaks_the_reader_s_words_and_names_no_table():
    body = _get(field_rows=()).json()
    message = body["empty_reason"]["message"]
    for forbidden in ("mdm_canonical_fields", "project_id", "NULL", "row", "migration"):
        assert forbidden not in message, message
    # And it names the gesture rather than only the absence.
    assert "declared" in message


def test_a_populated_vocabulary_carries_no_empty_reason():
    body = _get(field_rows=(_row(),)).json()
    assert body["empty_reason"] is None


# ===========================================================================
# The scope, which is the point of the route
# ===========================================================================


def test_the_scope_is_derived_from_the_absence_of_a_project_and_not_stored():
    body = _get(
        field_rows=(
            _row(id="mdm_PLATFORM", project_id=None, canonical_name="cost"),
            _row(
                id="mdm_PROJECT",
                project_id=PROJECT,
                canonical_name="cost",
                object_kind="video",
            ),
        )
    ).json()
    scopes = {field["id"]: field["scope"] for field in body["fields"]}
    assert scopes == {"mdm_PLATFORM": "platform", "mdm_PROJECT": "project"}
    # The same NAME at both scopes is legitimate -- two partial unique indexes
    # allow it on purpose -- so the answer must distinguish them by scope alone.
    assert {field["canonical_name"] for field in body["fields"]} == {"cost"}
    assert body["scope_counts"] == {"platform": 1, "project": 1}


def test_every_field_the_brief_names_reaches_the_caller():
    body = _get(
        field_rows=(
            _row(
                id="mdm_ONE",
                project_id=PROJECT,
                canonical_name="duration_s",
                concept_kind="metric",
                value_type="duration",
                aggregation="sum",
                object_kind="video",
            ),
        )
    ).json()
    field = body["fields"][0]
    for key in (
        "id",
        "canonical_name",
        "concept_kind",
        "value_type",
        "aggregation",
        "object_kind",
        "scope",
    ):
        assert key in field, key
    assert field["value_type"] == "duration"
    assert field["object_kind"] == "video"


def test_a_non_additive_metric_is_named_as_such_rather_than_left_blank():
    """A blank aggregation with no other signal reads as an unfinished field.

    The database refuses a metric that declares neither an aggregation nor
    `non_additive`, so the second is always the explanation of the first.
    """
    body = _get(
        field_rows=(_row(id="mdm_NA", aggregation=None, non_additive=True),)
    ).json()
    assert body["fields"][0]["aggregation"] is None
    assert body["fields"][0]["non_additive"] is True


# ===========================================================================
# Failing closed, and honestly
# ===========================================================================


def test_an_unreadable_vocabulary_is_503_and_carries_no_list_of_any_kind():
    response = _get(raise_on_fields=True)
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "canonical_fields_unavailable"
    assert "fields" not in body
    assert "not a count of zero" in body["message"]


def test_a_project_the_caller_cannot_reach_is_404_and_never_403():
    """Existence-hiding: a 403 would confirm the Project exists."""

    @contextmanager
    def _fake_connection():
        yield _Connection((ORG,), ())

    with (
        _auth_ok(),
        patch("core.db.get_connection", _fake_connection),
        patch("core.project_access.identity_has_org_access", return_value=False),
        _client() as client,
    ):
        response = client.get(BASE)
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_a_project_that_does_not_exist_is_404_too():
    response = _get(org_row=None)
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


# ===========================================================================
# The reader itself -- one SQL, two doors
# ===========================================================================


def test_the_reader_asks_for_both_scopes_and_only_active_rows():
    """The Governance lens and this route read through the SAME function.

    A catalog narrower than what the binding validators accept hides part of the
    vocabulary; one wider offers a field the validator then refuses. Both are the
    same defect in opposite directions, so the predicate is asserted here rather
    than trusted.
    """
    from core.canonical_field_registry import list_visible_canonical_fields

    seen: list[tuple[str, tuple]] = []

    class _Recorder(_Cursor):
        def execute(self, sql, params=None):
            seen.append((sql, params))
            super().execute(sql, params)

    class _Conn:
        def cursor(self):
            return _Recorder(None, (_row(),))

    fields = list_visible_canonical_fields(_Conn(), project_id=PROJECT)
    sql = seen[0][0]
    assert "status = 'active'" in sql
    assert "project_id = %(project_id)s OR project_id IS NULL" in sql
    # Named, not positional: the Governance lens guard
    # (`test_every_query_is_scoped_by_the_authenticated_project_or_organization`)
    # requires every lens query to NAME the scope it is bound to, and this reader
    # serves that lens as well as this route.
    assert seen[0][1] == {"project_id": PROJECT}
    assert fields[0]["scope"] == "platform"


@pytest.mark.parametrize("project_id", [None, PROJECT])
def test_the_reader_labels_each_row_by_its_own_project_column(project_id):
    from core.canonical_field_registry import list_visible_canonical_fields

    class _Conn:
        def cursor(self):
            return _Cursor(None, (_row(project_id=project_id),))

    field = list_visible_canonical_fields(_Conn(), project_id=PROJECT)[0]
    assert field["scope"] == ("platform" if project_id is None else "project")
