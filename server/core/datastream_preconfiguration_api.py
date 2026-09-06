"""Strict Project-scoped REST seam for Story 47.2 setup proposals."""

from __future__ import annotations

import json
import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.datastream_activation import (
    ActivationValidationError,
    compile_materialization_contract,
    execute_confirmed_operation,
    freeze_final_review,
    materialize_draft_mutation,
    persist_final_review,
    prepare_confirmation,
    publish_activate_mutation,
    read_candidate_review,
    read_final_review,
    read_materialization_status,
    read_preview,
)
from core.datastream_activation import (
    preview_idempotency_hash as _preview_idempotency_hash,
)
from core.datastream_preconfiguration import (
    PreconfigurationConflict,
    PreconfigurationNotFound,
    PreconfigurationValidationError,
    compile_draft,
    create_or_resume_draft,
    discard_draft,
    list_drafts,
    read_draft,
    read_proposal,
    update_draft,
)
from core.datastream_setup_observations import (
    ObservationConflict,
    ObservationNotFound,
    ObservationUnavailable,
    ObservationValidationError,
    create_observation,
    get_source_options,
    read_observation,
)
from core.entry_confirmations import (
    DATASTREAM_CANDIDATE_PUBLISH_ACTIVATE_COMMAND,
    DATASTREAM_SETUP_CREATE_DRAFT_COMMAND,
    EntryConfirmationRefused,
    EntryConfirmationValidationError,
)
from core.inbound_seam import resolve_inbound

# Setup-asset staging comes from the inbound package, asked for BY CAPABILITY so
# this module never names a provider (AD-2). Bound once at import: these are
# module-level functions and classes, not request-scoped closures. The block used
# to sit ABOVE the starlette imports, which is why seven E402 followed this file
# around; it is import-order-independent, so it belongs here.
SetupAssetNotFound = resolve_inbound("SetupAssetNotFound")
SetupAssetValidationError = resolve_inbound("SetupAssetValidationError")
load_setup_asset = resolve_inbound("load_setup_asset")
stage_setup_asset = resolve_inbound("stage_setup_asset")

logger = logging.getLogger(__name__)


def _bigquery_setup_reader(connection_id: str | None = None):
    """The production typed client for `external_bq` discovery, or a named refusal.

    Resolved through the module registry rather than imported: core never imports
    `server/modules/*` (AD-2), and the loader is the one place that knows which
    connectors this deployment carries. A deployment without the BigQuery
    connector says so, instead of raising an import error the operator would read
    as a broken screen.

    The connection is bound at CONSTRUCTION and per request, like
    `_sheets_setup_reader` below and for the same reason: BigQuery is a Google
    source and reads through the person's consent. `None` is the self-hosted
    fallback -- the deployment reading its own project with Application Default
    Credentials -- and never the path a customer takes.
    """
    from core.datastream_setup_observations import BIGQUERY_CONNECTORS  # noqa: PLC0415
    from core.main import get_loaded_modules  # noqa: PLC0415

    for loaded in get_loaded_modules():
        if loaded.name in BIGQUERY_CONNECTORS:
            reader = getattr(loaded.connector_module, "BigQuerySetupReader", None)
            if reader is not None:
                return reader(connection_id)
    raise ObservationUnavailable(
        "The BigQuery connector is not installed in this deployment, so no external "
        "warehouse schema can be read. Nothing was saved."
    )


def _sheets_setup_reader(connection_id: str):
    """The production typed client for `sheet_schema` discovery, or a refusal.

    Same shape as `_bigquery_setup_reader` above and for the same reason: core
    never imports `server/modules/*` (AD-2), so the module registry answers which
    connectors this deployment carries. A deployment without the Google Sheets
    connector says so rather than raising an import error an operator would read
    as a broken screen.

    The connection is bound at CONSTRUCTION and per request. The client fetches
    its own token from `nango_client` at call time; nothing here holds one.
    """
    from core.main import get_loaded_modules  # noqa: PLC0415

    for loaded in get_loaded_modules():
        if loaded.name == "google-sheets":
            reader = getattr(loaded.connector_module, "SheetsSetupReader", None)
            if reader is not None:
                return reader(connection_id)
    raise ObservationUnavailable(
        "The Google Sheets connector is not installed in this deployment, so no sheet "
        "structure can be read. Nothing was saved."
    )


def _source_account_connection_id(
    conn, *, project_id: str, source_account_ref: str, source_label: str
) -> str:
    """The authorization behind the chosen Source Account, scoped to the Project.

    Same join `_validate_reference_scope` already runs, read here for the one
    thing the validation does not return: the connection the token is minted
    from. Scoped by `project_id` in the SAME statement, so a Source Account of
    another organization cannot answer.

    IT LOST THE WORD `sheets` (AI-285). The join names no channel and never did;
    the name did. External BigQuery needs exactly this -- BigQuery is a Google
    source, so it reads through the person's consent like every other one -- and
    a resolver named for one channel is a resolver the next reader writes a
    second copy of. `source_label` is the only thing that was ever specific: the
    sentence a person reads when the account is not theirs.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cr.id FROM app.projects p "
            "JOIN app.connection_ref cr ON cr.owner_org_id=p.org_id "
            "JOIN app.credential_accounts ca ON ca.credential_id=cr.id "
            "WHERE p.id=%s AND ca.source_account_id=%s AND ca.available=TRUE",
            (project_id, source_account_ref),
        )
        row = cur.fetchone()
    if row is None:
        raise ObservationNotFound(f"{source_label} Source Account not found")
    return str(row[0])


def _declared_channel_contract(conn, *, project_id: str, draft_id: str) -> dict[str, Any]:
    """What the operator DECLARED about this channel on the current revision.

    Read from the draft rather than taken from the request body: the declaration
    is persisted operator input, and a discovery request that could carry it
    would let a browser assert a promise the draft never recorded.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.normalized_operator_input FROM app.datastream_setup_drafts d "
            "JOIN app.datastream_setup_draft_revisions r ON r.id=d.current_revision_id "
            "WHERE d.id=%s AND d.project_id=%s",
            (draft_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        return {}
    payload = row[0]
    if isinstance(payload, (str, bytes, bytearray)):
        payload = json.loads(payload)
    source = (payload or {}).get("source") if isinstance(payload, dict) else None
    declared = (source or {}).get("channel_contract") if isinstance(source, dict) else None
    return declared if isinstance(declared, dict) else {}


def _dispatch_activation_task(job_id: str | None) -> None:
    """Ask Cloud Tasks to run an activation job whose row is ALREADY committed.

    Story 56.3 (AD-36). Both call sites in this module enqueue inside their own
    transaction, on purpose: the job row must commit atomically with the state
    change that justifies it. The push task therefore cannot be created there --
    it would address a row that has not committed and may never commit. It is
    created here, after the commit, where the caller knows the write survived.

    Never raises and never blocks the response: the row is the ledger, so a
    dispatch that fails costs latency, not the operation -- the reconciliation
    sweep (story 56.4) re-dispatches anything left queued without a live task.
    """
    if not job_id:
        return
    try:
        from core.queue import dispatch_activation_task

        dispatch_activation_task(job_id)
    except Exception as exc:  # noqa: BLE001 -- see docstring: latency, not loss
        logger.warning(
            "activation dispatch skipped for job_id=%s: %s -- the row is committed "
            "and the reconciliation sweep will pick it up",
            job_id,
            exc,
        )


def _response(payload: Any, status: int = 200) -> Response:
    response = JSONResponse(payload, status_code=status)
    response.headers["Cache-Control"] = "no-store"
    return response


def _not_found() -> Response:
    return _response({"code": "not_found", "message": "Resource not found"}, 404)


async def _body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw.strip():
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise PreconfigurationValidationError("JSON body must be an object")
    return value


async def _authorize(request: Request, capability: str) -> str | Response:
    from core.admin_api import _check_auth, _strict_project_capability_allowed
    from core.db import get_connection

    authorized, identity = await _check_auth(request)
    if not authorized:
        return _response({"code": "unauthorized", "message": "Bearer token required"}, 401)
    actor = identity or "anonymous"
    try:
        with get_connection() as conn:
            allowed = _strict_project_capability_allowed(
                conn,
                identity=actor,
                project_id=request.path_params["project_id"],
                minimum_capability=capability,
                hold_access=capability == "edit",
            )
    except Exception:
        # NE PAS CONFONDRE << refuse >> ET << pas pu evaluer >>.
        #
        # Toute exception rendait 404 ici : une panne de base, un timeout, une
        # colonne manquante devenaient << ca n'existe pas >>, indiscernables d'un
        # refus de droits legitime -- et sans une ligne de journal. La personne
        # voit disparaitre un objet qui existe, et personne ne peut le
        # contredire.
        #
        # Le 404 de NON-DIVULGATION reste, lui, intact : c'est la ligne suivante,
        # et elle est deliberee. Ce qui change est le cas ou l'on n'a pas su
        # repondre -- il se declare comme tel.
        logger.exception(
            "datastream_preconfiguration: capability_check_failed project=%s capability=%s",
            request.path_params.get("project_id"),
            capability,
        )
        return _response(
            {"code": "preconfiguration_unavailable", "message": "Datastream setup is unavailable"},
            503,
        )
    return actor if allowed else _not_found()


def _key(request: Request) -> str | Response:
    value = (request.headers.get("Idempotency-Key") or "").strip()
    if not value:
        return _response(
            {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"}, 422
        )
    if len(value) > 200:
        return _response(
            {"code": "invalid_idempotency_key", "message": "Idempotency-Key is too long"}, 422
        )
    return value


#: Contraintes d'unicite dont la violation est un CHOIX de la personne, pas une
#: panne : chacune nomme le champ a changer. Sans cela elles tombaient dans le
#: fourre-tout, et « Datastream setup is unavailable » etait la reponse rendue a
#: quelqu'un dont le seul tort etait d'avoir repris un nom deja pris.
_OPERATOR_UNIQUE_CONSTRAINTS = {
    "uq_datastreams_project_name": (
        "name_taken",
        "This Project already has a Datastream with that name. Choose another name.",
    ),
}


def _error(exc: Exception) -> Response:
    constraint = getattr(getattr(exc, "diag", None), "constraint_name", None)
    if constraint in _OPERATOR_UNIQUE_CONSTRAINTS:
        code, message = _OPERATOR_UNIQUE_CONSTRAINTS[constraint]
        return _response({"code": code, "message": message}, 409)
    if isinstance(exc, (PreconfigurationNotFound, ObservationNotFound, SetupAssetNotFound)):
        return _not_found()
    if isinstance(exc, (PreconfigurationConflict, ObservationConflict)):
        return _response({"code": exc.code, "message": str(exc)}, 409)
    if isinstance(exc, ObservationUnavailable):
        return _response({"code": exc.code, "message": str(exc)}, 503)
    if isinstance(
        exc,
        (
            PreconfigurationValidationError,
            ObservationValidationError,
            SetupAssetValidationError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ),
    ):
        return _response({"code": "invalid_request", "message": str(exc)}, 422)
    # LE FOURRE-TOUT JOURNALISE, MAINTENANT.
    #
    # Il rendait ce 503 sans laisser la moindre trace : le 2026-08-04, la console
    # de Jean affichait << Datastream setup is unavailable >> pendant que
    # `PATCH .../datastream-setup-draft` echouait en boucle, et les journaux
    # Cloud Run ne portaient QUE la ligne HTTP -- aucun message applicatif. Ni
    # lui ni moi ne pouvions savoir pourquoi. Une exception non cartographiee
    # avalee en silence rend le defaut indiagnosticable par construction.
    #
    # `exception()` porte la trace complete. Le corps de la reponse, lui, ne
    # change pas : ce qui va au client reste volontairement muet.
    # `exc_info=exc` et NON `logger.exception()` : cette derniere ne trace que
    # l'exception COURANTE. `_error` recoit la sienne en argument et n'est pas
    # toujours appelee depuis un `except` -- la trace aurait alors ete
    # << NoneType: None >>, c'est-a-dire le meme silence sous un autre nom.
    logger.error(
        "datastream_preconfiguration: unmapped_error %s", type(exc).__name__, exc_info=exc
    )
    return _response(
        {"code": "preconfiguration_unavailable", "message": "Datastream setup is unavailable"}, 503
    )


async def _create(request: Request) -> Response:
    actor = await _authorize(request, "edit")
    if isinstance(actor, Response):
        return actor
    key = _key(request)
    if isinstance(key, Response):
        return key
    try:
        if await _body(request):
            raise PreconfigurationValidationError("Create body must be empty")
        from core.db import get_connection

        with get_connection() as conn:
            result = create_or_resume_draft(
                conn, project_id=request.path_params["project_id"], actor=actor, idempotency_key=key
            )
            conn.commit()
        return _response(result, 200 if result["idempotent_replay"] else 201)
    except Exception as exc:
        return _error(exc)


async def _list(request: Request) -> Response:
    actor = await _authorize(request, "view")
    if isinstance(actor, Response):
        return actor
    try:
        from core.db import get_connection

        with get_connection() as conn:
            return _response(
                {"drafts": list_drafts(conn, project_id=request.path_params["project_id"])}
            )
    except Exception as exc:
        return _error(exc)


async def _read(request: Request) -> Response:
    actor = await _authorize(request, "view")
    if isinstance(actor, Response):
        return actor
    try:
        from core.db import get_connection

        with get_connection() as conn:
            return _response(
                read_draft(
                    conn,
                    project_id=request.path_params["project_id"],
                    draft_id=request.path_params["draft_id"],
                )
            )
    except Exception as exc:
        return _error(exc)


async def _update(request: Request) -> Response:
    actor = await _authorize(request, "edit")
    if isinstance(actor, Response):
        return actor
    key = _key(request)
    if isinstance(key, Response):
        return key
    try:
        body = await _body(request)
        if not set(body).issubset({"expected_revision", "operator_input", "change_reason"}):
            raise PreconfigurationValidationError("Unsupported draft fields")
        if not isinstance(body.get("expected_revision"), int) or not isinstance(
            body.get("operator_input"), dict
        ):
            raise PreconfigurationValidationError(
                "expected_revision and operator_input are required"
            )
        from core.db import get_connection

        with get_connection() as conn:
            try:
                result = update_draft(
                    conn,
                    project_id=request.path_params["project_id"],
                    draft_id=request.path_params["draft_id"],
                    actor=actor,
                    idempotency_key=key,
                    expected_revision=body["expected_revision"],
                    operator_input=body["operator_input"],
                    change_reason=str(body.get("change_reason") or "autosave"),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return _response(result, 200)
    except Exception as exc:
        return _error(exc)


async def _discard(request: Request) -> Response:
    """`Discard this draft` — the terminal path, ratified by Jean 2026-08-31 (AI-336).

    `DELETE` on the draft resource, and a soft archive behind it: exactly the
    shape `DELETE /api/datastreams/{id}` already has for a Datastream, so one
    verb does not mean two different things on the same surface. No
    `Idempotency-Key` is demanded, for the same reason the template retirement
    does not demand one: the gesture is idempotent by construction, and a second
    discard is answered as the same discard.
    """
    actor = await _authorize(request, "edit")
    if isinstance(actor, Response):
        return actor
    try:
        from core.db import get_connection

        with get_connection() as conn:
            try:
                result = discard_draft(
                    conn,
                    project_id=request.path_params["project_id"],
                    draft_id=request.path_params["draft_id"],
                    actor=actor,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return _response(result, 200)
    except Exception as exc:
        return _error(exc)


async def _compile(request: Request) -> Response:
    actor = await _authorize(request, "edit")
    if isinstance(actor, Response):
        return actor
    key = _key(request)
    if isinstance(key, Response):
        return key
    try:
        body = await _body(request)
        if set(body) != {"expected_revision"} or not isinstance(body["expected_revision"], int):
            raise PreconfigurationValidationError("Compile requires exact expected_revision")
        from core.db import get_connection

        with get_connection() as conn:
            try:
                result = compile_draft(
                    conn,
                    project_id=request.path_params["project_id"],
                    draft_id=request.path_params["draft_id"],
                    actor=actor,
                    idempotency_key=key,
                    expected_revision=body["expected_revision"],
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return _response(result, 200 if result["idempotent_replay"] else 201)
    except Exception as exc:
        return _error(exc)


async def _proposal(request: Request) -> Response:
    actor = await _authorize(request, "view")
    if isinstance(actor, Response):
        return actor
    try:
        from core.db import get_connection

        with get_connection() as conn:
            return _response(
                read_proposal(
                    conn,
                    project_id=request.path_params["project_id"],
                    draft_id=request.path_params["draft_id"],
                    proposal_id=request.path_params["proposal_id"],
                )
            )
    except Exception as exc:
        return _error(exc)


async def _source_options(request: Request) -> Response:
    actor = await _authorize(request, "view")
    if isinstance(actor, Response):
        return actor
    try:
        from core.db import get_connection

        with get_connection() as conn:
            return _response(
                get_source_options(
                    conn,
                    project_id=request.path_params["project_id"],
                    draft_id=request.path_params["draft_id"],
                    actor=actor,
                )
            )
    except Exception as exc:
        return _error(exc)


async def _create_asset(request: Request) -> Response:
    actor = await _authorize(request, "edit")
    if isinstance(actor, Response):
        return actor
    key = _key(request)
    if isinstance(key, Response):
        return key
    try:
        filename = (request.headers.get("X-File-Name") or "").strip()
        declared_length = request.headers.get("Content-Length")
        if declared_length and int(declared_length) > 25 * 1024 * 1024:
            raise SetupAssetValidationError("The staged file exceeds the 25 MiB limit")
        data = await request.body()
        from core.db import get_connection

        with get_connection() as conn:
            try:
                result = stage_setup_asset(
                    conn,
                    project_id=request.path_params["project_id"],
                    draft_id=request.path_params["draft_id"],
                    actor=actor,
                    filename=filename,
                    content_type=request.headers.get("Content-Type"),
                    data=data,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return _response(result, 200 if result["idempotent_replay"] else 201)
    except Exception as exc:
        return _error(exc)


async def _create_observation(request: Request) -> Response:
    actor = await _authorize(request, "edit")
    if isinstance(actor, Response):
        return actor
    key = _key(request)
    if isinstance(key, Response):
        return key
    try:
        body = await _body(request)
        expected_revision = body.get("expected_revision")
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
            raise ObservationValidationError("expected_revision is required")
        from core.db import get_connection

        with get_connection() as conn:
            try:
                setup_adapter = None
                if (
                    body.get("mode") == "managed_feed"
                    and body.get("discovery_kind") == "file_schema"
                    and body.get("channel") in ("inbound_email", "webhook")
                ):
                    # AI-113: the first DELIVERED file reveals the shape.
                    #
                    # An uploaded file is staged, so the branch below can look it
                    # up by `staged_asset_ref`. A delivered file was staged by
                    # nobody -- it arrived, and it is identified by its content
                    # address in `app.inbound_raw_imports`. Same observation
                    # payload, same normalisation, same review screen: the wizard
                    # cannot tell which channel produced the shape, and it must
                    # not be able to.
                    from core.inbound_discovery import (  # noqa: PLC0415
                        observe_first_delivery,
                    )

                    def setup_adapter(adapter_request: dict[str, Any]) -> dict[str, Any]:
                        datastream_id = str(
                            adapter_request.get("source_account_ref") or ""
                        ).strip()
                        if not datastream_id:
                            # The draft has no inbound Datastream bound yet, so
                            # there is nothing to have received anything. Said
                            # as a coded exception rather than an error: this is
                            # the ordinary state before an address is issued.
                            return {
                                "adapter_ref": (
                                    "managed_feed.received_file.readonly.v1"
                                ),
                                "safe_metadata": {"fields": []},
                                "coverage": {
                                    "schema": "unavailable",
                                    "rows": "unavailable",
                                },
                                "exceptions": [{"code": "no_inbound_datastream_bound"}],
                            }
                        return observe_first_delivery(
                            conn, datastream_id=datastream_id
                        )

                elif (
                    body.get("mode") == "external_bq"
                    and body.get("discovery_kind") == "warehouse_schema"
                ):
                    # Story 57.1: the pair that was declared uncovered until the
                    # typed client existed. It is built HERE, inside the request,
                    # for the same reason the two branches around it are: an
                    # adapter is a closure, and a process-global one would serve
                    # the next project's request.
                    observe_external_bigquery = resolve_inbound("observe_external_bigquery")

                    def setup_adapter(adapter_request: dict[str, Any]) -> dict[str, Any]:
                        # L'AUTORISATION DE LA PERSONNE, pas celle du deploiement
                        # (AI-285). BigQuery est une source Google : elle se lit
                        # par le consentement Google, comme Sheets deux branches
                        # plus bas et comme tout le reste de la pile. Sans compte
                        # nomme, le lecteur retombe sur les identifiants du
                        # deploiement -- le cas auto-heberge, jamais le parcours
                        # d'un client.
                        source_account_ref = str(
                            adapter_request.get("source_account_ref") or ""
                        )
                        connection_id = (
                            _source_account_connection_id(
                                conn,
                                project_id=request.path_params["project_id"],
                                source_account_ref=source_account_ref,
                                source_label="External BigQuery",
                            )
                            if source_account_ref
                            else None
                        )
                        reader = _bigquery_setup_reader(connection_id)
                        try:
                            return observe_external_bigquery(adapter_request, client=reader)
                        except Exception as exc:
                            # NAMED, and told apart from an empty warehouse. An
                            # object whose schema is empty is an honest emptiness
                            # the adapter reports as `schema_unavailable`; this is
                            # the other case -- the read itself failed, nothing was
                            # saved, and the adapter is named so the operator is
                            # not left guessing which half broke.
                            raise ObservationUnavailable(
                                "The External BigQuery discovery adapter "
                                "(external_bq.readonly.v1) could not read this object. "
                                "Nothing was saved."
                            ) from exc

                elif (
                    body.get("mode") == "managed_feed"
                    and body.get("discovery_kind") == "channel_contract"
                ):
                    # Story 57.3, and the ONE pair with nothing to interrogate.
                    # An email address and a webhook token cannot be listed:
                    # there is no provider at the other end, and until a sender
                    # sends there is nothing at all. So the evidence is the
                    # operator's DECLARATION read back against the state of the
                    # deployment -- a contract, not an observation of a third
                    # party. Built HERE like the three branches around it,
                    # because it closes over this request's connection.
                    observe_channel_contract = resolve_inbound("observe_channel_contract")

                    def setup_adapter(adapter_request: dict[str, Any]) -> dict[str, Any]:
                        from core.inbound_discovery import (  # noqa: PLC0415
                            read_channel_contract,
                        )

                        project_id = request.path_params["project_id"]
                        try:
                            state = read_channel_contract(
                                conn,
                                project_id=project_id,
                                channel=str(adapter_request.get("channel") or ""),
                                declared=_declared_channel_contract(
                                    conn,
                                    project_id=project_id,
                                    draft_id=request.path_params["draft_id"],
                                ),
                                template_ref=adapter_request.get("template_ref"),
                            )
                        except Exception as exc:
                            # NAMED, and told apart from the emptinesses this
                            # channel has -- no verified domain, no address yet,
                            # no arrival declared and no delivery received are
                            # all coded exceptions on a SUCCESSFUL read. This is
                            # the other case: the state could not be read at all.
                            raise ObservationUnavailable(
                                "The inbound channel discovery adapter "
                                "(managed_feed.channel_contract.v1) could not read this "
                                "channel's state. Nothing was saved."
                            ) from exc
                        return observe_channel_contract(
                            adapter_request, channel_state=state
                        )

                elif (
                    body.get("mode") == "managed_feed"
                    and body.get("discovery_kind") == "sheet_schema"
                ):
                    # Story 57.2, the pair declared uncovered until a production
                    # class implemented `SheetsSetupClient`. Built HERE, inside
                    # the request, like the three branches around it: an adapter
                    # is a closure over this request's connection, and a
                    # process-global one would serve the next project's request.
                    observe_google_sheet = resolve_inbound("observe_google_sheet")

                    def setup_adapter(adapter_request: dict[str, Any]) -> dict[str, Any]:
                        connection_id = _source_account_connection_id(
                            conn,
                            project_id=request.path_params["project_id"],
                            source_account_ref=str(
                                adapter_request.get("source_account_ref") or ""
                            ),
                            source_label="Google Sheets",
                        )
                        reader = _sheets_setup_reader(connection_id)
                        try:
                            return observe_google_sheet(adapter_request, client=reader)
                        except Exception as exc:
                            # NAMED, and told apart from the three honest
                            # emptinesses this channel has -- an unnamed tab, a
                            # missing tab and an unusable header row are all
                            # reported as coded exceptions on a successful read.
                            # This is the other case: the read itself failed and
                            # nothing was saved.
                            raise ObservationUnavailable(
                                "The Google Sheets discovery adapter "
                                "(managed_feed.google_sheets.readonly.v1) could not read "
                                "this sheet. Nothing was saved."
                            ) from exc

                elif (
                    body.get("mode") == "managed_feed"
                    and body.get("discovery_kind") == "file_schema"
                ):
                    observe_staged_file = resolve_inbound("observe_staged_file")

                    def setup_adapter(adapter_request: dict[str, Any]) -> dict[str, Any]:
                        with conn.cursor() as cur:
                            cur.execute(
                                "SELECT storage_ref,detected_format "
                                "FROM app.datastream_setup_assets "
                                "WHERE id=%s AND draft_id=%s AND project_id=%s "
                                "AND state='available' AND expires_at>NOW()",
                                (
                                    adapter_request.get("staged_asset_ref"),
                                    request.path_params["draft_id"],
                                    request.path_params["project_id"],
                                ),
                            )
                            asset = cur.fetchone()
                        if asset is None:
                            raise SetupAssetNotFound("Staged asset not found")
                        return observe_staged_file(
                            adapter_request,
                            asset_loader=lambda _asset_ref: load_setup_asset(asset[0], asset[1]),
                        )

                result = create_observation(
                    conn,
                    project_id=request.path_params["project_id"],
                    draft_id=request.path_params["draft_id"],
                    actor=actor,
                    idempotency_key=key,
                    expected_revision=expected_revision,
                    request={
                        name: value for name, value in body.items() if name != "expected_revision"
                    },
                    adapter=setup_adapter,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return _response(result, 200 if result["idempotent_replay"] else 201)
    except Exception as exc:
        return _error(exc)


async def _read_observation(request: Request) -> Response:
    actor = await _authorize(request, "view")
    if isinstance(actor, Response):
        return actor
    try:
        from core.db import get_connection

        with get_connection() as conn:
            return _response(
                read_observation(
                    conn,
                    project_id=request.path_params["project_id"],
                    draft_id=request.path_params["draft_id"],
                    observation_id=request.path_params["observation_id"],
                )
            )
    except Exception as exc:
        return _error(exc)


def _activation_error(exc: Exception) -> Response:
    # A PLAN REFUSAL IS NOT AN OUTAGE. Unmapped, the trial cap fell through
    # `_error` into its mute 503 "Datastream setup is unavailable" -- the wizard
    # would tell a trial org its setup was broken, when the truth is that it has
    # a Datastream too many and knows the gesture that frees one. Same 409 and
    # same body as the creation seam (`datastreams_api.py:215-219`), so one
    # refusal reads identically wherever the person meets it.
    from core.trial_enforcement import TrialDatastreamLimitError  # noqa: PLC0415

    if isinstance(exc, TrialDatastreamLimitError):
        return _response(exc.to_dict(), 409)
    if isinstance(exc, EntryConfirmationRefused):
        return _response({"code": exc.code, "message": "Confirmation was refused"}, 409)
    if isinstance(exc, (ActivationValidationError, EntryConfirmationValidationError)):
        return _response(
            {"code": getattr(exc, "code", "invalid_request"), "message": str(exc)}, 422
        )
    return _error(exc)


def _preview_pins(proposal: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    dependencies = proposal.get("dependency_snapshot") or {}
    project_configuration = dependencies.get("project_configuration") or {}
    if not project_configuration.get("version_id"):
        raise ActivationValidationError("An active Project configuration version is required")
    section_hashes = proposal.get("section_fingerprints") or {}
    pins = {
        "draft_revision_id": proposal["draft_revision_ref"],
        "proposal_id": proposal["proposal_ref"],
        "observation_id": observation["observation_ref"],
        "connector_contract_version_id": observation.get("connector_contract_version_ref")
        or "not_applicable",
        "project_configuration_version_id": project_configuration["version_id"],
        "capability_version_ids": sorted(
            str(item["active_version_id"])
            for item in dependencies.get("capabilities") or []
            if item.get("active_version_id")
        ),
        "mapping_hash": section_hashes.get("physical_mapping"),
        "processing_hash": section_hashes.get("processing"),
    }
    if any(not pins.get(key) for key in ("mapping_hash", "processing_hash")):
        raise ActivationValidationError("Proposal mapping or processing evidence is incomplete")
    return pins


async def _create_preview(request: Request) -> Response:
    actor = await _authorize(request, "edit")
    if isinstance(actor, Response):
        return actor
    key = _key(request)
    if isinstance(key, Response):
        return key
    try:
        body = await _body(request)
        if set(body) != {"expected_revision", "proposal_ref", "observation_ref"}:
            raise ActivationValidationError("Preview requires exact persisted references")
        from core.db import get_connection
        from core.queue import enqueue_activation_work

        project_id = request.path_params["project_id"]
        draft_id = request.path_params["draft_id"]
        with get_connection() as conn:
            draft = read_draft(conn, project_id=project_id, draft_id=draft_id)
            if draft["current_revision"] != body["expected_revision"]:
                raise ActivationValidationError("Draft revision changed before preview")
            proposal = read_proposal(
                conn, project_id=project_id, draft_id=draft_id, proposal_id=body["proposal_ref"]
            )
            observation = read_observation(
                conn,
                project_id=project_id,
                draft_id=draft_id,
                observation_id=body["observation_ref"],
            )
            if (
                proposal.get("is_stale")
                or observation["draft_revision_ref"] != proposal["draft_revision_ref"]
            ):
                raise ActivationValidationError("Preview references are stale or incompatible")
            pins = _preview_pins(proposal, observation)
            # ESTIMATE BEFORE SCAN, and this is where the refusal stands.
            #
            # A preview of an external warehouse is the first read the operator can
            # launch, and BigQuery bills bytes scanned. The estimate rides with the
            # observation (`safe_metadata.quota_cost`, from a dry-run job that is
            # planned and not billed); without it the cost of this click is unknown,
            # so the click is refused rather than taken. The step-5 button is
            # disabled on the same criterion, so the refusal reads BEFORE the click.
            operator_input = draft.get("operator_input") or {}
            if operator_input.get("mode") == "external_bq" and not (
                observation.get("safe_metadata") or {}
            ).get("quota_cost"):
                raise ActivationValidationError(
                    "No scan estimate is attached to this observation, so the read cannot "
                    "be launched. Re-run Discover source."
                )
            channel = (operator_input.get("source") or {}).get("channel")
            queued = enqueue_activation_work(
                kind="setup_preview",
                project_id=project_id,
                draft_id=draft_id,
                correlation_id=pins["draft_revision_id"],
                payload={
                    "proposal_id": proposal["proposal_ref"],
                    "observation_id": observation["observation_ref"],
                    "pins": pins,
                    "channel": channel,
                },
                requested_by=actor,
                conn=conn,
            )
            conn.commit()
        # Story 56.3 (AD-36): the push task is dispatched AFTER the commit, never
        # from inside the enqueue -- a task addressed at an uncommitted row would
        # arrive to find nothing, or arrive for work that was rolled back. No-op
        # under QUEUE_BACKEND=local, where the polling worker is the dispatcher.
        _dispatch_activation_task(queued["job_id"])
        return _response(
            {
                **queued,
                "draft_ref": draft_id,
                "dependency_hash": proposal["dependency_fingerprint"],
            },
            202,
        )
    except Exception as exc:
        return _activation_error(exc)


async def _read_preview_job(request: Request) -> Response:
    actor = await _authorize(request, "view")
    if isinstance(actor, Response):
        return actor
    try:
        from core.db import get_connection

        project_id = request.path_params["project_id"]
        draft_id = request.path_params["draft_id"]
        job_id = request.path_params["job_id"]
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT state,error_code,correlation_id FROM app.datastream_activation_jobs "
                "WHERE id=%s AND kind='setup_preview' AND project_id=%s AND draft_id=%s",
                (job_id, project_id, draft_id),
            )
            job = cur.fetchone()
            if job is None:
                return _not_found()
            result: dict[str, Any] = {"job_ref": job_id, "state": job[0], "error_code": job[1]}
            if job[0] == "done":
                key_hash = _preview_idempotency_hash(str(job[2]))
                cur.execute(
                    "SELECT id FROM app.datastream_setup_previews "
                    "WHERE project_id=%s AND draft_id=%s AND idempotency_key_hash=%s",
                    (project_id, draft_id, key_hash),
                )
                preview_row = cur.fetchone()
                if preview_row is None:
                    raise ActivationValidationError("Completed preview job has no durable evidence")
                result["preview"] = read_preview(
                    conn, project_id=project_id, draft_id=draft_id, preview_id=preview_row[0]
                )
            return _response(result)
    except Exception as exc:
        return _activation_error(exc)


def _load_connector_capabilities(
    conn, *, project_id: str, operator_input: dict[str, Any]
) -> dict[str, Any] | None:
    if operator_input.get("mode") != "connector_pull":
        return None
    source = operator_input.get("source") or {}
    from core.account_topology import account_connector_sql  # noqa: PLC0415

    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT ca.credential_id
                 FROM app.credential_accounts ca
                 JOIN app.connection_ref cr ON cr.id=ca.credential_id
                 JOIN app.projects p ON p.id=%s AND p.org_id=cr.owner_org_id
                WHERE ca.source_account_id=%s AND ca.available=TRUE
                  AND {account_connector_sql()}""",  # noqa: S608 -- constant predicate
            (project_id, source.get("source_account_ref"), [source.get("connector_ref")]),
        )
        connection = cur.fetchone()
        cur.execute(
            "SELECT contract_snapshot FROM app.connector_contract_versions "
            "WHERE id=%s AND connector_id=%s",
            (source.get("connector_contract_version_ref"), source.get("connector_ref")),
        )
        contract = cur.fetchone()
    if connection is None or contract is None:
        raise ActivationValidationError("Reviewed Connector scope is no longer available")
    from core.source_capabilities import normalize_capabilities

    snapshot = contract[0] if isinstance(contract[0], dict) else json.loads(contract[0])
    manifest = snapshot.get("contract") if isinstance(snapshot.get("contract"), dict) else snapshot

    # LE MEME ECART QU'A LA COMPILATION, UN CRAN PLUS BAS. Cette ligne
    # `connector_contract_versions` est l'epingle ecrite a la premiere liaison ;
    # un module qui gagne un rapport le voit offert par le catalogue, accepte au
    # compile (corrige) -- et refuse ICI, a la revue finale, sur « Reviewed
    # Connector contract is unavailable ». Mesure 2026-08-12 : sept rapports sur
    # neuf mouraient a cette etape apres avoir passe toutes les precedentes.
    #
    # Quand l'epingle ignore le rapport choisi et que le module le declare
    # AUJOURD'HUI, le module gagne : c'est lui qui sera appele. La connexion
    # reste celle du compte choisi -- seule la definition du contrat change.
    report_ref = str(source.get("report_ref") or "")
    reports = ((manifest.get("source_capabilities") or {}).get("reports")) or []
    if report_ref and not any(item.get("id") == report_ref for item in reports):
        from core.context_seed import load_registry_entry  # noqa: PLC0415

        entry = load_registry_entry(str(source.get("connector_ref") or "")) or {}
        live = entry.get("manifest") if isinstance(entry, dict) else None
        live_reports = (
            ((live or {}).get("source_capabilities") or {}).get("reports")
        ) or []
        if any(item.get("id") == report_ref for item in live_reports):
            manifest = live

    capabilities = normalize_capabilities(
        manifest, project_id=project_id, connection_ref_id=connection[0]
    )
    import hashlib

    capabilities["capability_fingerprint"] = hashlib.sha256(
        json.dumps(capabilities, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return capabilities


def freeze_and_persist_final_review(
    conn,
    *,
    project_id: str,
    draft_id: str,
    preview_id: str,
    acknowledged_warning_ids: list[str],
    actor: str,
) -> dict[str, Any]:
    """Geler la revue finale d'un brouillon et la persister. LE seul compositeur.

    EXTRAIT DU HANDLER REST LE 2026-08-22 (story 67.23), et extrait plutot que
    recopie : la porte MCP du parcours gouverne doit composer la MEME revue, et
    une seconde composition serait un second produit. C'est la regle de
    `run_refetch` le meme jour -- un second moteur derriere une seconde porte est
    la faute, pas la reparation.

    CE QU'ELLE REFUSE N'EST PAS NEGOCIABLE PAR L'APPELANT, parce que le refus vit
    dans `freeze_final_review` et non ici : un item BLOQUANT ne se contourne pas
    (<< Blocking review items cannot be acknowledged away >>) et CHAQUE
    avertissement doit etre nomme (<< Every warning requires an explicit
    acknowledgement >>). Aucune porte ne peut assouplir l'un ou l'autre.

    Le fuseau de PLANIFICATION est lu sur la version de Configuration active, et
    son absence est ETIQUETEE plutot que remplie : `scheduling_default_unconfirmed`
    dit qu'UTC est l'horloge sur laquelle on tourne, pas une frontiere de
    restitution que ce Projet aurait confirmee.

    La connexion est celle de l'appelant : cette fonction ne commite pas, parce
    que les deux portes ont chacune leur transaction.
    """
    preview = read_preview(
        conn, project_id=project_id, draft_id=draft_id, preview_id=preview_id
    )
    proposal = read_proposal(
        conn,
        project_id=project_id,
        draft_id=draft_id,
        proposal_id=preview["proposal_ref"],
    )
    observation = read_observation(
        conn,
        project_id=project_id,
        draft_id=draft_id,
        observation_id=preview["observation_ref"],
    )
    draft = read_draft(conn, project_id=project_id, draft_id=draft_id)
    if preview["is_stale"] or proposal["is_stale"]:
        raise ActivationValidationError("Reviewed evidence changed before final review")
    with conn.cursor() as cur:
        # Story 48.3: this used to be COALESCE(pp.reporting_timezone,'UTC').
        # Two things were wrong with one expression. The value drives the
        # SCHEDULE (when the job runs), and reading it from the *reporting*
        # preference conflated two concepts; and the COALESCE meant an
        # unconfirmed Project silently got UTC, which reads downstream as a
        # decided reporting boundary. The confirmed value is now read from the
        # active Configuration Version -- the one authority -- and its absence
        # is recorded as such rather than filled in.
        cur.execute(
            """SELECT p.org_id,
                      cv.posture #>> '{defaults,reporting_timezone}'
                 FROM app.projects p
            LEFT JOIN app.project_configuration_versions cv
                   ON cv.project_id = p.id
                  AND cv.id = p.active_configuration_version_id
                WHERE p.id = %s""",
            (project_id,),
        )
        project = cur.fetchone()
    if project is None:
    # UNE LEVEE, PAS UNE `Response`. Cette fonction a deux appelants dont l'un
    # n'est pas HTTP : rendre un objet Starlette a la porte MCP lui donnerait
    # un 404 a serialiser en resultat d'outil. Le refus voyage en exception,
    # et chaque porte le traduit dans SA langue.
        raise PreconfigurationNotFound("Project not found")
    # No confirmed reporting timezone: the scheduler still needs a clock, and
    # UTC is the honest one to run on -- but it is labelled as a SCHEDULING
    # default here rather than presented as this Project's reporting boundary.
    schedule_timezone = project[1] or "UTC"
    schedule_timezone_origin = (
        "project_configuration" if project[1] else "scheduling_default_unconfirmed"
    )
    frozen = freeze_final_review(
        project_id=project_id,
        draft_id=draft_id,
        proposal=proposal,
        preview=preview,
        acknowledged_warning_ids=[str(value) for value in acknowledged_warning_ids],
    )
    contract = compile_materialization_contract(
        frozen_review=frozen,
        operator_input=draft.get("operator_input") or {},
        observation=observation,
        org_id=project[0],
        actor=actor,
        timezone_name=schedule_timezone,
        timezone_origin=schedule_timezone_origin,
        connector_capabilities=_load_connector_capabilities(
            conn, project_id=project_id, operator_input=draft.get("operator_input") or {}
        ),
    )
    result = persist_final_review(
        conn,
        project_id=project_id,
        draft_id=draft_id,
        preview_id=preview["preview_ref"],
        actor=actor,
        snapshot=contract,
    )
    return result


async def _prepare_final_review(request: Request) -> Response:
    actor = await _authorize(request, "edit")
    if isinstance(actor, Response):
        return actor
    key = _key(request)
    if isinstance(key, Response):
        return key
    try:
        body = await _body(request)
        if set(body) != {"preview_ref", "acknowledged_warning_ids"} or not isinstance(
            body["acknowledged_warning_ids"], list
        ):
            raise ActivationValidationError(
                "Final review requires preview and warning acknowledgements"
            )
        from core.db import get_connection

        project_id = request.path_params["project_id"]
        draft_id = request.path_params["draft_id"]
        with get_connection() as conn:
            result = freeze_and_persist_final_review(
                conn,
                project_id=project_id,
                draft_id=draft_id,
                preview_id=body["preview_ref"],
                acknowledged_warning_ids=[
                    str(value) for value in body["acknowledged_warning_ids"]
                ],
                actor=actor,
            )
            conn.commit()
        return _response(result, 200 if result["idempotent_replay"] else 201)
    except Exception as exc:
        return _activation_error(exc)


async def _prepare_draft_confirmation(request: Request) -> Response:
    actor = await _authorize(request, "edit")
    if isinstance(actor, Response):
        return actor
    key = _key(request)
    if isinstance(key, Response):
        return key
    try:
        body = await _body(request)
        if set(body) != {"final_review_ref"}:
            raise ActivationValidationError("Confirmation requires one final review reference")
        from core.db import get_connection

        project_id = request.path_params["project_id"]
        draft_id = request.path_params["draft_id"]
        with get_connection() as conn:
            review = read_final_review(
                conn,
                project_id=project_id,
                draft_id=draft_id,
                final_review_id=body["final_review_ref"],
            )
            if review["is_stale"]:
                raise ActivationValidationError("Final review changed before confirmation")
            issued = prepare_confirmation(
                conn,
                command_type=DATASTREAM_SETUP_CREATE_DRAFT_COMMAND,
                actor_person_id=actor,
                project_id=project_id,
                resource_id=body["final_review_ref"],
                content_hash=review["content_hash"],
                idempotency_key=key,
            )
            # `get_connection` closes without committing and psycopg rolls back:
            # without this the caller gets a 201 and a raw secret for a row that
            # never landed, and materialization answers `confirmation_invalid`.
            conn.commit()
        return _response(
            {
                "confirmation_ref": issued.confirmation_id,
                "confirmation_secret": issued.confirmation_secret,
                "command": issued.command_type,
                "content_hash": issued.payload_hash,
                "expires_at": issued.expires_at.isoformat(),
            },
            201,
        )
    except Exception as exc:
        return _activation_error(exc)


async def _confirm_create_draft(request: Request) -> Response:
    actor = await _authorize(request, "edit")
    if isinstance(actor, Response):
        return actor
    key = _key(request)
    if isinstance(key, Response):
        return key
    try:
        body = await _body(request)
        if set(body) != {"final_review_ref", "confirmation_ref", "confirmation_secret"}:
            raise ActivationValidationError("Draft confirmation payload is invalid")
        from core.db import get_connection

        project_id = request.path_params["project_id"]
        draft_id = request.path_params["draft_id"]
        with get_connection() as conn:
            review = read_final_review(
                conn,
                project_id=project_id,
                draft_id=draft_id,
                final_review_id=body["final_review_ref"],
            )
            if review["is_stale"]:
                raise ActivationValidationError("Final review changed before materialization")
            exact = {**review, "final_review_id": body["final_review_ref"]}
            operation = execute_confirmed_operation(
                conn,
                command_type=DATASTREAM_SETUP_CREATE_DRAFT_COMMAND,
                actor_person_id=actor,
                actor=actor,
                org_id=review["org_id"],
                project_id=project_id,
                resource_id=body["final_review_ref"],
                content_hash=review["content_hash"],
                idempotency_key=key,
                confirmation_id=body["confirmation_ref"],
                confirmation_secret=body["confirmation_secret"],
                mutation=lambda active_conn, operation_id: materialize_draft_mutation(
                    active_conn, operation_id=operation_id, review=exact, actor=actor
                ),
            )
            # `conn.transaction()` inside is a SAVEPOINT here, not a transaction:
            # the review was READ first and a SELECT opens the transaction, so
            # releasing the savepoint commits nothing and `get_connection` rolls
            # the whole materialization back. Measured: 201 returned, zero rows.
            conn.commit()
        # Story 56.3: `execute_confirmed_operation` runs under `conn.transaction()`,
        # so the candidate job row is committed once it returns. Dispatching a
        # REPLAYED operation again is safe on purpose: the claim refuses anything
        # that is not claimable, so a duplicate task is a no-op -- while a first
        # dispatch that failed still gets its second chance here.
        _dispatch_activation_task((operation.result or {}).get("candidate_job_id"))
        return _response(
            {
                "operation_ref": operation.operation_id,
                "outcome": operation.outcome,
                **operation.result,
            },
            200 if operation.replayed else 201,
        )
    except Exception as exc:
        return _activation_error(exc)


async def _materialization_status(request: Request) -> Response:
    actor = await _authorize(request, "view")
    if isinstance(actor, Response):
        return actor
    try:
        from core.db import get_connection

        with get_connection() as conn:
            return _response(
                read_materialization_status(
                    conn,
                    project_id=request.path_params["project_id"],
                    draft_id=request.path_params["draft_id"],
                )
            )
    except Exception as exc:
        return _activation_error(exc)


async def _candidate_review(request: Request) -> Response:
    actor = await _authorize(request, "view")
    if isinstance(actor, Response):
        return actor
    try:
        from core.db import get_connection

        with get_connection() as conn:
            return _response(
                read_candidate_review(
                    conn,
                    project_id=request.path_params["project_id"],
                    datastream_id=request.path_params["datastream_id"],
                    execution_id=request.path_params["execution_id"],
                )
            )
    except Exception as exc:
        return _activation_error(exc)


async def _prepare_candidate_confirmation(request: Request) -> Response:
    actor = await _authorize(request, "edit")
    if isinstance(actor, Response):
        return actor
    key = _key(request)
    if isinstance(key, Response):
        return key
    try:
        if await _body(request):
            raise ActivationValidationError("Candidate confirmation body must be empty")
        from core.db import get_connection

        project_id = request.path_params["project_id"]
        datastream_id = request.path_params["datastream_id"]
        execution_id = request.path_params["execution_id"]
        with get_connection() as conn:
            review = read_candidate_review(
                conn, project_id=project_id, datastream_id=datastream_id, execution_id=execution_id
            )
            issued = prepare_confirmation(
                conn,
                command_type=DATASTREAM_CANDIDATE_PUBLISH_ACTIVATE_COMMAND,
                actor_person_id=actor,
                project_id=project_id,
                resource_id=execution_id,
                content_hash=review["review_hash"],
                idempotency_key=key,
            )
            # See `_prepare_draft_confirmation`: a secret returned on a rolled
            # back row is a lost write wearing a refusal's clothes.
            conn.commit()
        return _response(
            {
                "confirmation_ref": issued.confirmation_id,
                "confirmation_secret": issued.confirmation_secret,
                "command": issued.command_type,
                "review_hash": review["review_hash"],
                "expires_at": issued.expires_at.isoformat(),
            },
            201,
        )
    except Exception as exc:
        return _activation_error(exc)


async def _confirm_publish_activate(request: Request) -> Response:
    actor = await _authorize(request, "edit")
    if isinstance(actor, Response):
        return actor
    key = _key(request)
    if isinstance(key, Response):
        return key
    try:
        body = await _body(request)
        if set(body) != {"confirmation_ref", "confirmation_secret"}:
            raise ActivationValidationError("Publication confirmation payload is invalid")
        from core.db import get_connection

        project_id = request.path_params["project_id"]
        datastream_id = request.path_params["datastream_id"]
        execution_id = request.path_params["execution_id"]
        with get_connection() as conn:
            review = read_candidate_review(
                conn, project_id=project_id, datastream_id=datastream_id, execution_id=execution_id
            )
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT org_id FROM app.datastreams WHERE id=%s AND project_id=%s",
                    (datastream_id, project_id),
                )
                org = cur.fetchone()
            if org is None:
                return _not_found()
            operation = execute_confirmed_operation(
                conn,
                command_type=DATASTREAM_CANDIDATE_PUBLISH_ACTIVATE_COMMAND,
                actor_person_id=actor,
                actor=actor,
                org_id=org[0],
                project_id=project_id,
                resource_id=execution_id,
                content_hash=review["review_hash"],
                idempotency_key=key,
                confirmation_id=body["confirmation_ref"],
                confirmation_secret=body["confirmation_secret"],
                mutation=lambda active_conn, operation_id: publish_activate_mutation(
                    active_conn, review=review, actor=actor, operation_id=operation_id
                ),
            )
            # `conn.transaction()` inside is a SAVEPOINT here, not a transaction:
            # the review was READ first and a SELECT opens the transaction, so
            # releasing the savepoint commits nothing and `get_connection` rolls
            # the whole materialization back. Measured: 201 returned, zero rows.
            conn.commit()
        return _response(
            {
                "operation_ref": operation.operation_id,
                "outcome": operation.outcome,
                **operation.result,
            }
        )
    except Exception as exc:
        return _activation_error(exc)


datastream_preconfiguration_routes = [
    Route(
        "/api/projects/{project_id}/datastream-setup-drafts/{draft_id}/previews",
        endpoint=_create_preview,
        methods=["POST"],
    ),
    Route(
        "/api/projects/{project_id}/datastream-setup-drafts/{draft_id}/preview-jobs/{job_id}",
        endpoint=_read_preview_job,
        methods=["GET"],
    ),
    Route(
        "/api/projects/{project_id}/datastream-setup-drafts/{draft_id}/final-reviews",
        endpoint=_prepare_final_review,
        methods=["POST"],
    ),
    Route(
        "/api/projects/{project_id}/datastream-setup-drafts/{draft_id}/draft-confirmations",
        endpoint=_prepare_draft_confirmation,
        methods=["POST"],
    ),
    Route(
        "/api/projects/{project_id}/datastream-setup-drafts/{draft_id}/materialize",
        endpoint=_confirm_create_draft,
        methods=["POST"],
    ),
    Route(
        "/api/projects/{project_id}/datastream-setup-drafts/{draft_id}/materialization",
        endpoint=_materialization_status,
        methods=["GET"],
    ),
    Route(
        "/api/projects/{project_id}/datastreams/{datastream_id}/executions/{execution_id}/candidate-review",
        endpoint=_candidate_review,
        methods=["GET"],
    ),
    Route(
        "/api/projects/{project_id}/datastreams/{datastream_id}/executions/{execution_id}/publish-confirmations",
        endpoint=_prepare_candidate_confirmation,
        methods=["POST"],
    ),
    Route(
        "/api/projects/{project_id}/datastreams/{datastream_id}/executions/{execution_id}/publish-activate",
        endpoint=_confirm_publish_activate,
        methods=["POST"],
    ),
    Route("/api/projects/{project_id}/datastream-setup-drafts", endpoint=_create, methods=["POST"]),
    Route(
        "/api/projects/{project_id}/datastream-setup-drafts/{draft_id}/source-options",
        endpoint=_source_options,
        methods=["GET"],
    ),
    Route(
        "/api/projects/{project_id}/datastream-setup-drafts/{draft_id}/assets",
        endpoint=_create_asset,
        methods=["POST"],
    ),
    Route(
        "/api/projects/{project_id}/datastream-setup-drafts/{draft_id}/observations",
        endpoint=_create_observation,
        methods=["POST"],
    ),
    Route(
        "/api/projects/{project_id}/datastream-setup-drafts/{draft_id}/observations/{observation_id}",
        endpoint=_read_observation,
        methods=["GET"],
    ),
    Route("/api/projects/{project_id}/datastream-setup-drafts", endpoint=_list, methods=["GET"]),
    Route(
        "/api/projects/{project_id}/datastream-setup-drafts/{draft_id}/compile",
        endpoint=_compile,
        methods=["POST"],
    ),
    Route(
        "/api/projects/{project_id}/datastream-setup-drafts/{draft_id}/proposals/{proposal_id}",
        endpoint=_proposal,
        methods=["GET"],
    ),
    Route(
        "/api/projects/{project_id}/datastream-setup-drafts/{draft_id}",
        endpoint=_read,
        methods=["GET"],
    ),
    Route(
        "/api/projects/{project_id}/datastream-setup-drafts/{draft_id}",
        endpoint=_update,
        methods=["PATCH"],
    ),
    Route(
        "/api/projects/{project_id}/datastream-setup-drafts/{draft_id}",
        endpoint=_discard,
        methods=["DELETE"],
    ),
]
