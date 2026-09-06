"""Closed, server-authored shapes for immutable analytical Results.

The semantic member vocabulary remains measure/dimension/time/classification.
Shapes describe the rows a Result publishes after semantic execution; they do
not add semantic roles and they are never inferred from labels or values.
"""

from __future__ import annotations

from typing import Any

WATERFALL_SHAPE = "waterfall_v1"
WATERFALL_ROLES = frozenset({"base", "delta", "subtotal", "total"})

WATERFALL_FIELDS: tuple[tuple[str, str], ...] = (
    ("wf_datum_key", "datum_key"),
    ("wf_sequence", "sequence"),
    ("wf_waterfall_role", "waterfall_role"),
    ("wf_component", "component"),
    ("wf_label", "label"),
    ("wf_value_micros", "value_micros"),
    ("wf_start_total_micros", "start_total_micros"),
    ("wf_running_total_micros", "running_total_micros"),
    ("wf_currency", "currency"),
    ("wf_tax_basis", "tax_basis"),
    ("wf_is_complete", "is_complete"),
    ("wf_covered_row_count", "covered_row_count"),
    ("wf_total_row_count", "total_row_count"),
    ("wf_gap_codes", "gap_codes"),
    ("wf_evidence_key", "evidence_key"),
)


class ResultShapeRefused(ValueError):
    def __init__(self, code: str, message: str, *, outcome: str = "refused"):
        super().__init__(message)
        self.code = code
        self.message = message
        self.outcome = outcome

    def __str__(self) -> str:
        return self.message


def _integer_micros(value: Any, member_id: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResultShapeRefused(
            "invalid_micros",
            f"waterfall member `{member_id}` must be integer micros or null",
        )
    return value


def _count(row: dict[str, Any], binding: Any, default: int) -> int:
    if not isinstance(binding, dict) or not binding.get("member_id"):
        return default
    raw = row.get(str(binding["member_id"]))
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ResultShapeRefused(
            "invalid_coverage", "waterfall coverage counts must be non-negative integers"
        )
    return raw


def _gap_codes(row: dict[str, Any], binding: Any) -> list[str]:
    if not isinstance(binding, dict) or not binding.get("member_id"):
        return []
    raw = row.get(str(binding["member_id"]))
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        values = raw.split("|")
    elif isinstance(raw, list):
        values = raw
    else:
        raise ResultShapeRefused(
            "invalid_gap_codes", "waterfall gap codes must be a list or pipe-delimited string"
        )
    return sorted({str(value).strip() for value in values if str(value).strip()})


def _currency(row: dict[str, Any], binding: Any) -> str:
    if not isinstance(binding, dict):
        raise ResultShapeRefused(
            "missing_currency", "waterfall_v1 needs a server-authored currency binding"
        )
    raw = binding.get("value")
    if raw is None and binding.get("member_id"):
        raw = row.get(str(binding["member_id"]))
    value = str(raw or "").strip().upper()
    if len(value) != 3 or not value.isalpha():
        raise ResultShapeRefused("missing_currency", "waterfall_v1 needs one ISO currency")
    return value


def _validate_components(descriptor: dict[str, Any]) -> list[dict[str, Any]]:
    components = descriptor.get("components")
    if not isinstance(components, list) or len(components) != 8:
        raise ResultShapeRefused(
            "invalid_waterfall_shape", "waterfall_v1 requires exactly eight ordered components"
        )
    seen: set[str] = set()
    for index, item in enumerate(components):
        if not isinstance(item, dict):
            raise ResultShapeRefused(
                "invalid_waterfall_shape", f"waterfall component {index + 1} must be an object"
            )
        component = str(item.get("component") or "")
        member_id = str(item.get("member_id") or "")
        role = str(item.get("waterfall_role") or "")
        if not component or component in seen or not member_id or role not in WATERFALL_ROLES:
            raise ResultShapeRefused(
                "invalid_waterfall_shape",
                f"waterfall component {index + 1} is incomplete or duplicated",
            )
        seen.add(component)
    if components[0].get("waterfall_role") != "base":
        raise ResultShapeRefused(
            "invalid_waterfall_shape", "the first waterfall component must be the base"
        )
    return components


def _validate_basis(components: list[dict[str, Any]], descriptor: dict[str, Any]) -> None:
    order = [str(value) for value in descriptor.get("allowed_basis_transition") or []]
    if not order:
        raise ResultShapeRefused(
            "incompatible_tax_basis", "waterfall_v1 needs an explicit tax basis transition"
        )
    ranks = {basis: index for index, basis in enumerate(order)}
    previous = -1
    for component in components:
        basis = str(component.get("tax_basis") or "")
        rank = ranks.get(basis)
        if rank is None or rank < previous:
            raise ResultShapeRefused(
                "incompatible_tax_basis",
                "the waterfall tax basis may only follow its declared transition",
            )
        previous = rank


def _empty_shape(descriptor: dict[str, Any]) -> dict[str, Any]:
    return {
        "rows": [],
        "schema": {
            "contract": WATERFALL_SHAPE,
            "fields": [{"id": field_id, "name": name} for field_id, name in WATERFALL_FIELDS],
        },
        "manifest": {
            "result_shape": WATERFALL_SHAPE,
            "field_bindings": {name: field_id for field_id, name in WATERFALL_FIELDS},
            "verification": descriptor.get("verification") or {"keep_separate": True, "rows": []},
        },
    }


def serialize_waterfall_v1(
    *,
    result_id: str,
    source_rows: list[dict[str, Any]],
    descriptor: dict[str, Any],
    grain: str | None,
    evidence_by_member: dict[str, str],
) -> dict[str, Any]:
    """Turn one governed summary row into the closed ``waterfall_v1`` Result.

    All arithmetic happens here, before persistence. The runtime reads
    ``value_micros``, ``start_total_micros`` and ``running_total_micros`` and
    performs no sum, subtraction, basis transition or null repair.
    """
    if descriptor.get("id", WATERFALL_SHAPE) != WATERFALL_SHAPE:
        raise ResultShapeRefused("unknown_result_shape", "the descriptor is not waterfall_v1")
    if grain in set(descriptor.get("non_additive_grains") or []):
        raise ResultShapeRefused(
            "non_additive_grain",
            f"`{grain}` is a non-additive grouping and cannot be flattened into one waterfall",
        )
    components = _validate_components(descriptor)
    _validate_basis(components, descriptor)
    if not source_rows:
        return _empty_shape(descriptor)
    if len(source_rows) != 1:
        raise ResultShapeRefused(
            "ambiguous_waterfall_rows",
            "waterfall_v1 requires one server-aggregated summary row and never "
            "sums groups in the client",
        )

    source = source_rows[0]
    currency = _currency(source, descriptor.get("currency"))
    covered = _count(source, descriptor.get("covered_row_count"), 1)
    total = _count(source, descriptor.get("total_row_count"), covered)
    if covered > total:
        raise ResultShapeRefused(
            "invalid_coverage", "covered_row_count cannot exceed total_row_count"
        )
    gaps = _gap_codes(source, descriptor.get("gap_codes"))

    pins: dict[str, Any] = {}
    for pin_name, binding in (descriptor.get("manifest_pins") or {}).items():
        if not isinstance(binding, dict) or not binding.get("member_id"):
            raise ResultShapeRefused(
                "invalid_manifest_pin", f"manifest pin `{pin_name}` has no member binding"
            )
        value = source.get(str(binding["member_id"]))
        if binding.get("required") and (value is None or value == ""):
            raise ResultShapeRefused(
                "missing_required_pin",
                f"required Result evidence pin `{pin_name}` is unavailable",
                outcome="unavailable",
            )
        pins[str(pin_name)] = value

    rows: list[dict[str, Any]] = []
    running: int | None = 0
    incomplete = False
    for sequence, component in enumerate(components, start=1):
        member_id = str(component["member_id"])
        raw_value = _integer_micros(source.get(member_id), member_id)
        start_total = running
        if incomplete or raw_value is None:
            incomplete = True
            value = None
            running = None
        else:
            value = raw_value
            role = str(component["waterfall_role"])
            if role in {"base", "delta"}:
                running = value if role == "base" else int(running or 0) + value
            else:
                if value != running:
                    raise ResultShapeRefused(
                        "inconsistent_total",
                        f"server total `{member_id}` does not equal the preceding waterfall total",
                    )
                running = value

        evidence_key = evidence_by_member.get(member_id) or f"missing:{member_id}"
        rows.append(
            {
                "datum_key": f"{result_id}:{component['component']}",
                "sequence": sequence,
                "waterfall_role": component["waterfall_role"],
                "component": component["component"],
                "label": str(component.get("label") or component["component"]),
                "value_micros": value,
                "start_total_micros": start_total,
                "running_total_micros": running,
                "currency": currency,
                "tax_basis": component.get("tax_basis"),
                "is_complete": value is not None,
                "covered_row_count": covered,
                "total_row_count": total,
                "gap_codes": gaps if value is None else [],
                "evidence_key": evidence_key,
            }
        )

    shaped = _empty_shape(descriptor)
    shaped["rows"] = rows
    shaped["manifest"]["coverage"] = {"covered_row_count": covered, "total_row_count": total}
    shaped["manifest"]["pins"] = pins
    return shaped
