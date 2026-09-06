"""monday error taxonomy -- story 67-21 (2026-08-17).

WHY THIS FILE EXISTS. ``manifest.error_map`` was empty on the argument that
monday "answers HTTP 200 and puts the failure in errors[].extensions.code", so
"there is no HTTP status to key these codes on". That is true of the GraphQL
layer and only of it. monday also publishes a table of errors carried on real
4xx/5xx statuses (developer.monday.com, "Error codes"), and it puts the code in
a TOP-LEVEL ``error_code`` -- the exact shape ``core.pull_errors`` already reads
first. No normalisation is needed here; only the keys were missing.

Two things were also wired shut. ``graphql_request`` raised
``MondayGraphQLError("auth_expired" / "permission_denied")`` on 401 and 403
BEFORE core saw the response, so:

  * the worker got an exception with no canonical ``error_class`` on the two
    statuses that matter most, and
  * no ``401:`` or ``403:`` key could ever have fired.

Both statuses now route through ``classify_http_error``. What that buys a
person: a monday OAuth access token has NO expiry and there is no refresh token,
so a rejected token was revoked -- ``auth_revoked``, reconnect -- and an IP
restriction is ``permission_denied``, because reconnecting cannot lift an
account allowlist and telling someone to try again sends them round a loop.

Offline: no network, no DB, no token.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")

_MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "monday"
_CONNECTOR_PATH = _MODULE_DIR / "connector.py"

# The ``connector`` fixture comes from this directory's conftest.py (session
# scoped) -- one load of the module for the whole suite.


@pytest.fixture(scope="module")
def error_map() -> dict:
    """The REAL manifest map -- never a retyped copy of it."""
    manifest = json.loads((_MODULE_DIR / "manifest.json").read_text(encoding="utf-8"))
    return manifest["error_map"]


class _CannedClient:
    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    def post(self, *args, **kwargs) -> httpx.Response:
        return self._response


def _response(connector, status: int, body: dict) -> httpx.Response:
    return httpx.Response(
        status,
        json=body,
        headers={"API-Version": connector.API_VERSION},
        request=httpx.Request("POST", "https://example.com/v2"),
    )


def _query(connector, status: int, body: dict):
    return connector.graphql_request(
        _CannedClient(_response(connector, status, body)), "token", "{ me { id } }"
    )


#: The shape a handled monday failure takes (developer.monday.com, Error codes).
_UNAUTHORIZED = {
    "error_code": "Unauthorized",
    "status_code": 401,
    "error_message": "Not Authenticated!",
}

_IP_RESTRICTED = {
    "error_code": "Your ip is restricted",
    "status_code": 401,
    "error_message": "Your ip is restricted",
}

_RESOURCE_NOT_FOUND = {
    "error_code": "ResourceNotFoundException",
    "status_code": 404,
    "error_message": "Resource not found",
}


def test_a_401_is_auth_revoked_and_keeps_the_payload(connector):
    """A monday OAuth token has no expiry: a rejected one was revoked."""
    with pytest.raises(Exception) as exc_info:
        _query(connector, 401, _UNAUTHORIZED)

    err = exc_info.value
    assert err.error_class == "auth_revoked"
    assert err.retryable is False
    assert err.user_action == "reconnect"
    assert err.provider_status == 401
    assert err.provider_payload == _UNAUTHORIZED


def test_a_401_and_a_403_now_reach_core_at_all(connector):
    """Both branches used to short-circuit into MondayGraphQLError.

    They raised BEFORE ``classify_http_error`` was ever called, with a class
    string chosen in the module ("auth_expired" / "permission_denied") -- so the
    manifest could have carried a perfect ``401:`` or ``403:`` key and nothing
    would have read it. The point of this test is the ABSENCE of the shortcut.
    """
    from core.pull_errors import ConnectorError

    for status, body in (
        (401, _UNAUTHORIZED),
        (403, {"error_code": "UserUnauthorizedException", "status_code": 403}),
    ):
        with pytest.raises(ConnectorError) as exc_info:
            _query(connector, status, body)
        err = exc_info.value
        assert not isinstance(err, connector.MondayGraphQLError), status
        assert err.provider_status == status


def test_an_ip_restriction_is_not_a_reconnect(connector):
    """Same 401, opposite instruction.

    Pure HTTP calls every 401 ``auth_expired`` -- "reconnect". Reconnecting
    cannot lift an account IP allowlist, so the person would loop. The map is
    the only place that can tell the two apart.
    """
    from core.pull_errors import classify_http_error

    with pytest.raises(Exception) as exc_info:
        _query(connector, 401, _IP_RESTRICTED)

    assert exc_info.value.error_class == "permission_denied"
    assert classify_http_error(401, _IP_RESTRICTED, None).error_class == "auth_expired"


def test_a_404_is_an_access_problem_not_an_unknown(connector):
    """monday's own table reads 'invalid user ID OR insufficient access'.

    core leaves a 404 ``unclassified``, i.e. retried to the dead letter.
    """
    from core.pull_errors import classify_http_error

    with pytest.raises(Exception) as exc_info:
        _query(connector, 404, _RESOURCE_NOT_FOUND)

    assert exc_info.value.error_class == "permission_denied"
    assert (
        classify_http_error(404, _RESOURCE_NOT_FOUND, None).error_class == "unclassified"
    )


def test_the_graphql_layer_codes_are_not_keyed_here(connector, error_map):
    """They ride on HTTP 200, which never reaches classify_http_error.

    They live in ``connector._ERROR_CODES`` where they apply. A key for them
    here would be a dead key -- the defect AI-114 removed.
    """
    graphql_only = set(connector._ERROR_CODES)
    keyed = {key.split(":", 1)[1] for key in error_map}
    assert not (graphql_only & keyed)


def test_the_map_refines_at_least_one_verdict(error_map):
    """A map that restates pure HTTP proves nothing its absence would not.

    Same criterion as the conformance gate
    (tests/conformance/test_pull_contract.py section 7), asserted here on the
    REAL manifest so this module fails first and locally.
    """
    from core.pull_errors import classify_http_error

    assert error_map, "error_map is declared and must not be empty"
    refining = {
        key: value
        for key, value in error_map.items()
        if value
        != classify_http_error(int(key.split(":", 1)[0]), None, None).error_class
    }
    assert refining == {
        "401:Unauthorized": "auth_revoked",
        "401:Your ip is restricted": "permission_denied",
        "404:ResourceNotFoundException": "permission_denied",
    }


def test_the_map_has_the_only_shape_core_can_read(error_map):
    """Keys are "<status>:<code>", values are canonical, and 429 is never keyed."""
    from core.pull_errors import ERROR_CLASSES

    for key, value in error_map.items():
        status, separator, code = key.partition(":")
        assert separator and status.isdigit() and len(status) == 3, key
        assert code, key
        assert status != "429", f"{key}: a 429 goes to the quota breaker, never here"
        assert value in ERROR_CLASSES, f"{key} -> {value}"


def test_the_connector_hands_the_map_to_core(connector, error_map):
    """A map nobody passes to classify_http_error classifies nothing."""
    assert connector._load_error_map() == error_map
    assert "_load_error_map()" in _CONNECTOR_PATH.read_text(encoding="utf-8")


def test_the_note_names_where_the_codes_come_from():
    """A code with no cited reference is indistinguishable from an invented one."""
    manifest = json.loads((_MODULE_DIR / "manifest.json").read_text(encoding="utf-8"))
    note = manifest.get("_error_map_note")
    assert note, "a declared map must carry its reference"
    assert note.isascii()  # AI-03
