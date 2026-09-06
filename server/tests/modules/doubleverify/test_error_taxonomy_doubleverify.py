"""DoubleVerify error taxonomy -- story 67-21 (2026-08-17).

WHY THIS FILE EXISTS, AND WHY THE MAP IS STILL EMPTY. Repair 6 of
reviews/audit-2026-08-17/13-connecteurs.md asked for this manifest's
``error_map`` to be filled. It is not, and the reason was re-measured today
rather than inherited: DoubleVerify publishes no error reference outside the
developer portal, and the portal requires a login. The four public third-party
mirrors of the DV Data API contract (Improvado, Salesforce/Datorama, Alli,
Supermetrics) were re-checked on 2026-08-17 and NONE carries an error table, an
error body example, or a single application-level code -- they document the
token, the three-step report flow and the dimensions, and nothing else.

The one confirmed provider-specific signal remains a free-text sentence
("There is a problem with the selected combination of dimensions and/or
metrics", HTTP 400), and a sentence with no stable identifier is not a key.

Writing OAuth or Bearer standard codes here instead would be inventing DV's
vocabulary: DV runs no OAuth flow at all -- the credential is a static Access
Token Hash minted in Pinnacle (manifest._auth_note) -- so those keys would be a
guess dressed as a reference. An invented key is worse than an empty map: it
makes the taxonomy look refined where it is not (the AI-114 finding).

What IS pinned here: the map is declared EMPTY (a statement -- "we looked"), the
seam is LIVE so a future key needs no code change, and the judgment DV DOES let
us make (an unknown or expired request id past the 30-day window is a settled
refusal, not a retryable unknown) still applies.

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

_MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "doubleverify"
_CONNECTOR_PATH = _MODULE_DIR / "connector.py"


@pytest.fixture(scope="module")
def connector():
    spec = importlib.util.spec_from_file_location(
        "connector_doubleverify_errors", _CONNECTOR_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((_MODULE_DIR / "manifest.json").read_text(encoding="utf-8"))


def _response(status: int, body: dict, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        json=body,
        headers=headers or {},
        request=httpx.Request("GET", "https://example.com/reports/req_EXAMPLE/status"),
    )


def test_a_401_is_auth_expired_and_keeps_the_payload(connector):
    """The real raise site: typed, not retryable, payload preserved as evidence.

    The body below is deliberately generic. DV publishes no error-body schema,
    and this test does not pretend otherwise -- what it pins is that ANY 401
    comes out actionable with its evidence attached, which is the whole
    guarantee available without the portal reference.
    """
    body = {"message": "User is missing a valid token"}
    with pytest.raises(Exception) as exc_info:
        connector._raise_for_status(_response(401, body), "doubleverify")

    err = exc_info.value
    assert err.error_class == "auth_expired"
    assert err.retryable is False
    assert err.user_action == "reconnect"
    assert err.provider_status == 401
    assert err.provider_payload == body


def test_error_map_is_declared_empty_not_absent(connector, manifest):
    """`{}` says "we looked"; a missing key says "nobody looked". Keep the former."""
    assert "error_map" in manifest
    assert manifest["error_map"] == {}
    assert connector._load_error_map() == {}
    note = manifest.get("_error_map_note")
    assert note, "an empty map must carry its reason"
    assert note.isascii()  # AI-03


def test_the_seam_is_live_even_though_the_map_is_empty(connector):
    """The map is HANDED to core, so a future key needs no code change."""
    source = _CONNECTOR_PATH.read_text(encoding="utf-8")
    assert "_load_error_map()" in source


def test_an_expired_request_id_is_a_settled_refusal(connector):
    """The one DV judgment that exists, and it is a status, not a code.

    DV expires a report request id 30 days after the data is ready. core leaves
    a 404 'unclassified', i.e. retryable -- so the worker would re-ask for an id
    that can never come back. The verdict lives in ``_STATUS_OVERRIDES``, where
    a status-level judgment belongs; ``error_map`` has no grammar for it.
    """
    from core.pull_errors import classify_http_error

    assert set(connector._STATUS_OVERRIDES) == {404}

    with pytest.raises(Exception) as exc_info:
        connector._raise_for_status(_response(404, {"message": "not found"}), "doubleverify")
    assert exc_info.value.error_class == "invalid_request"
    assert exc_info.value.retryable is False

    assert classify_http_error(404, None, None).error_class == "unclassified"


def test_the_pure_http_statuses_stay_with_core(connector):
    """An empty map must not read as "this connector classifies nothing"."""
    for status, expected in (
        (400, "invalid_request"),
        (403, "permission_denied"),
        (500, "provider_transient"),
    ):
        with pytest.raises(Exception) as exc_info:
            connector._raise_for_status(_response(status, {"message": "x"}), "doubleverify")
        assert exc_info.value.error_class == expected, status


def test_a_429_goes_to_the_breaker_not_the_taxonomy(connector):
    """Which is also why a 429 entry in any error_map is dead by construction."""
    from core.pull_errors import classify_http_error
    from core.quota import RateLimitError

    with pytest.raises(RateLimitError) as exc_info:
        connector._raise_for_status(
            _response(429, {"message": "slow down"}, {"Retry-After": "15"}),
            "doubleverify",
        )
    assert exc_info.value.retry_after == 15

    with pytest.raises(ValueError):
        classify_http_error(429, {})
