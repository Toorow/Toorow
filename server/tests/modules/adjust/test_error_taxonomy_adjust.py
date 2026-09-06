"""Adjust error taxonomy -- story 67-21 (2026-08-17).

WHY THIS FILE EXISTS, AND WHY THE MAP IS STILL EMPTY. Repair 6 of
reviews/audit-2026-08-17/13-connecteurs.md asked for this manifest's
``error_map`` to be filled. It is not, and that is a measurement rather than an
unfinished job: Adjust publishes response-code tables and NO error body at all.
The four Report Service API pages (index, authentication, reports, rate limits)
were re-read on 2026-08-17 and none of them names an error field, an error code,
or an example error payload; dev.adjust.com has no errors or troubleshooting
page for the RS API.

There is therefore no ``"<status>:<provider_code>"`` this repo could write
without inventing Adjust's vocabulary, and an invented key is worse than an
empty map: it makes the taxonomy look refined where it is not. That is the
AI-114 finding, and the reason this file asserts an ABSENCE deliberately.

Adjust does have a revoked case the taxonomy cannot see: resetting the API token
in the dashboard invalidates the old one immediately (rs-api/authentication),
and Adjust answers it with a bare 401, indistinguishable from any other. Closing
that needs one live capture (AI-13).

What IS pinned here: the map is declared EMPTY (a statement -- "we looked") and
the seam is LIVE, i.e. the map is handed to ``classify_http_error`` at every
raise site, so a key works the day one exists.

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

_MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "adjust"
_CONNECTOR_PATH = _MODULE_DIR / "connector.py"


@pytest.fixture(scope="module")
def connector():
    spec = importlib.util.spec_from_file_location(
        "connector_adjust_errors", _CONNECTOR_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((_MODULE_DIR / "manifest.json").read_text(encoding="utf-8"))


def _response(status: int, body: dict) -> httpx.Response:
    return httpx.Response(
        status,
        json=body,
        request=httpx.Request(
            "GET", "https://example.com/reports-service/filters_data"
        ),
    )


def _discover(connector, monkeypatch, response: httpx.Response):
    """Drive the real raise site of discover_accounts with a canned response."""
    from core import nango_client

    monkeypatch.setattr(
        nango_client, "get_fresh_token", lambda *a, **k: "token-not-used", raising=False
    )
    monkeypatch.setattr(connector.httpx, "get", lambda *a, **k: response)
    return connector.discover_accounts("conn_EXAMPLE")


def test_a_401_is_auth_expired_and_keeps_the_payload(connector, monkeypatch):
    """The real raise site: typed, not retryable, payload preserved as evidence.

    Adjust documents 401 as "your credentials are incorrect or absent" and says
    nothing more, so the pure-HTTP verdict is the whole verdict here.
    """
    body = {"error": "Unauthorized"}
    with pytest.raises(Exception) as exc_info:
        _discover(connector, monkeypatch, _response(401, body))

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
    """Both raise sites hand the map to core, so a future key needs no code change."""
    source = _CONNECTOR_PATH.read_text(encoding="utf-8")
    assert source.count("_load_error_map()") >= 2


def test_the_statuses_adjust_documents_all_land_somewhere(connector, monkeypatch):
    """Every documented response code resolves to an actionable class.

    Adjust's published table is 200/204/400/401/403/429/503/504. This walks the
    error half of it: if the empty map ever left one of them unclassified (i.e.
    retried forever with nothing to tell the operator), it would show here.
    """
    for status, expected in (
        (400, "invalid_request"),
        (403, "permission_denied"),
        (503, "provider_transient"),
        (504, "provider_transient"),
    ):
        with pytest.raises(Exception) as exc_info:
            _discover(connector, monkeypatch, _response(status, {"error": "x"}))
        assert exc_info.value.error_class == expected, status


def test_a_429_goes_to_the_breaker_not_the_taxonomy(connector, monkeypatch):
    """Which is also why a 429 entry in any error_map is dead by construction."""
    from core.pull_errors import classify_http_error
    from core.quota import RateLimitError

    response = httpx.Response(
        429,
        headers={"Retry-After": "30"},
        json={"error": "Too Many Requests"},
        request=httpx.Request("GET", "https://example.com/reports-service/filters_data"),
    )
    with pytest.raises(RateLimitError) as exc_info:
        _discover(connector, monkeypatch, response)
    assert exc_info.value.retry_after == 30

    with pytest.raises(ValueError):
        classify_http_error(429, {})


def test_a_204_is_an_empty_report_not_an_error(connector, manifest):
    """The one documented status that must NEVER become a taxonomy entry.

    Adjust answers 204 for a report with no rows. Treating it as an error would
    turn an honest zero-row pull into a failure the operator has to chase.
    """
    assert not [key for key in manifest["error_map"] if key.startswith("204:")]
    assert "204" in _CONNECTOR_PATH.read_text(encoding="utf-8")
