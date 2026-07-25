"""toorow -- Inbound ingress worker: server-driven managed-feed file ingestion.

Ingress parity keystone. A file delivered by an inbound transport (opaque
``channel`` data such as 'email' or 'webhook') for a managed-feed Datastream is
driven through the EXACT SAME import pipeline as a direct upload, so an inbound
file and a direct upload of the same bytes produce IDENTICAL results (same
ledger row shape, same execution candidate, same DQ/rejection gates).

This module is PURE ORCHESTRATION given resolved inputs:
    (datastream_id, project_id, file bytes, filename, channel, message_id, actor).
The transport wiring (delivery, quarantine, attachment extraction) is OUT of
scope -- it is the caller's concern. This function receives the already-extracted
bytes and drives ``csv_excel_import.run_import`` with the SAME arguments the
direct-upload route (``admin_api._confirm_csv_excel_import``) resolves, but
WITHOUT operator interaction: the plan version, mapping version, projection plan,
and parsing contract are all resolved from durable server-side state.

Design invariants:
  - AD-2: source-agnostic throughout. NO transport/vendor vocabulary appears in
    code, comments, or docstrings. ``channel`` is opaque data validated only
    against the Datastream's own ``config.channels`` allowlist -- this module
    never special-cases any particular channel value.
  - Fail-closed: an inbound file is UNATTENDED, so every precondition that a human
    would have satisfied interactively (correct source_kind, enabled Datastream,
    a locked plan + mapping, an allowed channel) is asserted up front and raises a
    typed error before ANY parsing or ledger write.
  - Parity: the pipeline inputs (plan_version_id, mapping_version_id,
    projection_plan, contract, write_mode, preferences) are resolved to be
    IDENTICAL to what the direct-upload path passes, so ``run_import`` cannot tell
    an inbound file from an upload.
  - Transaction ownership: this function NEVER commits or rolls back. The CALLER
    owns the transaction (mirrors ``run_import``'s own contract). The caller
    passes a live ``conn`` and commits/rolls back around this call.
  - run_import's own typed exceptions (ImportPayloadConflict, ImportInProgress,
    CsvExcelImportError, AppendUnavailable, InvalidImportContract) propagate
    UNCHANGED to the caller so the inbound worker maps them the same way the
    direct-upload route does.

ASCII-only source (AI-03). No private framework attributes (AI-02). Lazy imports
inside function bodies (no import cycle with core.main), matching the
import_templates.py / managed_feed_ledger.py style.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Source-agnostic identifiers used in idempotency + provenance metadata.
_SOURCE_KIND_MANAGED_FEED = "managed_feed"
_WRITE_MODE_REPLACE = "replace"


# ---------------------------------------------------------------------------
# Custom exceptions (fail-closed preconditions).
# ---------------------------------------------------------------------------


class InboundIngestValidationError(ValueError):
    """An input to the inbound worker is malformed (missing bytes, channel, etc.).

    A ValueError subclass so callers that already map 4xx off ValueError keep
    working; distinct type so the inbound worker's own guards are separable.
    """


class DatastreamNotIngestable(RuntimeError):
    """The Datastream cannot accept an unattended inbound import (fail closed).

    Raised when the Datastream is not found, is not a managed_feed, is disabled,
    does not permit the requested channel, or is not yet configured (missing a
    locked plan or mapping version). No parsing or ledger write is attempted.
    """


# ---------------------------------------------------------------------------
# Internal read helpers.
# ---------------------------------------------------------------------------


def _load_ingestable_datastream(
    conn,
    *,
    datastream_id: str,
    project_id: str,
) -> dict[str, Any]:
    """Read the Datastream's ingestion-relevant fields, scoped to project_id.

    Returns a dict with source_kind, enabled, config, current_plan_version_id,
    current_mapping_version_id. Returns None when the row does not exist for this
    (id, project) pair (AD-5: cross-project reads return not-found, not a leak).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT source_kind, enabled, config,
                   current_plan_version_id, current_mapping_version_id
            FROM app.datastreams
            WHERE id = %s AND project_id = %s
            """,
            (datastream_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "source_kind": row[0],
        "enabled": row[1],
        "config": row[2] or {},
        "current_plan_version_id": row[3],
        "current_mapping_version_id": row[4],
    }


def _fetch_projection_plan(
    conn,
    *,
    datastream_id: str,
    project_id: str,
    plan_version_id: str,
) -> dict[str, Any] | None:
    """Return the executable projection plan for the current plan version.

    PARITY SOURCE: the direct-upload route does not persist a standalone
    executable projection on the Datastream -- the operator UI compiles it fresh
    (POST /projection/compile) and posts it in the request body. The ONLY durable
    executable projection is the one recorded on the most recent
    ``app.datastream_executions`` row for the current plan version
    (``projection_plan_ref``). Reusing that exact blob guarantees the inbound path
    drives ``run_import`` with the SAME projection a prior direct upload used for
    this plan version -- true ingress parity, no re-compilation (which would need
    operator-only inputs: the elected dimension_projection + the source's current
    canonical breakdown).

    Returns the projection dict, or None when no prior execution exists for the
    current plan version (an un-imported, freshly-configured Datastream). The
    caller fails closed on None.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT projection_plan_ref
            FROM app.datastream_executions
            WHERE datastream_id = %s
              AND project_id = %s
              AND plan_version_id = %s
              AND projection_plan_ref IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (datastream_id, project_id, plan_version_id),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    raw = row[0]
    if isinstance(raw, dict):
        return raw
    import json  # noqa: PLC0415

    return json.loads(raw)


def _fetch_active_parsing_contract(
    conn,
    *,
    datastream_id: str,
    project_id: str,
) -> dict[str, Any] | None:
    """Return the most recent is_active parsing contract JSONB, or None.

    Reads the reusable versioned contract table so the inbound path parses the
    file with the SAME configuration a direct upload would reuse. Newest active
    version wins (mirrors the direct-upload contract read ordering).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT contract
            FROM app.csv_excel_import_contracts
            WHERE datastream_id = %s
              AND project_id = %s
              AND is_active = TRUE
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (datastream_id, project_id),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    raw = row[0]
    if isinstance(raw, dict):
        return raw
    import json  # noqa: PLC0415

    return json.loads(raw)


def _template_column_types(template_contract: dict[str, Any]) -> dict[str, str]:
    """Extract parser-usable column typing hints from a template contract.

    The semantic template contract carries a ``field_types`` map (field -> type).
    Only values that are valid ``csv_excel_import`` column types are forwarded --
    an unknown template type is DROPPED (not raised) so a template typing hint can
    never turn a fallback contract into an InvalidImportContract. Returns {} when
    no usable hint exists.
    """
    from core.csv_excel_import import SUPPORTED_COLUMN_TYPES  # noqa: PLC0415

    field_types = template_contract.get("field_types")
    if not isinstance(field_types, dict):
        return {}
    out: dict[str, str] = {}
    for name, ctype in field_types.items():
        if isinstance(name, str) and name.strip() and ctype in SUPPORTED_COLUMN_TYPES:
            out[name] = ctype
    return out


def _read_allow_empty_publication(conn, project_id: str) -> bool:
    """Read the project-scoped allow_empty_publication preference (fail closed).

    Mirrors the direct-upload route's ``_read_allow_empty_publication_pref``:
    defaults to False when the row/column is absent or on any read error, so the
    inbound and upload paths apply the SAME empty-publication policy.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT allow_empty_publication FROM app.project_preferences "
                "WHERE project_id = %s",
                (project_id,),
            )
            row = cur.fetchone()
    except Exception:  # noqa: BLE001 - fail closed on any read error.
        return False
    if row is None or row[0] is None:
        return False
    return bool(row[0])


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


def ingest_inbound_file(
    conn,
    *,
    datastream_id: str,
    project_id: str,
    file_bytes: bytes,
    filename: str | None,
    channel: str,
    message_id: str,
    actor: str,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Drive an inbound file through the direct-upload import pipeline (parity).

    Given the resolved inputs from an inbound transport, this resolves the SAME
    pipeline arguments the direct-upload route resolves -- WITHOUT operator
    interaction -- and calls ``csv_excel_import.run_import``. The result is
    byte-for-byte the outcome a direct upload of the same bytes would produce.

    Args:
      conn: caller-managed psycopg connection. This function DOES NOT commit or
        roll back -- the caller owns the transaction (mirrors run_import).
      datastream_id / project_id: the target managed-feed Datastream (AD-5 scoped).
      file_bytes: the already-extracted file payload.
      filename: the delivered filename (used for format detection); may be None.
      channel: opaque inbound channel identifier (e.g. 'email' | 'webhook'),
        validated ONLY against the Datastream's config.channels allowlist.
      message_id: the transport's idempotency/replay key. Redelivery of the SAME
        message replays cleanly (same idempotency_key -> run_import no-op/replay).
      actor: the service identity driving the ingest (e.g. 'inbound-worker').
      trace_id: optional correlation id (forwarded for logging only).

    Returns run_import's result dict, augmented with
    ``{"datastream_id", "channel", "message_id"}``.

    Raises:
      InboundIngestValidationError: file_bytes/channel/message_id are missing/blank.
      DatastreamNotIngestable: not found, not managed_feed, disabled, channel not
        allowed, or missing a locked plan/mapping version (unconfigured).
      (propagated) AppendUnavailable, CsvExcelImportError, InvalidImportContract,
      ImportPayloadConflict, ImportInProgress -- run_import's own errors are NOT
      caught here so the caller maps them exactly like the direct-upload route.
    """
    # ------------------------------------------------------------------
    # Input guards (fail closed before any read).
    # ------------------------------------------------------------------
    if not file_bytes:
        raise InboundIngestValidationError("file_bytes is empty")
    channel = (channel or "").strip().lower()
    if not channel:
        raise InboundIngestValidationError("channel is required")
    message_id = (message_id or "").strip()
    if not message_id:
        raise InboundIngestValidationError("message_id is required")

    # ------------------------------------------------------------------
    # Step 1: load the Datastream and assert every unattended precondition.
    # ------------------------------------------------------------------
    ds = _load_ingestable_datastream(
        conn, datastream_id=datastream_id, project_id=project_id
    )
    if ds is None:
        raise DatastreamNotIngestable(
            f"datastream '{datastream_id}' not found in project '{project_id}'"
        )
    if ds["source_kind"] != _SOURCE_KIND_MANAGED_FEED:
        raise DatastreamNotIngestable(
            f"datastream '{datastream_id}' is not a managed_feed "
            f"(source_kind={ds['source_kind']!r}); inbound ingest is not applicable"
        )
    if not ds["enabled"]:
        raise DatastreamNotIngestable(
            f"datastream '{datastream_id}' is disabled; inbound ingest is refused"
        )

    config = ds["config"] if isinstance(ds["config"], dict) else {}
    allowed = config.get("channels") or []
    allowed_set = {str(ch).strip().lower() for ch in allowed}
    if channel not in allowed_set:
        raise DatastreamNotIngestable(
            f"channel '{channel}' is not enabled for datastream '{datastream_id}' "
            f"(allowed: {sorted(allowed_set)})"
        )

    plan_version_id = ds["current_plan_version_id"]
    mapping_version_id = ds["current_mapping_version_id"]
    if not plan_version_id or not mapping_version_id:
        # An unattended import needs a locked plan + mapping; a Datastream that a
        # human never finished configuring cannot self-serve an inbound file.
        raise DatastreamNotIngestable(
            f"datastream '{datastream_id}' is not fully configured "
            f"(plan_version_id={plan_version_id!r}, "
            f"mapping_version_id={mapping_version_id!r}); "
            "lock a plan + mapping before enabling inbound ingest"
        )

    # ------------------------------------------------------------------
    # Step 2: resolve the pipeline inputs the direct-upload path resolves,
    # but from durable server-side state (no operator interaction).
    # ------------------------------------------------------------------
    projection_plan = _fetch_projection_plan(
        conn,
        datastream_id=datastream_id,
        project_id=project_id,
        plan_version_id=plan_version_id,
    )
    if projection_plan is None:
        # No prior execution recorded an executable projection for this plan
        # version -- there is nothing to replay unattended. Fail closed.
        raise DatastreamNotIngestable(
            f"datastream '{datastream_id}' has no executable projection recorded "
            f"for plan version '{plan_version_id}'; run at least one attended "
            "import before enabling inbound ingest"
        )

    # Parsing contract: prefer the most recent active reusable contract; else
    # build a MINIMAL auto-detect contract. We log which path was taken so the
    # fallback is never a silent platform default (per doctrine: no hardcode).
    from core.csv_excel_import import detect_format  # noqa: PLC0415

    contract = _fetch_active_parsing_contract(
        conn, datastream_id=datastream_id, project_id=project_id
    )
    if contract is not None:
        logger.info(
            "inbound_ingest: contract=active ds=%s trace=%s", datastream_id, trace_id
        )
    else:
        # FALLBACK (documented, not a hardcode): no active parsing contract exists
        # for this Datastream. Build the minimal contract run_import needs -- the
        # detected format plus write_mode=replace -- and enrich it with any column
        # typing hints the semantic template pins (kills ID/YYYYMMDD false-rejects
        # without an operator round-trip). detect_format raises UnsupportedFileType
        # (a CsvExcelImportError) which propagates to the caller unchanged.
        detected_format = detect_format(filename, file_bytes)
        contract = {"format": detected_format, "write_mode": _WRITE_MODE_REPLACE}

        template_code = config.get("template_code")
        template_version = config.get("template_version")
        if template_code and template_version is not None:
            import core.import_templates as _templates  # noqa: PLC0415

            template = _templates.get_template(conn, template_code, template_version)
            template_contract = (template or {}).get("contract") or {}
            column_types = _template_column_types(template_contract)
            if column_types:
                contract["column_types"] = column_types

        logger.info(
            "inbound_ingest: contract=fallback_autodetect format=%s "
            "template=%s ds=%s trace=%s",
            contract["format"],
            template_code,
            datastream_id,
            trace_id,
        )

    # ------------------------------------------------------------------
    # Step 3-4: idempotency key + provenance metadata (source-agnostic).
    # Redelivery of the SAME message replays cleanly through run_import.
    # ------------------------------------------------------------------
    idempotency_key = f"inbound:{channel}:{datastream_id}:{message_id}"
    source_metadata = {
        "ingress_channel": channel,
        "message_id": message_id,
        "filename": filename,
    }

    # Read the empty-publication preference exactly like the direct path so both
    # paths apply the same policy. force_empty_publish stays False: an unattended
    # inbound file never carries the operator's explicit empty-publish override.
    preferences = {
        "allow_empty_publication": _read_allow_empty_publication(conn, project_id)
    }

    # ------------------------------------------------------------------
    # Step 5: drive the SAME pipeline as a direct upload. Do NOT commit --
    # the caller owns the transaction (mirrors run_import). run_import's own
    # typed exceptions propagate unchanged.
    # ------------------------------------------------------------------
    from core.csv_excel_import import run_import  # noqa: PLC0415

    result = run_import(
        file_bytes,
        datastream_id=datastream_id,
        project_id=project_id,
        plan_version_id=plan_version_id,
        mapping_version_id=mapping_version_id,
        projection_plan=projection_plan,
        actor=actor,
        idempotency_key=idempotency_key,
        source_metadata=source_metadata,
        contract=contract,
        conn=conn,
        write_mode=_WRITE_MODE_REPLACE,
        force_empty_publish=False,
        preferences=preferences,
    )

    # ------------------------------------------------------------------
    # Step 6: augment the run result with the ingress provenance keys.
    # ------------------------------------------------------------------
    return {
        **result,
        "datastream_id": datastream_id,
        "channel": channel,
        "message_id": message_id,
    }
