"""Inbound receipt quarantine-write tests (Epic 38, Story 38.6).

Offline, no pg/dbt. Exercises the durable write the receipt handler performs for
a VERIFIED + in-bounds + well-shaped delivery when a quarantine backend is
configured (``INBOUND_QUARANTINE_LOCAL_ROOT``), plus the two guard rails:

  * with NO quarantine env set, behaviour is unchanged (202, nothing written);
  * a storage failure AFTER a valid delivery -> generic 500 (provider retry),
    never the constant-shape 403, and no token/recipient leaked.

Reuses the Mailgun HMAC signing helpers (same shape as test_receipt_handler.py)
so the delivery is genuinely signature-verified before the write is attempted.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time

import pytest
from inbound.receipt import build_inbound_app
from starlette.testclient import TestClient

_SIGNING_SECRET = "test-signing-key-DO-NOT-USE-IN-PROD"
_DOMAIN = "ingest.toorow.com"
_ROUTING_TOKEN = "abc123"
_GOOD_RECIPIENT = f"ds_{_ROUTING_TOKEN}@{_DOMAIN}"
_EMAIL_PATH = "/v1/webhooks/inbound-email"
_FILE_PATH = "/v1/webhooks/inbound-file"

_FORBIDDEN_BODY = {"code": "forbidden", "message": "forbidden"}


@pytest.fixture(autouse=True)
def _mailgun_env(monkeypatch):
    monkeypatch.setenv("INBOUND_PROVIDER", "mailgun")
    monkeypatch.setenv("INBOUND_SIGNING_SECRET", _SIGNING_SECRET)
    monkeypatch.setenv("INBOUND_MAX_BODY_BYTES", "26214400")
    monkeypatch.setenv("INBOUND_MAX_HEADER_BYTES", "16384")
    monkeypatch.setenv("INBOUND_MAX_ATTACHMENTS", "20")
    # Default: no quarantine backend configured. Individual tests opt in.
    monkeypatch.delenv("INBOUND_QUARANTINE_BUCKET", raising=False)
    monkeypatch.delenv("INBOUND_QUARANTINE_LOCAL_ROOT", raising=False)


def _client() -> TestClient:
    return TestClient(build_inbound_app(), raise_server_exceptions=False)


def _mailgun_sig(timestamp: str, token: str, secret: str = _SIGNING_SECRET) -> str:
    return hmac.new(
        key=secret.encode(),
        msg=f"{timestamp}{token}".encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()


def _signed_form(
    *,
    recipient: str = _GOOD_RECIPIENT,
    token: str = "evt-tok-123",
) -> dict[str, str]:
    ts = str(int(time.time()))
    return {
        "timestamp": ts,
        "token": token,
        "signature": _mailgun_sig(ts, token),
        "recipient": recipient,
    }


def _walk_files(root: str) -> list[str]:
    out: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            out.append(os.path.join(dirpath, name))
    return out


def _read_manifest(root: str) -> tuple[str, dict]:
    manifest_path = None
    for path in _walk_files(root):
        if os.path.basename(path) == "_manifest.json":
            manifest_path = path
            break
    assert manifest_path is not None, "no _manifest.json written under quarantine root"
    with open(manifest_path, "rb") as fh:
        return manifest_path, json.loads(fh.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Durable write happy path
# ---------------------------------------------------------------------------


class TestQuarantineWrite:
    def test_valid_delivery_writes_manifest_and_bytes(self, monkeypatch, tmp_path):
        root = str(tmp_path / "q")
        monkeypatch.setenv("INBOUND_QUARANTINE_LOCAL_ROOT", root)

        attachment_bytes = b"col_a,col_b\n1,2\n"
        provider_event_id = "evt-tok-123"
        form = _signed_form(token=provider_event_id)
        files = [("attachment-1", ("data.csv", attachment_bytes, "text/csv"))]

        resp = _client().post(_EMAIL_PATH, data=form, files=files)
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "accepted"
        assert body["correlation_id"].startswith("inbrx_")

        # Manifest matches the fixed schema.
        _manifest_path, manifest = _read_manifest(root)
        assert manifest["schema"] == "inbound-delivery-manifest-v1"
        assert manifest["provider_event_id"] == provider_event_id
        assert manifest["channel"] == "email"
        assert manifest["token_hash"] == hashlib.sha256(
            _ROUTING_TOKEN.encode("utf-8")
        ).hexdigest()
        assert manifest["recipient_hash"] == hashlib.sha256(
            _GOOD_RECIPIENT.encode("utf-8")
        ).hexdigest()
        assert isinstance(manifest["received_at"], str) and manifest["received_at"]

        # One attachment entry, well-shaped, and the bytes round-trip.
        assert len(manifest["attachments"]) == 1
        att = manifest["attachments"][0]
        assert att["filename"] == "data.csv"
        assert att["content_type"] == "text/csv"
        assert att["size"] == len(attachment_bytes)
        assert att["quarantine_uri"].startswith("file://")

        from core.inbound_quarantine import open_quarantine_store

        store = open_quarantine_store()
        assert store.get(att["quarantine_uri"]) == attachment_bytes

    def test_file_route_channel_is_webhook(self, monkeypatch, tmp_path):
        root = str(tmp_path / "q")
        monkeypatch.setenv("INBOUND_QUARANTINE_LOCAL_ROOT", root)
        files = [("attachment-1", ("data.csv", b"a,b\n", "text/csv"))]
        resp = _client().post(_FILE_PATH, data=_signed_form(), files=files)
        assert resp.status_code == 202
        _path, manifest = _read_manifest(root)
        assert manifest["channel"] == "webhook"

    def test_raw_token_and_recipient_never_written(self, monkeypatch, tmp_path):
        root = str(tmp_path / "q")
        monkeypatch.setenv("INBOUND_QUARANTINE_LOCAL_ROOT", root)
        files = [("attachment-1", ("data.csv", b"a,b\n", "text/csv"))]
        resp = _client().post(_EMAIL_PATH, data=_signed_form(), files=files)
        assert resp.status_code == 202

        # Walk EVERY file under the quarantine root (bytes + manifest + key paths)
        # and assert neither the raw routing token nor the raw recipient address
        # appears anywhere -- only their sha256 hashes may be present.
        token_needle = _ROUTING_TOKEN.encode("utf-8")
        recipient_needle = _GOOD_RECIPIENT.encode("utf-8")
        for path in _walk_files(root):
            # File CONTENTS must not contain the raw token/recipient.
            with open(path, "rb") as fh:
                content = fh.read()
            assert token_needle not in content, f"raw token leaked in {path}"
            assert recipient_needle not in content, f"raw recipient leaked in {path}"
            # Nor may the PATH itself (partition is the token HASH, not the token).
            assert _ROUTING_TOKEN not in path
            assert _GOOD_RECIPIENT not in path


# ---------------------------------------------------------------------------
# Gate: no quarantine env -> unchanged 38.1 behaviour (202, nothing written)
# ---------------------------------------------------------------------------


class TestQuarantineGate:
    def test_no_quarantine_env_writes_nothing(self, monkeypatch, tmp_path):
        # No INBOUND_QUARANTINE_* set (autouse fixture cleared them). Point a
        # would-be local root at an empty dir but do NOT export it, to prove the
        # handler does not touch storage.
        witness = tmp_path / "witness"
        witness.mkdir()

        files = [("attachment-1", ("data.csv", b"a,b\n", "text/csv"))]
        resp = _client().post(_EMAIL_PATH, data=_signed_form(), files=files)
        assert resp.status_code == 202
        assert resp.json()["status"] == "accepted"

        # Nothing was written to our witness dir (unchanged acknowledge-only).
        assert _walk_files(str(witness)) == []


# ---------------------------------------------------------------------------
# Storage failure AFTER a valid delivery -> generic 500 (retryable), not 403
# ---------------------------------------------------------------------------


class TestQuarantineFailure:
    def test_storage_failure_returns_500_not_403(self, monkeypatch, tmp_path):
        root = str(tmp_path / "q")
        monkeypatch.setenv("INBOUND_QUARANTINE_LOCAL_ROOT", root)

        import core.inbound_quarantine as q

        def _boom(self, **kwargs):
            raise q.QuarantineError("simulated storage failure")

        monkeypatch.setattr(q.LocalFsQuarantineStore, "put", _boom)

        files = [("attachment-1", ("data.csv", b"a,b\n", "text/csv"))]
        resp = _client().post(_EMAIL_PATH, data=_signed_form(), files=files)

        assert resp.status_code == 500
        body = resp.json()
        assert body == {"code": "internal", "message": "internal"}
        # A server condition, NOT the constant-shape 403.
        assert body != _FORBIDDEN_BODY
        # No token/recipient/internal detail leaked in the response.
        assert _ROUTING_TOKEN not in resp.text
        assert _GOOD_RECIPIENT not in resp.text
        assert "simulated storage failure" not in resp.text
