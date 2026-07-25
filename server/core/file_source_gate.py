"""toorow -- File-source required-field validation gate (Story 22.15, AD-7).

The lock checkpoint of the file-source ingestion framework. Before a template
locks, the gate confirms the required canonical fields were actually recovered
from the source and that no mapping is low-confidence or ambiguous. A missing
required field or an ambiguous/low-confidence binding is FLAGGED (surfaced with
the offending field) and NOT landed -- never a silent drop or mis-map.

The gate REUSES the Story 12.3 ``datastream_field_mapping.profile_fields``
confidence scoring + ambiguity detection (one vocabulary, no second scorer). It
is evaluated on a sample preview (``sample_only=True`` upstream); the template
locks only after a human confirms a PASSING preview, and that confirmation is
recorded durably through ``operations.execute_operation`` (audit + outbox).

Two layers: a PURE ``evaluate_required_field_gate`` (no I/O, offline-testable) +
``record_gate_confirmation`` (the durable human confirmation, execute_operation).
"""

from __future__ import annotations

from typing import Any

from core.datastream_field_mapping import profile_fields

ACTION_FILE_SOURCE_GATE_CONFIRMED = "file_source.template.gate_confirmed"


class FileSourceGateError(ValueError):
    """Base for gate errors (carries a stable code)."""

    code = "file_source_gate_error"


class GateNotPassed(FileSourceGateError):
    """A caller tried to lock/confirm a template whose gate did not pass."""

    code = "gate_not_passed"


def _infer_physical_type(values: list[Any]) -> str:
    """Infer a coarse physical type from sample values (for confidence scoring).

    Mirrors the spirit of csv_excel_import inference but stays dependency-light:
    the FIRST non-empty value decides date > number > string; an all-empty column
    is ``unknown`` so the gate keeps it blocking by default.
    """
    non_null = [str(v).strip() for v in values if v is not None and str(v).strip() != ""]
    if not non_null:
        return "unknown"
    head = non_null[0]
    from datetime import datetime  # noqa: PLC0415

    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y%m%d"):
        try:
            datetime.strptime(head, fmt)
            return "date"
        except ValueError:
            pass
    try:
        float(head.replace(",", ".").replace(" ", ""))
        return "decimal"
    except ValueError:
        return "string"


def evaluate_required_field_gate(
    template: dict[str, Any],
    mapping: dict[str, str],
    sample_rows: list[dict[str, Any]] | None = None,
    *,
    confidence_threshold: float = 0.75,
) -> dict[str, Any]:
    """Evaluate the required-field gate for a template + a source->canonical mapping.

    ``mapping`` is {source_column -> canonical_field_id}. ``sample_rows`` are the
    bounded preview rows (keyed by SOURCE column). Returns::

      {
        "passed": bool,
        "missing_required": [canonical_id, ...],   # required, not mapped at all
        "flagged": [ {field_id, canonical_target, blocking_reason, confidence}, ...],
        "ambiguities": [ ... ],                     # from profile_fields
      }

    A required canonical field with no mapping -> missing_required. A mapped field
    whose binding is blocking (low confidence / unknown / ambiguous) AND targets a
    required canonical field -> flagged. ``passed`` iff neither is present.
    """
    contract = template.get("contract") if "contract" in template else template
    required = list((contract or {}).get("required_fields") or [])
    required_set = set(required)

    mapped_targets = set(mapping.values())
    missing_required = [rid for rid in required if rid not in mapped_targets]

    sample = sample_rows or []
    field_records: list[dict[str, Any]] = []
    for source_col, canonical_id in mapping.items():
        values = [row.get(source_col) for row in sample]
        physical_type = _infer_physical_type(values)
        kind = "metric" if physical_type == "decimal" else (
            "date" if physical_type == "date" else "dimension"
        )
        field_records.append(
            {
                "field_id": source_col,
                "canonical_target": canonical_id,
                "physical_type": physical_type,
                "kind": kind,
            }
        )

    profile = profile_fields(
        field_records=field_records,
        sample_data=sample,
        confidence_threshold=confidence_threshold,
    )

    flagged: list[dict[str, Any]] = []
    for f in profile["fields"]:
        binding = f.get("binding") or {}
        target = binding.get("canonical_target")
        if binding.get("status") == "blocking" and target in required_set:
            flagged.append(
                {
                    "field_id": f["field_id"],
                    "canonical_target": target,
                    "blocking_reason": binding.get("blocking_reason"),
                    "confidence": (f.get("profile") or {}).get("confidence"),
                }
            )

    passed = not missing_required and not flagged
    return {
        "passed": passed,
        "missing_required": missing_required,
        "flagged": flagged,
        "ambiguities": profile.get("ambiguities", []),
    }


def record_gate_confirmation(
    conn,
    *,
    template_id: str,
    project_id: str,
    org_id: str,
    actor: str,
    gate_result: dict[str, Any],
    idempotency_key: str,
    content_hash: str,
    host_context: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Record a human confirmation of a PASSING gate through execute_operation (AD-7).

    A template locks only after a human confirms a passing preview. This writes the
    durable confirmation (audit + outbox + idempotent replay). Refuses to record a
    confirmation for a gate that did not pass (fail-closed).

    Returns the durable operation result dict.
    """
    if not gate_result.get("passed"):
        raise GateNotPassed(
            "the required-field gate did not pass; resolve missing/flagged fields "
            "before confirming (offending: "
            f"{gate_result.get('missing_required')} / {gate_result.get('flagged')})."
        )

    from core.operations import (  # noqa: PLC0415
        MutationResult,
        OperationSpec,
        _canonical_hash,
        execute_operation,
    )

    result_payload = {
        "template_id": template_id,
        "content_hash": content_hash,
        "confirmed": True,
    }

    def mutation(_conn, _operation_id: str) -> MutationResult:
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result_payload),
            result=result_payload,
            outbox_payload={"template_id": template_id, "confirmed": True},
        )

    spec = OperationSpec(
        command_type=ACTION_FILE_SOURCE_GATE_CONFIRMED,
        actor=actor,
        effective_org_id=org_id,
        resource_path=(
            f"organization:{org_id}",
            f"project:{project_id}",
            f"file_source_template:{template_id}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context or {},
        versions={"policy": "file-source-gate-v1"},
        request_payload={"template_id": template_id, "content_hash": content_hash},
        provider_references={},
        confirmation_mode="human",
        confirmation_reference=f"file-source-gate:{project_id}:{template_id}:{content_hash}",
        trace_id=trace_id,
    )
    op = execute_operation(conn, spec, mutation=mutation)
    return {"confirmed": True, "operation_id": op.operation_id, "replayed": op.replayed}
