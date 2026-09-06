"""GitHub error taxonomy -- story 67-21 (2026-08-17).

WHY THIS FILE EXISTS. ``manifest.error_map`` was empty, and its note said the
reason was structural: "classify_http_error extracts provider codes from
'error.code', 'error_code', or 'code' fields only, so no error_map keys will
ever fire for GitHub (the body carries no extractable code)". The first half was
true; the conclusion was not. GitHub's error vocabulary is not missing -- it is
the body's ``message`` string, and the repair is to put that string where core
looks (``connector._github_error_payload``), exactly as the stripe and square
connectors do for their own body shapes. Core stays free of provider words
(AD-2); the codes live in the manifest and nowhere else.

Two verdicts change as a result, and both matter to a person:

  * ``401 "Bad credentials"`` becomes ``auth_revoked``. The connector presents a
    user-supplied PAT verbatim -- there is no refresh flow -- so a rejected
    credential is one GitHub no longer holds, and the repair is a NEW token.
  * ``404 "Not Found"`` becomes ``permission_denied``. GitHub deliberately
    answers 404 rather than 403 for a private repository the token cannot see,
    so that a probe cannot learn the repository exists. Left to pure HTTP, that
    404 is ``unclassified`` -- retryable, forever, and mute about the missing
    access.

Offline: no network, no DB, no token. ``httpx.get`` is replaced for the length
of a test and the real raise site is driven.
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

_MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "github"
_CONNECTOR_PATH = _MODULE_DIR / "connector.py"


@pytest.fixture(scope="module")
def connector():
    spec = importlib.util.spec_from_file_location(
        "connector_github_errors", _CONNECTOR_PATH
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
        request=httpx.Request("GET", "https://example.com/repos/o/r/releases"),
    )


def _pull_releases(connector, monkeypatch, response: httpx.Response):
    monkeypatch.setattr(connector.httpx, "get", lambda *a, **k: response)
    return connector._pull_releases(
        owner="example-org",
        repo="example-repo",
        headers={},
        date_from="2026-08-01",
        date_to="2026-08-17",
        project_id="proj_EXAMPLE",
        pull_id="pull_EXAMPLE",
    )


#: The body GitHub returns for a token it no longer holds (deleted, rotated, or
#: revoked by secret scanning).
_BAD_CREDENTIALS = {
    "message": "Bad credentials",
    "documentation_url": "https://docs.github.com/rest",
    "status": "401",
}

#: The body GitHub returns for a private repository the token cannot see.
_NOT_FOUND = {
    "message": "Not Found",
    "documentation_url": "https://docs.github.com/rest",
    "status": "404",
}


def test_a_401_bad_credentials_is_auth_revoked_and_keeps_the_payload(
    connector, monkeypatch
):
    """The real raise site, the real body, the class pure HTTP never reaches."""
    with pytest.raises(Exception) as exc_info:
        _pull_releases(connector, monkeypatch, _response(401, _BAD_CREDENTIALS))

    err = exc_info.value
    assert err.error_class == "auth_revoked"
    assert err.retryable is False
    assert err.user_action == "reconnect"
    assert err.provider_status == 401

    # Evidence intact, and intact means IDENTICAL. The connector briefly copied
    # the message into a `reason` slot so core could key on it; core consults
    # `message` itself since 2026-08-17, so the body carried as evidence is the
    # body the API sent, key for key.
    assert err.provider_payload == _BAD_CREDENTIALS


def test_the_status_echo_is_declared_nowhere_so_the_message_key_is_the_one_that_fires(
    connector, error_map
):
    """The body carries ``"status": "401"`` beside the message, and that is fine.

    This API publishes no numeric error code: the whole vocabulary is the
    ``message`` string. The body also echoes the HTTP status, and core consults
    every candidate it can extract, echo included -- so the echo is tried FIRST.
    It misses, because a ``"401:401"`` key restates what pure HTTP already gave
    and is deliberately not declared here; the message key is then reached.

    Core does not promise that a message outranks a status: another shipped
    module (linkedin-ads) keys on the echo ON PURPOSE, so the precedence cannot be
    legislated in core. What makes this module correct is the MAP, and that is
    what this test pins -- on the real manifest, not on a probe.
    """
    from core.pull_errors import _extract_provider_codes, classify_http_error

    candidates = _extract_provider_codes(_BAD_CREDENTIALS)
    assert "401" in candidates, "the status echo is extractable"
    assert "Bad credentials" in candidates, "so is the message"
    assert candidates.index("401") < candidates.index("Bad credentials"), (
        "free text is consulted last, after every structured candidate"
    )

    assert "401:401" not in error_map, (
        "declaring the echo would shadow the message key and refine nothing"
    )
    assert classify_http_error(401, _BAD_CREDENTIALS, error_map).error_class == (
        "auth_revoked"
    )


def test_a_404_on_a_private_repository_is_permission_denied(
    connector, monkeypatch, error_map
):
    """GitHub hides a private repo behind a 404; core alone reads it as unknown."""
    from core.pull_errors import classify_http_error

    with pytest.raises(Exception) as exc_info:
        _pull_releases(connector, monkeypatch, _response(404, _NOT_FOUND))

    err = exc_info.value
    assert err.error_class == "permission_denied"
    assert err.retryable is False
    assert err.provider_status == 404

    # What core says without the map: retryable, and silent about the access.
    assert classify_http_error(404, _NOT_FOUND, None).error_class == "unclassified"
    assert error_map["404:Not Found"] == "permission_denied"


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
        "401:Bad credentials": "auth_revoked",
        "404:Not Found": "permission_denied",
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


def test_no_403_key_is_declared_because_a_403_never_reaches_core(
    connector, monkeypatch, error_map
):
    """A key that cannot fire is worse than no key -- it looks like coverage.

    ``_pull_releases`` / ``_pull_deployments`` still raise a raw PermissionError
    on 403 before ``classify_http_error`` is reached (GitHub answers 403, not
    429, on the primary rate limit, and splitting the two needs header
    inspection). Until that refactor lands a "403:..." entry here would be dead
    -- the exact defect AI-114 removed elsewhere. This test pins both halves: no
    403 key, and the reason there is none.
    """
    assert not [key for key in error_map if key.startswith("403:")]

    forbidden = {"message": "Resource not accessible by personal access token"}
    with pytest.raises(PermissionError):
        _pull_releases(connector, monkeypatch, _response(403, forbidden))


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
