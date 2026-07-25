"""Inbound receipt handler tests (Epic 38, Story 38.1).

Offline, no pg/dbt. Exercises the bounded ASGI handler through Starlette's
TestClient (same pattern as server/tests/core/test_admin_api_jobs.py):

  * constant-shape 403 on unsigned / oversize / bad-recipient / replay;
  * 202 on a valid signed fixture for BOTH adapters (mailgun + cloudflare_worker);
  * size / header / attachment-count guards.

The signing secret and provider are supplied via env, matching the deploy-time
contract. No secret appears in any response body.
"""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest
from inbound.receipt import build_inbound_app
from starlette.testclient import TestClient

_SIGNING_SECRET = "test-signing-key-DO-NOT-USE-IN-PROD"
_DOMAIN = "ingest.toorow.com"
_GOOD_RECIPIENT = f"ds_abc123@{_DOMAIN}"
_EMAIL_PATH = "/v1/webhooks/inbound-email"
_FILE_PATH = "/v1/webhooks/inbound-file"

# The exact constant-shape rejection body — identical for every failure class.
_FORBIDDEN_BODY = {"code": "forbidden", "message": "forbidden"}


@pytest.fixture(autouse=True)
def _mailgun_env(monkeypatch):
    monkeypatch.setenv("INBOUND_PROVIDER", "mailgun")
    monkeypatch.setenv("INBOUND_SIGNING_SECRET", _SIGNING_SECRET)
    monkeypatch.setenv("INBOUND_MAX_BODY_BYTES", "26214400")
    monkeypatch.setenv("INBOUND_MAX_HEADER_BYTES", "16384")
    monkeypatch.setenv("INBOUND_MAX_ATTACHMENTS", "20")


def _client() -> TestClient:
    return TestClient(build_inbound_app(), raise_server_exceptions=False)


def _mailgun_sig(timestamp: str, token: str, secret: str = _SIGNING_SECRET) -> str:
    return hmac.new(
        key=secret.encode(),
        msg=f"{timestamp}{token}".encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()


def _mailgun_form(
    *,
    recipient: str = _GOOD_RECIPIENT,
    token: str = "tok-abc",
    timestamp: str | None = None,
    signature: str | None = None,
) -> dict[str, str]:
    ts = timestamp if timestamp is not None else str(int(time.time()))
    sig = signature if signature is not None else _mailgun_sig(ts, token)
    return {
        "timestamp": ts,
        "token": token,
        "signature": sig,
        "recipient": recipient,
    }


# ---------------------------------------------------------------------------
# 202 — happy path (mailgun)
# ---------------------------------------------------------------------------


class TestMailgunAccept:
    def test_valid_signed_email_returns_202_with_correlation_id(self):
        resp = _client().post(_EMAIL_PATH, data=_mailgun_form())
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "accepted"
        assert body["correlation_id"].startswith("inbrx_")

    def test_valid_signed_file_route_also_202(self):
        resp = _client().post(_FILE_PATH, data=_mailgun_form())
        assert resp.status_code == 202

    def test_202_response_contains_no_secret(self):
        resp = _client().post(_EMAIL_PATH, data=_mailgun_form())
        assert _SIGNING_SECRET not in resp.text


# ---------------------------------------------------------------------------
# Constant-shape 403
# ---------------------------------------------------------------------------


class TestConstantShape403:
    def test_unsigned_request_is_403(self):
        # No signature/token/timestamp fields at all.
        resp = _client().post(_EMAIL_PATH, data={"recipient": _GOOD_RECIPIENT})
        assert resp.status_code == 403
        assert resp.json() == _FORBIDDEN_BODY

    def test_bad_signature_is_403(self):
        form = _mailgun_form(signature="deadbeef" * 8)
        resp = _client().post(_EMAIL_PATH, data=form)
        assert resp.status_code == 403
        assert resp.json() == _FORBIDDEN_BODY

    def test_unknown_recipient_shape_is_403(self):
        # Correctly signed, but recipient does not match ds_<token>@domain.
        form = _mailgun_form(recipient="not-a-datastream@ingest.toorow.com")
        resp = _client().post(_EMAIL_PATH, data=form)
        assert resp.status_code == 403
        assert resp.json() == _FORBIDDEN_BODY

    def test_replay_stale_timestamp_is_403(self):
        old_ts = str(int(time.time()) - 3600)  # 1h old, outside the replay window
        form = _mailgun_form(timestamp=old_ts)
        resp = _client().post(_EMAIL_PATH, data=form)
        assert resp.status_code == 403
        assert resp.json() == _FORBIDDEN_BODY

    def test_future_dated_timestamp_is_403(self):
        # Future-dated beyond the window (skew abuse) is rejected symmetrically.
        future_ts = str(int(time.time()) + 3600)
        form = _mailgun_form(timestamp=future_ts)
        resp = _client().post(_EMAIL_PATH, data=form)
        assert resp.status_code == 403
        assert resp.json() == _FORBIDDEN_BODY

    def test_non_ascii_signature_is_403_not_500(self):
        # Regression: a non-ASCII signature must fail closed, not crash
        # hmac.compare_digest into a distinguishable 500.
        form = _mailgun_form(signature="é" * 64)
        resp = _client().post(_EMAIL_PATH, data=form)
        assert resp.status_code == 403
        assert resp.json() == _FORBIDDEN_BODY

    def test_valid_signature_but_oversize_is_403(self, monkeypatch):
        # Ordering: the raw-body bound rejects before verification can accept.
        monkeypatch.setenv("INBOUND_MAX_BODY_BYTES", "128")
        form = _mailgun_form()  # correctly signed
        form["padding"] = "x" * 5000
        resp = _client().post(_EMAIL_PATH, data=form)
        assert resp.status_code == 403
        assert resp.json() == _FORBIDDEN_BODY

    def test_all_rejection_reasons_share_one_shape(self):
        client = _client()
        bodies = [
            client.post(_EMAIL_PATH, data={"recipient": _GOOD_RECIPIENT}).json(),
            client.post(_EMAIL_PATH, data=_mailgun_form(signature="x" * 64)).json(),
            client.post(
                _EMAIL_PATH, data=_mailgun_form(recipient="bad@x.test")
            ).json(),
        ]
        # No existence disclosure: every failure class is byte-identical.
        assert all(b == _FORBIDDEN_BODY for b in bodies)


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


class TestBounds:
    def test_oversize_body_is_403(self, monkeypatch):
        monkeypatch.setenv("INBOUND_MAX_BODY_BYTES", "128")
        big = "x" * 5000
        form = _mailgun_form()
        form["padding"] = big
        resp = _client().post(_EMAIL_PATH, data=form)
        assert resp.status_code == 403
        assert resp.json() == _FORBIDDEN_BODY

    def test_oversize_declared_content_length_is_403(self, monkeypatch):
        monkeypatch.setenv("INBOUND_MAX_BODY_BYTES", "10")
        resp = _client().post(
            _EMAIL_PATH,
            data=_mailgun_form(),
            headers={"content-length": "99999"},
        )
        assert resp.status_code == 403

    def test_oversize_headers_is_403(self, monkeypatch):
        monkeypatch.setenv("INBOUND_MAX_HEADER_BYTES", "64")
        resp = _client().post(
            _EMAIL_PATH,
            data=_mailgun_form(),
            headers={"x-bloat": "z" * 4000},
        )
        assert resp.status_code == 403
        assert resp.json() == _FORBIDDEN_BODY

    def test_too_many_attachments_is_403(self, monkeypatch):
        monkeypatch.setenv("INBOUND_MAX_ATTACHMENTS", "2")
        token = "tok-att"
        ts = str(int(time.time()))
        form = {
            "timestamp": ts,
            "token": token,
            "signature": _mailgun_sig(ts, token),
            "recipient": _GOOD_RECIPIENT,
        }
        files = [
            ("attachment-1", ("a.csv", b"a,b", "text/csv")),
            ("attachment-2", ("b.csv", b"c,d", "text/csv")),
            ("attachment-3", ("c.csv", b"e,f", "text/csv")),
        ]
        resp = _client().post(_EMAIL_PATH, data=form, files=files)
        assert resp.status_code == 403
        assert resp.json() == _FORBIDDEN_BODY

    def test_within_attachment_bound_is_202(self, monkeypatch):
        monkeypatch.setenv("INBOUND_MAX_ATTACHMENTS", "5")
        token = "tok-ok"
        ts = str(int(time.time()))
        form = {
            "timestamp": ts,
            "token": token,
            "signature": _mailgun_sig(ts, token),
            "recipient": _GOOD_RECIPIENT,
        }
        files = [("attachment-1", ("a.csv", b"a,b", "text/csv"))]
        resp = _client().post(_EMAIL_PATH, data=form, files=files)
        assert resp.status_code == 202


# ---------------------------------------------------------------------------
# Cloudflare worker adapter (seam proof)
# ---------------------------------------------------------------------------


class TestCloudflareWorkerAdapter:
    def _cf_env(self, monkeypatch):
        monkeypatch.setenv("INBOUND_PROVIDER", "cloudflare_worker")
        monkeypatch.setenv("INBOUND_SIGNING_SECRET", _SIGNING_SECRET)

    def test_valid_shared_secret_returns_202(self, monkeypatch):
        self._cf_env(monkeypatch)
        resp = _client().post(
            _EMAIL_PATH,
            data={"recipient": _GOOD_RECIPIENT},
            headers={
                "x-inbound-worker-secret": _SIGNING_SECRET,
                "x-inbound-event-id": "evt-123",
                "x-inbound-recipient": _GOOD_RECIPIENT,
            },
        )
        assert resp.status_code == 202
        assert resp.json()["correlation_id"].startswith("inbrx_")

    def test_wrong_shared_secret_is_constant_shape_403(self, monkeypatch):
        self._cf_env(monkeypatch)
        resp = _client().post(
            _EMAIL_PATH,
            data={"recipient": _GOOD_RECIPIENT},
            headers={
                "x-inbound-worker-secret": "wrong",
                "x-inbound-event-id": "evt-123",
                "x-inbound-recipient": _GOOD_RECIPIENT,
            },
        )
        assert resp.status_code == 403
        assert resp.json() == _FORBIDDEN_BODY

    def test_missing_event_id_is_403(self, monkeypatch):
        self._cf_env(monkeypatch)
        resp = _client().post(
            _EMAIL_PATH,
            data={"recipient": _GOOD_RECIPIENT},
            headers={
                "x-inbound-worker-secret": _SIGNING_SECRET,
                "x-inbound-recipient": _GOOD_RECIPIENT,
            },
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Operator misconfiguration — 500, but never leaks the configured provider name
# ---------------------------------------------------------------------------


def test_misconfigured_provider_returns_500_without_leaking_name(monkeypatch):
    monkeypatch.setenv("INBOUND_PROVIDER", "bogus-provider-xyz")
    resp = _client().post(_EMAIL_PATH, data=_mailgun_form())
    assert resp.status_code == 500
    assert "bogus-provider-xyz" not in resp.text
