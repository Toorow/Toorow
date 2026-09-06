"""The two doors of match discovery, and the answers they keep apart (story 66.2).

A read refuses nothing, so there is no 422 here. What there is: no identity is
401, a foreign Project is 404 and never 403, an honest emptiness is 200 with the
reason that names the gesture, and a failed catalog is 503 carrying NO list --
because "I could not read the sources" and "nothing can be crossed" would send a
person to two different places.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tests.support.statement_router import (
    StatementInventory,
    UnknownStatement,
    describe,
)

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

IDENTITY = "owner@example.com"
ORG = "org_EXAMPLE"
PROJECT = "proj_EXAMPLE"
DATASTREAM = "ds_EXAMPLE"
CATALOG = f"/api/projects/{PROJECT}/analyze/matches"
PER_SOURCE = f"/api/projects/{PROJECT}/datastreams/{DATASTREAM}/matches"

_ANSWER = {
    "matches": [
        {
            "kind": "governed",
            "authority": "governed",
            "observed_coverage": "unavailable",
            "execution_safety": "ready",
            "left": {"datastream_id": "ds_a", "name": "Campaign spend"},
            "right": {"datastream_id": "ds_b", "name": "Conversions"},
        }
    ],
    "counts": {"datastreams_published": 2, "governed": 1, "candidates": 0, "returned": 1},
    "bounds": {"max_datastreams_scanned": 200, "max_matches": 50, "truncated": False},
    "observed_coverage_state": "unavailable",
    "empty_reason": None,
}


# THE ROW `app.projects` READ ANSWERS WITH, in the shape the product reads.
# `resolve_strict_resource_access` projects four columns -- org, org status,
# member role, member status (core/project_access.py:184) -- and consumes them
# as `row[:4]` (core/project_access.py:211), refusing `not_found` on anything
# shorter. The fixture used to hold `(ORG,)`, a one-column fossil that no
# statement of this product has projected for as long as the guard has looked
# like this; nothing noticed, because nothing read it (see `_GUARD` below).
_ORG_MEMBER_ROW = (ORG, "active", "owner", "active")


# EVERY STATEMENT THIS CURSOR ANSWERS, named. Both are real product statements
# on the guard path this fake stands in for:
#   `select current_user`  -- core/db.py:201, the probe `request_connection`
#      runs before arming the Epic-36 RLS floor; `analyze_connection`
#      (core/query_specs_api.py:82) IS that connection.
#   the project access read -- core/project_access.py:184, the only statement
#      `_guard` (core/datastream_matches_api.py:70) reaches.
#
# MEASURED 2026-08-30: no test in this file reaches this cursor at all.
# `_serving()` patches `analyze_connection` AND
# `resolve_strict_resource_access`, and each test patches its producer, so
# nothing below the connection ever issues a statement -- proven by making
# `cursor()` raise `SystemExit`, which left 12 passed. That is the worst of the
# two silences the census names: a fake mute over an EMPTY set, still holding a
# copy of a query, ready to answer a four-column read with one column the day
# either patch is dropped. It now names the statement instead.
_GUARD = StatementInventory(
    "_Cursor (datastream matches guard)",
    current_user="select current_user",
    project_access="from app.projects",
)


class _Cursor:
    def __init__(self, org_row):
        self._org_row = org_row
        self._row = None
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        match _GUARD.match(sql):
            case "current_user":
                # `describe` refuses a projection with no FROM, and is right to:
                # there is no relation to derive a name from. Named here, once.
                self.description = [("current_user",)]
                self._row = (IDENTITY,)
            case "project_access":
                self.description = describe(sql)
                self._row = self._org_row
            case _:  # pragma: no cover - a name added above, unanswered here
                raise _GUARD.unknown(sql)

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, org_row):
        self._org_row = org_row

    def cursor(self):
        return _Cursor(self._org_row)


def _serving(org_row=_ORG_MEMBER_ROW, allowed=True):
    @contextmanager
    def _fake_connection(_identity=None):
        yield _Connection(org_row)

    return (
        patch("core.query_specs_api.analyze_connection", _fake_connection),
        patch(
            "core.project_access.resolve_strict_resource_access",
            return_value=SimpleNamespace(allowed=allowed, org_id=ORG if allowed else None),
        ),
    )


def _client():
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    return TestClient(build_asgi_app(), raise_server_exceptions=False)


def _auth_ok():
    return patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, IDENTITY)))


@contextmanager
def _producer(**patches):
    active = []
    try:
        for name, replacement in patches.items():
            patcher = patch(f"core.datastream_matches.{name}", replacement)
            patcher.start()
            active.append(patcher)
        yield
    finally:
        for patcher in reversed(active):
            patcher.stop()


def test_both_addresses_are_mounted():
    from core.admin_api import router

    paths = {getattr(route, "path", "") for route in router.routes}
    assert "/api/projects/{project_id}/analyze/matches" in paths
    assert "/api/projects/{project_id}/datastreams/{datastream_id}/matches" in paths


def test_no_authentication_is_no_answer():
    with patch("core.admin_api._check_auth", new=AsyncMock(return_value=(False, ""))):
        with _client() as client:
            assert client.get(CATALOG).status_code == 401
            assert client.get(PER_SOURCE).status_code == 401


def test_a_project_the_org_does_not_own_is_404():
    connection, access = _serving(allowed=False)
    with _auth_ok(), connection, access, _client() as client:
        assert client.get(CATALOG).status_code == 404


def test_the_catalog_carries_the_matches_the_producer_composed():
    connection, access = _serving()
    with _auth_ok(), connection, access, _producer(discover_matches=lambda *a, **k: dict(_ANSWER)):
        with _client() as client:
            response = client.get(CATALOG)
    assert response.status_code == 200
    body = response.json()
    assert body["counts"]["governed"] == 1
    assert body["matches"][0]["observed_coverage"] == "unavailable"
    assert body["bounds"]["truncated"] is False


def test_an_honest_emptiness_is_200_with_the_gesture():
    empty = dict(_ANSWER)
    empty["matches"] = []
    empty["empty_reason"] = {
        "code": "no_published_datastream",
        "message": "No source publishes a mapping yet, so nothing can be crossed.",
    }
    connection, access = _serving()
    with _auth_ok(), connection, access, _producer(discover_matches=lambda *a, **k: empty):
        with _client() as client:
            response = client.get(CATALOG)
    assert response.status_code == 200
    assert response.json()["empty_reason"]["code"] == "no_published_datastream"


def test_a_catalog_that_could_not_be_read_is_503_and_carries_no_list():
    from core.datastream_matches import MatchesUnavailable

    def _boom(*_a, **_k):
        raise MatchesUnavailable("the store did not answer")

    connection, access = _serving()
    with _auth_ok(), connection, access, _producer(discover_matches=_boom):
        with _client() as client:
            response = client.get(CATALOG)
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "matches_unavailable"
    assert "matches" not in body


def test_the_datastream_door_answers_404_for_a_foreign_source():
    from core.datastream_matches import DatastreamNotFound

    def _missing(*_a, **_k):
        raise DatastreamNotFound(DATASTREAM)

    connection, access = _serving()
    with _auth_ok(), connection, access, _producer(matches_for_datastream=_missing):
        with _client() as client:
            response = client.get(PER_SOURCE)
    assert response.status_code == 404


def test_the_datastream_door_names_the_source_it_answered_for():
    connection, access = _serving()
    with _auth_ok(), connection, access, _producer(
        matches_for_datastream=lambda *a, **k: dict(_ANSWER)
    ):
        with _client() as client:
            response = client.get(PER_SOURCE)
    assert response.status_code == 200
    assert response.json()["datastream_id"] == DATASTREAM


# ===========================================================================
# The profile door (story 66.3): a refusal names the gesture, never an empty
# profile that would read as "these sources share nothing"
# ===========================================================================

PROFILE = f"/api/projects/{PROJECT}/analyze/matches/profile"


def test_the_profile_address_is_mounted_before_the_parameterised_ones():
    from core.admin_api import router

    paths = [getattr(route, "path", "") for route in router.routes]
    profile = "/api/projects/{project_id}/analyze/matches/profile"
    assert profile in paths
    assert paths.index(profile) < paths.index("/api/projects/{project_id}/analyze/matches")


def test_a_profile_without_its_three_identities_is_422_and_says_where_to_open_it():
    connection, access = _serving()
    with _auth_ok(), connection, access, _client() as client:
        response = client.get(PROFILE)
    assert response.status_code == 422
    assert response.json()["code"] == "profile_request_incomplete"


def test_a_refused_profile_carries_its_code_and_its_missing_link():
    from core.match_profile import ProfileRefused

    def _refuse(*_a, **_k):
        raise ProfileRefused(
            "no_published_output",
            "Conversions has never produced an output.",
            missing_link="datastream_output_versions",
        )

    connection, access = _serving()
    with _auth_ok(), connection, access, patch("core.match_profile.profile_match", _refuse):
        with _client() as client:
            response = client.get(
                PROFILE,
                params={
                    "left": "ds_a",
                    "right": "ds_b",
                    "common_key_version_id": "mckv_1",
                    "relationship_name": "rel",
                    "view_version_id": "svv_1",
                },
            )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "no_published_output"
    assert body["missing_link"] == "datastream_output_versions"


def test_a_measured_profile_reaches_the_caller_whole():
    measured = {
        "left": {"state": "exact", "total_rows": 12},
        "right": {"state": "exact", "total_rows": 9},
        "matched": {"state": "exact", "matched_keys": 7},
        "multiplication": {"state": "exact", "worst_case_rows_per_key": 1},
        "execution_safety": "ready",
    }
    connection, access = _serving()
    with _auth_ok(), connection, access, patch(
        "core.match_profile.profile_match", lambda *a, **k: measured
    ):
        with _client() as client:
            response = client.get(
                PROFILE,
                params={
                    "left": "ds_a",
                    "right": "ds_b",
                    "common_key_version_id": "mckv_1",
                    "relationship_name": "rel",
                    "view_version_id": "svv_1",
                },
            )
    assert response.status_code == 200
    assert response.json()["profile"]["execution_safety"] == "ready"


def test_the_fake_refuses_a_statement_it_was_never_taught():
    """AI-317: the cursor NAMES an unknown statement instead of answering it.

    `SELECT person_id FROM app.person_identities` is not a hypothetical query:
    it is what `canonical_identity` (core/identity_bridge.py:82) issues on this
    very guard path, before the project read, for any identity that is not
    already a `person_<ULID>` -- and `owner@example.com` is not one. The old
    fake met it with `assert "app.projects" in sql`, an AssertionError that
    `canonical_identity` swallows in its own `except Exception` and turns back
    into "no translation". Silence, with extra steps.
    """
    cursor = _Cursor(_ORG_MEMBER_ROW)
    with pytest.raises(UnknownStatement) as raised:
        cursor.execute(
            "SELECT person_id FROM app.person_identities WHERE subject = %s LIMIT 2"
        )
    message = str(raised.value)
    assert "app.person_identities" in message
    assert "project_access" in message
