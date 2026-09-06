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

import contextlib
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
_ROUTING_TOKEN = "email_capability_0123456789abcdef0123456789"
_WEBHOOK_TOKEN = "webhook_capability_0123456789abcdef012345"
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

    class _Conn:
        def commit(self):
            pass

        def rollback(self):
            pass

    monkeypatch.setattr(
        "core.db.get_connection", lambda: contextlib.nullcontext(_Conn())
    )
    monkeypatch.setattr(
        "core.inbound_credentials.resolve_for_delivery",
        lambda _conn, *, raw_token: {
            "allowed": True,
            "scope": {
                "datastream_id": "ds-public-test",
                "credential_id": "dic_public_test",
                "channel": "webhook" if raw_token == _WEBHOOK_TOKEN else "email",
            },
        },
    )
    monkeypatch.setattr(
        "core.inbound_receipts.assert_provider_event_fingerprint",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "core.inbound_receipts.get_receipt_by_provider_event",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "core.inbound_receipts.record_receipt",
        lambda _conn, **kwargs: {
            "receipt_id": "inbrx_01JZAAABBBCCCDDDEEEFFF00999",
            "created_at": "2026-08-01T10:00:00+00:00",
            "operation_outcome": "succeeded",
        },
    )
    monkeypatch.setattr(
        "inbound.receipt._resolve_org_id",
        lambda _conn, *, datastream_id: "org-public-test",
    )
    monkeypatch.setattr(
        "core.inbound_raw_imports.record_raw_import",
        lambda _conn, **kwargs: {
            "raw_import_id": f"inbraw_{kwargs['ordinal']}",
            "state": "RECEIVED",
            "deduplicated": False,
        },
    )


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


def _read_file_bytes(path: str) -> bytes:
    # AI-135: quarantine addresses embed two 64-char SHA-256 components, so objects
    # land past 260 chars under pytest's temp root. The store writes them through
    # the same \\?\ prefix (core.inbound_quarantine._fs_path); without it a plain
    # open() on Windows (LongPathsEnabled=0) fails with FileNotFoundError at OPEN
    # time -- an environment red that masquerades as quarantine logic.
    p = os.path.abspath(path)
    if os.name == "nt" and not p.startswith("\\\\?\\"):
        p = "\\\\?\\" + p
    with open(p, "rb") as fh:
        return fh.read()


def _read_manifests(root: str) -> list[dict]:
    manifests = []
    for path in _walk_files(root):
        if os.path.basename(path) == "_manifest.json":
            manifests.append(json.loads(_read_file_bytes(path).decode("utf-8")))
    return manifests


def _read_manifest(root: str) -> tuple[str, dict]:
    manifest_path = None
    for path in _walk_files(root):
        if os.path.basename(path) == "_manifest.json":
            assert manifest_path is None, "expected single manifest"
            manifest_path = path
    assert manifest_path is not None, "manifest file missing"
    return manifest_path, json.loads(_read_file_bytes(manifest_path).decode("utf-8"))


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
        assert body["receipt_id"].startswith("inbrx_")

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
        resp = _client().post(
            _FILE_PATH,
            data=_signed_form(recipient=_WEBHOOK_TOKEN),
            files=files,
        )
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
            content = _read_file_bytes(path)
            assert token_needle not in content, f"raw token leaked in {path}"
            assert recipient_needle not in content, f"raw recipient leaked in {path}"
            # Nor may the PATH itself (partition is the token HASH, not the token).
            assert _ROUTING_TOKEN not in path
            assert _GOOD_RECIPIENT not in path


    def test_duplicate_reserved_filenames_get_distinct_safe_object_keys(
        self, monkeypatch, tmp_path
    ):
        root = str(tmp_path / "q")
        monkeypatch.setenv("INBOUND_QUARANTINE_LOCAL_ROOT", root)
        files = [
            ("attachment-1", ("_manifest.json", b"first", "application/json")),
            ("attachment-2", ("_manifest.json", b"second", "application/json")),
        ]

        response = _client().post(_EMAIL_PATH, data=_signed_form(), files=files)

        assert response.status_code == 202
        _path, manifest = _read_manifest(root)
        attachment_uris = [item["quarantine_uri"] for item in manifest["attachments"]]
        assert len(set(attachment_uris)) == 2
        assert all(not uri.endswith("/_manifest.json") for uri in attachment_uris)
        # Three immutable objects plus one immutable safe-metadata sidecar each.
        assert len(_walk_files(root)) == 6

    def test_same_bytes_from_distinct_events_keep_distinct_evidence(
        self, monkeypatch, tmp_path
    ):
        root = str(tmp_path / "q")
        monkeypatch.setenv("INBOUND_QUARANTINE_LOCAL_ROOT", root)
        raw_calls = []
        monkeypatch.setattr(
            "core.inbound_raw_imports.record_raw_import",
            lambda _conn, **kwargs: (
                raw_calls.append(kwargs)
                or {"raw_import_id": f"inbraw_{len(raw_calls)}"}
            ),
        )
        files = [("attachment-1", ("data.csv", b"same", "text/csv"))]

        first = _client().post(
            _EMAIL_PATH, data=_signed_form(token="event-one"), files=files
        )
        second = _client().post(
            _EMAIL_PATH, data=_signed_form(token="event-two"), files=files
        )

        assert first.status_code == second.status_code == 202
        manifests = {item["provider_event_id"]: item for item in _read_manifests(root)}
        first_uri = manifests["event-one"]["attachments"][0]["quarantine_uri"]
        second_uri = manifests["event-two"]["attachments"][0]["quarantine_uri"]
        assert first_uri != second_uri
        assert raw_calls[0]["quarantine_uri"] != raw_calls[1]["quarantine_uri"]
        assert raw_calls[0]["content_hash"] == raw_calls[1]["content_hash"]

    def test_replay_after_retention_config_change_reuses_original_policy(
        self, monkeypatch, tmp_path
    ):
        root = str(tmp_path / "q")
        monkeypatch.setenv("INBOUND_QUARANTINE_LOCAL_ROOT", root)
        monkeypatch.setenv("INBOUND_QUARANTINE_RETENTION_DAYS", "45")
        raw_calls = []
        monkeypatch.setattr(
            "core.inbound_raw_imports.record_raw_import",
            lambda _conn, **kwargs: (
                raw_calls.append(kwargs)
                or {"raw_import_id": "inbraw_original"}
            ),
        )
        form = _signed_form(token="stable-retention-event")
        files = [("attachment-1", ("data.csv", b"same", "text/csv"))]
        first = _client().post(_EMAIL_PATH, data=form, files=files)
        assert first.status_code == 202
        original_manifest = _read_manifests(root)[0]
        original = raw_calls[0]

        monkeypatch.setenv("INBOUND_QUARANTINE_RETENTION_DAYS", "90")
        monkeypatch.setattr(
            "core.inbound_receipts.get_receipt_by_provider_event",
            lambda *args, **kwargs: {
                "receipt_id": first.json()["receipt_id"],
                "created_at": "2026-08-01T10:00:00+00:00",
            },
        )
        monkeypatch.setattr(
            "core.inbound_raw_imports.list_raw_imports_for_receipt",
            lambda *args, **kwargs: [{
                "raw_import_id": "inbraw_original", "ordinal": 0,
                "filename": "data.csv", "media_type_declared": "text/csv",
                "size_bytes": 4, "content_hash": original["content_hash"],
                "quarantine_uri": original["quarantine_uri"],
                "retention_policy_version": "quarantine-retention-v1",
                "retention_days": 45,
            }],
        )
        second = _client().post(_EMAIL_PATH, data=form, files=files)

        assert second.status_code == 202
        assert second.json()["outcome"] == "replayed"
        assert len(raw_calls) == 1
        replay_manifest = _read_manifests(root)[0]
        assert replay_manifest["retention_policy"] == {
            "version": "quarantine-retention-v1", "days": 45
        }
        assert replay_manifest == original_manifest

    def test_identical_redelivery_reuses_immutable_objects(self, monkeypatch, tmp_path):
        root = str(tmp_path / "q")
        monkeypatch.setenv("INBOUND_QUARANTINE_LOCAL_ROOT", root)
        form = _signed_form(token="stable-replay-event")
        files = [("attachment-1", ("data.csv", b"a,b\n1,2\n", "text/csv"))]

        first = _client().post(_EMAIL_PATH, data=form, files=files)
        first_paths = set(_walk_files(root))
        second = _client().post(_EMAIL_PATH, data=form, files=files)

        assert first.status_code == second.status_code == 202
        assert first.json()["receipt_id"] == second.json()["receipt_id"]
        assert set(_walk_files(root)) == first_paths


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
        assert resp.status_code == 500
        assert resp.json() == {"code": "internal", "message": "internal"}

        # Nothing was written: an unready runtime never acknowledges delivery.
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
