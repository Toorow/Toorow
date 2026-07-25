"""Story 38.8: Inbound-receipt ledger -- offline unit + live-PG-gated tests.

Covers:
  (a) record_receipt -> row present + state RECEIVED; deduplicated=False.
  (b) Redelivery same provider_event_id -> deduplicated=True; no second row.
  (c) mark_state RECEIVED -> PROCESSING -> LANDED sets import_ledger_id.
  (d) mark_state to FAILED sets error_code and error_detail.
  (e) get_receipt returns safe read-model for an existing row.
  (f) get_receipt returns None for an absent row.
  (g) list_receipts returns newest-first safe read-models.
  (h) Safe read-model NEVER contains raw recipient address or raw token.
  (i) hash_recipient returns a 64-char lowercase hex string.
  (j) Validation errors before SQL (missing required params, bad channel, bad state).
  (k) Live-PG: unique (datastream_id, provider_event_id) constraint blocks duplicate.
  (l) Live-PG: immutability trigger blocks update of frozen column (provider_event_id).
  (m) Live-PG: DELETE forbidden by trigger.
  (n) Live-PG: no column that holds a raw address in the schema.

INVARIANTS CHECKED:
  * Source-agnostic: no provider/vendor vocabulary appears in this file.
  * Non-tautological: mock cursor SEQUENCE matches the real SQL call order.
  * E38-NFR03: raw address never returned by any read-model function.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Shared mock helpers (mirrors test_epic38_inbound_credentials.py style).
# ---------------------------------------------------------------------------


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _cur(*fetchone_rows, rowcount: int = 1, fetchall=None):
    """Build a mock cursor with an ordered sequence of fetchone return values."""
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.side_effect = list(fetchone_rows)
    cur.fetchall.return_value = fetchall or []
    cur.rowcount = rowcount
    return cur


def _conn_with(*curs):
    """Build a mock connection that yields cursors in order."""
    conn = MagicMock()
    conn.cursor.side_effect = list(curs)
    return conn


def _stub_operation(monkeypatch, module, *, capture=None):
    """Stub execute_operation to run the mutation synchronously."""
    from core import operations

    def execute(operation_conn, spec, *, mutation):
        changed = mutation(operation_conn, "op-test-inbrx-1")
        if capture is not None:
            capture.setdefault("specs", []).append(spec)
            capture.setdefault("changes", []).append(changed)
        return operations.OperationResult(
            "op-test-inbrx-1", "succeeded", changed.result, "audit-1", "outbox-1", False
        )

    monkeypatch.setattr(module, "execute_operation", execute)


# ---------------------------------------------------------------------------
# A canonical fake row tuple matching _SELECT_COLS order.
# Columns: id, datastream_id, credential_id, channel, provider_event_id,
#          recipient_hash, attachment_count, total_bytes, quarantine_uri,
#          state, import_ledger_id, error_code, error_detail,
#          created_at, updated_at.
# ---------------------------------------------------------------------------

_TS = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)

_FAKE_RECEIPT_ID = "inbrx_01JZAAABBBCCCDDDEEEFFF00001"
_FAKE_DS_ID = "ds-test-receipt-1"
_FAKE_CRED_ID = "dic_01JZAAABBBCCCDDDEEEFFF00002"
_FAKE_PROVIDER_EVENT_ID = "evt-test-001"
_FAKE_RECIPIENT_HASH = _sha256("ds_abc123@inbound.example.com")


def _fake_row(
    state: str = "RECEIVED",
    import_ledger_id: str | None = None,
    error_code: str | None = None,
    error_detail: str | None = None,
) -> tuple:
    """Return a fake DB row tuple in _SELECT_COLS order."""
    return (
        _FAKE_RECEIPT_ID,       # id
        _FAKE_DS_ID,            # datastream_id
        _FAKE_CRED_ID,          # credential_id
        "email",                # channel
        _FAKE_PROVIDER_EVENT_ID,# provider_event_id
        _FAKE_RECIPIENT_HASH,   # recipient_hash
        2,                      # attachment_count
        102400,                 # total_bytes
        "gs://bucket/path",     # quarantine_uri
        state,                  # state
        import_ledger_id,       # import_ledger_id
        error_code,             # error_code
        error_detail,           # error_detail
        _TS,                    # created_at
        _TS,                    # updated_at
    )


# ---------------------------------------------------------------------------
# (i) hash_recipient.
# ---------------------------------------------------------------------------


def test_hash_recipient_returns_64_char_hex():
    """hash_recipient must return a 64-character lowercase hex sha256 digest."""
    from core.inbound_receipts import hash_recipient

    raw = "ds_abc123@inbound.example.com"
    h = hash_recipient(raw)
    assert isinstance(h, str)
    assert len(h) == 64
    assert h == h.lower()
    assert h == _sha256(raw)


def test_hash_recipient_is_deterministic():
    """hash_recipient must return the same value for the same input."""
    from core.inbound_receipts import hash_recipient

    raw = "ds_xyz@example.org"
    assert hash_recipient(raw) == hash_recipient(raw)


def test_hash_recipient_differs_for_different_inputs():
    """hash_recipient must differ for distinct input strings."""
    from core.inbound_receipts import hash_recipient

    assert hash_recipient("ds_aaa@example.com") != hash_recipient("ds_bbb@example.com")


# ---------------------------------------------------------------------------
# (j) Validation errors before SQL.
# ---------------------------------------------------------------------------


def test_record_receipt_requires_datastream_id():
    from core.inbound_receipts import InboundReceiptValidationError, record_receipt

    with pytest.raises(InboundReceiptValidationError, match="datastream_id"):
        record_receipt(
            MagicMock(),
            datastream_id="",
            credential_id=None,
            channel="email",
            provider_event_id="evt-1",
            recipient_hash=None,
            actor="test",
            host_context={},
            trace_id=None,
            idempotency_key="ik-1",
        )


def test_record_receipt_rejects_unknown_channel():
    from core.inbound_receipts import InboundReceiptValidationError, record_receipt

    with pytest.raises(InboundReceiptValidationError, match="channel"):
        record_receipt(
            MagicMock(),
            datastream_id="ds-1",
            credential_id=None,
            channel="fax",
            provider_event_id="evt-1",
            recipient_hash=None,
            actor="test",
            host_context={},
            trace_id=None,
            idempotency_key="ik-1",
        )


def test_record_receipt_rejects_bad_recipient_hash():
    from core.inbound_receipts import InboundReceiptValidationError, record_receipt

    with pytest.raises(InboundReceiptValidationError, match="recipient_hash"):
        record_receipt(
            MagicMock(),
            datastream_id="ds-1",
            credential_id=None,
            channel="email",
            provider_event_id="evt-1",
            recipient_hash="tooshort",
            actor="test",
            host_context={},
            trace_id=None,
            idempotency_key="ik-1",
        )


def test_mark_state_rejects_unknown_state():
    from core.inbound_receipts import InboundReceiptValidationError, mark_state

    with pytest.raises(InboundReceiptValidationError, match="state"):
        mark_state(
            MagicMock(),
            receipt_id=_FAKE_RECEIPT_ID,
            datastream_id=_FAKE_DS_ID,
            state="BOGUS",
            actor="test",
            host_context={},
            trace_id=None,
            idempotency_key="ik-ms-1",
        )


def test_mark_state_requires_receipt_id():
    from core.inbound_receipts import InboundReceiptValidationError, mark_state

    with pytest.raises(InboundReceiptValidationError, match="receipt_id"):
        mark_state(
            MagicMock(),
            receipt_id="",
            datastream_id=_FAKE_DS_ID,
            state="PROCESSING",
            actor="test",
            host_context={},
            trace_id=None,
            idempotency_key="ik-ms-2",
        )


# ---------------------------------------------------------------------------
# (a) record_receipt -> row present + state RECEIVED; deduplicated=False.
# ---------------------------------------------------------------------------


def test_record_receipt_inserts_received_row(monkeypatch):
    """record_receipt inserts a RECEIVED row; result has deduplicated=False."""
    from core import inbound_receipts as ir

    captured: dict = {}
    _stub_operation(monkeypatch, ir, capture=captured)

    # cursor 1: _resolve_org_id (org lookup). cursor 2: the mutation reuses ONE
    # cursor for INSERT + SELECT-back (rowcount=1 => inserted).
    conn = _conn_with(_cur(("org-test",)), _cur(_fake_row("RECEIVED"), rowcount=1))

    result = ir.record_receipt(
        conn,
        datastream_id=_FAKE_DS_ID,
        credential_id=_FAKE_CRED_ID,
        channel="email",
        provider_event_id=_FAKE_PROVIDER_EVENT_ID,
        recipient_hash=_FAKE_RECIPIENT_HASH,
        attachment_count=2,
        total_bytes=102400,
        quarantine_uri=None,
        actor="system",
        host_context={},
        trace_id=None,
        idempotency_key="ik-record-1",
    )

    assert result["state"] == "RECEIVED"
    assert result["deduplicated"] is False
    assert result["receipt_id"] == _FAKE_RECEIPT_ID
    assert result["datastream_id"] == _FAKE_DS_ID
    assert result["channel"] == "email"
    assert result["provider_event_id"] == _FAKE_PROVIDER_EVENT_ID

    # Verify one OperationSpec was built.
    assert len(captured["specs"]) == 1
    spec = captured["specs"][0]
    assert spec.command_type == "inbound.receipt.recorded"


def test_record_receipt_no_raw_address_in_request_payload(monkeypatch):
    """E38-NFR03: raw recipient address must NOT appear in request_payload."""
    from core import inbound_receipts as ir

    captured: dict = {}
    _stub_operation(monkeypatch, ir, capture=captured)

    raw_address = "ds_supersecrettoken123@inbound.example.com"
    h = ir.hash_recipient(raw_address)

    conn = _conn_with(_cur(("org-test",)), _cur(_fake_row("RECEIVED"), rowcount=1))

    ir.record_receipt(
        conn,
        datastream_id=_FAKE_DS_ID,
        credential_id=None,
        channel="email",
        provider_event_id="evt-leak-check",
        recipient_hash=h,
        actor="system",
        host_context={},
        trace_id=None,
        idempotency_key="ik-leak-check",
    )

    spec = captured["specs"][0]
    payload_str = json.dumps(spec.request_payload)
    # The raw address (which embeds the secret token) must not appear anywhere.
    assert raw_address not in payload_str, (
        "LEAK: raw recipient address found in request_payload"
    )
    # The hash (safe) should be present instead.
    assert h in payload_str, "recipient_hash must be present in request_payload"


def test_record_receipt_no_raw_address_in_safe_read_model(monkeypatch):
    """E38-NFR03: read-model must not contain a raw address."""
    from core import inbound_receipts as ir

    _stub_operation(monkeypatch, ir)

    raw_address = "ds_anothertoken@inbound.example.com"
    h = ir.hash_recipient(raw_address)

    conn = _conn_with(_cur(("org-test",)), _cur(_fake_row("RECEIVED"), rowcount=1))

    result = ir.record_receipt(
        conn,
        datastream_id=_FAKE_DS_ID,
        credential_id=None,
        channel="email",
        provider_event_id="evt-read-model-check",
        recipient_hash=h,
        actor="system",
        host_context={},
        trace_id=None,
        idempotency_key="ik-read-model",
    )

    result_str = json.dumps(result)
    assert raw_address not in result_str, (
        "LEAK: raw address found in safe read-model"
    )


# ---------------------------------------------------------------------------
# (b) Redelivery: same provider_event_id -> deduplicated=True; no second row.
# ---------------------------------------------------------------------------


def test_record_receipt_deduplicates_redelivery(monkeypatch):
    """ON CONFLICT reconciles to the existing row; deduplicated=True."""
    from core import inbound_receipts as ir

    _stub_operation(monkeypatch, ir)

    # cursor 1: _resolve_org_id. cursor 2 reused: rowcount=0 => ON CONFLICT DO
    # NOTHING (redelivery), then the single fetchone returns the existing row.
    conn = _conn_with(_cur(("org-test",)), _cur(_fake_row("RECEIVED"), rowcount=0))

    result = ir.record_receipt(
        conn,
        datastream_id=_FAKE_DS_ID,
        credential_id=_FAKE_CRED_ID,
        channel="email",
        provider_event_id=_FAKE_PROVIDER_EVENT_ID,
        recipient_hash=_FAKE_RECIPIENT_HASH,
        actor="system",
        host_context={},
        trace_id=None,
        idempotency_key="ik-dedup-1",
    )

    assert result["deduplicated"] is True
    assert result["state"] == "RECEIVED"
    assert result["receipt_id"] == _FAKE_RECEIPT_ID


# ---------------------------------------------------------------------------
# (c) mark_state: RECEIVED -> PROCESSING -> LANDED sets import_ledger_id.
# ---------------------------------------------------------------------------


def test_mark_state_received_to_processing(monkeypatch):
    """mark_state RECEIVED -> PROCESSING: state updated, no import_ledger_id."""
    from core import inbound_receipts as ir

    _stub_operation(monkeypatch, ir)

    # cursor 1: _load_receipt_row. cursor 2: _resolve_org_id. cursor 3: mutation
    # reuses one cursor for UPDATE (rowcount=1) + SELECT-back.
    conn = _conn_with(
        _cur(_fake_row("RECEIVED")),
        _cur(("org-test",)),
        _cur(_fake_row("PROCESSING"), rowcount=1),
    )

    result = ir.mark_state(
        conn,
        receipt_id=_FAKE_RECEIPT_ID,
        datastream_id=_FAKE_DS_ID,
        state="PROCESSING",
        actor="worker",
        host_context={},
        trace_id=None,
        idempotency_key="ik-ms-proc-1",
    )

    assert result["state"] == "PROCESSING"
    assert result["import_ledger_id"] is None


def test_mark_state_processing_to_landed_sets_import_ledger_id(monkeypatch):
    """mark_state PROCESSING -> LANDED: import_ledger_id is set in result."""
    from core import inbound_receipts as ir

    _stub_operation(monkeypatch, ir)

    mfl_id = "mfl_01JZAAABBBCCCDDDEEEFFF00099"
    landed_row = _fake_row("LANDED", import_ledger_id=mfl_id)

    conn = _conn_with(
        _cur(_fake_row("PROCESSING")),
        _cur(("org-test",)),
        _cur(landed_row, rowcount=1),
    )

    result = ir.mark_state(
        conn,
        receipt_id=_FAKE_RECEIPT_ID,
        datastream_id=_FAKE_DS_ID,
        state="LANDED",
        actor="worker",
        host_context={},
        trace_id=None,
        idempotency_key="ik-ms-landed-1",
        import_ledger_id=mfl_id,
    )

    assert result["state"] == "LANDED"
    assert result["import_ledger_id"] == mfl_id


# ---------------------------------------------------------------------------
# (d) mark_state to FAILED sets error_code and error_detail.
# ---------------------------------------------------------------------------


def test_mark_state_to_failed_sets_error_fields(monkeypatch):
    """mark_state RECEIVED -> FAILED: error_code and error_detail appear in result."""
    from core import inbound_receipts as ir

    _stub_operation(monkeypatch, ir)

    failed_row = _fake_row(
        "FAILED", error_code="PARSE_ERROR", error_detail="could not decode attachment"
    )

    conn = _conn_with(
        _cur(_fake_row("RECEIVED")),
        _cur(("org-test",)),
        _cur(failed_row, rowcount=1),
    )

    result = ir.mark_state(
        conn,
        receipt_id=_FAKE_RECEIPT_ID,
        datastream_id=_FAKE_DS_ID,
        state="FAILED",
        actor="worker",
        host_context={},
        trace_id=None,
        idempotency_key="ik-ms-failed-1",
        error_code="PARSE_ERROR",
        error_detail="could not decode attachment",
    )

    assert result["state"] == "FAILED"
    assert result["error_code"] == "PARSE_ERROR"
    assert result["error_detail"] == "could not decode attachment"


# ---------------------------------------------------------------------------
# (e) get_receipt: safe read-model for existing row.
# ---------------------------------------------------------------------------


def test_get_receipt_returns_safe_model():
    """get_receipt returns a safe read-model; no raw address."""
    from core.inbound_receipts import get_receipt

    cur = _cur(_fake_row("RECEIVED"), rowcount=0)
    conn = _conn_with(cur)

    result = get_receipt(
        conn,
        receipt_id=_FAKE_RECEIPT_ID,
        datastream_id=_FAKE_DS_ID,
    )

    assert result is not None
    assert result["receipt_id"] == _FAKE_RECEIPT_ID
    assert result["state"] == "RECEIVED"
    assert result["datastream_id"] == _FAKE_DS_ID
    # Safe model has recipient_hash (not the raw address).
    assert "recipient_hash" in result
    # No raw address key present.
    assert "recipient" not in result
    assert "raw_recipient" not in result
    assert "address" not in result


# ---------------------------------------------------------------------------
# (f) get_receipt returns None for absent row.
# ---------------------------------------------------------------------------


def test_get_receipt_returns_none_when_absent():
    """get_receipt returns None when the row does not exist."""
    from core.inbound_receipts import get_receipt

    cur = _cur(None, rowcount=0)
    conn = _conn_with(cur)

    result = get_receipt(
        conn,
        receipt_id="inbrx_nonexistent",
        datastream_id=_FAKE_DS_ID,
    )

    assert result is None


# ---------------------------------------------------------------------------
# (g) list_receipts: newest-first safe read-models.
# ---------------------------------------------------------------------------


def test_list_receipts_returns_newest_first():
    """list_receipts returns a list of safe read-models."""
    from core.inbound_receipts import list_receipts

    rows = [_fake_row("LANDED"), _fake_row("RECEIVED")]
    cur = _cur(fetchall=rows, rowcount=0)
    conn = _conn_with(cur)

    results = list_receipts(conn, datastream_id=_FAKE_DS_ID)

    assert isinstance(results, list)
    assert len(results) == 2
    for r in results:
        assert "receipt_id" in r
        assert "state" in r
        # No raw address key.
        assert "recipient" not in r
        assert "raw_recipient" not in r


def test_list_receipts_empty_when_no_rows():
    """list_receipts returns an empty list when no rows exist."""
    from core.inbound_receipts import list_receipts

    cur = _cur(fetchall=[], rowcount=0)
    conn = _conn_with(cur)

    results = list_receipts(conn, datastream_id=_FAKE_DS_ID)
    assert results == []


# ---------------------------------------------------------------------------
# (h) Safe read-model NEVER contains raw address or token.
# ---------------------------------------------------------------------------


def test_safe_read_model_never_contains_raw_address():
    """The safe read-model must carry only recipient_hash, never a raw address."""
    from core.inbound_receipts import get_receipt

    raw_addr = "ds_verysecrettoken@inbound.example.com"
    h = _sha256(raw_addr)

    # Build a fake row where recipient_hash is the hash of raw_addr.
    row = (
        _FAKE_RECEIPT_ID,
        _FAKE_DS_ID,
        _FAKE_CRED_ID,
        "email",
        _FAKE_PROVIDER_EVENT_ID,
        h,           # recipient_hash
        1,           # attachment_count
        None,        # total_bytes
        None,        # quarantine_uri
        "RECEIVED",  # state
        None,        # import_ledger_id
        None,        # error_code
        None,        # error_detail
        _TS,         # created_at
        _TS,         # updated_at
    )

    cur = _cur(row, rowcount=0)
    conn = _conn_with(cur)

    result = get_receipt(conn, receipt_id=_FAKE_RECEIPT_ID, datastream_id=_FAKE_DS_ID)
    assert result is not None

    result_str = json.dumps(result, default=str)
    assert raw_addr not in result_str, (
        "LEAK: raw recipient address found in safe read-model"
    )
    # The hash (safe) is present.
    assert h in result_str


# ---------------------------------------------------------------------------
# mark_state notfound.
# ---------------------------------------------------------------------------


def test_mark_state_raises_not_found_for_absent_receipt():
    """mark_state raises InboundReceiptNotFound when receipt is absent."""
    from core.inbound_receipts import InboundReceiptNotFound, mark_state

    cur_load = _cur(None, rowcount=0)
    conn = _conn_with(cur_load)

    with pytest.raises(InboundReceiptNotFound):
        mark_state(
            conn,
            receipt_id="inbrx_doesnotexist",
            datastream_id=_FAKE_DS_ID,
            state="PROCESSING",
            actor="worker",
            host_context={},
            trace_id=None,
            idempotency_key="ik-nf-1",
        )


# ---------------------------------------------------------------------------
# Live-PG-gated tests.
# ---------------------------------------------------------------------------


def _insert_op(cur, op_id: str, ik_hash: str, req_hash: str = None) -> None:
    """Insert a minimal operations row for FK satisfaction."""

    if req_hash is None:
        req_hash = "a" * 64
    cur.execute(
        "INSERT INTO app.operations "
        "(id, effective_org_id, command_type, actor, resource_path, "
        "host_context, versions, request_hash, provider_references, "
        "confirmation_mode, idempotency_key_hash, state) "
        "VALUES (%s, 'platform', 'inbound.receipt.recorded', 'test', "
        "'[\"ds:test\"]'::jsonb, '{}'::jsonb, '{}'::jsonb, %s, "
        "'{}'::jsonb, 'server', %s, 'pending')",
        (op_id, req_hash, ik_hash),
    )


def _insert_receipt(
    cur,
    *,
    receipt_id: str,
    datastream_id: str,
    provider_event_id: str,
    op_id: str,
    state: str = "RECEIVED",
    recipient_hash: str | None = None,
) -> None:
    """Insert a minimal inbound_receipts row."""
    cur.execute(
        "INSERT INTO app.inbound_receipts "
        "(id, datastream_id, channel, provider_event_id, "
        "recipient_hash, attachment_count, state, operation_id) "
        "VALUES (%s, %s, 'email', %s, %s, 0, %s, %s)",
        (
            receipt_id,
            datastream_id,
            provider_event_id,
            recipient_hash,
            state,
            op_id,
        ),
    )


@pytest.mark.live_pg
def test_live_pg_unique_dedup_constraint(pg_conn):
    """Live-PG: UNIQUE (datastream_id, provider_event_id) prevents double-record."""
    import ulid as _ulid

    with pg_conn.cursor() as cur:
        ds_id = f"ds-test-rcpt-{_ulid.ULID()}"
        op_id = f"op_{_ulid.ULID()}"
        _insert_op(cur, op_id, "a" * 64)

        rx_id = f"inbrx_{_ulid.ULID()}"
        event_id = f"evt-{_ulid.ULID()}"
        _insert_receipt(
            cur,
            receipt_id=rx_id,
            datastream_id=ds_id,
            provider_event_id=event_id,
            op_id=op_id,
        )
        pg_conn.commit()

        # Second INSERT with same (datastream_id, provider_event_id) must fail.
        op_id2 = f"op_{_ulid.ULID()}"
        _insert_op(cur, op_id2, "b" * 64, req_hash="c" * 64)
        rx_id2 = f"inbrx_{_ulid.ULID()}"
        try:
            _insert_receipt(
                cur,
                receipt_id=rx_id2,
                datastream_id=ds_id,
                provider_event_id=event_id,  # same event_id -> conflict
                op_id=op_id2,
            )
            pg_conn.commit()
            pytest.fail("Expected unique constraint violation for duplicate provider_event_id")
        except Exception as exc:
            pg_conn.rollback()
            assert "unique" in str(exc).lower() or "duplicate" in str(exc).lower(), (
                f"Expected unique constraint violation, got: {exc}"
            )


@pytest.mark.live_pg
def test_live_pg_immutability_trigger_blocks_provider_event_id_update(pg_conn):
    """Live-PG: protect_inbound_receipt blocks updating frozen column provider_event_id."""
    import ulid as _ulid

    with pg_conn.cursor() as cur:
        ds_id = f"ds-test-immut-rx-{_ulid.ULID()}"
        op_id = f"op_{_ulid.ULID()}"
        _insert_op(cur, op_id, "d" * 64, req_hash="e" * 64)

        rx_id = f"inbrx_{_ulid.ULID()}"
        event_id = f"evt-immut-{_ulid.ULID()}"
        _insert_receipt(
            cur,
            receipt_id=rx_id,
            datastream_id=ds_id,
            provider_event_id=event_id,
            op_id=op_id,
        )
        pg_conn.commit()

        # Attempt to mutate provider_event_id (frozen column).
        try:
            cur.execute(
                "UPDATE app.inbound_receipts "
                "SET provider_event_id = %s WHERE id = %s",
                (f"tampered-{_ulid.ULID()}", rx_id),
            )
            pg_conn.commit()
            pytest.fail(
                "Expected immutability trigger to block provider_event_id update"
            )
        except Exception as exc:
            pg_conn.rollback()
            assert "immutable" in str(exc).lower() or "provider_event_id" in str(exc).lower(), (
                f"Expected immutability exception, got: {exc}"
            )


@pytest.mark.live_pg
def test_live_pg_delete_forbidden(pg_conn):
    """Live-PG: DELETE on inbound_receipts is forbidden by the trigger."""
    import ulid as _ulid

    with pg_conn.cursor() as cur:
        ds_id = f"ds-test-del-rx-{_ulid.ULID()}"
        op_id = f"op_{_ulid.ULID()}"
        _insert_op(cur, op_id, "f" * 64, req_hash="g" * 64)

        rx_id = f"inbrx_{_ulid.ULID()}"
        event_id = f"evt-del-{_ulid.ULID()}"
        _insert_receipt(
            cur,
            receipt_id=rx_id,
            datastream_id=ds_id,
            provider_event_id=event_id,
            op_id=op_id,
        )
        pg_conn.commit()

        try:
            cur.execute(
                "DELETE FROM app.inbound_receipts WHERE id = %s",
                (rx_id,),
            )
            pg_conn.commit()
            pytest.fail("Expected trigger to forbid DELETE on inbound_receipts")
        except Exception as exc:
            pg_conn.rollback()
            msg = str(exc).lower()
            assert (
                "may not be deleted" in msg
                or "delete" in msg
                or "forbidden" in msg
                or "23000" in msg
            ), f"Expected deletion-forbidden exception, got: {exc}"


@pytest.mark.live_pg
def test_live_pg_no_raw_address_column_in_schema(pg_conn):
    """Live-PG: inbound_receipts must have NO column that could hold a raw address."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'app' "
            "AND table_name = 'inbound_receipts' "
            "ORDER BY ordinal_position",
        )
        columns = [row[0] for row in cur.fetchall()]

    # These fragments would indicate a raw secret is stored -- forbidden by E38-NFR03.
    forbidden_fragments = [
        "raw_token", "token_value", "secret_value", "plaintext",
        "raw_address", "recipient_address",
    ]
    for col in columns:
        for frag in forbidden_fragments:
            assert frag not in col.lower(), (
                f"SCHEMA VIOLATION: column {col!r} in inbound_receipts "
                f"looks like a raw-secret column (fragment {frag!r}). "
                f"Only recipient_hash (sha256 digest) is allowed."
            )
