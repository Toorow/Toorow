"""Search Console error taxonomy -- the empty map, filled and wired.

``manifest.error_map`` was ``{}`` with a note that said Search Console "uses
standard HTTP status codes only". That was a statement about the CLASSIFIER, not
about GSC: core only extracted ``error.code``, which every Google API sets to the
numeric HTTP status, so the only writable key was ``"401:401"`` -- redundant by
construction. GSC does publish discriminating codes; they live in
``error.errors[].reason``, and ``_extract_provider_codes`` now offers that first.

The map is loaded from the REAL manifest here, never retyped, and exercised
through ``discover_accounts`` -- the raise site a person actually meets first,
before any datastream exists. No network, no DB.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")

_MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "gsc"
_CONNECTOR_PATH = _MODULE_DIR / "connector.py"
_MANIFEST_PATH = _MODULE_DIR / "manifest.json"

_SITES_URL = "https://www.googleapis.com/webmasters/v3/sites"

_KEY = re.compile(r"^(\d{3}):(.+)$")


@pytest.fixture(scope="module")
def connector():
    spec = importlib.util.spec_from_file_location(
        "connector_gsc_taxonomy", _CONNECTOR_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _manifest() -> dict:
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


def _error_map() -> dict:
    return _manifest()["error_map"]


#: The Google API Console envelope Search Console returns on an invalid token.
_AUTH_ERROR_BODY = {
    "error": {
        "code": 401,
        "message": "Invalid Credentials",
        "errors": [
            {
                "message": "Invalid Credentials",
                "domain": "global",
                "reason": "authError",
                "location": "Authorization",
                "locationType": "header",
            }
        ],
    }
}

#: The same envelope when the per-user rate limit is reached -- HTTP 403, and the
#: reason is the only thing that says "wait", not "you are not allowed".
_RATE_LIMIT_BODY = {
    "error": {
        "code": 403,
        "message": "Rate Limit Exceeded",
        "errors": [
            {
                "message": "Rate Limit Exceeded",
                "domain": "usageLimits",
                "reason": "userRateLimitExceeded",
            }
        ],
    }
}


@respx.mock
def test_a_401_is_auth_expired_and_the_provider_body_survives(connector):
    respx.get(_SITES_URL).mock(return_value=httpx.Response(401, json=_AUTH_ERROR_BODY))

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(Exception) as exc_info:
            connector.discover_accounts("conn_test")

    err = exc_info.value
    assert err.error_class == "auth_expired"
    assert err.retryable is False
    assert err.user_action == "reconnect"
    assert err.provider_status == 401
    assert err.provider_payload == _AUTH_ERROR_BODY


@respx.mock
def test_the_map_turns_a_rate_limited_403_into_something_worth_retrying(connector):
    """The refinement, measured against what core says WITHOUT the map.

    Pure HTTP reads any 403 as ``permission_denied``: retryable=False,
    user_action "reconnect". Google documents ``userRateLimitExceeded`` as "retry
    using exponential backoff", so the unrefined verdict abandoned a call that
    would have succeeded and pointed the operator at a credential that was fine.
    """
    from core.pull_errors import classify_http_error

    respx.get(_SITES_URL).mock(return_value=httpx.Response(403, json=_RATE_LIMIT_BODY))

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(Exception) as exc_info:
            connector.discover_accounts("conn_test")

    err = exc_info.value
    assert err.error_class == "provider_transient"
    assert err.retryable is True
    assert err.provider_status == 403
    assert err.provider_payload == _RATE_LIMIT_BODY
    assert err.error_class != classify_http_error(403, None, None).error_class


@respx.mock
def test_a_403_that_is_really_a_refusal_stays_permission_denied(connector):
    """The map refines; it does not blanket-rewrite. ``forbidden`` on a site the
    token does not own is still "ask for access", not "wait and retry"."""
    body = {
        "error": {
            "code": 403,
            "message": "User does not have sufficient permission for site.",
            "errors": [
                {"message": "Forbidden", "domain": "global", "reason": "forbidden"}
            ],
        }
    }
    respx.get(_SITES_URL).mock(return_value=httpx.Response(403, json=body))

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(Exception) as exc_info:
            connector.discover_accounts("conn_test")
    assert exc_info.value.error_class == "permission_denied"
    assert exc_info.value.retryable is False


def test_the_connector_reads_the_manifest_map_and_caches_it(connector):
    """A map the connector never loads classifies nothing (playbook step 4)."""
    assert connector._load_error_map() == _error_map()
    assert connector._load_error_map() is connector._load_error_map()


def test_the_map_has_the_only_shape_its_reader_can_look_up():
    """Keys ``<status>:<provider_code>``, values inside the taxonomy, no 429."""
    from core.pull_errors import ERROR_CLASSES, classify_http_error

    error_map = _error_map()
    assert error_map, "gsc must declare a filled error_map"

    refining = []
    for key, value in error_map.items():
        match = _KEY.match(key)
        assert match, f"{key!r} is not '<status>:<provider_code>'"
        status = int(match.group(1))
        assert status != 429, f"{key!r}: a 429 never reaches classify_http_error"
        assert value in ERROR_CLASSES, f"{key!r} -> {value!r} is outside the taxonomy"
        if value != classify_http_error(status, None, None).error_class:
            refining.append(key)

    assert refining, "a map that restates pure HTTP everywhere proves nothing"

    note = _manifest().get("_error_map_note")
    assert isinstance(note, str) and note.strip()
    assert note.isascii()  # AI-03
