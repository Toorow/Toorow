"""Sheets error taxonomy -- the map that was "intentionally omitted", now live.

``manifest._error_map_note`` said Sheets "is pure HTTP (no provider-specific
error codes or subcodes)" and that "a formal error_map dict is intentionally
omitted". Only the second half was a decision; the first was a description of the
CLASSIFIER as it stood -- core extracted ``error.code``, which Google sets to the
numeric HTTP status, so no key could ever discriminate. Sheets returns the shared
Google Workspace envelope, whose ``error.errors[].reason`` is a published registry,
and ``_extract_provider_codes`` now offers it first.

WHAT THIS MODULE KEEPS, ON PURPOSE. ``_fetch_sheet_values`` still raises Python
``PermissionError`` on a 403 and ``FileNotFoundError`` on a 404, because
``core/google_sheets_sync.py`` classifies the adapter exception BY TYPE
(PermissionError -> revoked_consent, FileNotFoundError -> spreadsheet_not_found).
The map is consulted first, and only a 403 it makes RETRYABLE -- a spent quota --
leaves as the typed connector error instead. That single divergence is the point
of the repair: an exhausted read quota was being reported to the operator as
"consent revoked".

No network, no DB: respx answers the Sheets host.
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

_MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "google-sheets"
_CONNECTOR_PATH = _MODULE_DIR / "connector.py"
_MANIFEST_PATH = _MODULE_DIR / "manifest.json"

_SPREADSHEET_ID = "SHEET_EXAMPLE"
_RANGE = "Budget"

_KEY = re.compile(r"^(\d{3}):(.+)$")


@pytest.fixture(scope="module")
def connector():
    spec = importlib.util.spec_from_file_location(
        "connector_sheets_taxonomy", _CONNECTOR_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _manifest() -> dict:
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


def _error_map() -> dict:
    return _manifest()["error_map"]


def _values_url(connector) -> str:
    """The URL the connector itself builds -- SHEETS_API_BASE is env-overridable,
    so retyping the host here would make the mock miss on a configured host."""
    return (
        f"{connector.SHEETS_API_BASE}/spreadsheets/{_SPREADSHEET_ID}/values/{_RANGE}"
    )


def _fetch(connector):
    return connector._fetch_sheet_values("fake-token", _SPREADSHEET_ID, _RANGE)


#: The Workspace envelope Sheets returns on a dead credential.
_AUTH_ERROR_BODY = {
    "error": {
        "code": 401,
        "message": "Request had invalid authentication credentials.",
        "status": "UNAUTHENTICATED",
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

#: The same envelope when the per-user read quota is spent -- HTTP 403.
_RATE_LIMIT_BODY = {
    "error": {
        "code": 403,
        "message": "User Rate Limit Exceeded",
        "errors": [
            {
                "message": "User Rate Limit Exceeded",
                "domain": "usageLimits",
                "reason": "userRateLimitExceeded",
            }
        ],
    }
}


@respx.mock
def test_a_401_is_auth_expired_and_the_provider_body_survives(connector):
    """Before this repair a 401 left as ``httpx.HTTPStatusError`` -- a status the
    worker had to re-interpret, carrying no class and no retry policy."""
    respx.get(_values_url(connector)).mock(
        return_value=httpx.Response(401, json=_AUTH_ERROR_BODY)
    )

    with pytest.raises(Exception) as exc_info:
        _fetch(connector)

    err = exc_info.value
    assert err.error_class == "auth_expired"
    assert err.retryable is False
    assert err.user_action == "reconnect"
    assert err.provider_status == 401
    assert err.provider_payload == _AUTH_ERROR_BODY


@respx.mock
def test_a_spent_quota_stops_being_reported_as_a_withdrawn_consent(connector):
    """The refinement, and the reason this map stopped being empty.

    Pure HTTP calls a 403 ``permission_denied`` (retryable=False), and this module
    turned it into ``PermissionError``, which ``core/google_sheets_sync.py`` reads
    as ``revoked_consent``. So a read quota that only needed backing off told the
    operator to re-authorise a connection that was never broken.
    """
    from core.pull_errors import classify_http_error

    respx.get(_values_url(connector)).mock(
        return_value=httpx.Response(403, json=_RATE_LIMIT_BODY)
    )

    with pytest.raises(Exception) as exc_info:
        _fetch(connector)

    err = exc_info.value
    assert not isinstance(err, PermissionError)
    assert err.error_class == "provider_transient"
    assert err.retryable is True
    assert err.provider_status == 403
    assert err.provider_payload == _RATE_LIMIT_BODY
    assert err.error_class != classify_http_error(403, None, None).error_class


@respx.mock
def test_a_real_403_keeps_the_permission_error_the_sync_classifies_on(connector):
    """The contract older than the taxonomy, and deliberately not broken.

    ``core/google_sheets_sync._classify_adapter_error`` matches the exception TYPE:
    ``PermissionError`` -> revoked_consent. Raising ``PermissionDeniedError``
    (a RuntimeError) instead would have silently downgraded a revoked consent to
    a range drift, so every non-retryable 403 still leaves as PermissionError.
    """
    body = {
        "error": {
            "code": 403,
            "message": "The caller does not have permission",
            "status": "PERMISSION_DENIED",
        }
    }
    respx.get(_values_url(connector)).mock(
        return_value=httpx.Response(403, json=body)
    )

    with pytest.raises(PermissionError):
        _fetch(connector)


@respx.mock
def test_a_404_keeps_the_file_not_found_error(connector):
    """Same reason: the sync reads FileNotFoundError as spreadsheet_not_found.

    This is why the map declares NO 404 key -- the classifier is never consulted
    there, so a key would be a decoration nobody reads.
    """
    respx.get(_values_url(connector)).mock(
        return_value=httpx.Response(404, json={"error": {"code": 404}})
    )

    with pytest.raises(FileNotFoundError):
        _fetch(connector)

    assert not [k for k in _error_map() if k.startswith("404:")]


def test_the_connector_reads_the_manifest_map_and_caches_it(connector):
    """A map the connector never loads classifies nothing (playbook step 4)."""
    assert connector._load_error_map() == _error_map()
    assert connector._load_error_map() is connector._load_error_map()


def test_the_map_has_the_only_shape_its_reader_can_look_up():
    """Keys ``<status>:<provider_code>``, values inside the taxonomy, no 429."""
    from core.pull_errors import ERROR_CLASSES, classify_http_error

    error_map = _error_map()
    assert error_map, "google-sheets must declare a filled error_map"

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
