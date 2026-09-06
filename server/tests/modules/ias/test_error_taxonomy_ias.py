"""IAS error taxonomy -- story 67-21 (2026-08-17).

WHY THIS FILE EXISTS. ``manifest.error_map`` was empty and its note gave the
reason: the IAS token endpoint answers ``{"error": "invalid_token", ...}``, a
STRING at the top-level ``error`` key, and ``core.pull_errors`` only descends
into an ``error`` OBJECT -- so "an error_map entry keyed on it could not refine
anything". The premise was right and the conclusion was wrong: what blocked the
key was the payload SHAPE, not the vocabulary, and reshaping a body before
handing it to core is what the stripe and square connectors already do.
core reads that RFC 6749 shape directly, so the connector hands the body over
untouched and keeps zero provider words in core (AD-2).

The vocabulary itself is not invented either. IAS runs a Cloud Foundry UAA at
``/uaa/oauth/token``, so its error set IS the published OAuth one -- RFC 6749
section 5.2 for the token endpoint, RFC 6750 section 3.1 for the Bearer
resource. Every key below is one of those finite values.

What changes for a person: ``400 invalid_grant`` and ``401 unauthorized``
("Bad credentials" from the password grant) stop reading as "malformed request"
and "credentials expired" and start reading as ``auth_revoked`` -- the
credential IAS issued no longer exists, so the repair is a new one, not a retry
and not a wait.

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

_MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "ias"
_CONNECTOR_PATH = _MODULE_DIR / "connector.py"


@pytest.fixture(scope="module")
def connector():
    spec = importlib.util.spec_from_file_location("connector_ias_errors", _CONNECTOR_PATH)
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
        request=httpx.Request("GET", "https://example.com/reportingservice/api/teams"),
    )


def _discover(connector, monkeypatch, response: httpx.Response):
    """Drive the real raise site of discover_accounts with a canned response."""
    from core import nango_client

    monkeypatch.setattr(
        nango_client, "get_fresh_token", lambda *a, **k: "token-not-used", raising=False
    )
    monkeypatch.setattr(connector.httpx, "get", lambda *a, **k: response)
    return connector.discover_accounts("conn_EXAMPLE")


#: What a UAA-protected resource answers for a token it no longer accepts.
_INVALID_TOKEN = {
    "error": "invalid_token",
    "error_description": "Access token expired: eyJhbGciOi...",
}

#: What the UAA password grant answers when the resource-owner credential is
#: gone (rotated, disabled, or de-provisioned).
_BAD_CREDENTIALS = {"error": "unauthorized", "error_description": "Bad credentials"}


def test_a_401_invalid_token_is_auth_expired_and_keeps_the_payload(
    connector, monkeypatch
):
    """The documented 401, driven through the real raise site."""
    with pytest.raises(Exception) as exc_info:
        _discover(connector, monkeypatch, _response(401, _INVALID_TOKEN))

    err = exc_info.value
    assert err.error_class == "auth_expired"
    assert err.retryable is False
    assert err.user_action == "reconnect"
    assert err.provider_status == 401

    # Evidence intact, and intact means IDENTICAL. The connector briefly copied
    # the OAuth `error` string into a `reason` slot so core could key on it; core
    # reads the RFC 6749 shape itself since 2026-08-17, so the body carried as
    # evidence is the body the token endpoint sent, key for key.
    assert err.provider_payload == _INVALID_TOKEN


def test_a_401_bad_credentials_is_auth_revoked_not_auth_expired(connector, monkeypatch):
    """Two different repairs, and pure HTTP cannot tell them apart.

    ``auth_expired`` says a refresh will fix it. ``auth_revoked`` says nothing
    will until somebody issues a new credential. Both arrive as a bare 401.
    """
    from core.pull_errors import classify_http_error

    with pytest.raises(Exception) as exc_info:
        _discover(connector, monkeypatch, _response(401, _BAD_CREDENTIALS))

    assert exc_info.value.error_class == "auth_revoked"
    # What core says without the map, on the same body.
    assert classify_http_error(401, _BAD_CREDENTIALS, None).error_class == "auth_expired"


def test_a_400_invalid_grant_is_auth_revoked_not_a_malformed_request(
    connector, error_map
):
    """The strongest refinement here: a 400 that is not the caller's fault.

    RFC 6749 5.2 puts ``invalid_grant`` on a 400, and core reads any 400 as
    ``invalid_request`` -- "the integration is broken, do not retry, tell
    nobody". It is in fact a revoked grant: the operator has to reconnect, and
    the map is the only thing that can say so.
    """
    from core.pull_errors import classify_http_error

    body = {"error": "invalid_grant", "error_description": "Bad credentials"}

    # The UNTOUCHED body: `{"error": "<string>"}` is RFC 6749's own shape and one
    # core consults directly, so no module-side normalisation stands between the
    # response and the verdict.
    assert classify_http_error(400, body, error_map).error_class == "auth_revoked"
    assert classify_http_error(400, body, None).error_class == "invalid_request"


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
        "400:invalid_grant": "auth_revoked",
        "400:invalid_scope": "permission_denied",
        "400:unauthorized_client": "permission_denied",
        "401:invalid_client": "auth_revoked",
        "401:unauthorized": "auth_revoked",
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
    source = _CONNECTOR_PATH.read_text(encoding="utf-8")
    assert source.count("_load_error_map()") >= 2  # both raise sites


def test_the_note_names_where_the_codes_come_from():
    """A code with no cited reference is indistinguishable from an invented one."""
    manifest = json.loads((_MODULE_DIR / "manifest.json").read_text(encoding="utf-8"))
    note = manifest.get("_error_map_note")
    assert note, "a declared map must carry its reference"
    assert note.isascii()  # AI-03
