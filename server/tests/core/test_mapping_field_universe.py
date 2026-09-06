"""The mapping chain had two breaks that produced silence rather than an error.

Measured on 2026-07-30, before these repairs: 42 Datastreams carried a plan
version and **zero** carried a mapping version, so nothing downstream of mapping
had ever run. The cause was not a policy or a missing screen. It was two
mechanical defects, each of which turned a real answer into an empty one:

1. the profiling route read `report["field_catalog"]`, a key that exists in no
   connector manifest and in no `$def` of the capabilities schema, so the field
   universe was empty for all 129 report profiles of all 38 connectors;
2. two source aggregations (`impression_weighted`, `latest`) are outside the
   mapping schema's enum, so any report containing one of those 14 fields was
   rejected wholesale by `normalize_mapping`.

These tests pin both repairs, and the last one walks the whole class rather than
the one connector that was reported.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from core.datastream_field_mapping import (
    _mapping_aggregation,
    compute_source_schema_hash,
    normalize_mapping,
    profile_fields,
)
from core.datastream_projection import compile_projection

ROOT = Path(__file__).resolve().parents[3]
MODULES = ROOT / "server" / "modules"

#: The enum `datastream-field-mapping.schema.json` accepts on a suggestion.
SCHEMA_AGGREGATIONS = {"none", "sum", "avg", "min", "max", "count", "custom"}


def _capabilities(module: str) -> dict:
    manifest = json.loads((MODULES / module / "manifest.json").read_text("utf-8"))
    return manifest.get("source_capabilities") or {}


#: Connector manifests on 2026-08-31. A FLOOR, not an equality. Criterion 13 of
#: `docs/product-architecture/module-boundaries.md`: three tests here iterate
#: `_all_manifests()` and assert inside the loop, so an empty glob asserts
#: nothing at all and the file stays green.
_MANIFESTS_AT_2026_08_31 = 39


def test_the_manifest_population_is_whole():
    """The tests below assert INSIDE a loop; this one runs when the loop is empty."""
    found = sorted(MODULES.glob("*/manifest.json"))
    assert len(found) >= _MANIFESTS_AT_2026_08_31, (
        f"{len(found)} connector manifests found under {MODULES}, "
        f"{_MANIFESTS_AT_2026_08_31} on 2026-08-31 -- the report-profile checks "
        "below iterate this list and prove nothing when it is short."
    )


def _all_manifests():
    for path in sorted(MODULES.glob("*/manifest.json")):
        try:
            manifest = json.loads(path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):  # pragma: no cover - a broken manifest
            continue
        capabilities = manifest.get("source_capabilities")
        if capabilities and capabilities.get("reports"):
            yield path.parent.name, capabilities


# --- Defect 1: the field universe --------------------------------------------


def test_a_report_resolves_the_fields_it_declares():
    from core.datastream_mapping_api import _report_field_records

    records = _report_field_records(_capabilities("gsc"), "catalog_daily")

    assert records, "catalog_daily resolved no field at all"
    resolved = [record["field_id"] for record in records]
    assert "clicks" in resolved
    assert "date" in resolved
    # Dimensions precede metrics so the grain reads before the measures.
    assert resolved.index("date") < resolved.index("clicks")


def test_the_field_catalog_key_the_route_used_to_read_exists_nowhere():
    """The regression that mattered. If a manifest ever grows `field_catalog`,
    this test is the place to decide whether it becomes contractual -- silently
    reading it again is what cost every connector its mapping."""
    offenders = [
        f"{module}:{report.get('id')}"
        for module, capabilities in _all_manifests()
        for report in capabilities["reports"]
        if isinstance(report, dict) and report.get("field_catalog")
    ]
    assert offenders == []


def test_an_unknown_report_resolves_to_nothing_rather_than_to_everything():
    from core.datastream_mapping_api import _report_field_records

    assert _report_field_records(_capabilities("gsc"), "no_such_report") == []


def test_a_report_naming_an_undeclared_field_skips_it_instead_of_inventing_one():
    from core.datastream_mapping_api import _report_field_records

    capabilities = {
        "fields": [{"field_id": "clicks", "kind": "metric", "physical_type": "integer"}],
        "reports": [{"id": "r", "dimensions": ["ghost"], "metrics": ["clicks"]}],
    }

    records = _report_field_records(capabilities, "r")

    assert [record["field_id"] for record in records] == ["clicks"]


def test_a_field_named_by_both_metrics_and_dimensions_is_resolved_once():
    from core.datastream_mapping_api import _report_field_records

    capabilities = {
        "fields": [{"field_id": "x", "kind": "metric", "physical_type": "integer"}],
        "reports": [{"id": "r", "dimensions": ["x"], "metrics": ["x"]}],
    }

    assert len(_report_field_records(capabilities, "r")) == 1


# --- Defect 2: the aggregation vocabulary ------------------------------------


@pytest.mark.parametrize("aggregation", sorted(SCHEMA_AGGREGATIONS))
def test_an_aggregation_the_schema_accepts_passes_through_untouched(aggregation):
    assert _mapping_aggregation(aggregation) == (aggregation, None)


@pytest.mark.parametrize("aggregation", ["impression_weighted", "latest"])
def test_a_source_aggregation_outside_the_enum_becomes_custom_and_keeps_its_term(aggregation):
    """`custom` alone would say "not one of the six" without ever saying which."""
    mapped, evidence = _mapping_aggregation(aggregation)

    assert mapped == "custom"
    assert evidence == f"source_aggregation:{aggregation}"


def test_the_source_term_survives_into_the_profiled_suggestion():
    profile = profile_fields(
        field_records=[
            {
                "field_id": "average_position",
                "kind": "metric",
                "physical_type": "decimal",
                "aggregation": "impression_weighted",
                "non_additive": True,
                "canonical_target": "average_position",
            }
        ],
        known_target_fields={"average_position"},
    )

    suggestion = profile["fields"][0]["suggestion"]
    assert suggestion["aggregation"] == "custom"
    assert suggestion["non_additive"] is True
    assert "source_aggregation:impression_weighted" in suggestion["evidence"]


# --- The class, not the instance ---------------------------------------------


def test_every_report_profile_of_every_connector_resolves_a_field_universe():
    """The defect was never GSC's. It was 129 report profiles across 38 modules."""
    from core.datastream_mapping_api import _report_field_records

    empty = [
        f"{module}:{report.get('id')}"
        for module, capabilities in _all_manifests()
        for report in capabilities["reports"]
        if isinstance(report, dict)
        and not _report_field_records(capabilities, report.get("id"))
    ]
    assert empty == [], f"{len(empty)} report profiles still resolve no field"


def test_every_report_profile_produces_a_schema_valid_mapping():
    """Profiling every real report and normalizing the result. This is what
    `impression_weighted` used to break, one whole report at a time."""
    from core.datastream_mapping_api import _report_field_records

    failures = []
    for module, capabilities in _all_manifests():
        for report in capabilities["reports"]:
            if not isinstance(report, dict):
                continue
            records = _report_field_records(capabilities, report.get("id"))
            if not records:
                continue
            profile = profile_fields(field_records=records)
            payload = {
                "mapping_contract_version": "1",
                "source_schema_hash": compute_source_schema_hash(records),
                "plan_version_id": "dsp_probe",
                "grain": profile.get("grain") or [],
                "fields": profile["fields"],
                "ambiguities": profile.get("ambiguities", []),
            }
            try:
                normalize_mapping(payload)
            except Exception as exc:  # noqa: BLE001 - the failure list is the message
                failures.append(f"{module}:{report.get('id')}: {exc}")

    assert failures == [], "\n".join(failures[:20])


# --- The chain, end to end ----------------------------------------------------


def test_the_gsc_chain_reaches_an_executable_projection():
    """Field resolution -> profiling -> operator decisions -> executable plan.

    The operator decisions are explicit and are the ones the compiler asks for:
    exclude the source dimensions that map to no approved target field, exclude
    the non-additive measure the compiler refuses to project into an additive
    fact, and reduce the grain until the estimate fits the governed limits.
    Profiling deliberately does none of this by itself -- a suggestion that
    confirmed itself would be the connector deciding the Project's semantics.
    """
    from core.datastream_mapping_api import _report_field_records

    records = _report_field_records(_capabilities("gsc"), "catalog_daily")
    profile = profile_fields(
        field_records=records,
        known_target_fields={
            "clicks", "impressions", "average_position", "date", "country", "page",
        },
    )

    for field in profile["fields"]:
        binding = field["binding"]
        keep = binding.get("canonical_target") and field["field_id"] != "average_position"
        binding["status"] = "confirmed" if keep else "excluded"
        binding["blocking_reason"] = None
        binding["confirmed_by"] = "pytest@example.com"
        binding["confirmed_reason"] = "accepted" if keep else "not an approved additive target"

    payload = {
        "mapping_contract_version": "1",
        "source_schema_hash": compute_source_schema_hash(records),
        "plan_version_id": "dsp_probe",
        "grain": ["date", "country"],
        "fields": profile["fields"],
        "ambiguities": profile.get("ambiguities", []),
    }
    normalized, content_hash = normalize_mapping(payload)

    projection = compile_projection(
        {
            "id": "dsm_probe",
            "plan_version_id": "dsp_probe",
            "source_schema_hash": payload["source_schema_hash"],
            "capability_fingerprint": None,
            "executable": True,
            "content_hash": content_hash,
            "mapping_payload": normalized,
        }
    )

    assert projection["executable"] is True, projection.get("issues")
    assert projection["grain"] == ["date", "country"]


def test_the_uncut_gsc_grain_is_refused_rather_than_silently_accepted():
    """The proposed 7-dimension grain estimates ~1e21 rows. A compiler that let
    that through would be the one that costs money at 3am."""
    from core.datastream_mapping_api import _report_field_records

    records = _report_field_records(_capabilities("gsc"), "catalog_daily")
    profile = profile_fields(field_records=records)
    payload = {
        "mapping_contract_version": "1",
        "source_schema_hash": compute_source_schema_hash(records),
        "plan_version_id": "dsp_probe",
        "grain": profile["grain"],
        "fields": profile["fields"],
        "ambiguities": profile.get("ambiguities", []),
    }
    normalized, content_hash = normalize_mapping(payload)

    projection = compile_projection(
        {
            "id": "dsm_probe",
            "plan_version_id": "dsp_probe",
            "source_schema_hash": payload["source_schema_hash"],
            "capability_fingerprint": None,
            "executable": True,
            "content_hash": content_hash,
            "mapping_payload": normalized,
        }
    )

    codes = {issue["code"] for issue in projection["issues"]}
    assert projection["executable"] is False
    assert "cardinality_over_limit" in codes
