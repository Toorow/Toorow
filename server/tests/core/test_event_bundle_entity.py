"""An event bundle's entity identifier is a mappable dimension (AI-373).

WHAT WAS MEASURED, 2026-09-04, on the reference project, through
`/mdm/common-keys/proposals`: the flow *video publications* -- a `connector_pull`
on an `exact_bundle` report whose landing is `context_events` -- mapped `date`
and nothing else. The pull already knew which video each marker was about (it
writes the key to `app.context_events.entity_key`), but no report declared it, so
the identifier never entered the field universe, never reached a mapping, could
not be pinned, and the shared-identity proposal counted ONE carrier for an
identity the project carries on two flows. `video x date` was undeclarable.

WHAT THESE TESTS PIN, and none of them names a connector, a report or an entity
kind -- the rule reads a DECLARATION (`datastream-workbench-and-wizard.md`,
amendment 2026-09-06):

  * the declaration travels into the governed catalogue as one of the report's
    DIMENSIONS -- the single seam `_normalize_report`, so the wizard's field
    universe and the intent validator inherit it together rather than one of them
    offering a column the other then refuses;
  * a selection carrying it is neither `unknown_report_field` nor
    `exact_bundle_required`, and one omitting it IS refused: the bundle is still
    complete by construction, one column larger;
  * a declaration that cannot be honoured is refused at validation rather than
    dropped in silence.
"""

from __future__ import annotations

import copy

from core.datastream_intents import validate_intent
from core.datastream_preconfiguration import _normalized_field_universe
from core.source_capabilities import (
    normalize_capabilities,
    validate_manifest_capabilities,
)

ENTITY_FIELD = "asset_ref"
ENTITY_KIND = "asset"


def _descriptor() -> dict:
    """One connector declaring both halves of the reference project's shape.

    `marker_bundle` is the event bundle: one marker per day per asset, landing in
    `context_events`. `asset_daily` is the fact report that measures those same
    assets. They share the asset identifier and the day -- which is exactly the
    pair a common key is declared over.
    """
    return {
        "contract_version": "1",
        "field_discovery": {"mode": "static", "allowed_targets": []},
        "fields": [
            {
                "field_id": "date",
                "source_field": "date",
                "kind": "dimension",
                "physical_type": "date",
                "description": "Reporting date.",
                "semantic_hints": ["date"],
                "canonical_target": "date",
                "aggregation": "none",
                "non_additive": False,
            },
            {
                "field_id": ENTITY_FIELD,
                "source_field": ENTITY_FIELD,
                "kind": "dimension",
                "physical_type": "string",
                "description": "The asset a row is about.",
                "semantic_hints": ["identifier"],
                "canonical_target": ENTITY_FIELD,
                "aggregation": "none",
                "non_additive": False,
            },
            {
                "field_id": "publication",
                "source_field": "published_at",
                "kind": "event",
                "physical_type": "datetime",
                "description": "An asset was published.",
                "semantic_hints": ["event"],
                "canonical_target": "publication",
                "aggregation": "none",
                "non_additive": False,
            },
            {
                "field_id": "views",
                "source_field": "views",
                "kind": "metric",
                "physical_type": "integer",
                "description": "Views.",
                "semantic_hints": ["count"],
                "canonical_target": "views",
                "aggregation": "sum",
                "non_additive": False,
            },
        ],
        "reports": [
            {
                "id": "marker_bundle",
                "selection_mode": "exact_bundle",
                "availability": {"status": "selectable"},
                "dispatch": {"callable": "pull_marker_bundle"},
                "landing": "context_events",
                "entity_field": ENTITY_FIELD,
                "entity_kind": ENTITY_KIND,
                "metrics": [],
                "dimensions": ["date"],
                "events": ["publication"],
                "supported_grains": [["date"]],
                "compatibility": [],
                "filters": [],
                "pagination": {
                    "mode": "none",
                    "completeness": "complete",
                    "max_pages": None,
                    "row_limit": None,
                    "truncation_signal": "none",
                },
                "quota_cost": {"read_points": 1, "unit": "request"},
                "incremental": {"mode": "full_refresh", "cursor_field": None},
                "cadence": {
                    "minimum_interval_minutes": 1440,
                    "supported_modes": ["daily", "manual"],
                },
            },
            {
                "id": "asset_daily",
                "selection_mode": "exact_bundle",
                "availability": {"status": "selectable"},
                "dispatch": {"callable": "pull_asset_daily"},
                "metrics": ["views"],
                "dimensions": ["date", ENTITY_FIELD],
                "supported_grains": [["date", ENTITY_FIELD]],
                "compatibility": [],
                "filters": [],
                "pagination": {
                    "mode": "none",
                    "completeness": "complete",
                    "max_pages": None,
                    "row_limit": None,
                    "truncation_signal": "none",
                },
                "quota_cost": {"read_points": 1, "unit": "request"},
                "incremental": {"mode": "date_window", "cursor_field": None},
                "cadence": {
                    "minimum_interval_minutes": 1440,
                    "supported_modes": ["daily", "manual"],
                },
            },
        ],
    }


def _manifest() -> dict:
    return {
        "schema_version": "1.2",
        "name": "test-event-module",
        "display_name": "Test Event Module",
        "auth_type": "none",
        "module_kind": "kpi",
        "report_profiles": [
            {
                "id": "marker_bundle",
                "display_name": "Publication markers",
                "metrics": [],
                "dimensions": ["date"],
                "events": ["publication"],
                "landing": "context_events",
                "extraction_capabilities": {
                    "row_limit": None,
                    "filters_supported": False,
                    "realtime": False,
                },
            },
            {
                "id": "asset_daily",
                "display_name": "Views by asset",
                "metrics": ["views"],
                "dimensions": ["date", ENTITY_FIELD],
                "extraction_capabilities": {
                    "row_limit": None,
                    "filters_supported": False,
                    "realtime": False,
                },
            },
        ],
        "canonical_metric_mapping": {"views": "views"},
        "canonical_dimension_mapping": {"date": "date", ENTITY_FIELD: ENTITY_FIELD},
        "source_capabilities": _descriptor(),
    }


def catalogue(manifest: dict | None = None) -> dict:
    return normalize_capabilities(
        manifest or _manifest(),
        project_id="proj_EXAMPLE",
        connection_ref_id="conn_EXAMPLE",
    )


def _report(catalog: dict, report_id: str) -> dict:
    return next(item for item in catalog["reports"] if item["id"] == report_id)


def _codes(manifest: dict) -> set[str]:
    return {issue.code for issue in validate_manifest_capabilities(manifest)}


def _universe(catalog: dict, report_id: str) -> list[str]:
    return [
        field["field_id"]
        for field in _normalized_field_universe({"connector_contract": {"contract": catalog}},
                                                report_id)
    ]


def _intent(selection: dict) -> dict:
    return {
        "contract_version": "1",
        "source": {
            "kind": "connector_pull",
            "writer_kind": "toorow",
            "connection_ref_id": "conn_EXAMPLE",
            "report_id": "marker_bundle",
            "selection": selection,
        },
        "destination": {"policy": "managed_raw"},
        "historical": {
            "start": "2026-01-01T00:00:00Z",
            "end_exclusive": "2026-02-01T00:00:00Z",
        },
        "schedule": {
            "mode": "daily",
            "interval_minutes": 1440,
            "timezone": "Europe/Paris",
            "watermark": {"kind": "date_window", "delay_minutes": 0},
            "late_arrival": {"lookback_minutes": 0},
            "retry": {
                "max_attempts": 3,
                "initial_backoff_seconds": 60,
                "max_backoff_seconds": 3600,
            },
            "missed_run": {"mode": "coalesce", "max_catchup_windows": 1},
        },
    }


# ---------------------------------------------------------------------------
# The declaration reaches the governed catalogue, and the mapping.
# ---------------------------------------------------------------------------


def test_the_declared_entity_identifier_is_a_dimension_of_the_governed_catalogue():
    report = _report(catalogue(), "marker_bundle")

    assert report["dimensions"] == ["date", ENTITY_FIELD]
    # Carried under its own name too: a reader that needs to know WHICH dimension
    # identifies the entity must not have to guess it back out of the list.
    assert report["entity_field"] == ENTITY_FIELD
    assert report["entity_kind"] == ENTITY_KIND


def test_the_entity_identifier_enters_the_mappable_field_universe_of_the_event_bundle():
    """The measured defect, in one line: the bundle offered `date` and nothing else."""
    catalog = catalogue()

    assert _universe(catalog, "marker_bundle") == [ENTITY_FIELD, "date"]
    # And the fact report of the same project carries the SAME identity, which is
    # what gives the shared-identity proposal two carriers instead of one.
    assert ENTITY_FIELD in _universe(catalog, "asset_daily")


def test_a_report_that_declares_no_entity_gains_no_dimension():
    """No connector is special. A report that says nothing gets nothing."""
    manifest = _manifest()
    del manifest["source_capabilities"]["reports"][0]["entity_field"]
    del manifest["source_capabilities"]["reports"][0]["entity_kind"]

    catalog = catalogue(manifest)
    assert _report(catalog, "marker_bundle")["dimensions"] == ["date"]
    assert _universe(catalog, "marker_bundle") == ["date"]


# ---------------------------------------------------------------------------
# The plan validator and the catalogue agree, so nothing is offered then refused.
# ---------------------------------------------------------------------------


def test_a_selection_carrying_the_entity_identifier_is_executable():
    result = validate_intent(
        _intent(
            {
                "selection_mode": "exact_bundle",
                "metrics": [],
                "dimensions": ["date", ENTITY_FIELD],
                "grain": ["date"],
                "filters": [],
            }
        ),
        capabilities=catalogue(),
    )

    assert result.executable, [issue.code for issue in result.issues]


def test_the_bundle_is_still_complete_by_construction_and_omitting_it_is_refused():
    result = validate_intent(
        _intent(
            {
                "selection_mode": "exact_bundle",
                "metrics": [],
                "dimensions": ["date"],
                "grain": ["date"],
                "filters": [],
            }
        ),
        capabilities=catalogue(),
    )

    codes = {issue.code for issue in result.issues}
    assert "exact_bundle_required" in codes


# ---------------------------------------------------------------------------
# A declaration that cannot be honoured is refused, never dropped in silence.
# ---------------------------------------------------------------------------


def test_an_entity_identifier_on_a_report_that_lands_no_event_is_refused():
    manifest = _manifest()
    manifest["source_capabilities"]["reports"][1]["entity_field"] = ENTITY_FIELD
    manifest["source_capabilities"]["reports"][1]["entity_kind"] = ENTITY_KIND

    assert "entity_identifier_without_event_landing" in _codes(manifest)


def test_half_a_declaration_is_refused():
    manifest = _manifest()
    del manifest["source_capabilities"]["reports"][0]["entity_kind"]

    assert "incomplete_entity_identifier" in _codes(manifest)


def test_an_entity_identifier_naming_an_undeclared_or_non_dimension_field_is_refused():
    unknown = copy.deepcopy(_manifest())
    unknown["source_capabilities"]["reports"][0]["entity_field"] = "no_such_field"
    assert "unknown_report_field" in _codes(unknown)

    measure = copy.deepcopy(_manifest())
    measure["source_capabilities"]["reports"][0]["entity_field"] = "views"
    assert "field_kind_mismatch" in _codes(measure)
