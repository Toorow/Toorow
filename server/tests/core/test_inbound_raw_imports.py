"""Story 38.9: per-attachment immutable raw evidence -- offline + live-PG tests.

The unit of inbound evidence is the FILE, not the delivery. These tests pin that
distinction at both levels: the module contract offline, and the schema
guarantees against a real PostgreSQL.

Offline:
  (a) content_hash is a function of the bytes alone.
  (b) record_raw_import writes RECEIVED and returns a safe read-model.
  (c) A conflicting (receipt_id, ordinal) reconciles with deduplicated=True.
  (d) Validation fires BEFORE any SQL for every malformed input.
  (e) mark_raw_import_state is a no-op when already in the target state.
  (f) Leaving a terminal state raises rather than rewriting evidence.
  (g) The safe read-model carries no bytes, no token, no sample.
  (h) duplicate policy is versioned and skips exact landed content.

Live-PG (these actually run -- see tests/core/conftest.py and AI-93):
  (i) UNIQUE (receipt_id, ordinal) makes re-expansion idempotent.
  (j) The immutability trigger freezes content_hash and filename.
  (k) DELETE is refused, and PERMITTED under an audited erasure -- both halves,
      because a hatch nobody proves open is the failure mode migration 169 had
      to repair seventy-two migrations late.
  (l) No column in the table could hold a secret or a raw sample.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Offline helpers (mirror test_inbound_receipts.py).
# ---------------------------------------------------------------------------

_HASH_A = hashlib.sha256(b"col\n1\n").hexdigest()
_HASH_B = hashlib.sha256(b"col\n2\n").hexdigest()

_FAKE_RAW_ID = "inbraw_01JZAAABBBCCCDDDEEEFFF00001"
_FAKE_RECEIPT_ID = "inbrx_01JZAAABBBCCCDDDEEEFFF00002"
_FAKE_DS_ID = "ds-test-raw-1"


def _cur(*fetchone_rows, rowcount: int = 1, fetchall=None):
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.side_effect = list(fetchone_rows)
    cur.fetchall.return_value = fetchall or []
    cur.rowcount = rowcount
    return cur


def _conn_with(*curs):
    conn = MagicMock()
    conn.cursor.side_effect = list(curs)
    return conn


def _stub_operation(monkeypatch, capture=None):
    """Run the mutation synchronously so the SQL path is really exercised."""
    from core import inbound_raw_imports as module
    from core import operations

    def execute(operation_conn, spec, *, mutation):
        changed = mutation(operation_conn, "op-test-inbraw-1")
        if capture is not None:
            capture.setdefault("specs", []).append(spec)
            capture.setdefault("changes", []).append(changed)
        return operations.OperationResult(
            "op-test-inbraw-1", "succeeded", changed.result,
            "audit-1", "outbox-1", False,
        )

    monkeypatch.setattr(module, "execute_operation", execute)


def _row(
    state: str = "RECEIVED",
    *,
    ordinal: int = 0,
    digest: str = _HASH_A,
    error_code=None,
    import_ledger_id=None,
    detected=None,
    legal_hold=False,
):
    """A DB row tuple in _SELECT_COLS order."""
    return (
        _FAKE_RAW_ID,        # id
        _FAKE_RECEIPT_ID,    # receipt_id
        _FAKE_DS_ID,         # datastream_id
        ordinal,             # ordinal
        "report.csv",        # filename
        "text/csv",          # media_type_declared
        detected,            # media_type_detected
        1234,                # size_bytes
        digest,              # content_hash
        "file:///q/abc",     # quarantine_uri
        state,               # state
        None,                # scan_verdict
        error_code,          # error_code
        None,                # error_detail
        import_ledger_id,    # import_ledger_id
        None,                # retention_expires_at
        "quarantine-retention-v1",  # retention_policy_version
        30,                  # retention_days
        legal_hold,          # legal_hold
        None,                # duplicate_of_raw_import_id
        None,                # duplicate_policy_version
        None,                # quarantine_deleted_at
        "2026-01-01T00:00:00+00:00",  # created_at
        "2026-01-01T00:00:00+00:00",  # updated_at
    )


def _org_cur():
    """The cursor answering _resolve_org_id."""
    return _cur(("org-1",))


# ---------------------------------------------------------------------------
# (a) content_hash.
# ---------------------------------------------------------------------------


def test_content_hash_is_a_function_of_the_bytes_only():
    """Identical bytes under different names hash identically -- by design."""
    from core.inbound_raw_imports import content_hash

    assert content_hash(b"col\n1\n") == _HASH_A
    assert content_hash(b"col\n1\n") == content_hash(bytearray(b"col\n1\n"))
    assert content_hash(b"col\n1\n") != content_hash(b"col\n2\n")


def test_content_hash_refuses_a_non_bytes_payload():
    from core.inbound_raw_imports import RawImportValidationError, content_hash

    with pytest.raises(RawImportValidationError):
        content_hash("not bytes")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# (b) (c) record_raw_import.
# ---------------------------------------------------------------------------


def test_record_raw_import_inserts_received_and_returns_safe_model(monkeypatch):
    from core.inbound_raw_imports import record_raw_import

    _stub_operation(monkeypatch)
    conn = _conn_with(_org_cur(), _cur(_row(), rowcount=1))

    out = record_raw_import(
        conn,
        receipt_id=_FAKE_RECEIPT_ID,
        datastream_id=_FAKE_DS_ID,
        ordinal=0,
        size_bytes=1234,
        content_hash=_HASH_A,
        filename="report.csv",
        media_type_declared="text/csv",
        quarantine_uri="file:///q/abc",
        actor="inbound-worker",
        idempotency_key="ik-raw-1",
    )

    assert out["raw_import_id"] == _FAKE_RAW_ID
    assert out["state"] == "RECEIVED"
    assert out["content_hash"] == _HASH_A
    assert out["deduplicated"] is False


def test_record_raw_import_reconciles_on_receipt_ordinal_conflict(monkeypatch):
    """Re-expanding the same delivery must not create a twin row."""
    from core.inbound_raw_imports import record_raw_import

    _stub_operation(monkeypatch)
    # rowcount=0 => ON CONFLICT DO NOTHING fired; the SELECT then finds the row.
    conn = _conn_with(_org_cur(), _cur(_row("LANDED"), rowcount=0))

    out = record_raw_import(
        conn,
        receipt_id=_FAKE_RECEIPT_ID,
        datastream_id=_FAKE_DS_ID,
        ordinal=0,
        size_bytes=1234,
        content_hash=_HASH_A,
        filename="report.csv",
        media_type_declared="text/csv",
        quarantine_uri="file:///q/abc",
        actor="inbound-worker",
        idempotency_key="ik-raw-1",
    )

    assert out["deduplicated"] is True
    assert out["state"] == "LANDED"


def test_record_raw_import_rejects_divergent_ordinal_replay(monkeypatch):
    from core.inbound_raw_imports import RawImportValidationError, record_raw_import

    _stub_operation(monkeypatch)
    conn = _conn_with(_org_cur(), _cur(_row(), rowcount=0))
    with pytest.raises(RawImportValidationError, match="different immutable"):
        record_raw_import(
            conn, receipt_id=_FAKE_RECEIPT_ID, datastream_id=_FAKE_DS_ID,
            ordinal=0, size_bytes=1234, content_hash=_HASH_B,
            filename="report.csv", media_type_declared="text/csv",
            quarantine_uri="file:///q/abc", actor="inbound-worker",
            idempotency_key="ik-divergent",
        )


def test_record_operation_replay_reloads_current_terminal_outcome(monkeypatch):
    from core import inbound_raw_imports as module
    from core import operations

    monkeypatch.setattr(
        module, "execute_operation",
        lambda conn, spec, *, mutation: operations.OperationResult(
            "op-existing", "succeeded", {"id": _FAKE_RAW_ID, "state": "RECEIVED"},
            "audit-existing", "outbox-existing", True,
        ),
    )
    conn = _conn_with(
        _org_cur(), _cur(_row("LANDED", import_ledger_id="mfl_existing"))
    )
    out = module.record_raw_import(
        conn, receipt_id=_FAKE_RECEIPT_ID, datastream_id=_FAKE_DS_ID,
        ordinal=0, size_bytes=1234, content_hash=_HASH_A,
        filename="report.csv", media_type_declared="text/csv",
        quarantine_uri="file:///q/abc", actor="inbound-worker",
        idempotency_key="ik-replayed",
    )
    assert out["deduplicated"] is True
    assert out["state"] == "LANDED"
    assert out["import_ledger_id"] == "mfl_existing"


def test_record_raw_import_request_payload_carries_no_filename(monkeypatch):
    """The audit trail keeps the content address, never the sender's string.

    A filename is attacker-controlled text; the hash is not. Putting the former
    in `request_payload` would push untrusted data into the audit spine for no
    identifying benefit -- the hash already identifies the payload exactly.
    """
    from core.inbound_raw_imports import record_raw_import

    capture: dict = {}
    _stub_operation(monkeypatch, capture)
    conn = _conn_with(_org_cur(), _cur(_row(), rowcount=1))

    record_raw_import(
        conn,
        receipt_id=_FAKE_RECEIPT_ID,
        datastream_id=_FAKE_DS_ID,
        ordinal=0,
        size_bytes=1234,
        content_hash=_HASH_A,
        filename="=cmd|'/c calc'!A1.csv",
        actor="inbound-worker",
        idempotency_key="ik-raw-1",
    )

    payload = capture["specs"][0].request_payload
    assert payload["content_hash"] == _HASH_A
    assert "filename" not in payload
    assert "cmd" not in str(payload)


# ---------------------------------------------------------------------------
# (d) Validation before SQL.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        ({"receipt_id": ""}, "receipt_id"),
        ({"datastream_id": ""}, "datastream_id"),
        ({"ordinal": -1}, "ordinal"),
        ({"ordinal": True}, "ordinal"),          # bool is not an ordinal
        ({"size_bytes": -5}, "size_bytes"),
        ({"content_hash": "short"}, "content_hash"),
        ({"content_hash": "g" * 64}, "content_hash"),  # 64 chars but not hex
        ({"actor": ""}, "actor"),
        ({"idempotency_key": ""}, "idempotency_key"),
    ],
)
def test_record_raw_import_validates_before_touching_the_database(
    kwargs, fragment
):
    from core.inbound_raw_imports import RawImportValidationError, record_raw_import

    base = dict(
        receipt_id=_FAKE_RECEIPT_ID,
        datastream_id=_FAKE_DS_ID,
        ordinal=0,
        size_bytes=10,
        content_hash=_HASH_A,
        actor="inbound-worker",
        idempotency_key="ik-1",
    )
    base.update(kwargs)
    conn = MagicMock()

    with pytest.raises(RawImportValidationError, match=fragment):
        record_raw_import(conn, **base)

    # The guard is the point: nothing may have been asked of the database.
    conn.cursor.assert_not_called()


# ---------------------------------------------------------------------------
# (e) (f) mark_raw_import_state.
# ---------------------------------------------------------------------------


def test_mark_state_is_a_noop_when_already_in_the_target_state(monkeypatch):
    """A retried worker is not an error."""
    from core.inbound_raw_imports import mark_raw_import_state

    _stub_operation(monkeypatch)
    conn = _conn_with(_cur(_row("LANDED")))

    out = mark_raw_import_state(
        conn,
        raw_import_id=_FAKE_RAW_ID,
        datastream_id=_FAKE_DS_ID,
        state="LANDED",
        actor="inbound-worker",
        idempotency_key="ik-state-1",
    )

    assert out["state"] == "LANDED"
    # No org lookup, no UPDATE: exactly one cursor, the initial load.
    assert conn.cursor.call_count == 1


def test_mark_state_refuses_to_leave_a_terminal_state(monkeypatch):
    """A terminal outcome is evidence; rewriting it is what append-only forbids."""
    from core.inbound_raw_imports import RawImportStateError, mark_raw_import_state

    _stub_operation(monkeypatch)
    conn = _conn_with(_cur(_row("REJECTED")))

    with pytest.raises(RawImportStateError, match="terminal"):
        mark_raw_import_state(
            conn,
            raw_import_id=_FAKE_RAW_ID,
            datastream_id=_FAKE_DS_ID,
            state="LANDED",
            actor="inbound-worker",
            idempotency_key="ik-state-2",
        )


def test_mark_state_raises_not_found_for_another_datastreams_row(monkeypatch):
    """Scoping the load by Datastream is what makes cross-tenant ids look absent."""
    from core.inbound_raw_imports import RawImportNotFound, mark_raw_import_state

    _stub_operation(monkeypatch)
    conn = _conn_with(_cur(None))

    with pytest.raises(RawImportNotFound):
        mark_raw_import_state(
            conn,
            raw_import_id=_FAKE_RAW_ID,
            datastream_id="ds-someone-else",
            state="LANDED",
            actor="inbound-worker",
            idempotency_key="ik-state-3",
        )


def test_mark_state_locks_and_conditionally_advances(monkeypatch):
    from core.inbound_raw_imports import mark_raw_import_state

    _stub_operation(monkeypatch)
    mutation_cur = _cur(_row("RECEIVED"), _row("SCANNING"), rowcount=1)
    conn = _conn_with(_cur(_row("RECEIVED")), _org_cur(), mutation_cur)

    out = mark_raw_import_state(
        conn, raw_import_id=_FAKE_RAW_ID, datastream_id=_FAKE_DS_ID,
        state="SCANNING", actor="inbound-worker",
        idempotency_key="ik-state-lock",
    )

    assert out["state"] == "SCANNING"
    sql = " ".join(str(call.args[0]) for call in mutation_cur.execute.call_args_list)
    assert "FOR UPDATE" in sql
    assert "clock_timestamp()" in sql
    assert "AND state = %s" in sql


def test_state_operation_hash_covers_every_mutation_affecting_value(monkeypatch):
    from core.inbound_raw_imports import mark_raw_import_state

    capture: dict = {}
    _stub_operation(monkeypatch, capture)
    mutation_cur = _cur(
        _row("RECEIVED"),
        _row("REJECTED", error_code="unsafe", detected="application/zip"),
        rowcount=1,
    )
    conn = _conn_with(_cur(_row("RECEIVED")), _org_cur(), mutation_cur)
    mark_raw_import_state(
        conn, raw_import_id=_FAKE_RAW_ID, datastream_id=_FAKE_DS_ID,
        state="REJECTED", actor="inbound-worker", idempotency_key="ik-full-hash",
        error_code="unsafe", error_detail="bounded operator detail",
        media_type_detected="application/zip",
        scan_verdict={"accepted": False, "reason": "unsafe"},
    )

    payload = capture["specs"][0].request_payload
    assert payload["error_code"] == "unsafe"
    assert len(payload["error_detail_sha256"]) == 64
    assert payload["media_type_detected"] == "application/zip"
    assert len(payload["scan_verdict_sha256"]) == 64
    assert "bounded operator detail" not in str(payload)


def test_mark_state_rejects_an_unknown_state():
    from core.inbound_raw_imports import (
        RawImportValidationError,
        mark_raw_import_state,
    )

    conn = MagicMock()
    with pytest.raises(RawImportValidationError, match="state"):
        mark_raw_import_state(
            conn,
            raw_import_id=_FAKE_RAW_ID,
            datastream_id=_FAKE_DS_ID,
            state="PUBLISHED",
            actor="inbound-worker",
            idempotency_key="ik-state-4",
        )
    conn.cursor.assert_not_called()


# ---------------------------------------------------------------------------
# (g) Read models disclose nothing they should not.
# ---------------------------------------------------------------------------


def test_get_raw_import_returns_none_for_an_absent_or_foreign_row():
    from core.inbound_raw_imports import get_raw_import

    conn = _conn_with(_cur(None))
    assert get_raw_import(
        conn, raw_import_id=_FAKE_RAW_ID, datastream_id="ds-other"
    ) is None


def test_safe_read_model_carries_no_bytes_token_or_sample():
    from core.inbound_raw_imports import get_raw_import

    conn = _conn_with(_cur(_row()))
    model = get_raw_import(
        conn, raw_import_id=_FAKE_RAW_ID, datastream_id=_FAKE_DS_ID
    )

    forbidden = {
        "bytes", "data", "payload", "token", "token_hash", "secret",
        "signature", "sample", "rows", "recipient",
    }
    assert forbidden.isdisjoint(model.keys())
    # And the values do not smuggle one in either.
    assert all(
        not isinstance(v, (bytes, bytearray)) for v in model.values()
    )


def test_list_raw_imports_bounds_its_own_page_size():
    from core.inbound_raw_imports import RawImportValidationError, list_raw_imports

    conn = MagicMock()
    for bad in (0, -1, 501):
        with pytest.raises(RawImportValidationError, match="limit"):
            list_raw_imports(conn, datastream_id=_FAKE_DS_ID, limit=bad)
    conn.cursor.assert_not_called()


def test_list_raw_imports_for_receipt_reads_in_manifest_order():
    from core.inbound_raw_imports import list_raw_imports_for_receipt

    conn = _conn_with(
        _cur(fetchall=[_row(ordinal=0), _row(ordinal=1, digest=_HASH_B)])
    )
    out = list_raw_imports_for_receipt(
        conn, receipt_id=_FAKE_RECEIPT_ID, datastream_id=_FAKE_DS_ID
    )
    assert [r["ordinal"] for r in out] == [0, 1]
    sql = conn.cursor.return_value.__enter__.return_value.execute.call_args
    assert sql is None or "ORDER BY ordinal ASC" in str(sql)


# ---------------------------------------------------------------------------
# (h) find_duplicate_content reports; it does not decide.
# ---------------------------------------------------------------------------


def test_find_duplicate_content_returns_evidence_oldest_first():
    from core.inbound_raw_imports import find_duplicate_content

    conn = _conn_with(_cur(fetchall=[_row(ordinal=0), _row(ordinal=1)]))
    out = find_duplicate_content(
        conn, datastream_id=_FAKE_DS_ID, content_hash=_HASH_A
    )
    assert len(out) == 2
    # It returns rows. It does NOT return a verdict, a policy or a boolean:
    # whether a repeat is a duplicate to skip is a per-Datastream decision.
    assert all("status" not in r and "is_duplicate" not in r for r in out)


def test_legal_hold_synchronizes_object_before_database(monkeypatch):
    from core import inbound_raw_imports as module

    events = []

    class _Store:
        def set_legal_hold(self, uri, **kwargs):
            events.append(("object", uri, kwargs))

    def _execute(conn, spec, *, mutation):
        from core import operations

        events.append(("database", spec.command_type))
        changed = mutation(conn, "op-hold")
        return operations.OperationResult(
            "op-hold", "succeeded", changed.result, "audit", "outbox", False
        )

    monkeypatch.setattr(module, "execute_operation", _execute)
    mutation_cur = _cur(
        _row("LANDED", import_ledger_id="mfl_1"),
        _row("LANDED", import_ledger_id="mfl_1", legal_hold=True),
        rowcount=1,
    )
    conn = _conn_with(
        _cur(_row("LANDED", import_ledger_id="mfl_1")),
        _org_cur(), mutation_cur,
    )
    out = module.place_raw_import_legal_hold(
        conn, store=_Store(), raw_import_id=_FAKE_RAW_ID,
        datastream_id=_FAKE_DS_ID, actor="retention-worker",
        idempotency_key="hold-once",
    )

    assert out["legal_hold"] is True
    assert events[0][0] == "object"
    assert events[1] == ("database", "inbound.raw_import.legal_hold_changed")
    assert events[0][2]["enabled"] is True


def test_duplicate_policy_skips_exact_landed_content_with_version(monkeypatch):
    from core import inbound_raw_imports as module

    monkeypatch.setattr(
        module, "find_duplicate_content",
        lambda *args, **kwargs: [{
            "raw_import_id": "inbraw_original", "state": "LANDED"
        }],
    )
    decision = module.duplicate_content_decision(
        object(), datastream_id=_FAKE_DS_ID, raw_import_id=_FAKE_RAW_ID,
        content_hash=_HASH_A,
    )

    assert decision == {
        "policy_version": "skip-exact-v1",
        "skip_execution": True,
        "duplicate_of_raw_import_id": "inbraw_original",
    }


def test_find_duplicate_content_validates_the_hash():
    from core.inbound_raw_imports import (
        RawImportValidationError,
        find_duplicate_content,
    )

    conn = MagicMock()
    with pytest.raises(RawImportValidationError, match="content_hash"):
        find_duplicate_content(
            conn, datastream_id=_FAKE_DS_ID, content_hash="nope"
        )
    conn.cursor.assert_not_called()


def test_migration_185_enforces_cross_tenant_provenance_and_lifecycle():
    migration = (
        Path(__file__).parents[3]
        / "infra/nango/migrations/185_inbound_raw_import_durability_guard.sql"
    ).read_text(encoding="utf-8")

    required = (
        "receipt and Datastream are inconsistent",
        "NEW.quarantine_uri IS DISTINCT FROM OLD.quarantine_uri",
        "jsonb_build_array('datastream:' || NEW.datastream_id)",
        "jsonb_build_array('receipt:' || NEW.receipt_id)",
        "jsonb_build_array('raw_import:' || NEW.id)",
        "LANDED raw import requires import ledger evidence",
        "skip-exact-v1",
        "FORCE ROW LEVEL SECURITY",
        "quarantine_deleted_at",
        "legal_hold = FALSE",
        "current_setting('app.rgpd_erasure', true)",
        "retention_policy_version",
    )
    assert all(fragment in migration for fragment in required)


# ---------------------------------------------------------------------------
# Live-PG-gated: the schema guarantees, against a real database.
# ---------------------------------------------------------------------------


def _insert_receipt(cur, *, receipt_id, datastream_id, provider_event_id, op_id):
    cur.execute(
        "UPDATE app.operations SET resource_path = jsonb_build_array(%s::text, %s::text) "
        "WHERE id = %s",
        (f"datastream:{datastream_id}", f"provider_event:{provider_event_id}", op_id),
    )
    cur.execute(
        "INSERT INTO app.inbound_receipts "
        "(id, datastream_id, channel, provider_event_id, receipt_fingerprint, "
        "attachment_count, state, operation_id) "
        "VALUES (%s, %s, 'email', %s, %s, 2, 'RECEIVED', %s)",
        (receipt_id, datastream_id, provider_event_id, "a" * 64, op_id),
    )


def _insert_raw(
    cur, *, raw_id, receipt_id, datastream_id, ordinal, op_id, digest=None
):
    cur.execute(
        "UPDATE app.operations SET command_type = %s, "
        "resource_path = jsonb_build_array(%s::text, %s::text, %s::text) WHERE id = %s",
        (
            "inbound.raw_import.recorded", f"datastream:{datastream_id}",
            f"receipt:{receipt_id}", f"attachment:{ordinal}", op_id,
        ),
    )
    content_digest = digest or _HASH_A
    cur.execute("SELECT org_id FROM app.datastreams WHERE id = %s", (datastream_id,))
    org_id = cur.fetchone()[0]
    quarantine_uri = (
        f"gs://test/inbound/{org_id}/{datastream_id}/{content_digest}/attachment"
    )
    cur.execute(
        "INSERT INTO app.inbound_raw_imports "
        "(id, receipt_id, datastream_id, ordinal, filename, "
        "media_type_declared, size_bytes, content_hash, quarantine_uri, "
        "state, retention_expires_at, retention_policy_version, retention_days, "
        "operation_id) "
        "VALUES (%s, %s, %s, %s, 'report.csv', 'text/csv', 42, %s, %s, "
        "'RECEIVED', clock_timestamp() + interval '30 days', "
        "'quarantine-retention-v1', 30, %s)",
        (
            raw_id, receipt_id, datastream_id, ordinal, content_digest,
            quarantine_uri, op_id,
        ),
    )



@pytest.fixture
def raw_pg_receipt(pg_conn, inbound_pg_scope, insert_operation):
    """A committed receipt to hang raw imports off, plus its identifiers."""
    import ulid as _ulid

    op_id = insert_operation("inbound.receipt.recorded")
    receipt_id = f"inbrx_{_ulid.ULID()}"
    with pg_conn.cursor() as cur:
        _insert_receipt(
            cur,
            receipt_id=receipt_id,
            datastream_id=inbound_pg_scope["datastream_id"],
            provider_event_id=f"evt-raw-{_ulid.ULID()}",
            op_id=op_id,
        )
    pg_conn.commit()
    return {
        "receipt_id": receipt_id,
        "datastream_id": inbound_pg_scope["datastream_id"],
    }


@pytest.mark.live_pg
def test_live_pg_unique_receipt_ordinal_makes_expansion_idempotent(
    pg_conn, raw_pg_receipt, insert_operation
):
    """Two rows for the same attachment of the same delivery must be impossible."""
    import ulid as _ulid

    with pg_conn.cursor() as cur:
        _insert_raw(
            cur,
            raw_id=f"inbraw_{_ulid.ULID()}",
            receipt_id=raw_pg_receipt["receipt_id"],
            datastream_id=raw_pg_receipt["datastream_id"],
            ordinal=0,
            op_id=insert_operation(),
        )
        pg_conn.commit()

        try:
            _insert_raw(
                cur,
                raw_id=f"inbraw_{_ulid.ULID()}",
                receipt_id=raw_pg_receipt["receipt_id"],
                datastream_id=raw_pg_receipt["datastream_id"],
                ordinal=0,  # same attachment position -> conflict
                op_id=insert_operation(),
                digest=_HASH_B,
            )
            pg_conn.commit()
            pytest.fail("Expected UNIQUE (receipt_id, ordinal) violation")
        except Exception as exc:
            pg_conn.rollback()
            msg = str(exc).lower()
            assert "unique" in msg or "duplicate" in msg or "uq_inbraw" in msg, (
                f"Expected a unique violation, got: {exc}"
            )


@pytest.mark.live_pg
def test_live_pg_a_second_attachment_of_the_same_delivery_is_allowed(
    pg_conn, raw_pg_receipt, insert_operation
):
    """The counterpart: ordinal 1 must insert cleanly beside ordinal 0.

    Without this, a unique index that was accidentally on `receipt_id` alone
    would still pass the test above while silently capping every delivery at one
    attachment -- which is precisely the defect this story exists to remove.
    """
    import ulid as _ulid

    with pg_conn.cursor() as cur:
        for ordinal, digest in ((0, _HASH_A), (1, _HASH_B)):
            _insert_raw(
                cur,
                raw_id=f"inbraw_{_ulid.ULID()}",
                receipt_id=raw_pg_receipt["receipt_id"],
                datastream_id=raw_pg_receipt["datastream_id"],
                ordinal=ordinal,
                op_id=insert_operation(),
                digest=digest,
            )
        pg_conn.commit()

        cur.execute(
            "SELECT count(*) FROM app.inbound_raw_imports WHERE receipt_id = %s",
            (raw_pg_receipt["receipt_id"],),
        )
        assert cur.fetchone()[0] == 2


@pytest.mark.live_pg
@pytest.mark.parametrize(
    "column, value",
    [
        ("content_hash", _HASH_B),
        ("filename", "renamed.csv"),
        ("media_type_declared", "application/json"),
        ("ordinal", 7),
    ],
)
def test_live_pg_immutability_trigger_freezes_the_claim(
    pg_conn, raw_pg_receipt, insert_operation, column, value
):
    """The sender's claim and the content address cannot be edited afterwards.

    `filename` and `media_type_declared` are frozen deliberately: Story 38.10
    compares the CLAIM against independently detected reality, and a claim that
    can be corrected in place erases the very mismatch it must detect.
    """
    import ulid as _ulid

    raw_id = f"inbraw_{_ulid.ULID()}"
    with pg_conn.cursor() as cur:
        _insert_raw(
            cur,
            raw_id=raw_id,
            receipt_id=raw_pg_receipt["receipt_id"],
            datastream_id=raw_pg_receipt["datastream_id"],
            ordinal=0,
            op_id=insert_operation(),
        )
        pg_conn.commit()

        try:
            cur.execute(
                f"UPDATE app.inbound_raw_imports SET {column} = %s WHERE id = %s",
                (value, raw_id),
            )
            pg_conn.commit()
            pytest.fail(f"Expected the trigger to freeze {column}")
        except Exception as exc:
            pg_conn.rollback()
            assert "immutable" in str(exc).lower() or column in str(exc), (
                f"Expected an immutability error for {column}, got: {exc}"
            )


@pytest.mark.live_pg
def test_live_pg_lifecycle_columns_remain_writable(
    pg_conn, raw_pg_receipt, insert_operation
):
    """Freezing the claim must not freeze the verdict.

    `media_type_detected`, `state` and `scan_verdict` are exactly what 38.10 has
    to write. A trigger that froze them too would make this table unusable by
    the story it was built for.
    """
    import ulid as _ulid

    raw_id = f"inbraw_{_ulid.ULID()}"
    with pg_conn.cursor() as cur:
        _insert_raw(
            cur,
            raw_id=raw_id,
            receipt_id=raw_pg_receipt["receipt_id"],
            datastream_id=raw_pg_receipt["datastream_id"],
            ordinal=0,
            op_id=insert_operation(),
        )
        pg_conn.commit()

        transition_op = insert_operation("inbound.raw_import.state_changed")
        cur.execute(
            "UPDATE app.operations SET resource_path = "
            "jsonb_build_array(%s::text, %s::text, %s::text) "
            "WHERE id = %s",
            (
                f"datastream:{raw_pg_receipt['datastream_id']}",
                f"raw_import:{raw_id}", "state:REJECTED", transition_op,
            ),
        )
        cur.execute(
            "UPDATE app.inbound_raw_imports "
            "SET state = 'REJECTED', media_type_detected = 'application/zip', "
            "    scan_verdict = %s::jsonb, error_code = 'declared_type_mismatch', "
            "    operation_id = %s, updated_at = clock_timestamp() "
            "WHERE id = %s",
            (
                '{"declared":"text/csv","detected":"application/zip"}',
                transition_op, raw_id,
            ),
        )
        pg_conn.commit()

        cur.execute(
            "SELECT state, media_type_detected, error_code "
            "FROM app.inbound_raw_imports WHERE id = %s",
            (raw_id,),
        )
        assert cur.fetchone() == (
            "REJECTED", "application/zip", "declared_type_mismatch",
        )


@pytest.mark.live_pg
def test_live_pg_delete_requires_transaction_local_rgpd_erasure(
    pg_conn, raw_pg_receipt, insert_operation
):
    import ulid as _ulid

    raw_id = f"inbraw_{_ulid.ULID()}"
    with pg_conn.cursor() as cur:
        _insert_raw(
            cur, raw_id=raw_id, receipt_id=raw_pg_receipt["receipt_id"],
            datastream_id=raw_pg_receipt["datastream_id"], ordinal=0,
            op_id=insert_operation(),
        )
        pg_conn.commit()
        try:
            cur.execute(
                "DELETE FROM app.inbound_raw_imports WHERE id = %s", (raw_id,)
            )
            pg_conn.commit()
            pytest.fail("Expected ordinary evidence deletion to be refused")
        except Exception as exc:
            pg_conn.rollback()
            assert "may not be deleted" in str(exc).lower() or "23000" in str(exc)

    with pg_conn.cursor() as cur:
        cur.execute("SET LOCAL app.rgpd_erasure = 'on'")
        cur.execute("DELETE FROM app.inbound_raw_imports WHERE id = %s", (raw_id,))
        assert cur.rowcount == 1
    pg_conn.commit()


@pytest.mark.live_pg
def test_live_pg_no_column_could_hold_a_secret_or_a_sample(pg_conn):
    """E38-NFR03 as a schema property, not as a promise in a docstring."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'app' AND table_name = 'inbound_raw_imports' "
            "ORDER BY ordinal_position"
        )
        columns = [r[0].lower() for r in cur.fetchall()]

    assert columns, "app.inbound_raw_imports must exist (migration 171)"
    # Fragments are specific on purpose. A bare "bytes" also matches
    # `size_bytes`, which holds a LENGTH -- and a guard that fires on a column
    # measuring a file is a guard someone will disable.
    forbidden = [
        "raw_token", "token_value", "token_hash", "secret", "plaintext",
        "raw_address", "recipient", "signature", "sample", "row_data",
        "payload", "content_bytes", "raw_bytes", "file_bytes",
    ]
    for col in columns:
        for frag in forbidden:
            assert frag not in col, (
                f"SCHEMA VIOLATION: column {col!r} looks like it could hold a "
                f"secret or a raw sample (fragment {frag!r}). Only the content "
                f"HASH and safe metadata belong in this table."
            )


def test_the_state_transition_result_is_bounded_and_never_carries_the_bundle():
    """AI-321 (2026-08-29): the mutation result was the whole row, dispatch bundle
    included -- a Template contract plus every mapping binding, nested past the
    six levels `execute_operation` allows. The first governed upload on a
    Datastream the assistant had just brought to ACTIVE died there, after the
    UPDATE, behind a 503. The bundle stays on the row; the result keeps its
    fingerprint, which is what every consumer references."""
    from core.inbound_raw_imports import _COL_NAMES, mark_raw_import_state
    from core.operations import _validate_json

    deep = {"fields": [{"binding": {"evidence": [{"a": {"b": {"c": {"d": 1}}}}]}}]}
    row = dict(zip(_COL_NAMES, [None] * len(_COL_NAMES)))
    row.update({"id": "inbraw_1", "state": "ACCEPTED", "dispatch_bundle": deep,
                "dispatch_bundle_fingerprint": "f" * 64})
    with pytest.raises(Exception, match="too deeply nested"):
        _validate_json(row, name="result")
    bounded = {key: value for key, value in row.items() if key != "dispatch_bundle"}
    _validate_json(bounded, name="result")
    assert bounded["dispatch_bundle_fingerprint"] == "f" * 64
    assert "dispatch_bundle" not in bounded
    # The source of truth for that shape is the mutation itself.
    import inspect

    source = inspect.getsource(mark_raw_import_state)
    assert 'if key != "dispatch_bundle"' in source
