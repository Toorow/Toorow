"""Thin, fail-closed HTTP routes for the four Project Governance collections.

Three reads and, since Story 49.2, one write: the guarded Master Data node
command. That route holds `edit` where the reads hold `view` -- seeing a registry
must not be enough to retire an identity other workspaces resolve against.

Client-supplied organization, owner, type and version metadata are untrusted:
scope is resolved from the authenticated identity every time, and nothing below
trusts a value that arrived in the URL beyond using it as an opaque lookup key.
"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.db import request_connection
from core.evidence_index import EvidenceCursorInvalid, EvidenceFilterInvalid
from core.governance_read_model import (
    GovernanceObjectNotFound,
    GovernanceUnknownRoute,
    compose_governance_collection,
    compose_governance_object,
    compose_governance_object_version,
)
from core.project_access import resolve_strict_resource_access

logger = logging.getLogger(__name__)


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


def _not_found() -> Response:
    """Existence-hiding. A missing object, a cross-scope object and a Project the
    caller has no grant on are indistinguishable from here, deliberately."""
    return _no_store(
        JSONResponse(
            {"code": "not_found", "message": "Governance object not found"}, status_code=404
        )
    )


def _denied() -> Response:
    """The caller is a known, active member of this organization whose capability
    is below what the read requires. Saying so discloses nothing they do not
    already know about their own membership."""
    return _no_store(
        JSONResponse(
            {
                "code": "denied",
                "message": "Your access to this Project does not include Governance.",
            },
            status_code=403,
        )
    )


def _unavailable(code: str) -> Response:
    return _no_store(
        JSONResponse({"code": code, "message": "Governance is unavailable"}, status_code=503)
    )


def _invalid_query(message: str) -> Response:
    """A cursor or filter outside its contract.

    Refused rather than dropped. A silently ignored filter renders a page that
    does not match the address that produced it, and a cursor that falls back to
    page one turns "show me the next 50" into "show me the first 50 again".
    Neither discloses anything: the caller is already authorized for this
    Project, and the failure is about their own query string.
    """
    return _no_store(JSONResponse({"code": "invalid_query", "message": message}, status_code=400))


#: The only query parameters the collection route reads. Anything else is a
#: refusal, so a forgotten parameter cannot become an accidental contract.
_COLLECTION_QUERY_KEYS = (
    "cursor",
    "limit",
    "from",
    "to",
    "owner_workspace",
    "record_kind",
    "correlation_kind",
    "correlation_id",
    "outcome",
    "q",
    "scope",
)


async def _governance_read(request: Request, shape: str) -> Response:
    from core.admin_api import _check_auth

    authorized, identity = await _check_auth(request)
    if not authorized:
        return _no_store(
            JSONResponse(
                {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
            )
        )

    project_id = (request.path_params.get("project_id") or "").strip()
    section = (request.path_params.get("section") or "").strip()
    object_type = (request.path_params.get("object_type") or "").strip()
    object_id = (request.path_params.get("object_id") or "").strip()
    version_id = (request.path_params.get("version_id") or "").strip()
    lens = (request.query_params.get("lens") or "").strip() or None
    actor = identity or "anonymous"

    query: dict[str, str] = {}
    if shape == "collection":
        for key in _COLLECTION_QUERY_KEYS:
            value = (request.query_params.get(key) or "").strip()
            if value:
                query[key] = value
        unknown = set(request.query_params) - set(_COLLECTION_QUERY_KEYS) - {"lens"}
        if unknown:
            return _invalid_query(f"Unsupported query parameter: {sorted(unknown)[0]}")
        if "limit" in query:
            if not query["limit"].isdigit() or not 1 <= int(query["limit"]) <= 200:
                return _invalid_query("limit must be an integer between 1 and 200")

    try:
        with request_connection(actor) as conn:
            decision = resolve_strict_resource_access(
                actor,
                conn,
                project_id=project_id,
                minimum_capability="view",
                hold_access=True,
            )
            if not decision.allowed:
                if decision.reason == "insufficient_capability":
                    return _denied()
                if decision.reason == "access_unavailable":
                    return _unavailable("governance_access_unavailable")
                return _not_found()
            org_id = decision.org_id or ""
            if not org_id:
                return _not_found()

            if shape == "collection":
                envelope = compose_governance_collection(
                    project_id, section, conn, lens=lens, org_id=org_id, query=query
                )
            elif shape == "object":
                envelope = compose_governance_object(
                    project_id, section, object_type, object_id, conn, org_id=org_id
                )
            else:
                envelope = compose_governance_object_version(
                    project_id, section, object_type, object_id, version_id, conn, org_id=org_id
                )
        return _no_store(JSONResponse(envelope, status_code=200))
    except GovernanceObjectNotFound:
        return _not_found()
    except (EvidenceCursorInvalid, EvidenceFilterInvalid) as exc:
        return _invalid_query(str(exc))
    except (GovernanceUnknownRoute, ValueError):
        # An unregistered section, lens, type or version shape. It is not an
        # object that might exist elsewhere, and it never falls back to a
        # neighbouring lens, the default lens, or the current version.
        return _no_store(
            JSONResponse(
                {"code": "unknown_route", "message": "This Governance address is not registered"},
                status_code=404,
            )
        )
    except Exception as exc:  # fail closed without disclosing resource existence.
        logger.warning(
            "Governance surface unavailable project=%s section=%s shape=%s: %s",
            project_id,
            section,
            shape,
            type(exc).__name__,
        )
        return _unavailable("governance_unavailable")


async def _country_workspace_read(request: Request) -> Response:
    """Read the complete Country Governance editor envelope."""

    from core.admin_api import _check_auth
    from core.country_workspace import load_country_workspace

    authorized, identity = await _check_auth(request)
    if not authorized:
        return _no_store(
            JSONResponse(
                {"code": "unauthorized", "message": "Bearer token required"},
                status_code=401,
            )
        )
    project_id = (request.path_params.get("project_id") or "").strip()
    actor = identity or "anonymous"
    try:
        with request_connection(actor) as conn:
            decision = resolve_strict_resource_access(
                actor,
                conn,
                project_id=project_id,
                minimum_capability="view",
                hold_access=True,
            )
            if not decision.allowed:
                if decision.reason == "insufficient_capability":
                    return _denied()
                if decision.reason == "access_unavailable":
                    return _unavailable("governance_access_unavailable")
                return _not_found()
            if not decision.org_id:
                return _not_found()
            envelope = load_country_workspace(conn, project_id=project_id)
        return _no_store(JSONResponse(envelope, status_code=200))
    except Exception as exc:  # noqa: BLE001 -- fail closed.
        logger.warning(
            "Country workspace unavailable project=%s: %s",
            project_id,
            type(exc).__name__,
        )
        return _unavailable("country_workspace_unavailable")


async def _country_workspace_command(request: Request) -> Response:
    """Run one audited Country preset, hierarchy or publication command."""

    from core.admin_api import _check_auth
    from core.country_workspace import CountryWorkspaceRefused
    from core.country_workspace_commands import (
        CountryWorkspaceImpactRefused,
        prepare_country_publish_confirmation,
        run_country_workspace_command,
    )
    from core.entry_confirmations import EntryConfirmationRefused
    from core.master_data import (
        MasterDataConflict,
        MasterDataNotFound,
        MasterDataUnavailable,
    )
    from core.operations import OperationIdempotencyConflict

    authorized, identity = await _check_auth(request)
    if not authorized:
        return _no_store(
            JSONResponse(
                {"code": "unauthorized", "message": "Bearer token required"},
                status_code=401,
            )
        )
    idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
    if not idempotency_key:
        return _no_store(
            JSONResponse(
                {
                    "code": "idempotency_key_required",
                    "message": "Send an Idempotency-Key header so a retry cannot act twice.",
                },
                status_code=428,
            )
        )
    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _no_store(
            JSONResponse(
                {"code": "invalid_body", "message": "Invalid JSON body"},
                status_code=400,
            )
        )
    if not isinstance(body, dict) or not isinstance(body.get("payload", {}), dict):
        return _no_store(
            JSONResponse(
                {"code": "invalid_body", "message": "Body and payload must be objects"},
                status_code=400,
            )
        )

    project_id = (request.path_params.get("project_id") or "").strip()
    actor = identity or "anonymous"
    requested_action = str(body.get("action") or "")
    try:
        with request_connection(actor) as conn:
            decision = resolve_strict_resource_access(
                actor,
                conn,
                project_id=project_id,
                minimum_capability=(
                    "manage" if requested_action in {"prepare_publish", "publish"} else "edit"
                ),
                hold_access=True,
            )
            if not decision.allowed:
                if decision.reason == "insufficient_capability":
                    return _denied()
                if decision.reason == "access_unavailable":
                    return _unavailable("governance_access_unavailable")
                return _not_found()
            org_id = decision.org_id or ""
            if not org_id:
                return _not_found()
            action = requested_action
            command_payload = body.get("payload") or {}
            if action == "prepare_publish":
                result = prepare_country_publish_confirmation(
                    conn,
                    project_id=project_id,
                    org_id=org_id,
                    actor=actor,
                    idempotency_key=idempotency_key,
                    payload=command_payload,
                )
            else:
                result = run_country_workspace_command(
                    conn,
                    project_id=project_id,
                    org_id=org_id,
                    actor=actor,
                    idempotency_key=idempotency_key,
                    action=action,
                    payload=command_payload,
                    trace_id=request.headers.get("X-Trace-Id"),
                )
            conn.commit()
        return _no_store(JSONResponse(result, status_code=200))
    except CountryWorkspaceImpactRefused as exc:
        return _no_store(
            JSONResponse(
                {
                    "code": exc.code,
                    "message": str(exc),
                    "impacts": exc.impacts,
                },
                status_code=409,
            )
        )
    except EntryConfirmationRefused as exc:
        return _no_store(
            JSONResponse(
                {"code": exc.code, "message": "Publication confirmation is invalid or expired."},
                status_code=409,
            )
        )
    except (CountryWorkspaceRefused, ValueError) as exc:
        return _no_store(
            JSONResponse(
                {
                    "code": getattr(exc, "code", "invalid_input"),
                    "message": str(exc),
                },
                status_code=422,
            )
        )
    except MasterDataConflict as exc:
        return _no_store(
            JSONResponse(
                {"code": "country_workspace_conflict", "message": str(exc)},
                status_code=409,
            )
        )
    except MasterDataNotFound:
        return _not_found()
    except MasterDataUnavailable:
        return _unavailable("country_workspace_evidence_unavailable")
    except OperationIdempotencyConflict:
        return _no_store(
            JSONResponse(
                {
                    "code": "idempotency_conflict",
                    "message": "This idempotency key is bound to another command.",
                },
                status_code=409,
            )
        )
    except Exception as exc:  # noqa: BLE001 -- fail closed.
        logger.warning(
            "Country workspace command unavailable project=%s: %s",
            project_id,
            type(exc).__name__,
        )
        return _unavailable("country_workspace_unavailable")


async def _master_data_node_command(request: Request) -> Response:
    """POST one guarded Master Data node command (Story 49.2).

    The first write route on this surface, which until now was read-only: the
    guard and the audit lived in `core.master_data` / `core.master_data_commands`
    and nothing could reach them. A command whose only caller is a test is the
    defect this repository keeps paying for.

    Authorization is `edit`, not `view`. The read routes above hold `view`, and
    reusing that here would have let anyone who can see the registry retire an
    identity other workspaces resolve against.
    """
    from core.admin_api import _check_auth
    from core.master_data import MasterDataNotFound, MasterDataUnavailable
    from core.master_data_commands import MasterDataCommandRefused, run_node_command

    authorized, identity = await _check_auth(request)
    if not authorized:
        return _no_store(
            JSONResponse(
                {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
            )
        )

    project_id = (request.path_params.get("project_id") or "").strip()
    node_id = (request.path_params.get("node_id") or "").strip()
    actor = identity or "anonymous"

    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _no_store(
            JSONResponse({"code": "invalid_body", "message": "Invalid JSON body"}, status_code=400)
        )
    if not isinstance(body, dict):
        return _no_store(
            JSONResponse({"code": "invalid_body", "message": "Body must be an object"}, 400)
        )

    # THE RENAME STATES THE BASE IT RENAMES FROM, and the door is where that is
    # made unskippable (`governance.md`, amendment of 2026-08-30). The key must be
    # PRESENT; its value may be null, which states "the object I read had no
    # published revision". A rename with no stated base is refused here rather
    # than run without a precondition -- 428, the same answer as the missing
    # idempotency key, because both are preconditions this door requires.
    if str(body.get("action") or "").strip() == "rename" and "expected_version" not in body:
        return _no_store(
            JSONResponse(
                {
                    "code": "expected_version_required",
                    "message": (
                        "Send expected_version -- the version identity you read -- so a "
                        "rename cannot overwrite a change made since."
                    ),
                },
                status_code=428,
            )
        )
    stated_version = body.get("expected_version")
    if stated_version is not None and not isinstance(stated_version, str):
        return _no_store(
            JSONResponse(
                {
                    "code": "invalid_param",
                    "message": "expected_version must be a version identity or null",
                },
                status_code=400,
            )
        )

    idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
    if not idempotency_key:
        # Required, never generated here: a key minted server-side makes every
        # retry a new command, which is the opposite of what it is for.
        return _no_store(
            JSONResponse(
                {
                    "code": "idempotency_key_required",
                    "message": "Send an Idempotency-Key header so a retry cannot act twice.",
                },
                status_code=428,
            )
        )

    try:
        with request_connection(actor) as conn:
            decision = resolve_strict_resource_access(
                actor,
                conn,
                project_id=project_id,
                minimum_capability="edit",
                hold_access=True,
            )
            if not decision.allowed:
                if decision.reason == "insufficient_capability":
                    return _denied()
                if decision.reason == "access_unavailable":
                    return _unavailable("governance_access_unavailable")
                return _not_found()
            org_id = decision.org_id or ""
            if not org_id:
                return _not_found()

            result = run_node_command(
                conn,
                project_id=project_id,
                org_id=org_id,
                node_id=node_id,
                action=str(body.get("action") or ""),
                actor=actor,
                idempotency_key=idempotency_key,
                acknowledge_impact=bool(body.get("acknowledge_impact")),
                # A rename carries the three fields that live in ONE version
                # payload. Publishing a version that kept only the label would
                # erase the other two, which is not what a rename does.
                label=(str(body.get("label")).strip() if body.get("label") else None),
                description=(
                    str(body.get("description")) if body.get("description") is not None else None
                ),
                classification_type=(
                    str(body.get("classification_type")).strip()
                    if body.get("classification_type")
                    else None
                ),
                # Passed only when the caller STATED it: the command tells the two
                # cases apart, and defaulting it here would turn "I forgot" into
                # "I read no revision" -- a precondition that cannot fire.
                **({"expected_version": stated_version} if "expected_version" in body else {}),
                reason=str(body.get("reason") or ""),
                trace_id=request.headers.get("X-Trace-Id"),
            )
            conn.commit()
        return _no_store(JSONResponse(result, status_code=200))
    except MasterDataCommandRefused as exc:
        # 409, and the named consumers travel with it: the operator has to be
        # able to see WHAT would break before deciding to acknowledge it.
        return _no_store(JSONResponse(exc.as_dict(), status_code=409))
    except MasterDataUnavailable:
        # Fail closed. "I could not check" must never render as "nothing depends
        # on this", so it is a 503 and not a success.
        return _unavailable("master_data_evidence_unavailable")
    except MasterDataNotFound:
        return _not_found()
    except ValueError as exc:
        return _no_store(JSONResponse({"code": "invalid_input", "message": str(exc)}, 422))
    except Exception as exc:  # fail closed without disclosing resource existence.
        logger.warning(
            "Master Data command unavailable project=%s node=%s: %s",
            project_id,
            node_id,
            type(exc).__name__,
        )
        return _unavailable("governance_unavailable")


async def _master_data_identity_create(request: Request) -> Response:
    """POST one new business identity into the authority (cutover of 2026-08-25).

    THE DOOR THE REFUSAL NAMED. `business_taxonomy`'s four identity writers refuse
    with one sentence -- *converge this organization, then create, rename or
    archive it in Master Data* -- and until this route existed the second half of
    that sentence pointed at nothing: `SUPPORTED_ACTIONS` held `archive` and
    `restore` only, so a converged organization could not declare a Business
    Domain anywhere at all.

    It is a SEPARATE route from the node command below because there is no node
    to address yet. Same capability as that command (`edit`), not `manage`:
    declaring an identity is an edit of the registry, where converging a whole
    organization's taxonomy is not.
    """
    from core.admin_api import _check_auth
    from core.master_data import MasterDataNotFound, MasterDataUnavailable
    from core.master_data_commands import MasterDataCommandRefused, create_business_identity
    from core.operations import OperationIdempotencyConflict

    authorized, identity = await _check_auth(request)
    if not authorized:
        return _no_store(
            JSONResponse(
                {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
            )
        )

    project_id = (request.path_params.get("project_id") or "").strip()
    actor = identity or "anonymous"

    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _no_store(
            JSONResponse({"code": "invalid_body", "message": "Invalid JSON body"}, status_code=400)
        )
    if not isinstance(body, dict):
        return _no_store(
            JSONResponse({"code": "invalid_body", "message": "Body must be an object"}, 400)
        )

    idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
    if not idempotency_key:
        return _no_store(
            JSONResponse(
                {
                    "code": "idempotency_key_required",
                    "message": "Send an Idempotency-Key header so a retry cannot act twice.",
                },
                status_code=428,
            )
        )

    try:
        with request_connection(actor) as conn:
            decision = resolve_strict_resource_access(
                actor,
                conn,
                project_id=project_id,
                minimum_capability="edit",
                hold_access=True,
            )
            if not decision.allowed:
                if decision.reason == "insufficient_capability":
                    return _denied()
                if decision.reason == "access_unavailable":
                    return _unavailable("governance_access_unavailable")
                return _not_found()
            org_id = decision.org_id or ""
            if not org_id:
                return _not_found()

            result = create_business_identity(
                conn,
                project_id=project_id,
                org_id=org_id,
                kind=str(body.get("kind") or ""),
                name=str(body.get("name") or ""),
                slug=(str(body.get("slug")).strip() if body.get("slug") else None),
                description=str(body.get("description") or ""),
                owner=(str(body.get("owner")).strip() if body.get("owner") else None),
                classification_type=(
                    str(body.get("classification_type")).strip()
                    if body.get("classification_type")
                    else None
                ),
                domain_node_id=(
                    str(body.get("domain_node_id")).strip()
                    if body.get("domain_node_id")
                    else None
                ),
                parent_node_id=(
                    str(body.get("parent_node_id")).strip()
                    if body.get("parent_node_id")
                    else None
                ),
                actor=actor,
                idempotency_key=idempotency_key,
                reason=str(body.get("reason") or ""),
                trace_id=request.headers.get("X-Trace-Id"),
            )
            conn.commit()
        return _no_store(JSONResponse(result, status_code=200))
    except MasterDataCommandRefused as exc:
        # 409 carrying its own code: the console tells the convergence refusal
        # from a duplicate short code, and the two are repaired by different
        # people on different screens.
        return _no_store(JSONResponse(exc.as_dict(), status_code=409))
    except MasterDataUnavailable:
        return _unavailable("master_data_evidence_unavailable")
    except MasterDataNotFound:
        return _not_found()
    except OperationIdempotencyConflict:
        return _no_store(
            JSONResponse(
                {
                    "code": "idempotency_conflict",
                    "message": "This idempotency key is bound to another command.",
                },
                status_code=409,
            )
        )
    except ValueError as exc:
        return _no_store(JSONResponse({"code": "invalid_input", "message": str(exc)}, 422))
    except Exception as exc:  # fail closed without disclosing resource existence.
        logger.warning(
            "Master Data creation unavailable project=%s: %s",
            project_id,
            type(exc).__name__,
        )
        return _unavailable("governance_unavailable")


async def _semantic_metric_presets(request: Request) -> Response:
    """GET the preconfigured calculated metrics this Project could adopt.

    A READ, and only a read -- which is why it lives here and not beside the four
    change-set routes of `semantic_model_api.py`, whose own header says nothing
    there returns a collection. It writes nothing and offers nothing it has not
    resolved: each preset comes back either with the exact `create_concept`
    intent the governed change set takes, or with the name of the Concept to
    declare before it can be offered at all.

    `view`, the same floor as every other read on this surface. Seeing what could
    be adopted is not adopting it; adoption goes through the change-set routes,
    which hold `edit`.
    """
    from core.admin_api import _check_auth
    from core.semantic_metric_presets import presets_for_project

    authorized, identity = await _check_auth(request)
    if not authorized:
        return _no_store(
            JSONResponse(
                {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
            )
        )
    project_id = (request.path_params.get("project_id") or "").strip()
    actor = identity or "anonymous"
    try:
        with request_connection(actor) as conn:
            decision = resolve_strict_resource_access(
                actor,
                conn,
                project_id=project_id,
                minimum_capability="view",
                hold_access=True,
            )
            if not decision.allowed:
                if decision.reason == "insufficient_capability":
                    return _denied()
                if decision.reason == "access_unavailable":
                    return _unavailable("governance_access_unavailable")
                return _not_found()
            return _no_store(
                JSONResponse(presets_for_project(conn, project_id), status_code=200)
            )
    except Exception as exc:  # fail closed without disclosing resource existence.
        logger.warning(
            "Semantic metric presets unavailable project=%s: %s: %s",
            project_id,
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        return _unavailable("governance_unavailable")


async def _master_data_convergence_plan(request: Request) -> Response:
    """GET what an organization's convergence would move (Story 49.2, AC1)."""

    return await _master_data_convergence(request, writing=False)


async def _master_data_convergence_command(request: Request) -> Response:
    """POST the convergence of an organization's business taxonomy (AC1)."""

    return await _master_data_convergence(request, writing=True)


async def _master_data_convergence(request: Request, *, writing: bool) -> Response:
    """The plan and the command, one implementation, TWO registered routes.

    The read and the write are registered separately -- as the Country pair
    above already is -- because `test_the_reads_are_read_only` states the rule
    this surface works under: a route that answers both a projection and a
    command makes the capability required depend on the verb rather than on the
    address. Here the two genuinely differ: `view` to see what would move,
    `manage` to move it.

    THE PLAN IS NOT A CONVENIENCE. The console has to be able to say what this
    gesture WOULD do before an operator asks for it; a command whose only way to
    be understood is to run it is a command nobody can consent to.

    Authorization for the write is `manage`, one step above the node command's
    `edit`. AC11 is explicit -- *"organization-master writes require
    organization manage permission"* -- and what moves here is the
    organization's whole business taxonomy, not one Project's registry. The
    organization is resolved FROM the authorized Project and never read from the
    body.
    """
    from core.admin_api import _check_auth
    from core.master_data import MasterDataError, MasterDataUnavailable
    from core.master_data_convergence import (
        ConvergenceRefused,
        converge_org_taxonomy,
        plan_convergence,
    )

    authorized, identity = await _check_auth(request)
    if not authorized:
        return _no_store(
            JSONResponse(
                {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
            )
        )

    project_id = (request.path_params.get("project_id") or "").strip()
    actor = identity or "anonymous"

    body: dict[str, object] = {}
    idempotency_key = ""
    if writing:
        try:
            parsed = json.loads(await request.body() or b"{}")
        except (ValueError, TypeError):
            return _no_store(
                JSONResponse(
                    {"code": "invalid_body", "message": "Invalid JSON body"}, status_code=400
                )
            )
        if not isinstance(parsed, dict):
            return _no_store(
                JSONResponse({"code": "invalid_body", "message": "Body must be an object"}, 400)
            )
        body = parsed
        idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
        if not idempotency_key:
            return _no_store(
                JSONResponse(
                    {
                        "code": "idempotency_key_required",
                        "message": (
                            "Send an Idempotency-Key header so a retry cannot act twice."
                        ),
                    },
                    status_code=428,
                )
            )

    try:
        with request_connection(actor) as conn:
            decision = resolve_strict_resource_access(
                actor,
                conn,
                project_id=project_id,
                minimum_capability="manage" if writing else "view",
                hold_access=True,
            )
            if not decision.allowed:
                if decision.reason == "insufficient_capability":
                    return _denied()
                if decision.reason == "access_unavailable":
                    return _unavailable("governance_access_unavailable")
                return _not_found()
            org_id = decision.org_id or ""
            if not org_id:
                return _not_found()

            if not writing:
                plan = plan_convergence(conn, org_id=org_id)
                return _no_store(
                    JSONResponse(
                        {**plan.as_dict(), "summary": plan.describe()}, status_code=200
                    )
                )

            result = converge_org_taxonomy(
                conn,
                org_id=org_id,
                project_id=project_id,
                actor=actor,
                idempotency_key=idempotency_key,
                reason=str(body.get("reason") or ""),
                trace_id=request.headers.get("X-Trace-Id"),
            )
            # REQUIRED, and it was missing. `db.get_connection` does not commit
            # -- it closes, and psycopg rolls an open transaction back on close
            # (`db.py:67-81`). Every write route on this surface said "converged
            # 6 domains" and left the database untouched. The Country command
            # beside it has always committed; these two had not.
            conn.commit()
        return _no_store(JSONResponse(result, status_code=200))
    except ConvergenceRefused as exc:
        # 409, carrying what could not be reached. An operator has to see WHICH
        # branch would have been dropped before deciding anything.
        return _no_store(JSONResponse(exc.as_dict(), status_code=409))
    except MasterDataUnavailable:
        return _unavailable("master_data_evidence_unavailable")
    except MasterDataError as exc:
        return _no_store(JSONResponse({"code": "invalid_input", "message": str(exc)}, 422))
    except Exception as exc:  # fail closed without disclosing resource existence.
        logger.warning(
            "Master Data convergence unavailable project=%s: %s",
            project_id,
            type(exc).__name__,
        )
        return _unavailable("governance_unavailable")


def _route(path: str, shape: str, name: str) -> Route:
    async def endpoint(request: Request) -> Response:
        return await _governance_read(request, shape)

    return Route(path, endpoint=endpoint, methods=["GET"], name=name)


GOVERNANCE_SURFACE_ROUTES = [
    Route(
        "/api/projects/{project_id}/governance/master-data/country",
        endpoint=_country_workspace_read,
        methods=["GET"],
        name="governance-country-workspace-read",
    ),
    Route(
        "/api/projects/{project_id}/governance/master-data/country",
        endpoint=_country_workspace_command,
        methods=["POST"],
        name="governance-country-workspace-command",
    ),
    # Exact Country routes must precede the generic section route below.
    # Node lifecycle likewise precedes generic object reads.
    # The one write route on this surface. It sits first so the read routes
    # below cannot shadow it: `/objects/{object_type}/{object_id}` would match
    # this path shape on a GET, and the two must stay distinguishable.
    Route(
        "/api/projects/{project_id}/governance/master-data/nodes/{node_id}/commands",
        endpoint=_master_data_node_command,
        methods=["POST"],
        name="governance-master-data-node-command",
    ),
    # The creation door. It sits beside the command above because it is the same
    # authority reached one step earlier: there is no node id to address until
    # this route mints one.
    Route(
        "/api/projects/{project_id}/governance/master-data/nodes",
        endpoint=_master_data_identity_create,
        methods=["POST"],
        name="governance-master-data-identity-create",
    ),
    # Exact path, so it precedes `/governance/{section}` for the same reason the
    # Country routes do: the generic collection route would match `master-data`
    # and swallow this one on a GET. Two Route objects rather than one with both
    # verbs -- see `_master_data_convergence`.
    Route(
        "/api/projects/{project_id}/governance/master-data/convergence",
        endpoint=_master_data_convergence_plan,
        methods=["GET"],
        name="governance-master-data-convergence-plan",
    ),
    Route(
        "/api/projects/{project_id}/governance/master-data/convergence",
        endpoint=_master_data_convergence_command,
        methods=["POST"],
        name="governance-master-data-convergence-command",
    ),
    # Exact path, and it must precede the generic object/collection routes for
    # the same reason the Country and convergence routes do.
    Route(
        "/api/projects/{project_id}/governance/semantic-model/metric-presets",
        endpoint=_semantic_metric_presets,
        methods=["GET"],
        name="governance-semantic-metric-presets",
    ),
    _route(
        "/api/projects/{project_id}/governance/{section}/objects/{object_type}/{object_id}/versions/{version_id}",
        "version",
        "governance-object-version",
    ),
    _route(
        "/api/projects/{project_id}/governance/{section}/objects/{object_type}/{object_id}",
        "object",
        "governance-object",
    ),
    _route(
        "/api/projects/{project_id}/governance/{section}", "collection", "governance-collection"
    ),
]
