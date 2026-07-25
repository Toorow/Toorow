"""Offline unit tests for the managed-feed import ledger (Story 12.8).

These run WITHOUT Postgres. They cover the PURE logic that the KEYSTONE ledger
exposes to Stories 12.9 (CSV/Excel) and 12.10 (Google Sheets):

  * idempotency-key hashing + payload fingerprint (same key + same payload -> same
    fingerprint; DIFFERENT payload -> DIFFERENT fingerprint -> the 409-conflict axis),
  * the content_hash EXCLUSION from the identity fingerprint (a new file with the
    same config is NOT an idempotency conflict),
  * the rejection-threshold gate (project-preference governed, fail-closed),
  * the RAW landing allocation (project-scoped, never a mart),
  * hex/format validation guards.

The live DB invariants (append-only trigger, write-once content_hash/execution_id,
terminal-outcome freeze, idempotent-replay, different-payload-reject, unchanged
no-op) are proven against real Postgres in
server/tests/integration/test_managed_feed_ledger_constraints.py (pg-gated).
"""

from __future__ import annotations

import os

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import pytest  # noqa: E402
from core.managed_feed_ledger import (  # noqa: E402  # noqa: E402
    DEFAULT_MAX_REJECTED_ROW_PCT,
    FORMAT_CSV,
    FORMAT_EXCEL,
    FORMAT_GOOGLE_SHEETS,
    GATE_REJECTION_THRESHOLD_EXCEEDED,
    MARKABLE_TERMINAL_OUTCOMES,
    OUTCOME_FAILED,
    OUTCOME_NOOP,
    OUTCOME_PUBLISHED,
    OUTCOME_REJECTED,
    TERMINAL_OUTCOMES,
    VALID_FORMATS,
    WRITE_MODE_REPLACE,
    ManagedFeedError,
    _is_hex64,
    _truncate,
    allocate_landing_relation,
    assert_managed_landing,
    build_import_identity,
    evaluate_rejection_gate,
    hash_idempotency_key,
    payload_fingerprint,
    resolve_rejection_threshold,
)

# ---------------------------------------------------------------------------
# Idempotency key hash + payload fingerprint.
# ---------------------------------------------------------------------------


def test_idempotency_key_hash_is_stable_64_hex():
    h = hash_idempotency_key("import-2026-07-23-A")
    assert len(h) == 64
    assert _is_hex64(h)
    # Deterministic.
    assert h == hash_idempotency_key("import-2026-07-23-A")
    # Different key -> different hash.
    assert h != hash_idempotency_key("import-2026-07-23-B")


def _identity(**overrides):
    base = dict(
        datastream_id="ds_01ABC",
        project_id="proj_1",
        plan_version_id="dsp_01",
        mapping_version_id="dmap_01",
        feed_format=FORMAT_CSV,
        write_mode=WRITE_MODE_REPLACE,
        source_metadata={"filename": "budgets.csv", "slot": "upload-1"},
    )
    base.update(overrides)
    return build_import_identity(**base)


def test_payload_fingerprint_stable_across_dict_ordering():
    a = payload_fingerprint(_identity(source_metadata={"a": 1, "b": 2}))
    b = payload_fingerprint(_identity(source_metadata={"b": 2, "a": 1}))
    assert a == b
    assert len(a) == 64 and _is_hex64(a)


def test_different_identity_payload_yields_different_fingerprint():
    base = payload_fingerprint(_identity())
    # A different mapping version is a different identity (-> conflict axis).
    assert base != payload_fingerprint(_identity(mapping_version_id="dmap_99"))
    # A different source slot is a different identity.
    assert base != payload_fingerprint(
        _identity(source_metadata={"filename": "budgets.csv", "slot": "upload-2"})
    )
    # A different write mode is a different identity.
    assert base != payload_fingerprint(_identity(write_mode="append"))


def test_content_hash_is_excluded_from_the_identity_fingerprint():
    # The identity payload MUST NOT include the snapshot content hash: re-importing a
    # NEW file with the SAME config is a legitimate re-import, not a 409 conflict.
    identity = _identity()
    assert "content_hash" not in identity
    # Two different snapshots of the same config share the identity fingerprint; the
    # content_hash axis is what distinguishes replay vs no-op vs new rows downstream.
    fp = payload_fingerprint(identity)
    assert fp == payload_fingerprint(_identity())


# ---------------------------------------------------------------------------
# Rejection-threshold gate (pure, project-preference governed, fail-closed).
# ---------------------------------------------------------------------------


def test_resolve_rejection_threshold_default_when_unset():
    pct, source = resolve_rejection_threshold(None)
    assert pct == DEFAULT_MAX_REJECTED_ROW_PCT
    assert source == "documented_default"


def test_resolve_rejection_threshold_uses_project_preference():
    pct, source = resolve_rejection_threshold({"max_rejected_row_pct": 5})
    assert pct == 5.0
    assert source == "project_preference"


def test_resolve_rejection_threshold_out_of_range_falls_back():
    for bad in (-1, 101):
        pct, source = resolve_rejection_threshold({"max_rejected_row_pct": bad})
        assert pct == DEFAULT_MAX_REJECTED_ROW_PCT
        assert source == "documented_default"


def test_no_rejections_never_blocks():
    assert (
        evaluate_rejection_gate(
            accepted_row_count=100, rejected_row_count=0, preferences=None
        )
        is None
    )


def test_all_rows_accepted_zero_total_never_blocks():
    assert (
        evaluate_rejection_gate(
            accepted_row_count=0, rejected_row_count=0, preferences=None
        )
        is None
    )


def test_rejections_below_threshold_do_not_block():
    # 10 rejected / 100 total = 10% < default 25% -> allowed (still downloadable).
    assert (
        evaluate_rejection_gate(
            accepted_row_count=90, rejected_row_count=10, preferences=None
        )
        is None
    )


def test_rejections_above_threshold_block_with_actionable_issue():
    # 40 rejected / 100 total = 40% > default 25% -> blocked.
    issue = evaluate_rejection_gate(
        accepted_row_count=60, rejected_row_count=40, preferences=None
    )
    assert issue is not None
    assert issue["code"] == GATE_REJECTION_THRESHOLD_EXCEEDED
    assert issue["repair"]["threshold_source"] == "documented_default"
    assert issue["repair"]["fix_source_or_mapping"]["rejected_pct"] == 40.0


def test_project_threshold_tightens_the_gate():
    # A tight 5% threshold blocks a 10% ratio that the default would allow.
    issue = evaluate_rejection_gate(
        accepted_row_count=90,
        rejected_row_count=10,
        preferences={"max_rejected_row_pct": 5},
    )
    assert issue is not None
    assert issue["repair"]["threshold_source"] == "project_preference"


def test_zero_pct_threshold_blocks_any_rejection():
    issue = evaluate_rejection_gate(
        accepted_row_count=999,
        rejected_row_count=1,
        preferences={"max_rejected_row_pct": 0},
    )
    assert issue is not None


def test_negative_counts_fail_closed():
    issue = evaluate_rejection_gate(
        accepted_row_count=-1, rejected_row_count=0, preferences=None
    )
    assert issue is not None
    assert issue["code"] == GATE_REJECTION_THRESHOLD_EXCEEDED


# ---------------------------------------------------------------------------
# RAW landing allocation (project-scoped, never a mart -- AD-8).
# ---------------------------------------------------------------------------


def test_landing_relation_uses_resolved_org_schema():
    rel = allocate_landing_relation(
        datastream_id="ds_01ABCDEF",
        project_id="proj_1",
        raw_schema="org_acme_raw",
    )
    assert rel == "org_acme_raw.managed_feed_ds_01ABCDEF"
    # It is a RAW landing, never a mart namespace.
    assert "mart" not in rel
    assert rel.startswith("org_acme_raw.")


def test_landing_relation_degrades_to_main_when_schema_unresolvable():
    rel = allocate_landing_relation(
        datastream_id="ds_01ABCDEF", project_id="proj_1", raw_schema=None
    )
    assert rel == "main.managed_feed_ds_01ABCDEF"


def test_landing_relation_is_per_datastream_stable():
    a = allocate_landing_relation(
        datastream_id="ds_X", project_id="p", raw_schema="org_x_raw"
    )
    b = allocate_landing_relation(
        datastream_id="ds_X", project_id="p", raw_schema="org_x_raw"
    )
    assert a == b  # replace-whole: one landing per datastream, stable name.


# ---------------------------------------------------------------------------
# Validation guards.
# ---------------------------------------------------------------------------


def test_valid_formats_are_the_closed_enum():
    assert VALID_FORMATS == frozenset(
        {FORMAT_CSV, FORMAT_EXCEL, FORMAT_GOOGLE_SHEETS}
    )


def test_is_hex64_guard():
    assert _is_hex64("a" * 64)
    assert not _is_hex64("A" * 64)  # upper-case is not the storage form
    assert not _is_hex64("a" * 63)
    assert not _is_hex64("g" * 64)
    assert not _is_hex64(None)  # type: ignore[arg-type]


def test_truncate_bounds_rejected_value():
    assert _truncate(None) is None
    assert _truncate("short") == "short"
    long = "x" * 1000
    out = _truncate(long)
    assert out is not None and out.endswith("...(truncated)")
    assert len(out) <= 512 + len("...(truncated)")


# ---------------------------------------------------------------------------
# M1: 'noop' is NOT a caller-markable terminal outcome.
# ---------------------------------------------------------------------------


def test_markable_terminal_outcomes_exclude_noop():
    # noop is minted only internally by _record_no_op; a caller must never flip a real
    # written import to 'noop' (that would mislabel landed rows as an unchanged no-op).
    assert OUTCOME_NOOP in TERMINAL_OUTCOMES
    assert OUTCOME_NOOP not in MARKABLE_TERMINAL_OUTCOMES
    assert MARKABLE_TERMINAL_OUTCOMES == frozenset(
        {OUTCOME_REJECTED, OUTCOME_PUBLISHED, OUTCOME_FAILED}
    )


# ---------------------------------------------------------------------------
# L4: a caller-supplied landing relation must target a managed_feed_* landing.
# ---------------------------------------------------------------------------


def test_assert_managed_landing_allows_managed_feed_and_none():
    assert assert_managed_landing(None) is None
    assert (
        assert_managed_landing("org_acme_raw.managed_feed_ds_1")
        == "org_acme_raw.managed_feed_ds_1"
    )
    assert assert_managed_landing("managed_feed_ds_1") == "managed_feed_ds_1"


def test_assert_managed_landing_rejects_a_mart_or_external_table():
    for bad in ("analytics.fact_daily_kpi", "org_acme_raw.marts_revenue", "some_table"):
        with pytest.raises(ManagedFeedError) as exc:
            assert_managed_landing(bad)
        assert exc.value.code == "invalid_landing_relation"
