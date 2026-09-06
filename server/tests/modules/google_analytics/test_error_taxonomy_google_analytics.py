"""GA4 error taxonomy -- the map that was declared absent, now declared and live.

Until 2026-08-17 this module carried no ``error_map`` at all, and its
``_error_map_note`` justified the absence with a claim that was true of the
CLASSIFIER, not of the provider: core only extracted ``error.code``, and Google
sets ``error.code`` to the numeric HTTP status, so the only key anyone could have
written was ``"403:403"`` -- a key that refines nothing. ``_extract_provider_codes``
now offers ``error.errors[].reason`` and the ``error.status`` enum BEFORE
``error.code``, so the tokens that actually discriminate are finally keyable, and
the map exists.

These tests use the REAL manifest map (loaded, never retyped) and the connector's
real raise site. No network, no DB: ``_ga4_runreport`` is the shared helper every
page pull goes through, and respx answers it.
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

_MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "google-analytics"
_CONNECTOR_PATH = _MODULE_DIR / "connector.py"
_MANIFEST_PATH = _MODULE_DIR / "manifest.json"

_RUN_REPORT_URL = (
    "https://analyticsdata.googleapis.com/v1beta/properties/TEST123:runReport"
)

_KEY = re.compile(r"^(\d{3}):(.+)$")


@pytest.fixture(scope="module")
def connector():
    spec = importlib.util.spec_from_file_location(
        "connector_ga4_taxonomy", _CONNECTOR_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _manifest() -> dict:
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


def _error_map() -> dict:
    return _manifest()["error_map"]


#: The body the GA4 Data API returns when the OAuth credential no longer works.
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

#: The body Google returns when the per-user read quota is spent. The reason is
#: what makes this different from a refusal, and it is the only place it is said.
_QUOTA_BODY = {
    "error": {
        "code": 403,
        "message": "Quota exceeded for quota metric 'Read requests'.",
        "status": "PERMISSION_DENIED",
        "errors": [
            {
                "message": "Quota exceeded for quota metric 'Read requests'.",
                "domain": "global",
                "reason": "rateLimitExceeded",
            }
        ],
    }
}


@respx.mock
def test_a_401_is_auth_expired_and_the_provider_body_survives(connector):
    """The documented raise path, end to end, with the evidence kept."""
    respx.post(_RUN_REPORT_URL).mock(
        return_value=httpx.Response(401, json=_UNAUTHENTICATED_BODY)
    )

    with pytest.raises(Exception) as exc_info:
        connector._ga4_runreport("TEST123", "fake-token", {"dateRanges": []})

    err = exc_info.value
    assert err.error_class == "auth_expired"
    assert err.retryable is False
    assert err.user_action == "reconnect"
    assert err.provider_status == 401
    assert err.provider_payload == _UNAUTHENTICATED_BODY


@respx.mock
def test_the_map_turns_a_quota_403_into_something_worth_retrying(connector):
    """The refinement, measured against what core says WITHOUT the map.

    A 403 is ``permission_denied`` on pure HTTP: retryable=False, user_action
    "reconnect". Google publishes ``rateLimitExceeded`` as "retry using
    exponential backoff" -- so the unrefined verdict abandoned a call that would
    have succeeded, and told the operator to re-authorise a credential that was
    never the problem.
    """
    from core.pull_errors import classify_http_error

    respx.post(_RUN_REPORT_URL).mock(return_value=httpx.Response(403, json=_QUOTA_BODY))

    with pytest.raises(Exception) as exc_info:
        connector._ga4_runreport("TEST123", "fake-token", {"dateRanges": []})

    err = exc_info.value
    assert err.error_class == "provider_transient"
    assert err.retryable is True
    assert err.provider_status == 403
    assert err.provider_payload == _QUOTA_BODY

    # What the same response produced before the map existed -- the measure of
    # what the map adds, not a restatement of it.
    assert classify_http_error(403, None, None).error_class == "permission_denied"
    assert err.error_class != classify_http_error(403, None, None).error_class


@respx.mock
def test_a_plain_403_still_means_permission_denied(connector):
    """The map REFINES; it does not replace. A refusal with no quota reason is
    still a refusal, and the pre-existing GA4 error tests depend on it."""
    body = {
        "error": {
            "code": 403,
            "message": "User does not have sufficient permissions for this property.",
            "status": "PERMISSION_DENIED",
        }
    }
    respx.post(_RUN_REPORT_URL).mock(return_value=httpx.Response(403, json=body))

    with pytest.raises(Exception) as exc_info:
        connector._ga4_runreport("TEST123", "fake-token", {"dateRanges": []})
    assert exc_info.value.error_class == "permission_denied"


def test_the_connector_reads_the_manifest_map_and_caches_it(connector):
    """A map the connector never loads classifies nothing (playbook step 4)."""
    assert connector._load_error_map() == _error_map()
    assert connector._load_error_map() is connector._load_error_map()


def test_the_map_has_the_only_shape_its_reader_can_look_up():
    """Keys ``<status>:<provider_code>``, values inside the taxonomy, no 429.

    ``classify_http_error`` looks up ``error_map[f"{status}:{code}"]`` and ignores
    an out-of-taxonomy value in silence; a 429 never reaches it at all (it raises
    ValueError -- 429 goes to core.quota.RateLimitError, the breaker path). So a
    malformed key is not a typo, it is an entry that will never fire.
    """
    from core.pull_errors import ERROR_CLASSES, classify_http_error

    error_map = _error_map()
    assert error_map, "google-analytics must declare a filled error_map"

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
