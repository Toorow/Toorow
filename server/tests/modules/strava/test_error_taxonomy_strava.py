"""Strava error taxonomy -- AI-114 (2026-08-01).

strava had NO module tests at all, which is part of why its error seam could
carry a declaration that never ran: ``manifest.error_map`` held one key, "404"
-> permission_denied, and the map's only reader
(``core.pull_errors.classify_http_error``) looks up "<status>:<provider_code>"
and nothing else. A bare "404" was never found, so the verdict a human wrote
down never applied: a 404 came out ``unclassified``, which is RETRYABLE.

The 404 judgment now lives in ``connector._STATUS_OVERRIDES``, where it applies.

Story 67-21 (2026-08-17) then filled the map: Strava's Fault ``errors[].code``
IS documented for the case that matters (a de-authorised token answers 401 with
``code: "invalid"``), and what had kept the map dead was the payload shape, not
the vocabulary -- core never opens a top-level ``errors`` array, so
core reads the Fault's ``errors[].code`` itself, so the body is passed untouched.

Offline: no network, no DB, no token. The seam is exercised directly.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")

_MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "strava"
_CONNECTOR_PATH = _MODULE_DIR / "connector.py"


@pytest.fixture(scope="module")
def connector():
    spec = importlib.util.spec_from_file_location(
        "connector_strava_errors", _CONNECTOR_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _response(status: int, body: dict, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        json=body,
        headers=headers or {},
        request=httpx.Request("GET", "https://example.com/api/v3/clubs/1"),
    )


_FAULT_404 = {
    "message": "Resource Not Found",
    "errors": [{"resource": "Club", "field": "id", "code": "not found"}],
}


def test_404_is_permission_denied_and_is_not_retried(connector):
    """The judgment the dead key expressed, now actually applied.

    A 404 on a club the athlete is supposed to belong to is an access problem:
    the person must be re-granted, not made to wait. The difference is not
    cosmetic -- ``unclassified`` is retryable, ``permission_denied`` is not.

    ORDER MATTERS, and it is unchanged: the competitor_snapshot 404 is an
    EXPECTED non-fatal "unreachable" outcome (a private or absent public club)
    and is handled by the CALLER before this function is ever reached, so this
    override only ever sees the own-club case.
    """
    from core.pull_errors import classify_http_error

    with pytest.raises(Exception) as exc_info:
        connector._raise_for_status(_response(404, _FAULT_404), context="own_club_full")

    err = exc_info.value
    assert err.error_class == "permission_denied"
    assert err.retryable is False
    assert err.provider_status == 404
    assert err.provider_payload == _FAULT_404

    # What core says without the module -- what the dead key produced.
    assert classify_http_error(404, _FAULT_404).error_class == "unclassified"


def test_403_exceeded_still_goes_to_the_breaker_not_the_taxonomy(connector):
    """The pre-existing shim is untouched by AI-114 and still wins.

    Strava historically answers 403 for a rate limit on some endpoints. That
    must reach ``core.quota.RateLimitError`` (breaker + Retry-After), never
    permission_denied -- otherwise a throttle reads as a revoked grant.
    """
    from core.quota import RateLimitError

    body = {
        "message": "Rate Limit Exceeded",
        "errors": [{"resource": "Application", "field": "rate limit",
                    "code": "exceeded"}],
    }
    with pytest.raises(RateLimitError) as exc_info:
        connector._raise_for_status(
            _response(403, body, {"Retry-After": "60"}), context="own_club_full"
        )
    assert exc_info.value.platform == "strava"
    assert exc_info.value.retry_after == 60


def test_403_without_exceeded_stays_permission_denied(connector):
    """The breaker shim must not swallow a real authorization failure."""
    body = {
        "message": "Authorization Error",
        "errors": [{"resource": "Club", "field": "access_token",
                    "code": "invalid"}],
    }
    with pytest.raises(Exception) as exc_info:
        connector._raise_for_status(_response(403, body), context="own_club_full")
    assert exc_info.value.error_class == "permission_denied"


def test_pure_http_statuses_are_left_to_core(connector):
    """_STATUS_OVERRIDES claims 404 and nothing else.

    A module-side table is only defensible while it stays the exception; if it
    grows to shadow statuses core already classifies, the taxonomy has drifted
    back into two implementations.
    """
    assert set(connector._STATUS_OVERRIDES) == {404}

    for status, expected in ((401, "auth_expired"), (400, "invalid_request"),
                             (500, "provider_transient")):
        with pytest.raises(Exception) as exc_info:
            connector._raise_for_status(_response(status, {"message": "x"}),
                                        context="own_club_full")
        assert exc_info.value.error_class == expected, status


# ---------------------------------------------------------------------------
# Story 67-21 (2026-08-17) -- the map stopped being empty.
#
# The 2026-08-01 note read Strava's Fault ``errors[].code`` as free-form. It is
# not: developers.strava.com/docs/authentication documents the response to a
# token whose grant is gone, verbatim, and the deauthorisation contract says
# EVERY access and refresh token for that athlete dies at once. That is
# auth_revoked -- "re-authorise", not "wait for a refresh" -- and pure HTTP can
# never produce it. What blocked the key was never the vocabulary: core does not
# descend into a top-level ``errors`` ARRAY, so no key could match until
# core reads the Fault's ``errors[].code`` directly from the untouched body.
# ---------------------------------------------------------------------------

#: The body Strava returns for a token the athlete de-authorised.
_FAULT_401_REVOKED = {
    "message": "Authorization Error",
    "errors": [{"resource": "Athlete", "field": "access_token", "code": "invalid"}],
}


def test_a_deauthorised_token_is_auth_revoked_not_auth_expired(connector):
    """The one class pure HTTP never reaches, on the body Strava documents.

    ``auth_expired`` and ``auth_revoked`` are not two names for one thing: the
    first says a refresh will fix it, the second says nothing will until the
    athlete re-authorises the application. Strava invalidates the refresh token
    too, so calling this "expired" tells the operator to wait for something that
    is never coming.
    """
    from core.pull_errors import classify_http_error

    with pytest.raises(Exception) as exc_info:
        connector._raise_for_status(
            _response(401, _FAULT_401_REVOKED), context="athlete/clubs"
        )

    err = exc_info.value
    assert err.error_class == "auth_revoked"
    assert err.retryable is False
    assert err.user_action == "reconnect"
    assert err.provider_status == 401

    # Evidence intact: normalisation WIDENS the payload, it never rewrites it.
    # Evidence intact, and intact means IDENTICAL. The connector briefly lifted
    # the Fault's `errors[0].code` to the top level so core could key on it; core
    # reads `errors[].code` itself since 2026-08-17, so the body carried as
    # evidence is the Fault Strava sent, key for key.
    assert err.provider_payload == _FAULT_401_REVOKED

    # The map fires on the UNTOUCHED Fault: core opens `errors[]` itself, so the
    # code no longer has to be lifted out of it by the module.
    assert (
        classify_http_error(
            401, _FAULT_401_REVOKED, connector._load_error_map()
        ).error_class
        == "auth_revoked"
    )
    # And it is the MAP that refines, not the shape: without it the same body is
    # only the pure-HTTP class, which would tell the operator to refresh a token
    # that no refresh can revive.
    assert classify_http_error(401, _FAULT_401_REVOKED, None).error_class == "auth_expired"


def test_the_map_refines_at_least_one_verdict(connector):
    """A map that restates pure HTTP proves nothing its absence would not.

    This is the conformance gate's own criterion
    (tests/conformance/test_pull_contract.py section 7), re-asserted here on the
    REAL manifest so the module fails first and locally.
    """
    from core.pull_errors import classify_http_error

    error_map = connector._load_error_map()
    assert error_map, "error_map is declared and must not be empty"

    refining = {
        key: value
        for key, value in error_map.items()
        if value != classify_http_error(int(key.split(":", 1)[0]), None, None).error_class
    }
    assert refining == {"401:invalid": "auth_revoked"}


def test_the_map_has_the_only_shape_core_can_read(connector):
    """Keys are "<status>:<code>", values are canonical, and 429 is never keyed."""
    from core.pull_errors import ERROR_CLASSES

    for key, value in connector._load_error_map().items():
        status, separator, code = key.partition(":")
        assert separator and status.isdigit() and len(status) == 3, key
        assert code, key
        assert status != "429", f"{key}: a 429 goes to the quota breaker, never here"
        assert value in ERROR_CLASSES, f"{key} -> {value}"


def test_the_map_is_read_and_the_note_is_kept(connector):
    """The map is DECLARED in the manifest and READ by the connector.

    A map nobody hands to core classifies nothing; a map with no note leaves the
    next reader guessing where its codes come from.
    """
    manifest = connector._load_manifest()
    assert manifest["error_map"] == connector._load_error_map()
    assert manifest.get("_error_map_note"), "a map must name the reference it comes from"
    assert manifest["_error_map_note"].isascii()  # AI-03
