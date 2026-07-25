"""Story 37.2 geographic Datastream plan compilation tests."""

from __future__ import annotations

from copy import deepcopy

from core.geographic_reporting import GeographicPosture


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
                "metrics": ["cost"],
                "dimensions": ["date", "campaign_id"],
                "grain": ["date", "campaign_id"],
                "filters": [],
            },
        },
        "destination": {"policy": "managed_raw"},
        "historical": {"start": None, "end_exclusive": None},
        "schedule": {
            "mode": "daily",
            "interval_minutes": 1440,
            "timezone": "Europe/Paris",
            "watermark": {"kind": "date_window", "delay_minutes": 60},
            "late_arrival": {"lookback_minutes": 1440},
            "retry": {
                "max_attempts": 3,
                "initial_backoff_seconds": 60,
                "max_backoff_seconds": 600,
            },
            "missed_run": {"mode": "coalesce", "max_catchup_windows": 3},
        },
    }


def _capabilities(*, compatible: bool = True) -> dict:
    grains = (
        [["date", "campaign_id"], ["date", "campaign_id", "geo_country"]]
        if compatible
        else [["date", "campaign_id"]]
    )
    dimensions = (
        ["date", "campaign_id", "geo_country"]
        if compatible
        else [
            "date",
            "campaign_id",
        ]
    )
    return {
        "contract_version": "1",
        "connection_ref_id": "conn_01",
        "module": {"name": "provider-x"},
        "fields": [
            {"field_id": "date", "kind": "dimension", "canonical_target": "date"},
            {
                "field_id": "campaign_id",
                "kind": "dimension",
                "canonical_target": "campaign_id",
            },
            {
                "field_id": "geo_country",
                "kind": "dimension",
                "canonical_target": "country",
            },
            {"field_id": "cost", "kind": "metric", "canonical_target": "cost"},
        ],
        "reports": [
            {
                "id": "campaign_daily",
                "selection_mode": "subset",
                "availability": {"status": "selectable"},
                "metrics": ["cost"],
                "dimensions": dimensions,
                "supported_grains": grains,
                "compatibility": [],
                "filters": [],
                "quota_cost": {"read_points": 3, "unit": "request"},
                "cadence": {
                    "minimum_interval_minutes": 60,
                    "supported_modes": ["daily"],
                },
            }
        ],
    }


def test_local_markets_adds_canonical_country_joint_grain_and_snapshot():
    from core.datastream_intents import compile_geographic_intent, validate_intent

    posture = GeographicPosture("local_markets", ("DE", "FR"))
    compiled = compile_geographic_intent(_intent(), posture, _capabilities())

    selection = compiled["source"]["selection"]
    assert selection["dimensions"] == ["campaign_id", "date", "geo_country"]
    assert selection["grain"] == ["campaign_id", "date", "geo_country"]
    assert compiled["geographic"] == {
        "mode": "local_markets",
        "country_codes": ["DE", "FR"],
        "posture_fingerprint": compiled["geographic"]["posture_fingerprint"],
        "compilation_status": "country_complete",
        "effective_country_field": "geo_country",
        "impact": {
            "grain_before": ["campaign_id", "date"],
            "grain_after": ["campaign_id", "date", "geo_country"],
            "added_dimensions": ["geo_country"],
            "tracked_country_count": 2,
            "quota_cost": {"read_points": 3, "unit": "request"},
            "cardinality_estimate": "provider_country_cardinality",
        },
    }
    assert validate_intent(compiled, capabilities=_capabilities()).executable is True


def test_local_incompatible_is_saved_as_blocking_actionable_draft():
    from core.datastream_intents import compile_geographic_intent, validate_intent

    compiled = compile_geographic_intent(
        _intent(),
        GeographicPosture("local_markets", ("FR",)),
        _capabilities(compatible=False),
    )
    result = validate_intent(compiled, capabilities=_capabilities(compatible=False))

    assert compiled["geographic"]["compilation_status"] == "blocked"
    assert result.executable is False
    issue = next(item for item in result.issues if item.code == "geographic_country_unavailable")
    assert issue.details["module_name"] == "provider-x"
    assert issue.details["report_id"] == "campaign_daily"
    assert issue.repair["action"] == "select_country_compatible_report"


def test_global_does_not_add_country_and_external_country_is_preserved():
    from core.datastream_intents import compile_geographic_intent

    global_compiled = compile_geographic_intent(_intent(), GeographicPosture(), _capabilities())
    assert global_compiled["source"]["selection"]["grain"] == ["campaign_id", "date"]
    assert global_compiled["geographic"]["compilation_status"] == "consolidated"

    external = deepcopy(_intent())
    external["source"] = {
        "kind": "external_bq",
        "writer_kind": "external",
        "external_object": {
            "project": "customer",
            "dataset": "analytics",
            "object": "daily",
            "writer_identity": "pipeline",
        },
        "selection": {
            "selection_mode": "subset",
            "metrics": ["cost"],
            "dimensions": ["date", "country"],
            "grain": ["date", "country"],
            "filters": [],
        },
    }
    external["destination"] = {"policy": "external_read_only"}
    preserved = compile_geographic_intent(external, GeographicPosture(), None)
    assert preserved["source"]["selection"]["grain"] == ["country", "date"]
    assert preserved["geographic"]["compilation_status"] == "preserved_full_grain"
    assert preserved["geographic"]["effective_country_field"] == "country"


def test_compilation_is_deterministic_idempotent_and_detects_staleness():
    from core.datastream_intents import (
        compile_geographic_intent,
        geographic_snapshot_matches,
        normalize_intent,
    )

    posture = GeographicPosture("local_markets", ("DE", "FR"))
    first = compile_geographic_intent(_intent(), posture, _capabilities())
    second = compile_geographic_intent(first, posture, _capabilities())
    assert second == first
    assert normalize_intent(second)[1] == normalize_intent(first)[1]
    assert geographic_snapshot_matches(first["geographic"], posture)
    assert not geographic_snapshot_matches(
        first["geographic"], GeographicPosture("local_markets", ("FR",))
    )
    capability_drift = compile_geographic_intent(first, posture, _capabilities(compatible=False))
    assert capability_drift["geographic"]["compilation_status"] == "blocked"
    assert capability_drift["source"]["selection"]["grain"] == ["campaign_id", "date"]

    switched_global = compile_geographic_intent(first, GeographicPosture(), _capabilities())
    assert switched_global["geographic"]["compilation_status"] == "consolidated"
    assert switched_global["source"]["selection"]["grain"] == ["campaign_id", "date"]


def test_save_persists_compiled_snapshot_inside_immutable_payload():
    import json
    from datetime import datetime, timezone

    from core.datastream_intents import save_datastream_intent

    from tests.core.test_datastream_intents import _scripted_conn

    inserted = (
        "dsp_geo",
        "ds_01",
        "proj_a",
        1,
        "1",
        {},
        "a" * 64,
        "1",
        "b" * 64,
        True,
        [],
        "member-1",
        datetime(2026, 7, 22, tzinfo=timezone.utc),
    )
    conn, cursor = _scripted_conn([("ds_01", None), None, (1,), inserted])

    save_datastream_intent(
        datastream_id="ds_01",
        project_id="proj_a",
        intent=_intent(),
        identity="member-1",
        idempotency_key="geo-plan-1",
        conn=conn,
        capabilities=_capabilities(),
        geographic_posture=GeographicPosture("local_markets", ("DE", "FR")),
    )

    insert_call = next(
        call
        for call in cursor.execute.call_args_list
        if "INSERT INTO app.datastream_plan_versions" in str(call.args[0])
    )
    payload = json.loads(insert_call.args[1][8])
    assert payload["geographic"]["mode"] == "local_markets"
    assert payload["geographic"]["country_codes"] == ["DE", "FR"]
    assert payload["source"]["selection"]["grain"] == [
        "campaign_id",
        "date",
        "geo_country",
    ]


def test_shared_project_posture_reader_defaults_and_locks():
    from unittest.mock import MagicMock

    from core.geographic_reporting import fetch_project_geographic_posture

    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = ("local_markets", ["FR", "DE"])

    posture = fetch_project_geographic_posture("proj_a", conn, for_update=True)

    assert posture == GeographicPosture("local_markets", ("DE", "FR"))
    sql = str(cursor.execute.call_args.args[0])
    assert "FOR UPDATE" in sql
