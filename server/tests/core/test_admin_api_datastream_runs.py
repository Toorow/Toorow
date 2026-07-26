from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from core.admin_api import (
    _enrich_datastream_runs,
    _normalize_run_interval,
    _read_datastream_runs,
)


def _connection(rows):
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    cursor.fetchall.return_value = rows
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn, cursor


def test_runs_are_universal_executions_and_ledger_only_enriches_membership():
    created = datetime(2026, 7, 22, 3, tzinfo=timezone.utc)
    changed = datetime(2026, 7, 22, 3, 4, 8, tzinfo=timezone.utc)
    rows = [
        (
            "dse_without_ledger",
            "failed",
            changed,
            0,
            "plan_1",
            "mapping_1",
            "provider_timeout",
            created,
            {"half_open_range": {"from": "2026-07-22", "to_exclusive": "2026-07-23"}},
            "operator",
        ),
        (
            "dse_with_ledger",
            "published",
            changed,
            42,
            "plan_1",
            "mapping_1",
            None,
            created,
            None,
            "operator",
        ),
    ]
    conn, cursor = _connection(rows)

    runs = _read_datastream_runs(conn, "ds_1", "proj_owner")
    _enrich_datastream_runs(
        runs,
        [
            {
                "id": "ledger_1",
                "execution_id": "dse_with_ledger",
                "outcome": "published",
                "row_count": 42,
                "rejected_row_count": 0,
                "snapshot_observed_at": "2026-07-22T03:00:00Z",
            }
        ],
        [{"execution_id": "dse_with_ledger"}],
        "dse_with_ledger",
    )

    assert [run["id"] for run in runs] == ["dse_without_ledger", "dse_with_ledger"]
    assert runs[0]["import_evidence"] is None
    assert runs[0]["recovery_interval"] == {
        "from": "2026-07-22",
        "to_exclusive": "2026-07-23",
    }
    assert runs[0]["duration_seconds"] == 248
    assert runs[1]["import_evidence"]["ledger_id"] == "ledger_1"
    assert runs[1]["publication_state"] == "current"
    assert cursor.execute.call_args.args[1] == ("ds_1", "proj_owner", 100)


def test_run_interval_normalizes_inclusive_end_and_rejects_invalid_ranges():
    assert _normalize_run_interval(
        {"interval": {"from": "2026-07-01", "to": "2026-07-05"}}
    ) == {"from": "2026-07-01", "to_exclusive": "2026-07-06"}
    assert _normalize_run_interval(
        {"half_open_range": {"from": "2026-07-05", "to_exclusive": "2026-07-05"}}
    ) is None
