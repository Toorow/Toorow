"""Shopify error taxonomy -- story 67-21 (2026-08-17).

WHY THIS FILE EXISTS. ``manifest.error_map`` was empty on the note "no
provider-level numeric error codes refine beyond the HTTP status". Numeric, no
-- but Shopify's error vocabulary is not numeric and never was: it is the
verbatim string carried in ``{"errors": "<message>"}``, and one of those strings
is the single most actionable failure this connector can meet.

Uninstalling a Shopify app invalidates its access token IMMEDIATELY, with no
grace period, and every subsequent Admin API call answers::

    401 {"errors": "[API] Invalid API key or access token
         (unrecognized login or wrong password)"}

Pure HTTP calls that ``auth_expired`` -- "your credentials expired", which
suggests a refresh that Shopify will never grant. It is ``auth_revoked``: the
merchant has to reinstall or re-authorise the app. Two different sentences to
put in front of a person, and only the map can tell them apart.

The mechanical reason the map could not have worked before is separate and is
also repaired: ``core.pull_errors._extract_provider_codes`` has no branch for a
top-level ``errors`` STRING, so ``connector._shopify_error_payload`` copies it
into the ``reason`` slot core consults first (the stripe/square normalisation
pattern). Core keeps zero provider words (AD-2).

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

_MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "shopify"
_CONNECTOR_PATH = _MODULE_DIR / "connector.py"


@pytest.fixture(scope="module")
def connector():
    spec = importlib.util.spec_from_file_location(
        "connector_shopify_errors", _CONNECTOR_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def error_map() -> dict:
    """The REAL manifest map -- never a retyped copy of it."""
    manifest = json.loads((_MODULE_DIR / "manifest.json").read_text(encoding="utf-8"))
    return manifest["error_map"]


def _response(status: int, body: dict) -> httpx.Response:
    return httpx.Response(
        status,
        json=body,
        request=httpx.Request(
            "GET", "https://example.myshopify.com/admin/api/2024-07/orders.json"
        ),
    )


def _pull(connector, monkeypatch, response: httpx.Response):
    """Drive the real raise site of pull() with a canned response."""
    from core import nango_client

    monkeypatch.setattr(
        nango_client, "get_fresh_token", lambda *a, **k: "token-not-used", raising=False
    )
    monkeypatch.setattr(connector.httpx, "get", lambda *a, **k: response)
    return connector.pull(
        connection_id="conn_EXAMPLE",
        date_from="2026-08-01",
        date_to="2026-08-17",
        project_id="proj_EXAMPLE",
        pull_id="pull_EXAMPLE",
        shop_domain="example.myshopify.com",
    )


#: The body Shopify returns once the app is uninstalled or the token revoked.
_UNINSTALLED = {
    "errors": (
        "[API] Invalid API key or access token "
        "(unrecognized login or wrong password)"
    )
}

_NOT_FOUND = {"errors": "Not Found"}


def test_an_uninstalled_app_is_auth_revoked_and_keeps_the_payload(
    connector, monkeypatch
):
    """The real raise site, the real body, the class pure HTTP never reaches."""
    with pytest.raises(Exception) as exc_info:
        _pull(connector, monkeypatch, _response(401, _UNINSTALLED))

    err = exc_info.value
    assert err.error_class == "auth_revoked"
    assert err.retryable is False
    assert err.user_action == "reconnect"
    assert err.provider_status == 401

    # Evidence intact, and intact means IDENTICAL. The connector briefly copied
    # the message into a `reason` slot so core could key on it; core reads the
    # bare `errors` string itself since 2026-08-17, so the body handed to the
    # error is the body the API sent, key for key.
    assert err.provider_payload == _UNINSTALLED


def test_core_reads_the_bare_errors_string_with_no_help_from_the_module(
    connector, error_map
):
    """The map matches on the UNTOUCHED body -- that is the whole repair.

    `{"errors": "<message>"}` is one of the conventional shapes
    `core.pull_errors._extract_provider_codes` consults, so the connector hands
    the response body over exactly as received. Before that, core stopped at
    `error.code` / `code`, this body offered neither, and the manifest map could
    not match a single key: a 401 here classified as `auth_expired` ("reconnect")
    when the truth was `auth_revoked` ("the app was uninstalled, grant it again").
    """
    from core.pull_errors import classify_http_error

    assert classify_http_error(401, _UNINSTALLED, error_map).error_class == "auth_revoked"
    # And without the map it is still only the pure-HTTP class -- the map is what
    # refines, not the shape.
    assert classify_http_error(401, _UNINSTALLED, None).error_class == "auth_expired"


def test_a_404_stops_being_retried_forever(connector, monkeypatch):
    """core leaves 404 'unclassified', which is RETRYABLE."""
    from core.pull_errors import classify_http_error

    with pytest.raises(Exception) as exc_info:
        _pull(connector, monkeypatch, _response(404, _NOT_FOUND))

    assert exc_info.value.error_class == "invalid_request"
    assert exc_info.value.retryable is False
    assert classify_http_error(404, None, None).error_class == "unclassified"


def test_a_field_keyed_errors_object_yields_no_code_and_no_verdict(error_map):
    """The 422 write shape is an OBJECT keyed by field, not an error code.

    `{"errors": {"order": ["is invalid"]}}` names which FIELD was rejected. None
    of those keys is an error code, and reading one as though it were would let a
    field name collide with a map key. Core extracts nothing from this shape, so
    the verdict stays the pure-HTTP one and the body is preserved whole.
    """
    from core.pull_errors import _extract_provider_codes, classify_http_error

    body = {"errors": {"order": ["is invalid"]}}
    assert _extract_provider_codes(body) == []
    error = classify_http_error(422, body, error_map)
    assert error.error_class == classify_http_error(422, None, None).error_class
    assert error.provider_payload == body


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
    assert set(refining) == set(error_map)


def test_the_map_has_the_only_shape_core_can_read(error_map):
    """Keys are "<status>:<code>", values are canonical, and 429 is never keyed."""
    from core.pull_errors import ERROR_CLASSES

    for key, value in error_map.items():
        status, separator, code = key.partition(":")
        assert separator and status.isdigit() and len(status) == 3, key
        assert code, key
        assert status != "429", f"{key}: a 429 goes to the quota breaker, never here"
        assert value in ERROR_CLASSES, f"{key} -> {value}"


def test_a_429_still_goes_to_the_breaker(connector, monkeypatch):
    """The leaky-bucket overflow keeps the Story 3.3 contract, untouched."""
    from core.quota import RateLimitError

    response = httpx.Response(
        429,
        headers={"Retry-After": "2"},
        json={"errors": "Too Many Requests"},
        request=httpx.Request(
            "GET", "https://example.myshopify.com/admin/api/2024-07/orders.json"
        ),
    )
    with pytest.raises(RateLimitError) as exc_info:
        _pull(connector, monkeypatch, response)
    assert exc_info.value.retry_after == 2


def test_the_connector_hands_the_map_to_core(connector, error_map):
    """A map nobody passes to classify_http_error classifies nothing."""
    assert connector._load_error_map() == error_map
    source = _CONNECTOR_PATH.read_text(encoding="utf-8")
    assert source.count("_load_error_map()") >= 3  # all three raise sites


def test_the_note_names_where_the_codes_come_from():
    """A code with no cited reference is indistinguishable from an invented one."""
    manifest = json.loads((_MODULE_DIR / "manifest.json").read_text(encoding="utf-8"))
    note = manifest.get("_error_map_note")
    assert note, "a declared map must carry its reference"
    assert note.isascii()  # AI-03
