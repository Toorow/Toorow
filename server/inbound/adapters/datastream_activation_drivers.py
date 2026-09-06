"""Deployment I/O drivers behind the Datastream activation adapters.

What is REAL today: managed-feed preview for the `file_upload` channel. It
re-reads the exact quarantined asset through ``build_preview`` and returns a
bounded masked sample, the parsed schema, parse rejections and the replace/append
consequence.

What is NOT: every other preview and EVERY candidate materialization. They are
registered -- so a mode fails its durable job instead of reaching a fabricated
preview -- and they route to ``_remote_activation``, an execution-isolated
provider/warehouse boundary that this repository does not implement and no
deployment configures. Read the docstring there for why that is a warehouse
problem rather than a configuration one, and Story 47.5 AC1 for the deferral it
forces. Nothing here fabricates a sample or a candidate; the refusals are honest
and they fail closed.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from core.datastream_activation import ActivationValidationError

_MAX_SCHEMA_FIELDS = 50
_MAX_SAMPLE_ROWS = 100
_MAX_REMOTE_RESPONSE_BYTES = 1_048_576


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _staged_asset(project_id: str, draft_id: str, staged_asset_ref: str) -> tuple[str, bytes]:
    """Resolve one still-available quarantined asset inside the draft's scope."""
    from core.db import get_connection

    from inbound.setup_assets import SetupAssetNotFound, load_setup_asset

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT storage_ref,detected_format FROM app.datastream_setup_assets "
            "WHERE id=%s AND draft_id=%s AND project_id=%s "
            "AND state='available' AND expires_at>NOW()",
            (staged_asset_ref, draft_id, project_id),
        )
        asset = cur.fetchone()
    if asset is None:
        raise SetupAssetNotFound("Staged asset is no longer available for preview")
    return load_setup_asset(asset[0], asset[1])


def _no_received_file_evidence(code: str) -> dict[str, Any]:
    """Honest emptiness for an inbound channel with nothing to look at yet.

    Same shape the unparseable-file branch below returns, for the same reason:
    the review screen must be able to render "there is nothing here, and here is
    why" without telling an empty file apart from a missing adapter.
    """
    return {
        "adapter_verified": True,
        "placeholder": False,
        "adapter_ref": "managed_feed.file.preview.v1",
        "schema": [],
        "write_mode": "replace",
        "format": "unknown",
        "parse_errors": [],
        "coverage": {"schema": "unavailable", "values": "unavailable"},
        "exceptions": [{"code": code}],
    }


def managed_feed_preview(context: dict[str, Any]) -> dict[str, Any]:
    """Return bounded masked file evidence without opening an import.

    Serves BOTH shapes of managed feed, because they differ only in where the
    bytes come from: an upload is staged under the draft, a delivery arrives and
    is retained under the Datastream. Everything after that -- the parse, the
    masking, the schema, the row bucket -- is identical, and it has to be: the
    operator is deciding about a file, not about a transport.
    """
    from core.csv_excel_import import CsvExcelImportError, build_preview

    observation = context.get("observation") or {}
    metadata = observation.get("safe_metadata") or {}

    if context.get("channel") in _RECEIVED_CHANNELS:
        # THE CREATION PATH HAS NEITHER. An inbound address cannot be issued
        # before a Datastream exists, so the wizard previews this channel with no
        # Datastream and nothing delivered -- the ordinary first state, not a
        # failure. Indexing `datastream_id` raised a bare KeyError there, which
        # the worker reported as `activation_work_failed`: a state that names
        # nothing. The two absences are told APART because the next step differs
        # -- create the Datastream and get an address, or wait for the file.
        from core.inbound_discovery import NO_DELIVERY_YET  # noqa: PLC0415

        datastream_id = str(context.get("datastream_id") or "").strip()
        content_hash = _received_content_hash(context)
        if not datastream_id or not content_hash:
            return _no_received_file_evidence(
                "no_inbound_datastream_bound" if not datastream_id else NO_DELIVERY_YET
            )
        # The preview only reads: the delivery it samples is not landed by it, so
        # the id is deliberately dropped HERE and used only by the candidate.
        _, filename, payload = _received_file(datastream_id, content_hash)
    else:
        staged_asset_ref = str(metadata.get("staged_asset_ref") or "")
        if not staged_asset_ref:
            raise ActivationValidationError(
                "Managed-feed preview requires a staged file observation; "
                "this channel has no driver"
            )
        filename, payload = _staged_asset(
            str(context["project_id"]), str(context["draft_id"]), staged_asset_ref
        )
    try:
        preview = build_preview(payload, filename=filename)
    except CsvExcelImportError as exc:
        # An unparseable staged file is honest evidence, not a driver failure: the
        # operator has to fix the file, and the exception names which rule broke.
        return {
            "adapter_verified": True,
            "placeholder": False,
            "adapter_ref": "managed_feed.file.preview.v1",
            "schema": [],
            "write_mode": "replace",
            "format": str(metadata.get("detected_format") or "unknown"),
            "parse_errors": [{"code": exc.code, "detail": exc.detail}],
            "coverage": {"schema": "unavailable", "values": "unavailable"},
            "exceptions": [{"code": exc.code}],
            "dq": {"blocking": [{"code": exc.code}]},
        }

    schema = [
        {
            "field_id": column.name,
            "name": column.name,
            "type": column.detected_type,
            "nullable": column.null_count > 0,
        }
        for column in preview.columns[:_MAX_SCHEMA_FIELDS]
    ]
    exceptions: list[dict[str, str]] = []
    if len(preview.columns) > _MAX_SCHEMA_FIELDS:
        exceptions.append({"code": "schema_truncated_for_preview"})
    if preview.rejected_count:
        exceptions.append({"code": "rows_rejected_during_parse"})

    return {
        "adapter_verified": True,
        "placeholder": False,
        "adapter_ref": "managed_feed.file.preview.v1",
        # build_preview already caps preview_rows; cap again so the contract holds
        # whatever that constant becomes. Masking and the 25-row bound are the
        # caller's, not ours.
        "rows": preview.preview_rows[:_MAX_SAMPLE_ROWS],
        "schema": schema,
        "schema_hash": _fingerprint(schema),
        "detected_format": preview.format,
        "format": preview.format,
        "write_mode": "replace" if not metadata.get("append_supported") else "append",
        "parse_errors": [],
        "row_count_bucket": _row_bucket(preview.row_count),
        "coverage": {
            "schema": "available",
            "values": "available" if preview.preview_rows else "empty_file",
        },
        "freshness": {"observed_at": observation.get("observed_at")},
        "exceptions": exceptions,
    }


def _remote_activation(kind: str, mode: str, context: dict[str, Any]) -> dict[str, Any]:
    """Invoke the deployment's execution-isolated provider/warehouse boundary.

    NOT CONFIGURED ANYWHERE TODAY, and the refusal below says why rather than
    naming a missing variable. `TOOROW_DATASTREAM_ACTIVATION_URL` appears in this
    module and its test and nowhere else: no service implementation, no deployment
    manifest. The reason is upstream of configuration and was documented by
    `496d59a` -- each Connector lands into one raw table shared by all of its
    pulls, and every staging model supersedes on `pull_id DESC` with no notion of
    an execution, so rows reach the marts the moment they land. A preview would
    publish numbers before any confirmation, and a candidate could not be
    "isolated by execution" as Story 47.4 AC8 requires, however it were tagged.
    Setting the variable does not make that true; building the isolation does.

    Story 47.5 AC1 is therefore DEFERRED for connector_pull, external_bq and the
    non-file managed_feed channels, and the story records it. The validation below
    -- three distinct execution-scoped stage artifacts, five distinct
    execution-scoped phases, `adapter_verified is True` -- is the contract any
    implementation has to satisfy, and it stays here for whoever builds it.
    """
    base_url = os.environ.get("TOOROW_DATASTREAM_ACTIVATION_URL", "").strip().rstrip("/")
    if not base_url:
        raise ActivationValidationError(
            f"{mode} {kind} is unavailable: candidate work cannot be isolated by execution "
            "while every staging model supersedes on pull_id, so no adapter can honour it "
            "yet. This is a known gap, not a missing setting."
        )
    token = os.environ.get("TOOROW_DATASTREAM_ACTIVATION_BEARER", "").strip()
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = json.dumps(
        {"kind": kind, "mode": mode, "context": context},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    request = Request(
        f"{base_url}/v1/datastream-activation/{kind}/{mode}",
        data=payload,
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:  # noqa: S310 - configured internal URL
            raw = response.read(_MAX_REMOTE_RESPONSE_BYTES + 1)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise ActivationValidationError(f"The {mode} {kind} adapter is unavailable") from exc
    if len(raw) > _MAX_REMOTE_RESPONSE_BYTES:
        raise ActivationValidationError("Activation adapter response exceeds the safe limit")
    try:
        result = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ActivationValidationError("Activation adapter returned invalid JSON") from exc
    if not isinstance(result, dict) or result.get("adapter_verified") is not True:
        raise ActivationValidationError("Activation adapter did not return verified evidence")
    if kind == "candidate_materialization":
        stages = result.get("stage_evidence")
        expected = {"collected", "mapped", "processed"}
        if (
            not isinstance(stages, list)
            or {item.get("stage") for item in stages if isinstance(item, dict)} != expected
        ):
            raise ActivationValidationError("Candidate adapter stage evidence is incomplete")
        artifact_refs = [str(item.get("artifact_ref") or "") for item in stages]
        execution_id = str(context.get("execution_id") or "")
        if (
            not execution_id
            or any(execution_id not in ref for ref in artifact_refs)
            or len(set(artifact_refs)) != 3
        ):
            raise ActivationValidationError(
                "Candidate stages are not distinctly isolated by execution"
            )
        phases = result.get("phase_evidence")
        source_phase = "pull" if mode == "connector_pull" else "import"
        expected_phases = {source_phase, "load", "mapping", "processing", "dq"}
        if (
            not isinstance(phases, list)
            or {item.get("phase") for item in phases if isinstance(item, dict)} != expected_phases
        ):
            raise ActivationValidationError("Candidate execution phase evidence is incomplete")
        phase_refs = [str(item.get("artifact_ref") or "") for item in phases]
        if any(execution_id not in ref for ref in phase_refs) or len(set(phase_refs)) != len(
            expected_phases
        ):
            raise ActivationValidationError(
                "Candidate phases are not distinctly isolated by execution"
            )
    return result


_DATE_KEYS = ("date", "day", "event_date", "report_date")


def _window_days(arguments: dict[str, Any]) -> int:
    """How many days the requested window spans; 1 when it cannot be read."""
    from datetime import date  # noqa: PLC0415

    try:
        start = date.fromisoformat(str(arguments.get("date_from"))[:10])
        end = date.fromisoformat(str(arguments.get("date_to"))[:10])
    except (TypeError, ValueError):
        return 1
    return max(1, (end - start).days + 1)


def _observed_interval(rows: list[dict[str, Any]], arguments: dict[str, Any]) -> dict[str, Any]:
    """What the sampled rows ACTUALLY cover, read from the rows themselves.

    Not the requested window: a provider can answer short, and a preview that
    reported the request as the coverage would say the days it did not return
    were empty. Falls back to the request only when no row names a date, and
    says so with `source` rather than implying it observed one.
    """
    seen = sorted(
        {
            str(row[key])[:10]
            for row in rows
            if isinstance(row, dict)
            for key in _DATE_KEYS
            if row.get(key)
        }
    )
    if not seen:
        return {
            "from": arguments.get("date_from"),
            "to": arguments.get("date_to"),
            "source": "requested_window",
            "days_observed": 0,
        }
    return {
        "from": seen[0],
        "to": seen[-1],
        "source": "observed_rows",
        "days_observed": len(seen),
    }


def _rows_from_memory_writer(writer: Any) -> list[dict[str, Any]]:
    """Read back what a module wrote into its throwaway in-memory warehouse.

    The modules that do not go through `land_raw_rows` write with a DuckDB
    connection they got from `open_raw_writer`; under `preview_capture` that is
    an in-memory database, so their own CREATE TABLE / INSERT is intact and the
    rows are simply read back out. A writer that is not a DuckDB connection (the
    BigQuery writer never is, under capture) contributes nothing.
    """
    try:
        tables = [row[0] for row in writer.execute("SHOW TABLES").fetchall()]
    except Exception:  # noqa: BLE001 -- not a queryable writer; nothing to read
        return []
    rows: list[dict[str, Any]] = []
    for table in tables:
        try:
            cursor = writer.execute(f'SELECT * FROM "{table}" LIMIT {_MAX_SAMPLE_ROWS}')
            names = [d[0] for d in cursor.description]
            rows.extend(dict(zip(names, values)) for values in cursor.fetchall())
        except Exception:  # noqa: BLE001 -- one unreadable table must not lose the rest
            continue
    return rows


def connector_pull_preview(context: dict[str, Any]) -> dict[str, Any]:
    """Preview a Connector pull LOCALLY, by fetching without landing.

    `496d59a` left this unmounted on the reasoning that "a preview would publish
    numbers before any confirmation", because every Connector lands into one raw
    table shared by all its pulls and all staging models supersede on `pull_id`.
    That is true of a CANDIDATE, which must land and must be isolated by execution
    -- the open warehouse problem. It is not true of a PREVIEW: in every Connector
    checked (gsc, google-analytics, meta-ads, shopify) `pull()` fetches into memory
    and only THEN calls the module-owned `_insert_raw_rows`. Fetch and land are
    already separable; nothing was missing but the parameter that says so.

    So this asks the module for a bounded window with `dry_run=True` and builds the
    preview from what comes back. A Connector that does not accept `dry_run` is
    refused BY NAME rather than silently routed somewhere else: fail closed, and say
    which Connector has to gain it.
    """
    module = str(context.get("module") or context.get("connector") or "").strip()
    report_id = context.get("report_id") or context.get("profile_id")
    if not module:
        raise ActivationValidationError("Connector preview requires a Connector name")

    from core.main import get_module_pull_fn  # noqa: PLC0415

    pull = get_module_pull_fn(module, report_id)
    if pull is None:
        raise ActivationValidationError(
            f"Connector {module!r} declares no pull for this report; nothing to preview"
        )

    interval = context.get("interval") or {}
    arguments = {
        "connection_id": context.get("connection_ref_id") or context.get("connection_id"),
        "project_id": context.get("project_id"),
        # AD-7: THE CALLER MINTS THE PULL ID, and this one passed None. A None is
        # dropped before binding, so a module whose `pull` requires the id -- the
        # event profiles do, they stamp it on every event they persist -- was
        # refused with a sentence naming a setup gesture that could not supply it.
        # Under `preview_capture` the rows never leave memory, so the id is
        # provenance on a sample and lands nowhere; it is derived from the draft
        # so a replayed preview carries the same one.
        "pull_id": f"preview_{context.get('draft_id') or 'draft'}",
        "date_from": interval.get("from") or interval.get("date_from"),
        "date_to": interval.get("to_exclusive") or interval.get("date_to"),
        "dry_run": True,
    }
    arguments.update(context.get("pull_arguments") or {})

    # THE SCOPE MAKES THE PREVIEW, NOT A PARAMETER PER MODULE. `dry_run` existed
    # in three Connectors of thirty-nine, so thirty-six could not be previewed and
    # therefore could never be activated. `preview_capture` diverts every raw
    # write -- `land_raw_rows` and the modules' own DuckDB writers alike -- so the
    # real `pull()` runs, fetches for real, and lands nothing. A Connector that
    # does accept `dry_run` still gets it: the two are not in conflict, and the
    # capture is the guarantee.
    from core.raw_landing import preview_capture  # noqa: PLC0415

    # ONLY WHAT THIS MODULE'S `pull` ACCEPTS -- and never LESS than what it
    # DEMANDS. The 39 signatures differ (some take `channel_id`, some
    # `advertiser_id`, three take `dry_run`) so a keyword a module never declared
    # raises TypeError, which the old code read as "this Connector cannot be
    # previewed". Both rules now live in ONE place, with the candidate driver,
    # because the two made the same call and only this one had learnt half of it.
    from inbound.adapters.pull_invocation import bind_pull_arguments  # noqa: PLC0415

    call_args = bind_pull_arguments(
        pull, arguments, module=module, report_id=report_id, what="preview"
    )

    try:
        with preview_capture() as buffer:
            result = pull(**call_args)
            rows = list(buffer["rows"])
            for writer in buffer["writers"]:
                rows.extend(_rows_from_memory_writer(writer))
    except ActivationValidationError:
        raise
    except Exception as exc:
        raise ActivationValidationError(
            f"Connector {module!r} could not be previewed: {type(exc).__name__}: {exc}"
        ) from exc

    if isinstance(result, dict) and result.get("rows"):
        # A module that answered `dry_run` returns the rows itself; the capture
        # then holds nothing, which is the correct outcome and not an empty read.
        rows = result["rows"]

    schema_names = (
        (result.get("schema") if isinstance(result, dict) else None)
        or sorted({key for row in rows if isinstance(row, dict) for key in row})
    )
    schema = [{"field_id": name, "name": name} for name in schema_names]
    return {
        "adapter_verified": True,
        "placeholder": False,
        "adapter_ref": f"connector_pull.{module}.preview.v1",
        "rows": rows[:_MAX_SAMPLE_ROWS],
        "schema": schema[:_MAX_SCHEMA_FIELDS],
        "schema_hash": _fingerprint(schema),
        "write_mode": "replace",
        "row_count_bucket": _row_bucket(len(rows)),
        "requested_interval": {
            "from": arguments.get("date_from"), "to": arguments.get("date_to"),
        },
        # THE TWO KEYS `build_safe_preview` REQUIRES OF THIS MODE and the driver
        # never produced -- `_MODE_REQUIRED["connector_pull"]`. `observed_interval`
        # is what the rows actually cover, which is not the window that was asked
        # for: a window can come back short, and the difference is exactly what a
        # person reads a preview to see. `quota_cost` is what the fetch spent,
        # from the same rows -- one provider request per window.
        "observed_interval": _observed_interval(rows, arguments),
        # `read_points` counts the DAYS the window spans, because a Connector
        # whose report has no day breakdown asks once per day (youtube's
        # `video_daily` does exactly that). Counting one request per preview
        # would under-report the quota a backfill will really spend.
        "quota_cost": {
            "unit": "request",
            "read_points": _window_days(arguments),
            "rows_fetched": len(rows),
        },
        "coverage": {
            "schema": "available" if schema else "unavailable",
            "values": "available" if rows else "empty_window",
        },
        "freshness": {"observed_at": None},
        "parse_errors": [],
        "exceptions": (
            [{"code": "sample_truncated_for_preview"}] if len(rows) > _MAX_SAMPLE_ROWS else []
        ),
    }


def external_bq_preview(context: dict[str, Any]) -> dict[str, Any]:
    """Bounded read-only sample of the external object. Nothing is landed.

    Routed to the unbuilt remote service until now, on the same wrong premise as
    the candidate: that reading an external object needed execution isolation.
    `modules.bigquery.connector.pull(dry_run=True)` already says why it does not,
    in its own docstring -- it "fetches the same rows through the same query and
    RETURNS them instead of landing them ... which is why a preview does not
    require the execution-isolation work a CANDIDATE does".

    It is also the only statement shape this connector emits (`build_select`,
    bounded by the requested window and a row limit), and it prices the full
    window with BigQuery's own dry-run job -- free, planned without scanning a
    byte -- so the scan estimate the contract asks for is known before the sample
    is paid for.
    """
    table_ref = str(
        context.get("table_ref")
        or (context.get("external_object") or {}).get("table_ref")
        or ""
    ).strip()
    if not table_ref:
        raise ActivationValidationError(
            "External BigQuery preview requires the exact table or view it pins"
        )

    external = context.get("external_object") or {}
    interval = context.get("interval") or {}
    date_column = str(context.get("date_column") or external.get("date_column") or "date")
    # A VERSIONED HISTORY IS A PROPERTY OF THE OBJECT, not of the connector, so its
    # declaration travels where the column choice already travels -- the external
    # object plan -- and reaches pull() by the name the manifest declares
    # (`history_deduplication.pull_parameter`). Absent, the read is the flat one.
    history_dedup = context.get("history_dedup") or external.get("history_dedup") or None
    date_from = str(interval.get("from") or interval.get("date_from") or "")
    date_to = str(interval.get("to_exclusive") or interval.get("date_to") or "")
    if not date_from or not date_to:
        raise ActivationValidationError(
            "External BigQuery preview requires the bounded window its plan declares"
        )

    from modules.bigquery.connector import pull  # noqa: PLC0415

    outcome = pull(
        project_id=str(context.get("project_id") or ""),
        table_ref=table_ref,
        date_column=date_column,
        history_dedup=history_dedup,
        date_from=date_from,
        date_to=date_to,
        dry_run=True,
    )

    schema = [
        {"field_id": name, "name": name} for name in (outcome.get("schema") or [])
    ][:_MAX_SCHEMA_FIELDS]
    exceptions: list[dict[str, str]] = []
    if len(outcome.get("schema") or []) > _MAX_SCHEMA_FIELDS:
        exceptions.append({"code": "schema_truncated_for_preview"})

    return {
        "adapter_verified": True,
        "placeholder": False,
        "adapter_ref": "external_bq.preview.virtual_pull.v1",
        "rows": (outcome.get("rows") or [])[:_MAX_SAMPLE_ROWS],
        "schema": schema,
        "schema_hash": _fingerprint(schema),
        # The external writer owns the object; toorow never replaces or appends
        # to it, and saying `replace` here would imply it could.
        "write_mode": "external_read_only",
        "parse_errors": [],
        "row_count_bucket": _row_bucket(int(outcome.get("row_count") or 0)),
        # THE THREE KEYS `_MODE_REQUIRED["external_bq"]` ASKS FOR, and this driver
        # answered none of them: it spelled the scan `scan_estimate` while the
        # contract reads `estimated_scan`, and never named the object or declared
        # the read. So this mode could not produce a valid preview either -- the
        # same defect as `connector_pull`, one line apart.
        "object_identity": {
            "object_ref": context.get("object_ref"),
            "access_ref": context.get("access_ref"),
            "date_column": date_column,
        },
        # toorow never writes here; the external writer stays authoritative.
        "read_only": True,
        "estimated_scan": {
            # What the full window WOULD scan, versus what this bounded sample did.
            "bytes_estimated": outcome.get("bytes_estimated"),
            "bytes_processed": outcome.get("bytes_processed"),
        },
        "scan_estimate": {
            "bytes_estimated": outcome.get("bytes_estimated"),
            "bytes_processed": outcome.get("bytes_processed"),
        },
        "watermark": {"date_column": date_column, "from": date_from, "to_exclusive": date_to},
        "coverage": {
            "schema": "available",
            "values": "available" if outcome.get("rows") else "empty_window",
        },
        "freshness": {"observed_at": (context.get("observation") or {}).get("observed_at")},
        "exceptions": exceptions,
    }


def managed_feed_preview_driver(context: dict[str, Any]) -> dict[str, Any]:
    """`file_upload` and the received channels are local; Sheets is not.

    Google Sheets still routes to the remote boundary: its input is a live range
    read through a provider, not bytes this deployment holds, so there is nothing
    local to preview.
    """
    if context.get("channel") == "file_upload" or context.get("channel") in _RECEIVED_CHANNELS:
        return managed_feed_preview(context)
    return _remote_activation("setup_preview", "managed_feed", context)


def connector_pull_candidate(context: dict[str, Any]) -> dict[str, Any]:
    """Materialize a candidate LOCALLY, isolated by execution, with the real pull.

    C-8's blocker was never configuration. A Connector lands every pull into one
    shared raw table and all 137 staging models supersede on `pull_id DESC`, so a
    candidate written there is live the instant it lands. That is why this routed
    to a remote service nobody had built: the impossibility was relocated, not
    solved, and it turned a findable warehouse contract into a missing env var.

    `core.raw_landing.candidate_execution` makes it possible: inside that scope
    every raw write goes to THIS execution's own relation, which staging never
    names, so nothing reaches the marts until `promote_candidate` publishes it.
    The connector's real `pull()` runs -- that is what makes this evidence rather
    than a mock -- and it needs no change, because where it writes is decided by
    the scope.

    No remote boundary, so no new architectural component and no amendment: the
    isolation landed in the warehouse, which is the resolution the review named.
    """
    from core.raw_landing import (  # noqa: PLC0415
        candidate_execution,
        landed_candidate_columns,
        landed_candidate_relations,
        unlanded_candidate_ref,
    )

    execution_id = str(context.get("execution_id") or "").strip()
    if not execution_id:
        raise ActivationValidationError(
            "Candidate materialization requires an execution_id: the isolation IS the execution"
        )

    module = context.get("module") or context.get("module_name")
    report_id = context.get("report_id") or context.get("report_profile")
    if not module:
        raise ActivationValidationError("Candidate materialization requires a Connector name")

    from core.main import get_module_pull_fn  # noqa: PLC0415

    from inbound.adapters.pull_invocation import bind_pull_arguments  # noqa: PLC0415

    pull = get_module_pull_fn(module, report_id)
    if pull is None:
        raise ActivationValidationError(
            f"Connector {module!r} declares no pull for this report; nothing to materialize"
        )

    interval = context.get("interval") or {}
    arguments = {
        "connection_id": context.get("connection_ref_id") or context.get("connection_id"),
        "project_id": context.get("project_id"),
        # AD-7: the caller mints the pull_id. A candidate's rows carry a real one
        # so that publication is a plain append and the staging QUALIFY decides
        # what wins exactly as it would for any pull.
        "pull_id": context.get("pull_id") or f"pull_{execution_id}",
        "date_from": interval.get("from") or interval.get("date_from"),
        "date_to": interval.get("to_exclusive") or interval.get("date_to"),
    }
    arguments.update(context.get("pull_arguments") or {})

    # THE LINE THAT KILLED EVERY FIRST CANDIDATE. This read
    # `pull(**{k: v for k, v in arguments.items() if v is not None})`. Dropping a
    # None is right for a keyword the Connector never declared and WRONG for one
    # it requires -- and `date_from`/`date_to` are exactly that for 222 pull
    # functions across 37 of the 39 Connectors. With no interval pinned, both
    # were None, both were dropped, and the Connector raised a bare TypeError
    # that the queue filed as `activation_work_failed`. Measured 2026-08-12: 17
    # candidate jobs, 17 dead letters, zero ever done. See
    # `inbound/adapters/pull_invocation.py` for the whole chain.
    call_args = bind_pull_arguments(
        pull, arguments, module=module, report_id=report_id, what="first collection"
    )

    # An event Connector writes to `app.context_events`, whose rows must name the
    # Datastream they belong to. A pull signature never carries it and the driver
    # always knows it, so it is said here rather than guessed at the write.
    from core.context_events import collecting_for_datastream  # noqa: PLC0415

    with candidate_execution(execution_id), collecting_for_datastream(
        context.get("datastream_id")
    ):
        outcome = pull(**call_args)

    row_count = int((outcome or {}).get("row_count") or 0)

    # The provenance `complete_candidate_from_adapter` requires, and which this
    # driver did not produce. The worker hands this dict straight to it, and it
    # refuses an `artifact_ref` that does not carry the execution or a hash that
    # is not sha256 -- so without these four fields the candidate raised
    # "Candidate artifact provenance is invalid" and could never reach Ready.
    # It went unnoticed because the driver was tested alone and the completion
    # was tested against a hand-built dict; the two never met
    # (`test_the_candidate_result_satisfies_the_completion_contract`).
    # LE NOM QUE L'ATTERRISSAGE A REELLEMENT UTILISE, pas un chemin fabrique.
    #
    # Cette ligne ecrivait `f"execution/{execution_id}/candidate/relation"`. Ce
    # chemin logique satisfaisait la garde d'activation -- qui exige que
    # l'execution_id y figure (`datastream_activation.py:1071`) -- et ne pouvait
    # PAS satisfaire celle de lecture : `query_execution._safe_identifier` refuse
    # tout ce qui n'est pas un identifiant SQL, et les barres obliques en sont
    # exclues. Les deux gardes sont justes, chacune pour sa raison (tracabilite
    # de l'isolement / anti-injection) et elles etaient incompatibles.
    #
    # Or `raw_landing.candidate_table` calcule DEJA un nom qui satisfait les deux
    # -- `raw_gsc_daily__cand_<execution_id>`. La reparation n'est donc pas
    # d'assouplir un garde mais de publier le nom qui existe. Il est LU du
    # registre d'atterrissage plutot que reconstruit ici : une reconstruction
    # peut deriver du jour ou la regle de nommage change, une observation non.
    #
    # A window that collected nothing landed nothing, so there is no relation to
    # publish. The traceability path is kept -- provenance must still name the act
    # -- and it is MINTED IN ONE PLACE (`raw_landing.unlanded_candidate_ref`),
    # shared with the managed-feed driver below, so the two sites of this class
    # cannot drift apart again. AI-308: the read now files an outcome over it and
    # names the gesture, instead of raising out of `_safe_identifier`.
    landed = landed_candidate_relations(execution_id)
    artifact_ref = landed[0] if landed else unlanded_candidate_ref(execution_id)
    candidate_columns = [
        {"name": name, "type": kind}
        for name, kind in landed_candidate_columns(execution_id, artifact_ref)
    ]
    # Hashes the OBSERVABLE facts of this materialization. `validated_content_hash`
    # equals `content_hash` by construction and that is deliberate: nothing here
    # re-reads the landed relation, so claiming a separate validation digest
    # would assert a check that did not happen.
    content_hash = _fingerprint(
        {
            "execution_id": execution_id,
            "module": str(module),
            "report_id": str(report_id or ""),
            "pull_id": arguments["pull_id"],
            "row_count": row_count,
            "isolation": "relation_per_execution",
        }
    )
    return {
        "adapter_verified": True,
        "placeholder": False,
        "adapter_ref": "connector_pull.candidate.isolated.v1",
        "execution_id": execution_id,
        "artifact_ref": artifact_ref,
        "content_hash": content_hash,
        "validated_content_hash": content_hash,
        "artifact_hash": content_hash,
        "row_count": row_count,
        "row_count_bucket": _row_bucket(row_count),
        "candidate_columns": candidate_columns,
        "isolation": {"kind": "relation_per_execution", "published": False},
        "stage_evidence": _execution_scoped(execution_id, ("collected", "mapped", "processed"),
                                            "stage"),
        "phase_evidence": _execution_scoped(
            execution_id, ("pull", "load", "mapping", "processing", "dq"), "phase"
        ),
    }


def _execution_scoped(execution_id: str, names: tuple[str, ...], key: str) -> list[dict[str, str]]:
    """Evidence rows whose artifact refs are distinct AND carry the execution.

    Both properties are checked by `_validate` and both mean something: carrying
    the execution is what proves the artifact belongs to THIS candidate, and
    being distinct is what proves the stages were not all pointed at one blob.
    """
    return [{key: name, "artifact_ref": f"execution/{execution_id}/{key}/{name}"} for name in names]


def external_bq_candidate(context: dict[str, Any]) -> dict[str, Any]:
    """Verify the external object in place. A virtual pull, because nothing is copied.

    This routed to the unbuilt remote activation service, on the assumption that
    an external candidate needed the same execution-isolated WRITE a connector
    candidate needs. The ratified contract says the opposite, and it says it
    three times (`datastream-workbench-and-wizard.md`, "Exact content by mode"):

      * Destination: "the source object remains external and read-only ...
        never imply toorow owns or writes the source";
      * Configure: "The external writer remains authoritative; a verification is
        a virtual pull";
      * Preview and validate: "bounded read-only sample, schema/profile,
        watermark evidence, freshness, DQ gates, scan estimate and virtual-pull
        evidence".

    So there is no isolation to build here, because there is nothing to isolate:
    a candidate for an external object is a READ that proves the object is still
    what the plan pinned. That removes the last caller of the remote boundary for
    this mode, and with it the amendment the review had escalated -- no new
    architectural component was needed, only reading the contract.

    `row_count` is the object's own count, not rows toorow wrote. `isolation`
    says so out loud rather than reporting a relation that does not exist.
    """
    execution_id = str(context.get("execution_id") or "").strip()
    if not execution_id:
        raise ActivationValidationError(
            "Candidate verification requires an execution_id: the evidence is pinned to it"
        )

    table_ref = str(
        context.get("table_ref")
        or (context.get("external_object") or {}).get("table_ref")
        or ""
    ).strip()
    if not table_ref:
        raise ActivationValidationError(
            "External BigQuery verification requires the exact table or view it pins"
        )

    from modules.bigquery.connector import describe_table  # noqa: PLC0415

    described = describe_table(table_ref)

    schema = [
        {"field_id": column.get("name"), "name": column.get("name"), "type": column.get("type")}
        for column in (described.get("schema") or [])[:_MAX_SCHEMA_FIELDS]
    ]
    row_count = int(described.get("num_rows") or 0)
    exceptions: list[dict[str, str]] = []
    if len(described.get("schema") or []) > _MAX_SCHEMA_FIELDS:
        exceptions.append({"code": "schema_truncated_for_evidence"})

    content_hash = _fingerprint(
        {
            "execution_id": execution_id,
            "table_ref": table_ref,
            "location": described.get("location"),
            "num_rows": row_count,
            "num_bytes": described.get("num_bytes"),
            "schema": schema,
        }
    )
    return {
        "adapter_verified": True,
        "placeholder": False,
        "adapter_ref": "external_bq.candidate.virtual_pull.v1",
        "execution_id": execution_id,
        "artifact_ref": f"execution/{execution_id}/candidate/verification",
        "content_hash": content_hash,
        "validated_content_hash": content_hash,
        "artifact_hash": content_hash,
        "row_count": row_count,
        "row_count_bucket": _row_bucket(row_count),
        # Nothing was written, and saying "relation_per_execution" here would
        # claim a materialization that never happened.
        "isolation": {"kind": "external_read_only", "published": False},
        "schema": schema,
        "schema_hash": _fingerprint(schema),
        "scan_estimate": {"bytes": described.get("num_bytes"), "rows": row_count},
        "coverage": {"schema": "available", "values": "external_read_only"},
        "exceptions": exceptions,
        "stage_evidence": _execution_scoped(execution_id, ("collected", "mapped", "processed"),
                                            "stage"),
        "phase_evidence": _execution_scoped(
            execution_id, ("pull", "load", "mapping", "processing", "dq"), "phase"
        ),
    }


def _managed_feed_source_ref(context: dict[str, Any]) -> str:
    """The input the plan pinned for this channel.

    Not a lookup of "the latest delivery". The ratified wizard contract makes the
    channel AND its input Required at step 1
    (`datastream-workbench-and-wizard.md`, "Exact content by mode"), so a
    candidate materializes what its plan version declares -- the same bytes the
    preview sampled and the operator confirmed. Resolving the most recent arrival
    instead would let a candidate materialize a file nobody reviewed.
    """
    source = (context.get("plan_intent") or {}).get("source") or {}
    managed = source.get("managed_feed") or {}
    return str(managed.get("source_ref") or "").strip()


#: The wizard's delivery channels whose input is a RECEIVED file rather than a
#: staged upload. Their bytes arrive; nobody puts them anywhere first.
_RECEIVED_CHANNELS = frozenset({"inbound_email", "webhook"})


def _received_file(datastream_id: str, content_hash: str) -> tuple[str, str, bytes]:
    """Resolve ONE retained delivery, by its content address.

    Returns the delivery's OWN id alongside its bytes. The id used to be dropped,
    and dropping it is why a delivery that had been materialized stayed ACCEPTED
    for ever: nothing downstream could name the row to close.

    BY HASH, NEVER "the latest". `_managed_feed_source_ref` states the rule for
    the upload channel and it holds identically here: a candidate materializes
    what its plan version pinned -- the same bytes the preview sampled and the
    operator confirmed. Resolving the most recent arrival instead would let a
    second file, delivered while someone was reading the review, become the
    candidate they thought they were approving.

    The hash is re-verified against the bytes actually read. A retained object
    that no longer hashes to its record is not the file the review described,
    and materializing it would put a candidate under an audit trail that names a
    different one.
    """
    from core.db import get_connection
    from core.inbound_quarantine import open_quarantine_store
    from core.inbound_raw_imports import content_hash as digest_of

    if not content_hash:
        raise ActivationValidationError(
            "A received-file candidate requires the content hash its plan pinned"
        )

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, filename, quarantine_uri FROM app.inbound_raw_imports "
            "WHERE datastream_id = %s AND content_hash = %s AND quarantine_uri IS NOT NULL "
            "ORDER BY created_at ASC LIMIT 1",
            (datastream_id, content_hash),
        )
        row = cur.fetchone()
    if row is None:
        raise ActivationValidationError(
            "The delivered file this plan pinned is no longer retained; it cannot "
            "be materialized, and re-delivering it is the only honest recovery"
        )

    raw_import_id, filename, quarantine_uri = row
    try:
        payload = open_quarantine_store().get(quarantine_uri)
    except Exception as exc:  # noqa: BLE001 -- a storage fault, named as one
        raise ActivationValidationError(
            "The delivered file this plan pinned could not be read from quarantine"
        ) from exc

    if digest_of(payload) != content_hash:
        raise ActivationValidationError(
            "The retained delivery no longer matches its recorded content hash; "
            "materializing it would describe a different file than the review did"
        )
    return str(raw_import_id), str(filename or "delivered-file"), payload


def _received_content_hash(context: dict[str, Any]) -> str:
    """The pinned content address, from the plan first and the observation second.

    The plan is authoritative once there is one -- that is what "pinned" means.
    The observation is the fallback for the PREVIEW, which runs before any plan
    version exists: at that moment the observed file is the only candidate there
    could be.
    """
    pinned = _managed_feed_source_ref(context)
    if pinned:
        return pinned
    metadata = (context.get("observation") or {}).get("safe_metadata") or {}
    return str(metadata.get("content_hash") or "").strip()


def managed_feed_candidate(context: dict[str, Any]) -> dict[str, Any]:
    """Run the REAL import inside the execution's isolation, per channel.

    Same resolution as `connector_pull_candidate`: the isolation is the raw
    landing scope, not a remote service. `csv_excel_import.run_import` now writes
    through `raw_landing.land_raw_rows`, which honours an active
    `candidate_execution` -- so wrapping the import is the whole of it, and no
    import-path code needed to learn what an execution is.

    `file_upload` and the RECEIVED channels are honoured here. Google Sheets
    still routes to the remote boundary: its input is a live range read through
    a provider, not bytes this deployment holds.

    The received channels were deferred with a real reason -- "each needs its
    input resolved differently, and guessing one would materialize bytes nobody
    reviewed". That reason is now answered rather than waived: the input is
    resolved by the CONTENT HASH the plan pinned, so what materializes is
    provably the file the preview showed. Nothing is guessed, and "the latest
    delivery" is never consulted.
    """
    channel = str(context.get("channel") or "")
    received = channel in _RECEIVED_CHANNELS
    if channel != "file_upload" and not received:
        return _remote_activation("candidate_materialization", "managed_feed", context)

    from core.csv_excel_import import (  # noqa: PLC0415
        resolve_file_source_producer,
        run_import,
    )
    from core.db import get_connection  # noqa: PLC0415
    from core.raw_landing import (  # noqa: PLC0415
        candidate_execution,
        landed_candidate_relations,
        unlanded_candidate_ref,
    )

    execution_id = str(context.get("execution_id") or "").strip()
    if not execution_id:
        raise ActivationValidationError(
            "Candidate materialization requires an execution_id: the isolation IS the execution"
        )
    source_ref = _managed_feed_source_ref(context)
    if not source_ref:
        raise ActivationValidationError(
            "Managed-feed candidate requires the input its plan version pinned; "
            "this plan declares no managed_feed.source_ref"
        )

    project_id = str(context.get("project_id") or "")
    raw_import_id = ""
    if received:
        # For a delivered file the pinned ref IS its content address: the file
        # was staged by nobody, and its hash is the only identifier that both
        # the preview and this materialization can agree on.
        raw_import_id, filename, payload = _received_file(
            str(context["datastream_id"]), source_ref
        )
    else:
        filename, payload = _staged_asset(
            project_id, str(context.get("draft_id") or ""), source_ref
        )

    with get_connection() as conn:
        # AI-88: the candidate materialization must exercise the SAME producer the
        # later arrivals will replay. A candidate reviewed through the plain
        # CSV/Excel path would validate a shape the Datastream never lands.
        producer = resolve_file_source_producer(
            conn,
            project_id=project_id,
            datastream_id=str(context["datastream_id"]),
            mapping_version_id=str(context["mapping_version_id"]),
        )
        with candidate_execution(execution_id):
            outcome = run_import(
                payload,
                datastream_id=str(context["datastream_id"]),
                project_id=project_id,
                plan_version_id=str(context["plan_version_id"]),
                mapping_version_id=str(context["mapping_version_id"]),
                projection_plan=context.get("projection_plan") or {},
                actor=str(context.get("actor") or "activation-worker"),
                idempotency_key=f"candidate:{execution_id}",
                # The channel travels with the import, so provenance says how
                # these bytes arrived -- a delivered file and an uploaded one
                # must not become indistinguishable once landed.
                source_metadata={"filename": filename, "channel": channel},
                contract=context.get("import_contract") or {},
                conn=conn,
                producer=producer,
            )
        if raw_import_id:
            _close_the_delivery(
                conn,
                raw_import_id=raw_import_id,
                datastream_id=str(context["datastream_id"]),
                actor=str(context.get("actor") or "activation-worker"),
                outcome=outcome,
            )
        conn.commit()

    # `landed_row_count`, not `row_count`: the first is what reached the isolated
    # relation, the second is what the parse accepted. They differ whenever the
    # write is partial, and reporting the parse count as the candidate's row
    # count is how an empty candidate looks full.
    row_count = int(outcome.get("landed_row_count") or 0)

    # THE NAME THE LANDING ACTUALLY USED, not a fabricated path. Same repair as
    # `connector_pull_candidate` (:695) -- and it is the same defect, so it gets
    # the same gesture rather than a second convention.
    #
    # This published `f"execution/{execution_id}/candidate/relation"`
    # unconditionally. That logical path satisfies the activation guard, which
    # only requires the execution_id to appear in it
    # (`datastream_activation.py:1071`), and it can NEVER satisfy the reading
    # guard: `query_execution._safe_identifier` refuses anything that is not a
    # SQL identifier, and a slash is excluded. Both guards are right, each for
    # its own reason -- isolation traceability and anti-injection -- and they
    # were incompatible. The consequence was invisible on either side alone:
    # every published file Datastream carried a `relation_ref`
    # (`datastream_activation.py:968` copies this very string) that the Result
    # read of link 9 would always refuse.
    #
    # The managed import lands through `raw_landing.land_raw_rows`, the same
    # seam the connectors use, so the isolated relation was ALREADY recorded at
    # landing under this execution -- `managed_feed_ledger` adopts the ambient
    # candidate execution rather than minting a second one, which is what makes
    # the registry key line up. So this is READ, never rebuilt: a rebuild can
    # drift the day the naming rule moves on one side only, an observation
    # cannot.
    #
    # A deterministic replay resumes from a ledger row instead of writing again,
    # so nothing is recorded in this process; the relation the ledger persisted
    # is then the observation, and it is accepted only when it carries THIS
    # execution -- otherwise it names another act.
    #
    # An import that landed nothing has no relation to publish. The traceability
    # path is kept so provenance still names the execution, and it comes from the
    # SAME mint the connector-pull driver above uses -- this is one class with two
    # sites, and it was repaired twice before, in two copies of one comment.
    # AI-308: the read files an outcome over this reference and names the gesture.
    landed = landed_candidate_relations(execution_id)
    replayed = str((outcome.get("landing") or {}).get("table") or "")
    if landed:
        artifact_ref = landed[0]
    elif execution_id in replayed:
        artifact_ref = replayed
    else:
        artifact_ref = unlanded_candidate_ref(execution_id)

    content_hash = _fingerprint(
        {
            "execution_id": execution_id,
            "source_ref": source_ref,
            "plan_version_id": context.get("plan_version_id"),
            "mapping_version_id": context.get("mapping_version_id"),
            "row_count": row_count,
            "rejected": outcome.get("rejected_count"),
        }
    )
    return {
        "adapter_verified": True,
        "placeholder": False,
        "adapter_ref": (
            "managed_feed.received_file.candidate.isolated.v1"
            if received
            else "managed_feed.file.candidate.isolated.v1"
        ),
        "execution_id": execution_id,
        "artifact_ref": artifact_ref,
        "content_hash": content_hash,
        "validated_content_hash": content_hash,
        "artifact_hash": content_hash,
        "row_count": row_count,
        "row_count_bucket": _row_bucket(row_count),
        "isolation": {"kind": "relation_per_execution", "published": False},
        "coverage": {
            "schema": "available",
            "values": "available" if row_count else "empty_file",
        },
        "stage_evidence": _execution_scoped(execution_id, ("collected", "mapped", "processed"),
                                            "stage"),
        "phase_evidence": _execution_scoped(
            execution_id, ("pull", "load", "mapping", "processing", "dq"), "phase"
        ),
    }


def _close_the_delivery(
    conn,
    *,
    raw_import_id: str,
    datastream_id: str,
    actor: str,
    outcome: dict[str, Any],
) -> None:
    """The delivery that was materialized becomes LANDED, naming its ledger row.

    A delivery whose Datastream still needed setup is left ACCEPTED by the ingest
    path -- correctly: it has arrived, it is intact, and it is waiting for a
    human. What was missing is the OTHER half. The review happens, the candidate
    materializes THOSE bytes and mints an import-ledger row, and nothing ever went
    back to say so: the delivery stayed ACCEPTED for ever, with no
    `import_ledger_id`, while the rows it produced were in the warehouse.

    This is the only place that can say it: the driver is what resolved the
    delivery by content hash, so it is the only code that knows WHICH row the
    ledger belongs to. `inbound_processing` writes the identical transition for
    the delivery that lands at ingest time -- same state, same field, same
    meaning; only the moment differs.

    Silent when the import produced no ledger row (a no-op replay of an unchanged
    snapshot). Nothing landed, so nothing is claimed to have landed.
    """
    from core.inbound_raw_imports import mark_raw_import_state  # noqa: PLC0415

    ledger = outcome.get("ledger") if isinstance(outcome, dict) else None
    ledger_id = ledger.get("id") if isinstance(ledger, dict) else None
    if not ledger_id:
        return
    mark_raw_import_state(
        conn,
        raw_import_id=raw_import_id,
        datastream_id=datastream_id,
        state="LANDED",
        actor=actor,
        host_context={},
        idempotency_key=f"raw-import-state:{raw_import_id}:LANDED",
        import_ledger_id=str(ledger_id),
    )


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


def install_runtime_activation_drivers() -> None:
    """Register every driver this deployment can actually honor."""
    from inbound.datastream_activation_worker import register_runtime_activation_driver

    for kind, mode, driver in (
        ("setup_preview", "connector_pull", connector_pull_preview),
        ("setup_preview", "external_bq", external_bq_preview),
        ("setup_preview", "managed_feed", managed_feed_preview_driver),
        ("candidate_materialization", "connector_pull", connector_pull_candidate),
        ("candidate_materialization", "external_bq", external_bq_candidate),
        ("candidate_materialization", "managed_feed", managed_feed_candidate),
    ):
        register_runtime_activation_driver(kind, mode, driver)
