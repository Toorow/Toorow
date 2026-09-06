"""The door a file-source template never had (Story 22.11 / 22.19 / 22.23).

WHY THIS MODULE EXISTS
Measured 2026-08-01 (AI-123): `create_file_source_template` had exactly ONE
caller -- `lock_adaptation_template` -- and that one had NONE. No HTTP route, no
MCP tool. A template could therefore only come into existence by writing to the
database directly, which is precisely how the single row in
`app.file_source_templates` in production got there: a test artifact nobody can
delete (AI-89).

Three acceptance criteria depended on that missing door, and none of them could
close: 22.11 (create and version a template), 22.23 (lock an adaptation as a
versioned template through the gate), and the second half of 22.19 -- "the
operator can resolve a flagged field and confirm, WHICH LOCKS THE TEMPLATE".

WHAT THIS MODULE DOES NOT DO
It adds no validation, no normalisation and no versioning rule of its own. All of
that already lives in `core.file_source_template`, which validates the contract,
asserts every declared field is an active canonical field in scope, computes the
content hash, and writes through `operations.execute_operation` (immutable,
audited, idempotent). A second copy of any of that here is the "second scorer"
mistake this epic has already made twice -- once in the adaptation re-analysis
(22.20) and once in the onboarding gate. The route resolves identity and scope,
and hands over.

IDEMPOTENT BY CONSTRUCTION, so a double-click cannot mint a second version: an
identical contract for the same (project, template_code) replays the existing
version rather than creating a row. That is the store's behaviour, not this
module's -- it is stated here because it is the reason no extra guard is needed.

SCOPE IS FAIL-CLOSED. Unknown project, foreign project and insufficient role
share one 404 envelope: probing this endpoint reveals nothing about what exists
in another project (AD-5).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
from copy import deepcopy
from datetime import UTC, datetime

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.file_source_template import (
    FileSourceTemplateValidationError,
    create_file_source_template,
    get_file_source_template,
    list_canonical_fields,
    list_file_source_template_versions,
)

logger = logging.getLogger(__name__)

_BASE = "/api/projects/{project_id}/file-source-templates"

#: One envelope for foreign, denied and absent (AD-5).
_NOT_FOUND = {"code": "not_found", "message": "Not found"}


async def _authorize(request: Request, role: str):
    """Return (identity, org_id, conn_factory) or a Response, denial first."""
    from core.admin_api import _check_auth, _require_datastream_role  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Authentication required"}, 401)
    project_id = request.path_params["project_id"]
    with get_connection() as conn:
        denied = _require_datastream_role(project_id, identity, role, conn)
        if denied is not None:
            return denied
        with conn.cursor() as cur:
            cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
            row = cur.fetchone()
    if row is None:
        return JSONResponse(_NOT_FOUND, 404)
    return str(identity), str(row[0])


async def _json_body(request: Request) -> dict:
    raw = await request.body()
    if not raw.strip():
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("request body must be an object")
    return value


async def _create_template(request: Request) -> Response:
    """POST {base} -- create (or replay) one immutable template version.

    Member, not viewer: this mints a governed artifact that later imports replay.

    A rejected contract answers 422 with the store's own message. That message
    names the offending key -- 'class' must be one of [...], 'placement.metric'
    must be a non-empty string -- because the operator who authored the template
    is the person who can fix it, and a generic "invalid template" would send
    them back to guessing.
    """
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]

    try:
        body = await _json_body(request)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)

    template_code = str(body.get("template_code") or "").strip()
    contract = body.get("contract")
    if not template_code or not isinstance(contract, dict):
        return JSONResponse(
            {
                "code": "missing_field",
                "message": "template_code and contract are required",
            },
            400,
        )
    if contract.get("kind") == "adaptation":
        return JSONResponse(
            {
                "code": "gate_confirmation_required",
                "message": "Adaptation templates must use the self-test and human-confirm route.",
            },
            422,
        )


    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            created = create_file_source_template(
                conn,
                project_id=project_id,
                org_id=org_id,
                template_code=template_code,
                contract=contract,
                created_by=identity,
                label=body.get("label"),
                idempotency_key=request.headers.get("Idempotency-Key"),
            )
            # The store never commits: it documents that the caller owns the
            # transaction, so a route that forgot this would validate, audit and
            # roll back -- silently.
            conn.commit()
    except FileSourceTemplateValidationError as exc:
        return JSONResponse({"code": "invalid_contract", "message": str(exc)}, 422)
    except Exception as exc:  # noqa: BLE001
        logger.error("file_source_template_api: create_failed project=%s: %s", project_id, exc)
        return JSONResponse(
            {"code": "unavailable", "message": "Template could not be created"}, 503
        )
    return JSONResponse(created, 201)


async def _list_versions(request: Request) -> Response:
    """GET {base}?template_code=... -- every version of one template code.

    Versions are immutable, so this is the audit trail: what was locked, when,
    and by whom. Without `template_code` it answers 400 rather than listing a
    project's whole template estate through a filter that was forgotten.
    """
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    project_id = request.path_params["project_id"]
    template_code = str(request.query_params.get("template_code") or "").strip()
    if not template_code:
        return JSONResponse(
            {"code": "missing_field", "message": "template_code is required"}, 400
        )


    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            versions = list_file_source_template_versions(
                conn, project_id=project_id, template_code=template_code
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("file_source_template_api: list_failed project=%s: %s", project_id, exc)
        return JSONResponse(
            {"code": "unavailable", "message": "Templates could not be read"}, 503
        )
    return JSONResponse({"versions": versions}, 200)


async def _get_template(request: Request) -> Response:
    """GET {base}/{template_code}/versions/{version} -- one exact version.

    Addressed by (code, version) rather than by `fst_` id because that is the
    store's own key: `get_file_source_template` takes exactly those two. Adding
    an id-based reader here to make the URL prettier would put a second lookup
    path on an immutable artifact, which is how two readers start to disagree.
    """
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    project_id = request.path_params["project_id"]
    template_code = request.path_params["template_code"]
    try:
        version = int(request.path_params["version"])
    except (TypeError, ValueError):
        return JSONResponse(
            {"code": "invalid_version", "message": "version must be an integer"}, 400
        )


    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            template = get_file_source_template(
                conn, project_id=project_id, template_code=template_code, version=version
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("file_source_template_api: get_failed project=%s: %s", project_id, exc)
        return JSONResponse(
            {"code": "unavailable", "message": "Template could not be read"}, 503
        )
    if template is None:
        # Absent and out-of-scope answer alike: the store already filters on
        # project_id, so a foreign id is indistinguishable from a missing one.
        return JSONResponse(_NOT_FOUND, 404)
    return JSONResponse(template, 200)


def _resolution_vocabulary(conn, *, project_id: str, template: object) -> set[str]:
    """The targets a human may resolve a column onto: the project's canonical
    fields AND the fields the bound Template declares.

    AI-321 (2026-08-29): the vocabulary was the MDM canonical fields alone, and
    none of a catalog Template's nine required fields is one -- so the preview
    proposed `datastream_id -> datastream_id` as `matched` and this door refused
    the very resolution it had proposed (`invalid resolution for datastream_id`)
    on every catalog-bound Datastream. A catalog that offers a field the
    validator then refuses is the bug `list_canonical_fields` names in its own
    docstring, in the other direction.
    """
    vocabulary = {field["id"] for field in list_canonical_fields(conn, project_id=project_id)}
    contract = (template or {}).get("contract") if isinstance(template, dict) else None
    contract = contract if isinstance(contract, dict) else {}
    for key in ("required_fields", "optional_fields"):
        vocabulary.update(str(f) for f in (contract.get(key) or []) if f)
    return vocabulary


async def _confirm_adaptation(request: Request) -> Response:
    """Self-test, gate and human-lock one adaptation Template atomically."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    try:
        body = await _json_body(request)
        encoded = str(body.get("file_base64") or "")
        required = (
            "file_base64",
            "template_code",
            "py_source",
            "placement_class",
            "placement",
            "required_fields",
            "grain",
            "datastream_id",
            "mapping_version_id",
        )
        missing = [name for name in required if not body.get(name)]
        if missing:
            raise ValueError("missing required fields: " + ", ".join(missing))
        from core.csv_excel_import import MAX_FILE_BYTES  # noqa: PLC0415

        if len(encoded) > 4 * ((MAX_FILE_BYTES + 2) // 3):
            return JSONResponse(
                {"code": "file_too_large", "message": "Sample exceeds the upload limit"},
                422,
            )
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError, binascii.Error) as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)

    from core.db import get_connection  # noqa: PLC0415
    from core.file_source_adaptation import (  # noqa: PLC0415
        lock_adaptation_template,
        reanalyse_with_adaptation,
    )
    from core.file_source_gate import FileSourceGateError  # noqa: PLC0415

    if len(data) > MAX_FILE_BYTES:
        return JSONResponse(
            {"code": "file_too_large", "message": "Sample exceeds the upload limit"},
            422,
        )
    contract = {
        "kind": "adaptation",
        "class": body["placement_class"],
        "grain": body["grain"],
        "placement": body["placement"],
        "required_fields": body["required_fields"],
        "optional_fields": body.get("optional_fields") or [],
    }
    try:
        reanalysis = reanalyse_with_adaptation(contract, data, str(body["py_source"]))
        if not reanalysis.get("ok"):
            return JSONResponse(
                {
                    "code": (reanalysis.get("error") or {}).get(
                        "code", "adaptation_self_test_failed"
                    ),
                    "message": (reanalysis.get("error") or {}).get(
                        "message", "Adaptation self-test failed"
                    ),
                },
                422,
            )
        gate = reanalysis.get("gate") or {}
        if not gate.get("passed"):
            return JSONResponse(
                {
                    "code": "gate_not_passed",
                    "message": "The recomputed adaptation gate did not pass.",
                    "preview": reanalysis,
                },
                422,
            )
        warnings = gate.get("ambiguities") or []
        warning_reason = str(body.get("warning_reason") or "").strip()
        if warnings and (body.get("accept_warnings") is not True or not warning_reason):
            return JSONResponse(
                {
                    "code": "warnings_require_decision",
                    "message": "Warnings require explicit acceptance and a reason.",
                    "warnings": warnings,
                },
                422,
            )
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT plan_version_id
                    FROM app.datastream_mapping_versions
                    WHERE id = %s AND datastream_id = %s AND project_id = %s
                    """,
                    (
                        body["mapping_version_id"],
                        body["datastream_id"],
                        project_id,
                    ),
                )
                mapping_row = cur.fetchone()
            if mapping_row is None:
                return JSONResponse(_NOT_FOUND, 404)
            created = lock_adaptation_template(
                conn,
                project_id=project_id,
                org_id=org_id,
                template_code=str(body["template_code"]),
                py_source=str(body["py_source"]),
                placement_class=str(body["placement_class"]),
                placement=body["placement"],
                required_fields=list(body["required_fields"]),
                optional_fields=list(body.get("optional_fields") or []),
                aliases=body.get("aliases"),
                grain=str(body["grain"]),
                created_by=identity,
                gate_result=gate,
                sample_content_hash=hashlib.sha256(data).hexdigest(),
                datastream_id=str(body["datastream_id"]),
                mapping_version_id=str(body["mapping_version_id"]),
                plan_version_id=str(mapping_row[0]),
                sample_filename=str(body.get("filename") or ""),
                accepted_warnings=list(warnings),
                warning_reason=warning_reason or None,
                label=body.get("label"),
                idempotency_key=request.headers.get("Idempotency-Key"),
            )
            conn.commit()
    except (ValueError, FileSourceGateError, FileSourceTemplateValidationError) as exc:
        return JSONResponse(
            {"code": getattr(exc, "code", "invalid_adaptation"), "message": str(exc)},
            422,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "file_source_template_api: adaptation_confirmation_failed project=%s: %s",
            project_id,
            exc,
        )
        return JSONResponse(
            {"code": "unavailable", "message": "Adaptation could not be locked"},
            503,
        )
    return JSONResponse({"template": created, "preview": reanalysis}, 201)


async def _confirm_template_mapping(request: Request) -> Response:
    """Confirm a recomputed preview and create a non-live mapping version."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]

    try:
        body = await _json_body(request)
        datastream_id = str(body.get("datastream_id") or "").strip()
        mapping_version_id = str(body.get("mapping_version_id") or "").strip()
        encoded = str(body.get("file_base64") or "")
        resolutions = body.get("resolutions") or []
        from core.csv_excel_import import MAX_FILE_BYTES  # noqa: PLC0415

        if not datastream_id or not mapping_version_id or not encoded:
            raise ValueError(
                "datastream_id, mapping_version_id and file_base64 are required"
            )
        if not isinstance(resolutions, list):
            raise ValueError("resolutions must be an array")
        if len(encoded) > 4 * ((MAX_FILE_BYTES + 2) // 3):
            return JSONResponse(
                {"code": "file_too_large", "message": "Sample exceeds the upload limit"},
                422,
            )
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError, binascii.Error) as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)

    from core.csv_excel_import import (  # noqa: PLC0415
        build_file_source_preview,
        resolve_file_source_producer,
    )
    from core.datastream_field_mapping import confirm_binding  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415
    from core.file_source_gate import (  # noqa: PLC0415
        FileSourceGateError,
        confirm_mapping_version,
    )

    if len(data) > MAX_FILE_BYTES:
        return JSONResponse(
            {"code": "file_too_large", "message": "Sample exceeds the upload limit"},
            422,
        )

    try:
        with get_connection() as conn:
            producer = resolve_file_source_producer(
                conn,
                project_id=project_id,
                datastream_id=datastream_id,
                mapping_version_id=mapping_version_id,
            )
            if producer is None:
                return JSONResponse(_NOT_FOUND, 404)

            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT mapping_payload, plan_version_id
                    FROM app.datastream_mapping_versions
                    WHERE id = %s AND datastream_id = %s AND project_id = %s
                    """,
                    (mapping_version_id, datastream_id, project_id),
                )
                row = cur.fetchone()
            if row is None:
                return JSONResponse(_NOT_FOUND, 404)
            mapping_payload = deepcopy(row[0] if isinstance(row[0], dict) else json.loads(row[0]))
            plan_version_id = str(row[1])

            canonical_ids = _resolution_vocabulary(
                conn, project_id=project_id, template=getattr(producer, "template", None)
            )
            normalized_resolutions: list[dict[str, str]] = []
            seen_sources: set[str] = set()
            for item in resolutions:
                if not isinstance(item, dict):
                    raise ValueError("each resolution must be an object")
                source = str(item.get("source_column") or "").strip()
                target = str(item.get("canonical_target") or "").strip()
                if not source or target not in canonical_ids:
                    raise ValueError(f"invalid resolution for {source or 'unknown column'}")
                if source in seen_sources:
                    raise ValueError(f"duplicate resolution for {source}")
                seen_sources.add(source)
                mapping_payload = confirm_binding(
                    mapping_payload,
                    source,
                    actor=identity,
                    reason="file-source preview confirmed",
                    canonical_target=target,
                )
                normalized_resolutions.append(
                    {"source_column": source, "canonical_target": target}
                )

            preview = build_file_source_preview(
                conn,
                data,
                project_id=project_id,
                datastream_id=datastream_id,
                mapping_version_id=mapping_version_id,
                filename=body.get("filename"),
                mapping_override=mapping_payload,
            )
            if preview is None:
                return JSONResponse(_NOT_FOUND, 404)
            gate = preview.get("gate") or {}
            if preview.get("blocked") or not gate.get("passed"):
                return JSONResponse(
                    {
                        "code": "gate_not_passed",
                        "message": "The server recomputed the preview and the gate did not pass.",
                        "preview": preview,
                    },
                    422,
                )

            raw_warnings = [
                *(mapping_payload.get("ambiguities") or []),
                *(preview.get("ambiguities") or []),
            ]
            accept_warnings = body.get("accept_warnings") is True
            warning_reason = str(body.get("warning_reason") or "").strip()
            if raw_warnings and (not accept_warnings or not warning_reason):
                return JSONResponse(
                    {
                        "code": "warnings_require_decision",
                        "message": "Warnings require explicit acceptance and a reason.",
                        "warnings": raw_warnings,
                    },
                    422,
                )
            accepted_warnings = []
            for index, warning in enumerate(raw_warnings):
                warning = warning if isinstance(warning, dict) else {}
                source = warning.get("source_column")
                field_ids = warning.get("field_ids") or ([source] if source else [])
                accepted_warnings.append(
                    {
                        "code": str(warning.get("code") or f"warning_{index + 1}"),
                        "path": str(warning.get("path") or "$.file_source.ambiguities"),
                        "field_ids": [str(value) for value in field_ids],
                        "candidates": [
                            str(value) for value in (warning.get("candidates") or [])
                        ],
                        "repair": {"accepted": True},
                    }
                )

            template_hash = str(producer.template.get("content_hash") or "")
            evidence = {
                "template_id": producer.template_id,
                "template_content_hash": template_hash,
                "sample_content_hash": hashlib.sha256(data).hexdigest(),
                "sample_filename": str(body.get("filename") or ""),
                "actor": identity,
                "confirmed_at": datetime.now(UTC).isoformat(),
                "resolutions": normalized_resolutions,
                "accepted_warnings": accepted_warnings,
                "warning_reason": warning_reason or None,
            }
            mapping_payload["ambiguities"] = []
            mapping_payload["file_source_confirmation"] = evidence

            decision_hash = hashlib.sha256(
                json.dumps(
                    {
                        "resolutions": normalized_resolutions,
                        "accepted_warnings": accepted_warnings,
                        "warning_reason": warning_reason or None,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            idem = request.headers.get("Idempotency-Key") or hashlib.sha256(
                (
                    f"{project_id}:{datastream_id}:{producer.template_id}:{mapping_version_id}:"
                    f"{template_hash}:{evidence['sample_content_hash']}:"
                    f"{evidence['sample_filename']}:{decision_hash}:{identity}"
                ).encode("utf-8")
            ).hexdigest()
            result = confirm_mapping_version(
                conn,
                template_id=producer.template_id,
                project_id=project_id,
                org_id=org_id,
                datastream_id=datastream_id,
                actor=identity,
                gate_result=gate,
                mapping_payload=mapping_payload,
                evidence=evidence,
                idempotency_key=idem,
                content_hash=template_hash,
                pinned_plan_version_id=plan_version_id,
            )
            conn.commit()
    except (ValueError, FileSourceGateError) as exc:
        return JSONResponse(
            {"code": getattr(exc, "code", "invalid_confirmation"), "message": str(exc)},
            422,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "file_source_template_api: confirmation_failed project=%s: %s",
            project_id,
            exc,
        )
        return JSONResponse(
            {"code": "unavailable", "message": "Confirmation could not be recorded"},
            503,
        )
    return JSONResponse(result, 201)


async def _list_canonical_fields(request: Request) -> Response:
    """GET {base}/canonical-fields -- the vocabulary a contract may declare.

    Measured 2026-08-01: `app.mdm_canonical_fields` was only ever queried to
    VALIDATE ids. Nothing listed it. So the create route could tell a person
    "these field ids are not active canonical fields in scope" and there was no
    way, anywhere in the product, to find out which ids were.

    Viewer: reading the vocabulary is not authoring with it.
    """
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    project_id = request.path_params["project_id"]


    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            fields = list_canonical_fields(conn, project_id=project_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("file_source_template_api: fields_failed project=%s: %s", project_id, exc)
        return JSONResponse(
            {"code": "unavailable", "message": "Canonical fields could not be read"}, 503
        )
    return JSONResponse({"fields": fields}, 200)


async def _declare_canonical_fields(request: Request) -> Response:
    """POST {base}/canonical-fields -- the client declares the fields it needs.

    THE DOOR `datastream_field_mapping.py:740` CALLED "elsewhere". That comment --
    "registry is minted elsewhere" -- named a writer that existed nowhere:
    measured 2026-08-08, `app.mdm_canonical_fields` held ZERO rows at both scopes
    while six modules read it and the GET above listed it. So the closed
    enumeration a Template validates against was closed on nothing, and the
    onboarding panel offered an empty select.

    ARBITRATION OF 2026-08-08 (Jean), which this route implements: the two
    branches `file-source-ingestion.md:45-51` left open are the two SCOPES, not
    two products. A client declares the dimensions and metrics THEIR object needs,
    at project scope; the platform vocabulary stays governed and is not minted
    here. The measure that decided it: one video needs eleven fields, of which the
    thirteen governed ones cover zero.

    Editor, not viewer: reading the vocabulary is not authoring with it -- the
    same split the GET above states in its own docstring.

    Body: {"fields": [{canonical_name, concept_kind, aggregation?, non_additive?,
    unit?, description?, dictionary_field_name?}, ...]}
    """
    auth = await _authorize(request, "editor")
    if isinstance(auth, Response):
        return auth
    identity, _org_id = auth
    project_id = request.path_params["project_id"]

    try:
        body = await _json_body(request)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)

    fields = body.get("fields")
    if not isinstance(fields, list) or not fields:
        return JSONResponse(
            {"code": "missing_field", "message": "fields must be a non-empty list"}, 400
        )

    from core.canonical_field_registry import CanonicalFieldError, declare_many  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415
    from core.object_kind_registry import object_kind_for_datastream  # noqa: PLC0415

    # Story 64.15 -- l objet est DERIVE, jamais reclame. Le corps peut nommer le
    # Datastream ; le serveur lit quel objet il alimente. Accepter un `object_kind`
    # du client lui permettrait d accrocher sa colonne a la definition d un autre
    # objet, et les deux repondraient ensuite differemment a la meme question.
    datastream_id = str(body.get("datastream_id") or "").strip()
    if any(isinstance(f, dict) and f.get("object_kind") for f in fields):
        return JSONResponse(
            {
                "code": "object_kind_not_accepted",
                "message": (
                    "object_kind is derived from the Datastream that feeds the object, "
                    "never sent: name the datastream_id instead."
                ),
            },
            422,
        )

    try:
        with get_connection() as conn:
            # All or nothing: eleven fields describe one video, and stopping
            # halfway leaves a half-described object whose mapping validates
            # against some of its own columns.
            object_kind = (
                object_kind_for_datastream(
                    conn, project_id=project_id, datastream_id=datastream_id
                )
                if datastream_id
                else None
            )
            stamped = [
                {**f, "object_kind": object_kind} if object_kind else dict(f)
                for f in fields
                if isinstance(f, dict)
            ]
            minted = declare_many(
                conn, project_id=project_id, fields=stamped, actor=identity
            )
            conn.commit()
    except CanonicalFieldError as exc:
        return JSONResponse({"code": "invalid_declaration", "message": str(exc)}, 422)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "file_source_template_api: canonical_declare_failed project=%s: %s",
            project_id,
            exc,
        )
        return JSONResponse(
            {"code": "unavailable", "message": "Canonical fields could not be written"}, 503
        )

    return JSONResponse({"fields": minted, "declared": len(minted)}, 201)


file_source_template_routes = [
    # Literal first: `canonical-fields` must be declared ahead of
    # `{template_code}` or Starlette reads it as a template code.
    Route(f"{_BASE}/canonical-fields", endpoint=_list_canonical_fields, methods=["GET"]),
    Route(f"{_BASE}/canonical-fields", endpoint=_declare_canonical_fields, methods=["POST"]),
    Route(
        f"{_BASE}/adaptations/confirm",
        endpoint=_confirm_adaptation,
        methods=["POST"],
    ),
    Route(f"{_BASE}/confirm", endpoint=_confirm_template_mapping, methods=["POST"]),
    Route(_BASE, endpoint=_create_template, methods=["POST"]),
    Route(_BASE, endpoint=_list_versions, methods=["GET"]),
    Route(
        f"{_BASE}/{{template_code}}/versions/{{version}}",
        endpoint=_get_template,
        methods=["GET"],
    ),
]
