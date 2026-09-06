"""The Datastream OBJECT: create it, read it, change it, remove it.

AD-40, second extraction (2026-08-12). `admin_api.py` assembles routes; it does
not implement them. This module holds the eight handlers of the object itself --
the list, the creation, the read, the patch, the deletion, the intent-version
list, the intent validation and the Viewer read model -- and nothing about what
the object COLLECTS, which is `datastream_collection_api`.

THE ROUTES LEAVE IN TWO COLLECTIONS, NOT ONE, AND THAT IS THE CONTRACT.
Starlette resolves in declaration order, and `admin_api.py` says so in a comment
at this exact place: the static sub-paths (`/versions`, `/validate`, `/run`, …)
must precede `/{id}`, or the parametrised route absorbs them. So
`DATASTREAM_OBJECT_ROUTES` splices where the static ones stood and
`DATASTREAM_OBJECT_TAIL_ROUTES` where the catch-alls did, with the other
families in between exactly as before. An extraction that merged them would be a
routing change wearing a refactoring's clothes.

The auth and scope seam stays in `admin_api.py` and is reached at CALL time --
see the forwarders below.
"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.audit import (
    ACTION_DATASTREAM_CREATED,
    ACTION_DATASTREAM_UPDATED,
    declare_action,
    insert_audit_row,
    write_audit_row,
)
from core.datastream_runs_read import (
    _enrich_datastream_runs,
    _read_current_candidate_execution,
    _read_current_published_execution,
    _read_datastream_runs,
    _read_latest_execution,
)

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12) : declarees ICI, a cote du code qui les ecrit, et non
# dans `core/audit.py`. Ce fichier etait un carrefour -- 43 editions de 29
# sujets depuis juin, dont 34 n'ajoutaient qu'une constante -- et 45 % des
# actions reellement ecrites en production n'y etaient meme pas declarees,
# parce que la liste etait trop loin pour valoir le detour. `write_audit_row`
# refuse desormais une action que personne n'a declaree.
ACTION_DATASTREAM_DELETED = declare_action("datastream.deleted")
#: The inverse of the soft archive (2026-08-18). Declared apart from
#: `datastream.updated` because an archive and its undo are the two ends of one
#: reversible decision, and an audit reader looking for "who brought this
#: Datastream back" must not have to read a diff of a generic update to find out.
ACTION_DATASTREAM_RESTORED = declare_action("datastream.restored")


logger = logging.getLogger("core.admin_api")


# --- le joint qui reste dans admin_api -----------------------------------
async def _check_auth(*args, **kwargs):
    """Le joint d'authentification/portee d'`admin_api`, atteint a l'APPEL."""
    from core.admin_api import _check_auth as _impl  # noqa: PLC0415
    return await _impl(*args, **kwargs)

def _enforce_datastream_project_scope(*args, **kwargs):
    """Le joint d'authentification/portee d'`admin_api`, atteint a l'APPEL."""
    from core.admin_api import _enforce_datastream_project_scope as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

def _require_datastream_role(*args, **kwargs):
    """Le joint d'authentification/portee d'`admin_api`, atteint a l'APPEL."""
    from core.admin_api import _require_datastream_role as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

def _resolve_datastream_route_scope(*args, **kwargs):
    """Le joint d'authentification/portee d'`admin_api`, atteint a l'APPEL."""
    from core.admin_api import _resolve_datastream_route_scope as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

# --- les handlers, deplaces TELS QUELS -----------------------------------

async def _list_datastreams(request: Request) -> Response:
    """GET /api/datastreams?project_id=<id> -- list datastreams for a project.

    Response (200): [{"id", "project_id", "name", "module_name", ...}]
    Error:
        400 -- missing project_id
        401 -- unauthorized
        500 -- DB error
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token d'acces requis"},
            status_code=401,
        )

    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id est requis"},
            status_code=400,
        )

    try:
        from core.datastreams import list_datastreams  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(project_id, identity, "viewer", conn)
            if role_error is not None:
                return role_error
            rows = list_datastreams(project_id, conn)
    except Exception as exc:
        logger.error("admin_api: list_datastreams_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur base de donnees: {exc}"},
            status_code=500,
        )

    return JSONResponse(rows)

async def _create_datastream(request: Request) -> Response:
    """POST /api/datastreams -- create a legacy row or versioned intent draft."""

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token d'acces requis"},
            status_code=401,
        )

    try:
        body: dict = json.loads(await request.body())
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Corps JSON invalide: {exc}"},
            status_code=400,
        )

    project_id = (body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "missing_field", "message": "project_id est requis"},
            status_code=422,
        )

    versioned = isinstance(body.get("intent"), dict)
    idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
    if versioned and not idempotency_key:
        return JSONResponse(
            {
                "code": "missing_idempotency_key",
                "message": "Idempotency-Key is required to create a version.",
            },
            status_code=400,
        )

    created_by = identity or "anonymous"
    try:
        from core.datastreams import create_datastream  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            minimum_role = (
                "owner"
                if versioned
                and body["intent"].get("destination", {}).get("policy") == "external_read_only"
                else "member"
            )
            role_error = _require_datastream_role(project_id, created_by, minimum_role, conn)
            if role_error is not None:
                return role_error

            if versioned:
                from core.flows import upsert_flow  # noqa: PLC0415
                from core.main import get_loaded_modules  # noqa: PLC0415

                definition = {
                    "schema_version": "2",
                    "kind": "datastream",
                    "project_id": project_id,
                    "name": (body.get("name") or "").strip(),
                    "intent": body["intent"],
                    # Read from the BODY, not the intent: what a Datastream is for
                    # is a property of the Datastream, not of the extraction
                    # contract. Two flows over one report may serve different
                    # purposes and their intents must stay comparable.
                    "data_role": body.get("data_role"),
                    "idempotency_key": idempotency_key,
                    "reason": body.get("reason", "rest_draft_created"),
                    "trace_id": request.headers.get("traceparent"),
                }
                result = upsert_flow(
                    project_id,
                    definition,
                    created_by,
                    conn,
                    loaded_modules=get_loaded_modules(),
                )
                response = dict(result["flow"])
                response["plan_version"] = result["plan_version"]
                return JSONResponse(response, status_code=201)

            row = create_datastream(body, project_id, created_by, conn)
            conn.commit()
    except ValueError as exc:
        return JSONResponse({"code": "invalid_input", "message": str(exc)}, status_code=422)
    except Exception as exc:
        # Story 34.2: trial datastream cap reached -- typed 409, no partial state.
        # (Lazy isinstance check keeps the import out of the sorted top block.)
        from core.trial_enforcement import TrialDatastreamLimitError  # noqa: PLC0415

        if isinstance(exc, TrialDatastreamLimitError):
            return JSONResponse(exc.to_dict(), status_code=409)
        from core.flows import (  # noqa: PLC0415
            FlowConflictError,
            FlowScopeError,
            FlowUnavailableError,
            FlowValidationError,
        )

        if isinstance(exc, FlowValidationError):
            return JSONResponse(
                {"code": "validation_error", "message": str(exc), "errors": exc.errors},
                status_code=422,
            )
        if isinstance(exc, FlowScopeError):
            return JSONResponse(
                {"code": "not_found", "message": "Flux de donnees introuvable"},
                status_code=404,
            )
        if isinstance(exc, FlowConflictError) or "UniqueViolation" in type(exc).__name__:
            return JSONResponse({"code": "conflict", "message": str(exc)}, status_code=409)
        if isinstance(exc, FlowUnavailableError):
            return JSONResponse({"code": "unavailable", "message": str(exc)}, status_code=503)
        logger.error("admin_api: create_datastream_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur base de donnees"},
            status_code=500,
        )

    write_audit_row(
        identity=created_by,
        action=ACTION_DATASTREAM_CREATED,
        provider_account="",
        connection_ref="",
        metadata={
            "datastream_id": row["id"],
            "project_id": project_id,
            "name": row["name"],
            "module_name": row["module_name"],
        },
    )
    return JSONResponse(row, status_code=201)

async def _get_datastream(request: Request) -> Response:
    """GET /api/datastreams/{id}?project_id=<id> -- single datastream.

    Response (200): datastream object.
    Error:
        400 -- missing project_id
        401 -- unauthorized
        404 -- not found or wrong project
        500 -- DB error
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token d'acces requis"},
            status_code=401,
        )

    ds_id = request.path_params.get("id", "")
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id est requis"},
            status_code=400,
        )

    try:
        from core.datastreams import get_datastream  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            row = get_datastream(ds_id, project_id, conn)
            if row is None:
                # Still check scope: if it exists but in another project, 404 + audit.
                # get_datastream already returns None for wrong project, so just 404.
                return JSONResponse(
                    {"code": "not_found", "message": "Flux de donnees introuvable"},
                    status_code=404,
                )
    except Exception as exc:
        logger.error("admin_api: get_datastream_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur base de donnees: {exc}"},
            status_code=500,
        )

    return JSONResponse(row)

async def _patch_datastream(request: Request) -> Response:
    """PATCH /api/datastreams/{id} -- update a datastream.

    Body (JSON): {"project_id": str, "name"?, "enabled"?, "schedule_mode"?,
                  "refetch_days"?, "date_window_days"?, "config"?,
                  "connection_ref_id"?, "report_profile_id"?, "plan_version_id"?}
    Response (200): updated datastream.
    Error:
        400 -- missing project_id
        401 -- unauthorized
        404 -- not found or wrong project
        409 -- name conflict
        422 -- validation error
        500 -- DB error
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token d'acces requis"},
            status_code=401,
        )

    ds_id = request.path_params.get("id", "")

    try:
        body_bytes_patch = await request.body()
        body: dict = json.loads(body_bytes_patch) if body_bytes_patch.strip() else {}
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Corps JSON invalide: {exc}"},
            status_code=400,
        )

    project_id = (body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "missing_field", "message": "project_id is required in the body"},
            status_code=400,
        )

    activation_replay = False
    activation_key_hash: str | None = None
    try:
        from core.datastreams import get_datastream, update_datastream  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            existing = get_datastream(ds_id, project_id, conn)
            if existing is None:
                return JSONResponse(
                    {"code": "not_found", "message": "Flux de donnees introuvable"},
                    status_code=404,
                )
            # Scope enforcement: the datastream exists in project_id (already verified above).
            scope_err = _enforce_datastream_project_scope(
                existing["project_id"],
                identity,
                ds_id,
                conn,
                claimed_project_id=project_id,
                minimum_role=(
                    "owner"
                    if isinstance(body.get("intent"), dict)
                    and body["intent"].get("destination", {}).get("policy") == "external_read_only"
                    else "member"
                ),
            )
            if scope_err is not None:
                return scope_err

            # THE ROLE IS A GOVERNED CHANGE, NOT A FIELD — amendment 9 of the
            # 2026-08-11 review, delivered 2026-08-31.
            #
            # `data_role` was in `update_datastream`'s allowed set all along, and
            # that door was reached through this body with no base stated and no
            # sentence anywhere naming what the move does downstream. The review
            # that raised this defect grepped `UPDATE … SET data_role` and
            # concluded no door existed; the SET clause is built at runtime, so
            # there was nothing for the grep to find and the door was open.
            #
            # Intercepted HERE and not inside `update_datastream`: the guard is
            # about what a CALLER stated (the base it read), which is a property
            # of the request and not of the row writer — and doing it here leaves
            # exactly one door instead of adding a second address for one gesture.
            if "data_role" in body:
                from core.datastream_data_role import (  # noqa: PLC0415
                    EXPECTED_FIELD,
                    DataRoleChangeRefused,
                    change_data_role,
                )

                try:
                    outcome = change_data_role(
                        conn,
                        project_id=project_id,
                        datastream_id=ds_id,
                        proposed=body.get("data_role"),
                        expected=body.get(EXPECTED_FIELD),
                        actor=identity or "anonymous",
                    )
                except DataRoleChangeRefused as refusal:
                    return JSONResponse(
                        {
                            "code": refusal.code,
                            "message": str(refusal),
                            **({"details": refusal.details} if refusal.details else {}),
                        },
                        # A base that moved is a CONFLICT and everything else is
                        # a refusal of the request: the two are different answers
                        # and a console retries only one of them.
                        status_code=409 if refusal.code == "stale_data_role"
                        else 404 if refusal.code == "not_found"
                        else 422,
                    )
                conn.commit()
                state = get_datastream(ds_id, project_id, conn) or {}
                # WHAT MOVED TRAVELS BACK. A caller cannot re-derive the ladder
                # effect of a change it did not compute, which is the same reason
                # the archive route answers `{"status": "archived"|"deleted"}`.
                return JSONResponse({**state, "data_role_change": outcome})

            if isinstance(body.get("intent"), dict):
                idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
                if not idempotency_key:
                    return JSONResponse(
                        {
                            "code": "missing_idempotency_key",
                            "message": "Idempotency-Key is required to create a version.",
                        },
                        status_code=400,
                    )
                from core.flows import (  # noqa: PLC0415
                    FlowConflictError,
                    FlowScopeError,
                    FlowUnavailableError,
                    FlowValidationError,
                    upsert_flow,
                )
                from core.main import get_loaded_modules  # noqa: PLC0415

                definition = {
                    "schema_version": "2",
                    "kind": "datastream",
                    "id": ds_id,
                    "project_id": project_id,
                    "name": (body.get("name") or existing.get("name") or "").strip(),
                    "intent": body["intent"],
                    "idempotency_key": idempotency_key,
                    "reason": body.get("reason", "rest_draft_revised"),
                    "trace_id": request.headers.get("traceparent"),
                }
                try:
                    result = upsert_flow(
                        project_id,
                        definition,
                        identity or "anonymous",
                        conn,
                        loaded_modules=get_loaded_modules(),
                    )
                except FlowValidationError as exc:
                    return JSONResponse({"code": "validation_error", "errors": exc.errors}, 422)
                except FlowConflictError as exc:
                    return JSONResponse({"code": "conflict", "message": str(exc)}, 409)
                except FlowScopeError:
                    return JSONResponse({"code": "not_found"}, 404)
                except FlowUnavailableError as exc:
                    return JSONResponse({"code": "unavailable", "message": str(exc)}, 503)
                response = dict(result["flow"])
                response["plan_version"] = result["plan_version"]
                return JSONResponse(response)

            # Every activation first locks and rereads the authoritative pointer.
            # This prevents a legacy-looking pre-lock snapshot from bypassing the
            # versioned compare-and-set after a concurrent plan revision.
            locked_activation = None
            if body.get("enabled") is True:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT d.current_plan_version_id,
                               p.executable,
                               p.normalized_payload,
                               d.config,
                               p.capability_fingerprint,
                               d.enabled,
                               d.schedule_mode
                        FROM app.datastreams d
                        LEFT JOIN app.datastream_plan_versions p
                          ON p.id = d.current_plan_version_id
                         AND p.datastream_id = d.id
                         AND p.project_id = d.project_id
                        WHERE d.id = %s
                          AND d.project_id = %s
                          AND d.archived_at IS NULL
                        FOR UPDATE OF d
                        """,
                        (ds_id, project_id),
                    )
                    locked_activation = cur.fetchone()
                if locked_activation is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "Flux de donnees introuvable"},
                        status_code=404,
                    )

            locked_config = (
                locked_activation[3]
                if locked_activation is not None
                else existing.get("config") or {}
            )
            if isinstance(locked_config, str):
                try:
                    locked_config = json.loads(locked_config)
                except (TypeError, ValueError):
                    locked_config = {}
            if (
                body.get("enabled") is True
                and isinstance(locked_config, dict)
                and locked_config.get("publish_gate") == "canonical_semantics_required"
            ):
                return JSONResponse(
                    {
                        "code": "publish_gate_active",
                        "message": (
                            "Activation impossible : les semantiques canoniques requises "
                            "are not completed yet for this generic inbound Datastream."
                        ),
                    },
                    status_code=422,
                )

            current_plan_id = locked_activation[0] if locked_activation is not None else None
            requested_plan_id = str(body.get("plan_version_id") or "").strip()
            if body.get("enabled") is True and current_plan_id is not None:
                if not requested_plan_id:
                    return JSONResponse(
                        {
                            "code": "missing_plan_version_id",
                            "message": (
                                "plan_version_id is required to activate this versioned "
                                "Datastream."
                            ),
                        },
                        status_code=422,
                    )
                if current_plan_id != requested_plan_id:
                    return JSONResponse(
                        {
                            "code": "stale_plan_version",
                            "message": (
                                "Le plan valide n'est plus le plan courant. "
                                "Revalidez le flux avant activation."
                            ),
                            "details": {
                                "requested_plan_version_id": requested_plan_id,
                                "current_plan_version_id": current_plan_id,
                            },
                        },
                        status_code=409,
                    )
                activation_key = (request.headers.get("Idempotency-Key") or "").strip()
                if not activation_key:
                    return JSONResponse(
                        {
                            "code": "missing_idempotency_key",
                            "message": "Idempotency-Key is required for activation.",
                        },
                        status_code=400,
                    )
                from hashlib import sha256  # noqa: PLC0415

                activation_key_hash = sha256(activation_key.encode("utf-8")).hexdigest()
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT metadata->>'plan_version_id'
                        FROM app.audit_log
                        WHERE action = %s
                          AND metadata->>'project_id' = %s
                          AND metadata->>'datastream_id' = %s
                          AND metadata->>'activation_key_hash' = %s
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (
                            ACTION_DATASTREAM_UPDATED,
                            project_id,
                            ds_id,
                            activation_key_hash,
                        ),
                    )
                    activation_evidence = cur.fetchone()
                if activation_evidence is not None:
                    if activation_evidence[0] != requested_plan_id:
                        return JSONResponse(
                            {
                                "code": "idempotency_conflict",
                                "message": "Idempotency-Key was already used for another plan.",
                            },
                            status_code=409,
                        )
                    replay_state = get_datastream(ds_id, project_id, conn)
                    if replay_state is None:
                        return JSONResponse({"code": "not_found"}, status_code=404)
                    return JSONResponse(replay_state)
                executable = locked_activation[1]
                normalized_payload = locked_activation[2]
                if executable is not True:
                    return JSONResponse(
                        {
                            "code": "activation_not_available",
                            "message": (
                                "Activation impossible : le plan versionne n'est pas "
                                "valide (executable). Corrigez le mapping/preview d'abord."
                            ),
                        },
                        status_code=422,
                    )
                if isinstance(normalized_payload, str):
                    try:
                        normalized_payload = json.loads(normalized_payload)
                    except (TypeError, ValueError):
                        normalized_payload = {}
                source = (normalized_payload or {}).get("source") or {}
                if source.get("kind") == "connector_pull":
                    from core.datastream_intents import validate_intent  # noqa: PLC0415
                    from core.main import get_loaded_modules  # noqa: PLC0415
                    from core.source_capabilities import (  # noqa: PLC0415
                        SourceCapabilitiesNotFound,
                        SourceCapabilitiesUnavailable,
                        get_scoped_source_capabilities,
                    )

                    connection_ref_id = str(source.get("connection_ref_id") or "").strip()
                    from core.source_capabilities import (  # noqa: PLC0415
                        get_project_connection_state,
                    )

                    connection_state = get_project_connection_state(
                        project_id=project_id,
                        connection_ref_id=connection_ref_id,
                        identity=identity or "anonymous",
                        conn=conn,
                    )
                    if (
                        not isinstance(connection_state, (tuple, list))
                        or len(connection_state) < 4
                        or connection_state[1] != "active"
                        or connection_state[2] is not True
                        # Second exemplaire de la porte corrigee dans
                        # project_access : 'stale' est l'etat NORMAL d'un jeton
                        # Google 55 minutes apres le consentement (le refresh a
                        # lieu a l'usage), et NULL est l'etat de toute connexion
                        # tant que le poller n'est pas passe. Les refuser ici
                        # renvoyait 422 sur la moindre modification d'un flux
                        # une heure apres sa creation, pendant que l'autre porte
                        # acceptait le meme credential.
                        or connection_state[3] not in ("ok", "stale", None)
                    ):
                        return JSONResponse(
                            {
                                "code": "provider_account_unusable",
                                "message": (
                                    "Le compte fournisseur n'est plus actif et sain. "
                                    "Reconnectez-le puis revalidez le flux."
                                ),
                            },
                            status_code=422,
                        )
                    try:
                        current_capabilities = get_scoped_source_capabilities(
                            project_id=project_id,
                            connection_ref_id=connection_ref_id,
                            identity=identity or "anonymous",
                            loaded_modules=get_loaded_modules(),
                            conn=conn,
                            module_name=source.get("module"),
                        )
                    except SourceCapabilitiesNotFound:
                        return JSONResponse(
                            {
                                "code": "provider_account_unusable",
                                "message": (
                                    "Le compte fournisseur du plan n'est plus actif. "
                                    "Reconnectez-le puis revalidez le flux."
                                ),
                            },
                            status_code=422,
                        )
                    except SourceCapabilitiesUnavailable:
                        return JSONResponse(
                            {
                                "code": "capabilities_unavailable",
                                "message": "Le catalogue de capacites est indisponible.",
                            },
                            status_code=503,
                        )
                    current_validation = validate_intent(
                        normalized_payload,
                        capabilities=current_capabilities,
                    )
                    saved_capability_fingerprint = locked_activation[4]
                    if (
                        current_validation.executable is not True
                        or not saved_capability_fingerprint
                        or current_validation.capability_fingerprint
                        != saved_capability_fingerprint
                    ):
                        return JSONResponse(
                            {
                                "code": "stale_capabilities",
                                "message": (
                                    "Les capacites du fournisseur ont change. "
                                    "Revalidez le plan avant activation."
                                ),
                                "details": {
                                    "plan_capability_fingerprint": saved_capability_fingerprint,
                                    "current_capability_fingerprint": (
                                        current_validation.capability_fingerprint
                                    ),
                                },
                            },
                            status_code=409,
                        )
                cadence = ((normalized_payload or {}).get("schedule") or {}).get("mode")
                # AI-217: the third copy of a table that did not know `weekly`.
                # A weekly plan activated a Datastream whose column said `manual`,
                # so the dispatcher never selected it and the screen said it runs
                # on demand -- both wrong, from one `.get(..., "manual")`.
                from core.datastreams import schedule_mode_for_cadence  # noqa: PLC0415

                mode = schedule_mode_for_cadence(cadence)
                if (
                    len(locked_activation) > 6
                    and locked_activation[5] is True
                    and locked_activation[6] == mode
                ):
                    # Serialized effect replay: the exact immutable plan is already
                    # active at the requested cadence. Return state without another
                    # UPDATE or audit event.
                    updated = get_datastream(ds_id, project_id, conn)
                    activation_replay = True
                else:
                    updated = update_datastream(
                        ds_id, project_id, {"enabled": True, "schedule_mode": mode}, conn
                    )
                    insert_audit_row(
                        conn,
                        identity=identity or "anonymous",
                        action=ACTION_DATASTREAM_UPDATED,
                        provider_account="",
                        connection_ref="",
                        metadata={
                            "datastream_id": ds_id,
                            "project_id": project_id,
                            "plan_version_id": requested_plan_id,
                            "activation_key_hash": activation_key_hash,
                            "operation": "datastream_activation",
                        },
                    )
                    conn.commit()
                    activation_replay = True
            elif body.get("enabled") is True and requested_plan_id:
                # A client presenting immutable-plan evidence must never fall through
                # to the legacy update path when the locked row has no current plan.
                return JSONResponse(
                    {
                        "code": "stale_plan_version",
                        "message": "Le flux ne reference plus le plan valide demande.",
                        "details": {
                            "requested_plan_version_id": requested_plan_id,
                            "current_plan_version_id": None,
                        },
                    },
                    status_code=409,
                )
            else:
                updated = update_datastream(ds_id, project_id, body, conn)
                conn.commit()
    except ValueError as exc:
        return JSONResponse(
            {"code": "invalid_input", "message": str(exc)},
            status_code=422,
        )
    except Exception as exc:
        # Story 34.2 (F1 fix): enabling a draft over the trial cap -> typed 409
        # (same as create), not a generic 500.
        from core.trial_enforcement import TrialDatastreamLimitError  # noqa: PLC0415

        if isinstance(exc, TrialDatastreamLimitError):
            return JSONResponse(exc.to_dict(), status_code=409)
        if "UniqueViolation" in type(exc).__name__ or "unique" in str(exc).lower():
            return JSONResponse(
                {
                    "code": "conflict",
                    "message": "A Datastream with this name already exists in this project",
                },
                status_code=409,
            )
        logger.error("admin_api: patch_datastream_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur base de donnees: {exc}"},
            status_code=500,
        )

    if updated is None:
        return JSONResponse(
            {"code": "not_found", "message": "Flux de donnees introuvable"},
            status_code=404,
        )

    if not activation_replay:
        write_audit_row(
            identity=identity or "anonymous",
            action=ACTION_DATASTREAM_UPDATED,
            provider_account="",
            connection_ref="",
            metadata={"datastream_id": ds_id, "project_id": project_id},
        )
    return JSONResponse(updated)

async def _delete_datastream(request: Request) -> Response:
    """DELETE /api/datastreams/{id}?project_id=<id> -- delete or soft-archive a datastream.

    Response (200): {"status": "deleted"|"archived", "id": str}
    Error:
        400 -- missing project_id
        401 -- unauthorized
        404 -- not found or wrong project
        500 -- DB error
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token d'acces requis"},
            status_code=401,
        )

    ds_id = request.path_params.get("id", "")
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id est requis"},
            status_code=400,
        )

    try:
        from core.datastreams import delete_datastream, get_datastream  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            existing = get_datastream(ds_id, project_id, conn)
            if existing is None:
                return JSONResponse(
                    {"code": "not_found", "message": "Flux de donnees introuvable"},
                    status_code=404,
                )
            scope_err = _enforce_datastream_project_scope(
                existing["project_id"],
                identity,
                ds_id,
                conn,
                claimed_project_id=project_id,
                minimum_role="owner",
            )
            if scope_err is not None:
                return scope_err

            # THE DISPOSITION IS REPORTED, NOT GUESSED. This route used to count
            # `app.pull_jobs` itself and infer "archived" from it -- one of the
            # three conditions `delete_datastream` actually weighs. A datastream
            # whose only history was an inbound receipt was therefore archived and
            # reported as `deleted`: the API answered something that had not
            # happened, and the caller had no way to know (AI-200).
            disposition = delete_datastream(
                ds_id, project_id, conn, archived_by=identity or "anonymous"
            )
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: delete_datastream_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur base de donnees: {exc}"},
            status_code=500,
        )

    if disposition is None:
        return JSONResponse(
            {"code": "not_found", "message": "Flux de donnees introuvable"},
            status_code=404,
        )

    status_label = disposition
    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_DATASTREAM_DELETED,
        provider_account="",
        connection_ref="",
        metadata={
            "datastream_id": ds_id,
            "project_id": project_id,
            "disposition": status_label,
        },
    )
    return JSONResponse({"status": status_label, "id": ds_id})

async def _restore_datastream(request: Request) -> Response:
    """POST /api/datastreams/{id}/restore -- undo a soft archive.

    THE OTHER HALF OF A GESTURE THE PRODUCT ALREADY PROMISED. `DELETE
    /api/datastreams/{id}` has soft-archived since story 21.5, and three surfaces
    tell a person the way back is to RESTORE it -- `SchedulePanel`'s `archived`
    run state ("Restoring it is what makes it runnable again"),
    `schedule_mcp.ARCHIVED_CANNOT_RUN`, and `datastream_dispatch.gate_refusal`
    ("Restore it before collecting anything for it"). No route restored anything.
    An archive was therefore terminal in practice, while every screen described
    it as reversible.

    Body (JSON): {"project_id": str}
    Response (200): the restored datastream row.
    Error:
        400 -- missing project_id
        401 -- unauthorized
        404 -- not found or wrong project
        409 -- not archived, or a live sibling holds its name
        500 -- DB error

    Owner-scoped, exactly like the archive it undoes: bringing a Datastream back
    into the Fleet is the same weight of decision as taking it out.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token d'acces requis"},
            status_code=401,
        )

    ds_id = request.path_params.get("id", "")

    try:
        body_bytes_restore = await request.body()
        body: dict = json.loads(body_bytes_restore) if body_bytes_restore.strip() else {}
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Corps JSON invalide: {exc}"},
            status_code=400,
        )

    project_id = (body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "missing_field", "message": "project_id is required in the body"},
            status_code=400,
        )

    try:
        from core.datastreams import (  # noqa: PLC0415
            NameTakenError,
            NotArchivedError,
            get_datastream,
            restore_datastream,
        )
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            existing = get_datastream(ds_id, project_id, conn)
            if existing is None:
                return JSONResponse(
                    {"code": "not_found", "message": "Flux de donnees introuvable"},
                    status_code=404,
                )
            scope_err = _enforce_datastream_project_scope(
                existing["project_id"],
                identity,
                ds_id,
                conn,
                claimed_project_id=project_id,
                minimum_role="owner",
            )
            if scope_err is not None:
                return scope_err
            try:
                restored = restore_datastream(ds_id, project_id, conn)
            except NotArchivedError:
                return JSONResponse(
                    {
                        "code": "not_archived",
                        "message": "This Datastream is not archived, so there is "
                        "nothing to restore.",
                    },
                    status_code=409,
                )
            except NameTakenError as taken:
                return JSONResponse(
                    {
                        "code": "name_taken",
                        "message": (
                            f"Another Datastream of this project is already called "
                            f"« {taken.name} ». Rename that one, or rename "
                            "this one, and restore it again."
                        ),
                    },
                    status_code=409,
                )
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: restore_datastream_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur base de donnees: {exc}"},
            status_code=500,
        )

    if restored is None:
        return JSONResponse(
            {"code": "not_found", "message": "Flux de donnees introuvable"},
            status_code=404,
        )

    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_DATASTREAM_RESTORED,
        provider_account="",
        connection_ref="",
        metadata={"datastream_id": ds_id, "project_id": project_id},
    )
    return JSONResponse(restored)


async def _list_datastream_versions(request: Request) -> Response:
    """GET immutable intent versions for one project-scoped Datastream."""

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_param", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    try:
        from core.datastream_intents import list_intent_versions  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            versions = list_intent_versions(ds_id, project_id, conn)
    except Exception as exc:
        logger.error("admin_api: datastream_versions_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Versions indisponibles"}, 503)
    return JSONResponse({"versions": versions})

async def _validate_datastream_intent(request: Request) -> Response:
    """Validate a draft intent without provider calls, queueing, or writes."""

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = (body.get("project_id") or "").strip()
    intent = body.get("intent")
    if not project_id or not isinstance(intent, dict):
        return JSONResponse(
            {"code": "missing_field", "message": "project_id et intent sont requis"}, 400
        )
    ds_id = request.path_params.get("id", "")
    try:
        from core.datastream_intents import (  # noqa: PLC0415
            DatastreamIntentStructuralError,
            validate_intent,
        )
        from core.datastreams import get_datastream  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "member", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            if get_datastream(ds_id, project_id, conn) is None:
                return JSONResponse(
                    {"code": "not_found", "message": "Datastream not found"}, 404
                )
            capabilities = None
            source = intent.get("source", {})
            if source.get("kind") == "connector_pull":
                from core.main import get_loaded_modules  # noqa: PLC0415
                from core.source_capabilities import (  # noqa: PLC0415
                    SourceCapabilitiesNotFound,
                    SourceCapabilitiesUnavailable,
                    get_scoped_source_capabilities,
                )

                try:
                    capabilities = get_scoped_source_capabilities(
                        project_id=project_id,
                        connection_ref_id=source.get("connection_ref_id", ""),
                        identity=identity or "anonymous",
                        loaded_modules=get_loaded_modules(),
                        conn=conn,
                        # The intent names its tool; validation must read the
                        # catalog of THAT tool. Omitted here, a Google intent
                        # created seconds earlier by a route that does pass it
                        # was then refused by its own validation -- create 201,
                        # validate 404, on the same object.
                        module_name=source.get("module"),
                    )
                except SourceCapabilitiesNotFound:
                    return JSONResponse({"code": "not_found", "message": "Source introuvable"}, 404)
                except SourceCapabilitiesUnavailable:
                    return JSONResponse(
                        {"code": "unavailable", "message": "Catalogue indisponible"}, 503
                    )
            result = validate_intent(intent, capabilities=capabilities)
    except DatastreamIntentStructuralError as exc:
        return JSONResponse(
            {"code": "invalid_intent", "issues": [item.as_dict() for item in exc.issues]},
            422,
        )
    except Exception as exc:
        logger.error("admin_api: validate_datastream_intent_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Validation indisponible"}, 503)

    payload = result.as_dict()
    return JSONResponse(payload, 200 if result.executable else 422)

async def _get_datastream_versions(request: Request) -> Response:
    """GET /api/datastreams/{id}/read-model?project_id=<id> (Viewer) -- Story 12.14.

    Assembles the versioned read model the 12.14 UI needs. 200 with:
      {
        plan_versions: [...],           # list_intent_versions (12.2)
        mapping_versions: [...],        # list_mapping_versions (12.3)
        current_published_execution_id, # the atomic pointer (12.5)
        current_candidate,              # newest non-terminal execution (12.5)
        published_execution,            # get_execution of the pointer (DQ state,
                                        #   freshness=state_changed_at, row_count,
                                        #   content_hash)
        publication_log: [...],         # get_publication_log (actor=published_by,
                                        #   prior_execution_id evidence)
        recent_imports: [...],          # list_ledger (row/rejection counts)
      }
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_param", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    try:
        from core.datastream_field_mapping import list_mapping_versions  # noqa: PLC0415
        from core.datastream_intents import list_intent_versions  # noqa: PLC0415
        from core.datastream_publication import (  # noqa: PLC0415
            ExecutionNotFound,
            get_execution,
            get_publication_log,
        )
        from core.datastreams import get_datastream  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415
        from core.managed_feed_ledger import list_ledger  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error

            data_project_id = _resolve_datastream_route_scope(conn, ds_id, project_id)
            if data_project_id is None:
                return JSONResponse(
                    {"code": "not_found", "message": "Flux de donnees introuvable"}, 404
                )
            datastream = get_datastream(ds_id, data_project_id, conn)
            if datastream is None:
                return JSONResponse(
                    {"code": "not_found", "message": "Flux de donnees introuvable"}, 404
                )

            plan_versions = list_intent_versions(ds_id, data_project_id, conn)
            mapping_versions = list_mapping_versions(ds_id, data_project_id, conn)

            current_published_execution_id = _read_current_published_execution(
                conn, ds_id, data_project_id
            )
            published_execution = None
            if current_published_execution_id:
                try:
                    published_execution = get_execution(
                        current_published_execution_id, data_project_id, conn
                    )
                except ExecutionNotFound:
                    published_execution = None

            current_candidate = _read_current_candidate_execution(
                conn, ds_id, data_project_id, current_published_execution_id
            )
            latest_execution = _read_latest_execution(conn, ds_id, data_project_id)
            publication_log = get_publication_log(ds_id, data_project_id, conn, limit=20)
            recent_imports = list_ledger(ds_id, data_project_id, conn, limit=20)
            runs = _read_datastream_runs(conn, ds_id, data_project_id, limit=100)
            _enrich_datastream_runs(
                runs, recent_imports, current_published_execution_id
            )
    except Exception as exc:
        logger.error("admin_api: get_datastream_versions_error: %s", exc)
        return JSONResponse(
            {"code": "unavailable", "message": "Lecture des versions indisponible"}, 503
        )

    return JSONResponse(
        {
            "datastream_id": ds_id,
            "project_id": project_id,
            "data_project_id": data_project_id,
            "datastream": datastream,
            "plan_versions": plan_versions,
            "mapping_versions": mapping_versions,
            "current_published_execution_id": current_published_execution_id,
            "published_execution": published_execution,
            "current_candidate": current_candidate,
            "latest_execution": latest_execution,
            "publication_log": publication_log,
            "recent_imports": recent_imports,
            "runs": runs,
            # PHASE B / TODO (honest -- no backing column in the 12.2-12.5 tables):
            #   * a trace_id per execution: publication_log carries published_by
            #     (actor) but there is no per-execution trace column in 042; the
            #     trace lives on app.operations for recovery ops only. Surfacing a
            #     unified per-version trace is Phase B.
            #   * an explicit per-execution rejection_count: rejection counts live on
            #     the managed_feed ledger rows (recent_imports), not on the 042
            #     execution row -- the UI joins by execution_id. A denormalised
            #     execution.rejection_count column is Phase B.
        }
    )


# --- LES ROUTES, dans l'ordre exact ou elles etaient declarees ------------
#
# Starlette resout dans l'ordre de declaration. Chaque collection est
# epissee par `admin_api` a la position que ses routes occupaient : meme
# chemins, memes methodes, meme ordre. La preuve est un dump de
# `admin_api.router.routes` avant/apres, pas une lecture de diff.

DATASTREAM_OBJECT_ROUTES = [
    # Story 8.2: datastream CRUD + /run.
    # IMPORTANT: /api/datastreams (list/create) must precede the parametrized
    # /{id} routes. The /run, /ledger, /refetch sub-routes must precede /{id}
    # so Starlette does not absorb them as id path parameters.
    Route("/api/datastreams", endpoint=_list_datastreams, methods=["GET"]),
    Route("/api/datastreams", endpoint=_create_datastream, methods=["POST"]),
    Route(
        "/api/datastreams/{id}/versions",
        endpoint=_list_datastream_versions,
        methods=["GET"],
    ),
    Route(
        "/api/datastreams/{id}/validate",
        endpoint=_validate_datastream_intent,
        methods=["POST"],
    ),
    # 2026-08-18: the undo of the soft archive. Declared HERE, with the other
    # sub-routes carrying a literal last segment, and BEFORE the `/{id}` family
    # below -- the rule this list's own header states, and the reason `/versions`
    # and `/validate` sit where they do.
    Route(
        "/api/datastreams/{id}/restore",
        endpoint=_restore_datastream,
        methods=["POST"],
    ),
]

DATASTREAM_OBJECT_TAIL_ROUTES = [
    # Story 12.14: versioned Datastream read model (Viewer). A DISTINCT
    # /read-model path (the /versions path is already taken by the 12.2 intent-
    # version list, _list_datastream_versions). Static sub-path precedes /{id}.
    Route(
        "/api/datastreams/{id}/read-model",
        endpoint=_get_datastream_versions,
        methods=["GET"],
    ),
    Route("/api/datastreams/{id}", endpoint=_get_datastream, methods=["GET"]),
    Route("/api/datastreams/{id}", endpoint=_patch_datastream, methods=["PATCH"]),
    Route("/api/datastreams/{id}", endpoint=_delete_datastream, methods=["DELETE"]),
]
