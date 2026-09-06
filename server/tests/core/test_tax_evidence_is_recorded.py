"""The successful-pull seam writes honest, immutable Tax evidence."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import patch

from core import fee_tax_source_types as source_types
from core import queue, tax_evidence


def _datastream() -> dict:
    return {
        "id": "dst_1",
        "current_plan_version_id": "dsp_1",
        "current_mapping_version_id": "dsm_1",
    }


def test_pull_bridge_records_only_explicit_returned_evidence() -> None:
    captured: dict = {}

    def fake_record(_conn, **kwargs):
        captured.update(kwargs)
        return "dste_1"

    resolution = SimpleNamespace(
        value="PAID_MEDIA", source=source_types.RESOLVED_BY_DERIVATION
    )
    with (
        patch("core.fee_tax_source_types.resolve_source_type", return_value=resolution),
        patch("core.tax_evidence.record_tax_evidence", side_effect=fake_record),
    ):
        result = tax_evidence.record_tax_evidence_for_pull(
            object(),
            project_id="prj_1",
            datastream=_datastream(),
            pull_id="pull_1",
            pull_result={
                "row_count": 12,
                "tax_evidence": {
                    "tax_posture": "inclusive",
                    "observed_inputs": {
                        "native_amount_micros": 42,
                        "native_currency": "EUR",
                        "invented": "ignored",
                    },
                    "geography_evidence": {"country_field": "country"},
                },
            },
            recorded_by="user@example.test",
        )

    assert result == "dste_1"
    assert captured["source_type"] == "PAID_MEDIA"
    assert captured["source_type_origin"] == "observed_mapping"
    assert captured["source_type_confidence"] == "medium"
    assert captured["tax_posture"] == "inclusive"
    assert captured["tax_posture_origin"] == "observed_field"
    assert captured["observed_inputs"] == {
        "native_amount_micros": 42,
        "native_currency": "EUR",
    }
    assert captured["geography_evidence"] == {"country_field": "country"}
    assert captured["plan_version_id"] == "dsp_1"
    assert captured["mapping_version_id"] == "dsm_1"


def test_pull_bridge_records_unknowns_instead_of_guessing() -> None:
    captured: dict = {}
    resolution = SimpleNamespace(value="UNKNOWN", source=source_types.RESOLVED_BY_DEFAULT)

    with (
        patch("core.fee_tax_source_types.resolve_source_type", return_value=resolution),
        patch(
            "core.tax_evidence.record_tax_evidence",
            side_effect=lambda _conn, **kwargs: captured.update(kwargs) or "dste_2",
        ),
    ):
        tax_evidence.record_tax_evidence_for_pull(
            object(),
            project_id="prj_1",
            datastream=_datastream(),
            pull_id="pull_2",
            pull_result={"row_count": 0, "tax_posture": "provider-default"},
        )

    assert captured["source_type"] == "UNKNOWN"
    assert captured["source_type_origin"] == "unresolved"
    assert captured["source_type_confidence"] == "none"
    assert captured["tax_posture"] == "unknown"
    assert captured["tax_posture_origin"] == "unresolved"
    assert captured["observed_inputs"] == {}
    assert captured["geography_evidence"] == {}


def test_declared_source_type_is_high_confidence_operator_evidence() -> None:
    captured: dict = {}
    resolution = SimpleNamespace(
        value="COMMERCE_REVENUE", source=source_types.RESOLVED_BY_DECLARATION
    )
    with (
        patch("core.fee_tax_source_types.resolve_source_type", return_value=resolution),
        patch(
            "core.tax_evidence.record_tax_evidence",
            side_effect=lambda _conn, **kwargs: captured.update(kwargs) or "dste_3",
        ),
    ):
        tax_evidence.record_tax_evidence_for_pull(
            object(),
            project_id="prj_1",
            datastream=_datastream(),
            pull_id="pull_3",
            pull_result=None,
        )
    assert captured["source_type_origin"] == "operator_override"
    assert captured["source_type_confidence"] == "high"


def test_queue_success_path_calls_tax_writer_best_effort() -> None:
    source = inspect.getsource(queue._execute_job)
    assert "record_tax_evidence_for_pull" in source
    assert "tax_evidence_failed" in source
    assert source.index("record_tax_evidence_for_pull") > source.index("_finish_job")
