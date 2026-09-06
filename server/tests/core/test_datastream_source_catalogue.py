"""What a connector declares as pullable, for the `Processing` selector.

Amendment 13 of `datastream-workbench-and-wizard.md` (ratified 2026-08-11) puts
a selector of dimensions and metrics on `Processing`, "lu du manifeste". These
tests hold the four ways such a read goes wrong while still returning a payload
a screen would render as an answer:

  * a `managed_feed` reported as "no field" instead of "no selection is pulled";
  * an unreadable module reported as an empty catalogue, which reads as "this
    connector offers nothing";
  * a report the module no longer declares silently producing an empty list;
  * `selection_mode` dropped, so the screen offers toggles on an `exact_bundle`
    report the plan validator refuses.

A REAL MANIFEST ON DISK, read through `load_registry_entry` — the same door the
creation wizard uses. A stubbed dictionary would prove the projection and not
the thread.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.datastream_source_catalogue import read_source_catalogue


def _manifest(name: str, *, reports: list[dict]) -> dict:
    return {
        "schema_version": "1.0",
        "name": name,
        "display_name": "Sample Connector",
        "auth_type": "nango",
        "source_capabilities": {
            "fields": [
                {"field_id": "date", "kind": "dimension", "description": "Reporting day."},
                {"field_id": "campaign_id", "kind": "dimension", "description": "Campaign ID."},
                {"field_id": "campaign_name", "kind": "dimension", "description": "Campaign name."},
                {"field_id": "clicks", "kind": "metric", "description": "Clicks on the ads."},
            ],
            "reports": reports,
        },
    }


_CATALOG_DRIVEN = {
    "id": "catalog_daily",
    "display_name": "Catalog-driven daily",
    "selection_mode": "catalog_driven",
    "availability": {"status": "selectable"},
    "metrics": ["clicks"],
    "dimensions": ["date", "campaign_id", "campaign_name"],
    "supported_grains": [["date", "campaign_id"]],
}
_EXACT_BUNDLE = {
    "id": "campaign_daily",
    "display_name": "Campaign daily",
    "selection_mode": "exact_bundle",
    "availability": {"status": "selectable"},
    "metrics": ["clicks"],
    "dimensions": ["date", "campaign_id"],
    "supported_grains": [["date", "campaign_id"]],
}


def _install(tmp_path: Path, monkeypatch, manifest: dict) -> None:
    from core import context_seed

    module_dir = tmp_path / str(manifest["name"])
    module_dir.mkdir(parents=True, exist_ok=True)
    (module_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(context_seed, "DEFAULT_MODULES_DIR", tmp_path)


def test_a_declared_report_carries_its_fields_with_the_manifests_own_words(
    tmp_path, monkeypatch
) -> None:
    _install(tmp_path, monkeypatch, _manifest("sample-connector", reports=[_CATALOG_DRIVEN]))

    catalogue = read_source_catalogue(
        source_kind="connector_pull", module_name="sample-connector", report_id="catalog_daily"
    )

    assert catalogue["state"] == "available"
    assert catalogue["selection_mode"] == "catalog_driven"
    assert [item["field_id"] for item in catalogue["dimensions"]] == [
        "date",
        "campaign_id",
        "campaign_name",
    ]
    # The DESCRIPTION is what tells a person what a field is; an id repeated in
    # both columns is a catalogue that explains nothing.
    assert catalogue["dimensions"][2]["description"] == "Campaign name."
    assert catalogue["metrics"] == [{"field_id": "clicks", "description": "Clicks on the ads."}]
    # The grain travels, because a selection that drops a grain column leaves the
    # plan with a grain nothing supports.
    assert catalogue["supported_grains"] == [["date", "campaign_id"]]


def test_selection_mode_travels_so_an_exact_bundle_is_never_offered_as_a_choice(
    tmp_path, monkeypatch
) -> None:
    """115 of the 140 declared reports are `exact_bundle` (measured 2026-08-12).

    `datastream_intents._validate_connector` raises `exact_bundle_required` for
    any selection that is not the COMPLETE declared bundle, so a screen that did
    not receive this key would offer toggles composing a plan nothing executes.
    """
    _install(tmp_path, monkeypatch, _manifest("sample-connector", reports=[_EXACT_BUNDLE]))

    catalogue = read_source_catalogue(
        source_kind="connector_pull", module_name="sample-connector", report_id="campaign_daily"
    )

    assert catalogue["selection_mode"] == "exact_bundle"


def test_a_file_source_is_told_it_pulls_no_selection_never_shown_an_empty_catalogue() -> None:
    """4 of the 6 live Datastreams are `managed_feed` with `module_name` NULL.

    An empty list here would read as "this connector offers no dimension", which
    is a different and false statement.
    """
    for mode in ("managed_feed", "external_bq"):
        catalogue = read_source_catalogue(source_kind=mode, module_name=None, report_id=None)
        assert catalogue["state"] == "no_module"
        assert catalogue["mode"] == mode
        assert "pulls no selection" in catalogue["reason"]
        assert "dimensions" not in catalogue


def test_an_unreadable_module_says_so_rather_than_reporting_no_field(
    tmp_path, monkeypatch
) -> None:
    from core import context_seed

    monkeypatch.setattr(context_seed, "DEFAULT_MODULES_DIR", tmp_path)

    catalogue = read_source_catalogue(
        source_kind="connector_pull", module_name="absent-connector", report_id="catalog_daily"
    )

    assert catalogue["state"] == "connector_unreadable"
    assert "absent-connector" in catalogue["reason"]

    def _raise(*_args: object, **_kwargs: object):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(context_seed, "load_registry_entry", _raise)
    assert (
        read_source_catalogue(
            source_kind="connector_pull", module_name="sample-connector", report_id="x"
        )["state"]
        == "connector_unreadable"
    )


def test_a_report_the_module_no_longer_declares_names_the_families_that_exist(
    tmp_path, monkeypatch
) -> None:
    _install(
        tmp_path,
        monkeypatch,
        _manifest("sample-connector", reports=[_CATALOG_DRIVEN, _EXACT_BUNDLE]),
    )

    catalogue = read_source_catalogue(
        source_kind="connector_pull", module_name="sample-connector", report_id="retired_report"
    )

    assert catalogue["state"] == "report_unknown"
    # An absence that does not name the gesture that fills it is a dead end.
    assert [item["report_ref"] for item in catalogue["report_options"]] == [
        "catalog_daily",
        "campaign_daily",
    ]


def test_a_plan_that_pins_no_report_says_nothing_bounds_the_selection(
    tmp_path, monkeypatch
) -> None:
    _install(tmp_path, monkeypatch, _manifest("sample-connector", reports=[_CATALOG_DRIVEN]))

    catalogue = read_source_catalogue(
        source_kind="connector_pull", module_name="sample-connector", report_id=None
    )

    assert catalogue["state"] == "no_report_pinned"
    assert [item["display_name"] for item in catalogue["report_options"]] == [
        "Catalog-driven daily"
    ]


def test_the_provider_backfill_bound_is_read_and_reported_absent_when_undeclared(
    tmp_path, monkeypatch
) -> None:
    """Amendment 14: « `max_provider_backfill_days` porte déjà cette borne et
    personne ne la lit pour cette question ». It is read here — and the answer
    for every report shipped today is that nobody declares it.

    Measured 2026-08-12 over `server/modules/*/manifest.json`: 140 declared
    reports, 0 carrying the key. So `None` is the LIVE branch, and it must stay
    distinguishable from a bound of `0` — which would mean no day may ever be
    re-collected, a claim no manifest makes. `True` is refused for the same
    reason: `isinstance(True, int)` is true in Python and `True` days is not a
    number of days.
    """
    bounded = dict(_CATALOG_DRIVEN, id="bounded_daily", max_provider_backfill_days=30)
    zero = dict(_CATALOG_DRIVEN, id="zero_daily", max_provider_backfill_days=0)
    truthy = dict(_CATALOG_DRIVEN, id="truthy_daily", max_provider_backfill_days=True)
    _install(
        tmp_path,
        monkeypatch,
        _manifest("sample-connector", reports=[_CATALOG_DRIVEN, bounded, zero, truthy]),
    )

    def _bound(report_id: str):
        return read_source_catalogue(
            source_kind="connector_pull",
            module_name="sample-connector",
            report_id=report_id,
        )["max_provider_backfill_days"]

    assert _bound("catalog_daily") is None
    assert _bound("bounded_daily") == 30
    assert _bound("zero_daily") is None
    assert _bound("truthy_daily") is None
