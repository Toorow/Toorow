"""Story 25.7 (playbook step 4): google-analytics typed-error contract.

Mirrors server/tests/modules/meta_ads/test_pull_meta.py's 401 path proof, adapted
to GA4's POST runReport surface. Uses respx to mock the GA4 Data API; no test
contacts the real API (real E2E is a human gate, AI-08 / Phase-B checklist).

What these tests pin:
  - A 401 UNAUTHENTICATED response routes through core.pull_errors.classify_http_error
    and raises auth_expired (user_action=reconnect, retryable=False) with the parsed
    Google error body preserved as evidence.
  - A 403 PERMISSION_DENIED response raises permission_denied via the pure-HTTP class.
  - The manifest declares NO error_map (Google error.code == numeric HTTP status, so a
    refinement map would be redundant/unreachable) but DOES carry the _error_map_note
    that justifies the absence per the playbook.

These assert the EXISTING typed raise sites (classify_http_error(status, body), no map)
are correct and unchanged -- Story 25.7 adds no code-level error refinement for GA4.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")

_MODULE_DIR = (
    Path(__file__).parents[4] / "server" / "modules" / "google-analytics"
)
_TOOROW_PATH = _MODULE_DIR / "connector.py"
_MANIFEST_PATH = _MODULE_DIR / "manifest.json"

_RUN_REPORT_URL = (
    "https://analyticsdata.googleapis.com/v1beta/properties/TEST123:runReport"
)


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_ga4_errors", _TOOROW_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


@respx.mock
def test_pull_401_raises_auth_expired_with_payload_preserved(connector, tmp_path, monkeypatch):
    """A 401 UNAUTHENTICATED runReport response raises auth_expired, payload preserved.

    Proves the connector routes non-200/non-429 through classify_http_error and that
    the parsed Google error body survives as evidence on the typed error (pure-HTTP
    classification -- GA4 needs no error_map to reach auth_expired on 401).
    """
    monkeypatch.setenv("GA4_PROPERTY_ID", "TEST123")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "ga4_401.duckdb"))

    google_error = {
        "error": {
            "code": 401,
            "message": "Request had invalid authentication credentials. "
            "Expected OAuth 2 access token.",
            "status": "UNAUTHENTICATED",
        }
    }
    respx.post(_RUN_REPORT_URL).mock(
        return_value=httpx.Response(401, json=google_error)
    )

    from core.pull_errors import AuthExpiredError

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(AuthExpiredError) as exc_info:
            connector.pull(
                connection_id="conn_test",
                date_from="2026-01-01",
                date_to="2026-01-07",
                project_id="jean-ga4",
                pull_id="pull_ga4_401",
            )

    err = exc_info.value
    assert err.error_class == "auth_expired"
    assert err.retryable is False
    assert err.user_action == "reconnect"
    assert err.provider_status == 401
    # Parsed Google payload preserved intact (status token is the evidence we keep).
    assert err.provider_payload["error"]["status"] == "UNAUTHENTICATED"
    assert err.provider_payload["error"]["code"] == 401


@respx.mock
def test_pull_403_raises_permission_denied(connector, tmp_path, monkeypatch):
    """A 403 PERMISSION_DENIED runReport response raises permission_denied (pure-HTTP)."""
    monkeypatch.setenv("GA4_PROPERTY_ID", "TEST123")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "ga4_403.duckdb"))

    google_error = {
        "error": {
            "code": 403,
            "message": "User does not have sufficient permissions for this property.",
            "status": "PERMISSION_DENIED",
        }
    }
    respx.post(_RUN_REPORT_URL).mock(
        return_value=httpx.Response(403, json=google_error)
    )

    from core.pull_errors import PermissionDeniedError

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(PermissionDeniedError) as exc_info:
            connector.pull(
                connection_id="conn_test",
                date_from="2026-01-01",
                date_to="2026-01-07",
                project_id="jean-ga4",
                pull_id="pull_ga4_403",
            )

    err = exc_info.value
    assert err.error_class == "permission_denied"
    assert err.retryable is False
    assert err.provider_status == 403
    assert err.provider_payload["error"]["status"] == "PERMISSION_DENIED"


def test_manifest_declares_no_error_map_but_justifies_it():
    """Story 25.7: GA4 manifest carries NO error_map but DOES carry _error_map_note.

    Google error.code == numeric HTTP status, so any refinement key would be either
    redundant with the pure-HTTP classification or unreachable by the generic
    extractor; the absence must be explicitly justified (playbook step 4).
    """
    manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    assert "error_map" not in manifest, (
        "google-analytics must NOT declare an error_map (Google error.code is the "
        "numeric HTTP status -- refinement would be redundant/unreachable)."
    )
    note = manifest.get("_error_map_note")
    assert isinstance(note, str) and note.strip(), (
        "manifest must carry an _error_map_note justifying the absent error_map."
    )
    # The justification must name the reason (numeric code == HTTP status).
    assert "code" in note and "status" in note.lower()
