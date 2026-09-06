"""Governed Workbench rollback contract proofs for Story 47.5."""

from __future__ import annotations

import inspect
from pathlib import Path

from core import admin_api, datastream_rollback

ROOT = Path(__file__).resolve().parents[3]


def test_only_exact_workbench_rollback_routes_are_mounted() -> None:
    paths = {route.path for route in admin_api.router.routes}
    base = "/api/projects/{project_id}/datastreams/{datastream_id}/workbench"

    assert f"{base}/outputs/rollback-preparations" in paths
    assert f"{base}/outputs/rollback-preparations/{{preparation_id}}/confirm" in paths
    assert "/api/datastreams/{id}/rollback/preview" not in paths
    assert "/api/datastreams/{id}/rollback" not in paths
    assert "/api/datastreams/{id}/sample" not in paths


def test_rollback_confirmation_uses_common_operation_and_atomic_pointer_restore() -> None:
    confirm_source = inspect.getsource(datastream_rollback.confirm_rollback)
    recovery_source = (ROOT / "server/core/dataset_recovery.py").read_text(encoding="utf-8")

    assert "execute_operation(" in confirm_source
    assert 'command_type="datastream.rollback"' in confirm_source
    assert "idempotency_key=preparation_id" in confirm_source
    assert "manage_transaction=False" in confirm_source
    assert "operation_id=%s" in confirm_source
    for restored in (
        "current_published_execution_id",
        "current_plan_version_id",
        "current_mapping_version_id",
        "lifecycle_state = 'active'",
        "schedule_mode = COALESCE",
    ):
        assert restored in recovery_source


def test_rollback_preparation_records_common_operation_reference() -> None:
    migration = (ROOT / "infra/nango/migrations/138_datastream_workbench_evidence.sql").read_text(
        encoding="utf-8"
    )
    table = migration.split("CREATE TABLE app.datastream_rollback_preparations", 1)[1].split(
        ");", 1
    )[0]

    assert "operation_id TEXT REFERENCES app.operations(id) ON DELETE RESTRICT" in table
