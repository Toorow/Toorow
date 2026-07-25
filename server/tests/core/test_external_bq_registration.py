"""Offline unit tests for read-only external BigQuery registration (Story 12.7).

These run WITHOUT Postgres and WITHOUT a live BigQuery client (no BQ credentials
exist -- live probes are Phase B). They prove:

  * NO-WRITE GUARANTEE by construction: every SQL the module can emit is read-only,
    and the write-verb guard rejects every write/DDL/DML/permission verb.
  * the safe-fail matrix maps each failure condition to its specific verdict +
    repair, and blocking verdicts never claim to publish.
  * the virtual pull commit carries pull_id + evidence provenance, and an unchanged
    observation is an auditable no-op (mints nothing).
  * the EXPLICIT scheduler guard excludes external_bq datastreams from provider
    dispatch.
  * ``observe_and_register`` composes the observation ledger with the 12.5 candidate
    registry (via a scripted fake connection) on a fresh ``ok`` verdict, records the
    ledger row on a no-op / blocking verdict, and never mints an execution on a
    blocking verdict.

The live-Postgres constraint tests (append-only trigger, real FK scope, real
scheduler-exclusion SQL) live in
server/tests/integration/test_external_bq_registration_constraints.py (pg-gated).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import external_bq_registration as ebq  # noqa: E402
from core.external_bq_registration import (  # noqa: E402
    BLOCKING_VERDICTS,
    VERDICT_ABSENT,
    VERDICT_CHANGED_AFTER_PREVIEW,
    VERDICT_EMPTY,
    VERDICT_MOVED,
    VERDICT_OK,
    VERDICT_PERMISSION_REVOKED,
    VERDICT_REGION_INCOMPATIBLE,
    VERDICT_UNCHANGED_NOOP,
    ExternalBQWriteAttempt,
    assert_read_only_sql,
    build_probe_plan,
    build_virtual_pull_commit,
    classify_observation,
    content_fingerprint,
    exclude_external_bq_dispatch,
    is_blocking_verdict,
    is_external_bq_datastream,
    is_unchanged_noop,
    object_ref,
)

_EXTERNAL_OBJECT = {
    "project": "acme-analytics",
    "dataset": "warehouse",
    "object": "fct_orders",
    "writer_identity": "dbt-runner@acme.iam.gserviceaccount.com",
}
_HASH_A = "a" * 64
_HASH_B = "b" * 64


# ---------------------------------------------------------------------------
# NO-WRITE GUARANTEE.
# ---------------------------------------------------------------------------


def test_read_only_select_is_accepted():
    assert assert_read_only_sql("SELECT 1") == "SELECT 1"
    assert assert_read_only_sql("WITH x AS (SELECT 1) SELECT * FROM x").startswith("WITH")


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a = 1",
        "DELETE FROM t",
        "MERGE INTO t USING s ON t.a = s.a",
        "TRUNCATE TABLE t",
        "DROP TABLE t",
        "ALTER TABLE t ADD COLUMN a INT64",
        "CREATE TABLE t AS SELECT 1",
        "CREATE OR REPLACE TABLE t AS SELECT 1",
        "GRANT SELECT ON t TO 'x'",
        "REVOKE SELECT ON t FROM 'x'",
        "LOAD DATA INTO t",
        "EXPORT DATA OPTIONS(uri='gs://x') AS SELECT 1",
        "CALL some_proc()",
    ],
)
def test_every_write_verb_is_rejected(statement):
    with pytest.raises(ExternalBQWriteAttempt):
        assert_read_only_sql(statement)


def test_probe_plan_contains_only_read_only_statements():
    plan = build_probe_plan(_EXTERNAL_OBJECT, sample_limit=50)
    assert plan["read_only"] is True
    assert plan["object_ref"] == "acme-analytics.warehouse.fct_orders"
    # Re-assert EVERY statement in the plan is read-only (defence in depth): if any
    # write verb ever leaks into a template this fails loudly.
    for step in plan["steps"]:
        assert_read_only_sql(step["sql"])  # raises if not read-only
    # The bounded sample carries the LIMIT literal only.
    sample = next(s for s in plan["steps"] if s["name"] == "sample")
    assert sample["limit"] == 50
    assert "LIMIT 50" in sample["sql"]


def test_probe_plan_limit_is_bounded():
    plan = build_probe_plan(_EXTERNAL_OBJECT, sample_limit=999999)
    sample = next(s for s in plan["steps"] if s["name"] == "sample")
    assert sample["limit"] == 1000  # clamped


def test_module_emits_no_write_sql_anywhere():
    """Scan the whole module's emitted probe surface -- zero write verbs.

    Builds a probe plan and asserts the concatenated SQL of every step contains no
    standalone write verb. This is the module-level proof requested by AC1 ("a guard
    test over the module's SQL/verbs").
    """
    import re

    plan = build_probe_plan(_EXTERNAL_OBJECT)
    all_sql = " \n ".join(step["sql"] for step in plan["steps"])
    tokens = {t.upper() for t in re.findall(r"[A-Za-z_]+", all_sql)}
    leaked = tokens & ebq.WRITE_VERBS
    assert not leaked, f"write verbs leaked into probe SQL: {sorted(leaked)}"


# ---------------------------------------------------------------------------
# Object ref validation.
# ---------------------------------------------------------------------------


def test_object_ref_requires_all_coordinates():
    with pytest.raises(ebq.ObservationInputError):
        object_ref({"project": "p", "dataset": "d"})
    with pytest.raises(ebq.ObservationInputError):
        object_ref({"project": "p", "dataset": "d", "object": "  "})


# ---------------------------------------------------------------------------
# Safe-fail matrix.
# ---------------------------------------------------------------------------


def _ok_kwargs(**overrides):
    base = {
        "exists": True,
        "readable": True,
        "row_count": 100,
        "object_location": "EU",
        "project_region": "EU",
        "moved": False,
        "expected_content_hash": None,
        "observed_content_hash": _HASH_A,
    }
    base.update(overrides)
    return base


def test_ok_verdict_when_object_is_fresh_and_readable():
    verdict, repair = classify_observation(**_ok_kwargs())
    assert verdict == VERDICT_OK
    assert repair == {}


def test_permission_revoked_takes_precedence():
    verdict, repair = classify_observation(**_ok_kwargs(readable=False, exists=False))
    assert verdict == VERDICT_PERMISSION_REVOKED
    assert repair["code"] == "external_read_permission_revoked"


def test_absent_object():
    verdict, _ = classify_observation(**_ok_kwargs(exists=False))
    assert verdict == VERDICT_ABSENT


def test_moved_object():
    verdict, _ = classify_observation(**_ok_kwargs(moved=True))
    assert verdict == VERDICT_MOVED


def test_region_incompatible():
    verdict, repair = classify_observation(
        **_ok_kwargs(object_location="US", project_region="EU")
    )
    assert verdict == VERDICT_REGION_INCOMPATIBLE
    assert repair["object_location"] == "US"
    assert repair["project_region"] == "EU"


def test_changed_after_preview():
    verdict, _ = classify_observation(
        **_ok_kwargs(expected_content_hash=_HASH_A, observed_content_hash=_HASH_B)
    )
    assert verdict == VERDICT_CHANGED_AFTER_PREVIEW


def test_empty_object_blocked_by_default():
    verdict, _ = classify_observation(**_ok_kwargs(row_count=0))
    assert verdict == VERDICT_EMPTY


def test_all_failure_verdicts_are_blocking_and_ok_is_not():
    for v in (
        VERDICT_ABSENT,
        VERDICT_EMPTY,
        VERDICT_MOVED,
        VERDICT_PERMISSION_REVOKED,
        VERDICT_REGION_INCOMPATIBLE,
        VERDICT_CHANGED_AFTER_PREVIEW,
    ):
        assert is_blocking_verdict(v) is True
        assert v in BLOCKING_VERDICTS
    assert is_blocking_verdict(VERDICT_OK) is False
    assert is_blocking_verdict(VERDICT_UNCHANGED_NOOP) is False


# ---------------------------------------------------------------------------
# Virtual pull commit + unchanged no-op.
# ---------------------------------------------------------------------------


def test_virtual_pull_commit_carries_provenance():
    commit = build_virtual_pull_commit(
        external_object=_EXTERNAL_OBJECT,
        watermark=datetime(2026, 7, 22, 3, 0, tzinfo=timezone.utc),
        content_hash=_HASH_A,
        schema_hash=_HASH_B,
        row_count=100,
        object_location="EU",
    )
    assert commit["pull_id"].startswith("vpull_")
    assert commit["kind"] == "virtual_external_bq"
    assert commit["object_ref"] == "acme-analytics.warehouse.fct_orders"
    assert commit["writer_identity"] == _EXTERNAL_OBJECT["writer_identity"]
    assert commit["content_hash"] == _HASH_A
    assert commit["schema_hash"] == _HASH_B
    assert commit["row_count"] == 100
    assert commit["watermark"] == "2026-07-22T03:00:00+00:00"


def test_virtual_pull_id_is_deterministic_from_evidence():
    # HIGH-1: the vpull id MUST be derived deterministically from the observation
    # evidence so a replay reproduces the SAME commit (byte-identical projection-plan
    # snapshot -> 12.5 idempotency fingerprint matches -> no false IdempotencyConflict).
    a = build_virtual_pull_commit(
        external_object=_EXTERNAL_OBJECT, watermark=None,
        content_hash=_HASH_A, schema_hash=_HASH_B, row_count=1, object_location="EU",
    )
    b = build_virtual_pull_commit(
        external_object=_EXTERNAL_OBJECT, watermark=None,
        content_hash=_HASH_A, schema_hash=_HASH_B, row_count=1, object_location="EU",
    )
    assert a["pull_id"] == b["pull_id"]
    # Different content evidence -> different vpull id (a genuinely new candidate).
    c = build_virtual_pull_commit(
        external_object=_EXTERNAL_OBJECT, watermark=None,
        content_hash=_HASH_B, schema_hash=_HASH_B, row_count=1, object_location="EU",
    )
    assert c["pull_id"] != a["pull_id"]


def test_content_fingerprint_is_deterministic_and_order_independent():
    rows_1 = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
    rows_2 = [{"b": 2, "a": 1}, {"b": 4, "a": 3}]  # same rows, keys reordered
    assert content_fingerprint(rows_1, 2) == content_fingerprint(rows_2, 2)
    # HIGH-2: a bounded sample without ORDER BY can return the SAME rows in a
    # different ROW order -- the fingerprint must be row-order-insensitive.
    rows_3 = [{"a": 3, "b": 4}, {"a": 1, "b": 2}]  # same rows, rows reordered
    assert content_fingerprint(rows_1, 2) == content_fingerprint(rows_3, 2)
    # A changed row count changes the fingerprint.
    assert content_fingerprint(rows_1, 2) != content_fingerprint(rows_1, 3)
    # A genuinely different row set changes the fingerprint.
    assert content_fingerprint(rows_1, 2) != content_fingerprint([{"a": 9, "b": 9}], 2)


def test_unchanged_noop_detection():
    assert is_unchanged_noop(
        prior_content_hash=_HASH_A, prior_schema_hash=_HASH_B,
        observed_content_hash=_HASH_A, observed_schema_hash=_HASH_B,
    ) is True
    # A changed content hash is NOT a no-op.
    assert is_unchanged_noop(
        prior_content_hash=_HASH_A, prior_schema_hash=_HASH_B,
        observed_content_hash=_HASH_B, observed_schema_hash=_HASH_B,
    ) is False
    # No prior committed observation -> never a no-op (first observation).
    assert is_unchanged_noop(
        prior_content_hash=None, prior_schema_hash=None,
        observed_content_hash=_HASH_A, observed_schema_hash=_HASH_B,
    ) is False


# ---------------------------------------------------------------------------
# Scheduler guard -- EXPLICIT invariant.
# ---------------------------------------------------------------------------


def test_is_external_bq_datastream_by_source_kind():
    assert is_external_bq_datastream({"source_kind": "external_bq"}) is True
    assert is_external_bq_datastream({"external_dispatch_excluded": True}) is True
    assert is_external_bq_datastream({"source_kind": "connector_pull"}) is False
    assert is_external_bq_datastream({"source_kind": "managed_feed"}) is False


def test_exclude_external_bq_dispatch_filters_only_external_bq():
    rows = [
        {"ds_id": "a", "source_kind": "connector_pull"},
        {"ds_id": "b", "source_kind": "external_bq"},
        {"ds_id": "c", "source_kind": "managed_feed"},
        {"ds_id": "d", "external_dispatch_excluded": True},
    ]
    kept = exclude_external_bq_dispatch(rows)
    kept_ids = {r["ds_id"] for r in kept}
    assert kept_ids == {"a", "c"}


# ---------------------------------------------------------------------------
# observe_and_register composition (scripted fake connection -- no Postgres).
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self.description = None
        self._result = None
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self._conn.executed.append(s)
        if "FROM app.external_bq_observations" in s and "verdict = 'ok'" in s:
            # Last committed observation lookup (unchanged-noop check).
            self.description = [("content_hash",), ("schema_hash",), ("watermark",), ("row_count",)]
            self._result = self._conn.prior_observation
        elif s.startswith("INSERT INTO app.external_bq_observations"):
            self.description = [
                ("id",), ("datastream_id",), ("project_id",), ("verdict",),
                ("execution_id",), ("observed_at",),
            ]
            # Capture the verdict written (params order matches the INSERT).
            self._conn.recorded_verdicts.append(params[13])
            self._result = ("ebqo_x", params[1], params[2], params[13], params[16], None)
        elif s.startswith("SAVEPOINT") or s.startswith("RELEASE") or s.startswith("ROLLBACK"):
            self._result = None
        elif s.startswith("INSERT INTO app.datastream_executions"):
            self._conn.execution_inserts += 1
            self.rowcount = 1
            self._result = None
        elif "FROM app.datastream_executions" in s and "WHERE id = %s AND project_id" in s:
            # _fetch_execution after the insert -> full row.
            self.description = [
                ("id",), ("datastream_id",), ("project_id",), ("plan_version_id",),
                ("mapping_version_id",), ("projection_plan_ref",), ("state",),
                ("state_changed_at",), ("content_hash",), ("row_count",),
                ("error_code",), ("error_detail",), ("idempotency_key_hash",),
                ("created_by",), ("created_at",), ("updated_at",),
            ]
            self._result = (
                "dse_minted", self._conn.datastream_id, self._conn.project_id,
                "dsp_x", "dmap_x", {}, "created", None, None, None, None, None,
                None, "actor", None, None,
            )
        elif "INSERT INTO app.audit_log" in s:
            self.rowcount = 1
            self._result = None
        else:
            self._result = None

    def fetchone(self):
        return self._result


class _FakeConn:
    def __init__(self, *, prior_observation=None):
        self.executed = []
        self.datastream_id = "ds_ext"
        self.project_id = "proj_ext"
        self.prior_observation = prior_observation
        self.recorded_verdicts = []
        self.execution_inserts = 0
        self.committed = False

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        pass


def _probe(**overrides):
    base = {
        "exists": True,
        "readable": True,
        "row_count": 100,
        "schema_hash": _HASH_B,
        "object_location": "EU",
        "sample_rows": [{"a": 1}],
        "watermark": datetime(2026, 7, 22, tzinfo=timezone.utc),
        "moved": False,
    }
    base.update(overrides)
    return base


def _register(conn, **overrides):
    kwargs = {
        "datastream_id": "ds_ext",
        "project_id": "proj_ext",
        "external_object": _EXTERNAL_OBJECT,
        "plan_version_id": "dsp_x",
        "mapping_version_id": "dmap_x",
        "projection_plan": {"executable": True, "measures": ["revenue"]},
        "probe_result": _probe(),
        "actor": "lead@acme",
        "idempotency_key": "idem-1",
        "conn": conn,
        "project_region": "EU",
    }
    kwargs.update(overrides)
    return ebq.observe_and_register(**kwargs)


def test_ok_verdict_mints_execution_and_records_observation():
    conn = _FakeConn(prior_observation=None)
    result = _register(conn)
    assert result["verdict"] == VERDICT_OK
    assert "execution" in result
    assert result["execution"]["id"] == "dse_minted"
    assert "virtual_pull_commit" in result
    assert result["virtual_pull_commit"]["pull_id"].startswith("vpull_")
    # Exactly one execution insert, and the observation row carries the ok verdict.
    assert conn.execution_inserts == 1
    assert VERDICT_OK in conn.recorded_verdicts


def test_unchanged_content_is_auditable_noop_no_execution():
    # Prior committed observation with the SAME content+schema as the probe.
    observed_hash = content_fingerprint(_probe()["sample_rows"], _probe()["row_count"])
    conn = _FakeConn(prior_observation=(observed_hash, _HASH_B, None, 100))
    result = _register(conn)
    assert result["verdict"] == VERDICT_UNCHANGED_NOOP
    assert "execution" not in result
    # No execution minted; observation recorded as a no-op.
    assert conn.execution_inserts == 0
    assert conn.recorded_verdicts == [VERDICT_UNCHANGED_NOOP]


def test_blocking_verdict_records_observation_but_mints_no_execution():
    conn = _FakeConn(prior_observation=None)
    result = _register(conn, probe_result=_probe(exists=False, readable=False))
    assert result["verdict"] == VERDICT_PERMISSION_REVOKED
    assert "execution" not in result
    assert conn.execution_inserts == 0
    assert conn.recorded_verdicts == [VERDICT_PERMISSION_REVOKED]


def test_region_incompatible_blocks_and_mints_no_execution():
    conn = _FakeConn(prior_observation=None)
    result = _register(conn, project_region="US")  # object is EU
    assert result["verdict"] == VERDICT_REGION_INCOMPATIBLE
    assert conn.execution_inserts == 0


def test_create_execution_takes_datastream_for_update_lock():
    # Story 12.12 M2: create_execution (an execution/replace mint) must take the SAME
    # app.datastreams FOR UPDATE pointer lock that rollback_dataset/commit_publication
    # hold, so the two operations serialize strictly. Proven here through the ok path
    # that mints an execution (observe_and_register -> create_execution).
    conn = _FakeConn(prior_observation=None)
    _register(conn)
    locked = [
        s for s in conn.executed
        if "FROM app.datastreams" in s and "FOR UPDATE" in s
    ]
    assert locked, "create_execution must SELECT app.datastreams ... FOR UPDATE (M2)"
