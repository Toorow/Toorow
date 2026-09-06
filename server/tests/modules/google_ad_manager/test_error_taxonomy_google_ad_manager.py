"""Ad Manager error taxonomy -- the TODO note, resolved into a live map.

``manifest._error_map_note`` read "TODO(catalogue port): ... Decide during port
whether a provider code map refines anything beyond pure-HTTP classification",
and pointed at the SOAP helpers of the gam-native reference. Two things settled
it. First, this module is REST-only (``_GAM_REST_BASE`` = the Ad Manager API v1),
so the SOAP ``ApiException`` reason enums never arrive here and are not what a key
can be written on. Second, the REST envelope does carry discriminating tokens --
``error.status`` and, on the shared Google surfaces, ``error.errors[].reason`` --
and ``core.pull_errors._extract_provider_codes`` now offers both before
``error.code``, which merely repeats the HTTP status.

The map is read from the REAL manifest and exercised through ``_gam_request``,
the single door every GAM call goes through. No network.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
from pathlib import Path

import httpx
import pytest
import respx

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")

_MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "google-ad-manager"
_CONNECTOR_PATH = _MODULE_DIR / "connector.py"
_MANIFEST_PATH = _MODULE_DIR / "manifest.json"

_NETWORKS_URL = "https://admanager.googleapis.com/v1/networks"

_KEY = re.compile(r"^(\d{3}):(.+)$")


@pytest.fixture(scope="module")
def connector():
    spec = importlib.util.spec_from_file_location(
        "connector_gam_taxonomy", _CONNECTOR_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _manifest() -> dict:
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


def _error_map() -> dict:
    return _manifest()["error_map"]


#: What the Ad Manager REST API answers when the bearer token is no longer valid.
_UNAUTHENTICATED_BODY = {
    "error": {
        "code": 401,
        "message": (
            "Request had invalid authentication credentials. Expected OAuth 2 "
            "access token, login cookie or other valid authentication credential."
        ),
        "status": "UNAUTHENTICATED",
    }
}

#: The quota refusal. It is an HTTP 403 and only the reason says "come back later".
_QUOTA_BODY = {
    "error": {
        "code": 403,
        "message": "Quota exceeded for quota metric 'Requests'.",
        "status": "PERMISSION_DENIED",
        "errors": [
            {
                "message": "Quota exceeded for quota metric 'Requests'.",
                "domain": "usageLimits",
                "reason": "rateLimitExceeded",
            }
        ],
    }
}


@respx.mock
def test_a_401_is_auth_expired_and_the_provider_body_survives(connector):
    respx.get(_NETWORKS_URL).mock(
        return_value=httpx.Response(401, json=_UNAUTHENTICATED_BODY)
    )

    with pytest.raises(Exception) as exc_info:
        connector._gam_request("GET", _NETWORKS_URL, "fake-token")

    err = exc_info.value
    assert err.error_class == "auth_expired"
    assert err.retryable is False
    assert err.user_action == "reconnect"
    assert err.provider_status == 401
    assert err.provider_payload == _UNAUTHENTICATED_BODY


@respx.mock
def test_the_map_turns_a_quota_403_into_something_worth_retrying(connector):
    """The refinement, against what core says WITHOUT the map.

    Pure HTTP calls every 403 ``permission_denied`` -- retryable=False. A GAM
    report submission refused for quota is exactly the call that WOULD succeed
    on the next window, and abandoning it loses the whole pull.
    """
    from core.pull_errors import classify_http_error

    respx.get(_NETWORKS_URL).mock(return_value=httpx.Response(403, json=_QUOTA_BODY))

    with pytest.raises(Exception) as exc_info:
        connector._gam_request("GET", _NETWORKS_URL, "fake-token")

    err = exc_info.value
    assert err.error_class == "provider_transient"
    assert err.retryable is True
    assert err.provider_status == 403
    assert err.provider_payload == _QUOTA_BODY
    assert err.error_class != classify_http_error(403, None, None).error_class


@respx.mock
def test_a_404_stops_being_replayed(connector):
    """A network code or report id that does not resolve is a selection drift.

    Core leaves 404 ``unclassified`` on purpose -- half the connectors read it as
    "no access", half as "no such thing", and it refuses to impose either. GAM
    answers 403 for the access case, so a 404 here really is "no such resource":
    ``invalid_request``, not retryable, instead of a replay to the dead letter.
    """
    from core.pull_errors import classify_http_error

    body = {
        "error": {
            "code": 404,
            "message": "Requested entity was not found.",
            "status": "NOT_FOUND",
        }
    }
    respx.get(_NETWORKS_URL).mock(return_value=httpx.Response(404, json=body))

    with pytest.raises(Exception) as exc_info:
        connector._gam_request("GET", _NETWORKS_URL, "fake-token")

    err = exc_info.value
    assert err.error_class == "invalid_request"
    assert err.retryable is False
    assert classify_http_error(404, None, None).error_class == "unclassified"


def test_the_connector_reads_the_manifest_map_and_caches_it(connector):
    """A map the connector never loads classifies nothing (playbook step 4)."""
    assert connector._load_error_map() == _error_map()
    assert connector._load_error_map() is connector._load_error_map()


def test_the_map_has_the_only_shape_its_reader_can_look_up():
    """Keys ``<status>:<provider_code>``, values inside the taxonomy, no 429."""
    from core.pull_errors import ERROR_CLASSES, classify_http_error

    error_map = _error_map()
    assert error_map, "google-ad-manager must declare a filled error_map"

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
    assert "TODO" not in note, "the port decision is made; the TODO must be gone"
