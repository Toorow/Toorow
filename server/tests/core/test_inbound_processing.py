"""Story 38.9: Inbound processing worker -- offline unit tests.

Covers ``core.inbound_processing.process_inbound_delivery`` with the sibling
seams (resolve_by_token_hash / record_receipt / get_receipt / mark_state /
ingest_inbound_file) monkeypatched, so NO Postgres or warehouse is needed. The
quarantine bytes use a real ``LocalFsQuarantineStore`` under a tmp dir.

Cases:
  (a) happy path: resolve -> receipt -> PROCESSING -> ingest -> LANDED with
      import_ledger_id; mark_state called in order [PROCESSING, LANDED].
  (b) denied token -> {"status": "denied"}; no receipt recorded.
  (c) duplicate terminal receipt -> {"status": "duplicate"}; no re-ingest.
  (d) zero data attachments -> FAILED with error_code=no_data_attachment.
  (e) more than one data attachment -> first processed + ignored_attachments.
  (f) ingest blocked -> REJECTED with error_code = reason.
  (g) ingest raises a typed error -> FAILED.
  (h) malformed manifest -> InboundProcessingValidationError.

INVARIANTS CHECKED:
  * Source-agnostic: this test carries no vendor vocabulary (the boundary scanner
    scans the core dir; the test stays provider-free too).
  * Non-tautological: assertions check the exact status, the recorded state
    ordering, and that denied deliveries never touch the receipt seam.
"""

from __future__ import annotations

import pathlib

import pytest

# ---------------------------------------------------------------------------
# Manifest + store helpers.
# ---------------------------------------------------------------------------


def _manifest(attachments, *, provider_event_id="evt-1", channel="email"):
    return {
        "schema": "inbound-delivery-manifest-v1",
        "provider_event_id": provider_event_id,
        "channel": channel,
        "token_hash": "a" * 64,
        "recipient_hash": "b" * 64,
        "received_at": "2026-01-01T00:00:00+00:00",
        "attachments": attachments,
    }


def _store(tmp_path: pathlib.Path):
    from core.inbound_quarantine import LocalFsQuarantineStore

    return LocalFsQuarantineStore(root=str(tmp_path))


def _put(store, *, filename="data.csv", data=b"col\n1\n", partition="p", message_id="m"):
    obj = store.put(
        partition=partition,
        message_id=message_id,
        filename=filename,
        data=data,
        content_type="text/csv",
    )
    return {
        "filename": filename,
        "quarantine_uri": obj.uri,
        "size": obj.size,
        "content_type": "text/csv",
    }


class _Recorder:
    """Captures the sibling-seam calls in order for assertions."""

    def __init__(self):
        self.states: list[str] = []
        self.receipt_recorded = 0
        self.ingested = 0
        self.mark_state_calls: list[dict] = []


def _patch_seams(
    monkeypatch,
    rec: _Recorder,
    *,
    allowed=True,
    deduplicated=False,
    existing_state=None,
    existing_ledger_id=None,
    ingest_result=None,
    ingest_error=None,
    project_id="proj-1",
):
    """Patch the lazily-imported sibling seams on their home modules."""
    import core.inbound_credentials as ic
    import core.inbound_ingest as ii
    import core.inbound_receipts as ir

    # resolve_by_token_hash
    def _resolve(conn, *, token_hash):
        if allowed:
            return {
                "allowed": True,
                "scope": {
                    "datastream_id": "ds-1",
                    "channel": "email",
                    "version": 1,
                    "credential_id": "dic_1",
                },
            }
        return {"allowed": False, "scope": None, "reason": "denied"}

    monkeypatch.setattr(ic, "resolve_by_token_hash", _resolve)

    # record_receipt
    def _record(conn, **kwargs):
        rec.receipt_recorded += 1
        return {"receipt_id": "inbrx_1", "deduplicated": deduplicated}

    monkeypatch.setattr(ir, "record_receipt", _record)

    # get_receipt (only used on dedup)
    def _get(conn, *, receipt_id, datastream_id):
        return {"state": existing_state, "import_ledger_id": existing_ledger_id}

    monkeypatch.setattr(ir, "get_receipt", _get)

    # mark_state
    def _mark(conn, *, receipt_id, datastream_id, state, **kwargs):
        rec.states.append(state)
        rec.mark_state_calls.append({"state": state, **kwargs})
        return {"state": state}

    monkeypatch.setattr(ir, "mark_state", _mark)

    # ingest_inbound_file
    def _ingest(conn, **kwargs):
        rec.ingested += 1
        if ingest_error is not None:
            raise ingest_error
        return ingest_result or {}

    monkeypatch.setattr(ii, "ingest_inbound_file", _ingest)

    # project_id read: patch the small DB helper directly.
    import core.inbound_processing as ip

    monkeypatch.setattr(
        ip, "_resolve_project_id", lambda conn, *, datastream_id: project_id
    )


# ---------------------------------------------------------------------------
# (a) happy path.
# ---------------------------------------------------------------------------


def test_happy_path_lands_with_ledger_id(monkeypatch, tmp_path):
    from core import inbound_processing as ip

    rec = _Recorder()
    _patch_seams(
        monkeypatch,
        rec,
        ingest_result={"blocked": False, "ledger": {"id": "mfl_123"}},
    )
    store = _store(tmp_path)
    att = _put(store)

    result = ip.process_inbound_delivery(
        object(), manifest=_manifest([att]), store=store
    )

    assert result["status"] == "landed"
    assert result["receipt_id"] == "inbrx_1"
    assert result["datastream_id"] == "ds-1"
    assert result["import_ledger_id"] == "mfl_123"
    assert result["ignored_attachments"] == []
    # Receipt recorded once, ingest once, states in order.
    assert rec.receipt_recorded == 1
    assert rec.ingested == 1
    assert rec.states == ["PROCESSING", "LANDED"]
    # LANDED carried the ledger id.
    landed = rec.mark_state_calls[-1]
    assert landed["import_ledger_id"] == "mfl_123"


# ---------------------------------------------------------------------------
# (b) denied token.
# ---------------------------------------------------------------------------


def test_denied_token_records_no_receipt(monkeypatch, tmp_path):
    from core import inbound_processing as ip

    rec = _Recorder()
    _patch_seams(monkeypatch, rec, allowed=False)
    store = _store(tmp_path)
    att = _put(store)

    result = ip.process_inbound_delivery(
        object(), manifest=_manifest([att]), store=store
    )

    assert result == {
        "status": "denied",
        "receipt_id": None,
        "datastream_id": None,
        "import_ledger_id": None,
        "ignored_attachments": [],
    }
    # No receipt recorded, no ingest, no state transitions.
    assert rec.receipt_recorded == 0
    assert rec.ingested == 0
    assert rec.states == []


# ---------------------------------------------------------------------------
# (c) duplicate terminal receipt -> no re-ingest.
# ---------------------------------------------------------------------------


def test_duplicate_terminal_receipt_short_circuits(monkeypatch, tmp_path):
    from core import inbound_processing as ip

    rec = _Recorder()
    _patch_seams(
        monkeypatch,
        rec,
        deduplicated=True,
        existing_state="LANDED",
        existing_ledger_id="mfl_prior",
    )
    store = _store(tmp_path)
    att = _put(store)

    result = ip.process_inbound_delivery(
        object(), manifest=_manifest([att]), store=store
    )

    assert result["status"] == "duplicate"
    assert result["receipt_id"] == "inbrx_1"
    assert result["import_ledger_id"] == "mfl_prior"
    # No re-ingest, no PROCESSING transition.
    assert rec.ingested == 0
    assert rec.states == []


def test_duplicate_non_terminal_receipt_continues(monkeypatch, tmp_path):
    """A deduplicated but non-terminal receipt re-drives ingestion (recovery)."""
    from core import inbound_processing as ip

    rec = _Recorder()
    _patch_seams(
        monkeypatch,
        rec,
        deduplicated=True,
        existing_state="RECEIVED",
        ingest_result={"blocked": False, "ledger": {"id": "mfl_9"}},
    )
    store = _store(tmp_path)
    att = _put(store)

    result = ip.process_inbound_delivery(
        object(), manifest=_manifest([att]), store=store
    )

    assert result["status"] == "landed"
    assert rec.ingested == 1
    assert rec.states == ["PROCESSING", "LANDED"]


# ---------------------------------------------------------------------------
# (d) zero data attachments.
# ---------------------------------------------------------------------------


def test_zero_data_attachments_fails(monkeypatch, tmp_path):
    from core import inbound_processing as ip

    rec = _Recorder()
    _patch_seams(monkeypatch, rec)
    store = _store(tmp_path)

    # Only the manifest object -- no data attachment.
    manifest_att = {
        "filename": "_manifest.json",
        "quarantine_uri": "file:///tmp/x/_manifest.json",
        "size": 10,
        "content_type": "application/json",
    }

    result = ip.process_inbound_delivery(
        object(), manifest=_manifest([manifest_att]), store=store
    )

    assert result["status"] == "failed"
    assert rec.ingested == 0
    # PROCESSING then FAILED(no_data_attachment).
    assert rec.states == ["PROCESSING", "FAILED"]
    assert rec.mark_state_calls[-1]["error_code"] == "no_data_attachment"


# ---------------------------------------------------------------------------
# (e) more than one data attachment -> first processed + ignored list.
# ---------------------------------------------------------------------------


def test_multiple_data_attachments_processes_first(monkeypatch, tmp_path):
    from core import inbound_processing as ip

    rec = _Recorder()
    _patch_seams(
        monkeypatch,
        rec,
        ingest_result={"blocked": False, "ledger": {"id": "mfl_a"}},
    )
    store = _store(tmp_path)
    att1 = _put(store, filename="first.csv", message_id="m1")
    att2 = _put(store, filename="second.csv", message_id="m2")

    result = ip.process_inbound_delivery(
        object(), manifest=_manifest([att1, att2]), store=store
    )

    assert result["status"] == "landed"
    assert result["ignored_attachments"] == ["second.csv"]
    assert rec.ingested == 1
    assert rec.states == ["PROCESSING", "LANDED"]


# ---------------------------------------------------------------------------
# (f) ingest blocked -> REJECTED.
# ---------------------------------------------------------------------------


def test_ingest_blocked_marks_rejected(monkeypatch, tmp_path):
    from core import inbound_processing as ip

    rec = _Recorder()
    _patch_seams(
        monkeypatch,
        rec,
        ingest_result={"blocked": True, "reason": "empty_import", "ledger": None},
    )
    store = _store(tmp_path)
    att = _put(store)

    result = ip.process_inbound_delivery(
        object(), manifest=_manifest([att]), store=store
    )

    assert result["status"] == "rejected"
    assert result["import_ledger_id"] is None
    assert rec.states == ["PROCESSING", "REJECTED"]
    assert rec.mark_state_calls[-1]["error_code"] == "empty_import"


# ---------------------------------------------------------------------------
# (g) ingest raises a typed error -> FAILED.
# ---------------------------------------------------------------------------


def test_ingest_typed_error_marks_failed(monkeypatch, tmp_path):
    from core import inbound_processing as ip
    from core.inbound_ingest import DatastreamNotIngestable

    rec = _Recorder()
    _patch_seams(
        monkeypatch,
        rec,
        ingest_error=DatastreamNotIngestable("not configured"),
    )
    store = _store(tmp_path)
    att = _put(store)

    result = ip.process_inbound_delivery(
        object(), manifest=_manifest([att]), store=store
    )

    assert result["status"] == "failed"
    assert rec.states == ["PROCESSING", "FAILED"]
    # error_code is the exception TYPE name (no leaked internals / message).
    assert rec.mark_state_calls[-1]["error_code"] == "DatastreamNotIngestable"


def test_ingest_validation_error_marks_failed(monkeypatch, tmp_path):
    from core import inbound_processing as ip
    from core.inbound_ingest import InboundIngestValidationError

    rec = _Recorder()
    _patch_seams(
        monkeypatch,
        rec,
        ingest_error=InboundIngestValidationError("bad bytes"),
    )
    store = _store(tmp_path)
    att = _put(store)

    result = ip.process_inbound_delivery(
        object(), manifest=_manifest([att]), store=store
    )

    assert result["status"] == "failed"
    assert rec.mark_state_calls[-1]["error_code"] == "InboundIngestValidationError"


# ---------------------------------------------------------------------------
# (h) malformed manifest.
# ---------------------------------------------------------------------------


def test_bad_schema_raises_validation_error(tmp_path):
    from core import inbound_processing as ip

    bad = _manifest([])
    bad["schema"] = "some-other-schema-v9"

    with pytest.raises(ip.InboundProcessingValidationError):
        ip.process_inbound_delivery(object(), manifest=bad, store=_store(tmp_path))


def test_missing_required_key_raises_validation_error(tmp_path):
    from core import inbound_processing as ip

    bad = _manifest([])
    del bad["provider_event_id"]

    with pytest.raises(ip.InboundProcessingValidationError):
        ip.process_inbound_delivery(object(), manifest=bad, store=_store(tmp_path))


def test_non_dict_manifest_raises_validation_error(tmp_path):
    from core import inbound_processing as ip

    with pytest.raises(ip.InboundProcessingValidationError):
        ip.process_inbound_delivery(
            object(), manifest=["not", "a", "dict"], store=_store(tmp_path)
        )


# ---------------------------------------------------------------------------
# quarantine read failure -> FAILED (defence-in-depth).
# ---------------------------------------------------------------------------


def test_quarantine_read_error_marks_failed(monkeypatch, tmp_path):
    from core import inbound_processing as ip

    rec = _Recorder()
    _patch_seams(monkeypatch, rec)
    store = _store(tmp_path)

    # An attachment whose uri points nowhere -> store.get raises QuarantineError.
    att = {
        "filename": "ghost.csv",
        "quarantine_uri": (tmp_path / "does-not-exist.csv").as_uri(),
        "size": 5,
        "content_type": "text/csv",
    }

    result = ip.process_inbound_delivery(
        object(), manifest=_manifest([att]), store=store
    )

    assert result["status"] == "failed"
    assert rec.ingested == 0
    assert rec.states == ["PROCESSING", "FAILED"]
    assert rec.mark_state_calls[-1]["error_code"] == "quarantine_read_error"
