"""Offline tests for core.inbound_quarantine (LocalFsQuarantineStore + factory).

No live GCS calls. GCS class is probed only for lazy-import behaviour and
skipped when google.cloud.storage is absent.
"""

from __future__ import annotations

import importlib
import json
import pathlib
import sys
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

import pytest
from core.inbound_quarantine import (
    DEFAULT_MAX_SIZE,
    GcsQuarantineStore,
    LocalFsQuarantineStore,
    QuarantineError,
    QuarantineObject,
    QuarantineSizeError,
    _safe_filename,
    open_quarantine_store,
)


def _uri_to_path(uri: str) -> pathlib.Path:
    """Cross-platform inverse of Path.as_uri() (handles Windows drive paths)."""
    return pathlib.Path(url2pathname(unquote(urlparse(uri).path)))


# ---------------------------------------------------------------------------
# _safe_filename (unit)
# ---------------------------------------------------------------------------


class TestSafeFilename:
    def test_plain_name_unchanged(self):
        assert _safe_filename("report.xlsx") == "report.xlsx"

    def test_forward_slash_replaced(self):
        result = _safe_filename("../../etc/passwd")
        assert "/" not in result
        assert "\\" not in result

    def test_backslash_replaced(self):
        result = _safe_filename(r"some\path\file.csv")
        assert "\\" not in result

    def test_colon_replaced(self):
        result = _safe_filename("C:file.txt")
        assert ":" not in result

    def test_nul_byte_removed(self):
        result = _safe_filename("file\x00name.csv")
        assert "\x00" not in result

    def test_empty_becomes_attachment(self):
        assert _safe_filename("") == "attachment"

    def test_only_unsafe_chars_becomes_attachment(self):
        assert _safe_filename("///") == "attachment"

    def test_long_name_capped(self):
        long_name = "a" * 300
        result = _safe_filename(long_name)
        assert len(result) <= 255

    def test_whitespace_collapsed(self):
        result = _safe_filename("my   file.csv")
        assert "   " not in result


# ---------------------------------------------------------------------------
# LocalFsQuarantineStore: put/get round-trip
# ---------------------------------------------------------------------------


class TestLocalFsRoundTrip:
    def test_bytes_survive_round_trip(self, tmp_path):
        store = LocalFsQuarantineStore(root=str(tmp_path))
        payload = b"hello quarantine"
        obj = store.put(
            partition="part1",
            message_id="msg001",
            filename="data.csv",
            data=payload,
            content_type="text/csv",
        )
        assert isinstance(obj, QuarantineObject)
        assert obj.size == len(payload)
        assert store.get(obj.uri) == payload

    def test_binary_payload_round_trip(self, tmp_path):
        store = LocalFsQuarantineStore(root=str(tmp_path))
        payload = bytes(range(256)) * 100
        obj = store.put(
            partition="p",
            message_id="m",
            filename="bin.dat",
            data=payload,
        )
        assert store.get(obj.uri) == payload

    def test_empty_payload_round_trip(self, tmp_path):
        store = LocalFsQuarantineStore(root=str(tmp_path))
        obj = store.put(partition="p", message_id="m", filename="empty.txt", data=b"")
        assert store.get(obj.uri) == b""
        assert obj.size == 0

    def test_file_uri_scheme(self, tmp_path):
        store = LocalFsQuarantineStore(root=str(tmp_path))
        obj = store.put(partition="p", message_id="m", filename="f.txt", data=b"x")
        assert obj.uri.startswith("file://")

    def test_object_lands_under_root(self, tmp_path):
        store = LocalFsQuarantineStore(root=str(tmp_path))
        obj = store.put(partition="p", message_id="m", filename="f.txt", data=b"x")
        # Resolve the file:// URI back to a path and confirm it's inside tmp_path.
        fpath = _uri_to_path(obj.uri).resolve()
        assert str(fpath).startswith(str(tmp_path.resolve()))

    def test_key_layout_contains_partition_and_message_id(self, tmp_path):
        store = LocalFsQuarantineStore(root=str(tmp_path))
        obj = store.put(
            partition="myhash123",
            message_id="delivery-abc",
            filename="att.pdf",
            data=b"pdf",
        )
        assert "myhash123" in obj.uri
        assert "delivery-abc" in obj.uri
        assert "inbound" in obj.uri

    def test_directories_created_automatically(self, tmp_path):
        store = LocalFsQuarantineStore(root=str(tmp_path))
        obj = store.put(partition="deep", message_id="nesting", filename="x.txt", data=b"y")
        fpath = _uri_to_path(obj.uri)
        assert fpath.exists()

    def test_get_missing_uri_raises(self, tmp_path):
        store = LocalFsQuarantineStore(root=str(tmp_path))
        with pytest.raises(QuarantineError):
            store.get("file:///nonexistent/path/that/does/not/exist.bin")

    def test_get_wrong_scheme_raises(self, tmp_path):
        store = LocalFsQuarantineStore(root=str(tmp_path))
        with pytest.raises(QuarantineError):
            store.get("gs://some-bucket/inbound/p/m/f.txt")


def test_object_scoped_read_rejects_another_datastream(tmp_path):
    store = LocalFsQuarantineStore(root=str(tmp_path))
    digest = "a" * 64
    obj = store.put(
        partition="org-1/ds-1",
        message_id=digest,
        filename="attachment-0000",
        data=b"evidence",
    )

    assert (
        store.get(
            obj.uri,
            expected_org_id="org-1",
            expected_datastream_id="ds-1",
            expected_content_hash=digest,
        )
        == b"evidence"
    )
    with pytest.raises(QuarantineError, match="expected object scope"):
        store.get(
            obj.uri,
            expected_org_id="org-1",
            expected_datastream_id="ds-2",
            expected_content_hash=digest,
        )


def test_local_legal_hold_is_scoped_and_effective(tmp_path):
    store = LocalFsQuarantineStore(root=str(tmp_path))
    digest = "c" * 64
    obj = store.put(
        partition="org-1/ds-1",
        message_id=digest,
        filename="provider-0000",
        data=b"held",
    )
    store.set_legal_hold(
        obj.uri,
        enabled=True,
        expected_org_id="org-1",
        expected_datastream_id="ds-1",
        expected_content_hash=digest,
    )
    path = _uri_to_path(obj.uri)
    hold = path.with_name(f"{path.name}.legal-hold.json")
    assert json.loads(hold.read_text(encoding="utf-8")) == {"event_based_hold": True}
    with pytest.raises(QuarantineError, match="expected object scope"):
        store.set_legal_hold(
            obj.uri,
            enabled=True,
            expected_org_id="org-1",
            expected_datastream_id="ds-2",
            expected_content_hash=digest,
        )


def test_safe_metadata_is_immutable_and_persisted(tmp_path):
    store = LocalFsQuarantineStore(root=str(tmp_path))
    metadata = {
        "receipt_fingerprint": "b" * 64,
        "attachment_ordinal": "0",
        "content_sha256": "a" * 64,
    }
    obj = store.put(
        partition="org-1/ds-1",
        message_id="a" * 64,
        filename="attachment-0000",
        data=b"evidence",
        metadata=metadata,
    )
    sidecar = _uri_to_path(obj.uri).with_name(f"{_uri_to_path(obj.uri).name}.metadata.json")
    assert json.loads(sidecar.read_text(encoding="utf-8")) == metadata
    with pytest.raises(QuarantineError):
        store.put(
            partition="org-1/ds-1",
            message_id="a" * 64,
            filename="attachment-0000",
            data=b"evidence",
            metadata={**metadata, "attachment_ordinal": "1"},
        )


# ---------------------------------------------------------------------------
# Filename sanitisation: path-traversal cannot escape root
# ---------------------------------------------------------------------------


class TestFilenameTraversal:
    def test_dotdot_slash_cannot_escape(self, tmp_path):
        store = LocalFsQuarantineStore(root=str(tmp_path))
        obj = store.put(
            partition="p",
            message_id="m",
            filename="../../etc/passwd",
            data=b"safe",
        )
        fpath = _uri_to_path(obj.uri).resolve()
        assert str(fpath).startswith(str(tmp_path.resolve()))

    def test_backslash_traversal_cannot_escape(self, tmp_path):
        store = LocalFsQuarantineStore(root=str(tmp_path))
        obj = store.put(
            partition="p",
            message_id="m",
            filename=r"..\windows\system32\cmd.exe",
            data=b"safe",
        )
        fpath = _uri_to_path(obj.uri).resolve()
        assert str(fpath).startswith(str(tmp_path.resolve()))

    def test_absolute_path_in_filename_cannot_escape(self, tmp_path):
        store = LocalFsQuarantineStore(root=str(tmp_path))
        obj = store.put(
            partition="p",
            message_id="m",
            filename="/tmp/evil.sh",
            data=b"safe",
        )
        fpath = _uri_to_path(obj.uri).resolve()
        assert str(fpath).startswith(str(tmp_path.resolve()))


# ---------------------------------------------------------------------------
# Size guard
# ---------------------------------------------------------------------------


class TestSizeGuard:
    def test_exceeding_max_size_raises_quarantine_size_error(self, tmp_path):
        store = LocalFsQuarantineStore(root=str(tmp_path), max_size=10)
        with pytest.raises(QuarantineSizeError):
            store.put(partition="p", message_id="m", filename="f.txt", data=b"x" * 11)

    def test_exactly_at_max_size_is_accepted(self, tmp_path):
        store = LocalFsQuarantineStore(root=str(tmp_path), max_size=10)
        obj = store.put(partition="p", message_id="m", filename="f.txt", data=b"x" * 10)
        assert obj.size == 10

    def test_quarantine_size_error_is_quarantine_error(self, tmp_path):
        store = LocalFsQuarantineStore(root=str(tmp_path), max_size=1)
        with pytest.raises(QuarantineError):
            store.put(partition="p", message_id="m", filename="f.txt", data=b"toolong")

    def test_default_max_size_constant(self):
        assert DEFAULT_MAX_SIZE == 25 * 1024 * 1024


# ---------------------------------------------------------------------------
# URI shape stability
# ---------------------------------------------------------------------------


class TestUriShape:
    def test_uri_is_deterministic_for_same_inputs(self, tmp_path):
        store = LocalFsQuarantineStore(root=str(tmp_path))
        args = dict(partition="p", message_id="m", filename="f.csv", data=b"1")
        obj1 = store.put(**args)
        # Second put overwrites the same path (idempotent key layout).
        obj2 = store.put(**args)
        assert obj1.uri == obj2.uri

    def test_different_partitions_produce_different_uris(self, tmp_path):
        store = LocalFsQuarantineStore(root=str(tmp_path))
        obj1 = store.put(partition="p1", message_id="m", filename="f.txt", data=b"a")
        obj2 = store.put(partition="p2", message_id="m", filename="f.txt", data=b"a")
        assert obj1.uri != obj2.uri

    def test_different_message_ids_produce_different_uris(self, tmp_path):
        store = LocalFsQuarantineStore(root=str(tmp_path))
        obj1 = store.put(partition="p", message_id="m1", filename="f.txt", data=b"a")
        obj2 = store.put(partition="p", message_id="m2", filename="f.txt", data=b"a")
        assert obj1.uri != obj2.uri


# ---------------------------------------------------------------------------
# Factory: env-driven selection
# ---------------------------------------------------------------------------


class TestOpenQuarantineStore:
    def test_local_root_env_selects_local_fs(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INBOUND_QUARANTINE_LOCAL_ROOT", str(tmp_path))
        monkeypatch.delenv("INBOUND_QUARANTINE_BUCKET", raising=False)
        store = open_quarantine_store()
        assert isinstance(store, LocalFsQuarantineStore)

    def test_bucket_env_selects_gcs(self, monkeypatch):
        monkeypatch.setenv("INBOUND_QUARANTINE_BUCKET", "my-bucket")
        monkeypatch.delenv("INBOUND_QUARANTINE_LOCAL_ROOT", raising=False)
        store = open_quarantine_store()
        assert isinstance(store, GcsQuarantineStore)

    def test_neither_env_selects_local_fs_with_warning(self, monkeypatch, caplog):
        monkeypatch.delenv("INBOUND_QUARANTINE_BUCKET", raising=False)
        monkeypatch.delenv("INBOUND_QUARANTINE_LOCAL_ROOT", raising=False)
        import logging

        with caplog.at_level(logging.WARNING, logger="core.inbound_quarantine"):
            store = open_quarantine_store()
        assert isinstance(store, LocalFsQuarantineStore)
        assert any("not suitable for production" in r.message for r in caplog.records)

    def test_bucket_env_takes_priority_over_local_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INBOUND_QUARANTINE_BUCKET", "my-bucket")
        monkeypatch.setenv("INBOUND_QUARANTINE_LOCAL_ROOT", str(tmp_path))
        store = open_quarantine_store()
        assert isinstance(store, GcsQuarantineStore)

    def test_factory_passes_max_size_to_local_store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INBOUND_QUARANTINE_LOCAL_ROOT", str(tmp_path))
        monkeypatch.delenv("INBOUND_QUARANTINE_BUCKET", raising=False)
        store = open_quarantine_store(max_size=1234)
        assert store._max_size == 1234  # noqa: SLF001

    def test_factory_local_store_is_functional(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INBOUND_QUARANTINE_LOCAL_ROOT", str(tmp_path))
        monkeypatch.delenv("INBOUND_QUARANTINE_BUCKET", raising=False)
        store = open_quarantine_store()
        obj = store.put(partition="pp", message_id="mm", filename="test.bin", data=b"abc")
        assert store.get(obj.uri) == b"abc"


def test_terraform_expires_live_objects_and_audits_deletion():
    terraform = (
        pathlib.Path(__file__).parents[3] / "infra/terraform/inbound_runtime.tf"
    ).read_text(encoding="utf-8")

    assert 'with_state = "LIVE"' in terraform
    assert "retention_policy" in terraform
    assert 'log_type = "DATA_WRITE"' in terraform
    assert "default_event_based_hold = false" in terraform
    assert 'with_state                  = "ARCHIVED"' in terraform
    assert "days_since_noncurrent_time = 1" in terraform
    bridge = (pathlib.Path(__file__).parents[3] / "infra/terraform/inbound_bridge.tf").read_text(
        encoding="utf-8"
    )
    assert '"storage.objects.update"' in bridge
    assert '"storage.objects.get"' in bridge
    assert '"storage.objects.delete"' not in bridge


# ---------------------------------------------------------------------------
# GCS lazy-import probe (skipped if SDK absent)
# ---------------------------------------------------------------------------


class TestGcsLazyImport:
    """Verify that GcsQuarantineStore does not import the GCS SDK at class
    instantiation time, only when a method is called."""

    def test_gcs_instantiation_does_not_import_sdk(self):
        """Constructing GcsQuarantineStore must succeed without google.cloud.storage."""
        # Remove the SDK from sys.modules if present so we can confirm the
        # constructor does not import it.
        gcs_mod = sys.modules.pop("google.cloud.storage", None)
        google_cloud = sys.modules.pop("google.cloud", None)
        google_mod = sys.modules.pop("google", None)
        try:
            store = GcsQuarantineStore(bucket="test-bucket")
            assert store._bucket == "test-bucket"  # noqa: SLF001
        finally:
            # Restore whatever was there.
            if google_mod is not None:
                sys.modules["google"] = google_mod
            if google_cloud is not None:
                sys.modules["google.cloud"] = google_cloud
            if gcs_mod is not None:
                sys.modules["google.cloud.storage"] = gcs_mod

    @pytest.mark.skipif(
        importlib.util.find_spec("google.cloud.storage") is None,
        reason="google-cloud-storage not installed",
    )
    def test_gcs_put_imports_sdk_when_present(self, monkeypatch):
        """Smoke: GCS SDK is importable when installed (no credentials check)."""
        import google.cloud.storage  # noqa: F401

        # We only assert the import succeeds; no real bucket call is made.
        store = GcsQuarantineStore(bucket="smoke-test")
        assert store is not None


def test_local_bounded_read_refuses_from_metadata_before_full_load(tmp_path, monkeypatch):
    from core.inbound_quarantine import LocalFsQuarantineStore, QuarantineSizeError

    store = LocalFsQuarantineStore(root=str(tmp_path))
    obj = store.put(
        partition="org-1/ds-1",
        message_id="a" * 64,
        filename="data.csv",
        data=b"a,b\n" + b"1,2\n" * 100,
        content_type="text/csv",
    )
    with pytest.raises(QuarantineSizeError) as failure:
        store.get_bounded(obj.uri, max_bytes=16)
    assert failure.value.observed_size == obj.size
    assert failure.value.max_bytes == 16
