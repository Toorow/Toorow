"""What a Datastream's connector declares as pullable, for the `Processing` selector.

RATIFIED TARGET: `datastream-workbench-and-wizard.md`, amendment 13 of the
2026-08-11 section -- "`Processing` porte un sélecteur de dimensions et de
métriques, lu du manifeste". The only path to `source.selection.dimensions` was
the raw contract editor; a tab that COUNTS dimensions and offers none is the
defect that amendment names.

ONE READING OF THE MANIFEST, NOT A SECOND ONE. The creation wizard already
projects a manifest into `{reports, fields}` -- `datastream_setup_observations.
_connector_option_contract` -- and this module calls that same function rather
than re-deriving the shape. A second projection of one catalogue is a copy free
to diverge the day a manifest key moves, and the wizard and the Workbench would
then offer two different catalogues for one connector.

WHAT IT DOES NOT DO: it never says what a person MAY pick. That verdict belongs
to `datastream_intents._validate_connector`, which owns `exact_bundle_required`,
`unknown_report_field` and `unsupported_grain`. This module reports the report's
`selection_mode`, its declared fields and its supported grains, and the screen
states the consequence in words. A second opinion on selectability here would be
a rule that can disagree with the one that actually refuses.

NO DATABASE. Everything it needs -- the module name and the report id -- is
already in the plan the caller is reading, so the `Processing` tab pays one file
read for the manifest and no extra query.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: A `managed_feed` and an `external_bq` carry no module, so there is no manifest
#: to read for them. They are not an error and they are not an empty catalogue:
#: they are a different question, and the screen answers it rather than hiding
#: the control (amendment 7 -- a control that silently disappears is the defect).
_MODELESS_REASON = {
    "managed_feed": (
        "A file source pulls no selection: the columns are the ones the file "
        "brings, so nothing is requested from a provider."
    ),
    "external_bq": (
        "An external BigQuery source pulls no selection: the columns are the "
        "ones the table already has."
    ),
}


def read_source_catalogue(
    *,
    source_kind: str | None,
    module_name: str | None,
    report_id: str | None,
) -> dict[str, Any]:
    """Project the connector manifest's declared fields for one pinned report.

    The returned `state` is what the screen renders on, and each value is a
    different sentence rather than one empty list:

      `no_module`            -- this mode pulls no selection at all.
      `connector_unreadable` -- the module is named and its manifest could not
                                be read. NEVER reported as "no field".
      `no_report_pinned`     -- the plan names no report, so nothing bounds
                                what is pullable.
      `report_unknown`       -- the plan names a report the module no longer
                                declares.
      `available`            -- the declared fields, with their own descriptions.
    """
    kind = str(source_kind or "").strip()
    module = str(module_name or "").strip()
    if kind and kind != "connector_pull" or not module:
        return {
            "state": "no_module",
            "mode": kind or "unknown",
            "reason": _MODELESS_REASON.get(
                kind,
                "This Datastream carries no connector module and pulls no selection, "
                "so no manifest declares its fields.",
            ),
        }

    from core.context_seed import load_registry_entry  # noqa: PLC0415

    try:
        entry = load_registry_entry(module)
    except Exception as exc:  # noqa: BLE001 -- one unreadable module never takes the tab down
        logger.warning(
            "datastream_source_catalogue: manifest unreadable module=%s: %s", module, exc
        )
        entry = None
    manifest = (entry or {}).get("manifest")
    if not isinstance(manifest, dict) or not manifest.get("source_capabilities"):
        return {
            "state": "connector_unreadable",
            "mode": "connector_pull",
            "connector_ref": module,
            "reason": (
                f"The registry could not be read for connector {module}, so what it "
                "declares as pullable is unknown."
            ),
            # TRUE EVEN HERE, and that is why it is repeated rather than folded
            # into `base`. What the collection stamps onto a row is written by the
            # landing code, not by the manifest -- an unreadable manifest makes the
            # PROVIDER's catalogue unknown and leaves this one exactly as certain.
            "collection_fields": _collection_fields(),
        }

    from core.datastream_setup_observations import (  # noqa: PLC0415
        _connector_option_contract,
    )

    contract = _connector_option_contract(manifest)
    descriptions = {
        str(field.get("field_id")): str(field.get("description") or field.get("field_id"))
        for field in contract.get("fields") or []
        if field.get("field_id")
    }
    reports = contract.get("reports") or []
    base = {
        "state": "available",
        "mode": "connector_pull",
        "connector_ref": module,
        "connector_display_name": str(manifest.get("display_name") or module),
        # WHAT THE COLLECTION WRITES, BESIDE WHAT THE PROVIDER SENDS -- and never
        # mixed into the same list. Measured 2026-08-12: 38 of the 39 connector
        # modules stamp `loaded_at` and `pull_id` onto every row they land, 47 of
        # the 54 staging models carry them through, and `fact_daily_kpi` selects
        # `MAX(loaded_at)` in each of its 34 blocks -- so the instant a person
        # asks for is already on their rows, on every one of them.
        #
        # It is published here as a DECLARATION and never as a choice. A field the
        # pinned report does not declare is refused by `datastream_intents`
        # (`unknown_report_field`), so a checkbox that added `loaded_at` to
        # `source.selection` would compose a plan nothing can execute. There is
        # nothing to add; there was something to SHOW.
        #
        # Free, like the rest of this module: `declared_collection_fields()` reads
        # no file, no warehouse and no database. What those columns actually hold
        # on this project's rows is a warehouse round trip and lives under
        # `collection_provenance`, composed by the caller -- so an unreadable
        # warehouse cannot take this declaration down with it.
        "collection_fields": _collection_fields(),
    }
    report_ref = str(report_id or "").strip()
    if not report_ref:
        return {
            **base,
            "state": "no_report_pinned",
            "reason": (
                "This plan version names no report family, so nothing bounds what "
                "this connector can be asked for."
            ),
            # The families this connector declares, so the sentence above can name
            # the gesture rather than only the absence.
            "report_options": [
                {"report_ref": item["report_ref"], "display_name": item["display_name"]}
                for item in reports
            ],
        }
    report = next((item for item in reports if item["report_ref"] == report_ref), None)
    if report is None:
        return {
            **base,
            "state": "report_unknown",
            "report_ref": report_ref,
            "reason": (
                f"This plan version pins report {report_ref!r}, which connector "
                f"{module} no longer declares."
            ),
            "report_options": [
                {"report_ref": item["report_ref"], "display_name": item["display_name"]}
                for item in reports
            ],
        }

    def _entries(field_ids: list[str]) -> list[dict[str, str]]:
        return [
            {"field_id": field_id, "description": descriptions.get(field_id, field_id)}
            for field_id in field_ids
        ]

    return {
        **base,
        "report_ref": report_ref,
        "report_display_name": report["display_name"],
        # `exact_bundle` (115 reports of 140, measured 2026-08-12) requires the
        # COMPLETE declared bundle; `catalog_driven` (25) lets a selection be a
        # real subset. The screen says which one it is instead of offering a
        # toggle that would compose a plan the validator refuses.
        "selection_mode": _declared_selection_mode(manifest, report_ref),
        # AMENDMENT 14: « `max_provider_backfill_days` porte déjà cette borne et
        # personne ne la lit pour cette question ». It is read here.
        #
        # RE-MEASURED 2026-08-21, and the figure that stood here was stale: this
        # comment claimed "0 of the 140 declared reports carry the key" from a
        # 2026-08-12 count, and **35 of the 140 do**, across seven modules
        # (`brevo`, `google-ads`, `gsc`, `linkedin-ads`, `meta-ads`,
        # `microsoft-ads`, `pinterest-ads`). The count was written the day before
        # the modules that declare one were written, and nothing re-ran it.
        #
        # The MECHANISM never depended on the figure and does not change: a
        # report that declares no bound is published as `None` rather than
        # defaulted, so the debt says how far back a re-collection may reach is
        # unknown instead of promising a history no provider has committed to.
        # What changes is that "unknown" is now the answer for 105 reports, not
        # for all of them — and the seven modules above show what establishing
        # one costs: reading the provider's own documentation and recording the
        # URL beside the number.
        "max_provider_backfill_days": _declared_backfill_bound(manifest, report_ref),
        "dimensions": _entries(report["dimensions"]),
        "metrics": _entries(report["metrics"]),
        # The grain is validated against this list on its own -- a selection that
        # drops a grain column leaves the plan with an unsupported grain.
        "supported_grains": report["supported_grains"],
        "availability": report["availability"],
    }


def _collection_fields() -> list[dict[str, Any]]:
    """The collection-written vocabulary, read from the one place that declares it.

    Imported rather than restated: `datastream_collection_provenance` owns both
    the words and the reason none of them is selectable, and a second copy here
    would be free to disagree with the module that actually reads the columns.
    """
    from core.datastream_collection_provenance import (  # noqa: PLC0415
        declared_collection_fields,
    )

    return declared_collection_fields()


def _raw_report(manifest: dict[str, Any], report_ref: str) -> dict[str, Any]:
    """The report AS DECLARED, before any projection.

    `_connector_option_contract` keeps what the creation wizard needed and drops
    the rest; two keys the selector needs -- `selection_mode` and
    `max_provider_backfill_days` -- live only here. One lookup serves both rather
    than two walks of the same list.
    """
    capabilities = manifest.get("source_capabilities")
    if not isinstance(capabilities, dict):
        return {}
    for report in capabilities.get("reports") or []:
        if isinstance(report, dict) and str(report.get("id")) == report_ref:
            return report
    return {}


def _declared_selection_mode(manifest: dict[str, Any], report_ref: str) -> str:
    """The report's own `selection_mode`, read where it is declared.

    `_connector_option_contract` does not carry it -- the wizard never needed it,
    because a creation always writes the declared bundle. The selector does need
    it, and reading it from the raw capabilities is one lookup rather than a
    second projection.
    """
    return str(_raw_report(manifest, report_ref).get("selection_mode") or "unknown")


def _declared_backfill_bound(manifest: dict[str, Any], report_ref: str) -> int | None:
    """How many days back this report may be re-collected, when it says.

    `None` is the honest answer for the 105 reports of 140 that declare no bound
    (measured 2026-08-21; the "every report shipped today" this sentence used to
    claim was already false when it was written). It must stay distinguishable
    from `0`: `0` would mean "no day may be re-collected", which no manifest
    claims. A bool is refused explicitly -- `isinstance(True, int)` is true in
    Python, and `True` days is not a bound.
    """
    bound = _raw_report(manifest, report_ref).get("max_provider_backfill_days")
    if isinstance(bound, bool) or not isinstance(bound, int) or bound <= 0:
        return None
    return bound
