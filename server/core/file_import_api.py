"""toorow -- the file-import surface: upload, contract, recurring sync.

Eight routes under `/api/datastreams/{id}`, in three stories:

  * 12.9 -- CSV/Excel governed import: preview, confirm, and the versioned
    parsing contract (`PUT`/list/get under `/import-contracts`);
  * 12.10 -- the recurring managed-feed sync schedule: configure, sync-now,
    status.

Extracted from `admin_api.py` under AD-40 --
`docs/product-architecture/module-boundaries.md` -- with the handler bodies
unchanged and in the order they were declared. The parsing itself is not here
and never was: it lives behind `core.csv_excel_import` (AD-41).

UPLOAD TRANSPORT. The upload bytes ride in a base64 `file_base64` JSON field
rather than a multipart form, because `server/core` has no multipart helper and
`mediaplan_api` documents the same gap and made the same choice. The parse and
ledger code is byte-oriented, so the transport is orthogonal.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.managed_feed_imports_api import _managed_feed_error_response

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The two seams this surface shares with the rest of the console.
# ---------------------------------------------------------------------------
#
# Reached at CALL time, never at import time, and that is not a style choice.
# `admin_api` imports the route collection below, so a module-level import here
# would close a cycle. It is also what keeps `patch("core.admin_api._check_auth")`
# working: a test that replaces the name on `admin_api` must still be the object
# these handlers call. The 56 sibling `*_api.py` modules do it this way for the
# same two reasons.


async def _check_auth(request: Request) -> tuple[bool, str]:
    """The one authentication seam of the console."""
    from core.admin_api import _check_auth as _impl  # noqa: PLC0415

    return await _impl(request)


def _require_datastream_role(
    project_id: str,
    identity: str,
    minimum_role: str,
    conn,
    *,
    datastream_id: str | None = None,
    pair_proven_by_read: bool = False,
) -> "Response | None":
    """Strict Viewer/Member/Owner access for Datastream surfaces (see AI-219)."""
    from core.admin_api import _require_datastream_role as _impl  # noqa: PLC0415

    return _impl(
        project_id,
        identity,
        minimum_role,
        conn,
        datastream_id=datastream_id,
        pair_proven_by_read=pair_proven_by_read,
    )


# ===========================================================================
# Story 12.9: CSV / Excel governed import.
#
# preview (Member), confirm-import (Member; Owner when force_empty_publish),
# version-contract PUT (Member), list/get import-contracts (Viewer). All reuse
# _require_datastream_role; cross-project returns a non-disclosing 404 + audit.
#
# UPLOAD TRANSPORT NOTE: the story's INTEGRATION SPEC sketches a multipart form,
# but server/core has no multipart/upload helper (mediaplan_api documents the same
# gap and uses a base64 JSON body). To match the file's convention EXACTLY and add
# no new dependency, the upload bytes ride in a base64 ``file_base64`` JSON field.
# The parse/ledger code is byte-oriented, so the transport is orthogonal. The
# short-lived upload-slot mechanism (Route 2 in the SPEC) stays Phase B; for now
# the confirm route re-sends the same base64 bytes.
# ===========================================================================


def _decode_upload_bytes(body: dict) -> tuple[bytes | None, Response | None]:
    """Decode the base64 ``file_base64`` upload field, capped BEFORE materialising.

    Returns ``(data, None)`` on success or ``(None, error_response)`` on a malformed
    / oversized / missing field. The size cap mirrors csv_excel_import.MAX_FILE_BYTES
    (25 MiB -- the inbound scan's bound, so the worker never rejects what the route
    admitted) and is enforced on the base64 string first (never build a bomb buffer),
    then re-checked on the decoded bytes.
    """
    import base64  # noqa: PLC0415

    from core.csv_excel_import import MAX_FILE_BYTES  # noqa: PLC0415

    too_large = JSONResponse(
        {
            "code": "file_too_large",
            "message": (
                f"This file is over the {MAX_FILE_BYTES // (1024 * 1024)} MB limit. "
                "Split it into smaller files and import them one after another."
            ),
        },
        422,
    )
    raw = body.get("file_base64")
    if not isinstance(raw, str) or not raw.strip():
        return None, JSONResponse(
            {"code": "missing_field", "message": "file_base64 est requis"}, 422
        )
    # Cap the base64 length first (base64 is ~4/3 the decoded size).
    if len(raw) > MAX_FILE_BYTES // 3 * 4 + 8:
        return None, too_large

    try:
        data = base64.b64decode(raw, validate=True)
    except Exception:
        return None, JSONResponse(
            {
                "code": "invalid_body",
                "message": (
                    "The upload field is not valid base64. Re-encode the file "
                    "and send it again."
                ),
            },
            422,
        )
    if len(data) > MAX_FILE_BYTES:
        return None, too_large
    return data, None


def _csv_excel_error_response(exc) -> Response | None:
    """Map a CsvExcelImportError to its stable ``.code`` -> HTTP (all 422), or None.

    Every parse / contract failure in Story 12.9 is a 422 (client-side data or
    configuration error): unsupported_file_type, empty_file, file_too_large,
    encoding_error, duplicate_columns, no_header_row, formula_in_cells,
    invalid_import_contract, append_unavailable, and the generic base by ``.code``.
    Returns None for an unrecognised type so the caller falls through to an opaque 503.
    """
    from core.csv_excel_import import CsvExcelImportError  # noqa: PLC0415

    if isinstance(exc, CsvExcelImportError):
        return JSONResponse(
            {"code": getattr(exc, "code", "invalid_import"), "message": str(exc)}, 422
        )
    return None


async def _preview_csv_excel_import(request: Request) -> Response:
    """POST /api/datastreams/{id}/imports/preview (Member) -- Story 12.9.

    Body: {project_id, file_base64, filename?, contract?}. Builds a bounded preview
    WITHOUT publishing or opening a ledger row. 200 with the preview dict (format,
    encoding, delimiter, sheet_name, columns, row_count, rejected_count,
    preview_rows, content_hash). Parse / contract errors -> 422 (stable code).

    The preview runs the SAME inbound scan (Story 38.10) as the landing path
    before any parser sees the bytes: a refusal is a 422 carrying the scan's
    stable reason code, and ``build_preview`` is never reached.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_field", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    data, dec_error = _decode_upload_bytes(body)
    if dec_error is not None:
        return dec_error
    contract = body.get("contract")
    filename = body.get("filename")
    try:
        from dataclasses import asdict  # noqa: PLC0415

        from core.csv_excel_import import build_preview  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415
        from core.inbound_scan import scan_bytes  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "member", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            # The same gate the landing path runs (Story 38.10): no parser --
            # preview included -- ever sees bytes the scan refused. The scan is
            # pure (no DB, no write); its reason is a stable refusal code.
            verdict = scan_bytes(data)
            if not verdict.accepted:
                return JSONResponse(
                    {
                        "code": verdict.reason,
                        "message": (
                            "This file was refused by the inbound safety scan "
                            f"({verdict.reason})."
                        ),
                    },
                    422,
                )
            try:
                preview = build_preview(data, filename=filename, contract=contract)
            except Exception as exc:  # noqa: BLE001 - mapped to a stable 422 below.
                mapped = _csv_excel_error_response(exc)
                if mapped is not None:
                    return mapped
                raise
            # Story 22.19: when the Datastream carries an `fst_` template binding,
            # the preview also carries what the onboarding review screen shows --
            # per-column recognition + confidence, the declared matrix placement,
            # and THE SAME landing gate the import runs. Before this, the gate had
            # a single caller (`run_import`), so a person could only discover an
            # unmapped required field by attempting the import.
            #
            # Best-effort by construction: a Datastream with no template gets
            # `None` and the CSV/Excel preview is byte-for-byte what it was. A
            # failure here degrades the extra panel, never the preview itself --
            # the ordinary import path predates this seam and must not start
            # failing because of it.
            file_source_preview = None
            try:
                from core.csv_excel_import import (  # noqa: PLC0415
                    build_file_source_preview,
                )

                file_source_preview = build_file_source_preview(
                    conn,
                    data,
                    project_id=project_id,
                    datastream_id=ds_id,
                    mapping_version_id=body.get("mapping_version_id"),
                    filename=filename,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("admin_api: file_source_preview_unavailable: %s", exc)
    except Exception as exc:
        logger.error("admin_api: preview_csv_excel_import_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Preview unavailable"}, 503)
    payload = asdict(preview)
    payload["file_source"] = file_source_preview
    return JSONResponse(payload, 200)


async def _confirm_csv_excel_import(request: Request) -> Response:
    """POST /api/datastreams/{id}/imports through the governed Upload ingress.

    Client-supplied plan, mapping, projection, parser and provenance documents are
    rejected: the server resolves one coherent immutable bundle after
    authorization. Upload then follows receipt -> quarantine -> malware scan ->
    raw import -> Universal Datastream dispatch, exactly like Email/Webhook.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token required"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception:
        return JSONResponse({"code": "invalid_body", "message": "Invalid JSON body"}, 400)

    if not isinstance(body, dict):
        return JSONResponse(
            {"code": "invalid_body", "message": "JSON body must be an object"},
            400,
        )
    project_id = str(body.get("project_id") or "").strip()
    idempotency_key = str(
        body.get("idempotency_key")
        or request.headers.get("Idempotency-Key")
        or ""
    ).strip()
    if not project_id or not idempotency_key:
        return JSONResponse(
            {
                "code": "missing_field",
                "message": "project_id and idempotency_key are required",
            },
            422,
        )
    forbidden_authority = sorted(
        key
        for key in {
            "plan_version_id",
            "mapping_version_id",
            "projection_plan",
            "source_metadata",
            "contract",
            "import_contract_id",
            "raw_schema",
        }
        if key in body
    )
    if forbidden_authority:
        return JSONResponse(
            {
                "code": "server_resolved_dispatch_contract",
                "message": (
                    "Plan, mapping, parser and provenance are resolved by the server."
                ),
                "rejected_fields": forbidden_authority,
            },
            422,
        )
    if body.get("force_empty_publish"):
        return JSONResponse(
            {
                "code": "empty_upload_not_supported",
                "message": "An uploaded file must contain governed candidate rows.",
            },
            422,
        )

    ds_id = request.path_params.get("id", "")
    data, decode_error = _decode_upload_bytes(body)
    if decode_error is not None:
        return decode_error

    try:
        from core.datastream_publication import PublicationError  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415
        from core.inbound_ingest import DatastreamNotIngestable  # noqa: PLC0415
        from core.inbound_processing import (  # noqa: PLC0415
            InboundProcessingValidationError,
            process_authorized_upload,
        )
        from core.managed_feed_ledger import ManagedFeedError  # noqa: PLC0415
        from core.managed_file_dispatch import (  # noqa: PLC0415
            ManagedFileDispatchError,
        )

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id,
                identity,
                "member",
                conn,
                datastream_id=ds_id,
            )
            if role_error is not None:
                return role_error
            try:
                upload = process_authorized_upload(
                    conn,
                    datastream_id=ds_id,
                    project_id=project_id,
                    file_bytes=data,
                    filename=body.get("filename"),
                    media_type=body.get("media_type"),
                    actor=identity or "anonymous",
                    idempotency_key=idempotency_key,
                    trace_id=request.headers.get("traceparent"),
                )
                conn.commit()
            except InboundProcessingValidationError as exc:
                conn.rollback()
                code = getattr(exc, "code", "upload_dispatch_rejected")
                return JSONResponse({"code": code, "message": str(exc)}, 422)
            except DatastreamNotIngestable as exc:
                # A DATASTREAM THAT CANNOT INGEST IS A NAMED REFUSAL, not a 503
                # (AI-321, 2026-08-29): the resolver raises it from inside the
                # scan, past the typed handling of `_process_one_attachment`,
                # and the catch-all below rendered "Import unavailable" for a
                # rule that can say what to configure.
                conn.rollback()
                return JSONResponse(
                    {"code": "datastream_not_ingestable", "message": str(exc)}, 422
                )
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                mapped = _csv_excel_error_response(exc)
                if mapped is None and isinstance(exc, ManagedFeedError):
                    mapped = _managed_feed_error_response(exc)
                if mapped is not None:
                    return mapped
                if isinstance(exc, ManagedFeedError):
                    return JSONResponse(
                        {"code": exc.code, "message": "Upload dispatch rejected"},
                        422,
                    )
                if isinstance(exc, (ManagedFileDispatchError, PublicationError)):
                    return JSONResponse(
                        {
                            "code": exc.code,
                            "message": "Upload dispatch requires reconciliation",
                        },
                        409,
                    )
                raise
    except Exception as exc:  # noqa: BLE001
        # THE TRACE, OR THE 503 IS A WALL (AI-321, 2026-08-29): this line logged
        # the exception TYPE alone -- `ProgrammingError` -- and the first import
        # on a Datastream the wizard had just brought to ACTIVE died behind it
        # with nothing in the journal to say which statement. The body rendered
        # to the client does not change; the server-side trace is what was
        # missing (the rule `datastream_workbench_api._error` already follows).
        logger.error(
            "admin_api: governed_upload_dispatch_error: %s", type(exc).__name__, exc_info=exc
        )
        return JSONResponse(
            {"code": "upload_dispatch_unavailable", "message": "Import unavailable"},
            503,
        )

    dispatch = upload.get("dispatch_result")
    payload = dict(dispatch) if isinstance(dispatch, dict) else {}
    payload.update(
        {
            "receipt_id": upload.get("receipt_id"),
            "raw_import_id": (
                upload.get("attachments", [{}])[0].get("raw_import_id")
                if upload.get("attachments")
                else None
            ),
            "ingress_channel": "upload",
            "status": upload.get("status"),
            "error_code": (
                upload.get("attachments", [{}])[0].get("error_code")
                if upload.get("attachments")
                else None
            ),
            # AI-321 (2026-08-29): the SENTENCE beside the code -- "required source
            # columns disappeared: datastream_id" -- was persisted on the raw import
            # and never returned, so the person who uploaded read a bare code.
            "error_detail": (
                upload.get("attachments", [{}])[0].get("error_detail")
                if upload.get("attachments")
                else None
            ),
            # A replay says so: the second identical upload executes nothing and
            # answers the receipt's terminal state (`process_authorized_upload`).
            "replayed": bool(upload.get("replayed", False)),
        }
    )
    return JSONResponse(payload, 200)


async def _put_import_contract(request: Request) -> Response:
    """PUT /api/datastreams/{id}/import-contracts (Member) -- Story 12.9.

    Body: {project_id, contract, label?}. Versions (or de-duplicates) the parsing
    contract via version_contract; returns the cic_<ULID> id (existing on a matching
    fingerprint). 200 {import_contract_id}. invalid_import_contract / append_unavailable
    -> 422.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_field", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    contract = body.get("contract")
    if not isinstance(contract, dict):
        return JSONResponse({"code": "missing_field", "message": "contract est requis"}, 422)
    label = body.get("label")
    try:
        from core.csv_excel_import import version_contract  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "member", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            try:
                contract_id = version_contract(
                    contract,
                    datastream_id=ds_id,
                    project_id=project_id,
                    actor=identity or "anonymous",
                    conn=conn,
                    label=label,
                )
                conn.commit()
            except Exception as exc:  # noqa: BLE001 - mapped to a stable 422 below.
                conn.rollback()
                mapped = _csv_excel_error_response(exc)
                if mapped is not None:
                    return mapped
                raise
    except Exception as exc:
        logger.error("admin_api: put_import_contract_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Contrat indisponible"}, 503)
    return JSONResponse({"import_contract_id": contract_id}, 200)


async def _list_import_contracts(request: Request) -> Response:
    """GET /api/datastreams/{id}/import-contracts?project_id=<id> (Viewer)."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_param", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, datastream_id, project_id, fingerprint, format,
                           write_mode, contract, label, is_active, created_by,
                           created_at
                    FROM app.csv_excel_import_contracts
                    WHERE datastream_id = %s AND project_id = %s
                    ORDER BY created_at DESC
                    """,
                    (ds_id, project_id),
                )
                cols = [d[0] for d in cur.description]
                contracts = [_import_contract_row_to_dict(cols, row) for row in cur.fetchall()]
    except Exception as exc:
        logger.error("admin_api: list_import_contracts_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Contrats indisponibles"}, 503)
    return JSONResponse({"contracts": contracts})


async def _get_import_contract(request: Request) -> Response:
    """GET /api/datastreams/{id}/import-contracts/{contract_id}?project_id=<id> (Viewer)."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_param", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    contract_id = request.path_params.get("contract_id", "")
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, datastream_id, project_id, fingerprint, format,
                           write_mode, contract, label, is_active, created_by,
                           created_at
                    FROM app.csv_excel_import_contracts
                    WHERE id = %s AND datastream_id = %s AND project_id = %s
                    """,
                    (contract_id, ds_id, project_id),
                )
                row = cur.fetchone()
                if row is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "Contrat introuvable"}, 404
                    )
                cols = [d[0] for d in cur.description]
                contract = _import_contract_row_to_dict(cols, row)
    except Exception as exc:
        logger.error("admin_api: get_import_contract_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Contrat indisponible"}, 503)
    return JSONResponse(contract, 200)


def _import_contract_row_to_dict(cols: list[str], row: tuple) -> dict:
    """Serialise an app.csv_excel_import_contracts row (ISO-8601 for timestamps)."""
    record: dict = {}
    for col, val in zip(cols, row):
        if col == "created_at" and val is not None and hasattr(val, "isoformat"):
            record[col] = val.isoformat()
        else:
            record[col] = val
    return record


# ===========================================================================
# Story 12.10: Google Sheets recurring sync (managed-feed sync schedule).
#
# configure (Member; upsert schedule), sync-now (Member; manual run), status
# (Viewer; schedule + last runs + next run). All reuse _require_datastream_role;
# cross-project returns a non-disclosing 404 + audit.
#
# PHASE_B_LIVE_BLOCKED: the production 15.6 sheets_adapter (live Google OAuth) is
# not available in this environment. sync-now injects None, so run_sync raises
# NotImplementedError -> mapped to a 503 with the PHASE_B_LIVE_BLOCKED marker. The
# one-line adapter injection is the documented Phase-B wiring (Open Questions #1).
# ===========================================================================


def _sync_schedule_row_to_dict(cols: list[str], row: tuple) -> dict:
    """Serialise an app.managed_feed_sync_schedule row (ISO-8601 for timestamps)."""
    record: dict = {}
    for col, val in zip(cols, row):
        if val is not None and hasattr(val, "isoformat"):
            record[col] = val.isoformat()
        else:
            record[col] = val
    return record


def _fetch_sync_schedule(conn, datastream_id: str, project_id: str) -> dict | None:
    """Read the sync schedule config for a (datastream, project) scope, or None."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT datastream_id, project_id, connection_id, spreadsheet_id,
                   sheet_range, sheet_name, column_mapping, cadence_mode,
                   cadence_policy, quota_profile, last_sync_at, last_ledger_id,
                   last_watermark, enabled, created_by, created_at, updated_at
            FROM app.managed_feed_sync_schedule
            WHERE datastream_id = %s AND project_id = %s
            """,
            (datastream_id, project_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return _sync_schedule_row_to_dict(cols, row)


async def _configure_managed_feed_sync(request: Request) -> Response:
    """POST /api/datastreams/{id}/managed-feed/configure (Member) -- Story 12.10.

    Upsert the sync schedule (spreadsheet, range, column_mapping, cadence,
    quota_profile). Validates the cadence via validate_cadence BEFORE the DB write
    (hourly without allow_hourly -> 422). 201 with the upserted schedule row.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_field", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    connection_id = str(body.get("connection_id") or "").strip()
    spreadsheet_id = str(body.get("spreadsheet_id") or "").strip()
    sheet_range = str(body.get("sheet_range") or "").strip()
    cadence_mode = str(body.get("cadence_mode") or "manual").strip()
    if not connection_id or not spreadsheet_id or not sheet_range:
        return JSONResponse(
            {
                "code": "missing_field",
                "message": "connection_id, spreadsheet_id et sheet_range sont requis",
            },
            422,
        )
    quota_profile = body.get("quota_profile")
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.google_sheets_sync import validate_cadence  # noqa: PLC0415

        # Validate the cadence BEFORE any DB write (fail closed).
        cadence_errors = validate_cadence(cadence_mode, quota_profile)
        if cadence_errors:
            return JSONResponse(
                {"code": "quota_hourly_not_permitted", "issues": cadence_errors}, 422
            )

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "member", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.managed_feed_sync_schedule
                        (datastream_id, project_id, connection_id, spreadsheet_id,
                         sheet_range, sheet_name, column_mapping, cadence_mode,
                         cadence_policy, quota_profile, enabled, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb,
                            %s::jsonb, %s, %s)
                    ON CONFLICT (datastream_id, project_id) DO UPDATE SET
                        connection_id = EXCLUDED.connection_id,
                        spreadsheet_id = EXCLUDED.spreadsheet_id,
                        sheet_range = EXCLUDED.sheet_range,
                        sheet_name = EXCLUDED.sheet_name,
                        column_mapping = EXCLUDED.column_mapping,
                        cadence_mode = EXCLUDED.cadence_mode,
                        cadence_policy = EXCLUDED.cadence_policy,
                        quota_profile = EXCLUDED.quota_profile,
                        enabled = EXCLUDED.enabled,
                        updated_at = NOW()
                    RETURNING datastream_id, project_id, connection_id, spreadsheet_id,
                              sheet_range, sheet_name, column_mapping, cadence_mode,
                              cadence_policy, quota_profile, last_sync_at, last_ledger_id,
                              last_watermark, enabled, created_by, created_at, updated_at
                    """,
                    (
                        ds_id,
                        project_id,
                        connection_id,
                        spreadsheet_id,
                        sheet_range,
                        body.get("sheet_name") or "",
                        json.dumps(body.get("column_mapping") or {}),
                        cadence_mode,
                        json.dumps(body.get("cadence_policy") or {}),
                        json.dumps(quota_profile or {}),
                        bool(body.get("enabled", cadence_mode != "manual")),
                        identity or "anonymous",
                    ),
                )
                cols = [d[0] for d in cur.description]
                schedule = _sync_schedule_row_to_dict(cols, cur.fetchone())
                conn.commit()
    except Exception as exc:
        logger.error("admin_api: configure_managed_feed_sync_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Configuration indisponible"}, 503)
    return JSONResponse(schedule, 201)


async def _sync_now_managed_feed(request: Request) -> Response:
    """POST /api/datastreams/{id}/managed-feed/sync-now (Member) -- Story 12.10.

    Trigger an immediate manual sync run through run_sync. Body: {project_id,
    run_id?}. 200 with the sync_result dict (may report outcome=failed for a
    safe-fail; the HTTP status stays 200 because the RUN succeeded). QuotaViolation /
    SheetsSyncError -> 422; ImportPayloadConflict / ImportInProgress -> 409.

    The Sheets adapter is INJECTED here through the inbound capability seam, the
    same way the scheduler dispatch hook does it -- this route used to pass
    `sheets_adapter=None` with a PHASE_B_LIVE_BLOCKED note, which made every
    sync-now raise NotImplementedError before reading a cell (mapped to a 503
    below; the handler stays as a defensive fallback, but it is no longer the
    expected path).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_field", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    run_id = str(body.get("run_id") or "").strip() or None
    force_empty_publish = bool(body.get("force_empty_publish", False))
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.google_sheets_sync import (  # noqa: PLC0415
            QuotaViolation,
            SheetsSyncError,
            run_sync,
        )
        from core.managed_feed_ledger import (  # noqa: PLC0415
            ImportInProgress,
            ImportPayloadConflict,
        )

        with get_connection() as conn:
            # Member floor; Owner floor when forcing an empty publish (mirrors the
            # publish route's owner-on-force pattern).
            minimum_role = "owner" if force_empty_publish else "member"
            role_error = _require_datastream_role(
                project_id, identity, minimum_role, conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            schedule = _fetch_sync_schedule(conn, ds_id, project_id)
            if schedule is None:
                # Non-disclosing: no schedule configured for this scope.
                return JSONResponse(
                    {"code": "not_found", "message": "Configuration de sync introuvable"}, 404
                )
            try:
                # The adapter crosses the source-agnostic inbound seam by
                # capability name (AD-2) -- the same resolution the scheduler's
                # dispatch hook uses. Was `None` with a "PHASE_B_LIVE_BLOCKED:
                # production injects the 15.6 adapter" note, which made every
                # sync-now raise NotImplementedError before reading a cell.
                from core.inbound_seam import resolve_inbound  # noqa: PLC0415

                result = run_sync(
                    datastream_id=ds_id,
                    project_id=project_id,
                    connection_id=schedule["connection_id"],
                    spreadsheet_id=schedule["spreadsheet_id"],
                    sheet_range=schedule["sheet_range"],
                    sheet_name=schedule.get("sheet_name") or "",
                    column_mapping=schedule.get("column_mapping") or {},
                    plan_version_id=str(body.get("plan_version_id") or "").strip(),
                    mapping_version_id=str(body.get("mapping_version_id") or "").strip(),
                    projection_plan=body.get("projection_plan") or {},
                    actor=identity or "anonymous",
                    cadence_mode=schedule.get("cadence_mode") or "manual",
                    quota_profile=schedule.get("quota_profile"),
                    run_id=run_id,
                    conn=conn,
                    sheets_adapter=resolve_inbound("managed_feed_values_adapter_factory")(),
                    force_empty_publish=force_empty_publish,
                )
                conn.commit()
            except QuotaViolation as exc:
                conn.rollback()
                return JSONResponse({"code": exc.code, "message": exc.detail}, 422)
            except (ImportPayloadConflict, ImportInProgress) as exc:
                conn.rollback()
                mapped = _managed_feed_error_response(exc)
                if mapped is not None:
                    return mapped
                raise
            except SheetsSyncError as exc:
                conn.rollback()
                return JSONResponse({"code": exc.code, "message": exc.detail}, 422)
            except NotImplementedError as exc:
                conn.rollback()
                # Defensive: run_sync still refuses a None adapter outright.
                return JSONResponse({"code": "phase_b_live_blocked", "message": str(exc)}, 503)
    except Exception as exc:
        logger.error("admin_api: sync_now_managed_feed_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Sync indisponible"}, 503)
    return JSONResponse(result, 200)


async def _status_managed_feed_sync(request: Request) -> Response:
    """GET /api/datastreams/{id}/managed-feed/status?project_id=<id> (Viewer).

    Story 12.10. Returns the sync schedule config + the last N ledger rows + the
    next-run description. 200 {schedule, last_runs, next_run}.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_param", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    try:
        from core.datastream_schedule import calculate_schedule_window  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415
        from core.google_sheets_sync import describe_next_run  # noqa: PLC0415
        from core.managed_feed_ledger import list_ledger  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            schedule = _fetch_sync_schedule(conn, ds_id, project_id)
            if schedule is None:
                return JSONResponse(
                    {"code": "not_found", "message": "Configuration de sync introuvable"}, 404
                )
            last_runs = list_ledger(ds_id, project_id, conn, limit=10)
            next_run: dict = {}
            cadence_policy = schedule.get("cadence_policy") or {}
            try:
                window = calculate_schedule_window(
                    cadence_policy,
                    now_utc=datetime.now(tz=timezone.utc),
                    last_committed_watermark=schedule.get("last_watermark"),
                )
                next_run = describe_next_run(window)
            except Exception:  # noqa: BLE001 - a manual / unschedulable config -> nulls.
                next_run = describe_next_run(None)
    except Exception as exc:
        logger.error("admin_api: status_managed_feed_sync_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Statut indisponible"}, 503)
    return JSONResponse({"schedule": schedule, "last_runs": last_runs, "next_run": next_run})


# ---------------------------------------------------------------------------
# The collection, in the order `admin_api` declared it.
# ---------------------------------------------------------------------------
#
# Starlette resolves in declaration order, so this order IS the contract. The
# 12.10 sync routes came FIRST here and keep that position; then
# `/imports/preview` before `/imports`, and `/import-contracts/{contract_id}`
# before `/import-contracts`. All of them precede the `/{id}` catch-alls that
# stay in `admin_api`.

FILE_IMPORT_ROUTES = [
    Route(
        "/api/datastreams/{id}/managed-feed/configure",
        endpoint=_configure_managed_feed_sync,
        methods=["POST"],
    ),
    Route(
        "/api/datastreams/{id}/managed-feed/sync-now",
        endpoint=_sync_now_managed_feed,
        methods=["POST"],
    ),
    Route(
        "/api/datastreams/{id}/managed-feed/status",
        endpoint=_status_managed_feed_sync,
        methods=["GET"],
    ),
    Route(
        "/api/datastreams/{id}/imports/preview",
        endpoint=_preview_csv_excel_import,
        methods=["POST"],
    ),
    Route(
        "/api/datastreams/{id}/imports",
        endpoint=_confirm_csv_excel_import,
        methods=["POST"],
    ),
    Route(
        "/api/datastreams/{id}/import-contracts/{contract_id}",
        endpoint=_get_import_contract,
        methods=["GET"],
    ),
    Route(
        "/api/datastreams/{id}/import-contracts",
        endpoint=_put_import_contract,
        methods=["PUT"],
    ),
    Route(
        "/api/datastreams/{id}/import-contracts",
        endpoint=_list_import_contracts,
        methods=["GET"],
    ),
]
