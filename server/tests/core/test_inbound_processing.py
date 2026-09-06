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

import hashlib
import pathlib

import pytest


class _CleanScanner:
    version = "test-clean-scanner-v1"

    def __call__(self, data: bytes) -> tuple[str, str]:
        return "clean", "test-signature-db"


@pytest.fixture(autouse=True)
def _configured_clean_scanner(monkeypatch):
    from core import inbound_scan

    monkeypatch.setattr(
        inbound_scan,
        "configured_malware_scanner",
        lambda **kwargs: _CleanScanner(),
    )


# ---------------------------------------------------------------------------
# Manifest + store helpers.
# ---------------------------------------------------------------------------


def _manifest(
    attachments,
    *,
    provider_event_id="evt-1",
    channel="email",
    retention_days=30,
):
    from core.inbound_receipts import canonical_receipt_fingerprint

    normalized = []
    for ordinal, attachment in enumerate(attachments):
        item = dict(attachment)
        item["ordinal"] = ordinal
        item.setdefault("content_sha256", "0" * 64)
        item.setdefault("size", 0)
        normalized.append(item)
    recipient_hash = "b" * 64
    return {
        "schema": "inbound-delivery-manifest-v1",
        "receipt_id": "inbrx_1",
        "provider_event_id": provider_event_id,
        "channel": channel,
        "token_hash": "a" * 64,
        "recipient_hash": recipient_hash,
        "received_at": "2026-01-01T00:00:00+00:00",
        "retention_policy": {
            "version": "quarantine-retention-v1",
            "days": retention_days,
        },
        "attachments": normalized,
        "receipt_fingerprint": canonical_receipt_fingerprint(
            datastream_id="ds-1",
            credential_id="dic_1",
            channel=channel,
            recipient_hash=recipient_hash,
            attachments=normalized,
        ),
    }


def _store(tmp_path: pathlib.Path):
    from core.inbound_quarantine import LocalFsQuarantineStore

    return LocalFsQuarantineStore(root=str(tmp_path))


def _put(store, *, filename="data.csv", data=b"col\n1\n", partition="p", message_id="m"):
    del partition, message_id
    digest = hashlib.sha256(data).hexdigest()
    obj = store.put(
        partition="org-1/ds-1",
        message_id=digest,
        filename=filename,
        data=data,
        content_type="text/csv",
    )
    return {
        "filename": filename,
        "quarantine_uri": obj.uri,
        "size": obj.size,
        "content_type": "text/csv",
        "content_sha256": digest,
    }


class _Recorder:
    """Captures the sibling-seam calls in order for assertions."""

    def __init__(self):
        self.states: list[str] = []
        self.receipt_recorded = 0
        self.ingested = 0
        self.mark_state_calls: list[dict] = []
        # Story 38.9: per-attachment raw-import evidence.
        self.raw_recorded: list[dict] = []
        self.raw_states: list[dict] = []
        self.ingest_calls: list[dict] = []


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
    ingest_by_filename=None,
    raw_dedup_state=None,
    content_duplicate=False,
    project_id="proj-1",
    credential_channel="email",
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
                    "channel": credential_channel,
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
        rec.ingest_calls.append(kwargs)
        if ingest_by_filename is not None:
            outcome = ingest_by_filename[kwargs.get("filename")]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        if ingest_error is not None:
            raise ingest_error
        return ingest_result or {}

    monkeypatch.setattr(ii, "ingest_inbound_file", _ingest)

    # Story 38.9 raw-import seam: one immutable row per attachment.
    import core.inbound_raw_imports as iri

    def _record_raw(conn, **kwargs):
        rec.raw_recorded.append(kwargs)
        dedup = raw_dedup_state is not None
        return {
            "raw_import_id": f"inbraw_{kwargs['ordinal']}",
            "deduplicated": dedup,
            "state": raw_dedup_state or "RECEIVED",
            "import_ledger_id": existing_ledger_id if dedup else None,
            "error_code": None,
        }

    monkeypatch.setattr(iri, "record_raw_import", _record_raw)
    monkeypatch.setattr(
        iri,
        "duplicate_content_decision",
        lambda *args, **kwargs: {
            "policy_version": "skip-exact-v1",
            "skip_execution": content_duplicate,
            "duplicate_of_raw_import_id": ("inbraw_original" if content_duplicate else None),
        },
    )

    def _mark_raw(conn, *, raw_import_id, datastream_id, state, **kwargs):
        rec.raw_states.append({"raw_import_id": raw_import_id, "state": state, **kwargs})
        return {"raw_import_id": raw_import_id, "state": state}

    monkeypatch.setattr(iri, "mark_raw_import_state", _mark_raw)

    # project_id read: patch the small DB helper directly.
    import core.inbound_processing as ip

    monkeypatch.setattr(ip, "_resolve_project_id", lambda conn, *, datastream_id: project_id)
    monkeypatch.setattr(ip, "_resolve_org_id", lambda conn, *, datastream_id: "org-1")


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

    result = ip.process_inbound_delivery(object(), manifest=_manifest([att]), store=store)

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


def test_upload_email_and_webhook_cross_their_real_processing_entries(monkeypatch):
    """Three transport entries, one governed attachment worker and artifact shape."""
    from core import inbound_processing as ip
    from core.inbound_quarantine import QuarantineObject

    class MemoryStore:
        def __init__(self):
            self.objects = {}

        def put(self, *, data, **kwargs):
            uri = "memory://" + hashlib.sha256(data).hexdigest()
            self.objects[uri] = data
            return QuarantineObject(uri=uri, size=len(data))

        def get(self, uri):
            return self.objects[uri]

        def get_bounded(self, uri, *, max_bytes, **kwargs):
            data = self.objects[uri]
            if len(data) > max_bytes:
                raise AssertionError("test payload exceeds bound")
            return data

    snapshots = {}
    payload = b"col\n1\n"
    for channel in ("upload", "email", "webhook"):
        with monkeypatch.context() as scoped:
            rec = _Recorder()
            _patch_seams(
                scoped,
                rec,
                credential_channel=channel,
                ingest_result={
                    "blocked": False,
                    "ledger": {"id": f"mfl_{channel}"},
                    "dispatch": {"state": "published"},
                },
            )
            store = MemoryStore()
            if channel == "upload":
                result = ip.process_authorized_upload(
                    object(),
                    datastream_id="ds-1",
                    project_id="proj-1",
                    file_bytes=payload,
                    filename="data.csv",
                    media_type="text/csv",
                    actor="member",
                    idempotency_key="same-governed-input",
                    store=store,
                )
            else:
                attachment = _put(store, data=payload)
                result = ip.process_inbound_delivery(
                    object(),
                    manifest=_manifest(
                        [attachment],
                        channel=channel,
                        provider_event_id=f"evt-{channel}",
                    ),
                    store=store,
                )
            snapshots[channel] = {
                "status": result["status"],
                "ingest_channel": rec.ingest_calls[0]["channel"],
                "bytes_hash": rec.raw_recorded[0]["content_hash"],
                "raw_states": [item["state"] for item in rec.raw_states],
                "receipt_terminal": rec.states[-1],
                "project_id": rec.ingest_calls[0]["project_id"],
                "datastream_id": rec.ingest_calls[0]["datastream_id"],
            }

    common = {
        key: value
        for key, value in snapshots["upload"].items()
        if key != "ingest_channel"
    }
    assert {item["ingest_channel"] for item in snapshots.values()} == {
        "upload",
        "email",
        "webhook",
    }
    assert all(
        {key: value for key, value in item.items() if key != "ingest_channel"}
        == common
        for item in snapshots.values()
    )


# ---------------------------------------------------------------------------
# (b) denied token.
# ---------------------------------------------------------------------------


def test_denied_token_records_no_receipt(monkeypatch, tmp_path):
    from core import inbound_processing as ip

    rec = _Recorder()
    _patch_seams(monkeypatch, rec, allowed=False)
    store = _store(tmp_path)
    att = _put(store)

    result = ip.process_inbound_delivery(object(), manifest=_manifest([att]), store=store)

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

    result = ip.process_inbound_delivery(object(), manifest=_manifest([att]), store=store)

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

    result = ip.process_inbound_delivery(object(), manifest=_manifest([att]), store=store)

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
    result = ip.process_inbound_delivery(object(), manifest=_manifest([]), store=_store(tmp_path))

    assert result["status"] == "failed"
    assert rec.ingested == 0
    assert rec.states == ["PROCESSING", "FAILED"]
    assert rec.mark_state_calls[-1]["error_code"] == "no_data_attachment"


def test_attachment_named_manifest_json_is_processed(monkeypatch, tmp_path):
    from core import inbound_processing as ip

    rec = _Recorder()
    _patch_seams(
        monkeypatch,
        rec,
        ingest_result={"blocked": False, "ledger": {"id": "mfl_manifest_name"}},
    )
    store = _store(tmp_path)
    attachment = _put(store, filename="_manifest.json", data=b'{"row":1}')

    result = ip.process_inbound_delivery(object(), manifest=_manifest([attachment]), store=store)

    assert result["status"] == "landed"
    assert rec.ingest_calls[0]["filename"] == "_manifest.json"


def test_every_data_attachment_gets_its_own_raw_import(monkeypatch, tmp_path):
    from core import inbound_processing as ip

    rec = _Recorder()
    _patch_seams(
        monkeypatch,
        rec,
        ingest_by_filename={
            "first.csv": {"blocked": False, "ledger": {"id": "mfl_a"}},
            "second.csv": {"blocked": False, "ledger": {"id": "mfl_b"}},
        },
    )
    store = _store(tmp_path)
    att1 = _put(store, filename="first.csv", message_id="m1", data=b"col\n1\n")
    att2 = _put(store, filename="second.csv", message_id="m2", data=b"col\n2\n")

    result = ip.process_inbound_delivery(object(), manifest=_manifest([att1, att2]), store=store)

    assert result["status"] == "landed"
    # Nothing is dropped any more, and the key survives empty for old callers.
    assert result["ignored_attachments"] == []
    # TWO files in, TWO executions, TWO raw imports, TWO ledger rows.
    assert rec.ingested == 2
    assert len(rec.raw_recorded) == 2
    assert [o["status"] for o in result["attachments"]] == ["landed", "landed"]
    assert [o["import_ledger_id"] for o in result["attachments"]] == [
        "mfl_a",
        "mfl_b",
    ]
    # Ordinals are the manifest positions, and they are what the raw imports key on.
    assert [r["ordinal"] for r in rec.raw_recorded] == [0, 1]
    # Each attachment carries the hash of ITS OWN bytes, not the delivery's.
    assert result["attachments"][0]["content_hash"] != (result["attachments"][1]["content_hash"])


def test_identical_attachments_share_a_content_hash_but_not_a_row(monkeypatch, tmp_path):
    """Two files, same bytes: one content ADDRESS, two independent records.

    This is the shape Story 38.9 AC4 reasons about. The hash is a function of
    the bytes alone, so identical payloads collide BY DESIGN -- that collision
    is what makes duplicate detection possible. What must NOT collide is the
    evidence: each arrival keeps its own row, its own ordinal and its own
    outcome, so the receipt-level audit trail survives whatever the Datastream's
    duplicate policy later decides to do.
    """
    from core import inbound_processing as ip

    rec = _Recorder()
    _patch_seams(
        monkeypatch,
        rec,
        ingest_result={"blocked": False, "ledger": {"id": "mfl_x"}},
    )
    store = _store(tmp_path)
    same = b"col\nidentical\n"
    att1 = _put(store, filename="a.csv", message_id="m1", data=same)
    att2 = _put(store, filename="b.csv", message_id="m2", data=same)

    result = ip.process_inbound_delivery(object(), manifest=_manifest([att1, att2]), store=store)

    hashes = [o["content_hash"] for o in result["attachments"]]
    assert hashes[0] == hashes[1]
    # ... and yet two distinct records were kept.
    assert len(rec.raw_recorded) == 2
    assert [r["ordinal"] for r in rec.raw_recorded] == [0, 1]


def test_each_attachment_gets_a_distinct_import_idempotency_key(monkeypatch, tmp_path):
    """The per-file message_id is what keeps N files from collapsing into 1.

    ``run_import`` is idempotent on ``message_id``. If every attachment of one
    delivery were driven with the bare provider_event_id, attachments 2..N would
    REPLAY attachment 1's import instead of creating their own -- two files in,
    one import out, silently. This is the assertion that would catch that.
    """
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

    ip.process_inbound_delivery(
        object(),
        manifest=_manifest([att1, att2], provider_event_id="evt-77"),
        store=store,
    )

    keys = [c["message_id"] for c in rec.ingest_calls]
    assert keys == ["evt-77#0", "evt-77#1"]
    assert len(set(keys)) == 2


def test_one_rejected_attachment_does_not_suppress_a_valid_sibling(monkeypatch, tmp_path):
    """38.9 AC1 / 38.6 AC: outcomes are independent, in both directions."""
    from core import inbound_processing as ip

    rec = _Recorder()
    _patch_seams(
        monkeypatch,
        rec,
        ingest_by_filename={
            "bad.csv": {"blocked": True, "reason": "empty_import_blocked"},
            "good.csv": {"blocked": False, "ledger": {"id": "mfl_ok"}},
        },
    )
    store = _store(tmp_path)
    bad = _put(store, filename="bad.csv", message_id="m1")
    good = _put(store, filename="good.csv", message_id="m2", data=b"col\n2\n")

    result = ip.process_inbound_delivery(object(), manifest=_manifest([bad, good]), store=store)

    outcomes = {o["filename"]: o for o in result["attachments"]}
    assert outcomes["bad.csv"]["status"] == "rejected"
    assert outcomes["bad.csv"]["error_code"] == "empty_import_blocked"
    # The valid sibling still landed -- the rejection did not suppress it.
    assert outcomes["good.csv"]["status"] == "landed"
    assert outcomes["good.csv"]["import_ledger_id"] == "mfl_ok"
    # Per-file states are terminal and independent. Both files also pass through
    # SCANNING/ACCEPTED now that Story 38.10's gate runs, so filter to the
    # terminal ones rather than pinning the whole walk here -- the walk itself
    # is asserted in test_inbound_scan.py.
    terminal = {"LANDED", "REJECTED", "FAILED"}
    assert sorted(s["state"] for s in rec.raw_states if s["state"] in terminal) == [
        "LANDED",
        "REJECTED",
    ]
    # The delivery landed (data IS queryable) but says so without hiding the loss.
    assert result["status"] == "landed"
    partial = rec.mark_state_calls[-1]
    assert partial["error_code"] == "partial_attachment_failure"


def test_all_attachments_failing_leaves_the_receipt_failed(monkeypatch, tmp_path):
    """No landed sibling => the delivery must not report success."""
    from core import inbound_processing as ip

    rec = _Recorder()
    _patch_seams(
        monkeypatch,
        rec,
        ingest_by_filename={
            "a.csv": {"blocked": True, "reason": "empty_import_blocked"},
            "b.csv": {"blocked": True, "reason": "empty_import_blocked"},
        },
    )
    store = _store(tmp_path)
    a = _put(store, filename="a.csv", message_id="m1")
    b = _put(store, filename="b.csv", message_id="m2", data=b"col\n3\n")

    result = ip.process_inbound_delivery(object(), manifest=_manifest([a, b]), store=store)

    assert result["status"] == "rejected"
    assert result["import_ledger_id"] is None
    assert rec.states == ["PROCESSING", "REJECTED"]


def test_unreadable_attachment_does_not_abort_its_siblings(monkeypatch, tmp_path):
    """A store read failure is one file's outcome, not the delivery's."""
    from core import inbound_processing as ip

    rec = _Recorder()
    _patch_seams(
        monkeypatch,
        rec,
        ingest_result={"blocked": False, "ledger": {"id": "mfl_ok"}},
    )
    store = _store(tmp_path)
    good = _put(store, filename="good.csv", message_id="m1")
    missing = {
        "filename": "gone.csv",
        "quarantine_uri": "file:///nowhere/does-not-exist.csv",
        "size": 10,
        "content_type": "text/csv",
    }

    result = ip.process_inbound_delivery(object(), manifest=_manifest([missing, good]), store=store)

    outcomes = {o["filename"]: o for o in result["attachments"]}
    assert outcomes["gone.csv"]["status"] == "failed"
    assert outcomes["gone.csv"]["error_code"] == "quarantine_read_error"
    # Manifest evidence created before HTTP 202 survives an unreadable object.
    assert outcomes["gone.csv"]["raw_import_id"] == "inbraw_0"
    # The readable sibling was still processed.
    assert outcomes["good.csv"]["status"] == "landed"
    assert rec.ingested == 1


def test_unexpected_attachment_failure_isolated_and_sibling_continues(monkeypatch, tmp_path):
    from core import inbound_processing as ip

    rec = _Recorder()
    _patch_seams(
        monkeypatch,
        rec,
        ingest_by_filename={
            "broken.csv": RuntimeError("unexpected parser crash"),
            "good.csv": {"blocked": False, "ledger": {"id": "mfl_good"}},
        },
    )
    store = _store(tmp_path)
    broken = _put(store, filename="broken.csv", data=b"a\n1\n")
    good = _put(store, filename="good.csv", data=b"a\n2\n")

    result = ip.process_inbound_delivery(object(), manifest=_manifest([broken, good]), store=store)

    assert result["status"] == "landed"
    assert [item["status"] for item in result["attachments"]] == ["failed", "landed"]
    assert any(item["state"] == "FAILED" for item in rec.raw_states)
    assert rec.ingested == 2


def test_the_scan_gate_actually_runs_in_the_worker(monkeypatch, tmp_path):
    """Story 38.10 wired, not merely written.

    A scanner that exists and is never called is the defect class this repo
    keeps removing. The proof is behavioural: deliver a real zip bomb beside a
    valid file, and check the parser was offered ONE of them.
    """
    import io
    import zipfile

    from core import inbound_processing as ip

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("bomb.csv", b"0" * (8 * 1024 * 1024))
    bomb = buf.getvalue()

    rec = _Recorder()
    _patch_seams(
        monkeypatch,
        rec,
        ingest_result={"blocked": False, "ledger": {"id": "mfl_ok"}},
    )
    store = _store(tmp_path)
    bad = _put(store, filename="bomb.zip", message_id="m1", data=bomb)
    good = _put(store, filename="fine.csv", message_id="m2", data=b"a,b\n1,2\n")

    result = ip.process_inbound_delivery(object(), manifest=_manifest([bad, good]), store=store)

    outcomes = {o["filename"]: o for o in result["attachments"]}
    assert outcomes["bomb.zip"]["status"] == "rejected"
    assert outcomes["bomb.zip"]["error_code"] in (
        "archive_compression_ratio_exceeded",
        "archive_uncompressed_too_large",
        "declared_type_mismatch",
    )
    # The bomb never reached a parser; its valid sibling did.
    assert rec.ingested == 1
    assert outcomes["fine.csv"]["status"] == "landed"


def test_terminal_attachment_is_not_reprocessed_on_redelivery(monkeypatch, tmp_path):
    """Idempotency holds at the FILE grain, not only at the delivery grain."""
    from core import inbound_processing as ip

    rec = _Recorder()
    _patch_seams(
        monkeypatch,
        rec,
        # The receipt dedups but is NOT terminal (recovery path), while the
        # attachment itself already reached LANDED on the first delivery.
        deduplicated=True,
        existing_state="PROCESSING",
        existing_ledger_id="mfl_prior",
        raw_dedup_state="LANDED",
        ingest_result={"blocked": False, "ledger": {"id": "mfl_new"}},
    )
    store = _store(tmp_path)
    att = _put(store)

    result = ip.process_inbound_delivery(object(), manifest=_manifest([att]), store=store)

    assert result["attachments"][0]["status"] == "duplicate"
    assert result["attachments"][0]["import_ledger_id"] == "mfl_prior"
    # The settled file was NOT driven through the pipeline a second time.
    assert rec.ingested == 0


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

    result = ip.process_inbound_delivery(object(), manifest=_manifest([att]), store=store)

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

    result = ip.process_inbound_delivery(object(), manifest=_manifest([att]), store=store)

    assert result["status"] == "failed"
    assert rec.states == ["PROCESSING", "FAILED"]
    # error_code is the exception TYPE name (no leaked internals / message).
    assert rec.mark_state_calls[-1]["error_code"] == "DatastreamNotIngestable"


def test_parser_error_code_reaches_raw_import_and_operator(monkeypatch, tmp_path):
    import json

    from core import inbound_processing as ip
    from core.csv_excel_import import ParserIssue, ParserReviewRequired

    rec = _Recorder()
    _patch_seams(
        monkeypatch,
        rec,
        ingest_error=ParserReviewRequired(
            [ParserIssue("date_locale_ambiguous", "day<\nscript>")]
        ),
    )
    store = _store(tmp_path)
    result = ip.process_inbound_delivery(object(), manifest=_manifest([_put(store)]), store=store)

    assert result["status"] == "failed"
    assert result["attachments"][0]["error_code"] == "parser_review_required"
    transition = rec.raw_states[-1]
    assert transition["error_code"] == "parser_review_required"
    detail = json.loads(transition["error_detail"])
    assert detail["schema"] == "parser-review-v1"
    assert detail["issues"] == [
        {"code": "date_locale_ambiguous", "field": "day<script>"}
    ]


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

    result = ip.process_inbound_delivery(object(), manifest=_manifest([att]), store=store)

    assert result["status"] == "failed"
    assert rec.mark_state_calls[-1]["error_code"] == "InboundIngestValidationError"


def test_non_default_retention_policy_reaches_raw_import_operation(monkeypatch, tmp_path):
    from core import inbound_processing as ip

    rec = _Recorder()
    _patch_seams(
        monkeypatch,
        rec,
        ingest_result={"blocked": False, "ledger": {"id": "mfl_retention"}},
    )
    store = _store(tmp_path)
    result = ip.process_inbound_delivery(
        object(), manifest=_manifest([_put(store)], retention_days=45), store=store
    )

    assert result["status"] == "landed"
    assert rec.raw_recorded[0]["retention_policy_version"] == ("quarantine-retention-v1")
    assert rec.raw_recorded[0]["retention_days"] == 45


def test_same_bytes_do_not_short_circuit_before_governed_versions(monkeypatch, tmp_path):
    from core import inbound_processing as ip

    rec = _Recorder()
    _patch_seams(
        monkeypatch,
        rec,
        content_duplicate=True,
        ingest_result={"blocked": False, "ledger": {"id": "mfl_version_aware"}},
    )
    store = _store(tmp_path)
    attachment = _put(store, data=b"same bytes")

    result = ip.process_inbound_delivery(object(), manifest=_manifest([attachment]), store=store)

    assert result["status"] == "landed"
    assert rec.ingested == 1
    assert rec.raw_recorded[0]["content_hash"] == attachment["content_sha256"]
    assert all(item["state"] != "DUPLICATE" for item in rec.raw_states)


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


def test_manifest_rejects_noncontiguous_attachment_ordinal(tmp_path):
    from core import inbound_processing as ip

    store = _store(tmp_path)
    attachment = _put(store)
    manifest = _manifest([attachment])
    manifest["attachments"][0]["ordinal"] = 7
    with pytest.raises(ip.InboundProcessingValidationError, match="ordinal"):
        ip.process_inbound_delivery(object(), manifest=manifest, store=store)


def test_manifest_rejects_forged_receipt_fingerprint(monkeypatch, tmp_path):
    from core import inbound_processing as ip

    _patch_seams(monkeypatch, _Recorder())
    store = _store(tmp_path)
    manifest = _manifest([_put(store)])
    manifest["receipt_fingerprint"] = "f" * 64
    with pytest.raises(ip.InboundProcessingValidationError, match="fingerprint"):
        ip.process_inbound_delivery(object(), manifest=manifest, store=store)


def test_non_dict_manifest_raises_validation_error(tmp_path):
    from core import inbound_processing as ip

    with pytest.raises(ip.InboundProcessingValidationError):
        ip.process_inbound_delivery(object(), manifest=["not", "a", "dict"], store=_store(tmp_path))


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

    result = ip.process_inbound_delivery(object(), manifest=_manifest([att]), store=store)

    assert result["status"] == "failed"
    assert rec.ingested == 0
    assert rec.states == ["PROCESSING", "FAILED"]
    assert rec.mark_state_calls[-1]["error_code"] == "quarantine_read_error"


# ---------------------------------------------------------------------------
# AI-113: the first delivery on a DRAFT is retained for review, not failed.
# ---------------------------------------------------------------------------


def test_a_draft_datastream_retains_its_first_delivery_instead_of_failing(monkeypatch, tmp_path):
    """The journey stops being inverted, proved in the worker.

    Before this, `ingest_inbound_file` refused any delivery whose Datastream had
    no locked plan -- so the first emailed file landed FAILED and a person had
    to upload one by hand first. Now a draft's file is ACCEPTED and describable,
    which is what the wizard needs to show its shape.
    """
    import core.inbound_discovery as idisc
    from core import inbound_processing as ip

    rec = _Recorder()
    _patch_seams(monkeypatch, rec)
    monkeypatch.setattr(idisc, "delivery_posture", lambda conn, **kw: idisc.POSTURE_DISCOVERY)

    store = _store(tmp_path)
    att = _put(store, filename="first.csv", data=b"a,b\n1,2\n")

    result = ip.process_inbound_delivery(object(), manifest=_manifest([att]), store=store)

    assert result["status"] == "observed"
    assert result["attachments"][0]["status"] == "observed"
    # The bytes are kept and marked usable, not thrown away as a failure.
    assert [s["state"] for s in rec.raw_states] == ["SCANNING", "ACCEPTED"]
    # And nothing was pushed at a pipeline that has nothing to publish into.
    assert rec.ingested == 0
    # The receipt is NOT marked failed: the delivery arrived intact and is
    # waiting for a human, which is not the same thing as an error.
    assert "FAILED" not in rec.states


def test_an_active_datastream_still_ingests_and_still_fails_loudly(monkeypatch, tmp_path):
    """The guard is bypassed only where there is nothing to publish against.

    An ACTIVE Datastream missing its plan is a real misconfiguration; routing it
    to the discovery path would replace an actionable failure with a silent
    description.
    """
    import core.inbound_discovery as idisc
    from core import inbound_processing as ip
    from core.inbound_ingest import DatastreamNotIngestable

    rec = _Recorder()
    _patch_seams(
        monkeypatch,
        rec,
        ingest_error=DatastreamNotIngestable("not fully configured"),
    )
    monkeypatch.setattr(idisc, "delivery_posture", lambda conn, **kw: idisc.POSTURE_INGEST)

    store = _store(tmp_path)
    att = _put(store, filename="x.csv", data=b"a,b\n1,2\n")

    result = ip.process_inbound_delivery(object(), manifest=_manifest([att]), store=store)

    assert result["status"] == "failed"
    assert result["attachments"][0]["error_code"] == "DatastreamNotIngestable"
    assert rec.ingested == 1


def test_manifest_declared_oversize_is_rejected_without_object_read(monkeypatch):
    from core import inbound_processing as ip
    from core.inbound_scan import DEFAULT_MAX_BYTES

    rec = _Recorder()
    _patch_seams(monkeypatch, rec)

    class NeverRead:
        def get_bounded(self, *args, **kwargs):
            raise AssertionError("oversized manifest must be refused before storage read")

    attachment = {
        "filename": "huge.csv",
        "quarantine_uri": "file:///never-read",
        "size": DEFAULT_MAX_BYTES + 1,
        "content_type": "text/csv",
        "content_sha256": "1" * 64,
    }
    result = ip.process_inbound_delivery(
        object(), manifest=_manifest([attachment]), store=NeverRead()
    )
    assert result["status"] == "rejected"
    assert result["attachments"][0]["error_code"] == "file_too_large"
    assert [item["state"] for item in rec.raw_states] == ["SCANNING", "REJECTED"]


def test_actual_oversize_behind_a_small_manifest_is_a_terminal_size_rejection(monkeypatch):
    from core import inbound_processing as ip
    from core.inbound_quarantine import QuarantineSizeError
    from core.inbound_scan import DEFAULT_MAX_BYTES

    rec = _Recorder()
    _patch_seams(monkeypatch, rec)

    class LyingStore:
        def get_bounded(self, *args, **kwargs):
            raise QuarantineSizeError(
                "too large",
                observed_size=DEFAULT_MAX_BYTES + 100,
                max_bytes=DEFAULT_MAX_BYTES,
            )

    attachment = {
        "filename": "lied.csv",
        "quarantine_uri": "file:///bounded-read",
        "size": 10,
        "content_type": "text/csv",
        "content_sha256": "2" * 64,
    }
    result = ip.process_inbound_delivery(
        object(), manifest=_manifest([attachment]), store=LyingStore()
    )
    assert result["attachments"][0]["status"] == "rejected"
    assert result["attachments"][0]["error_code"] == "file_too_large"
    verdict = rec.raw_states[-1]["scan_verdict"]
    assert verdict["evidence"]["size_bytes"] == DEFAULT_MAX_BYTES + 100



# ---------------------------------------------------------------------------
# Story 57.3 -- the DECLARED sender allowlist reaches durable evidence, and its
# refusal lands as a named rejection.
#
# The REFUSAL itself is not here: it lives in `ingest_inbound_file`, the point
# this path and the replay path (`core.inbound_reprocess`) share, and it is
# proved in `test_inbound_sender_policy.py`. What this file owns is the two ends
# of THIS path -- that the sender reaches the receipt, so a replay has something
# to read, and that the refusal comes back named rather than as a generic fault.
# ---------------------------------------------------------------------------


def test_the_sender_digests_reach_the_durable_receipt(monkeypatch, tmp_path):
    """A replay never sees a manifest. If the receipt does not carry the sender,
    nothing downstream can ever tell who sent the bytes it is re-importing."""
    import core.inbound_receipts as ir
    from core import inbound_processing as ip

    rec = _Recorder()
    _patch_seams(monkeypatch, rec, ingest_result={"blocked": False, "ledger": {"id": "mfl_1"}})
    recorded: list[dict] = []
    original = ir.record_receipt

    def _capture(conn, **kwargs):
        recorded.append(kwargs)
        return original(conn, **kwargs)

    monkeypatch.setattr(ir, "record_receipt", _capture)
    store = _store(tmp_path)
    manifest = {
        **_manifest([_put(store)]),
        "sender_hash": "c" * 64,
        "sender_domain_hash": "d" * 64,
    }

    ip.process_inbound_delivery(object(), manifest=manifest, store=store)

    assert recorded[-1]["sender_hash"] == "c" * 64
    assert recorded[-1]["sender_domain_hash"] == "d" * 64


def test_a_delivery_with_no_sender_records_none_rather_than_a_blank(monkeypatch, tmp_path):
    """A webhook carries no sender. `None` reads as unknown; "" would read as one."""
    import core.inbound_receipts as ir
    from core import inbound_processing as ip

    rec = _Recorder()
    _patch_seams(monkeypatch, rec, ingest_result={"blocked": False, "ledger": {"id": "mfl_1"}})
    recorded: list[dict] = []
    original = ir.record_receipt
    monkeypatch.setattr(
        ir,
        "record_receipt",
        lambda conn, **kwargs: (recorded.append(kwargs), original(conn, **kwargs))[1],
    )
    store = _store(tmp_path)

    ip.process_inbound_delivery(object(), manifest=_manifest([_put(store)]), store=store)

    assert recorded[-1]["sender_hash"] is None
    assert recorded[-1]["sender_domain_hash"] is None


def test_a_sender_refused_at_the_join_lands_as_a_named_rejection(monkeypatch, tmp_path):
    """The refusal comes back with ITS OWN code, not the generic ingest fault.

    "DatastreamNotIngestable" on a receipt sends the owner to the Datastream's
    configuration; `sender_not_allowed` sends them to who sent the file.
    """
    from core import inbound_processing as ip
    from core.inbound_ingest import SenderNotAllowed

    rec = _Recorder()
    _patch_seams(
        monkeypatch,
        rec,
        ingest_error=SenderNotAllowed("the sender of this delivery is not in the allowlist"),
    )
    store = _store(tmp_path)

    result = ip.process_inbound_delivery(object(), manifest=_manifest([_put(store)]), store=store)

    assert result["status"] == "failed"
    assert result["attachments"][0]["error_code"] == "sender_not_allowed"
    assert result["import_ledger_id"] is None
    # No address anywhere in what came back: the refusal is the answer.
    assert "@" not in str(result)


def test_a_typed_refusal_keeps_its_sentence_on_the_raw_import():
    """AI-321 (2026-08-29): `DatastreamNotIngestable` was persisted as its class name
    and a NULL detail; the sentence that names the gesture is now kept, bounded."""
    from core.inbound_ingest import DatastreamNotIngestable
    from core.inbound_processing import _parser_review_error_detail

    detail = _parser_review_error_detail(
        DatastreamNotIngestable(
            "the pinned plan/mapping bundle has incomplete fingerprint evidence"
        )
    )
    assert detail is not None
    assert "incomplete fingerprint evidence" in detail
    assert len(_parser_review_error_detail(DatastreamNotIngestable("x" * 5000)) or "") <= 520
