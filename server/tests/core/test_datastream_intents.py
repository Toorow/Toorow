"""Tests for the Story 12.2 immutable Datastream intent contract."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import jsonschema

SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "core" / "schemas" / "datastream-intent.schema.json"
)


def _intent() -> dict:
    return {
        "contract_version": "1",
        "source": {
            "kind": "connector_pull",
            "writer_kind": "toorow",
            "connection_ref_id": "conn_01",
            "report_id": "campaign_daily",
            "selection": {
                "selection_mode": "subset",
                "metrics": ["clicks", "cost"],
                "dimensions": ["date", "campaign_id"],
                "grain": ["date", "campaign_id"],
                "filters": [],
            },
        },
        "destination": {"policy": "managed_raw"},
        "historical": {
            "start": "2026-01-01T00:00:00Z",
            "end_exclusive": "2026-07-20T00:00:00Z",
        },
        "schedule": {
            "mode": "daily",
            "interval_minutes": 1440,
            "timezone": "Europe/Paris",
            "watermark": {"kind": "date_window", "delay_minutes": 120},
            "late_arrival": {"lookback_minutes": 4320},
            "retry": {
                "max_attempts": 5,
                "initial_backoff_seconds": 60,
                "max_backoff_seconds": 3600,
            },
            "missed_run": {"mode": "coalesce", "max_catchup_windows": 7},
        },
    }


def _validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def test_schema_accepts_connector_intent() -> None:
    assert list(_validator().iter_errors(_intent())) == []


def test_schema_accepts_safe_incomplete_selection() -> None:
    intent = _intent()
    intent["source"]["selection"]["metrics"] = []
    intent["source"]["selection"]["dimensions"] = []
    intent["source"]["selection"]["grain"] = []
    assert list(_validator().iter_errors(intent)) == []


def test_schema_rejects_secret_bearing_unknown_keys_recursively() -> None:
    intent = _intent()
    intent["source"]["credentials"] = {"token": "secret"}
    errors = list(_validator().iter_errors(intent))
    assert errors
    assert any("Additional properties" in error.message for error in errors)


def test_schema_enforces_external_bigquery_ownership() -> None:
    intent = _intent()
    intent["source"] = {
        "kind": "external_bq",
        "writer_kind": "external",
        "external_object": {
            "project": "customer-project",
            "dataset": "analytics",
            "object": "daily_export",
            "writer_identity": "customer-pipeline",
        },
        "selection": {
            "selection_mode": "subset",
            "metrics": [],
            "dimensions": [],
            "grain": [],
            "filters": [],
        },
    }
    intent["destination"] = {"policy": "external_read_only"}
    assert list(_validator().iter_errors(intent)) == []

    contradictory = deepcopy(intent)
    contradictory["source"]["writer_kind"] = "toorow"
    assert list(_validator().iter_errors(contradictory))


def _catalog() -> dict:
    return {
        "contract_version": "1",
        "project_id": "proj_a",
        "connection_ref_id": "conn_01",
        "module": {"name": "generic", "display_name": "Generic", "module_kind": "kpi"},
        "field_discovery": {"mode": "static", "allowed_targets": []},
        "fields": [
            {"field_id": "date", "kind": "dimension"},
            {"field_id": "campaign_id", "kind": "dimension"},
            {"field_id": "clicks", "kind": "metric"},
            {"field_id": "cost", "kind": "metric"},
        ],
        "reports": [
            {
                "id": "campaign_daily",
                "selection_mode": "subset",
                "availability": {"status": "selectable"},
                "metrics": ["clicks", "cost"],
                "dimensions": ["date", "campaign_id"],
                "supported_grains": [["date"], ["date", "campaign_id"]],
                "compatibility": [],
                "filters": [{"field_id": "date", "operators": ["gte", "lte"]}],
                "quota_cost": {"read_points": 2, "unit": "request"},
                "cadence": {
                    "minimum_interval_minutes": 60,
                    "supported_modes": ["manual", "daily", "hourly"],
                },
            }
        ],
    }


def test_normalization_is_deterministic_and_derives_writer() -> None:
    from core.datastream_intents import normalize_intent

    first = _intent()
    first["source"].pop("writer_kind")
    second = deepcopy(first)
    second["source"]["selection"]["metrics"] = ["cost", "clicks"]
    normalized_first, hash_first = normalize_intent(first)
    normalized_second, hash_second = normalize_intent(second)
    assert normalized_first["source"]["writer_kind"] == "toorow"
    assert normalized_first["source"]["selection"]["metrics"] == ["clicks", "cost"]
    assert normalized_second == normalized_first
    assert hash_second == hash_first
    assert len(hash_first) == 64


def test_semantic_validation_keeps_safe_incomplete_draft() -> None:
    from core.datastream_intents import validate_intent

    intent = _intent()
    intent["source"]["selection"]["metrics"] = []
    result = validate_intent(intent, capabilities=_catalog())
    assert result.executable is False
    assert "incomplete_selection" in {issue.code for issue in result.issues}


def test_connector_validation_rejects_unknown_field_and_unsupported_cadence() -> None:
    from core.datastream_intents import validate_intent

    intent = _intent()
    intent["source"]["selection"]["metrics"].append("secret_metric")
    intent["schedule"]["interval_minutes"] = 60
    catalog = _catalog()
    catalog["reports"][0]["cadence"] = {
        "minimum_interval_minutes": 1440,
        "supported_modes": ["manual", "daily"],
    }
    intent["schedule"]["mode"] = "hourly"
    result = validate_intent(intent, capabilities=catalog)
    codes = {issue.code for issue in result.issues}
    assert {"unknown_report_field", "unsupported_cadence"} <= codes
    cadence = next(issue for issue in result.issues if issue.code == "unsupported_cadence")
    assert cadence.details["minimum_interval_minutes"] == 1440
    assert cadence.details["quota_cost"] == {"read_points": 2, "unit": "request"}
    assert cadence.repair["mode"] == "daily"


def test_invalid_timezone_and_retry_bounds_are_structural_errors() -> None:
    from core.datastream_intents import DatastreamIntentStructuralError, normalize_intent

    intent = _intent()
    intent["schedule"]["timezone"] = "Mars/Olympus"
    intent["schedule"]["retry"]["max_attempts"] = 100
    try:
        normalize_intent(intent)
    except DatastreamIntentStructuralError as exc:
        codes = {issue.code for issue in exc.issues}
        assert "invalid_intent_schema" in codes
        assert "invalid_timezone" in codes
    else:
        raise AssertionError("invalid intent should be rejected")


def test_valid_connector_intent_is_executable_and_fingerprinted() -> None:
    from core.datastream_intents import validate_intent

    result = validate_intent(_intent(), capabilities=_catalog())
    assert result.executable is True
    assert result.issues == ()
    assert result.capability_contract_version == "1"
    assert len(result.capability_fingerprint or "") == 64


def _scripted_conn(fetchone_results: list[object]):
    from unittest.mock import MagicMock

    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.side_effect = fetchone_results
    conn.cursor.return_value = cur
    return conn, cur


def test_save_appends_version_moves_pointer_and_audits_atomically() -> None:
    from datetime import datetime, timezone

    from core.datastream_intents import save_datastream_intent

    created = datetime(2026, 7, 19, 12, tzinfo=timezone.utc)
    inserted = (
        "dsp_01",
        "ds_01",
        "proj_a",
        1,
        "1",
        _intent(),
        "a" * 64,
        "1",
        "b" * 64,
        True,
        [],
        "member-1",
        created,
    )
    conn, cur = _scripted_conn([("ds_01", None), None, (1,), inserted])

    result = save_datastream_intent(
        datastream_id="ds_01",
        project_id="proj_a",
        intent=_intent(),
        identity="member-1",
        idempotency_key="request-01",
        conn=conn,
        capabilities=_catalog(),
        trace_id="trace-01",
    )

    sql_calls = [str(call.args[0]) for call in cur.execute.call_args_list]
    assert any("FOR UPDATE" in sql for sql in sql_calls)
    lock_index = next(i for i, sql in enumerate(sql_calls) if "FOR UPDATE" in sql)
    retry_index = next(
        i for i, sql in enumerate(sql_calls) if "idempotency_key_hash" in sql and "SELECT" in sql
    )
    assert lock_index < retry_index
    assert any("INSERT INTO app.datastream_plan_versions" in sql for sql in sql_calls)
    assert any("INSERT INTO app.datastream_schedule_state" in sql for sql in sql_calls)
    schedule_call = next(
        call
        for call in cur.execute.call_args_list
        if "INSERT INTO app.datastream_schedule_state" in str(call.args[0])
    )
    assert len(schedule_call.args[1]) == 7
    assert schedule_call.args[1][-1] is not None
    assert any("enabled = FALSE" in sql for sql in sql_calls)
    assert any("INSERT INTO app.audit_log" in sql for sql in sql_calls)
    assert result["version_number"] == 1
    assert result["idempotent_replay"] is False
    conn.commit.assert_called_once_with()
    conn.rollback.assert_not_called()


def test_save_idempotency_replays_same_payload_and_conflicts_on_drift() -> None:
    from datetime import datetime, timezone

    import pytest
    from core.datastream_intents import (
        DatastreamIntentConflict,
        normalize_intent,
        save_datastream_intent,
    )

    normalized, content_hash = normalize_intent(_intent())
    existing = (
        "dsp_01",
        "ds_01",
        "proj_a",
        1,
        "1",
        normalized,
        content_hash,
        "1",
        "b" * 64,
        True,
        [],
        "member-1",
        datetime(2026, 7, 19, 12, tzinfo=timezone.utc),
    )
    conn, cur = _scripted_conn([("ds_01", "dsp_01"), existing])
    replay = save_datastream_intent(
        datastream_id="ds_01",
        project_id="proj_a",
        intent=_intent(),
        identity="member-1",
        idempotency_key="same-key",
        conn=conn,
        capabilities=_catalog(),
    )
    assert replay["id"] == "dsp_01"
    assert replay["idempotent_replay"] is True
    assert cur.execute.call_count == 2

    changed = _intent()
    changed["historical"]["start"] = "2025-01-01T00:00:00Z"
    conflict_conn, _ = _scripted_conn([("ds_01", "dsp_01"), existing])
    with pytest.raises(DatastreamIntentConflict, match="idempotency_conflict"):
        save_datastream_intent(
            datastream_id="ds_01",
            project_id="proj_a",
            intent=changed,
            identity="member-1",
            idempotency_key="same-key",
            conn=conflict_conn,
            capabilities=_catalog(),
        )
    conflict_conn.rollback.assert_called_once_with()

    cross_stream_conn, _ = _scripted_conn([("ds_other", None), existing])
    with pytest.raises(DatastreamIntentConflict, match="idempotency_conflict"):
        save_datastream_intent(
            datastream_id="ds_other",
            project_id="proj_a",
            intent=_intent(),
            identity="member-1",
            idempotency_key="same-key",
            conn=cross_stream_conn,
            capabilities=_catalog(),
        )
    cross_stream_conn.rollback.assert_called_once_with()


def test_audit_failure_rolls_back_version_and_pointer() -> None:
    from datetime import datetime, timezone
    from unittest.mock import patch

    import pytest
    from core.datastream_intents import DatastreamIntentUnavailable, save_datastream_intent

    inserted = (
        "dsp_01",
        "ds_01",
        "proj_a",
        1,
        "1",
        _intent(),
        "a" * 64,
        "1",
        "b" * 64,
        True,
        [],
        "member-1",
        datetime(2026, 7, 19, 12, tzinfo=timezone.utc),
    )
    conn, _ = _scripted_conn([("ds_01", None), None, (1,), inserted])
    with patch("core.audit.insert_audit_row", side_effect=RuntimeError("audit down")):
        with pytest.raises(DatastreamIntentUnavailable, match="persistence failed"):
            save_datastream_intent(
                datastream_id="ds_01",
                project_id="proj_a",
                intent=_intent(),
                identity="member-1",
                idempotency_key="request-02",
                conn=conn,
                capabilities=_catalog(),
            )
    conn.rollback.assert_called_once_with()
    conn.commit.assert_not_called()


def test_replay_recovers_compiled_intent_without_current_capabilities() -> None:
    from datetime import datetime, timezone

    import pytest
    from core.datastream_intents import (
        DatastreamIntentConflict,
        compile_geographic_intent,
        normalize_intent,
        replay_datastream_intent,
    )
    from core.geographic_reporting import GeographicPosture

    normalized, _ = normalize_intent(_intent())
    compiled = compile_geographic_intent(normalized, GeographicPosture(), _catalog())
    compiled, content_hash = normalize_intent(compiled)
    existing = (
        "dsp_01",
        "ds_01",
        "proj_a",
        1,
        "1",
        compiled,
        content_hash,
        "1",
        "b" * 64,
        True,
        [],
        "member-1",
        datetime(2026, 7, 19, 12, tzinfo=timezone.utc),
    )
    conn, cur = _scripted_conn([existing])

    replay = replay_datastream_intent(
        project_id="proj_a",
        intent=_intent(),
        idempotency_key="same-key",
        conn=conn,
    )

    assert replay is not None
    assert replay["datastream_id"] == "ds_01"
    assert replay["idempotent_replay"] is True
    assert cur.execute.call_count == 1

    changed = _intent()
    changed["historical"]["start"] = "2025-01-01T00:00:00Z"
    conflict_conn, _ = _scripted_conn([existing])
    with pytest.raises(DatastreamIntentConflict, match="idempotency_conflict"):
        replay_datastream_intent(
            project_id="proj_a",
            intent=changed,
            idempotency_key="same-key",
            conn=conflict_conn,
        )
