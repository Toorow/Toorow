"""Offline contracts for Story 38.10 durable AD-36 scan jobs."""

from __future__ import annotations

import pathlib

import pytest
from core import inbound_scan_jobs as jobs

_ROOT = pathlib.Path(__file__).resolve().parents[3]


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = conn.rowcount

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.conn.calls.append((sql, params))
        self.rowcount = self.conn.rowcount

    def fetchone(self):
        return self.conn.rows.pop(0) if self.conn.rows else None

    def fetchall(self):
        return self.conn.rows.pop(0) if self.conn.rows else []


class _Conn:
    def __init__(self, rows=None, rowcount=1):
        self.rows = list(rows or [])
        self.rowcount = rowcount
        self.calls = []

    def cursor(self):
        return _Cursor(self)


def test_fifth_failure_is_terminal_dead_letter_with_recovery_evidence():
    conn = _Conn(rows=[(5, 5)])
    state = jobs.fail_attempt(
        conn, job_id="inbscan_" + "a" * 24, error_code="scan_time_budget_exceeded"
    )
    assert state == "dead_letter"
    update = next(call for call in conn.calls if "UPDATE app.inbound_scan_jobs" in call[0])
    assert update[1][0] == "DEAD_LETTER"
    assert "retry_inbound_scan_job" in update[1][2]
    assert "scan_time_budget_exceeded" in update[1]


def test_pre_exhaustion_failure_waits_for_the_same_cloud_task_retry():
    conn = _Conn(rows=[(2, 5)])
    state = jobs.fail_attempt(
        conn, job_id="inbscan_" + "b" * 24, error_code="malware_scanner_unavailable"
    )
    assert state == "retry_wait"
    update = next(call for call in conn.calls if "UPDATE app.inbound_scan_jobs" in call[0])
    assert update[1][0] == "RETRY_WAIT"


def test_authorized_recovery_resets_only_a_dead_letter(monkeypatch):
    import core.inbound_receipts as receipts

    transitions = []
    monkeypatch.setattr(receipts, "mark_state", lambda conn, **kwargs: transitions.append(kwargs))
    conn = _Conn(
        rows=[
            None,
            ("inbrx_1", 0, "scan_attempts_exhausted", {"action": "retry"}),
            (1,),
            ("FAILED",),
        ],
        rowcount=1,
    )
    assert jobs.recover_dead_letter(
        conn,
        job_id="inbscan_" + "c" * 24,
        actor="operator-1",
        datastream_id="ds-1",
        trace_id="a" * 32,
        idempotency_key="recover-request-1",
    )
    lock_sql, lock_params = conn.calls[1]
    assert "state='DEAD_LETTER'" in lock_sql
    assert lock_params == ("inbscan_" + "c" * 24, "ds-1")
    assert "FOR UPDATE" in lock_sql

    assert transitions[0]["state"] == "PROCESSING"
    assert transitions[0]["scan_recovery"] is True


def test_reconciliation_reads_only_dispatchable_rows_without_a_task():
    conn = _Conn(rows=[[("inbscan_" + "d" * 24,), ("inbscan_" + "e" * 24,)]])
    ids = jobs.queued_without_task(conn, limit=2)
    assert len(ids) == 2
    sql = conn.calls[-1][0]
    assert "'QUEUED','RETRY_WAIT'" in sql
    assert "task_name IS NULL" in sql
    assert "SKIP LOCKED" in sql


def test_migration_is_additive_tenant_scoped_and_terminal():
    sql = (_ROOT / "infra/nango/migrations/186_inbound_scan_jobs.sql").read_text(encoding="utf-8")
    for evidence in (
        "CREATE TABLE IF NOT EXISTS app.inbound_scan_jobs",
        "'DEAD_LETTER'",
        "attempt_count = max_attempts",
        "recovery_evidence IS NOT NULL",
        "ENABLE ROW LEVEL SECURITY",
        "FORCE ROW LEVEL SECURITY",
        "epic36_has_resource_access",
    ):
        assert evidence in sql


def test_infrastructure_declares_all_three_ad36_roles_and_real_bounds():
    terraform = (_ROOT / "infra/terraform/inbound_bridge.tf").read_text(encoding="utf-8")
    for contract in (
        'resource "google_cloud_tasks_queue" "inbound_scan"',
        'resource "google_cloud_scheduler_job" "inbound_scan_reconcile"',
        'resource "google_pubsub_topic" "inbound_manifest_dead_letter"',
        "dead_letter_policy",
        "max_attempts",
        "max_delivery_attempts",
        'name  = "clamav"',
        "inbound_scan_timeout_seconds",
        "inbound_scan_memory",
        'resource "google_pubsub_subscription" "inbound_manifest_dead_letter"',
    ):
        assert contract in terraform
    bridge = (_ROOT / "server/inbound/bridge.py").read_text(encoding="utf-8")
    assert "/v1/internal/inbound-scan-task" in bridge
    assert "persist_scan_intent=True" in bridge


def test_pubsub_handler_only_creates_jobs_after_ledger_commit():
    bridge = (_ROOT / "server/inbound/bridge.py").read_text(encoding="utf-8")
    enqueue = bridge.index("jobs = enqueue_manifest_jobs")
    commit = bridge.index("conn.commit()", enqueue)
    dispatch = bridge.index("dispatch_job(", commit)
    assert enqueue < commit < dispatch
    assert "process_inbound_delivery(" not in bridge[enqueue:dispatch]


def test_claim_accepts_only_due_queue_states_and_appends_attempt_history():
    row = (
        "inbscan_" + "f" * 24,
        "gs://bucket/manifest",
        0,
        "inbrx_1",
        "ds-1",
        "QUEUED",
        0,
        5,
        "a" * 32,
        0,
    )
    conn = _Conn(rows=[row])
    claimed = jobs.claim_job(conn, job_id=row[0])
    assert claimed is not None and claimed["attempt"] == 1
    select_sql = conn.calls[0][0]
    assert "state IN ('QUEUED','RETRY_WAIT')" in select_sql
    assert "next_attempt_at" in select_sql
    assert any("INSERT INTO app.inbound_scan_job_attempts" in sql for sql, _ in conn.calls)


def test_recovery_is_tenant_scoped_and_append_only(monkeypatch):
    import core.inbound_receipts as receipts

    transitions = []
    monkeypatch.setattr(receipts, "mark_state", lambda conn, **kwargs: transitions.append(kwargs))
    conn = _Conn(
        rows=[
            None,
            ("inbrx_1", 0, "scan_attempts_exhausted", {"action": "retry"}),
            (1,),
            ("FAILED",),
        ],
        rowcount=1,
    )
    assert jobs.recover_dead_letter(
        conn,
        job_id="inbscan_" + "1" * 24,
        datastream_id="ds-1",
        actor="operator",
        trace_id="a" * 32,
        idempotency_key="recover-request-1",
    )
    assert "FOR UPDATE" in conn.calls[1][0]
    assert "INSERT INTO app.inbound_scan_job_recoveries" in conn.calls[2][0]
    assert "recovery_count=recovery_count+1" in conn.calls[3][0]
    assert transitions[0]["idempotency_key"].endswith(":1")
    assert transitions[0]["scan_recovery"] is True
    assert "datastream_id=%s" in conn.calls[1][0]
    assert "datastream_id=%s" in conn.calls[3][0]
    assert "datastream_id=%s" in conn.calls[4][0]


def test_recovery_of_another_job_is_idempotent_while_receipt_is_processing(
    monkeypatch,
):
    import core.inbound_receipts as receipts

    transitions = []
    monkeypatch.setattr(
        receipts, "mark_state", lambda conn, **kwargs: transitions.append(kwargs)
    )
    conn = _Conn(
        rows=[
            None,
            ("inbrx_1", 2, "scan_attempts_exhausted", {"action": "retry"}),
            (3,),
            ("PROCESSING",),
        ]
    )

    assert jobs.recover_dead_letter(
        conn,
        job_id="inbscan_" + "2" * 24,
        datastream_id="ds-1",
        actor="operator",
        trace_id="b" * 32,
        idempotency_key="recover-request-2",
    )
    assert transitions == []


def test_recovery_replays_a_committed_result_by_idempotency_key():
    conn = _Conn(rows=[(4, "c" * 32)])

    result = jobs.recover_dead_letter(
        conn,
        job_id="inbscan_" + "3" * 24,
        datastream_id="ds-1",
        actor="operator",
        trace_id="d" * 32,
        idempotency_key="same-request",
    )

    assert result == {
        "status": "queued",
        "job_id": "inbscan_" + "3" * 24,
        "trace_id": "c" * 32,
        "recovery_no": 4,
        "replayed": True,
    }
    assert len(conn.calls) == 1
    assert "idempotency_key_hash" in conn.calls[0][0]


def test_recovery_rechecks_idempotency_after_concurrent_commit():
    conn = _Conn(rows=[None, None, (2, "e" * 32)])

    result = jobs.recover_dead_letter(
        conn,
        job_id="inbscan_" + "4" * 24,
        datastream_id="ds-1",
        actor="operator",
        trace_id="f" * 32,
        idempotency_key="concurrent-request",
    )

    assert result is not None and result["replayed"] is True
    assert result["trace_id"] == "e" * 32
    assert len(conn.calls) == 3

def test_reconciliation_idempotency_key_changes_with_recovery_generation(monkeypatch):
    import core.inbound_receipts as receipts

    calls = []
    monkeypatch.setattr(
        receipts, "mark_state", lambda conn, **kwargs: calls.append(kwargs)
    )
    first = _Conn(rows=[[("DEAD_LETTER", None, "ds-1", None, 1)]])
    second = _Conn(
        rows=[
            [
                ("DEAD_LETTER", None, "ds-1", None, 2),
                ("REJECTED", "rejected", "ds-1", None, 1),
            ]
        ]
    )

    assert jobs.reconcile_receipt(
        first, receipt_id="inbrx_1", trace_id=None
    ) == "FAILED"
    assert jobs.reconcile_receipt(
        second, receipt_id="inbrx_1", trace_id=None
    ) == "FAILED"
    assert calls[0]["idempotency_key"].endswith(":FAILED:1")
    assert calls[1]["idempotency_key"].endswith(":FAILED:3")
    assert calls[0]["idempotency_key"] != calls[1]["idempotency_key"]

def test_duplicate_only_receipt_fold_matches_sequential_rejection(monkeypatch):
    import core.inbound_receipts as receipts

    conn = _Conn(rows=[[("SUCCEEDED", "duplicate", "ds-1", None, 0)]])
    calls = []
    monkeypatch.setattr(receipts, "mark_state", lambda conn, **kwargs: calls.append(kwargs))
    assert jobs.reconcile_receipt(conn, receipt_id="inbrx_1", trace_id=None) == "REJECTED"
    assert calls[0]["error_code"] == "duplicate_content_skipped"


def test_lost_cloud_task_with_recorded_name_becomes_dispatchable():
    conn = _Conn(rows=[[("inbscan_" + "2" * 24,)]])
    assert jobs.queued_without_task(conn) == ["inbscan_" + "2" * 24]
    sql = conn.calls[-1][0]
    assert "dispatched_at < clock_timestamp() - interval '65 minutes'" in sql


def test_migration_scopes_receipt_raw_and_append_only_history():
    sql = (_ROOT / "infra/nango/migrations/186_inbound_scan_jobs.sql").read_text(encoding="utf-8")
    for contract in (
        "CREATE TABLE IF NOT EXISTS app.inbound_scan_job_attempts",
        "CREATE TABLE IF NOT EXISTS app.inbound_scan_job_recoveries",
        "r.id=NEW.receipt_id AND r.datastream_id=NEW.datastream_id",
        "r.receipt_id=NEW.receipt_id",
        "inbound scan recovery evidence is append-only",
        "inbound_scan_job_attempts_strict",
        "inbound_scan_job_recoveries_strict",
    ):
        assert contract in sql


def test_clamav_image_is_digest_pinned_and_dlt_has_inspection_subscription():
    variables = (_ROOT / "infra/terraform/variables.tf").read_text(encoding="utf-8")
    terraform = (_ROOT / "infra/terraform/inbound_bridge.tf").read_text(encoding="utf-8")
    assert "clamav/clamav:1.4.3@sha256:" in variables
    assert 'resource "google_pubsub_subscription" "inbound_manifest_dead_letter"' in terraform
    assert 'message_retention_duration = "604800s"' in terraform


def test_offline_contracts_catch_unclosed_migration_and_terraform_blocks():
    migration = (_ROOT / "infra/nango/migrations/186_inbound_scan_jobs.sql").read_text(
        encoding="utf-8"
    )
    expected_guard = (
        "USING ERRCODE = '23000';"
        + chr(10)
        + "    END IF;"
        + chr(10)
        + "    IF OLD.state"
    )
    assert expected_guard in migration
    terraform = (_ROOT / "infra/terraform/inbound_bridge.tf").read_text(encoding="utf-8")
    assert terraform.count("{") == terraform.count("}")
    assert terraform.rstrip().endswith("}")

def test_managed_oidc_audiences_are_service_roots_and_attempt_policy_is_shared(
    monkeypatch,
):
    monkeypatch.setenv("INBOUND_SCAN_MAX_ATTEMPTS", "7")
    assert jobs.configured_max_attempts() == 7

    source = (_ROOT / "server/core/inbound_scan_jobs.py").read_text(encoding="utf-8")
    terraform = (_ROOT / "infra/terraform/inbound_bridge.tf").read_text(encoding="utf-8")
    variables = (_ROOT / "infra/terraform/variables.tf").read_text(encoding="utf-8")
    migration_186 = (_ROOT / "infra/nango/migrations/186_inbound_scan_jobs.sql").read_text(
        encoding="utf-8"
    )
    migration_189 = (
        _ROOT / "infra/nango/migrations/189_inbound_scan_attempt_range_guard.sql"
    ).read_text(encoding="utf-8")
    assert '"audience": base_url' in source
    assert (
        terraform.count("audience              = google_cloud_run_v2_service.inbound_bridge.uri")
        == 2
    )
    assert 'name  = "INBOUND_SCAN_MAX_ATTEMPTS"' in terraform
    assert "value = tostring(var.inbound_scan_max_attempts)" in terraform
    assert "var.inbound_scan_max_attempts >= 5" in variables
    assert "var.inbound_scan_max_attempts <= 20" in variables
    assert "max_attempts BETWEEN 1 AND 20" in migration_186
    assert "NEW.max_attempts NOT BETWEEN 5 AND 20" in migration_189


@pytest.mark.parametrize("value", ["1", "21"])
def test_invalid_runtime_attempt_policy_fails_closed(monkeypatch, value):
    monkeypatch.setenv("INBOUND_SCAN_MAX_ATTEMPTS", value)
    with pytest.raises(EnvironmentError):
        jobs.configured_max_attempts()


def test_recovery_idempotency_migration_is_additive_and_hashed():
    migration = (
        _ROOT / "infra/nango/migrations/190_inbound_scan_recovery_idempotency.sql"
    ).read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS idempotency_key_hash TEXT" in migration
    assert "uq_inbound_scan_recovery_idempotency" in migration
    assert "CHECK (idempotency_key_hash IS NOT NULL) NOT VALID" in migration
    assert "WHERE idempotency_key_hash IS NOT NULL" in migration

def test_recovery_migration_reopens_only_an_audited_exhausted_receipt():
    migration = (
        _ROOT / "infra/nango/migrations/187_inbound_scan_recovery_receipt_transition.sql"
    ).read_text(encoding="utf-8")
    for contract in (
        "OLD.error_code = 'scan_attempts_exhausted'",
        "app.inbound_scan_job_recoveries",
        "j.state = 'QUEUED'",
        "AND NOT scan_recovery",
        "OR scan_recovery",
    ):
        assert contract in migration
