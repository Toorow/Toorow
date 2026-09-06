"""The mapping a Datastream applies, and the projection it compiles.

AD-40, second extraction (2026-08-12). Four handlers: profiling a source against
the governed target, listing and reading the immutable mapping versions, and
compiling the projection plan a run executes. The writing of a mapping version
lives elsewhere (`_create_datastream_mapping_version`, still in `admin_api.py`
with the Project-scoped family) -- these are the Datastream-scoped doors only.
"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger("core.admin_api")

# --- le joint qui reste dans admin_api -----------------------------------
async def _check_auth(*args, **kwargs):
    """Le joint d'authentification/portee d'`admin_api`, atteint a l'APPEL."""
    from core.admin_api import _check_auth as _impl  # noqa: PLC0415
    return await _impl(*args, **kwargs)

def _require_datastream_role(*args, **kwargs):
    """Le joint d'authentification/portee d'`admin_api`, atteint a l'APPEL."""
    from core.admin_api import _require_datastream_role as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

# --- les handlers, deplaces TELS QUELS -----------------------------------

async def _profile_datastream_mapping(request: Request) -> Response:
    """POST /api/datastreams/{id}/mapping/profile -- physically profile fields and suggest roles."""
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
    sample_data = body.get("sample_data")
    if sample_data is not None and not isinstance(sample_data, list):
        return JSONResponse(
            {"code": "invalid_field", "message": "sample_data must be a list"}, 400
        )
    if isinstance(sample_data, list) and len(sample_data) > 500:
        return JSONResponse(
            {"code": "too_many_rows", "message": "sample_data is limited to 500 rows"}, 400
        )
    field_records = body.get("field_records")
    if field_records is not None and not isinstance(field_records, list):
        return JSONResponse(
            {"code": "invalid_field", "message": "field_records must be a list"}, 400
        )
    if isinstance(field_records, list) and len(field_records) > 200:
        return JSONResponse(
            {"code": "too_many_fields", "message": "field_records is limited to 200 fields"}, 400
        )

    try:
        from core.datastream_field_mapping import profile_fields  # noqa: PLC0415
        from core.datastreams import get_datastream  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "member", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            ds = get_datastream(ds_id, project_id, conn)
            if ds is None:
                return JSONResponse({"code": "not_found", "message": "Flux introuvable"}, 404)

            if (
                field_records is None
                and ds.get("connection_ref_id")
                and ds.get("report_profile_id")
            ):
                from core.main import get_loaded_modules  # noqa: PLC0415
                from core.source_capabilities import get_scoped_source_capabilities  # noqa: PLC0415

                try:
                    caps = get_scoped_source_capabilities(
                        project_id=project_id,
                        connection_ref_id=ds.get("connection_ref_id", ""),
                        identity=identity or "anonymous",
                        loaded_modules=get_loaded_modules(),
                        conn=conn,
                        # The datastream row records which module it reads.
                        module_name=ds.get("module_name"),
                    )
                    field_records = _report_field_records(caps, ds.get("report_profile_id"))
                except Exception as exc:
                    # This used to be `except Exception: pass`. The combination of a
                    # swallowed error and an empty field universe is why profiling
                    # returned an empty answer that looked like a legitimate one.
                    logger.error(
                        "admin_api: profile_datastream_mapping_capabilities_error: %s", exc
                    )
                    return JSONResponse(
                        {
                            "code": "capabilities_unavailable",
                            "message": (
                                "Source capabilities could not be resolved, so no field "
                                "can be profiled."
                            ),
                        },
                        503,
                    )

            if not field_records:
                # An empty universe cannot produce a mapping, and a profiling
                # response of zero fields reads as "this source has nothing to map".
                # Say which is true instead.
                return JSONResponse(
                    {
                        "code": "no_profilable_fields",
                        "message": (
                            "The selected report profile exposes no resolvable field. "
                            "Provide `field_records` explicitly, or check the connector "
                            "manifest declares the report's metrics and dimensions."
                        ),
                        "report_profile_id": ds.get("report_profile_id"),
                    },
                    422,
                )

            # THE VOCABULARY A BINDING IS CHECKED AGAINST -- Canonical Fields
            # first (story 49.3, readers step). This read used to be
            # `SELECT name FROM app.target_fields WHERE status = 'approved'`, so
            # the accepted vocabulary was the 15 rows migration 023 seeded into a
            # store that can no longer be written to. A field this Project
            # declared under its own source lives in `app.mdm_canonical_fields`
            # -- which `glossary.md` names as "the vocabulary a binding is checked
            # against" -- and every one of them was answered
            # `target_field_not_found`. The registry answers first; the dictionary
            # stays the layer below, still approved-only (H1: a draft field must
            # not appear as an accepted target).
            from core.governed_field_catalogue import binding_vocabulary  # noqa: PLC0415

            known_target_fields = binding_vocabulary(conn, project_id=project_id)

            result = profile_fields(
                field_records=field_records,
                sample_data=sample_data,
                known_target_fields=known_target_fields,
            )
            return JSONResponse(result, 200)
    except (TypeError, ValueError) as exc:
        return JSONResponse({"code": "invalid_input", "message": str(exc)}, 400)
    except Exception as exc:
        logger.error("admin_api: profile_datastream_mapping_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Profilage indisponible"}, 503)

async def _list_datastream_mapping_versions(request: Request) -> Response:
    """GET /api/datastreams/{id}/mapping/versions -- list mapping versions."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_param", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    try:
        from core.datastream_field_mapping import (  # noqa: PLC0415
            DatastreamMappingNotFound,
            list_mapping_versions,
        )
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            versions = list_mapping_versions(ds_id, project_id, conn)
            return JSONResponse({"versions": versions}, 200)
    except DatastreamMappingNotFound:
        return JSONResponse({"code": "not_found", "message": "Flux introuvable"}, 404)
    except Exception as exc:
        logger.error("admin_api: list_datastream_mapping_versions_error: %s", exc)
        return JSONResponse(
            {"code": "unavailable", "message": "Lecture des versions indisponible"}, 503
        )

async def _compare_datastream_mapping_versions(request: Request) -> Response:
    """GET /api/datastreams/{id}/mapping/versions/compare -- two versions, read side by side.

    WHY IT EXISTS — 2026-08-18. The ledger on the Mapping tab listed a ULID, a
    state, two counts, two hashes and a date. Nothing in that row says what the
    version DECIDED, and nothing between two rows says what changed: the history
    of the one object this tab exists to change was unreadable AS a history.

    IT IS A READ, AND IT MOVES NOTHING. Two recorded versions, the same composer
    the confirmation dialog uses (`core.mapping_value_diff`), and the same vocabulary
    resolution — so "this column stopped landing" is one sentence in the product,
    not two spellings of it. `viewer` is the role, because reading history is not
    proposing a change; proposing one is still `workbench/mapping/changes`.

    `base` and `against` accept either the version id or its integer number,
    exactly as `mapping/versions/{ver}` does — the ledger holds both, and forcing
    the caller to translate would be this door inventing a third address.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_param", "message": "project_id est requis"}, 400)
    base_spec = (request.query_params.get("base") or "").strip()
    against_spec = (request.query_params.get("against") or "").strip()
    if not base_spec or not against_spec:
        return JSONResponse(
            {
                "code": "missing_param",
                "message": "Two versions are required: `base` and `against`.",
            },
            400,
        )
    ds_id = request.path_params.get("id", "")
    try:
        from core.canonical_field_registry import list_visible_canonical_fields  # noqa: PLC0415
        from core.datastream_field_mapping import (  # noqa: PLC0415
            DatastreamMappingNotFound,
            get_mapping_version,
        )
        from core.db import get_connection  # noqa: PLC0415
        from core.mapping_value_diff import value_diff  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            try:
                base = get_mapping_version(ds_id, project_id, base_spec, conn)
                against = get_mapping_version(ds_id, project_id, against_spec, conn)
            except DatastreamMappingNotFound:
                return JSONResponse({"code": "not_found", "message": "Version introuvable"}, 404)
            # The name, never the identity -- the same rule the confirmation
            # dialog follows. A vocabulary that cannot be read costs the reading
            # its names and not the comparison.
            try:
                names = {
                    str(entry["id"]): str(entry["canonical_name"])
                    for entry in list_visible_canonical_fields(conn, project_id=project_id)
                }
            except Exception as exc:  # noqa: BLE001 -- unreadable is not empty
                logger.error("admin_api: compare_mapping_versions_vocabulary_error: %s", exc)
                names = {}
            return JSONResponse(
                {
                    "base": _version_summary(base),
                    "against": _version_summary(against),
                    "value_diff": value_diff(
                        "mapping",
                        _mapping_payload(base),
                        _mapping_payload(against),
                        canonical_names=names,
                    ),
                },
                200,
            )
    except Exception as exc:
        logger.error("admin_api: compare_datastream_mapping_versions_error: %s", exc)
        return JSONResponse(
            {"code": "unavailable", "message": "Comparaison des versions indisponible"}, 503
        )


def _mapping_payload(version: dict) -> dict:
    """The contract of one version, whatever shape the driver handed it back in.

    `jsonb` comes back as a dict on every connection this repository opens; a
    string would be a driver configuration difference, not a broken version, and
    answering `{}` for it would read as "this version binds nothing".
    """
    payload = version.get("mapping_payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return {}
    return payload if isinstance(payload, dict) else {}


def _version_summary(version: dict) -> dict:
    """What names a version in the answer: its id, its NUMBER, its author, its date.

    Not the payload. A comparison that echoed both contracts whole would put two
    full mapping documents on the wire to say which six readings differ.
    """
    return {
        "id": version.get("id"),
        "version_number": version.get("version_number"),
        "created_by": version.get("created_by"),
        "created_at": version.get("created_at"),
        "content_hash": version.get("content_hash"),
    }


async def _get_datastream_mapping_version(request: Request) -> Response:
    """GET /api/datastreams/{id}/mapping/versions/{ver} -- get single mapping version."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_param", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    ver_spec = request.path_params.get("ver", "")
    try:
        from core.datastream_field_mapping import (  # noqa: PLC0415
            DatastreamMappingNotFound,
            get_mapping_version,
        )
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            version = get_mapping_version(ds_id, project_id, ver_spec, conn)
            return JSONResponse(version, 200)
    except DatastreamMappingNotFound:
        return JSONResponse({"code": "not_found", "message": "Version introuvable"}, 404)
    except Exception as exc:
        logger.error("admin_api: get_datastream_mapping_version_error: %s", exc)
        return JSONResponse(
            {"code": "unavailable", "message": "Lecture de la version indisponible"}, 503
        )

async def _compile_datastream_projection(request: Request) -> Response:
    """POST /api/datastreams/{id}/projection/compile -- Story 12.4.

    Compile a SAFE KPI projection plan from an immutable 12.3 mapping version.
    Pure metadata over the mapping version + governed project preferences: it
    NEVER publishes, moves no pointer, and performs no BigQuery write
    (publication atomicity is 12.5). Member role required (viewer < member <
    owner). A projection that fails any compile gate returns 422 with the
    deterministic issue list and blocks publication semantics.

    Body: {"project_id", "mapping_version" (spec or number, default 'latest'),
           "dimension_projection" (optional field_id),
           "connector_canonical_breakdown" (optional; the connector's current
           canonical breakdown name -- required when dimension_projection is set,
           else the projection is rejected governed_dim_shadows_canonical),
           "approved" (optional)}.
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
    version_spec = body.get("mapping_version")
    dimension_projection = body.get("dimension_projection")
    # The connector's CURRENT canonical breakdown partition name (what
    # rollup.canonical_breakdown_per_connector / the dbt marts'
    # MIN(breakdown_dimension) already pin -- e.g. 'country' for GA4/GSC). Passed
    # in by the caller (Story 12.5 will derive it from the published fact); the
    # compiler REJECTS a governed projection that would sort at/before it and
    # re-pin canonical (governed_dim_shadows_canonical). Unknown => fail closed.
    connector_canonical_breakdown = body.get("connector_canonical_breakdown")
    approved = bool(body.get("approved", False))
    try:
        from core.datastream_field_mapping import (  # noqa: PLC0415
            DatastreamMappingNotFound,
            get_mapping_version,
            list_mapping_versions,
        )
        from core.datastream_projection import (  # noqa: PLC0415
            ProjectionCompileError,
            compile_projection,
        )
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "member", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error

            if version_spec in (None, "", "latest"):
                versions = list_mapping_versions(ds_id, project_id, conn)
                if not versions:
                    return JSONResponse(
                        {"code": "not_found", "message": "No mapping version"}, 404
                    )
                mapping_version = versions[0]  # newest-first
            else:
                mapping_version = get_mapping_version(ds_id, project_id, version_spec, conn)

            # Governed project-scoped cardinality/scan thresholds (no hardcode).
            preferences: dict = {}
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT max_projection_grain_cardinality, max_projection_scan_bytes
                    FROM app.project_preferences
                    WHERE project_id = %s
                    """,
                    (project_id,),
                )
                pref_row = cur.fetchone()
                if pref_row is not None:
                    preferences = {
                        "max_projection_grain_cardinality": pref_row[0],
                        "max_projection_scan_bytes": pref_row[1],
                    }

        plan = compile_projection(
            mapping_version,
            project_preferences=preferences,
            dimension_projection=dimension_projection,
            connector_canonical_breakdown=connector_canonical_breakdown,
            approved=approved,
        )
        if not plan["executable"]:
            return JSONResponse(
                {"code": "projection_rejected", "issues": plan["issues"], "plan": plan},
                422,
            )
        return JSONResponse(plan, 200)
    except DatastreamMappingNotFound:
        return JSONResponse({"code": "not_found", "message": "Version introuvable"}, 404)
    except ProjectionCompileError as exc:
        logger.error("admin_api: compile_datastream_projection_invalid: %s", exc)
        return JSONResponse(
            {"code": "unavailable", "message": "Compilation de projection indisponible"}, 503
        )
    except Exception as exc:
        logger.error("admin_api: compile_datastream_projection_error: %s", exc)
        return JSONResponse(
            {"code": "unavailable", "message": "Compilation de projection indisponible"}, 503
        )

def _report_field_records(capabilities: dict, report_profile_id: object) -> list[dict]:
    """Resolve the field universe a report profile actually exposes.

    This replaced a read of `report["field_catalog"]`, a key that appears in no
    connector manifest and in no `$def` of `source-capabilities.schema.json`. It
    therefore returned `[]` for every report of every connector -- measured on
    2026-07-30: 129 report profiles across 38 modules, zero carrying the key --
    so `profile_fields` was handed an empty universe every time and no mapping
    could ever be proposed. That is the mechanical reason 42 Datastreams reached
    a plan and none reached a mapping version.

    What the contract does declare is a report's `metrics` and `dimensions`, as
    lists of `field_id` resolving against the connector-level `fields`. Those
    records already carry exactly what profiling consumes -- `kind`,
    `physical_type`, `semantic_hints`, `canonical_target`, `aggregation`,
    `non_additive`. Order follows the report's own declaration so the proposal is
    stable between calls.
    """
    fields_by_id = {
        str(field.get("field_id")): field
        for field in capabilities.get("fields", [])
        if isinstance(field, dict) and field.get("field_id")
    }
    report = next(
        (
            candidate
            for candidate in capabilities.get("reports", [])
            if isinstance(candidate, dict) and candidate.get("id") == report_profile_id
        ),
        None,
    )
    if report is None:
        return []

    records: list[dict] = []
    seen: set[str] = set()
    # Dimensions first: the grain a mapping is built around reads better when the
    # keys precede the measures, and profiling proposes the date grain from them.
    for field_id in list(report.get("dimensions") or []) + list(report.get("metrics") or []):
        key = str(field_id)
        if key in seen:
            continue
        field = fields_by_id.get(key)
        if field is None:
            # A report naming a field the connector does not declare is a manifest
            # defect. Skipping it silently would hide it; the caller sees a short
            # universe and the conformance suite is where it gets named.
            logger.warning(
                "admin_api: report %r declares unknown field %r", report_profile_id, key
            )
            continue
        seen.add(key)
        records.append(field)
    return records


# --- LES ROUTES, dans l'ordre exact ou elles etaient declarees ------------
#
# Starlette resout dans l'ordre de declaration. Chaque collection est
# epissee par `admin_api` a la position que ses routes occupaient : meme
# chemins, memes methodes, meme ordre. La preuve est un dump de
# `admin_api.router.routes` avant/apres, pas une lecture de diff.

DATASTREAM_MAPPING_ROUTES = [
    Route(
        "/api/datastreams/{id}/mapping/profile",
        endpoint=_profile_datastream_mapping,
        methods=["POST"],
    ),
    Route(
        "/api/datastreams/{id}/mapping/versions",
        endpoint=_list_datastream_mapping_versions,
        methods=["GET"],
    ),
    # There is deliberately NO POST here. A previous pass of this session
    # mounted one, reading the orphaned `_create_datastream_mapping_version`
    # as the repository's named "handler no route exposes" defect. That was
    # wrong, and the blank line below is the scar of the removal rather than
    # an oversight:
    #
    #   * 26695dc retired this exact Route when it landed the six-tab
    #     Workbench, and moved the append to `workbench/mapping/changes`
    #     then `confirm` -> `datastream_change.confirm_change`;
    #   * that governed path runs inside `execute_operation` and calls
    #     `save_field_mapping(advance_pointer=False, commit=False)`;
    #   * the orphaned handler keeps the older shape -- no operation, and
    #     `advance_pointer` left at its `True` default -- so it would
    #     ACTIVATE the version it appends, which both governed callers
    #     refuse explicitly.
    #
    # Remounting it reintroduced an ungoverned, self-activating write path.
    # The orphan is the inventory of a retirement, not of a missing door.

    # TWO VERSIONS, SIDE BY SIDE. It is declared BEFORE `{ver}` and that order is
    # load-bearing: Starlette resolves in declaration order, so the path
    # parameter below would otherwise swallow `compare` and answer "version
    # introuvable" for a comparison. A GET, `viewer`, and it writes nothing.
    Route(
        "/api/datastreams/{id}/mapping/versions/compare",
        endpoint=_compare_datastream_mapping_versions,
        methods=["GET"],
    ),
    Route(
        "/api/datastreams/{id}/mapping/versions/{ver}",
        endpoint=_get_datastream_mapping_version,
        methods=["GET"],
    ),
    # Story 12.4: safe KPI projection compile (Member; NEVER publishes).
    Route(
        "/api/datastreams/{id}/projection/compile",
        endpoint=_compile_datastream_projection,
        methods=["POST"],
    ),
]
