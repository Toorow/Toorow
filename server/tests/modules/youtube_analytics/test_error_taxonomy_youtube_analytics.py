"""YouTube Analytics error taxonomy -- the declared-empty map, now filled.

AI-114 emptied this map for a good reason: its two keys ("404", "429") could
never be read. But "explicitly empty" was only ever half a verdict -- it said
"core can key on nothing here", and that was true of the CLASSIFIER, which
extracted ``error.code`` alone. YouTube publishes a reason registry
(https://developers.google.com/youtube/analytics/errors) in
``error.errors[].reason``, and ``_extract_provider_codes`` now offers it first.

The two holes AI-114 named STAY holes, and the tests below pin them: quota-reason
403s never reach the classifier (they go to the breaker) and 404 is a status-only
judgment in ``_STATUS_OVERRIDES``. Declaring keys for either would put back
exactly the dead entries AI-114 removed.

The map is loaded from the REAL manifest, never retyped. No network, no DB.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")

_MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "youtube-analytics"
_CONNECTOR_PATH = _MODULE_DIR / "connector.py"
_MANIFEST_PATH = _MODULE_DIR / "manifest.json"

_KEY = re.compile(r"^(\d{3}):(.+)$")


@pytest.fixture(scope="module")
def connector():
    spec = importlib.util.spec_from_file_location(
        "connector_youtube_taxonomy", _CONNECTOR_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _manifest() -> dict:
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


def _error_map() -> dict:
    return _manifest()["error_map"]


def _response(status: int, body: dict) -> httpx.Response:
    return httpx.Response(
        status,
        json=body,
        request=httpx.Request("GET", "https://youtubeanalytics.googleapis.com/v2/reports"),
    )


def _body(status: int, reason: str, message: str) -> dict:
    return {
        "error": {
            "code": status,
            "message": message,
            "errors": [
                {"message": message, "domain": "youtube.analytics", "reason": reason}
            ],
        }
    }


_AUTHORIZATION_EXPIRED_BODY = _body(
    401, "authorizationExpired", "The OAuth 2.0 access token is no longer valid."
)


def test_a_401_is_auth_expired_and_the_provider_body_survives(connector):
    with pytest.raises(Exception) as exc_info:
        connector._raise_for_status(_response(401, _AUTHORIZATION_EXPIRED_BODY))

    err = exc_info.value
    assert err.error_class == "auth_expired"
    assert err.retryable is False
    assert err.user_action == "reconnect"
    assert err.provider_status == 401
    assert err.provider_payload == _AUTHORIZATION_EXPIRED_BODY


def test_a_closed_youtube_account_is_revoked_access_not_a_missing_permission(connector):
    """The refinement, and the only one in this family core cannot reach alone.

    Pure HTTP reads every 403 as ``permission_denied`` -- "you are not allowed
    here", whose repair is to grant this credential more. YouTube documents
    ``authenticatedUserAccountClosed`` / ``authenticatedUserAccountSuspended`` as
    the account BEHIND the grant being gone: no scope, no re-consent and no
    retry on that account will ever work, and the honest instruction is to
    connect a different one. ``auth_revoked`` is the class that says so, and it
    is the one class the pure-HTTP table never produces.
    """
    from core.pull_errors import classify_http_error

    for reason in ("authenticatedUserAccountClosed", "authenticatedUserAccountSuspended"):
        body = _body(403, reason, "The YouTube account is closed or suspended.")
        with pytest.raises(Exception) as exc_info:
            connector._raise_for_status(_response(403, body))

        err = exc_info.value
        assert err.error_class == "auth_revoked", reason
        assert err.retryable is False
        assert err.user_action == "reconnect"
        assert err.provider_status == 403
        assert err.provider_payload == body
        assert err.error_class != classify_http_error(403, None, None).error_class


def test_a_missing_scope_is_still_a_plain_permission_refusal(connector):
    """``insufficientPermissions`` -- e.g. a monetary metric without the
    yt-analytics-monetary scope -- is the case where granting more DOES fix it."""
    body = _body(403, "insufficientPermissions", "The request is not properly authorized.")
    with pytest.raises(Exception) as exc_info:
        connector._raise_for_status(_response(403, body))
    assert exc_info.value.error_class == "permission_denied"


def test_the_quota_403s_still_bypass_the_map_entirely(connector):
    """AI-114's first hole, kept: those reasons go to the breaker, not the map.

    ``_raise_for_status`` routes a quota-reason 403 to ``core.quota.RateLimitError``
    BEFORE classify_http_error is reached. A "403:quotaExceeded" key would look
    like coverage and cover nothing -- the exact shape of the entries AI-114 cut.
    """
    from core.quota import RateLimitError

    error_map = _error_map()
    for reason in sorted(connector._QUOTA_REASONS):
        assert f"403:{reason}" not in error_map, reason
        resp = httpx.Response(
            403,
            json=_body(403, reason, "Quota exceeded."),
            request=httpx.Request("GET", "https://youtubeanalytics.googleapis.com/v2/reports"),
        )
        with pytest.raises(RateLimitError):
            connector._raise_for_status(resp)


def test_the_404_judgment_stays_where_a_status_only_rule_can_live(connector):
    """AI-114's second hole, kept: the grammar cannot express a status-only rule."""
    assert set(connector._STATUS_OVERRIDES) == {404}
    assert not [k for k in _error_map() if k.startswith("404:")]

    body = {"error": {"code": 404, "message": "Requested entity was not found."}}
    with pytest.raises(Exception) as exc_info:
        connector._raise_for_status(_response(404, body))
    assert exc_info.value.error_class == "permission_denied"


def test_the_connector_reads_the_manifest_map_and_caches_it(connector):
    """A map the connector never loads classifies nothing (playbook step 4)."""
    assert connector._load_error_map() == _error_map()


def test_the_map_has_the_only_shape_its_reader_can_look_up():
    """Keys ``<status>:<provider_code>``, values inside the taxonomy, no 429."""
    from core.pull_errors import ERROR_CLASSES, classify_http_error

    error_map = _error_map()
    assert error_map, "youtube-analytics must declare a filled error_map"

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
