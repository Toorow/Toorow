"""Epic 31 story 31.1 -- event capability contract tests.

Covers the event-as-first-class-capability contract:
  * dim_event_type.csv dictionary + is_known_event_type() (pendant of is_known_metric)
  * event_type is NOT surfaced as a report dimension
  * a kind='event' field passes the catalog schema AND diffs clean vs the manifest
  * a manifest carrying landing + canonical_event_mapping validates
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from core import report_dictionary as rd
from core.catalog_contract import diff_catalog_manifest, validate_catalog_schema
from referencing import Registry, Resource

_SCHEMAS = Path(__file__).parents[2] / "core" / "schemas"
_MODULES = Path(__file__).parents[2] / "modules"


# ---------------------------------------------------------------------------
# Canonical event dictionary (pendant of dim_metric.csv)
# ---------------------------------------------------------------------------


def test_known_event_types_from_seed() -> None:
    assert rd.is_known_event_type("video_upload") is True
    assert rd.is_known_event_type("release") is True
    assert rd.is_known_event_type("VIDEO_UPLOAD") is True  # case-insensitive
    assert rd.is_known_event_type("not_a_real_event") is False
    assert "video_upload" in rd.valid_event_types()


def test_event_type_is_not_a_dimension() -> None:
    # dim_event_type.csv must NOT leak into the report dimension vocabulary.
    assert rd.is_known_dimension("event_type") is False


# ---------------------------------------------------------------------------
# kind='event' field through the catalog contract
# ---------------------------------------------------------------------------

_EVENT_CATALOG = {
    "catalog_version": "1",
    "connector": "x",
    "api_version": "v1",
    "sources": [{"url": "u", "kind": "official", "fetched_at": "2026-07-21"}],
    "generated_at": "2026-07-21T00:00:00Z",
    "fields": [
        {
            "field_id": "video_upload",
            "source_field": "snippet.publishedAt",
            "kind": "event",
            "physical_type": "datetime",
            "description": "video publish event",
            "section": "EVENTS",
            "tier": "core",
            "exposure": "exposed",
        }
    ],
}

_EVENT_MANIFEST = {
    "source_capabilities": {
        "fields": [
            {
                "field_id": "video_upload",
                "source_field": "snippet.publishedAt",
                "kind": "event",
                "physical_type": "datetime",
                "description": "video publish event",
                "semantic_hints": ["event"],
                "canonical_target": "video_upload",
                "aggregation": "none",
                "non_additive": False,
                "label_source_field": "snippet.title",
            }
        ]
    }
}


def test_event_field_passes_catalog_schema() -> None:
    assert validate_catalog_schema(_EVENT_CATALOG) == []


def test_event_field_diffs_clean_against_manifest() -> None:
    assert diff_catalog_manifest(_EVENT_CATALOG, _EVENT_MANIFEST) == []


# ---------------------------------------------------------------------------
# manifest with landing + canonical_event_mapping
# ---------------------------------------------------------------------------


def _manifest_validator() -> jsonschema.Draft202012Validator:
    man_sch = json.loads((_SCHEMAS / "manifest.schema.json").read_text(encoding="utf-8"))
    cap_sch = json.loads((_SCHEMAS / "source-capabilities.schema.json").read_text(encoding="utf-8"))
    registry = Registry().with_resource(cap_sch["$id"], Resource.from_contents(cap_sch))
    return jsonschema.Draft202012Validator(man_sch, registry=registry)


def test_manifest_accepts_landing_and_canonical_event_mapping() -> None:
    man = json.loads((_MODULES / "adjust" / "manifest.json").read_text(encoding="utf-8"))
    man["canonical_event_mapping"] = {"video_upload": "video_upload"}
    man["report_profiles"][0]["landing"] = "fact_daily_kpi"
    man["source_capabilities"]["reports"][0]["landing"] = "context_events"
    errors = list(_manifest_validator().iter_errors(man))
    assert errors == [], [f"{list(e.absolute_path)}: {e.message}" for e in errors]


@pytest.mark.parametrize("landing", ["nope", "fact_daily", ""])
def test_manifest_rejects_unknown_landing(landing: str) -> None:
    man = json.loads((_MODULES / "adjust" / "manifest.json").read_text(encoding="utf-8"))
    man["report_profiles"][0]["landing"] = landing
    errors = list(_manifest_validator().iter_errors(man))
    assert errors, f"landing={landing!r} should be rejected by the schema enum"


# ---------------------------------------------------------------------------
# Story 31.2 -- landing resolver + connector event-type enforcement (pure)
# ---------------------------------------------------------------------------


def test_resolve_landing_explicit_wins() -> None:
    from core.context_events import resolve_landing

    assert resolve_landing({"landing": "context_events"}, "kpi") == "context_events"
    assert resolve_landing({"landing": "fact_daily_kpi"}, "context") == "fact_daily_kpi"


def test_resolve_landing_derived_from_module_kind() -> None:
    from core.context_events import resolve_landing

    # absent landing -> derived (zero churn for existing modules)
    assert resolve_landing({}, "context") == "context_events"
    assert resolve_landing({}, "kpi") == "fact_daily_kpi"
    assert resolve_landing({}, None) == "fact_daily_kpi"
    # unknown landing value falls back to the derivation too
    assert resolve_landing({"landing": "garbage"}, "context") == "context_events"


def test_validate_event_type_connector_drift_refused() -> None:
    from core.context_events import validate_event_type
    from fastmcp.exceptions import ToolError

    # known canonical type -> ok for any source
    validate_event_type("video_upload", "youtube-analytics")
    validate_event_type("release", "github")
    # connector emitting an unknown type -> typed refusal (drift, never silent)
    with pytest.raises(ToolError):
        validate_event_type("totally_made_up", "youtube-analytics")


def test_validate_event_type_manual_is_free() -> None:
    # manual events keep free-form types (backward compat) -- no raise.
    from core.context_events import validate_event_type

    validate_event_type("some_business_thing", "manual")


# ---------------------------------------------------------------------------
# Story 31.2 -- queue routes the populate-verification skip by LANDING, not
# module_kind, so a MIXED connector is correct. This asserts the exact decision
# the queue worker composes (resolve_landing == 'context_events' -> skip verify).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("profile", "module_kind", "expect_skip"),
    [
        # legacy context connector: derived context_events landing -> skip (as before)
        ({}, "context", True),
        # legacy kpi connector: derived fact_daily_kpi landing -> verify
        ({}, "kpi", False),
        # MIXED: an event profile on a kpi connector (YouTube video_upload) -> skip
        ({"landing": "context_events"}, "kpi", True),
        # MIXED: a kpi profile on a context connector -> verify (do not skip)
        ({"landing": "fact_daily_kpi"}, "context", False),
    ],
)
def test_verification_skip_routes_by_landing(profile, module_kind, expect_skip) -> None:
    from core.context_events import resolve_landing

    skip = resolve_landing(profile, module_kind) == "context_events"
    assert skip is expect_skip
