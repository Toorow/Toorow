"""Read-only, pre-Datastream discovery adapters.

These adapters return untrusted evidence to the core normalizer. They never write to a
provider, create a Datastream, or persist preview rows.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Protocol

from core.csv_excel_import import detect_format, parse_csv, parse_excel
from core.external_bq_registration import build_probe_plan


class BigQuerySetupClient(Protocol):
    def get_table_metadata(self, object_ref: str) -> dict[str, Any]: ...

    # The estimate is part of the contract, not an extra: a read of an external
    # warehouse spends the customer's money, and the only moment the cost can
    # change a decision is BEFORE the read. `None` means "no estimate", never 0.
    def estimate_scan_bytes(self, object_ref: str) -> int | None: ...


class SheetsSetupClient(Protocol):
    def get_sheet_metadata(self, sheet_ref: str) -> dict[str, Any]: ...


# Les deux raisons NOMMEES qu'une ligne d'en-tete a d'etre inexploitable. Le
# premier code existe deja (`core.google_sheets_sync:106`) ; le second est neuf.
# Tout autre cas -- ligne 1 vide -- garde `sheet_headers_unavailable`.
_NAMED_HEADER_FAULTS = ("duplicate_headers", "header_row_incomplete")


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _external_coordinates(value: str) -> dict[str, str]:
    parts = [part.strip() for part in value.split(".")]
    if len(parts) != 3 or any(not part for part in parts):
        raise ValueError("BigQuery object reference must be project.dataset.table_or_view")
    return {"project": parts[0], "dataset": parts[1], "object": parts[2]}


def observe_external_bigquery(
    request: dict[str, Any], *, client: BigQuerySetupClient
) -> dict[str, Any]:
    """Use an injected, access-scoped typed client for read-only table metadata.

    NO ROW EVER TRAVELS. The evidence carries the shape of the object and what a
    read would cost, never a value out of it: the bounded masked sample belongs to
    the preview step, and the normalizer refuses row keys outright.

    THE ESTIMATE IS TAKEN BEFORE THE RETURN IS BUILT, on purpose. It is the only
    cost evidence that exists before the step-5 read, and the preview refuses to
    run without it -- so an observation that carries no estimate must be visibly
    incomplete rather than quietly cheap.
    """
    coordinates = _external_coordinates(str(request["object_ref"]))
    plan = build_probe_plan(coordinates, sample_limit=1)
    if plan["read_only"] is not True:
        raise RuntimeError("BigQuery setup plan is not read-only")
    metadata = client.get_table_metadata(plan["object_ref"])
    fields = [
        {
            "name": str(field.get("name", "")),
            "field_id": str(field.get("field_id") or field.get("name", "")),
            "type": str(field.get("type", "unknown")),
            "nullable": bool(field.get("nullable", True)),
            # The BigQuery mode and the schema's description. They are what a
            # person reads INSTEAD of an example value: `REPEATED RECORD` says
            # more about a column than one of its rows would, and it takes
            # nothing out of the customer's warehouse.
            "mode": str(field.get("mode") or "NULLABLE"),
            **({"description": str(field["description"])} if field.get("description") else {}),
        }
        # NO SLICE HERE. The normalizer owns the bound (`MAX_OBSERVED_FIELDS`)
        # AND reports what it cut (`coverage.fields_listed` / `fields_observed`).
        # Slicing twice meant the count the screen compares against was already
        # the sliced one, so a 500-column table reported 200 observed and the
        # loss of the other 300 was invisible.
        for field in list(metadata.get("fields") or [])
        if isinstance(field, dict) and field.get("name")
    ]
    schema_hash = _fingerprint(fields)
    scan_bytes = client.estimate_scan_bytes(plan["object_ref"])
    estimated = isinstance(scan_bytes, int) and not isinstance(scan_bytes, bool)
    unselectable = int(metadata.get("unselectable_field_count") or 0)
    depth_truncated = bool(metadata.get("schema_depth_truncated"))
    exceptions: list[dict[str, Any]] = []
    if not fields:
        exceptions.append({"code": "schema_unavailable"})
    if not estimated:
        exceptions.append({"code": "scan_estimate_unavailable"})
    if depth_truncated:
        exceptions.append({"code": "schema_depth_truncated"})
    return {
        "adapter_ref": "external_bq.readonly.v1",
        "safe_metadata": {
            "fields": fields,
            "schema_hash": schema_hash,
            "location": metadata.get("location"),
            "watermark": metadata.get("watermark"),
            "freshness": metadata.get("freshness"),
            # `quota_cost` is the ONE key of `SAFE_METADATA_KEYS` that carries a
            # cost, so this is where the estimate can live at all. It says what it
            # measures beside the number: a byte count with no sentence is a
            # figure nobody can act on.
            "quota_cost": (
                {
                    "bytes_scanned_estimate": scan_bytes,
                    "unit": "bytes",
                    "measured_by": "bigquery_dry_run_query",
                    "measures": (
                        "What one read of this object would scan. Planned by BigQuery "
                        "without being executed, and not billed."
                    ),
                }
                if estimated
                else None
            ),
        },
        "coverage": {
            "schema": "available" if fields else "unavailable",
            "location": "available" if metadata.get("location") else "unavailable",
            "watermark": "available" if metadata.get("watermark") else "unavailable",
            "freshness": "available" if metadata.get("freshness") else "unavailable",
            "scan_estimate": "available" if estimated else "unavailable",
            # Folders and arrays are DESCRIBED above and cannot be selected.
            # Counted so the screen marks them instead of silently omitting them.
            "unselectable_fields": str(unselectable),
            "schema_depth": "truncated" if depth_truncated else "complete",
        },
        "exceptions": exceptions,
    }


def observe_staged_file(
    request: dict[str, Any],
    *,
    asset_loader: Callable[[str], tuple[str, bytes]],
) -> dict[str, Any]:
    """Parse one opaque staged asset and retain schema/counters, never rows."""
    staged_ref = str(request["staged_asset_ref"])
    filename, payload = asset_loader(staged_ref)
    detected = detect_format(filename, payload)
    parsed = (
        parse_csv(payload, max_rows=10_000)
        if detected == "csv"
        # A DISCOVERY READS; IT IMPORTS NOTHING. Refusing a workbook here because
        # a cell carries a formula makes the file unreachable at the step that
        # only looks at its shape -- and no contract exists yet to say otherwise,
        # because the contract is what the operator writes AFTER seeing this.
        # `parse_excel` reads cached values (`data_only=True`); macros, OLE
        # objects and external links stay refused, flag or no flag.
        else parse_excel(payload, max_rows=10_000, formulas_as_values=True)
    )
    fields = [
        {
            "name": column.name,
            "field_id": column.name,
            "type": column.detected_type,
            "nullable": column.null_count > 0,
        }
        for column in parsed.columns[:200]
    ]
    return {
        "adapter_ref": "managed_feed.file.readonly.v1",
        "safe_metadata": {
            "staged_asset_ref": staged_ref,
            "content_hash": parsed.content_hash,
            "schema_hash": _fingerprint(fields),
            "detected_format": detected,
            "fields": fields,
            "row_count_bucket": _row_bucket(parsed.detected_row_count),
            "append_supported": False,
        },
        "coverage": {"schema": "available", "rows": "bounded_count_only"},
        "exceptions": [],
    }


def observe_google_sheet(request: dict[str, Any], *, client: SheetsSetupClient) -> dict[str, Any]:
    """Describe ONE tab of one workbook: its header row, its bounds, no value.

    NO DATA ROW IS READ, and the boundary is a sentence rather than an intention:
    a header cell is the NAME of a column -- the exact equivalent of a BigQuery
    `field.name` -- and a cell on row 2 or below is a value that never leaves the
    workbook. `coverage.values` stays `not_observed`, and `raw_sample` /
    `sample_rows` remain refused by the normalizer.

    NO TYPE IS STATED EITHER. A spreadsheet declares none, and reading values to
    guess one is the pull this adapter refuses; `coverage.types` says
    `not_inferred` so the screen can say why rather than let an operator wonder.
    Every column therefore compiles as a dimension, and the type is confirmed at
    `Classify and map`.
    """
    metadata = client.get_sheet_metadata(str(request["sheet_ref"]))
    # NO SLICE HERE. The normalizer owns the bound (`MAX_OBSERVED_FIELDS`) AND
    # reports what it cut (`coverage.fields_listed` / `fields_observed`). Cutting
    # twice made the screen compare its list against an already-cut total.
    headers = [str(value) for value in list(metadata.get("headers") or []) if str(value).strip()]
    fields = [
        {
            "name": name,
            "field_id": name,
            # `unknown`, and the coverage key beside it says this is a refusal,
            # not a gap in the read.
            "type": "unknown",
            # A spreadsheet declares no nullability, so `NULLABLE` would be an
            # assertion nobody made. `UNKNOWN` is not a container type, so the
            # shared `is_container_field` rule never mistakes it for a folder.
            "nullable": True,
            "mode": "UNKNOWN",
        }
        # A header cell carries no description of its own -- there is nowhere in
        # a sheet for one to be written -- so none is invented here.
        for name in headers
    ]
    tab_state = str(metadata.get("tab_state") or "named")
    header_state = str(metadata.get("header_state") or ("assumed" if fields else "uncertain"))
    header_reason = str(metadata.get("header_reason") or "")
    row_count = metadata.get("row_count")
    has_grid = isinstance(row_count, int) and not isinstance(row_count, bool) and row_count > 0
    safe_metadata: dict[str, Any] = {
        "fields": fields,
        "schema_hash": _fingerprint(fields),
        "location": metadata.get("locale"),
        "append_supported": False,
        # THE TABS THIS WORKBOOK CARRIES, so the reference becomes a choice
        # instead of a name typed from memory. A title is not a value: the
        # metadata call reads no cell. NO SLICE and no count here either -- the
        # normalizer owns the bound for `objects` exactly as it does for
        # `fields`, and reports what it cut.
        "objects": list(metadata.get("tabs") or []),
    }
    if has_grid:
        # A BUCKET, NOT A COUNT. `rowCount` is the height of the GRID, not the
        # number of rows carrying data; presenting one for the other would be a
        # figure nobody measured.
        safe_metadata["row_count_bucket"] = _row_bucket(int(row_count))
    exceptions: list[dict[str, Any]] = []
    if tab_state == "unnamed":
        exceptions.append({"code": "sheet_tab_not_named"})
    elif tab_state == "not_found":
        # The connector's own stable code (`core.google_sheets_sync:104`), reused
        # rather than respelled: the workbook was read, only the tab is missing.
        exceptions.append({"code": "tab_not_found"})
    elif header_state == "uncertain":
        # An empty row 1 keeps the historical code; a row that IS there but
        # unusable says which way -- the repair differs, so the code does too.
        exceptions.append(
            {
                "code": header_reason
                if header_reason in _NAMED_HEADER_FAULTS
                else "sheet_headers_unavailable"
            }
        )
    elif not fields:
        exceptions.append({"code": "sheet_headers_unavailable"})
    return {
        "adapter_ref": "managed_feed.google_sheets.readonly.v1",
        "safe_metadata": safe_metadata,
        "coverage": {
            "schema": "available" if fields else "unavailable",
            "values": "not_observed",
            "tab": tab_state,
            "header": header_state,
            "types": "not_inferred",
            "grid": "available" if has_grid else "unavailable",
        },
        "exceptions": exceptions,
    }


def observe_channel_contract(
    request: dict[str, Any],
    *,
    channel_state: dict[str, Any],
    first_delivery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project a DECLARED inbound channel contract, calling nothing.

    THE PAIR THAT HAS NO PROVIDER. The four adapters beside this one read
    something: a warehouse, a workbook, a staged file, a persisted contract. An
    email address and a webhook token can be read by nobody -- there is no
    listing endpoint at the other end, and until a sender sends, there is
    nothing at all. What this adapter turns into evidence is therefore the
    OPERATOR'S PROMISE, read back against the state of the deployment that has
    to honour it (`core.inbound_discovery.read_channel_contract`).

    IT TAKES ITS STATE AS AN ARGUMENT, exactly like `observe_connector_contract`
    above: no connection, no network, no clock. That is what makes the contract
    provable offline, which for this channel is the only way it can be proved --
    no test account exists and none is asked for.

    DENY BY DEFAULT. Only the named clauses are projected; anything else in
    `channel_state` is dropped rather than forwarded, so describing a channel can
    never carry a capability into a setup review.
    """
    delivery = first_delivery if isinstance(first_delivery, dict) else None
    delivered_metadata = delivery.get("safe_metadata") if delivery else None
    safe_metadata: dict[str, Any] = (
        dict(delivered_metadata) if isinstance(delivered_metadata, dict) else {"fields": []}
    )
    safe_metadata.setdefault("fields", [])
    # NO `mode` IS ADDED to a delivered field. A tabular file declares neither
    # NULLABLE nor REQUIRED nor REPEATED, so stating one would be an assertion
    # nobody made; `DiscoveredSchema` renders the column empty instead.
    delivered_fields = safe_metadata.get("fields") or []
    delivery_codes = [
        str(item.get("code"))
        for item in (delivery.get("exceptions") if delivery else []) or []
        if isinstance(item, dict) and item.get("code")
    ]
    from core.inbound_discovery import (  # noqa: PLC0415
        NO_ARRIVAL_EXPECTATION,
        NO_DELIVERY_YET,
        NO_INBOUND_DOMAIN,
        NOT_ADDRESSABLE_YET,
    )

    exceptions: list[dict[str, Any]] = []
    if channel_state.get("domain") != "configured":
        exceptions.append({"code": NO_INBOUND_DOMAIN})
    if channel_state.get("capability") == "not_addressable_yet":
        exceptions.append({"code": NOT_ADDRESSABLE_YET})
    if channel_state.get("arrival") != "declared":
        exceptions.append({"code": NO_ARRIVAL_EXPECTATION})
    if delivered_fields:
        exceptions.extend({"code": code} for code in delivery_codes)
    else:
        # NOT A FAILURE, and the code says so. Nothing has arrived is the normal
        # state of a channel before its first delivery; the adapter that broke is
        # a 503 the route raises, and it names itself.
        exceptions.append({"code": delivery_codes[0] if delivery_codes else NO_DELIVERY_YET})
    coverage = {
        "domain": str(channel_state.get("domain") or "not_configured"),
        "capability": str(channel_state.get("capability") or "not_addressable_yet"),
        "format": str(channel_state.get("format") or "not_declared"),
        "sender": str(channel_state.get("sender") or "token_only"),
        "arrival": str(channel_state.get("arrival") or "not_declared"),
        "delivery": "observed" if delivered_fields else "none",
        # THE DOMAIN TRAVELS, THE ADDRESS DOES NOT. A verified domain is a fact
        # about this deployment; the address is a secret rendered exactly once,
        # against a Datastream that does not exist yet.
        "inbound_domain": str(channel_state.get("inbound_domain") or "not_configured"),
        "address_form": str(channel_state.get("address_form") or "not_allocated"),
        "allowed_senders": str(len(channel_state.get("allowed_senders") or [])),
        "expected_interval_minutes": str(
            channel_state.get("expected_interval_minutes") or "not_declared"
        ),
        "channel": str(channel_state.get("channel") or request.get("channel") or "unknown"),
    }
    template = channel_state.get("template")
    if isinstance(template, dict):
        coverage["template_code"] = str(template.get("template_code") or "unavailable")
        coverage["template_version"] = str(template.get("version") or "unavailable")
        safe_metadata.setdefault("grain", template.get("grain"))
    return {
        "adapter_ref": "managed_feed.channel_contract.v1",
        "safe_metadata": safe_metadata,
        "coverage": coverage,
        "exceptions": exceptions,
    }


def observe_connector_contract(
    request: dict[str, Any], *, contract: dict[str, Any]
) -> dict[str, Any]:
    """Project the persisted Connector contract without calling or mutating a provider."""
    manifest = contract.get("contract") if isinstance(contract.get("contract"), dict) else contract
    capabilities = (
        manifest.get("source_capabilities")
        if isinstance(manifest.get("source_capabilities"), dict)
        else manifest
    )
    report_ref = request.get("report_ref")
    reports = [item for item in list(capabilities.get("reports") or []) if isinstance(item, dict)]
    selected = (
        next((item for item in reports if item.get("id") == report_ref), None)
        if report_ref
        else None
    )
    fields = list(capabilities.get("fields") or [])
    return {
        "adapter_ref": "connector.contract.readonly.v1",
        "safe_metadata": {
            "report_refs": [str(item.get("id")) for item in reports[:200] if item.get("id")],
            "field_ids": [
                str(item.get("field_id"))
                for item in fields[:200]
                if isinstance(item, dict) and item.get("field_id")
            ],
            "date_fields": [
                str(item.get("field_id"))
                for item in fields[:200]
                if isinstance(item, dict) and item.get("kind") == "date"
            ],
            "grain": (selected or {}).get("supported_grains"),
            "history": (selected or {}).get("history"),
            "cadence": (selected or {}).get("cadence"),
            "quota_cost": (selected or {}).get("quota_cost"),
        },
        "coverage": {"contract": "available", "report": "available" if selected else "unavailable"},
        "exceptions": [] if selected or not report_ref else [{"code": "report_unavailable"}],
    }


def _row_bucket(count: int) -> str:
    if count == 0:
        return "0"
    if count < 100:
        return "1-99"
    if count < 1_000:
        return "100-999"
    if count < 10_000:
        return "1000-9999"
    return "10000+"
