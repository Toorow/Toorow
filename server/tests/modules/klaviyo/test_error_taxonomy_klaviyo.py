"""Klaviyo error taxonomy -- story 67-21 (2026-08-17).

WHY THIS FILE EXISTS. ``manifest.error_map`` was empty, on a reading that has
not survived a second look: "the 'code' field is a free-text string (e.g.
'invalid', 'not_found') rather than an enumerated integer". Klaviyo's JSON:API
``code`` is a STABLE ENUMERATED STRING -- 'not_authenticated',
'authentication_failed', 'permission_denied', 'invalid', 'not_found',
'server_error' are published in the provider's own error-handling reference. A
code does not have to be a number to be a code.

The other half of the note WAS right and is repaired here: core never descends
into a top-level ``errors`` ARRAY, so no key could have matched. The connector
now lifts the first error's ``code`` to the top level
(core reads the JSON:API `errors[]` array itself) AND hands the map to
``classify_http_error`` -- ``_post_reporting`` used to call it with two
arguments, so no Klaviyo error had ever been refined by this manifest.

What changes for a person: a Klaviyo private key never expires, so a rejected
key is one the account rotated or deleted. ``401 authentication_failed`` now
says ``auth_revoked`` (issue a new key) instead of ``auth_expired`` (wait for a
refresh that does not exist). And ``404 not_found`` stops being core's
retryable ``unclassified``, so a metric id that no longer exists is not requeued
to the dead letter.

Offline: no network, no DB, no token.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")

_MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "klaviyo"
_CONNECTOR_PATH = _MODULE_DIR / "connector.py"


@pytest.fixture(scope="module")
def connector():
    spec = importlib.util.spec_from_file_location(
        "connector_klaviyo_errors", _CONNECTOR_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def error_map() -> dict:
    """The REAL manifest map -- never a retyped copy of it."""
    manifest = json.loads((_MODULE_DIR / "manifest.json").read_text(encoding="utf-8"))
    return manifest["error_map"]


class _CannedClient:
    """The smallest thing ``_post_reporting`` needs: something with ``.post``."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    def post(self, *args, **kwargs) -> httpx.Response:
        return self._response


def _response(status: int, body: dict) -> httpx.Response:
    return httpx.Response(
        status,
        json=body,
        request=httpx.Request("POST", "https://example.com/api/campaign-values-reports"),
    )


def _post(connector, response: httpx.Response):
    return connector._post_reporting(
        _CannedClient(response), "campaign-values-reports", {}, {}
    )


#: The JSON:API body Klaviyo returns for a key it no longer recognises.
_AUTHENTICATION_FAILED = {
    "errors": [
        {
            "id": "00000000-0000-0000-0000-000000000000",
            "status": 401,
            "code": "authentication_failed",
            "title": "Incorrect authentication credentials.",
            "detail": "Missing or invalid authorization",
            "source": {"pointer": "/data/attributes/"},
        }
    ]
}

_NOT_FOUND = {
    "errors": [
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "status": 404,
            "code": "not_found",
            "title": "Not found.",
            "detail": "The requested metric does not exist.",
        }
    ]
}


def test_a_401_authentication_failed_is_auth_revoked_and_keeps_the_payload(connector):
    """The real raise site, the real body, the class pure HTTP never reaches."""
    with pytest.raises(Exception) as exc_info:
        _post(connector, _response(401, _AUTHENTICATION_FAILED))

    err = exc_info.value
    assert err.error_class == "auth_revoked"
    assert err.retryable is False
    assert err.user_action == "reconnect"
    assert err.provider_status == 401

    # Evidence intact: the whole errors array is preserved verbatim.
    # Evidence intact, and intact means IDENTICAL. The connector briefly lifted
    # the JSON:API `errors[0].code` to the top level so core could key on it; core
    # reads `errors[].code` itself since 2026-08-17, so the body carried as
    # evidence is the body the API sent, key for key.
    assert err.provider_payload == _AUTHENTICATION_FAILED


def test_a_404_not_found_stops_being_retried_forever(connector, error_map):
    """core leaves 404 'unclassified', which is RETRYABLE. A dead metric is not."""
    from core.pull_errors import classify_http_error

    with pytest.raises(Exception) as exc_info:
        _post(connector, _response(404, _NOT_FOUND))

    assert exc_info.value.error_class == "invalid_request"
    assert exc_info.value.retryable is False
    # Without the map, core leaves a 404 `unclassified` -- and unclassified is
    # RETRYABLE, so a metric that no longer exists would be requeued forever.
    assert classify_http_error(404, _NOT_FOUND, None).error_class == "unclassified"
    # With it, on the very same untouched body, the answer is terminal.
    assert classify_http_error(404, _NOT_FOUND, error_map).error_class == "invalid_request"


def test_core_reads_the_json_api_array_with_no_help_from_the_module(error_map):
    """The map matches on the UNTOUCHED body -- that is the whole repair.

    `{"errors": [{"code": ...}]}` is the JSON:API error shape and one
    `core.pull_errors._extract_provider_codes` consults. Before that, core stopped
    at `error.code` / `code`, this body offered neither, and no key of the map
    could match: a revoked API key answered 401 and classified `auth_expired`
    ("refresh it") when the truth was `auth_revoked` ("issue a new one").
    """
    from core.pull_errors import classify_http_error

    assert (
        classify_http_error(401, _AUTHENTICATION_FAILED, error_map).error_class
        == "auth_revoked"
    )
    # The map is what refines: the shape alone still gives the pure-HTTP class.
    assert (
        classify_http_error(401, _AUTHENTICATION_FAILED, None).error_class
        == "auth_expired"
    )


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
        "401:authentication_failed": "auth_revoked",
        "404:not_found": "invalid_request",
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


def test_422_is_still_left_to_core(error_map):
    """422 is generic HTTP semantics, not Klaviyo vocabulary.

    core.pull_errors._base_class_for_status has owned it since 2026-08-01. A key
    here would be a second implementation of a judgment core already makes.
    """
    from core.pull_errors import classify_http_error

    assert not [key for key in error_map if key.startswith("422:")]
    assert classify_http_error(422, None, None).error_class == "invalid_request"


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
